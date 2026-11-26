"""Narrow, non-production orchestration harness for canonical parameter runs.

This module is an integration seam, not the production ``StudyRunner``.  It
starts from an already-created or hydrated :class:`RetainedRunAuthority` and
serially exercises canonical admission and attempt recovery through one
long-lived :class:`RunAttemptScheduler`.  The scheduler's provider owns every
workspace and backend detail; the harness owns only proposal, deterministic
attempt identity, observation delivery, and termination sequencing.

Proposal and observation callbacks receive stable request/delivery ids.  They
must be idempotent: the Realm currently has no canonical method-call delivery
checkpoint, so a hydrated harness may replay a terminal completion after a
process crash.  That deliberate limitation is one reason this module is not a
drop-in runner.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence, Tuple

from .attempts import AttemptEnvelope
from .realm.run_snapshot import RunLedgerSnapshot
from .run_attempt_scheduler import RunAttemptAdvanceResult, RunAttemptScheduler
from .run_authority import RetainedRunAuthority


_PUBLIC_OUTCOMES = frozenset(
    {"success", "invalid", "failed", "timeout", "partial", "cancelled"}
)


class ParameterRunHarnessLimitReached(RuntimeError):
    """The caller-provided proposal limit ended an otherwise live harness."""


@dataclass(frozen=True)
class ParameterRunState:
    """Immutable callback view of one canonical run head."""

    run_id: str
    run_revision: int
    owner_revision: int
    run_status: str
    submission_state: str
    stop_code: str | None
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string.")
        for name in ("run_revision", "owner_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
        if self.run_status not in {"running", "succeeded", "failed", "cancelled"}:
            raise ValueError("run_status is unsupported.")
        if self.submission_state not in {"accepting", "draining", "terminal"}:
            raise ValueError("submission_state is unsupported.")
        if self.stop_code is not None and (
            not isinstance(self.stop_code, str) or not self.stop_code
        ):
            raise ValueError("stop_code must be a non-empty string or None.")
        object.__setattr__(self, "summary", _freeze_mapping(self.summary))


@dataclass(frozen=True)
class ParameterProposalRequest:
    """One replay-stable request to a parameter proposal callback."""

    request_id: str
    admission_id: str
    max_candidates: int
    state: ParameterRunState

    def __post_init__(self) -> None:
        _required_text(self.request_id, "proposal request_id")
        _required_text(self.admission_id, "proposal admission_id")
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or self.max_candidates <= 0
        ):
            raise ValueError("max_candidates must be a positive integer.")
        if not isinstance(self.state, ParameterRunState):
            raise TypeError("state must be a ParameterRunState.")


@dataclass(frozen=True)
class ParameterMethodObservation:
    """Minimal evaluator result safe for the harness's method callback.

    Output declarations, artifact bindings, execution metadata, runtime paths,
    backend identity, and raw event summaries are deliberately absent.  Error
    data is reduced to the small public fields needed to react to a failure.
    """

    status: str
    metric_values: Mapping[str, Any]
    constraint_results: Mapping[str, Any]
    error: Mapping[str, str]

    def __post_init__(self) -> None:
        _required_text(self.status, "observation status")
        if self.status not in _PUBLIC_OUTCOMES:
            raise ValueError("observation status is not a public outcome.")
        object.__setattr__(
            self, "metric_values", _freeze_mapping(self.metric_values)
        )
        object.__setattr__(
            self,
            "constraint_results",
            _freeze_mapping(self.constraint_results),
        )
        error = dict(self.error)
        if any(
            key not in {"phase", "type", "code", "message"}
            or not isinstance(value, str)
            for key, value in error.items()
        ):
            raise ValueError(
                "method observation error may contain only public string fields."
            )
        object.__setattr__(self, "error", MappingProxyType(error))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "metric_values": _thaw(self.metric_values),
            "constraint_results": _thaw(self.constraint_results),
            "error": dict(self.error),
        }

    @classmethod
    def from_envelope(
        cls,
        envelope: AttemptEnvelope,
        *,
        effective_status: str | None = None,
    ) -> "ParameterMethodObservation":
        """Filter one operator envelope into the method-facing DTO.

        ``effective_status`` is the canonical adopted outcome. It can differ
        from the environment envelope when platform finalization (for example,
        required artifact capture) downgrades an otherwise successful result.
        """

        return _method_observation(envelope, effective_status=effective_status)


@dataclass(frozen=True)
class ParameterObservationDelivery:
    """Method-facing terminal logical completion without operator artifacts."""

    delivery_id: str
    logical_trial_id: str
    candidate_id: str
    outcome: str
    code: str | None
    terminal_attempt_id: str | None
    attempt_count: int
    observation: ParameterMethodObservation | None

    def __post_init__(self) -> None:
        for name in ("delivery_id", "logical_trial_id", "candidate_id", "outcome"):
            _required_text(getattr(self, name), name)
        if self.outcome not in _PUBLIC_OUTCOMES:
            raise ValueError("completion outcome is not a public outcome.")
        if self.code is not None:
            _required_text(self.code, "completion code")
        if self.terminal_attempt_id is not None:
            _required_text(self.terminal_attempt_id, "terminal attempt_id")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("attempt_count must be a nonnegative integer.")
        if self.observation is not None and not isinstance(
            self.observation, ParameterMethodObservation
        ):
            raise TypeError(
                "observation must be a ParameterMethodObservation or None."
            )
        if self.observation is not None and self.observation.status != self.outcome:
            raise ValueError(
                "method observation status differs from completion outcome."
            )


@dataclass(frozen=True)
class ParameterRunTerminationRequest:
    """Typed handoff to canonical close/finalization policy."""

    reason: str
    state: ParameterRunState

    def __post_init__(self) -> None:
        if self.reason not in {"method_completed", "canonical_drained"}:
            raise ValueError("termination reason is unsupported.")
        if not isinstance(self.state, ParameterRunState):
            raise TypeError("state must be a ParameterRunState.")


@dataclass(frozen=True)
class ParameterRunHarnessResult:
    """Result after terminal state or a termination callback handoff.

    A non-``None`` ``termination_request`` proves only that the typed callback
    returned.  If that callback is a recorder/no-op, ``state`` may still be
    accepting or draining; callers must inspect ``canonical_terminal`` rather
    than treating every returned result as a finalized run.  ``attempt_advances``
    counts every scheduler call, including cleanup-only calls.  Pending cleanup
    ids are operational debt only; they never alter evaluator or run outcome.
    """

    state: ParameterRunState
    termination_request: ParameterRunTerminationRequest | None
    proposals: int
    attempt_advances: int
    pending_cleanup_attempt_ids: Tuple[str, ...]
    delivered_logical_trials: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, ParameterRunState):
            raise TypeError("state must be a ParameterRunState.")
        if self.termination_request is not None and not isinstance(
            self.termination_request, ParameterRunTerminationRequest
        ):
            raise TypeError(
                "termination_request must be a ParameterRunTerminationRequest "
                "or None."
            )
        for name in ("proposals", "attempt_advances"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
        pending = tuple(self.pending_cleanup_attempt_ids)
        delivered = tuple(self.delivered_logical_trials)
        for values, label in (
            (pending, "pending cleanup attempt ids"),
            (delivered, "delivered logical trial ids"),
        ):
            if any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{label} must contain nonempty strings.")
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} must be unique and sorted.")
        object.__setattr__(self, "pending_cleanup_attempt_ids", pending)
        object.__setattr__(self, "delivered_logical_trials", delivered)

    @property
    def canonical_terminal(self) -> bool:
        return self.state.run_status != "running"

    @property
    def termination_handed_off(self) -> bool:
        return self.termination_request is not None


class ParameterRunIdentitySource(Protocol):
    """Pure identity compiler for callback and attempt replay keys."""

    def proposal(
        self, *, run_id: str, expected_run_revision: int
    ) -> tuple[str, str]:
        """Return ``(request_id, admission_id)`` for one proposal."""

    def attempt(
        self,
        *,
        run_id: str,
        logical_trial_id: str,
        attempt_index: int,
    ) -> str:
        """Return the deterministic canonical attempt id."""

    def observation(self, *, run_id: str, logical_trial_id: str) -> str:
        """Return one stable terminal-completion delivery id."""


@dataclass(frozen=True)
class DigestParameterRunIdentitySource:
    """Default path-free deterministic identities for the integration harness."""

    namespace: str = "optpilot.parameter-run-harness.v1"

    def __post_init__(self) -> None:
        _required_text(self.namespace, "identity namespace")

    def proposal(
        self, *, run_id: str, expected_run_revision: int
    ) -> tuple[str, str]:
        key = _identity_digest(
            self.namespace, "proposal", run_id, str(expected_run_revision)
        )
        return f"proposal-{key[:24]}", f"admission-{key[:24]}"

    def attempt(
        self,
        *,
        run_id: str,
        logical_trial_id: str,
        attempt_index: int,
    ) -> str:
        key = _identity_digest(
            self.namespace,
            "attempt",
            run_id,
            logical_trial_id,
            str(attempt_index),
        )
        return f"attempt-{key[:24]}"

    def observation(self, *, run_id: str, logical_trial_id: str) -> str:
        key = _identity_digest(
            self.namespace, "observation", run_id, logical_trial_id
        )
        return f"delivery-{key[:24]}"


ProposalCallback = Callable[
    [ParameterProposalRequest], Sequence[Mapping[str, Any]]
]
ObserveCallback = Callable[[ParameterObservationDelivery], None]
TerminationCallback = Callable[
    [RetainedRunAuthority, ParameterRunTerminationRequest], None
]


@dataclass(frozen=True)
class CanonicalParameterRunTerminator:
    """Idempotent bridge from the harness handoff to canonical lifecycle APIs.

    It supplies no terminal status or code. Method exhaustion contributes only
    the explicit first-close reason; evidence-derived closes are preserved, and
    the Realm ledger derives the final pair after every logical trial drains.
    """

    operation_namespace: str = "parameter-harness/lifecycle/v1"

    def __post_init__(self) -> None:
        _required_text(self.operation_namespace, "termination operation namespace")

    def __call__(
        self,
        authority: RetainedRunAuthority,
        request: ParameterRunTerminationRequest,
    ) -> None:
        if not isinstance(authority, RetainedRunAuthority):
            raise TypeError("authority must be a RetainedRunAuthority.")
        if not isinstance(request, ParameterRunTerminationRequest):
            raise TypeError("request must be a ParameterRunTerminationRequest.")
        if request.state.run_id != authority.run_id:
            raise ValueError("termination request belongs to another run.")

        snapshot = authority.refresh_controller()
        if snapshot.run.state != "running":
            return
        submission = snapshot.control.current_submission
        if request.reason == "method_completed" and submission.state == "accepting":
            authority.close_submissions(
                operation_id=(
                    f"{self.operation_namespace}/{authority.run_id}/"
                    "close/method-completed"
                ),
                stop_code="method_completed",
            )
            snapshot = authority.refresh_controller()
            submission = snapshot.control.current_submission
        elif request.reason == "canonical_drained" and submission.state == "accepting":
            raise RuntimeError(
                "canonical_drained termination requires closed submissions."
            )

        if snapshot.run.state == "running":
            if submission.state != "draining":
                raise RuntimeError(
                    "A live parameter run can terminate only from draining state."
                )
            authority.finish(
                operation_id=f"{self.operation_namespace}/{authority.run_id}/finish"
            )


class ParameterRunHarness:
    """Serially exercise an already-created canonical parameter run.

    Every canonical attempt state is normal input.  Fresh accepted/retrying
    work receives a deterministic id; queued/running work reuses its exact
    retained id; terminal work receives at most one cleanup retry per
    :meth:`run` invocation.  ``observe`` remains at-least-once with a stable
    ``delivery_id``; callback implementations must deduplicate across hydrated
    harness instances.
    """

    def __init__(
        self,
        authority: RetainedRunAuthority,
        *,
        scheduler: RunAttemptScheduler,
        propose: ProposalCallback,
        observe: ObserveCallback,
        terminate: TerminationCallback | None = None,
        identity_source: ParameterRunIdentitySource | None = None,
        attempt_ttl_seconds: float = 300.0,
    ) -> None:
        if not isinstance(authority, RetainedRunAuthority):
            raise TypeError("authority must be a RetainedRunAuthority.")
        if not isinstance(scheduler, RunAttemptScheduler):
            raise TypeError("scheduler must be a RunAttemptScheduler.")
        if scheduler.authority is not authority:
            raise ValueError("scheduler must use the harness's exact authority.")
        for callback, label in (
            (propose, "propose"),
            (observe, "observe"),
        ):
            if not callable(callback):
                raise TypeError(f"{label} must be callable.")
        if terminate is not None and not callable(terminate):
            raise TypeError("terminate must be callable or None.")
        if (
            isinstance(attempt_ttl_seconds, bool)
            or not isinstance(attempt_ttl_seconds, (int, float))
            or attempt_ttl_seconds <= 0
        ):
            raise ValueError("attempt_ttl_seconds must be positive.")
        identities = identity_source or DigestParameterRunIdentitySource()
        for method in ("proposal", "attempt", "observation"):
            if not callable(getattr(identities, method, None)):
                raise TypeError(
                    "identity_source must provide proposal(), attempt(), and "
                    "observation()."
                )
        self.authority = authority
        self.scheduler = scheduler
        self.propose = propose
        self.observe = observe
        self.terminate = (
            CanonicalParameterRunTerminator() if terminate is None else terminate
        )
        self.identity_source = identities
        self.attempt_ttl_seconds = float(attempt_ttl_seconds)
        self._delivered_trials: set[str] = set()

    def run(self, *, max_proposals: int = 1000) -> ParameterRunHarnessResult:
        """Drive serial parameter work until terminal or a termination handoff.

        ``max_proposals`` is an integration safeguard, not a study stopping
        policy.  Reaching it raises instead of changing canonical run state.
        """

        if (
            isinstance(max_proposals, bool)
            or not isinstance(max_proposals, int)
            or max_proposals <= 0
        ):
            raise ValueError("max_proposals must be a positive integer.")
        proposal_count = 0
        attempt_advance_count = 0
        advanced_attempt_ids: set[str] = set()
        pending_cleanup_attempt_ids: set[str] = set()

        while True:
            snapshot = self.authority.refresh_controller()
            cleanup_advances = self._sweep_terminal_attempts(
                snapshot,
                advanced_attempt_ids=advanced_attempt_ids,
                pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
            )
            attempt_advance_count += cleanup_advances
            snapshot = self.authority.refresh_controller()

            selection = self._next_active_attempt(snapshot)
            if selection is not None:
                logical_trial_id, attempt_id = selection
                result = self.scheduler.advance(
                    logical_trial_id=logical_trial_id,
                    attempt_id=attempt_id,
                    attempt_ttl_seconds=self.attempt_ttl_seconds,
                )
                self._record_advance(
                    result,
                    expected_attempt_id=attempt_id,
                    advanced_attempt_ids=advanced_attempt_ids,
                    pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
                )
                attempt_advance_count += 1
                continue

            self._deliver_terminal_completions(snapshot)
            state = _state_from(self.authority, snapshot)

            if snapshot.run.state != "running":
                return self._result(
                    state=state,
                    termination_request=None,
                    proposals=proposal_count,
                    attempt_advances=attempt_advance_count,
                    pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
                )
            if self.authority.controller.submissions_closed:
                request = ParameterRunTerminationRequest(
                    reason="canonical_drained", state=state
                )
                self.terminate(self.authority, request)
                final_snapshot = self.authority.refresh_controller()
                return self._result(
                    state=_state_from(self.authority, final_snapshot),
                    termination_request=request,
                    proposals=proposal_count,
                    attempt_advances=attempt_advance_count,
                    pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
                )
            if proposal_count >= max_proposals:
                raise ParameterRunHarnessLimitReached(
                    "Parameter harness proposal limit reached without canonical stop."
                )

            width = self.authority.controller.next_proposal_width
            if width <= 0:
                raise RuntimeError(
                    "Accepting parameter run has no schedulable proposal width."
                )
            request_id, admission_id = self.identity_source.proposal(
                run_id=self.authority.run_id,
                expected_run_revision=self.authority.run_revision,
            )
            proposal_request = ParameterProposalRequest(
                request_id=request_id,
                admission_id=admission_id,
                max_candidates=width,
                state=state,
            )
            proposed = self.propose(proposal_request)
            proposal_count += 1
            candidates = _proposal_sequence(proposed)
            if not candidates:
                termination = ParameterRunTerminationRequest(
                    reason="method_completed", state=state
                )
                self.terminate(self.authority, termination)
                final_snapshot = self.authority.refresh_controller()
                return self._result(
                    state=_state_from(self.authority, final_snapshot),
                    termination_request=termination,
                    proposals=proposal_count,
                    attempt_advances=attempt_advance_count,
                    pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
                )
            self.authority.admit(candidates, admission_id=admission_id)

    def _sweep_terminal_attempts(
        self,
        snapshot: RunLedgerSnapshot,
        *,
        advanced_attempt_ids: set[str],
        pending_cleanup_attempt_ids: set[str],
    ) -> int:
        advances = 0
        for attempt in snapshot.attempts:
            if (
                attempt.state != "terminal"
                or attempt.attempt_id in advanced_attempt_ids
            ):
                continue
            result = self.scheduler.advance(
                logical_trial_id=attempt.logical_trial_id,
                attempt_id=attempt.attempt_id,
                attempt_ttl_seconds=self.attempt_ttl_seconds,
            )
            if result.action != "cleanup_only":
                raise RuntimeError(
                    "Canonical terminal attempt did not remain cleanup-only."
                )
            self._record_advance(
                result,
                expected_attempt_id=attempt.attempt_id,
                advanced_attempt_ids=advanced_attempt_ids,
                pending_cleanup_attempt_ids=pending_cleanup_attempt_ids,
            )
            advances += 1
        return advances

    def _next_active_attempt(
        self, snapshot: RunLedgerSnapshot
    ) -> tuple[str, str] | None:
        attempts_by_trial = {
            trial.admission.logical_trial_id: []
            for trial in snapshot.logical_trials
        }
        all_attempt_ids = {attempt.attempt_id for attempt in snapshot.attempts}
        for attempt in snapshot.attempts:
            attempts_by_trial[attempt.logical_trial_id].append(attempt)

        max_attempts = snapshot.control.manifest.retry_policy.max_attempts
        for trial in snapshot.logical_trials:
            trial_id = trial.admission.logical_trial_id
            attempts = attempts_by_trial[trial_id]
            live = [attempt for attempt in attempts if attempt.state != "terminal"]
            if len(live) > 1:
                raise RuntimeError(
                    "Logical trial has multiple nonterminal canonical attempts."
                )
            if live and live[0] is not attempts[-1]:
                raise RuntimeError(
                    "Logical trial has a nonterminal attempt before terminal history."
                )

            if trial.state == "terminal":
                if live:
                    raise RuntimeError(
                        "Terminal logical trial still has a nonterminal attempt."
                    )
                continue
            if trial.state in {"accepted", "retrying"}:
                if live:
                    raise RuntimeError(
                        "Schedulable logical trial already has a live attempt."
                    )
                if trial.state == "accepted" and attempts:
                    raise RuntimeError(
                        "Accepted logical trial unexpectedly has attempt history."
                    )
                if trial.state == "retrying" and not attempts:
                    raise RuntimeError(
                        "Retrying logical trial has no terminal attempt history."
                    )
                attempt_index = len(attempts) + 1
                if attempt_index > max_attempts:
                    raise RuntimeError(
                        "Retrying logical trial exhausted its canonical attempt policy."
                    )
                attempt_id = self.identity_source.attempt(
                    run_id=self.authority.run_id,
                    logical_trial_id=trial_id,
                    attempt_index=attempt_index,
                )
                _required_text(attempt_id, "identity source attempt id")
                if attempt_id in all_attempt_ids:
                    raise RuntimeError(
                        "Deterministic attempt id collides with canonical history."
                    )
                return trial_id, attempt_id
            if trial.state in {"queued", "running"}:
                if len(live) != 1:
                    raise RuntimeError(
                        "Queued or running logical trial lacks one live attempt."
                    )
                expected_attempt_state = (
                    "prepared" if trial.state == "queued" else "running"
                )
                if live[0].state != expected_attempt_state:
                    raise RuntimeError(
                        "Logical-trial state differs from its live attempt state."
                    )
                return trial_id, live[0].attempt_id
            raise RuntimeError(
                "Unsupported canonical logical-trial state in parameter harness."
            )
        return None

    @staticmethod
    def _record_advance(
        result: RunAttemptAdvanceResult,
        *,
        expected_attempt_id: str,
        advanced_attempt_ids: set[str],
        pending_cleanup_attempt_ids: set[str],
    ) -> None:
        if not isinstance(result, RunAttemptAdvanceResult):
            raise TypeError(
                "scheduler.advance() must return RunAttemptAdvanceResult."
            )
        if result.attempt.attempt_id != expected_attempt_id:
            raise RuntimeError(
                "Scheduler advanced a different canonical attempt."
            )
        if expected_attempt_id in advanced_attempt_ids:
            raise RuntimeError(
                "Harness attempted the same cleanup twice in one run invocation."
            )
        advanced_attempt_ids.add(expected_attempt_id)
        if result.cleanup.state == "pending":
            pending_cleanup_attempt_ids.add(expected_attempt_id)
        else:
            pending_cleanup_attempt_ids.discard(expected_attempt_id)

    def _deliver_terminal_completions(self, snapshot: RunLedgerSnapshot) -> None:
        transition_by_trial = {
            item.logical_trial_id: item
            for item in snapshot.logical_transitions
            if item.to_state == "terminal"
        }
        observations_by_attempt = {
            item.attempt_id: item for item in snapshot.observations
        }
        attempts_by_trial: dict[str, int] = {
            item.admission.logical_trial_id: 0 for item in snapshot.logical_trials
        }
        for attempt in snapshot.attempts:
            attempts_by_trial[attempt.logical_trial_id] += 1

        for trial in snapshot.logical_trials:
            trial_id = trial.admission.logical_trial_id
            if trial.state != "terminal" or trial_id in self._delivered_trials:
                continue
            transition = transition_by_trial.get(trial_id)
            if transition is None or transition.outcome is None:
                raise RuntimeError(
                    "Terminal logical trial is missing its canonical transition."
                )
            observation_record = (
                None
                if transition.attempt_id is None
                else observations_by_attempt.get(transition.attempt_id)
            )
            delivery = ParameterObservationDelivery(
                delivery_id=self.identity_source.observation(
                    run_id=self.authority.run_id,
                    logical_trial_id=trial_id,
                ),
                logical_trial_id=trial_id,
                candidate_id=trial.admission.candidate_id,
                outcome=transition.outcome,
                code=transition.code,
                terminal_attempt_id=transition.attempt_id,
                attempt_count=attempts_by_trial[trial_id],
                observation=(
                    None
                    if observation_record is None
                    else ParameterMethodObservation.from_envelope(
                        observation_record.envelope,
                        effective_status=transition.outcome,
                    )
                ),
            )
            self.observe(delivery)
            # Mark only after the callback returns.  A callback failure leaves
            # the same stable delivery eligible for a caller-controlled retry.
            self._delivered_trials.add(trial_id)

    def _result(
        self,
        *,
        state: ParameterRunState,
        termination_request: ParameterRunTerminationRequest | None,
        proposals: int,
        attempt_advances: int,
        pending_cleanup_attempt_ids: set[str],
    ) -> ParameterRunHarnessResult:
        return ParameterRunHarnessResult(
            state=state,
            termination_request=termination_request,
            proposals=proposals,
            attempt_advances=attempt_advances,
            pending_cleanup_attempt_ids=tuple(
                sorted(pending_cleanup_attempt_ids)
            ),
            delivered_logical_trials=tuple(sorted(self._delivered_trials)),
        )


def _state_from(
    authority: RetainedRunAuthority, snapshot: RunLedgerSnapshot
) -> ParameterRunState:
    submission = snapshot.control.current_submission
    terminal_code = (
        None if snapshot.finalization is None else snapshot.finalization.code
    )
    return ParameterRunState(
        run_id=authority.run_id,
        run_revision=snapshot.revision.revision,
        owner_revision=snapshot.revision.owner_revision,
        run_status=snapshot.run.state,
        submission_state=submission.state,
        stop_code=terminal_code or submission.stop_code,
        summary=authority.controller.summary(),
    )


def _proposal_sequence(
    values: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("proposal callback must return a sequence of mappings.")
    result = tuple(values)
    if any(not isinstance(item, Mapping) for item in result):
        raise TypeError("proposal callback must return candidate mappings.")
    return result


def _identity_digest(namespace: str, *coordinates: str) -> str:
    payload = "\0".join((namespace, *coordinates)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("summary must be a mapping.")

    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return copy.deepcopy(item)

    return freeze(value)


def _method_observation(
    envelope: AttemptEnvelope,
    *,
    effective_status: str | None = None,
) -> ParameterMethodObservation:
    """Project one operator envelope into the narrow method-facing DTO."""

    if not isinstance(envelope, AttemptEnvelope):
        raise TypeError("canonical observation envelope is malformed.")
    status = envelope.outcome if effective_status is None else effective_status
    public_error: dict[str, str] = {}
    for key in ("phase", "type", "code", "message"):
        value = envelope.error.get(key)
        if value is not None:
            public_error[key] = _bounded_public_text(value)
    return ParameterMethodObservation(
        status=status,
        metric_values=envelope.metric_values,
        constraint_results=envelope.constraint_results,
        error=public_error,
    )


def _bounded_public_text(value: Any, *, max_bytes: int = 4096) -> str:
    encoded = str(value).encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    suffix = b"..."
    return (encoded[: max_bytes - len(suffix)] + suffix).decode(
        "utf-8", errors="ignore"
    )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return copy.deepcopy(value)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string.")
    return value


__all__ = [
    "CanonicalParameterRunTerminator",
    "DigestParameterRunIdentitySource",
    "ObserveCallback",
    "ParameterMethodObservation",
    "ParameterObservationDelivery",
    "ParameterProposalRequest",
    "ParameterRunHarness",
    "ParameterRunHarnessLimitReached",
    "ParameterRunHarnessResult",
    "ParameterRunIdentitySource",
    "ParameterRunState",
    "ParameterRunTerminationRequest",
    "ProposalCallback",
    "TerminationCallback",
]
