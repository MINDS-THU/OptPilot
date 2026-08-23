"""Portable per-attempt runtime plans and portable binding evidence.

``RunDefinitionManifest`` remains the durable run-level specification.  This
module derives the smaller provider-neutral invocation plan needed by one
attempt.  A later provider binder may realize that plan with Realm-local lease
ids, volume ids, checkout paths, process environment, and backend handles, but
none of those operational facts belong in the records defined here.

The v1 compiler is intentionally narrow: one retained parameter/Python/process
environment, one immutable source projection, and two fresh ephemeral writable
volumes.  Unsupported semantics are rejected rather than silently omitted.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, NoReturn, Tuple

from .attempts import EvaluationSpec
from .realm._validation import freeze_json, lower_hex_digest, required_text, thaw_json
from .realm.errors import RealmIntegrityError
from .realm.filesystem_quota import FilesystemQuota
from .realm.manifests import validate_portable_path
from .realm.process_provider import ProcessProviderIdentity
from .realm.projection import ProjectionQuota, ProjectionSpec, TreeMapping
from .realm.refs import (
    CandidateRef,
    SnapshotRef,
    canonical_json_bytes,
    request_digest,
)
from .realm.run_closure import ScopeLayer, ScopePath
from .realm.run_definition import RunDefinitionManifest
from .realm.run_records import NormalizedCandidateEnvelope
from .retained_study_compiler import (
    LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA,
    RETAINED_PROCESS_STUDY_COMPILER_ID,
    RETAINED_PROCESS_STUDY_COMPILER_VERSION,
    package_projection_contract_features,
)
from .retained_file_candidates import (
    SEALED_FILE_CANDIDATE_SPEC_SCHEMA,
    portable_selection,
)
from .runtime_limits import (
    CONTROL_FILESYSTEM_QUOTA,
    MAX_ATTEMPT_INPUT_LAYERS,
    TRIAL_FILESYSTEM_QUOTA,
)
from .runtime_scopes import ENVIRONMENT_PREPARED_PYTHON_SCOPE
from .study_realm_compiler import STUDY_REALM_COMPILER_VERSION


JsonDict = Dict[str, Any]

PORTABLE_ATTEMPT_RUNTIME_SPEC_SCHEMA = "optpilot.portable-attempt-runtime-spec.v1"
EXECUTION_BINDING_EVIDENCE_SCHEMA = "optpilot.execution-binding-evidence.v1"
RUNTIME_PROVIDER_REQUIREMENT_SCHEMA = "optpilot.runtime-provider-requirement.v1"
PORTABLE_RUNTIME_SCOPE_SCHEMA = "optpilot.portable-runtime-scope.v1"
PROJECTION_SCOPE_SOURCE_SCHEMA = "optpilot.projection-scope-source.v1"
VOLUME_SCOPE_SOURCE_SCHEMA = "optpilot.volume-scope-source.v1"
LAYERED_VOLUME_SCOPE_SOURCE_SCHEMA = "optpilot.layered-volume-scope-source.v1"
PROJECTED_INPUT_LAYER_SCHEMA = "optpilot.projected-input-layer.v1"
FILE_CANDIDATE_MATERIALIZATION_SCHEMA = (
    "optpilot.file-candidate-materialization.v1"
)
PYTHON_CALLABLE_ENTRYPOINT_SCHEMA = "optpilot.python-callable-entrypoint.v1"
RUNTIME_RESOURCE_REQUEST_SCHEMA = "optpilot.runtime-resource-request.v1"
WRITABLE_VOLUME_REQUIREMENT_SCHEMA = "optpilot.writable-volume-requirement.v1"
PROJECTION_BINDING_EVIDENCE_SCHEMA = "optpilot.projection-binding-evidence.v1"
WRITABLE_VOLUME_EVIDENCE_SCHEMA = "optpilot.writable-volume-evidence.v1"
EXECUTION_PROVIDER_FACTS_SCHEMA = "optpilot.execution-provider-facts.v1"

ENVIRONMENT_PROJECTION = "environment-inputs"
ENVIRONMENT_PROJECTION_PARTITION = "environment"
ENVIRONMENT_PREPARED_PYTHON_PARTITION = "environment-prepared-python"
CANDIDATE_PROJECTION_PARTITION = "candidate"
ENVIRONMENT_SOURCE_SCOPE = "environment-source"
TRIAL_SCOPE = "trial"
CONTROL_SCOPE = "control"

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_PROVIDER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!~-]*$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+!~-]*$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACCESS_VALUES = frozenset({"read", "read-write"})
_NETWORK_POLICIES = frozenset({"disabled", "enabled"})
_CLEANUP_POLICIES = frozenset({"always"})
_QUOTA_ENFORCEMENTS = frozenset({"advisory", "enforced"})
_READ_ONLY_ENFORCEMENTS = frozenset({"advisory", "enforced"})
_INPUT_LAYER_COLLISION_POLICIES = frozenset({"identical", "replace"})

_MAX_RECORD_BYTES = 1024 * 1024
_MAX_SCOPES = 64
_MAX_IMPORT_ROOTS = 128
_MAX_VOLUME_REQUIREMENTS = 32
_MAX_METRICS = 1024
_MAX_EVIDENCE_ITEMS = 64

class RuntimeBindingCompileError(ValueError):
    """Stable rejection from the retained-process runtime compiler."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> NoReturn:
    raise RuntimeBindingCompileError(code, message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _compile_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            "runtime_shape_unsupported",
            f"{label} fields differ from the retained process contract.",
        )


def _canonical_round_trip(result: Any, payload: Mapping[str, Any], label: str) -> None:
    if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
        raise ValueError(f"{label} is not canonical.")


def _bounded_record(payload: Mapping[str, Any], label: str) -> None:
    if len(canonical_json_bytes(payload)) > _MAX_RECORD_BYTES:
        raise ValueError(f"{label} exceeds the maximum encoded size.")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("runtime_shape_invalid", f"{label} must be a mapping.")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail("runtime_shape_invalid", f"{label} must be a sequence.")
    return value


def _scope_name(value: Any, label: str = "scope") -> str:
    value = required_text(value, label, max_bytes=64)
    if _SCOPE_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical logical name.")
    return value


def _safe_identifier(value: Any, label: str, *, max_bytes: int = 512) -> str:
    value = required_text(value, label, max_bytes=max_bytes)
    if value in {".", ".."} or _SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a path-free ASCII identifier.")
    return value


def _provider_token(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=256)
    if _SAFE_PROVIDER_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a path-free provider token.")
    return value


def _portable_root_or_path(value: Any, label: str) -> str:
    if value == ".":
        return "."
    try:
        return validate_portable_path(value)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(f"{label} must be '.' or a portable relative path.") from error


def _logical_absolute_path(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=1024)
    if "\\" in value or "\x00" in value or not value.startswith("/optpilot/"):
        raise ValueError(f"{label} must be a fixed /optpilot logical path.")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError(f"{label} must be a canonical logical path.")
    return value


def _paths_overlap(first: str, second: str) -> bool:
    return first == second or first.startswith(second + "/") or second.startswith(
        first + "/"
    )


def _sha256_ref(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=71)
    if _SHA256_REF_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be sha256:<64 lowercase hex>.")
    return value


def _optional_timeout(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number or None.")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive finite number or None.")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _canonical_json_equal(first: Any, second: Any) -> bool:
    return canonical_json_bytes(thaw_json(first)) == canonical_json_bytes(
        thaw_json(second)
    )


def _lower_sha256(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _portable_pattern(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=1024)
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise ValueError(f"{label} must be a portable relative pattern.")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must be a portable relative pattern.")
    return value


def _validate_candidate_options(value: Mapping[str, Any]) -> None:
    forbidden_keys = {
        "blob",
        "bundleRef",
        "candidateRoot",
        "contentRef",
        "directory",
        "hostPath",
        "path",
        "snapshotRef",
        "storeId",
    }
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if key in forbidden_keys:
                    raise ValueError(
                        "candidate options contain a reserved ref or path field."
                    )
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str) and (
            current.startswith(("/", "\\", "blob:", "tree:", "file:"))
            or "://" in current
        ):
            raise ValueError("candidate options contain a ref or host path.")


@dataclass(frozen=True)
class CandidateRuntimeInput:
    """Storeless candidate content identity supplied by fenced run authority.

    Store placement and owner membership remain provider/ledger authority; this
    compiler input carries only the normalized candidate identity and its one
    immutable file tree.
    """

    candidate_ref: CandidateRef
    candidate_format: str
    snapshot_ref: SnapshotRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_ref, CandidateRef):
            raise TypeError("candidate runtime input requires a CandidateRef.")
        if self.candidate_format not in {"parameters", "files"}:
            raise ValueError("candidate runtime input format is unsupported.")
        if self.candidate_format == "parameters":
            if self.snapshot_ref is not None:
                raise ValueError("parameter candidate input cannot contain a snapshot.")
        elif not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("file candidate input requires one SnapshotRef.")

    @classmethod
    def from_envelope(
        cls, envelope: NormalizedCandidateEnvelope
    ) -> "CandidateRuntimeInput":
        if not isinstance(envelope, NormalizedCandidateEnvelope):
            raise TypeError("envelope must be a NormalizedCandidateEnvelope.")
        if envelope.candidate_format == "parameters":
            if envelope.content_refs:
                raise ValueError("parameter candidate envelope contains content.")
            return cls(envelope.candidate_ref, "parameters", None)
        if envelope.candidate_format != "files":
            raise ValueError("candidate runtime input format is unsupported.")
        if (
            len(envelope.content_refs) != 1
            or not isinstance(envelope.content_refs[0], SnapshotRef)
        ):
            raise ValueError("file candidate envelope requires exactly one tree snapshot.")
        return cls(envelope.candidate_ref, "files", envelope.content_refs[0])


@dataclass(frozen=True, order=True)
class ProjectedInputLayer:
    """One portable immutable input mapped through a projection partition."""

    scope: str
    projection_name: str
    projection_subpath: str
    snapshot_ref: SnapshotRef
    source_subpath: str = "."
    destination_subpath: str = "."
    precedence: int = 0
    collision_policy: str = "identical"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _scope_name(self.scope))
        object.__setattr__(
            self,
            "projection_name",
            _scope_name(self.projection_name, "input layer projection name"),
        )
        object.__setattr__(
            self,
            "projection_subpath",
            _portable_root_or_path(
                self.projection_subpath, "input layer projection subpath"
            ),
        )
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("projected input layer requires a SnapshotRef.")
        object.__setattr__(
            self,
            "source_subpath",
            _portable_root_or_path(
                self.source_subpath, "input layer source subpath"
            ),
        )
        object.__setattr__(
            self,
            "destination_subpath",
            _portable_root_or_path(
                self.destination_subpath, "input layer destination subpath"
            ),
        )
        _nonnegative_int(self.precedence, "input layer precedence")
        if self.collision_policy not in _INPUT_LAYER_COLLISION_POLICIES:
            raise ValueError("input layer collision policy is unsupported.")

    def to_dict(self) -> JsonDict:
        return {
            "collision_policy": self.collision_policy,
            "destination_subpath": self.destination_subpath,
            "precedence": self.precedence,
            "projection_name": self.projection_name,
            "projection_subpath": self.projection_subpath,
            "schema": PROJECTED_INPUT_LAYER_SCHEMA,
            "scope": self.scope,
            "snapshot_ref": str(self.snapshot_ref),
            "source_subpath": self.source_subpath,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectedInputLayer":
        _exact_keys(
            payload,
            {
                "collision_policy",
                "destination_subpath",
                "precedence",
                "projection_name",
                "projection_subpath",
                "schema",
                "scope",
                "snapshot_ref",
                "source_subpath",
            },
            "projected input layer",
        )
        if payload["schema"] != PROJECTED_INPUT_LAYER_SCHEMA:
            raise ValueError("projected input layer schema is unsupported.")
        result = cls(
            scope=payload["scope"],
            projection_name=payload["projection_name"],
            projection_subpath=payload["projection_subpath"],
            snapshot_ref=SnapshotRef.parse(payload["snapshot_ref"]),
            source_subpath=payload["source_subpath"],
            destination_subpath=payload["destination_subpath"],
            precedence=payload["precedence"],
            collision_policy=payload["collision_policy"],
        )
        _canonical_round_trip(result, payload, "projected input layer")
        return result


@dataclass(frozen=True, order=True)
class FileCandidateManifestEntry:
    path: str
    sha256: str
    size_bytes: int
    executable: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", _portable_root_or_path(self.path, "candidate file path")
        )
        if self.path == ".":
            raise ValueError("candidate file path cannot be '.'.")
        object.__setattr__(
            self, "sha256", _lower_sha256(self.sha256, "candidate file sha256")
        )
        _nonnegative_int(self.size_bytes, "candidate file sizeBytes")
        if not isinstance(self.executable, bool):
            raise TypeError("candidate file executable must be a boolean.")

    def to_dict(self) -> JsonDict:
        return {
            "executable": self.executable,
            "path": self.path,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FileCandidateManifestEntry":
        _exact_keys(
            payload,
            {"executable", "path", "sha256", "sizeBytes"},
            "candidate manifest file",
        )
        result = cls(
            path=payload["path"],
            sha256=payload["sha256"],
            size_bytes=payload["sizeBytes"],
            executable=payload["executable"],
        )
        _canonical_round_trip(result, payload, "candidate manifest file")
        return result


@dataclass(frozen=True)
class FileCandidateMaterialization:
    """Strict logical file materialization understood by the attempt worker."""

    root: ScopePath
    directories: Tuple[str, ...]
    files: Tuple[FileCandidateManifestEntry, ...]
    entrypoint: str | None
    options: Mapping[str, Any]
    required_files: Tuple[str, ...]
    allow_patterns: Tuple[str, ...]
    deny_patterns: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.root, ScopePath) or self.root.scope != TRIAL_SCOPE:
            raise ValueError("file candidate root must use the trial scope.")
        for name in (
            "directories",
            "files",
            "required_files",
            "allow_patterns",
            "deny_patterns",
        ):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"file candidate {name} must be a sequence.")
        directories = tuple(
            _portable_root_or_path(item, "candidate directory")
            for item in self.directories
        )
        if any(item == "." for item in directories):
            raise ValueError("candidate directories cannot contain '.'.")
        if len(set(directories)) != len(directories):
            raise ValueError("candidate manifest directories must be unique.")
        directories = tuple(
            sorted(directories, key=lambda item: item.encode("utf-8"))
        )
        raw_files = tuple(self.files)
        if not raw_files or any(
            not isinstance(item, FileCandidateManifestEntry) for item in raw_files
        ):
            raise ValueError("file candidate materialization requires manifest files.")
        files = tuple(
            sorted(raw_files, key=lambda item: item.path.encode("utf-8"))
        )
        if len({item.path for item in files}) != len(files):
            raise ValueError("candidate manifest file paths must be unique.")
        file_paths = {item.path for item in files}
        if file_paths.intersection(directories):
            raise ValueError("candidate manifest paths cannot be both files and directories.")
        all_paths = (*directories, *file_paths)
        for file_path in file_paths:
            if any(
                other != file_path and other.startswith(file_path + "/")
                for other in all_paths
            ):
                raise ValueError("candidate manifest contains a file ancestor.")
        directory_set = set(directories)
        for path in all_paths:
            parts = path.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) not in directory_set:
                    raise ValueError(
                        "candidate manifest omits an explicit parent directory."
                    )
        entrypoint = self.entrypoint
        if entrypoint is not None:
            entrypoint = _portable_root_or_path(entrypoint, "candidate entrypoint")
            if entrypoint == ".":
                raise ValueError("candidate entrypoint cannot be '.'.")
        options = freeze_json(self.options, label="candidate materialization options")
        if not isinstance(options, Mapping):
            raise TypeError("candidate materialization options must be a mapping.")
        if options.get("candidateRoot") != self.root.relative_path:
            raise ValueError(
                "candidate materialization options differ from the logical root."
            )
        extra_options = dict(thaw_json(options))
        extra_options.pop("candidateRoot")
        _validate_candidate_options(extra_options)
        required = tuple(
            sorted(
                (
                    _portable_root_or_path(item, "required candidate file")
                    for item in self.required_files
                ),
                key=lambda item: item.encode("utf-8"),
            )
        )
        if any(item == "." for item in required) or len(set(required)) != len(required):
            raise ValueError("required candidate files must be unique file paths.")
        allow = tuple(
            sorted(
                (
                    _portable_pattern(item, "candidate allow pattern")
                    for item in self.allow_patterns
                ),
                key=lambda item: item.encode("utf-8"),
            )
        )
        deny = tuple(
            sorted(
                (
                    _portable_pattern(item, "candidate deny pattern")
                    for item in self.deny_patterns
                ),
                key=lambda item: item.encode("utf-8"),
            )
        )
        if len(set(allow)) != len(allow) or len(set(deny)) != len(deny):
            raise ValueError("candidate policy patterns must be unique.")
        if not set(required).issubset(file_paths):
            raise ValueError("required candidate files are absent from the manifest.")
        if entrypoint is not None and entrypoint not in file_paths:
            raise ValueError("candidate entrypoint is absent from the manifest files.")
        for path in file_paths:
            if allow and not any(
                fnmatch.fnmatchcase(path, pattern) for pattern in allow
            ):
                raise ValueError("candidate manifest file is outside the allow policy.")
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in deny):
                raise ValueError("candidate manifest file is denied by policy.")
        object.__setattr__(self, "directories", directories)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "entrypoint", entrypoint)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "required_files", required)
        object.__setattr__(self, "allow_patterns", allow)
        object.__setattr__(self, "deny_patterns", deny)

    def to_dict(self) -> JsonDict:
        return {
            "allow_patterns": list(self.allow_patterns),
            "deny_patterns": list(self.deny_patterns),
            "directories": list(self.directories),
            "entrypoint": self.entrypoint,
            "files": [item.to_dict() for item in self.files],
            "options": thaw_json(self.options),
            "required_files": list(self.required_files),
            "root": self.root.to_dict(),
            "schema": FILE_CANDIDATE_MATERIALIZATION_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FileCandidateMaterialization":
        _exact_keys(
            payload,
            {
                "allow_patterns",
                "deny_patterns",
                "directories",
                "entrypoint",
                "files",
                "options",
                "required_files",
                "root",
                "schema",
            },
            "file candidate materialization",
        )
        if payload["schema"] != FILE_CANDIDATE_MATERIALIZATION_SCHEMA:
            raise ValueError("file candidate materialization schema is unsupported.")
        for name in (
            "allow_patterns",
            "deny_patterns",
            "directories",
            "files",
            "required_files",
        ):
            if not isinstance(payload[name], list):
                raise TypeError(f"file candidate materialization {name} must be a list.")
        result = cls(
            root=ScopePath.from_dict(payload["root"]),
            directories=tuple(payload["directories"]),
            files=tuple(
                FileCandidateManifestEntry.from_dict(item)
                for item in payload["files"]
            ),
            entrypoint=payload["entrypoint"],
            options=payload["options"],
            required_files=tuple(payload["required_files"]),
            allow_patterns=tuple(payload["allow_patterns"]),
            deny_patterns=tuple(payload["deny_patterns"]),
        )
        _canonical_round_trip(result, payload, "file candidate materialization")
        return result


@dataclass(frozen=True, order=True)
class ProjectionScopeSource:
    """A path inside one named immutable projection."""

    projection_name: str
    relative_path: str = "."

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "projection_name", _scope_name(self.projection_name, "projection name")
        )
        object.__setattr__(
            self,
            "relative_path",
            _portable_root_or_path(self.relative_path, "projection source path"),
        )

    def to_dict(self) -> JsonDict:
        return {
            "kind": "projection",
            "path": self.relative_path,
            "projection_name": self.projection_name,
            "schema": PROJECTION_SCOPE_SOURCE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionScopeSource":
        _exact_keys(
            payload,
            {"kind", "path", "projection_name", "schema"},
            "projection scope source",
        )
        if (
            payload["schema"] != PROJECTION_SCOPE_SOURCE_SCHEMA
            or payload["kind"] != "projection"
        ):
            raise ValueError("projection scope source schema/kind is unsupported.")
        result = cls(payload["projection_name"], payload["path"])
        _canonical_round_trip(result, payload, "projection scope source")
        return result


@dataclass(frozen=True, order=True)
class VolumeScopeSource:
    """The root of one named writable-volume requirement."""

    volume_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "volume_name", _scope_name(self.volume_name, "volume name")
        )

    def to_dict(self) -> JsonDict:
        return {
            "kind": "volume",
            "schema": VOLUME_SCOPE_SOURCE_SCHEMA,
            "volume_name": self.volume_name,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VolumeScopeSource":
        _exact_keys(payload, {"kind", "schema", "volume_name"}, "volume scope source")
        if payload["schema"] != VOLUME_SCOPE_SOURCE_SCHEMA or payload["kind"] != "volume":
            raise ValueError("volume scope source schema/kind is unsupported.")
        result = cls(payload["volume_name"])
        _canonical_round_trip(result, payload, "volume scope source")
        return result


@dataclass(frozen=True)
class LayeredVolumeScopeSource:
    """A fresh writable upper composed with ordered immutable lower layers.

    The record is transport-neutral.  A provider may realize the composition
    with an overlay, reflinks, or a verified physical fallback without changing
    the portable attempt identity.
    """

    volume_name: str
    lower_layers: Tuple[ProjectedInputLayer, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "volume_name", _scope_name(self.volume_name, "volume name")
        )
        if isinstance(self.lower_layers, (str, bytes)) or not isinstance(
            self.lower_layers, Sequence
        ):
            raise TypeError("layered volume lower_layers must be a sequence.")
        raw_layers = tuple(self.lower_layers)
        if not raw_layers or any(
            not isinstance(item, ProjectedInputLayer) for item in raw_layers
        ):
            raise TypeError(
                "layered volume lower_layers must contain ProjectedInputLayer values."
            )
        if len(raw_layers) > MAX_ATTEMPT_INPUT_LAYERS:
            raise ValueError("layered volume contains too many lower layers.")
        layers = tuple(sorted(raw_layers, key=lambda item: item.precedence))
        if len(set(layers)) != len(layers) or tuple(
            item.precedence for item in layers
        ) != tuple(range(len(layers))):
            raise ValueError(
                "layered volume lower_layers must have unique contiguous precedence."
            )
        object.__setattr__(self, "lower_layers", layers)

    def to_dict(self) -> JsonDict:
        return {
            "kind": "layered-volume",
            "lower_layers": [item.to_dict() for item in self.lower_layers],
            "schema": LAYERED_VOLUME_SCOPE_SOURCE_SCHEMA,
            "volume_name": self.volume_name,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LayeredVolumeScopeSource":
        _exact_keys(
            payload,
            {"kind", "lower_layers", "schema", "volume_name"},
            "layered volume scope source",
        )
        if (
            payload["schema"] != LAYERED_VOLUME_SCOPE_SOURCE_SCHEMA
            or payload["kind"] != "layered-volume"
            or not isinstance(payload["lower_layers"], list)
        ):
            raise ValueError(
                "layered volume scope source schema/kind is unsupported."
            )
        result = cls(
            payload["volume_name"],
            tuple(
                ProjectedInputLayer.from_dict(item)
                for item in payload["lower_layers"]
            ),
        )
        _canonical_round_trip(result, payload, "layered volume scope source")
        return result


ScopeSource = ProjectionScopeSource | VolumeScopeSource | LayeredVolumeScopeSource


def _scope_source_from_dict(payload: Mapping[str, Any]) -> ScopeSource:
    if not isinstance(payload, Mapping):
        raise TypeError("runtime scope source must be a mapping.")
    kind = payload.get("kind")
    if kind == "projection":
        return ProjectionScopeSource.from_dict(payload)
    if kind == "volume":
        return VolumeScopeSource.from_dict(payload)
    if kind == "layered-volume":
        return LayeredVolumeScopeSource.from_dict(payload)
    raise ValueError("runtime scope source kind is unsupported.")


@dataclass(frozen=True, order=True)
class PortableRuntimeScope:
    """One fixed logical path backed by a typed projection or volume source."""

    name: str
    logical_path: str
    access: str
    source: ScopeSource

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _scope_name(self.name))
        object.__setattr__(
            self,
            "logical_path",
            _logical_absolute_path(self.logical_path, "runtime scope logical_path"),
        )
        if self.access not in _ACCESS_VALUES:
            raise ValueError("runtime scope access is unsupported.")
        if not isinstance(
            self.source,
            (ProjectionScopeSource, VolumeScopeSource, LayeredVolumeScopeSource),
        ):
            raise TypeError("runtime scope source must be a typed scope source.")
        if isinstance(self.source, ProjectionScopeSource) and self.access != "read":
            raise ValueError("projection runtime scopes must be read-only.")
        if isinstance(
            self.source, (VolumeScopeSource, LayeredVolumeScopeSource)
        ) and self.access != "read-write":
            raise ValueError("volume runtime scopes must be writable.")
        if isinstance(self.source, LayeredVolumeScopeSource) and any(
            item.scope != self.name for item in self.source.lower_layers
        ):
            raise ValueError(
                "layered volume lower layers must target their runtime scope."
            )

    @property
    def source_kind(self) -> str:
        if isinstance(self.source, ProjectionScopeSource):
            return "projection"
        return "layered-volume" if isinstance(
            self.source, LayeredVolumeScopeSource
        ) else "volume"

    def to_dict(self) -> JsonDict:
        return {
            "access": self.access,
            "logical_path": self.logical_path,
            "name": self.name,
            "schema": PORTABLE_RUNTIME_SCOPE_SCHEMA,
            "source": self.source.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortableRuntimeScope":
        _exact_keys(
            payload,
            {"access", "logical_path", "name", "schema", "source"},
            "portable runtime scope",
        )
        if payload["schema"] != PORTABLE_RUNTIME_SCOPE_SCHEMA:
            raise ValueError("portable runtime scope schema is unsupported.")
        result = cls(
            name=payload["name"],
            logical_path=payload["logical_path"],
            access=payload["access"],
            source=_scope_source_from_dict(payload["source"]),
        )
        _canonical_round_trip(result, payload, "portable runtime scope")
        return result


@dataclass(frozen=True)
class PythonCallableEntrypoint:
    """Python callable selected from one logical read-only scope."""

    scope: str
    module: str
    attribute: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _scope_name(self.scope))
        module = required_text(self.module, "Python entrypoint module", max_bytes=512)
        if any(not part.isidentifier() for part in module.split(".")):
            raise ValueError("Python entrypoint module must contain identifiers.")
        attribute = required_text(
            self.attribute, "Python entrypoint attribute", max_bytes=256
        )
        if not attribute.isidentifier():
            raise ValueError("Python entrypoint attribute must be an identifier.")

    def to_dict(self) -> JsonDict:
        return {
            "attribute": self.attribute,
            "module": self.module,
            "schema": PYTHON_CALLABLE_ENTRYPOINT_SCHEMA,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PythonCallableEntrypoint":
        _exact_keys(
            payload,
            {"attribute", "module", "schema", "scope"},
            "Python callable entrypoint",
        )
        if payload["schema"] != PYTHON_CALLABLE_ENTRYPOINT_SCHEMA:
            raise ValueError("Python callable entrypoint schema is unsupported.")
        result = cls(payload["scope"], payload["module"], payload["attribute"])
        _canonical_round_trip(result, payload, "Python callable entrypoint")
        return result


@dataclass(frozen=True)
class RuntimeProviderRequirement:
    """Exact process-provider compatibility required by the v1 plan."""

    kind: str
    portability: str
    platform: str
    builder_fingerprint: str

    def __post_init__(self) -> None:
        if self.kind != "process":
            raise ValueError("the first runtime binding slice supports process only.")
        if self.portability != "provider-scoped":
            raise ValueError("the v1 runtime requirement must be provider-scoped.")
        object.__setattr__(
            self, "platform", _provider_token(self.platform, "provider platform")
        )
        lower_hex_digest(self.builder_fingerprint, "provider builder fingerprint")

    def to_dict(self) -> JsonDict:
        return {
            "builder_fingerprint": self.builder_fingerprint,
            "kind": self.kind,
            "platform": self.platform,
            "portability": self.portability,
            "schema": RUNTIME_PROVIDER_REQUIREMENT_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeProviderRequirement":
        _exact_keys(
            payload,
            {"builder_fingerprint", "kind", "platform", "portability", "schema"},
            "runtime provider requirement",
        )
        if payload["schema"] != RUNTIME_PROVIDER_REQUIREMENT_SCHEMA:
            raise ValueError("runtime provider requirement schema is unsupported.")
        result = cls(
            payload["kind"],
            payload["portability"],
            payload["platform"],
            payload["builder_fingerprint"],
        )
        _canonical_round_trip(result, payload, "runtime provider requirement")
        return result


@dataclass(frozen=True, order=True)
class WritableVolumeRequirement:
    """Portable lifetime, bound, and minimum enforcement requirement."""

    name: str
    quota: FilesystemQuota
    policy: str = "ephemeral"
    quota_enforcement: str = "advisory"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _scope_name(self.name, "volume name"))
        if not isinstance(self.quota, FilesystemQuota):
            raise TypeError("volume quota must be FilesystemQuota.")
        if self.policy != "ephemeral":
            raise ValueError("the v1 runtime plan supports ephemeral volumes only.")
        if self.quota_enforcement not in _QUOTA_ENFORCEMENTS:
            raise ValueError("volume quota enforcement is unsupported.")

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "policy": self.policy,
            "quota": self.quota.to_dict(),
            "quota_enforcement": self.quota_enforcement,
            "schema": WRITABLE_VOLUME_REQUIREMENT_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WritableVolumeRequirement":
        _exact_keys(
            payload,
            {"name", "policy", "quota", "quota_enforcement", "schema"},
            "volume requirement",
        )
        if payload["schema"] != WRITABLE_VOLUME_REQUIREMENT_SCHEMA:
            raise ValueError("volume requirement schema is unsupported.")
        result = cls(
            name=payload["name"],
            quota=FilesystemQuota.from_dict(payload["quota"]),
            policy=payload["policy"],
            quota_enforcement=payload["quota_enforcement"],
        )
        _canonical_round_trip(result, payload, "volume requirement")
        return result


@dataclass(frozen=True)
class RuntimeResourceRequest:
    """Resources the provider must admit for this invocation."""

    cpu: int
    memory_gib: int
    gpu: int

    def __post_init__(self) -> None:
        _positive_int(self.cpu, "runtime CPU request")
        _positive_int(self.memory_gib, "runtime memory request")
        _nonnegative_int(self.gpu, "runtime GPU request")

    def to_dict(self) -> JsonDict:
        return {
            "cpu": self.cpu,
            "gpu": self.gpu,
            "memory_gib": self.memory_gib,
            "schema": RUNTIME_RESOURCE_REQUEST_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeResourceRequest":
        _exact_keys(payload, {"cpu", "gpu", "memory_gib", "schema"}, "resources")
        if payload["schema"] != RUNTIME_RESOURCE_REQUEST_SCHEMA:
            raise ValueError("runtime resource request schema is unsupported.")
        result = cls(payload["cpu"], payload["memory_gib"], payload["gpu"])
        _canonical_round_trip(result, payload, "runtime resource request")
        return result


@dataclass(frozen=True)
class PortableAttemptRuntimeSpec:
    """Provider-neutral invocation plan for one exact evaluation."""

    run_definition_digest: str
    evaluation_spec_digest: str
    environment_revision_digest: str
    prepared_runtime_digest: str
    provider: RuntimeProviderRequirement
    projection_name: str
    projection_spec: ProjectionSpec
    writable_volumes: Tuple[WritableVolumeRequirement, ...]
    scopes: Tuple[PortableRuntimeScope, ...]
    entrypoint: PythonCallableEntrypoint
    python_import_roots: Tuple[ScopePath, ...]
    evaluator_settings: Mapping[str, Any]
    file_materialization: FileCandidateMaterialization | None
    declared_metric_names: Tuple[str, ...]
    workdir: ScopePath
    resources: RuntimeResourceRequest
    read_only_scope_enforcement: str = "advisory"
    timeout_seconds: float | None = None
    requested_network_policy: str = "disabled"
    cleanup_policy: str = "always"

    def __post_init__(self) -> None:
        lower_hex_digest(self.run_definition_digest, "run definition digest")
        _sha256_ref(self.evaluation_spec_digest, "evaluation spec digest")
        lower_hex_digest(self.environment_revision_digest, "environment revision digest")
        lower_hex_digest(self.prepared_runtime_digest, "prepared runtime digest")
        if not isinstance(self.provider, RuntimeProviderRequirement):
            raise TypeError("provider must be a RuntimeProviderRequirement.")
        projection_name = _scope_name(self.projection_name, "projection name")
        object.__setattr__(self, "projection_name", projection_name)
        if not isinstance(self.projection_spec, ProjectionSpec):
            raise TypeError("projection_spec must be a ProjectionSpec.")
        _safe_identifier(self.projection_spec.owner_id, "projection owner id")
        if not self.projection_spec.mappings or len(self.projection_spec.mappings) > 3:
            raise ValueError(
                "the retained runtime requires one to three projection mappings."
            )
        mapping_destinations = tuple(
            item.destination for item in self.projection_spec.mappings
        )
        if mapping_destinations not in {
            (ENVIRONMENT_PROJECTION_PARTITION,),
            (
                ENVIRONMENT_PROJECTION_PARTITION,
                ENVIRONMENT_PREPARED_PYTHON_PARTITION,
            ),
            (
                ENVIRONMENT_PROJECTION_PARTITION,
                CANDIDATE_PROJECTION_PARTITION,
            ),
            (
                ENVIRONMENT_PROJECTION_PARTITION,
                ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                CANDIDATE_PROJECTION_PARTITION,
            ),
        }:
            raise ValueError(
                "runtime projection mappings use unsupported partitions or order."
            )

        if isinstance(self.writable_volumes, (str, bytes)) or not isinstance(
            self.writable_volumes, Sequence
        ):
            raise TypeError("writable_volumes must be a sequence.")
        raw_volumes = tuple(self.writable_volumes)
        if any(not isinstance(item, WritableVolumeRequirement) for item in raw_volumes):
            raise TypeError("writable_volumes must contain volume requirements.")
        volumes = tuple(sorted(raw_volumes, key=lambda item: item.name))
        if not volumes or len(volumes) > _MAX_VOLUME_REQUIREMENTS:
            raise ValueError("writable volume requirements count is unsupported.")
        if len({item.name for item in volumes}) != len(volumes):
            raise ValueError("writable volume requirement names must be unique.")

        if isinstance(self.scopes, (str, bytes)) or not isinstance(self.scopes, Sequence):
            raise TypeError("scopes must be a sequence.")
        raw_scopes = tuple(self.scopes)
        if any(not isinstance(item, PortableRuntimeScope) for item in raw_scopes):
            raise TypeError("scopes must contain PortableRuntimeScope values.")
        scopes = tuple(sorted(raw_scopes, key=lambda item: item.name))
        if not scopes or len(scopes) > _MAX_SCOPES:
            raise ValueError("runtime scope count is unsupported.")
        if len({item.name for item in scopes}) != len(scopes):
            raise ValueError("runtime scope names must be unique.")
        if len({item.logical_path for item in scopes}) != len(scopes):
            raise ValueError("runtime logical paths must be unique.")
        for index, first in enumerate(scopes):
            for second in scopes[index + 1 :]:
                if _paths_overlap(first.logical_path, second.logical_path):
                    raise ValueError("runtime logical paths must not overlap.")

        projection_scopes = {
            item.name: item
            for item in scopes
            if isinstance(item.source, ProjectionScopeSource)
        }
        mappings_by_partition = {
            item.destination: item for item in self.projection_spec.mappings
        }
        has_prepared_python = (
            ENVIRONMENT_PREPARED_PYTHON_PARTITION in mappings_by_partition
        )
        expected_projection_scopes = {ENVIRONMENT_SOURCE_SCOPE}
        if has_prepared_python:
            expected_projection_scopes.add(ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        if (
            set(projection_scopes) != expected_projection_scopes
            or any(
                item.source.projection_name != projection_name
                for item in projection_scopes.values()
            )
            or projection_scopes[
                ENVIRONMENT_SOURCE_SCOPE
            ].source.relative_path != ENVIRONMENT_PROJECTION_PARTITION
            or (
                has_prepared_python
                and projection_scopes[
                    ENVIRONMENT_PREPARED_PYTHON_SCOPE
                ].source.relative_path
                != ENVIRONMENT_PREPARED_PYTHON_PARTITION
            )
        ):
            raise ValueError("projection scopes differ from the declared projection.")
        expected_source_subpaths = {
            ENVIRONMENT_PROJECTION_PARTITION: ".",
            ENVIRONMENT_PREPARED_PYTHON_PARTITION: "site-packages",
            CANDIDATE_PROJECTION_PARTITION: ".",
        }
        if any(
            item.source_subpath != expected_source_subpaths[item.destination]
            for item in self.projection_spec.mappings
        ):
            raise ValueError(
                "runtime projection partitions must map complete immutable trees, "
                "except the exact prepared Python subtree."
            )
        for item in projection_scopes.values():
            mapping = mappings_by_partition.get(item.source.relative_path)
            if mapping is None:
                raise ValueError(
                    "projection scope does not name an immutable input partition."
                )
        volume_scopes = {
            item.name: item
            for item in scopes
            if isinstance(
                item.source, (VolumeScopeSource, LayeredVolumeScopeSource)
            )
        }
        requirement_names = {item.name for item in volumes}
        if (
            {item.source.volume_name for item in volume_scopes.values()}
            != requirement_names
            or len(volume_scopes) != len(requirement_names)
        ):
            raise ValueError("volume scopes differ from writable volume requirements.")
        referenced_partitions = {
            item.source.relative_path for item in projection_scopes.values()
        }
        for scope in volume_scopes.values():
            if isinstance(scope.source, LayeredVolumeScopeSource):
                for layer in scope.source.lower_layers:
                    if (
                        layer.scope != scope.name
                        or layer.projection_name != projection_name
                    ):
                        raise ValueError(
                            "layered input names another runtime scope or projection."
                        )
                    mapping = mappings_by_partition.get(layer.projection_subpath)
                    if mapping is None or mapping.snapshot_ref != layer.snapshot_ref:
                        raise ValueError(
                            "layered input is absent from its immutable projection partition."
                        )
                    referenced_partitions.add(layer.projection_subpath)
        if referenced_partitions != set(mappings_by_partition):
            raise ValueError("runtime projection contains an unused input partition.")

        if not isinstance(self.entrypoint, PythonCallableEntrypoint):
            raise TypeError("entrypoint must be a PythonCallableEntrypoint.")
        if self.entrypoint.scope != ENVIRONMENT_SOURCE_SCOPE:
            raise ValueError("entrypoint must remain in the environment source scope.")
        if isinstance(self.python_import_roots, (str, bytes)) or not isinstance(
            self.python_import_roots, Sequence
        ):
            raise TypeError("python_import_roots must be a sequence.")
        roots = tuple(self.python_import_roots)
        if not roots or len(roots) > _MAX_IMPORT_ROOTS:
            raise ValueError("Python import-root count is unsupported.")
        if any(not isinstance(item, ScopePath) for item in roots):
            raise TypeError("python_import_roots must contain ScopePath values.")
        if len(set(roots)) != len(roots):
            raise ValueError("python_import_roots must not contain duplicates.")
        if any(item.scope not in projection_scopes for item in roots):
            raise ValueError("Python import roots must use immutable projection scopes.")
        source_roots = tuple(
            item for item in roots if item.scope == ENVIRONMENT_SOURCE_SCOPE
        )
        prepared_roots = tuple(
            item
            for item in roots
            if item.scope == ENVIRONMENT_PREPARED_PYTHON_SCOPE
        )
        if not source_roots:
            raise ValueError("Python import roots must include environment source.")
        if has_prepared_python:
            if (
                prepared_roots
                != (ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "."),)
                or roots[-1] != prepared_roots[0]
            ):
                raise ValueError(
                    "prepared Python dependencies must be one final root scope."
                )
        elif prepared_roots:
            raise ValueError(
                "prepared Python import root is absent from the projection."
            )

        if not isinstance(self.evaluator_settings, Mapping):
            raise TypeError("evaluator_settings must be a mapping.")
        settings = freeze_json(self.evaluator_settings, label="evaluator settings")
        if not isinstance(settings, Mapping):  # pragma: no cover - guarded above
            raise TypeError("evaluator_settings must be a mapping.")
        materialization = self.file_materialization
        if materialization is not None and not isinstance(
            materialization, FileCandidateMaterialization
        ):
            raise TypeError(
                "file_materialization must be a FileCandidateMaterialization or None."
            )
        layered_trial = volume_scopes.get(TRIAL_SCOPE)
        if materialization is not None:
            if layered_trial is None or not isinstance(
                layered_trial.source, LayeredVolumeScopeSource
            ):
                raise ValueError(
                    "file candidate materialization requires a layered trial scope."
                )
            layers = layered_trial.source.lower_layers
            replace_layers = tuple(
                item for item in layers if item.collision_policy == "replace"
            )
            if (
                len(replace_layers) != 1
                or replace_layers[0] != layers[-1]
                or replace_layers[0].projection_subpath
                != CANDIDATE_PROJECTION_PARTITION
                or replace_layers[0].destination_subpath
                != materialization.root.relative_path
                or any(
                    item.collision_policy != "identical"
                    or item.projection_subpath
                    != ENVIRONMENT_PROJECTION_PARTITION
                    for item in layers[:-1]
                )
            ):
                raise ValueError(
                    "file candidate materialization differs from its final replace layer."
                )
        else:
            expected_destinations = (ENVIRONMENT_PROJECTION_PARTITION,)
            if has_prepared_python:
                expected_destinations += (ENVIRONMENT_PREPARED_PYTHON_PARTITION,)
            if mapping_destinations != expected_destinations or any(
                isinstance(item.source, LayeredVolumeScopeSource)
                and any(
                    layer.collision_policy != "identical"
                    or layer.projection_subpath
                    != ENVIRONMENT_PROJECTION_PARTITION
                    for layer in item.source.lower_layers
                )
                for item in volume_scopes.values()
            ):
                raise ValueError(
                    "parameter input layers must alias only the environment partition."
                )
        if isinstance(self.declared_metric_names, (str, bytes)) or not isinstance(
            self.declared_metric_names, Sequence
        ):
            raise TypeError("declared_metric_names must be a sequence.")
        metrics = tuple(
            required_text(item, "declared metric name", max_bytes=256)
            for item in self.declared_metric_names
        )
        if not metrics or len(metrics) > _MAX_METRICS or len(set(metrics)) != len(metrics):
            raise ValueError("declared metric names must be nonempty and unique.")
        if not isinstance(self.workdir, ScopePath):
            raise TypeError("workdir must be a ScopePath.")
        if self.workdir.scope not in volume_scopes:
            raise ValueError("runtime workdir must use a writable volume scope.")
        if not isinstance(self.resources, RuntimeResourceRequest):
            raise TypeError("resources must be a RuntimeResourceRequest.")
        if self.read_only_scope_enforcement not in _READ_ONLY_ENFORCEMENTS:
            raise ValueError("read-only scope enforcement is unsupported.")
        timeout = _optional_timeout(self.timeout_seconds, "runtime timeout_seconds")
        if self.requested_network_policy not in _NETWORK_POLICIES:
            raise ValueError("requested network policy is unsupported by v1.")
        if self.cleanup_policy not in _CLEANUP_POLICIES:
            raise ValueError("runtime cleanup policy is unsupported by v1.")

        object.__setattr__(self, "writable_volumes", volumes)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "python_import_roots", roots)
        object.__setattr__(self, "evaluator_settings", settings)
        object.__setattr__(self, "file_materialization", materialization)
        object.__setattr__(self, "declared_metric_names", metrics)
        object.__setattr__(self, "timeout_seconds", timeout)
        _bounded_record(self.to_dict(), "portable attempt runtime spec")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "cleanup_policy": self.cleanup_policy,
            "declared_metric_names": list(self.declared_metric_names),
            "entrypoint": self.entrypoint.to_dict(),
            "environment_revision_digest": self.environment_revision_digest,
            "evaluation_spec_digest": self.evaluation_spec_digest,
            "evaluator_settings": thaw_json(self.evaluator_settings),
            "file_materialization": (
                None
                if self.file_materialization is None
                else self.file_materialization.to_dict()
            ),
            "prepared_runtime_digest": self.prepared_runtime_digest,
            "projection_name": self.projection_name,
            "projection_spec": self.projection_spec.to_dict(),
            "provider": self.provider.to_dict(),
            "python_import_roots": [item.to_dict() for item in self.python_import_roots],
            "read_only_scope_enforcement": self.read_only_scope_enforcement,
            "requested_network_policy": self.requested_network_policy,
            "resources": self.resources.to_dict(),
            "run_definition_digest": self.run_definition_digest,
            "schema": PORTABLE_ATTEMPT_RUNTIME_SPEC_SCHEMA,
            "scopes": [item.to_dict() for item in self.scopes],
            "timeout_seconds": self.timeout_seconds,
            "workdir": self.workdir.to_dict(),
            "writable_volumes": [item.to_dict() for item in self.writable_volumes],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortableAttemptRuntimeSpec":
        _exact_keys(
            payload,
            {
                "cleanup_policy",
                "declared_metric_names",
                "entrypoint",
                "environment_revision_digest",
                "evaluation_spec_digest",
                "evaluator_settings",
                "file_materialization",
                "prepared_runtime_digest",
                "projection_name",
                "projection_spec",
                "provider",
                "python_import_roots",
                "read_only_scope_enforcement",
                "requested_network_policy",
                "resources",
                "run_definition_digest",
                "schema",
                "scopes",
                "timeout_seconds",
                "workdir",
                "writable_volumes",
            },
            "portable attempt runtime spec",
        )
        if payload["schema"] != PORTABLE_ATTEMPT_RUNTIME_SPEC_SCHEMA:
            raise ValueError("portable attempt runtime spec schema is unsupported.")
        for name, limit in (
            ("scopes", _MAX_SCOPES),
            ("python_import_roots", _MAX_IMPORT_ROOTS),
            ("declared_metric_names", _MAX_METRICS),
            ("writable_volumes", _MAX_VOLUME_REQUIREMENTS),
        ):
            if not isinstance(payload[name], list) or len(payload[name]) > limit:
                raise TypeError(f"portable attempt runtime {name} must be a bounded list.")
        result = cls(
            run_definition_digest=payload["run_definition_digest"],
            evaluation_spec_digest=payload["evaluation_spec_digest"],
            environment_revision_digest=payload["environment_revision_digest"],
            prepared_runtime_digest=payload["prepared_runtime_digest"],
            provider=RuntimeProviderRequirement.from_dict(payload["provider"]),
            projection_name=payload["projection_name"],
            projection_spec=_projection_spec_from_dict(payload["projection_spec"]),
            writable_volumes=tuple(
                WritableVolumeRequirement.from_dict(item)
                for item in payload["writable_volumes"]
            ),
            scopes=tuple(PortableRuntimeScope.from_dict(item) for item in payload["scopes"]),
            entrypoint=PythonCallableEntrypoint.from_dict(payload["entrypoint"]),
            python_import_roots=tuple(
                ScopePath.from_dict(item) for item in payload["python_import_roots"]
            ),
            evaluator_settings=payload["evaluator_settings"],
            file_materialization=(
                None
                if payload["file_materialization"] is None
                else FileCandidateMaterialization.from_dict(
                    payload["file_materialization"]
                )
            ),
            declared_metric_names=tuple(payload["declared_metric_names"]),
            workdir=ScopePath.from_dict(payload["workdir"]),
            resources=RuntimeResourceRequest.from_dict(payload["resources"]),
            read_only_scope_enforcement=payload["read_only_scope_enforcement"],
            timeout_seconds=payload["timeout_seconds"],
            requested_network_policy=payload["requested_network_policy"],
            cleanup_policy=payload["cleanup_policy"],
        )
        _canonical_round_trip(result, payload, "portable attempt runtime spec")
        return result


@dataclass(frozen=True)
class ProjectionBindingEvidence:
    """Portable facts from one verified immutable projection realization."""

    logical_name: str
    access_enforcement: str
    spec_digest: str
    plan_digest: str
    source_snapshots: Tuple[SnapshotRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "logical_name", _scope_name(self.logical_name, "projection name")
        )
        if self.access_enforcement not in _READ_ONLY_ENFORCEMENTS:
            raise ValueError("projection access enforcement is unsupported.")
        lower_hex_digest(self.spec_digest, "projection spec digest")
        lower_hex_digest(self.plan_digest, "projection plan digest")
        raw_snapshots = tuple(self.source_snapshots)
        if not raw_snapshots or len(raw_snapshots) > _MAX_EVIDENCE_ITEMS:
            raise ValueError("projection source snapshot count is unsupported.")
        if any(not isinstance(item, SnapshotRef) for item in raw_snapshots):
            raise TypeError("projection source_snapshots must contain SnapshotRef values.")
        snapshots = tuple(sorted(set(raw_snapshots), key=str))
        object.__setattr__(self, "source_snapshots", snapshots)

    def to_dict(self) -> JsonDict:
        return {
            "logical_name": self.logical_name,
            "plan_digest": self.plan_digest,
            "access_enforcement": self.access_enforcement,
            "schema": PROJECTION_BINDING_EVIDENCE_SCHEMA,
            "source_snapshots": [str(item) for item in self.source_snapshots],
            "spec_digest": self.spec_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionBindingEvidence":
        _exact_keys(
            payload,
            {
                "access_enforcement",
                "logical_name",
                "plan_digest",
                "schema",
                "source_snapshots",
                "spec_digest",
            },
            "projection binding evidence",
        )
        if payload["schema"] != PROJECTION_BINDING_EVIDENCE_SCHEMA:
            raise ValueError("projection binding evidence schema is unsupported.")
        if (
            not isinstance(payload["source_snapshots"], list)
            or len(payload["source_snapshots"]) > _MAX_EVIDENCE_ITEMS
        ):
            raise TypeError("projection source_snapshots must be a bounded list.")
        result = cls(
            logical_name=payload["logical_name"],
            access_enforcement=payload["access_enforcement"],
            spec_digest=payload["spec_digest"],
            plan_digest=payload["plan_digest"],
            source_snapshots=tuple(
                SnapshotRef.parse(item) for item in payload["source_snapshots"]
            ),
        )
        _canonical_round_trip(result, payload, "projection binding evidence")
        return result


@dataclass(frozen=True, order=True)
class WritableVolumeEvidence:
    """Portable effective volume semantics; never a provider or local id."""

    logical_name: str
    quota: FilesystemQuota
    policy: str = "ephemeral"
    quota_enforcement: str = "advisory"

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_name", _scope_name(self.logical_name))
        if not isinstance(self.quota, FilesystemQuota):
            raise TypeError("writable volume evidence quota must be FilesystemQuota.")
        if self.policy != "ephemeral":
            raise ValueError("the v1 binding evidence supports ephemeral volumes only.")
        if self.quota_enforcement not in _QUOTA_ENFORCEMENTS:
            raise ValueError("writable volume quota enforcement is unsupported.")

    def to_dict(self) -> JsonDict:
        return {
            "logical_name": self.logical_name,
            "policy": self.policy,
            "quota": self.quota.to_dict(),
            "quota_enforcement": self.quota_enforcement,
            "schema": WRITABLE_VOLUME_EVIDENCE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WritableVolumeEvidence":
        _exact_keys(
            payload,
            {"logical_name", "policy", "quota", "quota_enforcement", "schema"},
            "writable volume evidence",
        )
        if payload["schema"] != WRITABLE_VOLUME_EVIDENCE_SCHEMA:
            raise ValueError("writable volume evidence schema is unsupported.")
        result = cls(
            logical_name=payload["logical_name"],
            quota=FilesystemQuota.from_dict(payload["quota"]),
            policy=payload["policy"],
            quota_enforcement=payload["quota_enforcement"],
        )
        _canonical_round_trip(result, payload, "writable volume evidence")
        return result


@dataclass(frozen=True)
class ExecutionProviderFacts:
    """Portable provider identity and its actual native-process enforcement."""

    kind: str
    sandbox_enforcement: str
    platform: str
    builder_fingerprint: str

    def __post_init__(self) -> None:
        if self.kind != "process":
            raise ValueError("the first execution provider must be process.")
        if self.sandbox_enforcement != "advisory":
            raise ValueError("native process sandbox enforcement must be advisory.")
        object.__setattr__(
            self, "platform", _provider_token(self.platform, "execution platform")
        )
        lower_hex_digest(self.builder_fingerprint, "execution builder fingerprint")

    def to_dict(self) -> JsonDict:
        return {
            "builder_fingerprint": self.builder_fingerprint,
            "kind": self.kind,
            "platform": self.platform,
            "sandbox_enforcement": self.sandbox_enforcement,
            "schema": EXECUTION_PROVIDER_FACTS_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionProviderFacts":
        _exact_keys(
            payload,
            {
                "builder_fingerprint",
                "kind",
                "platform",
                "sandbox_enforcement",
                "schema",
            },
            "execution provider facts",
        )
        if payload["schema"] != EXECUTION_PROVIDER_FACTS_SCHEMA:
            raise ValueError("execution provider facts schema is unsupported.")
        result = cls(
            payload["kind"],
            payload["sandbox_enforcement"],
            payload["platform"],
            payload["builder_fingerprint"],
        )
        _canonical_round_trip(result, payload, "execution provider facts")
        return result


@dataclass(frozen=True)
class ExecutionBindingEvidence:
    """Canonical evidence stable across equivalent physical realizations.

    A Realm binding record associates this semantic evidence with one attempt,
    binding id, projection lease, and volume usage leases.  Those operational
    handles are deliberately absent here.
    """

    portable_spec_digest: str
    provider: ExecutionProviderFacts
    logical_map: Tuple[PortableRuntimeScope, ...]
    projections: Tuple[ProjectionBindingEvidence, ...]
    writable_volumes: Tuple[WritableVolumeEvidence, ...]
    requested_network_policy: str
    cleanup_policy: str

    def __post_init__(self) -> None:
        lower_hex_digest(self.portable_spec_digest, "portable runtime spec digest")
        if not isinstance(self.provider, ExecutionProviderFacts):
            raise TypeError("provider must be ExecutionProviderFacts.")
        for value, label in (
            (self.logical_map, "logical_map"),
            (self.projections, "projections"),
            (self.writable_volumes, "writable_volumes"),
        ):
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"{label} must be a sequence.")
        raw_logical_map = tuple(self.logical_map)
        raw_projections = tuple(self.projections)
        raw_volumes = tuple(self.writable_volumes)
        if any(not isinstance(item, PortableRuntimeScope) for item in raw_logical_map):
            raise TypeError("logical_map must contain runtime scopes.")
        if any(not isinstance(item, ProjectionBindingEvidence) for item in raw_projections):
            raise TypeError("projections must contain projection evidence.")
        if any(not isinstance(item, WritableVolumeEvidence) for item in raw_volumes):
            raise TypeError("writable_volumes must contain volume evidence.")
        logical_map = tuple(sorted(raw_logical_map, key=lambda item: item.name))
        projections = tuple(sorted(raw_projections, key=lambda item: item.logical_name))
        volumes = tuple(sorted(raw_volumes, key=lambda item: item.logical_name))
        if not logical_map or len(logical_map) > _MAX_SCOPES:
            raise TypeError("logical_map must contain bounded runtime scopes.")
        if not projections or len(projections) > _MAX_EVIDENCE_ITEMS:
            raise TypeError("projections must contain bounded projection evidence.")
        if len(volumes) > _MAX_EVIDENCE_ITEMS:
            raise TypeError("writable_volumes must contain bounded volume evidence.")
        if len({item.name for item in logical_map}) != len(logical_map) or len(
            {item.logical_path for item in logical_map}
        ) != len(logical_map):
            raise ValueError("execution logical map must have unique names and paths.")
        for index, first in enumerate(logical_map):
            for second in logical_map[index + 1 :]:
                if _paths_overlap(first.logical_path, second.logical_path):
                    raise ValueError("execution logical paths must not overlap.")
        if len({item.logical_name for item in projections}) != len(projections):
            raise ValueError("projection evidence names must be unique.")
        if len({item.logical_name for item in volumes}) != len(volumes):
            raise ValueError("writable volume evidence names must be unique.")
        projection_names = {
            item.source.projection_name
            for item in logical_map
            if isinstance(item.source, ProjectionScopeSource)
        }
        volume_names = {
            item.source.volume_name
            for item in logical_map
            if isinstance(
                item.source, (VolumeScopeSource, LayeredVolumeScopeSource)
            )
        }
        if projection_names != {item.logical_name for item in projections}:
            raise ValueError("projection evidence differs from the logical map.")
        if volume_names != {item.logical_name for item in volumes}:
            raise ValueError("volume evidence differs from the logical map.")
        if self.requested_network_policy not in _NETWORK_POLICIES:
            raise ValueError("requested network policy is unsupported by v1.")
        if self.cleanup_policy not in _CLEANUP_POLICIES:
            raise ValueError("cleanup policy is unsupported by v1.")
        object.__setattr__(self, "logical_map", logical_map)
        object.__setattr__(self, "projections", projections)
        object.__setattr__(self, "writable_volumes", volumes)
        _bounded_record(self.to_dict(), "execution binding evidence")

    @property
    def fingerprint(self) -> str:
        return request_digest(self.to_dict())

    def validate_against(self, spec: PortableAttemptRuntimeSpec) -> None:
        """Check semantic correspondence; this does not authenticate receipts."""

        if not isinstance(spec, PortableAttemptRuntimeSpec):
            raise TypeError("spec must be PortableAttemptRuntimeSpec.")
        if self.portable_spec_digest != spec.digest or self.logical_map != spec.scopes:
            raise ValueError("binding evidence differs from the portable runtime spec.")
        if (
            self.provider.kind != spec.provider.kind
            or self.provider.platform != spec.provider.platform
            or self.provider.builder_fingerprint != spec.provider.builder_fingerprint
        ):
            raise ValueError("execution provider differs from the portable requirement.")
        expected_snapshots = tuple(
            sorted(
                {mapping.snapshot_ref for mapping in spec.projection_spec.mappings},
                key=str,
            )
        )
        if (
            len(self.projections) != 1
            or self.projections[0].logical_name != spec.projection_name
            or self.projections[0].spec_digest != spec.projection_spec.digest
            or self.projections[0].source_snapshots != expected_snapshots
        ):
            raise ValueError("projection evidence differs from the portable plan.")
        if (
            spec.read_only_scope_enforcement == "enforced"
            and self.projections[0].access_enforcement != "enforced"
        ):
            raise ValueError("projection access enforcement is weaker than required.")
        expected_volumes = {
            item.name: item for item in spec.writable_volumes
        }
        actual_volumes = {
            item.logical_name: item for item in self.writable_volumes
        }
        if set(actual_volumes) != set(expected_volumes):
            raise ValueError("writable volume evidence differs from the portable plan.")
        for name, requirement in expected_volumes.items():
            actual = actual_volumes[name]
            if actual.policy != requirement.policy or actual.quota != requirement.quota:
                raise ValueError("writable volume evidence differs from the portable plan.")
            if (
                requirement.quota_enforcement == "enforced"
                and actual.quota_enforcement != "enforced"
            ):
                raise ValueError(
                    "writable volume quota enforcement is weaker than required."
                )
        if (
            self.requested_network_policy != spec.requested_network_policy
            or self.cleanup_policy != spec.cleanup_policy
        ):
            raise ValueError("binding policies differ from the portable plan.")

    @classmethod
    def create(
        cls,
        spec: PortableAttemptRuntimeSpec,
        *,
        provider: ExecutionProviderFacts,
        projections: Sequence[ProjectionBindingEvidence],
        writable_volumes: Sequence[WritableVolumeEvidence],
    ) -> "ExecutionBindingEvidence":
        """Assemble already-verified portable facts and cross-check the plan.

        The future provider binder is responsible for obtaining these values
        from current READY projection and ACTIVE volume receipts tied to the
        exact attempt lease.  Arbitrary caller data is not authority.
        """

        result = cls(
            portable_spec_digest=spec.digest,
            provider=provider,
            logical_map=spec.scopes,
            projections=tuple(projections),
            writable_volumes=tuple(writable_volumes),
            requested_network_policy=spec.requested_network_policy,
            cleanup_policy=spec.cleanup_policy,
        )
        result.validate_against(spec)
        return result

    def to_dict(self) -> JsonDict:
        return {
            "cleanup_policy": self.cleanup_policy,
            "logical_map": [item.to_dict() for item in self.logical_map],
            "portable_spec_digest": self.portable_spec_digest,
            "projections": [item.to_dict() for item in self.projections],
            "provider": self.provider.to_dict(),
            "requested_network_policy": self.requested_network_policy,
            "schema": EXECUTION_BINDING_EVIDENCE_SCHEMA,
            "writable_volumes": [item.to_dict() for item in self.writable_volumes],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionBindingEvidence":
        _exact_keys(
            payload,
            {
                "cleanup_policy",
                "logical_map",
                "portable_spec_digest",
                "projections",
                "provider",
                "requested_network_policy",
                "schema",
                "writable_volumes",
            },
            "execution binding evidence",
        )
        if payload["schema"] != EXECUTION_BINDING_EVIDENCE_SCHEMA:
            raise ValueError("execution binding evidence schema is unsupported.")
        for name, limit in (
            ("logical_map", _MAX_SCOPES),
            ("projections", _MAX_EVIDENCE_ITEMS),
            ("writable_volumes", _MAX_EVIDENCE_ITEMS),
        ):
            if not isinstance(payload[name], list) or len(payload[name]) > limit:
                raise TypeError(f"execution binding evidence {name} must be a bounded list.")
        result = cls(
            portable_spec_digest=payload["portable_spec_digest"],
            provider=ExecutionProviderFacts.from_dict(payload["provider"]),
            logical_map=tuple(
                PortableRuntimeScope.from_dict(item) for item in payload["logical_map"]
            ),
            projections=tuple(
                ProjectionBindingEvidence.from_dict(item)
                for item in payload["projections"]
            ),
            writable_volumes=tuple(
                WritableVolumeEvidence.from_dict(item)
                for item in payload["writable_volumes"]
            ),
            requested_network_policy=payload["requested_network_policy"],
            cleanup_policy=payload["cleanup_policy"],
        )
        _canonical_round_trip(result, payload, "execution binding evidence")
        return result


def _projection_spec_from_dict(payload: Mapping[str, Any]) -> ProjectionSpec:
    _exact_keys(payload, {"format", "mappings", "owner_id", "quota"}, "projection spec")
    if payload["format"] != "optpilot.projection-spec.v1":
        raise ValueError("projection spec format is unsupported.")
    mappings = payload["mappings"]
    quota = payload["quota"]
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= 3:
        raise TypeError(
            "the retained runtime projection must contain one to three mappings."
        )
    _exact_keys(quota, {"max_entries", "max_file_bytes", "max_total_bytes"}, "quota")
    values = []
    for item in mappings:
        _exact_keys(item, {"destination", "snapshot_ref", "source_subpath"}, "tree mapping")
        values.append(
            TreeMapping(
                snapshot_ref=SnapshotRef.parse(item["snapshot_ref"]),
                destination=item["destination"],
                source_subpath=item["source_subpath"],
            )
        )
    result = ProjectionSpec(
        owner_id=payload["owner_id"],
        mappings=tuple(values),
        quota=ProjectionQuota(
            max_entries=quota["max_entries"],
            max_file_bytes=quota["max_file_bytes"],
            max_total_bytes=quota["max_total_bytes"],
        ),
    )
    if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
        raise ValueError("projection spec is not canonical.")
    return result


def _parse_callable(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        _fail("evaluator_callable_invalid", "Evaluator callable must use module:attribute.")
    module, separator, attribute = value.partition(":")
    if (
        not separator
        or not module
        or not attribute
        or any(not item.isidentifier() for item in module.split("."))
        or not attribute.isidentifier()
    ):
        _fail("evaluator_callable_invalid", "Evaluator callable must use module:attribute.")
    return module, attribute


def _config_parent(path: ScopePath) -> ScopePath:
    return ScopePath(path.scope, str(PurePosixPath(path.relative_path).parent))


def _effective_timeout(evaluate: Mapping[str, Any], spec: EvaluationSpec) -> float | None:
    values = []
    for value, label in (
        (evaluate.get("timeoutSeconds"), "environment evaluator timeout"),
        (spec.resource_profile.get("timeoutSeconds"), "attempt resource timeout"),
    ):
        if value is not None:
            try:
                values.append(_optional_timeout(value, label))
            except ValueError as error:
                _fail("runtime_timeout_invalid", str(error))
    return min(item for item in values if item is not None) if values else None


def _runtime_resources(resource_profile: Mapping[str, Any]) -> RuntimeResourceRequest:
    _compile_exact_keys(
        resource_profile,
        {"cpu", "gpu", "memoryGiB", "timeoutSeconds"},
        "evaluation resource profile",
    )
    try:
        return RuntimeResourceRequest(
            cpu=resource_profile["cpu"],
            memory_gib=resource_profile["memoryGiB"],
            gpu=resource_profile["gpu"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeBindingCompileError(
            "resource_profile_unsupported",
            "Evaluation resource requirements are unsupported.",
        ) from error


def _compile_file_materialization(
    *,
    candidate: Mapping[str, Any],
    candidate_contract: Mapping[str, Any],
) -> FileCandidateMaterialization:
    spec = _mapping(candidate.get("spec"), "sealed file candidate spec")
    _compile_exact_keys(
        spec,
        {"directories", "entrypoint", "files", "options", "schema"},
        "sealed file candidate spec",
    )
    if spec.get("schema") != SEALED_FILE_CANDIDATE_SPEC_SCHEMA:
        _fail(
            "candidate_content_invalid",
            "Sealed file candidate spec schema is unsupported.",
        )
    directories = _sequence(spec.get("directories"), "candidate directories")
    raw_files = _sequence(spec.get("files"), "candidate files")
    options = _mapping(spec.get("options"), "candidate options")

    materialization = _mapping(
        candidate_contract.get("materialization"),
        "file candidate materialization contract",
    )
    _compile_exact_keys(
        materialization,
        {"config", "implementation"},
        "file candidate materialization contract",
    )
    if materialization.get("implementation") != "builtin.workspace_bundle":
        _fail(
            "candidate_materialization_unsupported",
            "File candidates require the retained workspace bundle materializer.",
        )
    materialization_config = _mapping(
        materialization.get("config"), "file candidate materialization config"
    )
    if not set(materialization_config) <= {
        "candidateOptions",
        "candidateRoot",
        "entrypoint",
    }:
        _fail(
            "candidate_materialization_unsupported",
            "File candidate materialization config contains unsupported fields.",
        )
    try:
        root = portable_selection(
            materialization_config.get("candidateRoot", "."), "candidate root"
        )
        raw_entrypoint = materialization_config.get("entrypoint")
        entrypoint = (
            None
            if raw_entrypoint is None
            else portable_selection(raw_entrypoint, "candidate entrypoint")
        )
    except (TypeError, ValueError) as error:
        raise RuntimeBindingCompileError(
            "candidate_materialization_unsupported",
            "File candidate materialization paths are invalid.",
        ) from error
    candidate_options = materialization_config.get("candidateOptions", {})
    if (
        not isinstance(candidate_options, Mapping)
        or "candidateRoot" in candidate_options
    ):
        _fail(
            "candidate_materialization_unsupported",
            "File candidate options cannot replace the contract-owned root.",
        )
    expected_options = {"candidateRoot": root, **thaw_json(candidate_options)}
    if (
        not _canonical_json_equal(options, expected_options)
        or spec.get("entrypoint") != entrypoint
    ):
        _fail(
            "candidate_materialization_unsupported",
            "Sealed candidate options differ from the retained materialization contract.",
        )

    validation = _mapping(
        candidate_contract.get("validation"), "file candidate validation contract"
    )
    _compile_exact_keys(
        validation,
        {"config", "implementation"},
        "file candidate validation contract",
    )
    if validation.get("implementation") != "builtin.workspace_policy":
        _fail(
            "candidate_validation_unsupported",
            "File candidates require the retained workspace policy validator.",
        )
    validation_config = _mapping(
        validation.get("config"), "file candidate validation config"
    )
    if not set(validation_config) <= {"allow", "deny", "requiredFiles"}:
        _fail(
            "candidate_validation_unsupported",
            "File candidate validation config contains unsupported fields.",
        )
    try:
        result = FileCandidateMaterialization(
            root=ScopePath(TRIAL_SCOPE, root),
            directories=tuple(directories),
            files=tuple(
                FileCandidateManifestEntry.from_dict(item) for item in raw_files
            ),
            entrypoint=entrypoint,
            options=options,
            required_files=tuple(
                _sequence(
                    validation_config.get("requiredFiles", ()),
                    "required candidate files",
                )
            ),
            allow_patterns=tuple(
                _sequence(
                    validation_config.get("allow", ()),
                    "candidate allow patterns",
                )
            ),
            deny_patterns=tuple(
                _sequence(
                    validation_config.get("deny", ()),
                    "candidate deny patterns",
                )
            ),
        )
    except (RealmIntegrityError, TypeError, ValueError) as error:
        raise RuntimeBindingCompileError(
            "candidate_content_invalid",
            "Sealed file candidate manifest is invalid.",
        ) from error
    return result


def _expected_container_runtime_requirements(
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """The one shape a container environment's contract must carry.

    Defined beside the check that consumes it so the compiler and the binding
    cannot drift apart: the compiler emits this, the contract retains it, and
    the binding re-derives it from the manifest's own settings.
    """

    container: dict[str, Any] = {
        "image": settings.get("container_image_reference"),
        "platform": settings.get("container_platform"),
        "network": settings.get("container_network"),
    }
    if settings.get("container_limits"):
        # Present only when the component raised a limit, mirroring the
        # authoring compile -- an unraised component keeps its exact shape.
        container["limits"] = dict(settings["container_limits"])
    return {
        "type": "container",
        "container": container,
    }


def compile_retained_process_attempt_runtime(
    *,
    owner_id: str,
    run_definition: RunDefinitionManifest,
    evaluation_spec: EvaluationSpec,
    provider: ProcessProviderIdentity,
    candidate_input: CandidateRuntimeInput | None = None,
    container_execution_supported: bool = False,
) -> PortableAttemptRuntimeSpec:
    """Compile one strict retained process invocation without resolving paths.

    ``container_execution_supported`` is an explicit opt-in from the one caller
    that can actually run a container. Every other caller -- operator debug
    jobs, Studio probes -- keeps refusing, so a surface that cannot start a
    container never reports a container study as runnable.
    """

    try:
        _safe_identifier(owner_id, "runtime projection owner id")
    except ValueError as error:
        raise RuntimeBindingCompileError("owner_id_invalid", str(error)) from error
    if not isinstance(run_definition, RunDefinitionManifest):
        raise TypeError("run_definition must be a RunDefinitionManifest.")
    if not isinstance(evaluation_spec, EvaluationSpec):
        raise TypeError("evaluation_spec must be an EvaluationSpec.")
    if not isinstance(provider, ProcessProviderIdentity):
        raise TypeError("provider must be a ProcessProviderIdentity.")
    if candidate_input is not None and not isinstance(
        candidate_input, CandidateRuntimeInput
    ):
        raise TypeError("candidate_input must be a CandidateRuntimeInput or None.")

    closure = run_definition.evaluation_closure
    environment = closure.environment_revision
    runtime = closure.prepared_runtime
    template = closure.evaluation_template
    if (
        environment.compiler_id != RETAINED_PROCESS_STUDY_COMPILER_ID
        or environment.compiler_version != RETAINED_PROCESS_STUDY_COMPILER_VERSION
        or run_definition.compiler_version != STUDY_REALM_COMPILER_VERSION
    ):
        _fail(
            "compiler_unsupported",
            "Run/environment compiler provenance is unsupported by this binding slice.",
        )
    if (
        evaluation_spec.environment_id != environment.environment_id
        or evaluation_spec.environment_revision_digest != environment.digest
        or evaluation_spec.prepared_runtime_digest != runtime.digest
    ):
        _fail(
            "evaluation_anchor_mismatch",
            "Evaluation spec differs from the retained environment/runtime closure.",
        )
    if (
        not _canonical_json_equal(evaluation_spec.objective, template.objective)
        or not _canonical_json_equal(evaluation_spec.resource_profile, template.resource_profile)
        or not _canonical_json_equal(evaluation_spec.sandbox_spec, template.sandbox_spec)
    ):
        _fail(
            "evaluation_policy_mismatch",
            "Evaluation spec policies differ from the retained run template.",
        )
    candidate_format = evaluation_spec.candidate_format
    if environment.candidate_contract.get("format") != candidate_format:
        _fail(
            "candidate_contract_mismatch",
            "Evaluation candidate format differs from the retained environment contract.",
        )
    if candidate_format == "parameters":
        if candidate_input is not None and (
            candidate_input.candidate_format != "parameters"
            or str(candidate_input.candidate_ref) != evaluation_spec.candidate_ref
        ):
            _fail(
                "candidate_content_mismatch",
                "Parameter candidate authority differs from the evaluation candidate.",
            )
    elif candidate_format == "files":
        if (
            candidate_input is None
            or candidate_input.candidate_format != "files"
            or candidate_input.snapshot_ref is None
            or str(candidate_input.candidate_ref) != evaluation_spec.candidate_ref
        ):
            _fail(
                "candidate_content_unavailable",
                "File candidate evaluation requires its exact admitted tree input.",
            )
        expected_candidate_ref = CandidateRef.build(
            candidate_format="files",
            spec=thaw_json(evaluation_spec.candidate["spec"]),
            content_refs=(candidate_input.snapshot_ref,),
        )
        if expected_candidate_ref != candidate_input.candidate_ref:
            _fail(
                "candidate_content_mismatch",
                "File candidate identity differs from its sealed spec and tree.",
            )
    else:
        _fail(
            "candidate_format_unsupported",
            "The retained process runtime supports parameter and file candidates only.",
        )
    for field_name in ("validation", "materialization"):
        declared = environment.candidate_contract.get(field_name, {})
        supplied = evaluation_spec.candidate.get(field_name, {})
        if not isinstance(declared, Mapping) or not _canonical_json_equal(declared, supplied):
            _fail(
                "candidate_contract_mismatch",
                "Evaluation candidate semantics differ from the retained environment contract.",
            )

    is_container = (
        container_execution_supported
        and runtime.runtime_kind == "container"
        and runtime.oci_image_digest is not None
    )
    if not is_container and (
        runtime.runtime_kind != "process" or runtime.oci_image_digest is not None
    ):
        _fail("runtime_kind_unsupported", "The first binding slice supports process only.")
    if is_container:
        # An image names its contents exactly, so the manifest is portable and
        # carries no builder: the platform to check is the image's, and it is
        # checked against the machine at launch, not here.
        if runtime.portability != "portable":
            _fail(
                "runtime_portability_unsupported",
                "A retained container runtime must be portable.",
            )
        if runtime.builder_fingerprint is not None:
            _fail(
                "provider_mismatch",
                "A portable container runtime carries no builder fingerprint.",
            )
    else:
        if runtime.portability != "provider-scoped":
            _fail(
                "runtime_portability_unsupported",
                "The retained process runtime must be provider-scoped.",
            )
        if (
            runtime.platform != provider.platform
            or runtime.builder_fingerprint != provider.builder_fingerprint
        ):
            _fail(
                "provider_mismatch",
                "Current process provider differs from the retained runtime requirement.",
            )
    prepared_layers = tuple(runtime.prepared_layers)
    if len(prepared_layers) > 1:
        _fail(
            "prepared_layers_unsupported",
            "The retained process runtime supports at most one prepared Python layer.",
        )
    prepared_python = prepared_layers[0] if prepared_layers else None
    if prepared_python is not None and (
        prepared_python.scope != ENVIRONMENT_PREPARED_PYTHON_SCOPE
        or prepared_python.source_subpath != "site-packages"
        or prepared_python.destination_subpath != "."
        or prepared_python.precedence != 0
    ):
        _fail(
            "prepared_layers_unsupported",
            "The prepared Python layer must map its sealed site-packages subtree exactly.",
        )
    if len(environment.source_layers) != 1:
        _fail(
            "source_layers_unsupported",
            "The first binding slice requires one retained environment source layer.",
        )
    source = environment.source_layers[0]
    if (
        source.source_subpath != "."
        or source.destination_subpath != "."
        or source.precedence != 0
    ):
        _fail(
            "source_layers_unsupported",
            "The retained environment source must map one complete tree at precedence zero.",
        )
    retained_input_layers = tuple(
        sorted(environment.attempt_input_layers, key=lambda item: item.precedence)
    )
    projection_contract = environment.projection_contract
    try:
        projection_features = package_projection_contract_features(
            projection_contract
        )
    except (TypeError, ValueError) as error:
        raise RuntimeBindingCompileError(
            "attempt_input_contract_unsupported",
            "Retained package projection contract is malformed.",
        ) from error
    if retained_input_layers:
        if (
            "trial_workspace" not in projection_features
            or dict(projection_contract.get("trial_workspace", {}))
            != {
                "collision_policy": "identical-effective-entries-only",
                "destination_scope": TRIAL_SCOPE,
                "source_scope": source.scope,
            }
        ):
            _fail(
                "attempt_input_contract_unsupported",
                "Attempt input layers differ from the retained package projection contract.",
            )
        if (
            tuple(item.precedence for item in retained_input_layers)
            != tuple(range(len(retained_input_layers)))
            or any(
                item.scope != TRIAL_SCOPE
                or item.snapshot_ref != source.snapshot_ref
                for item in retained_input_layers
            )
        ):
            _fail(
                "attempt_input_layers_unsupported",
                "Attempt input layers must be ordered aliases of the retained package snapshot.",
            )
    elif "trial_workspace" in projection_features:
        _fail(
            "attempt_input_contract_unsupported",
            "Trial workspace contract has no retained attempt input layers.",
        )
    if runtime.workdir is None or runtime.workdir != _config_parent(environment.authored_config):
        _fail(
            "runtime_workdir_unsupported",
            "Retained process runtime workdir differs from its authored config directory.",
        )

    settings = _mapping(runtime.runtime_settings, "prepared runtime settings")
    expected_settings_keys = {"import_roots", "schema"}
    if is_container:
        expected_settings_keys = expected_settings_keys | {
            "container_platform",
            "container_image_reference",
            "container_network",
        }
    observed_settings_keys = set(settings)
    if is_container:
        # container_limits appears only when the component raised a limit.
        observed_settings_keys.discard("container_limits")
    if observed_settings_keys != expected_settings_keys or settings.get(
        "schema"
    ) != LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA:
        _fail(
            "runtime_settings_unsupported",
            "Prepared process runtime settings schema is unsupported.",
        )
    if is_container and (
        runtime.platform != settings.get("container_platform")
        or settings.get("container_network") not in ("enabled", "disabled")
        or not isinstance(settings.get("container_image_reference"), str)
    ):
        _fail(
            "runtime_settings_unsupported",
            "Prepared container runtime settings are malformed.",
        )
    raw_roots = _sequence(settings.get("import_roots"), "runtime import roots")
    roots = []
    for item in raw_roots:
        try:
            root = ScopePath.from_dict(item)
        except (RealmIntegrityError, TypeError, ValueError) as error:
            raise RuntimeBindingCompileError(
                "runtime_settings_unsupported",
                "Prepared runtime import roots are invalid.",
            ) from error
        if root.scope == source.scope:
            roots.append(ScopePath(ENVIRONMENT_SOURCE_SCOPE, root.relative_path))
        elif (
            prepared_python is not None
            and root.scope == prepared_python.scope
            and root.relative_path == "."
        ):
            roots.append(ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "."))
        else:
            _fail(
                "runtime_settings_unsupported",
                "Prepared runtime import root uses an undeclared source scope.",
            )
    if not roots or len(roots) > _MAX_IMPORT_ROOTS or len(set(roots)) != len(roots):
        _fail(
            "runtime_settings_unsupported",
            "Prepared runtime import roots must be bounded, nonempty, and unique.",
        )
    prepared_root = ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, ".")
    if not any(item.scope == ENVIRONMENT_SOURCE_SCOPE for item in roots) or (
        prepared_python is not None
        and (roots[-1] != prepared_root or roots.count(prepared_root) != 1)
    ):
        _fail(
            "runtime_settings_unsupported",
            "Prepared dependency imports must follow the retained environment source roots.",
        )

    contract = _mapping(environment.evaluator_contract, "environment evaluator contract")
    _compile_exact_keys(
        contract,
        {
            "access_policy",
            "adapter",
            "mutation_policy",
            "runtime_contract",
            "runtime_requirements",
            "schema",
        },
        "environment evaluator contract",
    )
    adapter = _mapping(contract.get("adapter"), "environment adapter")
    _compile_exact_keys(adapter, {"config", "implementation", "type"}, "environment adapter")
    adapter_config = _mapping(adapter.get("config"), "environment adapter config")
    _compile_exact_keys(
        adapter_config,
        {
            "candidate",
            "context",
            "evaluate",
            "interfaces",
            "metrics",
            "outputFiles",
            "records",
            "workspace",
        },
        "environment adapter config",
    )
    evaluate = _mapping(adapter_config.get("evaluate"), "environment evaluate")
    _compile_exact_keys(
        evaluate,
        {"callable", "config", "cwd", "env", "pythonPath", "timeoutSeconds", "type"},
        "environment evaluate",
    )
    runtime_contract = _mapping(contract.get("runtime_contract"), "runtime contract")
    _compile_exact_keys(runtime_contract, {"timeoutSeconds"}, "runtime contract")
    workspace = _mapping(adapter_config.get("workspace"), "environment workspace")
    _compile_exact_keys(workspace, {"copy"}, "environment workspace")
    metrics = _mapping(adapter_config.get("metrics"), "environment metrics")
    _compile_exact_keys(metrics, {"keys", "source"}, "environment metrics")
    metric_names = _sequence(metrics.get("keys"), "environment metric names")
    interfaces = _sequence(adapter_config.get("interfaces"), "environment interfaces")
    candidate_context = _mapping(
        environment.candidate_contract.get("context"), "candidate context"
    )
    if evaluate.get("pythonPath") != () or evaluate.get("env") != {}:
        _fail(
            "evaluator_environment_unsupported",
            "Evaluator paths/environment must be expressed by typed runtime scopes.",
        )
    expected_access_policy = (
        "CodeAwareReadOnly" if candidate_format == "files" else "SchemaAware"
    )
    expected_mutation_policy = (
        "TrialWorkspaceOnly" if candidate_format == "files" else "NoMutation"
    )
    if (
        contract.get("schema") != "optpilot.retained-study-environment-contract.v1"
        or contract.get("access_policy") != expected_access_policy
        or contract.get("mutation_policy") != expected_mutation_policy
        or (
            is_container
            and contract.get("runtime_requirements")
            != _expected_container_runtime_requirements(settings)
        )
        or (
            not is_container
            and contract.get("runtime_requirements")
            not in ({}, {"networkPolicy": "disabled"})
        )
        or adapter.get("type") != "configured_environment"
        or adapter.get("implementation") != "builtin.configured_environment"
        or evaluate.get("type") != "python"
        or evaluate.get("cwd") != "."
        or not isinstance(evaluate.get("config"), Mapping)
        or runtime_contract.get("timeoutSeconds") != evaluate.get("timeoutSeconds")
        or workspace.get("copy") != ()
        or adapter_config.get("outputFiles") != ()
        or adapter_config.get("records") != ()
        or metrics.get("source") != "return"
        or not metric_names
        or len(metric_names) > _MAX_METRICS
        or len(set(metric_names)) != len(metric_names)
        or not _canonical_json_equal(adapter_config.get("candidate"), candidate_context.get("candidate"))
        or not _canonical_json_equal(adapter_config.get("context"), candidate_context)
        or not _canonical_json_equal(
            interfaces,
            [profile.to_dict() for profile in environment.interface_profiles],
        )
    ):
        _fail(
            "evaluator_mode_unsupported",
            "The first binding slice requires the exact retained configured Python evaluator contract.",
        )
    module, attribute = _parse_callable(evaluate.get("callable"))
    file_materialization = (
        _compile_file_materialization(
            candidate=evaluation_spec.candidate,
            candidate_contract=environment.candidate_contract,
        )
        if candidate_format == "files"
        else None
    )

    execution = _mapping(run_definition.execution_policy, "execution policy")
    _compile_exact_keys(
        execution,
        {"backend", "defaults", "parallelism", "scheduler"},
        "execution policy",
    )
    backend = _mapping(execution.get("backend"), "execution backend")
    _compile_exact_keys(backend, {"config", "implementation", "type"}, "execution backend")
    defaults = _mapping(execution.get("defaults"), "execution defaults")
    _compile_exact_keys(
        defaults,
        {"resourceProfile", "retryPolicy", "sandboxSpec"},
        "execution defaults",
    )
    sandbox = _mapping(evaluation_spec.sandbox_spec, "evaluation sandbox")
    _compile_exact_keys(
        sandbox,
        {"cleanupPolicy", "environmentVariables", "networkPolicy", "runtimeType"},
        "evaluation sandbox",
    )
    expected_backend = (
        {
            "type": "container",
            "implementation": "builtin.local_container_backend",
            "config": {},
        }
        if is_container
        else {
            "type": "local",
            "implementation": "builtin.local_subprocess_backend",
            "config": {},
        }
    )
    if (
        backend.get("type") != expected_backend["type"]
        or backend.get("implementation") != expected_backend["implementation"]
        or (
            is_container
            and backend.get("config") != {}
        )
        or (
            not is_container
            and backend.get("config") not in ({}, {"networkPolicy": "disabled"})
        )
    ):
        _fail(
            "backend_unsupported",
            "The first process binding slice requires the builtin local backend.",
        )
    expected_sandbox_runtime = "container" if is_container else "process"
    expected_network = (
        settings.get("container_network") if is_container else "disabled"
    )
    if (
        sandbox.get("runtimeType") != expected_sandbox_runtime
        or sandbox.get("networkPolicy") != expected_network
        or sandbox.get("environmentVariables") != {}
        or sandbox.get("cleanupPolicy") != "always"
    ):
        _fail(
            "sandbox_unsupported",
            "The first process binding slice requires the exact empty, network-disabled sandbox request.",
        )
    if not _canonical_json_equal(defaults.get("sandboxSpec"), sandbox):
        _fail(
            "evaluation_policy_mismatch",
            "Execution defaults differ from the exact evaluation sandbox.",
        )
    resources = _runtime_resources(evaluation_spec.resource_profile)

    projected_layers = tuple(
        ProjectedInputLayer(
            scope=TRIAL_SCOPE,
            projection_name=ENVIRONMENT_PROJECTION,
            projection_subpath=ENVIRONMENT_PROJECTION_PARTITION,
            snapshot_ref=item.snapshot_ref,
            source_subpath=item.source_subpath,
            destination_subpath=item.destination_subpath,
            precedence=item.precedence,
            collision_policy="identical",
        )
        for item in retained_input_layers
    )
    projection_mappings = [
        TreeMapping(
            snapshot_ref=source.snapshot_ref,
            destination=ENVIRONMENT_PROJECTION_PARTITION,
            source_subpath=source.source_subpath,
        )
    ]
    if prepared_python is not None:
        projection_mappings.append(
            TreeMapping(
                snapshot_ref=prepared_python.snapshot_ref,
                destination=ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                source_subpath=prepared_python.source_subpath,
            )
        )
    if file_materialization is not None:
        assert candidate_input is not None
        assert candidate_input.snapshot_ref is not None
        projection_mappings.append(
            TreeMapping(
                snapshot_ref=candidate_input.snapshot_ref,
                destination=CANDIDATE_PROJECTION_PARTITION,
                source_subpath=".",
            )
        )
        projected_layers = (
            *projected_layers,
            ProjectedInputLayer(
                scope=TRIAL_SCOPE,
                projection_name=ENVIRONMENT_PROJECTION,
                projection_subpath=CANDIDATE_PROJECTION_PARTITION,
                snapshot_ref=candidate_input.snapshot_ref,
                source_subpath=".",
                destination_subpath=file_materialization.root.relative_path,
                precedence=len(projected_layers),
                collision_policy="replace",
            ),
        )

    projection_scopes = (
        PortableRuntimeScope(
            ENVIRONMENT_SOURCE_SCOPE,
            "/optpilot/environment_source",
            "read",
            ProjectionScopeSource(
                ENVIRONMENT_PROJECTION, ENVIRONMENT_PROJECTION_PARTITION
            ),
        ),
    )
    if prepared_python is not None:
        projection_scopes += (
            PortableRuntimeScope(
                ENVIRONMENT_PREPARED_PYTHON_SCOPE,
                "/optpilot/environment_prepared_python",
                "read",
                ProjectionScopeSource(
                    ENVIRONMENT_PROJECTION,
                    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                ),
            ),
        )
    scopes = (
        *projection_scopes,
        PortableRuntimeScope(
            TRIAL_SCOPE,
            "/optpilot/trial",
            "read-write",
            (
                LayeredVolumeScopeSource(TRIAL_SCOPE, projected_layers)
                if projected_layers
                else VolumeScopeSource(TRIAL_SCOPE)
            ),
        ),
        PortableRuntimeScope(
            CONTROL_SCOPE,
            "/optpilot/control",
            "read-write",
            VolumeScopeSource(CONTROL_SCOPE),
        ),
    )
    return PortableAttemptRuntimeSpec(
        run_definition_digest=run_definition.digest,
        evaluation_spec_digest=evaluation_spec.digest,
        environment_revision_digest=environment.digest,
        prepared_runtime_digest=runtime.digest,
        provider=RuntimeProviderRequirement(
            kind="process",
            # The executing provider is this host's process provider either
            # way: for a container attempt it is what supervises the engine
            # client. The image's identity is pinned separately through the
            # prepared-runtime digest, so recording host facts here keeps the
            # durable record shape unchanged and truthful about who ran.
            portability="provider-scoped",
            platform=provider.platform,
            builder_fingerprint=provider.builder_fingerprint,
        )
        if is_container
        else RuntimeProviderRequirement(
            kind="process",
            portability=runtime.portability,
            platform=runtime.platform,
            builder_fingerprint=runtime.builder_fingerprint,
        ),
        projection_name=ENVIRONMENT_PROJECTION,
        projection_spec=ProjectionSpec(
            owner_id=owner_id,
            mappings=tuple(projection_mappings),
        ),
        writable_volumes=(
            WritableVolumeRequirement(
                name=TRIAL_SCOPE,
                quota=TRIAL_FILESYSTEM_QUOTA,
                quota_enforcement="advisory",
            ),
            WritableVolumeRequirement(
                name=CONTROL_SCOPE,
                quota=CONTROL_FILESYSTEM_QUOTA,
                quota_enforcement="advisory",
            ),
        ),
        scopes=scopes,
        entrypoint=PythonCallableEntrypoint(
            scope=ENVIRONMENT_SOURCE_SCOPE,
            module=module,
            attribute=attribute,
        ),
        python_import_roots=tuple(roots),
        evaluator_settings=thaw_json(evaluate["config"]),
        file_materialization=file_materialization,
        declared_metric_names=tuple(metric_names),
        workdir=ScopePath(TRIAL_SCOPE, "."),
        resources=resources,
        read_only_scope_enforcement="advisory",
        timeout_seconds=_effective_timeout(evaluate, evaluation_spec),
        requested_network_policy=(
            str(expected_network) if is_container else "disabled"
        ),
        cleanup_policy="always",
    )


__all__ = [
    "CANDIDATE_PROJECTION_PARTITION",
    "CandidateRuntimeInput",
    "CONTROL_SCOPE",
    "CONTROL_FILESYSTEM_QUOTA",
    "ENVIRONMENT_PROJECTION",
    "ENVIRONMENT_PROJECTION_PARTITION",
    "ENVIRONMENT_PREPARED_PYTHON_PARTITION",
    "ENVIRONMENT_PREPARED_PYTHON_SCOPE",
    "ENVIRONMENT_SOURCE_SCOPE",
    "EXECUTION_BINDING_EVIDENCE_SCHEMA",
    "ExecutionBindingEvidence",
    "ExecutionProviderFacts",
    "FileCandidateManifestEntry",
    "FileCandidateMaterialization",
    "LayeredVolumeScopeSource",
    "PORTABLE_ATTEMPT_RUNTIME_SPEC_SCHEMA",
    "PortableAttemptRuntimeSpec",
    "PortableRuntimeScope",
    "ProjectionBindingEvidence",
    "ProjectionScopeSource",
    "ProjectedInputLayer",
    "PythonCallableEntrypoint",
    "RuntimeBindingCompileError",
    "RuntimeProviderRequirement",
    "RuntimeResourceRequest",
    "TRIAL_SCOPE",
    "TRIAL_FILESYSTEM_QUOTA",
    "VolumeScopeSource",
    "WritableVolumeEvidence",
    "WritableVolumeRequirement",
    "compile_retained_process_attempt_runtime",
]
