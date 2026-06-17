"""Local evidence storage for study runs."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List

from .models import utc_now_iso


class LocalEvidenceStore:
    def __init__(self, root_dir: Path, study_name: str, run_dir: Path = None):
        if run_dir is None:
            safe_name = study_name.replace(" ", "-")
            timestamp = utc_now_iso().replace(":", "-")
            self.run_dir = (root_dir / f"{safe_name}-{timestamp}").resolve()
            self.run_dir.mkdir(parents=True, exist_ok=False)
        else:
            self.run_dir = Path(run_dir).resolve()
            self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        (self.run_dir / "artifacts").mkdir(exist_ok=True)
        (self.run_dir / "candidates").mkdir(exist_ok=True)
        (self.run_dir / "trials").mkdir(exist_ok=True)

    @classmethod
    def open_run_dir(cls, run_dir: Path) -> "LocalEvidenceStore":
        return cls(Path(run_dir).parent, Path(run_dir).name, run_dir=Path(run_dir))

    def write_spec(self, spec: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "study_spec.json", spec)

    def write_run_policy(self, policy: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "run_policy.json", policy)

    def write_environment_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "environment_snapshot.json", snapshot)

    def write_run_lineage(self, lineage: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "run_lineage.json", lineage)

    def record_method_call(self, call: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "method_calls.jsonl", call)

    def record_scheduler_event(self, event: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "scheduler_events.jsonl", event)

    def record_method_event(self, event: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "method_events.jsonl", event)

    def record_artifact(self, artifact: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "artifacts.jsonl", artifact)

    def record_trial(self, trial: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "trials.jsonl", trial)

    def record_observation(self, observation: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "observations.jsonl", observation)

    def write_summary(self, summary: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "summary.json", summary)

    def create_trial_workspace(self, trial_id: str) -> Path:
        workspace = self.run_dir / "trials" / trial_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def read_method_calls(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "method_calls.jsonl")

    def read_scheduler_events(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "scheduler_events.jsonl")

    def read_method_events(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "method_events.jsonl")

    def read_artifacts(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "artifacts.jsonl")

    def read_trials(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "trials.jsonl")

    def read_observations(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "observations.jsonl")

    def read_summary(self) -> Dict[str, Any]:
        return self._read_json(self.run_dir / "summary.json")

    def read_run_policy(self) -> Dict[str, Any]:
        return self._read_json(self.run_dir / "run_policy.json")

    def read_environment_snapshot(self) -> Dict[str, Any]:
        return self._read_json(self.run_dir / "environment_snapshot.json")

    def read_run_lineage(self) -> Dict[str, Any]:
        return self._read_json(self.run_dir / "run_lineage.json")

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    def _append_jsonl(self, path: Path, payload: Dict[str, Any]) -> None:
        line = json.dumps(payload, sort_keys=True)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")

    def _read_json(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        with self._lock:
            lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line]
