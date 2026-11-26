"""Transactional storage for frozen experiment definitions, inputs and results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .realm.refs import BlobRef, canonical_json_bytes, request_digest


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def database(store):
    directory = Path(store)
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "experiments.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS blobs (
            id TEXT PRIMARY KEY, bytes BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS versions (
            id TEXT PRIMARY KEY, artifact TEXT NOT NULL, revision INTEGER NOT NULL,
            spec TEXT NOT NULL, source_card TEXT, UNIQUE(artifact,revision)
        );
        CREATE TABLE IF NOT EXISTS version_manifests (
            version TEXT NOT NULL REFERENCES versions(id), digest TEXT NOT NULL, PRIMARY KEY(version,digest)
        );
        CREATE TABLE IF NOT EXISTS version_derivations (
            version TEXT PRIMARY KEY REFERENCES versions(id), base TEXT NOT NULL REFERENCES versions(id),
            summary TEXT NOT NULL, created TEXT NOT NULL, CHECK(version <> base)
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY, spec TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, review TEXT NOT NULL REFERENCES reviews(id),
            state TEXT NOT NULL, created TEXT NOT NULL, finished TEXT, error TEXT,
            cancelled INTEGER NOT NULL DEFAULT 0, generator_used INTEGER NOT NULL DEFAULT 0,
            generator_done INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS trials (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), candidate TEXT NOT NULL,
            instance TEXT NOT NULL, repetition INTEGER NOT NULL, spec TEXT NOT NULL,
            UNIQUE(run,candidate,instance,repetition)
        );
        CREATE INDEX IF NOT EXISTS trials_by_run ON trials(run);
        CREATE TABLE IF NOT EXISTS resource_run_plans (
            run TEXT PRIMARY KEY REFERENCES runs(id), spec TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource_project (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource_activities (
            run TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL, activity TEXT NOT NULL,
            owner TEXT NOT NULL UNIQUE, spec TEXT NOT NULL, settlement TEXT, settled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run,stage,activity)
        );
        CREATE TABLE IF NOT EXISTS harbor_jobs (
            run TEXT PRIMARY KEY REFERENCES runs(id), job_id TEXT UNIQUE NOT NULL,
            directory TEXT NOT NULL, report TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposals (
            run TEXT NOT NULL REFERENCES runs(id), ordinal INTEGER NOT NULL,
            candidate TEXT NOT NULL REFERENCES versions(id), PRIMARY KEY(run,ordinal),
            UNIQUE(run,candidate)
        );
        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id), spec TEXT NOT NULL
        );
        """)
        yield db
    finally:
        db.close()


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def encode(value):
    return canonical_json_bytes(value).decode("utf-8")


def persist_version(db, spec, content=None):
    for ref, data in (content or {}).items():
        db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (ref, data))
    version = "v-" + request_digest(spec)
    db.execute(
        """INSERT OR IGNORE INTO versions(id,artifact,revision,spec)
           SELECT ?,?,COALESCE(MAX(revision),0)+1,? FROM versions WHERE artifact=?""",
        (version, spec["id"], encode(spec), spec["id"]),
    )
    return version


def version_row(db, version):
    row = db.execute("SELECT * FROM versions WHERE id=?", (version,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown saved version {version!r}.")
    return row


def version_spec(db, version):
    return json.loads(version_row(db, version)["spec"])


def version_info(row):
    spec = json.loads(row["spec"])
    version = {
        "id": row["id"], "artifactId": row["artifact"], "kind": spec["kind"],
        "name": spec["name"], "revision": row["revision"],
        "dependencies": spec["dependencies"],
        "fileCount": len(spec.get("fileRefs", {})),
    }

    for field in ("description", "format", "generatedByRunId", "generatorVersionId", "generationId"):
        if field in spec:
            version[field] = spec[field]
    if spec.get("resourceRequirements"):
        version["resourceRequirements"] = [
            {k: requirement[k] for k in ("capability", "unit", "resourceId") if k in requirement}
            for requirement in spec["resourceRequirements"]
        ]
    if spec["kind"] == "task":
        version.update(format=spec["decisionContract"]["format"], metrics=metric_names(spec),
                       metricDirections=metric_directions(spec))
    if spec["kind"] == "environment" and spec.get("interface"):
        version["visual"] = True
    return version


def derivation_info(row):
    return {"baseVersionId": row["base"], "versionId": row["version"],
            "summary": row["summary"], "createdAt": row["created"]}


def metric_names(task):
    return [item if isinstance(item, str) else item["name"] for item in task["metrics"]]


def metric_directions(task):
    return {m if isinstance(m, str) else m["name"]:
            "maximize" if isinstance(m, str) else m.get("direction", "maximize")
            for m in task["metrics"]}


def get_review(db, review_id):
    row = db.execute("SELECT spec FROM reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown reviewed study {review_id!r}.")
    return json.loads(row[0])


def get_run(db, run_id):
    row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown experiment run {run_id!r}.")
    return row


def get_trials(db, run_id):
    return [json.loads(row[0]) for row in db.execute(
        "SELECT spec FROM trials WHERE run=? ORDER BY rowid", (run_id,)
    )]


def put_trial(db, run_id, trial):
    db.execute(
        """INSERT INTO trials(id,run,candidate,instance,repetition,spec) VALUES (?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET spec=excluded.spec""",
        (trial["id"], run_id, trial["candidateVersionId"], trial["instanceVersionId"],
         trial["repetition"], encode(trial)),
    )


def materialize(db, directory, specs):
    files = {}
    executable = set()
    for spec in specs:
        executable.update(spec.get("executableFiles", []))
        for path, ref in spec["fileRefs"].items():
            if path in files and files[path] != ref:
                raise ValueError(f"Frozen dependencies contain conflicting files at {path!r}.")
            files[path] = ref
    for path, ref in files.items():
        payload = db.execute("SELECT bytes FROM blobs WHERE id=?", (ref,)).fetchone()[0]
        if str(BlobRef.from_bytes(payload)) != ref:
            raise ValueError(f"Frozen source failed its integrity check at {path!r}.")
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if path in executable:
            target.chmod(0o755)

