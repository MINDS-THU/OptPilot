"""Operational conformance checks for generated event traces.

The generated ``event_trace.jsonl`` is the behavioral evidence a simulator
leaves behind: a header row, ordered ``event``/``state`` observation rows,
and a summary footer with recorded/dropped counters. This module validates
that shape statically — the paper's operational-conformance idea (exit 0,
schema-valid trace) as a reusable helper. It is dependency-free and never
imports or executes generated code; behavioral (domain) checking stays
research-side.
"""

from __future__ import annotations

import json
import math


TRACE_SCHEMA = "devs.event-trace.v2"
MAX_CONFORMANCE_ERRORS = 20

_FOOTER_COUNTERS = (
    "recorded_events",
    "dropped_events",
    "recorded_states",
    "dropped_states",
    "recorded_records",
    "dropped_records",
)
_FOOTER_FLAGS = ("events_truncated", "states_truncated", "truncated")


def _is_finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _check_common_observation_fields(row: dict, label: str) -> list[str]:
    errors: list[str] = []
    if row.get("schema_version") != TRACE_SCHEMA:
        errors.append(f"{label}: schema_version must be {TRACE_SCHEMA!r}.")
    for key in ("sequence", "record_sequence"):
        value = row.get(key)
        if type(value) is not int or value < 1:
            errors.append(f"{label}: {key} must be a positive integer.")
    if not _is_finite_number(row.get("simulation_time")) or float(
        row["simulation_time"] if "simulation_time" in row else -1
    ) < 0:
        errors.append(f"{label}: simulation_time must be a finite number >= 0.")
    cycle = row.get("observation_cycle")
    if type(cycle) is not int or cycle < 0:
        errors.append(f"{label}: observation_cycle must be an integer >= 0.")
    for key in ("component", "component_id"):
        value = row.get(key)
        if not isinstance(value, str) or not value:
            errors.append(f"{label}: {key} must be a non-empty string.")
    return errors


def validate_event_trace_lines(lines) -> tuple[str, ...]:
    """Validate one trace's rows; return bounded, human-readable errors."""

    errors: list[str] = []

    def record(message: str) -> bool:
        if len(errors) < MAX_CONFORMANCE_ERRORS:
            errors.append(message)
        return len(errors) >= MAX_CONFORMANCE_ERRORS

    rows: list[dict] = []
    for index, raw in enumerate(lines):
        text = raw.strip()
        if not text:
            continue
        label = f"line {index + 1}"
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            if record(f"{label}: not valid JSON."):
                return tuple(errors)
            continue
        if not isinstance(row, dict):
            if record(f"{label}: trace rows must be JSON objects."):
                return tuple(errors)
            continue
        rows.append(row)

    if len(rows) < 2:
        record("Trace must contain a header row and a summary footer.")
        return tuple(errors)

    header = rows[0]
    if header.get("record_type") != "header":
        record("line 1: first row must have record_type 'header'.")
    if header.get("schema_version") != TRACE_SCHEMA:
        record(f"line 1: header schema_version must be {TRACE_SCHEMA!r}.")

    footer = rows[-1]
    if footer.get("record_type") != "summary":
        record("last row must be the summary footer (record_type 'summary').")
        footer = {}
    for key in _FOOTER_COUNTERS:
        value = footer.get(key)
        if footer and (type(value) is not int or value < 0):
            record(f"footer: {key} must be an integer >= 0.")
    for key in _FOOTER_FLAGS:
        if footer and type(footer.get(key)) is not bool:
            record(f"footer: {key} must be a boolean.")

    event_rows = 0
    state_rows = 0
    previous_record_sequence = 0
    for offset, row in enumerate(rows[1:-1]):
        label = f"row {offset + 2}"
        record_type = row.get("record_type")
        if record_type == "event":
            event_rows += 1
            for message in _check_common_observation_fields(row, label):
                if record(message):
                    return tuple(errors)
            if row.get("event_kind") != "output":
                record(f"{label}: event_kind must be 'output'.")
            port = row.get("port")
            if not isinstance(port, str) or not port:
                record(f"{label}: event rows need a non-empty port.")
            if "value" not in row:
                record(f"{label}: event rows must carry a value field.")
        elif record_type == "state":
            state_rows += 1
            for message in _check_common_observation_fields(row, label):
                if record(message):
                    return tuple(errors)
            observation = row.get("observation")
            if not isinstance(observation, str) or not observation:
                record(f"{label}: state rows need a non-empty observation.")
            sigma = row.get("sigma")
            if sigma is not None and not _is_finite_number(sigma):
                record(f"{label}: sigma must be finite or null.")
            if type(row.get("sigma_infinite")) is not bool:
                record(f"{label}: sigma_infinite must be a boolean.")
        else:
            record(
                f"{label}: record_type must be 'event' or 'state', "
                f"not {record_type!r}."
            )
            continue
        sequence = row.get("record_sequence")
        if type(sequence) is int and sequence <= previous_record_sequence:
            record(f"{label}: record_sequence must be strictly increasing.")
        if type(sequence) is int:
            previous_record_sequence = sequence
        if len(errors) >= MAX_CONFORMANCE_ERRORS:
            return tuple(errors)

    if footer:
        expectations = (
            ("recorded_events", event_rows),
            ("recorded_states", state_rows),
            ("recorded_records", event_rows + state_rows),
        )
        for key, expected in expectations:
            value = footer.get(key)
            if type(value) is int and value != expected:
                record(
                    f"footer: {key} is {value} but the trace contains "
                    f"{expected} matching rows."
                )
    return tuple(errors)


def validate_event_trace_text(text: str) -> tuple[str, ...]:
    return validate_event_trace_lines(text.splitlines())


__all__ = [
    "MAX_CONFORMANCE_ERRORS",
    "TRACE_SCHEMA",
    "validate_event_trace_lines",
    "validate_event_trace_text",
]
