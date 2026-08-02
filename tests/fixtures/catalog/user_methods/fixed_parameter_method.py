"""Example user-owned method implementation.

This lives outside ``src/optpilot`` on purpose: OptPilot owns the interface and
execution/evidence protocol; users own the optimization algorithm.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List


class FixedParameterMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.candidates = list(definition.get("config", {}).get("candidates", []))
        self._cursor = 0
        self.observed: List[Dict[str, Any]] = []
        if not self.candidates:
            raise ValueError("FixedParameterMethod requires config.candidates.")

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidate_format = self.study_spec.candidate.get("format", "parameters")
        proposed = []
        for _ in range(n_candidates):
            spec = dict(self.candidates[self._cursor % len(self.candidates)])
            self._cursor += 1
            proposed.append(
                {
                    "candidate_id": f"user-candidate-{uuid.uuid4().hex[:12]}",
                    "format": candidate_format,
                    "spec": spec,
                    "lineage": {"parents": [], "source": "tests.fixtures.catalog.user_methods"},
                    "generator": {
                        "method_id": self.definition["id"],
                        "strategy": "fixed_parameter_user_method",
                        "owned_by": "user",
                    },
                }
            )
        return proposed

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        self.observed.extend(observations)
