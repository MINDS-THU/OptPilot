"""Actor-bound planning and execution of noncanonical Operator Jobs.

Candidate Debug Run is the first job kind.  Its durable lifecycle stays
general: immutable selection, no-copy derived owner, approved portable plan,
fenced admission, exact provider launch intent, retained result, and confirmed
terminal cleanup.  This module never manufactures a canonical run/trial and
never copies a workspace.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..attempts import AttemptEnvelope, CapturedArtifact, EvaluationSpec
from ..runtime_binding import (
    CandidateRuntimeInput,
    PortableAttemptRuntimeSpec,
    compile_retained_process_attempt_runtime,
)
from ._validation import lower_hex_digest, thaw_json
from .attempt_finalizer import OPERATOR_JOB_OUTPUT_ROLE, RealmAttemptFinalizer
from .errors import (
    ContentRejected,
    InterfaceOutputDrainPending,
    RealmCapacityUnavailable,
    RealmConflict,
    RealmError,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .environment_preview import (
    EnvironmentPreviewPlan,
    compile_environment_preview_plan,
)
from .environment_preview_binding import (
    EnvironmentPreviewOutputCaptureDescriptor,
    ManagedEnvironmentPreviewBinding,
    RealmEnvironmentPreviewBinder,
)
from .interface_output_records import (
    InterfaceOutputGenerationRecord,
    InterfaceOutputGenerationState,
    InterfaceOutputGenerationStatusRecord,
)
from .interface_output_service import (
    InterfaceOutputSessionHandle,
    RealmInterfaceOutputSessionService,
)
from .interface_outputs import InterfaceOutputRecord, InterfaceOutputRecordRejection
from .inspection_service import RealmInspectionTargetService
from .leases import LeaseRecord, LeaseState
from .ledger import PrincipalRecord, RealmLedger
from .local_attempt_launcher import (
    LocalAttemptPlatformError,
    ManagedLocalAttempt,
    RealmLocalAttemptLauncher,
)
from .local_attempt_protocol import LocalAttemptWorkerLog
from .local_process_supervisor import (
    ProcessLaunchReservation,
    ProcessLaunchSealReceipt,
    WorkerStarted,
    WorkerTerminalProof,
)
from .local_container_web_provider import (
    ContainerWebLaunchRequest,
    LocalContainerWebBrokerBinding,
    LocalContainerWebEndpoint,
    LocalContainerWebProvider,
    LocalContainerWebProviderError,
    LocalContainerWebTerminal,
)
from .operator_attempt_binding import (
    OPERATOR_JOB_RESOURCE_TTL_SECONDS,
    ManagedOperatorAttemptBinding,
    RealmOperatorAttemptBinder,
)
from .operator_capacity_records import (
    OperatorCapacityReservationRecord,
    OperatorCapacityReservationState,
    operator_capacity_reservation_id,
)
from .operator_job_records import (
    OperatorJobCleanupComponentEvidence,
    OperatorJobCleanupComponentState,
    OperatorJobCleanupEvidence,
    OperatorJobCleanupState,
    OperatorJobDeclaredOutput,
    OperatorJobLaunchPlan,
    OperatorJobLogMetadata,
    OperatorJobOutcome,
    OperatorJobRecord,
    OperatorJobResult,
    OperatorJobState,
    OperatorJobTarget,
    OperatorJobTerminalDisposition,
    OperatorJobTerminalStatus,
    operator_job_id,
)
from .owner_derivation import Binding, OwnerDerivationManifest
from .owners import OwnerChange, OwnerChangeState, OwnerMembership, OwnerPermission
from .process_provider import ProcessProviderIdentity
from .refs import canonical_json_bytes, parse_physical_content_ref, request_digest
from .selections import SelectionRef


CANDIDATE_DEBUG_JOB_KIND = "candidate-debug-run"
CANDIDATE_EVALUATION_TARGET_KIND = "candidate-evaluation"
ENVIRONMENT_PREVIEW_JOB_KIND = "environment-preview"
ENVIRONMENT_INTERFACE_TARGET_KIND = "environment-interface"
LOCAL_PROCESS_BACKEND_KIND = "local-process"
LOCAL_CONTAINER_WEB_BACKEND_KIND = "local-container-web"
LOCAL_PROCESS_BACKEND_REALM = "local-host"
_INPUT_FACTS_SCHEMA = "optpilot.candidate-debug-run-input.v2"
_PREVIEW_INPUT_FACTS_SCHEMA = "optpilot.environment-preview-input.v1"
_MIN_ADMISSION_TTL_SECONDS = 3600.0
_RECOVERY_MARGIN_SECONDS = 3600.0
_CAPTURE_TTL_SECONDS = 3600.0
_CAPACITY_HEARTBEAT_MAX_INTERVAL_SECONDS = 30.0
_TERMINAL_OWNER_CHANGE_MAX_GENERATIONS = 256


def control_plane_never_started_proof(record: OperatorJobRecord) -> str:
    """Derive the only cleanup proof for a control-plane job never launched."""

    if not isinstance(record, OperatorJobRecord):
        raise TypeError("record must be an OperatorJobRecord.")
    if record.launch_intent is not None:
        raise RealmConflict("Control-plane job has already retained launch authority.")
    return request_digest(
        {
            "job_id": record.job_id,
            "plan_digest": record.plan_digest,
            "schema": "optpilot.control-plane-job-never-started.v1",
        }
    )


class EnvironmentPreviewFinalCapturePending(RealmConflict):
    """A terminal Preview still has an exact output capture in flight.

    The provider is already quiescent, but terminal adoption must retain every
    resource until the durable generation leaves ``sealing``.  Callers may
    retry the same Operator Job; they must not manufacture a result or clean the
    output volume in response to this condition.
    """


@dataclass(frozen=True)
class _DebugExecutionContext:
    job_id: str
    owner_id: str
    selection: SelectionRef
    evaluation_spec: EvaluationSpec
    portable_spec: PortableAttemptRuntimeSpec
    derivation: OwnerDerivationManifest
    source_fingerprints: tuple[str, ...]
    requested_seed: Any
    attempt_id: str
    binding_id: str
    launch_token: str
    evidence_fingerprint: str


@dataclass(frozen=True)
class _PreviewExecutionContext:
    job_id: str
    owner_id: str
    selection: SelectionRef
    preview_plan: EnvironmentPreviewPlan
    derivation: OwnerDerivationManifest
    source_fingerprints: tuple[str, ...]
    binding_id: str
    launch_token: str
    evidence_fingerprint: str


class _OperatorCapacityHeartbeat:
    """Renew one exact reservation while its local worker remains live."""

    def __init__(
        self,
        *,
        service: "RealmOperatorJobService",
        record: OperatorJobRecord,
        reservation: OperatorCapacityReservationRecord,
        handle: ManagedLocalAttempt,
    ) -> None:
        self._service = service
        self._record = record
        self._reservation = reservation
        self._handle = handle
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"optpilot-capacity-{record.job_id[-12:]}",
            daemon=True,
        )

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    @property
    def reservation(self) -> OperatorCapacityReservationRecord:
        with self._lock:
            return self._reservation

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        failure = self.failure
        if failure is not None:
            raise RealmCapacityUnavailable(
                "Operator Job lost its fenced capacity while executing."
            ) from failure

    def _run(self) -> None:
        ttl_seconds = _capacity_ttl_seconds(self._record)
        interval = min(
            _CAPACITY_HEARTBEAT_MAX_INTERVAL_SECONDS,
            max(1.0, ttl_seconds / 3.0),
        )
        while not self._stop.is_set():
            try:
                current = self.reservation
                renewed = self._service._ledger.renew_operator_capacity_reservation(
                    operation_id=_operation(
                        self._record.job_id,
                        "renew-capacity/"
                        f"generation-{current.generation}/"
                        f"heartbeat-{current.heartbeat_revision + 1}",
                    ),
                    actor_principal_id=self._service.principal_id,
                    reservation_id=current.reservation_id,
                    holder_id=current.holder_id,
                    fencing_token=current.fencing_token,
                    ttl_seconds=ttl_seconds,
                )
                self._service._validate_capacity_record(self._record, renewed)
                with self._lock:
                    self._reservation = renewed
            except BaseException as error:
                with self._lock:
                    self._failure = error
                try:
                    self._handle.stop(grace_period=0.1, timeout=10.0)
                except BaseException:
                    pass
                return
            if self._stop.wait(interval):
                return


class RealmOperatorJobService:
    """Run actor-authorized noncanonical work through the durable job ledger."""

    def __init__(
        self,
        ledger: RealmLedger,
        principal: PrincipalRecord,
        inspection_service: RealmInspectionTargetService,
        provider: ProcessProviderIdentity,
        attempt_binder: RealmOperatorAttemptBinder,
        launcher: RealmLocalAttemptLauncher,
        finalizer: RealmAttemptFinalizer,
        interface_output_service: RealmInterfaceOutputSessionService | None = None,
        environment_preview_binder: RealmEnvironmentPreviewBinder | None = None,
        container_web_provider: LocalContainerWebProvider | None = None,
        container_web_broker_authority: object | None = None,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")
        if not isinstance(inspection_service, RealmInspectionTargetService):
            raise TypeError(
                "inspection_service must be a RealmInspectionTargetService."
            )
        if not isinstance(provider, ProcessProviderIdentity):
            raise TypeError("provider must be a ProcessProviderIdentity.")
        if not isinstance(attempt_binder, RealmOperatorAttemptBinder):
            raise TypeError("attempt_binder must be a RealmOperatorAttemptBinder.")
        if not isinstance(launcher, RealmLocalAttemptLauncher):
            raise TypeError("launcher must be a RealmLocalAttemptLauncher.")
        if not isinstance(finalizer, RealmAttemptFinalizer):
            raise TypeError("finalizer must be a RealmAttemptFinalizer.")
        if inspection_service._ledger is not ledger:
            raise ValueError("inspection service must share the Operator Job ledger.")
        if inspection_service.principal_id != principal.principal_id:
            raise ValueError("inspection service must use the Operator Job principal.")
        if attempt_binder._ledger is not ledger:
            raise ValueError("attempt binder must share the Operator Job ledger.")
        if finalizer._ledger is not ledger:
            raise ValueError("finalizer must share the Operator Job ledger.")
        if finalizer._actor_principal_id != principal.principal_id:
            raise ValueError("finalizer must use the Operator Job principal.")
        if interface_output_service is not None:
            if not isinstance(
                interface_output_service, RealmInterfaceOutputSessionService
            ):
                raise TypeError(
                    "interface_output_service must be a "
                    "RealmInterfaceOutputSessionService."
                )
            if interface_output_service._ledger is not ledger:
                raise ValueError(
                    "interface output service must share the Operator Job ledger."
                )
            if interface_output_service._actor != principal.principal_id:
                raise ValueError(
                    "interface output service must use the Operator Job principal."
                )
        preview_values = (
            environment_preview_binder,
            container_web_provider,
            container_web_broker_authority,
        )
        if any(value is not None for value in preview_values) and not all(
            value is not None for value in preview_values
        ):
            raise ValueError(
                "Environment Preview binder, provider, and broker authority "
                "must be configured together."
            )
        if environment_preview_binder is not None:
            if interface_output_service is None:
                raise ValueError(
                    "Environment Preview execution requires the shared interface "
                    "output service."
                )
            if not isinstance(
                environment_preview_binder, RealmEnvironmentPreviewBinder
            ):
                raise TypeError(
                    "environment_preview_binder must be a "
                    "RealmEnvironmentPreviewBinder."
                )
            if not isinstance(container_web_provider, LocalContainerWebProvider):
                raise TypeError(
                    "container_web_provider must be a LocalContainerWebProvider."
                )
            if environment_preview_binder._ledger is not ledger:
                raise ValueError(
                    "Environment Preview binder must share the Operator Job ledger."
                )
            if environment_preview_binder._provider is not container_web_provider:
                raise ValueError(
                    "Environment Preview binder and Operator Job service must "
                    "share one container provider."
                )
        self._ledger = ledger
        self._principal = principal
        self._inspection = inspection_service
        self._provider = provider
        self._attempt_binder = attempt_binder
        self._launcher = launcher
        self._finalizer = finalizer
        self._interface_outputs = interface_output_service
        self._environment_preview_binder = environment_preview_binder
        self._container_web_provider = container_web_provider
        self._container_web_broker_authority = container_web_broker_authority
        self._cleanup_lock = threading.RLock()
        self._preview_output_state_lock = threading.RLock()
        self._preview_output_locks: dict[str, threading.RLock] = {}
        self._active_preview_bindings: dict[
            str, ManagedEnvironmentPreviewBinding
        ] = {}
        self._active_preview_output_handles: dict[
            str, InterfaceOutputSessionHandle
        ] = {}

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    @property
    def environment_preview_available(self) -> bool:
        return self._environment_preview_binder is not None

    def plan_candidate_debug_run(
        self,
        *,
        operation_id: str,
        selection: SelectionRef,
        seed: Any = None,
        repetition_index: int = 0,
    ) -> OperatorJobRecord:
        """Resolve, plan, and approve one candidate Debug Run.

        The user action requesting a Debug Run is the approval action.  No
        command, path, provider option, secret, owner id, or launch coordinate
        is accepted from the presentation layer.
        """

        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be nonempty text.")
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if (
            isinstance(repetition_index, bool)
            or not isinstance(repetition_index, int)
            or repetition_index < 0
        ):
            raise ValueError("repetition_index must be a nonnegative integer.")

        job_id = operator_job_id(operation_id)
        owner_id = _job_owner_id(job_id)
        try:
            existing = self.read(job_id=job_id)
        except RealmNotFound:
            existing = None
        if existing is not None:
            context = self._context_for_record(existing)
            if (
                context.selection != selection
                or context.requested_seed != seed
                or context.evaluation_spec.repetition_index != repetition_index
            ):
                raise RealmConflict(
                    "Operator Job operation id already names a different Debug Run."
                )
            return self._ensure_job_approved(existing)

        context = self._compile_planning_context(
            job_id=job_id,
            owner_id=owner_id,
            selection=selection,
            seed=seed,
            repetition_index=repetition_index,
        )
        self._ledger.derive_owner(
            operation_id=_operation(job_id, "derive-owner"),
            actor_principal_id=self.principal_id,
            manifest=context.derivation,
        )
        plan = self._launch_plan(context)
        planned = self._ledger.plan_operator_job(
            operation_id=_operation(job_id, "plan"),
            actor_principal_id=self.principal_id,
            job_owner_id=owner_id,
            plan=plan,
            job_id=job_id,
        )
        return self._ensure_job_approved(planned)

    def plan_environment_preview(
        self,
        *,
        operation_id: str,
        selection: SelectionRef,
        profile_id: str | None = None,
    ) -> OperatorJobRecord:
        """Approve one contextual Preview from an exact retained selection.

        ``profile_id`` is only a selector among profiles already snapshotted
        into the run closure.  Runtime commands, images, grants, mounts,
        provider coordinates, and credentials are never request inputs.
        """

        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be nonempty text.")
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if profile_id is not None and not isinstance(profile_id, str):
            raise TypeError("profile_id must be text or None.")

        job_id = operator_job_id(operation_id)
        owner_id = _job_owner_id(job_id)
        try:
            existing = self.read(job_id=job_id)
        except RealmNotFound:
            existing = None
        if existing is not None:
            context = self._preview_context_for_record(existing)
            target = self._inspection.resolve_candidate(selection=selection)
            requested = compile_environment_preview_plan(target, profile_id)
            if (
                context.selection != selection
                or context.preview_plan != requested
            ):
                raise RealmConflict(
                    "Operator Job operation id already names a different "
                    "Environment Preview."
                )
            return self._ensure_job_approved(existing)

        context = self._compile_preview_planning_context(
            job_id=job_id,
            owner_id=owner_id,
            selection=selection,
            profile_id=profile_id,
        )
        self._ledger.derive_owner(
            operation_id=_operation(job_id, "derive-owner"),
            actor_principal_id=self.principal_id,
            manifest=context.derivation,
        )
        planned = self._ledger.plan_operator_job(
            operation_id=_operation(job_id, "plan"),
            actor_principal_id=self.principal_id,
            job_owner_id=owner_id,
            plan=self._preview_launch_plan(context),
            job_id=job_id,
        )
        return self._ensure_job_approved(planned)

    def _ensure_job_approved(self, record: OperatorJobRecord) -> OperatorJobRecord:
        current = record
        if current.state is OperatorJobState.PLANNED:
            current = self._ledger.request_operator_job_approval(
                operation_id=_operation(current.job_id, "request-approval"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
            )
        if current.state is OperatorJobState.AWAITING_APPROVAL:
            current = self._ledger.approve_operator_job(
                operation_id=_operation(current.job_id, "approve"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
                expected_plan_digest=current.plan_digest,
                approval_scope_digest=request_digest(
                    {
                        "action": current.plan.job_kind,
                        "actor_principal_id": self.principal_id,
                        "job_id": current.job_id,
                        "plan_digest": current.plan_digest,
                        "schema": "optpilot.operator-job-approval-scope.v1",
                    }
                ),
            )
        return current

    def execute(self, *, job_id: str) -> OperatorJobRecord:
        """Start, adopt, or reconcile one approved job to a terminal head."""

        while True:
            record = self.read(job_id=job_id)
            if record.state.terminal:
                self._reconcile_terminal_cleanup(record)
                return self.read(job_id=job_id)
            if record.state in {
                OperatorJobState.PLANNED,
                OperatorJobState.AWAITING_APPROVAL,
            }:
                raise RealmConflict("Operator Job has not been approved.")
            if record.state is OperatorJobState.QUEUED:
                if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                    return self._execute_preview_queued(record)
                return self._execute_queued(record)
            if record.state in {
                OperatorJobState.STARTING,
                OperatorJobState.RUNNING,
            }:
                if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                    return self._resume_preview(record)
                return self._resume_launched(record)
            if record.state is OperatorJobState.STOPPING:
                if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                    return self._finish_preview_stopping(record)
                return self._finish_stopping(record)
            raise RealmIntegrityError("Operator Job state is unsupported.")

    def request_stop(
        self,
        *,
        operation_id: str,
        job_id: str,
        reason_code: str = "operator_requested",
    ) -> OperatorJobRecord:
        """Request cancellation and reconcile all currently retained authority."""

        current = self.read(job_id=job_id)
        try:
            stopped = self._ledger.request_operator_job_stop(
                operation_id=operation_id,
                actor_principal_id=self.principal_id,
                job_id=job_id,
                expected_revision=current.revision,
                reason_code=reason_code,
            )
        except RealmConflict:
            stopped = self.read(job_id=job_id)
            if not stopped.state.terminal and stopped.state is not OperatorJobState.STOPPING:
                raise
        if stopped.state is OperatorJobState.CANCELLED:
            if stopped.launch_intent is None:
                if stopped.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                    self._cleanup_unlaunched_preview_cancellation(stopped)
                else:
                    self._cleanup_unlaunched_cancellation(stopped)
            return self.read(job_id=job_id)
        if stopped.state.terminal:
            self._reconcile_terminal_cleanup(stopped)
            return self.read(job_id=job_id)
        if stopped.state is OperatorJobState.STOPPING:
            if stopped.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                return self._finish_preview_stopping(stopped)
            return self._finish_stopping(stopped)
        return stopped

    def read(self, *, job_id: str) -> OperatorJobRecord:
        return self._ledger.read_operator_job(
            actor_principal_id=self.principal_id,
            job_id=job_id,
        )

    def begin_control_plane_start(
        self,
        *,
        job_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> OperatorJobRecord:
        """Commit startup authority for a Core-owned control-plane job.

        This is the provider-neutral counterpart to the Debug/Preview launch
        paths.  It reserves the approved logical capacity and retains the
        exact source closure before recording the launch intent, but it does
        not start a process or infer any provider-private coordinate.  The
        caller remains responsible for the typed domain handoff that the plan
        names (for example, creating a canonical run controller).
        """

        current = self.read(job_id=job_id)
        if current.state is not OperatorJobState.QUEUED:
            if current.state in {
                OperatorJobState.STARTING,
                OperatorJobState.RUNNING,
                OperatorJobState.STOPPING,
            } or current.state.terminal:
                return current
            raise RealmConflict("Control-plane Operator Job is not approved.")
        self._ensure_capacity(current)
        admission = self._ensure_admission(current)
        try:
            return self._ledger.begin_operator_job_start(
                operation_id=_operation(current.job_id, "begin-control-plane-start"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
                admission_lease_id=admission.lease_id,
                admission_holder_id=admission.holder_id,
                admission_fencing_token=admission.fencing_token,
                binding_id=binding_id,
                launch_token=launch_token,
                provider_kind=current.plan.backend_kind,
                evidence_fingerprint=evidence_fingerprint,
                launch_request_digest=launch_request_digest,
            )
        except BaseException:
            # Capacity/admission are durable and intentionally left for exact
            # replay or terminal cleanup.  Releasing here could race a
            # successfully committed begin-start response.
            raise

    def mark_control_plane_running(self, *, job_id: str) -> OperatorJobRecord:
        """Confirm that a Core-owned job crossed its typed startup handoff."""

        current = self.read(job_id=job_id)
        if current.state is not OperatorJobState.STARTING:
            return current
        intent = _require_launch_intent(current)
        return self._ledger.mark_operator_job_running(
            operation_id=_operation(current.job_id, "mark-control-plane-running"),
            actor_principal_id=self.principal_id,
            job_id=current.job_id,
            expected_revision=current.revision,
            launch_token=intent.launch_token,
            admission_lease_id=intent.admission_lease_id,
            admission_fencing_token=intent.admission_fencing_token,
        )

    def finish_control_plane_job(
        self,
        *,
        job_id: str,
        result: OperatorJobResult,
        status: OperatorJobTerminalStatus,
        code: str,
        terminal_proof_digest: str,
        started: bool = True,
    ) -> OperatorJobRecord:
        """Finish a launched Core job without manufacturing provider output.

        ``terminal_proof_digest`` authenticates the typed domain fact that
        ended startup (such as a fenced controller term).  No filesystem
        output is captured; the path-free ``OperatorJobResult`` remains the
        durable evidence projection.
        """

        if not isinstance(result, OperatorJobResult):
            raise TypeError("result must be an OperatorJobResult.")
        if not isinstance(status, OperatorJobTerminalStatus):
            raise TypeError("status must be an OperatorJobTerminalStatus.")
        terminal_proof_digest = lower_hex_digest(
            terminal_proof_digest,
            "control-plane terminal proof digest",
        )
        if not isinstance(started, bool):
            raise TypeError("started must be a boolean.")
        current = self.read(job_id=job_id)
        if current.state.terminal:
            return self.complete_control_plane_cleanup(
                job_id=current.job_id,
                provider_evidence_digest=terminal_proof_digest,
            )
        intent = _require_launch_intent(current)
        change = self._begin_terminal_owner_change(
            current,
            operation_phase="control-plane-terminal-change",
            base_change_id=_logical_id(
                "operator-job-control-plane-terminal-change",
                {"job_id": current.job_id},
            ),
        )
        outcome = OperatorJobOutcome(
            status=status,
            code=code,
            started=started,
            disposition=(
                OperatorJobTerminalDisposition.NEVER_STARTED
                if not started
                else (
                    OperatorJobTerminalDisposition.EXITED
                    if status is not OperatorJobTerminalStatus.CANCELLED
                    else OperatorJobTerminalDisposition.KILLED
                )
            ),
            terminal_proof_digest=terminal_proof_digest,
            evidence_digest=result.digest,
            detail_digest=(
                request_digest(result.to_dict()["details"])
                if result.details
                else None
            ),
        )
        try:
            terminal = self._ledger.finish_operator_job(
                operation_id=_operation(current.job_id, "finish-control-plane"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
                launch_token=intent.launch_token,
                admission_lease_id=intent.admission_lease_id,
                admission_fencing_token=intent.admission_fencing_token,
                change_id=change.change_id,
                expected_owner_revision=0,
                additions=(),
                outcome=outcome,
                result=result,
            )
        except RealmConflict:
            current = self.read(job_id=current.job_id)
            if not current.state.terminal:
                raise
            terminal = current
        return self.complete_control_plane_cleanup(
            job_id=terminal.job_id,
            provider_evidence_digest=terminal_proof_digest,
        )

    def confirm_study_launch_controller(
        self,
        *,
        job_id: str,
        controller_lease_id: str,
        controller_holder_id: str,
        controller_fencing_token: int,
        controller_generation: int,
    ) -> OperatorJobRecord:
        """Confirm the only valid terminal outcome after a study handoff.

        Generic control-plane finish remains useful before a handoff and for
        other Core-owned job kinds.  Once a study launch has handed authority
        to a canonical Run, this typed command is its only terminal seam.
        """

        current = self.read(job_id=job_id)
        if current.state.terminal:
            confirmation = (
                self._ledger.read_study_launch_controller_confirmation(
                    actor_principal_id=self.principal_id,
                    job_id=job_id,
                )
            )
            if (
                current.state is not OperatorJobState.SUCCEEDED
                or confirmation.controller_lease_id != controller_lease_id
                or confirmation.controller_holder_id != controller_holder_id
                or confirmation.controller_fencing_token
                != controller_fencing_token
                or confirmation.controller_generation != controller_generation
            ):
                raise RealmConflict(
                    "Study launch already terminalized under another controller."
                )
            return self.complete_control_plane_cleanup(
                job_id=job_id,
                provider_evidence_digest=confirmation.terminal_proof_digest,
            )
        if current.state is not OperatorJobState.RUNNING:
            raise RealmConflict(
                "Study launch controller confirmation requires a running job."
            )
        change = self._begin_terminal_owner_change(
            current,
            operation_phase="control-plane-terminal-change",
            base_change_id=_logical_id(
                "operator-job-control-plane-terminal-change",
                {"job_id": current.job_id},
            ),
        )
        try:
            terminal = self._ledger.confirm_study_launch_controller(
                operation_id=_operation(
                    current.job_id, "confirm-study-launch-controller"
                ),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_job_revision=current.revision,
                controller_lease_id=controller_lease_id,
                controller_holder_id=controller_holder_id,
                controller_fencing_token=controller_fencing_token,
                controller_generation=controller_generation,
                change_id=change.change_id,
                expected_owner_revision=0,
            )
        except RealmConflict:
            latest = self.read(job_id=current.job_id)
            if not latest.state.terminal:
                raise
            confirmation = (
                self._ledger.read_study_launch_controller_confirmation(
                    actor_principal_id=self.principal_id,
                    job_id=current.job_id,
                )
            )
            if (
                latest.state is not OperatorJobState.SUCCEEDED
                or confirmation.controller_lease_id != controller_lease_id
                or confirmation.controller_holder_id != controller_holder_id
                or confirmation.controller_fencing_token
                != controller_fencing_token
                or confirmation.controller_generation != controller_generation
            ):
                raise RealmConflict(
                    "Study launch terminal race confirmed another controller."
                )
            terminal = latest
        confirmation = self._ledger.read_study_launch_controller_confirmation(
            actor_principal_id=self.principal_id,
            job_id=terminal.job_id,
        )
        return self.complete_control_plane_cleanup(
            job_id=terminal.job_id,
            provider_evidence_digest=confirmation.terminal_proof_digest,
        )

    def complete_control_plane_cleanup(
        self,
        *,
        job_id: str,
        provider_evidence_digest: str,
    ) -> OperatorJobRecord:
        """Release startup-only admission/capacity for a terminal Core job."""

        provider_evidence_digest = lower_hex_digest(
            provider_evidence_digest,
            "control-plane provider evidence digest",
        )
        current = self.read(job_id=job_id)
        if not current.state.terminal:
            raise RealmConflict("Control-plane cleanup requires a terminal job.")
        intent = current.launch_intent
        expected_proof = (
            control_plane_never_started_proof(current)
            if intent is None
            else (
                None
                if current.outcome is None
                else current.outcome.outcome.terminal_proof_digest
            )
        )
        if current.outcome is None or expected_proof != provider_evidence_digest:
            raise RealmConflict(
                "Control-plane cleanup proof differs from its terminal outcome."
            )
        if current.cleanup_state is OperatorJobCleanupState.COMPLETE:
            return current
        admission = None
        if intent is not None:
            try:
                admission = self._ledger.release_lease(
                    operation_id=_operation(current.job_id, "release-control-plane-admission"),
                    actor_principal_id=self.principal_id,
                    lease_id=intent.admission_lease_id,
                    holder_id=intent.admission_holder_id,
                    fencing_token=intent.admission_fencing_token,
                )
            except RealmNotFound:
                admission = None
        else:
            # Cancellation can win after deterministic startup admission was
            # retained but before begin-start committed its launch intent.
            # Prove and release that coordinate instead of leaking authority.
            try:
                retained = self._validate_deterministic_admission(current)
                admission = self._ledger.release_lease(
                    operation_id=_operation(
                        current.job_id, "release-passive-control-plane-admission"
                    ),
                    actor_principal_id=self.principal_id,
                    lease_id=retained.lease_id,
                    holder_id=retained.holder_id,
                    fencing_token=retained.fencing_token,
                )
            except RealmError:
                admission = None
        return self._complete_cleanup(
            current,
            provider_evidence_digest=provider_evidence_digest,
            resources_evidence_digest=request_digest(
                {
                    "job_id": current.job_id,
                    "resource_state": "no_external_resources",
                    "schema": "optpilot.control-plane-job-resource-cleanup.v1",
                    "terminal_proof_digest": provider_evidence_digest,
                }
            ),
            admission=admission,
        )

    def list_for_run(
        self,
        *,
        source_owner_id: str,
        run_id: str,
        states: Sequence[OperatorJobState] | None = None,
        cleanup_states: Sequence[OperatorJobCleanupState] | None = None,
        limit: int = 100,
    ) -> tuple[OperatorJobRecord, ...]:
        return self._ledger.list_operator_jobs_for_source(
            actor_principal_id=self.principal_id,
            source_owner_id=source_owner_id,
            source_kind="run",
            source_id=run_id,
            states=states,
            cleanup_states=cleanup_states,
            limit=limit,
        )

    def acquire_environment_preview_broker_binding(
        self, *, job_id: str
    ) -> LocalContainerWebBrokerBinding:
        """Mint a private upstream binding only for a currently running Preview.

        The returned credential-bearing object is intended for the in-process
        presentation broker.  It is never a public Operator Job record and is
        invalidated by stop, terminalization, provider-generation change, or
        administrator trust removal.
        """

        record = self.read(job_id=job_id)
        if (
            record.plan.job_kind != ENVIRONMENT_PREVIEW_JOB_KIND
            or record.state is not OperatorJobState.RUNNING
        ):
            raise RealmConflict(
                "Environment Preview presentation requires a running Preview job."
            )
        binder, provider, authority = self._preview_execution_dependencies()
        context = self._preview_context_for_record(record)
        admission = self._validate_launch_admission(record)
        target = self._inspection.resolve_candidate(selection=context.selection)
        managed = binder.recover_existing(
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            owner_id=record.owner_id,
            admission_lease=admission,
            operator_plan_digest=record.plan_digest,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            target=target,
            preview_plan=context.preview_plan,
            ttl_seconds=_resource_ttl_seconds(record),
        )
        self._validate_preview_launch_request(record, managed)
        observed = provider.start_or_adopt(managed.request)
        if not isinstance(observed, LocalContainerWebEndpoint):
            raise RealmConflict("Environment Preview is no longer running.")
        managed.validate()
        return provider.acquire_broker_binding(observed, authority=authority)

    def list_environment_preview_output_statuses(
        self, *, job_id: str
    ) -> tuple[InterfaceOutputGenerationStatusRecord, ...]:
        """Return path-free live output state for one authorized Preview job.

        Terminal Preview outputs are read from ``OperatorJobResult`` instead;
        their launch-scoped output session is deliberately retired during
        terminal cleanup.
        """

        record = self.read(job_id=job_id)
        if record.plan.job_kind != ENVIRONMENT_PREVIEW_JOB_KIND:
            raise RealmConflict("Operator Job is not an Environment Preview.")
        if not self._preview_context_for_record(record).preview_plan.outputs_enabled:
            return ()
        if record.state.terminal:
            return ()
        try:
            handle = self._preview_output_service().recover_session(
                launch_id=record.job_id
            )
        except RealmNotFound:
            return ()
        return self._preview_output_service().list_statuses(handle=handle)

    def retry_environment_preview_output(
        self, *, job_id: str, output_id: str
    ) -> InterfaceOutputGenerationStatusRecord:
        """Retry one failed generation only while its Preview is supervised.

        The caller supplies domain ids only.  Filesystem authority comes from
        the process-local managed binding retained by the supervisor.
        """

        if not isinstance(output_id, str) or not output_id:
            raise ValueError("output_id must be nonempty text.")
        lock = self._preview_output_lock(job_id)
        with lock:
            record = self.read(job_id=job_id)
            if record.plan.job_kind != ENVIRONMENT_PREVIEW_JOB_KIND:
                raise RealmConflict("Operator Job is not an Environment Preview.")
            if not self._preview_context_for_record(
                record
            ).preview_plan.outputs_enabled:
                raise RealmConflict(
                    "This Environment Preview profile does not declare outputs."
                )
            if record.state not in {
                OperatorJobState.STARTING,
                OperatorJobState.RUNNING,
            }:
                raise RealmConflict(
                    "Preview output capture can be retried only while the runtime is live."
                )
            with self._preview_output_state_lock:
                managed = self._active_preview_bindings.get(job_id)
                handle = self._active_preview_output_handles.get(job_id)
            if managed is None or handle is None:
                raise RealmConflict(
                    "Preview output retry is unavailable until this supervisor "
                    "adopts the live runtime."
                )
            service = self._preview_output_service()
            statuses = {
                item.output_id: item
                for item in service.list_statuses(handle=handle)
            }
            status = statuses.get(output_id)
            if status is None:
                raise RealmNotFound("Interface output generation was not found.")
            descriptor = managed.output_capture_descriptor
            roots = {"output": descriptor.source_root}
            if status.state is InterfaceOutputGenerationState.SEALING:
                service.resume_generation(
                    handle=handle,
                    output_id=output_id,
                    root_handles=roots,
                )
            elif status.state is InterfaceOutputGenerationState.FAILED:
                service.capture_generation(
                    handle=handle,
                    record=status.record,
                    root_handles=roots,
                )
            return next(
                item
                for item in service.list_statuses(handle=handle)
                if item.output_id == output_id
            )

    # -- plan reconstruction ---------------------------------------------

    def _compile_planning_context(
        self,
        *,
        job_id: str,
        owner_id: str,
        selection: SelectionRef,
        seed: Any,
        repetition_index: int,
    ) -> _DebugExecutionContext:
        target = self._inspection.resolve_candidate(selection=selection)
        if not target.runnable:
            raise RealmConflict("Selected candidate evaluation content is unavailable.")
        evaluation_spec = target.compile_evaluation_spec(
            seed=seed,
            repetition_index=repetition_index,
            metadata={"mode": CANDIDATE_DEBUG_JOB_KIND},
        )
        candidate_input = CandidateRuntimeInput.from_envelope(
            target.candidate.admission.envelope
        )
        portable_spec = compile_retained_process_attempt_runtime(
            owner_id=owner_id,
            run_definition=target.run_definition,
            evaluation_spec=evaluation_spec,
            provider=self._provider,
            candidate_input=candidate_input,
        )
        anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=self.principal_id,
            owner_id=selection.source_owner_id,
            revision=selection.owner_revision,
        )
        memberships = _select_source_memberships(
            (*target.candidate_bindings, *target.evaluation.content_bindings)
        )
        derivation = OwnerDerivationManifest(
            target_owner_id=owner_id,
            target_owner_kind="operator-job",
            sources=(anchor,),
            bindings=tuple(
                Binding(
                    source_owner_id=selection.source_owner_id,
                    source_store_id=item.store_id,
                    content_ref=item.content_ref,
                    source_role=item.role,
                    target_role=item.role,
                )
                for item in memberships
            ),
        )
        attempt_id, binding_id, launch_token = _execution_identities(job_id)
        evidence_fingerprint = request_digest(
            {
                "binding_id": binding_id,
                "evaluation_spec_digest": evaluation_spec.digest,
                "job_id": job_id,
                "portable_spec_digest": portable_spec.digest,
                "schema": "optpilot.operator-job-execution-evidence.v1",
            }
        )
        closure = target.run_definition.evaluation_closure
        source_fingerprints = tuple(
            sorted(
                {
                    target.selection.selection_digest,
                    target.run_definition.digest,
                    closure.environment_revision.digest,
                    closure.prepared_runtime.digest,
                    portable_spec.digest,
                    _plain_digest(evaluation_spec.digest),
                    request_digest(target.candidate.to_dict()),
                }
            )
        )
        return _DebugExecutionContext(
            job_id=job_id,
            owner_id=owner_id,
            selection=target.selection,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            derivation=derivation,
            source_fingerprints=source_fingerprints,
            requested_seed=seed,
            attempt_id=attempt_id,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
        )

    def _launch_plan(
        self, context: _DebugExecutionContext
    ) -> OperatorJobLaunchPlan:
        evaluation = context.evaluation_spec
        portable = context.portable_spec
        facts = {
            "evaluation_spec": evaluation.to_dict(),
            "portable_runtime_spec": portable.to_dict(),
            "requested_seed": context.requested_seed,
            "schema": _INPUT_FACTS_SCHEMA,
        }
        resources = {
            "cpu_millis": portable.resources.cpu * 1000,
            "memory_bytes": portable.resources.memory_gib * 1024**3,
        }
        if portable.resources.gpu:
            resources["gpu_count"] = portable.resources.gpu
        return OperatorJobLaunchPlan(
            job_kind=CANDIDATE_DEBUG_JOB_KIND,
            target=OperatorJobTarget(
                kind=CANDIDATE_EVALUATION_TARGET_KIND,
                selection=context.selection,
            ),
            input_facts=facts,
            input_facts_digest=hashlib.sha256(
                canonical_json_bytes(facts)
            ).hexdigest(),
            owner_derivation_manifest_digest=context.derivation.digest,
            source_fingerprints=context.source_fingerprints,
            runtime_fingerprint=portable.digest,
            entrypoint_profile="python-callable",
            projection_contract_digest=portable.projection_spec.digest,
            backend_kind=LOCAL_PROCESS_BACKEND_KIND,
            backend_realm=LOCAL_PROCESS_BACKEND_REALM,
            resource_claims=resources,
            timeout_seconds=portable.timeout_seconds or 300.0,
            network_policy="denied",
            network_enforcement="advisory",
            requested_secret_names=(),
            grants_digest=request_digest(
                {
                    "network_policy": "denied",
                    "network_enforcement": "advisory",
                    "requested_secret_names": [],
                    "schema": "optpilot.operator-job-grants.v1",
                }
            ),
            evidence_sink_kind="operator-job-result",
            evidence_sink_id=context.attempt_id,
            evidence_sink_digest=request_digest(
                {
                    "job_id": context.job_id,
                    "result_kind": CANDIDATE_DEBUG_JOB_KIND,
                    "schema": "optpilot.operator-job-evidence-sink.v1",
                }
            ),
            cancellation_guarantee="confirmed",
            priority_class="interactive",
        )

    def _compile_preview_planning_context(
        self,
        *,
        job_id: str,
        owner_id: str,
        selection: SelectionRef,
        profile_id: str | None,
    ) -> _PreviewExecutionContext:
        target = self._inspection.resolve_candidate(selection=selection)
        preview_plan = compile_environment_preview_plan(target, profile_id)
        anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=self.principal_id,
            owner_id=selection.source_owner_id,
            revision=selection.owner_revision,
        )
        memberships = _select_source_memberships(
            (*target.candidate_bindings, *target.evaluation.content_bindings)
        )
        derivation = OwnerDerivationManifest(
            target_owner_id=owner_id,
            target_owner_kind="operator-job",
            sources=(anchor,),
            bindings=tuple(
                Binding(
                    source_owner_id=selection.source_owner_id,
                    source_store_id=item.store_id,
                    content_ref=item.content_ref,
                    source_role=item.role,
                    target_role=item.role,
                )
                for item in memberships
            ),
        )
        binding_id, launch_token = _preview_execution_identities(job_id)
        evidence_fingerprint = request_digest(
            {
                "binding_id": binding_id,
                "job_id": job_id,
                "preview_plan_digest": preview_plan.digest,
                "schema": "optpilot.environment-preview-execution-evidence.v1",
            }
        )
        fingerprints = preview_plan.fingerprints
        source_fingerprints = tuple(
            sorted(
                {
                    preview_plan.digest,
                    fingerprints.source,
                    fingerprints.runtime,
                    fingerprints.candidate,
                    fingerprints.evaluation,
                    fingerprints.run_definition,
                    fingerprints.selection,
                }
            )
        )
        return _PreviewExecutionContext(
            job_id=job_id,
            owner_id=owner_id,
            selection=selection,
            preview_plan=preview_plan,
            derivation=derivation,
            source_fingerprints=source_fingerprints,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
        )

    def _preview_launch_plan(
        self, context: _PreviewExecutionContext
    ) -> OperatorJobLaunchPlan:
        preview = context.preview_plan
        facts = {
            "preview_plan": preview.to_dict(),
            "schema": _PREVIEW_INPUT_FACTS_SCHEMA,
        }
        resources = {
            "cpu_millis": preview.resources.cpu_millis,
            "memory_bytes": preview.resources.memory_bytes,
        }
        if preview.resources.gpu_count:
            resources["gpu_count"] = preview.resources.gpu_count
        return OperatorJobLaunchPlan(
            job_kind=ENVIRONMENT_PREVIEW_JOB_KIND,
            target=OperatorJobTarget(
                kind=ENVIRONMENT_INTERFACE_TARGET_KIND,
                selection=context.selection,
            ),
            input_facts=facts,
            input_facts_digest=hashlib.sha256(
                canonical_json_bytes(facts)
            ).hexdigest(),
            owner_derivation_manifest_digest=context.derivation.digest,
            source_fingerprints=context.source_fingerprints,
            runtime_fingerprint=preview.fingerprints.runtime,
            entrypoint_profile=preview.profile_id,
            projection_contract_digest=request_digest(
                {
                    "logical_paths": preview.paths.to_dict(),
                    "preview_plan_digest": preview.digest,
                    "schema": "optpilot.environment-preview-projection-contract.v1",
                    "source_fingerprint": preview.fingerprints.source,
                }
            ),
            backend_kind=LOCAL_CONTAINER_WEB_BACKEND_KIND,
            backend_realm=LOCAL_PROCESS_BACKEND_REALM,
            resource_claims=resources,
            timeout_seconds=preview.timeout_seconds,
            network_policy="denied",
            network_enforcement="enforced",
            requested_secret_names=(),
            grants_digest=request_digest(
                {
                    "network_policy": "denied",
                    "network_enforcement": "enforced",
                    "requested_secret_names": [],
                    "schema": "optpilot.operator-job-grants.v1",
                }
            ),
            evidence_sink_kind="operator-job-result",
            evidence_sink_id=_preview_result_id(context.job_id),
            evidence_sink_digest=request_digest(
                {
                    "job_id": context.job_id,
                    "result_kind": ENVIRONMENT_PREVIEW_JOB_KIND,
                    "schema": "optpilot.operator-job-evidence-sink.v1",
                }
            ),
            cancellation_guarantee="confirmed",
            priority_class="interactive",
        )

    def _preview_context_for_record(
        self, record: OperatorJobRecord
    ) -> _PreviewExecutionContext:
        if (
            record.plan.job_kind != ENVIRONMENT_PREVIEW_JOB_KIND
            or record.plan.target.kind != ENVIRONMENT_INTERFACE_TARGET_KIND
        ):
            raise RealmConflict("Operator Job is not an Environment Preview.")
        facts = dict(record.plan.input_facts)
        if set(facts) != {"preview_plan", "schema"} or facts.get(
            "schema"
        ) != _PREVIEW_INPUT_FACTS_SCHEMA:
            raise RealmIntegrityError(
                "Environment Preview retained input facts are unsupported."
            )
        try:
            preview_plan = EnvironmentPreviewPlan.from_dict(
                thaw_json(facts["preview_plan"])
            )
        except (TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Environment Preview retained plan is malformed."
            ) from error
        selection = record.plan.target.selection
        if preview_plan.selection != selection:
            raise RealmIntegrityError(
                "Environment Preview retained plan differs from its selection."
            )
        derivation = self._ledger.read_owner_derivation(
            actor_principal_id=self.principal_id,
            owner_id=record.owner_id,
        )
        if (
            derivation.target_owner_id != record.owner_id
            or derivation.target_owner_kind != "operator-job"
            or len(derivation.sources) != 1
            or derivation.sources[0].owner_id != selection.source_owner_id
            or derivation.sources[0].owner_revision != selection.owner_revision
        ):
            raise RealmIntegrityError(
                "Environment Preview owner derivation differs from its selection."
            )
        binding_id, launch_token = _preview_execution_identities(record.job_id)
        context = _PreviewExecutionContext(
            job_id=record.job_id,
            owner_id=record.owner_id,
            selection=selection,
            preview_plan=preview_plan,
            derivation=derivation,
            source_fingerprints=record.plan.source_fingerprints,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=request_digest(
                {
                    "binding_id": binding_id,
                    "job_id": record.job_id,
                    "preview_plan_digest": preview_plan.digest,
                    "schema": "optpilot.environment-preview-execution-evidence.v1",
                }
            ),
        )
        if self._preview_launch_plan(context) != record.plan:
            raise RealmIntegrityError(
                "Environment Preview Operator Job differs from its retained plan."
            )
        return context

    def _context_for_record(
        self, record: OperatorJobRecord
    ) -> _DebugExecutionContext:
        if (
            record.plan.job_kind != CANDIDATE_DEBUG_JOB_KIND
            or record.plan.target.kind != CANDIDATE_EVALUATION_TARGET_KIND
        ):
            raise RealmConflict("Operator Job kind is not supported by this service.")
        facts = dict(record.plan.input_facts)
        expected_fields = {
            "evaluation_spec",
            "portable_runtime_spec",
            "requested_seed",
            "schema",
        }
        if set(facts) != expected_fields or facts["schema"] != _INPUT_FACTS_SCHEMA:
            raise RealmIntegrityError("Operator Job input facts are unsupported.")
        try:
            evaluation_spec = EvaluationSpec.from_dict(
                thaw_json(facts["evaluation_spec"])
            )
            portable_spec = PortableAttemptRuntimeSpec.from_dict(
                thaw_json(facts["portable_runtime_spec"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Operator Job retained execution specs are malformed."
            ) from error
        selection = record.plan.target.selection
        provider = portable_spec.provider
        if (
            portable_spec.evaluation_spec_digest != evaluation_spec.digest
            or portable_spec.environment_revision_digest
            != evaluation_spec.environment_revision_digest
            or portable_spec.prepared_runtime_digest
            != evaluation_spec.prepared_runtime_digest
            or portable_spec.projection_spec.owner_id != record.owner_id
            or evaluation_spec.candidate_ref != selection.entity_ref
            or evaluation_spec.candidate_id != selection.entity_id
            or provider.kind != "process"
            or provider.platform != self._provider.platform
            or provider.builder_fingerprint != self._provider.builder_fingerprint
        ):
            raise RealmIntegrityError(
                "Operator Job retained execution specs differ from its target or provider."
            )
        persisted_derivation = self._ledger.read_owner_derivation(
            actor_principal_id=self.principal_id,
            owner_id=record.owner_id,
        )
        if (
            persisted_derivation.target_owner_id != record.owner_id
            or persisted_derivation.target_owner_kind != "operator-job"
            or len(persisted_derivation.sources) != 1
            or persisted_derivation.sources[0].owner_id != selection.source_owner_id
            or persisted_derivation.sources[0].owner_revision
            != selection.owner_revision
        ):
            raise RealmIntegrityError(
                "Operator Job owner derivation differs from its immutable selection."
            )
        attempt_id, binding_id, launch_token = _execution_identities(record.job_id)
        context = _DebugExecutionContext(
            job_id=record.job_id,
            owner_id=record.owner_id,
            selection=selection,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            derivation=persisted_derivation,
            source_fingerprints=record.plan.source_fingerprints,
            requested_seed=thaw_json(facts["requested_seed"]),
            attempt_id=attempt_id,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=request_digest(
                {
                    "binding_id": binding_id,
                    "evaluation_spec_digest": evaluation_spec.digest,
                    "job_id": record.job_id,
                    "portable_spec_digest": portable_spec.digest,
                    "schema": "optpilot.operator-job-execution-evidence.v1",
                }
            ),
        )
        if self._launch_plan(context) != record.plan:
            raise RealmIntegrityError(
                "Operator Job plan differs from its immutable resolution."
            )
        if persisted_derivation != context.derivation:
            raise RealmIntegrityError(
                "Operator Job owner derivation differs from its approved plan."
            )
        return context

    # -- launch and recovery ---------------------------------------------

    def _preview_execution_dependencies(
        self,
    ) -> tuple[
        RealmEnvironmentPreviewBinder,
        LocalContainerWebProvider,
        object,
    ]:
        binder = self._environment_preview_binder
        provider = self._container_web_provider
        authority = self._container_web_broker_authority
        if binder is None or provider is None or authority is None:
            raise RealmConflict(
                "Environment Preview execution provider is not configured."
            )
        return binder, provider, authority

    def _preview_output_service(self) -> RealmInterfaceOutputSessionService:
        service = self._interface_outputs
        if service is None:
            raise RealmConflict(
                "Environment Preview output capture is not configured."
            )
        return service

    def _preview_output_lock(self, job_id: str) -> threading.RLock:
        with self._preview_output_state_lock:
            lock = self._preview_output_locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._preview_output_locks[job_id] = lock
            return lock

    def _ensure_preview_output_session(
        self, record: OperatorJobRecord
    ) -> InterfaceOutputSessionHandle:
        if not self._preview_context_for_record(record).preview_plan.outputs_enabled:
            raise RealmConflict(
                "This Environment Preview profile does not declare outputs."
            )
        service = self._preview_output_service()
        try:
            handle = service.recover_session(launch_id=record.job_id)
        except RealmNotFound:
            try:
                handle = service.create_session(
                    operation_id=_operation(
                        record.job_id, "preview-output-session/create"
                    ),
                    launch_id=record.job_id,
                    ttl_seconds=_resource_ttl_seconds(record),
                )
            except RealmConflict:
                handle = service.recover_session(launch_id=record.job_id)
        if (
            handle.session.launch_id != record.job_id
            or handle.lease.owner_id != handle.session.owner_id
        ):
            raise RealmIntegrityError(
                "Environment Preview output session differs from its job."
            )
        return handle

    def _recover_preview_output_session(
        self, record: OperatorJobRecord
    ) -> InterfaceOutputSessionHandle:
        handle = self._preview_output_service().recover_session(
            launch_id=record.job_id
        )
        if handle.session.launch_id != record.job_id:
            raise RealmIntegrityError(
                "Environment Preview output session differs from its job."
            )
        return handle

    def _register_active_preview_output(
        self,
        *,
        job_id: str,
        managed: ManagedEnvironmentPreviewBinding,
        handle: InterfaceOutputSessionHandle,
    ) -> None:
        with self._preview_output_state_lock:
            self._active_preview_bindings[job_id] = managed
            self._active_preview_output_handles[job_id] = handle

    def _unregister_active_preview_output(
        self,
        *,
        job_id: str,
        managed: ManagedEnvironmentPreviewBinding,
    ) -> None:
        with self._preview_output_state_lock:
            if self._active_preview_bindings.get(job_id) is managed:
                self._active_preview_bindings.pop(job_id, None)
                self._active_preview_output_handles.pop(job_id, None)

    @staticmethod
    def _preview_output_control_signature(
        control_file: Path,
    ) -> tuple[int, int, int, int]:
        metadata = control_file.stat(follow_symlinks=False)
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )

    def _capture_preview_outputs(
        self,
        *,
        record: OperatorJobRecord,
        descriptor: EnvironmentPreviewOutputCaptureDescriptor,
        handle: InterfaceOutputSessionHandle,
        rejected_records: list[InterfaceOutputRecordRejection] | None = None,
        require_terminal_states: bool = False,
    ) -> tuple[
        tuple[InterfaceOutputGenerationStatusRecord, ...],
        tuple[InterfaceOutputRecord, ...],
    ]:
        if not isinstance(
            descriptor, EnvironmentPreviewOutputCaptureDescriptor
        ):
            raise TypeError(
                "Environment Preview output capture requires a private descriptor."
            )
        if not isinstance(require_terminal_states, bool):
            raise TypeError("require_terminal_states must be a boolean.")
        lock = self._preview_output_lock(record.job_id)
        with lock:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                raise RealmConflict(
                    "Environment Preview output capture requires a live runtime."
                )
            service = self._preview_output_service()
            record_lines: dict[str, int] = {}
            control_records = service.read_control_file(
                descriptor.control_file,
                rejected_records=rejected_records,
                record_lines=record_lines,
            )
            capture_pass = service.capture_records(
                handle=handle,
                records=control_records,
                root_handles={"output": descriptor.source_root},
                retry_failed=False,
                rejected_records=rejected_records,
                record_lines=record_lines,
            )
            statuses = service.list_statuses(handle=handle)
            if not require_terminal_states:
                return statuses, capture_pass.accepted_records

            # A second supervisor may already be sealing the exact persisted
            # attempt.  Resume it idempotently against the now-quiescent
            # provider volume, then re-read the durable state.  Never let a
            # terminal job commit race a still-SEALING generation: cleanup
            # would otherwise destroy the only authorized source tree.
            roots = {"output": descriptor.source_root}
            for status in statuses:
                if status.state is not InterfaceOutputGenerationState.SEALING:
                    continue
                try:
                    service.resume_generation(
                        handle=handle,
                        output_id=status.output_id,
                        root_handles=roots,
                    )
                except (ContentRejected, RealmConflict, RealmExpired):
                    # The attempt may have just completed or failed under the
                    # other adopter.  The authoritative re-read below decides.
                    pass
            statuses = service.list_statuses(handle=handle)
            if any(
                status.state is InterfaceOutputGenerationState.SEALING
                for status in statuses
            ):
                raise EnvironmentPreviewFinalCapturePending(
                    "Environment Preview final output capture is still pending."
                )
            return statuses, capture_pass.accepted_records

    @staticmethod
    def _validate_preview_launch_request(
        record: OperatorJobRecord,
        managed: ManagedEnvironmentPreviewBinding,
    ) -> None:
        intent = record.launch_intent
        request = managed.request
        if (
            request.job_id != record.job_id
            or request.portable_plan_digest != record.plan_digest
            or managed.evidence.operator_plan_digest != record.plan_digest
            or managed.evidence.launch_request_digest != request.digest
            or (
                intent is not None
                and (
                    request.binding_id != intent.binding_id
                    or request.launch_token != intent.launch_token
                    or request.digest != intent.launch_request_digest
                    or intent.provider_kind != LOCAL_CONTAINER_WEB_BACKEND_KIND
                )
            )
        ):
            raise RealmIntegrityError(
                "Environment Preview provider request differs from its Operator Job."
            )

    def _realize_preview_binding(
        self,
        *,
        record: OperatorJobRecord,
        admission: LeaseRecord,
        recover_only: bool,
    ) -> ManagedEnvironmentPreviewBinding:
        binder, _provider, _authority = self._preview_execution_dependencies()
        context = self._preview_context_for_record(record)
        target = self._inspection.resolve_candidate(selection=context.selection)
        bind = binder.recover_existing if recover_only else binder.realize
        managed = bind(
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            owner_id=record.owner_id,
            admission_lease=admission,
            operator_plan_digest=record.plan_digest,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            target=target,
            preview_plan=context.preview_plan,
            ttl_seconds=_resource_ttl_seconds(record),
        )
        self._validate_preview_launch_request(record, managed)
        return managed

    def _recover_preview_stop_request(
        self, record: OperatorJobRecord
    ) -> ContainerWebLaunchRequest:
        """Rebuild only the exact provider stop coordinate from durable facts."""

        binder, _provider, _authority = self._preview_execution_dependencies()
        context = self._preview_context_for_record(record)
        target = self._inspection.resolve_candidate(selection=context.selection)
        request = binder.recover_stop_request(
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            owner_id=record.owner_id,
            operator_plan_digest=record.plan_digest,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            target=target,
            preview_plan=context.preview_plan,
        )
        intent = _require_launch_intent(record)
        if (
            request.job_id != record.job_id
            or request.binding_id != intent.binding_id
            or request.launch_token != intent.launch_token
            or request.portable_plan_digest != record.plan_digest
            or request.digest != intent.launch_request_digest
        ):
            raise RealmIntegrityError(
                "Recovered Environment Preview stop request differs from launch intent."
            )
        return request

    def _stop_preview_without_live_binding(
        self, record: OperatorJobRecord
    ) -> LocalContainerWebTerminal:
        _binder, provider, _authority = self._preview_execution_dependencies()
        terminal = provider.stop(self._recover_preview_stop_request(record))
        self._validate_preview_terminal(record, terminal)
        return terminal

    def _execute_preview_queued(
        self, record: OperatorJobRecord
    ) -> OperatorJobRecord:
        self._preview_context_for_record(record)
        capacity = self._ensure_capacity(record)
        admission: LeaseRecord | None = None
        managed: ManagedEnvironmentPreviewBinding | None = None
        try:
            latest = self.read(job_id=record.job_id)
            if latest.state is not OperatorJobState.QUEUED:
                return self.execute(job_id=record.job_id)
            capacity = self._validate_capacity(latest, capacity)
            admission = self._ensure_admission(latest)
            managed = self._realize_preview_binding(
                record=latest,
                admission=admission,
                recover_only=False,
            )
            managed.validate()
            context = self._preview_context_for_record(latest)
            if context.preview_plan.outputs_enabled:
                # Establish durable capture authority before the provider can
                # possibly start. The sibling output owner is launch-scoped and
                # is retired only after final capture and job adoption.
                self._ensure_preview_output_session(latest)
            try:
                self._ledger.begin_operator_job_start(
                    operation_id=_operation(record.job_id, "begin-start"),
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    expected_revision=latest.revision,
                    admission_lease_id=admission.lease_id,
                    admission_holder_id=admission.holder_id,
                    admission_fencing_token=admission.fencing_token,
                    binding_id=context.binding_id,
                    launch_token=context.launch_token,
                    provider_kind=LOCAL_CONTAINER_WEB_BACKEND_KIND,
                    evidence_fingerprint=context.evidence_fingerprint,
                    launch_request_digest=managed.request.digest,
                )
            except RealmConflict:
                current = self.read(job_id=record.job_id)
                if current.state is OperatorJobState.CANCELLED:
                    self._cleanup_unlaunched_preview_cancellation(
                        current, managed=managed
                    )
                    return self.read(job_id=record.job_id)
                if current.state is OperatorJobState.STOPPING:
                    return self._finish_preview_stopping(
                        current, managed=managed, admission=admission
                    )
                if current.state in {
                    OperatorJobState.STARTING,
                    OperatorJobState.RUNNING,
                }:
                    # A concurrent adopter committed the same deterministic
                    # launch intent.  Continue with the exact binding already
                    # open in this process so its attachments remain owned and
                    # are detached by the eventual terminal cleanup.
                    return self._drive_preview(
                        current,
                        managed=managed,
                        admission=admission,
                        capacity=capacity,
                    )
                raise
        except Exception:
            current = self.read(job_id=record.job_id)
            if current.state is OperatorJobState.QUEUED and current.launch_intent is None:
                self._release_capacity(current)
            raise
        if admission is None or managed is None:
            raise RealmIntegrityError(
                "Environment Preview launch preparation was incomplete."
            )
        current = self.read(job_id=record.job_id)
        if current.state is OperatorJobState.STOPPING:
            return self._finish_preview_stopping(
                current, managed=managed, admission=admission
            )
        return self._drive_preview(
            current,
            managed=managed,
            admission=admission,
            capacity=capacity,
        )

    def _resume_preview(self, record: OperatorJobRecord) -> OperatorJobRecord:
        self._preview_context_for_record(record)
        managed: ManagedEnvironmentPreviewBinding | None = None
        try:
            capacity = self._validate_capacity(record)
        except RealmError as error:
            return self._reconcile_preview_capacity_loss(record, failure=error)
        try:
            admission = self._validate_launch_admission(record)
        except RealmError as error:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_preview_stopping(current)
            terminal = self._stop_preview_without_live_binding(current)
            return self._finish_preview_failure(
                self.read(job_id=record.job_id),
                terminal=terminal,
                code="operator_job_admission_lost",
                stage="admission",
                failure_type=type(error).__name__,
                managed=None,
                admission=None,
            )
        try:
            managed = self._realize_preview_binding(
                record=record,
                admission=admission,
                recover_only=True,
            )
            managed.validate()
        except RealmError as error:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                # The recovered binding failed validation, so do not hand its
                # request to the provider.  Stop by immutable launch identity,
                # then carry the local handle only into independently proven
                # terminal cleanup so its attachments can be closed.
                terminal = self._stop_preview_without_live_binding(current)
                return self._finish_preview_cancelled(
                    current,
                    terminal=terminal,
                    managed=managed,
                    admission=admission,
                )
            terminal = self._stop_preview_without_live_binding(current)
            return self._finish_preview_failure(
                self.read(job_id=record.job_id),
                terminal=terminal,
                code="environment_preview_binding_lost",
                stage="binding",
                failure_type=type(error).__name__,
                managed=managed,
                admission=admission,
            )
        return self._drive_preview(
            self.read(job_id=record.job_id),
            managed=managed,
            admission=admission,
            capacity=capacity,
        )

    def _drive_preview(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedEnvironmentPreviewBinding,
        admission: LeaseRecord,
        capacity: OperatorCapacityReservationRecord,
    ) -> OperatorJobRecord:
        context = self._preview_context_for_record(record)
        if not context.preview_plan.outputs_enabled:
            return self._drive_preview_loop(
                record,
                managed=managed,
                admission=admission,
                capacity=capacity,
                output_handle=None,
                output_control_file=None,
            )
        handle = self._ensure_preview_output_session(record)
        descriptor = managed.output_capture_descriptor
        self._register_active_preview_output(
            job_id=record.job_id,
            managed=managed,
            handle=handle,
        )
        try:
            return self._drive_preview_loop(
                record,
                managed=managed,
                admission=admission,
                capacity=capacity,
                output_handle=handle,
                output_control_file=descriptor.control_file,
            )
        except EnvironmentPreviewFinalCapturePending:
            # This supervisor no longer owns useful live work while another
            # adopter completes a persisted seal.  Preserve durable provider
            # resources but close this process's namespace descriptors before
            # the scheduler retries with a freshly reconstructed binding.
            managed.detach_for_recovery()
            raise
        finally:
            self._unregister_active_preview_output(
                job_id=record.job_id,
                managed=managed,
            )

    def _drive_preview_loop(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedEnvironmentPreviewBinding,
        admission: LeaseRecord,
        capacity: OperatorCapacityReservationRecord,
        output_handle: InterfaceOutputSessionHandle | None,
        output_control_file: Path | None,
    ) -> OperatorJobRecord:
        _binder, provider, _authority = self._preview_execution_dependencies()
        intent = _require_launch_intent(record)
        self._validate_preview_launch_request(record, managed)
        deadline = intent.created_at + record.plan.timeout_seconds
        interval = min(
            _CAPACITY_HEARTBEAT_MAX_INTERVAL_SECONDS,
            max(1.0, _capacity_ttl_seconds(record) / 3.0),
        )
        next_capacity_renewal = time.monotonic() + interval
        last_output_signature: tuple[int, int, int, int] | None = None
        while True:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_preview_stopping(
                    current, managed=managed, admission=admission
                )
            if time.time() >= deadline:
                terminal = provider.stop(managed.request)
                return self._finish_preview_failure(
                    current,
                    terminal=terminal,
                    code="environment_preview_timeout",
                    stage="timeout",
                    failure_type="TimeoutError",
                    managed=managed,
                    admission=admission,
                )
            if time.monotonic() >= next_capacity_renewal:
                try:
                    capacity = self._renew_capacity(current, capacity)
                except RealmError as error:
                    terminal = provider.stop(managed.request)
                    return self._finish_preview_failure(
                        self.read(job_id=record.job_id),
                        terminal=terminal,
                        code="operator_job_capacity_lost",
                        stage="capacity",
                        failure_type=type(error).__name__,
                        managed=managed,
                        admission=admission,
                )
                next_capacity_renewal = time.monotonic() + interval
            if output_handle is not None and output_control_file is not None:
                try:
                    signature = self._preview_output_control_signature(
                        output_control_file
                    )
                    if signature != last_output_signature:
                        try:
                            self._capture_preview_outputs(
                                record=current,
                                descriptor=managed.output_capture_descriptor,
                                handle=output_handle,
                            )
                        except ContentRejected:
                            # The final result records a bounded control-file
                            # diagnostic; unchanged invalid bytes are not retried
                            # in a tight supervision loop.
                            pass
                        last_output_signature = signature
                except (OSError, RealmError) as error:
                    terminal = provider.stop(managed.request)
                    return self._finish_preview_failure(
                        self.read(job_id=record.job_id),
                        terminal=terminal,
                        code="environment_preview_output_binding_lost",
                        stage="output-capture",
                        failure_type=type(error).__name__,
                        managed=managed,
                        admission=admission,
                    )
            try:
                observed = provider.start_or_adopt(managed.request)
            except LocalContainerWebProviderError as error:
                terminal = provider.stop(managed.request)
                return self._finish_preview_failure(
                    self.read(job_id=record.job_id),
                    terminal=terminal,
                    code=error.code,
                    stage="provider",
                    failure_type=type(error).__name__,
                    managed=managed,
                    admission=admission,
                )
            except Exception as error:
                terminal = provider.stop(managed.request)
                return self._finish_preview_failure(
                    self.read(job_id=record.job_id),
                    terminal=terminal,
                    code="environment_preview_execution_failed",
                    stage="orchestration",
                    failure_type=type(error).__name__,
                    managed=managed,
                    admission=admission,
                )
            if isinstance(observed, LocalContainerWebTerminal):
                return self._finish_preview_observation(
                    self.read(job_id=record.job_id),
                    terminal=observed,
                    managed=managed,
                    admission=admission,
                )
            current = self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STARTING:
                current = self._ledger.mark_operator_job_running(
                    operation_id=_operation(record.job_id, "mark-running"),
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    expected_revision=current.revision,
                    launch_token=intent.launch_token,
                    admission_lease_id=intent.admission_lease_id,
                    admission_fencing_token=intent.admission_fencing_token,
                )
            if current.state is OperatorJobState.STOPPING:
                return self._finish_preview_stopping(
                    current, managed=managed, admission=admission
                )
            time.sleep(0.25)

    def _renew_capacity(
        self,
        record: OperatorJobRecord,
        reservation: OperatorCapacityReservationRecord,
    ) -> OperatorCapacityReservationRecord:
        self._validate_capacity_record(record, reservation)
        renewed = self._ledger.renew_operator_capacity_reservation(
            operation_id=_operation(
                record.job_id,
                "renew-capacity/"
                f"generation-{reservation.generation}/"
                f"heartbeat-{reservation.heartbeat_revision + 1}",
            ),
            actor_principal_id=self.principal_id,
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
            ttl_seconds=_capacity_ttl_seconds(record),
        )
        return self._validate_capacity(record, renewed)

    def _reconcile_preview_capacity_loss(
        self, record: OperatorJobRecord, *, failure: BaseException
    ) -> OperatorJobRecord:
        admission: LeaseRecord | None = None
        managed: ManagedEnvironmentPreviewBinding | None = None
        try:
            admission = self._validate_launch_admission(record)
            managed = self._realize_preview_binding(
                record=record,
                admission=admission,
                recover_only=True,
            )
        except RealmError:
            managed = None
        _binder, provider, _authority = self._preview_execution_dependencies()
        terminal = (
            provider.stop(managed.request)
            if managed is not None
            else self._stop_preview_without_live_binding(record)
        )
        current = self.read(job_id=record.job_id)
        if current.state is OperatorJobState.STOPPING:
            return self._finish_preview_cancelled(
                current,
                terminal=terminal,
                managed=managed,
                admission=admission,
            )
        return self._finish_preview_failure(
            current,
            terminal=terminal,
            code="operator_job_capacity_lost",
            stage="capacity",
            failure_type=type(failure).__name__,
            managed=managed,
            admission=admission,
        )

    def _execute_queued(self, record: OperatorJobRecord) -> OperatorJobRecord:
        context = self._context_for_record(record)
        capacity = self._ensure_capacity(record)
        claim = None
        managed: ManagedOperatorAttemptBinding | None = None
        reservation: ProcessLaunchReservation | None = None
        launch_digest: str | None = None
        admission: LeaseRecord | None = None
        try:
            claim = self._launcher.claim_noncanonical_realization(
                launch_token=context.launch_token,
                binding_id=context.binding_id,
            )
            latest = self.read(job_id=record.job_id)
            if latest.state is not OperatorJobState.QUEUED:
                return self.execute(job_id=record.job_id)
            capacity = self._validate_capacity(latest, capacity)
            admission = self._ensure_admission(latest)
            managed = self._attempt_binder.realize(
                actor_principal_id=self.principal_id,
                job_id=record.job_id,
                owner_id=record.owner_id,
                admission_lease=admission,
                attempt_id=context.attempt_id,
                binding_id=context.binding_id,
                launch_token=context.launch_token,
                evidence_fingerprint=context.evidence_fingerprint,
                evaluation_spec=context.evaluation_spec,
                portable_spec=context.portable_spec,
                ttl_seconds=_resource_ttl_seconds(latest),
            )
            reservation = self._launcher.reserve_noncanonical(
                managed.local_binding,
                realization_claim=claim,
            )
            launch_digest = self._launcher.expected_launch_request_digest(
                managed.local_binding
            )
            try:
                self._ledger.begin_operator_job_start(
                    operation_id=_operation(record.job_id, "begin-start"),
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    expected_revision=latest.revision,
                    admission_lease_id=admission.lease_id,
                    admission_holder_id=admission.holder_id,
                    admission_fencing_token=admission.fencing_token,
                    binding_id=context.binding_id,
                    launch_token=context.launch_token,
                    provider_kind=LOCAL_PROCESS_BACKEND_KIND,
                    evidence_fingerprint=context.evidence_fingerprint,
                    launch_request_digest=launch_digest,
                )
            except RealmConflict:
                current = self.read(job_id=record.job_id)
                if current.state not in {
                    OperatorJobState.CANCELLED,
                    OperatorJobState.STOPPING,
                }:
                    raise
                # Terminal reconciliation seals or retires the same provider
                # coordinate.  Drop the realization gate first; retaining it
                # here would make the cleanup path wait on our own claim.
                self._launcher.release_noncanonical_realization(claim)
                claim = None
                if current.state is OperatorJobState.STOPPING:
                    reconciliation = self._launcher.reconcile_noncanonical_terminal(
                        launch_token=context.launch_token,
                        binding_id=context.binding_id,
                        evidence_fingerprint=context.evidence_fingerprint,
                        launch_request_digest=launch_digest,
                    )
                    return self._finish_cancelled(
                        current,
                        proof=reconciliation.proof,
                        managed=managed,
                        admission=admission,
                    )
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
        except Exception:
            current = self.read(job_id=record.job_id)
            if (
                current.state is OperatorJobState.QUEUED
                and current.launch_intent is None
            ):
                self._release_capacity(current)
            raise
        finally:
            if claim is not None:
                self._launcher.release_noncanonical_realization(claim)

        if (
            managed is None
            or reservation is None
            or launch_digest is None
            or admission is None
        ):
            raise RealmIntegrityError("Operator Job launch preparation was incomplete.")
        current = self.read(job_id=record.job_id)
        if current.state is OperatorJobState.STOPPING:
            return self._finish_stopping(
                current, managed=managed, admission=admission
            )
        try:
            handle = self._launcher.start_noncanonical(
                binding=managed.local_binding,
                reservation=reservation,
                launch_request_digest=launch_digest,
            )
        except Exception:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_stopping(
                    current, managed=managed, admission=admission
                )
            raise
        try:
            capacity = self._validate_capacity(current, capacity)
        except RealmError as error:
            proof = handle.stop(grace_period=0.1, timeout=10.0)
            return self._finish_capacity_lost(
                self.read(job_id=record.job_id),
                proof=proof,
                managed=managed,
                admission=admission,
                failure=error,
            )
        return self._drive_handle(
            current,
            managed=managed,
            admission=admission,
            capacity=capacity,
            handle=handle,
        )

    def _resume_launched(self, record: OperatorJobRecord) -> OperatorJobRecord:
        context = self._context_for_record(record)
        intent = _require_launch_intent(record)
        try:
            capacity = self._validate_capacity(record)
        except RealmError as error:
            return self._reconcile_capacity_loss(
                record,
                context=context,
                failure=error,
            )
        try:
            admission = self._validate_launch_admission(record)
        except RealmError as error:
            reconciliation = self._launcher.reconcile_noncanonical_terminal(
                launch_token=intent.launch_token,
                binding_id=intent.binding_id,
                evidence_fingerprint=intent.evidence_fingerprint,
                launch_request_digest=intent.launch_request_digest,
            )
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_cancelled(
                    current,
                    proof=reconciliation.proof,
                    managed=None,
                    admission=None,
                )
            return self._finish_with_empty_capture(
                current,
                proof=reconciliation.proof,
                result=_failure_result(
                    code="operator_job_admission_lost",
                    stage="admission",
                    failure_type=type(error).__name__,
                ),
                status=OperatorJobTerminalStatus.FAILED,
                code="operator_job_admission_lost",
                managed=None,
                admission=None,
            )
        try:
            managed = self._attempt_binder.recover_existing(
                actor_principal_id=self.principal_id,
                job_id=record.job_id,
                owner_id=record.owner_id,
                admission_lease=admission,
                attempt_id=context.attempt_id,
                binding_id=context.binding_id,
                launch_token=context.launch_token,
                evidence_fingerprint=context.evidence_fingerprint,
                evaluation_spec=context.evaluation_spec,
                portable_spec=context.portable_spec,
                ttl_seconds=_resource_ttl_seconds(record),
            )
        except (RealmError, OSError) as error:
            reconciliation = self._launcher.reconcile_noncanonical_terminal(
                launch_token=intent.launch_token,
                binding_id=intent.binding_id,
                evidence_fingerprint=intent.evidence_fingerprint,
                launch_request_digest=intent.launch_request_digest,
            )
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_cancelled(
                    current,
                    proof=reconciliation.proof,
                    managed=None,
                    admission=admission,
                )
            return self._finish_with_empty_capture(
                current,
                proof=reconciliation.proof,
                result=_failure_result(
                    code="operator_attempt_binding_lost",
                    stage="binding",
                    failure_type=type(error).__name__,
                ),
                status=OperatorJobTerminalStatus.FAILED,
                code="operator_attempt_binding_lost",
                managed=None,
                admission=admission,
            )
        try:
            _reservation, handle = self._launcher.recover_noncanonical(
                binding=managed.local_binding,
                launch_request_digest=intent.launch_request_digest,
            )
        except Exception:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_stopping(
                    current, managed=managed, admission=admission
                )
            raise
        return self._drive_handle(
            self.read(job_id=record.job_id),
            managed=managed,
            admission=admission,
            capacity=capacity,
            handle=handle,
        )

    def _reconcile_capacity_loss(
        self,
        record: OperatorJobRecord,
        *,
        context: _DebugExecutionContext,
        failure: BaseException,
    ) -> OperatorJobRecord:
        """Stop/adopt a launched worker without authorizing further execution."""

        intent = _require_launch_intent(record)
        try:
            admission = self._validate_launch_admission(record)
        except RealmError:
            admission = None
        managed: ManagedOperatorAttemptBinding | None = None
        if admission is not None:
            try:
                managed = self._attempt_binder.recover_existing(
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    owner_id=record.owner_id,
                    admission_lease=admission,
                    attempt_id=context.attempt_id,
                    binding_id=context.binding_id,
                    launch_token=context.launch_token,
                    evidence_fingerprint=context.evidence_fingerprint,
                    evaluation_spec=context.evaluation_spec,
                    portable_spec=context.portable_spec,
                    ttl_seconds=_resource_ttl_seconds(record),
                )
            except (RealmError, OSError):
                managed = None
        reconciliation = self._launcher.reconcile_noncanonical_terminal(
            launch_token=intent.launch_token,
            binding_id=intent.binding_id,
            evidence_fingerprint=intent.evidence_fingerprint,
            launch_request_digest=intent.launch_request_digest,
        )
        current = self.read(job_id=record.job_id)
        if current.state.terminal:
            self._reconcile_terminal_cleanup(current, managed=managed)
            return self.read(job_id=record.job_id)
        return self._finish_capacity_lost(
            current,
            proof=reconciliation.proof,
            managed=managed,
            admission=admission,
            failure=failure,
        )

    def _finish_capacity_lost(
        self,
        record: OperatorJobRecord,
        *,
        proof: WorkerTerminalProof,
        managed: ManagedOperatorAttemptBinding | None,
        admission: LeaseRecord | None,
        failure: BaseException,
    ) -> OperatorJobRecord:
        if record.state is OperatorJobState.STOPPING:
            return self._finish_cancelled(
                record,
                proof=proof,
                managed=managed,
                admission=admission,
            )
        return self._finish_with_empty_capture(
            record,
            proof=proof,
            result=_failure_result(
                code="operator_job_capacity_lost",
                stage="capacity",
                failure_type=type(failure).__name__,
            ),
            status=OperatorJobTerminalStatus.FAILED,
            code="operator_job_capacity_lost",
            managed=managed,
            admission=admission,
        )

    def _drive_handle(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedOperatorAttemptBinding,
        admission: LeaseRecord,
        capacity: OperatorCapacityReservationRecord,
        handle: ManagedLocalAttempt,
    ) -> OperatorJobRecord:
        intent = _require_launch_intent(record)
        if record.state is OperatorJobState.STOPPING:
            return self._finish_stopping(
                record, managed=managed, admission=admission
            )
        heartbeat = _OperatorCapacityHeartbeat(
            service=self,
            record=record,
            reservation=capacity,
            handle=handle,
        )
        capacity_failure: BaseException | None = None
        heartbeat.start()
        try:
            observation = handle.wait_started(
                timeout=min(10.0, record.plan.timeout_seconds)
            )
            current = self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                heartbeat.stop()
                return self._finish_stopping(
                    current, managed=managed, admission=admission
                )
            crossed_start = isinstance(observation, WorkerStarted) or (
                isinstance(observation, WorkerTerminalProof) and observation.started
            )
            if current.state is OperatorJobState.STARTING and crossed_start:
                current = self._ledger.mark_operator_job_running(
                    operation_id=_operation(record.job_id, "mark-running"),
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    expected_revision=current.revision,
                    launch_token=intent.launch_token,
                    admission_lease_id=intent.admission_lease_id,
                    admission_fencing_token=intent.admission_fencing_token,
                )
            envelope = handle.collect(timeout=record.plan.timeout_seconds + 10.0)
            heartbeat.stop()
            capacity_failure = heartbeat.failure
            heartbeat.raise_if_failed()
            try:
                capacity = self._validate_capacity(
                    self.read(job_id=record.job_id),
                    heartbeat.reservation,
                )
            except RealmError as error:
                capacity_failure = error
                raise
            proof = handle.terminal_proof
            if proof is None:
                raise RealmIntegrityError(
                    "Collected Operator Job is missing terminal provider proof."
                )
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_stopping(
                    current, managed=managed, admission=admission
                )
            try:
                self._validate_launch_admission(current)
            except RealmError as admission_error:
                return self._finish_with_empty_capture(
                    current,
                    proof=proof,
                    result=_failure_result(
                        code="operator_job_admission_lost",
                        stage="admission",
                        failure_type=type(admission_error).__name__,
                    ),
                    status=OperatorJobTerminalStatus.FAILED,
                    code="operator_job_admission_lost",
                    managed=managed,
                    admission=None,
                )
            return self._finish_envelope(
                current,
                envelope=envelope,
                logs=handle.logs,
                proof=proof,
                managed=managed,
                admission=admission,
            )
        except LocalAttemptPlatformError as error:
            heartbeat.stop()
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            if current.state is OperatorJobState.STOPPING:
                return self._finish_cancelled(
                    current,
                    proof=error.terminal_proof,
                    managed=managed,
                    admission=admission,
                )
            try:
                self._validate_launch_admission(current)
            except RealmError as admission_error:
                return self._finish_with_empty_capture(
                    current,
                    proof=error.terminal_proof,
                    result=_failure_result(
                        code="operator_job_admission_lost",
                        stage="admission",
                        failure_type=type(admission_error).__name__,
                    ),
                    status=OperatorJobTerminalStatus.FAILED,
                    code="operator_job_admission_lost",
                    managed=managed,
                    admission=None,
                )
            capacity_failure = capacity_failure or heartbeat.failure
            return self._finish_with_empty_capture(
                current,
                proof=error.terminal_proof,
                result=_failure_result(
                    code=(
                        "operator_job_capacity_lost"
                        if capacity_failure is not None
                        else error.code
                    ),
                    stage=(
                        "capacity"
                        if capacity_failure is not None
                        else "worker"
                    ),
                    failure_type=type(
                        capacity_failure
                        if capacity_failure is not None
                        else error
                    ).__name__,
                ),
                status=OperatorJobTerminalStatus.FAILED,
                code=(
                    "operator_job_capacity_lost"
                    if capacity_failure is not None
                    else error.code
                ),
                managed=managed,
                admission=admission,
            )
        except Exception as error:
            heartbeat.stop()
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            intent = _require_launch_intent(current)
            reconciliation = self._launcher.reconcile_noncanonical_terminal(
                launch_token=intent.launch_token,
                binding_id=intent.binding_id,
                evidence_fingerprint=intent.evidence_fingerprint,
                launch_request_digest=intent.launch_request_digest,
            )
            if current.state is OperatorJobState.STOPPING:
                return self._finish_cancelled(
                    current,
                    proof=reconciliation.proof,
                    managed=managed,
                    admission=admission,
                )
            try:
                self._validate_launch_admission(current)
            except RealmError as admission_error:
                return self._finish_with_empty_capture(
                    current,
                    proof=reconciliation.proof,
                    result=_failure_result(
                        code="operator_job_admission_lost",
                        stage="admission",
                        failure_type=type(admission_error).__name__,
                    ),
                    status=OperatorJobTerminalStatus.FAILED,
                    code="operator_job_admission_lost",
                    managed=managed,
                    admission=None,
                )
            capacity_failure = (
                capacity_failure
                or heartbeat.failure
                or (
                    error
                    if isinstance(error, RealmCapacityUnavailable)
                    else None
                )
            )
            failure_code = (
                "operator_job_capacity_lost"
                if capacity_failure is not None
                else "operator_job_execution_failed"
            )
            return self._finish_with_empty_capture(
                current,
                proof=reconciliation.proof,
                result=_failure_result(
                    code=failure_code,
                    stage=(
                        "capacity"
                        if capacity_failure is not None
                        else "orchestration"
                    ),
                    failure_type=type(
                        capacity_failure
                        if capacity_failure is not None
                        else error
                    ).__name__,
                ),
                status=OperatorJobTerminalStatus.FAILED,
                code=failure_code,
                managed=managed,
                admission=admission,
            )
        finally:
            heartbeat.stop()

    # -- terminal adoption ------------------------------------------------

    def _finish_envelope(
        self,
        record: OperatorJobRecord,
        *,
        envelope: AttemptEnvelope,
        logs: Sequence[LocalAttemptWorkerLog],
        proof: WorkerTerminalProof,
        managed: ManagedOperatorAttemptBinding,
        admission: LeaseRecord,
    ) -> OperatorJobRecord:
        change = self._begin_terminal_owner_change(
            record,
            operation_phase="output-change",
            base_change_id=_output_change_id(record.job_id),
        )
        try:
            artifacts = self._finalizer.capture_declared_outputs(
                envelope=envelope,
                binding=managed,
                change_id=change.change_id,
                membership_role=OPERATOR_JOB_OUTPUT_ROLE,
            )
        except Exception as error:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-output-change",
            )
            return self._finish_with_empty_capture(
                self.read(job_id=record.job_id),
                proof=proof,
                result=_failure_result(
                    code="output_capture_failed",
                    stage="output-capture",
                    failure_type=type(error).__name__,
                ),
                status=OperatorJobTerminalStatus.FAILED,
                code="output_capture_failed",
                managed=managed,
                admission=admission,
            )
        except BaseException:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-output-change",
            )
            raise

        try:
            outputs = tuple(_declared_output(item) for item in artifacts)
            additions = _output_memberships(artifacts)
            result = OperatorJobResult(
                result_kind=CANDIDATE_DEBUG_JOB_KIND,
                status=envelope.outcome,
                metrics=_numeric_metrics(envelope.metric_values),
                constraint_results=dict(envelope.constraint_results),
                event_summary=_normalize_result_tracebacks(
                    dict(envelope.event_summary)
                ),
                declared_outputs=outputs,
                logs=tuple(_job_log_metadata(item) for item in logs),
                details=_normalize_result_tracebacks(
                    {
                        "error": dict(envelope.error),
                        "materialization": dict(envelope.materialization),
                        "phase": envelope.phase,
                        "validation": dict(envelope.validation),
                        "wall_clock_seconds": envelope.wall_clock_seconds,
                    }
                ),
            )
            if envelope.outcome == "success":
                status = OperatorJobTerminalStatus.SUCCEEDED
                code = "completed"
            elif envelope.outcome == "cancelled":
                current = self.read(job_id=record.job_id)
                if current.state is not OperatorJobState.STOPPING:
                    current = self._ledger.request_operator_job_stop(
                        operation_id=_operation(record.job_id, "worker-cancelled"),
                        actor_principal_id=self.principal_id,
                        job_id=record.job_id,
                        expected_revision=current.revision,
                        reason_code="worker_cancelled",
                    )
                record = current
                status = OperatorJobTerminalStatus.CANCELLED
                code = "worker_cancelled"
            else:
                status = OperatorJobTerminalStatus.FAILED
                code = _envelope_code(envelope)
            return self._finish(
                record,
                proof=proof,
                result=result,
                status=status,
                code=code,
                change_id=change.change_id,
                additions=additions,
                managed=managed,
                admission=admission,
            )
        except RealmCapacityUnavailable as error:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-output-change-after-capacity-loss",
            )
            return self._finish_capacity_lost(
                self.read(job_id=record.job_id),
                proof=proof,
                managed=managed,
                admission=admission,
                failure=error,
            )
        except RealmConflict:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-output-change-after-conflict",
            )
            current = self.read(job_id=record.job_id)
            if current.state is not OperatorJobState.STOPPING:
                raise
            return self._finish_cancelled(
                current,
                proof=proof,
                managed=managed,
                admission=admission,
            )
        except BaseException:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-output-change-after-error",
            )
            raise

    def _abort_output_change(
        self,
        *,
        record: OperatorJobRecord,
        change_id: str,
        operation_suffix: str,
    ) -> None:
        self._ledger.abort_owner_change(
            operation_id=_operation(
                record.job_id,
                f"{operation_suffix}/"
                f"{request_digest({'change_id': change_id})[:16]}",
            ),
            actor_principal_id=self.principal_id,
            change_id=change_id,
        )

    def _begin_terminal_owner_change(
        self,
        record: OperatorJobRecord,
        *,
        operation_phase: str,
        base_change_id: str,
    ) -> OwnerChange:
        """Recover a current terminal change or advance past stale attempts.

        Owner-change operation receipts are intentionally immutable.  Reusing
        one deterministic receipt after its retention expired would therefore
        return an unusable change forever.  Terminal adoption keeps the legacy
        coordinate as generation zero, then advances through deterministic
        generation identities whenever the current row is aborted or expired.
        The eventual Operator Job finish still commits its owner change and
        terminal job head atomically in one ledger transaction.
        """

        for generation in range(_TERMINAL_OWNER_CHANGE_MAX_GENERATIONS):
            if generation == 0:
                operation_id = _operation(record.job_id, operation_phase)
                change_id = base_change_id
            else:
                operation_id = _operation(
                    record.job_id,
                    f"{operation_phase}/generation-{generation}",
                )
                change_id = _logical_id(
                    "operator-job-terminal-owner-change-generation",
                    {
                        "base_change_id": base_change_id,
                        "generation": generation,
                        "job_id": record.job_id,
                    },
                )
            issued = self._ledger.begin_owner_change(
                operation_id=operation_id,
                actor_principal_id=self.principal_id,
                owner_id=record.owner_id,
                expected_owner_revision=0,
                ttl_seconds=_CAPTURE_TTL_SECONDS,
                change_id=change_id,
            )
            current = self._ledger.read_owner_change(
                actor_principal_id=self.principal_id,
                change_id=change_id,
                permission=OwnerPermission.DERIVE,
            )
            if (
                current.change_id != issued.change_id
                or current.owner_id != record.owner_id
                or current.base_owner_revision != 0
                or current.retention_lease_id != issued.retention_lease_id
                or current.expires_at != issued.expires_at
            ):
                raise RealmIntegrityError(
                    "Operator Job terminal owner change identity changed."
                )
            if current.state is OwnerChangeState.COMMITTED:
                return current
            if (
                current.state is OwnerChangeState.ACTIVE
                and current.expires_at > time.time()
            ):
                return current
            if current.state is OwnerChangeState.ACTIVE:
                current = self._ledger.abort_owner_change(
                    operation_id=_operation(
                        record.job_id,
                        f"{operation_phase}/expire-generation-{generation}",
                    ),
                    actor_principal_id=self.principal_id,
                    change_id=current.change_id,
                )
                if current.state is OwnerChangeState.COMMITTED:
                    return current
            if current.state not in {
                OwnerChangeState.ABORTED,
                OwnerChangeState.EXPIRED,
            }:
                raise RealmIntegrityError(
                    "Operator Job terminal owner change has an unsupported state."
                )
        raise RealmConflict(
            "Operator Job terminal owner change recovery limit was reached."
        )

    @staticmethod
    def _validate_preview_terminal(
        record: OperatorJobRecord, terminal: LocalContainerWebTerminal
    ) -> None:
        intent = _require_launch_intent(record)
        if (
            terminal.job_id != record.job_id
            or terminal.binding_id != intent.binding_id
            or terminal.launch_token != intent.launch_token
            or terminal.launch_request_digest != intent.launch_request_digest
        ):
            raise RealmIntegrityError(
                "Environment Preview terminal proof differs from its launch intent."
            )

    def _final_preview_output_state(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
    ) -> tuple[
        InterfaceOutputSessionHandle,
        tuple[InterfaceOutputGenerationStatusRecord, ...],
        tuple[Mapping[str, Any], ...],
        bool,
    ]:
        """Capture once after terminal proof while the output volume still exists."""

        handle = self._recover_preview_output_session(record)
        service = self._preview_output_service()
        if handle.lease.state is not LeaseState.ACTIVE:
            # A previous terminal attempt already crossed the durable writer
            # fence.  The status set is immutable now: never reconstruct
            # filesystem authority or replay the deterministic close operation
            # with a newly observed final-record payload after a crash.
            statuses = service.list_statuses(handle=handle)
            if any(
                status.state is InterfaceOutputGenerationState.SEALING
                for status in statuses
            ):
                raise RealmIntegrityError(
                    "Fenced Environment Preview output session still has a "
                    "sealing generation."
                )
            return handle, statuses, (), False
        rejected: list[InterfaceOutputRecordRejection] = []
        capture_error: BaseException | None = None
        final_records: tuple[InterfaceOutputRecord, ...] | None = None
        try:
            binder, _provider, _authority = self._preview_execution_dependencies()
            context = self._preview_context_for_record(record)
            target = self._inspection.resolve_candidate(selection=context.selection)
            descriptor = binder.recover_terminal_output_capture(
                actor_principal_id=self.principal_id,
                job_id=record.job_id,
                owner_id=record.owner_id,
                operator_plan_digest=record.plan_digest,
                binding_id=context.binding_id,
                launch_token=context.launch_token,
                target=target,
                preview_plan=context.preview_plan,
                terminal=terminal,
            )
            statuses, final_records = self._capture_preview_outputs(
                record=record,
                descriptor=descriptor,
                handle=handle,
                rejected_records=rejected,
                require_terminal_states=True,
            )
        except EnvironmentPreviewFinalCapturePending:
            raise
        except (ContentRejected, OSError, RealmError) as error:
            # The provider is already terminal, so the safe fallback is to
            # adopt only generations that were durably captured earlier.  The
            # binder issues no guessed host path if exact terminal-only output
            # authority cannot be reconstructed.
            capture_error = error
            statuses = service.list_statuses(handle=handle)

        sealing = any(
            status.state is InterfaceOutputGenerationState.SEALING
            for status in statuses
        )
        if sealing:
            raise EnvironmentPreviewFinalCapturePending(
                "Environment Preview final output capture is still pending."
            )

        # This is the cross-supervisor linearization point.  A retry that
        # commits before the writer lease closes appears READY in the re-read
        # and is adopted below.  A retry still in flight is durably failed by
        # lease release.  No capture can start after this point, so the exact
        # status set cannot grow between adoption and terminal job commit.
        try:
            handle = service.close_capture(
                operation_id=_operation(
                    record.job_id, "preview-output-session/close-capture"
                ),
                handle=handle,
                require_drained=True,
                final_records=final_records,
            )
        except InterfaceOutputDrainPending as error:
            raise EnvironmentPreviewFinalCapturePending(
                "Environment Preview final output coverage is still pending."
            ) from error
        statuses = service.list_statuses(handle=handle)
        if any(
            status.state is InterfaceOutputGenerationState.SEALING
            for status in statuses
        ):
            raise RealmIntegrityError(
                "Closed Environment Preview output session still has a sealing generation."
            )

        diagnostics: list[Mapping[str, Any]] = []
        for status in statuses:
            if status.state is InterfaceOutputGenerationState.FAILED:
                diagnostics.append(
                    {
                        "attempt": status.attempt_number,
                        "code": status.error_code or "capture_failed",
                        "id": status.output_id,
                        "kind": status.kind.value,
                        "label": status.label,
                        "source": "generation",
                    }
                )
        diagnostics.extend(
            {
                "code": item.code,
                "line": item.line_number,
                "source": "control-file",
            }
            for item in rejected
        )
        if capture_error is not None:
            diagnostics.append(
                {
                    "code": "final_capture_unavailable",
                    "failure_type": type(capture_error).__name__,
                    "source": "supervisor",
                }
            )
        truncated = len(diagnostics) > 256
        return handle, statuses, tuple(diagnostics[:256]), truncated

    def _finish_preview_with_outputs(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
        managed: ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
        result_status: str,
        terminal_status: OperatorJobTerminalStatus,
        code: str,
        details: Mapping[str, Any],
    ) -> OperatorJobRecord:
        # Serialize final capture, no-copy adoption, terminal commit, and
        # output-session cleanup against explicit live retries.  Releasing the
        # lock after only the filesystem capture would allow a late retry to
        # create a ready generation that the terminal result did not adopt.
        with self._preview_output_lock(record.job_id):
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            return self._finish_preview_with_outputs_locked(
                current,
                terminal=terminal,
                managed=managed,
                admission=admission,
                result_status=result_status,
                terminal_status=terminal_status,
                code=code,
                details=details,
            )

    def _finish_preview_with_outputs_locked(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
        managed: ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
        result_status: str,
        terminal_status: OperatorJobTerminalStatus,
        code: str,
        details: Mapping[str, Any],
    ) -> OperatorJobRecord:
        outputs_enabled = self._preview_context_for_record(
            record
        ).preview_plan.outputs_enabled
        handle: InterfaceOutputSessionHandle | None = None
        diagnostics: tuple[Mapping[str, Any], ...] = ()
        diagnostics_truncated = False
        ready: tuple[InterfaceOutputGenerationRecord, ...] = ()
        if outputs_enabled:
            handle, statuses, diagnostics, diagnostics_truncated = (
                self._final_preview_output_state(record, terminal=terminal)
            )
            generations = tuple(
                status.ready_generation
                for status in statuses
                if status.state is InterfaceOutputGenerationState.READY
            )
            if any(
                item is None for item in generations
            ):  # pragma: no cover - invariant
                raise RealmIntegrityError(
                    "Ready Environment Preview output lacks its generation."
                )
            ready = tuple(item for item in generations if item is not None)
        outputs = tuple(_preview_declared_output(item) for item in ready)
        additions = _preview_output_memberships(ready)
        status_key = terminal_status.value
        change = self._begin_terminal_owner_change(
            record,
            operation_phase=f"preview-output-change/{status_key}",
            base_change_id=_preview_output_change_id(record.job_id, status_key),
        )
        try:
            if additions:
                if handle is None:  # pragma: no cover - guarded by outputs_enabled
                    raise RealmIntegrityError(
                        "Environment Preview output adoption lacks its session."
                    )
                held = self._ledger.hold_owner_content(
                    operation_id=_operation(
                        record.job_id,
                        f"preview-output-hold/{status_key}/"
                        f"{request_digest({'change_id': change.change_id})[:16]}",
                    ),
                    actor_principal_id=self.principal_id,
                    change_id=change.change_id,
                    memberships=additions,
                    source_owner_id=handle.session.owner_id,
                )
                if tuple(held) != additions:
                    raise RealmIntegrityError(
                        "Environment Preview output adoption changed membership identity."
                    )
            result_details = dict(details)
            if diagnostics:
                result_details["interface_output_diagnostics"] = diagnostics
            if diagnostics_truncated:
                result_details["interface_output_diagnostics_truncated"] = True
            result = OperatorJobResult(
                result_kind=ENVIRONMENT_PREVIEW_JOB_KIND,
                status=result_status,
                metrics={},
                constraint_results={},
                event_summary={},
                declared_outputs=outputs,
                logs=(),
                details=result_details,
            )
            return self._finish(
                record,
                proof=terminal,
                result=result,
                status=terminal_status,
                code=code,
                change_id=change.change_id,
                additions=additions,
                managed=managed,
                admission=admission,
            )
        except RealmConflict:
            current = self.read(job_id=record.job_id)
            if current.state.terminal:
                self._reconcile_terminal_cleanup(current, managed=managed)
                return self.read(job_id=record.job_id)
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix=f"abort-preview-output-change/{status_key}",
            )
            if current.state is OperatorJobState.STOPPING and (
                terminal_status is not OperatorJobTerminalStatus.CANCELLED
            ):
                return self._finish_preview_cancelled(
                    current,
                    terminal=terminal,
                    managed=managed,
                    admission=admission,
                )
            raise
        except BaseException:
            current = self.read(job_id=record.job_id)
            if not current.state.terminal:
                self._abort_output_change(
                    record=record,
                    change_id=change.change_id,
                    operation_suffix=f"abort-preview-output-change/{status_key}",
                )
            raise

    def _finish_preview_observation(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
        managed: ManagedEnvironmentPreviewBinding,
        admission: LeaseRecord,
    ) -> OperatorJobRecord:
        self._validate_preview_terminal(record, terminal)
        if record.state is OperatorJobState.STOPPING:
            return self._finish_preview_cancelled(
                record,
                terminal=terminal,
                managed=managed,
                admission=admission,
            )
        context = self._preview_context_for_record(record)
        succeeded = terminal.started and terminal.exit_code == 0
        return self._finish_preview_with_outputs(
            record,
            terminal=terminal,
            managed=managed,
            admission=admission,
            result_status="success" if succeeded else "failed",
            terminal_status=(
                OperatorJobTerminalStatus.SUCCEEDED
                if succeeded
                else OperatorJobTerminalStatus.FAILED
            ),
            code=("completed" if succeeded else "environment_preview_process_failed"),
            details={
                "exit_code": terminal.exit_code,
                "profile_id": context.preview_plan.profile_id,
            },
        )

    def _finish_preview_failure(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
        code: str,
        stage: str,
        failure_type: str,
        managed: ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        self._validate_preview_terminal(record, terminal)
        if record.state is OperatorJobState.STOPPING:
            return self._finish_preview_cancelled(
                record,
                terminal=terminal,
                managed=managed,
                admission=admission,
            )
        context = self._preview_context_for_record(record)
        return self._finish_preview_with_outputs(
            record,
            terminal=terminal,
            managed=managed,
            admission=admission,
            result_status="failed",
            terminal_status=OperatorJobTerminalStatus.FAILED,
            code=code,
            details={
                "exit_code": terminal.exit_code,
                "failure_type": failure_type,
                "profile_id": context.preview_plan.profile_id,
                "stage": stage,
            },
        )

    def _finish_preview_stopping(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedEnvironmentPreviewBinding | None = None,
        admission: LeaseRecord | None = None,
    ) -> OperatorJobRecord:
        if admission is None:
            try:
                admission = self._validate_launch_admission(record)
            except RealmError:
                admission = None
        if managed is None and admission is not None:
            try:
                managed = self._realize_preview_binding(
                    record=record,
                    admission=admission,
                    recover_only=True,
                )
            except RealmError:
                managed = None
        _binder, provider, _authority = self._preview_execution_dependencies()
        if managed is None:
            terminal = self._stop_preview_without_live_binding(record)
        else:
            self._validate_preview_launch_request(record, managed)
            terminal = provider.stop(managed.request)
        return self._finish_preview_cancelled(
            self.read(job_id=record.job_id),
            terminal=terminal,
            managed=managed,
            admission=admission,
        )

    def _finish_preview_cancelled(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal,
        managed: ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        self._validate_preview_terminal(record, terminal)
        context = self._preview_context_for_record(record)
        reason = record.stop.reason_code if record.stop is not None else "cancelled"
        return self._finish_preview_with_outputs(
            record,
            terminal=terminal,
            managed=managed,
            admission=admission,
            result_status="cancelled",
            terminal_status=OperatorJobTerminalStatus.CANCELLED,
            code=reason,
            details={
                "exit_code": terminal.exit_code,
                "profile_id": context.preview_plan.profile_id,
                "reason_code": reason,
            },
        )

    def _finish_stopping(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedOperatorAttemptBinding | None = None,
        admission: LeaseRecord | None = None,
    ) -> OperatorJobRecord:
        intent = _require_launch_intent(record)
        if admission is None:
            try:
                admission = self._validate_launch_admission(record)
            except RealmError:
                admission = None
        if managed is None and admission is not None:
            try:
                context = self._context_for_record(record)
                managed = self._attempt_binder.recover_existing(
                    actor_principal_id=self.principal_id,
                    job_id=record.job_id,
                    owner_id=record.owner_id,
                    admission_lease=admission,
                    attempt_id=context.attempt_id,
                    binding_id=context.binding_id,
                    launch_token=context.launch_token,
                    evidence_fingerprint=context.evidence_fingerprint,
                    evaluation_spec=context.evaluation_spec,
                    portable_spec=context.portable_spec,
                    ttl_seconds=_resource_ttl_seconds(record),
                )
            except (RealmError, OSError):
                managed = None
        reconciliation = self._launcher.reconcile_noncanonical_terminal(
            launch_token=intent.launch_token,
            binding_id=intent.binding_id,
            evidence_fingerprint=intent.evidence_fingerprint,
            launch_request_digest=intent.launch_request_digest,
        )
        return self._finish_cancelled(
            self.read(job_id=record.job_id),
            proof=reconciliation.proof,
            managed=managed,
            admission=admission,
        )

    def _finish_cancelled(
        self,
        record: OperatorJobRecord,
        *,
        proof: WorkerTerminalProof,
        managed: ManagedOperatorAttemptBinding | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        reason = record.stop.reason_code if record.stop is not None else "cancelled"
        return self._finish_with_empty_capture(
            record,
            proof=proof,
            result=OperatorJobResult(
                result_kind=CANDIDATE_DEBUG_JOB_KIND,
                status="cancelled",
                metrics={},
                constraint_results={},
                event_summary={},
                declared_outputs=(),
                logs=(),
                details={"reason_code": reason},
            ),
            status=OperatorJobTerminalStatus.CANCELLED,
            code=reason,
            managed=managed,
            admission=admission,
        )

    def _finish_with_empty_capture(
        self,
        record: OperatorJobRecord,
        *,
        proof: WorkerTerminalProof | LocalContainerWebTerminal,
        result: OperatorJobResult,
        status: OperatorJobTerminalStatus,
        code: str,
        managed: ManagedOperatorAttemptBinding | ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        change = self._begin_terminal_owner_change(
            record,
            operation_phase="terminal-change",
            base_change_id=_terminal_change_id(record.job_id),
        )
        try:
            return self._finish(
                record,
                proof=proof,
                result=result,
                status=status,
                code=code,
                change_id=change.change_id,
                additions=(),
                managed=managed,
                admission=admission,
            )
        except RealmConflict:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-terminal-change-after-conflict",
            )
            current = self.read(job_id=record.job_id)
            if not current.state.terminal:
                raise
            self._reconcile_terminal_cleanup(current, managed=managed)
            return self.read(job_id=record.job_id)
        except BaseException:
            self._abort_output_change(
                record=record,
                change_id=change.change_id,
                operation_suffix="abort-terminal-change-after-error",
            )
            raise

    def _finish(
        self,
        record: OperatorJobRecord,
        *,
        proof: WorkerTerminalProof | LocalContainerWebTerminal,
        result: OperatorJobResult,
        status: OperatorJobTerminalStatus,
        code: str,
        change_id: str,
        additions: Sequence[OwnerMembership],
        managed: ManagedOperatorAttemptBinding | ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        intent = _require_launch_intent(record)
        outcome = OperatorJobOutcome(
            status=status,
            code=code,
            started=proof.started,
            disposition=OperatorJobTerminalDisposition(proof.disposition),
            terminal_proof_digest=hashlib.sha256(proof.canonical_bytes).hexdigest(),
            evidence_digest=result.digest,
            detail_digest=(
                request_digest(result.to_dict()["details"])
                if result.details
                else None
            ),
        )
        terminal = self._ledger.finish_operator_job(
            operation_id=_operation(record.job_id, "finish"),
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            expected_revision=record.revision,
            launch_token=intent.launch_token,
            admission_lease_id=intent.admission_lease_id,
            admission_fencing_token=intent.admission_fencing_token,
            change_id=change_id,
            expected_owner_revision=0,
            additions=tuple(additions),
            outcome=outcome,
            result=result,
        )
        self._cleanup_after_terminal(
            terminal,
            managed=managed,
            admission=admission,
            proof=proof,
            launch_request_digest=intent.launch_request_digest,
        )
        return self.read(job_id=record.job_id)

    def _cleanup_after_terminal(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedOperatorAttemptBinding | ManagedEnvironmentPreviewBinding | None,
        admission: LeaseRecord | None,
        proof: WorkerTerminalProof | LocalContainerWebTerminal,
        launch_request_digest: str,
    ) -> None:
        if not record.state.terminal:
            raise RealmConflict("Operator Job cleanup requires a terminal ledger head.")
        if hashlib.sha256(proof.canonical_bytes).hexdigest() != (
            record.outcome.outcome.terminal_proof_digest
            if record.outcome is not None
            else None
        ):
            raise RealmIntegrityError(
                "Operator Job cleanup proof differs from its terminal commit."
            )
        if launch_request_digest != _require_launch_intent(record).launch_request_digest:
            raise RealmIntegrityError(
                "Operator Job cleanup request differs from its launch intent."
            )
        if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
            if not isinstance(proof, LocalContainerWebTerminal):
                raise RealmIntegrityError(
                    "Environment Preview cleanup requires container terminal proof."
                )
            with self._cleanup_lock:
                self._reconcile_preview_terminal_cleanup_locked(
                    record,
                    terminal=proof,
                    admission=admission,
                    managed=(
                        managed
                        if isinstance(managed, ManagedEnvironmentPreviewBinding)
                        else None
                    ),
                )
            return
        self._reconcile_terminal_cleanup(
            record,
            managed=(
                managed
                if isinstance(managed, ManagedOperatorAttemptBinding)
                else None
            ),
        )

    def _reconcile_terminal_cleanup(
        self,
        record: OperatorJobRecord,
        *,
        managed: (
            ManagedOperatorAttemptBinding
            | ManagedEnvironmentPreviewBinding
            | None
        ) = None,
    ) -> None:
        """Replay exact provider/resource cleanup from a terminal ledger head."""

        with self._cleanup_lock:
            self._reconcile_terminal_cleanup_locked(record, managed=managed)

    def _reconcile_terminal_cleanup_locked(
        self,
        record: OperatorJobRecord,
        *,
        managed: (
            ManagedOperatorAttemptBinding
            | ManagedEnvironmentPreviewBinding
            | None
        ) = None,
    ) -> None:
        """Serialized implementation for local concurrent cleanup adopters."""

        if not record.state.terminal:
            raise RealmConflict("Operator Job cleanup requires a terminal ledger head.")
        if (
            record.cleanup_state is OperatorJobCleanupState.COMPLETE
            and managed is None
        ):
            return
        if record.cleanup_state not in {
            OperatorJobCleanupState.PENDING,
            OperatorJobCleanupState.COMPLETE,
        }:
            raise RealmIntegrityError("Terminal Operator Job cleanup state is invalid.")
        if record.launch_intent is None:
            if record.state is OperatorJobState.CANCELLED:
                if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
                    self._cleanup_unlaunched_preview_cancellation(
                        record,
                        managed=(
                            managed
                            if isinstance(
                                managed, ManagedEnvironmentPreviewBinding
                            )
                            else None
                        ),
                    )
                else:
                    self._cleanup_unlaunched_cancellation(
                        record,
                        managed=(
                            managed
                            if isinstance(managed, ManagedOperatorAttemptBinding)
                            else None
                        ),
                    )
                return
            raise RealmIntegrityError("Terminal Operator Job lacks launch authority.")

        if record.plan.job_kind == ENVIRONMENT_PREVIEW_JOB_KIND:
            self._reconcile_preview_terminal_cleanup_locked(
                record,
                managed=(
                    managed
                    if isinstance(managed, ManagedEnvironmentPreviewBinding)
                    else None
                ),
            )
            return

        intent = record.launch_intent
        reconciliation = self._launcher.reconcile_noncanonical_terminal(
            launch_token=intent.launch_token,
            binding_id=intent.binding_id,
            evidence_fingerprint=intent.evidence_fingerprint,
            launch_request_digest=intent.launch_request_digest,
        )
        proof = reconciliation.proof
        self._validate_terminal_cleanup_proof(record, proof)
        # Reconciliation prior_state changes to ``retired`` on replay; the
        # authenticated terminal proof itself is byte-identical across every
        # successful provider cleanup adoption.
        provider_evidence = hashlib.sha256(proof.canonical_bytes).hexdigest()
        try:
            admission = self._validate_launch_admission(record)
        except RealmError:
            admission = None
        self._cleanup_debug_terminal_resources(
            record,
            authority=proof,
            provider_evidence_digest=provider_evidence,
            admission=admission,
            managed=(
                managed
                if isinstance(managed, ManagedOperatorAttemptBinding)
                else None
            ),
        )

    def _cleanup_debug_terminal_resources(
        self,
        record: OperatorJobRecord,
        *,
        authority: WorkerTerminalProof | ProcessLaunchSealReceipt,
        provider_evidence_digest: str | None = None,
        admission: LeaseRecord | None = None,
        managed: ManagedOperatorAttemptBinding | None = None,
    ) -> None:
        """Prove provider/resources before releasing admission and capacity."""

        context = self._context_for_record(record)
        cleanup = self._attempt_binder.cleanup_after_terminal(
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            owner_id=record.owner_id,
            admission_lease=admission,
            operator_plan_digest=record.plan_digest,
            attempt_id=context.attempt_id,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            evidence_fingerprint=context.evidence_fingerprint,
            evaluation_spec=context.evaluation_spec,
            portable_spec=context.portable_spec,
            authority=authority,
            ttl_seconds=_resource_ttl_seconds(record),
        )
        if managed is not None:
            managed.detach_after_terminal_cleanup(cleanup)
        if record.cleanup_state is OperatorJobCleanupState.COMPLETE:
            # Another adopter already completed the durable accounting chain.
            # Re-proving provider/resource cleanup above is sufficient
            # authority to close this process-local attachment; avoid replaying
            # accounting from a later observation point.
            return
        intent = record.launch_intent
        if intent is None:
            lease_id = _admission_lease_id(record.job_id)
            holder_id = _admission_holder_id(record.job_id)
            fencing_token = 1
        else:
            lease_id = intent.admission_lease_id
            holder_id = intent.admission_holder_id
            fencing_token = intent.admission_fencing_token
        try:
            admission_receipt = self._release_admission_identity(
                job_id=record.job_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
            )
        except RealmNotFound:
            admission_receipt = None
        self._complete_cleanup(
            record,
            provider_evidence_digest=(
                cleanup.terminal_authority_digest
                if provider_evidence_digest is None
                else provider_evidence_digest
            ),
            resources_evidence_digest=cleanup.digest,
            admission=admission_receipt,
        )

    def _cleanup_preview_terminal_resources(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal | None = None,
        admission: LeaseRecord | None = None,
        managed: ManagedEnvironmentPreviewBinding | None = None,
    ) -> None:
        """Prove all Preview cleanup before releasing accounting authority."""

        binder, _provider, _authority = self._preview_execution_dependencies()
        context = self._preview_context_for_record(record)
        target = self._inspection.resolve_candidate(selection=context.selection)
        cleanup = binder.cleanup_after_terminal(
            actor_principal_id=self.principal_id,
            job_id=record.job_id,
            owner_id=record.owner_id,
            admission_lease=admission,
            operator_plan_digest=record.plan_digest,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            target=target,
            preview_plan=context.preview_plan,
            terminal=terminal,
            ttl_seconds=_resource_ttl_seconds(record),
        )
        if managed is not None:
            managed.detach_after_terminal_cleanup(cleanup)
        output_retirement_digest = self._retire_preview_output_session(
            record,
            required=(
                record.launch_intent is not None
                and context.preview_plan.outputs_enabled
            ),
        )
        resources_evidence_digest = request_digest(
            {
                "binding_cleanup_digest": cleanup.digest,
                "interface_output_retirement_digest": output_retirement_digest,
                "schema": "optpilot.environment-preview-resource-cleanup.v2",
            }
        )
        if record.cleanup_state is OperatorJobCleanupState.COMPLETE:
            # As in the Debug path, exact cleanup evidence authorizes local
            # detachment while the completed ledger head already proves that
            # admission and capacity were released in the required order.
            return
        intent = record.launch_intent
        if intent is None:
            lease_id = _admission_lease_id(record.job_id)
            holder_id = _admission_holder_id(record.job_id)
            fencing_token = 1
        else:
            lease_id = intent.admission_lease_id
            holder_id = intent.admission_holder_id
            fencing_token = intent.admission_fencing_token
        try:
            admission_receipt = self._release_admission_identity(
                job_id=record.job_id,
                lease_id=lease_id,
                holder_id=holder_id,
                fencing_token=fencing_token,
            )
        except RealmNotFound:
            admission_receipt = None
        self._complete_cleanup(
            record,
            provider_evidence_digest=cleanup.provider_cleanup_digest,
            resources_evidence_digest=resources_evidence_digest,
            admission=admission_receipt,
        )

    def _retire_preview_output_session(
        self,
        record: OperatorJobRecord,
        *,
        required: bool,
    ) -> str | None:
        if not self._preview_context_for_record(record).preview_plan.outputs_enabled:
            return None
        service = self._preview_output_service()
        try:
            handle = service.recover_session(launch_id=record.job_id)
        except RealmNotFound as error:
            if required:
                raise RealmIntegrityError(
                    "Launched Environment Preview lacks its output session."
                ) from error
            return None
        receipt = service.retire_session(
            operation_id=_operation(
                record.job_id, "preview-output-session/retire"
            ),
            handle=handle,
        )
        return request_digest(receipt.to_dict())

    def _reconcile_preview_terminal_cleanup_locked(
        self,
        record: OperatorJobRecord,
        *,
        terminal: LocalContainerWebTerminal | None = None,
        admission: LeaseRecord | None = None,
        managed: ManagedEnvironmentPreviewBinding | None = None,
    ) -> None:
        """Replay exact container/resources/admission cleanup for one Preview."""

        self._cleanup_preview_terminal_resources(
            record,
            terminal=terminal,
            admission=admission,
            managed=managed,
        )

    @staticmethod
    def _validate_terminal_cleanup_proof(
        record: OperatorJobRecord,
        proof: WorkerTerminalProof | LocalContainerWebTerminal,
    ) -> None:
        if record.outcome is None:
            raise RealmIntegrityError("Terminal Operator Job lacks an outcome.")
        expected = record.outcome.outcome.terminal_proof_digest
        actual = hashlib.sha256(proof.canonical_bytes).hexdigest()
        if expected != actual:
            raise RealmIntegrityError(
                "Operator Job cleanup proof differs from its terminal outcome."
            )

    def _cleanup_unlaunched_cancellation(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedOperatorAttemptBinding | None = None,
    ) -> None:
        context = self._context_for_record(record)
        seal = self._launcher.seal_noncanonical_launch_if_absent(
            launch_token=context.launch_token,
            binding_id=context.binding_id,
        )
        authority: WorkerTerminalProof | ProcessLaunchSealReceipt = seal
        provider_evidence: str | None = None
        if seal.prior_state == "existing":
            proof = self._launcher.reconcile_unbound_noncanonical_terminal(
                seal=seal
            )
            if (
                proof.launch_token != context.launch_token
                or proof.binding_id != context.binding_id
                or proof.evidence_fingerprint != context.evidence_fingerprint
                or proof.disposition != "never_started"
            ):
                raise RealmIntegrityError(
                    "Passive Operator Job reservation proof differs from its plan."
                )
            authority = proof
            provider_evidence = request_digest(
                {
                    "format": "optpilot.operator-job-passive-provider-cleanup.v1",
                    "terminal_proof_digest": hashlib.sha256(
                        proof.canonical_bytes
                    ).hexdigest(),
                }
            )
        try:
            admission = self._validate_deterministic_admission(record)
        except RealmError:
            admission = None
        self._cleanup_debug_terminal_resources(
            record,
            authority=authority,
            provider_evidence_digest=provider_evidence,
            admission=admission,
            managed=managed,
        )

    def _cleanup_unlaunched_preview_cancellation(
        self,
        record: OperatorJobRecord,
        *,
        managed: ManagedEnvironmentPreviewBinding | None = None,
    ) -> None:
        if record.plan.job_kind != ENVIRONMENT_PREVIEW_JOB_KIND:
            raise RealmConflict("Operator Job is not an Environment Preview.")
        if record.launch_intent is not None:
            raise RealmConflict(
                "Launched Environment Preview requires terminal cleanup."
            )
        try:
            admission = self._validate_deterministic_admission(record)
        except RealmError:
            admission = None
        self._cleanup_preview_terminal_resources(
            record,
            admission=admission,
            managed=managed,
        )

    def _complete_cleanup(
        self,
        record: OperatorJobRecord,
        *,
        provider_evidence_digest: str,
        resources_evidence_digest: str | None,
        admission: LeaseRecord | None,
    ) -> OperatorJobRecord:
        """Fence and durably acknowledge one fully reconciled cleanup chain."""

        current = self.read(job_id=record.job_id)
        if current.cleanup_state is OperatorJobCleanupState.COMPLETE:
            return current
        if (
            current.state is not record.state
            or current.outcome is None
            or current.cleanup_state is not OperatorJobCleanupState.PENDING
        ):
            raise RealmConflict("Operator Job cleanup lifecycle changed.")
        complete = OperatorJobCleanupComponentState.COMPLETE
        not_applicable = OperatorJobCleanupComponentState.NOT_APPLICABLE
        capacity = self._release_capacity(current)
        evidence = OperatorJobCleanupEvidence(
            terminal_revision=current.revision,
            terminal_outcome_digest=hashlib.sha256(
                canonical_json_bytes(current.outcome.to_dict())
            ).hexdigest(),
            provider=OperatorJobCleanupComponentEvidence(
                state=complete,
                evidence_digest=provider_evidence_digest,
            ),
            resources=OperatorJobCleanupComponentEvidence(
                state=(complete if resources_evidence_digest is not None else not_applicable),
                evidence_digest=resources_evidence_digest,
            ),
            capacity=OperatorJobCleanupComponentEvidence(
                state=(complete if capacity is not None else not_applicable),
                evidence_digest=(
                    None
                    if capacity is None
                    else _capacity_cleanup_digest(capacity)
                ),
            ),
            admission=OperatorJobCleanupComponentEvidence(
                state=(complete if admission is not None else not_applicable),
                evidence_digest=(
                    None
                    if admission is None
                    else request_digest(
                        {
                            "fencing_token": admission.fencing_token,
                            "holder_id": admission.holder_id,
                            "lease_id": admission.lease_id,
                            "state": admission.state.value,
                        }
                    )
                ),
            ),
        )
        intent = current.launch_intent
        return self._ledger.complete_operator_job_cleanup(
            operation_id=_operation(current.job_id, "complete-cleanup"),
            actor_principal_id=self.principal_id,
            job_id=current.job_id,
            expected_revision=current.revision,
            evidence=evidence,
            launch_token=None if intent is None else intent.launch_token,
            admission_lease_id=None if admission is None else admission.lease_id,
            admission_holder_id=None if admission is None else admission.holder_id,
            admission_fencing_token=(
                None if admission is None else admission.fencing_token
            ),
            capacity_reservation_id=(
                None if capacity is None else capacity.reservation_id
            ),
            capacity_holder_id=(None if capacity is None else capacity.holder_id),
            capacity_fencing_token=(
                None if capacity is None else capacity.fencing_token
            ),
        )

    # -- capacity and admission -------------------------------------------

    def _ensure_capacity(
        self, record: OperatorJobRecord
    ) -> OperatorCapacityReservationRecord:
        reservation_id = operator_capacity_reservation_id(
            record.plan.backend_realm, record.job_id
        )
        try:
            existing = self._ledger.read_operator_capacity_reservation(
                actor_principal_id=self.principal_id,
                reservation_id=reservation_id,
            )
        except RealmNotFound:
            existing = None
        if existing is not None:
            self._validate_capacity_record(record, existing)
            if existing.state is OperatorCapacityReservationState.ACTIVE:
                return self._validate_capacity(record, existing)
            if existing.state is OperatorCapacityReservationState.RELEASED:
                if (
                    record.state is not OperatorJobState.QUEUED
                    or record.launch_intent is not None
                ):
                    raise RealmConflict(
                        "Released Operator Job capacity cannot authorize execution."
                    )
            generation = existing.generation + 1
        else:
            generation = 1
        reservation = self._ledger.acquire_operator_capacity_reservation(
            operation_id=_operation(
                record.job_id, f"acquire-capacity/generation-{generation}"
            ),
            actor_principal_id=self.principal_id,
            pool_name=record.plan.backend_realm,
            job_id=record.job_id,
            holder_id=_capacity_holder_id(record.job_id),
            ttl_seconds=_capacity_ttl_seconds(record),
        )
        self._validate_capacity_record(record, reservation)
        return reservation

    def _validate_capacity(
        self,
        record: OperatorJobRecord,
        reservation: OperatorCapacityReservationRecord | None = None,
    ) -> OperatorCapacityReservationRecord:
        if reservation is None:
            reservation = self._ledger.read_operator_capacity_reservation(
                actor_principal_id=self.principal_id,
                reservation_id=operator_capacity_reservation_id(
                    record.plan.backend_realm, record.job_id
                ),
            )
        self._validate_capacity_record(record, reservation)
        validated = self._ledger.validate_operator_capacity_reservation(
            actor_principal_id=self.principal_id,
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
        )
        self._validate_capacity_record(record, validated)
        return validated

    @staticmethod
    def _validate_capacity_record(
        record: OperatorJobRecord,
        reservation: OperatorCapacityReservationRecord,
    ) -> None:
        if (
            reservation.job_id != record.job_id
            or reservation.pool_name != record.plan.backend_realm
            or reservation.plan_digest != record.plan_digest
            or dict(reservation.claims) != dict(record.plan.resource_claims)
            or reservation.holder_id != _capacity_holder_id(record.job_id)
        ):
            raise RealmIntegrityError(
                "Operator Job capacity differs from its immutable plan."
            )
        intent = record.launch_intent
        if intent is not None and (
            intent.capacity_reservation_id != reservation.reservation_id
            or intent.capacity_holder_id != reservation.holder_id
            or intent.capacity_fencing_token != reservation.fencing_token
        ):
            raise RealmConflict(
                "Operator Job capacity differs from its launch authority."
            )

    def _release_capacity(
        self, record: OperatorJobRecord
    ) -> OperatorCapacityReservationRecord | None:
        reservation_id = operator_capacity_reservation_id(
            record.plan.backend_realm, record.job_id
        )
        try:
            reservation = self._ledger.read_operator_capacity_reservation(
                actor_principal_id=self.principal_id,
                reservation_id=reservation_id,
            )
        except RealmNotFound:
            return None
        self._validate_capacity_record(record, reservation)
        released = self._ledger.release_operator_capacity_reservation(
            operation_id=_operation(
                record.job_id,
                f"release-capacity/generation-{reservation.generation}",
            ),
            actor_principal_id=self.principal_id,
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
        )
        self._validate_capacity_record(record, released)
        if released.state not in {
            OperatorCapacityReservationState.RELEASED,
            OperatorCapacityReservationState.EXPIRED,
        }:
            raise RealmIntegrityError(
                "Operator Job capacity was not durably released."
            )
        pool = self._ledger.read_operator_capacity_pool(
            pool_name=released.pool_name
        )
        if pool.state.value == "blocked":
            self._ledger.ensure_operator_capacity_pool(
                operation_id=_operation(
                    record.job_id,
                    "reconcile-capacity-pool/"
                    f"revision-{pool.revision}/"
                    f"release-{released.updated_txn_id}",
                ),
                actor_principal_id=self.principal_id,
                pool_name=pool.pool_name,
                limits=dict(pool.limits),
            )
        return released

    def _ensure_admission(self, record: OperatorJobRecord) -> LeaseRecord:
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=self.principal_id,
            owner_id=record.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        return self._ledger.acquire_lease(
            operation_id=_operation(record.job_id, "admission"),
            actor_principal_id=self.principal_id,
            owner_id=record.owner_id,
            lease_kind="operator-job-admission",
            audience="operator-job",
            holder_id=_admission_holder_id(record.job_id),
            scope_key=f"operator-job-admission:{record.job_id}",
            ttl_seconds=max(
                _MIN_ADMISSION_TTL_SECONDS,
                _resource_ttl_seconds(record) + 300.0,
            ),
            metadata={"job_id": record.job_id, "plan_digest": record.plan_digest},
            content_roots=memberships,
            lease_id=_admission_lease_id(record.job_id),
        )

    def _validate_launch_admission(self, record: OperatorJobRecord) -> LeaseRecord:
        intent = _require_launch_intent(record)
        return self._ledger.validate_lease(
            actor_principal_id=self.principal_id,
            lease_id=intent.admission_lease_id,
            holder_id=intent.admission_holder_id,
            fencing_token=intent.admission_fencing_token,
        )

    def _validate_deterministic_admission(
        self, record: OperatorJobRecord
    ) -> LeaseRecord:
        return self._ledger.validate_lease(
            actor_principal_id=self.principal_id,
            lease_id=_admission_lease_id(record.job_id),
            holder_id=_admission_holder_id(record.job_id),
            fencing_token=1,
        )

    def _release_admission(self, job_id: str, admission: LeaseRecord) -> LeaseRecord:
        self._release_capacity(self.read(job_id=job_id))
        return self._ledger.release_lease(
            operation_id=_operation(job_id, "release-admission"),
            actor_principal_id=self.principal_id,
            lease_id=admission.lease_id,
            holder_id=admission.holder_id,
            fencing_token=admission.fencing_token,
        )

    def _release_admission_identity(
        self,
        *,
        job_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
    ) -> LeaseRecord:
        self._release_capacity(self.read(job_id=job_id))
        return self._ledger.release_lease(
            operation_id=_operation(job_id, "release-admission"),
            actor_principal_id=self.principal_id,
            lease_id=lease_id,
            holder_id=holder_id,
            fencing_token=fencing_token,
        )


def _select_source_memberships(
    memberships: Sequence[OwnerMembership],
) -> tuple[OwnerMembership, ...]:
    """Choose one deterministic current placement for each semantic source."""

    selected: dict[tuple[str, str], OwnerMembership] = {}
    for item in sorted(
        set(memberships),
        key=lambda value: (value.role, str(value.content_ref), value.store_id),
    ):
        selected.setdefault((item.role, str(item.content_ref)), item)
    if not selected:
        raise RealmConflict("Operator Job target has no retained source content.")
    return tuple(selected[key] for key in sorted(selected))


def _resource_ttl_seconds(record: OperatorJobRecord) -> float:
    """Keep resources live through the approved timeout and restart recovery."""

    return max(
        OPERATOR_JOB_RESOURCE_TTL_SECONDS,
        record.plan.timeout_seconds + _RECOVERY_MARGIN_SECONDS,
    )


def _capacity_ttl_seconds(record: OperatorJobRecord) -> float:
    """Keep accounting authority at least as long as retained resources."""

    return _resource_ttl_seconds(record)


def _capacity_holder_id(job_id: str) -> str:
    return _logical_id("operator-job-capacity-holder", {"job_id": job_id})


def _capacity_cleanup_digest(
    reservation: OperatorCapacityReservationRecord,
) -> str:
    return request_digest(
        {
            "claims_digest": reservation.claims_digest,
            "fencing_token": reservation.fencing_token,
            "generation": reservation.generation,
            "holder_id": reservation.holder_id,
            "pool_name": reservation.pool_name,
            "pool_revision": reservation.pool_revision,
            "reservation_id": reservation.reservation_id,
            "schema": "optpilot.operator-job-capacity-cleanup.v1",
            "state": reservation.state.value,
        }
    )


def _resource_cleanup_digest(
    record: OperatorJobRecord, *, disposition: str
) -> str:
    """Bind a resource-release attestation to one immutable launch scope."""

    return request_digest(
        {
            "binding_id": (
                None if record.launch_intent is None else record.launch_intent.binding_id
            ),
            "disposition": disposition,
            "job_id": record.job_id,
            "owner_id": record.owner_id,
            "plan_digest": record.plan_digest,
            "runtime_fingerprint": record.plan.runtime_fingerprint,
            "schema": "optpilot.operator-job-resource-cleanup.v1",
        }
    )


def _job_owner_id(job_id: str) -> str:
    return _logical_id("operator-job-owner", {"job_id": job_id})


def _execution_identities(job_id: str) -> tuple[str, str, str]:
    return (
        _logical_id("debug-attempt", {"job_id": job_id}),
        _logical_id("debug-binding", {"job_id": job_id}),
        _logical_id("debug-launch", {"job_id": job_id}),
    )


def _preview_execution_identities(job_id: str) -> tuple[str, str]:
    return (
        _logical_id("environment-preview-binding", {"job_id": job_id}),
        _logical_id("environment-preview-launch", {"job_id": job_id}),
    )


def _preview_result_id(job_id: str) -> str:
    return _logical_id("environment-preview-result", {"job_id": job_id})


def _admission_lease_id(job_id: str) -> str:
    return _logical_id("operator-job-admission", {"job_id": job_id})


def _admission_holder_id(job_id: str) -> str:
    return _logical_id("operator-job-holder", {"job_id": job_id})


def _output_change_id(job_id: str) -> str:
    return _logical_id("operator-job-output-change", {"job_id": job_id})


def _preview_output_change_id(job_id: str, terminal_status: str) -> str:
    return _logical_id(
        "operator-job-preview-output-change",
        {"job_id": job_id, "terminal_status": terminal_status},
    )


def _terminal_change_id(job_id: str) -> str:
    return _logical_id("operator-job-terminal-change", {"job_id": job_id})


def _logical_id(prefix: str, payload: Mapping[str, Any]) -> str:
    digest = request_digest(
        {"payload": dict(payload), "schema": f"optpilot.{prefix}.v1"}
    )
    return f"{prefix}-{digest[:40]}"


def _operation(job_id: str, phase: str) -> str:
    return f"operator-job/{job_id}/{phase}"


def _plain_digest(value: str) -> str:
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise RealmIntegrityError("Expected a lower hexadecimal SHA-256 digest.")
    return value


def _require_launch_intent(record: OperatorJobRecord):
    if record.launch_intent is None:
        raise RealmIntegrityError("Launched Operator Job lacks its launch intent.")
    return record.launch_intent


def _numeric_metrics(values: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RealmIntegrityError("Operator Job metric is not numeric.")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise RealmIntegrityError("Operator Job metric is not finite.")
        result[str(name)] = normalized
    return result


def _declared_output(artifact: CapturedArtifact) -> OperatorJobDeclaredOutput:
    declaration = artifact.declaration
    return OperatorJobDeclaredOutput(
        declaration_id=declaration.declaration_id,
        name=declaration.name,
        kind=declaration.kind,
        content_ref=artifact.content_ref,
        size_bytes=artifact.size_bytes,
        identity_digest=request_digest(
            {
                "content_ref": artifact.content_ref,
                "declaration": declaration.to_dict(),
                "schema": "optpilot.operator-job-declared-output-identity.v1",
                "size_bytes": artifact.size_bytes,
            }
        ),
        media_type=declaration.media_type,
    )


def _preview_declared_output(
    generation: InterfaceOutputGenerationRecord,
) -> OperatorJobDeclaredOutput:
    if not isinstance(generation, InterfaceOutputGenerationRecord):
        raise TypeError("Preview output generation is invalid.")
    content_ref = str(generation.content_ref)
    return OperatorJobDeclaredOutput(
        declaration_id=generation.output_id,
        name=generation.label,
        kind=generation.kind.value,
        content_ref=content_ref,
        size_bytes=generation.logical_bytes,
        identity_digest=request_digest(
            {
                "content_ref": content_ref,
                "logical_bytes": generation.logical_bytes,
                "output_id": generation.output_id,
                "record_digest": generation.record_digest,
                "schema": "optpilot.operator-job-interface-output-identity.v1",
            }
        ),
        media_type=None,
    )


def _preview_output_memberships(
    generations: Sequence[InterfaceOutputGenerationRecord],
) -> tuple[OwnerMembership, ...]:
    by_identity: dict[tuple[str, str, str], OwnerMembership] = {}
    for generation in generations:
        membership = OwnerMembership(
            generation.store_id,
            generation.content_ref,
            OPERATOR_JOB_OUTPUT_ROLE,
        )
        identity = (
            generation.store_id,
            str(generation.content_ref),
            OPERATOR_JOB_OUTPUT_ROLE,
        )
        by_identity[identity] = membership
    return tuple(by_identity[key] for key in sorted(by_identity))


def _output_memberships(
    artifacts: Sequence[CapturedArtifact],
) -> tuple[OwnerMembership, ...]:
    """Retain one owner membership per immutable content identity.

    Declarations remain distinct evidence records even when two logical names
    intentionally select the same file or tree.  Owner membership is a set,
    so adopting duplicate content must not manufacture duplicate additions.
    """

    by_identity: dict[tuple[str, str, str], OwnerMembership] = {}
    for artifact in artifacts:
        store_id = artifact.bindings[0]["store_id"]
        membership = OwnerMembership(
            store_id,
            parse_physical_content_ref(artifact.content_ref),
            OPERATOR_JOB_OUTPUT_ROLE,
        )
        identity = (store_id, artifact.content_ref, OPERATOR_JOB_OUTPUT_ROLE)
        by_identity[identity] = membership
    return tuple(by_identity[key] for key in sorted(by_identity))


def _job_log_metadata(log: LocalAttemptWorkerLog) -> OperatorJobLogMetadata:
    if not isinstance(log, LocalAttemptWorkerLog):
        raise TypeError("Operator Job worker log evidence is invalid.")
    return OperatorJobLogMetadata(
        stream=log.stream,
        byte_count=log.byte_count,
        line_count=log.line_count,
        truncated=log.truncated,
        content_digest=log.content_digest,
    )


def _normalize_result_tracebacks(value: Any) -> Any:
    """Make retained traceback fields portable without discarding errors.

    Attempt envelopes may carry conventional multi-line tracebacks, while the
    bounded Operator Job result record admits only trimmed text without control
    characters.  Preserve every non-empty traceback as one whitespace-normalized
    diagnostic string and omit only an empty traceback field.  Other error
    fields and unrelated result values remain unchanged and are still checked
    by :class:`OperatorJobResult`.
    """

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if key == "traceback" and isinstance(child, str):
                traceback_text = " ".join(child.split())
                if traceback_text:
                    normalized[key] = traceback_text
                continue
            normalized[key] = _normalize_result_tracebacks(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_result_tracebacks(child) for child in value]
    return value


def _failure_result(*, code: str, stage: str, failure_type: str) -> OperatorJobResult:
    return OperatorJobResult(
        result_kind=CANDIDATE_DEBUG_JOB_KIND,
        status="failed",
        metrics={},
        constraint_results={},
        event_summary={},
        declared_outputs=(),
        logs=(),
        details={"code": code, "failure_type": failure_type, "stage": stage},
    )


def _envelope_code(envelope: AttemptEnvelope) -> str:
    defaults = {
        "failed": "evaluation_failed",
        "invalid": "candidate_invalid",
        "partial": "evaluation_partial",
        "timeout": "evaluation_timeout",
    }
    candidate = envelope.error.get("code")
    if (
        isinstance(candidate, str)
        and candidate
        and len(candidate.encode("utf-8")) <= 128
        and "/" not in candidate
        and "\\" not in candidate
        and "\x00" not in candidate
    ):
        return candidate
    return defaults.get(envelope.outcome, "evaluation_failed")


__all__ = [
    "CANDIDATE_DEBUG_JOB_KIND",
    "CANDIDATE_EVALUATION_TARGET_KIND",
    "EnvironmentPreviewFinalCapturePending",
    "LOCAL_PROCESS_BACKEND_KIND",
    "LOCAL_PROCESS_BACKEND_REALM",
    "RealmOperatorJobService",
    "control_plane_never_started_proof",
]
