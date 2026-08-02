"""Worker entrypoint for process/container Python method execution."""

from __future__ import annotations

import contextlib
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .evidence import EvidenceView
from .method_runtime import (
    SUCCESS_STATES,
    TERMINAL_STATES,
    MethodSession,
    _call_with_optional_evidence,
    _extract_candidates,
    _status_state,
)
from .registry import resolve_component
from .spec import StudySpec
from .storage import LocalEvidenceStore


class StaticEvidenceView:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = dict(payload)

    def decision_context(self) -> Dict[str, Any]:
        return dict(self.payload)


class PythonMethodWorker:
    def __init__(self, init_payload: Dict[str, Any]):
        self.definition = dict(init_payload["method_definition"])
        self.study_spec = StudySpec(
            path=Path(init_payload["study_spec_path"]).resolve(),
            raw=dict(init_payload["study_spec_raw"]),
        )
        run_dir = init_payload.get("run_dir")
        self.evidence_view = (
            EvidenceView(LocalEvidenceStore.open_run_dir(Path(str(run_dir))), self.study_spec)
            if run_dir
            else StaticEvidenceView({})
        )
        for path in reversed(self.definition.get("implementation", {}).get("pythonPath", []) or []):
            if path and path not in sys.path:
                sys.path.insert(0, str(path))
        seed = int(init_payload.get("seed", 0) or 0)
        method_ref = self.definition.get("implementation", {}).get("callable")
        method_cls = resolve_component("method", str(method_ref))
        self.method = method_cls(self.definition, self.study_spec, random.Random(seed))

    def serve(self) -> int:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if request.get("op") == "shutdown":
                    self._write_response({"ok": True})
                    return 0
                response = self._handle(request)
                self._write_response({"ok": True, **response})
            except Exception as exc:
                self._write_response(
                    {
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
        return 0

    def _handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        op = request.get("op")
        if op == "propose":
            return self._propose(request)
        if op == "observe":
            return self._observe(request)
        raise ValueError(f"Unsupported method worker operation: {op!r}")

    def _propose(self, request: Dict[str, Any]) -> Dict[str, Any]:
        call_records: List[Dict[str, Any]] = []
        method_events: List[Dict[str, Any]] = []
        protocol = self.definition.get("implementation", {}).get("protocol", "optpilot.method.batch.v1")
        n_candidates = int(request.get("n_candidates", 1) or 1)
        study_state = dict(request.get("study_state", {}) or {})
        evidence_view = self.evidence_view

        with contextlib.redirect_stdout(sys.stderr):
            if protocol == "optpilot.method.session.v1":
                candidates = self._session_propose(n_candidates, study_state, evidence_view, call_records, method_events)
            elif protocol == "optpilot.method.batch.v1":
                candidates = self._batch_propose(n_candidates, study_state, evidence_view, call_records)
            else:
                raise NotImplementedError(f"Method protocol {protocol!r} is not implemented.")
        return {
            "candidates": candidates,
            "calls": call_records,
            "method_events": method_events,
        }

    def _batch_propose(
        self,
        n_candidates: int,
        study_state: Dict[str, Any],
        evidence_view: StaticEvidenceView,
        call_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if hasattr(self.method, "start") and hasattr(self.method, "poll") and hasattr(self.method, "finalize"):
            return self._lifecycle_propose(n_candidates, study_state, evidence_view, call_records)
        if not hasattr(self.method, "propose"):
            raise TypeError("Python batch method must implement propose/observe or start/poll/finalize.")
        candidates = _call_with_optional_evidence(self.method.propose, n_candidates, study_state, evidence_view)
        call_records.append(
            {
                "event": "proposed",
                "payload": {
                    "protocol": "optpilot.method.batch.v1",
                    "interface": "propose_observe",
                    "candidate_count": len(candidates),
                    "study_state": dict(study_state),
                    "worker": "python_method_worker",
                },
            }
        )
        return candidates

    def _session_propose(
        self,
        n_candidates: int,
        study_state: Dict[str, Any],
        evidence_view: StaticEvidenceView,
        call_records: List[Dict[str, Any]],
        method_events: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        session = MethodSession(
            method_id=self.definition["id"],
            definition=self.definition,
            study_spec=self.study_spec,
            study_state=study_state,
            evidence_view=evidence_view,
            n_candidates=n_candidates,
            record_event=lambda event: method_events.append(dict(event)),
        )
        if hasattr(self.method, "run"):
            result = self.method.run(session)
        elif callable(self.method):
            result = self.method(session)
        else:
            raise TypeError("Python session method must implement run(session) or be callable.")
        candidates = [*session.candidates, *_extract_candidates(result)]
        call_records.append(
            {
                "event": "completed",
                "payload": {
                    "protocol": "optpilot.method.session.v1",
                    "interface": "session",
                    "candidate_count": len(candidates),
                    "study_state": dict(study_state),
                    "events": len(session.events),
                    "worker": "python_method_worker",
                },
            }
        )
        return candidates

    def _lifecycle_propose(
        self,
        n_candidates: int,
        study_state: Dict[str, Any],
        evidence_view: StaticEvidenceView,
        call_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        config = self.definition.get("config", {})
        max_polls = int(config.get("maxPolls", 100))
        poll_interval_seconds = float(config.get("pollIntervalSeconds", 0.0))
        method_input = {
            "method_id": self.definition["id"],
            "method_definition": dict(self.definition),
            "study_state": dict(study_state),
            "study_spec": dict(self.study_spec.raw),
            "n_candidates": n_candidates,
            "evidence_context": evidence_view.decision_context(),
            "runtime_context": dict(study_state.get("runtime_context", {})),
        }
        handle = self.method.start(method_input)
        call_records.append(
            {
                "event": "started",
                "payload": {
                    "protocol": self.definition.get("implementation", {}).get("protocol", "optpilot.method.batch.v1"),
                    "interface": "lifecycle",
                    "handle": handle,
                    "n_candidates": n_candidates,
                    "study_state": dict(study_state),
                    "worker": "python_method_worker",
                },
            }
        )

        last_status: Dict[str, Any] = {}
        for poll_index in range(max_polls):
            last_status = self.method.poll(handle) or {}
            call_records.append(
                {
                    "event": "polled",
                    "payload": {
                        "handle": handle,
                        "poll_index": poll_index,
                        "status": dict(last_status),
                        "worker": "python_method_worker",
                    },
                }
            )
            state = _status_state(last_status)
            if state in TERMINAL_STATES:
                break
            if poll_interval_seconds > 0:
                time.sleep(poll_interval_seconds)
        else:
            raise TimeoutError(f"Method {self.definition['id']!r} did not reach a terminal state after {max_polls} polls.")

        state = _status_state(last_status)
        if state not in SUCCESS_STATES:
            raise RuntimeError(f"Method {self.definition['id']!r} ended with state {state!r}.")

        result = self.method.finalize(handle)
        candidates = _extract_candidates(result)
        call_records.append(
            {
                "event": "finalized",
                "payload": {
                    "handle": handle,
                    "candidate_count": len(candidates),
                    "status": dict(last_status),
                    "worker": "python_method_worker",
                },
            }
        )
        return candidates

    def _observe(self, request: Dict[str, Any]) -> Dict[str, Any]:
        observations = list(request.get("observations", []) or [])
        with contextlib.redirect_stdout(sys.stderr):
            if hasattr(self.method, "observe"):
                self.method.observe(observations)
            elif hasattr(self.method, "intervene"):
                self.method.intervene(
                    "__latest__",
                    {
                        "type": "observations",
                        "observations": observations,
                    },
                )
        return {
            "calls": [
                {
                    "event": "observed",
                    "payload": {
                        "observation_count": len(observations),
                        "statuses": [observation.get("status") for observation in observations],
                        "worker": "python_method_worker",
                    },
                }
            ]
        }

    def _write_response(self, payload: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        raise SystemExit("Usage: python -m optpilot.method_worker INIT_JSON")
    init_path = Path(argv[0]).resolve()
    worker = PythonMethodWorker(json.loads(init_path.read_text(encoding="utf-8")))
    return worker.serve()


if __name__ == "__main__":
    raise SystemExit(main())
