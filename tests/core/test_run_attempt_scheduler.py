from __future__ import annotations

import copy
import inspect
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_attempt_launcher import RealmLocalAttemptLauncher
from optpilot.realm.local_process_attempt_provider import (
    LocalAttemptCleanupResult,
    LocalAttemptProviderError,
    LocalAttemptStarted,
    LocalAttemptTerminal,
    LocalProcessAttemptProvider,
)
from optpilot.realm.local_process_supervisor import LocalProcessSupervisor
from optpilot.realm.process_execution_binder import (
    ProcessExecutionResourceError,
    ProcessExecutionResourceFailure,
)
from optpilot.realm.refs import canonical_json_bytes
from optpilot.run_attempt_heartbeat import RunAttemptHeartbeatCoordinator
from optpilot.run_attempt_scheduler import (
    RunAttemptAdvanceResult,
    RunAttemptHeartbeatPolicy,
    RunAttemptScheduler,
)
from optpilot.run_authority import RetainedRunAuthority
from optpilot.run_control_manifest import RetryPolicy
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)
from tests.core.test_realm_local_attempt_launcher import (
    _RetainedRuntimeFixture,
    _SimulatedParentCrash,
)


def _identity_normalizer(candidate: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(candidate)


def _legacy_test_normalizer(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result.setdefault("format", "parameters")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault(
        "generator", {"method_id": "method-a", "strategy": "external"}
    )
    result.setdefault("validation", {})
    result.setdefault("materialization", {})
    return result


class _ProviderProxy:
    """Instrument only the path-free provider facade used by the scheduler."""

    def __init__(
        self,
        delegate: LocalProcessAttemptProvider,
        *,
        after_start: Callable[[object, object], None] | None = None,
    ) -> None:
        self.delegate = delegate
        self._ledger = delegate._ledger
        self._actor_principal_id = delegate._actor_principal_id
        self.after_start = after_start
        self.start_calls = 0
        self.stop_calls = 0
        self.cleanup_calls = 0

    def start_or_attach(self, *, run_id: str, attempt_id: str, heartbeat):
        self.start_calls += 1
        observation = self.delegate.start_or_attach(
            run_id=run_id,
            attempt_id=attempt_id,
            heartbeat=heartbeat,
        )
        if self.after_start is not None:
            self.after_start(observation, heartbeat)
        return observation

    def wait_terminal(
        self, *, run_id: str, attempt_id: str, timeout: float | None = None
    ):
        return self.delegate.wait_terminal(
            run_id=run_id, attempt_id=attempt_id, timeout=timeout
        )

    def stop_and_wait_terminal(self, *, run_id: str, attempt_id: str, **kwargs):
        self.stop_calls += 1
        return self.delegate.stop_and_wait_terminal(
            run_id=run_id, attempt_id=attempt_id, **kwargs
        )

    def terminalize_current(
        self, *, run_id: str, attempt_id: str, heartbeat, **kwargs
    ):
        self.stop_calls += 1
        return self.delegate.terminalize_current(
            run_id=run_id,
            attempt_id=attempt_id,
            heartbeat=heartbeat,
            **kwargs,
        )

    def finalize_terminal(self, *, run_id: str, attempt_id: str, terminal):
        return self.delegate.finalize_terminal(
            run_id=run_id, attempt_id=attempt_id, terminal=terminal
        )

    def resume_cleanup(self, *, run_id: str, attempt_id: str):
        self.cleanup_calls += 1
        return self.delegate.resume_cleanup(run_id=run_id, attempt_id=attempt_id)


class _ControllableHeartbeat:
    def __init__(self, coordinator: RunAttemptHeartbeatCoordinator) -> None:
        self.coordinator = coordinator
        self.failure: Exception | None = None

    def start(self):
        self.coordinator.start()
        return self

    def attach_binding(self, binding) -> None:
        self.coordinator.attach_binding(binding)

    def heartbeat_once(self):
        if self.failure is not None:
            raise self.failure
        return self.coordinator.heartbeat_once()

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.coordinator.raise_if_failed()

    def stop(self) -> None:
        self.coordinator.stop()

    @property
    def stopped(self) -> bool:
        return self.coordinator.stopped


class _CleanupOnlyProvider:
    def __init__(self, ledger: RealmLedger, *, actor_principal_id: str) -> None:
        self._ledger = ledger
        self._actor_principal_id = actor_principal_id
        self.cleanup_calls: list[tuple[str, str]] = []

    def start_or_attach(self, **_kwargs):
        raise AssertionError("terminal cleanup must not start a provider")

    def wait_terminal(self, **_kwargs):
        raise AssertionError("terminal cleanup must not wait for a provider")

    def stop_and_wait_terminal(self, **_kwargs):
        raise AssertionError("terminal cleanup must not stop a provider")

    def terminalize_current(self, **_kwargs):
        raise AssertionError("terminal cleanup must not terminalize a provider")

    def finalize_terminal(self, **_kwargs):
        raise AssertionError("terminal cleanup must not finalize again")

    def resume_cleanup(self, *, run_id: str, attempt_id: str):
        self.cleanup_calls.append((run_id, attempt_id))
        return LocalAttemptCleanupResult(
            run_id=run_id, attempt_id=attempt_id, state="not_required"
        )


@unittest.skipUnless(os.name == "posix", "local attempt process is POSIX-only")
class RunAttemptSchedulerTest(unittest.TestCase):
    def runtime(
        self, *, fresh: bool = False, evaluation_delay_seconds: float = 0.0
    ) -> _RetainedRuntimeFixture:
        if fresh:
            with mock.patch.object(
                RealmLedger,
                "prepare_run_attempt",
                autospec=True,
                return_value=None,
            ):
                fixture = _RetainedRuntimeFixture(
                    evaluation_delay_seconds=evaluation_delay_seconds
                )
        else:
            fixture = _RetainedRuntimeFixture(
                evaluation_delay_seconds=evaluation_delay_seconds
            )
        self.addCleanup(fixture.close)
        return fixture

    def authority(
        self, fixture: _RetainedRuntimeFixture
    ) -> RetainedRunAuthority:
        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        return RetainedRunAuthority.hydrate(
            ledger=fixture.ledger,
            actor_principal_id="operator",
            run_id=fixture.created.run.run_id,
            candidate_normalizer=_identity_normalizer,
            normalizer_version=snapshot.control.manifest.normalizer_version,
        )

    def provider(
        self,
        fixture: _RetainedRuntimeFixture,
        *,
        provider_root: str = "scheduler-provider",
        fault_injector=None,
    ) -> tuple[
        LocalProcessSupervisor,
        RealmLocalAttemptLauncher,
        Any,
        RealmAttemptFinalizer,
        LocalProcessAttemptProvider,
    ]:
        supervisor = LocalProcessSupervisor(
            fixture.root / provider_root, fault_injector=fault_injector
        )
        launcher = RealmLocalAttemptLauncher(supervisor)
        binder = fixture.binder_for(launcher)
        finalizer = RealmAttemptFinalizer(
            fixture.ledger,
            fixture.content,
            actor_principal_id="operator",
            store_id=fixture.store.store_id,
        )
        provider = LocalProcessAttemptProvider(
            fixture.ledger,
            binder,
            launcher,
            finalizer,
            actor_principal_id="operator",
        )
        return supervisor, launcher, binder, finalizer, provider

    def scheduler(
        self,
        fixture: _RetainedRuntimeFixture,
        provider,
        *,
        authority: RetainedRunAuthority | None = None,
        heartbeat_factory=None,
    ) -> RunAttemptScheduler:
        return RunAttemptScheduler(
            self.authority(fixture) if authority is None else authority,
            provider,
            heartbeat_factory=heartbeat_factory,
            heartbeat_policy=RunAttemptHeartbeatPolicy(interval_seconds=30.0),
        )

    @staticmethod
    def coordinates(fixture: _RetainedRuntimeFixture) -> dict[str, str]:
        return {
            "run_id": fixture.created.run.run_id,
            "attempt_id": "attempt-a",
        }

    def heartbeat(
        self, fixture: _RetainedRuntimeFixture
    ) -> RunAttemptHeartbeatCoordinator:
        receipt = fixture.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator", **self.coordinates(fixture)
        )
        heartbeat = RunAttemptHeartbeatCoordinator(
            fixture.ledger,
            actor_principal_id="operator",
            receipt=receipt,
            interval_seconds=30.0,
        )
        heartbeat.start()
        self.addCleanup(heartbeat.stop)
        return heartbeat

    def confirm(
        self,
        fixture: _RetainedRuntimeFixture,
        observation: LocalAttemptStarted | LocalAttemptTerminal,
    ) -> None:
        if isinstance(observation, LocalAttemptStarted):
            launch_token = observation.launch_token
            binding_id = observation.binding_id
            evidence_fingerprint = observation.evidence_fingerprint
            launch_request_digest = observation.launch_request_digest
        else:
            launch_token = observation.evidence.launch_token
            binding_id = observation.evidence.binding_id
            evidence_fingerprint = observation.evidence.evidence_fingerprint
            launch_request_digest = observation.evidence.launch_request_digest
        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        fixture.ledger.confirm_run_attempt_launch(
            operation_id="scheduler-test/manual-confirm",
            actor_principal_id="operator",
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
            expected_run_revision=snapshot.revision.revision,
            **self.coordinates(fixture),
            **fixture.controller_arguments(),
        )

    def replace_controller(
        self, fixture: _RetainedRuntimeFixture
    ) -> RetainedRunAuthority:
        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        fixture.ledger.replace_run_controller(
            operation_id="scheduler-test/controller/replace",
            actor_principal_id="operator",
            run_id=snapshot.run.run_id,
            expected_controller_generation=snapshot.run.controller_generation,
            expected_controller_lease_id=snapshot.run.controller_lease_id,
            expected_controller_holder_id=snapshot.run.controller_holder_id,
            expected_controller_fencing_token=snapshot.run.controller_fencing_token,
            new_controller_holder_id="replacement-controller",
            controller_ttl_seconds=300,
        )
        return self.authority(fixture)

    def advance(self, scheduler: RunAttemptScheduler) -> RunAttemptAdvanceResult:
        return scheduler.advance(
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
            attempt_ttl_seconds=60,
        )

    def test_fresh_success_prepares_runs_adopts_and_cleans(self) -> None:
        fixture = self.runtime(fresh=True)
        _supervisor, _launcher, _binder, _finalizer, provider = self.provider(
            fixture
        )

        result = self.advance(self.scheduler(fixture, provider))

        self.assertEqual(result.action, "adopted")
        self.assertEqual(result.attempt.state, "terminal")
        self.assertEqual(result.attempt.outcome, "success")
        self.assertEqual(result.logical_transition.to_state, "terminal")
        self.assertEqual(result.cleanup.state, "cleaned")
        self.assertTrue(result.physically_started)

    def test_shared_scheduler_overlaps_real_evaluator_processes(self) -> None:
        barrier_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(barrier_temporary.cleanup)
        barrier = Path(barrier_temporary.name)
        evaluator_source = (
            "from pathlib import Path\n"
            "import time\n"
            "def evaluate(candidate, context):\n"
            f"    barrier = Path({str(barrier)!r})\n"
            "    marker = barrier / (str(candidate['x']) + '.ready')\n"
            "    marker.write_text('ready', encoding='utf-8')\n"
            "    deadline = time.monotonic() + 5.0\n"
            "    while len(tuple(barrier.glob('*.ready'))) < 2:\n"
            "        if time.monotonic() >= deadline:\n"
            "            raise RuntimeError('parallel evaluator rendezvous timed out')\n"
            "        time.sleep(0.02)\n"
            "    return {'score': candidate['x']}\n"
        )
        fixture = _RetainedRuntimeFixture(
            evaluator_source=evaluator_source,
            include_second_candidate=True,
            attempt_ttl_seconds=60,
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, _binder, _finalizer, provider = self.provider(
            fixture, provider_root="parallel-scheduler-provider"
        )
        scheduler = self.scheduler(fixture, provider)
        startup_barrier = threading.Barrier(2, timeout=5)
        start_or_attach = provider.start_or_attach

        def gated_start_or_attach(**kwargs):
            startup_barrier.wait()
            return start_or_attach(**kwargs)

        with mock.patch.object(
            provider, "start_or_attach", side_effect=gated_start_or_attach
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(
                        scheduler.advance,
                        logical_trial_id="trial-a",
                        attempt_id="attempt-a",
                        attempt_ttl_seconds=60,
                    ),
                    executor.submit(
                        scheduler.advance,
                        logical_trial_id="trial-b",
                        attempt_id="attempt-b",
                        attempt_ttl_seconds=60,
                    ),
                )
                results = tuple(future.result(timeout=15) for future in futures)

        self.assertEqual(
            [result.attempt.outcome for result in results], ["success", "success"]
        )
        self.assertEqual(
            {path.name for path in barrier.glob("*.ready")},
            {"0.5.ready", "0.75.ready"},
        )

    def test_launch_confirmation_retries_concurrent_provider_binding_commit(
        self,
    ) -> None:
        """A provider bind may advance the run head inside confirm's CAS window."""

        fixture = _RetainedRuntimeFixture(
            include_second_candidate=True,
            evaluation_delay_seconds=0.1,
            attempt_ttl_seconds=60,
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, _binder, _finalizer, provider = self.provider(
            fixture, provider_root="binding-confirm-race-provider"
        )
        scheduler = self.scheduler(fixture, provider)
        allow_second_binding = threading.Event()
        second_binding_committed = threading.Event()
        stale_confirmation_injected = threading.Event()
        confirmation_calls: list[str] = []
        commit_binding = fixture.ledger.commit_run_attempt_binding
        confirm_launch = fixture.ledger.confirm_run_attempt_launch

        def racing_commit_binding(**kwargs):
            attempt_id = kwargs["draft"].attempt_id
            if attempt_id == "attempt-b" and not allow_second_binding.wait(
                timeout=5
            ):
                raise RuntimeError("second binding race gate timed out")
            receipt = commit_binding(**kwargs)
            if attempt_id == "attempt-b":
                second_binding_committed.set()
            return receipt

        def racing_confirm_launch(**kwargs):
            attempt_id = kwargs["attempt_id"]
            confirmation_calls.append(attempt_id)
            if (
                attempt_id == "attempt-a"
                and not stale_confirmation_injected.is_set()
            ):
                # ``expected_run_revision`` was chosen before entering this
                # wrapper.  Let B commit its binding now so A's first canonical
                # compare-and-swap deterministically observes harmless drift.
                stale_confirmation_injected.set()
                allow_second_binding.set()
                if not second_binding_committed.wait(timeout=5):
                    raise RuntimeError("second binding did not commit in time")
            return confirm_launch(**kwargs)

        with mock.patch.object(
            fixture.ledger,
            "commit_run_attempt_binding",
            side_effect=racing_commit_binding,
        ), mock.patch.object(
            fixture.ledger,
            "confirm_run_attempt_launch",
            side_effect=racing_confirm_launch,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(
                        scheduler.advance,
                        logical_trial_id="trial-a",
                        attempt_id="attempt-a",
                        attempt_ttl_seconds=60,
                    ),
                    executor.submit(
                        scheduler.advance,
                        logical_trial_id="trial-b",
                        attempt_id="attempt-b",
                        attempt_ttl_seconds=60,
                    ),
                )
                results = tuple(future.result(timeout=15) for future in futures)

        self.assertTrue(stale_confirmation_injected.is_set())
        self.assertGreaterEqual(confirmation_calls.count("attempt-a"), 2)
        self.assertEqual(
            [result.attempt.outcome for result in results], ["success", "success"]
        )

    def test_prepared_and_running_attempts_are_recovered(self) -> None:
        with self.subTest(state="prepared"):
            fixture = self.runtime()
            _s, _l, _b, _f, provider = self.provider(fixture)
            with mock.patch.object(
                fixture.ledger,
                "prepare_run_attempt",
                side_effect=AssertionError("prepared recovery must not prepare again"),
            ):
                result = self.advance(self.scheduler(fixture, provider))
            self.assertEqual(result.action, "adopted")
            self.assertTrue(result.physically_started)

        with self.subTest(state="running"):
            fixture = self.runtime(evaluation_delay_seconds=0.25)
            _s, _l, _b, _f, provider = self.provider(fixture)
            heartbeat = self.heartbeat(fixture)
            observation = provider.start_or_attach(
                heartbeat=heartbeat, **self.coordinates(fixture)
            )
            self.confirm(fixture, observation)
            heartbeat.stop()

            result = self.advance(self.scheduler(fixture, provider))
            self.assertEqual(result.action, "adopted")
            self.assertTrue(result.physically_started)
            self.assertEqual(result.attempt.state, "terminal")

    def test_terminalize_prepared_attempt_never_enters_start_path(self) -> None:
        fixture = self.runtime()
        _s, _l, _b, _f, delegate = self.provider(fixture)
        provider = _ProviderProxy(delegate)
        scheduler = self.scheduler(fixture, provider)

        result = scheduler.terminalize(
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
        )

        self.assertEqual(result.action, "terminalized")
        self.assertFalse(result.physically_started)
        self.assertEqual(result.attempt.state, "terminal")
        self.assertEqual(result.attempt.code, "worker_never_started")
        self.assertEqual(provider.start_calls, 0)
        self.assertEqual(provider.stop_calls, 1)

    def test_terminalization_winning_startup_race_prevents_late_confirmation(
        self,
    ) -> None:
        fixture = self.runtime(evaluation_delay_seconds=2.0)
        _s, _l, _b, _f, provider = self.provider(fixture)
        scheduler = self.scheduler(fixture, provider)
        started = threading.Event()
        release = threading.Event()
        start_or_attach = provider.start_or_attach

        def gated_start_or_attach(**kwargs):
            observation = start_or_attach(**kwargs)
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("startup race gate timed out")
            return observation

        with mock.patch.object(
            provider, "start_or_attach", side_effect=gated_start_or_attach
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.advance, scheduler)
                try:
                    self.assertTrue(started.wait(timeout=5))
                    terminalized = scheduler.terminalize(
                        logical_trial_id="trial-a",
                        attempt_id="attempt-a",
                    )
                finally:
                    release.set()
                raced = future.result(timeout=10)

        self.assertEqual(terminalized.action, "terminalized")
        self.assertEqual(raced.action, "cleanup_only")
        self.assertEqual(raced.attempt, terminalized.attempt)
        self.assertTrue(raced.physically_started)

    def test_never_started_prepared_attempt_is_adopted_without_false_start(
        self,
    ) -> None:
        fixture = self.runtime()

        def crash(point: str) -> None:
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        _s1, _l1, _b1, _f1, crashing = self.provider(
            fixture, fault_injector=crash
        )
        heartbeat = self.heartbeat(fixture)
        with self.assertRaises(_SimulatedParentCrash):
            crashing.start_or_attach(
                heartbeat=heartbeat, **self.coordinates(fixture)
            )
        heartbeat.stop()

        _s2, _l2, _b2, _f2, recovered = self.provider(fixture)
        result = self.advance(self.scheduler(fixture, recovered))

        self.assertEqual(result.action, "adopted")
        self.assertFalse(result.physically_started)
        self.assertEqual(result.attempt.code, "worker_never_started")
        self.assertEqual(result.attempt.state, "terminal")

    def test_terminal_attempt_is_cleanup_only_and_pending_debt_is_retryable(
        self,
    ) -> None:
        fixture = self.runtime(fresh=True)
        _s, _l, binder, _f, provider = self.provider(fixture)
        failure = ProcessExecutionResourceError(
            "private cleanup failed",
            (
                ProcessExecutionResourceFailure(
                    phase="terminal-cleanup",
                    resource_kind="volume",
                    logical_name="trial",
                    error=RuntimeError(str(fixture.root / "private")),
                ),
            ),
        )
        with mock.patch.object(
            binder, "resume_authorized_cleanup", side_effect=failure
        ):
            first = self.advance(self.scheduler(fixture, provider))
        self.assertEqual(first.action, "adopted")
        self.assertEqual(first.cleanup.state, "pending")
        self.assertEqual(first.cleanup.code, "provider_cleanup_pending")

        second = self.advance(self.scheduler(fixture, provider))
        self.assertEqual(second.action, "cleanup_only")
        self.assertEqual(second.cleanup.state, "cleaned")
        self.assertEqual(second.attempt, first.attempt)
        self.assertEqual(second.logical_transition, first.logical_transition)

    def test_finalizer_failure_retries_from_durable_terminal_evidence(self) -> None:
        fixture = self.runtime()
        _s, _l, _b, finalizer, provider = self.provider(fixture)
        real_finalize = finalizer.finalize
        calls = 0

        def flaky_finalize(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("capture interrupted")
            return real_finalize(**kwargs)

        with mock.patch.object(finalizer, "finalize", side_effect=flaky_finalize):
            scheduler = self.scheduler(fixture, provider)
            with self.assertRaises(LocalAttemptProviderError) as raised:
                self.advance(scheduler)
            self.assertEqual(raised.exception.operation, "finalize_terminal")
            self.assertEqual(raised.exception.code, "provider_operation_failed")
            attempt = fixture.ledger.read_run_attempt(
                actor_principal_id="operator", **self.coordinates(fixture)
            )
            self.assertEqual(attempt.state, "running")
            fixture.ledger.read_run_attempt_terminal_evidence(
                actor_principal_id="operator", **self.coordinates(fixture)
            )

            result = self.advance(scheduler)

        self.assertEqual(calls, 2)
        self.assertEqual(result.action, "adopted")
        self.assertEqual(result.attempt.state, "terminal")

    def test_adoption_response_loss_accepts_only_exact_canonical_digest(self) -> None:
        fixture = self.runtime(fresh=True)
        _s, _l, _b, _f, provider = self.provider(fixture)
        real_adopt = fixture.ledger.adopt_run_attempt

        def lose_response(**kwargs):
            real_adopt(**kwargs)
            raise RuntimeError("adoption response lost")

        with mock.patch.object(
            fixture.ledger, "adopt_run_attempt", side_effect=lose_response
        ):
            result = self.advance(self.scheduler(fixture, provider))

        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        transition = next(
            item
            for item in snapshot.attempt_transitions
            if item.attempt_id == result.attempt.attempt_id
            and item.transition_index == result.attempt.head_transition_index
        )
        self.assertEqual(result.action, "adopted")
        self.assertIn("finalization_digest", transition.payload)

    def test_old_generation_unbound_and_bound_attempts_are_reconciled(self) -> None:
        with self.subTest(binding="unbound"):
            fixture = self.runtime()
            _s, _l, _b, _f, provider = self.provider(fixture)
            proxied = _ProviderProxy(provider)
            replacement = self.replace_controller(fixture)

            result = self.advance(
                self.scheduler(fixture, proxied, authority=replacement)
            )
            self.assertEqual(result.action, "reconciled")
            self.assertEqual(result.attempt.code, "attempt_authority_lost")
            self.assertFalse(result.physically_started)
            self.assertEqual(proxied.stop_calls, 0)
            self.assertEqual(result.cleanup.state, "not_required")

        with self.subTest(binding="bound-without-evidence"):
            fixture = self.runtime()
            _s, launcher, binder, _f, provider = self.provider(fixture)
            prepared = binder.prepare_prepared(
                actor_principal_id="operator", **self.coordinates(fixture)
            )
            compiled = launcher._compile(prepared)
            reservation = launcher._reserve(compiled)
            prepared._commit_reserved_launch(reservation)
            replacement = self.replace_controller(fixture)
            proxied = _ProviderProxy(provider)

            result = self.advance(
                self.scheduler(fixture, proxied, authority=replacement)
            )
            self.assertEqual(result.action, "reconciled")
            self.assertEqual(proxied.stop_calls, 1)
            self.assertFalse(result.physically_started)
            self.assertEqual(result.cleanup.state, "cleaned")

    def test_confirmation_failure_stops_writer_and_never_adopts(self) -> None:
        fixture = self.runtime(fresh=True, evaluation_delay_seconds=2.0)
        _s, _l, _b, _f, provider = self.provider(fixture)
        proxied = _ProviderProxy(provider)
        heartbeats: list[RunAttemptHeartbeatCoordinator] = []

        def heartbeat_factory(receipt):
            heartbeat = RunAttemptHeartbeatCoordinator(
                fixture.ledger,
                actor_principal_id="operator",
                receipt=receipt,
                interval_seconds=30.0,
            )
            heartbeats.append(heartbeat)
            return heartbeat

        with mock.patch.object(
            fixture.ledger,
            "confirm_run_attempt_launch",
            side_effect=RealmConflict("confirmation fence lost"),
        ):
            with self.assertRaisesRegex(RealmConflict, "confirmation fence lost"):
                self.advance(
                    self.scheduler(
                        fixture,
                        proxied,
                        heartbeat_factory=heartbeat_factory,
                    )
                )

        attempt = fixture.ledger.read_run_attempt(
            actor_principal_id="operator", **self.coordinates(fixture)
        )
        evidence = fixture.ledger.read_run_attempt_terminal_evidence(
            actor_principal_id="operator", **self.coordinates(fixture)
        )
        self.assertEqual(attempt.state, "prepared")
        self.assertTrue(evidence.started)
        self.assertEqual(evidence.disposition, "killed")
        self.assertEqual(proxied.stop_calls, 1)
        self.assertTrue(heartbeats[0].stopped)

    def test_heartbeat_failure_stops_writer_and_leaves_no_adoption(self) -> None:
        fixture = self.runtime(fresh=True, evaluation_delay_seconds=2.0)
        _s, _l, _b, _f, provider = self.provider(fixture)
        controlled: list[_ControllableHeartbeat] = []

        def heartbeat_factory(receipt):
            heartbeat = _ControllableHeartbeat(
                RunAttemptHeartbeatCoordinator(
                    fixture.ledger,
                    actor_principal_id="operator",
                    receipt=receipt,
                    interval_seconds=30.0,
                )
            )
            controlled.append(heartbeat)
            return heartbeat

        def fail_after_start(_observation, heartbeat) -> None:
            heartbeat.failure = RuntimeError("heartbeat authority lost")

        proxied = _ProviderProxy(provider, after_start=fail_after_start)
        with self.assertRaisesRegex(RuntimeError, "heartbeat authority lost"):
            self.advance(
                self.scheduler(
                    fixture,
                    proxied,
                    heartbeat_factory=heartbeat_factory,
                )
            )

        attempt = fixture.ledger.read_run_attempt(
            actor_principal_id="operator", **self.coordinates(fixture)
        )
        evidence = fixture.ledger.read_run_attempt_terminal_evidence(
            actor_principal_id="operator", **self.coordinates(fixture)
        )
        self.assertEqual(attempt.state, "prepared")
        self.assertEqual(evidence.disposition, "killed")
        self.assertEqual(proxied.stop_calls, 1)
        self.assertTrue(controlled[0].stopped)

    def test_process_crash_after_durable_start_recovers_without_in_process_drain(
        self,
    ) -> None:
        fixture = self.runtime(evaluation_delay_seconds=0.5)
        _s1, _l1, _b1, _f1, provider1 = self.provider(fixture)
        heartbeats: list[RunAttemptHeartbeatCoordinator] = []

        def heartbeat_factory(receipt):
            heartbeat = RunAttemptHeartbeatCoordinator(
                fixture.ledger,
                actor_principal_id="operator",
                receipt=receipt,
                interval_seconds=30.0,
            )
            heartbeats.append(heartbeat)
            return heartbeat

        def crash_after_start(observation, _heartbeat) -> None:
            self.assertIsInstance(
                observation, (LocalAttemptStarted, LocalAttemptTerminal)
            )
            raise _SimulatedParentCrash()

        crashing = _ProviderProxy(provider1, after_start=crash_after_start)
        with self.assertRaises(_SimulatedParentCrash):
            self.advance(
                self.scheduler(
                    fixture,
                    crashing,
                    heartbeat_factory=heartbeat_factory,
                )
            )
        self.assertEqual(crashing.stop_calls, 0)
        self.assertFalse(heartbeats[0].stopped)
        # End the simulated dead process's renewal thread without touching its
        # worker, then recover exclusively by durable coordinates.
        heartbeats[0].stop()

        _s2, _l2, _b2, _f2, provider2 = self.provider(fixture)
        result = self.advance(self.scheduler(fixture, provider2))
        self.assertEqual(result.action, "adopted")
        self.assertTrue(result.physically_started)

    def test_public_api_and_result_never_expose_paths_proofs_or_operation_ids(
        self,
    ) -> None:
        fixture = self.runtime(fresh=True)
        _s, _l, _b, _f, provider = self.provider(fixture)
        result = self.advance(self.scheduler(fixture, provider))

        self.assertEqual(
            tuple(RunAttemptAdvanceResult.__dataclass_fields__),
            (
                "attempt",
                "logical_transition",
                "action",
                "cleanup",
                "physically_started",
            ),
        )
        advance_parameters = inspect.signature(RunAttemptScheduler.advance).parameters
        self.assertEqual(
            tuple(advance_parameters),
            (
                "self",
                "logical_trial_id",
                "attempt_id",
                "attempt_ttl_seconds",
            ),
        )
        public = canonical_json_bytes(
            {
                "attempt": result.attempt.to_dict(),
                "logical_transition": result.logical_transition.to_dict(),
                "action": result.action,
                "cleanup": result.cleanup.__dict__,
                "physically_started": result.physically_started,
            }
        )
        self.assertNotIn(str(fixture.root).encode(), public)
        for private_name in (
            b"backend_token",
            b"terminal_proof",
            b"workspace",
            b"operation_id",
            b"finalization",
        ):
            self.assertNotIn(private_name, public)

    def test_cleanup_of_old_terminal_retry_returns_its_exact_transition(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        ledger = RealmLedger(root / "realm.sqlite3")
        self.addCleanup(ledger.close)
        store = LocalContentStore(root / "store", store_id="local-a")
        self.addCleanup(store.close)
        ledger.register_principal(
            operation_id="scheduler/principal",
            principal_id="operator",
            kind="human",
        )
        ledger.register_store(
            operation_id="scheduler/store",
            store_id=store.store_id,
            backend_kind=store.BACKEND_KIND,
            root_marker=store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=ledger,
            store=store,
            root=root,
            actor_principal_id="operator",
            prefix="scheduler-old-terminal",
        )
        manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=2),
            method_id="method-a",
            retry_policy=RetryPolicy(
                max_attempts=2, retryable_outcomes=("failed",)
            ),
        )
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        created = ledger.create_run_namespace(
            operation_id="scheduler/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        authority = RetainedRunAuthority.from_create_receipt(
            ledger=ledger,
            actor_principal_id="operator",
            receipt=created,
            candidate_normalizer=_legacy_test_normalizer,
            normalizer_version=manifest.normalizer_version,
        )
        logical_trial_id = authority.admit(
            [
                {
                    "candidate_id": "candidate-a",
                    "format": "parameters",
                    "spec": {"x": 1},
                }
            ],
            admission_id="batch-a",
        )[0].logical_trial_id

        first = ledger.prepare_run_attempt(
            operation_id="scheduler/first/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id=logical_trial_id,
            attempt_id="attempt-a",
            expected_run_revision=authority.run_revision,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
            attempt_ttl_seconds=60,
        )
        ledger.adopt_run_attempt(
            operation_id="scheduler/first/adopt",
            actor_principal_id="operator",
            run_id="run-a",
            attempt_id="attempt-a",
            change_id=first.attempt.capture_change_id,
            finalization=AttemptFinalization(
                attempt_id="attempt-a",
                evaluation_spec_digest=first.attempt.evaluation_spec_digest,
                binding_id=first.attempt.binding_id,
                effective_outcome="failed",
                effective_code="test_failure",
                captured_artifacts=(),
                platform_error={
                    "code": "test_failure",
                    "message": "synthetic retry",
                    "details": {"phase": "test"},
                },
            ),
            expected_run_revision=first.revision.revision,
            expected_owner_revision=first.revision.owner_revision,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
        )
        authority.refresh_controller()
        second = ledger.prepare_run_attempt(
            operation_id="scheduler/second/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id=logical_trial_id,
            attempt_id="attempt-b",
            expected_run_revision=authority.run_revision,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
            attempt_ttl_seconds=60,
        )
        envelope = AttemptEnvelope(
            attempt_id="attempt-b",
            evaluation_spec_digest=second.attempt.evaluation_spec_digest,
            binding_id=second.attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 1}, "metadata": {}},
            metric_values={"score": 1.0},
            constraint_results={},
            output_declarations=(),
            event_summary={"primary_metric": "score"},
            execution_metadata={"worker": "synthetic"},
            error={},
        )
        ledger.adopt_run_attempt(
            operation_id="scheduler/second/adopt",
            actor_principal_id="operator",
            run_id="run-a",
            attempt_id="attempt-b",
            change_id=second.attempt.capture_change_id,
            finalization=AttemptFinalization(
                attempt_id="attempt-b",
                evaluation_spec_digest=second.attempt.evaluation_spec_digest,
                binding_id=second.attempt.binding_id,
                effective_outcome="success",
                effective_code=None,
                captured_artifacts=(),
                envelope=envelope,
            ),
            expected_run_revision=second.revision.revision,
            expected_owner_revision=second.revision.owner_revision,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
        )
        authority.refresh_controller()
        provider = _CleanupOnlyProvider(ledger, actor_principal_id="operator")

        result = RunAttemptScheduler(authority, provider).advance(
            logical_trial_id=logical_trial_id,
            attempt_id="attempt-a",
        )

        self.assertEqual(result.action, "cleanup_only")
        self.assertEqual(result.attempt.attempt_id, "attempt-a")
        self.assertEqual(result.logical_transition.attempt_id, "attempt-a")
        self.assertEqual(result.logical_transition.to_state, "retrying")
        current = authority.refresh_controller().logical_transitions[-1]
        self.assertEqual(current.attempt_id, "attempt-b")


if __name__ == "__main__":
    unittest.main()
