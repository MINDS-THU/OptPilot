from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from optpilot_studio.ui.server import (
    CatalogEntryRef,
    InterfaceLaunchCapacityExceeded,
    PublicAccessOptions,
    UiLaunchJob,
    UiState,
    _catalog_entry_asset_id,
    _catalog_edit_workspace_operation_id,
    _handler_factory,
    _REQUEST_PRINCIPAL,
    _reserve_catalog_publication_ownership,
    _shared_catalog_interface_launch,
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

    def test_interface_preview_keeps_the_trusted_workspace_endpoint_contract(self) -> None:
        captured = {}

        def open_endpoint(*, key, endpoint):
            captured["key"] = key
            captured["endpoint"] = endpoint
            return SimpleNamespace(preview_url="http://127.0.0.1:31000/")

        with patch.object(
            self.state.workspace_runtime,
            "_read_record",
            return_value={
                "code_server_started_at": 1,
                "container_name": "workspace-container",
                "image": "workspace-image",
                "started_at": 1,
                "host_port": 32000,
            },
        ), patch.object(
            self.state.presentation_broker,
            "open",
            side_effect=open_endpoint,
        ):
            self.state._workspace_preview_proxy(
                "interface-launch-example",
                3000,
                "http://127.0.0.1:32000/proxy/3000",
                allowed_ports=[3000],
            )

        endpoint = captured["endpoint"]
        self.assertEqual(endpoint.owner_kind, "workspace-runtime")
        self.assertEqual(endpoint.owner_id, "interface-launch-example")
        self.assertEqual(endpoint.websocket_origin_policy, "omit")

    def test_admin_can_share_a_live_interface_without_granting_stop_access(self) -> None:
        bob = self._register("interface-bob", "interface-bob-password-123")
        admin = self._login_admin()
        admin_principal = self.state.shared_auth.principal_from_cookie(admin)
        self.assertIsNotNone(admin_principal)
        launch_id = "launch-shared-classroom"
        job = UiLaunchJob(
            launch_id=launch_id,
            kind="resource",
            uid="catalog-resource-ref",
            label="Shared generator",
            port=3000,
            profile_id="default",
            status="ready",
            launch_scope="catalog-transient",
            owner_account_id=admin_principal.account_id,
            runtime_workspace={"id": f"interface-{launch_id}"},
        )
        with self.state._lock:
            self.state.interface_launches[launch_id] = job
        self.addCleanup(self.state.interface_launches.pop, launch_id, None)
        self.state.shared_auth.claim_asset(
            asset_type="interface-launch",
            asset_id=launch_id,
            principal=admin_principal,
        )
        runtime_workspace_id = f"interface-{launch_id}"
        self.state.shared_auth.claim_asset(
            asset_type="runtime-workspace",
            asset_id=runtime_workspace_id,
            principal=admin_principal,
        )

        status, _headers, body = self._request(
            "GET", f"/api/interface-launches/{launch_id}", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND, body)
        status, _headers, body = self._request(
            "GET", "/api/interface-launches", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(json.loads(body)["launches"], [])

        visibility_path = f"/api/interface-launches/{launch_id}/visibility"
        status, _headers, body = self._request(
            "POST",
            visibility_path,
            payload={
                "schema": "optpilot.interface-launch-visibility.v1",
                "visibility": "public",
            },
            cookie=bob,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN, body)

        status, _headers, body = self._request(
            "POST",
            visibility_path,
            payload={
                "schema": "optpilot.interface-launch-visibility.v1",
                "visibility": "public",
            },
            cookie=admin,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(json.loads(body)["access"]["visibility"], "public")

        status, _headers, body = self._request(
            "GET", f"/api/interface-launches/{launch_id}", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        launch = json.loads(body)["launch"]
        self.assertEqual(launch["access"]["visibility"], "public")
        self.assertFalse(launch["access"]["can_manage_visibility"])
        self.assertFalse(launch["can_stop"])
        status, _headers, body = self._request(
            "GET", "/api/interface-launches", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        listed = json.loads(body)["launches"]
        self.assertEqual([item["launch_id"] for item in listed], [launch_id])
        self.assertFalse(listed[0]["can_stop"])

        bob_principal = self.state.shared_auth.principal_from_cookie(bob)
        self.assertIsNotNone(bob_principal)
        principal_token = _REQUEST_PRINCIPAL.set(bob_principal)
        try:
            reused = _shared_catalog_interface_launch(
                self.state,
                kind="resource",
                uid="catalog-resource-ref",
                profile_id="default",
            )
        finally:
            _REQUEST_PRINCIPAL.reset(principal_token)
        self.assertIsNotNone(reused)
        self.assertEqual(reused["launch_id"], launch_id)
        self.assertFalse(reused["can_stop"])

        with patch.object(
            self.state.presentation_broker,
            "owner_for_port",
            return_value=("workspace-runtime", runtime_workspace_id),
        ), patch.object(self.state.workspace_runtime, "touch") as touch:
            status, _headers, _body = self._request_with_headers(
                "/api/auth/verify",
                cookie=bob,
                target_kind="presentation",
                target_port="3000",
            )
        self.assertEqual(status, HTTPStatus.NO_CONTENT)
        touch.assert_called_once_with(runtime_workspace_id)

        with patch.object(
            self.state.workspace_runtime,
            "workspace_id_for_code_server_port",
            return_value=runtime_workspace_id,
        ):
            status, _headers, _body = self._request_with_headers(
                "/api/auth/verify",
                cookie=bob,
                target_kind="code",
                target_port="28766",
            )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

        status, _headers, body = self._request(
            "POST",
            f"/api/interface-launches/{launch_id}/stop",
            payload={},
            cookie=bob,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND, body)

        status, _headers, body = self._request(
            "POST",
            visibility_path,
            payload={
                "schema": "optpilot.interface-launch-visibility.v1",
                "visibility": "private",
            },
            cookie=admin,
            mutation=True,
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        status, _headers, body = self._request(
            "GET", "/api/interface-launches", cookie=bob
        )
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertEqual(json.loads(body)["launches"], [])
        with patch.object(
            self.state.presentation_broker,
            "owner_for_port",
            return_value=("workspace-runtime", runtime_workspace_id),
        ):
            status, _headers, _body = self._request_with_headers(
                "/api/auth/verify",
                cookie=bob,
                target_kind="presentation",
                target_port="3000",
            )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

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

    def test_catalog_item_visibility_is_private_then_owner_or_admin_controlled(
        self,
    ) -> None:
        alice = self._register("catalog-alice", "catalog-alice-password-123")
        bob = self._register("catalog-bob", "catalog-bob-password-456")
        admin = self._login_admin()
        entry_ref = CatalogEntryRef(
            source_kind="realm-catalog",
            source_id="student-package",
            source_revision=1,
            source_digest="a" * 64,
            kind="resource",
            entry_id="student-viewer",
            focus_path="resources/student-viewer",
        )
        entry = {
            "config": "resource",
            "id": "student-viewer",
            "uid": entry_ref.token(),
            "ref": entry_ref.to_dict(),
            "label": "Student viewer",
            "description": "A privately published item.",
            "package_id": "student-package",
            "path": "catalog://student-package/resources/student-viewer",
            "summary": {},
            "tags": [],
        }
        index = {
            "roots": ["catalog://student-package"],
            "environments": [],
            "methods": [],
            "studies": [],
            "resources": [entry],
            "sources": [],
            "builtins": {},
        }
        alice_principal = self.state.shared_auth.principal_from_cookie(alice)
        self.assertIsNotNone(alice_principal)
        asset_id = _catalog_entry_asset_id(
            package_id="student-package",
            kind="resource",
            entry_id="student-viewer",
        )
        self.state.shared_auth.claim_asset(
            asset_type="catalog-entry",
            asset_id=asset_id,
            principal=alice_principal,
        )

        with patch(
            "optpilot_studio.ui.server._catalog_index_payload",
            return_value=index,
        ):
            status, _headers, body = self._request("GET", "/api/catalog", cookie=alice)
            self.assertEqual(status, HTTPStatus.OK, body)
            listed = json.loads(body)["resources"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["access"]["visibility"], "private")
            self.assertTrue(listed[0]["access"]["can_manage_visibility"])

            status, _headers, body = self._request("GET", "/api/catalog", cookie=bob)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(json.loads(body)["resources"], [])
            self.assertEqual(json.loads(body)["roots"], [])
            status, _headers, body = self._request(
                "GET", f"/api/resources/{entry_ref.token()}", cookie=bob
            )
            self.assertEqual(status, HTTPStatus.NOT_FOUND, body)

            visibility_path = (
                "/api/catalog/resource/"
                f"{entry_ref.token()}/visibility"
            )
            status, _headers, body = self._request(
                "POST",
                visibility_path,
                payload={
                    "schema": "optpilot.catalog-entry-visibility.v1",
                    "visibility": "public",
                },
                cookie=alice,
                mutation=True,
            )
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(json.loads(body)["access"]["visibility"], "public")

            status, _headers, body = self._request("GET", "/api/catalog", cookie=bob)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(len(json.loads(body)["resources"]), 1)

            status, _headers, body = self._request(
                "POST",
                visibility_path,
                payload={
                    "schema": "optpilot.catalog-entry-visibility.v1",
                    "visibility": "private",
                },
                cookie=bob,
                mutation=True,
            )
            self.assertEqual(status, HTTPStatus.FORBIDDEN, body)

            status, _headers, body = self._request(
                "POST",
                visibility_path,
                payload={
                    "schema": "optpilot.catalog-entry-visibility.v1",
                    "visibility": "private",
                },
                cookie=admin,
                mutation=True,
            )
            self.assertEqual(status, HTTPStatus.OK, body)
            status, _headers, body = self._request("GET", "/api/catalog", cookie=bob)
            self.assertEqual(status, HTTPStatus.OK, body)
            self.assertEqual(json.loads(body)["resources"], [])

    def test_new_catalog_publication_is_reserved_private_for_its_publisher(self) -> None:
        alice_cookie = self._register(
            "publishing-alice", "publishing-alice-password-123"
        )
        alice = self.state.shared_auth.principal_from_cookie(alice_cookie)
        self.assertIsNotNone(alice)
        empty_index = {
            "roots": [],
            "environments": [],
            "methods": [],
            "studies": [],
            "resources": [],
            "sources": [],
            "builtins": {},
        }
        principal_token = _REQUEST_PRINCIPAL.set(alice)
        try:
            with patch(
                "optpilot_studio.ui.server._catalog_index_payload",
                return_value=empty_index,
            ):
                _reserve_catalog_publication_ownership(
                    self.state,
                    package_id="alice-package",
                    entries=[{"kind": "resource", "id": "alice-viewer"}],
                )
        finally:
            _REQUEST_PRINCIPAL.reset(principal_token)

        ownership = self.state.shared_auth.asset_ownership(
            asset_type="catalog-entry",
            asset_id=_catalog_entry_asset_id(
                package_id="alice-package",
                kind="resource",
                entry_id="alice-viewer",
            ),
        )
        self.assertEqual(ownership["owner_account_id"], alice.account_id)
        self.assertEqual(ownership["visibility"], "private")

    def test_cached_compatibility_is_filtered_for_each_account(self) -> None:
        alice_cookie = self._register(
            "compat-alice", "compat-alice-password-123"
        )
        bob_cookie = self._register("compat-bob", "compat-bob-password-456")
        alice = self.state.shared_auth.principal_from_cookie(alice_cookie)
        self.assertIsNotNone(alice)
        environment_ref = CatalogEntryRef(
            source_kind="realm-catalog",
            source_id="private-environment-package",
            source_revision=1,
            source_digest="b" * 64,
            kind="environment",
            entry_id="private-environment",
            focus_path="environments/private.yaml",
        )
        method_ref = CatalogEntryRef(
            source_kind="realm-catalog",
            source_id="public-method-package",
            source_revision=1,
            source_digest="c" * 64,
            kind="method",
            entry_id="public-method",
            focus_path="methods/public.yaml",
        )

        def entry(reference: CatalogEntryRef) -> dict[str, object]:
            return {
                "config": reference.kind,
                "id": reference.entry_id,
                "uid": reference.token(),
                "ref": reference.to_dict(),
                "label": reference.entry_id,
                "package_id": reference.source_id,
                "path": f"catalog://{reference.source_id}/{reference.focus_path}",
                "summary": {},
            }

        environment = entry(environment_ref)
        method = entry(method_ref)
        index = {
            "roots": [
                "catalog://private-environment-package",
                "catalog://public-method-package",
            ],
            "environments": [environment],
            "methods": [method],
            "studies": [],
            "resources": [],
            "sources": [],
            "builtins": {},
        }
        raw_compatibility = {
            "environments": [environment],
            "methods": [method],
            "pairs": [
                {
                    "compatible": True,
                    "environment": environment,
                    "method": method,
                    "checks": [],
                    "reasons": [],
                }
            ],
        }
        self.state.shared_auth.claim_asset(
            asset_type="catalog-entry",
            asset_id=_catalog_entry_asset_id(
                package_id=environment_ref.source_id,
                kind=environment_ref.kind,
                entry_id=environment_ref.entry_id,
            ),
            principal=alice,
        )
        self.state.catalog_refresh_ttl_seconds = 60
        self.state._compatibility_cache = (
            index,
            time.monotonic(),
            raw_compatibility,
        )

        with patch(
            "optpilot_studio.ui.server._catalog_index_payload",
            return_value=index,
        ):
            status, _headers, body = self._request(
                "GET", "/api/compatibility", cookie=bob_cookie
            )
            self.assertEqual(status, HTTPStatus.OK, body)
            bob_payload = json.loads(body)
            self.assertEqual(bob_payload["environments"], [])
            self.assertEqual(bob_payload["pairs"], [])
            self.assertEqual(len(bob_payload["methods"]), 1)

            status, _headers, body = self._request(
                "GET", "/api/compatibility", cookie=alice_cookie
            )
            self.assertEqual(status, HTTPStatus.OK, body)
            alice_payload = json.loads(body)
            self.assertEqual(len(alice_payload["environments"]), 1)
            self.assertEqual(len(alice_payload["pairs"]), 1)

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
