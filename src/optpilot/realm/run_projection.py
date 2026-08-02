"""Pure operator-facing summary projection for a canonical run snapshot.

This module derives compact monitoring facts from :class:`RunLedgerSnapshot`.
It is deliberately a read model: it has no persistence or mutation API, cannot
be deserialized as recovery authority, and never names a workspace checkout.
Callers that need fresher facts read another ledger snapshot and derive another
projection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .run_records import RUN_STATES
from .run_snapshot import RunLedgerSnapshot


RUN_SUMMARY_PROJECTION_SCHEMA = "optpilot.run-summary-projection.v1"

_LOGICAL_STATE_ORDER = ("accepted", "queued", "running", "retrying", "terminal")
_ATTEMPT_STATE_ORDER = ("prepared", "running", "terminal")
_OBSERVATION_OUTCOME_ORDER = (
    "success",
    "invalid",
    "failed",
    "timeout",
    "partial",
    "cancelled",
)
_FINAL_FAILURE_OUTCOMES = frozenset({"invalid", "failed", "timeout", "partial"})


def _count_by(
    values: tuple[Any, ...], keys: tuple[str, ...], field: str
) -> Mapping[str, int]:
    counts = {key: 0 for key in keys}
    for value in values:
        key = getattr(value, field)
        counts[key] += 1
    return MappingProxyType(counts)


def _freeze_counts(
    value: Mapping[str, int], *, keys: tuple[str, ...], label: str
) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError(f"{label} must contain every canonical state exactly once.")
    result: dict[str, int] = {}
    for key in keys:
        count = value[key]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{label}.{key} must be a nonnegative integer.")
        result[key] = count
    return MappingProxyType(result)


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _finite_metric(value: Any) -> float | None:
    # Canonical metric values are JSON numbers.  Do not turn strings or booleans
    # into apparently valid optimization evidence in an operator projection.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    metric = float(value)
    return metric if math.isfinite(metric) else None


@dataclass(frozen=True)
class RunProjectionCursor:
    """Ledger head consumed by one derived projection.

    ``sequence`` is the latest committed global run-event sequence.  A live
    reader can request events strictly after it, while ``revision`` fences any
    selection or action that must resolve against this exact run head.
    """

    revision: int
    sequence: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.revision, "projection cursor revision")
        _nonnegative_int(self.sequence, "projection cursor sequence")

    @property
    def next_sequence(self) -> int:
        return self.sequence + 1

    def to_dict(self) -> dict[str, int]:
        return {"revision": self.revision, "sequence": self.sequence}


@dataclass(frozen=True)
class RunSummaryProjection:
    """Small immutable status/summary view derived from one ledger snapshot."""

    run_id: str
    run_status: str
    submission_state: str
    stop_code: str | None
    retention_state: str
    objective_metric: str
    objective_direction: str
    max_trials: int | None
    remaining_trials: int | None
    candidate_count: int
    accepted_logical_trials: int
    terminal_logical_trials: int
    successful_logical_trials: int
    successful_objective_observations: int
    final_logical_failures: int
    no_improvement_count: int
    logical_trials_by_state: Mapping[str, int]
    attempt_count: int
    retry_count: int
    attempts_by_state: Mapping[str, int]
    observation_count: int
    observations_by_outcome: Mapping[str, int]
    best_metric: float | None
    best_candidate_id: str | None
    best_logical_trial_id: str | None
    best_attempt_id: str | None
    best_observation_id: str | None
    cursor: RunProjectionCursor

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string.")
        if self.run_status not in RUN_STATES:
            raise ValueError("run_status is unsupported.")
        if self.submission_state not in {"accepting", "draining", "terminal"}:
            raise ValueError("submission_state is unsupported.")
        if self.retention_state not in {"active", "retired"}:
            raise ValueError("retention_state is unsupported.")
        if not isinstance(self.objective_metric, str) or not self.objective_metric:
            raise ValueError("objective_metric must be a non-empty string.")
        if self.objective_direction not in {"minimize", "maximize"}:
            raise ValueError("objective_direction is unsupported.")
        if self.stop_code is not None and (
            not isinstance(self.stop_code, str) or not self.stop_code
        ):
            raise ValueError("stop_code must be a non-empty string or None.")
        if self.submission_state == "accepting" and self.stop_code is not None:
            raise ValueError("An accepting run cannot expose a stop code.")
        if self.submission_state != "accepting" and self.stop_code is None:
            raise ValueError("A draining or terminal run requires a stop code.")

        if self.max_trials is not None:
            if (
                isinstance(self.max_trials, bool)
                or not isinstance(self.max_trials, int)
                or self.max_trials <= 0
            ):
                raise ValueError("max_trials must be a positive integer or None.")
        if self.remaining_trials is not None:
            _nonnegative_int(self.remaining_trials, "remaining_trials")
        if (self.max_trials is None) != (self.remaining_trials is None):
            raise ValueError(
                "max_trials and remaining_trials must both be bounded or unbounded."
            )

        for name in (
            "candidate_count",
            "accepted_logical_trials",
            "terminal_logical_trials",
            "successful_logical_trials",
            "successful_objective_observations",
            "final_logical_failures",
            "no_improvement_count",
            "attempt_count",
            "retry_count",
            "observation_count",
        ):
            _nonnegative_int(getattr(self, name), name)

        logical_counts = _freeze_counts(
            self.logical_trials_by_state,
            keys=_LOGICAL_STATE_ORDER,
            label="logical_trials_by_state",
        )
        attempt_counts = _freeze_counts(
            self.attempts_by_state,
            keys=_ATTEMPT_STATE_ORDER,
            label="attempts_by_state",
        )
        observation_counts = _freeze_counts(
            self.observations_by_outcome,
            keys=_OBSERVATION_OUTCOME_ORDER,
            label="observations_by_outcome",
        )
        object.__setattr__(self, "logical_trials_by_state", logical_counts)
        object.__setattr__(self, "attempts_by_state", attempt_counts)
        object.__setattr__(self, "observations_by_outcome", observation_counts)
        if sum(logical_counts.values()) != self.accepted_logical_trials:
            raise ValueError("Logical state counts differ from accepted_logical_trials.")
        if logical_counts["terminal"] != self.terminal_logical_trials:
            raise ValueError("Terminal logical count differs from logical state counts.")
        if sum(attempt_counts.values()) != self.attempt_count:
            raise ValueError("Attempt state counts differ from attempt_count.")
        if sum(observation_counts.values()) != self.observation_count:
            raise ValueError("Observation outcome counts differ from observation_count.")
        if self.successful_logical_trials > self.terminal_logical_trials:
            raise ValueError("Successful logical trials exceed terminal logical trials.")
        if self.successful_objective_observations > self.successful_logical_trials:
            raise ValueError(
                "Successful objective observations exceed successful logical trials."
            )
        if self.final_logical_failures > self.terminal_logical_trials:
            raise ValueError("Final logical failures exceed terminal logical trials.")
        if self.no_improvement_count > self.terminal_logical_trials:
            raise ValueError("No-improvement count exceeds terminal logical trials.")
        if self.retry_count > self.attempt_count:
            raise ValueError("Retry count exceeds attempt count.")
        if self.max_trials is not None and (
            self.remaining_trials != self.max_trials - self.accepted_logical_trials
        ):
            raise ValueError("remaining_trials differs from the canonical budget.")

        best_ids = (
            self.best_candidate_id,
            self.best_logical_trial_id,
            self.best_attempt_id,
            self.best_observation_id,
        )
        if self.best_metric is None:
            if any(value is not None for value in best_ids):
                raise ValueError("Best-result identities require a best_metric.")
        else:
            if not isinstance(self.best_metric, float) or not math.isfinite(
                self.best_metric
            ):
                raise ValueError("best_metric must be a finite float or None.")
            if any(not isinstance(value, str) or not value for value in best_ids):
                raise ValueError("A best metric requires every stable result identity.")
        if not isinstance(self.cursor, RunProjectionCursor):
            raise TypeError("cursor must be a RunProjectionCursor.")

    @property
    def active_logical_trials(self) -> int:
        return self.accepted_logical_trials - self.terminal_logical_trials

    @classmethod
    def from_snapshot(cls, snapshot: RunLedgerSnapshot) -> "RunSummaryProjection":
        """Derive one summary from immutable canonical facts.

        Best-result selection visits terminal logical transitions in global
        sequence order and considers only the observation anchored by that
        logical trial's terminal attempt.  Earlier failed/retried attempts are
        still counted, but cannot become the trial's displayed result.  Strict
        objective comparison keeps the first terminal result on an exact tie.
        Convergence ``min_delta`` is intentionally not a ranking threshold.
        """

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")

        logical_counts = _count_by(
            snapshot.logical_trials, _LOGICAL_STATE_ORDER, "state"
        )
        attempt_counts = _count_by(snapshot.attempts, _ATTEMPT_STATE_ORDER, "state")
        observation_counts = _count_by(
            tuple(observation.envelope for observation in snapshot.observations),
            _OBSERVATION_OUTCOME_ORDER,
            "outcome",
        )

        candidate_by_key = {
            candidate.candidate_key: candidate for candidate in snapshot.candidates
        }
        trial_by_id = {
            trial.admission.logical_trial_id: trial
            for trial in snapshot.logical_trials
        }
        attempt_by_id = {attempt.attempt_id: attempt for attempt in snapshot.attempts}
        observation_by_attempt = {
            observation.attempt_id: observation
            for observation in snapshot.observations
        }
        terminal_transitions = sorted(
            (
                transition
                for transition in snapshot.logical_transitions
                if transition.to_state == "terminal"
            ),
            key=lambda transition: transition.sequence,
        )

        successful = sum(
            transition.outcome == "success" for transition in terminal_transitions
        )
        failures = sum(
            transition.outcome in _FINAL_FAILURE_OUTCOMES
            for transition in terminal_transitions
        )
        metric_name = snapshot.control.manifest.objective_metric
        direction = snapshot.control.manifest.objective_direction
        best_metric: float | None = None
        best_candidate_id: str | None = None
        best_trial_id: str | None = None
        best_attempt_id: str | None = None
        best_observation_id: str | None = None
        successful_objective_observations = 0
        convergence_best: float | None = None
        no_improvement_count = 0
        min_delta = snapshot.control.manifest.convergence.min_delta
        for transition in terminal_transitions:
            metric = None
            attempt = None
            observation = None
            if transition.outcome == "success" and transition.attempt_id is not None:
                attempt = attempt_by_id.get(transition.attempt_id)
                observation = observation_by_attempt.get(transition.attempt_id)
                if (
                    attempt is not None
                    and observation is not None
                    and observation.status == "success"
                ):
                    metric = _finite_metric(
                        observation.envelope.metric_values.get(metric_name)
                    )
            convergence_improved = metric is not None and (
                convergence_best is None
                or (
                    metric > convergence_best + min_delta
                    if direction == "maximize"
                    else metric < convergence_best - min_delta
                )
            )
            if convergence_improved:
                convergence_best = metric
                no_improvement_count = 0
            else:
                no_improvement_count += 1
            if metric is None:
                continue
            successful_objective_observations += 1
            ranking_improved = best_metric is None or (
                metric > best_metric if direction == "maximize" else metric < best_metric
            )
            if ranking_improved:
                trial = trial_by_id[transition.logical_trial_id]
                candidate = candidate_by_key[trial.candidate_key]
                best_metric = metric
                best_candidate_id = candidate.candidate_id
                best_trial_id = transition.logical_trial_id
                best_attempt_id = attempt.attempt_id
                best_observation_id = observation.observation_id

        attempts_per_trial: dict[str, int] = {
            trial_id: 0 for trial_id in trial_by_id
        }
        for attempt in snapshot.attempts:
            attempts_per_trial[attempt.logical_trial_id] += 1
        retry_count = sum(max(0, count - 1) for count in attempts_per_trial.values())

        submission = snapshot.control.current_submission
        terminal_code = (
            None if snapshot.finalization is None else snapshot.finalization.code
        )
        max_trials = snapshot.control.manifest.max_trials
        accepted = len(snapshot.logical_trials)
        return cls(
            run_id=snapshot.run.run_id,
            run_status=snapshot.run.state,
            submission_state=submission.state,
            stop_code=terminal_code or submission.stop_code,
            retention_state=snapshot.run.retention_state,
            objective_metric=metric_name,
            objective_direction=direction,
            max_trials=max_trials,
            remaining_trials=(None if max_trials is None else max_trials - accepted),
            candidate_count=len(snapshot.candidates),
            accepted_logical_trials=accepted,
            terminal_logical_trials=len(terminal_transitions),
            successful_logical_trials=successful,
            successful_objective_observations=successful_objective_observations,
            final_logical_failures=failures,
            no_improvement_count=no_improvement_count,
            logical_trials_by_state=logical_counts,
            attempt_count=len(snapshot.attempts),
            retry_count=retry_count,
            attempts_by_state=attempt_counts,
            observation_count=len(snapshot.observations),
            observations_by_outcome=observation_counts,
            best_metric=best_metric,
            best_candidate_id=best_candidate_id,
            best_logical_trial_id=best_trial_id,
            best_attempt_id=best_attempt_id,
            best_observation_id=best_observation_id,
            cursor=RunProjectionCursor(
                revision=snapshot.revision.revision,
                sequence=snapshot.revision.last_sequence,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible presentation data, never recovery state."""

        best = None
        if self.best_metric is not None:
            best = {
                "metric": self.best_metric,
                "candidate_id": self.best_candidate_id,
                "logical_trial_id": self.best_logical_trial_id,
                "attempt_id": self.best_attempt_id,
                "observation_id": self.best_observation_id,
            }
        return {
            "schema": RUN_SUMMARY_PROJECTION_SCHEMA,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "submission_state": self.submission_state,
            "stop_code": self.stop_code,
            "retention_state": self.retention_state,
            "objective": {
                "metric": self.objective_metric,
                "direction": self.objective_direction,
            },
            "budget": {
                "max_trials": self.max_trials,
                "remaining_trials": self.remaining_trials,
            },
            "counts": {
                "candidates": self.candidate_count,
                "logical_trials": {
                    "total": self.accepted_logical_trials,
                    "active": self.active_logical_trials,
                    "terminal": self.terminal_logical_trials,
                    "successful": self.successful_logical_trials,
                    "successful_objective_observations": (
                        self.successful_objective_observations
                    ),
                    "final_failures": self.final_logical_failures,
                    "no_improvement": self.no_improvement_count,
                    "by_state": dict(self.logical_trials_by_state),
                },
                "attempts": {
                    "total": self.attempt_count,
                    "retries": self.retry_count,
                    "by_state": dict(self.attempts_by_state),
                },
                "observations": {
                    "total": self.observation_count,
                    "by_outcome": dict(self.observations_by_outcome),
                },
            },
            "best": best,
            "cursor": self.cursor.to_dict(),
        }


__all__ = [
    "RUN_SUMMARY_PROJECTION_SCHEMA",
    "RunProjectionCursor",
    "RunSummaryProjection",
]
