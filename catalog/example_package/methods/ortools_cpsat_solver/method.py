"""Method that solves job-shop cases with JobShopLib OR-Tools CP-SAT."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from catalog.example_package.methods.job_shop_lib_solvers import solve_job_shop_cases


JsonDict = Dict[str, Any]


class OrToolsCpSatSolverMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True
        time_limit = float(self.settings.get("timeLimitSeconds", 10.0))
        try:
            from job_shop_lib.constraint_programming import ORToolsSolver
        except ImportError as exc:
            raise RuntimeError("This example requires JobShopLib. Install it with `uv sync --extra examples`.") from exc
        solutions = solve_job_shop_cases(
            study_state,
            lambda: ORToolsSolver(max_time_in_seconds=max(time_limit * 0.8, 1.0)),
        )
        return [
            {
                "candidate_id": f"job-shop-lib-cpsat-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": {"solutions": solutions},
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "job_shop_lib_ortools_solver",
                    "time_limit_seconds": time_limit,
                },
                "metadata": {"summary": "Schedules produced by JobShopLib ORToolsSolver."},
            }
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None
