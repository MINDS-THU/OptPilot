"""Pure contracts for deriving an exact-plan child run from a sealed parent.

The records here describe *what* a child must evaluate.  They deliberately do
not carry candidate envelopes, content memberships, paths, observations, or a
ready ledger admission plan.  The actor-bound write service must re-resolve all
of those facts from RealmLedger under its own authorization and write fence.

The first supported preset is intentionally narrow: reuse the sealed parent's
exact definition and selected candidate identities, repeat their exact ordered
evaluation coordinates, admit exactly that many logical trials, and never
start a method.  Future presets can extend the general child-run service without
weakening this format-neutral exact-plan contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..run_execution_profile import RunExecutionProfile
from ._validation import (
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
    thaw_json,
)
from .leases import LeaseRecord
from .errors import RealmConflict, RealmIntegrityError
from .refs import CandidateRef, canonical_json_bytes
from .refs import request_digest as canonical_request_digest
from .run_records import (
    RunAdmissionReceipt,
    RunCandidateRecord,
    RunCreateReceipt,
)
from .run_snapshot import RunLedgerSnapshot
from .run_terminal_seal import RunTerminalAnchor


CHILD_RUN_CANDIDATE_ANCHOR_SCHEMA = "optpilot.child-run-candidate-anchor.v1"
CHILD_RUN_EVALUATION_COORDINATE_SCHEMA = "optpilot.child-run-evaluation-coordinate.v1"
CHILD_RUN_EVALUATION_PLAN_SCHEMA = "optpilot.child-run-evaluation-plan.v1"
EXACT_PLAN_CHILD_RUN_REQUEST_SCHEMA = "optpilot.exact-plan-child-run-request.v2"
EXACT_PLAN_CHILD_RUN_RECEIPT_SCHEMA = "optpilot.exact-plan-child-run-receipt.v2"
EXACT_PLAN_CHILD_RUN_COMMIT_RECEIPT_SCHEMA = (
    "optpilot.exact-plan-child-run-commit-receipt.v2"
)
EXACT_PLAN_CHILD_RUN_LINEAGE_SCHEMA = "optpilot.exact-plan-child-run-lineage.v2"

EXACT_PLAN_METHOD_POLICY = "none"
EXACT_PLAN_SOURCE_POLICY = "reuse_exact"

# One explicit request must remain safe to validate, retain, and show in full
# before confirmation.  Execution may still schedule the admitted coordinates
# in smaller evaluator batches.
MAX_EXACT_PLAN_CANDIDATES = 4096
MAX_EXACT_PLAN_COORDINATES = 4096
MAX_EXACT_PLAN_BYTES = 1024 * 1024

_CANDIDATE_ANCHOR_DIGEST_DOMAIN = b"optpilot/child-run-candidate-anchor/v1"
_COORDINATE_DIGEST_DOMAIN = b"optpilot/child-run-evaluation-coordinate/v1"
_PLAN_DIGEST_DOMAIN = b"optpilot/child-run-evaluation-plan/v1"
_REQUEST_DIGEST_DOMAIN = b"optpilot/exact-plan-child-run-request/v2"
_OPERATION_IDENTITY_SCHEMA = "optpilot.exact-plan-child-run-operation.v1"


def _digest(domain: bytes, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_json_bytes(payload)).hexdigest()


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
    if len(canonical_json_bytes(payload)) > MAX_EXACT_PLAN_BYTES:
        raise ValueError(f"{label} exceeds {MAX_EXACT_PLAN_BYTES} encoded bytes.")


def _coordinate_key(
    coordinate: "ChildRunEvaluationCoordinate",
) -> tuple[str, bytes, int]:
    return (
        str(coordinate.candidate_ref),
        canonical_json_bytes(thaw_json(coordinate.seed)),
        coordinate.repetition_index,
    )


@dataclass(frozen=True)
class ExactPlanChildRunIdentities:
    """Opaque deterministic identities for one idempotent child operation."""

    run_id: str
    owner_id: str
    controller_holder_id: str
    internal_admit_operation_id: str
    owner_change_id: str
    owner_change_retention_lease_id: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            required_text(getattr(self, name), name.replace("_", " "), max_bytes=512)


def exact_plan_child_run_identities(
    operation_id: str,
) -> ExactPlanChildRunIdentities:
    """Derive all opaque namespace coordinates without opening Realm."""

    required_text(operation_id, "child-run operation id", max_bytes=512)
    digest = canonical_request_digest(
        {"operation_id": operation_id, "schema": _OPERATION_IDENTITY_SCHEMA}
    )
    coordinate = digest[:32]
    return ExactPlanChildRunIdentities(
        run_id=f"run-{coordinate}",
        owner_id=f"run-owner-{coordinate}",
        controller_holder_id=f"child-controller-{coordinate}",
        internal_admit_operation_id=f"child-run-admit-{coordinate}",
        owner_change_id=f"child-run-change-{coordinate}",
        owner_change_retention_lease_id=f"child-run-retention-{coordinate}",
    )


@dataclass(frozen=True)
class ChildRunCandidateAnchor:
    """Parent-local immutable identity of one selected candidate entity."""

    parent_run_id: str
    candidate_id: str
    candidate_ref: CandidateRef
    accepted_sequence: int

    def __post_init__(self) -> None:
        required_text(self.parent_run_id, "parent run id")
        required_text(self.candidate_id, "parent candidate id")
        if not isinstance(self.candidate_ref, CandidateRef):
            raise TypeError("candidate_ref must be a CandidateRef.")
        positive_int(self.accepted_sequence, "candidate accepted sequence")

    @classmethod
    def from_record(cls, record: RunCandidateRecord) -> "ChildRunCandidateAnchor":
        if not isinstance(record, RunCandidateRecord):
            raise TypeError("record must be a RunCandidateRecord.")
        return cls(
            parent_run_id=record.run_id,
            candidate_id=record.candidate_id,
            candidate_ref=record.candidate_ref,
            accepted_sequence=record.accepted_sequence,
        )

    @property
    def digest(self) -> str:
        return _digest(_CANDIDATE_ANCHOR_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_sequence": self.accepted_sequence,
            "candidate_id": self.candidate_id,
            "candidate_ref": str(self.candidate_ref),
            "parent_run_id": self.parent_run_id,
            "schema": CHILD_RUN_CANDIDATE_ANCHOR_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChildRunCandidateAnchor":
        _exact_keys(
            payload,
            {
                "accepted_sequence",
                "candidate_id",
                "candidate_ref",
                "parent_run_id",
                "schema",
            },
            "child-run candidate anchor",
        )
        if payload["schema"] != CHILD_RUN_CANDIDATE_ANCHOR_SCHEMA:
            raise ValueError("child-run candidate anchor schema is unsupported.")
        return cls(
            parent_run_id=payload["parent_run_id"],
            candidate_id=payload["candidate_id"],
            candidate_ref=CandidateRef.parse(payload["candidate_ref"]),
            accepted_sequence=payload["accepted_sequence"],
        )


@dataclass(frozen=True)
class ChildRunEvaluationCoordinate:
    """One child logical trial, tied to its exact parent budget coordinate."""

    candidate_ref: CandidateRef
    parent_logical_trial_id: str
    parent_budget_slot: int
    seed: Any
    repetition_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_ref, CandidateRef):
            raise TypeError("candidate_ref must be a CandidateRef.")
        required_text(self.parent_logical_trial_id, "parent logical trial id")
        positive_int(self.parent_budget_slot, "parent budget slot")
        object.__setattr__(
            self,
            "seed",
            freeze_json(self.seed, label="child-run evaluation seed"),
        )
        nonnegative_int(self.repetition_index, "evaluation repetition index")

    @property
    def digest(self) -> str:
        return _digest(_COORDINATE_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": str(self.candidate_ref),
            "parent_budget_slot": self.parent_budget_slot,
            "parent_logical_trial_id": self.parent_logical_trial_id,
            "repetition_index": self.repetition_index,
            "schema": CHILD_RUN_EVALUATION_COORDINATE_SCHEMA,
            "seed": thaw_json(self.seed),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChildRunEvaluationCoordinate":
        _exact_keys(
            payload,
            {
                "candidate_ref",
                "parent_budget_slot",
                "parent_logical_trial_id",
                "repetition_index",
                "schema",
                "seed",
            },
            "child-run evaluation coordinate",
        )
        if payload["schema"] != CHILD_RUN_EVALUATION_COORDINATE_SCHEMA:
            raise ValueError("child-run evaluation coordinate schema is unsupported.")
        return cls(
            candidate_ref=CandidateRef.parse(payload["candidate_ref"]),
            parent_logical_trial_id=payload["parent_logical_trial_id"],
            parent_budget_slot=payload["parent_budget_slot"],
            seed=payload["seed"],
            repetition_index=payload["repetition_index"],
        )


@dataclass(frozen=True)
class ChildRunEvaluationPlan:
    """Complete explicit logical-trial plan for one methodless child run."""

    coordinates: tuple[ChildRunEvaluationCoordinate, ...]
    max_trials: int

    def __post_init__(self) -> None:
        coordinates = tuple(self.coordinates)
        if not coordinates:
            raise ValueError("An exact child-run evaluation plan cannot be empty.")
        if len(coordinates) > MAX_EXACT_PLAN_COORDINATES:
            raise ValueError(
                "Exact child-run evaluation plan exceeds "
                f"{MAX_EXACT_PLAN_COORDINATES} coordinates."
            )
        if any(
            not isinstance(item, ChildRunEvaluationCoordinate) for item in coordinates
        ):
            raise TypeError(
                "coordinates must contain ChildRunEvaluationCoordinate values."
            )
        positive_int(self.max_trials, "child-run max_trials")
        if self.max_trials != len(coordinates):
            raise ValueError(
                "Exact child-run max_trials must equal its explicit coordinate count."
            )
        slots = tuple(item.parent_budget_slot for item in coordinates)
        if slots != tuple(sorted(slots)) or len(set(slots)) != len(slots):
            raise ValueError(
                "Exact child-run coordinates must preserve unique parent budget order."
            )
        logical_trial_ids = tuple(item.parent_logical_trial_id for item in coordinates)
        if len(set(logical_trial_ids)) != len(logical_trial_ids):
            raise ValueError(
                "Exact child-run plan contains duplicate parent logical trial ids."
            )
        keys = tuple(_coordinate_key(item) for item in coordinates)
        if len(set(keys)) != len(keys):
            raise ValueError(
                "Exact child-run plan contains duplicate candidate/seed/repetition "
                "coordinates."
            )
        object.__setattr__(self, "coordinates", coordinates)
        _bounded_record(self.to_dict(), "exact child-run evaluation plan")

    @property
    def digest(self) -> str:
        return _digest(_PLAN_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinates": [item.to_dict() for item in self.coordinates],
            "max_trials": self.max_trials,
            "schema": CHILD_RUN_EVALUATION_PLAN_SCHEMA,
        }

    def to_internal_confirmation_dict(self) -> dict[str, Any]:
        """Return the complete bounded plan for trusted service-side review.

        This is not a browser projection: it intentionally contains immutable
        Realm refs and parent logical-trial ids.  A presentation service must
        mint a separately redacted public view after authorization.
        """

        return {
            "coordinate_count": len(self.coordinates),
            "coordinates": [item.to_dict() for item in self.coordinates],
            "max_trials": self.max_trials,
            "plan_digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChildRunEvaluationPlan":
        _exact_keys(
            payload,
            {"coordinates", "max_trials", "schema"},
            "child-run evaluation plan",
        )
        if payload["schema"] != CHILD_RUN_EVALUATION_PLAN_SCHEMA:
            raise ValueError("child-run evaluation plan schema is unsupported.")
        coordinates = payload["coordinates"]
        if not isinstance(coordinates, list):
            raise TypeError("child-run evaluation plan coordinates must be a list.")
        return cls(
            coordinates=tuple(
                ChildRunEvaluationCoordinate.from_dict(item) for item in coordinates
            ),
            max_trials=payload["max_trials"],
        )


@dataclass(frozen=True)
class ExactPlanChildRunRequest:
    """Canonical plural request for the terminal-parent exact-plan preset."""

    parent: RunTerminalAnchor
    candidates: tuple[ChildRunCandidateAnchor, ...]
    evaluation_plan: ChildRunEvaluationPlan
    execution_profile: RunExecutionProfile = field(
        default_factory=RunExecutionProfile
    )
    method_policy: str = EXACT_PLAN_METHOD_POLICY
    source_policy: str = EXACT_PLAN_SOURCE_POLICY
    config_overrides: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.parent, RunTerminalAnchor):
            raise TypeError("parent must be a RunTerminalAnchor.")
        candidates = tuple(self.candidates)
        if not candidates:
            raise ValueError("An exact child-run request requires selected candidates.")
        if len(candidates) > MAX_EXACT_PLAN_CANDIDATES:
            raise ValueError(
                f"Exact child-run request exceeds {MAX_EXACT_PLAN_CANDIDATES} candidates."
            )
        if any(not isinstance(item, ChildRunCandidateAnchor) for item in candidates):
            raise TypeError("candidates must contain ChildRunCandidateAnchor values.")
        sequences = tuple(item.accepted_sequence for item in candidates)
        if sequences != tuple(sorted(sequences)) or len(set(sequences)) != len(
            sequences
        ):
            raise ValueError(
                "Exact child-run candidates must be in unique parent acceptance order."
            )
        if any(item.parent_run_id != self.parent.run_id for item in candidates):
            raise ValueError(
                "Selected candidate anchor refers to a different parent run."
            )
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValueError(
                "Exact child-run request contains duplicate candidate ids."
            )
        candidate_refs = {item.candidate_ref for item in candidates}
        if len(candidate_refs) != len(candidates):
            raise ValueError(
                "Exact child-run request contains duplicate candidate refs."
            )
        if not isinstance(self.evaluation_plan, ChildRunEvaluationPlan):
            raise TypeError("evaluation_plan must be a ChildRunEvaluationPlan.")
        if not isinstance(self.execution_profile, RunExecutionProfile):
            raise TypeError("execution_profile must be a RunExecutionProfile.")
        planned_refs = {item.candidate_ref for item in self.evaluation_plan.coordinates}
        if planned_refs != candidate_refs:
            raise ValueError(
                "Every selected candidate must have coordinates and every coordinate "
                "must refer to a selected candidate."
            )
        if self.method_policy != EXACT_PLAN_METHOD_POLICY:
            raise ValueError("Exact-plan re-evaluation must use method_policy='none'.")
        if self.source_policy != EXACT_PLAN_SOURCE_POLICY:
            raise ValueError(
                "Exact-plan re-evaluation must use source_policy='reuse_exact'."
            )
        if not isinstance(self.config_overrides, Mapping):
            raise TypeError("config_overrides must be a mapping.")
        overrides = freeze_json(
            self.config_overrides, label="exact child-run config overrides"
        )
        if overrides:
            raise ValueError(
                "Exact-plan re-evaluation does not allow config overrides."
            )
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "config_overrides", MappingProxyType({}))
        _bounded_record(self.to_dict(), "exact child-run request")

    @property
    def max_trials(self) -> int:
        return self.evaluation_plan.max_trials

    @property
    def digest(self) -> str:
        return _digest(_REQUEST_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "config_overrides": {},
            "evaluation_plan": self.evaluation_plan.to_dict(),
            "execution_profile": self.execution_profile.to_dict(),
            "method_policy": self.method_policy,
            "parent": self.parent.to_dict(),
            "schema": EXACT_PLAN_CHILD_RUN_REQUEST_SCHEMA,
            "source_policy": self.source_policy,
        }

    def to_internal_confirmation_dict(self) -> dict[str, Any]:
        """Return all request facts for trusted service-side confirmation.

        The result contains owner/content authority coordinates and must not be
        sent to a browser.  Studio uses an actor-bound redacted projection.
        """

        return {
            "candidate_count": len(self.candidates),
            "candidates": [item.to_dict() for item in self.candidates],
            "config_overrides": {},
            "evaluation_plan": (self.evaluation_plan.to_internal_confirmation_dict()),
            "execution_profile": self.execution_profile.to_dict(),
            "method_policy": self.method_policy,
            "parent": self.parent.to_dict(),
            "request_digest": self.digest,
            "source_policy": self.source_policy,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExactPlanChildRunRequest":
        _exact_keys(
            payload,
            {
                "candidates",
                "config_overrides",
                "evaluation_plan",
                "execution_profile",
                "method_policy",
                "parent",
                "schema",
                "source_policy",
            },
            "exact-plan child-run request",
        )
        if payload["schema"] != EXACT_PLAN_CHILD_RUN_REQUEST_SCHEMA:
            raise ValueError("exact-plan child-run request schema is unsupported.")
        candidates = payload["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("exact-plan child-run candidates must be a list.")
        return cls(
            parent=RunTerminalAnchor.from_dict(payload["parent"]),
            candidates=tuple(
                ChildRunCandidateAnchor.from_dict(item) for item in candidates
            ),
            evaluation_plan=ChildRunEvaluationPlan.from_dict(
                payload["evaluation_plan"]
            ),
            execution_profile=RunExecutionProfile.from_dict(
                payload["execution_profile"]
            ),
            method_policy=payload["method_policy"],
            source_policy=payload["source_policy"],
            config_overrides=payload["config_overrides"],
        )


@dataclass(frozen=True)
class ExactPlanChildRunLineage:
    """Bounded recovery and audit commitment retained in child metadata."""

    parent_run_id: str
    parent_seal_digest: str
    parent_definition_digest: str
    request_digest: str
    evaluation_plan_digest: str
    candidate_anchor_digests: tuple[str, ...]
    selected_candidate_count: int
    logical_trial_count: int
    execution_profile: RunExecutionProfile
    method_policy: str
    source_policy: str

    def __post_init__(self) -> None:
        required_text(self.parent_run_id, "child lineage parent run id")
        for value, label in (
            (self.parent_seal_digest, "child lineage parent seal digest"),
            (
                self.parent_definition_digest,
                "child lineage parent definition digest",
            ),
            (self.request_digest, "child lineage request digest"),
            (
                self.evaluation_plan_digest,
                "child lineage evaluation plan digest",
            ),
        ):
            lower_hex_digest(value, label)
        candidate_anchor_digests = tuple(self.candidate_anchor_digests)
        if not candidate_anchor_digests:
            raise ValueError("Child lineage requires selected candidate commitments.")
        for digest in candidate_anchor_digests:
            lower_hex_digest(digest, "child lineage candidate anchor digest")
        if len(set(candidate_anchor_digests)) != len(candidate_anchor_digests):
            raise ValueError("Child lineage candidate commitments must be unique.")
        positive_int(
            self.selected_candidate_count,
            "child lineage selected candidate count",
        )
        positive_int(self.logical_trial_count, "child lineage logical trial count")
        if self.selected_candidate_count != len(candidate_anchor_digests):
            raise ValueError(
                "Child lineage candidate count differs from its commitments."
            )
        if not isinstance(self.execution_profile, RunExecutionProfile):
            raise TypeError("Child lineage execution_profile is invalid.")
        if self.method_policy != EXACT_PLAN_METHOD_POLICY:
            raise ValueError("Child lineage requires method_policy='none'.")
        if self.source_policy != EXACT_PLAN_SOURCE_POLICY:
            raise ValueError("Child lineage requires source_policy='reuse_exact'.")
        object.__setattr__(self, "candidate_anchor_digests", candidate_anchor_digests)
        _bounded_record(self.to_dict(), "exact child-run lineage")

    @classmethod
    def from_request(
        cls, request: ExactPlanChildRunRequest
    ) -> "ExactPlanChildRunLineage":
        if not isinstance(request, ExactPlanChildRunRequest):
            raise TypeError("request must be an ExactPlanChildRunRequest.")
        return cls(
            parent_run_id=request.parent.run_id,
            parent_seal_digest=request.parent.seal_digest,
            parent_definition_digest=request.parent.definition_digest,
            request_digest=request.digest,
            evaluation_plan_digest=request.evaluation_plan.digest,
            candidate_anchor_digests=tuple(item.digest for item in request.candidates),
            selected_candidate_count=len(request.candidates),
            logical_trial_count=request.max_trials,
            execution_profile=request.execution_profile,
            method_policy=request.method_policy,
            source_policy=request.source_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_anchor_digests": list(self.candidate_anchor_digests),
            "evaluation_plan_digest": self.evaluation_plan_digest,
            "execution_profile": self.execution_profile.to_dict(),
            "logical_trial_count": self.logical_trial_count,
            "method_policy": self.method_policy,
            "parent_definition_digest": self.parent_definition_digest,
            "parent_run_id": self.parent_run_id,
            "parent_seal_digest": self.parent_seal_digest,
            "request_digest": self.request_digest,
            "schema": EXACT_PLAN_CHILD_RUN_LINEAGE_SCHEMA,
            "selected_candidate_count": self.selected_candidate_count,
            "source_policy": self.source_policy,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExactPlanChildRunLineage":
        _exact_keys(
            payload,
            {
                "candidate_anchor_digests",
                "evaluation_plan_digest",
                "execution_profile",
                "logical_trial_count",
                "method_policy",
                "parent_definition_digest",
                "parent_run_id",
                "parent_seal_digest",
                "request_digest",
                "schema",
                "selected_candidate_count",
                "source_policy",
            },
            "exact child-run lineage",
        )
        if payload["schema"] != EXACT_PLAN_CHILD_RUN_LINEAGE_SCHEMA:
            raise ValueError("exact child-run lineage schema is unsupported.")
        digests = payload["candidate_anchor_digests"]
        if not isinstance(digests, (list, tuple)):
            raise TypeError("child lineage candidate commitments must be a sequence.")
        return cls(
            parent_run_id=payload["parent_run_id"],
            parent_seal_digest=payload["parent_seal_digest"],
            parent_definition_digest=payload["parent_definition_digest"],
            request_digest=payload["request_digest"],
            evaluation_plan_digest=payload["evaluation_plan_digest"],
            candidate_anchor_digests=tuple(digests),
            selected_candidate_count=payload["selected_candidate_count"],
            logical_trial_count=payload["logical_trial_count"],
            execution_profile=RunExecutionProfile.from_dict(
                payload["execution_profile"]
            ),
            method_policy=payload["method_policy"],
            source_policy=payload["source_policy"],
        )


def exact_plan_child_lineage_from_snapshot(
    snapshot: RunLedgerSnapshot,
) -> ExactPlanChildRunLineage:
    """Validate and return one run's typed methodless child lineage."""

    if not isinstance(snapshot, RunLedgerSnapshot):
        raise TypeError("snapshot must be a RunLedgerSnapshot.")
    lineage_payload = snapshot.definition.metadata.get("child_run")
    if not isinstance(lineage_payload, Mapping):
        raise RealmConflict("Run is not an exact-plan child run.")
    try:
        lineage = ExactPlanChildRunLineage.from_dict(lineage_payload)
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError("Exact-plan child lineage is invalid.") from error
    if (
        lineage.logical_trial_count != snapshot.run.max_trials
        or lineage.logical_trial_count != snapshot.run.accepted_logical_trials
        or lineage.selected_candidate_count != len(snapshot.candidates)
        or snapshot.method_exchange_preparations
        or snapshot.method_exchange_completions
    ):
        raise RealmIntegrityError(
            "Exact-plan child is not a complete methodless seed plan."
        )
    submission = snapshot.control.current_submission
    if snapshot.run.state == "running":
        if (
            snapshot.run.retention_state != "active"
            or snapshot.finalization is not None
            or snapshot.terminal_seal is not None
            or submission.state != "draining"
            or submission.stop_code != "max_trials"
        ):
            raise RealmIntegrityError(
                "Exact-plan child is not a reconcilable methodless seed plan."
            )
    elif (
        submission.state != "terminal"
        or snapshot.finalization is None
        or snapshot.terminal_seal is None
    ):
        raise RealmIntegrityError(
            "Terminal exact-plan child lacks canonical finalization."
        )
    return lineage


@dataclass(frozen=True)
class ExactPlanChildRunReceipt:
    """Pure builder receipt; no child namespace has been created yet."""

    request: ExactPlanChildRunRequest
    parent_definition_digest: str
    parent_evaluation_template_digest: str
    plan_digest: str
    request_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExactPlanChildRunRequest):
            raise TypeError("request must be an ExactPlanChildRunRequest.")
        lower_hex_digest(self.parent_definition_digest, "parent run definition digest")
        lower_hex_digest(
            self.parent_evaluation_template_digest,
            "parent evaluation template digest",
        )
        lower_hex_digest(self.plan_digest, "child-run evaluation plan digest")
        lower_hex_digest(self.request_digest, "exact child-run request digest")
        if self.parent_definition_digest != self.request.parent.definition_digest:
            raise ValueError(
                "Receipt parent definition digest differs from the terminal anchor."
            )
        if self.plan_digest != self.request.evaluation_plan.digest:
            raise ValueError("Receipt plan digest differs from the exact request.")
        if self.request_digest != self.request.digest:
            raise ValueError("Receipt request digest differs from the exact request.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_definition_digest": self.parent_definition_digest,
            "parent_evaluation_template_digest": (
                self.parent_evaluation_template_digest
            ),
            "plan_digest": self.plan_digest,
            "request": self.request.to_dict(),
            "request_digest": self.request_digest,
            "schema": EXACT_PLAN_CHILD_RUN_RECEIPT_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExactPlanChildRunReceipt":
        _exact_keys(
            payload,
            {
                "parent_definition_digest",
                "parent_evaluation_template_digest",
                "plan_digest",
                "request",
                "request_digest",
                "schema",
            },
            "exact-plan child-run receipt",
        )
        if payload["schema"] != EXACT_PLAN_CHILD_RUN_RECEIPT_SCHEMA:
            raise ValueError("exact-plan child-run receipt schema is unsupported.")
        return cls(
            request=ExactPlanChildRunRequest.from_dict(payload["request"]),
            parent_definition_digest=payload["parent_definition_digest"],
            parent_evaluation_template_digest=payload[
                "parent_evaluation_template_digest"
            ],
            plan_digest=payload["plan_digest"],
            request_digest=payload["request_digest"],
        )


@dataclass(frozen=True)
class ExactPlanChildRunCommitReceipt:
    """Atomic child namespace creation and seed admission receipt."""

    parent: RunTerminalAnchor
    request_digest: str
    creation: RunCreateReceipt
    admission: RunAdmissionReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.parent, RunTerminalAnchor):
            raise TypeError("parent must be a RunTerminalAnchor.")
        lower_hex_digest(self.request_digest, "exact child-run request digest")
        if not isinstance(self.creation, RunCreateReceipt):
            raise TypeError("creation must be a RunCreateReceipt.")
        if not isinstance(self.admission, RunAdmissionReceipt):
            raise TypeError("admission must be a RunAdmissionReceipt.")
        created = self.creation
        admitted = self.admission
        if (
            created.run.run_id != admitted.run.run_id
            or created.run.owner_id != admitted.run.owner_id
            or created.revision.run_id != admitted.revision.run_id
            or created.run.current_revision != 0
            or created.revision.revision != 0
            or admitted.run.current_revision != 1
            or admitted.revision.revision != 1
            or admitted.run.accepted_logical_trials != len(admitted.logical_trials)
            or admitted.run.max_trials != len(admitted.logical_trials)
            or admitted.run.controller_lease_id != created.controller_lease.lease_id
            or admitted.run.controller_holder_id != created.controller_lease.holder_id
            or admitted.run.controller_fencing_token
            != created.controller_lease.fencing_token
            or created.revision.operation_kind != "run.create"
            or admitted.revision.operation_kind != "run.admit"
            or created.revision.txn_id >= admitted.revision.txn_id
        ):
            raise ValueError(
                "Atomic exact child-run creation and admission anchors differ."
            )
        if admitted.run.accepted_logical_trials < 1:
            raise ValueError(
                "Atomic exact child-run receipt must contain seeded trials."
            )

    @property
    def run_id(self) -> str:
        return self.admission.run.run_id

    @property
    def owner_id(self) -> str:
        return self.admission.run.owner_id

    @property
    def controller_lease(self) -> LeaseRecord:
        return self.creation.controller_lease

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission": self.admission.to_dict(),
            "creation": self.creation.to_dict(),
            "parent": self.parent.to_dict(),
            "request_digest": self.request_digest,
            "schema": EXACT_PLAN_CHILD_RUN_COMMIT_RECEIPT_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExactPlanChildRunCommitReceipt":
        # Ledger transaction receipts add their standard replay marker.
        values = dict(payload)
        receipt_version = values.pop("receipt_version", 1)
        if receipt_version != 1:
            raise ValueError("exact child-run receipt version is unsupported.")
        _exact_keys(
            values,
            {"admission", "creation", "parent", "request_digest", "schema"},
            "exact-plan child-run commit receipt",
        )
        if values["schema"] != EXACT_PLAN_CHILD_RUN_COMMIT_RECEIPT_SCHEMA:
            raise ValueError(
                "exact-plan child-run commit receipt schema is unsupported."
            )
        return cls(
            parent=RunTerminalAnchor.from_dict(values["parent"]),
            request_digest=values["request_digest"],
            creation=RunCreateReceipt.from_dict(values["creation"]),
            admission=RunAdmissionReceipt.from_dict(values["admission"]),
        )


def build_exact_plan_child_run_request(
    *,
    snapshot: RunLedgerSnapshot,
    parent: RunTerminalAnchor,
    selected_candidates: Sequence[ChildRunCandidateAnchor],
    execution_profile: RunExecutionProfile | None = None,
) -> ExactPlanChildRunReceipt:
    """Build the one safe preset from an exact sealed parent snapshot.

    This function is pure.  It does not authorize the caller, retain content,
    create an owner/run, or admit a trial.  The eventual write service must
    resolve this request again against RealmLedger before committing anything.
    """

    if not isinstance(snapshot, RunLedgerSnapshot):
        raise TypeError("snapshot must be a RunLedgerSnapshot.")
    if not isinstance(parent, RunTerminalAnchor):
        raise TypeError("parent must be a RunTerminalAnchor.")
    if execution_profile is None:
        execution_profile = RunExecutionProfile()
    elif not isinstance(execution_profile, RunExecutionProfile):
        raise TypeError("execution_profile must be a RunExecutionProfile or None.")
    if snapshot.run.state == "running" or snapshot.finalization is None:
        raise ValueError("Exact-plan child runs require a terminal parent snapshot.")
    terminal_seal = snapshot.terminal_seal
    if terminal_seal is None:
        raise ValueError("Exact-plan child runs require a sealed terminal parent.")
    if terminal_seal.anchor != parent:
        raise ValueError("Terminal parent anchor differs from the authorized snapshot.")

    requested = tuple(selected_candidates)
    if not requested:
        raise ValueError("At least one parent candidate must be selected.")
    if any(not isinstance(item, ChildRunCandidateAnchor) for item in requested):
        raise TypeError(
            "selected_candidates must contain ChildRunCandidateAnchor values."
        )
    if len(requested) > MAX_EXACT_PLAN_CANDIDATES:
        raise ValueError(
            f"Exact child-run request exceeds {MAX_EXACT_PLAN_CANDIDATES} candidates."
        )

    candidate_by_sequence = {
        item.accepted_sequence: item for item in snapshot.candidates
    }
    resolved: list[tuple[ChildRunCandidateAnchor, RunCandidateRecord]] = []
    for selected in requested:
        record = candidate_by_sequence.get(selected.accepted_sequence)
        if record is None or ChildRunCandidateAnchor.from_record(record) != selected:
            raise ValueError(
                "Selected candidate anchor differs from the terminal parent snapshot."
            )
        resolved.append((selected, record))
    resolved.sort(key=lambda item: item[0].accepted_sequence)
    anchors = tuple(item[0] for item in resolved)
    if len({item.candidate_ref for item in anchors}) != len(anchors):
        raise ValueError("Exact child-run request contains duplicate candidate refs.")

    candidate_ref_by_key = {
        record.candidate_key: anchor.candidate_ref for anchor, record in resolved
    }
    default_seed = snapshot.evaluation_closure.evaluation_template.default_seed
    coordinates: list[ChildRunEvaluationCoordinate] = []
    for trial in snapshot.logical_trials:
        candidate_ref = candidate_ref_by_key.get(trial.candidate_key)
        if candidate_ref is None:
            continue
        seed = trial.admission.seed
        if seed is None:
            seed = default_seed
        coordinates.append(
            ChildRunEvaluationCoordinate(
                candidate_ref=candidate_ref,
                parent_logical_trial_id=trial.admission.logical_trial_id,
                parent_budget_slot=trial.budget_slot,
                seed=thaw_json(seed),
                repetition_index=trial.admission.repetition_index,
            )
        )

    plan = ChildRunEvaluationPlan(
        coordinates=tuple(coordinates),
        max_trials=len(coordinates),
    )
    request = ExactPlanChildRunRequest(
        parent=parent,
        candidates=anchors,
        evaluation_plan=plan,
        execution_profile=execution_profile,
    )
    return ExactPlanChildRunReceipt(
        request=request,
        parent_definition_digest=snapshot.definition.digest,
        parent_evaluation_template_digest=(
            snapshot.evaluation_closure.evaluation_template.digest
        ),
        plan_digest=plan.digest,
        request_digest=request.digest,
    )


__all__ = [
    "CHILD_RUN_CANDIDATE_ANCHOR_SCHEMA",
    "CHILD_RUN_EVALUATION_COORDINATE_SCHEMA",
    "CHILD_RUN_EVALUATION_PLAN_SCHEMA",
    "EXACT_PLAN_CHILD_RUN_RECEIPT_SCHEMA",
    "EXACT_PLAN_CHILD_RUN_COMMIT_RECEIPT_SCHEMA",
    "EXACT_PLAN_CHILD_RUN_LINEAGE_SCHEMA",
    "EXACT_PLAN_CHILD_RUN_REQUEST_SCHEMA",
    "EXACT_PLAN_METHOD_POLICY",
    "EXACT_PLAN_SOURCE_POLICY",
    "MAX_EXACT_PLAN_BYTES",
    "MAX_EXACT_PLAN_CANDIDATES",
    "MAX_EXACT_PLAN_COORDINATES",
    "ChildRunCandidateAnchor",
    "ChildRunEvaluationCoordinate",
    "ChildRunEvaluationPlan",
    "ExactPlanChildRunReceipt",
    "ExactPlanChildRunCommitReceipt",
    "ExactPlanChildRunIdentities",
    "ExactPlanChildRunLineage",
    "ExactPlanChildRunRequest",
    "build_exact_plan_child_run_request",
    "exact_plan_child_lineage_from_snapshot",
    "exact_plan_child_run_identities",
]
