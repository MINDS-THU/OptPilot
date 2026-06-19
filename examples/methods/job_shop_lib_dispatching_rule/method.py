"""Method that solves job-shop instances with a JobShopLib dispatching rule."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from examples.methods.job_shop_lib_solvers import solve_study_instances


JsonDict = Dict[str, Any]


class JobShopLibDispatchingRuleMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True
        rule = str(self.settings.get("dispatchingRule", "most_work_remaining"))
        try:
            from job_shop_lib.dispatching.rules import DispatchingRuleSolver
        except ImportError as exc:
            raise RuntimeError("This example requires JobShopLib. Install it with `uv sync --extra examples`.") from exc
        solutions = solve_study_instances(study_state, lambda: DispatchingRuleSolver(rule))
        return [
            {
                "candidate_id": f"job-shop-lib-dispatch-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": {"solutions": solutions},
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "job_shop_lib_dispatching_rule",
                    "dispatching_rule": rule,
                },
                "metadata": {"summary": f"Schedules produced by JobShopLib DispatchingRuleSolver({rule!r})."},
            }
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None
