"""Watchdog renewal for one live Realm run controller.

The coordinator owns only liveness.  A run controller remains the lifecycle
authority for its method worker and projections; this object periodically
renews that controller's exact fenced lease and then asks each already-created
method resource to renew itself.  The target set is frozen before the first
round so one stable name can never silently begin referring to another
resource.

One round is ordered as follows::

    controller lease -> named method resources (lexicographic order)

The newest controller lease and the successful target-name prefix are updated
after every step.  A failure therefore remains inspectable without pretending
that the rest of the round completed.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .realm.leases import LeaseRecord, LeaseState


_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")
_TARGET_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")
_EXCEPTION_NAME_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


class _ControllerHeartbeatLedger(Protocol):
    def heartbeat_lease(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        lease_id: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> LeaseRecord: ...


class RunControllerHeartbeatTarget(Protocol):
    """One already-realized method resource owned by the controller term."""

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> object: ...


@dataclass(frozen=True)
class RunControllerHeartbeatSnapshot:
    """Newest controller fact and successful prefix of the current round."""

    controller_lease: LeaseRecord
    round_number: int
    controller_renewed: bool
    completed_target_names: tuple[str, ...]


@dataclass(frozen=True)
class RunControllerHeartbeatFailure:
    """A failed round retained for deterministic foreground propagation."""

    phase: str
    round_number: int
    cause: BaseException


class RunControllerHeartbeatError(RuntimeError):
    """Bounded, path-free surface for a retained watchdog failure."""

    def __init__(self, failure: RunControllerHeartbeatFailure) -> None:
        if not isinstance(failure, RunControllerHeartbeatFailure):
            raise TypeError("failure must be a RunControllerHeartbeatFailure.")
        self.failure = failure
        super().__init__(
            "Run-controller heartbeat failed during "
            f"{failure.phase} in round {failure.round_number}: "
            f"{_exception_type_name(failure.cause)}."
        )


class RunControllerHeartbeatStateError(RuntimeError):
    """Raised when the watchdog is used outside its live lifecycle."""


RunControllerFailureCallback = Callable[[RunControllerHeartbeatFailure], None]


@dataclass(frozen=True)
class _TargetAttachment:
    name: str
    identity: int
    heartbeat: Callable[..., object]


class RunControllerHeartbeatCoordinator:
    """Renew one exact run-controller term and its method resources.

    Construction does not create a thread.  Targets may be supplied at
    construction or attached with :meth:`attach_target`; attachment closes at
    the first manual round or :meth:`start`, whichever comes first.  Capturing
    the target's heartbeat callable at attachment time also prevents a later
    attribute swap from changing the resource behind a stable name.

    ``start()`` launches an immediate round, followed by fixed-interval rounds.
    ``stop()`` is idempotent and does not return while a background or already
    entered manual round (including its failure callback) can still be active.

    Heartbeat steps deliberately catch ``BaseException``, matching the attempt
    coordinator: even a crash-like failure becomes a retained watchdog fact and
    stops future renewal.  The public error exposes only its phase, round, and
    exception type; the original cause remains available on ``failure`` for
    trusted diagnostics and is not exception-chained into the public surface.
    """

    def __init__(
        self,
        ledger: _ControllerHeartbeatLedger,
        *,
        actor_principal_id: str,
        run_id: str,
        controller_lease: LeaseRecord,
        ttl_seconds: float,
        interval_seconds: float | None = None,
        session_id: str | None = None,
        targets: Mapping[str, RunControllerHeartbeatTarget] | None = None,
        failure_callback: RunControllerFailureCallback | None = None,
    ) -> None:
        if not callable(getattr(ledger, "heartbeat_lease", None)):
            raise TypeError("ledger must provide heartbeat_lease().")
        actor = _nonempty_text(actor_principal_id, "actor_principal_id")
        selected_run_id = _nonempty_text(run_id, "run_id")
        _validate_controller_lease(controller_lease, selected_run_id)
        selected_ttl = _positive_finite(ttl_seconds, "ttl_seconds")
        selected_interval = (
            selected_ttl / 3.0
            if interval_seconds is None
            else _positive_finite(interval_seconds, "interval_seconds")
        )
        selected_interval = _positive_finite(
            selected_interval, "interval_seconds"
        )
        selected_session = uuid.uuid4().hex if session_id is None else session_id
        if not isinstance(selected_session, str) or not _SESSION_ID_PATTERN.fullmatch(
            selected_session
        ):
            raise ValueError(
                "session_id must contain 1-128 ASCII letters, digits, '.', '_', "
                "or '-'."
            )
        if failure_callback is not None and not callable(failure_callback):
            raise TypeError("failure_callback must be callable.")
        if targets is not None and not isinstance(targets, Mapping):
            raise TypeError("targets must be a mapping from names to resources.")

        self._ledger = ledger
        self._actor_principal_id = actor
        self._run_id = selected_run_id
        self._ttl_seconds = selected_ttl
        self._interval_seconds = selected_interval
        self._session_id = selected_session
        self._failure_callback = failure_callback

        actor_key = _identity_key(actor)
        run_key = _identity_key(selected_run_id)
        term_key = _identity_key(
            f"{controller_lease.lease_id}\0{controller_lease.fencing_token}"
        )
        self._operation_prefix = (
            "run-controller-heartbeat/"
            f"{actor_key}/{run_key}/{term_key}/{selected_session}"
        )

        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._round_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._round_number = 0
        self._completed_rounds = 0
        self._failure: RunControllerHeartbeatFailure | None = None
        self._failure_callback_claimed = False
        self._failure_callback_active = False
        self._failure_callback_cause: BaseException | None = None
        self._callback_thread_id: int | None = None
        self._attachments: dict[str, _TargetAttachment] = {}
        self._target_identities: set[int] = set()
        self._snapshot = RunControllerHeartbeatSnapshot(
            controller_lease=controller_lease,
            round_number=0,
            controller_renewed=False,
            completed_target_names=(),
        )

        if targets:
            for name, target in targets.items():
                self.attach_target(name, target)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def snapshot(self) -> RunControllerHeartbeatSnapshot:
        """Return the newest fact and successful current-round prefix."""

        with self._state_lock:
            return self._snapshot

    @property
    def controller_lease(self) -> LeaseRecord:
        with self._state_lock:
            return self._snapshot.controller_lease

    @property
    def target_names(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(sorted(self._attachments))

    @property
    def failure(self) -> RunControllerHeartbeatFailure | None:
        with self._state_lock:
            return self._failure

    @property
    def failure_callback_cause(self) -> BaseException | None:
        """Return a retained callback failure without replacing the root cause."""

        with self._state_lock:
            return self._failure_callback_cause

    @property
    def completed_rounds(self) -> int:
        with self._state_lock:
            return self._completed_rounds

    @property
    def running(self) -> bool:
        with self._state_lock:
            return (
                self._started
                and not self._stopped
                and self._failure is None
                and not self._stop_requested.is_set()
            )

    @property
    def stopped(self) -> bool:
        with self._state_lock:
            return self._stopped

    def attach_target(
        self, name: str, target: RunControllerHeartbeatTarget
    ) -> None:
        """Attach one stable named target before the first renewal round."""

        selected_name = _target_name(name)
        heartbeat = getattr(target, "heartbeat", None)
        if not callable(heartbeat):
            raise TypeError("target must provide heartbeat().")
        identity = id(target)
        attachment = _TargetAttachment(selected_name, identity, heartbeat)

        with self._round_lock:
            with self._state_lock:
                if self._started or self._round_number != 0:
                    raise RunControllerHeartbeatStateError(
                        "Cannot attach method resources after renewal begins."
                    )
                if self._stopped or self._stop_requested.is_set():
                    raise RunControllerHeartbeatStateError(
                        "Cannot attach method resources after stop."
                    )
                if self._failure is not None:
                    raise RunControllerHeartbeatStateError(
                        "Cannot attach method resources after heartbeat failure."
                    )
                if selected_name in self._attachments:
                    raise RunControllerHeartbeatStateError(
                        "A method resource with that name was already attached."
                    )
                if identity in self._target_identities:
                    raise RunControllerHeartbeatStateError(
                        "That method resource was already attached under another name."
                    )
                self._attachments[selected_name] = attachment
                self._target_identities.add(identity)

    def heartbeat_once(self) -> RunControllerHeartbeatSnapshot:
        """Run one ordered renewal round and return its newest prefix facts."""

        failure_to_raise: RunControllerHeartbeatFailure | None = None
        callback: RunControllerFailureCallback | None = None

        with self._round_lock:
            with self._state_lock:
                if self._stopped or self._stop_requested.is_set():
                    raise RunControllerHeartbeatStateError(
                        "Heartbeat coordinator is stopping or stopped."
                    )
                if self._failure is not None:
                    raise RunControllerHeartbeatError(self._failure) from None
                self._round_number += 1
                round_number = self._round_number
                attachments = tuple(
                    self._attachments[name] for name in sorted(self._attachments)
                )
                self._snapshot = RunControllerHeartbeatSnapshot(
                    controller_lease=self._snapshot.controller_lease,
                    round_number=round_number,
                    controller_renewed=False,
                    completed_target_names=(),
                )

            operation_root = f"{self._operation_prefix}/{round_number:016d}"
            phase = "controller"
            try:
                previous = self.controller_lease
                controller = self._ledger.heartbeat_lease(
                    operation_id=f"{operation_root}/controller",
                    actor_principal_id=self._actor_principal_id,
                    lease_id=previous.lease_id,
                    holder_id=previous.holder_id,
                    fencing_token=previous.fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
                _validate_renewed_controller(controller, previous, self._run_id)
                with self._state_lock:
                    self._snapshot = RunControllerHeartbeatSnapshot(
                        controller_lease=controller,
                        round_number=round_number,
                        controller_renewed=True,
                        completed_target_names=(),
                    )

                completed: list[str] = []
                for attachment in attachments:
                    phase = f"target:{attachment.name}"
                    attachment.heartbeat(
                        operation_id=(
                            f"{operation_root}/target/{attachment.name}"
                        ),
                        ttl_seconds=self._ttl_seconds,
                    )
                    completed.append(attachment.name)
                    with self._state_lock:
                        self._snapshot = RunControllerHeartbeatSnapshot(
                            controller_lease=self._snapshot.controller_lease,
                            round_number=round_number,
                            controller_renewed=True,
                            completed_target_names=tuple(completed),
                        )
            except BaseException as cause:
                recorded = RunControllerHeartbeatFailure(
                    phase=phase,
                    round_number=round_number,
                    cause=cause,
                )
                with self._state_lock:
                    if self._failure is None:
                        self._failure = recorded
                    else:
                        recorded = self._failure
                    self._stop_requested.set()
                    if (
                        self._failure_callback is not None
                        and not self._failure_callback_claimed
                    ):
                        self._failure_callback_claimed = True
                        self._failure_callback_active = True
                        callback = self._failure_callback
                failure_to_raise = recorded
            else:
                with self._state_lock:
                    self._completed_rounds += 1
                    return self._snapshot

        if callback is not None:
            try:
                with self._state_lock:
                    self._callback_thread_id = threading.get_ident()
                try:
                    callback(failure_to_raise)  # type: ignore[arg-type]
                except BaseException as callback_cause:
                    with self._state_lock:
                        self._failure_callback_cause = callback_cause
            finally:
                with self._state_changed:
                    self._callback_thread_id = None
                    self._failure_callback_active = False
                    self._state_changed.notify_all()

        if failure_to_raise is None:  # pragma: no cover - defensive invariant
            raise AssertionError("A failed heartbeat round lost its failure fact.")
        raise RunControllerHeartbeatError(failure_to_raise) from None

    def start(self) -> "RunControllerHeartbeatCoordinator":
        """Start immediate background renewal followed by fixed intervals."""

        with self._state_lock:
            if self._started:
                raise RunControllerHeartbeatStateError(
                    "Heartbeat coordinator was already started."
                )
            if self._stopped or self._stop_requested.is_set():
                raise RunControllerHeartbeatStateError(
                    "Heartbeat coordinator is stopping or stopped."
                )
            if self._failure is not None:
                raise RunControllerHeartbeatError(self._failure) from None
            self._started = True
            thread = threading.Thread(
                target=self._background_main,
                name=(
                    "optpilot-controller-heartbeat-"
                    f"{_identity_key(self._run_id)}-{self._session_id[:8]}"
                ),
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self._started = False
                raise
        return self

    def raise_if_failed(self) -> None:
        """Raise the bounded foreground surface for a retained failure."""

        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RunControllerHeartbeatError(failure) from None

    def stop(self) -> None:
        """Stop renewal and fence every entered round and failure callback."""

        current_thread = threading.current_thread()
        current_id = threading.get_ident()
        with self._state_lock:
            if self._stopped:
                return
            if self._thread is current_thread:
                raise RunControllerHeartbeatStateError(
                    "The heartbeat thread cannot synchronously stop itself."
                )
            if self._callback_thread_id == current_id:
                raise RunControllerHeartbeatStateError(
                    "The heartbeat failure callback cannot synchronously stop its coordinator."
                )
            self._stop_requested.set()
            thread = self._thread
        if thread is not None:
            thread.join()
        # A manual round can exist without a background thread.  Its failure
        # claims callback activity before releasing this fence; the condition
        # then waits without holding a lock around caller-controlled callback
        # code.
        with self._round_lock:
            pass
        with self._state_changed:
            while self._failure_callback_active:
                self._state_changed.wait()
            self._stopped = True

    def _background_main(self) -> None:
        while not self._stop_requested.is_set():
            try:
                self.heartbeat_once()
            except (RunControllerHeartbeatError, RunControllerHeartbeatStateError):
                return
            if self._stop_requested.wait(self._interval_seconds):
                return


def _validate_controller_lease(lease: object, run_id: str) -> None:
    if not isinstance(lease, LeaseRecord):
        raise TypeError("controller_lease must be a LeaseRecord.")
    generation = lease.metadata.get("controller_generation")
    if (
        lease.parent_lease_id is not None
        or lease.lease_kind != "run-controller"
        or lease.audience != "realm-ledger"
        or lease.scope_key != f"run:{run_id}"
        or lease.metadata.get("run_id") != run_id
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise ValueError("controller_lease does not identify the requested run term.")
    if lease.state is not LeaseState.ACTIVE:
        raise ValueError("controller_lease must be active.")


def _validate_renewed_controller(
    renewed: object, previous: LeaseRecord, run_id: str
) -> None:
    if not isinstance(renewed, LeaseRecord):
        raise TypeError("ledger heartbeat returned a non-lease record.")
    _validate_controller_lease(renewed, run_id)
    identity_fields = (
        "lease_id",
        "owner_id",
        "parent_lease_id",
        "lease_kind",
        "audience",
        "holder_id",
        "scope_key",
        "fencing_token",
        "created_at",
        "metadata",
    )
    if any(getattr(renewed, field) != getattr(previous, field) for field in identity_fields):
        raise RunControllerHeartbeatStateError(
            "Controller heartbeat changed the fenced term identity."
        )
    if renewed.heartbeat_revision <= previous.heartbeat_revision:
        raise RunControllerHeartbeatStateError(
            "Controller heartbeat did not advance its revision."
        )
    if renewed.updated_at < previous.updated_at:
        raise RunControllerHeartbeatStateError(
            "Controller heartbeat regressed its update time."
        )
    if renewed.expires_at <= renewed.updated_at or renewed.expires_at <= time.time():
        raise RunControllerHeartbeatStateError(
            "Controller heartbeat did not return a live lease."
        )


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text.")
    return value


def _target_name(value: object) -> str:
    if not isinstance(value, str) or not _TARGET_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "target name must contain 1-64 ASCII letters, digits, '.', '_', or '-'."
        )
    return value


def _positive_finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{label} must be a positive finite number.")
    return float(value)


def _identity_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _exception_type_name(cause: BaseException) -> str:
    name = type(cause).__name__
    return name if _EXCEPTION_NAME_PATTERN.fullmatch(name) else "BaseException"


__all__ = [
    "RunControllerFailureCallback",
    "RunControllerHeartbeatCoordinator",
    "RunControllerHeartbeatError",
    "RunControllerHeartbeatFailure",
    "RunControllerHeartbeatSnapshot",
    "RunControllerHeartbeatStateError",
    "RunControllerHeartbeatTarget",
]
