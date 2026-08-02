"""Bounded decision summary for one exact canonical Run head.

The low-level :mod:`run_projection` summary intentionally reports the best
single terminal observation because that is useful controller evidence.  A
person deciding which Candidate to use needs a different answer: only a
Candidate whose complete evaluation plan produced an aggregate can take part
in Candidate-level comparison.

This module keeps those meanings separate.  It derives a compact Overview from
the same immutable snapshot as the Workbench, reuses
``CandidateResultIndex`` for completeness and ranking, and exposes a fixed-size
objective series that never depends on an entity page requested by a client.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ._validation import freeze_json, thaw_json
from .refs import canonical_json_bytes
from .run_candidate_results import CandidateResultIndex
from .run_comparability import RunComparabilityProjection
from .run_projection import RunSummaryProjection
from .run_snapshot import RunLedgerSnapshot


RUN_OVERVIEW_PROJECTION_SCHEMA = "optpilot.run-overview-projection.v1"
RUN_OVERVIEW_OBJECTIVE_SERIES_SCHEMA = "optpilot.run-objective-series.v1"
RUN_OVERVIEW_OBJECTIVE_ORDER = "candidate-accepted-sequence.v1"
RUN_OVERVIEW_OBJECTIVE_SAMPLING = "uniform-complete-candidates.v1"
RUN_OVERVIEW_MAX_OBJECTIVE_POINTS = 100
RUN_OVERVIEW_MAX_RESPONSE_BYTES = 256 * 1024


def _same_head(
    snapshot: RunLedgerSnapshot,
    *,
    run_id: str,
    revision: int,
    sequence: int,
) -> bool:
    return (
        run_id == snapshot.run.run_id
        and revision == snapshot.revision.revision
        and sequence == snapshot.revision.last_sequence
    )


def _uniform_sample(
    values: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Select a stable first-to-last sample under the fixed public bound."""

    if len(values) <= RUN_OVERVIEW_MAX_OBJECTIVE_POINTS:
        return tuple(values)
    last = len(values) - 1
    denominator = RUN_OVERVIEW_MAX_OBJECTIVE_POINTS - 1
    indexes = tuple(
        (position * last) // denominator
        for position in range(RUN_OVERVIEW_MAX_OBJECTIVE_POINTS)
    )
    # ``len(values) > limit`` makes these indexes strictly increasing, while
    # retaining the assertion as a defensive guard around future bound edits.
    if len(indexes) != len(set(indexes)) or indexes[0] != 0 or indexes[-1] != last:
        raise ValueError("Run Overview objective sampling is not canonical.")
    return tuple(values[index] for index in indexes)


def _best_unavailable_reason(
    *,
    summary: RunSummaryProjection,
    complete_candidates: int,
    comparison_groups: int,
    ranked_groups: int,
) -> str:
    if summary.candidate_count == 0:
        return "waiting_for_first_candidate"
    if complete_candidates == 0:
        if (
            summary.accepted_logical_trials > 0
            and summary.terminal_logical_trials == summary.accepted_logical_trials
            and summary.successful_logical_trials == 0
        ):
            return "all_evaluations_failed"
        if summary.run_status in {"succeeded", "failed", "cancelled"}:
            return "run_finished_without_complete_candidate"
        return "no_complete_candidate_yet"
    if comparison_groups > 1:
        return "complete_candidates_use_different_evaluation_plans"
    if ranked_groups == 0:
        return "only_one_complete_candidate"
    return "no_comparable_complete_candidate"


@dataclass(frozen=True)
class RunOverviewProjection:
    """Immutable, bounded human-facing summary for one Run head."""

    run_id: str
    revision: int
    sequence: int
    _payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("Run Overview run_id must be non-empty.")
        for name in ("revision", "sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Run Overview {name} must be nonnegative.")
        payload = freeze_json(self._payload, label="run overview projection")
        if not isinstance(payload, Mapping):
            raise TypeError("Run Overview projection must be a mapping.")
        if payload.get("schema") != RUN_OVERVIEW_PROJECTION_SCHEMA:
            raise ValueError("Run Overview projection schema is invalid.")
        if payload.get("run_id") != self.run_id or payload.get("head") != self.head:
            raise ValueError("Run Overview payload differs from its exact head.")
        if (
            len(canonical_json_bytes(thaw_json(payload)))
            > RUN_OVERVIEW_MAX_RESPONSE_BYTES
        ):
            raise ValueError("Run Overview projection exceeds its response bound.")
        object.__setattr__(self, "_payload", payload)

    @property
    def head(self) -> dict[str, int]:
        return {"revision": self.revision, "sequence": self.sequence}

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self._payload)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunLedgerSnapshot,
        *,
        summary: RunSummaryProjection | None = None,
        candidate_results: CandidateResultIndex | None = None,
        comparability: RunComparabilityProjection | None = None,
    ) -> "RunOverviewProjection":
        """Derive an Overview from one snapshot and same-head projections."""

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        selected_summary = (
            RunSummaryProjection.from_snapshot(snapshot)
            if summary is None
            else summary
        )
        selected_results = (
            CandidateResultIndex.from_snapshot(snapshot)
            if candidate_results is None
            else candidate_results
        )
        selected_comparability = (
            RunComparabilityProjection.from_snapshot(snapshot)
            if comparability is None
            else comparability
        )
        if not isinstance(selected_summary, RunSummaryProjection):
            raise TypeError("summary must be a RunSummaryProjection or None.")
        if not isinstance(selected_results, CandidateResultIndex):
            raise TypeError("candidate_results must be a CandidateResultIndex or None.")
        if not isinstance(selected_comparability, RunComparabilityProjection):
            raise TypeError(
                "comparability must be a RunComparabilityProjection or None."
            )
        if not _same_head(
            snapshot,
            run_id=selected_summary.run_id,
            revision=selected_summary.cursor.revision,
            sequence=selected_summary.cursor.sequence,
        ):
            raise ValueError("Run Overview summary differs from its snapshot head.")
        if not _same_head(
            snapshot,
            run_id=selected_results.run_id,
            revision=selected_results.revision,
            sequence=selected_results.sequence,
        ):
            raise ValueError(
                "Run Overview Candidate results differ from its snapshot head."
            )
        if not _same_head(
            snapshot,
            run_id=selected_comparability.run_id,
            revision=selected_comparability.revision,
            sequence=selected_comparability.sequence,
        ):
            raise ValueError(
                "Run Overview comparability differs from its snapshot head."
            )

        candidates = sorted(
            snapshot.candidates,
            key=lambda candidate: (
                candidate.accepted_sequence,
                candidate.candidate_id,
            ),
        )
        complete_points: list[dict[str, Any]] = []
        best_candidates: list[tuple[Any, Mapping[str, Any]]] = []
        for candidate in candidates:
            result = selected_results.for_candidate_key(candidate.candidate_key)
            aggregate = result.get("aggregate")
            if not isinstance(aggregate, Mapping):
                continue
            comparison = result.get("comparison")
            if not isinstance(comparison, Mapping):
                raise ValueError("Candidate result comparison is invalid.")
            value = aggregate.get("value")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError("Complete Candidate aggregate must be finite.")
            point = {
                "candidate_id": candidate.candidate_id,
                "accepted_sequence": candidate.accepted_sequence,
                "value": float(value),
                "sample_count": aggregate.get("sample_count"),
                "evaluation_plan_group": comparison.get("group_ordinal"),
                "comparison_eligible": comparison.get("eligible") is True,
                "rank": comparison.get("rank"),
                "tie_count": comparison.get("tie_count", 0),
            }
            complete_points.append(point)
            if point["comparison_eligible"] and point["rank"] == 1:
                best_candidates.append((candidate, result))

        result_counts = selected_results.summary["counts"]
        complete_count = len(complete_points)
        comparison_groups = int(result_counts["comparison_groups"])
        ranked_groups = int(result_counts["ranked_groups"])
        best: dict[str, Any]
        if comparison_groups == 1 and ranked_groups == 1 and best_candidates:
            candidate, result = min(
                best_candidates,
                key=lambda item: (
                    item[0].accepted_sequence,
                    item[0].candidate_id,
                ),
            )
            aggregate = result["aggregate"]
            comparison = result["comparison"]
            best = {
                "available": True,
                "reason": None,
                "candidate_id": candidate.candidate_id,
                "value": float(aggregate["value"]),
                "sample_count": aggregate["sample_count"],
                "rank": comparison["rank"],
                "tie_count": comparison["tie_count"],
                "evaluation_plan_group": comparison["group_ordinal"],
            }
        else:
            best = {
                "available": False,
                "reason": _best_unavailable_reason(
                    summary=selected_summary,
                    complete_candidates=complete_count,
                    comparison_groups=comparison_groups,
                    ranked_groups=ranked_groups,
                ),
                "candidate_id": None,
                "value": None,
                "sample_count": None,
                "rank": None,
                "tie_count": 0,
                "evaluation_plan_group": None,
            }

        sampled_points = _uniform_sample(complete_points)
        all_values = [point["value"] for point in complete_points]
        objective_series = {
            "schema": RUN_OVERVIEW_OBJECTIVE_SERIES_SCHEMA,
            "order": RUN_OVERVIEW_OBJECTIVE_ORDER,
            "sampling": RUN_OVERVIEW_OBJECTIVE_SAMPLING,
            "total_complete_candidates": complete_count,
            "returned": len(sampled_points),
            "omitted": complete_count - len(sampled_points),
            "truncated": len(sampled_points) != complete_count,
            "summary": {
                "minimum": min(all_values) if all_values else None,
                "maximum": max(all_values) if all_values else None,
                "last_in_order": all_values[-1] if all_values else None,
            },
            "points": list(sampled_points),
        }
        payload = {
            "schema": RUN_OVERVIEW_PROJECTION_SCHEMA,
            "run_id": snapshot.run.run_id,
            "head": {
                "revision": snapshot.revision.revision,
                "sequence": snapshot.revision.last_sequence,
            },
            "status": {
                "run_status": selected_summary.run_status,
                "submission_state": selected_summary.submission_state,
                "stop_code": selected_summary.stop_code,
                "finality": selected_results.summary["finality"],
            },
            "objective": {
                "metric": selected_summary.objective_metric,
                "direction": selected_summary.objective_direction,
                "aggregation_mode": selected_results.summary["objective"][
                    "aggregation_mode"
                ],
            },
            "counts": {
                "candidates": {
                    "accepted": selected_summary.candidate_count,
                    "complete": complete_count,
                    "incomplete": selected_summary.candidate_count - complete_count,
                    "comparison_groups": comparison_groups,
                    "ranked_groups": ranked_groups,
                },
                "logical_trials": {
                    "planned": selected_summary.max_trials,
                    "accepted": selected_summary.accepted_logical_trials,
                    "active": selected_summary.active_logical_trials,
                    "terminal": selected_summary.terminal_logical_trials,
                    "successful": selected_summary.successful_logical_trials,
                    "failed": selected_summary.final_logical_failures,
                    "stopped": (
                        selected_summary.terminal_logical_trials
                        - selected_summary.successful_logical_trials
                        - selected_summary.final_logical_failures
                    ),
                },
                "attempts": {
                    "total": selected_summary.attempt_count,
                    "retries": selected_summary.retry_count,
                },
                "observations": {"total": selected_summary.observation_count},
            },
            "best_candidate": best,
            "objective_series": objective_series,
            "failure_count": selected_summary.final_logical_failures,
            "limitations": {
                "max_objective_points": RUN_OVERVIEW_MAX_OBJECTIVE_POINTS,
                "entity_page_size_independent": True,
                "candidate_ranking_scope": "single_evaluation_plan",
            },
        }
        return cls(
            run_id=snapshot.run.run_id,
            revision=snapshot.revision.revision,
            sequence=snapshot.revision.last_sequence,
            _payload=payload,
        )


__all__ = [
    "RUN_OVERVIEW_MAX_OBJECTIVE_POINTS",
    "RUN_OVERVIEW_MAX_RESPONSE_BYTES",
    "RUN_OVERVIEW_OBJECTIVE_ORDER",
    "RUN_OVERVIEW_OBJECTIVE_SAMPLING",
    "RUN_OVERVIEW_OBJECTIVE_SERIES_SCHEMA",
    "RUN_OVERVIEW_PROJECTION_SCHEMA",
    "RunOverviewProjection",
]
