"""Durable Core lifecycle for retained study launch and run handoff.

Studio may request and render this service, but it never owns a launcher,
process, run status mirror, or controller fence.  Before handoff the durable
Operator Job is authoritative.  The immutable handoff row switches authority
to the canonical run and its fenced controller.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .method_launch_environment import (
    MethodLaunchEnvironment,
    MethodLaunchEnvironmentDescriptor,
    method_environment_names,
)
from .realm._validation import thaw_json
from .realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from .realm.operator_job_records import (
    OperatorJobCleanupState,
    OperatorJobLaunchPlan,
    OperatorJobRecord,
    OperatorJobResult,
    OperatorJobState,
    OperatorJobTarget,
    OperatorJobTerminalStatus,
    operator_job_id,
)
from .realm.operator_job_service import control_plane_never_started_proof
from .realm.owner_derivation import Binding, OwnerDerivationManifest
from .realm.owners import OwnerPermission
from .realm.refs import canonical_json_bytes, request_digest
from .realm.selections import SelectionRef
from .realm.study_definition import StudyDefinitionReceipt
from .realm.study_launch_records import (
    RunCancellationRequestRecord,
    StudyLaunchHandoffRecord,
)
from .realm_run_execution_service import (
    RunExecutionDeferred,
)
from .retained_study_service import RetainedStudyPreparationReceipt
from .run_execution_profile import (
    RunExecutionProfile,
    method_exchange_timeout_seconds,
)
from .study_launch_ids import local_study_operation_identities


STUDY_LAUNCH_JOB_KIND = "study-launch"
STUDY_DEFINITION_TARGET_KIND = "study-definition-run"
STUDY_LAUNCH_BACKEND_KIND = "realm-run-controller"
STUDY_LAUNCH_BACKEND_REALM = "local-host"
STUDY_LAUNCH_INPUT_SCHEMA = "optpilot.study-launch-input.v3"
STUDY_LAUNCH_RESULT_SCHEMA = "optpilot.study-launch-result.v1"
STUDY_LAUNCH_STARTUP_TIMEOUT_SECONDS = 900.0
METHOD_ENVIRONMENT_BINDING_SCHEMA = "optpilot.method-environment-binding.v1"
_METHOD_ENVIRONMENT_BINDING_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+@~-]*$"
)


class StudyLaunchDispatchDeferred(RealmConflict):
    """Another live fenced controller currently owns this handed-off run."""


def _normalize_execution_profile(
    value: RunExecutionProfile | None,
    *,
    definition: StudyDefinitionReceipt | None = None,
) -> RunExecutionProfile:
    if value is None:
        if definition is None:
            return RunExecutionProfile()
        method_contract = (
            definition.manifest.run_definition.method_revision.method_contract
        )
        runtime_requirements = method_contract.get("runtime_requirements")
        return RunExecutionProfile(
            method_request_timeout_seconds=method_exchange_timeout_seconds(
                runtime_requirements
            )
        )
    if not isinstance(value, RunExecutionProfile):
        raise TypeError("execution_profile must be a RunExecutionProfile or None.")
    return value


def _bounded_binding_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8", errors="strict")) > 512
    ):
        raise ValueError(f"{label} must be bounded nonempty text.")
    return value


def _bounded_binding_token(value: Any, label: str) -> str:
    result = _bounded_binding_text(value, label)
    try:
        result.encode("ascii", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be a path-free ASCII token.") from error
    if _METHOD_ENVIRONMENT_BINDING_TOKEN_RE.fullmatch(result) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return result


def _normalize_method_environment_binding(
    definition: StudyDefinitionReceipt,
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind retained Method requirements to non-secret local value revisions."""

    names = method_environment_names(definition.manifest.run_definition)
    if value is None:
        if names:
            joined = ", ".join(names)
            raise ValueError(
                "method_environment_binding is required for retained Method "
                f"variables: {joined}."
            )
        return {
            "binding_revision": "method-environment-none",
            "recoverability": "none",
            "requirements": [],
            "schema": METHOD_ENVIRONMENT_BINDING_SCHEMA,
        }
    if not isinstance(value, Mapping) or set(value) != {
        "binding_revision",
        "recoverability",
        "requirements",
        "schema",
    }:
        raise ValueError("method_environment_binding fields differ.")
    if value.get("schema") != METHOD_ENVIRONMENT_BINDING_SCHEMA:
        raise ValueError("method_environment_binding schema is unsupported.")
    binding_revision = _bounded_binding_token(
        value.get("binding_revision"),
        "method_environment_binding.binding_revision",
    )
    recoverability = value.get("recoverability")
    if recoverability not in {"none", "process-lifetime", "settings-revision"}:
        raise ValueError(
            "method_environment_binding.recoverability is unsupported."
        )
    raw_requirements = value.get("requirements")
    if isinstance(raw_requirements, (str, bytes)) or not isinstance(
        raw_requirements, Sequence
    ):
        raise ValueError(
            "method_environment_binding.requirements must be a list."
        )
    requirements: list[dict[str, str]] = []
    for index, raw in enumerate(raw_requirements):
        if not isinstance(raw, Mapping) or set(raw) != {
            "name",
            "revision_id",
            "source",
        }:
            raise ValueError(
                "method_environment_binding requirement fields differ."
            )
        name = _bounded_binding_text(
            raw.get("name"),
            f"method_environment_binding.requirements[{index}].name",
        )
        source = raw.get("source")
        if source not in {"process-environment", "studio-settings"}:
            raise ValueError(
                "method_environment_binding requirement source is unsupported."
            )
        revision_id = _bounded_binding_token(
            raw.get("revision_id"),
            f"method_environment_binding.requirements[{index}].revision_id",
        )
        requirements.append(
            {
                "name": name,
                "revision_id": revision_id,
                "source": source,
            }
        )
    if tuple(item["name"] for item in requirements) != names:
        raise ValueError(
            "method_environment_binding requirements differ from the retained "
            "Method declaration."
        )
    if not names and recoverability != "none":
        raise ValueError(
            "A Method without local values must use recoverability 'none'."
        )
    if names and recoverability == "none":
        raise ValueError(
            "A Method with local values must declare binding recoverability."
        )
    expected_source = {
        "process-lifetime": "process-environment",
        "settings-revision": "studio-settings",
    }.get(recoverability)
    if expected_source is not None and any(
        item["source"] != expected_source for item in requirements
    ):
        raise ValueError(
            "method_environment_binding sources differ from recoverability."
        )
    return {
        "binding_revision": binding_revision,
        "recoverability": recoverability,
        "requirements": requirements,
        "schema": METHOD_ENVIRONMENT_BINDING_SCHEMA,
    }


class _RuntimeGraph(Protocol):
    actor_principal_id: str
    ledger: Any
    content_store: Any
    retained_study_service: Any
    operator_jobs: Any
    run_reader: Any
    closed: bool


@dataclass(frozen=True)
class StudyLaunchView:
    """Path-free public projection over one job, handoff, and optional run."""

    job: OperatorJobRecord
    handoff: StudyLaunchHandoffRecord | None
    run_summary: Mapping[str, Any] | None
    cancellation_request: RunCancellationRequestRecord | None

    @property
    def launch_id(self) -> str:
        return self.job.job_id

    @property
    def run_id(self) -> str | None:
        return None if self.handoff is None else self.handoff.run_id

    @property
    def status(self) -> str:
        if self.handoff is not None and self.run_summary is not None:
            run_status = str(self.run_summary.get("run_status") or "running")
            if run_status == "running" and self.cancellation_request is not None:
                return "stopping"
            if run_status == "succeeded":
                return "completed"
            return run_status
        states = {
            OperatorJobState.PLANNED: "queued",
            OperatorJobState.AWAITING_APPROVAL: "queued",
            OperatorJobState.QUEUED: "queued",
            OperatorJobState.STARTING: "running",
            OperatorJobState.RUNNING: "running",
            OperatorJobState.STOPPING: "stopping",
            OperatorJobState.SUCCEEDED: "completed",
            OperatorJobState.FAILED: "failed",
            OperatorJobState.CANCELLED: "cancelled",
        }
        return states[self.job.state]

    @property
    def can_stop(self) -> bool:
        if self.handoff is None:
            return not self.job.state.terminal and self.job.state is not OperatorJobState.STOPPING
        return bool(
            self.cancellation_request is None
            and self.run_summary is not None
            and self.run_summary.get("run_status") == "running"
        )

    def to_dict(self) -> dict[str, Any]:
        facts = dict(self.job.plan.input_facts)
        execution_profile = _plan_context(self.job)["execution_profile"]
        display = facts.get("display")
        display = dict(display) if isinstance(display, Mapping) else {}
        outcome = self.job.outcome
        finished_at = None if outcome is None else outcome.created_at
        return {
            "launch_id": self.launch_id,
            # ``job_id`` remains an alias while the Studio browser migrates
            # from its old process-local row vocabulary.
            "job_id": self.launch_id,
            "run_id": self.run_id,
            "study_name": display.get("study_name"),
            "environment_id": display.get("environment_id"),
            "method_id": display.get("method_id"),
            "status": self.status,
            "launch_state": self.job.state.value,
            "run_status": (
                None
                if self.run_summary is None
                else self.run_summary.get("run_status")
            ),
            "started_at": self.job.created_at,
            "finished_at": finished_at,
            "can_stop": self.can_stop,
            "cancellation_requested": self.cancellation_request is not None,
            "execution_profile": execution_profile.to_dict(),
            "summary": dict(self.run_summary or {}),
        }


class RealmStudyLaunchService:
    """Plan, hand off, recover, and cancel retained local studies."""

    def __init__(self, runtime: _RuntimeGraph) -> None:
        required = (
            "actor_principal_id",
            "ledger",
            "content_store",
            "retained_study_service",
            "operator_jobs",
            "run_reader",
        )
        if any(not hasattr(runtime, name) for name in required):
            raise TypeError("runtime does not provide the study launch service graph.")
        self._runtime = runtime

    @property
    def principal_id(self) -> str:
        return self._runtime.actor_principal_id

    def plan_local_package(
        self,
        *,
        operation_id: str,
        package_root: Path,
        study_config_path: Path,
        display_name: str | None = None,
        execution_profile: RunExecutionProfile | None = None,
        method_environment_binding: Mapping[str, Any] | None = None,
        process_environment_binding_revision: str | None = None,
        launch_inputs: Mapping[str, Any] | None = None,
    ) -> StudyLaunchView:
        """Retain one exact authored package and approve its launch job."""

        if (
            method_environment_binding is not None
            and process_environment_binding_revision is not None
        ):
            raise ValueError(
                "Choose an explicit Method environment binding or a "
                "process-lifetime binding revision, not both."
            )
        # Reject a malformed explicit operational override before capturing
        # any package content.  A missing profile is intentionally left as
        # ``None`` until the immutable Method revision has been compiled, so
        # its declared exchange timeout can supply the normal default.
        if execution_profile is not None:
            execution_profile = _normalize_execution_profile(execution_profile)
        identities = local_study_operation_identities(operation_id)
        preparation = self._runtime.retained_study_service.prepare_local_package(
            operation_id=operation_id,
            actor_principal_id=self.principal_id,
            store_id=self._runtime.content_store.store_id,
            package_root=package_root,
            study_config_path=study_config_path,
            source_owner_id=identities["source_owner_id"],
            study_definition_owner_id=identities["definition_owner_id"],
            launch_inputs=launch_inputs,
        )
        if (
            method_environment_binding is None
            and process_environment_binding_revision is not None
        ):
            names = method_environment_names(
                preparation.study_definition.manifest.run_definition
            )
            if names:
                revision = _bounded_binding_token(
                    process_environment_binding_revision,
                    "process_environment_binding_revision",
                )
                method_environment_binding = {
                    "binding_revision": revision,
                    "recoverability": "process-lifetime",
                    "requirements": [
                        {
                            "name": name,
                            "revision_id": revision,
                            "source": "process-environment",
                        }
                        for name in names
                    ],
                    "schema": METHOD_ENVIRONMENT_BINDING_SCHEMA,
                }
        return self.plan_definition(
            operation_id=operation_id,
            preparation=preparation,
            display_name=display_name,
            execution_profile=execution_profile,
            method_environment_binding=method_environment_binding,
        )

    def plan_definition(
        self,
        *,
        operation_id: str,
        preparation: RetainedStudyPreparationReceipt,
        display_name: str | None = None,
        execution_profile: RunExecutionProfile | None = None,
        method_environment_binding: Mapping[str, Any] | None = None,
    ) -> StudyLaunchView:
        """Approve one exact retained definition without realizing a runtime."""

        if not isinstance(preparation, RetainedStudyPreparationReceipt):
            raise TypeError("preparation must be a retained study receipt.")
        if display_name is not None and (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name.encode("utf-8")) > 256
        ):
            raise ValueError("display_name must be bounded nonempty text or None.")
        definition = preparation.study_definition
        execution_profile = _normalize_execution_profile(
            execution_profile,
            definition=definition,
        )
        environment_binding = _normalize_method_environment_binding(
            definition,
            method_environment_binding,
        )
        identities = local_study_operation_identities(operation_id)
        if definition.owner.owner_id != identities["definition_owner_id"]:
            raise RealmConflict("Study definition differs from its launch operation.")
        job_operation_id = _job_operation_id(operation_id)
        job_id = operator_job_id(job_operation_id)
        try:
            existing = self._runtime.operator_jobs.read(job_id=job_id)
        except RealmNotFound:
            existing = None
        if existing is not None:
            _validate_existing_plan(
                existing,
                definition,
                identities,
                execution_profile,
                environment_binding,
            )
            approved = self._ensure_approved(existing)
            return self._view(approved)

        owner_id = _job_owner_id(job_id)
        memberships = self._runtime.ledger.list_owner_memberships(
            actor_principal_id=self.principal_id,
            owner_id=definition.owner.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        anchor = self._runtime.ledger.read_owner_source_anchor(
            actor_principal_id=self.principal_id,
            owner_id=definition.owner.owner_id,
            revision=definition.owner.revision,
        )
        derivation = OwnerDerivationManifest(
            target_owner_id=owner_id,
            target_owner_kind="operator-job",
            sources=(anchor,),
            bindings=tuple(
                Binding(
                    source_owner_id=definition.owner.owner_id,
                    source_store_id=item.store_id,
                    content_ref=item.content_ref,
                    source_role=item.role,
                    target_role=item.role,
                )
                for item in memberships
            ),
        )
        self._runtime.ledger.derive_owner(
            operation_id=_phase(job_id, "derive-owner"),
            actor_principal_id=self.principal_id,
            manifest=derivation,
        )
        plan = _launch_plan(
            job_id=job_id,
            definition=definition,
            identities=identities,
            derivation=derivation,
            display_name=display_name,
            execution_profile=execution_profile,
            method_environment_binding=environment_binding,
        )
        planned = self._runtime.ledger.plan_operator_job(
            operation_id=_phase(job_id, "plan"),
            actor_principal_id=self.principal_id,
            job_owner_id=owner_id,
            plan=plan,
            job_id=job_id,
        )
        return self._view(self._ensure_approved(planned))

    def execute(
        self,
        *,
        launch_id: str,
        method_environment: (
            Mapping[str, str]
            | MethodLaunchEnvironment
            | MethodLaunchEnvironmentDescriptor
            | None
        ) = None,
    ) -> StudyLaunchView:
        """Reconcile one launch and, when claimed, drive its canonical run.

        Launch environment values are an ephemeral caller-to-Method-provider
        handoff.  They are deliberately absent from the durable launch plan.
        """

        current = self._runtime.operator_jobs.read(job_id=launch_id)
        if current.plan.job_kind != STUDY_LAUNCH_JOB_KIND:
            raise RealmConflict("Operator Job is not a study launch.")
        handoff = self._read_handoff_optional(launch_id)

        if current.state.terminal and current.cleanup_state.value == "pending":
            current = self._complete_terminal_cleanup(current)

        if current.state in {
            OperatorJobState.PLANNED,
            OperatorJobState.AWAITING_APPROVAL,
        }:
            current = self._ensure_approved(current)
        if current.state is OperatorJobState.QUEUED:
            context = _plan_context(current)
            current = self._runtime.operator_jobs.begin_control_plane_start(
                job_id=current.job_id,
                binding_id=context["binding_id"],
                launch_token=context["launch_token"],
                evidence_fingerprint=context["evidence_fingerprint"],
                launch_request_digest=context["launch_request_digest"],
            )
        if current.state is OperatorJobState.STOPPING and handoff is None:
            current = self._finish_pre_handoff_cancellation(current)
            current = self._complete_terminal_cleanup(current)
            return self._view(current)
        if current.state is OperatorJobState.CANCELLED and handoff is None:
            current = self._complete_terminal_cleanup(current)
            return self._view(current)
        if current.state is OperatorJobState.FAILED and handoff is None:
            return self._view(current)
        if handoff is None:
            if current.state is not OperatorJobState.STARTING:
                raise RealmIntegrityError(
                    "Study launch reached handoff from an unsupported job state."
                )
            try:
                handoff = self._runtime.ledger.handoff_study_launch_to_run(
                    operation_id=_phase(current.job_id, "handoff-run-create"),
                    actor_principal_id=self.principal_id,
                    job_id=current.job_id,
                    expected_job_revision=current.revision,
                ).handoff
            except RealmConflict:
                # A concurrent Stop can advance STARTING to STOPPING between
                # this read and the atomic handoff. Re-read the winning durable
                # side and finish it instead of stranding lifecycle debt.
                current = self._runtime.operator_jobs.read(job_id=current.job_id)
                handoff = self._read_handoff_optional(current.job_id)
                if current.state is OperatorJobState.STOPPING and handoff is None:
                    current = self._finish_pre_handoff_cancellation(current)
                    current = self._complete_terminal_cleanup(current)
                    return self._view(current)
                if current.state in {
                    OperatorJobState.CANCELLED,
                    OperatorJobState.FAILED,
                } and handoff is None:
                    if current.cleanup_state.value == "pending":
                        current = self._complete_terminal_cleanup(current)
                    return self._view(current)
                if handoff is None:
                    raise

        snapshot = self._runtime.ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=handoff.run_id,
        )
        if snapshot.run.state != "running":
            return self._view(self._runtime.operator_jobs.read(job_id=launch_id))
        if (
            current.state is OperatorJobState.SUCCEEDED
            and snapshot.run.controller_generation > 1
            and snapshot.controller_lease.state.value == "active"
            and snapshot.controller_lease.expires_at > time.time()
        ):
            # The launch lifecycle is already complete and a live controller
            # term owns execution.  Status reads/replayed launch requests must
            # not steal it; expiry-aware startup reconciliation will take over
            # only when that term actually becomes recoverable.
            return self._view(current)
        try:
            self._runtime.run_execution.execute(
                run_id=handoff.run_id,
                method_environment=method_environment,
            )
        except RunExecutionDeferred as error:
            raise StudyLaunchDispatchDeferred(str(error)) from error
        return self._view(self._runtime.operator_jobs.read(job_id=launch_id))

    def request_cancel(
        self,
        *,
        operation_id: str,
        launch_id: str,
        reason_code: str = "user_cancelled",
    ) -> StudyLaunchView:
        """Route cancellation to exactly one side of the durable handoff."""

        handoff = None
        for attempt in range(8):
            current = self._runtime.operator_jobs.read(job_id=launch_id)
            if current.plan.job_kind != STUDY_LAUNCH_JOB_KIND:
                raise RealmConflict("Operator Job is not a study launch.")
            handoff = self._read_handoff_optional(launch_id)
            if handoff is not None:
                break
            if current.state is OperatorJobState.STOPPING:
                if current.stop is None or current.stop.reason_code != reason_code:
                    raise RealmConflict(
                        "Study launch already has a different stop request."
                    )
                current = self._finish_pre_handoff_cancellation(current)
                current = self._complete_terminal_cleanup(current)
                return self._view(current)
            if current.state is OperatorJobState.CANCELLED:
                if current.stop is not None and current.stop.reason_code != reason_code:
                    raise RealmConflict(
                        "Study launch already has a different stop request."
                    )
                if current.cleanup_state.value == "pending":
                    current = self._complete_terminal_cleanup(current)
                return self._view(current)
            if current.state is OperatorJobState.FAILED:
                return self._view(current)
            try:
                stopped = self._runtime.ledger.request_operator_job_stop(
                    operation_id=operation_id,
                    actor_principal_id=self.principal_id,
                    job_id=launch_id,
                    expected_revision=current.revision,
                    reason_code=reason_code,
                )
            except RealmConflict:
                # QUEUED -> STARTING and STARTING -> handoff can both win after
                # our read. Re-read the exact durable head until either the
                # stop or the run side becomes authoritative.
                if attempt == 7:
                    raise
                continue
            else:
                if stopped.state is OperatorJobState.STOPPING:
                    stopped = self._finish_pre_handoff_cancellation(stopped)
                if stopped.state is OperatorJobState.CANCELLED:
                    stopped = self._complete_terminal_cleanup(stopped)
                return self._view(stopped)
        if handoff is None:  # pragma: no cover - loop exits only on a handoff
            raise RealmConflict("Study launch cancellation did not converge.")
        self._runtime.ledger.request_run_cancellation(
            operation_id=operation_id,
            actor_principal_id=self.principal_id,
            run_id=handoff.run_id,
            reason_code=reason_code,
        )
        return self._view(self._runtime.operator_jobs.read(job_id=launch_id))

    def request_run_cancel(
        self,
        *,
        operation_id: str,
        run_id: str,
        reason_code: str = "user_cancelled",
    ) -> StudyLaunchView:
        """Request cancellation from the canonical Run side of a handoff."""

        request = self._runtime.ledger.request_run_cancellation(
            operation_id=operation_id,
            actor_principal_id=self.principal_id,
            run_id=run_id,
            reason_code=reason_code,
        )
        return self.read(launch_id=request.job_id)

    def read(self, *, launch_id: str) -> StudyLaunchView:
        record = self._runtime.operator_jobs.read(job_id=launch_id)
        if record.plan.job_kind != STUDY_LAUNCH_JOB_KIND:
            raise RealmConflict("Operator Job is not a study launch.")
        return self._view(record)

    def read_for_run(self, *, run_id: str) -> StudyLaunchView:
        handoff = self._runtime.ledger.read_study_launch_handoff_for_run(
            actor_principal_id=self.principal_id,
            run_id=run_id,
        )
        return self.read(launch_id=handoff.job_id)

    def list(self, *, limit: int = 200) -> tuple[StudyLaunchView, ...]:
        records = self._runtime.ledger.list_operator_jobs_for_actor(
            actor_principal_id=self.principal_id,
            job_kind=STUDY_LAUNCH_JOB_KIND,
            limit=limit,
        )
        return tuple(self._view(record) for record in records)

    def list_reconcilable(
        self, *, page_size: int = 200
    ) -> tuple[StudyLaunchView, ...]:
        """Enumerate every launch with pre-handoff or cleanup lifecycle debt."""

        records: dict[str, OperatorJobRecord] = {}

        def scan_jobs(**filters: Any) -> None:
            cursor = None
            while True:
                page = self._runtime.ledger.list_operator_jobs_for_actor_page(
                    actor_principal_id=self.principal_id,
                    job_kind=STUDY_LAUNCH_JOB_KIND,
                    cursor=cursor,
                    limit=page_size,
                    **filters,
                )
                records.update((record.job_id, record) for record in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    return

        scan_jobs(states=tuple(state for state in OperatorJobState if not state.terminal))
        scan_jobs(cleanup_states=(OperatorJobCleanupState.PENDING,))

        views: dict[str, StudyLaunchView] = {
            job_id: self._view(record) for job_id, record in records.items()
        }
        return tuple(
            sorted(
                views.values(),
                key=lambda view: (-view.job.updated_at, view.launch_id),
            )
        )

    def _ensure_approved(self, current: OperatorJobRecord) -> OperatorJobRecord:
        if current.state is OperatorJobState.PLANNED:
            current = self._runtime.ledger.request_operator_job_approval(
                operation_id=_phase(current.job_id, "request-approval"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
            )
        if current.state is OperatorJobState.AWAITING_APPROVAL:
            current = self._runtime.ledger.approve_operator_job(
                operation_id=_phase(current.job_id, "approve"),
                actor_principal_id=self.principal_id,
                job_id=current.job_id,
                expected_revision=current.revision,
                expected_plan_digest=current.plan_digest,
                approval_scope_digest=request_digest(
                    {
                        "action": STUDY_LAUNCH_JOB_KIND,
                        "actor_principal_id": self.principal_id,
                        "job_id": current.job_id,
                        "plan_digest": current.plan_digest,
                        "schema": "optpilot.operator-job-approval-scope.v1",
                    }
                ),
            )
        return current

    def _finish_pre_handoff_cancellation(
        self, record: OperatorJobRecord
    ) -> OperatorJobRecord:
        reason = record.stop.reason_code if record.stop is not None else "user_cancelled"
        proof = request_digest(
            {
                "job_id": record.job_id,
                "launch_token": (
                    None if record.launch_intent is None else record.launch_intent.launch_token
                ),
                "reason_code": reason,
                "schema": "optpilot.study-launch-pre-handoff-cancel.v1",
            }
        )
        result = OperatorJobResult(
            result_kind=STUDY_LAUNCH_JOB_KIND,
            status="cancelled",
            metrics={},
            constraint_results={},
            event_summary={},
            declared_outputs=(),
            logs=(),
            details={"reason_code": reason, "schema": STUDY_LAUNCH_RESULT_SCHEMA},
        )
        return self._runtime.operator_jobs.finish_control_plane_job(
            job_id=record.job_id,
            result=result,
            status=OperatorJobTerminalStatus.CANCELLED,
            code=reason,
            terminal_proof_digest=proof,
        )

    def _complete_terminal_cleanup(
        self, record: OperatorJobRecord
    ) -> OperatorJobRecord:
        proof = (
            _never_started_proof(record)
            if record.launch_intent is None
            else (
                None
                if record.outcome is None
                else record.outcome.outcome.terminal_proof_digest
            )
        )
        if proof is None:
            raise RealmIntegrityError("Terminal study launch lacks cleanup authority.")
        return self._runtime.operator_jobs.complete_control_plane_cleanup(
            job_id=record.job_id,
            provider_evidence_digest=proof,
        )

    def _read_handoff_optional(self, launch_id: str) -> Any | None:
        try:
            return self._runtime.ledger.read_study_launch_handoff(
                actor_principal_id=self.principal_id,
                job_id=launch_id,
            )
        except RealmNotFound:
            return None

    def _view(self, record: OperatorJobRecord) -> StudyLaunchView:
        handoff = self._read_handoff_optional(record.job_id)
        summary = None
        cancellation_request = None
        if handoff is not None:
            try:
                summary = self._runtime.run_reader.summary(
                    run_id=handoff.run_id
                ).to_dict()
            except RealmNotFound:
                raise RealmIntegrityError(
                    "Study launch handoff names a missing canonical run."
                ) from None
            cancellation_request = self._runtime.ledger.read_run_cancellation_request(
                actor_principal_id=self.principal_id,
                run_id=handoff.run_id,
            )
        return StudyLaunchView(record, handoff, summary, cancellation_request)


def _job_operation_id(operation_id: str) -> str:
    return "study-launch/" + request_digest(
        {"operation_id": operation_id, "schema": "optpilot.study-launch-job.v1"}
    )


def study_launch_id_for_operation(operation_id: str) -> str:
    """Return the durable launch id for one idempotent launch operation.

    Presentation clients use this pure derivation to recover after an
    uncertain response without reading or recapturing mutable authored input.
    The id conveys no authority; the Realm service still validates every read.
    """

    return operator_job_id(_job_operation_id(operation_id))


def _phase(job_id: str, phase: str) -> str:
    return f"study-launch/{job_id}/{phase}"


def _job_owner_id(job_id: str) -> str:
    return "study-launch-owner-" + request_digest(
        {"job_id": job_id, "schema": "optpilot.study-launch-owner.v1"}
    )[:32]


def _definition_selection(definition: StudyDefinitionReceipt) -> SelectionRef:
    return SelectionRef.from_study_definition(definition)


def _launch_plan(
    *,
    job_id: str,
    definition: StudyDefinitionReceipt,
    identities: Mapping[str, str],
    derivation: OwnerDerivationManifest,
    display_name: str | None,
    execution_profile: RunExecutionProfile,
    method_environment_binding: Mapping[str, Any],
) -> OperatorJobLaunchPlan:
    manifest = definition.manifest
    run_definition = manifest.run_definition
    environment_id = run_definition.evaluation_closure.environment_revision.environment_id
    method_id = run_definition.method_revision.method_id
    facts = {
        "display": {
            "environment_id": environment_id,
            "method_id": method_id,
            "study_name": display_name,
        },
        "execution_profile": execution_profile.to_dict(),
        "method_environment_binding": dict(method_environment_binding),
        "study_definition": {
            "manifest_digest": manifest.digest,
            "owner_id": definition.owner.owner_id,
            "owner_revision": definition.owner.revision,
            "run_definition_digest": manifest.run_definition_digest,
        },
        "run": {
            "bootstrap_controller_holder_id": identities["controller_holder_id"],
            "owner_id": identities["run_owner_id"],
            "run_id": identities["run_id"],
        },
        "schema": STUDY_LAUNCH_INPUT_SCHEMA,
    }
    selection = _definition_selection(definition)
    return OperatorJobLaunchPlan(
        job_kind=STUDY_LAUNCH_JOB_KIND,
        target=OperatorJobTarget(kind=STUDY_DEFINITION_TARGET_KIND, selection=selection),
        input_facts=facts,
        input_facts_digest=hashlib.sha256(canonical_json_bytes(facts)).hexdigest(),
        owner_derivation_manifest_digest=derivation.digest,
        source_fingerprints=tuple(
            sorted(
                {
                    manifest.digest,
                    manifest.run_definition_digest,
                    derivation.sources[0].owner_manifest_digest,
                    selection.selection_digest,
                }
            )
        ),
        runtime_fingerprint=manifest.run_definition_digest,
        entrypoint_profile="retained-batch-study",
        projection_contract_digest=derivation.digest,
        backend_kind=STUDY_LAUNCH_BACKEND_KIND,
        backend_realm=STUDY_LAUNCH_BACKEND_REALM,
        resource_claims={"cpu_millis": 1, "memory_bytes": 1},
        timeout_seconds=STUDY_LAUNCH_STARTUP_TIMEOUT_SECONDS,
        network_policy="denied",
        network_enforcement="enforced",
        requested_secret_names=(),
        grants_digest=request_digest(
            {
                "network_enforcement": "enforced",
                "network_policy": "denied",
                "requested_secret_names": [],
                "schema": "optpilot.operator-job-grants.v1",
            }
        ),
        evidence_sink_kind="canonical-run",
        evidence_sink_id=identities["run_id"],
        evidence_sink_digest=request_digest(
            {
                "job_id": job_id,
                "run_id": identities["run_id"],
                "schema": "optpilot.study-launch-evidence-sink.v1",
            }
        ),
        cancellation_guarantee="handoff-routed",
        priority_class="interactive",
    )


def _validate_existing_plan(
    record: OperatorJobRecord,
    definition: StudyDefinitionReceipt,
    identities: Mapping[str, str],
    execution_profile: RunExecutionProfile,
    method_environment_binding: Mapping[str, Any],
) -> None:
    if record.plan.job_kind != STUDY_LAUNCH_JOB_KIND:
        raise RealmConflict("Study launch operation names another Operator Job.")
    facts = _plan_context(record)["facts"]
    expected_definition = {
        "manifest_digest": definition.manifest.digest,
        "owner_id": definition.owner.owner_id,
        "owner_revision": definition.owner.revision,
        "run_definition_digest": definition.manifest.run_definition_digest,
    }
    expected_run = {
        "bootstrap_controller_holder_id": identities["controller_holder_id"],
        "owner_id": identities["run_owner_id"],
        "run_id": identities["run_id"],
    }
    if (
        facts["study_definition"] != expected_definition
        or facts["run"] != expected_run
        or facts["execution_profile"] != execution_profile.to_dict()
        or thaw_json(facts["method_environment_binding"])
        != dict(method_environment_binding)
    ):
        raise RealmConflict(
            "Study launch operation names another exact definition, execution "
            "profile, or Method environment binding."
        )


def _plan_context(record: OperatorJobRecord) -> dict[str, Any]:
    if (
        record.plan.job_kind != STUDY_LAUNCH_JOB_KIND
        or record.plan.target.kind != STUDY_DEFINITION_TARGET_KIND
    ):
        raise RealmConflict("Operator Job is not a study launch.")
    facts = dict(record.plan.input_facts)
    if set(facts) != {
        "display",
        "execution_profile",
        "method_environment_binding",
        "run",
        "schema",
        "study_definition",
    } or facts.get("schema") != STUDY_LAUNCH_INPUT_SCHEMA:
        raise RealmIntegrityError("Study launch retained input facts are unsupported.")
    try:
        execution_profile = RunExecutionProfile.from_dict(
            facts["execution_profile"]
        )
    except (TypeError, ValueError, KeyError):
        raise RealmIntegrityError(
            "Study launch retained execution profile is malformed."
        ) from None
    digest = request_digest(
        {
            "job_id": record.job_id,
            "plan_digest": record.plan_digest,
            "schema": "optpilot.study-launch-startup-identity.v1",
        }
    )
    return {
        "binding_id": f"study-launch-binding-{digest[:32]}",
        "evidence_fingerprint": request_digest(
            {
                "identity_digest": digest,
                "plan_digest": record.plan_digest,
                "schema": "optpilot.study-launch-startup-evidence.v1",
            }
        ),
        "facts": facts,
        "execution_profile": execution_profile,
        "launch_request_digest": request_digest(
            {
                "input_facts_digest": record.plan.input_facts_digest,
                "job_id": record.job_id,
                "plan_digest": record.plan_digest,
                "schema": "optpilot.study-launch-request.v1",
            }
        ),
        "launch_token": f"study-launch-token-{digest[:32]}",
    }


def _never_started_proof(record: OperatorJobRecord) -> str:
    return control_plane_never_started_proof(record)


__all__ = [
    "RealmStudyLaunchService",
    "STUDY_LAUNCH_JOB_KIND",
    "StudyLaunchDispatchDeferred",
    "StudyLaunchView",
    "study_launch_id_for_operation",
]
