"""Deterministic weighted dispatch-rule tuning method."""

from __future__ import annotations

import itertools
import uuid
from typing import Any, Dict, Iterable, List


JsonDict = Dict[str, Any]


class TuneDispatchWeightsMethod:
    """Propose a small deterministic grid of dispatch-rule weight settings."""

    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.settings = dict(definition.get("config", {}))
        self._candidates = self._build_candidates()
        self._index = 0

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if n_candidates <= 0 or self._index >= len(self._candidates):
            return []
        batch = self._candidates[self._index : self._index + n_candidates]
        self._index += len(batch)
        return [
            {
                "candidate_id": f"dispatch-weights-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": values,
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "deterministic_dispatch_weight_grid",
                    "candidate_index": self._index - len(batch) + offset,
                },
                "metadata": {"summary": "Weighted dispatch-rule parameters from a deterministic grid."},
            }
            for offset, values in enumerate(batch)
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None

    def _build_candidates(self) -> List[JsonDict]:
        names = [
            "remaining_work_weight",
            "processing_time_weight",
            "machine_ready_weight",
            "job_ready_weight",
        ]
        defaults = {
            "remaining_work_weight": 1.0,
            "processing_time_weight": -1.0,
            "machine_ready_weight": -0.1,
            "job_ready_weight": -0.1,
        }
        candidates: List[JsonDict] = []
        seen = set()

        for values in itertools.chain([defaults], self._configured_candidates(names)):
            bounded = {name: self._bounded_value(name, values.get(name, defaults[name])) for name in names}
            key = tuple(bounded[name] for name in names)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(bounded)
            if len(candidates) >= int(self.settings.get("maxCandidates", 12)):
                return candidates

        configured_grid = self.settings.get("grid", {})
        if isinstance(configured_grid, dict) and configured_grid:
            grid = {name: self._bounded_values(name, configured_grid.get(name, [defaults[name]])) for name in names}
        else:
            grid = {
                "remaining_work_weight": self._bounded_values("remaining_work_weight", [0.0, 1.0, 2.0]),
                "processing_time_weight": self._bounded_values("processing_time_weight", [-2.0, -1.0, 0.0]),
                "machine_ready_weight": self._bounded_values("machine_ready_weight", [-0.5, -0.1, 0.0]),
                "job_ready_weight": self._bounded_values("job_ready_weight", [-0.5, -0.1, 0.0]),
            }

        for values_tuple in itertools.product(*(grid[name] for name in names)):
            values = {name: float(value) for name, value in zip(names, values_tuple)}
            key = tuple(values[name] for name in names)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(values)
            if len(candidates) >= int(self.settings.get("maxCandidates", 12)):
                break
        return candidates

    def _configured_candidates(self, names: List[str]) -> List[JsonDict]:
        configured = self.settings.get("candidates", [])
        if not isinstance(configured, list):
            return []
        candidates = []
        for item in configured:
            if not isinstance(item, dict):
                continue
            candidates.append({name: float(item[name]) for name in names if name in item})
        return candidates

    def _bounded_values(self, name: str, raw_values: Iterable[Any]) -> List[float]:
        return [self._bounded_value(name, raw_value) for raw_value in raw_values if self._in_bounds(name, raw_value)] or [
            self._bounded_value(name, self._search_space().get(name, {}).get("default", 0.0))
        ]

    def _bounded_value(self, name: str, raw_value: Any) -> float:
        value = float(raw_value)
        definition = self._search_space().get(name, {})
        low = float(definition.get("min", "-inf"))
        high = float(definition.get("max", "inf"))
        return min(max(value, low), high)

    def _in_bounds(self, name: str, raw_value: Any) -> bool:
        search_space = self._search_space()
        definition = search_space.get(name, {})
        low = float(definition.get("min", "-inf"))
        high = float(definition.get("max", "inf"))
        value = float(raw_value)
        return low <= value <= high

    def _search_space(self) -> JsonDict:
        search_space = self.settings.get("searchSpace", {})
        if isinstance(search_space, dict) and search_space:
            return search_space
        candidate = getattr(self.study_spec, "candidate", {}) or {}
        return dict(candidate.get("context", {}).get("candidate", {}).get("parameters", {}).get("schema", {}))
