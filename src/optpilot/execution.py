"""Execution backend and evaluator for OptPilot."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from .artifacts import MaterializationRecord, ValidationReport
from .container_utils import build_container_image, container_pythonpath, dedupe_mounts, network_args
from .models import Observation, ResourceProfile, SandboxSpec, TrialSpec, utc_now_iso


class Evaluator:
    def __init__(self, study_spec, target_adapter, evidence_store, materializer, artifact_validator):
        self.study_spec = study_spec
        self.target_adapter = target_adapter
        self.evidence_store = evidence_store
        self.materializer = materializer
        self.artifact_validator = artifact_validator

    def run_trial(self, trial_spec: TrialSpec) -> List[Observation]:
        workspace = self.evidence_store.create_trial_workspace(trial_spec.trial_id)
        instance_results: List[Dict[str, Any]] = []
        started = time.monotonic()
        artifact_context = {
            "trial_id": trial_spec.trial_id,
            "study_id": trial_spec.study_id,
            "workspace": str(workspace),
            "resource_profile": trial_spec.resource_profile.to_dict(),
            "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
            "backend_identity": trial_spec.metadata.get("backend_identity", {}),
            "backend_worker": trial_spec.metadata.get("backend_worker", {}),
        }
        try:
            validation_report = self.artifact_validator.validate(trial_spec.artifact, artifact_context)
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
            self._record_artifact(trial_spec, validation_report, materialization_record, error=observation.event_summary["error"])
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
            self._record_artifact(trial_spec, validation_report, materialization_record)
            self._record_trial(trial_spec, observation.status)
            self.evidence_store.record_observation(observation.to_dict())
            return [observation]

        try:
            materialization_record = self.materializer.materialize(trial_spec.artifact, workspace, artifact_context)
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
            self._record_artifact(trial_spec, validation_report, materialization_record, error=observation.event_summary["error"])
            self._record_trial(trial_spec, observation.status)
            self.evidence_store.record_observation(observation.to_dict())
            return [observation]

        self._record_artifact(trial_spec, validation_report, materialization_record)
        for index, instance in enumerate(trial_spec.instances):
            context = {
                "trial_id": trial_spec.trial_id,
                "study_id": trial_spec.study_id,
                "workspace": str(workspace),
                "instance_index": index,
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
                result = self.target_adapter.evaluate(materialization_record.runtime_spec, instance, context)
                _validate_target_result(result)
            except Exception as exc:
                result = _exception_instance_result(exc, "target_evaluation", workspace, index)
            instance_results.append(result)
        elapsed = time.monotonic() - started
        observation = self._aggregate_results(trial_spec, instance_results, elapsed, materialization_record)
        self._record_trial(trial_spec, observation.status)
        self.evidence_store.record_observation(observation.to_dict())
        return [observation]

    def _record_artifact(
        self,
        trial_spec: TrialSpec,
        validation_report: ValidationReport,
        materialization_record: MaterializationRecord,
        error: Dict[str, Any] = None,
    ) -> None:
        payload = {
            "artifact_id": trial_spec.artifact["artifact_id"],
            "study_id": trial_spec.study_id,
            "trial_id": trial_spec.trial_id,
            "artifact_kind": trial_spec.artifact["artifact_kind"],
            "spec": dict(trial_spec.artifact.get("spec", {})),
            "lineage": dict(trial_spec.artifact.get("lineage", {})),
            "generator_record": dict(trial_spec.artifact.get("generator_record", {})),
            "validation_rules": dict(trial_spec.artifact.get("validation_rules", {})),
            "materialization_plan": dict(trial_spec.artifact.get("materialization_plan", {})),
            "validation": validation_report.to_dict(),
            "materialization": materialization_record.to_dict(),
            "created_at": utc_now_iso(),
        }
        if error:
            payload["error"] = error
        self.evidence_store.record_artifact(payload)

    def _record_trial(self, trial_spec: TrialSpec, status: str) -> None:
        self.evidence_store.record_trial(
            {
                "trial_id": trial_spec.trial_id,
                "study_id": trial_spec.study_id,
                "method_id": trial_spec.method_id,
                "artifact_id": trial_spec.artifact["artifact_id"],
                "artifact_kind": trial_spec.artifact["artifact_kind"],
                "artifact": dict(trial_spec.artifact),
                "instance_count": len(trial_spec.instances),
                "status": status,
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "created_at": utc_now_iso(),
            }
        )

    def _aggregate_results(
        self,
        trial_spec: TrialSpec,
        instance_results: List[Dict[str, Any]],
        wall_clock_seconds: float,
        materialization_record: MaterializationRecord,
    ) -> Observation:
        primary_metric = trial_spec.objective["primaryMetric"]["name"]
        aggregated_metrics = _aggregate_metric_values(instance_results, trial_spec.objective)
        artifacts = list(materialization_record.artifacts)
        for result in instance_results:
            artifacts.extend(result.get("artifacts", []))
        artifacts = self._retain_artifacts(trial_spec.trial_id, artifacts)
        statuses = {result.get("status", "success") for result in instance_results}
        status = _aggregate_status(statuses)
        failure_events = [
            result.get("event_summary", {}).get("error")
            for result in instance_results
            if result.get("event_summary", {}).get("error")
        ]
        return Observation(
            trial_id=trial_spec.trial_id,
            study_id=trial_spec.study_id,
            artifact_id=trial_spec.artifact["artifact_id"],
            target_id=self.study_spec.target["targetId"],
            instance_descriptor={"mode": self.study_spec.evaluation_scope["mode"], "count": len(trial_spec.instances)},
            status=status,
            metric_values=aggregated_metrics,
            constraint_results={},
            resource_usage={
                "requested": trial_spec.resource_profile.to_dict(),
                "wallClockSeconds": wall_clock_seconds,
            },
            artifacts=artifacts,
            event_summary={
                "primary_metric": primary_metric,
                "evaluated_instances": len(trial_spec.instances),
                "materialization": materialization_record.metadata,
                "errors": failure_events,
            },
            provenance={
                "method_id": trial_spec.method_id,
                "target_version": self.study_spec.target.get("targetVersion"),
                "seed": trial_spec.metadata.get("seed"),
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "artifact_lineage": dict(trial_spec.artifact.get("lineage", {})),
                "generator_record": dict(trial_spec.artifact.get("generator_record", {})),
            },
        )

    def _retain_artifacts(self, trial_id: str, artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.study_spec.evidence.get("artifactStorage", "reference") != "copy":
            return artifacts
        retained = []
        destination_root = self.evidence_store.run_dir / "evidence_artifacts" / trial_id
        for index, artifact in enumerate(artifacts):
            item = dict(artifact)
            source_value = item.get("path") or item.get("contentRef")
            if not source_value:
                retained.append(item)
                continue
            source = Path(str(source_value))
            if not source.exists() or not source.is_file():
                retained.append(item)
                continue
            relative_name = _safe_artifact_copy_name(source.name, index)
            destination = destination_root / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            item["originalPath"] = str(source)
            item["copiedPath"] = str(destination)
            item["path"] = str(destination)
            retained.append(item)
        return retained

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
            "message": "; ".join(validation_report.errors) or "Artifact validation failed.",
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
            artifact_id=trial_spec.artifact["artifact_id"],
            target_id=self.study_spec.target["targetId"],
            instance_descriptor={"mode": self.study_spec.evaluation_scope["mode"], "count": len(trial_spec.instances)},
            status=status,
            metric_values={},
            constraint_results={},
            resource_usage={
                "requested": trial_spec.resource_profile.to_dict(),
                "wallClockSeconds": wall_clock_seconds,
            },
            artifacts=list(materialization_record.artifacts),
            event_summary={
                "primary_metric": trial_spec.objective["primaryMetric"]["name"],
                "evaluated_instances": 0,
                "materialization": materialization_record.metadata,
                "error": error,
                "errors": [error],
            },
            provenance={
                "method_id": trial_spec.method_id,
                "target_version": self.study_spec.target.get("targetVersion"),
                "seed": trial_spec.metadata.get("seed"),
                "resource_profile": trial_spec.resource_profile.to_dict(),
                "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
                "backend_identity": trial_spec.metadata.get("backend_identity", {}),
                "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
                "backend_worker": trial_spec.metadata.get("backend_worker", {}),
                "artifact_lineage": dict(trial_spec.artifact.get("lineage", {})),
                "generator_record": dict(trial_spec.artifact.get("generator_record", {})),
            },
        )


class LocalExecutionBackend:
    def __init__(self, definition: Dict[str, Any], evaluator: Evaluator, max_workers: int = 1):
        self.definition = definition
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
        self.evaluator = evaluator
        self.max_workers = max_workers
        self.run_dir = evaluator.evidence_store.run_dir
        self.handles_dir = self.run_dir / "backend_handles"
        self.handles_dir.mkdir(parents=True, exist_ok=True)
        self._processes: Dict[str, subprocess.Popen] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._trial_specs: Dict[str, TrialSpec] = {}
        self._paths: Dict[str, Dict[str, Path]] = {}
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
        env = os.environ.copy()
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "optpilot.worker", str(input_path)],
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
        started = time.monotonic()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            elapsed = time.monotonic() - started
            observation = _backend_timeout_observation(
                self.evaluator.study_spec,
                trial_spec,
                elapsed,
                _worker_artifacts(paths),
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
            _worker_artifacts(paths),
            error,
        )
        self.evaluator._record_trial(trial_spec, observation.status)
        self.evaluator.evidence_store.record_observation(observation.to_dict())
        return [observation]


class LocalContainerExecutionBackend(LocalSubprocessExecutionBackend):
    """Run each trial worker through a Docker/Podman-compatible container CLI.

    The backend intentionally reuses the JSON worker contract from
    ``LocalSubprocessExecutionBackend``. Host paths are mounted at the same
    absolute paths inside the container, which keeps compiled configs and trial
    evidence paths stable across local and container execution.
    """

    def __init__(self, definition: Dict[str, Any], evaluator: Evaluator, max_workers: int = 1):
        super().__init__(definition, evaluator, max_workers=max_workers)
        self.config = dict(definition.get("config", {}))
        self.container_executable = str(self.config.get("containerExecutable", "docker"))
        self.python_executable = str(self.config.get("pythonExecutable", "python"))
        self._build_lock = threading.Lock()
        self._built_image_keys = set()

    def submit(self, trial_spec: TrialSpec) -> str:
        handle = f"handle-{uuid.uuid4().hex[:12]}"
        handle_dir = self.handles_dir / handle
        handle_dir.mkdir(parents=True, exist_ok=False)
        input_path = handle_dir / "worker_input.json"
        output_path = handle_dir / "worker_output.json"
        stdout_path = handle_dir / "worker_stdout.log"
        stderr_path = handle_dir / "worker_stderr.log"
        container_name = _container_name(handle)
        image = _container_image(self.config, trial_spec)
        build_metadata = self._ensure_container_image(image, trial_spec)
        worker_metadata = {
            "handle": handle,
            "backend": "local_container",
            "container_executable": self.container_executable,
            "container_image": image,
            "container_build": build_metadata,
            "container_name": container_name,
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

        command = self._container_command(
            trial_spec,
            container_name=container_name,
            image=image,
            input_path=input_path,
        )
        worker_metadata["command"] = list(command)
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
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
        return handle

    def _ensure_container_image(self, image: str, trial_spec: TrialSpec) -> Dict[str, Any]:
        build = self.config.get("build")
        if not build:
            return {}
        if not isinstance(build, dict):
            raise ValueError("execution.config.build must be an object.")
        key = (self.container_executable, image, json.dumps(build, sort_keys=True))
        with self._build_lock:
            if key in self._built_image_keys:
                return {"configured": True, "status": "already_built", "image": image}
            metadata = build_container_image(
                executable=self.container_executable,
                image=image,
                build=build,
                base_dir=self.evaluator.study_spec.base_dir,
                timeout=int(build.get("timeoutSeconds", 0) or trial_spec.resource_profile.timeout_seconds or 0) or None,
            )
            self._built_image_keys.add(key)
        return {
            "configured": True,
            "status": "built",
            "image": image,
            "command": metadata.get("command", []),
            "returncode": metadata.get("returncode"),
        }

    def cancel(self, handle: str) -> None:
        self._force_remove_container(handle)
        super().cancel(handle)

    def collect(self, handle: str) -> List[Observation]:
        process = self._processes[handle]
        trial_spec = self._trial_specs[handle]
        paths = self._paths[handle]
        timeout_seconds = max(1, int(trial_spec.resource_profile.timeout_seconds))
        started = time.monotonic()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._force_remove_container(handle)
            process.kill()
            process.wait()
            elapsed = time.monotonic() - started
            observation = _backend_timeout_observation(
                self.evaluator.study_spec,
                trial_spec,
                elapsed,
                _worker_artifacts(paths),
                exc,
            )
            self.evaluator._record_trial(trial_spec, observation.status)
            self.evaluator.evidence_store.record_observation(observation.to_dict())
            return [observation]

        if paths["output"].exists():
            payload = json.loads(paths["output"].read_text(encoding="utf-8"))
            return [Observation(**item) for item in payload.get("observations", [])]
        elapsed = time.monotonic() - started
        error = RuntimeError(f"Container worker exited with code {returncode} without writing output.")
        observation = _backend_failure_observation(
            self.evaluator.study_spec,
            trial_spec,
            elapsed,
            _worker_artifacts(paths),
            error,
        )
        self.evaluator._record_trial(trial_spec, observation.status)
        self.evaluator.evidence_store.record_observation(observation.to_dict())
        return [observation]

    def _container_command(
        self,
        trial_spec: TrialSpec,
        *,
        container_name: str,
        image: str,
        input_path: Path,
    ) -> List[str]:
        command = [
            self.container_executable,
            "run",
            "--rm",
            "--name",
            container_name,
        ]
        command.extend(network_args(trial_spec.sandbox_spec.network_policy))
        for host_path, mode in _container_mounts(self.evaluator.study_spec, self.run_dir, trial_spec.sandbox_spec):
            command.extend(["-v", f"{host_path}:{host_path}:{mode}"])
        command.extend(["-w", str(Path.cwd().resolve())])
        env = {
            "PYTHONPATH": container_pythonpath(),
            "PYTHONDONTWRITEBYTECODE": "1",
            **{str(key): str(value) for key, value in trial_spec.sandbox_spec.environment_variables.items()},
        }
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
        if trial_spec.resource_profile.cpu:
            command.extend(["--cpus", str(trial_spec.resource_profile.cpu)])
        if trial_spec.resource_profile.memory_gib:
            command.extend(["--memory", f"{trial_spec.resource_profile.memory_gib}g"])
        command.extend(list(self.config.get("extraArgs", []) or []))
        command.extend([image, self.python_executable, "-m", "optpilot.worker", str(input_path)])
        return command

    def _force_remove_container(self, handle: str) -> None:
        metadata = self._metadata.get(handle, {})
        container_name = metadata.get("container_name")
        if not container_name:
            return
        subprocess.run(
            [self.container_executable, "rm", "-f", str(container_name)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _aggregate_metric_values(instance_results: List[Dict[str, Any]], objective: Dict[str, Any]) -> Dict[str, Any]:
    if not instance_results:
        return {}
    metric_names = set()
    for result in instance_results:
        metric_names.update(result.get("metric_values", {}).keys())
    aggregation = objective.get("aggregation", {}).get("mode", "mean")
    aggregated: Dict[str, Any] = {}
    for metric_name in metric_names:
        values = [
            float(result.get("metric_values", {})[metric_name])
            for result in instance_results
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


def _validation_exception_report(exc: Exception) -> ValidationReport:
    return ValidationReport(
        accepted=False,
        errors=[str(exc)],
        metadata={"exception": _error_payload(exc, "validation")},
    )


def _exception_instance_result(exc: Exception, phase: str, workspace: Path, instance_index: int) -> Dict[str, Any]:
    status = _status_for_exception(exc)
    return {
        "status": status,
        "metric_values": {},
        "constraint_results": {},
        "artifacts": _failure_artifacts(workspace, instance_index),
        "event_summary": {
            "error": _error_payload(exc, phase),
        },
    }


def _validate_target_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise TypeError("Target adapter result must be a JSON-like object.")
    status = result.get("status", "success")
    if not isinstance(status, str):
        raise TypeError("Target adapter result status must be a string.")
    metric_values = result.get("metric_values", {})
    if not isinstance(metric_values, dict):
        raise TypeError("Target adapter result metric_values must be a dict.")
    for metric_name, metric_value in metric_values.items():
        if not isinstance(metric_name, str):
            raise TypeError("Target adapter metric names must be strings.")
        if not isinstance(metric_value, (int, float, bool)):
            raise TypeError(f"Target adapter metric {metric_name!r} must be numeric or boolean.")
    constraint_results = result.get("constraint_results", {})
    if not isinstance(constraint_results, dict):
        raise TypeError("Target adapter result constraint_results must be a dict.")
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise TypeError("Target adapter result artifacts must be a list.")
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise TypeError(f"Target adapter artifact entry {index} must be an object.")
    event_summary = result.get("event_summary", {})
    if not isinstance(event_summary, dict):
        raise TypeError("Target adapter result event_summary must be a dict.")


def _aggregate_status(statuses) -> str:
    if not statuses:
        return "failed"
    if statuses == {"success"}:
        return "success"
    non_success = statuses - {"success"}
    if len(non_success) == 1 and "success" not in statuses:
        return next(iter(non_success))
    return "partial"


def _status_for_exception(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    return "failed"


def _error_payload(exc: Exception, phase: str) -> Dict[str, Any]:
    return {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)),
    }


def _failure_artifacts(workspace: Path, instance_index: int) -> List[Dict[str, Any]]:
    artifacts = []
    for name, artifact_type, path in [
        ("cli_artifact_input", "json", workspace / f"cli_artifact_{instance_index}.json"),
        ("cli_instance_input", "json", workspace / f"cli_instance_{instance_index}.json"),
        ("cli_result_output", "json", workspace / f"cli_result_{instance_index}.json"),
        ("cli_stdout", "log", workspace / f"cli_stdout_{instance_index}.log"),
        ("cli_stderr", "log", workspace / f"cli_stderr_{instance_index}.log"),
    ]:
        if path.exists():
            artifacts.append({"type": artifact_type, "name": name, "path": str(path)})
    return artifacts


def _worker_artifacts(paths: Dict[str, Path]) -> List[Dict[str, Any]]:
    artifacts = []
    for name, artifact_type, path in [
        ("backend_worker_stdout", "log", paths["stdout"]),
        ("backend_worker_stderr", "log", paths["stderr"]),
        ("backend_worker_output", "json", paths["output"]),
    ]:
        if path.exists():
            artifacts.append({"type": artifact_type, "name": name, "path": str(path)})
    return artifacts


def _container_image(config: Dict[str, Any], trial_spec: TrialSpec) -> str:
    build = config.get("build", {}) if isinstance(config.get("build", {}), dict) else {}
    image = config.get("image") or build.get("tag") or trial_spec.resource_profile.runtime_image
    if not image:
        raise ValueError("Container backend requires execution.config.image, execution.config.build.tag, or resourceProfile.runtimeImage.")
    return str(image)


def _container_name(handle: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in handle.lower())
    return f"optpilot-{safe}"


def _container_mounts(study_spec, run_dir: Path, sandbox_spec: SandboxSpec) -> List[tuple[str, str]]:
    mounts: List[tuple[Path, str]] = [
        (Path.cwd().resolve(), "ro"),
        (Path(run_dir).resolve(), "rw"),
        (study_spec.base_dir.resolve(), "ro"),
    ]
    for value in sandbox_spec.read_only_mounts:
        mounts.append((Path(str(value)).resolve(), "ro"))
    if sandbox_spec.writable_workspace:
        mounts.append((Path(str(sandbox_spec.writable_workspace)).resolve(), "rw"))
    return [(str(path), mode) for path, mode in dedupe_mounts(mounts)]


def _backend_timeout_observation(
    study_spec,
    trial_spec: TrialSpec,
    wall_clock_seconds: float,
    artifacts: List[Dict[str, Any]],
    exc: Exception,
) -> Observation:
    return _backend_terminal_observation(
        study_spec,
        trial_spec,
        "timeout",
        wall_clock_seconds,
        artifacts,
        _error_payload(exc, "backend_execution"),
    )


def _backend_failure_observation(
    study_spec,
    trial_spec: TrialSpec,
    wall_clock_seconds: float,
    artifacts: List[Dict[str, Any]],
    exc: Exception,
) -> Observation:
    return _backend_terminal_observation(
        study_spec,
        trial_spec,
        "failed",
        wall_clock_seconds,
        artifacts,
        _error_payload(exc, "backend_execution"),
    )


def _backend_terminal_observation(
    study_spec,
    trial_spec: TrialSpec,
    status: str,
    wall_clock_seconds: float,
    artifacts: List[Dict[str, Any]],
    error: Dict[str, Any],
) -> Observation:
    return Observation(
        trial_id=trial_spec.trial_id,
        study_id=trial_spec.study_id,
        artifact_id=trial_spec.artifact["artifact_id"],
        target_id=study_spec.target["targetId"],
        instance_descriptor={"mode": study_spec.evaluation_scope["mode"], "count": len(trial_spec.instances)},
        status=status,
        metric_values={},
        constraint_results={},
        resource_usage={
            "requested": trial_spec.resource_profile.to_dict(),
            "wallClockSeconds": wall_clock_seconds,
        },
        artifacts=artifacts,
        event_summary={
            "primary_metric": trial_spec.objective["primaryMetric"]["name"],
            "evaluated_instances": 0,
            "error": error,
            "errors": [error],
        },
        provenance={
            "method_id": trial_spec.method_id,
            "target_version": study_spec.target.get("targetVersion"),
            "seed": trial_spec.metadata.get("seed"),
            "resource_profile": trial_spec.resource_profile.to_dict(),
            "sandbox_spec": trial_spec.sandbox_spec.to_dict(),
            "backend_identity": trial_spec.metadata.get("backend_identity", {}),
            "scheduler_identity": trial_spec.metadata.get("scheduler_identity", {}),
            "backend_worker": trial_spec.metadata.get("backend_worker", {}),
            "artifact_lineage": dict(trial_spec.artifact.get("lineage", {})),
            "generator_record": dict(trial_spec.artifact.get("generator_record", {})),
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
        artifact=dict(payload["artifact"]),
        instances=list(payload["instances"]),
        objective=dict(payload["objective"]),
        resource_profile=ResourceProfile.from_dict(payload.get("resource_profile")),
        sandbox_spec=SandboxSpec.from_dict(payload.get("sandbox_spec")),
        metadata=dict(payload.get("metadata", {})),
    )


def _safe_artifact_copy_name(name: str, index: int) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)
    safe = safe.strip("._") or "artifact"
    return f"{index:04d}-{safe}"
