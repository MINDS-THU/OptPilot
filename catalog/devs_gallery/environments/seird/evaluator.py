"""Evaluator for the DEVS-Gen generated SEIRD epidemic simulator.

Runs the generated xDEVS model in-process for one candidate parameter set and
returns the final compartment values as metrics. The simulator code under
``devs_project/`` is unmodified DEVS-Gen output; this adapter is the only
OptPilot-authored code.
"""

from __future__ import annotations

import logging
from typing import Any, Dict


def evaluate(candidate_runtime: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    # The generated logger streams JSONL process logs for every event;
    # evaluation only needs final state.
    logging.disable(logging.CRITICAL)
    try:
        from xdevs.sim import Coordinator, SimulationClock

        from devs_project.devs_utils.devs_context import set_global_clock
        from devs_project.SEIRD_D1 import SEIRD_D1

        settings = context.get("settings") or {}
        simulation_time = float(settings.get("simulationTime", 30.0))
        total_population = int(settings.get("totalPopulation", 1000))

        clock = SimulationClock()
        set_global_clock(clock)
        model = SEIRD_D1(
            name="SEIRD_D1",
            parent=None,
            test_name="optpilot-trial",
            mortality=float(candidate_runtime["mortality"]),
            infectivity_period=float(candidate_runtime["infectivity_period"]),
            dt=float(settings.get("dt", 0.1)),
            incubation_period=float(candidate_runtime["incubation_period"]),
            total_population=total_population,
            initial_infective=int(candidate_runtime["initial_infective"]),
            transmission_rate=float(candidate_runtime["transmission_rate"]),
            simulation_time=simulation_time,
        )
        simulator = Coordinator(model, clock)
        simulator.initialize()
        simulator.simulate_time(simulation_time)
        simulator.exit()
    finally:
        logging.disable(logging.NOTSET)

    integrator = next(
        component
        for component in model.components
        if type(component).__name__ == "SEIRDIntegrator"
    )
    return {
        "metric_values": {
            "deceased": float(integrator.D),
            "recovered": float(integrator.R),
            "infective": float(integrator.I),
            "exposed": float(integrator.E),
            "susceptible": float(integrator.S),
        }
    }
