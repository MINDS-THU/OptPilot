"""Execution backend and evaluator for OptPilot."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from .host_env import compile_host_env_declarations

from .attempt_semantics import (
    PUBLIC_OBSERVATION_STATUSES,
    error_payload as _error_payload,
    exception_evaluation_result as _exception_evaluation_result,
    status_for_exception as _status_for_exception,
    unsupported_observation_status_result as _unsupported_observation_status_result,
    validate_environment_result as _validate_environment_result,
    validation_exception_report as _validation_exception_report,
)
from .candidate_materialization import MaterializationRecord, ValidationReport
from .models import Observation, ResourceProfile, SandboxSpec, TrialSpec, utc_now_iso
from .setup import apply_prepared_env, minimal_host_env


class Evaluator:
    def __init__(self, study_spec, environment_adapter, evidence_store, materializer, candidate_validator):
        self.study_spec = study_spec
        self.environment_adapter = environment_adapter
        self.evidence_store = evidence_store
        self.materializer = materializer
        self.candidate_validator = candidate_validator

    def run_trial(self, trial_spec: TrialSpec) -> List[Observation]:
        parent_trial_id = str(trial_spec.metadata.get("parent_trial_id") or trial_spec.trial_id)
        attempt_index = int(trial_spec.metadata.get("attempt_index", 1) or 1)
        workspace = self.evidence_store.create_trial_workspace(parent_trial_id, attempt_index=attempt_index)
        trial_spec.metadata["trial_workspace"] = str(workspace)
        started = time.monotonic()
        candidate_context = {
            "trial_id": trial_spec.trial_id,
            "parent_trial_id": parent_trial_id,
            "attempt_index": attempt_index,
            "study_id": trial_spec.study_id,
            "workspace": str(workspace),
            "resource_profile": trial_spec.resource_profile.to_dict(),
            "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
            "backend_identity": trial_spec.metadata.get("backend_identity", {}),
            "backend_worker": trial_spec.metadata.get("backend_worker", {}),
        }
        try:
            validation_report = self.candidate_validator.validate(trial_spec.candidate, candidate_context)
        except Exception as exc:
            validation_report = _validation_exception_report(exc)
            materialization_record = MaterializationRecord(runtime_spec={}, metadata={"skipped": True})
            observation = self._failure_observation(
                trial_spec,
                "failed",
                "validation",
                exc,
                time.monotonic() - started,
                materialization_record,
            )
            self._record_candidate(trial_spec, validation_report, materialization_record, error=observation.event_summary["error"])
            self._record_trial(trial_spec, observation.status)
            self.evidence_store.record_observation(observation.to_dict())
            return [observation]

        if not validation_report.accepted:
            materialization_record = MaterializationRecord(runtime_spec={}, metadata={"skipped": True})
            observation = self._invalid_observation(
                trial_spec,
                validation_report,
                time.monotonic() - started,
                materialization_record,
            )
            self._record_candidate(trial_spec, validation_report, materialization_record)
            self._record_trial(trial_spec, observation.status)
            self.evidence_store.record_observation(observation.to_dict())
            return [observation]

        try:
            materialization_record = self.materializer.materialize(trial_spec.candidate, workspace, candidate_context)
        except Exception as exc:
            materialization_record = MaterializationRecord(runtime_spec={}, metadata={"failed": True})
            observation = self._failure_observation(
                trial_spec,
                _status_for_exception(exc),
                "materialization",
                exc,
                time.monotonic() - started,
                materialization_record,
            )
            self._record_candidate(trial_spec, validation_report, materialization_record, error=observation.event_summary["error"])
            self._record_trial(trial_spec, observation.status)
            self.evidence_store.record_observation(observation.to_dict())
            return [observation]

        self._record_candidate(trial_spec, validation_report, materialization_record)
        context = {
            "trial_id": trial_spec.trial_id,
            "parent_trial_id": parent_trial_id,
            "attempt_index": attempt_index,
            "study_id": trial_spec.study_id,
            "workspace": str(workspace),
            "resource_profile": {
                "cpu": trial_spec.resource_profile.cpu,
                "memoryGiB": trial_spec.resource_profile.memory_gib,
                "gpu": trial_spec.resource_profile.gpu,
                "gpuClass": trial_spec.resource_profile.gpu_class,
                "timeoutSeconds": trial_spec.resource_profile.timeout_seconds,
            },
            "sandbox_spec": {
                "runtimeType": trial_spec.sandbox_spec.runtime_type,
                "networkPolicy": trial_spec.sandbox_spec.network_policy,
                "cleanupPolicy": trial_spec.sandbox_spec.cleanup_policy,
            },
            "backend_identity": trial_spec.metadata.get("backend_identity", {}),
            "backend_worker": trial_spec.metadata.get("backend_worker", {}),
        }
        try:
            result = self.environment_adapter.evaluate(materialization_record.runtime_spec, context)
            result = _validate_environment_result(result)
        except Exception as exc:
            result = _exception_evaluation_result(exc, "environment_evaluation", workspace)
        elapsed = time.monotonic() - started
        observation = self._observation_from_result(trial_spec, result, elapsed, materialization_record)
        self._record_trial(trial_spec, observation.status)
        self.evidence_store.record_observation(observation.to_dict())
        return [observation]

    def _record_candidate(
        self,
        trial_spec: TrialSpec,
        validation_report: ValidationReport,
        materialization_record: MaterializationRecord,
        error: Dict[str, Any] = None,
    ) -> None:
        payload = {
            "candidate_id": trial_spec.candidate["candidate_id"],
            "study_id": trial_spec.study_id,
            "trial_id": trial_spec.trial_id,
            "format": trial_spec.candidate["format"],
            "spec": dict(trial_spec.candidate.get("spec", {})),
            "lineage": dict(trial_spec.candidate.get("lineage", {})),
            "generator": dict(trial_spec.candidate.get("generator", {})),
            "validation_spec": dict(trial_spec.candidate.get("validation", {})),
            "materialization_spec": dict(trial_spec.candidate.get("materialization", {})),
            "validation": validation_report.to_dict(),
            "materialization": materialization_record.to_dict(),
            "created_at": utc_now_iso(),
        }
        if error:
            payload["error"] = error
        self.evidence_store.record_candidate(payload)

    def _record_trial(self, trial_spec: TrialSpec, status: str) -> None:
        self.evidence_store.record_trial(
            {
                "trial_id": trial_spec.trial_id,
                "study_id": trial_spec.study_id,
                "method_id": trial_spec.method_id,
                "candidate_id": trial_spec.candidate["candidate_id"],
                "format": trial_spec.candidate["format"],
                "candidate": dict(trial_spec.candidate),
                "status": status,
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "attempt_index": trial_spec.metadata.get("attempt_index", 1),
                "parent_trial_id": trial_spec.metadata.get("parent_trial_id", trial_spec.trial_id),
                "workspace": trial_spec.metadata.get("trial_workspace", ""),
                "created_at": utc_now_iso(),
            }
        )

    def _observation_from_result(
        self,
        trial_spec: TrialSpec,
        result: Dict[str, Any],
        wall_clock_seconds: float,
        materialization_record: MaterializationRecord,
    ) -> Observation:
        primary_metric = trial_spec.objective["primaryMetric"]["name"]
        output_files = list(materialization_record.output_files)
        output_files.extend(result.get("output_files", []))
        event_summary = dict(result.get("event_summary", {}))
        event_summary.setdefault("primary_metric", primary_metric)
        event_summary["materialization"] = materialization_record.metadata
        if event_summary.get("error") and "errors" not in event_summary:
            event_summary["errors"] = [event_summary["error"]]
        return Observation(
            trial_id=trial_spec.trial_id,
            study_id=trial_spec.study_id,
            candidate_id=trial_spec.candidate["candidate_id"],
            environment_id=self.study_spec.environment["environmentId"],
            status=result.get("status", "success"),
            metric_values=dict(result.get("metric_values", {})),
            constraint_results=dict(result.get("constraint_results", {})),
            resource_usage={
                "requested": trial_spec.resource_profile.to_dict(),
                "wallClockSeconds": wall_clock_seconds,
            },
            output_files=output_files,
            event_summary=event_summary,
            provenance={
                "method_id": trial_spec.method_id,
                "environment_version": self.study_spec.environment.get("environmentVersion"),
                "seed": trial_spec.metadata.get("seed"),
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "attempt_index": trial_spec.metadata.get("attempt_index", 1),
                "parent_trial_id": trial_spec.metadata.get("parent_trial_id", trial_spec.trial_id),
                "candidate_lineage": dict(trial_spec.candidate.get("lineage", {})),
                "generator": dict(trial_spec.candidate.get("generator", {})),
            },
        )

    def _invalid_observation(
        self,
        trial_spec: TrialSpec,
        validation_report: ValidationReport,
        wall_clock_seconds: float,
        materialization_record: MaterializationRecord,
    ) -> Observation:
        error = {
            "phase": "validation",
            "type": "ValidationError",
            "message": "; ".join(validation_report.errors) or "Candidate validation failed.",
            "errors": list(validation_report.errors),
        }
        return self._terminal_observation(
            trial_spec,
            "invalid",
            wall_clock_seconds,
            materialization_record,
            error,
        )

    def _failure_observation(
        self,
        trial_spec: TrialSpec,
        status: str,
        phase: str,
        exc: Exception,
        wall_clock_seconds: float,
        materialization_record: MaterializationRecord,
    ) -> Observation:
        return self._terminal_observation(
            trial_spec,
            status,
            wall_clock_seconds,
            materialization_record,
            _error_payload(exc, phase),
        )

    def _terminal_observation(
        self,
        trial_spec: TrialSpec,
        status: str,
        wall_clock_seconds: float,
        materialization_record: MaterializationRecord,
        error: Dict[str, Any],
    ) -> Observation:
        return Observation(
            trial_id=trial_spec.trial_id,
            study_id=trial_spec.study_id,
            candidate_id=trial_spec.candidate["candidate_id"],
            environment_id=self.study_spec.environment["environmentId"],
            status=status,
            metric_values={},
            constraint_results={},
            resource_usage={
                "requested": trial_spec.resource_profile.to_dict(),
                "wallClockSeconds": wall_clock_seconds,
            },
            output_files=list(materialization_record.output_files),
            event_summary={
                "primary_metric": trial_spec.objective["primaryMetric"]["name"],
                "materialization": materialization_record.metadata,
                "error": error,
                "errors": [error],
            },
            provenance={
                "method_id": trial_spec.method_id,
                "environment_version": self.study_spec.environment.get("environmentVersion"),
                "seed": trial_spec.metadata.get("seed"),
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "attempt_index": trial_spec.metadata.get("attempt_index", 1),
                "parent_trial_id": trial_spec.metadata.get("parent_trial_id", trial_spec.trial_id),
                "candidate_lineage": dict(trial_spec.candidate.get("lineage", {})),
                "generator": dict(trial_spec.candidate.get("generator", {})),
            },
        )


class LocalExecutionBackend:
    def __init__(self, definition: Dict[str, Any], evaluator: Evaluator, max_workers: int = 1):
        self.definition = definition
        self.config = dict(definition.get("config", {}) or {})
        self.evaluator = evaluator
        self.max_workers = max_workers
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._futures: Dict[str, concurrent.futures.Future] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, trial_spec: TrialSpec) -> str:
        handle = f"handle-{uuid.uuid4().hex[:12]}"
        worker_metadata = {
            "handle": handle,
            "backend": "local_thread",
            "worker_pool": "ThreadPoolExecutor",
            "max_workers": self.max_workers,
            "submitted_at": utc_now_iso(),
        }
        trial_spec.metadata["backend_worker"] = dict(worker_metadata)
        future = self.executor.submit(self.evaluator.run_trial, trial_spec)
        with self._lock:
            self._futures[handle] = future
            self._metadata[handle] = worker_metadata
        return handle

    def status(self, handle: str) -> Dict[str, Any]:
        future = self._futures[handle]
        if future.cancelled():
            state = "cancelled"
        elif future.done():
            state = "finished"
        else:
            state = "running"
        return {"handle": handle, "state": state, "worker": dict(self._metadata.get(handle, {}))}

    def cancel(self, handle: str) -> None:
        self._futures[handle].cancel()

    def collect(self, handle: str) -> List[Observation]:
        return self._futures[handle].result()


class LocalSubprocessExecutionBackend:
    """Run each trial in a separate Python worker process.

    This reference backend provides hard process-level cancellation for trials
    that exceed their declared ``ResourceProfile.timeoutSeconds``. It is still a
    local backend, not a sandbox.
    """

    def __init__(self, definition: Dict[str, Any], evaluator: Evaluator, max_workers: int = 1):
        self.definition = definition
        self.config = dict(definition.get("config", {}) or {})
        self.evaluator = evaluator
        self.max_workers = max_workers
        self.run_dir = evaluator.evidence_store.run_dir
        self.handles_dir = self.run_dir / "backend_handles"
        self.handles_dir.mkdir(parents=True, exist_ok=True)
        self._processes: Dict[str, subprocess.Popen] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._trial_specs: Dict[str, TrialSpec] = {}
        self._paths: Dict[str, Dict[str, Path]] = {}
        self._started_monotonic: Dict[str, float] = {}
        self._lock = threading.Lock()

    def submit(self, trial_spec: TrialSpec) -> str:
        handle = f"handle-{uuid.uuid4().hex[:12]}"
        handle_dir = self.handles_dir / handle
        handle_dir.mkdir(parents=True, exist_ok=False)
        input_path = handle_dir / "worker_input.json"
        output_path = handle_dir / "worker_output.json"
        stdout_path = handle_dir / "worker_stdout.log"
        stderr_path = handle_dir / "worker_stderr.log"
        worker_metadata = {
            "handle": handle,
            "backend": "local_subprocess",
            "worker_process": "python -m optpilot.worker",
            "submitted_at": utc_now_iso(),
            "timeoutSeconds": trial_spec.resource_profile.timeout_seconds,
            "input_path": str(input_path),
            "output_path": str(output_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        trial_spec.metadata["backend_worker"] = dict(worker_metadata)
        input_payload = {
            "study_spec_path": str(self.evaluator.study_spec.path),
            "study_spec_raw": self.evaluator.study_spec.raw,
            "run_dir": str(self.run_dir),
            "trial_spec": trial_spec_to_dict(trial_spec),
            "output_path": str(output_path),
        }
        input_path.write_text(json.dumps(input_payload, indent=2, sort_keys=True), encoding="utf-8")
        env = _worker_process_env(self.config)
        python_executable = str(self.config.get("workerPythonExecutable") or sys.executable)
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [python_executable, "-m", "optpilot.worker", str(input_path)],
                cwd=str(Path.cwd()),
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        worker_metadata["pid"] = process.pid
        with self._lock:
            self._processes[handle] = process
            self._metadata[handle] = worker_metadata
            self._trial_specs[handle] = trial_spec
            self._paths[handle] = {
                "output": output_path,
                "stdout": stdout_path,
                "stderr": stderr_path,
            }
            self._started_monotonic[handle] = time.monotonic()
        return handle

    def status(self, handle: str) -> Dict[str, Any]:
        process = self._processes[handle]
        returncode = process.poll()
        if returncode is None:
            state = "running"
        elif returncode == 0:
            state = "finished"
        else:
            state = "failed"
        return {
            "handle": handle,
            "state": state,
            "return_code": returncode,
            "worker": dict(self._metadata.get(handle, {})),
        }

    def cancel(self, handle: str) -> None:
        process = self._processes[handle]
        if process.poll() is None:
            process.kill()
            process.wait()

    def collect(self, handle: str) -> List[Observation]:
        process = self._processes[handle]
        trial_spec = self._trial_specs[handle]
        paths = self._paths[handle]
        timeout_seconds = max(1, int(trial_spec.resource_profile.timeout_seconds))
        started = self._started_monotonic.get(handle, time.monotonic())
        remaining = timeout_seconds - (time.monotonic() - started)
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout_seconds)
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            elapsed = time.monotonic() - started
            observation = _backend_timeout_observation(
                self.evaluator.study_spec,
                trial_spec,
                elapsed,
                _worker_output_files(paths),
                exc,
            )
            self.evaluator._record_trial(trial_spec, observation.status)
            self.evaluator.evidence_store.record_observation(observation.to_dict())
            return [observation]

        if paths["output"].exists():
            payload = json.loads(paths["output"].read_text(encoding="utf-8"))
            return [Observation(**item) for item in payload.get("observations", [])]
        elapsed = time.monotonic() - started
        error = RuntimeError(f"Local subprocess worker exited with code {returncode} without writing output.")
        observation = _backend_failure_observation(
            self.evaluator.study_spec,
            trial_spec,
            elapsed,
            _worker_output_files(paths),
            error,
        )
        self.evaluator._record_trial(trial_spec, observation.status)
        self.evaluator.evidence_store.record_observation(observation.to_dict())
        return [observation]


def _aggregate_metric_values(results: List[Dict[str, Any]], objective: Dict[str, Any]) -> Dict[str, Any]:
    if not results:
        return {}
    metric_names = set()
    for result in results:
        metric_names.update(result.get("metric_values", {}).keys())
    aggregation = objective.get("aggregation", {}).get("mode", "mean")
    aggregated: Dict[str, Any] = {}
    for metric_name in metric_names:
        values = [
            float(result.get("metric_values", {})[metric_name])
            for result in results
            if metric_name in result.get("metric_values", {})
        ]
        if not values:
            continue
        aggregated[metric_name] = _aggregate_values(values, aggregation, objective, metric_name)
    return aggregated


def _aggregate_values(values: List[float], aggregation: str, objective: Dict[str, Any], metric_name: str) -> float:
    if aggregation == "mean":
        return sum(values) / len(values)
    if aggregation == "median":
        sorted_values = sorted(values)
        midpoint = len(sorted_values) // 2
        if len(sorted_values) % 2:
            return sorted_values[midpoint]
        return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2
    if aggregation == "min":
        return min(values)
    if aggregation == "max":
        return max(values)
    if aggregation == "sum":
        return sum(values)
    if aggregation == "last":
        return values[-1]
    if aggregation == "weighted_mean":
        weights = objective.get("aggregation", {}).get("weights", {})
        resolved_weights = _aggregation_weights(weights, metric_name, len(values))
        total_weight = sum(resolved_weights)
        if total_weight == 0:
            raise ValueError("weighted_mean aggregation requires non-zero total weight.")
        return sum(value * weight for value, weight in zip(values, resolved_weights)) / total_weight
    raise NotImplementedError(f"Unsupported aggregation mode: {aggregation}")


def _aggregation_weights(weights: Any, metric_name: str, count: int) -> List[float]:
    if isinstance(weights, list):
        if len(weights) != count:
            raise ValueError(f"weighted_mean weights length {len(weights)} does not match value count {count}.")
        return [float(weight) for weight in weights]
    if isinstance(weights, dict):
        metric_weights = weights.get(metric_name, weights.get("*"))
        if isinstance(metric_weights, list):
            if len(metric_weights) != count:
                raise ValueError(
                    f"weighted_mean weights for {metric_name!r} length {len(metric_weights)} does not match value count {count}."
                )
            return [float(weight) for weight in metric_weights]
        if metric_weights is not None:
            return [float(metric_weights)] * count
    return [1.0] * count


def _aggregate_status(statuses) -> str:
    if not statuses:
        return "failed"
    if statuses == {"success"}:
        return "success"
    non_success = statuses - {"success"}
    if len(non_success) == 1 and "success" not in statuses:
        return next(iter(non_success))
    return "partial"


def _worker_output_files(paths: Dict[str, Path]) -> List[Dict[str, Any]]:
    output_files = []
    for name, file_type, path in [
        ("backend_worker_stdout", "log", paths["stdout"]),
        ("backend_worker_stderr", "log", paths["stderr"]),
        ("backend_worker_output", "json", paths["output"]),
    ]:
        if path.exists():
            output_files.append({"type": file_type, "name": name, "path": str(path)})
    return output_files


def _backend_timeout_observation(
    study_spec,
    trial_spec: TrialSpec,
    wall_clock_seconds: float,
    output_files: List[Dict[str, Any]],
    exc: Exception,
) -> Observation:
    return _backend_terminal_observation(
        study_spec,
        trial_spec,
        "timeout",
        wall_clock_seconds,
        output_files,
        _error_payload(exc, "backend_execution"),
    )


def _backend_failure_observation(
    study_spec,
    trial_spec: TrialSpec,
    wall_clock_seconds: float,
    output_files: List[Dict[str, Any]],
    exc: Exception,
) -> Observation:
    return _backend_terminal_observation(
        study_spec,
        trial_spec,
        "failed",
        wall_clock_seconds,
        output_files,
        _error_payload(exc, "backend_execution"),
    )


def _backend_terminal_observation(
    study_spec,
    trial_spec: TrialSpec,
    status: str,
    wall_clock_seconds: float,
    output_files: List[Dict[str, Any]],
    error: Dict[str, Any],
) -> Observation:
    return Observation(
        trial_id=trial_spec.trial_id,
        study_id=trial_spec.study_id,
        candidate_id=trial_spec.candidate["candidate_id"],
        environment_id=study_spec.environment["environmentId"],
        status=status,
        metric_values={},
        constraint_results={},
        resource_usage={
            "requested": trial_spec.resource_profile.to_dict(),
            "wallClockSeconds": wall_clock_seconds,
        },
        output_files=output_files,
        event_summary={
            "primary_metric": trial_spec.objective["primaryMetric"]["name"],
            "error": error,
            "errors": [error],
        },
        provenance={
            "method_id": trial_spec.method_id,
            "environment_version": study_spec.environment.get("environmentVersion"),
            "seed": trial_spec.metadata.get("seed"),
            "resource_profile": trial_spec.resource_profile.to_dict(),
            "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
            "backend_identity": trial_spec.metadata.get("backend_identity", {}),
            "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
            "backend_worker": trial_spec.metadata.get("backend_worker", {}),
            "attempt_index": trial_spec.metadata.get("attempt_index", 1),
            "parent_trial_id": trial_spec.metadata.get("parent_trial_id", trial_spec.trial_id),
            "candidate_lineage": dict(trial_spec.candidate.get("lineage", {})),
            "generator": dict(trial_spec.candidate.get("generator", {})),
        },
    )


def trial_spec_to_dict(trial_spec: TrialSpec) -> Dict[str, Any]:
    payload = asdict(trial_spec)
    payload["resource_profile"] = trial_spec.resource_profile.to_dict()
    payload["sandbox_spec"] = trial_spec.sandbox_spec.to_dict()
    return payload


def trial_spec_from_dict(payload: Dict[str, Any]) -> TrialSpec:
    return TrialSpec(
        trial_id=payload["trial_id"],
        study_id=payload["study_id"],
        method_id=payload["method_id"],
        candidate=dict(payload["candidate"]),
        objective=dict(payload["objective"]),
        resource_profile=ResourceProfile.from_dict(payload.get("resource_profile")),
        sandbox_spec=SandboxSpec.from_dict(payload.get("sandbox_spec")),
        metadata=dict(payload.get("metadata", {})),
    )


def _worker_process_env(config: Dict[str, Any]) -> Dict[str, str]:
    env = apply_prepared_env(minimal_host_env(), config.get("preparedEnv", {}))
    env.update({str(key): str(value) for key, value in config.get("env", {}).items()})
    env.update({str(key): str(value) for key, value in config.get("environmentVariables", {}).items()})
    for declared in compile_host_env_declarations(
        config.get("envFromHost", []), location="runtime.envFromHost"
    ):
        if declared.name in os.environ:
            env[declared.name] = os.environ[declared.name]
        elif declared.default is not None:
            env[declared.name] = declared.default
    pythonpath_entries = [str(Path(__file__).resolve().parents[1]), str(Path.cwd().resolve())]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env
