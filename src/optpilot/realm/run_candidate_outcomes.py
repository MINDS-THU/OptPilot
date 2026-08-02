"""Generic candidate-outcome comparison for one exact canonical run head.

Outcome comparison is independent of candidate representation.  It therefore
remains useful for parameter, file, and opaque candidates even when Core has no
generic presenter for the candidate inputs themselves.  Only the retained
primary objective and explicitly authored secondary metrics are included.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ._validation import freeze_json, thaw_json
from .run_candidate_evidence import (
    CandidateEvaluationEvidence,
    CandidateEvaluationEvidenceIndex,
)
from .run_candidate_results import (
    RUN_CANDIDATE_SUPPORTED_AGGREGATIONS,
    CandidateResultIndex,
    aggregate_candidate_metric,
    finite_candidate_metric,
)
from .run_snapshot import RunLedgerSnapshot
from .selections import SelectionEligibility


RUN_CANDIDATE_OUTCOME_COMPARISON_SCHEMA = "optpilot.run-candidate-outcome-comparison.v1"
RUN_CANDIDATE_CONSTRAINT_COMPARISON_SCHEMA = (
    "optpilot.run-candidate-constraint-comparison.v1"
)
RUN_CANDIDATE_OUTCOME_MAX_METRICS = 32
RUN_CANDIDATE_OUTCOME_MAX_METRIC_NAME_BYTES = 256
RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINTS = 32
RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINT_NAME_BYTES = 256

_IRREVERSIBLE_EVIDENCE_REASONS = (
    "terminal_result_not_successful",
    "terminal_observation_missing",
    "terminal_observation_not_successful",
    "metric_missing_or_nonfinite",
)


def _bounded_metric_name(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= RUN_CANDIDATE_OUTCOME_MAX_METRIC_NAME_BYTES:
        return value, False
    prefix = encoded[:RUN_CANDIDATE_OUTCOME_MAX_METRIC_NAME_BYTES]
    while prefix:
        try:
            return prefix.decode("utf-8"), True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return "metric", True


def _bounded_constraint_name(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINT_NAME_BYTES:
        return value, False
    prefix = encoded[:RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINT_NAME_BYTES]
    while prefix:
        try:
            return prefix.decode("utf-8"), True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return "constraint", True


def _secondary_metric_names(snapshot: RunLedgerSnapshot) -> tuple[str, ...]:
    objective = snapshot.evaluation_closure.evaluation_template.objective
    raw = objective.get("secondaryMetrics", ())
    if raw is None:
        raw = ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("retained objective secondaryMetrics must be a sequence.")
    primary = snapshot.control.manifest.objective_metric
    result = tuple(raw)
    if any(not isinstance(name, str) or not name for name in result):
        raise ValueError(
            "retained objective secondaryMetrics must contain non-empty names."
        )
    if len(set(result)) != len(result) or primary in result:
        raise ValueError("retained objective metric names must be unique.")
    return result


def _aggregation_mode(snapshot: RunLedgerSnapshot) -> str | None:
    aggregation = snapshot.evaluation_closure.evaluation_template.objective.get(
        "aggregation"
    )
    if not isinstance(aggregation, Mapping):
        return None
    mode = aggregation.get("mode")
    return mode if isinstance(mode, str) else None


def _secondary_metric_cell(
    evidence: CandidateEvaluationEvidence,
    *,
    metric_name: str,
    aggregation_mode: str | None,
) -> dict[str, Any]:
    samples: list[float] = []
    evidence_reasons: set[str] = set()
    for coordinate in evidence.coordinates:
        if coordinate.state != "terminal":
            continue
        if coordinate.result_reason is not None:
            evidence_reasons.add(coordinate.result_reason)
            continue
        observation = coordinate.observation
        if observation is None:  # Defensive index fence.
            evidence_reasons.add("terminal_observation_missing")
            continue
        value = finite_candidate_metric(
            observation.envelope.metric_values.get(metric_name)
        )
        if value is None:
            evidence_reasons.add("metric_missing_or_nonfinite")
            continue
        samples.append(value)

    irreversible_reason = next(
        (
            reason
            for reason in _IRREVERSIBLE_EVIDENCE_REASONS
            if reason in evidence_reasons
        ),
        None,
    )
    planned = len(evidence.coordinates)
    if aggregation_mode not in RUN_CANDIDATE_SUPPORTED_AGGREGATIONS:
        reason = "objective_aggregation_not_supported"
    elif not evidence.coordinates:
        reason = "not_evaluated"
    elif irreversible_reason is not None:
        reason = irreversible_reason
    elif evidence.active:
        reason = "candidate_evaluation_active"
    elif evidence.successful != planned:
        reason = "terminal_result_not_successful"
    elif len(samples) != planned:
        reason = "metric_missing_or_nonfinite"
    else:
        reason = None

    aggregate = None
    if reason is None:
        value = aggregate_candidate_metric(tuple(samples), aggregation_mode)
        if value is None:
            reason = "aggregate_not_finite"
        else:
            aggregate = {"value": value, "sample_count": len(samples)}
    return {
        "status": "complete" if aggregate is not None else "incomplete",
        "reason": reason,
        "coverage": {
            "planned": planned,
            "active": evidence.active,
            "terminal": evidence.terminal,
            "successful": evidence.successful,
            "usable": len(samples),
        },
        "aggregate": aggregate,
    }


def _primary_metric_cell(result: Mapping[str, Any]) -> dict[str, Any]:
    counts = result["counts"]
    aggregate = result["aggregate"]
    return {
        "status": "complete" if aggregate is not None else "incomplete",
        "reason": result["reason"],
        "coverage": {
            "planned": counts["logical_trials"],
            "active": counts["active"],
            "terminal": counts["terminal"],
            "successful": counts["successful"],
            "usable": counts["usable_objectives"],
        },
        "aggregate": None if aggregate is None else thaw_json(aggregate),
    }


def _metric_relation(
    *,
    plan_matches: bool,
    role: str,
    direction: str | None,
    baseline: Mapping[str, Any],
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    if not plan_matches:
        return {
            "eligible": False,
            "reason": "evaluation_plan_mismatch",
            "numeric": None,
            "delta": None,
            "delta_semantics": "comparison_minus_baseline",
            "preferred_operand": None,
        }
    baseline_aggregate = baseline.get("aggregate")
    comparison_aggregate = comparison.get("aggregate")
    if baseline_aggregate is None and comparison_aggregate is None:
        reason = "both_metrics_incomplete"
    elif baseline_aggregate is None:
        reason = "baseline_metric_incomplete"
    elif comparison_aggregate is None:
        reason = "comparison_metric_incomplete"
    else:
        reason = None
    if reason is not None:
        return {
            "eligible": False,
            "reason": reason,
            "numeric": None,
            "delta": None,
            "delta_semantics": "comparison_minus_baseline",
            "preferred_operand": None,
        }

    baseline_value = float(baseline_aggregate["value"])
    comparison_value = float(comparison_aggregate["value"])
    if comparison_value == baseline_value:
        numeric = "equal"
        preferred = "tie" if role == "primary" else None
    elif comparison_value > baseline_value:
        numeric = "higher"
        preferred = (
            ("comparison" if direction == "maximize" else "baseline")
            if role == "primary"
            else None
        )
    else:
        numeric = "lower"
        preferred = (
            ("comparison" if direction == "minimize" else "baseline")
            if role == "primary"
            else None
        )
    delta = comparison_value - baseline_value
    if not math.isfinite(delta):
        delta = None
    return {
        "eligible": True,
        "reason": None,
        "numeric": numeric,
        "delta": delta,
        "delta_semantics": "comparison_minus_baseline",
        "preferred_operand": preferred,
    }


def _constraint_names(
    baseline: CandidateEvaluationEvidence,
    comparison: CandidateEvaluationEvidence,
) -> tuple[str, ...]:
    names: set[str] = set()
    for evidence in (baseline, comparison):
        for coordinate in evidence.coordinates:
            observation = coordinate.observation
            if observation is None:
                continue
            names.update(
                name
                for name in observation.envelope.constraint_results
                if isinstance(name, str) and name
            )
    return tuple(sorted(names, key=lambda value: value.encode("utf-8")))


def _constraint_cell(
    evidence: CandidateEvaluationEvidence,
    *,
    constraint_name: str,
) -> dict[str, Any]:
    satisfied = 0
    violated = 0
    missing = 0
    unsupported = 0
    evidence_reasons: set[str] = set()
    for coordinate in evidence.coordinates:
        if coordinate.state != "terminal":
            continue
        if coordinate.result_reason is not None:
            evidence_reasons.add(coordinate.result_reason)
            continue
        observation = coordinate.observation
        if observation is None:
            evidence_reasons.add("terminal_observation_missing")
            continue
        results = observation.envelope.constraint_results
        if constraint_name not in results:
            missing += 1
            continue
        value = results[constraint_name]
        if not isinstance(value, bool):
            unsupported += 1
        elif value:
            satisfied += 1
        else:
            violated += 1

    planned = len(evidence.coordinates)
    irreversible_reason = next(
        (
            reason
            for reason in _IRREVERSIBLE_EVIDENCE_REASONS
            if reason in evidence_reasons
        ),
        None,
    )
    if not evidence.coordinates:
        reason = "not_evaluated"
    elif irreversible_reason is not None:
        reason = irreversible_reason
    elif missing:
        reason = "constraint_result_missing"
    elif unsupported:
        reason = "constraint_result_not_boolean"
    elif evidence.active:
        reason = "candidate_evaluation_active"
    elif evidence.successful != planned:
        reason = "terminal_result_not_successful"
    elif satisfied + violated != planned:
        reason = "constraint_result_incomplete"
    else:
        reason = None
    complete = reason is None
    return {
        "status": "complete" if complete else "incomplete",
        "reason": reason,
        "coverage": {
            "planned": planned,
            "active": evidence.active,
            "terminal": evidence.terminal,
            "successful": evidence.successful,
            "satisfied": satisfied,
            "violated": violated,
            "missing": missing,
            "unsupported": unsupported,
        },
        "all_satisfied": satisfied == planned if complete else None,
    }


def _constraint_relation(
    *,
    plan_matches: bool,
    baseline: Mapping[str, Any],
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    if not plan_matches:
        return {
            "eligible": False,
            "reason": "evaluation_plan_mismatch",
            "relation": None,
            "preferred_operand": None,
        }
    if baseline.get("status") != "complete":
        reason = "baseline_constraint_incomplete"
    elif comparison.get("status") != "complete":
        reason = "comparison_constraint_incomplete"
    else:
        reason = None
    if reason is not None:
        return {
            "eligible": False,
            "reason": reason,
            "relation": None,
            "preferred_operand": None,
        }
    baseline_satisfied = baseline.get("all_satisfied") is True
    comparison_satisfied = comparison.get("all_satisfied") is True
    if baseline_satisfied and comparison_satisfied:
        relation = "both_satisfied"
        preferred = "tie"
    elif baseline_satisfied:
        relation = "baseline_only_satisfied"
        preferred = "baseline"
    elif comparison_satisfied:
        relation = "comparison_only_satisfied"
        preferred = "comparison"
    else:
        relation = "both_violated"
        preferred = None
    return {
        "eligible": True,
        "reason": None,
        "relation": relation,
        "preferred_operand": preferred,
    }


def _constraint_comparison(
    *,
    baseline: CandidateEvaluationEvidence,
    comparison: CandidateEvaluationEvidence,
    plan_matches: bool,
) -> dict[str, Any]:
    names = _constraint_names(baseline, comparison)
    selected = names[:RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINTS]
    rows = []
    for ordinal, name in enumerate(selected):
        baseline_cell = _constraint_cell(baseline, constraint_name=name)
        comparison_cell = _constraint_cell(comparison, constraint_name=name)
        display_name, name_truncated = _bounded_constraint_name(name)
        rows.append(
            {
                "ordinal": ordinal,
                "name": display_name,
                "name_truncated": name_truncated,
                "semantics": "boolean_satisfied",
                "baseline": baseline_cell,
                "comparison": comparison_cell,
                "relation": _constraint_relation(
                    plan_matches=plan_matches,
                    baseline=baseline_cell,
                    comparison=comparison_cell,
                ),
            }
        )
    omitted = len(names) - len(selected)
    eligibility = (
        SelectionEligibility.ready()
        if names
        else SelectionEligibility.unavailable(
            "no_constraint_results",
            "No evaluator constraint results were retained for these candidates.",
        )
    )
    return {
        "schema": RUN_CANDIDATE_CONSTRAINT_COMPARISON_SCHEMA,
        "eligibility": eligibility.to_dict(),
        "semantics": {
            "value": "boolean",
            "true": "satisfied",
            "false": "violated",
            "preference": "feasible_over_infeasible_only",
        },
        "total": len(names),
        "returned": len(rows),
        "omitted": omitted,
        "truncated": omitted > 0,
        "rows": rows,
    }


@dataclass(frozen=True)
class RunCandidateOutcomeComparison:
    """Bounded metric comparison for two candidates at one exact run head."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.payload, label="candidate outcome comparison")
        if not isinstance(frozen, Mapping):
            raise TypeError("candidate outcome comparison must be a mapping.")
        object.__setattr__(self, "payload", MappingProxyType(dict(frozen)))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self.payload)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunLedgerSnapshot,
        *,
        baseline_candidate_key: str,
        comparison_candidate_key: str,
        evidence_index: CandidateEvaluationEvidenceIndex | None = None,
        result_index: CandidateResultIndex | None = None,
    ) -> "RunCandidateOutcomeComparison":
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        evidence = (
            CandidateEvaluationEvidenceIndex.from_snapshot(snapshot)
            if evidence_index is None
            else evidence_index
        )
        if not isinstance(evidence, CandidateEvaluationEvidenceIndex):
            raise TypeError(
                "evidence_index must be a CandidateEvaluationEvidenceIndex or None."
            )
        if not evidence.matches_snapshot(snapshot):
            raise ValueError(
                "candidate evidence and snapshot do not describe the same run head."
            )
        results = (
            CandidateResultIndex.from_snapshot(snapshot, evidence_index=evidence)
            if result_index is None
            else result_index
        )
        if not isinstance(results, CandidateResultIndex):
            raise TypeError("result_index must be a CandidateResultIndex or None.")
        if (
            results.run_id != snapshot.run.run_id
            or results.revision != snapshot.revision.revision
            or results.sequence != snapshot.revision.last_sequence
        ):
            raise ValueError(
                "candidate results and snapshot do not describe the same run head."
            )

        baseline_evidence = evidence.for_candidate_key(baseline_candidate_key)
        comparison_evidence = evidence.for_candidate_key(comparison_candidate_key)
        baseline_result = results.for_candidate_key(baseline_candidate_key)
        comparison_result = results.for_candidate_key(comparison_candidate_key)
        primary = snapshot.control.manifest.objective_metric
        direction = snapshot.control.manifest.objective_direction
        aggregation_mode = _aggregation_mode(snapshot)
        all_metrics = (primary, *_secondary_metric_names(snapshot))
        selected_metrics = all_metrics[:RUN_CANDIDATE_OUTCOME_MAX_METRICS]
        omitted = len(all_metrics) - len(selected_metrics)
        plan_matches = baseline_evidence.plan_digest == comparison_evidence.plan_digest

        metric_rows = []
        for ordinal, metric_name in enumerate(selected_metrics):
            role = "primary" if ordinal == 0 else "secondary"
            if role == "primary":
                baseline_cell = _primary_metric_cell(baseline_result)
                comparison_cell = _primary_metric_cell(comparison_result)
                metric_direction: str | None = direction
            else:
                baseline_cell = _secondary_metric_cell(
                    baseline_evidence,
                    metric_name=metric_name,
                    aggregation_mode=aggregation_mode,
                )
                comparison_cell = _secondary_metric_cell(
                    comparison_evidence,
                    metric_name=metric_name,
                    aggregation_mode=aggregation_mode,
                )
                metric_direction = None
            display_name, name_truncated = _bounded_metric_name(metric_name)
            metric_rows.append(
                {
                    "ordinal": ordinal,
                    "name": display_name,
                    "name_truncated": name_truncated,
                    "role": role,
                    "direction": metric_direction,
                    "aggregation_mode": aggregation_mode,
                    "baseline": baseline_cell,
                    "comparison": comparison_cell,
                    "relation": _metric_relation(
                        plan_matches=plan_matches,
                        role=role,
                        direction=metric_direction,
                        baseline=baseline_cell,
                        comparison=comparison_cell,
                    ),
                }
            )

        constraints = _constraint_comparison(
            baseline=baseline_evidence,
            comparison=comparison_evidence,
            plan_matches=plan_matches,
        )
        return cls(
            {
                "schema": RUN_CANDIDATE_OUTCOME_COMPARISON_SCHEMA,
                "eligibility": SelectionEligibility.ready().to_dict(),
                "evaluation_plan": {
                    "relation": "matching" if plan_matches else "different",
                    "baseline": {
                        "digest": baseline_evidence.plan_digest,
                        "coordinate_count": len(baseline_evidence.coordinates),
                    },
                    "comparison": {
                        "digest": comparison_evidence.plan_digest,
                        "coordinate_count": len(comparison_evidence.coordinates),
                    },
                },
                "metrics": {
                    "total": len(all_metrics),
                    "returned": len(metric_rows),
                    "omitted": omitted,
                    "truncated": omitted > 0,
                    "rows": metric_rows,
                },
                "constraints": constraints,
            }
        )


__all__ = [
    "RUN_CANDIDATE_CONSTRAINT_COMPARISON_SCHEMA",
    "RUN_CANDIDATE_OUTCOME_COMPARISON_SCHEMA",
    "RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINTS",
    "RUN_CANDIDATE_OUTCOME_MAX_CONSTRAINT_NAME_BYTES",
    "RUN_CANDIDATE_OUTCOME_MAX_METRICS",
    "RUN_CANDIDATE_OUTCOME_MAX_METRIC_NAME_BYTES",
    "RunCandidateOutcomeComparison",
]
