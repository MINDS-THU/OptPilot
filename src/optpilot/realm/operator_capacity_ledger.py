"""RealmLedger mixin for fenced Operator Job capacity reservations."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any

from ._validation import positive_int, required_text
from .errors import (
    RealmCapacityUnavailable,
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .operator_capacity_records import (
    OperatorCapacityPoolRecord,
    OperatorCapacityPoolState,
    OperatorCapacityReservationRecord,
    OperatorCapacityReservationState,
    capacity_resources_digest,
    normalize_capacity_resources,
    operator_capacity_reservation_id,
)
from .operator_job_records import OperatorJobLaunchPlan, OperatorJobState
from .owners import OwnerPermission
from .refs import canonical_json_bytes


_ADMISSIBLE_JOB_STATES = frozenset(
    {
        OperatorJobState.QUEUED,
        OperatorJobState.STARTING,
        OperatorJobState.RUNNING,
        OperatorJobState.STOPPING,
    }
)


class OperatorCapacityLedgerMixin:
    """Typed Realm-wide capacity operations mixed into :class:`RealmLedger`.

    Acquisition accepts no caller-authored claim map.  It authorizes the
    Operator Job, loads its exact approved immutable plan, and accounts that
    plan's ``resource_claims`` under the write lock used by every reservation.
    """

    def ensure_operator_capacity_pool(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        pool_name: str,
        limits: Mapping[str, int],
    ) -> OperatorCapacityPoolRecord:
        """Create or reconcile one named pool against freshly observed limits.

        A changed limit increments the pool revision.  If any nonexpired
        reservation from the prior revision remains active, the new revision
        is durably ``blocked``: acquisition and renewal stay unavailable until
        those jobs release or expire and a later ensure operation makes the
        pool ``ready``.  Callers must check the returned state.
        """
        operation_id = required_text(operation_id, "operation_id")
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        pool_name = _logical_identifier(
            pool_name, "operator capacity pool name", max_bytes=128
        )
        limits_value = normalize_capacity_resources(
            limits,
            label="operator capacity pool limits",
            allow_zero=True,
        )
        limits_digest = capacity_resources_digest(limits_value)

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            if connection.execute(
                "SELECT 1 FROM principals WHERE principal_id = ?",
                (actor_principal_id,),
            ).fetchone() is None:
                raise RealmNotFound("Entity not found.")
            existing = connection.execute(
                "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
                (pool_name,),
            ).fetchone()
            if existing is not None:
                record = _capacity_pool_from_row(existing)
                if record.created_by_principal_id != actor_principal_id:
                    raise RealmNotFound("Entity not found.")
                self._expire_operator_capacity_reservations_in_txn(
                    connection,
                    pool_name=pool_name,
                    now=now,
                    txn_id=txn_id,
                )
                active = int(
                    connection.execute(
                        "SELECT count(*) FROM operator_capacity_reservations "
                        "WHERE pool_name = ? AND state = 'active' AND expires_at > ?",
                        (pool_name, now),
                    ).fetchone()[0]
                )
                limits_changed = record.limits_digest != limits_digest
                can_unblock = (
                    record.state is OperatorCapacityPoolState.BLOCKED
                    and active == 0
                )
                if not limits_changed and not can_unblock:
                    return record.to_dict()
                state = (
                    OperatorCapacityPoolState.BLOCKED
                    if active
                    else OperatorCapacityPoolState.READY
                )
                try:
                    updated = connection.execute(
                        "UPDATE operator_capacity_pools SET limits_json = ?, "
                        "limits_digest = ?, revision = revision + 1, state = ?, "
                        "updated_by_principal_id = ?, updated_txn_id = ?, "
                        "updated_at = ? WHERE pool_name = ? AND revision = ?",
                        (
                            canonical_json_bytes(dict(limits_value)).decode("utf-8"),
                            limits_digest,
                            state.value,
                            actor_principal_id,
                            txn_id,
                            now,
                            pool_name,
                            record.revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise RealmConflict(
                            "Operator capacity pool revision changed."
                        )
                except sqlite3.IntegrityError as error:
                    raise RealmIntegrityError(
                        "Operator capacity pool reconciliation failed its "
                        "durable integrity checks."
                    ) from error
                refreshed = connection.execute(
                    "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
                    (pool_name,),
                ).fetchone()
                return _capacity_pool_from_row(refreshed).to_dict()
            limits_json = canonical_json_bytes(dict(limits_value)).decode("utf-8")
            try:
                connection.execute(
                    "INSERT INTO operator_capacity_pools("
                    "pool_name, limits_json, limits_digest, revision, state, "
                    "created_by_principal_id, created_txn_id, created_at, "
                    "updated_by_principal_id, updated_txn_id, updated_at) "
                    "VALUES (?, ?, ?, 0, 'ready', ?, ?, ?, ?, ?, ?)",
                    (
                        pool_name,
                        limits_json,
                        limits_digest,
                        actor_principal_id,
                        txn_id,
                        now,
                        actor_principal_id,
                        txn_id,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO operator_capacity_fence_counters(pool_name, next_token) "
                    "VALUES (?, 1)",
                    (pool_name,),
                )
            except sqlite3.IntegrityError as error:
                raise RealmIntegrityError(
                    "Operator capacity pool failed its durable integrity checks."
                ) from error
            return OperatorCapacityPoolRecord(
                pool_name=pool_name,
                limits=limits_value,
                limits_digest=limits_digest,
                revision=0,
                state=OperatorCapacityPoolState.READY,
                created_by_principal_id=actor_principal_id,
                created_txn_id=txn_id,
                created_at=now,
                updated_by_principal_id=actor_principal_id,
                updated_txn_id=txn_id,
                updated_at=now,
            ).to_dict()

        return OperatorCapacityPoolRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-capacity.pool.ensure",
                request={
                    "actor_principal_id": actor_principal_id,
                    "limits": dict(limits_value),
                    "pool_name": pool_name,
                },
                body=body,
            )
        )

    def read_operator_capacity_pool(
        self, *, pool_name: str
    ) -> OperatorCapacityPoolRecord:
        pool_name = _logical_identifier(
            pool_name, "operator capacity pool name", max_bytes=128
        )
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
                (pool_name,),
            ).fetchone()
            if row is None:
                raise RealmNotFound("Entity not found.")
            return _capacity_pool_from_row(row)
        finally:
            connection.close()

    def acquire_operator_capacity_reservation(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        pool_name: str,
        job_id: str,
        holder_id: str,
        ttl_seconds: float,
    ) -> OperatorCapacityReservationRecord:
        operation_id = required_text(operation_id, "operation_id")
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        pool_name = _logical_identifier(
            pool_name, "operator capacity pool name", max_bytes=128
        )
        job_id = _logical_identifier(job_id, "operator capacity job id")
        holder_id = _logical_identifier(holder_id, "operator capacity holder id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        reservation_id = operator_capacity_reservation_id(pool_name, job_id)

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            job, plan = self._authorized_capacity_job_plan(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
            )
            if OperatorJobState(job["state"]) not in _ADMISSIBLE_JOB_STATES:
                raise RealmConflict("Operator Job is not admissible for execution.")
            approval = connection.execute(
                "SELECT 1 FROM operator_job_approvals "
                "WHERE job_id = ? AND plan_digest = ?",
                (job_id, plan.digest),
            ).fetchone()
            if approval is None:
                raise RealmConflict("Operator Job plan is not approved.")
            pool_row = connection.execute(
                "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
                (pool_name,),
            ).fetchone()
            if pool_row is None:
                raise RealmNotFound("Entity not found.")
            pool = _capacity_pool_from_row(pool_row)
            if pool.state is not OperatorCapacityPoolState.READY:
                raise RealmCapacityUnavailable(
                    "Operator capacity pool is blocked pending reconciliation."
                )
            if plan.backend_realm != pool_name:
                raise RealmConflict(
                    "Operator Job backend realm differs from its capacity pool."
                )
            claims = normalize_capacity_resources(
                plan.resource_claims,
                label="operator capacity claims",
                allow_zero=False,
            )
            claims_digest = capacity_resources_digest(claims)
            self._expire_operator_capacity_reservations_in_txn(
                connection,
                pool_name=pool_name,
                now=now,
                txn_id=txn_id,
            )
            existing = connection.execute(
                "SELECT * FROM operator_capacity_reservations "
                "WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                current = _capacity_reservation_from_row(existing)
                if (
                    current.pool_name != pool_name
                    or current.job_id != job_id
                    or current.plan_digest != plan.digest
                    or current.claims_digest != claims_digest
                    or dict(current.claims) != dict(claims)
                ):
                    raise RealmConflict(
                        "Operator capacity reservation differs from the approved plan."
                    )
                if current.state is OperatorCapacityReservationState.ACTIVE:
                    if current.pool_revision != pool.revision:
                        raise RealmCapacityUnavailable(
                            "Operator capacity reservation was fenced by a pool "
                            "reconfiguration."
                        )
                    if current.holder_id != holder_id:
                        raise RealmConflict(
                            "Operator capacity reservation has another active holder."
                        )
                    return current.to_dict()
                if current.state is OperatorCapacityReservationState.RELEASED:
                    launch = connection.execute(
                        "SELECT 1 FROM operator_job_launch_intents WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if (
                        OperatorJobState(job["state"]) is not OperatorJobState.QUEUED
                        or launch is not None
                    ):
                        raise RealmConflict(
                            "Released Operator capacity cannot be reacquired."
                        )

            _require_capacity_available(
                connection,
                pool=pool,
                requested_claims=claims,
                now=now,
            )
            fencing_token = _next_capacity_fencing_token(
                connection, pool_name=pool_name
            )
            expires_at = _expiry(now, ttl_seconds)
            try:
                if existing is None:
                    connection.execute(
                        "INSERT INTO operator_capacity_reservations("
                        "reservation_id, pool_name, pool_revision, job_id, "
                        "plan_digest, claims_json, claims_digest, holder_id, "
                        "fencing_token, generation, "
                        "heartbeat_revision, state, expires_at, "
                        "acquired_by_principal_id, acquired_txn_id, updated_txn_id, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "1, 0, 'active', ?, ?, ?, ?, ?, ?)",
                        (
                            reservation_id,
                            pool_name,
                            pool.revision,
                            job_id,
                            plan.digest,
                            canonical_json_bytes(dict(claims)).decode("utf-8"),
                            claims_digest,
                            holder_id,
                            fencing_token,
                            expires_at,
                            actor_principal_id,
                            txn_id,
                            txn_id,
                            now,
                            now,
                        ),
                    )
                else:
                    updated = connection.execute(
                        "UPDATE operator_capacity_reservations SET "
                        "pool_revision = ?, holder_id = ?, "
                        "fencing_token = ?, generation = generation + 1, "
                        "heartbeat_revision = 0, state = 'active', expires_at = ?, "
                        "acquired_by_principal_id = ?, acquired_txn_id = ?, "
                        "updated_txn_id = ?, updated_at = ? "
                        "WHERE reservation_id = ? AND state IN ('expired', 'released')",
                        (
                            pool.revision,
                            holder_id,
                            fencing_token,
                            expires_at,
                            actor_principal_id,
                            txn_id,
                            txn_id,
                            now,
                            reservation_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise RealmConflict(
                            "Operator capacity reservation lifecycle changed."
                        )
            except sqlite3.IntegrityError as error:
                raise RealmIntegrityError(
                    "Operator capacity reservation failed its durable integrity checks."
                ) from error
            row = connection.execute(
                "SELECT * FROM operator_capacity_reservations "
                "WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return _capacity_reservation_from_row(row).to_dict()

        return OperatorCapacityReservationRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-capacity.acquire",
                request={
                    "actor_principal_id": actor_principal_id,
                    "holder_id": holder_id,
                    "job_id": job_id,
                    "pool_name": pool_name,
                    "reservation_id": reservation_id,
                    "ttl_seconds": ttl_seconds,
                },
                body=body,
            )
        )

    def renew_operator_capacity_reservation(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        reservation_id: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> OperatorCapacityReservationRecord:
        operation_id = required_text(operation_id, "operation_id")
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        reservation_id = _logical_identifier(
            reservation_id, "operator capacity reservation id"
        )
        holder_id = _logical_identifier(holder_id, "operator capacity holder id")
        fencing_token = positive_int(
            fencing_token, "operator capacity fencing token"
        )
        ttl_seconds = _positive_ttl(ttl_seconds)

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_capacity_reservation_row(
                connection,
                actor_principal_id=actor_principal_id,
                reservation_id=reservation_id,
            )
            current = _capacity_reservation_from_row(row)
            _require_capacity_fence(current, holder_id, fencing_token)
            if current.state is OperatorCapacityReservationState.EXPIRED:
                return current.to_dict()
            _require_current_capacity_pool(
                connection, reservation=current
            )
            job_state = connection.execute(
                "SELECT state FROM operator_jobs WHERE job_id = ?",
                (current.job_id,),
            ).fetchone()
            if (
                job_state is None
                or OperatorJobState(job_state["state"])
                not in _ADMISSIBLE_JOB_STATES
            ):
                raise RealmConflict(
                    "Terminal Operator Job capacity cannot be renewed."
                )
            if current.state is not OperatorCapacityReservationState.ACTIVE:
                raise RealmConflict("Operator capacity reservation is not active.")
            if current.expires_at <= now:
                connection.execute(
                    "UPDATE operator_capacity_reservations SET state = 'expired', "
                    "updated_txn_id = ?, updated_at = ? WHERE reservation_id = ?",
                    (txn_id, now, reservation_id),
                )
            else:
                connection.execute(
                    "UPDATE operator_capacity_reservations SET "
                    "heartbeat_revision = heartbeat_revision + 1, expires_at = ?, "
                    "updated_txn_id = ?, updated_at = ? WHERE reservation_id = ?",
                    (_expiry(now, ttl_seconds), txn_id, now, reservation_id),
                )
            updated = connection.execute(
                "SELECT * FROM operator_capacity_reservations "
                "WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            return _capacity_reservation_from_row(updated).to_dict()

        result = OperatorCapacityReservationRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-capacity.renew",
                request={
                    "actor_principal_id": actor_principal_id,
                    "fencing_token": fencing_token,
                    "holder_id": holder_id,
                    "reservation_id": reservation_id,
                    "ttl_seconds": ttl_seconds,
                },
                body=body,
            )
        )
        if result.state is OperatorCapacityReservationState.EXPIRED:
            raise RealmExpired("Operator capacity reservation expired.")
        return result

    def validate_operator_capacity_reservation(
        self,
        *,
        actor_principal_id: str,
        reservation_id: str,
        holder_id: str,
        fencing_token: int,
    ) -> OperatorCapacityReservationRecord:
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        reservation_id = _logical_identifier(
            reservation_id, "operator capacity reservation id"
        )
        holder_id = _logical_identifier(holder_id, "operator capacity holder id")
        fencing_token = positive_int(
            fencing_token, "operator capacity fencing token"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._authorized_capacity_reservation_row(
                connection,
                actor_principal_id=actor_principal_id,
                reservation_id=reservation_id,
            )
            current = _capacity_reservation_from_row(row)
            _require_capacity_fence(current, holder_id, fencing_token)
            now = self._operator_capacity_now()
            if (
                current.state is OperatorCapacityReservationState.ACTIVE
                and current.expires_at <= now
            ):
                connection.execute(
                    "UPDATE operator_capacity_reservations SET state = 'expired', "
                    "updated_at = ? WHERE reservation_id = ?",
                    (now, reservation_id),
                )
                current = _capacity_reservation_from_row(
                    connection.execute(
                        "SELECT * FROM operator_capacity_reservations "
                        "WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                )
            if current.state is OperatorCapacityReservationState.ACTIVE:
                _require_current_capacity_pool(
                    connection, reservation=current
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if current.state is OperatorCapacityReservationState.EXPIRED:
            raise RealmExpired("Operator capacity reservation expired.")
        if current.state is not OperatorCapacityReservationState.ACTIVE:
            raise RealmConflict("Operator capacity reservation is not active.")
        return current

    def release_operator_capacity_reservation(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        reservation_id: str,
        holder_id: str,
        fencing_token: int,
    ) -> OperatorCapacityReservationRecord:
        operation_id = required_text(operation_id, "operation_id")
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        reservation_id = _logical_identifier(
            reservation_id, "operator capacity reservation id"
        )
        holder_id = _logical_identifier(holder_id, "operator capacity holder id")
        fencing_token = positive_int(
            fencing_token, "operator capacity fencing token"
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_capacity_reservation_row(
                connection,
                actor_principal_id=actor_principal_id,
                reservation_id=reservation_id,
            )
            current = _capacity_reservation_from_row(row)
            _require_capacity_fence(current, holder_id, fencing_token)
            if current.state is OperatorCapacityReservationState.ACTIVE:
                state = (
                    OperatorCapacityReservationState.EXPIRED
                    if current.expires_at <= now
                    else OperatorCapacityReservationState.RELEASED
                )
                connection.execute(
                    "UPDATE operator_capacity_reservations SET state = ?, "
                    "updated_txn_id = ?, updated_at = ? WHERE reservation_id = ?",
                    (state.value, txn_id, now, reservation_id),
                )
                current = _capacity_reservation_from_row(
                    connection.execute(
                        "SELECT * FROM operator_capacity_reservations "
                        "WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                )
            return current.to_dict()

        return OperatorCapacityReservationRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-capacity.release",
                request={
                    "actor_principal_id": actor_principal_id,
                    "fencing_token": fencing_token,
                    "holder_id": holder_id,
                    "reservation_id": reservation_id,
                },
                body=body,
            )
        )

    def read_operator_capacity_reservation(
        self,
        *,
        actor_principal_id: str,
        reservation_id: str,
    ) -> OperatorCapacityReservationRecord:
        actor_principal_id = _logical_identifier(
            actor_principal_id, "operator capacity actor principal id"
        )
        reservation_id = _logical_identifier(
            reservation_id, "operator capacity reservation id"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._authorized_capacity_reservation_row(
                connection,
                actor_principal_id=actor_principal_id,
                reservation_id=reservation_id,
            )
            record = _capacity_reservation_from_row(row)
            now = self._operator_capacity_now()
            if (
                record.state is OperatorCapacityReservationState.ACTIVE
                and record.expires_at <= now
            ):
                connection.execute(
                    "UPDATE operator_capacity_reservations SET state = 'expired', "
                    "updated_at = ? WHERE reservation_id = ?",
                    (now, reservation_id),
                )
                record = _capacity_reservation_from_row(
                    connection.execute(
                        "SELECT * FROM operator_capacity_reservations "
                        "WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                )
            connection.commit()
            return record
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _operator_capacity_now() -> float:
        # A tiny seam keeps expiry tests deterministic without allowing callers
        # to supply authoritative timestamps.
        import time

        return time.time()

    def _authorized_capacity_job_plan(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        job_id: str,
    ) -> tuple[sqlite3.Row, OperatorJobLaunchPlan]:
        job = self._authorized_operator_job_row(
            connection,
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            permission=OwnerPermission.DERIVE,
        )
        plan = _operator_job_plan_from_row(job)
        return job, plan

    def _authorized_capacity_reservation_row(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        reservation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operator_capacity_reservations "
            "WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise RealmNotFound("Entity not found.")
        _job, plan = self._authorized_capacity_job_plan(
            connection,
            actor_principal_id=actor_principal_id,
            job_id=row["job_id"],
        )
        record = _capacity_reservation_from_row(row)
        pool_row = connection.execute(
            "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
            (record.pool_name,),
        ).fetchone()
        if pool_row is None:
            raise RealmIntegrityError(
                "Persisted Operator capacity pool is missing."
            )
        pool = _capacity_pool_from_row(pool_row)
        if (
            record.plan_digest != plan.digest
            or record.pool_name != plan.backend_realm
            or dict(record.claims) != dict(plan.resource_claims)
        ):
            raise RealmIntegrityError(
                "Persisted Operator capacity differs from its immutable job plan."
            )
        return row

    @staticmethod
    def _expire_operator_capacity_reservations_in_txn(
        connection: sqlite3.Connection,
        *,
        pool_name: str,
        now: float,
        txn_id: int,
    ) -> None:
        connection.execute(
            "UPDATE operator_capacity_reservations SET state = 'expired', "
            "updated_txn_id = ?, updated_at = ? "
            "WHERE pool_name = ? AND state = 'active' AND expires_at <= ?",
            (txn_id, now, pool_name, now),
        )


def _logical_identifier(value: Any, label: str, *, max_bytes: int = 512) -> str:
    value = required_text(value, label, max_bytes=max_bytes)
    if (
        "/" in value
        or "\\" in value
        or value.startswith((".", "~"))
        or (len(value) >= 2 and value[1] == ":" and value[0].isalpha())
    ):
        raise ValueError(f"{label} must be a path-free logical identifier.")
    return value


def _positive_ttl(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ttl_seconds must be a positive finite number.")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("ttl_seconds must be a positive finite number.")
    return result


def _expiry(now: float, ttl_seconds: float) -> float:
    result = now + ttl_seconds
    if not math.isfinite(result):
        raise ValueError("ttl_seconds produces a non-finite expiry.")
    return result


def _canonical_json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
        encoded = canonical_json_bytes(decoded).decode("utf-8")
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError(f"Persisted {label} is invalid JSON.") from error
    if not isinstance(decoded, dict) or encoded != value:
        raise RealmIntegrityError(f"Persisted {label} is not a canonical object.")
    return decoded


def _capacity_pool_from_row(row: sqlite3.Row) -> OperatorCapacityPoolRecord:
    try:
        return OperatorCapacityPoolRecord(
            pool_name=row["pool_name"],
            limits=_canonical_json_object(
                row["limits_json"], "operator capacity limits"
            ),
            limits_digest=row["limits_digest"],
            revision=int(row["revision"]),
            state=OperatorCapacityPoolState(row["state"]),
            created_by_principal_id=row["created_by_principal_id"],
            created_txn_id=int(row["created_txn_id"]),
            created_at=float(row["created_at"]),
            updated_by_principal_id=row["updated_by_principal_id"],
            updated_txn_id=int(row["updated_txn_id"]),
            updated_at=float(row["updated_at"]),
        )
    except RealmIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError(
            "Persisted operator capacity pool is malformed."
        ) from error


def _capacity_reservation_from_row(
    row: sqlite3.Row,
) -> OperatorCapacityReservationRecord:
    try:
        return OperatorCapacityReservationRecord(
            reservation_id=row["reservation_id"],
            pool_name=row["pool_name"],
            pool_revision=int(row["pool_revision"]),
            job_id=row["job_id"],
            plan_digest=row["plan_digest"],
            claims=_canonical_json_object(
                row["claims_json"], "operator capacity claims"
            ),
            claims_digest=row["claims_digest"],
            holder_id=row["holder_id"],
            fencing_token=int(row["fencing_token"]),
            generation=int(row["generation"]),
            heartbeat_revision=int(row["heartbeat_revision"]),
            state=OperatorCapacityReservationState(row["state"]),
            expires_at=float(row["expires_at"]),
            acquired_by_principal_id=row["acquired_by_principal_id"],
            acquired_txn_id=int(row["acquired_txn_id"]),
            updated_txn_id=int(row["updated_txn_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
    except RealmIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError(
            "Persisted operator capacity reservation is malformed."
        ) from error


def _operator_job_plan_from_row(row: sqlite3.Row) -> OperatorJobLaunchPlan:
    plan = OperatorJobLaunchPlan.from_dict(
        _canonical_json_object(row["plan_json"], "operator job launch plan")
    )
    if plan.digest != row["plan_digest"]:
        raise RealmIntegrityError(
            "Persisted Operator Job plan digest is inconsistent."
        )
    return plan


def _next_capacity_fencing_token(
    connection: sqlite3.Connection, *, pool_name: str
) -> int:
    row = connection.execute(
        "SELECT next_token FROM operator_capacity_fence_counters "
        "WHERE pool_name = ?",
        (pool_name,),
    ).fetchone()
    if row is None:
        raise RealmIntegrityError("Operator capacity fence counter is missing.")
    token = positive_int(row["next_token"], "operator capacity next fence")
    if token >= (1 << 63) - 1:
        raise RealmIntegrityError("Operator capacity fence counter is exhausted.")
    updated = connection.execute(
        "UPDATE operator_capacity_fence_counters SET next_token = ? "
        "WHERE pool_name = ? AND next_token = ?",
        (token + 1, pool_name, token),
    )
    if updated.rowcount != 1:
        raise RealmConflict("Operator capacity fence counter changed.")
    return token


def _require_capacity_available(
    connection: sqlite3.Connection,
    *,
    pool: OperatorCapacityPoolRecord,
    requested_claims: Mapping[str, int],
    now: float,
) -> None:
    used: dict[str, int] = {}
    rows = connection.execute(
        "SELECT * FROM operator_capacity_reservations "
        "WHERE pool_name = ? AND state = 'active' AND expires_at > ?",
        (pool.pool_name, now),
    ).fetchall()
    for row in rows:
        reservation = _capacity_reservation_from_row(row)
        if reservation.pool_revision != pool.revision:
            raise RealmIntegrityError(
                "Ready Operator capacity pool retains a stale active reservation."
            )
        for name, amount in reservation.claims.items():
            used[name] = used.get(name, 0) + amount
            limit = pool.limits.get(name)
            if limit is None or used[name] > limit:
                raise RealmIntegrityError(
                    "Persisted active Operator reservations exceed pool capacity."
                )
    for name, amount in requested_claims.items():
        limit = pool.limits.get(name)
        if limit is None or used.get(name, 0) + amount > limit:
            raise RealmCapacityUnavailable(
                f"Operator capacity pool {pool.pool_name!r} cannot fit "
                f"the approved {name!r} claim."
            )


def _require_current_capacity_pool(
    connection: sqlite3.Connection,
    *,
    reservation: OperatorCapacityReservationRecord,
) -> OperatorCapacityPoolRecord:
    row = connection.execute(
        "SELECT * FROM operator_capacity_pools WHERE pool_name = ?",
        (reservation.pool_name,),
    ).fetchone()
    if row is None:
        raise RealmIntegrityError("Persisted Operator capacity pool is missing.")
    pool = _capacity_pool_from_row(row)
    if (
        pool.state is not OperatorCapacityPoolState.READY
        or reservation.pool_revision != pool.revision
    ):
        raise RealmCapacityUnavailable(
            "Operator capacity reservation was fenced by pool reconfiguration."
        )
    if any(
        pool.limits.get(name, -1) < amount
        for name, amount in reservation.claims.items()
    ):
        raise RealmCapacityUnavailable(
            "Operator capacity reservation exceeds the current pool limits."
        )
    return pool


def _require_capacity_fence(
    record: OperatorCapacityReservationRecord,
    holder_id: str,
    fencing_token: int,
) -> None:
    if record.holder_id != holder_id or record.fencing_token != fencing_token:
        raise RealmConflict(
            "Operator capacity holder or fencing token is stale."
        )


__all__ = ["OperatorCapacityLedgerMixin"]
