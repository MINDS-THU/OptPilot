"""Serve the production/AGV Unity interface and its private MQTT bridge."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import mimetypes
import re
import secrets
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from interface_runtime import (
    MAX_REQUEST_BYTES,
    TOPIC_ROOT,
    CandidateReplayManager,
    InterfaceRequestError,
)
from mqtt_bridge import LocalMQTTBroker


_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_MAX_REGISTERED_WEBSOCKET_ORIGINS = 8


class InterfaceApplication:
    """Environment-owned state shared by HTTP request threads."""

    def __init__(
        self,
        *,
        environment_root: Path,
        candidate_root: Path | None = None,
        runtime_root: Path | None = None,
        viewer_wait_seconds: float = 12.0,
    ) -> None:
        self.environment_root = environment_root.resolve()
        self.web_root = self.environment_root / "interface_web"
        self.unity_root = self.environment_root / "unity_webgl"
        for required in (
            self.web_root / "index.html",
            self.unity_root / "index.html",
            self.unity_root / "Build" / "SimPy.loader.js",
        ):
            if not required.is_file():
                raise FileNotFoundError(f"Interface asset is missing: {required}")
        self.broker = LocalMQTTBroker()
        self.replays = CandidateReplayManager(
            environment_root=self.environment_root,
            broker=self.broker,
            candidate_root=candidate_root,
            runtime_root=runtime_root,
            viewer_wait_seconds=viewer_wait_seconds,
        )
        self.client_id = "optpilot-unity-" + secrets.token_hex(8)
        self._origin_lock = threading.Lock()
        self._websocket_origins: set[str] = set()

    def close(self) -> None:
        self.replays.close()
        self.broker.close()

    def mqtt_config(self, headers: Any) -> dict[str, Any]:
        """Create the viewer config for the browser-visible presentation origin."""

        host, port, secure = _presentation_endpoint(headers)
        if _config_request_can_register_origin(headers):
            origin = _endpoint_origin(host, port, secure)
            with self._origin_lock:
                if (
                    origin in self._websocket_origins
                    or len(self._websocket_origins)
                    < _MAX_REGISTERED_WEBSOCKET_ORIGINS
                ):
                    self._websocket_origins.add(origin)
        connection = {
            "auto_reconnect": True,
            "client_id": self.client_id,
            "connect_timeout": 15,
            "host": host,
            "keep_alive": 30,
            "mqtt_version": 5,
            "password": "",
            "port": port,
            "reconnect_delay": 2,
            "subscribe_broker_topic": "",
            "username": "",
        }
        mode = "wss" if secure else "ws"
        return {
            "common_topic": {"Root_Topic_Head": TOPIC_ROOT},
            "connect_mode": {mode: True},
            "tcp": dict(connection),
            "ws": dict(connection),
            "wss": dict(connection),
        }

    def websocket_origin_allowed(self, value: str | None) -> bool:
        """Accept registered browser origins and origin-less non-browser clients."""

        if not value:
            return True
        origin = _canonical_origin(value)
        if origin is None:
            return False
        with self._origin_lock:
            return origin in self._websocket_origins


class InterfaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: InterfaceApplication,
    ) -> None:
        self.application = application
        super().__init__(server_address, InterfaceRequestHandler)


class InterfaceRequestHandler(BaseHTTPRequestHandler):
    """Bounded same-origin API, static host, and WebSocket upgrade."""

    protocol_version = "HTTP/1.1"
    server_version = "OptPilotAGVInterface/1"

    @property
    def application(self) -> InterfaceApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._get(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._get(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/run":
                response = self.application.replays.start(payload)
                self._json(HTTPStatus.ACCEPTED, response)
            elif path == "/api/replay":
                response = self.application.replays.replay(payload)
                self._json(HTTPStatus.ACCEPTED, response)
            elif path == "/api/stop":
                self._json(HTTPStatus.OK, self.application.replays.stop())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
        except InterfaceRequestError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid request: {error}"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get(self, *, head_only: bool) -> None:
        path = urlparse(self.path).path
        if path == "/mqtt":
            if head_only:
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "WebSocket required."})
                return
            self._websocket()
            return
        if path == "/ready":
            self._json(
                HTTPStatus.OK,
                {
                    "service": "production-agv-unity-interface",
                    "status": "ready",
                },
                head_only=head_only,
            )
            return
        if path == "/api/state":
            self._json(
                HTTPStatus.OK,
                self.application.replays.state(),
                head_only=head_only,
            )
            return
        if path == "/api/candidate":
            try:
                candidate = self.application.replays.candidate_payload()
            except (OSError, InterfaceRequestError, UnicodeDecodeError) as error:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(error)},
                    head_only=head_only,
                )
                return
            self._json(HTTPStatus.OK, candidate, head_only=head_only)
            return
        if path == "/unity/StreamingAssets/MQTTBroker.json":
            self._json(
                HTTPStatus.OK,
                self.application.mqtt_config(self.headers),
                head_only=head_only,
                cache_control="no-store",
            )
            return
        if path in {"", "/"}:
            self._static(
                self.application.web_root,
                "index.html",
                head_only=head_only,
                cache_control="no-store",
            )
            return
        if path.startswith("/assets/"):
            self._static(
                self.application.web_root,
                path.removeprefix("/assets/"),
                head_only=head_only,
                cache_control="public, max-age=300",
            )
            return
        if path in {"/unity", "/unity/"}:
            self._static(
                self.application.unity_root,
                "index.html",
                head_only=head_only,
                cache_control="no-store",
            )
            return
        if path.startswith("/unity/"):
            self._static(
                self.application.unity_root,
                path.removeprefix("/unity/"),
                head_only=head_only,
                cache_control="public, max-age=31536000, immutable",
            )
            return
        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": "Endpoint not found."},
            head_only=head_only,
        )

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("Content-Length is invalid.") from error
        if not 0 <= length <= MAX_REQUEST_BYTES:
            self.close_connection = True
            raise ValueError("Request body exceeds the interface limit.")
        if length == 0:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        head_only: bool = False,
        cache_control: str = "no-store",
    ) -> None:
        data = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _static(
        self,
        root: Path,
        relative_name: str,
        *,
        head_only: bool,
        cache_control: str,
    ) -> None:
        try:
            relative = PurePosixPath(unquote(relative_name))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError
            path = root.joinpath(*relative.parts).resolve()
            path.relative_to(root.resolve())
        except (ValueError, OSError):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Static path is invalid."},
                head_only=head_only,
            )
            return
        if not path.is_file():
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "Asset not found."},
                head_only=head_only,
            )
            return
        media_type, content_encoding = _asset_type(path)
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                self.wfile.write(chunk)

    def _websocket(self) -> None:
        origin = self.headers.get("Origin")
        if not self.application.websocket_origin_allowed(origin):
            self.close_connection = True
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "WebSocket Origin was not registered by the viewer config."},
            )
            return
        try:
            key, protocol = _validated_websocket_headers(self.headers)
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        accept = base64.b64encode(
            hashlib.sha1(
                key.encode("ascii")
                + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
                usedforsecurity=False,
            ).digest()
        ).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.send_header("Sec-WebSocket-Protocol", protocol)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.application.broker.serve_websocket(self.connection)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    environment_root: Path | None = None,
    candidate_root: Path | None = None,
    runtime_root: Path | None = None,
    viewer_wait_seconds: float = 12.0,
) -> InterfaceHTTPServer:
    root = (
        Path(__file__).resolve().parent
        if environment_root is None
        else environment_root
    )
    application = InterfaceApplication(
        environment_root=root,
        candidate_root=candidate_root,
        runtime_root=runtime_root,
        viewer_wait_seconds=viewer_wait_seconds,
    )
    try:
        return InterfaceHTTPServer((host, port), application)
    except Exception:
        application.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    arguments = parser.parse_args(argv)
    server = create_server(host=arguments.host, port=arguments.port)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except ValueError:  # pragma: no cover - non-main-thread embedding
            pass
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        server.application.close()
    return 0


def _presentation_endpoint(headers: Any) -> tuple[str, int, bool]:
    """Derive the browser-visible endpoint, including through Studio's proxy."""

    app_host, app_port = _request_host_endpoint(headers)
    for name in ("Origin", "Referer"):
        value = headers.get(name)
        if not value:
            continue
        endpoint = _http_endpoint(value)
        if endpoint is not None and _origin_host_allowed(app_host, endpoint[0]):
            return endpoint
    return app_host, app_port, False


def _request_host_endpoint(headers: Any) -> tuple[str, int]:
    host_header = headers.get("Host", "127.0.0.1:8080")
    parsed = urlparse("//" + host_header)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not _safe_host(parsed.hostname)
    ):
        return "127.0.0.1", 8080
    return parsed.hostname, port or 80


def _origin_host_allowed(app_host: str, origin_host: str) -> bool:
    if _is_loopback_host(app_host):
        return _is_loopback_host(origin_host)
    return origin_host.lower() == app_host.lower()


def _is_loopback_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _http_endpoint(value: str) -> tuple[str, int, bool] | None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not _safe_host(parsed.hostname)
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    secure = parsed.scheme == "https"
    return parsed.hostname, port or (443 if secure else 80), secure


def _endpoint_origin(host: str, port: int, secure: bool) -> str:
    scheme = "https" if secure else "http"
    display_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{display_host.lower()}:{port}"


def _canonical_origin(value: str) -> str | None:
    endpoint = _http_endpoint(value)
    if endpoint is None:
        return None
    return _endpoint_origin(*endpoint)


def _config_request_can_register_origin(headers: Any) -> bool:
    # Browser cross-site fetches must not be able to register their own Origin
    # before opening a WebSocket. Same-origin viewer fetches and non-browser
    # clients (which generally omit Fetch Metadata) remain supported.
    fetch_site = headers.get("Sec-Fetch-Site", "").strip().lower()
    return fetch_site in {"", "none", "same-origin"}


def _safe_host(host: str) -> bool:
    if ":" in host:
        # urlparse already validates IPv6 syntax; Unity receives the literal
        # host and its client is responsible for URL bracket formatting.
        return len(host) <= 64
    return _HOST_RE.fullmatch(host) is not None and ".." not in host


def _validated_websocket_headers(headers: Any) -> tuple[str, str]:
    upgrade = headers.get("Upgrade", "").lower()
    connections = {
        item.strip().lower()
        for item in headers.get("Connection", "").split(",")
    }
    if upgrade != "websocket" or "upgrade" not in connections:
        raise ValueError("A WebSocket upgrade is required.")
    if headers.get("Sec-WebSocket-Version") != "13":
        raise ValueError("WebSocket version 13 is required.")
    key = headers.get("Sec-WebSocket-Key", "")
    try:
        decoded = base64.b64decode(key, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("WebSocket key is invalid.") from error
    if len(decoded) != 16:
        raise ValueError("WebSocket key is invalid.")
    protocols = [
        item.strip()
        for item in headers.get("Sec-WebSocket-Protocol", "").split(",")
        if item.strip()
    ]
    if "mqtt" not in protocols:
        raise ValueError("The mqtt WebSocket subprotocol is required.")
    return key, "mqtt"


def _asset_type(path: Path) -> tuple[str, str | None]:
    name = path.name
    if name.endswith(".wasm.unityweb"):
        return "application/wasm", "br"
    if name.endswith(".framework.js.unityweb"):
        return "text/javascript; charset=utf-8", "br"
    if name.endswith(".data.unityweb"):
        return "application/octet-stream", "br"
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if media_type.startswith("text/") or media_type in {
        "application/javascript",
        "application/json",
    }:
        media_type += "; charset=utf-8"
    return media_type, None


if __name__ == "__main__":
    raise SystemExit(main())
