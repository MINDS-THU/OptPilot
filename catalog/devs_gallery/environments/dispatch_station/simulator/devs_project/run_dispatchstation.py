import argparse
import json
import math
import os
from pathlib import Path

from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock
from devs_project.devs_utils.event_trace import attach_event_trace

from .DispatchStation import DispatchStation

OPTPILOT_RESULT_FILE = "summary.json"

OPTPILOT_METRICS = {
    "average_waiting_time": {
        "direction": "minimize",
        "description": "Average waiting time of completed jobs (minutes)"
    },
    "completed_jobs": {
        "direction": "maximize",
        "description": "Number of completed jobs"
    },
    "machine_utilization": {
        "direction": "maximize",
        "description": "Fraction of time the machine was busy"
    }
}

OPTPILOT_POLICY = {
    "file": "devs_project/policy.py",
    "entrypoint": "create_policy",
    "description": "Dispatch policy that selects the next job from the waiting list given a snapshot of waiting jobs and current time."
}


def write_simulation_summary(metrics, simulated_time, metric_note=None):
    """Write the portable post-run result only when a result root is supplied."""
    result_root = os.environ.get("OPTPILOT_SIMULATION_RESULTS_DIR")
    if not result_root:
        return
    checked_metrics = {}
    for name, value in metrics.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Simulation metric names must be non-empty strings")
        if isinstance(value, bool):
            checked_metrics[name] = value
        elif isinstance(value, int):
            checked_metrics[name] = value
        elif isinstance(value, float) and math.isfinite(value):
            checked_metrics[name] = value
        else:
            raise ValueError(
                f"Simulation metric {name!r} must be bool, int, or finite float"
            )
    numeric_time = float(simulated_time)
    if not math.isfinite(numeric_time):
        raise ValueError("Simulated time must be finite")
    payload = {
        "schema_version": "devs.simulation-result.v1",
        "metrics": checked_metrics,
        "run": {"completed": True, "simulated_time": numeric_time},
    }
    if metric_note:
        payload["metric_note"] = str(metric_note)
    target = Path(result_root) / OPTPILOT_RESULT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run DispatchStation simulation")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducible job arrival sequence")
    parser.add_argument("--simulate_time", type=float, default=480.0,
                        help="Simulation duration in minutes (default: 8-hour shift)")
    args = parser.parse_args()

    seed = args.seed
    simulate_time = args.simulate_time

    clock = SimulationClock()
    set_global_clock(clock)

    model = DispatchStation(
        name="DispatchStation",
        parent=None,
        seed=seed
    )

    sim = Coordinator(model, clock)
    attach_event_trace(sim, model)

    sim.initialize()
    numeric_horizon = float(simulate_time)
    sim.simulate_time(numeric_horizon + 1e-9)
    sim.exit()

    # Collect metrics from the MetricsCollector sub-model
    metrics_collector = model.metrics
    completed_jobs = metrics_collector.count
    total_waiting = metrics_collector.total_waiting
    total_processing = metrics_collector.total_processing

    if completed_jobs > 0:
        avg_waiting = total_waiting / completed_jobs
    else:
        avg_waiting = 0.0

    utilization = total_processing / numeric_horizon if numeric_horizon > 0 else 0.0

    write_simulation_summary(
        metrics={
            "completed_jobs": completed_jobs,
            "average_waiting_time": avg_waiting,
            "machine_utilization": utilization
        },
        simulated_time=numeric_horizon
    )