#!/usr/bin/env python3
"""Render the fail-closed TLS gateway for a shared Studio deployment."""

from __future__ import annotations

import ipaddress
import os
import sys


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name}.")
    return value


def _integer(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer.") from error
    if value < 1 or value > 65535:
        raise SystemExit(f"{name} must be a TCP port or positive range size.")
    return value


def _nginx_value(value: str) -> str:
    if any(
        character.isspace() or character in "{};$\0\"'\\#"
        for character in value
    ):
        raise SystemExit("An nginx configuration value contains unsafe characters.")
    return value


def _allowlist() -> str:
    values = _required("ALLOWED_CIDRS").split()
    for value in values:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as error:
            raise SystemExit(f"Invalid ALLOWED_CIDRS entry: {value}") from error
    return "\n".join(f"        allow {value};" for value in values) + "\n        deny all;"


def _tls() -> str:
    certificate = _nginx_value(_required("TLS_CERTIFICATE"))
    key = _nginx_value(_required("TLS_CERTIFICATE_KEY"))
    return f"""    ssl_certificate {certificate};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:OptPilotTLS:10m;
    ssl_session_timeout 1h;
    add_header Strict-Transport-Security \"max-age=31536000\" always;
    add_header X-Content-Type-Options \"nosniff\" always;
    add_header Referrer-Policy \"same-origin\" always;"""


def _auth_location(kind: str, studio_port: int) -> str:
    return f"""    location = /__optpilot_auth {{
        internal;
        proxy_pass http://127.0.0.1:{studio_port}/api/auth/verify;
        proxy_pass_request_body off;
        proxy_set_header Content-Length \"\";
        proxy_set_header Cookie $http_cookie;
        proxy_set_header Authorization \"\";
        proxy_set_header X-OptPilot-Target-Kind {kind};
        proxy_set_header X-OptPilot-Target-Port $server_port;
    }}"""


def _proxy_headers(*, strip_cookie: bool) -> str:
    cookie = '        proxy_set_header Cookie "";\n        proxy_hide_header Set-Cookie;\n' if strip_cookie else ""
    return f"""        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Authorization \"\";
{cookie}        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
        proxy_buffering off;"""


def render() -> str:
    bind_ip = _nginx_value(_required("PUBLIC_BIND_IP"))
    try:
        bind_address = ipaddress.ip_address(bind_ip)
    except ValueError as error:
        raise SystemExit("PUBLIC_BIND_IP must be one specific IP address.") from error
    if bind_address.is_unspecified:
        raise SystemExit("PUBLIC_BIND_IP must not be a wildcard address.")
    try:
        backend_addresses = {
            ipaddress.ip_address(os.environ.get("STUDIO_HOST", "127.0.0.1")),
            ipaddress.ip_address(
                os.environ.get("WORKSPACE_RUNTIME_HOST", "127.0.0.1")
            ),
        }
    except ValueError as error:
        raise SystemExit("Backend listen hosts must be IP addresses.") from error
    if bind_address in backend_addresses:
        raise SystemExit(
            "PUBLIC_BIND_IP must differ from loopback backend addresses so dynamic ports cannot collide."
        )
    public_host = _nginx_value(_required("PUBLIC_HOST"))
    server_name = _nginx_value(os.environ.get("PUBLIC_SERVER_NAME", public_host))
    studio_port = _integer("STUDIO_PORT", 28666)
    code_start = _integer("WORKSPACE_RUNTIME_PORT_START", 28766)
    count = _integer("WORKSPACE_RUNTIME_PORT_COUNT", 110)
    preview_offset = _integer("PREVIEW_PORT_OFFSET", 1000)
    if code_start + count - 1 > 65535 or code_start + preview_offset + count - 1 > 65535:
        raise SystemExit("Workspace or Preview port range exceeds 65535.")
    code_ports = set(range(code_start, code_start + count))
    preview_ports = set(
        range(code_start + preview_offset, code_start + preview_offset + count)
    )
    if studio_port in code_ports | preview_ports or code_ports & preview_ports:
        raise SystemExit("Studio, Workspace, and Preview port ranges must not overlap.")
    allowed = _allowlist()
    common_headers = _proxy_headers(strip_cookie=False)

    blocks: list[str] = []
    blocks.append(
        f"""server {{
    listen {bind_ip}:{studio_port} ssl;
    server_name {server_name};
{_tls()}
{_auth_location('studio', studio_port)}
    location = /login {{
{allowed}
{common_headers}
        proxy_pass http://127.0.0.1:{studio_port};
    }}
    location = /api/auth/login {{
{allowed}
{common_headers}
        proxy_pass http://127.0.0.1:{studio_port};
    }}
    location = /api/auth/session {{
{allowed}
{common_headers}
        proxy_pass http://127.0.0.1:{studio_port};
    }}
    location /api/ {{
{allowed}
        auth_request /__optpilot_auth;
{common_headers}
        proxy_pass http://127.0.0.1:{studio_port};
    }}
    location / {{
{allowed}
        auth_request /__optpilot_auth;
        error_page 401 = @login_required;
{common_headers}
        proxy_pass http://127.0.0.1:{studio_port};
    }}
    location @login_required {{
        return 303 https://{public_host}:{studio_port}/login;
    }}
}}"""
    )

    for kind, ports, strip_cookie in (
        ("code", sorted(code_ports), True),
        ("presentation", sorted(preview_ports), False),
    ):
        headers = _proxy_headers(strip_cookie=strip_cookie)
        for port in ports:
            blocks.append(
                f"""server {{
    listen {bind_ip}:{port} ssl;
    server_name {server_name};
{_tls()}
{_auth_location(kind, studio_port)}
    location / {{
{allowed}
        auth_request /__optpilot_auth;
{headers}
        proxy_pass http://127.0.0.1:$server_port;
    }}
}}"""
            )
    return "\n\n".join(blocks) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render())
