"""Provider integration for one retained Python batch-method worker.

This module is deliberately narrower than a study driver.  It realizes the
method-side immutable projection and writable control volume selected by one
current run-controller term, supervises exactly one long-lived socket worker,
and exposes replay-safe protocol calls.  Proposal order, normalization,
checkpointing, and recovery policy remain Realm-driver responsibilities.

All filesystem paths in this module are provider-private operational facts.
The public identity, heartbeat receipt, and exceptions contain only bounded
semantic or hashed coordinates.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
import dataclasses
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from .method_launch_environment import (
    MethodLaunchEnvironment,
    MethodLaunchEnvironmentDescriptor,
    MethodLaunchEnvironmentError,
    method_environment_names,
)
from .run_terminal_policy import method_feedback_obligations_resolved
from .realm.ephemeral_volume_records import EphemeralVolumeState
from .realm.ephemeral_volume_service import (
    ManagedEphemeralVolume,
    _volume_operation_identity,
)
from .realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from .realm.content import AllowedTreeSource
from .realm.filesystem_quota import FilesystemQuota
from .realm.leases import LeaseRecord, LeaseState
from .realm.local_process_supervisor import (
    LocalProcessSupervisor,
    ProcessLaunchRealizationClaim,
    ProcessLaunchPrivateEnvironment,
    ProcessLaunchRequest,
    ProcessLaunchReservation,
    ProcessLaunchSealReceipt,
    ProcessLaunchUnauthenticatable,
    SupervisedProcess,
    WorkerStarted,
    WorkerTerminalProof,
)
from .realm.local_runtime import LocalRealmRuntime
from .realm.owners import OwnerMembership, OwnerPermission
from .realm.projection import ProjectionSpec, TreeMapping
from .realm.projection_records import ProjectionRealizationState
from .realm.projection_service import (
    ManagedReadOnlyProjection,
    _PRIVATE_OPERATION_COORDINATE_FORMAT,
)
from .realm.refs import canonical_json_bytes, request_digest
from .realm.run_closure import ScopeLayer, ScopePath
from .realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
    RunDefinitionManifest,
)
from .realm.run_snapshot import RunLedgerSnapshot
from .retained_batch_worker import (
    BATCH_PROTOCOL,
    BATCH_REQUEST_SCHEMA,
    BATCH_RESPONSE_SCHEMA,
    INITIAL_BATCH_EXCHANGE_CHAIN,
    MAX_BATCH_DURABLE_RESPONSE_BYTES,
    MAX_UNIX_SOCKET_PATH_BYTES,
    RetainedBatchWorkerInit,
    retained_batch_exchange_chain_digest,
    unix_batch_worker_request,
)
from .retained_file_candidates import (
    CANDIDATE_STAGING_QUOTA,
    FileCandidateDraft,
    FileCandidateStagingBinding,
    candidate_staging_exchange_coordinate,
    durable_worker_response_digest,
    file_candidate_draft_token,
)
from .retained_study_compiler import (
    METHOD_CONTEXT_SCOPE,
    package_projection_contract_features,
)


_RUNTIME_ID_FORMAT = "optpilot.retained-batch-runtime-identity.v1"
_PROJECTION_FORMAT = "optpilot.retained-batch-method-projection.v1"
_EVIDENCE_FORMAT = "optpilot.retained-batch-worker-evidence.v1"
_SOCKET_NAMESPACE_FORMAT = "optpilot.retained-batch-socket-namespace.v1"
_SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+@~-]*\Z")
_SAFE_ERROR_CODE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_MAX_EXCHANGE_ID_BYTES = 512
_CONTROL_QUOTA = FilesystemQuota(
    max_entries=8,
    max_file_bytes=64 * 1024 * 1024,
    max_total_bytes=128 * 1024 * 1024,
)
_INIT_FILE = "worker-init.json"
_DIAGNOSTIC_FILE = "worker-diagnostics.jsonl"
_CONTROL_ENDPOINT_NAME = "retained-batch-control"
_MINIMAL_ENV = MappingProxyType(
    {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
)
_TERMINAL_CLEANUP_TTL_SECONDS = 300.0
_CLEANUP_DIAGNOSTIC_REPEAT_SECONDS = 30.0
_MAX_CLEANUP_DIAGNOSTIC_KEYS = 256
_CLEANUP_DIAGNOSTIC_LOCK = threading.Lock()
_CLEANUP_DIAGNOSTICS: dict[
    tuple[str, int, str, str, str], tuple[float, int]
] = {}

_PUBLIC_MESSAGES = {
    "invalid_request": "The retained batch runtime request is invalid.",
    "snapshot_changed": "The retained batch runtime controller term changed.",
    "definition_unsupported": "The retained batch runtime definition is unsupported.",
    "content_unavailable": "The retained batch runtime content is unavailable.",
    "prior_generation_cleanup_failed": (
        "The prior retained batch worker could not be retired safely."
    ),
    "resource_realization_failed": (
        "The retained batch runtime resources could not be realized."
    ),
    "worker_start_failed": "The retained batch worker could not be started safely.",
    "worker_terminal": "The retained batch worker is terminal.",
    "worker_request_timeout": "The retained batch worker request timed out.",
    "worker_unavailable": "The retained batch worker is unavailable.",
    "worker_protocol_error": "The retained batch worker response is invalid.",
    "heartbeat_failed": "The retained batch runtime heartbeat failed.",
    "runtime_closed": "The retained batch runtime is closed.",
    "cleanup_failed": "The retained batch runtime cleanup did not complete.",
    "container_engine_unavailable": (
        "No container program is available to run this method's image."
    ),
    "container_image_unavailable": (
        "The method's image is absent, does not match its fingerprint, or was "
        "built for a different architecture."
    ),
    "container_untrusted": (
        "The method's image has not been approved for study execution."
    ),
    "container_environment_value_unsupported": (
        "A declared environment value cannot be carried into a container."
    ),
    "container_worker_unrecoverable": (
        "A method container from this controller generation already exists; "
        "launch again under a new generation."
    ),
}


class _LocalRuntimeGraph(Protocol):
    actor_principal_id: str
    ledger: Any
    content_store: Any
    projection_service: Any
    volume_service: Any
    process_supervisor: LocalProcessSupervisor


class RetainedBatchRuntimeError(RuntimeError):
    """Bounded, path-free provider/runtime failure."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        if code not in _PUBLIC_MESSAGES:
            raise ValueError("retained batch runtime error code is unsupported.")
        self.code = code
        message = _PUBLIC_MESSAGES[code]
        # Bounded detail only -- an image reference and the command that
        # clears it, never a host path. A refusal the person cannot act on is
        # barely better than a silent one.
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)


def _report_cleanup_diagnostic(
    *,
    run_id: str,
    generation: int,
    phase: str,
    error: BaseException,
) -> None:
    """Write one bounded provider-local diagnostic before the public redaction.

    The caller still receives only :class:`RetainedBatchRuntimeError`.  This
    line is for the local Studio operator and is deliberately bounded so a
    malformed provider exception cannot flood the service terminal.
    """

    message = " ".join(str(error).splitlines())
    if len(message) > 1000:
        message = message[:997] + "..."
    key = (run_id, generation, phase, type(error).__name__, message)
    now = time.monotonic()
    suppressed = 0
    with _CLEANUP_DIAGNOSTIC_LOCK:
        previous = _CLEANUP_DIAGNOSTICS.get(key)
        if (
            previous is not None
            and now - previous[0] < _CLEANUP_DIAGNOSTIC_REPEAT_SECONDS
        ):
            _CLEANUP_DIAGNOSTICS[key] = (previous[0], previous[1] + 1)
            return
        if previous is not None:
            suppressed = previous[1]
            _CLEANUP_DIAGNOSTICS.pop(key)
        _CLEANUP_DIAGNOSTICS[key] = (now, 0)
        while len(_CLEANUP_DIAGNOSTICS) > _MAX_CLEANUP_DIAGNOSTIC_KEYS:
            oldest = next(iter(_CLEANUP_DIAGNOSTICS))
            _CLEANUP_DIAGNOSTICS.pop(oldest)
    repeat_note = (
        f" ({suppressed} identical repeats suppressed)"
        if suppressed
        else ""
    )
    print(
        "OptPilot retained-worker cleanup failed "
        f"for {run_id} generation {generation} during {phase}: "
        f"{type(error).__name__}: {message}{repeat_note}",
        file=sys.stderr,
        flush=True,
    )


class RetainedBatchMethodError(RuntimeError):
    """One replayed method operation returned a canonical error response."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        diagnostic_id: str | None,
        response_digest: str,
        cause: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(code, str) or _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("retained batch method error code is invalid.")
        if diagnostic_id is not None and (
            not isinstance(diagnostic_id, str)
            or len(diagnostic_id) != 32
            or any(character not in "0123456789abcdef" for character in diagnostic_id)
        ):
            raise ValueError("retained batch method diagnostic id is invalid.")
        selected_message = _required_text(
            message, "retained batch method error message", max_bytes=256
        )
        selected_digest = _lower_digest(
            response_digest, "retained batch method response digest"
        )
        self.code = code
        self.message = selected_message
        self.diagnostic_id = diagnostic_id
        self.response_digest = selected_digest
        # The worker's public response deliberately says only "the retained
        # method operation failed", so that its digest stays reproducible.
        # The real cause lives in the worker's private diagnostic log on a
        # volume that is erased seconds later, which is why it is read back
        # here rather than left to be looked up by id afterwards.
        self.cause = dict(cause) if isinstance(cause, Mapping) else None
        super().__init__(f"Retained batch method operation failed ({code}).")


class RetainedBatchProtocolError(RuntimeError):
    """A bounded canonical response cannot satisfy the typed protocol contract."""

    def __init__(
        self,
        *,
        exchange_id: str,
        operation: str,
        exchange_sequence: int | None,
        request_digest: str | None,
        response_digest: str,
    ) -> None:
        selected_exchange = _exchange_id(exchange_id)
        if operation not in {"propose", "observe", "ack", "status", "shutdown"}:
            raise ValueError("retained batch protocol operation is unsupported.")
        state_changing = operation in {"propose", "observe"}
        if state_changing:
            selected_sequence = _positive_int(
                exchange_sequence, "protocol error exchange sequence"
            )
            selected_request_digest = _lower_digest(
                request_digest, "protocol error request digest"
            )
        else:
            if exchange_sequence is not None or request_digest is not None:
                raise ValueError(
                    "non-state-changing protocol errors have no request coordinate."
                )
            selected_sequence = None
            selected_request_digest = None
        self.code = "worker_protocol_error"
        self.exchange_id = selected_exchange
        self.operation = operation
        self.exchange_sequence = selected_sequence
        self.request_digest = selected_request_digest
        self.response_digest = _lower_digest(
            response_digest, "protocol error response digest"
        )
        super().__init__(
            "The retained batch worker returned a canonical protocol-invalid response."
        )


@dataclass(frozen=True)
class RetainedBatchRuntimeIdentity:
    """Path-free identity of the worker selected by one controller term."""

    run_id: str
    controller_generation: int
    launch_token: str
    binding_id: str
    run_definition_digest: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _positive_int(self.controller_generation, "controller_generation")
        _safe_token(self.launch_token, "launch_token")
        _safe_token(self.binding_id, "binding_id")
        _lower_digest(self.run_definition_digest, "run_definition_digest")


@dataclass(frozen=True)
class RetainedBatchRuntimeHeartbeat:
    """Newest path-free child authorities renewed by one heartbeat call."""

    projection_lease_id: str
    projection_expires_at: float
    volume_lease_id: str
    volume_expires_at: float

    def __post_init__(self) -> None:
        _required_text(self.projection_lease_id, "projection_lease_id")
        _required_text(self.volume_lease_id, "volume_lease_id")
        _finite(self.projection_expires_at, "projection_expires_at")
        _finite(self.volume_expires_at, "volume_expires_at")


@dataclass(frozen=True)
class RetainedBatchInactiveReconciliation:
    """Path-free proof that one inactive method generation was reconciled."""

    run_id: str
    controller_generation: int
    run_definition_digest: str
    worker_disposition: Literal[
        "absent",
        "already_retired",
        "already_terminal",
        "never_started",
        "stopped",
        "quarantined",
    ]
    resources_reconciled: bool

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _positive_int(self.controller_generation, "controller_generation")
        _lower_digest(self.run_definition_digest, "run_definition_digest")
        if self.worker_disposition not in {
            "absent",
            "already_retired",
            "already_terminal",
            "never_started",
            "stopped",
            "quarantined",
        }:
            raise ValueError("worker_disposition is unsupported.")
        if self.resources_reconciled is not True:
            raise ValueError("inactive method receipt must be reconciled.")


@dataclass(frozen=True)
class RetainedBatchWorkerResponse:
    """One complete canonical worker success response and its exact digest."""

    response: Mapping[str, Any]
    response_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.response, Mapping):
            raise TypeError("response must be a mapping.")
        copied = _json_copy(_deep_thaw_json(self.response), "worker response")
        digest = _lower_digest(self.response_digest, "worker response digest")
        if durable_worker_response_digest(copied) != digest:
            raise ValueError("worker response digest does not match its response.")
        object.__setattr__(self, "response", _deep_freeze_json(copied))
        object.__setattr__(self, "response_digest", digest)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached mutable JSON copy of the exact worker response."""

        return _deep_thaw_json(self.response)


@dataclass(frozen=True)
class RetainedBatchProposal:
    """Unnormalized candidate payloads returned by the retained method."""

    exchange_id: str
    exchange_sequence: int
    candidates: tuple[Mapping[str, Any], ...]
    request_digest: str
    response_digest: str

    def __post_init__(self) -> None:
        _exchange_id(self.exchange_id)
        _positive_int(self.exchange_sequence, "proposal exchange sequence")
        _lower_digest(self.request_digest, "proposal request digest")
        _lower_digest(self.response_digest, "proposal response digest")
        frozen: list[Mapping[str, Any]] = []
        for candidate in self.candidates:
            if not isinstance(candidate, Mapping):
                raise TypeError("candidates must contain mappings.")
            copied = _json_copy(_deep_thaw_json(candidate), "candidate")
            frozen.append(_deep_freeze_json(copied))
        object.__setattr__(self, "candidates", tuple(frozen))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached mutable JSON representation of this receipt."""

        return {
            "candidates": [_deep_thaw_json(value) for value in self.candidates],
            "exchange_id": self.exchange_id,
            "exchange_sequence": self.exchange_sequence,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
        }


@dataclass(frozen=True)
class RetainedBatchObservationAck:
    exchange_id: str
    exchange_sequence: int
    observation_count: int
    request_digest: str
    response_digest: str

    def __post_init__(self) -> None:
        _exchange_id(self.exchange_id)
        _positive_int(self.exchange_sequence, "observation exchange sequence")
        _nonnegative_int(self.observation_count, "observation_count")
        _lower_digest(self.request_digest, "observation request digest")
        _lower_digest(self.response_digest, "observation response digest")


@dataclass(frozen=True)
class RetainedBatchExchangeCoordinate:
    exchange_id: str
    exchange_sequence: int
    request_digest: str
    response_digest: str

    def __post_init__(self) -> None:
        _exchange_id(self.exchange_id)
        _positive_int(self.exchange_sequence, "exchange sequence")
        _lower_digest(self.request_digest, "exchange request digest")
        _lower_digest(self.response_digest, "exchange response digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange_id": self.exchange_id,
            "exchange_sequence": self.exchange_sequence,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
        }


@dataclass(frozen=True)
class RetainedBatchCacheAck:
    request_exchange_id: str
    acknowledged_sequence: int
    acknowledged_chain: str
    acknowledged_exchange: RetainedBatchExchangeCoordinate
    response_digest: str

    def __post_init__(self) -> None:
        _exchange_id(self.request_exchange_id)
        _positive_int(self.acknowledged_sequence, "acknowledged sequence")
        _lower_digest(self.acknowledged_chain, "acknowledged chain")
        if not isinstance(
            self.acknowledged_exchange, RetainedBatchExchangeCoordinate
        ):
            raise TypeError(
                "acknowledged_exchange must be a RetainedBatchExchangeCoordinate."
            )
        if (
            self.acknowledged_sequence
            != self.acknowledged_exchange.exchange_sequence
        ):
            raise ValueError("acknowledged sequence differs from its exchange.")
        _lower_digest(self.response_digest, "ack response digest")


@dataclass(frozen=True)
class RetainedBatchWorkerStatus:
    request_exchange_id: str
    acknowledged_sequence: int
    acknowledged_chain: str
    pending_exchange: RetainedBatchExchangeCoordinate | None
    pending_response_bytes: int
    response_digest: str

    def __post_init__(self) -> None:
        _exchange_id(self.request_exchange_id)
        _nonnegative_int(self.acknowledged_sequence, "acknowledged sequence")
        _lower_digest(self.acknowledged_chain, "acknowledged chain")
        if self.acknowledged_sequence == 0 and (
            self.acknowledged_chain != INITIAL_BATCH_EXCHANGE_CHAIN
        ):
            raise ValueError("initial acknowledged chain differs.")
        if self.pending_exchange is not None:
            if not isinstance(
                self.pending_exchange, RetainedBatchExchangeCoordinate
            ):
                raise TypeError(
                    "pending_exchange must be a RetainedBatchExchangeCoordinate or None."
                )
            if (
                self.pending_exchange.exchange_sequence
                != self.acknowledged_sequence + 1
            ):
                raise ValueError("pending exchange is not the next sequence.")
        _nonnegative_int(self.pending_response_bytes, "pending response bytes")
        if (
            (self.pending_exchange is None) != (self.pending_response_bytes == 0)
            or self.pending_response_bytes > MAX_BATCH_DURABLE_RESPONSE_BYTES
        ):
            raise ValueError("pending response size differs from pending state.")
        _lower_digest(self.response_digest, "status response digest")


@dataclass(frozen=True)
class _ProjectionBinding:
    store_id: str
    spec: ProjectionSpec
    scope_roots: Mapping[str, str]


@dataclass(frozen=True)
class _SocketIdentity:
    device_id: int
    inode: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.device_id, "socket device id")
        _positive_int(self.inode, "socket inode")


class RetainedBatchRuntimeProvider:
    """Realize retained workers using one existing :class:`LocalRealmRuntime`.

    The provider holds references to the existing ledger, store, projection,
    volume, and supervisor services.  It never opens another authority or
    provider root.  ``socket_parent`` is a provider-private placement knob used
    only because AF_UNIX path lengths are substantially shorter than normal
    Realm projection paths.
    """

    def __init__(
        self,
        runtime: LocalRealmRuntime | _LocalRuntimeGraph,
        *,
        method_environment: (
            MethodLaunchEnvironment
            | MethodLaunchEnvironmentDescriptor
            | None
        ) = None,
        socket_parent: Path = Path("/tmp"),
        python_executable: Path | None = None,
        request_client: Callable[..., Mapping[str, Any]] = (
            unix_batch_worker_request
        ),
        socket_probe: Callable[[Path], bool | _SocketIdentity] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runtime = runtime
        try:
            ledger = runtime.ledger
            projection_service = runtime.projection_service
            volume_service = runtime.volume_service
            supervisor = runtime.process_supervisor
            actor = runtime.actor_principal_id
            store_id = runtime.content_store.store_id
        except AttributeError as error:
            raise TypeError(
                "runtime must provide one composed local Realm service graph."
            ) from error
        if (
            getattr(projection_service, "ledger", None) is not ledger
            or getattr(volume_service, "ledger", None) is not ledger
        ):
            raise ValueError("retained batch services must share one Realm ledger.")
        if (
            not callable(getattr(supervisor, "reserve", None))
            or not callable(
                getattr(supervisor, "claim_launch_realization", None)
            )
            or not callable(
                getattr(supervisor, "release_launch_realization", None)
            )
            or not callable(getattr(supervisor, "seal_launch_if_absent", None))
        ):
            raise TypeError("runtime process supervisor is unavailable.")
        self._actor_principal_id = _required_text(actor, "actor_principal_id")
        self._store_id = _required_text(store_id, "content store id", max_bytes=128)
        self._ledger = ledger
        self._projection_service = projection_service
        self._volume_service = volume_service
        self._supervisor = supervisor
        if method_environment is not None and not isinstance(
            method_environment,
            (MethodLaunchEnvironment, MethodLaunchEnvironmentDescriptor),
        ):
            raise TypeError(
                "method_environment must be a MethodLaunchEnvironment, "
                "MethodLaunchEnvironmentDescriptor, or None."
            )
        if isinstance(method_environment, MethodLaunchEnvironment):
            self._method_environment_descriptor = method_environment.descriptor
            self._method_environment_values: MethodLaunchEnvironment | None = (
                method_environment
            )
        else:
            self._method_environment_descriptor = method_environment
            self._method_environment_values = None
        if not isinstance(socket_parent, Path) or not socket_parent.is_absolute():
            raise ValueError("socket_parent must be an absolute Path.")
        self._socket_parent = socket_parent
        executable = Path(sys.executable) if python_executable is None else python_executable
        self._python_executable = _real_executable(executable)
        if not callable(request_client):
            raise TypeError("request_client must be callable.")
        if socket_probe is not None and not callable(socket_probe):
            raise TypeError("socket_probe must be callable or None.")
        if not callable(monotonic) or not callable(sleep):
            raise TypeError("monotonic and sleep must be callable.")
        self._request_client = request_client
        self._socket_probe = socket_probe or _probe_worker_socket
        self._monotonic = monotonic
        self._sleep = sleep

    def realize(
        self,
        snapshot: RunLedgerSnapshot,
        *,
        ttl_seconds: float = 300.0,
        start_timeout: float = 10.0,
        request_timeout: float = 10.0,
    ) -> "RetainedPythonBatchRuntime":
        """Start or recover the one worker selected by ``snapshot``'s term."""

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        ttl = _positive_finite(ttl_seconds, "ttl_seconds")
        start_wait = _positive_finite(start_timeout, "start_timeout")
        request_wait = _positive_finite(request_timeout, "request_timeout")

        projection: ManagedReadOnlyProjection | None = None
        volume: ManagedEphemeralVolume | None = None
        candidate_volume: ManagedEphemeralVolume | None = None
        candidate_staging: FileCandidateStagingBinding | None = None
        reservation: ProcessLaunchReservation | None = None
        process: SupervisedProcess | None = None
        realization_claim: ProcessLaunchRealizationClaim | None = None
        socket_path: Path | None = None
        stage = "snapshot_changed"
        try:
            current = self._validate_current_snapshot(snapshot)
            definition = current.definition
            stage = "definition_unsupported"
            _validate_batch_definition(definition)
            if definition.prepared_method_runtime.runtime_kind == "container":
                # The container path raises only typed runtime errors, so the
                # generic wrap-as-stage handler below cannot mislabel one.
                return self._realize_container(
                    current=current,
                    definition=definition,
                    ttl=ttl,
                    start_wait=start_wait,
                    request_wait=request_wait,
                )
            method_environment_descriptor = (
                self._method_environment_descriptor_for(definition)
            )
            uses_file_candidates = _definition_candidate_format(definition) == "files"
            memberships = self._ledger.list_owner_memberships(
                actor_principal_id=self._actor_principal_id,
                owner_id=current.run.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            binding = _build_projection_binding(
                definition=definition,
                owner_id=current.run.owner_id,
                memberships=memberships,
                available_store_ids=self._projection_service.available_store_ids,
            )
            if binding.store_id != self._store_id:
                raise RealmNotFound("Entity not found.")

            coordinates = _worker_coordinates(
                realm_id=self._ledger.realm_id,
                run_id=current.run.run_id,
                controller_generation=current.controller_term.generation,
                run_definition_digest=definition.digest,
            )
            stage = "prior_generation_cleanup_failed"
            if coordinates.controller_generation > 1:
                # A controller may be replaced without ever realizing its
                # method worker.  Therefore N-1 is not sufficient: a still-live
                # generation N-2 worker must not survive merely because the
                # skipped term had no provider row.  Deterministic coordinates
                # let us authenticate and retire every lower generation
                # without process enumeration or PID authority.
                prior_authorities = {
                    generation: self._ledger.read_run_controller_term_authority(
                        actor_principal_id=self._actor_principal_id,
                        run_id=current.run.run_id,
                        generation=generation,
                    ).controller_lease
                    for generation in range(1, coordinates.controller_generation)
                }
                for generation, parent_lease in prior_authorities.items():
                    try:
                        self._preflight_generation(
                            run_id=current.run.run_id,
                            owner_id=current.run.owner_id,
                            generation=generation,
                            definition=definition,
                            binding=binding,
                            expected_parent_lease=parent_lease,
                        )
                    except Exception as error:
                        _report_cleanup_diagnostic(
                            run_id=current.run.run_id,
                            generation=generation,
                            phase="preflight",
                            error=error,
                        )
                        raise
                for generation, parent_lease in prior_authorities.items():
                    try:
                        self._reconcile_generation(
                            run_id=current.run.run_id,
                            owner_id=current.run.owner_id,
                            generation=generation,
                            definition=definition,
                            binding=binding,
                            expected_parent_lease=parent_lease,
                            grace_period=1.0,
                            timeout=10.0,
                        )
                    except Exception as error:
                        _report_cleanup_diagnostic(
                            run_id=current.run.run_id,
                            generation=generation,
                            phase="reconcile",
                            error=error,
                        )
                        raise

            realization_claim = self._supervisor.claim_launch_realization(
                launch_token=coordinates.launch_token,
                binding_id=coordinates.binding_id,
                timeout=start_wait,
            )

            operation_prefix = f"retained-batch-runtime/{coordinates.coordinate}"
            holder_id = f"method-worker-{coordinates.coordinate[:40]}"
            consumer_metadata = {
                "controller_generation": coordinates.controller_generation,
                "run_definition_digest": definition.digest,
                "run_id": current.run.run_id,
                "schema": "optpilot.retained-batch-projection-consumer.v1",
            }
            stage = "resource_realization_failed"
            try:
                projection = (
                    self._projection_service.recover_existing_private_read_only(
                        operation_id=f"{operation_prefix}/projection",
                        actor_principal_id=self._actor_principal_id,
                        store_id=binding.store_id,
                        spec=binding.spec,
                        holder_id=holder_id,
                        ttl_seconds=ttl,
                        consumer_kind="retained-batch-method",
                        consumer_metadata=consumer_metadata,
                    )
                )
            except RealmNotFound:
                projection = self._projection_service.project_read_only(
                    operation_id=f"{operation_prefix}/projection",
                    actor_principal_id=self._actor_principal_id,
                    store_id=binding.store_id,
                    spec=binding.spec,
                    holder_id=holder_id,
                    ttl_seconds=ttl,
                    consumer_kind="retained-batch-method",
                    consumer_metadata=consumer_metadata,
                    sharing_policy="private",
                )
            try:
                volume = self._volume_service.recover_existing(
                    operation_id=f"{operation_prefix}/control-volume",
                    actor_principal_id=self._actor_principal_id,
                    parent_lease=current.controller_lease,
                    holder_id=holder_id,
                    quota=_CONTROL_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl,
                )
            except RealmNotFound:
                volume = self._volume_service.create(
                    operation_id=f"{operation_prefix}/control-volume",
                    actor_principal_id=self._actor_principal_id,
                    parent_lease=current.controller_lease,
                    holder_id=holder_id,
                    quota=_CONTROL_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl,
                )
            if uses_file_candidates:
                try:
                    candidate_volume = self._volume_service.recover_existing(
                        operation_id=f"{operation_prefix}/candidate-staging-volume",
                        actor_principal_id=self._actor_principal_id,
                        parent_lease=current.controller_lease,
                        holder_id=holder_id,
                        quota=CANDIDATE_STAGING_QUOTA,
                        quota_enforcement="advisory",
                        ttl_seconds=ttl,
                    )
                except RealmNotFound:
                    candidate_volume = self._volume_service.create(
                        operation_id=f"{operation_prefix}/candidate-staging-volume",
                        actor_principal_id=self._actor_principal_id,
                        parent_lease=current.controller_lease,
                        holder_id=holder_id,
                        quota=CANDIDATE_STAGING_QUOTA,
                        quota_enforcement="advisory",
                        ttl_seconds=ttl,
                    )
                candidate_root = _prepare_candidate_staging_root(
                    candidate_volume.path
                )
                candidate_staging = FileCandidateStagingBinding(
                    run_id=current.run.run_id,
                    controller_generation=coordinates.controller_generation,
                    volume_id=candidate_volume.record.volume_id,
                    usage_lease_id=candidate_volume.lease.lease_id,
                    usage_fencing_token=candidate_volume.lease.fencing_token,
                    root_path=str(candidate_root),
                )

            socket_namespace = _prepare_socket_namespace(
                self._socket_parent,
                realm_id=self._ledger.realm_id,
            )
            socket_path = socket_namespace / f"w-{coordinates.coordinate[:24]}.sock"
            _validate_socket_path_length(socket_path)
            control_root = volume.path
            init_path = control_root / _INIT_FILE
            diagnostic_path = control_root / _DIAGNOSTIC_FILE
            initialization = RetainedBatchWorkerInit(
                run_definition=definition,
                projection_root=str(projection.root_path),
                scope_roots=binding.scope_roots,
                socket_path=str(socket_path),
                diagnostic_path=str(diagnostic_path),
                candidate_staging=candidate_staging,
            )
            init_bytes = initialization.to_bytes()
            _publish_or_verify_private_file(init_path, init_bytes)
            _prepare_private_append_file(diagnostic_path)
            request = ProcessLaunchRequest(
                argv=(
                    str(self._python_executable),
                    "-m",
                    "optpilot.retained_batch_worker",
                    str(init_path),
                ),
                cwd=str(control_root),
                env={
                    **_MINIMAL_ENV,
                    # Python's randomized string/set hashing otherwise changes
                    # method behavior across controller recovery despite an
                    # identical retained seed policy.
                    "PYTHONHASHSEED": str(int(definition.digest[:8], 16)),
                },
                private_env_names=method_environment_descriptor.names,
                private_env_binding_revision=(
                    method_environment_descriptor.binding_revision
                    if method_environment_descriptor.names
                    else None
                ),
            )
            evidence_fingerprint = request_digest(
                {
                    "coordinate": coordinates.coordinate,
                    "format": _EVIDENCE_FORMAT,
                    "initialization_digest": hashlib.sha256(init_bytes).hexdigest(),
                    "projection_spec_digest": binding.spec.digest,
                    "run_definition_digest": definition.digest,
                }
            )

            stage = "worker_start_failed"
            values_binding = self._method_environment_values
            values_available = (
                values_binding is not None
                and values_binding.values_available
            )
            if request.private_env_names and not values_available:
                try:
                    reservation = self._supervisor.lookup_reservation(
                        launch_token=coordinates.launch_token,
                        binding_id=coordinates.binding_id,
                        evidence_fingerprint=evidence_fingerprint,
                        launch_request_digest=request.digest,
                    )
                except RealmNotFound as error:
                    raise MethodLaunchEnvironmentError(
                        "method_environment_values_unavailable",
                        "Method environment values are required to start this Run.",
                        names=request.private_env_names,
                    ) from error
            else:
                reservation = self._supervisor.reserve(
                    launch_token=coordinates.launch_token,
                    binding_id=coordinates.binding_id,
                    evidence_fingerprint=evidence_fingerprint,
                    request=request,
                    realization_claim=realization_claim,
                )
            reservation_state = self._supervisor.reservation_state(reservation)
            attachment_kind: Literal["started", "attached"] = (
                "started" if reservation_state == "reserved" else "attached"
            )
            if reservation_state == "reserved":
                if os.path.lexists(socket_path):
                    raise RealmIntegrityError(
                        "retained batch socket exists before physical start."
                    )
                if request.private_env_names and not values_available:
                    # Preserve the passive reservation.  It can still be
                    # started when the exact retained binding is supplied;
                    # failed-realization cleanup must not terminalize it.
                    reservation = None
                    raise MethodLaunchEnvironmentError(
                        "method_environment_values_unavailable",
                        "Method environment values are required to start this Run.",
                        names=request.private_env_names,
                    )
            private_environment: ProcessLaunchPrivateEnvironment | None = None
            if reservation_state == "reserved" and request.private_env_names:
                private_environment = self._take_method_private_environment(
                    definition
                )
            else:
                self._discard_method_environment_values()
            assert reservation is not None
            try:
                if private_environment is None:
                    process = self._supervisor.start_reserved(reservation)
                else:
                    process = self._supervisor.start_reserved(
                        reservation,
                        private_environment=private_environment,
                    )
            finally:
                if private_environment is not None:
                    private_environment.discard()
            start_result = process.wait_started(timeout=start_wait)
            if isinstance(start_result, WorkerTerminalProof):
                self._validate_terminal_proof(
                    start_result, reservation=reservation, request=request
                )
                raise _WorkerBecameTerminal
            if not isinstance(start_result, WorkerStarted) or (
                start_result.launch_token != reservation.launch_token
                or start_result.binding_id != reservation.binding_id
                or start_result.evidence_fingerprint
                != reservation.evidence_fingerprint
                or start_result.launch_request_digest != request.digest
            ):
                raise RealmIntegrityError(
                    "retained batch worker start observation differs."
                )
            socket_identity = self._wait_until_socket_ready(
                socket_path=socket_path,
                reservation=reservation,
                timeout=start_wait,
            )
            registration = self._supervisor.record_unix_socket_endpoint(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
                endpoint_name=_CONTROL_ENDPOINT_NAME,
                path=socket_path,
                device_id=socket_identity.device_id,
                inode=socket_identity.inode,
            )
            if (
                registration.launch_token != reservation.launch_token
                or registration.binding_id != reservation.binding_id
                or registration.evidence_fingerprint
                != reservation.evidence_fingerprint
                or registration.launch_request_digest
                != reservation.launch_request_digest
                or registration.endpoint_name != _CONTROL_ENDPOINT_NAME
                or registration.state != "recorded"
            ):
                raise RealmIntegrityError(
                    "retained batch socket registration differs."
                )
        except _WorkerBecameTerminal:
            self._cleanup_failed_realization(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                reservation=reservation,
                socket_path=socket_path,
            )
            raise RetainedBatchRuntimeError("worker_terminal") from None
        except RetainedBatchRuntimeError:
            self._cleanup_failed_realization(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                reservation=reservation,
                socket_path=socket_path,
            )
            raise
        except MethodLaunchEnvironmentError:
            self._cleanup_failed_realization(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                reservation=reservation,
                socket_path=socket_path,
            )
            raise
        except Exception:
            self._cleanup_failed_realization(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                reservation=reservation,
                socket_path=socket_path,
            )
            raise RetainedBatchRuntimeError(stage) from None
        finally:
            if realization_claim is not None:
                self._supervisor.release_launch_realization(realization_claim)

        assert projection is not None
        assert volume is not None
        assert reservation is not None
        assert process is not None
        return RetainedPythonBatchRuntime(
            identity=RetainedBatchRuntimeIdentity(
                run_id=current.run.run_id,
                controller_generation=coordinates.controller_generation,
                launch_token=coordinates.launch_token,
                binding_id=coordinates.binding_id,
                run_definition_digest=definition.digest,
            ),
            projection=projection,
            volume=volume,
            candidate_volume=candidate_volume,
            candidate_staging=candidate_staging,
            supervisor=self._supervisor,
            process=process,
            reservation=reservation,
            socket_path=socket_path,
            request_client=self._request_client,
            request_timeout=request_wait,
            attachment_kind=attachment_kind,
        )

    def _realize_container(
        self,
        *,
        current: Any,
        definition: RunDefinitionManifest,
        ttl: float,
        start_wait: float,
        request_wait: float,
    ) -> "RetainedPythonBatchRuntime":
        """Start the one method container selected by the current term.

        Gates come first and nothing is written until they pass: the engine
        must exist, the image must be approved for study execution, and the
        image on this machine must be the one the record names, built for the
        declared architecture. A replayed definition passes through here
        exactly like a fresh one, which is the point of gating at realization
        rather than at compilation.
        """

        from .container_engine import (
            ContainerEngineError,
            resolve_container_engine,
            verify_image_available,
        )
        from .container_launch import ContainerMount, LaunchSpec
        from .container_method_process import (
            ContainerMethodProcess,
            remove_container_if_present,
        )
        from .realm.provider_trust_records import (
            PROVIDER_TRUST_EXECUTION_CONTRACT,
        )

        settings = definition.prepared_method_runtime.runtime_settings
        reference = str(settings["container_image_reference"])
        platform = str(settings["container_platform"])
        network = settings["container_network"] == "enabled"
        raised_limits = dict(settings.get("container_limits") or {})

        descriptor = self._method_environment_descriptor_for(definition)
        values_binding = self._method_environment_values
        if descriptor.names and (
            values_binding is None or not values_binding.values_available
        ):
            # Checked before anything is written: starting without the declared
            # values would run the method in a state its author never meant,
            # and a container has no attach path that could supply them later.
            raise MethodLaunchEnvironmentError(
                "method_environment_values_unavailable",
                "Method environment values are required to start this Run.",
                names=descriptor.names,
            )

        try:
            engine = resolve_container_engine()
        except ContainerEngineError:
            raise RetainedBatchRuntimeError(
                "container_engine_unavailable"
            ) from None

        trust = getattr(self._runtime, "provider_trust_policy", None)
        approval = None
        if trust is not None:
            try:
                approval = trust.read_active(
                    image_ref=reference,
                    contract=PROVIDER_TRUST_EXECUTION_CONTRACT,
                )
            except Exception:
                approval = None
        if approval is None:
            # No policy service, or no approval: both fail closed. Running
            # someone's image without a recorded approval is the thing the
            # trust ledger exists to prevent.
            from .realm.provider_trust_records import image_approval_remedy

            raise RetainedBatchRuntimeError(
                "container_untrusted",
                f"The image is {reference}. {image_approval_remedy(reference)}",
            )

        try:
            verify_image_available(engine, reference, platform)
        except ContainerEngineError:
            raise RetainedBatchRuntimeError(
                "container_image_unavailable"
            ) from None

        uses_file_candidates = _definition_candidate_format(definition) == "files"
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=self._actor_principal_id,
            owner_id=current.run.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        binding = _build_projection_binding(
            definition=definition,
            owner_id=current.run.owner_id,
            memberships=memberships,
            available_store_ids=self._projection_service.available_store_ids,
        )
        if binding.store_id != self._store_id:
            raise RealmNotFound("Entity not found.")

        coordinates = _worker_coordinates(
            realm_id=self._ledger.realm_id,
            run_id=current.run.run_id,
            controller_generation=current.controller_term.generation,
            run_definition_digest=definition.digest,
        )
        container_name = f"optpilot-mw-{coordinates.coordinate[:24]}"

        # Leftovers from earlier controller generations are removed, never
        # adopted: their stream died with their controller. A container already
        # holding THIS generation's name means this controller cannot know what
        # it is -- refuse rather than guess, and a new generation removes it.
        for generation in range(1, coordinates.controller_generation):
            stale = _worker_coordinates(
                realm_id=self._ledger.realm_id,
                run_id=current.run.run_id,
                controller_generation=generation,
                run_definition_digest=definition.digest,
            )
            remove_container_if_present(
                engine, f"optpilot-mw-{stale.coordinate[:24]}"
            )
        import subprocess as _subprocess

        probe = _subprocess.run(
            [engine, "container", "inspect", container_name],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode == 0:
            raise RetainedBatchRuntimeError("container_worker_unrecoverable")

        operation_prefix = f"retained-batch-runtime/{coordinates.coordinate}"
        holder_id = f"method-worker-{coordinates.coordinate[:40]}"
        consumer_metadata = {
            "controller_generation": coordinates.controller_generation,
            "run_definition_digest": definition.digest,
            "run_id": current.run.run_id,
            "schema": "optpilot.retained-batch-projection-consumer.v1",
        }
        projection: ManagedReadOnlyProjection | None = None
        volume: ManagedEphemeralVolume | None = None
        candidate_volume: ManagedEphemeralVolume | None = None
        process: ContainerMethodProcess | None = None
        try:
            try:
                projection = (
                    self._projection_service.recover_existing_private_read_only(
                        operation_id=f"{operation_prefix}/projection",
                        actor_principal_id=self._actor_principal_id,
                        store_id=binding.store_id,
                        spec=binding.spec,
                        holder_id=holder_id,
                        ttl_seconds=ttl,
                        consumer_kind="retained-batch-method",
                        consumer_metadata=consumer_metadata,
                    )
                )
            except RealmNotFound:
                projection = self._projection_service.project_read_only(
                    operation_id=f"{operation_prefix}/projection",
                    actor_principal_id=self._actor_principal_id,
                    store_id=binding.store_id,
                    spec=binding.spec,
                    holder_id=holder_id,
                    ttl_seconds=ttl,
                    consumer_kind="retained-batch-method",
                    consumer_metadata=consumer_metadata,
                    sharing_policy="private",
                )
            try:
                volume = self._volume_service.recover_existing(
                    operation_id=f"{operation_prefix}/control-volume",
                    actor_principal_id=self._actor_principal_id,
                    parent_lease=current.controller_lease,
                    holder_id=holder_id,
                    quota=_CONTROL_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl,
                )
            except RealmNotFound:
                volume = self._volume_service.create(
                    operation_id=f"{operation_prefix}/control-volume",
                    actor_principal_id=self._actor_principal_id,
                    parent_lease=current.controller_lease,
                    holder_id=holder_id,
                    quota=_CONTROL_QUOTA,
                    quota_enforcement="advisory",
                    ttl_seconds=ttl,
                )
            host_staging: FileCandidateStagingBinding | None = None
            container_staging: FileCandidateStagingBinding | None = None
            if uses_file_candidates:
                try:
                    candidate_volume = self._volume_service.recover_existing(
                        operation_id=(
                            f"{operation_prefix}/candidate-staging-volume"
                        ),
                        actor_principal_id=self._actor_principal_id,
                        parent_lease=current.controller_lease,
                        holder_id=holder_id,
                        quota=CANDIDATE_STAGING_QUOTA,
                        quota_enforcement="advisory",
                        ttl_seconds=ttl,
                    )
                except RealmNotFound:
                    candidate_volume = self._volume_service.create(
                        operation_id=(
                            f"{operation_prefix}/candidate-staging-volume"
                        ),
                        actor_principal_id=self._actor_principal_id,
                        parent_lease=current.controller_lease,
                        holder_id=holder_id,
                        quota=CANDIDATE_STAGING_QUOTA,
                        quota_enforcement="advisory",
                        ttl_seconds=ttl,
                    )
                candidate_root = _prepare_candidate_staging_root(
                    candidate_volume.path
                )
                shared = dict(
                    run_id=current.run.run_id,
                    controller_generation=coordinates.controller_generation,
                    volume_id=candidate_volume.record.volume_id,
                    usage_lease_id=candidate_volume.lease.lease_id,
                    usage_fencing_token=candidate_volume.lease.fencing_token,
                )
                # Two views of one directory: the worker sees the mount, the
                # host keeps reading frozen candidates at its own path.
                host_staging = FileCandidateStagingBinding(
                    root_path=str(candidate_root), **shared
                )
                container_staging = FileCandidateStagingBinding(
                    root_path="/optpilot/candidates/staging", **shared
                )

            control_root = volume.path
            init_path = control_root / _INIT_FILE
            diagnostic_path = control_root / _DIAGNOSTIC_FILE
            initialization = RetainedBatchWorkerInit(
                run_definition=definition,
                projection_root="/optpilot/projection",
                scope_roots=binding.scope_roots,
                socket_path=None,
                diagnostic_path=f"/optpilot/control/{_DIAGNOSTIC_FILE}",
                candidate_staging=container_staging,
            )
            init_bytes = initialization.to_bytes()
            _publish_or_verify_private_file(init_path, init_bytes)
            _prepare_private_append_file(diagnostic_path)

            evidence_fingerprint = request_digest(
                {
                    "coordinate": coordinates.coordinate,
                    "format": _EVIDENCE_FORMAT,
                    "initialization_digest": hashlib.sha256(
                        init_bytes
                    ).hexdigest(),
                    "projection_spec_digest": binding.spec.digest,
                    "run_definition_digest": definition.digest,
                }
            )
            del evidence_fingerprint  # recorded via the init bytes themselves

            import optpilot as _optpilot_package

            launcher_root = (
                Path(_optpilot_package.__file__).resolve().parent.parent
            )
            mounts = [
                ContainerMount(launcher_root, "/optpilot/launcher"),
                ContainerMount(projection.root_path, "/optpilot/projection"),
                ContainerMount(control_root, "/optpilot/control", read_only=False),
            ]
            if candidate_volume is not None:
                mounts.append(
                    ContainerMount(
                        candidate_volume.path,
                        "/optpilot/candidates",
                        read_only=False,
                    )
                )
            spec = LaunchSpec(
                image=reference,
                platform=platform,
                name=container_name,
                command=(
                    "python3",
                    "-m",
                    "optpilot.retained_batch_worker",
                    "--stdio",
                    f"/optpilot/control/{_INIT_FILE}",
                ),
                mounts=mounts,
                workdir="/optpilot/control",
                network=network,
                env={
                    **_MINIMAL_ENV,
                    "PYTHONHASHSEED": str(int(definition.digest[:8], 16)),
                },
                import_paths=("/optpilot/launcher",),
                limits=_method_container_limits(raised_limits),
                interactive=True,
            )
            env_file: Path | None = None
            if descriptor.names:
                assert values_binding is not None
                values = values_binding.take_process_environment()
                self._method_environment_values = None
                try:
                    for name, value in values.items():
                        if "\n" in value or "\x00" in value or "\n" in name:
                            # The env-file format has no escaping: one line per
                            # value. Refusing beats delivering a truncated
                            # secret that fails much later, far from its cause.
                            raise RetainedBatchRuntimeError(
                                "container_environment_value_unsupported"
                            )
                    # Host-private, outside every mount, gone again right after
                    # the engine client has read it. The value never touches the
                    # command line, where any process on the machine could read
                    # it for as long as the exchange lives.
                    private_root = Path(self._socket_parent)
                    private_root.mkdir(parents=True, exist_ok=True)
                    env_file = private_root / f"mw-env-{coordinates.coordinate[:24]}"
                    descriptor_fd = os.open(
                        env_file,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(descriptor_fd, "w", encoding="utf-8") as handle:
                        for name in sorted(values):
                            handle.write(f"{name}={values[name]}\n")
                    spec = dataclasses.replace(spec, env_file=env_file)
                finally:
                    values.clear()
            stderr_log = open(control_root / "worker-stderr.log", "ab")
            try:
                process = ContainerMethodProcess(
                    engine, spec, stderr_target=stderr_log
                )
            finally:
                stderr_log.close()
            ready = process.request(
                {
                    "exchange_id": "container-ready",
                    "op": "status",
                    "payload": {},
                    "schema": BATCH_REQUEST_SCHEMA,
                },
                timeout=start_wait,
            )
            if ready.get("ok") is not True:
                raise RetainedBatchRuntimeError("worker_start_failed")
            if env_file is not None:
                # The engine client read it while creating the container; the
                # readiness answer proves that point is past.
                env_file.unlink(missing_ok=True)
                env_file = None
        except RetainedBatchRuntimeError:
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            self._cleanup_failed_container(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                process=process,
            )
            raise
        except Exception:
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            self._cleanup_failed_container(
                projection=projection,
                volume=volume,
                candidate_volume=candidate_volume,
                process=process,
            )
            raise RetainedBatchRuntimeError("worker_start_failed") from None

        assert projection is not None and volume is not None
        assert process is not None
        return RetainedPythonBatchRuntime(
            identity=RetainedBatchRuntimeIdentity(
                run_id=current.run.run_id,
                controller_generation=coordinates.controller_generation,
                launch_token=coordinates.launch_token,
                binding_id=coordinates.binding_id,
                run_definition_digest=definition.digest,
            ),
            projection=projection,
            volume=volume,
            candidate_volume=candidate_volume,
            candidate_staging=host_staging,
            supervisor=None,
            process=None,
            reservation=None,
            socket_path=None,
            request_client=process.request_client(),
            request_timeout=request_wait,
            attachment_kind="started",
            container_process=process,
        )

    def _cleanup_failed_container(
        self,
        *,
        projection: ManagedReadOnlyProjection | None,
        volume: ManagedEphemeralVolume | None,
        candidate_volume: ManagedEphemeralVolume | None,
        process: Any,
    ) -> None:
        if process is not None:
            try:
                process.stop(grace_seconds=5.0)
            except Exception:
                pass
        for resource in (candidate_volume, volume, projection):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                pass

    def _method_environment_descriptor_for(
        self, definition: RunDefinitionManifest
    ) -> MethodLaunchEnvironmentDescriptor:
        """Return the value-free binding identity for one retained Method."""

        required_names = method_environment_names(definition)
        descriptor = self._method_environment_descriptor
        if descriptor is None:
            if required_names:
                raise MethodLaunchEnvironmentError(
                    "method_environment_missing",
                    "Method environment values have not been bound.",
                    names=required_names,
                )
            descriptor = MethodLaunchEnvironmentDescriptor(
                names=(),
                binding_revision="none",
            )
        if not descriptor.matches(definition):
            raise MethodLaunchEnvironmentError(
                "method_environment_binding_mismatch",
                "Method launch environment requirements changed before execution.",
                names=required_names,
            )
        return descriptor

    def _take_method_private_environment(
        self,
        definition: RunDefinitionManifest,
    ) -> ProcessLaunchPrivateEnvironment:
        descriptor = self._method_environment_descriptor_for(definition)
        binding = self._method_environment_values
        if binding is None:
            raise MethodLaunchEnvironmentError(
                "method_environment_values_unavailable",
                "Method environment values are required to start this Run.",
                names=descriptor.names,
            )
        values = binding.take_process_environment()
        self._method_environment_values = None
        try:
            return ProcessLaunchPrivateEnvironment(
                binding_revision=descriptor.binding_revision,
                values=values,
            )
        finally:
            values.clear()

    def _discard_method_environment_values(self) -> None:
        binding = self._method_environment_values
        self._method_environment_values = None
        if binding is not None:
            binding.discard_values()

    def reconcile_inactive(
        self,
        snapshot: RunLedgerSnapshot,
        *,
        grace_period: float = 1.0,
        timeout: float = 10.0,
    ) -> RetainedBatchInactiveReconciliation:
        """Reconcile an exact method-inactive run term without starting it.

        Method inactivity is canonical, not caller-selected: a terminal run is
        inactive, as is a draining run whose feedback obligations are resolved.
        Hard-stop submission codes waive pending feedback through the ledger's
        atomic method-exchange abandonment transition.  Deterministic provider
        coordinates then authenticate and reconcile every generation without
        realizing or attaching to the method worker.
        """

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        grace = _nonnegative_finite(grace_period, "grace_period")
        wait = _positive_finite(timeout, "timeout")
        try:
            current = self._validate_current_inactive_snapshot(snapshot)
        except Exception:
            raise RetainedBatchRuntimeError("snapshot_changed") from None
        try:
            definition = current.definition
            _validate_batch_definition(definition)
            self._method_environment_descriptor_for(definition)
        except Exception:
            raise RetainedBatchRuntimeError("definition_unsupported") from None
        try:
            memberships = self._ledger.list_owner_memberships(
                actor_principal_id=self._actor_principal_id,
                owner_id=current.run.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            binding = _build_projection_binding(
                definition=definition,
                owner_id=current.run.owner_id,
                memberships=memberships,
                available_store_ids=self._projection_service.available_store_ids,
            )
            if binding.store_id != self._store_id:
                raise RealmNotFound("Entity not found.")
        except Exception:
            raise RetainedBatchRuntimeError("content_unavailable") from None

        disposition: Literal[
            "absent",
            "already_retired",
            "already_terminal",
            "never_started",
            "stopped",
            "quarantined",
        ] = "absent"
        try:
            authorities = {
                generation: self._ledger.read_run_controller_term_authority(
                    actor_principal_id=self._actor_principal_id,
                    run_id=current.run.run_id,
                    generation=generation,
                ).controller_lease
                for generation in range(
                    1, current.controller_term.generation + 1
                )
            }
            if (
                authorities[current.controller_term.generation]
                != current.controller_lease
            ):
                raise RealmIntegrityError(
                    "terminal controller authority differs from the run snapshot."
                )
            for generation, parent_lease in authorities.items():
                self._preflight_generation(
                    run_id=current.run.run_id,
                    owner_id=current.run.owner_id,
                    generation=generation,
                    definition=definition,
                    binding=binding,
                    expected_parent_lease=parent_lease,
                )
        except Exception:
            raise RetainedBatchRuntimeError("cleanup_failed") from None

        failed = False
        # A skipped controller term can leave a complete provider generation
        # even when its successor never attached to it.  Terminal retirement is
        # therefore a full deterministic sweep, not merely cleanup of the last
        # generation.  Each generation is authenticated independently before
        # its worker or resources are touched.
        for generation, parent_lease in authorities.items():
            try:
                selected = self._reconcile_generation(
                    run_id=current.run.run_id,
                    owner_id=current.run.owner_id,
                    generation=generation,
                    definition=definition,
                    binding=binding,
                    expected_parent_lease=parent_lease,
                    grace_period=grace,
                    timeout=wait,
                )
            except Exception:
                failed = True
            else:
                if generation == current.controller_term.generation:
                    disposition = selected
        if failed:
            raise RetainedBatchRuntimeError("cleanup_failed")
        return RetainedBatchInactiveReconciliation(
            run_id=current.run.run_id,
            controller_generation=current.controller_term.generation,
            run_definition_digest=definition.digest,
            worker_disposition=disposition,
            resources_reconciled=True,
        )

    def _preflight_generation(
        self,
        *,
        run_id: str,
        owner_id: str,
        generation: int,
        definition: RunDefinitionManifest,
        binding: _ProjectionBinding,
        expected_parent_lease: LeaseRecord,
    ) -> None:
        """Authenticate every generation resource before any cleanup effect."""

        coordinates = _worker_coordinates(
            realm_id=self._ledger.realm_id,
            run_id=run_id,
            controller_generation=generation,
            run_definition_digest=definition.digest,
        )
        operation_prefix = f"retained-batch-runtime/{coordinates.coordinate}"
        holder_id = f"method-worker-{coordinates.coordinate[:40]}"
        consumer_metadata = {
            "controller_generation": generation,
            "run_definition_digest": definition.digest,
            "run_id": run_id,
            "schema": "optpilot.retained-batch-projection-consumer.v1",
        }
        self._inspect_generation_projection(
            operation_id=f"{operation_prefix}/projection",
            binding=binding,
            holder_id=holder_id,
            consumer_metadata=consumer_metadata,
        )
        self._inspect_generation_volume(
            operation_id=f"{operation_prefix}/control-volume",
            owner_id=owner_id,
            run_id=run_id,
            generation=generation,
            holder_id=holder_id,
            expected_parent_lease=expected_parent_lease,
            quota=_CONTROL_QUOTA,
            label="control",
        )
        if _definition_candidate_format(definition) == "files":
            self._inspect_generation_volume(
                operation_id=f"{operation_prefix}/candidate-staging-volume",
                owner_id=owner_id,
                run_id=run_id,
                generation=generation,
                holder_id=holder_id,
                expected_parent_lease=expected_parent_lease,
                quota=CANDIDATE_STAGING_QUOTA,
                label="candidate staging",
            )

    def _reconcile_generation(
        self,
        *,
        run_id: str,
        owner_id: str,
        generation: int,
        definition: RunDefinitionManifest,
        binding: _ProjectionBinding,
        expected_parent_lease: LeaseRecord,
        grace_period: float,
        timeout: float,
    ) -> Literal[
        "absent",
        "already_retired",
        "already_terminal",
        "never_started",
        "stopped",
        "quarantined",
    ]:
        coordinates = _worker_coordinates(
            realm_id=self._ledger.realm_id,
            run_id=run_id,
            controller_generation=generation,
            run_definition_digest=definition.digest,
        )
        operation_prefix = f"retained-batch-runtime/{coordinates.coordinate}"
        projection_operation = f"{operation_prefix}/projection"
        volume_operation = f"{operation_prefix}/control-volume"
        candidate_volume_operation = (
            f"{operation_prefix}/candidate-staging-volume"
        )
        uses_file_candidates = _definition_candidate_format(definition) == "files"
        holder_id = f"method-worker-{coordinates.coordinate[:40]}"
        consumer_metadata = {
            "controller_generation": generation,
            "run_definition_digest": definition.digest,
            "run_id": run_id,
            "schema": "optpilot.retained-batch-projection-consumer.v1",
        }
        launch_seal = self._supervisor.seal_launch_if_absent(
            launch_token=coordinates.launch_token,
            binding_id=coordinates.binding_id,
            timeout=timeout,
        )
        if (
            not isinstance(launch_seal, ProcessLaunchSealReceipt)
            or launch_seal.launch_token != coordinates.launch_token
            or launch_seal.binding_id != coordinates.binding_id
        ):
            raise RealmIntegrityError(
                "retained batch launch seal decision differs."
            )
        projection_records, projection_root = self._inspect_generation_projection(
            operation_id=projection_operation,
            binding=binding,
            holder_id=holder_id,
            consumer_metadata=consumer_metadata,
        )
        volume_record, control_root = self._inspect_generation_volume(
            operation_id=volume_operation,
            owner_id=owner_id,
            run_id=run_id,
            generation=generation,
            holder_id=holder_id,
            expected_parent_lease=expected_parent_lease,
            quota=_CONTROL_QUOTA,
            label="control",
        )
        candidate_volume_record: Any | None = None
        candidate_root: Path | None = None
        if uses_file_candidates:
            (
                candidate_volume_record,
                candidate_root,
            ) = self._inspect_generation_volume(
                operation_id=candidate_volume_operation,
                owner_id=owner_id,
                run_id=run_id,
                generation=generation,
                holder_id=holder_id,
                expected_parent_lease=expected_parent_lease,
                quota=CANDIDATE_STAGING_QUOTA,
                label="candidate staging",
            )

        namespace = _prepare_socket_namespace(
            self._socket_parent, realm_id=self._ledger.realm_id
        )
        socket_path = namespace / f"w-{coordinates.coordinate[:24]}.sock"
        _validate_socket_path_length(socket_path)

        expected_evidence: str | None = None
        expected_request_digest: str | None = None
        has_complete_resources = (
            projection_root is not None and control_root is not None
            and (not uses_file_candidates or candidate_root is not None)
        )
        if has_complete_resources:
            candidate_staging = None
            if uses_file_candidates:
                assert candidate_volume_record is not None
                assert candidate_root is not None
                candidate_staging = FileCandidateStagingBinding(
                    run_id=run_id,
                    controller_generation=generation,
                    volume_id=candidate_volume_record.volume_id,
                    usage_lease_id=candidate_volume_record.usage_lease_id,
                    usage_fencing_token=1,
                    # The live worker binding used the canonical provider root
                    # returned by _prepare_candidate_staging_root.  Terminal
                    # replay must reconstruct that same string after cleanup
                    # has removed the generation wrapper, so resolve only the
                    # still-present provider root and append the authenticated
                    # deterministic relative name.
                    root_path=str(
                        self._volume_service._root_binding.path.resolve(
                            strict=True
                        )
                        / candidate_volume_record.relative_name
                        / "data"
                    ),
                )
            initialization = RetainedBatchWorkerInit(
                run_definition=definition,
                projection_root=str(projection_root),
                scope_roots=binding.scope_roots,
                socket_path=str(socket_path),
                diagnostic_path=str(control_root / _DIAGNOSTIC_FILE),
                candidate_staging=candidate_staging,
            )
            init_bytes = initialization.to_bytes()
            method_environment_descriptor = (
                self._method_environment_descriptor_for(definition)
            )
            request = ProcessLaunchRequest(
                argv=(
                    str(self._python_executable),
                    "-m",
                    "optpilot.retained_batch_worker",
                    str(control_root / _INIT_FILE),
                ),
                cwd=str(control_root),
                env={
                    **_MINIMAL_ENV,
                    "PYTHONHASHSEED": str(int(definition.digest[:8], 16)),
                },
                private_env_names=method_environment_descriptor.names,
                private_env_binding_revision=(
                    method_environment_descriptor.binding_revision
                    if method_environment_descriptor.names
                    else None
                ),
            )
            expected_request_digest = request.digest
            expected_evidence = request_digest(
                {
                    "coordinate": coordinates.coordinate,
                    "format": _EVIDENCE_FORMAT,
                    "initialization_digest": hashlib.sha256(init_bytes).hexdigest(),
                    "projection_spec_digest": binding.spec.digest,
                    "run_definition_digest": definition.digest,
                }
            )

        disposition: Literal[
            "absent",
            "already_retired",
            "already_terminal",
            "never_started",
            "stopped",
            "quarantined",
        ] = "absent"
        if not launch_seal.sealed:
            if expected_evidence is None or expected_request_digest is None:
                raise RealmIntegrityError(
                    "existing retained batch launch lacks complete resources."
                )
            try:
                reconciliation = self._supervisor.reconcile_terminal_launch(
                    launch_token=coordinates.launch_token,
                    binding_id=coordinates.binding_id,
                    evidence_fingerprint=expected_evidence,
                    launch_request_digest=expected_request_digest,
                    grace_period=grace_period,
                    timeout=timeout,
                )
            except ProcessLaunchUnauthenticatable:
                # The recorded worker can never be re-authenticated (its
                # startup coordinates or lock identity are permanently
                # unreconstructable - a crashed provider, a moved
                # interpreter, replaced artifacts). Quarantine retires the
                # row only after the supervisor proves the process is dead;
                # otherwise the original conflict propagates and this
                # generation stays unreconciled.
                self._supervisor.quarantine_unauthenticatable_launch(
                    launch_token=coordinates.launch_token,
                    binding_id=coordinates.binding_id,
                    timeout=timeout,
                )
                disposition = "quarantined"
            else:
                proof = reconciliation.proof
                if (
                    reconciliation.retired is not True
                    or reconciliation.endpoints_reconciled is not True
                    or reconciliation.launch_token != coordinates.launch_token
                    or reconciliation.binding_id != coordinates.binding_id
                    or reconciliation.evidence_fingerprint != expected_evidence
                    or reconciliation.launch_request_digest
                    != expected_request_digest
                    or proof.launch_token != coordinates.launch_token
                    or proof.binding_id != coordinates.binding_id
                    or proof.evidence_fingerprint != expected_evidence
                    or proof.launch_request_digest != expected_request_digest
                ):
                    raise RealmIntegrityError(
                        "retained batch terminal reconciliation differs."
                    )
                disposition = {
                    "reserved": "never_started",
                    "start_requested": "stopped",
                    "terminal": "already_terminal",
                    "retired": "already_retired",
                }[reconciliation.prior_state]

        # The token/binding seal is installed before this second resource read,
        # so a stale realizer cannot fill in a missing resource and reserve the
        # worker afterward.  A row that won the ordering first is never stopped
        # with that weaker identity: both resources must reconstruct all four
        # startup coordinates before exact terminal reconciliation.
        # Endpoint deletion, when authorized, has already occurred inside the
        # supervisor using its provider-owned registry identity.  A present
        # unrecorded or replaced pathname therefore fails closed here.
        _require_socket_absent(socket_path)
        if candidate_volume_record is not None:
            self._reconcile_terminal_volume(
                operation_id=candidate_volume_operation,
                owner_id=owner_id,
                run_id=run_id,
                generation=generation,
                holder_id=holder_id,
                expected_parent_lease=expected_parent_lease,
                quota=CANDIDATE_STAGING_QUOTA,
                label="candidate staging",
            )
        if volume_record is not None:
            self._reconcile_terminal_volume(
                operation_id=volume_operation,
                owner_id=owner_id,
                run_id=run_id,
                generation=generation,
                holder_id=holder_id,
                expected_parent_lease=expected_parent_lease,
                quota=_CONTROL_QUOTA,
                label="control",
            )
        if projection_records:
            self._reconcile_terminal_projection(
                operation_id=projection_operation,
                binding=binding,
                holder_id=holder_id,
                consumer_metadata=consumer_metadata,
            )
        return disposition

    def _reconcile_terminal_volume(
        self,
        *,
        operation_id: str,
        owner_id: str,
        run_id: str,
        generation: int,
        holder_id: str,
        expected_parent_lease: LeaseRecord,
        quota: FilesystemQuota,
        label: str,
    ) -> None:
        record, _path = self._inspect_generation_volume(
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            generation=generation,
            holder_id=holder_id,
            expected_parent_lease=expected_parent_lease,
            quota=quota,
            label=label,
        )
        if record is None:
            return
        _key, volume_id, _lease_id = _volume_operation_identity(operation_id)
        reconcile_key = request_digest(
            {
                "format": "optpilot.retained-batch-terminal-volume-cleanup.v1",
                "operation_id": operation_id,
                "volume_id": volume_id,
            }
        )
        try:
            receipt = self._volume_service.reconcile_volume(
                operation_id=(
                    "retained-batch.terminal.volume-reconcile/" + reconcile_key
                ),
                volume_id=volume_id,
                ttl_seconds=_TERMINAL_CLEANUP_TTL_SECONDS,
            )
        except RealmNotFound:
            return
        if receipt.volume.state is not EphemeralVolumeState.CLEANED:
            raise RealmIntegrityError(
                "retained batch control volume did not reconcile."
            )

    def _inspect_generation_volume(
        self,
        *,
        operation_id: str,
        owner_id: str,
        run_id: str,
        generation: int,
        holder_id: str,
        expected_parent_lease: LeaseRecord,
        quota: FilesystemQuota,
        label: str,
    ) -> tuple[Any | None, Path | None]:
        if not isinstance(quota, FilesystemQuota):
            raise TypeError("retained batch volume quota is invalid.")
        selected_label = _required_text(label, "retained batch volume label")
        service = self._volume_service
        service._ensure_registered_root(require_active=False)
        key, volume_id, usage_lease_id = _volume_operation_identity(operation_id)
        try:
            record = service._maintenance_volume(volume_id)
        except RealmNotFound:
            return None, None
        binding = service._root_binding
        expected_claim = request_digest(
            {
                "format": "optpilot.ephemeral-volume-claim-nonce.v1",
                "volume_id": volume_id,
                "volume_root_id": binding.volume_root_id,
            }
        )
        if (
            record.volume_id != volume_id
            or record.volume_root_id != binding.volume_root_id
            or record.owner_id != owner_id
            or record.usage_lease_id != usage_lease_id
            or record.provider_kind != binding.provider_kind
            or record.quota != quota
            or record.quota_enforcement != "advisory"
            or record.claim_nonce != expected_claim
            or record.relative_name != f"volume-{key[:48]}"
            or record.parent_lease_id != expected_parent_lease.lease_id
            or expected_parent_lease.owner_id != owner_id
            or expected_parent_lease.parent_lease_id is not None
            or expected_parent_lease.lease_kind != "run-controller"
            or expected_parent_lease.audience != "realm-ledger"
            or expected_parent_lease.scope_key != f"run:{run_id}"
            or dict(expected_parent_lease.metadata)
            != {
                "controller_generation": generation,
                "run_id": run_id,
            }
        ):
            raise RealmIntegrityError(
                f"retained batch {selected_label} volume operation differs."
            )
        if record.state is EphemeralVolumeState.QUARANTINED:
            raise RealmIntegrityError(
                f"retained batch {selected_label} volume is quarantined."
            )
        path = binding.path / record.relative_name / "data"
        if record.state is EphemeralVolumeState.ACTIVE:
            identity = service._current_identity(record)
            _validate_managed_directory_identity(
                binding.path / record.relative_name,
                device_id=identity.wrapper_device_id,
                inode=identity.wrapper_inode,
                label=f"retained batch {selected_label} volume wrapper",
            )
            _validate_managed_directory_identity(
                path,
                device_id=identity.data_device_id,
                inode=identity.data_inode,
                label=f"retained batch {selected_label} volume data",
            )
            # Terminal maintenance may run after the usage lease expires.  Its
            # authority is the immutable operation-derived volume identity and
            # provider-owned filesystem identity checked above, never renewed
            # liveness authority from a stale controller term.
        return record, path

    def _inspect_generation_projection(
        self,
        *,
        operation_id: str,
        binding: _ProjectionBinding,
        holder_id: str,
        consumer_metadata: Mapping[str, Any],
    ) -> tuple[tuple[Any, ...], Path | None]:
        service = self._projection_service
        service._ensure_registered_root(require_active=False)
        root_binding = service._root_binding
        root_id = root_binding.projection_root_id
        operation_coordinate = request_digest(
            {
                "format": _PRIVATE_OPERATION_COORDINATE_FORMAT,
                "realm_id": self._ledger.realm_id,
                "operation_id": operation_id,
            }
        )
        records = self._ledger.list_projection_realizations(
            actor_principal_id=service._maintenance_principal_id,
            projection_root_id=root_id,
            states=tuple(ProjectionRealizationState),
        )
        selected: list[Any] = []
        runtime_record: Any | None = None
        for record in records:
            sharing = record.availability_resolution.get("realization_sharing")
            if not isinstance(sharing, Mapping) or sharing.get(
                "operation_coordinate_digest"
            ) != operation_coordinate:
                continue
            expected_request_digest = request_digest(
                {
                    "format": "optpilot.projection-request.v1",
                    "spec_digest": binding.spec.digest,
                    "availability_resolution_digest": request_digest(
                        _deep_thaw_json(record.availability_resolution)
                    ),
                    "provider_kind": service._provider.PROVIDER_KIND,
                }
            )
            if (
                record.projection_root_id != root_id
                or record.owner_id != binding.spec.owner_id
                or record.store_id != binding.store_id
                or record.provider_kind != service._provider.PROVIDER_KIND
                or record.spec_digest != binding.spec.digest
                or _deep_thaw_json(record.spec) != binding.spec.to_dict()
                or sharing.get("policy") != "private"
                or record.request_digest != expected_request_digest
            ):
                raise RealmIntegrityError(
                    "retained batch projection operation differs."
                )
            selected.append(record)
            consumers = self._ledger.list_projection_consumers(
                actor_principal_id=self._actor_principal_id,
                realization_id=record.realization_id,
            )
            if len(consumers) > 1:
                raise RealmIntegrityError(
                    "retained batch private projection has multiple consumers."
                )
            if consumers:
                consumer = consumers[0]
                authority = self._ledger.read_projection_consumer_authority(
                    actor_principal_id=self._actor_principal_id,
                    realization_id=record.realization_id,
                    consumer_id=consumer.consumer_id,
                )
                consumer_lease = authority.consumer_lease
                if (
                    authority.realization != record
                    or authority.consumer != consumer
                    or consumer.consumer_kind != "retained-batch-method"
                    or dict(consumer.metadata) != dict(consumer_metadata)
                    or consumer_lease.owner_id != binding.spec.owner_id
                    or consumer_lease.holder_id != holder_id
                    or consumer_lease.fencing_token != 1
                ):
                    raise RealmIntegrityError(
                        "retained batch projection consumer differs."
                    )
                if record.state is ProjectionRealizationState.READY:
                    # Like volume maintenance, cleanup must continue after the
                    # consumer lease expires.  The private operation coordinate,
                    # exact spec/request, consumer kind/metadata, and durable
                    # provider identities are the cleanup authority.
                    if runtime_record is not None:
                        raise RealmIntegrityError(
                            "retained batch projection has multiple live copies."
                        )
                    runtime_record = record
        if not selected:
            return (), None
        if runtime_record is None:
            runtime_record = max(
                selected,
                key=lambda item: (
                    getattr(item, "created_at", 0.0), item.realization_id
                ),
            )
        path = root_binding.path / runtime_record.relative_name / "root"
        if runtime_record.state is ProjectionRealizationState.READY:
            identity = service._current_namespace_identity(runtime_record)
            _validate_managed_directory_identity(
                root_binding.path / runtime_record.relative_name,
                device_id=identity.wrapper_device_id,
                inode=identity.wrapper_inode,
                label="retained batch projection wrapper",
            )
            _validate_managed_directory_identity(
                path,
                device_id=identity.tree_device_id,
                inode=identity.tree_inode,
                label="retained batch projection tree",
            )
        return tuple(selected), path

    def _reconcile_terminal_projection(
        self,
        *,
        operation_id: str,
        binding: _ProjectionBinding,
        holder_id: str,
        consumer_metadata: Mapping[str, Any],
    ) -> None:
        service = self._projection_service
        selected, _runtime_path = self._inspect_generation_projection(
            operation_id=operation_id,
            binding=binding,
            holder_id=holder_id,
            consumer_metadata=consumer_metadata,
        )

        for record in selected:
            if record.state is ProjectionRealizationState.CLEANED:
                continue
            retired = False
            if record.state is ProjectionRealizationState.READY:
                consumers = self._ledger.list_projection_consumers(
                    actor_principal_id=self._actor_principal_id,
                    realization_id=record.realization_id,
                )
                if len(consumers) > 1:
                    raise RealmIntegrityError(
                        "retained batch private projection has multiple consumers."
                    )
                if consumers:
                    consumer = consumers[0]
                    if (
                        consumer.consumer_kind != "retained-batch-method"
                        or dict(consumer.metadata) != dict(consumer_metadata)
                    ):
                        raise RealmIntegrityError(
                            "retained batch projection consumer differs."
                        )
                    try:
                        projection = service.reattach_private_read_only_consumer(
                            actor_principal_id=self._actor_principal_id,
                            expected_operation_id=operation_id,
                            realization_id=record.realization_id,
                            consumer_id=consumer.consumer_id,
                            consumer_holder_id=holder_id,
                            consumer_fencing_token=1,
                        )
                        cleaned = service.retire_private_projection(
                            projection,
                            ttl_seconds=_TERMINAL_CLEANUP_TTL_SECONDS,
                        )
                        if cleaned.state is not ProjectionRealizationState.CLEANED:
                            raise RealmIntegrityError(
                                "retained batch private projection did not retire."
                            )
                        retired = True
                    except (RealmConflict, RealmExpired, RealmNotFound):
                        retired = False
            if not retired:
                sharing = record.availability_resolution.get(
                    "realization_sharing"
                )
                if not isinstance(sharing, Mapping):
                    raise RealmIntegrityError(
                        "retained batch projection sharing differs."
                    )
                operation_coordinate = sharing.get(
                    "operation_coordinate_digest"
                )
                if not isinstance(operation_coordinate, str):
                    raise RealmIntegrityError(
                        "retained batch projection operation differs."
                    )
                key = request_digest(
                    {
                        "format": (
                            "optpilot.retained-batch-terminal-projection-cleanup.v1"
                        ),
                        "operation_id": operation_id,
                        "realization_id": record.realization_id,
                    }
                )
                cleaned = service.retire_private_projection_operation(
                    operation_id=(
                        "retained-batch.terminal.projection-retire/" + key
                    ),
                    actor_principal_id=self._actor_principal_id,
                    realization_id=record.realization_id,
                    expected_owner_id=binding.spec.owner_id,
                    expected_store_id=binding.store_id,
                    expected_spec=binding.spec,
                    expected_operation_coordinate_digest=operation_coordinate,
                    expected_consumer_holder_id=holder_id,
                    expected_consumer_kind="retained-batch-method",
                    expected_consumer_metadata=consumer_metadata,
                    ttl_seconds=_TERMINAL_CLEANUP_TTL_SECONDS,
                )
                if cleaned.state is not ProjectionRealizationState.CLEANED:
                    raise RealmIntegrityError(
                        "retained batch projection did not reconcile."
                    )

    def _validate_current_snapshot(
        self, snapshot: RunLedgerSnapshot
    ) -> RunLedgerSnapshot:
        run = snapshot.run
        if run.state != "running" or run.retention_state != "active":
            raise RealmConflict("run is not live.")
        current = self._ledger.read_run_snapshot(
            actor_principal_id=self._actor_principal_id,
            run_id=run.run_id,
        )
        expected_term = (
            snapshot.controller_term.generation,
            snapshot.controller_term.lease_id,
            snapshot.controller_term.holder_id,
            snapshot.controller_term.fencing_token,
        )
        actual_term = (
            current.controller_term.generation,
            current.controller_term.lease_id,
            current.controller_term.holder_id,
            current.controller_term.fencing_token,
        )
        if (
            current.run.run_id != run.run_id
            or current.run.owner_id != run.owner_id
            or actual_term != expected_term
            or current.definition.digest != snapshot.definition.digest
            or current.controller_lease.state is not LeaseState.ACTIVE
        ):
            raise RealmConflict("run controller term changed.")
        return current

    def _validate_current_inactive_snapshot(
        self, snapshot: RunLedgerSnapshot
    ) -> RunLedgerSnapshot:
        run = snapshot.run
        if run.retention_state != "active" or not self._method_is_inactive(snapshot):
            raise RealmConflict("run method is not canonically inactive.")
        current = self._ledger.read_run_snapshot(
            actor_principal_id=self._actor_principal_id,
            run_id=run.run_id,
        )
        expected_term = (
            snapshot.controller_term.generation,
            snapshot.controller_term.lease_id,
            snapshot.controller_term.holder_id,
            snapshot.controller_term.fencing_token,
        )
        actual_term = (
            current.controller_term.generation,
            current.controller_term.lease_id,
            current.controller_term.holder_id,
            current.controller_term.fencing_token,
        )
        expected_lease = (
            snapshot.controller_lease.lease_id,
            snapshot.controller_lease.holder_id,
            snapshot.controller_lease.fencing_token,
        )
        actual_lease = (
            current.controller_lease.lease_id,
            current.controller_lease.holder_id,
            current.controller_lease.fencing_token,
        )
        if (
            current.run.run_id != run.run_id
            or current.run.owner_id != run.owner_id
            or current.run.state != run.state
            or current.run.retention_state != "active"
            or current.run.current_revision != run.current_revision
            or actual_term != expected_term
            or actual_lease != expected_lease
            or current.definition.digest != snapshot.definition.digest
            or current.revision != snapshot.revision
            or not self._method_is_inactive(current)
        ):
            raise RealmConflict("inactive run controller term changed.")
        if (
            current.run.state == "running"
            and current.controller_lease.state is not LeaseState.ACTIVE
        ):
            raise RealmConflict("inactive running controller lease is not active.")
        return current

    @staticmethod
    def _method_is_inactive(snapshot: RunLedgerSnapshot) -> bool:
        if snapshot.run.state in {"succeeded", "failed", "cancelled"}:
            return snapshot.finalization is not None
        if snapshot.run.state != "running" or snapshot.finalization is not None:
            return False
        return method_feedback_obligations_resolved(snapshot)

    def _wait_until_socket_ready(
        self,
        *,
        socket_path: Path,
        reservation: ProcessLaunchReservation,
        timeout: float,
    ) -> _SocketIdentity:
        deadline = self._monotonic() + timeout
        while True:
            readiness = self._socket_probe(socket_path)
            if isinstance(readiness, _SocketIdentity):
                return readiness
            if readiness is True:
                # Boolean probes are retained as a narrow test seam; production
                # readiness returns the identity captured after the connect.
                return _capture_worker_socket_identity(socket_path)
            if readiness not in {False, None}:
                raise TypeError("socket_probe returned an unsupported value.")
            proof = self._supervisor.lookup_terminal_proof(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
            )
            if proof is not None:
                self._supervisor.retire_terminal(proof)
                raise _WorkerBecameTerminal
            now = self._monotonic()
            if now >= deadline:
                raise TimeoutError("retained batch worker socket readiness timed out.")
            self._sleep(min(0.02, max(0.0, deadline - now)))

    def _validate_terminal_proof(
        self,
        proof: WorkerTerminalProof,
        *,
        reservation: ProcessLaunchReservation,
        request: ProcessLaunchRequest,
    ) -> None:
        if (
            proof.launch_token != reservation.launch_token
            or proof.binding_id != reservation.binding_id
            or proof.evidence_fingerprint != reservation.evidence_fingerprint
            or proof.launch_request_digest != request.digest
        ):
            raise RealmIntegrityError(
                "retained batch terminal proof differs from its reservation."
            )
        self._supervisor.validate_terminal_proof(proof)

    def _cleanup_failed_realization(
        self,
        *,
        projection: ManagedReadOnlyProjection | None,
        volume: ManagedEphemeralVolume | None,
        candidate_volume: ManagedEphemeralVolume | None,
        reservation: ProcessLaunchReservation | None,
        socket_path: Path | None,
    ) -> None:
        safe_to_release = reservation is None
        if reservation is not None:
            try:
                reconciliation = self._supervisor.reconcile_terminal_launch(
                    launch_token=reservation.launch_token,
                    binding_id=reservation.binding_id,
                    evidence_fingerprint=reservation.evidence_fingerprint,
                    launch_request_digest=reservation.launch_request_digest,
                    grace_period=0.2,
                    timeout=5.0,
                )
                proof = reconciliation.proof
                if (
                    reconciliation.retired is not True
                    or reconciliation.endpoints_reconciled is not True
                    or reconciliation.launch_token != reservation.launch_token
                    or reconciliation.binding_id != reservation.binding_id
                    or reconciliation.evidence_fingerprint
                    != reservation.evidence_fingerprint
                    or reconciliation.launch_request_digest
                    != reservation.launch_request_digest
                    or proof.launch_token != reservation.launch_token
                    or proof.binding_id != reservation.binding_id
                    or proof.evidence_fingerprint
                    != reservation.evidence_fingerprint
                    or proof.launch_request_digest
                    != reservation.launch_request_digest
                ):
                    raise RealmIntegrityError(
                        "failed retained batch launch reconciliation differs."
                    )
                if socket_path is not None:
                    _require_socket_absent(socket_path)
                safe_to_release = True
            except Exception:
                safe_to_release = False
        if not safe_to_release:
            return
        if candidate_volume is not None:
            try:
                candidate_volume.close()
            except Exception:
                pass
        if volume is not None:
            try:
                volume.close()
            except Exception:
                pass
        if projection is not None:
            try:
                projection.close()
            except Exception:
                pass


class RetainedPythonBatchRuntime:
    """Live request handle for one exact supervised retained batch worker."""

    def __init__(
        self,
        *,
        identity: RetainedBatchRuntimeIdentity,
        projection: ManagedReadOnlyProjection,
        volume: ManagedEphemeralVolume,
        candidate_volume: ManagedEphemeralVolume | None,
        candidate_staging: FileCandidateStagingBinding | None,
        supervisor: LocalProcessSupervisor | None,
        process: SupervisedProcess | None,
        reservation: ProcessLaunchReservation | None,
        socket_path: Path | None,
        request_client: Callable[..., Mapping[str, Any]],
        request_timeout: float,
        attachment_kind: Literal["started", "attached"],
        container_process: Any | None = None,
    ) -> None:
        if attachment_kind not in {"started", "attached"}:
            raise ValueError("attachment_kind is unsupported.")
        # Exactly one of the two worker shapes: a supervised process with its
        # reservation and socket, or a held container with its stream.
        if (container_process is None) == (reservation is None):
            raise ValueError(
                "a runtime holds either a supervised process or a container."
            )
        self._container_process = container_process
        self.identity = identity
        self.attachment_kind = attachment_kind
        self._projection = projection
        self._volume = volume
        if (candidate_volume is None) != (candidate_staging is None):
            raise ValueError(
                "candidate staging volume and binding must be present together."
            )
        self._candidate_volume = candidate_volume
        self._candidate_staging = candidate_staging
        self._supervisor = supervisor
        self._process = process
        self._reservation = reservation
        self._socket_path = socket_path
        self._request_client = request_client
        self._request_timeout = request_timeout
        self._terminal_proof: WorkerTerminalProof | None = None
        self._closed = False
        self._stopping = False
        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._resource_lock = threading.RLock()
        self._termination_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def propose(
        self,
        exchange_id: str,
        *,
        exchange_sequence: int,
        n_candidates: int,
        study_state: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> RetainedBatchProposal:
        exchange = _exchange_id(exchange_id)
        if (
            isinstance(n_candidates, bool)
            or not isinstance(n_candidates, int)
            or not 1 <= n_candidates <= MAX_BATCH_EXCHANGE_ITEMS
        ):
            raise ValueError(
                "n_candidates must be within the durable batch item bound."
            )
        if not isinstance(study_state, Mapping) or not isinstance(evidence, Mapping):
            raise TypeError("study_state and evidence must be mappings.")
        sequence = _positive_int(exchange_sequence, "exchange_sequence")
        payload = {
            "evidence": _json_copy(evidence, "evidence"),
            "n_candidates": n_candidates,
            "study_state": _json_copy(study_state, "study_state"),
        }
        request_digest_value = retained_batch_worker_request_digest(
            "propose", payload
        )
        response = self._request(
            exchange,
            "propose",
            payload,
            exchange_sequence=sequence,
        )
        result = _exact_result(
            response,
            {"candidates"},
            exchange_id=exchange,
            operation="propose",
            exchange_sequence=sequence,
            request_digest_value=request_digest_value,
        )
        candidates = result["candidates"]
        if not isinstance(candidates, tuple) or any(
            not isinstance(candidate, Mapping) for candidate in candidates
        ) or len(candidates) > n_candidates:
            raise _protocol_response_error(
                response,
                exchange_id=exchange,
                operation="propose",
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
            )
        try:
            if self._candidate_staging is not None:
                drafts = tuple(
                    FileCandidateDraft.from_candidate(candidate)
                    for candidate in candidates
                )
                for ordinal, draft in enumerate(drafts):
                    if draft.draft.selection != (
                        f"candidate-{ordinal:08d}/files"
                    ):
                        raise ValueError("file-candidate draft selection differs.")
            elif any(candidate.get("format") == "files" for candidate in candidates):
                raise ValueError("file-candidate staging is absent.")
        except (KeyError, TypeError, ValueError, RecursionError):
            raise _protocol_response_error(
                response,
                exchange_id=exchange,
                operation="propose",
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
            ) from None
        return RetainedBatchProposal(
            exchange,
            sequence,
            tuple(candidates),
            request_digest_value,
            response.response_digest,
        )

    def resolve_file_candidate_source(
        self,
        *,
        exchange_id: str,
        exchange_sequence: int,
        ordinal: int,
        candidate: Mapping[str, Any] | FileCandidateDraft,
    ) -> AllowedTreeSource:
        """Authenticate one frozen draft and return its provider-private source."""

        exchange = _exchange_id(exchange_id)
        sequence = _positive_int(exchange_sequence, "exchange_sequence")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal < MAX_BATCH_EXCHANGE_ITEMS
        ):
            raise ValueError("file-candidate ordinal is outside the durable bound.")
        draft = (
            candidate
            if isinstance(candidate, FileCandidateDraft)
            else FileCandidateDraft.from_candidate(candidate)
        )
        expected_selection = f"candidate-{ordinal:08d}/files"
        if draft.draft.selection != expected_selection:
            raise ValueError("file-candidate draft selection differs.")
        with self._resource_lock:
            with self._state_lock:
                self._ensure_live_unlocked()
            root, binding = self._authenticated_candidate_staging()
            expected_token = file_candidate_draft_token(
                binding=binding,
                exchange_id=exchange,
                exchange_sequence=sequence,
                ordinal=ordinal,
                declaration_digest=draft.declaration_digest,
            )
            if draft.draft.token != expected_token:
                raise ValueError("file-candidate draft token differs.")
            coordinate = candidate_staging_exchange_coordinate(
                run_id=self.identity.run_id,
                exchange_id=exchange,
                exchange_sequence=sequence,
            )
            frozen_exchange = _candidate_frozen_exchange(root, coordinate)
            # Validate the volume authority and namespace after resolving the
            # selection as well; callers repeat this method after sealing to
            # close the complete capture race.
            self._authenticated_candidate_staging()
            return AllowedTreeSource(frozen_exchange, expected_selection)

    def cleanup_file_candidate_exchange(
        self, *, exchange_id: str, exchange_sequence: int
    ) -> None:
        """Delete one exact accepted/rejected staging exchange idempotently."""

        exchange = _exchange_id(exchange_id)
        sequence = _positive_int(exchange_sequence, "exchange_sequence")
        with self._resource_lock:
            with self._state_lock:
                self._ensure_live_unlocked()
            root, _binding = self._authenticated_candidate_staging()
            coordinate = candidate_staging_exchange_coordinate(
                run_id=self.identity.run_id,
                exchange_id=exchange,
                exchange_sequence=sequence,
            )
            for phase in ("active", "frozen"):
                _remove_candidate_staging_exchange(root, phase, coordinate)
            self._authenticated_candidate_staging()

    def _authenticated_candidate_staging(
        self,
    ) -> tuple[Path, FileCandidateStagingBinding]:
        volume = self._candidate_volume
        binding = self._candidate_staging
        if volume is None or binding is None:
            raise ValueError("file-candidate staging is unavailable.")
        root = volume.path.resolve(strict=True)
        record = volume.record
        lease = volume.lease
        if (
            binding.run_id != self.identity.run_id
            or binding.controller_generation
            != self.identity.controller_generation
            or binding.volume_id != record.volume_id
            or binding.usage_lease_id != lease.lease_id
            or binding.usage_fencing_token != lease.fencing_token
            or binding.root_path != str(root)
        ):
            raise ValueError("file-candidate staging authority differs.")
        _validate_candidate_staging_root(root)
        return root, binding

    def request(
        self,
        exchange_id: str,
        operation: Literal["propose", "observe", "ack", "status"],
        payload: Mapping[str, Any],
        *,
        exchange_sequence: int | None = None,
    ) -> RetainedBatchWorkerResponse:
        """Return one complete validated canonical success response.

        Canonical ``ok:false`` responses raise :class:`RetainedBatchMethodError`.
        That exception retains the stable worker error fields and the SHA-256
        digest of the complete canonical response.  Bounded canonical responses
        that violate the typed shape raise :class:`RetainedBatchProtocolError`
        with both request and response coordinates.  Provider/transport failures
        instead raise :class:`RetainedBatchRuntimeError`, so a driver never has
        to manufacture a method result for an exchange that produced none.
        """

        exchange = _exchange_id(exchange_id)
        if operation not in {"propose", "observe", "ack", "status"}:
            raise ValueError("retained batch operation is unsupported.")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping.")
        if operation in {"propose", "observe"}:
            sequence = _positive_int(exchange_sequence, "exchange_sequence")
        elif exchange_sequence is not None:
            raise ValueError(
                "exchange_sequence is only valid for propose or observe."
            )
        else:
            sequence = None
        return self._request(
            exchange,
            operation,
            _json_copy(payload, "request payload"),
            exchange_sequence=sequence,
        )

    def observe(
        self,
        exchange_id: str,
        *,
        exchange_sequence: int,
        observations: Sequence[Mapping[str, Any]],
    ) -> RetainedBatchObservationAck:
        exchange = _exchange_id(exchange_id)
        if isinstance(observations, (str, bytes)) or not isinstance(
            observations, Sequence
        ):
            raise TypeError("observations must be a sequence of mappings.")
        values = []
        for observation in observations:
            if not isinstance(observation, Mapping):
                raise TypeError("observations must contain mappings.")
            values.append(_json_copy(observation, "observation"))
        if not 1 <= len(values) <= MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError(
                "observations must be within the durable batch item bound."
            )
        sequence = _positive_int(exchange_sequence, "exchange_sequence")
        payload = {"observations": values}
        request_digest_value = retained_batch_worker_request_digest(
            "observe", payload
        )
        response = self._request(
            exchange,
            "observe",
            payload,
            exchange_sequence=sequence,
        )
        result = _exact_result(
            response,
            {"observation_count"},
            exchange_id=exchange,
            operation="observe",
            exchange_sequence=sequence,
            request_digest_value=request_digest_value,
        )
        count = result["observation_count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(values)
        ):
            raise _protocol_response_error(
                response,
                exchange_id=exchange,
                operation="observe",
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
            )
        return RetainedBatchObservationAck(
            exchange,
            sequence,
            count,
            request_digest_value,
            response.response_digest,
        )

    def ack(
        self,
        request_exchange_id: str,
        *,
        exchange: RetainedBatchExchangeCoordinate,
        previous_acknowledged_chain: str,
    ) -> RetainedBatchCacheAck:
        request_id = _exchange_id(request_exchange_id)
        if not isinstance(exchange, RetainedBatchExchangeCoordinate):
            raise TypeError("exchange must be a RetainedBatchExchangeCoordinate.")
        previous_chain = _lower_digest(
            previous_acknowledged_chain, "previous acknowledged chain"
        )
        response = self._request(
            request_id, "ack", {"exchange": exchange.to_dict()}
        )
        result = _exact_result(
            response,
            {
                "acknowledged_chain",
                "acknowledged_exchange",
                "acknowledged_sequence",
            },
            exchange_id=request_id,
            operation="ack",
        )
        expected_chain = retained_batch_exchange_chain_digest(
            previous_chain,
            exchange_id=exchange.exchange_id,
            exchange_sequence=exchange.exchange_sequence,
            request_digest_value=exchange.request_digest,
            response_digest=exchange.response_digest,
        )
        if (
            result["acknowledged_sequence"] != exchange.exchange_sequence
            or result["acknowledged_chain"] != expected_chain
            or result["acknowledged_exchange"] != exchange.to_dict()
        ):
            raise _protocol_response_error(
                response,
                exchange_id=request_id,
                operation="ack",
            )
        return RetainedBatchCacheAck(
            request_exchange_id=request_id,
            acknowledged_sequence=exchange.exchange_sequence,
            acknowledged_chain=expected_chain,
            acknowledged_exchange=exchange,
            response_digest=response.response_digest,
        )

    def status(self, request_exchange_id: str) -> RetainedBatchWorkerStatus:
        request_id = _exchange_id(request_exchange_id)
        response = self._request(request_id, "status", {})
        result = _exact_result(
            response,
            {
                "acknowledged_chain",
                "acknowledged_sequence",
                "pending_exchange",
                "pending_response_bytes",
            },
            exchange_id=request_id,
            operation="status",
        )
        pending_value = result["pending_exchange"]
        try:
            pending = (
                None
                if pending_value is None
                else RetainedBatchExchangeCoordinate(**pending_value)
            )
            return RetainedBatchWorkerStatus(
                request_exchange_id=request_id,
                acknowledged_sequence=result["acknowledged_sequence"],
                acknowledged_chain=result["acknowledged_chain"],
                pending_exchange=pending,
                pending_response_bytes=result["pending_response_bytes"],
                response_digest=response.response_digest,
            )
        except (KeyError, TypeError, ValueError):
            raise _protocol_response_error(
                response,
                exchange_id=request_id,
                operation="status",
            ) from None

    def heartbeat(
        self, *, operation_id: str, ttl_seconds: float
    ) -> RetainedBatchRuntimeHeartbeat:
        operation = _required_text(operation_id, "operation_id")
        ttl = _positive_finite(ttl_seconds, "ttl_seconds")
        # Resource renewal is independent of the serialized method callback
        # socket.  A long propose/observe call therefore cannot starve its
        # projection or control-volume leases.  Cleanup uses this same lock so
        # release cannot race an in-flight heartbeat.
        with self._resource_lock:
            with self._state_lock:
                self._ensure_live_unlocked()
            try:
                projection_lease = self._projection.heartbeat(
                    operation_id=f"{operation}/projection", ttl_seconds=ttl
                )
                volume_lease = self._volume.heartbeat(
                    operation_id=f"{operation}/volume", ttl_seconds=ttl
                )
                if self._candidate_volume is not None:
                    self._candidate_volume.heartbeat(
                        operation_id=f"{operation}/candidate-staging-volume",
                        ttl_seconds=ttl,
                    )
            except Exception:
                raise RetainedBatchRuntimeError("heartbeat_failed") from None
            if not isinstance(projection_lease, LeaseRecord) or not isinstance(
                volume_lease, LeaseRecord
            ):
                raise RetainedBatchRuntimeError("heartbeat_failed")
            return RetainedBatchRuntimeHeartbeat(
                projection_lease_id=projection_lease.lease_id,
                projection_expires_at=projection_lease.expires_at,
                volume_lease_id=volume_lease.lease_id,
                volume_expires_at=volume_lease.expires_at,
            )

    def shutdown(
        self,
        *,
        grace_period: float = 1.0,
        timeout: float = 10.0,
    ) -> None:
        """Request protocol shutdown, force-stop on timeout, then clean up."""

        self._terminate(
            orderly=True, grace_period=grace_period, timeout=timeout
        )

    def force_stop(
        self,
        *,
        grace_period: float = 1.0,
        timeout: float = 10.0,
    ) -> None:
        """Stop the authenticated process group, retire it, then clean up."""

        self._terminate(
            orderly=False, grace_period=grace_period, timeout=timeout
        )

    def close(self) -> None:
        self.shutdown()

    def _request(
        self,
        exchange_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        exchange_sequence: int | None = None,
    ) -> RetainedBatchWorkerResponse:
        # Only worker protocol calls serialize with each other.  Neither
        # heartbeat nor forced cancellation waits for this lock.
        with self._request_lock:
            with self._state_lock:
                self._ensure_live_unlocked()
            return self._request_locked(
                exchange_id,
                operation,
                payload,
                exchange_sequence=exchange_sequence,
            )

    def _request_locked(
        self,
        exchange_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        exchange_sequence: int | None = None,
        allow_stopping: bool = False,
        timeout_override: float | None = None,
    ) -> RetainedBatchWorkerResponse:
        request = {
            "exchange_id": exchange_id,
            "op": operation,
            "payload": dict(payload),
            "schema": BATCH_REQUEST_SCHEMA,
        }
        if operation in {"propose", "observe"}:
            request["exchange_sequence"] = _positive_int(
                exchange_sequence, "exchange_sequence"
            )
        elif exchange_sequence is not None:
            raise ValueError(
                "exchange_sequence is only valid for propose or observe."
            )
        try:
            response = self._request_client(
                self._socket_path,
                request,
                timeout=(
                    self._request_timeout
                    if timeout_override is None
                    else timeout_override
                ),
            )
        except Exception as error:
            proof = self._lookup_terminal_proof()
            if proof is not None:
                with self._state_lock:
                    self._terminal_proof = proof
                try:
                    self._supervisor.retire_terminal(proof)
                except Exception:
                    pass
                raise RetainedBatchRuntimeError("worker_terminal") from None
            # Preserve an explicit configured-deadline outcome.  A generic
            # transport loss remains recoverable, while a socket/request
            # timeout tells the retained driver that reissuing this exact
            # callback under successive controller terms would only loop.
            if isinstance(error, TimeoutError):
                raise RetainedBatchRuntimeError(
                    "worker_request_timeout"
                ) from None
            raise RetainedBatchRuntimeError("worker_unavailable") from None
        if not isinstance(response, Mapping):
            raise RetainedBatchRuntimeError("worker_protocol_error")
        try:
            response_bytes = canonical_json_bytes(response)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise RetainedBatchRuntimeError("worker_protocol_error") from None
        response_digest = durable_worker_response_digest(response)

        def protocol_error() -> RetainedBatchProtocolError:
            request_digest_value = (
                retained_batch_worker_request_digest(operation, payload)
                if operation in {"propose", "observe"}
                else None
            )
            return RetainedBatchProtocolError(
                exchange_id=exchange_id,
                operation=operation,
                exchange_sequence=exchange_sequence,
                request_digest=request_digest_value,
                response_digest=response_digest,
            )

        if len(response_bytes) > MAX_BATCH_DURABLE_RESPONSE_BYTES:
            raise protocol_error()
        response = json.loads(response_bytes.decode("utf-8"))
        with self._state_lock:
            if (
                self._closed
                or self._terminal_proof is not None
                or (self._stopping and not allow_stopping)
            ):
                raise RetainedBatchRuntimeError("worker_terminal")
        expected_common = {"exchange_id", "ok", "schema"}
        if response.get("schema") != BATCH_RESPONSE_SCHEMA or response.get(
            "exchange_id"
        ) != exchange_id:
            raise protocol_error()
        if response.get("ok") is True:
            if set(response) != expected_common | {"result"} or not isinstance(
                response.get("result"), Mapping
            ):
                raise protocol_error()
            try:
                return RetainedBatchWorkerResponse(response, response_digest)
            except (TypeError, ValueError, RecursionError):
                raise protocol_error() from None
        if response.get("ok") is not False or set(response) != expected_common | {
            "error"
        }:
            raise protocol_error()
        error = response.get("error")
        if not isinstance(error, Mapping) or set(error) != {
            "code",
            "diagnostic_id",
            "message",
        }:
            raise protocol_error()
        try:
            raise RetainedBatchMethodError(
                code=error["code"],
                message=error["message"],
                diagnostic_id=error["diagnostic_id"],
                response_digest=response_digest,
                cause=self._recorded_cause(error.get("diagnostic_id")),
            )
        except (KeyError, TypeError, ValueError):
            raise protocol_error() from None

    def _recorded_cause(self, diagnostic_id: Any) -> Mapping[str, Any] | None:
        """Read one private diagnostic back before its volume is erased.

        Every failure path here returns None: a method failure must be
        reported as a method failure even when its explanation cannot be
        recovered, and never as a protocol error.
        """

        if not isinstance(diagnostic_id, str) or not diagnostic_id:
            return None
        try:
            path = self._volume.path / _DIAGNOSTIC_FILE
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        # The sink doubles as the method's redirected stdout,
                        # so unparseable lines are expected, not an error.
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(event, Mapping)
                        and event.get("diagnostic_id") == diagnostic_id
                    ):
                        return {
                            "type": event.get("exception_type"),
                            "message": event.get("message"),
                        }
        except Exception:
            return None
        return None

    def _lookup_terminal_proof(self) -> WorkerTerminalProof | None:
        if self._reservation is None:
            # A container worker leaves no terminal proof: its death is its
            # exit status, already surfaced by the exchange that noticed.
            return None
        try:
            proof = self._supervisor.lookup_terminal_proof(
                launch_token=self._reservation.launch_token,
                binding_id=self._reservation.binding_id,
                evidence_fingerprint=self._reservation.evidence_fingerprint,
                launch_request_digest=self._reservation.launch_request_digest,
            )
        except Exception:
            return None
        if proof is None:
            return None
        if (
            proof.launch_token != self._reservation.launch_token
            or proof.binding_id != self._reservation.binding_id
            or proof.evidence_fingerprint != self._reservation.evidence_fingerprint
            or proof.launch_request_digest
            != self._reservation.launch_request_digest
        ):
            return None
        return proof

    def _terminate(
        self, *, orderly: bool, grace_period: float, timeout: float
    ) -> None:
        grace = _nonnegative_finite(grace_period, "grace_period")
        wait = _positive_finite(timeout, "timeout")
        if self._container_process is not None:
            return self._terminate_container(
                orderly=orderly, grace=grace, wait=wait
            )
        with self._termination_lock:
            with self._state_lock:
                if self._closed:
                    return
                proof = self._terminal_proof
                self._stopping = True

            try:
                if proof is None and orderly:
                    shutdown_id = f"shutdown-{self.identity.launch_token[-40:]}"
                    # Never queue shutdown behind a long-running callback.  If
                    # the request lane is occupied, authenticated process-group
                    # stop is the bounded cancellation path.
                    acquired = self._request_lock.acquire(blocking=False)
                    if acquired:
                        try:
                            response = self._request_locked(
                                shutdown_id,
                                "shutdown",
                                {},
                                allow_stopping=True,
                                timeout_override=min(
                                    self._request_timeout, wait
                                ),
                            )
                            result = _exact_result(
                                response,
                                {"shutdown"},
                                exchange_id=shutdown_id,
                                operation="shutdown",
                            )
                            if result["shutdown"] is not True:
                                raise _protocol_response_error(
                                    response,
                                    exchange_id=shutdown_id,
                                    operation="shutdown",
                                )
                            try:
                                proof = self._process.wait(timeout=wait)
                            except TimeoutError:
                                proof = None
                        except (
                            RetainedBatchRuntimeError,
                            RetainedBatchMethodError,
                            RetainedBatchProtocolError,
                        ):
                            with self._state_lock:
                                proof = self._terminal_proof
                        finally:
                            self._request_lock.release()
                if proof is None:
                    proof = self._process.stop(
                        grace_period=grace, timeout=wait
                    )
            except Exception:
                with self._state_lock:
                    if self._terminal_proof is None:
                        self._stopping = False
                raise RetainedBatchRuntimeError("cleanup_failed") from None

            if (
                proof.launch_token != self._reservation.launch_token
                or proof.binding_id != self._reservation.binding_id
                or proof.evidence_fingerprint
                != self._reservation.evidence_fingerprint
                or proof.launch_request_digest
                != self._reservation.launch_request_digest
            ):
                with self._state_lock:
                    self._stopping = False
                raise RetainedBatchRuntimeError("cleanup_failed")
            with self._state_lock:
                self._terminal_proof = proof

            # A heartbeat that entered before ``_stopping`` was published may
            # finish, but final resource release waits for it here.  Requests
            # never acquire this lock and cannot delay cancellation.
            with self._resource_lock:
                failed = False
                try:
                    reconciliation = self._supervisor.reconcile_terminal_launch(
                        launch_token=self._reservation.launch_token,
                        binding_id=self._reservation.binding_id,
                        evidence_fingerprint=(
                            self._reservation.evidence_fingerprint
                        ),
                        launch_request_digest=(
                            self._reservation.launch_request_digest
                        ),
                        grace_period=grace,
                        timeout=wait,
                    )
                    if (
                        reconciliation.retired is not True
                        or reconciliation.endpoints_reconciled is not True
                        or reconciliation.proof != proof
                        or reconciliation.launch_token
                        != self._reservation.launch_token
                        or reconciliation.binding_id
                        != self._reservation.binding_id
                        or reconciliation.evidence_fingerprint
                        != self._reservation.evidence_fingerprint
                        or reconciliation.launch_request_digest
                        != self._reservation.launch_request_digest
                    ):
                        raise RealmIntegrityError(
                            "retained batch live reconciliation differs."
                        )
                    _require_socket_absent(self._socket_path)
                except Exception:
                    failed = True
                try:
                    if self._candidate_volume is not None:
                        self._candidate_volume.close()
                except Exception:
                    failed = True
                try:
                    self._volume.close()
                except Exception:
                    failed = True
                try:
                    self._projection.close()
                except Exception:
                    failed = True
            if failed:
                raise RetainedBatchRuntimeError("cleanup_failed")
            with self._state_lock:
                self._closed = True

    def _terminate_container(
        self, *, orderly: bool, grace: float, wait: float
    ) -> None:
        """End the held container and release what it was using.

        The polite path asks the worker to shut down over its own stream, so
        state is closed by the code that owns it; the stop that follows covers
        a worker wedged inside authored code. There are no terminal proofs to
        validate -- the container's exit status is its account of itself.
        """

        with self._termination_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._stopping = True
            try:
                if orderly:
                    acquired = self._request_lock.acquire(blocking=False)
                    if acquired:
                        try:
                            self._request_locked(
                                f"shutdown-{self.identity.launch_token[-40:]}",
                                "shutdown",
                                {},
                                allow_stopping=True,
                                timeout_override=min(self._request_timeout, wait),
                            )
                        except (
                            RetainedBatchRuntimeError,
                            RetainedBatchMethodError,
                            RetainedBatchProtocolError,
                        ):
                            pass
                        finally:
                            self._request_lock.release()
                self._container_process.stop(grace_seconds=max(grace, 1.0))
            except Exception:
                with self._state_lock:
                    self._stopping = False
                raise RetainedBatchRuntimeError("cleanup_failed") from None
            with self._resource_lock:
                failed = False
                try:
                    if self._candidate_volume is not None:
                        self._candidate_volume.close()
                except Exception:
                    failed = True
                try:
                    self._volume.close()
                except Exception:
                    failed = True
                try:
                    self._projection.close()
                except Exception:
                    failed = True
            if failed:
                raise RetainedBatchRuntimeError("cleanup_failed")
            with self._state_lock:
                self._closed = True

    def _ensure_live_unlocked(self) -> None:
        if self._closed or self._terminal_proof is not None or self._stopping:
            raise RetainedBatchRuntimeError("runtime_closed")

    def __enter__(self) -> "RetainedPythonBatchRuntime":
        with self._state_lock:
            self._ensure_live_unlocked()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class _WorkerCoordinates:
    coordinate: str
    run_id: str
    controller_generation: int
    launch_token: str
    binding_id: str


class _WorkerBecameTerminal(Exception):
    pass


def _method_container_limits(raised: Mapping[str, Any]):
    """Defaults, with only the limits the component raised replaced."""

    from .container_launch import ContainerLimits

    defaults = ContainerLimits()
    return ContainerLimits(
        cpus=str(raised.get("cpus", defaults.cpus)),
        memory=str(raised.get("memory", defaults.memory)),
        pids=int(raised.get("pids", defaults.pids)),
    )


def _worker_coordinates(
    *,
    realm_id: str,
    run_id: str,
    controller_generation: int,
    run_definition_digest: str,
) -> _WorkerCoordinates:
    """Return the generation coordinate; the semantic digest is evidence only.

    A run definition is immutable, so it is intentionally excluded from the
    launch token.  If corrupted recovery presents changed semantics for the
    same generation, the stable token converges on the supervisor's exact
    request replay check instead of selecting a second worker.
    """

    _required_text(realm_id, "realm_id")
    selected_run = _required_text(run_id, "run_id")
    generation = _positive_int(controller_generation, "controller_generation")
    _lower_digest(run_definition_digest, "run_definition_digest")
    coordinate = request_digest(
        {
            "controller_generation": generation,
            "format": _RUNTIME_ID_FORMAT,
            "realm_id": realm_id,
            "run_id": selected_run,
        }
    )
    return _WorkerCoordinates(
        coordinate=coordinate,
        run_id=selected_run,
        controller_generation=generation,
        launch_token=f"method-worker-{coordinate[:48]}",
        binding_id=f"method-worker-binding-{coordinate[:40]}",
    )


def _build_projection_binding(
    *,
    definition: RunDefinitionManifest,
    owner_id: str,
    memberships: Sequence[OwnerMembership],
    available_store_ids: Sequence[str],
) -> _ProjectionBinding:
    required_scopes = _required_method_scopes(definition)
    method_layers: list[tuple[str, ScopeLayer]] = [
        (RUN_METHOD_SOURCE_ROLE, layer)
        for layer in definition.method_revision.source_layers
        if layer.scope in required_scopes
    ]
    method_layers.extend(
        (RUN_PREPARED_METHOD_RUNTIME_ROLE, layer)
        for layer in definition.prepared_method_runtime.prepared_layers
        if layer.scope in required_scopes
    )
    if {layer.scope for _, layer in method_layers} != required_scopes:
        raise RealmConflict("retained method scope content is incomplete.")
    if isinstance(memberships, (str, bytes)) or not isinstance(
        memberships, Sequence
    ):
        raise TypeError("memberships must be a sequence.")
    membership_values = tuple(memberships)
    if any(not isinstance(value, OwnerMembership) for value in membership_values):
        raise TypeError("memberships must contain OwnerMembership values.")
    membership_index: dict[tuple[str, object], list[OwnerMembership]] = {}
    for membership in membership_values:
        membership_index.setdefault(
            (membership.role, membership.content_ref), []
        ).append(membership)
    stores: set[str] = set()
    for role, layer in method_layers:
        placements = membership_index.get((role, layer.snapshot_ref), [])
        if len(placements) != 1:
            raise RealmNotFound("Entity not found.")
        stores.add(placements[0].store_id)
    if len(stores) != 1:
        raise RealmConflict(
            "retained method layers require one local projection store."
        )
    store_id = next(iter(stores))
    if store_id not in set(available_store_ids):
        raise RealmNotFound("Entity not found.")

    scope_roots = {
        scope: f"scopes/{scope}"
        for scope in sorted(required_scopes, key=lambda value: value.encode("ascii"))
    }
    context_binding = _method_context_binding(definition)
    if context_binding is not None:
        logical_scope, source_scope, source_path = context_binding
        scope_roots[logical_scope] = _join_portable(
            scope_roots[source_scope], source_path
        )
    ordered_layers = sorted(
        method_layers,
        key=lambda item: (
            item[1].scope.encode("ascii"),
            item[1].precedence,
            item[1].destination_subpath.encode("utf-8"),
            item[1].source_subpath.encode("utf-8"),
            str(item[1].snapshot_ref),
            item[0].encode("ascii"),
        ),
    )
    mappings = tuple(
        TreeMapping(
            snapshot_ref=layer.snapshot_ref,
            source_subpath=layer.source_subpath,
            destination=_join_portable(
                scope_roots[layer.scope], layer.destination_subpath
            ),
        )
        for _role, layer in ordered_layers
    )
    spec = ProjectionSpec(owner_id=owner_id, mappings=mappings)
    # Bind the exact scope layout into one additional semantic digest check.
    request_digest(
        {
            "format": _PROJECTION_FORMAT,
            "scope_roots": scope_roots,
            "spec": spec.to_dict(),
        }
    )
    return _ProjectionBinding(
        store_id=store_id,
        spec=spec,
        scope_roots=MappingProxyType(scope_roots),
    )


def _required_method_scopes(definition: RunDefinitionManifest) -> set[str]:
    runtime = definition.prepared_method_runtime
    if runtime.workdir is None:
        raise ValueError("retained method workdir is absent.")
    settings = runtime.runtime_settings
    expected_keys = {"import_roots", "schema"}
    if runtime.runtime_kind == "container":
        # The same three container facts the worker itself expects; the two
        # exact key sets are defined beside each other there.
        expected_keys = expected_keys | {
            "container_platform",
            "container_image_reference",
            "container_network",
        }
    observed_keys = set(settings) if isinstance(settings, Mapping) else set()
    if runtime.runtime_kind == "container":
        # Present only when the component raised a resource limit.
        observed_keys.discard("container_limits")
    if not isinstance(settings, Mapping) or observed_keys != expected_keys:
        raise ValueError("retained method runtime settings are unsupported.")
    if settings.get("schema") != "optpilot.logical-python-process-runtime-settings.v1":
        raise ValueError("retained method runtime settings schema is unsupported.")
    values = settings.get("import_roots")
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("retained method import roots are absent.")
    roots = tuple(ScopePath.from_dict(value) for value in values)
    if len(set(roots)) != len(roots):
        raise ValueError("retained method import roots contain duplicates.")
    return {
        definition.method_revision.authored_config.scope,
        runtime.workdir.scope,
        *(value.scope for value in roots),
    }


def _method_context_binding(
    definition: RunDefinitionManifest,
) -> tuple[str, str, str] | None:
    environment = definition.evaluation_closure.environment_revision
    contract = environment.projection_contract
    try:
        features = package_projection_contract_features(contract)
    except (TypeError, ValueError) as error:
        raise ValueError("retained method-context projection contract is unsupported.")
    if "method_context" not in features:
        return None
    binding = contract.get("method_context")
    if not isinstance(binding, Mapping) or set(binding) != {
        "logical_scope",
        "source",
    }:
        raise ValueError("retained method-context projection contract is invalid.")
    source = binding.get("source")
    if not isinstance(source, Mapping) or set(source) != {"path", "scope"}:
        raise ValueError("retained method-context projection source is invalid.")
    logical_scope = binding.get("logical_scope")
    source_scope = source.get("scope")
    source_path = source.get("path")
    if logical_scope != METHOD_CONTEXT_SCOPE:
        raise ValueError("retained method-context logical scope is unsupported.")
    if not isinstance(source_scope, str) or not isinstance(source_path, str):
        raise ValueError("retained method-context projection source is invalid.")
    if source_path != ".":
        parsed_source_path = PurePosixPath(source_path)
        if (
            parsed_source_path.is_absolute()
            or str(parsed_source_path) != source_path
            or any(part in {"", ".", ".."} for part in parsed_source_path.parts)
        ):
            raise ValueError("retained method-context source path is invalid.")
    method_sources = tuple(
        layer
        for layer in definition.method_revision.source_layers
        if layer.scope == source_scope
        and layer.source_subpath == "."
        and layer.destination_subpath == "."
    )
    environment_sources = tuple(
        layer
        for layer in environment.source_layers
        if layer.scope == source_scope
        and layer.source_subpath == "."
        and layer.destination_subpath == "."
    )
    if (
        len(method_sources) != 1
        or len(environment_sources) != 1
        or method_sources[0].snapshot_ref != environment_sources[0].snapshot_ref
    ):
        raise ValueError(
            "retained method context is not anchored to the shared package source."
        )
    required_scopes = _required_method_scopes(definition)
    if source_scope not in required_scopes or logical_scope in required_scopes:
        raise ValueError("retained method-context scope aliases are invalid.")
    return logical_scope, source_scope, source_path


def _validate_batch_definition(definition: RunDefinitionManifest) -> None:
    if not isinstance(definition, RunDefinitionManifest):
        raise TypeError("run definition must be a RunDefinitionManifest.")
    method_runtime = definition.prepared_method_runtime
    if (
        definition.method_revision.protocol != BATCH_PROTOCOL
        or method_runtime.runtime_kind not in ("process", "container")
    ):
        raise ValueError("retained method protocol/runtime is unsupported.")
    if method_runtime.runtime_kind == "container":
        settings = method_runtime.runtime_settings
        reference = (
            settings.get("container_image_reference")
            if isinstance(settings, Mapping)
            else None
        )
        platform = (
            settings.get("container_platform")
            if isinstance(settings, Mapping)
            else None
        )
        network = (
            settings.get("container_network")
            if isinstance(settings, Mapping)
            else None
        )
        if (
            method_runtime.portability != "portable"
            or not isinstance(reference, str)
            or not reference
            or not isinstance(platform, str)
            or not platform
            or network not in ("enabled", "disabled")
        ):
            raise ValueError("retained container method runtime is malformed.")
    implementation = definition.method_revision.method_contract.get("implementation")
    if not isinstance(implementation, Mapping) or implementation.get("type") not in {
        "python",
        "command",
    }:
        raise ValueError("retained method implementation is unsupported.")
    _definition_candidate_format(definition)
    _required_method_scopes(definition)
    _method_context_binding(definition)


def _definition_candidate_format(definition: RunDefinitionManifest) -> str:
    contract = definition.evaluation_closure.environment_revision.candidate_contract
    if not isinstance(contract, Mapping) or contract.get("format") not in {
        "parameters",
        "files",
    }:
        raise ValueError("retained candidate contract format is unsupported.")
    return contract["format"]


_RUNTIME_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _prepare_candidate_staging_root(root: Path) -> Path:
    """Create the two provider-owned phase directories before worker start."""

    root = Path(root)
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise RealmIntegrityError(
            "retained file-candidate staging root is unavailable."
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RealmIntegrityError(
            "retained file-candidate staging root is invalid."
        )
    root_fd = os.open(resolved, _RUNTIME_DIRECTORY_FLAGS)
    try:
        for name in ("active", "frozen"):
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            child = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(child.st_mode):
                raise RealmIntegrityError(
                    "retained file-candidate staging phase is invalid."
                )
        if set(os.listdir(root_fd)) != {"active", "frozen"}:
            raise RealmIntegrityError(
                "retained file-candidate staging namespace differs."
            )
    except OSError as error:
        raise RealmIntegrityError(
            "retained file-candidate staging namespace is unavailable."
        ) from error
    finally:
        os.close(root_fd)
    return resolved


def _validate_candidate_staging_root(root: Path) -> None:
    root = Path(root)
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise RealmIntegrityError(
            "retained file-candidate staging root is unavailable."
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != root
    ):
        raise RealmIntegrityError(
            "retained file-candidate staging root identity differs."
        )
    for phase in ("active", "frozen"):
        _candidate_staging_phase(root, phase)


def _candidate_staging_phase(root: Path, phase: str) -> Path:
    if phase not in {"active", "frozen"}:
        raise ValueError("file-candidate staging phase is unsupported.")
    path = root / phase
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RealmIntegrityError(
            "retained file-candidate staging phase is unavailable."
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved.parent != root
        or resolved != path
    ):
        raise RealmIntegrityError(
            "retained file-candidate staging phase identity differs."
        )
    return resolved


def _candidate_frozen_exchange(root: Path, coordinate: str) -> Path:
    if not isinstance(coordinate, str) or not coordinate.startswith("exchange-"):
        raise ValueError("file-candidate exchange coordinate is invalid.")
    active = _candidate_staging_phase(root, "active")
    frozen = _candidate_staging_phase(root, "frozen")
    if os.path.lexists(active / coordinate):
        raise ValueError("file-candidate exchange is not frozen.")
    exchange = frozen / coordinate
    try:
        metadata = exchange.lstat()
        resolved = exchange.resolve(strict=True)
    except OSError as error:
        raise ValueError("file-candidate frozen exchange is unavailable.") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != exchange
        or resolved.parent != frozen
    ):
        raise ValueError("file-candidate frozen exchange identity differs.")
    return resolved


def _remove_candidate_staging_exchange(
    root: Path, phase: str, coordinate: str
) -> None:
    parent = _candidate_staging_phase(root, phase)
    parent_fd = os.open(parent, _RUNTIME_DIRECTORY_FLAGS)
    tombstone = f".cleanup-{coordinate}"
    try:
        _remove_staging_entry(parent_fd, tombstone, missing_ok=True)
        try:
            os.rename(
                coordinate,
                tombstone,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return
        _remove_staging_entry(parent_fd, tombstone, missing_ok=False)
    finally:
        os.close(parent_fd)


def _remove_staging_entry(parent_fd: int, name: str, *, missing_ok: bool) -> None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if not stat.S_ISDIR(before.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(name, _RUNTIME_DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RealmIntegrityError(
                "retained file-candidate cleanup identity changed."
            )
        for child in os.listdir(child_fd):
            if child in {".", ".."}:
                raise RealmIntegrityError(
                    "retained file-candidate cleanup entry is invalid."
                )
            _remove_staging_entry(child_fd, child, missing_ok=False)
    finally:
        os.close(child_fd)
    after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise RealmIntegrityError(
            "retained file-candidate cleanup identity changed."
        )
    os.rmdir(name, dir_fd=parent_fd)


def _prepare_socket_namespace(parent: Path, *, realm_id: str) -> Path:
    if os.name != "posix":
        raise NotImplementedError("retained batch Unix sockets require POSIX.")
    try:
        resolved_parent = parent.resolve(strict=True)
        parent_metadata = resolved_parent.stat()
    except OSError as error:
        raise RealmIntegrityError(
            "retained batch socket parent is unavailable."
        ) from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise RealmIntegrityError("retained batch socket parent is not a directory.")
    namespace_key = request_digest(
        {
            "format": _SOCKET_NAMESPACE_FORMAT,
            "realm_id": _required_text(realm_id, "realm_id"),
            "uid": os.getuid(),
        }
    )
    namespace = resolved_parent / f"oprbw-{namespace_key[:16]}"
    try:
        namespace.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise RealmIntegrityError(
            "retained batch socket namespace cannot be created."
        ) from error
    try:
        metadata = namespace.lstat()
        resolved = namespace.resolve(strict=True)
    except OSError as error:
        raise RealmIntegrityError(
            "retained batch socket namespace cannot be inspected."
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or resolved != namespace
        or resolved.parent != resolved_parent
    ):
        raise RealmIntegrityError(
            "retained batch socket namespace identity is invalid."
        )
    return resolved


def _probe_worker_socket(path: Path) -> _SocketIdentity | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.2)
            connection.connect(str(path))
    except OSError:
        return None
    # Bind readiness to the exact object observed after the successful connect;
    # the supervisor records it and refuses to unlink a later replacement.
    try:
        ready = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISSOCK(ready.st_mode)
        or ready.st_uid != os.getuid()
        or stat.S_IMODE(ready.st_mode) != 0o600
        or (ready.st_dev, ready.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        return None
    return _SocketIdentity(ready.st_dev, ready.st_ino)


def _capture_worker_socket_identity(path: Path) -> _SocketIdentity:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RealmIntegrityError(
            "retained batch socket identity cannot be inspected."
        ) from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RealmIntegrityError("retained batch socket identity is invalid.")
    return _SocketIdentity(metadata.st_dev, metadata.st_ino)


def _require_socket_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RealmIntegrityError(
            "retained batch socket absence cannot be proven."
        ) from error
    raise RealmIntegrityError("retained batch socket unexpectedly exists.")


def _validate_managed_directory_identity(
    path: Path,
    *,
    device_id: int | None,
    inode: int | None,
    label: str,
) -> None:
    if device_id is None or inode is None:
        raise RealmIntegrityError(f"{label} has no durable identity.")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RealmIntegrityError(f"{label} cannot be inspected.") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or (metadata.st_dev, metadata.st_ino) != (device_id, inode)
    ):
        raise RealmIntegrityError(f"{label} identity differs.")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_or_verify_private_file(path: Path, expected: bytes) -> None:
    if not isinstance(expected, bytes):
        raise TypeError("expected file content must be bytes.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _verify_private_file(path, expected)
        _fsync_directory(path.parent)
        return
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RealmIntegrityError("retained batch control file is invalid.")
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(expected):
            written = os.write(descriptor, expected[offset:])
            if written <= 0:
                raise OSError("control file write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _verify_private_file(path, expected)
    _fsync_directory(path.parent)


def _verify_private_file(path: Path, expected: bytes) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != len(expected)
        ):
            raise RealmIntegrityError("retained batch control file differs.")
        chunks: list[bytes] = []
        remaining = len(expected)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise RealmIntegrityError("retained batch control file ended early.")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or b"".join(chunks) != expected:
            raise RealmIntegrityError("retained batch control file differs.")
    finally:
        os.close(descriptor)


def _prepare_private_append_file(path: Path) -> None:
    """Create an empty private append target or validate its grown replay."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _CONTROL_QUOTA.max_file_bytes
        ):
            raise RealmIntegrityError(
                "retained batch diagnostic file identity is invalid."
            )
        os.fchmod(descriptor, 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_socket_path_length(path: Path) -> None:
    if len(os.fsencode(path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise ValueError("retained batch socket path exceeds its bound.")


def _real_executable(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("python_executable must be an absolute Path.")
    try:
        # Preserve the invoked path: virtual environments select their prefix
        # from that path even when the executable itself is a symlink to the
        # base interpreter.  Resolving it here would silently drop the venv and
        # make the installed ``optpilot`` module unavailable to the child.
        resolved_target = path.resolve(strict=True)
        metadata = resolved_target.stat()
    except OSError as error:
        raise ValueError("python_executable is unavailable.") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("python_executable must be an executable file.")
    return path


def _exact_result(
    response: RetainedBatchWorkerResponse,
    expected_fields: set[str],
    *,
    exchange_id: str,
    operation: str,
    exchange_sequence: int | None = None,
    request_digest_value: str | None = None,
) -> Mapping[str, Any]:
    if not isinstance(response, RetainedBatchWorkerResponse):
        raise TypeError("response must be a RetainedBatchWorkerResponse.")
    result = response.response.get("result")
    if not isinstance(result, Mapping) or set(result) != expected_fields:
        raise _protocol_response_error(
            response,
            exchange_id=exchange_id,
            operation=operation,
            exchange_sequence=exchange_sequence,
            request_digest_value=request_digest_value,
        )
    return result


def _protocol_response_error(
    response: RetainedBatchWorkerResponse,
    *,
    exchange_id: str,
    operation: str,
    exchange_sequence: int | None = None,
    request_digest_value: str | None = None,
) -> RetainedBatchProtocolError:
    if not isinstance(response, RetainedBatchWorkerResponse):
        raise TypeError("response must be a RetainedBatchWorkerResponse.")
    return RetainedBatchProtocolError(
        exchange_id=exchange_id,
        operation=operation,
        exchange_sequence=exchange_sequence,
        request_digest=request_digest_value,
        response_digest=response.response_digest,
    )


def retained_batch_worker_request_digest(
    operation: Literal["propose", "observe"], payload: Mapping[str, Any]
) -> str:
    """Return the worker's canonical state-changing request digest.

    Exchange id and total-order sequence are durable coordinates, while this
    digest intentionally covers the callback operation and payload exactly as
    the worker does.  Exporting the helper keeps status/ACK reconciliation from
    duplicating that subtle protocol semantic in the driver.
    """

    if operation not in {"propose", "observe"}:
        raise ValueError("only propose and observe have worker request digests.")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping.")
    copied = _json_copy(payload, "worker request payload")
    return request_digest({"op": operation, "payload": copied})


def _join_portable(root: str, relative: str) -> str:
    if relative == ".":
        return root
    value = f"{root}/{relative}"
    path = PurePosixPath(value)
    if path.is_absolute() or any(component in {"", ".", ".."} for component in path.parts):
        raise ValueError("portable projection path is invalid.")
    return value


def _json_copy(value: Any, label: str) -> Any:
    try:
        encoded = canonical_json_bytes(value)
        return json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"{label} must contain canonical JSON values.") from error


def _deep_freeze_json(value: Any) -> Any:
    """Freeze a value already normalized by :func:`_json_copy`."""

    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze_json(child) for child in value)
    return value


def _deep_thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw_json(child) for child in value]
    return value


def _required_text(value: Any, label: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text.")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8 text.") from error
    if size > max_bytes:
        raise ValueError(f"{label} exceeds its size bound.")
    return value


def _safe_token(value: Any, label: str) -> str:
    selected = _required_text(value, label, max_bytes=512)
    try:
        selected.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be a path-free ASCII token.") from error
    if _SAFE_TOKEN.fullmatch(selected) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return selected


def _exchange_id(value: Any) -> str:
    selected = _safe_token(value, "exchange_id")
    if len(selected.encode("ascii")) > _MAX_EXCHANGE_ID_BYTES:
        raise ValueError("exchange_id exceeds its size bound.")
    return selected


def _lower_digest(value: Any, label: str) -> str:
    selected = _required_text(value, label, max_bytes=64)
    if len(selected) != 64 or any(character not in "0123456789abcdef" for character in selected):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return selected


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite.")
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError(f"{label} must be finite.")
    return selected


def _positive_finite(value: Any, label: str) -> float:
    selected = _finite(value, label)
    if selected <= 0:
        raise ValueError(f"{label} must be positive.")
    return selected


def _nonnegative_finite(value: Any, label: str) -> float:
    selected = _finite(value, label)
    if selected < 0:
        raise ValueError(f"{label} must be nonnegative.")
    return selected


__all__ = [
    "RetainedBatchCacheAck",
    "RetainedBatchExchangeCoordinate",
    "RetainedBatchMethodError",
    "RetainedBatchObservationAck",
    "RetainedBatchProposal",
    "RetainedBatchProtocolError",
    "RetainedBatchRuntimeError",
    "RetainedBatchRuntimeHeartbeat",
    "RetainedBatchRuntimeIdentity",
    "RetainedBatchRuntimeProvider",
    "RetainedBatchInactiveReconciliation",
    "RetainedBatchWorkerResponse",
    "RetainedBatchWorkerStatus",
    "RetainedPythonBatchRuntime",
    "retained_batch_worker_request_digest",
]
