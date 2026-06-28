"""OptPilot evaluator wrapper for the job-shop scheduling example."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .simulator import (
    load_instance,
    schedule_by_dispatch_rule,
    summarize_schedule,
    validate_schedule,
    weighted_dispatch_score,
)


JsonDict = Dict[str, Any]


def evaluate(candidate_runtime: JsonDict, context: JsonDict) -> JsonDict:
    workspace = Path(candidate_runtime.get("workspace") or context["workspace"]).resolve()
    candidate_root = Path(candidate_runtime.get("candidateRoot") or workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    mode = _evaluation_mode(candidate_runtime, candidate_root)
    case_results = []
    output_files = []
    for case in _load_cases(context.get("settings", {})):
        result = _evaluate_case(candidate_runtime, candidate_root, context, case, mode)
        case_results.append(result)
        schedule_path = workspace / f"schedule_{case['id']}.json"
        metrics_path = workspace / f"job_shop_metrics_{case['id']}.json"
        schedule_path.write_text(
            json.dumps({"case_id": case["id"], "operations": result["schedule"]}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        metrics_path.write_text(json.dumps(result["metrics"], indent=2, sort_keys=True), encoding="utf-8")
        output_files.extend(
            [
                {"type": "json", "name": f"schedule_{case['id']}", "path": str(schedule_path)},
                {"type": "json", "name": f"job_shop_metrics_{case['id']}", "path": str(metrics_path)},
            ]
        )

    aggregated_metrics = _aggregate_metrics([result["metrics"] for result in case_results])
    summary_path = workspace / "job_shop_metrics.json"
    summary_path.write_text(
        json.dumps(
            {
                "metrics": aggregated_metrics,
                "cases": [
                    {
                        "id": result["id"],
                        "name": result["name"],
                        "metrics": result["metrics"],
                        "operation_count": len(result["schedule"]),
                    }
                    for result in case_results
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    output_files.append({"type": "json", "name": "job_shop_metrics", "path": str(summary_path)})
    return {
        "status": "success",
        "metric_values": aggregated_metrics,
        "output_files": output_files,
        "event_summary": {
            "adapter": "job_shop_scheduling_evaluator",
            "mode": mode,
            "case_count": len(case_results),
            "cases": [result["id"] for result in case_results],
        },
    }


def _evaluate_case(
    candidate_runtime: JsonDict,
    candidate_root: Path,
    context: JsonDict,
    case: JsonDict,
    mode: str,
) -> JsonDict:
    instance = dict(case["payload"])
    job_shop = load_instance(instance)
    if mode == "parameters":
        if "solutions" in candidate_runtime:
            schedule = _extract_solution_schedule(candidate_runtime, case["id"])
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
    return {
        "id": case["id"],
        "name": job_shop.name,
        "metrics": metrics,
        "schedule": validated,
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


def _extract_solution_schedule(candidate_runtime: JsonDict, case_id: str) -> List[JsonDict]:
    solutions = candidate_runtime.get("solutions")
    if not isinstance(solutions, dict):
        raise TypeError("Schedule-solution candidates must define a solutions object.")
    solution = solutions.get(case_id)
    if not isinstance(solution, dict):
        raise KeyError(f"Schedule-solution candidate does not define a solution for case {case_id!r}.")
    operations = solution.get("operations")
    if not isinstance(operations, list):
        raise TypeError(f"Solution for case {case_id!r} must define an operations list.")
    return list(operations)


def _load_cases(settings: JsonDict) -> List[JsonDict]:
    cases = settings.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Job-shop evaluator settings.cases must be a non-empty list.")
    loaded = []
    base_dir = Path(__file__).resolve().parent
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise TypeError(f"Job-shop evaluator settings.cases[{index}] must be an object.")
        case_id = str(case.get("id") or "")
        if not case_id:
            raise ValueError(f"Job-shop evaluator settings.cases[{index}] must define id.")
        if "payload" in case:
            payload = case["payload"]
        elif "path" in case:
            path = Path(str(case["path"]))
            if not path.is_absolute():
                path = base_dir / path
            with path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        else:
            raise ValueError(f"Job-shop evaluator settings.cases[{index}] must define path or payload.")
        if not isinstance(payload, dict):
            raise TypeError(f"Job-shop case {case_id!r} must load to an object.")
        loaded.append({"id": case_id, "payload": dict(payload)})
    return loaded


def _aggregate_metrics(metrics_by_case: List[JsonDict]) -> JsonDict:
    metric_names = sorted({key for metrics in metrics_by_case for key in metrics})
    aggregated: JsonDict = {}
    for name in metric_names:
        values = [float(metrics[name]) for metrics in metrics_by_case if isinstance(metrics.get(name), (int, float, bool))]
        if values:
            aggregated[name] = sum(values) / len(values)
    return aggregated


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
