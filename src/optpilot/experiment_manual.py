"""Human-operated trials with stable work orders and idempotent measurement receipts."""
import math

from ._experiment_store import (database, encode, get_run, get_trials, metric_names,
                                now, put_trial, transaction, version_spec)
from .experiment import TERMINAL, execute, run_lock


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS manual_work (
                  run TEXT NOT NULL REFERENCES runs(id), trial TEXT NOT NULL REFERENCES trials(id),
                  state TEXT NOT NULL, operator TEXT, receipt TEXT, payload TEXT,
                  PRIMARY KEY(run,trial), UNIQUE(run,receipt))""")


def _work(db, run_id, trial_id):
    _schema(db)
    row = db.execute("SELECT * FROM manual_work WHERE run=? AND trial=?", (run_id, trial_id)).fetchone()
    if row is None:
        raise ValueError("No work order for this trial. Start or resume the evaluation first.")
    return row


def tasks(store, run_id):
    with database(store) as db:
        _schema(db)
        get_run(db, run_id)
        rows = {r["trial"]: r for r in db.execute("SELECT * FROM manual_work WHERE run=?", (run_id,))}
        result = []
        for trial in get_trials(db, run_id):
            row = rows.get(trial["id"])
            if row is None:
                continue
            task = version_spec(db, trial["taskVersionId"])
            candidate = version_spec(db, trial["candidateVersionId"])
            instance = version_spec(db, trial["instanceVersionId"])
            result.append(dict(id=trial["id"], trialId=trial["id"], candidateVersionId=trial["candidateVersionId"],
                               instanceVersionId=trial["instanceVersionId"], taskVersionId=trial["taskVersionId"],
                               repetition=trial["repetition"], state=row["state"], operator=row["operator"],
                               receipt=row["receipt"], metrics=metric_names(task), candidate=candidate["value"],
                               parameters=instance["parameters"], instructions=task.get("description", task["name"])))
        return {"tasks": result}


def advance(db, store, run_id, study):
    from .experiment_resource_plan import begin_trial_resources, activity_for, finish_resources
    _schema(db)
    for trial in get_trials(db, run_id):
        # A committed measurement may still need its shared reservation released after a crash.
        if trial["state"] == "succeeded":
            handle = activity_for(db, run_id, "manual", trial["id"] + "/1")
            finish_resources(db, handle, "succeeded")
            continue
        if trial["state"] in TERMINAL:
            continue
        begin_trial_resources(db, run_id, study, trial, 1, stage="manual")
        with transaction(db):
            db.execute("INSERT OR IGNORE INTO manual_work(run,trial,state) VALUES (?,?,'offered')", (run_id, trial["id"]))
        return False
    return True


def claim(store, run_id, trial_id, operator):
    if not isinstance(operator, str) or not 0 < len(operator.strip()) <= 200:
        raise ValueError("Enter the operator name.")
    operator = operator.strip()
    with run_lock(store, run_id), database(store) as db, transaction(db):
        row = _work(db, run_id, trial_id)
        if row["state"] == "claimed" and row["operator"] == operator:
            return {"runId": run_id, "trialId": trial_id, "state": "claimed"}
        if row["state"] != "offered" or get_run(db, run_id)["cancelled"]:
            raise ValueError("This work order is no longer available to claim.")
        trial = next(t for t in get_trials(db, run_id) if t["id"] == trial_id)
        db.execute("UPDATE manual_work SET state='claimed',operator=? WHERE run=? AND trial=?", (operator, run_id, trial_id))
        trial.update(state="running", usedVersionIds=trial["coordinateVersionIds"])
        trial["attempts"].append(dict(number=1, state="running", startedAt=now(), usedVersionIds=trial["usedVersionIds"]))
        put_trial(db, run_id, trial)
    return {"runId": run_id, "trialId": trial_id, "state": "claimed"}


def complete(store, run_id, trial_id, metrics, evidence, receipt):
    if not isinstance(receipt, str) or not 0 < len(receipt.strip()) <= 256:
        raise ValueError("Supply the stable measurement receipt.")
    receipt = receipt.strip()
    payload = encode({"metrics": metrics, "evidence": evidence, "receipt": receipt})
    if len(payload.encode()) > 1024 * 1024:
        raise ValueError("Measurement evidence must fit within 1 MiB.")
    with run_lock(store, run_id), database(store) as db:
        with transaction(db):
            row = _work(db, run_id, trial_id)
            run = get_run(db, run_id)
            if row["state"] == "completed":
                if row["payload"] != payload:
                    raise ValueError("This work order already has a different measurement.")
            else:
                if row["state"] != "claimed" or run["cancelled"] or run["state"] in TERMINAL:
                    raise ValueError("Claim the open work order before recording a measurement.")
                if db.execute("SELECT 1 FROM manual_work WHERE run=? AND receipt=? AND trial<>?", (run_id, receipt, trial_id)).fetchone():
                    raise ValueError("The receipt already belongs to another trial.")
                trial = next(t for t in get_trials(db, run_id) if t["id"] == trial_id)
                names = metric_names(version_spec(db, trial["taskVersionId"]))
                if not isinstance(metrics, dict) or set(metrics) != set(names) or not all(
                    type(v) in (int, float) and math.isfinite(v) for v in metrics.values()
                ):
                    raise ValueError(f"Enter a finite numeric reading for each declared metric: {names}")
                measurement = {"receipt": receipt, "operator": row["operator"], "measurement": evidence}
                trial.update(state="succeeded", metrics=metrics, evidence=measurement)
                trial["attempts"][-1].update(state="succeeded", finishedAt=now(), metrics=metrics, evidence=measurement)
                put_trial(db, run_id, trial)
                db.execute("UPDATE manual_work SET state='completed',receipt=?,payload=? WHERE run=? AND trial=?",
                           (receipt, payload, run_id, trial_id))
            review_id = run["review"]
    result = execute(store, review_id, run_id)
    return {"runId": run_id, "state": result["state"]}


def cancel_work(db, run_id):
    from .experiment_resource_plan import activity_for, finish_resources
    _schema(db)
    for row in db.execute("SELECT * FROM manual_work WHERE run=?", (run_id,)).fetchall():
        handle = activity_for(db, run_id, "manual", row["trial"] + "/1")
        if row["state"] == "completed":
            finish_resources(db, handle, "succeeded")
        elif row["state"] != "cancelled":
            finish_resources(db, handle, "cancelled", started=row["state"] == "claimed")
            db.execute("UPDATE manual_work SET state='cancelled' WHERE run=? AND trial=?", (run_id, row["trial"]))

