"""Evidence query views exposed to methods and analysis tools."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


JsonDict = Dict[str, Any]


@dataclass
class EvidenceSummary:
    completed_trials: int
    observation_count: int
    artifact_count: int
    method_call_count: int
    scheduler_event_count: int
    method_event_count: int
    status_counts: JsonDict
    best_trial_id: Optional[str]
    best_artifact_id: Optional[str]
    best_metric: Optional[float]
    primary_metric_name: str
    primary_metric_direction: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


class EvidenceView:
    def __init__(self, store, study_spec):
        self.store = store
        self.study_spec = study_spec

    def observations(self, limit: Optional[int] = None, status: Optional[str] = None) -> List[JsonDict]:
        observations = self.store.read_observations()
        if status is not None:
            observations = [observation for observation in observations if observation.get("status") == status]
        return _limit_tail(observations, limit)

    def trials(self, limit: Optional[int] = None, status: Optional[str] = None) -> List[JsonDict]:
        trials = self.store.read_trials()
        if status is not None:
            trials = [trial for trial in trials if trial.get("status") == status]
        return _limit_tail(trials, limit)

    def artifacts(self, limit: Optional[int] = None) -> List[JsonDict]:
        return _limit_tail(self.store.read_artifacts(), limit)

    def record_streams(
        self,
        name: Optional[str] = None,
        *,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        trial_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> List[JsonDict]:
        """Return extracted record stream metadata from observations.

        Configured environments write CSV/JSONL/SQLite extracts as JSONL files
        and attach a recordsToExtract report to each observation. This method
        gives methods and analysis tools a stable way to discover those streams
        without walking observation artifacts by hand.
        """

        streams: List[JsonDict] = []
        for observation_index, observation in enumerate(self.store.read_observations()):
            if status is not None and observation.get("status") != status:
                continue
            if trial_id is not None and observation.get("trial_id") != trial_id:
                continue
            if artifact_id is not None and observation.get("artifact_id") != artifact_id:
                continue
            for stream in _observation_record_streams(observation):
                if name is not None and stream.get("name") != name:
                    continue
                streams.append(
                    {
                        "name": stream.get("name"),
                        "source": stream.get("source"),
                        "path": stream.get("path"),
                        "contentRef": stream.get("contentRef"),
                        "record_count": stream.get("record_count"),
                        "trial_id": observation.get("trial_id"),
                        "artifact_id": observation.get("artifact_id"),
                        "status": observation.get("status"),
                        "observation_index": observation_index,
                    }
                )
        return _limit_tail(streams, limit)

    def records(
        self,
        name: Optional[str] = None,
        *,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        trial_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[JsonDict]:
        """Read rows from extracted record streams.

        Returned items wrap the original row in ``record`` and include stream,
        trial, and artifact provenance. Missing content refs are skipped so a
        method can safely query partial historical evidence.
        """

        rows: List[JsonDict] = []
        for stream in self.record_streams(
            name=name,
            status=status,
            trial_id=trial_id,
            artifact_id=artifact_id,
        ):
            content_ref = stream.get("contentRef")
            if not content_ref:
                continue
            path = _resolve_evidence_path(content_ref, self.store)
            if not path.exists():
                continue
            for row_index, row in enumerate(_read_jsonl_rows(path)):
                rows.append(
                    {
                        "name": stream.get("name"),
                        "source": stream.get("source"),
                        "trial_id": stream.get("trial_id"),
                        "artifact_id": stream.get("artifact_id"),
                        "status": stream.get("status"),
                        "row_index": row_index,
                        "record": row,
                    }
                )
        if newest_first:
            rows.reverse()
            if limit is None:
                return rows
            if limit <= 0:
                return []
            return rows[:limit]
        return _limit_tail(rows, limit)

    def method_calls(self, limit: Optional[int] = None) -> List[JsonDict]:
        return _limit_tail(self.store.read_method_calls(), limit)

    def scheduler_events(self, limit: Optional[int] = None) -> List[JsonDict]:
        if not hasattr(self.store, "read_scheduler_events"):
            return []
        return _limit_tail(self.store.read_scheduler_events(), limit)

    def method_events(
        self,
        limit: Optional[int] = None,
        method_id: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[JsonDict]:
        if not hasattr(self.store, "read_method_events"):
            return []
        events = self.store.read_method_events()
        if method_id is not None:
            events = [item for item in events if item.get("method_id") == method_id]
        if event is not None:
            events = [item for item in events if item.get("event") == event]
        return _limit_tail(events, limit)

    def query_events(
        self,
        event_types: Optional[Sequence[str] | str] = None,
        *,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        trial_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        method_id: Optional[str] = None,
        event: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[JsonDict]:
        """Query normalized evidence records across local event streams.

        This is intentionally a small read API: it gives methods and
        analysis tools one stable shape without hiding the original record.
        """

        selected = _normalize_event_types(event_types)
        records = []
        for event_type in selected:
            for source_index, payload in enumerate(self._read_event_type(event_type)):
                normalized = _normalize_event_record(event_type, payload, source_index)
                if not _matches_event(
                    normalized,
                    status=status,
                    trial_id=trial_id,
                    artifact_id=artifact_id,
                method_id=method_id,
                    event=event,
                ):
                    continue
                records.append(normalized)
        records.sort(key=lambda item: (item.get("created_at") or "", item["event_type"], item["source_index"]))
        if newest_first:
            records.reverse()
        return _limit_tail(records, limit)

    def summary(self) -> EvidenceSummary:
        observations = self.store.read_observations()
        artifacts = self.store.read_artifacts()
        method_calls = self.store.read_method_calls()
        scheduler_events = self.scheduler_events()
        method_events = self.method_events()
        primary_metric = self.study_spec.primary_metric_name
        direction = self.study_spec.primary_metric_direction
        status_counts: JsonDict = {}
        best_metric: Optional[float] = None
        best_trial_id: Optional[str] = None
        best_artifact_id: Optional[str] = None
        for observation in observations:
            status = observation.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            metric_values = observation.get("metric_values", {})
            if primary_metric not in metric_values:
                continue
            metric = float(metric_values[primary_metric])
            if _is_better(metric, best_metric, direction):
                best_metric = metric
                best_trial_id = observation.get("trial_id")
                best_artifact_id = observation.get("artifact_id")
        return EvidenceSummary(
            completed_trials=len(observations),
            observation_count=len(observations),
            artifact_count=len(artifacts),
            method_call_count=len(method_calls),
            scheduler_event_count=len(scheduler_events),
            method_event_count=len(method_events),
            status_counts=status_counts,
            best_trial_id=best_trial_id,
            best_artifact_id=best_artifact_id,
            best_metric=best_metric,
            primary_metric_name=primary_metric,
            primary_metric_direction=direction,
        )

    def decision_context(self) -> JsonDict:
        summary = self.summary()
        recent_failures = [
            observation
            for observation in self.observations(limit=5)
            if observation.get("status") in {"failed", "invalid", "timeout", "partial"}
        ]
        return {
            "summary": summary.to_dict(),
            "recent_failure_count": len(recent_failures),
            "recent_failures": [
                {
                    "trial_id": observation.get("trial_id"),
                    "artifact_id": observation.get("artifact_id"),
                    "status": observation.get("status"),
                    "errors": observation.get("event_summary", {}).get("errors", []),
                }
                for observation in recent_failures
            ],
        }

    def _read_event_type(self, event_type: str) -> List[JsonDict]:
        if event_type == "observation":
            return self.store.read_observations()
        if event_type == "trial":
            return self.store.read_trials()
        if event_type == "artifact":
            return self.store.read_artifacts()
        if event_type == "method_call":
            return self.store.read_method_calls()
        if event_type == "scheduler_event":
            return self.scheduler_events()
        if event_type == "method_event":
            return self.method_events()
        raise ValueError(f"Unsupported evidence event type: {event_type!r}")


def _limit_tail(items: List[JsonDict], limit: Optional[int]) -> List[JsonDict]:
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


def _observation_record_streams(observation: JsonDict) -> List[JsonDict]:
    report = observation.get("event_summary", {}).get("recordsToExtract")
    if isinstance(report, dict) and isinstance(report.get("streams"), list):
        return [dict(stream) for stream in report["streams"] if isinstance(stream, dict)]
    for artifact in observation.get("artifacts", []) or []:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("name") != "records_to_extract_report":
            continue
        path = artifact.get("path")
        if not path:
            continue
        report_path = Path(path)
        if not report_path.exists():
            continue
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if isinstance(streams, list):
            return [dict(stream) for stream in streams if isinstance(stream, dict)]
    return []


def _resolve_evidence_path(value: str, store) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    run_dir = Path(getattr(store, "run_dir", "."))
    return (run_dir / path).resolve()


def _read_jsonl_rows(path: Path) -> List[JsonDict]:
    rows: List[JsonDict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
            else:
                rows.append({"value": item})
    return rows


EVENT_TYPE_ALIASES = {
    "observations": "observation",
    "trials": "trial",
    "artifacts": "artifact",
    "method_calls": "method_call",
    "calls": "method_call",
    "scheduler_events": "scheduler_event",
    "method_events": "method_event",
    "events": "method_event",
}
EVENT_TYPES = [
    "observation",
    "trial",
    "artifact",
    "method_call",
    "scheduler_event",
    "method_event",
]


def _normalize_event_types(event_types: Optional[Sequence[str] | str]) -> List[str]:
    if event_types is None:
        return list(EVENT_TYPES)
    if isinstance(event_types, str):
        raw_values: Iterable[str] = [event_types]
    else:
        raw_values = event_types
    normalized = []
    for value in raw_values:
        event_type = EVENT_TYPE_ALIASES.get(value, value)
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unsupported evidence event type: {value!r}")
        normalized.append(event_type)
    return normalized


def _normalize_event_record(event_type: str, payload: JsonDict, source_index: int) -> JsonDict:
    return {
        "event_type": event_type,
        "source_index": source_index,
        "created_at": payload.get("created_at") or payload.get("finished_at") or payload.get("started_at"),
        "trial_id": payload.get("trial_id"),
        "artifact_id": payload.get("artifact_id"),
        "method_id": payload.get("method_id"),
        "status": payload.get("status"),
        "event": payload.get("event"),
        "record": dict(payload),
    }


def _matches_event(
    item: JsonDict,
    *,
    status: Optional[str],
    trial_id: Optional[str],
    artifact_id: Optional[str],
    method_id: Optional[str],
    event: Optional[str],
) -> bool:
    if status is not None and item.get("status") != status:
        return False
    if trial_id is not None and item.get("trial_id") != trial_id:
        return False
    if artifact_id is not None and item.get("artifact_id") != artifact_id:
        return False
    if method_id is not None and item.get("method_id") != method_id:
        return False
    if event is not None and item.get("event") != event:
        return False
    return True


def _is_better(candidate: float, current: Optional[float], direction: str) -> bool:
    if current is None:
        return True
    if direction == "maximize":
        return candidate > current
    if direction == "minimize":
        return candidate < current
    raise ValueError(f"Unsupported optimization direction: {direction}")
