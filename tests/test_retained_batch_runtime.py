from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from optpilot.method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from optpilot.method_launch_environment import MethodLaunchEnvironment
from optpilot.realm.ephemeral_volume_records import EphemeralVolumeState
from optpilot.realm.ephemeral_volume_service import _volume_operation_identity
from optpilot.realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from optpilot.realm.leases import LeaseRecord, LeaseState
from optpilot.realm.local_process_supervisor import (
    ProcessLaunchPrivateEnvironment,
    ProcessLaunchRequest,
    ProcessLaunchReservation,
    ProcessLaunchSealReceipt,
    WorkerStarted,
    WorkerTerminalProof,
)
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.projection_records import ProjectionRealizationState
from optpilot.realm.refs import canonical_json_bytes, request_digest
from optpilot.realm.run_closure import ScopeLayer, ScopePath
from optpilot.realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
)
from optpilot.realm.run_snapshot import RunLedgerSnapshot
from optpilot.retained_batch_runtime import (
    RetainedBatchExchangeCoordinate,
    RetainedBatchMethodError,
    RetainedBatchProtocolError,
    RetainedBatchRuntimeError,
    RetainedBatchRuntimeProvider,
    _SocketIdentity,
    _build_projection_binding,
    _report_cleanup_diagnostic,
    retained_batch_worker_request_digest,
    _worker_coordinates,
)
from optpilot.retained_batch_worker import (
    BATCH_RESPONSE_SCHEMA,
    INITIAL_BATCH_EXCHANGE_CHAIN,
    MAX_BATCH_DURABLE_RESPONSE_BYTES,
    MAX_UNIX_SOCKET_PATH_BYTES,
    RetainedBatchWorkerInit,
    RetainedPythonBatchEngine,
    retained_batch_exchange_chain_digest,
)
from optpilot.retained_study_compiler import compile_retained_process_study
from tests.test_retained_batch_worker import _context_definition, _definition
from tests.test_retained_study_compiler import (
    _file_study,
    _manifest,
    _package,
    _provider,
    _study,
)


def _lease(
    *,
    lease_id: str,
    owner_id: str = "run-owner",
    holder_id: str = "controller-holder",
    lease_kind: str = "run-controller",
    audience: str = "realm-ledger",
    scope_key: str = "run:run-one",
    parent_lease_id: str | None = None,
    generation: int = 1,
    updated_at: float = 1.0,
    state: LeaseState = LeaseState.ACTIVE,
    metadata: dict[str, Any] | None = None,
) -> LeaseRecord:
    return LeaseRecord(
        lease_id=lease_id,
        owner_id=owner_id,
        parent_lease_id=parent_lease_id,
        lease_kind=lease_kind,
        audience=audience,
        holder_id=holder_id,
        scope_key=scope_key,
        fencing_token=1,
        heartbeat_revision=0,
        state=state,
        expires_at=updated_at + 300.0,
        created_at=1.0,
        updated_at=updated_at,
        metadata=(
            {"controller_generation": generation, "run_id": "run-one"}
            if metadata is None
            else metadata
        ),
    )


def _snapshot(
    definition: Any,
    *,
    generation: int = 1,
    run_state: str = "running",
    current_revision: int | None = None,
    submission_state: str | None = None,
    stop_code: str | None = None,
    method_exchange_preparations: tuple[Any, ...] = (),
    method_exchange_completions: tuple[Any, ...] = (),
) -> RunLedgerSnapshot:
    lease = _lease(
        lease_id=f"controller-{generation}", generation=generation
    )
    result = object.__new__(RunLedgerSnapshot)
    revision = generation if current_revision is None else current_revision
    object.__setattr__(
        result,
        "run",
        SimpleNamespace(
            run_id="run-one",
            owner_id="run-owner",
            state=run_state,
            retention_state="active",
            controller_generation=generation,
            current_revision=revision,
        ),
    )
    object.__setattr__(
        result,
        "controller_term",
        SimpleNamespace(
            generation=generation,
            lease_id=lease.lease_id,
            holder_id=lease.holder_id,
            fencing_token=lease.fencing_token,
        ),
    )
    object.__setattr__(result, "controller_lease", lease)
    object.__setattr__(result, "definition", definition)
    object.__setattr__(
        result,
        "revision",
        SimpleNamespace(revision=revision, last_sequence=revision),
    )
    selected_submission = submission_state or (
        "accepting" if run_state == "running" else "terminal"
    )
    object.__setattr__(
        result,
        "control",
        SimpleNamespace(
            current_submission=SimpleNamespace(
                state=selected_submission,
                stop_code=stop_code,
                run_revision=revision,
            )
        ),
    )
    object.__setattr__(
        result,
        "method_exchange_preparations",
        method_exchange_preparations,
    )
    object.__setattr__(
        result,
        "method_exchange_completions",
        method_exchange_completions,
    )
    object.__setattr__(
        result,
        "finalization",
        None
        if run_state == "running"
        else SimpleNamespace(run_id="run-one", terminal_state=run_state),
    )
    return result


def _memberships(definition: Any) -> tuple[OwnerMembership, ...]:
    values = [
        OwnerMembership("test-store", layer.snapshot_ref, RUN_METHOD_SOURCE_ROLE)
        for layer in definition.method_revision.source_layers
    ]
    values.extend(
        OwnerMembership(
            "test-store",
            layer.snapshot_ref,
            RUN_PREPARED_METHOD_RUNTIME_ROLE,
        )
        for layer in definition.prepared_method_runtime.prepared_layers
    )
    return tuple(sorted(set(values)))


def _file_definition() -> Any:
    return compile_retained_process_study(
        _file_study(),
        package=_package(),
        package_manifest=_manifest(),
        provider=_provider(),
        target_owner_id="retained-batch-file-cleanup-definition",
    ).run_definition


class _FakeLedger:
    def __init__(self, snapshot: RunLedgerSnapshot) -> None:
        self.realm_id = "realm-for-retained-batch-tests"
        self.current = snapshot
        self.memberships = _memberships(snapshot.definition)
        self.leases: dict[str, LeaseRecord] = {
            snapshot.controller_lease.lease_id: snapshot.controller_lease
        }
        self.projection_service: _FakeProjectionService | None = None
        self.volume_service: _FakeVolumeService | None = None

    def read_run_snapshot(self, **_kwargs: Any) -> RunLedgerSnapshot:
        return self.current

    def list_owner_memberships(self, **_kwargs: Any):
        return self.memberships

    def list_projection_realizations(self, **kwargs: Any):
        assert self.projection_service is not None
        states = set(kwargs["states"])
        return tuple(
            record
            for record in self.projection_service.records
            if record.state in states
        )

    def list_projection_consumers(self, **kwargs: Any):
        assert self.projection_service is not None
        consumer = self.projection_service.consumers.get(kwargs["realization_id"])
        return () if consumer is None else (consumer,)

    def read_projection_consumer_authority(self, **kwargs: Any):
        assert self.projection_service is not None
        record = next(
            item
            for item in self.projection_service.records
            if item.realization_id == kwargs["realization_id"]
        )
        consumer = self.projection_service.consumers[record.realization_id]
        if consumer.consumer_id != kwargs["consumer_id"]:
            raise RealmNotFound("absent")
        return SimpleNamespace(
            realization=record,
            consumer=consumer,
            consumer_lease=self.leases[consumer.lease_id],
        )

    def read_run_controller_term_authority(self, **kwargs: Any):
        generation = kwargs["generation"]
        lease_id = f"controller-{generation}"
        lease = self.leases.get(lease_id)
        if lease is None:
            if generation == self.current.controller_term.generation:
                lease = self.current.controller_lease
            else:
                lease = _lease(lease_id=lease_id, generation=generation)
            self.leases[lease_id] = lease
        return SimpleNamespace(controller_lease=lease)

    def validate_lease(self, **kwargs: Any) -> LeaseRecord:
        try:
            lease = self.leases[kwargs["lease_id"]]
        except KeyError as error:
            raise RealmNotFound("absent") from error
        if (
            lease.holder_id != kwargs["holder_id"]
            or lease.fencing_token != kwargs["fencing_token"]
            or lease.state is not LeaseState.ACTIVE
        ):
            raise RealmConflict("lease differs")
        return lease


class _FakeProjection:
    def __init__(
        self,
        service: "_FakeProjectionService",
        root: Path,
        log: list[str],
        name: str,
        realization_id: str,
        consumer_id: str,
        lease: LeaseRecord,
    ) -> None:
        self._service = service
        self._root = root
        self._log = log
        self._name = name
        self.realization_id = realization_id
        self.consumer_id = consumer_id
        self.consumer_lease = lease
        self._closed = False
        self._lease = lease

    @property
    def root_path(self) -> Path:
        return self._root

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> LeaseRecord:
        self._log.append(f"projection.heartbeat:{operation_id}")
        return replace(
            self._lease,
            heartbeat_revision=self._lease.heartbeat_revision + 1,
            expires_at=self._lease.updated_at + ttl_seconds,
        )

    def close(self) -> None:
        self._log.append(f"projection.close:{self._name}")
        self._closed = True


class _FakeProjectionService:
    def __init__(self, ledger: _FakeLedger, root: Path, log: list[str]) -> None:
        self.ledger = ledger
        self.available_store_ids = ("test-store",)
        self._root = root
        self._log = log
        self._specs: dict[str, Any] = {}
        self._operations: dict[str, str] = {}
        self._count = 0
        self._maintenance_principal_id = "fake-projection-maintenance"
        self._root_binding = SimpleNamespace(
            projection_root_id="fake-projection-root",
            path=root.parents[1],
        )
        self._provider = SimpleNamespace(PROVIDER_KIND="fake-projection")
        self.records: list[Any] = []
        self.consumers: dict[str, Any] = {}
        self.fail_reconcile_once = False
        ledger.projection_service = self

    def _ensure_registered_root(self, *, require_active: bool = True) -> None:
        return None

    def _current_namespace_identity(self, record: Any) -> Any:
        wrapper = self._root_binding.path / record.relative_name
        tree = wrapper / "root"
        wrapper_metadata = wrapper.lstat()
        tree_metadata = tree.lstat()
        return SimpleNamespace(
            wrapper_device_id=wrapper_metadata.st_dev,
            wrapper_inode=wrapper_metadata.st_ino,
            tree_device_id=tree_metadata.st_dev,
            tree_inode=tree_metadata.st_ino,
        )

    def _handle(self, realization_id: str) -> _FakeProjection:
        consumer = self.consumers[realization_id]
        lease = self.ledger.leases[consumer.lease_id]
        self._count += 1
        return _FakeProjection(
            self,
            self._root,
            self._log,
            f"p{self._count}",
            realization_id,
            consumer.consumer_id,
            lease,
        )

    def recover_existing_private_read_only(self, **kwargs: Any) -> _FakeProjection:
        operation = kwargs["operation_id"]
        if operation not in self._specs:
            raise RealmNotFound("absent")
        if self._specs[operation] != kwargs["spec"]:
            raise RealmConflict("changed projection spec")
        self._log.append("projection.recover")
        return self._handle(self._operations[operation])

    def project_read_only(self, **kwargs: Any) -> _FakeProjection:
        operation = kwargs["operation_id"]
        self._specs[operation] = kwargs["spec"]
        realization_id = f"projection-realization-{len(self.records) + 1}"
        consumer_id = f"projection-consumer-{len(self.records) + 1}"
        owner_lease_id = f"projection-owner-{len(self.records) + 1}"
        consumer_lease_id = f"projection-lease-{len(self.records) + 1}"
        wrapper = self._root.parent.lstat()
        tree = self._root.lstat()
        operation_coordinate = request_digest(
            {
                "format": "optpilot.projection-private-operation-coordinate.v1",
                "realm_id": self.ledger.realm_id,
                "operation_id": operation,
            }
        )
        availability = {
            "backend_kind": "fake-content",
            "format": "optpilot.projection-availability.v1",
            "realization_sharing": {
                "operation_coordinate_digest": operation_coordinate,
                "policy": "private",
            },
            "root_marker": "fake-root-marker",
            "snapshot_roots": [],
            "store_id": kwargs["store_id"],
        }
        semantic_digest = request_digest(
            {
                "format": "optpilot.projection-request.v1",
                "spec_digest": kwargs["spec"].digest,
                "availability_resolution_digest": request_digest(availability),
                "provider_kind": self._provider.PROVIDER_KIND,
            }
        )
        record = SimpleNamespace(
            realization_id=realization_id,
            projection_root_id=self._root_binding.projection_root_id,
            owner_id=kwargs["spec"].owner_id,
            store_id=kwargs["store_id"],
            spec=kwargs["spec"].to_dict(),
            spec_digest=kwargs["spec"].digest,
            availability_resolution=availability,
            request_digest=semantic_digest,
            provider_kind=self._provider.PROVIDER_KIND,
            relative_name=self._root.parent.name,
            state=ProjectionRealizationState.READY,
            owner_lease_id=owner_lease_id,
            wrapper_device_id=wrapper.st_dev,
            wrapper_inode=wrapper.st_ino,
            exposed_tree_device_id=tree.st_dev,
            exposed_tree_inode=tree.st_ino,
            created_at=float(len(self.records) + 1),
        )
        consumer = SimpleNamespace(
            consumer_id=consumer_id,
            realization_id=realization_id,
            lease_id=consumer_lease_id,
            consumer_kind=kwargs["consumer_kind"],
            metadata=dict(kwargs["consumer_metadata"]),
        )
        lease = _lease(
            lease_id=consumer_lease_id,
            owner_id=kwargs["spec"].owner_id,
            parent_lease_id=owner_lease_id,
            holder_id=kwargs["holder_id"],
            lease_kind="projection-consumer",
            audience="runtime",
            scope_key=f"projection-consumer:{realization_id}:{consumer_id}",
            metadata={
                "consumer_id": consumer_id,
                "consumer_kind": kwargs["consumer_kind"],
                "realization_id": realization_id,
            },
        )
        self.records.append(record)
        self.consumers[realization_id] = consumer
        self.ledger.leases[consumer_lease_id] = lease
        self._operations[operation] = realization_id
        self._log.append("projection.create")
        return self._handle(realization_id)

    def reattach_private_read_only_consumer(self, **kwargs: Any) -> _FakeProjection:
        realization_id = kwargs["realization_id"]
        consumer = self.consumers[realization_id]
        if consumer.consumer_id != kwargs["consumer_id"]:
            raise RealmConflict("consumer differs")
        lease = self.ledger.leases[consumer.lease_id]
        if lease.state is not LeaseState.ACTIVE:
            raise RealmExpired("consumer authority is no longer active")
        return self._handle(realization_id)

    def retire_private_projection(
        self, projection: _FakeProjection, *, ttl_seconds: float
    ) -> Any:
        record = next(
            item
            for item in self.records
            if item.realization_id == projection.realization_id
        )
        record.state = ProjectionRealizationState.CLEANED
        self.consumers.pop(record.realization_id, None)
        self._log.append(f"projection.retire:{record.realization_id}")
        return record

    def retire_private_projection_operation(self, **kwargs: Any) -> Any:
        record = next(
            item
            for item in self.records
            if item.realization_id == kwargs["realization_id"]
        )
        sharing = record.availability_resolution["realization_sharing"]
        consumer = self.consumers[record.realization_id]
        lease = self.ledger.leases[consumer.lease_id]
        if (
            record.owner_id != kwargs["expected_owner_id"]
            or record.store_id != kwargs["expected_store_id"]
            or record.spec != kwargs["expected_spec"].to_dict()
            or sharing["operation_coordinate_digest"]
            != kwargs["expected_operation_coordinate_digest"]
            or lease.holder_id != kwargs["expected_consumer_holder_id"]
            or consumer.consumer_kind != kwargs["expected_consumer_kind"]
            or consumer.metadata != kwargs["expected_consumer_metadata"]
        ):
            raise RealmConflict("private projection operation differs")
        record.state = ProjectionRealizationState.CLEANED
        self.consumers.pop(record.realization_id, None)
        self._log.append(f"projection.retire-operation:{record.realization_id}")
        return record

    def reconcile_projection(self, **kwargs: Any) -> Any:
        if self.fail_reconcile_once:
            self.fail_reconcile_once = False
            raise OSError("/private/projection-cleanup")
        record = next(
            item
            for item in self.records
            if item.realization_id == kwargs["realization_id"]
        )
        record.state = ProjectionRealizationState.CLEANED
        self.consumers.pop(record.realization_id, None)
        self._log.append(f"projection.reconcile:{record.realization_id}")
        return SimpleNamespace(realization=record)


class _FakeVolume:
    def __init__(
        self,
        service: "_FakeVolumeService",
        path: Path,
        log: list[str],
        name: str,
        volume_id: str,
        lease: LeaseRecord,
    ) -> None:
        self._service = service
        self._path = path
        self._log = log
        self._name = name
        self._volume_id = volume_id
        self.fail_heartbeat = False
        self._lease = lease

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record(self) -> Any:
        return self._service.records[self._volume_id]

    @property
    def lease(self) -> LeaseRecord:
        return self._lease

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> LeaseRecord:
        self._log.append(f"volume.heartbeat:{operation_id}")
        if self.fail_heartbeat:
            raise ValueError("/private/provider/control-volume")
        return replace(
            self._lease,
            heartbeat_revision=self._lease.heartbeat_revision + 1,
            expires_at=self._lease.updated_at + ttl_seconds,
        )

    def close(self) -> None:
        self._log.append(f"volume.close:{self._name}")
        if self._service.fail_close_once:
            self._service.fail_close_once = False
            raise OSError("/private/provider/control-volume")
        self._service.records[self._volume_id].state = EphemeralVolumeState.CLEANED


class _FakeVolumeService:
    def __init__(self, ledger: _FakeLedger, root: Path, log: list[str]) -> None:
        self.ledger = ledger
        self._root = root
        self._root.mkdir()
        self._log = log
        self._operations: set[str] = set()
        self._operation_ids: dict[str, str] = {}
        self.records: dict[str, Any] = {}
        self._count = 0
        self.handles: list[_FakeVolume] = []
        self.fail_close_once = False
        self.fail_reconcile_once = False
        self._root_binding = SimpleNamespace(
            volume_root_id="fake-volume-root",
            provider_kind="fake-volume",
            path=root,
        )
        ledger.volume_service = self

    def _ensure_registered_root(self, *, require_active: bool = True) -> None:
        return None

    def _current_identity(self, record: Any) -> Any:
        wrapper = self._root / record.relative_name
        data = wrapper / "data"
        wrapper_metadata = wrapper.lstat()
        data_metadata = data.lstat()
        return SimpleNamespace(
            wrapper_device_id=wrapper_metadata.st_dev,
            wrapper_inode=wrapper_metadata.st_ino,
            data_device_id=data_metadata.st_dev,
            data_inode=data_metadata.st_ino,
        )

    def _handle(self, volume_id: str) -> _FakeVolume:
        record = self.records[volume_id]
        lease = self.ledger.leases[record.usage_lease_id]
        self._count += 1
        handle = _FakeVolume(
            self,
            self._root / record.relative_name / "data",
            self._log,
            f"v{self._count}",
            volume_id,
            lease,
        )
        self.handles.append(handle)
        return handle

    def recover_existing(self, **kwargs: Any) -> _FakeVolume:
        operation = kwargs["operation_id"]
        if operation not in self._operations:
            raise RealmNotFound("absent")
        self._log.append("volume.recover")
        return self._handle(self._operation_ids[operation])

    def create(self, **kwargs: Any) -> _FakeVolume:
        operation = kwargs["operation_id"]
        self._operations.add(operation)
        key, volume_id, usage_lease_id = _volume_operation_identity(operation)
        relative_name = f"volume-{key[:48]}"
        wrapper = self._root / relative_name
        data = wrapper / "data"
        data.mkdir(parents=True, exist_ok=True)
        wrapper_metadata = wrapper.lstat()
        data_metadata = data.lstat()
        record = SimpleNamespace(
            volume_id=volume_id,
            volume_root_id=self._root_binding.volume_root_id,
            owner_id=kwargs["parent_lease"].owner_id,
            parent_lease_id=kwargs["parent_lease"].lease_id,
            usage_lease_id=usage_lease_id,
            provider_kind=self._root_binding.provider_kind,
            quota=kwargs["quota"],
            quota_enforcement=kwargs["quota_enforcement"],
            claim_nonce=request_digest(
                {
                    "format": "optpilot.ephemeral-volume-claim-nonce.v1",
                    "volume_id": volume_id,
                    "volume_root_id": self._root_binding.volume_root_id,
                }
            ),
            relative_name=relative_name,
            state=EphemeralVolumeState.ACTIVE,
            wrapper_device_id=wrapper_metadata.st_dev,
            wrapper_inode=wrapper_metadata.st_ino,
            data_device_id=data_metadata.st_dev,
            data_inode=data_metadata.st_ino,
        )
        lease = _lease(
            lease_id=usage_lease_id,
            owner_id=kwargs["parent_lease"].owner_id,
            parent_lease_id=kwargs["parent_lease"].lease_id,
            holder_id=kwargs["holder_id"],
            lease_kind="ephemeral-volume",
            audience=kwargs["parent_lease"].audience,
            scope_key=f"ephemeral-volume:{volume_id}",
            metadata={
                "volume_id": volume_id,
                "volume_root_id": self._root_binding.volume_root_id,
            },
        )
        self.records[volume_id] = record
        self.ledger.leases[usage_lease_id] = lease
        self._operation_ids[operation] = volume_id
        self._log.append("volume.create")
        return self._handle(volume_id)

    def _maintenance_volume(self, volume_id: str) -> Any:
        try:
            return self.records[volume_id]
        except KeyError as error:
            raise RealmNotFound("absent") from error

    def reconcile_volume(self, **kwargs: Any) -> Any:
        if self.fail_reconcile_once:
            self.fail_reconcile_once = False
            raise OSError("/private/provider/control-volume")
        record = self._maintenance_volume(kwargs["volume_id"])
        record.state = EphemeralVolumeState.CLEANED
        self._log.append(f"volume.reconcile:{record.volume_id}")
        return SimpleNamespace(volume=record)


class _FakeProcess:
    def __init__(self, supervisor: "_FakeSupervisor", token: str) -> None:
        self.supervisor = supervisor
        self.token = token

    def wait_started(self, timeout: float | None = None):
        self.supervisor.log.append(f"process.wait_started:{self.token}")
        row = self.supervisor.rows[self.token]
        if row.terminal is not None:
            return row.terminal
        reservation = row.reservation
        return WorkerStarted(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            backend_token=reservation.backend_token,
            launch_request_digest=reservation.launch_request_digest,
            provider_generation=reservation.provider_generation,
        )

    def wait(self, timeout: float | None = None) -> WorkerTerminalProof:
        self.supervisor.log.append(f"process.wait:{self.token}")
        row = self.supervisor.rows[self.token]
        if row.terminal is None:
            raise TimeoutError
        return row.terminal

    def stop(
        self,
        *,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> WorkerTerminalProof:
        return self.supervisor._stop(
            self.token, grace_period=grace_period, timeout=timeout
        )


class _FakeSupervisor:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.rows: dict[str, Any] = {}
        self.seals: dict[str, str] = {}
        self.realization_claims: dict[str, int] = {}
        self._realization_condition = threading.Condition()
        self.reserve_requests: list[ProcessLaunchRequest] = []
        self.private_environment_reprs: list[str] = []
        self.physical_starts = 0
        self.physical_start_tokens: list[str] = []
        self.last_started_token: str | None = None
        self.fail_stop_response_once = False
        self.fail_retire_response_once = False
        self.reconcile_calls: list[str] = []
        self.endpoints: dict[str, Any] = {}

    def reserve(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        request: ProcessLaunchRequest,
        realization_claim: Any = None,
    ) -> ProcessLaunchReservation:
        self.reserve_requests.append(request)
        sealed_binding = self.seals.get(launch_token)
        if sealed_binding is not None:
            raise RealmConflict("launch coordinate is terminally sealed")
        existing = self.rows.get(launch_token)
        if existing is not None:
            reservation = existing.reservation
            if (
                reservation.binding_id != binding_id
                or reservation.evidence_fingerprint != evidence_fingerprint
                or reservation.launch_request_digest != request.digest
                or (not existing.retired and existing.request != request)
            ):
                raise RealmConflict("changed launch request")
            return reservation
        backend = request_digest({"launch_token": launch_token})
        reservation = ProcessLaunchReservation(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            backend_token=backend,
            launch_request_digest=request.digest,
            provider_generation=1,
        )
        self.rows[launch_token] = SimpleNamespace(
            binding_id=binding_id,
            reservation=reservation,
            request=request,
            state="reserved",
            terminal=None,
            retired=False,
        )
        self.log.append(f"supervisor.reserve:{launch_token}")
        return reservation

    def lookup_reservation(self, **kwargs: Any) -> ProcessLaunchReservation:
        try:
            row = self.rows[kwargs["launch_token"]]
        except KeyError as error:
            raise RealmNotFound("absent") from error
        reservation = row.reservation
        if (
            reservation.binding_id != kwargs["binding_id"]
            or reservation.evidence_fingerprint
            != kwargs["evidence_fingerprint"]
            or reservation.launch_request_digest
            != kwargs["launch_request_digest"]
        ):
            raise RealmConflict("exact launch coordinates differ")
        return reservation

    def claim_launch_realization(
        self, *, launch_token: str, binding_id: str, timeout: float | None
    ) -> Any:
        with self._realization_condition:
            if launch_token in self.seals:
                raise RealmConflict("launch coordinate is unavailable")
            row = self.rows.get(launch_token)
            if row is not None and row.binding_id != binding_id:
                raise RealmConflict("launch coordinate differs")
            self.realization_claims[launch_token] = (
                self.realization_claims.get(launch_token, 0) + 1
            )
        return SimpleNamespace(
            launch_token=launch_token,
            binding_id=binding_id,
            released=False,
        )

    def release_launch_realization(self, claim: Any) -> None:
        with self._realization_condition:
            if claim.released:
                return
            count = self.realization_claims[claim.launch_token]
            if count == 1:
                self.realization_claims.pop(claim.launch_token)
            else:
                self.realization_claims[claim.launch_token] = count - 1
            claim.released = True
            self._realization_condition.notify_all()

    def reservation_state(self, reservation: ProcessLaunchReservation) -> str:
        row = self.rows[reservation.launch_token]
        return "terminal" if row.terminal is not None else row.state

    def record_unix_socket_endpoint(self, **kwargs: Any) -> Any:
        token = kwargs["launch_token"]
        row = self._required_row(token)
        reservation = row.reservation
        if (
            reservation.binding_id != kwargs["binding_id"]
            or reservation.evidence_fingerprint
            != kwargs["evidence_fingerprint"]
            or reservation.launch_request_digest
            != kwargs["launch_request_digest"]
        ):
            raise RealmConflict("exact launch coordinates differ")
        existing = self.endpoints.get(token)
        recorded = SimpleNamespace(
            endpoint_name=kwargs["endpoint_name"],
            path=Path(kwargs["path"]),
            device_id=kwargs["device_id"],
            inode=kwargs["inode"],
            state="recorded",
        )
        if existing is None:
            self.endpoints[token] = recorded
            existing = recorded
        elif (
            existing.endpoint_name != recorded.endpoint_name
            or existing.path != recorded.path
            or existing.device_id != recorded.device_id
            or existing.inode != recorded.inode
        ):
            raise RealmConflict("changed endpoint registration")
        return SimpleNamespace(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
            endpoint_name=existing.endpoint_name,
            state=existing.state,
        )

    def start_reserved(
        self,
        reservation: ProcessLaunchReservation,
        *,
        private_environment: ProcessLaunchPrivateEnvironment | None = None,
    ) -> _FakeProcess:
        row = self.rows[reservation.launch_token]
        if row.state == "reserved" and row.request.private_env_names:
            if (
                private_environment is None
                or private_environment.names != row.request.private_env_names
                or private_environment.binding_revision
                != row.request.private_env_binding_revision
            ):
                raise RealmConflict("private environment binding differs")
            self.private_environment_reprs.append(repr(private_environment))
        elif private_environment is not None:
            raise RealmConflict("private environment is unexpected")
        if row.state == "reserved" and row.terminal is None:
            row.state = "start_requested"
            self.physical_starts += 1
            self.physical_start_tokens.append(reservation.launch_token)
            self.last_started_token = reservation.launch_token
            self.log.append(f"supervisor.start:{reservation.launch_token}")
        return _FakeProcess(self, reservation.launch_token)

    def lookup_terminal_proof(self, **kwargs: Any):
        row = self.rows[kwargs["launch_token"]]
        reservation = row.reservation
        if (
            reservation.binding_id != kwargs["binding_id"]
            or reservation.evidence_fingerprint != kwargs["evidence_fingerprint"]
            or reservation.launch_request_digest != kwargs["launch_request_digest"]
        ):
            raise RealmConflict("wrong proof coordinates")
        return row.terminal

    def validate_terminal_proof(self, proof: WorkerTerminalProof):
        if self.rows[proof.launch_token].terminal != proof:
            raise RealmConflict("wrong proof")
        return proof

    def _proof(self, token: str, disposition: str) -> WorkerTerminalProof:
        reservation = self.rows[token].reservation
        return WorkerTerminalProof(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            backend_token=reservation.backend_token,
            launch_request_digest=reservation.launch_request_digest,
            disposition=disposition,
            provider_generation=reservation.provider_generation,
            terminal_at=2.0,
        )

    def mark_exited(self, token: str | None = None) -> WorkerTerminalProof:
        selected = token or self.last_started_token
        assert selected is not None
        row = self.rows[selected]
        if row.terminal is None:
            row.terminal = self._proof(selected, "exited")
        return row.terminal

    def _stop(
        self,
        launch_token: str,
        *,
        grace_period: float,
        timeout: float | None,
    ) -> WorkerTerminalProof:
        self.log.append(f"supervisor.stop:{launch_token}")
        row = self.rows[launch_token]
        if row.terminal is None:
            row.terminal = self._proof(launch_token, "killed")
        if self.fail_stop_response_once:
            self.fail_stop_response_once = False
            raise OSError("lost stop response")
        return row.terminal

    def retire_terminal(self, proof: WorkerTerminalProof) -> WorkerTerminalProof:
        self.validate_terminal_proof(proof)
        endpoint = self.endpoints.get(proof.launch_token)
        if endpoint is not None and endpoint.state != "reconciled":
            raise RealmConflict("endpoint cleanup remains pending")
        self.rows[proof.launch_token].retired = True
        self.log.append(f"supervisor.retire:{proof.launch_token}")
        if self.fail_retire_response_once:
            self.fail_retire_response_once = False
            raise OSError("lost retirement response")
        return proof

    def reconcile_terminal_launch(self, **kwargs: Any) -> Any:
        token = kwargs["launch_token"]
        self.reconcile_calls.append(token)
        row = self._required_row(token)
        reservation = row.reservation
        if (
            reservation.binding_id != kwargs["binding_id"]
            or reservation.evidence_fingerprint
            != kwargs["evidence_fingerprint"]
            or reservation.launch_request_digest
            != kwargs["launch_request_digest"]
        ):
            raise RealmConflict("exact launch coordinates differ")
        if row.retired:
            prior_state = "retired"
            proof = row.terminal
        elif row.terminal is not None:
            prior_state = "terminal"
            proof = row.terminal
        elif row.state == "reserved":
            prior_state = "reserved"
            proof = self.abandon_reserved(reservation)
        else:
            prior_state = "start_requested"
            try:
                proof = self._stop(
                    token,
                    grace_period=kwargs["grace_period"],
                    timeout=kwargs["timeout"],
                )
            except OSError:
                proof = row.terminal
                if proof is None:
                    raise
        assert proof is not None
        endpoint = self.endpoints.get(token)
        if endpoint is not None and endpoint.state != "reconciled":
            if os.path.lexists(endpoint.path):
                metadata = endpoint.path.lstat()
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (metadata.st_dev, metadata.st_ino)
                    != (endpoint.device_id, endpoint.inode)
                ):
                    raise RealmIntegrityError("recorded socket identity changed")
                endpoint.path.unlink()
            endpoint.state = "reconciled"
        try:
            self.retire_terminal(proof)
        except OSError:
            if not row.retired:
                raise
        return SimpleNamespace(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
            prior_state=prior_state,
            proof=proof,
            endpoints_reconciled=True,
            retired=True,
        )

    def seal_launch_if_absent(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> ProcessLaunchSealReceipt:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._realization_condition:
            while self.realization_claims.get(launch_token, 0):
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("realization claim remained active")
                self._realization_condition.wait(timeout=remaining)
            row = self.rows.get(launch_token)
            sealed_binding = self.seals.get(launch_token)
            if row is not None and sealed_binding is not None:
                raise RealmIntegrityError("row and seal coexist")
            if row is not None:
                if row.binding_id != binding_id:
                    raise RealmConflict("launch coordinate differs")
                prior_state = "existing"
            elif sealed_binding is not None:
                if sealed_binding != binding_id:
                    raise RealmConflict("launch coordinate differs")
                prior_state = "sealed"
            else:
                self.seals[launch_token] = binding_id
                self.log.append(f"supervisor.seal:{launch_token}")
                prior_state = "absent"
            return ProcessLaunchSealReceipt(
                launch_token=launch_token,
                binding_id=binding_id,
                prior_state=prior_state,
            )

    def abandon_reserved(
        self, reservation: ProcessLaunchReservation
    ) -> WorkerTerminalProof:
        row = self.rows[reservation.launch_token]
        if row.state != "reserved":
            raise RealmConflict("already started")
        row.terminal = self._proof(reservation.launch_token, "never_started")
        return row.terminal

    def retire_passive_orphan(
        self, *, launch_token: str, binding_id: str
    ) -> WorkerTerminalProof:
        self.log.append(f"supervisor.retire_orphan:{launch_token}")
        row = self.rows.get(launch_token)
        if row is None:
            raise RealmNotFound("absent")
        if row.binding_id != binding_id:
            raise RealmConflict("wrong binding")
        if row.state != "reserved":
            raise RealmConflict("already started")
        proof = self.abandon_reserved(row.reservation)
        return self.retire_terminal(proof)

    def _required_row(self, launch_token: str):
        try:
            return self.rows[launch_token]
        except KeyError as error:
            raise RealmNotFound("absent") from error


class _FakeRequestClient:
    def __init__(self, supervisor: _FakeSupervisor, log: list[str]) -> None:
        self.supervisor = supervisor
        self.log = log
        self.error_response = False
        self.transport_failure = False
        self.acknowledged_sequence = 0
        self.acknowledged_chain = INITIAL_BATCH_EXCHANGE_CHAIN
        self.pending_exchange: dict[str, Any] | None = None

    def __call__(
        self, _socket_path: Path, request: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        operation = request["op"]
        self.log.append(f"request:{operation}")
        if self.transport_failure:
            raise OSError("/private/provider/socket")
        if self.error_response:
            return {
                "error": {
                    "code": "method_failed",
                    "diagnostic_id": "0" * 32,
                    "message": "The retained method operation failed.",
                },
                "exchange_id": request["exchange_id"],
                "ok": False,
                "schema": BATCH_RESPONSE_SCHEMA,
            }
        if operation == "propose":
            result = {"candidates": [{"format": "parameters", "spec": {"x": 1}}]}
        elif operation == "observe":
            result = {"observation_count": len(request["payload"]["observations"])}
        elif operation == "ack":
            exchange = request["payload"]["exchange"]
            self.acknowledged_chain = retained_batch_exchange_chain_digest(
                self.acknowledged_chain,
                exchange_id=exchange["exchange_id"],
                exchange_sequence=exchange["exchange_sequence"],
                request_digest_value=exchange["request_digest"],
                response_digest=exchange["response_digest"],
            )
            self.acknowledged_sequence = exchange["exchange_sequence"]
            self.pending_exchange = None
            result = {
                "acknowledged_chain": self.acknowledged_chain,
                "acknowledged_exchange": exchange,
                "acknowledged_sequence": self.acknowledged_sequence,
            }
        elif operation == "status":
            result = {
                "acknowledged_chain": self.acknowledged_chain,
                "acknowledged_sequence": self.acknowledged_sequence,
                "pending_exchange": self.pending_exchange,
                "pending_response_bytes": 0,
            }
        elif operation == "shutdown":
            self.supervisor.mark_exited()
            result = {"shutdown": True}
        else:  # pragma: no cover - runtime validates public operations
            raise AssertionError(operation)
        response = {
            "exchange_id": request["exchange_id"],
            "ok": True,
            "result": result,
            "schema": BATCH_RESPONSE_SCHEMA,
        }
        if operation in {"propose", "observe"}:
            self.pending_exchange = {
                "exchange_id": request["exchange_id"],
                "exchange_sequence": request["exchange_sequence"],
                "request_digest": retained_batch_worker_request_digest(
                    operation, request["payload"]
                ),
                "response_digest": hashlib.sha256(
                    canonical_json_bytes(response)
                ).hexdigest(),
            }
        if operation == "status" and self.pending_exchange is not None:
            response["result"]["pending_exchange"] = self.pending_exchange
            response["result"]["pending_response_bytes"] = 1
        return response


@unittest.skipUnless(os.name == "posix", "retained batch runtime is POSIX-only")
class RetainedBatchRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket_temporary = tempfile.TemporaryDirectory(
            dir="/tmp", prefix="oprbw-test-"
        )
        self.addCleanup(self.socket_temporary.cleanup)
        self.definition = _definition()
        self.snapshot = _snapshot(self.definition)
        self.log: list[str] = []
        self.ledger = _FakeLedger(self.snapshot)
        self.projection_root = (
            self.root / "projection-provider" / "projection-shared" / "root"
        )
        self.projection_root.mkdir(parents=True)
        method_root = (
            self.projection_root
            / "scopes"
            / "study-package-source"
            / "methods"
        )
        method_root.mkdir(parents=True)
        (method_root / "random.yaml").write_text(
            "retained: true\n", encoding="utf-8"
        )
        (method_root / "method_impl.py").write_text(
            """
class Method:
    def __init__(self, definition, study_spec, rng): self.observed = 0
    def propose(self, n_candidates, study_state, evidence_view):
        return [{"format": "parameters", "spec": {"observed": self.observed}}]
    def observe(self, observations): self.observed += len(observations)
""",
            encoding="utf-8",
        )
        sys.modules.pop("method_impl", None)
        self.addCleanup(sys.modules.pop, "method_impl", None)
        self.control_root = self.root / "control"
        self.projections = _FakeProjectionService(
            self.ledger, self.projection_root, self.log
        )
        self.volumes = _FakeVolumeService(
            self.ledger, self.control_root, self.log
        )
        self.supervisor = _FakeSupervisor(self.log)
        self.client = _FakeRequestClient(self.supervisor, self.log)
        self.graph = SimpleNamespace(
            actor_principal_id="local-user:test",
            ledger=self.ledger,
            content_store=SimpleNamespace(store_id="test-store"),
            projection_service=self.projections,
            volume_service=self.volumes,
            process_supervisor=self.supervisor,
        )
        self.provider = RetainedBatchRuntimeProvider(
            self.graph,
            socket_parent=Path(self.socket_temporary.name),
            python_executable=Path(sys.executable),
            request_client=self.client,
            socket_probe=self._fake_socket_probe,
        )
        self.handles = []
        self.addCleanup(self._cleanup_handles)

    def _realize(self, snapshot: RunLedgerSnapshot | None = None):
        handle = self.provider.realize(snapshot or self.snapshot)
        self.handles.append(handle)
        return handle

    def _realize_with_client(self, client: Any):
        provider = RetainedBatchRuntimeProvider(
            self.graph,
            socket_parent=Path(self.socket_temporary.name),
            python_executable=Path(sys.executable),
            request_client=client,
            socket_probe=self._fake_socket_probe,
        )
        handle = provider.realize(self.snapshot)
        self.handles.append(handle)
        return handle

    @staticmethod
    def _fake_socket_probe(_path: Path) -> _SocketIdentity:
        # Most CI sandboxes deny AF_UNIX bind.  The provider's injectable
        # readiness seam still supplies an exact identity value while the
        # production probe performs the real post-connect lstat checks.
        return _SocketIdentity(0, 1)

    def _cleanup_handles(self) -> None:
        self.client.transport_failure = False
        self.client.error_response = False
        for handle in reversed(self.handles):
            try:
                handle.force_stop()
            except RetainedBatchRuntimeError:
                pass

    def test_method_context_is_a_no_copy_alias_in_the_existing_projection(self) -> None:
        definition = _context_definition()

        binding = _build_projection_binding(
            definition=definition,
            owner_id="run-owner",
            memberships=_memberships(definition),
            available_store_ids=("test-store",),
        )

        self.assertEqual(len(binding.spec.mappings), 1)
        self.assertEqual(
            binding.scope_roots["method-context"],
            "scopes/study-package-source/environments/context",
        )
        self.assertEqual(
            binding.scope_roots["study-package-source"],
            "scopes/study-package-source",
        )

    def test_exact_recovery_attaches_without_second_start_and_uses_short_socket(self) -> None:
        first = self._realize()
        first_request = self.supervisor.reserve_requests[-1]
        first_init = RetainedBatchWorkerInit.from_bytes(
            Path(first_request.argv[-1]).read_bytes()
        )
        Path(first_init.diagnostic_path).write_text(
            "grown diagnostics\n", encoding="utf-8"
        )

        second = self._realize()
        second_request = self.supervisor.reserve_requests[-1]

        self.assertEqual(first.attachment_kind, "started")
        self.assertEqual(second.attachment_kind, "attached")
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(self.supervisor.physical_starts, 1)
        self.assertEqual(first_request, second_request)
        self.assertEqual(first_request.argv[0], sys.executable)
        self.assertEqual(
            first_request.env["PYTHONHASHSEED"],
            str(int(self.definition.digest[:8], 16)),
        )
        self.assertNotIn("PATH", first_request.env)
        self.assertLessEqual(
            len(os.fsencode(first_init.socket_path)), MAX_UNIX_SOCKET_PATH_BYTES
        )
        namespace = Path(first_init.socket_path).parent
        metadata = namespace.lstat()
        self.assertTrue(stat.S_ISDIR(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
        self.assertEqual(
            Path(first_init.diagnostic_path).read_text(encoding="utf-8"),
            "grown diagnostics\n",
        )

    def test_launch_environment_reaches_only_the_method_worker_request(self) -> None:
        study = _study()
        study.method["runtime"]["envFromHost"] = [
            "OPENROUTER_API_KEY",
            "OPTPILOT_LLM_MODEL",
        ]
        definition = compile_retained_process_study(
            study,
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="retained-batch-method-environment",
        ).run_definition
        snapshot = _snapshot(definition)
        self.definition = definition
        self.snapshot = snapshot
        self.ledger.current = snapshot
        self.ledger.memberships = _memberships(definition)
        binding = MethodLaunchEnvironment.for_definition(
            definition,
            {
                "OPENROUTER_API_KEY": "private-api-key-value",
                "OPTPILOT_LLM_MODEL": "provider/model",
                "UNDECLARED_HOST_VALUE": "must-not-cross",
            },
            binding_revision="settings-revision-1",
        )
        provider = RetainedBatchRuntimeProvider(
            self.graph,
            method_environment=binding,
            socket_parent=Path(self.socket_temporary.name),
            python_executable=Path(sys.executable),
            request_client=self.client,
            socket_probe=self._fake_socket_probe,
        )

        handle = provider.realize(snapshot)
        self.handles.append(handle)
        request = self.supervisor.reserve_requests[-1]
        initialization = Path(request.argv[-1]).read_bytes()

        self.assertNotIn("OPENROUTER_API_KEY", request.env)
        self.assertNotIn("OPTPILOT_LLM_MODEL", request.env)
        self.assertNotIn("UNDECLARED_HOST_VALUE", request.env)
        self.assertEqual(
            request.private_env_names,
            ("OPENROUTER_API_KEY", "OPTPILOT_LLM_MODEL"),
        )
        self.assertEqual(
            request.private_env_binding_revision, "settings-revision-1"
        )
        self.assertNotIn("private-api-key-value", repr(request))
        self.assertNotIn(
            b"private-api-key-value", request.canonical_bytes
        )
        self.assertEqual(len(self.supervisor.private_environment_reprs), 1)
        self.assertNotIn(
            "private-api-key-value",
            self.supervisor.private_environment_reprs[0],
        )
        self.assertFalse(binding.values_available)
        self.assertNotIn(b"private-api-key-value", initialization)
        self.assertNotIn(
            "private-api-key-value",
            json.dumps(definition.to_dict(), sort_keys=True),
        )

        recovering_provider = RetainedBatchRuntimeProvider(
            self.graph,
            method_environment=binding.descriptor,
            socket_parent=Path(self.socket_temporary.name),
            python_executable=Path(sys.executable),
            request_client=self.client,
            socket_probe=self._fake_socket_probe,
        )
        recovered = recovering_provider.realize(snapshot)
        self.handles.append(recovered)
        self.assertEqual(recovered.attachment_kind, "attached")
        self.assertEqual(self.supervisor.physical_starts, 1)

    def test_recovery_reobserves_claimed_namespaces_after_remount(self) -> None:
        first = self._realize()
        for record in self.volumes.records.values():
            record.wrapper_device_id += 1000
            record.wrapper_inode += 1000
            record.data_device_id += 1000
            record.data_inode += 1000
        for record in self.projections.records:
            record.wrapper_device_id += 1000
            record.wrapper_inode += 1000
            record.exposed_tree_device_id += 1000
            record.exposed_tree_inode += 1000

        recovered = self._realize()

        self.assertEqual(first.identity, recovered.identity)
        self.assertEqual(recovered.attachment_kind, "attached")
        self.assertEqual(self.supervisor.physical_starts, 1)

        terminal = _snapshot(
            self.definition,
            run_state="succeeded",
            current_revision=2,
        )
        self.ledger.current = terminal
        receipt = self.provider.reconcile_inactive(terminal)

        self.assertEqual(receipt.worker_disposition, "stopped")
        self.assertTrue(
            all(
                record.state is EphemeralVolumeState.CLEANED
                for record in self.volumes.records.values()
            )
        )
        self.assertTrue(
            all(
                record.state is ProjectionRealizationState.CLEANED
                for record in self.projections.records
            )
        )

    def test_generic_response_and_canonical_method_failure_are_distinct_from_transport(self) -> None:
        runtime = self._realize()
        response = runtime.request(
            "proposal-1",
            "propose",
            {"evidence": {}, "n_candidates": 1, "study_state": {}},
            exchange_sequence=1,
        )
        self.assertEqual(
            set(response.response), {"exchange_id", "ok", "result", "schema"}
        )
        self.assertEqual(
            response.response_digest,
            hashlib.sha256(canonical_json_bytes(response.to_dict())).hexdigest(),
        )
        with self.assertRaises(TypeError):
            response.response["result"]["candidates"][0]["spec"]["x"] = 2
        detached = response.to_dict()
        detached["result"]["candidates"][0]["spec"]["x"] = 3
        self.assertEqual(
            response.response["result"]["candidates"][0]["spec"]["x"], 1
        )

        self.client.error_response = True
        with self.assertRaises(RetainedBatchMethodError) as captured:
            runtime.request(
                "proposal-2",
                "propose",
                {"evidence": {}, "n_candidates": 1, "study_state": {}},
                exchange_sequence=1,
            )
        error = captured.exception
        expected_response = {
            "error": {
                "code": "method_failed",
                "diagnostic_id": "0" * 32,
                "message": "The retained method operation failed.",
            },
            "exchange_id": "proposal-2",
            "ok": False,
            "schema": BATCH_RESPONSE_SCHEMA,
        }
        self.assertEqual(error.code, "method_failed")
        self.assertEqual(error.message, expected_response["error"]["message"])
        self.assertEqual(error.diagnostic_id, "0" * 32)
        self.assertEqual(
            error.response_digest,
            hashlib.sha256(canonical_json_bytes(expected_response)).hexdigest(),
        )

        self.client.error_response = False
        self.client.transport_failure = True
        with self.assertRaises(RetainedBatchRuntimeError) as transport:
            runtime.request(
                "proposal-3",
                "propose",
                {"evidence": {}, "n_candidates": 1, "study_state": {}},
                exchange_sequence=1,
            )
        self.assertEqual(transport.exception.code, "worker_unavailable")
        self.assertNotIn("private", str(transport.exception).lower())

    def test_typed_status_ack_and_retained_state_with_real_engine(self) -> None:
        engine = RetainedPythonBatchEngine(
            run_definition=self.definition,
            projection_root=self.projection_root,
            scope_roots={
                "study-package-source": "scopes/study-package-source"
            },
        )
        self.addCleanup(engine.close)
        runtime = self._realize_with_client(
            lambda _path, request, *, timeout: engine.handle(request)
        )

        initial = runtime.status("status-initial")
        self.assertEqual(initial.acknowledged_sequence, 0)
        self.assertEqual(
            initial.acknowledged_chain, INITIAL_BATCH_EXCHANGE_CHAIN
        )
        self.assertIsNone(initial.pending_exchange)
        self.assertEqual(initial.pending_response_bytes, 0)

        proposal = runtime.propose(
            "proposal-one",
            exchange_sequence=1,
            n_candidates=1,
            study_state={},
            evidence={},
        )
        self.assertEqual(proposal.candidates[0]["spec"]["observed"], 0)
        with self.assertRaises(TypeError):
            proposal.candidates[0]["spec"]["observed"] = 4
        detached = proposal.to_dict()
        detached["candidates"][0]["spec"]["observed"] = 5
        self.assertEqual(proposal.candidates[0]["spec"]["observed"], 0)
        proposal_coordinate = RetainedBatchExchangeCoordinate(
            proposal.exchange_id,
            proposal.exchange_sequence,
            proposal.request_digest,
            proposal.response_digest,
        )

        pending = runtime.status("status-pending-proposal")
        self.assertEqual(pending.pending_exchange, proposal_coordinate)
        self.assertGreater(pending.pending_response_bytes, 0)
        proposal_ack = runtime.ack(
            "ack-proposal-one",
            exchange=proposal_coordinate,
            previous_acknowledged_chain=INITIAL_BATCH_EXCHANGE_CHAIN,
        )
        self.assertEqual(proposal_ack.acknowledged_sequence, 1)
        after_proposal = runtime.status("status-after-proposal")
        self.assertEqual(after_proposal.acknowledged_sequence, 1)
        self.assertEqual(
            after_proposal.acknowledged_chain,
            proposal_ack.acknowledged_chain,
        )
        self.assertIsNone(after_proposal.pending_exchange)

        observation = runtime.observe(
            "observation-one",
            exchange_sequence=2,
            observations=[{"candidate_id": "candidate-one", "value": 1.0}],
        )
        observation_coordinate = RetainedBatchExchangeCoordinate(
            observation.exchange_id,
            observation.exchange_sequence,
            observation.request_digest,
            observation.response_digest,
        )
        observation_ack = runtime.ack(
            "ack-observation-one",
            exchange=observation_coordinate,
            previous_acknowledged_chain=proposal_ack.acknowledged_chain,
        )
        self.assertEqual(observation_ack.acknowledged_sequence, 2)

        next_proposal = runtime.propose(
            "proposal-two",
            exchange_sequence=3,
            n_candidates=1,
            study_state={},
            evidence={},
        )
        self.assertEqual(next_proposal.candidates[0]["spec"]["observed"], 1)

    def test_typed_protocol_errors_preserve_exact_request_and_response_digests(self) -> None:
        def malformed_client(
            _path: Path, request: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            if request["op"] == "propose":
                result = {
                    "candidates": [
                        {"format": "parameters", "spec": {"x": 1}},
                        {"format": "parameters", "spec": {"x": 2}},
                    ]
                }
            elif request["op"] == "observe":
                result = {"observation_count": 0}
            else:  # pragma: no cover - force_stop performs no protocol call
                result = {}
            return {
                "exchange_id": request["exchange_id"],
                "ok": True,
                "result": result,
                "schema": BATCH_RESPONSE_SCHEMA,
            }

        runtime = self._realize_with_client(malformed_client)
        proposal_payload = {
            "evidence": {},
            "n_candidates": 1,
            "study_state": {},
        }
        proposal_response = {
            "exchange_id": "proposal-overproduced",
            "ok": True,
            "result": {
                "candidates": [
                    {"format": "parameters", "spec": {"x": 1}},
                    {"format": "parameters", "spec": {"x": 2}},
                ]
            },
            "schema": BATCH_RESPONSE_SCHEMA,
        }
        with self.assertRaises(RetainedBatchProtocolError) as proposal_error:
            runtime.propose(
                "proposal-overproduced",
                exchange_sequence=1,
                n_candidates=1,
                study_state={},
                evidence={},
            )
        self.assertEqual(proposal_error.exception.exchange_sequence, 1)
        self.assertEqual(
            proposal_error.exception.request_digest,
            retained_batch_worker_request_digest("propose", proposal_payload),
        )
        self.assertEqual(
            proposal_error.exception.response_digest,
            hashlib.sha256(
                canonical_json_bytes(proposal_response)
            ).hexdigest(),
        )

        observation_payload = {"observations": [{"value": 1}]}
        observation_response = {
            "exchange_id": "observation-wrong-count",
            "ok": True,
            "result": {"observation_count": 0},
            "schema": BATCH_RESPONSE_SCHEMA,
        }
        with self.assertRaises(RetainedBatchProtocolError) as observation_error:
            runtime.observe(
                "observation-wrong-count",
                exchange_sequence=2,
                observations=observation_payload["observations"],
            )
        self.assertEqual(observation_error.exception.exchange_sequence, 2)
        self.assertEqual(
            observation_error.exception.request_digest,
            retained_batch_worker_request_digest(
                "observe", observation_payload
            ),
        )
        self.assertEqual(
            observation_error.exception.response_digest,
            hashlib.sha256(
                canonical_json_bytes(observation_response)
            ).hexdigest(),
        )

    def test_typed_durable_item_bounds_reject_before_worker_call(self) -> None:
        runtime = self._realize()
        request_count = len(
            [value for value in self.log if value.startswith("request:")]
        )
        with self.assertRaises(ValueError):
            runtime.propose(
                "proposal-too-wide",
                exchange_sequence=1,
                n_candidates=MAX_BATCH_EXCHANGE_ITEMS + 1,
                study_state={},
                evidence={},
            )
        with self.assertRaises(ValueError):
            runtime.observe(
                "observation-empty",
                exchange_sequence=1,
                observations=[],
            )
        with self.assertRaises(ValueError):
            runtime.observe(
                "observation-too-wide",
                exchange_sequence=1,
                observations=[{}] * (MAX_BATCH_EXCHANGE_ITEMS + 1),
            )
        self.assertEqual(
            len([value for value in self.log if value.startswith("request:")]),
            request_count,
        )

    def test_runtime_accepts_exact_durable_response_cap_and_rejects_one_byte_more(self) -> None:
        def capped_client(
            _path: Path, request: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            response = {
                "exchange_id": request["exchange_id"],
                "ok": True,
                "result": {"padding": ""},
                "schema": BATCH_RESPONSE_SCHEMA,
            }
            target_size = MAX_BATCH_DURABLE_RESPONSE_BYTES + int(
                request["exchange_id"] == "response-over-cap"
            )
            response["result"]["padding"] = "x" * (
                target_size - len(canonical_json_bytes(response))
            )
            self.assertEqual(len(canonical_json_bytes(response)), target_size)
            return response

        runtime = self._realize_with_client(capped_client)
        accepted = runtime.request("response-at-cap", "status", {})
        self.assertEqual(
            len(canonical_json_bytes(accepted.to_dict())),
            MAX_BATCH_DURABLE_RESPONSE_BYTES,
        )
        with self.assertRaises(RetainedBatchProtocolError) as rejected:
            runtime.request("response-over-cap", "status", {})
        self.assertEqual(rejected.exception.code, "worker_protocol_error")
        self.assertEqual(rejected.exception.exchange_id, "response-over-cap")
        self.assertIsNone(rejected.exception.request_digest)

    def test_blocked_request_does_not_starve_heartbeat_or_force_stop(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_client(
            path: Path, request: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            if request["op"] == "propose":
                entered.set()
                if not release.wait(5):  # pragma: no cover - avoids hung failure
                    raise TimeoutError
            return self.client(path, request, timeout=timeout)

        runtime = self._realize_with_client(blocking_client)
        request_errors: list[BaseException] = []
        heartbeat_results: list[Any] = []
        heartbeat_errors: list[BaseException] = []
        stop_errors: list[BaseException] = []

        def request_target() -> None:
            try:
                runtime.request(
                    "blocked-proposal",
                    "propose",
                    {"evidence": {}, "n_candidates": 1, "study_state": {}},
                    exchange_sequence=1,
                )
            except BaseException as error:
                request_errors.append(error)

        def heartbeat_target() -> None:
            try:
                heartbeat_results.append(
                    runtime.heartbeat(
                        operation_id="while-request-blocked", ttl_seconds=30
                    )
                )
            except BaseException as error:
                heartbeat_errors.append(error)

        def stop_target() -> None:
            try:
                runtime.force_stop(timeout=1)
            except BaseException as error:
                stop_errors.append(error)

        request_thread = threading.Thread(target=request_target, daemon=True)
        request_thread.start()
        self.assertTrue(entered.wait(1))
        heartbeat_thread = threading.Thread(target=heartbeat_target, daemon=True)
        heartbeat_thread.start()
        heartbeat_thread.join(1)
        heartbeat_finished = not heartbeat_thread.is_alive()
        stop_thread = threading.Thread(target=stop_target, daemon=True)
        stop_thread.start()
        stop_thread.join(1)
        stop_finished = not stop_thread.is_alive()

        release.set()
        request_thread.join(2)
        heartbeat_thread.join(2)
        stop_thread.join(2)

        self.assertTrue(heartbeat_finished)
        self.assertTrue(stop_finished)
        self.assertEqual(len(heartbeat_results), 1)
        self.assertEqual(heartbeat_errors, [])
        self.assertEqual(stop_errors, [])
        self.assertEqual(len(request_errors), 1)
        self.assertIsInstance(request_errors[0], RetainedBatchRuntimeError)
        self.assertEqual(request_errors[0].code, "worker_terminal")
        self.assertTrue(runtime.closed)

    def test_heartbeat_is_projection_then_volume_and_hides_pathful_failure(self) -> None:
        runtime = self._realize()
        heartbeat = runtime.heartbeat(operation_id="round-1", ttl_seconds=30)
        self.assertTrue(heartbeat.projection_lease_id.startswith("projection-"))
        self.assertTrue(heartbeat.volume_lease_id.startswith("ephemeral-volume-"))
        self.assertEqual(
            [item for item in self.log if ".heartbeat:" in item],
            [
                "projection.heartbeat:round-1/projection",
                "volume.heartbeat:round-1/volume",
            ],
        )

        self.volumes.handles[-1].fail_heartbeat = True
        with self.assertRaises(RetainedBatchRuntimeError) as captured:
            runtime.heartbeat(operation_id="round-2", ttl_seconds=30)
        self.assertEqual(captured.exception.code, "heartbeat_failed")
        self.assertNotIn("/private", str(captured.exception))
        self.assertEqual(
            [item for item in self.log if "round-2" in item],
            [
                "projection.heartbeat:round-2/projection",
                "volume.heartbeat:round-2/volume",
            ],
        )

    def test_orderly_shutdown_retires_before_volume_then_projection_cleanup(self) -> None:
        runtime = self._realize()
        self.log.clear()

        runtime.shutdown()

        request_index = self.log.index("request:shutdown")
        wait_index = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("process.wait:")
        )
        retire_index = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("supervisor.retire:")
        )
        volume_index = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("volume.close:")
        )
        projection_index = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("projection.close:")
        )
        self.assertLess(request_index, wait_index)
        self.assertLess(wait_index, retire_index)
        self.assertLess(retire_index, volume_index)
        self.assertLess(volume_index, projection_index)
        self.assertTrue(runtime.closed)

    def test_cleanup_refuses_replaced_socket_inode_but_releases_managed_resources(self) -> None:
        runtime = self._realize()
        runtime._socket_path.touch(mode=0o600)
        os.chmod(runtime._socket_path, 0o600)
        replacement = runtime._socket_path.lstat()
        self.log.clear()
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                runtime.force_stop()

            self.assertEqual(captured.exception.code, "cleanup_failed")
            after = runtime._socket_path.lstat()
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (replacement.st_dev, replacement.st_ino),
            )
            self.assertTrue(
                any(value.startswith("volume.close:") for value in self.log)
            )
            self.assertTrue(
                any(value.startswith("projection.close:") for value in self.log)
            )
        finally:
            if os.path.lexists(runtime._socket_path):
                runtime._socket_path.unlink()
        self.assertFalse(runtime.closed)

    def test_new_generation_stops_and_retires_immediately_prior_worker_first(self) -> None:
        prior_runtime = self._realize()
        prior = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=1,
            run_definition_digest=self.definition.digest,
        )

        generation_two = _snapshot(self.definition, generation=2)
        self.ledger.current = generation_two
        self.log.clear()
        runtime = self._realize(generation_two)

        stop_index = self.log.index(f"supervisor.stop:{prior.launch_token}")
        retire_index = self.log.index(f"supervisor.retire:{prior.launch_token}")
        current_start_index = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("supervisor.start:")
            and not value.endswith(prior.launch_token)
        )
        self.assertLess(stop_index, retire_index)
        self.assertLess(retire_index, current_start_index)
        self.assertEqual(runtime.identity.controller_generation, 2)
        self.assertFalse(prior_runtime.closed)

    def test_terminal_cleanup_waits_for_crashed_pre_reservation_realizer(self) -> None:
        stale_snapshot = self.snapshot
        stale = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=1,
            run_definition_digest=self.definition.digest,
        )
        terminal_snapshot = _snapshot(
            self.definition,
            run_state="succeeded",
            current_revision=2,
        )
        reserve_entered = threading.Event()
        crash_stale = threading.Event()
        terminal_completed = threading.Event()
        original_reserve = self.supervisor.reserve
        stale_volume_operation = (
            f"retained-batch-runtime/{stale.coordinate}/control-volume"
        )

        class SimulatedRealizerCrash(BaseException):
            pass

        def reserve_then_crash(**kwargs: Any) -> ProcessLaunchReservation:
            if kwargs["launch_token"] == stale.launch_token:
                reserve_entered.set()
                if not crash_stale.wait(timeout=5.0):
                    raise TimeoutError("stale reserve crash barrier timed out")
                raise SimulatedRealizerCrash
            return original_reserve(**kwargs)

        self.supervisor.reserve = reserve_then_crash
        stale_outcome: dict[str, Any] = {}
        terminal_outcome: dict[str, Any] = {}

        def realize_stale() -> None:
            try:
                stale_outcome["runtime"] = self.provider.realize(stale_snapshot)
            except BaseException as error:
                stale_outcome["error"] = error

        thread = threading.Thread(target=realize_stale, daemon=True)
        thread.start()
        self.assertTrue(reserve_entered.wait(timeout=5.0))
        self.assertNotIn(stale.launch_token, self.supervisor.rows)
        self.assertEqual(len(self.projections.records), 1)
        _key, stale_volume_id, _usage_lease_id = _volume_operation_identity(
            stale_volume_operation
        )
        self.assertIs(
            self.volumes.records[stale_volume_id].state,
            EphemeralVolumeState.ACTIVE,
        )

        def reconcile_terminal() -> None:
            try:
                terminal_outcome["receipt"] = self.provider.reconcile_inactive(
                    terminal_snapshot
                )
            except BaseException as error:
                terminal_outcome["error"] = error
            finally:
                terminal_completed.set()

        try:
            self.ledger.current = terminal_snapshot
            terminal_thread = threading.Thread(
                target=reconcile_terminal, daemon=True
            )
            terminal_thread.start()
            self.assertFalse(terminal_completed.wait(timeout=0.05))
        finally:
            crash_stale.set()
            thread.join(timeout=5.0)
            terminal_thread.join(timeout=5.0)
            self.supervisor.reserve = original_reserve

        self.assertFalse(thread.is_alive())
        self.assertFalse(terminal_thread.is_alive())
        error = stale_outcome.get("error")
        self.assertIsInstance(error, SimulatedRealizerCrash)
        self.assertNotIn("runtime", stale_outcome)
        self.assertNotIn("error", terminal_outcome)
        retirement = terminal_outcome["receipt"]
        self.assertEqual(retirement.worker_disposition, "absent")
        self.assertEqual(
            self.supervisor.seals[stale.launch_token], stale.binding_id
        )
        self.assertNotIn(stale.launch_token, self.supervisor.physical_start_tokens)
        self.assertEqual(self.supervisor.physical_starts, 0)
        self.assertNotIn(stale.launch_token, self.supervisor.rows)
        self.assertIs(
            self.projections.records[0].state,
            ProjectionRealizationState.CLEANED,
        )
        self.assertIs(
            self.volumes.records[stale_volume_id].state,
            EphemeralVolumeState.CLEANED,
        )

    def test_negative_seal_replays_cleaned_file_candidate_resources(self) -> None:
        self.definition = _file_definition()
        self.snapshot = _snapshot(self.definition)
        self.ledger.current = self.snapshot
        self.ledger.memberships = _memberships(self.definition)
        self.ledger.leases = {
            self.snapshot.controller_lease.lease_id: self.snapshot.controller_lease
        }
        prior = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=1,
            run_definition_digest=self.definition.digest,
        )
        original_reserve = self.supervisor.reserve

        def fail_before_reservation(**_kwargs: Any) -> ProcessLaunchReservation:
            raise OSError("simulated pre-reservation crash")

        self.supervisor.reserve = fail_before_reservation
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self.provider.realize(self.snapshot)
            self.assertEqual(captured.exception.code, "worker_start_failed")
        finally:
            self.supervisor.reserve = original_reserve

        self.assertNotIn(prior.launch_token, self.supervisor.rows)
        self.assertEqual(len(self.volumes.records), 2)
        for record in self.volumes.records.values():
            self.assertIs(record.state, EphemeralVolumeState.CLEANED)
            shutil.rmtree(self.control_root / record.relative_name)

        generation_two = _snapshot(self.definition, generation=2)
        self.ledger.current = generation_two
        runtime = self._realize(generation_two)

        self.assertEqual(runtime.identity.controller_generation, 2)
        self.assertEqual(self.supervisor.seals[prior.launch_token], prior.binding_id)
        self.assertTrue(
            all(
                record.state is ProjectionRealizationState.CLEANED
                for record in self.projections.records[:-1]
            )
        )

    def test_prior_generation_wrong_volume_parent_fails_before_cleanup_effects(self) -> None:
        self._realize()
        prior = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=1,
            run_definition_digest=self.definition.digest,
        )
        operation = (
            f"retained-batch-runtime/{prior.coordinate}/control-volume"
        )
        _key, volume_id, _usage_lease_id = _volume_operation_identity(operation)
        volume = self.volumes.records[volume_id]
        original_parent = volume.parent_lease_id
        volume.parent_lease_id = "unrelated-controller-lease"
        generation_two = _snapshot(self.definition, generation=2)
        self.ledger.current = generation_two
        self.log.clear()
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self._realize(generation_two)
            self.assertEqual(captured.exception.code, "prior_generation_cleanup_failed")
            self.assertFalse(
                any(
                    item.startswith(("supervisor.stop:", "supervisor.retire:"))
                    for item in self.log
                )
            )
            self.assertFalse(
                any(
                    item.startswith(("volume.reconcile:", "projection.reconcile:"))
                    for item in self.log
                )
            )
            self.assertIs(volume.state, EphemeralVolumeState.ACTIVE)
            self.assertIs(
                self.projections.records[0].state,
                ProjectionRealizationState.READY,
            )
        finally:
            volume.parent_lease_id = original_parent

    def test_repeated_cleanup_diagnostic_is_rate_limited(self) -> None:
        captured = io.StringIO()
        with redirect_stderr(captured):
            for _index in range(3):
                _report_cleanup_diagnostic(
                    run_id="run-diagnostic-rate-limit",
                    generation=7,
                    phase="preflight",
                    error=RealmIntegrityError("same durable failure"),
                )

        lines = captured.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("run-diagnostic-rate-limit generation 7", lines[0])
        self.assertIn("same durable failure", lines[0])

    def test_prior_generation_wrong_projection_consumer_lease_fails_before_cleanup_effects(self) -> None:
        self._realize()
        realization = self.projections.records[0]
        consumer = self.projections.consumers[realization.realization_id]
        original = self.ledger.leases[consumer.lease_id]
        self.ledger.leases[consumer.lease_id] = replace(
            original, holder_id="unrelated-holder"
        )
        generation_two = _snapshot(self.definition, generation=2)
        self.ledger.current = generation_two
        self.log.clear()
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self._realize(generation_two)
            self.assertEqual(captured.exception.code, "prior_generation_cleanup_failed")
            self.assertFalse(
                any(
                    item.startswith(("supervisor.stop:", "supervisor.retire:"))
                    for item in self.log
                )
            )
            self.assertFalse(
                any(
                    item.startswith(("volume.reconcile:", "projection.reconcile:"))
                    for item in self.log
                )
            )
            self.assertIs(
                realization.state, ProjectionRealizationState.READY
            )
            self.assertTrue(
                all(
                    record.state is EphemeralVolumeState.ACTIVE
                    for record in self.volumes.records.values()
                )
            )
        finally:
            self.ledger.leases[consumer.lease_id] = original

    def test_generation_three_retires_live_generation_one_across_skipped_term(self) -> None:
        generation_one = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=1,
            run_definition_digest=self.definition.digest,
        )
        generation_two = _worker_coordinates(
            realm_id=self.ledger.realm_id,
            run_id="run-one",
            controller_generation=2,
            run_definition_digest=self.definition.digest,
        )
        self._realize()
        generation_two_snapshot = _snapshot(self.definition, generation=2)
        self.ledger.current = generation_two_snapshot
        original_reconcile = self.provider._reconcile_generation
        self.provider._reconcile_generation = lambda **_kwargs: "absent"
        try:
            self._realize(generation_two_snapshot)
        finally:
            self.provider._reconcile_generation = original_reconcile

        generation_three = _snapshot(self.definition, generation=3)
        self.ledger.current = generation_three
        self.log.clear()
        runtime = self._realize(generation_three)

        old_stop = self.log.index(
            f"supervisor.stop:{generation_one.launch_token}"
        )
        second_stop = self.log.index(
            f"supervisor.stop:{generation_two.launch_token}"
        )
        current_start = next(
            index
            for index, value in enumerate(self.log)
            if value.startswith("supervisor.start:")
            and not value.endswith(generation_one.launch_token)
        )
        self.assertLess(old_stop, second_stop)
        self.assertLess(second_stop, current_start)
        self.assertTrue(self.supervisor.rows[generation_one.launch_token].retired)
        self.assertTrue(self.supervisor.rows[generation_two.launch_token].retired)
        self.assertTrue(
            all(
                record.state is ProjectionRealizationState.CLEANED
                for record in self.projections.records
                if record.availability_resolution["realization_sharing"][
                    "operation_coordinate_digest"
                ]
                in {
                    request_digest(
                        {
                            "format": "optpilot.projection-private-operation-coordinate.v1",
                            "realm_id": self.ledger.realm_id,
                            "operation_id": (
                                "retained-batch-runtime/"
                                f"{coordinates.coordinate}/projection"
                            ),
                        }
                    )
                    for coordinates in (generation_one, generation_two)
                }
            )
        )
        for coordinates in (generation_one, generation_two):
            operation = (
                f"retained-batch-runtime/{coordinates.coordinate}/control-volume"
            )
            _key, volume_id, _lease_id = _volume_operation_identity(operation)
            self.assertIs(
                self.volumes.records[volume_id].state,
                EphemeralVolumeState.CLEANED,
            )
        self.assertEqual(runtime.identity.controller_generation, 3)

    def test_terminal_retirement_never_starts_and_replays_after_live_crash(self) -> None:
        runtime = self._realize()
        terminal = _snapshot(
            self.definition,
            run_state="succeeded",
            current_revision=2,
        )
        self.ledger.current = terminal
        starts = self.supervisor.physical_starts
        reserves = len(self.supervisor.reserve_requests)
        self.log.clear()

        receipt = self.provider.reconcile_inactive(terminal)

        self.assertEqual(receipt.run_id, "run-one")
        self.assertEqual(receipt.controller_generation, 1)
        self.assertEqual(receipt.run_definition_digest, self.definition.digest)
        self.assertEqual(receipt.worker_disposition, "stopped")
        self.assertTrue(receipt.resources_reconciled)
        self.assertEqual(self.supervisor.physical_starts, starts)
        self.assertEqual(len(self.supervisor.reserve_requests), reserves)
        self.assertNotIn("request:shutdown", self.log)
        self.assertTrue(
            self.supervisor.rows[runtime.identity.launch_token].retired
        )
        self.assertTrue(
            all(
                record.state is ProjectionRealizationState.CLEANED
                for record in self.projections.records
            )
        )
        self.assertTrue(
            all(
                record.state is EphemeralVolumeState.CLEANED
                for record in self.volumes.records.values()
            )
        )

        replay = self.provider.reconcile_inactive(terminal)
        self.assertEqual(replay.worker_disposition, "already_retired")
        self.assertTrue(replay.resources_reconciled)
        self.assertEqual(self.supervisor.physical_starts, starts)
        self.assertEqual(len(self.supervisor.reserve_requests), reserves)

    def test_terminal_retirement_of_unrealized_run_is_absent_and_nonspawning(self) -> None:
        terminal = _snapshot(
            self.definition,
            run_state="cancelled",
            current_revision=2,
        )
        self.ledger.current = terminal

        receipt = self.provider.reconcile_inactive(terminal)

        self.assertEqual(receipt.worker_disposition, "absent")
        self.assertTrue(receipt.resources_reconciled)
        self.assertEqual(self.supervisor.physical_starts, 0)
        self.assertEqual(self.supervisor.reserve_requests, [])
        self.assertEqual(self.supervisor.reconcile_calls, [])

    def test_hard_draining_run_reconciles_inactive_without_realizing_worker(self) -> None:
        draining = _snapshot(
            self.definition,
            current_revision=2,
            submission_state="draining",
            stop_code="user_cancelled",
        )
        self.ledger.current = draining

        receipt = self.provider.reconcile_inactive(draining)

        self.assertEqual(receipt.worker_disposition, "absent")
        self.assertTrue(receipt.resources_reconciled)
        self.assertEqual(self.supervisor.physical_starts, 0)
        self.assertEqual(self.supervisor.reserve_requests, [])

    def test_soft_fully_resolved_drain_is_method_inactive(self) -> None:
        preparations = (
            SimpleNamespace(exchange_id="proposal-1"),
            SimpleNamespace(exchange_id="observation-1"),
        )
        completions = (
            SimpleNamespace(
                exchange_id="proposal-1",
                kind="proposal",
                outcome="admitted",
                round_index=1,
            ),
            SimpleNamespace(
                exchange_id="observation-1",
                kind="observation",
                outcome="acknowledged",
                round_index=1,
            ),
        )
        draining = _snapshot(
            self.definition,
            current_revision=3,
            submission_state="draining",
            stop_code="max_trials",
            method_exchange_preparations=preparations,
            method_exchange_completions=completions,
        )
        self.ledger.current = draining

        receipt = self.provider.reconcile_inactive(draining)

        self.assertEqual(receipt.worker_disposition, "absent")
        self.assertTrue(receipt.resources_reconciled)
        self.assertEqual(self.supervisor.physical_starts, 0)

    def test_terminal_retirement_converges_after_lost_stop_and_retire_responses(self) -> None:
        runtime = self._realize()
        terminal = _snapshot(
            self.definition,
            run_state="failed",
            current_revision=2,
        )
        self.ledger.current = terminal
        self.supervisor.fail_stop_response_once = True
        self.supervisor.fail_retire_response_once = True

        receipt = self.provider.reconcile_inactive(terminal)

        self.assertEqual(receipt.worker_disposition, "stopped")
        row = self.supervisor.rows[runtime.identity.launch_token]
        self.assertIsNotNone(row.terminal)
        self.assertTrue(row.retired)
        self.assertTrue(receipt.resources_reconciled)

    def test_terminal_retirement_retries_partial_live_shutdown_cleanup(self) -> None:
        runtime = self._realize()
        self.volumes.fail_close_once = True

        with self.assertRaises(RetainedBatchRuntimeError) as failed_shutdown:
            runtime.shutdown()
        self.assertEqual(failed_shutdown.exception.code, "cleanup_failed")

        terminal = _snapshot(
            self.definition,
            run_state="succeeded",
            current_revision=2,
        )
        self.ledger.current = terminal
        receipt = self.provider.reconcile_inactive(terminal)
        self.assertEqual(receipt.worker_disposition, "already_retired")
        self.assertTrue(receipt.resources_reconciled)

    def test_terminal_retirement_fences_released_private_projection_consumer(self) -> None:
        runtime = self._realize()
        runtime.shutdown()
        record = self.projections.records[0]
        consumer = self.projections.consumers[record.realization_id]
        lease = self.ledger.leases[consumer.lease_id]
        self.ledger.leases[consumer.lease_id] = replace(
            lease, state=LeaseState.RELEASED
        )
        terminal = _snapshot(
            self.definition,
            run_state="succeeded",
            current_revision=2,
        )
        self.ledger.current = terminal
        self.log.clear()

        receipt = self.provider.reconcile_inactive(terminal)

        self.assertEqual(receipt.worker_disposition, "already_retired")
        self.assertTrue(receipt.resources_reconciled)
        self.assertIs(record.state, ProjectionRealizationState.CLEANED)
        self.assertIn(
            f"projection.retire-operation:{record.realization_id}", self.log
        )
        self.assertFalse(
            any(item.startswith("projection.reconcile:") for item in self.log)
        )

    def test_started_before_endpoint_registration_stops_but_unproven_socket_fails_closed(self) -> None:
        runtime = self._realize()
        self.supervisor.endpoints.pop(runtime.identity.launch_token)
        # Worker-writable control data is never deletion authority, even when
        # it forges the legacy evidence filename and plausible identity fields.
        forged_evidence = (
            self.volumes.handles[-1].path / "worker-socket-evidence.json"
        )
        forged_evidence.write_bytes(
            b'{"device_id":0,"inode":1,"launch_token":"forged"}'
        )
        os.chmod(forged_evidence, 0o600)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                listener.bind(str(runtime._socket_path))
            except PermissionError:
                # Some CI sandboxes deny AF_UNIX bind.  A foreign filesystem
                # object exercises the same recovery invariant: without durable
                # supervisor-owned inode identity, no present endpoint may be
                # unlinked.
                listener.close()
                listener = None
                runtime._socket_path.touch(mode=0o600)
            os.chmod(runtime._socket_path, 0o600)
            terminal = _snapshot(
                self.definition,
                run_state="failed",
                current_revision=2,
            )
            self.ledger.current = terminal

            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self.provider.reconcile_inactive(terminal)

            self.assertEqual(captured.exception.code, "cleanup_failed")
            row = self.supervisor.rows[runtime.identity.launch_token]
            self.assertTrue(row.retired)
            self.assertTrue(os.path.lexists(runtime._socket_path))
            self.assertTrue(
                any(
                    record.state is EphemeralVolumeState.ACTIVE
                    for record in self.volumes.records.values()
                )
            )
        finally:
            if listener is not None:
                listener.close()
            if os.path.lexists(runtime._socket_path):
                runtime._socket_path.unlink()

        retry = self.provider.reconcile_inactive(terminal)
        self.assertEqual(retry.worker_disposition, "already_retired")
        self.assertTrue(retry.resources_reconciled)

    def test_terminal_retirement_refuses_replaced_socket_inode(self) -> None:
        runtime = self._realize()
        recorded_endpoint = self.supervisor.endpoints[
            runtime.identity.launch_token
        ]
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                listener.bind(str(runtime._socket_path))
            except PermissionError:
                listener.close()
                listener = None
                runtime._socket_path.touch(mode=0o600)
            os.chmod(runtime._socket_path, 0o600)
            replacement = runtime._socket_path.lstat()
            self.assertNotEqual(
                (replacement.st_dev, replacement.st_ino),
                (
                    recorded_endpoint.device_id,
                    recorded_endpoint.inode,
                ),
            )
            terminal = _snapshot(
                self.definition,
                run_state="succeeded",
                current_revision=2,
            )
            self.ledger.current = terminal

            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self.provider.reconcile_inactive(terminal)

            self.assertEqual(captured.exception.code, "cleanup_failed")
            self.assertTrue(os.path.lexists(runtime._socket_path))
            after = runtime._socket_path.lstat()
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (replacement.st_dev, replacement.st_ino),
            )
        finally:
            if listener is not None:
                listener.close()
            if os.path.lexists(runtime._socket_path):
                runtime._socket_path.unlink()

        retry = self.provider.reconcile_inactive(terminal)
        self.assertEqual(retry.worker_disposition, "already_terminal")

    def test_terminal_retirement_rejects_unrelated_process_row_without_side_effect(self) -> None:
        runtime = self._realize()
        row = self.supervisor.rows[runtime.identity.launch_token]
        original = row.reservation
        row.reservation = replace(
            original,
            evidence_fingerprint=request_digest({"unrelated": True}),
        )
        terminal = _snapshot(
            self.definition,
            run_state="failed",
            current_revision=2,
        )
        self.ledger.current = terminal
        self.log.clear()
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self.provider.reconcile_inactive(terminal)
            self.assertEqual(captured.exception.code, "cleanup_failed")
            self.assertIsNone(row.terminal)
            self.assertFalse(row.retired)
            self.assertFalse(
                any(value.startswith("volume.reconcile:") for value in self.log)
            )
            self.assertFalse(
                any(value.startswith("projection.reconcile:") for value in self.log)
            )
        finally:
            row.reservation = original

    def test_terminal_retirement_rejects_unrelated_volume_authority(self) -> None:
        runtime = self._realize()
        record = next(iter(self.volumes.records.values()))
        original_owner = record.owner_id
        record.owner_id = "unrelated-owner"
        terminal = _snapshot(
            self.definition,
            run_state="failed",
            current_revision=2,
        )
        self.ledger.current = terminal
        try:
            with self.assertRaises(RetainedBatchRuntimeError) as captured:
                self.provider.reconcile_inactive(terminal)
            self.assertEqual(captured.exception.code, "cleanup_failed")
            row = self.supervisor.rows[runtime.identity.launch_token]
            self.assertIsNone(row.terminal)
            self.assertFalse(row.retired)
            self.assertIs(record.state, EphemeralVolumeState.ACTIVE)
        finally:
            record.owner_id = original_owner

    def test_terminal_retirement_rejects_changed_snapshot_before_cleanup(self) -> None:
        terminal = _snapshot(
            self.definition,
            run_state="cancelled",
            current_revision=2,
        )
        self.ledger.current = terminal
        stale = _snapshot(
            self.definition,
            run_state="cancelled",
            current_revision=3,
        )

        with self.assertRaises(RetainedBatchRuntimeError) as captured:
            self.provider.reconcile_inactive(stale)

        self.assertEqual(captured.exception.code, "snapshot_changed")
        self.assertEqual(self.supervisor.reconcile_calls, [])

    def test_same_generation_changed_definition_or_scope_fails_without_second_worker(self) -> None:
        first = self._realize()
        source = self.definition.method_revision.source_layers[0]
        changed_scope = "alternate-method-source"
        method = replace(
            self.definition.method_revision,
            authored_config=ScopePath(
                changed_scope,
                self.definition.method_revision.authored_config.relative_path,
            ),
            source_layers=(
                ScopeLayer(
                    changed_scope,
                    source.snapshot_ref,
                    source_subpath=source.source_subpath,
                    destination_subpath=source.destination_subpath,
                    precedence=source.precedence,
                ),
            ),
        )
        prepared = replace(
            self.definition.prepared_method_runtime,
            method_revision_digest=method.digest,
            runtime_settings={
                "import_roots": [
                    {
                        "path": self.definition.prepared_method_runtime.runtime_settings[
                            "import_roots"
                        ][0]["path"],
                        "scope": changed_scope,
                    }
                ],
                "schema": "optpilot.logical-python-process-runtime-settings.v1",
            },
            workdir=ScopePath(
                changed_scope,
                self.definition.prepared_method_runtime.workdir.relative_path,
            ),
        )
        changed_definition = replace(
            self.definition,
            method_revision=method,
            prepared_method_runtime=prepared,
        )
        changed = _snapshot(changed_definition)
        self.ledger.current = changed
        self.ledger.memberships = _memberships(changed_definition)

        with self.assertRaises(RetainedBatchRuntimeError) as captured:
            self.provider.realize(changed)
        self.assertEqual(captured.exception.code, "resource_realization_failed")
        self.assertEqual(self.supervisor.physical_starts, 1)
        self.assertFalse(first.closed)


if __name__ == "__main__":
    unittest.main()
