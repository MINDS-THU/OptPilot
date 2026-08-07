"""Well-formedness evaluator for one-time OR solution artifacts (W3).

The evaluator deliberately does not re-solve anything: correctness of a
natural-language OR answer is not machine-checkable in general. It scores
whether the returned artifact is a complete, parseable record of what the
solver did, so a Run's `solved` metric means "a well-formed answer with an
auditable trail exists", never "the answer is proven optimal".
"""

from __future__ import annotations

import json
import math

REPORT_SCHEMA = "optpilot.or-solving-report.v1"
_REQUIRED_REPORT_KEYS = ("schema", "mode", "problem", "agent_response")


def evaluate(candidate, context):
    spec = candidate.get("spec", candidate) if isinstance(candidate, dict) else {}
    objective_value = spec.get("objective_value")
    answer_found = bool(spec.get("answer_found"))
    report_text = spec.get("report_json")

    problems = []
    report = None
    if not isinstance(report_text, str) or not report_text.strip():
        problems.append("report_json is empty")
    else:
        try:
            report = json.loads(report_text)
        except ValueError as error:
            problems.append(f"report_json is not valid JSON: {error}")
    if isinstance(report, dict):
        if report.get("schema") != REPORT_SCHEMA:
            problems.append(
                f"report schema is {report.get('schema')!r}, expected {REPORT_SCHEMA!r}"
            )
        for key in _REQUIRED_REPORT_KEYS:
            if not report.get(key):
                problems.append(f"report is missing {key!r}")
    elif report is not None:
        problems.append("report_json must encode a JSON object")

    if answer_found:
        if not isinstance(objective_value, (int, float)) or not math.isfinite(
            float(objective_value)
        ):
            problems.append("answer_found is true but objective_value is not finite")
        if isinstance(report, dict) and report.get("predicted") is None:
            problems.append("answer_found is true but report.predicted is absent")

    solved = 1.0 if answer_found and not problems else 0.0
    return {
        "solved": solved,
        "objective_value": float(objective_value)
        if isinstance(objective_value, (int, float)) and math.isfinite(float(objective_value))
        else 0.0,
        "artifact_bytes": float(len(report_text) if isinstance(report_text, str) else 0),
    }
