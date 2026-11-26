"""Shared normalization for canonical and noncanonical evaluation attempts.

This module contains only semantic conversion of validator/environment results
and exceptions.  It does not know about evidence stores, budgets, run state, or
workspace ownership, so both the legacy evaluator and :mod:`optpilot.attempts`
can depend on it without reversing the intended execution dependency.
"""

from __future__ import annotations

import subprocess
import traceback
from pathlib import Path
from typing import Any, Dict, List

from .candidate_materialization import ValidationReport


JsonDict = Dict[str, Any]

PUBLIC_OBSERVATION_STATUSES = frozenset(
    {"success", "invalid", "failed", "timeout", "partial", "cancelled"}
)


def validation_exception_report(exc: Exception) -> ValidationReport:
    return ValidationReport(
        accepted=False,
        errors=[str(exc)],
        metadata={"exception": error_payload(exc, "validation")},
    )


def exception_evaluation_result(
    exc: Exception, phase: str, workspace: Path
) -> JsonDict:
    return {
        "status": status_for_exception(exc),
        "metric_values": {},
        "constraint_results": {},
        "output_files": failure_output_files(workspace),
        "event_summary": {
            "error": error_payload(exc, phase),
        },
    }


def validate_environment_result(result: Any) -> JsonDict:
    if not isinstance(result, dict):
        raise TypeError("Environment evaluator result must be a JSON-like object.")
    status = result.get("status", "success")
    if not isinstance(status, str):
        raise TypeError("Environment evaluator result status must be a string.")
    if status not in PUBLIC_OBSERVATION_STATUSES:
        return unsupported_observation_status_result(result, status)
    metric_values = result.get("metric_values", {})
    if not isinstance(metric_values, dict):
        raise TypeError("Environment evaluator result metric_values must be a dict.")
    for metric_name, metric_value in metric_values.items():
        if not isinstance(metric_name, str):
            raise TypeError("Environment evaluator metric names must be strings.")
        if not isinstance(metric_value, (int, float, bool)):
            raise TypeError(
                f"Environment evaluator metric {metric_name!r} must be numeric or boolean."
            )
    constraint_results = result.get("constraint_results", {})
    if not isinstance(constraint_results, dict):
        raise TypeError("Environment evaluator result constraint_results must be a dict.")
    output_files = result.get("output_files", [])
    if not isinstance(output_files, list):
        raise TypeError("Environment evaluator result output_files must be a list.")
    for index, output_file in enumerate(output_files):
        if not isinstance(output_file, dict):
            raise TypeError(
                f"Environment evaluator output_file entry {index} must be an object."
            )
    event_summary = result.get("event_summary", {})
    if not isinstance(event_summary, dict):
        raise TypeError("Environment evaluator result event_summary must be a dict.")
    return result


def unsupported_observation_status_result(
    result: JsonDict, raw_status: str
) -> JsonDict:
    error = {
        "phase": "environment_evaluation",
        "type": "UnsupportedObservationStatus",
        "code": "unsupported_observation_status",
        "message": (
            "Environment evaluator returned unsupported observation status "
            f"{raw_status!r}."
        ),
        "raw_status": raw_status,
    }
    source_summary = result.get("event_summary", {})
    event_summary = dict(source_summary) if isinstance(source_summary, dict) else {}
    prior_errors = event_summary.get("errors", [])
    errors = [error]
    if isinstance(prior_errors, list):
        errors.extend(prior_errors)
    prior_error = event_summary.get("error")
    if prior_error is not None and prior_error not in errors:
        errors.append(prior_error)
    event_summary["error"] = error
    event_summary["errors"] = errors

    output_files = result.get("output_files", [])
    if not isinstance(output_files, list) or any(
        not isinstance(item, dict) for item in output_files
    ):
        output_files = []
    return {
        "status": "failed",
        "metric_values": {},
        "constraint_results": {},
        "output_files": list(output_files),
        "event_summary": event_summary,
    }


def status_for_exception(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    return "failed"


def error_payload(exc: Exception, phase: str) -> JsonDict:
    return {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)
        ),
    }


def failure_output_files(workspace: Path) -> List[JsonDict]:
    output_files: List[JsonDict] = []
    for name, file_type, path in [
        ("cli_candidate_input", "json", workspace / "cli_candidate.json"),
        ("cli_settings_input", "json", workspace / "cli_settings.json"),
        ("cli_result_output", "json", workspace / "cli_result.json"),
        ("cli_stdout", "log", workspace / "cli_stdout.log"),
        ("cli_stderr", "log", workspace / "cli_stderr.log"),
    ]:
        if path.exists():
            output_files.append(
                {"type": file_type, "name": name, "path": str(path)}
            )
    return output_files


__all__ = [
    "PUBLIC_OBSERVATION_STATUSES",
    "error_payload",
    "exception_evaluation_result",
    "failure_output_files",
    "status_for_exception",
    "unsupported_observation_status_result",
    "validate_environment_result",
    "validation_exception_report",
]
