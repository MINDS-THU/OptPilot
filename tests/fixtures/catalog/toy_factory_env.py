"""Toy one-shot environment used by OptPilot tests."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict


MODE_BONUS = {
    "balanced": 6.0,
    "aggressive": 3.0,
    "conservative": 0.0,
}


def evaluate(candidate: Dict[str, float], instance: Dict[str, float], context: Dict[str, str]) -> Dict[str, object]:
    x = float(candidate["x"])
    y = int(candidate["y"])
    mode = candidate["mode"]
    target_x = float(instance["target_x"])
    target_y = int(instance["target_y"])
    sleep_seconds = float(instance.get("sleep_seconds", 0.0))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    penalty = abs(x - target_x) * 18.0 + abs(y - target_y) * 7.5
    throughput = max(0.0, 100.0 - penalty + MODE_BONUS.get(mode, 0.0))
    cycle_time = 200.0 - throughput

    workspace = Path(context["workspace"])
    metrics_path = workspace / f"metrics_{context['instance_index']}.csv"
    with metrics_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["x", "y", "mode", "throughput", "cycle_time"])
        writer.writeheader()
        writer.writerow(
            {
                "x": x,
                "y": y,
                "mode": mode,
                "throughput": throughput,
                "cycle_time": cycle_time,
            }
        )

    return {
        "status": "success",
        "metric_values": {
            "throughput": throughput,
            "cycle_time": cycle_time,
        },
        "output_files": [
            {
                "type": "csv",
                "path": str(metrics_path),
            }
        ],
        "event_summary": {
            "target_x": target_x,
            "target_y": target_y,
        },
    }
