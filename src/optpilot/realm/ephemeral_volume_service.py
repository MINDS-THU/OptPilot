"""Realm-managed ephemeral writable-volume orchestration.

This service intentionally exposes no workspace semantics.  It binds a fresh
local writable directory to an existing runtime lease, validates that exact
lease and namespace before access, and turns release or expiry into durable,
replayable provider cleanup debt.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional

from .ephemeral_volume_namespace import (
    AttachedEphemeralVolumeNamespace,
    EphemeralVolumeNamespaceClaim,
    EphemeralVolumeNamespaceIdentity,
    EphemeralVolumeRootBinding,
    attach_ephemeral_volume_namespace,
    cleanup_ephemeral_volume_namespace,
    complete_ephemeral_volume_cleanup_namespace,
    create_ephemeral_volume_namespace,
    find_ephemeral_volume_namespace_identity,
    observe_active_ephemeral_volume_namespace_identity,
    prepare_ephemeral_volume_root,
    validate_ephemeral_volume_root,
)
from .ephemeral_volume_records import (
    EphemeralVolumeCleanupReceipt,
    EphemeralVolumeReceipt,
    EphemeralVolumeRecord,
    EphemeralVolumeState,
)
from .errors import (
    add_exception_note,
    RealmConflict,
    RealmError,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
    RealmStorageIdentityChanged,
)
from .filesystem_quota import FilesystemQuota
from .ledger import RealmLedger
from .leases import LeaseRecord
from .layered_volume_realization import (
    LocalLayeredVolumePlan,
    realize_local_layered_volume_plan,
    validate_local_layered_volume_plan,
)
from .refs import request_digest


_LEDGER_ID_NAMESPACE = uuid.UUID("a811e801-fdc1-43c8-b985-dcab229ffcea")
_INITIALIZATION_LOCK_GUARD = threading.Lock()


@dataclass
class _InitializationLockEntry:
    lock: threading.Lock
    users: int = 0


_INITIALIZATION_LOCKS: dict[tuple[object, ...], _InitializationLockEntry] = {}


class ManagedEphemeralVolume:
    """One exact writable namespace guarded by a child runtime lease.

    The lease is a cooperative authorization fence.  A returned host ``Path``
    is not an OS-revocable capability: trusted callers must stop the supervised
    writer before cleanup, and must not retain or reuse a path after validation
    fails.  A future execution binding must provide process/mount isolation
    before exposing this provider to untrusted code.
    """

    def __init__(
        self,
        *,
        service: "RealmEphemeralVolumeService",
        actor_principal_id: str,
        receipt: EphemeralVolumeReceipt,
        namespace: AttachedEphemeralVolumeNamespace,
        release_operation_id: str,
    ) -> None:
        self._service = service
        self._actor_principal_id = actor_principal_id
        self._receipt = receipt
        self._namespace = namespace
        self._release_operation_id = release_operation_id
        self._closed = False
        self._release_complete = False
        self._cleanup_complete = False
        self._required_initialization_proof: dict[str, object] | None = None
        self._lock = threading.RLock()

    @property
    def record(self) -> EphemeralVolumeRecord:
        return self._receipt.volume

    @property
    def lease(self) -> LeaseRecord:
        return self._receipt.usage_lease

    @property
    def path(self) -> Path:
        self.validate()
        return self._namespace.path

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def validate(self) -> None:
        with self._lock:
            self._validate_identity_only()
            try:
                self._validate_runtime_tree()
            except RealmIntegrityError as error:
                self._service._quarantine_after_identity_failure(
                    self.record.volume_id, error
                )
                raise

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> LeaseRecord:
        with self._lock:
            if self._closed:
                raise RealmExpired("Ephemeral volume is closed.")
            # Physical identity must be current before extending authority.
            # It is checked again after the commit to close the mutation race.
            try:
                self._validate_runtime_tree()
            except RealmIntegrityError as error:
                self._service._quarantine_after_identity_failure(
                    self.record.volume_id, error
                )
                raise
            heartbeat = self._service.ledger.heartbeat_ephemeral_volume(
                operation_id=operation_id,
                actor_principal_id=self._actor_principal_id,
                volume_id=self.record.volume_id,
                holder_id=self.lease.holder_id,
                fencing_token=self.lease.fencing_token,
                ttl_seconds=ttl_seconds,
            )
            # Operation replay recovers the committed receipt, not proof that
            # its parent authority is still current at this later instant.
            self._receipt = self._service.ledger.validate_ephemeral_volume(
                actor_principal_id=self._actor_principal_id,
                volume_id=heartbeat.volume.volume_id,
                holder_id=heartbeat.usage_lease.holder_id,
                fencing_token=heartbeat.usage_lease.fencing_token,
            )
            try:
                self._validate_runtime_tree()
            except RealmIntegrityError as error:
                self._service._quarantine_after_identity_failure(
                    self.record.volume_id, error
                )
                raise
            return self.lease

    def heartbeat_initialization(
        self, *, operation_id: str, ttl_seconds: float
    ) -> LeaseRecord:
        """Renew copy-time authority without scanning a transient seed tree.

        Initialization pulses mint a fresh operation id for every call.  The
        typed ledger heartbeat therefore performs the current-lease and
        ancestor checks itself; repeating those checks in separate ledger
        transactions would consume a substantial part of a short lease while
        several initialization resources are being renewed.
        """

        with self._lock:
            if self._closed:
                raise RealmExpired("Ephemeral volume is closed.")
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._service._quarantine_after_identity_failure(
                    self.record.volume_id, error
                )
                raise
            heartbeat = self._service.ledger.heartbeat_ephemeral_volume(
                operation_id=operation_id,
                actor_principal_id=self._actor_principal_id,
                volume_id=self.record.volume_id,
                holder_id=self.lease.holder_id,
                fencing_token=self.lease.fencing_token,
                ttl_seconds=ttl_seconds,
            )
            if heartbeat.usage_lease.expires_at <= time.time():
                raise RealmExpired(
                    "Ephemeral volume heartbeat returned an expired lease."
                )
            self._receipt = heartbeat
            try:
                self._namespace.validate()
            except RealmIntegrityError as error:
                self._service._quarantine_after_identity_failure(
                    self.record.volume_id, error
                )
                raise
            return self.lease

    def portable_record(self) -> dict[str, object]:
        return self.record.portable_record()

    def initialize_layered(
        self,
        *,
        source_root: Path,
        plan: LocalLayeredVolumePlan,
        initialization_identity: Mapping[str, object],
        authorize_publication: Callable[[], None],
        progress: Callable[[], None] | None = None,
    ) -> bool:
        """Realize immutable lowers once before this volume reaches a worker."""

        self._validate_initialization_arguments(plan, initialization_identity)
        if not callable(authorize_publication):
            raise TypeError("authorize_publication must be callable.")
        source_root = Path(source_root)
        if not source_root.is_absolute():
            raise ValueError("layered volume source_root must be absolute.")
        with self._lock:
            if self._closed:
                raise RealmExpired("Ephemeral volume is closed.")
            self._validate_identity_only()
            record = self.record
            identity = self._namespace.identity
            proof = self._layered_initialization_proof(
                plan=plan, initialization_identity=initialization_identity
            )
            lock_identity = (
                record.volume_root_id,
                record.volume_id,
                identity.wrapper_device_id,
                identity.wrapper_inode,
                identity.data_device_id,
                identity.data_inode,
            )
            with self._service._initialization_identity_lock(
                lock_identity, progress=progress
            ):
                initialized = self._namespace.initialize_once(
                    proof=proof,
                    realize=lambda destination_fd: realize_local_layered_volume_plan(
                        source_root,
                        destination_fd,
                        plan,
                        progress=progress,
                    ),
                    validate_existing=lambda destination_fd: (
                        validate_local_layered_volume_plan(
                            destination_fd,
                            plan,
                            progress=progress,
                        )
                    ),
                    authorize_publication=authorize_publication,
                    progress=progress,
                )
                self._required_initialization_proof = proof
                return initialized

    def require_layered_initialization(
        self,
        *,
        plan: LocalLayeredVolumePlan,
        initialization_identity: Mapping[str, object],
        progress: Callable[[], None] | None = None,
    ) -> None:
        """Authenticate committed initialization without reading mutable data."""

        self._validate_initialization_arguments(plan, initialization_identity)
        with self._lock:
            self._validate_identity_only()
            identity = self._namespace.identity
            proof = self._layered_initialization_proof(
                plan=plan, initialization_identity=initialization_identity
            )
            lock_identity = (
                self.record.volume_root_id,
                self.record.volume_id,
                identity.wrapper_device_id,
                identity.wrapper_inode,
                identity.data_device_id,
                identity.data_inode,
            )
            with self._service._initialization_identity_lock(
                lock_identity, progress=progress
            ):
                self._namespace.require_initialization_proof(
                    proof=proof, progress=progress
                )
            self._required_initialization_proof = proof

    def _validate_identity_only(self) -> None:
        if self._closed:
            raise RealmExpired("Ephemeral volume is closed.")
        self._receipt = self._service.ledger.validate_ephemeral_volume(
            actor_principal_id=self._actor_principal_id,
            volume_id=self.record.volume_id,
            holder_id=self.lease.holder_id,
            fencing_token=self.lease.fencing_token,
        )
        try:
            self._namespace.validate()
        except RealmIntegrityError as error:
            self._service._quarantine_after_identity_failure(
                self.record.volume_id, error
            )
            raise

    def _validate_runtime_tree(self) -> None:
        self._namespace.validate_quota(self.record.quota)
        if self._required_initialization_proof is not None:
            self._namespace.require_initialization_proof(
                proof=self._required_initialization_proof
            )

    @staticmethod
    def _validate_initialization_arguments(
        plan: LocalLayeredVolumePlan,
        initialization_identity: Mapping[str, object],
    ) -> None:
        if not isinstance(plan, LocalLayeredVolumePlan):
            raise TypeError("plan must be a LocalLayeredVolumePlan.")
        if not isinstance(initialization_identity, Mapping):
            raise TypeError("initialization_identity must be a mapping.")
        expected_identity_keys = {
            "lower_layers_digest",
            "portable_spec_digest",
            "projection_consumer_fencing_token",
            "projection_consumer_id",
            "projection_plan_digest",
            "projection_realization_id",
            "projection_spec_digest",
        }
        if set(initialization_identity) != expected_identity_keys:
            raise ValueError("layered volume initialization identity fields differ.")
        for name in (
            "lower_layers_digest",
            "portable_spec_digest",
            "projection_plan_digest",
            "projection_spec_digest",
        ):
            value = initialization_identity[name]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(
                    f"layered volume initialization {name} is invalid."
                )
        for name in ("projection_consumer_id", "projection_realization_id"):
            _required_text(
                initialization_identity[name],
                f"layered volume initialization {name}",
            )
        fencing = initialization_identity["projection_consumer_fencing_token"]
        if isinstance(fencing, bool) or not isinstance(fencing, int) or fencing <= 0:
            raise ValueError(
                "layered volume initialization projection fence is invalid."
            )

    def _layered_initialization_proof(
        self,
        *,
        plan: LocalLayeredVolumePlan,
        initialization_identity: Mapping[str, object],
    ) -> dict[str, object]:
        self._validate_initialization_arguments(plan, initialization_identity)
        identity = self._namespace.identity
        record = self.record
        return {
            "composition": {
                **dict(initialization_identity),
                "effective_tree_digest": plan.digest,
            },
            "format": "optpilot.local-layered-volume-initialization.v1",
            "volume": {
                "data_device_id": identity.data_device_id,
                "data_inode": identity.data_inode,
                "usage_fencing_token": self.lease.fencing_token,
                "usage_lease_id": self.lease.lease_id,
                "volume_id": record.volume_id,
                "volume_root_id": record.volume_root_id,
                "wrapper_device_id": identity.wrapper_device_id,
                "wrapper_inode": identity.wrapper_inode,
            },
        }

    def close(self) -> None:
        """Release and synchronously remove the exact local namespace.

        The ledger cannot reach ``cleaned`` until provider deletion succeeds.
        If deletion fails, the volume remains cleanup debt and the error is
        surfaced.  A later reconciler can safely resume.
        """

        with self._lock:
            if self._cleanup_complete:
                return
            if not self._release_complete:
                try:
                    self._receipt = self._service.ledger.release_ephemeral_volume(
                        operation_id=self._release_operation_id,
                        actor_principal_id=self._actor_principal_id,
                        volume_id=self.record.volume_id,
                        holder_id=self.lease.holder_id,
                        fencing_token=self.lease.fencing_token,
                    )
                except (RealmConflict, RealmExpired) as error:
                    # Exact authority is already unusable.  Reconciliation
                    # decides whether this is cleanup debt or quarantine.  A
                    # request-conflict while authority is still current is not
                    # release proof and must leave the attachment retryable.
                    try:
                        self._service.ledger.validate_ephemeral_volume(
                            actor_principal_id=self._actor_principal_id,
                            volume_id=self.record.volume_id,
                            holder_id=self.lease.holder_id,
                            fencing_token=self.lease.fencing_token,
                        )
                    except RealmError:
                        pass
                    else:
                        raise error
                except BaseException:
                    # A transient pre-commit failure must leave this handle
                    # retryable and attached.  A commit/response-loss retry is
                    # recovered by the exact ledger operation on next close.
                    raise
                self._release_complete = True
            if not self._closed:
                self._namespace.close()
                self._closed = True
            self._service.reconcile_volume(
                operation_id=f"{self._release_operation_id}/reconcile",
                volume_id=self.record.volume_id,
            )
            self._cleanup_complete = True

    def _detach_without_release(self) -> None:
        """Close only this process's namespace attachment during recovery."""

        with self._lock:
            if self._closed:
                return
            self._namespace.close()
            self._closed = True

    def __enter__(self) -> "ManagedEphemeralVolume":
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class EphemeralVolumeReconcileReceipt:
    volume: EphemeralVolumeRecord
    namespace_removed: bool
    already_complete: bool = False


@dataclass(frozen=True)
class EphemeralVolumeReconcileOutcome:
    volume_id: str
    receipt: Optional[EphemeralVolumeReconcileReceipt] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        succeeded = self.receipt is not None
        failed = self.error_type is not None and self.error_message is not None
        if succeeded == failed:
            raise ValueError("reconcile outcome requires one success or failure.")

    @property
    def ok(self) -> bool:
        return self.receipt is not None


class _CleanupHeartbeat:
    def __init__(
        self,
        *,
        service: "RealmEphemeralVolumeService",
        receipt: EphemeralVolumeCleanupReceipt,
        operation_prefix: str,
        ttl_seconds: float,
    ) -> None:
        self._service = service
        self._receipt = receipt
        self._operation_prefix = operation_prefix
        self._ttl_seconds = ttl_seconds
        self._interval = max(0.001, min(ttl_seconds / 3.0, 30.0))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"ephemeral-volume-cleaner-{receipt.volume.volume_id[-8:]}",
            daemon=True,
        )

    @property
    def receipt(self) -> EphemeralVolumeCleanupReceipt:
        with self._lock:
            return self._receipt

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RealmConflict("Ephemeral volume cleaner heartbeat failed.") from error

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(self._interval):
            index += 1
            current = self.receipt
            token = current.volume.cleanup_token
            if token is None:
                with self._lock:
                    self._error = RealmIntegrityError(
                        "Ephemeral volume cleanup token is missing."
                    )
                return
            try:
                updated = self._service.ledger.heartbeat_ephemeral_volume_cleanup(
                    operation_id=f"{self._operation_prefix}/{index}",
                    actor_principal_id=self._service.maintenance_principal_id,
                    volume_id=current.volume.volume_id,
                    cleaner_holder_id=current.cleanup_lease.holder_id,
                    cleaner_fencing_token=current.cleanup_lease.fencing_token,
                    cleanup_token=token,
                    ttl_seconds=self._ttl_seconds,
                )
            except BaseException as error:
                with self._lock:
                    self._error = error
                return
            with self._lock:
                self._receipt = updated


class RealmEphemeralVolumeService:
    """Allocate fresh writable directories and reconcile their cleanup debt."""

    def __init__(self, ledger: RealmLedger, *, volume_root: Path) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        self._ledger = ledger
        self._root_binding = prepare_ephemeral_volume_root(
            volume_root, realm_id=ledger.realm_id
        )
        principal_digest = request_digest(
            {
                "format": "optpilot.ephemeral-volume-maintainer.v1",
                "realm_id": ledger.realm_id,
                "volume_root_id": self._root_binding.volume_root_id,
            }
        )
        self._maintenance_principal_id = (
            f"ephemeral-volume-maintainer-{principal_digest[:40]}"
        )
        self._ensure_registered_root(require_active=False)

    @property
    def ledger(self) -> RealmLedger:
        return self._ledger

    @property
    def root_binding(self) -> EphemeralVolumeRootBinding:
        return self._root_binding

    @property
    def maintenance_principal_id(self) -> str:
        return self._maintenance_principal_id

    def create(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        parent_lease: LeaseRecord,
        holder_id: str,
        quota: FilesystemQuota,
        quota_enforcement: str = "advisory",
        ttl_seconds: float = 300,
    ) -> ManagedEphemeralVolume:
        _required_text(operation_id, "operation_id")
        _required_text(actor_principal_id, "actor_principal_id")
        _required_text(holder_id, "holder_id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        if not isinstance(parent_lease, LeaseRecord):
            raise TypeError("parent_lease must be a LeaseRecord.")
        if not isinstance(quota, FilesystemQuota):
            raise TypeError("quota must be FilesystemQuota.")
        if quota_enforcement != "advisory":
            raise ValueError(
                "local ephemeral volumes support advisory quota enforcement only."
            )
        self._ensure_registered_root()
        # The durable ledger operation is keyed only by the caller's public
        # operation id.  Its canonical request contains every other argument,
        # so changing TTL, holder, parent, actor, or root conflicts instead of
        # silently allocating a second volume.
        key, volume_id, usage_lease_id = _volume_operation_identity(operation_id)
        relative_name = f"volume-{key[:48]}"
        claim_nonce = request_digest(
            {
                "format": "optpilot.ephemeral-volume-claim-nonce.v1",
                "volume_id": volume_id,
                "volume_root_id": self._root_binding.volume_root_id,
            }
        )
        create_operation = f"ephemeral-volume.create/{key}"
        receipt = self._ledger.create_ephemeral_volume(
            operation_id=create_operation,
            actor_principal_id=actor_principal_id,
            volume_root_id=self._root_binding.volume_root_id,
            parent_lease_id=parent_lease.lease_id,
            parent_holder_id=parent_lease.holder_id,
            parent_fencing_token=parent_lease.fencing_token,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            provider_kind=self._root_binding.provider_kind,
            quota=quota,
            quota_enforcement=quota_enforcement,
            claim_nonce=claim_nonce,
            relative_name=relative_name,
            volume_id=volume_id,
            usage_lease_id=usage_lease_id,
        )
        current = self._ledger.read_ephemeral_volume(
            actor_principal_id=actor_principal_id,
            volume_id=receipt.volume.volume_id,
        )
        if current.state is EphemeralVolumeState.ALLOCATING:
            try:
                claim, identity = create_ephemeral_volume_namespace(
                    self._root_binding,
                    directory_name=current.relative_name,
                    volume_id=current.volume_id,
                    claim_nonce=current.claim_nonce,
                )
            except (RealmIntegrityError, RealmConflict) as error:
                # Another exact replay may have activated the namespace and
                # started using it after our stale ALLOCATING read.  Prove the
                # now-persisted active identity before treating nonempty data
                # as corruption.
                latest = self._ledger.read_ephemeral_volume(
                    actor_principal_id=actor_principal_id,
                    volume_id=current.volume_id,
                )
                if latest.state is not EphemeralVolumeState.ACTIVE:
                    self._quarantine_after_identity_failure(current.volume_id, error)
                    raise
                receipt = self._ledger.validate_ephemeral_volume(
                    actor_principal_id=actor_principal_id,
                    volume_id=current.volume_id,
                    holder_id=receipt.usage_lease.holder_id,
                    fencing_token=receipt.usage_lease.fencing_token,
                )
            else:
                try:
                    receipt = self._ledger.activate_ephemeral_volume(
                        operation_id=f"ephemeral-volume.activate/{key}",
                        actor_principal_id=actor_principal_id,
                        volume_id=current.volume_id,
                        holder_id=receipt.usage_lease.holder_id,
                        fencing_token=receipt.usage_lease.fencing_token,
                        wrapper_device_id=identity.wrapper_device_id,
                        wrapper_inode=identity.wrapper_inode,
                        data_device_id=_required_identity(identity.data_device_id),
                        data_inode=_required_identity(identity.data_inode),
                    )
                except RealmIntegrityError as error:
                    self._quarantine_after_identity_failure(current.volume_id, error)
                    raise
                except (RealmConflict, RealmExpired):
                    self._release_abandoned_create(
                        actor_principal_id=actor_principal_id,
                        receipt=receipt,
                        key=key,
                    )
                    raise
        elif current.state is EphemeralVolumeState.ACTIVE:
            receipt = self._ledger.validate_ephemeral_volume(
                actor_principal_id=actor_principal_id,
                volume_id=current.volume_id,
                holder_id=receipt.usage_lease.holder_id,
                fencing_token=receipt.usage_lease.fencing_token,
            )
            claim = self._claim(current)
            identity = self._current_identity(current)
        else:
            raise RealmConflict(
                "Ephemeral volume creation operation is no longer available."
            )
        if receipt.volume.state is EphemeralVolumeState.ACTIVE:
            claim = self._claim(receipt.volume)
            identity = self._current_identity(receipt.volume)
        try:
            namespace = attach_ephemeral_volume_namespace(
                self._root_binding, claim, identity
            )
            namespace.validate_quota(receipt.volume.quota)
        except (RealmIntegrityError, RealmConflict) as error:
            if "namespace" in locals():
                namespace.close()
            self._quarantine_after_identity_failure(receipt.volume.volume_id, error)
            raise
        return ManagedEphemeralVolume(
            service=self,
            actor_principal_id=actor_principal_id,
            receipt=receipt,
            namespace=namespace,
            release_operation_id=f"ephemeral-volume.release/{key}",
        )

    def recover_existing(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        parent_lease: LeaseRecord,
        holder_id: str,
        quota: FilesystemQuota,
        quota_enforcement: str = "advisory",
        ttl_seconds: float = 300,
    ) -> ManagedEphemeralVolume:
        """Recover the exact live volume selected by a public operation.

        Recovery never replays the creator's actor-bound create request and
        never lists a volume root.  The operation deterministically selects
        one volume, usage lease, and namespace claim; the current actor must
        then prove the exact live parent and every immutable volume policy
        field.  If the row is still allocating, recovery finishes the exact
        namespace publication and activation before attaching it.

        The original TTL is checked before the first usage heartbeat.  Once
        renewed, only current live authority can be reconstructed from the
        lease record, while the parent attempt remains the TTL policy source.
        """

        _required_text(operation_id, "operation_id")
        _required_text(actor_principal_id, "actor_principal_id")
        _required_text(holder_id, "holder_id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        if not isinstance(parent_lease, LeaseRecord):
            raise TypeError("parent_lease must be a LeaseRecord.")
        if not isinstance(quota, FilesystemQuota):
            raise TypeError("quota must be FilesystemQuota.")
        if quota_enforcement != "advisory":
            raise ValueError(
                "local ephemeral volumes support advisory quota enforcement only."
            )
        self._ensure_registered_root()
        current_parent = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=parent_lease.lease_id,
            holder_id=parent_lease.holder_id,
            fencing_token=parent_lease.fencing_token,
        )
        if not _same_lease_identity(current_parent, parent_lease):
            raise RealmConflict(
                "Ephemeral volume parent authority differs from the request."
            )

        key, volume_id, usage_lease_id = _volume_operation_identity(operation_id)
        relative_name = f"volume-{key[:48]}"
        claim_nonce = request_digest(
            {
                "format": "optpilot.ephemeral-volume-claim-nonce.v1",
                "volume_id": volume_id,
                "volume_root_id": self._root_binding.volume_root_id,
            }
        )
        record = self._ledger.read_ephemeral_volume(
            actor_principal_id=actor_principal_id,
            volume_id=volume_id,
        )
        if (
            record.state
            not in {EphemeralVolumeState.ALLOCATING, EphemeralVolumeState.ACTIVE}
            or record.volume_id != volume_id
            or record.volume_root_id != self._root_binding.volume_root_id
            or record.owner_id != current_parent.owner_id
            or record.parent_lease_id != current_parent.lease_id
            or record.usage_lease_id != usage_lease_id
            or record.provider_kind != self._root_binding.provider_kind
            or record.quota != quota
            or record.quota_enforcement != quota_enforcement
            or record.claim_nonce != claim_nonce
            or record.relative_name != relative_name
        ):
            raise RealmConflict(
                "Existing ephemeral volume differs from the requested semantics."
            )

        # A volume usage lease is create-once and cannot be replaced in place,
        # so its only current fencing token is the initial token.
        usage_lease = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=usage_lease_id,
            holder_id=holder_id,
            fencing_token=1,
        )
        expected_metadata = {
            "volume_id": volume_id,
            "volume_root_id": self._root_binding.volume_root_id,
        }
        if (
            usage_lease.owner_id != current_parent.owner_id
            or usage_lease.parent_lease_id != current_parent.lease_id
            or usage_lease.lease_kind != "ephemeral-volume"
            or usage_lease.audience != current_parent.audience
            or usage_lease.holder_id != holder_id
            or usage_lease.scope_key != f"ephemeral-volume:{volume_id}"
            or dict(usage_lease.metadata) != expected_metadata
        ):
            raise RealmIntegrityError(
                "Existing ephemeral volume usage authority is malformed."
            )
        _require_initial_ttl_if_unrenewed(
            usage_lease,
            ttl_seconds=ttl_seconds,
            label="ephemeral volume",
            parent_lease=current_parent,
        )
        if record.state is EphemeralVolumeState.ALLOCATING:
            receipt = self._activate_recovered_volume(
                actor_principal_id=actor_principal_id,
                key=key,
                record=record,
                usage_lease=usage_lease,
            )
            record = receipt.volume
        if record.state is not EphemeralVolumeState.ACTIVE:
            raise RealmConflict("Recovered ephemeral volume is not active.")
        return self.reattach(
            actor_principal_id=actor_principal_id,
            volume_id=volume_id,
            holder_id=holder_id,
            fencing_token=usage_lease.fencing_token,
        )

    def _activate_recovered_volume(
        self,
        *,
        actor_principal_id: str,
        key: str,
        record: EphemeralVolumeRecord,
        usage_lease: LeaseRecord,
    ) -> EphemeralVolumeReceipt:
        """Publish or recover one exact allocating namespace and activate it."""

        try:
            _claim, identity = create_ephemeral_volume_namespace(
                self._root_binding,
                directory_name=record.relative_name,
                volume_id=record.volume_id,
                claim_nonce=record.claim_nonce,
            )
        except (RealmIntegrityError, RealmConflict) as error:
            latest = self._ledger.read_ephemeral_volume(
                actor_principal_id=actor_principal_id,
                volume_id=record.volume_id,
            )
            if latest.state is not EphemeralVolumeState.ACTIVE:
                self._quarantine_after_identity_failure(record.volume_id, error)
                raise
            return self._ledger.validate_ephemeral_volume(
                actor_principal_id=actor_principal_id,
                volume_id=record.volume_id,
                holder_id=usage_lease.holder_id,
                fencing_token=usage_lease.fencing_token,
            )
        try:
            return self._ledger.activate_ephemeral_volume(
                operation_id=f"ephemeral-volume.activate/{key}",
                actor_principal_id=actor_principal_id,
                volume_id=record.volume_id,
                holder_id=usage_lease.holder_id,
                fencing_token=usage_lease.fencing_token,
                wrapper_device_id=identity.wrapper_device_id,
                wrapper_inode=identity.wrapper_inode,
                data_device_id=_required_identity(identity.data_device_id),
                data_inode=_required_identity(identity.data_inode),
            )
        except RealmConflict:
            # A concurrent authorized recovery may have committed activation
            # under another actor.  Only the exact now-active row is accepted.
            latest = self._ledger.read_ephemeral_volume(
                actor_principal_id=actor_principal_id,
                volume_id=record.volume_id,
            )
            if latest.state is not EphemeralVolumeState.ACTIVE:
                raise
            return self._ledger.validate_ephemeral_volume(
                actor_principal_id=actor_principal_id,
                volume_id=record.volume_id,
                holder_id=usage_lease.holder_id,
                fencing_token=usage_lease.fencing_token,
            )

    def reattach(
        self,
        *,
        actor_principal_id: str,
        volume_id: str,
        holder_id: str,
        fencing_token: int,
    ) -> ManagedEphemeralVolume:
        """Reopen one exact current volume without replaying its creator.

        The current actor is authorized afresh and must present the persisted
        holder/fence selected by the binding provider.  The service derives the
        namespace and release operation from trusted records; no host path,
        quota, parent lease, or provider identity is caller supplied.
        """

        _required_text(actor_principal_id, "actor_principal_id")
        _required_text(volume_id, "volume_id")
        _required_text(holder_id, "holder_id")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token must be a positive integer.")
        self._ensure_registered_root()
        receipt = self._ledger.validate_ephemeral_volume(
            actor_principal_id=actor_principal_id,
            volume_id=volume_id,
            holder_id=holder_id,
            fencing_token=fencing_token,
        )
        if (
            receipt.volume.volume_root_id != self._root_binding.volume_root_id
            or receipt.volume.provider_kind != self._root_binding.provider_kind
        ):
            raise RealmConflict(
                "Ephemeral volume belongs to a different physical provider."
            )
        claim = self._claim(receipt.volume)
        identity = self._current_identity(receipt.volume)
        try:
            namespace = attach_ephemeral_volume_namespace(
                self._root_binding, claim, identity
            )
            namespace.validate_quota(receipt.volume.quota)
        except (RealmIntegrityError, RealmConflict) as error:
            if "namespace" in locals():
                namespace.close()
            self._quarantine_after_identity_failure(volume_id, error)
            raise
        release_key = request_digest(
            {
                "format": "optpilot.ephemeral-volume-reattach-release.v1",
                "realm_id": self._ledger.realm_id,
                "actor_principal_id": actor_principal_id,
                "volume_root_id": self._root_binding.volume_root_id,
                "volume_id": volume_id,
                "usage_lease_id": receipt.usage_lease.lease_id,
            }
        )
        return ManagedEphemeralVolume(
            service=self,
            actor_principal_id=actor_principal_id,
            receipt=receipt,
            namespace=namespace,
            release_operation_id=f"ephemeral-volume.recovery.release/{release_key}",
        )

    def reconcile_volume(
        self,
        *,
        operation_id: str,
        volume_id: str,
        ttl_seconds: float = 300,
    ) -> EphemeralVolumeReconcileReceipt:
        _required_text(operation_id, "operation_id")
        _required_text(volume_id, "volume_id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        self._ensure_registered_root(require_active=False)
        targets = self._ledger.coordinate_ephemeral_volume_reconcile_request(
            operation_id=operation_id,
            actor_principal_id=self._maintenance_principal_id,
            volume_root_id=self._root_binding.volume_root_id,
            volume_id=volume_id,
        )
        if targets != (volume_id,):
            raise RealmIntegrityError(
                "Ephemeral volume reconciliation target receipt is malformed."
            )
        record = self._maintenance_volume(volume_id)
        if record.state is EphemeralVolumeState.QUARANTINED:
            raise RealmConflict(
                "Quarantined ephemeral volume requires explicit forensic resolution."
            )
        if record.state is EphemeralVolumeState.CLEANED:
            if record.cleanup_token is not None:
                complete_ephemeral_volume_cleanup_namespace(
                    self._root_binding,
                    self._claim(record),
                    cleanup_token=record.cleanup_token,
                )
            return EphemeralVolumeReconcileReceipt(
                record, namespace_removed=False, already_complete=True
            )
        cleanup_token = _cleanup_token(record)
        try:
            cleanup = self._cleanup_claim(record=record, ttl_seconds=ttl_seconds)
        except RealmError:
            adopted = self._adopt_exact_cleaned(
                expected=record, cleanup_token=cleanup_token
            )
            if adopted is not None:
                return adopted
            raise
        token = cleanup.volume.cleanup_token
        if token is None:
            raise RealmIntegrityError("Cleaning ephemeral volume has no cleanup token.")
        if token != cleanup_token:
            raise RealmIntegrityError(
                "Cleaning ephemeral volume has a different cleanup token."
            )
        claim = self._claim(cleanup.volume)
        try:
            identity = find_ephemeral_volume_namespace_identity(
                self._root_binding,
                claim,
                directory_name=cleanup.volume.relative_name,
                cleanup_token=token,
            )
        except RealmIntegrityError as error:
            adopted = self._adopt_exact_cleaned(
                expected=record, cleanup_token=token
            )
            if adopted is not None:
                return adopted
            self._quarantine_after_identity_failure(volume_id, error)
            raise
        heartbeat = _CleanupHeartbeat(
            service=self,
            receipt=cleanup,
            operation_prefix=(
                f"ephemeral-volume.cleanup.heartbeat/{_cleanup_key(volume_id)}/"
                f"{cleanup.volume.cleanup_generation}/"
                f"{cleanup.cleanup_lease.fencing_token}"
            ),
            ttl_seconds=ttl_seconds,
        )
        heartbeat.start()
        heartbeat_running = True
        namespace_removed = False
        try:
            try:
                if identity is not None:
                    namespace_removed = cleanup_ephemeral_volume_namespace(
                        self._root_binding,
                        claim,
                        identity,
                        cleanup_token=token,
                    )
            except RealmIntegrityError as error:
                adopted = self._adopt_exact_cleaned(
                    expected=record, cleanup_token=token
                )
                if adopted is not None:
                    return adopted
                self._quarantine_after_identity_failure(volume_id, error)
                raise
            try:
                heartbeat.raise_if_failed()
                current = heartbeat.receipt
                completed = self._ledger.complete_ephemeral_volume_cleanup(
                    operation_id=(
                        "ephemeral-volume.cleanup.complete/"
                        f"{_cleanup_key(volume_id)}"
                    ),
                    actor_principal_id=self._maintenance_principal_id,
                    volume_id=volume_id,
                    cleaner_holder_id=current.cleanup_lease.holder_id,
                    cleaner_fencing_token=current.cleanup_lease.fencing_token,
                    cleanup_token=token,
                )
            except RealmError:
                adopted = self._adopt_exact_cleaned(
                    expected=record, cleanup_token=token
                )
                if adopted is not None:
                    return adopted
                raise
            heartbeat.stop()
            heartbeat_running = False
        finally:
            if heartbeat_running:
                heartbeat.stop()
        complete_ephemeral_volume_cleanup_namespace(
            self._root_binding, claim, cleanup_token=token
        )
        return EphemeralVolumeReconcileReceipt(
            completed.volume,
            namespace_removed=namespace_removed,
            already_complete=False,
        )

    def reconcile_all(
        self,
        *,
        operation_id: str,
        ttl_seconds: float = 300,
    ) -> tuple[EphemeralVolumeReconcileOutcome, ...]:
        _required_text(operation_id, "operation_id")
        ttl_seconds = _positive_ttl(ttl_seconds)
        self._ensure_registered_root(require_active=False)
        targets = self._ledger.list_ephemeral_volume_cleanup_debt(
            operation_id=f"{operation_id}/debt",
            actor_principal_id=self._maintenance_principal_id,
            volume_root_id=self._root_binding.volume_root_id,
        )
        outcomes = []
        for volume_id in targets:
            try:
                receipt = self.reconcile_volume(
                    operation_id=f"{operation_id}/{volume_id}",
                    volume_id=volume_id,
                    ttl_seconds=ttl_seconds,
                )
            except BaseException as error:
                outcomes.append(
                    EphemeralVolumeReconcileOutcome(
                        volume_id=volume_id,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                )
            else:
                outcomes.append(
                    EphemeralVolumeReconcileOutcome(
                        volume_id=volume_id, receipt=receipt
                    )
                )
        return tuple(outcomes)

    def _ensure_registered_root(self, *, require_active: bool = True) -> None:
        validate_ephemeral_volume_root(self._root_binding)
        binding = self._root_binding
        marker_digest = self._ledger.ephemeral_volume_root_marker_digest(
            volume_root_id=binding.volume_root_id,
            backend_kind=binding.provider_kind,
            claim_nonce=binding.claim_nonce,
        )
        facts = {
            "volume_root_id": binding.volume_root_id,
            "canonical_path": str(binding.path),
            "backend_kind": binding.provider_kind,
            "marker_digest": marker_digest,
            "claim_nonce": binding.claim_nonce,
            "device_id": binding.device_id,
            "inode": binding.inode,
        }
        principal_key = request_digest(
            {
                "format": "optpilot.ephemeral-volume-maintainer-register.v1",
                "realm_id": self._ledger.realm_id,
                "principal_id": self._maintenance_principal_id,
            }
        )
        self._ledger.register_principal(
            operation_id=f"ephemeral-volume.maintenance.principal/{principal_key}",
            principal_id=self._maintenance_principal_id,
            kind="service",
        )
        try:
            root = self._ledger.validate_ephemeral_volume_root(**facts)
        except RealmNotFound:
            registration_key = request_digest(
                {"format": "optpilot.ephemeral-volume-root-register.v1", **facts}
            )
            try:
                root = self._ledger.register_ephemeral_volume_root(
                    operation_id=f"ephemeral-volume.root.register/{registration_key}",
                    actor_principal_id=self._maintenance_principal_id,
                    **facts,
                )
            except RealmConflict:
                try:
                    root = self._ledger.validate_ephemeral_volume_root(**facts)
                except RealmNotFound as validation_error:
                    raise RealmStorageIdentityChanged(
                        "OptPilot cannot safely attach the local Realm writable "
                        "storage because its durable claim marker or registered "
                        "ownership differs from this root. No files were changed. "
                        "This can happen after storage is partially copied, "
                        "restored, synchronized, or its claim is damaged. Use a new "
                        "empty OPTPILOT_REALM_ROOT on supported local storage, or "
                        "restore the exact registered storage before reopening it."
                    ) from validation_error
        if require_active and root.state != "active":
            raise RealmConflict("Ephemeral volume root is unavailable.")
        if root.registered_by_principal_id != self._maintenance_principal_id:
            raise RealmConflict(
                "Ephemeral volume root is not owned by its maintenance principal."
            )

    @contextmanager
    def _initialization_identity_lock(
        self,
        identity: tuple[object, ...],
        *,
        progress: Callable[[], None] | None,
    ) -> Iterator[None]:
        """Serialize same-process attachments before taking the wrapper flock."""

        with _INITIALIZATION_LOCK_GUARD:
            entry = _INITIALIZATION_LOCKS.get(identity)
            if entry is None:
                entry = _InitializationLockEntry(threading.Lock())
                _INITIALIZATION_LOCKS[identity] = entry
            entry.users += 1
        acquired = False
        try:
            while not acquired:
                acquired = entry.lock.acquire(timeout=0.001)
                if not acquired and progress is not None:
                    progress()
            yield
        finally:
            if acquired:
                entry.lock.release()
            with _INITIALIZATION_LOCK_GUARD:
                entry.users -= 1
                if entry.users == 0 and _INITIALIZATION_LOCKS.get(identity) is entry:
                    del _INITIALIZATION_LOCKS[identity]

    def _maintenance_volume(self, volume_id: str) -> EphemeralVolumeRecord:
        records = self._ledger.list_ephemeral_volumes(
            actor_principal_id=self._maintenance_principal_id,
            volume_root_id=self._root_binding.volume_root_id,
            states=tuple(EphemeralVolumeState),
        )
        matches = tuple(item for item in records if item.volume_id == volume_id)
        if not matches:
            raise RealmNotFound("Entity not found.")
        return matches[0]

    def _adopt_exact_cleaned(
        self,
        *,
        expected: EphemeralVolumeRecord,
        cleanup_token: str,
    ) -> EphemeralVolumeReconcileReceipt | None:
        """Converge when another exact root reconciler completed first."""

        current = self._maintenance_volume(expected.volume_id)
        fixed_fields = (
            "volume_id",
            "volume_root_id",
            "owner_id",
            "parent_lease_id",
            "usage_lease_id",
            "provider_kind",
            "quota",
            "quota_enforcement",
            "claim_nonce",
            "relative_name",
            "created_at",
        )
        optional_identity_fields = (
            "wrapper_device_id",
            "wrapper_inode",
            "data_device_id",
            "data_inode",
        )
        if any(
            getattr(current, name) != getattr(expected, name)
            for name in fixed_fields
        ) or any(
            getattr(expected, name) is not None
            and getattr(current, name) != getattr(expected, name)
            for name in optional_identity_fields
        ):
            raise RealmIntegrityError(
                "Ephemeral volume identity changed during reconciliation."
            )
        if current.state is not EphemeralVolumeState.CLEANED:
            return None
        if current.cleanup_token != cleanup_token:
            raise RealmIntegrityError(
                "Cleaned ephemeral volume has a different cleanup token."
            )
        complete_ephemeral_volume_cleanup_namespace(
            self._root_binding,
            self._claim(current),
            cleanup_token=cleanup_token,
        )
        return EphemeralVolumeReconcileReceipt(
            current, namespace_removed=False, already_complete=True
        )

    def _cleanup_claim(
        self,
        *,
        record: EphemeralVolumeRecord,
        ttl_seconds: float,
    ) -> EphemeralVolumeCleanupReceipt:
        key = _cleanup_key(record.volume_id)
        token = _cleanup_token(record)
        initial = {
            "operation_id": f"ephemeral-volume.cleanup.claim/{key}",
            "actor_principal_id": self._maintenance_principal_id,
            "volume_id": record.volume_id,
            "cleaner_holder_id": f"ephemeral-volume-cleaner-{key[:40]}",
            "cleaner_ttl_seconds": ttl_seconds,
            "cleanup_token": token,
        }
        if record.state is not EphemeralVolumeState.CLEANING:
            return self._ledger.claim_ephemeral_volume_cleanup(**initial)
        try:
            replay = self._ledger.claim_ephemeral_volume_cleanup(**initial)
            if _cleanup_receipt_current(replay):
                return replay
        except RealmConflict:
            pass

        def reclaim(
            *, target_generation: int, expected_generation: int
        ) -> EphemeralVolumeCleanupReceipt:
            return self._ledger.reclaim_ephemeral_volume_cleanup(
                operation_id=(
                    f"ephemeral-volume.cleanup.reclaim/{key}/{target_generation}"
                ),
                actor_principal_id=self._maintenance_principal_id,
                volume_id=record.volume_id,
                expected_cleanup_generation=expected_generation,
                cleaner_holder_id=(
                    f"ephemeral-volume-cleaner-{key[:30]}-{target_generation}"
                ),
                cleaner_ttl_seconds=ttl_seconds,
                cleanup_token=token,
            )

        generation = record.cleanup_generation
        if generation > 1:
            try:
                replay = reclaim(
                    target_generation=generation,
                    expected_generation=generation - 1,
                )
                if _cleanup_receipt_current(replay):
                    return replay
            except RealmConflict:
                pass
        return reclaim(
            target_generation=generation + 1,
            expected_generation=generation,
        )

    def _release_abandoned_create(
        self,
        *,
        actor_principal_id: str,
        receipt: EphemeralVolumeReceipt,
        key: str,
    ) -> None:
        try:
            self._ledger.release_ephemeral_volume(
                operation_id=f"ephemeral-volume.abandon/{key}",
                actor_principal_id=actor_principal_id,
                volume_id=receipt.volume.volume_id,
                holder_id=receipt.usage_lease.holder_id,
                fencing_token=receipt.usage_lease.fencing_token,
            )
        except BaseException:
            return

    def _quarantine_after_identity_failure(
        self, volume_id: str, error: BaseException
    ) -> None:
        try:
            self._ledger.quarantine_ephemeral_volume(
                operation_id=(
                    f"ephemeral-volume.quarantine/{_cleanup_key(volume_id)}/"
                    f"{request_digest({'reason': str(error)})}"
                ),
                actor_principal_id=self._maintenance_principal_id,
                volume_id=volume_id,
                reason=str(error),
            )
        except BaseException as quarantine_error:
            add_exception_note(error, 
                f"ephemeral volume quarantine also failed: {quarantine_error}"
            )

    def _claim(
        self, record: EphemeralVolumeRecord
    ) -> EphemeralVolumeNamespaceClaim:
        return EphemeralVolumeNamespaceClaim(
            self._ledger.realm_id,
            record.volume_root_id,
            record.volume_id,
            record.claim_nonce,
        )

    def _current_identity(
        self, record: EphemeralVolumeRecord
    ) -> EphemeralVolumeNamespaceIdentity:
        if (
            record.wrapper_device_id is None
            or record.wrapper_inode is None
            or record.data_device_id is None
            or record.data_inode is None
        ):
            raise RealmIntegrityError("Ephemeral volume namespace identity is incomplete.")
        return observe_active_ephemeral_volume_namespace_identity(
            self._root_binding,
            self._claim(record),
            directory_name=record.relative_name,
        )


def _cleanup_key(volume_id: str) -> str:
    return request_digest(
        {"format": "optpilot.ephemeral-volume-cleanup-key.v1", "volume_id": volume_id}
    )


def _cleanup_token(record: EphemeralVolumeRecord) -> str:
    return record.cleanup_token or request_digest(
        {
            "format": "optpilot.ephemeral-volume-cleanup-token.v1",
            "volume_id": record.volume_id,
            "volume_root_id": record.volume_root_id,
        }
    )


def _volume_operation_identity(operation_id: str) -> tuple[str, str, str]:
    """Derive the exact public volume, create operation, and usage lease ids."""

    key = request_digest(
        {
            "format": "optpilot.ephemeral-volume-public-operation.v1",
            "operation_id": operation_id,
        }
    )
    volume_id = f"ephemeral-volume-{key[:48]}"
    create_operation_id = f"ephemeral-volume.create/{key}"
    usage_lease_id = (
        "ephemeral-volume-lease-"
        f"{uuid.uuid5(_LEDGER_ID_NAMESPACE, create_operation_id).hex}"
    )
    return key, volume_id, usage_lease_id


def _same_lease_identity(current: LeaseRecord, expected: LeaseRecord) -> bool:
    """Compare immutable lease identity while allowing heartbeat facts to move."""

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


def _require_initial_ttl_if_unrenewed(
    lease: LeaseRecord,
    *,
    ttl_seconds: float,
    label: str,
    parent_lease: LeaseRecord,
) -> None:
    if lease.heartbeat_revision != 0 or parent_lease.heartbeat_revision != 0:
        return
    expected_expiry = min(
        lease.created_at + ttl_seconds,
        parent_lease.expires_at,
    )
    if not math.isclose(
        lease.expires_at, expected_expiry, rel_tol=1e-9, abs_tol=1e-6
    ):
        raise RealmConflict(f"Existing {label} has a different initial TTL.")


def _required_identity(value: Optional[int]) -> int:
    if value is None:
        raise RealmIntegrityError("Ephemeral volume namespace identity is incomplete.")
    return value


def _cleanup_receipt_current(receipt: EphemeralVolumeCleanupReceipt) -> bool:
    return (
        receipt.cleanup_lease.state.value == "active"
        and receipt.cleanup_lease.expires_at > time.time()
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text.")
    return value


def _positive_ttl(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("ttl_seconds must be positive.")
    return float(value)


__all__ = [
    "EphemeralVolumeReconcileOutcome",
    "EphemeralVolumeReconcileReceipt",
    "ManagedEphemeralVolume",
    "RealmEphemeralVolumeService",
]
