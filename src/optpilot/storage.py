"""Local evidence storage for study runs."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .models import utc_now_iso


class LocalEvidenceStore:
    def __init__(self, root_dir: Path, study_name: str, run_dir: Path = None):
        if run_dir is None:
            root = Path(root_dir).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_study_slug(study_name)
            timestamp = utc_now_iso().replace(":", "-")
            self.run_dir = root / f"{safe_name}-{timestamp}"
            self.run_dir.mkdir(parents=True, exist_ok=False)
        else:
            self.run_dir = Path(run_dir).resolve()
            self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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

    def record_controller_event(self, event: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "controller_events.jsonl", event)

    def record_candidate(self, candidate: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "candidates.jsonl", candidate)

    def record_trial(self, trial: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "trials.jsonl", trial)

    def record_observation(self, observation: Dict[str, Any]) -> None:
        self._append_jsonl(self.run_dir / "observations.jsonl", observation)

    def write_summary(self, summary: Dict[str, Any]) -> None:
        self._write_json(self.run_dir / "summary.json", summary)

    def create_trial_workspace(self, trial_id: str, *, attempt_index: int | None = None) -> Path:
        trial_key = _safe_internal_path_key(trial_id, "trial_id")
        if attempt_index is None:
            workspace = self.run_dir / "trials" / trial_key
        else:
            if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index <= 0:
                raise ValueError("attempt_index must be a positive integer.")
            workspace = self.run_dir / "trials" / trial_key / f"attempt-{attempt_index}"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def read_method_calls(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "method_calls.jsonl")

    def read_scheduler_events(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "scheduler_events.jsonl")

    def read_method_events(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "method_events.jsonl")

    def read_controller_events(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "controller_events.jsonl")

    def read_candidates(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.run_dir / "candidates.jsonl")

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
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self._lock:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some supported filesystems/platforms do not allow directory fsync;
        # os.replace still provides atomic visibility there.
        pass
    finally:
        os.close(descriptor)


_INTERNAL_PATH_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_PATH_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _safe_internal_path_key(value: str, label: str) -> str:
    reserved = (
        isinstance(value, str)
        and value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_PATH_STEMS
    )
    if (
        not isinstance(value, str)
        or not _INTERNAL_PATH_KEY.fullmatch(value)
        or value in {".", ".."}
        or value.endswith((" ", "."))
        or reserved
    ):
        raise ValueError(
            f"{label} is not a safe internal path key; expected 1-128 ASCII letters, digits, '.', '_', or '-'."
        )
    return value


def _safe_study_slug(study_name: str) -> str:
    if not isinstance(study_name, str):
        raise TypeError("study_name must be a string.")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", study_name).strip("-.")[:80]
    return slug or "study"
