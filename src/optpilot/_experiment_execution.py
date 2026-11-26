"""Execute trusted Python code from frozen versions and checkpoint each Trial and Attempt."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from ._experiment_store import (encode, get_run, get_trials, materialize, metric_names, now,
                                persist_version, put_trial, transaction, version_spec)
from ._experiment_study import candidate_value, schedule
from .realm.refs import request_digest
from . import experiment_resource_plan as resources

WORKER = str(Path(__file__).with_name("_experiment_worker.py").resolve())


class Cancelled(Exception):
    pass


def check_cancelled(db, run_id):
    if get_run(db, run_id)["cancelled"]:
        raise Cancelled("Evaluation cancelled.")


def stop_process(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.communicate()


def python_call(db, run_id, mode, payload, directory, timeout, on_launch):
    check_cancelled(db, run_id)
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", WORKER, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=directory,
        start_new_session=os.name == "posix",
    )
    request = encode(payload) + "\n"
    deadline = time.monotonic() + timeout
    try:
        on_launch(process.pid)
        while True:
            check_cancelled(db, run_id)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"The {mode} worker exceeded {timeout} seconds.")
            try:
                stdout, stderr = process.communicate(request, timeout=min(left, 0.3))
                break
            except subprocess.TimeoutExpired:
                request = None
    except BaseException:
        stop_process(process)
        raise
    if process.returncode:
        raise RuntimeError(stderr[-16000:] or f"The {mode} worker exited with status {process.returncode}.")
    try:
        response = json.loads(stdout)
    except ValueError as exc:
        raise RuntimeError("Invalid JSON response from worker: " + (stderr or stdout)[-2000:]) from exc
    if "error" in response:
        raise RuntimeError(response["error"] + ("\n" + stderr[-4000:] if stderr else ""))
    return response["value"], stderr[-16000:]


def evaluate(db, run_id, study, trial, attempt_number, on_launch, bindings=()):
    task = version_spec(db, trial["taskVersionId"])
    instance = version_spec(db, trial["instanceVersionId"])
    candidate = version_spec(db, trial["candidateVersionId"])
    environments = [version_spec(db, dep) for dep in task["dependencies"]]
    settings = dict(environments[0].get("settings", {})) if environments else {}
    for key, parameter in task.get("environmentParameters", {}).items():
        settings[key] = instance["parameters"][parameter]
    with TemporaryDirectory(prefix="optpilot-trial-") as temp:
        grader = Path(temp) / "grader"
        grader.mkdir()
        materialize(db, grader, [*environments, task, instance])
        submission = {"format": candidate["format"]}
        if candidate["format"] == "parameters":
            submission["value"] = candidate_value(task, candidate)
        else:
            directory = Path(temp) / "submission"
            directory.mkdir()
            materialize(db, directory, [candidate])
            submission.update(entrypoint=candidate["entrypoint"], directory=str(directory))
            submission.update({key: task["decisionContract"][key] for key in ("input", "output")
                               if key in task["decisionContract"]})
        payload = {
            "entrypoint": task["evaluator"], "candidate": submission,
            "context": {
                "parameters": instance["parameters"], "private": instance.get("private", {}),
                "environment": settings, "seed": trial["seed"], "repetition": trial["repetition"],
                "attempt": attempt_number, "workspace": str(grader),
            },
        }
        if bindings:
            payload["context"]["resourceBindings"] = list(bindings)
        if study.get("sshTarget"):
            from .experiment_ssh import evaluate as evaluate_remote
            value, logs = evaluate_remote(db, run_id, study, trial, attempt_number, on_launch, payload,
                                          grader, directory if candidate["format"] == "program" else None)
        else:
            value, logs = python_call(db, run_id, "evaluate", payload, grader, study["timeoutSeconds"], on_launch)
    if not isinstance(value, dict):
        raise ValueError("A Task evaluator must return a metric mapping or {metrics, evidence}.")
    metrics = value.get("metrics", value)
    if not isinstance(metrics, dict) or set(metrics) != set(metric_names(task)):
        raise ValueError(f"Expected exactly these Task metrics: {metric_names(task)}")
    if not all(type(v) in (int, float) and math.isfinite(v) for v in metrics.values()):
        raise ValueError("Metric values must be finite numbers.")
    result = {"metrics": metrics, "evidence": value.get("evidence")} if "metrics" in value else {"metrics": metrics}
    if logs:
        result["logs"] = logs
    return result


def checkpoint(db, run_id, trial):
    with transaction(db):
        put_trial(db, run_id, trial)


def _finish_attempt(db, run_id, trial, handle, attempt):
    with transaction(db):
        put_trial(db, run_id, trial)
        resources.finish_resources(db, handle, attempt["state"], started=bool(attempt.get("usedVersionIds")),
                                   elapsed_seconds=attempt.get("elapsedSeconds"))
    resources.recover_settlements(db, run_id)


def execute_trial(db, run_id, study, trial):
    if trial["state"] not in ("queued", "running"):
        return
    attempts = trial["attempts"]
    in_flight = attempts[-1] if attempts and attempts[-1]["state"] == "running" else None
    if in_flight and not study.get("sshTarget"):
        resources.check_worker(in_flight)
        activity_id = f"{trial['id']}/{in_flight['number']}"
        handle = resources.activity_for(db, run_id, "attempt", activity_id)
        in_flight.update(state="failed", finishedAt=now(), error="The previous worker stopped before recording an outcome.")
        trial.update(state="queued" if len(attempts) <= study["retries"] else "failed", error=in_flight["error"])
        _finish_attempt(db, run_id, trial, handle, in_flight)
        in_flight = None
    while in_flight or len(attempts) <= study["retries"]:
        check_cancelled(db, run_id)
        number = in_flight["number"] if in_flight else len(attempts) + 1
        if not in_flight and attempts:
            resources.check_retry(db, run_id, trial["id"], attempts[-1]["number"])
        handle = resources.begin_trial_resources(db, run_id, study, trial, number)
        attempt = in_flight
        if attempt is None:
            attempt = {"number": number, "state": "running", "startedAt": now()}
            attempts.append(attempt)
            trial["state"] = "running"
            checkpoint(db, run_id, trial)
        in_flight = None
        start_time = None

        def record_submission(pid=None):
            nonlocal start_time
            if pid is not None:
                attempt["workerPid"] = pid
            start_time = time.monotonic()
            trial["usedVersionIds"] = trial["coordinateVersionIds"]
            attempt["usedVersionIds"] = trial["usedVersionIds"]
            checkpoint(db, run_id, trial)

        try:
            result = evaluate(db, run_id, study, trial, number, record_submission, handle["items"])
        except resources.ResourceWait:
            # Remote submission may have succeeded: retain its identity, capacity and Attempt.
            checkpoint(db, run_id, trial)
            raise
        except Cancelled:
            if study.get("sshTarget"):
                elapsed = attempt.get("remote", {}).get("elapsedSeconds")
                if elapsed is not None:
                    attempt["elapsedSeconds"] = elapsed
            elif start_time is not None:
                attempt["elapsedSeconds"] = time.monotonic() - start_time
            checkpoint(db, run_id, trial)
            raise
        except (ValueError, RuntimeError, OSError, ArithmeticError) as exc:
            attempt.update(state="failed", finishedAt=now(), error=str(exc))
            trial.update(state="queued" if len(attempts) <= study["retries"] else "failed", error=str(exc))
        else:
            attempt.update(state="succeeded", finishedAt=now(), **result)
            trial.pop("error", None)
            trial.update(state="succeeded", **result)
        if study.get("sshTarget"):
            elapsed = attempt.get("remote", {}).get("elapsedSeconds")
            if elapsed is not None:
                attempt["elapsedSeconds"] = elapsed
        elif start_time is not None:
            attempt["elapsedSeconds"] = time.monotonic() - start_time
        _finish_attempt(db, run_id, trial, handle, attempt)
        if trial["state"] == "succeeded":
            break


def generate(db, run_id, study, count):
    from ._experiment_catalog import freeze_one
    from .schema_validation import validate_experiment_schema

    vid = study["generatorVersionId"]
    generator = version_spec(db, vid)
    feedback = []
    for trial in get_trials(db, run_id):
        task = version_spec(db, trial["taskVersionId"])
        names = task.get("feedbackMetrics", [])
        if names and trial["state"] == "succeeded":
            candidate = version_spec(db, trial["candidateVersionId"])
            feedback.append({
                "trialId": trial["id"], "candidateVersionId": trial["candidateVersionId"],
                "format": candidate["format"], "value": candidate.get("value"),
                "instanceVersionId": trial["instanceVersionId"],
                "metrics": {name: trial["metrics"][name] for name in names},
            })
    remaining = study["generationBudget"] - count
    context = {"feedback": feedback, "remainingBudget": remaining, "generatedCount": count,
               "seed": study["seed"],
               "tasks": [{"decisionContract": version_spec(db, t)["decisionContract"],
                          "observations": version_spec(db, t).get("observations")}
                         for t in study["taskVersionIds"]]}
    prior_generation = db.execute("SELECT id FROM generations WHERE run=? AND json_extract(spec,'$.state')='failed' "
                                  "AND json_extract(spec,'$.input.generatedCount')=? ORDER BY rowid DESC LIMIT 1",
                                  (run_id, count)).fetchone()
    if prior_generation:
        old = resources.activity_for(db, run_id, "generation", prior_generation["id"])
        resources.check_reconciliation(old, "generation")
    ordinal = db.execute("SELECT COUNT(*) FROM generations WHERE run=?", (run_id,)).fetchone()[0]
    generation_id = "generation-" + request_digest([run_id, count, ordinal])
    handle = resources.begin_generation(db, run_id, study, generation_id)
    start_time = None
    if handle["items"]:
        context["resourceBindings"] = handle["items"]
    receipt = {"id": generation_id, "generatorVersionId": vid, "state": "running",
               "startedAt": now(), "input": context, "candidateVersionIds": []}
    def checkpoint_generation():
        db.execute("INSERT INTO generations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET spec=excluded.spec",
                   (generation_id, run_id, encode(receipt)))
    def record_generator(pid):
        nonlocal start_time
        receipt["workerPid"] = pid
        start_time = time.monotonic()
        with transaction(db):
            db.execute("UPDATE runs SET generator_used=1 WHERE id=?", (run_id,))
            checkpoint_generation()
    try:
        with TemporaryDirectory(prefix="optpilot-generator-") as temp:
            directory = Path(temp)
            materialize(db, directory, [generator])
            value, logs = python_call(db, run_id, "generate", {
                "entrypoint": generator["entrypoint"], "context": dict(context, workspace=str(directory))},
                directory, study["timeoutSeconds"], record_generator)
            batch = value if isinstance(value, list) else [value]
            if len(batch) > remaining or not all(isinstance(v, dict) for v in batch):
                raise ValueError(f"Generator must return up to {remaining} candidate submissions.")
            candidate_format = context["tasks"][0]["decisionContract"]["format"]
            with transaction(db):
                check_cancelled(db, run_id)
                for offset, item in enumerate(batch, start=1):
                    ordinal = count + offset
                    # Parameter generators may return a bare value. Program submissions declare
                    # entrypoint/files in this generation's workspace; snapshot before it is removed.
                    if candidate_format == "program" or generator.get("outputFormat") == "candidate":
                        raw = dict(item)
                        raw.setdefault("format", candidate_format)
                    else:
                        raw = {"format": "parameters", "value": item}
                    raw["id"] = "generated-" + request_digest([run_id, ordinal])
                    raw.setdefault("name", f"{generator['name']} · {ordinal}")
                    validate_experiment_schema({"candidates": [raw]})
                    for task_id in study["taskVersionIds"]:
                        candidate_value(version_spec(db, task_id), raw)
                    version = freeze_one(directory, db, "candidate", raw, {}, provenance={
                        "dependencies": [vid], "generatedByRunId": run_id,
                        "generatorVersionId": vid, "generationId": generation_id,
                    })["id"]
                    db.execute("INSERT INTO proposals VALUES (?,?,?)", (run_id, ordinal, version))
                    schedule(db, run_id, study, version)
                    receipt["candidateVersionIds"].append(version)
                if not batch:
                    db.execute("UPDATE runs SET generator_done=1 WHERE id=?", (run_id,))
                receipt.update(state="succeeded", finishedAt=now(), logs=logs)
                checkpoint_generation()
                resources.finish_resources(db, handle, "succeeded", elapsed_seconds=time.monotonic() - start_time)
    except BaseException as exc:
        receipt.update(state="cancelled" if isinstance(exc, Cancelled) else "failed",
                       finishedAt=now(), error=str(exc), candidateVersionIds=[])
        with transaction(db):
            checkpoint_generation()
            resources.finish_resources(db, handle, receipt["state"], started=start_time is not None,
                                       elapsed_seconds=(time.monotonic()-start_time) if start_time is not None else None)
        resources.recover_settlements(db, run_id)
        raise
    resources.recover_settlements(db, run_id)
    return len(batch)


def run_native(db, run_id, study):
    for row in db.execute("SELECT spec FROM generations WHERE run=? AND json_extract(spec,'$.state')='running'", (run_id,)).fetchall():
        prior = json.loads(row["spec"])
        resources.check_worker(prior)
        handle = resources.activity_for(db, run_id, "generation", prior["id"])
        prior.update(state="failed", error="Generator stopped before recording its outcome.", finishedAt=now())
        with transaction(db):
            db.execute("UPDATE generations SET spec=? WHERE id=?", (encode(prior), prior["id"]))
            resources.finish_resources(db, handle, "failed")
    resources.recover_settlements(db, run_id)
    while True:
        check_cancelled(db, run_id)
        for trial in get_trials(db, run_id):
            execute_trial(db, run_id, study, trial)
        check_cancelled(db, run_id)
        count = db.execute("SELECT COUNT(*) FROM proposals WHERE run=?", (run_id,)).fetchone()[0]
        if count >= study["generationBudget"] or get_run(db, run_id)["generator_done"]:
            break
        if not generate(db, run_id, study, count):
            break
