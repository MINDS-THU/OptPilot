"""Path-free scheduler facade for retained local process attempts.

The local provider is the only object outside the process implementation that
coordinates host resources, the private process registry, and the control
volume protocol.  Its public values contain only canonical Realm identities.
In particular, native paths, process requests, reservations, backend tokens,
and raw terminal proofs never cross this module's public boundary.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ..attempts import AttemptFinalization
from ..run_attempt_heartbeat import RunAttemptHeartbeatCoordinator
from ._validation import lower_hex_digest, required_text
from .attempt_finalizer import RealmAttemptFinalizer
from .errors import RealmConflict, RealmError, RealmIntegrityError, RealmNotFound
from .execution_binding_records import (
    ExecutionBindingRecord,
    ExecutionLaunchIntentRecord,
    ExecutionTerminalEvidenceRecord,
)
from .ledger import RealmLedger
from .local_attempt_launcher import (
    LocalAttemptPlatformError,
    ManagedLocalAttempt,
    RealmLocalAttemptLauncher,
    _CompiledLocalAttemptLaunch,
)
from .local_process_supervisor import (
    ProcessLaunchReservation,
    SupervisedProcess,
    WorkerStarted,
    WorkerTerminalProof,
)
from .process_execution_binder import (
    ManagedProcessExecutionBinding,
    PreparedProcessExecutionBinding,
    ProcessExecutionResourceError,
    RealmProcessExecutionBinder,
)
from .run_attempt_records import RunAttemptRecord


@dataclass(frozen=True, order=True)
class LocalAttemptStarted:
    """Path-free observation of one exact physically started attempt."""

    run_id: str
    attempt_id: str
    binding_id: str
    launch_token: str
    evidence_fingerprint: str
    launch_request_digest: str

    def __post_init__(self) -> None:
        required_text(self.run_id, "started attempt run id")
        required_text(self.attempt_id, "started attempt id")
        required_text(self.binding_id, "started attempt binding id")
        required_text(self.launch_token, "started attempt launch token")
        lower_hex_digest(
            self.evidence_fingerprint, "started attempt evidence fingerprint"
        )
        lower_hex_digest(
            self.launch_request_digest, "started attempt request digest"
        )


@dataclass(frozen=True, order=True)
class LocalAttemptTerminal:
    """Public terminal observation containing only retained Realm evidence."""

    evidence: ExecutionTerminalEvidenceRecord

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, ExecutionTerminalEvidenceRecord):
            raise TypeError(
                "evidence must be an ExecutionTerminalEvidenceRecord."
            )

    @property
    def run_id(self) -> str:
        return self.evidence.run_id

    @property
    def attempt_id(self) -> str:
        return self.evidence.attempt_id

    @property
    def binding_id(self) -> str:
        return self.evidence.binding_id

    @property
    def started(self) -> bool:
        return self.evidence.started

    @property
    def disposition(self) -> str:
        return self.evidence.disposition


LocalAttemptStartObservation: TypeAlias = (
    LocalAttemptStarted | LocalAttemptTerminal
)


@dataclass(frozen=True, order=True)
class LocalAttemptCleanupResult:
    """Path-free status of idempotent provider-resource cleanup."""

    run_id: str
    attempt_id: str
    state: Literal["cleaned", "not_required", "pending"]
    code: str | None = None

    def __post_init__(self) -> None:
        required_text(self.run_id, "cleanup result run id")
        required_text(self.attempt_id, "cleanup result attempt id")
        if self.state not in {"cleaned", "not_required", "pending"}:
            raise ValueError("cleanup result state is unsupported.")
        if self.state == "pending":
            if self.code != "provider_cleanup_pending":
                raise ValueError("pending cleanup requires its allowlisted code.")
        elif self.code is not None:
            raise ValueError("completed cleanup must not carry an error code.")


_PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "provider_integrity_failure": (
        "The local attempt provider detected inconsistent retained state."
    ),
    "provider_operation_failed": (
        "The local attempt provider could not complete the requested operation."
    ),
    "provider_resource_failure": (
        "The local attempt provider could not maintain its managed resources."
    ),
    "provider_state_conflict": (
        "The local attempt provider state changed before the operation completed."
    ),
    "provider_state_unavailable": (
        "The requested local attempt provider state is unavailable."
    ),
    "provider_timeout": "The local attempt provider operation timed out.",
}
_PUBLIC_OPERATIONS = {
    "finalize_terminal",
    "resume_cleanup",
    "start_or_attach",
    "stop_and_wait_terminal",
    "terminalize_current",
    "wait_terminal",
}


class LocalAttemptProviderError(RealmError):
    """Allowlisted path-free failure returned by the scheduler facade."""

    def __init__(self, *, operation: str, code: str) -> None:
        if operation not in _PUBLIC_OPERATIONS:
            raise ValueError("local attempt provider operation is unsupported.")
        try:
            message = _PUBLIC_ERROR_MESSAGES[code]
        except KeyError as error:
            raise ValueError(
                "local attempt provider error code is unsupported."
            ) from error
        self.operation = operation
        self.code = code
        super().__init__(message)


@dataclass
class _LocalAttemptSession:
    binding: ManagedProcessExecutionBinding
    handle: ManagedLocalAttempt
    heartbeat_target: (
        PreparedProcessExecutionBinding | ManagedProcessExecutionBinding
    )
    heartbeat_identity: int
    heartbeat: RunAttemptHeartbeatCoordinator


@dataclass
class _LocalAttemptOpening:
    """Retryable provider-private startup state before a session is published."""

    heartbeat_target: (
        PreparedProcessExecutionBinding | ManagedProcessExecutionBinding
    )
    heartbeat_identity: int
    heartbeat: RunAttemptHeartbeatCoordinator
    prepared: PreparedProcessExecutionBinding | None = None
    binding: ManagedProcessExecutionBinding | None = None
    compiled: _CompiledLocalAttemptLaunch | None = None
    reservation: ProcessLaunchReservation | None = None
    terminal_proof: WorkerTerminalProof | None = None
    handle: ManagedLocalAttempt | None = None


_PLATFORM_FAILURES: dict[str, tuple[str, str]] = {
    "worker_never_started": (
        "The local attempt worker never crossed physical start.",
        "launch",
    ),
    "worker_stopped": (
        "The local attempt worker was stopped before collection.",
        "collection",
    ),
    "worker_result_missing": (
        "The exited local attempt worker did not publish a result.",
        "collection",
    ),
    "worker_result_invalid": (
        "The local attempt worker result failed canonical validation.",
        "collection",
    ),
}


class LocalProcessAttemptProvider:
    """Own the exact local process lifecycle behind a path-free facade."""

    def __init__(
        self,
        ledger: RealmLedger,
        binder: RealmProcessExecutionBinder,
        launcher: RealmLocalAttemptLauncher,
        finalizer: RealmAttemptFinalizer,
        *,
        actor_principal_id: str,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(binder, RealmProcessExecutionBinder):
            raise TypeError("binder must be a RealmProcessExecutionBinder.")
        if not isinstance(launcher, RealmLocalAttemptLauncher):
            raise TypeError("launcher must be a RealmLocalAttemptLauncher.")
        if not isinstance(finalizer, RealmAttemptFinalizer):
            raise TypeError("finalizer must be a RealmAttemptFinalizer.")
        required_text(actor_principal_id, "provider actor principal id")
        if (
            getattr(binder, "_ledger", None) is not ledger
            or getattr(finalizer, "_ledger", None) is not ledger
        ):
            raise ValueError(
                "provider binder and finalizer must share the exact Realm ledger."
            )
        reservation_verifier = getattr(
            binder, "_launch_reservation_verifier", None
        )
        terminal_verifier = getattr(binder, "_terminal_proof_verifier", None)
        if (
            getattr(reservation_verifier, "__self__", None) is not launcher
            or getattr(reservation_verifier, "__func__", None)
            is not RealmLocalAttemptLauncher.verify_launch_reservation
            or getattr(terminal_verifier, "__self__", None) is not launcher
            or getattr(terminal_verifier, "__func__", None)
            is not RealmLocalAttemptLauncher._validate_supervisor_terminal_proof
        ):
            raise ValueError(
                "provider binder must use this launcher's exact reservation "
                "and terminal-proof verifiers."
            )
        self._ledger = ledger
        self._binder = binder
        self._launcher = launcher
        self._finalizer = finalizer
        self._actor_principal_id = actor_principal_id
        self._state_lock = threading.RLock()
        self._coordinate_locks: dict[tuple[str, str], threading.RLock] = {}
        self._sessions: dict[tuple[str, str], _LocalAttemptSession] = {}
        self._openings: dict[tuple[str, str], _LocalAttemptOpening] = {}

    def start_or_attach(
        self,
        *,
        run_id: str,
        attempt_id: str,
        heartbeat: RunAttemptHeartbeatCoordinator,
    ) -> LocalAttemptStartObservation:
        """Start an unbound attempt or attach to its exact durable launch.

        A fresh path attaches realized resources to the supplied heartbeat
        before reserving any provider launch.  A recovery path requires the
        already-committed reservation to exist in the private registry; it
        never creates a replacement for a durable launch intent.
        """

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        self._validate_heartbeat_shape(heartbeat)
        failure: LocalAttemptProviderError | None = None
        try:
            return self._start_or_attach(
                run_id=run_id,
                attempt_id=attempt_id,
                heartbeat=heartbeat,
            )
        except Exception as error:
            failure = self._normalize_public_error("start_or_attach", error)
        assert failure is not None
        raise failure

    def _start_or_attach(
        self,
        *,
        run_id: str,
        attempt_id: str,
        heartbeat: RunAttemptHeartbeatCoordinator,
    ) -> LocalAttemptStartObservation:
        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        self._validate_heartbeat_shape(heartbeat)
        key = (run_id, attempt_id)
        with self._coordinate_lock(key):
            session = self._sessions.get(key)
            if session is None:
                opening = self._openings.get(key)
                if opening is None:
                    opening = self._create_opening(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        heartbeat=heartbeat,
                    )
                    self._openings[key] = opening
                else:
                    self._adopt_opening_heartbeat(opening, heartbeat)
                session = self._advance_opening(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    opening=opening,
                )
                self._sessions[key] = session
                self._openings.pop(key, None)
            elif session.heartbeat_identity != id(heartbeat):
                self._attach_heartbeat(heartbeat, session.heartbeat_target)
                session.heartbeat_identity = id(heartbeat)
                session.heartbeat = heartbeat
        # A process can remain alive before publishing its handshake.  Install
        # the exact session and release the coordinate lock before waiting so a
        # concurrent cancellation or replacement controller can always stop it.
        observation = self._wait_started_with_heartbeat(session)
        if isinstance(observation, WorkerTerminalProof):
            return self._record_terminal(session, observation)
        if not isinstance(observation, WorkerStarted):
            raise RealmIntegrityError(
                "local process returned an invalid start observation."
            )
        return LocalAttemptStarted(
            run_id=run_id,
            attempt_id=attempt_id,
            binding_id=observation.binding_id,
            launch_token=observation.launch_token,
            evidence_fingerprint=observation.evidence_fingerprint,
            launch_request_digest=observation.launch_request_digest,
        )

    def wait_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        timeout: float | None = None,
    ) -> LocalAttemptTerminal:
        """Wait for the current exact session and retain terminal evidence."""

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        timeout = self._validate_timeout(timeout)
        failure: LocalAttemptProviderError | None = None
        try:
            return self._wait_terminal(
                run_id=run_id, attempt_id=attempt_id, timeout=timeout
            )
        except Exception as error:
            failure = self._normalize_public_error("wait_terminal", error)
        assert failure is not None
        raise failure

    def _wait_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        timeout: float | None,
    ) -> LocalAttemptTerminal:

        key = self._coordinates(run_id, attempt_id)
        with self._coordinate_lock(key):
            session = self._required_session(key)
        # Waiting must not hold the coordinate lock: cancellation and
        # controller-replacement fencing need to stop the same live writer.
        proof = self._wait_terminal_with_heartbeat(session, timeout=timeout)
        return self._record_terminal(session, proof)

    def stop_and_wait_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> LocalAttemptTerminal:
        """Stop/drain the exact current worker and retain terminal evidence."""

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        grace_period = self._validate_grace_period(grace_period)
        timeout = self._validate_timeout(timeout)
        failure: LocalAttemptProviderError | None = None
        try:
            return self._stop_and_wait_terminal(
                run_id=run_id,
                attempt_id=attempt_id,
                grace_period=grace_period,
                timeout=timeout,
            )
        except Exception as error:
            failure = self._normalize_public_error(
                "stop_and_wait_terminal", error
            )
        assert failure is not None
        raise failure

    def terminalize_current(
        self,
        *,
        run_id: str,
        attempt_id: str,
        heartbeat: RunAttemptHeartbeatCoordinator,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> LocalAttemptTerminal:
        """Terminalize the exact current attempt without causing a fresh start.

        A passive attempt is realized only through the non-spawning
        reserve/commit boundary and then abandoned.  An already start-requested
        attempt is attached and stopped.  This is the provider lifecycle seam
        used by hard-stop recovery: it produces the same retained terminal
        evidence and provider session as normal execution, while guaranteeing
        that cancellation itself never launches a worker.
        """

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        self._validate_heartbeat_shape(heartbeat)
        grace_period = self._validate_grace_period(grace_period)
        timeout = self._validate_timeout(timeout)
        failure: LocalAttemptProviderError | None = None
        try:
            return self._terminalize_current(
                run_id=run_id,
                attempt_id=attempt_id,
                heartbeat=heartbeat,
                grace_period=grace_period,
                timeout=timeout,
            )
        except Exception as error:
            failure = self._normalize_public_error(
                "terminalize_current", error
            )
        assert failure is not None
        raise failure

    def _terminalize_current(
        self,
        *,
        run_id: str,
        attempt_id: str,
        heartbeat: RunAttemptHeartbeatCoordinator,
        grace_period: float,
        timeout: float | None,
    ) -> LocalAttemptTerminal:
        key = self._coordinates(run_id, attempt_id)
        with self._coordinate_lock(key):
            session = self._sessions.get(key)
            if session is not None:
                if session.heartbeat_identity != id(heartbeat):
                    self._attach_heartbeat(heartbeat, session.heartbeat_target)
                    session.heartbeat_identity = id(heartbeat)
                    session.heartbeat = heartbeat
                proof = session.handle.stop(
                    grace_period=grace_period, timeout=timeout
                )
                heartbeat.raise_if_failed()
                return self._record_terminal(session, proof)

            opening = self._openings.get(key)
            if opening is None:
                opening = self._create_opening(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    heartbeat=heartbeat,
                )
                self._openings[key] = opening
            else:
                self._adopt_opening_heartbeat(opening, heartbeat)
            self._terminalize_opening_without_start(
                run_id=run_id,
                attempt_id=attempt_id,
                opening=opening,
                grace_period=grace_period,
                timeout=timeout,
            )
            session = self._session_from_opening(opening)
            self._sessions[key] = session
            self._openings.pop(key, None)
            proof = opening.terminal_proof
            if proof is None:  # pragma: no cover - protected by helper
                raise RealmIntegrityError(
                    "terminalized local attempt lacks terminal proof."
                )
            heartbeat.raise_if_failed()
            return self._record_terminal(session, proof)

    def _terminalize_opening_without_start(
        self,
        *,
        run_id: str,
        attempt_id: str,
        opening: _LocalAttemptOpening,
        grace_period: float,
        timeout: float | None,
    ) -> None:
        """Finish a provider opening using only stop or reserved abandonment."""

        opening.heartbeat.raise_if_failed()
        if opening.compiled is None:
            target = opening.binding or opening.prepared
            if target is None:
                raise RealmIntegrityError(
                    "local attempt opening has no compilable binding."
                )
            opening.compiled = self._launcher._compile(target)
        compiled = opening.compiled

        if opening.binding is None:
            prepared = opening.prepared
            if prepared is None:
                raise RealmIntegrityError(
                    "local attempt opening has no prepared binding."
                )
            if opening.reservation is None:
                opening.reservation = self._launcher._reserve(compiled)
            try:
                opening.heartbeat.raise_if_failed()
                opening.binding = prepared._commit_reserved_launch(
                    opening.reservation
                )
            except BaseException:
                try:
                    opening.terminal_proof = self._launcher._abandon_reserved(
                        opening.reservation
                    )
                except Exception:
                    pass
                raise

        binding = opening.binding
        assert binding is not None
        if opening.reservation is None:
            if (
                binding.launch_intent.launch_request_digest
                != compiled.process_request.digest
            ):
                raise RealmIntegrityError(
                    "durable launch intent differs from the reconstructed request."
                )
            opening.reservation = self._launcher._lookup_reservation(compiled)
        reservation = opening.reservation
        assert reservation is not None

        state = self._launcher._reservation_state(reservation)
        if state == "reserved":
            attempt = self._ledger.read_run_attempt(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if attempt.state == "running":
                raise RealmIntegrityError(
                    "canonically running attempt still has a passive reservation."
                )
            proof = self._launcher._abandon_reserved(reservation)
            # Starting an already-terminal reservation only constructs a
            # replayable handle; the supervisor cannot spawn from this state.
            process = self._launcher._start_reserved(reservation)
        else:
            process = self._launcher._start_reserved(reservation)
            proof = process.stop(
                grace_period=grace_period, timeout=timeout
            )
        opening.terminal_proof = proof
        self._authenticate_terminal_binding(binding=binding, proof=proof)
        opening.handle = self._launcher._attach(
            binding=binding,
            compiled=compiled,
            process=process,
        )

    def _stop_and_wait_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        grace_period: float,
        timeout: float | None,
    ) -> LocalAttemptTerminal:

        key = self._coordinates(run_id, attempt_id)
        with self._coordinate_lock(key):
            session = self._sessions.get(key)
            if session is not None:
                return self._record_terminal(
                    session,
                    session.handle.stop(
                        grace_period=grace_period, timeout=timeout
                    ),
                )
            return self._stop_by_durable_coordinates(
                run_id=run_id,
                attempt_id=attempt_id,
                grace_period=grace_period,
                timeout=timeout,
            )

    def finalize_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        terminal: LocalAttemptTerminal,
    ) -> AttemptFinalization:
        """Collect and capture one authenticated terminal attempt.

        Started attempts must already be canonically running.  A never-started
        attempt remains prepared and produces an allowlisted platform failure
        without reading its writable trial scope.
        """

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        self._validate_terminal_argument(
            run_id=run_id, attempt_id=attempt_id, terminal=terminal
        )
        failure: LocalAttemptProviderError | None = None
        try:
            return self._finalize_terminal(
                run_id=run_id, attempt_id=attempt_id, terminal=terminal
            )
        except Exception as error:
            failure = self._normalize_public_error("finalize_terminal", error)
        assert failure is not None
        raise failure

    def _finalize_terminal(
        self,
        *,
        run_id: str,
        attempt_id: str,
        terminal: LocalAttemptTerminal,
    ) -> AttemptFinalization:
        key = self._coordinates(run_id, attempt_id)
        if not isinstance(terminal, LocalAttemptTerminal):
            raise TypeError("terminal must be a LocalAttemptTerminal.")
        if (terminal.run_id, terminal.attempt_id) != key:
            raise ValueError("terminal evidence names a different attempt.")
        with self._coordinate_lock(key):
            session = self._required_session(key)
            durable = self._ledger.read_run_attempt_terminal_evidence(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if durable != terminal.evidence:
                raise RealmIntegrityError(
                    "terminal evidence changed before finalization."
                )
            attempt = self._ledger.read_run_attempt(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if terminal.started:
                if attempt.state != "running":
                    raise RealmConflict(
                        "A physically started attempt must be confirmed before finalization."
                    )
            else:
                if attempt.state != "prepared":
                    raise RealmIntegrityError(
                        "Never-started evidence differs from canonical attempt state."
                    )
                return self._platform_failure(
                    session=session,
                    attempt=attempt,
                    code="worker_never_started",
                    disposition=terminal.disposition,
                )

            try:
                envelope = session.handle.collect()
            except LocalAttemptPlatformError as error:
                observed = self._record_terminal(
                    session, error.terminal_proof
                )
                if observed != terminal or error.code not in _PLATFORM_FAILURES:
                    raise RealmIntegrityError(
                        "local platform failure differs from terminal evidence."
                    ) from None
                return self._platform_failure(
                    session=session,
                    attempt=attempt,
                    code=error.code,
                    disposition=terminal.disposition,
                )
            return self._finalizer.finalize(
                envelope=envelope,
                binding=session.binding,
                change_id=attempt.capture_change_id,
            )

    def _remove_container_orphan(self, attempt) -> None:
        """Best-effort removal of a container the client never got to remove.

        --rm covers every exit the engine client lives to see. The one orphan
        case is the client being killed outright -- the watchdog's escalation
        path -- which leaves the container running with nobody holding its
        stream. Only container definitions pay the lookup; failure to remove
        is not failure to clean up, because the name is deterministic and the
        next generation removes it too.
        """

        try:
            definition = self._ledger.read_run_definition(
                actor_principal_id=self._actor_principal_id,
                run_id=attempt.run_id,
            )
            if (
                definition.evaluation_closure.prepared_runtime.runtime_kind
                != "container"
            ):
                return
            from ..container_engine import resolve_container_engine
            from ..container_method_process import remove_container_if_present
            from .local_attempt_launcher import RealmLocalAttemptLauncher

            remove_container_if_present(
                resolve_container_engine(),
                RealmLocalAttemptLauncher.container_attempt_name(
                    attempt.launch_token
                ),
            )
        except Exception:
            return

    def resume_cleanup(
        self, *, run_id: str, attempt_id: str
    ) -> LocalAttemptCleanupResult:
        """Retry adoption-authorized cleanup without a provider proof."""

        run_id, attempt_id = self._coordinates(run_id, attempt_id)
        failure: LocalAttemptProviderError | None = None
        try:
            return self._resume_cleanup(run_id=run_id, attempt_id=attempt_id)
        except Exception as error:
            failure = self._normalize_public_error("resume_cleanup", error)
        assert failure is not None
        raise failure

    def _resume_cleanup(
        self, *, run_id: str, attempt_id: str
    ) -> LocalAttemptCleanupResult:

        key = self._coordinates(run_id, attempt_id)
        with self._coordinate_lock(key):
            attempt = self._ledger.read_run_attempt(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if attempt.state != "terminal":
                raise RealmConflict(
                    "Provider cleanup requires a terminal canonical attempt."
                )
            self._remove_container_orphan(attempt)
            try:
                self._ledger.read_run_attempt_binding(
                    actor_principal_id=self._actor_principal_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
            except RealmNotFound:
                try:
                    retired = self._launcher._retire_passive_orphan(
                        launch_token=attempt.launch_token,
                        binding_id=attempt.binding_id,
                    )
                except Exception:
                    self._sessions.pop(key, None)
                    self._openings.pop(key, None)
                    return LocalAttemptCleanupResult(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        state="pending",
                        code="provider_cleanup_pending",
                    )
                self._sessions.pop(key, None)
                self._openings.pop(key, None)
                return LocalAttemptCleanupResult(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    state="cleaned" if retired else "not_required",
                )
            evidence = self._ledger.read_run_attempt_terminal_evidence(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            try:
                self._binder.resume_authorized_cleanup(
                    actor_principal_id=self._actor_principal_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
                self._launcher._retire_terminal(evidence)
            except Exception:
                self._sessions.pop(key, None)
                self._openings.pop(key, None)
                return LocalAttemptCleanupResult(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    state="pending",
                    code="provider_cleanup_pending",
                )
            self._sessions.pop(key, None)
            self._openings.pop(key, None)
            return LocalAttemptCleanupResult(
                run_id=run_id,
                attempt_id=attempt_id,
                state="cleaned",
            )

    def _create_opening(
        self,
        *,
        run_id: str,
        attempt_id: str,
        heartbeat: RunAttemptHeartbeatCoordinator,
    ) -> _LocalAttemptOpening:
        try:
            self._ledger.read_run_attempt_binding(
                actor_principal_id=self._actor_principal_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
        except RealmNotFound:
            try:
                prepared = self._binder.prepare_prepared(
                    actor_principal_id=self._actor_principal_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
            except RealmConflict as prepare_error:
                # Only an exact concurrently committed binding converts this
                # preflight conflict into recovery.
                try:
                    self._ledger.read_run_attempt_binding(
                        actor_principal_id=self._actor_principal_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                    )
                except RealmNotFound:
                    raise prepare_error
            else:
                self._attach_heartbeat(heartbeat, prepared)
                return _LocalAttemptOpening(
                    heartbeat_target=prepared,
                    heartbeat_identity=id(heartbeat),
                    heartbeat=heartbeat,
                    prepared=prepared,
                )
        binding = self._binder.recover(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        self._attach_heartbeat(heartbeat, binding)
        return _LocalAttemptOpening(
            heartbeat_target=binding,
            heartbeat_identity=id(heartbeat),
            heartbeat=heartbeat,
            binding=binding,
        )

    def _adopt_opening_heartbeat(
        self,
        opening: _LocalAttemptOpening,
        heartbeat: RunAttemptHeartbeatCoordinator,
    ) -> None:
        if opening.heartbeat_identity == id(heartbeat):
            return
        self._attach_heartbeat(heartbeat, opening.heartbeat_target)
        opening.heartbeat_identity = id(heartbeat)
        opening.heartbeat = heartbeat

    def _advance_opening(
        self,
        *,
        run_id: str,
        attempt_id: str,
        opening: _LocalAttemptOpening,
    ) -> _LocalAttemptSession:
        if opening.handle is not None and opening.binding is not None:
            return self._session_from_opening(opening)
        try:
            opening.heartbeat.raise_if_failed()
        except Exception:
            if opening.binding is not None:
                try:
                    self._stop_by_durable_coordinates(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        grace_period=0.25,
                        timeout=10.0,
                    )
                except Exception:
                    raise RealmIntegrityError(
                        "heartbeat failed and exact startup drain remains pending."
                    ) from None
            raise
        if opening.compiled is None:
            target = opening.binding or opening.prepared
            if target is None:
                raise RealmIntegrityError(
                    "local attempt opening has no compilable binding."
                )
            opening.compiled = self._launcher._compile(target)
        compiled = opening.compiled
        if opening.binding is None:
            prepared = opening.prepared
            if prepared is None:
                raise RealmIntegrityError(
                    "local attempt opening has no prepared binding."
                )
            if opening.reservation is None:
                opening.reservation = self._launcher._reserve(compiled)
            try:
                opening.heartbeat.raise_if_failed()
                opening.binding = prepared._commit_reserved_launch(
                    opening.reservation
                )
            except Exception:
                try:
                    opening.terminal_proof = self._launcher._abandon_reserved(
                        opening.reservation
                    )
                except RealmConflict:
                    pass
                except Exception:
                    pass
                raise
        binding = opening.binding
        assert binding is not None
        if opening.reservation is None:
            if (
                binding.launch_intent.launch_request_digest
                != compiled.process_request.digest
            ):
                raise RealmIntegrityError(
                    "durable launch intent differs from the reconstructed request."
                )
            # A binding proves reserve already happened.  Recovery is strictly
            # lookup-only so registry loss cannot duplicate a worker.
            opening.reservation = self._launcher._lookup_reservation(compiled)
        reservation = opening.reservation
        assert reservation is not None
        if opening.terminal_proof is not None:
            process = self._launcher._start_reserved(reservation)
            opening.handle = self._launcher._attach(
                binding=binding, compiled=compiled, process=process
            )
            return self._session_from_opening(opening)

        process: SupervisedProcess | None = None
        try:
            opening.heartbeat.raise_if_failed()
            self._launcher._publish(compiled)
            opening.heartbeat.raise_if_failed()
            process = self._launcher._start_reserved(reservation)
            opening.handle = self._launcher._attach(
                binding=binding, compiled=compiled, process=process
            )
        except Exception:
            try:
                self._drain_opening(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    opening=opening,
                    process=process,
                )
            except Exception:
                raise RealmIntegrityError(
                    "local attempt startup failed and exact drain remains pending."
                ) from None
            raise
        return self._session_from_opening(opening)

    def _drain_opening(
        self,
        *,
        run_id: str,
        attempt_id: str,
        opening: _LocalAttemptOpening,
        process: SupervisedProcess | None,
    ) -> None:
        binding = opening.binding
        compiled = opening.compiled
        reservation = opening.reservation
        if binding is None or compiled is None or reservation is None:
            raise RealmIntegrityError(
                "local attempt drain lacks exact committed startup state."
            )
        state = self._launcher._reservation_state(reservation)
        attempt = self._ledger.read_run_attempt(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        if state == "reserved":
            if attempt.state == "running":
                raise RealmIntegrityError(
                    "canonically running attempt still has a passive reservation."
                )
            proof = self._launcher._abandon_reserved(reservation)
            process = self._launcher._start_reserved(reservation)
        else:
            if process is None:
                process = self._launcher._start_reserved(reservation)
            proof = process.stop(grace_period=0.25, timeout=10.0)
        opening.terminal_proof = proof
        self._authenticate_terminal_binding(binding=binding, proof=proof)
        # Installing a terminal handle makes exact retry cheap.  A second attach
        # failure does not undo the already authenticated drain; retry can attach
        # to the same terminal reservation later.
        try:
            opening.handle = self._launcher._attach(
                binding=binding, compiled=compiled, process=process
            )
        except Exception:
            opening.handle = None

    @staticmethod
    def _session_from_opening(
        opening: _LocalAttemptOpening,
    ) -> _LocalAttemptSession:
        if opening.binding is None or opening.handle is None:
            raise RealmIntegrityError(
                "local attempt opening is not ready for publication."
            )
        return _LocalAttemptSession(
            binding=opening.binding,
            handle=opening.handle,
            heartbeat_target=opening.heartbeat_target,
            heartbeat_identity=opening.heartbeat_identity,
            heartbeat=opening.heartbeat,
        )

    def _wait_started_with_heartbeat(
        self, session: _LocalAttemptSession
    ) -> WorkerStarted | WorkerTerminalProof:
        while True:
            self._raise_heartbeat_or_stop(session)
            try:
                observation = session.handle.wait_started(timeout=0.25)
            except TimeoutError:
                continue
            self._raise_heartbeat_or_stop(session)
            return observation

    def _wait_terminal_with_heartbeat(
        self,
        session: _LocalAttemptSession,
        *,
        timeout: float | None,
    ) -> WorkerTerminalProof:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise ValueError("timeout must be nonnegative or None.")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            self._raise_heartbeat_or_stop(session)
            wait_for = 0.25
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("waiting for local attempt timed out.")
                wait_for = min(wait_for, remaining)
            try:
                proof = session.handle.wait(timeout=wait_for)
            except TimeoutError:
                continue
            self._raise_heartbeat_or_stop(session)
            return proof

    def _raise_heartbeat_or_stop(
        self, session: _LocalAttemptSession
    ) -> None:
        try:
            session.heartbeat.raise_if_failed()
        except Exception as heartbeat_error:
            try:
                proof = session.handle.stop(
                    grace_period=0.25, timeout=10.0
                )
                self._record_terminal(session, proof)
            except Exception as stop_error:
                heartbeat_error.add_note(
                    "Stopping and authenticating the worker after heartbeat "
                    "failure also failed: "
                    f"{type(stop_error).__name__}."
                )
            raise

    def _stop_by_durable_coordinates(
        self,
        *,
        run_id: str,
        attempt_id: str,
        grace_period: float,
        timeout: float | None,
    ) -> LocalAttemptTerminal:
        """Fence a stale-generation launch without reattaching its resources."""

        attempt = self._ledger.read_run_attempt(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        binding = self._ledger.read_run_attempt_binding(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        launch = self._ledger.read_run_attempt_launch_intent(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        try:
            launch.validate_binding(binding, attempt)
        except ValueError as error:
            raise RealmIntegrityError(
                "durable launch chain is inconsistent."
            ) from error
        reservation = self._launcher._lookup_reservation_coordinates(
            launch_token=launch.launch_token,
            binding_id=launch.binding_id,
            evidence_fingerprint=launch.evidence_fingerprint,
            launch_request_digest=launch.launch_request_digest,
        )
        reservation_state = self._launcher._reservation_state(reservation)
        if reservation_state == "reserved":
            if attempt.state == "running":
                # Never irreversibly turn a passive provider row into
                # never-started evidence that canonical running state rejects.
                raise RealmIntegrityError(
                    "canonically running attempt still has a passive reservation."
                )
            proof = self._launcher._abandon_reserved(reservation)
        else:
            process = self._launcher._start_reserved(reservation)
            proof = process.stop(
                grace_period=grace_period, timeout=timeout
            )
        evidence = self._authenticate_terminal_coordinates(
            run_id=run_id,
            attempt_id=attempt_id,
            binding=binding,
            attempt=attempt,
            launch=launch,
            proof=proof,
        )
        return LocalAttemptTerminal(evidence=evidence)

    def _authenticate_terminal_coordinates(
        self,
        *,
        run_id: str,
        attempt_id: str,
        binding: ExecutionBindingRecord,
        attempt: RunAttemptRecord,
        launch: ExecutionLaunchIntentRecord,
        proof: WorkerTerminalProof,
    ) -> ExecutionTerminalEvidenceRecord:
        evidence = self._binder.authenticate_and_record_terminal(
            actor_principal_id=self._actor_principal_id,
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_proof=proof,
        )
        if not isinstance(evidence, ExecutionTerminalEvidenceRecord):
            raise RealmIntegrityError(
                "terminal authentication did not return retained evidence."
            )
        try:
            evidence.validate_launch_intent(binding, attempt, launch)
        except (TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "retained terminal evidence differs from the durable launch."
            ) from error
        if (
            evidence.launch_token != proof.launch_token
            or evidence.binding_id != proof.binding_id
            or evidence.evidence_fingerprint != proof.evidence_fingerprint
            or evidence.launch_request_digest != proof.launch_request_digest
            or evidence.started != proof.started
            or evidence.disposition != proof.disposition
        ):
            raise RealmIntegrityError(
                "retained terminal evidence differs from the provider proof."
            )
        return evidence

    def _record_terminal(
        self,
        session: _LocalAttemptSession,
        proof: WorkerTerminalProof,
    ) -> LocalAttemptTerminal:
        return LocalAttemptTerminal(
            evidence=self._authenticate_terminal_binding(
                binding=session.binding, proof=proof
            )
        )

    def _authenticate_terminal_binding(
        self,
        *,
        binding: ManagedProcessExecutionBinding,
        proof: WorkerTerminalProof,
    ) -> ExecutionTerminalEvidenceRecord:
        if not isinstance(proof, WorkerTerminalProof):
            raise TypeError("proof must be a WorkerTerminalProof.")
        # The managed binding authenticates the private provider proof and
        # commits its path-free fingerprint.  The raw value remains in this
        # stack frame and is never included in a public result.
        evidence = binding.authenticate_and_record_terminal(proof)
        if not isinstance(evidence, ExecutionTerminalEvidenceRecord):
            raise RealmIntegrityError(
                "terminal authentication did not return retained evidence."
            )
        try:
            evidence.validate_launch_intent(
                binding.receipt.binding,
                binding.receipt.attempt,
                binding.launch_intent,
            )
        except ValueError as error:
            raise RealmIntegrityError(
                "retained terminal evidence differs from the managed launch."
            ) from error
        if (
            evidence.launch_token != proof.launch_token
            or evidence.binding_id != proof.binding_id
            or evidence.evidence_fingerprint != proof.evidence_fingerprint
            or evidence.launch_request_digest != proof.launch_request_digest
            or evidence.started != proof.started
            or evidence.disposition != proof.disposition
        ):
            raise RealmIntegrityError(
                "retained terminal evidence differs from the provider proof."
            )
        return evidence

    def _platform_failure(
        self,
        *,
        session: _LocalAttemptSession,
        attempt: RunAttemptRecord,
        code: str,
        disposition: str,
    ) -> AttemptFinalization:
        try:
            message, phase = _PLATFORM_FAILURES[code]
        except KeyError as error:
            raise RealmIntegrityError(
                "local platform failure code is not allowlisted."
            ) from error
        return AttemptFinalization(
            attempt_id=attempt.attempt_id,
            evaluation_spec_digest=attempt.evaluation_spec_digest,
            binding_id=session.binding.receipt.binding.binding_id,
            effective_outcome="failed",
            effective_code=code,
            captured_artifacts=(),
            platform_error={
                "code": code,
                "message": message,
                "details": {
                    "phase": phase,
                    "worker_disposition": disposition,
                },
            },
        )

    @staticmethod
    def _attach_heartbeat(
        heartbeat: RunAttemptHeartbeatCoordinator,
        target: PreparedProcessExecutionBinding | ManagedProcessExecutionBinding,
    ) -> None:
        heartbeat.attach_binding(target)

    def _required_session(
        self, key: tuple[str, str]
    ) -> _LocalAttemptSession:
        try:
            return self._sessions[key]
        except KeyError as error:
            raise RealmConflict(
                "start_or_attach must establish this provider session first."
            ) from error

    @staticmethod
    def _validate_heartbeat_shape(
        heartbeat: RunAttemptHeartbeatCoordinator,
    ) -> None:
        if not callable(getattr(heartbeat, "attach_binding", None)):
            raise TypeError("heartbeat must provide attach_binding().")
        if not callable(getattr(heartbeat, "raise_if_failed", None)):
            raise TypeError("heartbeat must provide raise_if_failed().")

    @staticmethod
    def _validate_timeout(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not float("-inf") < float(timeout) < float("inf")
            or timeout < 0
        ):
            raise ValueError("timeout must be finite and nonnegative or None.")
        return float(timeout)

    @staticmethod
    def _validate_grace_period(grace_period: float) -> float:
        if (
            isinstance(grace_period, bool)
            or not isinstance(grace_period, (int, float))
            or not float("-inf") < float(grace_period) < float("inf")
            or grace_period < 0
        ):
            raise ValueError("grace_period must be finite and nonnegative.")
        return float(grace_period)

    @staticmethod
    def _validate_terminal_argument(
        *, run_id: str, attempt_id: str, terminal: LocalAttemptTerminal
    ) -> None:
        if not isinstance(terminal, LocalAttemptTerminal):
            raise TypeError("terminal must be a LocalAttemptTerminal.")
        if (terminal.run_id, terminal.attempt_id) != (run_id, attempt_id):
            raise ValueError("terminal evidence names a different attempt.")

    @staticmethod
    def _normalize_public_error(
        operation: str, error: Exception
    ) -> LocalAttemptProviderError:
        if isinstance(error, TimeoutError):
            code = "provider_timeout"
        elif isinstance(error, RealmNotFound):
            code = "provider_state_unavailable"
        elif isinstance(error, RealmIntegrityError):
            code = "provider_integrity_failure"
        elif isinstance(error, RealmConflict):
            code = "provider_state_conflict"
        elif isinstance(error, ProcessExecutionResourceError):
            code = "provider_resource_failure"
        else:
            code = "provider_operation_failed"
        return LocalAttemptProviderError(operation=operation, code=code)

    def _coordinate_lock(
        self, key: tuple[str, str]
    ) -> threading.RLock:
        with self._state_lock:
            return self._coordinate_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _coordinates(run_id: str, attempt_id: str) -> tuple[str, str]:
        return (
            required_text(run_id, "local attempt run id"),
            required_text(attempt_id, "local attempt id"),
        )


__all__ = [
    "LocalAttemptCleanupResult",
    "LocalAttemptProviderError",
    "LocalAttemptStarted",
    "LocalAttemptStartObservation",
    "LocalAttemptTerminal",
    "LocalProcessAttemptProvider",
]
