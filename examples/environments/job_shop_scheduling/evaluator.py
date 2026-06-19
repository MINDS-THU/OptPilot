"""OptPilot evaluator wrapper for the job-shop scheduling example."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

from .simulator import (
    load_instance,
    schedule_by_dispatch_rule,
    summarize_schedule,
    validate_schedule,
    weighted_dispatch_score,
)


JsonDict = Dict[str, Any]


def evaluate(candidate_runtime: JsonDict, instance: JsonDict, context: JsonDict) -> JsonDict:
    job_shop = load_instance(instance)
    workspace = Path(candidate_runtime.get("workspace") or context["workspace"]).resolve()
    candidate_root = Path(candidate_runtime.get("candidateRoot") or workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    mode = _evaluation_mode(candidate_runtime, candidate_root)
    if mode == "parameters":
        if "solutions" in candidate_runtime:
            schedule = _extract_solution_schedule(candidate_runtime, instance)
        else:
            schedule = schedule_by_dispatch_rule(job_shop, weighted_dispatch_score(candidate_runtime))
    elif mode == "dispatch_rule":
        module = _load_module(candidate_root / "dispatch_rule.py", "optpilot_job_shop_dispatch_rule")
        if not hasattr(module, "score"):
            raise AttributeError("dispatch_rule.py must define score(operation, machine, state).")
        schedule = schedule_by_dispatch_rule(job_shop, module.score)
    elif mode == "solver":
        module = _load_module(candidate_root / "solver.py", "optpilot_job_shop_solver")
        if not hasattr(module, "solve"):
            raise AttributeError("solver.py must define solve(instance, time_limit_seconds, context).")
        result = module.solve(instance, int(context.get("resource_profile", {}).get("timeoutSeconds", 60)), context)
        schedule = _extract_schedule(result)
    else:
        raise ValueError(f"Unsupported job-shop candidate mode: {mode}")

    validated = validate_schedule(job_shop, schedule)
    metrics = summarize_schedule(job_shop, validated)
    schedule_path = workspace / "schedule.json"
    metrics_path = workspace / "job_shop_metrics.json"
    schedule_path.write_text(json.dumps({"operations": validated}, indent=2, sort_keys=True), encoding="utf-8")
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "status": "success",
        "metric_values": metrics,
        "output_files": [
            {"type": "json", "name": "schedule", "path": str(schedule_path)},
            {"type": "json", "name": "job_shop_metrics", "path": str(metrics_path)},
        ],
        "event_summary": {
            "adapter": "job_shop_scheduling_evaluator",
            "mode": mode,
            "instance": job_shop.name,
            "operation_count": len(validated),
        },
    }


def _evaluation_mode(candidate_runtime: JsonDict, candidate_root: Path) -> str:
    if "workspace" not in candidate_runtime and "candidateRoot" not in candidate_runtime:
        return "parameters"
    if (candidate_root / "dispatch_rule.py").is_file():
        return "dispatch_rule"
    if (candidate_root / "solver.py").is_file():
        return "solver"
    files = candidate_runtime.get("files", []) or []
    paths = {str(item.get("path")) for item in files if isinstance(item, dict)}
    if "dispatch_rule.py" in paths:
        return "dispatch_rule"
    if "solver.py" in paths:
        return "solver"
    raise ValueError("File candidate must materialize dispatch_rule.py or solver.py.")


def _extract_solution_schedule(candidate_runtime: JsonDict, instance: JsonDict) -> List[JsonDict]:
    solutions = candidate_runtime.get("solutions")
    if not isinstance(solutions, dict):
        raise TypeError("Schedule-solution candidates must define a solutions object.")
    instance_id = str(instance.get("_optpilot_instance_id") or instance.get("name") or "")
    if not instance_id:
        raise ValueError("Cannot select a schedule solution because the instance has no OptPilot instance id.")
    solution = solutions.get(instance_id)
    if not isinstance(solution, dict):
        raise KeyError(f"Schedule-solution candidate does not define a solution for instance {instance_id!r}.")
    operations = solution.get("operations")
    if not isinstance(operations, list):
        raise TypeError(f"Solution for instance {instance_id!r} must define an operations list.")
    return list(operations)


def _load_module(path: Path, name: str):
    if not path.is_file():
        raise FileNotFoundError(f"Expected generated Python file at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import generated Python file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_schedule(result: Any) -> List[JsonDict]:
    if isinstance(result, dict) and isinstance(result.get("operations"), list):
        return list(result["operations"])
    if isinstance(result, list):
        return list(result)
    raise TypeError("solve(...) must return a list of operations or {'operations': [...]} dictionary.")
