"""Lease supervision for one live Realm run attempt.

The coordinator deliberately owns only renewal.  It does not prepare, bind,
launch, stop, finalize, or adopt an attempt.  Those lifecycle owners can keep
one coordinator alive around their blocking work and surface a background
failure at their next safe boundary with :meth:`raise_if_failed`.

One round renews the authority chain from parent to child::

    controller lease -> attempt lease -> capture change + retention lease
                     -> provider resources (when attached)

Each successful step replaces the corresponding fact in ``receipt`` before
the next step begins.  Consequently, a later failure is explicit while the
caller still has the newest durable facts from the successful prefix.

``receipt`` is therefore a composite of several ledger transactions rather
than one coherent snapshot, and it must not be read as one.  The run
controller lease at its root is shared: every other live attempt in the run
renews it, and so does the run controller watchdog.  A renewal by any of
them between two of this round's steps legitimately moves the controller's
expiry past the controller record this round already cached, and the ledger
then clamps the child renewed next to that newer parent expiry.  Comparing a
freshly renewed child against a cached parent across those instants proves
nothing, which is why parent-first expiry ordering is asserted only where
the whole chain is read in one transaction -- see
``run_attempt_records.validate_run_attempt_heartbeat_expiry_chain``.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from .realm.leases import LeaseRecord
from .realm.owners import OwnerChangeHeartbeatReceipt
from .realm.run_attempt_records import (
    RunAttemptHeartbeatAuthorityReceipt,
    RunAttemptPreparationReceipt,
)


_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")


class _HeartbeatLedger(Protocol):
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

    def heartbeat_owner_change(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        change_id: str,
        retention_lease_id: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> OwnerChangeHeartbeatReceipt: ...


class _ProviderHeartbeatTarget(Protocol):
    """One already-realized provider resource set owned by this attempt."""

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None: ...


HeartbeatReceipt = (
    RunAttemptPreparationReceipt | RunAttemptHeartbeatAuthorityReceipt
)


@dataclass(frozen=True)
class RunAttemptHeartbeatFailure:
    """A failed round retained for deterministic foreground propagation."""

    phase: str
    round_number: int
    cause: BaseException


class RunAttemptHeartbeatError(RuntimeError):
    """Raised when a synchronous or background heartbeat round failed."""

    def __init__(self, failure: RunAttemptHeartbeatFailure) -> None:
        if not isinstance(failure, RunAttemptHeartbeatFailure):
            raise TypeError("failure must be a RunAttemptHeartbeatFailure.")
        self.failure = failure
        super().__init__(
            "Run-attempt heartbeat failed during "
            f"{failure.phase} in round {failure.round_number}: "
            f"{type(failure.cause).__name__}."
        )


class RunAttemptHeartbeatStateError(RuntimeError):
    """Raised when the coordinator is used outside its live lifecycle."""


class RunAttemptHeartbeatCoordinator:
    """Renew one attempt's complete live authority chain.

    Construction does not start a thread.  Call :meth:`start` when background
    renewal is required, or call :meth:`heartbeat_once` at explicit scheduler
    boundaries.  ``stop()`` is idempotent and, once it returns, no background
    or already-entered manual heartbeat can still call the ledger or provider.

    A process binding may be supplied at construction or attached once later.
    The latter is the prepare-before-bind path: earlier rounds keep the Realm
    capture alive, and subsequent rounds additionally renew provider resources.
    """

    def __init__(
        self,
        ledger: _HeartbeatLedger,
        *,
        actor_principal_id: str,
        receipt: HeartbeatReceipt,
        ttl_seconds: float | None = None,
        interval_seconds: float | None = None,
        session_id: str | None = None,
        binding: _ProviderHeartbeatTarget | None = None,
    ) -> None:
        if not callable(getattr(ledger, "heartbeat_lease", None)) or not callable(
            getattr(ledger, "heartbeat_owner_change", None)
        ):
            raise TypeError("ledger must provide typed lease heartbeat methods.")
        if (
            not isinstance(actor_principal_id, str)
            or not actor_principal_id
            or "\x00" in actor_principal_id
        ):
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(
            receipt,
            (RunAttemptPreparationReceipt, RunAttemptHeartbeatAuthorityReceipt),
        ):
            raise TypeError(
                "receipt must be a RunAttemptPreparationReceipt or "
                "RunAttemptHeartbeatAuthorityReceipt."
            )

        selected_ttl = (
            receipt.resource_ttl_seconds
            if ttl_seconds is None
            else _positive_finite(ttl_seconds, "ttl_seconds")
        )
        selected_ttl = _positive_finite(selected_ttl, "ttl_seconds")
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

        self._ledger = ledger
        self._actor_principal_id = actor_principal_id
        self._receipt: HeartbeatReceipt = receipt
        self._ttl_seconds = selected_ttl
        self._interval_seconds = selected_interval
        self._session_id = selected_session
        actor_key = hashlib.sha256(actor_principal_id.encode("utf-8")).hexdigest()[:16]
        self._operation_prefix = (
            f"run-attempt-heartbeat/{actor_key}/{selected_session}"
        )

        self._state_lock = threading.RLock()
        self._round_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._round_number = 0
        self._completed_rounds = 0
        self._failure: RunAttemptHeartbeatFailure | None = None
        self._binding: _ProviderHeartbeatTarget | None = None
        self._binding_attached = False
        if binding is not None:
            self.attach_binding(binding)

    @property
    def receipt(self) -> HeartbeatReceipt:
        """Return the newest facts, including any successful partial round."""

        with self._state_lock:
            return self._receipt

    @property
    def failure(self) -> RunAttemptHeartbeatFailure | None:
        with self._state_lock:
            return self._failure

    @property
    def completed_rounds(self) -> int:
        with self._state_lock:
            return self._completed_rounds

    @property
    def session_id(self) -> str:
        return self._session_id

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

    def attach_binding(self, binding: _ProviderHeartbeatTarget) -> None:
        """Attach one provider resource set, before or after its Realm commit.

        A prepared process binding exposes ``run_id``/``attempt_id`` directly;
        a recovered durable binding exposes them through its typed receipt.  In
        both cases the target owns the same realized resources for the rest of
        the attempt, so the coordinator never swaps attachments at commit time.
        """

        if not callable(getattr(binding, "heartbeat", None)):
            raise TypeError("binding must provide heartbeat().")
        binding_run_id, binding_attempt_id = _provider_target_identity(binding)

        with self._round_lock:
            with self._state_lock:
                if self._binding_attached:
                    raise RunAttemptHeartbeatStateError(
                        "A provider binding was already attached."
                    )
                # A failed round records the failure AND requests stop, so
                # the failure must be tested first.  Testing stop first
                # reported every broken heartbeat as a deliberate stop and
                # discarded the cause the round captured for exactly this --
                # which is how a renewal failure here reached the caller as an
                # unexplained provider error.
                if self._failure is not None:
                    raise RunAttemptHeartbeatStateError(
                        "Cannot attach provider resources after heartbeat failure."
                    ) from self._failure.cause
                if self._stopped or self._stop_requested.is_set():
                    raise RunAttemptHeartbeatStateError(
                        "Cannot attach provider resources after stop."
                    )
                if (
                    binding_run_id != self._receipt.run.run_id
                    or binding_attempt_id != self._receipt.attempt.attempt_id
                ):
                    raise ValueError(
                        "Provider binding differs from the heartbeat attempt."
                    )
                self._binding = binding
                self._binding_attached = True

    def heartbeat_once(self) -> HeartbeatReceipt:
        """Run one ordered renewal round and return the newest receipt facts."""

        with self._round_lock:
            with self._state_lock:
                if self._stopped or self._stop_requested.is_set():
                    raise RunAttemptHeartbeatStateError(
                        "Heartbeat coordinator is stopping or stopped."
                    )
                if self._failure is not None:
                    raise RunAttemptHeartbeatError(self._failure) from self._failure.cause
                self._round_number += 1
                round_number = self._round_number
                binding = self._binding

            operation_root = f"{self._operation_prefix}/{round_number:016d}"
            phase = "controller"
            try:
                current = self.receipt
                controller = self._ledger.heartbeat_lease(
                    operation_id=f"{operation_root}/controller",
                    actor_principal_id=self._actor_principal_id,
                    lease_id=current.controller_lease.lease_id,
                    holder_id=current.controller_lease.holder_id,
                    fencing_token=current.controller_lease.fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
                self._replace_receipt(controller_lease=controller)

                phase = "attempt"
                current = self.receipt
                attempt = self._ledger.heartbeat_lease(
                    operation_id=f"{operation_root}/attempt",
                    actor_principal_id=self._actor_principal_id,
                    lease_id=current.attempt_lease.lease_id,
                    holder_id=current.attempt_lease.holder_id,
                    fencing_token=current.attempt_lease.fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
                self._replace_receipt(attempt_lease=attempt)

                phase = "capture"
                current = self.receipt
                capture = self._ledger.heartbeat_owner_change(
                    operation_id=f"{operation_root}/capture",
                    actor_principal_id=self._actor_principal_id,
                    change_id=current.capture_change.change_id,
                    retention_lease_id=current.capture_retention_lease.lease_id,
                    holder_id=current.capture_retention_lease.holder_id,
                    fencing_token=current.capture_retention_lease.fencing_token,
                    ttl_seconds=self._ttl_seconds,
                )
                self._replace_receipt(
                    capture_change=capture.change,
                    capture_retention_lease=capture.retention_lease,
                )

                if binding is not None:
                    phase = "binding"
                    binding.heartbeat(
                        operation_id=f"{operation_root}/provider",
                        ttl_seconds=self._ttl_seconds,
                    )
            except BaseException as cause:
                failure = RunAttemptHeartbeatFailure(
                    phase=phase,
                    round_number=round_number,
                    cause=cause,
                )
                with self._state_lock:
                    if self._failure is None:
                        self._failure = failure
                    else:
                        failure = self._failure
                    self._stop_requested.set()
                raise RunAttemptHeartbeatError(failure) from cause

            with self._state_lock:
                self._completed_rounds += 1
                return self._receipt

    def start(self) -> "RunAttemptHeartbeatCoordinator":
        """Start immediate background renewal followed by fixed intervals."""

        with self._state_lock:
            if self._started:
                raise RunAttemptHeartbeatStateError(
                    "Heartbeat coordinator was already started."
                )
            if self._stopped or self._stop_requested.is_set():
                raise RunAttemptHeartbeatStateError(
                    "Heartbeat coordinator is stopping or stopped."
                )
            if self._failure is not None:
                raise RunAttemptHeartbeatError(self._failure) from self._failure.cause
            self._started = True
            thread = threading.Thread(
                target=self._background_main,
                name=(
                    "optpilot-attempt-heartbeat-"
                    f"{self._receipt.attempt.attempt_id}-{self._session_id[:8]}"
                ),
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self

    def raise_if_failed(self) -> None:
        """Raise a foreground error for a failed synchronous/background round."""

        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RunAttemptHeartbeatError(failure) from failure.cause

    def stop(self) -> None:
        """Stop renewal and wait out every already-entered round; idempotent."""

        with self._state_lock:
            if self._stopped:
                return
            self._stop_requested.set()
            thread = self._thread
        if thread is threading.current_thread():
            raise RunAttemptHeartbeatStateError(
                "The heartbeat thread cannot synchronously stop itself."
            )
        if thread is not None:
            thread.join()
        # A public heartbeat_once() can be active even when no background
        # thread exists.  Crossing this lock is the shutdown completion fence.
        with self._round_lock:
            pass
        with self._state_lock:
            self._stopped = True

    def _replace_receipt(self, **updates: object) -> None:
        with self._state_lock:
            self._receipt = replace(self._receipt, **updates)

    def _background_main(self) -> None:
        while not self._stop_requested.is_set():
            try:
                self.heartbeat_once()
            except RunAttemptHeartbeatError:
                return
            except RunAttemptHeartbeatStateError:
                # stop() may win before the thread enters its first round.
                return
            if self._stop_requested.wait(self._interval_seconds):
                return


def _provider_target_identity(
    target: _ProviderHeartbeatTarget,
) -> tuple[str, str]:
    """Read the attempt identity without requiring one binding lifecycle type."""

    run_id = getattr(target, "run_id", None)
    attempt_id = getattr(target, "attempt_id", None)
    if run_id is None and attempt_id is None:
        try:
            binding = target.receipt.binding  # type: ignore[attr-defined]
            run_id = binding.run_id
            attempt_id = binding.attempt_id
        except (AttributeError, TypeError) as error:
            raise TypeError(
                "binding must expose run_id/attempt_id directly or through a "
                "canonical run-attempt binding receipt."
            ) from error
    if (
        not isinstance(run_id, str)
        or not run_id
        or "\x00" in run_id
        or not isinstance(attempt_id, str)
        or not attempt_id
        or "\x00" in attempt_id
    ):
        raise TypeError("binding run_id and attempt_id must be nonempty text.")
    return run_id, attempt_id


def _positive_finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{label} must be a positive finite number.")
    return float(value)


__all__ = [
    "HeartbeatReceipt",
    "RunAttemptHeartbeatCoordinator",
    "RunAttemptHeartbeatError",
    "RunAttemptHeartbeatFailure",
    "RunAttemptHeartbeatStateError",
]
