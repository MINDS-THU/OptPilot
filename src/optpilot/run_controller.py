"""Pure batch run-control state for proposal and logical-trial accounting.

This module deliberately has no persistence, scheduler, or worker side effects.
It is the WP1A state-machine boundary that a runner can place in front of those
components.  Fenced persistence and transactional evidence belong to the later
RunLedger integration.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .candidate_materialization import normalize_candidate
from .run_terminal_policy import (
    CANCELLATION_STOP_CODES,
    FAILURE_STOP_CODES,
    FINAL_FAILURE_OUTCOMES,
    NORMAL_STOP_CODES,
)


JsonDict = Dict[str, Any]
CandidateNormalizer = Callable[[JsonDict], Mapping[str, Any]]
LogicalTrialIdFactory = Callable[[], str]

RUN_STATUSES = frozenset({"running", "succeeded", "failed", "cancelled"})
STANDARD_OUTCOMES = frozenset({"success", "invalid", "failed", "timeout", "partial", "cancelled"})
FAILURE_OUTCOMES = FINAL_FAILURE_OUTCOMES

_CANCEL_STOP_CODES = CANCELLATION_STOP_CODES
_FAILURE_STOP_CODES = FAILURE_STOP_CODES
_NORMAL_STOP_CODES = NORMAL_STOP_CODES
_STOP_PRIORITY = {
    "method_completed": 10,
    "converged": 20,
    "max_trials": 30,
    "wall_clock_budget": 40,
    "max_failures": 50,
    "method_failed": 60,
    "evaluator_failed": 60,
    "controller_lost": 60,
    "protocol_error": 70,
    "user_cancelled": 80,
    "signal_cancelled": 80,
    "admin_cancelled": 80,
}


class RunControllerError(RuntimeError):
    """Base class for controller errors."""


class RunControllerStateError(RunControllerError):
    """Raised when the caller violates the controller integration contract."""


class MethodProtocolError(RunControllerError):
    """A method proposal was rejected before any logical slot was accepted."""

    def __init__(self, code: str, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ProposalRejection:
    """Structured rejection information for the future evidence adapter."""

    code: str
    message: str
    details: JsonDict


@dataclass(frozen=True)
class ControllerEvent:
    """Ordered pure transition that an evidence adapter may project."""

    controller_sequence: int
    event: str
    payload: JsonDict

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": "optpilot.controller.event.v1",
            "controller_sequence": self.controller_sequence,
            "event": self.event,
            **copy.deepcopy(self.payload),
        }


@dataclass(frozen=True)
class AcceptedLogicalTrial:
    """One accepted candidate and its one budget-consuming logical slot."""

    logical_trial_id: str
    candidate: JsonDict


@dataclass(frozen=True)
class PreparedProposal:
    """Pure, deterministic proposal result awaiting one canonical ledger commit."""

    admission_id: str
    expected_run_revision: int
    requested_width: int
    candidates: Tuple[JsonDict, ...]
    logical_trial_ids: Tuple[str, ...]
    digest: str

    @classmethod
    def build(
        cls,
        *,
        admission_id: str,
        expected_run_revision: int,
        requested_width: int,
        candidates: Sequence[Mapping[str, Any]],
        logical_trial_ids: Sequence[str],
    ) -> "PreparedProposal":
        copied = tuple(copy.deepcopy(dict(item)) for item in candidates)
        trial_ids = tuple(logical_trial_ids)
        payload = {
            "admission_id": admission_id,
            "expected_run_revision": expected_run_revision,
            "requested_width": requested_width,
            "candidates": copied,
            "logical_trial_ids": trial_ids,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            admission_id,
            expected_run_revision,
            requested_width,
            copied,
            trial_ids,
            digest,
        )

    @property
    def candidate_ids(self) -> Tuple[str, ...]:
        return tuple(item["candidate_id"] for item in self.candidates)


@dataclass(frozen=True)
class LogicalTrialSnapshot:
    """Read-only projection of controller-owned logical-trial state."""

    logical_trial_id: str
    candidate_id: str
    candidate: JsonDict
    state: str
    outcome: Optional[str]
    code: Optional[str]
    attempt_count: int
    observation_count: int
    metric_values: JsonDict
    completion_metadata: JsonDict


@dataclass(frozen=True)
class LogicalTrialRestoreState:
    """Canonical logical-trial facts needed to rebuild the controller cache.

    This is deliberately a small, Realm-neutral projection rather than a
    second persistence model.  A ledger reader derives it from typed candidate,
    logical-trial, attempt, and observation records.  ``terminal_sequence`` is
    required because convergence and tie handling depend on canonical
    completion order, not on candidate admission order.
    """

    logical_trial_id: str
    candidate: Mapping[str, Any]
    state: str
    outcome: Optional[str] = None
    code: Optional[str] = None
    terminal_sequence: Optional[int] = None
    attempt_count: int = 0
    observation_count: int = 0
    metric_values: Mapping[str, Any] = field(default_factory=dict)
    completion_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.logical_trial_id, str) or not self.logical_trial_id:
            raise ValueError("logical_trial_id must be a non-empty string.")
        if not isinstance(self.candidate, Mapping):
            raise TypeError("candidate must be a mapping.")
        if self.state not in {"accepted", "queued", "running", "retrying", "terminal"}:
            raise ValueError("logical trial restore state is unsupported.")
        if self.state == "terminal":
            if self.outcome not in STANDARD_OUTCOMES:
                raise ValueError("Terminal logical trial restore state requires an outcome.")
            _positive_int(self.terminal_sequence, "terminal_sequence")
        elif self.outcome is not None or self.code is not None or self.terminal_sequence is not None:
            raise ValueError(
                "Nonterminal logical trial restore state cannot define terminal facts."
            )
        if self.code is not None and (not isinstance(self.code, str) or not self.code):
            raise ValueError("code must be a non-empty string or None.")
        _nonnegative_int(self.attempt_count, "attempt_count")
        _nonnegative_int(self.observation_count, "observation_count")
        if self.observation_count > self.attempt_count:
            raise ValueError("observation_count cannot exceed attempt_count.")
        for field in ("metric_values", "completion_metadata"):
            value = getattr(self, field)
            if not isinstance(value, Mapping):
                raise TypeError(f"{field} must be a mapping.")
            object.__setattr__(self, field, _thaw_controller_value(value))
        object.__setattr__(self, "candidate", _thaw_controller_value(self.candidate))
        try:
            json.dumps(
                {
                    "candidate": self.candidate,
                    "metric_values": self.metric_values,
                    "completion_metadata": self.completion_metadata,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Logical trial restore facts must be JSON-compatible."
            ) from error


@dataclass(frozen=True)
class RunControllerRestoreState:
    """Validated canonical projection used to reconstruct a fresh controller.

    ``submission_stop_code`` records why proposal admission closed.
    ``terminal_code`` is the optional final run code.  They are separate facts:
    for example, submissions may drain because the trial budget was exhausted
    while the run ultimately fails because no successful observation exists.
    """

    run_status: str
    submission_state: str
    submission_stop_code: Optional[str]
    terminal_code: Optional[str]
    logical_trials: Tuple[LogicalTrialRestoreState, ...]

    def __post_init__(self) -> None:
        if self.run_status not in RUN_STATUSES:
            raise ValueError("run_status is unsupported.")
        if self.submission_state not in {"accepting", "draining", "terminal"}:
            raise ValueError("submission_state is unsupported.")
        for field in ("submission_stop_code", "terminal_code"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{field} must be a non-empty string or None.")
        if self.submission_state == "accepting":
            if self.run_status != "running" or self.submission_stop_code is not None:
                raise ValueError(
                    "Accepting submissions require a running run and no stop code."
                )
            if self.terminal_code is not None:
                raise ValueError("A running accepting run cannot define terminal_code.")
        elif self.submission_stop_code is None:
            raise ValueError("Draining or terminal submissions require a stop code.")
        if self.submission_state == "draining":
            if self.run_status != "running" or self.terminal_code is not None:
                raise ValueError(
                    "Draining submissions require a running, non-finalized run."
                )
        if self.submission_state == "terminal" and self.run_status == "running":
            raise ValueError("Terminal submissions require a terminal run status.")
        if self.run_status != "running" and self.submission_state != "terminal":
            raise ValueError("A terminal run requires terminal submission control.")
        trials = tuple(self.logical_trials)
        if any(not isinstance(item, LogicalTrialRestoreState) for item in trials):
            raise TypeError(
                "logical_trials must contain LogicalTrialRestoreState values."
            )
        if self.run_status != "running" and any(
            item.state != "terminal" for item in trials
        ):
            raise ValueError("A terminal run cannot contain nonterminal logical trials.")
        object.__setattr__(self, "logical_trials", trials)


@dataclass
class _LogicalTrial:
    logical_trial_id: str
    candidate: JsonDict
    state: str = "accepted"
    outcome: Optional[str] = None
    code: Optional[str] = None
    attempt_count: int = 0
    observation_count: int = 0
    metric_values: Optional[JsonDict] = None
    completion_metadata: Optional[JsonDict] = None


class _CandidateContractView:
    """Minimal view required by the existing public-shape normalizer."""

    def __init__(self, candidate_contract: Mapping[str, Any]):
        self.candidate = copy.deepcopy(dict(candidate_contract))


class RunController:
    """Own the deterministic state transitions of a synchronous batch run.

    The controller is intentionally single-threaded.  A caller asks
    :attr:`next_proposal_width`, invokes the method with that exact width, and
    passes the complete returned sequence to :meth:`accept_proposal`.  Accepted
    items each reserve exactly one logical trial; evaluator retries are reported
    later as completion metadata and never reserve another slot.
    """

    def __init__(
        self,
        *,
        method_id: str,
        candidate_contract: Mapping[str, Any],
        objective_metric: str,
        objective_direction: str,
        proposal_width: int,
        max_trials: Optional[int],
        max_failures: Optional[int] = None,
        patience_trials: Optional[int] = None,
        min_delta: float = 0.0,
        candidate_normalizer: Optional[CandidateNormalizer] = None,
        logical_trial_id_factory: Optional[LogicalTrialIdFactory] = None,
    ):
        if not isinstance(method_id, str) or not method_id:
            raise ValueError("method_id must be a non-empty string.")
        if not isinstance(candidate_contract, Mapping):
            raise TypeError("candidate_contract must be a mapping.")
        if not isinstance(objective_metric, str) or not objective_metric:
            raise ValueError("objective_metric must be a non-empty string.")
        if objective_direction not in {"minimize", "maximize"}:
            raise ValueError("objective_direction must be 'minimize' or 'maximize'.")
        self._proposal_width = _positive_int(proposal_width, "proposal_width")
        self._max_trials = _optional_positive_int(max_trials, "max_trials")
        self._max_failures = _optional_positive_int(max_failures, "max_failures")
        self._patience_trials = _optional_positive_int(patience_trials, "patience_trials")
        self._min_delta = float(min_delta)
        if not math.isfinite(self._min_delta) or self._min_delta < 0:
            raise ValueError("min_delta must be a finite, non-negative number.")

        self.method_id = method_id
        self.objective_metric = objective_metric
        self.objective_direction = objective_direction
        contract_view = _CandidateContractView(candidate_contract)
        self._candidate_normalizer = candidate_normalizer or (
            lambda candidate: normalize_candidate(candidate, contract_view, method_id)
        )
        self._logical_trial_id_factory = logical_trial_id_factory or (
            lambda: f"trial-{uuid.uuid4().hex[:12]}"
        )

        self._run_status = "running"
        self._stop_code: Optional[str] = None
        self._trials: Dict[str, _LogicalTrial] = {}
        self._candidate_ids = set()
        self._rejections = []
        self._controller_events = []
        self._applied_admissions: Dict[str, tuple[str, int, Tuple[str, ...]]] = {}
        self._terminal_logical_trials = 0
        self._successful_logical_trials = 0
        self._successful_objective_observations = 0
        self._final_logical_failures = 0
        self._total_attempts = 0
        self._total_observations = 0
        self._retry_count = 0
        self._best_metric: Optional[float] = None
        self._best_candidate_id: Optional[str] = None
        self._best_logical_trial_id: Optional[str] = None
        self._no_improvement_count = 0

    @property
    def run_status(self) -> str:
        return self._run_status

    @property
    def stop_code(self) -> Optional[str]:
        return self._stop_code

    @property
    def submissions_closed(self) -> bool:
        return self._run_status != "running" or self._stop_code is not None

    @property
    def accepted_logical_trials(self) -> int:
        return len(self._trials)

    @property
    def terminal_logical_trials(self) -> int:
        return self._terminal_logical_trials

    @property
    def active_logical_trials(self) -> int:
        return self.accepted_logical_trials - self._terminal_logical_trials

    @property
    def remaining_trials(self) -> Optional[int]:
        if self._max_trials is None:
            return None
        return max(0, self._max_trials - self.accepted_logical_trials)

    @property
    def next_proposal_width(self) -> int:
        """Return the exact maximum width for the next synchronous proposal."""

        if self.submissions_closed or self.active_logical_trials:
            return 0
        if self.remaining_trials is None:
            return self._proposal_width
        return min(self._proposal_width, self.remaining_trials)

    @property
    def accepted_candidate_ids(self) -> Tuple[str, ...]:
        return tuple(trial.candidate["candidate_id"] for trial in self._trials.values())

    @property
    def rejections(self) -> Tuple[ProposalRejection, ...]:
        return tuple(self._rejections)

    @property
    def controller_events(self) -> Tuple[ControllerEvent, ...]:
        """Return ordered transitions without assigning canonical ledger ids."""

        return tuple(
            ControllerEvent(event.controller_sequence, event.event, copy.deepcopy(event.payload))
            for event in self._controller_events
        )

    def controller_events_since(self, controller_sequence: int = 0) -> Tuple[ControllerEvent, ...]:
        """Return transitions after a caller-owned projection checkpoint."""

        if isinstance(controller_sequence, bool) or not isinstance(controller_sequence, int) or controller_sequence < 0:
            raise ValueError("controller_sequence must be a non-negative integer.")
        return tuple(
            ControllerEvent(event.controller_sequence, event.event, copy.deepcopy(event.payload))
            for event in self._controller_events
            if event.controller_sequence > controller_sequence
        )

    @property
    def logical_trials(self) -> Tuple[LogicalTrialSnapshot, ...]:
        return tuple(self._snapshot_trial(trial) for trial in self._trials.values())

    def restore_canonical_state(
        self, state: RunControllerRestoreState
    ) -> "RunController":
        """Rebuild this disposable cache from one canonical persisted snapshot.

        Restoration is valid only on a fresh controller built from the exact
        :class:`RunControlManifest` inputs.  All validation and derived-summary
        calculation happen before mutation, so a rejected snapshot leaves the
        controller pristine.  Canonical ledger history remains the evidence
        authority; restoration intentionally emits no controller events.
        """

        if not isinstance(state, RunControllerRestoreState):
            raise TypeError("state must be a RunControllerRestoreState.")
        if (
            self._trials
            or self._candidate_ids
            or self._rejections
            or self._controller_events
            or self._applied_admissions
        ):
            raise RunControllerStateError(
                "Canonical state can be restored only into a fresh controller."
            )
        if self._run_status != "running" or self._stop_code is not None:
            raise RunControllerStateError(
                "Canonical state can be restored only into a fresh controller."
            )
        if self._max_trials is not None and len(state.logical_trials) > self._max_trials:
            raise RunControllerStateError(
                "Canonical logical trials exceed the controller trial budget."
            )

        staged_trials: Dict[str, _LogicalTrial] = {}
        staged_candidates: Dict[str, JsonDict] = {}
        terminal_sequences = set()
        for restored in state.logical_trials:
            if restored.logical_trial_id in staged_trials:
                raise RunControllerStateError(
                    f"Duplicate canonical logical trial id {restored.logical_trial_id!r}."
                )
            candidate = self._validate_restored_candidate(restored.candidate)
            candidate_id = candidate["candidate_id"]
            prior_candidate = staged_candidates.get(candidate_id)
            if prior_candidate is not None and prior_candidate != candidate:
                raise RunControllerStateError(
                    f"Canonical candidate id {candidate_id!r} has conflicting semantics."
                )
            staged_candidates[candidate_id] = candidate
            if restored.terminal_sequence is not None:
                if restored.terminal_sequence in terminal_sequences:
                    raise RunControllerStateError(
                        "Canonical terminal logical-trial sequences must be unique."
                    )
                terminal_sequences.add(restored.terminal_sequence)
            staged_trials[restored.logical_trial_id] = _LogicalTrial(
                logical_trial_id=restored.logical_trial_id,
                candidate=candidate,
                state=restored.state,
                outcome=restored.outcome,
                code=restored.code,
                attempt_count=restored.attempt_count,
                observation_count=restored.observation_count,
                metric_values=copy.deepcopy(dict(restored.metric_values)),
                completion_metadata=copy.deepcopy(dict(restored.completion_metadata)),
            )

        terminal = sorted(
            (item for item in state.logical_trials if item.state == "terminal"),
            key=lambda item: int(item.terminal_sequence or 0),
        )
        terminal_count = len(terminal)
        successful_count = 0
        successful_objective_count = 0
        failure_count = 0
        best_metric: Optional[float] = None
        best_candidate_id: Optional[str] = None
        best_logical_trial_id: Optional[str] = None
        no_improvement_count = 0
        for restored in terminal:
            improved = False
            if restored.outcome == "success":
                successful_count += 1
                metric = _finite_metric(restored.metric_values.get(self.objective_metric))
                if metric is not None:
                    successful_objective_count += 1
                    if _metric_is_better(
                        metric,
                        current=best_metric,
                        direction=self.objective_direction,
                        min_delta=self._min_delta,
                    ):
                        best_metric = metric
                        best_candidate_id = staged_trials[
                            restored.logical_trial_id
                        ].candidate["candidate_id"]
                        best_logical_trial_id = restored.logical_trial_id
                        improved = True
            if restored.outcome in FAILURE_OUTCOMES:
                failure_count += 1
            no_improvement_count = 0 if improved else no_improvement_count + 1

        # The controller's aggregate counters describe resolved logical trials.
        # Active attempts remain visible on their trial snapshot, then their
        # full counts are incorporated exactly once by record_completion.
        total_attempts = sum(item.attempt_count for item in terminal)
        total_observations = sum(item.observation_count for item in terminal)
        retry_count = sum(
            max(0, item.attempt_count - 1) for item in terminal
        )

        # Single mutation point after the complete snapshot has been validated.
        self._trials = staged_trials
        self._candidate_ids = set(staged_candidates)
        self._run_status = state.run_status
        self._stop_code = (
            None
            if state.submission_state == "accepting"
            else state.terminal_code or state.submission_stop_code
        )
        self._terminal_logical_trials = terminal_count
        self._successful_logical_trials = successful_count
        self._successful_objective_observations = successful_objective_count
        self._final_logical_failures = failure_count
        self._total_attempts = total_attempts
        self._total_observations = total_observations
        self._retry_count = retry_count
        self._best_metric = best_metric
        self._best_candidate_id = best_candidate_id
        self._best_logical_trial_id = best_logical_trial_id
        self._no_improvement_count = no_improvement_count
        return self

    def preflight_proposal(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        admission_id: str,
        expected_run_revision: int,
    ) -> PreparedProposal:
        """Normalize and allocate deterministic ids without mutating controller state."""

        self._require_proposal_open()
        if not isinstance(admission_id, str) or not admission_id.strip():
            raise ValueError("admission_id must be a non-empty string.")
        expected_run_revision = _nonnegative_int(
            expected_run_revision, "expected_run_revision"
        )
        raw_candidates = self._materialize_proposal_pure(candidates)
        requested_width = self.next_proposal_width
        if len(raw_candidates) > requested_width:
            raise MethodProtocolError(
                "batch_overproduced",
                f"Batch method returned {len(raw_candidates)} candidates after being asked "
                f"for at most {requested_width}.",
                details={
                    "requested_width": requested_width,
                    "returned_count": len(raw_candidates),
                },
            )
        normalized_candidates: list[JsonDict] = []
        proposal_ids = set()
        for index, raw_candidate in enumerate(raw_candidates):
            try:
                normalized = self._normalize_and_validate_candidate(raw_candidate)
            except Exception as error:
                raise MethodProtocolError(
                    "candidate_malformed",
                    f"Candidate at proposal index {index} is malformed: {error}",
                    details={"candidate_index": index},
                ) from error
            candidate_id = normalized["candidate_id"]
            if candidate_id in proposal_ids or candidate_id in self._candidate_ids:
                raise MethodProtocolError(
                    "duplicate_candidate_id",
                    f"Candidate id {candidate_id!r} is already present in this run or proposal.",
                    details={"candidate_index": index, "candidate_id": candidate_id},
                )
            proposal_ids.add(candidate_id)
            normalized_candidates.append(normalized)
        logical_trial_ids = tuple(
            "trial-"
            + hashlib.sha256(
                f"{admission_id}/{index}".encode("utf-8")
            ).hexdigest()[:16]
            for index in range(len(normalized_candidates))
        )
        if len(set(logical_trial_ids)) != len(logical_trial_ids) or any(
            item in self._trials for item in logical_trial_ids
        ):
            raise RunControllerStateError(
                "Deterministic logical trial id collides with controller state."
            )
        return PreparedProposal.build(
            admission_id=admission_id,
            expected_run_revision=expected_run_revision,
            requested_width=requested_width,
            candidates=normalized_candidates,
            logical_trial_ids=logical_trial_ids,
        )

    def apply_admission(self, prepared: PreparedProposal, receipt: Any) -> Tuple[AcceptedLogicalTrial, ...]:
        """Apply one already committed Realm admission exactly once as a cache update."""

        from .realm.run_records import RunAdmissionReceipt

        if not isinstance(prepared, PreparedProposal):
            raise TypeError("prepared must be a PreparedProposal.")
        if not isinstance(receipt, RunAdmissionReceipt):
            raise TypeError("receipt must be a RunAdmissionReceipt.")
        if not prepared.candidates:
            raise RunControllerStateError("An empty proposal has no admission to apply.")
        if receipt.revision.operation_kind != "run.admit":
            raise RunControllerStateError("Receipt is not a run admission.")
        if (
            receipt.revision.revision != prepared.expected_run_revision + 1
            or receipt.run.current_revision != receipt.revision.revision
        ):
            raise RunControllerStateError("Admission receipt revision is stale or mismatched.")
        receipt_candidate_ids = tuple(item.candidate_id for item in receipt.candidates)
        receipt_trial_ids = tuple(
            item.admission.logical_trial_id for item in receipt.logical_trials
        )
        receipt_trial_candidates = tuple(
            item.admission.candidate_id for item in receipt.logical_trials
        )
        if (
            receipt_candidate_ids != prepared.candidate_ids
            or receipt_trial_ids != prepared.logical_trial_ids
            or receipt_trial_candidates != prepared.candidate_ids
        ):
            raise RunControllerStateError(
                "Admission receipt identities or ordering differ from preflight."
            )
        for candidate, admitted in zip(prepared.candidates, receipt.candidates):
            admission = admitted.admission
            if (
                admission.envelope.candidate_format != candidate["format"]
                or _thaw_controller_value(admission.envelope.spec) != candidate["spec"]
                or _thaw_controller_value(admission.lineage) != candidate["lineage"]
                or _thaw_controller_value(admission.generator) != candidate["generator"]
            ):
                raise RunControllerStateError(
                    "Admission receipt candidate semantics differ from preflight."
                )
        applied = self._applied_admissions.get(prepared.admission_id)
        receipt_anchor = (
            prepared.digest,
            receipt.revision.revision,
            prepared.logical_trial_ids,
        )
        if applied is not None:
            if applied != receipt_anchor:
                raise RunControllerStateError(
                    "Admission id was already applied with different facts."
                )
            return tuple(
                AcceptedLogicalTrial(
                    logical_trial_id=logical_trial_id,
                    candidate=copy.deepcopy(self._trials[logical_trial_id].candidate),
                )
                for logical_trial_id in prepared.logical_trial_ids
            )
        self._require_proposal_open()
        if any(item in self._candidate_ids for item in prepared.candidate_ids) or any(
            item in self._trials for item in prepared.logical_trial_ids
        ):
            raise RunControllerStateError(
                "Controller state changed after proposal preflight."
            )
        staged = tuple(
            _LogicalTrial(logical_trial_id=logical_trial_id, candidate=copy.deepcopy(candidate))
            for logical_trial_id, candidate in zip(
                prepared.logical_trial_ids, prepared.candidates
            )
        )
        for trial in staged:
            self._trials[trial.logical_trial_id] = trial
            self._candidate_ids.add(trial.candidate["candidate_id"])
        self._applied_admissions[prepared.admission_id] = receipt_anchor
        self._emit_event(
            "proposal.accepted",
            {
                "admission_id": prepared.admission_id,
                "ledger_run_revision": receipt.revision.revision,
                "ledger_txn_id": receipt.revision.txn_id,
                "requested_width": prepared.requested_width,
                "accepted_count": len(staged),
                "logical_trials": [
                    {
                        "logical_trial_id": trial.logical_trial_id,
                        "candidate_id": trial.candidate["candidate_id"],
                    }
                    for trial in staged
                ],
                "accepted_logical_trials": self.accepted_logical_trials,
                "remaining_trials": self.remaining_trials,
            },
        )
        if self.remaining_trials == 0:
            self._close_submissions("max_trials")
        return tuple(
            AcceptedLogicalTrial(
                logical_trial_id=trial.logical_trial_id,
                candidate=copy.deepcopy(trial.candidate),
            )
            for trial in staged
        )

    def accept_proposal(self, candidates: Sequence[Mapping[str, Any]]) -> Tuple[AcceptedLogicalTrial, ...]:
        """Normalize and atomically reserve one logical slot per candidate.

        Empty output is a normal method completion.  Any structural error,
        duplicate id, or overproduction rejects the entire non-empty proposal,
        records a protocol rejection, and fails the batch run with
        ``stop_code=protocol_error``.  Candidate ids and budget remain exactly
        as they were before that proposal.
        """

        self._require_proposal_open()
        raw_candidates = self._materialize_proposal(candidates)
        requested_width = self.next_proposal_width
        if len(raw_candidates) > requested_width:
            self._reject_proposal(
                "batch_overproduced",
                f"Batch method returned {len(raw_candidates)} candidates after being asked for at most {requested_width}.",
                details={"requested_width": requested_width, "returned_count": len(raw_candidates)},
            )
        if not raw_candidates:
            self.finish_method()
            return ()

        normalized_candidates = []
        proposal_ids = set()
        for index, raw_candidate in enumerate(raw_candidates):
            try:
                normalized = self._normalize_and_validate_candidate(raw_candidate)
            except Exception as exc:
                self._reject_proposal(
                    "candidate_malformed",
                    f"Candidate at proposal index {index} is malformed: {exc}",
                    details={"candidate_index": index},
                )
            candidate_id = normalized["candidate_id"]
            if candidate_id in proposal_ids or candidate_id in self._candidate_ids:
                self._reject_proposal(
                    "duplicate_candidate_id",
                    f"Candidate id {candidate_id!r} is already present in this run or proposal.",
                    details={"candidate_index": index, "candidate_id": candidate_id},
                )
            proposal_ids.add(candidate_id)
            normalized_candidates.append(normalized)

        staged_trials = []
        staged_trial_ids = set()
        for candidate in normalized_candidates:
            logical_trial_id = self._logical_trial_id_factory()
            if not isinstance(logical_trial_id, str) or not logical_trial_id:
                raise RunControllerStateError("logical_trial_id_factory must return a non-empty string.")
            if logical_trial_id in self._trials or logical_trial_id in staged_trial_ids:
                raise RunControllerStateError(f"logical_trial_id_factory returned duplicate id {logical_trial_id!r}.")
            staged_trial_ids.add(logical_trial_id)
            staged_trials.append(_LogicalTrial(logical_trial_id=logical_trial_id, candidate=candidate))

        # This is the only proposal-acceptance mutation point.
        for trial in staged_trials:
            self._trials[trial.logical_trial_id] = trial
            self._candidate_ids.add(trial.candidate["candidate_id"])

        self._emit_event(
            "proposal.accepted",
            {
                "requested_width": requested_width,
                "accepted_count": len(staged_trials),
                "logical_trials": [
                    {
                        "logical_trial_id": trial.logical_trial_id,
                        "candidate_id": trial.candidate["candidate_id"],
                    }
                    for trial in staged_trials
                ],
                "accepted_logical_trials": self.accepted_logical_trials,
                "remaining_trials": self.remaining_trials,
            },
        )
        if self.remaining_trials == 0:
            self._close_submissions("max_trials")

        return tuple(
            AcceptedLogicalTrial(
                logical_trial_id=trial.logical_trial_id,
                candidate=copy.deepcopy(trial.candidate),
            )
            for trial in staged_trials
        )

    def record_completion(
        self,
        logical_trial_id: str,
        observation: Any,
        *,
        attempt_count: int = 1,
        observation_count: int = 1,
        completion_metadata: Optional[Mapping[str, Any]] = None,
    ) -> LogicalTrialSnapshot:
        """Terminalize one accepted logical trial without reserving another slot."""

        if self._run_status != "running":
            raise RunControllerStateError("Cannot record a completion after the run is terminal.")
        trial = self._trials.get(logical_trial_id)
        if trial is None:
            raise RunControllerStateError(f"Unknown logical trial id: {logical_trial_id!r}.")
        if trial.state == "terminal":
            raise RunControllerStateError(f"Logical trial {logical_trial_id!r} is already terminal.")
        attempt_count = _nonnegative_int(attempt_count, "attempt_count")
        observation_count = _nonnegative_int(observation_count, "observation_count")

        raw_outcome = _observation_field(observation, "status", None)
        metadata = copy.deepcopy(dict(completion_metadata or {}))
        if raw_outcome in STANDARD_OUTCOMES:
            outcome = str(raw_outcome)
            code = None if outcome == "success" else f"trial_{outcome}"
        else:
            outcome = "failed"
            code = "unsupported_observation_status"
            metadata["raw_status"] = raw_outcome

        candidate_id = _observation_field(observation, "candidate_id", None)
        if candidate_id is not None and candidate_id != trial.candidate["candidate_id"]:
            raise RunControllerStateError(
                f"Observation candidate_id {candidate_id!r} does not match accepted candidate "
                f"{trial.candidate['candidate_id']!r}."
            )
        metric_values = _observation_field(observation, "metric_values", {})
        if not isinstance(metric_values, Mapping):
            raise RunControllerStateError("Observation metric_values must be a mapping.")

        trial.state = "terminal"
        trial.outcome = outcome
        trial.code = code
        trial.attempt_count = attempt_count
        trial.observation_count = observation_count
        trial.metric_values = copy.deepcopy(dict(metric_values))
        trial.completion_metadata = metadata
        self._terminal_logical_trials += 1
        self._total_attempts += attempt_count
        self._total_observations += observation_count
        self._retry_count += max(0, attempt_count - 1)

        improved = False
        if outcome == "success":
            self._successful_logical_trials += 1
            metric = _finite_metric(metric_values.get(self.objective_metric))
            if metric is not None:
                self._successful_objective_observations += 1
                if self._is_better(metric):
                    self._best_metric = metric
                    self._best_candidate_id = trial.candidate["candidate_id"]
                    self._best_logical_trial_id = logical_trial_id
                    improved = True
        if outcome in FAILURE_OUTCOMES:
            self._final_logical_failures += 1

        self._no_improvement_count = 0 if improved else self._no_improvement_count + 1
        self._emit_event(
            "logical_trial.terminal",
            {
                "logical_trial_id": logical_trial_id,
                "candidate_id": trial.candidate["candidate_id"],
                "outcome": outcome,
                "code": code,
                "attempt_count": attempt_count,
                "observation_count": observation_count,
                "terminal_logical_trials": self._terminal_logical_trials,
                "final_logical_failures": self._final_logical_failures,
                "best_metric": self._best_metric,
                "best_candidate_id": self._best_candidate_id,
            },
        )
        if self._max_failures is not None and self._final_logical_failures >= self._max_failures:
            self._close_submissions("max_failures")
        elif (
            self._patience_trials is not None
            and self._no_improvement_count >= self._patience_trials
            and self.active_logical_trials == 0
            and self._stop_code is None
        ):
            self._close_submissions("converged")

        self._maybe_finalize()
        return self._snapshot_trial(trial)

    def finish_method(self) -> None:
        """Record normal method exhaustion/return and drain accepted work."""

        self._require_running()
        self._close_submissions("method_completed")
        self._maybe_finalize()

    def stop_for_wall_clock(self) -> None:
        """Close submissions for wall-clock expiry and drain accepted work."""

        self._require_running()
        self._close_submissions("wall_clock_budget")
        self._maybe_finalize()

    def cancel(self, stop_code: str = "user_cancelled") -> None:
        """Request an explicit cancellation and await terminal attempt records."""

        if stop_code not in _CANCEL_STOP_CODES:
            raise ValueError(f"Unsupported cancellation stop code: {stop_code!r}.")
        self._require_running()
        self._close_submissions(stop_code)
        self._maybe_finalize()

    def fail(self, stop_code: str) -> None:
        """Record an unrecoverable run failure and drain any accepted work."""

        if stop_code not in _FAILURE_STOP_CODES - {"protocol_error", "max_failures"}:
            raise ValueError(f"Unsupported fatal stop code: {stop_code!r}.")
        self._require_running()
        self._close_submissions(stop_code)
        self._maybe_finalize()

    def summary(self) -> JsonDict:
        """Return a JSON-compatible live/final summary projection."""

        return {
            "run_status": self._run_status,
            "stop_code": self._stop_code,
            "submissions_closed": self.submissions_closed,
            "proposal_width": self._proposal_width,
            "max_trials": self._max_trials,
            "remaining_trials": self.remaining_trials,
            "accepted_logical_trials": self.accepted_logical_trials,
            "terminal_logical_trials": self._terminal_logical_trials,
            "active_logical_trials": self.active_logical_trials,
            "completed_trials": self._terminal_logical_trials,
            "successful_logical_trials": self._successful_logical_trials,
            "successful_objective_observations": self._successful_objective_observations,
            "final_logical_failures": self._final_logical_failures,
            "failure_count": self._final_logical_failures,
            "attempt_count": self._total_attempts,
            "observation_count": self._total_observations,
            "retry_count": self._retry_count,
            "candidate_count": len(self._candidate_ids),
            "rejected_proposals": len(self._rejections),
            "best_metric": self._best_metric,
            "best_candidate_id": self._best_candidate_id,
            "best_logical_trial_id": self._best_logical_trial_id,
            "no_improvement_count": self._no_improvement_count,
        }

    def _materialize_proposal(self, candidates: Sequence[Mapping[str, Any]]) -> Sequence[Any]:
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            self._reject_proposal(
                "candidate_malformed",
                "Batch proposal must be a sequence of candidate mappings.",
                details={},
            )
        return tuple(candidates)

    @staticmethod
    def _materialize_proposal_pure(
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[Any]:
        """Validate the proposal container without recording a legacy rejection."""

        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise MethodProtocolError(
                "candidate_malformed",
                "Batch proposal must be a sequence of candidate mappings.",
                details={},
            )
        return tuple(candidates)

    def _normalize_and_validate_candidate(self, candidate: Any) -> JsonDict:
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be a mapping")
        normalized = self._candidate_normalizer(copy.deepcopy(dict(candidate)))
        if not isinstance(normalized, Mapping):
            raise TypeError("candidate normalizer must return a mapping")
        result = copy.deepcopy(dict(normalized))
        candidate_id = result.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate_id must be a non-empty string")
        candidate_format = result.get("format")
        if not isinstance(candidate_format, str) or not candidate_format:
            raise ValueError("format must be a non-empty string")
        for field in ("spec", "lineage", "generator", "validation", "materialization"):
            if not isinstance(result.get(field), Mapping):
                raise ValueError(f"{field} must be a mapping")
            result[field] = copy.deepcopy(dict(result[field]))
        return result

    def _validate_restored_candidate(self, candidate: Mapping[str, Any]) -> JsonDict:
        """Verify that canonical candidate facts still match the resolved normalizer."""

        raw = copy.deepcopy(dict(candidate))
        normalized = self._normalize_and_validate_candidate(raw)
        if normalized != raw:
            raise RunControllerStateError(
                "Canonical candidate differs from the resolved normalizer output."
            )
        try:
            json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RunControllerStateError(
                "Canonical candidate is not JSON-compatible."
            ) from error
        return raw

    def _is_better(self, candidate_metric: float) -> bool:
        return _metric_is_better(
            candidate_metric,
            current=self._best_metric,
            direction=self.objective_direction,
            min_delta=self._min_delta,
        )

    def _snapshot_trial(self, trial: _LogicalTrial) -> LogicalTrialSnapshot:
        return LogicalTrialSnapshot(
            logical_trial_id=trial.logical_trial_id,
            candidate_id=trial.candidate["candidate_id"],
            candidate=copy.deepcopy(trial.candidate),
            state=trial.state,
            outcome=trial.outcome,
            code=trial.code,
            attempt_count=trial.attempt_count,
            observation_count=trial.observation_count,
            metric_values=copy.deepcopy(trial.metric_values or {}),
            completion_metadata=copy.deepcopy(trial.completion_metadata or {}),
        )

    def _require_running(self) -> None:
        if self._run_status != "running":
            raise RunControllerStateError(
                f"Run is already terminal with status {self._run_status!r} and stop_code {self._stop_code!r}."
            )

    def _require_proposal_open(self) -> None:
        self._require_running()
        if self._stop_code is not None:
            raise RunControllerStateError(f"Submissions are closed with stop_code {self._stop_code!r}.")
        if self.active_logical_trials:
            raise RunControllerStateError("Batch proposal cannot start while logical trials are still active.")
        if self.next_proposal_width <= 0:
            raise RunControllerStateError("No proposal budget remains.")

    def _reject_proposal(self, code: str, message: str, *, details: Mapping[str, Any]) -> None:
        rejection = ProposalRejection(code=code, message=message, details=copy.deepcopy(dict(details)))
        self._rejections.append(rejection)
        self._emit_event(
            "proposal.rejected",
            {
                "code": code,
                "message": message,
                "details": copy.deepcopy(dict(details)),
                "accepted_logical_trials": self.accepted_logical_trials,
                "remaining_trials": self.remaining_trials,
            },
        )
        self._close_submissions("protocol_error")
        self._maybe_finalize()
        raise MethodProtocolError(code, message, details=details)

    def _close_submissions(self, stop_code: str) -> None:
        if stop_code not in _STOP_PRIORITY:
            raise ValueError(f"Unsupported stop code: {stop_code!r}.")
        current_priority = _STOP_PRIORITY.get(self._stop_code or "", -1)
        if _STOP_PRIORITY[stop_code] > current_priority:
            previous_stop_code = self._stop_code
            self._stop_code = stop_code
            self._emit_event(
                "submissions.closed",
                {
                    "stop_code": stop_code,
                    "previous_stop_code": previous_stop_code,
                    "active_logical_trials": self.active_logical_trials,
                },
            )

    def _maybe_finalize(self) -> None:
        if self._run_status != "running" or self._stop_code is None or self.active_logical_trials:
            return
        if self._stop_code in _CANCEL_STOP_CODES:
            self._run_status = "cancelled"
        elif self._stop_code in _FAILURE_STOP_CODES:
            self._run_status = "failed"
        else:
            if self._stop_code not in _NORMAL_STOP_CODES:
                raise RunControllerStateError(f"Cannot finalize unknown stop code {self._stop_code!r}.")
            if self._successful_objective_observations:
                self._run_status = "succeeded"
            else:
                self._run_status = "failed"
                self._stop_code = "no_successful_observation"
        self._emit_event(
            "run.terminal",
            {
                "run_status": self._run_status,
                "stop_code": self._stop_code,
                "accepted_logical_trials": self.accepted_logical_trials,
                "terminal_logical_trials": self._terminal_logical_trials,
                "attempt_count": self._total_attempts,
                "observation_count": self._total_observations,
                "final_logical_failures": self._final_logical_failures,
                "best_metric": self._best_metric,
                "best_candidate_id": self._best_candidate_id,
            },
        )

    def _emit_event(self, event: str, payload: Mapping[str, Any]) -> None:
        self._controller_events.append(
            ControllerEvent(
                controller_sequence=len(self._controller_events) + 1,
                event=event,
                payload=copy.deepcopy(dict(payload)),
            )
        )


def _observation_field(observation: Any, field: str, default: Any) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(field, default)
    return getattr(observation, field, default)


def _thaw_controller_value(value: Any) -> Any:
    """Return an ordinary JSON-shaped value from immutable Realm records."""

    if isinstance(value, Mapping):
        return {key: _thaw_controller_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_controller_value(item) for item in value]
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer.")
    return value


def _optional_positive_int(value: Any, field: str) -> Optional[int]:
    if value in (None, 0):
        return None
    return _positive_int(value, field)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer.")
    return value


def _finite_metric(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        metric = float(value)
    except (TypeError, ValueError):
        return None
    return metric if math.isfinite(metric) else None


def _metric_is_better(
    candidate: float,
    *,
    current: Optional[float],
    direction: str,
    min_delta: float,
) -> bool:
    if current is None:
        return True
    if direction == "maximize":
        return candidate > current + min_delta
    return candidate < current - min_delta
