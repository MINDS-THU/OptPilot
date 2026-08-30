"""Studio's local HTTP API rejects cross-site and unauthenticated mutation."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from optpilot_studio.ui.server import UiState, _handler_factory


def _can_bind_loopback() -> bool:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
    except OSError:
        return False
    finally:
        listener.close()
    return True


_LOOPBACK_TCP_BIND_AVAILABLE = _can_bind_loopback()


@unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
class StudioHttpMutationSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_factory(self.state)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self._close_server)

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.state.close_coordination()

    def _security_context(self) -> dict:
        with urlopen(f"{self.base_url}/api/security-context", timeout=5) as response:
            self.assertEqual(response.status, HTTPStatus.OK)
            self.assertEqual(
                response.headers.get("Cache-Control"), "no-store, max-age=0"
            )
            return json.loads(response.read().decode("utf-8"))

    def _request(
        self,
        path: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        payload: dict | None = None,
    ):
        return urlopen(
            Request(
                f"{self.base_url}{path}",
                data=json.dumps(payload or {}).encode("utf-8"),
                headers=headers or {},
                method=method,
            ),
            timeout=5,
        )

    def _assert_http_error(self, status: HTTPStatus, request) -> dict:
        with self.assertRaises(HTTPError) as caught:
            request()
        error = caught.exception
        try:
            self.assertEqual(error.code, status)
            return json.loads(error.read().decode("utf-8"))
        finally:
            error.close()

    def test_bootstrap_issues_a_process_token_and_authenticated_post_works(self) -> None:
        context = self._security_context()
        self.assertEqual(context["schema"], "optpilot.studio-security-context.v1")
        self.assertEqual(context["csrf_header"], "X-OptPilot-CSRF-Token")
        self.assertGreaterEqual(len(context["csrf_token"]), 32)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Origin": self.base_url,
            context["csrf_header"]: context["csrf_token"],
        }
        with self._request(
            "/api/agent-sessions",
            headers=headers,
            payload={"id": "secured-session", "title": "Secured"},
        ) as response:
            self.assertEqual(response.status, HTTPStatus.CREATED)
            created = json.loads(response.read().decode("utf-8"))
        self.assertEqual(created["session"]["id"], "secured-session")

        # Local scripts and API clients do not send a browser Origin. Possession
        # of the per-process token remains sufficient for that non-browser path.
        with self._request(
            "/api/agent-sessions",
            headers={
                "Content-Type": "application/json",
                context["csrf_header"]: context["csrf_token"],
            },
            payload={"id": "local-script-session", "title": "Script"},
        ) as response:
            self.assertEqual(response.status, HTTPStatus.CREATED)

    def test_post_rejects_missing_token_cross_origin_and_non_json(self) -> None:
        context = self._security_context()
        base_headers = {
            "Content-Type": "application/json",
            "Origin": self.base_url,
        }
        missing = self._assert_http_error(
            HTTPStatus.FORBIDDEN,
            lambda: self._request(
                "/api/agent-sessions", headers=base_headers, payload={"title": "x"}
            ),
        )
        self.assertEqual(missing["code"], "studio_mutation_token_invalid")

        token_headers = {
            **base_headers,
            context["csrf_header"]: context["csrf_token"],
        }
        cross_origin = self._assert_http_error(
            HTTPStatus.FORBIDDEN,
            lambda: self._request(
                "/api/agent-sessions",
                headers={**token_headers, "Origin": "https://attacker.example"},
                payload={"title": "x"},
            ),
        )
        self.assertEqual(cross_origin["code"], "studio_cross_origin_mutation")

        wrong_media = self._assert_http_error(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            lambda: self._request(
                "/api/agent-sessions",
                headers={**token_headers, "Content-Type": "text/plain"},
                payload={"title": "x"},
            ),
        )
        self.assertEqual(wrong_media["code"], "studio_json_content_type_required")

    def test_delete_has_the_same_boundary_as_post(self) -> None:
        context = self._security_context()
        missing = self._assert_http_error(
            HTTPStatus.FORBIDDEN,
            lambda: self._request(
                "/api/workspaces/no-such-workspace",
                method="DELETE",
                headers={"Content-Type": "application/json"},
            ),
        )
        self.assertEqual(missing["code"], "studio_mutation_token_invalid")

        cross_site = self._assert_http_error(
            HTTPStatus.FORBIDDEN,
            lambda: self._request(
                "/api/workspaces/no-such-workspace",
                method="DELETE",
                headers={
                    "Content-Type": "application/json",
                    "Sec-Fetch-Site": "cross-site",
                    context["csrf_header"]: context["csrf_token"],
                },
            ),
        )
        self.assertEqual(cross_site["code"], "studio_cross_origin_mutation")

        workspace_root = self.root / "delete-me"
        workspace_root.mkdir()
        authenticated_headers = {
            "Content-Type": "application/json",
            context["csrf_header"]: context["csrf_token"],
        }
        with self._request(
            "/api/workspaces",
            headers=authenticated_headers,
            payload={
                "id": "delete-me",
                "title": "Delete me",
                "root": str(workspace_root),
                "initialize_if_empty": False,
            },
        ) as response:
            self.assertEqual(response.status, HTTPStatus.CREATED)
        with self._request(
            "/api/workspaces/delete-me",
            method="DELETE",
            headers=authenticated_headers,
        ) as response:
            self.assertEqual(response.status, HTTPStatus.OK)

    def test_security_context_refuses_a_dns_rebinding_host(self) -> None:
        payload = self._assert_http_error(
            HTTPStatus.FORBIDDEN,
            lambda: urlopen(
                Request(
                    f"{self.base_url}/api/security-context",
                    headers={"Host": "attacker.example"},
                ),
                timeout=5,
            ),
        )
        self.assertEqual(payload["code"], "studio_untrusted_host")


if __name__ == "__main__":
    unittest.main()
