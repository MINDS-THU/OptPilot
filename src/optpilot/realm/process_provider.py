"""Path-free identity for the current Python process runtime provider.

The full builder input is intentionally reduced to a digest.  Runtime paths,
environment values, executable locations, and distribution locations are not
accepted by the pure builder and cannot enter the retained identity.
"""

from __future__ import annotations

import hashlib
import os
import platform as platform_module
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import Any

from ._validation import lower_hex_digest, required_text
from .refs import canonical_json_bytes


PROCESS_PROVIDER_BUILDER_SCHEMA = "optpilot.process-provider-builder.v1"
MAX_PROCESS_PROVIDER_DISTRIBUTIONS = 10_000
MAX_PROCESS_PROVIDER_INPUT_BYTES = 1024 * 1024
MAX_DISTRIBUTION_NAME_BYTES = 128
MAX_DISTRIBUTION_VERSION_BYTES = 256

_FACT_LIMITS = {
    "os_name": 32,
    "platform_machine": 128,
    "platform_release": 256,
    "platform_system": 64,
    "python_cache_tag": 128,
    "python_implementation": 64,
    "python_version": 64,
    "sys_platform": 64,
}
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!~-]*$")
_DISTRIBUTION_NAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
_DISTRIBUTION_SEPARATOR_RE = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class ProcessProviderIdentity:
    """Retainable compatibility identity for one process runtime provider."""

    builder_fingerprint: str
    platform: str

    def __post_init__(self) -> None:
        lower_hex_digest(
            self.builder_fingerprint, "process provider builder fingerprint"
        )
        _safe_token(self.platform, "process provider platform", max_bytes=256)

    def to_dict(self) -> dict[str, str]:
        return {
            "builder_fingerprint": self.builder_fingerprint,
            "platform": self.platform,
        }


def build_process_provider_identity(
    runtime_facts: Mapping[str, Any],
    distributions: Iterable[Sequence[str]],
) -> ProcessProviderIdentity:
    """Build an identity from injected, path-free facts and a package lock.

    ``runtime_facts`` must contain exactly the documented fixed keys.  Each
    distribution is a two-item ``(name, version)`` sequence.  Names are
    normalized like Python distribution names and duplicates after
    normalization are rejected.
    """

    facts = _validated_runtime_facts(runtime_facts)
    lock = _validated_distribution_lock(distributions)
    payload = {
        "distributions": [
            {"name": name, "version": version} for name, version in lock
        ],
        "runtime": facts,
        "schema": PROCESS_PROVIDER_BUILDER_SCHEMA,
    }
    encoded = canonical_json_bytes(payload)
    if len(encoded) > MAX_PROCESS_PROVIDER_INPUT_BYTES:
        raise ValueError(
            "process provider builder input exceeds the maximum encoded size."
        )
    fingerprint = hashlib.sha256(
        b"optpilot/process-provider-builder/v1\0" + encoded
    ).hexdigest()
    platform = f"{facts['sys_platform'].lower()}-{facts['platform_machine'].lower()}"
    return ProcessProviderIdentity(fingerprint, platform)


def current_process_provider_identity() -> ProcessProviderIdentity:
    """Describe the active Python process provider without retaining host paths."""

    runtime_facts = {
        "os_name": os.name,
        "platform_machine": platform_module.machine(),
        "platform_release": platform_module.release(),
        "platform_system": platform_module.system(),
        "python_cache_tag": sys.implementation.cache_tag,
        "python_implementation": sys.implementation.name,
        "python_version": platform_module.python_version(),
        "sys_platform": sys.platform,
    }

    # ``distributions()`` may expose stale or shadowed metadata records.  The
    # active importlib view is one version per normalized name; resolve that
    # view before passing the lock to the duplicate-strict pure builder.
    names: set[str] = set()
    for distribution in importlib_metadata.distributions():
        name = distribution.metadata.get("Name")
        if name is None:
            raise ValueError("installed distribution is missing its Name metadata.")
        names.add(_canonical_distribution_name(name))
    lock = tuple(
        (name, importlib_metadata.version(name))
        for name in sorted(names, key=lambda value: value.encode("ascii"))
    )
    return build_process_provider_identity(runtime_facts, lock)


def _validated_runtime_facts(runtime_facts: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(runtime_facts, Mapping):
        raise TypeError("process provider runtime_facts must be a mapping.")
    actual = set(runtime_facts)
    expected = set(_FACT_LIMITS)
    if actual != expected:
        raise ValueError(
            "process provider runtime fact fields differ; "
            f"missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}."
        )
    return {
        key: _safe_token(
            runtime_facts[key],
            f"process provider runtime fact {key}",
            max_bytes=_FACT_LIMITS[key],
        )
        for key in sorted(expected)
    }


def _validated_distribution_lock(
    distributions: Iterable[Sequence[str]],
) -> tuple[tuple[str, str], ...]:
    if isinstance(distributions, (str, bytes, Mapping)) or not isinstance(
        distributions, Iterable
    ):
        raise TypeError("process provider distributions must be an iterable of pairs.")
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in distributions:
        if (
            isinstance(item, (str, bytes))
            or not isinstance(item, Sequence)
            or len(item) != 2
        ):
            raise TypeError(
                "each process provider distribution must be a name/version pair."
            )
        name = _canonical_distribution_name(item[0])
        version = _safe_token(
            item[1],
            f"distribution {name} version",
            max_bytes=MAX_DISTRIBUTION_VERSION_BYTES,
        )
        if name in seen:
            raise ValueError(
                f"process provider distributions contain duplicate name {name!r}."
            )
        seen.add(name)
        values.append((name, version))
        if len(values) > MAX_PROCESS_PROVIDER_DISTRIBUTIONS:
            raise ValueError("process provider distributions exceed the maximum count.")
    return tuple(sorted(values, key=lambda item: item[0].encode("ascii")))


def _canonical_distribution_name(value: Any) -> str:
    name = required_text(
        value,
        "process provider distribution name",
        max_bytes=MAX_DISTRIBUTION_NAME_BYTES,
    )
    if _DISTRIBUTION_NAME_RE.fullmatch(name) is None:
        raise ValueError("process provider distribution name is invalid.")
    return _DISTRIBUTION_SEPARATOR_RE.sub("-", name).lower()


def _safe_token(value: Any, label: str, *, max_bytes: int) -> str:
    token = required_text(value, label, max_bytes=max_bytes)
    if _SAFE_TOKEN_RE.fullmatch(token) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return token


__all__ = [
    "MAX_PROCESS_PROVIDER_DISTRIBUTIONS",
    "MAX_PROCESS_PROVIDER_INPUT_BYTES",
    "PROCESS_PROVIDER_BUILDER_SCHEMA",
    "ProcessProviderIdentity",
    "build_process_provider_identity",
    "current_process_provider_identity",
]
