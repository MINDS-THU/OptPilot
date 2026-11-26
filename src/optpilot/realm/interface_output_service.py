"""Supervisor-facing orchestration for durable interface output generations."""

from __future__ import annotations

import uuid
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._validation import required_text
from .errors import ContentRejected, RealmConflict, RealmExpired, RealmNotFound
from .interface_output_records import (
    INTERFACE_OUTPUT_SESSION_ROLE,
    InterfaceOutputGenerationRecord,
    InterfaceOutputGenerationState,
    InterfaceOutputGenerationStatusRecord,
    InterfaceOutputSessionRecord,
    InterfaceOutputSessionRetirementReceipt,
    InterfaceOutputSessionState,
)
from .interface_outputs import (
    INTERFACE_OUTPUT_SCHEMA,
    InterfaceOutputRecord,
    InterfaceOutputRecordRejection,
    list_interface_output_tree_paths,
    read_interface_output_records,
    seal_interface_output_generation,
)
from .leases import LeaseRecord, LeaseState
from .ledger import RealmLedger
from .manifests import SealLimits
from .owners import OwnerMembership, OwnerPermission
from .service import RealmContentService
from .refs import request_digest


DEFAULT_INTERFACE_OUTPUT_LIMITS = SealLimits(
    max_entries=10_000,
    max_depth=48,
    max_total_bytes=1024**3,
    max_file_bytes=256 * 1024**2,
    max_path_bytes=4096,
    max_component_bytes=255,
)


@dataclass(frozen=True)
class InterfaceOutputSessionHandle:
    """Path-free authority returned only to the launch supervisor."""

    session: InterfaceOutputSessionRecord
    lease: LeaseRecord

    def __post_init__(self) -> None:
        if self.lease.lease_id != self.session.session_lease_id:
            raise ValueError("interface output session lease differs from its session.")
        if self.lease.owner_id != self.session.owner_id:
            raise ValueError("interface output session lease belongs to another owner.")
        if self.lease.lease_kind != "interface-output-session":
            raise ValueError("interface output session lease has the wrong kind.")
        if self.lease.audience != "interface-supervisor":
            raise ValueError("interface output session lease has the wrong audience.")
        if (
            self.lease.scope_key
            != f"interface-output-session:{self.session.session_id}"
        ):
            raise ValueError("interface output session lease has the wrong scope.")

    def to_dict(self) -> dict[str, object]:
        return {"session": self.session.to_dict(), "lease": self.lease.to_dict()}


@dataclass(frozen=True)
class InterfaceOutputCapturePass:
    """One control-file read classified against the durable session."""

    accepted_records: tuple[InterfaceOutputRecord, ...]
    generations: tuple[InterfaceOutputGenerationRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.accepted_records, tuple) or any(
            not isinstance(record, InterfaceOutputRecord)
            for record in self.accepted_records
        ):
            raise TypeError("accepted_records must be interface output records.")
        if not isinstance(self.generations, tuple) or any(
            not isinstance(generation, InterfaceOutputGenerationRecord)
            for generation in self.generations
        ):
            raise TypeError("generations must be interface output generations.")


class RealmInterfaceOutputSessionService:
    """Capture and retain launch outputs under one exact, fenced writer lease.

    Filesystem roots enter only at ``capture_control_file``. Every successful
    return is a persisted, path-free generation whose immutable content is
    already attached to the session owner. Failed captures remain visible and
    retryable with a fresh attempt identity.
    """

    def __init__(
        self,
        ledger: RealmLedger,
        content: RealmContentService,
        *,
        actor_principal_id: str,
        store_id: str,
        limits: SealLimits = DEFAULT_INTERFACE_OUTPUT_LIMITS,
        max_session_bytes: int = 2 * 1024**3,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(content, RealmContentService):
            raise TypeError("content must be a RealmContentService.")
        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id is required.")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id is required.")
        if not isinstance(limits, SealLimits):
            raise TypeError("limits must be SealLimits.")
        if (
            isinstance(max_session_bytes, bool)
            or not isinstance(max_session_bytes, int)
            or max_session_bytes <= 0
        ):
            raise ValueError("max_session_bytes must be a positive integer.")
        self._ledger = ledger
        self._content = content
        self._actor = actor_principal_id
        self._store_id = store_id
        self._limits = limits
        self._max_session_bytes = max_session_bytes

    def create_session(
        self,
        *,
        operation_id: str,
        launch_id: str,
        ttl_seconds: float = 3600,
        max_generations: int = 256,
    ) -> InterfaceOutputSessionHandle:
        session, lease = self._ledger.create_interface_output_session(
            operation_id=operation_id,
            actor_principal_id=self._actor,
            launch_id=launch_id,
            ttl_seconds=ttl_seconds,
            max_generations=max_generations,
            max_logical_bytes=self._max_session_bytes,
        )
        return InterfaceOutputSessionHandle(session, lease)

    def recover_session(self, *, launch_id: str) -> InterfaceOutputSessionHandle:
        session, lease = self._ledger.read_interface_output_session_handle(
            actor_principal_id=self._actor,
            launch_id=launch_id,
        )
        return InterfaceOutputSessionHandle(session, lease)

    def reclaimable_sessions(self) -> tuple[InterfaceOutputSessionHandle, ...]:
        """Return sessions whose runtime lease ended and need cleanup proof."""

        return tuple(
            InterfaceOutputSessionHandle(session, lease)
            for session, lease in self._ledger.list_reclaimable_interface_output_session_handles(
                actor_principal_id=self._actor
            )
        )

    def heartbeat_session(
        self,
        *,
        operation_id: str,
        handle: InterfaceOutputSessionHandle,
        ttl_seconds: float = 3600,
    ) -> InterfaceOutputSessionHandle:
        self._require_handle(handle)
        lease = self._ledger.heartbeat_interface_output_session(
            operation_id=operation_id,
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            ttl_seconds=ttl_seconds,
        )
        session = self._ledger.read_interface_output_session(
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            permission=OwnerPermission.DERIVE,
        )
        return InterfaceOutputSessionHandle(session, lease)

    def resume_expired_session(
        self,
        *,
        operation_id: str,
        handle: InterfaceOutputSessionHandle,
        ttl_seconds: float = 3600,
    ) -> InterfaceOutputSessionHandle:
        """Advance the fence for one exact time-expired live session.

        Any capture suspended with the old lease is first materialized as
        failed so it cannot remain an invisible blocker after host resume.
        """

        self._require_handle(handle)
        operation_id = required_text(
            operation_id, "operation_id", max_bytes=512
        )
        cleanup_key = request_digest(
            {
                "schema": "optpilot.interface-output-session-resume-cleanup.v1",
                "operation_id": operation_id,
            }
        )
        self._ledger.expire_stale_interface_output_capture(
            operation_id=f"ios-resume-expire-{cleanup_key[:40]}",
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
        )
        session, lease, previous = (
            self._ledger.resume_expired_interface_output_session(
                operation_id=operation_id,
                actor_principal_id=self._actor,
                session_id=handle.session.session_id,
                lease_id=handle.lease.lease_id,
                holder_id=handle.lease.holder_id,
                fencing_token=handle.lease.fencing_token,
                ttl_seconds=ttl_seconds,
            )
        )
        if (
            previous.lease_id != handle.lease.lease_id
            or previous.holder_id != handle.lease.holder_id
            or previous.fencing_token != handle.lease.fencing_token
            or previous.state is not LeaseState.EXPIRED
        ):
            raise RealmConflict(
                "Interface output resume receipt does not match the stale fence."
            )
        return InterfaceOutputSessionHandle(session, lease)

    def list_statuses(
        self, *, handle: InterfaceOutputSessionHandle
    ) -> tuple[InterfaceOutputGenerationStatusRecord, ...]:
        """Return the path-free durable generations visible to this supervisor."""

        self._require_handle(handle)
        return self._ledger.list_interface_output_statuses(
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
        )

    def list_tree_selections(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        root_path: Path,
        max_entries: int = 512,
        max_depth: int = 24,
    ) -> tuple[str, ...]:
        """List portable tree choices under one supervisor-granted root.

        The returned strings are advisory launch-relative choices. This method
        reauthorizes the exact live session and performs bounded no-follow
        discovery; capture reopens the chosen path independently.
        """

        self._require_live_handle(handle)
        return list_interface_output_tree_paths(
            root_path,
            max_entries=max_entries,
            max_depth=max_depth,
        )

    def capture_tree_selection(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        label: str,
        relative_path: str,
        root_handle: str,
        root_path: Path,
    ) -> InterfaceOutputGenerationStatusRecord:
        """Declare and capture one supervisor-selected tree generation.

        Callers provide only a trusted launch root handle/path pair plus the
        user's label and canonical relative choice. The durable id is minted
        from the normalized declaration so retries are idempotent. Capture then
        enters the same status, retry, selection, and Keep lifecycle as records
        emitted through the interface control file.
        """

        self._require_live_handle(handle)
        provisional = InterfaceOutputRecord.from_dict(
            {
                "schema_version": INTERFACE_OUTPUT_SCHEMA,
                "id": "selected-tree",
                "label": label,
                "kind": "tree",
                "root": root_handle,
                "path": relative_path,
            }
        )
        output_id = (
            "selected-tree-"
            + request_digest(
                {
                    "format": "optpilot.interface-output-tree-selection.v1",
                    "label": provisional.label,
                    "root": provisional.root_handle,
                    "path": provisional.relative_path,
                }
            )[:24]
        )
        record = InterfaceOutputRecord(
            output_id=output_id,
            label=provisional.label,
            kind=provisional.kind,
            root_handle=provisional.root_handle,
            relative_path=provisional.relative_path,
        )
        self.capture_records(
            handle=handle,
            records=(record,),
            root_handles={root_handle: Path(root_path)},
        )
        try:
            return self._ledger.read_interface_output_status(
                actor_principal_id=self._actor,
                session_id=handle.session.session_id,
                output_id=output_id,
                permission=OwnerPermission.DERIVE,
            )
        except RealmNotFound as error:
            raise RealmConflict(
                "Interface output capture authority changed before the tree "
                "selection could be recorded."
            ) from error

    def capture_control_file(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        control_file: Path,
        root_handles: Mapping[str, Path],
        retry_failed: bool = False,
        rejected_records: list[InterfaceOutputRecordRejection] | None = None,
    ) -> tuple[InterfaceOutputGenerationRecord, ...]:
        """Capture newly declared generations without implicitly retrying failures.

        Watchers may safely call this method whenever the control file changes.
        A failed generation remains failed until a user/supervisor explicitly
        requests a retry, preventing one bad declaration from becoming a tight
        retry loop. Ready generations are returned idempotently.
        """

        record_lines: dict[str, int] = {}
        records = self.read_control_file(
            control_file,
            rejected_records=rejected_records,
            record_lines=record_lines,
        )
        return self.capture_records(
            handle=handle,
            records=records,
            root_handles=root_handles,
            retry_failed=retry_failed,
            rejected_records=rejected_records,
            record_lines=record_lines,
        ).generations

    @staticmethod
    def read_control_file(
        control_file: Path,
        *,
        rejected_records: list[InterfaceOutputRecordRejection] | None = None,
        record_lines: dict[str, int] | None = None,
    ) -> tuple[InterfaceOutputRecord, ...]:
        """Read one bounded no-follow snapshot of the control file."""

        return read_interface_output_records(
            control_file,
            max_records=256,
            tolerate_invalid_records=True,
            rejected_records=rejected_records,
            accepted_record_lines=record_lines,
        )

    def capture_records(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        records: Sequence[InterfaceOutputRecord],
        root_handles: Mapping[str, Path],
        retry_failed: bool = False,
        rejected_records: list[InterfaceOutputRecordRejection] | None = None,
        record_lines: Mapping[str, int] | None = None,
    ) -> InterfaceOutputCapturePass:
        """Capture one read's records and return the durably accepted subset."""

        self._require_handle(handle)
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise TypeError("records must be a sequence of interface output records.")
        normalized = tuple(records)
        if any(not isinstance(record, InterfaceOutputRecord) for record in normalized):
            raise TypeError("records must contain interface output records.")
        if len({record.output_id for record in normalized}) != len(normalized):
            raise ValueError("records must contain unique interface output ids.")
        if not isinstance(retry_failed, bool):
            raise TypeError("retry_failed must be a boolean.")
        if rejected_records is not None and not isinstance(rejected_records, list):
            raise TypeError("rejected_records must be a list or None.")
        if record_lines is not None and not isinstance(record_lines, Mapping):
            raise TypeError("record_lines must be a mapping or None.")

        accepted: list[InterfaceOutputRecord] = []
        captured: list[InterfaceOutputGenerationRecord] = []
        for index, record in enumerate(normalized, start=1):
            try:
                try:
                    existing = self._ledger.read_interface_output_status(
                        actor_principal_id=self._actor,
                        session_id=handle.session.session_id,
                        output_id=record.output_id,
                        permission=OwnerPermission.DERIVE,
                    )
                except RealmNotFound:
                    existing = None
                if existing is not None:
                    if existing.record != record:
                        line_number = index
                        if record_lines is not None:
                            candidate = record_lines.get(record.output_id, index)
                            if (
                                not isinstance(candidate, bool)
                                and isinstance(candidate, int)
                                and candidate > 0
                            ):
                                line_number = candidate
                        if rejected_records is not None:
                            rejected_records.append(
                                InterfaceOutputRecordRejection(
                                    line_number,
                                    "conflicting_output_id",
                                )
                            )
                        continue
                    accepted.append(record)
                    if existing.state is InterfaceOutputGenerationState.READY:
                        generation = existing.ready_generation
                        assert generation is not None
                        captured.append(generation)
                        continue
                    if existing.state is InterfaceOutputGenerationState.SEALING:
                        captured.append(
                            self.resume_generation(
                                handle=handle,
                                output_id=record.output_id,
                                root_handles=root_handles,
                            )
                        )
                        continue
                    if not retry_failed:
                        continue
                else:
                    accepted.append(record)
                captured.append(
                    self.capture_generation(
                        handle=handle,
                        record=record,
                        root_handles=root_handles,
                    )
                )
            except (ContentRejected, OSError, RealmConflict, RealmExpired):
                # Failure state is durable and path-free. Continue so one bad
                # declaration cannot starve later independent generations.
                continue
        return InterfaceOutputCapturePass(tuple(accepted), tuple(captured))

    def capture_generation(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        record: InterfaceOutputRecord,
        root_handles: Mapping[str, Path],
    ) -> InterfaceOutputGenerationRecord:
        self._require_handle(handle)
        if not isinstance(record, InterfaceOutputRecord):
            raise TypeError("record must be an InterfaceOutputRecord.")
        nonce = uuid.uuid4().hex
        operation_prefix = f"ioattempt-{nonce}"
        status = self._ledger.begin_interface_output_capture(
            operation_id=f"{operation_prefix}-begin",
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            record=record,
            attempt_id=f"ioa-{nonce}",
            operation_prefix=operation_prefix,
        )
        if status.state is InterfaceOutputGenerationState.READY:
            generation = status.ready_generation
            assert generation is not None
            return generation

        return self._execute_capture_attempt(
            handle=handle,
            status=status,
            root_handles=root_handles,
        )

    def resume_generation(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        output_id: str,
        root_handles: Mapping[str, Path],
    ) -> InterfaceOutputGenerationRecord:
        """Adopt one persisted in-flight attempt after supervisor restart."""

        self._require_handle(handle)
        status = self._ledger.read_interface_output_status(
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            output_id=output_id,
            permission=OwnerPermission.DERIVE,
        )
        if status.state is InterfaceOutputGenerationState.READY:
            generation = status.ready_generation
            assert generation is not None
            return generation
        if status.state is not InterfaceOutputGenerationState.SEALING:
            raise RealmConflict("Interface output generation has no capture to resume.")
        return self._execute_capture_attempt(
            handle=handle,
            status=status,
            root_handles=root_handles,
        )

    def _execute_capture_attempt(
        self,
        *,
        handle: InterfaceOutputSessionHandle,
        status: InterfaceOutputGenerationStatusRecord,
        root_handles: Mapping[str, Path],
    ) -> InterfaceOutputGenerationRecord:
        record = status.record
        operation_prefix = status.operation_prefix

        try:
            capture = self._content.capture(
                actor_principal_id=self._actor,
                change_id=status.change_id,
                store_id=self._store_id,
            )
            sealed = seal_interface_output_generation(
                capture,
                record=record,
                root_handles=root_handles,
                operation_id=(
                    f"{operation_prefix}-seal" if record.kind.value == "tree" else None
                ),
                limits=self._limits,
            )
            membership = OwnerMembership(
                self._store_id,
                sealed.content_ref,
                INTERFACE_OUTPUT_SESSION_ROLE,
            )
            self._ledger.hold_owner_content(
                operation_id=f"{operation_prefix}-hold",
                actor_principal_id=self._actor,
                change_id=status.change_id,
                memberships=(membership,),
            )
            return self._ledger.commit_interface_output_generation(
                operation_id=f"{operation_prefix}-commit",
                actor_principal_id=self._actor,
                session_id=handle.session.session_id,
                lease_id=handle.lease.lease_id,
                holder_id=handle.lease.holder_id,
                fencing_token=handle.lease.fencing_token,
                output_id=record.output_id,
                attempt_id=status.attempt_id,
                attempt_number=status.attempt_number,
                change_id=status.change_id,
                sealed=sealed,
                store_id=self._store_id,
            )
        except BaseException as error:
            try:
                self._ledger.fail_interface_output_capture(
                    operation_id=f"{operation_prefix}-fail",
                    actor_principal_id=self._actor,
                    session_id=handle.session.session_id,
                    lease_id=handle.lease.lease_id,
                    holder_id=handle.lease.holder_id,
                    fencing_token=handle.lease.fencing_token,
                    output_id=record.output_id,
                    attempt_id=status.attempt_id,
                    attempt_number=status.attempt_number,
                    error_code=_capture_error_code(error),
                )
            except (RealmConflict, RealmExpired, RealmNotFound):
                pass
            raise

    def retire_session(
        self,
        *,
        operation_id: str,
        handle: InterfaceOutputSessionHandle,
    ) -> InterfaceOutputSessionRetirementReceipt:
        """Release ownership only after the caller proves runtime/output cleanup."""

        self._require_handle(handle)
        self.close_capture(
            operation_id=f"{operation_id}-release-lease",
            handle=handle,
        )
        return self._ledger.retire_interface_output_session(
            operation_id=f"{operation_id}-retire",
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
        )

    def close_capture(
        self,
        *,
        operation_id: str,
        handle: InterfaceOutputSessionHandle,
        require_drained: bool = False,
        final_records: Sequence[InterfaceOutputRecord] | None = None,
    ) -> InterfaceOutputSessionHandle:
        """Durably fence new capture while retaining ready generations.

        Terminal adopters call this after their final capture/drain and before
        deriving another owner.  Releasing the exact writer lease linearizes
        against concurrent retries.  Ordinary teardown materializes an
        in-flight attempt failed.  Terminal adopters instead set
        ``require_drained`` so the same transaction refuses to close until no
        attempt is sealing and every accepted final record has exact durable
        coverage.  The session owner and its immutable content stay available
        until the caller later proves runtime cleanup and retires the session.
        """

        self._require_handle(handle)
        lease = self._ledger.release_interface_output_session_lease(
            operation_id=operation_id,
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            require_drained=require_drained,
            final_records=final_records,
        )
        session = self._ledger.read_interface_output_session(
            actor_principal_id=self._actor,
            session_id=handle.session.session_id,
            permission=OwnerPermission.DERIVE,
        )
        return InterfaceOutputSessionHandle(session, lease)

    @staticmethod
    def _require_handle(handle: InterfaceOutputSessionHandle) -> None:
        if not isinstance(handle, InterfaceOutputSessionHandle):
            raise TypeError("handle must be an InterfaceOutputSessionHandle.")

    def _require_live_handle(self, handle: InterfaceOutputSessionHandle) -> None:
        self._require_handle(handle)
        current = self.recover_session(launch_id=handle.session.launch_id)
        if (
            current.session.session_id != handle.session.session_id
            or current.session.owner_id != handle.session.owner_id
            or current.lease.lease_id != handle.lease.lease_id
            or current.lease.holder_id != handle.lease.holder_id
            or current.lease.fencing_token != handle.lease.fencing_token
        ):
            raise RealmConflict("Interface output session authority changed.")
        if current.session.state is not InterfaceOutputSessionState.ACTIVE:
            raise RealmConflict("Interface output session is closed.")
        if current.lease.state is not LeaseState.ACTIVE:
            raise RealmConflict("Interface output capture lease is closed.")
        if current.lease.expires_at <= time.time():
            raise RealmExpired("Interface output capture lease expired.")


def _capture_error_code(error: BaseException) -> str:
    if isinstance(error, ContentRejected):
        return "content_rejected"
    if isinstance(error, RealmExpired):
        return "session_expired"
    if isinstance(error, RealmConflict):
        return "realm_conflict"
    return "capture_failed"


__all__ = [
    "DEFAULT_INTERFACE_OUTPUT_LIMITS",
    "InterfaceOutputCapturePass",
    "InterfaceOutputSessionHandle",
    "RealmInterfaceOutputSessionService",
]
