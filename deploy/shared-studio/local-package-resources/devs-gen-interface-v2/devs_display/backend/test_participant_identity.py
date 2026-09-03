import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from devs_display.backend.participant_identity import PARTICIPANT_COOKIE_NAME
from devs_display.backend.routes import create_app
from devs_display.backend.server import DEVSBackendService


class _UnusedAgent:
    tools = []

    def run(self, prompt, reset=False):
        return "unused"


class ParticipantIdentityTests(unittest.TestCase):
    def setUp(self):
        self._auth = patch.dict(
            os.environ,
            {
                "DEVS_DISPLAY_PASSWORD": "",
                "DEVS_DISPLAY_IDENTITY_MODE": "cookie",
                "DEVS_DISPLAY_PARTICIPANT_COOKIE_SECURE": "0",
            },
            clear=False,
        )
        self._auth.start()
        self.addCleanup(self._auth.stop)

    def _service(self, root: str) -> DEVSBackendService:
        return DEVSBackendService(
            _UnusedAgent(),
            root,
            start_worker=False,
            registry_path=str(Path(root) / "data" / "session-registry.json"),
        )

    def test_cookie_identity_is_durable_and_session_access_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            app = create_app(service)

            with TestClient(app) as alice, TestClient(app) as bob:
                first = alice.get("/sessions")
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json()["sessions"], [])
                cookie_header = first.headers.get("set-cookie", "")
                self.assertIn(PARTICIPANT_COOKIE_NAME, cookie_header)
                self.assertIn("HttpOnly", cookie_header)
                self.assertIn("SameSite=lax", cookie_header)

                created = alice.post(
                    "/sessions",
                    json={
                        "title": "Alice design",
                        "clone_projects": [],
                        "client_id": "participant_spoofed-by-browser",
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                alice_session = created.json()["session"]
                self.assertNotEqual(
                    alice_session["owner_client_id"],
                    "participant_spoofed-by-browser",
                )

                alice_list = alice.get("/sessions?include_all=true")
                self.assertEqual(
                    [row["session_id"] for row in alice_list.json()["sessions"]],
                    [alice_session["session_id"]],
                )
                self.assertEqual(bob.get("/sessions").json()["sessions"], [])
                denied = bob.get(f"/sessions/{alice_session['session_id']}")
                self.assertEqual(denied.status_code, 404, denied.text)

                bob_created = bob.post(
                    "/sessions",
                    json={"title": "Bob design", "clone_projects": []},
                )
                self.assertEqual(bob_created.status_code, 200, bob_created.text)
                bob_session = bob_created.json()["session"]
                clone_denied = alice.post(
                    f"/sessions/{alice_session['session_id']}/projects:clone",
                    json={
                        "clone_projects": [
                            {
                                "source_session_id": bob_session["session_id"],
                                "source_project_id": "proj_not-visible",
                            }
                        ]
                    },
                )
                self.assertEqual(clone_denied.status_code, 404, clone_denied.text)

                alice_cookie = alice.cookies.get(PARTICIPANT_COOKIE_NAME)
                self.assertTrue(alice_cookie)

            # The signing key and participant registry live under the stable
            # data root, so closing and reopening the browser can resume.
            with TestClient(create_app(service)) as resumed:
                resumed.cookies.set(PARTICIPANT_COOKIE_NAME, alice_cookie)
                sessions = resumed.get("/sessions")
                self.assertEqual(sessions.status_code, 200, sessions.text)
                self.assertEqual(
                    [row["session_id"] for row in sessions.json()["sessions"]],
                    [alice_session["session_id"]],
                )

            database_path = Path(tmp) / "data" / "participants.sqlite3"
            secret_path = Path(tmp) / "data" / ".participant-cookie-secret"
            self.assertTrue(database_path.is_file())
            self.assertTrue(secret_path.is_file())
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)

    def test_tampered_cookie_gets_a_new_empty_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            with TestClient(create_app(service)) as client:
                client.get("/sessions")
                created = client.post(
                    "/sessions",
                    json={"title": "Private design", "clone_projects": []},
                )
                self.assertEqual(created.status_code, 200, created.text)
                token = client.cookies.get(PARTICIPANT_COOKIE_NAME)
                client.cookies.set(PARTICIPANT_COOKIE_NAME, f"{token}tampered")
                self.assertEqual(client.get("/sessions").json()["sessions"], [])

    def test_launch_identity_uses_the_runtime_root_without_setting_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            launch_a_root = str(Path(tmp) / "launch-a")
            launch_b_root = str(Path(tmp) / "launch-b")
            with patch.dict(
                os.environ,
                {"DEVS_DISPLAY_IDENTITY_MODE": "launch"},
                clear=False,
            ):
                service_a = self._service(launch_a_root)
                service_b = self._service(launch_b_root)
                with TestClient(create_app(service_a)) as launch_a, TestClient(
                    create_app(service_b)
                ) as launch_b:
                    # A stale standalone cookie is ignored and never replaced.
                    launch_a.cookies.set(PARTICIPANT_COOKIE_NAME, "stale-cookie")
                    first_a = launch_a.get("/sessions")
                    self.assertEqual(first_a.status_code, 200, first_a.text)
                    self.assertNotIn("set-cookie", first_a.headers)

                    created_a = launch_a.post(
                        "/sessions",
                        json={"title": "Launch A", "clone_projects": []},
                    )
                    self.assertEqual(created_a.status_code, 200, created_a.text)
                    session_a = created_a.json()["session"]

                    # A second browser visiting the same Interface sees the
                    # same launch-owned data without needing a browser cookie.
                    with TestClient(create_app(service_a)) as same_launch:
                        same_launch_sessions = same_launch.get("/sessions")
                        self.assertEqual(
                            [
                                row["session_id"]
                                for row in same_launch_sessions.json()["sessions"]
                            ],
                            [session_a["session_id"]],
                        )
                        self.assertNotIn("set-cookie", same_launch_sessions.headers)

                    # Another Interface launch has a different root and owner.
                    self.assertEqual(launch_b.get("/sessions").json()["sessions"], [])
                    created_b = launch_b.post(
                        "/sessions",
                        json={"title": "Launch B", "clone_projects": []},
                    )
                    self.assertEqual(created_b.status_code, 200, created_b.text)
                    session_b = created_b.json()["session"]
                    self.assertNotEqual(
                        session_a["owner_client_id"],
                        session_b["owner_client_id"],
                    )
                    self.assertEqual(
                        [
                            row["session_id"]
                            for row in launch_a.get("/sessions").json()["sessions"]
                        ],
                        [session_a["session_id"]],
                    )

            for root in (launch_a_root, launch_b_root):
                data_root = Path(root) / "data"
                self.assertFalse((data_root / "participants.sqlite3").exists())
                self.assertFalse((data_root / ".participant-cookie-secret").exists())

    def test_unknown_identity_mode_fails_during_app_creation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_IDENTITY_MODE": "shared-maybe"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "must be 'cookie' or 'launch'"):
                create_app(self._service(tmp))


if __name__ == "__main__":
    unittest.main()
