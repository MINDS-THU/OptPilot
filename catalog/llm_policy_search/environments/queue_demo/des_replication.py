"""Domain-independent seeded-replication scaffolding for DES environments.

The trace-aware policy-search loop needs the same evaluation shape from
every discrete-event environment: run the candidate policy over declared
seeds, pick the worst replication, deterministically replay it into a
bounded SQLite trace, and aggregate KPIs — including the ``worst_seed`` /
``worst_total_score`` pair the method uses for exact-seed replay. This
module is that shape with the domain factored out: an environment supplies
one ``run_once`` callable and gets the whole contract.

Copy this file into your environment folder (bring-your-own-simulator) or
import it from a sibling template; it depends only on the standard library.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence

RunOnce = Callable[..., Mapping[str, Any]]

_DATABASE_FILENAME = "worst_run.db"
_METRICS_FILENAME = "metrics.json"


def _finite(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number, got {value!r}.")
    return float(value)


def _seeds(settings: Mapping[str, Any]) -> Sequence[int]:
    raw = settings.get("seeds")
    if (
        not isinstance(raw, list)
        or not raw
        or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in raw)
    ):
        raise ValueError("settings.seeds must be a non-empty list of integers.")
    if len(set(raw)) != len(raw):
        raise ValueError("settings.seeds must be unique.")
    return list(raw)


def evaluate_policy_candidate(
    candidate_runtime: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    run_once: RunOnce,
) -> Dict[str, Any]:
    """Seeded replications → worst-run trace → aggregated metric values.

    ``run_once(candidate_dir, settings, seed, database_path)`` must run one
    deterministic replication and return ``{"total_score": <finite float>,
    "kpis": {<name>: <finite float>, ...}}``; when ``database_path`` is not
    None it must also write the bounded SQLite trace of that replication.
    """

    workspace = Path(str(context["workspace"])).resolve()
    candidate_dir = Path(
        str(candidate_runtime.get("candidateRoot") or workspace / "candidate")
    ).resolve()
    settings = dict(context.get("settings") or {})
    seeds = _seeds(settings)

    records = []
    for seed in seeds:
        result = run_once(
            candidate_dir=candidate_dir,
            settings=settings,
            seed=seed,
            database_path=None,
        )
        records.append(
            {
                "seed": seed,
                "total_score": _finite(
                    result.get("total_score"), f"seed {seed} total_score"
                ),
                "kpis": {
                    str(name): _finite(value, f"seed {seed} kpi {name}")
                    for name, value in dict(result.get("kpis") or {}).items()
                },
            }
        )

    worst = min(records, key=lambda record: (record["total_score"], record["seed"]))
    database_path = workspace / _DATABASE_FILENAME
    replayed = run_once(
        candidate_dir=candidate_dir,
        settings=settings,
        seed=worst["seed"],
        database_path=database_path,
    )
    replay_score = _finite(replayed.get("total_score"), "replayed total_score")
    if not math.isclose(
        replay_score,
        worst["total_score"],
        rel_tol=1e-9,
        abs_tol=max(1e-9, abs(worst["total_score"]) * 1e-9),
    ):
        raise RuntimeError(
            "Worst-seed replay diverged from the original replication "
            f"({replay_score} != {worst['total_score']}); the simulator is "
            "not deterministic for a fixed seed."
        )
    if not database_path.is_file() or database_path.stat().st_size == 0:
        raise RuntimeError("Worst-seed replay did not write its SQLite trace.")

    scores = [record["total_score"] for record in records]
    metric_values: Dict[str, float] = {
        "mean_total_score": statistics.fmean(scores),
        "std_total_score": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
        "min_total_score": min(scores),
        "max_total_score": max(scores),
        "worst_seed": float(worst["seed"]),
        "worst_total_score": worst["total_score"],
    }
    kpi_names = sorted({name for record in records for name in record["kpis"]})
    for name in kpi_names:
        values = [
            record["kpis"][name] for record in records if name in record["kpis"]
        ]
        metric_values[f"mean_{name}"] = statistics.fmean(values)

    metrics_path = workspace / _METRICS_FILENAME
    metrics_path.write_text(
        json.dumps(
            {
                "schema": "optpilot.des-replication-metrics.v1",
                "replications": records,
                "metric_values": metric_values,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "status": "success",
        "metric_values": metric_values,
        "output_files": [
            {
                "declaration_id": "metrics",
                "name": "metrics",
                "path": metrics_path.relative_to(workspace).as_posix(),
                "kind": "file",
                "media_type": "application/json",
                "metadata": {"replications": len(records)},
            },
            {
                "declaration_id": "worst_run",
                "name": "worst_run",
                "path": database_path.relative_to(workspace).as_posix(),
                "kind": "file",
                "media_type": "application/vnd.sqlite3",
                "metadata": {"seed": worst["seed"]},
            },
        ],
        "event_summary": {
            "replications": len(records),
            "worst_seed": worst["seed"],
        },
    }
