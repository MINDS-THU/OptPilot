"""Seeded random search over the environment's declared parameter schema."""

from __future__ import annotations


class RandomSearch:
    def __init__(self, definition, study_spec, rng):
        self.definition = definition
        self.rng = rng
        candidate = study_spec.candidate if hasattr(study_spec, "candidate") else {}
        context = candidate.get("context", {}) if isinstance(candidate, dict) else {}
        parameters = (
            ((context.get("candidate") or {}).get("parameters") or {}).get("schema")
            or {}
        )
        self.schema = parameters if isinstance(parameters, dict) else {}
        if not self.schema:
            raise ValueError(
                "gallery-random-search needs candidate.parameters.schema in the "
                "environment context."
            )
        self.counter = 0

    def _sample_value(self, declaration):
        value_type = declaration.get("valueType")
        if value_type == "categorical":
            values = list(declaration.get("values") or [])
            return values[self.rng.randrange(len(values))]
        if value_type == "bool":
            return bool(self.rng.getrandbits(1))
        minimum = declaration.get("min")
        maximum = declaration.get("max")
        if value_type == "int":
            low = int(minimum if minimum is not None else 0)
            high = int(maximum if maximum is not None else low + 100)
            return self.rng.randint(low, high)
        low = float(minimum if minimum is not None else 0.0)
        high = float(maximum if maximum is not None else low + 1.0)
        return low + (high - low) * self.rng.random()

    def propose(self, n_candidates, study_state, evidence_view=None):
        proposals = []
        for _ in range(max(0, int(n_candidates))):
            self.counter += 1
            spec = {
                name: self._sample_value(declaration)
                for name, declaration in self.schema.items()
                if isinstance(declaration, dict)
            }
            proposals.append(
                {
                    "candidate_id": f"random-{self.counter}",
                    "format": "parameters",
                    "spec": spec,
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
