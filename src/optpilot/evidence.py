"""Evidence query views exposed to controllers and engines."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


JsonDict = Dict[str, Any]


@dataclass
class EvidenceSummary:
    completed_trials: int
    observation_count: int
    artifact_count: int
    decision_count: int
    scheduler_event_count: int
    engine_snapshot_count: int
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

    def controller_decisions(self, limit: Optional[int] = None) -> List[JsonDict]:
        return _limit_tail(self.store.read_controller_decisions(), limit)

    def scheduler_events(self, limit: Optional[int] = None) -> List[JsonDict]:
        if not hasattr(self.store, "read_scheduler_events"):
            return []
        return _limit_tail(self.store.read_scheduler_events(), limit)

    def engine_snapshots(
        self,
        limit: Optional[int] = None,
        engine_id: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[JsonDict]:
        if not hasattr(self.store, "read_engine_snapshots"):
            return []
        snapshots = self.store.read_engine_snapshots()
        if engine_id is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.get("engine_id") == engine_id]
        if event is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.get("event") == event]
        return _limit_tail(snapshots, limit)

    def query_events(
        self,
        event_types: Optional[Sequence[str] | str] = None,
        *,
        limit: Optional[int] = None,
        status: Optional[str] = None,
        trial_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        engine_id: Optional[str] = None,
        event: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[JsonDict]:
        """Query normalized evidence records across local event streams.

        This is intentionally a small read API: it gives controllers and
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
                    engine_id=engine_id,
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
        decisions = self.store.read_controller_decisions()
        scheduler_events = self.scheduler_events()
        engine_snapshots = self.engine_snapshots()
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
            decision_count=len(decisions),
            scheduler_event_count=len(scheduler_events),
            engine_snapshot_count=len(engine_snapshots),
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
        if event_type == "controller_decision":
            return self.store.read_controller_decisions()
        if event_type == "scheduler_event":
            return self.scheduler_events()
        if event_type == "engine_snapshot":
            return self.engine_snapshots()
        raise ValueError(f"Unsupported evidence event type: {event_type!r}")


def _limit_tail(items: List[JsonDict], limit: Optional[int]) -> List[JsonDict]:
    if limit is None:
        return items
    if limit <= 0:
        return []
    return items[-limit:]


EVENT_TYPE_ALIASES = {
    "observations": "observation",
    "trials": "trial",
    "artifacts": "artifact",
    "controller_decisions": "controller_decision",
    "decisions": "controller_decision",
    "scheduler_events": "scheduler_event",
    "engine_snapshots": "engine_snapshot",
}
EVENT_TYPES = [
    "observation",
    "trial",
    "artifact",
    "controller_decision",
    "scheduler_event",
    "engine_snapshot",
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
        "engine_id": payload.get("engine_id"),
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
    engine_id: Optional[str],
    event: Optional[str],
) -> bool:
    if status is not None and item.get("status") != status:
        return False
    if trial_id is not None and item.get("trial_id") != trial_id:
        return False
    if artifact_id is not None and item.get("artifact_id") != artifact_id:
        return False
    if engine_id is not None and item.get("engine_id") != engine_id:
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
