"""Provider-private realization for one approved Environment Preview.

The portable :mod:`environment_preview` compiler deliberately stops before
store placement, host paths, leases, and provider handles.  This module binds
that immutable plan to one exact Operator Job admission fence and to the local
authenticated-container provider.  It never consults mutable catalog state and
never creates a disposable workspace copy: immutable lower layers are attached
through one private projection and all writable state lives in one bounded,
job-owned ephemeral volume.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ._validation import lower_hex_digest, required_text, thaw_json
from .environment_preview import (
    ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
    EnvironmentPreviewPlan,
    compile_environment_preview_plan,
)
from .ephemeral_volume_service import (
    ManagedEphemeralVolume,
    RealmEphemeralVolumeService,
    _volume_operation_identity,
)
from .ephemeral_volume_namespace import (
    _DATA_NAME as _VOLUME_DATA_NAME,
    attach_ephemeral_volume_namespace,
)
from .ephemeral_volume_records import EphemeralVolumeRecord, EphemeralVolumeState
from .errors import RealmConflict, RealmError, RealmIntegrityError, RealmNotFound
from .filesystem_quota import FilesystemQuota
from .inspection import ResolvedCandidateInspectionTarget
from .leases import LeaseRecord
from .ledger import RealmLedger
from .local_container_web_provider import (
    ContainerWebLaunchRequest,
    ContainerWebMount,
    ContainerWebRunIdentity,
    LocalContainerWebProvider,
    LocalContainerWebTerminal,
)
from .operator_job_records import OperatorJobRecord, OperatorJobState
from .owners import OwnerPermission
from .projection import ProjectionSpec, TreeMapping
from .projection_namespace import _EXPOSED_TREE_NAME
from .projection_records import ProjectionRealizationRecord, ProjectionRealizationState
from .projection_service import (
    ManagedReadOnlyProjection,
    RealmProjectionService,
    _require_private_operation_resolution,
)
from .refs import SnapshotRef, canonical_json_bytes, request_digest
from .run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    ScopeLayer,
)
from .run_records import RUN_CANDIDATE_ROLE


ENVIRONMENT_PREVIEW_BINDING_EVIDENCE_SCHEMA = (
    "optpilot.environment-preview-binding-evidence.v1"
)
ENVIRONMENT_PREVIEW_CLEANUP_EVIDENCE_SCHEMA = (
    "optpilot.environment-preview-cleanup-evidence.v1"
)
ENVIRONMENT_PREVIEW_RESOURCE_TTL_SECONDS = 300.0
ENVIRONMENT_PREVIEW_PIDS_LIMIT = 256
ENVIRONMENT_PREVIEW_VOLUME_QUOTA = FilesystemQuota(
    max_entries=10_000,
    max_file_bytes=256 * 1024**2,
    max_total_bytes=1024**3,
)

_PREVIEW_JOB_KIND = "environment-preview"
_PREVIEW_TARGET_KIND = "environment-interface"
_PREVIEW_INPUT_SCHEMA = "optpilot.environment-preview-input.v1"
_PREVIEW_BACKEND_KIND = "local-container-web"
_PREVIEW_BACKEND_REALM = "local-host"
_LAYOUT_DIRECTORY = "preview"
_LAYOUT_STAGING = ".environment-preview-initializing"
_LAYOUT_MARKER = ".environment-preview-layout"
_LAYOUT_MARKER_TEMP = ".environment-preview-layout.tmp"
_PROJECTION_APP_PARTITION = "app"
_PROJECTION_CANDIDATE_PARTITION = "candidate"
_LAYOUT_DIRECTORIES = (
    "artifacts",
    "control",
    "output",
    "prepared_outputs",
    "runtime_env",
    "workspace",
)
_MIN_CONTAINER_CPU_MILLIS = 200
_MIN_CONTAINER_MEMORY_BYTES = 128 * 1024**2
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _path_free_identifier(value: object, label: str) -> str:
    result = required_text(value, label, max_bytes=512)
    if (
        "/" in result
        or "\\" in result
        or result.startswith((".", "~"))
        or (len(result) >= 2 and result[1] == ":" and result[0].isalpha())
    ):
        raise ValueError(f"{label} must be a path-free logical identifier.")
    return result


def _exact_keys(payload: Mapping[str, object], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


@dataclass(frozen=True)
class EnvironmentPreviewBindingEvidence:
    """Path-free identity of one exact provider-private realization."""

    job_id: str
    binding_id: str
    launch_token: str
    operator_plan_digest: str
    preview_plan_digest: str
    projection_spec_digest: str
    context_digest: str
    resources_digest: str
    launch_request_digest: str
    schema_version: str = ENVIRONMENT_PREVIEW_BINDING_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ENVIRONMENT_PREVIEW_BINDING_EVIDENCE_SCHEMA:
            raise ValueError("environment preview binding evidence schema is unsupported.")
        for name in ("job_id", "binding_id", "launch_token"):
            _path_free_identifier(
                getattr(self, name), f"environment preview binding {name}"
            )
        for name in (
            "operator_plan_digest",
            "preview_plan_digest",
            "projection_spec_digest",
            "context_digest",
            "resources_digest",
            "launch_request_digest",
        ):
            lower_hex_digest(
                getattr(self, name),
                f"environment preview binding {name}",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "context_digest": self.context_digest,
            "job_id": self.job_id,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "operator_plan_digest": self.operator_plan_digest,
            "preview_plan_digest": self.preview_plan_digest,
            "projection_spec_digest": self.projection_spec_digest,
            "resources_digest": self.resources_digest,
            "schema_version": self.schema_version,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "EnvironmentPreviewBindingEvidence":
        try:
            _exact_keys(
                payload,
                set(cls.__dataclass_fields__),
                "environment preview binding evidence",
            )
            result = cls(**dict(payload))
            if result.to_dict() != dict(payload):
                raise ValueError("environment preview binding evidence is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted environment preview binding evidence is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class EnvironmentPreviewCleanupEvidence:
    """Portable proof that each provider-private cleanup phase completed."""

    job_id: str
    binding_id: str
    launch_token: str
    operator_plan_digest: str
    preview_plan_digest: str
    launch_request_digest: str
    terminal_digest: str
    provider_cleanup_digest: str
    projection_cleanup_digest: str
    volume_cleanup_digest: str
    schema_version: str = ENVIRONMENT_PREVIEW_CLEANUP_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != ENVIRONMENT_PREVIEW_CLEANUP_EVIDENCE_SCHEMA:
            raise ValueError("environment preview cleanup evidence schema is unsupported.")
        for name in ("job_id", "binding_id", "launch_token"):
            _path_free_identifier(
                getattr(self, name), f"environment preview cleanup {name}"
            )
        for name in (
            "operator_plan_digest",
            "preview_plan_digest",
            "launch_request_digest",
            "terminal_digest",
            "provider_cleanup_digest",
            "projection_cleanup_digest",
            "volume_cleanup_digest",
        ):
            lower_hex_digest(
                getattr(self, name), f"environment preview cleanup {name}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "job_id": self.job_id,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "operator_plan_digest": self.operator_plan_digest,
            "preview_plan_digest": self.preview_plan_digest,
            "projection_cleanup_digest": self.projection_cleanup_digest,
            "provider_cleanup_digest": self.provider_cleanup_digest,
            "schema_version": self.schema_version,
            "terminal_digest": self.terminal_digest,
            "volume_cleanup_digest": self.volume_cleanup_digest,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "EnvironmentPreviewCleanupEvidence":
        try:
            _exact_keys(
                payload,
                set(cls.__dataclass_fields__),
                "environment preview cleanup evidence",
            )
            result = cls(**dict(payload))
            if result.to_dict() != dict(payload):
                raise ValueError("environment preview cleanup evidence is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted environment preview cleanup evidence is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class _PreviewLayoutPaths:
    context: Path
    outputs_file: Path
    workspace: Path
    artifacts: Path
    output: Path
    runtime_env: Path
    prepared_outputs: Path


@dataclass(frozen=True)
class _PreviewControlIdentity:
    directory_device: int
    directory_inode: int
    file_device: int
    file_inode: int

    def __post_init__(self) -> None:
        for value, label, allow_zero in (
            (self.directory_device, "control directory device", True),
            (self.directory_inode, "control directory inode", False),
            (self.file_device, "control file device", True),
            (self.file_inode, "control file inode", False),
        ):
            minimum = 0 if allow_zero else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"Environment Preview {label} is invalid.")


class _PreviewLayoutAttachmentTracker:
    """Remember descriptor observations only for this binder attachment.

    The enclosing ephemeral-volume claim and immutable layout claim select the
    durable control namespace across launches.  Device and inode numbers then
    fence path replacement while one binder remains attached, but are not
    persisted as cross-launch ownership facts.
    """

    def __init__(self) -> None:
        self._observations: dict[
            str, tuple[str, _PreviewControlIdentity]
        ] = {}
        self._lock = threading.RLock()

    def observe(
        self,
        *,
        attachment_key: str,
        control_claim_nonce: str,
        identity: _PreviewControlIdentity,
        replace_missing_claim: bool = False,
    ) -> None:
        lower_hex_digest(
            attachment_key, "Environment Preview layout attachment key"
        )
        lower_hex_digest(
            control_claim_nonce, "Environment Preview control claim nonce"
        )
        if not isinstance(identity, _PreviewControlIdentity):
            raise TypeError("identity must be a _PreviewControlIdentity.")
        if not isinstance(replace_missing_claim, bool):
            raise TypeError("replace_missing_claim must be a boolean.")
        observation = (control_claim_nonce, identity)
        with self._lock:
            previous = self._observations.get(attachment_key)
            if (
                previous is not None
                and previous != observation
                and not replace_missing_claim
            ):
                raise RealmIntegrityError(
                    "Environment Preview output control identity was replaced "
                    "while attached."
                )
            self._observations[attachment_key] = observation


@dataclass(frozen=True)
class EnvironmentPreviewOutputCaptureDescriptor:
    """Supervisor-private paths that authorize capture from only ``output``.

    This descriptor deliberately has no portable or serialization form.  It is
    meaningful only after the binder has validated either the live binding or
    the exact retained terminal launch coordinates that issued it.
    """

    source_root: Path
    control_file: Path

    def __post_init__(self) -> None:
        source_root = Path(self.source_root)
        control_file = Path(self.control_file)
        if not source_root.is_absolute() or not control_file.is_absolute():
            raise ValueError(
                "Environment Preview output capture paths must be absolute."
            )
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "control_file", control_file)


@dataclass(frozen=True)
class _PreviewAdmissionIdentity:
    lease_id: str
    holder_id: str
    fencing_token: int


class ManagedEnvironmentPreviewBinding:
    """Exact request plus provider-private resource handles for one Preview."""

    def __init__(
        self,
        *,
        provider: LocalContainerWebProvider,
        projection_service: RealmProjectionService,
        projection: ManagedReadOnlyProjection,
        volume: ManagedEphemeralVolume,
        request: ContainerWebLaunchRequest,
        evidence: EnvironmentPreviewBindingEvidence,
        preview_plan: EnvironmentPreviewPlan,
        projection_spec: ProjectionSpec,
        layout_attachment_tracker: _PreviewLayoutAttachmentTracker,
        layout_attachment_key: str,
        authority_validator: Callable[[], LeaseRecord],
    ) -> None:
        if not isinstance(provider, LocalContainerWebProvider):
            raise TypeError("provider must be a LocalContainerWebProvider.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(projection, ManagedReadOnlyProjection):
            raise TypeError("projection must be a ManagedReadOnlyProjection.")
        if not isinstance(volume, ManagedEphemeralVolume):
            raise TypeError("volume must be a ManagedEphemeralVolume.")
        if not isinstance(request, ContainerWebLaunchRequest):
            raise TypeError("request must be a ContainerWebLaunchRequest.")
        if not isinstance(evidence, EnvironmentPreviewBindingEvidence):
            raise TypeError("evidence must be EnvironmentPreviewBindingEvidence.")
        if not isinstance(preview_plan, EnvironmentPreviewPlan):
            raise TypeError("preview_plan must be an EnvironmentPreviewPlan.")
        if not isinstance(projection_spec, ProjectionSpec):
            raise TypeError("projection_spec must be a ProjectionSpec.")
        if not isinstance(
            layout_attachment_tracker, _PreviewLayoutAttachmentTracker
        ):
            raise TypeError(
                "layout_attachment_tracker must be a "
                "_PreviewLayoutAttachmentTracker."
            )
        lower_hex_digest(
            layout_attachment_key,
            "Environment Preview layout attachment key",
        )
        if not callable(authority_validator):
            raise TypeError("authority_validator must be callable.")
        self._provider = provider
        self._projection_service = projection_service
        self._projection = projection
        self._volume = volume
        self._request = request
        self._evidence = evidence
        self._preview_plan = preview_plan
        self._projection_spec = projection_spec
        self._layout_attachment_tracker = layout_attachment_tracker
        self._layout_attachment_key = layout_attachment_key
        self._authority_validator = authority_validator
        self._provider_released = False
        self._projection_released = False
        self._volume_released = False
        self._released = False
        self._lock = threading.RLock()

    @property
    def request(self) -> ContainerWebLaunchRequest:
        """Return provider-private launch data to the local supervisor only."""

        return self._request

    @property
    def evidence(self) -> EnvironmentPreviewBindingEvidence:
        return self._evidence

    @property
    def output_capture_descriptor(self) -> EnvironmentPreviewOutputCaptureDescriptor:
        """Return live output-only capture authority after exact revalidation."""

        if not self._preview_plan.outputs_enabled:
            raise RealmConflict(
                "This Environment Preview profile does not declare outputs."
            )
        self.validate()
        output_mount = next(
            (
                item
                for item in self._request.mounts
                if item.container_path == self._preview_plan.paths.output_root
            ),
            None,
        )
        control_mount = next(
            (
                item
                for item in self._request.mounts
                if item.container_path == self._preview_plan.paths.outputs_file
            ),
            None,
        )
        if (
            output_mount is None
            or output_mount.mode != "read-write"
            or control_mount is None
            or control_mount.mode != "read-write"
        ):
            raise RealmIntegrityError(
                "Environment Preview output capture authority has changed."
            )
        return EnvironmentPreviewOutputCaptureDescriptor(
            source_root=output_mount.host_path,
            control_file=control_mount.host_path,
        )

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def validate(self) -> None:
        """Re-prove admission, namespaces, layout, and exact request identity."""

        with self._lock:
            if self._released:
                raise RealmConflict("Environment Preview resources are released.")
            self._authority_validator()
            if not self._provider.is_gateway_image_trusted(
                self._preview_plan.runtime.image_ref
            ):
                raise RealmConflict(
                    "Environment Preview image is no longer trusted for authenticated ingress."
                )
            self._projection.validate()
            self._volume.validate()
            layout = _prepare_or_validate_layout(
                self._volume,
                context_bytes=self._preview_plan.context.canonical_bytes,
                attachment_tracker=self._layout_attachment_tracker,
                attachment_key=self._layout_attachment_key,
            )
            expected_request = _launch_request(
                job_id=self._request.job_id,
                binding_id=self._request.binding_id,
                launch_token=self._request.launch_token,
                operator_plan_digest=self._evidence.operator_plan_digest,
                plan=self._preview_plan,
                app_path=_projected_app_path(
                    self._projection.root_path, self._preview_plan
                ),
                layout=layout,
            )
            expected_evidence = _binding_evidence(
                request=expected_request,
                operator_plan_digest=self._evidence.operator_plan_digest,
                plan=self._preview_plan,
                projection_spec=self._projection_spec,
                projection=self._projection,
                volume=self._volume,
            )
            if expected_request != self._request or expected_evidence != self._evidence:
                raise RealmIntegrityError(
                    "Environment Preview provider binding identity has changed."
                )

    def release_after_terminal(self, terminal: LocalContainerWebTerminal) -> None:
        """Retire provider state and mounts only after exact terminal proof."""

        if not isinstance(terminal, LocalContainerWebTerminal):
            raise TypeError("terminal must be a LocalContainerWebTerminal.")
        request = self._request
        if (
            terminal.job_id != request.job_id
            or terminal.binding_id != request.binding_id
            or terminal.launch_token != request.launch_token
            or terminal.launch_request_digest != request.digest
        ):
            raise RealmConflict(
                "Container terminal evidence differs from the Environment Preview request."
            )
        with self._lock:
            if self._released:
                return
            if not self._provider_released:
                self._provider.cleanup(request)
                self._provider_released = True
            if not self._projection_released:
                self._projection_service.retire_private_projection(self._projection)
                self._projection_released = True
            if not self._volume_released:
                self._volume.close()
                self._volume_released = True
            self._released = True

    def detach_after_terminal_cleanup(
        self, evidence: EnvironmentPreviewCleanupEvidence
    ) -> None:
        """Close local attachments after durable cleanup was proven."""

        if not isinstance(evidence, EnvironmentPreviewCleanupEvidence):
            raise TypeError(
                "evidence must be EnvironmentPreviewCleanupEvidence."
            )
        if (
            evidence.job_id != self._evidence.job_id
            or evidence.binding_id != self._evidence.binding_id
            or evidence.launch_token != self._evidence.launch_token
            or evidence.operator_plan_digest
            != self._evidence.operator_plan_digest
            or evidence.preview_plan_digest != self._preview_plan.digest
            or evidence.launch_request_digest != self._request.digest
        ):
            raise RealmConflict(
                "Environment Preview cleanup evidence differs from its attachment."
            )
        with self._lock:
            if self._released:
                return
            self._volume._detach_without_release()
            self._volume_released = True
            self._projection._detach_after_private_retirement()
            self._projection_released = True
            self._provider_released = True
            self._released = True

    def detach_for_recovery(self) -> None:
        """Close local attachments without retiring durable Preview resources.

        A later supervisor can reconstruct fresh attachments from the exact
        ledger binding.  Provider state, the projection consumer, and the
        ephemeral volume deliberately remain live until terminal cleanup.
        """

        with self._lock:
            if self._released:
                return
            self._volume._detach_without_release()
            self._volume_released = True
            self._projection._detach_without_release()
            self._projection_released = True
            self._released = True


class RealmEnvironmentPreviewBinder:
    """Bind a retained Preview plan under exact Operator Job authority."""

    def __init__(
        self,
        ledger: RealmLedger,
        projection_service: RealmProjectionService,
        volume_service: RealmEphemeralVolumeService,
        provider: LocalContainerWebProvider,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(volume_service, RealmEphemeralVolumeService):
            raise TypeError("volume_service must be a RealmEphemeralVolumeService.")
        if not isinstance(provider, LocalContainerWebProvider):
            raise TypeError("provider must be a LocalContainerWebProvider.")
        if projection_service.ledger is not ledger or volume_service.ledger is not ledger:
            raise ValueError("Environment Preview services must share one Realm ledger.")
        self._ledger = ledger
        self._projection_service = projection_service
        self._volume_service = volume_service
        self._provider = provider
        self._layout_attachments = _PreviewLayoutAttachmentTracker()

    def validate_plan(self, preview_plan: EnvironmentPreviewPlan) -> None:
        """Pure provider-capability check used before exposing a launch action."""

        if not isinstance(preview_plan, EnvironmentPreviewPlan):
            raise TypeError("preview_plan must be an EnvironmentPreviewPlan.")
        _validate_provider_plan(self._provider, preview_plan)

    def realize(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        ttl_seconds: float = ENVIRONMENT_PREVIEW_RESOURCE_TTL_SECONDS,
    ) -> ManagedEnvironmentPreviewBinding:
        """Recover deterministic resources or create only missing resources."""

        return self._bind(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            admission_lease=admission_lease,
            operator_plan_digest=operator_plan_digest,
            binding_id=binding_id,
            launch_token=launch_token,
            target=target,
            preview_plan=preview_plan,
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
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        ttl_seconds: float = ENVIRONMENT_PREVIEW_RESOURCE_TTL_SECONDS,
    ) -> ManagedEnvironmentPreviewBinding:
        """Reattach exact existing resources without creating missing ones."""

        return self._bind(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            admission_lease=admission_lease,
            operator_plan_digest=operator_plan_digest,
            binding_id=binding_id,
            launch_token=launch_token,
            target=target,
            preview_plan=preview_plan,
            ttl_seconds=ttl_seconds,
            create_missing=False,
        )

    def recover_stop_request(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
    ) -> ContainerWebLaunchRequest:
        """Rebuild an exact launched provider request without live authority.

        This is a stop-only recovery coordinate: it never creates, attaches,
        or renews a Realm resource.  The durable launch intent and immutable
        provider-private resource records must reconstruct the same request.
        """

        _projection, _volume, request = self._recover_launched_coordinates(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            binding_id=binding_id,
            launch_token=launch_token,
            target=target,
            preview_plan=preview_plan,
            recovery_kind="stop",
        )
        return request

    def recover_terminal_output_capture(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        terminal: LocalContainerWebTerminal,
    ) -> EnvironmentPreviewOutputCaptureDescriptor:
        """Issue output-only capture authority after exact terminal proof.

        Unlike live binding recovery, this path deliberately does not require
        admission authority.  It reconstructs the launched request from the
        immutable job intent and provider-private resource records, requires
        the exact provider terminal for that request, and validates the
        already-published volume namespace and nonce-bound control layout without
        creating, attaching as a managed resource, repairing, or renewing
        anything.
        """

        if not preview_plan.outputs_enabled:
            raise RealmConflict(
                "This Environment Preview profile does not declare outputs."
            )
        if not isinstance(terminal, LocalContainerWebTerminal):
            raise TypeError("terminal must be a LocalContainerWebTerminal.")
        _projection, volume, request = self._recover_launched_coordinates(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            binding_id=binding_id,
            launch_token=launch_token,
            target=target,
            preview_plan=preview_plan,
            recovery_kind="terminal output capture",
        )
        if (
            terminal.job_id != request.job_id
            or terminal.binding_id != request.binding_id
            or terminal.launch_token != request.launch_token
            or terminal.launch_request_digest != request.digest
        ):
            raise RealmConflict(
                "Container terminal evidence differs from the Environment Preview request."
            )
        if volume.state is not EphemeralVolumeState.ACTIVE:
            raise RealmConflict(
                "Environment Preview output volume is no longer retained for capture."
            )

        namespace = attach_ephemeral_volume_namespace(
            self._volume_service.root_binding,
            self._volume_service._claim(volume),
            self._volume_service._current_identity(volume),
        )
        try:
            layout = _validate_existing_layout(
                namespace.path,
                context_bytes=preview_plan.context.canonical_bytes,
                attachment_tracker=self._layout_attachments,
                attachment_key=_layout_attachment_key(volume),
            )
        finally:
            namespace.close()

        output_mounts = tuple(
            item
            for item in request.mounts
            if item.container_path == preview_plan.paths.output_root
        )
        control_mounts = tuple(
            item
            for item in request.mounts
            if item.container_path == preview_plan.paths.outputs_file
        )
        if (
            len(output_mounts) != 1
            or output_mounts[0].mode != "read-write"
            or output_mounts[0].host_path != layout.output
            or len(control_mounts) != 1
            or control_mounts[0].mode != "read-write"
            or control_mounts[0].host_path != layout.outputs_file
        ):
            raise RealmIntegrityError(
                "Environment Preview terminal output capture authority has changed."
            )
        return EnvironmentPreviewOutputCaptureDescriptor(
            source_root=layout.output,
            control_file=layout.outputs_file,
        )

    def _recover_launched_coordinates(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        recovery_kind: str,
    ) -> tuple[
        ProjectionRealizationRecord,
        EphemeralVolumeRecord,
        ContainerWebLaunchRequest,
    ]:
        """Reconstruct one exact launched request without live authority."""

        recovery_kind = required_text(
            recovery_kind, "Environment Preview recovery kind", max_bytes=128
        )
        label = f"Environment Preview {recovery_kind}"
        actor_principal_id = required_text(
            actor_principal_id, f"{label} actor principal id"
        )
        job_id = _path_free_identifier(job_id, f"{label} job id")
        owner_id = _path_free_identifier(owner_id, f"{label} owner id")
        binding_id = _path_free_identifier(binding_id, f"{label} binding id")
        launch_token = _path_free_identifier(launch_token, f"{label} launch token")
        operator_plan_digest = lower_hex_digest(
            operator_plan_digest, f"{label} Operator Job plan digest"
        )
        if not isinstance(target, ResolvedCandidateInspectionTarget):
            raise TypeError("target must be a ResolvedCandidateInspectionTarget.")
        if not isinstance(preview_plan, EnvironmentPreviewPlan):
            raise TypeError("preview_plan must be an EnvironmentPreviewPlan.")
        if compile_environment_preview_plan(
            target, profile_id=preview_plan.profile_id
        ) != preview_plan:
            raise RealmConflict(
                f"{label} plan differs from the retained target."
            )
        _validate_retained_prepared_layers(target=target, plan=preview_plan)
        job = self._validate_job_plan(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
        )
        intent = job.launch_intent
        admission_identity = _deterministic_admission_identity(job_id)
        if (
            intent is None
            or intent.binding_id != binding_id
            or intent.launch_token != launch_token
            or intent.provider_kind != _PREVIEW_BACKEND_KIND
            or intent.admission_lease_id != admission_identity.lease_id
            or intent.admission_holder_id != admission_identity.holder_id
            or intent.admission_fencing_token != admission_identity.fencing_token
        ):
            raise RealmConflict(
                f"{label} authority differs from its launch intent."
            )
        projection_spec, expected_placements = _projection_spec(
            owner_id=owner_id, target=target
        )
        store_id = self._resolve_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            expected_placements=expected_placements,
        )
        projection_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="projection",
        )
        volume_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="volume",
        )
        return self._cleanup_coordinates(
            job_id=job_id,
            owner_id=owner_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
            projection_spec=projection_spec,
            projection_operation=projection_operation,
            volume_operation=volume_operation,
            store_id=store_id,
            admission_lease_id=admission_identity.lease_id,
            expected_launch_request_digest=intent.launch_request_digest,
        )

    def cleanup_after_terminal(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord | None = None,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        terminal: LocalContainerWebTerminal | None = None,
        ttl_seconds: float = ENVIRONMENT_PREVIEW_RESOURCE_TTL_SECONDS,
    ) -> EnvironmentPreviewCleanupEvidence:
        """Replay provider, projection, and volume cleanup independently.

        Unlike :meth:`recover_existing`, this path does not require both live
        resources.  It reconstructs the exact provider request from durable
        provider-private resource records, proves it against the terminal
        Operator Job commit, and then reconciles each remaining component.
        Exact replay therefore survives a crash after any earlier phase.
        """

        actor_principal_id = required_text(
            actor_principal_id, "Environment Preview cleanup actor principal id"
        )
        job_id = _path_free_identifier(job_id, "Environment Preview cleanup job id")
        owner_id = _path_free_identifier(
            owner_id, "Environment Preview cleanup owner id"
        )
        binding_id = _path_free_identifier(
            binding_id, "Environment Preview cleanup binding id"
        )
        launch_token = _path_free_identifier(
            launch_token, "Environment Preview cleanup launch token"
        )
        operator_plan_digest = lower_hex_digest(
            operator_plan_digest,
            "Environment Preview cleanup Operator Job plan digest",
        )
        if admission_lease is not None and not isinstance(admission_lease, LeaseRecord):
            raise TypeError("admission_lease must be a LeaseRecord or None.")
        if not isinstance(target, ResolvedCandidateInspectionTarget):
            raise TypeError("target must be a ResolvedCandidateInspectionTarget.")
        if not isinstance(preview_plan, EnvironmentPreviewPlan):
            raise TypeError("preview_plan must be an EnvironmentPreviewPlan.")
        if terminal is not None and not isinstance(terminal, LocalContainerWebTerminal):
            raise TypeError("terminal must be a LocalContainerWebTerminal or None.")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("Environment Preview cleanup ttl_seconds must be positive.")
        recomputed = compile_environment_preview_plan(
            target, profile_id=preview_plan.profile_id
        )
        if recomputed != preview_plan:
            raise RealmConflict(
                "Environment Preview cleanup plan differs from the retained target."
            )
        _validate_retained_prepared_layers(target=target, plan=preview_plan)
        job = self._validate_job_plan(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
        )
        intent = job.launch_intent
        outcome = job.outcome
        admission_identity = _deterministic_admission_identity(job_id)
        _validate_supplied_admission(
            admission_lease,
            identity=admission_identity,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
        )
        if (
            not job.state.terminal
            or outcome is None
        ):
            raise RealmConflict(
                "Environment Preview cleanup requires a terminal Operator Job."
            )
        if intent is None:
            return self._cleanup_cancelled_before_launch(
                actor_principal_id=actor_principal_id,
                job=job,
                owner_id=owner_id,
                operator_plan_digest=operator_plan_digest,
                binding_id=binding_id,
                launch_token=launch_token,
                target=target,
                preview_plan=preview_plan,
                admission_identity=admission_identity,
                terminal=terminal,
                ttl_seconds=ttl_seconds,
            )
        if (
            intent.binding_id != binding_id
            or intent.launch_token != launch_token
            or intent.provider_kind != _PREVIEW_BACKEND_KIND
            or intent.admission_lease_id != admission_identity.lease_id
            or intent.admission_holder_id != admission_identity.holder_id
            or intent.admission_fencing_token != admission_identity.fencing_token
        ):
            raise RealmConflict(
                "Environment Preview cleanup authority differs from its terminal job."
            )
        projection_spec, expected_placements = _projection_spec(
            owner_id=owner_id,
            target=target,
        )
        store_id = self._resolve_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            expected_placements=expected_placements,
        )
        holder_id = _resource_holder_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
        )
        projection_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="projection",
        )
        volume_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="volume",
        )
        projection_record, volume_record, request = self._cleanup_coordinates(
            job_id=job_id,
            owner_id=owner_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
            projection_spec=projection_spec,
            projection_operation=projection_operation,
            volume_operation=volume_operation,
            store_id=store_id,
            admission_lease_id=admission_identity.lease_id,
            expected_launch_request_digest=intent.launch_request_digest,
        )
        if terminal is None:
            terminal = self._provider.stop(request)
        terminal_digest = hashlib.sha256(terminal.canonical_bytes).hexdigest()
        if (
            terminal.job_id != job_id
            or terminal.binding_id != binding_id
            or terminal.launch_token != launch_token
            or terminal.launch_request_digest != request.digest
            or terminal_digest != outcome.outcome.terminal_proof_digest
        ):
            raise RealmConflict(
                "Environment Preview cleanup terminal differs from its terminal job."
            )
        self._provider.cleanup(request)
        projection_record = self._cleanup_projection_record(
            actor_principal_id=actor_principal_id,
            record=projection_record,
            operation_id=projection_operation,
            store_id=store_id,
            spec=projection_spec,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            metadata=_projection_metadata(
                job_id=job_id,
                binding_id=binding_id,
                operator_plan_digest=operator_plan_digest,
                preview_plan_digest=preview_plan.digest,
            ),
        )
        volume_record = self._cleanup_volume_record(
            actor_principal_id=actor_principal_id,
            record=volume_record,
            operation_id=volume_operation,
            holder_id=holder_id,
            admission_identity=admission_identity,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            ttl_seconds=ttl_seconds,
        )
        provider_cleanup_digest = request_digest(
            {
                "format": "optpilot.environment-preview-provider-cleanup.v1",
                "launch_request_digest": request.digest,
                "terminal_digest": terminal_digest,
            }
        )
        projection_cleanup_digest = request_digest(
            {
                "format": "optpilot.environment-preview-projection-cleanup.v1",
                "plan_digest": projection_record.plan_digest,
                "projection_spec_digest": projection_spec.digest,
                "state": projection_record.state.value,
            }
        )
        volume_cleanup_digest = request_digest(
            {
                "format": "optpilot.environment-preview-volume-cleanup.v1",
                "policy": volume_record.portable_record(),
                "state": volume_record.state.value,
            }
        )
        return EnvironmentPreviewCleanupEvidence(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            preview_plan_digest=preview_plan.digest,
            launch_request_digest=request.digest,
            terminal_digest=terminal_digest,
            provider_cleanup_digest=provider_cleanup_digest,
            projection_cleanup_digest=projection_cleanup_digest,
            volume_cleanup_digest=volume_cleanup_digest,
        )

    def _cleanup_cancelled_before_launch(
        self,
        *,
        actor_principal_id: str,
        job: OperatorJobRecord,
        owner_id: str,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        admission_identity: _PreviewAdmissionIdentity,
        terminal: LocalContainerWebTerminal | None,
        ttl_seconds: float,
    ) -> EnvironmentPreviewCleanupEvidence:
        outcome = job.outcome
        if (
            job.state is not OperatorJobState.CANCELLED
            or outcome is None
            or outcome.outcome.status.value != "cancelled"
            or outcome.outcome.started
            or outcome.outcome.disposition.value != "never_started"
            or outcome.outcome.terminal_proof_digest is not None
        ):
            raise RealmConflict(
                "Unlaunched Environment Preview cleanup requires exact cancellation proof."
            )
        projection_spec, expected_placements = _projection_spec(
            owner_id=owner_id, target=target
        )
        store_id = self._resolve_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            expected_placements=expected_placements,
        )
        holder_id = _resource_holder_id(
            job_id=job.job_id,
            binding_id=binding_id,
            launch_token=launch_token,
        )
        projection_operation = _resource_operation_id(
            job_id=job.job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="projection",
        )
        volume_operation = _resource_operation_id(
            job_id=job.job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="volume",
        )

        projection_records = self._ledger.list_projection_realizations(
            actor_principal_id=self._projection_service.maintenance_principal_id,
            projection_root_id=self._projection_service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        matches: list[ProjectionRealizationRecord] = []
        for item in projection_records:
            try:
                _require_private_operation_resolution(
                    item,
                    realm_id=self._ledger.realm_id,
                    expected_operation_id=projection_operation,
                )
            except RealmConflict:
                continue
            matches.append(item)
        if len(matches) > 1:
            raise RealmIntegrityError(
                "Unlaunched Environment Preview projection operation is not unique."
            )
        projection_record = matches[0] if matches else None
        if projection_record is not None and (
            projection_record.state is ProjectionRealizationState.QUARANTINED
            or projection_record.projection_root_id
            != self._projection_service.root_binding.projection_root_id
            or projection_record.owner_id != owner_id
            or projection_record.store_id != store_id
            or projection_record.spec_digest != projection_spec.digest
            or thaw_json(projection_record.spec) != projection_spec.to_dict()
        ):
            raise RealmConflict(
                "Unlaunched Environment Preview projection differs from its operation."
            )

        volume_key, volume_id, usage_lease_id = _volume_operation_identity(
            volume_operation
        )
        try:
            volume_record = self._volume_service._maintenance_volume(volume_id)
        except RealmNotFound:
            volume_record = None
        if volume_record is not None and (
            volume_record.state is EphemeralVolumeState.QUARANTINED
            or volume_record.volume_root_id
            != self._volume_service.root_binding.volume_root_id
            or volume_record.owner_id != owner_id
            or volume_record.parent_lease_id != admission_identity.lease_id
            or volume_record.usage_lease_id != usage_lease_id
            or volume_record.provider_kind
            != self._volume_service.root_binding.provider_kind
            or volume_record.quota != ENVIRONMENT_PREVIEW_VOLUME_QUOTA
            or volume_record.quota_enforcement != "advisory"
            or volume_record.relative_name != f"volume-{volume_key[:48]}"
        ):
            raise RealmConflict(
                "Unlaunched Environment Preview volume differs from its operation."
            )

        projection_relative_name = (
            projection_record.relative_name
            if projection_record is not None
            else self._prospective_projection_relative_name(
                actor_principal_id=actor_principal_id,
                operation_id=projection_operation,
                store_id=store_id,
                spec=projection_spec,
            )
        )
        volume_relative_name = (
            volume_record.relative_name
            if volume_record is not None
            else f"volume-{volume_key[:48]}"
        )
        projection_root = (
            self._projection_service.root_binding.path
            / projection_relative_name
            / _EXPOSED_TREE_NAME
        )
        volume_root = (
            self._volume_service.root_binding.path
            / volume_relative_name
            / _VOLUME_DATA_NAME
            / _LAYOUT_DIRECTORY
        )
        request = _launch_request(
            job_id=job.job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            plan=preview_plan,
            app_path=_projected_app_path(projection_root, preview_plan),
            layout=_PreviewLayoutPaths(
                context=volume_root / "context.json",
                outputs_file=volume_root / "control" / "outputs.jsonl",
                workspace=volume_root / "workspace",
                artifacts=volume_root / "artifacts",
                output=volume_root / "output",
                runtime_env=volume_root / "runtime_env",
                prepared_outputs=volume_root / "prepared_outputs",
            ),
        )
        terminal = terminal or self._provider.stop(request)
        if (
            terminal.job_id != job.job_id
            or terminal.binding_id != binding_id
            or terminal.launch_token != launch_token
            or terminal.launch_request_digest != request.digest
            or terminal.started
        ):
            raise RealmIntegrityError(
                "Cancelled unlaunched Preview lacks exact never-started proof."
            )
        terminal_digest = hashlib.sha256(terminal.canonical_bytes).hexdigest()
        launch_request_digest = request.digest
        self._provider.cleanup(request)
        provider_cleanup_digest = request_digest(
            {
                "format": "optpilot.environment-preview-provider-cleanup.v1",
                "launch_request_digest": launch_request_digest,
                "terminal_digest": terminal_digest,
                "disposition": "never-started",
            }
        )

        if projection_record is None:
            projection_cleanup_digest = request_digest(
                {
                    "format": "optpilot.environment-preview-projection-cleanup.v1",
                    "projection_spec_digest": projection_spec.digest,
                    "state": "absent",
                }
            )
        else:
            projection_record = self._cleanup_projection_record(
                actor_principal_id=actor_principal_id,
                record=projection_record,
                operation_id=projection_operation,
                store_id=store_id,
                spec=projection_spec,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                metadata=_projection_metadata(
                    job_id=job.job_id,
                    binding_id=binding_id,
                    operator_plan_digest=operator_plan_digest,
                    preview_plan_digest=preview_plan.digest,
                ),
            )
            projection_cleanup_digest = request_digest(
                {
                    "format": "optpilot.environment-preview-projection-cleanup.v1",
                    "plan_digest": projection_record.plan_digest,
                    "projection_spec_digest": projection_spec.digest,
                    "state": projection_record.state.value,
                }
            )
        if volume_record is None:
            volume_cleanup_digest = request_digest(
                {
                    "format": "optpilot.environment-preview-volume-cleanup.v1",
                    "policy": "ephemeral",
                    "state": "absent",
                }
            )
        else:
            volume_record = self._cleanup_volume_record(
                actor_principal_id=actor_principal_id,
                record=volume_record,
                operation_id=volume_operation,
                holder_id=holder_id,
                admission_identity=admission_identity,
                job_id=job.job_id,
                owner_id=owner_id,
                operator_plan_digest=operator_plan_digest,
                ttl_seconds=ttl_seconds,
            )
            volume_cleanup_digest = request_digest(
                {
                    "format": "optpilot.environment-preview-volume-cleanup.v1",
                    "policy": volume_record.portable_record(),
                    "state": volume_record.state.value,
                }
            )
        return EnvironmentPreviewCleanupEvidence(
            job_id=job.job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            preview_plan_digest=preview_plan.digest,
            launch_request_digest=launch_request_digest,
            terminal_digest=terminal_digest,
            provider_cleanup_digest=provider_cleanup_digest,
            projection_cleanup_digest=projection_cleanup_digest,
            volume_cleanup_digest=volume_cleanup_digest,
        )

    def _bind(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        operator_plan_digest: str,
        binding_id: str,
        launch_token: str,
        target: ResolvedCandidateInspectionTarget,
        preview_plan: EnvironmentPreviewPlan,
        ttl_seconds: float,
        create_missing: bool,
    ) -> ManagedEnvironmentPreviewBinding:
        actor_principal_id = required_text(
            actor_principal_id, "Environment Preview actor principal id"
        )
        job_id = _path_free_identifier(job_id, "Environment Preview job id")
        owner_id = _path_free_identifier(owner_id, "Environment Preview owner id")
        binding_id = _path_free_identifier(
            binding_id, "Environment Preview binding id"
        )
        launch_token = _path_free_identifier(
            launch_token, "Environment Preview launch token"
        )
        operator_plan_digest = lower_hex_digest(
            operator_plan_digest, "Environment Preview Operator Job plan digest"
        )
        if not isinstance(admission_lease, LeaseRecord):
            raise TypeError("admission_lease must be a LeaseRecord.")
        if not isinstance(target, ResolvedCandidateInspectionTarget):
            raise TypeError("target must be a ResolvedCandidateInspectionTarget.")
        if not isinstance(preview_plan, EnvironmentPreviewPlan):
            raise TypeError("preview_plan must be an EnvironmentPreviewPlan.")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("Environment Preview resource ttl_seconds must be positive.")

        recomputed = compile_environment_preview_plan(
            target, profile_id=preview_plan.profile_id
        )
        if recomputed != preview_plan:
            raise RealmConflict(
                "Environment Preview plan differs from the retained inspection target."
            )
        _validate_retained_prepared_layers(target=target, plan=preview_plan)
        current_admission = self._validate_authority(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            admission_lease=admission_lease,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
        )
        projection_spec, expected_placements = _projection_spec(
            owner_id=owner_id,
            target=target,
        )
        store_id = self._resolve_store(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            expected_placements=expected_placements,
        )
        _validate_provider_plan(self._provider, preview_plan)
        # Construct once with provider-private dummy paths so all request-shape
        # failures happen before allocating any Realm resource.
        _launch_request(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            plan=preview_plan,
            app_path=_projected_app_path(
                Path("/__optpilot_preview_preflight"), preview_plan
            ),
            layout=_PreviewLayoutPaths(
                context=Path("/__optpilot_preview_preflight/context.json"),
                outputs_file=Path(
                    "/__optpilot_preview_preflight/control/outputs.jsonl"
                ),
                workspace=Path("/__optpilot_preview_preflight/workspace"),
                artifacts=Path("/__optpilot_preview_preflight/artifacts"),
                output=Path("/__optpilot_preview_preflight/output"),
                runtime_env=Path("/__optpilot_preview_preflight/runtime_env"),
                prepared_outputs=Path(
                    "/__optpilot_preview_preflight/prepared_outputs"
                ),
            ),
        )

        holder_id = _resource_holder_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
        )
        projection_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="projection",
        )
        volume_operation = _resource_operation_id(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            resource_kind="volume",
        )
        metadata = _projection_metadata(
            job_id=job_id,
            binding_id=binding_id,
            operator_plan_digest=operator_plan_digest,
            preview_plan_digest=preview_plan.digest,
        )
        try:
            projection = self._projection_service.recover_existing_private_read_only(
                operation_id=projection_operation,
                actor_principal_id=actor_principal_id,
                store_id=store_id,
                spec=projection_spec,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                consumer_kind="environment-preview",
                consumer_metadata=metadata,
            )
        except RealmNotFound:
            if not create_missing:
                raise
            projection = self._projection_service.project_read_only(
                operation_id=projection_operation,
                actor_principal_id=actor_principal_id,
                store_id=store_id,
                spec=projection_spec,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                consumer_kind="environment-preview",
                consumer_metadata=metadata,
                sharing_policy="private",
            )

        try:
            try:
                volume = self._volume_service.recover_existing(
                    operation_id=volume_operation,
                    actor_principal_id=actor_principal_id,
                    parent_lease=current_admission,
                    holder_id=holder_id,
                    quota=ENVIRONMENT_PREVIEW_VOLUME_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl_seconds,
                )
            except RealmNotFound:
                if not create_missing:
                    raise
                volume = self._volume_service.create(
                    operation_id=volume_operation,
                    actor_principal_id=actor_principal_id,
                    parent_lease=current_admission,
                    holder_id=holder_id,
                    quota=ENVIRONMENT_PREVIEW_VOLUME_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl_seconds,
                )
            layout = _prepare_or_validate_layout(
                volume,
                context_bytes=preview_plan.context.canonical_bytes,
                attachment_tracker=self._layout_attachments,
                attachment_key=_layout_attachment_key(volume.record),
            )
            request = _launch_request(
                job_id=job_id,
                binding_id=binding_id,
                launch_token=launch_token,
                operator_plan_digest=operator_plan_digest,
                plan=preview_plan,
                app_path=_projected_app_path(projection.root_path, preview_plan),
                layout=layout,
            )
            evidence = _binding_evidence(
                request=request,
                operator_plan_digest=operator_plan_digest,
                plan=preview_plan,
                projection_spec=projection_spec,
                projection=projection,
                volume=volume,
            )
            return ManagedEnvironmentPreviewBinding(
                provider=self._provider,
                projection_service=self._projection_service,
                projection=projection,
                volume=volume,
                request=request,
                evidence=evidence,
                preview_plan=preview_plan,
                projection_spec=projection_spec,
                layout_attachment_tracker=self._layout_attachments,
                layout_attachment_key=_layout_attachment_key(volume.record),
                authority_validator=lambda: self._validate_authority(
                    actor_principal_id=actor_principal_id,
                    job_id=job_id,
                    owner_id=owner_id,
                    admission_lease=admission_lease,
                    operator_plan_digest=operator_plan_digest,
                    preview_plan=preview_plan,
                ),
            )
        except BaseException as error:
            error.add_note(
                "Partially realized Environment Preview resources were retained "
                "for exact recovery and TTL cleanup."
            )
            raise

    def _validate_job_plan(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        preview_plan: EnvironmentPreviewPlan,
    ) -> OperatorJobRecord:
        """Re-prove the immutable job/owner derivation without a live lease."""

        owner = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if owner.owner_kind != "operator-job":
            raise RealmConflict(
                "Environment Preview resources require an operator-job owner."
            )
        job = self._ledger.read_operator_job(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
        )
        derivation = self._ledger.read_owner_derivation(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
        )
        expected_claims = {
            "cpu_millis": preview_plan.resources.cpu_millis,
            "memory_bytes": preview_plan.resources.memory_bytes,
        }
        if preview_plan.resources.gpu_count:
            expected_claims["gpu_count"] = preview_plan.resources.gpu_count
        expected_sources = tuple(
            sorted(
                {
                    preview_plan.digest,
                    preview_plan.fingerprints.source,
                    preview_plan.fingerprints.runtime,
                    preview_plan.fingerprints.candidate,
                    preview_plan.fingerprints.evaluation,
                    preview_plan.fingerprints.run_definition,
                    preview_plan.fingerprints.selection,
                }
            )
        )
        facts = thaw_json(job.plan.input_facts)
        projection_contract_digest = request_digest(
            {
                "logical_paths": preview_plan.paths.to_dict(),
                "preview_plan_digest": preview_plan.digest,
                "schema": "optpilot.environment-preview-projection-contract.v1",
                "source_fingerprint": preview_plan.fingerprints.source,
            }
        )
        if (
            job.owner_id != owner_id
            or job.plan_digest != operator_plan_digest
            or job.approval is None
            or job.state
            in {OperatorJobState.PLANNED, OperatorJobState.AWAITING_APPROVAL}
            or job.plan.job_kind != _PREVIEW_JOB_KIND
            or job.plan.target.kind != _PREVIEW_TARGET_KIND
            or job.plan.target.selection != preview_plan.selection
            or facts
            != {
                "preview_plan": preview_plan.to_dict(),
                "schema": _PREVIEW_INPUT_SCHEMA,
            }
            or job.plan.owner_derivation_manifest_digest != derivation.digest
            or job.plan.source_fingerprints != expected_sources
            or job.plan.runtime_fingerprint != preview_plan.fingerprints.runtime
            or job.plan.entrypoint_profile != preview_plan.profile_id
            or job.plan.projection_contract_digest != projection_contract_digest
            or job.plan.backend_kind != _PREVIEW_BACKEND_KIND
            or job.plan.backend_realm != _PREVIEW_BACKEND_REALM
            or dict(job.plan.resource_claims) != expected_claims
            or job.plan.timeout_seconds != preview_plan.timeout_seconds
            or job.plan.network_policy != "denied"
            or job.plan.network_enforcement != "enforced"
            or job.plan.requested_secret_names
        ):
            raise RealmConflict(
                "Environment Preview Operator Job differs from its retained plan."
            )
        return job

    def _validate_authority(
        self,
        *,
        actor_principal_id: str,
        job_id: str,
        owner_id: str,
        admission_lease: LeaseRecord,
        operator_plan_digest: str,
        preview_plan: EnvironmentPreviewPlan,
    ) -> LeaseRecord:
        self._validate_job_plan(
            actor_principal_id=actor_principal_id,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
            preview_plan=preview_plan,
        )
        current = self._ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=admission_lease.lease_id,
            holder_id=admission_lease.holder_id,
            fencing_token=admission_lease.fencing_token,
        )
        if (
            current != admission_lease
        ):
            raise RealmConflict(
                "Operator Job admission authority differs from the Preview request."
            )
        _validate_admission_identity(
            current,
            job_id=job_id,
            owner_id=owner_id,
            operator_plan_digest=operator_plan_digest,
        )
        return current

    def _cleanup_coordinates(
        self,
        *,
        job_id: str,
        owner_id: str,
        binding_id: str,
        launch_token: str,
        operator_plan_digest: str,
        preview_plan: EnvironmentPreviewPlan,
        projection_spec: ProjectionSpec,
        projection_operation: str,
        volume_operation: str,
        store_id: str,
        admission_lease_id: str,
        expected_launch_request_digest: str,
    ) -> tuple[
        ProjectionRealizationRecord,
        EphemeralVolumeRecord,
        ContainerWebLaunchRequest,
    ]:
        """Recover immutable launch coordinates even after namespaces vanish."""

        volume_key, volume_id, usage_lease_id = _volume_operation_identity(
            volume_operation
        )
        volume_record = self._volume_service._maintenance_volume(volume_id)
        if (
            volume_record.state is EphemeralVolumeState.QUARANTINED
            or volume_record.volume_root_id
            != self._volume_service.root_binding.volume_root_id
            or volume_record.owner_id != owner_id
            or volume_record.parent_lease_id != admission_lease_id
            or volume_record.usage_lease_id != usage_lease_id
            or volume_record.provider_kind
            != self._volume_service.root_binding.provider_kind
            or volume_record.quota != ENVIRONMENT_PREVIEW_VOLUME_QUOTA
            or volume_record.quota_enforcement != "advisory"
            or volume_record.relative_name != f"volume-{volume_key[:48]}"
        ):
            raise RealmConflict(
                "Environment Preview cleanup volume differs from its exact operation."
            )

        realizations = self._ledger.list_projection_realizations(
            actor_principal_id=self._projection_service.maintenance_principal_id,
            projection_root_id=(
                self._projection_service.root_binding.projection_root_id
            ),
            states=tuple(ProjectionRealizationState),
        )
        operation_realizations: list[ProjectionRealizationRecord] = []
        for item in realizations:
            try:
                _require_private_operation_resolution(
                    item,
                    realm_id=self._ledger.realm_id,
                    expected_operation_id=projection_operation,
                )
            except RealmConflict:
                continue
            operation_realizations.append(item)
        if len(operation_realizations) != 1:
            raise RealmConflict(
                "Environment Preview cleanup projection operation is not unique."
            )
        projection_record = operation_realizations[0]
        if (
            projection_record.state is ProjectionRealizationState.QUARANTINED
            or projection_record.projection_root_id
            != self._projection_service.root_binding.projection_root_id
            or projection_record.owner_id != owner_id
            or projection_record.store_id != store_id
            or projection_record.spec_digest != projection_spec.digest
            or thaw_json(projection_record.spec) != projection_spec.to_dict()
        ):
            raise RealmConflict(
                "Environment Preview cleanup projection differs from its exact operation."
            )

        projection_root = (
            self._projection_service.root_binding.path
            / projection_record.relative_name
            / _EXPOSED_TREE_NAME
        )
        volume_root = (
            self._volume_service.root_binding.path
            / volume_record.relative_name
            / _VOLUME_DATA_NAME
            / _LAYOUT_DIRECTORY
        )
        request = _launch_request(
            job_id=job_id,
            binding_id=binding_id,
            launch_token=launch_token,
            operator_plan_digest=operator_plan_digest,
            plan=preview_plan,
            app_path=_projected_app_path(projection_root, preview_plan),
            layout=_PreviewLayoutPaths(
                context=volume_root / "context.json",
                outputs_file=volume_root / "control" / "outputs.jsonl",
                workspace=volume_root / "workspace",
                artifacts=volume_root / "artifacts",
                output=volume_root / "output",
                runtime_env=volume_root / "runtime_env",
                prepared_outputs=volume_root / "prepared_outputs",
            ),
        )
        if request.digest != expected_launch_request_digest:
            raise RealmConflict(
                "Environment Preview durable resources reconstruct another launch request."
            )
        return projection_record, volume_record, request

    def _prospective_projection_relative_name(
        self,
        *,
        actor_principal_id: str,
        operation_id: str,
        store_id: str,
        spec: ProjectionSpec,
    ) -> str:
        """Derive attempt-zero placement without creating a projection row."""

        try:
            store = self._projection_service._stores[store_id]
        except KeyError as error:  # pragma: no cover - resolved immediately before
            raise RealmNotFound("Entity not found.") from error
        roots = tuple(sorted({item.snapshot_ref for item in spec.mappings}, key=str))
        private_operation_digest = request_digest(
            {
                "format": "optpilot.projection-private-operation-coordinate.v1",
                "realm_id": self._ledger.realm_id,
                "operation_id": operation_id,
            }
        )
        resolution = self._projection_service._availability_resolution(
            store,
            roots,
            private_operation_digest=private_operation_digest,
        )
        semantic_digest = self._projection_service._semantic_request_digest(
            spec, resolution
        )
        create_key = request_digest(
            {
                "format": "optpilot.projection-create-request.v1",
                "operation_id": operation_id,
                "actor_principal_id": actor_principal_id,
                "projection_root_id": (
                    self._projection_service.root_binding.projection_root_id
                ),
                "request_digest": semantic_digest,
                "creation_attempt": 0,
            }
        )
        return f"projection-{create_key[:40]}"

    def _cleanup_projection_record(
        self,
        *,
        actor_principal_id: str,
        record: ProjectionRealizationRecord,
        operation_id: str,
        store_id: str,
        spec: ProjectionSpec,
        holder_id: str,
        ttl_seconds: float,
        metadata: Mapping[str, object],
    ) -> ProjectionRealizationRecord:
        fresh = self._projection_service._maintenance_realization(
            record.realization_id
        )
        _validate_same_projection_record(record, fresh)
        if fresh.state is ProjectionRealizationState.QUARANTINED:
            raise RealmConflict(
                "Quarantined Environment Preview projection requires forensic resolution."
            )
        if fresh.state is ProjectionRealizationState.CLEANED:
            return fresh
        if fresh.state is ProjectionRealizationState.READY:
            try:
                projection = (
                    self._projection_service.recover_existing_private_read_only(
                        operation_id=operation_id,
                        actor_principal_id=actor_principal_id,
                        store_id=store_id,
                        spec=spec,
                        holder_id=holder_id,
                        ttl_seconds=ttl_seconds,
                        consumer_kind="environment-preview",
                        consumer_metadata=metadata,
                    )
                )
            except RealmIntegrityError:
                raise
            except RealmError:
                projection = None
            if projection is not None:
                if projection.realization.realization_id != fresh.realization_id:
                    projection.close()
                    raise RealmIntegrityError(
                        "Environment Preview projection recovery selected another realization."
                    )
                retired = self._projection_service.retire_private_projection(
                    projection, ttl_seconds=ttl_seconds
                )
                _validate_same_projection_record(record, retired)
                if retired.state is not ProjectionRealizationState.CLEANED:
                    raise RealmIntegrityError(
                        "Environment Preview projection retirement did not finish cleanup."
                    )
                return retired
        receipt = self._projection_service.reconcile_projection(
            operation_id=_cleanup_operation_id(
                operation_id, resource_kind="projection"
            ),
            realization_id=fresh.realization_id,
            ttl_seconds=ttl_seconds,
        )
        _validate_same_projection_record(record, receipt.realization)
        if receipt.realization.state is not ProjectionRealizationState.CLEANED:
            raise RealmIntegrityError(
                "Environment Preview projection reconciliation did not finish cleanup."
            )
        return receipt.realization

    def _cleanup_volume_record(
        self,
        *,
        actor_principal_id: str,
        record: EphemeralVolumeRecord,
        operation_id: str,
        holder_id: str,
        admission_identity: _PreviewAdmissionIdentity,
        job_id: str,
        owner_id: str,
        operator_plan_digest: str,
        ttl_seconds: float,
    ) -> EphemeralVolumeRecord:
        fresh = self._volume_service._maintenance_volume(record.volume_id)
        _validate_same_volume_record(record, fresh)
        if fresh.state is EphemeralVolumeState.QUARANTINED:
            raise RealmConflict(
                "Quarantined Environment Preview volume requires forensic resolution."
            )
        if fresh.state is EphemeralVolumeState.CLEANED:
            return fresh

        try:
            current_admission = self._ledger.validate_lease(
                actor_principal_id=actor_principal_id,
                lease_id=admission_identity.lease_id,
                holder_id=admission_identity.holder_id,
                fencing_token=admission_identity.fencing_token,
            )
            _validate_admission_identity(
                current_admission,
                job_id=job_id,
                owner_id=owner_id,
                operator_plan_digest=operator_plan_digest,
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
                    quota=ENVIRONMENT_PREVIEW_VOLUME_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl_seconds,
                )
                if volume.record.volume_id != fresh.volume_id:
                    volume._detach_without_release()
                    raise RealmIntegrityError(
                        "Environment Preview volume recovery selected another volume."
                    )
                volume.close()
            except RealmIntegrityError:
                raise
            except RealmError:
                pass
            fresh = self._volume_service._maintenance_volume(record.volume_id)
            _validate_same_volume_record(record, fresh)
            if fresh.state is EphemeralVolumeState.CLEANED:
                return fresh

        receipt = self._volume_service.reconcile_volume(
            operation_id=_cleanup_operation_id(
                operation_id, resource_kind="volume"
            ),
            volume_id=fresh.volume_id,
            ttl_seconds=ttl_seconds,
        )
        _validate_same_volume_record(record, receipt.volume)
        if receipt.volume.state is not EphemeralVolumeState.CLEANED:
            raise RealmIntegrityError(
                "Environment Preview volume reconciliation did not finish cleanup."
            )
        return receipt.volume

    def _resolve_store(
        self,
        *,
        actor_principal_id: str,
        owner_id: str,
        expected_placements: tuple[tuple[str, object], ...],
    ) -> str:
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        stores: set[str] = set()
        for role, snapshot_ref in expected_placements:
            placements = tuple(
                item
                for item in memberships
                if item.role == role and item.content_ref == snapshot_ref
            )
            if len(placements) != 1:
                raise RealmConflict(
                    "Each retained Environment Preview layer requires exactly one "
                    "job-owned store placement."
                )
            stores.add(placements[0].store_id)
        if len(stores) != 1:
            raise RealmConflict(
                "Environment Preview layers require unsupported multi-store placement."
            )
        store_id = next(iter(stores))
        if store_id not in self._projection_service.available_store_ids:
            raise RealmConflict(
                "The retained Environment Preview content store is not locally available."
            )
        return store_id


def _validate_admission_identity(
    lease: LeaseRecord,
    *,
    job_id: str,
    owner_id: str,
    operator_plan_digest: str,
) -> None:
    if (
        lease.owner_id != owner_id
        or lease.lease_kind != "operator-job-admission"
        or lease.audience != "operator-job"
        or lease.scope_key != f"operator-job-admission:{job_id}"
        or lease.parent_lease_id is not None
        or dict(lease.metadata)
        != {"job_id": job_id, "plan_digest": operator_plan_digest}
    ):
        raise RealmConflict(
            "Operator Job admission identity differs from the Preview request."
        )


def _deterministic_admission_identity(job_id: str) -> _PreviewAdmissionIdentity:
    def logical_id(prefix: str) -> str:
        digest = request_digest(
            {
                "payload": {"job_id": job_id},
                "schema": f"optpilot.{prefix}.v1",
            }
        )
        return f"{prefix}-{digest[:40]}"

    return _PreviewAdmissionIdentity(
        lease_id=logical_id("operator-job-admission"),
        holder_id=logical_id("operator-job-holder"),
        fencing_token=1,
    )


def _validate_supplied_admission(
    lease: LeaseRecord | None,
    *,
    identity: _PreviewAdmissionIdentity,
    job_id: str,
    owner_id: str,
    operator_plan_digest: str,
) -> None:
    if lease is None:
        return
    _validate_admission_identity(
        lease,
        job_id=job_id,
        owner_id=owner_id,
        operator_plan_digest=operator_plan_digest,
    )
    if (
        lease.lease_id != identity.lease_id
        or lease.holder_id != identity.holder_id
        or lease.fencing_token != identity.fencing_token
    ):
        raise RealmConflict(
            "Operator Job admission differs from its deterministic Preview identity."
        )


def _validate_retained_prepared_layers(
    *,
    target: ResolvedCandidateInspectionTarget,
    plan: EnvironmentPreviewPlan,
) -> None:
    """Fail closed when attached prepared bytes are not portable to this launch."""

    prepared = target.evaluation.closure.prepared_runtime
    if not prepared.prepared_layers:
        return
    if prepared.portability != "portable":
        raise RealmConflict(
            "Environment Preview cannot attach provider-scoped prepared runtime layers."
        )
    if prepared.platform is not None and prepared.platform != plan.runtime.platform:
        raise RealmConflict(
            "Environment Preview prepared layers target another container platform."
        )


def _projection_metadata(
    *,
    job_id: str,
    binding_id: str,
    operator_plan_digest: str,
    preview_plan_digest: str,
) -> dict[str, str]:
    return {
        "binding_id": binding_id,
        "job_id": job_id,
        "operator_plan_digest": operator_plan_digest,
        "preview_plan_digest": preview_plan_digest,
        "schema": "optpilot.environment-preview-projection-consumer.v1",
    }


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
        "plan_digest": actual.plan_digest == expected.plan_digest,
        "copied_logical_bytes": (
            actual.copied_logical_bytes == expected.copied_logical_bytes
        ),
        "copied_file_count": actual.copied_file_count == expected.copied_file_count,
    }
    changed = sorted(name for name, equal in comparisons.items() if not equal)
    if changed:
        raise RealmIntegrityError(
            "Environment Preview projection immutable identity changed during cleanup: "
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
            "Environment Preview volume immutable identity changed during cleanup."
        )


def _cleanup_operation_id(operation_id: str, *, resource_kind: str) -> str:
    digest = request_digest(
        {
            "format": "optpilot.environment-preview-terminal-cleanup.v1",
            "operation_id": operation_id,
            "resource_kind": resource_kind,
        }
    )
    return f"environment-preview.cleanup.{resource_kind}/{digest}"


def _projection_spec(
    *,
    owner_id: str,
    target: ResolvedCandidateInspectionTarget,
) -> tuple[ProjectionSpec, tuple[tuple[str, object], ...]]:
    closure = target.evaluation.closure
    candidate = getattr(target, "candidate", None)
    file_candidate = (
        candidate is not None
        and candidate.admission.envelope.candidate_format == "files"
    )
    grouped: tuple[tuple[str, tuple[ScopeLayer, ...]], ...] = (
        (
            RUN_ENVIRONMENT_SOURCE_ROLE,
            closure.environment_revision.source_layers,
        ),
        (RUN_PREPARED_RUNTIME_ROLE, closure.prepared_runtime.prepared_layers),
    )
    mappings: list[TreeMapping] = []
    placements: set[tuple[str, object]] = set()
    seen_mappings: set[tuple[object, str, str]] = set()
    environment_mapping_count = 0
    for role, layers in grouped:
        for layer in layers:
            if layer.precedence != 0:
                raise RealmConflict(
                    "Environment Preview cannot safely approximate retained layer precedence."
                )
            destination = layer.destination_subpath
            if file_candidate:
                destination = (
                    _PROJECTION_APP_PARTITION
                    if destination == "."
                    else f"{_PROJECTION_APP_PARTITION}/{destination}"
                )
            identity = (
                layer.snapshot_ref,
                destination,
                layer.source_subpath,
            )
            if identity in seen_mappings:
                raise RealmConflict(
                    "Environment Preview retained layers contain a duplicate mapping."
                )
            seen_mappings.add(identity)
            mappings.append(
                TreeMapping(
                    snapshot_ref=layer.snapshot_ref,
                    destination=destination,
                    source_subpath=layer.source_subpath,
                )
            )
            environment_mapping_count += 1
            placements.add((role, layer.snapshot_ref))
    if environment_mapping_count == 0:
        raise RealmIntegrityError(
            "Environment Preview retained environment source layers are missing."
        )
    if candidate is not None:
        envelope = candidate.admission.envelope
        if envelope.candidate_format == "files":
            if (
                len(envelope.content_refs) != 1
                or not isinstance(envelope.content_refs[0], SnapshotRef)
            ):
                raise RealmIntegrityError(
                    "Environment Preview file candidate lacks one exact tree snapshot."
                )
            candidate_snapshot = envelope.content_refs[0]
            mappings.append(
                TreeMapping(
                    snapshot_ref=candidate_snapshot,
                    destination=_PROJECTION_CANDIDATE_PARTITION,
                    source_subpath=".",
                )
            )
            placements.add((RUN_CANDIDATE_ROLE, candidate_snapshot))
    ordered_placements = tuple(
        sorted(placements, key=lambda item: (item[0], str(item[1])))
    )
    return ProjectionSpec(owner_id=owner_id, mappings=tuple(mappings)), ordered_placements


def _validate_provider_plan(
    provider: LocalContainerWebProvider,
    plan: EnvironmentPreviewPlan,
) -> None:
    if (
        plan.runtime.engine is not None
        and plan.runtime.engine != "docker"
    ):
        raise RealmConflict(
            "Environment Preview container engine differs from the approved profile."
        )
    if plan.resources.gpu_count != 0:
        raise RealmConflict(
            "The local Environment Preview provider cannot enforce GPU claims."
        )
    if (
        plan.resources.cpu_millis < _MIN_CONTAINER_CPU_MILLIS
        or plan.resources.memory_bytes < _MIN_CONTAINER_MEMORY_BYTES
    ):
        raise RealmConflict(
            "Environment Preview resources are below the authenticated container minimum."
        )
    if not provider.is_gateway_image_trusted(plan.runtime.image_ref):
        raise RealmConflict(
            "Environment Preview image is not trusted for authenticated ingress."
        )


def _projected_app_path(
    projection_root: Path,
    plan: EnvironmentPreviewPlan,
) -> Path:
    """Return the app subtree without changing legacy parameter projections."""

    return (
        projection_root / _PROJECTION_APP_PARTITION
        if plan.context.candidate_format == "files"
        else projection_root
    )


def _launch_request(
    *,
    job_id: str,
    binding_id: str,
    launch_token: str,
    operator_plan_digest: str,
    plan: EnvironmentPreviewPlan,
    app_path: Path,
    layout: _PreviewLayoutPaths,
) -> ContainerWebLaunchRequest:
    paths = plan.paths
    mounts = [
        ContainerWebMount(app_path, paths.app, "read-only"),
        ContainerWebMount(layout.context, paths.context, "read-only"),
        ContainerWebMount(layout.runtime_env, paths.runtime_env, "read-only"),
        ContainerWebMount(
            layout.prepared_outputs, paths.prepared_outputs, "read-only"
        ),
        ContainerWebMount(layout.workspace, paths.workspace, "read-write"),
        ContainerWebMount(layout.artifacts, paths.artifacts, "read-only"),
    ]
    if plan.context.candidate_format == "files":
        if (
            plan.context.candidate_root
            != ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT
        ):
            raise RealmIntegrityError(
                "Environment Preview file candidate root changed before binding."
            )
        mounts.insert(
            1,
            ContainerWebMount(
                app_path.parent / _PROJECTION_CANDIDATE_PARTITION,
                ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
                "read-only",
            ),
        )
    if plan.outputs_enabled:
        mounts.extend(
            (
                ContainerWebMount(
                    layout.outputs_file, paths.outputs_file, "read-write"
                ),
                ContainerWebMount(layout.output, paths.output_root, "read-write"),
            )
        )
    return ContainerWebLaunchRequest(
        job_id=job_id,
        binding_id=binding_id,
        launch_token=launch_token,
        portable_plan_digest=operator_plan_digest,
        image_ref=plan.runtime.image_ref,
        platform=plan.runtime.platform,
        command=plan.invocation.command,
        workdir=plan.invocation.workdir,
        environment=plan.invocation.environment,
        run_identity=ContainerWebRunIdentity(uid=os.geteuid(), gid=os.getegid()),
        mounts=tuple(mounts),
        ports=(plan.presentation.port, *plan.presentation.extra_ports),
        primary_port=plan.presentation.port,
        ready_path=plan.presentation.readiness.path,
        ready_timeout_seconds=plan.presentation.readiness.timeout_seconds,
        network_policy="denied",
        cpu_millis=plan.resources.cpu_millis,
        memory_bytes=plan.resources.memory_bytes,
        pids_limit=ENVIRONMENT_PREVIEW_PIDS_LIMIT,
        timeout_seconds=plan.timeout_seconds,
    )


def _binding_evidence(
    *,
    request: ContainerWebLaunchRequest,
    operator_plan_digest: str,
    plan: EnvironmentPreviewPlan,
    projection_spec: ProjectionSpec,
    projection: ManagedReadOnlyProjection,
    volume: ManagedEphemeralVolume,
) -> EnvironmentPreviewBindingEvidence:
    context_digest = hashlib.sha256(plan.context.canonical_bytes).hexdigest()
    resources_digest = request_digest(
        {
            "format": "optpilot.environment-preview-resources.v1",
            "projection": projection.portable_record(),
            "volume": volume.portable_record(),
        }
    )
    return EnvironmentPreviewBindingEvidence(
        job_id=request.job_id,
        binding_id=request.binding_id,
        launch_token=request.launch_token,
        operator_plan_digest=operator_plan_digest,
        preview_plan_digest=plan.digest,
        projection_spec_digest=projection_spec.digest,
        context_digest=context_digest,
        resources_digest=resources_digest,
        launch_request_digest=request.digest,
    )


def _resource_holder_id(*, job_id: str, binding_id: str, launch_token: str) -> str:
    digest = request_digest(
        {
            "binding_id": binding_id,
            "format": "optpilot.environment-preview-resource-holder.v1",
            "job_id": job_id,
            "launch_token": launch_token,
        }
    )
    return f"environment-preview-resource-{digest[:48]}"


def _resource_operation_id(
    *,
    job_id: str,
    binding_id: str,
    launch_token: str,
    resource_kind: str,
) -> str:
    digest = request_digest(
        {
            "binding_id": binding_id,
            "format": "optpilot.environment-preview-resource-operation.v1",
            "job_id": job_id,
            "launch_token": launch_token,
            "resource_kind": resource_kind,
        }
    )
    return f"environment-preview.resource/{digest}"


def _layout_attachment_key(volume: EphemeralVolumeRecord) -> str:
    """Return the path-free durable coordinate of one Preview volume layout."""

    if not isinstance(volume, EphemeralVolumeRecord):
        raise TypeError("volume must be an EphemeralVolumeRecord.")
    return request_digest(
        {
            "format": "optpilot.environment-preview-layout-attachment.v1",
            "volume_root_id": volume.volume_root_id,
            "volume_id": volume.volume_id,
            "volume_claim_nonce": volume.claim_nonce,
        }
    )


def _layout_marker(
    context_digest: str,
    control_claim_nonce: str,
) -> bytes:
    lower_hex_digest(context_digest, "Environment Preview context digest")
    lower_hex_digest(
        control_claim_nonce, "Environment Preview control claim nonce"
    )
    return (
        "optpilot.environment-preview-layout.v3\n"
        f"context-sha256:{context_digest}\n"
        f"control-claim-nonce:{control_claim_nonce}\n"
    ).encode("ascii")


def _control_claim_nonce(*, attachment_key: str, context_digest: str) -> str:
    """Bind the private layout claim to its exact volume and context."""

    return request_digest(
        {
            "format": "optpilot.environment-preview-control-claim.v1",
            "layout_attachment_key": lower_hex_digest(
                attachment_key,
                "Environment Preview layout attachment key",
            ),
            "context_digest": lower_hex_digest(
                context_digest,
                "Environment Preview context digest",
            ),
        }
    )


def _parse_layout_marker(
    payload: bytes,
    *,
    context_digest: str,
) -> str:
    try:
        text = payload.decode("ascii")
        lines = text.splitlines()
        if len(lines) != 3 or lines[0] != "optpilot.environment-preview-layout.v3":
            raise ValueError("unsupported schema")
        values: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or name in values:
                raise ValueError("invalid field")
            values[name] = value
        if values.pop("context-sha256", None) != context_digest:
            raise ValueError("context digest changed")
        control_claim_nonce = lower_hex_digest(
            values.pop("control-claim-nonce", None),
            "Environment Preview control claim nonce",
        )
        if values or _layout_marker(context_digest, control_claim_nonce) != payload:
            raise ValueError("marker is not canonical")
        return control_claim_nonce
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise RealmIntegrityError(
            f"Environment Preview layout marker is invalid: {error}"
        ) from error


def _validate_existing_layout(
    root: Path,
    *,
    context_bytes: bytes,
    attachment_tracker: _PreviewLayoutAttachmentTracker,
    attachment_key: str,
) -> _PreviewLayoutPaths:
    """Validate one already-published layout without repairing or renewing it."""

    if not isinstance(context_bytes, bytes) or not context_bytes:
        raise ValueError("Environment Preview context bytes must be nonempty.")
    if not isinstance(attachment_tracker, _PreviewLayoutAttachmentTracker):
        raise TypeError(
            "attachment_tracker must be a _PreviewLayoutAttachmentTracker."
        )
    lower_hex_digest(attachment_key, "Environment Preview layout attachment key")
    root = Path(root)
    root_fd: int | None = None
    locked = False
    context_digest = hashlib.sha256(context_bytes).hexdigest()
    expected_control_claim_nonce = _control_claim_nonce(
        attachment_key=attachment_key,
        context_digest=context_digest,
    )
    try:
        root_fd = os.open(root, _DIRECTORY_OPEN_FLAGS)
        fcntl.flock(root_fd, fcntl.LOCK_SH)
        locked = True
        if set(os.listdir(root_fd)) != {_LAYOUT_DIRECTORY, _LAYOUT_MARKER}:
            raise RealmIntegrityError(
                "Environment Preview retained volume layout is incomplete."
            )
        marker_bytes = _read_bounded_regular_file(
            root_fd,
            _LAYOUT_MARKER,
            label="Environment Preview layout marker",
            require_read_only=True,
        )
        control_claim_nonce = _parse_layout_marker(
            marker_bytes,
            context_digest=context_digest,
        )
        if control_claim_nonce != expected_control_claim_nonce:
            raise RealmIntegrityError(
                "Environment Preview layout marker belongs to another volume claim."
            )
        control_identity = _validate_layout_directory(
            root_fd,
            context_bytes,
        )
        if (
            set(os.listdir(root_fd)) != {_LAYOUT_DIRECTORY, _LAYOUT_MARKER}
            or _read_bounded_regular_file(
                root_fd,
                _LAYOUT_MARKER,
                label="Environment Preview layout marker",
                require_read_only=True,
            )
            != marker_bytes
        ):
            raise RealmIntegrityError(
                "Environment Preview retained volume layout changed during validation."
            )
        _validate_layout_directory(
            root_fd,
            context_bytes,
            expected_control_identity=control_identity,
        )
        attachment_tracker.observe(
            attachment_key=attachment_key,
            control_claim_nonce=control_claim_nonce,
            identity=control_identity,
        )
    except RealmIntegrityError:
        raise
    except OSError as error:
        raise RealmIntegrityError(
            f"Environment Preview retained volume layout is unavailable: {error}"
        ) from error
    finally:
        if root_fd is not None:
            try:
                if locked:
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
            finally:
                os.close(root_fd)

    base = root / _LAYOUT_DIRECTORY
    return _PreviewLayoutPaths(
        context=base / "context.json",
        outputs_file=base / "control" / "outputs.jsonl",
        workspace=base / "workspace",
        artifacts=base / "artifacts",
        output=base / "output",
        runtime_env=base / "runtime_env",
        prepared_outputs=base / "prepared_outputs",
    )


def _prepare_or_validate_layout(
    volume: ManagedEphemeralVolume,
    *,
    context_bytes: bytes,
    attachment_tracker: _PreviewLayoutAttachmentTracker,
    attachment_key: str,
) -> _PreviewLayoutPaths:
    if not isinstance(context_bytes, bytes) or not context_bytes:
        raise ValueError("Environment Preview context bytes must be nonempty.")
    if not isinstance(attachment_tracker, _PreviewLayoutAttachmentTracker):
        raise TypeError(
            "attachment_tracker must be a _PreviewLayoutAttachmentTracker."
        )
    lower_hex_digest(attachment_key, "Environment Preview layout attachment key")
    volume.validate()
    root = volume.path
    root_fd = os.open(root, _DIRECTORY_OPEN_FLAGS)
    context_digest = hashlib.sha256(context_bytes).hexdigest()
    expected_control_claim_nonce = _control_claim_nonce(
        attachment_key=attachment_key,
        context_digest=context_digest,
    )
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        names = set(os.listdir(root_fd))
        marker_present = _LAYOUT_MARKER in names
        layout_present = _LAYOUT_DIRECTORY in names
        allowed_partial = {
            _LAYOUT_DIRECTORY,
            _LAYOUT_MARKER,
            _LAYOUT_MARKER_TEMP,
            _LAYOUT_STAGING,
        }
        if not names <= allowed_partial:
            raise RealmIntegrityError(
                "Environment Preview volume contains an unexpected root entry."
            )
        if marker_present and not layout_present:
            raise RealmIntegrityError(
                "Environment Preview completed layout is missing."
            )
        if marker_present:
            if _LAYOUT_STAGING in names or _LAYOUT_MARKER_TEMP in names:
                raise RealmIntegrityError(
                    "Environment Preview completed layout contains initialization debris."
                )
            marker_bytes = _read_bounded_regular_file(
                root_fd,
                _LAYOUT_MARKER,
                label="Environment Preview layout marker",
                require_read_only=True,
            )
            control_claim_nonce = _parse_layout_marker(
                marker_bytes,
                context_digest=context_digest,
            )
            if control_claim_nonce != expected_control_claim_nonce:
                raise RealmIntegrityError(
                    "Environment Preview layout marker belongs to another volume claim."
                )
            control_identity = _validate_layout_directory(
                root_fd,
                context_bytes,
            )
        else:
            if layout_present:
                control_identity = _validate_layout_directory(
                    root_fd,
                    context_bytes,
                )
            else:
                if _LAYOUT_MARKER_TEMP in names:
                    _remove_regular_file(root_fd, _LAYOUT_MARKER_TEMP)
                if _LAYOUT_STAGING in names:
                    _remove_tree(root_fd, _LAYOUT_STAGING)
                _create_layout(root_fd, context_bytes)
                control_identity = _validate_layout_directory(
                    root_fd,
                    context_bytes,
                )
            control_claim_nonce = expected_control_claim_nonce
            _create_marker(
                root_fd,
                _layout_marker(context_digest, control_claim_nonce),
            )
        _validate_layout_directory(
            root_fd,
            context_bytes,
            expected_control_identity=control_identity,
        )
        final_names = set(os.listdir(root_fd))
        if final_names != {_LAYOUT_DIRECTORY, _LAYOUT_MARKER}:
            raise RealmIntegrityError(
                "Environment Preview volume layout is not canonical."
            )
        attachment_tracker.observe(
            attachment_key=attachment_key,
            control_claim_nonce=control_claim_nonce,
            identity=control_identity,
            replace_missing_claim=not marker_present,
        )
        _fsync(root_fd)
    except OSError as error:
        raise RealmIntegrityError(
            f"Environment Preview volume layout is unavailable: {error}"
        ) from error
    finally:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        finally:
            os.close(root_fd)
    volume.validate()
    base = root / _LAYOUT_DIRECTORY
    return _PreviewLayoutPaths(
        context=base / "context.json",
        outputs_file=base / "control" / "outputs.jsonl",
        workspace=base / "workspace",
        artifacts=base / "artifacts",
        output=base / "output",
        runtime_env=base / "runtime_env",
        prepared_outputs=base / "prepared_outputs",
    )


def _create_layout(root_fd: int, context_bytes: bytes) -> None:
    os.mkdir(_LAYOUT_STAGING, mode=0o700, dir_fd=root_fd)
    staging_fd = os.open(_LAYOUT_STAGING, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
    try:
        for name in _LAYOUT_DIRECTORIES:
            os.mkdir(name, mode=0o700, dir_fd=staging_fd)
        control_fd = os.open("control", _DIRECTORY_OPEN_FLAGS, dir_fd=staging_fd)
        try:
            outputs_fd = os.open(
                "outputs.jsonl",
                _FILE_CREATE_FLAGS,
                0o600,
                dir_fd=control_fd,
            )
            try:
                os.fchmod(outputs_fd, 0o600)
                _fsync(outputs_fd)
            finally:
                os.close(outputs_fd)
            _fsync(control_fd)
        finally:
            os.close(control_fd)
        context_fd = os.open(
            "context.json",
            _FILE_CREATE_FLAGS,
            0o400,
            dir_fd=staging_fd,
        )
        try:
            _write_all(context_fd, context_bytes)
            os.fchmod(context_fd, 0o400)
            _fsync(context_fd)
        finally:
            os.close(context_fd)
        _fsync(staging_fd)
    finally:
        os.close(staging_fd)
    os.rename(
        _LAYOUT_STAGING,
        _LAYOUT_DIRECTORY,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    _fsync(root_fd)


def _create_marker(root_fd: int, marker_bytes: bytes) -> None:
    names = set(os.listdir(root_fd))
    if _LAYOUT_MARKER_TEMP in names:
        _remove_regular_file(root_fd, _LAYOUT_MARKER_TEMP)
    marker_fd = os.open(
        _LAYOUT_MARKER_TEMP,
        _FILE_CREATE_FLAGS,
        0o400,
        dir_fd=root_fd,
    )
    try:
        _write_all(marker_fd, marker_bytes)
        os.fchmod(marker_fd, 0o400)
        _fsync(marker_fd)
    finally:
        os.close(marker_fd)
    os.rename(
        _LAYOUT_MARKER_TEMP,
        _LAYOUT_MARKER,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    _fsync(root_fd)


def _validate_layout_directory(
    root_fd: int,
    context_bytes: bytes,
    *,
    expected_control_identity: _PreviewControlIdentity | None = None,
) -> _PreviewControlIdentity:
    layout_fd = os.open(_LAYOUT_DIRECTORY, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
    try:
        expected = {"context.json", *_LAYOUT_DIRECTORIES}
        if set(os.listdir(layout_fd)) != expected:
            raise RealmIntegrityError(
                "Environment Preview volume contains an unexpected layout entry."
            )
        _validate_regular_file(
            layout_fd,
            "context.json",
            context_bytes,
            label="Environment Preview context",
            require_read_only=True,
        )
        control_identity = _validate_control_layout(
            layout_fd,
            expected=expected_control_identity,
        )
        for name in _LAYOUT_DIRECTORIES:
            if name == "control":
                continue
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=layout_fd)
            try:
                if name in {"artifacts", "prepared_outputs", "runtime_env"} and os.listdir(
                    child_fd
                ):
                    raise RealmIntegrityError(
                        f"Environment Preview read-only {name} directory was modified."
                    )
            finally:
                os.close(child_fd)
        return control_identity
    finally:
        os.close(layout_fd)


def _validate_control_layout(
    layout_fd: int,
    *,
    expected: _PreviewControlIdentity | None,
) -> _PreviewControlIdentity:
    directory_info = os.stat("control", dir_fd=layout_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_IMODE(directory_info.st_mode) != 0o700
    ):
        raise RealmIntegrityError(
            "Environment Preview output control directory metadata was modified."
        )
    control_fd = os.open("control", _DIRECTORY_OPEN_FLAGS, dir_fd=layout_fd)
    try:
        opened_directory = os.fstat(control_fd)
        if (
            (opened_directory.st_dev, opened_directory.st_ino)
            != (directory_info.st_dev, directory_info.st_ino)
            or set(os.listdir(control_fd)) != {"outputs.jsonl"}
        ):
            raise RealmIntegrityError(
                "Environment Preview output control directory was replaced."
            )
        file_info = os.stat(
            "outputs.jsonl",
            dir_fd=control_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(file_info.st_mode)
            or file_info.st_nlink != 1
            or stat.S_IMODE(file_info.st_mode) != 0o600
        ):
            raise RealmIntegrityError(
                "Environment Preview output control file metadata was modified."
            )
        file_fd = os.open("outputs.jsonl", _FILE_READ_FLAGS, dir_fd=control_fd)
        try:
            opened_file = os.fstat(file_fd)
            if (
                (opened_file.st_dev, opened_file.st_ino)
                != (file_info.st_dev, file_info.st_ino)
            ):
                raise RealmIntegrityError(
                    "Environment Preview output control file was replaced."
                )
        finally:
            os.close(file_fd)
        observed = _PreviewControlIdentity(
            directory_device=directory_info.st_dev,
            directory_inode=directory_info.st_ino,
            file_device=file_info.st_dev,
            file_inode=file_info.st_ino,
        )
        if expected is not None and observed != expected:
            raise RealmIntegrityError(
                "Environment Preview output control identity was replaced."
            )
        return observed
    finally:
        os.close(control_fd)


def _read_bounded_regular_file(
    parent_fd: int,
    name: str,
    *,
    label: str,
    require_read_only: bool,
    max_bytes: int = 4096,
) -> bytes:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > max_bytes
        or (require_read_only and info.st_mode & 0o222)
    ):
        raise RealmIntegrityError(f"{label} metadata was modified.")
    fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RealmIntegrityError(f"{label} was replaced.")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if len(payload) > max_bytes or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RealmIntegrityError(f"{label} changed while it was read.")
        return payload
    finally:
        os.close(fd)


def _validate_regular_file(
    parent_fd: int,
    name: str,
    expected: bytes,
    *,
    label: str,
    require_read_only: bool,
) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size != len(expected)
        or (require_read_only and info.st_mode & 0o222)
    ):
        raise RealmIntegrityError(f"{label} metadata was modified.")
    fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    try:
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != expected:
            raise RealmIntegrityError(f"{label} bytes were modified.")
    finally:
        os.close(fd)


def _remove_regular_file(parent_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RealmIntegrityError(
            "Environment Preview initialization file has an unsupported type."
        )
    os.unlink(name, dir_fd=parent_fd)


def _remove_tree(parent_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise RealmIntegrityError(
            "Environment Preview initialization tree has an unsupported type."
        )
    child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(child_fd):
            child_info = os.stat(child, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(child_info.st_mode):
                _remove_tree(child_fd, child)
            elif stat.S_ISREG(child_info.st_mode):
                os.unlink(child, dir_fd=child_fd)
            else:
                raise RealmIntegrityError(
                    "Environment Preview initialization tree was tampered with."
                )
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:  # pragma: no cover - defensive OS contract
            raise OSError("short write while creating Environment Preview layout")
        written += count


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


__all__ = [
    "ENVIRONMENT_PREVIEW_BINDING_EVIDENCE_SCHEMA",
    "ENVIRONMENT_PREVIEW_PIDS_LIMIT",
    "ENVIRONMENT_PREVIEW_RESOURCE_TTL_SECONDS",
    "ENVIRONMENT_PREVIEW_VOLUME_QUOTA",
    "EnvironmentPreviewBindingEvidence",
    "EnvironmentPreviewOutputCaptureDescriptor",
    "ManagedEnvironmentPreviewBinding",
    "RealmEnvironmentPreviewBinder",
]
