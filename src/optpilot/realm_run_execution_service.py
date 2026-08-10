"""One durable dispatcher for every supported canonical local run.

Run origin determines only how the immutable execution plan is resolved:
study runs are linked to their approved Operator Job, while exact-plan child
runs carry typed lineage in their retained definition.  Controller claiming,
expiry recovery, driver construction, and cancellation-aware driving are
shared here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .method_launch_environment import (
    MethodLaunchEnvironment,
    MethodLaunchEnvironmentDescriptor,
    method_environment_names,
)
from .realm._validation import required_text
from .realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .realm.operator_job_records import OperatorJobRecord, OperatorJobState
from .realm.refs import request_digest
from .realm.run_child import exact_plan_child_lineage_from_snapshot
from .realm.run_projection import RunSummaryProjection
from .realm.run_snapshot import RunLedgerSnapshot
from .realm_retained_batch_run_driver import (
    RealmRetainedBatchRunDriver,
    RunControllerTakeoverExpectation,
)
from .retained_driver_factory import candidate_normalizer_for_run_definition
from .run_authority import RetainedRunAuthority
from .run_execution_profile import RunExecutionProfile
from .study_realm_compiler import CANDIDATE_NORMALIZER_VERSION


RUN_EXECUTION_MODE_RETAINED_BATCH = "retained-batch"
RUN_EXECUTION_MODE_EXACT_PLAN = "exact-plan-methodless"


class RunExecutionDeferred(RealmConflict):
    """Another live fenced controller currently owns the canonical run."""


class _RuntimeGraph(Protocol):
    actor_principal_id: str
    ledger: Any
    operator_jobs: Any
    run_reader: Any
    closed: bool


@dataclass(frozen=True)
class RunExecutionDescriptor:
    """Path-free immutable execution facts resolved from canonical state."""

    run_id: str
    mode: str
    profile: RunExecutionProfile
    study_launch_id: str | None = None
    method_environment_names: tuple[str, ...] = ()
    method_environment_binding_revision: str | None = None
    method_environment_revision_ids: tuple[str, ...] = ()
    method_environment_source: str | None = None
    method_environment_recoverability: str = "none"

    def __post_init__(self) -> None:
        required_text(self.run_id, "run execution run id", max_bytes=512)
        if self.mode not in {
            RUN_EXECUTION_MODE_RETAINED_BATCH,
            RUN_EXECUTION_MODE_EXACT_PLAN,
        }:
            raise ValueError("Run execution mode is unsupported.")
        if not isinstance(self.profile, RunExecutionProfile):
            raise TypeError("profile must be a RunExecutionProfile.")
        if self.mode == RUN_EXECUTION_MODE_RETAINED_BATCH:
            required_text(
                self.study_launch_id,
                "run execution study launch id",
                max_bytes=512,
            )
        elif self.study_launch_id is not None:
            raise ValueError("Exact-plan execution cannot name a study launch.")
        names = tuple(self.method_environment_names)
        revisions = tuple(self.method_environment_revision_ids)
        if len(names) != len(set(names)):
            raise ValueError("Method environment names must be unique.")
        if self.mode == RUN_EXECUTION_MODE_EXACT_PLAN and (
            names
            or revisions
            or self.method_environment_binding_revision is not None
            or self.method_environment_source is not None
            or self.method_environment_recoverability != "none"
        ):
            raise ValueError(
                "Exact-plan methodless execution cannot require Method environment."
            )
        if self.mode == RUN_EXECUTION_MODE_RETAINED_BATCH:
            required_text(
                self.method_environment_binding_revision,
                "Method environment binding revision",
                max_bytes=512,
            )
            if len(revisions) != len(names):
                raise ValueError(
                    "Method environment revision ids must align with names."
                )
            if names:
                if self.method_environment_source not in {
                    "process-environment",
                    "studio-settings",
                }:
                    raise ValueError("Method environment source is unsupported.")
                if self.method_environment_recoverability not in {
                    "process-lifetime",
                    "settings-revision",
                }:
                    raise ValueError(
                        "Method environment recoverability is unsupported."
                    )
            elif (
                revisions
                or self.method_environment_source is not None
                or self.method_environment_recoverability != "none"
            ):
                raise ValueError(
                    "A Method without local values has an invalid binding."
                )
        object.__setattr__(self, "method_environment_names", names)
        object.__setattr__(self, "method_environment_revision_ids", revisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "profile": self.profile.to_dict(),
            "run_id": self.run_id,
            "study_launch_id": self.study_launch_id,
            "method_environment_names": list(self.method_environment_names),
            "method_environment_binding_revision": (
                self.method_environment_binding_revision
            ),
            "method_environment_revision_ids": list(
                self.method_environment_revision_ids
            ),
            "method_environment_source": self.method_environment_source,
            "method_environment_recoverability": (
                self.method_environment_recoverability
            ),
        }


@dataclass(frozen=True)
class _MethodlessReconciliation:
    run_id: str
    controller_generation: int
    run_definition_digest: str
    worker_disposition: str = "never_started"
    resources_reconciled: bool = True


class _ExactPlanMethodlessRuntimeProvider:
    @staticmethod
    def realize(_snapshot: RunLedgerSnapshot, **_kwargs: Any) -> Any:
        raise RealmIntegrityError(
            "An exact-plan child must never realize its retained method."
        )

    @staticmethod
    def reconcile_inactive(
        snapshot: RunLedgerSnapshot, **_kwargs: Any
    ) -> _MethodlessReconciliation:
        exact_plan_child_lineage_from_snapshot(snapshot)
        if (
            snapshot.control.current_submission.state not in {"draining", "terminal"}
            or snapshot.method_exchange_preparations
            or snapshot.method_exchange_completions
        ):
            raise RealmIntegrityError(
                "Exact-plan child has unexpected method-active state."
            )
        return _MethodlessReconciliation(
            run_id=snapshot.run.run_id,
            controller_generation=snapshot.run.controller_generation,
            run_definition_digest=snapshot.definition.digest,
        )


def new_run_execution_dispatch_operation_id() -> str:
    """Return an opaque id for one retryable local dispatch claim."""

    return f"run-execution-dispatch/{uuid.uuid4().hex}"


class RealmRunExecutionService:
    """Resolve, claim, recover, and drive supported canonical runs."""

    def __init__(self, runtime: _RuntimeGraph) -> None:
        for name in ("actor_principal_id", "ledger", "operator_jobs", "run_reader"):
            if not hasattr(runtime, name):
                raise TypeError("runtime does not provide the run execution graph.")
        self._runtime = runtime

    @property
    def principal_id(self) -> str:
        return self._runtime.actor_principal_id

    def describe(self, *, run_id: str) -> RunExecutionDescriptor:
        snapshot = self._snapshot(run_id)
        return self._descriptor(snapshot)

    def list_reconcilable(
        self, *, page_size: int = 100
    ) -> tuple[RunExecutionDescriptor, ...]:
        """Collect every authorized running run with a retained execution plan."""

        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise TypeError("page_size must be a positive integer.")
        if page_size <= 0 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100.")
        descriptors: dict[str, RunExecutionDescriptor] = {}
        page_token = None
        seen_tokens: set[str] = set()
        while True:
            page = self._runtime.run_reader.list_runs(
                page_token=page_token,
                limit=page_size,
            )
            for item in page.items:
                if item.state != "running" or item.retention_state != "active":
                    continue
                try:
                    snapshot = self._snapshot(item.run_id)
                    descriptor = self._descriptor(snapshot)
                except (RealmConflict, RealmIntegrityError, RealmNotFound):
                    # An unsupported or malformed run remains visible in the
                    # canonical catalog for diagnosis and cannot hide a valid
                    # independently recoverable neighbor.
                    continue
                descriptors[descriptor.run_id] = descriptor
            page_token = page.next_page_token
            if page_token is None:
                break
            if page_token in seen_tokens:
                raise RealmIntegrityError(
                    "Run catalog repeated a run-execution recovery cursor."
                )
            seen_tokens.add(page_token)
        return tuple(descriptors[run_id] for run_id in sorted(descriptors))

    def execute(
        self,
        *,
        run_id: str,
        dispatch_operation_id: str | None = None,
        method_environment: (
            Mapping[str, str]
            | MethodLaunchEnvironment
            | MethodLaunchEnvironmentDescriptor
            | None
        ) = None,
    ) -> RunSummaryProjection:
        """Drive one canonical run with launch-scoped Method environment.

        ``method_environment`` is an operational source.  Only names declared
        by the retained Method are selected, and values are passed directly to
        that Method worker rather than added to Run closure or evidence.
        """

        snapshot = self._snapshot(run_id)
        descriptor = self._descriptor(snapshot)
        if snapshot.run.state != "running":
            return RunSummaryProjection.from_snapshot(snapshot)
        method_binding: (
            MethodLaunchEnvironment | MethodLaunchEnvironmentDescriptor | None
        ) = None
        if descriptor.mode == RUN_EXECUTION_MODE_RETAINED_BATCH:
            expected_binding = MethodLaunchEnvironmentDescriptor.for_definition(
                snapshot.definition,
                binding_revision=str(
                    descriptor.method_environment_binding_revision
                ),
            )
            if isinstance(
                method_environment,
                (MethodLaunchEnvironment, MethodLaunchEnvironmentDescriptor),
            ):
                supplied_descriptor = (
                    method_environment.descriptor
                    if isinstance(method_environment, MethodLaunchEnvironment)
                    else method_environment
                )
                if supplied_descriptor != expected_binding:
                    if isinstance(method_environment, MethodLaunchEnvironment):
                        method_environment.discard_values()
                    raise RealmConflict(
                        "Method environment binding differs from this Run."
                    )
                method_binding = method_environment
            elif method_environment is None:
                method_binding = expected_binding
            else:
                method_binding = MethodLaunchEnvironment.for_definition(
                    snapshot.definition,
                    method_environment,
                    binding_revision=expected_binding.binding_revision,
                )
            # Do not retain a caller-owned mapping in this long-running frame.
            method_environment = None
        elif method_environment:
            raise ValueError(
                "Exact-plan methodless execution cannot receive Method environment."
            )
        selected_dispatch = dispatch_operation_id or (
            new_run_execution_dispatch_operation_id()
        )
        required_text(
            selected_dispatch,
            "run execution dispatch operation id",
            max_bytes=512,
        )
        driver = self._claim_driver(
            descriptor=descriptor,
            snapshot=snapshot,
            dispatch_operation_id=selected_dispatch,
            method_environment=method_binding,
        )
        claimed = driver.authority.refresh_controller()
        if descriptor.study_launch_id is not None:
            self._confirm_study_controller(descriptor, claimed)
        result = driver.run()
        if not isinstance(result, RunSummaryProjection):
            raise TypeError("Canonical run driver returned an invalid summary.")
        if result.run_id != descriptor.run_id:
            raise RealmIntegrityError("Canonical run driver returned another run.")
        return result

    def _snapshot(self, run_id: str) -> RunLedgerSnapshot:
        run_id = required_text(run_id, "run execution run id", max_bytes=512)
        return self._runtime.ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=run_id,
        )

    def _descriptor(self, snapshot: RunLedgerSnapshot) -> RunExecutionDescriptor:
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        metadata = snapshot.definition.metadata
        if isinstance(metadata, Mapping) and "child_run" in metadata:
            lineage = exact_plan_child_lineage_from_snapshot(snapshot)
            return RunExecutionDescriptor(
                run_id=snapshot.run.run_id,
                mode=RUN_EXECUTION_MODE_EXACT_PLAN,
                profile=lineage.execution_profile,
            )

        handoff = self._runtime.ledger.read_study_launch_handoff_for_run(
            actor_principal_id=self.principal_id,
            run_id=snapshot.run.run_id,
        )
        record = self._runtime.operator_jobs.read(job_id=handoff.job_id)
        # Local import avoids making the lifecycle service and composition root
        # depend on this dispatcher during module initialization.
        from .study_launch_service import (
            STUDY_LAUNCH_JOB_KIND,
            _plan_context,
        )

        context = _plan_context(record)
        facts = context["facts"]
        environment_binding = facts["method_environment_binding"]
        environment_requirements = environment_binding["requirements"]
        environment_sources = {
            item["source"] for item in environment_requirements
        }
        if (
            record.plan.job_kind != STUDY_LAUNCH_JOB_KIND
            or handoff.plan_digest != record.plan_digest
            or handoff.run_id != snapshot.run.run_id
            or facts["run"]["run_id"] != snapshot.run.run_id
            or handoff.run_definition_digest != snapshot.definition.digest
        ):
            raise RealmIntegrityError(
                "Study launch handoff differs from its canonical run plan."
            )
        return RunExecutionDescriptor(
            run_id=snapshot.run.run_id,
            mode=RUN_EXECUTION_MODE_RETAINED_BATCH,
            profile=context["execution_profile"],
            study_launch_id=record.job_id,
            method_environment_names=method_environment_names(snapshot.definition),
            method_environment_binding_revision=environment_binding[
                "binding_revision"
            ],
            method_environment_revision_ids=tuple(
                item["revision_id"] for item in environment_requirements
            ),
            method_environment_source=(
                next(iter(environment_sources)) if environment_sources else None
            ),
            method_environment_recoverability=environment_binding[
                "recoverability"
            ],
        )

    def _claim_driver(
        self,
        *,
        descriptor: RunExecutionDescriptor,
        snapshot: RunLedgerSnapshot,
        dispatch_operation_id: str,
        method_environment: (
            MethodLaunchEnvironment
            | MethodLaunchEnvironmentDescriptor
            | None
        ) = None,
    ) -> RealmRetainedBatchRunDriver:
        digest = request_digest(
            {
                "dispatch_operation_id": dispatch_operation_id,
                "run_id": descriptor.run_id,
                "schema": "optpilot.run-execution-dispatch.v1",
            }
        )
        holder_id = f"run-execution-controller-{digest}"
        normalizer = candidate_normalizer_for_run_definition(snapshot.definition)
        provider = (
            _ExactPlanMethodlessRuntimeProvider()
            if descriptor.mode == RUN_EXECUTION_MODE_EXACT_PLAN
            else None
        )
        options = {
            "attempt_ttl_seconds": descriptor.profile.attempt_ttl_seconds,
            "heartbeat_interval_seconds": (
                descriptor.profile.heartbeat_interval_seconds
            ),
            "method_request_timeout": (
                descriptor.profile.method_request_timeout_seconds
            ),
            "method_start_timeout": descriptor.profile.method_start_timeout_seconds,
        }
        if provider is not None:
            options["method_runtime_provider"] = provider
        else:
            options["method_environment"] = method_environment

        lease_live = (
            snapshot.controller_lease.state.value == "active"
            and snapshot.controller_lease.expires_at > time.time()
        )
        if snapshot.run.controller_generation > 1 and lease_live:
            if snapshot.run.controller_holder_id != holder_id:
                raise RunExecutionDeferred(
                    "A live fenced controller already owns the canonical run."
                )
            authority = RetainedRunAuthority.hydrate(
                ledger=self._runtime.ledger,
                actor_principal_id=self.principal_id,
                run_id=descriptor.run_id,
                candidate_normalizer=normalizer,
                normalizer_version=CANDIDATE_NORMALIZER_VERSION,
            )
            return RealmRetainedBatchRunDriver(
                self._runtime,
                authority,
                controller_ttl_seconds=descriptor.profile.controller_ttl_seconds,
                **options,
            )

        expectation = RunControllerTakeoverExpectation.from_snapshot(snapshot)
        try:
            return RealmRetainedBatchRunDriver.take_over(
                self._runtime,
                expected_controller=expectation,
                takeover_operation_id=(
                    f"run-execution/{digest}/generation-"
                    f"{expectation.controller_generation + 1}/takeover"
                ),
                new_controller_holder_id=holder_id,
                candidate_normalizer=normalizer,
                normalizer_version=CANDIDATE_NORMALIZER_VERSION,
                controller_ttl_seconds=(
                    descriptor.profile.controller_ttl_seconds
                ),
                require_previous_controller_expired=(
                    expectation.controller_generation > 1
                ),
                **options,
            )
        except RealmConflict as error:
            raise RunExecutionDeferred(
                "Another reconciler claimed the canonical run."
            ) from error

    def _confirm_study_controller(
        self,
        descriptor: RunExecutionDescriptor,
        claimed: RunLedgerSnapshot,
    ) -> None:
        assert descriptor.study_launch_id is not None
        current = self._runtime.operator_jobs.read(
            job_id=descriptor.study_launch_id
        )
        if current.state is OperatorJobState.STARTING:
            try:
                current = self._runtime.operator_jobs.mark_control_plane_running(
                    job_id=current.job_id
                )
            except RealmExpired:
                # The launching control plane's startup window expired before
                # the run was ever confirmed - the launcher process is gone
                # (for example a CLI launch that died before execution).
                # Replaying the intent forever can never confirm it. Finish
                # the launch terminally, fenced by the controller term this
                # dispatch just claimed, and request cancellation of the
                # canonical run so the claimed driver drains it to terminal.
                self._finish_expired_startup(current, claimed)
                return
        if current.state is OperatorJobState.RUNNING:
            self._runtime.operator_jobs.confirm_study_launch_controller(
                job_id=current.job_id,
                controller_lease_id=claimed.run.controller_lease_id,
                controller_holder_id=claimed.run.controller_holder_id,
                controller_fencing_token=claimed.run.controller_fencing_token,
                controller_generation=claimed.run.controller_generation,
            )
        elif current.state is not OperatorJobState.SUCCEEDED:
            raise RealmIntegrityError(
                "Study launch cannot confirm its canonical run controller."
            )

    def _finish_expired_startup(
        self,
        record: OperatorJobRecord,
        claimed: RunLedgerSnapshot,
    ) -> None:
        """Cancel one orphaned run whose launch startup window expired.

        A handed-off study launch may only finish through typed controller
        confirmation, which an expired startup can never provide. The launch
        record therefore stays in its durable startup state; requesting run
        cancellation lets the controller term this dispatch just claimed
        drain the canonical run to ``cancelled``, after which every launch
        view derives its terminal status from the run summary and no
        recovery path re-dispatches it.
        """

        del record
        try:
            self._runtime.ledger.request_run_cancellation(
                operation_id=(
                    "run-execution/startup-expired/"
                    f"{claimed.run.run_id}"
                ),
                actor_principal_id=self.principal_id,
                run_id=claimed.run.run_id,
                reason_code="user_cancelled",
            )
        except RealmConflict:
            # A cancellation request already exists; the claimed driver
            # consumes it either way.
            pass


__all__ = [
    "RUN_EXECUTION_MODE_EXACT_PLAN",
    "RUN_EXECUTION_MODE_RETAINED_BATCH",
    "RealmRunExecutionService",
    "RunExecutionDeferred",
    "RunExecutionDescriptor",
    "new_run_execution_dispatch_operation_id",
]
