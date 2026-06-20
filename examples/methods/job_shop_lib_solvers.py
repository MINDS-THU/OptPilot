"""Shared helpers for JobShopLib-backed OptPilot method examples."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

import yaml


JsonDict = Dict[str, Any]


def solve_job_shop_cases(study_state: JsonDict, solver_factory: Callable[[], Any]) -> JsonDict:
    cases = load_job_shop_cases(study_state)
    solutions: JsonDict = {}
    for case_id, payload in cases.items():
        job_shop = to_job_shop_lib_instance(payload)
        schedule = solver_factory()(job_shop)
        solutions[case_id] = {"operations": schedule_to_operations(schedule)}
    return solutions


def load_job_shop_cases(study_state: JsonDict) -> Dict[str, JsonDict]:
    candidate_context = study_state.get("candidate_context", {})
    method_context = candidate_context.get("methodContext", {}) if isinstance(candidate_context, dict) else {}
    references = method_context.get("references", []) if isinstance(method_context, dict) else []
    cases: Dict[str, JsonDict] = {}
    for reference in references:
        if not isinstance(reference, dict) or reference.get("type") != "job_shop_case":
            continue
        case_id = str(reference.get("name") or "")
        path = reference.get("path")
        if not case_id or not path:
            continue
        with Path(str(path)).open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            raise TypeError(f"Job-shop case {case_id!r} must load to an object.")
        cases[case_id] = dict(payload)
    if not cases:
        raise ValueError("JobShopLib methods require job_shop_case entries in methodContext.references.")
    return cases


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
