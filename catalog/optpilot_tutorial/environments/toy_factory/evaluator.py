"""A tiny deterministic evaluator used by the package tutorial."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


MODE_THROUGHPUT = {"efficient": 0.88, "balanced": 1.0, "fast": 1.12}
MODE_COST = {"efficient": 0.85, "balanced": 1.0, "fast": 1.25}


def evaluate(candidate: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    workers = int(candidate["workers"])
    buffer_capacity = int(candidate["buffer_capacity"])
    mode = str(candidate["mode"])
    demand = float((context.get("settings") or {}).get("demand", 72))

    capacity = workers * 11.5 + min(buffer_capacity, 14) * 0.8
    throughput = min(demand, capacity * MODE_THROUGHPUT[mode])
    operating_cost = workers * 8.0 * MODE_COST[mode] + buffer_capacity * 0.35
    score = throughput - 0.55 * operating_cost

    workspace = Path(context["workspace"])
    report = workspace / "evaluation.json"
    report.write_text(
        json.dumps(
            {
                "candidate": candidate,
                "metrics": {
                    "score": score,
                    "throughput": throughput,
                    "operating_cost": operating_cost,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "status": "success",
        "metric_values": {
            "score": score,
            "throughput": throughput,
            "operating_cost": operating_cost,
        },
        "constraint_results": {},
        "output_files": [
            {
                "declaration_id": "tutorial-evaluation",
                "name": "evaluation",
                "path": report.name,
                "kind": "file",
                "media_type": "application/json",
                "metadata": {"category": "evaluation_summary"},
            }
        ],
        "event_summary": {"evaluator": "tutorial-toy-factory"},
    }
