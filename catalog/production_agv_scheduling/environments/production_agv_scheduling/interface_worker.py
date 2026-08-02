"""Bounded child-process runner for one visual candidate replay."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

from simulation_runner import run_policy_once


MAX_EVENTS = 50_000
MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_ADDRESS_SPACE_BYTES = 1536 * 1024 * 1024
MAX_CHILD_PROCESSES = 64


class TelemetryRecorder:
    """Write a bounded visual event stream with single-owner product handoffs."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("w", encoding="utf-8")
        self._agv_statuses: dict[str, dict[str, Any]] = {}
        self._product_agv: dict[str, str] = {}
        self.event_count = 0
        self.total_bytes = 0
        self.truncated = False

    def close(self) -> None:
        self._handle.close()

    def publish(self, topic: str, payload: Any, _qos: int, _retain: bool) -> None:
        if self.truncated:
            return
        topic_text = str(topic)
        payload_text = _payload_text(payload)
        pending = self._handoff_records(topic_text, payload_text)
        pending.append((topic_text, payload_text))

        encoded_records: list[tuple[str, str, str]] = []
        pending_bytes = 0
        for offset, (record_topic, record_payload) in enumerate(pending):
            if len(record_payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                self.truncated = True
                return
            record = {
                "event_sequence": self.event_count + offset,
                "payload": record_payload,
                "simulation_time": _simulation_time(record_payload),
                "topic": record_topic,
            }
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            pending_bytes += len(encoded.encode("utf-8")) + 1
            encoded_records.append((record_topic, record_payload, encoded))
        if (
            self.event_count + len(encoded_records) > MAX_EVENTS
            or self.total_bytes + pending_bytes > MAX_EVENT_BYTES
        ):
            self.truncated = True
            return
        for record_topic, record_payload, encoded in encoded_records:
            self._handle.write(encoded)
            self._handle.write("\n")
            self._track_agv_payload(record_topic, record_payload)
        self.event_count += len(encoded_records)
        self.total_bytes += pending_bytes

    def _handoff_records(self, topic: str, payload: str) -> list[tuple[str, str]]:
        """Clear an AGV payload before a destination claims the same product."""

        if _is_agv_topic(topic):
            return []
        decoded = _decoded_object(payload)
        if decoded is None:
            return []
        release_by_agv: dict[str, set[str]] = {}
        for product_id in _container_product_ids(decoded):
            agv_topic = self._product_agv.get(product_id)
            if agv_topic is not None:
                release_by_agv.setdefault(agv_topic, set()).add(product_id)

        records: list[tuple[str, str]] = []
        for agv_topic, product_ids in release_by_agv.items():
            previous = self._agv_statuses.get(agv_topic)
            if previous is None:
                continue
            status = dict(previous)
            status["payload"] = [
                product_id
                for product_id in _product_ids(previous.get("payload"))
                if product_id not in product_ids
            ]
            records.append(
                (
                    agv_topic,
                    json.dumps(
                        status,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
        return records

    def _track_agv_payload(self, topic: str, payload: str) -> None:
        if not _is_agv_topic(topic):
            return
        decoded = _decoded_object(payload)
        if decoded is None:
            return
        previous = self._agv_statuses.get(topic, {})
        for product_id in _product_ids(previous.get("payload")):
            if self._product_agv.get(product_id) == topic:
                self._product_agv.pop(product_id, None)
        self._agv_statuses[topic] = decoded
        for product_id in _product_ids(decoded.get("payload")):
            self._product_agv[product_id] = topic


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--events", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--trace", required=True)
    arguments = parser.parse_args(argv)

    _apply_resource_limits()
    settings_path = Path(arguments.settings).resolve()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise TypeError("Visual replay settings must be a JSON object.")

    recorder = TelemetryRecorder(Path(arguments.events).resolve())
    try:
        kpi = run_policy_once(
            candidate_dir=Path(arguments.candidate_dir).resolve(),
            settings=settings,
            seed=arguments.seed,
            database_path=Path(arguments.trace).resolve(),
            telemetry_sink=recorder.publish,
        )
    finally:
        recorder.close()
    result = {
        "event_bytes": recorder.total_bytes,
        "event_count": recorder.event_count,
        "events_truncated": recorder.truncated,
        "kpi": kpi,
        "seed": arguments.seed,
    }
    _write_json_atomic(Path(arguments.result).resolve(), result)
    return 0


def _payload_text(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    if isinstance(payload, bytearray):
        return bytes(payload).decode("utf-8")
    if isinstance(payload, str):
        return payload
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _simulation_time(payload: str) -> float | None:
    decoded = _decoded_object(payload)
    if decoded is None:
        return None
    value = decoded.get("timestamp", decoded.get("created_at"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _decoded_object(payload: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _is_agv_topic(topic: str) -> bool:
    return "/agv/" in topic and topic.endswith("/status")


def _container_product_ids(payload: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for field in (
        "buffer",
        "output_buffer",
        "upper_buffer",
        "lower_buffer",
    ):
        result.update(_product_ids(payload.get(field)))
    return result


def _product_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            identifier = item.get("id", item.get("product_id"))
            if isinstance(identifier, str):
                result.append(identifier)
    return result


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _apply_resource_limits() -> None:
    """Apply conservative POSIX limits in addition to the interface container."""

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows development fallback
        return
    limit_specs = (
        ("RLIMIT_CPU", 55, 60),
        ("RLIMIT_FSIZE", 64 * 1024 * 1024, 64 * 1024 * 1024),
        ("RLIMIT_NOFILE", 64, 64),
        ("RLIMIT_AS", MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES),
        ("RLIMIT_NPROC", MAX_CHILD_PROCESSES, MAX_CHILD_PROCESSES),
        ("RLIMIT_CORE", 0, 0),
    )
    for resource_name, desired_soft, desired_hard in limit_specs:
        resource_kind = getattr(resource, resource_name, None)
        if resource_kind is None:
            continue
        try:
            _, current_hard = resource.getrlimit(resource_kind)
            hard = desired_hard
            if current_hard != resource.RLIM_INFINITY:
                hard = min(hard, current_hard)
            soft = min(desired_soft, hard)
            resource.setrlimit(resource_kind, (soft, hard))
        except (OSError, ValueError):
            # The container/provider remains the authoritative boundary when a
            # host kernel does not permit lowering one optional child limit.
            continue


if __name__ == "__main__":
    raise SystemExit(main())
