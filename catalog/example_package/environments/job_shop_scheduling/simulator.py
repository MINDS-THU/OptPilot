"""Small job-shop scheduling API used by the OptPilot example wrapper.

This module is intentionally independent from OptPilot. The evaluator imports
it as an existing environment API would be imported by a user-owned wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

import yaml


JsonDict = Dict[str, Any]
ScoreFunction = Callable[[JsonDict, JsonDict, JsonDict], float]


@dataclass(frozen=True)
class OperationDef:
    job: int
    operation: int
    machine: int
    duration: int


@dataclass(frozen=True)
class JobShopInstance:
    name: str
    jobs: List[List[OperationDef]]
    machine_count: int
    lower_bound: float
    due_date: float | None = None

    @property
    def total_work(self) -> int:
        return sum(operation.duration for job in self.jobs for operation in job)


def load_instance(payload: JsonDict) -> JobShopInstance:
    jobs_payload = payload.get("jobs")
    if not isinstance(jobs_payload, list) or not jobs_payload:
        raise ValueError("Job-shop instance must define a non-empty jobs list.")

    jobs: List[List[OperationDef]] = []
    max_machine = -1
    max_machine_load: Dict[int, int] = {}
    max_job_work = 0

    for job_index, job_payload in enumerate(jobs_payload):
        if not isinstance(job_payload, list) or not job_payload:
            raise ValueError(f"Job {job_index} must be a non-empty operation list.")
        job: List[OperationDef] = []
        job_work = 0
        for operation_index, item in enumerate(job_payload):
            if not isinstance(item, dict):
                raise ValueError(f"Operation {job_index}/{operation_index} must be an object.")
            machine = int(item["machine"])
            duration = int(item["duration"])
            if machine < 0:
                raise ValueError("Machine ids must be non-negative.")
            if duration <= 0:
                raise ValueError("Operation durations must be positive.")
            max_machine = max(max_machine, machine)
            max_machine_load[machine] = max_machine_load.get(machine, 0) + duration
            job_work += duration
            job.append(OperationDef(job_index, operation_index, machine, duration))
        max_job_work = max(max_job_work, job_work)
        jobs.append(job)

    machine_count = int(payload.get("machine_count", max_machine + 1))
    if machine_count <= max_machine:
        raise ValueError("machine_count must cover all operation machine ids.")
    fallback_lower_bound = max([*max_machine_load.values(), max_job_work, 1])
    lower_bound = float(payload.get("lower_bound") or fallback_lower_bound)
    due_date = payload.get("due_date")
    return JobShopInstance(
        name=str(payload.get("name", "job-shop-instance")),
        jobs=jobs,
        machine_count=machine_count,
        lower_bound=lower_bound,
        due_date=float(due_date) if due_date is not None else None,
    )


def load_instance_file(path: str | Path) -> JobShopInstance:
    with Path(path).open("r", encoding="utf-8") as handle:
        return load_instance(yaml.safe_load(handle) or {})


def weighted_dispatch_score(weights: JsonDict) -> ScoreFunction:
    remaining_work_weight = float(weights.get("remaining_work_weight", 1.0))
    processing_time_weight = float(weights.get("processing_time_weight", -1.0))
    machine_ready_weight = float(weights.get("machine_ready_weight", -0.1))
    job_ready_weight = float(weights.get("job_ready_weight", -0.1))

    def score(operation: JsonDict, machine: JsonDict, state: JsonDict) -> float:
        return (
            remaining_work_weight * float(operation["remaining_work"])
            + processing_time_weight * float(operation["duration"])
            + machine_ready_weight * float(machine["ready_time"])
            + job_ready_weight * float(operation["job_ready_time"])
        )

    return score


def schedule_by_dispatch_rule(instance: JobShopInstance, score_fn: ScoreFunction) -> List[JsonDict]:
    next_operation = [0 for _ in instance.jobs]
    job_ready = [0 for _ in instance.jobs]
    machine_ready = [0 for _ in range(instance.machine_count)]
    schedule: List[JsonDict] = []

    while any(next_operation[job] < len(instance.jobs[job]) for job in range(len(instance.jobs))):
        candidate_records: List[tuple[float, int, int, OperationDef, JsonDict, JsonDict, JsonDict]] = []
        for job_index, job in enumerate(instance.jobs):
            operation_index = next_operation[job_index]
            if operation_index >= len(job):
                continue
            operation = job[operation_index]
            earliest_start = max(job_ready[job_index], machine_ready[operation.machine])
            remaining_work = sum(item.duration for item in job[operation_index:])
            operation_payload = {
                "job": job_index,
                "operation": operation_index,
                "machine": operation.machine,
                "duration": operation.duration,
                "remaining_work": remaining_work,
                "job_ready_time": job_ready[job_index],
                "earliest_start": earliest_start,
            }
            machine_payload = {
                "machine": operation.machine,
                "ready_time": machine_ready[operation.machine],
            }
            state_payload = {
                "scheduled_count": len(schedule),
                "machine_ready": list(machine_ready),
                "job_ready": list(job_ready),
                "open_jobs": sum(next_operation[job_id] < len(instance.jobs[job_id]) for job_id in range(len(instance.jobs))),
            }
            value = float(score_fn(operation_payload, machine_payload, state_payload))
            candidate_records.append((value, -earliest_start, -operation.duration, operation, operation_payload, machine_payload, state_payload))

        if not candidate_records:
            raise RuntimeError("No schedulable operation found.")

        _, _, _, operation, _, _, _ = max(candidate_records, key=lambda item: (item[0], item[1], item[2], -item[3].job, -item[3].operation))
        start = max(job_ready[operation.job], machine_ready[operation.machine])
        end = start + operation.duration
        schedule.append(
            {
                "job": operation.job,
                "operation": operation.operation,
                "machine": operation.machine,
                "start": start,
                "end": end,
                "duration": operation.duration,
            }
        )
        next_operation[operation.job] += 1
        job_ready[operation.job] = end
        machine_ready[operation.machine] = end

    return schedule


def validate_schedule(instance: JobShopInstance, schedule: Iterable[JsonDict]) -> List[JsonDict]:
    operations = [dict(item) for item in schedule]
    expected = {
        (operation.job, operation.operation): operation
        for job in instance.jobs
        for operation in job
    }
    seen = set()
    by_machine: Dict[int, List[JsonDict]] = {machine: [] for machine in range(instance.machine_count)}
    by_job: Dict[int, List[JsonDict]] = {job: [] for job in range(len(instance.jobs))}

    for item in operations:
        key = (int(item["job"]), int(item["operation"]))
        if key not in expected:
            raise ValueError(f"Unknown scheduled operation: {key}")
        if key in seen:
            raise ValueError(f"Duplicate scheduled operation: {key}")
        seen.add(key)
        operation = expected[key]
        machine = int(item["machine"])
        start = int(item["start"])
        end = int(item["end"])
        if machine != operation.machine:
            raise ValueError(f"Operation {key} assigned to machine {machine}, expected {operation.machine}.")
        if end - start != operation.duration:
            raise ValueError(f"Operation {key} duration is {end - start}, expected {operation.duration}.")
        if start < 0:
            raise ValueError(f"Operation {key} starts before time zero.")
        normalized = {
            "job": operation.job,
            "operation": operation.operation,
            "machine": operation.machine,
            "start": start,
            "end": end,
            "duration": operation.duration,
        }
        by_machine[operation.machine].append(normalized)
        by_job[operation.job].append(normalized)

    missing = sorted(set(expected) - seen)
    if missing:
        raise ValueError(f"Schedule is missing operations: {missing}")

    for machine, items in by_machine.items():
        ordered = sorted(items, key=lambda item: (item["start"], item["end"]))
        for previous, current in zip(ordered, ordered[1:]):
            if current["start"] < previous["end"]:
                raise ValueError(
                    f"Machine {machine} overlap: job {previous['job']} op {previous['operation']} "
                    f"and job {current['job']} op {current['operation']}."
                )

    for job, items in by_job.items():
        ordered = sorted(items, key=lambda item: item["operation"])
        for expected_index, item in enumerate(ordered):
            if item["operation"] != expected_index:
                raise ValueError(f"Job {job} operations are not complete and ordered.")
        for previous, current in zip(ordered, ordered[1:]):
            if current["start"] < previous["end"]:
                raise ValueError(
                    f"Job {job} precedence violation between operations {previous['operation']} and {current['operation']}."
                )

    return operations


def summarize_schedule(instance: JobShopInstance, schedule: List[JsonDict]) -> JsonDict:
    validated = validate_schedule(instance, schedule)
    makespan = max(item["end"] for item in validated) if validated else 0
    total_work = instance.total_work
    utilization = total_work / (makespan * instance.machine_count) if makespan and instance.machine_count else 0.0
    tardiness = max(0.0, makespan - instance.due_date) if instance.due_date is not None else 0.0
    normalized = makespan / instance.lower_bound if instance.lower_bound else float(makespan)
    return {
        "makespan": float(makespan),
        "normalized_makespan": float(normalized),
        "tardiness": float(tardiness),
        "utilization": float(utilization),
        "feasible": 1.0,
        "operation_count": float(len(validated)),
    }
