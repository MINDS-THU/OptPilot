import json
import os
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.shared_auth import (
    COOKIE_NAME,
    SharedAuth,
    SharedAuthConfigurationError,
    SharedLoginCredentials,
    write_credentials,
)


class SharedAuthTests(unittest.TestCase):
    def test_credentials_and_sessions_survive_restart_without_storing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials_path = root / "credentials.json"
            database_path = root / "sessions.sqlite3"
            write_credentials(
                credentials_path,
                username="students",
                password="correct horse battery staple",
            )
            self.assertEqual(credentials_path.stat().st_mode & 0o777, 0o600)
            auth = SharedAuth.from_files(
                credentials_path=credentials_path,
                database_path=database_path,
            )
            self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(
                auth.login(username="students", password="wrong-password", client_key="a")
            )
            token = auth.login(
                username="students",
                password="correct horse battery staple",
                client_key="a",
            )
            self.assertTrue(token)
            self.assertNotIn(token, database_path.read_bytes().decode("latin-1"))
            restarted = SharedAuth.from_files(
                credentials_path=credentials_path,
                database_path=database_path,
            )
            cookie = f"unrelated=1; {COOKIE_NAME}={token}"
            self.assertTrue(restarted.verify_cookie(cookie))
            restarted.logout_cookie(cookie)
            self.assertFalse(restarted.verify_cookie(cookie))

    def test_expired_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials_path = root / "credentials.json"
            write_credentials(credentials_path, username="u", password="long-enough-password")
            now = [1000.0]
            auth = SharedAuth(
                credentials=SharedLoginCredentials.load(credentials_path),
                database_path=root / "sessions.sqlite3",
                session_ttl_seconds=300,
                clock=lambda: now[0],
            )
            token = auth.login(username="u", password="long-enough-password", client_key="a")
            self.assertTrue(auth.verify_token(token or ""))
            now[0] += 301
            self.assertFalse(auth.verify_token(token or ""))

    def test_credentials_file_permissions_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            write_credentials(path, username="u", password="long-enough-password")
            os.chmod(path, 0o644)
            with self.assertRaises(SharedAuthConfigurationError):
                SharedLoginCredentials.load(path)

    def test_cookie_has_required_browser_security_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "credentials.json"
            write_credentials(path, username="u", password="long-enough-password")
            auth = SharedAuth.from_files(
                credentials_path=path,
                database_path=root / "sessions.sqlite3",
            )
            cookie = auth.session_cookie("opaque")
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("Path=/", cookie)

    def test_credentials_do_not_contain_plaintext_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            password = "this-password-must-not-be-stored"
            write_credentials(path, username="u", password=password)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(password, path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "optpilot.shared-login-credentials.v1")

    def test_unicode_username_can_authenticate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "credentials.json"
            write_credentials(path, username="学生", password="long-enough-password")
            auth = SharedAuth.from_files(
                credentials_path=path,
                database_path=root / "sessions.sqlite3",
            )
            self.assertTrue(
                auth.login(
                    username="学生",
                    password="long-enough-password",
                    client_key="a",
                )
            )

    def test_credential_writer_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("retain me", encoding="utf-8")
            link = root / "credentials.json"
            link.symlink_to(target)
            with self.assertRaises(SharedAuthConfigurationError):
                write_credentials(
                    link,
                    username="students",
                    password="long-enough-password",
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "retain me")


if __name__ == "__main__":
    unittest.main()
