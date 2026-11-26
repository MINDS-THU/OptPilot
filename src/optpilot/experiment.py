"""Public versioned Study API shared by the CLI and the desktop bridge.

A project manifest describes editable inputs. Review resolves every selection to
immutable versions in the project store. Admission and execution only need that
store. The native runner evaluates trusted local Python code.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import hashlib
import json
from tempfile import TemporaryDirectory
from statistics import mean

from ._experiment_catalog import MANIFEST, discover, entry_file, safe_path, save, text_value
from ._experiment_draft import checkout, draft_diff, save_derived
from ._experiment_execution import Cancelled, check_cancelled, run_native
from ._experiment_store import (database, derivation_info, get_review, get_run, get_trials, now, put_trial,
                                transaction, version_info, version_row)
from ._experiment_study import review, schedule
from . import experiment_resource_plan as resources
from .realm.refs import BlobRef, request_digest

TERMINAL = {"succeeded", "failed", "cancelled"}


def run_summary(db, row, study=None):
    run_id = row["id"]
    study = study or get_review(db, row["review"])
    used = {v[0] for v in db.execute("""
        SELECT DISTINCT edge.value FROM trials, json_each(trials.spec, '$.usedVersionIds') AS edge
        WHERE trials.run=?
        """, (run_id,))}
    if row["generator_used"]:
        used.add(study["generatorVersionId"])
    completed = db.execute("""
        SELECT COUNT(*) FROM trials WHERE run=? AND json_extract(spec,'$.state') IN ('succeeded','failed','cancelled')
        """, (run_id,)).fetchone()[0]
    result = dict(study, id=run_id, reviewId=study["id"], state=row["state"], createdAt=row["created"],
                  completedTrials=completed, usedVersionIds=sorted(used), executionOrigin="managed")
    if row["finished"]:
        result["finishedAt"] = row["finished"]
    if row["error"]:
        result["error"] = row["error"]
    from .external_results import run_metadata
    if study["runner"] == "harbor":
        from .harbor_runner import metadata
        result.update(metadata(db, run_id))
    result.update(run_metadata(db, run_id))
    return result


def admit(store, review_id, run_id):
    text_value(run_id, "runId")
    with database(store) as db, transaction(db):
        study = get_review(db, review_id)
        created = db.execute(
            "INSERT OR IGNORE INTO runs(id,review,state,created) VALUES (?,?,'queued',?)",
            (run_id, review_id, now()),
        ).rowcount
        row = get_run(db, run_id)
        if row["review"] != review_id:
            raise ValueError("Run ID is already bound to another Study revision.")
        if created:
            for candidate in study["candidateVersionIds"]:
                schedule(db, run_id, study, candidate)
        return run_summary(db, row, study)


@contextmanager
def run_lock(store, run_id, *, blocking=True):
    directory = Path(store) / "locks"
    directory.mkdir(exist_ok=True)
    path = directory / request_digest(run_id)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except PermissionError:
                if blocking:
                    raise
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                if blocking:
                    raise
                yield False
                return
        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def finish(db, run_id, state, error=None):
    if state == "cancelled":
        for trial in get_trials(db, run_id):
            if trial["state"] in ("queued", "running"):
                if trial["attempts"] and trial["attempts"][-1]["state"] == "running":
                    trial["attempts"][-1].update(state="cancelled", finishedAt=now())
                trial["state"] = "cancelled"
                put_trial(db, run_id, trial)
    db.execute("UPDATE runs SET state=?, finished=?, error=? WHERE id=?", (state, now(), error, run_id))


def _wait(db, run_id, reason):
    with transaction(db):
        db.execute("UPDATE runs SET state='waiting', error=?, finished=NULL WHERE id=?", (str(reason), run_id))
        return bool(get_run(db, run_id)["cancelled"])


def _cancel_execution(db, store, run_id, study):
    if study["runner"] == "harbor":
        from .harbor_runner import job_root
        from ._harbor_supervisor import claim
        directory = job_root(store, run_id)
        if directory.exists():
            (directory / "cancel").touch()
            if not (directory / "exit.json").is_file():
                with claim(directory) as supervisor:
                    if supervisor is None:
                        raise resources.ResourceWait("Waiting for the Harbor supervisor to confirm cancellation.")
    with transaction(db):
        if study.get("sshTarget"):
            from .experiment_ssh import cancel_pending
            cancel_pending(db, run_id)
        if study["runner"] == "manual":
            from .experiment_manual import cancel_work
            cancel_work(db, run_id)
        resources.close_open(db, run_id, "cancelled")
        finish(db, run_id, "cancelled")


def _cancel_or_wait(db, store, run_id, study):
    try:
        _cancel_execution(db, store, run_id, study)
    except resources.ResourceWait as exc:
        _wait(db, run_id, exc)


def execute(store, review_id, run_id):
    admit(store, review_id, run_id)
    with run_lock(store, run_id), database(store) as db:
        resources.recover_settlements(db, run_id)
        row = get_run(db, run_id)
        if row["state"] not in TERMINAL:
            study = get_review(db, review_id)
            try:
                with transaction(db):
                    check_cancelled(db, run_id)
                    db.execute("UPDATE runs SET state='running', error=NULL WHERE id=?", (run_id,))
                resources.launch_plan(db, run_id, study)
                if study["runner"] == "harbor":
                    from .harbor_runner import controller
                    handle = resources.begin_harbor(db, run_id, study)
                    controller(db, store, run_id, study)
                elif study["runner"] == "manual":
                    from .experiment_manual import advance
                    if not advance(db, store, run_id, study):
                        raise resources.ResourceWait("Waiting for the operator to claim or complete a work order.")
                else:
                    run_native(db, run_id, study)
                with transaction(db):
                    check_cancelled(db, run_id)
                    failed = [t for t in get_trials(db, run_id) if t["state"] == "failed"]
                    error = f"{len(failed)} Trial(s) failed. {failed[0]['error']}" if failed else None
                    state = "failed" if failed else "succeeded"
                    if study["runner"] == "harbor":
                        resources.finish_resources(db, handle, state)
                    finish(db, run_id, state, error)
            except resources.ResourceWait as exc:
                if _wait(db, run_id, exc):
                    _cancel_or_wait(db, store, run_id, study)
            except Cancelled:
                _cancel_or_wait(db, store, run_id, study)
            except (ValueError, RuntimeError, OSError, ArithmeticError) as exc:
                try:
                    with transaction(db):
                        resources.close_open(db, run_id, "failed")
                        finish(db, run_id, "failed", str(exc))
                except resources.ResourceWait as pending:
                    _wait(db, run_id, pending)
            resources.recover_settlements(db, run_id)
    return detail(store, run_id)


def cancel(store, run_id):
    with database(store) as db, transaction(db):
        row = get_run(db, run_id)
        if row["state"] not in TERMINAL:
            db.execute("UPDATE runs SET cancelled=1 WHERE id=?", (run_id,))
            if row["state"] == "running" and get_review(db, row["review"])["runner"] == "harbor":
                from .harbor_runner import job_root
                directory = job_root(store, run_id)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "cancel").touch()
        result = run_summary(db, get_run(db, run_id))
    if result["state"] not in TERMINAL:
        # If the controller is alive, it will consume the cancellation flag.
        # If it died, reconcile durable receipts under the same execution lock.
        with run_lock(store, run_id, blocking=result["state"] != "running") as claimed:
            if claimed:
                with database(store) as db:
                    row = get_run(db, run_id)
                    if row["state"] not in TERMINAL:
                        _cancel_or_wait(db, store, run_id, get_review(db, row["review"]))
                    resources.recover_settlements(db, run_id)
                    result = run_summary(db, get_run(db, run_id))
    return result


def aggregate(trials):
    groups = {}
    for trial in trials:
        key = trial["candidateVersionId"], trial["taskVersionId"]
        groups.setdefault(key, []).append(trial)
    aggregates = []
    for (candidate, task), group in groups.items():
        valid = [t for t in group if t["state"] == "succeeded"]
        samples = {}
        for trial in valid:
            for key, value in trial["metrics"].items():
                samples.setdefault(key, []).append(value)
        aggregates.append({
            "candidateVersionId": candidate, "taskVersionId": task, "totalTrials": len(group),
            "validTrials": len(valid), "metrics": {
                key: {"mean": mean(values), "min": min(values), "max": max(values), "count": len(values)}
                for key, values in samples.items()
            },
        })
    return aggregates


def detail(store, run_id):
    with database(store) as db:
        db.execute("BEGIN")
        result = run_summary(db, get_run(db, run_id))
        trials = get_trials(db, run_id)
        for trial in trials:
            trial.pop("coordinateVersionIds", None)
        from .external_results import detail_metadata
        result.update(trials=trials, aggregates=aggregate(trials))
        result["resourceUsage"] = resources.run_resource_detail(db, run_id)
        result["generations"] = [json.loads(row[0]) for row in db.execute(
            "SELECT spec FROM generations WHERE run=? ORDER BY rowid", (run_id,))]
        if result["runner"] == "harbor":
            from .harbor_runner import metadata
            result.update(metadata(db, run_id, include_summary=True))
        result.update(detail_metadata(db, run_id))
        return result


def graph(store):
    from ._harbor_results import graph_usage
    from .experiment_resources import graph_resources
    from .external_results import graph_trials
    with database(store) as db:
        db.execute("BEGIN")
        usage = graph_resources(db)
        runs = []
        for row in db.execute("SELECT * FROM runs ORDER BY created DESC,rowid DESC"):
            result = run_summary(db, row)
            # Keep the canvas summary small: no evidence, tool logs or private inputs.
            trials = [dict(candidateVersionId=t[0], taskVersionId=t[1], state=t[2],
                           metrics=json.loads(t[3] or "{}"), instanceVersionId=t[4]) for t in db.execute("""
                SELECT candidate, json_extract(spec,'$.taskVersionId'),
                       json_extract(spec,'$.state'), json_extract(spec,'$.metrics'), instance
                FROM trials WHERE run=? ORDER BY rowid
            """, (row["id"],))]
            if result["executionOrigin"] == "import":
                trials = graph_trials(db, row["id"])
            result["summary"] = {
                "resources": usage.get(row["id"], {"items": []}),
                "aggregates": aggregate(trials),
                "states": {state: sum(t["state"] == state for t in trials)
                           for state in ("queued", "running", "succeeded", "failed", "cancelled")},
                "activeTrials": [{key: t[key] for key in ("candidateVersionId", "instanceVersionId")}
                                 for t in trials if t["state"] == "running"][:3],
                "generating": result["state"] == "running" and db.execute(
                    """SELECT 1 FROM generations WHERE run=? AND json_extract(spec,'$.state')='running' LIMIT 1""",
                    (row["id"],)).fetchone() is not None,
            }
            harbor_usage = graph_usage(db, result)
            if harbor_usage:
                result["summary"]["resources"]["harbor"] = harbor_usage
            if result["executionOrigin"] == "import":
                result["summary"]["observedScope"] = {
                    "instances": len({t["instanceVersionId"] for t in trials}),
                    "candidates": len({t["candidateVersionId"] for t in trials}),
                }
            runs.append(result)
        versions = [version_info(row) for row in db.execute("SELECT * FROM versions ORDER BY rowid")]
        visual_envs = {item["id"] for item in versions if item.get("visual")}
        for item in versions:
            if item["kind"] == "task" and visual_envs.intersection(item["dependencies"]):
                item["visual"] = True
        return {
            "versions": versions,
            "runs": runs,
            "derivations": [derivation_info(row) for row in db.execute("SELECT * FROM version_derivations ORDER BY rowid")],
        }


def version(store, version_id):
    """Describe a retained artifact without exposing grader-only Instance values."""
    with database(store) as db:
        row = version_row(db, version_id)
        spec = json.loads(row["spec"])
        files = []
        for path, ref in sorted(spec.pop("fileRefs").items()):
            data = db.execute("SELECT bytes FROM blobs WHERE id=?", (ref,)).fetchone()[0]
            files.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        spec.pop("private", None)
        digests = [item[0] for item in db.execute(
            "SELECT digest FROM version_manifests WHERE version=? ORDER BY digest", (version_id,))]
        detail = {"version": version_info(row), "definition": spec, "files": files, "manifestDigests": digests}
        derivation = db.execute("SELECT * FROM version_derivations WHERE version=?", (version_id,)).fetchone()
        if derivation is not None:
            detail["derivation"] = derivation_info(derivation)
        return detail


def version_file(store, version_id, path):
    """Read a bounded preview from this version's blobs, never from working files."""
    with database(store) as db:
        spec = json.loads(version_row(db, version_id)["spec"])
        ref = spec["fileRefs"].get(path)
        if ref is None:
            raise ValueError(f"File {path!r} is not saved in this version.")
        data = db.execute("SELECT bytes FROM blobs WHERE id=?", (ref,)).fetchone()[0]
        result = {"versionId": version_id, "path": path, "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
        if len(data) > 512 * 1024:
            return {**result, "unavailable": "too-large"}
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return {**result, "unavailable": "binary"}
        if "\0" in content:
            return {**result, "unavailable": "binary"}
        return {**result, "content": content}


def export_version_files(store, version_id, destination):
    """Stage saved blobs for the desktop's read-only virtual filesystem."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    with database(store) as db:
        row = version_row(db, version_id)
        spec = json.loads(row["spec"])
        files = []
        for path, ref in sorted(spec["fileRefs"].items()):
            safe_path(path)
            data = db.execute("SELECT bytes FROM blobs WHERE id=?", (ref,)).fetchone()[0]
            if str(BlobRef.from_bytes(data)) != ref:
                raise ValueError(f"Saved file failed its integrity check at {path!r}.")
            digest = hashlib.sha256(data).hexdigest()
            blob = destination / digest
            if not blob.exists():
                blob.write_bytes(data)
                blob.chmod(0o400)
            files.append({"path": path, "blob": digest, "size": len(data)})
        preferred = next((entry_file(spec[field]) for field in ("entrypoint", "evaluator") if spec.get(field)), None)
        if spec.get("runner") == "harbor":
            preferred = spec["harborTask"] + "/instruction.md"
        if preferred not in spec["fileRefs"]:
            preferred = next((file["path"] for file in files
                              if file["path"].rsplit("/", 1)[-1].lower() in {"readme.md", "instruction.md"}), None)
        (destination / "index.json").write_text(json.dumps({"files": files, "preferred": preferred}), encoding="utf-8")
    return {"name": version_info(row)["name"], "fileCount": len(files)}


def validate_manifest(path):
    """Resolve and validate the entire project in a disposable store."""
    path = Path(path)
    if path.name != MANIFEST or not path.is_file():
        raise ValueError(f"Expected an existing {MANIFEST} file.")
    with TemporaryDirectory(prefix="optpilot-validation-") as store:
        found = discover(path.parent, store)
        study_ids = [study["id"] for study in found["studies"]]
        if len(study_ids) != len(set(study_ids)):
            raise ValueError("Study ids must be unique.")
        for study_id in study_ids:
            review(path.parent, store, study_id)


__all__ = ["admit", "cancel", "checkout", "detail", "discover", "draft_diff", "execute", "graph", "review",
           "save", "save_derived", "validate_manifest", "version", "version_file"]


def refresh_results(store, run_id):
    """Re-read actual Harbor receipts after a runner/importer update; never launch work."""
    from .harbor_runner import job_root, read_completed
    with run_lock(store, run_id), database(store) as db:
        row = get_run(db, run_id)
        study = get_review(db, row["review"])
        if study["runner"] != "harbor" or row["state"] not in TERMINAL:
            raise ValueError("Refresh requires a completed managed Harbor evaluation.")
        directory = job_root(store, run_id)
        if not (directory / "jobs/evaluation/result.json").is_file():
            raise ValueError("The original Harbor job receipts are not available.")
        read_completed(db, run_id, directory, study)
        if row["state"] != "cancelled":
            failed = [t for t in get_trials(db, run_id) if t["state"] == "failed"]
            with transaction(db):
                db.execute("UPDATE runs SET state=?,error=? WHERE id=?",
                           ("failed" if failed else "succeeded",
                            f"{len(failed)} Trial(s) failed. {failed[0]['error']}" if failed else None, run_id))
    return detail(store, run_id)
