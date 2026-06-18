"""Method that emits a JobShopLib dispatching-rule solver candidate."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from optpilot.candidate_files import CandidateFileStore


JsonDict = Dict[str, Any]


class JobShopLibDispatchingRuleMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        runtime_context = dict(study_state.get("runtime_context", {}))
        candidate_store_dir = runtime_context.get("candidate_store_dir")
        if not candidate_store_dir:
            raise ValueError("JobShopLibDispatchingRuleMethod requires runtime_context.candidate_store_dir.")
        self._emitted = True
        workspace = Path(candidate_store_dir).parent / "method_generated" / self.definition["id"]
        workspace.mkdir(parents=True, exist_ok=True)
        rule = str(self.settings.get("dispatchingRule", "most_work_remaining"))
        solver_path = workspace / "solver.py"
        solver_path.write_text(_solver_source(rule), encoding="utf-8")
        store = CandidateFileStore(
            candidate_store_dir,
            content_ref_mode=runtime_context.get("candidate_content_ref_mode", "absolute"),
        )
        return [
            store.store_file(
                solver_path,
                path="solver.py",
                candidate_id=f"job-shop-lib-dispatch-{uuid.uuid4().hex[:12]}",
                generator={
                    "method_id": self.definition["id"],
                    "strategy": "job_shop_lib_dispatching_rule",
                    "dispatching_rule": rule,
                },
                metadata={"summary": f"JobShopLib DispatchingRuleSolver({rule!r})."},
            )
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None


def _solver_source(rule: str) -> str:
    return f'''"""Generated JobShopLib dispatching-rule solver candidate."""

from __future__ import annotations

from typing import Any, Dict


DISPATCHING_RULE = {rule!r}


def solve(instance: Dict[str, Any], time_limit_seconds: int, context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from job_shop_lib import JobShopInstance, Operation
        from job_shop_lib.dispatching.rules import DispatchingRuleSolver
    except ImportError as exc:
        raise RuntimeError(
            "This example requires JobShopLib. Install it with `uv sync --extra examples`."
        ) from exc

    job_shop = _to_job_shop_lib_instance(instance, JobShopInstance, Operation)
    schedule = DispatchingRuleSolver(DISPATCHING_RULE)(job_shop)
    return {{
        "operations": _schedule_to_operations(schedule),
        "solver": {{"name": "job_shop_lib.DispatchingRuleSolver", "dispatching_rule": DISPATCHING_RULE}},
    }}


def _to_job_shop_lib_instance(payload, JobShopInstance, Operation):
    jobs = [
        [Operation(machines=int(item["machine"]), duration=int(item["duration"])) for item in job]
        for job in payload["jobs"]
    ]
    metadata = {{"lower_bound": payload.get("lower_bound"), "due_date": payload.get("due_date")}}
    return JobShopInstance(jobs, name=str(payload.get("name", "job-shop-instance")), **metadata)


def _schedule_to_operations(schedule):
    operations = []
    for machine_ops in schedule.schedule:
        for scheduled in machine_ops:
            operation = scheduled.operation
            operations.append(
                {{
                    "job": int(operation.job_id),
                    "operation": int(operation.position_in_job),
                    "machine": int(scheduled.machine_id),
                    "start": int(scheduled.start_time),
                    "end": int(scheduled.end_time),
                    "duration": int(operation.duration),
                }}
            )
    return sorted(operations, key=lambda item: (item["start"], item["machine"], item["job"], item["operation"]))
'''
