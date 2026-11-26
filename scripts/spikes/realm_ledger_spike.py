"""Disposable RealmLedger architecture spike.

This module proves transaction, fencing, provisional-retention, and idempotency
invariants before the production WP4A API/schema is designed.  It is deliberately
not imported by ``optpilot`` and must not be wired into the current run path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence


JsonDict = Dict[str, Any]
FaultHook = Callable[[str], None]


class LedgerSpikeError(RuntimeError):
    """Base error for the disposable spike."""


class LedgerConflict(LedgerSpikeError):
    """An expected revision, fence, identity, or idempotency check failed."""


class LedgerNotFound(LedgerSpikeError):
    """A requested realm entity does not exist."""


class LedgerExpired(LedgerSpikeError):
    """A provisional owner transaction is no longer active."""


@dataclass(frozen=True)
class RunReceipt:
    operation_id: str
    run_id: str
    run_revision: int
    owner_revision: int
    first_sequence: int
    last_sequence: int
    accepted_trials: int
    handle_id: str

    @classmethod
    def from_dict(cls, payload: JsonDict) -> "RunReceipt":
        return cls(**payload)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class FenceReceipt:
    operation_id: str
    run_id: str
    run_revision: int
    fencing_token: int
    controller_id: str

    @classmethod
    def from_dict(cls, payload: JsonDict) -> "FenceReceipt":
        return cls(**payload)

    def to_dict(self) -> JsonDict:
        return asdict(self)


SCHEMA = """
CREATE TABLE IF NOT EXISTS realm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    store_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    owner_kind TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS content_objects (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    content_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    sealed INTEGER NOT NULL CHECK(sealed IN (0, 1)),
    verified INTEGER NOT NULL CHECK(verified IN (0, 1)),
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (store_id, content_ref)
);

CREATE TABLE IF NOT EXISTS owner_intents (
    intent_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    base_owner_revision INTEGER NOT NULL CHECK(base_owner_revision >= 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'committed', 'expired', 'aborted')),
    expires_at REAL NOT NULL,
    committed_operation_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS provisional_content (
    intent_id TEXT NOT NULL REFERENCES owner_intents(intent_id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (intent_id, store_id, content_ref, role),
    FOREIGN KEY (store_id, content_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE IF NOT EXISTS owner_content (
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    added_owner_revision INTEGER NOT NULL,
    added_operation_id TEXT NOT NULL,
    PRIMARY KEY (owner_id, store_id, content_ref, role),
    FOREIGN KEY (store_id, content_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    revision INTEGER NOT NULL DEFAULT 0,
    controller_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL DEFAULT 1,
    max_trials INTEGER NOT NULL CHECK(max_trials >= 0),
    accepted_trials INTEGER NOT NULL DEFAULT 0,
    next_sequence INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'running',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    candidate_id TEXT NOT NULL,
    candidate_ref TEXT NOT NULL,
    candidate_format TEXT NOT NULL,
    content_refs_json TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, candidate_id),
    UNIQUE (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS logical_trials (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    logical_trial_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    repetition_index INTEGER NOT NULL CHECK(repetition_index >= 0),
    budget_slot INTEGER NOT NULL,
    state TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, logical_trial_id),
    UNIQUE (run_id, budget_slot),
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES candidates(run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS handles (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    handle_id TEXT NOT NULL,
    logical_trial_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('accepted', 'terminal')),
    accepted_sequence INTEGER NOT NULL,
    terminal_sequence INTEGER,
    accepted_operation_id TEXT NOT NULL,
    terminal_operation_id TEXT,
    PRIMARY KEY (run_id, handle_id),
    UNIQUE (run_id, logical_trial_id),
    UNIQUE (run_id, accepted_sequence),
    FOREIGN KEY (run_id, logical_trial_id)
        REFERENCES logical_trials(run_id, logical_trial_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    attempt_id TEXT NOT NULL,
    logical_trial_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL CHECK(attempt_index > 0),
    outcome TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, attempt_id),
    UNIQUE (run_id, logical_trial_id, attempt_index),
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, logical_trial_id)
        REFERENCES logical_trials(run_id, logical_trial_id)
);

CREATE TABLE IF NOT EXISTS observations (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    observation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, observation_id),
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, attempt_id)
        REFERENCES attempts(run_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    artifact_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_id),
    FOREIGN KEY (run_id, attempt_id)
        REFERENCES attempts(run_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    committed_at REAL NOT NULL
);
"""


class RealmLedgerSpike:
    """Small SQLite/WAL model used only to prove the reviewed invariants."""

    def __init__(self, database_path: Path, *, fault_hook: Optional[FaultHook] = None):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.fault_hook = fault_hook
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise LedgerSpikeError(f"SQLite did not enable WAL mode: {mode}")
            try:
                connection.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version=1;\nCOMMIT;")
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO realm_meta(key, value) VALUES('schema_version', 'spike-1')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO realm_meta(key, value) VALUES('realm_id', ?)",
                    (str(uuid.uuid4()),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        self._fault("before_write_lock")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
            # This hook deliberately runs while the connection remains open so
            # subprocess tests can prove abrupt post-COMMIT WAL recovery rather
            # than merely simulating a response lost after a clean close.
            self._fault("after_commit")
        finally:
            connection.close()

    def _fault(self, step: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(step)

    def create_run(
        self,
        *,
        run_id: str,
        store_id: str,
        max_trials: int,
        owner_id: Optional[str] = None,
        controller_id: str = "controller-a",
        now: Optional[float] = None,
    ) -> JsonDict:
        if not run_id or not store_id or not controller_id:
            raise ValueError("run_id, store_id, and controller_id are required")
        if max_trials < 0:
            raise ValueError("max_trials must be nonnegative")
        owner_id = owner_id or f"run:{run_id}"
        try:
            with self._write() as connection:
                timestamp = time.time() if now is None else float(now)
                connection.execute(
                    "INSERT OR IGNORE INTO stores(store_id, created_at) VALUES(?, ?)",
                    (store_id, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO owners(
                        owner_id, store_id, owner_kind, revision, state, created_at, updated_at
                    ) VALUES(?, ?, 'run', 0, 'active', ?, ?)
                    """,
                    (owner_id, store_id, timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, owner_id, revision, controller_id, fencing_token, max_trials,
                        accepted_trials, next_sequence, state, created_at, updated_at
                    ) VALUES(?, ?, 0, ?, 1, ?, 0, 0, 'running', ?, ?)
                    """,
                    (run_id, owner_id, controller_id, max_trials, timestamp, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerConflict(f"run or owner already exists: {run_id}") from exc
        return self.read_run(run_id)

    def begin_owner_intent(
        self,
        *,
        owner_id: str,
        ttl_seconds: float,
        intent_id: Optional[str] = None,
        expected_owner_revision: Optional[int] = None,
        now: Optional[float] = None,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        intent_id = intent_id or str(uuid.uuid4())
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            owner = connection.execute(
                "SELECT store_id, revision FROM owners WHERE owner_id=? AND state='active'",
                (owner_id,),
            ).fetchone()
            if owner is None:
                raise LedgerNotFound(f"active owner not found: {owner_id}")
            base_owner_revision = int(owner["revision"])
            if (
                expected_owner_revision is not None
                and base_owner_revision != int(expected_owner_revision)
            ):
                raise LedgerConflict(
                    "stale owner revision at intent creation: "
                    f"expected {expected_owner_revision}, current {base_owner_revision}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO owner_intents(
                        intent_id, owner_id, store_id, base_owner_revision, state, expires_at,
                        committed_operation_id, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'active', ?, NULL, ?, ?)
                    """,
                    (
                        intent_id,
                        owner_id,
                        owner["store_id"],
                        base_owner_revision,
                        timestamp + ttl_seconds,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict(f"owner intent already exists: {intent_id}") from exc
        return intent_id

    def stage_content(
        self,
        *,
        intent_id: str,
        content_ref: str,
        role: str,
        size_bytes: int,
        kind: str = "blob",
        sealed: bool = True,
        verified: bool = True,
        metadata: Optional[JsonDict] = None,
        now: Optional[float] = None,
    ) -> None:
        if not content_ref or not role or not kind:
            raise ValueError("content_ref, role, and kind are required")
        if size_bytes < 0:
            raise ValueError("size_bytes must be nonnegative")
        metadata_json = _canonical_json(metadata or {})
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            intent = self._active_intent(connection, intent_id, timestamp)
            existing = connection.execute(
                """
                SELECT kind, size_bytes, sealed, verified, metadata_json
                FROM content_objects WHERE store_id=? AND content_ref=?
                """,
                (intent["store_id"], content_ref),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO content_objects(
                        store_id, content_ref, kind, size_bytes, sealed, verified,
                        metadata_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent["store_id"],
                        content_ref,
                        kind,
                        size_bytes,
                        1 if sealed else 0,
                        1 if verified else 0,
                        metadata_json,
                        timestamp,
                    ),
                )
            elif (
                existing["kind"] != kind
                or int(existing["size_bytes"]) != size_bytes
                or int(existing["sealed"]) != (1 if sealed else 0)
                or int(existing["verified"]) != (1 if verified else 0)
                or existing["metadata_json"] != metadata_json
            ):
                raise LedgerConflict(f"content identity metadata mismatch: {content_ref}")
            connection.execute(
                """
                INSERT OR IGNORE INTO provisional_content(intent_id, store_id, content_ref, role)
                VALUES(?, ?, ?, ?)
                """,
                (intent_id, intent["store_id"], content_ref, role),
            )

    def commit_candidate(
        self,
        *,
        operation_id: str,
        intent_id: str,
        run_id: str,
        expected_run_revision: int,
        controller_id: str,
        fencing_token: int,
        candidate_id: str,
        candidate_ref: str,
        candidate_format: str,
        candidate_content_refs: Sequence[str],
        logical_trial_id: str,
        handle_id: str,
        seed: int,
        repetition_index: int,
        payload: Optional[JsonDict] = None,
        now: Optional[float] = None,
    ) -> RunReceipt:
        content_refs = tuple(sorted(candidate_content_refs))
        if len(content_refs) != len(set(content_refs)) or any(not ref for ref in content_refs):
            raise ValueError("candidate_content_refs must contain unique nonempty refs")
        if not controller_id or not handle_id:
            raise ValueError("controller_id and handle_id are required")
        semantic_payload = payload or {}
        expected_candidate_ref = candidate_ref_for(
            candidate_format=candidate_format,
            spec=semantic_payload,
            content_refs=content_refs,
        )
        if candidate_ref != expected_candidate_ref:
            raise LedgerConflict("candidate_ref does not match its normalized envelope and content closure")
        request = {
            "kind": "candidate_accept",
            "intent_id": intent_id,
            "run_id": run_id,
            "expected_run_revision": expected_run_revision,
            "controller_id": controller_id,
            "fencing_token": fencing_token,
            "candidate_id": candidate_id,
            "candidate_ref": candidate_ref,
            "candidate_format": candidate_format,
            "candidate_content_refs": list(content_refs),
            "logical_trial_id": logical_trial_id,
            "handle_id": handle_id,
            "seed": seed,
            "repetition_index": repetition_index,
            "payload": semantic_payload,
        }
        request_hash = _hash_json(request)
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            replay = self._operation_replay(connection, operation_id, request_hash)
            if replay is not None:
                return RunReceipt.from_dict(replay)
            run = self._writable_run(
                connection,
                run_id=run_id,
                expected_run_revision=expected_run_revision,
                controller_id=controller_id,
                fencing_token=fencing_token,
            )
            intent = self._active_intent(connection, intent_id, timestamp)
            self._assert_intent_owner(intent, run)
            if int(run["accepted_trials"]) >= int(run["max_trials"]):
                raise LedgerConflict("run trial budget is exhausted")

            expected_membership = {(ref, "candidate") for ref in content_refs}

            owner_revision = self._adopt_intent_content(
                connection,
                intent=intent,
                expected_membership=expected_membership,
                operation_id=operation_id,
                timestamp=timestamp,
            )
            self._fault("after_owner_membership")

            first_sequence = int(run["next_sequence"]) + 1
            last_sequence = first_sequence + 1
            budget_slot = int(run["accepted_trials"]) + 1
            try:
                connection.execute(
                    """
                    INSERT INTO candidates(
                        run_id, candidate_id, candidate_ref, candidate_format,
                        content_refs_json, sequence, payload_json, operation_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        candidate_id,
                        candidate_ref,
                        candidate_format,
                        _canonical_json(list(content_refs)),
                        first_sequence,
                        _canonical_json(semantic_payload),
                        operation_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO logical_trials(
                        run_id, logical_trial_id, candidate_id, seed,
                        repetition_index, budget_slot, state, sequence, operation_id
                    ) VALUES(?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        run_id,
                        logical_trial_id,
                        candidate_id,
                        int(seed),
                        int(repetition_index),
                        budget_slot,
                        last_sequence,
                        operation_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO handles(
                        run_id, handle_id, logical_trial_id, state,
                        accepted_sequence, terminal_sequence,
                        accepted_operation_id, terminal_operation_id
                    ) VALUES(?, ?, ?, 'accepted', ?, NULL, ?, NULL)
                    """,
                    (run_id, handle_id, logical_trial_id, last_sequence, operation_id),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    sequence=first_sequence,
                    event_type="candidate.accepted",
                    entity_id=candidate_id,
                    payload={"candidate_ref": candidate_ref, "content_refs": list(content_refs)},
                    operation_id=operation_id,
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    sequence=last_sequence,
                    event_type="handle.accepted",
                    entity_id=handle_id,
                    payload={
                        "logical_trial_id": logical_trial_id,
                        "budget_slot": budget_slot,
                        "seed": seed,
                        "repetition_index": repetition_index,
                    },
                    operation_id=operation_id,
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict(
                    "candidate, trial, handle, sequence, or budget slot already exists"
                ) from exc

            run_revision = int(run["revision"]) + 1
            connection.execute(
                """
                UPDATE runs
                SET revision=?, accepted_trials=?, next_sequence=?, updated_at=?
                WHERE run_id=?
                """,
                (run_revision, budget_slot, last_sequence, timestamp, run_id),
            )
            self._fault("after_domain_records")

            receipt = RunReceipt(
                operation_id=operation_id,
                run_id=run_id,
                run_revision=run_revision,
                owner_revision=owner_revision,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
                accepted_trials=budget_slot,
                handle_id=handle_id,
            )
            self._record_operation(
                connection,
                operation_id,
                run_id,
                request_hash,
                receipt.to_dict(),
                timestamp,
            )
            self._fault("before_commit")
            return receipt

    def commit_attempt(
        self,
        *,
        operation_id: str,
        intent_id: str,
        run_id: str,
        expected_run_revision: int,
        controller_id: str,
        fencing_token: int,
        logical_trial_id: str,
        attempt_id: str,
        attempt_index: int,
        outcome: str,
        observation: JsonDict,
        artifacts: Sequence[JsonDict],
        payload: Optional[JsonDict] = None,
        now: Optional[float] = None,
    ) -> RunReceipt:
        normalized_artifacts = _normalize_artifacts(artifacts)
        request = {
            "kind": "attempt_complete",
            "intent_id": intent_id,
            "run_id": run_id,
            "expected_run_revision": expected_run_revision,
            "controller_id": controller_id,
            "fencing_token": fencing_token,
            "logical_trial_id": logical_trial_id,
            "attempt_id": attempt_id,
            "attempt_index": attempt_index,
            "outcome": outcome,
            "observation": observation,
            "artifacts": normalized_artifacts,
            "payload": payload or {},
        }
        request_hash = _hash_json(request)
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            replay = self._operation_replay(connection, operation_id, request_hash)
            if replay is not None:
                return RunReceipt.from_dict(replay)
            run = self._writable_run(
                connection,
                run_id=run_id,
                expected_run_revision=expected_run_revision,
                controller_id=controller_id,
                fencing_token=fencing_token,
            )
            intent = self._active_intent(connection, intent_id, timestamp)
            self._assert_intent_owner(intent, run)
            trial = connection.execute(
                """
                SELECT logical_trials.state AS trial_state,
                       handles.state AS handle_state,
                       handles.handle_id AS handle_id
                FROM logical_trials
                JOIN handles USING(run_id, logical_trial_id)
                WHERE logical_trials.run_id=? AND logical_trials.logical_trial_id=?
                """,
                (run_id, logical_trial_id),
            ).fetchone()
            if trial is None:
                raise LedgerNotFound(f"logical trial or handle not found: {logical_trial_id}")
            if trial["trial_state"] == "terminal" or trial["handle_state"] == "terminal":
                raise LedgerConflict(f"logical trial is already terminal: {logical_trial_id}")
            handle_id = str(trial["handle_id"])

            expected_membership = {
                (artifact["content_ref"], artifact["role"])
                for artifact in normalized_artifacts
            }

            owner_revision = self._adopt_intent_content(
                connection,
                intent=intent,
                expected_membership=expected_membership,
                operation_id=operation_id,
                timestamp=timestamp,
            )
            self._fault("after_owner_membership")

            first_sequence = int(run["next_sequence"]) + 1
            last_sequence = first_sequence + 1
            observation_id = f"observation:{attempt_id}"
            try:
                connection.execute(
                    """
                    INSERT INTO attempts(
                        run_id, attempt_id, logical_trial_id, attempt_index,
                        outcome, sequence, payload_json, operation_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        attempt_id,
                        logical_trial_id,
                        int(attempt_index),
                        outcome,
                        first_sequence,
                        _canonical_json(payload or {}),
                        operation_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO observations(
                        run_id, observation_id, attempt_id, sequence,
                        payload_json, operation_id
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, observation_id, attempt_id, last_sequence, _canonical_json(observation), operation_id),
                )
                connection.executemany(
                    """
                    INSERT INTO artifacts(
                        run_id, artifact_id, attempt_id, content_ref, role, operation_id
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            run_id,
                            artifact["artifact_id"],
                            attempt_id,
                            artifact["content_ref"],
                            artifact["role"],
                            operation_id,
                        )
                        for artifact in normalized_artifacts
                    ],
                )
                connection.execute(
                    """
                    UPDATE logical_trials SET state='terminal'
                    WHERE run_id=? AND logical_trial_id=?
                    """,
                    (run_id, logical_trial_id),
                )
                connection.execute(
                    """
                    UPDATE handles
                    SET state='terminal', terminal_sequence=?, terminal_operation_id=?
                    WHERE run_id=? AND handle_id=?
                    """,
                    (first_sequence, operation_id, run_id, handle_id),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    sequence=first_sequence,
                    event_type="attempt.terminal",
                    entity_id=attempt_id,
                    payload={
                        "handle_id": handle_id,
                        "outcome": outcome,
                        "attempt_index": attempt_index,
                    },
                    operation_id=operation_id,
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    sequence=last_sequence,
                    event_type="observation.committed",
                    entity_id=observation_id,
                    payload={"attempt_id": attempt_id},
                    operation_id=operation_id,
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict("attempt, observation, or sequence already exists") from exc

            run_revision = int(run["revision"]) + 1
            connection.execute(
                "UPDATE runs SET revision=?, next_sequence=?, updated_at=? WHERE run_id=?",
                (run_revision, last_sequence, timestamp, run_id),
            )
            self._fault("after_domain_records")
            receipt = RunReceipt(
                operation_id=operation_id,
                run_id=run_id,
                run_revision=run_revision,
                owner_revision=owner_revision,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
                accepted_trials=int(run["accepted_trials"]),
                handle_id=handle_id,
            )
            self._record_operation(
                connection,
                operation_id,
                run_id,
                request_hash,
                receipt.to_dict(),
                timestamp,
            )
            self._fault("before_commit")
            return receipt

    def advance_fence(
        self,
        *,
        operation_id: str,
        run_id: str,
        expected_controller_id: str,
        expected_fencing_token: int,
        new_controller_id: str,
        now: Optional[float] = None,
    ) -> FenceReceipt:
        if not expected_controller_id or not new_controller_id:
            raise ValueError("expected_controller_id and new_controller_id are required")
        request = {
            "kind": "controller_handoff",
            "run_id": run_id,
            "expected_controller_id": expected_controller_id,
            "expected_fencing_token": expected_fencing_token,
            "new_controller_id": new_controller_id,
        }
        request_hash = _hash_json(request)
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            replay = self._operation_replay(connection, operation_id, request_hash)
            if replay is not None:
                return FenceReceipt.from_dict(replay)
            run = connection.execute(
                "SELECT controller_id, fencing_token, revision, state FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise LedgerNotFound(f"run not found: {run_id}")
            if run["state"] != "running":
                raise LedgerConflict(f"run is not writable: {run['state']}")
            if run["controller_id"] != expected_controller_id:
                raise LedgerConflict("controller handoff has a stale holder identity")
            if int(run["fencing_token"]) != int(expected_fencing_token):
                raise LedgerConflict("stale fencing token")
            new_token = int(run["fencing_token"]) + 1
            run_revision = int(run["revision"]) + 1
            connection.execute(
                """
                UPDATE runs
                SET controller_id=?, fencing_token=?, revision=?, updated_at=?
                WHERE run_id=?
                """,
                (new_controller_id, new_token, run_revision, timestamp, run_id),
            )
            receipt = FenceReceipt(
                operation_id=operation_id,
                run_id=run_id,
                run_revision=run_revision,
                fencing_token=new_token,
                controller_id=new_controller_id,
            )
            self._record_operation(
                connection,
                operation_id,
                run_id,
                request_hash,
                receipt.to_dict(),
                timestamp,
            )
            self._fault("before_commit")
            return receipt

    def expire_owner_intents(self, *, now: Optional[float] = None) -> int:
        with self._write() as connection:
            timestamp = time.time() if now is None else float(now)
            rows = connection.execute(
                "SELECT intent_id FROM owner_intents WHERE state='active' AND expires_at<=?",
                (timestamp,),
            ).fetchall()
            if not rows:
                return 0
            intent_ids = [row["intent_id"] for row in rows]
            connection.executemany(
                "DELETE FROM provisional_content WHERE intent_id=?",
                [(intent_id,) for intent_id in intent_ids],
            )
            connection.executemany(
                "UPDATE owner_intents SET state='expired', updated_at=? WHERE intent_id=?",
                [(timestamp, intent_id) for intent_id in intent_ids],
            )
            return len(intent_ids)

    def read_run(self, run_id: str) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                result = self._read_run_from_connection(connection, run_id)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def snapshot(self, run_id: str) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                run = self._read_run_from_connection(connection, run_id)
                self._fault("after_snapshot_run_read")
                owner_content = [dict(row) for row in connection.execute(
                    """
                    SELECT store_id, content_ref, role, added_owner_revision, added_operation_id
                    FROM owner_content WHERE owner_id=? ORDER BY content_ref, role
                    """,
                    (run["owner_id"],),
                )]
                result = {
                    "run": run,
                    "owner_content": owner_content,
                    "candidates": self._rows_with_json(connection, "candidates", run_id),
                    "logical_trials": self._rows_with_json(connection, "logical_trials", run_id),
                    "handles": [dict(row) for row in connection.execute(
                        "SELECT * FROM handles WHERE run_id=? ORDER BY accepted_sequence",
                        (run_id,),
                    )],
                    "attempts": self._rows_with_json(connection, "attempts", run_id),
                    "observations": self._rows_with_json(connection, "observations", run_id),
                    "artifacts": [dict(row) for row in connection.execute(
                        "SELECT * FROM artifacts WHERE run_id=? ORDER BY artifact_id",
                        (run_id,),
                    )],
                    "events": self._rows_with_json(connection, "run_events", run_id),
                    "operations": [dict(row) for row in connection.execute(
                        "SELECT operation_id, request_hash, result_json FROM operations WHERE run_id=? ORDER BY committed_at",
                        (run_id,),
                    )],
                }
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def integrity_check(self) -> JsonDict:
        with self._connect() as connection:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        return {"journal_mode": mode, "integrity": integrity, "foreign_key_errors": foreign_keys}

    @staticmethod
    def _read_run_from_connection(connection: sqlite3.Connection, run_id: str) -> JsonDict:
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise LedgerNotFound(f"run not found: {run_id}")
        owner = connection.execute("SELECT * FROM owners WHERE owner_id=?", (run["owner_id"],)).fetchone()
        assert owner is not None
        return {
            "run_id": run["run_id"],
            "owner_id": run["owner_id"],
            "store_id": owner["store_id"],
            "run_revision": int(run["revision"]),
            "owner_revision": int(owner["revision"]),
            "controller_id": run["controller_id"],
            "fencing_token": int(run["fencing_token"]),
            "max_trials": int(run["max_trials"]),
            "accepted_trials": int(run["accepted_trials"]),
            "next_sequence": int(run["next_sequence"]),
            "state": run["state"],
        }

    def _active_intent(self, connection: sqlite3.Connection, intent_id: str, now: float) -> sqlite3.Row:
        intent = connection.execute("SELECT * FROM owner_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if intent is None:
            raise LedgerNotFound(f"owner intent not found: {intent_id}")
        if intent["state"] != "active" or float(intent["expires_at"]) <= now:
            raise LedgerExpired(f"owner intent is not active: {intent_id}")
        return intent

    @staticmethod
    def _assert_intent_owner(intent: sqlite3.Row, run: sqlite3.Row) -> None:
        if intent["owner_id"] != run["owner_id"]:
            raise LedgerConflict("owner intent does not belong to the run owner")

    @staticmethod
    def _writable_run(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        expected_run_revision: int,
        controller_id: str,
        fencing_token: int,
    ) -> sqlite3.Row:
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise LedgerNotFound(f"run not found: {run_id}")
        if run["state"] != "running":
            raise LedgerConflict(f"run is not writable: {run['state']}")
        if run["controller_id"] != controller_id:
            raise LedgerConflict("stale controller identity")
        if int(run["fencing_token"]) != int(fencing_token):
            raise LedgerConflict("stale fencing token")
        if int(run["revision"]) != int(expected_run_revision):
            raise LedgerConflict(
                f"stale run revision: expected {expected_run_revision}, current {run['revision']}"
            )
        return run

    def _adopt_intent_content(
        self,
        connection: sqlite3.Connection,
        *,
        intent: sqlite3.Row,
        expected_membership: set[tuple[str, str]],
        operation_id: str,
        timestamp: float,
    ) -> int:
        refs = connection.execute(
            """
            SELECT store_id, content_ref, role FROM provisional_content
            WHERE intent_id=? ORDER BY content_ref, role
            """,
            (intent["intent_id"],),
        ).fetchall()
        actual_membership = {(row["content_ref"], row["role"]) for row in refs}
        if actual_membership != expected_membership:
            missing = sorted(expected_membership - actual_membership)
            extra = sorted(actual_membership - expected_membership)
            raise LedgerConflict(
                f"provisional content closure mismatch; missing={missing}, extra={extra}"
            )
        owner = connection.execute(
            "SELECT revision FROM owners WHERE owner_id=?",
            (intent["owner_id"],),
        ).fetchone()
        if owner is None:
            raise LedgerNotFound(f"owner not found: {intent['owner_id']}")
        owner_revision = int(owner["revision"])
        if owner_revision != int(intent["base_owner_revision"]):
            raise LedgerConflict(
                "stale owner revision at intent commit: "
                f"expected {intent['base_owner_revision']}, current {owner_revision}"
            )
        unsafe = connection.execute(
            """
            SELECT provisional_content.content_ref
            FROM provisional_content
            JOIN content_objects USING(store_id, content_ref)
            WHERE provisional_content.intent_id=?
              AND (content_objects.sealed != 1 OR content_objects.verified != 1)
            ORDER BY provisional_content.content_ref
            """,
            (intent["intent_id"],),
        ).fetchall()
        if unsafe:
            raise LedgerConflict(
                "owner adoption requires sealed and verified content: "
                f"{[row['content_ref'] for row in unsafe]}"
            )
        existing_membership = {
            (row["content_ref"], row["role"])
            for row in connection.execute(
                "SELECT content_ref, role FROM owner_content WHERE owner_id=?",
                (intent["owner_id"],),
            )
        }
        new_refs = [
            row for row in refs
            if (row["content_ref"], row["role"]) not in existing_membership
        ]
        if new_refs:
            owner_revision += 1
            connection.executemany(
                """
                INSERT INTO owner_content(
                    owner_id, store_id, content_ref, role,
                    added_owner_revision, added_operation_id
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        intent["owner_id"],
                        row["store_id"],
                        row["content_ref"],
                        row["role"],
                        owner_revision,
                        operation_id,
                    )
                    for row in new_refs
                ],
            )
            connection.execute(
                "UPDATE owners SET revision=?, updated_at=? WHERE owner_id=?",
                (owner_revision, timestamp, intent["owner_id"]),
            )
        connection.execute(
            """
            UPDATE owner_intents
            SET state='committed', committed_operation_id=?, updated_at=?
            WHERE intent_id=?
            """,
            (operation_id, timestamp, intent["intent_id"]),
        )
        connection.execute("DELETE FROM provisional_content WHERE intent_id=?", (intent["intent_id"],))
        return owner_revision

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        entity_id: str,
        payload: JsonDict,
        operation_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO run_events(
                run_id, sequence, event_id, event_type,
                entity_id, payload_json, operation_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                f"{operation_id}:{sequence}",
                event_type,
                entity_id,
                _canonical_json(payload),
                operation_id,
            ),
        )

    @staticmethod
    def _operation_replay(
        connection: sqlite3.Connection,
        operation_id: str,
        request_hash: str,
    ) -> Optional[JsonDict]:
        row = connection.execute(
            "SELECT request_hash, result_json FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise LedgerConflict(f"operation id reused with a different request: {operation_id}")
        return json.loads(row["result_json"])

    @staticmethod
    def _record_operation(
        connection: sqlite3.Connection,
        operation_id: str,
        run_id: str,
        request_hash: str,
        result: JsonDict,
        timestamp: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operations(operation_id, run_id, request_hash, result_json, committed_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (operation_id, run_id, request_hash, _canonical_json(result), timestamp),
        )

    @staticmethod
    def _rows_with_json(connection: sqlite3.Connection, table: str, run_id: str) -> list[JsonDict]:
        allowed = {"candidates", "logical_trials", "attempts", "observations", "run_events"}
        if table not in allowed:
            raise ValueError(f"unsupported spike table: {table}")
        rows = []
        for row in connection.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY sequence", (run_id,)):
            payload = dict(row)
            for key in ("payload_json", "content_refs_json"):
                if key in payload:
                    payload[key.removesuffix("_json")] = json.loads(payload.pop(key))
            rows.append(payload)
        return rows


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash_json(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def candidate_ref_for(
    *,
    candidate_format: str,
    spec: JsonDict,
    content_refs: Sequence[str],
) -> str:
    """Hash the semantic candidate envelope used by the spike."""

    refs = tuple(sorted(content_refs))
    if not candidate_format:
        raise ValueError("candidate_format is required")
    if len(refs) != len(set(refs)) or any(not ref for ref in refs):
        raise ValueError("content_refs must contain unique nonempty strings")
    return _hash_json({
        "schema": "optpilot.candidate-spike.v1",
        "format": candidate_format,
        "spec": spec,
        "content_refs": list(refs),
    })


def _normalize_artifacts(artifacts: Sequence[JsonDict]) -> list[JsonDict]:
    required = {"artifact_id", "content_ref", "role"}
    normalized = []
    seen_ids = set()
    for value in artifacts:
        if set(value) != required:
            raise ValueError(f"artifact entries require exactly {sorted(required)}")
        artifact = {key: str(value[key]) for key in sorted(required)}
        if any(not artifact[key] for key in required):
            raise ValueError("artifact fields must be nonempty")
        if artifact["artifact_id"] in seen_ids:
            raise ValueError(f"duplicate artifact_id: {artifact['artifact_id']}")
        seen_ids.add(artifact["artifact_id"])
        normalized.append(artifact)
    return sorted(normalized, key=lambda item: item["artifact_id"])


def fake_content_ref(label: str) -> str:
    """Return a stable fake digest for spike fixtures; no bytes are stored here."""

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()
