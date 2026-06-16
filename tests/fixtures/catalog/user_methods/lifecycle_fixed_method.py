"""Example user-owned lifecycle method.

This demonstrates the long-running method shape without putting optimization
logic into OptPilot core.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List


class LifecycleFixedParameterMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.candidates = list(definition.get("config", {}).get("candidates", []))
        self._runs: Dict[str, Dict[str, Any]] = {}
        self.observed: List[Dict[str, Any]] = []
        if not self.candidates:
            raise ValueError("LifecycleFixedParameterMethod requires config.candidates.")

    def start(self, method_input: Dict[str, Any]) -> str:
        handle = f"method-run-{uuid.uuid4().hex[:12]}"
        n_candidates = int(method_input.get("n_candidates", 1))
        self._runs[handle] = {
            "state": "running",
            "poll_count": 0,
            "n_candidates": n_candidates,
            "study_state": dict(method_input.get("study_state", {})),
        }
        return handle

    def poll(self, handle: str) -> Dict[str, Any]:
        run = self._runs[handle]
        run["poll_count"] += 1
        run["state"] = "completed"
        return {
            "state": run["state"],
            "poll_count": run["poll_count"],
            "candidate_count": run["n_candidates"],
        }

    def intervene(self, handle: str, action: Dict[str, Any]) -> None:
        if action.get("type") == "observations":
            self.observed.extend(action.get("observations", []))

    def finalize(self, handle: str) -> Dict[str, Any]:
        run = self._runs[handle]
        artifact_kind = self.study_spec.primary_artifact.get("kind", "parameter_spec")
        artifacts = []
        for index in range(run["n_candidates"]):
            spec = dict(self.candidates[index % len(self.candidates)])
            artifacts.append(
                {
                    "artifact_id": f"lifecycle-artifact-{uuid.uuid4().hex[:12]}",
                    "artifact_kind": artifact_kind,
                    "spec": spec,
                    "lineage": {"parents": [], "source": "tests.fixtures.catalog.user_methods.lifecycle_fixed_method"},
                    "generator_record": {
                        "method_id": self.definition["id"],
                        "strategy": "lifecycle_fixed_parameter_user_method",
                        "owned_by": "user",
                        "handle": handle,
                    },
                }
            )
        return {"artifacts": artifacts}

