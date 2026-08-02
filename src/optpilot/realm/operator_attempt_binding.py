"""Provider-private resource binding for noncanonical Operator Job attempts.

Canonical trials and candidate Debug Runs compile the same portable runtime
specification.  This module reuses the canonical process binder's resource
session contract without manufacturing a run, logical trial, or canonical
attempt row.  Durable Operator Job records retain only path-free identities;
host paths remain inside :class:`LocalAttemptExecutionBinding`.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from ..attempts import EvaluationSpec
from ..runtime_binding import (
    CANDIDATE_PROJECTION_PARTITION,
    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
    ENVIRONMENT_PROJECTION_PARTITION,
    LayeredVolumeScopeSource,
    PortableAttemptRuntimeSpec,
)
from ._validation import lower_hex_digest, required_text, thaw_json
from .ephemeral_volume_records import EphemeralVolumeRecord, EphemeralVolumeState
from .ephemeral_volume_service import (
    RealmEphemeralVolumeService,
    _volume_operation_identity,
)
from .errors import RealmConflict, RealmError, RealmIntegrityError, RealmNotFound
from .leases import LeaseRecord
from .ledger import RealmLedger
from .layered_volume_realization import compile_local_layered_volume_plan
from .local_attempt_launcher import LocalAttemptExecutionBinding
from .local_process_supervisor import ProcessLaunchSealReceipt, WorkerTerminalProof
from .operator_job_records import OperatorJobRecord, OperatorJobState
from .owners import OwnerMembership, OwnerPermission
from .projection import ProjectionSpec
from .process_execution_binder import (
    ProcessExecutionResourceError,
    ProcessExecutionResourceFailure,
    RealizedProcessRuntimeResources,
    _InitializationLeasePulse,
    _layered_initialization_identity,
    _require_private_projection,
    heartbeat_process_runtime_resources,
    release_process_runtime_resources,
    resolve_process_runtime_scopes,
    validate_process_runtime_resources,
)
from .projection_records import ProjectionRealizationRecord, ProjectionRealizationState
from .projection_service import (
    RealmProjectionService,
    _require_private_operation_resolution,
)
from .refs import canonical_json_bytes, request_digest
from .run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
)
from .run_records import RUN_CANDIDATE_ROLE


OPERATOR_JOB_RESOURCE_TTL_SECONDS = 300.0
OPERATOR_ATTEMPT_CLEANUP_EVIDENCE_SCHEMA = (
    "optpilot.operator-attempt-cleanup-evidence.v1"
)

_PROJECTION_SOURCE_ROLES = frozenset(
    {
        RUN_ATTEMPT_INPUT_ROLE,
        RUN_CANDIDATE_ROLE,
        RUN_ENVIRONMENT_SOURCE_ROLE,
        RUN_PREPARED_RUNTIME_ROLE,
    }
)


def _resolve_exact_projection_store(
    *,
    spec: PortableAttemptRuntimeSpec,
    memberships: Sequence[OwnerMembership],
    available_store_ids: Iterable[str],
    require_available: bool,
) -> str:
    """Authenticate every projected source and select one complete placement.

    The portable runtime already fixes destination and layer semantics.  This
    provider boundary additionally proves that the derived Operator Job owner
    holds the exact semantic roles for those immutable roots.  Store mirrors
    are allowed, but one selected store must contain every required role/ref
    pair so a single composite projection cannot silently substitute bytes.
    """

    if not isinstance(spec, PortableAttemptRuntimeSpec):
        raise TypeError("spec must be a PortableAttemptRuntimeSpec.")
    normalized_memberships = tuple(memberships)
    if any(not isinstance(item, OwnerMembership) for item in normalized_memberships):
        raise TypeError("memberships must contain OwnerMembership values.")
    if not isinstance(require_available, bool):
        raise TypeError("require_available must be a boolean.")

    mappings = spec.projection_spec.mappings
    prepared_mapping = next(
        (
            item
            for item in mappings
            if item.destination == ENVIRONMENT_PREPARED_PYTHON_PARTITION
        ),
        None,
    )
    expected_destinations = (ENVIRONMENT_PROJECTION_PARTITION,)
    if prepared_mapping is not None:
        expected_destinations += (ENVIRONMENT_PREPARED_PYTHON_PARTITION,)
    if spec.file_materialization is not None:
        expected_destinations += (CANDIDATE_PROJECTION_PARTITION,)
    if (
        tuple(item.destination for item in mappings) != expected_destinations
        or any(
            item.source_subpath
            != (
                "site-packages"
                if item.destination == ENVIRONMENT_PREPARED_PYTHON_PARTITION
                else "."
            )
            for item in mappings
        )
    ):
        raise RealmIntegrityError(
            "Operator Job projection mappings differ from its runtime semantics."
        )

    environment_snapshot = mappings[0].snapshot_ref
    expected_role_refs = {
        (RUN_ENVIRONMENT_SOURCE_ROLE, environment_snapshot),
    }
    if prepared_mapping is not None:
        expected_role_refs.add(
            (RUN_PREPARED_RUNTIME_ROLE, prepared_mapping.snapshot_ref)
        )
    seed_snapshots = {
        layer.snapshot_ref
        for scope in spec.scopes
        if isinstance(scope.source, LayeredVolumeScopeSource)
        for layer in scope.source.lower_layers
        if layer.collision_policy == "identical"
    }
    if seed_snapshots:
        if seed_snapshots != {environment_snapshot}:
            raise RealmIntegrityError(
                "Operator Job retained input layers differ from its environment mapping."
            )
        expected_role_refs.add((RUN_ATTEMPT_INPUT_ROLE, environment_snapshot))
    if spec.file_materialization is not None:
        candidate_mapping = next(
            item
            for item in mappings
            if item.destination == CANDIDATE_PROJECTION_PARTITION
        )
        expected_role_refs.add((RUN_CANDIDATE_ROLE, candidate_mapping.snapshot_ref))

    actual_role_refs = {
        (item.role, item.content_ref)
        for item in normalized_memberships
        if item.role in _PROJECTION_SOURCE_ROLES
    }
    if actual_role_refs != expected_role_refs:
        raise RealmConflict(
            "Operator Job projection sources differ from its derived owner authority."
        )

    stores_by_role_ref = {
        role_ref: {
            item.store_id
            for item in normalized_memberships
            if (item.role, item.content_ref) == role_ref
        }
        for role_ref in expected_role_refs
    }
    common_store_ids: set[str] | None = None
    for store_ids in stores_by_role_ref.values():
        common_store_ids = (
            set(store_ids)
            if common_store_ids is None
            else common_store_ids.intersection(store_ids)
        )
    if not common_store_ids:
        raise RealmConflict(
            "Operator Job projection sources have no common authorized content store."
        )
    if require_available:
        common_store_ids.intersection_update(set(available_store_ids))
        if not common_store_ids:
            raise RealmConflict(
                "Operator Job projection sources have no common locally available content store."
            )
    return min(common_store_ids, key=lambda item: item.encode("utf-8"))


@dataclass(frozen=True)
class OperatorAttemptCleanupEvidence:
    """Path-free proof that every deterministic Debug resource was reconciled."""

    job_id: str
    binding_id: str
    launch_token: str
    operator_plan_digest: str
    portable_spec_digest: str
    terminal_authority_digest: str
    projection_cleanup_digest: str
    volume_cleanup_digests: tuple[tuple[str, str], ...]
    schema_version: str = OPERATOR_ATTEMPT_CLEANUP_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != OPERATOR_ATTEMPT_CLEANUP_EVIDENCE_SCHEMA:
            raise ValueError("operator attempt cleanup evidence schema is unsupported.")
        for name in ("job_id", "binding_id", "launch_token"):
            required_text(
                getattr(self, name), f"operator attempt cleanup {name}", max_bytes=512
            )
        for name in (
            "operator_plan_digest",
            "portable_spec_digest",
            "terminal_authority_digest",
            "projection_cleanup_digest",
        ):
            lower_hex_digest(
                getattr(self, name), f"operator attempt cleanup {name}"
            )
        normalized = tuple(self.volume_cleanup_digests)
        if normalized != tuple(sorted(normalized)):
            raise ValueError("operator attempt volume cleanup evidence must be sorted.")
        if len({name for name, _digest in normalized}) != len(normalized):
            raise ValueError("operator attempt volume cleanup names must be unique.")
        for logical_name, digest in normalized:
            required_text(
                logical_name,
                "operator attempt cleanup volume logical name",
                max_bytes=256,
            )
            lower_hex_digest(digest, "operator attempt cleanup volume digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "job_id": self.job_id,
            "launch_token": self.launch_token,
            "operator_plan_digest": self.operator_plan_digest,
            "portable_spec_digest": self.portable_spec_digest,
            "projection_cleanup_digest": self.projection_cleanup_digest,
            "schema_version": self.schema_version,
            "terminal_authority_digest": self.terminal_authority_digest,
            "volume_cleanup_digests": [
                {"digest": digest, "logical_name": logical_name}
                for logical_name, digest in self.volume_cleanup_digests
            ],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())


@dataclass(frozen=True)
class _AdmissionIdentity:
    lease_id: str
    holder_id: str
    fencing_token: int


class ManagedOperatorAttemptBinding:
    """One exact noncanonical attempt plus its attached provider resources."""

    def __init__(
        self,
        *,
        job_id: str,
        operator_plan_digest: str,
        projection_service: RealmProjectionService,
        local_binding: LocalAttemptExecutionBinding,
        resources: RealizedProcessRuntimeResources,
    ) -> None:
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(local_binding, LocalAttemptExecutionBinding):
            raise TypeError("local_binding must be a LocalAttemptExecutionBinding.")
        if not isinstance(resources, RealizedProcessRuntimeResources):
            raise TypeError("resources must be RealizedProcessRuntimeResources.")
        self._job_id = required_text(job_id, "managed Operator Job id")
        self._operator_plan_digest = lower_hex_digest(
            operator_plan_digest, "managed Operator Job plan digest"
        )
        self._projection_service = projection_service
        self._local_binding = local_binding
        self._resources = resources
        self._released = False
        self._lock = threading.RLock()

    @property
    def local_binding(self) -> LocalAttemptExecutionBinding:
        return self._local_binding

    @property
    def portable_spec(self) -> PortableAttemptRuntimeSpec:
        return self._local_binding.portable_spec

    @property
    def binding_id(self) -> str:
        return self._local_binding.binding_id

    @property
    def scope_paths(self) -> Mapping[str, Path]:
        """Return provider-private paths only to the local attempt launcher."""

        return self._local_binding.scope_paths

    @property
    def workdir(self) -> Path:
        return self._local_binding.workdir

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def validate(self) -> None:
        with self._lock:
            if self._released:
                raise RealmConflict("Operator Job attempt resources are released.")
            validate_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
            )

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        with self._lock:
            if self._released:
                raise RealmConflict("Operator Job attempt resources are released.")
            heartbeat_process_runtime_resources(
                resources=self._resources,
                projection_name=self.portable_spec.projection_name,
                operation_id=operation_id,
                ttl_seconds=ttl_seconds,
            )

    def release_after_execution_terminalized(
        self,
        authority: WorkerTerminalProof | ProcessLaunchSealReceipt,
    ) -> None:
        """Release after an exact launch coordinate is provably terminal.

        A worker proof covers a launch which reached the provider registry.  A
        negative seal covers the cancellation race where no launch existed and
        the provider has durably prohibited a later reservation.  A seal which
        reports ``existing`` is deliberately insufficient: that launch must be
        reconciled with its full startup coordinates first.
        """

        binding = self._local_binding
        if isinstance(authority, WorkerTerminalProof):
            if (
                authority.launch_token != binding.launch_token
                or authority.binding_id != binding.binding_id
                or authority.evidence_fingerprint != binding.evidence_fingerprint
            ):
                raise RealmConflict(
                    "worker terminal proof differs from the Operator Job binding."
                )
        elif isinstance(authority, ProcessLaunchSealReceipt):
            if (
                not authority.sealed
                or authority.launch_token != binding.launch_token
                or authority.binding_id != binding.binding_id
            ):
                raise RealmConflict(
                    "worker launch seal is not cleanup authority for the "
                    "Operator Job binding."
                )
        else:
            raise TypeError(
                "authority must be a WorkerTerminalProof or "
                "ProcessLaunchSealReceipt."
            )
        with self._lock:
            if self._released:
                return
            release_process_runtime_resources(
                projection_service=self._projection_service,
                resources=self._resources,
            )
            self._released = True

    def detach_after_terminal_cleanup(
        self, evidence: OperatorAttemptCleanupEvidence
    ) -> None:
        """Close process-local attachments after durable identity cleanup."""

        if not isinstance(evidence, OperatorAttemptCleanupEvidence):
            raise TypeError(
                "evidence must be an OperatorAttemptCleanupEvidence."
            )
        binding = self._local_binding
        if (
            evidence.job_id != self._job_id
            or evidence.binding_id != binding.binding_id
            or evidence.launch_token != binding.launch_token
            or evidence.operator_plan_digest != self._operator_plan_digest
            or evidence.portable_spec_digest != binding.portable_spec.digest
        ):
            raise RealmConflict(
                "Operator attempt cleanup evidence differs from its attachment."
            )
        with self._lock:
            if self._released:
                return
            failures: list[ProcessExecutionResourceFailure] = []
            for logical_name, volume in self._resources.volumes:
                try:
                    volume._detach_without_release()
                except BaseException as error:
                    failures.append(
                        ProcessExecutionResourceFailure(
                            "detach", "volume", logical_name, error
                        )
                    )
            try:
                self._resources.projection._detach_after_private_retirement()
            except BaseException as error:
                failures.append(
                    ProcessExecutionResourceFailure(
                        "detach",
                        "private-projection",
                        binding.portable_spec.projection_name,
                        error,
                    )
                )
            if failures:
                raise ProcessExecutionResourceError(
                    "Operator attempt attachment detach was incomplete", failures
                )
            self._released = True


class RealmOperatorAttemptBinder:
    """Realize one path-free Operator Job plan under fenced job authority."""

    def __init__(
        self,
        ledger: RealmLedger,
        projection_service: RealmProjectionService,
        volume_service: RealmEphemeralVolumeService,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(volume_service, RealmEphemeralVolumeService):
            raise TypeError("volume_service must be a RealmEphemeralVolumeService.")
        if projection_service.ledger is not ledger or volume_service.ledger is not ledger:
            raise ValueError("operator execution services must share one Realm ledger.")
        self._ledger = ledger
        self._projection_service = projection_service
        self._volume_service = volume_service

    def realize(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        attempt_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        ttl_seconds: float = OPERATOR_JOB_RESOURCE_TTL_SECONDS,
    ) -> ManagedOperatorAttemptBinding:
        """Recover deterministic resources or create only missing resources."""

        return self._bind(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            admission_lease=admission_lease,
            attempt_id=attempt_id,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            ttl_seconds=ttl_seconds,
            create_missing=True,
        )

    def recover_existing(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        attempt_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        ttl_seconds: float = OPERATOR_JOB_RESOURCE_TTL_SECONDS,
    ) -> ManagedOperatorAttemptBinding:
        """Reattach exact existing resources without creating missing ones."""

        return self._bind(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            admission_lease=admission_lease,
            attempt_id=attempt_id,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            ttl_seconds=ttl_seconds,
            create_missing=False,
        )

    def cleanup_after_terminal(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        attempt_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        authority: WorkerTerminalProof | ProcessLaunchSealReceipt,
        admission_lease: LeaseRecord | None = None,
        ttl_seconds: float = OPERATOR_JOB_RESOURCE_TTL_SECONDS,
    ) -> OperatorAttemptCleanupEvidence:
        """Reconcile every deterministic resource without requiring live leases.

        Provider terminalization must happen before this call.  A launched job
        supplies its exact worker proof; a cancellation that never committed a
        launch intent supplies either the provider's negative seal or the proof
        from retiring the passive reservation that won the race.  Projection
        and volume rows are then discovered by immutable operation identity and
        reconciled independently, so replay survives any partial cleanup.
        """

        actor_principal_id = required_text(
            actor_principal_id, "operator attempt cleanup actor principal id"
        )
        job_id = required_text(job_id, "operator attempt cleanup job id")
        owner_id = required_text(owner_id, "operator attempt cleanup owner id")
        operator_plan_digest = lower_hex_digest(
            operator_plan_digest, "operator attempt cleanup plan digest"
        )
        attempt_id = required_text(
            attempt_id, "operator attempt cleanup attempt id"
        )
        binding_id = required_text(
            binding_id, "operator attempt cleanup binding id"
        )
        launch_token = required_text(
            launch_token, "operator attempt cleanup launch token"
        )
        evidence_fingerprint = lower_hex_digest(
            evidence_fingerprint,
            "operator attempt cleanup evidence fingerprint",
        )
        if not isinstance(evaluation_spec, EvaluationSpec):
            raise TypeError("evaluation_spec must be an EvaluationSpec.")
        if not isinstance(portable_spec, PortableAttemptRuntimeSpec):
            raise TypeError("portable_spec must be a PortableAttemptRuntimeSpec.")
        if not isinstance(
            authority, (WorkerTerminalProof, ProcessLaunchSealReceipt)
        ):
            raise TypeError(
                "authority must be a WorkerTerminalProof or ProcessLaunchSealReceipt."
            )
        if admission_lease is not None and not isinstance(admission_lease, LeaseRecord):
            raise TypeError("admission_lease must be a LeaseRecord or None.")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("operator attempt cleanup ttl_seconds must be positive.")

        job = self._validate_job_runtime(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            attempt_id=attempt_id,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            require_terminal=True,
        )
        admission_identity = self._validate_cleanup_authority(
            job=job,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
            authority=authority,
            admission_lease=admission_lease,
        )
        store_id = self._resolve_projection_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            spec=portable_spec,
            require_available=False,
        )
        holder_id = _resource_holder_id(job_id=job_id, binding_id=binding_id)
        projection_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            resource_kind="projection",
            logical_name=portable_spec.projection_name,
        )
        projection_metadata = _projection_metadata(
            job_id=job_id,
            binding_id=binding_id,
            logical_name=portable_spec.projection_name,
        )

        failures: list[ProcessExecutionResourceFailure] = []
        volume_evidence: list[tuple[str, str]] = []
        for requirement in portable_spec.writable_volumes:
            operation_id = _resource_operation_id(
                job_id=job_id,
                binding_id=binding_id,
                resource_kind="volume",
                logical_name=requirement.name,
            )
            try:
                digest = self._cleanup_volume_operation(
                    actor_principal_id=actor_principal_id,
                    job=job,
                    admission_identity=admission_identity,
                    operation_id=operation_id,
                    holder_id=holder_id,
                    logical_name=requirement.name,
                    quota=requirement.quota,
                    quota_enforcement=requirement.quota_enforcement,
                    allow_absent=job.launch_intent is None,
                    ttl_seconds=float(ttl_seconds),
                )
            except BaseException as error:
                failures.append(
                    ProcessExecutionResourceFailure(
                        "cleanup", "volume", requirement.name, error
                    )
                )
            else:
                volume_evidence.append((requirement.name, digest))

        projection_digest: str | None = None
        try:
            projection_digest = self._cleanup_projection_operation(
                actor_principal_id=actor_principal_id,
                operation_id=projection_operation,
                owner_id=owner_id,
                store_id=store_id,
                spec=portable_spec.projection_spec,
                holder_id=holder_id,
                ttl_seconds=float(ttl_seconds),
                metadata=projection_metadata,
                allow_absent=job.launch_intent is None,
            )
        except BaseException as error:
            failures.append(
                ProcessExecutionResourceFailure(
                    "cleanup",
                    "private-projection",
                    portable_spec.projection_name,
                    error,
                )
            )
        if failures:
            raise ProcessExecutionResourceError(
                "Operator attempt resource cleanup was incomplete", failures
            )
        if projection_digest is None:  # pragma: no cover - assigned or failed
            raise RealmIntegrityError("Operator attempt projection cleanup lacks evidence.")
        return OperatorAttemptCleanupEvidence(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            portable_spec_digest=portable_spec.digest,
            terminal_authority_digest=_terminal_authority_digest(authority),
            projection_cleanup_digest=projection_digest,
            volume_cleanup_digests=tuple(sorted(volume_evidence)),
        )

    def _validate_job_runtime(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        attempt_id: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        require_terminal: bool,
    ) -> OperatorJobRecord:
        if (
            portable_spec.projection_spec.owner_id != owner_id
            or portable_spec.evaluation_spec_digest != evaluation_spec.digest
            or portable_spec.provider.kind != "process"
        ):
            raise RealmConflict(
                "Operator Job runtime differs from its owner or evaluation request."
            )
        owner = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if owner.owner_kind != "operator-job":
            raise RealmConflict("Operator Job resources require an operator-job owner.")
        job = self._ledger.read_operator_job(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
        )
        derivation = self._ledger.read_owner_derivation(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
        )
        if (
            job.owner_id != owner_id
            or job.plan_digest != operator_plan_digest
            or job.approval is None
            or job.state
            in {OperatorJobState.PLANNED, OperatorJobState.AWAITING_APPROVAL}
            or (require_terminal and not job.state.terminal)
            or job.plan.job_kind != "candidate-debug-run"
            or job.plan.target.kind != "candidate-evaluation"
            or job.plan.evidence_sink_id != attempt_id
            or job.plan.owner_derivation_manifest_digest != derivation.digest
            or job.plan.backend_kind != "local-process"
            or job.plan.runtime_fingerprint != portable_spec.digest
            or job.plan.projection_contract_digest
            != portable_spec.projection_spec.digest
        ):
            raise RealmConflict(
                "Operator Job plan differs from the requested process resources."
            )
        return job

    def _validate_cleanup_authority(
        self,
        *,
        job: OperatorJobRecord,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        authority: WorkerTerminalProof | ProcessLaunchSealReceipt,
        admission_lease: LeaseRecord | None,
    ) -> _AdmissionIdentity:
        intent = job.launch_intent
        outcome = job.outcome
        deterministic_identity = _deterministic_admission_identity(job.job_id)
        if outcome is None:
            raise RealmConflict("Operator attempt cleanup requires a terminal outcome.")
        if intent is not None:
            if not isinstance(authority, WorkerTerminalProof):
                raise RealmConflict(
                    "Launched Operator attempt cleanup requires worker terminal proof."
                )
            identity = deterministic_identity
            if (
                intent.binding_id != binding_id
                or intent.launch_token != launch_token
                or intent.provider_kind != "local-process"
                or intent.evidence_fingerprint != evidence_fingerprint
                or intent.admission_lease_id != identity.lease_id
                or intent.admission_holder_id != identity.holder_id
                or intent.admission_fencing_token != identity.fencing_token
                or authority.binding_id != binding_id
                or authority.launch_token != launch_token
                or authority.evidence_fingerprint != evidence_fingerprint
                or authority.launch_request_digest != intent.launch_request_digest
                or hashlib.sha256(authority.canonical_bytes).hexdigest()
                != outcome.outcome.terminal_proof_digest
            ):
                raise RealmConflict(
                    "Operator attempt cleanup proof differs from its terminal job."
                )
        else:
            identity = deterministic_identity
            if (
                job.state is not OperatorJobState.CANCELLED
                or outcome.outcome.status.value != "cancelled"
                or outcome.outcome.started
                or outcome.outcome.disposition.value != "never_started"
                or outcome.outcome.terminal_proof_digest is not None
            ):
                raise RealmConflict(
                    "Unlaunched Operator attempt cleanup requires exact cancellation."
                )
            if isinstance(authority, ProcessLaunchSealReceipt):
                if (
                    not authority.sealed
                    or authority.binding_id != binding_id
                    or authority.launch_token != launch_token
                ):
                    raise RealmConflict(
                        "Operator attempt launch seal differs from its cancellation."
                    )
            elif (
                authority.binding_id != binding_id
                or authority.launch_token != launch_token
                or authority.evidence_fingerprint != evidence_fingerprint
                or authority.disposition != "never_started"
            ):
                raise RealmConflict(
                    "Passive Operator attempt proof differs from its cancellation."
                )
        if admission_lease is not None:
            _validate_admission_lease(
                admission_lease,
                identity=identity,
                job_id=job.job_id,
                owner_id=job.owner_id,
                operator_plan_digest=job.plan_digest,
            )
        return identity

    def _cleanup_projection_operation(
        self,
        *,
        actor_principal_id: str,
        operation_id: str,
        owner_id: str,
        store_id: str,
        spec: ProjectionSpec,
        holder_id: str,
        ttl_seconds: float,
        metadata: Mapping[str, object],
        allow_absent: bool,
    ) -> str:
        records = self._ledger.list_projection_realizations(
            actor_principal_id=self._projection_service.maintenance_principal_id,
            projection_root_id=self._projection_service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        matches: list[ProjectionRealizationRecord] = []
        for item in records:
            try:
                _require_private_operation_resolution(
                    item,
                    realm_id=self._ledger.realm_id,
                    expected_operation_id=operation_id,
                )
            except RealmConflict:
                continue
            if (
                item.projection_root_id
                != self._projection_service.root_binding.projection_root_id
                or item.owner_id != owner_id
                or item.store_id != store_id
                or item.spec_digest != spec.digest
                or thaw_json(item.spec) != spec.to_dict()
                or item.state is ProjectionRealizationState.QUARANTINED
            ):
                raise RealmConflict(
                    "Operator attempt projection differs from its exact operation."
                )
            matches.append(item)

        if not matches and not allow_absent:
            raise RealmIntegrityError(
                "Launched Operator attempt is missing its durable projection."
            )

        cleaned: list[ProjectionRealizationRecord] = []
        operation_coordinate_digest = request_digest(
            {
                "format": "optpilot.projection-private-operation-coordinate.v1",
                "operation_id": operation_id,
                "realm_id": self._ledger.realm_id,
            }
        )
        for item in sorted(matches, key=lambda value: value.realization_id):
            if item.state is ProjectionRealizationState.CLEANED:
                cleaned.append(item)
                continue
            try:
                fresh = self._ledger.validate_private_projection_operation(
                    actor_principal_id=actor_principal_id,
                    projection_root_id=(
                        self._projection_service.root_binding.projection_root_id
                    ),
                    realization_id=item.realization_id,
                    expected_owner_id=owner_id,
                    expected_store_id=store_id,
                    expected_spec=spec.to_dict(),
                    expected_provider_kind=self._projection_service.provider_kind,
                    expected_operation_coordinate_digest=(
                        operation_coordinate_digest
                    ),
                    expected_consumer_holder_id=holder_id,
                    expected_consumer_kind="operator-job-attempt",
                    expected_consumer_metadata=metadata,
                )
            except RealmNotFound:
                current = self._projection_service._maintenance_realization(
                    item.realization_id
                )
                _validate_same_projection_record(item, current)
                _require_private_operation_resolution(
                    current,
                    realm_id=self._ledger.realm_id,
                    expected_operation_id=operation_id,
                )
                if current.state is ProjectionRealizationState.CLEANED:
                    cleaned.append(current)
                    continue
                if current.state not in {
                    ProjectionRealizationState.CLOSING,
                    ProjectionRealizationState.CLEANING,
                }:
                    raise
                # Another exact cleanup adopter can retire the private
                # operation between our snapshot and validation.  Once its
                # durable record is closing, the operation coordinate and
                # immutable realization identity remain sufficient authority
                # to converge through root maintenance.  Missing validation
                # while the realization is still live remains an error.
                fresh = current
            _validate_same_projection_record(item, fresh)
            if fresh.state in {
                ProjectionRealizationState.CREATING,
                ProjectionRealizationState.MATERIALIZING,
                ProjectionRealizationState.READY,
                ProjectionRealizationState.CLEANED,
            }:
                retire_operation = _cleanup_operation_id(
                    operation_id,
                    resource_kind="projection-retire",
                    logical_name=item.realization_id,
                )
                try:
                    fresh = (
                        self._projection_service.retire_private_projection_operation(
                            operation_id=retire_operation,
                            actor_principal_id=actor_principal_id,
                            realization_id=fresh.realization_id,
                            expected_owner_id=owner_id,
                            expected_store_id=store_id,
                            expected_spec=spec,
                            expected_operation_coordinate_digest=(
                                operation_coordinate_digest
                            ),
                            expected_consumer_holder_id=holder_id,
                            expected_consumer_kind="operator-job-attempt",
                            expected_consumer_metadata=metadata,
                            ttl_seconds=ttl_seconds,
                        )
                    )
                except RealmConflict:
                    current = self._projection_service._maintenance_realization(
                        item.realization_id
                    )
                    _validate_same_projection_record(item, current)
                    if current.state not in {
                        ProjectionRealizationState.CLOSING,
                        ProjectionRealizationState.CLEANING,
                        ProjectionRealizationState.CLEANED,
                    }:
                        raise
            if fresh.state is not ProjectionRealizationState.CLEANED:
                fresh = self._projection_service._maintenance_realization(
                    item.realization_id
                )
                _validate_same_projection_record(item, fresh)
                if fresh.state is not ProjectionRealizationState.CLEANED:
                    receipt = self._projection_service.reconcile_projection(
                        operation_id=_cleanup_operation_id(
                            operation_id,
                            resource_kind="projection",
                            logical_name=item.realization_id,
                        ),
                        realization_id=item.realization_id,
                        ttl_seconds=ttl_seconds,
                    )
                    fresh = receipt.realization
                    _validate_same_projection_record(item, fresh)
            if fresh.state is not ProjectionRealizationState.CLEANED:
                raise RealmIntegrityError(
                    "Operator attempt projection cleanup did not complete."
                )
            cleaned.append(fresh)
        return request_digest(
            {
                "format": "optpilot.operator-attempt-projection-cleanup.v1",
                "projection_spec_digest": spec.digest,
                "realizations": [
                    {
                        "plan_digest": item.plan_digest,
                        "realization_id": item.realization_id,
                        "state": item.state.value,
                    }
                    for item in cleaned
                ],
            }
        )

    def _cleanup_volume_operation(
        self,
        *,
        actor_principal_id: str,
        job: OperatorJobRecord,
        admission_identity: _AdmissionIdentity,
        operation_id: str,
        holder_id: str,
        logical_name: str,
        quota,
        quota_enforcement: str,
        allow_absent: bool,
        ttl_seconds: float,
    ) -> str:
        volume_key, volume_id, usage_lease_id = _volume_operation_identity(
            operation_id
        )
        try:
            record = self._volume_service._maintenance_volume(volume_id)
        except RealmNotFound:
            record = None
        if record is None:
            if not allow_absent:
                raise RealmIntegrityError(
                    "Launched Operator attempt is missing a durable volume."
                )
            return request_digest(
                {
                    "format": "optpilot.operator-attempt-volume-cleanup.v1",
                    "logical_name": logical_name,
                    "policy": "ephemeral",
                    "state": "absent",
                }
            )
        if (
            record.state is EphemeralVolumeState.QUARANTINED
            or record.volume_root_id != self._volume_service.root_binding.volume_root_id
            or record.owner_id != job.owner_id
            or record.parent_lease_id != admission_identity.lease_id
            or record.usage_lease_id != usage_lease_id
            or record.provider_kind != self._volume_service.root_binding.provider_kind
            or record.quota != quota
            or record.quota_enforcement != quota_enforcement
            or record.claim_nonce
            != request_digest(
                {
                    "format": "optpilot.ephemeral-volume-claim-nonce.v1",
                    "volume_id": volume_id,
                    "volume_root_id": (
                        self._volume_service.root_binding.volume_root_id
                    ),
                }
            )
            or record.relative_name != f"volume-{volume_key[:48]}"
        ):
            raise RealmConflict(
                "Operator attempt volume differs from its exact operation."
            )
        fresh = self._volume_service._maintenance_volume(record.volume_id)
        _validate_same_volume_record(record, fresh)
        if fresh.state is not EphemeralVolumeState.CLEANED:
            try:
                current_admission = self._ledger.validate_lease(
                    actor_principal_id=actor_principal_id,
                    lease_id=admission_identity.lease_id,
                    holder_id=admission_identity.holder_id,
                    fencing_token=admission_identity.fencing_token,
                )
                _validate_admission_lease(
                    current_admission,
                    identity=admission_identity,
                    job_id=job.job_id,
                    owner_id=job.owner_id,
                    operator_plan_digest=job.plan_digest,
                )
            except RealmIntegrityError:
                raise
            except RealmError:
                current_admission = None
            if (
                current_admission is not None
                and fresh.state
                in {EphemeralVolumeState.ALLOCATING, EphemeralVolumeState.ACTIVE}
            ):
                try:
                    volume = self._volume_service.recover_existing(
                        operation_id=operation_id,
                        actor_principal_id=actor_principal_id,
                        parent_lease=current_admission,
                        holder_id=holder_id,
                        quota=quota,
                        quota_enforcement=quota_enforcement,
                        ttl_seconds=ttl_seconds,
                    )
                    if volume.record.volume_id != fresh.volume_id:
                        volume._detach_without_release()
                        raise RealmIntegrityError(
                            "Operator attempt volume recovery selected another volume."
                        )
                    volume.close()
                except RealmIntegrityError:
                    raise
                except RealmError:
                    pass
                fresh = self._volume_service._maintenance_volume(record.volume_id)
                _validate_same_volume_record(record, fresh)
            if fresh.state is not EphemeralVolumeState.CLEANED:
                receipt = self._volume_service.reconcile_volume(
                    operation_id=_cleanup_operation_id(
                        operation_id,
                        resource_kind="volume",
                        logical_name=logical_name,
                    ),
                    volume_id=fresh.volume_id,
                    ttl_seconds=ttl_seconds,
                )
                fresh = receipt.volume
                _validate_same_volume_record(record, fresh)
        if fresh.state is not EphemeralVolumeState.CLEANED:
            raise RealmIntegrityError(
                "Operator attempt volume cleanup did not complete."
            )
        return request_digest(
            {
                "format": "optpilot.operator-attempt-volume-cleanup.v1",
                "logical_name": logical_name,
                "policy": fresh.portable_record(),
                "state": fresh.state.value,
            }
        )

    def _bind(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        attempt_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        ttl_seconds: float,
        create_missing: bool,
    ) -> ManagedOperatorAttemptBinding:
        actor_principal_id = required_text(
            actor_principal_id, "operator attempt actor principal id"
        )
        job_id = required_text(job_id, "operator job id")
        owner_id = required_text(owner_id, "operator job owner id")
        attempt_id = required_text(attempt_id, "operator debug attempt id")
        binding_id = required_text(binding_id, "operator attempt binding id")
        launch_token = required_text(launch_token, "operator attempt launch token")
        evidence_fingerprint = lower_hex_digest(
            evidence_fingerprint, "operator attempt evidence fingerprint"
        )
        if not isinstance(admission_lease, LeaseRecord):
            raise TypeError("admission_lease must be a LeaseRecord.")
        if not isinstance(evaluation_spec, EvaluationSpec):
            raise TypeError("evaluation_spec must be an EvaluationSpec.")
        if not isinstance(portable_spec, PortableAttemptRuntimeSpec):
            raise TypeError("portable_spec must be a PortableAttemptRuntimeSpec.")
        if (
            portable_spec.projection_spec.owner_id != owner_id
            or portable_spec.evaluation_spec_digest != evaluation_spec.digest
            or portable_spec.provider.kind != "process"
        ):
            raise RealmConflict(
                "Operator Job runtime differs from its owner or evaluation request."
            )
        owner = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if owner.owner_kind != "operator-job":
            raise RealmConflict(
                "Operator Job resources require an operator-job owner."
            )
        job = self._ledger.read_operator_job(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
        )
        derivation = self._ledger.read_owner_derivation(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
        )
        if (
            job.owner_id != owner_id
            or job.approval is None
            or job.state
            in {OperatorJobState.PLANNED, OperatorJobState.AWAITING_APPROVAL}
            or job.plan.owner_derivation_manifest_digest != derivation.digest
            or job.plan.backend_kind != "local-process"
            or job.plan.runtime_fingerprint != portable_spec.digest
            or job.plan.projection_contract_digest
            != portable_spec.projection_spec.digest
        ):
            raise RealmConflict(
                "Operator Job plan differs from the requested process resources."
            )
        current_admission = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=admission_lease.lease_id,
            holder_id=admission_lease.holder_id,
            fencing_token=admission_lease.fencing_token,
        )
        if (
            current_admission != admission_lease
            or current_admission.owner_id != owner_id
            or current_admission.lease_kind != "operator-job-admission"
            or current_admission.audience != "operator-job"
            or current_admission.scope_key != f"operator-job-admission:{job_id}"
            or current_admission.parent_lease_id is not None
            or dict(current_admission.metadata)
            != {"job_id": job_id, "plan_digest": job.plan_digest}
        ):
            raise RealmConflict(
                "Operator Job admission authority differs from the resource request."
            )
        _validate_admission_lease(
            current_admission,
            identity=_deterministic_admission_identity(job_id),
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=job.plan_digest,
        )
        if job.plan.evidence_sink_id != attempt_id:
            raise RealmConflict(
                "Operator Job attempt identity differs from its evidence sink."
            )

        store_id = self._resolve_projection_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            spec=portable_spec,
        )
        holder_id = _resource_holder_id(job_id=job_id, binding_id=binding_id)
        projection_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            resource_kind="projection",
            logical_name=portable_spec.projection_name,
        )
        metadata = {
            "binding_id": binding_id,
            "job_id": job_id,
            "logical_name": portable_spec.projection_name,
            "schema": "optpilot.operator-job-projection-consumer.v1",
        }
        try:
            projection = self._projection_service.recover_existing_private_read_only(
                operation_id=projection_operation,
                actor_principal_id=actor_principal_id,
                store_id=store_id,
                spec=portable_spec.projection_spec,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                consumer_kind="operator-job-attempt",
                consumer_metadata=metadata,
            )
        except RealmNotFound:
            if not create_missing:
                raise
            projection = self._projection_service.project_read_only(
                operation_id=projection_operation,
                actor_principal_id=actor_principal_id,
                store_id=store_id,
                spec=portable_spec.projection_spec,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                consumer_kind="operator-job-attempt",
                consumer_metadata=metadata,
                sharing_policy="private",
            )

        layered_scopes = tuple(
            scope
            for scope in portable_spec.scopes
            if isinstance(scope.source, LayeredVolumeScopeSource)
        )
        volumes = []
        pulse: _InitializationLeasePulse | None = None
        try:
            if layered_scopes:
                _require_private_projection(projection)
                pulse = _InitializationLeasePulse(
                    projection=projection,
                    volumes=(),
                    operation_prefix=(
                        "operator-binding.initialize/"
                        f"{job_id}/{binding_id}"
                    ),
                    ttl_seconds=ttl_seconds,
                )
                pulse.pulse(force=True)
                pulse.start()
            for requirement in portable_spec.writable_volumes:
                operation = _resource_operation_id(
                    job_id=job_id,
                    binding_id=binding_id,
                    resource_kind="volume",
                    logical_name=requirement.name,
                )
                try:
                    volume = self._volume_service.recover_existing(
                        operation_id=operation,
                        actor_principal_id=actor_principal_id,
                        parent_lease=current_admission,
                        holder_id=holder_id,
                        quota=requirement.quota,
                        quota_enforcement=requirement.quota_enforcement,
                        ttl_seconds=ttl_seconds,
                    )
                except RealmNotFound:
                    if not create_missing:
                        raise
                    volume = self._volume_service.create(
                        operation_id=operation,
                        actor_principal_id=actor_principal_id,
                        parent_lease=current_admission,
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
                    item.name: item for item in portable_spec.writable_volumes
                }
                assert pulse is not None

                def authorize_publication() -> None:
                    pulse.pulse(force=True)
                    self._refresh_initialization_authority(
                        actor_principal_id=actor_principal_id,
                        expected_job=job,
                        admission_lease=current_admission,
                        attempt_id=attempt_id,
                        evaluation_spec=evaluation_spec,
                        portable_spec=portable_spec,
                    )

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
                    pulse.pulse(force=True)
                    volume.initialize_layered(
                        source_root=source_root,
                        plan=plan,
                        initialization_identity=_layered_initialization_identity(
                            spec=portable_spec,
                            projection=projection,
                            source=source,
                        ),
                        authorize_publication=authorize_publication,
                        progress=pulse,
                    )
                pulse.pulse(force=True)
                pulse.stop()
            realized = RealizedProcessRuntimeResources(
                projection=projection,
                volumes=tuple(volumes),
                resolved_scopes=resolve_process_runtime_scopes(
                    portable_spec, projection, tuple(volumes)
                ),
            )
            local_binding = LocalAttemptExecutionBinding(
                attempt_id=attempt_id,
                binding_id=binding_id,
                launch_token=launch_token,
                evidence_fingerprint=evidence_fingerprint,
                evaluation_spec=evaluation_spec,
                portable_spec=portable_spec,
                scope_paths=MappingProxyType(
                    {
                        item.scope.name: item.host_path
                        for item in realized.resolved_scopes
                    }
                ),
                validate_resources=lambda: validate_process_runtime_resources(
                    resources=realized,
                    projection_name=portable_spec.projection_name,
                ),
            )
            return ManagedOperatorAttemptBinding(
                job_id=job_id,
                operator_plan_digest=job.plan_digest,
                projection_service=self._projection_service,
                local_binding=local_binding,
                resources=realized,
            )
        except BaseException as error:
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
            # As with canonical attempts, deterministic resources stay retained
            # for exact recovery or TTL reconciliation.  Releasing a partial
            # set here could race a concurrent authorized realizer.
            error.add_note(
                "Partially realized Operator Job resources were retained for "
                "exact recovery and TTL cleanup."
            )
            raise

    def _refresh_initialization_authority(
        self,
        *,
        actor_principal_id: str,
        expected_job: OperatorJobRecord,
        admission_lease: LeaseRecord,
        attempt_id: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
    ) -> None:
        """Fence layered-volume publication against stop or lease loss."""

        current_job = self._validate_job_runtime(
            actor_principal_id=actor_principal_id,
            job_id=expected_job.job_id,
            owner_id=expected_job.owner_id,
            operator_plan_digest=expected_job.plan_digest,
            attempt_id=attempt_id,
            evaluation_spec=evaluation_spec,
            portable_spec=portable_spec,
            require_terminal=False,
        )
        if current_job != expected_job:
            raise RealmConflict(
                "Operator Job authority changed during provider initialization."
            )
        refreshed_admission = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=admission_lease.lease_id,
            holder_id=admission_lease.holder_id,
            fencing_token=admission_lease.fencing_token,
        )
        _validate_admission_lease(
            refreshed_admission,
            identity=_deterministic_admission_identity(expected_job.job_id),
            job_id=expected_job.job_id,
            owner_id=expected_job.owner_id,
            operator_plan_digest=expected_job.plan_digest,
        )

    def _resolve_projection_store(
        self,
        *,
        actor_principal_id: str,
        owner_id: str,
        spec: PortableAttemptRuntimeSpec,
        require_available: bool = True,
    ) -> str:
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        return _resolve_exact_projection_store(
            spec=spec,
            memberships=memberships,
            available_store_ids=self._projection_service.available_store_ids,
            require_available=require_available,
        )


def _resource_holder_id(*, job_id: str, binding_id: str) -> str:
    digest = request_digest(
        {
            "format": "optpilot.operator-job-resource-holder.v1",
            "binding_id": binding_id,
            "job_id": job_id,
        }
    )
    return f"operator-job-resource-{digest[:48]}"


def _resource_operation_id(
    *,
    job_id: str,
    binding_id: str,
    resource_kind: str,
    logical_name: str,
) -> str:
    digest = request_digest(
        {
            "format": "optpilot.operator-job-resource-operation.v1",
            "binding_id": binding_id,
            "job_id": job_id,
            "logical_name": logical_name,
            "resource_kind": resource_kind,
        }
    )
    return f"operator-job.resource/{digest}"


def _projection_metadata(
    *, job_id: str, binding_id: str, logical_name: str
) -> dict[str, str]:
    return {
        "binding_id": binding_id,
        "job_id": job_id,
        "logical_name": logical_name,
        "schema": "optpilot.operator-job-projection-consumer.v1",
    }


def _deterministic_admission_identity(job_id: str) -> _AdmissionIdentity:
    def logical_id(prefix: str) -> str:
        digest = request_digest(
            {
                "payload": {"job_id": job_id},
                "schema": f"optpilot.{prefix}.v1",
            }
        )
        return f"{prefix}-{digest[:40]}"

    return _AdmissionIdentity(
        lease_id=logical_id("operator-job-admission"),
        holder_id=logical_id("operator-job-holder"),
        fencing_token=1,
    )


def _validate_admission_lease(
    lease: LeaseRecord,
    *,
    identity: _AdmissionIdentity,
    job_id: str,
    owner_id: str,
    operator_plan_digest: str,
) -> None:
    if (
        lease.lease_id != identity.lease_id
        or lease.holder_id != identity.holder_id
        or lease.fencing_token != identity.fencing_token
        or lease.owner_id != owner_id
        or lease.lease_kind != "operator-job-admission"
        or lease.audience != "operator-job"
        or lease.scope_key != f"operator-job-admission:{job_id}"
        or lease.parent_lease_id is not None
        or dict(lease.metadata)
        != {"job_id": job_id, "plan_digest": operator_plan_digest}
    ):
        raise RealmConflict(
            "Operator Job admission identity differs from its Debug resources."
        )


def _validate_same_projection_record(
    expected: ProjectionRealizationRecord,
    actual: ProjectionRealizationRecord,
) -> None:
    comparisons = {
        "realization_id": actual.realization_id == expected.realization_id,
        "projection_root_id": actual.projection_root_id == expected.projection_root_id,
        "owner_id": actual.owner_id == expected.owner_id,
        "store_id": actual.store_id == expected.store_id,
        "spec_digest": actual.spec_digest == expected.spec_digest,
        "spec": thaw_json(actual.spec) == thaw_json(expected.spec),
        "availability_resolution_digest": (
            actual.availability_resolution_digest
            == expected.availability_resolution_digest
        ),
        "availability_resolution": (
            thaw_json(actual.availability_resolution)
            == thaw_json(expected.availability_resolution)
        ),
        "request_digest": actual.request_digest == expected.request_digest,
        "provider_kind": actual.provider_kind == expected.provider_kind,
        "claim_nonce": actual.claim_nonce == expected.claim_nonce,
        "relative_name": actual.relative_name == expected.relative_name,
        "plan_digest": (
            expected.plan_digest is None
            or actual.plan_digest == expected.plan_digest
        ),
        "copied_logical_bytes": (
            expected.copied_logical_bytes is None
            or actual.copied_logical_bytes == expected.copied_logical_bytes
        ),
        "copied_file_count": (
            expected.copied_file_count is None
            or actual.copied_file_count == expected.copied_file_count
        ),
    }
    changed = sorted(name for name, equal in comparisons.items() if not equal)
    if changed:
        raise RealmIntegrityError(
            "Operator attempt projection immutable identity changed during cleanup: "
            f"{changed!r}."
        )


def _validate_same_volume_record(
    expected: EphemeralVolumeRecord,
    actual: EphemeralVolumeRecord,
) -> None:
    if (
        actual.volume_id != expected.volume_id
        or actual.volume_root_id != expected.volume_root_id
        or actual.owner_id != expected.owner_id
        or actual.parent_lease_id != expected.parent_lease_id
        or actual.usage_lease_id != expected.usage_lease_id
        or actual.provider_kind != expected.provider_kind
        or actual.quota != expected.quota
        or actual.quota_enforcement != expected.quota_enforcement
        or actual.claim_nonce != expected.claim_nonce
        or actual.relative_name != expected.relative_name
    ):
        raise RealmIntegrityError(
            "Operator attempt volume immutable identity changed during cleanup."
        )


def _cleanup_operation_id(
    operation_id: str, *, resource_kind: str, logical_name: str
) -> str:
    digest = request_digest(
        {
            "format": "optpilot.operator-attempt-terminal-cleanup.v1",
            "logical_name": logical_name,
            "operation_id": operation_id,
            "resource_kind": resource_kind,
        }
    )
    return f"operator-attempt.cleanup.{resource_kind}/{digest}"


def _terminal_authority_digest(
    authority: WorkerTerminalProof | ProcessLaunchSealReceipt,
) -> str:
    if isinstance(authority, WorkerTerminalProof):
        return hashlib.sha256(authority.canonical_bytes).hexdigest()
    # ``absent`` is the first successful seal and ``sealed`` is its replay.
    # They prove the same negative provider decision, so cleanup evidence must
    # not depend on which side of a crash observed that decision.
    if not authority.sealed:
        raise RealmConflict(
            "An existing launch receipt is not negative cleanup authority."
        )
    return request_digest(
        {
            "binding_id": authority.binding_id,
            "format": "optpilot.operator-attempt-negative-launch-authority.v1",
            "launch_token": authority.launch_token,
            "sealed": True,
        }
    )


__all__ = [
    "ManagedOperatorAttemptBinding",
    "OPERATOR_ATTEMPT_CLEANUP_EVIDENCE_SCHEMA",
    "OPERATOR_JOB_RESOURCE_TTL_SECONDS",
    "OperatorAttemptCleanupEvidence",
    "RealmOperatorAttemptBinder",
]
