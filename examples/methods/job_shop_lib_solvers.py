"""Shared helpers for JobShopLib-backed OptPilot method examples."""

from __future__ import annotations

from typing import Any, Callable, Dict


JsonDict = Dict[str, Any]


def solve_study_instances(study_state: JsonDict, solver_factory: Callable[[], Any]) -> JsonDict:
    instances = study_state.get("instances", [])
    if not isinstance(instances, list) or not instances:
        raise ValueError("JobShopLib methods require study_state.instances with id and payload for each instance.")
    solutions: JsonDict = {}
    for item in instances:
        if not isinstance(item, dict):
            raise ValueError("Each study_state.instances entry must be an object.")
        instance_id = str(item.get("id", ""))
        payload = item.get("payload")
        if not instance_id or not isinstance(payload, dict):
            raise ValueError("Each study_state.instances entry must define id and payload.")
        job_shop = to_job_shop_lib_instance(payload)
        schedule = solver_factory()(job_shop)
        solutions[instance_id] = {"operations": schedule_to_operations(schedule)}
    return solutions


def to_job_shop_lib_instance(payload: JsonDict):
    try:
        from job_shop_lib import JobShopInstance, Operation
    except ImportError as exc:
        raise RuntimeError("This example requires JobShopLib. Install it with `uv sync --extra examples`.") from exc

    jobs = [
        [Operation(machines=int(item["machine"]), duration=int(item["duration"])) for item in job]
        for job in payload["jobs"]
    ]
    metadata = {"lower_bound": payload.get("lower_bound"), "due_date": payload.get("due_date")}
    return JobShopInstance(jobs, name=str(payload.get("name", "job-shop-instance")), **metadata)


def schedule_to_operations(schedule) -> list[JsonDict]:
    operations: list[JsonDict] = []
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
