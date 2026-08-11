"""Durable, ledger-authorized local projection orchestration.

A projection realization is a reusable physical cache.  Runtime callers never
own or copy that cache: each caller receives a narrow consumer lease and an
independently opened descriptor view of the exact published namespace.  The
Realm ledger coordinates realization, publication, attachment, and cleanup
across processes; the in-process maps below are only replay optimizations.
"""

from __future__ import annotations

import os
import math
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Optional, Tuple

from . import content as content_module
from .content import LocalContentStore
from .errors import (
    ContentCorrupt,
    RealmAuthorizationError,
    RealmConflict,
    RealmError,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
    RealmStorageIdentityChanged,
)
from .leases import LeaseRecord
from .ledger import RealmLedger
from .manifests import TreeManifest
from .owners import OwnerPermission
from .projection import (
    ProjectionContentCapability,
    ProjectionLease,
    ProjectionSpec,
    ProjectionTarget,
    TreeMapping,
    VerifiedCopyProjectionProvider,
)
from .projection_namespace import (
    AttachedProjectionNamespace,
    ProjectionNamespaceClaim,
    ProjectionNamespaceIdentity,
    ProjectionRootBinding,
    attach_projection_namespace,
    cleanup_projection_namespace,
    complete_projection_cleanup_namespace,
    create_projection_wrapper,
    find_projection_wrapper_identity,
    observe_ready_projection_namespace_identity,
    prepare_projection_root,
    record_projection_tree_identity,
    validate_projection_root,
)
from .projection_records import (
    ProjectionClaimReceipt,
    ProjectionConsumerRecord,
    ProjectionConsumerReceipt,
    ProjectionRealizationRecord,
    ProjectionRealizationState,
)
from .refs import BlobRef, SnapshotRef, request_digest
from .selections import SelectionEligibility, SelectionRef
from .workspaces import WorkspaceRecord, WorkspaceRevision


_AVAILABILITY_FORMAT = "optpilot.projection-availability.v1"
_REQUEST_FORMAT = "optpilot.projection-request.v1"
_CONSUMER_REQUEST_FORMAT = "optpilot.projection-consumer-request.v1"
_PRIVATE_OPERATION_COORDINATE_FORMAT = (
    "optpilot.projection-private-operation-coordinate.v1"
)
_REALIZATION_SHARING_POLICIES = frozenset({"shared", "private"})
_SELECTION_METADATA_KEY = "selection_ref"
# Consumer TTL is a delivered-view lifetime, not a filesystem build deadline.
# Keep an actively heartbeating materializer comfortably alive even when the
# requested view is deliberately very short lived.  The ledger drops this
# grace atomically when the first consumer is acquired.
_ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS = 5.0
# A build proves liveness through its heartbeat cadence, not through the
# delivered-view TTL.  Cap the build-phase owner/builder term so a builder
# that dies without releasing its claim (killed process, dropped HTTP handler
# thread) becomes fenceable within minutes even when the requested consumer
# view is very long lived; the first consumer acquisition atomically
# re-extends the owner to the full requested view lifetime.
_ACTIVE_MATERIALIZATION_LEASE_MAX_SECONDS = 120.0
# How long a reader waits on someone else's in-flight build before probing
# for a dead builder, and how often it re-probes.  Probes are fenced by the
# ledger: they can only retire a build whose owner term has already lapsed,
# so an actively heartbeating builder is never disturbed.
_STALE_BUILD_TAKEOVER_GRACE_SECONDS = 1.0
_STALE_BUILD_TAKEOVER_INTERVAL_SECONDS = 1.0


class SelectionProjectionUnavailable(RealmConflict):
    """A valid selection cannot currently produce a read-only tree view."""

    def __init__(self, eligibility: SelectionEligibility) -> None:
        if not isinstance(eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if eligibility.eligible:
            raise ValueError("an eligible selection is available for projection.")
        self.eligibility = eligibility
        super().__init__(f"{eligibility.code}: {eligibility.reason}")


class _LedgerProjectionContentCapability(ProjectionContentCapability):
    """Exact content capability held only while one realization is built."""

    def __init__(
        self,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        store: LocalContentStore,
        realization_id: str,
        owner_lease: LeaseRecord,
        snapshot_roots: Tuple[SnapshotRef, ...],
        revalidation_interval_seconds: float = 0.0,
    ) -> None:
        self._ledger = ledger
        self._actor_principal_id = actor_principal_id
        self._store = store
        self._realization_id = realization_id
        self._lease = owner_lease
        self._snapshot_roots = tuple(sorted(set(snapshot_roots), key=str))
        self._allowed_blobs: set[BlobRef] = set()
        self._manifests: dict[SnapshotRef, TreeManifest] = {}
        self._authorized = False
        self._revalidation_interval = max(0.0, float(revalidation_interval_seconds))
        self._validated_at: float | None = None
        self._lock = threading.RLock()

    @property
    def owner_id(self) -> str:
        return self._lease.owner_id

    @property
    def lease_id(self) -> str:
        return self._lease.lease_id

    @property
    def fencing_token(self) -> int:
        return self._lease.fencing_token

    def _validate_exact_closure(self) -> LeaseRecord:
        return self._ledger.validate_projection_lease(
            actor_principal_id=self._actor_principal_id,
            owner_id=self._lease.owner_id,
            lease_id=self._lease.lease_id,
            holder_id=self._lease.holder_id,
            fencing_token=self._lease.fencing_token,
            store_id=self._store.store_id,
            backend_kind=self._store.BACKEND_KIND,
            root_marker=self._store.root_marker,
            snapshot_roots=self._snapshot_roots,
        )

    def authorize_exact_closure(self) -> None:
        self._store._assert_root_identity()
        self._validate_exact_closure()
        with self._lock:
            self._authorized = True
            self._validated_at = time.monotonic()

    def assert_current(self) -> None:
        self._store._assert_root_identity()
        with self._lock:
            if not self._authorized:
                raise RealmAuthorizationError("Projection content is unavailable.")
            # The ledger round-trip is the dominant per-file materialization
            # cost, while fencing at publish is enforced transactionally by
            # publish_projection_ready and liveness by the builder heartbeat.
            # Between those anchors a bounded-staleness check suffices here.
            if (
                self._revalidation_interval > 0.0
                and self._validated_at is not None
                and time.monotonic() - self._validated_at
                < self._revalidation_interval
            ):
                return
        current = self._ledger.validate_projection_owner_lease(
            actor_principal_id=self._actor_principal_id,
            realization_id=self._realization_id,
            lease_id=self._lease.lease_id,
            holder_id=self._lease.holder_id,
            fencing_token=self._lease.fencing_token,
        )
        if (
            current.owner_id != self._lease.owner_id
            or current.lease_kind != "projection-owner"
            or current.audience != "runtime"
        ):
            raise RealmNotFound("Entity not found.")
        with self._lock:
            self._validated_at = time.monotonic()

    def load_tree(self, snapshot_ref: SnapshotRef) -> TreeManifest:
        if not isinstance(snapshot_ref, SnapshotRef):
            raise TypeError("snapshot_ref must be a SnapshotRef.")
        self.assert_current()
        if snapshot_ref not in self._snapshot_roots:
            raise RealmAuthorizationError("Projection content is unavailable.")
        with self._lock:
            manifest = self._manifests.get(snapshot_ref)
            if manifest is None:
                manifest = self._store.verify_tree(snapshot_ref, verify_children=True)
                self._manifests[snapshot_ref] = manifest
                self._allowed_blobs.update(
                    entry.blob_ref
                    for entry in manifest.entries
                    if entry.kind == "file" and entry.blob_ref is not None
                )
        self.assert_current()
        return manifest

    @contextmanager
    def open_blob(self, blob_ref: BlobRef) -> Iterator[BinaryIO]:
        if not isinstance(blob_ref, BlobRef):
            raise TypeError("blob_ref must be a BlobRef.")
        self.assert_current()
        with self._lock:
            if blob_ref not in self._allowed_blobs:
                raise RealmAuthorizationError("Projection content is unavailable.")
        object_fd = self._store._open_live_object_fd(blob_ref)
        data_fd: Optional[int] = None
        try:
            content_module._require_immutable_object_directory(object_fd, blob_ref)
            manifest = content_module._verify_blob_fd(object_fd, blob_ref)
            data_fd = content_module._open_immutable_regular_child(
                object_fd,
                "data",
                label=f"projection blob {blob_ref}",
            )
            if os.fstat(data_fd).st_size != manifest.size:
                raise ContentCorrupt(f"Projection blob size changed: {blob_ref}.")
        finally:
            os.close(object_fd)
        assert data_fd is not None
        stream = os.fdopen(data_fd, "rb", closefd=True)
        try:
            yield stream
        finally:
            stream.close()


class _BuilderHeartbeat:
    """Renew one typed builder and its owner while materialization runs."""

    def __init__(
        self,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        realization_id: str,
        claim: ProjectionClaimReceipt,
        operation_prefix: str,
        ttl_seconds: float,
    ) -> None:
        self._ledger = ledger
        self._actor_principal_id = actor_principal_id
        self._realization_id = realization_id
        self._owner = claim.owner_lease
        self._builder = claim.builder_lease
        # Heartbeats are renewal attempts, not semantic replays across worker
        # incarnations.  A reclaimed cleanup/materialization term may reuse a
        # stable lifecycle prefix with new holder/fence credentials; scoping
        # every heartbeat object prevents it from replaying a predecessor's
        # cached `/initial` or numbered receipt.
        self._operation_prefix = f"{operation_prefix}/{uuid.uuid4().hex}"
        self._ttl_seconds = float(ttl_seconds)
        self._interval = max(0.001, min(self._ttl_seconds / 3.0, 30.0))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"projection-builder-heartbeat-{realization_id[-8:]}",
            daemon=True,
        )

    @property
    def owner_lease(self) -> LeaseRecord:
        with self._lock:
            return self._owner

    @property
    def builder_lease(self) -> LeaseRecord:
        with self._lock:
            return self._builder

    def start(self) -> None:
        # Renew synchronously before yielding materialization to filesystem
        # work. With a deliberately short TTL, waiting one heartbeat interval
        # before the first renewal lets thread scheduling alone consume the
        # entire builder term before the namespace claim is recorded.
        receipt = self._ledger.heartbeat_projection_builder(
            operation_id=f"{self._operation_prefix}/initial",
            actor_principal_id=self._actor_principal_id,
            realization_id=self._realization_id,
            owner_holder_id=self._owner.holder_id,
            owner_fencing_token=self._owner.fencing_token,
            builder_holder_id=self._builder.holder_id,
            builder_fencing_token=self._builder.fencing_token,
            ttl_seconds=self._ttl_seconds,
        )
        with self._lock:
            self._owner = receipt.owner_lease
            self._builder = receipt.child_lease
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RealmConflict("Projection builder heartbeat failed.") from error

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(self._interval):
            index += 1
            try:
                receipt = self._ledger.heartbeat_projection_builder(
                    operation_id=f"{self._operation_prefix}/{index}",
                    actor_principal_id=self._actor_principal_id,
                    realization_id=self._realization_id,
                    owner_holder_id=self._owner.holder_id,
                    owner_fencing_token=self._owner.fencing_token,
                    builder_holder_id=self._builder.holder_id,
                    builder_fencing_token=self._builder.fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
            except BaseException as error:
                with self._lock:
                    self._error = error
                return
            with self._lock:
                self._owner = receipt.owner_lease
                self._builder = receipt.child_lease


class ManagedReadOnlyProjection:
    """A narrow consumer view over one reusable durable realization."""

    def __init__(
        self,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        receipt: ProjectionConsumerReceipt,
        namespace: AttachedProjectionNamespace,
        release_operation_id: str,
        cache_key: str,
        on_closed: Callable[[str], None],
        on_integrity_failure: Callable[[str, BaseException], None],
    ) -> None:
        self._ledger = ledger
        self._actor_principal_id = actor_principal_id
        self._receipt = receipt
        self._namespace = namespace
        self._release_operation_id = release_operation_id
        self._cache_key = cache_key
        self._on_closed = on_closed
        self._on_integrity_failure = on_integrity_failure
        self._closed = False
        self._release_complete = False
        self._lock = threading.RLock()

    @property
    def realization(self) -> ProjectionRealizationRecord:
        return self._receipt.realization

    @property
    def consumer_id(self) -> str:
        return self._receipt.consumer.consumer_id

    @property
    def consumer_lease(self) -> LeaseRecord:
        return self._receipt.consumer_lease

    @property
    def authority_lease(self) -> LeaseRecord:
        """Compatibility alias for callers that only need the narrow lease."""

        return self.consumer_lease

    @property
    def root_path(self) -> Path:
        self.validate()
        return self._namespace.root_path

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def validate(self) -> None:
        with self._lock:
            if self._closed:
                raise RealmExpired("Projection consumer is closed.")
            self._receipt = self._ledger.validate_projection_consumer(
                actor_principal_id=self._actor_principal_id,
                realization_id=self.realization.realization_id,
                consumer_id=self.consumer_id,
                consumer_holder_id=self.consumer_lease.holder_id,
                consumer_fencing_token=self.consumer_lease.fencing_token,
            )
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._handle_namespace_integrity_failure(error)
                raise

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> LeaseRecord:
        with self._lock:
            if self._closed:
                raise RealmNotFound("Entity not found.")
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._handle_namespace_integrity_failure(error)
                raise
            heartbeat = self._ledger.heartbeat_projection_consumer(
                operation_id=operation_id,
                actor_principal_id=self._actor_principal_id,
                realization_id=self.realization.realization_id,
                consumer_id=self.consumer_id,
                consumer_holder_id=self.consumer_lease.holder_id,
                consumer_fencing_token=self.consumer_lease.fencing_token,
                ttl_seconds=ttl_seconds,
            )
            # Operation replay recovers a committed receipt, not proof that the
            # consumer authority remains current at this later instant.
            self._receipt = self._ledger.validate_projection_consumer(
                actor_principal_id=self._actor_principal_id,
                realization_id=heartbeat.realization.realization_id,
                consumer_id=heartbeat.consumer.consumer_id,
                consumer_holder_id=heartbeat.consumer_lease.holder_id,
                consumer_fencing_token=heartbeat.consumer_lease.fencing_token,
            )
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._handle_namespace_integrity_failure(error)
                raise
            return self.consumer_lease

    def heartbeat_initialization(
        self, *, operation_id: str, ttl_seconds: float
    ) -> LeaseRecord:
        """Renew a private initialization view with one typed ledger write.

        The initialization coordinator supplies a fresh operation id for each
        call.  The typed heartbeat atomically validates and renews the consumer
        and its owner, so a second ledger validation would only shorten the
        useful renewal window on high-latency filesystems.
        """

        with self._lock:
            if self._closed:
                raise RealmNotFound("Entity not found.")
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._handle_namespace_integrity_failure(error)
                raise
            heartbeat = self._ledger.heartbeat_projection_consumer(
                operation_id=operation_id,
                actor_principal_id=self._actor_principal_id,
                realization_id=self.realization.realization_id,
                consumer_id=self.consumer_id,
                consumer_holder_id=self.consumer_lease.holder_id,
                consumer_fencing_token=self.consumer_lease.fencing_token,
                ttl_seconds=ttl_seconds,
            )
            if heartbeat.consumer_lease.expires_at <= time.time():
                raise RealmExpired(
                    "Projection heartbeat returned an expired lease."
                )
            self._receipt = heartbeat
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._handle_namespace_integrity_failure(error)
                raise
            return self.consumer_lease

    def portable_record(self) -> dict[str, object]:
        realization = self.realization
        return {
            "format": "optpilot.read-only-projection.v1",
            "spec": _thaw_mapping(realization.spec),
            "spec_digest": realization.spec_digest,
            "plan_digest": realization.plan_digest,
            "copied_logical_bytes": realization.copied_logical_bytes,
            "copied_file_count": realization.copied_file_count,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._release_complete:
                try:
                    self._ledger.release_projection_consumer(
                        operation_id=self._release_operation_id,
                        actor_principal_id=self._actor_principal_id,
                        realization_id=self.realization.realization_id,
                        consumer_id=self.consumer_id,
                        consumer_holder_id=self.consumer_lease.holder_id,
                        consumer_fencing_token=self.consumer_lease.fencing_token,
                    )
                except (RealmConflict, RealmExpired, RealmNotFound):
                    # Revoked/expired authority is already unusable.
                    pass
                except BaseException:
                    # Keep the attachment and cache entry retryable.  If the
                    # operation committed and only its response was lost, the
                    # exact operation replay resolves it on the next close.
                    raise
                self._release_complete = True
            self._namespace.close()
            self._closed = True
            self._on_closed(self._cache_key)

    def _detach_after_private_retirement(self) -> None:
        """Detach after the ledger atomically retired this exact consumer."""

        with self._lock:
            if self._closed:
                return
            self._release_complete = True
            self._namespace.close()
            self._closed = True
            self._on_closed(self._cache_key)

    def _detach_without_release(self) -> None:
        """Close only this process's namespace attachment during recovery."""

        with self._lock:
            if self._closed:
                return
            self._namespace.close()
            self._closed = True
            self._on_closed(self._cache_key)

    def _handle_namespace_integrity_failure(
        self, error: RealmIntegrityError
    ) -> None:
        self._on_integrity_failure(self.realization.realization_id, error)
        self._on_closed(self._cache_key)

    def __enter__(self) -> "ManagedReadOnlyProjection":
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class WorkspaceReadOnlyProjection:
    workspace: WorkspaceRecord
    revision: WorkspaceRevision
    projection: ManagedReadOnlyProjection


@dataclass(frozen=True)
class ProjectionReconcileReceipt:
    realization: ProjectionRealizationRecord
    namespace_removed: bool
    already_complete: bool = False


@dataclass(frozen=True)
class ProjectionReconcileOutcome:
    realization_id: str
    receipt: ProjectionReconcileReceipt | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        succeeded = self.receipt is not None
        failed = self.error_type is not None and self.error_message is not None
        if succeeded == failed:
            raise ValueError(
                "projection reconcile outcome requires one success or failure"
            )

    @property
    def ok(self) -> bool:
        return self.receipt is not None


class RealmProjectionService:
    """Resolve immutable specs into provider-scoped realizations and consumers."""

    def __init__(
        self,
        ledger: RealmLedger,
        *,
        local_stores: Mapping[str, LocalContentStore],
        projection_root: Path,
        coordination_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        stores = dict(local_stores)
        for store_id, store in stores.items():
            if not isinstance(store, LocalContentStore) or store_id != store.store_id:
                raise ValueError(
                    "local_stores must be keyed by each LocalContentStore store_id."
                )
            ledger.validate_store_binding(
                store_id=store_id,
                backend_kind=store.BACKEND_KIND,
                root_marker=store.root_marker,
            )
        if (
            isinstance(coordination_timeout_seconds, bool)
            or not isinstance(coordination_timeout_seconds, (int, float))
            or coordination_timeout_seconds <= 0
        ):
            raise ValueError("coordination_timeout_seconds must be positive.")
        self._ledger = ledger
        self._stores = stores
        self._provider = VerifiedCopyProjectionProvider()
        self._root_binding = prepare_projection_root(
            projection_root, realm_id=ledger.realm_id
        )
        maintenance_digest = request_digest(
            {
                "format": "optpilot.projection-maintenance-principal.v1",
                "realm_id": ledger.realm_id,
                "projection_root_id": self._root_binding.projection_root_id,
            }
        )
        self._maintenance_principal_id = (
            f"projection-maintainer-{maintenance_digest[:40]}"
        )
        self._coordination_timeout_seconds = float(coordination_timeout_seconds)
        self._instance_id = uuid.uuid4().hex
        self._active: dict[str, ManagedReadOnlyProjection] = {}
        self._building: dict[str, Future[ManagedReadOnlyProjection]] = {}
        self._lock = threading.RLock()

    @property
    def root_binding(self) -> ProjectionRootBinding:
        return self._root_binding

    @property
    def ledger(self) -> RealmLedger:
        """Return the exact Realm authority used by this service."""

        return self._ledger

    @property
    def available_store_ids(self) -> Tuple[str, ...]:
        """Return the deterministic ids of physically attached local stores."""

        return tuple(sorted(self._stores))

    @property
    def maintenance_principal_id(self) -> str:
        return self._maintenance_principal_id

    @property
    def provider_kind(self) -> str:
        """Return the local provider identity without exposing host paths."""

        return self._provider.PROVIDER_KIND

    def project_read_only(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store_id: str,
        spec: ProjectionSpec,
        holder_id: str,
        ttl_seconds: float = 300,
        consumer_kind: str = "read-only-runtime",
        consumer_metadata: Optional[Mapping[str, Any]] = None,
        sharing_policy: str = "shared",
    ) -> ManagedReadOnlyProjection:
        """Project content with shared or operation-private physical reuse.

        ``sharing_policy`` is an internal provider decision, not portable
        projection semantics.  Native advisory consumers use ``private`` so
        another public operation cannot receive the same writable-by-UID copy;
        enforcing read-only providers retain the default shared cache.
        """

        _required_service_text(operation_id, "operation_id")
        _required_service_text(actor_principal_id, "actor_principal_id")
        _required_service_text(holder_id, "holder_id")
        _required_service_text(consumer_kind, "consumer_kind", max_bytes=128)
        sharing_policy = _realization_sharing_policy(sharing_policy)
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        if not isinstance(spec, ProjectionSpec):
            raise TypeError("spec must be a ProjectionSpec.")
        if not spec.mappings:
            raise ValueError("A read-only content projection requires at least one mapping.")
        try:
            store = self._stores[store_id]
        except (KeyError, TypeError) as error:
            raise RealmNotFound("Entity not found.") from error
        metadata = dict(consumer_metadata or {})
        roots = tuple(sorted({item.snapshot_ref for item in spec.mappings}, key=str))
        private_operation_digest = None
        if sharing_policy == "private":
            private_operation_digest = request_digest(
                {
                    "format": _PRIVATE_OPERATION_COORDINATE_FORMAT,
                    "realm_id": self._ledger.realm_id,
                    "operation_id": operation_id,
                }
            )
        resolution = self._availability_resolution(
            store,
            roots,
            private_operation_digest=private_operation_digest,
        )
        semantic_digest = self._semantic_request_digest(spec, resolution)
        cache_key = request_digest(
            {
                "format": _CONSUMER_REQUEST_FORMAT,
                "operation_id": operation_id,
                "actor_principal_id": actor_principal_id,
                "projection_root_id": self._root_binding.projection_root_id,
                "owner_id": spec.owner_id,
                "request_digest": semantic_digest,
                "consumer_holder_id": holder_id,
                "consumer_ttl_seconds": ttl_seconds,
                "consumer_kind": consumer_kind,
                "metadata": metadata,
            }
        )
        with self._lock:
            existing = self._active.get(cache_key)
            if existing is None:
                future = self._building.get(cache_key)
                leader = future is None
                if future is None:
                    future = Future()
                    self._building[cache_key] = future
            else:
                future = None
                leader = False
        if existing is not None:
            existing.validate()
            return existing
        assert future is not None
        if not leader:
            projection = future.result()
            projection.validate()
            return projection
        try:
            projection = self._project_once(
                cache_key=cache_key,
                operation_id=operation_id,
                actor_principal_id=actor_principal_id,
                store=store,
                spec=spec,
                roots=roots,
                resolution=resolution,
                semantic_digest=semantic_digest,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                consumer_kind=consumer_kind,
                consumer_metadata=metadata,
            )
        except BaseException as error:
            with self._lock:
                self._building.pop(cache_key, None)
                future.set_exception(error)
            raise
        with self._lock:
            self._active[cache_key] = projection
            self._building.pop(cache_key, None)
            future.set_result(projection)
        return projection

    def recover_existing_private_read_only(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store_id: str,
        spec: ProjectionSpec,
        holder_id: str,
        ttl_seconds: float = 300,
        consumer_kind: str = "read-only-runtime",
        consumer_metadata: Optional[Mapping[str, Any]] = None,
    ) -> ManagedReadOnlyProjection:
        """Recover the exact live private consumer selected by an operation.

        This does not replay the creator's actor-bound ledger requests and it
        does not enumerate a projection root.  The public operation
        coordinate, immutable projection semantics, and current actor
        authorization resolve one private realization and consumer.  If a
        crash happened before either durable object existed, deterministic
        recovery operations finish only that missing portion.

        The original TTL is checked while the consumer has never been renewed.
        After a heartbeat, the ledger no longer retains that historical
        duration; current live authority is then the lifecycle proof and the
        caller's higher-level attempt record remains the TTL policy source.
        """

        _required_service_text(operation_id, "operation_id")
        _required_service_text(actor_principal_id, "actor_principal_id")
        _required_service_text(holder_id, "holder_id")
        _required_service_text(consumer_kind, "consumer_kind", max_bytes=128)
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        if not isinstance(spec, ProjectionSpec):
            raise TypeError("spec must be a ProjectionSpec.")
        if not spec.mappings:
            raise ValueError(
                "A read-only content projection requires at least one mapping."
            )
        try:
            store = self._stores[store_id]
        except (KeyError, TypeError) as error:
            raise RealmNotFound("Entity not found.") from error
        metadata = dict(consumer_metadata or {})
        roots = tuple(
            sorted({item.snapshot_ref for item in spec.mappings}, key=str)
        )
        private_operation_digest = request_digest(
            {
                "format": _PRIVATE_OPERATION_COORDINATE_FORMAT,
                "realm_id": self._ledger.realm_id,
                "operation_id": operation_id,
            }
        )
        resolution = self._availability_resolution(
            store,
            roots,
            private_operation_digest=private_operation_digest,
        )
        semantic_digest = self._semantic_request_digest(spec, resolution)
        self._ensure_registered_root()
        realization = self._recover_ready_private_realization(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            store=store,
            spec=spec,
            roots=roots,
            resolution=resolution,
            semantic_digest=semantic_digest,
            ttl_seconds=ttl_seconds,
        )
        if (
            realization.state is not ProjectionRealizationState.READY
            or realization.projection_root_id
            != self._root_binding.projection_root_id
            or realization.owner_id != spec.owner_id
            or realization.store_id != store.store_id
            or realization.provider_kind != self._provider.PROVIDER_KIND
            or realization.spec_digest != spec.digest
            or _thaw_mapping(realization.spec) != spec.to_dict()
            or realization.request_digest != semantic_digest
            or _thaw_mapping(realization.availability_resolution) != resolution
        ):
            raise RealmConflict(
                "Existing private projection differs from the requested semantics."
            )
        _require_private_operation_resolution(
            realization,
            realm_id=self._ledger.realm_id,
            expected_operation_id=operation_id,
        )
        consumer = self._recover_private_consumer(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            realization=realization,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            consumer_kind=consumer_kind,
            consumer_metadata=metadata,
        )
        if (
            consumer.realization_id != realization.realization_id
            or consumer.consumer_kind != consumer_kind
            or _thaw_mapping(consumer.metadata) != metadata
        ):
            raise RealmConflict(
                "Existing private projection consumer differs from the request."
            )

        # Consumer leases are create-once children.  Their initial fencing
        # token is therefore the only token that can be current; a released or
        # otherwise substituted lease fails closed here.
        consumer_lease = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=consumer.lease_id,
            holder_id=holder_id,
            fencing_token=1,
        )
        expected_lease_metadata = {
            "realization_id": realization.realization_id,
            "consumer_id": consumer.consumer_id,
            "consumer_kind": consumer_kind,
        }
        if (
            consumer_lease.owner_id != realization.owner_id
            or consumer_lease.parent_lease_id != realization.owner_lease_id
            or consumer_lease.lease_kind != "projection-consumer"
            or consumer_lease.audience != "runtime"
            or consumer_lease.holder_id != holder_id
            or consumer_lease.scope_key
            != (
                "projection-consumer:"
                f"{realization.realization_id}:{consumer.consumer_id}"
            )
            or _thaw_mapping(consumer_lease.metadata) != expected_lease_metadata
        ):
            raise RealmIntegrityError(
                "Existing private projection consumer authority is malformed."
            )
        _require_initial_ttl_if_unrenewed(
            consumer_lease,
            ttl_seconds=ttl_seconds,
            label="private projection consumer",
        )
        return self.reattach_private_read_only_consumer(
            actor_principal_id=actor_principal_id,
            expected_operation_id=operation_id,
            realization_id=realization.realization_id,
            consumer_id=consumer.consumer_id,
            consumer_holder_id=holder_id,
            consumer_fencing_token=consumer_lease.fencing_token,
        )

    def _recover_ready_private_realization(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store: LocalContentStore,
        spec: ProjectionSpec,
        roots: Tuple[SnapshotRef, ...],
        resolution: Mapping[str, Any],
        semantic_digest: str,
        ttl_seconds: float,
    ) -> ProjectionRealizationRecord:
        """Finish or replace only the realization selected by one coordinate."""

        recovery_key = request_digest(
            {
                "format": "optpilot.projection-private-recovery.v1",
                "realm_id": self._ledger.realm_id,
                "projection_root_id": self._root_binding.projection_root_id,
                "operation_id": operation_id,
                "request_digest": semantic_digest,
            }
        )
        realization = self._ledger.find_active_projection_realization(
            actor_principal_id=actor_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            owner_id=spec.owner_id,
            request_digest_value=semantic_digest,
        )
        if realization is None:
            raise RealmNotFound("Entity not found.")
        if realization.state in {
            ProjectionRealizationState.CREATING,
            ProjectionRealizationState.MATERIALIZING,
        }:
            self._reconcile_abandoned_private_build(
                realization=realization,
                ttl_seconds=ttl_seconds,
            )
            create_operation = (
                f"projection.private-recovery.realize/{recovery_key}/"
                f"{request_digest({'retired_realization_id': realization.realization_id})}"
            )
            realization = self._ensure_ready_realization(
                operation_id=create_operation,
                actor_principal_id=actor_principal_id,
                store=store,
                spec=spec,
                roots=roots,
                resolution=resolution,
                semantic_digest=semantic_digest,
                ttl_seconds=ttl_seconds,
                creation_attempt=0,
            )
        return realization

    def _reconcile_abandoned_private_build(
        self,
        *,
        realization: ProjectionRealizationRecord,
        ttl_seconds: float,
    ) -> None:
        """Fence an expired builder, establish cleanup proof, and retire it."""

        try:
            closing = self._ledger.close_projection_realization(
                operation_id=(
                    "projection.maintenance.close/"
                    f"{_cleanup_key(realization.realization_id)}"
                ),
                actor_principal_id=self._maintenance_principal_id,
                realization_id=realization.realization_id,
                owner_holder_id=None,
                owner_fencing_token=None,
            )
        except (RealmConflict, RealmExpired) as error:
            raise RealmConflict(
                "Private projection creation is still under a live provider term."
            ) from error
        if (
            closing.state is ProjectionRealizationState.CLOSING
            and closing.wrapper_inode is None
        ):
            # A crash can occur after the row commit but before the first
            # namespace claim.  Once maintenance has fenced the expired owner,
            # publishing the exact empty claim gives cleanup an authenticated
            # object (or recovers the exact partial wrapper) to retire.
            create_projection_wrapper(
                self._root_binding,
                directory_name=closing.relative_name,
                realization_id=closing.realization_id,
                claim_nonce=closing.claim_nonce,
            )
        reconcile_key = request_digest(
            {
                "format": "optpilot.projection-private-build-reconcile.v1",
                "projection_root_id": self._root_binding.projection_root_id,
                "realization_id": realization.realization_id,
            }
        )
        cleaned = self.reconcile_projection(
            operation_id=f"projection.private-recovery.reconcile/{reconcile_key}",
            realization_id=realization.realization_id,
            ttl_seconds=ttl_seconds,
        )
        if cleaned.realization.state is not ProjectionRealizationState.CLEANED:
            raise RealmIntegrityError(
                "Abandoned private projection was not retired before recovery."
            )

    def _recover_private_consumer(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        realization: ProjectionRealizationRecord,
        holder_id: str,
        ttl_seconds: float,
        consumer_kind: str,
        consumer_metadata: Mapping[str, Any],
    ) -> ProjectionConsumerRecord:
        """Return or atomically create the sole exact recovery consumer."""

        consumers = self._ledger.list_projection_consumers(
            actor_principal_id=actor_principal_id,
            realization_id=realization.realization_id,
        )
        if not consumers:
            key = request_digest(
                {
                    "format": "optpilot.projection-private-consumer-recovery.v1",
                    "realm_id": self._ledger.realm_id,
                    "projection_root_id": self._root_binding.projection_root_id,
                    "operation_id": operation_id,
                    "realization_id": realization.realization_id,
                    "request_digest": realization.request_digest,
                    "holder_id": holder_id,
                    "ttl_seconds": ttl_seconds,
                    "consumer_kind": consumer_kind,
                    "metadata": consumer_metadata,
                }
            )
            consumer_id = f"projection-recovery-consumer-{key[:48]}"
            consumer_lease_id = f"projection-recovery-consumer-lease-{key[:48]}"
            try:
                self._ledger.acquire_projection_consumer(
                    operation_id=f"projection.consumer.recover/{key}",
                    actor_principal_id=actor_principal_id,
                    realization_id=realization.realization_id,
                    consumer_holder_id=holder_id,
                    consumer_ttl_seconds=ttl_seconds,
                    consumer_kind=consumer_kind,
                    metadata=consumer_metadata,
                    consumer_id=consumer_id,
                    consumer_lease_id=consumer_lease_id,
                    release_materialization_grace=True,
                )
            except RealmConflict:
                # A concurrent authorized recovery may have committed the
                # deterministic consumer under another actor.  The exact
                # durable consumer below is the convergence point.
                pass
            consumers = self._ledger.list_projection_consumers(
                actor_principal_id=actor_principal_id,
                realization_id=realization.realization_id,
            )
        if len(consumers) != 1:
            raise RealmConflict(
                "Existing private projection does not have one exact consumer."
            )
        return consumers[0]

    def reattach_private_read_only_consumer(
        self,
        *,
        actor_principal_id: str,
        expected_operation_id: str,
        realization_id: str,
        consumer_id: str,
        consumer_holder_id: str,
        consumer_fencing_token: int,
    ) -> ManagedReadOnlyProjection:
        """Reopen one exact current consumer without replaying its creator.

        Recovery authorization belongs to the current actor and the current
        holder/fence.  ``expected_operation_id`` is not a create request: it
        proves that the persisted realization is the operation-private copy
        assigned to the recovered native attempt.  No host path, owner holder,
        or owner fence is accepted from the caller.
        """

        _required_service_text(actor_principal_id, "actor_principal_id")
        _required_service_text(expected_operation_id, "expected_operation_id")
        _required_service_text(realization_id, "realization_id")
        _required_service_text(consumer_id, "consumer_id")
        _required_service_text(consumer_holder_id, "consumer_holder_id")
        if (
            isinstance(consumer_fencing_token, bool)
            or not isinstance(consumer_fencing_token, int)
            or consumer_fencing_token <= 0
        ):
            raise ValueError("consumer_fencing_token must be a positive integer.")
        self._ensure_registered_root()
        receipt = self._ledger.validate_projection_consumer(
            actor_principal_id=actor_principal_id,
            realization_id=realization_id,
            consumer_id=consumer_id,
            consumer_holder_id=consumer_holder_id,
            consumer_fencing_token=consumer_fencing_token,
        )
        realization = receipt.realization
        if (
            realization.projection_root_id
            != self._root_binding.projection_root_id
            or realization.provider_kind != self._provider.PROVIDER_KIND
        ):
            raise RealmConflict(
                "Projection consumer belongs to a different physical provider."
            )
        _require_private_operation_resolution(
            realization,
            realm_id=self._ledger.realm_id,
            expected_operation_id=expected_operation_id,
        )
        cache_key = request_digest(
            {
                "format": "optpilot.projection-private-reattach.v1",
                "realm_id": self._ledger.realm_id,
                "actor_principal_id": actor_principal_id,
                "projection_root_id": self._root_binding.projection_root_id,
                "expected_operation_id": expected_operation_id,
                "realization_id": realization_id,
                "consumer_id": consumer_id,
                "consumer_lease_id": receipt.consumer_lease.lease_id,
                "consumer_fencing_token": consumer_fencing_token,
            }
        )
        with self._lock:
            active = self._active.get(cache_key)
        if active is not None:
            active.validate()
            return active
        try:
            namespace = attach_projection_namespace(
                self._root_binding,
                self._namespace_claim(realization),
                self._current_namespace_identity(realization),
            )
        except RealmIntegrityError as error:
            self._quarantine_after_identity_failure(
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                error=error,
            )
            raise
        release_key = request_digest(
            {
                "format": "optpilot.projection-private-reattach-release.v1",
                "realm_id": self._ledger.realm_id,
                "actor_principal_id": actor_principal_id,
                "realization_id": realization_id,
                "consumer_id": consumer_id,
                "consumer_lease_id": receipt.consumer_lease.lease_id,
            }
        )
        result = ManagedReadOnlyProjection(
            ledger=self._ledger,
            actor_principal_id=actor_principal_id,
            receipt=receipt,
            namespace=namespace,
            release_operation_id=(
                f"projection.consumer.recovery.release/{release_key}"
            ),
            cache_key=cache_key,
            on_closed=self._forget,
            on_integrity_failure=lambda failed_realization_id, error: (
                self._quarantine_after_identity_failure(
                    actor_principal_id=actor_principal_id,
                    realization_id=failed_realization_id,
                    error=error,
                )
            ),
        )
        with self._lock:
            existing = self._active.get(cache_key)
            if existing is None:
                self._active[cache_key] = result
            else:
                namespace.close()
                result = existing
        result.validate()
        return result

    def retire_private_projection(
        self,
        projection: ManagedReadOnlyProjection,
        *,
        ttl_seconds: float = 300,
    ) -> ProjectionRealizationRecord:
        """Release one exact consumer and synchronously retire its private copy.

        One narrow ledger operation atomically proves that this is the sole
        consumer of the exact operation-private realization, releases its
        consumer/owner lease hierarchy, and moves the realization to cleanup
        debt.  Root maintenance then performs identity-proven physical cleanup.
        Exact retry resumes either phase without exposing broad owner authority
        or accepting a holder, fence, realization id, or host path from the
        caller.
        """

        if not isinstance(projection, ManagedReadOnlyProjection):
            raise TypeError("projection must be a ManagedReadOnlyProjection.")
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        if (
            projection._ledger is not self._ledger
            or projection.realization.projection_root_id
            != self._root_binding.projection_root_id
        ):
            raise RealmConflict(
                "Projection consumer belongs to a different projection service."
            )
        _require_private_operation_resolution(
            projection.realization,
            realm_id=self._ledger.realm_id,
        )
        sharing = projection.realization.availability_resolution[
            "realization_sharing"
        ]
        if not isinstance(sharing, Mapping):  # pragma: no cover - checked above
            raise RealmIntegrityError(
                "Private projection sharing resolution is malformed."
            )
        coordinate_digest = sharing.get("operation_coordinate_digest")
        if not isinstance(coordinate_digest, str):  # pragma: no cover - checked above
            raise RealmIntegrityError(
                "Private projection operation coordinate is malformed."
            )
        key = request_digest(
            {
                "format": "optpilot.projection-private-retirement.v1",
                "realm_id": self._ledger.realm_id,
                "actor_principal_id": projection._actor_principal_id,
                "projection_root_id": self._root_binding.projection_root_id,
                "realization_id": projection.realization.realization_id,
                "consumer_id": projection.consumer_id,
                "consumer_lease_id": projection.consumer_lease.lease_id,
                "operation_coordinate_digest": coordinate_digest,
            }
        )
        retired = self._ledger.retire_private_projection_consumer(
            operation_id=f"projection.private-retire/{key}",
            actor_principal_id=projection._actor_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            realization_id=projection.realization.realization_id,
            consumer_id=projection.consumer_id,
            consumer_holder_id=projection.consumer_lease.holder_id,
            consumer_fencing_token=projection.consumer_lease.fencing_token,
            expected_operation_coordinate_digest=coordinate_digest,
        )
        if retired.state is not ProjectionRealizationState.CLOSING:
            raise RealmIntegrityError(
                "Private projection retirement did not create cleanup debt."
            )
        projection._detach_after_private_retirement()
        maintenance_key = request_digest(
            {
                "format": "optpilot.projection-private-retirement-reconcile.v1",
                "realm_id": self._ledger.realm_id,
                "projection_root_id": self._root_binding.projection_root_id,
                "realization_id": projection.realization.realization_id,
            }
        )
        return self.reconcile_projection(
            operation_id=f"projection.private-retire.reconcile/{maintenance_key}",
            realization_id=projection.realization.realization_id,
            ttl_seconds=ttl_seconds,
        ).realization

    def retire_private_projection_operation(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        realization_id: str,
        expected_owner_id: str,
        expected_store_id: str,
        expected_spec: ProjectionSpec,
        expected_operation_coordinate_digest: str,
        expected_consumer_holder_id: str,
        expected_consumer_kind: str,
        expected_consumer_metadata: Mapping[str, Any],
        ttl_seconds: float = 300,
    ) -> ProjectionRealizationRecord:
        """Retire an exact partial or attached private operation realization.

        The ledger fences its deterministic authority first.  If creation
        stopped before publishing a namespace claim, the provider then creates
        (or recovers) the exact empty wrapper needed by the ordinary root
        reconciler.  No source-store attachment or owner credential is needed.
        """

        _required_service_text(operation_id, "operation_id")
        _required_service_text(actor_principal_id, "actor_principal_id")
        _required_service_text(realization_id, "realization_id")
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        if not isinstance(expected_spec, ProjectionSpec):
            raise TypeError("expected_spec must be a ProjectionSpec.")
        self._ensure_registered_root(require_active=False)
        exact_arguments = {
            "actor_principal_id": actor_principal_id,
            "projection_root_id": self._root_binding.projection_root_id,
            "realization_id": realization_id,
            "expected_owner_id": expected_owner_id,
            "expected_store_id": expected_store_id,
            "expected_spec": expected_spec.to_dict(),
            "expected_provider_kind": self._provider.PROVIDER_KIND,
            "expected_operation_coordinate_digest": (
                expected_operation_coordinate_digest
            ),
            "expected_consumer_holder_id": expected_consumer_holder_id,
            "expected_consumer_kind": expected_consumer_kind,
            "expected_consumer_metadata": expected_consumer_metadata,
        }
        try:
            self._ledger.retire_private_projection_operation(
                operation_id=operation_id,
                **exact_arguments,
            )
        except RealmConflict:
            adopted = self._ledger.validate_private_projection_operation(
                **exact_arguments
            )
            if adopted.state not in {
                ProjectionRealizationState.CLOSING,
                ProjectionRealizationState.CLEANING,
                ProjectionRealizationState.CLEANED,
            }:
                raise
        current = self._maintenance_realization(realization_id)
        if current.state is ProjectionRealizationState.CLEANED:
            if current.cleanup_token is not None:
                complete_projection_cleanup_namespace(
                    self._root_binding,
                    self._namespace_claim(current),
                    cleanup_token=current.cleanup_token,
                )
            return current
        if (
            current.state is ProjectionRealizationState.CLOSING
            and current.wrapper_inode is None
        ):
            create_projection_wrapper(
                self._root_binding,
                directory_name=current.relative_name,
                realization_id=current.realization_id,
                claim_nonce=current.claim_nonce,
            )
        key = request_digest(
            {
                "format": "optpilot.projection-private-operation-retirement.v1",
                "projection_root_id": self._root_binding.projection_root_id,
                "realization_id": realization_id,
                "operation_id": operation_id,
            }
        )
        return self.reconcile_projection(
            operation_id=f"projection.private-operation.reconcile/{key}",
            realization_id=realization_id,
            ttl_seconds=ttl_seconds,
        ).realization

    def project_selection_read_only(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        selection: SelectionRef,
        holder_id: str,
        ttl_seconds: float = 300,
        consumer_kind: str = "selection-inspection",
        consumer_metadata: Optional[Mapping[str, Any]] = None,
    ) -> ManagedReadOnlyProjection:
        """Open an immutable selection through the normal consumer lease path.

        Resolution and projection both reauthorize the source owner.  No
        workspace owner is minted and no content is re-ingested; the selected
        tree is mapped into the existing reusable projection realization.
        """

        _required_service_text(operation_id, "operation_id")
        _required_service_text(actor_principal_id, "actor_principal_id")
        _required_service_text(holder_id, "holder_id")
        _required_service_text(consumer_kind, "consumer_kind", max_bytes=128)
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        metadata = dict(consumer_metadata or {})
        if _SELECTION_METADATA_KEY in metadata:
            raise ValueError(
                f"consumer_metadata key {_SELECTION_METADATA_KEY!r} is reserved."
            )
        resolution = self._ledger.resolve_selection_for_read_projection(
            actor_principal_id=actor_principal_id,
            selection=selection,
        )
        if not resolution.eligibility.eligible or resolution.root is None:
            raise SelectionProjectionUnavailable(resolution.eligibility)
        metadata[_SELECTION_METADATA_KEY] = selection.to_dict()
        return self.project_read_only(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            store_id=resolution.root.store_id,
            spec=ProjectionSpec(
                owner_id=selection.source_owner_id,
                mappings=(TreeMapping(resolution.root.content_ref),),
            ),
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            consumer_kind=consumer_kind,
            consumer_metadata=metadata,
        )

    def project_workspace_read_only(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        workspace_id: str,
        holder_id: str,
        ttl_seconds: float = 300,
        consumer_kind: str = "workspace-runtime",
        consumer_metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorkspaceReadOnlyProjection:
        workspace, revision = self._ledger.read_workspace(
            actor_principal_id=actor_principal_id,
            workspace_id=workspace_id,
            permission=OwnerPermission.DERIVE,
        )
        projection = self.project_read_only(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            store_id=revision.root_store_id,
            spec=ProjectionSpec(
                owner_id=workspace.owner_id,
                mappings=(TreeMapping(revision.root_ref),),
            ),
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            consumer_kind=consumer_kind,
            consumer_metadata=consumer_metadata,
        )
        return WorkspaceReadOnlyProjection(workspace, revision, projection)

    def reconcile_projection(
        self,
        *,
        operation_id: str,
        realization_id: str,
        ttl_seconds: float = 300,
    ) -> ProjectionReconcileReceipt:
        """Close and clean one abandoned realization under root-only authority."""

        _required_service_text(operation_id, "operation_id")
        _required_service_text(realization_id, "realization_id")
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        self._ensure_registered_root(require_active=False)
        coordinated = self._ledger.coordinate_projection_reconcile_request(
            operation_id=operation_id,
            actor_principal_id=self._maintenance_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            realization_id=realization_id,
        )
        if coordinated != (realization_id,):
            raise RealmIntegrityError(
                "Projection reconciliation target receipt is malformed."
            )
        record = self._maintenance_realization(realization_id)
        namespace_removed = False
        if record.state is ProjectionRealizationState.QUARANTINED:
            raise RealmConflict(
                "Quarantined projection requires explicit forensic resolution."
            )
        if record.state is ProjectionRealizationState.CLEANED:
            if record.cleanup_token is not None:
                complete_projection_cleanup_namespace(
                    self._root_binding,
                    self._namespace_claim(record),
                    cleanup_token=record.cleanup_token,
                )
            return ProjectionReconcileReceipt(record, False, already_complete=True)
        cleanup_token = self._maintenance_cleanup_token(record)
        if record.state in {
            ProjectionRealizationState.CREATING,
            ProjectionRealizationState.MATERIALIZING,
            ProjectionRealizationState.READY,
        }:
            try:
                record = self._ledger.close_projection_realization(
                    operation_id=(
                        "projection.maintenance.close/"
                        f"{_cleanup_key(realization_id)}"
                    ),
                    actor_principal_id=self._maintenance_principal_id,
                    realization_id=realization_id,
                    owner_holder_id=None,
                    owner_fencing_token=None,
                )
            except RealmError:
                adopted = self._adopt_exact_cleaned(
                    expected=record, cleanup_token=cleanup_token
                )
                if adopted is not None:
                    return adopted
                current = self._maintenance_realization(realization_id)
                self._validate_reconcile_identity(record, current)
                if current.state not in {
                    ProjectionRealizationState.CLOSING,
                    ProjectionRealizationState.CLEANING,
                }:
                    raise
                record = current
        claim = self._namespace_claim(record)
        try:
            identity = self._preflight_cleanup_identity(
                record=record,
                claim=claim,
                cleanup_token=cleanup_token,
            )
        except RealmIntegrityError as error:
            adopted = self._adopt_exact_cleaned(
                expected=record, cleanup_token=cleanup_token
            )
            if adopted is not None:
                return adopted
            self._quarantine_after_identity_failure(
                actor_principal_id=self._maintenance_principal_id,
                realization_id=realization_id,
                error=error,
            )
            raise
        try:
            cleanup = self._maintenance_cleanup_claim(
                record=record,
                ttl_seconds=ttl_seconds,
            )
        except RealmError:
            adopted = self._adopt_exact_cleaned(
                expected=record, cleanup_token=cleanup_token
            )
            if adopted is not None:
                return adopted
            raise
        cleanup_record = cleanup.realization
        if cleanup_record.cleanup_token != cleanup_token:
            raise RealmIntegrityError(
                "Cleaning projection has a different cleanup token."
            )
        if cleanup_record.cleanup_token is None:
            raise RealmIntegrityError("Cleaning projection has no cleanup token.")
        heartbeat = _BuilderHeartbeat(
            ledger=self._ledger,
            actor_principal_id=self._maintenance_principal_id,
            realization_id=realization_id,
            claim=cleanup,
            operation_prefix=(
                f"projection.maintenance.heartbeat/{_cleanup_key(realization_id)}"
            ),
            ttl_seconds=ttl_seconds,
        )
        try:
            heartbeat.start()
        except RealmError:
            # The deterministic cleanup claim can be replayed by two
            # reconcilers.  If the winner commits CLEANED between the loser's
            # claim receipt and its first heartbeat, that stale heartbeat no
            # longer has live owner authority.  Adopt only the exact cleaned
            # realization and token; every other failure remains fatal.
            adopted = self._adopt_exact_cleaned(
                expected=record, cleanup_token=cleanup_token
            )
            if adopted is not None:
                return adopted
            raise
        heartbeat_running = True
        try:
            try:
                namespace_removed = cleanup_projection_namespace(
                    self._root_binding,
                    claim,
                    identity,
                    cleanup_token=cleanup_token,
                )
            except RealmIntegrityError as error:
                adopted = self._adopt_exact_cleaned(
                    expected=record, cleanup_token=cleanup_token
                )
                if adopted is not None:
                    return adopted
                self._quarantine_after_identity_failure(
                    actor_principal_id=self._maintenance_principal_id,
                    realization_id=realization_id,
                    error=error,
                )
                raise
            try:
                heartbeat.raise_if_failed()
                completed = self._ledger.complete_projection_cleanup(
                    operation_id=(
                        "projection.maintenance.complete/"
                        f"{_cleanup_key(realization_id)}"
                    ),
                    actor_principal_id=self._maintenance_principal_id,
                    realization_id=realization_id,
                    owner_holder_id=heartbeat.owner_lease.holder_id,
                    owner_fencing_token=heartbeat.owner_lease.fencing_token,
                    builder_holder_id=heartbeat.builder_lease.holder_id,
                    builder_fencing_token=heartbeat.builder_lease.fencing_token,
                    cleanup_token=cleanup_token,
                )
            except RealmError:
                adopted = self._adopt_exact_cleaned(
                    expected=record, cleanup_token=cleanup_token
                )
                if adopted is not None:
                    return adopted
                raise
            heartbeat.stop()
            heartbeat_running = False
        finally:
            if heartbeat_running:
                heartbeat.stop()
        complete_projection_cleanup_namespace(
            self._root_binding,
            claim,
            cleanup_token=cleanup_token,
        )
        return ProjectionReconcileReceipt(
            completed.realization,
            namespace_removed,
            already_complete=False,
        )

    def reconcile_all_projections(
        self,
        *,
        operation_id: str,
        ttl_seconds: float = 300,
    ) -> tuple[ProjectionReconcileOutcome, ...]:
        """Reconcile one stable root snapshot while isolating poison entries."""

        _required_service_text(operation_id, "operation_id")
        ttl_seconds = _positive_service_ttl(ttl_seconds)
        self._ensure_registered_root(require_active=False)
        targets = self._ledger.coordinate_projection_reconcile_request(
            operation_id=operation_id,
            actor_principal_id=self._maintenance_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            realization_id=None,
        )
        outcomes = []
        for realization_id in targets:
            record = self._maintenance_realization(realization_id)
            if record.state is ProjectionRealizationState.QUARANTINED:
                continue
            try:
                receipt = self.reconcile_projection(
                    operation_id=f"{operation_id}/{record.realization_id}",
                    realization_id=record.realization_id,
                    ttl_seconds=ttl_seconds,
                )
            except BaseException as error:
                outcomes.append(
                    ProjectionReconcileOutcome(
                        realization_id=record.realization_id,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                )
            else:
                outcomes.append(
                    ProjectionReconcileOutcome(
                        realization_id=record.realization_id,
                        receipt=receipt,
                    )
                )
        return tuple(outcomes)

    def _project_once(
        self,
        *,
        cache_key: str,
        operation_id: str,
        actor_principal_id: str,
        store: LocalContentStore,
        spec: ProjectionSpec,
        roots: Tuple[SnapshotRef, ...],
        resolution: Mapping[str, Any],
        semantic_digest: str,
        holder_id: str,
        ttl_seconds: float,
        consumer_kind: str,
        consumer_metadata: Mapping[str, Any],
    ) -> ManagedReadOnlyProjection:
        self._ensure_registered_root()
        coordinated = self._ledger.coordinate_projection_consumer_request(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            owner_id=spec.owner_id,
            request_digest_value=semantic_digest,
            consumer_holder_id=holder_id,
            consumer_ttl_seconds=ttl_seconds,
            consumer_kind=consumer_kind,
            metadata=consumer_metadata,
        )
        if coordinated != cache_key:
            raise RealmIntegrityError(
                "Projection consumer coordination digest disagrees with the service request."
            )
        realization = self._ensure_ready_realization(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            store=store,
            spec=spec,
            roots=roots,
            resolution=resolution,
            semantic_digest=semantic_digest,
            ttl_seconds=ttl_seconds,
            creation_attempt=0,
        )
        acquire_operation_id = f"projection.consumer.acquire/{cache_key}"
        release_operation_id = f"projection.consumer.release/{cache_key}"
        try:
            receipt = self._ledger.acquire_projection_consumer(
                operation_id=acquire_operation_id,
                actor_principal_id=actor_principal_id,
                realization_id=realization.realization_id,
                consumer_holder_id=holder_id,
                consumer_ttl_seconds=ttl_seconds,
                consumer_kind=consumer_kind,
                metadata=consumer_metadata,
                release_materialization_grace=True,
            )
        except RealmExpired:
            # READY is durable cache state, not proof that its owner term is
            # still live.  Retire that exact abandoned realization and use a
            # fresh create identity; replaying attempt zero would only return
            # the now-CLEANED creation receipt.
            self.reconcile_projection(
                operation_id=(
                    f"projection.consumer.recover/{cache_key}/"
                    f"{realization.realization_id}"
                ),
                realization_id=realization.realization_id,
                ttl_seconds=ttl_seconds,
            )
            realization = self._ensure_ready_realization(
                operation_id=operation_id,
                actor_principal_id=actor_principal_id,
                store=store,
                spec=spec,
                roots=roots,
                resolution=resolution,
                semantic_digest=semantic_digest,
                ttl_seconds=ttl_seconds,
                creation_attempt=1,
            )
            receipt = self._ledger.acquire_projection_consumer(
                operation_id=acquire_operation_id,
                actor_principal_id=actor_principal_id,
                realization_id=realization.realization_id,
                consumer_holder_id=holder_id,
                consumer_ttl_seconds=ttl_seconds,
                consumer_kind=consumer_kind,
                metadata=consumer_metadata,
                release_materialization_grace=True,
            )
        try:
            receipt = self._ledger.validate_projection_consumer(
                actor_principal_id=actor_principal_id,
                realization_id=receipt.realization.realization_id,
                consumer_id=receipt.consumer.consumer_id,
                consumer_holder_id=receipt.consumer_lease.holder_id,
                consumer_fencing_token=receipt.consumer_lease.fencing_token,
            )
        except (RealmConflict, RealmExpired) as error:
            raise RealmConflict(
                "Projection operation already refers to an unavailable consumer."
            ) from error
        try:
            namespace = attach_projection_namespace(
                self._root_binding,
                self._namespace_claim(receipt.realization),
                self._current_namespace_identity(receipt.realization),
            )
        except BaseException as error:
            try:
                self._ledger.release_projection_consumer(
                    operation_id=release_operation_id,
                    actor_principal_id=actor_principal_id,
                    realization_id=receipt.realization.realization_id,
                    consumer_id=receipt.consumer.consumer_id,
                    consumer_holder_id=receipt.consumer_lease.holder_id,
                    consumer_fencing_token=receipt.consumer_lease.fencing_token,
                )
            except BaseException as release_error:
                error.add_note(
                    f"projection consumer release also failed: {release_error}"
                )
            if isinstance(error, RealmIntegrityError):
                self._quarantine_after_identity_failure(
                    actor_principal_id=actor_principal_id,
                    realization_id=receipt.realization.realization_id,
                    error=error,
                )
            raise
        return ManagedReadOnlyProjection(
            ledger=self._ledger,
            actor_principal_id=actor_principal_id,
            receipt=receipt,
            namespace=namespace,
            release_operation_id=release_operation_id,
            cache_key=cache_key,
            on_closed=self._forget,
            on_integrity_failure=lambda realization_id, error: (
                self._quarantine_after_identity_failure(
                    actor_principal_id=actor_principal_id,
                    realization_id=realization_id,
                    error=error,
                )
            ),
        )

    def _ensure_ready_realization(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store: LocalContentStore,
        spec: ProjectionSpec,
        roots: Tuple[SnapshotRef, ...],
        resolution: Mapping[str, Any],
        semantic_digest: str,
        ttl_seconds: float,
        creation_attempt: int,
        stale_build_takeover_attempts: int = 2,
    ) -> ProjectionRealizationRecord:
        if isinstance(creation_attempt, bool) or not isinstance(creation_attempt, int):
            raise TypeError("creation_attempt must be an integer.")
        if creation_attempt < 0:
            raise ValueError("creation_attempt must be nonnegative.")
        materialization_ttl_seconds = min(
            max(ttl_seconds, _ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS),
            _ACTIVE_MATERIALIZATION_LEASE_MAX_SECONDS,
        )
        root_id = self._root_binding.projection_root_id
        active = self._ledger.find_active_projection_realization(
            actor_principal_id=actor_principal_id,
            projection_root_id=root_id,
            owner_id=spec.owner_id,
            request_digest_value=semantic_digest,
        )
        create_key = request_digest(
            {
                "format": "optpilot.projection-create-request.v1",
                "operation_id": operation_id,
                "actor_principal_id": actor_principal_id,
                "projection_root_id": root_id,
                "request_digest": semantic_digest,
                "creation_attempt": creation_attempt,
            }
        )
        create_receipt = None
        if active is None or active.state is ProjectionRealizationState.CREATING:
            try:
                create_receipt = self._ledger.create_projection_realization(
                    operation_id=f"projection.realization.create/{create_key}",
                    actor_principal_id=actor_principal_id,
                    projection_root_id=root_id,
                    owner_id=spec.owner_id,
                    store_id=store.store_id,
                    spec=spec.to_dict(),
                    availability_resolution=resolution,
                    request_digest_value=semantic_digest,
                    provider_kind=self._provider.PROVIDER_KIND,
                    claim_nonce=request_digest(
                        {"format": "optpilot.projection-claim-nonce.v1", "key": create_key}
                    ),
                    relative_name=f"projection-{create_key[:40]}",
                    snapshot_roots=roots,
                    owner_holder_id=f"projection-owner-{create_key[:40]}",
                    owner_ttl_seconds=materialization_ttl_seconds,
                )
            except RealmConflict:
                # Another operation/process may have won the semantic unique
                # request.  Its durable row, not a directory race, coordinates us.
                pass
            active = self._ledger.find_active_projection_realization(
                actor_principal_id=actor_principal_id,
                projection_root_id=root_id,
                owner_id=spec.owner_id,
                request_digest_value=semantic_digest,
            )
        if active is None:
            raise RealmConflict("Projection realization coordination was lost.")
        if active.state is ProjectionRealizationState.READY:
            return active
        if (
            active.state is ProjectionRealizationState.CREATING
            and create_receipt is not None
            and active.realization_id == create_receipt.realization.realization_id
        ):
            claim_key = request_digest(
                {
                    "format": "optpilot.projection-builder-claim.v1",
                    "realization_id": active.realization_id,
                    "service_instance": self._instance_id,
                }
            )
            try:
                claim = self._ledger.claim_projection_materialization(
                    operation_id=f"projection.materialization.claim/{claim_key}",
                    actor_principal_id=actor_principal_id,
                    realization_id=active.realization_id,
                    owner_holder_id=create_receipt.owner_lease.holder_id,
                    owner_fencing_token=create_receipt.owner_lease.fencing_token,
                    builder_holder_id=f"projection-builder-{claim_key[:40]}",
                    builder_ttl_seconds=materialization_ttl_seconds,
                )
            except RealmConflict:
                claim = None
            if claim is not None:
                return self._materialize(
                    actor_principal_id=actor_principal_id,
                    store=store,
                    spec=spec,
                    roots=roots,
                    claim=claim,
                    ttl_seconds=materialization_ttl_seconds,
                    operation_key=claim_key,
                )
        waited = self._wait_for_ready(
            actor_principal_id=actor_principal_id,
            realization_id=active.realization_id,
            stale_build_ttl_seconds=(
                materialization_ttl_seconds
                if stale_build_takeover_attempts > 0
                else None
            ),
        )
        if waited is not None:
            return waited
        # The stalled build's lapsed owner term was fenced and its realization
        # retired.  Rebuild under a takeover-scoped create identity so exact
        # replay of the original creation cannot resurrect the retired row.
        takeover_operation = "projection.stale-build.recreate/" + request_digest(
            {
                "format": "optpilot.projection-stale-build-takeover.v1",
                "operation_id": operation_id,
                "creation_attempt": creation_attempt,
                "retired_realization_id": active.realization_id,
            }
        )
        return self._ensure_ready_realization(
            operation_id=takeover_operation,
            actor_principal_id=actor_principal_id,
            store=store,
            spec=spec,
            roots=roots,
            resolution=resolution,
            semantic_digest=semantic_digest,
            ttl_seconds=ttl_seconds,
            creation_attempt=0,
            stale_build_takeover_attempts=stale_build_takeover_attempts - 1,
        )

    def _materialize(
        self,
        *,
        actor_principal_id: str,
        store: LocalContentStore,
        spec: ProjectionSpec,
        roots: Tuple[SnapshotRef, ...],
        claim: ProjectionClaimReceipt,
        ttl_seconds: float,
        operation_key: str,
    ) -> ProjectionRealizationRecord:
        realization = claim.realization
        capability = _LedgerProjectionContentCapability(
            ledger=self._ledger,
            actor_principal_id=actor_principal_id,
            store=store,
            realization_id=realization.realization_id,
            owner_lease=claim.owner_lease,
            snapshot_roots=roots,
            # Match the builder heartbeat cadence: the lease design already
            # tolerates this much staleness between fencing observations.
            revalidation_interval_seconds=max(0.001, min(ttl_seconds / 3.0, 30.0)),
        )
        heartbeat = _BuilderHeartbeat(
            ledger=self._ledger,
            actor_principal_id=actor_principal_id,
            realization_id=realization.realization_id,
            claim=claim,
            operation_prefix=f"projection.builder.heartbeat/{operation_key}",
            ttl_seconds=ttl_seconds,
        )
        physical: ProjectionLease | None = None
        namespace_identity: ProjectionNamespaceIdentity | None = None
        namespace_claim = self._namespace_claim(realization)
        heartbeat_running = False
        try:
            heartbeat.start()
            heartbeat_running = True
            capability.authorize_exact_closure()
            namespace_claim, namespace_identity = create_projection_wrapper(
                self._root_binding,
                directory_name=realization.relative_name,
                realization_id=realization.realization_id,
                claim_nonce=realization.claim_nonce,
            )
            claim = self._ledger.record_projection_namespace_claim(
                operation_id=f"projection.materialization.namespace/{operation_key}",
                actor_principal_id=actor_principal_id,
                realization_id=realization.realization_id,
                builder_holder_id=claim.builder_lease.holder_id,
                builder_fencing_token=claim.builder_lease.fencing_token,
                wrapper_device_id=namespace_identity.wrapper_device_id,
                wrapper_inode=namespace_identity.wrapper_inode,
            )
            physical = self._provider.project(
                spec=spec,
                source=capability,
                target=ProjectionTarget(
                    self._root_binding.path / realization.relative_name,
                    "root",
                ),
            )
            namespace_identity = record_projection_tree_identity(
                self._root_binding,
                namespace_claim,
                namespace_identity,
            )
            assert namespace_identity.tree_device_id is not None
            assert namespace_identity.tree_inode is not None
            heartbeat.raise_if_failed()
            published = self._ledger.publish_projection_ready(
                operation_id=f"projection.materialization.publish/{operation_key}",
                actor_principal_id=actor_principal_id,
                realization_id=realization.realization_id,
                builder_holder_id=heartbeat.builder_lease.holder_id,
                builder_fencing_token=heartbeat.builder_lease.fencing_token,
                wrapper_device_id=namespace_identity.wrapper_device_id,
                wrapper_inode=namespace_identity.wrapper_inode,
                exposed_tree_device_id=namespace_identity.tree_device_id,
                exposed_tree_inode=namespace_identity.tree_inode,
                plan_digest=physical.plan.digest,
                copied_logical_bytes=physical.plan.logical_bytes,
                copied_file_count=sum(
                    1 for entry in physical.plan.entries if entry.kind == "file"
                ),
            )
            heartbeat.stop()
            heartbeat_running = False
            physical.detach_after_publish()
            return published.realization
        except BaseException as error:
            if heartbeat_running:
                heartbeat.stop()
            if physical is not None and not physical.detached and not physical.cleaned:
                try:
                    physical.cleanup()
                except BaseException as cleanup_error:
                    error.add_note(f"projection provider rollback also failed: {cleanup_error}")
            durable_cleanup: ProjectionRealizationRecord | None = None
            try:
                durable_cleanup = self._close_and_cleanup(
                    actor_principal_id=actor_principal_id,
                    realization_id=realization.realization_id,
                    owner_lease=heartbeat.owner_lease,
                    ttl_seconds=ttl_seconds,
                )
            except BaseException as cleanup_error:
                error.add_note(f"projection realization cleanup also failed: {cleanup_error}")
            if (
                durable_cleanup is not None
                and durable_cleanup.state is ProjectionRealizationState.CLEANED
                and namespace_identity is not None
            ):
                # A stale builder can resume after the reconciler proved the
                # old name absent and removed its tombstone.  If it recreated
                # the exact old claim before observing its fence failure, erase
                # only that authenticated, never-published leftover.
                try:
                    lingering = find_projection_wrapper_identity(
                        self._root_binding,
                        namespace_claim,
                        directory_name=realization.relative_name,
                    )
                    if lingering is not None:
                        rollback_token = request_digest(
                            {
                                "format": "optpilot.projection-builder-rollback.v1",
                                "realization_id": realization.realization_id,
                                "operation_key": operation_key,
                            }
                        )
                        cleanup_projection_namespace(
                            self._root_binding,
                            namespace_claim,
                            lingering,
                            cleanup_token=rollback_token,
                        )
                        complete_projection_cleanup_namespace(
                            self._root_binding,
                            namespace_claim,
                            cleanup_token=rollback_token,
                        )
                except BaseException as cleanup_error:
                    error.add_note(
                        "stale projection builder namespace cleanup also failed: "
                        f"{cleanup_error}"
                    )
            raise

    def _close_and_cleanup(
        self,
        *,
        actor_principal_id: str,
        realization_id: str,
        owner_lease: LeaseRecord,
        ttl_seconds: float,
    ) -> ProjectionRealizationRecord:
        key = request_digest(
            {"format": "optpilot.projection-cleanup.v1", "realization_id": realization_id}
        )
        record = self._ledger.read_projection_realization(
            actor_principal_id=actor_principal_id,
            realization_id=realization_id,
        )
        if record.state in {
            ProjectionRealizationState.CREATING,
            ProjectionRealizationState.MATERIALIZING,
            ProjectionRealizationState.READY,
        }:
            record = self._ledger.close_projection_realization(
                operation_id=f"projection.realization.close/{key}",
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                owner_holder_id=owner_lease.holder_id,
                owner_fencing_token=owner_lease.fencing_token,
            )
        if record.state is ProjectionRealizationState.CLOSING:
            cleanup_token = request_digest(
                {"format": "optpilot.projection-cleanup-token.v1", "key": key}
            )
            cleanup = self._ledger.claim_projection_cleanup(
                operation_id=f"projection.cleanup.claim/{key}",
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                owner_holder_id=owner_lease.holder_id,
                owner_fencing_token=owner_lease.fencing_token,
                builder_holder_id=f"projection-cleaner-{key[:40]}",
                builder_ttl_seconds=ttl_seconds,
                cleanup_token=cleanup_token,
            )
        elif record.state is ProjectionRealizationState.CLEANING:
            # Exact claim replay recovers credentials after a lost response.
            cleanup_token = record.cleanup_token
            if cleanup_token is None:
                raise RealmIntegrityError("Cleaning projection has no cleanup token.")
            cleanup = self._ledger.claim_projection_cleanup(
                operation_id=f"projection.cleanup.claim/{key}",
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                owner_holder_id=owner_lease.holder_id,
                owner_fencing_token=owner_lease.fencing_token,
                builder_holder_id=f"projection-cleaner-{key[:40]}",
                builder_ttl_seconds=ttl_seconds,
                cleanup_token=cleanup_token,
            )
        else:
            return record
        cleanup_record = cleanup.realization
        namespace_claim = self._namespace_claim(cleanup_record)
        if cleanup_record.cleanup_token is None:
            raise RealmIntegrityError("Cleaning projection has no cleanup token.")
        try:
            identity = self._preflight_cleanup_identity(
                record=cleanup_record,
                claim=namespace_claim,
                cleanup_token=cleanup_record.cleanup_token,
            )
            cleanup_projection_namespace(
                self._root_binding,
                namespace_claim,
                identity,
                cleanup_token=cleanup_record.cleanup_token,
            )
        except RealmIntegrityError as error:
            self._quarantine_after_identity_failure(
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                error=error,
            )
            raise
        completed = self._ledger.complete_projection_cleanup(
            operation_id=f"projection.cleanup.complete/{key}",
            actor_principal_id=actor_principal_id,
            realization_id=realization_id,
            owner_holder_id=cleanup.owner_lease.holder_id,
            owner_fencing_token=cleanup.owner_lease.fencing_token,
            builder_holder_id=cleanup.builder_lease.holder_id,
            builder_fencing_token=cleanup.builder_lease.fencing_token,
            cleanup_token=cleanup_record.cleanup_token,
        )
        complete_projection_cleanup_namespace(
            self._root_binding,
            namespace_claim,
            cleanup_token=cleanup_record.cleanup_token,
        )
        return completed.realization

    def _wait_for_ready(
        self,
        *,
        actor_principal_id: str,
        realization_id: str,
        stale_build_ttl_seconds: float | None = None,
    ) -> ProjectionRealizationRecord | None:
        """Wait for publication, probing for a dead builder along the way.

        A builder that dies without releasing its claim (killed process,
        dropped HTTP handler thread) leaves the realization in a creating or
        materializing state that no surviving process will ever publish.  When
        ``stale_build_ttl_seconds`` is provided, this wait periodically
        attempts a fenced retirement of the stalled build; the ledger rejects
        the attempt while the builder's owner term is still live, so an
        actively heartbeating builder is never disturbed.  ``None`` is
        returned only after such a takeover retired the realization, telling
        the caller to rebuild under a fresh create identity.
        """

        now = time.monotonic()
        deadline = now + self._coordination_timeout_seconds
        next_probe = now + _STALE_BUILD_TAKEOVER_GRACE_SECONDS
        # Back off while a builder crawls: fixed fast polling multiplies
        # authority-connection churn per waiting consumer and slows the
        # materialization every waiter is blocked on.
        delay = 0.01
        while True:
            record = self._ledger.read_projection_realization(
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
            )
            if record.state is ProjectionRealizationState.READY:
                return record
            if record.state not in {
                ProjectionRealizationState.CREATING,
                ProjectionRealizationState.MATERIALIZING,
            }:
                raise RealmConflict(
                    f"Projection realization became {record.state.value} before publication."
                )
            now = time.monotonic()
            if stale_build_ttl_seconds is not None and now >= next_probe:
                next_probe = now + _STALE_BUILD_TAKEOVER_INTERVAL_SECONDS
                if self._try_retire_stale_build(
                    realization_id=realization_id,
                    ttl_seconds=stale_build_ttl_seconds,
                ):
                    return None
            if time.monotonic() >= deadline:
                raise RealmConflict("Timed out waiting for projection publication.")
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)

    def _try_retire_stale_build(
        self, *, realization_id: str, ttl_seconds: float
    ) -> bool:
        """Fence and retire one dead-builder realization; False while live.

        The maintenance close is the fencing decision: the ledger only allows
        it once the build's owner term has lapsed, so this can never disturb a
        heartbeating builder.  After fencing, the ordinary root reconciler
        retires the physical namespace exactly as private-build recovery does.
        """

        try:
            closing = self._ledger.close_projection_realization(
                operation_id=(
                    "projection.maintenance.close/"
                    f"{_cleanup_key(realization_id)}"
                ),
                actor_principal_id=self._maintenance_principal_id,
                realization_id=realization_id,
                owner_holder_id=None,
                owner_fencing_token=None,
            )
        except (RealmConflict, RealmExpired):
            return False
        if (
            closing.state is ProjectionRealizationState.CLOSING
            and closing.wrapper_inode is None
        ):
            # The builder can die after the row commit but before the first
            # namespace claim.  Once the lapsed owner is fenced, publishing
            # the exact empty claim gives cleanup an authenticated object (or
            # recovers the exact partial wrapper) to retire.
            create_projection_wrapper(
                self._root_binding,
                directory_name=closing.relative_name,
                realization_id=closing.realization_id,
                claim_nonce=closing.claim_nonce,
            )
        takeover_key = request_digest(
            {
                "format": "optpilot.projection-stale-build-reconcile.v1",
                "projection_root_id": self._root_binding.projection_root_id,
                "realization_id": realization_id,
            }
        )
        cleaned = self.reconcile_projection(
            operation_id=f"projection.stale-build.reconcile/{takeover_key}",
            realization_id=realization_id,
            ttl_seconds=ttl_seconds,
        )
        if cleaned.realization.state is not ProjectionRealizationState.CLEANED:
            raise RealmIntegrityError(
                "Stale projection build was not retired before takeover."
            )
        return True

    def _ensure_registered_root(self, *, require_active: bool = True) -> None:
        validate_projection_root(self._root_binding)
        binding = self._root_binding
        marker_digest = self._ledger.projection_root_marker_digest(
            projection_root_id=binding.projection_root_id,
            backend_kind=self._provider.PROVIDER_KIND,
            claim_nonce=binding.claim_nonce,
        )
        facts = {
            "projection_root_id": binding.projection_root_id,
            "canonical_path": str(binding.path),
            "backend_kind": self._provider.PROVIDER_KIND,
            "marker_digest": marker_digest,
            "claim_nonce": binding.claim_nonce,
            "device_id": binding.device_id,
            "inode": binding.inode,
        }
        principal_operation = request_digest(
            {
                "format": "optpilot.projection-maintenance-principal-register.v1",
                "realm_id": self._ledger.realm_id,
                "principal_id": self._maintenance_principal_id,
            }
        )
        self._ledger.register_principal(
            operation_id=f"projection.maintenance.principal/{principal_operation}",
            principal_id=self._maintenance_principal_id,
            kind="service",
        )
        try:
            record = self._ledger.validate_projection_root(**facts)
        except RealmNotFound:
            registration_key = request_digest(
                {"format": "optpilot.projection-root-registration.v1", **facts}
            )
            try:
                record = self._ledger.register_projection_root(
                    operation_id=f"projection.root.register/{registration_key}",
                    actor_principal_id=self._maintenance_principal_id,
                    **facts,
                )
            except RealmConflict:
                # A different principal/service may have registered the same
                # exact physical root between validation and registration.
                try:
                    record = self._ledger.validate_projection_root(**facts)
                except RealmNotFound as validation_error:
                    raise RealmStorageIdentityChanged(
                        "OptPilot cannot safely attach the local Realm read-only "
                        "projection storage because its durable claim marker or "
                        "registered ownership differs from this root. No files were "
                        "changed. This can happen after storage is partially copied, "
                        "restored, synchronized, or its claim is damaged. Use a new "
                        "empty OPTPILOT_REALM_ROOT on supported "
                        "local storage, or restore the exact registered storage "
                        "before reopening it."
                    ) from validation_error
        if require_active and record.state != "active":
            raise RealmConflict("Projection root is unavailable.")
        if record.registered_by_principal_id != self._maintenance_principal_id:
            raise RealmConflict(
                "Projection root is not owned by its maintenance principal."
            )

    def _maintenance_realization(
        self, realization_id: str
    ) -> ProjectionRealizationRecord:
        """Read one root-local realization without acquiring content authority."""

        records = self._ledger.list_projection_realizations(
            actor_principal_id=self._maintenance_principal_id,
            projection_root_id=self._root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        matches = tuple(
            record for record in records if record.realization_id == realization_id
        )
        if not matches:
            raise RealmNotFound("Entity not found.")
        if len(matches) != 1:  # pragma: no cover - primary key makes this impossible
            raise RealmIntegrityError("Projection realization identity is not unique.")
        return matches[0]

    @staticmethod
    def _validate_reconcile_identity(
        expected: ProjectionRealizationRecord,
        current: ProjectionRealizationRecord,
    ) -> None:
        fixed_fields = (
            "realization_id",
            "projection_root_id",
            "owner_id",
            "store_id",
            "spec",
            "spec_digest",
            "availability_resolution",
            "availability_resolution_digest",
            "request_digest",
            "provider_kind",
            "claim_nonce",
            "relative_name",
            "created_at",
        )
        forward_fill_fields = (
            "materialization_builder_lease_id",
            "wrapper_device_id",
            "wrapper_inode",
            "exposed_tree_device_id",
            "exposed_tree_inode",
            "plan_digest",
            "copied_logical_bytes",
            "copied_file_count",
        )
        if any(
            _thaw_mapping(getattr(current, name))
            != _thaw_mapping(getattr(expected, name))
            if name in {"spec", "availability_resolution"}
            else getattr(current, name) != getattr(expected, name)
            for name in fixed_fields
        ) or any(
            getattr(expected, name) is not None
            and getattr(current, name) != getattr(expected, name)
            for name in forward_fill_fields
        ):
            raise RealmIntegrityError(
                "Projection identity changed during reconciliation."
            )

    def _adopt_exact_cleaned(
        self,
        *,
        expected: ProjectionRealizationRecord,
        cleanup_token: str,
    ) -> ProjectionReconcileReceipt | None:
        """Converge when another exact root reconciler completed first."""

        current = self._maintenance_realization(expected.realization_id)
        self._validate_reconcile_identity(expected, current)
        if current.state is not ProjectionRealizationState.CLEANED:
            return None
        if current.cleanup_token != cleanup_token:
            raise RealmIntegrityError(
                "Cleaned projection has a different cleanup token."
            )
        complete_projection_cleanup_namespace(
            self._root_binding,
            self._namespace_claim(current),
            cleanup_token=cleanup_token,
        )
        return ProjectionReconcileReceipt(
            current, namespace_removed=False, already_complete=True
        )

    def _maintenance_cleanup_claim(
        self,
        *,
        record: ProjectionRealizationRecord,
        ttl_seconds: float,
    ) -> ProjectionClaimReceipt:
        """Claim or recover one cleanup term with replayable generation fences."""

        if record.state not in {
            ProjectionRealizationState.CLOSING,
            ProjectionRealizationState.CLEANING,
        }:
            raise RealmConflict("Projection is not available for cleanup.")
        key = _cleanup_key(record.realization_id)
        initial_operation = f"projection.maintenance.cleanup.claim/{key}"
        cleanup_token = self._maintenance_cleanup_token(record)
        initial_kwargs = {
            "operation_id": initial_operation,
            "actor_principal_id": self._maintenance_principal_id,
            "realization_id": record.realization_id,
            "owner_holder_id": f"projection-maintenance-owner-{key[:40]}",
            "owner_fencing_token": None,
            "builder_holder_id": f"projection-maintenance-cleaner-{key[:40]}",
            "builder_ttl_seconds": ttl_seconds,
            "cleanup_token": cleanup_token,
        }
        if record.state is ProjectionRealizationState.CLOSING:
            return self._ledger.claim_projection_cleanup(**initial_kwargs)

        # If the maintenance claim committed but its response was lost, the
        # operation ledger can recover the exact credentials even though the
        # realization is already CLEANING.
        try:
            return self._ledger.claim_projection_cleanup(**initial_kwargs)
        except RealmConflict:
            pass

        def reclaim(
            *, target_generation: int, expected_generation: int
        ) -> ProjectionClaimReceipt:
            operation = (
                f"projection.maintenance.cleanup.reclaim/{key}/"
                f"{target_generation}"
            )
            return self._ledger.reclaim_projection_cleanup(
                operation_id=operation,
                actor_principal_id=self._maintenance_principal_id,
                realization_id=record.realization_id,
                expected_owner_generation=expected_generation,
                owner_holder_id=(
                    f"projection-maintenance-owner-{key[:32]}-{target_generation}"
                ),
                builder_holder_id=(
                    f"projection-maintenance-cleaner-{key[:32]}-{target_generation}"
                ),
                builder_ttl_seconds=ttl_seconds,
                cleanup_token=cleanup_token,
            )

        generation = record.owner_generation
        if generation > 0:
            try:
                # Exact replay after a committed reclaim uses its resulting
                # generation.  The expected-generation guard prevents a new
                # operation from accidentally advancing this branch.
                return reclaim(
                    target_generation=generation,
                    expected_generation=generation - 1,
                )
            except RealmConflict:
                pass
        return reclaim(
            target_generation=generation + 1,
            expected_generation=generation,
        )

    @staticmethod
    def _maintenance_cleanup_token(record: ProjectionRealizationRecord) -> str:
        return record.cleanup_token or request_digest(
            {
                "format": "optpilot.projection-cleanup-token.v1",
                "realization_id": record.realization_id,
                "projection_root_id": record.projection_root_id,
            }
        )

    def _preflight_cleanup_identity(
        self,
        *,
        record: ProjectionRealizationRecord,
        claim: ProjectionNamespaceClaim,
        cleanup_token: str,
    ) -> ProjectionNamespaceIdentity:
        found = find_projection_wrapper_identity(
            self._root_binding,
            claim,
            directory_name=record.relative_name,
            cleanup_token=cleanup_token,
        )
        if found is None:  # pragma: no cover - cleanup lookup fails closed
            raise RealmIntegrityError(
                "Projection namespace is absent without exact retirement proof."
            )
        if (
            record.exposed_tree_device_id is not None
            and record.exposed_tree_inode is not None
            and (
                found.tree_device_id is None
                or found.tree_inode is None
            )
        ):
            raise RealmIntegrityError(
                "Projection tree disappeared before exact retirement."
            )
        # ``find_projection_wrapper_identity`` captures a live tree while
        # holding the root lock, or returns the exact identity from a durable
        # retirement proof after another reconciler removed the namespace.
        # Reopening the public name here would race that exact cleanup and
        # could misclassify successful idempotent replay as corruption.
        return found

    def _availability_resolution(
        self,
        store: LocalContentStore,
        roots: Tuple[SnapshotRef, ...],
        *,
        private_operation_digest: Optional[str] = None,
    ) -> dict[str, object]:
        resolution: dict[str, object] = {
            "format": _AVAILABILITY_FORMAT,
            "store_id": store.store_id,
            "backend_kind": store.BACKEND_KIND,
            "root_marker": store.root_marker,
            "snapshot_roots": [str(root) for root in roots],
        }
        if private_operation_digest is not None:
            resolution["realization_sharing"] = {
                "policy": "private",
                "operation_coordinate_digest": private_operation_digest,
            }
        return resolution

    def _semantic_request_digest(
        self, spec: ProjectionSpec, resolution: Mapping[str, Any]
    ) -> str:
        return request_digest(
            {
                "format": _REQUEST_FORMAT,
                "spec_digest": spec.digest,
                "availability_resolution_digest": request_digest(resolution),
                "provider_kind": self._provider.PROVIDER_KIND,
            }
        )

    def _quarantine_after_identity_failure(
        self,
        *,
        actor_principal_id: str,
        realization_id: str,
        error: BaseException,
    ) -> None:
        try:
            self._ledger.quarantine_projection_realization(
                operation_id=(
                    f"projection.quarantine/{_cleanup_key(realization_id)}/"
                    f"{request_digest({'actor': actor_principal_id, 'reason': str(error)})}"
                ),
                actor_principal_id=actor_principal_id,
                realization_id=realization_id,
                reason=str(error),
            )
        except BaseException as quarantine_error:
            error.add_note(
                f"projection realization quarantine also failed: {quarantine_error}"
            )

    def _namespace_claim(
        self, realization: ProjectionRealizationRecord
    ) -> ProjectionNamespaceClaim:
        return ProjectionNamespaceClaim(
            realm_id=self._root_binding.realm_id,
            projection_root_id=realization.projection_root_id,
            realization_id=realization.realization_id,
            claim_nonce=realization.claim_nonce,
        )

    def _current_namespace_identity(
        self, realization: ProjectionRealizationRecord
    ) -> ProjectionNamespaceIdentity:
        # Persisted descriptor numbers are historical observations only.  The
        # immutable claim marker selects the namespace across launches; this
        # attachment then captures fresh descriptor facts for all live fences.
        if realization.wrapper_inode is None:
            raise RealmIntegrityError("Projection realization has no wrapper claim.")
        return observe_ready_projection_namespace_identity(
            self._root_binding,
            self._namespace_claim(realization),
            directory_name=realization.relative_name,
        )

    def _forget(self, cache_key: str) -> None:
        with self._lock:
            self._active.pop(cache_key, None)


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Convert frozen JSON mappings/tuples into ordinary JSON containers."""

    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [thaw(child) for child in item]
        return item

    return thaw(value)


def _required_service_text(
    value: object, label: str, *, max_bytes: int = 4096
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8.") from error
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _positive_service_ttl(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("ttl_seconds must be a positive finite number.")
    return float(value)


def _realization_sharing_policy(value: object) -> str:
    if not isinstance(value, str) or value not in _REALIZATION_SHARING_POLICIES:
        raise ValueError("sharing_policy must be 'shared' or 'private'.")
    return value


def _require_private_operation_resolution(
    realization: ProjectionRealizationRecord,
    *,
    realm_id: str,
    expected_operation_id: str | None = None,
) -> None:
    """Reject shared or coordinate-substituted projection realizations."""

    if not isinstance(realization, ProjectionRealizationRecord):
        raise TypeError("realization must be a ProjectionRealizationRecord.")
    sharing = realization.availability_resolution.get("realization_sharing")
    if not isinstance(sharing, Mapping) or set(sharing) != {
        "policy",
        "operation_coordinate_digest",
    }:
        raise RealmConflict("Projection realization is not operation-private.")
    if sharing.get("policy") != "private":
        raise RealmConflict("Projection realization is not operation-private.")
    if expected_operation_id is not None:
        expected = request_digest(
            {
                "format": _PRIVATE_OPERATION_COORDINATE_FORMAT,
                "realm_id": realm_id,
                "operation_id": expected_operation_id,
            }
        )
        if sharing.get("operation_coordinate_digest") != expected:
            raise RealmConflict(
                "Private projection realization belongs to another operation."
            )


def _require_initial_ttl_if_unrenewed(
    lease: LeaseRecord, *, ttl_seconds: float, label: str
) -> None:
    """Check creation TTL only while the lease still exposes that duration."""

    if lease.heartbeat_revision != 0:
        return
    initial_ttl = lease.expires_at - lease.created_at
    if not math.isclose(initial_ttl, ttl_seconds, rel_tol=1e-9, abs_tol=1e-6):
        raise RealmConflict(f"Existing {label} has a different initial TTL.")


def _cleanup_key(realization_id: str) -> str:
    return request_digest(
        {
            "format": "optpilot.projection-maintenance-cleanup.v1",
            "realization_id": realization_id,
        }
    )


__all__ = [
    "ManagedReadOnlyProjection",
    "ProjectionReconcileOutcome",
    "ProjectionReconcileReceipt",
    "RealmProjectionService",
    "WorkspaceReadOnlyProjection",
]
