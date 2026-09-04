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
from unittest.mock import patch

from optpilot_studio.ui.server import (
    PublicAccessOptions,
    UiState,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeOptions,
    _handler_factory,
    _workspace_presentation_generation,
)
from optpilot_studio.ui.shared_auth import SharedAuth, write_credentials


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
class StudioSharedLoginHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        credentials = root / "credentials.json"
        write_credentials(
            credentials,
            username="students",
            password="correct horse battery staple",
        )
        shared_auth = SharedAuth.from_files(
            credentials_path=credentials,
            database_path=root / "sessions.sqlite3",
        )
        public_access = PublicAccessOptions.from_url(
            "https://studio.example:8443", trust_loopback_proxy=True
        )
        self.state = UiState(
            cwd=root,
            catalog_roots=[],
            run_roots=[],
            public_access=public_access,
            shared_auth=shared_auth,
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
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    @staticmethod
    def _proxy_headers(**extra: str) -> dict[str, str]:
        return {
            "Host": "127.0.0.1:8765",
            "X-Forwarded-Host": "studio.example:8443",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "166.111.1.2",
            **extra,
        }

    def test_login_protects_studio_and_authorizes_proxy_subrequests(self) -> None:
        status, headers, _body = self._request(
            "GET", "/", headers=self._proxy_headers()
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(headers["Location"].startswith("/login?next="))

        payload = json.dumps(
            {
                "username": "students",
                "password": "correct horse battery staple",
            }
        ).encode("utf-8")
        status, headers, body = self._request(
            "POST",
            "/api/auth/login",
            body=payload,
            headers=self._proxy_headers(
                **{
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Origin": "https://studio.example:8443",
                }
            ),
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("__Host-optpilot_session=", cookie)

        status, _headers, body = self._request(
            "GET",
            "/api/security-context",
            headers=self._proxy_headers(Cookie=cookie),
        )
        self.assertEqual(status, HTTPStatus.OK, body)

        status, _headers, _body = self._request(
            "GET",
            "/api/auth/verify",
            headers={
                "Cookie": cookie,
                "X-OptPilot-Target-Kind": "studio",
                "X-OptPilot-Target-Port": "8443",
            },
        )
        self.assertEqual(status, HTTPStatus.NO_CONTENT)

        status, _headers, _body = self._request(
            "GET",
            "/api/auth/verify",
            headers={
                "Cookie": cookie,
                "X-OptPilot-Target-Kind": "code",
                "X-OptPilot-Target-Port": "18766",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_login_rejects_spoofed_public_origin(self) -> None:
        payload = json.dumps(
            {
                "username": "students",
                "password": "correct horse battery staple",
            }
        ).encode("utf-8")
        status, _headers, body = self._request(
            "POST",
            "/api/auth/login",
            body=payload,
            headers=self._proxy_headers(
                **{
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Origin": "https://attacker.example",
                }
            ),
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(json.loads(body)["code"], "studio_login_cross_origin")


class WorkspaceRuntimePublicPortTests(unittest.TestCase):
    def test_preview_ownership_is_exact_and_briefly_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / "runtime",
                options=WorkspaceRuntimeOptions(port_start=31000, port_count=2),
            )
            record = {
                "workspace_id": "workspace-1",
                "container_name": "optpilot-workspace-1",
                "host_port": 31000,
                "image": "workspace:latest",
                "started_at": "start-1",
                "code_server_started_at": "code-1",
            }
            generation = _workspace_presentation_generation("workspace-1", record)
            with (
                patch.object(manager, "_read_record", return_value=record),
                patch.object(manager, "_container_running", return_value=True) as running,
            ):
                self.assertTrue(
                    manager.owns_workspace_code_server_port(
                        "workspace-1", 31000, generation
                    )
                )
                self.assertTrue(
                    manager.owns_workspace_code_server_port(
                        "workspace-1", 31000, generation
                    )
                )
                self.assertFalse(
                    manager.owns_workspace_code_server_port(
                        "workspace-1", 31000, "stale-generation"
                    )
                )

            running.assert_called_once_with("optpilot-workspace-1")

    def test_delete_immediately_invalidates_cached_port_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / "runtime",
                options=WorkspaceRuntimeOptions(port_start=31000, port_count=2),
            )
            manager._mark_code_server_port_owned(31000)
            self.assertTrue(manager.owns_code_server_port(31000))

            self.assertFalse(manager.delete("already-absent"))
            self.assertFalse(manager.owns_code_server_port(31000))

    def test_allocator_never_escapes_the_published_port_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / "runtime",
                options=WorkspaceRuntimeOptions(port_start=31000, port_count=2),
            )
            with (
                patch.object(
                    manager, "_read_record", return_value={"host_port": 45000}
                ),
                patch.object(manager, "_reserved_host_ports", return_value={31000}),
                patch("optpilot_studio.ui.server._port_listening", return_value=False),
            ):
                self.assertEqual(manager._host_port("workspace"), 31001)

            with (
                patch.object(manager, "_read_record", return_value={}),
                patch.object(
                    manager, "_reserved_host_ports", return_value={31000, 31001}
                ),
            ):
                with self.assertRaisesRegex(OSError, "31000-31001"):
                    manager._host_port("workspace")

    def test_ownership_rejects_a_port_outside_the_published_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / "runtime",
                options=WorkspaceRuntimeOptions(port_start=31000, port_count=2),
            )
            with patch.object(manager, "_container_running") as running:
                self.assertFalse(manager.owns_code_server_port(45000))
                running.assert_not_called()


if __name__ == "__main__":
    unittest.main()
