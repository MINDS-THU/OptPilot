"""Example user-owned controller that inspects prior evidence."""

from __future__ import annotations

from typing import Any, Dict, List

from optpilot.models import ControllerDecision


class EvidenceAwareController:
    def __init__(self, definition: Dict[str, Any], study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def decide(self, study_state: Dict[str, Any], engines: List[Dict[str, Any]], evidence_view) -> ControllerDecision:
        engine_id = self.definition.get("config", {}).get("engineId") or engines[0]["id"]
        engine_def = next(engine for engine in engines if engine["id"] == engine_id)
        evidence_context = evidence_view.decision_context()
        recent_failure_count = int(evidence_context.get("recent_failure_count", 0))
        configured_batch_size = int(engine_def.get("config", {}).get("batchSize", 1))
        batch_size = 1 if recent_failure_count else configured_batch_size
        return ControllerDecision(
            engine_id=engine_id,
            batch_size=max(1, batch_size),
            reason="evidence-aware controller selected engine using prior observations",
            metadata={
                "controller_id": self.definition["id"],
                "evidence_context": evidence_context,
                "recent_failure_count": recent_failure_count,
            },
        )
