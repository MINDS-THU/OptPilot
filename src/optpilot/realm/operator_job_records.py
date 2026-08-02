"""Path-free durable records for supervised Operator Jobs.

An Operator Job is the common control-plane envelope for Studio-started work.
The records in this module deliberately describe immutable logical facts and
provider-neutral lifecycle evidence.  Host paths, process ids, ports, secret
values, and provider-private backend tokens belong to execution bindings and
supervisor registries, never to this internal durable ledger projection.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from ._validation import (
    finite_time,
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    optional_text,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .refs import canonical_json_bytes, parse_physical_content_ref
from .selections import SelectionRef


JsonDict = Dict[str, Any]
OPERATOR_JOB_PLAN_SCHEMA = "optpilot.operator-job-plan.v2"
OPERATOR_JOB_RESULT_SCHEMA = "optpilot.operator-job-result.v1"
OPERATOR_JOB_CLEANUP_EVIDENCE_SCHEMA = "optpilot.operator-job-cleanup-evidence.v1"
OPERATOR_JOB_OUTPUT_ROLE = "operator-job-output"
MAX_OPERATOR_JOB_PLAN_BYTES = 1024 * 1024
MAX_OPERATOR_JOB_RESULT_BYTES = 1024 * 1024
MAX_OPERATOR_JOB_RESULT_ITEMS = 1024
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "absolute_path",
        "canonical_path",
        "cwd",
        "host_path",
        "host_paths",
        "root_path",
        "workspace_path",
        "working_directory",
    }
)


def _path_free_identifier(value: Any, label: str, *, max_bytes: int = 512) -> str:
    result = required_text(value, label, max_bytes=max_bytes)
    if (
        "/" in result
        or "\\" in result
        or result.startswith((".", "~"))
        or (len(result) >= 2 and result[1] == ":" and result[0].isalpha())
    ):
        raise ValueError(f"{label} must be a path-free logical identifier.")
    return result


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def operator_job_id(operation_id: str) -> str:
    """Return the deterministic id for one idempotent planning operation."""

    operation_id = required_text(operation_id, "operator job operation id")
    return f"operator-job-{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:32]}"


class OperatorJobState(str, Enum):
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            OperatorJobState.SUCCEEDED,
            OperatorJobState.FAILED,
            OperatorJobState.CANCELLED,
        }


class OperatorJobReconciliationState(str, Enum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    DEGRADED = "degraded"


class OperatorJobCleanupState(str, Enum):
    """Independent debt state for post-terminal external cleanup.

    Cleanup cannot be required before a terminal outcome exists.  A terminal
    commit creates durable ``pending`` debt; only a separately fenced cleanup
    receipt moves it to ``complete``.  Failures intentionally remain pending
    instead of being hidden behind a vague degraded state.
    """

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETE = "complete"


class OperatorJobCleanupComponentState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"


@dataclass(frozen=True, order=True)
class OperatorJobCleanupComponentEvidence:
    state: OperatorJobCleanupComponentState
    evidence_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, OperatorJobCleanupComponentState):
            raise ValueError("operator job cleanup component state is invalid.")
        if self.state is OperatorJobCleanupComponentState.COMPLETE:
            lower_hex_digest(
                self.evidence_digest,
                "operator job cleanup component evidence digest",
            )
        elif self.evidence_digest is not None:
            raise ValueError(
                "not-applicable operator job cleanup component has evidence."
            )

    def to_dict(self) -> JsonDict:
        return {
            "evidence_digest": self.evidence_digest,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "OperatorJobCleanupComponentEvidence":
        try:
            if set(payload) != {"evidence_digest", "state"}:
                raise ValueError("operator job cleanup component fields differ.")
            result = cls(
                state=OperatorJobCleanupComponentState(payload["state"]),
                evidence_digest=payload["evidence_digest"],
            )
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                dict(payload)
            ):
                raise ValueError("operator job cleanup component is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted operator job cleanup component is malformed."
            ) from error


@dataclass(frozen=True)
class OperatorJobCleanupEvidence:
    """Path-free evidence that every applicable cleanup phase completed."""

    terminal_revision: int
    terminal_outcome_digest: str
    provider: OperatorJobCleanupComponentEvidence
    resources: OperatorJobCleanupComponentEvidence
    capacity: OperatorJobCleanupComponentEvidence
    admission: OperatorJobCleanupComponentEvidence
    schema_version: str = OPERATOR_JOB_CLEANUP_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        nonnegative_int(self.terminal_revision, "operator job terminal revision")
        lower_hex_digest(
            self.terminal_outcome_digest,
            "operator job terminal outcome digest",
        )
        if self.schema_version != OPERATOR_JOB_CLEANUP_EVIDENCE_SCHEMA:
            raise ValueError("operator job cleanup evidence schema is unsupported.")
        for name in ("provider", "resources", "capacity", "admission"):
            if not isinstance(
                getattr(self, name), OperatorJobCleanupComponentEvidence
            ):
                raise TypeError(
                    f"operator job cleanup {name} evidence has an invalid type."
                )
        if self.provider.state is not OperatorJobCleanupComponentState.COMPLETE:
            raise ValueError("operator job provider cleanup must be complete.")

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "admission": self.admission.to_dict(),
            "capacity": self.capacity.to_dict(),
            "provider": self.provider.to_dict(),
            "resources": self.resources.to_dict(),
            "schema_version": self.schema_version,
            "terminal_outcome_digest": self.terminal_outcome_digest,
            "terminal_revision": self.terminal_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobCleanupEvidence":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job cleanup evidence fields differ.")
            result = cls(
                terminal_revision=payload["terminal_revision"],
                terminal_outcome_digest=payload["terminal_outcome_digest"],
                provider=OperatorJobCleanupComponentEvidence.from_dict(
                    payload["provider"]
                ),
                resources=OperatorJobCleanupComponentEvidence.from_dict(
                    payload["resources"]
                ),
                capacity=OperatorJobCleanupComponentEvidence.from_dict(
                    payload["capacity"]
                ),
                admission=OperatorJobCleanupComponentEvidence.from_dict(
                    payload["admission"]
                ),
                schema_version=payload["schema_version"],
            )
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                dict(payload)
            ):
                raise ValueError("operator job cleanup evidence is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted operator job cleanup evidence is malformed."
            ) from error


@dataclass(frozen=True)
class OperatorJobCleanupRecord:
    job_id: str
    evidence: OperatorJobCleanupEvidence
    evidence_digest: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job cleanup job id")
        if not isinstance(self.evidence, OperatorJobCleanupEvidence):
            raise TypeError("operator job cleanup evidence is invalid.")
        lower_hex_digest(self.evidence_digest, "operator job cleanup evidence digest")
        if self.evidence_digest != self.evidence.digest:
            raise ValueError("operator job cleanup evidence digest is inconsistent.")
        required_text(
            self.created_by_principal_id,
            "operator job cleanup principal id",
        )
        positive_int(self.created_txn_id, "operator job cleanup transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "operator job cleanup time"),
        )

    def to_dict(self) -> JsonDict:
        return {
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "evidence": self.evidence.to_dict(),
            "evidence_digest": self.evidence_digest,
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobCleanupRecord":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job cleanup record fields differ.")
            return cls(
                job_id=payload["job_id"],
                evidence=OperatorJobCleanupEvidence.from_dict(payload["evidence"]),
                evidence_digest=payload["evidence_digest"],
                created_by_principal_id=payload["created_by_principal_id"],
                created_txn_id=payload["created_txn_id"],
                created_at=payload["created_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted operator job cleanup record is malformed."
            ) from error


class OperatorJobTerminalStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperatorJobTerminalDisposition(str, Enum):
    NEVER_STARTED = "never_started"
    EXITED = "exited"
    KILLED = "killed"


@dataclass(frozen=True, order=True)
class OperatorJobTarget:
    """One immutable logical target without a realization coordinate."""

    kind: str
    selection: SelectionRef

    def __post_init__(self) -> None:
        _path_free_identifier(self.kind, "operator job target kind", max_bytes=128)
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("operator job target selection must be SelectionRef.")
        for field in ("source_id", "entity_id", "entity_ref"):
            _path_free_identifier(
                getattr(self.selection, field),
                f"operator job selection {field}",
                max_bytes=512,
            )

    @property
    def selection_digest(self) -> str:
        return self.selection.selection_digest

    @property
    def source_owner_id(self) -> str:
        return self.selection.source_owner_id

    def to_dict(self) -> JsonDict:
        return {
            "kind": self.kind,
            "selection": self.selection.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobTarget":
        try:
            if set(payload) != {"kind", "selection"}:
                raise ValueError("operator job target has unknown or missing fields.")
            result = cls(
                kind=payload["kind"],
                selection=SelectionRef.from_dict(payload["selection"]),
            )
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
                raise ValueError("operator job target is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("Persisted operator job target is malformed.") from error


@dataclass(frozen=True)
class OperatorJobLaunchPlan:
    """Exact approval unit for any supervised Studio-started execution.

    ``resource_claims`` uses provider-neutral integer units keyed by logical
    resource name (for example ``cpu_millis`` or ``gpu_count``).  Provider
    placement and host coordinates are resolved only after approval.
    """

    job_kind: str
    target: OperatorJobTarget
    input_facts: Mapping[str, Any]
    input_facts_digest: str
    owner_derivation_manifest_digest: str
    source_fingerprints: Tuple[str, ...]
    runtime_fingerprint: str
    entrypoint_profile: str
    projection_contract_digest: str
    backend_kind: str
    backend_realm: str
    resource_claims: Mapping[str, int]
    timeout_seconds: float
    network_policy: str
    network_enforcement: str
    requested_secret_names: Tuple[str, ...]
    grants_digest: str
    evidence_sink_kind: str
    evidence_sink_id: str
    evidence_sink_digest: str
    cancellation_guarantee: str
    priority_class: str
    schema_version: str = OPERATOR_JOB_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != OPERATOR_JOB_PLAN_SCHEMA:
            raise ValueError("operator job plan schema version is unsupported.")
        _path_free_identifier(self.job_kind, "operator job kind", max_bytes=128)
        if not isinstance(self.target, OperatorJobTarget):
            raise TypeError("operator job target must be OperatorJobTarget.")
        if not isinstance(self.input_facts, Mapping):
            raise TypeError("operator job input facts must be a mapping.")
        input_facts = _portable_result_json(
            self.input_facts, label="operator job input facts"
        )
        object.__setattr__(self, "input_facts", input_facts)
        lower_hex_digest(self.input_facts_digest, "operator job input facts digest")
        if self.input_facts_digest != _canonical_digest(thaw_json(input_facts)):
            raise ValueError("operator job input facts digest is inconsistent.")
        lower_hex_digest(
            self.owner_derivation_manifest_digest,
            "operator job owner derivation manifest digest",
        )
        fingerprints = tuple(self.source_fingerprints)
        for value in fingerprints:
            lower_hex_digest(value, "operator job source fingerprint")
        if not fingerprints or fingerprints != tuple(sorted(set(fingerprints))):
            raise ValueError(
                "operator job source fingerprints must be non-empty, unique, and sorted."
            )
        object.__setattr__(self, "source_fingerprints", fingerprints)
        lower_hex_digest(self.runtime_fingerprint, "operator job runtime fingerprint")
        _path_free_identifier(
            self.entrypoint_profile, "operator job entrypoint profile", max_bytes=256
        )
        lower_hex_digest(
            self.projection_contract_digest,
            "operator job projection contract digest",
        )
        _path_free_identifier(
            self.backend_kind, "operator job backend kind", max_bytes=128
        )
        _path_free_identifier(
            self.backend_realm, "operator job backend realm", max_bytes=256
        )
        claims = freeze_json(dict(self.resource_claims), label="operator job resources")
        if not isinstance(claims, Mapping) or not claims:
            raise ValueError("operator job resource claims must not be empty.")
        normalized_claims: Dict[str, int] = {}
        for name, amount in claims.items():
            _path_free_identifier(name, "operator job resource name", max_bytes=128)
            positive_int(amount, f"operator job resource {name}")
            normalized_claims[name] = amount
        object.__setattr__(self, "resource_claims", MappingProxyType(normalized_claims))
        timeout = finite_time(self.timeout_seconds, "operator job timeout")
        if timeout <= 0:
            raise ValueError("operator job timeout must be positive.")
        object.__setattr__(self, "timeout_seconds", timeout)
        if self.network_policy not in {"denied", "restricted", "unrestricted"}:
            raise ValueError("operator job network policy is unsupported.")
        if self.network_enforcement not in {"advisory", "enforced"}:
            raise ValueError("operator job network enforcement is unsupported.")
        secrets = tuple(
            required_text(item, "operator job requested secret name", max_bytes=256)
            for item in self.requested_secret_names
        )
        if secrets != tuple(sorted(set(secrets))):
            raise ValueError(
                "operator job requested secret names must be unique and sorted."
            )
        object.__setattr__(self, "requested_secret_names", secrets)
        lower_hex_digest(self.grants_digest, "operator job grants digest")
        _path_free_identifier(
            self.evidence_sink_kind, "operator job evidence sink kind", max_bytes=128
        )
        _path_free_identifier(
            self.evidence_sink_id, "operator job evidence sink id", max_bytes=512
        )
        lower_hex_digest(
            self.evidence_sink_digest, "operator job evidence sink digest"
        )
        if self.cancellation_guarantee not in {
            "confirmed",
            "best_effort",
            "unconfirmed",
            "handoff-routed",
        }:
            raise ValueError("operator job cancellation guarantee is unsupported.")
        _path_free_identifier(
            self.priority_class, "operator job priority class", max_bytes=128
        )
        if len(canonical_json_bytes(self.to_dict())) > MAX_OPERATOR_JOB_PLAN_BYTES:
            raise ValueError("operator job launch plan exceeds its encoded byte limit.")

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "backend_kind": self.backend_kind,
            "backend_realm": self.backend_realm,
            "cancellation_guarantee": self.cancellation_guarantee,
            "entrypoint_profile": self.entrypoint_profile,
            "evidence_sink_digest": self.evidence_sink_digest,
            "evidence_sink_id": self.evidence_sink_id,
            "evidence_sink_kind": self.evidence_sink_kind,
            "grants_digest": self.grants_digest,
            "input_facts": thaw_json(self.input_facts),
            "input_facts_digest": self.input_facts_digest,
            "job_kind": self.job_kind,
            "network_enforcement": self.network_enforcement,
            "network_policy": self.network_policy,
            "owner_derivation_manifest_digest": self.owner_derivation_manifest_digest,
            "priority_class": self.priority_class,
            "projection_contract_digest": self.projection_contract_digest,
            "requested_secret_names": list(self.requested_secret_names),
            "resource_claims": thaw_json(self.resource_claims),
            "runtime_fingerprint": self.runtime_fingerprint,
            "schema_version": self.schema_version,
            "source_fingerprints": list(self.source_fingerprints),
            "target": self.target.to_dict(),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobLaunchPlan":
        try:
            expected = set(cls.__dataclass_fields__)
            if set(payload) != expected:
                raise ValueError("operator job launch plan has unknown or missing fields.")
            values = dict(payload)
            values["target"] = OperatorJobTarget.from_dict(values["target"])
            values["source_fingerprints"] = tuple(values["source_fingerprints"])
            values["requested_secret_names"] = tuple(values["requested_secret_names"])
            result = cls(**values)
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
                raise ValueError("operator job launch plan is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted operator job launch plan is malformed."
            ) from error


@dataclass(frozen=True, order=True)
class OperatorJobApprovalRecord:
    job_id: str
    plan_digest: str
    approval_scope_digest: str
    approved_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job approval job id")
        lower_hex_digest(self.plan_digest, "operator job approval plan digest")
        lower_hex_digest(
            self.approval_scope_digest, "operator job approval scope digest"
        )
        required_text(
            self.approved_by_principal_id, "operator job approval principal id"
        )
        positive_int(self.created_txn_id, "operator job approval transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job approval time")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobApprovalRecord":
        return _persisted_record(cls, payload, "operator job approval")


@dataclass(frozen=True, order=True)
class OperatorJobLaunchIntentRecord:
    job_id: str
    plan_digest: str
    capacity_reservation_id: str
    capacity_holder_id: str
    capacity_fencing_token: int
    admission_lease_id: str
    admission_holder_id: str
    admission_fencing_token: int
    binding_id: str
    launch_token: str
    provider_kind: str
    evidence_fingerprint: str
    launch_request_digest: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job launch job id")
        lower_hex_digest(self.plan_digest, "operator job launch plan digest")
        _path_free_identifier(
            self.capacity_reservation_id,
            "operator job capacity reservation id",
        )
        _path_free_identifier(
            self.capacity_holder_id,
            "operator job capacity holder id",
            max_bytes=512,
        )
        positive_int(
            self.capacity_fencing_token,
            "operator job capacity fencing token",
        )
        required_text(self.admission_lease_id, "operator job admission lease id")
        _path_free_identifier(
            self.admission_holder_id,
            "operator job admission holder id",
            max_bytes=512,
        )
        positive_int(
            self.admission_fencing_token, "operator job admission fencing token"
        )
        _path_free_identifier(self.binding_id, "operator job binding id")
        _path_free_identifier(self.launch_token, "operator job launch token")
        _path_free_identifier(
            self.provider_kind, "operator job provider kind", max_bytes=128
        )
        lower_hex_digest(
            self.evidence_fingerprint, "operator job execution evidence fingerprint"
        )
        lower_hex_digest(
            self.launch_request_digest, "operator job launch request digest"
        )
        required_text(
            self.created_by_principal_id, "operator job launch creator principal id"
        )
        positive_int(self.created_txn_id, "operator job launch transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job launch time")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobLaunchIntentRecord":
        return _persisted_record(cls, payload, "operator job launch intent")


@dataclass(frozen=True, order=True)
class OperatorJobStopRecord:
    job_id: str
    reason_code: str
    requested_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job stop job id")
        _path_free_identifier(
            self.reason_code, "operator job stop reason code", max_bytes=128
        )
        required_text(
            self.requested_by_principal_id, "operator job stop principal id"
        )
        positive_int(self.created_txn_id, "operator job stop transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job stop time")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobStopRecord":
        return _persisted_record(cls, payload, "operator job stop")


def _portable_result_json(value: Any, *, label: str, depth: int = 0) -> Any:
    """Freeze bounded JSON while rejecting provider-private path material."""

    if depth > 24:
        raise ValueError(f"{label} exceeds the maximum nesting depth.")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number.")
        return value
    if isinstance(value, str):
        required_text(value, label, max_bytes=16 * 1024)
        lowered = value.casefold()
        portable_logical_path = (
            (
                label.endswith(".logical_path")
                and value.startswith("/optpilot/")
            )
            or (
                label.startswith("operator job input facts.preview_plan.")
                and value.startswith("/optpilot/interface/")
            )
            or (
                label
                == "operator job input facts.preview_plan.presentation.readyPath"
                and value.startswith("/")
                and not value.startswith("//")
                and ".." not in value.split("/")
            )
        ) and "\\" not in value
        if (
            (value.startswith(("/", "~/", "~\\")) and not portable_logical_path)
            or _WINDOWS_ABSOLUTE.match(value)
            or lowered.startswith("file://")
        ):
            raise ValueError(f"{label} must not contain an absolute host path.")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_OPERATOR_JOB_RESULT_ITEMS:
            raise ValueError(f"{label} contains too many fields.")
        result: Dict[str, Any] = {}
        for key, child in value.items():
            key = required_text(key, f"{label} key", max_bytes=256)
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_RESULT_KEYS or normalized.endswith(
                ("_host_path", "_absolute_path")
            ):
                raise ValueError(f"{label} contains a provider-private path field.")
            result[key] = _portable_result_json(
                child, label=f"{label}.{key}", depth=depth + 1
            )
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_OPERATOR_JOB_RESULT_ITEMS:
            raise ValueError(f"{label} contains too many items.")
        return tuple(
            _portable_result_json(child, label=label, depth=depth + 1)
            for child in value
        )
    raise ValueError(f"{label} contains a non-JSON value.")


@dataclass(frozen=True, order=True)
class OperatorJobDeclaredOutput:
    declaration_id: str
    name: str
    kind: str
    content_ref: str
    size_bytes: int
    identity_digest: str
    media_type: Optional[str] = None

    def __post_init__(self) -> None:
        _path_free_identifier(
            self.declaration_id, "operator job output declaration id"
        )
        required_text(self.name, "operator job output name", max_bytes=512)
        _portable_result_json(self.name, label="operator job output name")
        if self.kind not in {"file", "tree"}:
            raise ValueError("operator job output kind must be file or tree.")
        reference = parse_physical_content_ref(self.content_ref)
        if (self.kind == "file") != self.content_ref.startswith("blob:sha256:"):
            raise ValueError("operator job output kind differs from its content ref.")
        if str(reference) != self.content_ref:
            raise ValueError("operator job output content ref is not canonical.")
        nonnegative_int(self.size_bytes, "operator job output size")
        lower_hex_digest(self.identity_digest, "operator job output identity digest")
        optional_text(self.media_type, "operator job output media type", max_bytes=256)

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobDeclaredOutput":
        return _persisted_record(cls, payload, "operator job declared output")


@dataclass(frozen=True, order=True)
class OperatorJobLogMetadata:
    stream: str
    byte_count: int
    line_count: int
    truncated: bool
    content_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr", "system"}:
            raise ValueError("operator job log stream is unsupported.")
        nonnegative_int(self.byte_count, "operator job log byte count")
        nonnegative_int(self.line_count, "operator job log line count")
        if not isinstance(self.truncated, bool):
            raise TypeError("operator job log truncated must be a boolean.")
        if self.content_digest is not None:
            lower_hex_digest(
                self.content_digest, "operator job retained log content digest"
            )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobLogMetadata":
        return _persisted_record(cls, payload, "operator job log metadata")


@dataclass(frozen=True)
class OperatorJobResult:
    """Bounded job-kind-neutral evidence retained for post-restart inspection."""

    result_kind: str
    status: str
    metrics: Mapping[str, float]
    constraint_results: Mapping[str, Any]
    event_summary: Mapping[str, Any]
    declared_outputs: Tuple[OperatorJobDeclaredOutput, ...]
    logs: Tuple[OperatorJobLogMetadata, ...]
    details: Mapping[str, Any]
    schema_version: str = OPERATOR_JOB_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != OPERATOR_JOB_RESULT_SCHEMA:
            raise ValueError("operator job result schema version is unsupported.")
        _path_free_identifier(
            self.result_kind, "operator job result kind", max_bytes=128
        )
        _path_free_identifier(self.status, "operator job result status", max_bytes=128)
        if not isinstance(self.metrics, Mapping):
            raise TypeError("operator job result metrics must be a mapping.")
        if len(self.metrics) > MAX_OPERATOR_JOB_RESULT_ITEMS:
            raise ValueError("operator job result contains too many metrics.")
        metrics: Dict[str, float] = {}
        for name, value in self.metrics.items():
            name = required_text(name, "operator job metric name", max_bytes=256)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("operator job metric values must be numeric.")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError("operator job metric values must be finite.")
            metrics[name] = normalized
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        for field in ("constraint_results", "event_summary", "details"):
            value = getattr(self, field)
            if not isinstance(value, Mapping):
                raise TypeError(f"operator job result {field} must be a mapping.")
            object.__setattr__(
                self,
                field,
                _portable_result_json(value, label=f"operator job result {field}"),
            )
        outputs = tuple(self.declared_outputs)
        if len(outputs) > MAX_OPERATOR_JOB_RESULT_ITEMS or any(
            not isinstance(item, OperatorJobDeclaredOutput) for item in outputs
        ):
            raise ValueError("operator job declared outputs are invalid or unbounded.")
        if len({item.declaration_id for item in outputs}) != len(outputs):
            raise ValueError("operator job output declaration ids must be unique.")
        object.__setattr__(self, "declared_outputs", outputs)
        logs = tuple(self.logs)
        if len(logs) > 8 or any(
            not isinstance(item, OperatorJobLogMetadata) for item in logs
        ):
            raise ValueError("operator job log metadata is invalid or unbounded.")
        if len({item.stream for item in logs}) != len(logs):
            raise ValueError("operator job result contains duplicate log streams.")
        object.__setattr__(self, "logs", logs)
        if len(canonical_json_bytes(self.to_dict())) > MAX_OPERATOR_JOB_RESULT_BYTES:
            raise ValueError("operator job result exceeds its encoded byte limit.")

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "constraint_results": thaw_json(self.constraint_results),
            "declared_outputs": [item.to_dict() for item in self.declared_outputs],
            "details": thaw_json(self.details),
            "event_summary": thaw_json(self.event_summary),
            "logs": [item.to_dict() for item in self.logs],
            "metrics": dict(self.metrics),
            "result_kind": self.result_kind,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobResult":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job result has unknown or missing fields.")
            values = dict(payload)
            values["declared_outputs"] = tuple(
                OperatorJobDeclaredOutput.from_dict(item)
                for item in values["declared_outputs"]
            )
            values["logs"] = tuple(
                OperatorJobLogMetadata.from_dict(item) for item in values["logs"]
            )
            result = cls(**values)
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
                raise ValueError("operator job result is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError("Persisted operator job result is malformed.") from error


@dataclass(frozen=True)
class OperatorJobResultRecord:
    job_id: str
    result: OperatorJobResult
    result_digest: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job result job id")
        if not isinstance(self.result, OperatorJobResult):
            raise TypeError("operator job result record requires OperatorJobResult.")
        lower_hex_digest(self.result_digest, "operator job result digest")
        if self.result_digest != self.result.digest:
            raise ValueError("operator job result digest is inconsistent.")
        required_text(
            self.created_by_principal_id, "operator job result creator principal id"
        )
        positive_int(self.created_txn_id, "operator job result transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job result time")
        )

    def to_dict(self) -> JsonDict:
        return {
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "job_id": self.job_id,
            "result": self.result.to_dict(),
            "result_digest": self.result_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobResultRecord":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job result record has invalid fields.")
            return cls(
                job_id=payload["job_id"],
                result=OperatorJobResult.from_dict(payload["result"]),
                result_digest=payload["result_digest"],
                created_by_principal_id=payload["created_by_principal_id"],
                created_txn_id=payload["created_txn_id"],
                created_at=payload["created_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted operator job result record is malformed."
            ) from error


@dataclass(frozen=True, order=True)
class OperatorJobOutcome:
    status: OperatorJobTerminalStatus
    code: str
    started: bool
    disposition: OperatorJobTerminalDisposition
    terminal_proof_digest: Optional[str] = None
    evidence_digest: Optional[str] = None
    detail_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, OperatorJobTerminalStatus):
            raise ValueError("operator job outcome status is invalid.")
        _path_free_identifier(self.code, "operator job outcome code", max_bytes=128)
        if not isinstance(self.started, bool):
            raise TypeError("operator job outcome started must be a boolean.")
        if not isinstance(self.disposition, OperatorJobTerminalDisposition):
            raise ValueError("operator job outcome disposition is invalid.")
        if self.started != (
            self.disposition is not OperatorJobTerminalDisposition.NEVER_STARTED
        ):
            raise ValueError(
                "operator job outcome started differs from its disposition."
            )
        if self.status is OperatorJobTerminalStatus.SUCCEEDED and (
            not self.started
            or self.disposition is not OperatorJobTerminalDisposition.EXITED
        ):
            raise ValueError("a succeeded operator job must have exited after starting.")
        for field in (
            "terminal_proof_digest",
            "evidence_digest",
            "detail_digest",
        ):
            value = getattr(self, field)
            if value is not None:
                lower_hex_digest(value, f"operator job outcome {field}")

    def to_dict(self) -> JsonDict:
        return {
            "code": self.code,
            "detail_digest": self.detail_digest,
            "disposition": self.disposition.value,
            "evidence_digest": self.evidence_digest,
            "started": self.started,
            "status": self.status.value,
            "terminal_proof_digest": self.terminal_proof_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobOutcome":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job outcome has unknown or missing fields.")
            values = dict(payload)
            values["status"] = OperatorJobTerminalStatus(values["status"])
            values["disposition"] = OperatorJobTerminalDisposition(
                values["disposition"]
            )
            result = cls(**values)
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
                raise ValueError("operator job outcome is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("Persisted operator job outcome is malformed.") from error


@dataclass(frozen=True, order=True)
class OperatorJobOutcomeRecord:
    job_id: str
    outcome: OperatorJobOutcome
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job outcome job id")
        if not isinstance(self.outcome, OperatorJobOutcome):
            raise TypeError("operator job outcome record requires OperatorJobOutcome.")
        required_text(
            self.created_by_principal_id, "operator job outcome creator principal id"
        )
        positive_int(self.created_txn_id, "operator job outcome transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job outcome time")
        )

    def to_dict(self) -> JsonDict:
        return {
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "job_id": self.job_id,
            "outcome": self.outcome.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobOutcomeRecord":
        try:
            if set(payload) != set(cls.__dataclass_fields__):
                raise ValueError("operator job outcome record has invalid fields.")
            return cls(
                job_id=payload["job_id"],
                outcome=OperatorJobOutcome.from_dict(payload["outcome"]),
                created_by_principal_id=payload["created_by_principal_id"],
                created_txn_id=payload["created_txn_id"],
                created_at=payload["created_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted operator job outcome record is malformed."
            ) from error


@dataclass(frozen=True, order=True)
class OperatorJobRevisionRecord:
    job_id: str
    revision: int
    state: OperatorJobState
    reconciliation_state: OperatorJobReconciliationState
    cleanup_state: OperatorJobCleanupState
    operation_kind: str
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job revision job id")
        nonnegative_int(self.revision, "operator job revision")
        if not isinstance(self.state, OperatorJobState):
            raise ValueError("operator job revision state is invalid.")
        if not isinstance(
            self.reconciliation_state, OperatorJobReconciliationState
        ):
            raise ValueError("operator job reconciliation state is invalid.")
        if not isinstance(self.cleanup_state, OperatorJobCleanupState):
            raise ValueError("operator job cleanup state is invalid.")
        required_text(
            self.operation_kind, "operator job revision operation kind", max_bytes=128
        )
        positive_int(self.txn_id, "operator job revision transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job revision time")
        )

    def to_dict(self) -> JsonDict:
        result = dict(self.__dict__)
        result["state"] = self.state.value
        result["reconciliation_state"] = self.reconciliation_state.value
        result["cleanup_state"] = self.cleanup_state.value
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobRevisionRecord":
        try:
            values = dict(payload)
            values["state"] = OperatorJobState(values["state"])
            values["reconciliation_state"] = OperatorJobReconciliationState(
                values["reconciliation_state"]
            )
            values["cleanup_state"] = OperatorJobCleanupState(
                values["cleanup_state"]
            )
            return cls(**values)
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted operator job revision is malformed."
            ) from error


@dataclass(frozen=True)
class OperatorJobRecord:
    job_id: str
    owner_id: str
    plan: OperatorJobLaunchPlan
    plan_digest: str
    state: OperatorJobState
    reconciliation_state: OperatorJobReconciliationState
    cleanup_state: OperatorJobCleanupState
    revision: int
    created_by_principal_id: str
    created_txn_id: int
    created_at: float
    updated_at: float
    approval: Optional[OperatorJobApprovalRecord] = None
    launch_intent: Optional[OperatorJobLaunchIntentRecord] = None
    stop: Optional[OperatorJobStopRecord] = None
    outcome: Optional[OperatorJobOutcomeRecord] = None
    result: Optional[OperatorJobResultRecord] = None
    cleanup: Optional[OperatorJobCleanupRecord] = None

    def __post_init__(self) -> None:
        required_text(self.job_id, "operator job id")
        required_text(self.owner_id, "operator job owner id")
        if not isinstance(self.plan, OperatorJobLaunchPlan):
            raise TypeError("operator job plan must be OperatorJobLaunchPlan.")
        if self.owner_id == self.plan.target.source_owner_id:
            raise ValueError(
                "operator job resources require a distinct derived job owner."
            )
        lower_hex_digest(self.plan_digest, "operator job plan digest")
        if self.plan_digest != self.plan.digest:
            raise ValueError("operator job plan digest is inconsistent.")
        if not isinstance(self.state, OperatorJobState):
            raise ValueError("operator job state is invalid.")
        if not isinstance(
            self.reconciliation_state, OperatorJobReconciliationState
        ):
            raise ValueError("operator job reconciliation state is invalid.")
        if not isinstance(self.cleanup_state, OperatorJobCleanupState):
            raise ValueError("operator job cleanup state is invalid.")
        nonnegative_int(self.revision, "operator job revision")
        required_text(
            self.created_by_principal_id, "operator job creator principal id"
        )
        positive_int(self.created_txn_id, "operator job created transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "operator job created_at")
        )
        object.__setattr__(
            self, "updated_at", finite_time(self.updated_at, "operator job updated_at")
        )
        if self.updated_at < self.created_at:
            raise ValueError("operator job updated_at precedes created_at.")
        if self.approval is not None and (
            self.approval.job_id != self.job_id
            or self.approval.plan_digest != self.plan_digest
        ):
            raise ValueError("operator job approval differs from its job.")
        if self.launch_intent is not None and (
            self.launch_intent.job_id != self.job_id
            or self.launch_intent.plan_digest != self.plan_digest
        ):
            raise ValueError("operator job launch intent differs from its job.")
        if self.stop is not None and self.stop.job_id != self.job_id:
            raise ValueError("operator job stop differs from its job.")
        if self.outcome is not None and self.outcome.job_id != self.job_id:
            raise ValueError("operator job outcome differs from its job.")
        if self.result is not None and self.result.job_id != self.job_id:
            raise ValueError("operator job result differs from its job.")
        if self.cleanup is not None and self.cleanup.job_id != self.job_id:
            raise ValueError("operator job cleanup receipt differs from its job.")
        if self.stop is not None and self.state not in {
            OperatorJobState.STOPPING,
            OperatorJobState.CANCELLED,
        }:
            raise ValueError("operator job stop request appears in an invalid state.")
        if self.state in {
            OperatorJobState.STOPPING,
            OperatorJobState.CANCELLED,
        } and self.stop is None:
            raise ValueError("stopping or cancelled operator job requires a stop request.")
        prelaunch = self.state in {
            OperatorJobState.PLANNED,
            OperatorJobState.AWAITING_APPROVAL,
            OperatorJobState.QUEUED,
        }
        if prelaunch and self.launch_intent is not None:
            raise ValueError("pre-launch operator job has a launch intent.")
        if self.state in {
            OperatorJobState.QUEUED,
            OperatorJobState.STARTING,
            OperatorJobState.RUNNING,
            OperatorJobState.STOPPING,
            OperatorJobState.SUCCEEDED,
            OperatorJobState.FAILED,
        } and self.approval is None:
            raise ValueError("approved operator job state is missing approval evidence.")
        if self.state in {
            OperatorJobState.STARTING,
            OperatorJobState.RUNNING,
            OperatorJobState.STOPPING,
            OperatorJobState.SUCCEEDED,
        } and self.launch_intent is None:
            raise ValueError("started operator job state is missing launch intent.")
        if self.state.terminal != (self.outcome is not None):
            raise ValueError("operator job terminal state and outcome differ.")
        if self.outcome is not None:
            if self.outcome.outcome.status.value != self.state.value:
                raise ValueError("operator job state differs from terminal outcome.")
            if self.launch_intent is not None:
                if self.outcome.outcome.terminal_proof_digest is None:
                    raise ValueError("launched operator job outcome requires terminal proof.")
            elif (
                self.state is not OperatorJobState.CANCELLED
                or self.outcome.outcome.started
                or self.outcome.outcome.terminal_proof_digest is not None
            ):
                raise ValueError("only an unstarted cancellation may omit launch proof.")
        if self.launch_intent is not None and self.state.terminal:
            if self.result is None:
                raise ValueError("launched terminal operator job requires retained result.")
            if self.result.created_txn_id != self.outcome.created_txn_id:
                raise ValueError("operator job result and outcome were not committed together.")
            if (
                self.result.created_by_principal_id
                != self.outcome.created_by_principal_id
                or self.result.created_at != self.outcome.created_at
                or self.result.result_digest
                != self.outcome.outcome.evidence_digest
            ):
                raise ValueError(
                    "operator job result differs from its exact terminal outcome."
                )
        elif self.result is not None:
            raise ValueError("only a launched terminal operator job may retain a result.")
        if prelaunch:
            valid_reconciliation = {
                OperatorJobReconciliationState.NOT_STARTED
            }
        elif self.state in {OperatorJobState.STARTING, OperatorJobState.RUNNING}:
            valid_reconciliation = {OperatorJobReconciliationState.PENDING}
        elif self.state is OperatorJobState.STOPPING:
            valid_reconciliation = {
                OperatorJobReconciliationState.PENDING,
                OperatorJobReconciliationState.UNCONFIRMED,
                OperatorJobReconciliationState.DEGRADED,
            }
        else:
            valid_reconciliation = {OperatorJobReconciliationState.CONFIRMED}
        if self.reconciliation_state not in valid_reconciliation:
            raise ValueError("operator job reconciliation state is inconsistent.")
        if not self.state.terminal:
            if (
                self.cleanup_state is not OperatorJobCleanupState.NOT_REQUIRED
                or self.cleanup is not None
            ):
                raise ValueError("nonterminal operator job cannot have cleanup debt.")
        elif self.cleanup_state is OperatorJobCleanupState.PENDING:
            if self.cleanup is not None:
                raise ValueError("pending operator job cleanup has a receipt.")
        elif self.cleanup_state is OperatorJobCleanupState.COMPLETE:
            if self.cleanup is None:
                raise ValueError("completed operator job cleanup lacks a receipt.")
            if self.cleanup.evidence.terminal_revision + 1 != self.revision:
                raise ValueError(
                    "operator job cleanup receipt is not the next terminal event."
                )
            if self.outcome is None or self.cleanup.evidence.terminal_outcome_digest != (
                _canonical_digest(self.outcome.to_dict())
            ):
                raise ValueError(
                    "operator job cleanup receipt differs from its terminal outcome."
                )
        else:
            raise ValueError("terminal operator job cleanup state is invalid.")

    def to_dict(self) -> JsonDict:
        return {
            "approval": None if self.approval is None else self.approval.to_dict(),
            "cleanup": None if self.cleanup is None else self.cleanup.to_dict(),
            "cleanup_state": self.cleanup_state.value,
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "job_id": self.job_id,
            "launch_intent": (
                None if self.launch_intent is None else self.launch_intent.to_dict()
            ),
            "outcome": None if self.outcome is None else self.outcome.to_dict(),
            "owner_id": self.owner_id,
            "plan": self.plan.to_dict(),
            "plan_digest": self.plan_digest,
            "reconciliation_state": self.reconciliation_state.value,
            "result": None if self.result is None else self.result.to_dict(),
            "revision": self.revision,
            "state": self.state.value,
            "stop": None if self.stop is None else self.stop.to_dict(),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorJobRecord":
        try:
            values = dict(payload)
            receipt_version = values.pop("receipt_version", None)
            if receipt_version not in {None, 1}:
                raise ValueError("operator job receipt version is unsupported.")
            values["plan"] = OperatorJobLaunchPlan.from_dict(values["plan"])
            values["state"] = OperatorJobState(values["state"])
            values["reconciliation_state"] = OperatorJobReconciliationState(
                values["reconciliation_state"]
            )
            values["cleanup_state"] = OperatorJobCleanupState(
                values["cleanup_state"]
            )
            if values.get("approval") is not None:
                values["approval"] = OperatorJobApprovalRecord.from_dict(
                    values["approval"]
                )
            if values.get("launch_intent") is not None:
                values["launch_intent"] = OperatorJobLaunchIntentRecord.from_dict(
                    values["launch_intent"]
                )
            if values.get("stop") is not None:
                values["stop"] = OperatorJobStopRecord.from_dict(values["stop"])
            if values.get("outcome") is not None:
                values["outcome"] = OperatorJobOutcomeRecord.from_dict(
                    values["outcome"]
                )
            if values.get("result") is not None:
                values["result"] = OperatorJobResultRecord.from_dict(
                    values["result"]
                )
            if values.get("cleanup") is not None:
                values["cleanup"] = OperatorJobCleanupRecord.from_dict(
                    values["cleanup"]
                )
            return cls(**values)
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError("Persisted operator job is malformed.") from error


def _persisted_record(cls: Any, payload: Mapping[str, Any], label: str) -> Any:
    try:
        if set(payload) != set(cls.__dataclass_fields__):
            raise ValueError(f"{label} has invalid fields.")
        return cls(**dict(payload))
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError(f"Persisted {label} is malformed.") from error


__all__ = [
    "MAX_OPERATOR_JOB_PLAN_BYTES",
    "MAX_OPERATOR_JOB_RESULT_BYTES",
    "OPERATOR_JOB_PLAN_SCHEMA",
    "OPERATOR_JOB_RESULT_SCHEMA",
    "OPERATOR_JOB_CLEANUP_EVIDENCE_SCHEMA",
    "OPERATOR_JOB_OUTPUT_ROLE",
    "OperatorJobApprovalRecord",
    "OperatorJobCleanupComponentEvidence",
    "OperatorJobCleanupComponentState",
    "OperatorJobCleanupEvidence",
    "OperatorJobCleanupRecord",
    "OperatorJobCleanupState",
    "OperatorJobDeclaredOutput",
    "OperatorJobLaunchIntentRecord",
    "OperatorJobLaunchPlan",
    "OperatorJobOutcome",
    "OperatorJobOutcomeRecord",
    "OperatorJobReconciliationState",
    "OperatorJobRecord",
    "OperatorJobResult",
    "OperatorJobResultRecord",
    "OperatorJobRevisionRecord",
    "OperatorJobState",
    "OperatorJobStopRecord",
    "OperatorJobTarget",
    "OperatorJobLogMetadata",
    "OperatorJobTerminalDisposition",
    "OperatorJobTerminalStatus",
    "operator_job_id",
]
