"""Shared helpers for Docker/Podman-compatible container runtimes."""

from __future__ import annotations

from typing import List


def network_args(policy: str) -> List[str]:
    normalized = str(policy or "disabled").lower()
    if normalized in {"disabled", "none", "off"}:
        return ["--network", "none"]
    if normalized == "host":
        return ["--network", "host"]
    if normalized in {"enabled", "default", "bridge"}:
        return []
    raise ValueError(f"Unsupported container network policy: {policy!r}")
