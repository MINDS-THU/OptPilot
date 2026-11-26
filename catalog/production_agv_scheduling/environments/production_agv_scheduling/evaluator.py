"""OptPilot evaluator for executable production-and-AGV policies."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
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
    candidate_sha256 = candidate_fingerprint(candidate_root)

    records: list[JsonDict] = []
    for seed in settings["seeds"]:
        kpi = run_policy_once(
            candidate_dir=candidate_root,
            settings=settings,
            seed=seed,
            database_path=None,
        )
        _require_candidate_fingerprint(
            candidate_root,
            candidate_sha256,
            phase=f"replication seed {seed}",
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
    _require_candidate_fingerprint(
        candidate_root,
        candidate_sha256,
        phase=f"worst-seed replay {worst_seed}",
    )
    if not trace_path.is_file():
        raise RuntimeError("Worst-seed replay did not create worst_run.db.")
    _validate_trace_database(trace_path)
    _assert_canonical_kpi_equal(worst_record["kpi"], replayed_kpi)

    metrics = _aggregate(records, settings)
    report = {
        "schema": "production-agv-evaluation.v1",
        "candidate_sha256": candidate_sha256,
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
    metrics_path.write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

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
            "candidate_sha256": candidate_sha256,
        },
    }


def _require_candidate_fingerprint(
    candidate_root: Path,
    expected_sha256: str,
    *,
    phase: str,
) -> None:
    actual_sha256 = candidate_fingerprint(candidate_root)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Candidate bundle changed during evaluation "
            f"({phase}); expected sha256={expected_sha256}, "
            f"found sha256={actual_sha256}."
        )


def _assert_canonical_kpi_equal(
    first: Any,
    replayed: Any,
    *,
    path: str = "$",
) -> None:
    if isinstance(first, bool) or isinstance(replayed, bool):
        if type(first) is not bool or type(replayed) is not bool or first != replayed:
            raise RuntimeError(
                f"Worst-seed replay KPI mismatch at {path}: {first!r} != {replayed!r}."
            )
        return
    if isinstance(first, (int, float)) or isinstance(replayed, (int, float)):
        if not isinstance(first, (int, float)) or not isinstance(
            replayed, (int, float)
        ):
            raise RuntimeError(
                f"Worst-seed replay KPI structure mismatch at {path}: "
                f"{type(first).__name__} != {type(replayed).__name__}."
            )
        first_number = float(first)
        replayed_number = float(replayed)
        if not math.isfinite(first_number) or not math.isfinite(replayed_number):
            raise RuntimeError(
                f"Worst-seed replay KPI contains a non-finite number at {path}."
            )
        if not math.isclose(
            first_number,
            replayed_number,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise RuntimeError(
                f"Worst-seed replay KPI mismatch at {path}: "
                f"{first_number!r} != {replayed_number!r}."
            )
        return
    if isinstance(first, Mapping) or isinstance(replayed, Mapping):
        if not isinstance(first, Mapping) or not isinstance(replayed, Mapping):
            raise RuntimeError(
                f"Worst-seed replay KPI structure mismatch at {path}: "
                f"{type(first).__name__} != {type(replayed).__name__}."
            )
        first_keys = set(first)
        replayed_keys = set(replayed)
        if first_keys != replayed_keys:
            raise RuntimeError(
                f"Worst-seed replay KPI keys differ at {path}: "
                f"first_only={sorted(first_keys - replayed_keys)!r}, "
                f"replay_only={sorted(replayed_keys - first_keys)!r}."
            )
        for key in sorted(first_keys):
            _assert_canonical_kpi_equal(
                first[key],
                replayed[key],
                path=f"{path}.{key}",
            )
        return
    is_first_sequence = isinstance(first, Sequence) and not isinstance(
        first, (str, bytes, bytearray)
    )
    is_replayed_sequence = isinstance(replayed, Sequence) and not isinstance(
        replayed, (str, bytes, bytearray)
    )
    if is_first_sequence or is_replayed_sequence:
        if (
            not is_first_sequence
            or not is_replayed_sequence
            or type(first) is not type(replayed)
            or len(first) != len(replayed)
        ):
            raise RuntimeError(
                f"Worst-seed replay KPI sequence structure mismatch at {path}."
            )
        for index, (first_item, replayed_item) in enumerate(
            zip(first, replayed, strict=True)
        ):
            _assert_canonical_kpi_equal(
                first_item,
                replayed_item,
                path=f"{path}[{index}]",
            )
        return
    if type(first) is not type(replayed) or first != replayed:
        raise RuntimeError(
            f"Worst-seed replay KPI mismatch at {path}: {first!r} != {replayed!r}."
        )


def _validate_trace_database(trace_path: Path) -> None:
    required_tables = ("kpi", "order")
    try:
        with sqlite3.connect(f"{trace_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            quick_check = [
                str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            if quick_check != ["ok"]:
                raise RuntimeError(
                    "Worst-seed SQLite trace failed PRAGMA quick_check: "
                    + "; ".join(quick_check)
                )
            available_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = sorted(set(required_tables).difference(available_tables))
            if missing:
                raise RuntimeError(
                    f"Worst-seed SQLite trace is missing required tables: {missing!r}."
                )
            for table in required_tables:
                row_count = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                if row_count <= 0:
                    raise RuntimeError(
                        f"Worst-seed SQLite trace table {table!r} has no rows."
                    )
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Worst-seed SQLite trace is unreadable or corrupt: {exc}"
        ) from exc


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
