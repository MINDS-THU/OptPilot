"""Immutable records for atomic run candidate admission.

This module deliberately stops before attempts and observations.  Admission
commits a normalized candidate identity, one or more logical budget slots,
stable submission handles, and their run revision/event anchors together.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .leases import LeaseRecord
from .owners import OwnerCommitReceipt, OwnerMembership
from .refs import (
    BlobRef,
    CandidateRef,
    PhysicalContentRef,
    SnapshotRef,
    canonical_json_bytes,
    parse_physical_content_ref,
    request_digest,
)
from .run_terminal_seal import RunTerminalSeal
from .run_closure import ResolvedRunEvaluationClosure


JsonDict = Dict[str, Any]
SUPPORTED_CANDIDATE_FORMATS = frozenset({"parameters", "files", "opaque"})
RUN_STATES = frozenset({"running", "succeeded", "failed", "cancelled"})
LOGICAL_TRIAL_STATES = frozenset(
    {"accepted", "queued", "running", "retrying", "terminal"}
)
LOGICAL_TRIAL_OUTCOMES = frozenset(
    {"success", "invalid", "failed", "timeout", "partial", "cancelled"}
)
RUN_CANDIDATE_ROLE = "run-candidate"

_ENVELOPE_SCHEMA = "optpilot.normalized-candidate.v1"
_PLAN_SCHEMA = "optpilot.run-admission-plan.v2"
_SELECTION_SCHEMA = "optpilot.run-candidate-selection.v1"


@dataclass(frozen=True)
class NormalizedCandidateEnvelope:
    candidate_format: str
    spec: Mapping[str, Any]
    content_refs: Tuple[PhysicalContentRef, ...]
    candidate_ref: CandidateRef

    def __post_init__(self) -> None:
        required_text(self.candidate_format, "candidate format", max_bytes=128)
        if self.candidate_format not in SUPPORTED_CANDIDATE_FORMATS:
            raise ValueError(
                f"Unsupported candidate format: {self.candidate_format!r}."
            )
        spec = _freeze_mapping(self.spec, "candidate spec")
        refs = tuple(sorted(set(self.content_refs), key=str))
        if any(not isinstance(item, (BlobRef, SnapshotRef)) for item in refs):
            raise TypeError("candidate content refs must be blob or tree refs.")
        if self.candidate_format == "parameters" and refs:
            raise ValueError(
                "Parameter candidates cannot contain physical content refs."
            )
        if self.candidate_format == "files" and not refs:
            raise ValueError(
                "File candidates require at least one physical content ref."
            )
        expected = CandidateRef.build(
            candidate_format=self.candidate_format,
            spec=_thaw(spec),
            content_refs=refs,
        )
        if self.candidate_ref != expected:
            raise ValueError("candidate_ref differs from the normalized envelope.")
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "content_refs", refs)

    @classmethod
    def build(
        cls,
        *,
        candidate_format: str,
        spec: Mapping[str, Any],
        content_refs: Sequence[PhysicalContentRef] = (),
    ) -> "NormalizedCandidateEnvelope":
        refs = tuple(sorted(set(content_refs), key=str))
        candidate_ref = CandidateRef.build(
            candidate_format=candidate_format,
            spec=dict(spec),
            content_refs=refs,
        )
        return cls(candidate_format, spec, refs, candidate_ref)

    def to_dict(self) -> JsonDict:
        return {
            "schema": _ENVELOPE_SCHEMA,
            "candidate_ref": str(self.candidate_ref),
            "format": self.candidate_format,
            "spec": _thaw(self.spec),
            "content_refs": [str(item) for item in self.content_refs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizedCandidateEnvelope":
        try:
            _exact_keys(
                payload,
                {"schema", "candidate_ref", "format", "spec", "content_refs"},
                "candidate envelope",
            )
            if payload["schema"] != _ENVELOPE_SCHEMA:
                raise ValueError("candidate envelope schema is unsupported.")
            content_refs = payload["content_refs"]
            if not isinstance(content_refs, list):
                raise TypeError("candidate envelope content_refs must be a list.")
            result = cls(
                candidate_format=payload["format"],
                spec=payload["spec"],
                content_refs=tuple(
                    parse_physical_content_ref(item) for item in content_refs
                ),
                candidate_ref=CandidateRef.parse(payload["candidate_ref"]),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("candidate envelope is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Candidate envelope is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CandidateAdmission:
    candidate_id: str
    envelope: NormalizedCandidateEnvelope
    lineage: Mapping[str, Any] = field(default_factory=dict)
    generator: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required_text(self.candidate_id, "candidate id", max_bytes=512)
        if not isinstance(self.envelope, NormalizedCandidateEnvelope):
            raise TypeError("envelope must be a NormalizedCandidateEnvelope.")
        for name in ("lineage", "generator"):
            value = _freeze_mapping(getattr(self, name), name)
            object.__setattr__(self, name, value)

    @property
    def candidate_ref(self) -> CandidateRef:
        return self.envelope.candidate_ref

    def to_dict(self) -> JsonDict:
        return {
            "candidate_id": self.candidate_id,
            "envelope": self.envelope.to_dict(),
            "lineage": _thaw(self.lineage),
            "generator": _thaw(self.generator),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateAdmission":
        _exact_keys(
            payload,
            {"candidate_id", "envelope", "lineage", "generator"},
            "candidate admission",
        )
        return cls(
            candidate_id=payload["candidate_id"],
            envelope=NormalizedCandidateEnvelope.from_dict(payload["envelope"]),
            lineage=payload["lineage"],
            generator=payload["generator"],
        )


@dataclass(frozen=True)
class LogicalTrialAdmission:
    logical_trial_id: str
    candidate_id: str
    seed: Any = None
    repetition_index: int = 0
    submission_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required_text(self.logical_trial_id, "logical trial id", max_bytes=512)
        required_text(self.candidate_id, "candidate id", max_bytes=512)
        nonnegative_int(self.repetition_index, "repetition index")
        object.__setattr__(self, "seed", _freeze(self.seed, "trial seed"))
        object.__setattr__(
            self,
            "submission_metadata",
            _freeze_mapping(self.submission_metadata, "submission metadata"),
        )

    def to_dict(self) -> JsonDict:
        return {
            "logical_trial_id": self.logical_trial_id,
            "candidate_id": self.candidate_id,
            "seed": _thaw(self.seed),
            "repetition_index": self.repetition_index,
            "submission_metadata": _thaw(self.submission_metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LogicalTrialAdmission":
        _exact_keys(
            payload,
            {
                "logical_trial_id",
                "candidate_id",
                "seed",
                "repetition_index",
                "submission_metadata",
            },
            "logical trial admission",
        )
        return cls(
            logical_trial_id=payload["logical_trial_id"],
            candidate_id=payload["candidate_id"],
            seed=payload["seed"],
            repetition_index=payload["repetition_index"],
            submission_metadata=payload["submission_metadata"],
        )


@dataclass(frozen=True)
class SessionHandleAdmission:
    handle_id: str
    logical_trial_id: str

    def __post_init__(self) -> None:
        required_text(self.handle_id, "session handle id", max_bytes=512)
        required_text(self.logical_trial_id, "logical trial id", max_bytes=512)

    def to_dict(self) -> JsonDict:
        return {
            "handle_id": self.handle_id,
            "logical_trial_id": self.logical_trial_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionHandleAdmission":
        _exact_keys(
            payload, {"handle_id", "logical_trial_id"}, "session handle admission"
        )
        return cls(
            handle_id=payload["handle_id"],
            logical_trial_id=payload["logical_trial_id"],
        )


@dataclass(frozen=True)
class RunAdmissionPlan:
    candidates: Tuple[CandidateAdmission, ...]
    logical_trials: Tuple[LogicalTrialAdmission, ...]
    session_handles: Tuple[SessionHandleAdmission, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        trials = tuple(self.logical_trials)
        handles = tuple(self.session_handles)
        if not candidates or not trials:
            raise ValueError(
                "A run admission plan requires candidates and logical trials."
            )
        if any(not isinstance(item, CandidateAdmission) for item in candidates):
            raise TypeError("candidates must contain CandidateAdmission values.")
        if any(not isinstance(item, LogicalTrialAdmission) for item in trials):
            raise TypeError("logical_trials must contain LogicalTrialAdmission values.")
        if any(not isinstance(item, SessionHandleAdmission) for item in handles):
            raise TypeError(
                "session_handles must contain SessionHandleAdmission values."
            )
        _unique((item.candidate_id for item in candidates), "candidate ids")
        _unique((item.logical_trial_id for item in trials), "logical trial ids")
        _unique((item.handle_id for item in handles), "session handle ids")
        _unique(
            (item.logical_trial_id for item in handles), "handled logical trial ids"
        )
        candidate_ids = {item.candidate_id for item in candidates}
        trial_candidate_ids = {item.candidate_id for item in trials}
        if not trial_candidate_ids.issubset(candidate_ids):
            raise ValueError(
                "Every logical trial must reference a candidate in the plan."
            )
        if candidate_ids != trial_candidate_ids:
            raise ValueError(
                "Every admitted candidate must have at least one logical trial."
            )
        trial_ids = {item.logical_trial_id for item in trials}
        if not {item.logical_trial_id for item in handles}.issubset(trial_ids):
            raise ValueError(
                "Every session handle must reference a logical trial in the plan."
            )
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "logical_trials", trials)
        object.__setattr__(self, "session_handles", handles)

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "schema": _PLAN_SCHEMA,
            "candidates": [item.to_dict() for item in self.candidates],
            "logical_trials": [item.to_dict() for item in self.logical_trials],
            "session_handles": [item.to_dict() for item in self.session_handles],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAdmissionPlan":
        _exact_keys(
            payload,
            {"schema", "candidates", "logical_trials", "session_handles"},
            "run admission plan",
        )
        if payload["schema"] != _PLAN_SCHEMA:
            raise RealmIntegrityError("Run admission plan schema is unsupported.")
        try:
            result = cls(
                candidates=tuple(
                    CandidateAdmission.from_dict(item) for item in payload["candidates"]
                ),
                logical_trials=tuple(
                    LogicalTrialAdmission.from_dict(item)
                    for item in payload["logical_trials"]
                ),
                session_handles=tuple(
                    SessionHandleAdmission.from_dict(item)
                    for item in payload["session_handles"]
                ),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("run admission plan is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Run admission plan is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class RunNamespaceRecord:
    run_id: str
    owner_id: str
    state: str
    retention_state: str
    current_revision: int
    next_sequence: int
    max_trials: int | None
    accepted_logical_trials: int
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int
    controller_generation: int
    controller_txn_id: int
    created_txn_id: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.owner_id, "run owner id")
        if self.state not in RUN_STATES:
            raise ValueError("run state is unsupported.")
        if self.retention_state not in {"active", "retired"}:
            raise ValueError("run retention state is unsupported.")
        nonnegative_int(self.current_revision, "run revision")
        positive_int(self.next_sequence, "run next sequence")
        if self.max_trials is not None:
            positive_int(self.max_trials, "run max trials")
        nonnegative_int(self.accepted_logical_trials, "accepted logical trials")
        if (
            self.max_trials is not None
            and self.accepted_logical_trials > self.max_trials
        ):
            raise ValueError("accepted logical trials exceed max_trials.")
        required_text(self.controller_lease_id, "controller lease id")
        required_text(self.controller_holder_id, "controller holder id")
        positive_int(self.controller_fencing_token, "controller fencing token")
        positive_int(self.controller_generation, "controller generation")
        positive_int(self.controller_txn_id, "controller transaction id")
        positive_int(self.created_txn_id, "run created transaction id")
        created = finite_time(self.created_at, "run created_at")
        updated = finite_time(self.updated_at, "run updated_at")
        if updated < created:
            raise ValueError("run updated_at precedes created_at.")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunNamespaceRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run namespace")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunRevisionRecord:
    run_id: str
    revision: int
    owner_revision: int
    last_sequence: int
    next_sequence: int
    accepted_logical_trials: int
    controller_generation: int
    writer_controller_lease_id: str
    writer_controller_fencing_token: int
    operation_kind: str
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        nonnegative_int(self.revision, "run revision")
        nonnegative_int(self.owner_revision, "run owner revision")
        nonnegative_int(self.last_sequence, "run last sequence")
        positive_int(self.next_sequence, "run next sequence")
        if self.next_sequence != self.last_sequence + 1:
            raise ValueError("run next_sequence must follow last_sequence.")
        nonnegative_int(self.accepted_logical_trials, "accepted logical trials")
        positive_int(self.controller_generation, "controller generation")
        required_text(self.writer_controller_lease_id, "writer controller lease id")
        positive_int(
            self.writer_controller_fencing_token,
            "writer controller fencing token",
        )
        required_text(self.operation_kind, "run revision operation kind", max_bytes=128)
        positive_int(self.txn_id, "run revision transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunRevisionRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run revision")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunCreateReceipt:
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    controller_lease: LeaseRecord
    definition_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be a RunRevisionRecord.")
        if not isinstance(self.controller_lease, LeaseRecord):
            raise TypeError("controller_lease must be a LeaseRecord.")
        lower_hex_digest(self.definition_digest, "run definition digest")

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "controller_lease": self.controller_lease.to_dict(),
            "definition_digest": self.definition_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunCreateReceipt":
        payload = _without_receipt_version(payload, "run create receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run create receipt")
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            controller_lease=LeaseRecord.from_dict(payload["controller_lease"]),
            definition_digest=payload["definition_digest"],
        )


@dataclass(frozen=True)
class RunControllerTermRecord:
    run_id: str
    run_revision: int
    generation: int
    lease_id: str
    holder_id: str
    fencing_token: int
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        nonnegative_int(self.run_revision, "controller term run revision")
        positive_int(self.generation, "controller generation")
        required_text(self.lease_id, "controller lease id")
        required_text(self.holder_id, "controller holder id")
        positive_int(self.fencing_token, "controller fencing token")
        positive_int(self.txn_id, "controller transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "controller term created_at"),
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunControllerTermRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run controller term")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunControllerReplacementReceipt:
    run: RunNamespaceRecord
    term: RunControllerTermRecord
    previous_controller_lease: LeaseRecord
    controller_lease: LeaseRecord

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.term, RunControllerTermRecord):
            raise TypeError("term must be a RunControllerTermRecord.")
        if not isinstance(self.previous_controller_lease, LeaseRecord):
            raise TypeError("previous_controller_lease must be a LeaseRecord.")
        if not isinstance(self.controller_lease, LeaseRecord):
            raise TypeError("controller_lease must be a LeaseRecord.")
        if (
            self.run.controller_generation != self.term.generation
            or self.run.current_revision != self.term.run_revision
            or self.run.controller_lease_id != self.term.lease_id
            or self.controller_lease.lease_id != self.term.lease_id
            or self.controller_lease.holder_id != self.term.holder_id
            or self.controller_lease.fencing_token != self.term.fencing_token
        ):
            raise ValueError("controller replacement receipt records do not agree.")

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "term": self.term.to_dict(),
            "previous_controller_lease": self.previous_controller_lease.to_dict(),
            "controller_lease": self.controller_lease.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunControllerReplacementReceipt":
        payload = _without_receipt_version(
            payload, "run controller replacement receipt"
        )
        _exact_keys(
            payload, set(cls.__dataclass_fields__), "run controller replacement receipt"
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            term=RunControllerTermRecord.from_dict(payload["term"]),
            previous_controller_lease=LeaseRecord.from_dict(
                payload["previous_controller_lease"]
            ),
            controller_lease=LeaseRecord.from_dict(payload["controller_lease"]),
        )


@dataclass(frozen=True)
class RunControllerTermAuthorityReceipt:
    """Exact historical controller term and the lease that created it."""

    term: RunControllerTermRecord
    controller_lease: LeaseRecord

    def __post_init__(self) -> None:
        if not isinstance(self.term, RunControllerTermRecord):
            raise TypeError("term must be a RunControllerTermRecord.")
        if not isinstance(self.controller_lease, LeaseRecord):
            raise TypeError("controller_lease must be a LeaseRecord.")
        lease = self.controller_lease
        if (
            self.term.lease_id != lease.lease_id
            or self.term.holder_id != lease.holder_id
            or self.term.fencing_token != lease.fencing_token
        ):
            raise ValueError("controller term differs from its lease authority.")

    def to_dict(self) -> JsonDict:
        return {
            "controller_lease": self.controller_lease.to_dict(),
            "term": self.term.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunControllerTermAuthorityReceipt":
        payload = _without_receipt_version(
            payload, "run controller term authority receipt"
        )
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "run controller term authority receipt",
        )
        return cls(
            term=RunControllerTermRecord.from_dict(payload["term"]),
            controller_lease=LeaseRecord.from_dict(payload["controller_lease"]),
        )


@dataclass(frozen=True)
class RunCandidateRecord:
    run_id: str
    candidate_key: str
    admission: CandidateAdmission
    accepted_run_revision: int
    accepted_owner_revision: int
    accepted_sequence: int
    accepted_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.candidate_key, "candidate key")
        if not isinstance(self.admission, CandidateAdmission):
            raise TypeError("admission must be a CandidateAdmission.")
        positive_int(self.accepted_run_revision, "accepted run revision")
        nonnegative_int(self.accepted_owner_revision, "accepted owner revision")
        positive_int(self.accepted_sequence, "accepted sequence")
        positive_int(self.accepted_txn_id, "accepted transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    @property
    def candidate_ref(self) -> CandidateRef:
        return self.admission.candidate_ref

    @property
    def candidate_id(self) -> str:
        return self.admission.candidate_id

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "candidate_key": self.candidate_key,
            "admission": self.admission.to_dict(),
            "accepted_run_revision": self.accepted_run_revision,
            "accepted_owner_revision": self.accepted_owner_revision,
            "accepted_sequence": self.accepted_sequence,
            "accepted_txn_id": self.accepted_txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunCandidateRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run candidate record")
        values = dict(payload)
        values["admission"] = CandidateAdmission.from_dict(values["admission"])
        return cls(**values)


@dataclass(frozen=True)
class LogicalTrialRecord:
    run_id: str
    candidate_key: str
    admission: LogicalTrialAdmission
    budget_slot: int
    state: str
    accepted_sequence: int
    accepted_txn_id: int

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.candidate_key, "candidate key")
        if not isinstance(self.admission, LogicalTrialAdmission):
            raise TypeError("admission must be a LogicalTrialAdmission.")
        positive_int(self.budget_slot, "budget slot")
        if self.state not in LOGICAL_TRIAL_STATES:
            raise ValueError("logical trial state is unsupported.")
        positive_int(self.accepted_sequence, "accepted sequence")
        positive_int(self.accepted_txn_id, "accepted transaction id")

    def to_dict(self) -> JsonDict:
        return {
            "run_id": self.run_id,
            "candidate_key": self.candidate_key,
            "admission": self.admission.to_dict(),
            "budget_slot": self.budget_slot,
            "state": self.state,
            "accepted_sequence": self.accepted_sequence,
            "accepted_txn_id": self.accepted_txn_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LogicalTrialRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "logical trial record")
        values = dict(payload)
        values["admission"] = LogicalTrialAdmission.from_dict(values["admission"])
        return cls(**values)


@dataclass(frozen=True)
class LogicalTrialTransitionRecord:
    run_id: str
    logical_trial_id: str
    transition_index: int
    from_state: str | None
    to_state: str
    outcome: str | None
    code: str | None
    attempt_id: str | None
    sequence: int
    run_revision: int
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.logical_trial_id, "logical trial id")
        positive_int(self.transition_index, "logical transition index")
        if self.from_state is not None and self.from_state not in LOGICAL_TRIAL_STATES:
            raise ValueError("logical transition from_state is unsupported.")
        if self.to_state not in LOGICAL_TRIAL_STATES:
            raise ValueError("logical transition to_state is unsupported.")
        if self.transition_index == 1:
            if self.from_state is not None or self.to_state != "accepted":
                raise ValueError("initial logical transition must enter accepted.")
        elif self.from_state is None or self.to_state == "accepted":
            raise ValueError("noninitial logical transition requires a prior state.")
        if self.to_state == "terminal":
            if self.outcome not in LOGICAL_TRIAL_OUTCOMES:
                raise ValueError(
                    "terminal logical transition requires a standard outcome."
                )
        elif self.outcome is not None:
            raise ValueError("nonterminal logical transition cannot have an outcome.")
        for name in ("code", "attempt_id"):
            value = getattr(self, name)
            if value is not None:
                required_text(value, name.replace("_", " "), max_bytes=512)
        positive_int(self.sequence, "logical transition sequence")
        positive_int(self.run_revision, "logical transition run revision")
        positive_int(self.txn_id, "logical transition transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LogicalTrialTransitionRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "logical trial transition")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunLogicalTrialTransitionReceipt:
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    transition: LogicalTrialTransitionRecord

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be a RunRevisionRecord.")
        if not isinstance(self.transition, LogicalTrialTransitionRecord):
            raise TypeError("transition must be a LogicalTrialTransitionRecord.")
        if (
            self.run.current_revision != self.revision.revision
            or self.revision.revision != self.transition.run_revision
            or self.revision.txn_id != self.transition.txn_id
        ):
            raise ValueError("logical transition receipt anchors do not agree.")

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "transition": self.transition.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunLogicalTrialTransitionReceipt":
        payload = _without_receipt_version(payload, "logical transition receipt")
        _exact_keys(
            payload, set(cls.__dataclass_fields__), "logical transition receipt"
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            transition=LogicalTrialTransitionRecord.from_dict(payload["transition"]),
        )


@dataclass(frozen=True)
class RunFinalizationRecord:
    run_id: str
    terminal_state: str
    code: str | None
    run_revision: int
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        if self.terminal_state not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("run finalization state is unsupported.")
        if self.code is not None:
            required_text(self.code, "run finalization code", max_bytes=512)
        positive_int(self.run_revision, "run finalization revision")
        positive_int(self.txn_id, "run finalization transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunFinalizationRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run finalization")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunFinishReceipt:
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    finalization: RunFinalizationRecord
    terminal_seal: RunTerminalSeal

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be a RunRevisionRecord.")
        if not isinstance(self.finalization, RunFinalizationRecord):
            raise TypeError("finalization must be a RunFinalizationRecord.")
        if not isinstance(self.terminal_seal, RunTerminalSeal):
            raise TypeError("terminal_seal must be a RunTerminalSeal.")
        seal = self.terminal_seal
        if (
            self.run.run_id != self.revision.run_id
            or self.run.run_id != self.finalization.run_id
            or self.run.run_id != seal.run_id
            or self.run.owner_id != seal.owner_id
            or self.run.state != self.finalization.terminal_state
            or self.run.state != seal.terminal_state
            or self.run.current_revision != self.revision.revision
            or self.revision.revision != self.finalization.run_revision
            or self.revision.revision != seal.finalization_revision
            or self.revision.txn_id != self.finalization.txn_id
            or self.revision.txn_id != seal.finalization_txn_id
            or self.revision.owner_revision != seal.owner_revision
            or self.revision.last_sequence != seal.last_sequence
            or self.revision.accepted_logical_trials != seal.accepted_logical_trials
            or self.finalization.code != seal.code
        ):
            raise ValueError("run finish receipt anchors do not agree.")

    def to_dict(self) -> JsonDict:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "finalization": self.finalization.to_dict(),
            "terminal_seal": self.terminal_seal.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunFinishReceipt":
        payload = _without_receipt_version(payload, "run finish receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run finish receipt")
        return cls(
            RunNamespaceRecord.from_dict(payload["run"]),
            RunRevisionRecord.from_dict(payload["revision"]),
            RunFinalizationRecord.from_dict(payload["finalization"]),
            RunTerminalSeal.from_dict(payload["terminal_seal"]),
        )


@dataclass(frozen=True)
class RunRetirementRecord:
    run_id: str
    run_revision: int
    owner_revision: int
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        positive_int(self.run_revision, "run retirement revision")
        nonnegative_int(self.owner_revision, "run retirement owner revision")
        positive_int(self.txn_id, "run retirement transaction id")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunRetirementRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run retirement")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunDeletionRecord:
    """The note left where a deleted run's record used to be.

    A deleted run must stay distinguishable from one that never existed, so
    the note names what was removed and when: the run's terminal state, its
    definition digest when one was recorded, how many rows of each kind were
    erased, and which container images the record named at the moment it was
    erased.
    """

    run_id: str
    run_revision: int
    owner_revision: int
    txn_id: int
    actor_principal_id: str
    run_definition_digest: Optional[str]
    run_terminal_state: str
    run_created_at: float
    deleted_counts: Mapping[str, int]
    named_image_digests: tuple[str, ...]
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        positive_int(self.run_revision, "run deletion revision")
        nonnegative_int(self.owner_revision, "run deletion owner revision")
        positive_int(self.txn_id, "run deletion transaction id")
        required_text(self.actor_principal_id, "run deletion actor")
        if self.run_definition_digest is not None:
            required_text(self.run_definition_digest, "run definition digest")
        required_text(self.run_terminal_state, "run terminal state")
        object.__setattr__(
            self,
            "run_created_at",
            finite_time(self.run_created_at, "run created_at"),
        )
        counts = dict(self.deleted_counts)
        for table, count in counts.items():
            required_text(table, "deleted table name")
            nonnegative_int(count, f"deleted count for {table}")
        object.__setattr__(self, "deleted_counts", counts)
        digests = tuple(self.named_image_digests)
        for digest in digests:
            required_text(digest, "named image digest")
        object.__setattr__(self, "named_image_digests", digests)
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "created_at")
        )

    def to_dict(self) -> JsonDict:
        payload = dict(self.__dict__)
        payload["deleted_counts"] = dict(self.deleted_counts)
        payload["named_image_digests"] = list(self.named_image_digests)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunDeletionRecord":
        payload = _without_receipt_version(payload, "run deletion")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run deletion")
        values = dict(payload)
        values["named_image_digests"] = tuple(values["named_image_digests"])
        return cls(**values)


@dataclass(frozen=True)
class RunRetirementReceipt:
    owner_commit: OwnerCommitReceipt
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    retirement: RunRetirementRecord

    def to_dict(self) -> JsonDict:
        return {
            "owner_commit": self.owner_commit.to_dict(),
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "retirement": self.retirement.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunRetirementReceipt":
        payload = _without_receipt_version(payload, "run retirement receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run retirement receipt")
        return cls(
            OwnerCommitReceipt.from_dict(payload["owner_commit"]),
            RunNamespaceRecord.from_dict(payload["run"]),
            RunRevisionRecord.from_dict(payload["revision"]),
            RunRetirementRecord.from_dict(payload["retirement"]),
        )


@dataclass(frozen=True)
class RunAdmissionReceipt:
    owner_commit: OwnerCommitReceipt
    run: RunNamespaceRecord
    revision: RunRevisionRecord
    candidates: Tuple[RunCandidateRecord, ...]
    logical_trials: Tuple[LogicalTrialRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.owner_commit, OwnerCommitReceipt):
            raise TypeError("owner_commit must be an OwnerCommitReceipt.")
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be a RunRevisionRecord.")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "logical_trials", tuple(self.logical_trials))

    def to_dict(self) -> JsonDict:
        return {
            "owner_commit": self.owner_commit.to_dict(),
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "logical_trials": [item.to_dict() for item in self.logical_trials],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunAdmissionReceipt":
        payload = _without_receipt_version(payload, "run admission receipt")
        _exact_keys(payload, set(cls.__dataclass_fields__), "run admission receipt")
        return cls(
            owner_commit=OwnerCommitReceipt.from_dict(payload["owner_commit"]),
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            candidates=tuple(
                RunCandidateRecord.from_dict(item) for item in payload["candidates"]
            ),
            logical_trials=tuple(
                LogicalTrialRecord.from_dict(item) for item in payload["logical_trials"]
            ),
        )


@dataclass(frozen=True)
class RunCandidateSelection:
    run_id: str
    evaluation_template_digest: str
    run_revision: int
    owner_revision: int
    sequence: int
    candidate_id: str
    candidate_ref: CandidateRef
    selection_digest: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        evaluation_template_digest: str,
        run_revision: int,
        owner_revision: int,
        sequence: int,
        candidate_id: str,
        candidate_ref: CandidateRef,
    ) -> "RunCandidateSelection":
        return cls(
            run_id,
            evaluation_template_digest,
            run_revision,
            owner_revision,
            sequence,
            candidate_id,
            candidate_ref,
            _selection_digest(
                run_id=run_id,
                evaluation_template_digest=evaluation_template_digest,
                run_revision=run_revision,
                owner_revision=owner_revision,
                sequence=sequence,
                candidate_id=candidate_id,
                candidate_ref=candidate_ref,
            ),
        )

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        lower_hex_digest(self.evaluation_template_digest, "evaluation template digest")
        positive_int(self.run_revision, "run revision")
        nonnegative_int(self.owner_revision, "owner revision")
        positive_int(self.sequence, "selection sequence")
        required_text(self.candidate_id, "candidate id")
        if not isinstance(self.candidate_ref, CandidateRef):
            raise TypeError("candidate_ref must be a CandidateRef.")
        expected = _selection_digest(
            run_id=self.run_id,
            evaluation_template_digest=self.evaluation_template_digest,
            run_revision=self.run_revision,
            owner_revision=self.owner_revision,
            sequence=self.sequence,
            candidate_id=self.candidate_id,
            candidate_ref=self.candidate_ref,
        )
        if self.selection_digest != expected:
            raise ValueError("selection digest differs from its immutable anchor.")

    def to_dict(self) -> JsonDict:
        return {
            "schema": _SELECTION_SCHEMA,
            "run_id": self.run_id,
            "evaluation_template_digest": self.evaluation_template_digest,
            "run_revision": self.run_revision,
            "owner_revision": self.owner_revision,
            "sequence": self.sequence,
            "candidate_id": self.candidate_id,
            "candidate_ref": str(self.candidate_ref),
            "selection_digest": self.selection_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunCandidateSelection":
        _exact_keys(
            payload,
            {
                "schema",
                "run_id",
                "evaluation_template_digest",
                "run_revision",
                "owner_revision",
                "sequence",
                "candidate_id",
                "candidate_ref",
                "selection_digest",
            },
            "run candidate selection",
        )
        if payload["schema"] != _SELECTION_SCHEMA:
            raise RealmIntegrityError("Run candidate selection schema is unsupported.")
        try:
            return cls(
                run_id=payload["run_id"],
                evaluation_template_digest=payload["evaluation_template_digest"],
                run_revision=payload["run_revision"],
                owner_revision=payload["owner_revision"],
                sequence=payload["sequence"],
                candidate_id=payload["candidate_id"],
                candidate_ref=CandidateRef.parse(payload["candidate_ref"]),
                selection_digest=payload["selection_digest"],
            )
        except (TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Run candidate selection is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class ResolvedRunCandidate:
    selection: RunCandidateSelection
    record: RunCandidateRecord
    content_bindings: Tuple[OwnerMembership, ...]
    availability: str = "available"

    def __post_init__(self) -> None:
        if not isinstance(self.selection, RunCandidateSelection):
            raise TypeError("selection must be a RunCandidateSelection.")
        if not isinstance(self.record, RunCandidateRecord):
            raise TypeError("record must be a RunCandidateRecord.")
        bindings = tuple(sorted(set(self.content_bindings)))
        if any(item.role != RUN_CANDIDATE_ROLE for item in bindings):
            raise ValueError("candidate content bindings require role run-candidate.")
        expected_refs = set(self.record.admission.envelope.content_refs)
        actual_refs = {item.content_ref for item in bindings}
        if not actual_refs.issubset(expected_refs):
            raise ValueError("candidate bindings contain undeclared content refs.")
        if self.availability not in {"available", "unavailable"}:
            raise ValueError("candidate availability is unsupported.")
        if (actual_refs == expected_refs) != (self.availability == "available"):
            raise ValueError(
                "candidate availability differs from its content bindings."
            )
        object.__setattr__(self, "content_bindings", bindings)


@dataclass(frozen=True)
class ResolvedRunCandidateEvaluation:
    candidate: ResolvedRunCandidate
    evaluation: ResolvedRunEvaluationClosure

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ResolvedRunCandidate):
            raise TypeError("candidate must be a ResolvedRunCandidate.")
        if not isinstance(self.evaluation, ResolvedRunEvaluationClosure):
            raise TypeError("evaluation must be a ResolvedRunEvaluationClosure.")
        if (
            self.candidate.selection.evaluation_template_digest
            != self.evaluation.closure.evaluation_template.digest
        ):
            raise ValueError("candidate selection targets another evaluation template.")

    def to_dict(self) -> JsonDict:
        return {
            "candidate": {
                "selection": self.candidate.selection.to_dict(),
                "record": self.candidate.record.to_dict(),
                "content_bindings": [
                    item.to_dict() for item in self.candidate.content_bindings
                ],
                "availability": self.candidate.availability,
            },
            "evaluation": self.evaluation.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolvedRunCandidateEvaluation":
        try:
            _exact_keys(
                payload, {"candidate", "evaluation"}, "resolved candidate evaluation"
            )
            candidate = payload["candidate"]
            _exact_keys(
                candidate,
                {"selection", "record", "content_bindings", "availability"},
                "resolved candidate",
            )
            return cls(
                candidate=ResolvedRunCandidate(
                    selection=RunCandidateSelection.from_dict(candidate["selection"]),
                    record=RunCandidateRecord.from_dict(candidate["record"]),
                    content_bindings=tuple(
                        OwnerMembership.from_dict(item)
                        for item in candidate["content_bindings"]
                    ),
                    availability=candidate["availability"],
                ),
                evaluation=ResolvedRunEvaluationClosure.from_dict(
                    payload["evaluation"]
                ),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted resolved candidate evaluation is invalid: {error}"
            ) from error


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    result = _freeze(value, label)
    assert isinstance(result, Mapping)
    canonical_json_bytes(_thaw(result))
    return result


def _selection_digest(
    *,
    run_id: str,
    evaluation_template_digest: str,
    run_revision: int,
    owner_revision: int,
    sequence: int,
    candidate_id: str,
    candidate_ref: CandidateRef,
) -> str:
    return request_digest(
        {
            "schema": _SELECTION_SCHEMA,
            "run_id": run_id,
            "evaluation_template_digest": evaluation_template_digest,
            "run_revision": run_revision,
            "owner_revision": owner_revision,
            "sequence": sequence,
            "candidate_id": candidate_id,
            "candidate_ref": str(candidate_ref),
        }
    )


def _freeze(value: Any, label: str) -> Any:
    if isinstance(value, Mapping):
        result: JsonDict = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} keys must be strings.")
            result[key] = _freeze(child, f"{label}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{label}[]") for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError(f"{label} must contain finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{label} must contain canonical JSON values.")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _unique(values: Sequence[Any] | Any, label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"Run admission plan contains duplicate {label}.")
        seen.add(value)


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
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
        raise RealmIntegrityError(f"{label} receipt version is unsupported.")
    return result


__all__ = [
    "CandidateAdmission",
    "LogicalTrialAdmission",
    "LogicalTrialRecord",
    "NormalizedCandidateEnvelope",
    "ResolvedRunCandidate",
    "RunAdmissionPlan",
    "RunAdmissionReceipt",
    "RunCandidateRecord",
    "RunCandidateSelection",
    "RunCreateReceipt",
    "RunNamespaceRecord",
    "RunRevisionRecord",
    "SessionHandleAdmission",
    "RUN_CANDIDATE_ROLE",
]
