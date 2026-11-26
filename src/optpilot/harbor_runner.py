"""Delegate an entire reviewed Study to Harbor's local-job CLI and reconcile its receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from ._experiment_catalog import safe_path
from ._experiment_execution import Cancelled
from ._experiment_store import (encode, get_run, get_trials, materialize, metric_names,
                                put_trial, transaction, version_spec)
from ._harbor_results import harbor_trial, read_json
from .realm.refs import request_digest


def package_files(root, name):
    path = safe_path(name)
    directory = root.joinpath(path)
    if not directory.is_dir() or any((root / p).is_symlink() for p in [path, *path.parents]):
        raise ValueError(f"Expected a local Harbor task directory at {name!r}.")
    if not all((directory / f).is_file() for f in ("task.toml", "instruction.md")):
        raise ValueError("A Harbor package needs task.toml and instruction.md.")
    result = []
    for folder, dirs, files in os.walk(directory, followlinks=False):
        for item in dirs + files:
            source = Path(folder) / item
            if source.is_symlink():
                raise ValueError(f"Harbor task package contains a symlink: {source.name}")
        result.extend(str((Path(folder) / f).relative_to(root).as_posix()) for f in files)
        if len(result) > 4096:
            raise ValueError("Harbor task package exceeds 4096 files.")
    return result


def job_root(store, run_id):
    return Path(store).resolve() / "harbor" / request_digest(run_id)


def case_name(instance, repetition):
    return "case-" + request_digest([instance, repetition])[:18]


def job_executable():
    configured = os.environ.get("OPTPILOT_HARBOR_EXECUTABLE")
    if configured and not Path(configured).is_absolute():
        raise ValueError("OPTPILOT_HARBOR_EXECUTABLE must be an absolute path to the Harbor executable.")
    path = shutil.which(configured or "harbor")
    if not path:
        raise ValueError("Install Harbor on this computer and make its harbor command available on PATH, or set OPTPILOT_HARBOR_EXECUTABLE to its installed executable.")
    return path


def stage(db, directory, study):
    config = directory / "study.json"
    if config.is_file():
        return
    directory.mkdir(parents=True, exist_ok=True)
    tasks = []
    for instance in study["instanceVersionIds"]:
        iv = version_spec(db, instance)
        task = version_spec(db, iv["dependencies"][0])
        prefix = task["harborTask"] + "/"
        # Each independent repetition gets a unique stable source path. Harbor's own
        # TrialConfig carries this path even if the comparison uses the same agent.
        for rep in range(1, study["repetitions"] + 1):
            target = directory / "tasks" / case_name(instance, rep)
            frozen = {"fileRefs": {k[len(prefix):]: v for k, v in task["fileRefs"].items() if k.startswith(prefix)},
                      "executableFiles": [p[len(prefix):] for p in task.get("executableFiles", []) if p.startswith(prefix)]}
            materialize(db, target, [frozen])
            tasks.append({"path": str(target)})
    settings = {
        "schema_version": 1, "job_name": "evaluation", "jobs_dir": str(directory / "jobs"),
        "tasks": tasks, "agents": [version_spec(db, c)["agent"] for c in study["candidateVersionIds"]],
        "n_attempts": 1, "n_concurrent_trials": 1, "retry": {"max_retries": study["retries"]},
        "environment": {"type": "docker"}, "quiet": True,
    }
    tmp = directory / "study.tmp"
    tmp.write_text(encode(settings), encoding="utf-8")
    tmp.replace(config)


def record_job(db, run_id, job):
    path = job / "result.json"
    if not path.is_file():
        return
    try:
        raw = read_json(path, [32 * 1024 * 1024])
    except json.JSONDecodeError:
        return
    job_id = raw.get("id")
    if not isinstance(job_id, str) or not job_id:
        return
    summary = {"reportedTrials": raw.get("n_total_trials", 0), "stats": raw.get("stats", {})}
    payload = encode(summary)
    prior = db.execute("SELECT job_id,report FROM harbor_jobs WHERE run=?", (run_id,)).fetchone()
    if prior and prior["job_id"] != job_id:
        raise ValueError("Harbor returned a different job ID for this Run.")
    if not prior or prior["report"] != payload:
        with transaction(db):
            db.execute("""INSERT INTO harbor_jobs VALUES (?,?,?,?) ON CONFLICT(run)
                          DO UPDATE SET report=excluded.report""", (run_id, job_id, str(job), payload))


def metadata(db, run_id, *, include_summary=False):
    row = db.execute("SELECT * FROM harbor_jobs WHERE run=?", (run_id,)).fetchone()
    if not row:
        return {}
    report = json.loads(row["report"])
    count = db.execute("""SELECT COUNT(*) FROM trials WHERE run=?
                          AND json_extract(spec,'$.externalTrialId') IS NOT NULL""", (run_id,)).fetchone()[0]
    total = report["reportedTrials"]
    # Resolve a copied project's runner files without rewriting its original receipts.
    db_path = next(r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main")
    relocated = job_root(Path(db_path).parent, run_id) / "jobs" / "evaluation"
    directory = str(relocated) if relocated.is_dir() else row["directory"]
    result = {"runnerDirectory": directory, "external": {
        "source": "Harbor", "jobId": row["job_id"], "reportedTrials": total, "importedTrials": count,
        "coverage": "trials" if count == total else "partial" if count else "summary",
    }}
    if include_summary:
        result["externalSummary"] = report["stats"]
    return result


def read_completed(db, run_id, directory, study):
    job = directory / "jobs" / "evaluation"
    if not job.is_dir():
        return
    record_job(db, run_id, job)
    trials = get_trials(db, run_id)
    cases = {str(directory / "tasks" / case_name(t["instanceVersionId"], t["repetition"])):
             (t["instanceVersionId"], t["repetition"]) for t in trials}
    agents = {}
    for candidate in study["candidateVersionIds"]:
        cfg = version_spec(db, candidate)["agent"]
        agents[candidate] = cfg
    by_coordinate = {(t["instanceVersionId"], t["repetition"], t["candidateVersionId"]): t for t in trials}
    for folder in sorted(job.iterdir()):
        path = folder / "result.json"
        if folder.is_symlink() or not folder.is_dir() or not path.is_file():
            continue
        try:
            raw = read_json(path, [32 * 1024 * 1024])
        except json.JSONDecodeError:
            continue  # Harbor may still be writing this record.
        if not raw.get("finished_at"):
            continue
        row = harbor_trial(raw)
        reported = raw.get("config", {})
        case = cases.get((reported.get("task") or {}).get("path"))
        cfg = reported.get("agent") or {}
        matches = [candidate for candidate, frozen in agents.items() if all(cfg.get(key) == value for key, value in frozen.items())]
        candidate = matches[0] if len(matches) == 1 else None
        if case is None or candidate is None:
            raise ValueError(f"Harbor trial {row['id']} does not match the frozen launch configuration.")
        trial = by_coordinate[(*case, candidate)]
        prior_trial = encode(trial)
        task = version_spec(db, trial["taskVersionId"])
        names = metric_names(task)
        valid = row["state"] == "succeeded" and all(m in row["metrics"] for m in names)
        error = row["evidence"].get("errors") or "Harbor did not report all declared Task metrics."
        trial.update(state="succeeded" if valid else "failed", usedVersionIds=trial["coordinateVersionIds"],
                     metrics={m: row["metrics"][m] for m in names} if valid else {}, externalTrialId=row["id"],
                     evidence={**row["evidence"], "externalTrialId": row["id"], "externalJobId": reported.get("job_id"),
                               "agentConfiguration": row["candidate"], "requestedAgent": agents[candidate],
                               "reportedAgent": raw.get("agent_info", {}), "reportedConfiguration": {key: cfg.get(key) for key in agents[candidate]},
                               "resultFile": str(path), "finishedAt": row["finishedAt"]})
        # Harbor owns retries. The terminal TrialResult alone gives no per-Attempt trace.
        trial.pop("seed", None)
        if any(e.get("exception_type") == "CancelledError" for e in row["evidence"].get("errors", [])):
            trial["state"] = "cancelled"
        if not valid:
            trial["error"] = str(error)
        else:
            trial.pop("error", None)
        if encode(trial) != prior_trial:
            with transaction(db):
                put_trial(db, run_id, trial)


def controller(db, store, run_id, study):
    from ._harbor_auth import environment
    runner_environment = environment()
    directory = job_root(store, run_id)
    stage(db, directory, study)
    receipt = directory / "exit.json"
    supervisor = None
    last_launch = 0.
    while not receipt.is_file():
        if get_run(db, run_id)["cancelled"]:
            (directory / "cancel").touch()
        if supervisor is None or (supervisor.poll() is not None and time.monotonic() - last_launch > 5):
            supervisor = subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).with_name("_harbor_supervisor.py").resolve()),
                 str(directory), job_executable()], cwd=directory,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix", env=runner_environment,
            )
            last_launch = time.monotonic()
        read_completed(db, run_id, directory, study)
        time.sleep(0.3)
    if supervisor is not None:
        supervisor.wait(timeout=5)
    read_completed(db, run_id, directory, study)
    exit_info = json.loads(receipt.read_bytes())
    if exit_info.get("cancelled") or get_run(db, run_id)["cancelled"]:
        raise Cancelled("Harbor job cancelled.")
    if exit_info.get("code"):
        log = directory / "runner.log"
        tail = ""
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 3000))
                tail = stream.read().decode("utf-8", errors="replace")
        raise RuntimeError(exit_info.get("error") or f"Harbor exited with code {exit_info['code']}. {tail}")
    result = read_json(directory / "jobs" / "evaluation" / "result.json", [32 * 1024 * 1024])
    if not result.get("finished_at"):
        raise RuntimeError("Harbor exited without a final job result.")
    if result.get("stats", {}).get("n_cancelled_trials", 0):
        raise Cancelled("Harbor reports a cancelled job.")
    expected = study["plannedTrials"]
    retained = sum(bool(t.get("externalTrialId")) for t in get_trials(db, run_id))
    if result.get("n_total_trials") != expected or retained != expected:
        raise RuntimeError(f"Harbor returned {retained} bound Trial records for {expected} requested Trials.")

