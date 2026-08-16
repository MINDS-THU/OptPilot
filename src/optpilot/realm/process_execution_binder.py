"""Realm-native realization of one retained process attempt.

This module is the provider boundary between a portable attempt plan and the
host paths needed by a supervised local process.  Durable records contain only
portable semantics plus fenced Realm-local ids.  Host paths live solely in a
``ManagedProcessExecutionBinding`` and are never accepted from callers.

The first native process provider is deliberately advisory: its verified-copy
projection is private to one attempt operation, but the operating system does
not make that copy immutable to a process running as the same user.  The
supervisor must therefore stop the worker before resource release.  The public
release method additionally requires the canonical attempt to be terminal.
"""

from __future__ import annotations

import os

import math
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence, Tuple

from ..runtime_binding import (
    CandidateRuntimeInput,
    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    ENVIRONMENT_SOURCE_SCOPE,
    LayeredVolumeScopeSource,
    PortableAttemptRuntimeSpec,
    PortableRuntimeScope,
    ProjectionScopeSource,
    VolumeScopeSource,
    compile_retained_process_attempt_runtime,
)
from ._validation import lower_hex_digest, thaw_json
from .errors import (
    RealmConflict,
    RealmError,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .execution_binding_records import (
    ExecutionBindingDraft,
    ExecutionBindingRecord,
    ExecutionLaunchIntentRecord,
    ExecutionProjectionHandle,
    ExecutionTerminalEvidenceRecord,
    ExecutionVolumeHandle,
    RunAttemptBindingAuthorityReceipt,
    RunAttemptBindingReceipt,
    projection_private_coordinate_digest,
    run_attempt_binding_operation_id,
    run_attempt_projection_operation_id,
    run_attempt_resource_holder_id,
    run_attempt_terminal_evidence_operation_id,
    run_attempt_volume_operation_id,
)
from .ephemeral_volume_service import (
    ManagedEphemeralVolume,
    RealmEphemeralVolumeService,
)
from .ephemeral_volume_records import EphemeralVolumeState
from .ledger import RealmLedger
from .layered_volume_realization import compile_local_layered_volume_plan
from .leases import LeaseRecord
from .local_process_supervisor import WorkerTerminalProof
from .owners import OwnerChange, OwnerMembership, OwnerPermission
from .process_provider import ProcessProviderIdentity
from .projection_records import (
    ProjectionRealizationRecord,
    ProjectionRealizationState,
)
from .projection_service import ManagedReadOnlyProjection, RealmProjectionService
from .refs import canonical_json_bytes, request_digest
from .run_records import RUN_CANDIDATE_ROLE
from .run_attempt_records import (
    RunAttemptHeartbeatAuthorityReceipt,
    RunAttemptPreparationReceipt,
)
from .run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    ScopePath,
)


@dataclass(frozen=True)
class ResolvedRuntimeScope:
    """One typed portable scope paired with its transient host root."""

    scope: PortableRuntimeScope
    host_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PortableRuntimeScope):
            raise TypeError("scope must be a PortableRuntimeScope.")
        path = Path(self.host_path)
        if not path.is_absolute():
            raise ValueError("resolved runtime scope host_path must be absolute.")
        object.__setattr__(self, "host_path", path)

    def resolve(self, value: ScopePath) -> Path:
        """Resolve one canonical relative path inside this exact scope."""

        if not isinstance(value, ScopePath):
            raise TypeError("value must be a ScopePath.")
        if value.scope != self.scope.name:
            raise ValueError("scope path names a different runtime scope.")
        if value.relative_path == ".":
            return self.host_path
        # ScopePath already rejects absolute paths, ``..``, and noncanonical
        # separators.  Keep this lexical: writable trees may legitimately gain
        # symlinks while the trusted native worker runs.
        return self.host_path.joinpath(*value.relative_path.split("/"))


@dataclass(frozen=True)
class ProcessExecutionResourceFailure:
    """One failed validation, heartbeat, or release action."""

    phase: str
    resource_kind: str
    logical_name: str
    error: BaseException


class ProcessExecutionResourceError(RealmError):
    """Several resource actions were attempted and at least one failed."""

    def __init__(
        self,
        message: str,
        failures: Sequence[ProcessExecutionResourceFailure],
    ) -> None:
        values = tuple(failures)
        if not values:
            raise ValueError("resource error requires at least one failure.")
        self.failures = values
        summary = "; ".join(
            f"{item.phase}:{item.resource_kind}:{item.logical_name}: "
            f"{type(item.error).__name__}: {item.error}"
            for item in values
        )
        super().__init__(f"{message} ({summary})")


@dataclass(frozen=True)
class RealizedProcessRuntimeResources:
    projection: ManagedReadOnlyProjection
    volumes: Tuple[tuple[str, ManagedEphemeralVolume], ...]
    resolved_scopes: Tuple[ResolvedRuntimeScope, ...]


class _InitializationLeasePulse:
    """Renew exact child resources while provider copy progresses.

    Child renewals are capped by their ancestor's expiry.  Two children
    renewed on opposite sides of an ancestor heartbeat can therefore receive
    materially different deadlines even when both request the same TTL.
    Scheduling is consequently per resource and follows the expiry actually
    returned by the ledger; a single completion-based TTL cadence can let the
    earlier child expire while a later sibling remains live.
    """

    def __init__(
        self,
        *,
        projection: ManagedReadOnlyProjection,
        volumes: Sequence[tuple[str, ManagedEphemeralVolume]],
        operation_prefix: str,
        ttl_seconds: float,
    ) -> None:
        self._projection = projection
        self._volumes = list(volumes)
        self._operation_prefix = (
            f"{operation_prefix}/session-{uuid.uuid4().hex}"
        )
        self._ttl_seconds = float(ttl_seconds)
        self._interval = max(0.001, min(self._ttl_seconds / 3.0, 30.0))
        self._index = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._next_due: dict[tuple[str, str], float] = {
            ("projection", ""): self._next_due_from_lease(
                projection.consumer_lease
            )
        }
        self._heartbeat_revisions: dict[tuple[str, str], int] = {
            ("projection", ""): projection.consumer_lease.heartbeat_revision
        }
        for logical_name, volume in self._volumes:
            key = ("volume", logical_name)
            self._next_due[key] = self._next_due_from_lease(volume.lease)
            self._heartbeat_revisions[key] = volume.lease.heartbeat_revision

    def __call__(self) -> None:
        self.pulse()

    def add_volume(
        self, logical_name: str, volume: ManagedEphemeralVolume
    ) -> None:
        with self._lock:
            if any(name == logical_name for name, _item in self._volumes):
                raise RealmIntegrityError(
                    "Initialization heartbeat volume name is duplicated."
                )
            self._volumes.append((logical_name, volume))
            key = ("volume", logical_name)
            self._next_due[key] = self._next_due_from_lease(volume.lease)
            self._heartbeat_revisions[key] = volume.lease.heartbeat_revision
            self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Initialization heartbeat is already started.")
            self._thread = threading.Thread(
                target=self._run,
                name=f"layered-volume-heartbeat-{self._operation_prefix[-12:]}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, raise_error: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        if raise_error:
            self.raise_if_failed()

    def raise_if_failed(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RealmConflict(
                "Layered volume initialization heartbeat failed."
            ) from error

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                if self._error is not None:
                    return
                next_due = min(self._next_due.values())
            delay = max(0.0, next_due - time.monotonic())
            if self._wake.wait(delay):
                continue
            if self._stop.is_set():
                return
            try:
                self.pulse()
            except BaseException as error:
                with self._lock:
                    self._error = error
                    self._wake.set()
                return

    def pulse(self, *, force: bool = False) -> None:
        with self._lock:
            if self._error is not None:
                raise RealmConflict(
                    "Layered volume initialization heartbeat failed."
                ) from self._error
            now = time.monotonic()
            targets: list[
                tuple[
                    float,
                    tuple[str, str],
                    ManagedReadOnlyProjection | ManagedEphemeralVolume,
                ]
            ] = [
                (
                    self._next_due[("projection", "")],
                    ("projection", ""),
                    self._projection,
                )
            ]
            targets.extend(
                (
                    self._next_due[("volume", logical_name)],
                    ("volume", logical_name),
                    volume,
                )
                for logical_name, volume in self._volumes
            )
            if not force:
                targets = [item for item in targets if item[0] <= now]
            if not targets:
                return
            self._index += 1
            suffix = self._index
            targets.sort(key=lambda item: (item[0], item[1]))

        # Never hold the coordinator lock while entering a resource handle.
        # Layered initialization holds the current volume's re-entrant handle
        # lock while its foreground progress callback pulses.  A background
        # pulse may therefore wait for that handle, but it must not prevent the
        # foreground callback from reserving a distinct heartbeat operation.
        for _due, key, target in targets:
            kind, logical_name = key
            if kind == "projection":
                lease = target.heartbeat_initialization(
                    operation_id=(
                        f"{self._operation_prefix}/{suffix}/projection"
                    ),
                    ttl_seconds=self._ttl_seconds,
                )
            else:
                assert isinstance(target, ManagedEphemeralVolume)
                lease = target.heartbeat_initialization(
                    operation_id=(
                        f"{self._operation_prefix}/{suffix}/volume/{logical_name}"
                    ),
                    ttl_seconds=self._ttl_seconds,
                )
            self._record_heartbeat(key, lease)

    def _record_heartbeat(
        self, key: tuple[str, str], lease: LeaseRecord
    ) -> None:
        with self._lock:
            previous_revision = self._heartbeat_revisions[key]
            if lease.heartbeat_revision >= previous_revision:
                next_due = self._next_due_from_lease(lease)
                self._heartbeat_revisions[key] = lease.heartbeat_revision
                self._next_due[key] = next_due
                self._wake.set()

    def _next_due_from_lease(self, lease: LeaseRecord) -> float:
        remaining = lease.expires_at - time.time()
        if remaining <= 0:
            raise RealmExpired(
                "Initialization resource lease expired during heartbeat."
            )
        return time.monotonic() + min(self._interval, remaining / 3.0)


@dataclass(frozen=True)
class ContainerAttemptPlan:
    """Everything the launcher needs to start one attempt in a container.

    Never persisted: fully re-derivable from the run definition, and re-derived
    on every bind and every recovery -- which is what makes the gates run again
    each time, so an image removed mid-run fails the next attempt honestly.
    """

    engine_path: str
    image_reference: str
    platform: str
    network: bool
    user: str
    #: Host variables the engine client itself needs (finding its daemon).
    #: Captured once at gating so a recompile is byte-identical.
    engine_env: tuple[tuple[str, str], ...]
    #: Resource limits the component raised, as sorted (name, value) pairs;
    #: empty means the defaults apply.
    limits: tuple[tuple[str, str | int], ...] = ()


#: What the engine client may see of this host's environment. The client is the
#: program talking to the container daemon, not the evaluator; it needs its
#: daemon coordinates and nothing else.
_ENGINE_ENV_ALLOWLIST = ("HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")


class PreparedProcessExecutionBinding:
    """Realized resources authenticated for a not-yet-committed launch.

    A provider may use this object to build and durably reserve its exact
    pathful launch request.  Only the provider-internal
    :meth:`_commit_reserved_launch` seam may turn that reservation into the
    atomic Realm binding/launch intent.  The same resources remain attached
    after commit, and this object remains a valid heartbeat target while that
    handoff is in flight.
    """

    def __init__(
        self,
        *,
        binder: "RealmProcessExecutionBinder",
        actor_principal_id: str,
        authority: RunAttemptHeartbeatAuthorityReceipt,
        draft: ExecutionBindingDraft,
        resources: RealizedProcessRuntimeResources,
        container_plan: "ContainerAttemptPlan | None" = None,
    ) -> None:
        self.container_plan = container_plan
        if not isinstance(authority, RunAttemptHeartbeatAuthorityReceipt):
            raise TypeError(
                "authority must be a RunAttemptHeartbeatAuthorityReceipt."
            )
        if not isinstance(draft, ExecutionBindingDraft):
            raise TypeError("draft must be an ExecutionBindingDraft.")
        draft.validate_attempt(authority.attempt)
        self._binder = binder
        self._actor_principal_id = actor_principal_id
        self._authority = authority
        self._draft = draft
        self._resources = resources
        self._scope_by_name = MappingProxyType(
            {item.scope.name: item for item in resources.resolved_scopes}
        )
        self._committed: ManagedProcessExecutionBinding | None = None
        self._lock = threading.RLock()

    @property
    def run_id(self) -> str:
        return self._draft.run_id

    @property
    def attempt_id(self) -> str:
        return self._draft.attempt_id

    @property
    def attempt(self):
        return self._authority.attempt

    @property
    def draft(self) -> ExecutionBindingDraft:
        return self._draft

    @property
    def portable_spec(self) -> PortableAttemptRuntimeSpec:
        return self._draft.portable_spec

    @property
    def scope_paths(self) -> Mapping[str, Path]:
        self.validate()
        return MappingProxyType(
            {name: item.host_path for name, item in self._scope_by_name.items()}
        )

    @property
    def python_import_paths(self) -> Tuple[Path, ...]:
        return tuple(
            self.resolve_scope_path(item)
            for item in self.portable_spec.python_import_roots
        )

    @property
    def workdir(self) -> Path:
        return self.resolve_scope_path(self.portable_spec.workdir)

    def resolve_scope_path(self, value: ScopePath) -> Path:
        if not isinstance(value, ScopePath):
            raise TypeError("value must be a ScopePath.")
        self.validate()
        try:
            scope = self._scope_by_name[value.scope]
        except KeyError as error:
            raise ValueError(
                "scope path is absent from the runtime binding."
            ) from error
        return scope.resolve(value)

    def validate(self) -> None:
        with self._lock:
            validate_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
            )

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        """Renew realized resources before, during, or after launch commit."""

        with self._lock:
            heartbeat_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
                operation_id=operation_id,
                ttl_seconds=ttl_seconds,
            )

    def _commit_reserved_launch(
        self, reservation: object
    ) -> "ManagedProcessExecutionBinding":
        """Provider-internal atomic commit of one authenticated reservation."""

        with self._lock:
            if self._committed is not None:
                self._binder._verify_launch_reservation(self, reservation)
                return self._committed
            managed = self._binder._commit_reserved_launch(
                prepared=self,
                reservation=reservation,
            )
            self._committed = managed
            return managed


class ManagedProcessExecutionBinding:
    """Attached resources for one durably bound process attempt.

    There is intentionally no generic ``close`` method and no context-manager
    protocol.  A native host path is not an OS-revocable capability.  Callers
    may release it only after worker termination and canonical attempt
    terminalization.
    """

    def __init__(
        self,
        *,
        binder: "RealmProcessExecutionBinder",
        actor_principal_id: str,
        receipt: RunAttemptBindingReceipt | RunAttemptBindingAuthorityReceipt,
        resources: RealizedProcessRuntimeResources,
        container_plan: "ContainerAttemptPlan | None" = None,
    ) -> None:
        self.container_plan = container_plan
        if not isinstance(
            receipt, (RunAttemptBindingReceipt, RunAttemptBindingAuthorityReceipt)
        ):
            raise TypeError("receipt must describe a current run-attempt binding.")
        self._binder = binder
        self._actor_principal_id = actor_principal_id
        self._receipt = receipt
        self._resources = resources
        self._scope_by_name = MappingProxyType(
            {item.scope.name: item for item in resources.resolved_scopes}
        )
        self._released = False
        self._lock = threading.RLock()

    @property
    def receipt(
        self,
    ) -> RunAttemptBindingReceipt | RunAttemptBindingAuthorityReceipt:
        return self._receipt

    @property
    def run_id(self) -> str:
        return self._receipt.binding.run_id

    @property
    def attempt_id(self) -> str:
        return self._receipt.binding.attempt_id

    @property
    def binding_id(self) -> str:
        return self._receipt.binding.binding_id

    @property
    def commit_receipt(self) -> RunAttemptBindingReceipt | None:
        return (
            self._receipt
            if isinstance(self._receipt, RunAttemptBindingReceipt)
            else None
        )

    @property
    def authority_receipt(self) -> RunAttemptBindingAuthorityReceipt | None:
        return (
            self._receipt
            if isinstance(self._receipt, RunAttemptBindingAuthorityReceipt)
            else None
        )

    @property
    def launch_intent(self) -> ExecutionLaunchIntentRecord:
        """Return the intent committed atomically with this binding."""

        return self._receipt.launch_intent

    @property
    def portable_spec(self) -> PortableAttemptRuntimeSpec:
        return self._receipt.binding.portable_spec

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def resolved_scopes(self) -> Tuple[ResolvedRuntimeScope, ...]:
        self.validate()
        return self._resources.resolved_scopes

    @property
    def scope_paths(self) -> Mapping[str, Path]:
        """Return a validated transient name-to-host-root map."""

        self.validate()
        return MappingProxyType(
            {name: item.host_path for name, item in self._scope_by_name.items()}
        )

    @property
    def python_import_paths(self) -> Tuple[Path, ...]:
        return tuple(
            self.resolve_scope_path(item)
            for item in self.portable_spec.python_import_roots
        )

    @property
    def workdir(self) -> Path:
        return self.resolve_scope_path(self.portable_spec.workdir)

    def resolve_scope_path(self, value: ScopePath) -> Path:
        if not isinstance(value, ScopePath):
            raise TypeError("value must be a ScopePath.")
        self.validate()
        try:
            scope = self._scope_by_name[value.scope]
        except KeyError as error:
            raise ValueError("scope path is absent from the runtime binding.") from error
        return scope.resolve(value)

    def validate(self) -> None:
        """Validate every current lease and physical namespace."""

        with self._lock:
            if self._released:
                raise RealmConflict("Process execution binding is released.")
            validate_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
            )

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        """Renew all provider resources without hiding partial failures.

        The run controller remains responsible for heartbeating the controller
        and attempt leases first.  ``operation_id`` identifies this repeated
        heartbeat round; exact retry with the same TTL is idempotent.
        """

        with self._lock:
            if self._released:
                raise RealmConflict("Process execution binding is released.")
            heartbeat_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
                operation_id=operation_id,
                ttl_seconds=ttl_seconds,
            )

    def authenticate_and_record_terminal(
        self, terminal_proof: WorkerTerminalProof
    ) -> ExecutionTerminalEvidenceRecord:
        """Authenticate provider proof and return its durable path-free record."""

        return self._binder.authenticate_and_record_terminal(
            actor_principal_id=self._actor_principal_id,
            run_id=self._receipt.binding.run_id,
            attempt_id=self._receipt.binding.attempt_id,
            terminal_proof=terminal_proof,
        )

    def release_after_worker_stopped(
        self, terminal_proof: WorkerTerminalProof
    ) -> None:
        """Release all resources after canonical terminalization.

        The provider registry must authenticate ``terminal_proof`` and it must
        name this exact launch, binding, and evidence fingerprint.  Canonical
        attempt terminality is checked independently before any resource is
        mutated.
        """

        with self._lock:
            if self._released:
                return
            self._binder._cleanup_terminal_binding(
                actor_principal_id=self._actor_principal_id,
                run_id=self._receipt.binding.run_id,
                attempt_id=self._receipt.binding.attempt_id,
                terminal_proof=terminal_proof,
                resources=self._resources,
            )
            self._released = True


class RealmProcessExecutionBinder:
    """Compile, realize, authenticate, and commit one process attempt binding."""

    def __init__(
        self,
        ledger: RealmLedger,
        projection_service: RealmProjectionService,
        volume_service: RealmEphemeralVolumeService,
        provider: ProcessProviderIdentity,
        *,
        trust_policy: object | None = None,
        launch_reservation_verifier: (
            Callable[[object, PreparedProcessExecutionBinding], str] | None
        ) = None,
        terminal_proof_verifier: (
            Callable[[WorkerTerminalProof], WorkerTerminalProof] | None
        ) = None,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(volume_service, RealmEphemeralVolumeService):
            raise TypeError("volume_service must be a RealmEphemeralVolumeService.")
        if not isinstance(provider, ProcessProviderIdentity):
            raise TypeError("provider must be a ProcessProviderIdentity.")
        if projection_service.ledger is not ledger or volume_service.ledger is not ledger:
            raise ValueError("execution services must share the exact Realm ledger.")
        if terminal_proof_verifier is not None and not callable(
            terminal_proof_verifier
        ):
            raise TypeError("terminal_proof_verifier must be callable or None.")
        if launch_reservation_verifier is not None and not callable(
            launch_reservation_verifier
        ):
            raise TypeError(
                "launch_reservation_verifier must be callable or None."
            )
        self._ledger = ledger
        self._projection_service = projection_service
        self._volume_service = volume_service
        self._provider = provider
        self._trust_policy = trust_policy
        self._launch_reservation_verifier = launch_reservation_verifier
        self._terminal_proof_verifier = terminal_proof_verifier

    def _container_attempt_plan(self, definition) -> "ContainerAttemptPlan | None":
        """Gate and describe a container attempt, or None for a process one.

        Runs on every bind and every recovery -- there is no cached approval,
        so revoking trust or removing the image stops the next attempt rather
        than being discovered much later.
        """

        runtime = definition.evaluation_closure.prepared_runtime
        if runtime.runtime_kind != "container":
            return None
        from ..container_engine import (
            ContainerEngineError,
            resolve_container_engine,
            verify_image_available,
        )
        from .provider_trust_records import PROVIDER_TRUST_EXECUTION_CONTRACT

        settings = runtime.runtime_settings
        reference = str(settings["container_image_reference"])
        platform = str(settings["container_platform"])
        try:
            engine = resolve_container_engine()
        except ContainerEngineError as error:
            raise RealmConflict(
                f"This environment runs in a container, and {error}"
            ) from error
        approval = None
        if self._trust_policy is not None:
            try:
                approval = self._trust_policy.read_active(
                    image_ref=reference,
                    contract=PROVIDER_TRUST_EXECUTION_CONTRACT,
                )
            except Exception:
                approval = None
        if approval is None:
            raise RealmConflict(
                "The environment's image has not been approved for study "
                "execution."
            )
        try:
            verify_image_available(engine, reference, platform)
        except ContainerEngineError as error:
            raise RealmConflict(str(error)) from error
        return ContainerAttemptPlan(
            engine_path=engine,
            image_reference=reference,
            platform=platform,
            network=settings["container_network"] == "enabled",
            user=f"{os.getuid()}:{os.getgid()}",
            engine_env=tuple(
                (name, os.environ[name])
                for name in _ENGINE_ENV_ALLOWLIST
                if name in os.environ
            ),
            limits=tuple(
                sorted((dict(settings.get("container_limits") or {})).items())
            ),
        )

    def prepare_binding(
        self,
        *,
        actor_principal_id: str,
        preparation: RunAttemptPreparationReceipt,
    ) -> PreparedProcessExecutionBinding:
        """Realize and preflight one exact prepared attempt without committing."""

        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(preparation, RunAttemptPreparationReceipt):
            raise TypeError("preparation must be a RunAttemptPreparationReceipt.")
        current = self._ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id=actor_principal_id,
            run_id=preparation.attempt.run_id,
            attempt_id=preparation.attempt.attempt_id,
        )
        if (
            current.attempt != preparation.attempt
            or current.resource_ttl_seconds != preparation.resource_ttl_seconds
        ):
            raise RealmConflict("Prepared attempt changed before provider binding.")
        return self._prepare_authority(
            actor_principal_id=actor_principal_id,
            authority=current,
        )

    def prepare_prepared(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
    ) -> PreparedProcessExecutionBinding:
        """Realize one canonical prepared attempt from current persisted authority.

        Unlike :meth:`bind`, this recovery seam never accepts a historical
        preparation receipt.  It reads the current persisted controller,
        attempt, capture-change, and retention-lease authority chain before
        replaying deterministic realization operations.  A caller recovering
        an already committed binding must use :meth:`recover` instead.
        """

        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty text.")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be nonempty text.")
        authority = self._ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        if authority.attempt.state != "prepared":
            raise RealmConflict("Only a prepared attempt can acquire a new binding.")
        return self._prepare_authority(
            actor_principal_id=actor_principal_id,
            authority=authority,
        )

    def _prepare_authority(
        self,
        *,
        actor_principal_id: str,
        authority: RunAttemptHeartbeatAuthorityReceipt,
    ) -> PreparedProcessExecutionBinding:
        if not isinstance(authority, RunAttemptHeartbeatAuthorityReceipt):
            raise TypeError(
                "authority must be a RunAttemptHeartbeatAuthorityReceipt."
            )
        attempt = authority.attempt
        if attempt.state != "prepared":
            raise RealmConflict("Only a prepared attempt can acquire a new binding.")
        definition = self._ledger.read_run_definition(
            actor_principal_id=actor_principal_id,
            run_id=attempt.run_id,
        )
        candidate_input, candidate_bindings = _candidate_authority_input(authority)
        spec = compile_retained_process_attempt_runtime(
            owner_id=authority.run.owner_id,
            run_definition=definition,
            evaluation_spec=attempt.evaluation_spec,
            provider=self._provider,
            candidate_input=candidate_input,
            container_execution_supported=True,
        )
        container_plan = self._container_attempt_plan(definition)
        if spec.run_definition_digest != definition.digest:
            raise RealmIntegrityError(
                "Compiled runtime plan differs from the retained run definition."
            )
        store_id = self._resolve_input_store(
            actor_principal_id=actor_principal_id,
            spec=spec,
            candidate_bindings=candidate_bindings,
        )
        ttl_seconds = authority.resource_ttl_seconds
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise RealmIntegrityError("Prepared attempt lease duration is malformed.")
        holder_id = run_attempt_resource_holder_id(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding_id=attempt.binding_id,
        )

        resources = self._realize_resources(
            actor_principal_id=actor_principal_id,
            preparation=authority,
            spec=spec,
            store_id=store_id,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
        )
        current_authority = self._refresh_attempt_authority(
            actor_principal_id=actor_principal_id,
            expected=authority,
        )

        projection_handle = _projection_handle(spec, resources.projection)
        volume_handles = tuple(
            _volume_handle(logical_name, volume)
            for logical_name, volume in resources.volumes
        )
        last_conflict: RealmConflict | None = None
        for _retry in range(16):
            try:
                draft = self._ledger.preflight_run_attempt_binding(
                    actor_principal_id=actor_principal_id,
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    run_definition_digest=definition.digest,
                    provider=self._provider,
                    projections=(projection_handle,),
                    writable_volumes=volume_handles,
                    resource_ttl_seconds=ttl_seconds,
                    expected_run_revision=current_authority.run.current_revision,
                    controller_lease_id=(
                        current_authority.controller_lease.lease_id
                    ),
                    controller_holder_id=(
                        current_authority.controller_lease.holder_id
                    ),
                    controller_fencing_token=(
                        current_authority.controller_lease.fencing_token
                    ),
                )
                break
            except RealmConflict as error:
                last_conflict = error
                try:
                    durable = self._ledger.read_run_attempt_binding(
                        actor_principal_id=actor_principal_id,
                        run_id=attempt.run_id,
                        attempt_id=attempt.attempt_id,
                    )
                except RealmNotFound:
                    durable = None
                if durable is not None:
                    if (
                        durable.portable_spec != spec
                        or durable.projections != (projection_handle,)
                        or durable.writable_volumes != volume_handles
                    ):
                        raise RealmIntegrityError(
                            "Concurrent durable binding differs from realized resources."
                        ) from error
                    error.add_note(
                        "The exact resources became durably bound and were retained; "
                        "recover the committed binding."
                    )
                    raise
                refreshed = self._ledger.read_run_attempt_heartbeat_authority(
                    actor_principal_id=actor_principal_id,
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                )
                if (
                    refreshed.attempt != attempt
                    or refreshed.resource_ttl_seconds != ttl_seconds
                ):
                    error.add_note(
                        "Realized unbound resources were retained for TTL cleanup "
                        "because attempt authority changed."
                    )
                    raise
                current_authority = refreshed
            except BaseException as error:
                error.add_note(
                    "Realized unbound resources were retained for deterministic "
                    "recovery and TTL cleanup."
                )
                raise
        else:
            assert last_conflict is not None
            last_conflict.add_note(
                "Preflight retried after repeated unrelated run-head changes; "
                "realized resources were retained."
            )
            raise last_conflict
        return PreparedProcessExecutionBinding(
            binder=self,
            actor_principal_id=actor_principal_id,
            container_plan=container_plan,
            authority=current_authority,
            draft=draft,
            resources=resources,
        )

    def recover(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
    ) -> ManagedProcessExecutionBinding:
        """Reattach one durable prepared/running binding as the current actor.

        Recovery never replays creator-bound allocation requests.  The ledger
        first revalidates the exact persisted handles and their live ancestor
        authority; the provider services then reopen only those namespaces.
        """

        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty text.")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be nonempty text.")
        authority = self._ledger.validate_run_attempt_binding_authority(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        binding = authority.binding
        spec = binding.portable_spec
        if (
            spec.provider.kind != "process"
            or spec.provider.builder_fingerprint
            != self._provider.builder_fingerprint
            or spec.provider.platform != self._provider.platform
            or binding.evidence.provider.builder_fingerprint
            != self._provider.builder_fingerprint
            or binding.evidence.provider.platform != self._provider.platform
        ):
            raise RealmConflict(
                "Durable attempt binding belongs to another process provider."
            )
        resources = self._reattach_resources(
            actor_principal_id=actor_principal_id,
            authority=authority,
        )
        try:
            recovered_plan = self._container_attempt_plan(
                self._ledger.read_run_definition(
                    actor_principal_id=actor_principal_id,
                    run_id=run_id,
                )
            )
        except (RealmNotFound, RealmConflict):
            # An actor authorized for the binding but not the definition can
            # still recover a PROCESS attempt, whose command needs no plan. If
            # the attempt was a container one, recovering without its plan
            # compiles the process command, whose digest cannot match the
            # reservation -- so this falls closed at verification rather than
            # running the wrong thing.
            recovered_plan = None
        return ManagedProcessExecutionBinding(
            binder=self,
            actor_principal_id=actor_principal_id,
            container_plan=recovered_plan,
            receipt=authority,
            resources=resources,
        )

    def _verify_launch_reservation(
        self,
        prepared: PreparedProcessExecutionBinding,
        reservation: object,
    ) -> str:
        verifier = self._launch_reservation_verifier
        if verifier is None:
            raise RealmConflict(
                "Atomic binding requires a provider reservation verifier."
            )
        digest = lower_hex_digest(
            verifier(reservation, prepared),
            "provider launch reservation request digest",
        )
        try:
            launch_token = reservation.launch_token  # type: ignore[attr-defined]
            binding_id = reservation.binding_id  # type: ignore[attr-defined]
            evidence_fingerprint = reservation.evidence_fingerprint  # type: ignore[attr-defined]
            reservation_digest = reservation.launch_request_digest  # type: ignore[attr-defined]
        except AttributeError as error:
            raise TypeError(
                "reservation must expose its path-free launch identity."
            ) from error
        if (
            launch_token != prepared.attempt.launch_token
            or binding_id != prepared.draft.binding_id
            or evidence_fingerprint != prepared.draft.evidence_fingerprint
            or reservation_digest != digest
        ):
            raise RealmConflict(
                "Provider reservation differs from the prepared binding draft."
            )
        return digest

    def _commit_reserved_launch(
        self,
        *,
        prepared: PreparedProcessExecutionBinding,
        reservation: object,
    ) -> ManagedProcessExecutionBinding:
        """Commit binding plus launch intent, retrying harmless head drift."""

        if prepared._binder is not self:
            raise ValueError("prepared binding belongs to another binder.")
        launch_request_digest = self._verify_launch_reservation(
            prepared, reservation
        )
        last_conflict: RealmConflict | None = None
        for _retry in range(16):
            authority = prepared._authority
            try:
                receipt = self._ledger.commit_run_attempt_binding(
                    operation_id=run_attempt_binding_operation_id(
                        run_id=prepared.run_id,
                        attempt_id=prepared.attempt_id,
                        binding_id=prepared.draft.binding_id,
                    ),
                    actor_principal_id=prepared._actor_principal_id,
                    draft=prepared.draft,
                    launch_request_digest=launch_request_digest,
                    expected_run_revision=authority.run.current_revision,
                    controller_lease_id=authority.controller_lease.lease_id,
                    controller_holder_id=authority.controller_lease.holder_id,
                    controller_fencing_token=(
                        authority.controller_lease.fencing_token
                    ),
                )
                return self._managed_from_reserved_commit(
                    prepared=prepared,
                    receipt=receipt,
                    launch_request_digest=launch_request_digest,
                )
            except RealmConflict as error:
                last_conflict = error
                durable = self._read_exact_ambiguous_binding(
                    prepared=prepared,
                    launch_request_digest=launch_request_digest,
                )
                if durable is not None:
                    return self._managed_from_reserved_commit(
                        prepared=prepared,
                        receipt=durable,
                        launch_request_digest=launch_request_digest,
                    )
                current = self._ledger.read_run_attempt_heartbeat_authority(
                    actor_principal_id=prepared._actor_principal_id,
                    run_id=prepared.run_id,
                    attempt_id=prepared.attempt_id,
                )
                if (
                    current.attempt != prepared.attempt
                    or current.resource_ttl_seconds
                    != prepared.draft.resource_ttl_seconds
                ):
                    raise RealmConflict(
                        "Attempt authority changed after provider reservation."
                    ) from error
                # Retry the atomic transaction directly against the refreshed
                # authority.  It independently reconstructs and compares the
                # binding draft, so a second read-only preflight adds no safety
                # and creates another unhandled compare-and-swap race window.
                prepared._authority = current
                continue
            except BaseException as error:
                durable = self._read_exact_ambiguous_binding(
                    prepared=prepared,
                    launch_request_digest=launch_request_digest,
                    original_error=error,
                )
                if durable is None:
                    error.add_note(
                        "Provider reservation and realized resources were retained; "
                        "abandon the reservation before unbound cleanup."
                    )
                    raise
                return self._managed_from_reserved_commit(
                    prepared=prepared,
                    receipt=durable,
                    launch_request_digest=launch_request_digest,
                )
        assert last_conflict is not None
        last_conflict.add_note(
            "Atomic binding retried after repeated unrelated run-head changes; "
            "the provider reservation and resources were retained."
        )
        raise last_conflict

    def _read_exact_ambiguous_binding(
        self,
        *,
        prepared: PreparedProcessExecutionBinding,
        launch_request_digest: str,
        original_error: BaseException | None = None,
    ) -> RunAttemptBindingAuthorityReceipt | None:
        try:
            durable = self._ledger.validate_run_attempt_binding_authority(
                actor_principal_id=prepared._actor_principal_id,
                run_id=prepared.run_id,
                attempt_id=prepared.attempt_id,
            )
        except RealmNotFound:
            return None
        except RealmConflict:
            # A committed attempt can race from prepared to terminal; the
            # caller still receives the original error with resources retained.
            return None
        except BaseException as proof_error:
            if original_error is not None:
                original_error.add_note(
                    "Atomic binding outcome could not be proven: "
                    f"{type(proof_error).__name__}: {proof_error}"
                )
                return None
            raise
        self._require_exact_reserved_commit(
            prepared=prepared,
            receipt=durable,
            launch_request_digest=launch_request_digest,
        )
        return durable

    def _managed_from_reserved_commit(
        self,
        *,
        prepared: PreparedProcessExecutionBinding,
        receipt: RunAttemptBindingReceipt | RunAttemptBindingAuthorityReceipt,
        launch_request_digest: str,
    ) -> ManagedProcessExecutionBinding:
        self._require_exact_reserved_commit(
            prepared=prepared,
            receipt=receipt,
            launch_request_digest=launch_request_digest,
        )
        return ManagedProcessExecutionBinding(
            binder=self,
            actor_principal_id=prepared._actor_principal_id,
            receipt=receipt,
            resources=prepared._resources,
            container_plan=prepared.container_plan,
        )

    @staticmethod
    def _require_exact_reserved_commit(
        *,
        prepared: PreparedProcessExecutionBinding,
        receipt: RunAttemptBindingReceipt | RunAttemptBindingAuthorityReceipt,
        launch_request_digest: str,
    ) -> None:
        binding = receipt.binding
        intent = receipt.launch_intent
        if (
            binding.run_id != prepared.draft.run_id
            or binding.attempt_id != prepared.draft.attempt_id
            or binding.binding_id != prepared.draft.binding_id
            or binding.portable_spec != prepared.draft.portable_spec
            or binding.evidence != prepared.draft.evidence
            or binding.projections != prepared.draft.projections
            or binding.writable_volumes != prepared.draft.writable_volumes
            or binding.resource_ttl_seconds
            != prepared.draft.resource_ttl_seconds
            or intent.launch_request_digest != launch_request_digest
            or intent.launch_token != prepared.attempt.launch_token
        ):
            raise RealmIntegrityError(
                "Durable atomic launch differs from the provider reservation."
            )

    def authenticate_and_record_terminal(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
        terminal_proof: WorkerTerminalProof,
    ) -> ExecutionTerminalEvidenceRecord:
        """Authenticate termination and retain its path-free evidence record.

        Controllers call this before a finalizer reads writable trial data.
        Canonical attempt terminality is intentionally not required yet: the
        finalization/adoption step follows this observation.  Persisting this
        evidence first lets adoption atomically create cleanup authority.
        """

        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty text.")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be nonempty text.")
        if not isinstance(terminal_proof, WorkerTerminalProof):
            raise TypeError("terminal_proof must be a WorkerTerminalProof.")
        verifier = self._terminal_proof_verifier
        if verifier is None:
            raise RealmConflict(
                "Worker termination validation requires a provider proof verifier."
            )
        attempt = self._ledger.read_run_attempt(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        binding = self._ledger.read_run_attempt_binding(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        launch_intent = self._ledger.read_run_attempt_launch_intent(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        launch_intent.validate_binding(binding, attempt)
        if (
            terminal_proof.launch_token != attempt.launch_token
            or terminal_proof.binding_id != binding.binding_id
            or terminal_proof.binding_id != attempt.binding_id
            or terminal_proof.evidence_fingerprint != binding.evidence_fingerprint
            or terminal_proof.launch_request_digest
            != launch_intent.launch_request_digest
        ):
            raise RealmConflict(
                "Process terminal proof differs from the exact attempt binding."
            )
        if attempt.state == "running" and not terminal_proof.started:
            raise RealmConflict(
                "A confirmed-running attempt cannot have never-started evidence."
            )
        self._require_provider_binding(binding)
        verified = verifier(terminal_proof)
        if not isinstance(verified, WorkerTerminalProof) or verified != terminal_proof:
            raise RealmIntegrityError(
                "Process terminal proof verifier returned different evidence."
            )
        proof_fingerprint = request_digest(
            {
                "format": "optpilot.execution-terminal-evidence.v1",
                "provider_kind": binding.portable_spec.provider.kind,
                "proof": verified.to_dict(),
            }
        )
        return self._ledger.commit_run_attempt_terminal_evidence(
            operation_id=run_attempt_terminal_evidence_operation_id(
                actor_principal_id=actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
                binding_id=binding.binding_id,
                proof_fingerprint=proof_fingerprint,
            ),
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
            binding_id=binding.binding_id,
            launch_token=verified.launch_token,
            provider_kind=binding.portable_spec.provider.kind,
            evidence_fingerprint=binding.evidence_fingerprint,
            launch_request_digest=verified.launch_request_digest,
            proof_fingerprint=proof_fingerprint,
            started=verified.started,
            disposition=verified.disposition,
        )

    def cleanup_terminal_binding(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
        terminal_proof: WorkerTerminalProof,
    ) -> ExecutionBindingRecord:
        """Clean a terminal attempt's exact durable resources after restart.

        This path deliberately does not reattach runtime namespaces or require
        their leases to remain live.  It is the crash-recovery seam for a
        controller that adopted the attempt before provider cleanup completed.
        """

        return self._cleanup_terminal_binding(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_proof=terminal_proof,
            resources=None,
        )

    def resume_authorized_cleanup(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
    ) -> ExecutionBindingRecord:
        """Retry physical cleanup from its immutable durable authorization.

        This seam deliberately does not re-read a provider registry proof.  The
        proof was consumed once when cleanup authorization was committed.  A
        restart instead validates the current actor, canonical terminal state,
        exact binding and launch intent, immutable cleanup authorization, and
        every durable resource handle before the first cleanup side effect.
        """

        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty text.")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be nonempty text.")
        authority = self._ledger.validate_run_attempt_cleanup_authority(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        self._require_provider_binding(authority.binding)
        projection_handle, local_volumes = self._preflight_terminal_resources(
            binding=authority.binding,
            resources=None,
        )
        return self._perform_authorized_cleanup(
            actor_principal_id=actor_principal_id,
            binding=authority.binding,
            projection_handle=projection_handle,
            local_volumes=local_volumes,
            resources=None,
        )

    def _cleanup_terminal_binding(
        self,
        *,
        actor_principal_id: str,
        run_id: str,
        attempt_id: str,
        terminal_proof: WorkerTerminalProof,
        resources: RealizedProcessRuntimeResources | None,
    ) -> ExecutionBindingRecord:
        """Proof-gate then attempt every exact cleanup action."""

        self.authenticate_and_record_terminal(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_proof=terminal_proof,
        )
        attempt = self._ledger.read_run_attempt(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        if attempt.state != "terminal":
            raise RealmConflict(
                "Process resources cannot be released before the attempt is terminal."
            )
        authority = self._ledger.validate_run_attempt_cleanup_authority(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        binding = authority.binding
        self._require_provider_binding(binding)
        projection_handle, local_volumes = self._preflight_terminal_resources(
            binding=binding,
            resources=resources,
        )
        return self._perform_authorized_cleanup(
            actor_principal_id=actor_principal_id,
            binding=binding,
            projection_handle=projection_handle,
            local_volumes=local_volumes,
            resources=resources,
        )

    def _perform_authorized_cleanup(
        self,
        *,
        actor_principal_id: str,
        binding: ExecutionBindingRecord,
        projection_handle: ExecutionProjectionHandle,
        local_volumes: Mapping[str, ManagedEphemeralVolume],
        resources: RealizedProcessRuntimeResources | None,
    ) -> ExecutionBindingRecord:
        """Attempt every exact physical cleanup after typed authorization."""

        failures: list[ProcessExecutionResourceFailure] = []
        for handle in binding.writable_volumes:
            try:
                key = request_digest(
                    {
                        "format": "optpilot.process-terminal-volume-cleanup.v1",
                        "realm_id": self._ledger.realm_id,
                        "volume_root_id": self._volume_service.root_binding.volume_root_id,
                        "binding_id": binding.binding_id,
                        "volume_id": handle.volume_id,
                    }
                )
                receipt = self._volume_service.reconcile_volume(
                    operation_id=f"process-terminal.volume/{key}",
                    volume_id=handle.volume_id,
                )
                if receipt.volume.state is not EphemeralVolumeState.CLEANED:
                    raise RealmIntegrityError(
                        "Terminal volume cleanup did not reach cleaned state."
                    )
                local = local_volumes.get(handle.logical_name)
                if local is not None:
                    local._detach_without_release()
            except BaseException as error:
                failures.append(
                    ProcessExecutionResourceFailure(
                        "terminal-cleanup", "volume", handle.logical_name, error
                    )
                )
        try:
            cleaned = self._retire_terminal_projection(
                actor_principal_id=actor_principal_id,
                binding=binding,
                handle=projection_handle,
            )
            if cleaned.state is not ProjectionRealizationState.CLEANED:
                raise RealmIntegrityError(
                    "Terminal projection cleanup did not reach cleaned state."
                )
            if resources is not None:
                resources.projection._detach_after_private_retirement()
        except BaseException as error:
            failures.append(
                ProcessExecutionResourceFailure(
                    "terminal-cleanup",
                    "private-projection",
                    projection_handle.logical_name,
                    error,
                )
            )
        if failures:
            raise ProcessExecutionResourceError(
                "Process terminal binding cleanup was incomplete", failures
            )
        return binding

    def _require_provider_binding(self, binding: ExecutionBindingRecord) -> None:
        spec = binding.portable_spec
        if (
            spec.provider.kind != "process"
            or spec.provider.builder_fingerprint
            != self._provider.builder_fingerprint
            or spec.provider.platform != self._provider.platform
            or binding.evidence.provider.builder_fingerprint
            != self._provider.builder_fingerprint
            or binding.evidence.provider.platform != self._provider.platform
        ):
            raise RealmConflict(
                "Durable attempt binding belongs to another process provider."
            )

    def _preflight_terminal_resources(
        self,
        *,
        binding: ExecutionBindingRecord,
        resources: RealizedProcessRuntimeResources | None,
    ) -> tuple[
        ExecutionProjectionHandle, Mapping[str, ManagedEphemeralVolume]
    ]:
        spec = binding.portable_spec
        if len(binding.projections) != 1:
            raise RealmIntegrityError(
                "Native process cleanup requires one projection handle."
            )
        projection_handle = binding.projections[0]
        if projection_handle.logical_name != spec.projection_name:
            raise RealmIntegrityError(
                "Durable projection handle differs from the portable plan."
            )
        volume_handles = {
            item.logical_name: item for item in binding.writable_volumes
        }
        if len(volume_handles) != len(binding.writable_volumes) or set(
            volume_handles
        ) != {item.name for item in spec.writable_volumes}:
            raise RealmIntegrityError(
                "Durable volume handles differ from the portable plan."
            )
        if resources is None:
            return projection_handle, MappingProxyType({})
        if _projection_handle(spec, resources.projection) != projection_handle:
            raise RealmIntegrityError(
                "Attached projection differs from the durable execution handle."
            )
        local_volumes = dict(resources.volumes)
        if set(local_volumes) != set(volume_handles):
            raise RealmIntegrityError(
                "Attached volumes differ from the durable execution handles."
            )
        for logical_name, volume in local_volumes.items():
            if _volume_handle(logical_name, volume) != volume_handles[logical_name]:
                raise RealmIntegrityError(
                    "Attached volume differs from its durable execution handle."
                )
        return projection_handle, MappingProxyType(local_volumes)

    def _retire_terminal_projection(
        self,
        *,
        actor_principal_id: str,
        binding: ExecutionBindingRecord,
        handle: ExecutionProjectionHandle,
    ) -> ProjectionRealizationRecord:
        expected_operation = run_attempt_projection_operation_id(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            binding_id=binding.binding_id,
            logical_name=handle.logical_name,
        )
        coordinate_digest = projection_private_coordinate_digest(
            realm_id=self._ledger.realm_id,
            operation_id=expected_operation,
        )
        key = request_digest(
            {
                "format": "optpilot.process-terminal-projection-retirement.v1",
                "realm_id": self._ledger.realm_id,
                "actor_principal_id": actor_principal_id,
                "projection_root_id": self._projection_service.root_binding.projection_root_id,
                "binding_id": binding.binding_id,
                "realization_id": handle.realization_id,
                "consumer_id": handle.consumer_id,
                "consumer_lease_id": handle.consumer_lease_id,
                "operation_coordinate_digest": coordinate_digest,
            }
        )
        current = self._ledger.read_projection_realization(
            actor_principal_id=actor_principal_id,
            realization_id=handle.realization_id,
        )
        _require_durable_private_projection(
            current,
            handle=handle,
            projection_root_id=(
                self._projection_service.root_binding.projection_root_id
            ),
            coordinate_digest=coordinate_digest,
            spec_digest=binding.portable_spec.projection_spec.digest,
        )
        consumers = self._ledger.list_projection_consumers(
            actor_principal_id=actor_principal_id,
            realization_id=handle.realization_id,
        )
        if (
            len(consumers) != 1
            or consumers[0].consumer_id != handle.consumer_id
            or consumers[0].lease_id != handle.consumer_lease_id
        ):
            raise RealmIntegrityError(
                "Durable private projection consumer identity differs."
            )
        try:
            self._ledger.retire_private_projection_consumer(
                operation_id=f"process-terminal.projection-retire/{key}",
                actor_principal_id=actor_principal_id,
                projection_root_id=(
                    self._projection_service.root_binding.projection_root_id
                ),
                realization_id=handle.realization_id,
                consumer_id=handle.consumer_id,
                consumer_holder_id=run_attempt_resource_holder_id(
                    run_id=binding.run_id,
                    attempt_id=binding.attempt_id,
                    binding_id=binding.binding_id,
                ),
                consumer_fencing_token=handle.consumer_fencing_token,
                expected_operation_coordinate_digest=coordinate_digest,
            )
        except RealmConflict:
            current = self._ledger.read_projection_realization(
                actor_principal_id=actor_principal_id,
                realization_id=handle.realization_id,
            )
            _require_durable_private_projection(
                current,
                handle=handle,
                projection_root_id=(
                    self._projection_service.root_binding.projection_root_id
                ),
                coordinate_digest=coordinate_digest,
                spec_digest=binding.portable_spec.projection_spec.digest,
            )
            if current.state not in {
                ProjectionRealizationState.CLOSING,
                ProjectionRealizationState.CLEANING,
                ProjectionRealizationState.CLEANED,
            }:
                raise
        cleanup_key = request_digest(
            {
                "format": "optpilot.process-terminal-projection-cleanup.v1",
                "realm_id": self._ledger.realm_id,
                "projection_root_id": self._projection_service.root_binding.projection_root_id,
                "realization_id": handle.realization_id,
            }
        )
        return self._projection_service.reconcile_projection(
            operation_id=f"process-terminal.projection-reconcile/{cleanup_key}",
            realization_id=handle.realization_id,
        ).realization

    def _resolve_input_store(
        self,
        *,
        actor_principal_id: str,
        spec: PortableAttemptRuntimeSpec,
        candidate_bindings: Sequence[OwnerMembership],
    ) -> str:
        mappings = spec.projection_spec.mappings
        if not mappings or len(mappings) > 3:
            raise RealmIntegrityError(
                "The native process binder requires one composite input projection."
            )
        source_scopes = tuple(
            item
            for item in spec.scopes
            if item.name == ENVIRONMENT_SOURCE_SCOPE
            and isinstance(item.source, ProjectionScopeSource)
        )
        if len(source_scopes) != 1:
            raise RealmIntegrityError(
                "The native process binder requires one environment source scope."
            )
        source_partition = source_scopes[0].source.relative_path
        environment_mappings = tuple(
            item for item in mappings if item.destination == source_partition
        )
        if (
            len(environment_mappings) != 1
            or environment_mappings[0].source_subpath != "."
        ):
            raise RealmIntegrityError(
                "The environment source partition must map one complete snapshot."
            )
        environment_snapshot = environment_mappings[0].snapshot_ref
        prepared_scopes = tuple(
            item
            for item in spec.scopes
            if item.name == ENVIRONMENT_PREPARED_PYTHON_SCOPE
            and isinstance(item.source, ProjectionScopeSource)
        )
        prepared_mappings = tuple(
            item
            for item in mappings
            if item.destination == ENVIRONMENT_PREPARED_PYTHON_PARTITION
        )
        if bool(prepared_scopes) != bool(prepared_mappings) or (
            prepared_scopes
            and (
                len(prepared_scopes) != 1
                or len(prepared_mappings) != 1
                or prepared_scopes[0].source.relative_path
                != ENVIRONMENT_PREPARED_PYTHON_PARTITION
                or prepared_mappings[0].source_subpath != "site-packages"
            )
        ):
            raise RealmIntegrityError(
                "The prepared Python scope differs from its immutable projection subtree."
            )
        lower_layers = tuple(
            layer
            for scope in spec.scopes
            if isinstance(scope.source, LayeredVolumeScopeSource)
            for layer in scope.source.lower_layers
        )
        seed_layers = tuple(
            item for item in lower_layers if item.collision_policy == "identical"
        )
        candidate_layers = tuple(
            item for item in lower_layers if item.collision_policy == "replace"
        )
        if any(
            item.snapshot_ref != environment_snapshot
            or item.projection_subpath != source_partition
            for item in seed_layers
        ):
            raise RealmIntegrityError(
                "Retained seed inputs do not alias the environment partition."
            )
        materialization = spec.file_materialization
        if materialization is None:
            if candidate_layers or candidate_bindings:
                raise RealmIntegrityError(
                    "Parameter runtime unexpectedly contains candidate content."
                )
        else:
            if len(candidate_layers) != 1 or not candidate_bindings:
                raise RealmConflict(
                    "File candidate runtime requires its exact admitted candidate placement."
                )
            candidate_layer = candidate_layers[0]
            candidate_mapping = tuple(
                item
                for item in mappings
                if item.destination == candidate_layer.projection_subpath
            )
            if (
                len(candidate_mapping) != 1
                or candidate_mapping[0].source_subpath != "."
                or candidate_mapping[0].snapshot_ref != candidate_layer.snapshot_ref
                or candidate_layer.destination_subpath
                != materialization.root.relative_path
            ):
                raise RealmIntegrityError(
                    "File candidate layer differs from its projection/materialization."
                )
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=spec.projection_spec.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        placements = tuple(
            item
            for item in memberships
            if item.role == RUN_ENVIRONMENT_SOURCE_ROLE
            and item.content_ref == environment_snapshot
        )
        if not placements:
            raise RealmConflict(
                "The retained environment source has no authorized store placement."
            )
        eligible_store_ids = {item.store_id for item in placements}
        if prepared_mappings:
            prepared_snapshot = prepared_mappings[0].snapshot_ref
            prepared_placements = tuple(
                item
                for item in memberships
                if item.role == RUN_PREPARED_RUNTIME_ROLE
                and item.content_ref == prepared_snapshot
            )
            if not prepared_placements:
                raise RealmConflict(
                    "The retained prepared runtime has no authorized store placement."
                )
            eligible_store_ids.intersection_update(
                item.store_id for item in prepared_placements
            )
        if seed_layers:
            input_placements = tuple(
                item
                for item in memberships
                if item.role == RUN_ATTEMPT_INPUT_ROLE
                and item.content_ref == environment_snapshot
            )
            if not input_placements:
                raise RealmConflict(
                    "Retained attempt inputs have no authorized store placement."
                )
            eligible_store_ids.intersection_update(
                item.store_id for item in input_placements
            )
        if materialization is not None:
            candidate_layer = candidate_layers[0]
            if any(
                candidate_binding.role != RUN_CANDIDATE_ROLE
                or candidate_binding.content_ref
                != candidate_layer.snapshot_ref
                or candidate_binding not in memberships
                for candidate_binding in candidate_bindings
            ):
                raise RealmConflict(
                    "Admitted candidate content is not authorized for this run."
                )
            eligible_store_ids.intersection_update(
                item.store_id for item in candidate_bindings
            )
        eligible_store_ids.intersection_update(
            self._projection_service.available_store_ids
        )
        if not eligible_store_ids:
            raise RealmConflict(
                "Attempt inputs have no common locally available content store."
            )
        return min(eligible_store_ids, key=lambda item: item.encode("utf-8"))

    def _refresh_attempt_authority(
        self,
        *,
        actor_principal_id: str,
        expected: (
            RunAttemptPreparationReceipt | RunAttemptHeartbeatAuthorityReceipt
        ),
    ) -> RunAttemptHeartbeatAuthorityReceipt:
        """Revalidate the exact attempt fence after provider-side I/O."""

        current = self._ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id=actor_principal_id,
            run_id=expected.attempt.run_id,
            attempt_id=expected.attempt.attempt_id,
        )
        if (
            current.attempt != expected.attempt
            or current.resource_ttl_seconds != expected.resource_ttl_seconds
            or not _same_owner_change_authority(
                current.capture_change, expected.capture_change
            )
            or not _same_lease_authority(
                current.attempt_lease, expected.attempt_lease
            )
            or not _same_lease_authority(
                current.controller_lease, expected.controller_lease
            )
            or not _same_lease_authority(
                current.capture_retention_lease,
                expected.capture_retention_lease,
            )
            or current.candidate != expected.candidate
            or current.candidate_content_bindings
            != expected.candidate_content_bindings
        ):
            raise RealmConflict(
                "Attempt authority changed during provider initialization."
            )
        return current

    def _realize_resources(
        self,
        *,
        actor_principal_id: str,
        preparation: (
            RunAttemptPreparationReceipt | RunAttemptHeartbeatAuthorityReceipt
        ),
        spec: PortableAttemptRuntimeSpec,
        store_id: str,
        holder_id: str,
        ttl_seconds: float,
    ) -> RealizedProcessRuntimeResources:
        attempt = preparation.attempt
        layered_scopes = tuple(
            scope
            for scope in spec.scopes
            if isinstance(scope.source, LayeredVolumeScopeSource)
        )
        projection_operation = run_attempt_projection_operation_id(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding_id=attempt.binding_id,
            logical_name=spec.projection_name,
        )
        projection: ManagedReadOnlyProjection | None = None
        volumes: list[tuple[str, ManagedEphemeralVolume]] = []
        pulse: _InitializationLeasePulse | None = None
        consumer_metadata = {
            "attempt_id": attempt.attempt_id,
            "binding_id": attempt.binding_id,
            "logical_name": spec.projection_name,
            "run_id": attempt.run_id,
            "schema": "optpilot.run-attempt-projection-consumer.v1",
        }
        try:
            try:
                projection = (
                    self._projection_service.recover_existing_private_read_only(
                        operation_id=projection_operation,
                        actor_principal_id=actor_principal_id,
                        store_id=store_id,
                        spec=spec.projection_spec,
                        holder_id=holder_id,
                        ttl_seconds=ttl_seconds,
                        consumer_kind="run-attempt",
                        consumer_metadata=consumer_metadata,
                    )
                )
            except RealmNotFound:
                projection = self._projection_service.project_read_only(
                    operation_id=projection_operation,
                    actor_principal_id=actor_principal_id,
                    store_id=store_id,
                    spec=spec.projection_spec,
                    holder_id=holder_id,
                    ttl_seconds=ttl_seconds,
                    consumer_kind="run-attempt",
                    consumer_metadata=consumer_metadata,
                    sharing_policy="private",
                )
            if layered_scopes:
                pulse = _InitializationLeasePulse(
                    projection=projection,
                    volumes=(),
                    operation_prefix=(
                        "process-binding.initialize/"
                        f"{attempt.run_id}/{attempt.attempt_id}/{attempt.binding_id}"
                    ),
                    ttl_seconds=ttl_seconds,
                )
                pulse.pulse(force=True)
                pulse.start()
            for requirement in spec.writable_volumes:
                volume_operation = run_attempt_volume_operation_id(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    binding_id=attempt.binding_id,
                    logical_name=requirement.name,
                )
                try:
                    volume = self._volume_service.recover_existing(
                        operation_id=volume_operation,
                        actor_principal_id=actor_principal_id,
                        parent_lease=preparation.attempt_lease,
                        holder_id=holder_id,
                        quota=requirement.quota,
                        quota_enforcement=requirement.quota_enforcement,
                        ttl_seconds=ttl_seconds,
                    )
                except RealmNotFound:
                    volume = self._volume_service.create(
                        operation_id=volume_operation,
                        actor_principal_id=actor_principal_id,
                        parent_lease=preparation.attempt_lease,
                        holder_id=holder_id,
                        quota=requirement.quota,
                        quota_enforcement=requirement.quota_enforcement,
                        ttl_seconds=ttl_seconds,
                    )
                volumes.append((requirement.name, volume))
                if pulse is not None:
                    pulse.add_volume(requirement.name, volume)
            if layered_scopes:
                projection.validate()
                _require_private_projection(projection)
                volume_by_name = dict(volumes)
                requirement_by_name = {
                    item.name: item for item in spec.writable_volumes
                }
                assert pulse is not None

                def authorize_publication() -> None:
                    pulse.pulse(force=True)
                    self._refresh_attempt_authority(
                        actor_principal_id=actor_principal_id,
                        expected=preparation,
                    )

                source_root = projection.root_path
                for scope in layered_scopes:
                    source = scope.source
                    assert isinstance(source, LayeredVolumeScopeSource)
                    try:
                        volume = volume_by_name[source.volume_name]
                        requirement = requirement_by_name[source.volume_name]
                    except KeyError as error:  # defensive persisted-plan check
                        raise RealmIntegrityError(
                            "Layered runtime scope names an unknown writable volume."
                        ) from error
                    plan = compile_local_layered_volume_plan(
                        source_root,
                        source.lower_layers,
                        requirement.quota,
                        progress=pulse,
                    )
                    pulse.pulse(force=True)
                    volume.initialize_layered(
                        source_root=source_root,
                        plan=plan,
                        initialization_identity=_layered_initialization_identity(
                            spec=spec,
                            projection=projection,
                            source=source,
                        ),
                        authorize_publication=authorize_publication,
                        progress=pulse,
                    )
                pulse.pulse(force=True)
                pulse.stop()
            resources = RealizedProcessRuntimeResources(
                projection=projection,
                volumes=tuple(volumes),
                resolved_scopes=resolve_process_runtime_scopes(
                    spec, projection, tuple(volumes)
                ),
            )
            _require_private_projection(resources.projection)
            return resources
        except BaseException as error:
            if pulse is not None:
                try:
                    pulse.stop(raise_error=False)
                except BaseException:
                    pass
            if projection is not None:
                for _logical_name, volume in volumes:
                    try:
                        volume._detach_without_release()
                    except BaseException:
                        pass
                error.add_note(
                    "Partially realized resources were retained for typed "
                    "cross-actor recovery and TTL cleanup."
                )
            raise

    def _reattach_resources(
        self,
        *,
        actor_principal_id: str,
        authority: RunAttemptBindingAuthorityReceipt,
    ) -> RealizedProcessRuntimeResources:
        binding = authority.binding
        spec = binding.portable_spec
        if len(binding.projections) != 1:
            raise RealmIntegrityError(
                "Native process recovery requires one projection handle."
            )
        projection_handle = binding.projections[0]
        if projection_handle.logical_name != spec.projection_name:
            raise RealmIntegrityError(
                "Durable projection handle differs from the portable plan."
            )
        expected_projection_operation = run_attempt_projection_operation_id(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            binding_id=binding.binding_id,
            logical_name=spec.projection_name,
        )
        holder_id = run_attempt_resource_holder_id(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            binding_id=binding.binding_id,
        )
        projection: ManagedReadOnlyProjection | None = None
        pulse: _InitializationLeasePulse | None = None
        volumes: list[tuple[str, ManagedEphemeralVolume]] = []
        try:
            projection = (
                self._projection_service.reattach_private_read_only_consumer(
                    actor_principal_id=actor_principal_id,
                    expected_operation_id=expected_projection_operation,
                    realization_id=projection_handle.realization_id,
                    consumer_id=projection_handle.consumer_id,
                    consumer_holder_id=holder_id,
                    consumer_fencing_token=projection_handle.consumer_fencing_token,
                )
            )
            if _projection_handle(spec, projection) != projection_handle:
                raise RealmIntegrityError(
                    "Reattached projection differs from the durable execution "
                    "handle."
                )
            layered_scopes = tuple(
                scope
                for scope in spec.scopes
                if isinstance(scope.source, LayeredVolumeScopeSource)
            )
            volume_handles = {
                item.logical_name: item for item in binding.writable_volumes
            }
            if len(volume_handles) != len(binding.writable_volumes) or set(
                volume_handles
            ) != {item.name for item in spec.writable_volumes}:
                raise RealmIntegrityError(
                    "Durable volume handles differ from the portable plan."
                )
            if layered_scopes:
                pulse = _InitializationLeasePulse(
                    projection=projection,
                    volumes=(),
                    operation_prefix=(
                        "process-binding.recover-initialization/"
                        f"{binding.run_id}/{binding.attempt_id}/"
                        f"{binding.binding_id}"
                    ),
                    ttl_seconds=binding.resource_ttl_seconds,
                )
                pulse.pulse(force=True)
                pulse.start()
            for requirement in spec.writable_volumes:
                handle = volume_handles[requirement.name]
                volume = self._volume_service.reattach(
                    actor_principal_id=actor_principal_id,
                    volume_id=handle.volume_id,
                    holder_id=holder_id,
                    fencing_token=handle.usage_fencing_token,
                )
                if _volume_handle(requirement.name, volume) != handle:
                    volume._detach_without_release()
                    raise RealmIntegrityError(
                        "Reattached volume differs from its durable execution handle."
                    )
                volumes.append((requirement.name, volume))
                if pulse is not None:
                    pulse.add_volume(requirement.name, volume)
            if layered_scopes:
                assert pulse is not None
                volume_by_name = dict(volumes)
                requirement_by_name = {
                    item.name: item for item in spec.writable_volumes
                }
                source_root = projection.root_path
                for scope in layered_scopes:
                    source = scope.source
                    assert isinstance(source, LayeredVolumeScopeSource)
                    try:
                        volume = volume_by_name[source.volume_name]
                        requirement = requirement_by_name[source.volume_name]
                    except KeyError as error:
                        raise RealmIntegrityError(
                            "Layered runtime scope names an unknown writable volume."
                        ) from error
                    plan = compile_local_layered_volume_plan(
                        source_root,
                        source.lower_layers,
                        requirement.quota,
                        progress=pulse,
                    )
                    volume.require_layered_initialization(
                        plan=plan,
                        initialization_identity=_layered_initialization_identity(
                            spec=spec,
                            projection=projection,
                            source=source,
                        ),
                        progress=pulse,
                    )
                pulse.pulse(force=True)
                pulse.stop()
            return RealizedProcessRuntimeResources(
                projection=projection,
                volumes=tuple(volumes),
                resolved_scopes=resolve_process_runtime_scopes(
                    spec, projection, tuple(volumes)
                ),
            )
        except BaseException:
            if pulse is not None:
                try:
                    pulse.stop(raise_error=False)
                except BaseException:
                    pass
            for _logical_name, volume in volumes:
                try:
                    volume._detach_without_release()
                except BaseException:
                    pass
            if projection is not None:
                try:
                    projection._detach_without_release()
                except BaseException:
                    pass
            raise

def validate_process_runtime_resources(
    *,
    resources: RealizedProcessRuntimeResources,
    projection_name: str,
) -> None:
    failures: list[ProcessExecutionResourceFailure] = []
    try:
        resources.projection.validate()
    except BaseException as error:
        failures.append(
            ProcessExecutionResourceFailure(
                "validate", "projection", projection_name, error
            )
        )
    for logical_name, volume in resources.volumes:
        try:
            volume.validate()
        except BaseException as error:
            failures.append(
                ProcessExecutionResourceFailure(
                    "validate", "volume", logical_name, error
                )
            )
    if failures:
        raise ProcessExecutionResourceError(
            "Process execution binding validation failed", failures
        )


def heartbeat_process_runtime_resources(
    *,
    resources: RealizedProcessRuntimeResources,
    projection_name: str,
    operation_id: str,
    ttl_seconds: float,
) -> None:
    if not isinstance(operation_id, str) or not operation_id or "\x00" in operation_id:
        raise ValueError("operation_id must be nonempty text.")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(float(ttl_seconds))
        or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be a positive finite number.")
    failures: list[ProcessExecutionResourceFailure] = []
    try:
        resources.projection.heartbeat(
            operation_id=f"{operation_id}/projection",
            ttl_seconds=float(ttl_seconds),
        )
    except BaseException as error:
        failures.append(
            ProcessExecutionResourceFailure(
                "heartbeat", "projection", projection_name, error
            )
        )
    for logical_name, volume in resources.volumes:
        try:
            volume.heartbeat(
                operation_id=f"{operation_id}/volume/{logical_name}",
                ttl_seconds=float(ttl_seconds),
            )
        except BaseException as error:
            failures.append(
                ProcessExecutionResourceFailure(
                    "heartbeat", "volume", logical_name, error
                )
            )
    if failures:
        raise ProcessExecutionResourceError(
            "Process execution binding heartbeat failed", failures
        )


def _projection_handle(
    spec: PortableAttemptRuntimeSpec,
    projection: ManagedReadOnlyProjection,
) -> ExecutionProjectionHandle:
    return ExecutionProjectionHandle(
        logical_name=spec.projection_name,
        provider_kind=projection.realization.provider_kind,
        realization_id=projection.realization.realization_id,
        consumer_id=projection.consumer_id,
        consumer_lease_id=projection.consumer_lease.lease_id,
        consumer_fencing_token=projection.consumer_lease.fencing_token,
    )


def _volume_handle(
    logical_name: str,
    volume: ManagedEphemeralVolume,
) -> ExecutionVolumeHandle:
    return ExecutionVolumeHandle(
        logical_name=logical_name,
        provider_kind=volume.record.provider_kind,
        volume_id=volume.record.volume_id,
        usage_lease_id=volume.lease.lease_id,
        usage_fencing_token=volume.lease.fencing_token,
    )


def _layered_initialization_identity(
    *,
    spec: PortableAttemptRuntimeSpec,
    projection: ManagedReadOnlyProjection,
    source: LayeredVolumeScopeSource,
) -> dict[str, object]:
    return {
        "lower_layers_digest": request_digest(
            {
                "format": "optpilot.layered-volume-lowers.v1",
                "layers": [item.to_dict() for item in source.lower_layers],
            }
        ),
        "portable_spec_digest": spec.digest,
        "projection_consumer_fencing_token": (
            projection.consumer_lease.fencing_token
        ),
        "projection_consumer_id": projection.consumer_id,
        "projection_plan_digest": projection.realization.plan_digest,
        "projection_realization_id": projection.realization.realization_id,
        "projection_spec_digest": projection.realization.spec_digest,
    }


def resolve_process_runtime_scopes(
    spec: PortableAttemptRuntimeSpec,
    projection: ManagedReadOnlyProjection,
    volumes: Tuple[tuple[str, ManagedEphemeralVolume], ...],
) -> Tuple[ResolvedRuntimeScope, ...]:
    volume_paths = {name: volume.path for name, volume in volumes}
    projection_root = projection.root_path
    resolved: list[ResolvedRuntimeScope] = []
    for scope in spec.scopes:
        source = scope.source
        if isinstance(source, ProjectionScopeSource):
            if source.projection_name != spec.projection_name:
                raise RealmIntegrityError(
                    "Runtime scope names an unknown projection realization."
                )
            host_path = projection_root
            if source.relative_path != ".":
                host_path = host_path.joinpath(*source.relative_path.split("/"))
        elif isinstance(source, (VolumeScopeSource, LayeredVolumeScopeSource)):
            try:
                host_path = volume_paths[source.volume_name]
            except KeyError as error:
                raise RealmIntegrityError(
                    "Runtime scope names an unknown writable volume."
                ) from error
        else:  # pragma: no cover - PortableRuntimeScope validates its union
            raise RealmIntegrityError("Runtime scope source is unsupported.")
        resolved.append(ResolvedRuntimeScope(scope, host_path))
    return tuple(resolved)


def _require_private_projection(projection: ManagedReadOnlyProjection) -> None:
    sharing = projection.realization.availability_resolution.get(
        "realization_sharing"
    )
    if not isinstance(sharing, Mapping) or sharing.get("policy") != "private":
        raise RealmIntegrityError(
            "Native advisory execution requires an operation-private projection."
        )


def _same_lease_authority(current: LeaseRecord, expected: LeaseRecord) -> bool:
    """Compare immutable lease authority while permitting heartbeat movement."""

    return (
        current.lease_id == expected.lease_id
        and current.owner_id == expected.owner_id
        and current.parent_lease_id == expected.parent_lease_id
        and current.lease_kind == expected.lease_kind
        and current.audience == expected.audience
        and current.holder_id == expected.holder_id
        and current.scope_key == expected.scope_key
        and current.fencing_token == expected.fencing_token
        and dict(current.metadata) == dict(expected.metadata)
    )


def _candidate_authority_input(
    authority: RunAttemptHeartbeatAuthorityReceipt,
) -> tuple[CandidateRuntimeInput | None, tuple[OwnerMembership, ...]]:
    """Extract the admitted candidate content from the fenced attempt read.

    Candidate identity and placement are read from the same fenced ledger
    transaction as the attempt.  Provider code never discovers candidate
    content through an unfenced owner-membership scan.
    """

    candidate_format = authority.attempt.evaluation_spec.candidate_format
    candidate = authority.candidate
    bindings = authority.candidate_content_bindings
    if any(not isinstance(item, OwnerMembership) for item in bindings):
        raise RealmIntegrityError("Attempt candidate bindings are malformed.")
    if candidate_format == "parameters":
        if bindings:
            raise RealmIntegrityError(
                "Parameter attempt authority unexpectedly contains candidate content."
            )
        return None, ()
    if candidate_format != "files":
        raise RealmConflict("Attempt candidate format is unsupported by this provider.")
    try:
        envelope = candidate.admission.envelope
        candidate_input = CandidateRuntimeInput.from_envelope(envelope)
    except (AttributeError, TypeError, ValueError) as error:
        raise RealmIntegrityError(
            "Attempt candidate authority is malformed."
        ) from error
    evaluation = authority.attempt.evaluation_spec
    if (
        candidate.admission.candidate_id != evaluation.candidate_id
        or str(candidate_input.candidate_ref) != evaluation.candidate_ref
        or candidate_input.candidate_format != evaluation.candidate_format
        or canonical_json_bytes(thaw_json(envelope.spec))
        != canonical_json_bytes(thaw_json(evaluation.candidate["spec"]))
        or {item.content_ref for item in bindings}
        != set(envelope.content_refs)
        or any(item.role != RUN_CANDIDATE_ROLE for item in bindings)
    ):
        raise RealmIntegrityError(
            "Attempt candidate authority differs from its evaluation spec."
        )
    return candidate_input, bindings


def _same_owner_change_authority(
    current: OwnerChange, expected: OwnerChange
) -> bool:
    """Compare immutable capture identity while permitting heartbeat expiry."""

    return (
        current.change_id == expected.change_id
        and current.owner_id == expected.owner_id
        and current.base_owner_revision == expected.base_owner_revision
        and current.retention_lease_id == expected.retention_lease_id
        and current.state == expected.state
    )


def _require_durable_private_projection(
    realization: ProjectionRealizationRecord,
    *,
    handle: ExecutionProjectionHandle,
    projection_root_id: str,
    coordinate_digest: str,
    spec_digest: str,
) -> None:
    sharing = realization.availability_resolution.get("realization_sharing")
    if (
        realization.realization_id != handle.realization_id
        or realization.projection_root_id != projection_root_id
        or realization.provider_kind != handle.provider_kind
        or realization.spec_digest != spec_digest
        or not isinstance(sharing, Mapping)
        or set(sharing) != {"policy", "operation_coordinate_digest"}
        or sharing.get("policy") != "private"
        or sharing.get("operation_coordinate_digest") != coordinate_digest
    ):
        raise RealmIntegrityError(
            "Durable projection differs from the exact private runtime handle."
        )


def release_process_runtime_resources(
    *,
    projection_service: RealmProjectionService,
    resources: RealizedProcessRuntimeResources,
) -> None:
    """Attempt every cleanup action in safe provider order."""

    failures: list[ProcessExecutionResourceFailure] = []
    for logical_name, volume in resources.volumes:
        try:
            volume.close()
        except BaseException as error:
            failures.append(
                ProcessExecutionResourceFailure(
                    "release", "volume", logical_name, error
                )
            )
    try:
        _require_private_projection(resources.projection)
        projection_service.retire_private_projection(resources.projection)
    except BaseException as error:
        failures.append(
            ProcessExecutionResourceFailure(
                "release", "private-projection", "environment-inputs", error
            )
        )
    if failures:
        raise ProcessExecutionResourceError(
            "Process execution resource release was incomplete", failures
        )


__all__ = [
    "ManagedProcessExecutionBinding",
    "PreparedProcessExecutionBinding",
    "ProcessExecutionResourceError",
    "ProcessExecutionResourceFailure",
    "RealizedProcessRuntimeResources",
    "RealmProcessExecutionBinder",
    "ResolvedRuntimeScope",
    "heartbeat_process_runtime_resources",
    "release_process_runtime_resources",
    "resolve_process_runtime_scopes",
    "validate_process_runtime_resources",
]
