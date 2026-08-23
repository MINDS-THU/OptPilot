"""Small, copyable random-search Method used by the tutorial package."""

from __future__ import annotations


class TutorialRandomSearch:
    def __init__(self, definition, study_spec, rng):
        self.definition = definition
        self.rng = rng
        candidate = study_spec.candidate if hasattr(study_spec, "candidate") else {}
        context = candidate.get("context", {}) if isinstance(candidate, dict) else {}
        schema = (((context.get("candidate") or {}).get("parameters") or {}).get("schema") or {})
        if not isinstance(schema, dict) or not schema:
            raise ValueError("The tutorial Method needs candidate.parameters.schema.")
        self.schema = schema
        self.counter = 0

    def _sample(self, declaration):
        value_type = declaration.get("valueType")
        if value_type == "categorical":
            values = list(declaration.get("values") or [])
            return values[self.rng.randrange(len(values))]
        if value_type == "bool":
            return bool(self.rng.getrandbits(1))
        if value_type == "int":
            return self.rng.randint(int(declaration["min"]), int(declaration["max"]))
        low = float(declaration["min"])
        high = float(declaration["max"])
        return low + (high - low) * self.rng.random()

    def propose(self, n_candidates, study_state, evidence_view=None):
        proposals = []
        for _ in range(max(0, int(n_candidates))):
            self.counter += 1
            proposals.append(
                {
                    "candidate_id": f"tutorial-{self.counter}",
                    "format": "parameters",
                    "spec": {
                        name: self._sample(declaration)
                        for name, declaration in self.schema.items()
                    },
                    "lineage": {"parents": []},
                    "generator": {
                        "method_id": self.definition["id"],
                        "strategy": "uniform-random",
                    },
                }
            )
        return proposals

    def observe(self, observations):
        return None
