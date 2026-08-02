"""OptPilot evaluator for executable production-and-AGV policies."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from replay import replay_candidate
from simulation_runner import candidate_fingerprint, normalize_settings, run_policy_once


JsonDict = dict[str, Any]


def evaluate(candidate_runtime: JsonDict, context: JsonDict) -> JsonDict:
    """Evaluate one bounded file candidate over explicit common random seeds."""

    workspace = Path(candidate_runtime.get("workspace") or context["workspace"]).resolve()
    candidate_root = Path(candidate_runtime.get("candidateRoot") or workspace / "candidate").resolve()
    try:
        candidate_root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("candidateRoot must be inside the disposable trial workspace.") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    settings = normalize_settings(context.get("settings", {}))

    records: list[JsonDict] = []
    for seed in settings["seeds"]:
        kpi = run_policy_once(
            candidate_dir=candidate_root,
            settings=settings,
            seed=seed,
            database_path=None,
        )
        records.append({"seed": seed, "kpi": kpi})

    worst_record = min(records, key=lambda item: (item["kpi"]["total_score"], item["seed"]))
    worst_seed = int(worst_record["seed"])
    worst_total_score = float(worst_record["kpi"]["total_score"])
    trace_path = workspace / "worst_run.db"
    replayed_kpi = replay_candidate(
        candidate_dir=candidate_root,
        settings=settings,
        seed=worst_seed,
        database_path=trace_path,
    )
    if not math.isclose(
        float(replayed_kpi["total_score"]),
        worst_total_score,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise RuntimeError(
            "Worst-seed replay did not reproduce its score: "
            f"first={worst_total_score}, replay={replayed_kpi['total_score']}."
        )
    if not trace_path.is_file():
        raise RuntimeError("Worst-seed replay did not create worst_run.db.")

    metrics = _aggregate(records, settings)
    report = {
        "schema": "production-agv-evaluation.v1",
        "candidate_sha256": candidate_fingerprint(candidate_root),
        "settings": settings,
        "metrics": metrics,
        "worst_run": {
            "seed": worst_seed,
            "kpi": replayed_kpi,
            "database": trace_path.name,
        },
        "replications": records,
    }
    metrics_path = workspace / "metrics.json"
    metrics_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "status": "success",
        "metric_values": metrics,
        "constraint_results": {},
        "output_files": [
            {
                "declaration_id": "production-agv-metrics",
                "name": "metrics",
                "path": metrics_path.name,
                "kind": "file",
                "media_type": "application/json",
                "metadata": {"category": "evaluation_summary"},
            },
            {
                "declaration_id": "production-agv-worst-trace",
                "name": "worst_run",
                "path": trace_path.name,
                "kind": "file",
                "media_type": "application/vnd.sqlite3",
                "metadata": {
                    "category": "simulation_trace",
                    "seed": worst_seed,
                    "total_score": worst_total_score,
                },
            },
        ],
        "event_summary": {
            "adapter": "production_agv_scheduling",
            "replication_count": len(records),
            "seeds": list(settings["seeds"]),
            "worst_seed": worst_seed,
            "candidate_sha256": report["candidate_sha256"],
        },
    }


def _aggregate(records: list[JsonDict], settings: Mapping[str, Any]) -> dict[str, float | int]:
    totals = [float(item["kpi"]["total_score"]) for item in records]
    mean_total = statistics.fmean(totals)
    # Equation 17 in the paper uses the sample standard deviation (R - 1).
    # A one-replication smoke has no dispersion estimate and reports zero.
    std_total = statistics.stdev(totals) if len(totals) > 1 else 0.0
    worst_record = min(records, key=lambda item: (item["kpi"]["total_score"], item["seed"]))
    metrics: dict[str, float | int] = {
        "total_score": mean_total,
        "mean_total_score": mean_total,
        "std_total_score": std_total,
        "min_total_score": min(totals),
        "max_total_score": max(totals),
        "stability_fitness": mean_total - float(settings["stability_lambda"]) * std_total,
        "worst_seed": int(worst_record["seed"]),
        "worst_total_score": float(worst_record["kpi"]["total_score"]),
        "replication_count": len(records),
    }
    for source, target in (
        ("efficiency_score", "mean_efficiency_score"),
        ("quality_cost_score", "mean_quality_cost_score"),
        ("agv_score", "mean_agv_score"),
    ):
        metrics[target] = statistics.fmean(float(item["kpi"][source]) for item in records)
    for group, names in (
        ("efficiency_components", ("order_completion", "production_cycle", "device_utilization")),
        ("quality_cost_components", ("first_pass_rate", "cost_efficiency")),
        ("agv_components", ("charge_strategy", "energy_efficiency", "utilization")),
    ):
        for name in names:
            values = [float(item["kpi"][group][name]) for item in records]
            metrics[f"mean_{name}"] = statistics.fmean(values)
    fallback_replans = [_policy_fallback_replans(item["kpi"]) for item in records]
    metrics["mean_policy_fallback_replans"] = statistics.fmean(fallback_replans)
    metrics["policy_fallback_replication_count"] = sum(
        value > 0.0 for value in fallback_replans
    )
    return metrics


def _policy_fallback_replans(kpi: Mapping[str, Any]) -> float:
    diagnostics = kpi.get("policy_diagnostics", {})
    if not isinstance(diagnostics, Mapping):
        raise TypeError("policy_diagnostics must be an object when present.")
    engine = diagnostics.get("engine", {})
    if not isinstance(engine, Mapping):
        raise TypeError("policy_diagnostics.engine must be an object when present.")
    value = engine.get("heuristic_fallback_replans", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("heuristic_fallback_replans must be numeric.")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError("heuristic_fallback_replans must be finite and non-negative.")
    return result
