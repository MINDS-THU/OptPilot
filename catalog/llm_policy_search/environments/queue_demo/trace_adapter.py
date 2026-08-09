"""JSONL event-trace → SQLite adapter for DEVS-Gen generated simulators.

Generated simulators emit the ``devs.event-trace.v2`` JSONL stream
(header row, ``event``/``state`` observation rows, summary footer). The
trace-aware policy-search method consumes a queryable SQLite database.
This adapter converts one stream into the standard trace database shape —
``events``, ``states``, and ``kpi`` tables — so a generated simulator
satisfies the ``simulation_trace`` convention without changes.

Part of the bring-your-own-simulator template; copy it next to your
evaluator when wrapping a DEVS-Gen bundle. Standard library only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

_MAX_ROWS = 200_000
_MAX_VALUE_CHARS = 2_000


def _bounded_value(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:_MAX_VALUE_CHARS]


def convert_event_trace(
    jsonl_path: Path,
    database_path: Path,
    *,
    kpis: Mapping[str, float] | None = None,
) -> dict[str, int]:
    """Convert one JSONL event trace into the standard SQLite trace shape.

    Returns row counts per table. Raises on a structurally broken stream
    (missing header/footer); optional ``kpis`` land in the ``kpi`` table so
    the trace is self-describing for the search method's SQL access.
    """

    lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    if (
        len(rows) < 2
        or rows[0].get("record_type") != "header"
        or rows[-1].get("record_type") != "summary"
    ):
        raise ValueError(
            "Event trace must carry its header row and summary footer."
        )

    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    counts = {"events": 0, "states": 0, "kpi": 0}
    try:
        connection.execute(
            "CREATE TABLE events (sequence INTEGER, record_sequence INTEGER, "
            "simulation_time REAL, component TEXT, component_id TEXT, "
            "port TEXT, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE states (sequence INTEGER, record_sequence INTEGER, "
            "simulation_time REAL, component TEXT, component_id TEXT, "
            "phase TEXT, sigma REAL, sigma_infinite INTEGER, domain_state TEXT)"
        )
        connection.execute("CREATE TABLE kpi (name TEXT, value REAL)")
        for row in rows[1:-1][:_MAX_ROWS]:
            record_type = row.get("record_type")
            if record_type == "event":
                connection.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("sequence"),
                        row.get("record_sequence"),
                        row.get("simulation_time"),
                        row.get("component"),
                        row.get("component_id"),
                        row.get("port"),
                        _bounded_value(row.get("value")),
                    ),
                )
                counts["events"] += 1
            elif record_type == "state":
                connection.execute(
                    "INSERT INTO states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("sequence"),
                        row.get("record_sequence"),
                        row.get("simulation_time"),
                        row.get("component"),
                        row.get("component_id"),
                        row.get("phase"),
                        row.get("sigma"),
                        1 if row.get("sigma_infinite") else 0,
                        (
                            _bounded_value(row["domain_state"])
                            if "domain_state" in row
                            else None
                        ),
                    ),
                )
                counts["states"] += 1
        for name, value in sorted((kpis or {}).items()):
            connection.execute(
                "INSERT INTO kpi VALUES (?, ?)", (str(name), float(value))
            )
            counts["kpi"] += 1
        connection.commit()
    finally:
        connection.close()
    return counts
