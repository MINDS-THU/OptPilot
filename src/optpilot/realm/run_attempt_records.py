"""Typed canonical records for run attempts, observations, and artifacts.

The value objects in this module mirror schema-v7 without exposing SQLite's
JSON column split as a second public representation.  Semantic payloads remain
typed as :class:`EvaluationSpec`, :class:`AttemptEnvelope`, and
:class:`OutputDeclaration`; their persisted digest and projected columns are
derived from those canonical values by the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from ..attempts import AttemptEnvelope, EvaluationSpec, OutputDeclaration
from ..run_control_manifest import SubmissionControlRecord
from ..run_terminal_policy import DERIVED_SUBMISSION_STOP_CODES
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
from .leases import LeaseRecord, LeaseState
from .owners import (
    OwnerChange,
    OwnerChangeState,
    OwnerCommitReceipt,
    OwnerMembership,
)
from .refs import (
    BlobRef,
    PhysicalContentRef,
    SnapshotRef,
    parse_physical_content_ref,
    request_digest,
)
from .run_records import (
    LOGICAL_TRIAL_OUTCOMES,
    RUN_CANDIDATE_ROLE,
    LogicalTrialTransitionRecord,
    RunCandidateRecord,
    RunNamespaceRecord,
    RunRevisionRecord,
)


JsonDict = dict[str, Any]
RUN_ARTIFACT_ROLE = "run-artifact"
RUN_ATTEMPT_STATES = frozenset({"prepared", "running", "terminal"})
RUN_ATTEMPT_OUTCOMES = LOGICAL_TRIAL_OUTCOMES
_EVALUATION_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "format",
        "spec",
        "lineage",
        "generator",
        "validation",
        "materialization",
    }
)


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


def _freeze_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    frozen = freeze_json(value, label=label)
    if not isinstance(frozen, Mapping):  # Defensive: Mapping always freezes to Mapping.
        raise TypeError(f"{label} must be a mapping.")
    return frozen


def _validate_attempt_state(
    *, state: str, outcome: str | None, code: str | None, label: str
) -> None:
    if state not in RUN_ATTEMPT_STATES:
        raise ValueError(f"{label} state is unsupported.")
    optional_text(code, f"{label} code", max_bytes=512)
    if state != "terminal":
        if outcome is not None or code is not None:
            raise ValueError(f"Nonterminal {label} cannot have an outcome or code.")
        return
    if outcome not in RUN_ATTEMPT_OUTCOMES:
        raise ValueError(f"Terminal {label} requires a standard outcome.")
    if outcome == "success":
        if code is not None:
            raise ValueError(f"Successful {label} cannot have a code.")
    elif code is None:
        raise ValueError(f"Non-success {label} requires a code.")


@dataclass(frozen=True)
class RunAttemptRecord:
    """Current canonical head for one run-owned execution attempt."""

    run_id: str
    attempt_id: str
    logical_trial_id: str
    attempt_index: int
    controller_generation: int
    evaluation_spec: EvaluationSpec
    prepared_runtime_digest: str
    binding_id: str
    launch_token: str
    attempt_lease_id: str
    capture_change_id: str
    state: str
    outcome: str | None
    code: str | None
    head_transition_index: int
    prepared_run_revision: int
    prepared_sequence: int
    prepared_txn_id: int
    prepared_at: float
    updated_at: float

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "attempt_id",
            "logical_trial_id",
            "binding_id",
            "launch_token",
            "attempt_lease_id",
            "capture_change_id",
        ):
            required_text(getattr(self, name), name.replace("_", " "))
        positive_int(self.attempt_index, "attempt index")
        positive_int(self.controller_generation, "attempt controller generation")
        if not isinstance(self.evaluation_spec, EvaluationSpec):
            raise TypeError("evaluation_spec must be an EvaluationSpec.")
        lower_hex_digest(self.prepared_runtime_digest, "prepared runtime digest")
        if self.prepared_runtime_digest != self.evaluation_spec.prepared_runtime_digest:
            raise ValueError(
                "prepared_runtime_digest differs from the evaluation spec."
            )
        _validate_attempt_state(
            state=self.state,
            outcome=self.outcome,
            code=self.code,
            label="run attempt",
        )
        positive_int(self.head_transition_index, "attempt head transition index")
        positive_int(self.prepared_run_revision, "attempt prepared run revision")
        positive_int(self.prepared_sequence, "attempt prepared sequence")
        positive_int(self.prepared_txn_id, "attempt prepared transaction id")
        prepared = finite_time(self.prepared_at, "attempt prepared_at")
        updated = finite_time(self.updated_at, "attempt updated_at")
        if updated < prepared:
            raise ValueError("attempt updated_at precedes prepared_at.")
        object.__setattr__(self, "prepared_at", prepared)
        object.__setattr__(self, "updated_at", updated)

    @property
    def evaluation_spec_digest(self) -> str:
        return self.evaluation_spec.digest

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "logical_trial_id": self.logical_trial_id,
            "attempt_index": self.attempt_index,
            "controller_generation": self.controller_generation,
            "evaluation_spec_digest": self.evaluation_spec_digest,
            "evaluation_spec": self.evaluation_spec.to_dict(),
            "prepared_runtime_digest": self.prepared_runtime_digest,
            "binding_id": self.binding_id,
            "launch_token": self.launch_token,
            "attempt_lease_id": self.attempt_lease_id,
            "capture_change_id": self.capture_change_id,
            "state": self.state,
            "outcome": self.outcome,
            "code": self.code,
            "head_transition_index": self.head_transition_index,
            "prepared_run_revision": self.prepared_run_revision,
            "prepared_sequence": self.prepared_sequence,
            "prepared_txn_id": self.prepared_txn_id,
            "prepared_at": self.prepared_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptRecord":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__) | {"evaluation_spec_digest"},
            "run attempt",
        )
        spec = EvaluationSpec.from_dict(payload["evaluation_spec"])
        if payload["evaluation_spec_digest"] != spec.digest:
            raise ValueError("run attempt evaluation_spec_digest is invalid.")
        values = dict(payload)
        values.pop("evaluation_spec_digest")
        values["evaluation_spec"] = spec
        result = cls(**values)
        if result.to_dict() != dict(payload):
            raise ValueError("run attempt is not canonical.")
        return result


@dataclass(frozen=True)
class RunAttemptTransitionRecord:
    """One immutable transition in an attempt's state machine."""

    run_id: str
    attempt_id: str
    transition_index: int
    from_state: str | None
    to_state: str
    outcome: str | None
    code: str | None
    payload: Mapping[str, Any]
    sequence: int
    run_revision: int
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.attempt_id, "attempt id")
        positive_int(self.transition_index, "attempt transition index")
        if self.transition_index == 1:
            if self.from_state is not None or self.to_state != "prepared":
                raise ValueError("Initial attempt transition must enter prepared.")
        else:
            if self.from_state not in {"prepared", "running"}:
                raise ValueError("Noninitial attempt transition requires a prior state.")
            allowed = (
                self.from_state == "prepared"
                and self.to_state in {"running", "terminal"}
            ) or (
                self.from_state == "running" and self.to_state == "terminal"
            )
            if not allowed:
                raise ValueError("Attempt transition edge is unsupported.")
        _validate_attempt_state(
            state=self.to_state,
            outcome=self.outcome,
            code=self.code,
            label="attempt transition",
        )
        object.__setattr__(
            self,
            "payload",
            _freeze_mapping(self.payload, "attempt transition payload"),
        )
        positive_int(self.sequence, "attempt transition sequence")
        positive_int(self.run_revision, "attempt transition run revision")
        positive_int(self.txn_id, "attempt transition transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "attempt transition created_at"),
        )

    @property
    def payload_digest(self) -> str:
        return request_digest(thaw_json(self.payload))

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "transition_index": self.transition_index,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "outcome": self.outcome,
            "code": self.code,
            "payload": thaw_json(self.payload),
            "payload_digest": self.payload_digest,
            "sequence": self.sequence,
            "run_revision": self.run_revision,
            "txn_id": self.txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptTransitionRecord":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__) | {"payload_digest"},
            "run attempt transition",
        )
        values = dict(payload)
        payload_digest = values.pop("payload_digest")
        result = cls(**values)
        if payload_digest != result.payload_digest:
            raise ValueError("run attempt transition payload_digest is invalid.")
        if result.to_dict() != dict(payload):
            raise ValueError("run attempt transition is not canonical.")
        return result


@dataclass(frozen=True)
class RunObservationRecord:
    """One environment-evaluation envelope adopted into a run."""

    run_id: str
    observation_id: str
    attempt_id: str
    envelope: AttemptEnvelope
    adopted_run_revision: int
    adopted_sequence: int
    adopted_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.observation_id, "observation id")
        required_text(self.attempt_id, "attempt id")
        if not isinstance(self.envelope, AttemptEnvelope):
            raise TypeError("envelope must be an AttemptEnvelope.")
        if self.envelope.attempt_id != self.attempt_id:
            raise ValueError("observation attempt_id differs from its envelope.")
        if self.envelope.phase != "environment_evaluation":
            raise ValueError(
                "Canonical run observations require the environment_evaluation phase."
            )
        positive_int(self.adopted_run_revision, "observation adopted run revision")
        positive_int(self.adopted_sequence, "observation adopted sequence")
        positive_int(self.adopted_txn_id, "observation adopted transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "observation created_at"),
        )

    @property
    def envelope_digest(self) -> str:
        return self.envelope.digest

    @property
    def status(self) -> str:
        return self.envelope.outcome

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "observation_id": self.observation_id,
            "attempt_id": self.attempt_id,
            "envelope_digest": self.envelope_digest,
            "envelope": self.envelope.to_dict(),
            "adopted_run_revision": self.adopted_run_revision,
            "adopted_sequence": self.adopted_sequence,
            "adopted_txn_id": self.adopted_txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunObservationRecord":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__) | {"envelope_digest"},
            "run observation",
        )
        envelope = AttemptEnvelope.from_dict(payload["envelope"])
        if payload["envelope_digest"] != envelope.digest:
            raise ValueError("run observation envelope_digest is invalid.")
        values = dict(payload)
        values.pop("envelope_digest")
        values["envelope"] = envelope
        result = cls(**values)
        if result.to_dict() != dict(payload):
            raise ValueError("run observation is not canonical.")
        return result


@dataclass(frozen=True)
class RunArtifactRecord:
    """One retained output declaration and its physical content identity."""

    run_id: str
    artifact_id: str
    attempt_id: str
    observation_id: str | None
    declaration: OutputDeclaration
    content_ref: PhysicalContentRef
    size_bytes: int
    visibility: str
    capture_metadata: Mapping[str, Any]
    adopted_run_revision: int
    adopted_sequence: int
    adopted_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.artifact_id, "artifact id")
        required_text(self.attempt_id, "attempt id")
        optional_text(self.observation_id, "observation id")
        if not isinstance(self.declaration, OutputDeclaration):
            raise TypeError("declaration must be an OutputDeclaration.")
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise TypeError("content_ref must be a physical blob or tree reference.")
        if self.declaration.kind == "file" and not isinstance(self.content_ref, BlobRef):
            raise ValueError("File artifacts require a blob content ref.")
        if self.declaration.kind == "tree" and not isinstance(self.content_ref, SnapshotRef):
            raise ValueError("Tree artifacts require a tree content ref.")
        nonnegative_int(self.size_bytes, "artifact size_bytes")
        if self.visibility not in {"operator", "method"}:
            raise ValueError("artifact visibility must be 'operator' or 'method'.")
        object.__setattr__(
            self,
            "capture_metadata",
            _freeze_mapping(self.capture_metadata, "artifact capture metadata"),
        )
        positive_int(self.adopted_run_revision, "artifact adopted run revision")
        positive_int(self.adopted_sequence, "artifact adopted sequence")
        positive_int(self.adopted_txn_id, "artifact adopted transaction id")
        object.__setattr__(self, "created_at", finite_time(self.created_at, "artifact created_at"))

    @property
    def declaration_id(self) -> str:
        return self.declaration.declaration_id

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "attempt_id": self.attempt_id,
            "observation_id": self.observation_id,
            "declaration": self.declaration.to_dict(),
            "content_ref": str(self.content_ref),
            "size_bytes": self.size_bytes,
            "visibility": self.visibility,
            "capture_metadata": thaw_json(self.capture_metadata),
            "adopted_run_revision": self.adopted_run_revision,
            "adopted_sequence": self.adopted_sequence,
            "adopted_txn_id": self.adopted_txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunArtifactRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run artifact")
        values = dict(payload)
        values["declaration"] = OutputDeclaration.from_dict(values["declaration"])
        values["content_ref"] = parse_physical_content_ref(values["content_ref"])
        result = cls(**values)
        if result.to_dict() != dict(payload):
            raise ValueError("run artifact is not canonical.")
        return result


def _validate_receipt_anchor(
    run: RunNamespaceRecord,
    revision: RunRevisionRecord,
    *,
    operation_kind: str,
) -> None:
    if not isinstance(run, RunNamespaceRecord):
        raise TypeError("run must be a RunNamespaceRecord.")
    if not isinstance(revision, RunRevisionRecord):
        raise TypeError("revision must be a RunRevisionRecord.")
    if (
        run.run_id != revision.run_id
        or run.current_revision != revision.revision
        or run.next_sequence != revision.next_sequence
        or run.accepted_logical_trials != revision.accepted_logical_trials
        or run.controller_generation != revision.controller_generation
        or run.controller_lease_id != revision.writer_controller_lease_id
        or run.controller_fencing_token
        != revision.writer_controller_fencing_token
    ):
        raise ValueError("run attempt receipt run/revision anchors do not agree.")
    if revision.operation_kind != operation_kind:
        raise ValueError("run attempt receipt has the wrong revision operation kind.")


def _validate_transition_receipt(
    *,
    run: RunNamespaceRecord,
    revision: RunRevisionRecord,
    attempt: RunAttemptRecord,
    attempt_transition: RunAttemptTransitionRecord,
    logical_transition: LogicalTrialTransitionRecord,
    operation_kind: str,
    trailing_sequences: int = 0,
) -> None:
    _validate_receipt_anchor(run, revision, operation_kind=operation_kind)
    if not isinstance(attempt, RunAttemptRecord):
        raise TypeError("attempt must be a RunAttemptRecord.")
    if not isinstance(attempt_transition, RunAttemptTransitionRecord):
        raise TypeError("attempt_transition must be a RunAttemptTransitionRecord.")
    if not isinstance(logical_transition, LogicalTrialTransitionRecord):
        raise TypeError("logical_transition must be a LogicalTrialTransitionRecord.")
    if (
        attempt.run_id != run.run_id
        or attempt_transition.run_id != run.run_id
        or logical_transition.run_id != run.run_id
        or attempt_transition.attempt_id != attempt.attempt_id
        or logical_transition.logical_trial_id != attempt.logical_trial_id
        or logical_transition.attempt_id != attempt.attempt_id
        or attempt_transition.run_revision != revision.revision
        or logical_transition.run_revision != revision.revision
        or attempt_transition.txn_id != revision.txn_id
        or logical_transition.txn_id != revision.txn_id
        or attempt.head_transition_index != attempt_transition.transition_index
        or attempt.state != attempt_transition.to_state
        or attempt.outcome != attempt_transition.outcome
        or attempt.code != attempt_transition.code
        or logical_transition.sequence != attempt_transition.sequence + 1
        or revision.last_sequence != logical_transition.sequence + trailing_sequences
    ):
        raise ValueError("run attempt receipt transition anchors do not agree.")


def _validate_heartbeat_authority(
    *,
    run: RunNamespaceRecord,
    attempt: RunAttemptRecord,
    controller_lease: LeaseRecord,
    attempt_lease: LeaseRecord,
    capture_change: OwnerChange,
    capture_retention_lease: LeaseRecord,
) -> None:
    for value, expected, label in (
        (run, RunNamespaceRecord, "run"),
        (attempt, RunAttemptRecord, "attempt"),
        (controller_lease, LeaseRecord, "controller_lease"),
        (attempt_lease, LeaseRecord, "attempt_lease"),
        (capture_change, OwnerChange, "capture_change"),
        (
            capture_retention_lease,
            LeaseRecord,
            "capture_retention_lease",
        ),
    ):
        if not isinstance(value, expected):
            raise TypeError(f"{label} must be a {expected.__name__}.")
    resource_ttl_seconds = finite_time(
        attempt_lease.metadata.get("resource_ttl_seconds"),
        "attempt resource TTL",
    )
    if resource_ttl_seconds <= 0:
        raise ValueError("attempt resource TTL must be positive.")
    if (
        run.state != "running"
        or run.retention_state != "active"
        or attempt.run_id != run.run_id
        or attempt.controller_generation != run.controller_generation
        or attempt.state not in {"prepared", "running"}
        or controller_lease.lease_id != run.controller_lease_id
        or controller_lease.owner_id != run.owner_id
        or controller_lease.parent_lease_id is not None
        or controller_lease.lease_kind != "run-controller"
        or controller_lease.audience != "realm-ledger"
        or controller_lease.holder_id != run.controller_holder_id
        or controller_lease.fencing_token != run.controller_fencing_token
        or controller_lease.scope_key != f"run:{run.run_id}"
        or controller_lease.state is not LeaseState.ACTIVE
        or attempt_lease.lease_id != attempt.attempt_lease_id
        or attempt_lease.owner_id != run.owner_id
        or attempt_lease.parent_lease_id != controller_lease.lease_id
        or attempt_lease.lease_kind != "run-attempt"
        or attempt_lease.audience != "realm-ledger"
        or attempt_lease.holder_id != controller_lease.holder_id
        or attempt_lease.scope_key
        != f"run-attempt:{run.run_id}:{attempt.attempt_id}"
        or attempt_lease.state is not LeaseState.ACTIVE
        or capture_change.change_id != attempt.capture_change_id
        or capture_change.owner_id != run.owner_id
        or capture_change.state is not OwnerChangeState.ACTIVE
        or capture_retention_lease.lease_id
        != capture_change.retention_lease_id
        or capture_retention_lease.owner_id != run.owner_id
        or capture_retention_lease.parent_lease_id != attempt_lease.lease_id
        or capture_retention_lease.lease_kind != "owner-change-retention"
        or capture_retention_lease.audience != "realm-ledger"
        or capture_retention_lease.scope_key
        != f"owner-change:{capture_change.change_id}"
        or capture_retention_lease.state is not LeaseState.ACTIVE
        or capture_retention_lease.expires_at != capture_change.expires_at
        or attempt_lease.expires_at > controller_lease.expires_at
        or capture_retention_lease.expires_at > attempt_lease.expires_at
    ):
        raise ValueError(
            "Run attempt heartbeat authority anchors do not agree."
        )


def validate_run_attempt_candidate_authority(
    *,
    attempt: RunAttemptRecord,
    candidate: RunCandidateRecord,
    candidate_content_bindings: Tuple[OwnerMembership, ...],
) -> Tuple[OwnerMembership, ...]:
    """Validate the exact admitted candidate and its active physical placements."""

    if not isinstance(attempt, RunAttemptRecord):
        raise TypeError("attempt must be a RunAttemptRecord.")
    if not isinstance(candidate, RunCandidateRecord):
        raise TypeError("candidate must be a RunCandidateRecord.")
    if not isinstance(candidate_content_bindings, tuple):
        raise TypeError("candidate_content_bindings must be a tuple.")
    if any(
        not isinstance(item, OwnerMembership)
        for item in candidate_content_bindings
    ):
        raise TypeError(
            "candidate_content_bindings must contain OwnerMembership values."
        )
    if len(set(candidate_content_bindings)) != len(candidate_content_bindings):
        raise ValueError("candidate_content_bindings must not contain duplicates.")
    normalized_bindings = tuple(sorted(candidate_content_bindings))
    if candidate_content_bindings != normalized_bindings:
        raise ValueError("candidate_content_bindings must be canonically ordered.")

    envelope = candidate.admission.envelope
    evaluation_candidate = thaw_json(attempt.evaluation_spec.candidate)
    if not isinstance(evaluation_candidate, Mapping):  # EvaluationSpec invariant.
        raise ValueError("attempt evaluation candidate must be a mapping.")
    if set(evaluation_candidate) != _EVALUATION_CANDIDATE_FIELDS:
        raise ValueError(
            "Attempt evaluation candidate fields differ from the canonical admission."
        )
    if not isinstance(evaluation_candidate["validation"], Mapping) or not isinstance(
        evaluation_candidate["materialization"], Mapping
    ):
        raise ValueError(
            "Attempt evaluation validation and materialization must be mappings."
        )
    expected_candidate = {
        "candidate_id": candidate.candidate_id,
        "format": envelope.candidate_format,
        "spec": thaw_json(envelope.spec),
        "lineage": thaw_json(candidate.admission.lineage),
        "generator": thaw_json(candidate.admission.generator),
    }
    if (
        candidate.run_id != attempt.run_id
        or candidate.accepted_run_revision >= attempt.prepared_run_revision
        or candidate.accepted_sequence >= attempt.prepared_sequence
        or candidate.accepted_txn_id >= attempt.prepared_txn_id
        or attempt.evaluation_spec.candidate_ref != str(candidate.candidate_ref)
        or any(
            evaluation_candidate[field_name] != expected_value
            for field_name, expected_value in expected_candidate.items()
        )
    ):
        raise ValueError(
            "Run attempt candidate authority differs from its evaluation spec."
        )

    refs = envelope.content_refs
    if envelope.candidate_format == "parameters":
        if refs:
            raise ValueError("Parameter attempt candidate cannot contain content refs.")
    elif envelope.candidate_format == "files":
        if len(refs) != 1 or not isinstance(refs[0], SnapshotRef):
            raise ValueError(
                "File attempt candidate requires exactly one tree snapshot."
            )
    else:
        raise ValueError("Run attempt candidate format is unsupported.")
    if any(
        binding.role != RUN_CANDIDATE_ROLE
        for binding in normalized_bindings
    ):
        raise ValueError(
            "Attempt candidate content bindings require role run-candidate."
        )
    if {binding.content_ref for binding in normalized_bindings} != set(refs):
        raise ValueError(
            "Attempt candidate content bindings differ from its exact refs."
        )
    return normalized_bindings


@dataclass(frozen=True)
class RunAttemptHeartbeatAuthorityReceipt:
    """Current exact authority chain for one live attempt heartbeat round."""

    run: RunNamespaceRecord
    attempt: RunAttemptRecord
    controller_lease: LeaseRecord
    attempt_lease: LeaseRecord
    capture_change: OwnerChange
    capture_retention_lease: LeaseRecord
    candidate: RunCandidateRecord
    candidate_content_bindings: Tuple[OwnerMembership, ...]

    def __post_init__(self) -> None:
        _validate_heartbeat_authority(
            run=self.run,
            attempt=self.attempt,
            controller_lease=self.controller_lease,
            attempt_lease=self.attempt_lease,
            capture_change=self.capture_change,
            capture_retention_lease=self.capture_retention_lease,
        )
        object.__setattr__(
            self,
            "candidate_content_bindings",
            validate_run_attempt_candidate_authority(
                attempt=self.attempt,
                candidate=self.candidate,
                candidate_content_bindings=self.candidate_content_bindings,
            ),
        )

    @property
    def resource_ttl_seconds(self) -> float:
        return float(self.attempt_lease.metadata["resource_ttl_seconds"])

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "attempt": self.attempt.to_dict(),
            "controller_lease": self.controller_lease.to_dict(),
            "attempt_lease": self.attempt_lease.to_dict(),
            "capture_change": self.capture_change.to_dict(),
            "capture_retention_lease": self.capture_retention_lease.to_dict(),
            "candidate": self.candidate.to_dict(),
            "candidate_content_bindings": [
                item.to_dict() for item in self.candidate_content_bindings
            ],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunAttemptHeartbeatAuthorityReceipt":
        payload = _without_receipt_version(
            payload, "run attempt heartbeat authority receipt"
        )
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "run attempt heartbeat authority receipt",
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            controller_lease=LeaseRecord.from_dict(
                payload["controller_lease"]
            ),
            attempt_lease=LeaseRecord.from_dict(payload["attempt_lease"]),
            capture_change=OwnerChange.from_dict(payload["capture_change"]),
            capture_retention_lease=LeaseRecord.from_dict(
                payload["capture_retention_lease"]
            ),
            candidate=RunCandidateRecord.from_dict(payload["candidate"]),
            candidate_content_bindings=tuple(
                OwnerMembership.from_dict(item)
                for item in payload["candidate_content_bindings"]
            ),
        )


@dataclass(frozen=True)
class RunAttemptPreparationReceipt:
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    controller_lease: LeaseRecord
    attempt_lease: LeaseRecord
    capture_change: OwnerChange
    capture_retention_lease: LeaseRecord
    attempt: RunAttemptRecord
    attempt_transition: RunAttemptTransitionRecord
    logical_transition: LogicalTrialTransitionRecord

    def __post_init__(self) -> None:
        _validate_transition_receipt(
            run=self.run,
            revision=self.revision,
            attempt=self.attempt,
            attempt_transition=self.attempt_transition,
            logical_transition=self.logical_transition,
            operation_kind="run.attempt.prepare",
        )
        _validate_heartbeat_authority(
            run=self.run,
            attempt=self.attempt,
            controller_lease=self.controller_lease,
            attempt_lease=self.attempt_lease,
            capture_change=self.capture_change,
            capture_retention_lease=self.capture_retention_lease,
        )
        if (
            self.attempt.state != "prepared"
            or self.attempt.head_transition_index != 1
            or self.attempt.prepared_run_revision != self.revision.revision
            or self.attempt.prepared_sequence != self.attempt_transition.sequence
            or self.attempt.prepared_txn_id != self.revision.txn_id
            or self.attempt.prepared_at != self.attempt_transition.created_at
            or self.attempt.updated_at != self.attempt_transition.created_at
            or self.logical_transition.to_state != "queued"
            or self.attempt.controller_generation != self.run.controller_generation
            or self.attempt_lease.lease_id != self.attempt.attempt_lease_id
            or self.attempt_lease.owner_id != self.run.owner_id
            or self.attempt_lease.parent_lease_id != self.run.controller_lease_id
            or self.attempt_lease.lease_kind != "run-attempt"
            or self.attempt_lease.audience != "realm-ledger"
            or self.attempt_lease.scope_key
            != f"run-attempt:{self.run.run_id}:{self.attempt.attempt_id}"
            or self.attempt_lease.state is not LeaseState.ACTIVE
        ):
            raise ValueError("run attempt preparation receipt anchors do not agree.")

    @property
    def resource_ttl_seconds(self) -> float:
        return float(self.attempt_lease.metadata["resource_ttl_seconds"])

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "controller_lease": self.controller_lease.to_dict(),
            "attempt_lease": self.attempt_lease.to_dict(),
            "capture_change": self.capture_change.to_dict(),
            "capture_retention_lease": self.capture_retention_lease.to_dict(),
            "attempt": self.attempt.to_dict(),
            "attempt_transition": self.attempt_transition.to_dict(),
            "logical_transition": self.logical_transition.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptPreparationReceipt":
        payload = _without_receipt_version(payload, "run attempt preparation receipt")
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "run attempt preparation receipt",
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            controller_lease=LeaseRecord.from_dict(payload["controller_lease"]),
            attempt_lease=LeaseRecord.from_dict(payload["attempt_lease"]),
            capture_change=OwnerChange.from_dict(payload["capture_change"]),
            capture_retention_lease=LeaseRecord.from_dict(
                payload["capture_retention_lease"]
            ),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            attempt_transition=RunAttemptTransitionRecord.from_dict(
                payload["attempt_transition"]
            ),
            logical_transition=LogicalTrialTransitionRecord.from_dict(
                payload["logical_transition"]
            ),
        )


@dataclass(frozen=True)
class RunAttemptLaunchReceipt:
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    attempt: RunAttemptRecord
    attempt_transition: RunAttemptTransitionRecord
    logical_transition: LogicalTrialTransitionRecord

    def __post_init__(self) -> None:
        _validate_transition_receipt(
            run=self.run,
            revision=self.revision,
            attempt=self.attempt,
            attempt_transition=self.attempt_transition,
            logical_transition=self.logical_transition,
            operation_kind="run.attempt.confirm",
        )
        if (
            self.attempt.state != "running"
            or self.attempt_transition.from_state != "prepared"
            or self.attempt.updated_at != self.attempt_transition.created_at
            or self.logical_transition.from_state != "queued"
            or self.logical_transition.to_state != "running"
        ):
            raise ValueError("run attempt launch receipt anchors do not agree.")

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "attempt": self.attempt.to_dict(),
            "attempt_transition": self.attempt_transition.to_dict(),
            "logical_transition": self.logical_transition.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptLaunchReceipt":
        payload = _without_receipt_version(payload, "run attempt launch receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run attempt launch receipt")
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            attempt_transition=RunAttemptTransitionRecord.from_dict(
                payload["attempt_transition"]
            ),
            logical_transition=LogicalTrialTransitionRecord.from_dict(
                payload["logical_transition"]
            ),
        )


@dataclass(frozen=True)
class RunAttemptAdoptionReceipt:
    owner_commit: OwnerCommitReceipt
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    attempt: RunAttemptRecord
    attempt_transition: RunAttemptTransitionRecord
    logical_transition: LogicalTrialTransitionRecord
    observation: RunObservationRecord | None
    artifacts: Tuple[RunArtifactRecord, ...]
    submission_control: SubmissionControlRecord | None = None

    def __post_init__(self) -> None:
        if self.submission_control is not None and not isinstance(
            self.submission_control, SubmissionControlRecord
        ):
            raise TypeError(
                "submission_control must be a SubmissionControlRecord or None."
            )
        _validate_transition_receipt(
            run=self.run,
            revision=self.revision,
            attempt=self.attempt,
            attempt_transition=self.attempt_transition,
            logical_transition=self.logical_transition,
            operation_kind="run.attempt.adopt",
            trailing_sequences=1 if self.submission_control is not None else 0,
        )
        if not isinstance(self.owner_commit, OwnerCommitReceipt):
            raise TypeError("owner_commit must be an OwnerCommitReceipt.")
        if self.observation is not None and not isinstance(
            self.observation, RunObservationRecord
        ):
            raise TypeError("observation must be a RunObservationRecord or None.")
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, RunArtifactRecord) for item in artifacts):
            raise TypeError("artifacts must contain RunArtifactRecord values.")
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise ValueError("run attempt adoption artifacts must have unique ids.")
        if len({item.declaration_id for item in artifacts}) != len(artifacts):
            raise ValueError("run attempt adoption cannot retain a declaration twice.")
        object.__setattr__(self, "artifacts", artifacts)

        if (
            self.attempt.state != "terminal"
            or self.attempt_transition.to_state != "terminal"
            or self.attempt.updated_at != self.attempt_transition.created_at
            or self.logical_transition.to_state not in {"retrying", "terminal"}
            or self.owner_commit.owner_id != self.run.owner_id
            or self.owner_commit.change_id != self.attempt.capture_change_id
            or self.owner_commit.owner_revision != self.revision.owner_revision
        ):
            raise ValueError("run attempt adoption receipt anchors do not agree.")
        if self.logical_transition.to_state == "terminal" and (
            self.logical_transition.outcome != self.attempt.outcome
        ):
            raise ValueError("terminal logical outcome differs from the attempt outcome.")
        if self.submission_control is not None and (
            self.logical_transition.to_state != "terminal"
            or self.submission_control.run_revision != self.revision.revision
            or self.submission_control.previous_state != "accepting"
            or self.submission_control.state != "draining"
            or self.submission_control.stop_code
            not in DERIVED_SUBMISSION_STOP_CODES - {"max_trials"}
        ):
            raise ValueError(
                "derived submission control differs from its adoption anchors."
            )

        observation_id = None
        if self.observation is not None:
            observation_id = self.observation.observation_id
            if (
                self.observation.run_id != self.run.run_id
                or self.observation.attempt_id != self.attempt.attempt_id
                or self.observation.adopted_run_revision != self.revision.revision
                or self.observation.adopted_sequence
                != self.attempt_transition.sequence
                or self.observation.adopted_txn_id != self.revision.txn_id
                or self.observation.envelope.evaluation_spec_digest
                != self.attempt.evaluation_spec_digest
                or self.observation.envelope.binding_id != self.attempt.binding_id
            ):
                raise ValueError("run observation differs from its adoption anchors.")
        for artifact in artifacts:
            if (
                artifact.run_id != self.run.run_id
                or artifact.attempt_id != self.attempt.attempt_id
                or artifact.observation_id != observation_id
                or artifact.adopted_run_revision != self.revision.revision
                or artifact.adopted_sequence != self.attempt_transition.sequence
                or artifact.adopted_txn_id != self.revision.txn_id
            ):
                raise ValueError("run artifact differs from its adoption anchors.")

    def to_dict(self) -> JsonDict:
        return {
            "owner_commit": self.owner_commit.to_dict(),
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "attempt": self.attempt.to_dict(),
            "attempt_transition": self.attempt_transition.to_dict(),
            "logical_transition": self.logical_transition.to_dict(),
            "observation": None if self.observation is None else self.observation.to_dict(),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "submission_control": (
                None
                if self.submission_control is None
                else self.submission_control.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptAdoptionReceipt":
        payload = _without_receipt_version(payload, "run attempt adoption receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run attempt adoption receipt")
        raw_observation = payload["observation"]
        return cls(
            owner_commit=OwnerCommitReceipt.from_dict(payload["owner_commit"]),
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            attempt_transition=RunAttemptTransitionRecord.from_dict(
                payload["attempt_transition"]
            ),
            logical_transition=LogicalTrialTransitionRecord.from_dict(
                payload["logical_transition"]
            ),
            observation=(
                None
                if raw_observation is None
                else RunObservationRecord.from_dict(raw_observation)
            ),
            artifacts=tuple(
                RunArtifactRecord.from_dict(item) for item in payload["artifacts"]
            ),
            submission_control=(
                None
                if payload["submission_control"] is None
                else SubmissionControlRecord.from_dict(payload["submission_control"])
            ),
        )


@dataclass(frozen=True)
class RunAttemptLossReceipt:
    """Canonical recovery receipt for an attempt fenced by controller takeover."""

    run: RunNamespaceRecord
    revision: RunRevisionRecord
    attempt: RunAttemptRecord
    attempt_transition: RunAttemptTransitionRecord
    logical_transition: LogicalTrialTransitionRecord
    submission_control: SubmissionControlRecord | None = None

    def __post_init__(self) -> None:
        if self.submission_control is not None and not isinstance(
            self.submission_control, SubmissionControlRecord
        ):
            raise TypeError(
                "submission_control must be a SubmissionControlRecord or None."
            )
        _validate_transition_receipt(
            run=self.run,
            revision=self.revision,
            attempt=self.attempt,
            attempt_transition=self.attempt_transition,
            logical_transition=self.logical_transition,
            operation_kind="run.attempt.reconcile",
            trailing_sequences=1 if self.submission_control is not None else 0,
        )
        if (
            self.attempt.state != "terminal"
            or self.attempt.outcome != "failed"
            or self.attempt.code != "attempt_authority_lost"
            or self.attempt_transition.from_state not in {"prepared", "running"}
            or self.attempt_transition.to_state != "terminal"
            or self.attempt_transition.outcome != "failed"
            or self.attempt_transition.code != "attempt_authority_lost"
            or self.attempt.updated_at != self.attempt_transition.created_at
            or self.attempt.controller_generation >= self.run.controller_generation
            or self.logical_transition.from_state
            != (
                "queued"
                if self.attempt_transition.from_state == "prepared"
                else "running"
            )
            or self.logical_transition.to_state not in {"retrying", "terminal"}
            or self.logical_transition.code != "attempt_authority_lost"
        ):
            raise ValueError("run attempt loss receipt anchors do not agree.")
        if self.logical_transition.to_state == "retrying":
            if self.logical_transition.outcome is not None:
                raise ValueError("Retrying loss transition cannot have an outcome.")
        elif self.logical_transition.outcome != "failed":
            raise ValueError("Terminal loss transition must have failed outcome.")

        payload = thaw_json(self.attempt_transition.payload)
        expected_payload_keys = {
            "binding_state",
            "lost_controller_generation",
            "replacement_controller_generation",
            "started",
            "terminal_disposition",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_payload_keys
            or payload["lost_controller_generation"]
            != self.attempt.controller_generation
            or payload["replacement_controller_generation"]
            != self.run.controller_generation
            or payload["binding_state"] not in {"unbound", "bound"}
        ):
            raise ValueError("run attempt loss payload is malformed.")
        if payload["binding_state"] == "unbound":
            if (
                payload["started"] is not None
                or payload["terminal_disposition"] is not None
            ):
                raise ValueError("Unbound loss payload cannot claim execution facts.")
        else:
            started = payload["started"]
            disposition = payload["terminal_disposition"]
            if (
                not isinstance(started, bool)
                or disposition not in {"never_started", "exited", "killed"}
                or started != (disposition != "never_started")
                or (
                    self.attempt_transition.from_state == "running"
                    and not started
                )
            ):
                raise ValueError("Bound loss payload contradicts terminal evidence.")

        if self.submission_control is not None and (
            self.logical_transition.to_state != "terminal"
            or self.submission_control.run_revision != self.revision.revision
            or self.submission_control.previous_state != "accepting"
            or self.submission_control.state != "draining"
            or self.submission_control.stop_code != "max_failures"
        ):
            raise ValueError(
                "derived submission control differs from its loss anchors."
            )

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "attempt": self.attempt.to_dict(),
            "attempt_transition": self.attempt_transition.to_dict(),
            "logical_transition": self.logical_transition.to_dict(),
            "submission_control": (
                None
                if self.submission_control is None
                else self.submission_control.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAttemptLossReceipt":
        payload = _without_receipt_version(payload, "run attempt loss receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run attempt loss receipt")
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            attempt=RunAttemptRecord.from_dict(payload["attempt"]),
            attempt_transition=RunAttemptTransitionRecord.from_dict(
                payload["attempt_transition"]
            ),
            logical_transition=LogicalTrialTransitionRecord.from_dict(
                payload["logical_transition"]
            ),
            submission_control=(
                None
                if payload["submission_control"] is None
                else SubmissionControlRecord.from_dict(payload["submission_control"])
            ),
        )


__all__ = [
    "RUN_ARTIFACT_ROLE",
    "RUN_ATTEMPT_OUTCOMES",
    "RUN_ATTEMPT_STATES",
    "RunArtifactRecord",
    "RunAttemptAdoptionReceipt",
    "RunAttemptHeartbeatAuthorityReceipt",
    "RunAttemptLaunchReceipt",
    "RunAttemptLossReceipt",
    "RunAttemptPreparationReceipt",
    "RunAttemptRecord",
    "RunAttemptTransitionRecord",
    "RunObservationRecord",
]
