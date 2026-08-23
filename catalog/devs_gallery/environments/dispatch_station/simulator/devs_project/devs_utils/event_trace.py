"""Bounded JSONL event tracing for generated xDEVS simulations.

The helper belongs to every generated simulator bundle.  It deliberately has
no OptPilot import: a runner opts in by attaching it to the root xDEVS
``Coordinator`` before ``initialize``.  When the execution environment supplies
``OPTPILOT_SIMULATION_RESULTS_DIR``, atomic-model output events and lightweight
post-transition control-state observations are written to a portable, bounded
``event_trace.jsonl`` result.
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
from types import FunctionType
from typing import Any, BinaryIO

from xdevs.abc import Transducer
from xdevs.models import Atomic, Component, Coupled, Port


TRACE_RESULT_FILE = "event_trace.jsonl"
TRACE_RESULTS_ENV = "OPTPILOT_SIMULATION_RESULTS_DIR"
TRACE_LEGACY_SCHEMA = "devs.event-trace.v1"
TRACE_SCHEMA = "devs.event-trace.v2"
TRACE_CAPABILITIES = (
    "output-events",
    "transition-control-state",
    "explicit-domain-state",
    "canonical-component-ids",
    "observation-cycles",
)

# Keep the trace below Studio's text-preview ceiling while still retaining a
# useful classroom-scale event history.  The terminal summary is always kept.
DEFAULT_MAX_BYTES = 384 * 1024
_SUMMARY_RESERVE_BYTES = 512
# State observations are useful teaching context, but output events remain the
# primary portable trace contract. Capping states independently prevents a
# fast internal-transition loop from consuming the whole file before a later
# output event is emitted.
_STATE_BUDGET_FRACTION = 0.25
_MAX_VALUE_DEPTH = 6
_MAX_VALUE_NODES = 128
_MAX_COLLECTION_ITEMS = 64
_MAX_STRING_LENGTH = 2048
_MAX_COMPONENT_DEPTH = 64
_MAX_COMPONENT_NAME_LENGTH = 256
_MAX_TYPE_NAME_LENGTH = 512
_NO_TRACE_STATE = object()


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


def _explicit_trace_state(component: Atomic) -> Any:
    """Call an opt-in model projection without inspecting model attributes.

    Only a normal ``trace_state`` method declared directly on the concrete
    atomic class is eligible.  In particular, this never treats ``__dict__``
    fields as observable model state and never invokes dynamic attribute
    lookup on the model.  A broken hook is represented as unavailable rather
    than allowed to interrupt the simulation.
    """

    try:
        namespace = type.__getattribute__(type(component), "__dict__")
        hook = namespace.get("trace_state")
    except Exception:
        return _NO_TRACE_STATE
    if type(hook) is not FunctionType:
        return _NO_TRACE_STATE
    try:
        value = hook(component)
    except Exception:
        return {"unavailable": True}
    try:
        return _JSONSafeValue().convert(value)
    except Exception:
        return {
            "type": _qualified_type(value),
            "unavailable": True,
        }


def _component_labels(component: Component) -> list[str]:
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
    return list(reversed(names))


def _component_path(component: Component) -> str:
    """Return the dotted display path retained by the v1 trace contract."""

    return ".".join(_component_labels(component))


def _component_id(component: Component) -> str:
    """Return the canonical path used by the generated-model structure graph.

    Structure graphs always call the selected top-level instance ``root`` and
    identify descendants with slash-separated instance names.  The dotted
    ``component`` field remains in every event for v1 readers and for display.
    """

    labels = _component_labels(component)
    if len(labels) <= 1:
        return "root"
    descendants = labels if labels[0] in {"<cycle>", "…"} else labels[1:]
    return "/".join(("root", *descendants))


def _atomic_output_ports(component: Component):
    if isinstance(component, Atomic):
        yield from component.out_ports
        return
    if isinstance(component, Coupled):
        for child in component.components:
            yield from _atomic_output_ports(child)


class JSONLEventTrace(Transducer):
    """Stream output events and transitioned control states to bounded JSONL."""

    def __init__(
        self,
        target: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        initial_time: float = 0.0,
    ):
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
        self.initial_time = self._simulation_time(initial_time)
        self._stream: BinaryIO | None = None
        self._bytes_written = 0
        self._state_bytes_written = 0
        self._state_max_bytes = max(
            0,
            int(
                (self.max_bytes - _SUMMARY_RESERVE_BYTES)
                * _STATE_BUDGET_FRACTION
            ),
        )
        self._recorded_events = 0
        self._dropped_events = 0
        self._recorded_states = 0
        self._dropped_states = 0
        self._sequence = 0
        self._state_sequence = 0
        self._record_sequence = 0
        # One cycle is one transducer ``bulk_data`` callback.  xDEVS can run
        # several zero-delay transitions at the same simulation time, so time
        # alone is not enough to reconstruct honest replay steps.
        self._observation_cycle = 0
        self._event_capacity_exhausted = False
        self._state_capacity_exhausted = False

    def create_known_data_types_map(self):
        # ``bulk_data`` performs its own recursive, bounded conversion.
        return (type(None), bool, int, float, str, list, dict)

    def initialize(self) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.target.open("wb")
        header = self._line(
            {
                "schema_version": TRACE_SCHEMA,
                "record_type": "header",
                "capabilities": list(TRACE_CAPABILITIES),
                "component_id_format": "root/<child-instance>/...",
                "legacy_event_schema": TRACE_LEGACY_SCHEMA,
            }
        )
        if len(header) > self.max_bytes - _SUMMARY_RESERVE_BYTES:
            self._stream.close()
            self._stream = None
            raise RuntimeError("Event trace header exceeded the byte budget")
        self._stream.write(header)
        self._bytes_written = len(header)

        described_components = []
        for component in self.target_components:
            try:
                described_components.append(
                    (
                        _component_id(component),
                        _component_path(component),
                        component,
                    )
                )
            except Exception:
                self._drop_unreadable_state()
        for _, _, component in sorted(
            described_components,
            key=lambda item: (item[0], item[1]),
        ):
            try:
                self._record_state(
                    self.initial_time,
                    component,
                    observation="initialized",
                )
            except Exception:
                # ``_record_state`` advances its sequence before observing the
                # component, so an unexpected failure counts this attempt once.
                self._dropped_states += 1
        self._stream.flush()

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
        self._record_sequence += 1
        self._dropped_events += 1

    def _drop_unreadable_state(self) -> None:
        self._state_sequence += 1
        self._record_sequence += 1
        self._dropped_states += 1

    @staticmethod
    def _simulation_time(value: Any) -> float | None:
        try:
            numeric_time = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric_time if math.isfinite(numeric_time) else None

    def _write_record(self, payload: Mapping[str, Any], *, kind: str) -> None:
        capacity_exhausted = (
            self._event_capacity_exhausted
            if kind == "event"
            else self._state_capacity_exhausted
        )
        if capacity_exhausted:
            if kind == "event":
                self._dropped_events += 1
            else:
                self._dropped_states += 1
            return
        try:
            line = self._line(payload)
        except Exception:
            if kind == "event":
                self._dropped_events += 1
            else:
                self._dropped_states += 1
            return
        state_budget_available = (
            kind != "state"
            or self._state_bytes_written + len(line) <= self._state_max_bytes
        )
        if (
            self._stream is not None
            and state_budget_available
            and self._bytes_written + len(line)
            <= self.max_bytes - _SUMMARY_RESERVE_BYTES
        ):
            self._stream.write(line)
            self._bytes_written += len(line)
            if kind == "event":
                self._recorded_events += 1
            else:
                self._recorded_states += 1
                self._state_bytes_written += len(line)
            return

        if kind == "event":
            self._dropped_events += 1
        else:
            self._dropped_states += 1
        # Keep deterministic per-kind prefixes and avoid repeatedly serializing
        # observations after their allowance is exhausted. A state-only limit
        # must never disable later output-event recording.
        if kind == "event":
            self._event_capacity_exhausted = True
        else:
            self._state_capacity_exhausted = True

    def _record_event(
        self,
        simulation_time: float,
        observation_cycle: int,
        component_id: str,
        component: str,
        port_name: str,
        value: Any,
    ) -> None:
        self._sequence += 1
        self._record_sequence += 1
        if self._event_capacity_exhausted:
            self._dropped_events += 1
            return
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
            "event_kind": "output",
            "sequence": self._sequence,
            "record_sequence": self._record_sequence,
            "simulation_time": self._simulation_time(simulation_time),
            "observation_cycle": observation_cycle,
            "component_id": component_id,
            "component": component,
            "port": port_name,
            "value": safe_value,
        }
        self._write_record(payload, kind="event")

    def _record_state(
        self,
        simulation_time: float,
        component: Atomic,
        *,
        observation: str = "post_transition",
        observation_cycle: int = 0,
    ) -> None:
        self._state_sequence += 1
        self._record_sequence += 1
        if self._state_capacity_exhausted:
            self._dropped_states += 1
            return

        try:
            raw_phase = object.__getattribute__(component, "phase")
        except Exception:
            raw_phase = "<unavailable>"
        phase = _safe_label(raw_phase, maximum=_MAX_COMPONENT_NAME_LENGTH)

        try:
            raw_sigma = object.__getattribute__(component, "sigma")
        except Exception:
            raw_sigma = None
        sigma: float | None = None
        sigma_infinite = False
        if type(raw_sigma) in (int, float):
            try:
                numeric_sigma = float(raw_sigma)
            except (TypeError, ValueError, OverflowError):
                numeric_sigma = None
            if numeric_sigma is not None:
                sigma_infinite = math.isinf(numeric_sigma)
                if math.isfinite(numeric_sigma):
                    sigma = numeric_sigma

        payload = {
            "schema_version": TRACE_SCHEMA,
            "record_type": "state",
            "observation": observation,
            "sequence": self._state_sequence,
            "record_sequence": self._record_sequence,
            "simulation_time": self._simulation_time(simulation_time),
            "observation_cycle": observation_cycle,
            "component_id": _component_id(component),
            "component": _component_path(component),
            "phase": phase,
            "sigma": sigma,
            "sigma_infinite": sigma_infinite,
        }
        domain_state = _explicit_trace_state(component)
        if domain_state is not _NO_TRACE_STATE:
            payload["domain_state"] = domain_state
        self._write_record(payload, kind="state")

    def bulk_data(self, sim_time: float) -> None:
        self._observation_cycle += 1
        observation_cycle = self._observation_cycle
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
            component_id = (
                _component_id(parent)
                if isinstance(parent, Component)
                else "root"
            )
            port_name = _safe_label(name, maximum=_MAX_COMPONENT_NAME_LENGTH)
            described_ports.append((component_id, component, port_name, port))

        try:
            # Component and port names define the public deterministic order.
            # Python's stable sort preserves the simulator's order only for the
            # pathological case of duplicate public names; never use object ids,
            # which vary between processes and can leak runtime details.
            for component_id, component, port_name, port in sorted(
                described_ports,
                key=lambda item: (item[0], item[1], item[2]),
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
                            observation_cycle,
                            component_id,
                            component,
                            port_name,
                            value,
                        )
                except Exception:
                    self._drop_unreadable_event()

            transitioned = self.imminent_components or []
            unique_components: dict[int, Atomic] = {
                id(component): component for component in transitioned
            }
            described_components = []
            for component in unique_components.values():
                try:
                    described_components.append(
                        (
                            _component_id(component),
                            _component_path(component),
                            component,
                        )
                    )
                except Exception:
                    self._drop_unreadable_state()
            for _, _, component in sorted(
                described_components,
                key=lambda item: (item[0], item[1]),
            ):
                try:
                    self._record_state(
                        sim_time,
                        component,
                        observation_cycle=observation_cycle,
                    )
                except Exception:
                    self._dropped_states += 1
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
                "recorded_states": self._recorded_states,
                "dropped_states": self._dropped_states,
                "recorded_records": (
                    self._recorded_events + self._recorded_states
                ),
                "dropped_records": self._dropped_events + self._dropped_states,
                "events_truncated": self._dropped_events > 0,
                "states_truncated": self._dropped_states > 0,
                "truncated": (
                    self._dropped_events > 0 or self._dropped_states > 0
                ),
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
        initial_time=getattr(getattr(coordinator, "clock", None), "time", 0.0),
    )
    trace.add_target_component(model)
    for port in _atomic_output_ports(model):
        trace.add_target_port(port)
    coordinator.add_transducer(trace)
    return trace


__all__ = [
    "DEFAULT_MAX_BYTES",
    "JSONLEventTrace",
    "TRACE_CAPABILITIES",
    "TRACE_LEGACY_SCHEMA",
    "TRACE_RESULT_FILE",
    "TRACE_RESULTS_ENV",
    "TRACE_SCHEMA",
    "attach_event_trace",
]
