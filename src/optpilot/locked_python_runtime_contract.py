"""Dependency-free contract for exact retained Python dependency layers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .realm._validation import lower_hex_digest, required_text
from .realm.manifests import validate_portable_path
from .realm.owner_derivation import SourceAnchor
from .realm.owners import OwnerMembership
from .realm.refs import SnapshotRef
from .runtime_scopes import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    METHOD_PREPARED_PYTHON_SCOPE,
)


LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA = "optpilot.locked-python-runtime-policy.v1"
LOCKED_PYTHON_RUNTIME_LAYOUT = "retained-pure-python-wheel-layer-v1"
LOCKED_PYTHON_RUNTIME_OWNER_KIND = "locked-python-runtime"
LOCKED_PYTHON_RUNTIME_SOURCE_ROLE = "locked-python-runtime-payload"
MAX_WHEELS = 256
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
MAX_PREPARATION_SECONDS = 600


class LockedPythonRuntimeError(ValueError):
    """Stable pre-Run rejection for unsupported or unsafe dependencies."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = required_text(code, "locked runtime error code", max_bytes=128)


def validate_locked_python_setup_declaration(setup: Any) -> Mapping[str, Any]:
    """Return the canonical declaration supported by retained Studies.

    This check is filesystem-free. The preparer subsequently validates paths,
    lock digests, wheel formats, and retained bytes against the sealed package.
    """

    if not isinstance(setup, Mapping):
        raise LockedPythonRuntimeError(
            "dependency_setup_invalid", "runtime.setup must be an object."
        )
    allowed_setup = {"cache", "steps", "timeoutSeconds"}
    if set(setup) - allowed_setup or setup.get("cache") != "prepared":
        raise LockedPythonRuntimeError(
            "dependency_setup_unsupported",
            "Study dependencies require runtime.setup.cache: prepared and no setup environment values.",
        )
    raw_steps = setup.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) != 1:
        raise LockedPythonRuntimeError(
            "dependency_setup_unsupported",
            "Study dependencies require exactly one locked python-venv setup step.",
        )
    step = raw_steps[0]
    allowed_step = {
        "uses",
        "cwd",
        "venv",
        "requirements",
        "installProject",
        "python",
        "env",
    }
    if (
        not isinstance(step, Mapping)
        or set(step) - allowed_step
        or step.get("uses") != "python-venv"
        or step.get("installProject") not in (None, False)
        or step.get("python") not in (None, "")
        or step.get("venv") not in (None, "")
        or step.get("env") not in (None, {})
    ):
        raise LockedPythonRuntimeError(
            "dependency_setup_unsupported",
            "Study preparation accepts only vendored hash-locked pure-Python wheels; arbitrary setup and editable installs are unsupported.",
        )
    requirements = step.get("requirements")
    if not isinstance(requirements, list) or not requirements or any(
        not isinstance(value, str) or not value for value in requirements
    ):
        raise LockedPythonRuntimeError(
            "dependency_lock_missing",
            "The python-venv step must name at least one package-relative wheel lock file.",
        )
    raw_timeout = setup.get("timeoutSeconds", 300)
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError) as error:
        raise LockedPythonRuntimeError(
            "dependency_timeout_invalid", "Dependency preparation timeout is invalid."
        ) from error
    if isinstance(raw_timeout, bool) or not 1 <= timeout_seconds <= MAX_PREPARATION_SECONDS:
        raise LockedPythonRuntimeError(
            "dependency_timeout_unsupported",
            f"Dependency preparation timeout must be at most {MAX_PREPARATION_SECONDS} seconds.",
        )
    cwd = step.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd:
        raise LockedPythonRuntimeError(
            "dependency_path_invalid",
            "Dependency setup cwd must be a package-relative path.",
        )
    return {
        "cache": "prepared",
        "timeoutSeconds": timeout_seconds,
        "steps": [
            {
                "uses": "python-venv",
                "cwd": cwd,
                "requirements": list(requirements),
            }
        ],
    }


def parse_locked_python_wheel_lock(
    payload: bytes,
) -> tuple[tuple[str, str], ...]:
    """Parse the one canonical offline wheel-lock syntax used by preparation."""

    if not isinstance(payload, bytes):
        raise TypeError("locked Python wheel lock payload must be bytes.")
    if len(payload) > MAX_LOCK_BYTES:
        raise LockedPythonRuntimeError(
            "dependency_lock_oversized",
            "Dependency lock files exceed the supported size.",
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LockedPythonRuntimeError(
            "dependency_lock_invalid",
            "Dependency lock must be bounded UTF-8 text.",
        ) from error
    result: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line or line.endswith("\\"):
            raise LockedPythonRuntimeError(
                "dependency_lock_unsupported",
                f"Dependency lock line {line_number} uses unsupported syntax.",
            )
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as error:
            raise LockedPythonRuntimeError(
                "dependency_lock_invalid",
                f"Dependency lock line {line_number} is invalid.",
            ) from error
        if len(tokens) != 2 or not tokens[1].startswith("--hash=sha256:"):
            raise LockedPythonRuntimeError(
                "dependency_lock_unsupported",
                f"Dependency lock line {line_number} must contain one vendored "
                "wheel and one sha256 hash.",
            )
        digest = tokens[1].removeprefix("--hash=sha256:")
        try:
            lower_hex_digest(digest, "dependency wheel hash")
        except (TypeError, ValueError) as error:
            raise LockedPythonRuntimeError(
                "dependency_lock_invalid",
                f"Dependency lock line {line_number} has an invalid hash.",
            ) from error
        result.append((tokens[0], digest))
    if not result:
        raise LockedPythonRuntimeError(
            "dependency_lock_empty",
            "Dependency lock contains no vendored wheels.",
        )
    return tuple(result)


@dataclass(frozen=True)
class LockedWheel:
    package_relative_path: str
    sha256: str
    size_bytes: int
    path: Path

    def __post_init__(self) -> None:
        validate_portable_path(self.package_relative_path)
        lower_hex_digest(self.sha256, "locked wheel digest")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 1
            or self.size_bytes > MAX_WHEEL_BYTES
        ):
            raise ValueError("locked wheel size is outside the supported bounds.")
        if not isinstance(self.path, Path) or not self.path.is_file():
            raise ValueError("locked wheel file is unavailable.")

    def identity(self) -> dict[str, Any]:
        return {
            "path": self.package_relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class LockedPythonRuntimePlan:
    component_kind: str
    component_id: str
    config_relative_path: str
    setup: Mapping[str, Any]
    lock_relative_paths: tuple[str, ...]
    wheels: tuple[LockedWheel, ...]
    timeout_seconds: int
    key_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.component_kind not in {"environment", "method"}:
            raise ValueError("locked runtime component kind is unsupported.")
        required_text(self.component_id, "locked runtime component id", max_bytes=512)
        validate_portable_path(self.config_relative_path)
        if not self.wheels or len(self.wheels) > MAX_WHEELS:
            raise ValueError("locked runtime wheel count is outside the supported bounds.")
        if not 1 <= self.timeout_seconds <= MAX_PREPARATION_SECONDS:
            raise ValueError("locked runtime timeout is outside the supported bounds.")


@dataclass(frozen=True)
class PreparedPythonRuntime:
    component_kind: str
    cache_key: str
    source_anchor: SourceAnchor
    membership: OwnerMembership
    scope: str
    import_roots: tuple[str, ...] = (".",)

    def __post_init__(self) -> None:
        if self.component_kind not in {"environment", "method"}:
            raise ValueError("prepared runtime component kind is unsupported.")
        lower_hex_digest(self.cache_key, "prepared runtime cache key")
        if not isinstance(self.source_anchor, SourceAnchor):
            raise TypeError("prepared runtime source_anchor is invalid.")
        if not isinstance(self.membership, OwnerMembership):
            raise TypeError("prepared runtime membership is invalid.")
        if (
            self.membership.role != LOCKED_PYTHON_RUNTIME_SOURCE_ROLE
            or not isinstance(self.membership.content_ref, SnapshotRef)
        ):
            raise ValueError("prepared runtime membership is invalid.")
        expected_scope = (
            ENVIRONMENT_PREPARED_PYTHON_SCOPE
            if self.component_kind == "environment"
            else METHOD_PREPARED_PYTHON_SCOPE
        )
        if self.scope != expected_scope or self.import_roots != (".",):
            raise ValueError("prepared runtime logical scope is invalid.")


__all__ = [
    "ENVIRONMENT_PREPARED_PYTHON_SCOPE",
    "LOCKED_PYTHON_RUNTIME_LAYOUT",
    "LOCKED_PYTHON_RUNTIME_OWNER_KIND",
    "LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA",
    "LOCKED_PYTHON_RUNTIME_SOURCE_ROLE",
    "LockedPythonRuntimeError",
    "LockedPythonRuntimePlan",
    "LockedWheel",
    "MAX_LOCK_BYTES",
    "MAX_PREPARATION_SECONDS",
    "MAX_WHEELS",
    "MAX_WHEEL_BYTES",
    "METHOD_PREPARED_PYTHON_SCOPE",
    "PreparedPythonRuntime",
    "parse_locked_python_wheel_lock",
    "validate_locked_python_setup_declaration",
]
