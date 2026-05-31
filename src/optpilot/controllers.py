"""Built-in study controllers."""

from __future__ import annotations

from typing import Any, Dict, List

from .models import ControllerDecision


class SingleEngineController:
    def __init__(self, definition: Dict[str, Any], study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def decide(self, study_state: Dict[str, Any], engines: List[Dict[str, Any]], evidence_view=None) -> ControllerDecision:
        configured_engine = self.definition.get("config", {}).get("engineId")
        if configured_engine:
            engine_id = configured_engine
        else:
            engine_id = engines[0]["id"]
        engine_def = next(engine for engine in engines if engine["id"] == engine_id)
        batch_size = int(
            engine_def.get("config", {}).get(
                "batchSize",
                self.study_spec.candidate_parallelism,
            )
        )
        return ControllerDecision(
            engine_id=engine_id,
            batch_size=max(1, batch_size),
            reason="single-engine controller selected active engine",
            metadata={
                "controller_id": self.definition["id"],
                "evidence_context": evidence_view.decision_context() if evidence_view else {},
            },
        )
