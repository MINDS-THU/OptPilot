"""Typed durable records for one provider-realized run-attempt binding.

The portable invocation plan and its canonical evidence are intentionally
separate from Realm-local handles.  Projection realization ids, consumer
leases, and writable-volume leases are needed for fencing and cleanup, but
they must never become portable runtime identity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from ..runtime_binding import (
    ExecutionBindingEvidence,
    PortableAttemptRuntimeSpec,
)
from ._validation import finite_time, lower_hex_digest, positive_int, required_text
from .errors import RealmIntegrityError
from .refs import canonical_json_bytes, request_digest
from .run_attempt_records import RunAttemptRecord
from .run_records import RunNamespaceRecord, RunRevisionRecord


JsonDict = dict[str, Any]
_REALM_ID_NAMESPACE = uuid.UUID("a811e801-fdc1-43c8-b985-dcab229ffcea")


def _resource_coordinate_digest(
    *, run_id: str, attempt_id: str, binding_id: str, logical_name: str, kind: str
) -> str:
    return request_digest(
        {
            "attempt_id": required_text(attempt_id, "attempt id"),
            "binding_id": required_text(binding_id, "binding id"),
            "format": "optpilot.run-attempt-resource-coordinate.v1",
            "kind": required_text(kind, "resource coordinate kind", max_bytes=32),
            "logical_name": required_text(
                logical_name, "resource logical name", max_bytes=64
            ),
            "run_id": required_text(run_id, "run id"),
        }
    )


def run_attempt_projection_operation_id(
    *, run_id: str, attempt_id: str, binding_id: str, logical_name: str
) -> str:
    """Return the bounded public operation id for one private projection."""

    digest = _resource_coordinate_digest(
        run_id=run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        logical_name=logical_name,
        kind="projection",
    )
    return f"run-attempt-projection/{digest}"


def run_attempt_resource_holder_id(
    *, run_id: str, attempt_id: str, binding_id: str
) -> str:
    """Return the one deterministic provider holder for an attempt binding."""

    digest = request_digest(
        {
            "attempt_id": required_text(attempt_id, "attempt id"),
            "binding_id": required_text(binding_id, "binding id"),
            "format": "optpilot.run-attempt-resource-holder.v1",
            "run_id": required_text(run_id, "run id"),
        }
    )
    return f"run-attempt-resource-holder-{digest[:48]}"


def run_attempt_binding_operation_id(
    *, run_id: str, attempt_id: str, binding_id: str
) -> str:
    """Return the deterministic ledger commit operation for startup intent."""

    digest = request_digest(
        {
            "attempt_id": required_text(attempt_id, "attempt id"),
            "binding_id": required_text(binding_id, "binding id"),
            "format": "optpilot.run-attempt-binding-operation.v1",
            "run_id": required_text(run_id, "run id"),
        }
    )
    return f"run-attempt-binding/{digest}"


def run_attempt_terminal_evidence_operation_id(
    *,
    run_id: str,
    attempt_id: str,
    binding_id: str,
    actor_principal_id: str,
    proof_fingerprint: str,
) -> str:
    """Return the actor-scoped immutable terminal-evidence operation id."""

    digest = request_digest(
        {
            "attempt_id": required_text(attempt_id, "attempt id"),
            "actor_principal_id": required_text(
                actor_principal_id, "actor principal id"
            ),
            "binding_id": required_text(binding_id, "binding id"),
            "format": "optpilot.run-attempt-terminal-evidence-operation.v1",
            "proof_fingerprint": lower_hex_digest(
                proof_fingerprint, "terminal proof fingerprint"
            ),
            "run_id": required_text(run_id, "run id"),
        }
    )
    return f"run-attempt-terminal-evidence/{digest}"


def projection_private_coordinate_digest(
    *, realm_id: str, operation_id: str
) -> str:
    """Match the projection service's private realization coordinate."""

    return request_digest(
        {
            "format": "optpilot.projection-private-operation-coordinate.v1",
            "operation_id": required_text(operation_id, "projection operation id"),
            "realm_id": required_text(realm_id, "realm id"),
        }
    )


def run_attempt_volume_operation_id(
    *, run_id: str, attempt_id: str, binding_id: str, logical_name: str
) -> str:
    """Return the bounded public operation id for one fresh volume."""

    digest = _resource_coordinate_digest(
        run_id=run_id,
        attempt_id=attempt_id,
        binding_id=binding_id,
        logical_name=logical_name,
        kind="volume",
    )
    return f"run-attempt-volume/{digest}"


def run_attempt_volume_operational_ids(operation_id: str) -> tuple[str, str]:
    """Match public volume-service and typed ledger lease identities exactly."""

    operation_id = required_text(operation_id, "volume operation id")
    key = request_digest(
        {
            "format": "optpilot.ephemeral-volume-public-operation.v1",
            "operation_id": operation_id,
        }
    )
    volume_id = f"ephemeral-volume-{key[:48]}"
    create_operation_id = f"ephemeral-volume.create/{key}"
    usage_lease_id = (
        "ephemeral-volume-lease-"
        f"{uuid.uuid5(_REALM_ID_NAMESPACE, create_operation_id).hex}"
    )
    return volume_id, usage_lease_id


def run_attempt_volume_create_operation_id(operation_id: str) -> str:
    """Match the typed ledger operation used by the public volume service."""

    operation_id = required_text(operation_id, "volume operation id")
    key = request_digest(
        {
            "format": "optpilot.ephemeral-volume-public-operation.v1",
            "operation_id": operation_id,
        }
    )
    return f"ephemeral-volume.create/{key}"


def _exact_keys(
    payload: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _without_receipt_version(
    payload: Mapping[str, Any], label: str
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    result = dict(payload)
    version = result.pop("receipt_version", 1)
    if version != 1:
        raise ValueError(f"{label} receipt_version is unsupported.")
    return result


@dataclass(frozen=True, order=True)
class ExecutionProjectionHandle:
    """Realm-local authority for one immutable logical projection."""

    logical_name: str
    provider_kind: str
    realization_id: str
    consumer_id: str
    consumer_lease_id: str
    consumer_fencing_token: int

    def __post_init__(self) -> None:
        required_text(self.logical_name, "projection logical name", max_bytes=64)
        required_text(self.provider_kind, "projection provider kind", max_bytes=128)
        required_text(self.realization_id, "projection realization id")
        required_text(self.consumer_id, "projection consumer id")
        required_text(self.consumer_lease_id, "projection consumer lease id")
        positive_int(
            self.consumer_fencing_token,
            "projection consumer fencing token",
        )

    def to_dict(self) -> JsonDict:
        return {
            "consumer_fencing_token": self.consumer_fencing_token,
            "consumer_id": self.consumer_id,
            "consumer_lease_id": self.consumer_lease_id,
            "logical_name": self.logical_name,
            "provider_kind": self.provider_kind,
            "realization_id": self.realization_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionProjectionHandle":
        _exact_keys(
            payload,
            {
                "consumer_fencing_token",
                "consumer_id",
                "consumer_lease_id",
                "logical_name",
                "provider_kind",
                "realization_id",
            },
            "execution projection handle",
        )
        return cls(**dict(payload))


@dataclass(frozen=True, order=True)
class ExecutionVolumeHandle:
    """Realm-local authority for one fresh logical writable volume."""

    logical_name: str
    provider_kind: str
    volume_id: str
    usage_lease_id: str
    usage_fencing_token: int

    def __post_init__(self) -> None:
        required_text(self.logical_name, "volume logical name", max_bytes=64)
        required_text(self.provider_kind, "volume provider kind", max_bytes=128)
        required_text(self.volume_id, "ephemeral volume id")
        required_text(self.usage_lease_id, "ephemeral volume usage lease id")
        positive_int(
            self.usage_fencing_token,
            "ephemeral volume usage fencing token",
        )

    def to_dict(self) -> JsonDict:
        return {
            "logical_name": self.logical_name,
            "provider_kind": self.provider_kind,
            "usage_fencing_token": self.usage_fencing_token,
            "usage_lease_id": self.usage_lease_id,
            "volume_id": self.volume_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionVolumeHandle":
        _exact_keys(
            payload,
            {
                "logical_name",
                "provider_kind",
                "usage_fencing_token",
                "usage_lease_id",
                "volume_id",
            },
            "execution volume handle",
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class ExecutionBindingRecord:
    """One immutable startup intent and its exact cleanup authorities.

    The record's existence is the durable ``bound`` fact for an otherwise
    prepared attempt.  Attempt state therefore need not duplicate this fact.
    The operational handles remain immutable after their leases are released
    so recovery can reconcile the exact resources originally assigned.
    """

    run_id: str
    attempt_id: str
    binding_id: str
    portable_spec: PortableAttemptRuntimeSpec
    evidence: ExecutionBindingEvidence
    projections: Tuple[ExecutionProjectionHandle, ...]
    writable_volumes: Tuple[ExecutionVolumeHandle, ...]
    resource_ttl_seconds: float
    created_run_revision: int
    created_sequence: int
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "execution binding run id")
        required_text(self.attempt_id, "execution binding attempt id")
        required_text(self.binding_id, "execution binding id")
        if not isinstance(self.portable_spec, PortableAttemptRuntimeSpec):
            raise TypeError("portable_spec must be PortableAttemptRuntimeSpec.")
        if not isinstance(self.evidence, ExecutionBindingEvidence):
            raise TypeError("evidence must be ExecutionBindingEvidence.")
        self.evidence.validate_against(self.portable_spec)

        raw_projections = tuple(self.projections)
        raw_volumes = tuple(self.writable_volumes)
        if any(
            not isinstance(item, ExecutionProjectionHandle)
            for item in raw_projections
        ):
            raise TypeError("projections must contain ExecutionProjectionHandle values.")
        if any(not isinstance(item, ExecutionVolumeHandle) for item in raw_volumes):
            raise TypeError(
                "writable_volumes must contain ExecutionVolumeHandle values."
            )
        projections = tuple(sorted(raw_projections))
        volumes = tuple(sorted(raw_volumes))
        if len({item.logical_name for item in projections}) != len(projections):
            raise ValueError("execution projection logical names must be unique.")
        if len({item.realization_id for item in projections}) != len(projections):
            raise ValueError("execution projection realizations must be unique.")
        if len({item.consumer_id for item in projections}) != len(projections):
            raise ValueError("execution projection consumers must be unique.")
        if len({item.consumer_lease_id for item in projections}) != len(projections):
            raise ValueError("execution projection consumer leases must be unique.")
        if len({item.logical_name for item in volumes}) != len(volumes):
            raise ValueError("execution volume logical names must be unique.")
        if len({item.volume_id for item in volumes}) != len(volumes):
            raise ValueError("execution volume ids must be unique.")
        if len({item.usage_lease_id for item in volumes}) != len(volumes):
            raise ValueError("execution volume usage leases must be unique.")
        projection_lease_ids = {item.consumer_lease_id for item in projections}
        volume_lease_ids = {item.usage_lease_id for item in volumes}
        if projection_lease_ids.intersection(volume_lease_ids):
            raise ValueError("execution authority lease ids must be globally unique.")
        if {item.logical_name for item in projections} != {
            item.logical_name for item in self.evidence.projections
        }:
            raise ValueError("operational projections differ from portable evidence.")
        if {item.logical_name for item in volumes} != {
            item.logical_name for item in self.evidence.writable_volumes
        }:
            raise ValueError("operational volumes differ from portable evidence.")
        resource_ttl_seconds = finite_time(
            self.resource_ttl_seconds, "binding resource_ttl_seconds"
        )
        if resource_ttl_seconds <= 0:
            raise ValueError("binding resource_ttl_seconds must be positive.")
        positive_int(self.created_run_revision, "binding created run revision")
        positive_int(self.created_sequence, "binding created sequence")
        positive_int(self.created_txn_id, "binding created transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "binding created_at"),
        )
        object.__setattr__(self, "projections", projections)
        object.__setattr__(self, "writable_volumes", volumes)
        object.__setattr__(self, "resource_ttl_seconds", resource_ttl_seconds)

    def validate_attempt(self, attempt: RunAttemptRecord) -> None:
        """Anchor the portable invocation to the exact prepared attempt."""

        if not isinstance(attempt, RunAttemptRecord):
            raise TypeError("attempt must be RunAttemptRecord.")
        if (
            attempt.run_id != self.run_id
            or attempt.attempt_id != self.attempt_id
            or attempt.binding_id != self.binding_id
            or self.portable_spec.evaluation_spec_digest
            != attempt.evaluation_spec_digest
            or self.portable_spec.environment_revision_digest
            != attempt.evaluation_spec.environment_revision_digest
            or self.portable_spec.prepared_runtime_digest
            != attempt.prepared_runtime_digest
        ):
            raise ValueError("execution binding differs from the prepared attempt.")

    @property
    def portable_spec_digest(self) -> str:
        return self.portable_spec.digest

    @property
    def evidence_fingerprint(self) -> str:
        return self.evidence.fingerprint

    def portable_record(self) -> JsonDict:
        """Return path-free semantics without Realm-local lifecycle handles."""

        return {
            "evidence": self.evidence.to_dict(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "portable_spec": self.portable_spec.to_dict(),
            "portable_spec_digest": self.portable_spec_digest,
        }

    def to_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "created_run_revision": self.created_run_revision,
            "created_sequence": self.created_sequence,
            "created_txn_id": self.created_txn_id,
            "evidence": self.evidence.to_dict(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "portable_spec": self.portable_spec.to_dict(),
            "portable_spec_digest": self.portable_spec_digest,
            "projections": [item.to_dict() for item in self.projections],
            "resource_ttl_seconds": self.resource_ttl_seconds,
            "run_id": self.run_id,
            "writable_volumes": [
                item.to_dict() for item in self.writable_volumes
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionBindingRecord":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__)
            | {"evidence_fingerprint", "portable_spec_digest"},
            "execution binding",
        )
        spec = PortableAttemptRuntimeSpec.from_dict(payload["portable_spec"])
        evidence = ExecutionBindingEvidence.from_dict(payload["evidence"])
        if not isinstance(payload["projections"], list) or not isinstance(
            payload["writable_volumes"], list
        ):
            raise TypeError("execution binding handles must be lists.")
        if (
            len(payload["projections"]) != len(evidence.projections)
            or len(payload["writable_volumes"])
            != len(evidence.writable_volumes)
        ):
            raise ValueError("execution binding handle counts differ from evidence.")
        if payload["portable_spec_digest"] != spec.digest:
            raise ValueError("execution binding portable spec digest is invalid.")
        if payload["evidence_fingerprint"] != evidence.fingerprint:
            raise ValueError("execution binding evidence fingerprint is invalid.")
        values = dict(payload)
        values.pop("portable_spec_digest")
        values.pop("evidence_fingerprint")
        values["portable_spec"] = spec
        values["evidence"] = evidence
        values["projections"] = tuple(
            ExecutionProjectionHandle.from_dict(item)
            for item in payload["projections"]
        )
        values["writable_volumes"] = tuple(
            ExecutionVolumeHandle.from_dict(item)
            for item in payload["writable_volumes"]
        )
        result = cls(**values)
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
            raise ValueError("execution binding is not canonical.")
        return result


@dataclass(frozen=True)
class ExecutionBindingDraft:
    """Read-only, path-free provider binding facts authenticated pre-commit."""

    run_id: str
    attempt_id: str
    binding_id: str
    portable_spec: PortableAttemptRuntimeSpec
    evidence: ExecutionBindingEvidence
    projections: Tuple[ExecutionProjectionHandle, ...]
    writable_volumes: Tuple[ExecutionVolumeHandle, ...]
    resource_ttl_seconds: float

    def __post_init__(self) -> None:
        # Reuse the durable record's complete semantic/handle validation.  The
        # synthetic creation facts are never serialized or exposed by a draft.
        validated = ExecutionBindingRecord(
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            binding_id=self.binding_id,
            portable_spec=self.portable_spec,
            evidence=self.evidence,
            projections=tuple(self.projections),
            writable_volumes=tuple(self.writable_volumes),
            resource_ttl_seconds=self.resource_ttl_seconds,
            created_run_revision=1,
            created_sequence=1,
            created_txn_id=1,
            created_at=0.0,
        )
        object.__setattr__(self, "projections", validated.projections)
        object.__setattr__(
            self, "writable_volumes", validated.writable_volumes
        )
        object.__setattr__(
            self, "resource_ttl_seconds", validated.resource_ttl_seconds
        )

    @property
    def portable_spec_digest(self) -> str:
        return self.portable_spec.digest

    @property
    def evidence_fingerprint(self) -> str:
        return self.evidence.fingerprint

    def validate_attempt(self, attempt: RunAttemptRecord) -> None:
        self.to_binding(
            created_run_revision=1,
            created_sequence=1,
            created_txn_id=1,
            created_at=0.0,
        ).validate_attempt(attempt)

    def to_binding(
        self,
        *,
        created_run_revision: int,
        created_sequence: int,
        created_txn_id: int,
        created_at: float,
    ) -> ExecutionBindingRecord:
        return ExecutionBindingRecord(
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            binding_id=self.binding_id,
            portable_spec=self.portable_spec,
            evidence=self.evidence,
            projections=self.projections,
            writable_volumes=self.writable_volumes,
            resource_ttl_seconds=self.resource_ttl_seconds,
            created_run_revision=created_run_revision,
            created_sequence=created_sequence,
            created_txn_id=created_txn_id,
            created_at=created_at,
        )

    def to_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "evidence": self.evidence.to_dict(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "portable_spec": self.portable_spec.to_dict(),
            "portable_spec_digest": self.portable_spec_digest,
            "projections": [item.to_dict() for item in self.projections],
            "resource_ttl_seconds": self.resource_ttl_seconds,
            "run_id": self.run_id,
            "writable_volumes": [
                item.to_dict() for item in self.writable_volumes
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionBindingDraft":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__)
            | {"evidence_fingerprint", "portable_spec_digest"},
            "execution binding draft",
        )
        spec = PortableAttemptRuntimeSpec.from_dict(payload["portable_spec"])
        evidence = ExecutionBindingEvidence.from_dict(payload["evidence"])
        if payload["portable_spec_digest"] != spec.digest:
            raise ValueError("execution binding draft spec digest is invalid.")
        if payload["evidence_fingerprint"] != evidence.fingerprint:
            raise ValueError(
                "execution binding draft evidence fingerprint is invalid."
            )
        result = cls(
            run_id=payload["run_id"],
            attempt_id=payload["attempt_id"],
            binding_id=payload["binding_id"],
            portable_spec=spec,
            evidence=evidence,
            projections=tuple(
                ExecutionProjectionHandle.from_dict(item)
                for item in payload["projections"]
            ),
            writable_volumes=tuple(
                ExecutionVolumeHandle.from_dict(item)
                for item in payload["writable_volumes"]
            ),
            resource_ttl_seconds=payload["resource_ttl_seconds"],
        )
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
            dict(payload)
        ):
            raise ValueError("execution binding draft is not canonical.")
        return result


@dataclass(frozen=True, order=True)
class ExecutionLaunchIntentRecord:
    """Path-free immutable intent for one exact provider launch request."""

    run_id: str
    attempt_id: str
    binding_id: str
    launch_token: str
    provider_kind: str
    evidence_fingerprint: str
    launch_request_digest: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "launch intent run id")
        required_text(self.attempt_id, "launch intent attempt id")
        required_text(self.binding_id, "launch intent binding id")
        required_text(self.launch_token, "launch intent token")
        required_text(
            self.provider_kind, "launch intent provider kind", max_bytes=128
        )
        lower_hex_digest(
            self.evidence_fingerprint, "launch intent evidence fingerprint"
        )
        lower_hex_digest(
            self.launch_request_digest, "launch intent request digest"
        )
        required_text(
            self.created_by_principal_id, "launch intent creator principal id"
        )
        positive_int(self.created_txn_id, "launch intent transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "launch intent created_at"),
        )

    def validate_binding(
        self, binding: ExecutionBindingRecord, attempt: RunAttemptRecord
    ) -> None:
        if not isinstance(binding, ExecutionBindingRecord):
            raise TypeError("binding must be an ExecutionBindingRecord.")
        if not isinstance(attempt, RunAttemptRecord):
            raise TypeError("attempt must be a RunAttemptRecord.")
        if (
            self.run_id != binding.run_id
            or self.attempt_id != binding.attempt_id
            or self.binding_id != binding.binding_id
            or self.launch_token != attempt.launch_token
            or self.provider_kind != binding.portable_spec.provider.kind
            or self.evidence_fingerprint != binding.evidence_fingerprint
        ):
            raise ValueError("launch intent differs from its execution binding.")

    def to_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "provider_kind": self.provider_kind,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionLaunchIntentRecord":
        payload = _without_receipt_version(payload, "execution launch intent")
        _exact_keys(payload, set(cls.__dataclass_fields__), "execution launch intent")
        result = cls(**dict(payload))
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
            raise ValueError("execution launch intent is not canonical.")
        return result


@dataclass(frozen=True, order=True)
class ExecutionTerminalEvidenceRecord:
    """Path-free immutable fact produced after exact provider proof validation.

    The provider's backend token and raw proof deliberately remain outside the
    Realm ledger.  ``proof_fingerprint`` commits to that proof so exact replay
    can be recognized without turning provider-private authority into retained
    run metadata.
    """

    run_id: str
    attempt_id: str
    binding_id: str
    launch_token: str
    provider_kind: str
    evidence_fingerprint: str
    launch_request_digest: str
    proof_fingerprint: str
    started: bool
    disposition: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "terminal evidence run id")
        required_text(self.attempt_id, "terminal evidence attempt id")
        required_text(self.binding_id, "terminal evidence binding id")
        required_text(self.launch_token, "terminal evidence launch token")
        required_text(
            self.provider_kind, "terminal evidence provider kind", max_bytes=128
        )
        lower_hex_digest(
            self.evidence_fingerprint, "terminal evidence binding fingerprint"
        )
        lower_hex_digest(
            self.launch_request_digest, "terminal evidence launch request digest"
        )
        lower_hex_digest(self.proof_fingerprint, "terminal proof fingerprint")
        if not isinstance(self.started, bool):
            raise TypeError("terminal evidence started must be a boolean.")
        if self.disposition not in {"never_started", "exited", "killed"}:
            raise ValueError("terminal evidence disposition is unsupported.")
        if self.started != (self.disposition != "never_started"):
            raise ValueError(
                "terminal evidence started differs from its disposition."
            )
        required_text(
            self.created_by_principal_id,
            "terminal evidence creator principal id",
        )
        positive_int(self.created_txn_id, "terminal evidence transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "terminal evidence created_at"),
        )

    def validate_launch_intent(
        self,
        binding: ExecutionBindingRecord,
        attempt: RunAttemptRecord,
        launch_intent: ExecutionLaunchIntentRecord,
    ) -> None:
        if not isinstance(launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError("launch_intent must be an ExecutionLaunchIntentRecord.")
        launch_intent.validate_binding(binding, attempt)
        if (
            self.run_id != launch_intent.run_id
            or self.attempt_id != launch_intent.attempt_id
            or self.binding_id != launch_intent.binding_id
            or self.launch_token != launch_intent.launch_token
            or self.provider_kind != launch_intent.provider_kind
            or self.evidence_fingerprint != launch_intent.evidence_fingerprint
            or self.launch_request_digest != launch_intent.launch_request_digest
        ):
            raise ValueError("terminal evidence differs from its launch intent.")

    def to_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "disposition": self.disposition,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "proof_fingerprint": self.proof_fingerprint,
            "provider_kind": self.provider_kind,
            "run_id": self.run_id,
            "started": self.started,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ExecutionTerminalEvidenceRecord":
        payload = _without_receipt_version(payload, "execution terminal evidence")
        _exact_keys(payload, set(cls.__dataclass_fields__), "execution terminal evidence")
        result = cls(**dict(payload))
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
            raise ValueError("execution terminal evidence is not canonical.")
        return result


@dataclass(frozen=True, order=True)
class ExecutionCleanupAuthorizationRecord:
    """Immutable authorization to retire one terminal binding's resources."""

    run_id: str
    attempt_id: str
    binding_id: str
    launch_token: str
    provider_kind: str
    evidence_fingerprint: str
    launch_request_digest: str
    terminal_evidence_fingerprint: str
    authorized_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "cleanup authorization run id")
        required_text(self.attempt_id, "cleanup authorization attempt id")
        required_text(self.binding_id, "cleanup authorization binding id")
        required_text(self.launch_token, "cleanup authorization launch token")
        required_text(
            self.provider_kind,
            "cleanup authorization provider kind",
            max_bytes=128,
        )
        lower_hex_digest(
            self.evidence_fingerprint,
            "cleanup authorization evidence fingerprint",
        )
        lower_hex_digest(
            self.launch_request_digest,
            "cleanup authorization launch request digest",
        )
        lower_hex_digest(
            self.terminal_evidence_fingerprint,
            "cleanup authorization terminal evidence fingerprint",
        )
        required_text(
            self.authorized_by_principal_id,
            "cleanup authorization principal id",
        )
        positive_int(self.created_txn_id, "cleanup authorization transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "cleanup authorization created_at"),
        )

    def validate_launch_intent(
        self,
        binding: ExecutionBindingRecord,
        attempt: RunAttemptRecord,
        launch_intent: ExecutionLaunchIntentRecord,
    ) -> None:
        if not isinstance(launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError("launch_intent must be an ExecutionLaunchIntentRecord.")
        launch_intent.validate_binding(binding, attempt)
        if (
            self.run_id != launch_intent.run_id
            or self.attempt_id != launch_intent.attempt_id
            or self.binding_id != launch_intent.binding_id
            or self.launch_token != launch_intent.launch_token
            or self.provider_kind != launch_intent.provider_kind
            or self.evidence_fingerprint != launch_intent.evidence_fingerprint
            or self.launch_request_digest != launch_intent.launch_request_digest
        ):
            raise ValueError(
                "cleanup authorization differs from its launch intent."
            )

    def validate_terminal_evidence(
        self,
        binding: ExecutionBindingRecord,
        attempt: RunAttemptRecord,
        launch_intent: ExecutionLaunchIntentRecord,
        terminal_evidence: ExecutionTerminalEvidenceRecord,
    ) -> None:
        if not isinstance(terminal_evidence, ExecutionTerminalEvidenceRecord):
            raise TypeError(
                "terminal_evidence must be an ExecutionTerminalEvidenceRecord."
            )
        terminal_evidence.validate_launch_intent(binding, attempt, launch_intent)
        self.validate_launch_intent(binding, attempt, launch_intent)
        if self.terminal_evidence_fingerprint != terminal_evidence.proof_fingerprint:
            raise ValueError(
                "cleanup authorization differs from its terminal evidence."
            )

    def to_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "authorized_by_principal_id": self.authorized_by_principal_id,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "created_txn_id": self.created_txn_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "provider_kind": self.provider_kind,
            "run_id": self.run_id,
            "terminal_evidence_fingerprint": self.terminal_evidence_fingerprint,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ExecutionCleanupAuthorizationRecord":
        payload = _without_receipt_version(
            payload, "execution cleanup authorization"
        )
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "execution cleanup authorization",
        )
        result = cls(**dict(payload))
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(dict(payload)):
            raise ValueError("execution cleanup authorization is not canonical.")
        return result


@dataclass(frozen=True)
class ExecutionCleanupAuthorityReceipt:
    """Current typed authority to resume already-authorized provider cleanup."""

    attempt: RunAttemptRecord
    binding: ExecutionBindingRecord
    launch_intent: ExecutionLaunchIntentRecord
    terminal_evidence: ExecutionTerminalEvidenceRecord
    cleanup_authorization: ExecutionCleanupAuthorizationRecord

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, RunAttemptRecord):
            raise TypeError("attempt must be a RunAttemptRecord.")
        if not isinstance(self.binding, ExecutionBindingRecord):
            raise TypeError("binding must be an ExecutionBindingRecord.")
        if not isinstance(self.launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError(
                "launch_intent must be an ExecutionLaunchIntentRecord."
            )
        if not isinstance(
            self.terminal_evidence, ExecutionTerminalEvidenceRecord
        ):
            raise TypeError(
                "terminal_evidence must be an ExecutionTerminalEvidenceRecord."
            )
        if not isinstance(
            self.cleanup_authorization, ExecutionCleanupAuthorizationRecord
        ):
            raise TypeError(
                "cleanup_authorization must be an "
                "ExecutionCleanupAuthorizationRecord."
            )
        if self.attempt.state != "terminal":
            raise ValueError("cleanup authority requires a terminal attempt.")
        self.binding.validate_attempt(self.attempt)
        self.launch_intent.validate_binding(self.binding, self.attempt)
        self.cleanup_authorization.validate_terminal_evidence(
            self.binding,
            self.attempt,
            self.launch_intent,
            self.terminal_evidence,
        )

    def to_dict(self) -> JsonDict:
        return {
            "attempt": self.attempt.to_dict(),
            "binding": self.binding.to_dict(),
            "cleanup_authorization": self.cleanup_authorization.to_dict(),
            "launch_intent": self.launch_intent.to_dict(),
            "terminal_evidence": self.terminal_evidence.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ExecutionCleanupAuthorityReceipt":
        payload = _without_receipt_version(
            payload, "execution cleanup authority receipt"
        )
        _exact_keys(
            payload,
            {
                "attempt",
                "binding",
                "cleanup_authorization",
                "launch_intent",
                "terminal_evidence",
            },
            "execution cleanup authority receipt",
        )
        try:
            return cls(
                attempt=RunAttemptRecord.from_dict(payload["attempt"]),
                binding=ExecutionBindingRecord.from_dict(payload["binding"]),
                launch_intent=ExecutionLaunchIntentRecord.from_dict(
                    payload["launch_intent"]
                ),
                terminal_evidence=ExecutionTerminalEvidenceRecord.from_dict(
                    payload["terminal_evidence"]
                ),
                cleanup_authorization=ExecutionCleanupAuthorizationRecord.from_dict(
                    payload["cleanup_authorization"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted execution cleanup authority is malformed."
            ) from error


@dataclass(frozen=True)
class ExecutionBindingLaunchReceipt:
    """Immutable exact binding and provider-launch intent, without live authority."""

    attempt: RunAttemptRecord
    binding: ExecutionBindingRecord
    launch_intent: ExecutionLaunchIntentRecord

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, RunAttemptRecord):
            raise TypeError("attempt must be RunAttemptRecord.")
        if not isinstance(self.binding, ExecutionBindingRecord):
            raise TypeError("binding must be ExecutionBindingRecord.")
        if not isinstance(self.launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError("launch_intent must be ExecutionLaunchIntentRecord.")
        self.binding.validate_attempt(self.attempt)
        self.launch_intent.validate_binding(self.binding, self.attempt)
        if (
            self.launch_intent.created_txn_id != self.binding.created_txn_id
            or self.launch_intent.created_at != self.binding.created_at
        ):
            raise ValueError("execution binding and launch creation anchors differ.")

    def to_dict(self) -> JsonDict:
        return {
            "attempt": self.attempt.to_dict(),
            "binding": self.binding.to_dict(),
            "launch_intent": self.launch_intent.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ExecutionBindingLaunchReceipt":
        payload = _without_receipt_version(
            payload, "execution binding launch receipt"
        )
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "execution binding launch receipt",
        )
        try:
            result = cls(
                attempt=RunAttemptRecord.from_dict(payload["attempt"]),
                binding=ExecutionBindingRecord.from_dict(payload["binding"]),
                launch_intent=ExecutionLaunchIntentRecord.from_dict(
                    payload["launch_intent"]
                ),
            )
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                dict(payload)
            ):
                raise ValueError(
                    "execution binding launch receipt is not canonical."
                )
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted execution binding launch receipt is malformed."
            ) from error


@dataclass(frozen=True)
class RunAttemptBindingReceipt:
    """Canonical run receipt for one committed startup intent."""

    run: RunNamespaceRecord
    revision: RunRevisionRecord
    attempt: RunAttemptRecord
    binding: ExecutionBindingRecord
    launch_intent: ExecutionLaunchIntentRecord

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be RunRevisionRecord.")
        if not isinstance(self.attempt, RunAttemptRecord):
            raise TypeError("attempt must be RunAttemptRecord.")
        if not isinstance(self.binding, ExecutionBindingRecord):
            raise TypeError("binding must be ExecutionBindingRecord.")
        if not isinstance(self.launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError("launch_intent must be ExecutionLaunchIntentRecord.")
        if (
            self.run.run_id != self.binding.run_id
            or self.revision.run_id != self.binding.run_id
            or self.attempt.run_id != self.binding.run_id
            or self.attempt.attempt_id != self.binding.attempt_id
            or self.attempt.binding_id != self.binding.binding_id
            or self.run.current_revision != self.revision.revision
            or self.run.next_sequence != self.revision.next_sequence
            or self.run.accepted_logical_trials
            != self.revision.accepted_logical_trials
            or self.run.controller_generation
            != self.revision.controller_generation
            or self.run.controller_lease_id
            != self.revision.writer_controller_lease_id
            or self.run.controller_fencing_token
            != self.revision.writer_controller_fencing_token
            or self.revision.operation_kind != "run.attempt.bind"
            or self.binding.created_run_revision != self.revision.revision
            or self.binding.created_sequence != self.revision.last_sequence
            or self.binding.created_txn_id != self.revision.txn_id
            or self.binding.created_at != self.revision.created_at
            or self.attempt.state != "prepared"
            or self.attempt.head_transition_index != 1
            or self.binding.created_run_revision
            <= self.attempt.prepared_run_revision
            or self.binding.created_sequence <= self.attempt.prepared_sequence
            or self.binding.created_txn_id <= self.attempt.prepared_txn_id
            or self.binding.created_at < self.attempt.prepared_at
            or self.launch_intent.created_txn_id != self.binding.created_txn_id
            or self.launch_intent.created_at != self.binding.created_at
        ):
            raise ValueError("run-attempt binding receipt identities differ.")
        self.binding.validate_attempt(self.attempt)
        self.launch_intent.validate_binding(self.binding, self.attempt)

    def to_dict(self) -> JsonDict:
        return {
            "attempt": self.attempt.to_dict(),
            "binding": self.binding.to_dict(),
            "launch_intent": self.launch_intent.to_dict(),
            "revision": self.revision.to_dict(),
            "run": self.run.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptBindingReceipt":
        payload = _without_receipt_version(
            payload, "run-attempt binding receipt"
        )
        _exact_keys(
            payload,
            {"attempt", "binding", "launch_intent", "revision", "run"},
            "run-attempt binding receipt",
        )
        try:
            result = cls(
                run=RunNamespaceRecord.from_dict(payload["run"]),
                revision=RunRevisionRecord.from_dict(payload["revision"]),
                attempt=RunAttemptRecord.from_dict(payload["attempt"]),
                binding=ExecutionBindingRecord.from_dict(payload["binding"]),
                launch_intent=ExecutionLaunchIntentRecord.from_dict(
                    payload["launch_intent"]
                ),
            )
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                dict(payload)
            ):
                raise ValueError("run-attempt binding receipt is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted run-attempt binding receipt is malformed."
            ) from error


@dataclass(frozen=True)
class RunAttemptBindingAuthorityReceipt:
    """Current authority needed to recover a prepared or running binding."""

    run: RunNamespaceRecord
    revision: RunRevisionRecord
    attempt: RunAttemptRecord
    attempt_lease: "LeaseRecord"
    binding: ExecutionBindingRecord
    launch_intent: ExecutionLaunchIntentRecord

    def __post_init__(self) -> None:
        # Importing here avoids making the records module part of lease setup.
        from .leases import LeaseRecord

        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be RunRevisionRecord.")
        if not isinstance(self.attempt, RunAttemptRecord):
            raise TypeError("attempt must be RunAttemptRecord.")
        if not isinstance(self.attempt_lease, LeaseRecord):
            raise TypeError("attempt_lease must be LeaseRecord.")
        if not isinstance(self.binding, ExecutionBindingRecord):
            raise TypeError("binding must be ExecutionBindingRecord.")
        if not isinstance(self.launch_intent, ExecutionLaunchIntentRecord):
            raise TypeError("launch_intent must be ExecutionLaunchIntentRecord.")
        if (
            self.run.run_id != self.revision.run_id
            or self.run.run_id != self.attempt.run_id
            or self.run.run_id != self.binding.run_id
            or self.run.current_revision != self.revision.revision
            or self.run.next_sequence != self.revision.next_sequence
            or self.run.controller_generation != self.revision.controller_generation
            or self.attempt.attempt_id != self.binding.attempt_id
            or self.attempt.binding_id != self.binding.binding_id
            or self.attempt.state not in {"prepared", "running"}
            or self.attempt_lease.lease_id != self.attempt.attempt_lease_id
            or self.attempt_lease.owner_id != self.run.owner_id
            or self.attempt_lease.parent_lease_id != self.run.controller_lease_id
            or self.attempt_lease.lease_kind != "run-attempt"
            or self.attempt_lease.audience != "realm-ledger"
            or self.attempt_lease.holder_id != self.run.controller_holder_id
            or self.attempt_lease.scope_key
            != f"run-attempt:{self.run.run_id}:{self.attempt.attempt_id}"
            or self.attempt_lease.state.value != "active"
        ):
            raise ValueError("run-attempt binding authority anchors differ.")
        self.binding.validate_attempt(self.attempt)
        self.launch_intent.validate_binding(self.binding, self.attempt)

    def to_dict(self) -> JsonDict:
        return {
            "attempt": self.attempt.to_dict(),
            "attempt_lease": self.attempt_lease.to_dict(),
            "binding": self.binding.to_dict(),
            "launch_intent": self.launch_intent.to_dict(),
            "revision": self.revision.to_dict(),
            "run": self.run.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunAttemptBindingAuthorityReceipt":
        from .leases import LeaseRecord

        _exact_keys(
            payload,
            {
                "attempt",
                "attempt_lease",
                "binding",
                "launch_intent",
                "revision",
                "run",
            },
            "run-attempt binding authority receipt",
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            attempt_lease=LeaseRecord.from_dict(payload["attempt_lease"]),
            binding=ExecutionBindingRecord.from_dict(payload["binding"]),
            launch_intent=ExecutionLaunchIntentRecord.from_dict(
                payload["launch_intent"]
            ),
        )


__all__ = [
    "ExecutionBindingRecord",
    "ExecutionBindingDraft",
    "ExecutionBindingLaunchReceipt",
    "ExecutionCleanupAuthorizationRecord",
    "ExecutionCleanupAuthorityReceipt",
    "ExecutionLaunchIntentRecord",
    "ExecutionTerminalEvidenceRecord",
    "ExecutionProjectionHandle",
    "ExecutionVolumeHandle",
    "RunAttemptBindingReceipt",
    "RunAttemptBindingAuthorityReceipt",
    "projection_private_coordinate_digest",
    "run_attempt_binding_operation_id",
    "run_attempt_projection_operation_id",
    "run_attempt_resource_holder_id",
    "run_attempt_terminal_evidence_operation_id",
    "run_attempt_volume_operation_id",
    "run_attempt_volume_operational_ids",
    "run_attempt_volume_create_operation_id",
]
