"""Import an external runner's terminal report against an immutable Study review."""

import json
from datetime import datetime

from ._experiment_store import database, encode, get_review, get_trials, metric_names, now, transaction, version_spec
from ._harbor_results import metrics, read_report, text
from .realm.refs import request_digest


TERMINAL = {"succeeded", "failed", "cancelled"}


def _table(db):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='external_reports'").fetchone()


def run_metadata(db, run_id):
    if not _table(db):
        return {}
    row = db.execute("SELECT metadata FROM external_reports WHERE run=?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def graph_trials(db, run_id):
    return [dict(candidateVersionId=t[0], taskVersionId=t[1], state=t[2], metrics=json.loads(t[3] or "{}"),
                 instanceVersionId=t[4]) for t in db.execute("""
        SELECT json_extract(spec,'$.candidateVersionId'), json_extract(spec,'$.taskVersionId'),
               json_extract(spec,'$.state'), json_extract(spec,'$.metrics'), json_extract(spec,'$.instanceVersionId')
        FROM external_trials WHERE run=? ORDER BY external_id
    """, (run_id,))]


def detail_metadata(db, run_id):
    if not _table(db):
        return {}
    row = db.execute("SELECT summary FROM external_reports WHERE run=?", (run_id,)).fetchone()
    if not row:
        return {}
    from .experiment import aggregate

    trials = [json.loads(item[0]) for item in db.execute("SELECT spec FROM external_trials WHERE run=? ORDER BY external_id", (run_id,))]
    aggregates = aggregate(trials)
    for group in aggregates:
        group["validTrials"] = sum(t["state"] == "succeeded" and bool(t["metrics"]) for t in trials
                                   if (t["candidateVersionId"], t["taskVersionId"]) == (group["candidateVersionId"], group["taskVersionId"]))
    return {"trials": trials, "aggregates": aggregates, "externalSummary": json.loads(row[0])}


def _time(value):
    if value is not None:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("Result times must use ISO date/time strings.") from error
    return value


def _count(value, field, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer of at least {minimum}.")
    return value


def _state(row):
    state = row.get("state")
    if state not in TERMINAL:
        raise ValueError("External result records must include a terminal state.")
    return state


def _normalize(report):
    if not isinstance(report, dict) or report.get("format") != "optpilot.results.v1":
        raise ValueError("Expected an optpilot.results.v1 report.")
    source, job = text(report.get("source"), "external source"), text(report.get("jobId"), "external job ID")
    summary = report.get("summary", {})
    records = report.get("trials", [])
    if not isinstance(summary, dict) or not isinstance(records, list):
        raise ValueError("Expected a summary object and an array of trials.")
    rows = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("Each external trial must be an object.")
        tid = text(raw.get("id"), "external trial ID")
        row = {"id": tid, "instance": text(raw.get("instance"), "external instance name"),
               "candidate": text(raw.get("candidate"), "external candidate name"), "state": _state(raw),
               "metrics": metrics(raw.get("metrics", {})), "attempts": []}
        if "repetition" in raw:
            row["repetition"] = _count(raw["repetition"], "repetition", 1)
        if "seed" in raw:
            row["seed"] = _count(raw["seed"], "seed")
        for field in ("evidence", "error"):
            if field in raw:
                row[field] = raw[field] if field == "evidence" else text(raw[field], "error message")
        for field in ("startedAt", "finishedAt"):
            if raw.get(field):
                row[field] = _time(raw[field])
        seen = set()
        attempts = raw.get("attempts", [])
        if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
            raise ValueError("Trial attempts must be a list of records.")
        for number, attempt in enumerate(attempts, start=1):
            aid = text(attempt.get("id"), "external attempt ID")
            if aid in seen:
                raise ValueError("External attempt IDs must be unique in each Trial.")
            seen.add(aid)
            observed = {"externalAttemptId": aid, "number": number, "state": _state(attempt)}
            for field in ("startedAt", "finishedAt"):
                if attempt.get(field):
                    observed[field] = _time(attempt[field])
            if "metrics" in attempt:
                observed["metrics"] = metrics(attempt["metrics"])
            for field in ("error", "evidence"):
                if field in attempt:
                    observed[field] = attempt[field]
            row["attempts"].append(observed)
        if tid in rows and rows[tid] != row:
            raise ValueError(f"External trial {tid!r} has conflicting records.")
        rows[tid] = row
    if not rows and "reportedTrials" not in report:
        raise ValueError("Include the reported trial count with a summary-only export.")
    total = _count(report.get("reportedTrials", len(rows)), "reportedTrials")
    if len(rows) > total:
        raise ValueError("More distinct trial records than the reported coverage.")
    result = {"source": source, "jobId": job, "reportedTrials": total, "state": _state(report),
              "startedAt": _time(report.get("startedAt")), "finishedAt": _time(report.get("finishedAt")),
              "summary": summary, "trials": [rows[k] for k in sorted(rows)]}
    encode(result)  # Reject non-JSON evidence and nonfinite numbers before retention.
    return result


def _preview(report):
    trials = report["trials"]
    return {**{k: report[k] for k in ("source", "jobId", "state", "reportedTrials")},
            "digest": request_digest(report), "recordCount": len(trials),
            "candidates": sorted({t["candidate"] for t in trials}), "instances": sorted({t["instance"] for t in trials})}


def preview(path):
    return _preview(_normalize(read_report(path)))


def _ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS external_reports(
        run TEXT PRIMARY KEY REFERENCES runs(id), metadata TEXT NOT NULL, summary TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS external_bindings(
        run TEXT NOT NULL REFERENCES external_reports(run), kind TEXT NOT NULL, name TEXT NOT NULL,
        version TEXT NOT NULL REFERENCES versions(id), PRIMARY KEY(run,kind,name))""")
    db.execute("""CREATE TABLE IF NOT EXISTS external_trials(
        run TEXT NOT NULL REFERENCES external_reports(run), external_id TEXT NOT NULL, spec TEXT NOT NULL,
        PRIMARY KEY(run,external_id))""")


def _bind(study, bindings, keys, key, selection):
    mapped = bindings.get(key, {})
    if not isinstance(mapped, dict) or set(mapped) != set(keys):
        raise ValueError(f"Match each external {key} entry to its exact reviewed version.")
    if any(v not in study[selection] for v in mapped.values()):
        raise ValueError(f"External {key} bindings must use versions from this review.")
    return mapped


def _managed_run(db, review_id, report, candidates, instances, suites):
    if report["source"] != "Harbor" or not db.execute("SELECT 1 FROM sqlite_schema WHERE name='harbor_jobs'").fetchone():
        return None
    known = db.execute("""SELECT runs.id, runs.review FROM harbor_jobs JOIN runs ON runs.id=harbor_jobs.run
                          WHERE harbor_jobs.job_id=?""", (report["jobId"],)).fetchone()
    if not known:
        return None
    if known["review"] != review_id:
        raise ValueError(f"This Harbor job already belongs to run {known['id']}, under a different Study revision.")
    trials = {t.get("externalTrialId"): t for t in get_trials(db, known["id"])}
    for row in report["trials"]:
        trial = trials.get(row["id"])
        if (not trial or candidates[row["candidate"]] != trial["candidateVersionId"]
                or instances[row["instance"]] != trial["instanceVersionId"]
                or trial['metrics'] != row['metrics']
                or any(s not in trial['usedVersionIds'] for s in suites if trial['instanceVersionId'] in version_spec(db,s)['dependencies'])):
            raise ValueError(f"The selected report or bindings disagree with the recorded run {known['id']}.")
    return {"runId": known["id"]}


def import_report(store, review_id, report, bindings, expected_digest=None):
    """Keep one run per source/job ID. Additional files may fill gaps in a terminal export."""
    report = _normalize(report)
    info = _preview(report)
    if expected_digest and expected_digest != info["digest"]:
        raise ValueError("The exported results changed. Choose and review the export again.")
    run_id = "external-" + request_digest([report["source"], report["jobId"]])
    if not isinstance(bindings, dict):
        raise ValueError("External version bindings must be an object.")
    with database(store) as db, transaction(db):
        _ensure_schema(db)
        study = get_review(db, review_id)
        candidates = _bind(study, bindings, info["candidates"], "candidates", "candidateVersionIds")
        instances = _bind(study, bindings, info["instances"], "instances", "instanceVersionIds")
        raw_suites = bindings.get("suiteVersionIds", [])
        if not isinstance(raw_suites, list) or any(not isinstance(value, str) or not value for value in raw_suites):
            raise ValueError("suiteVersionIds must be an array of saved suite version IDs.")
        suites = list(dict.fromkeys(raw_suites))
        if any(v not in study["suiteVersionIds"] for v in suites):
            raise ValueError("Choose only the suite versions used in this review.")
        managed = _managed_run(db, review_id, report, candidates, instances, suites)
        if managed:
            return managed
        prev = db.execute("SELECT review,state FROM runs WHERE id=?", (run_id,)).fetchone()
        if prev and (prev["review"] != review_id or prev["state"] != report["state"]):
            raise ValueError("This external job is already associated with another review or outcome.")
        if not prev:
            db.execute("INSERT INTO runs(id,review,state,created,finished) VALUES(?,?,?,?,?)",
                       (run_id, review_id, report["state"], report["startedAt"] or now(), report["finishedAt"] or now()))
            db.execute("INSERT INTO external_reports VALUES(?,?,?)", (run_id, "{}", encode(report["summary"])))
        else:
            previous = db.execute("SELECT summary,metadata FROM external_reports WHERE run=?", (run_id,)).fetchone()
            if previous["summary"] != encode(report["summary"]) or json.loads(previous["metadata"])["external"]["reportedTrials"] != report["reportedTrials"]:
                raise ValueError("This external job already has different reported totals or a different summary.")
        for kind, names in (("candidate", candidates), ("instance", instances)):
            for name, version in names.items():
                prior = db.execute("SELECT version FROM external_bindings WHERE run=? AND kind=? AND name=?", (run_id, kind, name)).fetchone()
                if prior and prior[0] != version:
                    raise ValueError(f"{name!r} already identifies a different version in this job.")
                db.execute("INSERT OR IGNORE INTO external_bindings VALUES (?,?,?,?)", (run_id, kind, name, version))
        for row in report["trials"]:
            candidate, instance = candidates[row["candidate"]], instances[row["instance"]]
            task = version_spec(db, instance)["dependencies"][0]
            task_spec = version_spec(db, task)
            extra = set(row["metrics"]) - set(metric_names(task_spec))
            if extra:
                raise ValueError(f"The mapped task does not declare these reported metrics: {sorted(extra)}.")
            included = [v for v in suites if instance in version_spec(db, v)["dependencies"]]
            used = [candidate, instance, task, *task_spec["dependencies"], *included]
            trial = {**row, "id": "trial-" + request_digest([run_id, row["id"]]), "externalTrialId": row["id"],
                     "candidateVersionId": candidate, "instanceVersionId": instance, "taskVersionId": task,
                     "usedVersionIds": list(dict.fromkeys(used))}
            payload = encode(trial)
            prior = db.execute("SELECT spec FROM external_trials WHERE run=? AND external_id=?", (run_id, row["id"])).fetchone()
            if prior and prior[0] != payload:
                raise ValueError(f"External trial {row['id']!r} changed its bindings or recorded evidence.")
            db.execute("INSERT OR IGNORE INTO external_trials VALUES(?,?,?)", (run_id, row["id"], payload))
        rows = [json.loads(item[0]) for item in db.execute("SELECT spec FROM external_trials WHERE run=?", (run_id,))]
        if len(rows) > report["reportedTrials"]:
            raise ValueError("Distinct imported trial records exceed the job’s reported total.")
        coverage = "summary" if not rows else "trials" if len(rows) == report["reportedTrials"] else "partial"
        external = {"source": info["source"], "jobId": info["jobId"], "reportedTrials": info["reportedTrials"],
                    "importedTrials": len(rows), "coverage": coverage}
        metadata = {"executionOrigin": "import", "external": external, "completedTrials": len(rows),
                    "usedVersionIds": sorted({v for row in rows for v in row["usedVersionIds"]})}
        db.execute("UPDATE external_reports SET metadata=? WHERE run=?", (encode(metadata), run_id))
    return {"runId": run_id, "external": external}


def import_file(store, review_id, path, bindings, expected_digest=None):
    return import_report(store, review_id, read_report(path), bindings, expected_digest)
