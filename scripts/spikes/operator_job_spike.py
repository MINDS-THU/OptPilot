"""Disposable Operator Job reconciliation and handoff architecture spike.

This module deliberately lives outside :mod:`optpilot` and OptPilot Studio.  It
proves the durable-token and transaction boundaries needed by the reviewed
design; it is not a production job launcher.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional


JsonDict = Dict[str, Any]
FaultHook = Callable[[str], None]


class OperatorJobSpikeError(RuntimeError):
    """Base error for the disposable spike."""


class OperatorJobConflict(OperatorJobSpikeError):
    """A token, digest, state, identity, or fencing check failed."""


class OperatorJobNotFound(OperatorJobSpikeError):
    """A requested disposable entity does not exist."""


def digest_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_json(payload: Optional[str], default: Any) -> Any:
    if not payload:
        return default
    return json.loads(payload)


REALM_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'planned', 'awaiting_approval', 'queued', 'starting', 'running',
        'stopping', 'succeeded', 'failed', 'cancelled'
    )),
    phase TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    startup_token TEXT NOT NULL UNIQUE,
    admission_token TEXT NOT NULL UNIQUE,
    backend_token TEXT NOT NULL UNIQUE,
    run_id TEXT UNIQUE,
    approved_digest TEXT,
    stop_reason TEXT,
    terminal_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admission_claims (
    admission_token TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES operator_jobs(job_id),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    authority_lease_id TEXT UNIQUE,
    authority_fencing_token INTEGER,
    state TEXT NOT NULL CHECK(state IN ('requested', 'held', 'transferred', 'released')),
    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('operator_job', 'run')),
    owner_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS backend_bindings (
    backend_token TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES operator_jobs(job_id),
    launch_digest TEXT NOT NULL,
    launch_json TEXT NOT NULL,
    authority_execution_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('intended', 'live', 'stopped', 'exited', 'unknown')),
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run_startups (
    run_id TEXT PRIMARY KEY,
    source_job_id TEXT NOT NULL UNIQUE REFERENCES operator_jobs(job_id),
    startup_token TEXT NOT NULL UNIQUE,
    controller_id TEXT,
    fencing_token INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('awaiting_heartbeat', 'active', 'stopping', 'terminal')),
    heartbeat_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS study_handoffs (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES run_startups(run_id),
    admission_token TEXT NOT NULL UNIQUE REFERENCES admission_claims(admission_token),
    operation_id TEXT NOT NULL UNIQUE,
    committed_at REAL NOT NULL
);
"""


EXTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS fake_admission_allocations (
    admission_token TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    lease_id TEXT UNIQUE,
    fencing_token INTEGER,
    state TEXT NOT NULL CHECK(state IN ('held', 'released')),
    created_count INTEGER NOT NULL CHECK(created_count IN (0, 1)),
    ensure_calls INTEGER NOT NULL DEFAULT 0,
    release_calls INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fake_backend_executions (
    backend_token TEXT PRIMARY KEY,
    launch_digest TEXT NOT NULL,
    launch_json TEXT NOT NULL,
    admission_lease_id TEXT,
    execution_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('live', 'stopped', 'exited', 'unknown')),
    created_count INTEGER NOT NULL CHECK(created_count IN (0, 1)),
    ensure_calls INTEGER NOT NULL DEFAULT 0,
    stop_calls INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class _SqliteBase:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=20.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class OperatorJobLedgerSpike(_SqliteBase):
    """Realm-local durable job, admission-ownership, and handoff records."""

    def __init__(self, database_path: Path, *, fault_hook: Optional[FaultHook] = None):
        super().__init__(database_path)
        self.fault_hook = fault_hook
        with self._connect() as connection:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise OperatorJobSpikeError(f"SQLite did not enable WAL mode: {mode}")
            connection.executescript(REALM_SCHEMA)

    def _fault(self, label: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(label)

    def create_job(
        self,
        *,
        request_id: str,
        plan: JsonDict,
        job_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        plan = json.loads(_json(plan))
        plan_digest = digest_payload(plan)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM operator_jobs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["plan_digest"]) != plan_digest:
                    raise OperatorJobConflict("request_id was replayed with a different launch plan")
                return self._job_payload(connection, existing)

            resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
            startup_token = f"startup-{uuid.uuid4().hex}"
            admission_token = f"{startup_token}:admission"
            backend_token = f"{startup_token}:backend"
            run_id = f"run-{hashlib.sha256(startup_token.encode('utf-8')).hexdigest()[:16]}"
            connection.execute(
                """
                INSERT INTO operator_jobs(
                    job_id, request_id, kind, plan_digest, plan_json, state, phase,
                    revision, startup_token, admission_token, backend_token, run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'planned', 'none', 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_job_id,
                    request_id,
                    str(plan.get("kind") or "study_launch"),
                    plan_digest,
                    _json(plan),
                    startup_token,
                    admission_token,
                    backend_token,
                    run_id,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM operator_jobs WHERE job_id = ?", (resolved_job_id,)).fetchone()
            return self._job_payload(connection, row)

    def request_approval(self, job_id: str, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            if job["state"] == "planned":
                connection.execute(
                    "UPDATE operator_jobs SET state = 'awaiting_approval', revision = revision + 1, updated_at = ? WHERE job_id = ?",
                    (timestamp, job_id),
                )
            elif job["state"] != "awaiting_approval":
                raise OperatorJobConflict(f"cannot request approval from {job['state']}")
            return self._job_payload(connection, self._require_job(connection, job_id))

    def approve(self, job_id: str, *, expected_plan_digest: str, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            if str(job["plan_digest"]) != expected_plan_digest:
                raise OperatorJobConflict("approval digest does not match the immutable launch plan")
            if job["state"] == "queued":
                return self._job_payload(connection, job)
            if job["state"] not in {"planned", "awaiting_approval"}:
                raise OperatorJobConflict(f"cannot approve job in {job['state']}")

            plan = _load_json(job["plan_json"], {})
            admission_request = {
                "resources": plan.get("resources", {}),
                "startup_token": job["startup_token"],
            }
            launch_request = {
                "plan_digest": job["plan_digest"],
                "startup_token": job["startup_token"],
                "run_id": job["run_id"],
            }
            connection.execute(
                """
                INSERT INTO admission_claims(
                    admission_token, job_id, request_digest, request_json, state,
                    owner_kind, owner_id, updated_at
                ) VALUES (?, ?, ?, ?, 'requested', 'operator_job', ?, ?)
                """,
                (
                    job["admission_token"],
                    job_id,
                    digest_payload(admission_request),
                    _json(admission_request),
                    job_id,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO backend_bindings(
                    backend_token, job_id, launch_digest, launch_json, state, updated_at
                ) VALUES (?, ?, ?, ?, 'intended', ?)
                """,
                (
                    job["backend_token"],
                    job_id,
                    digest_payload(launch_request),
                    _json(launch_request),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE operator_jobs
                SET state = 'queued', phase = 'acquire_admission', approved_digest = ?,
                    revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (expected_plan_digest, timestamp, job_id),
            )
            return self._job_payload(connection, self._require_job(connection, job_id))

    def record_admission(self, job_id: str, observation: JsonDict, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            claim = self._require_claim(connection, job_id)
            self._verify_observation(observation, "admission_token", claim["admission_token"], "request_digest", claim["request_digest"])
            if observation.get("state") != "held" or not observation.get("lease_id"):
                raise OperatorJobConflict("admission authority did not return a held lease")
            if job["state"] == "cancelled":
                return self._job_payload(connection, job)
            if job["state"] == "stopping":
                if claim["state"] == "requested":
                    connection.execute(
                        """
                        UPDATE admission_claims
                        SET authority_lease_id = ?, authority_fencing_token = ?, state = 'held', updated_at = ?
                        WHERE admission_token = ? AND state = 'requested'
                        """,
                        (
                            observation["lease_id"],
                            int(observation["fencing_token"]),
                            timestamp,
                            claim["admission_token"],
                        ),
                    )
                elif claim["state"] == "held":
                    self._verify_existing_admission(claim, observation)
                return self._job_payload(connection, self._require_job(connection, job_id))
            if job["state"] == "starting":
                self._verify_existing_admission(claim, observation)
                return self._job_payload(connection, job)
            if job["state"] != "queued" or claim["state"] != "requested":
                raise OperatorJobConflict(f"cannot record admission while job is {job['state']}")
            connection.execute(
                """
                UPDATE admission_claims
                SET authority_lease_id = ?, authority_fencing_token = ?, state = 'held', updated_at = ?
                WHERE admission_token = ?
                """,
                (
                    observation["lease_id"],
                    int(observation["fencing_token"]),
                    timestamp,
                    claim["admission_token"],
                ),
            )
            self._fault("admission_after_claim_update")
            connection.execute(
                """
                UPDATE operator_jobs
                SET state = 'starting', phase = 'ensure_backend', revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, job_id),
            )
            return self._job_payload(connection, self._require_job(connection, job_id))

    def record_backend(self, job_id: str, observation: JsonDict, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            binding = self._require_binding(connection, job_id)
            self._verify_observation(observation, "backend_token", binding["backend_token"], "launch_digest", binding["launch_digest"])
            if observation.get("state") != "live" or not observation.get("execution_id"):
                raise OperatorJobConflict("backend authority did not return a live execution")
            if job["state"] == "cancelled":
                return self._job_payload(connection, job)
            if job["state"] == "stopping":
                if binding["state"] == "intended":
                    connection.execute(
                        """
                        UPDATE backend_bindings
                        SET authority_execution_id = ?, state = 'live', updated_at = ?
                        WHERE backend_token = ? AND state = 'intended'
                        """,
                        (observation["execution_id"], timestamp, binding["backend_token"]),
                    )
                elif binding["state"] == "live":
                    self._verify_existing_backend(binding, observation)
                return self._job_payload(connection, self._require_job(connection, job_id))
            if job["state"] == "running":
                self._verify_existing_backend(binding, observation)
                return self._job_payload(connection, job)
            if job["state"] != "starting" or binding["state"] != "intended":
                raise OperatorJobConflict(f"cannot record backend while job is {job['state']}")
            connection.execute(
                """
                UPDATE backend_bindings
                SET authority_execution_id = ?, state = 'live', updated_at = ?
                WHERE backend_token = ?
                """,
                (observation["execution_id"], timestamp, binding["backend_token"]),
            )
            self._fault("backend_after_binding_update")
            connection.execute(
                """
                UPDATE operator_jobs
                SET state = 'running', phase = 'create_run_startup', revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, job_id),
            )
            return self._job_payload(connection, self._require_job(connection, job_id))

    def commit_study_handoff(self, job_id: str, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            existing = connection.execute("SELECT * FROM study_handoffs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is not None:
                self._verify_handoff(connection, job, existing)
                return self._job_payload(connection, job)
            if job["kind"] != "study_launch" or job["state"] != "running" or job["phase"] != "create_run_startup":
                raise OperatorJobConflict(f"job is not ready for study handoff: {job['state']}/{job['phase']}")
            claim = self._require_claim(connection, job_id)
            binding = self._require_binding(connection, job_id)
            if claim["state"] != "held" or claim["owner_kind"] != "operator_job" or claim["owner_id"] != job_id:
                raise OperatorJobConflict("study handoff requires the job-owned held admission claim")
            if binding["state"] != "live":
                raise OperatorJobConflict("study handoff requires a live backend identity")

            connection.execute(
                """
                INSERT INTO run_startups(
                    run_id, source_job_id, startup_token, fencing_token, state, updated_at
                ) VALUES (?, ?, ?, 1, 'awaiting_heartbeat', ?)
                """,
                (job["run_id"], job_id, job["startup_token"], timestamp),
            )
            self._fault("handoff_after_run_startup_insert")
            connection.execute(
                """
                UPDATE admission_claims
                SET state = 'transferred', owner_kind = 'run', owner_id = ?, updated_at = ?
                WHERE admission_token = ? AND state = 'held' AND owner_kind = 'operator_job' AND owner_id = ?
                """,
                (job["run_id"], timestamp, job["admission_token"], job_id),
            )
            if connection.total_changes < 2:
                raise OperatorJobConflict("admission claim transfer did not occur")
            self._fault("handoff_after_claim_transfer")
            connection.execute(
                """
                INSERT INTO study_handoffs(job_id, run_id, admission_token, operation_id, committed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, job["run_id"], job["admission_token"], f"handoff:{job_id}", timestamp),
            )
            connection.execute(
                """
                UPDATE operator_jobs
                SET phase = 'await_controller_heartbeat', revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, job_id),
            )
            self._fault("handoff_before_commit")
            return self._job_payload(connection, self._require_job(connection, job_id))

    def controller_heartbeat(
        self,
        *,
        run_id: str,
        startup_token: str,
        controller_id: str,
        fencing_token: int,
        now: Optional[float] = None,
    ) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            startup = connection.execute("SELECT * FROM run_startups WHERE run_id = ?", (run_id,)).fetchone()
            if startup is None:
                raise OperatorJobNotFound(run_id)
            if startup["startup_token"] != startup_token or int(startup["fencing_token"]) != int(fencing_token):
                raise OperatorJobConflict("controller startup token or fencing token is stale")
            job = self._require_job(connection, str(startup["source_job_id"]))
            if startup["state"] == "active":
                if startup["controller_id"] != controller_id or job["state"] != "succeeded":
                    raise OperatorJobConflict("active controller identity does not match the replay")
                return self._job_payload(connection, job)
            if startup["state"] != "awaiting_heartbeat" or job["phase"] != "await_controller_heartbeat":
                raise OperatorJobConflict("run startup is not awaiting this heartbeat")
            connection.execute(
                """
                UPDATE run_startups
                SET controller_id = ?, state = 'active', heartbeat_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (controller_id, timestamp, timestamp, run_id),
            )
            self._fault("heartbeat_after_controller_activation")
            result = {"run_id": run_id, "controller_id": controller_id, "fencing_token": int(fencing_token)}
            connection.execute(
                """
                UPDATE operator_jobs
                SET state = 'succeeded', phase = 'done', terminal_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (_json(result), timestamp, job["job_id"]),
            )
            self._fault("heartbeat_before_commit")
            return self._job_payload(connection, self._require_job(connection, str(job["job_id"])))

    def request_stop(self, job_id: str, *, reason: str = "user_cancelled", now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            handoff = connection.execute("SELECT * FROM study_handoffs WHERE job_id = ?", (job_id,)).fetchone()
            if handoff is not None:
                return {
                    "redirect": "run",
                    "run_id": str(handoff["run_id"]),
                    "job": self._job_payload(connection, job),
                }
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                return {"redirect": None, "job": self._job_payload(connection, job)}
            if job["state"] in {"planned", "awaiting_approval"}:
                result = {"stop_reason": reason, "resources_acquired": False}
                connection.execute(
                    """
                    UPDATE operator_jobs
                    SET state = 'cancelled', phase = 'done', stop_reason = ?, terminal_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (reason, _json(result), timestamp, job_id),
                )
                return {
                    "redirect": None,
                    "job": self._job_payload(connection, self._require_job(connection, job_id)),
                }
            if job["state"] != "stopping":
                connection.execute(
                    """
                    UPDATE operator_jobs
                    SET state = 'stopping', phase = 'stop_backend', stop_reason = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (reason, timestamp, job_id),
                )
            return {"redirect": None, "job": self._job_payload(connection, self._require_job(connection, job_id))}

    def record_backend_stopped(self, job_id: str, observation: JsonDict, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            binding = self._require_binding(connection, job_id)
            self._verify_observation(observation, "backend_token", binding["backend_token"], "launch_digest", binding["launch_digest"])
            if observation.get("state") not in {"stopped", "absent"}:
                raise OperatorJobConflict("backend termination is not confirmed")
            if job["state"] == "cancelled":
                if binding["state"] != "stopped":
                    raise OperatorJobConflict("cancelled job does not record a stopped backend")
                return self._job_payload(connection, job)
            if job["state"] == "stopping" and job["phase"] == "release_admission":
                if binding["state"] != "stopped":
                    raise OperatorJobConflict("release phase does not record a stopped backend")
                return self._job_payload(connection, job)
            if job["state"] != "stopping" or job["phase"] != "stop_backend":
                raise OperatorJobConflict(f"cannot record backend stop while job is {job['state']}")
            connection.execute(
                "UPDATE backend_bindings SET state = 'stopped', updated_at = ? WHERE backend_token = ?",
                (timestamp, binding["backend_token"]),
            )
            self._fault("stop_after_backend_record")
            connection.execute(
                """
                UPDATE operator_jobs
                SET phase = 'release_admission', revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, job_id),
            )
            return self._job_payload(connection, self._require_job(connection, job_id))

    def record_admission_released(self, job_id: str, observation: JsonDict, *, now: Optional[float] = None) -> JsonDict:
        timestamp = float(time.time() if now is None else now)
        with self._write() as connection:
            job = self._require_job(connection, job_id)
            claim = self._require_claim(connection, job_id)
            self._verify_observation(observation, "admission_token", claim["admission_token"], "request_digest", claim["request_digest"])
            if observation.get("state") not in {"released", "absent"}:
                raise OperatorJobConflict("admission release is not confirmed")
            if job["state"] == "cancelled":
                if claim["state"] != "released":
                    raise OperatorJobConflict("cancelled job does not record released admission")
                return self._job_payload(connection, job)
            if job["state"] != "stopping" or job["phase"] != "release_admission":
                raise OperatorJobConflict("job is not ready to release admission")
            if claim["owner_kind"] != "operator_job" or claim["owner_id"] != job_id:
                raise OperatorJobConflict("Operator Job cannot release a run-owned admission claim")
            connection.execute(
                "UPDATE admission_claims SET state = 'released', updated_at = ? WHERE admission_token = ?",
                (timestamp, claim["admission_token"]),
            )
            self._fault("stop_after_admission_record")
            result = {"stop_reason": job["stop_reason"] or "cancelled"}
            connection.execute(
                """
                UPDATE operator_jobs
                SET state = 'cancelled', phase = 'done', terminal_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (_json(result), timestamp, job_id),
            )
            return self._job_payload(connection, self._require_job(connection, job_id))

    def snapshot(self, job_id: str) -> JsonDict:
        with self._connect() as connection:
            job = self._require_job(connection, job_id)
            return self._job_payload(connection, job)

    def assert_invariants(self) -> JsonDict:
        with self._connect() as connection:
            jobs = connection.execute("SELECT * FROM operator_jobs ORDER BY job_id").fetchall()
            handoffs = connection.execute("SELECT * FROM study_handoffs ORDER BY job_id").fetchall()
            for handoff in handoffs:
                job = self._require_job(connection, str(handoff["job_id"]))
                claim = self._require_claim(connection, str(handoff["job_id"]))
                startup = connection.execute("SELECT * FROM run_startups WHERE run_id = ?", (handoff["run_id"],)).fetchone()
                if startup is None:
                    raise AssertionError("handoff has no run startup")
                if claim["state"] != "transferred" or claim["owner_kind"] != "run" or claim["owner_id"] != handoff["run_id"]:
                    raise AssertionError("handoff did not transfer the exact claim to the run")
                if job["state"] == "succeeded" and startup["state"] != "active":
                    raise AssertionError("successful study launch has no active controller")
            for job in jobs:
                claim = connection.execute("SELECT * FROM admission_claims WHERE job_id = ?", (job["job_id"],)).fetchone()
                handoff = connection.execute("SELECT 1 FROM study_handoffs WHERE job_id = ?", (job["job_id"],)).fetchone()
                if job["state"] in {"failed", "cancelled"} and handoff is None and claim is not None and claim["state"] != "released":
                    raise AssertionError("terminal non-handoff job still owns admission")
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
            return {"jobs": len(jobs), "handoffs": len(handoffs), "integrity": integrity}

    def _job_payload(self, connection: sqlite3.Connection, job: sqlite3.Row) -> JsonDict:
        claim = connection.execute("SELECT * FROM admission_claims WHERE job_id = ?", (job["job_id"],)).fetchone()
        binding = connection.execute("SELECT * FROM backend_bindings WHERE job_id = ?", (job["job_id"],)).fetchone()
        startup = connection.execute("SELECT * FROM run_startups WHERE source_job_id = ?", (job["job_id"],)).fetchone()
        handoff = connection.execute("SELECT * FROM study_handoffs WHERE job_id = ?", (job["job_id"],)).fetchone()
        payload = dict(job)
        payload["plan"] = _load_json(payload.pop("plan_json"), {})
        payload["terminal"] = _load_json(payload.pop("terminal_json"), {})
        payload["admission"] = dict(claim) if claim is not None else None
        if payload["admission"] is not None:
            payload["admission"]["request"] = _load_json(payload["admission"].pop("request_json"), {})
        payload["backend"] = dict(binding) if binding is not None else None
        if payload["backend"] is not None:
            payload["backend"]["launch"] = _load_json(payload["backend"].pop("launch_json"), {})
        payload["run_startup"] = dict(startup) if startup is not None else None
        payload["handoff"] = dict(handoff) if handoff is not None else None
        return payload

    @staticmethod
    def _require_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM operator_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise OperatorJobNotFound(job_id)
        return row

    @staticmethod
    def _require_claim(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM admission_claims WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise OperatorJobNotFound(f"admission claim for {job_id}")
        return row

    @staticmethod
    def _require_binding(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM backend_bindings WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise OperatorJobNotFound(f"backend binding for {job_id}")
        return row

    @staticmethod
    def _verify_observation(observation: JsonDict, token_key: str, token: str, digest_key: str, digest: str) -> None:
        if observation.get(token_key) != token or observation.get(digest_key) != digest:
            raise OperatorJobConflict("external observation token or digest does not match the durable intent")

    @staticmethod
    def _verify_existing_admission(claim: sqlite3.Row, observation: JsonDict) -> None:
        if claim["authority_lease_id"] != observation["lease_id"] or int(claim["authority_fencing_token"]) != int(observation["fencing_token"]):
            raise OperatorJobConflict("admission replay returned a different lease identity")

    @staticmethod
    def _verify_existing_backend(binding: sqlite3.Row, observation: JsonDict) -> None:
        if binding["authority_execution_id"] != observation["execution_id"]:
            raise OperatorJobConflict("backend replay returned a different execution identity")

    @staticmethod
    def _verify_handoff(connection: sqlite3.Connection, job: sqlite3.Row, handoff: sqlite3.Row) -> None:
        claim = connection.execute("SELECT * FROM admission_claims WHERE job_id = ?", (job["job_id"],)).fetchone()
        startup = connection.execute("SELECT * FROM run_startups WHERE run_id = ?", (handoff["run_id"],)).fetchone()
        if claim is None or startup is None:
            raise OperatorJobConflict("existing handoff is incomplete")
        if handoff["run_id"] != job["run_id"] or handoff["admission_token"] != job["admission_token"]:
            raise OperatorJobConflict("existing handoff identity differs from the job")
        if claim["owner_kind"] != "run" or claim["owner_id"] != job["run_id"] or claim["state"] != "transferred":
            raise OperatorJobConflict("existing handoff does not own the admission claim")


class _ExternalAuthorityBase(_SqliteBase):
    def __init__(self, database_path: Path):
        super().__init__(database_path)
        with self._connect() as connection:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise OperatorJobSpikeError(f"SQLite did not enable external WAL mode: {mode}")
            connection.executescript(EXTERNAL_SCHEMA)


class DurableFakeAdmission(_ExternalAuthorityBase):
    """Durable idempotent fake for a local or remote capacity authority."""

    def ensure_acquired(self, *, admission_token: str, request_digest: str, request: JsonDict) -> JsonDict:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM fake_admission_allocations WHERE admission_token = ?", (admission_token,)
            ).fetchone()
            if row is not None:
                self._verify_request(row, request_digest, request)
                connection.execute(
                    "UPDATE fake_admission_allocations SET ensure_calls = ensure_calls + 1, updated_at = ? WHERE admission_token = ?",
                    (time.time(), admission_token),
                )
                if row["state"] == "released":
                    raise OperatorJobConflict("released admission token cannot allocate capacity again")
                return self._payload(connection, admission_token)
            lease_id = f"lease-{hashlib.sha256(admission_token.encode('utf-8')).hexdigest()[:16]}"
            connection.execute(
                """
                INSERT INTO fake_admission_allocations(
                    admission_token, request_digest, request_json, lease_id, fencing_token,
                    state, created_count, ensure_calls, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'held', 1, 1, ?)
                """,
                (admission_token, request_digest, _json(request), lease_id, time.time()),
            )
            return self._payload(connection, admission_token)

    def ensure_released(self, *, admission_token: str, request_digest: str, request: JsonDict) -> JsonDict:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM fake_admission_allocations WHERE admission_token = ?", (admission_token,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO fake_admission_allocations(
                        admission_token, request_digest, request_json, state,
                        created_count, release_calls, updated_at
                    ) VALUES (?, ?, ?, 'released', 0, 1, ?)
                    """,
                    (admission_token, request_digest, _json(request), time.time()),
                )
            else:
                self._verify_request(row, request_digest, request)
                connection.execute(
                    """
                    UPDATE fake_admission_allocations
                    SET state = 'released', release_calls = release_calls + 1, updated_at = ?
                    WHERE admission_token = ?
                    """,
                    (time.time(), admission_token),
                )
            payload = self._payload(connection, admission_token)
            payload["state"] = "released"
            return payload

    def inspect(self, admission_token: str) -> Optional[JsonDict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fake_admission_allocations WHERE admission_token = ?", (admission_token,)
            ).fetchone()
            return self._payload(connection, admission_token) if row is not None else None

    @staticmethod
    def _verify_request(row: sqlite3.Row, digest: str, request: JsonDict) -> None:
        if row["request_digest"] != digest or row["request_json"] != _json(request):
            raise OperatorJobConflict("admission token was reused with a different request digest")

    @staticmethod
    def _payload(connection: sqlite3.Connection, token: str) -> JsonDict:
        row = connection.execute(
            "SELECT * FROM fake_admission_allocations WHERE admission_token = ?", (token,)
        ).fetchone()
        if row is None:
            raise OperatorJobNotFound(token)
        return {
            "admission_token": row["admission_token"],
            "request_digest": row["request_digest"],
            "lease_id": row["lease_id"],
            "fencing_token": row["fencing_token"],
            "state": row["state"],
            "created_count": row["created_count"],
            "ensure_calls": row["ensure_calls"],
            "release_calls": row["release_calls"],
        }


class DurableFakeBackend(_ExternalAuthorityBase):
    """Durable idempotent fake for an independently reconcilable backend."""

    def ensure_started(
        self,
        *,
        backend_token: str,
        launch_digest: str,
        launch: JsonDict,
        admission_lease_id: str,
    ) -> JsonDict:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM fake_backend_executions WHERE backend_token = ?", (backend_token,)
            ).fetchone()
            if row is not None:
                self._verify_launch(row, launch_digest, launch, admission_lease_id)
                connection.execute(
                    "UPDATE fake_backend_executions SET ensure_calls = ensure_calls + 1, updated_at = ? WHERE backend_token = ?",
                    (time.time(), backend_token),
                )
                if row["state"] == "stopped":
                    raise OperatorJobConflict("stopped backend token cannot create another execution")
                return self._payload(connection, backend_token)
            execution_id = f"execution-{hashlib.sha256(backend_token.encode('utf-8')).hexdigest()[:16]}"
            connection.execute(
                """
                INSERT INTO fake_backend_executions(
                    backend_token, launch_digest, launch_json, admission_lease_id,
                    execution_id, state, created_count, ensure_calls, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'live', 1, 1, ?)
                """,
                (backend_token, launch_digest, _json(launch), admission_lease_id, execution_id, time.time()),
            )
            return self._payload(connection, backend_token)

    def ensure_stopped(self, *, backend_token: str, launch_digest: str, launch: JsonDict) -> JsonDict:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM fake_backend_executions WHERE backend_token = ?", (backend_token,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO fake_backend_executions(
                        backend_token, launch_digest, launch_json, state,
                        created_count, stop_calls, updated_at
                    ) VALUES (?, ?, ?, 'stopped', 0, 1, ?)
                    """,
                    (backend_token, launch_digest, _json(launch), time.time()),
                )
            else:
                if row["launch_digest"] != launch_digest or row["launch_json"] != _json(launch):
                    raise OperatorJobConflict("backend token was reused with a different launch digest")
                connection.execute(
                    """
                    UPDATE fake_backend_executions
                    SET state = 'stopped', stop_calls = stop_calls + 1, updated_at = ?
                    WHERE backend_token = ?
                    """,
                    (time.time(), backend_token),
                )
            payload = self._payload(connection, backend_token)
            payload["state"] = "stopped"
            return payload

    def inspect(self, backend_token: str) -> Optional[JsonDict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fake_backend_executions WHERE backend_token = ?", (backend_token,)
            ).fetchone()
            return self._payload(connection, backend_token) if row is not None else None

    @staticmethod
    def _verify_launch(row: sqlite3.Row, digest: str, launch: JsonDict, lease_id: str) -> None:
        if row["launch_digest"] != digest or row["launch_json"] != _json(launch) or row["admission_lease_id"] != lease_id:
            raise OperatorJobConflict("backend token was reused with a different launch digest or admission lease")

    @staticmethod
    def _payload(connection: sqlite3.Connection, token: str) -> JsonDict:
        row = connection.execute(
            "SELECT * FROM fake_backend_executions WHERE backend_token = ?", (token,)
        ).fetchone()
        if row is None:
            raise OperatorJobNotFound(token)
        return {
            "backend_token": row["backend_token"],
            "launch_digest": row["launch_digest"],
            "admission_lease_id": row["admission_lease_id"],
            "execution_id": row["execution_id"],
            "state": row["state"],
            "created_count": row["created_count"],
            "ensure_calls": row["ensure_calls"],
            "stop_calls": row["stop_calls"],
        }


class OperatorJobSupervisorSpike:
    """One-step reconciler whose every external effect has a durable token."""

    TERMINAL = {"succeeded", "failed", "cancelled"}

    def __init__(
        self,
        ledger: OperatorJobLedgerSpike,
        admission: DurableFakeAdmission,
        backend: DurableFakeBackend,
        *,
        fault_hook: Optional[FaultHook] = None,
    ):
        self.ledger = ledger
        self.admission = admission
        self.backend = backend
        self.fault_hook = fault_hook

    def _fault(self, label: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(label)

    def reconcile_once(self, job_id: str) -> JsonDict:
        job = self.ledger.snapshot(job_id)
        if job["state"] in self.TERMINAL or job["state"] in {"planned", "awaiting_approval"}:
            return job
        if job["state"] == "queued":
            claim = job["admission"]
            self._fault("before_admission_external")
            try:
                observation = self.admission.ensure_acquired(
                    admission_token=claim["admission_token"],
                    request_digest=claim["request_digest"],
                    request=claim["request"],
                )
            except OperatorJobConflict:
                current = self.ledger.snapshot(job_id)
                if current["state"] == "stopping" or current["state"] in self.TERMINAL:
                    return current
                raise
            self._fault("after_admission_external_commit")
            result = self.ledger.record_admission(job_id, observation)
            self._fault("after_admission_ledger_commit")
            return result
        if job["state"] == "starting":
            claim = job["admission"]
            binding = job["backend"]
            self._fault("before_backend_external")
            try:
                observation = self.backend.ensure_started(
                    backend_token=binding["backend_token"],
                    launch_digest=binding["launch_digest"],
                    launch=binding["launch"],
                    admission_lease_id=claim["authority_lease_id"],
                )
            except OperatorJobConflict:
                current = self.ledger.snapshot(job_id)
                if current["state"] == "stopping" or current["state"] in self.TERMINAL:
                    return current
                raise
            self._fault("after_backend_external_commit")
            result = self.ledger.record_backend(job_id, observation)
            self._fault("after_backend_ledger_commit")
            return result
        if job["state"] == "running":
            if job["phase"] == "create_run_startup":
                self._fault("before_handoff_ledger_transaction")
                result = self.ledger.commit_study_handoff(job_id)
                self._fault("after_handoff_ledger_commit")
                return result
            return job
        if job["state"] == "stopping":
            binding = job["backend"]
            if job["phase"] == "stop_backend":
                self._fault("before_backend_stop_external")
                observation = self.backend.ensure_stopped(
                    backend_token=binding["backend_token"],
                    launch_digest=binding["launch_digest"],
                    launch=binding["launch"],
                )
                self._fault("after_backend_stop_external_commit")
                result = self.ledger.record_backend_stopped(job_id, observation)
                self._fault("after_backend_stop_ledger_commit")
                return result
            if job["phase"] == "release_admission":
                claim = job["admission"]
                self._fault("before_admission_release_external")
                observation = self.admission.ensure_released(
                    admission_token=claim["admission_token"],
                    request_digest=claim["request_digest"],
                    request=claim["request"],
                )
                self._fault("after_admission_release_external_commit")
                result = self.ledger.record_admission_released(job_id, observation)
                self._fault("after_admission_release_ledger_commit")
                return result
        raise OperatorJobConflict(f"unsupported reconciler state {job['state']}/{job['phase']}")

    def reconcile_until_blocked(self, job_id: str, *, max_steps: int = 20) -> JsonDict:
        for _ in range(max_steps):
            before = self.ledger.snapshot(job_id)
            after = self.reconcile_once(job_id)
            if (before["state"], before["phase"], before["revision"]) == (
                after["state"],
                after["phase"],
                after["revision"],
            ):
                return after
        raise OperatorJobSpikeError("reconciler did not reach a stable state")
