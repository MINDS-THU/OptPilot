"""Deterministic run stopping and terminal-outcome policy.

This module contains no ledger, controller, or runtime side effects.  Both the
live controller and the canonical ledger can use the same vocabulary while the
ledger remains authoritative after recovery.

Submission control records retain the *first* reason that admission closed.
The final decision is derived separately because a normal drain (for example
``max_trials``) may later accumulate enough final logical failures to make the
effective terminal result ``failed/max_failures``.  Canonical history is never
rewritten to express that later result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


CANCELLATION_STOP_CODES = frozenset(
    {"user_cancelled", "signal_cancelled", "admin_cancelled"}
)
FAILURE_STOP_CODES = frozenset(
    {
        "protocol_error",
        "max_failures",
        "method_failed",
        "evaluator_failed",
        "controller_lost",
    }
)
NORMAL_STOP_CODES = frozenset(
    {"max_trials", "wall_clock_budget", "converged", "method_completed"}
)
SUBMISSION_STOP_CODES = (
    CANCELLATION_STOP_CODES | FAILURE_STOP_CODES | NORMAL_STOP_CODES
)

# These reasons are produced only from canonical evidence in the same
# transaction that reaches the threshold.  They must not be caller-selected by
# the generic explicit-close API.
DERIVED_SUBMISSION_STOP_CODES = frozenset(
    {"max_trials", "max_failures", "converged"}
)
EXPLICIT_SUBMISSION_STOP_CODES = SUBMISSION_STOP_CODES - DERIVED_SUBMISSION_STOP_CODES
# These stops may atomically abandon one in-flight retained-method exchange.
# Trial-count/convergence/failure-threshold drains must still resolve the method
# callback honestly.  Wall-clock expiration is intentionally a hard deadline,
# alongside cancellation and fatal control failures.
METHOD_EXCHANGE_ABANDON_STOP_CODES = frozenset(
    CANCELLATION_STOP_CODES
    | (FAILURE_STOP_CODES - {"max_failures"})
    | {"wall_clock_budget"}
)
METHOD_EXCHANGE_FEEDBACK_DRAIN_STOP_CODES = frozenset(
    {"max_trials", "converged", "max_failures"}
)
FINAL_FAILURE_OUTCOMES = frozenset({"invalid", "failed", "timeout", "partial"})


@dataclass(frozen=True)
class TerminalLogicalResult:
    """One final logical result in canonical terminal-transition order."""

    outcome: str
    objective_value: float | None

    def __post_init__(self) -> None:
        if self.outcome not in {
            "success",
            "invalid",
            "failed",
            "timeout",
            "partial",
            "cancelled",
        }:
            raise ValueError("terminal logical outcome is unsupported.")
        value = self.objective_value
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("objective_value must be a finite number or None.")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("objective_value must be a finite number or None.")
            object.__setattr__(self, "objective_value", value)
        if self.outcome != "success" and value is not None:
            raise ValueError("Only a successful logical result may carry an objective value.")


@dataclass(frozen=True)
class RunTerminalDecision:
    """The only canonical terminal state/code pairs for a run."""

    run_status: str
    code: str

    def __post_init__(self) -> None:
        if self.run_status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("run_status is not terminal.")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("terminal code must be a non-empty string.")


def derive_post_adoption_stop(
    *,
    terminal_results: Iterable[TerminalLogicalResult],
    active_logical_trials: int,
    max_failures: int | None,
    patience_trials: int | None,
    min_delta: float,
    objective_direction: str,
) -> str | None:
    """Return the evidence-derived admission-close reason, if one is reached.

    ``max_failures`` has precedence over convergence, matching the run terminal
    table.  Convergence is meaningful only after all currently accepted work
    drains; otherwise a completion-order race could close admission while an
    already accepted result is still capable of improving the objective.
    """

    if isinstance(active_logical_trials, bool) or not isinstance(
        active_logical_trials, int
    ) or active_logical_trials < 0:
        raise ValueError("active_logical_trials must be a nonnegative integer.")
    for value, label in (
        (max_failures, "max_failures"),
        (patience_trials, "patience_trials"),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{label} must be a positive integer or None.")
    if isinstance(min_delta, bool) or not isinstance(min_delta, (int, float)):
        raise ValueError("min_delta must be a finite nonnegative number.")
    min_delta = float(min_delta)
    if not math.isfinite(min_delta) or min_delta < 0:
        raise ValueError("min_delta must be a finite nonnegative number.")
    if objective_direction not in {"minimize", "maximize"}:
        raise ValueError("objective_direction is unsupported.")
    results = tuple(terminal_results)
    if any(not isinstance(item, TerminalLogicalResult) for item in results):
        raise TypeError("terminal_results must contain TerminalLogicalResult values.")

    failures = sum(item.outcome in FINAL_FAILURE_OUTCOMES for item in results)
    if max_failures is not None and failures >= max_failures:
        return "max_failures"
    if patience_trials is None or active_logical_trials:
        return None

    best: float | None = None
    no_improvement = 0
    for result in results:
        value = result.objective_value
        improved = value is not None and (
            best is None
            or (
                value > best + min_delta
                if objective_direction == "maximize"
                else value < best - min_delta
            )
        )
        if improved:
            best = value
            no_improvement = 0
        else:
            no_improvement += 1
    return "converged" if no_improvement >= patience_trials else None


def derive_terminal_decision(
    *,
    submission_stop_code: str,
    terminal_results: Iterable[TerminalLogicalResult],
    max_failures: int | None,
) -> RunTerminalDecision:
    """Derive a final run result from immutable close reason and trial facts."""

    if submission_stop_code not in SUBMISSION_STOP_CODES:
        raise ValueError(
            f"Unsupported canonical submission stop code: {submission_stop_code!r}."
        )
    if max_failures is not None and (
        isinstance(max_failures, bool)
        or not isinstance(max_failures, int)
        or max_failures <= 0
    ):
        raise ValueError("max_failures must be a positive integer or None.")
    results = tuple(terminal_results)
    if any(not isinstance(item, TerminalLogicalResult) for item in results):
        raise TypeError("terminal_results must contain TerminalLogicalResult values.")

    if submission_stop_code in CANCELLATION_STOP_CODES:
        return RunTerminalDecision("cancelled", submission_stop_code)
    if submission_stop_code in FAILURE_STOP_CODES:
        return RunTerminalDecision("failed", submission_stop_code)

    failures = sum(item.outcome in FINAL_FAILURE_OUTCOMES for item in results)
    if max_failures is not None and failures >= max_failures:
        return RunTerminalDecision("failed", "max_failures")
    if any(item.outcome == "success" and item.objective_value is not None for item in results):
        return RunTerminalDecision("succeeded", submission_stop_code)
    return RunTerminalDecision("failed", "no_successful_observation")


def finite_objective_value(value: Any) -> float | None:
    """Normalize an untrusted JSON metric without coercing strings or booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def method_feedback_obligations_resolved(snapshot: Any) -> bool:
    """Return whether a draining run no longer needs its retained method.

    A hard stop canonically abandons any in-flight callback.  A feedback drain
    remains method-active until every prepared callback is completed and every
    admitted proposal round has its observation completion.  Keeping this
    predicate beside the stop-code policy gives the runtime owner and run
    driver one shared definition of method inactivity without making either
    layer responsible for the other's side effects.
    """

    submission = snapshot.control.current_submission
    if submission.state != "draining" or submission.stop_code is None:
        return False
    if submission.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES:
        return True

    completed_exchange_ids = {
        item.exchange_id for item in snapshot.method_exchange_completions
    }
    if any(
        item.exchange_id not in completed_exchange_ids
        for item in snapshot.method_exchange_preparations
    ):
        return False
    completed_observation_rounds = {
        item.round_index
        for item in snapshot.method_exchange_completions
        if item.kind == "observation"
    }
    return not any(
        item.kind == "proposal"
        and item.outcome == "admitted"
        and item.round_index not in completed_observation_rounds
        for item in snapshot.method_exchange_completions
    )


__all__ = [
    "CANCELLATION_STOP_CODES",
    "DERIVED_SUBMISSION_STOP_CODES",
    "EXPLICIT_SUBMISSION_STOP_CODES",
    "FAILURE_STOP_CODES",
    "FINAL_FAILURE_OUTCOMES",
    "METHOD_EXCHANGE_ABANDON_STOP_CODES",
    "METHOD_EXCHANGE_FEEDBACK_DRAIN_STOP_CODES",
    "NORMAL_STOP_CODES",
    "RunTerminalDecision",
    "SUBMISSION_STOP_CODES",
    "TerminalLogicalResult",
    "derive_post_adoption_stop",
    "derive_terminal_decision",
    "finite_objective_value",
    "method_feedback_obligations_resolved",
]
