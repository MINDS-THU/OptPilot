"""COOPA Solve Console — the coopa-solver method's launchable interface.

A single-process web app (Python stdlib only) served on
``$OPTPILOT_INTERFACE_PORT``:

- **Interactive mode**: extract a confidence-scored formulation, present it
  with element provenance for review, accept user feedback (re-extract) or
  approval (solve), then show the generated solver code and the numeric
  result.
- **Automatic mode**: run the full pipeline unattended and show everything
  at the end.
- **Mock mode**: canned formulation/code/result so the console can be
  demonstrated and tested without COOPA, network, or API keys.

Jobs run on daemon threads; the page polls job state. COOPA is imported
lazily per job through ``coopa_shim`` so the console starts instantly and
reports readiness separately.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4-pro"
MAX_JOBS = 32

sys.path.insert(0, str(ROOT))


def _coopa_home_candidates() -> list[Path]:
    candidates = []
    raw = os.environ.get("COOPA_HOME", "").strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.append(ROOT / "coopa_home")
    return candidates


def _resolve_coopa_home() -> Path | None:
    for candidate in _coopa_home_candidates():
        if (candidate / "apps" / "operations_research").is_dir():
            return candidate
    return None


_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _runtime_root() -> Path:
    raw = os.environ.get("OPTPILOT_INTERFACE_RUNTIME_ROOT", "")
    root = Path(raw) if raw else ROOT / ".runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_job(payload: dict) -> dict:
    job_id = uuid.uuid4().hex[:12]
    workspace = _runtime_root() / "jobs" / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "mode": payload["mode"],
        "mock": bool(payload.get("mock")),
        "problem": payload["problem"],
        "model": str(payload.get("model") or DEFAULT_MODEL),
        "agent_mode": str(payload.get("agentMode") or "manager"),
        "skip_formulation": bool(payload.get("skipFormulation")),
        "max_refinement_iterations": int(payload.get("maxRefinementIterations") or 2),
        "workspace": str(workspace),
        "phase": "queued",
        "status_note": "Queued.",
        "formulation": None,
        "confidence": None,
        "refinement_iterations": 0,
        "feedback_history": [],
        "report": None,
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _JOBS_LOCK:
        if len(_JOBS) >= MAX_JOBS:
            oldest = min(_JOBS.values(), key=lambda j: j["created_at"])
            if oldest["phase"] in ("done", "failed"):
                _JOBS.pop(oldest["id"], None)
            else:
                raise ValueError("Too many active solve jobs; try again later.")
        _JOBS[job_id] = job
    return job


def _update(job: dict, **fields) -> None:
    with _JOBS_LOCK:
        job.update(fields)
        job["updated_at"] = time.time()


def _public_job(job: dict) -> dict:
    with _JOBS_LOCK:
        return {
            key: job[key]
            for key in (
                "id",
                "mode",
                "mock",
                "problem",
                "model",
                "agent_mode",
                "skip_formulation",
                "max_refinement_iterations",
                "phase",
                "status_note",
                "formulation",
                "confidence",
                "refinement_iterations",
                "feedback_history",
                "report",
                "error",
            )
        }


# ---------------------------------------------------------------- mock data


# Mirrors COOPA's OptimizationFormulation / FormulationEvaluation shapes so
# the mock demos the exact rendering the real pipeline produces.
_MOCK_FORMULATION = {
    "question": "MOCK — what mix of x and y maximizes 3x + 5y?",
    "parameters": [
        {
            "name": "profit_x", "data_type": "int", "value": 3, "units": "$/unit",
            "description": "Objective coefficient of x",
            "source": {"quote": "Maximize 3x + 5y", "location": "objective", "note": None},
        },
        {
            "name": "profit_y", "data_type": "int", "value": 5, "units": "$/unit",
            "description": "Objective coefficient of y",
            "source": {"quote": "Maximize 3x + 5y", "location": "objective", "note": None},
        },
    ],
    "variables": [
        {
            "name": "x", "data_type": "continuous", "domain": "x >= 0",
            "description": "First decision quantity",
            "source": {"quote": "x >= 0", "location": None, "note": None},
        },
        {
            "name": "y", "data_type": "continuous", "domain": "y >= 0",
            "description": "Second decision quantity",
            "source": {"quote": "y >= 0", "location": None, "note": None},
        },
    ],
    "objective": {
        "sense": "maximize", "expression": "3x + 5y",
        "description": "Maximize total value", "variables_involved": ["x", "y"],
        "source": {"quote": "Maximize 3x + 5y", "location": None, "note": None},
    },
    "constraints": [
        {
            "name": "cap_x", "sense": "<=", "expression": "x <= 4",
            "variables_involved": ["x"],
            "source": {"quote": "x <= 4", "location": None, "note": None},
        },
        {
            "name": "cap_y", "sense": "<=", "expression": "2y <= 12",
            "variables_involved": ["y"],
            "source": {"quote": "2y <= 12", "location": None, "note": None},
        },
        {
            "name": "joint_capacity", "sense": "<=", "expression": "3x + 2y <= 18",
            "variables_involved": ["x", "y"],
            "source": {"quote": "3x + 2y <= 18", "location": None, "note": None},
        },
    ],
}

_MOCK_CONFIDENCE = {
    "parameters": {"confidence": 95, "explanation": "MOCK — placeholder score."},
    "decision_variables": {"confidence": 92, "explanation": "MOCK — placeholder score."},
    "objective": {"confidence": 96, "explanation": "MOCK — placeholder score."},
    "constraints": {"confidence": 90, "explanation": "MOCK — placeholder score."},
    "overall_confidence": 93,
    "overall_assessment": "MOCK confidence — no LLM was consulted.",
}

_MOCK_CODE = """# MOCK solver — no LLM generated this.
from pyomo.environ import *
model = ConcreteModel()
model.x = Var(bounds=(0, 4))
model.y = Var(bounds=(0, None))
model.obj = Objective(expr=3 * model.x + 5 * model.y, sense=maximize)
model.c1 = Constraint(expr=2 * model.y <= 12)
model.c2 = Constraint(expr=3 * model.x + 2 * model.y <= 18)
# Known optimum for the classic example: x=2, y=6 -> 36
print("RESULT: 36.0")
"""


def _mock_report(job: dict) -> dict:
    return {
        "schema": "optpilot.or-solving-report.v1",
        "mode": "mock",
        "problem": job["problem"],
        "model": "none",
        "routing": "mock",
        "agent_response": "MOCK RESULT — no solver ran. Placeholder answer: 36.0",
        "predicted": 36.0,
        "formulation": job["formulation"],
        "formulation_confidence": job["confidence"],
        "refinement_iterations": job["refinement_iterations"],
        "generated_files": [
            {"path": "solve.py", "bytes": len(_MOCK_CODE), "content": _MOCK_CODE}
        ],
    }


# ------------------------------------------------------------------- phases


def _run_formulation(job: dict, feedback: str | None = None) -> None:
    _update(
        job,
        phase="formulating",
        error=None,
        status_note=(
            "Incorporating your feedback into a revised formulation…"
            if feedback
            else "Extracting a confidence-scored formulation…"
        ),
    )
    try:
        if job["mock"]:
            time.sleep(1.0)
            formulation = dict(_MOCK_FORMULATION)
            if feedback:
                formulation["note"] = (
                    f"MOCK revision incorporating feedback: {feedback[:200]}"
                )
            _update(
                job,
                formulation=formulation,
                confidence=dict(_MOCK_CONFIDENCE),
                refinement_iterations=1,
            )
        else:
            from coopa_shim import extract_formulation

            extracted = extract_formulation(
                job["problem"],
                model_id=job["model"],
                max_refinement_iterations=job["max_refinement_iterations"],
                feedback=feedback,
                work_dir=job["workspace"],
            )
            _update(
                job,
                formulation=extracted["formulation"],
                confidence=extracted["confidence"],
                refinement_iterations=extracted["refinement_iterations"],
            )
        if job["mode"] == "interactive":
            _update(
                job,
                phase="awaiting_review",
                status_note=(
                    "Review the formulation: approve it to solve, or send "
                    "feedback for a revision."
                ),
            )
        else:
            _run_solve(job)
    except Exception as error:
        _update(
            job,
            phase="failed",
            error=f"{error}\n\n{traceback.format_exc()[-2000:]}",
            status_note="Formulation failed.",
        )


def _run_solve(job: dict) -> None:
    _update(
        job,
        phase="solving",
        error=None,
        status_note="Routing to the optimizer agents and generating solver code…",
    )
    try:
        if job["mock"]:
            time.sleep(1.5)
            report = _mock_report(job)
        else:
            from coopa_shim import solve_with_formulation

            report = solve_with_formulation(
                job["problem"],
                job["formulation"],
                model_id=job["model"],
                agent_mode=job["agent_mode"],
                work_dir=job["workspace"],
                formulation_confidence=job["confidence"],
                refinement_iterations=job["refinement_iterations"],
            )
        _update(job, report=report, phase="done", status_note="Solved.")
    except Exception as error:
        _update(
            job,
            phase="failed",
            error=f"{error}\n\n{traceback.format_exc()[-2000:]}",
            status_note="Solving failed.",
        )


def _start_job(job: dict) -> None:
    if job["mode"] == "auto" and job["skip_formulation"]:
        target = _run_solve
    else:
        target = _run_formulation
    threading.Thread(target=target, args=(job,), daemon=True).start()


# --------------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "CoopaSolveConsole/1.0"

    def log_message(self, fmt, *args):  # noqa: N802 - quieter stderr
        sys.stderr.write("console: " + fmt % args + "\n")

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            raise ValueError("Request body length is invalid.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            # Read per request: the catalog source is bind-mounted into the
            # launch runtime, so page edits are visible without a relaunch.
            self._html((ROOT / "solve_console.html").read_text(encoding="utf-8"))
            return
        if self.path == "/api/status":
            home = _resolve_coopa_home()
            self._json(
                {
                    "coopa_ready": home is not None,
                    "coopa_home": str(home) if home else None,
                    "api_key_configured": bool(os.environ.get("OPENROUTER_API_KEY")),
                    "default_model": DEFAULT_MODEL,
                    "candidates_checked": [str(c) for c in _coopa_home_candidates()],
                }
            )
            return
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{12})", self.path)
        if match:
            with _JOBS_LOCK:
                job = _JOBS.get(match.group(1))
            if job is None:
                self._json({"error": "Unknown job."}, status=404)
            else:
                self._json(_public_job(job))
            return
        self._json({"error": "Not found."}, status=404)

    def do_POST(self):  # noqa: N802
        try:
            if self.path == "/api/solve":
                payload = self._read_body()
                problem = str(payload.get("problem") or "").strip()
                mode = str(payload.get("mode") or "interactive")
                if not problem:
                    raise ValueError("Enter the problem statement first.")
                if mode not in ("interactive", "auto"):
                    raise ValueError("mode must be interactive or auto.")
                if not payload.get("mock"):
                    if _resolve_coopa_home() is None:
                        raise ValueError(
                            "COOPA is not available in this runtime. Place a "
                            "COOPA checkout at methods/coopa_solver/coopa_home "
                            "(it is deliberately not redistributed), or tick "
                            "Mock to demo the console without it."
                        )
                    if not os.environ.get("OPENROUTER_API_KEY"):
                        raise ValueError(
                            "OPENROUTER_API_KEY is not granted to this "
                            "interface; configure it in Studio Settings."
                        )
                job = _new_job({**payload, "problem": problem, "mode": mode})
                _start_job(job)
                self._json(_public_job(job))
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/(feedback|approve)", self.path)
            if match:
                job_id, action = match.group(1), match.group(2)
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                if job is None:
                    self._json({"error": "Unknown job."}, status=404)
                    return
                if job["phase"] != "awaiting_review":
                    raise ValueError(
                        "This job is not awaiting review "
                        f"(current phase: {job['phase']})."
                    )
                if action == "feedback":
                    feedback = str(self._read_body().get("feedback") or "").strip()
                    if not feedback:
                        raise ValueError("Feedback text is empty.")
                    with _JOBS_LOCK:
                        job["feedback_history"].append(feedback)
                    threading.Thread(
                        target=_run_formulation, args=(job, feedback), daemon=True
                    ).start()
                else:
                    threading.Thread(
                        target=_run_solve, args=(job,), daemon=True
                    ).start()
                self._json(_public_job(job))
                return
            self._json({"error": "Not found."}, status=404)
        except ValueError as error:
            self._json({"error": str(error)}, status=400)
        except Exception as error:  # pragma: no cover - defensive
            self._json({"error": f"Internal error: {error}"}, status=500)


def main() -> int:
    port = int(
        os.environ.get("OPTPILOT_INTERFACE_PORT")
        or os.environ.get("PORT")
        or 8000
    )
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), _Handler)
    sys.stderr.write(f"COOPA Solve Console listening on {host}:{port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
