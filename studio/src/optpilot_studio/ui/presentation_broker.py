"""Studio-owned browser presentation for provider-owned web endpoints.

The broker deliberately knows nothing about environments, candidates, or
workspaces.  A runtime provider proves ownership of one or more loopback HTTP
routes and supplies a validator which fences those routes for their complete
lifetime.  Studio then exposes the primary route on a fresh browser origin with
launch-scoped authentication.

Host ports and authentication tokens are presentation-private operational
coordinates.  They must never be copied into a Realm Operator Job result or a
public run projection.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import math
import re
import secrets
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


_TOKEN_QUERY = "__optpilot_presentation_token"
_TOKEN_COOKIE = "optpilot_presentation_token"
_PRIVATE_COOKIE_NAMES = {_TOKEN_COOKIE, "optpilot_preview_token"}
_EXTRA_PORT_PREFIX = "/__optpilot_port/"
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_WEBSOCKET_HANDSHAKE_BYTES = 64 * 1024
_MAX_WEBSOCKET_HANDSHAKE_HEADERS = 100
_MAX_WEBSOCKET_RELAY_BUFFER_BYTES = 1024 * 1024
_WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 10.0
_WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_WEBSOCKET_IDLE_TIMEOUT_SECONDS = 30.0 * 60.0
_WEBSOCKET_MAX_LIFETIME_SECONDS = 24.0 * 60.0 * 60.0
_WEBSOCKET_REVALIDATE_SECONDS = 5.0
_WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_PRESENTATION_CLOSE_TIMEOUT_SECONDS = 2.0
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PRIVATE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-optpilot-presentation-token",
}
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_MAX_PRIVATE_AUTHORIZATION_HEADERS = 16
_MAX_PRIVATE_AUTHORIZATION_VALUE_BYTES = 16 * 1024
_UNSAFE_PRIVATE_AUTHORIZATION_HEADERS = {
    "accept-encoding",
    "content-length",
    "cookie",
    "host",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    *_HOP_BY_HOP_HEADERS,
}


def _required_text(value: object, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text.")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds its byte limit.")
    return value


def _loopback_http_base_url(value: object, label: str) -> str:
    text = _required_text(value, label, max_bytes=4096).rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError(f"{label} must be an absolute loopback HTTP URL with a port.")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError(f"{label} must use a loopback IP literal.") from error
    if not address.is_loopback:
        raise ValueError(f"{label} must use a loopback IP literal.")
    return text


class OwnedWebEndpoint:
    """Provider-private proof that exact loopback routes belong to one launch.

    ``validate`` must fail when the backing launch, its fence, or any published
    route no longer matches.  The broker calls it at open time and again before
    every request, preventing port-reuse confused-deputy failures.
    """

    def __init__(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        generation: str,
        access_policy: str,
        websocket_origin_policy: str = "preserve",
        primary_port: int,
        routes: Mapping[int, str],
        validate: Callable[[], None],
    ) -> None:
        self._owner_kind = _required_text(owner_kind, "endpoint owner kind")
        self._owner_id = _required_text(owner_id, "endpoint owner id")
        self._generation = _required_text(generation, "endpoint generation")
        if access_policy not in {"launch-authenticated", "trusted-local-authoring"}:
            raise ValueError("endpoint access_policy is unsupported.")
        if (
            self._owner_kind.startswith("operator-job")
            and access_policy != "launch-authenticated"
        ):
            raise ValueError(
                "Operator Job endpoints require launch-authenticated ingress."
            )
        self._access_policy = access_policy
        if websocket_origin_policy not in {"preserve", "omit"}:
            raise ValueError("endpoint websocket_origin_policy is unsupported.")
        if (
            websocket_origin_policy == "omit"
            and (
                self._owner_kind != "workspace-runtime"
                or access_policy != "trusted-local-authoring"
            )
        ):
            raise ValueError(
                "Only trusted workspace runtime endpoints may omit WebSocket Origin."
            )
        self._websocket_origin_policy = websocket_origin_policy
        if not callable(validate):
            raise TypeError("endpoint validate must be callable.")
        if not isinstance(routes, Mapping) or not routes:
            raise ValueError("endpoint routes must be a nonempty mapping.")
        normalized: dict[int, str] = {}
        for raw_port, raw_url in routes.items():
            if (
                isinstance(raw_port, bool)
                or not isinstance(raw_port, int)
                or raw_port < 1
                or raw_port > 65535
            ):
                raise ValueError("endpoint route ports must be integers from 1 to 65535.")
            normalized[raw_port] = _loopback_http_base_url(
                raw_url, f"endpoint route {raw_port}"
            )
        if primary_port not in normalized:
            raise ValueError("endpoint primary port is absent from its routes.")
        self._primary_port = primary_port
        self._routes = MappingProxyType(dict(sorted(normalized.items())))
        self._validate = validate
        self._provider_binding: Any | None = None
        digest_input = "\0".join(
            [
                "optpilot-owned-web-endpoint-v1",
                self._owner_kind,
                self._owner_id,
                self._generation,
                self._access_policy,
                self._websocket_origin_policy,
                *(f"{port}={self._routes[port]}" for port in self._routes),
            ]
        ).encode("utf-8")
        self._ownership_digest = hashlib.sha256(digest_input).hexdigest()

    @classmethod
    def from_provider_binding(cls, binding: Any) -> "OwnedWebEndpoint":
        """Wrap one opaque provider capability without exposing its secret.

        A binding is deliberately structural so runtime providers do not
        depend on Studio.  It must carry the complete endpoint identity,
        routes, validation authority, and provider-private authorization
        headers.  Ordinary trusted-local endpoints continue to use the public
        constructor and therefore have no upstream credential.
        """

        if binding is None:
            raise TypeError("provider binding is required.")
        try:
            validate = binding.validate
            endpoint = cls(
                owner_kind=binding.owner_kind,
                owner_id=binding.owner_id,
                generation=binding.generation,
                access_policy=binding.access_policy,
                primary_port=binding.primary_port,
                routes=binding.routes,
                validate=validate,
            )
        except AttributeError as error:
            raise TypeError(
                "provider binding does not implement the owned web endpoint contract."
            ) from error
        # Fail closed before a browser origin is allocated.  Credentials
        # remain owned by the opaque provider binding and are fetched again
        # only while constructing each upstream request.
        _private_authorization_headers(binding)
        endpoint._provider_binding = binding
        return endpoint

    def __repr__(self) -> str:
        return (
            "OwnedWebEndpoint("
            f"owner_kind={self._owner_kind!r}, owner_id={self._owner_id!r}, "
            f"generation={self._generation!r}, access_policy={self._access_policy!r}, "
            f"primary_port={self._primary_port!r}, authorization=<redacted>)"
        )

    @property
    def owner_kind(self) -> str:
        return self._owner_kind

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def ownership_digest(self) -> str:
        return self._ownership_digest

    @property
    def access_policy(self) -> str:
        return self._access_policy

    @property
    def websocket_origin_policy(self) -> str:
        return self._websocket_origin_policy

    @property
    def primary_port(self) -> int:
        return self._primary_port

    @property
    def logical_ports(self) -> tuple[int, ...]:
        return tuple(self._routes)

    def validate(self) -> None:
        self._validate()

    def target_for(self, logical_port: int) -> str:
        self.validate()
        try:
            return self._routes[logical_port]
        except KeyError as error:
            raise ValueError("endpoint logical port is not published.") from error

    def _inject_upstream_authorization(self, headers: dict[str, str]) -> None:
        """Replace client collisions with the provider-owned credential."""

        if self._provider_binding is None:
            return
        private_headers = _private_authorization_headers(self._provider_binding)
        private_names = {name.lower() for name in private_headers}
        for name in tuple(headers):
            if name.lower() in private_names:
                del headers[name]
        headers.update(private_headers)

    def _response_header_is_private(self, name: str, value: str) -> bool:
        """Keep provider credentials out of upstream response headers."""

        if self._provider_binding is None:
            return False
        private_headers = _private_authorization_headers(self._provider_binding)
        if name.lower() in {item.lower() for item in private_headers}:
            return True
        return any(
            secret in name or secret in value
            for secret in private_headers.values()
        )


class _PresentationHTTPServer(ThreadingHTTPServer):
    """Threaded loopback server whose requests and sockets can be revoked."""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._connection_lock = threading.RLock()
        self._active_connections: set[socket.socket] = set()
        self._request_threads: set[threading.Thread] = set()
        self._closing = threading.Event()
        super().__init__(*args, **kwargs)

    def process_request(
        self, request: socket.socket, client_address: Any
    ) -> None:
        """Register the actual handler thread before it can begin running.

        ``ThreadingMixIn`` normally forgets daemon request threads during
        ``server_close``.  Presentation handlers validate provider ownership
        and may be relaying a WebSocket, so the broker needs its own truthful
        quiescence proof before the provider runtime can be released.
        """

        if self._closing.is_set():
            self.shutdown_request(request)
            return
        thread = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name=f"optpilot-presentation-request-{id(request):x}",
            daemon=True,
        )
        with self._connection_lock:
            self._request_threads = {
                candidate
                for candidate in self._request_threads
                if candidate.is_alive()
            }
            self._request_threads.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._connection_lock:
                self._request_threads.discard(thread)
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: Any
    ) -> None:
        with self._connection_lock:
            self._active_connections.add(request)
            closing = self._closing.is_set()
        if closing:
            try:
                self.shutdown_request(request)
            finally:
                with self._connection_lock:
                    self._active_connections.discard(request)
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connection_lock:
                self._active_connections.discard(request)

    def close_active_connections(self) -> None:
        with self._connection_lock:
            connections = tuple(self._active_connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                connection.close()

    def reject_new_requests(self) -> None:
        """Fence accepted-but-not-yet-running requests during shutdown."""

        self._closing.set()

    def handle_error(
        self, request: socket.socket, client_address: Any
    ) -> None:
        # Revoking a presentation deliberately severs browser sockets. Do not
        # emit a traceback when a handler notices that expected disconnect
        # while finishing its current response.
        if isinstance(
            sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)
        ):
            return
        super().handle_error(request, client_address)

    def live_request_threads(self) -> tuple[threading.Thread, ...]:
        """Return handlers which have not actually returned yet."""

        with self._connection_lock:
            live = tuple(
                thread
                for thread in self._request_threads
                if thread.is_alive()
            )
            self._request_threads = set(live)
        return live


@dataclass
class WebPresentationLease:
    """One in-memory, revocable, isolated browser presentation."""

    lease_id: str
    key: str
    host: str
    port: int
    endpoint: OwnedWebEndpoint = field(repr=False)
    token: str = field(repr=False)
    server: ThreadingHTTPServer = field(repr=False)
    thread: threading.Thread = field(repr=False)
    stop_event: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    close_thread: threading.Thread | None = field(default=None, repr=False)
    close_failed: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    started_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def open_url(self) -> str:
        return f"{self.url}?{_TOKEN_QUERY}={quote(self.token)}"

    @property
    def preview_url(self) -> str:
        """UI-facing alias retained while workspace callers are migrated."""

        return self.open_url

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def public_dict(self) -> dict[str, Any]:
        """Return only browser-safe presentation state."""

        return {
            "lease_id": self.lease_id,
            "presentation_kind": "web",
            "open_url": self.open_url,
            "started_at": self.started_at,
            "supports_extra_ports": len(self.endpoint.logical_ports) > 1,
            "supports_websocket": True,
            "access_policy": self.endpoint.access_policy,
        }


class WebPresentationBroker:
    """Own fresh browser origins for verified provider endpoints."""

    def __init__(self, *, host: str = "127.0.0.1", port_start: int = 19766) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("presentation broker host must be loopback.")
        if port_start < 1 or port_start > 65535:
            raise ValueError("presentation broker port_start is invalid.")
        self._host = host
        self._port_start = port_start
        self._leases: dict[str, WebPresentationLease] = {}
        self._lock = threading.RLock()

    def open(self, *, key: str, endpoint: OwnedWebEndpoint) -> WebPresentationLease:
        key = _required_text(key, "presentation key")
        if not isinstance(endpoint, OwnedWebEndpoint):
            raise TypeError("endpoint must be an OwnedWebEndpoint.")
        endpoint.validate()
        with self._lock:
            current = self._leases.get(key)
            if (
                current is not None
                and current.running
                and not current.stop_event.is_set()
                and current.endpoint.ownership_digest == endpoint.ownership_digest
            ):
                # Providers may remint an equivalent opaque broker binding on
                # every reconciliation.  Stable ownership identity, followed
                # by validation of both the retained and newly presented
                # capabilities, is the idempotency boundary; Python object
                # identity is not a runtime contract.
                current.endpoint.validate()
                endpoint.validate()
                return current
            if current is not None:
                deadline = (
                    time.monotonic() + _PRESENTATION_CLOSE_TIMEOUT_SECONDS
                )
                self._begin_close_locked(current)
                if not self._finish_close_locked(
                    key, current, deadline=deadline
                ):
                    raise RuntimeError(
                        "The previous presentation is still closing."
                    )
            token = secrets.token_urlsafe(32)
            stop_event = threading.Event()
            handler = _handler_factory(
                endpoint, token=token, stop_event=stop_event
            )
            server = None
            for port in range(
                self._port_start, min(65536, self._port_start + 1000)
            ):
                try:
                    server = _PresentationHTTPServer((self._host, port), handler)
                    break
                except OSError:
                    continue
            if server is None:
                raise OSError(
                    "No loopback port is available for a presentation origin."
                )
            lease_id = f"presentation-{secrets.token_hex(24)}"
            thread = threading.Thread(
                target=lambda: server.serve_forever(poll_interval=0.1),
                name=f"optpilot-presentation-{lease_id}",
                daemon=True,
            )
            lease = WebPresentationLease(
                lease_id=lease_id,
                key=key,
                host=self._host,
                port=server.server_port,
                endpoint=endpoint,
                token=token,
                server=server,
                thread=thread,
                stop_event=stop_event,
            )
            self._leases[key] = lease
            thread.start()
            return lease

    def close(
        self,
        key: str,
        *,
        timeout_seconds: float = _PRESENTATION_CLOSE_TIMEOUT_SECONDS,
    ) -> bool:
        """Close one origin and prove that every request handler returned."""

        key = _required_text(key, "presentation key")
        deadline = _presentation_close_deadline(timeout_seconds)
        if not self._acquire_lock_before(deadline):
            return False
        try:
            lease = self._leases.get(key)
            if lease is None:
                return True
            self._begin_close_locked(lease)
            return self._finish_close_locked(key, lease, deadline=deadline)
        finally:
            self._lock.release()

    def close_all(
        self,
        *,
        timeout_seconds: float = _PRESENTATION_CLOSE_TIMEOUT_SECONDS,
    ) -> bool:
        """Close all origins under one shared deadline."""

        deadline = _presentation_close_deadline(timeout_seconds)
        if not self._acquire_lock_before(deadline):
            return False
        try:
            leases = tuple(self._leases.items())
            for _key, lease in leases:
                self._begin_close_locked(lease)
            all_quiesced = True
            for key, lease in leases:
                if not self._finish_close_locked(
                    key, lease, deadline=deadline
                ):
                    all_quiesced = False
            return all_quiesced
        finally:
            self._lock.release()

    def _acquire_lock_before(self, deadline: float) -> bool:
        remaining = max(0.0, deadline - time.monotonic())
        return self._lock.acquire(timeout=remaining)

    def _begin_close_locked(self, lease: WebPresentationLease) -> None:
        """Revoke a lease and start listener shutdown without blocking."""

        lease.stop_event.set()
        if isinstance(lease.server, _PresentationHTTPServer):
            lease.server.reject_new_requests()
        current = lease.close_thread
        if current is not None:
            if current.is_alive() or not lease.close_failed.is_set():
                return
            # A later close call may retry a failed listener shutdown while
            # the lease remains owned by the broker.
            lease.close_failed.clear()

        def close_listener() -> None:
            try:
                if isinstance(lease.server, _PresentationHTTPServer):
                    lease.server.close_active_connections()
                if lease.thread.is_alive():
                    lease.server.shutdown()
            except BaseException:
                lease.close_failed.set()
            finally:
                try:
                    if isinstance(lease.server, _PresentationHTTPServer):
                        lease.server.close_active_connections()
                    lease.server.server_close()
                except BaseException:
                    lease.close_failed.set()

        close_thread = threading.Thread(
            target=close_listener,
            name=f"optpilot-presentation-close-{lease.lease_id}",
            daemon=True,
        )
        lease.close_thread = close_thread
        try:
            close_thread.start()
        except BaseException:
            lease.close_failed.set()

    def _finish_close_locked(
        self,
        key: str,
        lease: WebPresentationLease,
        *,
        deadline: float,
    ) -> bool:
        """Wait only to ``deadline`` and retire the lease on full quiescence."""

        close_thread = lease.close_thread
        if close_thread is None or not _join_thread_before(
            close_thread, deadline
        ):
            return False
        if lease.close_failed.is_set():
            return False
        if not _join_thread_before(lease.thread, deadline):
            return False
        if isinstance(lease.server, _PresentationHTTPServer):
            while True:
                request_threads = lease.server.live_request_threads()
                if not request_threads:
                    break
                for request_thread in request_threads:
                    if not _join_thread_before(request_thread, deadline):
                        return False
        if self._leases.get(key) is lease:
            self._leases.pop(key, None)
        return True


def _presentation_close_deadline(timeout_seconds: float) -> float:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "presentation close timeout must be a finite nonnegative number."
        ) from error
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(
            "presentation close timeout must be a finite nonnegative number."
        )
    return time.monotonic() + timeout


def _join_thread_before(thread: threading.Thread, deadline: float) -> bool:
    if thread is threading.current_thread():
        return False
    if not thread.is_alive():
        return True
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not thread.is_alive()


def _handler_factory(
    endpoint: OwnedWebEndpoint,
    *,
    token: str,
    stop_event: threading.Event | None = None,
):
    presentation_token = _required_text(token, "presentation token")
    handler_token = presentation_token
    handler_stop_event = stop_event or threading.Event()

    class PresentationProxyHandler(BaseHTTPRequestHandler):
        server_version = "OptPilotPresentation/1"
        protocol_version = "HTTP/1.1"
        presentation_token = handler_token
        presentation_stop_event = handler_stop_event

        def do_GET(self) -> None:  # noqa: N802
            if _looks_like_websocket_upgrade(self.headers):
                self._websocket()
            else:
                self._proxy()

        def do_HEAD(self) -> None:  # noqa: N802
            self._proxy(head_only=True)

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy()

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy()

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._proxy()

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _proxy(self, *, head_only: bool = False) -> None:
            parsed = urlparse(self.path)
            supplied_by_query = self._query_token(parsed) == presentation_token
            if not self._authorized(parsed):
                self._text(HTTPStatus.FORBIDDEN, "Presentation token is missing or invalid.", head_only)
                return
            try:
                upstream = self._upstream(parsed)
                endpoint.validate()
            except (RuntimeError, ValueError) as error:
                self._text(HTTPStatus.BAD_GATEWAY, str(error), head_only)
                return
            try:
                body = self._body()
            except ValueError as error:
                self._text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(error), head_only)
                return
            headers = {
                key: value
                for key, value in self.headers.items()
                if _forward_request_header(key)
            }
            app_cookie = _application_cookie(self.headers.get("Cookie", ""))
            if app_cookie:
                headers["Cookie"] = app_cookie
            headers["Accept-Encoding"] = "identity"
            try:
                endpoint._inject_upstream_authorization(headers)
            except (TypeError, ValueError):
                self._text(
                    HTTPStatus.BAD_GATEWAY,
                    "The provider authorization binding is invalid.",
                    head_only,
                )
                return
            request = Request(upstream, data=body, headers=headers, method=self.command)
            try:
                with urlopen(request, timeout=30) as response:
                    data = _bounded_read(response, _MAX_RESPONSE_BYTES)
                    self._response(
                        response.status,
                        response.headers,
                        data,
                        head_only=head_only,
                        set_token_cookie=supplied_by_query,
                    )
            except HTTPError as error:
                data = _bounded_read(error, _MAX_RESPONSE_BYTES)
                self._response(
                    error.code,
                    error.headers,
                    data,
                    head_only=head_only,
                    set_token_cookie=supplied_by_query,
                )
            except URLError:
                self._text(
                    HTTPStatus.BAD_GATEWAY,
                    "The owned presentation endpoint is unavailable.",
                    head_only,
                )
            except ValueError as error:
                self._text(HTTPStatus.BAD_GATEWAY, str(error), head_only)

        def _websocket(self) -> None:
            self.close_connection = True
            parsed = urlparse(self.path)
            supplied_by_query = self._query_token(parsed) == presentation_token
            if not self._authorized(parsed):
                self._text(
                    HTTPStatus.FORBIDDEN,
                    "Presentation token is missing or invalid.",
                    False,
                )
                return
            if self.presentation_stop_event.is_set():
                self._text(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "The presentation lease is closing.",
                    False,
                )
                return
            try:
                request_headers, client_key, requested_protocols = (
                    _validated_websocket_request_headers(self.headers)
                )
            except ValueError:
                self._text(
                    HTTPStatus.BAD_REQUEST,
                    "The WebSocket upgrade request is invalid.",
                    False,
                )
                return
            try:
                upstream = self._upstream(parsed)
                endpoint.validate()
                host, port, request_target, host_header = (
                    _websocket_upstream_coordinates(upstream)
                )
                forwarded_headers = _websocket_forward_headers(
                    request_headers,
                    host_header=host_header,
                    origin_policy=endpoint.websocket_origin_policy,
                )
                endpoint._inject_upstream_authorization(forwarded_headers)
                request_bytes = _serialize_websocket_request(
                    request_target, forwarded_headers
                )
            except (TypeError, ValueError, RuntimeError):
                self._text(
                    HTTPStatus.BAD_GATEWAY,
                    "The owned WebSocket endpoint is invalid.",
                    False,
                )
                return

            switched = False
            try:
                with socket.create_connection(
                    (host, port), timeout=_WEBSOCKET_CONNECT_TIMEOUT_SECONDS
                ) as upstream_socket:
                    upstream_socket.settimeout(
                        _WEBSOCKET_HANDSHAKE_TIMEOUT_SECONDS
                    )
                    endpoint.validate()
                    upstream_socket.sendall(request_bytes)
                    response_block, initial_upstream_data = (
                        _read_websocket_handshake(upstream_socket)
                    )
                    response_headers = _validated_websocket_response_headers(
                        response_block,
                        client_key=client_key,
                        requested_protocols=requested_protocols,
                        endpoint=endpoint,
                    )
                    # Fence once more after the network round trip, immediately
                    # before granting the browser a long-lived upgraded stream.
                    endpoint.validate()
                    self._send_websocket_switch(
                        response_headers,
                        set_token_cookie=supplied_by_query,
                    )
                    switched = True
                    if initial_upstream_data:
                        self.connection.sendall(initial_upstream_data)
                    _relay_websocket_streams(
                        self.connection,
                        upstream_socket,
                        stop_event=self.presentation_stop_event,
                        validate=endpoint.validate,
                    )
            except (OSError, TypeError, ValueError, RuntimeError):
                if not switched:
                    self._text(
                        HTTPStatus.BAD_GATEWAY,
                        "The owned WebSocket endpoint is unavailable.",
                        False,
                    )

        def _send_websocket_switch(
            self,
            headers: tuple[tuple[str, str], ...],
            *,
            set_token_cookie: bool,
        ) -> None:
            self.send_response_only(HTTPStatus.SWITCHING_PROTOCOLS)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            for name, value in headers:
                self.send_header(name, value)
            if set_token_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{_TOKEN_COOKIE}={presentation_token}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.flush()

        def _authorized(self, parsed: Any) -> bool:
            if self._query_token(parsed) == presentation_token:
                return True
            for chunk in self.headers.get("Cookie", "").split(";"):
                name, separator, value = chunk.strip().partition("=")
                if separator and name == _TOKEN_COOKIE and value == presentation_token:
                    return True
            return False

        def _query_token(self, parsed: Any) -> str:
            values = parse_qs(parsed.query, keep_blank_values=True).get(
                _TOKEN_QUERY, []
            )
            return str(values[0]) if values else ""

        def _upstream(self, parsed: Any) -> str:
            logical_port = endpoint.primary_port
            path = parsed.path
            if path.startswith(_EXTRA_PORT_PREFIX):
                remainder = path.removeprefix(_EXTRA_PORT_PREFIX)
                port_text, separator, tail = remainder.partition("/")
                try:
                    logical_port = int(port_text)
                except ValueError as error:
                    raise ValueError("Presentation extra-port route is invalid.") from error
                path = f"/{tail}" if separator else "/"
            target = endpoint.target_for(logical_port)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop(_TOKEN_QUERY, None)
            query = urlencode(params, doseq=True)
            return f"{target}/{path.lstrip('/')}{'?' + query if query else ''}"

        def _body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError as error:
                raise ValueError("Presentation request Content-Length is invalid.") from error
            if length < 0 or length > _MAX_REQUEST_BYTES:
                raise ValueError("Presentation request exceeds its byte limit.")
            return self.rfile.read(length) if length else None

        def _response(
            self,
            status: int,
            headers: Any,
            data: bytes,
            *,
            head_only: bool,
            set_token_cookie: bool,
        ) -> None:
            self.send_response(status)
            for key, value in headers.items():
                lowered = key.lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in {
                    "content-length",
                    "content-encoding",
                }:
                    continue
                if (
                    lowered == "set-cookie"
                    and _cookie_name(value) in _PRIVATE_COOKIE_NAMES
                ):
                    continue
                if endpoint._response_header_is_private(key, str(value)):
                    continue
                self.send_header(key, value)
            if set_token_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{_TOKEN_COOKIE}={presentation_token}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _text(self, status: HTTPStatus, message: str, head_only: bool) -> None:
            data = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

    return PresentationProxyHandler


def _looks_like_websocket_upgrade(headers: Any) -> bool:
    return bool(
        headers.get("Upgrade")
        or headers.get("Sec-WebSocket-Key")
        or headers.get("Sec-WebSocket-Version")
    )


def _validated_websocket_request_headers(
    headers: Any,
) -> tuple[tuple[tuple[str, str], ...], str, tuple[str, ...]]:
    items = _validated_http_header_items(
        headers.items(),
        label="WebSocket request",
    )
    if "websocket" not in _comma_header_tokens(items, "upgrade"):
        raise ValueError("WebSocket Upgrade header is missing.")
    if "upgrade" not in _comma_header_tokens(items, "connection"):
        raise ValueError("WebSocket Connection header is missing.")
    keys = _header_values(items, "sec-websocket-key")
    versions = _header_values(items, "sec-websocket-version")
    if len(keys) != 1 or len(versions) != 1 or versions[0].strip() != "13":
        raise ValueError("WebSocket version or key is invalid.")
    client_key = keys[0].strip()
    try:
        decoded_key = base64.b64decode(client_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("WebSocket key is invalid.") from error
    if len(decoded_key) != 16:
        raise ValueError("WebSocket key is invalid.")
    if _header_values(items, "content-length") or _header_values(
        items, "transfer-encoding"
    ):
        raise ValueError("WebSocket handshakes may not carry a request body.")
    protocols: list[str] = []
    for value in _header_values(items, "sec-websocket-protocol"):
        for protocol in value.split(","):
            token = protocol.strip()
            if not token or _HTTP_HEADER_NAME_RE.fullmatch(token) is None:
                raise ValueError("WebSocket subprotocol is invalid.")
            if token in protocols:
                raise ValueError("WebSocket subprotocols must be unique.")
            protocols.append(token)
    return items, client_key, tuple(protocols)


def _validated_http_header_items(
    raw_items: Any,
    *,
    label: str,
) -> tuple[tuple[str, str], ...]:
    items = tuple(raw_items)
    if len(items) > _MAX_WEBSOCKET_HANDSHAKE_HEADERS:
        raise ValueError(f"{label} has too many headers.")
    total_bytes = 0
    normalized: list[tuple[str, str]] = []
    for raw_name, raw_value in items:
        if (
            not isinstance(raw_name, str)
            or _HTTP_HEADER_NAME_RE.fullmatch(raw_name) is None
            or not isinstance(raw_value, str)
            or _contains_unsafe_http_value_character(raw_value)
        ):
            raise ValueError(f"{label} contains an invalid header.")
        try:
            encoded_name = raw_name.encode("ascii")
            encoded_value = raw_value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError(f"{label} contains an invalid header.") from error
        total_bytes += len(encoded_name) + len(encoded_value) + 4
        if total_bytes > _MAX_WEBSOCKET_HANDSHAKE_BYTES:
            raise ValueError(f"{label} headers exceed their byte limit.")
        normalized.append((raw_name, raw_value.strip(" \t")))
    return tuple(normalized)


def _contains_unsafe_http_value_character(value: str) -> bool:
    return any(
        character in {"\r", "\n", "\x00"}
        or (ord(character) < 32 and character != "\t")
        or ord(character) == 127
        for character in value
    )


def _header_values(
    items: tuple[tuple[str, str], ...], name: str
) -> tuple[str, ...]:
    lowered = name.lower()
    return tuple(value for key, value in items if key.lower() == lowered)


def _comma_header_tokens(
    items: tuple[tuple[str, str], ...], name: str
) -> set[str]:
    return {
        token.strip().lower()
        for value in _header_values(items, name)
        for token in value.split(",")
        if token.strip()
    }


def _websocket_upstream_coordinates(
    upstream: str,
) -> tuple[str, int, str, str]:
    parsed = urlparse(upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("WebSocket upstream URL is invalid.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("WebSocket upstream must use a loopback IP literal.") from error
    if not address.is_loopback:
        raise ValueError("WebSocket upstream must use a loopback IP literal.")
    path = parsed.path or "/"
    if parsed.params:
        path = f"{path};{parsed.params}"
    request_target = f"{path}{'?' + parsed.query if parsed.query else ''}"
    try:
        target_bytes = request_target.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("WebSocket request target must be ASCII.") from error
    if (
        not target_bytes
        or len(target_bytes) > 16 * 1024
        or any(byte <= 32 or byte == 127 for byte in target_bytes)
    ):
        raise ValueError("WebSocket request target is invalid.")
    host_header = (
        f"[{parsed.hostname}]:{parsed.port}"
        if ":" in parsed.hostname
        else f"{parsed.hostname}:{parsed.port}"
    )
    return parsed.hostname, parsed.port, request_target, host_header


def _websocket_forward_headers(
    request_headers: tuple[tuple[str, str], ...],
    *,
    host_header: str,
    origin_policy: str = "preserve",
) -> dict[str, str]:
    if origin_policy not in {"preserve", "omit"}:
        raise ValueError("WebSocket Origin forwarding policy is unsupported.")
    forwarded: dict[str, str] = {}
    for name, value in request_headers:
        lowered = name.lower()
        if lowered in {"accept-encoding", "content-length"} or (
            lowered == "origin" and origin_policy == "omit"
        ):
            continue
        if _forward_request_header(name):
            _append_combined_header(forwarded, name, value)
    cookies = "; ".join(_header_values(request_headers, "cookie"))
    application_cookie = _application_cookie(cookies)
    if application_cookie:
        forwarded["Cookie"] = application_cookie
    forwarded["Host"] = host_header
    forwarded["Upgrade"] = "websocket"
    forwarded["Connection"] = "Upgrade"
    return forwarded


def _append_combined_header(
    headers: dict[str, str], name: str, value: str
) -> None:
    lowered = name.lower()
    for current in tuple(headers):
        if current.lower() == lowered:
            headers[current] = f"{headers[current]}, {value}"
            return
    headers[name] = value


def _serialize_websocket_request(
    request_target: str, headers: Mapping[str, str]
) -> bytes:
    items = _validated_http_header_items(
        headers.items(),
        label="WebSocket upstream request",
    )
    try:
        request_line = f"GET {request_target} HTTP/1.1\r\n".encode("ascii")
        header_lines = b"".join(
            f"{name}: {value}\r\n".encode("latin-1") for name, value in items
        )
    except UnicodeEncodeError as error:
        raise ValueError("WebSocket upstream request is not encodable.") from error
    serialized = request_line + header_lines + b"\r\n"
    if len(serialized) > _MAX_WEBSOCKET_HANDSHAKE_BYTES:
        raise ValueError("WebSocket upstream request exceeds its byte limit.")
    return serialized


def _read_websocket_handshake(
    upstream_socket: socket.socket,
) -> tuple[bytes, bytes]:
    received = bytearray()
    delimiter = b"\r\n\r\n"
    while True:
        chunk = upstream_socket.recv(65_536)
        if not chunk:
            raise ValueError("WebSocket upstream closed before its handshake.")
        received.extend(chunk)
        end = received.find(delimiter)
        if end >= 0:
            end += len(delimiter)
            if end > _MAX_WEBSOCKET_HANDSHAKE_BYTES:
                raise ValueError("WebSocket upstream handshake is too large.")
            return bytes(received[:end]), bytes(received[end:])
        if len(received) > _MAX_WEBSOCKET_HANDSHAKE_BYTES:
            raise ValueError("WebSocket upstream handshake is too large.")


def _validated_websocket_response_headers(
    response_block: bytes,
    *,
    client_key: str,
    requested_protocols: tuple[str, ...],
    endpoint: OwnedWebEndpoint,
) -> tuple[tuple[str, str], ...]:
    if not response_block.endswith(b"\r\n\r\n"):
        raise ValueError("WebSocket upstream handshake is incomplete.")
    lines = response_block[:-4].split(b"\r\n")
    if not lines or re.fullmatch(rb"HTTP/1\.[01] 101(?:[ \t].*)?", lines[0]) is None:
        raise ValueError("WebSocket upstream did not switch protocols.")
    raw_headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise ValueError("WebSocket upstream returned an invalid header.")
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii")
            value = raw_value.strip(b" \t").decode("latin-1")
        except UnicodeDecodeError as error:
            raise ValueError(
                "WebSocket upstream returned an invalid header."
            ) from error
        raw_headers.append((name, value))
    items = _validated_http_header_items(
        raw_headers,
        label="WebSocket upstream response",
    )
    if "websocket" not in _comma_header_tokens(items, "upgrade"):
        raise ValueError("WebSocket upstream Upgrade header is invalid.")
    if "upgrade" not in _comma_header_tokens(items, "connection"):
        raise ValueError("WebSocket upstream Connection header is invalid.")
    accepts = _header_values(items, "sec-websocket-accept")
    expected_accept = base64.b64encode(
        hashlib.sha1(
            f"{client_key}{_WEBSOCKET_ACCEPT_GUID}".encode("ascii"),
            usedforsecurity=False,
        ).digest()
    ).decode("ascii")
    if len(accepts) != 1 or not secrets.compare_digest(
        accepts[0].strip(), expected_accept
    ):
        raise ValueError("WebSocket upstream acceptance proof is invalid.")
    selected_protocols = _header_values(items, "sec-websocket-protocol")
    if len(selected_protocols) > 1:
        raise ValueError("WebSocket upstream selected multiple subprotocols.")
    selected_protocol = ""
    if selected_protocols:
        selected_protocol = selected_protocols[0].strip()
        if (
            not selected_protocol
            or "," in selected_protocol
            or selected_protocol not in requested_protocols
        ):
            raise ValueError("WebSocket upstream selected an invalid subprotocol.")

    sanitized: list[tuple[str, str]] = [
        ("Sec-WebSocket-Accept", expected_accept)
    ]
    for name, value in items:
        lowered = name.lower()
        if lowered in {
            "connection",
            "content-encoding",
            "content-length",
            "sec-websocket-accept",
            "sec-websocket-protocol",
            "upgrade",
            *_HOP_BY_HOP_HEADERS,
        }:
            continue
        if (
            lowered == "set-cookie"
            and _cookie_name(value) in _PRIVATE_COOKIE_NAMES
        ):
            continue
        if endpoint._response_header_is_private(name, value):
            continue
        sanitized.append((name, value))
    if selected_protocol:
        sanitized.append(("Sec-WebSocket-Protocol", selected_protocol))
    return tuple(sanitized)


def _relay_websocket_streams(
    client_socket: socket.socket,
    upstream_socket: socket.socket,
    *,
    stop_event: threading.Event,
    validate: Callable[[], None],
    idle_timeout: float = _WEBSOCKET_IDLE_TIMEOUT_SECONDS,
    max_lifetime: float = _WEBSOCKET_MAX_LIFETIME_SECONDS,
    revalidate_interval: float = _WEBSOCKET_REVALIDATE_SECONDS,
    max_buffer_bytes: int = _MAX_WEBSOCKET_RELAY_BUFFER_BYTES,
) -> None:
    if (
        idle_timeout <= 0
        or max_lifetime <= 0
        or revalidate_interval <= 0
        or max_buffer_bytes <= 0
    ):
        raise ValueError("WebSocket relay bounds must be positive.")
    sockets = (client_socket, upstream_socket)
    peers = {client_socket: upstream_socket, upstream_socket: client_socket}
    buffers = {client_socket: bytearray(), upstream_socket: bytearray()}
    read_open = {client_socket: True, upstream_socket: True}
    write_shutdown: set[socket.socket] = set()
    registered: set[socket.socket] = set()
    selector = selectors.DefaultSelector()
    started = last_activity = time.monotonic()
    next_validation = started
    try:
        for relay_socket in sockets:
            relay_socket.setblocking(False)
        while not stop_event.is_set():
            now = time.monotonic()
            if now - started >= max_lifetime or now - last_activity >= idle_timeout:
                break
            if now >= next_validation:
                try:
                    validate()
                except Exception:
                    break
                next_validation = now + revalidate_interval

            for relay_socket in sockets:
                events = 0
                destination = peers[relay_socket]
                if (
                    read_open[relay_socket]
                    and len(buffers[destination]) < max_buffer_bytes
                ):
                    events |= selectors.EVENT_READ
                if buffers[relay_socket]:
                    events |= selectors.EVENT_WRITE
                if events and relay_socket in registered:
                    selector.modify(relay_socket, events)
                elif events:
                    selector.register(relay_socket, events)
                    registered.add(relay_socket)
                elif relay_socket in registered:
                    selector.unregister(relay_socket)
                    registered.remove(relay_socket)
            if not registered:
                break
            timeout = min(
                1.0,
                max(0.0, max_lifetime - (now - started)),
                max(0.0, idle_timeout - (now - last_activity)),
                max(0.0, next_validation - now),
            )
            for selected, events in selector.select(timeout):
                relay_socket = selected.fileobj
                assert isinstance(relay_socket, socket.socket)
                destination = peers[relay_socket]
                if events & selectors.EVENT_READ:
                    remaining = max_buffer_bytes - len(buffers[destination])
                    try:
                        data = relay_socket.recv(min(65_536, remaining))
                    except BlockingIOError:
                        data = None
                    except OSError:
                        return
                    if data:
                        buffers[destination].extend(data)
                        last_activity = time.monotonic()
                    elif data == b"":
                        read_open[relay_socket] = False
                if events & selectors.EVENT_WRITE and buffers[relay_socket]:
                    try:
                        sent = relay_socket.send(buffers[relay_socket])
                    except BlockingIOError:
                        sent = None
                    except OSError:
                        return
                    if sent is not None and sent <= 0:
                        return
                    if sent:
                        del buffers[relay_socket][:sent]
                        last_activity = time.monotonic()

            for source in sockets:
                destination = peers[source]
                if (
                    not read_open[source]
                    and not buffers[destination]
                    and destination not in write_shutdown
                ):
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_shutdown.add(destination)
    finally:
        selector.close()
        for relay_socket in sockets:
            try:
                relay_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _forward_request_header(name: str) -> bool:
    lowered = name.lower()
    if lowered in {"host", "cookie"} or lowered in _HOP_BY_HOP_HEADERS:
        return False
    if lowered in _PRIVATE_HEADERS or lowered.startswith("x-optpilot-"):
        return False
    return True


def _private_authorization_headers(binding: Any) -> dict[str, str]:
    """Validate and copy a provider binding's secret headers for one request."""

    try:
        raw_headers = binding.authorization_headers
    except AttributeError as error:
        raise TypeError(
            "provider binding does not supply authorization_headers."
        ) from error
    if not isinstance(raw_headers, Mapping) or not raw_headers:
        raise ValueError(
            "provider authorization_headers must be a nonempty mapping."
        )
    if len(raw_headers) > _MAX_PRIVATE_AUTHORIZATION_HEADERS:
        raise ValueError("provider authorization_headers exceeds its item limit.")
    normalized: dict[str, str] = {}
    seen_names: set[str] = set()
    for raw_name, raw_value in raw_headers.items():
        if (
            not isinstance(raw_name, str)
            or _HTTP_HEADER_NAME_RE.fullmatch(raw_name) is None
        ):
            raise ValueError("provider authorization header name is invalid.")
        lowered = raw_name.lower()
        if lowered in seen_names:
            raise ValueError(
                "provider authorization header names must be case-insensitively unique."
            )
        if lowered in _UNSAFE_PRIVATE_AUTHORIZATION_HEADERS:
            raise ValueError(
                "provider authorization header may not control HTTP routing or framing."
            )
        if (
            not isinstance(raw_value, str)
            or not raw_value
            or "\r" in raw_value
            or "\n" in raw_value
            or "\x00" in raw_value
            or len(raw_value.encode("utf-8"))
            > _MAX_PRIVATE_AUTHORIZATION_VALUE_BYTES
        ):
            raise ValueError("provider authorization header value is invalid.")
        seen_names.add(lowered)
        normalized[raw_name] = raw_value
    return normalized


def _application_cookie(value: str) -> str:
    return "; ".join(
        chunk.strip()
        for chunk in value.split(";")
        if chunk.strip().partition("=")[0] not in _PRIVATE_COOKIE_NAMES
    )


def _cookie_name(value: str) -> str:
    return value.partition(";")[0].partition("=")[0].strip()


def _bounded_read(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Presentation response exceeds its byte limit.")
    return data


__all__ = [
    "OwnedWebEndpoint",
    "WebPresentationBroker",
    "WebPresentationLease",
]
