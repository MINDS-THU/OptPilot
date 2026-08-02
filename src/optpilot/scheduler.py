"""Trial scheduling layer for execution backends."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, List

from .models import AttemptResult, LogicalTrialResult, Observation, TrialSpec, utc_now_iso


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

    def run_batch(self, trial_specs: List[TrialSpec]) -> List[LogicalTrialResult]:
        handles: List[Dict[str, Any]] = []
        for index, trial_spec in enumerate(trial_specs):
            try:
                handle = self.backend.submit(trial_spec)
            except Exception as exc:
                return self._abort_partial_submission(
                    trial_specs,
                    handles,
                    failed_index=index,
                    error=exc,
                )
            handles.append(_handle_record(handle, trial_spec))
        self._record_batch_submitted(trial_specs, handles)
        return self.collect_batch(handles)

    def _record_batch_submitted(
        self,
        trial_specs: List[TrialSpec],
        handles: List[Dict[str, Any]],
    ) -> None:
        self.evidence_store.record_scheduler_event(
            {
                "event": "batch_submitted",
                "scheduler": self.identity,
                "trial_count": len(trial_specs),
                "handles": [_public_handle_record(handle) for handle in handles],
                "created_at": utc_now_iso(),
            }
        )

    def collect_batch(self, handles: List[Dict[str, Any]]) -> List[LogicalTrialResult]:
        results: List[LogicalTrialResult] = []
        handle_records: List[Dict[str, Any]] = []
        for handle_record in handles:
            result, final_record = self._collect_with_retries(handle_record)
            results.append(result)
            handle_records.append(final_record)
        self._record_batch_collected(results, handle_records)
        return results

    def _collect_with_retries(self, handle_record: Dict[str, Any]):
        retry_policy = _retry_policy(self.definition)
        attempts: List[AttemptResult] = []
        current_record = dict(handle_record)
        for attempt_index in range(1, retry_policy["max_attempts"] + 1):
            handle = current_record["handle"]
            collected: List[Observation] = []
            attempt_error: Dict[str, Any] = {}
            try:
                collected_value = self.backend.collect(handle)
                if not isinstance(collected_value, list):
                    raise TypeError("Execution backend collect() must return a list of Observation values.")
                collected = list(collected_value)
            except Exception as exc:
                attempt_error = _scheduler_error("collection", "backend_collect_failed", exc)
                cancel_error = self._cancel_handle(handle)
                if cancel_error:
                    attempt_error["cancel_error"] = cancel_error
            status = self._safe_status(handle)
            attempt = AttemptResult(
                attempt_index=attempt_index,
                trial_id=current_record["trial_id"],
                handle=handle,
                state=status.get("state"),
                observations=list(collected),
                worker=dict(status.get("worker") or {}),
                error=attempt_error,
            )
            attempts.append(attempt)
            should_retry = bool(attempt_error) or _should_retry(collected, retry_policy)
            if attempt_index >= retry_policy["max_attempts"] or not should_retry:
                attempt_record = attempt.to_event_dict()
                attempt_record["final"] = True
                return LogicalTrialResult(
                    logical_trial_id=handle_record["trial_id"],
                    candidate_id=handle_record["candidate_id"],
                    attempts=attempts,
                    error=dict(attempt_error),
                ), {
                    "handle": handle,
                    "trial_id": handle_record["trial_id"],
                    "state": status.get("state"),
                    "observation_count": len(collected),
                    "attempt_count": attempt_index,
                    "attempts": [item.to_event_dict() for item in attempts[:-1]]
                    + [attempt_record],
                    "worker": dict(status.get("worker") or {}),
                }
            if retry_policy["delay_seconds"] > 0:
                time.sleep(retry_policy["delay_seconds"])
            next_trial_spec = _retry_trial_spec(
                current_record["trial_spec"],
                attempt_index + 1,
            )
            try:
                next_handle = self.backend.submit(next_trial_spec)
            except Exception as exc:
                error = _scheduler_error("retry_dispatch", "retry_dispatch_failed", exc)
                return LogicalTrialResult(
                    logical_trial_id=handle_record["trial_id"],
                    candidate_id=handle_record["candidate_id"],
                    attempts=attempts,
                    error=error,
                ), {
                    "handle": handle,
                    "trial_id": handle_record["trial_id"],
                    "state": status.get("state"),
                    "observation_count": len(collected),
                    "attempt_count": attempt_index,
                    "attempts": [item.to_event_dict() for item in attempts],
                    "worker": dict(status.get("worker") or {}),
                    "error": error,
                }
            current_record = _handle_record(next_handle, next_trial_spec)
            self.evidence_store.record_scheduler_event(
                {
                    "event": "trial_retried",
                    "scheduler": self.identity,
                    "previous_attempt": attempt.to_event_dict(),
                    "next_handle": next_handle,
                    "next_trial_id": next_trial_spec.trial_id,
                    "created_at": utc_now_iso(),
                }
            )
        return LogicalTrialResult(
            logical_trial_id=handle_record["trial_id"],
            candidate_id=handle_record["candidate_id"],
            attempts=attempts,
            error={"phase": "collection", "code": "scheduler_exhausted", "message": "Scheduler exhausted retry loop."},
        ), attempts[-1].to_event_dict()

    def _abort_partial_submission(
        self,
        trial_specs: List[TrialSpec],
        handles: List[Dict[str, Any]],
        *,
        failed_index: int,
        error: Exception,
    ) -> List[LogicalTrialResult]:
        dispatch_error = _scheduler_error("dispatch", "backend_submit_failed", error)
        self.evidence_store.record_scheduler_event(
            {
                "event": "batch_submission_failed",
                "scheduler": self.identity,
                "trial_count": len(trial_specs),
                "submitted_count": len(handles),
                "failed_index": failed_index,
                "failed_trial_id": trial_specs[failed_index].trial_id,
                "handles": [_public_handle_record(handle) for handle in handles],
                "error": dispatch_error,
                "created_at": utc_now_iso(),
            }
        )

        results_by_trial: Dict[str, LogicalTrialResult] = {}
        handle_records: List[Dict[str, Any]] = []
        for handle_record in handles:
            result, final_record = self._cancel_and_drain(handle_record, dispatch_error)
            results_by_trial[result.logical_trial_id] = result
            handle_records.append(final_record)

        for index, trial_spec in enumerate(trial_specs[len(handles) :], start=len(handles)):
            result_error = dict(dispatch_error)
            if index != failed_index:
                result_error = {
                    "phase": "dispatch",
                    "type": "BatchAborted",
                    "code": "batch_aborted_before_dispatch",
                    "message": "Batch dispatch stopped after an earlier backend submission failed.",
                    "failed_trial_id": trial_specs[failed_index].trial_id,
                }
            results_by_trial[trial_spec.trial_id] = LogicalTrialResult(
                logical_trial_id=trial_spec.trial_id,
                candidate_id=trial_spec.candidate["candidate_id"],
                attempts=[],
                error=result_error,
            )

        results = [results_by_trial[trial_spec.trial_id] for trial_spec in trial_specs]
        self._record_batch_collected(results, handle_records, aborted=True)
        return results

    def _cancel_and_drain(
        self,
        handle_record: Dict[str, Any],
        dispatch_error: Dict[str, Any],
    ):
        handle = handle_record["handle"]
        cancel_error = self._cancel_handle(handle)
        collected: List[Observation] = []
        collect_error: Dict[str, Any] = {}
        try:
            collected_value = self.backend.collect(handle)
            if not isinstance(collected_value, list):
                raise TypeError("Execution backend collect() must return a list of Observation values.")
            collected = list(collected_value)
        except Exception as exc:
            collect_error = _scheduler_error("cancel_drain", "backend_drain_failed", exc)
        status = self._safe_status(handle)
        attempt_error = dict(collect_error)
        if cancel_error:
            attempt_error["cancel_error"] = cancel_error
        attempt = AttemptResult(
            attempt_index=1,
            trial_id=handle_record["trial_id"],
            handle=handle,
            state=status.get("state"),
            observations=collected,
            worker=dict(status.get("worker") or {}),
            error=attempt_error,
        )
        result_error = {
            "phase": "dispatch",
            "type": "BatchAborted",
            "code": "batch_aborted_after_partial_dispatch",
            "message": "Submitted work was cancelled and drained after another batch submission failed.",
            "dispatch_error": dict(dispatch_error),
        }
        result = LogicalTrialResult(
            logical_trial_id=handle_record["trial_id"],
            candidate_id=handle_record["candidate_id"],
            attempts=[attempt],
            error=result_error,
        )
        final_record = {
            "handle": handle,
            "trial_id": handle_record["trial_id"],
            "state": status.get("state"),
            "observation_count": len(collected),
            "attempt_count": 1,
            "attempts": [attempt.to_event_dict()],
            "worker": dict(status.get("worker") or {}),
            "error": result_error,
        }
        return result, final_record

    def _cancel_handle(self, handle: Any) -> Dict[str, Any]:
        try:
            self.backend.cancel(handle)
        except Exception as exc:
            return _scheduler_error("cancellation", "backend_cancel_failed", exc)
        return {}

    def _safe_status(self, handle: Any) -> Dict[str, Any]:
        try:
            status = self.backend.status(handle)
            if not isinstance(status, dict):
                raise TypeError("Execution backend status() must return a mapping.")
            return dict(status)
        except Exception as exc:
            return {
                "state": "unknown",
                "worker": {},
                "status_error": _scheduler_error("status", "backend_status_failed", exc),
            }

    def _record_batch_collected(
        self,
        results: List[LogicalTrialResult],
        handle_records: List[Dict[str, Any]],
        *,
        aborted: bool = False,
    ) -> None:
        self.evidence_store.record_scheduler_event(
            {
                "event": "batch_collected",
                "scheduler": self.identity,
                "trial_count": len(results),
                "attempt_count": sum(result.attempt_count for result in results),
                "observation_count": sum(result.observation_count for result in results),
                "final_observation_count": sum(len(result.final_observations) for result in results),
                "aborted": aborted,
                "handles": handle_records,
                "logical_results": [_logical_result_event(result) for result in results],
                "created_at": utc_now_iso(),
            }
        )

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


def _handle_record(handle: Any, trial_spec: TrialSpec) -> Dict[str, Any]:
    return {
        "handle": handle,
        "trial_id": trial_spec.trial_id,
        "method_id": trial_spec.method_id,
        "candidate_id": trial_spec.candidate["candidate_id"],
        "trial_spec": trial_spec,
    }


def _scheduler_error(phase: str, code: str, error: Exception) -> Dict[str, Any]:
    return {
        "phase": phase,
        "type": type(error).__name__,
        "code": code,
        "message": str(error),
    }


def _logical_result_event(result: LogicalTrialResult) -> Dict[str, Any]:
    return {
        "logical_trial_id": result.logical_trial_id,
        "candidate_id": result.candidate_id,
        "attempt_count": result.attempt_count,
        "observation_count": result.observation_count,
        "final_observation_count": len(result.final_observations),
        "error": dict(result.error),
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
        metadata=metadata,
    )
