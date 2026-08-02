"""Internal composition root for one OS-local Realm runtime.

This module centralizes construction only.  It does not choose a global Realm
location, derive an actor from request data, or add another read model.  A
trusted application supplies an absolute private root and its already-observed
local principal id; the runtime binds the existing Realm services to that one
authority.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Sequence

from optpilot.retained_study_service import RetainedStudyService

from ._validation import required_text
from .attempt_finalizer import RealmAttemptFinalizer
from .catalog_service import RealmCatalogPublicationService
from .configured_package_ingress_service import (
    ConfiguredPackageIngressService,
)
from .config import prepare_private_directory
from .content import LocalContentStore
from .editable_workspace_service import RealmEditableWorkspaceService
from .environment_preview_binding import RealmEnvironmentPreviewBinder
from .ephemeral_volume_service import RealmEphemeralVolumeService
from .inspection_service import RealmInspectionTargetService
from .interface_output_service import RealmInterfaceOutputSessionService
from .ledger import PrincipalRecord, RealmLedger
from .local_attempt_launcher import RealmLocalAttemptLauncher
from .local_capacity import conservative_local_host_capacity_limits
from .local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
)
from .local_process_attempt_provider import LocalProcessAttemptProvider
from .local_process_supervisor import LocalProcessSupervisor
from .operator_attempt_binding import RealmOperatorAttemptBinder
from .operator_capacity_records import OperatorCapacityPoolRecord
from .operator_job_service import RealmOperatorJobService
from .process_execution_binder import RealmProcessExecutionBinder
from .process_provider import (
    ProcessProviderIdentity,
    current_process_provider_identity,
)
from .projection_service import RealmProjectionService
from .refs import request_digest
from .run_reader import (
    LOCAL_REALM_PRINCIPAL_KIND,
    RealmRunReadService,
    register_current_local_principal,
)
from .run_child_service import RealmChildRunService
from .review_collection_service import RealmReviewCollectionService
from .run_views import RealmRunViewService
from .shortlist_service import RealmShortlistService
from .selection_service import RealmSelectionActionService
from .selection_content_service import RealmSelectionContentService
from .service import RealmContentService


LOCAL_REALM_CONTENT_STORE_ID = "local-primary-v1"
LOCAL_OPERATOR_CAPACITY_POOL = "local-host"

_AUTHORITY_DIRECTORY = "authority"
_CONTENT_DIRECTORY = "content"
_EDITABLE_WORKSPACE_DIRECTORY = "editable-workspaces"
_PROJECTION_DIRECTORY = "projections"
_VOLUME_DIRECTORY = "volumes"
_PROCESS_DIRECTORY = "processes"
_CONTAINER_WEB_DIRECTORY = "container-web"
_RETAINED_DEPENDENCY_CACHE_DIRECTORY = "retained-dependency-cache"


class LocalRealmRuntime:
    """Own the in-process service graph for one local Realm.

    Use :meth:`open` so failed construction can close the Realm ledger and CAS
    descriptors as one unit.  ``actor_principal_id`` is an application
    composition input: callers must pass the server-observed local identity,
    never an HTTP/request-provided principal.

    Closing releases this process's ledger and content-store descriptors.  It
    deliberately does not stop durable supervised workers; their lifecycle is
    reconciled through canonical attempt state and the process provider.
    """

    root: Path
    actor_principal_id: str
    principal: PrincipalRecord
    ledger: RealmLedger
    content_store: LocalContentStore
    content_service: RealmContentService
    projection_service: RealmProjectionService
    editable_workspaces: RealmEditableWorkspaceService
    volume_service: RealmEphemeralVolumeService
    process_provider: ProcessProviderIdentity
    process_supervisor: LocalProcessSupervisor
    attempt_launcher: RealmLocalAttemptLauncher
    execution_binder: RealmProcessExecutionBinder
    attempt_finalizer: RealmAttemptFinalizer
    attempt_provider: LocalProcessAttemptProvider
    retained_study_service: RetainedStudyService
    study_launches: Any
    run_execution: Any
    run_reader: RealmRunReadService
    run_views: RealmRunViewService
    child_runs: RealmChildRunService
    review_collections: RealmReviewCollectionService
    shortlists: RealmShortlistService
    catalog: RealmCatalogPublicationService
    configured_package_ingress: ConfiguredPackageIngressService
    selection_actions: RealmSelectionActionService
    selection_content: RealmSelectionContentService
    inspection_targets: RealmInspectionTargetService
    interface_outputs: RealmInterfaceOutputSessionService
    operator_attempt_binder: RealmOperatorAttemptBinder
    operator_capacity_pool: OperatorCapacityPoolRecord
    operator_jobs: RealmOperatorJobService
    environment_preview_binder: RealmEnvironmentPreviewBinder | None
    container_web_provider: LocalContainerWebProvider | None
    container_web_broker_authority: object | None

    def __init__(self) -> None:
        raise TypeError("Use LocalRealmRuntime.open().")

    @classmethod
    def open(
        cls,
        *,
        realm_root: Path,
        actor_principal_id: str | None = None,
        container_web_executable: str | None = None,
        trusted_container_gateway_images: Sequence[ContainerGatewayImageTrust] = (),
    ) -> "LocalRealmRuntime":
        """Open the exact local service graph rooted below ``realm_root``.

        Production callers omit ``actor_principal_id`` and bind the principal
        derived from the server-observed OS account.  The explicit value is a
        trusted application-composition and test seam; it must never be filled
        from request data.
        """

        if actor_principal_id is not None:
            actor_principal_id = required_text(
                actor_principal_id,
                "local Realm runtime actor principal id",
            )
        if container_web_executable is not None:
            container_web_executable = required_text(
                container_web_executable,
                "local container web executable",
            )
        trusted_container_gateway_images = tuple(trusted_container_gateway_images)
        if any(
            not isinstance(item, ContainerGatewayImageTrust)
            for item in trusted_container_gateway_images
        ):
            raise TypeError(
                "trusted_container_gateway_images must contain "
                "ContainerGatewayImageTrust values."
            )
        root = _prepare_absolute_root(realm_root)

        ledger: RealmLedger | None = None
        store: LocalContentStore | None = None
        try:
            authority_root = prepare_private_directory(root / _AUTHORITY_DIRECTORY)
            ledger = RealmLedger(authority_root / "realm.sqlite3")
            if actor_principal_id is None:
                principal = register_current_local_principal(ledger)
                actor_principal_id = principal.principal_id
            else:
                principal = _register_actor(
                    ledger=ledger,
                    actor_principal_id=actor_principal_id,
                )

            capacity_limits = conservative_local_host_capacity_limits()
            capacity_pool = ledger.ensure_operator_capacity_pool(
                operation_id=(
                    "local-runtime/bootstrap/operator-capacity/"
                    + request_digest(
                        {
                            "actor_principal_id": actor_principal_id,
                            "limits": dict(capacity_limits),
                            "pool_name": LOCAL_OPERATOR_CAPACITY_POOL,
                            "schema": "optpilot.local-capacity-bootstrap.v1",
                        }
                    )
                    + "/attempt-"
                    + uuid.uuid4().hex
                ),
                actor_principal_id=actor_principal_id,
                pool_name=LOCAL_OPERATOR_CAPACITY_POOL,
                limits=capacity_limits,
            )

            content_root = prepare_private_directory(root / _CONTENT_DIRECTORY)
            store = LocalContentStore(
                content_root,
                store_id=LOCAL_REALM_CONTENT_STORE_ID,
            )
            ledger.register_store(
                operation_id="local-runtime/bootstrap/store/local-primary-v1",
                store_id=store.store_id,
                backend_kind=store.BACKEND_KIND,
                root_marker=store.root_marker,
            )
            local_stores = {store.store_id: store}
            content_service = RealmContentService(
                ledger,
                local_stores=local_stores,
            )
            projection_service = RealmProjectionService(
                ledger,
                local_stores=local_stores,
                projection_root=prepare_private_directory(root / _PROJECTION_DIRECTORY),
            )
            editable_workspaces = RealmEditableWorkspaceService(
                ledger,
                content_service,
                projection_service,
                actor_principal_id=actor_principal_id,
                checkout_root=root / _EDITABLE_WORKSPACE_DIRECTORY,
            )
            editable_workspaces.reap_abandoned_workspace_creations(
                operation_id=(
                    "local-runtime/bootstrap/workspace-assembly-reap/"
                    + uuid.uuid4().hex
                ),
                limit=100,
            )
            volume_service = RealmEphemeralVolumeService(
                ledger,
                volume_root=prepare_private_directory(root / _VOLUME_DIRECTORY),
            )
            container_web_broker_authority: object | None = None
            container_web_provider: LocalContainerWebProvider | None = None
            environment_preview_binder: RealmEnvironmentPreviewBinder | None = None
            if container_web_executable is not None:
                container_web_broker_authority = object()
                container_web_provider = LocalContainerWebProvider(
                    executable=container_web_executable,
                    control_root=prepare_private_directory(
                        root / _CONTAINER_WEB_DIRECTORY
                    ),
                    broker_authority=container_web_broker_authority,
                    trusted_gateway_images=trusted_container_gateway_images,
                )
                environment_preview_binder = RealmEnvironmentPreviewBinder(
                    ledger,
                    projection_service,
                    volume_service,
                    container_web_provider,
                )

            process_provider = current_process_provider_identity()
            process_supervisor = LocalProcessSupervisor(
                prepare_private_directory(root / _PROCESS_DIRECTORY)
            )
            attempt_launcher = RealmLocalAttemptLauncher(process_supervisor)
            execution_binder = RealmProcessExecutionBinder(
                ledger,
                projection_service,
                volume_service,
                process_provider,
                launch_reservation_verifier=(
                    attempt_launcher.verify_launch_reservation
                ),
                terminal_proof_verifier=(
                    attempt_launcher._validate_supervisor_terminal_proof
                ),
            )
            attempt_finalizer = RealmAttemptFinalizer(
                ledger,
                content_service,
                actor_principal_id=actor_principal_id,
                store_id=store.store_id,
            )
            attempt_provider = LocalProcessAttemptProvider(
                ledger,
                execution_binder,
                attempt_launcher,
                attempt_finalizer,
                actor_principal_id=actor_principal_id,
            )
            retained_study_service = RetainedStudyService(
                ledger,
                content_service,
                projection_service,
                process_provider,
                dependency_cache_root=prepare_private_directory(
                    root / _RETAINED_DEPENDENCY_CACHE_DIRECTORY
                ),
            )
            catalog = RealmCatalogPublicationService(
                ledger,
                content_service,
                principal,
                local_stores,
            )
            configured_package_ingress = ConfiguredPackageIngressService(
                ledger,
                content_service,
                projection_service,
                catalog,
                actor_principal_id,
                store,
            )
            configured_package_ingress.reap_cleanup_debt(limit=100)
            selection_actions = RealmSelectionActionService(ledger, principal)
            selection_content = RealmSelectionContentService(
                ledger,
                principal,
                local_stores=local_stores,
            )
            run_reader = RealmRunReadService(ledger, principal)
            run_views = RealmRunViewService(
                ledger,
                principal,
                selection_content,
            )
            inspection_targets = RealmInspectionTargetService(ledger, principal)
            interface_outputs = RealmInterfaceOutputSessionService(
                ledger,
                content_service,
                actor_principal_id=actor_principal_id,
                store_id=store.store_id,
            )
            operator_attempt_binder = RealmOperatorAttemptBinder(
                ledger,
                projection_service,
                volume_service,
            )
            operator_jobs = RealmOperatorJobService(
                ledger,
                principal,
                inspection_targets,
                process_provider,
                operator_attempt_binder,
                attempt_launcher,
                attempt_finalizer,
                interface_output_service=interface_outputs,
                environment_preview_binder=environment_preview_binder,
                container_web_provider=container_web_provider,
                container_web_broker_authority=container_web_broker_authority,
            )
        except BaseException:
            if store is not None:
                store.close()
            if ledger is not None:
                ledger.close()
            raise

        runtime = object.__new__(cls)
        runtime.root = root
        runtime.actor_principal_id = actor_principal_id
        runtime.principal = principal
        runtime.ledger = ledger
        runtime.content_store = store
        runtime.content_service = content_service
        runtime.projection_service = projection_service
        runtime.editable_workspaces = editable_workspaces
        runtime.volume_service = volume_service
        runtime.process_provider = process_provider
        runtime.process_supervisor = process_supervisor
        runtime.attempt_launcher = attempt_launcher
        runtime.execution_binder = execution_binder
        runtime.attempt_finalizer = attempt_finalizer
        runtime.attempt_provider = attempt_provider
        runtime.retained_study_service = retained_study_service
        runtime.run_reader = run_reader
        runtime.run_views = run_views
        runtime.child_runs = RealmChildRunService(ledger, principal)
        runtime.review_collections = RealmReviewCollectionService(ledger, principal)
        runtime.shortlists = RealmShortlistService(runtime.review_collections)
        runtime.catalog = catalog
        runtime.configured_package_ingress = configured_package_ingress
        runtime.selection_actions = selection_actions
        runtime.selection_content = selection_content
        runtime.inspection_targets = inspection_targets
        runtime.interface_outputs = interface_outputs
        runtime.operator_attempt_binder = operator_attempt_binder
        runtime.operator_capacity_pool = capacity_pool
        runtime.operator_jobs = operator_jobs
        # Imported lazily to avoid a module cycle: the retained driver names
        # LocalRealmRuntime for runtime validation, while this Core service is
        # composed only after the runtime graph itself exists.
        from optpilot.study_launch_service import RealmStudyLaunchService
        from optpilot.realm_run_execution_service import RealmRunExecutionService

        runtime.study_launches = RealmStudyLaunchService(runtime)
        runtime.run_execution = RealmRunExecutionService(runtime)
        runtime.environment_preview_binder = environment_preview_binder
        runtime.container_web_provider = container_web_provider
        runtime.container_web_broker_authority = container_web_broker_authority
        runtime._closed = False
        return runtime

    @property
    def closed(self) -> bool:
        """Whether this composition root released its owned descriptors."""

        return self._closed

    def close(self) -> None:
        """Release owned descriptors exactly once."""

        if self._closed:
            return
        self._closed = True
        try:
            self.content_store.close()
        finally:
            self.ledger.close()

    def __enter__(self) -> "LocalRealmRuntime":
        if self._closed:
            raise RuntimeError("Local Realm runtime is closed.")
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def _prepare_absolute_root(realm_root: Path) -> Path:
    if not isinstance(realm_root, Path):
        raise TypeError("realm_root must be a Path.")
    if not realm_root.is_absolute():
        raise ValueError("realm_root must be an absolute path.")
    return prepare_private_directory(realm_root)


def _register_actor(
    *,
    ledger: RealmLedger,
    actor_principal_id: str,
) -> PrincipalRecord:
    digest = request_digest(
        {
            "format": "optpilot.local-runtime-principal-bootstrap.v1",
            "principal_id": actor_principal_id,
            "realm_id": ledger.realm_id,
        }
    )
    return ledger.register_principal(
        operation_id=f"local-runtime/bootstrap/principal/{digest}",
        principal_id=actor_principal_id,
        kind=LOCAL_REALM_PRINCIPAL_KIND,
    )


__all__ = [
    "LOCAL_REALM_CONTENT_STORE_ID",
    "LocalRealmRuntime",
]
