"""Trusted stdlib ingress used by the local container web provider.

The provider mounts this file and a provider-private control directory into a
dedicated sidecar.  The sidecar authenticates the first HTTP request on every
TCP connection, removes the launch-scoped credential, and then becomes a
transparent byte tunnel.  The tunnel therefore preserves HTTP upgrades (and
WebSocket frames) without teaching OptPilot an application-specific protocol.

This module intentionally has no dependency on the rest of OptPilot so it can
run with ``python -I -S`` in an approved Python-capable interface image.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import signal
from pathlib import Path
from typing import Final


_CONTROL_ROOT: Final = Path("/run/optpilot-gateway")
_ROUTES_PATH: Final = _CONTROL_ROOT / "routes.json"
_TOKEN_PATH: Final = _CONTROL_ROOT / "token"
_AUTH_HEADER: Final = b"x-optpilot-presentation-ingress"
_MAX_HEADER_BYTES: Final = 65_536
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_HOST_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")


class _Unauthorized(Exception):
    pass


def _authorized_header_block(header_block: bytes, token: str) -> bytes:
    """Validate and remove the private ingress header from one HTTP request."""

    if len(header_block) > _MAX_HEADER_BYTES or not header_block.endswith(b"\r\n\r\n"):
        raise _Unauthorized
    lines = header_block[:-4].split(b"\r\n")
    if not lines or b" " not in lines[0] or b"\x00" in lines[0]:
        raise _Unauthorized
    kept = [lines[0]]
    supplied: list[bytes] = []
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise _Unauthorized
        name, value = line.split(b":", 1)
        if name.strip().lower() == _AUTH_HEADER:
            supplied.append(value.strip())
        else:
            kept.append(line)
    expected = token.encode("ascii")
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0], expected):
        raise _Unauthorized
    return b"\r\n".join(kept) + b"\r\n\r\n"


async def _copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65_536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (AttributeError, ConnectionError, OSError):
            pass


async def _reject(writer: asyncio.StreamWriter, status: bytes) -> None:
    writer.write(
        b"HTTP/1.1 "
        + status
        + b"\r\nConnection: close\r\nContent-Length: 0\r\n"
        + b"X-OptPilot-Gateway: 1\r\n\r\n"
    )
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    target_host: str,
    target_port: int,
    token: str,
) -> None:
    try:
        header = await asyncio.wait_for(
            client_reader.readuntil(b"\r\n\r\n"), timeout=10.0
        )
        forwarded_header = _authorized_header_block(header, token)
    except _Unauthorized:
        await _reject(client_writer, b"401 Unauthorized")
        return
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        await _reject(client_writer, b"431 Request Header Fields Too Large")
        return

    try:
        target_reader, target_writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=5.0
        )
    except (ConnectionError, OSError, TimeoutError):
        await _reject(client_writer, b"502 Bad Gateway")
        return

    try:
        target_writer.write(forwarded_header)
        await target_writer.drain()
        client_to_target = asyncio.create_task(
            _copy_stream(client_reader, target_writer)
        )
        target_to_client = asyncio.create_task(
            _copy_stream(target_reader, client_writer)
        )
        # Preserve TCP half-close semantics.  In particular, a client may
        # finish sending its request before the application has produced the
        # HTTP response or completed a WebSocket close handshake.
        await asyncio.gather(
            client_to_target, target_to_client, return_exceptions=True
        )
    finally:
        target_writer.close()
        client_writer.close()
        await asyncio.gather(
            target_writer.wait_closed(),
            client_writer.wait_closed(),
            return_exceptions=True,
        )


def _load_control() -> tuple[str, tuple[int, ...], str]:
    token = _TOKEN_PATH.read_text(encoding="ascii")
    if _TOKEN_RE.fullmatch(token) is None:
        raise RuntimeError("invalid gateway credential")
    payload = json.loads(_ROUTES_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "optpilot.gateway.v1":
        raise RuntimeError("invalid gateway control schema")
    target_host = payload.get("target_host")
    ports = payload.get("ports")
    if not isinstance(target_host, str) or _HOST_RE.fullmatch(target_host) is None:
        raise RuntimeError("invalid gateway target")
    if (
        not isinstance(ports, list)
        or not ports
        or len(ports) > 16
        or any(isinstance(port, bool) or not isinstance(port, int) for port in ports)
        or any(port < 1 or port > 65_535 for port in ports)
        or len(set(ports)) != len(ports)
    ):
        raise RuntimeError("invalid gateway ports")
    return target_host, tuple(sorted(ports)), token


async def _main() -> None:
    target_host, ports, token = _load_control()
    servers = []
    for port in ports:
        servers.append(
            await asyncio.start_server(
                lambda reader, writer, target_port=port: _handle(
                    reader,
                    writer,
                    target_host=target_host,
                    target_port=target_port,
                    token=token,
                ),
                host="0.0.0.0",
                port=port,
                limit=_MAX_HEADER_BYTES,
            )
        )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    for server in servers:
        server.close()
    await asyncio.gather(*(server.wait_closed() for server in servers))


if __name__ == "__main__":
    asyncio.run(_main())
