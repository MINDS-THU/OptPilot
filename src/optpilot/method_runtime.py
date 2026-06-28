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
from .setup import apply_prepared_env


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
        self._python_worker: Optional[_PythonMethodWorkerClient] = None

    def propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view=None) -> List[Dict[str, Any]]:
        implementation = self.definition.get("implementation", {})
        protocol = implementation.get("protocol", "optpilot.method.batch.v1")
        if implementation.get("type") == "python" and self.method is None:
            return self._python_worker_propose(n_candidates, study_state, evidence_view)
        if protocol == "optpilot.method.session.v1":
            return self._session_propose(n_candidates, study_state, evidence_view)
        if protocol != "optpilot.method.batch.v1":
            raise NotImplementedError(f"Method protocol {protocol!r} is not implemented.")

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

    def _session_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view=None) -> List[Dict[str, Any]]:
        implementation = self.definition.get("implementation", {})
        if implementation.get("type") != "python":
            raise TypeError("optpilot.method.session.v1 currently requires a Python method implementation.")
        if self.method is None:
            raise TypeError(f"Method {self.method_id!r} has no Python implementation instance.")
        session = MethodSession(
            method_id=self.method_id,
            definition=self.definition,
            study_spec=self.study_spec,
            study_state=study_state,
            evidence_view=evidence_view,
            n_candidates=n_candidates,
            record_event=self._record_event,
        )
        if hasattr(self.method, "run"):
            result = self.method.run(session)
        elif callable(self.method):
            result = self.method(session)
        else:
            raise TypeError(f"Session method {self.method_id!r} must implement run(session) or be callable.")
        candidates = [*session.candidates, *_extract_candidates(result)]
        self._record_call(
            "completed",
            {
                "protocol": "optpilot.method.session.v1",
                "interface": "session",
                "candidate_count": len(candidates),
                "study_state": dict(study_state),
                "events": len(session.events),
            },
        )
        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        implementation = self.definition.get("implementation", {})
        if implementation.get("type") == "python" and self.method is None:
            response = self._ensure_python_worker().request(
                {
                    "op": "observe",
                    "observations": observations,
                }
            )
            self._record_worker_response(response)
            return
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

    def close(self) -> None:
        if self._python_worker is not None:
            self._python_worker.close()
            self._python_worker = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _python_worker_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view=None) -> List[Dict[str, Any]]:
        response = self._ensure_python_worker().request(
            {
                "op": "propose",
                "n_candidates": n_candidates,
                "study_state": dict(study_state),
                "evidence": evidence_view.decision_context() if evidence_view else {},
            }
        )
        self._record_worker_response(response)
        return [dict(candidate) for candidate in response.get("candidates", []) or []]

    def _ensure_python_worker(self) -> "_PythonMethodWorkerClient":
        if self._python_worker is not None:
            return self._python_worker
        runtime = _runtime_with_default_workdir(self.definition.get("runtime", {}) or {}, self.definition)
        build_metadata = self._ensure_container_runtime(runtime)
        self._python_worker = _PythonMethodWorkerClient(
            definition=self.definition,
            runtime=runtime,
            evidence_store=self.evidence_store,
            study_spec=self.study_spec,
            build_metadata=build_metadata,
        )
        self._python_worker.start()
        return self._python_worker

    def _record_worker_response(self, response: Dict[str, Any]) -> None:
        for event in response.get("method_events", []) or []:
            if isinstance(event, dict):
                self._record_event(dict(event))
        for call in response.get("calls", []) or []:
            if not isinstance(call, dict):
                continue
            self._record_call(str(call.get("event", "completed")), dict(call.get("payload", {}) or {}))

    def _command_batch_propose(self, n_candidates: int, study_state: Dict[str, Any], evidence_view) -> List[Dict[str, Any]]:
        implementation = self.definition.get("implementation", {})
        runtime = _runtime_with_default_workdir(self.definition.get("runtime", {}) or {}, self.definition)
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
        candidates = _extract_candidates(response)
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
        candidate_context = dict(self.study_spec.candidate.get("context", {}))
        runtime_context = {
            **dict(study_state.get("runtime_context", {})),
            "method_workspace": str(call_dir),
        }
        return {
            "protocol": "optpilot.method.batch.v1",
            "request_id": call_id,
            "n_candidates": n_candidates,
            "candidate": dict(candidate_context.get("candidate", {})),
            "methodContext": dict(candidate_context.get("methodContext", {})),
            "study_state": dict(study_state),
            "objective": dict(self.study_spec.objective),
            "candidate_context": candidate_context,
            "evidence": evidence_view.decision_context() if evidence_view else {},
            "runtime_context": runtime_context,
            "settings": dict(self.definition.get("settings", self.definition.get("config", {}))),
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
        candidates = _extract_candidates(result)
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


class _PythonMethodWorkerClient:
    def __init__(
        self,
        *,
        definition: Dict[str, Any],
        runtime: Dict[str, Any],
        evidence_store,
        study_spec,
        build_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.definition = definition
        self.runtime = runtime
        self.evidence_store = evidence_store
        self.study_spec = study_spec
        self.build_metadata = build_metadata or {}
        self.worker_id = f"method-worker-{uuid.uuid4().hex[:12]}"
        self.worker_dir = evidence_store.run_dir / "method_runtime" / self.worker_id
        self.worker_dir.mkdir(parents=True, exist_ok=False)
        self.init_path = self.worker_dir / "init.json"
        self.stderr_path = self.worker_dir / "stderr.log"
        self.process: Optional[subprocess.Popen] = None
        self._stderr_handle = None

    def start(self) -> None:
        seed = int(self.study_spec.reproducibility.get("seedPolicy", {}).get("globalSeed", 0) or 0)
        self.init_path.write_text(
            json.dumps(
                {
                    "method_definition": self.definition,
                    "study_spec_path": str(self.study_spec.path),
                    "study_spec_raw": self.study_spec.raw,
                    "run_dir": str(self.evidence_store.run_dir),
                    "seed": seed,
                    "build_metadata": self.build_metadata,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        runtime_type = str(self.runtime.get("type", "process") or "process")
        cwd = _runtime_cwd(self.runtime, Path(str(self.definition.get("configBaseDir") or self.study_spec.base_dir)))
        if runtime_type == "container":
            command = _python_method_worker_container_command(
                self.runtime,
                self.study_spec,
                self.worker_id,
                self.init_path,
                cwd,
                self.definition.get("implementation", {}),
            )
            env = os.environ.copy()
            popen_cwd = Path.cwd()
        elif runtime_type in {"process", "host", "local"}:
            python_executable = str(self.runtime.get("workerPythonExecutable") or sys.executable)
            command = [python_executable, "-m", "optpilot.method_worker", str(self.init_path)]
            env = _python_method_worker_env(self.runtime, self.definition.get("implementation", {}))
            popen_cwd = cwd
        else:
            raise ValueError(f"Unsupported Python method runtime.type: {runtime_type!r}")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=str(popen_cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_handle,
            text=True,
            bufsize=1,
        )

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("Python method worker is not running.")
        self.process.stdin.write(json.dumps(payload, sort_keys=True) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            returncode = self.process.poll()
            stderr = self.stderr_path.read_text(encoding="utf-8") if self.stderr_path.exists() else ""
            raise RuntimeError(f"Python method worker stopped unexpectedly with code {returncode}: {stderr.strip()}")
        response = json.loads(line)
        if not response.get("ok"):
            error = response.get("error", {})
            raise RuntimeError(f"Python method worker failed: {error.get('type', 'Error')}: {error.get('message', '')}")
        return response

    def close(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                try:
                    self.request({"op": "shutdown"})
                except Exception:
                    self.process.kill()
                self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self._stderr_handle is not None:
                self._stderr_handle.close()
            self.process = None


class MethodSession:
    """Small active proposal surface for Python session methods."""

    def __init__(
        self,
        *,
        method_id: str,
        definition: Dict[str, Any],
        study_spec,
        study_state: Dict[str, Any],
        evidence_view,
        n_candidates: int,
        record_event,
    ):
        self.method_id = method_id
        self.definition = definition
        self.study_spec = study_spec
        self.study_state = dict(study_state)
        self.n_candidates = n_candidates
        self._evidence_view = evidence_view
        self._record_event = record_event
        self._candidates: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []

    @property
    def evidence(self) -> Dict[str, Any]:
        if self._evidence_view is None:
            return {}
        return self._evidence_view.decision_context()

    @property
    def candidate_context(self) -> Dict[str, Any]:
        return dict(self.study_spec.candidate.get("context", {}))

    @property
    def config(self) -> Dict[str, Any]:
        return dict(self.definition.get("config", {}))

    @property
    def candidates(self) -> List[Dict[str, Any]]:
        return [dict(candidate) for candidate in self._candidates]

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [dict(event) for event in self._events]

    def submit(self, candidate_or_candidates: Any) -> None:
        if isinstance(candidate_or_candidates, dict) and not (
            "candidates" in candidate_or_candidates
        ):
            self._candidates.append(dict(candidate_or_candidates))
            return
        self._candidates.extend(_extract_candidates(candidate_or_candidates))

    def event(self, payload: Dict[str, Any]) -> None:
        event = dict(payload)
        self._events.append(event)
        self._record_event(event)


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


def _extract_candidates(result: Any) -> List[Dict[str, Any]]:
    if isinstance(result, dict):
        candidates = result.get("candidates", [])
    else:
        candidates = result
    if candidates is None:
        return []
    if not isinstance(candidates, list):
        raise TypeError("Method must return a list or a dict containing a candidates list.")
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
        return base_dir.resolve()
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


def _runtime_with_default_workdir(runtime: Dict[str, Any], definition: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(runtime or {})
    result.setdefault("type", "process")
    if not result.get("workdir") and definition.get("configBaseDir"):
        result["workdir"] = str(definition["configBaseDir"])
    return result


def _python_method_worker_env(runtime: Dict[str, Any], implementation: Dict[str, Any]) -> Dict[str, str]:
    env = _host_method_env(runtime)
    entries = []
    entries.extend(str(path) for path in implementation.get("pythonPath", []) or [] if path)
    entries.append(str(Path(__file__).resolve().parents[1]))
    entries.append(str(Path.cwd().resolve()))
    existing = env.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _python_method_worker_container_command(
    runtime: Dict[str, Any],
    study_spec,
    worker_id: str,
    init_path: Path,
    cwd: Path,
    implementation: Dict[str, Any],
) -> List[str]:
    image = _method_container_image(runtime, study_spec)
    container_executable = str(runtime.get("containerExecutable", "docker"))
    container_name = _safe_container_name(f"optpilot-{worker_id}")
    python_executable = str(runtime.get("pythonExecutable", "python"))
    command = [python_executable, "-m", "optpilot.method_worker", str(init_path)]
    docker_command = [
        container_executable,
        "run",
        "--rm",
        "-i",
        "--name",
        container_name,
    ]
    docker_command.extend(network_args(str(runtime.get("networkPolicy", "disabled"))))
    for host_path, mode in _method_container_mounts(runtime, study_spec, init_path.parent, cwd, command):
        docker_command.extend(["-v", f"{host_path}:{host_path}:{mode}"])
    docker_command.extend(["-w", str(cwd)])
    pythonpath_entries = [container_pythonpath()]
    pythonpath_entries.extend(str(path) for path in implementation.get("pythonPath", []) or [] if path)
    env = {
        "PYTHONPATH": os.pathsep.join(pythonpath_entries),
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
        (Path(__file__).resolve().parents[1], "ro"),
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
    env = apply_prepared_env(os.environ.copy(), runtime.get("preparedEnv", {}))
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
        or build.get("tag")
    )
    if not image:
        raise ValueError("Container method runtime requires runtime.image or runtime.build.tag.")
    return str(image)


def _runtime_summary(runtime: Dict[str, Any], build_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    runtime_type = str(runtime.get("type", "host") or "host")
    summary = {"type": runtime_type}
    if runtime_type == "container":
        build = runtime.get("build", {}) if isinstance(runtime.get("build", {}), dict) else {}
        summary.update(
            {
                "container_executable": runtime.get("containerExecutable", "docker"),
                "container_image": runtime.get("image") or build.get("tag"),
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
