"""Fixed weighted dispatch-rule parameter method."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List


class FixedRuleParametersMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self._emitted = False

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True
        settings = dict(self.definition.get("config", {}))
        values = dict(settings.get("values", {}))
        if not values:
            values = {
                "remaining_work_weight": 1.0,
                "processing_time_weight": -1.0,
                "machine_ready_weight": -0.1,
                "job_ready_weight": -0.1,
            }
        return [
            {
                "candidate_id": f"fixed-rule-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": values,
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "fixed_rule_parameters",
                },
            }
        ]

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        return None
