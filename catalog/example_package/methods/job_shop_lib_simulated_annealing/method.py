"""Method that solves job-shop cases with JobShopLib simulated annealing."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from job_shop_lib_solvers import solve_job_shop_cases


JsonDict = Dict[str, Any]


class JobShopLibSimulatedAnnealingMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True
        initial_temperature = float(self.settings.get("initialTemperature", 2500.0))
        ending_temperature = float(self.settings.get("endingTemperature", 2.5))
        steps = int(self.settings.get("steps", 1000))
        updates = int(self.settings.get("updates", 0))
        seed = int(self.settings.get("seed", 0))
        try:
            from job_shop_lib.metaheuristics import SimulatedAnnealingSolver
        except ImportError as exc:
            raise RuntimeError("This example requires JobShopLib. Install it with `uv sync --extra examples`.") from exc
        solutions = solve_job_shop_cases(
            study_state,
            lambda: SimulatedAnnealingSolver(
                initial_temperature=initial_temperature,
                ending_temperature=ending_temperature,
                steps=steps,
                updates=updates,
                seed=seed,
            ),
        )
        return [
            {
                "candidate_id": f"job-shop-lib-sa-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": {"solutions": solutions},
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "job_shop_lib_simulated_annealing",
                },
                "metadata": {"summary": "Schedules produced by JobShopLib SimulatedAnnealingSolver."},
            }
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None
