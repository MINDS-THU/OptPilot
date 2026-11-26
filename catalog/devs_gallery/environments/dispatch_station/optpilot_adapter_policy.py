"""Starter adapter: evaluate an editable policy against the generated simulator.

Per candidate: overlay the candidate policy over the bundle's declared
policy file, run one replication per configured seed, score each from the
summary metrics, keep the worst replication's converted SQLite trace, and
report aggregate metric values. Review the seed handling before enabling.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

_POLICY_FILE = 'devs_project/policy.py'
_ENTRYPOINT = 'create_policy'
_MAX_TRACE_ROWS = 200000


def _run_replication(simulator, results_dir, settings, seed):
    results_dir.mkdir(parents=True, exist_ok=True)
    for stale in results_dir.iterdir():
        stale.unlink()
    argv = [sys.executable, "-u", "run.py"]
    seed_argument = str(settings.get("seedArgument") or "")
    if seed_argument:
        argv += [f"--{seed_argument}", str(seed)]
    completed = subprocess.run(
        argv,
        cwd=simulator,
        env={
            "PATH": str(Path(sys.executable).parent),
            "HOME": str(results_dir),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONHASHSEED": str(seed),
            "OPTPILOT_SIMULATION_RESULTS_DIR": str(results_dir),
            "DEVS_SIMULATION_RESULTS_DIR": str(results_dir),
            **(
                {"PYTHONPATH": __import__("os").environ["PYTHONPATH"]}
                if __import__("os").environ.get("PYTHONPATH")
                else {}
            ),
        },
        capture_output=True,
        text=True,
        timeout=float(settings.get("timeoutSeconds", 300.0)),
    )
    if completed.returncode:
        raise RuntimeError(
            f"Simulation exited with {completed.returncode}: "
            + completed.stderr[-2000:]
        )
    summary = json.loads((results_dir / "summary.json").read_text(encoding="utf-8"))
    metrics = summary.get("metrics") or {}
    score_metric = str(settings.get("scoreMetric") or "score")
    if score_metric not in metrics:
        raise RuntimeError(
            f"summary.json does not report the score metric {score_metric!r}."
        )
    value = float(metrics[score_metric])
    score = value if settings.get("scoreDirection") != "minimize" else -value
    return score, metrics


def _convert_trace(jsonl_path, database_path, kpis):
    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE events (sequence INTEGER, record_sequence INTEGER, "
            "simulation_time REAL, component TEXT, port TEXT, value TEXT)"
        )
        connection.execute(
            "CREATE TABLE states (sequence INTEGER, record_sequence INTEGER, "
            "simulation_time REAL, component TEXT, phase TEXT, sigma REAL)"
        )
        connection.execute("CREATE TABLE kpi (name TEXT, value REAL)")
        for row in rows[1:-1][:_MAX_TRACE_ROWS]:
            if row.get("record_type") == "event":
                connection.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row.get("sequence"), row.get("record_sequence"),
                        row.get("simulation_time"), row.get("component"),
                        row.get("port"), json.dumps(row.get("value"), default=str)[:2000],
                    ),
                )
            elif row.get("record_type") == "state":
                connection.execute(
                    "INSERT INTO states VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row.get("sequence"), row.get("record_sequence"),
                        row.get("simulation_time"), row.get("component"),
                        row.get("phase"), row.get("sigma"),
                    ),
                )
        for name, value in sorted((kpis or {}).items()):
            if isinstance(value, (int, float)):
                connection.execute(
                    "INSERT INTO kpi VALUES (?, ?)", (str(name), float(value))
                )
        connection.commit()
    finally:
        connection.close()


def _prepared_simulator(workspace, candidate_dir):
    simulator = workspace / "simulator"
    if not simulator.is_dir():
        # Replay contexts run in a bare workspace without the trial
        # materialization; copy the environment-source simulator tree.
        source = Path(__file__).resolve().parent / "simulator"
        shutil.copytree(
            source,
            simulator,
            ignore=shutil.ignore_patterns("__pycache__", "runtime_dependencies"),
        )
        # The environment source projection is read-only; the copy must be
        # writable so the candidate policy can overlay its declared file.
        import os as _os

        for directory, _subdirs, files in _os.walk(simulator):
            _os.chmod(directory, 0o755)
            for name in files:
                _os.chmod(Path(directory) / name, 0o644)
    policy_target = simulator / _POLICY_FILE
    candidate_policy = candidate_dir / Path(_POLICY_FILE).name
    if not candidate_policy.is_file():
        raise FileNotFoundError(
            f"Candidate must provide {Path(_POLICY_FILE).name}."
        )
    policy_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(candidate_policy, policy_target)
    return simulator


def evaluate(candidate_runtime, context):
    workspace = Path(context["workspace"]).resolve()
    candidate_dir = Path(
        candidate_runtime.get("candidateRoot") or workspace / "candidate"
    ).resolve()
    settings = dict(context.get("settings") or {})
    seeds = settings.get("seeds") or [101, 102, 103]
    simulator = _prepared_simulator(workspace, candidate_dir)

    records = []
    for seed in seeds:
        score, metrics = _run_replication(
            simulator, workspace / f"results-{seed}", settings, int(seed)
        )
        records.append((int(seed), score, metrics))
    worst_seed, worst_score, worst_metrics = min(
        records, key=lambda record: (record[1], record[0])
    )
    database_path = workspace / "worst_run.db"
    if database_path.exists():
        database_path.unlink()
    trace = workspace / f"results-{worst_seed}" / "event_trace.jsonl"
    if trace.is_file():
        _convert_trace(trace, database_path, worst_metrics)
    scores = [record[1] for record in records]
    metric_values = {
        "mean_total_score": sum(scores) / len(scores),
        "min_total_score": min(scores),
        "max_total_score": max(scores),
        "worst_seed": float(worst_seed),
        "worst_total_score": worst_score,
    }
    output_files = []
    if database_path.is_file():
        output_files.append(
            {
                "declaration_id": "worst_run",
                "name": "worst_run",
                "path": database_path.relative_to(workspace).as_posix(),
                "kind": "file",
                "media_type": "application/vnd.sqlite3",
                "metadata": {"seed": worst_seed},
            }
        )
    return {
        "status": "success",
        "metric_values": metric_values,
        "output_files": output_files,
        "event_summary": {"replications": len(records), "worst_seed": worst_seed},
    }


def replay_candidate(*, candidate_dir, settings, seed, database_path):
    workspace = Path(database_path).resolve().parent
    simulator = _prepared_simulator(workspace, Path(candidate_dir))
    results_dir = workspace / f"replay-{int(seed)}"
    score, metrics = _run_replication(
        simulator, results_dir, dict(settings), int(seed)
    )
    trace = results_dir / "event_trace.jsonl"
    database = Path(database_path)
    if database.exists():
        database.unlink()
    if trace.is_file():
        _convert_trace(trace, database, metrics)
    else:
        _convert_trace_empty = sqlite3.connect(database)
        _convert_trace_empty.execute("CREATE TABLE kpi (name TEXT, value REAL)")
        _convert_trace_empty.commit()
        _convert_trace_empty.close()
    return {"total_score": score, "metrics": metrics}
