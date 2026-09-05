import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.ui.shared_auth import (
    COOKIE_NAME,
    ClassroomAuth,
    SharedAuth,
    SharedAuthConfigurationError,
    SharedLoginCredentials,
    _main,
    write_credentials,
)


class SharedAuthTests(unittest.TestCase):
    def test_cli_rejects_short_password_without_traceback_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            credentials_path = Path(tmp) / "credentials.json"
            stderr = io.StringIO()
            with (
                patch("getpass.getpass", return_value="too-short"),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                _main([str(credentials_path), "--username", "students"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("at least 12 characters", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(credentials_path.exists())

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


class ClassroomAuthTests(unittest.TestCase):
    def _auth(self, root: Path, **overrides):
        options = {
            "database_path": root / "classroom.sqlite3",
            "admin_password": "admin-correct-horse-battery-staple",
            "invitation_code": "class-invitation-2026",
            "registration_enabled": True,
        }
        options.update(overrides)
        return ClassroomAuth(**options)

    def test_invited_student_registers_and_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = self._auth(root)
            token = auth.register(
                username="Student One",
                display_name="Alice",
                password="student-password-123",
                invitation_code="class-invitation-2026",
                client_key="client-a",
            )
            principal = auth.principal_from_token(token)
            self.assertIsNotNone(principal)
            self.assertEqual(principal.username, "Student One")
            self.assertEqual(principal.display_name, "Alice")
            self.assertEqual(principal.role, "student")
            self.assertNotIn(token, auth.database_path.read_bytes().decode("latin-1"))

            # Closing registration changes only account creation; existing
            # students must still be able to return after a restart.
            restarted = self._auth(root, registration_enabled=False)
            login_token = restarted.login(
                username="student one",
                password="student-password-123",
                client_key="client-b",
            )
            recovered = restarted.principal_from_token(login_token or "")
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.account_id, principal.account_id)
            self.assertFalse(restarted.registration_enabled)

    def test_registration_requires_enabled_valid_invitation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disabled = self._auth(root, registration_enabled=False)
            with self.assertRaisesRegex(PermissionError, "closed"):
                disabled.register(
                    username="student",
                    display_name="",
                    password="student-password-123",
                    invitation_code="class-invitation-2026",
                    client_key="client-a",
                )
            enabled = self._auth(root)
            with self.assertRaisesRegex(PermissionError, "invalid"):
                enabled.register(
                    username="student",
                    display_name="",
                    password="student-password-123",
                    invitation_code="wrong-invitation-code",
                    client_key="client-a",
                )

    def test_admin_cannot_register_and_uses_startup_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = self._auth(root)
            with self.assertRaisesRegex(ValueError, "reserved"):
                auth.register(
                    username="admin",
                    display_name="",
                    password="student-password-123",
                    invitation_code="class-invitation-2026",
                    client_key="client-a",
                )
            token = auth.login(
                username="admin",
                password="admin-correct-horse-battery-staple",
                client_key="client-a",
            )
            principal = auth.principal_from_token(token or "")
            self.assertIsNotNone(principal)
            self.assertEqual(principal.role, "admin")

            restarted = self._auth(
                root, admin_password="new-admin-correct-horse-battery-staple"
            )
            self.assertIsNone(restarted.principal_from_token(token or ""))
            self.assertIsNone(
                restarted.login(
                    username="admin",
                    password="admin-correct-horse-battery-staple",
                    client_key="client-b",
                )
            )
            self.assertTrue(
                restarted.login(
                    username="admin",
                    password="new-admin-correct-horse-battery-staple",
                    client_key="client-c",
                )
            )

    def test_account_limit_and_duplicate_username_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = self._auth(root, max_accounts=1)
            auth.register(
                username="student-one",
                display_name="",
                password="student-password-123",
                invitation_code="class-invitation-2026",
                client_key="client-a",
            )
            with self.assertRaisesRegex(RuntimeError, "account limit"):
                auth.register(
                    username="student-two",
                    display_name="",
                    password="student-password-456",
                    invitation_code="class-invitation-2026",
                    client_key="client-b",
                )

            duplicate_auth = self._auth(root, max_accounts=2)
            with self.assertRaisesRegex(ValueError, "already registered"):
                duplicate_auth.register(
                    username="STUDENT-ONE",
                    display_name="",
                    password="student-password-456",
                    invitation_code="class-invitation-2026",
                    client_key="client-c",
                )

    def test_asset_ownership_is_private_and_admin_can_read_legacy_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._auth(Path(tmp))
            first_token = auth.register(
                username="student-one",
                display_name="",
                password="student-password-123",
                invitation_code="class-invitation-2026",
                client_key="client-a",
            )
            second_token = auth.register(
                username="student-two",
                display_name="",
                password="student-password-456",
                invitation_code="class-invitation-2026",
                client_key="client-b",
            )
            first = auth.principal_from_token(first_token)
            second = auth.principal_from_token(second_token)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            auth.claim_asset(
                asset_type="workspace",
                asset_id="ws-one",
                principal=first,
            )
            self.assertTrue(
                auth.can_access_asset(
                    asset_type="workspace", asset_id="ws-one", principal=first
                )
            )
            self.assertFalse(
                auth.can_access_asset(
                    asset_type="workspace", asset_id="ws-one", principal=second
                )
            )
            with self.assertRaises(PermissionError):
                auth.claim_asset(
                    asset_type="workspace",
                    asset_id="ws-one",
                    principal=second,
                )
            admin_token = auth.login(
                username="admin",
                password="admin-correct-horse-battery-staple",
                client_key="client-admin",
            )
            admin = auth.principal_from_token(admin_token or "")
            self.assertIsNotNone(admin)
            self.assertTrue(
                auth.can_access_asset(
                    asset_type="workspace", asset_id="legacy-unclaimed", principal=admin
                )
            )

    def test_catalog_visibility_is_controlled_by_owner_or_admin_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._auth(Path(tmp))
            first_token = auth.register(
                username="catalog-owner",
                display_name="",
                password="catalog-owner-password-123",
                invitation_code="class-invitation-2026",
                client_key="client-owner",
            )
            second_token = auth.register(
                username="catalog-reader",
                display_name="",
                password="catalog-reader-password-456",
                invitation_code="class-invitation-2026",
                client_key="client-reader",
            )
            owner = auth.principal_from_token(first_token)
            reader = auth.principal_from_token(second_token)
            self.assertIsNotNone(owner)
            self.assertIsNotNone(reader)
            asset_id = "catalog-entry/example"
            auth.claim_asset(
                asset_type="catalog-entry",
                asset_id=asset_id,
                principal=owner,
            )

            with self.assertRaisesRegex(PermissionError, "owner or admin"):
                auth.set_catalog_entry_visibility(
                    asset_id=asset_id,
                    visibility="classroom",
                    principal=reader,
                )
            auth.set_catalog_entry_visibility(
                asset_id=asset_id,
                visibility="classroom",
                principal=owner,
            )
            self.assertTrue(
                auth.can_access_asset(
                    asset_type="catalog-entry",
                    asset_id=asset_id,
                    principal=reader,
                )
            )
            events = auth.asset_visibility_events(
                asset_type="catalog-entry", asset_id=asset_id
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["actor_account_id"], owner.account_id)
            self.assertEqual(events[0]["previous_visibility"], "private")
            self.assertEqual(events[0]["visibility"], "classroom")

            admin_token = auth.login(
                username="admin",
                password="admin-correct-horse-battery-staple",
                client_key="client-admin",
            )
            admin = auth.principal_from_token(admin_token or "")
            self.assertIsNotNone(admin)
            auth.set_catalog_entry_visibility(
                asset_id=asset_id,
                visibility="private",
                principal=admin,
            )
            self.assertFalse(
                auth.can_access_asset(
                    asset_type="catalog-entry",
                    asset_id=asset_id,
                    principal=reader,
                )
            )

    def test_admin_can_adopt_legacy_catalog_entry_without_hiding_it_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = self._auth(Path(tmp))
            admin_token = auth.login(
                username="admin",
                password="admin-correct-horse-battery-staple",
                client_key="client-admin",
            )
            admin = auth.principal_from_token(admin_token or "")
            self.assertIsNotNone(admin)

            auth.set_catalog_entry_visibility(
                asset_id="catalog-entry/legacy",
                visibility="private",
                principal=admin,
            )
            ownership = auth.asset_ownership(
                asset_type="catalog-entry", asset_id="catalog-entry/legacy"
            )
            self.assertEqual(ownership["owner_account_id"], admin.account_id)
            self.assertEqual(ownership["visibility"], "private")
            events = auth.asset_visibility_events(
                asset_type="catalog-entry", asset_id="catalog-entry/legacy"
            )
            self.assertEqual(events[0]["previous_visibility"], "classroom")


if __name__ == "__main__":
    unittest.main()
