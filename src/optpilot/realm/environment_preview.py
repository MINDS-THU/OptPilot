"""Portable planning records for one contextual Environment Preview.

The compiler in this module is deliberately below Studio and above provider
realization.  It reads one exact retained environment revision from a resolved
candidate inspection target and produces only immutable, provider-neutral
facts.  Host paths, store placement, credentials, leases, ports on the host,
and backend handles belong to a later execution binding.

The executable slice is intentionally narrow: parameter candidates or one
exact retained file tree, web presentation, a sha256-pinned container,
denied/enforced network access, and no setup, builds, or host-derived
environment. Unsupported declarations fail closed instead of being
approximated by a process launcher or an editable workspace copy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from ._validation import (
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmConflict, RealmIntegrityError
from .inspection import ResolvedCandidateInspectionTarget
from .refs import CandidateRef, canonical_json_bytes, request_digest
from .run_closure import (
    InterfaceAcceptsSpec,
    InterfaceLaunchProfile,
    InterfaceResourceSpec,
    WebPresentationSpec,
)
from .selections import SelectionRef


JsonDict = Dict[str, Any]

ENVIRONMENT_PREVIEW_CONTEXT_SCHEMA = "optpilot.environment-preview-context.v1"
ENVIRONMENT_PREVIEW_PLAN_SCHEMA = "optpilot.environment-preview-plan.v1"
ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE = (
    "application/vnd.optpilot.candidate+json"
)
ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT = "/optpilot/interface/candidate"

_PLAN_KIND = "environment-preview"
_NETWORK_POLICY = "denied"
_NETWORK_ENFORCEMENT = "enforced"
_RUNTIME_KIND = "container"
_PRESENTATION_KIND = "web"
_MAX_PLAN_PORTS = 16
_MAX_PARAMETER_SPEC_BYTES = 64 * 1024
_MAX_ENVIRONMENT_ITEMS = 256
_MAX_COMMAND_ITEMS = 256
_MAX_RECORD_BYTES = 1024 * 1024

_IMMUTABLE_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$"
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|CREDENTIALS?|API_KEY|ACCESS_KEY|"
    r"PRIVATE_KEY|AUTH_TOKEN|ACCESS_TOKEN|CLIENT_SECRET|TOKEN)$",
    re.IGNORECASE,
)
_SENSITIVE_PARAMETER_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "auth_token",
        "client_secret",
        "credential",
        "credentials",
        "host_path",
        "password",
        "passwd",
        "private_key",
        "provider_coordinate",
        "secret",
        "store_id",
        "token",
    }
)
_FIXED_INTERFACE_ENV_PATHS = {
    "OPTPILOT_INTERFACE_CANDIDATE_ROOT": ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
    "OPTPILOT_INTERFACE_CONTEXT": "/optpilot/interface/context.json",
    "OPTPILOT_INTERFACE_OUTPUT_ROOT": "/optpilot/interface/output",
    "OPTPILOT_INTERFACE_OUTPUTS_FILE": "/optpilot/interface/control/outputs.jsonl",
}


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


def _optional_token(value: Any, label: str, *, max_bytes: int = 1024) -> str | None:
    if value is None:
        return None
    return required_text(value, label, max_bytes=max_bytes)


def _frozen_public_environment(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("environment preview invocation environment must be a mapping.")
    if len(value) > _MAX_ENVIRONMENT_ITEMS:
        raise ValueError("environment preview invocation contains too many variables.")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = required_text(
            raw_name, "environment preview variable name", max_bytes=256
        )
        if _ENV_NAME_RE.fullmatch(name) is None:
            raise ValueError(
                f"environment preview variable name {name!r} is invalid."
            )
        if not isinstance(raw_value, str) or raw_value != raw_value.strip():
            raise ValueError(
                f"environment preview variable {name!r} must be a trimmed string."
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in raw_value):
            raise ValueError(
                f"environment preview variable {name!r} contains control characters."
            )
        if len(raw_value.encode("utf-8", errors="strict")) > 16_384:
            raise ValueError(
                f"environment preview variable {name!r} exceeds 16384 UTF-8 bytes."
            )
        text = raw_value
        if _SECRET_ENV_RE.search(name):
            raise ValueError(
                f"environment preview variable {name!r} is credential-shaped; "
                "credentials are unsupported in the first release."
            )
        is_fixed_interface_path = _FIXED_INTERFACE_ENV_PATHS.get(name) == text
        if _looks_like_host_path(text) and not is_fixed_interface_path:
            raise ValueError(
                f"environment preview variable {name!r} contains a host path."
            )
        result[name] = text
    return MappingProxyType(dict(sorted(result.items())))


def _looks_like_host_path(value: str) -> bool:
    lowered = value.lower()
    portable_parts = value.replace("\\", "/").split("/")
    return (
        value.startswith(("/", "\\\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value) is not None
        or lowered.startswith("file:")
        or "\\" in value
        or ".." in portable_parts
    )


def _parameter_spec_is_context_safe(value: Any, *, key: str | None = None) -> bool:
    if key is not None:
        normalized_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
        normalized_key = normalized_key.lower().replace("-", "_")
        if normalized_key in _SENSITIVE_PARAMETER_KEYS:
            return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return not _looks_like_host_path(value) and "\x00" not in value
    if isinstance(value, Mapping):
        return all(
            isinstance(child_key, str)
            and _parameter_spec_is_context_safe(child, key=child_key)
            for child_key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_parameter_spec_is_context_safe(child) for child in value)
    return False


def _bounded_parameter_spec(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    thawed = thaw_json(value)
    if not isinstance(thawed, Mapping):  # pragma: no cover - candidate invariant
        return None
    if not _parameter_spec_is_context_safe(thawed):
        return None
    if len(canonical_json_bytes(thawed)) > _MAX_PARAMETER_SPEC_BYTES:
        return None
    frozen = freeze_json(thawed, label="environment preview candidate parameters")
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        return None
    return frozen


@dataclass(frozen=True)
class EnvironmentPreviewLogicalPaths:
    """Fixed container paths; none is a provider or host coordinate."""

    context: str = "/optpilot/interface/context.json"
    app: str = "/optpilot/interface/app"
    runtime_env: str = "/optpilot/interface/runtime_env"
    prepared_outputs: str = "/optpilot/interface/prepared_outputs"
    workspace: str = "/optpilot/interface/workspace"
    artifacts: str = "/optpilot/interface/artifacts"
    output_root: str = "/optpilot/interface/output"
    outputs_file: str = "/optpilot/interface/control/outputs.jsonl"

    def __post_init__(self) -> None:
        expected = {
            "context": "/optpilot/interface/context.json",
            "app": "/optpilot/interface/app",
            "runtime_env": "/optpilot/interface/runtime_env",
            "prepared_outputs": "/optpilot/interface/prepared_outputs",
            "workspace": "/optpilot/interface/workspace",
            "artifacts": "/optpilot/interface/artifacts",
            "output_root": "/optpilot/interface/output",
            "outputs_file": "/optpilot/interface/control/outputs.jsonl",
        }
        if self.to_dict() != expected:
            raise ValueError("environment preview logical paths are fixed by the contract.")

    def to_dict(self) -> JsonDict:
        return {
            "context": self.context,
            "app": self.app,
            "runtime_env": self.runtime_env,
            "prepared_outputs": self.prepared_outputs,
            "workspace": self.workspace,
            "artifacts": self.artifacts,
            "output_root": self.output_root,
            "outputs_file": self.outputs_file,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewLogicalPaths":
        _exact_keys(payload, set(cls.__dataclass_fields__), "preview logical paths")
        return cls(**dict(payload))


@dataclass(frozen=True)
class EnvironmentPreviewFingerprints:
    """Stable semantic anchors without their potentially sensitive manifests."""

    source: str
    runtime: str
    candidate: str
    evaluation: str
    run_definition: str
    selection: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            lower_hex_digest(
                getattr(self, name), f"environment preview {name} fingerprint"
            )

    def to_dict(self) -> JsonDict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewFingerprints":
        _exact_keys(payload, set(cls.__dataclass_fields__), "preview fingerprints")
        return cls(**dict(payload))


@dataclass(frozen=True)
class EnvironmentPreviewContainerRuntime:
    """Effective sha256-pinned runtime after profile/base composition."""

    image_ref: str
    prepared_runtime_digest: str
    source: str
    engine: Optional[str] = None
    platform: Optional[str] = None
    kind: str = _RUNTIME_KIND

    def __post_init__(self) -> None:
        if self.kind != _RUNTIME_KIND:
            raise ValueError("environment preview runtime must be a container.")
        image = required_text(
            self.image_ref, "environment preview immutable image", max_bytes=1024
        )
        if _IMMUTABLE_IMAGE_RE.fullmatch(image) is None:
            raise ValueError(
                "environment preview image_ref must be pinned by a sha256 digest."
            )
        lower_hex_digest(
            self.prepared_runtime_digest,
            "environment preview prepared runtime digest",
        )
        if self.source not in {"interface-profile", "prepared-runtime"}:
            raise ValueError("environment preview runtime source is unsupported.")
        engine = _optional_token(
            self.engine, "environment preview container engine", max_bytes=64
        )
        platform = _optional_token(
            self.platform, "environment preview container platform", max_bytes=256
        )
        object.__setattr__(self, "image_ref", image)
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "platform", platform)

    def to_dict(self) -> JsonDict:
        return {
            "engine": self.engine,
            "imageRef": self.image_ref,
            "kind": self.kind,
            "platform": self.platform,
            "preparedRuntimeDigest": self.prepared_runtime_digest,
            "source": self.source,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewContainerRuntime":
        _exact_keys(
            payload,
            {
                "engine",
                "imageRef",
                "kind",
                "platform",
                "preparedRuntimeDigest",
                "source",
            },
            "preview container runtime",
        )
        return cls(
            image_ref=payload["imageRef"],
            prepared_runtime_digest=payload["preparedRuntimeDigest"],
            source=payload["source"],
            engine=payload["engine"],
            platform=payload["platform"],
            kind=payload["kind"],
        )


@dataclass(frozen=True)
class EnvironmentPreviewInvocation:
    """Exact profile command plus its logical app-relative working directory."""

    command: Tuple[str, ...]
    authored_cwd: str
    workdir: str
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, (tuple, list))
            or not self.command
            or len(self.command) > _MAX_COMMAND_ITEMS
        ):
            raise ValueError("environment preview command must be bounded and nonempty.")
        command = tuple(
            required_text(item, "environment preview command item", max_bytes=16_384)
            for item in self.command
        )
        authored_cwd = required_text(
            self.authored_cwd, "environment preview authored cwd", max_bytes=4096
        )
        workdir = required_text(
            self.workdir, "environment preview logical workdir", max_bytes=8192
        )
        expected_workdir = "/optpilot/interface/app"
        if authored_cwd != ".":
            expected_workdir += "/" + authored_cwd
        if workdir != expected_workdir:
            raise ValueError(
                "environment preview workdir must resolve the profile cwd under app."
            )
        environment = _frozen_public_environment(self.environment)
        if (
            environment.get("OPTPILOT_INTERFACE_CONTEXT")
            != "/optpilot/interface/context.json"
        ):
            raise ValueError("environment preview context variable is missing or changed.")
        output_root = environment.get("OPTPILOT_INTERFACE_OUTPUT_ROOT")
        outputs_file = environment.get("OPTPILOT_INTERFACE_OUTPUTS_FILE")
        if (output_root is None) != (outputs_file is None):
            raise ValueError(
                "environment preview output variables must be supplied together."
            )
        if output_root is not None and (
            output_root != "/optpilot/interface/output"
            or outputs_file != "/optpilot/interface/control/outputs.jsonl"
        ):
            raise ValueError(
                "environment preview output variables are changed."
            )
        if "OPTPILOT_INTERFACE_PROFILE_ID" not in environment:
            raise ValueError("environment preview profile variable is missing.")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "authored_cwd", authored_cwd)
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "environment", environment)

    def to_dict(self) -> JsonDict:
        return {
            "authoredCwd": self.authored_cwd,
            "command": list(self.command),
            "environment": dict(self.environment),
            "workdir": self.workdir,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewInvocation":
        _exact_keys(
            payload,
            {"authoredCwd", "command", "environment", "workdir"},
            "preview invocation",
        )
        if not isinstance(payload["command"], list):
            raise TypeError("preview invocation command must be a list.")
        return cls(
            command=tuple(payload["command"]),
            authored_cwd=payload["authoredCwd"],
            workdir=payload["workdir"],
            environment=payload["environment"],
        )


@dataclass(frozen=True)
class EnvironmentPreviewResourceClaims:
    """Provider-neutral capacity claims derived exactly from the profile."""

    cpu_millis: int
    memory_bytes: int
    gpu_count: int = 0

    def __post_init__(self) -> None:
        positive_int(self.cpu_millis, "environment preview cpu_millis")
        positive_int(self.memory_bytes, "environment preview memory_bytes")
        nonnegative_int(self.gpu_count, "environment preview gpu_count")
        if (
            self.cpu_millis > 1_024_000
            or self.memory_bytes > 1_048_576 * 1024**2
            or self.gpu_count > 64
        ):
            raise ValueError("environment preview resource claims exceed platform bounds.")

    @classmethod
    def from_profile(
        cls, resources: InterfaceResourceSpec
    ) -> "EnvironmentPreviewResourceClaims":
        return cls(
            cpu_millis=resources.cpu * 1000,
            memory_bytes=resources.memory_mib * 1024**2,
            gpu_count=resources.gpus,
        )

    def to_dict(self) -> JsonDict:
        return {
            "cpu_millis": self.cpu_millis,
            "gpu_count": self.gpu_count,
            "memory_bytes": self.memory_bytes,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewResourceClaims":
        _exact_keys(payload, set(cls.__dataclass_fields__), "preview resource claims")
        return cls(**dict(payload))


@dataclass(frozen=True)
class EnvironmentPreviewContext:
    """Bounded context manifest visible to the untrusted preview application."""

    plan_digest: str
    profile_id: str
    accepts: InterfaceAcceptsSpec
    selection: SelectionRef
    candidate_format: str
    candidate_ref: str
    fingerprints: EnvironmentPreviewFingerprints
    paths: EnvironmentPreviewLogicalPaths
    outputs_enabled: bool = False
    parameter_spec: Optional[Mapping[str, Any]] = None
    candidate_root: Optional[str] = None

    def __post_init__(self) -> None:
        lower_hex_digest(self.plan_digest, "environment preview plan digest")
        required_text(self.profile_id, "environment preview profile id", max_bytes=256)
        if not isinstance(self.accepts, InterfaceAcceptsSpec):
            raise TypeError("environment preview accepts must be InterfaceAcceptsSpec.")
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("environment preview selection must be SelectionRef.")
        if self.selection.kind != "candidate":
            raise ValueError("environment preview context requires a candidate selection.")
        for value, label in (
            (self.selection.source_id, "selection source id"),
            (self.selection.source_owner_id, "selection source owner id"),
            (self.selection.entity_id, "selection entity id"),
        ):
            if _looks_like_host_path(value):
                raise ValueError(
                    f"environment preview {label} must not be a host path."
                )
        if self.candidate_format not in {"parameters", "files"}:
            raise ValueError("environment preview context candidate format is unsupported.")
        candidate_ref = str(CandidateRef.parse(self.candidate_ref))
        if candidate_ref != self.selection.entity_ref:
            raise ValueError(
                "environment preview candidate_ref differs from the selection."
            )
        if not isinstance(self.fingerprints, EnvironmentPreviewFingerprints):
            raise TypeError("environment preview fingerprints are invalid.")
        if not isinstance(self.paths, EnvironmentPreviewLogicalPaths):
            raise TypeError("environment preview logical paths are invalid.")
        if not isinstance(self.outputs_enabled, bool):
            raise TypeError("environment preview outputs_enabled must be a boolean.")
        parameter_spec = self.parameter_spec
        candidate_root = self.candidate_root
        if self.candidate_format == "parameters" and candidate_root is not None:
            raise ValueError(
                "parameter candidate context cannot declare a candidate root."
            )
        if self.candidate_format == "files":
            if candidate_root != ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT:
                raise ValueError(
                    "file candidate context must use the fixed logical candidate root."
                )
            if parameter_spec is not None:
                raise ValueError(
                    "file candidate context cannot embed parameter candidate content."
                )
        if parameter_spec is not None:
            bounded = _bounded_parameter_spec(parameter_spec)
            if bounded is None:
                raise ValueError(
                    "environment preview parameter spec is unsafe or too large."
                )
            parameter_spec = bounded
        object.__setattr__(self, "candidate_ref", candidate_ref)
        object.__setattr__(self, "parameter_spec", parameter_spec)
        object.__setattr__(self, "candidate_root", candidate_root)
        _bounded_record(self.to_dict(), "environment preview context")

    def _identity_dict(self) -> JsonDict:
        candidate = {
            "format": self.candidate_format,
            "candidateRef": self.candidate_ref,
            "parameters": (
                None
                if self.parameter_spec is None
                else thaw_json(self.parameter_spec)
            ),
        }
        if self.candidate_format == "files":
            candidate["candidateRoot"] = self.candidate_root
        return {
            "accepts": self.accepts.to_dict(),
            "candidate": candidate,
            "fingerprints": self.fingerprints.to_dict(),
            "paths": self.paths.to_dict(),
            "profile": {
                "id": self.profile_id,
                "networkEnforcement": _NETWORK_ENFORCEMENT,
                "networkPolicy": _NETWORK_POLICY,
                "presentationKind": _PRESENTATION_KIND,
                "requestedSecretNames": [],
                "outputs": self.outputs_enabled,
            },
            "schema": ENVIRONMENT_PREVIEW_CONTEXT_SCHEMA,
            "selection": self.selection.to_dict(),
        }

    def to_dict(self) -> JsonDict:
        result = self._identity_dict()
        result["planDigest"] = self.plan_digest
        return result

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewContext":
        try:
            _exact_keys(
                payload,
                {
                    "accepts",
                    "candidate",
                    "fingerprints",
                    "paths",
                    "planDigest",
                    "profile",
                    "schema",
                    "selection",
                },
                "environment preview context",
            )
            if payload["schema"] != ENVIRONMENT_PREVIEW_CONTEXT_SCHEMA:
                raise ValueError("environment preview context schema is unsupported.")
            profile = payload["profile"]
            _exact_keys(
                profile,
                {
                    "id",
                    "networkEnforcement",
                    "networkPolicy",
                    "presentationKind",
                    "requestedSecretNames",
                    "outputs",
                },
                "environment preview context profile",
            )
            if profile != {
                "id": profile["id"],
                "networkEnforcement": _NETWORK_ENFORCEMENT,
                "networkPolicy": _NETWORK_POLICY,
                "presentationKind": _PRESENTATION_KIND,
                "requestedSecretNames": [],
                "outputs": profile["outputs"],
            }:
                raise ValueError("environment preview context grants are unsupported.")
            candidate = payload["candidate"]
            if not isinstance(candidate, Mapping):
                raise TypeError("environment preview context candidate must be a mapping.")
            candidate_format = candidate.get("format")
            candidate_fields = {"candidateRef", "format", "parameters"}
            if candidate_format == "files":
                candidate_fields.add("candidateRoot")
            _exact_keys(
                candidate,
                candidate_fields,
                "environment preview context candidate",
            )
            result = cls(
                plan_digest=payload["planDigest"],
                profile_id=profile["id"],
                accepts=InterfaceAcceptsSpec.from_dict(payload["accepts"]),
                selection=SelectionRef.from_dict(payload["selection"]),
                candidate_format=candidate["format"],
                candidate_ref=candidate["candidateRef"],
                fingerprints=EnvironmentPreviewFingerprints.from_dict(
                    payload["fingerprints"]
                ),
                paths=EnvironmentPreviewLogicalPaths.from_dict(payload["paths"]),
                outputs_enabled=profile["outputs"],
                parameter_spec=candidate["parameters"],
                candidate_root=candidate.get("candidateRoot"),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("environment preview context is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted environment preview context is invalid: {error}"
            ) from error


def _plan_identity_payload(
    *,
    profile_id: str,
    accepts: InterfaceAcceptsSpec,
    selection: SelectionRef,
    invocation: EnvironmentPreviewInvocation,
    runtime: EnvironmentPreviewContainerRuntime,
    resources: EnvironmentPreviewResourceClaims,
    timeout_seconds: int,
    presentation: WebPresentationSpec,
    paths: EnvironmentPreviewLogicalPaths,
    fingerprints: EnvironmentPreviewFingerprints,
    context_identity: Mapping[str, Any],
    outputs_enabled: bool,
    kind: str = _PLAN_KIND,
) -> JsonDict:
    return {
        "accepts": accepts.to_dict(),
        "context": dict(context_identity),
        "fingerprints": fingerprints.to_dict(),
        "grants": {
            "networkEnforcement": _NETWORK_ENFORCEMENT,
            "networkPolicy": _NETWORK_POLICY,
            "requestedSecretNames": [],
        },
        "invocation": invocation.to_dict(),
        "kind": kind,
        "logicalPaths": paths.to_dict(),
        "outputsEnabled": outputs_enabled,
        "presentation": presentation.to_dict(),
        "profileId": profile_id,
        "resources": resources.to_dict(),
        "runtime": runtime.to_dict(),
        "schema": ENVIRONMENT_PREVIEW_PLAN_SCHEMA,
        "selection": selection.to_dict(),
        "timeoutSeconds": timeout_seconds,
    }


@dataclass(frozen=True)
class EnvironmentPreviewPlan:
    """Exact approved-input-independent plan for a later Operator Job binding."""

    plan_digest: str
    profile_id: str
    accepts: InterfaceAcceptsSpec
    selection: SelectionRef
    invocation: EnvironmentPreviewInvocation
    runtime: EnvironmentPreviewContainerRuntime
    resources: EnvironmentPreviewResourceClaims
    timeout_seconds: int
    presentation: WebPresentationSpec
    paths: EnvironmentPreviewLogicalPaths
    fingerprints: EnvironmentPreviewFingerprints
    context: EnvironmentPreviewContext
    outputs_enabled: bool = False
    kind: str = _PLAN_KIND

    def __post_init__(self) -> None:
        if self.kind != _PLAN_KIND:
            raise ValueError("environment preview plan kind is unsupported.")
        lower_hex_digest(self.plan_digest, "environment preview plan digest")
        required_text(self.profile_id, "environment preview profile id", max_bytes=256)
        if not isinstance(self.accepts, InterfaceAcceptsSpec):
            raise TypeError("environment preview plan accepts are invalid.")
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("environment preview plan selection is invalid.")
        if not isinstance(self.invocation, EnvironmentPreviewInvocation):
            raise TypeError("environment preview plan invocation is invalid.")
        if not isinstance(self.runtime, EnvironmentPreviewContainerRuntime):
            raise TypeError("environment preview plan runtime is invalid.")
        if not isinstance(self.resources, EnvironmentPreviewResourceClaims):
            raise TypeError("environment preview plan resources are invalid.")
        if not isinstance(self.presentation, WebPresentationSpec):
            raise TypeError("environment preview plan presentation is invalid.")
        if not isinstance(self.paths, EnvironmentPreviewLogicalPaths):
            raise TypeError("environment preview plan paths are invalid.")
        if not isinstance(self.fingerprints, EnvironmentPreviewFingerprints):
            raise TypeError("environment preview plan fingerprints are invalid.")
        if not isinstance(self.context, EnvironmentPreviewContext):
            raise TypeError("environment preview plan context is invalid.")
        if not isinstance(self.outputs_enabled, bool):
            raise TypeError("environment preview outputs_enabled must be a boolean.")
        positive_int(self.timeout_seconds, "environment preview timeout_seconds")
        if self.timeout_seconds > 604_800:
            raise ValueError("environment preview timeout exceeds seven days.")
        if 1 + len(self.presentation.extra_ports) > _MAX_PLAN_PORTS:
            raise ValueError("environment preview presentation requests too many ports.")
        if (
            self.context.plan_digest != self.plan_digest
            or self.context.profile_id != self.profile_id
            or self.context.accepts != self.accepts
            or self.context.selection != self.selection
            or self.context.paths != self.paths
            or self.context.fingerprints != self.fingerprints
            or self.context.outputs_enabled != self.outputs_enabled
            or (
                "OPTPILOT_INTERFACE_OUTPUT_ROOT" in self.invocation.environment
            )
            != self.outputs_enabled
            or self.invocation.environment.get("OPTPILOT_INTERFACE_PROFILE_ID")
            != self.profile_id
        ):
            raise ValueError("environment preview context differs from its plan.")
        expected = request_digest(self._identity_dict())
        if self.plan_digest != expected:
            raise ValueError("environment preview plan digest differs from its facts.")
        _bounded_record(self.to_dict(), "environment preview plan")

    def _identity_dict(self) -> JsonDict:
        return _plan_identity_payload(
            profile_id=self.profile_id,
            accepts=self.accepts,
            selection=self.selection,
            invocation=self.invocation,
            runtime=self.runtime,
            resources=self.resources,
            timeout_seconds=self.timeout_seconds,
            presentation=self.presentation,
            paths=self.paths,
            fingerprints=self.fingerprints,
            context_identity=self.context._identity_dict(),
            outputs_enabled=self.outputs_enabled,
            kind=self.kind,
        )

    def to_dict(self) -> JsonDict:
        result = self._identity_dict()
        result["context"] = self.context.to_dict()
        result["planDigest"] = self.plan_digest
        return result

    @property
    def digest(self) -> str:
        return self.plan_digest

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def build(
        cls,
        *,
        profile_id: str,
        accepts: InterfaceAcceptsSpec,
        selection: SelectionRef,
        invocation: EnvironmentPreviewInvocation,
        runtime: EnvironmentPreviewContainerRuntime,
        resources: EnvironmentPreviewResourceClaims,
        timeout_seconds: int,
        presentation: WebPresentationSpec,
        paths: EnvironmentPreviewLogicalPaths,
        fingerprints: EnvironmentPreviewFingerprints,
        candidate_format: str,
        candidate_ref: str,
        parameter_spec: Mapping[str, Any] | None,
        candidate_root: str | None = None,
        outputs_enabled: bool = False,
    ) -> "EnvironmentPreviewPlan":
        # The context carries a back-reference to the surrounding plan.  Hash
        # the context identity without that one field, then place the resulting
        # digest in both records; ``__post_init__`` verifies the complete link.
        placeholder_context = EnvironmentPreviewContext(
            plan_digest="0" * 64,
            profile_id=profile_id,
            accepts=accepts,
            selection=selection,
            candidate_format=candidate_format,
            candidate_ref=candidate_ref,
            fingerprints=fingerprints,
            paths=paths,
            outputs_enabled=outputs_enabled,
            parameter_spec=parameter_spec,
            candidate_root=candidate_root,
        )
        plan_digest = request_digest(
            _plan_identity_payload(
                profile_id=profile_id,
                accepts=accepts,
                selection=selection,
                invocation=invocation,
                runtime=runtime,
                resources=resources,
                timeout_seconds=timeout_seconds,
                presentation=presentation,
                paths=paths,
                fingerprints=fingerprints,
                context_identity=placeholder_context._identity_dict(),
                outputs_enabled=outputs_enabled,
            )
        )
        context = replace(placeholder_context, plan_digest=plan_digest)
        return cls(
            plan_digest=plan_digest,
            profile_id=profile_id,
            accepts=accepts,
            selection=selection,
            invocation=invocation,
            runtime=runtime,
            resources=resources,
            timeout_seconds=timeout_seconds,
            presentation=presentation,
            paths=paths,
            fingerprints=fingerprints,
            context=context,
            outputs_enabled=outputs_enabled,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EnvironmentPreviewPlan":
        try:
            _exact_keys(
                payload,
                {
                    "accepts",
                    "context",
                    "fingerprints",
                    "grants",
                    "invocation",
                    "kind",
                    "logicalPaths",
                    "outputsEnabled",
                    "planDigest",
                    "presentation",
                    "profileId",
                    "resources",
                    "runtime",
                    "schema",
                    "selection",
                    "timeoutSeconds",
                },
                "environment preview plan",
            )
            if payload["schema"] != ENVIRONMENT_PREVIEW_PLAN_SCHEMA:
                raise ValueError("environment preview plan schema is unsupported.")
            expected_grants = {
                "networkEnforcement": _NETWORK_ENFORCEMENT,
                "networkPolicy": _NETWORK_POLICY,
                "requestedSecretNames": [],
            }
            if payload["grants"] != expected_grants:
                raise ValueError("environment preview plan grants are unsupported.")
            result = cls(
                plan_digest=payload["planDigest"],
                profile_id=payload["profileId"],
                accepts=InterfaceAcceptsSpec.from_dict(payload["accepts"]),
                selection=SelectionRef.from_dict(payload["selection"]),
                invocation=EnvironmentPreviewInvocation.from_dict(
                    payload["invocation"]
                ),
                runtime=EnvironmentPreviewContainerRuntime.from_dict(
                    payload["runtime"]
                ),
                resources=EnvironmentPreviewResourceClaims.from_dict(
                    payload["resources"]
                ),
                timeout_seconds=payload["timeoutSeconds"],
                presentation=WebPresentationSpec.from_dict(
                    payload["presentation"]
                ),
                paths=EnvironmentPreviewLogicalPaths.from_dict(
                    payload["logicalPaths"]
                ),
                fingerprints=EnvironmentPreviewFingerprints.from_dict(
                    payload["fingerprints"]
                ),
                context=EnvironmentPreviewContext.from_dict(payload["context"]),
                outputs_enabled=payload["outputsEnabled"],
                kind=payload["kind"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("environment preview plan is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted environment preview plan is invalid: {error}"
            ) from error


def compile_environment_preview_plan(
    target: ResolvedCandidateInspectionTarget,
    profile_id: str | None = None,
) -> EnvironmentPreviewPlan:
    """Compile one exact retained candidate/profile into a portable plan.

    No lookup of current catalog source occurs here.  ``profile_id`` is resolved
    only against the interface profiles inside the target's snapshotted
    :class:`EnvironmentRevisionManifest`.
    """

    if not isinstance(target, ResolvedCandidateInspectionTarget):
        raise TypeError("target must be a ResolvedCandidateInspectionTarget.")
    if profile_id is not None and not isinstance(profile_id, str):
        raise TypeError("profile_id must be a string or None.")
    if not target.runnable:
        raise RealmConflict("Selected candidate inspection content is unavailable.")
    if target.selection.kind != "candidate":  # pragma: no cover - target invariant
        raise RealmConflict("Environment Preview requires a candidate selection.")
    candidate_format = target.candidate.admission.envelope.candidate_format
    if candidate_format not in {"parameters", "files"}:
        raise RealmConflict(
            "Environment Preview supports retained parameter and file candidates; "
            "opaque candidates are unsupported."
        )
    try:
        evaluation_spec = target.compile_evaluation_spec()
    except (TypeError, ValueError) as error:
        raise RealmConflict(
            f"Selected candidate is incompatible with its retained environment: {error}"
        ) from error

    closure = target.evaluation.closure
    environment = closure.environment_revision
    profile = _select_profile(environment.interface_profiles, profile_id)
    _validate_profile_compatibility(profile)
    runtime = _effective_container_runtime(target, profile)

    paths = EnvironmentPreviewLogicalPaths()
    authored_env = dict(profile.env)
    reserved = {
        "OPTPILOT_INTERFACE_CONTEXT": paths.context,
        "OPTPILOT_INTERFACE_PROFILE_ID": profile.profile_id,
    }
    if candidate_format == "files":
        reserved["OPTPILOT_INTERFACE_CANDIDATE_ROOT"] = (
            ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT
        )
    if profile.outputs:
        reserved.update(
            {
                "OPTPILOT_INTERFACE_OUTPUT_ROOT": paths.output_root,
                "OPTPILOT_INTERFACE_OUTPUTS_FILE": paths.outputs_file,
            }
        )
    reserved_names = set(reserved) | {
        "OPTPILOT_INTERFACE_CANDIDATE_ROOT",
        "OPTPILOT_INTERFACE_OUTPUT_ROOT",
        "OPTPILOT_INTERFACE_OUTPUTS_FILE",
    }
    collisions = set(authored_env) & reserved_names
    if collisions:
        raise RealmConflict(
            "Environment Preview profile overrides reserved interface variables: "
            f"{sorted(collisions)!r}."
        )
    authored_env.update(reserved)
    workdir = paths.app if profile.cwd == "." else f"{paths.app}/{profile.cwd}"
    try:
        invocation = EnvironmentPreviewInvocation(
            command=profile.command,
            authored_cwd=profile.cwd,
            workdir=workdir,
            environment=authored_env,
        )
    except (TypeError, ValueError) as error:
        raise RealmConflict(
            f"Environment Preview profile environment is unsafe: {error}"
        ) from error

    runtime_fingerprint = request_digest(
        {
            "effective_runtime": runtime.to_dict(),
            "prepared_runtime_digest": closure.prepared_runtime.digest,
            "schema": "optpilot.environment-preview-runtime-fingerprint.v1",
        }
    )
    fingerprints = EnvironmentPreviewFingerprints(
        source=environment.digest,
        runtime=runtime_fingerprint,
        candidate=request_digest(target.candidate.to_dict()),
        evaluation=closure.evaluation_template.digest,
        run_definition=target.run_definition.digest,
        selection=target.selection.selection_digest,
    )
    parameter_spec = (
        _bounded_parameter_spec(evaluation_spec.candidate["spec"])
        if candidate_format == "parameters"
        else None
    )
    return EnvironmentPreviewPlan.build(
        profile_id=profile.profile_id,
        accepts=profile.accepts,
        selection=target.selection,
        invocation=invocation,
        runtime=runtime,
        resources=EnvironmentPreviewResourceClaims.from_profile(profile.resources),
        timeout_seconds=profile.timeout_seconds,
        presentation=profile.presentation,
        paths=paths,
        fingerprints=fingerprints,
        candidate_format=candidate_format,
        candidate_ref=str(target.candidate.candidate_ref),
        parameter_spec=parameter_spec,
        candidate_root=(
            ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT
            if candidate_format == "files"
            else None
        ),
        outputs_enabled=profile.outputs,
    )


def _select_profile(
    profiles: Sequence[InterfaceLaunchProfile], profile_id: str | None
) -> InterfaceLaunchProfile:
    profile_list = tuple(profiles)
    if not profile_list:
        raise RealmConflict(
            "The retained environment revision declares no Preview profiles."
        )
    requested = "" if profile_id is None else profile_id.strip()
    if requested:
        for profile in profile_list:
            if profile.profile_id == requested:
                return profile
        available = ", ".join(profile.profile_id for profile in profile_list)
        raise RealmConflict(
            f"Unknown retained Environment Preview profile {requested!r}; "
            f"available profiles: {available}."
        )
    for profile in profile_list:
        if profile.profile_id == "default":
            return profile
    if len(profile_list) == 1:
        return profile_list[0]
    available = ", ".join(profile.profile_id for profile in profile_list)
    raise RealmConflict(
        "profile_id is required when the retained environment declares multiple "
        f"named Preview profiles; available profiles: {available}."
    )


def _validate_profile_compatibility(profile: InterfaceLaunchProfile) -> None:
    if "candidate" not in profile.accepts.selection_kinds:
        raise RealmConflict(
            f"Environment Preview profile {profile.profile_id!r} does not accept "
            "candidate selections."
        )
    media_types = profile.accepts.media_types
    if media_types and ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE not in media_types:
        raise RealmConflict(
            f"Environment Preview profile {profile.profile_id!r} does not accept "
            f"{ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE!r}."
        )
    if profile.presentation.kind != _PRESENTATION_KIND:
        raise RealmConflict("Environment Preview currently supports only web presentation.")
    if 1 + len(profile.presentation.extra_ports) > _MAX_PLAN_PORTS:
        raise RealmConflict(
            f"Environment Preview profile {profile.profile_id!r} requests more "
            f"than {_MAX_PLAN_PORTS} container ports."
        )
    if profile.grants.network != "disabled":
        raise RealmConflict(
            "Environment Preview first release requires denied container network access."
        )
    if profile.grants.env_from_host:
        raise RealmConflict(
            "Environment Preview first release does not support host environment variables."
        )
    if profile.grants.secrets_from_host:
        raise RealmConflict(
            "Environment Preview first release does not support host secrets."
        )
    if profile.runtime.setup is not None:
        raise RealmConflict(
            "Environment Preview first release does not run profile setup steps."
        )
    if profile.runtime.sandbox == "process":
        raise RealmConflict(
            "Environment Preview first release requires an enforceable container runtime."
        )


def _effective_container_runtime(
    target: ResolvedCandidateInspectionTarget,
    profile: InterfaceLaunchProfile,
) -> EnvironmentPreviewContainerRuntime:
    prepared = target.evaluation.closure.prepared_runtime
    container = profile.runtime.container
    if profile.runtime.sandbox == "container":
        if container is None:  # pragma: no cover - profile invariant
            raise RealmConflict("Container Preview profile has no container settings.")
        if container.build is not None:
            raise RealmConflict(
                "Environment Preview first release does not build container images."
            )
        if container.image is None:  # pragma: no cover - profile invariant
            raise RealmConflict("Container Preview profile has no image.")
        try:
            return EnvironmentPreviewContainerRuntime(
                image_ref=container.image,
                prepared_runtime_digest=prepared.digest,
                source="interface-profile",
                engine=container.engine,
                platform=(
                    container.platform
                    or (
                        prepared.platform
                        if prepared.runtime_kind == "container"
                        else None
                    )
                ),
            )
        except ValueError as error:
            raise RealmConflict(str(error)) from error

    if profile.runtime.sandbox is not None:  # pragma: no cover - enum invariant
        raise RealmConflict("Environment Preview profile runtime is unsupported.")
    if prepared.portability != "portable":
        raise RealmConflict(
            "Environment Preview can inherit only a portable retained prepared runtime."
        )
    if prepared.runtime_kind != "container" or prepared.oci_image_digest is None:
        raise RealmConflict(
            "Environment Preview profile omits a container override, but the exact "
            "retained prepared runtime is not a sha256-pinned container."
        )
    return EnvironmentPreviewContainerRuntime(
        image_ref=prepared.oci_image_digest,
        prepared_runtime_digest=prepared.digest,
        source="prepared-runtime",
        platform=prepared.platform,
    )


__all__ = [
    "ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE",
    "ENVIRONMENT_PREVIEW_CONTEXT_SCHEMA",
    "ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT",
    "ENVIRONMENT_PREVIEW_PLAN_SCHEMA",
    "EnvironmentPreviewContainerRuntime",
    "EnvironmentPreviewContext",
    "EnvironmentPreviewFingerprints",
    "EnvironmentPreviewInvocation",
    "EnvironmentPreviewLogicalPaths",
    "EnvironmentPreviewPlan",
    "EnvironmentPreviewResourceClaims",
    "compile_environment_preview_plan",
]
