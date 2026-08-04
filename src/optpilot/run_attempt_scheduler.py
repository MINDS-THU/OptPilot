"""Canonical-state driver for one local-process run attempt.

The scheduler owns no workspace, backend handle, launch request, terminal proof,
or caller-supplied operation id.  It repeatedly reads the Realm run head and
coordinates the two lifecycle owners that already exist:

* :class:`RunAttemptHeartbeatCoordinator` keeps live authority/resources valid;
* :class:`LocalProcessAttemptProvider` owns every local platform side effect.

Consequently, calling :meth:`RunAttemptScheduler.advance` after a crash is the
same operation as calling it for the first time.  Canonical terminal state is
the completion boundary; provider cleanup is idempotent debt reported alongside
that state rather than another attempt outcome.
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, TypeAlias

from .attempts import AttemptFinalization
from .realm.errors import RealmConflict, RealmIntegrityError
from .realm.execution_binding_records import (
    ExecutionBindingRecord,
    ExecutionLaunchIntentRecord,
    ExecutionTerminalEvidenceRecord,
)
from .realm.local_process_attempt_provider import (
    LocalAttemptCleanupResult,
    LocalAttemptStarted,
    LocalAttemptTerminal,
    LocalProcessAttemptProvider,
)
from .realm.run_attempt_records import (
    RunAttemptHeartbeatAuthorityReceipt,
    RunAttemptRecord,
)
from .realm.run_records import LogicalTrialTransitionRecord
from .realm.run_snapshot import RunLedgerSnapshot
from .run_attempt_heartbeat import RunAttemptHeartbeatCoordinator


RunAttemptAdvanceAction: TypeAlias = Literal[
    "adopted", "terminalized", "reconciled", "cleanup_only"
]


# Canonical attempt writes are optimistic compare-and-swap operations.  The
# scheduler serializes its own short phases, but provider binding commits are
# deliberately allowed to advance the same run head while another evaluator is
# realizing resources or waiting for a handshake.  A bounded retry therefore
# handles unrelated, forward-only head drift without replaying provider work.
_CANONICAL_HEAD_RETRY_LIMIT = 64


class _MutableRunAuthority(Protocol):
    """The canonical cursor surface shared with ``RetainedRunAuthority``."""

    ledger: Any
    actor_principal_id: str
    run_id: str
    owner_id: str
    run_revision: int
    owner_revision: int
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int

    def refresh_controller(self) -> RunLedgerSnapshot: ...


class _AttemptHeartbeat(Protocol):
    def start(self) -> Any: ...

    def heartbeat_once(self) -> Any: ...

    def raise_if_failed(self) -> None: ...

    def stop(self) -> None: ...


HeartbeatFactory: TypeAlias = Callable[
    [RunAttemptHeartbeatAuthorityReceipt], _AttemptHeartbeat
]


@dataclass(frozen=True)
class RunAttemptHeartbeatPolicy:
    """Optional timing overrides for the default heartbeat coordinator."""

    ttl_seconds: float | None = None
    interval_seconds: float | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.ttl_seconds, "ttl_seconds"),
            (self.interval_seconds, "interval_seconds"),
        ):
            if value is not None:
                _positive_finite(value, label)


@dataclass(frozen=True)
class RunAttemptAdvanceResult:
    """Small, path-free result for one canonical advance decision."""

    attempt: RunAttemptRecord
    logical_transition: LogicalTrialTransitionRecord
    action: RunAttemptAdvanceAction
    cleanup: LocalAttemptCleanupResult
    physically_started: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, RunAttemptRecord):
            raise TypeError("attempt must be a RunAttemptRecord.")
        if not isinstance(self.logical_transition, LogicalTrialTransitionRecord):
            raise TypeError(
                "logical_transition must be a LogicalTrialTransitionRecord."
            )
        if self.action not in {
            "adopted",
            "terminalized",
            "reconciled",
            "cleanup_only",
        }:
            raise ValueError("action is unsupported.")
        if not isinstance(self.cleanup, LocalAttemptCleanupResult):
            raise TypeError("cleanup must be a LocalAttemptCleanupResult.")
        if not isinstance(self.physically_started, bool):
            raise TypeError("physically_started must be a boolean.")
        if (
            self.attempt.state != "terminal"
            or self.logical_transition.logical_trial_id
            != self.attempt.logical_trial_id
            or self.logical_transition.attempt_id != self.attempt.attempt_id
            or self.cleanup.run_id != self.attempt.run_id
            or self.cleanup.attempt_id != self.attempt.attempt_id
        ):
            raise ValueError("advance result canonical anchors do not agree.")


class RunAttemptScheduler:
    """Advance or recover one logical attempt to canonical terminal state.

    Cursor mutation is intentionally serialized by this object.  Physical
    process work may block, while every canonical write is followed by a fresh
    snapshot instead of locally applying an assumed ``revision + 1`` receipt.
    """

    def __init__(
        self,
        authority: _MutableRunAuthority,
        provider: LocalProcessAttemptProvider,
        *,
        heartbeat_factory: HeartbeatFactory | None = None,
        heartbeat_policy: RunAttemptHeartbeatPolicy | None = None,
    ) -> None:
        _validate_authority(authority)
        _validate_provider(provider)
        if heartbeat_factory is not None and not callable(heartbeat_factory):
            raise TypeError("heartbeat_factory must be callable or None.")
        policy = heartbeat_policy or RunAttemptHeartbeatPolicy()
        if not isinstance(policy, RunAttemptHeartbeatPolicy):
            raise TypeError(
                "heartbeat_policy must be a RunAttemptHeartbeatPolicy or None."
            )
        provider_ledger = getattr(provider, "_ledger", authority.ledger)
        if provider_ledger is not authority.ledger:
            raise ValueError("authority and provider must share the exact ledger.")
        provider_actor = getattr(
            provider, "_actor_principal_id", authority.actor_principal_id
        )
        if provider_actor != authority.actor_principal_id:
            raise ValueError("authority and provider must share the actor principal.")
        self.authority = authority
        self.provider = provider
        self.heartbeat_factory = heartbeat_factory
        self.heartbeat_policy = policy
        # One authority cursor is shared by every attempt in this run.  Keep
        # scheduler-owned canonical phases serialized while allowing provider
        # realization, binding, launch, handshake, and evaluation to overlap.
        # Provider binding commits intentionally sit outside this lock; the
        # short compare-and-swap writes below refresh and retry when one of
        # those commits advances the run head.
        self._canonical_phase_lock = threading.RLock()

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float = 300.0,
    ) -> RunAttemptAdvanceResult:
        """Drive exactly ``attempt_id`` to terminal, recovering if necessary."""

        logical_trial_id = _required_text(logical_trial_id, "logical_trial_id")
        attempt_id = _required_text(attempt_id, "attempt_id")
        ttl = _positive_finite(attempt_ttl_seconds, "attempt_ttl_seconds")

        with self._canonical_phase_lock:
            snapshot = self._refresh()
            attempt = _find_attempt(snapshot, attempt_id)
            if attempt is None:
                snapshot = self._prepare_or_recover(
                    snapshot=snapshot,
                    logical_trial_id=logical_trial_id,
                    attempt_id=attempt_id,
                    attempt_ttl_seconds=ttl,
                )
                attempt = _require_attempt(snapshot, attempt_id)
            if attempt.logical_trial_id != logical_trial_id:
                raise RealmConflict("Attempt id belongs to a different logical trial.")

            if attempt.state == "terminal":
                return self._terminal_result(
                    snapshot=snapshot,
                    attempt=attempt,
                    action="cleanup_only",
                    physically_started=_physical_start_from_snapshot(
                        snapshot, attempt.attempt_id
                    ),
                )

            current_generation = snapshot.run.controller_generation
            if attempt.controller_generation > current_generation:
                raise RealmIntegrityError(
                    "Attempt controller generation is ahead of the canonical run."
                )
            if attempt.controller_generation < current_generation:
                return self._reconcile_stale(snapshot=snapshot, attempt=attempt)
            if attempt.state not in {"prepared", "running"}:
                raise RealmIntegrityError("Run attempt state is unsupported.")
        return self._drive_current(snapshot=snapshot, attempt=attempt)

    def terminalize(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
    ) -> RunAttemptAdvanceResult:
        """Stop or abandon one exact active attempt without launching it.

        This is the cancellation counterpart to :meth:`advance`.  A passive
        prepared attempt crosses the durable binding boundary so its resources
        can be authenticated, but the provider abandons its reservation before
        physical start.  A worker whose start was already requested is stopped
        and finalized normally.
        """

        logical_trial_id = _required_text(logical_trial_id, "logical_trial_id")
        attempt_id = _required_text(attempt_id, "attempt_id")
        snapshot = self._refresh()
        attempt = _require_attempt(snapshot, attempt_id)
        if attempt.logical_trial_id != logical_trial_id:
            raise RealmConflict("Attempt id belongs to a different logical trial.")
        if attempt.state == "terminal":
            return self._terminal_result(
                snapshot=snapshot,
                attempt=attempt,
                action="cleanup_only",
                physically_started=_physical_start_from_snapshot(
                    snapshot, attempt.attempt_id
                ),
            )

        current_generation = snapshot.run.controller_generation
        if attempt.controller_generation > current_generation:
            raise RealmIntegrityError(
                "Attempt controller generation is ahead of the canonical run."
            )
        if attempt.controller_generation < current_generation:
            return self._reconcile_stale(snapshot=snapshot, attempt=attempt)
        if attempt.state not in {"prepared", "running"}:
            raise RealmIntegrityError("Run attempt state is unsupported.")

        heartbeat_receipt = (
            self.authority.ledger.read_run_attempt_heartbeat_authority(
                actor_principal_id=self.authority.actor_principal_id,
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
            )
        )
        heartbeat = self._make_heartbeat(heartbeat_receipt)
        try:
            heartbeat.start()
            heartbeat.raise_if_failed()
            terminal = self.provider.terminalize_current(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                heartbeat=heartbeat,  # type: ignore[arg-type]
            )
            heartbeat.raise_if_failed()
            snapshot = self._refresh()
            attempt = _require_attempt(snapshot, attempt.attempt_id)
            if terminal.started:
                snapshot = self._confirm_started_or_recover(
                    snapshot=snapshot,
                    attempt=attempt,
                    observation=terminal,
                )
                attempt = _require_attempt(snapshot, attempt.attempt_id)
            _validate_terminal_observation(snapshot, attempt, terminal)
            finalization = self.provider.finalize_terminal(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                terminal=terminal,
            )
            _validate_finalization(finalization, attempt)
            heartbeat.heartbeat_once()
            heartbeat.raise_if_failed()
        except BaseException as error:
            _stop_heartbeat_without_masking(heartbeat, error)
            raise
        else:
            heartbeat.stop()

        return self._adopt_finalization(
            snapshot=self._refresh(),
            attempt_id=attempt.attempt_id,
            finalization=finalization,
            physically_started=terminal.started,
            action="terminalized",
        )

    def _prepare_or_recover(
        self,
        *,
        snapshot: RunLedgerSnapshot,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> RunLedgerSnapshot:
        last_conflict: RealmConflict | None = None
        current = snapshot
        for _retry in range(_CANONICAL_HEAD_RETRY_LIMIT):
            revision = current.revision.revision
            operation_id = _operation_id(
                run_id=self.authority.run_id,
                attempt_id=attempt_id,
                action="prepare",
                run_revision=revision,
                owner_revision=current.revision.owner_revision,
            )
            try:
                self.authority.ledger.prepare_run_attempt(
                    operation_id=operation_id,
                    actor_principal_id=self.authority.actor_principal_id,
                    run_id=self.authority.run_id,
                    logical_trial_id=logical_trial_id,
                    attempt_id=attempt_id,
                    expected_run_revision=revision,
                    controller_lease_id=self.authority.controller_lease_id,
                    controller_holder_id=self.authority.controller_holder_id,
                    controller_fencing_token=(
                        self.authority.controller_fencing_token
                    ),
                    attempt_ttl_seconds=attempt_ttl_seconds,
                )
            except Exception as error:
                recovered = self._refresh_after_error(error, "attempt preparation")
                attempt = _find_attempt(recovered, attempt_id)
                if (
                    attempt is not None
                    and attempt.logical_trial_id == logical_trial_id
                    and attempt.prepared_run_revision == revision + 1
                ):
                    return recovered
                if (
                    not isinstance(error, RealmConflict)
                    or attempt is not None
                    or not _run_head_advanced(current, recovered)
                ):
                    raise
                # Another evaluator committed a binding (or another short
                # controller phase advanced the head) after our refresh.  No
                # attempt was created by this operation, so retry only the
                # canonical preparation against the new head.
                last_conflict = error
                current = recovered
                continue
            return self._refresh()
        assert last_conflict is not None
        last_conflict.add_note(
            "Attempt preparation could not acquire a stable run head after "
            "repeated concurrent canonical commits."
        )
        raise last_conflict

    def _reconcile_stale(
        self, *, snapshot: RunLedgerSnapshot, attempt: RunAttemptRecord
    ) -> RunAttemptAdvanceResult:
        binding, launch, evidence = _execution_records(snapshot, attempt.attempt_id)
        _validate_execution_shape(
            attempt=attempt,
            binding=binding,
            launch=launch,
            evidence=evidence,
        )
        physically_started = evidence.started if evidence is not None else False
        if binding is not None and evidence is None:
            terminal = self.provider.stop_and_wait_terminal(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
            )
            if not isinstance(terminal, LocalAttemptTerminal):
                raise TypeError(
                    "provider.stop_and_wait_terminal() must return "
                    "LocalAttemptTerminal."
                )
            physically_started = terminal.started
            snapshot = self._refresh()
            refreshed_attempt = _require_attempt(snapshot, attempt.attempt_id)
            binding, launch, evidence = _execution_records(
                snapshot, attempt.attempt_id
            )
            _validate_execution_shape(
                attempt=refreshed_attempt,
                binding=binding,
                launch=launch,
                evidence=evidence,
            )
            if evidence != terminal.evidence:
                raise RealmIntegrityError(
                    "Provider terminal observation differs from retained evidence."
                )
            attempt = refreshed_attempt

        revision = snapshot.revision.revision
        owner_revision = snapshot.revision.owner_revision
        operation_id = _operation_id(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            action="reconcile",
            run_revision=revision,
            owner_revision=owner_revision,
        )
        try:
            self.authority.ledger.reconcile_lost_run_attempt(
                operation_id=operation_id,
                actor_principal_id=self.authority.actor_principal_id,
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                expected_run_revision=revision,
                expected_owner_revision=owner_revision,
                controller_lease_id=self.authority.controller_lease_id,
                controller_holder_id=self.authority.controller_holder_id,
                controller_fencing_token=self.authority.controller_fencing_token,
            )
        except Exception as error:
            recovered = self._refresh_after_error(error, "lost-attempt reconciliation")
            canonical = _require_attempt(recovered, attempt.attempt_id)
            if canonical.state != "terminal":
                raise
            snapshot = recovered
        else:
            snapshot = self._refresh()
        canonical = _require_attempt(snapshot, attempt.attempt_id)
        if canonical.state != "terminal":
            raise RealmIntegrityError(
                "Lost-attempt reconciliation did not produce terminal state."
            )
        return self._terminal_result(
            snapshot=snapshot,
            attempt=canonical,
            action="reconciled",
            physically_started=physically_started,
        )

    def _drive_current(
        self, *, snapshot: RunLedgerSnapshot, attempt: RunAttemptRecord
    ) -> RunAttemptAdvanceResult:
        with self._canonical_phase_lock:
            snapshot = self._refresh()
            attempt = _require_attempt(snapshot, attempt.attempt_id)
            heartbeat_receipt = (
                self.authority.ledger.read_run_attempt_heartbeat_authority(
                    actor_principal_id=self.authority.actor_principal_id,
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                )
            )
        heartbeat = self._make_heartbeat(heartbeat_receipt)
        provider_entered = False
        terminal: LocalAttemptTerminal | None = None
        physically_started = False
        heartbeat_stopped = False
        try:
            heartbeat.start()
            heartbeat.raise_if_failed()
            # Re-read immediately before crossing into provider startup.  A
            # cancellation or replacement controller may have resolved this
            # attempt since ``advance`` selected it.  The provider also checks
            # the exact durable attempt authority at bind/commit time, but this
            # short canonical check avoids entering it when resolution already
            # won the race.
            with self._canonical_phase_lock:
                snapshot = self._refresh()
                attempt = _require_attempt(snapshot, attempt.attempt_id)
                terminal_before_start = attempt.state == "terminal"
            if terminal_before_start:
                heartbeat.stop()
                heartbeat_stopped = True
                return self._terminal_result(
                    snapshot=snapshot,
                    attempt=attempt,
                    action="cleanup_only",
                    physically_started=_physical_start_from_snapshot(
                        snapshot, attempt.attempt_id
                    ),
                )
            # Provider startup owns its own exact attempt coordinate and its
            # binding commit retries harmless run-head drift.  Do not hold the
            # run's canonical cursor lock while realizing resources, launching
            # the worker, or waiting for its authenticated handshake: those
            # operations can dominate a short evaluation and would otherwise
            # make declared evaluator capacity appear serial.  Re-enter the
            # canonical phase only to confirm the observed physical start.
            provider_entered = True
            observation = self.provider.start_or_attach(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                heartbeat=heartbeat,  # type: ignore[arg-type]
            )
            heartbeat.raise_if_failed()
            if not isinstance(
                observation, (LocalAttemptStarted, LocalAttemptTerminal)
            ):
                raise TypeError(
                    "provider.start_or_attach() returned an unsupported observation."
                )
            physically_started = (
                True
                if isinstance(observation, LocalAttemptStarted)
                else observation.started
            )
            with self._canonical_phase_lock:
                snapshot = self._refresh()
                attempt = _require_attempt(snapshot, attempt.attempt_id)
                terminal_during_start = attempt.state == "terminal"
                if not terminal_during_start and physically_started:
                    snapshot = self._confirm_started_or_recover(
                        snapshot=snapshot,
                        attempt=attempt,
                        observation=observation,
                    )
                    attempt = _require_attempt(snapshot, attempt.attempt_id)
                elif not terminal_during_start:
                    if attempt.state != "prepared":
                        raise RealmIntegrityError(
                            "Never-started evidence requires a prepared attempt."
                        )
                    _validate_terminal_observation(snapshot, attempt, observation)

            if terminal_during_start:
                heartbeat.stop()
                heartbeat_stopped = True
                return self._terminal_result(
                    snapshot=snapshot,
                    attempt=attempt,
                    action="cleanup_only",
                    physically_started=_physical_start_from_snapshot(
                        snapshot, attempt.attempt_id
                    ),
                )

            # The provider wait remains concurrent.  It may retain terminal
            # evidence, but it does not mutate the run/owner revision cursor
            # used by another attempt's canonical phase.
            if isinstance(observation, LocalAttemptStarted):
                terminal = self.provider.wait_terminal(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                )
            else:
                terminal = observation
            if not isinstance(terminal, LocalAttemptTerminal):
                raise TypeError(
                    "provider.wait_terminal() must return LocalAttemptTerminal."
                )
            heartbeat.raise_if_failed()
            # Finalization may create an owner capture consumed by adoption.
            # Keep both operations in the same phase so another attempt cannot
            # advance the owner cursor between them.
            with self._canonical_phase_lock:
                snapshot = self._refresh()
                attempt = _require_attempt(snapshot, attempt.attempt_id)
                _validate_terminal_observation(snapshot, attempt, terminal)

                finalization = self.provider.finalize_terminal(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    terminal=terminal,
                )
                _validate_finalization(finalization, attempt)
                heartbeat.heartbeat_once()
                heartbeat.raise_if_failed()
                # This is the handoff fence: no renewal thread remains when
                # canonical adoption consumes the final heartbeat round.
                heartbeat.stop()
                heartbeat_stopped = True
                return self._adopt_finalization(
                    snapshot=self._refresh(),
                    attempt_id=attempt.attempt_id,
                    finalization=finalization,
                    physically_started=physically_started,
                )
        except Exception as error:
            if provider_entered and terminal is None:
                _stop_provider_without_masking(
                    provider=self.provider,
                    attempt=attempt,
                    primary=error,
                )
            if not heartbeat_stopped:
                _stop_heartbeat_without_masking(heartbeat, error)
            raise

    def _confirm_started_or_recover(
        self,
        *,
        snapshot: RunLedgerSnapshot,
        attempt: RunAttemptRecord,
        observation: LocalAttemptStarted | LocalAttemptTerminal,
    ) -> RunLedgerSnapshot:
        attempt_id = attempt.attempt_id
        current = snapshot
        canonical = attempt
        last_conflict: RealmConflict | None = None
        for _retry in range(_CANONICAL_HEAD_RETRY_LIMIT):
            _validate_started_observation(current, canonical, observation)
            if canonical.state == "running":
                return current
            if canonical.state != "prepared":
                raise RealmConflict(
                    "Physically started attempt is neither prepared nor running."
                )
            binding, launch, _ = _execution_records(current, attempt_id)
            assert binding is not None and launch is not None
            revision = current.revision.revision
            operation_id = _operation_id(
                run_id=canonical.run_id,
                attempt_id=attempt_id,
                action="confirm",
                run_revision=revision,
                owner_revision=current.revision.owner_revision,
            )
            try:
                self.authority.ledger.confirm_run_attempt_launch(
                    operation_id=operation_id,
                    actor_principal_id=self.authority.actor_principal_id,
                    run_id=canonical.run_id,
                    attempt_id=attempt_id,
                    launch_token=canonical.launch_token,
                    binding_id=binding.binding_id,
                    evidence_fingerprint=binding.evidence_fingerprint,
                    launch_request_digest=launch.launch_request_digest,
                    expected_run_revision=revision,
                    controller_lease_id=self.authority.controller_lease_id,
                    controller_holder_id=self.authority.controller_holder_id,
                    controller_fencing_token=(
                        self.authority.controller_fencing_token
                    ),
                )
            except Exception as error:
                recovered = self._refresh_after_error(error, "attempt confirmation")
                recovered_attempt = _require_attempt(recovered, attempt_id)
                if recovered_attempt.state == "running":
                    _validate_started_observation(
                        recovered, recovered_attempt, observation
                    )
                    return recovered
                if (
                    not isinstance(error, RealmConflict)
                    or recovered_attempt.state != "prepared"
                    or not _run_head_advanced(current, recovered)
                ):
                    raise
                # Binding commits for other evaluators are deliberately not
                # serialized with their resource realization or handshake.
                # They may win this exact compare-and-swap window; retain the
                # physical-start observation and retry only confirmation.
                _validate_started_observation(
                    recovered, recovered_attempt, observation
                )
                last_conflict = error
                current = recovered
                canonical = recovered_attempt
                continue
            recovered = self._refresh()
            recovered_attempt = _require_attempt(recovered, attempt_id)
            if recovered_attempt.state != "running":
                raise RealmIntegrityError(
                    "Launch confirmation did not produce running state."
                )
            _validate_started_observation(recovered, recovered_attempt, observation)
            return recovered
        assert last_conflict is not None
        last_conflict.add_note(
            "Attempt launch confirmation could not acquire a stable run head "
            "after repeated concurrent binding commits."
        )
        raise last_conflict

    def _adopt_finalization(
        self,
        *,
        snapshot: RunLedgerSnapshot,
        attempt_id: str,
        finalization: AttemptFinalization,
        physically_started: bool,
        action: RunAttemptAdvanceAction = "adopted",
    ) -> RunAttemptAdvanceResult:
        current = snapshot
        last_conflict: RealmConflict | None = None
        for _retry in range(_CANONICAL_HEAD_RETRY_LIMIT):
            attempt = _require_attempt(current, attempt_id)
            if attempt.state == "terminal":
                if not _has_exact_finalization(
                    current, attempt, finalization.digest
                ):
                    raise RealmConflict(
                        "Terminal attempt contains a different finalization."
                    )
                break
            _validate_finalization(finalization, attempt)
            revision = current.revision.revision
            owner_revision = current.revision.owner_revision
            operation_id = _operation_id(
                run_id=attempt.run_id,
                attempt_id=attempt.attempt_id,
                action="adopt",
                run_revision=revision,
                owner_revision=owner_revision,
            )
            try:
                self.authority.ledger.adopt_run_attempt(
                    operation_id=operation_id,
                    actor_principal_id=self.authority.actor_principal_id,
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    change_id=attempt.capture_change_id,
                    finalization=finalization,
                    expected_run_revision=revision,
                    expected_owner_revision=owner_revision,
                    controller_lease_id=self.authority.controller_lease_id,
                    controller_holder_id=self.authority.controller_holder_id,
                    controller_fencing_token=(
                        self.authority.controller_fencing_token
                    ),
                )
            except Exception as error:
                recovered = self._refresh_after_error(error, "attempt adoption")
                canonical = _require_attempt(recovered, attempt_id)
                if _has_exact_finalization(
                    recovered, canonical, finalization.digest
                ):
                    current = recovered
                    break
                if (
                    not isinstance(error, RealmConflict)
                    or canonical.state == "terminal"
                    or not _run_head_advanced(current, recovered)
                ):
                    raise
                # The captured finalization remains immutable.  If a provider
                # binding for another evaluator advanced only the run head,
                # retry adoption instead of repeating collection/capture.
                _validate_finalization(finalization, canonical)
                last_conflict = error
                current = recovered
                continue
            current = self._refresh()
            canonical = _require_attempt(current, attempt_id)
            if not _has_exact_finalization(
                current, canonical, finalization.digest
            ):
                raise RealmIntegrityError(
                    "Adoption response differs from the canonical finalization."
                )
            break
        else:
            assert last_conflict is not None
            last_conflict.add_note(
                "Attempt adoption could not acquire a stable run/owner head "
                "after repeated concurrent canonical commits."
            )
            raise last_conflict
        snapshot = current
        attempt = _require_attempt(snapshot, attempt_id)
        return self._terminal_result(
            snapshot=snapshot,
            attempt=attempt,
            action=action,
            physically_started=physically_started,
        )

    def _terminal_result(
        self,
        *,
        snapshot: RunLedgerSnapshot,
        attempt: RunAttemptRecord,
        action: RunAttemptAdvanceAction,
        physically_started: bool,
    ) -> RunAttemptAdvanceResult:
        if attempt.state != "terminal":
            raise RealmIntegrityError("Advance result requires terminal state.")
        logical_transition = _logical_resolution(snapshot, attempt)
        cleanup = self.provider.resume_cleanup(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
        )
        if not isinstance(cleanup, LocalAttemptCleanupResult):
            raise TypeError(
                "provider.resume_cleanup() must return LocalAttemptCleanupResult."
            )
        return RunAttemptAdvanceResult(
            attempt=attempt,
            logical_transition=logical_transition,
            action=action,
            cleanup=cleanup,
            physically_started=physically_started,
        )

    def _make_heartbeat(
        self, receipt: RunAttemptHeartbeatAuthorityReceipt
    ) -> _AttemptHeartbeat:
        if self.heartbeat_factory is None:
            heartbeat: _AttemptHeartbeat = RunAttemptHeartbeatCoordinator(
                self.authority.ledger,
                actor_principal_id=self.authority.actor_principal_id,
                receipt=receipt,
                ttl_seconds=self.heartbeat_policy.ttl_seconds,
                interval_seconds=self.heartbeat_policy.interval_seconds,
            )
        else:
            heartbeat = self.heartbeat_factory(receipt)
        _validate_heartbeat(heartbeat)
        return heartbeat

    def _refresh(self) -> RunLedgerSnapshot:
        snapshot = self.authority.refresh_controller()
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError(
                "authority.refresh_controller() must return RunLedgerSnapshot."
            )
        if (
            snapshot.run.run_id != self.authority.run_id
            or snapshot.run.owner_id != self.authority.owner_id
            or snapshot.run.current_revision != self.authority.run_revision
            or snapshot.revision.owner_revision != self.authority.owner_revision
            or snapshot.run.controller_lease_id
            != self.authority.controller_lease_id
            or snapshot.run.controller_holder_id
            != self.authority.controller_holder_id
            or snapshot.run.controller_fencing_token
            != self.authority.controller_fencing_token
        ):
            raise RuntimeError(
                "Refreshed run head differs from the scheduler authority."
            )
        return snapshot

    def _refresh_after_error(
        self, primary: Exception, boundary: str
    ) -> RunLedgerSnapshot:
        try:
            return self._refresh()
        except Exception as refresh_error:
            if hasattr(primary, "add_note"):
                primary.add_note(
                    f"Canonical refresh after {boundary} also failed: "
                    f"{type(refresh_error).__name__}."
                )
            raise primary from refresh_error


def _validate_authority(authority: Any) -> None:
    fields = (
        "ledger",
        "actor_principal_id",
        "run_id",
        "owner_id",
        "run_revision",
        "owner_revision",
        "controller_lease_id",
        "controller_holder_id",
        "controller_fencing_token",
    )
    missing = [name for name in fields if not hasattr(authority, name)]
    if missing:
        raise TypeError(
            "authority is missing required cursor fields: " + ", ".join(missing)
        )
    if not callable(getattr(authority, "refresh_controller", None)):
        raise TypeError("authority must provide refresh_controller().")
    ledger_methods = (
        "prepare_run_attempt",
        "read_run_attempt_heartbeat_authority",
        "confirm_run_attempt_launch",
        "adopt_run_attempt",
        "reconcile_lost_run_attempt",
    )
    missing_methods = [
        name
        for name in ledger_methods
        if not callable(getattr(authority.ledger, name, None))
    ]
    if missing_methods:
        raise TypeError(
            "authority ledger is missing required attempt methods: "
            + ", ".join(missing_methods)
        )


def _validate_provider(provider: Any) -> None:
    methods = (
        "start_or_attach",
        "terminalize_current",
        "wait_terminal",
        "stop_and_wait_terminal",
        "finalize_terminal",
        "resume_cleanup",
    )
    missing = [name for name in methods if not callable(getattr(provider, name, None))]
    if missing:
        raise TypeError(
            "provider is missing required lifecycle methods: " + ", ".join(missing)
        )


def _validate_heartbeat(heartbeat: Any) -> None:
    methods = ("start", "heartbeat_once", "raise_if_failed", "stop")
    missing = [name for name in methods if not callable(getattr(heartbeat, name, None))]
    if missing:
        raise TypeError(
            "heartbeat is missing required lifecycle methods: " + ", ".join(missing)
        )


def _find_attempt(
    snapshot: RunLedgerSnapshot, attempt_id: str
) -> RunAttemptRecord | None:
    return next(
        (item for item in snapshot.attempts if item.attempt_id == attempt_id),
        None,
    )


def _run_head_advanced(
    before: RunLedgerSnapshot, after: RunLedgerSnapshot
) -> bool:
    """Return whether an exact-controller refresh observed forward CAS drift."""

    if before.run.run_id != after.run.run_id:
        raise RealmIntegrityError("Canonical retry crossed run identities.")
    return (
        after.revision.revision > before.revision.revision
        or after.revision.owner_revision > before.revision.owner_revision
    )


def _require_attempt(
    snapshot: RunLedgerSnapshot, attempt_id: str
) -> RunAttemptRecord:
    attempt = _find_attempt(snapshot, attempt_id)
    if attempt is None:
        raise RealmIntegrityError("Canonical attempt disappeared during advance.")
    return attempt


def _logical_resolution(
    snapshot: RunLedgerSnapshot, attempt: RunAttemptRecord
) -> LogicalTrialTransitionRecord:
    attempt_transition = next(
        (
            item
            for item in snapshot.attempt_transitions
            if item.attempt_id == attempt.attempt_id
            and item.transition_index == attempt.head_transition_index
        ),
        None,
    )
    if attempt_transition is None or attempt_transition.to_state != "terminal":
        raise RealmIntegrityError("Terminal attempt transition history is missing.")
    matches = tuple(
        item
        for item in snapshot.logical_transitions
        if item.logical_trial_id == attempt.logical_trial_id
        and item.attempt_id == attempt.attempt_id
        and item.run_revision == attempt_transition.run_revision
        and item.sequence == attempt_transition.sequence + 1
    )
    if len(matches) != 1:
        raise RealmIntegrityError(
            "Exact logical transition for the terminal attempt is missing."
        )
    return matches[0]


def _execution_records(
    snapshot: RunLedgerSnapshot,
    attempt_id: str,
) -> tuple[
    ExecutionBindingRecord | None,
    ExecutionLaunchIntentRecord | None,
    ExecutionTerminalEvidenceRecord | None,
]:
    binding = next(
        (item for item in snapshot.execution_bindings if item.attempt_id == attempt_id),
        None,
    )
    launch = next(
        (
            item
            for item in snapshot.execution_launch_intents
            if item.attempt_id == attempt_id
        ),
        None,
    )
    evidence = next(
        (
            item
            for item in snapshot.execution_terminal_evidence
            if item.attempt_id == attempt_id
        ),
        None,
    )
    return binding, launch, evidence


def _validate_execution_shape(
    *,
    attempt: RunAttemptRecord,
    binding: ExecutionBindingRecord | None,
    launch: ExecutionLaunchIntentRecord | None,
    evidence: ExecutionTerminalEvidenceRecord | None,
) -> None:
    if binding is None:
        if launch is not None or evidence is not None:
            raise RealmIntegrityError(
                "Launch or terminal evidence exists without an execution binding."
            )
        if attempt.state == "running":
            raise RealmIntegrityError("Running attempt has no execution binding.")
        return
    try:
        binding.validate_attempt(attempt)
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError(
            "Execution binding differs from the canonical attempt."
        ) from error
    if launch is None:
        raise RealmIntegrityError("Execution binding has no launch intent.")
    try:
        launch.validate_binding(binding, attempt)
        if evidence is not None:
            evidence.validate_launch_intent(binding, attempt, launch)
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError(
            "Durable local execution facts do not agree."
        ) from error


def _validate_started_observation(
    snapshot: RunLedgerSnapshot,
    attempt: RunAttemptRecord,
    observation: LocalAttemptStarted | LocalAttemptTerminal,
) -> None:
    binding, launch, evidence = _execution_records(snapshot, attempt.attempt_id)
    _validate_execution_shape(
        attempt=attempt,
        binding=binding,
        launch=launch,
        evidence=evidence,
    )
    if binding is None or launch is None:
        raise RealmIntegrityError("Started observation has no durable launch.")
    if isinstance(observation, LocalAttemptStarted):
        if (
            observation.run_id != attempt.run_id
            or observation.attempt_id != attempt.attempt_id
            or observation.binding_id != binding.binding_id
            or observation.launch_token != attempt.launch_token
            or observation.evidence_fingerprint != binding.evidence_fingerprint
            or observation.launch_request_digest != launch.launch_request_digest
        ):
            raise RealmIntegrityError(
                "Provider start observation differs from the durable launch."
            )
        return
    if not observation.started:
        raise RealmIntegrityError("Started validation received never-started evidence.")
    if evidence != observation.evidence:
        raise RealmIntegrityError(
            "Provider terminal observation differs from retained evidence."
        )


def _validate_terminal_observation(
    snapshot: RunLedgerSnapshot,
    attempt: RunAttemptRecord,
    terminal: LocalAttemptTerminal,
) -> None:
    if not isinstance(terminal, LocalAttemptTerminal):
        raise TypeError("terminal must be a LocalAttemptTerminal.")
    binding, launch, evidence = _execution_records(snapshot, attempt.attempt_id)
    _validate_execution_shape(
        attempt=attempt,
        binding=binding,
        launch=launch,
        evidence=evidence,
    )
    if evidence != terminal.evidence:
        raise RealmIntegrityError(
            "Provider terminal observation differs from retained evidence."
        )
    if terminal.started and attempt.state != "running":
        raise RealmIntegrityError(
            "Started terminal evidence requires a running canonical attempt."
        )
    if not terminal.started and attempt.state != "prepared":
        raise RealmIntegrityError(
            "Never-started terminal evidence requires a prepared attempt."
        )


def _physical_start_from_snapshot(
    snapshot: RunLedgerSnapshot, attempt_id: str
) -> bool:
    _, _, evidence = _execution_records(snapshot, attempt_id)
    return evidence.started if evidence is not None else False


def _validate_finalization(
    finalization: Any, attempt: RunAttemptRecord
) -> None:
    if not isinstance(finalization, AttemptFinalization):
        raise TypeError(
            "provider.finalize_terminal() must return AttemptFinalization."
        )
    if (
        finalization.attempt_id != attempt.attempt_id
        or finalization.evaluation_spec_digest != attempt.evaluation_spec_digest
        or finalization.binding_id != attempt.binding_id
    ):
        raise RealmIntegrityError(
            "Provider finalization differs from the canonical attempt."
        )


def _has_exact_finalization(
    snapshot: RunLedgerSnapshot,
    attempt: RunAttemptRecord,
    finalization_digest: str,
) -> bool:
    if attempt.state != "terminal":
        return False
    transition = next(
        (
            item
            for item in snapshot.attempt_transitions
            if item.attempt_id == attempt.attempt_id
            and item.transition_index == attempt.head_transition_index
        ),
        None,
    )
    return bool(
        transition is not None
        and transition.to_state == "terminal"
        and transition.payload.get("finalization_digest") == finalization_digest
    )


def _stop_provider_without_masking(
    *,
    provider: LocalProcessAttemptProvider,
    attempt: RunAttemptRecord,
    primary: BaseException,
) -> None:
    try:
        provider.stop_and_wait_terminal(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
        )
    except Exception as stop_error:
        if hasattr(primary, "add_note"):
            primary.add_note(
                "Provider stop/drain after failed attempt advance also failed: "
                f"{type(stop_error).__name__}."
            )


def _stop_heartbeat_without_masking(
    heartbeat: _AttemptHeartbeat, primary: BaseException
) -> None:
    try:
        heartbeat.stop()
    except Exception as stop_error:
        if hasattr(primary, "add_note"):
            primary.add_note(
                "Heartbeat stop after failed attempt advance also failed: "
                f"{type(stop_error).__name__}."
            )


def _operation_id(
    *,
    run_id: str,
    attempt_id: str,
    action: str,
    run_revision: int,
    owner_revision: int,
) -> str:
    material = (
        f"v1\x00{run_id}\x00{attempt_id}\x00{action}\x00"
        f"{run_revision}\x00{owner_revision}"
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"run-attempt-scheduler/{action}/{digest[:40]}"


def _required_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be nonempty trimmed text.")
    return value


def _positive_finite(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{label} must be a positive finite number.")
    return float(value)


__all__ = [
    "HeartbeatFactory",
    "RunAttemptAdvanceAction",
    "RunAttemptAdvanceResult",
    "RunAttemptHeartbeatPolicy",
    "RunAttemptScheduler",
]
