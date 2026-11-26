"""Realm-native production orchestration for retained parameter batch runs.

This module is the single writer-side composition over the existing authorities:

* :class:`RetainedRunAuthority` owns normalized candidate admission and its
  disposable :class:`RunController` cache;
* :class:`RunAttemptScheduler` owns attempt realization/recovery;
* the Realm method-exchange ledger owns exact callback inputs and canonical
  effects; and
* :class:`RetainedPythonBatchRuntime` owns one replayable retained worker.

The worker is an at-least-once callback executor, never canonical state.  A
callback response is committed to Realm before it is acknowledged to the
worker.  Recovery validates the worker's constant-size cumulative watermark
against the dense completed ledger prefix, replays any missing completed
callbacks from their exact retained inputs, and fails closed if a full response
digest changes.  No workspace or provider path crosses this boundary.

Evaluator work is bounded by both the retained definition's
``evaluator_capacity`` and a local host-dispatch implementation limit.  The
attempt scheduler serializes short canonical phases while evaluator waits
overlap; method observations remain one deterministic ordered batch after every
admitted logical trial is terminal.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from .method_exchange_projection import (
    build_method_observation_exchange_input,
    build_method_proposal_exchange_input,
    observation_worker_payload,
    proposal_worker_payload,
)
from .method_launch_environment import (
    MethodLaunchEnvironment,
    MethodLaunchEnvironmentDescriptor,
)
from .realm.local_runtime import LocalRealmRuntime
from .realm.errors import ContentRejected, SourceChanged
from .realm.method_exchange_records import (
    RunMethodExchangeCompletionRecord,
    RunMethodExchangePreparationRecord,
    method_exchange_sequence,
)
from .realm.refs import canonical_json_bytes
from .realm.run_workbench import reduce_run_diagnostic
from .realm.run_projection import RunSummaryProjection
from .realm.run_records import RunCreateReceipt
from .realm.run_snapshot import RunLedgerSnapshot
from .retained_batch_runtime import (
    RetainedBatchCacheAck,
    RetainedBatchExchangeCoordinate,
    RetainedBatchMethodError,
    RetainedBatchProtocolError,
    RetainedBatchRuntimeError,
    RetainedBatchRuntimeProvider,
    RetainedBatchWorkerResponse,
    RetainedBatchWorkerStatus,
    retained_batch_worker_request_digest,
)
from .retained_batch_worker import (
    BATCH_RESPONSE_SCHEMA,
    INITIAL_BATCH_EXCHANGE_CHAIN,
    retained_batch_exchange_chain_digest,
)
from .run_attempt_scheduler import RunAttemptScheduler
from .run_authority import RetainedRunAuthority
from .run_controller import CandidateNormalizer, MethodProtocolError
from .run_controller_heartbeat import RunControllerHeartbeatCoordinator
from .runtime_limits import MAX_LOCAL_EVALUATOR_DISPATCH_THREADS
from .run_terminal_policy import (
    CANCELLATION_STOP_CODES,
    METHOD_EXCHANGE_ABANDON_STOP_CODES,
    method_feedback_obligations_resolved,
)


_PROTOCOL_METHOD_CODES = frozenset(
    {
        "batch_overproduced",
        "invalid_observation",
        "invalid_proposal",
        "response_too_large",
    }
)
_ERROR_MESSAGES = {
    "canonical_state_invalid": "Canonical retained-batch run state is invalid.",
    "definitive_cleanup_failed": "A failed retained-batch worker could not be retired safely.",
    "exchange_rejected": "The retained-batch worker rejected the canonical exchange order.",
    "replay_diverged": "The retained-batch worker response changed during recovery.",
    "worker_state_diverged": "The retained-batch worker state differs from Realm.",
}


class RealmRetainedBatchRunError(RuntimeError):
    """Bounded path-free failure surfaced by the production driver."""

    def __init__(
        self,
        code: str,
        *,
        canonical_failure_code: str | None = None,
    ) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("retained-batch run error code is unsupported.")
        evidence_code = canonical_failure_code or code
        if evidence_code not in _ERROR_MESSAGES:
            raise ValueError("canonical failure code is unsupported.")
        self.code = code
        # Cleanup is a secondary operational failure.  When cleanup itself
        # fails, keep the already-proven canonical divergence available as
        # bounded evidence without exposing provider exception text or paths.
        self.canonical_failure_code = evidence_code
        super().__init__(_ERROR_MESSAGES[code])


class _RuntimeGraph(Protocol):
    actor_principal_id: str
    ledger: Any
    attempt_provider: Any
    content_service: Any
    content_store: Any


class _MethodRuntimeProvider(Protocol):
    def realize(self, snapshot: RunLedgerSnapshot, **kwargs: Any) -> Any: ...

    def reconcile_inactive(
        self, snapshot: RunLedgerSnapshot, **kwargs: Any
    ) -> Any: ...


class _AttemptScheduler(Protocol):
    authority: RetainedRunAuthority

    def advance(
        self,
        *,
        logical_trial_id: str,
        attempt_id: str,
        attempt_ttl_seconds: float,
    ) -> Any: ...

    def terminalize(
        self, *, logical_trial_id: str, attempt_id: str
    ) -> Any: ...


class _Heartbeat(Protocol):
    def start(self) -> Any: ...

    def stop(self) -> None: ...

    def raise_if_failed(self) -> None: ...


HeartbeatFactory = Callable[[RunLedgerSnapshot, Any], _Heartbeat]


@dataclass(frozen=True)
class _InactiveMethodDrive:
    snapshot: RunLedgerSnapshot


@dataclass(frozen=True)
class DigestRealmAttemptIdentitySource:
    """Deterministic attempt identities under one logical budget slot."""

    namespace: str = "optpilot.realm-retained-batch-driver.v1"

    def __post_init__(self) -> None:
        _required_text(self.namespace, "attempt identity namespace")

    def attempt(
        self,
        *,
        run_id: str,
        logical_trial_id: str,
        attempt_index: int,
    ) -> str:
        _required_text(run_id, "run_id")
        _required_text(logical_trial_id, "logical_trial_id")
        _positive_int(attempt_index, "attempt_index")
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "attempt_index": attempt_index,
                    "logical_trial_id": logical_trial_id,
                    "namespace": self.namespace,
                    "run_id": run_id,
                }
            )
        ).hexdigest()
        return f"attempt-{digest[:24]}"


@dataclass(frozen=True)
class RunControllerTakeoverExpectation:
    """Exact prior controller term retained across takeover response loss."""

    run_id: str
    controller_generation: int
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _positive_int(self.controller_generation, "controller_generation")
        _required_text(self.controller_lease_id, "controller_lease_id")
        _required_text(self.controller_holder_id, "controller_holder_id")
        _positive_int(self.controller_fencing_token, "controller_fencing_token")

    @classmethod
    def from_snapshot(
        cls, snapshot: RunLedgerSnapshot
    ) -> "RunControllerTakeoverExpectation":
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        if snapshot.run.state != "running":
            raise ValueError("Only a running run requires controller takeover.")
        return cls(
            run_id=snapshot.run.run_id,
            controller_generation=snapshot.run.controller_generation,
            controller_lease_id=snapshot.run.controller_lease_id,
            controller_holder_id=snapshot.run.controller_holder_id,
            controller_fencing_token=snapshot.run.controller_fencing_token,
        )


def _recorded_method_cause(
    cause: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Reduce a worker's private cause into what a Run may durably record.

    The reduction happens here, before the ledger write, so the durable
    exchange stream keeps its no-tracebacks, no-host-paths promise
    structurally rather than by the reader remembering to redact.
    """

    if not isinstance(cause, Mapping):
        return None
    error_type, summary, truncated = reduce_run_diagnostic(cause)
    if error_type is None and summary is None:
        return None
    return {"type": error_type, "message": summary, "truncated": truncated}


@dataclass(frozen=True)
class _Invocation:
    response_digest: str
    response: Mapping[str, Any] | None = None
    failure_outcome: str | None = None
    error_code: str | None = None
    error_json: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _lower_digest(self.response_digest, "method response digest")
        if self.failure_outcome is None:
            if (
                self.response is None
                or self.error_code is not None
                or self.error_json is not None
            ):
                raise ValueError("Successful invocation requires only a response.")
        elif self.failure_outcome not in {"method_failed", "protocol_error"}:
            raise ValueError("Invocation failure outcome is unsupported.")
        elif self.response is not None or self.error_code is None:
            raise ValueError("Failed invocation requires an error code.")


@dataclass(frozen=True)
class _CanonicalExchange:
    preparation: RunMethodExchangePreparationRecord
    completion: RunMethodExchangeCompletionRecord | None
    operation: str
    payload: Mapping[str, Any]
    sequence: int
    request_digest: str

    @property
    def coordinate(self) -> RetainedBatchExchangeCoordinate:
        if self.completion is None:
            raise ValueError("Incomplete exchange has no acknowledged coordinate.")
        return RetainedBatchExchangeCoordinate(
            exchange_id=self.preparation.exchange_id,
            exchange_sequence=self.sequence,
            request_digest=self.request_digest,
            response_digest=self.completion.response_digest,
        )


class RealmRetainedBatchRunDriver:
    """Drive one parameter batch run from canonical Realm state to terminal.

    A new run should use :meth:`from_create_receipt`.  Recovery must use
    :meth:`take_over`, which appends a fenced controller replacement before
    hydration; this class intentionally exposes no convenience that silently
    adopts another controller's current term.

    Attempt execution uses the retained definition's evaluator capacity.  The
    scheduler keeps canonical run/owner cursor phases serialized while this
    driver overlaps evaluator waits in a bounded worker pool.  Method exchange
    preparation and delivery remain on this single driver thread.
    """

    def __init__(
        self,
        runtime: LocalRealmRuntime | _RuntimeGraph,
        authority: RetainedRunAuthority,
        *,
        method_environment: (
            MethodLaunchEnvironment
            | MethodLaunchEnvironmentDescriptor
            | None
        ) = None,
        method_runtime_provider: _MethodRuntimeProvider | None = None,
        scheduler: _AttemptScheduler | None = None,
        heartbeat_factory: HeartbeatFactory | None = None,
        identity_source: DigestRealmAttemptIdentitySource | None = None,
        controller_ttl_seconds: float = 300.0,
        heartbeat_interval_seconds: float | None = None,
        attempt_ttl_seconds: float = 300.0,
        method_start_timeout: float = 10.0,
        method_request_timeout: float = 10.0,
    ) -> None:
        if not isinstance(authority, RetainedRunAuthority):
            raise TypeError("authority must be a RetainedRunAuthority.")
        for name in ("ledger", "actor_principal_id", "attempt_provider"):
            if not hasattr(runtime, name):
                raise TypeError("runtime does not provide the local Realm service graph.")
        if authority.candidate_contract.get("format") == "files" and any(
            not hasattr(runtime, name) for name in ("content_service", "content_store")
        ):
            raise TypeError(
                "file-candidate runs require the local Realm content service graph."
            )
        if runtime.ledger is not authority.ledger:
            raise ValueError("runtime and authority must share the exact Realm ledger.")
        if runtime.actor_principal_id != authority.actor_principal_id:
            raise ValueError("runtime and authority must share the actor principal.")

        selected_scheduler = scheduler or RunAttemptScheduler(
            authority, runtime.attempt_provider
        )
        if getattr(selected_scheduler, "authority", None) is not authority:
            raise ValueError("scheduler must use the driver's exact authority.")
        if not callable(getattr(selected_scheduler, "advance", None)):
            raise TypeError("scheduler must provide advance().")
        if method_environment is not None and not isinstance(
            method_environment,
            (MethodLaunchEnvironment, MethodLaunchEnvironmentDescriptor),
        ):
            raise TypeError(
                "method_environment must be a MethodLaunchEnvironment, "
                "MethodLaunchEnvironmentDescriptor, or None."
            )
        if method_runtime_provider is not None and method_environment is not None:
            raise ValueError(
                "method_environment cannot accompany a custom method runtime provider."
            )
        provider = method_runtime_provider or RetainedBatchRuntimeProvider(
            runtime, method_environment=method_environment
        )
        if not callable(getattr(provider, "realize", None)):
            raise TypeError("method_runtime_provider must provide realize().")
        if heartbeat_factory is not None and not callable(heartbeat_factory):
            raise TypeError("heartbeat_factory must be callable or None.")
        identities = identity_source or DigestRealmAttemptIdentitySource()
        if not callable(getattr(identities, "attempt", None)):
            raise TypeError("identity_source must provide attempt().")

        self.runtime = runtime
        self.authority = authority
        self.method_runtime_provider = provider
        self.scheduler = selected_scheduler
        self.heartbeat_factory = heartbeat_factory
        self.identity_source = identities
        self.controller_ttl_seconds = _positive_finite(
            controller_ttl_seconds, "controller_ttl_seconds"
        )
        self.heartbeat_interval_seconds = (
            None
            if heartbeat_interval_seconds is None
            else _positive_finite(
                heartbeat_interval_seconds, "heartbeat_interval_seconds"
            )
        )
        self.attempt_ttl_seconds = _positive_finite(
            attempt_ttl_seconds, "attempt_ttl_seconds"
        )
        self.method_start_timeout = _positive_finite(
            method_start_timeout, "method_start_timeout"
        )
        self.method_request_timeout = _positive_finite(
            method_request_timeout, "method_request_timeout"
        )
        self._acknowledged_sequence = 0
        self._acknowledged_chain = INITIAL_BATCH_EXCHANGE_CHAIN
        self._cleaned_attempt_ids: set[str] = set()

    @classmethod
    def from_create_receipt(
        cls,
        runtime: LocalRealmRuntime,
        *,
        receipt: RunCreateReceipt,
        candidate_normalizer: CandidateNormalizer,
        normalizer_version: str,
        **kwargs: Any,
    ) -> "RealmRetainedBatchRunDriver":
        """Bind the controller term atomically returned by fresh run creation."""

        authority = RetainedRunAuthority.from_create_receipt(
            ledger=runtime.ledger,
            actor_principal_id=runtime.actor_principal_id,
            receipt=receipt,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
        )
        return cls(runtime, authority, **kwargs)

    @classmethod
    def take_over(
        cls,
        runtime: LocalRealmRuntime,
        *,
        expected_controller: RunControllerTakeoverExpectation,
        takeover_operation_id: str,
        new_controller_holder_id: str,
        candidate_normalizer: CandidateNormalizer,
        normalizer_version: str,
        controller_ttl_seconds: float = 300.0,
        require_previous_controller_expired: bool = False,
        **kwargs: Any,
    ) -> "RealmRetainedBatchRunDriver":
        """Append an explicit fenced controller replacement, then hydrate."""

        if not isinstance(expected_controller, RunControllerTakeoverExpectation):
            raise TypeError(
                "expected_controller must be a RunControllerTakeoverExpectation."
            )
        _required_text(takeover_operation_id, "takeover_operation_id")
        _required_text(new_controller_holder_id, "new_controller_holder_id")
        ttl = _positive_finite(controller_ttl_seconds, "controller_ttl_seconds")
        replacement = runtime.ledger.replace_run_controller(
            operation_id=takeover_operation_id,
            actor_principal_id=runtime.actor_principal_id,
            run_id=expected_controller.run_id,
            expected_controller_generation=(
                expected_controller.controller_generation
            ),
            expected_controller_lease_id=(
                expected_controller.controller_lease_id
            ),
            expected_controller_holder_id=(
                expected_controller.controller_holder_id
            ),
            expected_controller_fencing_token=(
                expected_controller.controller_fencing_token
            ),
            new_controller_holder_id=new_controller_holder_id,
            controller_ttl_seconds=ttl,
            require_previous_controller_expired=require_previous_controller_expired,
        )
        if (
            replacement.run.run_id != expected_controller.run_id
            or replacement.run.controller_generation
            != expected_controller.controller_generation + 1
            or replacement.run.controller_holder_id != new_controller_holder_id
        ):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        authority = RetainedRunAuthority.hydrate(
            ledger=runtime.ledger,
            actor_principal_id=runtime.actor_principal_id,
            run_id=expected_controller.run_id,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
        )
        hydrated = authority.refresh_controller()
        if (
            hydrated.run.controller_generation
            != replacement.run.controller_generation
            or authority.controller_lease_id
            != replacement.controller_lease.lease_id
            or authority.controller_holder_id
            != replacement.controller_lease.holder_id
            or authority.controller_fencing_token
            != replacement.controller_lease.fencing_token
        ):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        return cls(
            runtime,
            authority,
            controller_ttl_seconds=ttl,
            **kwargs,
        )

    def run(self) -> RunSummaryProjection:
        """Drive until canonical terminal state and return a path-free summary."""

        snapshot = self.authority.refresh_controller()
        snapshot = self._apply_durable_cancellation(snapshot)
        if snapshot.run.state != "running":
            self._reconcile_inactive_method(snapshot)
            return RunSummaryProjection.from_snapshot(snapshot)
        if method_feedback_obligations_resolved(snapshot):
            return self._drive_without_method(snapshot)

        method = self.method_runtime_provider.realize(
            snapshot,
            ttl_seconds=self.controller_ttl_seconds,
            start_timeout=self.method_start_timeout,
            request_timeout=self.method_request_timeout,
        )
        heartbeat = self._make_heartbeat(snapshot, method)
        completed = False
        inactive: _InactiveMethodDrive | None = None
        definitive_failure: RealmRetainedBatchRunError | None = None
        try:
            heartbeat.start()
            heartbeat.raise_if_failed()
            self._synchronize_worker(method, heartbeat)
            outcome = self._drive(method, heartbeat)
            if isinstance(outcome, _InactiveMethodDrive):
                inactive = outcome
            else:
                summary = outcome
                completed = True
        except RealmRetainedBatchRunError as error:
            # A proven chain/digest/canonical-state divergence cannot be
            # repaired by attaching to this same worker.  Retire it now; a
            # later explicit controller takeover may start a clean generation.
            # Transport loss and callback interruption use other exception
            # types and intentionally leave the exact pending worker state for
            # same-term attachment or takeover reconciliation.
            definitive_failure = error
        finally:
            heartbeat.stop()
        if definitive_failure is not None:
            try:
                method.force_stop()
            except Exception:
                raise RealmRetainedBatchRunError(
                    "definitive_cleanup_failed",
                    canonical_failure_code=(
                        definitive_failure.canonical_failure_code
                    ),
                ) from None
            try:
                self._close_after_definitive_method_failure()
            except Exception:
                raise RealmRetainedBatchRunError(
                    "definitive_cleanup_failed",
                    canonical_failure_code=(
                        definitive_failure.canonical_failure_code
                    ),
                ) from None
            raise definitive_failure
        if inactive is not None:
            submission = inactive.snapshot.control.current_submission
            if submission.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES:
                method.force_stop()
            else:
                method.shutdown()
            return self._drive_without_method(inactive.snapshot)
        if completed:
            method.shutdown()
        return summary

    def _reconcile_inactive_method(self, snapshot: RunLedgerSnapshot) -> None:
        reconcile = getattr(
            self.method_runtime_provider, "reconcile_inactive", None
        )
        if not callable(reconcile):
            raise TypeError(
                "method_runtime_provider must provide reconcile_inactive()."
            )
        receipt = reconcile(
            snapshot,
            grace_period=1.0,
            timeout=self.method_request_timeout,
        )
        if (
            getattr(receipt, "run_id", None) != snapshot.run.run_id
            or getattr(receipt, "controller_generation", None)
            != snapshot.run.controller_generation
            or getattr(receipt, "run_definition_digest", None)
            != snapshot.definition.digest
            or getattr(receipt, "worker_disposition", None)
            not in {
                "absent",
                "already_retired",
                "already_terminal",
                "never_started",
                "stopped",
                "quarantined",
            }
            or getattr(receipt, "resources_reconciled", None) is not True
        ):
            raise RealmRetainedBatchRunError("canonical_state_invalid")

    def _close_after_definitive_method_failure(self) -> None:
        """Hard-close a retired divergent method and drain without a worker."""

        snapshot = self.authority.refresh_controller()
        if snapshot.run.state != "running":
            self._reconcile_inactive_method(snapshot)
            return
        submission = snapshot.control.current_submission
        if submission.state == "accepting":
            self.authority.close_submissions(
                operation_id=(
                    f"run/{self.authority.run_id}/method/definitive/protocol-error"
                ),
                stop_code="protocol_error",
            )
            snapshot = self.authority.refresh_controller()
        elif (
            submission.state == "draining"
            and submission.stop_code
            not in METHOD_EXCHANGE_ABANDON_STOP_CODES
        ):
            self.authority.escalate_stop(
                operation_id=(
                    f"run/{self.authority.run_id}/method/definitive/"
                    "protocol-error-escalation"
                ),
                stop_code="protocol_error",
            )
            snapshot = self.authority.refresh_controller()
        if not method_feedback_obligations_resolved(snapshot):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        self._drive_without_method(snapshot)

    def _apply_durable_cancellation(
        self, snapshot: RunLedgerSnapshot
    ) -> RunLedgerSnapshot:
        """Apply Core's durable cancellation request through run authority.

        The request record is an intent fence, not a second run controller.
        This driver remains the only holder allowed to mutate the run: at a
        scheduling boundary it closes an accepting run, or upgrades an
        existing soft drain to the requested cancellation.  The request
        digest makes both canonical operations stable across driver recovery.
        """

        if snapshot.run.state != "running":
            return snapshot
        request = self.runtime.ledger.read_run_cancellation_request(
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
        )
        if request is None:
            return snapshot
        if request.reason_code not in CANCELLATION_STOP_CODES:
            raise RealmRetainedBatchRunError("canonical_state_invalid")

        submission = snapshot.control.current_submission
        operation_prefix = (
            f"run/{self.authority.run_id}/cancellation/"
            f"{request.request_digest}"
        )
        if submission.state == "accepting":
            self.authority.close_submissions(
                operation_id=f"{operation_prefix}/close",
                stop_code=request.reason_code,
            )
            return self.authority.refresh_controller()
        if (
            submission.state == "draining"
            and submission.stop_code not in CANCELLATION_STOP_CODES
        ):
            self.authority.escalate_stop(
                operation_id=f"{operation_prefix}/escalate",
                stop_code=request.reason_code,
            )
            return self.authority.refresh_controller()
        return snapshot

    def _make_heartbeat(self, snapshot: RunLedgerSnapshot, method: Any) -> _Heartbeat:
        if self.heartbeat_factory is not None:
            heartbeat = self.heartbeat_factory(snapshot, method)
        else:
            heartbeat = RunControllerHeartbeatCoordinator(
                self.runtime.ledger,
                actor_principal_id=self.runtime.actor_principal_id,
                run_id=self.authority.run_id,
                controller_lease=snapshot.controller_lease,
                ttl_seconds=self.controller_ttl_seconds,
                interval_seconds=self.heartbeat_interval_seconds,
                targets={"method": method},
            )
        for name in ("start", "stop", "raise_if_failed"):
            if not callable(getattr(heartbeat, name, None)):
                raise TypeError("heartbeat_factory returned an invalid coordinator.")
        return heartbeat

    def _drive(
        self, method: Any, heartbeat: _Heartbeat
    ) -> RunSummaryProjection | _InactiveMethodDrive:
        while True:
            heartbeat.raise_if_failed()
            snapshot = self.authority.refresh_controller()
            snapshot = self._apply_durable_cancellation(snapshot)
            if snapshot.run.state != "running":
                return RunSummaryProjection.from_snapshot(snapshot)
            if (
                method_feedback_obligations_resolved(snapshot)
                and snapshot.control.current_submission.stop_code
                in METHOD_EXCHANGE_ABANDON_STOP_CODES
            ):
                return _InactiveMethodDrive(snapshot)

            pending = self._pending_exchange(snapshot)
            if pending is not None and pending.kind == "proposal":
                inactive = self._process_preparation(
                    snapshot, pending, method, heartbeat
                )
                if inactive is not None:
                    return inactive
                continue

            self._sweep_terminal_attempt_cleanup(snapshot, heartbeat)
            snapshot = self.authority.refresh_controller()
            attempts = self._next_attempts(snapshot)
            if attempts:
                self._advance_attempts(
                    attempts,
                    evaluator_capacity=snapshot.definition.evaluator_capacity,
                )
                heartbeat.raise_if_failed()
                continue

            pending = self._pending_exchange(snapshot)
            if pending is not None:
                if pending.kind != "observation":
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                inactive = self._process_preparation(
                    snapshot, pending, method, heartbeat
                )
                if inactive is not None:
                    return inactive
                continue

            round_index = self._round_awaiting_observation(snapshot)
            if round_index is not None:
                exchange_input = build_method_observation_exchange_input(
                    snapshot, round_index=round_index
                )
                self._prepare_exchange(
                    snapshot,
                    round_index=round_index,
                    exchange_input=exchange_input,
                )
                continue

            submission = snapshot.control.current_submission
            if submission.state == "draining":
                if any(trial.state != "terminal" for trial in snapshot.logical_trials):
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                self.authority.finish(
                    operation_id=f"run/{self.authority.run_id}/finish"
                )
                continue
            if submission.state != "accepting":
                raise RealmRetainedBatchRunError("canonical_state_invalid")

            width = self.authority.controller.next_proposal_width
            if width <= 0:
                raise RealmRetainedBatchRunError("canonical_state_invalid")
            round_index = 1 + sum(
                item.kind == "proposal"
                for item in snapshot.method_exchange_preparations
            )
            exchange_input = build_method_proposal_exchange_input(
                snapshot, requested_width=width
            )
            self._prepare_exchange(
                snapshot,
                round_index=round_index,
                exchange_input=exchange_input,
            )

    def _drive_without_method(
        self, snapshot: RunLedgerSnapshot
    ) -> RunSummaryProjection:
        """Reconcile an inactive method, execute seeded work, and finish."""

        if snapshot.run.state == "running" and not (
            method_feedback_obligations_resolved(snapshot)
        ):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        self._reconcile_inactive_method(snapshot)

        while True:
            snapshot = self.authority.refresh_controller()
            snapshot = self._apply_durable_cancellation(snapshot)
            if snapshot.run.state != "running":
                return RunSummaryProjection.from_snapshot(snapshot)
            if not method_feedback_obligations_resolved(snapshot):
                raise RealmRetainedBatchRunError("canonical_state_invalid")

            submission = snapshot.control.current_submission
            hard_stop = (
                submission.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES
            )
            cleaned_terminal_attempt = False
            for attempt in snapshot.attempts:
                if (
                    attempt.state == "terminal"
                    and attempt.attempt_id not in self._cleaned_attempt_ids
                ):
                    self.scheduler.advance(
                        logical_trial_id=attempt.logical_trial_id,
                        attempt_id=attempt.attempt_id,
                        attempt_ttl_seconds=self.attempt_ttl_seconds,
                    )
                    self._cleaned_attempt_ids.add(attempt.attempt_id)
                    cleaned_terminal_attempt = True
                    break
            if cleaned_terminal_attempt:
                continue

            if not hard_stop:
                attempts = self._next_attempts(snapshot)
                if attempts:
                    self._advance_attempts(
                        attempts,
                        evaluator_capacity=snapshot.definition.evaluator_capacity,
                    )
                    continue

            unresolved = next(
                (
                    trial
                    for trial in snapshot.logical_trials
                    if trial.state != "terminal"
                ),
                None,
            )
            if unresolved is None:
                self.authority.finish(
                    operation_id=f"run/{self.authority.run_id}/finish"
                )
                continue
            if not hard_stop:
                raise RealmRetainedBatchRunError("canonical_state_invalid")

            live = tuple(
                item
                for item in snapshot.attempts
                if item.logical_trial_id
                == unresolved.admission.logical_trial_id
                and item.state != "terminal"
            )
            if len(live) > 1:
                raise RealmRetainedBatchRunError("canonical_state_invalid")
            if live:
                self.scheduler.terminalize(
                    logical_trial_id=unresolved.admission.logical_trial_id,
                    attempt_id=live[0].attempt_id,
                )
                continue
            if unresolved.state not in {"accepted", "retrying"}:
                raise RealmRetainedBatchRunError("canonical_state_invalid")
            self.runtime.ledger.cancel_run_logical_trial(
                operation_id=(
                    f"run/{self.authority.run_id}/hard-stop/"
                    f"{unresolved.admission.logical_trial_id}/cancel"
                ),
                actor_principal_id=self.authority.actor_principal_id,
                run_id=self.authority.run_id,
                logical_trial_id=(
                    unresolved.admission.logical_trial_id
                ),
                expected_run_revision=snapshot.revision.revision,
                controller_lease_id=self.authority.controller_lease_id,
                controller_holder_id=self.authority.controller_holder_id,
                controller_fencing_token=(
                    self.authority.controller_fencing_token
                ),
                code=submission.stop_code,
            )
            self.authority.refresh_controller()

    def _prepare_exchange(
        self,
        snapshot: RunLedgerSnapshot,
        *,
        round_index: int,
        exchange_input: Any,
    ) -> RunMethodExchangePreparationRecord:
        kind = exchange_input.kind
        return self.runtime.ledger.prepare_run_method_exchange(
            operation_id=(
                f"run/{self.authority.run_id}/method/{round_index}/{kind}/prepare"
            ),
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            round_index=round_index,
            expected_run_revision=snapshot.revision.revision,
            expected_controller_generation=snapshot.run.controller_generation,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
            exchange_input=exchange_input,
        )

    def _process_preparation(
        self,
        snapshot: RunLedgerSnapshot,
        preparation: RunMethodExchangePreparationRecord,
        method: Any,
        heartbeat: _Heartbeat,
    ) -> _InactiveMethodDrive | None:
        exchange = self._exchange(snapshot, preparation, completion=None)
        try:
            invocation = self._invoke(method, exchange)
        except RetainedBatchRuntimeError as error:
            if error.code != "worker_request_timeout":
                # Generic transport loss remains recoverable through exact
                # worker attachment/replay.  Only the configured request
                # deadline is a definitive response-less Method outcome.
                raise
            # The configured callback deadline produced no canonical Method
            # response.  Do not manufacture a completion and do not leave the
            # prepared exchange for endless controller-term replay.  A hard
            # method_failed close atomically retains the exact preparation as
            # abandoned, so recovery sees why the Run stopped without
            # reissuing the same over-deadline external Method call.
            return _InactiveMethodDrive(
                self._abandon_timed_out_method_exchange(
                    preparation, runtime_error_code=error.code
                )
            )
        heartbeat.raise_if_failed()
        if preparation.kind == "proposal":
            completion = self._complete_proposal(
                preparation, invocation, method=method
            )
        else:
            completion = self._complete_observation(preparation, invocation)
        self.authority.refresh_controller()
        self._ack_completion(method, exchange, completion)
        heartbeat.raise_if_failed()
        return None

    def _abandon_timed_out_method_exchange(
        self,
        preparation: RunMethodExchangePreparationRecord,
        *,
        runtime_error_code: str,
    ) -> RunLedgerSnapshot:
        """Retain one response-less Method failure and hard-stop its Run.

        A timed-out callback differs from a canonical ``ok:false`` Method
        response: there is no honest response digest to complete.  The Realm
        hard-stop operation already provides the correct response-less
        primitive by retaining ``method_exchange_abandoned`` beside the
        ``method_failed`` submission close.
        """

        if runtime_error_code != "worker_request_timeout":
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        snapshot = self.authority.refresh_controller()
        pending = self._pending_exchange(snapshot)
        if snapshot.run.state != "running" or pending != preparation:
            raise RealmRetainedBatchRunError("canonical_state_invalid")

        operation_id = (
            f"run/{self.authority.run_id}/method/"
            f"{preparation.exchange_id}/runtime-{runtime_error_code}"
        )
        submission = snapshot.control.current_submission
        if submission.state == "accepting":
            self.authority.close_submissions(
                operation_id=f"{operation_id}/close",
                stop_code="method_failed",
            )
        elif submission.state == "draining":
            if submission.stop_code not in METHOD_EXCHANGE_ABANDON_STOP_CODES:
                self.authority.escalate_stop(
                    operation_id=f"{operation_id}/escalate",
                    stop_code="method_failed",
                )
        else:
            raise RealmRetainedBatchRunError("canonical_state_invalid")

        snapshot = self.authority.refresh_controller()
        if not method_feedback_obligations_resolved(snapshot):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        return snapshot

    def _complete_proposal(
        self,
        preparation: RunMethodExchangePreparationRecord,
        invocation: _Invocation,
        *,
        method: Any,
    ) -> RunMethodExchangeCompletionRecord:
        if invocation.failure_outcome is not None:
            return self._close_proposal(
                preparation,
                outcome=invocation.failure_outcome,
                response_digest=invocation.response_digest,
                error_code=invocation.error_code,
                error_json=invocation.error_json,
            )
        assert invocation.response is not None
        try:
            result = _success_result(
                invocation.response,
                exchange_id=preparation.exchange_id,
                expected_fields={"candidates"},
            )
            candidates = result["candidates"]
            if isinstance(candidates, (str, bytes)) or not isinstance(
                candidates, Sequence
            ) or any(not isinstance(item, Mapping) for item in candidates):
                raise ValueError("proposal candidates are malformed")
            candidate_values = tuple(dict(item) for item in candidates)
        except (KeyError, TypeError, ValueError):
            return self._close_proposal(
                preparation,
                outcome="protocol_error",
                response_digest=invocation.response_digest,
                error_code="method_response_invalid",
            )

        if not candidate_values:
            return self._close_proposal(
                preparation,
                outcome="empty",
                response_digest=invocation.response_digest,
                error_code=None,
            )
        try:
            if self.authority.candidate_contract.get("format") == "files":
                if not callable(
                    getattr(method, "resolve_file_candidate_source", None)
                ):
                    raise TypeError(
                        "file-candidate method runtime has no staging resolver."
                    )

                def resolve_source(index: int, draft: Any) -> Any:
                    current = self.runtime.ledger.read_run_snapshot(
                        actor_principal_id=self.authority.actor_principal_id,
                        run_id=self.authority.run_id,
                    )
                    pending = tuple(
                        item
                        for item in current.method_exchange_preparations
                        if item.exchange_id == preparation.exchange_id
                    )
                    completed = {
                        item.exchange_id
                        for item in current.method_exchange_completions
                    }
                    if (
                        current.run.state != "running"
                        or current.revision.revision != self.authority.run_revision
                        or current.run.controller_lease_id
                        != self.authority.controller_lease_id
                        or current.run.controller_holder_id
                        != self.authority.controller_holder_id
                        or current.run.controller_fencing_token
                        != self.authority.controller_fencing_token
                        or pending != (preparation,)
                        or preparation.exchange_id in completed
                    ):
                        raise RuntimeError(
                            "Canonical controller or proposal coordinate changed."
                        )
                    return method.resolve_file_candidate_source(
                        exchange_id=preparation.exchange_id,
                        exchange_sequence=method_exchange_sequence(
                            round_index=preparation.round_index,
                            kind="proposal",
                        ),
                        ordinal=index,
                        candidate=draft,
                    )

                receipt = self.authority.complete_staged_file_method_proposal(
                    preparation,
                    candidates=candidate_values,
                    response_digest=invocation.response_digest,
                    content_service=self.runtime.content_service,
                    store_id=self.runtime.content_store.store_id,
                    source_resolver=resolve_source,
                    change_ttl_seconds=self.controller_ttl_seconds,
                    heartbeat_interval_seconds=self.heartbeat_interval_seconds,
                )
            else:
                receipt = self.authority.complete_method_proposal(
                    preparation,
                    candidates=candidate_values,
                    response_digest=invocation.response_digest,
                )
        except MethodProtocolError as error:
            return self._close_proposal(
                preparation,
                outcome="protocol_error",
                response_digest=invocation.response_digest,
                error_code=error.code,
            )
        except (ContentRejected, SourceChanged, TypeError, ValueError):
            return self._close_proposal(
                preparation,
                outcome="protocol_error",
                response_digest=invocation.response_digest,
                error_code="candidate_malformed",
            )
        return receipt.completion

    def _close_proposal(
        self,
        preparation: RunMethodExchangePreparationRecord,
        *,
        outcome: str,
        response_digest: str,
        error_code: str | None,
        error_json: Mapping[str, Any] | None = None,
    ) -> RunMethodExchangeCompletionRecord:
        receipt = self.runtime.ledger.complete_run_method_proposal_exchange(
            operation_id=(
                f"run/{self.authority.run_id}/method/{preparation.round_index}/"
                "proposal/complete"
            ),
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            round_index=preparation.round_index,
            prepared_input_digest=preparation.input_digest,
            outcome=outcome,
            response_digest=response_digest,
            error_code=error_code,
            error_json=error_json,
            expected_run_revision=self.authority.run_revision,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
        )
        return receipt.completion

    def _complete_observation(
        self,
        preparation: RunMethodExchangePreparationRecord,
        invocation: _Invocation,
    ) -> RunMethodExchangeCompletionRecord:
        if invocation.failure_outcome is None:
            assert invocation.response is not None
            try:
                result = _success_result(
                    invocation.response,
                    exchange_id=preparation.exchange_id,
                    expected_fields={"observation_count"},
                )
                count = result["observation_count"]
                expected = len(preparation.exchange_input.logical_trial_ids)
                if isinstance(count, bool) or not isinstance(count, int) or count != expected:
                    raise ValueError("observation count differs")
            except (KeyError, TypeError, ValueError):
                outcome = "protocol_error"
                error_code = "method_response_invalid"
                error_json = None
            else:
                outcome = "acknowledged"
                error_code = None
                error_json = None
        else:
            outcome = invocation.failure_outcome
            error_code = invocation.error_code
            error_json = invocation.error_json
        receipt = self.runtime.ledger.complete_run_method_observation_exchange(
            operation_id=(
                f"run/{self.authority.run_id}/method/{preparation.round_index}/"
                "observation/complete"
            ),
            actor_principal_id=self.authority.actor_principal_id,
            run_id=self.authority.run_id,
            round_index=preparation.round_index,
            prepared_input_digest=preparation.input_digest,
            outcome=outcome,
            response_digest=invocation.response_digest,
            error_code=error_code,
            error_json=error_json,
            expected_run_revision=self.authority.run_revision,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
        )
        return receipt.completion

    def _invoke(self, method: Any, exchange: _CanonicalExchange) -> _Invocation:
        try:
            response = method.request(
                exchange.preparation.exchange_id,
                exchange.operation,
                exchange.payload,
                exchange_sequence=exchange.sequence,
            )
        except RetainedBatchMethodError as error:
            outcome = _method_error_outcome(error.code)
            return _Invocation(
                response_digest=error.response_digest,
                failure_outcome=outcome,
                error_code=error.code,
                error_json=_recorded_method_cause(error.cause),
            )
        except RetainedBatchProtocolError as error:
            if (
                error.exchange_id != exchange.preparation.exchange_id
                or error.operation != exchange.operation
                or error.exchange_sequence != exchange.sequence
                or error.request_digest != exchange.request_digest
            ):
                raise RealmRetainedBatchRunError("worker_state_diverged") from None
            return _Invocation(
                response_digest=error.response_digest,
                failure_outcome="protocol_error",
                error_code=error.code,
            )
        if not isinstance(response, RetainedBatchWorkerResponse):
            raise RealmRetainedBatchRunError("worker_state_diverged")
        return _Invocation(
            response_digest=response.response_digest,
            response=response.to_dict(),
        )

    def _synchronize_worker(self, method: Any, heartbeat: _Heartbeat) -> None:
        snapshot = self.authority.refresh_controller()
        exchanges = self._canonical_stream(snapshot)
        completed_count = sum(item.completion is not None for item in exchanges)
        try:
            status = method.status(
                _control_exchange_id(self.authority.run_id, "status")
            )
        except (RetainedBatchMethodError, RetainedBatchProtocolError):
            raise RealmRetainedBatchRunError("worker_state_diverged") from None
        if not isinstance(status, RetainedBatchWorkerStatus):
            raise RealmRetainedBatchRunError("worker_state_diverged")
        heartbeat.raise_if_failed()
        if status.acknowledged_sequence > completed_count:
            raise RealmRetainedBatchRunError("worker_state_diverged")

        expected_chains = [INITIAL_BATCH_EXCHANGE_CHAIN]
        for exchange in exchanges[:completed_count]:
            expected_chains.append(
                retained_batch_exchange_chain_digest(
                    expected_chains[-1],
                    exchange_id=exchange.preparation.exchange_id,
                    exchange_sequence=exchange.sequence,
                    request_digest_value=exchange.request_digest,
                    response_digest=exchange.completion.response_digest,
                )
            )
        if status.acknowledged_chain != expected_chains[status.acknowledged_sequence]:
            raise RealmRetainedBatchRunError("worker_state_diverged")
        self._acknowledged_sequence = status.acknowledged_sequence
        self._acknowledged_chain = status.acknowledged_chain
        for completed_exchange in exchanges[: self._acknowledged_sequence]:
            self._cleanup_file_exchange(method, completed_exchange)

        pending = status.pending_exchange
        if pending is not None:
            next_sequence = self._acknowledged_sequence + 1
            if next_sequence > len(exchanges):
                raise RealmRetainedBatchRunError("worker_state_diverged")
            expected = exchanges[next_sequence - 1]
            if (
                pending.exchange_id != expected.preparation.exchange_id
                or pending.exchange_sequence != expected.sequence
                or pending.request_digest != expected.request_digest
                or (
                    expected.completion is not None
                    and pending.response_digest
                    != expected.completion.response_digest
                )
            ):
                raise RealmRetainedBatchRunError("worker_state_diverged")
            if expected.completion is None:
                return
            self._ack_completion(method, expected, expected.completion)

        while self._acknowledged_sequence < completed_count:
            exchange = exchanges[self._acknowledged_sequence]
            replayed = self._invoke(method, exchange)
            heartbeat.raise_if_failed()
            if replayed.response_digest != exchange.completion.response_digest:
                raise RealmRetainedBatchRunError("replay_diverged")
            self._ack_completion(method, exchange, exchange.completion)

    def _ack_completion(
        self,
        method: Any,
        exchange: _CanonicalExchange,
        completion: RunMethodExchangeCompletionRecord,
    ) -> None:
        snapshot = self.authority.refresh_controller()
        canonical = next(
            (
                item
                for item in snapshot.method_exchange_completions
                if item.exchange_id == completion.exchange_id
            ),
            None,
        )
        if canonical != completion:
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        if exchange.sequence != self._acknowledged_sequence + 1:
            raise RealmRetainedBatchRunError("worker_state_diverged")
        coordinate = RetainedBatchExchangeCoordinate(
            exchange_id=exchange.preparation.exchange_id,
            exchange_sequence=exchange.sequence,
            request_digest=exchange.request_digest,
            response_digest=completion.response_digest,
        )
        try:
            ack = method.ack(
                _control_exchange_id(
                    self.authority.run_id, f"ack-{exchange.sequence}"
                ),
                exchange=coordinate,
                previous_acknowledged_chain=self._acknowledged_chain,
            )
        except (RetainedBatchMethodError, RetainedBatchProtocolError):
            raise RealmRetainedBatchRunError("worker_state_diverged") from None
        if not isinstance(ack, RetainedBatchCacheAck):
            raise RealmRetainedBatchRunError("worker_state_diverged")
        expected_chain = retained_batch_exchange_chain_digest(
            self._acknowledged_chain,
            exchange_id=coordinate.exchange_id,
            exchange_sequence=coordinate.exchange_sequence,
            request_digest_value=coordinate.request_digest,
            response_digest=coordinate.response_digest,
        )
        if (
            ack.acknowledged_sequence != exchange.sequence
            or ack.acknowledged_exchange != coordinate
            or ack.acknowledged_chain != expected_chain
        ):
            raise RealmRetainedBatchRunError("worker_state_diverged")
        self._acknowledged_sequence = ack.acknowledged_sequence
        self._acknowledged_chain = ack.acknowledged_chain
        self._cleanup_file_exchange(method, exchange)

    def _cleanup_file_exchange(self, method: Any, exchange: _CanonicalExchange) -> None:
        if (
            self.authority.candidate_contract.get("format") != "files"
            or exchange.preparation.kind != "proposal"
        ):
            return
        cleanup = getattr(method, "cleanup_file_candidate_exchange", None)
        if not callable(cleanup):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        cleanup(
            exchange_id=exchange.preparation.exchange_id,
            exchange_sequence=exchange.sequence,
        )

    def _canonical_stream(
        self, snapshot: RunLedgerSnapshot
    ) -> tuple[_CanonicalExchange, ...]:
        completions = {
            item.exchange_id: item for item in snapshot.method_exchange_completions
        }
        result = tuple(
            self._exchange(
                snapshot,
                preparation,
                completion=completions.get(preparation.exchange_id),
            )
            for preparation in snapshot.method_exchange_preparations
        )
        if tuple(item.sequence for item in result) != tuple(
            range(1, len(result) + 1)
        ):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        completed = tuple(item.completion is not None for item in result)
        if completed != tuple(index < sum(completed) for index in range(len(result))):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        return result

    def _exchange(
        self,
        snapshot: RunLedgerSnapshot,
        preparation: RunMethodExchangePreparationRecord,
        *,
        completion: RunMethodExchangeCompletionRecord | None,
    ) -> _CanonicalExchange:
        sequence = method_exchange_sequence(
            round_index=preparation.round_index,
            kind=preparation.kind,
        )
        if preparation.kind == "proposal":
            operation = "propose"
            payload = proposal_worker_payload(preparation)
        else:
            operation = "observe"
            payload = observation_worker_payload(snapshot, preparation)
        request_digest = retained_batch_worker_request_digest(operation, payload)
        return _CanonicalExchange(
            preparation=preparation,
            completion=completion,
            operation=operation,
            payload=payload,
            sequence=sequence,
            request_digest=request_digest,
        )

    @staticmethod
    def _pending_exchange(
        snapshot: RunLedgerSnapshot,
    ) -> RunMethodExchangePreparationRecord | None:
        completed_ids = {
            item.exchange_id for item in snapshot.method_exchange_completions
        }
        pending = tuple(
            item
            for item in snapshot.method_exchange_preparations
            if item.exchange_id not in completed_ids
        )
        if len(pending) > 1:
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        if not pending:
            return None
        preparation = pending[0]
        submission = snapshot.control.current_submission
        if (
            submission.state == "draining"
            and submission.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES
            and submission.run_revision > preparation.prepared_run_revision
        ):
            return None
        return preparation

    @staticmethod
    def _round_awaiting_observation(snapshot: RunLedgerSnapshot) -> int | None:
        observation_rounds = {
            item.round_index
            for item in snapshot.method_exchange_preparations
            if item.kind == "observation"
        }
        submission = snapshot.control.current_submission
        pending = tuple(
            item.round_index
            for item in snapshot.method_exchange_completions
            if item.kind == "proposal"
            and item.outcome == "admitted"
            and item.round_index not in observation_rounds
            and not (
                submission.state == "draining"
                and submission.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES
                and submission.run_revision > item.committed_run_revision
            )
        )
        if len(pending) > 1:
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        return None if not pending else pending[0]

    def _sweep_terminal_attempt_cleanup(
        self, snapshot: RunLedgerSnapshot, heartbeat: _Heartbeat
    ) -> None:
        for attempt in snapshot.attempts:
            if (
                attempt.state != "terminal"
                or attempt.attempt_id in self._cleaned_attempt_ids
            ):
                continue
            self.scheduler.advance(
                logical_trial_id=attempt.logical_trial_id,
                attempt_id=attempt.attempt_id,
                attempt_ttl_seconds=self.attempt_ttl_seconds,
            )
            heartbeat.raise_if_failed()
            self._cleaned_attempt_ids.add(attempt.attempt_id)

    def _advance_attempts(
        self,
        attempts: Sequence[tuple[str, str]],
        *,
        evaluator_capacity: int,
    ) -> None:
        """Advance one deterministic attempt set with bounded physical overlap."""

        coordinates = tuple(attempts)
        if not coordinates:
            return
        capacity = _positive_int(evaluator_capacity, "evaluator capacity")
        if len(set(coordinates)) != len(coordinates):
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        if capacity == 1 or len(coordinates) == 1:
            for logical_trial_id, attempt_id in coordinates:
                self.scheduler.advance(
                    logical_trial_id=logical_trial_id,
                    attempt_id=attempt_id,
                    attempt_ttl_seconds=self.attempt_ttl_seconds,
                )
            return

        dispatch_width = min(
            capacity,
            len(coordinates),
            MAX_LOCAL_EVALUATOR_DISPATCH_THREADS,
        )
        executor = ThreadPoolExecutor(
            max_workers=dispatch_width,
            thread_name_prefix="optpilot-evaluator",
        )
        futures = tuple(
            executor.submit(
                self.scheduler.advance,
                logical_trial_id=logical_trial_id,
                attempt_id=attempt_id,
                attempt_ttl_seconds=self.attempt_ttl_seconds,
            )
            for logical_trial_id, attempt_id in coordinates
        )
        try:
            # Consume in canonical trial order. Completion/adoption order may
            # differ, but observation projection is anchored to proposal order.
            for future in futures:
                future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    def _next_attempts(
        self, snapshot: RunLedgerSnapshot
    ) -> tuple[tuple[str, str], ...]:
        attempts_by_trial = {
            trial.admission.logical_trial_id: []
            for trial in snapshot.logical_trials
        }
        all_attempt_ids = {item.attempt_id for item in snapshot.attempts}
        for attempt in snapshot.attempts:
            attempts_by_trial[attempt.logical_trial_id].append(attempt)
        max_attempts = snapshot.control.manifest.retry_policy.max_attempts
        selected: list[tuple[str, str]] = []

        for trial in snapshot.logical_trials:
            trial_id = trial.admission.logical_trial_id
            attempts = attempts_by_trial[trial_id]
            live = [item for item in attempts if item.state != "terminal"]
            if len(live) > 1 or (live and live[0] is not attempts[-1]):
                raise RealmRetainedBatchRunError("canonical_state_invalid")
            if trial.state == "terminal":
                if live:
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                continue
            if trial.state in {"accepted", "retrying"}:
                if live or (trial.state == "accepted" and attempts) or (
                    trial.state == "retrying" and not attempts
                ):
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                attempt_index = len(attempts) + 1
                if attempt_index > max_attempts:
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                attempt_id = self.identity_source.attempt(
                    run_id=self.authority.run_id,
                    logical_trial_id=trial_id,
                    attempt_index=attempt_index,
                )
                if attempt_id in all_attempt_ids:
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                selected.append((trial_id, attempt_id))
                all_attempt_ids.add(attempt_id)
                continue
            if trial.state in {"queued", "running"}:
                expected = "prepared" if trial.state == "queued" else "running"
                if len(live) != 1 or live[0].state != expected:
                    raise RealmRetainedBatchRunError("canonical_state_invalid")
                selected.append((trial_id, live[0].attempt_id))
                continue
            raise RealmRetainedBatchRunError("canonical_state_invalid")
        return tuple(selected)


def _success_result(
    response: Mapping[str, Any],
    *,
    exchange_id: str,
    expected_fields: set[str],
) -> Mapping[str, Any]:
    if set(response) != {"exchange_id", "ok", "result", "schema"}:
        raise ValueError("worker success response fields differ")
    if (
        response["schema"] != BATCH_RESPONSE_SCHEMA
        or response["exchange_id"] != exchange_id
        or response["ok"] is not True
        or not isinstance(response["result"], Mapping)
        or set(response["result"]) != expected_fields
    ):
        raise ValueError("worker success response is invalid")
    return response["result"]


def _method_error_outcome(code: str) -> str:
    if code == "method_failed":
        return "method_failed"
    if code in _PROTOCOL_METHOD_CODES:
        return "protocol_error"
    raise RealmRetainedBatchRunError("exchange_rejected")


def _control_exchange_id(run_id: str, purpose: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({"purpose": purpose, "run_id": run_id})
    ).hexdigest()
    return f"driver-{purpose.split('-', 1)[0]}-{digest[:32]}"


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text.")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _positive_finite(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{label} must be positive and finite.")
    return float(value)


def _lower_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")
    return value


__all__ = [
    "DigestRealmAttemptIdentitySource",
    "RealmRetainedBatchRunDriver",
    "RealmRetainedBatchRunError",
    "RunControllerTakeoverExpectation",
]
