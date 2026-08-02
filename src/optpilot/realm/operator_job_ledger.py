"""RealmLedger mixin for the provider-neutral Operator Job state machine."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import (
    RealmCapacityUnavailable,
    RealmConflict,
    RealmIntegrityError,
    RealmNotFound,
)
from .operator_job_records import (
    OPERATOR_JOB_OUTPUT_ROLE,
    OperatorJobApprovalRecord,
    OperatorJobCleanupComponentState,
    OperatorJobCleanupEvidence,
    OperatorJobCleanupRecord,
    OperatorJobCleanupState,
    OperatorJobDeclaredOutput,
    OperatorJobLaunchIntentRecord,
    OperatorJobLaunchPlan,
    OperatorJobOutcome,
    OperatorJobOutcomeRecord,
    OperatorJobReconciliationState,
    OperatorJobRecord,
    OperatorJobResult,
    OperatorJobResultRecord,
    OperatorJobRevisionRecord,
    OperatorJobState,
    OperatorJobStopRecord,
    OperatorJobTerminalDisposition,
    OperatorJobTerminalStatus,
    operator_job_id,
)
from .owners import OwnerMembership, OwnerPermission
from .refs import SnapshotRef, canonical_json_bytes, parse_physical_content_ref, request_digest
from .selections import (
    ResolvedSelection,
    ResolvedSelectionContent,
    SelectionEligibility,
    SelectionRef,
)


_TERMINAL_STATES = frozenset(
    {
        OperatorJobState.SUCCEEDED,
        OperatorJobState.FAILED,
        OperatorJobState.CANCELLED,
    }
)
_MAX_OPERATOR_JOB_LIST_LIMIT = 200
_OPERATOR_JOB_OUTPUT_SELECTION_CONTEXT_SCHEMA = (
    "optpilot.operator-job-output-selection-context.v1"
)
_OPERATOR_JOB_ACTOR_SCAN_SCHEMA = "optpilot.operator-job-actor-scan.v1"


@dataclass(frozen=True)
class OperatorJobActorCursor:
    """Typed keyset boundary for one actor-scoped Operator Job scan.

    The cursor is not an authorization capability.  Every page independently
    reapplies the actor ACL predicate, while ``scope_digest`` prevents callers
    from accidentally reusing a boundary with different filters or an actor.
    """

    updated_at: float
    job_id: str
    scope_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "updated_at",
            finite_time(self.updated_at, "operator job actor cursor updated_at"),
        )
        object.__setattr__(
            self,
            "job_id",
            required_text(self.job_id, "operator job actor cursor job id"),
        )
        object.__setattr__(
            self,
            "scope_digest",
            lower_hex_digest(
                self.scope_digest, "operator job actor cursor scope digest"
            ),
        )


@dataclass(frozen=True)
class OperatorJobActorPage:
    """One bounded, authorized page from an actor-wide recovery scan."""

    items: Tuple[OperatorJobRecord, ...]
    limit: int
    next_cursor: Optional[OperatorJobActorCursor]

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(not isinstance(item, OperatorJobRecord) for item in items):
            raise TypeError("items must contain OperatorJobRecord values.")
        if len({item.job_id for item in items}) != len(items):
            raise ValueError("operator job actor page contains duplicate jobs.")
        limit = positive_int(self.limit, "operator job actor page limit")
        if limit > _MAX_OPERATOR_JOB_LIST_LIMIT:
            raise ValueError(
                f"operator job actor page limit exceeds {_MAX_OPERATOR_JOB_LIST_LIMIT}."
            )
        if len(items) > limit:
            raise ValueError("operator job actor page exceeds its limit.")
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, OperatorJobActorCursor):
                raise TypeError(
                    "next_cursor must be an OperatorJobActorCursor or None."
                )
            if not items or (
                self.next_cursor.updated_at != items[-1].updated_at
                or self.next_cursor.job_id != items[-1].job_id
            ):
                raise ValueError(
                    "operator job actor page cursor differs from its last item."
                )
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "limit", limit)


class OperatorJobLedgerMixin:
    """Typed Operator Job operations mixed into :class:`RealmLedger`.

    The mixin intentionally relies only on RealmLedger's transactional and ACL
    primitives.  Provider reservation, process creation, and cleanup remain
    outside SQLite; their exact path-free identity is committed by
    :meth:`begin_operator_job_start` before any external start side effect.
    """

    def plan_operator_job(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_owner_id: str,
        plan: OperatorJobLaunchPlan,
        job_id: Optional[str] = None,
    ) -> OperatorJobRecord:
        operation_id = required_text(operation_id, "operation_id")
        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        job_owner_id = required_text(job_owner_id, "operator job owner id")
        if not isinstance(plan, OperatorJobLaunchPlan):
            raise TypeError("plan must be an OperatorJobLaunchPlan.")
        job_id = (
            required_text(job_id, "operator job id")
            if job_id is not None
            else operator_job_id(operation_id)
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            owner = self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=job_owner_id,
                permission=OwnerPermission.ADMIN,
            )
            self._require_active_owner(owner)
            if owner["owner_kind"] != "operator-job":
                raise RealmConflict("Operator Job requires an operator-job owner.")
            if connection.execute(
                "SELECT 1 FROM operator_jobs "
                "WHERE job_id = ? OR owner_id = ? LIMIT 1",
                (job_id, job_owner_id),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Operator Job id or derived job owner is already bound."
                )
            if job_owner_id == plan.target.source_owner_id:
                raise RealmConflict(
                    "Operator Job owner must be derived from, not equal to, its source owner."
                )
            source = connection.execute(
                "SELECT derivation.manifest_digest "
                "FROM owner_derivation_sources source "
                "JOIN owner_derivation_manifests derivation "
                "ON derivation.target_owner_id = source.target_owner_id "
                "WHERE source.target_owner_id = ? AND source.source_owner_id = ?",
                (job_owner_id, plan.target.source_owner_id),
            ).fetchone()
            if source is None:
                raise RealmConflict(
                    "Operator Job owner is not anchored to the target source owner."
                )
            if (
                source["manifest_digest"]
                != plan.owner_derivation_manifest_digest
            ):
                raise RealmConflict(
                    "Operator Job plan differs from its no-copy owner derivation."
                )
            plan_json = canonical_json_bytes(plan.to_dict()).decode("utf-8")
            try:
                connection.execute(
                    "INSERT INTO operator_jobs("
                    "job_id, owner_id, source_owner_id, source_kind, source_id, "
                    "target_selection_digest, "
                    "job_kind, plan_json, plan_digest, state, reconciliation_state, "
                    "cleanup_state, revision, created_by_principal_id, created_txn_id, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', "
                    "'not_started', 'not_required', 0, ?, ?, ?, ?)",
                    (
                        job_id,
                        job_owner_id,
                        plan.target.source_owner_id,
                        plan.target.selection.source_kind,
                        plan.target.selection.source_id,
                        plan.target.selection_digest,
                        plan.job_kind,
                        plan_json,
                        plan.digest,
                        actor_principal_id,
                        txn_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise RealmIntegrityError(
                    "Operator Job plan failed its durable integrity checks."
                ) from error
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=0,
                state=OperatorJobState.PLANNED,
                reconciliation_state=OperatorJobReconciliationState.NOT_STARTED,
                cleanup_state=OperatorJobCleanupState.NOT_REQUIRED,
                operation_kind="operator-job.plan",
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.plan",
                request={
                    "actor_principal_id": actor_principal_id,
                    "job_id": job_id,
                    "job_owner_id": job_owner_id,
                    "plan": plan.to_dict(),
                },
                body=body,
            )
        )

    def request_operator_job_approval(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            self._require_operator_job_head(
                row, expected_revision, (OperatorJobState.PLANNED,)
            )
            revision = expected_revision + 1
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=OperatorJobState.AWAITING_APPROVAL,
                reconciliation_state=OperatorJobReconciliationState.NOT_STARTED,
                now=now,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=OperatorJobState.AWAITING_APPROVAL,
                reconciliation_state=OperatorJobReconciliationState.NOT_STARTED,
                operation_kind="operator-job.request-approval",
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.request-approval",
                request={
                    "actor_principal_id": actor_principal_id,
                    "expected_revision": expected_revision,
                    "job_id": job_id,
                },
                body=body,
            )
        )

    def approve_operator_job(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        expected_plan_digest: str,
        approval_scope_digest: str,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )
        expected_plan_digest = lower_hex_digest(
            expected_plan_digest, "operator job expected plan digest"
        )
        approval_scope_digest = lower_hex_digest(
            approval_scope_digest, "operator job approval scope digest"
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.ADMIN,
            )
            self._require_operator_job_head(
                row, expected_revision, (OperatorJobState.AWAITING_APPROVAL,)
            )
            if row["plan_digest"] != expected_plan_digest:
                raise RealmConflict("Operator Job plan changed before approval.")
            connection.execute(
                "INSERT INTO operator_job_approvals("
                "job_id, plan_digest, approval_scope_digest, "
                "approved_by_principal_id, created_txn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    expected_plan_digest,
                    approval_scope_digest,
                    actor_principal_id,
                    txn_id,
                    now,
                ),
            )
            revision = expected_revision + 1
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=OperatorJobState.QUEUED,
                reconciliation_state=OperatorJobReconciliationState.NOT_STARTED,
                now=now,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=OperatorJobState.QUEUED,
                reconciliation_state=OperatorJobReconciliationState.NOT_STARTED,
                operation_kind="operator-job.approve",
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.approve",
                request={
                    "actor_principal_id": actor_principal_id,
                    "approval_scope_digest": approval_scope_digest,
                    "expected_plan_digest": expected_plan_digest,
                    "expected_revision": expected_revision,
                    "job_id": job_id,
                },
                body=body,
            )
        )

    def begin_operator_job_start(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        admission_lease_id: str,
        admission_holder_id: str,
        admission_fencing_token: int,
        binding_id: str,
        launch_token: str,
        provider_kind: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )
        admission_lease_id = required_text(
            admission_lease_id, "operator job admission lease id"
        )
        admission_holder_id = required_text(
            admission_holder_id, "operator job admission holder id"
        )
        admission_fencing_token = positive_int(
            admission_fencing_token, "operator job admission fencing token"
        )
        binding_id = required_text(binding_id, "operator job binding id")
        launch_token = required_text(launch_token, "operator job launch token")
        provider_kind = required_text(
            provider_kind, "operator job provider kind", max_bytes=128
        )
        evidence_fingerprint = lower_hex_digest(
            evidence_fingerprint, "operator job evidence fingerprint"
        )
        launch_request_digest = lower_hex_digest(
            launch_request_digest, "operator job launch request digest"
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            self._require_operator_job_head(
                row, expected_revision, (OperatorJobState.QUEUED,)
            )
            plan = self._operator_job_plan_from_row(row)
            if provider_kind != plan.backend_kind:
                raise RealmConflict(
                    "Operator Job provider differs from the approved backend kind."
                )
            capacity = self._require_operator_job_capacity_for_plan(
                connection,
                job_id=job_id,
                plan=plan,
                now=now,
                require_current=True,
            )
            admission = self._authorized_lease_row(
                connection, actor_principal_id, admission_lease_id
            )
            self._require_current_lease(
                admission, admission_holder_id, admission_fencing_token, now
            )
            metadata = _json_object(
                admission["metadata_json"], "operator job admission metadata"
            )
            if (
                admission["owner_id"] != row["owner_id"]
                or admission["lease_kind"] != "operator-job-admission"
                or admission["audience"] != "operator-job"
                or metadata
                != {"job_id": job_id, "plan_digest": row["plan_digest"]}
            ):
                raise RealmConflict(
                    "Admission lease differs from the exact Operator Job plan."
                )
            launch = OperatorJobLaunchIntentRecord(
                job_id=job_id,
                plan_digest=row["plan_digest"],
                capacity_reservation_id=capacity["reservation_id"],
                capacity_holder_id=capacity["holder_id"],
                capacity_fencing_token=int(capacity["fencing_token"]),
                admission_lease_id=admission_lease_id,
                admission_holder_id=admission_holder_id,
                admission_fencing_token=admission_fencing_token,
                binding_id=binding_id,
                launch_token=launch_token,
                provider_kind=provider_kind,
                evidence_fingerprint=evidence_fingerprint,
                launch_request_digest=launch_request_digest,
                created_by_principal_id=actor_principal_id,
                created_txn_id=txn_id,
                created_at=now,
            )
            try:
                connection.execute(
                    "INSERT INTO operator_job_launch_intents("
                    "job_id, plan_digest, capacity_reservation_id, "
                    "capacity_holder_id, capacity_fencing_token, admission_lease_id, "
                    "admission_holder_id, admission_fencing_token, binding_id, "
                    "launch_token, provider_kind, "
                    "evidence_fingerprint, launch_request_digest, "
                    "created_by_principal_id, created_txn_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        launch.job_id,
                        launch.plan_digest,
                        launch.capacity_reservation_id,
                        launch.capacity_holder_id,
                        launch.capacity_fencing_token,
                        launch.admission_lease_id,
                        launch.admission_holder_id,
                        launch.admission_fencing_token,
                        launch.binding_id,
                        launch.launch_token,
                        launch.provider_kind,
                        launch.evidence_fingerprint,
                        launch.launch_request_digest,
                        launch.created_by_principal_id,
                        launch.created_txn_id,
                        launch.created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise RealmConflict(
                    "Operator Job launch identity is already bound or stale."
                ) from error
            revision = expected_revision + 1
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=OperatorJobState.STARTING,
                reconciliation_state=OperatorJobReconciliationState.PENDING,
                now=now,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=OperatorJobState.STARTING,
                reconciliation_state=OperatorJobReconciliationState.PENDING,
                operation_kind="operator-job.begin-start",
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.begin-start",
                request={
                    "actor_principal_id": actor_principal_id,
                    "admission_fencing_token": admission_fencing_token,
                    "admission_holder_id": admission_holder_id,
                    "admission_lease_id": admission_lease_id,
                    "binding_id": binding_id,
                    "evidence_fingerprint": evidence_fingerprint,
                    "expected_revision": expected_revision,
                    "job_id": job_id,
                    "launch_request_digest": launch_request_digest,
                    "launch_token": launch_token,
                    "provider_kind": provider_kind,
                },
                body=body,
            )
        )

    def mark_operator_job_running(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        launch_token: str,
        admission_lease_id: str,
        admission_fencing_token: int,
    ) -> OperatorJobRecord:
        return self._advance_launched_operator_job(
            operation_id=operation_id,
            operation_kind="operator-job.mark-running",
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            expected_revision=expected_revision,
            launch_token=launch_token,
            admission_lease_id=admission_lease_id,
            admission_fencing_token=admission_fencing_token,
            expected_states=(OperatorJobState.STARTING,),
            state=OperatorJobState.RUNNING,
            reconciliation_state=OperatorJobReconciliationState.PENDING,
            require_current_admission=True,
        )

    def request_operator_job_stop(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        reason_code: str,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )
        reason_code = required_text(
            reason_code, "operator job stop reason code", max_bytes=128
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            state = OperatorJobState(row["state"])
            if int(row["revision"]) != expected_revision:
                raise RealmConflict("Operator Job revision changed.")
            if state in _TERMINAL_STATES:
                return self._operator_job_record_in_txn(connection, job_id).to_dict()
            existing_stop = connection.execute(
                "SELECT reason_code FROM operator_job_stop_requests WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing_stop is not None:
                if existing_stop["reason_code"] != reason_code:
                    raise RealmConflict("Operator Job already has a different stop reason.")
                return self._operator_job_record_in_txn(connection, job_id).to_dict()
            try:
                connection.execute(
                    "INSERT INTO operator_job_stop_requests("
                    "job_id, reason_code, requested_by_principal_id, created_txn_id, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (job_id, reason_code, actor_principal_id, txn_id, now),
                )
            except sqlite3.IntegrityError as error:
                if "handed-off study launch cancellation" in str(error):
                    raise RealmConflict(
                        "Handed-off study launch cancellation belongs to its run."
                    ) from error
                raise
            revision = expected_revision + 1
            if state in {
                OperatorJobState.PLANNED,
                OperatorJobState.AWAITING_APPROVAL,
                OperatorJobState.QUEUED,
            }:
                outcome = OperatorJobOutcome(
                    status=OperatorJobTerminalStatus.CANCELLED,
                    code=reason_code,
                    started=False,
                    disposition=OperatorJobTerminalDisposition.NEVER_STARTED,
                )
                self._insert_operator_job_outcome(
                    connection,
                    job_id=job_id,
                    outcome=outcome,
                    actor_principal_id=actor_principal_id,
                    txn_id=txn_id,
                    now=now,
                )
                next_state = OperatorJobState.CANCELLED
                reconciliation = OperatorJobReconciliationState.CONFIRMED
                cleanup_state = OperatorJobCleanupState.PENDING
            elif state in {OperatorJobState.STARTING, OperatorJobState.RUNNING}:
                next_state = OperatorJobState.STOPPING
                reconciliation = OperatorJobReconciliationState.PENDING
                cleanup_state = OperatorJobCleanupState.NOT_REQUIRED
            else:
                raise RealmConflict("Operator Job is already stopping.")
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=next_state,
                reconciliation_state=reconciliation,
                now=now,
                cleanup_state=cleanup_state,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=next_state,
                reconciliation_state=reconciliation,
                operation_kind="operator-job.request-stop",
                txn_id=txn_id,
                now=now,
                cleanup_state=cleanup_state,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.request-stop",
                request={
                    "actor_principal_id": actor_principal_id,
                    "expected_revision": expected_revision,
                    "job_id": job_id,
                    "reason_code": reason_code,
                },
                body=body,
            )
        )

    def mark_operator_job_stopping_unconfirmed(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        launch_token: str,
        admission_lease_id: str,
        admission_fencing_token: int,
        degraded: bool = False,
    ) -> OperatorJobRecord:
        if not isinstance(degraded, bool):
            raise TypeError("degraded must be a boolean.")
        return self._advance_launched_operator_job(
            operation_id=operation_id,
            operation_kind="operator-job.reconcile-stopping",
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            expected_revision=expected_revision,
            launch_token=launch_token,
            admission_lease_id=admission_lease_id,
            admission_fencing_token=admission_fencing_token,
            expected_states=(OperatorJobState.STOPPING,),
            state=OperatorJobState.STOPPING,
            reconciliation_state=(
                OperatorJobReconciliationState.DEGRADED
                if degraded
                else OperatorJobReconciliationState.UNCONFIRMED
            ),
            require_current_admission=False,
            request_extra={"degraded": degraded},
        )

    def finish_operator_job(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        launch_token: str,
        admission_lease_id: str,
        admission_fencing_token: int,
        change_id: str,
        expected_owner_revision: int,
        additions: Sequence[OwnerMembership],
        outcome: OperatorJobOutcome,
        result: OperatorJobResult,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )
        launch_token = required_text(launch_token, "operator job launch token")
        admission_lease_id = required_text(
            admission_lease_id, "operator job admission lease id"
        )
        admission_fencing_token = positive_int(
            admission_fencing_token, "operator job admission fencing token"
        )
        change_id = required_text(change_id, "operator job capture change id")
        expected_owner_revision = nonnegative_int(
            expected_owner_revision, "operator job expected owner revision"
        )
        additions_value = _normalize_operator_job_memberships(additions)
        if not isinstance(outcome, OperatorJobOutcome):
            raise TypeError("outcome must be an OperatorJobOutcome.")
        if not isinstance(result, OperatorJobResult):
            raise TypeError("result must be an OperatorJobResult.")
        if outcome.terminal_proof_digest is None:
            raise ValueError("launched Operator Job outcome requires terminal proof.")
        if outcome.evidence_digest != result.digest:
            raise ValueError(
                "Operator Job outcome evidence digest must equal the retained result digest."
            )
        declared_refs = tuple(
            sorted({output.content_ref for output in result.declared_outputs})
        )
        addition_refs = tuple(
            sorted(str(membership.content_ref) for membership in additions_value)
        )
        if (
            len(set(addition_refs)) != len(addition_refs)
            or declared_refs != addition_refs
            or any(
                membership.role != OPERATOR_JOB_OUTPUT_ROLE
                for membership in additions_value
            )
        ):
            raise ValueError(
                "Operator Job declared outputs require one exact shared "
                f"{OPERATOR_JOB_OUTPUT_ROLE} membership per content ref."
            )
        additions_by_ref = {
            str(membership.content_ref): membership
            for membership in additions_value
        }

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            state = OperatorJobState(row["state"])
            if int(row["revision"]) != expected_revision:
                raise RealmConflict("Operator Job revision changed.")
            allowed = {
                OperatorJobTerminalStatus.SUCCEEDED: {OperatorJobState.RUNNING},
                OperatorJobTerminalStatus.FAILED: {
                    OperatorJobState.STARTING,
                    OperatorJobState.RUNNING,
                },
                OperatorJobTerminalStatus.CANCELLED: {OperatorJobState.STOPPING},
            }[outcome.status]
            if state not in allowed:
                raise RealmConflict(
                    "Operator Job state cannot accept this terminal outcome."
                )
            if connection.execute(
                "SELECT 1 FROM study_launch_handoffs WHERE job_id = ?",
                (job_id,),
            ).fetchone() is not None:
                raise RealmConflict(
                    "A handed-off study launch requires typed controller "
                    "confirmation, not generic Operator Job finish."
                )
            self._require_operator_job_launch_identity(
                connection,
                job_id=job_id,
                launch_token=launch_token,
                admission_lease_id=admission_lease_id,
                admission_fencing_token=admission_fencing_token,
                now=now,
                require_current=False,
                require_current_capacity=(
                    outcome.status is OperatorJobTerminalStatus.SUCCEEDED
                ),
            )
            capture = self._authorized_active_change(
                connection,
                actor_principal_id=actor_principal_id,
                change_id=change_id,
                permission=OwnerPermission.DERIVE,
                now=now,
            )
            if capture["owner_id"] != row["owner_id"]:
                raise RealmConflict(
                    "Operator Job capture change belongs to a different owner."
                )
            owner_commit = self._commit_owner_change_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                operation_id=operation_id,
                actor_principal_id=actor_principal_id,
                change_id=change_id,
                expected_owner_revision=expected_owner_revision,
                additions=additions_value,
                removals=(),
            )
            if owner_commit.owner_id != row["owner_id"]:
                raise RealmIntegrityError(
                    "Operator Job capture committed to a different owner."
                )
            for output in result.declared_outputs:
                membership = additions_by_ref[output.content_ref]
                retained = connection.execute(
                    "SELECT content.logical_bytes FROM owner_memberships membership "
                    "JOIN content_objects content "
                    "ON content.store_id = membership.store_id "
                    "AND content.content_ref = membership.content_ref "
                    "WHERE membership.owner_id = ? AND membership.store_id = ? "
                    "AND membership.content_ref = ? AND membership.role = ? "
                    "AND membership.added_txn_id = ? "
                    "AND membership.removed_revision IS NULL LIMIT 1",
                    (
                        row["owner_id"],
                        membership.store_id,
                        output.content_ref,
                        OPERATOR_JOB_OUTPUT_ROLE,
                        txn_id,
                    ),
                ).fetchone()
                if retained is None or int(retained["logical_bytes"]) != output.size_bytes:
                    raise RealmConflict(
                        "Operator Job result references output not retained with its exact size."
                    )
            self._insert_operator_job_outcome(
                connection,
                job_id=job_id,
                outcome=outcome,
                actor_principal_id=actor_principal_id,
                txn_id=txn_id,
                now=now,
            )
            self._insert_operator_job_result(
                connection,
                job_id=job_id,
                result=result,
                actor_principal_id=actor_principal_id,
                txn_id=txn_id,
                now=now,
            )
            revision = expected_revision + 1
            next_state = OperatorJobState(outcome.status.value)
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=next_state,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                now=now,
                cleanup_state=OperatorJobCleanupState.PENDING,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=next_state,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                operation_kind="operator-job.finish",
                txn_id=txn_id,
                now=now,
                cleanup_state=OperatorJobCleanupState.PENDING,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        receipt = OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.finish",
                request={
                    "actor_principal_id": actor_principal_id,
                    "admission_fencing_token": admission_fencing_token,
                    "admission_lease_id": admission_lease_id,
                    "additions": [item.to_dict() for item in additions_value],
                    "change_id": change_id,
                    "expected_revision": expected_revision,
                    "expected_owner_revision": expected_owner_revision,
                    "job_id": job_id,
                    "launch_token": launch_token,
                    "outcome": outcome.to_dict(),
                    "result": result.to_dict(),
                },
                body=body,
            )
        )
        connection = self._connect()
        try:
            current = self._operator_job_record_in_txn(connection, job_id)
        finally:
            connection.close()
        if current.to_dict() != receipt.to_dict():
            raise RealmIntegrityError(
                "Operator Job finish receipt differs from its durable terminal state."
            )
        return current

    def read_operator_job(
        self, *, actor_principal_id: str, job_id: str
    ) -> OperatorJobRecord:
        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        job_id = required_text(job_id, "operator job id")
        connection = self._connect()
        try:
            self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.METADATA_READ,
            )
            return self._operator_job_record_in_txn(connection, job_id)
        finally:
            connection.close()

    def mint_operator_job_output_selection(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        output_id: str,
    ) -> SelectionRef:
        """Mint a path-free selection for one declared terminal job output.

        Callers name only the Operator Job and its result declaration.  The
        authority discovers and anchors the exact terminal-result revision,
        the owner revision committed by that same transaction, and the full
        declared-output identity.  The returned value is evidence to resolve
        again, not a bearer credential and not a provider coordinate.
        """

        actor_principal_id = required_text(
            actor_principal_id, "operator job output actor principal id"
        )
        job_id = required_text(job_id, "operator job output job id")
        output_id = required_text(output_id, "operator job output id")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            job = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.METADATA_READ,
            )
            owner = connection.execute(
                "SELECT * FROM owners WHERE owner_id = ?", (job["owner_id"],)
            ).fetchone()
            if owner is None:
                raise RealmIntegrityError("Operator Job owner is missing.")
            self._require_active_owner(owner)
            (
                terminal_revision,
                terminal_owner_revision,
                result,
                output,
            ) = self._operator_job_output_selection_anchor_in_txn(
                connection,
                job=job,
                output_id=output_id,
            )
            selection = self._build_operator_job_output_selection(
                job=job,
                terminal_revision=terminal_revision,
                terminal_owner_revision=terminal_owner_revision,
                result=result,
                output=output,
            )
            connection.commit()
            return selection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _resolve_operator_job_output_selection_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        selection: SelectionRef,
        allowed_permissions: Sequence[OwnerPermission],
        content_read: bool = False,
    ) -> ResolvedSelection | ResolvedSelectionContent:
        """Re-resolve one retained Operator Job output without host paths."""

        resolution_type = ResolvedSelectionContent if content_read else ResolvedSelection

        if selection.source_kind != "operator-job":
            raise ValueError("selection is not an Operator Job output selection.")
        job = connection.execute(
            "SELECT * FROM operator_jobs WHERE job_id = ?", (selection.source_id,)
        ).fetchone()
        if job is None or job["owner_id"] != selection.source_owner_id:
            raise RealmNotFound("Entity not found.")
        owner = self._authorize_owner_any(
            connection,
            actor_principal_id=actor_principal_id,
            owner_id=job["owner_id"],
            permissions=allowed_permissions,
        )
        self._require_active_owner(owner)
        (
            terminal_revision,
            terminal_owner_revision,
            result,
            output,
        ) = self._operator_job_output_selection_anchor_in_txn(
            connection,
            job=job,
            output_id=selection.entity_id,
        )
        expected = self._build_operator_job_output_selection(
            job=job,
            terminal_revision=terminal_revision,
            terminal_owner_revision=terminal_owner_revision,
            result=result,
            output=output,
        )
        if selection != expected:
            raise RealmNotFound("Entity not found.")

        current_owner_revision = int(owner["revision"])
        content_ref = parse_physical_content_ref(output.content_ref)
        if not content_read and not isinstance(content_ref, SnapshotRef):
            return ResolvedSelection(
                selection,
                current_owner_revision,
                SelectionEligibility.unsupported(
                    "operator_job_file_output_not_tree",
                    "This saved file is result evidence, not an editable project folder.",
                ),
            )
        available = connection.execute(
            "SELECT membership.store_id FROM owner_memberships membership "
            "JOIN content_objects content "
            "ON content.store_id = membership.store_id "
            "AND content.content_ref = membership.content_ref "
            "WHERE membership.owner_id = ? AND membership.content_ref = ? "
            "AND membership.role = ? AND membership.added_txn_id = ? "
            "AND membership.removed_revision IS NULL "
            "AND content.kind = ? AND content.logical_bytes = ? "
            "AND content.lifecycle_state = 'live' "
            "AND content.trust_state = 'verified_local' "
            "ORDER BY membership.store_id",
            (
                job["owner_id"],
                output.content_ref,
                OPERATOR_JOB_OUTPUT_ROLE,
                result.created_txn_id,
                "tree" if isinstance(content_ref, SnapshotRef) else "blob",
                output.size_bytes,
            ),
        ).fetchall()
        if not available:
            return resolution_type(
                selection,
                current_owner_revision,
                SelectionEligibility.unavailable(
                    "selection_content_unavailable",
                    "The selected Operator Job output is no longer retained and verified.",
                ),
            )
        if len(available) != 1:
            raise RealmIntegrityError(
                "Operator Job output has ambiguous active retention membership."
            )
        return resolution_type(
            selection,
            current_owner_revision,
            SelectionEligibility.ready(),
            OwnerMembership(
                available[0]["store_id"], content_ref, OPERATOR_JOB_OUTPUT_ROLE
            ),
        )

    def complete_operator_job_cleanup(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        evidence: OperatorJobCleanupEvidence,
        launch_token: Optional[str] = None,
        admission_lease_id: Optional[str] = None,
        admission_holder_id: Optional[str] = None,
        admission_fencing_token: Optional[int] = None,
        capacity_reservation_id: Optional[str] = None,
        capacity_holder_id: Optional[str] = None,
        capacity_fencing_token: Optional[int] = None,
    ) -> OperatorJobRecord:
        """Commit completion of every applicable post-terminal cleanup phase.

        External cleanup is deliberately not conflated with the terminal
        outcome.  This operation can run only against a terminal ``pending``
        head and validates the immutable launch chain plus the released
        admission/capacity fences before writing a separate receipt/event.
        """

        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )
        if not isinstance(evidence, OperatorJobCleanupEvidence):
            raise TypeError("evidence must be OperatorJobCleanupEvidence.")
        launch_token = _optional_operator_job_text(
            launch_token, "operator job cleanup launch token"
        )
        admission_identity = _optional_operator_job_fence(
            admission_lease_id,
            admission_holder_id,
            admission_fencing_token,
            label="operator job cleanup admission",
        )
        capacity_identity = _optional_operator_job_fence(
            capacity_reservation_id,
            capacity_holder_id,
            capacity_fencing_token,
            label="operator job cleanup capacity",
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            state = OperatorJobState(row["state"])
            if (
                int(row["revision"]) != expected_revision
                or not state.terminal
                or OperatorJobCleanupState(row["cleanup_state"])
                is not OperatorJobCleanupState.PENDING
            ):
                raise RealmConflict("Operator Job cleanup lifecycle changed.")
            outcome_row = connection.execute(
                "SELECT * FROM operator_job_outcomes WHERE job_id = ?", (job_id,)
            ).fetchone()
            if outcome_row is None or (
                evidence.terminal_revision != expected_revision
                or evidence.terminal_outcome_digest != outcome_row["outcome_digest"]
            ):
                raise RealmConflict(
                    "Operator Job cleanup evidence differs from its terminal outcome."
                )

            launch = connection.execute(
                "SELECT * FROM operator_job_launch_intents WHERE job_id = ?", (job_id,)
            ).fetchone()
            if launch is None:
                if launch_token is not None:
                    raise RealmConflict(
                        "Unlaunched Operator Job cleanup supplied launch authority."
                    )
            else:
                if launch_token is None or admission_identity is None:
                    raise RealmConflict(
                        "Launched Operator Job cleanup lacks exact launch authority."
                    )
                launch = self._require_operator_job_launch_identity(
                    connection,
                    job_id=job_id,
                    launch_token=launch_token,
                    admission_lease_id=admission_identity[0],
                    admission_fencing_token=admission_identity[2],
                    now=now,
                    require_current=False,
                )
                if launch["admission_holder_id"] != admission_identity[1]:
                    raise RealmConflict(
                        "Operator Job cleanup admission holder is stale."
                    )
                if capacity_identity is None or (
                    launch["capacity_reservation_id"] != capacity_identity[0]
                    or launch["capacity_holder_id"] != capacity_identity[1]
                    or int(launch["capacity_fencing_token"])
                    != capacity_identity[2]
                ):
                    raise RealmConflict(
                        "Operator Job cleanup capacity fence is stale."
                    )

            admission = connection.execute(
                "SELECT * FROM leases WHERE owner_id = ? "
                "AND lease_kind = 'operator-job-admission' "
                "AND scope_key = ?",
                (row["owner_id"], f"operator-job-admission:{job_id}"),
            ).fetchone()
            self._require_operator_job_cleanup_component(
                component=evidence.admission,
                identity=admission_identity,
                durable_row=admission,
                id_column="lease_id",
                state_column="state",
                holder_column="holder_id",
                fence_column="fencing_token",
                label="admission",
            )
            # Deterministic absence is positive cleanup evidence.  Even a job
            # cancelled before admission proves that none of its resource
            # operation coordinates exist; it is not "not applicable" debt.
            if (
                evidence.resources.state
                is not OperatorJobCleanupComponentState.COMPLETE
            ):
                raise RealmConflict(
                    "Operator Job resource cleanup evidence is incomplete."
                )

            capacity = connection.execute(
                "SELECT * FROM operator_capacity_reservations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self._require_operator_job_cleanup_component(
                component=evidence.capacity,
                identity=capacity_identity,
                durable_row=capacity,
                id_column="reservation_id",
                state_column="state",
                holder_column="holder_id",
                fence_column="fencing_token",
                label="capacity",
            )

            connection.execute(
                "INSERT INTO operator_job_cleanup_receipts("
                "job_id, terminal_revision, terminal_state, evidence_digest, "
                "evidence_json, created_by_principal_id, created_txn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    expected_revision,
                    state.value,
                    evidence.digest,
                    canonical_json_bytes(evidence.to_dict()).decode("utf-8"),
                    actor_principal_id,
                    txn_id,
                    now,
                ),
            )
            revision = expected_revision + 1
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=state,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                cleanup_state=OperatorJobCleanupState.COMPLETE,
                now=now,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=state,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                cleanup_state=OperatorJobCleanupState.COMPLETE,
                operation_kind="operator-job.complete-cleanup",
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        receipt = OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="operator-job.complete-cleanup",
                request={
                    "actor_principal_id": actor_principal_id,
                    "admission_fencing_token": (
                        None if admission_identity is None else admission_identity[2]
                    ),
                    "admission_holder_id": (
                        None if admission_identity is None else admission_identity[1]
                    ),
                    "admission_lease_id": (
                        None if admission_identity is None else admission_identity[0]
                    ),
                    "capacity_fencing_token": (
                        None if capacity_identity is None else capacity_identity[2]
                    ),
                    "capacity_holder_id": (
                        None if capacity_identity is None else capacity_identity[1]
                    ),
                    "capacity_reservation_id": (
                        None if capacity_identity is None else capacity_identity[0]
                    ),
                    "evidence": evidence.to_dict(),
                    "expected_revision": expected_revision,
                    "job_id": job_id,
                    "launch_token": launch_token,
                },
                body=body,
            )
        )
        current = self.read_operator_job(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
        )
        if current.to_dict() != receipt.to_dict():
            raise RealmIntegrityError(
                "Operator Job cleanup receipt differs from its durable head."
            )
        return current

    @staticmethod
    def _require_operator_job_cleanup_component(
        *,
        component: Any,
        identity: Optional[tuple[str, str, int]],
        durable_row: Optional[sqlite3.Row],
        id_column: str,
        state_column: str,
        holder_column: str,
        fence_column: str,
        label: str,
    ) -> None:
        if durable_row is None:
            if (
                identity is not None
                or component.state
                is not OperatorJobCleanupComponentState.NOT_APPLICABLE
            ):
                raise RealmConflict(
                    f"Operator Job {label} cleanup evidence is not applicable."
                )
            return
        if identity is None or (
            durable_row[id_column] != identity[0]
            or durable_row[holder_column] != identity[1]
            or int(durable_row[fence_column]) != identity[2]
        ):
            raise RealmConflict(f"Operator Job {label} cleanup fence is stale.")
        if durable_row[state_column] == "active" or (
            component.state is not OperatorJobCleanupComponentState.COMPLETE
        ):
            raise RealmConflict(f"Operator Job {label} cleanup is incomplete.")

    def list_operator_jobs(
        self,
        *,
        actor_principal_id: str,
        owner_id: str,
        states: Optional[Sequence[OperatorJobState]] = None,
        cleanup_states: Optional[Sequence[OperatorJobCleanupState]] = None,
        limit: int = 100,
    ) -> Tuple[OperatorJobRecord, ...]:
        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        owner_id = required_text(owner_id, "operator job owner id")
        limit = self._operator_job_list_limit(limit)
        normalized: Optional[Tuple[OperatorJobState, ...]] = None
        if states is not None:
            values = tuple(states)
            if not values or any(not isinstance(item, OperatorJobState) for item in values):
                raise ValueError("states must contain OperatorJobState values.")
            normalized = tuple(sorted(set(values), key=lambda item: item.value))
        normalized_cleanup = _normalize_operator_job_cleanup_states(cleanup_states)
        connection = self._connect()
        try:
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=owner_id,
                permission=OwnerPermission.METADATA_READ,
            )
            predicates = ["owner_id = ?"]
            parameters: list[Any] = [owner_id]
            if normalized is not None:
                placeholders = ", ".join("?" for _ in normalized)
                predicates.append(f"state IN ({placeholders})")
                parameters.extend(item.value for item in normalized)
            if normalized_cleanup is not None:
                placeholders = ", ".join("?" for _ in normalized_cleanup)
                predicates.append(f"cleanup_state IN ({placeholders})")
                parameters.extend(item.value for item in normalized_cleanup)
            parameters.append(limit)
            if normalized is None and normalized_cleanup is None:
                rows = connection.execute(
                    "SELECT job_id FROM operator_jobs WHERE owner_id = ? "
                    "ORDER BY updated_at DESC, job_id LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT job_id FROM operator_jobs WHERE "
                    + " AND ".join(predicates)
                    + " ORDER BY updated_at DESC, job_id LIMIT ?",
                    tuple(parameters),
                ).fetchall()
            return tuple(
                self._operator_job_record_in_txn(connection, row["job_id"])
                for row in rows
            )
        finally:
            connection.close()

    def list_operator_jobs_for_source(
        self,
        *,
        actor_principal_id: str,
        source_owner_id: str,
        source_kind: str,
        source_id: str,
        job_kind: Optional[str] = None,
        states: Optional[Sequence[OperatorJobState]] = None,
        cleanup_states: Optional[Sequence[OperatorJobCleanupState]] = None,
        limit: int = 100,
    ) -> Tuple[OperatorJobRecord, ...]:
        """List authorized jobs attached to one persisted selection source.

        Studio derives these coordinates from its authorized run/workspace
        context.  The query never accepts a job owner as a substitute for the
        source and filters every result through the derived owner's direct ACL.
        """

        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        source_owner_id = required_text(
            source_owner_id, "operator job source owner id"
        )
        source_kind = required_text(
            source_kind, "operator job source kind", max_bytes=128
        )
        source_id = required_text(source_id, "operator job source id")
        if job_kind is not None:
            job_kind = required_text(
                job_kind, "operator job kind", max_bytes=128
            )
        limit = self._operator_job_list_limit(limit)
        normalized: Optional[Tuple[OperatorJobState, ...]] = None
        if states is not None:
            values = tuple(states)
            if not values or any(
                not isinstance(item, OperatorJobState) for item in values
            ):
                raise ValueError("states must contain OperatorJobState values.")
            normalized = tuple(sorted(set(values), key=lambda item: item.value))
        normalized_cleanup = _normalize_operator_job_cleanup_states(cleanup_states)
        connection = self._connect()
        try:
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=source_owner_id,
                permission=OwnerPermission.METADATA_READ,
            )
            predicates = [
                "job.source_owner_id = ?",
                "job.source_kind = ?",
                "job.source_id = ?",
                "(owner.principal_id = ? OR EXISTS ("
                "SELECT 1 FROM owner_grants grant_record "
                "WHERE grant_record.owner_id = job.owner_id "
                "AND grant_record.principal_id = ? "
                "AND grant_record.permission IN ('metadata_read', 'admin') "
                "AND grant_record.removed_revision IS NULL))",
            ]
            parameters: list[Any] = [
                source_owner_id,
                source_kind,
                source_id,
                actor_principal_id,
                actor_principal_id,
            ]
            if job_kind is not None:
                predicates.append("job.job_kind = ?")
                parameters.append(job_kind)
            if normalized is not None:
                placeholders = ", ".join("?" for _ in normalized)
                predicates.append(f"job.state IN ({placeholders})")
                parameters.extend(item.value for item in normalized)
            if normalized_cleanup is not None:
                placeholders = ", ".join("?" for _ in normalized_cleanup)
                predicates.append(f"job.cleanup_state IN ({placeholders})")
                parameters.extend(item.value for item in normalized_cleanup)
            parameters.append(limit)
            rows = connection.execute(
                "SELECT job.job_id FROM operator_jobs job "
                "JOIN owners owner ON owner.owner_id = job.owner_id WHERE "
                + " AND ".join(predicates)
                + " ORDER BY job.updated_at DESC, job.job_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
            return tuple(
                self._operator_job_record_in_txn(connection, row["job_id"])
                for row in rows
            )
        finally:
            connection.close()

    def list_operator_jobs_for_actor(
        self,
        *,
        actor_principal_id: str,
        job_kind: Optional[str] = None,
        states: Optional[Sequence[OperatorJobState]] = None,
        cleanup_states: Optional[Sequence[OperatorJobCleanupState]] = None,
        limit: int = 100,
    ) -> Tuple[OperatorJobRecord, ...]:
        """List bounded jobs directly visible to one authenticated actor.

        This is the recovery discovery seam for job kinds whose exact source
        is not known to a fresh presentation process (notably study launch
        before its run handoff).  It is deliberately actor-bound and returns
        the same canonical records as source-scoped reads; it is not a second
        status model.  Recovery callers that must enumerate the complete
        result set should use :meth:`list_operator_jobs_for_actor_page`.
        """

        return self.list_operator_jobs_for_actor_page(
            actor_principal_id=actor_principal_id,
            job_kind=job_kind,
            states=states,
            cleanup_states=cleanup_states,
            limit=limit,
        ).items

    def list_operator_jobs_for_actor_page(
        self,
        *,
        actor_principal_id: str,
        job_kind: Optional[str] = None,
        states: Optional[Sequence[OperatorJobState]] = None,
        cleanup_states: Optional[Sequence[OperatorJobCleanupState]] = None,
        cursor: Optional[OperatorJobActorCursor] = None,
        limit: int = 100,
    ) -> OperatorJobActorPage:
        """Scan an actor's visible jobs with a stable typed keyset cursor.

        Ordering is newest ``updated_at`` first and then ascending ``job_id``.
        The explicit tie breaker makes equal-timestamp pages deterministic.
        The page cursor is scoped to the actor and normalized filters, but is
        not a bearer token: authorization is evaluated again for every page.
        """

        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        if job_kind is not None:
            job_kind = required_text(
                job_kind, "operator job kind", max_bytes=128
            )
        limit = self._operator_job_list_limit(limit)
        normalized: Optional[Tuple[OperatorJobState, ...]] = None
        if states is not None:
            values = tuple(states)
            if not values or any(
                not isinstance(item, OperatorJobState) for item in values
            ):
                raise ValueError("states must contain OperatorJobState values.")
            normalized = tuple(sorted(set(values), key=lambda item: item.value))
        normalized_cleanup = _normalize_operator_job_cleanup_states(cleanup_states)
        if cursor is not None and not isinstance(cursor, OperatorJobActorCursor):
            raise TypeError("cursor must be an OperatorJobActorCursor or None.")
        scope_digest = request_digest(
            {
                "actor_principal_id": actor_principal_id,
                "cleanup_states": (
                    None
                    if normalized_cleanup is None
                    else [item.value for item in normalized_cleanup]
                ),
                "job_kind": job_kind,
                "order": ["updated_at:desc", "job_id:asc"],
                "schema": _OPERATOR_JOB_ACTOR_SCAN_SCHEMA,
                "states": (
                    None
                    if normalized is None
                    else [item.value for item in normalized]
                ),
            }
        )
        if cursor is not None and cursor.scope_digest != scope_digest:
            raise ValueError("operator job actor cursor does not match this scan.")
        predicates = [
            "(owner.principal_id = ? OR EXISTS ("
            "SELECT 1 FROM owner_grants grant_record "
            "WHERE grant_record.owner_id = job.owner_id "
            "AND grant_record.principal_id = ? "
            "AND grant_record.permission IN ('metadata_read', 'admin') "
            "AND grant_record.removed_revision IS NULL))"
        ]
        parameters: list[Any] = [actor_principal_id, actor_principal_id]
        if job_kind is not None:
            predicates.append("job.job_kind = ?")
            parameters.append(job_kind)
        if normalized is not None:
            placeholders = ", ".join("?" for _ in normalized)
            predicates.append(f"job.state IN ({placeholders})")
            parameters.extend(item.value for item in normalized)
        if normalized_cleanup is not None:
            placeholders = ", ".join("?" for _ in normalized_cleanup)
            predicates.append(f"job.cleanup_state IN ({placeholders})")
            parameters.extend(item.value for item in normalized_cleanup)
        if cursor is not None:
            predicates.append(
                "(job.updated_at < ? OR "
                "(job.updated_at = ? AND job.job_id > ?))"
            )
            parameters.extend(
                (cursor.updated_at, cursor.updated_at, cursor.job_id)
            )
        parameters.append(limit + 1)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT job.job_id, job.updated_at FROM operator_jobs job "
                "JOIN owners owner ON owner.owner_id = job.owner_id WHERE "
                + " AND ".join(predicates)
                + " ORDER BY job.updated_at DESC, job.job_id ASC LIMIT ?",
                tuple(parameters),
            ).fetchall()
            selected_rows = rows[:limit]
            items = tuple(
                self._operator_job_record_in_txn(connection, row["job_id"])
                for row in selected_rows
            )
            next_cursor = None
            if len(rows) > limit:
                boundary = selected_rows[-1]
                next_cursor = OperatorJobActorCursor(
                    updated_at=boundary["updated_at"],
                    job_id=boundary["job_id"],
                    scope_digest=scope_digest,
                )
            result = OperatorJobActorPage(
                items=items,
                limit=limit,
                next_cursor=next_cursor,
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_operator_job_revisions(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        after_revision: Optional[int] = None,
        limit: int = 100,
    ) -> Tuple[OperatorJobRevisionRecord, ...]:
        actor_principal_id = required_text(
            actor_principal_id, "operator job actor principal id"
        )
        job_id = required_text(job_id, "operator job id")
        if after_revision is not None:
            after_revision = nonnegative_int(
                after_revision, "operator job revision cursor"
            )
        limit = self._operator_job_list_limit(limit)
        connection = self._connect()
        try:
            self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.METADATA_READ,
            )
            if after_revision is None:
                rows = connection.execute(
                    "SELECT * FROM operator_job_revisions WHERE job_id = ? "
                    "ORDER BY revision LIMIT ?",
                    (job_id, limit),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM operator_job_revisions WHERE job_id = ? "
                    "AND revision > ? ORDER BY revision LIMIT ?",
                    (job_id, after_revision, limit),
                )
            return tuple(_operator_job_revision_from_row(row) for row in rows)
        finally:
            connection.close()

    @staticmethod
    def _operator_job_list_limit(value: int) -> int:
        value = positive_int(value, "operator job list limit")
        if value > _MAX_OPERATOR_JOB_LIST_LIMIT:
            raise ValueError(
                f"operator job list limit exceeds {_MAX_OPERATOR_JOB_LIST_LIMIT}."
            )
        return value

    def _advance_launched_operator_job(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
        launch_token: str,
        admission_lease_id: str,
        admission_fencing_token: int,
        expected_states: Tuple[OperatorJobState, ...],
        state: OperatorJobState,
        reconciliation_state: OperatorJobReconciliationState,
        require_current_admission: bool,
        request_extra: Optional[Mapping[str, Any]] = None,
    ) -> OperatorJobRecord:
        operation_id, actor_principal_id, job_id, expected_revision = (
            self._normalize_operator_job_transition(
                operation_id,
                actor_principal_id,
                job_id,
                expected_revision,
            )
        )

        launch_token = required_text(launch_token, "operator job launch token")
        admission_lease_id = required_text(
            admission_lease_id, "operator job admission lease id"
        )
        admission_fencing_token = positive_int(
            admission_fencing_token, "operator job admission fencing token"
        )

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            row = self._authorized_operator_job_row(
                connection,
                actor_principal_id=actor_principal_id,
                job_id=job_id,
                permission=OwnerPermission.DERIVE,
            )
            self._require_operator_job_head(row, expected_revision, expected_states)
            self._require_operator_job_launch_identity(
                connection,
                job_id=job_id,
                launch_token=launch_token,
                admission_lease_id=admission_lease_id,
                admission_fencing_token=admission_fencing_token,
                now=now,
                require_current=require_current_admission,
                require_current_capacity=require_current_admission,
            )
            revision = expected_revision + 1
            self._update_operator_job_head(
                connection,
                job_id=job_id,
                expected_revision=expected_revision,
                revision=revision,
                state=state,
                reconciliation_state=reconciliation_state,
                now=now,
            )
            self._insert_operator_job_revision(
                connection,
                job_id=job_id,
                revision=revision,
                state=state,
                reconciliation_state=reconciliation_state,
                operation_kind=operation_kind,
                txn_id=txn_id,
                now=now,
            )
            return self._operator_job_record_in_txn(connection, job_id).to_dict()

        request = {
            "actor_principal_id": actor_principal_id,
            "admission_fencing_token": admission_fencing_token,
            "admission_lease_id": admission_lease_id,
            "expected_revision": expected_revision,
            "job_id": job_id,
            "launch_token": launch_token,
        }
        request.update(dict(request_extra or {}))
        return OperatorJobRecord.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind=operation_kind,
                request=request,
                body=body,
            )
        )

    @staticmethod
    def _normalize_operator_job_transition(
        operation_id: str,
        actor_principal_id: str,
        job_id: str,
        expected_revision: int,
    ) -> tuple[str, str, str, int]:
        return (
            required_text(operation_id, "operation_id"),
            required_text(actor_principal_id, "operator job actor principal id"),
            required_text(job_id, "operator job id"),
            nonnegative_int(expected_revision, "operator job expected revision"),
        )

    def _authorized_operator_job_row(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        job_id: str,
        permission: OwnerPermission,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operator_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise RealmNotFound("Entity not found.")
        self._authorize_owner(
            connection,
            actor_principal_id=actor_principal_id,
            owner_id=row["owner_id"],
            permission=permission,
        )
        return row

    @staticmethod
    def _require_operator_job_head(
        row: sqlite3.Row,
        expected_revision: int,
        expected_states: Tuple[OperatorJobState, ...],
    ) -> None:
        if (
            int(row["revision"]) != expected_revision
            or OperatorJobState(row["state"]) not in expected_states
        ):
            raise RealmConflict("Operator Job revision or lifecycle changed.")

    @staticmethod
    def _update_operator_job_head(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        expected_revision: int,
        revision: int,
        state: OperatorJobState,
        reconciliation_state: OperatorJobReconciliationState,
        now: float,
        cleanup_state: OperatorJobCleanupState = OperatorJobCleanupState.NOT_REQUIRED,
    ) -> None:
        updated = connection.execute(
            "UPDATE operator_jobs SET state = ?, reconciliation_state = ?, "
            "cleanup_state = ?, revision = ?, updated_at = ? "
            "WHERE job_id = ? AND revision = ?",
            (
                state.value,
                reconciliation_state.value,
                cleanup_state.value,
                revision,
                now,
                job_id,
                expected_revision,
            ),
        )
        if updated.rowcount != 1:
            raise RealmConflict("Operator Job revision changed.")

    @staticmethod
    def _insert_operator_job_revision(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        revision: int,
        state: OperatorJobState,
        reconciliation_state: OperatorJobReconciliationState,
        operation_kind: str,
        txn_id: int,
        now: float,
        cleanup_state: OperatorJobCleanupState = OperatorJobCleanupState.NOT_REQUIRED,
    ) -> None:
        connection.execute(
            "INSERT INTO operator_job_revisions("
            "job_id, revision, state, reconciliation_state, cleanup_state, "
            "operation_kind, txn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                revision,
                state.value,
                reconciliation_state.value,
                cleanup_state.value,
                operation_kind,
                txn_id,
                now,
            ),
        )

    def _require_operator_job_launch_identity(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        launch_token: str,
        admission_lease_id: str,
        admission_fencing_token: int,
        now: float,
        require_current: bool,
        require_current_capacity: bool = False,
    ) -> sqlite3.Row:
        launch = connection.execute(
            "SELECT * FROM operator_job_launch_intents WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if launch is None or (
            launch["launch_token"] != launch_token
            or launch["admission_lease_id"] != admission_lease_id
            or int(launch["admission_fencing_token"]) != admission_fencing_token
        ):
            raise RealmConflict("Operator Job launch authority is stale.")
        admission = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (admission_lease_id,)
        ).fetchone()
        if admission is None or (
            admission["holder_id"] != launch["admission_holder_id"]
            or int(admission["fencing_token"]) != admission_fencing_token
        ):
            raise RealmConflict("Operator Job admission authority is stale.")
        if require_current:
            self._require_current_lease(
                admission,
                launch["admission_holder_id"],
                admission_fencing_token,
                now,
            )
        row = connection.execute(
            "SELECT * FROM operator_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RealmNotFound("Entity not found.")
        plan = self._operator_job_plan_from_row(row)
        capacity = self._require_operator_job_capacity_for_plan(
            connection,
            job_id=job_id,
            plan=plan,
            now=now,
            require_current=require_current_capacity,
        )
        if (
            launch["capacity_reservation_id"] != capacity["reservation_id"]
            or launch["capacity_holder_id"] != capacity["holder_id"]
            or int(launch["capacity_fencing_token"])
            != int(capacity["fencing_token"])
        ):
            raise RealmConflict("Operator Job capacity launch authority is stale.")
        return launch

    @staticmethod
    def _require_operator_job_capacity_for_plan(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        plan: OperatorJobLaunchPlan,
        now: float,
        require_current: bool,
    ) -> sqlite3.Row:
        capacity = connection.execute(
            "SELECT * FROM operator_capacity_reservations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if capacity is None:
            raise RealmCapacityUnavailable(
                "Operator Job has no capacity reservation."
            )
        try:
            claims = _json_object(
                capacity["claims_json"], "operator job capacity claims"
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted Operator Job capacity claims are invalid."
            ) from error
        if (
            capacity["job_id"] != job_id
            or capacity["plan_digest"] != plan.digest
            or capacity["pool_name"] != plan.backend_realm
            or claims != dict(plan.resource_claims)
        ):
            raise RealmIntegrityError(
                "Operator Job capacity differs from its immutable plan."
            )
        if not require_current:
            return capacity
        pool = connection.execute(
            "SELECT revision, state FROM operator_capacity_pools "
            "WHERE pool_name = ?",
            (capacity["pool_name"],),
        ).fetchone()
        if (
            capacity["state"] != "active"
            or float(capacity["expires_at"]) <= now
            or pool is None
            or pool["state"] != "ready"
            or int(pool["revision"]) != int(capacity["pool_revision"])
        ):
            raise RealmCapacityUnavailable(
                "Operator Job capacity reservation is no longer current."
            )
        return capacity

    @staticmethod
    def _insert_operator_job_outcome(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        outcome: OperatorJobOutcome,
        actor_principal_id: str,
        txn_id: int,
        now: float,
    ) -> None:
        record = OperatorJobOutcomeRecord(
            job_id=job_id,
            outcome=outcome,
            created_by_principal_id=actor_principal_id,
            created_txn_id=txn_id,
            created_at=now,
        )
        outcome_digest = hashlib.sha256(
            canonical_json_bytes(record.to_dict())
        ).hexdigest()
        connection.execute(
            "INSERT INTO operator_job_outcomes("
            "job_id, status, code, started, disposition, terminal_proof_digest, "
            "evidence_digest, detail_digest, outcome_digest, created_by_principal_id, "
            "created_txn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                outcome.status.value,
                outcome.code,
                int(outcome.started),
                outcome.disposition.value,
                outcome.terminal_proof_digest,
                outcome.evidence_digest,
                outcome.detail_digest,
                outcome_digest,
                actor_principal_id,
                txn_id,
                now,
            ),
        )

    @staticmethod
    def _insert_operator_job_result(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        result: OperatorJobResult,
        actor_principal_id: str,
        txn_id: int,
        now: float,
    ) -> None:
        connection.execute(
            "INSERT INTO operator_job_results("
            "job_id, result_digest, result_json, created_by_principal_id, "
            "created_txn_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                result.digest,
                canonical_json_bytes(result.to_dict()).decode("utf-8"),
                actor_principal_id,
                txn_id,
                now,
            ),
        )

    def _operator_job_output_selection_anchor_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        job: sqlite3.Row,
        output_id: str,
    ) -> tuple[int, int, OperatorJobResultRecord, OperatorJobDeclaredOutput]:
        """Load the immutable result/capture anchors for one declared output."""

        if OperatorJobState(job["state"]) not in _TERMINAL_STATES:
            raise RealmConflict(
                "Operator Job outputs can be selected only after a terminal result."
            )
        record = self._operator_job_record_in_txn(connection, job["job_id"])
        if (
            record.owner_id != job["owner_id"]
            or record.state not in _TERMINAL_STATES
            or record.result is None
            or record.outcome is None
        ):
            raise RealmNotFound("Entity not found.")
        result = record.result
        if (
            result.created_txn_id != record.outcome.created_txn_id
            or record.outcome.outcome.evidence_digest != result.result_digest
        ):
            raise RealmIntegrityError(
                "Operator Job result differs from its terminal evidence identity."
            )
        terminal_revision = connection.execute(
            "SELECT revision, state FROM operator_job_revisions "
            "WHERE job_id = ? AND txn_id = ?",
            (record.job_id, result.created_txn_id),
        ).fetchone()
        if (
            terminal_revision is None
            or terminal_revision["state"] != record.state.value
            or int(terminal_revision["revision"]) > record.revision
        ):
            raise RealmIntegrityError(
                "Operator Job result lacks its exact terminal revision anchor."
            )
        owner_revision = connection.execute(
            "SELECT revision FROM owner_revisions "
            "WHERE owner_id = ? AND txn_id = ?",
            (record.owner_id, result.created_txn_id),
        ).fetchone()
        if owner_revision is None:
            raise RealmIntegrityError(
                "Operator Job result lacks its exact owner revision anchor."
            )
        outputs = tuple(
            output
            for output in result.result.declared_outputs
            if output.declaration_id == output_id
        )
        if len(outputs) != 1:
            raise RealmNotFound("Entity not found.")
        return (
            int(terminal_revision["revision"]),
            int(owner_revision["revision"]),
            result,
            outputs[0],
        )

    @staticmethod
    def _build_operator_job_output_selection(
        *,
        job: sqlite3.Row,
        terminal_revision: int,
        terminal_owner_revision: int,
        result: OperatorJobResultRecord,
        output: OperatorJobDeclaredOutput,
    ) -> SelectionRef:
        context_digest = request_digest(
            {
                "schema": _OPERATOR_JOB_OUTPUT_SELECTION_CONTEXT_SCHEMA,
                "job_id": job["job_id"],
                "terminal_revision": terminal_revision,
                "terminal_state": job["state"],
                "owner_revision": terminal_owner_revision,
                "result_digest": result.result_digest,
                "output": output.to_dict(),
            }
        )
        return SelectionRef.build(
            kind="artifact",
            source_kind="operator-job",
            source_id=job["job_id"],
            source_owner_id=job["owner_id"],
            source_revision=terminal_revision,
            owner_revision=terminal_owner_revision,
            source_sequence=None,
            entity_sequence=None,
            entity_id=output.declaration_id,
            entity_ref=output.content_ref,
            context_digest=context_digest,
            relative_path=None,
        )

    @staticmethod
    def _operator_job_plan_from_row(row: sqlite3.Row) -> OperatorJobLaunchPlan:
        return OperatorJobLaunchPlan.from_dict(
            _json_object(row["plan_json"], "operator job plan")
        )

    def _operator_job_record_in_txn(
        self, connection: sqlite3.Connection, job_id: str
    ) -> OperatorJobRecord:
        row = connection.execute(
            "SELECT * FROM operator_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise RealmNotFound("Entity not found.")
        approval_row = connection.execute(
            "SELECT * FROM operator_job_approvals WHERE job_id = ?", (job_id,)
        ).fetchone()
        launch_row = connection.execute(
            "SELECT * FROM operator_job_launch_intents WHERE job_id = ?", (job_id,)
        ).fetchone()
        stop_row = connection.execute(
            "SELECT * FROM operator_job_stop_requests WHERE job_id = ?", (job_id,)
        ).fetchone()
        outcome_row = connection.execute(
            "SELECT * FROM operator_job_outcomes WHERE job_id = ?", (job_id,)
        ).fetchone()
        result_row = connection.execute(
            "SELECT * FROM operator_job_results WHERE job_id = ?", (job_id,)
        ).fetchone()
        cleanup_row = connection.execute(
            "SELECT * FROM operator_job_cleanup_receipts WHERE job_id = ?", (job_id,)
        ).fetchone()
        try:
            approval = (
                None
                if approval_row is None
                else OperatorJobApprovalRecord(
                    job_id=approval_row["job_id"],
                    plan_digest=approval_row["plan_digest"],
                    approval_scope_digest=approval_row["approval_scope_digest"],
                    approved_by_principal_id=approval_row[
                        "approved_by_principal_id"
                    ],
                    created_txn_id=approval_row["created_txn_id"],
                    created_at=approval_row["created_at"],
                )
            )
            launch = (
                None
                if launch_row is None
                else OperatorJobLaunchIntentRecord(
                    job_id=launch_row["job_id"],
                    plan_digest=launch_row["plan_digest"],
                    capacity_reservation_id=launch_row[
                        "capacity_reservation_id"
                    ],
                    capacity_holder_id=launch_row["capacity_holder_id"],
                    capacity_fencing_token=launch_row[
                        "capacity_fencing_token"
                    ],
                    admission_lease_id=launch_row["admission_lease_id"],
                    admission_holder_id=launch_row["admission_holder_id"],
                    admission_fencing_token=launch_row[
                        "admission_fencing_token"
                    ],
                    binding_id=launch_row["binding_id"],
                    launch_token=launch_row["launch_token"],
                    provider_kind=launch_row["provider_kind"],
                    evidence_fingerprint=launch_row["evidence_fingerprint"],
                    launch_request_digest=launch_row["launch_request_digest"],
                    created_by_principal_id=launch_row[
                        "created_by_principal_id"
                    ],
                    created_txn_id=launch_row["created_txn_id"],
                    created_at=launch_row["created_at"],
                )
            )
            stop = (
                None
                if stop_row is None
                else OperatorJobStopRecord(
                    job_id=stop_row["job_id"],
                    reason_code=stop_row["reason_code"],
                    requested_by_principal_id=stop_row[
                        "requested_by_principal_id"
                    ],
                    created_txn_id=stop_row["created_txn_id"],
                    created_at=stop_row["created_at"],
                )
            )
            outcome = (
                None
                if outcome_row is None
                else OperatorJobOutcomeRecord(
                    job_id=outcome_row["job_id"],
                    outcome=OperatorJobOutcome(
                        status=OperatorJobTerminalStatus(outcome_row["status"]),
                        code=outcome_row["code"],
                        started=bool(outcome_row["started"]),
                        disposition=OperatorJobTerminalDisposition(
                            outcome_row["disposition"]
                        ),
                        terminal_proof_digest=outcome_row[
                            "terminal_proof_digest"
                        ],
                        evidence_digest=outcome_row["evidence_digest"],
                        detail_digest=outcome_row["detail_digest"],
                    ),
                    created_by_principal_id=outcome_row[
                        "created_by_principal_id"
                    ],
                    created_txn_id=outcome_row["created_txn_id"],
                    created_at=outcome_row["created_at"],
                )
            )
            if outcome is not None and outcome_row[
                "outcome_digest"
            ] != hashlib.sha256(
                canonical_json_bytes(outcome.to_dict())
            ).hexdigest():
                raise RealmIntegrityError(
                    "Persisted Operator Job outcome digest is inconsistent."
                )
            result = (
                None
                if result_row is None
                else OperatorJobResultRecord(
                    job_id=result_row["job_id"],
                    result=OperatorJobResult.from_dict(
                        _json_object(result_row["result_json"], "operator job result")
                    ),
                    result_digest=result_row["result_digest"],
                    created_by_principal_id=result_row[
                        "created_by_principal_id"
                    ],
                    created_txn_id=result_row["created_txn_id"],
                    created_at=result_row["created_at"],
                )
            )
            cleanup = (
                None
                if cleanup_row is None
                else OperatorJobCleanupRecord(
                    job_id=cleanup_row["job_id"],
                    evidence=OperatorJobCleanupEvidence.from_dict(
                        _json_object(
                            cleanup_row["evidence_json"],
                            "operator job cleanup evidence",
                        )
                    ),
                    evidence_digest=cleanup_row["evidence_digest"],
                    created_by_principal_id=cleanup_row[
                        "created_by_principal_id"
                    ],
                    created_txn_id=cleanup_row["created_txn_id"],
                    created_at=cleanup_row["created_at"],
                )
            )
            record = OperatorJobRecord(
                job_id=row["job_id"],
                owner_id=row["owner_id"],
                plan=self._operator_job_plan_from_row(row),
                plan_digest=row["plan_digest"],
                state=OperatorJobState(row["state"]),
                reconciliation_state=OperatorJobReconciliationState(
                    row["reconciliation_state"]
                ),
                cleanup_state=OperatorJobCleanupState(row["cleanup_state"]),
                revision=row["revision"],
                created_by_principal_id=row["created_by_principal_id"],
                created_txn_id=row["created_txn_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                approval=approval,
                launch_intent=launch,
                stop=stop,
                outcome=outcome,
                result=result,
                cleanup=cleanup,
            )
            self._require_operator_job_terminal_capture(connection, record)
            return record
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("Persisted Operator Job is malformed.") from error

    @staticmethod
    def _require_operator_job_terminal_capture(
        connection: sqlite3.Connection,
        record: OperatorJobRecord,
    ) -> None:
        if not record.state.terminal or record.launch_intent is None:
            return
        if record.outcome is None or record.result is None:
            raise RealmIntegrityError(
                "Launched terminal Operator Job is missing its terminal evidence."
            )
        captures = connection.execute(
            "SELECT change_id FROM owner_transactions "
            "WHERE owner_id = ? AND state = 'committed' "
            "AND committed_txn_id = ?",
            (record.owner_id, record.outcome.created_txn_id),
        ).fetchall()
        if len(captures) != 1:
            raise RealmIntegrityError(
                "Launched terminal Operator Job lacks one exact capture commit."
            )
        additions = connection.execute(
            "SELECT store_id, content_ref, role "
            "FROM owner_transaction_additions WHERE change_id = ? "
            "ORDER BY store_id, content_ref, role",
            (captures[0]["change_id"],),
        ).fetchall()
        declared_refs = tuple(
            sorted(
                {
                    output.content_ref
                    for output in record.result.result.declared_outputs
                }
            )
        )
        addition_refs = tuple(sorted(row["content_ref"] for row in additions))
        if (
            len(set(addition_refs)) != len(addition_refs)
            or declared_refs != addition_refs
            or any(row["role"] != OPERATOR_JOB_OUTPUT_ROLE for row in additions)
        ):
            raise RealmIntegrityError(
                "Operator Job terminal capture differs from its declared outputs."
            )
        for row in additions:
            output_sizes = {
                output.size_bytes
                for output in record.result.result.declared_outputs
                if output.content_ref == row["content_ref"]
            }
            if len(output_sizes) != 1:
                raise RealmIntegrityError(
                    "Operator Job declarations disagree about shared output size."
                )
            output_size = next(iter(output_sizes))
            retained = connection.execute(
                "SELECT content.logical_bytes FROM owner_memberships membership "
                "JOIN content_objects content "
                "ON content.store_id = membership.store_id "
                "AND content.content_ref = membership.content_ref "
                "WHERE membership.owner_id = ? AND membership.store_id = ? "
                "AND membership.content_ref = ? AND membership.role = ? "
                "AND membership.added_txn_id = ? LIMIT 1",
                (
                    record.owner_id,
                    row["store_id"],
                    row["content_ref"],
                    row["role"],
                    record.outcome.created_txn_id,
                ),
            ).fetchone()
            if retained is None or int(retained["logical_bytes"]) != output_size:
                raise RealmIntegrityError(
                    "Operator Job terminal output was not captured with its exact size."
                )


def _operator_job_revision_from_row(row: sqlite3.Row) -> OperatorJobRevisionRecord:
    try:
        return OperatorJobRevisionRecord(
            job_id=row["job_id"],
            revision=row["revision"],
            state=OperatorJobState(row["state"]),
            reconciliation_state=OperatorJobReconciliationState(
                row["reconciliation_state"]
            ),
            cleanup_state=OperatorJobCleanupState(row["cleanup_state"]),
            operation_kind=row["operation_kind"],
            txn_id=row["txn_id"],
            created_at=row["created_at"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError("Persisted Operator Job revision is malformed.") from error


def _json_object(raw: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise RealmIntegrityError(f"Persisted {label} is not JSON text.")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError(f"Persisted {label} is invalid JSON.") from error
    if not isinstance(value, Mapping):
        raise RealmIntegrityError(f"Persisted {label} is not an object.")
    return value


def _optional_operator_job_text(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return required_text(value, label)


def _optional_operator_job_fence(
    identity: Any,
    holder: Any,
    fencing_token: Any,
    *,
    label: str,
) -> Optional[tuple[str, str, int]]:
    values = (identity, holder, fencing_token)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(f"{label} identity must be supplied as one exact fence.")
    return (
        required_text(identity, f"{label} id"),
        required_text(holder, f"{label} holder id"),
        positive_int(fencing_token, f"{label} fencing token"),
    )


def _normalize_operator_job_cleanup_states(
    states: Optional[Sequence[OperatorJobCleanupState]],
) -> Optional[Tuple[OperatorJobCleanupState, ...]]:
    if states is None:
        return None
    values = tuple(states)
    if not values or any(
        not isinstance(item, OperatorJobCleanupState) for item in values
    ):
        raise ValueError(
            "cleanup_states must contain OperatorJobCleanupState values."
        )
    return tuple(sorted(set(values), key=lambda item: item.value))


def _normalize_operator_job_memberships(
    memberships: Sequence[OwnerMembership],
) -> Tuple[OwnerMembership, ...]:
    if isinstance(memberships, (str, bytes)):
        raise TypeError(
            "operator job additions must be OwnerMembership values."
        )
    by_identity: dict[tuple[str, str, str], OwnerMembership] = {}
    for membership in memberships:
        if not isinstance(membership, OwnerMembership):
            raise TypeError(
                "operator job additions must contain OwnerMembership values."
            )
        identity = (
            membership.store_id,
            str(membership.content_ref),
            membership.role,
        )
        if identity in by_identity:
            raise ValueError("operator job additions must not contain duplicates.")
        by_identity[identity] = membership
    return tuple(by_identity[key] for key in sorted(by_identity))


__all__ = [
    "OperatorJobActorCursor",
    "OperatorJobActorPage",
    "OperatorJobLedgerMixin",
]
