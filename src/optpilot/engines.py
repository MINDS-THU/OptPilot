"""Reference engines used for smoke tests and examples.

Production optimization engines should be supplied by users through the
``python:module:Class`` component hook, not implemented as OptPilot core logic.
"""

from __future__ import annotations

import random
import uuid
from typing import Any, Dict, List


class ReferenceRandomSearchEngine:
    def __init__(self, definition: Dict[str, Any], study_spec, rng: random.Random):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self.best_observation = None

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        search_space = self.definition.get("config", {}).get("searchSpace")
        if not search_space:
            raise ValueError("ReferenceRandomSearchEngine requires config.searchSpace")
        artifact_kind = self.study_spec.primary_artifact.get("kind", "parameter_spec")
        candidates = []
        for _ in range(n_candidates):
            spec = {name: self._sample_parameter(param_def) for name, param_def in search_space.items()}
            candidates.append(
                {
                    "artifact_id": f"artifact-{uuid.uuid4().hex[:12]}",
                    "artifact_kind": artifact_kind,
                    "spec": spec,
                    "lineage": {"parents": []},
                    "generator_record": {"engine_id": self.definition["id"], "strategy": "random_search"},
                    "validation_rules": dict(self.study_spec.primary_artifact.get("validationRules", {})),
                    "materialization_plan": dict(self.study_spec.primary_artifact.get("materializationPlan", {})),
                }
            )
        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        if observations:
            self.best_observation = observations[0]

    def _sample_parameter(self, definition: Dict[str, Any]) -> Any:
        param_type = definition.get("type", "float")
        if param_type == "float":
            return self.rng.uniform(definition["min"], definition["max"])
        if param_type == "int":
            return self.rng.randint(definition["min"], definition["max"])
        if param_type == "categorical":
            return self.rng.choice(definition["values"])
        raise ValueError(f"Unsupported parameter type: {param_type}")


RandomSearchEngine = ReferenceRandomSearchEngine
