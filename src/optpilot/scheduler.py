"""Trial scheduling layer for execution backends."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, List

from .models import Observation, TrialSpec, utc_now_iso


class LocalTrialScheduler:
    """Reference scheduler that submits a batch to one execution backend.

    The scheduler is intentionally small: it owns backend handles, records
    scheduling evidence, and preserves batch ordering. Placement, sandboxing,
    and process supervision remain backend responsibilities.
    """

    def __init__(self, definition: Dict[str, Any], backend, evidence_store):
        self.definition = definition
        self.backend = backend
        self.evidence_store = evidence_store

    def run_batch(self, trial_specs: List[TrialSpec]) -> List[Observation]:
        handles = self.submit_batch(trial_specs)
        return self.collect_batch(handles)

    def submit_batch(self, trial_specs: List[TrialSpec]) -> List[Dict[str, Any]]:
        handles: List[Dict[str, Any]] = []
        for trial_spec in trial_specs:
            handle = self.backend.submit(trial_spec)
            record = {
                "handle": handle,
                "trial_id": trial_spec.trial_id,
                "engine_id": trial_spec.engine_id,
                "artifact_id": trial_spec.artifact["artifact_id"],
                "trial_spec": trial_spec,
            }
            handles.append(record)
        self.evidence_store.record_scheduler_event(
            {
                "event": "batch_submitted",
                "scheduler": self.identity,
                "trial_count": len(trial_specs),
                "handles": [_public_handle_record(handle) for handle in handles],
                "created_at": utc_now_iso(),
            }
        )
        return handles

    def collect_batch(self, handles: List[Dict[str, Any]]) -> List[Observation]:
        observations: List[Observation] = []
        handle_records: List[Dict[str, Any]] = []
        for handle_record in handles:
            final_collected, final_record = self._collect_with_retries(handle_record)
            observations.extend(final_collected)
            handle_records.append(final_record)
        self.evidence_store.record_scheduler_event(
            {
                "event": "batch_collected",
                "scheduler": self.identity,
                "trial_count": len(handles),
                "observation_count": len(observations),
                "handles": handle_records,
                "created_at": utc_now_iso(),
            }
        )
        return observations

    def _collect_with_retries(self, handle_record: Dict[str, Any]):
        retry_policy = _retry_policy(self.definition)
        attempt_records = []
        current_record = dict(handle_record)
        collected: List[Observation] = []
        for attempt_index in range(1, retry_policy["max_attempts"] + 1):
            handle = current_record["handle"]
            collected = self.backend.collect(handle)
            status = self.backend.status(handle)
            attempt_record = {
                "handle": handle,
                "trial_id": current_record["trial_id"],
                "state": status.get("state"),
                "observation_count": len(collected),
                "attempt_index": attempt_index,
                "worker": status.get("worker", {}),
                "statuses": [observation.status for observation in collected],
            }
            attempt_records.append(attempt_record)
            if attempt_index >= retry_policy["max_attempts"] or not _should_retry(collected, retry_policy):
                attempt_record["final"] = True
                return collected, {
                    "handle": handle,
                    "trial_id": current_record["trial_id"],
                    "state": status.get("state"),
                    "observation_count": len(collected),
                    "attempt_count": attempt_index,
                    "attempts": attempt_records,
                    "worker": status.get("worker", {}),
                }
            if retry_policy["delay_seconds"] > 0:
                time.sleep(retry_policy["delay_seconds"])
            next_trial_spec = _retry_trial_spec(
                current_record["trial_spec"],
                attempt_index + 1,
            )
            next_handle = self.backend.submit(next_trial_spec)
            current_record = {
                "handle": next_handle,
                "trial_id": next_trial_spec.trial_id,
                "engine_id": next_trial_spec.engine_id,
                "artifact_id": next_trial_spec.artifact["artifact_id"],
                "trial_spec": next_trial_spec,
            }
            self.evidence_store.record_scheduler_event(
                {
                    "event": "trial_retried",
                    "scheduler": self.identity,
                    "previous_attempt": attempt_record,
                    "next_handle": next_handle,
                    "next_trial_id": next_trial_spec.trial_id,
                    "created_at": utc_now_iso(),
                }
            )
        return collected, attempt_records[-1]

    @property
    def identity(self) -> Dict[str, Any]:
        return {
            "type": self.definition.get("type", "local"),
            "implementation": self.definition.get("implementation", "builtin.local_scheduler"),
            "config": dict(self.definition.get("config", {})),
        }


def _retry_policy(definition: Dict[str, Any]) -> Dict[str, Any]:
    config = definition.get("config", {})
    retry_config = dict(config.get("retryPolicy", {}))
    max_attempts = int(retry_config.get("maxAttempts", retry_config.get("max_attempts", 1)))
    return {
        "max_attempts": max(1, max_attempts),
        "retry_statuses": set(retry_config.get("retryStatuses", retry_config.get("retry_statuses", ["failed", "timeout"]))),
        "delay_seconds": float(retry_config.get("delaySeconds", retry_config.get("delay_seconds", 0.0))),
    }


def _public_handle_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in record.items() if key != "trial_spec"}


def _should_retry(observations: List[Observation], retry_policy: Dict[str, Any]) -> bool:
    if not observations:
        return True
    retry_statuses = retry_policy["retry_statuses"]
    return any(observation.status in retry_statuses for observation in observations)


def _retry_trial_spec(trial_spec: TrialSpec, attempt_index: int) -> TrialSpec:
    metadata = dict(trial_spec.metadata)
    metadata["attempt_index"] = attempt_index
    metadata.setdefault("parent_trial_id", trial_spec.metadata.get("parent_trial_id", trial_spec.trial_id))
    return replace(
        trial_spec,
        trial_id=f"{metadata['parent_trial_id']}-attempt-{attempt_index}",
        metadata=metadata,
    )
