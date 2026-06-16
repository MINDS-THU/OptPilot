"""Example user-owned method that inspects prior evidence while proposing."""

from __future__ import annotations

from typing import Any, Dict, List

from .fixed_parameter_method import FixedParameterMethod


class EvidenceAwareMethod(FixedParameterMethod):
    def propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
        prior = evidence_view.decision_context()
        artifacts = super().propose(n_candidates, study_state)
        for artifact in artifacts:
            artifact.setdefault("generator_record", {})["prior_observation_count"] = len(
                prior.get("recent_observations", [])
            )
        return artifacts

