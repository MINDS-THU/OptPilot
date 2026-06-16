"""Evaluation helper for the SA simulator code-edit example."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


SIMULATION_ARGS = (
    "duration",
    "num_aircraft",
    "pallet_interval",
    "pallet_expiration_time",
    "flight_time",
    "unload_time",
    "return_time",
    "maintenance_time",
)


def evaluate(artifact_spec: Dict[str, Any], instance: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    workspace = Path(artifact_spec.get("workspace") or context["workspace"]).resolve()
    simulator_root = Path(artifact_spec.get("candidateRoot") or (workspace / "simulator")).resolve()
    stdout_path = workspace / "sa_events.jsonl"
    stderr_path = workspace / "sa_stderr.log"
    metrics_path = workspace / "sa_metrics.json"

    command = [sys.executable, "-m", "devs_project.run_strategicairlift_d0"]
    for name in SIMULATION_ARGS:
        if name in instance:
            command.extend([f"--{name}", str(instance[name])])

    timeout_seconds = int(instance.get("timeoutSeconds", 180))
    process = subprocess.Popen(
        command,
        cwd=str(simulator_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_text, stderr_text = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        stdout_text, stderr_text = process.communicate()
        stdout_path.write_text(_coerce_text(stdout_text or exc.stdout), encoding="utf-8")
        stderr_path.write_text(_coerce_text(stderr_text or exc.stderr), encoding="utf-8")
        raise

    stdout_path.write_text(_coerce_text(stdout_text), encoding="utf-8")
    stderr_path.write_text(_coerce_text(stderr_text), encoding="utf-8")

    if process.returncode != 0:
        raise RuntimeError(
            f"SA simulator failed with exit code {process.returncode}: {_coerce_text(stderr_text).strip()}"
        )

    events = _parse_jsonl(_coerce_text(stdout_text).splitlines())
    metrics = _summarize_events(events)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")

    event_counts: Dict[str, int] = {}
    for event in events:
        event_name = str(event.get("event", "unknown"))
        event_counts[event_name] = event_counts.get(event_name, 0) + 1

    return {
        "status": "success",
        "metric_values": metrics,
        "artifacts": [
            {"type": "log", "name": "sa_events", "path": str(stdout_path)},
            {"type": "log", "name": "sa_stderr", "path": str(stderr_path)},
            {"type": "json", "name": "sa_metrics", "path": str(metrics_path)},
        ],
        "event_summary": {
            "adapter": "sa_python_evaluator",
            "simulator_root": str(simulator_root),
            "command": command,
            "event_counts": event_counts,
        },
    }


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _parse_jsonl(lines: Iterable[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise TypeError("Expected each SA event to be a JSON object.")
        events.append(payload)
    return events


def _summarize_events(events: List[Dict[str, Any]]) -> Dict[str, float]:
    delivered_latencies: List[float] = []
    delivered_count = 0
    expired_count = 0
    generated_count = 0

    for event in events:
        event_name = event.get("event")
        payload = event.get("payload") or {}
        if event_name == "pallet_generated":
            generated_count += 1
        elif event_name == "pallet_expired":
            expired_count += 1
        elif event_name == "pallet_delivered":
            delivered_count += 1
            latency = payload.get("latency")
            if latency is not None:
                delivered_latencies.append(float(latency))

    mean_latency = sum(delivered_latencies) / len(delivered_latencies) if delivered_latencies else 0.0
    max_latency = max(delivered_latencies) if delivered_latencies else 0.0
    delivery_ratio = delivered_count / generated_count if generated_count else 0.0
    expiration_ratio = expired_count / generated_count if generated_count else 0.0
    service_score = delivered_count - expired_count - (mean_latency / 100.0)

    return {
        "service_score": float(service_score),
        "delivered_count": float(delivered_count),
        "expired_count": float(expired_count),
        "generated_count": float(generated_count),
        "mean_latency": float(mean_latency),
        "max_latency": float(max_latency),
        "delivery_ratio": float(delivery_ratio),
        "expiration_ratio": float(expiration_ratio),
    }