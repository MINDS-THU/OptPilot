"""Runtime adapters for user-owned engine implementations."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .models import utc_now_iso


TERMINAL_STATES = {"completed", "failed", "finished", "succeeded", "cancelled"}
SUCCESS_STATES = {"completed", "finished", "succeeded"}


class EngineRuntime:
    """Normalizes synchronous and lifecycle engine implementations.

    OptPilot keeps optimization algorithms user-owned. This wrapper only adapts
    supported engine shapes into the runner's batch proposal/observation flow
    and records engine lifecycle evidence.
    """

    def __init__(self, definition: Dict[str, Any], engine, evidence_store, study_spec):
        self.definition = definition
        self.engine = engine
        self.evidence_store = evidence_store
        self.study_spec = study_spec
        self.engine_id = definition["id"]

    def propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view=None) -> List[Dict[str, Any]]:
        if hasattr(self.engine, "start") and hasattr(self.engine, "poll") and hasattr(self.engine, "finalize"):
            return self._lifecycle_propose(n_candidates, study_state, evidence_view)
        if hasattr(self.engine, "propose"):
            candidates = self.engine.propose(n_candidates, study_state)
            self._record_snapshot(
                "proposed",
                {
                    "interface": "propose_observe",
                    "candidate_count": len(candidates),
                    "study_state": dict(study_state),
                },
            )
            return candidates
        raise TypeError(
            f"Engine {self.engine_id!r} must implement either propose/observe or start/poll/finalize."
        )

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        if hasattr(self.engine, "observe"):
            self.engine.observe(observations)
        elif hasattr(self.engine, "intervene"):
            self.engine.intervene(
                "__latest__",
                {
                    "type": "observations",
                    "observations": observations,
                },
            )
        self._record_snapshot(
            "observed",
            {
                "observation_count": len(observations),
                "statuses": [observation.get("status") for observation in observations],
            },
        )

    def _lifecycle_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
        config = self.definition.get("config", {})
        max_polls = int(config.get("maxPolls", 100))
        poll_interval_seconds = float(config.get("pollIntervalSeconds", 0.0))
        engine_input = {
            "engine_id": self.engine_id,
            "engine_definition": dict(self.definition),
            "study_state": dict(study_state),
            "study_spec": dict(self.study_spec.raw),
            "n_candidates": n_candidates,
            "evidence_context": evidence_view.decision_context() if evidence_view else {},
            "runtime_context": dict(study_state.get("runtime_context", {})),
        }
        handle = self.engine.start(engine_input)
        self._record_snapshot(
            "started",
            {
                "interface": "lifecycle",
                "handle": handle,
                "n_candidates": n_candidates,
                "study_state": dict(study_state),
            },
        )

        last_status: Dict[str, Any] = {}
        for poll_index in range(max_polls):
            last_status = self.engine.poll(handle) or {}
            self._record_snapshot(
                "polled",
                {
                    "handle": handle,
                    "poll_index": poll_index,
                    "status": dict(last_status),
                },
            )
            state = _status_state(last_status)
            if state in TERMINAL_STATES:
                break
            if poll_interval_seconds > 0:
                time.sleep(poll_interval_seconds)
        else:
            raise TimeoutError(
                f"Engine {self.engine_id!r} did not reach a terminal state after {max_polls} polls."
            )

        state = _status_state(last_status)
        if state not in SUCCESS_STATES:
            raise RuntimeError(f"Engine {self.engine_id!r} ended with state {state!r}.")

        result = self.engine.finalize(handle)
        candidates = _extract_candidate_artifacts(result)
        self._record_snapshot(
            "finalized",
            {
                "handle": handle,
                "candidate_count": len(candidates),
                "status": dict(last_status),
            },
        )
        return candidates

    def _record_snapshot(self, event: str, payload: Dict[str, Any]) -> None:
        if not hasattr(self.evidence_store, "record_engine_snapshot"):
            return
        self.evidence_store.record_engine_snapshot(
            {
                "engine_id": self.engine_id,
                "event": event,
                "payload": payload,
                "created_at": utc_now_iso(),
            }
        )


def _status_state(status: Dict[str, Any]) -> Optional[str]:
    state = status.get("state") or status.get("status")
    if state is None and status.get("done") is True:
        return "completed"
    return str(state).lower() if state is not None else None


def _extract_candidate_artifacts(result: Any) -> List[Dict[str, Any]]:
    if isinstance(result, dict):
        candidates = result.get("artifacts", result.get("candidates", []))
    else:
        candidates = result
    if candidates is None:
        return []
    if not isinstance(candidates, list):
        raise TypeError("Lifecycle engine finalize() must return a list or a dict containing an artifacts list.")
    return [dict(candidate) for candidate in candidates]
