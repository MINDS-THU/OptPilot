"""Crash-recoverable POSIX supervision for one local process provider.

The registry in this module is provider-private operational state.  Host
paths, argv values, and non-secret process environment values are allowed in
``ProcessLaunchRequest`` but never appear in ``WorkerTerminalProof``.
Launch-only private environment values are instead carried through an
anonymous inherited pipe and are absent from the request, registry, manifest,
and object representations.  A per-launch inherited ``flock`` is the liveness
authority; recorded PID values are used only after an authenticated handshake
and are never, by themselves, evidence that the worker is still intended.

This first provider slice is intentionally POSIX-only.  It does not introduce
a host-wide signing secret: atomic records are bound to an unpredictable
per-launch backend token stored in the private registry and to the stable
device/inode identity of the inherited lock file.  The private wrapper also
forks a recovery watchdog before the worker: both retain that exact lock, so
loss of the primary monitor after handshake is recovered without elevating a
recorded PID or PGID into authority by itself.

Provider-owned endpoint records use the same launch registry as cleanup
authority.  Worker-writable volumes are deliberately not consulted when an
endpoint is unlinked.  Ownership and private-directory checks limit accidental
cross-user deletion, but processes running as the same uid are not isolated by
this provider; a stronger same-uid adversary boundary requires an OS sandbox.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

try:  # pragma: no branch - the public constructor also checks availability
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

from ._validation import finite_time, lower_hex_digest, positive_int
from .errors import RealmConflict, RealmIntegrityError, RealmNotFound


class ProcessLaunchUnauthenticatable(Exception):
    """Marker base: a recorded launch cannot be re-authenticated.

    Raised (via the concrete subclasses below) when the durable process
    registry row can no longer be matched against its startup coordinates or
    its on-disk lock identity. Callers holding only the weaker token/binding
    identity may respond with
    :meth:`LocalProcessSupervisor.quarantine_unauthenticatable_launch`, which
    retires such a row only after proving the recorded process is dead.
    """


class ProcessRecoveryCoordinateMismatch(
    ProcessLaunchUnauthenticatable, RealmConflict
):
    """The registry row's startup coordinates differ from reconstruction."""


class ProcessLockIdentityError(
    ProcessLaunchUnauthenticatable, RealmIntegrityError
):
    """The recorded process lock file is missing or was replaced."""
from .refs import canonical_json_bytes, request_digest


JsonDict = dict[str, Any]
WorkerDisposition = Literal["never_started", "exited", "killed"]
LaunchState = Literal["reserved", "start_requested"]
ProcessTerminalPriorState = Literal[
    "reserved",
    "start_requested",
    "terminal",
    "retired",
]
ProcessLaunchSealPriorState = Literal["absent", "sealed", "existing"]
_ReservationInsertResult = Literal["inserted", "launch_conflict", "sealed"]
ProcessEndpointState = Literal["recorded", "reconciled"]

_REQUEST_FORMAT = "optpilot.local-process-launch-request.v2"
_MANIFEST_SCHEMA = "optpilot.local-process-launch-manifest.v1"
_PRIVATE_ENVIRONMENT_SCHEMA = "optpilot.local-process-private-environment.v1"
_HANDSHAKE_SCHEMA = "optpilot.local-process-handshake.v1"
_RESULT_SCHEMA = "optpilot.local-process-result.v1"
_RECOVERED_RESULT_SCHEMA = "optpilot.local-process-recovered-result.v1"
_NEVER_STARTED_RESULT_SCHEMA = "optpilot.local-process-never-started-result.v1"
_STOP_SCHEMA = "optpilot.local-process-stop.v1"
_RETIRED_REQUEST_SCHEMA = "optpilot.local-process-retired-request.v1"
_FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK = (
    "primary_exit_after_worker_fork_before_handshake"
)
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_MAX_ARG_COUNT = 16_384
_MAX_ENV_COUNT = 65_536
_MAX_VALUE_BYTES = 1024 * 1024
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@~-]*$")
_ENV_NAME_RE = re.compile(r"^[^=\x00]+$")
_PRIVATE_REGISTRY_SCHEMA_VERSION = 5

_WRAPPER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_WRAPPER_PROCESSES_LOCK = threading.Lock()


def _safe_id(value: Any, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string.")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be a path-free ASCII token.") from error
    if len(encoded) > max_bytes or _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return value


def _nonnegative_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be nonnegative and finite.")
    selected = float(value)
    if not float("-inf") < selected < float("inf") or selected < 0:
        raise ValueError(f"{label} must be nonnegative and finite.")
    return selected


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _optional_nonnegative_finite_number(
    value: Any, label: str
) -> float | None:
    if value is None:
        return None
    return _nonnegative_finite_number(value, label)


def _process_text(value: Any, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string.")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty.")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL.")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8 text.") from error
    if size > _MAX_VALUE_BYTES:
        raise ValueError(f"{label} exceeds the provider value size bound.")
    return value


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


@dataclass(frozen=True, repr=False)
class ProcessLaunchRequest:
    """Exact operational request for a provider-local child process.

    The environment is complete rather than ambient: the wrapper passes this
    mapping directly to ``execve`` and does not merge the supervisor's host
    environment.  ``cwd`` is absolute so replay cannot depend on a different
    supervisor working directory.
    """

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    private_env_names: tuple[str, ...] = ()
    private_env_binding_revision: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)) or not isinstance(
            self.argv, Sequence
        ):
            raise TypeError("process argv must be a sequence of strings.")
        argv = tuple(
            _process_text(value, f"process argv[{index}]", nonempty=index == 0)
            for index, value in enumerate(self.argv)
        )
        if not argv:
            raise ValueError("process argv must contain an executable.")
        if len(argv) > _MAX_ARG_COUNT:
            raise ValueError("process argv exceeds the provider count bound.")
        cwd = _process_text(self.cwd, "process cwd", nonempty=True)
        if not os.path.isabs(cwd):
            raise ValueError("process cwd must be absolute.")
        if not isinstance(self.env, Mapping):
            raise TypeError("process env must be a mapping of strings.")
        if len(self.env) > _MAX_ENV_COUNT:
            raise ValueError("process env exceeds the provider count bound.")
        env: dict[str, str] = {}
        for raw_name, raw_value in self.env.items():
            name = _process_text(raw_name, "process environment name", nonempty=True)
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ValueError("process environment names must not contain '='.")
            env[name] = _process_text(
                raw_value, f"process environment value {name!r}"
            )
        if isinstance(self.private_env_names, (str, bytes)) or not isinstance(
            self.private_env_names, Sequence
        ):
            raise TypeError("private process environment names must be a sequence.")
        private_names: list[str] = []
        seen_private_names: set[str] = set()
        for index, raw_name in enumerate(self.private_env_names):
            name = _process_text(
                raw_name,
                f"private process environment name[{index}]",
                nonempty=True,
            )
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ValueError(
                    "private process environment names must not contain '='."
                )
            if name in seen_private_names:
                raise ValueError(
                    "private process environment names must not contain duplicates."
                )
            if name in env:
                raise ValueError(
                    "private process environment names must not overlap process env."
                )
            private_names.append(name)
            seen_private_names.add(name)
        if len(private_names) > _MAX_ENV_COUNT:
            raise ValueError(
                "private process environment names exceed the provider count bound."
            )
        private_names = sorted(private_names)
        if private_names:
            private_revision = _safe_id(
                self.private_env_binding_revision,
                "private process environment binding revision",
            )
        elif self.private_env_binding_revision is not None:
            raise ValueError(
                "private process environment binding revision requires names."
            )
        else:
            private_revision = None
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(
            self,
            "env",
            MappingProxyType(dict(sorted(env.items(), key=lambda item: item[0]))),
        )
        object.__setattr__(self, "private_env_names", tuple(private_names))
        object.__setattr__(
            self, "private_env_binding_revision", private_revision
        )
        if len(self.canonical_bytes) > _MAX_REQUEST_BYTES:
            raise ValueError("process launch request exceeds its encoded size bound.")

    def to_dict(self) -> JsonDict:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env": dict(self.env),
            "private_env_binding_revision": self.private_env_binding_revision,
            "private_env_names": list(self.private_env_names),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProcessLaunchRequest":
        _exact_keys(
            payload,
            {
                "argv",
                "cwd",
                "env",
                "private_env_binding_revision",
                "private_env_names",
            },
            "process launch request",
        )
        return cls(
            argv=cast(Any, payload["argv"]),
            cwd=cast(Any, payload["cwd"]),
            env=cast(Any, payload["env"]),
            private_env_names=cast(Any, payload["private_env_names"]),
            private_env_binding_revision=cast(
                Any, payload["private_env_binding_revision"]
            ),
        )

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return request_digest({"format": _REQUEST_FORMAT, **self.to_dict()})

    def __repr__(self) -> str:
        return (
            "ProcessLaunchRequest("
            f"argv={self.argv!r}, cwd={self.cwd!r}, "
            f"env_names={tuple(self.env)!r}, "
            f"private_env_names={self.private_env_names!r}, "
            "private_env_binding_revision="
            f"{self.private_env_binding_revision!r})"
        )


class ProcessLaunchPrivateEnvironment:
    """One-use private values for a reserved physical process start.

    The bundle is intentionally non-serializable and its representation
    contains only the value-free binding identity.
    """

    __slots__ = ("_binding_revision", "_names", "_values")

    def __init__(
        self,
        *,
        binding_revision: str,
        values: Mapping[str, str],
    ) -> None:
        revision = _safe_id(
            binding_revision, "private process environment binding revision"
        )
        if not isinstance(values, Mapping):
            raise TypeError("private process environment values must be a mapping.")
        if len(values) > _MAX_ENV_COUNT:
            raise ValueError(
                "private process environment values exceed the provider count bound."
            )
        selected: dict[str, str] = {}
        for raw_name, raw_value in values.items():
            name = _process_text(
                raw_name, "private process environment name", nonempty=True
            )
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ValueError(
                    "private process environment names must not contain '='."
                )
            selected[name] = _process_text(
                raw_value, f"private process environment value {name!r}"
            )
        self._binding_revision = revision
        self._names = tuple(sorted(selected))
        self._values: dict[str, str] | None = selected

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    @property
    def binding_revision(self) -> str:
        return self._binding_revision

    def _matches(self, request: ProcessLaunchRequest) -> bool:
        return (
            self._names == request.private_env_names
            and self._binding_revision
            == request.private_env_binding_revision
        )

    def _take(self, request: ProcessLaunchRequest) -> dict[str, str]:
        if not self._matches(request):
            self.discard()
            raise RealmConflict(
                "private process environment binding differs from the launch request."
            )
        values = self._values
        if values is None:
            raise RealmConflict(
                "private process environment values are no longer available."
            )
        self._values = None
        return values

    def discard(self) -> None:
        values = self._values
        self._values = None
        if values is not None:
            values.clear()

    def __repr__(self) -> str:
        return (
            "ProcessLaunchPrivateEnvironment("
            f"names={self._names!r}, "
            f"binding_revision={self._binding_revision!r})"
        )


@dataclass(frozen=True, order=True)
class WorkerTerminalProof:
    """Path-free durable evidence that one launch token is terminal."""

    launch_token: str
    binding_id: str
    evidence_fingerprint: str
    backend_token: str
    launch_request_digest: str
    disposition: WorkerDisposition
    provider_generation: int
    terminal_at: float

    def __post_init__(self) -> None:
        _safe_id(self.launch_token, "worker launch token")
        _safe_id(self.binding_id, "worker binding id")
        lower_hex_digest(self.evidence_fingerprint, "worker evidence fingerprint")
        lower_hex_digest(self.backend_token, "worker backend token")
        lower_hex_digest(self.launch_request_digest, "worker launch request digest")
        if self.disposition not in {"never_started", "exited", "killed"}:
            raise ValueError("worker disposition is unsupported.")
        positive_int(self.provider_generation, "worker provider generation")
        finite_time(self.terminal_at, "worker terminal time")

    def to_dict(self) -> JsonDict:
        return {
            "backend_token": self.backend_token,
            "binding_id": self.binding_id,
            "disposition": self.disposition,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "provider_generation": self.provider_generation,
            "terminal_at": self.terminal_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkerTerminalProof":
        _exact_keys(
            payload,
            {
                "backend_token",
                "binding_id",
                "disposition",
                "evidence_fingerprint",
                "launch_request_digest",
                "launch_token",
                "provider_generation",
                "terminal_at",
            },
            "worker terminal proof",
        )
        return cls(**dict(payload))

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def started(self) -> bool:
        """Whether this terminal launch crossed the physical-start handshake."""

        return self.disposition != "never_started"


@dataclass(frozen=True, order=True)
class ProcessTerminalReconciliation:
    """Path-free receipt for exact stop/retire reconciliation.

    ``prior_state`` records the authenticated registry state observed by this
    call.  It is intentionally allowed to become ``retired`` on replay: the
    terminal proof remains byte-identical, while callers can distinguish a
    first reconciliation from convergence after a lost response.  The proof
    carries all four startup coordinates and is revalidated against the
    provider registry before this receipt can be returned.
    """

    prior_state: ProcessTerminalPriorState
    proof: WorkerTerminalProof
    endpoints_reconciled: Literal[True] = True
    retired: Literal[True] = True

    def __post_init__(self) -> None:
        if self.prior_state not in {
            "reserved",
            "start_requested",
            "terminal",
            "retired",
        }:
            raise ValueError("process terminal prior state is unsupported.")
        if not isinstance(self.proof, WorkerTerminalProof):
            raise TypeError("process terminal reconciliation proof is invalid.")
        if self.endpoints_reconciled is not True:
            raise ValueError(
                "process terminal reconciliation must reconcile endpoints."
            )
        if self.retired is not True:
            raise ValueError("process terminal reconciliation must be retired.")

    @property
    def launch_token(self) -> str:
        return self.proof.launch_token

    @property
    def binding_id(self) -> str:
        return self.proof.binding_id

    @property
    def evidence_fingerprint(self) -> str:
        return self.proof.evidence_fingerprint

    @property
    def launch_request_digest(self) -> str:
        return self.proof.launch_request_digest

    def to_dict(self) -> JsonDict:
        return {
            "endpoints_reconciled": True,
            "prior_state": self.prior_state,
            "proof": self.proof.to_dict(),
            "retired": True,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True, order=True)
class ProcessLaunchSealReceipt:
    """Path-free result of atomically sealing one absent launch coordinate.

    ``absent`` means this call installed the durable negative seal, ``sealed``
    is its replay, and ``existing`` means a launch row won the ordering first.
    The latter is deliberately not cleanup authority: callers must recover all
    four startup coordinates and use exact terminal reconciliation before any
    process side effect.
    """

    launch_token: str
    binding_id: str
    prior_state: ProcessLaunchSealPriorState

    def __post_init__(self) -> None:
        _safe_id(self.launch_token, "sealed launch token")
        _safe_id(self.binding_id, "sealed binding id")
        if self.prior_state not in {"absent", "sealed", "existing"}:
            raise ValueError("process launch seal prior state is unsupported.")

    @property
    def sealed(self) -> bool:
        return self.prior_state != "existing"

    def to_dict(self) -> JsonDict:
        return {
            "binding_id": self.binding_id,
            "launch_token": self.launch_token,
            "prior_state": self.prior_state,
            "sealed": self.sealed,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True, order=True)
class ProcessUnixSocketRegistration:
    """Path-free receipt for one provider-owned UNIX socket identity."""

    launch_token: str
    binding_id: str
    evidence_fingerprint: str
    launch_request_digest: str
    endpoint_name: str
    state: ProcessEndpointState

    def __post_init__(self) -> None:
        _safe_id(self.launch_token, "endpoint launch token")
        _safe_id(self.binding_id, "endpoint binding id")
        lower_hex_digest(
            self.evidence_fingerprint, "endpoint evidence fingerprint"
        )
        lower_hex_digest(
            self.launch_request_digest, "endpoint launch request digest"
        )
        _safe_id(self.endpoint_name, "process endpoint name")
        if self.state not in {"recorded", "reconciled"}:
            raise ValueError("process endpoint state is unsupported.")

    def to_dict(self) -> JsonDict:
        return {
            "binding_id": self.binding_id,
            "endpoint_name": self.endpoint_name,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "state": self.state,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True, order=True)
class WorkerStarted:
    """Path-free observation of one authenticated live worker handshake."""

    launch_token: str
    binding_id: str
    evidence_fingerprint: str
    backend_token: str
    launch_request_digest: str
    provider_generation: int
    started: Literal[True] = True

    def __post_init__(self) -> None:
        _safe_id(self.launch_token, "started worker launch token")
        _safe_id(self.binding_id, "started worker binding id")
        lower_hex_digest(
            self.evidence_fingerprint, "started worker evidence fingerprint"
        )
        lower_hex_digest(self.backend_token, "started worker backend token")
        lower_hex_digest(
            self.launch_request_digest, "started worker launch request digest"
        )
        positive_int(
            self.provider_generation, "started worker provider generation"
        )
        if self.started is not True:
            raise ValueError("WorkerStarted.started must be true.")

    def to_dict(self) -> JsonDict:
        return {
            "backend_token": self.backend_token,
            "binding_id": self.binding_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "provider_generation": self.provider_generation,
            "started": True,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkerStarted":
        _exact_keys(
            payload,
            {
                "backend_token",
                "binding_id",
                "evidence_fingerprint",
                "launch_request_digest",
                "launch_token",
                "provider_generation",
                "started",
            },
            "worker started observation",
        )
        return cls(**dict(payload))

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


WorkerStartResult = WorkerStarted | WorkerTerminalProof


@dataclass(frozen=True, order=True)
class ProcessLaunchReservation:
    """Path-free identity for one exact, non-spawning provider reservation.

    The complete :class:`ProcessLaunchRequest` remains in the private provider
    registry.  This receipt is the capability passed across the later Realm
    launch-intent commit and control-publication boundary.  It deliberately
    carries no snapshot of mutable provider state: exact replay remains valid
    after the reservation becomes start-requested or terminal.
    """

    launch_token: str
    binding_id: str
    evidence_fingerprint: str
    backend_token: str
    launch_request_digest: str
    provider_generation: int

    def __post_init__(self) -> None:
        _safe_id(self.launch_token, "reserved launch token")
        _safe_id(self.binding_id, "reserved binding id")
        lower_hex_digest(
            self.evidence_fingerprint, "reserved evidence fingerprint"
        )
        lower_hex_digest(self.backend_token, "reserved backend token")
        lower_hex_digest(
            self.launch_request_digest, "reserved launch request digest"
        )
        positive_int(
            self.provider_generation, "reserved provider generation"
        )

    def to_dict(self) -> JsonDict:
        return {
            "backend_token": self.backend_token,
            "binding_id": self.binding_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "launch_token": self.launch_token,
            "provider_generation": self.provider_generation,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ProcessLaunchReservation":
        _exact_keys(
            payload,
            set(cls.__dataclass_fields__),
            "process launch reservation",
        )
        return cls(**dict(payload))

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


class ProcessLaunchRealizationClaim:
    """Provider-private crash-released claim held before resource creation.

    The public coordinates are path-free.  The descriptor and filesystem
    identity remain private to :class:`LocalProcessSupervisor`; closing the
    descriptor releases the shared realization gate automatically on process
    death.
    """

    __slots__ = (
        "launch_token",
        "binding_id",
        "provider_generation",
        "_descriptor",
        "_gate_device",
        "_gate_inode",
        "_released",
    )

    def __init__(
        self,
        *,
        launch_token: str,
        binding_id: str,
        provider_generation: int,
        descriptor: int,
        gate_device: int,
        gate_inode: int,
    ) -> None:
        self.launch_token = _safe_id(
            launch_token, "realization claim launch token"
        )
        self.binding_id = _safe_id(binding_id, "realization claim binding id")
        self.provider_generation = positive_int(
            provider_generation, "realization claim provider generation"
        )
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            raise TypeError("realization claim descriptor is invalid.")
        self._descriptor = descriptor
        self._gate_device = _nonnegative_int(
            gate_device, "realization claim gate device"
        )
        self._gate_inode = _positive_int(
            gate_inode, "realization claim gate inode"
        )
        self._released = False

    @property
    def released(self) -> bool:
        return self._released


@dataclass(frozen=True)
class _LaunchRow:
    launch_token: str
    binding_id: str
    evidence_fingerprint: str
    backend_token: str
    request: ProcessLaunchRequest | None
    launch_request_digest: str
    provider_generation: int
    coordinate: str
    lock_device: int
    lock_inode: int
    launch_state: LaunchState
    stop_requested: bool
    terminal: WorkerTerminalProof | None
    retired: bool


@dataclass(frozen=True)
class _LaunchSealRow:
    launch_token: str
    binding_id: str
    created_at: float


@dataclass(frozen=True)
class _UnixSocketEndpointRow:
    launch_token: str
    endpoint_name: str
    path: str | None
    path_digest: str
    parent_device: int
    parent_inode: int
    socket_device: int
    socket_inode: int
    state: ProcessEndpointState


class SupervisedProcess:
    """Replayable handle for one exact local process launch."""

    def __init__(self, supervisor: "LocalProcessSupervisor", launch_token: str):
        self._supervisor = supervisor
        self.launch_token = launch_token

    def wait(self, timeout: float | None = None) -> WorkerTerminalProof:
        """Wait until the inherited lock is released and return one proof."""

        return self._supervisor._wait(self.launch_token, timeout=timeout)

    def wait_started(self, timeout: float | None = None) -> WorkerStartResult:
        """Wait for a physical-start observation or a terminal proof.

        ``WorkerStarted`` means the authenticated handshake was observed while
        the inherited liveness lock was held.  A returned terminal proof means
        the launch raced to terminal first: ``exited`` and ``killed`` did
        physically start, while ``never_started`` did not.
        """

        return self._supervisor._wait_started(self.launch_token, timeout=timeout)

    def stop(
        self,
        *,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> WorkerTerminalProof:
        """Terminate the worker process group, then return its durable proof."""

        return self._supervisor._stop(
            self.launch_token,
            grace_period=grace_period,
            timeout=timeout,
        )


class LocalProcessSupervisor:
    """Provider-private, restart-safe POSIX local process supervisor."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        poll_interval: float = 0.02,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        fault_injector: Callable[[str], None] | None = None,
        _wrapper_fault_mode: str | None = None,
    ) -> None:
        if os.name != "posix" or fcntl is None:
            raise NotImplementedError(
                "LocalProcessSupervisor currently requires POSIX flock and process groups."
            )
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("process supervisor poll_interval must be positive.")
        if _wrapper_fault_mode not in {
            None,
            _FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK,
        }:
            raise ValueError("local process wrapper fault mode is unsupported.")
        self.root = Path(root).expanduser().resolve()
        self._launches_root = self.root / "launches"
        self._realization_gates_root = self.root / "realization-gates"
        self.database_path = self.root / "process_registry.sqlite3"
        self._poll_interval = float(poll_interval)
        self._clock = clock
        self._monotonic = monotonic
        self._fault_injector = fault_injector
        self._wrapper_fault_mode = _wrapper_fault_mode
        self._initialize_private_root()
        self._initialize_registry()
        self.provider_generation = self._claim_provider_generation()

    def claim_launch_realization(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> ProcessLaunchRealizationClaim:
        """Hold the crash-released gate required before creating resources.

        Multiple exact realizers may share the gate for replay.  A terminal
        cleanup takes it exclusively before installing a negative seal, so a
        successful cleanup resource sweep begins only after every earlier
        creator has returned or crashed.  A seal that won first rejects the
        claim before any caller-visible resource may be created.
        """

        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        descriptor, metadata = self._acquire_realization_gate(
            launch_token=launch_token,
            exclusive=False,
            timeout=timeout,
        )
        try:
            seal = self._read_launch_seal(launch_token)
            row = self._read_row(launch_token)
            if seal is not None and row is not None:
                raise RealmIntegrityError(
                    "local process launch and negative seal coexist."
                )
            if seal is not None:
                raise RealmConflict(
                    "local process launch coordinate is unavailable."
                )
            if row is not None and row.binding_id != binding_id:
                raise RealmConflict(
                    "local process launch coordinate differs from provider registry."
                )
            return ProcessLaunchRealizationClaim(
                launch_token=launch_token,
                binding_id=binding_id,
                provider_generation=self.provider_generation,
                descriptor=descriptor,
                gate_device=metadata.st_dev,
                gate_inode=metadata.st_ino,
            )
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

    def release_launch_realization(
        self, claim: ProcessLaunchRealizationClaim
    ) -> None:
        """Release one exact realization claim; replay is idempotent."""

        if not isinstance(claim, ProcessLaunchRealizationClaim):
            raise TypeError("claim must be a ProcessLaunchRealizationClaim.")
        if claim.released:
            return
        try:
            self._validate_realization_claim(claim)
        finally:
            try:
                fcntl.flock(claim._descriptor, fcntl.LOCK_UN)
            finally:
                os.close(claim._descriptor)
                claim._released = True

    def reserve(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        request: ProcessLaunchRequest,
        realization_claim: ProcessLaunchRealizationClaim | None = None,
    ) -> ProcessLaunchReservation:
        """Durably reserve one exact request without publishing or spawning it.

        A repeated launch token must carry the identical binding, evidence, and
        byte-canonical request.  A reserved row is passive: lookup and restart
        never reinterpret it as started or terminal, and only
        :meth:`start_reserved` or :meth:`abandon_reserved` may advance it.
        """

        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        lower_hex_digest(evidence_fingerprint, "worker evidence fingerprint")
        if not isinstance(request, ProcessLaunchRequest):
            raise TypeError("request must be a ProcessLaunchRequest.")
        if realization_claim is not None:
            self._validate_realization_claim(realization_claim)
            if (
                realization_claim.launch_token != launch_token
                or realization_claim.binding_id != binding_id
            ):
                raise RealmConflict(
                    "realization claim differs from process reservation."
                )

        expected = (binding_id, evidence_fingerprint, request)
        seal = self._read_launch_seal(launch_token)
        existing = self._read_row(launch_token)
        if seal is not None and existing is not None:
            raise RealmIntegrityError(
                "local process launch and negative seal coexist."
            )
        if seal is not None:
            raise RealmConflict("local process launch coordinate is unavailable.")
        if existing is not None:
            self._validate_replay(existing, *expected)
            return self._reservation_from_row(existing)

        coordinate = hashlib.sha256(
            b"optpilot/local-process-coordinate/v1\0" + launch_token.encode("ascii")
        ).hexdigest()
        launch_dir = self._ensure_launch_directory(coordinate)
        lock_fd = self._open_lock(launch_dir / "liveness.lock")
        acquired = False
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                return self._reservation_during_concurrent_commit(
                    launch_token, binding_id, evidence_fingerprint, request
                )

            # Another reserver may have committed between our first read and
            # lock acquisition.  Attach to its exact identity without changing
            # its durable state.
            existing = self._read_row(launch_token)
            if existing is not None:
                self._validate_replay(existing, *expected)
                return self._reservation_from_row(existing)

            self._assert_clean_launch_artifacts(launch_dir)
            metadata = os.fstat(lock_fd)
            backend_token = secrets.token_hex(32)
            row = _LaunchRow(
                launch_token=launch_token,
                binding_id=binding_id,
                evidence_fingerprint=evidence_fingerprint,
                backend_token=backend_token,
                request=request,
                launch_request_digest=request.digest,
                provider_generation=self.provider_generation,
                coordinate=coordinate,
                lock_device=metadata.st_dev,
                lock_inode=metadata.st_ino,
                launch_state="reserved",
                stop_requested=False,
                terminal=None,
                retired=False,
            )
            insertion = self._insert_reservation(row)
            if insertion == "sealed":
                self._delete_retired_launch_directory(row)
                raise RealmConflict("local process launch coordinate is unavailable.")
            if insertion == "launch_conflict":
                existing = self._required_row(launch_token)
                self._validate_replay(existing, *expected)
                return self._reservation_from_row(existing)
            if insertion != "inserted":  # pragma: no cover - closed literal
                raise RealmIntegrityError(
                    "local process reservation decision is invalid."
                )
            return self._reservation_from_row(row)
        finally:
            if acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)

    def lookup_reservation(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> ProcessLaunchReservation:
        """Recover an exact path-free reservation without its host request."""

        row = self._lookup_exact_row(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
        )
        return self._reservation_from_row(row)

    def seal_launch_if_absent(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> ProcessLaunchSealReceipt:
        """Atomically prohibit any future reservation for an absent launch.

        The decision is made in the same provider registry write order used by
        reservation insertion.  If no process row exists, an immutable seal
        keyed by launch token and binding is inserted.  Exact replay returns
        ``sealed``; another binding conflicts without revealing whether a row
        or seal owns the token.  If a matching process row already exists this
        method returns ``existing`` and performs no cleanup.  The weaker
        token/binding identity is therefore sufficient only to prevent a
        future start, never to stop an existing process.
        """

        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        descriptor, _metadata = self._acquire_realization_gate(
            launch_token=launch_token,
            exclusive=True,
            timeout=timeout,
        )
        try:
            return self._seal_launch_registry_if_absent(
                launch_token=launch_token,
                binding_id=binding_id,
            )
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _seal_launch_registry_if_absent(
        self, *, launch_token: str, binding_id: str
    ) -> ProcessLaunchSealReceipt:
        now = float(self._clock())
        finite_time(now, "process launch seal time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            launch_record = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (launch_token,),
            ).fetchone()
            seal_record = connection.execute(
                "SELECT * FROM process_launch_seals WHERE launch_token = ?",
                (launch_token,),
            ).fetchone()
            if launch_record is not None and seal_record is not None:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process launch and negative seal coexist."
                )
            if launch_record is not None:
                launch = self._decode_row(launch_record)
                if launch.binding_id != binding_id:
                    connection.rollback()
                    raise RealmConflict(
                        "local process launch coordinate differs from provider registry."
                    )
                prior_state: ProcessLaunchSealPriorState = "existing"
                connection.rollback()
            elif seal_record is not None:
                seal = self._decode_launch_seal(seal_record)
                if seal.binding_id != binding_id:
                    connection.rollback()
                    raise RealmConflict(
                        "local process launch coordinate differs from provider registry."
                    )
                prior_state = "sealed"
                connection.rollback()
            else:
                connection.execute(
                    """
                    INSERT INTO process_launch_seals(
                        launch_token, binding_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (launch_token, binding_id, now),
                )
                connection.commit()
                prior_state = "absent"
        return ProcessLaunchSealReceipt(
            launch_token=launch_token,
            binding_id=binding_id,
            prior_state=prior_state,
        )

    def quarantine_unauthenticatable_launch(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> str:
        """Retire one unauthenticatable launch after proving its process died.

        Exact terminal reconciliation requires all four startup coordinates
        and the recorded lock identity. A crashed provider, a moved
        interpreter, or replaced on-disk artifacts can make both permanently
        unreconstructable, which previously left the launch row - and every
        run holding it - in an endless cleanup-retry loop. This method
        accepts the weaker token/binding identity, but only retires the row
        after proving the recorded worker process no longer exists:

        - a ``reserved`` row never spawned a process;
        - otherwise the handshake's recorded ``worker_pid`` must be gone
          (or provably not ours);
        - failing that, the recorded liveness lock must still match the
          registry identity and be free.

        When death cannot be proven the original conflict is preserved and
        nothing changes. On success the row and its endpoint records are
        removed and the durable negative seal is installed, so the ordinary
        terminal reconciliation path converges as ``sealed`` on replay. The
        launch directory's artifacts are deliberately left for forensics.
        Returns the prior launch state.
        """

        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        descriptor, _metadata = self._acquire_realization_gate(
            launch_token=launch_token,
            exclusive=True,
            timeout=timeout,
        )
        try:
            row = self._required_row(launch_token)
            if row.binding_id != binding_id:
                raise RealmConflict(
                    "local process launch coordinate differs from provider registry."
                )
            if row.retired:
                return "retired"
            if row.launch_state != "reserved":
                self._assert_recorded_process_dead(row)
            launch_state = row.launch_state
            self._remove_recorded_sockets_loosely(launch_token)
            now = float(self._clock())
            finite_time(now, "process launch quarantine time")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT retired FROM process_launches WHERE launch_token = ?",
                    (launch_token,),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    raise RealmConflict(
                        "local process launch changed during quarantine."
                    )
                connection.execute(
                    "DELETE FROM process_unix_socket_endpoints "
                    "WHERE launch_token = ?",
                    (launch_token,),
                )
                connection.execute(
                    "DELETE FROM process_launches WHERE launch_token = ?",
                    (launch_token,),
                )
                connection.execute(
                    """
                    INSERT INTO process_launch_seals(
                        launch_token, binding_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (launch_token, binding_id, now),
                )
                connection.commit()
            return launch_state
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _assert_recorded_process_dead(self, row: _LaunchRow) -> None:
        """Prove the recorded worker died; raise the original conflict shape."""

        launch_dir = self._launch_directory(row.coordinate)
        worker_pid: int | None = None
        try:
            payload = json.loads(
                (launch_dir / "handshake.json").read_text(encoding="utf-8")
            )
            candidate = payload.get("worker_pid")
            if (
                not isinstance(candidate, bool)
                and isinstance(candidate, int)
                and candidate > 1
            ):
                worker_pid = candidate
        except (OSError, ValueError, AttributeError):
            worker_pid = None
        if worker_pid is not None:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                # A recycled PID owned by another user cannot be our worker.
                return
            raise RealmConflict(
                "local process quarantine refused: the recorded worker PID "
                "is still running."
            )
        # No usable handshake: fall back to the liveness lock. A live worker
        # holds an exclusive flock on the recorded lock inode for its whole
        # lifetime, so a free matching lock proves death. Anything less
        # provable fails closed.
        lock_path = launch_dir / "liveness.lock"
        try:
            lock_fd = self._open_existing_lock(lock_path, row)
        except RealmIntegrityError as error:
            raise RealmConflict(
                "local process quarantine refused: worker death could not "
                "be proven."
            ) from error
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RealmConflict(
                    "local process quarantine refused: the recorded worker "
                    "still holds its liveness lock."
                ) from error
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _remove_recorded_sockets_loosely(self, launch_token: str) -> None:
        """Best-effort unlink of recorded endpoint sockets before quarantine.

        Only a path whose current identity still matches the endpoint record
        (a socket with the recorded device and inode) is removed; anything
        else is left untouched. Registry rows are deleted transactionally by
        the caller.
        """

        for endpoint in self._list_unix_socket_endpoints(launch_token):
            if endpoint.path is None:
                continue
            try:
                path = self._unix_socket_path(endpoint.path)
                current = os.lstat(path)
                if (
                    stat.S_ISSOCK(current.st_mode)
                    and current.st_dev == endpoint.socket_device
                    and current.st_ino == endpoint.socket_inode
                ):
                    os.unlink(path)
            except (OSError, ValueError):
                continue

    def record_unix_socket_endpoint(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
        endpoint_name: str,
        path: str | os.PathLike[str],
        device_id: int,
        inode: int,
    ) -> ProcessUnixSocketRegistration:
        """Bind one ready UNIX socket to an exact launch registry row.

        The path and filesystem identity remain provider-private.  The exact
        four portable launch coordinates are checked before the filesystem or
        endpoint registry is touched.  Initial publication requires a real,
        current-user-owned ``0600`` socket in a current-user-owned private
        directory.  Exact replay is allowed after the socket disappears so a
        committed-but-unseen publication can still converge.
        """

        row = self._lookup_exact_row(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
        )
        endpoint_name = _safe_id(endpoint_name, "process endpoint name")
        selected_path = self._unix_socket_path(path)
        selected_device = _nonnegative_int(device_id, "UNIX socket device id")
        selected_inode = _positive_int(inode, "UNIX socket inode")
        path_text = str(selected_path)
        path_digest = self._unix_socket_path_digest(path_text)
        existing = self._read_unix_socket_endpoint(
            launch_token=row.launch_token, endpoint_name=endpoint_name
        )
        if existing is not None:
            if (
                existing.path_digest != path_digest
                or existing.socket_device != selected_device
                or existing.socket_inode != selected_inode
                or (
                    existing.state == "recorded" and existing.path != path_text
                )
            ):
                raise RealmConflict(
                    "UNIX socket endpoint differs from provider registry."
                )
            return self._endpoint_registration(row, existing)
        if row.retired:
            raise RealmConflict(
                "retired local process launch cannot acquire an endpoint."
            )
        if row.launch_state != "start_requested":
            raise RealmConflict(
                "local process endpoint requires a committed start request."
            )
        parent_metadata, socket_metadata = self._inspect_ready_unix_socket(
            selected_path
        )
        if (
            socket_metadata.st_dev != selected_device
            or socket_metadata.st_ino != selected_inode
        ):
            raise RealmIntegrityError(
                "ready UNIX socket identity differs from the recorded observation."
            )
        now = float(self._clock())
        finite_time(now, "local process endpoint publication time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (row.launch_token,),
            ).fetchone()
            if persisted is None:
                connection.rollback()
                raise RealmNotFound("local process launch does not exist.")
            current = self._decode_row(persisted)
            self._validate_exact_coordinates(current, row)
            if current.retired:
                connection.rollback()
                raise RealmConflict(
                    "retired local process launch cannot acquire an endpoint."
                )
            if current.launch_state != "start_requested":
                connection.rollback()
                raise RealmConflict(
                    "local process endpoint requires a committed start request."
                )
            existing_record = connection.execute(
                """
                SELECT * FROM process_unix_socket_endpoints
                WHERE launch_token = ? AND endpoint_name = ?
                """,
                (row.launch_token, endpoint_name),
            ).fetchone()
            if existing_record is not None:
                existing = self._decode_unix_socket_endpoint(existing_record)
                connection.rollback()
                if (
                    existing.path_digest != path_digest
                    or existing.parent_device != parent_metadata.st_dev
                    or existing.parent_inode != parent_metadata.st_ino
                    or existing.socket_device != selected_device
                    or existing.socket_inode != selected_inode
                    or (
                        existing.state == "recorded"
                        and existing.path != path_text
                    )
                ):
                    raise RealmConflict(
                        "UNIX socket endpoint differs from provider registry."
                    )
                return self._endpoint_registration(current, existing)
            try:
                connection.execute(
                    """
                    INSERT INTO process_unix_socket_endpoints(
                        launch_token, endpoint_name, path, path_digest,
                        parent_device, parent_inode, socket_device, socket_inode,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?)
                    """,
                    (
                        row.launch_token,
                        endpoint_name,
                        path_text,
                        path_digest,
                        parent_metadata.st_dev,
                        parent_metadata.st_ino,
                        selected_device,
                        selected_inode,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise RealmConflict(
                    "UNIX socket endpoint path is already registered."
                ) from error
            connection.commit()
        endpoint = _UnixSocketEndpointRow(
            launch_token=row.launch_token,
            endpoint_name=endpoint_name,
            path=path_text,
            path_digest=path_digest,
            parent_device=parent_metadata.st_dev,
            parent_inode=parent_metadata.st_ino,
            socket_device=selected_device,
            socket_inode=selected_inode,
            state="recorded",
        )
        return self._endpoint_registration(row, endpoint)

    def start_reserved(
        self,
        reservation: ProcessLaunchReservation,
        *,
        private_environment: ProcessLaunchPrivateEnvironment | None = None,
    ) -> SupervisedProcess:
        """Request physical start once, or attach to the exact prior request.

        ``start_requested`` is committed before the manifest is published or a
        wrapper is spawned.  Once that state is visible this method never
        respawns: an unlocked request without a handshake reconciles to an
        authenticated ``never_started`` proof.  A passive attachment needs no
        private values.  A still-reserved request whose durable descriptor
        declares private environment names requires one exact, one-use bundle.
        """

        if private_environment is not None and not isinstance(
            private_environment, ProcessLaunchPrivateEnvironment
        ):
            raise TypeError(
                "private_environment must be a "
                "ProcessLaunchPrivateEnvironment or None."
            )
        row = self._row_for_reservation(reservation)
        if row.terminal is not None:
            if private_environment is not None:
                private_environment.discard()
            return SupervisedProcess(self, row.launch_token)
        if row.launch_state == "start_requested":
            if private_environment is not None:
                private_environment.discard()
            self._refresh(row.launch_token)
            return SupervisedProcess(self, row.launch_token)

        launch_dir = self._launch_directory(row.coordinate)
        lock_fd = self._open_existing_lock(launch_dir / "liveness.lock", row)
        acquired = False
        start_committed = False
        private_values: dict[str, str] | None = None
        deadline = self._monotonic() + 5.0
        try:
            while not acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    current = self._row_for_reservation(reservation)
                    if current.terminal is not None:
                        if private_environment is not None:
                            private_environment.discard()
                        if current.launch_state == "reserved":
                            self._validate_reserved_artifacts(current)
                        else:
                            self._validate_live_artifacts(current)
                        return SupervisedProcess(self, current.launch_token)
                    if current.launch_state == "start_requested":
                        if private_environment is not None:
                            private_environment.discard()
                        self._validate_live_artifacts(current)
                        return SupervisedProcess(self, current.launch_token)
                    if self._monotonic() >= deadline:
                        raise RealmIntegrityError(
                            "reserved local process lock remained held without "
                            "a durable start decision."
                        )
                    time.sleep(self._poll_interval)

            current = self._row_for_reservation(reservation)
            if current.terminal is not None:
                if private_environment is not None:
                    private_environment.discard()
                return SupervisedProcess(self, current.launch_token)
            if current.launch_state == "start_requested":
                if private_environment is not None:
                    private_environment.discard()
                self._reconcile_while_locked(current, lock_fd)
                return SupervisedProcess(self, current.launch_token)

            abandoned = self._recover_reserved_abandonment_if_present(current)
            if abandoned is not None:
                if private_environment is not None:
                    private_environment.discard()
                self._commit_terminal(current, abandoned)
                return SupervisedProcess(self, current.launch_token)
            self._assert_clean_launch_artifacts(launch_dir)
            request = self._required_process_request(current)
            if request.private_env_names:
                if private_environment is None:
                    raise RealmConflict(
                        "private process environment values are required for "
                        "this new launch."
                    )
                private_values = private_environment._take(request)
            elif private_environment is not None:
                private_environment.discard()
                raise RealmConflict(
                    "private process environment values were supplied for a "
                    "launch that does not declare them."
                )
            current = self._mark_start_requested(reservation)
            if current.terminal is not None:
                if private_values is not None:
                    private_values.clear()
                return SupervisedProcess(self, current.launch_token)
            if current.launch_state != "start_requested":  # pragma: no cover
                raise RealmIntegrityError(
                    "local process start decision was not persisted."
                )
            start_committed = True
            manifest_path = launch_dir / "manifest.json"
            self._publish_record(manifest_path, self._manifest(current))
            if self._fault_injector is not None:
                # Preserve the established crash-cut name.  The durable row is
                # now start-requested and the exact manifest is published, but
                # no wrapper has yet been spawned.
                self._fault_injector("intent_committed")
            try:
                process = self._spawn_wrapper(
                    manifest_path,
                    lock_fd,
                    request=request,
                    private_values=private_values,
                )
            except Exception:
                # Keep start_requested.  Exact recovery observes the released
                # lock and records never_started; it must not respawn.
                raise
            self._track_wrapper(process)
            return SupervisedProcess(self, current.launch_token)
        finally:
            if private_values is not None:
                private_values.clear()
            # Once start is durable, a spawn call may have forked even if it
            # raises or the caller is interrupted before Popen returns.  An
            # explicit LOCK_UN would revoke an inherited witness as well, so
            # from that point onward the parent only closes its descriptor.
            # If no child inherited it, close releases the lock normally.
            if acquired and not start_committed:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)

    def abandon_reserved(
        self, reservation: ProcessLaunchReservation
    ) -> WorkerTerminalProof:
        """Terminalize an exact reservation that never requested physical start."""

        row = self._row_for_reservation(reservation)
        if row.launch_state != "reserved":
            raise RealmConflict("local process start was already requested.")
        if row.terminal is not None:
            return row.terminal
        launch_dir = self._launch_directory(row.coordinate)
        lock_fd = self._open_existing_lock(launch_dir / "liveness.lock", row)
        acquired = False
        deadline = self._monotonic() + 5.0
        try:
            while not acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    current = self._row_for_reservation(reservation)
                    if current.launch_state == "start_requested":
                        raise RealmConflict(
                            "local process start was already requested."
                        )
                    if current.terminal is not None:
                        return current.terminal
                    if self._monotonic() >= deadline:
                        raise RealmIntegrityError(
                            "reserved local process lock remained held without "
                            "a durable decision."
                        )
                    time.sleep(self._poll_interval)

            current = self._row_for_reservation(reservation)
            if current.launch_state != "reserved":
                raise RealmConflict("local process start was already requested.")
            if current.terminal is not None:
                return current.terminal
            existing = self._recover_reserved_abandonment_if_present(current)
            if existing is not None:
                return self._commit_terminal(current, existing)
            self._assert_clean_launch_artifacts(launch_dir)
            proof = self._new_proof(current, disposition="never_started")
            self._publish_never_started_result(current, proof)
            return self._commit_terminal(current, proof)
        finally:
            if acquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)

    def launch(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        request: ProcessLaunchRequest,
        private_environment: ProcessLaunchPrivateEnvironment | None = None,
    ) -> SupervisedProcess:
        """Compatibility convenience for exact reserve followed by start."""

        return self.start_reserved(
            self.reserve(
                launch_token=launch_token,
                binding_id=binding_id,
                evidence_fingerprint=evidence_fingerprint,
                request=request,
            ),
            private_environment=private_environment,
        )

    def validate_terminal_proof(
        self, proof: WorkerTerminalProof
    ) -> WorkerTerminalProof:
        """Reload and return the exact terminal proof committed by this provider.

        Constructing a valid-looking public dataclass is insufficient: the
        launch token must exist in this private registry and its committed
        canonical proof must match byte for byte.
        """

        if not isinstance(proof, WorkerTerminalProof):
            raise TypeError("proof must be a WorkerTerminalProof.")
        row = self._required_row(proof.launch_token)
        if row.terminal is None:
            raise RealmConflict(
                "local process launch has no committed terminal proof."
            )
        if (
            row.terminal != proof
            or row.terminal.canonical_bytes != proof.canonical_bytes
        ):
            raise RealmIntegrityError(
                "terminal proof is not the exact proof committed by this provider."
            )
        return row.terminal

    def lookup_terminal_proof(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> WorkerTerminalProof | None:
        """Return one exact registry-authenticated proof without a host request.

        This is the narrow recovery seam for callers that retained portable
        binding coordinates but deliberately did not retain or reconstruct the
        host ``ProcessLaunchRequest``.  All four expected coordinates must
        match the private startup intent before provider reconciliation occurs;
        a different launch token can never substitute another registry row.
        ``None`` means that exact launch remains nonterminal.
        """

        row = self._lookup_exact_row(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
        )
        proof = row.terminal
        if proof is None:
            proof = self._refresh(row.launch_token)
        if proof is None:
            return None
        return self.validate_terminal_proof(proof)

    def reservation_state(
        self, reservation: ProcessLaunchReservation
    ) -> Literal["reserved", "start_requested", "terminal"]:
        """Return the exact non-pathful decision state for one reservation."""

        row = self._row_for_reservation(reservation)
        if row.terminal is not None:
            return "terminal"
        return row.launch_state

    def retire_terminal(self, proof: WorkerTerminalProof) -> WorkerTerminalProof:
        """Redact one authenticated terminal request and delete launch artifacts.

        Retirement commits a non-spawnable tombstone before touching the launch
        directory.  A crash can therefore leave cleanup debt, but can never make
        the same launch token eligible for another physical start.  The raw
        terminal proof remains provider-private so late exact authentication is
        possible; argv, cwd, env, and all native paths are removed.
        """

        proof = self.validate_terminal_proof(proof)
        row = self._required_row(proof.launch_token)
        self._validate_proof_identity(row, proof)
        self._require_unix_socket_endpoints_reconciled(row.launch_token)
        retired_request = canonical_json_bytes(
            {
                "launch_request_digest": row.launch_request_digest,
                "schema": _RETIRED_REQUEST_SCHEMA,
            }
        )
        if not row.retired:
            now = float(self._clock())
            finite_time(now, "local process retirement time")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                persisted = connection.execute(
                    "SELECT * FROM process_launches WHERE launch_token = ?",
                    (row.launch_token,),
                ).fetchone()
                if persisted is None:
                    connection.rollback()
                    raise RealmNotFound("local process launch does not exist.")
                current = self._decode_row(persisted)
                self._validate_proof_identity(current, proof)
                if current.terminal != proof:
                    connection.rollback()
                    raise RealmIntegrityError(
                        "local process terminal proof changed before retirement."
                    )
                pending_endpoint = connection.execute(
                    """
                    SELECT 1 FROM process_unix_socket_endpoints
                    WHERE launch_token = ? AND state != 'reconciled'
                    LIMIT 1
                    """,
                    (row.launch_token,),
                ).fetchone()
                if pending_endpoint is not None:
                    connection.rollback()
                    raise RealmConflict(
                        "local process endpoint cleanup remains pending."
                    )
                if current.retired:
                    connection.rollback()
                else:
                    cursor = connection.execute(
                        """
                        UPDATE process_launches
                        SET request_json = ?, retired = 1, updated_at = ?
                        WHERE launch_token = ?
                          AND retired = 0
                          AND terminal_json IS NOT NULL
                        """,
                        (retired_request, now, row.launch_token),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        raise RealmIntegrityError(
                            "local process launch changed during retirement."
                        )
                    connection.commit()
        # secure_delete scrubs the replaced cell in the main database.  A
        # truncating checkpoint is part of retirement so the WAL cannot retain
        # the old pathful request after a successful return.
        self._checkpoint_redacted_registry()
        self._delete_retired_launch_directory(row)
        return proof

    def retire_passive_orphan(
        self, *, launch_token: str, binding_id: str
    ) -> WorkerTerminalProof:
        """Abandon and retire one unbound reservation without starting it.

        Only the canonical attempt's launch token and binding id cross into this
        seam.  Remaining coordinates stay private.  A start-requested row is
        never stopped or retired here because an unbound terminal attempt lacks
        the stronger launch receipt required to authenticate that side effect.
        """

        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        row = self._required_row(launch_token)
        if row.binding_id != binding_id:
            raise RealmConflict(
                "orphan reservation differs from the canonical attempt."
            )
        if row.retired:
            if row.terminal is None:  # pragma: no cover - row decoder enforces
                raise RealmIntegrityError(
                    "retired orphan reservation has no terminal proof."
                )
            return self.retire_terminal(row.terminal)
        if row.launch_state != "reserved":
            raise RealmConflict(
                "unbound start-requested launch requires stronger authority."
            )
        proof = self.abandon_reserved(self._reservation_from_row(row))
        return self.retire_terminal(proof)

    def reconcile_terminal_launch(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> ProcessTerminalReconciliation:
        """Stop and retire one exact retained launch without exposing rows.

        All four path-free startup coordinates are mandatory.  They are
        checked before any stop, terminalization, redaction, or directory
        cleanup side effect.  A passive reservation is abandoned without
        spawning; a start-requested launch is stopped through the supervisor's
        authenticated process-group protocol; an existing terminal proof is
        simply retired.  Repeating this call after any committed-but-unseen
        terminal or retirement decision converges on the same proof.

        The method deliberately owns construction of the internal process
        handle.  A caller that retained only canonical coordinates therefore
        never reads a private registry row and never receives a token-only
        ``SupervisedProcess`` capability.
        """

        grace = _nonnegative_finite_number(
            grace_period, "process stop grace_period"
        )
        wait = _optional_nonnegative_finite_number(
            timeout, "process wait timeout"
        )
        expected = {
            "launch_token": launch_token,
            "binding_id": binding_id,
            "evidence_fingerprint": evidence_fingerprint,
            "launch_request_digest": launch_request_digest,
        }

        # The only concurrent forward transition that needs a retry is
        # reserved -> start_requested.  Every other action either yields a
        # terminal proof or leaves the exact prior state unchanged.
        for _attempt in range(2):
            row = self._lookup_exact_row(**expected)
            if row.retired:
                if row.terminal is None:  # pragma: no cover - decoder enforces
                    raise RealmIntegrityError(
                        "retired local process launch has no terminal proof."
                    )
                prior_state: ProcessTerminalPriorState = "retired"
                proof = row.terminal
            elif row.terminal is not None:
                prior_state = "terminal"
                proof = row.terminal
            else:
                prior_state = row.launch_state
                try:
                    if row.launch_state == "reserved":
                        proof = self.abandon_reserved(
                            self._reservation_from_row(row)
                        )
                    else:
                        proof = self._stop(
                            row.launch_token,
                            grace_period=grace,
                            timeout=wait,
                        )
                except Exception:
                    # A terminal commit may have succeeded before its response
                    # was lost.  Only the same four authenticated coordinates
                    # may recover that proof.  Otherwise preserve the original
                    # error, except for the single monotonic start race.
                    current = self._lookup_exact_row(**expected)
                    if current.terminal is not None:
                        proof = current.terminal
                    elif (
                        row.launch_state == "reserved"
                        and current.launch_state == "start_requested"
                    ):
                        continue
                    else:
                        raise

            if (
                proof.launch_token != row.launch_token
                or proof.binding_id != row.binding_id
                or proof.evidence_fingerprint != row.evidence_fingerprint
                or proof.launch_request_digest != row.launch_request_digest
            ):
                raise RealmIntegrityError(
                    "terminal proof differs from process recovery coordinates."
                )
            proof = self.validate_terminal_proof(proof)
            self._reconcile_unix_socket_endpoints(row)
            self.retire_terminal(proof)
            retired = self._lookup_exact_row(**expected)
            if not retired.retired or retired.terminal != proof:
                raise RealmIntegrityError(
                    "local process launch did not retire exactly."
                )
            return ProcessTerminalReconciliation(
                prior_state=prior_state,
                proof=proof,
                endpoints_reconciled=True,
            )

        raise RealmIntegrityError(
            "local process launch kept changing during terminal reconciliation."
        )

    def _wait_started(
        self, launch_token: str, *, timeout: float | None
    ) -> WorkerStartResult:
        deadline = self._deadline(timeout)
        while True:
            observation = self._observe_started(launch_token)
            if observation is not None:
                return observation
            self._sleep_until_next_poll(deadline, "waiting for local process start")

    def _wait(
        self, launch_token: str, *, timeout: float | None
    ) -> WorkerTerminalProof:
        deadline = self._deadline(timeout)
        while True:
            proof = self._refresh(launch_token)
            if proof is not None:
                return proof
            self._sleep_until_next_poll(deadline, "waiting for local process")

    def _observe_started(self, launch_token: str) -> WorkerStartResult | None:
        row = self._required_row(launch_token)
        if row.terminal is not None:
            return row.terminal
        launch_dir = self._launch_directory(row.coordinate)
        lock_fd = self._open_existing_lock(launch_dir / "liveness.lock", row)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if row.launch_state == "reserved":
                    self._validate_reserved_artifacts(row)
                    return None
                self._validate_live_artifacts(row)
                handshake = self._read_handshake_if_present(row)
                if handshake is None:
                    return None
                # The valid handshake plus the simultaneously held inherited
                # lock is physical-start authority.  Recorded PID values are
                # not consulted for liveness beyond handshake validation.
                return self._new_started(row)
            return self._reconcile_while_locked(row, lock_fd)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

    def _stop(
        self,
        launch_token: str,
        *,
        grace_period: float,
        timeout: float | None,
    ) -> WorkerTerminalProof:
        if (
            isinstance(grace_period, bool)
            or not isinstance(grace_period, (int, float))
            or not float("-inf") < float(grace_period) < float("inf")
            or grace_period < 0
        ):
            raise ValueError("process stop grace_period must be nonnegative.")
        deadline = self._deadline(timeout)
        row = self._mark_stop_requested(launch_token)
        if row.terminal is not None:
            return row.terminal
        self._publish_or_validate_stop(row, float(grace_period))

        while True:
            proof = self._refresh(launch_token)
            if proof is not None:
                return proof
            self._sleep_until_next_poll(deadline, "stopping local process")

    def _refresh(self, launch_token: str) -> WorkerTerminalProof | None:
        row = self._required_row(launch_token)
        if row.terminal is not None:
            return row.terminal
        launch_dir = self._launch_directory(row.coordinate)
        lock_fd = self._open_existing_lock(launch_dir / "liveness.lock", row)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if row.launch_state == "reserved":
                    self._validate_reserved_artifacts(row)
                    return None
                self._validate_live_artifacts(row)
                return None
            return self._reconcile_while_locked(row, lock_fd)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)

    def _reconcile_while_locked(
        self, row: _LaunchRow, lock_fd: int
    ) -> WorkerTerminalProof | None:
        current = self._required_row(row.launch_token)
        request = self._required_process_request(row)
        self._validate_replay(
            current, row.binding_id, row.evidence_fingerprint, request
        )
        if current.terminal is not None:
            return current.terminal
        metadata = os.fstat(lock_fd)
        if metadata.st_dev != row.lock_device or metadata.st_ino != row.lock_inode:
            raise RealmIntegrityError("local process lock identity changed.")
        if current.launch_state == "reserved":
            # A reservation is deliberately passive.  Even an unlocked lock
            # is not evidence that a physical start was attempted.
            self._validate_reserved_artifacts(current)
            return None
        manifest_present = self._validate_manifest_if_present(current)
        self._validate_stop_if_present(current)
        handshake = self._read_handshake_if_present(current)
        result = self._read_result_if_present(current)
        if handshake is not None and not manifest_present:
            raise RealmIntegrityError(
                "handshaken local process is missing its launch manifest."
            )
        if handshake is None:
            if result is None:
                proof = self._new_proof(current, disposition="never_started")
                self._publish_never_started_result(current, proof)
                result = self._read_result_if_present(current)
                if result is None:  # pragma: no cover - publication is synchronous
                    raise RealmIntegrityError(
                        "local process never-started result was not published."
                    )
            proof = self._validate_result(current, result, None)
        else:
            if result is None:
                raise RealmIntegrityError(
                    "handshaken local process released its lock without an "
                    "authenticated terminal result."
                )
            proof = self._validate_result(current, result, handshake)
        return self._commit_terminal(current, proof)

    def _reservation_during_concurrent_commit(
        self,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        request: ProcessLaunchRequest,
    ) -> ProcessLaunchReservation:
        deadline = self._monotonic() + 5.0
        while True:
            row = self._read_row(launch_token)
            seal = self._read_launch_seal(launch_token)
            if row is not None and seal is not None:
                raise RealmIntegrityError(
                    "local process launch and negative seal coexist."
                )
            if seal is not None:
                raise RealmConflict(
                    "local process launch coordinate is unavailable."
                )
            if row is not None:
                self._validate_replay(
                    row, binding_id, evidence_fingerprint, request
                )
                return self._reservation_from_row(row)
            if self._monotonic() >= deadline:
                raise RealmIntegrityError(
                    "local process lock is held without a committed startup intent."
                )
            time.sleep(self._poll_interval)

    @staticmethod
    def _reservation_from_row(row: _LaunchRow) -> ProcessLaunchReservation:
        return ProcessLaunchReservation(
            launch_token=row.launch_token,
            binding_id=row.binding_id,
            evidence_fingerprint=row.evidence_fingerprint,
            backend_token=row.backend_token,
            launch_request_digest=row.launch_request_digest,
            provider_generation=row.provider_generation,
        )

    def _row_for_reservation(
        self, reservation: ProcessLaunchReservation
    ) -> _LaunchRow:
        if not isinstance(reservation, ProcessLaunchReservation):
            raise TypeError("reservation must be a ProcessLaunchReservation.")
        row = self._required_row(reservation.launch_token)
        self._validate_reservation(row, reservation)
        return row

    @staticmethod
    def _validate_reservation(
        row: _LaunchRow, reservation: ProcessLaunchReservation
    ) -> None:
        if (
            row.launch_token != reservation.launch_token
            or row.binding_id != reservation.binding_id
            or row.evidence_fingerprint != reservation.evidence_fingerprint
            or row.backend_token != reservation.backend_token
            or row.launch_request_digest != reservation.launch_request_digest
            or row.provider_generation != reservation.provider_generation
        ):
            raise RealmConflict(
                "process reservation differs from provider registry."
            )

    def _lookup_exact_row(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> _LaunchRow:
        launch_token = _safe_id(launch_token, "worker launch token")
        binding_id = _safe_id(binding_id, "worker binding id")
        evidence_fingerprint = lower_hex_digest(
            evidence_fingerprint, "worker evidence fingerprint"
        )
        launch_request_digest = lower_hex_digest(
            launch_request_digest, "worker launch request digest"
        )
        row = self._required_row(launch_token)
        if (
            row.binding_id != binding_id
            or row.evidence_fingerprint != evidence_fingerprint
            or row.launch_request_digest != launch_request_digest
        ):
            raise ProcessRecoveryCoordinateMismatch(
                "local process recovery coordinates do not match startup intent."
            )
        return row

    @staticmethod
    def _validate_exact_coordinates(current: _LaunchRow, expected: _LaunchRow) -> None:
        if (
            LocalProcessSupervisor._reservation_from_row(current)
            != LocalProcessSupervisor._reservation_from_row(expected)
            or current.coordinate != expected.coordinate
            or current.lock_device != expected.lock_device
            or current.lock_inode != expected.lock_inode
        ):
            raise RealmIntegrityError(
                "local process launch identity changed during endpoint operation."
            )

    @staticmethod
    def _unix_socket_path(path: str | os.PathLike[str]) -> Path:
        try:
            raw_path = os.fspath(path)
        except TypeError as error:
            raise TypeError("UNIX socket path must be path-like.") from error
        if not isinstance(raw_path, str):
            raise TypeError("UNIX socket path must be text.")
        _process_text(raw_path, "UNIX socket path", nonempty=True)
        selected = Path(raw_path)
        if (
            not selected.is_absolute()
            or selected.name in {"", ".", ".."}
            or ".." in selected.parts
            or str(selected) != raw_path
        ):
            raise ValueError("UNIX socket path must be absolute and normalized.")
        return selected

    @staticmethod
    def _unix_socket_path_digest(path: str) -> str:
        return hashlib.sha256(
            b"optpilot/local-process-unix-socket-path/v1\0" + os.fsencode(path)
        ).hexdigest()

    @staticmethod
    def _inspect_ready_unix_socket(path: Path) -> tuple[os.stat_result, os.stat_result]:
        try:
            parent = path.parent.lstat()
            endpoint = path.lstat()
        except OSError as error:
            raise RealmIntegrityError(
                "ready UNIX socket identity cannot be inspected."
            ) from error
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise RealmIntegrityError(
                "ready UNIX socket parent must be a private owned directory."
            )
        if (
            not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid()
            or stat.S_IMODE(endpoint.st_mode) != 0o600
        ):
            raise RealmIntegrityError(
                "ready UNIX socket must be an owned 0600 socket."
            )
        return parent, endpoint

    def _read_unix_socket_endpoint(
        self, *, launch_token: str, endpoint_name: str
    ) -> _UnixSocketEndpointRow | None:
        with self._connect() as connection:
            record = connection.execute(
                """
                SELECT * FROM process_unix_socket_endpoints
                WHERE launch_token = ? AND endpoint_name = ?
                """,
                (launch_token, endpoint_name),
            ).fetchone()
        return (
            None
            if record is None
            else self._decode_unix_socket_endpoint(record)
        )

    def _list_unix_socket_endpoints(
        self, launch_token: str
    ) -> tuple[_UnixSocketEndpointRow, ...]:
        with self._connect() as connection:
            records = connection.execute(
                """
                SELECT * FROM process_unix_socket_endpoints
                WHERE launch_token = ? ORDER BY endpoint_name
                """,
                (launch_token,),
            ).fetchall()
        return tuple(self._decode_unix_socket_endpoint(record) for record in records)

    def _decode_unix_socket_endpoint(
        self, record: sqlite3.Row
    ) -> _UnixSocketEndpointRow:
        try:
            launch_token = _safe_id(
                record["launch_token"], "endpoint launch token"
            )
            endpoint_name = _safe_id(
                record["endpoint_name"], "process endpoint name"
            )
            if record["endpoint_kind"] != "unix_socket":
                raise ValueError("endpoint kind is invalid")
            state = cast(ProcessEndpointState, record["state"])
            if state not in {"recorded", "reconciled"}:
                raise ValueError("endpoint state is invalid")
            path = record["path"]
            if state == "recorded":
                if not isinstance(path, str):
                    raise ValueError("recorded endpoint path is absent")
                selected_path = self._unix_socket_path(path)
                if str(selected_path) != path:
                    raise ValueError("recorded endpoint path is not normalized")
            elif path is not None:
                raise ValueError("reconciled endpoint path was not redacted")
            path_digest = lower_hex_digest(
                record["path_digest"], "UNIX socket path digest"
            )
            if path is not None and path_digest != self._unix_socket_path_digest(path):
                raise ValueError("endpoint path digest differs")
            parent_device = _nonnegative_int(
                record["parent_device"], "UNIX socket parent device id"
            )
            parent_inode = _positive_int(
                record["parent_inode"], "UNIX socket parent inode"
            )
            socket_device = _nonnegative_int(
                record["socket_device"], "UNIX socket device id"
            )
            socket_inode = _positive_int(
                record["socket_inode"], "UNIX socket inode"
            )
            finite_time(float(record["created_at"]), "endpoint creation time")
            finite_time(float(record["updated_at"]), "endpoint update time")
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "local process endpoint registry row is invalid."
            ) from error
        return _UnixSocketEndpointRow(
            launch_token=launch_token,
            endpoint_name=endpoint_name,
            path=path,
            path_digest=path_digest,
            parent_device=parent_device,
            parent_inode=parent_inode,
            socket_device=socket_device,
            socket_inode=socket_inode,
            state=state,
        )

    @staticmethod
    def _endpoint_registration(
        launch: _LaunchRow, endpoint: _UnixSocketEndpointRow
    ) -> ProcessUnixSocketRegistration:
        if endpoint.launch_token != launch.launch_token:
            raise RealmIntegrityError(
                "local process endpoint belongs to another launch."
            )
        return ProcessUnixSocketRegistration(
            launch_token=launch.launch_token,
            binding_id=launch.binding_id,
            evidence_fingerprint=launch.evidence_fingerprint,
            launch_request_digest=launch.launch_request_digest,
            endpoint_name=endpoint.endpoint_name,
            state=endpoint.state,
        )

    def _reconcile_unix_socket_endpoints(self, launch: _LaunchRow) -> None:
        for endpoint in self._list_unix_socket_endpoints(launch.launch_token):
            if endpoint.state == "reconciled":
                continue
            if launch.retired:
                raise RealmIntegrityError(
                    "retired local process has an unreconciled endpoint."
                )
            if endpoint.path is None:  # pragma: no cover - decoder enforces
                raise RealmIntegrityError(
                    "recorded local process endpoint has no path."
                )
            path = self._unix_socket_path(endpoint.path)
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                parent_fd = os.open(path.parent, flags)
            except OSError as error:
                raise RealmIntegrityError(
                    "recorded UNIX socket parent cannot be opened."
                ) from error
            try:
                parent = os.fstat(parent_fd)
                if (
                    not stat.S_ISDIR(parent.st_mode)
                    or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700
                    or parent.st_dev != endpoint.parent_device
                    or parent.st_ino != endpoint.parent_inode
                ):
                    raise RealmIntegrityError(
                        "recorded UNIX socket parent identity changed."
                    )
                try:
                    current = os.stat(
                        path.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    current = None
                except OSError as error:
                    raise RealmIntegrityError(
                        "recorded UNIX socket cannot be inspected."
                    ) from error
                if current is not None:
                    if (
                        not stat.S_ISSOCK(current.st_mode)
                        or current.st_uid != os.getuid()
                        or stat.S_IMODE(current.st_mode) != 0o600
                        or current.st_dev != endpoint.socket_device
                        or current.st_ino != endpoint.socket_inode
                    ):
                        raise RealmIntegrityError(
                            "recorded UNIX socket identity changed."
                        )
                    try:
                        os.unlink(path.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError as error:
                        raise RealmIntegrityError(
                            "recorded UNIX socket could not be unlinked."
                        ) from error
                    try:
                        os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RealmIntegrityError(
                            "recorded UNIX socket remained after unlink."
                        )
                    if self._fault_injector is not None:
                        self._fault_injector("endpoint_unlinked")
            finally:
                os.close(parent_fd)
            self._mark_unix_socket_endpoint_reconciled(endpoint)

    def _mark_unix_socket_endpoint_reconciled(
        self, expected: _UnixSocketEndpointRow
    ) -> None:
        now = float(self._clock())
        finite_time(now, "local process endpoint reconciliation time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                """
                SELECT * FROM process_unix_socket_endpoints
                WHERE launch_token = ? AND endpoint_name = ?
                """,
                (expected.launch_token, expected.endpoint_name),
            ).fetchone()
            if record is None:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process endpoint disappeared during reconciliation."
                )
            current = self._decode_unix_socket_endpoint(record)
            if current.state == "reconciled":
                connection.rollback()
                if (
                    current.path_digest != expected.path_digest
                    or current.parent_device != expected.parent_device
                    or current.parent_inode != expected.parent_inode
                    or current.socket_device != expected.socket_device
                    or current.socket_inode != expected.socket_inode
                ):
                    raise RealmIntegrityError(
                        "local process endpoint changed across reconciliation."
                    )
                return
            if current != expected:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process endpoint changed during reconciliation."
                )
            cursor = connection.execute(
                """
                UPDATE process_unix_socket_endpoints
                SET path = NULL, state = 'reconciled', updated_at = ?
                WHERE launch_token = ? AND endpoint_name = ?
                  AND state = 'recorded' AND path_digest = ?
                """,
                (
                    now,
                    expected.launch_token,
                    expected.endpoint_name,
                    expected.path_digest,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process endpoint changed during reconciliation."
                )
            connection.commit()

    def _require_unix_socket_endpoints_reconciled(self, launch_token: str) -> None:
        pending = tuple(
            endpoint
            for endpoint in self._list_unix_socket_endpoints(launch_token)
            if endpoint.state != "reconciled"
        )
        if pending:
            raise RealmConflict("local process endpoint cleanup remains pending.")

    def _recover_reserved_abandonment_if_present(
        self, row: _LaunchRow
    ) -> WorkerTerminalProof | None:
        if row.launch_state != "reserved":
            raise RealmConflict("local process start was already requested.")
        return self._validate_reserved_artifacts(row)

    def _validate_reserved_artifacts(
        self, row: _LaunchRow
    ) -> WorkerTerminalProof | None:
        launch_dir = self._launch_directory(row.coordinate)
        unexpected = [
            name
            for name in ("manifest.json", "handshake.json", "stop.json")
            if os.path.lexists(launch_dir / name)
        ]
        if unexpected:
            raise RealmIntegrityError(
                "reserved local process has unexpected launch artifacts."
            )
        result = self._read_result_if_present(row)
        if result is None:
            return None
        proof = self._validate_result(row, result, None)
        if proof.disposition != "never_started":  # defensive; validator agrees
            raise RealmIntegrityError(
                "reserved local process result must be never_started."
            )
        return proof

    def _validate_replay(
        self,
        row: _LaunchRow,
        binding_id: str,
        evidence_fingerprint: str,
        request: ProcessLaunchRequest,
    ) -> None:
        if (
            row.binding_id != binding_id
            or row.evidence_fingerprint != evidence_fingerprint
            or row.launch_request_digest != request.digest
            or (
                not row.retired
                and self._required_process_request(row).canonical_bytes
                != request.canonical_bytes
            )
        ):
            raise RealmConflict(
                "launch token is already bound to a different process request."
            )

    def _validate_live_artifacts(self, row: _LaunchRow) -> None:
        manifest_present = self._validate_manifest_if_present(row)
        self._validate_stop_if_present(row)
        handshake = self._read_handshake_if_present(row)
        result = self._read_result_if_present(row)
        if handshake is not None and not manifest_present:
            raise RealmIntegrityError(
                "handshaken local process is missing its launch manifest."
            )
        if result is not None:
            self._validate_result(row, result, handshake)

    def _validate_manifest_if_present(self, row: _LaunchRow) -> bool:
        path = self._launch_directory(row.coordinate) / "manifest.json"
        if not os.path.lexists(path):
            return False
        payload = self._read_record(path)
        expected = self._manifest(row)
        if payload != expected:
            raise RealmIntegrityError("local process launch manifest was changed.")
        return True

    def _read_handshake_if_present(self, row: _LaunchRow) -> JsonDict | None:
        path = self._launch_directory(row.coordinate) / "handshake.json"
        if not os.path.lexists(path):
            return None
        payload = self._read_record(path)
        expected_keys = set(self._identity_fields(row)) | {
            "schema",
            "worker_pgid",
            "worker_pid",
            "wrapper_pid",
        }
        if set(payload) != expected_keys or payload.get("schema") != _HANDSHAKE_SCHEMA:
            raise RealmIntegrityError("local process handshake fields differ.")
        for key, value in self._identity_fields(row).items():
            if payload.get(key) != value:
                raise RealmIntegrityError(
                    "local process handshake identity does not match startup intent."
                )
        worker_pid = payload.get("worker_pid")
        worker_pgid = payload.get("worker_pgid")
        wrapper_pid = payload.get("wrapper_pid")
        if (
            isinstance(worker_pid, bool)
            or not isinstance(worker_pid, int)
            or worker_pid <= 1
            or worker_pgid != worker_pid
            or isinstance(wrapper_pid, bool)
            or not isinstance(wrapper_pid, int)
            or wrapper_pid <= 1
            or wrapper_pid == worker_pid
        ):
            raise RealmIntegrityError("local process handshake PID fields are invalid.")
        return payload

    def _read_result_if_present(self, row: _LaunchRow) -> JsonDict | None:
        path = self._launch_directory(row.coordinate) / "result.json"
        if not os.path.lexists(path):
            return None
        return self._read_record(path)

    def _validate_result(
        self, row: _LaunchRow, payload: JsonDict, handshake: JsonDict | None
    ) -> WorkerTerminalProof:
        self._validate_stop_if_present(row)
        if payload.get("schema") == _NEVER_STARTED_RESULT_SCHEMA:
            expected_keys = set(self._identity_fields(row)) | {"proof", "schema"}
            if set(payload) != expected_keys or handshake is not None:
                raise RealmIntegrityError(
                    "local process never-started result fields differ."
                )
            for key, value in self._identity_fields(row).items():
                if payload.get(key) != value:
                    raise RealmIntegrityError(
                        "local process never-started result identity does not "
                        "match startup intent."
                    )
            try:
                proof = WorkerTerminalProof.from_dict(payload["proof"])
            except (KeyError, TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    "local process never-started proof is invalid."
                ) from error
            self._validate_proof_identity(row, proof)
            if proof.disposition != "never_started":
                raise RealmIntegrityError(
                    "local process never-started result has the wrong disposition."
                )
            return proof

        if handshake is None:
            raise RealmIntegrityError(
                "local process wrapper result exists without an authenticated handshake."
            )
        if payload.get("schema") == _RECOVERED_RESULT_SCHEMA:
            expected_keys = set(self._identity_fields(row)) | {
                "proof",
                "recovery_reason",
                "schema",
                "worker_pgid",
                "worker_pid",
            }
            if set(payload) != expected_keys:
                raise RealmIntegrityError(
                    "recovered local process result fields differ."
                )
            for key, value in self._identity_fields(row).items():
                if payload.get(key) != value:
                    raise RealmIntegrityError(
                        "recovered local process result identity does not match "
                        "startup intent."
                    )
            if (
                payload.get("worker_pid") != handshake["worker_pid"]
                or payload.get("worker_pgid") != handshake["worker_pgid"]
                or payload.get("recovery_reason") != "primary_monitor_lost"
            ):
                raise RealmIntegrityError(
                    "recovered local process result does not match its "
                    "authenticated handshake."
                )
            try:
                proof = WorkerTerminalProof.from_dict(payload["proof"])
            except (KeyError, TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    "recovered local process terminal proof is invalid."
                ) from error
            self._validate_proof_identity(row, proof)
            if proof.disposition != "killed":
                raise RealmIntegrityError(
                    "monitor-loss recovery must have a killed disposition."
                )
            return proof

        expected_keys = set(self._identity_fields(row)) | {
            "proof",
            "schema",
            "wait_status",
            "worker_pgid",
            "worker_pid",
        }
        if set(payload) != expected_keys or payload.get("schema") != _RESULT_SCHEMA:
            raise RealmIntegrityError("local process result fields differ.")
        for key, value in self._identity_fields(row).items():
            if payload.get(key) != value:
                raise RealmIntegrityError(
                    "local process result identity does not match startup intent."
                )
        if (
            payload.get("worker_pid") != handshake["worker_pid"]
            or payload.get("worker_pgid") != handshake["worker_pgid"]
        ):
            raise RealmIntegrityError(
                "local process result does not match its authenticated handshake."
            )
        wait_status = payload.get("wait_status")
        if isinstance(wait_status, bool) or not isinstance(wait_status, int) or wait_status < 0:
            raise RealmIntegrityError("local process wait status is invalid.")
        try:
            proof = WorkerTerminalProof.from_dict(payload["proof"])
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("local process terminal proof is invalid.") from error
        self._validate_proof_identity(row, proof)
        if proof.disposition == "never_started":
            raise RealmIntegrityError(
                "a handshaken local process cannot report never_started."
            )
        return proof

    def _publish_never_started_result(
        self, row: _LaunchRow, proof: WorkerTerminalProof
    ) -> None:
        path = self._launch_directory(row.coordinate) / "result.json"
        payload = {
            **self._identity_fields(row),
            "proof": proof.to_dict(),
            "schema": _NEVER_STARTED_RESULT_SCHEMA,
        }
        try:
            self._publish_record(path, payload)
        except FileExistsError:
            if self._read_record(path) != payload:
                raise RealmIntegrityError(
                    "local process never-started result was changed."
                )

    def _validate_proof_identity(
        self, row: _LaunchRow, proof: WorkerTerminalProof
    ) -> None:
        if (
            proof.launch_token != row.launch_token
            or proof.binding_id != row.binding_id
            or proof.evidence_fingerprint != row.evidence_fingerprint
            or proof.backend_token != row.backend_token
            or proof.launch_request_digest != row.launch_request_digest
            or proof.provider_generation != row.provider_generation
        ):
            raise RealmIntegrityError(
                "local process terminal proof does not match startup intent."
            )

    def _new_proof(
        self, row: _LaunchRow, *, disposition: WorkerDisposition
    ) -> WorkerTerminalProof:
        return WorkerTerminalProof(
            launch_token=row.launch_token,
            binding_id=row.binding_id,
            evidence_fingerprint=row.evidence_fingerprint,
            backend_token=row.backend_token,
            launch_request_digest=row.launch_request_digest,
            disposition=disposition,
            provider_generation=row.provider_generation,
            terminal_at=float(self._clock()),
        )

    @staticmethod
    def _new_started(row: _LaunchRow) -> WorkerStarted:
        return WorkerStarted(
            launch_token=row.launch_token,
            binding_id=row.binding_id,
            evidence_fingerprint=row.evidence_fingerprint,
            backend_token=row.backend_token,
            launch_request_digest=row.launch_request_digest,
            provider_generation=row.provider_generation,
        )

    def _manifest(self, row: _LaunchRow) -> JsonDict:
        launch_dir = self._launch_directory(row.coordinate)
        request = self._required_process_request(row)
        return {
            **self._identity_fields(row),
            "coordinate": row.coordinate,
            "handshake_path": str(launch_dir / "handshake.json"),
            "launch_request": request.to_dict(),
            "result_path": str(launch_dir / "result.json"),
            "schema": _MANIFEST_SCHEMA,
            "stop_path": str(launch_dir / "stop.json"),
        }

    @staticmethod
    def _required_process_request(row: _LaunchRow) -> ProcessLaunchRequest:
        if row.retired or row.request is None:
            raise RealmIntegrityError(
                "retired local process launch has no executable request."
            )
        return row.request

    def _identity_fields(self, row: _LaunchRow) -> JsonDict:
        return {
            "backend_token": row.backend_token,
            "binding_id": row.binding_id,
            "evidence_fingerprint": row.evidence_fingerprint,
            "launch_request_digest": row.launch_request_digest,
            "launch_token": row.launch_token,
            "lock_device": row.lock_device,
            "lock_inode": row.lock_inode,
            "provider_generation": row.provider_generation,
        }

    def _publish_or_validate_stop(
        self, row: _LaunchRow, grace_period: float
    ) -> None:
        path = self._launch_directory(row.coordinate) / "stop.json"
        payload = {
            **self._identity_fields(row),
            "grace_period": grace_period,
            "schema": _STOP_SCHEMA,
        }
        try:
            self._publish_record(path, payload)
        except FileExistsError:
            self._validate_stop_record(row, self._read_record(path))

    def _validate_stop_if_present(self, row: _LaunchRow) -> bool:
        path = self._launch_directory(row.coordinate) / "stop.json"
        if not os.path.lexists(path):
            return False
        self._validate_stop_record(row, self._read_record(path))
        return True

    def _validate_stop_record(self, row: _LaunchRow, payload: JsonDict) -> None:
        expected_keys = set(self._identity_fields(row)) | {
            "grace_period",
            "schema",
        }
        if (
            set(payload) != expected_keys
            or payload.get("schema") != _STOP_SCHEMA
            or not row.stop_requested
        ):
            raise RealmIntegrityError(
                "local process stop record does not match durable stop intent."
            )
        for key, value in self._identity_fields(row).items():
            if payload.get(key) != value:
                raise RealmIntegrityError(
                    "local process stop record does not match durable stop intent."
                )
        grace_period = payload.get("grace_period")
        if (
            isinstance(grace_period, bool)
            or not isinstance(grace_period, (int, float))
            or not float("-inf") < float(grace_period) < float("inf")
            or grace_period < 0
        ):
            raise RealmIntegrityError(
                "local process stop record grace period is invalid."
            )

    def _spawn_wrapper(
        self,
        manifest_path: Path,
        lock_fd: int,
        *,
        request: ProcessLaunchRequest,
        private_values: Mapping[str, str] | None,
    ) -> subprocess.Popen[bytes]:
        wrapper_path = Path(__file__).with_name("_local_process_wrapper.py")
        private_read_fd = -1
        private_write_fd = -1
        private_payload: bytearray | None = None
        if request.private_env_names:
            if private_values is None or tuple(sorted(private_values)) != (
                request.private_env_names
            ):
                raise RealmIntegrityError(
                    "private process environment values differ from the request."
                )
            private_payload = bytearray(
                canonical_json_bytes(
                    {
                        "binding_revision": (
                            request.private_env_binding_revision
                        ),
                        "env": dict(private_values),
                        "schema": _PRIVATE_ENVIRONMENT_SCHEMA,
                    }
                )
            )
            if len(private_payload) > _MAX_RECORD_BYTES:
                raise ValueError(
                    "private process environment payload exceeds its size bound."
                )
            private_read_fd, private_write_fd = os.pipe()
            os.set_inheritable(private_read_fd, True)
        elif private_values:
            raise RealmIntegrityError(
                "private process environment values have no request descriptor."
            )
        arguments = [
            sys.executable,
            str(wrapper_path),
            str(manifest_path),
            str(lock_fd),
            str(private_read_fd),
        ]
        if self._wrapper_fault_mode is not None:
            arguments.append(self._wrapper_fault_mode)
        pass_fds = (
            (lock_fd, private_read_fd)
            if private_read_fd >= 0
            else (lock_fd,)
        )
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
                env={},
            )
            if private_read_fd >= 0:
                os.close(private_read_fd)
                private_read_fd = -1
            if private_payload is not None:
                framed = bytearray(len(private_payload).to_bytes(8, "big"))
                framed.extend(private_payload)
                try:
                    view = memoryview(framed)
                    while view:
                        written = os.write(private_write_fd, view)
                        if written <= 0:
                            raise RuntimeError(
                                "short private process environment pipe write."
                            )
                        view = view[written:]
                finally:
                    for index in range(len(framed)):
                        framed[index] = 0
            return process
        except Exception:
            if process is not None:
                self._track_wrapper(process)
            raise
        finally:
            if private_read_fd >= 0:
                os.close(private_read_fd)
            if private_write_fd >= 0:
                os.close(private_write_fd)
            if private_payload is not None:
                for index in range(len(private_payload)):
                    private_payload[index] = 0

    @staticmethod
    def _track_wrapper(process: subprocess.Popen[bytes]) -> None:
        with _WRAPPER_PROCESSES_LOCK:
            _WRAPPER_PROCESSES[process.pid] = process

        def reap() -> None:
            try:
                process.wait()
            finally:
                with _WRAPPER_PROCESSES_LOCK:
                    _WRAPPER_PROCESSES.pop(process.pid, None)

        threading.Thread(
            target=reap,
            name=f"optpilot-process-wrapper-{process.pid}",
            daemon=True,
        ).start()

    def _acquire_realization_gate(
        self,
        *,
        launch_token: str,
        exclusive: bool,
        timeout: float | None,
    ) -> tuple[int, os.stat_result]:
        wait = _optional_nonnegative_finite_number(
            timeout, "realization gate timeout"
        )
        coordinate = hashlib.sha256(
            b"optpilot/local-process-coordinate/v1\0"
            + launch_token.encode("ascii")
        ).hexdigest()
        path = self._realization_gates_root / coordinate
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            deadline = None if wait is None else self._monotonic() + wait
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if deadline is not None and self._monotonic() >= deadline:
                        raise TimeoutError(
                            "timed out waiting for local process realization gate."
                        )
                    time.sleep(self._poll_interval)
            metadata = os.fstat(descriptor)
            path_metadata = os.lstat(path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != path_metadata.st_dev
                or metadata.st_ino != path_metadata.st_ino
            ):
                raise RealmIntegrityError(
                    "local process realization gate identity is invalid."
                )
            return descriptor, metadata
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            raise

    def _validate_realization_claim(
        self, claim: ProcessLaunchRealizationClaim
    ) -> None:
        if not isinstance(claim, ProcessLaunchRealizationClaim):
            raise TypeError("claim must be a ProcessLaunchRealizationClaim.")
        if claim.released:
            raise RealmConflict("process realization claim was released.")
        if claim.provider_generation != self.provider_generation:
            raise RealmConflict(
                "process realization claim belongs to another provider generation."
            )
        try:
            metadata = os.fstat(claim._descriptor)
            coordinate = hashlib.sha256(
                b"optpilot/local-process-coordinate/v1\0"
                + claim.launch_token.encode("ascii")
            ).hexdigest()
            path_metadata = os.lstat(self._realization_gates_root / coordinate)
        except OSError as error:
            raise RealmIntegrityError(
                "local process realization gate is unavailable."
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_dev != claim._gate_device
            or metadata.st_ino != claim._gate_inode
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise RealmIntegrityError(
                "local process realization claim identity changed."
            )

    def _initialize_private_root(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._launches_root.mkdir(mode=0o700, exist_ok=True)
        self._realization_gates_root.mkdir(mode=0o700, exist_ok=True)
        for path in (self.root, self._launches_root, self._realization_gates_root):
            metadata = os.lstat(path)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise RealmIntegrityError(
                    "local process provider root must be a real directory."
                )
            if metadata.st_uid != os.getuid():
                raise RealmIntegrityError(
                    "local process provider root must be owned by the current user."
                )
            os.chmod(path, 0o700)

    def _initialize_registry(self) -> None:
        if os.path.lexists(self.database_path):
            metadata = os.lstat(self.database_path)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise RealmIntegrityError(
                    "local process registry must be a regular file."
                )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version_row = connection.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0])
            if version > _PRIVATE_REGISTRY_SCHEMA_VERSION:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process registry schema is newer than this provider."
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    next_generation INTEGER NOT NULL CHECK (next_generation > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS process_launches (
                    launch_token TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    backend_token TEXT NOT NULL,
                    request_json BLOB NOT NULL,
                    launch_request_digest TEXT NOT NULL,
                    provider_generation INTEGER NOT NULL CHECK (provider_generation > 0),
                    coordinate TEXT NOT NULL UNIQUE,
                    lock_device INTEGER NOT NULL,
                    lock_inode INTEGER NOT NULL,
                    launch_state TEXT NOT NULL DEFAULT 'reserved'
                        CHECK (launch_state IN ('reserved', 'start_requested')),
                    stop_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (stop_requested IN (0, 1)),
                    terminal_json BLOB,
                    retired INTEGER NOT NULL DEFAULT 0
                        CHECK (retired IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS process_launch_seals (
                    launch_token TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS process_unix_socket_endpoints (
                    launch_token TEXT NOT NULL,
                    endpoint_name TEXT NOT NULL,
                    endpoint_kind TEXT NOT NULL DEFAULT 'unix_socket'
                        CHECK (endpoint_kind = 'unix_socket'),
                    path TEXT,
                    path_digest TEXT NOT NULL UNIQUE,
                    parent_device INTEGER NOT NULL CHECK (parent_device >= 0),
                    parent_inode INTEGER NOT NULL CHECK (parent_inode > 0),
                    socket_device INTEGER NOT NULL CHECK (socket_device >= 0),
                    socket_inode INTEGER NOT NULL CHECK (socket_inode > 0),
                    state TEXT NOT NULL
                        CHECK (state IN ('recorded', 'reconciled')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (launch_token, endpoint_name),
                    FOREIGN KEY (launch_token) REFERENCES process_launches(launch_token),
                    CHECK (
                        (state = 'recorded' AND path IS NOT NULL)
                        OR (state = 'reconciled' AND path IS NULL)
                    )
                )
                """
            )
            columns = {
                column["name"]
                for column in connection.execute(
                    "PRAGMA table_info(process_launches)"
                ).fetchall()
            }
            if "launch_state" not in columns:
                if version >= _PRIVATE_REGISTRY_SCHEMA_VERSION:
                    connection.rollback()
                    raise RealmIntegrityError(
                        "local process registry schema version is inconsistent."
                    )
                # Every pre-v2 row was an already-committed launch intent.  It
                # must retain the old reconciliation behavior and can never be
                # reclassified as a passive reservation.
                connection.execute(
                    """
                    ALTER TABLE process_launches
                    ADD COLUMN launch_state TEXT NOT NULL DEFAULT 'start_requested'
                        CHECK (launch_state IN ('reserved', 'start_requested'))
                    """
                )
            if "retired" not in columns:
                if version >= _PRIVATE_REGISTRY_SCHEMA_VERSION:
                    connection.rollback()
                    raise RealmIntegrityError(
                        "local process registry schema version is inconsistent."
                    )
                connection.execute(
                    """
                    ALTER TABLE process_launches
                    ADD COLUMN retired INTEGER NOT NULL DEFAULT 0
                        CHECK (retired IN (0, 1))
                    """
                )
            connection.execute(
                f"PRAGMA user_version = {_PRIVATE_REGISTRY_SCHEMA_VERSION}"
            )
            connection.commit()
        os.chmod(self.database_path, 0o600)

    def _claim_provider_generation(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_generation FROM provider_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                generation = 1
                connection.execute(
                    "INSERT INTO provider_metadata(singleton, next_generation) VALUES (1, 2)"
                )
            else:
                generation = row["next_generation"]
                positive_int(generation, "local process provider generation")
                connection.execute(
                    "UPDATE provider_metadata SET next_generation = ? WHERE singleton = 1",
                    (generation + 1,),
                )
            connection.commit()
            return generation

    def _insert_reservation(self, row: _LaunchRow) -> _ReservationInsertResult:
        if row.request is None or row.retired:
            raise RealmIntegrityError(
                "new local process reservation has no executable request."
            )
        now = float(self._clock())
        finite_time(now, "local process startup time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            seal_record = connection.execute(
                "SELECT * FROM process_launch_seals WHERE launch_token = ?",
                (row.launch_token,),
            ).fetchone()
            if seal_record is not None:
                seal = self._decode_launch_seal(seal_record)
                connection.rollback()
                if seal.binding_id != row.binding_id:
                    return "sealed"
                return "sealed"
            try:
                connection.execute(
                    """
                    INSERT INTO process_launches(
                        launch_token, binding_id, evidence_fingerprint, backend_token,
                        request_json, launch_request_digest, provider_generation,
                        coordinate, lock_device, lock_inode, launch_state,
                        stop_requested, terminal_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                    """,
                    (
                        row.launch_token,
                        row.binding_id,
                        row.evidence_fingerprint,
                        row.backend_token,
                        row.request.canonical_bytes,
                        row.launch_request_digest,
                        row.provider_generation,
                        row.coordinate,
                        row.lock_device,
                        row.lock_inode,
                        row.launch_state,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return "launch_conflict"
            connection.commit()
            return "inserted"

    def _mark_start_requested(
        self, reservation: ProcessLaunchReservation
    ) -> _LaunchRow:
        if not isinstance(reservation, ProcessLaunchReservation):
            raise TypeError("reservation must be a ProcessLaunchReservation.")
        now = float(self._clock())
        finite_time(now, "local process start request time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (reservation.launch_token,),
            ).fetchone()
            if persisted is None:
                connection.rollback()
                raise RealmNotFound("local process launch does not exist.")
            current = self._decode_row(persisted)
            self._validate_reservation(current, reservation)
            if (
                current.terminal is not None
                or current.launch_state == "start_requested"
            ):
                connection.rollback()
                return current
            cursor = connection.execute(
                """
                UPDATE process_launches
                SET launch_state = 'start_requested', updated_at = ?
                WHERE launch_token = ?
                  AND launch_state = 'reserved'
                  AND terminal_json IS NULL
                """,
                (now, reservation.launch_token),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE serializes
                connection.rollback()
                raise RealmIntegrityError(
                    "local process reservation changed during start commit."
                )
            connection.commit()
        return self._row_for_reservation(reservation)

    def _commit_terminal(
        self, row: _LaunchRow, proof: WorkerTerminalProof
    ) -> WorkerTerminalProof:
        self._validate_proof_identity(row, proof)
        if row.launch_state == "reserved" and proof.disposition != "never_started":
            raise RealmIntegrityError(
                "reserved local process terminal proof must be never_started."
            )
        encoded = proof.canonical_bytes
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (row.launch_token,),
            ).fetchone()
            if persisted is None:
                connection.rollback()
                raise RealmNotFound("local process launch does not exist.")
            current = self._decode_row(persisted)
            if (
                self._reservation_from_row(current)
                != self._reservation_from_row(row)
                or current.coordinate != row.coordinate
                or current.lock_device != row.lock_device
                or current.lock_inode != row.lock_inode
                or current.launch_state != row.launch_state
            ):
                connection.rollback()
                raise RealmIntegrityError(
                    "local process launch identity changed before terminal commit."
                )
            if current.terminal is not None:
                connection.rollback()
                if current.terminal != proof:
                    raise RealmIntegrityError(
                        "local process terminal proof changed across replay."
                    )
                return current.terminal
            cursor = connection.execute(
                """
                UPDATE process_launches
                SET terminal_json = ?, updated_at = ?
                WHERE launch_token = ?
                  AND launch_state = ?
                  AND terminal_json IS NULL
                """,
                (
                    encoded,
                    proof.terminal_at,
                    row.launch_token,
                    row.launch_state,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - transaction serializes
                connection.rollback()
                raise RealmIntegrityError(
                    "local process changed during terminal commit."
                )
            connection.commit()
            return proof

    def _mark_stop_requested(self, launch_token: str) -> _LaunchRow:
        launch_token = _safe_id(launch_token, "worker launch token")
        now = float(self._clock())
        finite_time(now, "local process stop request time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (launch_token,),
            ).fetchone()
            if persisted is None:
                connection.rollback()
                raise RealmNotFound("local process launch does not exist.")
            current = self._decode_row(persisted)
            if current.terminal is not None:
                connection.rollback()
                return current
            if current.launch_state == "reserved":
                connection.rollback()
                raise RealmConflict(
                    "reserved local process has not requested start."
                )
            cursor = connection.execute(
                """
                UPDATE process_launches
                SET stop_requested = 1, updated_at = ?
                WHERE launch_token = ?
                  AND launch_state = 'start_requested'
                  AND terminal_json IS NULL
                """,
                (now, launch_token),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RealmIntegrityError(
                    "local process changed during stop commit."
                )
            connection.commit()
        return self._required_row(launch_token)

    def _read_row(self, launch_token: str) -> _LaunchRow | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM process_launches WHERE launch_token = ?",
                (launch_token,),
            ).fetchone()
        return None if row is None else self._decode_row(row)

    def _read_launch_seal(self, launch_token: str) -> _LaunchSealRow | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM process_launch_seals WHERE launch_token = ?",
                (launch_token,),
            ).fetchone()
        return None if row is None else self._decode_launch_seal(row)

    def _required_row(self, launch_token: str) -> _LaunchRow:
        row = self._read_row(launch_token)
        if row is None:
            raise RealmNotFound("local process launch does not exist.")
        return row

    def _decode_row(self, row: sqlite3.Row) -> _LaunchRow:
        try:
            request_bytes = bytes(row["request_json"])
            request_payload = json.loads(request_bytes.decode("utf-8"))
            if canonical_json_bytes(request_payload) != request_bytes:
                raise ValueError("request is not canonical")
            if row["retired"] not in {0, 1}:
                raise ValueError("retired flag is invalid")
            retired = bool(row["retired"])
            if retired:
                _exact_keys(
                    request_payload,
                    {"launch_request_digest", "schema"},
                    "retired process request",
                )
                if request_payload["schema"] != _RETIRED_REQUEST_SCHEMA:
                    raise ValueError("retired request schema is invalid")
                request = None
            else:
                request = ProcessLaunchRequest.from_dict(request_payload)
            terminal = (
                None
                if row["terminal_json"] is None
                else self._proof_from_bytes(row["terminal_json"])
            )
            decoded = _LaunchRow(
                launch_token=_safe_id(row["launch_token"], "worker launch token"),
                binding_id=_safe_id(row["binding_id"], "worker binding id"),
                evidence_fingerprint=lower_hex_digest(
                    row["evidence_fingerprint"], "worker evidence fingerprint"
                ),
                backend_token=lower_hex_digest(
                    row["backend_token"], "worker backend token"
                ),
                request=request,
                launch_request_digest=lower_hex_digest(
                    row["launch_request_digest"], "worker launch request digest"
                ),
                provider_generation=positive_int(
                    row["provider_generation"], "worker provider generation"
                ),
                coordinate=lower_hex_digest(
                    row["coordinate"], "worker launch coordinate"
                ),
                lock_device=int(row["lock_device"]),
                lock_inode=int(row["lock_inode"]),
                launch_state=cast(LaunchState, row["launch_state"]),
                stop_requested=bool(row["stop_requested"]),
                terminal=terminal,
                retired=retired,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(
                "local process registry row is invalid."
            ) from error
        if decoded.retired:
            if (
                request_payload["launch_request_digest"]
                != decoded.launch_request_digest
                or decoded.terminal is None
            ):
                raise RealmIntegrityError(
                    "retired local process tombstone is inconsistent."
                )
        elif (
            decoded.request is None
            or decoded.launch_request_digest != decoded.request.digest
        ):
            raise RealmIntegrityError(
                "local process registry request digest does not match."
            )
        if row["stop_requested"] not in {0, 1}:
            raise RealmIntegrityError("local process stop flag is invalid.")
        if decoded.launch_state not in {"reserved", "start_requested"}:
            raise RealmIntegrityError("local process launch state is invalid.")
        if decoded.lock_device < 0 or decoded.lock_inode <= 0:
            raise RealmIntegrityError("local process lock identity is invalid.")
        if terminal is not None:
            self._validate_proof_identity(decoded, terminal)
        if decoded.launch_state == "reserved":
            if decoded.stop_requested:
                raise RealmIntegrityError(
                    "reserved local process cannot have a stop request."
                )
            if terminal is not None and terminal.disposition != "never_started":
                raise RealmIntegrityError(
                    "reserved local process terminal proof must be never_started."
                )
        return decoded

    @staticmethod
    def _decode_launch_seal(row: sqlite3.Row) -> _LaunchSealRow:
        try:
            return _LaunchSealRow(
                launch_token=_safe_id(
                    row["launch_token"], "sealed worker launch token"
                ),
                binding_id=_safe_id(row["binding_id"], "sealed worker binding id"),
                created_at=finite_time(
                    row["created_at"], "sealed worker creation time"
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "local process negative seal row is invalid."
            ) from error

    @staticmethod
    def _proof_from_bytes(value: Any) -> WorkerTerminalProof:
        try:
            encoded = bytes(value)
            payload = json.loads(encoded.decode("utf-8"))
            if canonical_json_bytes(payload) != encoded:
                raise ValueError("terminal proof is not canonical")
            return WorkerTerminalProof.from_dict(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(
                "persisted local process terminal proof is invalid."
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _checkpoint_redacted_registry(self) -> None:
        with self._connect() as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise RealmConflict(
                "local process registry redaction checkpoint remains pending."
            )

    def _delete_retired_launch_directory(self, row: _LaunchRow) -> None:
        """Delete one exact provider-private directory without path traversal."""

        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_fd = os.open(self._launches_root, flags)
        try:
            try:
                directory_fd = os.open(row.coordinate, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            try:
                metadata = os.fstat(directory_fd)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                ):
                    raise RealmIntegrityError(
                        "retired local process directory identity is invalid."
                    )
                for name in os.listdir(directory_fd):
                    if not isinstance(name, str) or name in {".", ".."}:
                        raise RealmIntegrityError(
                            "retired local process directory entry is invalid."
                        )
                    try:
                        entry = os.stat(
                            name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(entry.st_mode):
                        raise RealmIntegrityError(
                            "retired local process directory contains a directory."
                        )
                    try:
                        os.unlink(name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            try:
                os.rmdir(row.coordinate, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _ensure_launch_directory(self, coordinate: str) -> Path:
        path = self._launch_directory(coordinate)
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = os.lstat(path)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RealmIntegrityError(
                "local process launch directory identity is invalid."
            )
        os.chmod(path, 0o700)
        return path

    def _launch_directory(self, coordinate: str) -> Path:
        lower_hex_digest(coordinate, "worker launch coordinate")
        return self._launches_root / coordinate

    @staticmethod
    def _open_lock(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(descriptor)
            raise RealmIntegrityError("local process lock file identity is invalid.")
        return descriptor

    @staticmethod
    def _open_existing_lock(path: Path, row: _LaunchRow) -> int:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, OSError) as error:
            raise ProcessLockIdentityError(
                "local process lock file is unavailable."
            ) from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_dev != row.lock_device
            or metadata.st_ino != row.lock_inode
        ):
            os.close(descriptor)
            raise ProcessLockIdentityError(
                "local process lock file identity changed."
            )
        return descriptor

    @staticmethod
    def _assert_clean_launch_artifacts(launch_dir: Path) -> None:
        for name in ("manifest.json", "handshake.json", "result.json", "stop.json"):
            if os.path.lexists(launch_dir / name):
                raise RealmIntegrityError(
                    "unregistered local process launch artifacts already exist."
                )

    @staticmethod
    def _record_digest(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            b"optpilot/local-process-record/v1\0" + canonical_json_bytes(payload)
        ).hexdigest()

    def _publish_record(self, path: Path, payload: JsonDict) -> None:
        body = dict(payload)
        body["record_digest"] = self._record_digest(body)
        encoded = canonical_json_bytes(body)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("local process provider record exceeds its size bound.")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short local process provider record write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _read_record(self, path: Path) -> JsonDict:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise RealmIntegrityError(
                "local process provider record is unavailable."
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RealmIntegrityError(
                    "local process provider record identity is invalid."
                )
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_RECORD_BYTES:
                    raise RealmIntegrityError(
                        "local process provider record exceeds its size bound."
                    )
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        encoded = b"".join(chunks)
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(
                "local process provider record is not JSON."
            ) from error
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != encoded:
            raise RealmIntegrityError(
                "local process provider record is not canonical JSON."
            )
        recorded_digest = payload.pop("record_digest", None)
        if recorded_digest != self._record_digest(payload):
            raise RealmIntegrityError(
                "local process provider record digest does not match."
            )
        return payload

    def _deadline(self, timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("process wait timeout must be nonnegative or None.")
        return self._monotonic() + float(timeout)

    def _sleep_until_next_poll(self, deadline: float | None, action: str) -> None:
        now = self._monotonic()
        if deadline is not None and now >= deadline:
            raise TimeoutError(f"timed out {action}.")
        delay = self._poll_interval
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - now))
        time.sleep(delay)


__all__ = [
    "LocalProcessSupervisor",
    "ProcessEndpointState",
    "ProcessLaunchRealizationClaim",
    "ProcessLaunchPrivateEnvironment",
    "ProcessLaunchReservation",
    "ProcessLaunchRequest",
    "ProcessLaunchSealPriorState",
    "ProcessLaunchSealReceipt",
    "ProcessTerminalPriorState",
    "ProcessTerminalReconciliation",
    "ProcessUnixSocketRegistration",
    "SupervisedProcess",
    "WorkerDisposition",
    "WorkerStarted",
    "WorkerStartResult",
    "WorkerTerminalProof",
]
