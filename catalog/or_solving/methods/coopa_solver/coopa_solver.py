"""Command-protocol batch adapter for one-time COOPA OR solving (W3 over F3).

Executed once per proposal exchange by the retained method worker: reads the
batch request JSON on stdin, solves the per-launch `problem` study input
(F2 — delivered under ``settings["inputs"]``), and writes one candidate to
stdout whose spec carries the numeric answer plus the full solution artifact
(schema ``optpilot.or-solving-report.v1``) as bounded JSON text.

Infrastructure problems (missing input, missing COOPA_HOME, missing
dependencies) raise so the exchange fails loudly with a diagnostic. Solve
quality problems (the pipeline ran but found no numeric answer) return an
artifact with ``answer_found: false`` so the Run completes with solved=0 and
the evidence retained for auditing.
"""

from __future__ import annotations

import json
import os
import sys

REPORT_SCHEMA = "optpilot.or-solving-report.v1"
MAX_REPORT_BYTES = 200_000


def _protect_stdout():
    """Reserve the real stdout for the JSON response only.

    The retained worker parses this process's entire stdout as JSON, but the
    COOPA pipeline prints agent logs to stdout and executes generated solver
    code in child processes that inherit fd 1. Duplicate the true stdout to a
    private handle and point fd 1 at stderr so every stray print — Python or
    subprocess — lands in the worker diagnostics instead of the response.
    """

    real_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(real_fd, "w", encoding="utf-8")


def bounded_report_json(report: dict) -> str:
    text = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if len(text.encode("utf-8")) <= MAX_REPORT_BYTES:
        return text
    # Drop the largest free-text fields first, then hard-truncate.
    for key in ("generated_files", "agent_response", "formulation"):
        if key in report:
            report = dict(report)
            report[key] = f"[truncated: {key} exceeded the retained artifact bound]"
            text = json.dumps(report, ensure_ascii=False, sort_keys=True)
            if len(text.encode("utf-8")) <= MAX_REPORT_BYTES:
                return text
    return text.encode("utf-8")[:MAX_REPORT_BYTES].decode("utf-8", errors="ignore")


def mock_report(problem: str) -> dict:
    return {
        "schema": REPORT_SCHEMA,
        "mode": "mock",
        "problem": problem,
        "agent_response": (
            "MOCK RESULT — no solver ran. This artifact only exercises the "
            "OptPilot machinery; the objective value below is a placeholder."
        ),
        "predicted": 0.0,
        "routing": "mock",
        "formulation": None,
        "formulation_confidence": None,
        "refinement_iterations": 0,
        "generated_files": [],
        "model": "none",
    }


def main() -> int:
    response_stream = _protect_stdout()
    request = json.load(sys.stdin)
    settings = request.get("settings") or {}
    inputs = settings.get("inputs") or {}
    problem = str(inputs.get("problem") or "").strip()
    if not problem:
        raise SystemExit(
            "The 'problem' study input is empty. Launch this Run setup with "
            "--input problem=\"...\" (CLI) or fill the Launch inputs form (Studio)."
        )

    if settings.get("mockSolve"):
        report = mock_report(problem)
        predicted = report["predicted"]
        answer_found = True
    else:
        from coopa_shim import solve_problem

        workspace = str(
            (request.get("runtime_context") or {}).get("method_workspace") or "."
        )
        report = solve_problem(
            problem,
            model_id=str(settings.get("model") or "openrouter/qwen/qwen3-coder"),
            agent_mode=str(settings.get("agentMode") or "manager"),
            skip_formulation=bool(settings.get("skipFormulation")),
            max_refinement_iterations=int(settings.get("maxRefinementIterations") or 2),
            work_dir=workspace,
        )
        predicted = report.get("predicted")
        answer_found = isinstance(predicted, (int, float))

    candidate = {
        "candidate_id": "or-solve-1",
        "format": "parameters",
        "spec": {
            "objective_value": float(predicted) if answer_found else 0.0,
            "answer_found": bool(answer_found),
            "report_json": bounded_report_json(report),
        },
    }
    json.dump({"candidates": [candidate]}, response_stream)
    response_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
