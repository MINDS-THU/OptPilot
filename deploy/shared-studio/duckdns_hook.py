#!/usr/bin/env python3
"""Bounded Certbot manual hook for one DuckDNS hostname."""

from __future__ import annotations

import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_URL = "https://www.duckdns.org/update"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def read_token() -> str:
    path = Path(required("DUCKDNS_TOKEN_FILE"))
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("DuckDNS token path is not a regular file.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("DuckDNS token file must not be accessible by group or others.")
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("DuckDNS token file is empty or malformed.")
    return token


def update(**values: str) -> None:
    parameters = {
        "domains": required("DUCKDNS_SUBDOMAIN"),
        "token": read_token(),
        **values,
    }
    request = urllib.request.Request(
        f"{API_URL}?{urllib.parse.urlencode(parameters)}",
        headers={"User-Agent": "OptPilot-DuckDNS-ACME/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = response.read(64).decode("ascii", errors="replace").strip()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"DuckDNS returned HTTP {error.code}.") from None
    except urllib.error.URLError as error:
        raise SystemExit(f"DuckDNS request failed: {error.reason}") from None
    if result != "OK":
        raise SystemExit("DuckDNS rejected the update.")


def validate_certbot_domain() -> None:
    expected = f"{required('DUCKDNS_SUBDOMAIN')}.duckdns.org"
    actual = required("CERTBOT_DOMAIN").rstrip(".").lower()
    if actual != expected:
        raise SystemExit(f"Refusing DuckDNS update for unexpected domain: {actual}")


def authenticate() -> None:
    validate_certbot_domain()
    update(txt=required("CERTBOT_VALIDATION"))
    delay = int(os.environ.get("DUCKDNS_PROPAGATION_SECONDS", "60"))
    if not 0 <= delay <= 600:
        raise SystemExit("DUCKDNS_PROPAGATION_SECONDS must be between 0 and 600.")
    print(f"DuckDNS TXT record updated; waiting {delay}s for DNS propagation.")
    time.sleep(delay)


def cleanup() -> None:
    validate_certbot_domain()
    update(txt="", clear="true")
    print("DuckDNS TXT challenge record cleared.")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action == "auth":
        authenticate()
    elif action == "cleanup":
        cleanup()
    else:
        raise SystemExit("Usage: duckdns_hook.py auth|cleanup")


if __name__ == "__main__":
    main()
