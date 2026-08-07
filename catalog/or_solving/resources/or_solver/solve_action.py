"""F4 resource action: solve one OR problem for a human user.

Reads validated inputs from ``OPTPILOT_RESOURCE_ACTION_INPUTS_FILE``, runs the
same pruned COOPA pipeline as the ``coopa-solver`` method (reusing
``methods/coopa_solver/coopa_shim.py`` from this package), and writes
human-oriented results under ``OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT``:

- ``answer.txt``   — the numeric answer and a short run summary
- ``report.json``  — the full audit artifact (optpilot.or-solving-report.v1)
- ``workspace/``   — the solver files the agents generated

Unlike the method adapter, stdout needs no protection here: the action
executor captures stdout/stderr as diagnostics and reads results from the
output folder only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_METHOD_DIR = Path(__file__).resolve().parents[2] / "methods" / "coopa_solver"


def _mock_report(problem: str) -> dict:
    return {
        "schema": "optpilot.or-solving-report.v1",
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


def _answer_text(report: dict) -> str:
    predicted = report.get("predicted")
    lines = [
        (
            f"Predicted objective value: {predicted}"
            if isinstance(predicted, (int, float))
            else "No numeric answer was found — see report.json for the full transcript."
        ),
        "",
        f"Mode: {report.get('mode')}",
        f"Routing: {report.get('routing')}",
        f"Model: {report.get('model')}",
        f"Formulation refinement iterations: {report.get('refinement_iterations')}",
        "",
        "Problem:",
        str(report.get("problem") or "").strip(),
        "",
        "Full artifact: report.json — generated solver files: workspace/",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    inputs = json.loads(
        Path(os.environ["OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"]).read_text(
            encoding="utf-8"
        )
    )
    output_root = Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
    problem = str(inputs.get("problem") or "").strip()
    if not problem:
        raise SystemExit("The 'problem' input is empty.")

    workspace = output_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    if inputs.get("mock"):
        report = _mock_report(problem)
    else:
        sys.path.insert(0, str(_METHOD_DIR))
        from coopa_shim import solve_problem

        report = solve_problem(
            problem,
            model_id=str(inputs["model"]),
            agent_mode=str(inputs["agentMode"]),
            skip_formulation=bool(inputs["skipFormulation"]),
            max_refinement_iterations=int(inputs["maxRefinementIterations"]),
            work_dir=str(workspace),
        )

    (output_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_root / "answer.txt").write_text(_answer_text(report), encoding="utf-8")
    print(_answer_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
