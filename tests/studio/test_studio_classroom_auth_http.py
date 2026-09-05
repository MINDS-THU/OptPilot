from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from optpilot_studio.ui.server import (
    InterfaceLaunchCapacityExceeded,
    PublicAccessOptions,
    UiState,
    _catalog_edit_workspace_operation_id,
    _handler_factory,
    _REQUEST_PRINCIPAL,
)
from optpilot_studio.ui.shared_auth import ClassroomAuth


def _can_bind_loopback() -> bool:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
    except OSError:
        return False
    finally:
        listener.close()
    return True


@unittest.skipUnless(_can_bind_loopback(), "sandbox denies loopback TCP bind")
class StudioClassroomAuthHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        classroom_auth = ClassroomAuth(
            database_path=self.root / "private" / "classroom.sqlite3",
            admin_password="admin-correct-horse-battery-staple",
            invitation_code="class-invitation-2026",
            registration_enabled=True,
        )
        public_access = PublicAccessOptions.from_url(
            "https://studio.example:8443", trust_loopback_proxy=True
        )
        self.state = UiState(
            cwd=self.root / "state",
            catalog_roots=[],
            run_roots=[],
            public_access=public_access,
            shared_auth=classroom_auth,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_factory(self.state)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.state.close_coordination()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        cookie: str = "",
        mutation: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = {
            "Host": "127.0.0.1:8765",
            "X-Forwarded-Host": "studio.example:8443",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "166.111.1.2",
        }
        if cookie:
            headers["Cookie"] = cookie
        if payload is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Origin": "https://studio.example:8443",
                }
            )
        if mutation:
            headers["X-OptPilot-CSRF-Token"] = self.state.http_mutation_token
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _register(self, username: str, password: str) -> str:
        status, headers, body = self._request(
            "POST",
            "/api/auth/register",
            payload={
                "username": username,
                "display_name": username.title(),
                "password": password,
                "invitation_code": "class-invitation-2026",
            },
        )
        self.assertEqual(status, HTTPStatus.CREATED, body)
        return headers["Set-Cookie"].split(";", 1)[0]

    def _login_admin(self) -> str:
        status, headers, body = self._request(
            "POST",
            "/api/auth/login",
            payload={
                "username": "admin",
                "password": "admin-correct-horse-battery-staple",
            },
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_registration_and_private_conversation_workspace_scope(self) -> None:
        status, _headers, page = self._request("GET", "/register")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn(b"invitation code", page)

        alice = self._register("alice", "alice-password-123")
        bob = self._register("bob", "bob-password-456")

        status, _headers, body = self._request(
            "POST",
            "/api/agent-sessions",
            payload={"title": "Alice only"},
            cookie=alice,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.CREATED, body)
        alice_session = json.loads(body)["session"]["id"]

        status, _headers, body = self._request(
            "POST",
            "/api/workspaces",
            payload={"title": "Alice workspace"},
            cookie=alice,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.CREATED, body)
        alice_workspace = json.loads(body)["workspace"]["id"]

        status, _headers, body = self._request(
            "GET", "/api/agent-sessions", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertNotIn(
            alice_session,
            [item["id"] for item in json.loads(body)["sessions"]],
        )
        status, _headers, body = self._request("GET", "/api/workspaces", cookie=bob)
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertNotIn(
            alice_workspace,
            [item["id"] for item in json.loads(body)["workspaces"]],
        )
        with patch.object(
            self.state.workspace_runtime,
            "workspace_id_for_code_server_port",
            return_value=alice_workspace,
        ):
            for cookie, expected in (
                (alice, HTTPStatus.NO_CONTENT),
                (bob, HTTPStatus.FORBIDDEN),
            ):
                status, _headers, _body = self._request_with_headers(
                    "/api/auth/verify",
                    cookie=cookie,
                    target_kind="code",
                    target_port="28766",
                )
                self.assertEqual(status, expected)
        status, _headers, _body = self._request(
            "GET", f"/api/agent-sessions/{alice_session}", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

        admin = self._login_admin()
        status, _headers, body = self._request(
            "GET", "/api/agent-sessions", cookie=admin
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertIn(
            alice_session,
            [item["id"] for item in json.loads(body)["sessions"]],
        )

    def test_student_cannot_change_shared_settings_or_open_host_folder(self) -> None:
        student = self._register("student", "student-password-123")
        status, _headers, body = self._request(
            "POST",
            "/api/agent/settings",
            payload={"model": "untrusted-change"},
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)

        status, _headers, body = self._request(
            "GET", "/api/workspace", cookie=student
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertNotIn("cwd", json.loads(body))
        status, _headers, body = self._request(
            "GET", "/api/health", cookie=student
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertNotIn("cwd", json.loads(body))
        status, _headers, body = self._request(
            "POST",
            "/api/workspaces/connect-local-folder",
            payload={"path": str(self.root)},
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)
        status, _headers, body = self._request(
            "POST",
            "/api/workspaces",
            payload={"title": "Unsafe", "root": str(self.root)},
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)
        status, _headers, body = self._request(
            "POST",
            "/api/agent-sessions",
            payload={
                "title": "Unsafe",
                "openhands_conversation_id": "other-account-runtime",
            },
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)
        status, _headers, body = self._request(
            "POST",
            "/api/studies/workspace",
            payload={"study_path": "/private/study.yaml"},
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)
        status, _headers, body = self._request(
            "POST",
            "/api/code-server/stop",
            payload={},
            cookie=student,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)

    def test_interface_capacity_conflict_lists_only_stoppable_launches(self) -> None:
        student = self._register("capacity", "capacity-password-123")
        error = InterfaceLaunchCapacityExceeded(
            limit=1,
            active_interfaces=[
                {
                    "launch_id": "launch-existing",
                    "label": "Existing interface",
                    "status": "ready",
                    "started_at": 123.0,
                    "updated_at": 456.0,
                    "launch_scope": "catalog-transient",
                    "can_stop": True,
                }
            ],
        )
        with patch(
            "optpilot_studio.ui.server._start_workspace_interface_launch",
            side_effect=error,
        ):
            status, _headers, body = self._request(
                "POST",
                "/api/workspaces/unused/launch-interface-job",
                payload={"profile_id": "default"},
                cookie=student,
                mutation=True,
            )
        payload = json.loads(body)
        self.assertEqual(status, HTTPStatus.CONFLICT, body)
        self.assertEqual(payload["code"], "interface_launch_capacity_reached")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["active_interfaces"], error.active_interfaces)

    def test_catalog_workspace_coordinates_are_account_scoped(self) -> None:
        alice_cookie = self._register("alice", "alice-password-123")
        bob_cookie = self._register("bob", "bob-password-456")
        source = SimpleNamespace(
            relative_path="resources/example/optpilot.resource.yaml",
            selection=SimpleNamespace(
                to_dict=lambda: {
                    "kind": "catalog-package",
                    "source_id": "package-a",
                    "source_revision": 1,
                }
            ),
        )
        coordinates = []
        for cookie in (alice_cookie, bob_cookie):
            principal = self.state.shared_auth.principal_from_cookie(cookie)
            principal_token = _REQUEST_PRINCIPAL.set(principal)
            try:
                coordinates.append(
                    _catalog_edit_workspace_operation_id(
                        self.state, "resource", source
                    )
                )
            finally:
                _REQUEST_PRINCIPAL.reset(principal_token)
        self.assertNotEqual(coordinates[0], coordinates[1])

    def _request_with_headers(
        self, path: str, *, cookie: str, target_kind: str, target_port: str
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Cookie": cookie,
                    "X-OptPilot-Target-Kind": target_kind,
                    "X-OptPilot-Target-Port": target_port,
                },
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
