"""Reference methods used for smoke tests and examples.

Production optimization methods should be supplied by users through the
public ``module:Class`` hook or command method protocol. The built-in method is
intentionally simple and exists to exercise the runner.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List


class ReferenceRandomSearchMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self.observed: List[Dict[str, Any]] = []

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        search_space = self.definition.get("config", {}).get("searchSpace", {})
        candidates = []
        for _ in range(n_candidates):
            spec = {
                name: self._sample_parameter(parameter_def)
                for name, parameter_def in search_space.items()
            }
            candidates.append(
                {
                    "candidate_id": f"candidate-{uuid.uuid4().hex[:12]}",
                    "format": self.study_spec.candidate.get("format", "parameters"),
                    "spec": spec,
                    "lineage": {"parents": []},
                    "generator": {"method_id": self.definition["id"], "strategy": "random_search"},
                }
            )
        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        self.observed.extend(observations)

    def _sample_parameter(self, definition: Dict[str, Any]) -> Any:
        param_type = definition.get("valueType", definition.get("type", "float"))
        if param_type == "int":
            return self.rng.randint(int(definition.get("min", 0)), int(definition.get("max", 10)))
        if param_type == "categorical":
            values = list(definition.get("values", []))
            if not values:
                raise ValueError("categorical parameter requires values")
            return self.rng.choice(values)
        if param_type == "bool":
            return bool(self.rng.getrandbits(1))
        if param_type == "string":
            values = list(definition.get("values", []))
            if values:
                return self.rng.choice(values)
            return str(definition.get("default", ""))
        low = float(definition.get("min", 0.0))
        high = float(definition.get("max", 1.0))
        return self.rng.uniform(low, high)
