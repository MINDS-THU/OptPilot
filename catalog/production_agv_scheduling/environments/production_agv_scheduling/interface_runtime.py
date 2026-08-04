"""Ephemeral candidate execution and telemetry replay for the Unity interface."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from mqtt_bridge import LocalMQTTBroker


MAX_REQUEST_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 384 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_FILES = 128
MAX_ERROR_BYTES = 8 * 1024
MAX_SIMULATION_HORIZON = 500.0
MAX_WORKER_SECONDS = 65.0
TOPIC_ROOT = "optpilot_offline"
VIEWER_READY_TOPIC = f"{TOPIC_ROOT}/kpi/status"

_PRIMARY_FILES = ("scheduler.py", "param_estimator.py")


class InterfaceRequestError(ValueError):
    """User-correctable interface request error."""


class CandidateReplayManager:
    """Own one-at-a-time bounded candidate runs and visual replays."""

    def __init__(
        self,
        *,
        environment_root: Path,
        broker: LocalMQTTBroker,
        candidate_root: Path | None = None,
        runtime_root: Path | None = None,
        viewer_wait_seconds: float = 12.0,
    ) -> None:
        self.environment_root = environment_root.resolve()
        self.broker = broker
        self.candidate_root = (
            candidate_root.resolve()
            if candidate_root is not None
            else discover_candidate_root(self.environment_root)
        )
        self._owned_runtime = runtime_root is None
        self.runtime_root = (
            _create_runtime_root()
            if runtime_root is None
            else runtime_root.resolve()
        )
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.viewer_wait_seconds = max(0.0, float(viewer_wait_seconds))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._last_events: Path | None = None
        self._last_result: dict[str, Any] | None = None
        self._run_sequence = 0
        self._state: dict[str, Any] = {
            "events_delivered": 0,
            "events_published": 0,
            "message": "Ready to run a candidate.",
            "run_id": None,
            "status": "idle",
        }

    def candidate_payload(self) -> dict[str, Any]:
        """Return the two primary editable files and bounded metadata."""

        files = _validated_candidate_files(self.candidate_root)
        primary = {
            name: (self.candidate_root / name).read_text(encoding="utf-8")
            for name in _PRIMARY_FILES
        }
        additional = [
            path.as_posix()
            for path in files
            if path.as_posix() not in _PRIMARY_FILES
        ]
        defaults = json.loads(
            (self.environment_root / "settings" / "smoke.json").read_text(
                encoding="utf-8"
            )
        )
        return {
            "additional_files": additional,
            "candidate": primary,
            "defaults": {
                "disable_faults": bool(defaults["disable_faults"]),
                "replay_speed": 1.0,
                "seed": int(defaults["seeds"][0]),
                "simulation_horizon": float(defaults["simulation_horizon"]),
                "time_step": float(defaults["time_step"]),
            },
            "limits": {
                "max_candidate_bytes": MAX_CANDIDATE_BYTES,
                "max_simulation_horizon": MAX_SIMULATION_HORIZON,
                "max_source_bytes": MAX_SOURCE_BYTES,
            },
            "source": (
                "selected candidate"
                if self.candidate_root != self.environment_root / "initial"
                else "initial policy"
            ),
            "topic_root": TOPIC_ROOT,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._state)
            result["viewer_clients"] = self.broker.client_count
            result["viewer_ready"] = self.broker.has_subscriber(VIEWER_READY_TOPIC)
            if self._last_result is not None:
                result["result"] = copy.deepcopy(self._last_result)
            return result

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        source, settings, seed, replay_speed = _validated_run_request(request)
        with self._lock:
            if self._active_locked():
                raise InterfaceRequestError(
                    "A candidate is already running or replaying. Stop it first."
                )
            self._run_sequence += 1
            run_id = f"visual-{self._run_sequence:04d}"
            run_root = self.runtime_root / run_id
            run_root.mkdir(parents=False, exist_ok=False)
            candidate = run_root / "candidate"
            _stage_candidate(self.candidate_root, candidate, source)
            settings_path = run_root / "settings.json"
            settings_path.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n",
                encoding="utf-8",
            )
            events = run_root / "events.jsonl"
            result = run_root / "result.json"
            trace = run_root / "trace.db"
            worker = self.environment_root / "interface_worker.py"
            command = [
                sys.executable,
                str(worker),
                "--candidate-dir",
                str(candidate),
                "--settings",
                str(settings_path),
                "--seed",
                str(seed),
                "--events",
                str(events),
                "--result",
                str(result),
                "--trace",
                str(trace),
            ]
            stop_event = threading.Event()
            self._stop_event = stop_event
            self.broker.reset_viewer_generation(run_id)
            self._last_result = None
            self._state = {
                "events_delivered": 0,
                "events_published": 0,
                "message": "Evaluating the candidate in an offline child process.",
                "run_id": run_id,
                "status": "running",
            }
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.environment_root,
                    env=_worker_environment(
                        self.environment_root,
                        pycache_root=run_root / "pycache",
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
                self._process = process
                if process.stderr is None:  # pragma: no cover - PIPE invariant
                    raise RuntimeError("Candidate worker stderr pipe was not created.")
                stderr_drain = _BoundedPipeDrain(
                    process.stderr,
                    limit=MAX_ERROR_BYTES,
                )
            except Exception:
                self._state.update(
                    {
                        "message": "The candidate worker could not be started.",
                        "status": "failed",
                    }
                )
                raise
            thread = threading.Thread(
                target=self._monitor_run,
                args=(
                    run_id,
                    events,
                    result,
                    replay_speed,
                    stop_event,
                    stderr_drain,
                    process,
                ),
                name=f"agv-interface-{run_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self.state()

    def replay(self, request: dict[str, Any]) -> dict[str, Any]:
        speed = _number(request.get("replay_speed", 1.0), "replay_speed", 1.0, 1.0)
        with self._lock:
            if self._active_locked():
                raise InterfaceRequestError(
                    "A candidate is already running or replaying. Stop it first."
                )
            if self._last_events is None or not self._last_events.is_file():
                raise InterfaceRequestError("Run a candidate before requesting replay.")
            self._run_sequence += 1
            run_id = f"replay-{self._run_sequence:04d}"
            events = self._last_events
            stop_event = threading.Event()
            self._stop_event = stop_event
            self.broker.reset_viewer_generation(run_id)
            self._state = {
                "events_delivered": 0,
                "events_published": 0,
                "message": "Preparing the recorded telemetry replay.",
                "run_id": run_id,
                "status": "waiting_for_viewer",
            }
            thread = threading.Thread(
                target=self._replay_events,
                args=(run_id, events, speed, stop_event),
                name=f"agv-interface-{run_id}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            process = self._process
            if process is not None and process.poll() is None:
                _terminate_process(process)
            if self._state["status"] in {
                "running",
                "waiting_for_viewer",
                "replaying",
            }:
                self._state.update(
                    {
                        "message": "Candidate run stopped.",
                        "status": "stopped",
                    }
                )
            return self.state()

    def close(self) -> None:
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if self._owned_runtime:
            shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _monitor_run(
        self,
        run_id: str,
        events_path: Path,
        result_path: Path,
        replay_speed: float,
        stop_event: threading.Event,
        stderr_drain: "_BoundedPipeDrain",
        process: subprocess.Popen[bytes],
    ) -> None:
        timed_out = False
        try:
            process.wait(timeout=MAX_WORKER_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(process)
        finally:
            stderr_drain.join(timeout=2.0)
            with self._lock:
                if self._process is process:
                    self._process = None
        stderr = stderr_drain.text()
        if stop_event.is_set():
            return
        if timed_out:
            self._fail_run(run_id, "Candidate evaluation exceeded 65 seconds.", stderr)
            return
        if process.returncode != 0 or not result_path.is_file():
            message = "Candidate evaluation failed."
            self._fail_run(run_id, message, stderr)
            return
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise TypeError("worker result is not an object")
        except (OSError, TypeError, json.JSONDecodeError) as error:
            self._fail_run(run_id, "Candidate result could not be read.", str(error))
            return
        with self._lock:
            if self._state.get("run_id") != run_id:
                return
            self._last_events = events_path
            self._last_result = result
            self._state.update(
                {
                    "event_count": int(result.get("event_count", 0)),
                    "events_truncated": bool(result.get("events_truncated", False)),
                    "message": "Candidate evaluated; waiting briefly for the Unity viewer.",
                    "status": "waiting_for_viewer",
                }
            )
        self._replay_events(run_id, events_path, replay_speed, stop_event)

    def _replay_events(
        self,
        run_id: str,
        events_path: Path,
        speed: float,
        stop_event: threading.Event,
    ) -> None:
        if not self._wait_for_viewer(run_id, speed, stop_event):
            return
        replay_time: float | None = None
        delivered_events = 0
        published = 0
        try:
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if stop_event.is_set():
                        return
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("telemetry record is not an object")
                    topic = record.get("topic")
                    payload = record.get("payload")
                    simulation_time = record.get("simulation_time")
                    if not isinstance(topic, str) or not topic.startswith(TOPIC_ROOT + "/"):
                        raise ValueError("telemetry topic is outside the local root")
                    if not isinstance(payload, str):
                        raise ValueError("telemetry payload is not text")
                    if isinstance(simulation_time, (int, float)) and not isinstance(
                        simulation_time, bool
                    ):
                        current_time = float(simulation_time)
                        if math.isfinite(current_time):
                            current_time = max(
                                current_time,
                                replay_time if replay_time is not None else current_time,
                            )
                        if math.isfinite(current_time) and replay_time is not None:
                            delay = max(0.0, current_time - replay_time) / speed
                            if stop_event.wait(delay):
                                return
                        replay_time = current_time
                    while True:
                        if not self._wait_for_viewer(run_id, speed, stop_event):
                            return
                        viewer_subscribed_to_topic = self.broker.has_subscriber(topic)
                        delivered = self.broker.publish(topic, payload, retain=True)
                        if delivered > 0 or not viewer_subscribed_to_topic:
                            break
                        with self._lock:
                            if self._state.get("run_id") != run_id:
                                return
                            self._state.update(
                                {
                                    "message": (
                                        "The 3D viewer disconnected. Replay is paused "
                                        "and will resume when it reconnects."
                                    ),
                                    "status": "waiting_for_viewer",
                                }
                            )
                        if stop_event.wait(0.1):
                            return
                    published += 1
                    if delivered > 0:
                        delivered_events += 1
                    with self._lock:
                        if self._state.get("run_id") != run_id:
                            return
                        self._state["events_published"] = published
                        self._state["events_delivered"] = delivered_events
                        if replay_time is not None and math.isfinite(replay_time):
                            self._state["simulation_time"] = replay_time
                        self._state["message"] = self._replay_progress_message(
                            speed,
                            replay_time,
                        )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._fail_run(run_id, "Recorded telemetry could not be replayed.", str(error))
            return
        with self._lock:
            if self._state.get("run_id") != run_id or stop_event.is_set():
                return
            last_result = getattr(self, "_last_result", None)
            motion_events = (
                last_result.get("motion_event_count")
                if isinstance(last_result, dict)
                else None
            )
            if motion_events == 0:
                message = (
                    "Replay complete, but this run contained no AGV movement. "
                    "Increase the horizon (30 is recommended) or inspect the candidate."
                )
            else:
                message = "Replay complete. You can replay it again or edit the candidate."
            self._state.update(
                {
                    "events_published": published,
                    "events_delivered": delivered_events,
                    "message": message,
                    "status": "completed",
                }
            )

    def _wait_for_viewer(
        self,
        run_id: str,
        speed: float,
        stop_event: threading.Event,
    ) -> bool:
        """Pause without consuming telemetry until Unity has a live subscription."""

        started = time.monotonic()
        while not self.broker.has_subscriber(VIEWER_READY_TOPIC):
            if stop_event.is_set():
                return False
            elapsed = time.monotonic() - started
            if self.viewer_wait_seconds and elapsed >= self.viewer_wait_seconds:
                message = (
                    "Still loading the 3D viewer. Replay has not started, so no "
                    "animation events will be missed."
                )
            else:
                message = (
                    "Loading the 3D viewer. Replay will start after it subscribes "
                    "to local telemetry."
                )
            with self._lock:
                if self._state.get("run_id") != run_id:
                    return False
                self._state.update(
                    {
                        "message": message,
                        "status": "waiting_for_viewer",
                    }
                )
            if stop_event.wait(0.1):
                return False
        with self._lock:
            if self._state.get("run_id") != run_id or stop_event.is_set():
                return False
            self._state.update(
                {
                    "message": self._replay_progress_message(speed, None),
                    "status": "replaying",
                }
            )
        return True

    def _replay_progress_message(
        self,
        speed: float,
        replay_time: float | None,
    ) -> str:
        last_result = getattr(self, "_last_result", None)
        first_motion = (
            last_result.get("first_motion_time")
            if isinstance(last_result, dict)
            else None
        )
        if (
            replay_time is not None
            and isinstance(first_motion, (int, float))
            and not isinstance(first_motion, bool)
            and math.isfinite(float(first_motion))
            and replay_time < float(first_motion)
        ):
            remaining = max(0.0, float(first_motion) - replay_time) / speed
            return (
                f"Replaying initialization at t={replay_time:g}. First AGV movement "
                f"in about {remaining:g} seconds."
            )
        time_text = "" if replay_time is None else f" (t={replay_time:g})"
        return f"Replaying telemetry at {speed:g}× simulation speed{time_text}."

    def _fail_run(self, run_id: str, message: str, details: str) -> None:
        safe_details = details.encode("utf-8", errors="replace")[-MAX_ERROR_BYTES:].decode(
            "utf-8", errors="replace"
        )
        with self._lock:
            if self._state.get("run_id") != run_id:
                return
            self._state.update(
                {
                    "error": safe_details.strip(),
                    "message": message,
                    "status": "failed",
                }
            )

    def _active_locked(self) -> bool:
        return self._state.get("status") in {
            "running",
            "waiting_for_viewer",
            "replaying",
        }


def discover_candidate_root(environment_root: Path) -> Path:
    authored = os.environ.get("OPTPILOT_CANDIDATE_DIR")
    candidates = []
    if authored:
        candidates.append(Path(authored))
    candidates.append(Path("/optpilot/interface/candidate"))
    candidates.append(environment_root / "initial")
    for candidate in candidates:
        if candidate.is_dir() and all((candidate / name).is_file() for name in _PRIMARY_FILES):
            return candidate.resolve()
    raise FileNotFoundError(
        "No file candidate is available; expected scheduler.py and "
        "param_estimator.py in the contextual candidate mount or initial/."
    )


def _validated_run_request(
    request: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any], int, float]:
    if not isinstance(request, dict):
        raise InterfaceRequestError("Run request must be a JSON object.")
    candidate = request.get("candidate")
    if not isinstance(candidate, dict):
        raise InterfaceRequestError("candidate must be an object.")
    unknown = sorted(set(candidate) - set(_PRIMARY_FILES))
    if unknown:
        raise InterfaceRequestError(f"Unknown primary candidate files: {unknown!r}.")
    source: dict[str, str] = {}
    for name in _PRIMARY_FILES:
        value = candidate.get(name)
        if not isinstance(value, str):
            raise InterfaceRequestError(f"candidate.{name} must be text.")
        if "\x00" in value:
            raise InterfaceRequestError(f"candidate.{name} cannot contain NUL.")
        if len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise InterfaceRequestError(f"candidate.{name} exceeds the source limit.")
        source[name] = value

    options = request.get("options", {})
    if not isinstance(options, dict):
        raise InterfaceRequestError("options must be an object.")
    seed = options.get("seed", 123)
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not -(2**31) <= seed < 2**31
    ):
        raise InterfaceRequestError("seed must be a signed 32-bit integer.")
    horizon = _number(
        options.get("simulation_horizon", 30.0),
        "simulation_horizon",
        1.0,
        MAX_SIMULATION_HORIZON,
    )
    time_step = _number(options.get("time_step", 0.5), "time_step", 0.1, 10.0)
    if time_step > horizon:
        raise InterfaceRequestError("time_step cannot exceed simulation_horizon.")
    replay_speed = _number(
        options.get("replay_speed", 1.0),
        "replay_speed",
        1.0,
        1.0,
    )
    disable_faults = options.get("disable_faults", True)
    if not isinstance(disable_faults, bool):
        raise InterfaceRequestError("disable_faults must be a boolean.")
    settings = {
        "disable_faults": disable_faults,
        "fault_duration": [20.0, 60.0],
        "fault_interval": [120.0, 180.0],
        "order_interval": [10.0, 10.0],
        "repeat_runs": 1,
        "seeds": [seed],
        "simulation_horizon": horizon,
        "stability_lambda": 0.35,
        "time_step": time_step,
        "time_unit": "minutes",
    }
    return source, settings, seed, replay_speed


def _validated_candidate_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        raise FileNotFoundError(f"Candidate root does not exist: {root}")
    files: list[Path] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InterfaceRequestError("Candidate cannot contain symbolic links.")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        allowed = relative.as_posix() in _PRIMARY_FILES or (
            relative.parts and relative.parts[0] == "policy"
        )
        if not allowed or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        size = path.stat().st_size
        total_bytes += size
        files.append(relative)
        if len(files) > MAX_CANDIDATE_FILES or total_bytes > MAX_CANDIDATE_BYTES:
            raise InterfaceRequestError("Candidate exceeds the interface copy limits.")
    for name in _PRIMARY_FILES:
        if Path(name) not in files:
            raise InterfaceRequestError(f"Candidate is missing {name}.")
    return tuple(files)


def _stage_candidate(root: Path, destination: Path, source: dict[str, str]) -> None:
    files = _validated_candidate_files(root)
    destination.mkdir(parents=True, exist_ok=False)
    for relative in files:
        source_path = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
    for name, content in source.items():
        (destination / name).write_text(content, encoding="utf-8")


def _worker_environment(
    environment_root: Path,
    *,
    pycache_root: Path | None = None,
) -> dict[str, str]:
    result = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(environment_root),
        "PYTHONUNBUFFERED": "1",
    }
    if pycache_root is not None:
        result["PYTHONPYCACHEPREFIX"] = str(pycache_root.resolve())
    temporary = os.environ.get("TMPDIR")
    if temporary:
        result["TMPDIR"] = temporary
    return result


class _BoundedPipeDrain:
    """Continuously drain a worker pipe while retaining only its bounded tail."""

    def __init__(self, stream: BinaryIO, *, limit: int) -> None:
        self._stream = stream
        self._limit = max(0, int(limit))
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._drain,
            name="agv-interface-stderr",
            daemon=True,
        )
        self._thread.start()

    def join(self, *, timeout: float) -> None:
        self._thread.join(timeout=timeout)

    def text(self) -> str:
        with self._lock:
            data = bytes(self._buffer)
        return data.decode("utf-8", errors="replace")

    def _drain(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        try:
            while chunk := read(8192):
                if self._limit == 0:
                    continue
                with self._lock:
                    self._buffer.extend(chunk)
                    excess = len(self._buffer) - self._limit
                    if excess > 0:
                        del self._buffer[:excess]
        except OSError:
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


def _create_runtime_root() -> Path:
    candidates = [
        os.environ.get("OPTPILOT_INTERFACE_RUNTIME_ROOT"),
        "/optpilot/interface/workspace",
    ]
    for raw in candidates:
        if not raw:
            continue
        base = Path(raw)
        if base.is_dir() and os.access(base, os.W_OK):
            return Path(tempfile.mkdtemp(prefix="agv-unity-", dir=base))
    return Path(tempfile.mkdtemp(prefix="agv-unity-"))


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InterfaceRequestError(f"{label} must be a number.")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InterfaceRequestError(
            f"{label} must be between {minimum:g} and {maximum:g}."
        )
    return result


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows development fallback
            process.terminate()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
        except OSError:
            pass
