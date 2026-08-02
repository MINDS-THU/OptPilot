"""Bounded JSONL event tracing for generated xDEVS simulations.

The helper belongs to every generated simulator bundle.  It deliberately has
no OptPilot import: a runner opts in by attaching it to the root xDEVS
``Coordinator`` before ``initialize``.  When the execution environment supplies
``OPTPILOT_SIMULATION_RESULTS_DIR``, atomic-model output events are written to a
portable, bounded ``event_trace.jsonl`` result.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import json
import math
import os
from collections.abc import Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO

from xdevs.abc import Transducer
from xdevs.models import Atomic, Component, Coupled, Port


TRACE_RESULT_FILE = "event_trace.jsonl"
TRACE_RESULTS_ENV = "OPTPILOT_SIMULATION_RESULTS_DIR"
TRACE_SCHEMA = "devs.event-trace.v1"

# Keep the trace below Studio's text-preview ceiling while still retaining a
# useful classroom-scale event history.  The terminal summary is always kept.
DEFAULT_MAX_BYTES = 384 * 1024
_SUMMARY_RESERVE_BYTES = 512
_MAX_VALUE_DEPTH = 6
_MAX_VALUE_NODES = 128
_MAX_COLLECTION_ITEMS = 64
_MAX_STRING_LENGTH = 2048
_MAX_COMPONENT_DEPTH = 64
_MAX_COMPONENT_NAME_LENGTH = 256
_MAX_TYPE_NAME_LENGTH = 512


def _bounded_text(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else value[:maximum] + "…"


def _qualified_type(value: Any) -> str:
    value_type = type(value)
    try:
        module = type.__getattribute__(value_type, "__module__")
        name = type.__getattribute__(value_type, "__qualname__")
    except Exception:
        return "object"
    if type(module) is not str or type(name) is not str:
        return "object"
    return _bounded_text(f"{module}.{name}", _MAX_TYPE_NAME_LENGTH)


def _safe_label(value: Any, *, maximum: int) -> str:
    """Return bounded identity text without invoking arbitrary ``__str__``."""

    if type(value) is str:
        text = value
    elif value is None or type(value) in (bool, int):
        try:
            text = str(value)
        except Exception:
            text = f"<{_qualified_type(value)}>"
    elif type(value) is float:
        text = str(value) if math.isfinite(value) else "null"
    else:
        text = f"<{_qualified_type(value)}>"
    return _bounded_text(text, maximum)


class _JSONSafeValue:
    """Convert an event payload without allowing it to grow without bound."""

    def __init__(self) -> None:
        self.remaining_nodes = _MAX_VALUE_NODES
        self.seen: set[int] = set()

    @staticmethod
    def _text(value: str) -> str:
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return value[:_MAX_STRING_LENGTH] + "…"

    @staticmethod
    def _mapping_key(value: Any) -> str:
        if isinstance(value, str):
            return _JSONSafeValue._text(value)
        if value is None or isinstance(value, (bool, int)):
            return str(value)
        if isinstance(value, float):
            return str(value) if math.isfinite(value) else "null"
        return f"<{_qualified_type(value)}>"

    def convert(self, value: Any, depth: int = 0) -> Any:
        if self.remaining_nodes <= 0:
            return {"truncated": True}
        self.remaining_nodes -= 1
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, enum.Enum):
            return self.convert(value.value, depth)
        if isinstance(value, (bytes, bytearray, memoryview)):
            encoded = base64.b64encode(bytes(value[:_MAX_STRING_LENGTH])).decode(
                "ascii"
            )
            return {
                "encoding": "base64",
                "data": encoded,
                "truncated": len(value) > _MAX_STRING_LENGTH,
            }
        if depth >= _MAX_VALUE_DEPTH:
            return {"type": _qualified_type(value), "truncated": True}

        identity = id(value)
        if identity in self.seen:
            return {"type": _qualified_type(value), "cycle": True}
        self.seen.add(identity)
        try:
            if isinstance(value, Mapping):
                raw_pairs = list(
                    islice(value.items(), _MAX_COLLECTION_ITEMS + 1)
                )
                collection_truncated = len(raw_pairs) > _MAX_COLLECTION_ITEMS
                raw_pairs = raw_pairs[:_MAX_COLLECTION_ITEMS]
                pairs = sorted(
                    (
                        (
                            self._mapping_key(key),
                            self.convert(item, depth + 1),
                        )
                        for key, item in raw_pairs
                        if self.remaining_nodes > 0
                    ),
                    key=lambda pair: pair[0],
                )
                result: dict[str, Any] = {}
                for key, item in pairs:
                    unique_key = key
                    suffix = 2
                    while unique_key in result:
                        unique_key = f"{key}#{suffix}"
                        suffix += 1
                    result[unique_key] = item
                if collection_truncated or len(pairs) < len(raw_pairs):
                    result["__trace_truncated__"] = True
                return result

            if isinstance(value, (set, frozenset)):
                if len(value) > _MAX_COLLECTION_ITEMS:
                    return {
                        "type": _qualified_type(value),
                        "size": len(value),
                        "truncated": True,
                    }
                converted = [
                    self.convert(item, depth + 1)
                    for item in value
                    if self.remaining_nodes > 0
                ]
                result = sorted(
                    converted,
                    key=lambda item: json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                if len(converted) < len(value):
                    result.append({"truncated": True})
                return result

            if isinstance(value, Sequence):
                raw_items = list(
                    islice(iter(value), _MAX_COLLECTION_ITEMS + 1)
                )
                collection_truncated = len(raw_items) > _MAX_COLLECTION_ITEMS
                raw_items = raw_items[:_MAX_COLLECTION_ITEMS]
                result = [
                    self.convert(item, depth + 1)
                    for item in raw_items
                    if self.remaining_nodes > 0
                ]
                if collection_truncated or len(result) < len(raw_items):
                    result.append({"truncated": True})
                return result

            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                fields = dataclasses.fields(value)
                result = {}
                for field in fields[:_MAX_COLLECTION_ITEMS]:
                    if self.remaining_nodes <= 0:
                        break
                    result[field.name] = self.convert(
                        getattr(value, field.name), depth + 1
                    )
                if len(result) < len(fields):
                    result["__trace_truncated__"] = True
                return result

            attributes = getattr(value, "__dict__", None)
            if isinstance(attributes, dict):
                raw_attributes = list(
                    islice(attributes.items(), _MAX_COLLECTION_ITEMS + 1)
                )
                collection_truncated = (
                    len(raw_attributes) > _MAX_COLLECTION_ITEMS
                )
                raw_attributes = raw_attributes[:_MAX_COLLECTION_ITEMS]
                public = {}
                for name, item in sorted(
                    raw_attributes,
                    key=lambda pair: str(pair[0]),
                ):
                    if self.remaining_nodes <= 0:
                        break
                    if isinstance(name, str) and not name.startswith("_"):
                        public[self._text(name)] = self.convert(item, depth + 1)
                if collection_truncated or len(public) < len(raw_attributes):
                    public["__trace_truncated__"] = True
                if public:
                    return {"type": _qualified_type(value), "fields": public}

            # Avoid arbitrary repr strings (including process-specific memory
            # addresses) when an event type has no portable data representation.
            return {"type": _qualified_type(value)}
        finally:
            self.seen.discard(identity)


def _component_path(component: Component) -> str:
    names: list[str] = []
    current: Component | None = component
    seen: set[int] = set()
    while current is not None and len(names) < _MAX_COMPONENT_DEPTH:
        identity = id(current)
        if identity in seen:
            names.append("<cycle>")
            break
        seen.add(identity)
        try:
            name_value = object.__getattribute__(current, "name")
        except Exception:
            name_value = None
        names.append(
            _safe_label(name_value, maximum=_MAX_COMPONENT_NAME_LENGTH)
        )
        try:
            current = object.__getattribute__(current, "parent")
        except Exception:
            current = None
    if current is not None and (not names or names[-1] != "<cycle>"):
        names.append("…")
    return ".".join(reversed(names))


def _atomic_output_ports(component: Component):
    if isinstance(component, Atomic):
        yield from component.out_ports
        return
    if isinstance(component, Coupled):
        for child in component.components:
            yield from _atomic_output_ports(child)


class JSONLEventTrace(Transducer):
    """Stream atomic output events to one size-bounded JSONL file."""

    def __init__(self, target: Path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        if max_bytes < _SUMMARY_RESERVE_BYTES * 2:
            raise ValueError(
                f"max_bytes must be at least {_SUMMARY_RESERVE_BYTES * 2}"
            )
        super().__init__(
            transducer_id="generated_simulation_event_trace",
            include_names=True,
            exhaustive=False,
        )
        self.target = target
        self.max_bytes = max_bytes
        self._stream: BinaryIO | None = None
        self._bytes_written = 0
        self._recorded_events = 0
        self._dropped_events = 0
        self._sequence = 0
        self._capacity_exhausted = False

    def create_known_data_types_map(self):
        # ``bulk_data`` performs its own recursive, bounded conversion.
        return (type(None), bool, int, float, str, list, dict)

    def initialize(self) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.target.open("wb")

    @staticmethod
    def _line(payload: Mapping[str, Any]) -> bytes:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _drop_unreadable_event(self) -> None:
        self._sequence += 1
        self._dropped_events += 1

    def _record_event(
        self,
        simulation_time: float,
        component: str,
        port_name: str,
        value: Any,
    ) -> None:
        self._sequence += 1
        if self._capacity_exhausted:
            self._dropped_events += 1
            return
        try:
            numeric_time = float(simulation_time)
        except (TypeError, ValueError, OverflowError):
            numeric_time = None
        try:
            safe_value = _JSONSafeValue().convert(value)
        except Exception:
            # A custom payload may throw from iteration, attribute access, or a
            # mapping implementation. Preserve the event without exposing the
            # exception or allowing observation to fail the simulation.
            safe_value = {
                "type": _qualified_type(value),
                "unavailable": True,
            }
        payload = {
            "schema_version": TRACE_SCHEMA,
            "record_type": "event",
            "sequence": self._sequence,
            "simulation_time": (
                numeric_time
                if numeric_time is not None and math.isfinite(numeric_time)
                else None
            ),
            "component": component,
            "port": port_name,
            "value": safe_value,
        }
        try:
            line = self._line(payload)
        except Exception:
            self._dropped_events += 1
            return
        if (
            self._stream is not None
            and self._bytes_written + len(line)
            <= self.max_bytes - _SUMMARY_RESERVE_BYTES
        ):
            self._stream.write(line)
            self._bytes_written += len(line)
            self._recorded_events += 1
        else:
            self._dropped_events += 1
            # Keep a deterministic prefix and avoid repeatedly serializing
            # payloads after the byte budget has already been reached.
            self._capacity_exhausted = True

    def bulk_data(self, sim_time: float) -> None:
        ports = self.imminent_ports or []
        unique_ports: dict[int, Port] = {id(port): port for port in ports}
        described_ports = []
        for port in unique_ports.values():
            try:
                parent = object.__getattribute__(port, "parent")
            except Exception:
                parent = None
            try:
                name = object.__getattribute__(port, "name")
            except Exception:
                name = None
            component = (
                _component_path(parent)
                if isinstance(parent, Component)
                else "<unknown>"
            )
            port_name = _safe_label(name, maximum=_MAX_COMPONENT_NAME_LENGTH)
            described_ports.append((component, port_name, port))

        try:
            # Component and port names define the public deterministic order.
            # Python's stable sort preserves the simulator's order only for the
            # pathological case of duplicate public names; never use object ids,
            # which vary between processes and can leak runtime details.
            for component, port_name, port in sorted(
                described_ports,
                key=lambda item: (item[0], item[1]),
            ):
                try:
                    values = iter(port.values)
                    while True:
                        try:
                            value = next(values)
                        except StopIteration:
                            break
                        except Exception:
                            self._drop_unreadable_event()
                            break
                        self._record_event(
                            sim_time,
                            component,
                            port_name,
                            value,
                        )
                except Exception:
                    self._drop_unreadable_event()
        finally:
            # Flush once per simulation timestamp. A timeout or forced stop can
            # then retain completed event rows even though no footer was written.
            if self._stream is not None:
                self._stream.flush()

    def exit(self) -> None:
        if self._stream is None:
            return
        footer = self._line(
            {
                "schema_version": TRACE_SCHEMA,
                "record_type": "summary",
                "recorded_events": self._recorded_events,
                "dropped_events": self._dropped_events,
                "truncated": self._dropped_events > 0,
            }
        )
        if self._bytes_written + len(footer) > self.max_bytes:
            self._stream.close()
            self._stream = None
            raise RuntimeError("Event trace summary exceeded its reserved byte budget")
        self._stream.write(footer)
        self._stream.flush()
        self._stream.close()
        self._stream = None


def attach_event_trace(
    coordinator,
    model: Component,
    *,
    result_root: str | os.PathLike[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> JSONLEventTrace | None:
    """Attach the standard trace before ``coordinator.initialize()``.

    Standalone smoke tests do not receive a results directory and therefore do
    not write a trace.  Managed executions always supply one and receive a
    trace footer even when the model emits no output events.
    """

    supplied_root = result_root or os.environ.get(TRACE_RESULTS_ENV)
    if not supplied_root:
        return None
    trace = JSONLEventTrace(
        Path(supplied_root) / TRACE_RESULT_FILE,
        max_bytes=max_bytes,
    )
    for port in _atomic_output_ports(model):
        trace.add_target_port(port)
    coordinator.add_transducer(trace)
    return trace


__all__ = [
    "DEFAULT_MAX_BYTES",
    "JSONLEventTrace",
    "TRACE_RESULT_FILE",
    "TRACE_RESULTS_ENV",
    "TRACE_SCHEMA",
    "attach_event_trace",
]
