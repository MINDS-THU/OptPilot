# This is an example of simulating a coupled model that includes a Generator and a Collector. 

### BEGIN: General Import
import argparse
import json
import math
import os
from pathlib import Path

from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock
from devs_project.devs_utils.event_trace import attach_event_trace
### END

### BEGIN: Model import, must be relative
from .coupled_example import SimpleCoupled
### END

OPTPILOT_RESULT_FILE = "summary.json"


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
    ### BEGIN: Parameter Configuration (ArgParse)
    parser = argparse.ArgumentParser(description="Run SimpleCoupled simulation")
    
    # Define arguments with defaults suitable for the scenario
    parser.add_argument("--gen1_init_value", type=int, default=0, help="Initial value for generator 1")
    parser.add_argument("--gen2_init_value", type=int, default=10, help="Initial value for generator 2")
    parser.add_argument("--simulate_time", type=float, default=60, help="Simulation duration")
    parser.add_argument("--test_name", type=str, default="test_run", help="Name of the test run, unused")
    
    args = parser.parse_args()
    
    # Assign to local variables for clarity (optional, can use args.x directly)
    gen1_init_value = args.gen1_init_value
    gen2_init_value = args.gen2_init_value
    simulate_time = args.simulate_time
    test_name = args.test_name # unused. It is extracted for clarity.
    
    ### BEGIN: Initialization
    clock = SimulationClock()
    set_global_clock(clock) # register the clock
    
    model = SimpleCoupled( # instance the model
        name="simple_coupled", 
        parent=None,
        gen1_init_value=gen1_init_value,
        gen2_init_value=gen2_init_value
    )
    sim = Coordinator(model, clock)
    attach_event_trace(sim, model)
    ### END

    ### BEGIN: Simulation Execution
    sim.initialize()
    numeric_horizon = float(simulate_time)
    sim.simulate_time(numeric_horizon + 1e-9)
    sim.exit()

    # Replace this example extraction with real outcome KPIs exposed by the
    # generated root model. Never guess attributes or invent a generic score.
    write_simulation_summary(
        metrics={},
        simulated_time=numeric_horizon,
        metric_note=(
            "Expose a stable model outcome (for example a completed-item count) "
            "before using this simulator as an optimization Environment."
        ),
    )
    ### END
