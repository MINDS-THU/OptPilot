"""Runtime adapters for user-owned method implementations."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .container_utils import build_container_image, container_pythonpath, dedupe_mounts, network_args
from .models import utc_now_iso


TERMINAL_STATES = {"completed", "failed", "finished", "succeeded", "cancelled"}
SUCCESS_STATES = {"completed", "finished", "succeeded"}


class MethodRuntime:
    """Normalizes supported method protocols into the runner's batch flow."""

    def __init__(self, definition: Dict[str, Any], method, evidence_store, study_spec):
        self.definition = definition
        self.method = method
        self.evidence_store = evidence_store
        self.study_spec = study_spec
        self.method_id = definition["id"]
        self._built_container_images = set()

    def propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view=None) -> List[Dict[str, Any]]:
        implementation = self.definition.get("implementation", {})
        protocol = implementation.get("protocol", "optpilot.method.batch.v1")
        if protocol != "optpilot.method.batch.v1":
            raise NotImplementedError(f"Method protocol {protocol!r} is not implemented yet.")

        if implementation.get("type") == "command":
            return self._command_batch_propose(n_candidates, study_state, evidence_view)
        if self.method is None:
            raise TypeError(f"Method {self.method_id!r} has no Python implementation instance.")
        if hasattr(self.method, "start") and hasattr(self.method, "poll") and hasattr(self.method, "finalize"):
            return self._lifecycle_propose(n_candidates, study_state, evidence_view)
        if hasattr(self.method, "propose"):
            candidates = _call_with_optional_evidence(self.method.propose, n_candidates, study_state, evidence_view)
            self._record_call(
                "proposed",
                {
                    "protocol": protocol,
                    "interface": "propose_observe",
                    "candidate_count": len(candidates),
                    "study_state": dict(study_state),
                },
            )
            return candidates
        raise TypeError(
            f"Method {self.method_id!r} must implement propose/observe, start/poll/finalize, or command batch protocol."
        )

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        if self.method is not None and hasattr(self.method, "observe"):
            self.method.observe(observations)
        elif self.method is not None and hasattr(self.method, "intervene"):
            self.method.intervene(
                "__latest__",
                {
                    "type": "observations",
                    "observations": observations,
                },
            )
        self._record_call(
            "observed",
            {
                "observation_count": len(observations),
                "statuses": [observation.get("status") for observation in observations],
            },
        )

    def _command_batch_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
        implementation = self.definition.get("implementation", {})
        runtime = self.definition.get("runtime", {}) or {}
        call_id = f"method-call-{uuid.uuid4().hex[:12]}"
        call_dir = self.evidence_store.run_dir / "method_calls" / call_id
        call_dir.mkdir(parents=True, exist_ok=False)
        input_path = call_dir / "request.json"
        output_path = call_dir / "response.json"
        stdout_path = call_dir / "stdout.log"
        stderr_path = call_dir / "stderr.log"
        request = self._batch_request(call_id, n_candidates, study_state, evidence_view, call_dir)
        input_path.write_text(json.dumps(request, indent=2, sort_keys=True), encoding="utf-8")

        command = _format_command(list(implementation["command"]), input_path, output_path)
        command_prefix = list(runtime.get("commandPrefix", []) or [])
        full_command = command_prefix + command
        started = utc_now_iso()
        timeout = int(self.definition.get("resourceProfile", {}).get("timeoutSeconds", 0) or 0) or None
        build_metadata = self._ensure_container_runtime(runtime)
        if _command_uses_placeholders(implementation["command"]):
            completed = _run_method_command(
                full_command,
                runtime,
                self.study_spec,
                call_id,
                call_dir,
                timeout=timeout,
                input_text=None,
            )
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != 0:
                raise RuntimeError(f"Method command failed with exit code {completed.returncode}: {completed.stderr.strip()}")
            if not output_path.exists():
                raise FileNotFoundError(f"Method command did not write response file: {output_path}")
            response = json.loads(output_path.read_text(encoding="utf-8"))
        else:
            completed = _run_method_command(
                full_command,
                runtime,
                self.study_spec,
                call_id,
                call_dir,
                input=json.dumps(request),
                timeout=timeout,
            )
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != 0:
                raise RuntimeError(f"Method command failed with exit code {completed.returncode}: {completed.stderr.strip()}")
            response = json.loads(completed.stdout or "{}")
            output_path.write_text(json.dumps(response, indent=2, sort_keys=True), encoding="utf-8")

        method_events = response.get("method_events", []) or []
        for event in method_events:
            if isinstance(event, dict):
                self._record_event(dict(event))
        candidates = _extract_candidate_artifacts(response)
        self._record_call(
            "completed",
            {
                "protocol": "optpilot.method.batch.v1",
                "interface": "command",
                "call_id": call_id,
                "runtime": _runtime_summary(runtime, build_metadata),
                "candidate_count": len(candidates),
                "command": full_command,
                "input_path": str(input_path),
                "output_path": str(output_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "started_at": started,
            },
        )
        return candidates

    def _ensure_container_runtime(self, runtime: Dict[str, Any]) -> Dict[str, Any]:
        if str(runtime.get("type", "host") or "host") != "container":
            return {}
        build = runtime.get("build")
        if not build:
            return {}
        if not isinstance(build, dict):
            raise ValueError("method runtime.build must be an object.")
        image = _method_container_image(runtime, self.study_spec)
        key = (str(runtime.get("containerExecutable", "docker")), image, json.dumps(build, sort_keys=True))
        if key in self._built_container_images:
            return {"configured": True, "status": "already_built", "image": image}
        metadata = build_container_image(
            executable=str(runtime.get("containerExecutable", "docker")),
            image=image,
            build=build,
            base_dir=self.study_spec.base_dir,
            timeout=int(build.get("timeoutSeconds", 0) or runtime.get("buildTimeoutSeconds", 0) or 0) or None,
        )
        self._built_container_images.add(key)
        payload = {
            "runtime": "container",
            "image": image,
            "command": metadata.get("command", []),
            "returncode": metadata.get("returncode"),
        }
        self._record_call("runtime_built", payload)
        return {"configured": True, "status": "built", **payload}

    def _batch_request(self, call_id: str, n_candidates: int, study_state: Dict[str, Any], evidence_view, call_dir: Path) -> Dict[str, Any]:
        return {
            "protocol": "optpilot.method.batch.v1",
            "request_id": call_id,
            "n_candidates": n_candidates,
            "study_state": dict(study_state),
            "objective": dict(self.study_spec.objective),
            "candidate_context": dict(self.study_spec.primary_artifact.get("candidateContext", {})),
            "evidence": evidence_view.decision_context() if evidence_view else {},
            "runtime_context": {
                **dict(study_state.get("runtime_context", {})),
                "method_workspace": str(call_dir),
            },
            "config": dict(self.definition.get("config", {})),
        }

    def _lifecycle_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
        config = self.definition.get("config", {})
        max_polls = int(config.get("maxPolls", 100))
        poll_interval_seconds = float(config.get("pollIntervalSeconds", 0.0))
        method_input = {
            "method_id": self.method_id,
            "method_definition": dict(self.definition),
            "study_state": dict(study_state),
            "study_spec": dict(self.study_spec.raw),
            "n_candidates": n_candidates,
            "evidence_context": evidence_view.decision_context() if evidence_view else {},
            "runtime_context": dict(study_state.get("runtime_context", {})),
        }
        handle = self.method.start(method_input)
        self._record_call(
            "started",
            {
                "protocol": self.definition.get("implementation", {}).get("protocol", "optpilot.method.batch.v1"),
                "interface": "lifecycle",
                "handle": handle,
                "n_candidates": n_candidates,
                "study_state": dict(study_state),
            },
        )

        last_status: Dict[str, Any] = {}
        for poll_index in range(max_polls):
            last_status = self.method.poll(handle) or {}
            self._record_call(
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
                f"Method {self.method_id!r} did not reach a terminal state after {max_polls} polls."
            )

        state = _status_state(last_status)
        if state not in SUCCESS_STATES:
            raise RuntimeError(f"Method {self.method_id!r} ended with state {state!r}.")

        result = self.method.finalize(handle)
        candidates = _extract_candidate_artifacts(result)
        self._record_call(
            "finalized",
            {
                "handle": handle,
                "candidate_count": len(candidates),
                "status": dict(last_status),
            },
        )
        return candidates

    def _record_call(self, event: str, payload: Dict[str, Any]) -> None:
        if not hasattr(self.evidence_store, "record_method_call"):
            return
        self.evidence_store.record_method_call(
            {
                "method_id": self.method_id,
                "event": event,
                "payload": payload,
                "created_at": utc_now_iso(),
            }
        )

    def _record_event(self, payload: Dict[str, Any]) -> None:
        if not hasattr(self.evidence_store, "record_method_event"):
            return
        self.evidence_store.record_method_event(
            {
                "method_id": self.method_id,
                "event": payload.get("event", payload.get("level", "event")),
                "payload": payload,
                "created_at": utc_now_iso(),
            }
        )


def _call_with_optional_evidence(function, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
    parameters = inspect.signature(function).parameters
    if len(parameters) >= 3:
        return function(n_candidates, study_state, evidence_view)
    return function(n_candidates, study_state)


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
        raise TypeError("Method must return a list or a dict containing an artifacts list.")
    return [dict(candidate) for candidate in candidates]


def _format_command(command: List[str], input_path: Path, output_path: Path) -> List[str]:
    return [
        item.replace("{input_file}", str(input_path)).replace("{output_file}", str(output_path))
        for item in command
    ]


def _command_uses_placeholders(command: List[str]) -> bool:
    return any("{input_file}" in item or "{output_file}" in item for item in command)


def _runtime_cwd(runtime: Dict[str, Any], base_dir: Path) -> Path:
    workdir = runtime.get("workdir") or runtime.get("project")
    if not workdir:
        return Path.cwd()
    path = Path(str(workdir))
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _run_method_command(
    command: List[str],
    runtime: Dict[str, Any],
    study_spec,
    call_id: str,
    call_dir: Path,
    *,
    timeout: Optional[int],
    input_text: Optional[str] = None,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    if input_text is None:
        input_text = input
    runtime_type = str(runtime.get("type", "host") or "host")
    cwd = _runtime_cwd(runtime, study_spec.base_dir)
    if runtime_type in {"host", "process", "local"}:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=_host_method_env(runtime),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    if runtime_type == "container":
        return subprocess.run(
            _method_container_command(command, runtime, study_spec, call_id, call_dir, cwd, input_text=input_text),
            cwd=str(Path.cwd()),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    raise ValueError(f"Unsupported method runtime.type: {runtime_type!r}")


def _method_container_command(
    command: List[str],
    runtime: Dict[str, Any],
    study_spec,
    call_id: str,
    call_dir: Path,
    cwd: Path,
    *,
    input_text: Optional[str],
) -> List[str]:
    image = _method_container_image(runtime, study_spec)
    container_executable = str(runtime.get("containerExecutable", "docker"))
    container_name = _safe_container_name(f"optpilot-method-{call_id}")
    docker_command = [
        container_executable,
        "run",
        "--rm",
        "--name",
        container_name,
    ]
    if input_text is not None:
        docker_command.append("-i")
    docker_command.extend(network_args(str(runtime.get("networkPolicy", "disabled"))))
    for host_path, mode in _method_container_mounts(runtime, study_spec, call_dir, cwd, command):
        docker_command.extend(["-v", f"{host_path}:{host_path}:{mode}"])
    docker_command.extend(["-w", str(cwd)])
    env = {
        "PYTHONPATH": _container_pythonpath(),
        "PYTHONDONTWRITEBYTECODE": "1",
        **{str(key): str(value) for key, value in runtime.get("env", {}).items()},
        **{str(key): str(value) for key, value in runtime.get("environmentVariables", {}).items()},
        **_host_env_passthrough(runtime),
    }
    for key, value in env.items():
        docker_command.extend(["-e", f"{key}={value}"])
    docker_command.extend([str(item) for item in runtime.get("extraArgs", []) or []])
    docker_command.extend([str(image), *command])
    return docker_command


def _method_container_mounts(
    runtime: Dict[str, Any],
    study_spec,
    call_dir: Path,
    cwd: Path,
    command: List[str],
) -> List[tuple[str, str]]:
    mounts: List[tuple[Path, str]] = [
        (Path.cwd().resolve(), "ro"),
        (study_spec.base_dir.resolve(), "ro"),
        (cwd.resolve(), "ro"),
        (call_dir.resolve(), "rw"),
    ]
    for value in runtime.get("readOnlyMounts", []) or []:
        mounts.append((Path(str(value)).expanduser().resolve(), "ro"))
    for value in runtime.get("writableMounts", []) or []:
        mounts.append((Path(str(value)).expanduser().resolve(), "rw"))
    for item in command:
        path = Path(str(item))
        if path.is_absolute() and path.exists():
            mounts.append((path if path.is_dir() else path.parent, "ro"))
    return [(str(path), mode) for path, mode in dedupe_mounts(mounts)]


def _container_pythonpath() -> str:
    return container_pythonpath()


def _host_method_env(runtime: Dict[str, Any]) -> Dict[str, str]:
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in runtime.get("env", {}).items()})
    env.update({str(key): str(value) for key, value in runtime.get("environmentVariables", {}).items()})
    env.update(_host_env_passthrough(runtime))
    return env


def _host_env_passthrough(runtime: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key in runtime.get("envFromHost", []) or []:
        key = str(key)
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def _safe_container_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value.lower())


def _method_container_image(runtime: Dict[str, Any], study_spec) -> str:
    build = runtime.get("build", {}) if isinstance(runtime.get("build", {}), dict) else {}
    image = (
        runtime.get("image")
        or runtime.get("runtimeImage")
        or build.get("tag")
        or study_spec.method.get("resourceProfile", {}).get("runtimeImage")
    )
    if not image:
        raise ValueError("Container method runtime requires runtime.image, runtime.build.tag, or method.resourceProfile.runtimeImage.")
    return str(image)


def _runtime_summary(runtime: Dict[str, Any], build_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    runtime_type = str(runtime.get("type", "host") or "host")
    summary = {"type": runtime_type}
    if runtime_type == "container":
        build = runtime.get("build", {}) if isinstance(runtime.get("build", {}), dict) else {}
        summary.update(
            {
                "container_executable": runtime.get("containerExecutable", "docker"),
                "container_image": runtime.get("image") or runtime.get("runtimeImage") or build.get("tag"),
                "network_policy": runtime.get("networkPolicy", "disabled"),
            }
        )
        if build_metadata:
            summary["build"] = {
                key: value
                for key, value in build_metadata.items()
                if key in {"configured", "status", "image", "command", "returncode"}
            }
    if runtime.get("workdir") or runtime.get("project"):
        summary["workdir"] = runtime.get("workdir") or runtime.get("project")
    return summary
