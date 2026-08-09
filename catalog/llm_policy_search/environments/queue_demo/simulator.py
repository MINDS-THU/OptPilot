"""Deterministic single-server queue with an executable dispatch policy.

The demo domain for the bring-your-own-simulator template: jobs of mixed
classes arrive over one shift; a single server asks the candidate policy
which waiting job to serve next. The candidate's ``scheduler.py`` must
expose ``create_scheduler()`` returning an object whose ``run(snapshot)``
returns the id of one waiting job.

The simulator is deterministic for a fixed seed and writes the standard
bounded SQLite trace (``events`` and ``kpi`` tables) when asked, so the
worst replication can be replayed and inspected by the search method.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping

_JOB_CLASSES = (
    {"name": "standard", "weight": 6, "service": 4.0, "late_penalty": 1.0},
    {"name": "express", "weight": 3, "service": 2.0, "late_penalty": 3.0},
    {"name": "bulky", "weight": 1, "service": 9.0, "late_penalty": 1.5},
)
_MAX_TRACE_EVENTS = 20000


def _build_arrivals(rng: random.Random, horizon: float, mean_gap: float):
    arrivals = []
    time = 0.0
    index = 0
    while True:
        time += rng.expovariate(1.0 / mean_gap)
        if time >= horizon:
            break
        index += 1
        choice = rng.choices(
            _JOB_CLASSES, weights=[item["weight"] for item in _JOB_CLASSES]
        )[0]
        arrivals.append(
            {
                "id": f"job-{index:04d}",
                "class": choice["name"],
                "arrival": round(time, 6),
                "service_time": choice["service"],
                "late_penalty": choice["late_penalty"],
                "due": round(time + 4.0 * choice["service"], 6),
            }
        )
    return arrivals


def run_once(
    *,
    candidate_dir: Path,
    settings: Mapping[str, Any],
    seed: int,
    database_path: Path | None,
) -> Dict[str, Any]:
    """One deterministic replication under the candidate policy."""

    import importlib.util

    scheduler_path = Path(candidate_dir) / "scheduler.py"
    spec = importlib.util.spec_from_file_location("candidate_scheduler", scheduler_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scheduler = module.create_scheduler()

    horizon = float(settings.get("horizon", 480.0))
    mean_gap = float(settings.get("meanArrivalGap", 3.0))
    rng = random.Random(seed)
    arrivals = _build_arrivals(rng, horizon, mean_gap)

    events = []

    def record(time: float, event: str, job_id: str, detail: float = 0.0) -> None:
        if len(events) < _MAX_TRACE_EVENTS:
            events.append((round(time, 6), event, job_id, round(detail, 6)))

    waiting: Dict[str, Dict[str, Any]] = {}
    completed = []
    time = 0.0
    pending = list(arrivals)
    while pending or waiting:
        while pending and pending[0]["arrival"] <= time:
            job = pending.pop(0)
            waiting[job["id"]] = job
            record(job["arrival"], "arrival", job["id"])
        if not waiting:
            time = pending[0]["arrival"]
            continue
        snapshot = {
            "time": round(time, 6),
            "queue": [
                {
                    "id": job["id"],
                    "class": job["class"],
                    "arrival": job["arrival"],
                    "service_time": job["service_time"],
                    "due": job["due"],
                    "late_penalty": job["late_penalty"],
                    "waited": round(time - job["arrival"], 6),
                }
                for job in sorted(waiting.values(), key=lambda item: item["arrival"])
            ],
        }
        chosen_id = scheduler.run(snapshot)
        if chosen_id not in waiting:
            raise RuntimeError(
                f"Policy chose {chosen_id!r}, which is not a waiting job id."
            )
        job = waiting.pop(chosen_id)
        record(time, "start", job["id"])
        time += job["service_time"]
        lateness = max(0.0, time - job["due"])
        completed.append(
            {
                "id": job["id"],
                "wait": time - job["arrival"] - job["service_time"],
                "lateness": lateness,
                "weighted_lateness": lateness * job["late_penalty"],
            }
        )
        record(time, "finish", job["id"], lateness)

    served = len(completed)
    mean_wait = (
        sum(item["wait"] for item in completed) / served if served else 0.0
    )
    weighted_lateness = sum(item["weighted_lateness"] for item in completed)
    total_score = 100.0 - mean_wait - 0.5 * weighted_lateness / max(1, served)
    kpis = {
        "served": float(served),
        "mean_wait": mean_wait,
        "weighted_lateness": weighted_lateness,
    }

    if database_path is not None:
        database = Path(database_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            database.unlink()
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE events "
                "(time REAL, event TEXT, job_id TEXT, detail REAL)"
            )
            connection.executemany(
                "INSERT INTO events VALUES (?, ?, ?, ?)", events
            )
            connection.execute("CREATE TABLE kpi (name TEXT, value REAL)")
            connection.executemany(
                "INSERT INTO kpi VALUES (?, ?)",
                [("total_score", total_score), *sorted(kpis.items())],
            )
            connection.commit()
        finally:
            connection.close()

    return {"total_score": total_score, "kpis": kpis}
