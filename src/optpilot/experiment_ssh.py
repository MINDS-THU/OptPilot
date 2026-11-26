"""Dispatch native trial workers over an explicitly selected, registered SSH connection."""
from contextlib import closing
import io
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tarfile
import time
import uuid

from ._experiment_execution import Cancelled
from ._experiment_store import get_run, transaction
from .realm.refs import request_digest

DRIVER = Path(__file__).with_name("_ssh_trial_worker.py")
WORKER = Path(__file__).with_name("_experiment_worker.py")


def _db():
    from .experiment_resources import registry_directory
    folder = registry_directory()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "ssh-targets.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS targets (id TEXT PRIMARY KEY, name TEXT, host TEXT, python TEXT)")
    return db


def targets():
    with closing(_db()) as db:
        return {"targets": [dict(row) for row in db.execute("SELECT * FROM targets ORDER BY name,id")]}


def register(target):
    if not isinstance(target, dict) or set(target) - {"id", "name", "host", "python"}:
        raise ValueError("Supply an SSH target id, name, host alias and optional Python executable.")
    identifier, name, host, python = (target.get("id"), target.get("name"), target.get("host"), target.get("python", "python3"))
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}", identifier):
        raise ValueError("Invalid SSH target ID.")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9@_.-]{0,199}", host):
        raise ValueError("Supply an alias from your SSH configuration.")
    if not isinstance(name, str) or not name.strip() or len(name) > 150:
        raise ValueError("Enter a name for the target.")
    if not isinstance(python, str) or not re.fullmatch(r"(?:/[a-zA-Z0-9_./+-]*/)?[a-zA-Z0-9_][a-zA-Z0-9_.+-]*", python):
        raise ValueError("Supply an executable name or absolute POSIX path for Python.")
    entry = dict(id=identifier, name=name, host=host, python=python)
    with closing(_db()) as db, db:
        db.execute("INSERT OR IGNORE INTO targets VALUES (:id,:name,:host,:python)", entry)
        row = db.execute("SELECT * FROM targets WHERE id=?", (identifier,)).fetchone()
        if row["host"] != host or row["python"] != python:
            raise ValueError("Use a new target ID for a different host or Python executable; existing Runs retain their target.")
        db.execute("UPDATE targets SET name=? WHERE id=?", (name, identifier))
    return {"target": entry}


def _target(identifier):
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM targets WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise ValueError(f"Register the SSH target {identifier!r} and verify its connection.")
    return dict(row)


def _invoke(target, action, identity="", data=None):
    command = shlex.join([target["python"], "-I", "-B", "-c", DRIVER.read_text(), action, identity])
    reply = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=7",
                            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-T", "--", target["host"], command],
                           input=data or b"", capture_output=True, timeout=180 if data else 20)
    if reply.returncode:
        raise OSError(reply.stderr.decode("utf-8", "replace")[-2500:] or f"SSH exited with status {reply.returncode}")
    try:
        value = json.loads(reply.stdout)
        if not isinstance(value.get("state"), str):
            raise ValueError("Missing state")
        return value
    except (ValueError, AttributeError) as exc:
        raise OSError("The SSH target did not return an OptPilot worker receipt.") from exc


def target_status(identifier):
    try:
        target = _target(identifier)
        _invoke(target, "probe")
        return {"ready": True, "status": "ready", "location": target["host"], "reason": "Remote Python responded over SSH."}
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return {"ready": False, "status": "unavailable", "location": identifier, "reason": str(exc)}


def _bundle(payload, grader, candidate_directory, timeout):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        archive.add(DRIVER, "driver.py")
        archive.add(WORKER, "worker.py")
        archive.add(grader, "grader")
        if candidate_directory is not None:
            archive.add(candidate_directory, "candidate")
        contents = json.dumps({**payload, "_timeout": timeout}, allow_nan=False).encode()
        info = tarfile.TarInfo("payload.json")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))
    if buf.tell() > 256 * 1024 * 1024:
        raise ValueError("Frozen worker files exceed the SSH upload limit (256 MiB compressed).")
    return buf.getvalue()


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ssh_attempts (run TEXT NOT NULL REFERENCES runs(id), trial TEXT NOT NULL,
               attempt INTEGER NOT NULL, target TEXT NOT NULL, job TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
               PRIMARY KEY(run,trial,attempt))""")


def _receipt(db, run_id, trial_id, attempt, target):
    _schema(db)
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO ssh_attempts VALUES (?,?,?,?,?,'new')",
                   (run_id, trial_id, attempt, target, request_digest(uuid.uuid4().hex)))
        return dict(db.execute("SELECT * FROM ssh_attempts WHERE run=? AND trial=? AND attempt=?",
                               (run_id, trial_id, attempt)).fetchone())


def _remember(db, job, state):
    with transaction(db):
        db.execute("UPDATE ssh_attempts SET state=? WHERE job=?", (state, job))


def _remote(target, action, job, data=None):
    from .experiment_resource_plan import ResourceWait
    try:
        return _invoke(target, action, job, data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResourceWait(f"SSH {target['host']}, job {job}: reconnect and resume this same Run. {exc}") from exc


def evaluate(db, run_id, study, trial, attempt_number, on_launch, payload, grader, candidate_directory=None):
    from .experiment_resource_plan import ResourceWait
    try:
        target = _target(study["sshTarget"])
    except ValueError as exc:
        raise ResourceWait(str(exc)) from exc
    rec = _receipt(db, run_id, trial["id"], attempt_number, target["id"])
    job = rec["job"]
    info = {"targetId": target["id"], "host": target["host"], "jobId": job}
    trial["attempts"][-1]["remote"] = info
    reply = _remote(target, "status", job)
    if reply["state"] in ("missing", "queued"):
        if reply["state"] == "missing" and rec["state"] not in ("new", "submitting"):
            raise ResourceWait(f"Previously accepted SSH job {job} has no receipt on {target['host']}; reconcile its status before proceeding.")
        _remember(db, job, "submitting")
        reply = _remote(target, "submit", job, _bundle(payload, grader, candidate_directory, study["timeoutSeconds"]))
    on_launch()
    while True:
        state = reply["state"]
        _remember(db, job, state)
        if "elapsedSeconds" in reply:
            info["elapsedSeconds"] = reply["elapsedSeconds"]
        if state == "succeeded":
            return reply["value"], reply.get("logs", "")
        if state == "failed":
            raise RuntimeError(reply.get("error", "Remote trial failed."))
        if state == "cancelled":
            raise Cancelled("Remote trial cancelled.")
        if state not in ("running", "queued"):
            raise ResourceWait(f"Verify the outcome of SSH job {job} on {target['host']} before continuing.")
        time.sleep(.5)
        reply = _remote(target, "cancel" if get_run(db, run_id)["cancelled"] else "status", job)


def cancel_pending(db, run_id):
    from .experiment_resource_plan import ResourceWait
    _schema(db)
    rows = db.execute("SELECT * FROM ssh_attempts WHERE run=? AND state NOT IN ('succeeded','failed','cancelled')", (run_id,)).fetchall()
    for row in rows:
        target, job = _target(row["target"]), row["job"]
        for _ in range(4):
            reply = _remote(target, "cancel", job)
            state = reply["state"]
            if state in ("succeeded","failed","cancelled") or (state == "missing" and row["state"] == "new"):
                db.execute("UPDATE ssh_attempts SET state=? WHERE job=?", ("cancelled" if state == "missing" else state, job))
                break
            time.sleep(.25)
        else:
            raise ResourceWait(f"Waiting for cancellation confirmation of SSH job {job}.")

