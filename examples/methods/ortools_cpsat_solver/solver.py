"""JobShopLib OR-Tools CP-SAT solver candidate.

This file is emitted as a file candidate by the example CP-SAT method. It is
imported by the job-shop evaluator inside the trial workspace.
"""

from __future__ import annotations

from typing import Any, Dict


JsonDict = Dict[str, Any]


def solve(instance: JsonDict, time_limit_seconds: int, context: JsonDict) -> JsonDict:
    try:
        from job_shop_lib import JobShopInstance, Operation
        from job_shop_lib.constraint_programming import ORToolsSolver
    except ImportError as exc:
        raise RuntimeError(
            "This example requires JobShopLib. Install it with `uv sync --extra examples`."
        ) from exc

    job_shop = _to_job_shop_lib_instance(instance, JobShopInstance, Operation)
    solver = ORToolsSolver(max_time_in_seconds=max(float(time_limit_seconds) * 0.8, 1.0))
    schedule = solver(job_shop)
    return {
        "operations": _schedule_to_operations(schedule),
        "solver": {
            "name": "job_shop_lib.ORToolsSolver",
            "metadata": dict(getattr(schedule, "metadata", {}) or {}),
        },
    }


def _to_job_shop_lib_instance(payload, JobShopInstance, Operation):
    jobs = [
        [Operation(machines=int(item["machine"]), duration=int(item["duration"])) for item in job]
        for job in payload["jobs"]
    ]
    metadata = {"lower_bound": payload.get("lower_bound"), "due_date": payload.get("due_date")}
    return JobShopInstance(jobs, name=str(payload.get("name", "job-shop-instance")), **metadata)


def _schedule_to_operations(schedule):
    operations = []
    for machine_ops in schedule.schedule:
        for scheduled in machine_ops:
            operation = scheduled.operation
            operations.append(
                {
                    "job": int(operation.job_id),
                    "operation": int(operation.position_in_job),
                    "machine": int(scheduled.machine_id),
                    "start": int(scheduled.start_time),
                    "end": int(scheduled.end_time),
                    "duration": int(operation.duration),
                }
            )
    return sorted(operations, key=lambda item: (item["start"], item["machine"], item["job"], item["operation"]))
