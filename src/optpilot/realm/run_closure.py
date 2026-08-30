"""Portable immutable environment/runtime closure for one run.

These records are the semantic inputs from which canonical attempts and
Studio Debug Runs can later compile the same :class:`EvaluationSpec`.  They
contain only immutable content references, logical scope names, portable
relative paths, and canonical JSON.  Store placement and provider-specific
paths remain availability/runtime facts outside this module.

Mappings such as evaluator settings are intentionally treated as opaque JSON.
Path-like strings inside them are neither rejected nor interpreted as content
references.  Every path or content dependency that OptPilot owns must instead
appear in a typed :class:`ScopePath` or :class:`ScopeLayer` field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from ._validation import (
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    optional_text,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .manifests import validate_portable_path
from .owners import OwnerMembership
from .refs import PhysicalContentRef, SnapshotRef, canonical_json_bytes, request_digest
from ..host_env import HostEnvDeclaration, compile_host_env_declarations
from ..image_reference import IMAGE_DIGEST_RE


JsonDict = Dict[str, Any]

ENVIRONMENT_REVISION_SCHEMA = "optpilot.environment-revision.v1"
PREPARED_ENVIRONMENT_RUNTIME_SCHEMA = "optpilot.prepared-environment-runtime.v1"
RUN_EVALUATION_TEMPLATE_SCHEMA = "optpilot.run-evaluation-template.v1"
RUN_EVALUATION_CLOSURE_SCHEMA = "optpilot.run-evaluation-closure.v1"

RUN_ENVIRONMENT_SOURCE_ROLE = "run-environment-source"
RUN_ATTEMPT_INPUT_ROLE = "run-attempt-input"
RUN_PREPARED_RUNTIME_ROLE = "run-prepared-runtime"

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[ -~]*)?$"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_OCI_DIGEST_RE = IMAGE_DIGEST_RE
_RUNTIME_KINDS = frozenset({"process", "container"})
_PORTABILITY_VALUES = frozenset({"portable", "provider-scoped"})
_INTERFACE_NETWORK_GRANTS = frozenset({"disabled", "enabled"})
_INTERFACE_SANDBOXES = frozenset({"container", "process"})
_INTERFACE_SELECTION_KINDS = frozenset(
    {"artifact", "candidate", "trial", "workspace"}
)
_INTERFACE_SETUP_KINDS = frozenset({"command", "npm", "python-venv", "uv"})
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_INTERFACE_COMMAND_TOKENS = 256
_MAX_INTERFACE_ENV_ENTRIES = 256
_MAX_INTERFACE_PORTS = 32
_MAX_INTERFACE_OUTPUT_ACTIONS = 16


def _scope_name(value: Any, label: str = "scope") -> str:
    value = required_text(value, label, max_bytes=64)
    if _SCOPE_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must start with a lowercase letter and contain only "
            "lowercase letters, digits, or hyphens."
        )
    return value


def _portable_root_or_path(value: Any, label: str) -> str:
    if value == ".":
        return "."
    try:
        return validate_portable_path(value)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(f"{label} must be '.' or a portable relative path.") from error


def _freeze_mapping(value: Any, label: str, *, nonempty: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty.")
    frozen = freeze_json(value, label=label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError(f"{label} must be a mapping.")
    canonical_json_bytes(thaw_json(frozen))
    return frozen


def _canonical_layers(values: Sequence["ScopeLayer"], label: str) -> Tuple["ScopeLayer", ...]:
    layers = tuple(values)
    if any(not isinstance(item, ScopeLayer) for item in layers):
        raise TypeError(f"{label} must contain ScopeLayer values.")
    if len(set(layers)) != len(layers):
        raise ValueError(f"{label} must not contain duplicate layers.")
    targets = set()
    for layer in layers:
        target = (layer.scope, layer.destination_subpath, layer.precedence)
        if target in targets:
            raise ValueError(
                f"{label} assigns the same scope, destination, and precedence more than once."
            )
        targets.add(target)
    return tuple(sorted(layers, key=_layer_sort_key))


def _layer_sort_key(layer: "ScopeLayer") -> tuple[object, ...]:
    return (
        layer.scope.encode("utf-8"),
        layer.destination_subpath.encode("utf-8"),
        layer.precedence,
        layer.source_subpath.encode("utf-8"),
        str(layer.snapshot_ref),
    )


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _bounded_record(payload: Mapping[str, Any], label: str) -> None:
    if len(canonical_json_bytes(payload)) > _MAX_RECORD_BYTES:
        raise ValueError(f"{label} exceeds the maximum encoded size.")


def _bounded_text(
    value: Any,
    label: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{label} must be a trimmed string.")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8 text.") from error
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _environment_map(value: Any, label: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if len(value) > _MAX_INTERFACE_ENV_ENTRIES:
        raise ValueError(f"{label} contains too many entries.")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = required_text(raw_name, f"{label} name", max_bytes=128)
        if _ENV_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"{label} name {name!r} is invalid.")
        text = _bounded_text(
            raw_value,
            f"{label} value for {name}",
            max_bytes=8192,
            allow_empty=True,
        )
        if text.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(text):
            raise ValueError(
                f"{label} value for {name} must not contain an absolute host path."
            )
        normalized[name] = text
    return MappingProxyType(dict(sorted(normalized.items())))


def _portable_command(values: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise TypeError(f"{label} must be a non-empty sequence.")
    if len(values) > _MAX_INTERFACE_COMMAND_TOKENS:
        raise ValueError(f"{label} contains too many tokens.")
    result: list[str] = []
    for index, value in enumerate(values):
        token = _bounded_text(
            value,
            f"{label}[{index}]",
            max_bytes=8192,
            allow_empty=False,
        )
        if token.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(token):
            raise ValueError(f"{label}[{index}] must not be an absolute host path.")
        if "\\" in token:
            raise ValueError(f"{label}[{index}] contains a non-portable separator.")
        if "/" in token and ".." in token.split("/"):
            raise ValueError(f"{label}[{index}] contains path traversal.")
        result.append(token)
    return tuple(result)


def _unique_texts(
    values: Any,
    label: str,
    *,
    max_items: int = 256,
    max_bytes: int = 512,
) -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{label} must be a sequence.")
    if len(values) > max_items:
        raise ValueError(f"{label} contains too many values.")
    result = tuple(
        required_text(item, f"{label} item", max_bytes=max_bytes) for item in values
    )
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates.")
    return tuple(sorted(result, key=lambda item: item.encode("utf-8")))


@dataclass(frozen=True, order=True)
class ScopePath:
    """One path relative to a named logical runtime scope."""

    scope: str
    relative_path: str = "."

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _scope_name(self.scope))
        object.__setattr__(
            self,
            "relative_path",
            _portable_root_or_path(self.relative_path, "scope relative_path"),
        )

    def to_dict(self) -> JsonDict:
        return {"path": self.relative_path, "scope": self.scope}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScopePath":
        try:
            _exact_keys(payload, {"path", "scope"}, "scope path")
            result = cls(scope=payload["scope"], relative_path=payload["path"])
            if result.to_dict() != dict(payload):
                raise ValueError("scope path is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted scope path is invalid: {error}") from error


@dataclass(frozen=True, order=True)
class ScopeLayer:
    """Map an immutable snapshot subtree into a named logical scope."""

    scope: str
    snapshot_ref: SnapshotRef
    source_subpath: str = "."
    destination_subpath: str = "."
    precedence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _scope_name(self.scope))
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("scope layer snapshot_ref must be a SnapshotRef.")
        object.__setattr__(
            self,
            "source_subpath",
            _portable_root_or_path(self.source_subpath, "scope layer source_subpath"),
        )
        object.__setattr__(
            self,
            "destination_subpath",
            _portable_root_or_path(
                self.destination_subpath, "scope layer destination_subpath"
            ),
        )
        nonnegative_int(self.precedence, "scope layer precedence")

    def to_dict(self) -> JsonDict:
        return {
            "destination_subpath": self.destination_subpath,
            "precedence": self.precedence,
            "scope": self.scope,
            "snapshot_ref": str(self.snapshot_ref),
            "source_subpath": self.source_subpath,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScopeLayer":
        try:
            _exact_keys(
                payload,
                {
                    "destination_subpath",
                    "precedence",
                    "scope",
                    "snapshot_ref",
                    "source_subpath",
                },
                "scope layer",
            )
            result = cls(
                scope=payload["scope"],
                snapshot_ref=SnapshotRef.parse(payload["snapshot_ref"]),
                source_subpath=payload["source_subpath"],
                destination_subpath=payload["destination_subpath"],
                precedence=payload["precedence"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("scope layer is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted scope layer is invalid: {error}") from error


@dataclass(frozen=True)
class InterfaceProcessInvocation:
    """One authored, provider-neutral process invocation for an interface."""

    command: Tuple[str, ...]
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "command",
            _portable_command(self.command, "interface command"),
        )
        object.__setattr__(
            self,
            "cwd",
            _portable_root_or_path(self.cwd, "interface cwd"),
        )
        object.__setattr__(
            self,
            "env",
            _environment_map(self.env, "interface environment"),
        )

    def to_dict(self) -> JsonDict:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "env": dict(self.env),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceProcessInvocation":
        try:
            _exact_keys(payload, {"command", "cwd", "env"}, "interface invocation")
            result = cls(
                command=tuple(payload["command"]),
                cwd=payload["cwd"],
                env=payload["env"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("interface invocation is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface invocation is invalid: {error}"
            ) from error


def _canonical_interface_setup_step(
    payload: Any,
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    uses = required_text(payload.get("uses"), f"{label}.uses", max_bytes=32)
    if uses not in _INTERFACE_SETUP_KINDS:
        raise ValueError(f"{label}.uses is unsupported.")
    common = {"uses", "cwd", "env"}
    variant_keys = {
        "uv": {"extras", "groups", "frozen"},
        "python-venv": {"installProject", "python", "requirements", "venv"},
        "npm": {"install"},
        "command": {"command"},
    }[uses]
    actual = set(payload)
    allowed = common | variant_keys
    if not actual <= allowed:
        raise ValueError(f"{label} has unsupported fields {sorted(actual - allowed)!r}.")
    if uses == "command" and "command" not in payload:
        raise ValueError(f"{label}.command is required.")

    result: JsonDict = {"uses": uses}
    if "cwd" in payload:
        result["cwd"] = _portable_root_or_path(payload["cwd"], f"{label}.cwd")
    if "env" in payload:
        env = _environment_map(payload["env"], f"{label}.env")
        if env:
            result["env"] = dict(env)

    for name in ("extras", "groups"):
        if name in payload:
            values = _unique_texts(payload[name], f"{label}.{name}")
            if values:
                result[name] = list(values)
    if "frozen" in payload:
        if not isinstance(payload["frozen"], bool):
            raise ValueError(f"{label}.frozen must be boolean.")
        if payload["frozen"]:
            result["frozen"] = True

    if uses == "python-venv":
        if "python" in payload:
            result["python"] = _portable_command(
                (payload["python"],), f"{label}.python"
            )[0]
        if "venv" in payload:
            result["venv"] = _portable_root_or_path(
                payload["venv"], f"{label}.venv"
            )
        if "requirements" in payload:
            requirements = tuple(
                _portable_root_or_path(item, f"{label}.requirements item")
                for item in _unique_texts(
                    payload["requirements"], f"{label}.requirements"
                )
            )
            if requirements:
                result["requirements"] = list(requirements)
        if "installProject" in payload:
            if not isinstance(payload["installProject"], bool):
                raise ValueError(f"{label}.installProject must be boolean.")
            if payload["installProject"]:
                result["installProject"] = True

    if uses == "npm" and "install" in payload:
        install = payload["install"]
        if install not in {"ci", "install"}:
            raise ValueError(f"{label}.install must be 'ci' or 'install'.")
        if install != "ci":
            result["install"] = install

    if uses == "command":
        result["command"] = list(
            _portable_command(payload["command"], f"{label}.command")
        )
    return MappingProxyType(result)


@dataclass(frozen=True)
class InterfaceSetupSpec:
    """Strict, path-free setup authored for one interface profile."""

    steps: Tuple[Mapping[str, Any], ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cache: Optional[str] = None
    timeout_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.steps, (list, tuple)) or not self.steps:
            raise ValueError("interface setup steps must be non-empty.")
        if len(self.steps) > 64:
            raise ValueError("interface setup contains too many steps.")
        steps = tuple(
            _canonical_interface_setup_step(
                item,
                label=f"interface setup step {index}",
            )
            for index, item in enumerate(self.steps)
        )
        timeout = self.timeout_seconds
        if timeout is not None:
            positive_int(timeout, "interface setup timeout_seconds")
            if timeout > 86_400:
                raise ValueError("interface setup timeout_seconds exceeds one day.")
        object.__setattr__(self, "steps", steps)
        object.__setattr__(
            self,
            "env",
            _environment_map(self.env, "interface setup environment"),
        )
        if self.cache not in {None, "prepared"}:
            raise ValueError("interface setup cache must be 'prepared' when set.")

    def to_dict(self) -> JsonDict:
        result: JsonDict = {
            "steps": [thaw_json(item) for item in self.steps],
        }
        if self.env:
            result["env"] = dict(self.env)
        if self.cache is not None:
            result["cache"] = self.cache
        if self.timeout_seconds is not None:
            result["timeoutSeconds"] = self.timeout_seconds
        return result

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceSetupSpec":
        if not isinstance(payload, Mapping):
            raise TypeError("interface setup must be a mapping.")
        actual = set(payload)
        allowed = {"cache", "env", "steps", "timeoutSeconds"}
        if not actual <= allowed:
            raise ValueError(
                "interface setup fields differ; "
                f"extra={sorted(actual - allowed)!r}."
            )
        if "steps" not in payload:
            raise ValueError("interface setup.steps is required.")
        return cls(
            steps=tuple(payload["steps"]),
            env=payload.get("env", {}),
            cache=payload.get("cache"),
            timeout_seconds=payload.get("timeoutSeconds"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceSetupSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface setup is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted interface setup is invalid: {error}") from error


@dataclass(frozen=True)
class InterfaceRuntimeSpec:
    """Restricted profile-specific runtime preparation fragment."""

    sandbox: Optional[str] = None
    setup: Optional[InterfaceSetupSpec] = None
    container: Optional["InterfaceContainerSpec"] = None

    def __post_init__(self) -> None:
        if self.sandbox is not None and self.sandbox not in _INTERFACE_SANDBOXES:
            raise ValueError("interface runtime sandbox must be 'process' or 'container'.")
        if self.setup is not None and not isinstance(self.setup, InterfaceSetupSpec):
            raise TypeError("interface runtime setup must be InterfaceSetupSpec or None.")
        if self.container is not None and not isinstance(
            self.container, InterfaceContainerSpec
        ):
            raise TypeError(
                "interface runtime container must be InterfaceContainerSpec or None."
            )
        if self.sandbox == "container" and self.container is None:
            raise ValueError("container interface runtime requires container settings.")
        if self.sandbox != "container" and self.container is not None:
            raise ValueError(
                "interface container settings require sandbox 'container'."
            )

    def to_dict(self) -> JsonDict:
        result: JsonDict = {}
        if self.sandbox is not None:
            result["sandbox"] = self.sandbox
        if self.setup is not None:
            result["setup"] = self.setup.to_dict()
        if self.container is not None:
            result["container"] = self.container.to_dict()
        return result

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceRuntimeSpec":
        if not isinstance(payload, Mapping):
            raise TypeError("interface runtime must be a mapping.")
        if not set(payload) <= {"container", "sandbox", "setup"}:
            raise ValueError(
                "interface runtime may contain only sandbox, setup, and container."
            )
        setup = payload.get("setup")
        container = payload.get("container")
        return cls(
            sandbox=payload.get("sandbox"),
            setup=(
                None
                if setup is None
                else InterfaceSetupSpec.from_authoring_dict(setup)
            ),
            container=(
                None
                if container is None
                else InterfaceContainerSpec.from_authoring_dict(container)
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceRuntimeSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface runtime is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface runtime is invalid: {error}"
            ) from error


def _interface_runtime_token(value: Any, label: str, *, max_bytes: int = 512) -> str:
    token = required_text(value, label, max_bytes=max_bytes)
    if token.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(token):
        raise ValueError(f"{label} must not be an absolute host path.")
    if "\\" in token or any(character.isspace() for character in token):
        raise ValueError(f"{label} is not a portable runtime token.")
    if ".." in token.split("/"):
        raise ValueError(f"{label} contains path traversal.")
    return token


@dataclass(frozen=True)
class InterfaceContainerBuildSpec:
    """Portable authored container build input for one interface profile."""

    tag: str
    context: str = "."
    dockerfile: str = "Dockerfile"
    target: Optional[str] = None
    args: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tag",
            _interface_runtime_token(self.tag, "interface container build tag"),
        )
        object.__setattr__(
            self,
            "context",
            _portable_root_or_path(
                self.context, "interface container build context"
            ),
        )
        object.__setattr__(
            self,
            "dockerfile",
            _portable_root_or_path(
                self.dockerfile, "interface container build dockerfile"
            ),
        )
        target = optional_text(
            self.target, "interface container build target", max_bytes=256
        )
        if target is not None:
            target = _interface_runtime_token(
                target, "interface container build target", max_bytes=256
            )
        object.__setattr__(self, "target", target)
        object.__setattr__(
            self,
            "args",
            _environment_map(self.args, "interface container build arguments"),
        )

    def to_dict(self) -> JsonDict:
        result: JsonDict = {
            "args": dict(self.args),
            "context": self.context,
            "dockerfile": self.dockerfile,
            "tag": self.tag,
        }
        if self.target is not None:
            result["target"] = self.target
        return result

    @classmethod
    def from_authoring_dict(
        cls, payload: Mapping[str, Any]
    ) -> "InterfaceContainerBuildSpec":
        if not isinstance(payload, Mapping):
            raise TypeError("interface container build must be a mapping.")
        actual = set(payload)
        allowed = {"args", "context", "dockerfile", "tag", "target"}
        if not actual <= allowed or "tag" not in payload:
            raise ValueError(
                "interface container build fields differ; "
                f"missing={sorted({'tag'} - actual)!r}, "
                f"extra={sorted(actual - allowed)!r}."
            )
        return cls(
            tag=payload["tag"],
            context=payload.get("context", "."),
            dockerfile=payload.get("dockerfile", "Dockerfile"),
            target=payload.get("target"),
            args=payload.get("args", {}),
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "InterfaceContainerBuildSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface container build is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface container build is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceContainerSpec:
    """Restricted container realization choice without launch authorities."""

    image: Optional[str] = None
    build: Optional[InterfaceContainerBuildSpec] = None
    platform: Optional[str] = None
    engine: str = "docker"

    def __post_init__(self) -> None:
        image = optional_text(self.image, "interface container image", max_bytes=1024)
        if image is not None:
            image = _interface_runtime_token(
                image, "interface container image", max_bytes=1024
            )
        if self.build is not None and not isinstance(
            self.build, InterfaceContainerBuildSpec
        ):
            raise TypeError(
                "interface container build must be InterfaceContainerBuildSpec or None."
            )
        if (image is None) == (self.build is None):
            raise ValueError(
                "interface container requires exactly one of image or build."
            )
        platform = optional_text(
            self.platform, "interface container platform", max_bytes=256
        )
        if platform is not None:
            platform = _interface_runtime_token(
                platform, "interface container platform", max_bytes=256
            )
        engine = _interface_runtime_token(
            self.engine, "interface container engine", max_bytes=64
        )
        if "/" in engine:
            raise ValueError(
                "interface container engine must name an engine, not an executable path."
            )
        object.__setattr__(self, "image", image)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "engine", engine)

    def to_dict(self) -> JsonDict:
        result: JsonDict = {"engine": self.engine}
        if self.image is not None:
            result["image"] = self.image
        if self.build is not None:
            result["build"] = self.build.to_dict()
        if self.platform is not None:
            result["platform"] = self.platform
        return result

    @classmethod
    def from_authoring_dict(
        cls, payload: Mapping[str, Any]
    ) -> "InterfaceContainerSpec":
        if not isinstance(payload, Mapping):
            raise TypeError("interface container must be a mapping.")
        actual = set(payload)
        allowed = {"build", "engine", "image", "platform"}
        if not actual <= allowed:
            raise ValueError(
                f"interface container has unsupported fields {sorted(actual - allowed)!r}."
            )
        build = payload.get("build")
        return cls(
            image=payload.get("image"),
            build=(
                None
                if build is None
                else InterfaceContainerBuildSpec.from_authoring_dict(build)
            ),
            platform=payload.get("platform"),
            engine=payload.get("engine", "docker"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceContainerSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface container is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface container is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceGrantSpec:
    """Launch-time authorities requested by an interface profile."""

    network: str = "disabled"
    env_from_host: Tuple[str, ...] = field(default_factory=tuple)
    secrets_from_host: Tuple[str, ...] = field(default_factory=tuple)
    # Public package fallbacks are retained with the grant so a preview rebuilt
    # from immutable Run evidence behaves like the authored interface. Resolved
    # host values and secrets are never stored. This field is part of equality:
    # changing a default changes launch behavior and retained serialization.
    env_from_host_declarations: Tuple[HostEnvDeclaration, ...] = field(
        default_factory=tuple,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.network not in _INTERFACE_NETWORK_GRANTS:
            raise ValueError("interface network grant must be 'disabled' or 'enabled'.")
        environment = _unique_texts(
            self.env_from_host,
            "interface host environment names",
            max_items=128,
            max_bytes=128,
        )
        secrets = _unique_texts(
            self.secrets_from_host,
            "interface host secret names",
            max_items=128,
            max_bytes=128,
        )
        overlap = set(environment) & set(secrets)
        if overlap:
            raise ValueError(
                "interface host environment and secret names overlap: "
                f"{sorted(overlap)!r}."
            )
        for name in (*environment, *secrets):
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ValueError(f"interface host environment name {name!r} is invalid.")
        declarations = tuple(self.env_from_host_declarations)
        if not declarations:
            # Normalize direct construction and historical names-only records
            # to the same semantic representation used by current authoring.
            declarations = tuple(HostEnvDeclaration(name) for name in environment)
        if any(not isinstance(item, HostEnvDeclaration) for item in declarations):
            raise TypeError(
                "interface host environment declarations must be HostEnvDeclaration values."
            )
        if declarations and tuple(item.name for item in declarations) != environment:
            raise ValueError(
                "interface host environment declarations must match the retained names."
            )
        object.__setattr__(self, "env_from_host", environment)
        object.__setattr__(self, "secrets_from_host", secrets)
        object.__setattr__(self, "env_from_host_declarations", declarations)

    @property
    def authoring_env_from_host(self) -> Tuple[Any, ...]:
        """Return declarations with public defaults for launch-time resolution.

        Older retained records contain names only. A current authored or
        retained profile preserves any public fallback and description.
        """

        if not self.env_from_host_declarations:
            return tuple(self.env_from_host)
        result = []
        for declaration in self.env_from_host_declarations:
            if declaration.default is None:
                result.append(declaration.name)
                continue
            item: JsonDict = {
                "name": declaration.name,
                "default": declaration.default,
            }
            if declaration.description:
                item["description"] = declaration.description
            result.append(item)
        return tuple(result)

    def to_dict(self) -> JsonDict:
        return {
            "envFromHost": list(self.authoring_env_from_host),
            "network": self.network,
            "secretsFromHost": list(self.secrets_from_host),
        }

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceGrantSpec":
        _exact_keys(
            payload,
            {"envFromHost", "network", "secretsFromHost"},
            "interface grants",
        )
        # An authored declaration may name a value or name it with a public
        # package default. The declaration is retained because it changes
        # launch behavior; resolved host values and secrets remain excluded.
        declarations = compile_host_env_declarations(
            payload["envFromHost"], location="interface grants.envFromHost"
        )
        declarations = tuple(
            sorted(declarations, key=lambda item: item.name.encode("utf-8"))
        )
        return cls(
            network=payload["network"],
            env_from_host=tuple(item.name for item in declarations),
            secrets_from_host=tuple(payload["secretsFromHost"]),
            env_from_host_declarations=declarations,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceGrantSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface grants are not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted interface grants are invalid: {error}") from error


@dataclass(frozen=True)
class InterfaceResourceSpec:
    """Bounded capacity request for one interface session."""

    cpu: int
    memory_mib: int
    gpus: int = 0

    def __post_init__(self) -> None:
        positive_int(self.cpu, "interface resources cpu")
        positive_int(self.memory_mib, "interface resources memory_mib")
        nonnegative_int(self.gpus, "interface resources gpus")
        if self.cpu > 1024 or self.memory_mib > 1_048_576 or self.gpus > 64:
            raise ValueError("interface resource request exceeds platform bounds.")

    def to_dict(self) -> JsonDict:
        return {"cpu": self.cpu, "gpus": self.gpus, "memoryMiB": self.memory_mib}

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceResourceSpec":
        _exact_keys(payload, {"cpu", "gpus", "memoryMiB"}, "interface resources")
        return cls(
            cpu=payload["cpu"],
            memory_mib=payload["memoryMiB"],
            gpus=payload["gpus"],
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceResourceSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface resources are not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface resources are invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceReadinessSpec:
    """Bounded HTTP readiness check for a web presentation."""

    path: str = "/"
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        path = _bounded_text(
            self.path,
            "interface readiness path",
            max_bytes=2048,
        )
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("interface readiness path must be one local HTTP path.")
        if "#" in path or "://" in path or ".." in path.split("/"):
            raise ValueError("interface readiness path is not local and canonical.")
        nonnegative_int(self.timeout_seconds, "interface readiness timeout_seconds")
        if self.timeout_seconds > 600:
            raise ValueError("interface readiness timeout_seconds exceeds ten minutes.")
        object.__setattr__(self, "path", path)

    def to_dict(self) -> JsonDict:
        return {
            "readyPath": self.path,
            "readyTimeoutSeconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceReadinessSpec":
        try:
            _exact_keys(
                payload,
                {"readyPath", "readyTimeoutSeconds"},
                "interface readiness",
            )
            result = cls(
                path=payload["readyPath"],
                timeout_seconds=payload["readyTimeoutSeconds"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("interface readiness is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface readiness is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class WebPresentationSpec:
    """A provider-neutral web presentation and its readiness contract."""

    port: int
    extra_ports: Tuple[int, ...] = field(default_factory=tuple)
    readiness: InterfaceReadinessSpec = field(default_factory=InterfaceReadinessSpec)
    kind: str = "web"

    def __post_init__(self) -> None:
        if self.kind != "web":
            raise ValueError("interface presentation kind must be 'web'.")
        positive_int(self.port, "interface presentation port")
        if self.port > 65_535:
            raise ValueError("interface presentation port exceeds 65535.")
        if not isinstance(self.extra_ports, (list, tuple)):
            raise TypeError("interface extra_ports must be a sequence.")
        if len(self.extra_ports) > _MAX_INTERFACE_PORTS:
            raise ValueError("interface presentation contains too many extra ports.")
        ports = tuple(sorted(self.extra_ports))
        if len(set(ports)) != len(ports) or self.port in ports:
            raise ValueError("interface presentation ports must be unique.")
        for port in ports:
            positive_int(port, "interface extra port")
            if port > 65_535:
                raise ValueError("interface extra port exceeds 65535.")
        if not isinstance(self.readiness, InterfaceReadinessSpec):
            raise TypeError("interface readiness must be InterfaceReadinessSpec.")
        object.__setattr__(self, "extra_ports", ports)

    def to_dict(self) -> JsonDict:
        return {
            "extraPorts": list(self.extra_ports),
            "kind": self.kind,
            "port": self.port,
            **self.readiness.to_dict(),
        }

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "WebPresentationSpec":
        _exact_keys(
            payload,
            {"extraPorts", "kind", "port", "readyPath", "readyTimeoutSeconds"},
            "web presentation",
        )
        return cls(
            port=payload["port"],
            extra_ports=tuple(payload["extraPorts"]),
            readiness=InterfaceReadinessSpec(
                path=payload["readyPath"],
                timeout_seconds=payload["readyTimeoutSeconds"],
            ),
            kind=payload["kind"],
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WebPresentationSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("web presentation is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted web presentation is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceAcceptsSpec:
    """Immutable selection compatibility declared by an interface."""

    selection_kinds: Tuple[str, ...]
    media_types: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        kinds = _unique_texts(
            self.selection_kinds,
            "interface selection kinds",
            max_items=len(_INTERFACE_SELECTION_KINDS),
            max_bytes=32,
        )
        if not kinds or not set(kinds) <= _INTERFACE_SELECTION_KINDS:
            raise ValueError("interface selection kinds are empty or unsupported.")
        media_types = _unique_texts(
            self.media_types,
            "interface media types",
            max_items=64,
            max_bytes=256,
        )
        for media_type in media_types:
            if _MEDIA_TYPE_RE.fullmatch(media_type) is None:
                raise ValueError(f"interface media type {media_type!r} is invalid.")
        object.__setattr__(self, "selection_kinds", kinds)
        object.__setattr__(self, "media_types", media_types)

    def to_dict(self) -> JsonDict:
        return {
            "mediaTypes": list(self.media_types),
            "selectionKinds": list(self.selection_kinds),
        }

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceAcceptsSpec":
        _exact_keys(
            payload,
            {"mediaTypes", "selectionKinds"},
            "interface accepts",
        )
        return cls(
            selection_kinds=tuple(payload["selectionKinds"]),
            media_types=tuple(payload["mediaTypes"]),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceAcceptsSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface accepts are not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface accepts are invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceOutputActionSpec:
    """One registered command that may run against a sealed interface output.

    Output actions are authored in the Catalog interface profile rather than
    accepted from the runtime output control file.  This keeps the executable
    allowlist in the exact registered Catalog revision.  ``runtime`` means
    OptPilot prepares a new isolated execution from the originating
    interface's prepared runtime; it never reuses the live interface process,
    its writable state, or its launch-time environment/secret grants.
    """

    action_id: str
    label: str
    command: Tuple[str, ...]
    timeout_seconds: int
    cwd: str = "."
    accepts_arguments: bool = False
    runtime: str = "originating-interface"
    show_in_output_card: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "action_id",
            _scope_name(self.action_id, "interface output action id"),
        )
        object.__setattr__(
            self,
            "label",
            _bounded_text(
                self.label,
                "interface output action label",
                max_bytes=256,
            ),
        )
        object.__setattr__(
            self,
            "command",
            _portable_command(
                self.command,
                "interface output action command",
            ),
        )
        object.__setattr__(
            self,
            "cwd",
            _portable_root_or_path(
                self.cwd,
                "interface output action cwd",
            ),
        )
        positive_int(
            self.timeout_seconds,
            "interface output action timeout_seconds",
        )
        if self.timeout_seconds > 3_600:
            raise ValueError(
                "interface output action timeout_seconds exceeds one hour."
            )
        if not isinstance(self.accepts_arguments, bool):
            raise TypeError(
                "interface output action accepts_arguments must be a boolean."
            )
        if not isinstance(self.show_in_output_card, bool):
            raise TypeError(
                "interface output action show_in_output_card must be a boolean."
            )
        if self.runtime != "originating-interface":
            raise ValueError(
                "interface output action runtime must be "
                "'originating-interface'."
            )

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "acceptsArguments": self.accepts_arguments,
            "command": list(self.command),
            "cwd": self.cwd,
            "id": self.action_id,
            "label": self.label,
            "runtime": self.runtime,
            "timeoutSeconds": self.timeout_seconds,
        }
        if not self.show_in_output_card:
            payload["showInOutputCard"] = False
        return payload

    @classmethod
    def from_authoring_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "InterfaceOutputActionSpec":
        if not isinstance(payload, Mapping):
            raise TypeError("interface output action must be a mapping.")
        actual = set(payload)
        allowed = {
            "acceptsArguments",
            "command",
            "cwd",
            "id",
            "label",
            "runtime",
            "showInOutputCard",
            "timeoutSeconds",
        }
        required = {"command", "id", "label", "timeoutSeconds"}
        if not actual <= allowed or not required <= actual:
            raise ValueError(
                "interface output action fields differ; "
                f"missing={sorted(required - actual)!r}, "
                f"extra={sorted(actual - allowed)!r}."
            )
        return cls(
            action_id=payload["id"],
            label=payload["label"],
            command=tuple(payload["command"]),
            timeout_seconds=payload["timeoutSeconds"],
            cwd=payload.get("cwd", "."),
            accepts_arguments=payload.get("acceptsArguments", False),
            runtime=payload.get("runtime", "originating-interface"),
            show_in_output_card=payload.get("showInOutputCard", True),
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "InterfaceOutputActionSpec":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface output action is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface output action is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceLaunchProfile:
    """Complete typed, path-free contract for one contextual interface."""

    profile_id: str
    label: str
    description: str
    process: InterfaceProcessInvocation
    runtime: InterfaceRuntimeSpec
    grants: InterfaceGrantSpec
    resources: InterfaceResourceSpec
    timeout_seconds: int
    presentation: WebPresentationSpec
    accepts: InterfaceAcceptsSpec
    outputs: bool = False
    output_actions: Tuple[InterfaceOutputActionSpec, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _scope_name(self.profile_id, "interface profile id"),
        )
        object.__setattr__(
            self,
            "label",
            _bounded_text(self.label, "interface profile label", max_bytes=256),
        )
        object.__setattr__(
            self,
            "description",
            _bounded_text(
                self.description,
                "interface profile description",
                max_bytes=4096,
                allow_empty=True,
            ),
        )
        if not isinstance(self.process, InterfaceProcessInvocation):
            raise TypeError("interface process must be InterfaceProcessInvocation.")
        if not isinstance(self.runtime, InterfaceRuntimeSpec):
            raise TypeError("interface runtime must be InterfaceRuntimeSpec.")
        if not isinstance(self.grants, InterfaceGrantSpec):
            raise TypeError("interface grants must be InterfaceGrantSpec.")
        if not isinstance(self.resources, InterfaceResourceSpec):
            raise TypeError("interface resources must be InterfaceResourceSpec.")
        if not isinstance(self.presentation, WebPresentationSpec):
            raise TypeError("interface presentation must be WebPresentationSpec.")
        if not isinstance(self.accepts, InterfaceAcceptsSpec):
            raise TypeError("interface accepts must be InterfaceAcceptsSpec.")
        if not isinstance(self.outputs, bool):
            raise TypeError("interface outputs must be a boolean.")
        actions = tuple(self.output_actions)
        if any(
            not isinstance(action, InterfaceOutputActionSpec)
            for action in actions
        ):
            raise TypeError(
                "interface output_actions must contain "
                "InterfaceOutputActionSpec values."
            )
        if len(actions) > _MAX_INTERFACE_OUTPUT_ACTIONS:
            raise ValueError("interface output_actions contains too many actions.")
        if len({action.action_id for action in actions}) != len(actions):
            raise ValueError("interface output action ids must be unique.")
        if actions and not self.outputs:
            raise ValueError(
                "interface output actions require output reporting."
            )
        object.__setattr__(self, "output_actions", actions)
        positive_int(self.timeout_seconds, "interface timeout_seconds")
        if self.timeout_seconds > 604_800:
            raise ValueError("interface timeout_seconds exceeds seven days.")
        _bounded_record(self.to_dict(), "interface launch profile")

    @property
    def command(self) -> Tuple[str, ...]:
        return self.process.command

    @property
    def cwd(self) -> str:
        return self.process.cwd

    @property
    def env(self) -> Mapping[str, str]:
        return self.process.env

    def to_dict(self) -> JsonDict:
        process = self.process.to_dict()
        result = {
            "accepts": self.accepts.to_dict(),
            "command": process["command"],
            "cwd": process["cwd"],
            "description": self.description,
            "env": process["env"],
            "grants": self.grants.to_dict(),
            "id": self.profile_id,
            "label": self.label,
            "presentation": self.presentation.to_dict(),
            "resources": self.resources.to_dict(),
            "runtime": self.runtime.to_dict(),
            "timeoutSeconds": self.timeout_seconds,
        }
        if self.outputs:
            result["outputs"] = (
                {
                    "actions": [
                        action.to_dict() for action in self.output_actions
                    ]
                }
                if self.output_actions
                else True
            )
        return result

    @classmethod
    def from_authoring_dict(cls, payload: Mapping[str, Any]) -> "InterfaceLaunchProfile":
        expected = {
            "accepts",
            "command",
            "cwd",
            "description",
            "env",
            "grants",
            "id",
            "label",
            "presentation",
            "resources",
            "runtime",
            "timeoutSeconds",
        }
        if isinstance(payload, Mapping) and "outputs" in payload:
            expected.add("outputs")
        _exact_keys(
            payload,
            expected,
            "interface launch profile",
        )
        raw_outputs = payload.get("outputs")
        if raw_outputs is True:
            output_actions: Tuple[InterfaceOutputActionSpec, ...] = ()
        elif isinstance(raw_outputs, Mapping):
            if set(raw_outputs) != {"actions"}:
                raise ValueError(
                    "interface outputs mapping must contain exactly actions."
                )
            raw_actions = raw_outputs["actions"]
            if (
                isinstance(raw_actions, (str, bytes))
                or not isinstance(raw_actions, (list, tuple))
                or not raw_actions
            ):
                raise ValueError(
                    "interface outputs actions must be a non-empty sequence."
                )
            if len(raw_actions) > _MAX_INTERFACE_OUTPUT_ACTIONS:
                raise ValueError(
                    "interface outputs actions contains too many actions."
                )
            output_actions = tuple(
                InterfaceOutputActionSpec.from_authoring_dict(action)
                for action in raw_actions
            )
        elif raw_outputs is None:
            output_actions = ()
        else:
            raise ValueError(
                "interface outputs must be true or an actions mapping when declared."
            )
        return cls(
            profile_id=payload["id"],
            label=payload["label"],
            description=payload["description"],
            process=InterfaceProcessInvocation(
                command=tuple(payload["command"]),
                cwd=payload["cwd"],
                env=payload["env"],
            ),
            runtime=InterfaceRuntimeSpec.from_authoring_dict(payload["runtime"]),
            grants=InterfaceGrantSpec.from_authoring_dict(payload["grants"]),
            resources=InterfaceResourceSpec.from_authoring_dict(payload["resources"]),
            timeout_seconds=payload["timeoutSeconds"],
            presentation=WebPresentationSpec.from_authoring_dict(payload["presentation"]),
            accepts=InterfaceAcceptsSpec.from_authoring_dict(payload["accepts"]),
            outputs=raw_outputs is not None,
            output_actions=output_actions,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InterfaceLaunchProfile":
        try:
            result = cls.from_authoring_dict(payload)
            if result.to_dict() != dict(payload):
                raise ValueError("interface launch profile is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface launch profile is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class EnvironmentRevisionManifest:
    """Exact compiled environment semantics and immutable input layers."""

    environment_id: str
    compiler_id: str
    compiler_version: str
    authored_config: ScopePath
    source_layers: Tuple[ScopeLayer, ...]
    evaluator_contract: Mapping[str, Any]
    candidate_contract: Mapping[str, Any]
    attempt_input_layers: Tuple[ScopeLayer, ...] = field(default_factory=tuple)
    projection_contract: Mapping[str, Any] = field(default_factory=dict)
    interface_profiles: Tuple[InterfaceLaunchProfile, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        required_text(self.environment_id, "environment id")
        required_text(self.compiler_id, "environment compiler id", max_bytes=256)
        required_text(self.compiler_version, "environment compiler version", max_bytes=128)
        if not isinstance(self.authored_config, ScopePath):
            raise TypeError("authored_config must be a ScopePath.")
        sources = _canonical_layers(self.source_layers, "environment source_layers")
        if not sources:
            raise ValueError("environment source_layers must not be empty.")
        if self.authored_config.scope not in {item.scope for item in sources}:
            raise ValueError("authored_config scope is absent from environment source_layers.")
        attempt_inputs = _canonical_layers(
            self.attempt_input_layers, "environment attempt_input_layers"
        )
        profiles = tuple(self.interface_profiles)
        if any(not isinstance(item, InterfaceLaunchProfile) for item in profiles):
            raise TypeError(
                "environment interface_profiles must contain InterfaceLaunchProfile values."
            )
        if len({item.profile_id for item in profiles}) != len(profiles):
            raise ValueError("environment interface profile ids must be unique.")
        profiles = tuple(sorted(profiles, key=lambda item: item.profile_id.encode("utf-8")))
        object.__setattr__(self, "source_layers", sources)
        object.__setattr__(self, "attempt_input_layers", attempt_inputs)
        object.__setattr__(
            self,
            "evaluator_contract",
            _freeze_mapping(self.evaluator_contract, "evaluator contract", nonempty=True),
        )
        object.__setattr__(
            self,
            "candidate_contract",
            _freeze_mapping(self.candidate_contract, "candidate contract", nonempty=True),
        )
        object.__setattr__(
            self,
            "projection_contract",
            _freeze_mapping(self.projection_contract, "projection contract"),
        )
        object.__setattr__(self, "interface_profiles", profiles)
        _bounded_record(self.to_dict(), "environment revision manifest")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def revision_digest(self) -> str:
        return self.digest

    def to_dict(self) -> JsonDict:
        return {
            "attempt_input_layers": [item.to_dict() for item in self.attempt_input_layers],
            "authored_config": self.authored_config.to_dict(),
            "candidate_contract": thaw_json(self.candidate_contract),
            "compiler": {"id": self.compiler_id, "version": self.compiler_version},
            "environment_id": self.environment_id,
            "evaluator_contract": thaw_json(self.evaluator_contract),
            "interface_profiles": [item.to_dict() for item in self.interface_profiles],
            "projection_contract": thaw_json(self.projection_contract),
            "schema": ENVIRONMENT_REVISION_SCHEMA,
            "source_layers": [item.to_dict() for item in self.source_layers],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EnvironmentRevisionManifest":
        try:
            _exact_keys(
                payload,
                {
                    "attempt_input_layers",
                    "authored_config",
                    "candidate_contract",
                    "compiler",
                    "environment_id",
                    "evaluator_contract",
                    "interface_profiles",
                    "projection_contract",
                    "schema",
                    "source_layers",
                },
                "environment revision manifest",
            )
            if payload["schema"] != ENVIRONMENT_REVISION_SCHEMA:
                raise ValueError("environment revision schema is unsupported.")
            compiler = payload["compiler"]
            _exact_keys(compiler, {"id", "version"}, "environment compiler")
            for name in ("source_layers", "attempt_input_layers", "interface_profiles"):
                if not isinstance(payload[name], list):
                    raise TypeError(f"environment revision {name} must be a list.")
            result = cls(
                environment_id=payload["environment_id"],
                compiler_id=compiler["id"],
                compiler_version=compiler["version"],
                authored_config=ScopePath.from_dict(payload["authored_config"]),
                source_layers=tuple(
                    ScopeLayer.from_dict(item) for item in payload["source_layers"]
                ),
                evaluator_contract=payload["evaluator_contract"],
                candidate_contract=payload["candidate_contract"],
                attempt_input_layers=tuple(
                    ScopeLayer.from_dict(item)
                    for item in payload["attempt_input_layers"]
                ),
                projection_contract=payload["projection_contract"],
                interface_profiles=tuple(
                    InterfaceLaunchProfile.from_dict(item)
                    for item in payload["interface_profiles"]
                ),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("environment revision manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted environment revision manifest is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class PreparedEnvironmentRuntimeManifest:
    """Exact portable runtime requirements and prepared immutable layers."""

    environment_revision_digest: str
    runtime_kind: str
    runtime_settings: Mapping[str, Any]
    prepared_layers: Tuple[ScopeLayer, ...] = field(default_factory=tuple)
    workdir: Optional[ScopePath] = None
    oci_image_digest: Optional[str] = None
    platform: Optional[str] = None
    portability: str = "portable"
    builder_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        lower_hex_digest(
            self.environment_revision_digest, "runtime environment revision digest"
        )
        required_text(self.runtime_kind, "runtime kind", max_bytes=64)
        if self.runtime_kind not in _RUNTIME_KINDS:
            raise ValueError("runtime_kind must be 'process' or 'container'.")
        if self.workdir is not None and not isinstance(self.workdir, ScopePath):
            raise TypeError("runtime workdir must be a ScopePath or None.")
        image_digest = optional_text(
            self.oci_image_digest, "runtime OCI image digest", max_bytes=71
        )
        if image_digest is not None and _OCI_DIGEST_RE.fullmatch(image_digest) is None:
            raise ValueError("runtime OCI image digest must be sha256:<64 lowercase hex>.")
        if self.runtime_kind == "container" and image_digest is None:
            raise ValueError("container runtimes require an immutable OCI image digest.")
        if self.runtime_kind == "process" and image_digest is not None:
            raise ValueError("process runtimes cannot define an OCI image digest.")
        optional_text(self.platform, "runtime platform", max_bytes=256)
        required_text(self.portability, "runtime portability", max_bytes=64)
        if self.portability not in _PORTABILITY_VALUES:
            raise ValueError(
                "runtime portability must be 'portable' or 'provider-scoped'."
            )
        if self.builder_fingerprint is not None:
            lower_hex_digest(self.builder_fingerprint, "runtime builder fingerprint")
        object.__setattr__(
            self,
            "runtime_settings",
            _freeze_mapping(self.runtime_settings, "runtime settings"),
        )
        object.__setattr__(
            self,
            "prepared_layers",
            _canonical_layers(self.prepared_layers, "runtime prepared_layers"),
        )
        object.__setattr__(self, "oci_image_digest", image_digest)
        _bounded_record(self.to_dict(), "prepared environment runtime manifest")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def runtime_digest(self) -> str:
        return self.digest

    def to_dict(self) -> JsonDict:
        return {
            "builder_fingerprint": self.builder_fingerprint,
            "environment_revision_digest": self.environment_revision_digest,
            "oci_image_digest": self.oci_image_digest,
            "platform": self.platform,
            "portability": self.portability,
            "prepared_layers": [item.to_dict() for item in self.prepared_layers],
            "runtime_kind": self.runtime_kind,
            "runtime_settings": thaw_json(self.runtime_settings),
            "schema": PREPARED_ENVIRONMENT_RUNTIME_SCHEMA,
            "workdir": self.workdir.to_dict() if self.workdir is not None else None,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "PreparedEnvironmentRuntimeManifest":
        try:
            _exact_keys(
                payload,
                {
                    "builder_fingerprint",
                    "environment_revision_digest",
                    "oci_image_digest",
                    "platform",
                    "portability",
                    "prepared_layers",
                    "runtime_kind",
                    "runtime_settings",
                    "schema",
                    "workdir",
                },
                "prepared environment runtime manifest",
            )
            if payload["schema"] != PREPARED_ENVIRONMENT_RUNTIME_SCHEMA:
                raise ValueError("prepared environment runtime schema is unsupported.")
            if not isinstance(payload["prepared_layers"], list):
                raise TypeError("runtime prepared_layers must be a list.")
            result = cls(
                environment_revision_digest=payload["environment_revision_digest"],
                runtime_kind=payload["runtime_kind"],
                runtime_settings=payload["runtime_settings"],
                prepared_layers=tuple(
                    ScopeLayer.from_dict(item) for item in payload["prepared_layers"]
                ),
                workdir=(
                    ScopePath.from_dict(payload["workdir"])
                    if payload["workdir"] is not None
                    else None
                ),
                oci_image_digest=payload["oci_image_digest"],
                platform=payload["platform"],
                portability=payload["portability"],
                builder_fingerprint=payload["builder_fingerprint"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("prepared environment runtime manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted prepared environment runtime manifest is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class RunEvaluationTemplate:
    """Run-specific semantic defaults bound to exact environment/runtime revisions."""

    environment_revision_digest: str
    runtime_revision_digest: str
    objective: Mapping[str, Any]
    resource_profile: Mapping[str, Any]
    sandbox_spec: Mapping[str, Any]
    default_seed: Any = None

    def __post_init__(self) -> None:
        lower_hex_digest(
            self.environment_revision_digest, "template environment revision digest"
        )
        lower_hex_digest(self.runtime_revision_digest, "template runtime revision digest")
        objective = _freeze_mapping(self.objective, "template objective", nonempty=True)
        primary = objective.get("primaryMetric")
        if not isinstance(primary, Mapping):
            raise ValueError("template objective.primaryMetric must be a mapping.")
        required_text(
            primary.get("name"), "template objective primary metric name", max_bytes=256
        )
        object.__setattr__(self, "objective", objective)
        object.__setattr__(
            self,
            "resource_profile",
            _freeze_mapping(self.resource_profile, "template resource profile"),
        )
        object.__setattr__(
            self,
            "sandbox_spec",
            _freeze_mapping(self.sandbox_spec, "template sandbox spec"),
        )
        object.__setattr__(
            self, "default_seed", freeze_json(self.default_seed, label="template default seed")
        )
        _bounded_record(self.to_dict(), "run evaluation template")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def template_digest(self) -> str:
        return self.digest

    def to_dict(self) -> JsonDict:
        return {
            "default_seed": thaw_json(self.default_seed),
            "environment_revision_digest": self.environment_revision_digest,
            "objective": thaw_json(self.objective),
            "resource_profile": thaw_json(self.resource_profile),
            "runtime_revision_digest": self.runtime_revision_digest,
            "sandbox_spec": thaw_json(self.sandbox_spec),
            "schema": RUN_EVALUATION_TEMPLATE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunEvaluationTemplate":
        try:
            _exact_keys(
                payload,
                {
                    "default_seed",
                    "environment_revision_digest",
                    "objective",
                    "resource_profile",
                    "runtime_revision_digest",
                    "sandbox_spec",
                    "schema",
                },
                "run evaluation template",
            )
            if payload["schema"] != RUN_EVALUATION_TEMPLATE_SCHEMA:
                raise ValueError("run evaluation template schema is unsupported.")
            result = cls(
                environment_revision_digest=payload["environment_revision_digest"],
                runtime_revision_digest=payload["runtime_revision_digest"],
                objective=payload["objective"],
                resource_profile=payload["resource_profile"],
                sandbox_spec=payload["sandbox_spec"],
                default_seed=payload["default_seed"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("run evaluation template is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted run evaluation template is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class RunEvaluationClosure:
    """Consistent environment, runtime, and run-template identity bundle."""

    environment_revision: EnvironmentRevisionManifest
    prepared_runtime: PreparedEnvironmentRuntimeManifest
    evaluation_template: RunEvaluationTemplate

    def __post_init__(self) -> None:
        if not isinstance(self.environment_revision, EnvironmentRevisionManifest):
            raise TypeError("environment_revision must be an EnvironmentRevisionManifest.")
        if not isinstance(self.prepared_runtime, PreparedEnvironmentRuntimeManifest):
            raise TypeError(
                "prepared_runtime must be a PreparedEnvironmentRuntimeManifest."
            )
        if not isinstance(self.evaluation_template, RunEvaluationTemplate):
            raise TypeError("evaluation_template must be a RunEvaluationTemplate.")
        if (
            self.prepared_runtime.environment_revision_digest
            != self.environment_revision.digest
        ):
            raise ValueError("prepared runtime targets a different environment revision.")
        if (
            self.evaluation_template.environment_revision_digest
            != self.environment_revision.digest
        ):
            raise ValueError("evaluation template targets a different environment revision.")
        if self.evaluation_template.runtime_revision_digest != self.prepared_runtime.digest:
            raise ValueError("evaluation template targets a different runtime revision.")
        scopes = {
            item.scope
            for item in (
                *self.environment_revision.source_layers,
                *self.environment_revision.attempt_input_layers,
                *self.prepared_runtime.prepared_layers,
            )
        }
        if (
            self.prepared_runtime.workdir is not None
            and self.prepared_runtime.workdir.scope not in scopes
        ):
            raise ValueError("runtime workdir scope is absent from the closure layers.")
        _bounded_record(self.to_dict(), "run evaluation closure")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def required_content_refs(
        self,
    ) -> Tuple[Tuple[str, PhysicalContentRef], ...]:
        """Return the exact semantic roots without store-placement facts."""

        values = {
            (RUN_ENVIRONMENT_SOURCE_ROLE, item.snapshot_ref)
            for item in self.environment_revision.source_layers
        }
        values.update(
            (RUN_ATTEMPT_INPUT_ROLE, item.snapshot_ref)
            for item in self.environment_revision.attempt_input_layers
        )
        values.update(
            (RUN_PREPARED_RUNTIME_ROLE, item.snapshot_ref)
            for item in self.prepared_runtime.prepared_layers
        )
        return tuple(sorted(values, key=lambda item: (item[0], str(item[1]))))

    @property
    def content_refs_by_role(self) -> Mapping[str, Tuple[PhysicalContentRef, ...]]:
        grouped: dict[str, list[PhysicalContentRef]] = {}
        for role, content_ref in self.required_content_refs:
            grouped.setdefault(role, []).append(content_ref)
        return MappingProxyType(
            {role: tuple(refs) for role, refs in sorted(grouped.items())}
        )

    def to_dict(self) -> JsonDict:
        return {
            "environment_revision": self.environment_revision.to_dict(),
            "environment_revision_digest": self.environment_revision.digest,
            "evaluation_template": self.evaluation_template.to_dict(),
            "evaluation_template_digest": self.evaluation_template.digest,
            "prepared_runtime": self.prepared_runtime.to_dict(),
            "runtime_revision_digest": self.prepared_runtime.digest,
            "schema": RUN_EVALUATION_CLOSURE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunEvaluationClosure":
        try:
            _exact_keys(
                payload,
                {
                    "environment_revision",
                    "environment_revision_digest",
                    "evaluation_template",
                    "evaluation_template_digest",
                    "prepared_runtime",
                    "runtime_revision_digest",
                    "schema",
                },
                "run evaluation closure",
            )
            if payload["schema"] != RUN_EVALUATION_CLOSURE_SCHEMA:
                raise ValueError("run evaluation closure schema is unsupported.")
            environment = EnvironmentRevisionManifest.from_dict(
                payload["environment_revision"]
            )
            runtime = PreparedEnvironmentRuntimeManifest.from_dict(
                payload["prepared_runtime"]
            )
            template = RunEvaluationTemplate.from_dict(payload["evaluation_template"])
            if payload["environment_revision_digest"] != environment.digest:
                raise ValueError("environment revision digest does not match its manifest.")
            if payload["runtime_revision_digest"] != runtime.digest:
                raise ValueError("runtime revision digest does not match its manifest.")
            if payload["evaluation_template_digest"] != template.digest:
                raise ValueError("evaluation template digest does not match its manifest.")
            result = cls(environment, runtime, template)
            if result.to_dict() != dict(payload):
                raise ValueError("run evaluation closure is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted run evaluation closure is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class ResolvedRunEvaluationClosure:
    """One immutable evaluation closure plus all current store availabilities."""

    closure: RunEvaluationClosure
    content_bindings: Tuple[OwnerMembership, ...]
    availability: str = "available"

    def __post_init__(self) -> None:
        if not isinstance(self.closure, RunEvaluationClosure):
            raise TypeError("closure must be a RunEvaluationClosure.")
        bindings = tuple(sorted(set(self.content_bindings)))
        required = set(self.closure.required_content_refs)
        actual = {(item.role, item.content_ref) for item in bindings}
        if not actual.issubset(required):
            raise ValueError("resolved evaluation bindings contain undeclared refs.")
        if self.availability not in {"available", "unavailable"}:
            raise ValueError("resolved evaluation availability is unsupported.")
        if (actual == required) != (self.availability == "available"):
            raise ValueError(
                "resolved evaluation availability differs from its bindings."
            )
        object.__setattr__(self, "content_bindings", bindings)

    def to_dict(self) -> JsonDict:
        return {
            "closure": self.closure.to_dict(),
            "content_bindings": [item.to_dict() for item in self.content_bindings],
            "availability": self.availability,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ResolvedRunEvaluationClosure":
        try:
            _exact_keys(
                payload,
                {"closure", "content_bindings", "availability"},
                "resolved run evaluation closure",
            )
            if not isinstance(payload["content_bindings"], list):
                raise TypeError("resolved content_bindings must be a list.")
            return cls(
                closure=RunEvaluationClosure.from_dict(payload["closure"]),
                content_bindings=tuple(
                    OwnerMembership.from_dict(item)
                    for item in payload["content_bindings"]
                ),
                availability=payload["availability"],
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted resolved evaluation closure is invalid: {error}"
            ) from error


__all__ = [
    "EnvironmentRevisionManifest",
    "InterfaceAcceptsSpec",
    "InterfaceContainerBuildSpec",
    "InterfaceContainerSpec",
    "InterfaceGrantSpec",
    "InterfaceLaunchProfile",
    "InterfaceOutputActionSpec",
    "InterfaceProcessInvocation",
    "InterfaceReadinessSpec",
    "InterfaceResourceSpec",
    "InterfaceRuntimeSpec",
    "InterfaceSetupSpec",
    "PreparedEnvironmentRuntimeManifest",
    "RunEvaluationClosure",
    "RunEvaluationTemplate",
    "ResolvedRunEvaluationClosure",
    "ScopeLayer",
    "ScopePath",
    "WebPresentationSpec",
    "RUN_ATTEMPT_INPUT_ROLE",
    "RUN_ENVIRONMENT_SOURCE_ROLE",
    "RUN_PREPARED_RUNTIME_ROLE",
]
