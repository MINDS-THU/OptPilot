"""Pure candidate-level result projection for one canonical run head.

The projection deliberately derives comparison facts from one complete
``RunLedgerSnapshot``.  It persists nothing and never turns a presentation
result into execution or content authority.  Raw observations remain the
canonical evidence; this module only determines when the retained objective
defines a complete, comparable candidate result.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._validation import freeze_json
from .run_candidate_evidence import CandidateEvaluationEvidenceIndex
from .run_snapshot import RunLedgerSnapshot


RUN_CANDIDATE_RESULT_SCHEMA = "optpilot.run-candidate-result.v1"
RUN_CANDIDATE_RESULT_SUMMARY_SCHEMA = "optpilot.run-candidate-result-summary.v1"
RUN_CANDIDATE_RESULT_ORDER = "evaluation-plan-group-accepted-then-rank.v1"

RUN_CANDIDATE_SUPPORTED_AGGREGATIONS = frozenset(
    {"mean", "median", "min", "max", "sum", "last", "weighted_mean"}
)
_IRREVERSIBLE_EVIDENCE_REASONS = (
    "terminal_result_not_successful",
    "terminal_observation_missing",
    "terminal_observation_not_successful",
    "primary_objective_missing_or_nonfinite",
)


def finite_candidate_metric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def aggregate_candidate_metric(values: tuple[float, ...], mode: str) -> float | None:
    # Realm currently retains only aggregation.mode.  execution.py defines an
    # absent weighted_mean weight vector as uniform weights, so candidate-level
    # projection preserves that established semantic instead of rejecting it.
    if mode in {"mean", "weighted_mean"}:
        result = sum(values) / len(values)
    elif mode == "median":
        result = float(statistics.median(values))
    elif mode == "min":
        result = min(values)
    elif mode == "max":
        result = max(values)
    elif mode == "sum":
        result = sum(values)
    elif mode == "last":
        result = values[-1]
    else:  # The caller fences modes before invoking the reducer.
        raise ValueError(f"Unsupported candidate aggregation mode: {mode!r}.")
    return float(result) if math.isfinite(result) else None


def _objective_shape(snapshot: RunLedgerSnapshot) -> tuple[str, str, str | None]:
    metric = snapshot.control.manifest.objective_metric
    direction = snapshot.control.manifest.objective_direction
    objective = snapshot.evaluation_closure.evaluation_template.objective
    aggregation = objective.get("aggregation")
    mode = aggregation.get("mode") if isinstance(aggregation, Mapping) else None
    return metric, direction, mode if isinstance(mode, str) else None


@dataclass(frozen=True)
class CandidateResultIndex:
    """Immutable candidate results and presentation order for one run head."""

    run_id: str
    revision: int
    sequence: int
    _results: Mapping[str, Mapping[str, Any]]
    ordered_candidate_keys: tuple[str, ...]
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("candidate result run_id must be non-empty.")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
            or isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("candidate result head must be nonnegative integers.")
        if not isinstance(self._results, Mapping):
            raise TypeError("candidate results must be a mapping.")
        results: dict[str, Mapping[str, Any]] = {}
        for candidate_key, result in self._results.items():
            if not isinstance(candidate_key, str) or not candidate_key:
                raise ValueError("candidate result keys must be non-empty strings.")
            frozen = freeze_json(result, label="candidate result")
            if not isinstance(frozen, Mapping):
                raise TypeError("candidate result must be a mapping.")
            results[candidate_key] = frozen
        order = tuple(self.ordered_candidate_keys)
        if len(order) != len(set(order)) or set(order) != set(results):
            raise ValueError(
                "candidate result order must contain every candidate once."
            )
        frozen_summary = freeze_json(self.summary, label="candidate result summary")
        if not isinstance(frozen_summary, Mapping):
            raise TypeError("candidate result summary must be a mapping.")
        object.__setattr__(self, "_results", MappingProxyType(results))
        object.__setattr__(self, "ordered_candidate_keys", order)
        object.__setattr__(self, "summary", frozen_summary)

    def for_candidate_key(self, candidate_key: str) -> Mapping[str, Any]:
        """Return one immutable result by the ledger's canonical candidate key."""

        try:
            return self._results[candidate_key]
        except KeyError as error:
            raise KeyError(f"Unknown candidate key: {candidate_key!r}.") from error

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunLedgerSnapshot,
        *,
        evidence_index: CandidateEvaluationEvidenceIndex | None = None,
    ) -> "CandidateResultIndex":
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

        metric_name, direction, aggregation_mode = _objective_shape(snapshot)
        finality = "provisional_at_head" if snapshot.run.state == "running" else "final"

        mutable_results: dict[str, dict[str, Any]] = {}
        accepted_order = {
            candidate.candidate_key: (
                candidate.accepted_sequence,
                candidate.candidate_id,
            )
            for candidate in snapshot.candidates
        }

        for candidate in snapshot.candidates:
            candidate_evidence = evidence.for_candidate_key(candidate.candidate_key)
            coordinate_count = len(candidate_evidence.coordinates)
            usable_samples: list[tuple[int, float, str, str, str]] = []
            evidence_reasons: set[str] = set()

            for coordinate in candidate_evidence.coordinates:
                if coordinate.state != "terminal":
                    continue
                if coordinate.result_reason is not None:
                    evidence_reasons.add(coordinate.result_reason)
                    continue
                observation = coordinate.observation
                attempt = coordinate.attempt
                if observation is None or attempt is None:  # Defensive index fence.
                    evidence_reasons.add("terminal_observation_missing")
                    continue
                metric = finite_candidate_metric(
                    observation.envelope.metric_values.get(metric_name)
                )
                if metric is None:
                    evidence_reasons.add("primary_objective_missing_or_nonfinite")
                    continue
                usable_samples.append(
                    (
                        coordinate.budget_slot,
                        metric,
                        coordinate.logical_trial_id,
                        attempt.attempt_id,
                        observation.observation_id,
                    )
                )

            irreversible_reason = next(
                (
                    reason
                    for reason in _IRREVERSIBLE_EVIDENCE_REASONS
                    if reason in evidence_reasons
                ),
                None,
            )
            if aggregation_mode not in RUN_CANDIDATE_SUPPORTED_AGGREGATIONS:
                aggregate_reason = "objective_aggregation_not_supported"
            elif not candidate_evidence.coordinates:
                aggregate_reason = "not_evaluated"
            elif irreversible_reason is not None:
                aggregate_reason = irreversible_reason
            elif candidate_evidence.active:
                aggregate_reason = "candidate_evaluation_active"
            elif candidate_evidence.successful != coordinate_count:
                aggregate_reason = "terminal_result_not_successful"
            elif len(usable_samples) != coordinate_count:
                aggregate_reason = "primary_objective_missing_or_nonfinite"
            else:
                aggregate_reason = None

            aggregate = None
            if aggregate_reason is None:
                aggregate_value = aggregate_candidate_metric(
                    tuple(sample[1] for sample in usable_samples),
                    aggregation_mode,
                )
                if aggregate_value is None:
                    aggregate_reason = "aggregate_not_finite"
                else:
                    aggregate = {
                        "value": aggregate_value,
                        "sample_count": len(usable_samples),
                    }

            representative = None
            if usable_samples:
                _, _, trial_id, attempt_id, observation_id = usable_samples[-1]
                representative = {
                    "logical_trial_id": trial_id,
                    "attempt_id": attempt_id,
                    "observation_id": observation_id,
                }

            mutable_results[candidate.candidate_key] = {
                "schema": RUN_CANDIDATE_RESULT_SCHEMA,
                "status": "evidence_only" if aggregate is None else "aggregate_only",
                "reason": aggregate_reason,
                "objective": {
                    "metric": metric_name,
                    "direction": direction,
                    "aggregation_mode": aggregation_mode,
                },
                "counts": {
                    "logical_trials": coordinate_count,
                    "active": candidate_evidence.active,
                    "terminal": candidate_evidence.terminal,
                    "successful": candidate_evidence.successful,
                    "terminal_failures": (
                        candidate_evidence.terminal - candidate_evidence.successful
                    ),
                    "usable_objectives": len(usable_samples),
                    "attempts": candidate_evidence.total_attempts,
                    "retries": candidate_evidence.retries,
                },
                "evaluation_plan": {
                    "digest": candidate_evidence.plan_digest,
                    "coordinate_count": coordinate_count,
                },
                "aggregate": aggregate,
                "comparison": {
                    "eligible": False,
                    "rank": None,
                    "group_size": 0,
                    "ranked_candidate_count": 0,
                    "tie_count": 0,
                    "finality": finality,
                    "reason": aggregate_reason,
                    "group_digest": candidate_evidence.plan_digest,
                    "group_ordinal": None,
                    "scope": "within_evaluation_plan",
                },
                "representative": representative,
            }

        plan_groups: dict[str, list[str]] = {}
        aggregate_groups: dict[str, list[str]] = {}
        for candidate_key, result in mutable_results.items():
            plan_digest = result["evaluation_plan"]["digest"]
            plan_groups.setdefault(plan_digest, []).append(candidate_key)
            if result["aggregate"] is not None:
                aggregate_groups.setdefault(plan_digest, []).append(candidate_key)
        ordered_plan_groups = sorted(
            plan_groups.items(),
            key=lambda item: min(
                accepted_order[candidate_key] for candidate_key in item[1]
            ),
        )
        aggregate_candidate_count = sum(
            len(group) for group in aggregate_groups.values()
        )

        ranked_group_count = 0
        ordered_candidate_keys_list: list[str] = []
        for group_ordinal, (plan_digest, all_candidates) in enumerate(
            ordered_plan_groups,
            start=1,
        ):
            all_candidates.sort(key=accepted_order.__getitem__)
            aggregate_candidates = aggregate_groups.get(plan_digest, [])
            aggregate_candidates.sort(key=accepted_order.__getitem__)
            group_size = len(aggregate_candidates)
            ranked_candidate_count = group_size if group_size >= 2 else 0
            for candidate_key in all_candidates:
                mutable_results[candidate_key]["comparison"].update(
                    {
                        "group_size": group_size,
                        "ranked_candidate_count": ranked_candidate_count,
                        "group_ordinal": group_ordinal,
                    }
                )

            ranked_candidates: list[str] = []
            aggregate_only_candidates: list[str] = []
            if group_size == 1:
                aggregate_only_candidates = list(aggregate_candidates)
                reason = (
                    "evaluation_plan_mismatch"
                    if aggregate_candidate_count > 1
                    else "insufficient_comparators"
                )
                mutable_results[aggregate_candidates[0]]["comparison"]["reason"] = (
                    reason
                )
            elif group_size >= 2:
                ranked_group_count += 1
                ranked_candidates = sorted(
                    aggregate_candidates,
                    key=lambda candidate_key: (
                        (
                            mutable_results[candidate_key]["aggregate"]["value"]
                            if direction == "minimize"
                            else -mutable_results[candidate_key]["aggregate"]["value"]
                        ),
                        *accepted_order[candidate_key],
                    ),
                )
                tie_counts: dict[float, int] = {}
                for candidate_key in ranked_candidates:
                    value = mutable_results[candidate_key]["aggregate"]["value"]
                    tie_counts[value] = tie_counts.get(value, 0) + 1
                previous_value: float | None = None
                rank = 0
                for index, candidate_key in enumerate(ranked_candidates, start=1):
                    result = mutable_results[candidate_key]
                    value = result["aggregate"]["value"]
                    if previous_value is None or value != previous_value:
                        rank = index
                        previous_value = value
                    result["status"] = "rankable"
                    result["reason"] = None
                    result["comparison"].update(
                        {
                            "eligible": True,
                            "rank": rank,
                            "tie_count": tie_counts[value],
                            "reason": None,
                        }
                    )

            evidence_candidates = sorted(
                (
                    candidate_key
                    for candidate_key in all_candidates
                    if mutable_results[candidate_key]["aggregate"] is None
                ),
                key=accepted_order.__getitem__,
            )
            ordered_candidate_keys_list.extend(ranked_candidates)
            ordered_candidate_keys_list.extend(aggregate_only_candidates)
            ordered_candidate_keys_list.extend(evidence_candidates)
        ordered_candidate_keys = tuple(ordered_candidate_keys_list)

        status_counts = {"rankable": 0, "aggregate_only": 0, "evidence_only": 0}
        for result in mutable_results.values():
            status_counts[result["status"]] += 1
        summary = {
            "schema": RUN_CANDIDATE_RESULT_SUMMARY_SCHEMA,
            "objective": {
                "metric": metric_name,
                "direction": direction,
                "aggregation_mode": aggregation_mode,
            },
            "counts": {
                **status_counts,
                "comparison_groups": len(aggregate_groups),
                "ranked_groups": ranked_group_count,
            },
            "finality": finality,
            "order": RUN_CANDIDATE_RESULT_ORDER,
        }
        return cls(
            run_id=snapshot.run.run_id,
            revision=snapshot.revision.revision,
            sequence=snapshot.revision.last_sequence,
            _results=mutable_results,
            ordered_candidate_keys=ordered_candidate_keys,
            summary=summary,
        )


__all__ = [
    "RUN_CANDIDATE_RESULT_ORDER",
    "RUN_CANDIDATE_RESULT_SCHEMA",
    "RUN_CANDIDATE_RESULT_SUMMARY_SCHEMA",
    "RUN_CANDIDATE_SUPPORTED_AGGREGATIONS",
    "CandidateResultIndex",
    "aggregate_candidate_metric",
    "finite_candidate_metric",
]
