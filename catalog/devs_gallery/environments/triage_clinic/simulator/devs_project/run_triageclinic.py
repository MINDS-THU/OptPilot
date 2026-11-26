import argparse
import json
import math
import os
from pathlib import Path

from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock
from devs_project.devs_utils.event_trace import attach_event_trace

from .TriageClinic import TriageClinic

OPTPILOT_RESULT_FILE = "summary.json"
OPTPILOT_METRICS = {
    "patients_served": {
        "direction": "maximize",
        "description": "Total number of patients served during the shift",
    },
    "avg_urgency_weighted_waiting_time": {
        "direction": "minimize",
        "description": "Average urgency-weighted waiting time across all served patients",
    },
}
OPTPILOT_POLICY = {
    "file": "generated_simulator/devs_project/TriageClinic_libs/ClinicCore_libs/TriagePolicy.py",
    "entrypoint": "TriagePolicy",
    "description": "Triage decision logic that selects the next patient when the doctor becomes free",
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
    parser = argparse.ArgumentParser(description="Run TriageClinic simulation")

    parser.add_argument(
        "--shift_duration",
        type=float,
        default=8.0,
        help="Total shift duration in hours",
    )
    parser.add_argument(
        "--inter_arrival_mean",
        type=float,
        default=0.5,
        help="Mean inter-arrival time in hours (exponential distribution)",
    )
    parser.add_argument(
        "--urgency_probs",
        type=str,
        default="0.5,0.3,0.2",
        help="Comma-separated probability weights for urgency levels 1,2,3",
    )
    parser.add_argument(
        "--exam_durations",
        type=str,
        default="0.25,0.5,1.0",
        help="Comma-separated mean exam durations in hours for each urgency level",
    )
    parser.add_argument(
        "--simulate_time",
        type=float,
        default=8.0,
        help="Simulation duration in hours",
    )
    # Registration-time addition: seeded replications for policy search.
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for a reproducible arrival/exam sequence",
    )

    args = parser.parse_args()

    import random as _random

    _random.seed(args.seed)

    shift_duration = args.shift_duration
    inter_arrival_mean = args.inter_arrival_mean
    urgency_probs = [float(x.strip()) for x in args.urgency_probs.split(",")]
    exam_durations = [float(x.strip()) for x in args.exam_durations.split(",")]
    simulate_time = args.simulate_time

    clock = SimulationClock()
    set_global_clock(clock)

    core_model = TriageClinic(
        name="TriageClinic",
        parent=None,
        shift_duration=shift_duration,
        inter_arrival_mean=inter_arrival_mean,
        urgency_probs=urgency_probs,
        exam_durations=exam_durations,
    )
    model = core_model

    sim = Coordinator(model, clock)
    attach_event_trace(sim, model)

    sim.initialize()
    numeric_horizon = float(simulate_time)
    sim.simulate_time(numeric_horizon + 1e-9)
    sim.exit()

    doctor = model.clinic_core.doctor
    patients_served = int(doctor.patients_served)
    total_weighted_wait = float(doctor.total_urgency_weighted_waiting_time)
    avg_urgency_weighted_waiting_time = (
        total_weighted_wait / patients_served if patients_served > 0 else 0.0
    )

    write_simulation_summary(
        metrics={
            "patients_served": patients_served,
            "avg_urgency_weighted_waiting_time": avg_urgency_weighted_waiting_time,
        },
        simulated_time=numeric_horizon,
    )