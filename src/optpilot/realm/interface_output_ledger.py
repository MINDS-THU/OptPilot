"""RealmLedger mixin for fenced, durable interface-output capture."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ._validation import required_text
from .errors import (
    InterfaceOutputDrainPending,
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .interface_output_records import (
    INTERFACE_OUTPUT_SESSION_ROLE,
    InterfaceOutputGenerationRecord,
    InterfaceOutputGenerationState,
    InterfaceOutputGenerationStatusRecord,
    InterfaceOutputSessionRecord,
    InterfaceOutputSessionRetirementReceipt,
    InterfaceOutputSessionState,
)
from .interface_outputs import InterfaceOutputRecord, SealedInterfaceOutput
from .leases import LeaseRecord, LeaseState
from .owners import OwnerMembership, OwnerPermission
from .refs import SnapshotRef, request_digest
from .selections import (
    ResolvedSelection,
    ResolvedSelectionContent,
    SelectionEligibility,
    SelectionRef,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _identifier(value: object, label: str, *, max_bytes: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8", errors="strict")) > max_bytes
        or _IDENTIFIER_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{label} must be a bounded path-free identifier.")
    return value


def _positive_int(value: object, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} exceeds {maximum}.")
    return value


def _positive_ttl(value: object, label: str = "ttl_seconds") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"interface output {label} must be positive.")
    return float(value)


def _stable_identifier(prefix: str, operation_id: str) -> str:
    return f"{prefix}-{request_digest({'kind': prefix, 'operation_id': operation_id})[:32]}"


def _missing() -> RealmNotFound:
    return RealmNotFound("Entity not found.")


def _session_from_row(row: Mapping[str, Any] | None) -> InterfaceOutputSessionRecord:
    if row is None:
        raise _missing()
    try:
        return InterfaceOutputSessionRecord(
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            launch_id=row["launch_id"],
            session_lease_id=row["session_lease_id"],
            state=InterfaceOutputSessionState(row["state"]),
            current_revision=int(row["current_revision"]),
            max_generations=int(row["max_generations"]),
            max_logical_bytes=int(row["max_logical_bytes"]),
            created_txn_id=int(row["created_txn_id"]),
            updated_txn_id=int(row["updated_txn_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError(
            f"Persisted interface output session is invalid: {error}"
        ) from error


def _lease_from_row(row: Mapping[str, Any] | None) -> LeaseRecord:
    if row is None:
        raise _missing()
    try:
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            raise ValueError("lease metadata is not an object")
        return LeaseRecord(
            lease_id=row["lease_id"],
            owner_id=row["owner_id"],
            parent_lease_id=row["parent_lease_id"],
            lease_kind=row["lease_kind"],
            audience=row["audience"],
            holder_id=row["holder_id"],
            scope_key=row["scope_key"],
            fencing_token=int(row["fencing_token"]),
            heartbeat_revision=int(row["heartbeat_revision"]),
            state=LeaseState(row["state"]),
            expires_at=float(row["expires_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=metadata,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(
            f"Persisted interface output lease is invalid: {error}"
        ) from error


class InterfaceOutputLedgerMixin:
    """Typed domain operations mixed into :class:`RealmLedger`."""

    def create_interface_output_session(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        launch_id: str,
        ttl_seconds: float,
        max_generations: int = 256,
        max_logical_bytes: int = 2 * 1024**3,
        session_id: str | None = None,
        owner_id: str | None = None,
        lease_id: str | None = None,
    ) -> tuple[InterfaceOutputSessionRecord, LeaseRecord]:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        launch_id = _identifier(launch_id, "interface output launch id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        max_generations = _positive_int(
            max_generations, "interface output max_generations", maximum=256
        )
        max_logical_bytes = _positive_int(
            max_logical_bytes, "interface output max_logical_bytes"
        )
        session_id = _identifier(
            session_id or _stable_identifier("ios", operation_id),
            "interface output session id",
        )
        owner_id = _identifier(
            owner_id or _stable_identifier("ios-owner", operation_id),
            "interface output owner id",
        )
        lease_id = _identifier(
            lease_id or _stable_identifier("ios-lease", operation_id),
            "interface output session lease id",
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            if connection.execute(
                "SELECT 1 FROM interface_output_sessions "
                "WHERE session_id = ? OR owner_id = ? OR launch_id = ? LIMIT 1",
                (session_id, owner_id, launch_id),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Interface output session identity is already registered."
                )
            self._create_owner_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                owner_id=owner_id,
                owner_kind="interface-output-session",
                principal_id=actor_principal_id,
            )
            lease = self._acquire_lease_in_txn(
                connection,
                lease_id=lease_id,
                owner_id=owner_id,
                parent_lease_id=None,
                lease_kind="interface-output-session",
                audience="interface-supervisor",
                holder_id=actor_principal_id,
                scope_key=f"interface-output-session:{session_id}",
                ttl_seconds=ttl_seconds,
                metadata={"launch_id": launch_id, "session_id": session_id},
                now=now,
            )
            connection.execute(
                "INSERT INTO interface_output_sessions("
                "session_id, owner_id, launch_id, session_lease_id, state, "
                "current_revision, max_generations, max_logical_bytes, "
                "created_txn_id, updated_txn_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', 0, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    owner_id,
                    launch_id,
                    lease_id,
                    max_generations,
                    max_logical_bytes,
                    txn_id,
                    txn_id,
                    now,
                    now,
                ),
            )
            session = _session_from_row(
                connection.execute(
                    "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            )
            return {"session": session.to_dict(), "lease": lease.to_dict()}

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.session.create",
            request={
                "actor_principal_id": actor_principal_id,
                "launch_id": launch_id,
                "ttl_seconds": ttl_seconds,
                "max_generations": max_generations,
                "max_logical_bytes": max_logical_bytes,
                "session_id": session_id,
                "owner_id": owner_id,
                "lease_id": lease_id,
            },
            body=body,
        )
        return (
            InterfaceOutputSessionRecord.from_dict(receipt["session"]),
            LeaseRecord.from_dict(receipt["lease"]),
        )

    def read_interface_output_session(
        self,
        *,
        actor_principal_id: str,
        session_id: str,
        permission: OwnerPermission = OwnerPermission.METADATA_READ,
    ) -> InterfaceOutputSessionRecord:
        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        session_id = _identifier(session_id, "interface output session id")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=row["owner_id"],
                permission=permission,
            )
            result = _session_from_row(row)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_interface_output_session_handle(
        self,
        *,
        actor_principal_id: str,
        launch_id: str,
    ) -> tuple[InterfaceOutputSessionRecord, LeaseRecord]:
        """Recover the path-free session authority for supervisor reconciliation."""

        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        launch_id = _identifier(launch_id, "interface output launch id")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE launch_id = ?",
                (launch_id,),
            ).fetchone()
            if row is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=row["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            lease_row = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?",
                (row["session_lease_id"],),
            ).fetchone()
            result = (_session_from_row(row), _lease_from_row(lease_row))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_reclaimable_interface_output_session_handles(
        self,
        *,
        actor_principal_id: str,
        limit: int = 256,
    ) -> tuple[tuple[InterfaceOutputSessionRecord, LeaseRecord], ...]:
        """List expired/released sessions awaiting external cleanup and retirement."""

        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        limit = _positive_int(limit, "interface output reconciliation limit", maximum=256)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT session.*, lease.state AS lease_state, "
                "lease.expires_at AS lease_expires_at "
                "FROM interface_output_sessions session "
                "JOIN leases lease ON lease.lease_id = session.session_lease_id "
                "WHERE session.state = 'active' "
                "AND (lease.state <> 'active' OR lease.expires_at <= ?) "
                "ORDER BY session.updated_at, session.session_id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
            result: list[tuple[InterfaceOutputSessionRecord, LeaseRecord]] = []
            for row in rows:
                try:
                    self._authorize_owner(
                        connection,
                        actor_principal_id=actor_principal_id,
                        owner_id=row["owner_id"],
                        permission=OwnerPermission.ADMIN,
                    )
                except RealmNotFound:
                    continue
                lease_row = connection.execute(
                    "SELECT * FROM leases WHERE lease_id = ?",
                    (row["session_lease_id"],),
                ).fetchone()
                result.append((_session_from_row(row), _lease_from_row(lease_row)))
            connection.commit()
            return tuple(result)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def expire_stale_interface_output_capture(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
    ) -> InterfaceOutputGenerationStatusRecord | None:
        """Materialize an expired/deauthorized in-flight capture as failed."""

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        session_id = _identifier(session_id, "interface output session id")

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=session["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            row = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                (session["owner_id"], session_id),
            ).fetchone()
            if row is None:
                return {"expired": False, "id": None}
            status = InterfaceOutputGenerationStatusRecord.from_row(row)
            session_lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?",
                (session["session_lease_id"],),
            ).fetchone()
            if session_lease is None:
                raise RealmIntegrityError("Interface output session lease is missing.")
            session_current = (
                session_lease["state"] == LeaseState.ACTIVE.value
                and float(session_lease["expires_at"]) > now
            )
            attempt_current = (
                status.attempt_expires_at is not None
                and status.attempt_expires_at > now
            )
            if session_current and attempt_current:
                return {"expired": False, "id": status.output_id}
            self._close_interface_output_attempt_in_txn(
                connection,
                status=status,
                txn_id=txn_id,
                now=now,
                error_code=("attempt_expired" if session_current else "session_ended"),
            )
            if (
                session_lease["state"] == LeaseState.ACTIVE.value
                and float(session_lease["expires_at"]) <= now
            ):
                connection.execute(
                    "UPDATE leases SET state = 'expired', updated_at = ? "
                    "WHERE lease_id = ? AND state = 'active'",
                    (now, session["session_lease_id"]),
                )
                self._cascade_lease_descendants(
                    connection,
                    session["session_lease_id"],
                    LeaseState.EXPIRED,
                    now,
                )
            return {"expired": True, "id": status.output_id}

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.capture.expire",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
            },
            body=body,
        )
        if receipt.get("id") is None:
            return None
        return self.read_interface_output_status(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            output_id=str(receipt["id"]),
            permission=OwnerPermission.ADMIN,
        )

    def heartbeat_interface_output_session(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> LeaseRecord:
        ttl_seconds = _positive_ttl(ttl_seconds)

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            _session, lease = self._require_interface_output_fence(
                connection,
                actor_principal_id=actor_principal_id,
                session_id=session_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
                now=now,
            )
            expires_at = now + ttl_seconds
            connection.execute(
                "UPDATE leases SET heartbeat_revision = heartbeat_revision + 1, "
                "expires_at = ?, updated_at = ? WHERE lease_id = ?",
                (expires_at, now, lease_id),
            )
            return _lease_from_row(
                connection.execute(
                    "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
                ).fetchone()
            ).to_dict()

        return LeaseRecord.from_dict(
            self._operate(
                operation_id=required_text(operation_id, "operation_id", max_bytes=512),
                operation_kind="interface-output.session.heartbeat",
                request={
                    "actor_principal_id": actor_principal_id,
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "holder_id": holder_id,
                    "fencing_token": fencing_token,
                    "ttl_seconds": ttl_seconds,
                },
                body=body,
            )
        )

    def resume_expired_interface_output_session(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: float,
        replacement_lease_id: str | None = None,
    ) -> tuple[InterfaceOutputSessionRecord, LeaseRecord, LeaseRecord]:
        """Replace one exact expired writer lease with a higher fence.

        This transition is deliberately narrower than a heartbeat.  It never
        revives the old lease and cannot replace a released/revoked Stop fence.
        A caller must first finish or expire any in-flight capture attempt.
        """

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        session_id = _identifier(session_id, "interface output session id")
        lease_id = _identifier(lease_id, "interface output lease id")
        holder_id = required_text(holder_id, "interface output lease holder")
        fencing_token = _positive_int(
            fencing_token, "interface output fencing token"
        )
        ttl_seconds = _positive_ttl(ttl_seconds)
        replacement_lease_id = _identifier(
            replacement_lease_id
            or _stable_identifier("ios-lease", operation_id),
            "replacement interface output lease id",
        )
        if replacement_lease_id == lease_id:
            raise ValueError("Replacement interface output lease id must be new.")

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=session["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            if session["state"] != InterfaceOutputSessionState.ACTIVE.value:
                raise RealmConflict("Interface output session is not active.")
            previous = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            self._require_interface_output_lease_identity(
                session,
                previous,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
            )
            assert previous is not None
            if previous["state"] not in {
                LeaseState.ACTIVE.value,
                LeaseState.EXPIRED.value,
            }:
                raise RealmConflict(
                    "Only the exact time-expired session lease can resume."
                )
            if float(previous["expires_at"]) > now:
                raise RealmConflict(
                    "Interface output session lease has not expired."
                )
            if connection.execute(
                "SELECT 1 FROM interface_output_generations "
                "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                (session_id,),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Interface output capture cleanup is required before resume."
                )

            if previous["state"] == LeaseState.ACTIVE.value:
                connection.execute(
                    "UPDATE leases SET state = 'expired', updated_at = ? "
                    "WHERE lease_id = ? AND state = 'active'",
                    (now, lease_id),
                )
            self._cascade_lease_descendants(
                connection, lease_id, LeaseState.EXPIRED, now
            )
            self._expire_changes_for_leases(connection, (lease_id,), now)
            replacement = self._acquire_lease_in_txn(
                connection,
                lease_id=replacement_lease_id,
                owner_id=session["owner_id"],
                parent_lease_id=None,
                lease_kind="interface-output-session",
                audience="interface-supervisor",
                holder_id=holder_id,
                scope_key=f"interface-output-session:{session_id}",
                ttl_seconds=ttl_seconds,
                metadata={
                    "launch_id": session["launch_id"],
                    "session_id": session_id,
                    "resumed_from_lease_id": lease_id,
                },
                now=now,
            )
            connection.execute(
                "UPDATE interface_output_sessions SET session_lease_id = ?, "
                "updated_txn_id = ?, updated_at = ? WHERE session_id = ?",
                (replacement.lease_id, txn_id, now, session_id),
            )
            resumed = _session_from_row(
                connection.execute(
                    "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            )
            expired = _lease_from_row(
                connection.execute(
                    "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
                ).fetchone()
            )
            return {
                "session": resumed.to_dict(),
                "lease": replacement.to_dict(),
                "previous_lease": expired.to_dict(),
            }

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.session.resume",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
                "lease_id": lease_id,
                "holder_id": holder_id,
                "fencing_token": fencing_token,
                "ttl_seconds": ttl_seconds,
                "replacement_lease_id": replacement_lease_id,
            },
            body=body,
        )
        return (
            InterfaceOutputSessionRecord.from_dict(receipt["session"]),
            LeaseRecord.from_dict(receipt["lease"]),
            LeaseRecord.from_dict(receipt["previous_lease"]),
        )

    def release_interface_output_session_lease(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        require_drained: bool = False,
        final_records: Sequence[InterfaceOutputRecord] | None = None,
    ) -> LeaseRecord:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        if not isinstance(require_drained, bool):
            raise TypeError("require_drained must be a boolean.")
        if final_records is not None:
            if not require_drained:
                raise ValueError("final_records require drained capture semantics.")
            if isinstance(final_records, (str, bytes)) or not isinstance(
                final_records, Sequence
            ):
                raise TypeError("final_records must be a sequence or None.")
            normalized_final_records = tuple(final_records)
            if any(
                not isinstance(record, InterfaceOutputRecord)
                for record in normalized_final_records
            ):
                raise TypeError("final_records must contain interface output records.")
            if len(normalized_final_records) > 256:
                raise ValueError("final_records exceed the interface output limit.")
            if len(
                {record.output_id for record in normalized_final_records}
            ) != len(normalized_final_records):
                raise ValueError("final_records must contain unique output ids.")
        else:
            normalized_final_records = None

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=session["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            self._require_interface_output_lease_identity(
                session,
                lease,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
            )
            if require_drained:
                active = connection.execute(
                    "SELECT 1 FROM interface_output_generations "
                    "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise InterfaceOutputDrainPending(
                        "Interface output capture is still sealing."
                    )
                for final_record in normalized_final_records or ():
                    final_row = connection.execute(
                        "SELECT generation.*, ? AS owner_id "
                        "FROM interface_output_generations generation "
                        "WHERE session_id = ? AND output_id = ?",
                        (session["owner_id"], session_id, final_record.output_id),
                    ).fetchone()
                    if final_row is None:
                        raise InterfaceOutputDrainPending(
                            "Interface output final record coverage is incomplete."
                        )
                    final_status = InterfaceOutputGenerationStatusRecord.from_row(
                        final_row
                    )
                    if (
                        final_status.record != final_record
                        or final_status.record_digest
                        != request_digest(final_record.to_dict())
                    ):
                        # The caller classified against a stale concurrent
                        # view.  It must re-read the one final control snapshot
                        # against the newly durable declaration; closing here
                        # would either omit a valid record or bless an id reuse.
                        raise InterfaceOutputDrainPending(
                            "Interface output final record coverage changed."
                        )
            sealing_row = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                (session["owner_id"], session_id),
            ).fetchone()
            if sealing_row is not None:
                self._close_interface_output_attempt_in_txn(
                    connection,
                    status=InterfaceOutputGenerationStatusRecord.from_row(
                        sealing_row
                    ),
                    txn_id=txn_id,
                    now=now,
                    error_code="session_ended",
                )
            if lease["state"] == LeaseState.ACTIVE.value:
                state = (
                    LeaseState.EXPIRED
                    if float(lease["expires_at"]) <= now
                    else LeaseState.RELEASED
                )
                connection.execute(
                    "UPDATE leases SET state = ?, updated_at = ? WHERE lease_id = ?",
                    (state.value, now, lease_id),
                )
                self._cascade_lease_descendants(
                    connection,
                    lease_id,
                    LeaseState.EXPIRED
                    if state is LeaseState.EXPIRED
                    else LeaseState.REVOKED,
                    now,
                )
                self._expire_changes_for_leases(connection, (lease_id,), now)
            return _lease_from_row(
                connection.execute(
                    "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
                ).fetchone()
            ).to_dict()

        return LeaseRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="interface-output.session.release",
                request={
                    "actor_principal_id": actor_principal_id,
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "holder_id": holder_id,
                    "fencing_token": fencing_token,
                    "require_drained": require_drained,
                    "final_records": (
                        None
                        if normalized_final_records is None
                        else [
                            record.to_dict()
                            for record in normalized_final_records
                        ]
                    ),
                },
                body=body,
            )
        )

    def read_interface_output_status(
        self,
        *,
        actor_principal_id: str,
        session_id: str,
        output_id: str,
        permission: OwnerPermission = OwnerPermission.METADATA_READ,
    ) -> InterfaceOutputGenerationStatusRecord:
        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        session_id = _identifier(session_id, "interface output session id")
        output_id = _identifier(output_id, "interface output id", max_bytes=128)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT generation.*, session.owner_id "
                "FROM interface_output_generations generation "
                "JOIN interface_output_sessions session "
                "ON session.session_id = generation.session_id "
                "WHERE generation.session_id = ? AND generation.output_id = ?",
                (session_id, output_id),
            ).fetchone()
            if row is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=row["owner_id"],
                permission=permission,
            )
            result = InterfaceOutputGenerationStatusRecord.from_row(row)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_interface_output_statuses(
        self,
        *,
        actor_principal_id: str,
        session_id: str,
        limit: int = 256,
    ) -> tuple[InterfaceOutputGenerationStatusRecord, ...]:
        limit = _positive_int(limit, "interface output status limit", maximum=256)
        session = self.read_interface_output_session(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            permission=OwnerPermission.METADATA_READ,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=session.owner_id,
                permission=OwnerPermission.METADATA_READ,
            )
            rows = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? ORDER BY created_txn_id LIMIT ?",
                (session.owner_id, session.session_id, limit),
            ).fetchall()
            result = tuple(
                InterfaceOutputGenerationStatusRecord.from_row(row) for row in rows
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_interface_output_generation(
        self,
        *,
        actor_principal_id: str,
        session_id: str,
        output_id: str,
        permission: OwnerPermission = OwnerPermission.METADATA_READ,
    ) -> InterfaceOutputGenerationRecord:
        status = self.read_interface_output_status(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            output_id=output_id,
            permission=permission,
        )
        generation = status.ready_generation
        if generation is None:
            raise _missing()
        return generation

    def list_interface_output_generations(
        self,
        *,
        actor_principal_id: str,
        session_id: str,
        limit: int = 256,
    ) -> tuple[InterfaceOutputGenerationRecord, ...]:
        return tuple(
            generation
            for status in self.list_interface_output_statuses(
                actor_principal_id=actor_principal_id,
                session_id=session_id,
                limit=limit,
            )
            if (generation := status.ready_generation) is not None
        )

    def begin_interface_output_capture(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        record: InterfaceOutputRecord,
        attempt_ttl_seconds: float = 300,
        attempt_id: str | None = None,
        operation_prefix: str | None = None,
    ) -> InterfaceOutputGenerationStatusRecord:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        session_id = _identifier(session_id, "interface output session id")
        if not isinstance(record, InterfaceOutputRecord):
            raise TypeError("record must be an InterfaceOutputRecord.")
        attempt_ttl_seconds = _positive_ttl(
            attempt_ttl_seconds, "attempt_ttl_seconds"
        )
        attempt_id = _identifier(
            attempt_id or _stable_identifier("ioa", operation_id),
            "interface output attempt id",
        )
        operation_prefix = _identifier(
            operation_prefix or _stable_identifier("iop", operation_id),
            "interface output operation prefix",
        )
        record_digest = request_digest(record.to_dict())

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session, _lease = self._require_interface_output_fence(
                connection,
                actor_principal_id=actor_principal_id,
                session_id=session_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
                now=now,
            )
            if session["state"] != InterfaceOutputSessionState.ACTIVE.value:
                raise RealmConflict("Interface output session is not active.")
            existing_row = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? AND output_id = ?",
                (session["owner_id"], session_id, record.output_id),
            ).fetchone()
            if existing_row is not None:
                existing = InterfaceOutputGenerationStatusRecord.from_row(existing_row)
                if existing.record_digest != record_digest or existing.record != record:
                    raise RealmConflict(
                        "Interface output id already names a different record."
                    )
                if existing.state is InterfaceOutputGenerationState.READY:
                    return {
                        "id": record.output_id,
                        "attempt_id": existing.attempt_id,
                        "attempt_number": existing.attempt_number,
                        "ready": True,
                    }
                if (
                    existing.state is InterfaceOutputGenerationState.SEALING
                    and existing.attempt_expires_at is not None
                    and existing.attempt_expires_at > now
                ):
                    raise RealmConflict("Interface output capture is already in progress.")
                if existing.state is InterfaceOutputGenerationState.SEALING:
                    self._abandon_interface_output_change_in_txn(
                        connection,
                        status=existing,
                        now=now,
                    )
                attempt_number = existing.attempt_number + 1
            else:
                attempt_number = 1

            stale = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? AND state = 'sealing' "
                "AND attempt_expires_at <= ? LIMIT 1",
                (session["owner_id"], session_id, now),
            ).fetchone()
            if stale is not None:
                self._close_interface_output_attempt_in_txn(
                    connection,
                    status=InterfaceOutputGenerationStatusRecord.from_row(stale),
                    txn_id=txn_id,
                    now=now,
                    error_code="attempt_expired",
                )
            active = connection.execute(
                "SELECT 1 FROM interface_output_generations "
                "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                (session_id,),
            ).fetchone()
            if active is not None:
                raise RealmConflict("Another interface output capture is in progress.")
            if existing_row is None:
                registered_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM interface_output_generations "
                        "WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()["count"]
                )
                if registered_count >= int(session["max_generations"]):
                    raise RealmConflict(
                        "Interface output session generation limit reached."
                    )
            if connection.execute(
                "SELECT 1 FROM interface_output_capture_attempts "
                "WHERE attempt_id = ? OR operation_prefix = ? LIMIT 1",
                (attempt_id, operation_prefix),
            ).fetchone() is not None:
                raise RealmConflict("Interface output capture attempt identity was reused.")
            change_id, retention_lease_id, attempt_expires_at = (
                self._create_interface_output_change_in_txn(
                    connection,
                    session=session,
                    actor_principal_id=actor_principal_id,
                    attempt_id=attempt_id,
                    ttl_seconds=attempt_ttl_seconds,
                    now=now,
                )
            )
            connection.execute(
                "INSERT INTO interface_output_capture_attempts("
                "attempt_id, session_id, output_id, attempt_number, "
                "operation_prefix, change_id, retention_lease_id, "
                "created_txn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    session_id,
                    record.output_id,
                    attempt_number,
                    operation_prefix,
                    change_id,
                    retention_lease_id,
                    txn_id,
                    now,
                ),
            )
            if existing_row is None:
                connection.execute(
                    "INSERT INTO interface_output_generations("
                    "session_id, output_id, label, kind, root_handle, relative_path, "
                    "record_digest, state, attempt_number, attempt_id, "
                    "operation_prefix, change_id, retention_lease_id, "
                    "attempt_expires_at, error_code, session_revision, owner_revision, "
                    "store_id, content_ref, logical_bytes, committed_txn_id, "
                    "created_txn_id, updated_txn_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'sealing', 1, ?, ?, ?, ?, ?, "
                    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (
                        session_id,
                        record.output_id,
                        record.label,
                        record.kind.value,
                        record.root_handle,
                        record.relative_path,
                        record_digest,
                        attempt_id,
                        operation_prefix,
                        change_id,
                        retention_lease_id,
                        attempt_expires_at,
                        txn_id,
                        txn_id,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE interface_output_generations SET state = 'sealing', "
                    "attempt_number = ?, attempt_id = ?, operation_prefix = ?, "
                    "change_id = ?, retention_lease_id = ?, attempt_expires_at = ?, "
                    "error_code = NULL, updated_txn_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND output_id = ?",
                    (
                        attempt_number,
                        attempt_id,
                        operation_prefix,
                        change_id,
                        retention_lease_id,
                        attempt_expires_at,
                        txn_id,
                        now,
                        session_id,
                        record.output_id,
                    ),
                )
            return {
                "id": record.output_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "ready": False,
            }

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.capture.begin",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
                "lease_id": lease_id,
                "holder_id": holder_id,
                "fencing_token": fencing_token,
                "record": record.to_dict(),
                "attempt_id": attempt_id,
                "operation_prefix": operation_prefix,
                "attempt_ttl_seconds": attempt_ttl_seconds,
            },
            body=body,
        )
        status = self.read_interface_output_status(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            output_id=receipt["id"],
            permission=OwnerPermission.DERIVE,
        )
        if bool(receipt["ready"]):
            if status.state is not InterfaceOutputGenerationState.READY:
                raise RealmConflict("Ready interface output replay became stale.")
            return status
        if (
            status.attempt_id != receipt["attempt_id"]
            or status.attempt_number != int(receipt["attempt_number"])
        ):
            raise RealmConflict("Interface output capture begin replay is stale.")
        return status

    def fail_interface_output_capture(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        output_id: str,
        attempt_id: str,
        attempt_number: int,
        error_code: str,
    ) -> InterfaceOutputGenerationStatusRecord:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        session_id = _identifier(session_id, "interface output session id")
        output_id = _identifier(output_id, "interface output id", max_bytes=128)
        attempt_id = _identifier(attempt_id, "interface output attempt id")
        attempt_number = _positive_int(
            attempt_number, "interface output attempt number"
        )
        error_code = _identifier(
            error_code, "interface output failure code", max_bytes=128
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            self._require_interface_output_fence(
                connection,
                actor_principal_id=actor_principal_id,
                session_id=session_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
                now=now,
            )
            row = connection.execute(
                "SELECT generation.*, session.owner_id "
                "FROM interface_output_generations generation "
                "JOIN interface_output_sessions session USING(session_id) "
                "WHERE generation.session_id = ? AND generation.output_id = ?",
                (session_id, output_id),
            ).fetchone()
            if row is None:
                raise _missing()
            status = InterfaceOutputGenerationStatusRecord.from_row(row)
            if status.state is InterfaceOutputGenerationState.READY:
                return {"id": output_id}
            if (
                status.attempt_id != attempt_id
                or status.attempt_number != attempt_number
            ):
                raise RealmConflict("Interface output capture attempt is stale.")
            if status.state is InterfaceOutputGenerationState.SEALING:
                self._close_interface_output_attempt_in_txn(
                    connection,
                    status=status,
                    txn_id=txn_id,
                    now=now,
                    error_code=error_code,
                )
            elif status.error_code != error_code:
                raise RealmConflict("Interface output capture already failed differently.")
            return {"id": output_id}

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.capture.fail",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
                "lease_id": lease_id,
                "holder_id": holder_id,
                "fencing_token": fencing_token,
                "output_id": output_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "error_code": error_code,
            },
            body=body,
        )
        return self.read_interface_output_status(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            output_id=receipt["id"],
            permission=OwnerPermission.DERIVE,
        )

    def commit_interface_output_generation(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        output_id: str,
        attempt_id: str,
        attempt_number: int,
        change_id: str,
        sealed: SealedInterfaceOutput,
        store_id: str,
    ) -> InterfaceOutputGenerationRecord:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        session_id = _identifier(session_id, "interface output session id")
        output_id = _identifier(output_id, "interface output id", max_bytes=128)
        attempt_id = _identifier(attempt_id, "interface output attempt id")
        attempt_number = _positive_int(
            attempt_number, "interface output attempt number"
        )
        change_id = required_text(change_id, "interface output change id")
        store_id = _identifier(store_id, "interface output store id", max_bytes=128)
        if not isinstance(sealed, SealedInterfaceOutput):
            raise TypeError("sealed must be a SealedInterfaceOutput.")
        if sealed.record.output_id != output_id:
            raise ValueError("sealed output differs from the captured output id.")
        membership = OwnerMembership(
            store_id, sealed.content_ref, INTERFACE_OUTPUT_SESSION_ROLE
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session, _lease = self._require_interface_output_fence(
                connection,
                actor_principal_id=actor_principal_id,
                session_id=session_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
                now=now,
            )
            if session["state"] != InterfaceOutputSessionState.ACTIVE.value:
                raise RealmConflict("Interface output session is not active.")
            row = connection.execute(
                "SELECT generation.*, ? AS owner_id "
                "FROM interface_output_generations generation "
                "WHERE session_id = ? AND output_id = ?",
                (session["owner_id"], session_id, output_id),
            ).fetchone()
            if row is None:
                raise _missing()
            status = InterfaceOutputGenerationStatusRecord.from_row(row)
            if status.record != sealed.record:
                raise RealmConflict("Sealed interface output record changed.")
            if status.state is InterfaceOutputGenerationState.READY:
                generation = status.ready_generation
                assert generation is not None
                if generation.content_ref != sealed.content_ref:
                    raise RealmConflict(
                        "Interface output id already names different content."
                    )
                return {"id": output_id, "committed": True}
            if (
                status.state is not InterfaceOutputGenerationState.SEALING
                or status.attempt_id != attempt_id
                or status.attempt_number != attempt_number
                or status.change_id != change_id
            ):
                raise RealmConflict("Interface output capture attempt is stale.")
            if (
                status.attempt_expires_at is None
                or status.attempt_expires_at <= now
            ):
                self._close_interface_output_attempt_in_txn(
                    connection,
                    status=status,
                    txn_id=txn_id,
                    now=now,
                    error_code="attempt_expired",
                )
                return {
                    "id": output_id,
                    "committed": False,
                    "error_code": "attempt_expired",
                }
            content = connection.execute(
                "SELECT kind, logical_bytes, lifecycle_state, trust_state "
                "FROM content_objects WHERE store_id = ? AND content_ref = ?",
                (store_id, str(sealed.content_ref)),
            ).fetchone()
            expected_kind = "tree" if status.kind.value == "tree" else "blob"
            if (
                content is None
                or content["kind"] != expected_kind
                or content["lifecycle_state"] != "live"
                or content["trust_state"] != "verified_local"
            ):
                raise RealmConflict("Interface output content is not verified and live.")
            logical_bytes = int(content["logical_bytes"])
            retained_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(logical_bytes), 0) AS total "
                    "FROM interface_output_generations "
                    "WHERE session_id = ? AND state = 'ready'",
                    (session_id,),
                ).fetchone()["total"]
            )
            if retained_bytes + logical_bytes > int(session["max_logical_bytes"]):
                raise RealmConflict("Interface output session byte limit reached.")
            if int(session["current_revision"]) >= int(session["max_generations"]):
                raise RealmConflict("Interface output session generation limit reached.")
            change = connection.execute(
                "SELECT * FROM owner_transactions WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if (
                change is None
                or change["owner_id"] != session["owner_id"]
                or change["state"] != "active"
            ):
                raise _missing()
            additions = self._planned_change_memberships(connection, change_id)
            if additions != (membership,):
                raise RealmConflict(
                    "Interface output change must hold exactly its sealed root."
                )
            owner_commit = self._commit_owner_change_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                operation_id=operation_id,
                actor_principal_id=actor_principal_id,
                change_id=change_id,
                expected_owner_revision=int(change["base_owner_revision"]),
                additions=(membership,),
                removals=(),
            )
            session_revision = int(session["current_revision"]) + 1
            connection.execute(
                "UPDATE interface_output_sessions SET current_revision = ?, "
                "updated_txn_id = ?, updated_at = ? WHERE session_id = ?",
                (session_revision, txn_id, now, session_id),
            )
            connection.execute(
                "UPDATE interface_output_generations SET state = 'ready', "
                "attempt_expires_at = NULL, error_code = NULL, "
                "session_revision = ?, owner_revision = ?, store_id = ?, "
                "content_ref = ?, logical_bytes = ?, committed_txn_id = ?, "
                "updated_txn_id = ?, updated_at = ? "
                "WHERE session_id = ? AND output_id = ?",
                (
                    session_revision,
                    owner_commit.owner_revision,
                    store_id,
                    str(sealed.content_ref),
                    logical_bytes,
                    txn_id,
                    txn_id,
                    now,
                    session_id,
                    output_id,
                ),
            )
            return {"id": output_id, "committed": True}

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.generation.commit",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
                "lease_id": lease_id,
                "holder_id": holder_id,
                "fencing_token": fencing_token,
                "output_id": output_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "change_id": change_id,
                "record": sealed.record.to_dict(),
                "record_digest": request_digest(sealed.record.to_dict()),
                "content_ref": str(sealed.content_ref),
                "store_id": store_id,
            },
            body=body,
        )
        if not bool(receipt.get("committed")):
            raise RealmExpired("Interface output capture attempt expired.")
        return self.read_interface_output_generation(
            actor_principal_id=actor_principal_id,
            session_id=session_id,
            output_id=receipt["id"],
            permission=OwnerPermission.DERIVE,
        )

    def retire_interface_output_session(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        session_id: str,
    ) -> InterfaceOutputSessionRetirementReceipt:
        """Retire content after external runtime/output cleanup has been proved."""

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "interface output actor principal id"
        )
        session_id = _identifier(session_id, "interface output session id")

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            session = connection.execute(
                "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise _missing()
            owner = self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=session["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            self._require_active_owner(owner)
            if session["state"] != InterfaceOutputSessionState.ACTIVE.value:
                raise RealmConflict("Interface output session is not active.")
            session_lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?",
                (session["session_lease_id"],),
            ).fetchone()
            if session_lease is None:
                raise RealmIntegrityError("Interface output session lease is missing.")
            if (
                session_lease["state"] == LeaseState.ACTIVE.value
                and float(session_lease["expires_at"]) > now
            ):
                raise RealmConflict(
                    "Interface output runtime/session lease must end before retirement."
                )
            if connection.execute(
                "SELECT 1 FROM interface_output_generations "
                "WHERE session_id = ? AND state = 'sealing' LIMIT 1",
                (session_id,),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Interface output capture cleanup is required before retirement."
                )
            expired_ids = tuple(
                row["lease_id"]
                for row in connection.execute(
                    "SELECT lease_id FROM leases WHERE owner_id = ? "
                    "AND state = 'active' AND expires_at <= ? ORDER BY lease_id",
                    (session["owner_id"], now),
                )
            )
            for expired_id in expired_ids:
                connection.execute(
                    "UPDATE leases SET state = 'expired', updated_at = ? "
                    "WHERE lease_id = ? AND state = 'active'",
                    (now, expired_id),
                )
                self._cascade_lease_descendants(
                    connection, expired_id, LeaseState.EXPIRED, now
                )
            if expired_ids:
                self._expire_changes_for_leases(connection, expired_ids, now)
            active_consumer = connection.execute(
                "SELECT 1 FROM leases WHERE owner_id = ? AND state = 'active' "
                "AND expires_at > ? AND lease_id <> ? LIMIT 1",
                (session["owner_id"], now, session["session_lease_id"]),
            ).fetchone()
            if active_consumer is not None:
                raise RealmConflict("Interface output session still has an active consumer.")
            if connection.execute(
                "SELECT 1 FROM owner_edges WHERE "
                "(parent_owner_id = ? OR child_owner_id = ?) "
                "AND removed_revision IS NULL LIMIT 1",
                (session["owner_id"], session["owner_id"]),
            ).fetchone() is not None:
                raise RealmConflict("Interface output session still has an active owner link.")
            previous_revision = int(owner["revision"])
            revision = previous_revision + 1
            removals = self._active_owner_memberships(connection, session["owner_id"])
            connection.executemany(
                "UPDATE owner_memberships SET removed_revision = ?, removed_txn_id = ? "
                "WHERE owner_id = ? AND store_id = ? AND content_ref = ? AND role = ? "
                "AND removed_revision IS NULL",
                (
                    (
                        revision,
                        txn_id,
                        session["owner_id"],
                        item.store_id,
                        str(item.content_ref),
                        item.role,
                    )
                    for item in removals
                ),
            )
            connection.execute(
                "UPDATE owner_grants SET removed_revision = ? "
                "WHERE owner_id = ? AND removed_revision IS NULL",
                (revision, session["owner_id"]),
            )
            connection.execute(
                "UPDATE owners SET state = 'deleted', updated_at = ? WHERE owner_id = ?",
                (now, session["owner_id"]),
            )
            self._record_owner_revision(
                connection, session["owner_id"], revision, txn_id, now
            )
            connection.execute(
                "UPDATE interface_output_sessions SET state = 'retired', "
                "updated_txn_id = ?, updated_at = ? WHERE session_id = ?",
                (txn_id, now, session_id),
            )
            retired = _session_from_row(
                connection.execute(
                    "SELECT * FROM interface_output_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            )
            return InterfaceOutputSessionRetirementReceipt(
                session=retired,
                previous_owner_revision=previous_revision,
                owner_revision=revision,
                released_memberships=len(removals),
            ).to_dict()

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="interface-output.session.retire",
            request={
                "actor_principal_id": actor_principal_id,
                "session_id": session_id,
            },
            body=body,
        )
        return InterfaceOutputSessionRetirementReceipt(
            session=InterfaceOutputSessionRecord.from_dict(receipt["session"]),
            previous_owner_revision=int(receipt["previous_owner_revision"]),
            owner_revision=int(receipt["owner_revision"]),
            released_memberships=int(receipt["released_memberships"]),
        )

    def _create_interface_output_change_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        actor_principal_id: str,
        attempt_id: str,
        ttl_seconds: float,
        now: float,
    ) -> tuple[str, str, float]:
        """Create the exact provisional owner change bound to one attempt."""

        key = request_digest(
            {
                "schema": "optpilot.interface-output-attempt-change.v1",
                "session_id": session["session_id"],
                "attempt_id": attempt_id,
            }
        )
        change_id = f"ios-change-{key[:40]}"
        retention_lease_id = f"ios-retention-{key[:40]}"
        owner = connection.execute(
            "SELECT * FROM owners WHERE owner_id = ?", (session["owner_id"],)
        ).fetchone()
        if owner is None:
            raise _missing()
        self._require_active_owner(owner)
        lease = self._acquire_lease_in_txn(
            connection,
            lease_id=retention_lease_id,
            owner_id=session["owner_id"],
            parent_lease_id=None,
            lease_kind="owner-change-retention",
            audience="realm-ledger",
            holder_id=actor_principal_id,
            scope_key=f"owner-change:{change_id}",
            ttl_seconds=ttl_seconds,
            metadata={"change_id": change_id, "interface_attempt_id": attempt_id},
            now=now,
        )
        try:
            connection.execute(
                "INSERT INTO owner_transactions("
                "change_id, owner_id, base_owner_revision, retention_lease_id, "
                "state, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    change_id,
                    session["owner_id"],
                    int(owner["revision"]),
                    retention_lease_id,
                    lease.expires_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RealmConflict(
                "Interface output owner-change identity is already registered."
            ) from error
        return change_id, retention_lease_id, lease.expires_at

    def _close_interface_output_attempt_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        status: InterfaceOutputGenerationStatusRecord,
        txn_id: int,
        now: float,
        error_code: str,
    ) -> None:
        """Fail one current attempt and abandon its exact provisional capture."""

        if status.state is not InterfaceOutputGenerationState.SEALING:
            raise RealmConflict("Interface output capture is not sealing.")
        connection.execute(
            "UPDATE interface_output_generations SET state = 'failed', "
            "attempt_expires_at = NULL, error_code = ?, "
            "updated_txn_id = ?, updated_at = ? "
            "WHERE session_id = ? AND output_id = ? AND attempt_id = ?",
            (
                error_code,
                txn_id,
                now,
                status.session_id,
                status.output_id,
                status.attempt_id,
            ),
        )
        self._abandon_interface_output_change_in_txn(
            connection,
            status=status,
            now=now,
        )

    def _abandon_interface_output_change_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        status: InterfaceOutputGenerationStatusRecord,
        now: float,
    ) -> None:
        """Abandon only the provisional owner-change linked to an attempt."""

        change = connection.execute(
            "SELECT * FROM owner_transactions WHERE change_id = ?",
            (status.change_id,),
        ).fetchone()
        if change is None or change["retention_lease_id"] != status.retention_lease_id:
            raise RealmIntegrityError(
                "Interface output capture change linkage is malformed."
            )
        if change["state"] == "active":
            change_state = (
                "expired" if float(change["expires_at"]) <= now else "aborted"
            )
            connection.execute(
                "UPDATE owner_transactions SET state = ?, updated_at = ? "
                "WHERE change_id = ?",
                (change_state, now, status.change_id),
            )
        lease = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ?",
            (status.retention_lease_id,),
        ).fetchone()
        if lease is None:
            raise RealmIntegrityError(
                "Interface output capture retention lease is missing."
            )
        if lease["state"] == LeaseState.ACTIVE.value:
            lease_state = (
                LeaseState.EXPIRED.value
                if float(lease["expires_at"]) <= now
                else LeaseState.RELEASED.value
            )
            connection.execute(
                "UPDATE leases SET state = ?, updated_at = ? WHERE lease_id = ?",
                (lease_state, now, status.retention_lease_id),
            )
        self._abandon_incomplete_staging(connection, (status.change_id,), now)

    def _require_interface_output_fence(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        session_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        now: float,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        session_id = _identifier(session_id, "interface output session id")
        lease_id = _identifier(lease_id, "interface output lease id")
        holder_id = required_text(holder_id, "interface output lease holder")
        fencing_token = _positive_int(
            fencing_token, "interface output fencing token"
        )
        session = connection.execute(
            "SELECT * FROM interface_output_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise _missing()
        self._authorize_owner(
            connection,
            actor_principal_id=actor_principal_id,
            owner_id=session["owner_id"],
            permission=OwnerPermission.DERIVE,
        )
        lease = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        self._require_interface_output_lease_identity(
            session,
            lease,
            lease_id=lease_id,
            holder_id=holder_id,
            fencing_token=fencing_token,
        )
        self._require_current_lease(lease, holder_id, fencing_token, now)
        return session, lease

    @staticmethod
    def _require_interface_output_lease_identity(
        session: sqlite3.Row,
        lease: sqlite3.Row | None,
        *,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
    ) -> None:
        if (
            lease is None
            or session["session_lease_id"] != lease_id
            or lease["owner_id"] != session["owner_id"]
            or lease["parent_lease_id"] is not None
            or lease["lease_kind"] != "interface-output-session"
            or lease["audience"] != "interface-supervisor"
            or lease["scope_key"]
            != f"interface-output-session:{session['session_id']}"
            or lease["holder_id"] != holder_id
            or int(lease["fencing_token"]) != fencing_token
        ):
            raise RealmConflict("Interface output session lease fence is stale.")

    def _resolve_interface_output_selection_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        selection: SelectionRef,
        allowed_permissions: Sequence[OwnerPermission],
        content_read: bool = False,
    ) -> ResolvedSelection | ResolvedSelectionContent:
        resolution_type = ResolvedSelectionContent if content_read else ResolvedSelection
        if selection.source_kind != "interface-output":
            raise ValueError("selection is not an interface output selection.")
        session = connection.execute(
            "SELECT * FROM interface_output_sessions WHERE session_id = ?",
            (selection.source_id,),
        ).fetchone()
        if (
            session is None
            or session["owner_id"] != selection.source_owner_id
            or session["state"] != InterfaceOutputSessionState.ACTIVE.value
        ):
            raise _missing()
        owner = self._authorize_owner_any(
            connection,
            actor_principal_id=actor_principal_id,
            owner_id=session["owner_id"],
            permissions=allowed_permissions,
        )
        self._require_active_owner(owner)
        generation_row = connection.execute(
            "SELECT generation.*, ? AS owner_id "
            "FROM interface_output_generations generation "
            "WHERE session_id = ? AND output_id = ? AND state = 'ready'",
            (session["owner_id"], selection.source_id, selection.entity_id),
        ).fetchone()
        if generation_row is None:
            raise _missing()
        status = InterfaceOutputGenerationStatusRecord.from_row(generation_row)
        generation = status.ready_generation
        if generation is None:
            raise _missing()
        if (
            selection.kind != "artifact"
            or selection.source_revision != generation.session_revision
            or selection.owner_revision != generation.owner_revision
            or selection.entity_ref != str(generation.content_ref)
            or selection.context_digest != generation.record_digest
            or selection.relative_path is not None
            or generation.selection != selection
        ):
            raise _missing()
        current_owner_revision = int(owner["revision"])
        if not content_read and not isinstance(generation.content_ref, SnapshotRef):
            return ResolvedSelection(
                selection,
                current_owner_revision,
                SelectionEligibility.unsupported(
                    "file_artifact_not_tree",
                    "A retained file output is not an editable workspace tree.",
                ),
            )
        available = connection.execute(
            "SELECT 1 FROM owner_memberships membership "
            "JOIN content_objects content "
            "ON content.store_id = membership.store_id "
            "AND content.content_ref = membership.content_ref "
            "WHERE membership.owner_id = ? AND membership.store_id = ? "
            "AND membership.content_ref = ? AND membership.role = ? "
            "AND membership.removed_revision IS NULL "
            "AND content.lifecycle_state = 'live' "
            "AND content.trust_state = 'verified_local'",
            (
                session["owner_id"],
                generation.store_id,
                str(generation.content_ref),
                INTERFACE_OUTPUT_SESSION_ROLE,
            ),
        ).fetchone()
        if available is None:
            return resolution_type(
                selection,
                current_owner_revision,
                SelectionEligibility.unavailable(
                    "selection_content_unavailable",
                    "The selected interface output is no longer retained and verified.",
                ),
            )
        return resolution_type(
            selection,
            current_owner_revision,
            SelectionEligibility.ready(),
            generation.membership,
        )


__all__ = ["InterfaceOutputLedgerMixin"]
