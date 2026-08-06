from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from optpilot_studio.ui.presentation_broker import (
    OwnedWebEndpoint,
    WebPresentationBroker,
    WebPresentationLease,
    _relay_websocket_streams,
    _serialize_websocket_request,
    _validated_websocket_request_headers,
    _validated_websocket_response_headers,
    _websocket_forward_headers,
)


class _UpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []
    websocket_received: list[bytes] = []
    websocket_provider_secret = ""
    websocket_hold_open = False
    websocket_reject_origin = False
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests.append(
            {
                "authorization": self.headers.get("Authorization", ""),
                "cookie": self.headers.get("Cookie", ""),
                "origin": self.headers.get("Origin", ""),
                "path": self.path,
                "presentation_ingress": self.headers.get(
                    "X-OptPilot-Presentation-Ingress", ""
                ),
                "x_optpilot": self.headers.get("X-OptPilot-Secret", ""),
            }
        )
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        body = json.dumps(type(self).requests[-1], sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "application_session=ok; Path=/")
        self.send_header(
            "Set-Cookie", "optpilot_presentation_token=forged; Path=/"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self) -> None:
        if type(self).websocket_reject_origin and self.headers.get("Origin"):
            self.send_response_only(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        client_key = self.headers.get("Sec-WebSocket-Key", "")
        accepted = base64.b64encode(
            hashlib.sha1(
                f"{client_key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode(
                    "ascii"
                )
            ).digest()
        ).decode("ascii")
        secret = type(self).websocket_provider_secret
        self.send_response_only(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accepted)
        self.send_header("Sec-WebSocket-Protocol", "chat")
        self.send_header("X-Upstream-Visible", "yes")
        self.send_header("X-OptPilot-Presentation-Ingress", secret)
        self.send_header("X-Echoed-Provider-Secret", secret)
        self.send_header("Set-Cookie", "application_websocket=ok; Path=/")
        self.send_header(
            "Set-Cookie", "optpilot_presentation_token=forged; Path=/"
        )
        self.end_headers()
        self.wfile.flush()
        self.connection.settimeout(3)
        first = self.connection.recv(4096)
        type(self).websocket_received.append(first)
        self.connection.sendall(b"server-websocket-bytes")
        if type(self).websocket_hold_open:
            while self.connection.recv(4096):
                pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ProviderBinding:
    def __init__(
        self,
        *,
        route: str,
        secret: str,
        state: dict[str, bool] | None = None,
        authorization_headers: dict[str, str] | None = None,
    ) -> None:
        self.owner_kind = "operator-job-container"
        self.owner_id = "operator-job-1"
        self.generation = "fence-1"
        self.access_policy = "launch-authenticated"
        self.primary_port = 5173
        self.routes = {5173: route}
        self.authorization_headers = authorization_headers or {
            "Authorization": f"Bearer {secret}",
            "X-OptPilot-Presentation-Ingress": secret,
        }
        self._state = state if state is not None else {"valid": True}

    def validate(self) -> None:
        if not self._state["valid"]:
            raise RuntimeError("provider binding expired")


class _HeaderItems:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


def _read_socket_header(connection: socket.socket) -> tuple[bytes, bytes]:
    received = bytearray()
    while b"\r\n\r\n" not in received:
        chunk = connection.recv(4096)
        if not chunk:
            break
        received.extend(chunk)
    header, separator, remainder = bytes(received).partition(b"\r\n\r\n")
    if not separator:
        raise AssertionError("connection closed before an HTTP header was received")
    return header + separator, remainder


def _recv_exact(
    connection: socket.socket, size: int, *, initial: bytes = b""
) -> bytes:
    received = bytearray(initial)
    while len(received) < size:
        chunk = connection.recv(size - len(received))
        if not chunk:
            break
        received.extend(chunk)
    return bytes(received)


class StudioPresentationBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        _UpstreamHandler.requests = []
        _UpstreamHandler.websocket_received = []
        _UpstreamHandler.websocket_provider_secret = ""
        _UpstreamHandler.websocket_hold_open = False
        _UpstreamHandler.websocket_reject_origin = False
        try:
            self.upstream = ThreadingHTTPServer(
                ("127.0.0.1", 0), _UpstreamHandler
            )
        except PermissionError:
            self.upstream = None
            self.upstream_thread = None
            self.broker = None
            return
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.broker = WebPresentationBroker(host="127.0.0.1", port_start=29766)

    def tearDown(self) -> None:
        if self.broker is not None:
            self.broker.close_all()
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()

    def _require_loopback_servers(self) -> None:
        if self.upstream is None or self.broker is None:
            self.skipTest("sandbox does not permit loopback server sockets")

    def _endpoint(self, state: dict[str, bool] | None = None) -> OwnedWebEndpoint:
        self._require_loopback_servers()
        assert self.upstream is not None
        ownership = state if state is not None else {"valid": True}

        def validate() -> None:
            if not ownership["valid"]:
                raise RuntimeError("presentation endpoint ownership expired")

        port = self.upstream.server_port
        return OwnedWebEndpoint(
            owner_kind="operator-job",
            owner_id="operator-job-1",
            generation="fence-1",
            access_policy="launch-authenticated",
            primary_port=5173,
            routes={5173: f"http://127.0.0.1:{port}"},
            validate=validate,
        )

    def test_requires_provider_owned_loopback_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            OwnedWebEndpoint(
                owner_kind="operator-job",
                owner_id="job",
                generation="fence",
                access_policy="launch-authenticated",
                primary_port=80,
                routes={80: "http://example.com:80"},
                validate=lambda: None,
            )
        with self.assertRaisesRegex(ValueError, "absent"):
            OwnedWebEndpoint(
                owner_kind="operator-job",
                owner_id="job",
                generation="fence",
                access_policy="launch-authenticated",
                primary_port=81,
                routes={80: "http://127.0.0.1:8080"},
                validate=lambda: None,
            )
        with self.assertRaisesRegex(ValueError, "launch-authenticated"):
            OwnedWebEndpoint(
                owner_kind="operator-job-container",
                owner_id="job",
                generation="fence",
                access_policy="trusted-local-authoring",
                primary_port=80,
                routes={80: "http://127.0.0.1:8080"},
                validate=lambda: None,
            )
        with self.assertRaisesRegex(ValueError, "trusted workspace runtime"):
            OwnedWebEndpoint(
                owner_kind="operator-job",
                owner_id="job",
                generation="fence",
                access_policy="launch-authenticated",
                websocket_origin_policy="omit",
                primary_port=80,
                routes={80: "http://127.0.0.1:8080"},
                validate=lambda: None,
            )
        workspace_endpoint = OwnedWebEndpoint(
            owner_kind="workspace-runtime",
            owner_id="workspace",
            generation="fence",
            access_policy="trusted-local-authoring",
            websocket_origin_policy="omit",
            primary_port=8080,
            routes={8080: "http://127.0.0.1:45173/proxy/8080"},
            validate=lambda: None,
        )
        self.assertEqual(workspace_endpoint.websocket_origin_policy, "omit")

    def test_fresh_origin_requires_token_and_hides_upstream_coordinates(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        lease = self.broker.open(key="job-1", endpoint=self._endpoint())
        with self.assertRaises(HTTPError) as caught:
            urlopen(lease.url, timeout=2)
        self.assertEqual(caught.exception.code, 403)

        request = Request(
            lease.open_url + "&query=value",
            headers={
                "Authorization": "Bearer studio-secret",
                "Cookie": "application_session=user; optpilot_presentation_token=ignored",
                "X-OptPilot-Secret": "private",
            },
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
            set_cookies = response.headers.get_all("Set-Cookie")
        self.assertEqual(payload["path"], "/?query=value")
        self.assertEqual(payload["authorization"], "")
        self.assertEqual(payload["x_optpilot"], "")
        self.assertEqual(payload["cookie"], "application_session=user")
        self.assertEqual(set_cookies[0], "application_session=ok; Path=/")
        self.assertTrue(
            any(value.startswith("optpilot_presentation_token=") for value in set_cookies)
        )
        self.assertFalse(any("forged" in value for value in set_cookies))

        public = lease.public_dict()
        self.assertEqual(public["presentation_kind"], "web")
        self.assertTrue(public["supports_websocket"])
        self.assertNotIn(str(self.upstream.server_port), json.dumps(public))
        self.assertNotIn("target", public)

    def test_websocket_upgrade_uses_same_auth_fence_and_private_binding(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        provider_secret = "provider-websocket-secret"
        _UpstreamHandler.websocket_provider_secret = provider_secret
        _UpstreamHandler.websocket_hold_open = True
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(
                route=f"http://127.0.0.1:{self.upstream.server_port}",
                secret=provider_secret,
            )
        )
        lease = self.broker.open(key="container-websocket", endpoint=endpoint)
        token_name = urlparse(lease.open_url).query.partition("=")[0]
        client_key = "dGhlIHNhbXBsZSBub25jZQ=="
        request = (
            f"GET /socket?{token_name}=client-controlled&mode=watch HTTP/1.1\r\n"
            f"Host: {lease.host}:{lease.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: keep-alive, Upgrade\r\n"
            f"Sec-WebSocket-Key: {client_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: chat, superchat\r\n"
            "Origin: http://127.0.0.1:29766\r\n"
            "Authorization: Bearer client-controlled\r\n"
            "X-OptPilot-Presentation-Ingress: client-controlled\r\n"
            "X-OptPilot-Secret: client-controlled\r\n"
            "Cookie: application_session=user; "
            f"optpilot_presentation_token={lease.token}\r\n"
            "\r\n"
        ).encode("ascii")

        with socket.create_connection((lease.host, lease.port), timeout=2) as browser:
            browser.settimeout(2)
            browser.sendall(request)
            response_header, initial = _read_socket_header(browser)
            response_text = response_header.decode("latin-1")
            self.assertTrue(response_text.startswith("HTTP/1.1 101"))
            self.assertIn("Sec-WebSocket-Protocol: chat\r\n", response_text)
            self.assertIn("X-Upstream-Visible: yes\r\n", response_text)
            self.assertIn("application_websocket=ok", response_text)
            self.assertNotIn(provider_secret, response_text)
            self.assertNotIn("forged", response_text)
            self.assertNotIn("optpilot_presentation_token=", response_text)

            upstream_request = _UpstreamHandler.requests[-1]
            self.assertEqual(upstream_request["path"], "/socket?mode=watch")
            self.assertEqual(
                upstream_request["authorization"],
                f"Bearer {provider_secret}",
            )
            self.assertEqual(
                upstream_request["presentation_ingress"], provider_secret
            )
            self.assertEqual(upstream_request["x_optpilot"], "")
            self.assertEqual(
                upstream_request["cookie"], "application_session=user"
            )
            self.assertEqual(
                upstream_request["origin"], "http://127.0.0.1:29766"
            )

            browser.sendall(b"client-websocket-bytes")
            reply = _recv_exact(
                browser,
                len(b"server-websocket-bytes"),
                initial=initial,
            )
            self.assertEqual(reply, b"server-websocket-bytes")
            self.assertEqual(
                _UpstreamHandler.websocket_received,
                [b"client-websocket-bytes"],
            )

            self.assertTrue(self.broker.close("container-websocket"))
            self.assertFalse(lease.running)
            try:
                self.assertEqual(browser.recv(1), b"")
            except ConnectionResetError:
                pass

    def test_workspace_websocket_omits_origin_for_code_server_proxy_hop(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        _UpstreamHandler.websocket_reject_origin = True
        endpoint = OwnedWebEndpoint(
            owner_kind="workspace-runtime",
            owner_id="workspace-1",
            generation="fence-1",
            access_policy="trusted-local-authoring",
            websocket_origin_policy="omit",
            primary_port=8080,
            routes={8080: f"http://127.0.0.1:{self.upstream.server_port}"},
            validate=lambda: None,
        )
        lease = self.broker.open(key="workspace-websocket", endpoint=endpoint)
        client_key = "dGhlIHNhbXBsZSBub25jZQ=="
        request = (
            "GET /socket HTTP/1.1\r\n"
            f"Host: {lease.host}:{lease.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {client_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: chat\r\n"
            "Origin: http://127.0.0.1:29766\r\n"
            f"Cookie: optpilot_presentation_token={lease.token}\r\n"
            "\r\n"
        ).encode("ascii")

        with socket.create_connection((lease.host, lease.port), timeout=2) as browser:
            browser.settimeout(2)
            browser.sendall(request)
            response_header, initial = _read_socket_header(browser)
            self.assertTrue(response_header.startswith(b"HTTP/1.1 101"))
            browser.sendall(b"workspace-websocket-bytes")
            reply = _recv_exact(
                browser,
                len(b"server-websocket-bytes"),
                initial=initial,
            )
            self.assertEqual(reply, b"server-websocket-bytes")

        self.assertEqual(_UpstreamHandler.requests[-1]["origin"], "")

    def test_websocket_upgrade_without_browser_token_never_reaches_upstream(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None
        lease = self.broker.open(key="unauthorized-websocket", endpoint=self._endpoint())
        request = (
            "GET /socket HTTP/1.1\r\n"
            f"Host: {lease.host}:{lease.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        with socket.create_connection((lease.host, lease.port), timeout=2) as browser:
            browser.settimeout(2)
            browser.sendall(request)
            response_header, _remainder = _read_socket_header(browser)
        self.assertTrue(response_header.startswith(b"HTTP/1.1 403"))
        self.assertEqual(_UpstreamHandler.requests, [])

    def test_websocket_handshake_contract_is_bounded_and_body_free(self) -> None:
        base_headers = [
            ("Upgrade", "websocket"),
            ("Connection", "keep-alive, Upgrade"),
            ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
            ("Sec-WebSocket-Version", "13"),
        ]
        items, client_key, protocols = _validated_websocket_request_headers(
            _HeaderItems(base_headers)
        )
        self.assertEqual(items, tuple(base_headers))
        self.assertEqual(client_key, "dGhlIHNhbXBsZSBub25jZQ==")
        self.assertEqual(protocols, ())

        invalid_variants = (
            base_headers + [("Sec-WebSocket-Key", client_key)],
            base_headers + [("Content-Length", "0")],
            base_headers + [("X-Oversized", "x" * (64 * 1024))],
            [
                (name, "invalid\r\nInjected: yes" if name == "Upgrade" else value)
                for name, value in base_headers
            ],
        )
        for headers in invalid_variants:
            with self.subTest(last_header=headers[-1][0]):
                with self.assertRaises(ValueError):
                    _validated_websocket_request_headers(_HeaderItems(headers))

    def test_websocket_upstream_headers_strip_studio_state_and_inject_provider_auth(self) -> None:
        provider_secret = "provider-websocket-secret"
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(
                route="http://127.0.0.1:45173",
                secret=provider_secret,
            )
        )
        incoming = _HeaderItems(
            [
                ("Upgrade", "websocket"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
                ("Sec-WebSocket-Version", "13"),
                ("Origin", "http://127.0.0.1:29766"),
                ("Authorization", "Bearer client-controlled"),
                ("X-OptPilot-Presentation-Ingress", "client-controlled"),
                (
                    "Cookie",
                    "application_session=user; "
                    "optpilot_presentation_token=browser-secret",
                ),
            ]
        )
        request_headers, _key, _protocols = (
            _validated_websocket_request_headers(incoming)
        )
        forwarded = _websocket_forward_headers(
            request_headers,
            host_header="127.0.0.1:45173",
        )
        origin_omitted = _websocket_forward_headers(
            request_headers,
            host_header="127.0.0.1:45173",
            origin_policy="omit",
        )
        endpoint._inject_upstream_authorization(forwarded)
        serialized = _serialize_websocket_request("/socket?mode=watch", forwarded)
        request_text = serialized.decode("latin-1")
        self.assertIn(
            f"Authorization: Bearer {provider_secret}\r\n", request_text
        )
        self.assertIn(
            f"X-OptPilot-Presentation-Ingress: {provider_secret}\r\n",
            request_text,
        )
        self.assertIn("Cookie: application_session=user\r\n", request_text)
        self.assertIn("Origin: http://127.0.0.1:29766\r\n", request_text)
        self.assertFalse(
            any(name.lower() == "origin" for name in origin_omitted)
        )
        self.assertNotIn("client-controlled", request_text)
        self.assertNotIn("browser-secret", request_text)

    def test_websocket_response_headers_never_reflect_provider_credentials(self) -> None:
        provider_secret = "provider-websocket-secret"
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(
                route="http://127.0.0.1:45173",
                secret=provider_secret,
            )
        )
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
            f"X-OptPilot-Presentation-Ingress: {provider_secret}\r\n"
            f"X-Echoed-Provider-Secret: {provider_secret}\r\n"
            f"X-{provider_secret}: hidden\r\n"
            "X-Upstream-Visible: yes\r\n"
            "Set-Cookie: application_websocket=ok; Path=/\r\n"
            "Set-Cookie: optpilot_presentation_token=forged; Path=/\r\n"
            "\r\n"
        ).encode("latin-1")
        headers = _validated_websocket_response_headers(
            response,
            client_key="dGhlIHNhbXBsZSBub25jZQ==",
            requested_protocols=(),
            endpoint=endpoint,
        )
        response_text = "\r\n".join(
            f"{name}: {value}" for name, value in headers
        )
        self.assertIn("X-Upstream-Visible: yes", response_text)
        self.assertIn("application_websocket=ok", response_text)
        self.assertNotIn(provider_secret, response_text)
        self.assertNotIn("forged", response_text)
        with self.assertRaises(ValueError):
            _validated_websocket_response_headers(
                response.replace(
                    b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", b"invalid-accept-value"
                ),
                client_key="dGhlIHNhbXBsZSBub25jZQ==",
                requested_protocols=(),
                endpoint=endpoint,
            )

    def test_websocket_relay_is_bidirectional_and_closes_on_fence_loss(self) -> None:
        browser, broker_client = socket.socketpair()
        broker_upstream, application = socket.socketpair()
        browser.settimeout(2)
        application.settimeout(2)
        state = {"valid": True, "validations": 0}

        def validate() -> None:
            state["validations"] += 1
            if not state["valid"]:
                raise RuntimeError("endpoint fence expired")

        thread = threading.Thread(
            target=_relay_websocket_streams,
            kwargs={
                "client_socket": broker_client,
                "upstream_socket": broker_upstream,
                "stop_event": threading.Event(),
                "validate": validate,
                "idle_timeout": 1.0,
                "max_lifetime": 2.0,
                "revalidate_interval": 0.01,
                "max_buffer_bytes": 64,
            },
            daemon=True,
        )
        try:
            thread.start()
            browser.sendall(b"browser-to-application")
            self.assertEqual(
                _recv_exact(application, len(b"browser-to-application")),
                b"browser-to-application",
            )
            application.sendall(b"application-to-browser")
            self.assertEqual(
                _recv_exact(browser, len(b"application-to-browser")),
                b"application-to-browser",
            )
            state["valid"] = False
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(state["validations"], 2)
        finally:
            for connection in (browser, broker_client, broker_upstream, application):
                connection.close()

    def test_websocket_relay_has_bounded_idle_and_absolute_lifetimes(self) -> None:
        for idle_timeout, max_lifetime in ((0.05, 1.0), (1.0, 0.05)):
            with self.subTest(
                idle_timeout=idle_timeout,
                max_lifetime=max_lifetime,
            ):
                browser, broker_client = socket.socketpair()
                broker_upstream, application = socket.socketpair()
                thread = threading.Thread(
                    target=_relay_websocket_streams,
                    kwargs={
                        "client_socket": broker_client,
                        "upstream_socket": broker_upstream,
                        "stop_event": threading.Event(),
                        "validate": lambda: None,
                        "idle_timeout": idle_timeout,
                        "max_lifetime": max_lifetime,
                        "revalidate_interval": 0.01,
                    },
                    daemon=True,
                )
                try:
                    started = time.monotonic()
                    thread.start()
                    thread.join(timeout=1)
                    self.assertFalse(thread.is_alive())
                    self.assertLess(time.monotonic() - started, 0.5)
                finally:
                    for connection in (
                        browser,
                        broker_client,
                        broker_upstream,
                        application,
                    ):
                        connection.close()

    def test_provider_binding_injects_private_authorization_and_wins_collisions(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        provider_secret = "provider-only-secret"
        route = f"http://127.0.0.1:{self.upstream.server_port}"
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(route=route, secret=provider_secret)
        )
        lease = self.broker.open(key="container-job-1", endpoint=endpoint)
        request = Request(
            lease.open_url,
            headers={
                "Authorization": "Bearer client-controlled",
                "X-OptPilot-Presentation-Ingress": "client-controlled",
            },
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["authorization"], f"Bearer {provider_secret}")
        self.assertEqual(payload["presentation_ingress"], provider_secret)

    def test_private_authorization_replaces_case_insensitive_client_collisions(self) -> None:
        provider_secret = "provider-only-secret"
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(
                route="http://127.0.0.1:45173",
                secret=provider_secret,
            )
        )
        headers = {
            "authorization": "Bearer client-controlled",
            "x-optpilot-presentation-ingress": "client-controlled",
            "X-Application-Header": "preserved",
        }
        endpoint._inject_upstream_authorization(headers)
        self.assertEqual(headers["Authorization"], f"Bearer {provider_secret}")
        self.assertEqual(
            headers["X-OptPilot-Presentation-Ingress"], provider_secret
        )
        self.assertEqual(headers["X-Application-Header"], "preserved")
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-optpilot-presentation-ingress", headers)

    def test_provider_credential_is_absent_from_identity_and_public_views(self) -> None:
        route = "http://127.0.0.1:45173"
        first_secret = "first-provider-secret"
        second_secret = "second-provider-secret"
        first = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(route=route, secret=first_secret)
        )
        second = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(route=route, secret=second_secret)
        )
        self.assertEqual(first.ownership_digest, second.ownership_digest)
        self.assertNotIn(first_secret, repr(first))
        self.assertNotIn(second_secret, repr(second))

        lease = WebPresentationLease(
            lease_id="presentation-1",
            key="container-job-1",
            host="127.0.0.1",
            port=45174,
            endpoint=first,
            token="browser-token",
            server=object(),  # type: ignore[arg-type]
            thread=object(),  # type: ignore[arg-type]
        )
        public_text = json.dumps(lease.public_dict(), sort_keys=True)
        self.assertNotIn(first_secret, public_text)
        self.assertNotIn(first_secret, repr(lease))

    def test_provider_binding_is_revalidated_for_authorization_requests(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        state = {"valid": True}
        route = f"http://127.0.0.1:{self.upstream.server_port}"
        endpoint = OwnedWebEndpoint.from_provider_binding(
            _ProviderBinding(route=route, secret="provider-secret", state=state)
        )
        lease = self.broker.open(key="container-job-1", endpoint=endpoint)
        with urlopen(lease.open_url, timeout=2) as response:
            self.assertEqual(response.status, 200)
        state["valid"] = False
        with self.assertRaises(HTTPError) as caught:
            urlopen(lease.open_url, timeout=2)
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(len(_UpstreamHandler.requests), 1)

    def test_changed_provider_headers_fail_closed_before_forwarding(self) -> None:
        route = "http://127.0.0.1:45173"
        binding = _ProviderBinding(route=route, secret="provider-secret")
        endpoint = OwnedWebEndpoint.from_provider_binding(binding)
        binding.authorization_headers = {"Host": "provider-secret"}
        with self.assertRaises(ValueError) as caught:
            endpoint._inject_upstream_authorization({})
        self.assertNotIn("provider-secret", str(caught.exception))

    def test_provider_authorization_headers_fail_closed_without_secret_disclosure(self) -> None:
        route = "http://127.0.0.1:45173"
        secret = "must-not-appear-in-errors"
        invalid_headers = (
            {"Host": secret},
            {"Sec-WebSocket-Key": secret},
            {"X-Private": f"{secret}\r\nInjected: true"},
            {"Authorization": secret, "authorization": secret},
        )
        for headers in invalid_headers:
            with self.subTest(headers=tuple(headers)):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    OwnedWebEndpoint.from_provider_binding(
                        _ProviderBinding(
                            route=route,
                            secret=secret,
                            authorization_headers=headers,
                        )
                    )
                self.assertNotIn(secret, str(caught.exception))

    def test_revalidates_ownership_before_every_request(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None
        state = {"valid": True}
        lease = self.broker.open(key="job-1", endpoint=self._endpoint(state))
        with urlopen(lease.open_url, timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(_UpstreamHandler.requests), 1)

        state["valid"] = False
        with self.assertRaises(HTTPError) as caught:
            urlopen(lease.open_url, timeout=2)
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(len(_UpstreamHandler.requests), 1)

    def test_same_owned_generation_reuses_origin_and_changed_generation_rotates(self) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        endpoint = self._endpoint()
        first = self.broker.open(key="job-1", endpoint=endpoint)
        repeated = self.broker.open(key="job-1", endpoint=self._endpoint())
        self.assertIs(first, repeated)

        port = self.upstream.server_port
        replacement = OwnedWebEndpoint(
            owner_kind="operator-job",
            owner_id="operator-job-1",
            generation="fence-2",
            access_policy="launch-authenticated",
            primary_port=5173,
            routes={5173: f"http://127.0.0.1:{port}"},
            validate=lambda: None,
        )
        second = self.broker.open(key="job-1", endpoint=replacement)
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertNotEqual(first.open_url, second.open_url)

    def test_close_reports_live_proxy_handler_until_it_actually_returns(
        self,
    ) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        main_thread = threading.current_thread()
        entered = threading.Event()
        release = threading.Event()
        client_errors: list[BaseException] = []

        def validate() -> None:
            if threading.current_thread() is not main_thread:
                entered.set()
                release.wait(timeout=5)

        endpoint = OwnedWebEndpoint(
            owner_kind="operator-job",
            owner_id="operator-job-blocked",
            generation="fence-blocked",
            access_policy="launch-authenticated",
            primary_port=5173,
            routes={
                5173: f"http://127.0.0.1:{self.upstream.server_port}"
            },
            validate=validate,
        )
        lease = self.broker.open(key="blocked-proxy", endpoint=endpoint)

        def request_preview() -> None:
            try:
                with urlopen(lease.open_url, timeout=2) as response:
                    response.read()
            except BaseException as error:
                client_errors.append(error)

        client = threading.Thread(target=request_preview, daemon=True)
        client.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            started = time.monotonic()
            self.assertFalse(
                self.broker.close(
                    "blocked-proxy",
                    timeout_seconds=0.05,
                )
            )
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            client.join(timeout=2)

        self.assertFalse(client.is_alive())
        self.assertTrue(
            self.broker.close("blocked-proxy", timeout_seconds=1.0)
        )
        self.assertFalse(lease.running)

    def test_close_all_uses_one_deadline_and_retains_unsettled_lease(
        self,
    ) -> None:
        self._require_loopback_servers()
        assert self.broker is not None and self.upstream is not None
        main_thread = threading.current_thread()
        entered = threading.Event()
        release = threading.Event()

        def validate() -> None:
            if threading.current_thread() is not main_thread:
                entered.set()
                release.wait(timeout=5)

        endpoint = OwnedWebEndpoint(
            owner_kind="operator-job",
            owner_id="operator-job-close-all",
            generation="fence-close-all",
            access_policy="launch-authenticated",
            primary_port=5173,
            routes={
                5173: f"http://127.0.0.1:{self.upstream.server_port}"
            },
            validate=validate,
        )
        lease = self.broker.open(key="blocked-close-all", endpoint=endpoint)

        def request_preview() -> None:
            try:
                urlopen(lease.open_url, timeout=2).close()
            except BaseException:
                pass

        client = threading.Thread(target=request_preview, daemon=True)
        client.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            started = time.monotonic()
            self.assertFalse(
                self.broker.close_all(timeout_seconds=0.05)
            )
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            client.join(timeout=2)

        self.assertFalse(client.is_alive())
        self.assertTrue(self.broker.close_all(timeout_seconds=1.0))


if __name__ == "__main__":
    unittest.main()
