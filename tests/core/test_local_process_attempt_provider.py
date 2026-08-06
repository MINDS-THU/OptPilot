from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_attempt_launcher import (
    ManagedLocalAttempt,
    RealmLocalAttemptLauncher,
)
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
    RealmProcessExecutionBinder,
)
from optpilot.realm.refs import canonical_json_bytes
from optpilot.run_attempt_heartbeat import RunAttemptHeartbeatCoordinator
from tests.core.test_realm_local_attempt_launcher import (
    _RetainedRuntimeFixture,
    _SimulatedParentCrash,
)


class _ControllableHeartbeat:
    """Delegate attachment while allowing a deterministic foreground failure."""

    def __init__(self, coordinator: RunAttemptHeartbeatCoordinator) -> None:
        self.coordinator = coordinator
        self.failure: Exception | None = None

    def attach_binding(self, binding) -> None:
        self.coordinator.attach_binding(binding)

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.coordinator.raise_if_failed()


@unittest.skipUnless(os.name == "posix", "local attempt process is POSIX-only")
class LocalProcessAttemptProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)

    def _provider(self, **supervisor_options):
        supervisor = LocalProcessSupervisor(
            self.fixture.root / "process-provider", **supervisor_options
        )
        launcher = RealmLocalAttemptLauncher(supervisor)
        binder = self.fixture.binder_for(launcher)
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        provider = LocalProcessAttemptProvider(
            self.fixture.ledger,
            binder,
            launcher,
            finalizer,
            actor_principal_id="operator",
        )
        return supervisor, launcher, binder, provider

    def _heartbeat(self) -> RunAttemptHeartbeatCoordinator:
        receipt = self.fixture.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            attempt_id=self.fixture.preparation.attempt.attempt_id,
        )
        coordinator = RunAttemptHeartbeatCoordinator(
            self.fixture.ledger,
            actor_principal_id="operator",
            receipt=receipt,
            interval_seconds=30.0,
        )
        coordinator.start()
        self.addCleanup(coordinator.stop)
        return coordinator

    @property
    def coordinates(self) -> dict[str, str]:
        return {
            "run_id": self.fixture.created.run.run_id,
            "attempt_id": self.fixture.preparation.attempt.attempt_id,
        }

    def _confirm(self, observation: LocalAttemptStarted | LocalAttemptTerminal):
        if isinstance(observation, LocalAttemptStarted):
            launch_token = observation.launch_token
            binding_id = observation.binding_id
            evidence_fingerprint = observation.evidence_fingerprint
            launch_request_digest = observation.launch_request_digest
        else:
            evidence = observation.evidence
            self.assertTrue(evidence.started)
            launch_token = evidence.launch_token
            binding_id = evidence.binding_id
            evidence_fingerprint = evidence.evidence_fingerprint
            launch_request_digest = evidence.launch_request_digest
        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.coordinates["run_id"]
        )
        return self.fixture.ledger.confirm_run_attempt_launch(
            operation_id="provider-test/attempt/confirm",
            actor_principal_id="operator",
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
            expected_run_revision=snapshot.run.current_revision,
            **self.coordinates,
            **self.fixture.controller_arguments(),
        )

    def _adopt(self, finalization):
        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.coordinates["run_id"]
        )
        attempt = self.fixture.ledger.read_run_attempt(
            actor_principal_id="operator", **self.coordinates
        )
        return self.fixture.ledger.adopt_run_attempt(
            operation_id="provider-test/attempt/adopt",
            actor_principal_id="operator",
            change_id=attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=snapshot.run.current_revision,
            expected_owner_revision=snapshot.revision.owner_revision,
            **self.coordinates,
            **self.fixture.controller_arguments(),
        )

    def _assert_path_free_error(
        self,
        error: LocalAttemptProviderError,
        *,
        code: str | None = None,
    ) -> None:
        self.assertIsInstance(error, LocalAttemptProviderError)
        if code is not None:
            self.assertEqual(error.code, code)
        rendered = repr((str(error), error.args, vars(error))).encode("utf-8")
        self.assertNotIn(str(self.fixture.root).encode("utf-8"), rendered)
        for forbidden in (
            b"argv",
            b"cwd",
            b"env",
            b"backend_token",
            b"provider_generation",
            b"terminal_proof",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def _durable_terminal(self):
        return self.fixture.ledger.read_run_attempt_terminal_evidence(
            actor_principal_id="operator", **self.coordinates
        )

    def test_fresh_attempt_is_path_free_and_cleanup_debt_is_retryable(self) -> None:
        supervisor, launcher, binder, provider = self._provider()
        heartbeat = self._heartbeat()
        observation = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertIsInstance(observation, (LocalAttemptStarted, LocalAttemptTerminal))
        self._confirm(observation)
        terminal = (
            observation
            if isinstance(observation, LocalAttemptTerminal)
            else provider.wait_terminal(timeout=10.0, **self.coordinates)
        )
        self.assertIsInstance(terminal, LocalAttemptTerminal)
        self.assertTrue(terminal.started)
        finalization = provider.finalize_terminal(
            terminal=terminal, **self.coordinates
        )
        self.assertEqual(finalization.effective_outcome, "success")
        heartbeat.stop()
        adoption = self._adopt(finalization)
        self.assertEqual(adoption.attempt.state, "terminal")

        private_root = str(self.fixture.root).encode("utf-8")
        public = canonical_json_bytes(
            {
                "terminal": terminal.evidence.to_dict(),
                "finalization": finalization.to_dict(),
            }
        )
        self.assertNotIn(private_root, public)
        self.assertNotIn(b"backend_token", public)
        self.assertNotIn(b"provider_generation", public)
        self.assertNotIn(b'"argv"', public)
        self.assertNotIn(b'"cwd"', public)
        self.assertNotIn(b'"env"', public)

        failure = ProcessExecutionResourceError(
            "private cleanup failed",
            (
                ProcessExecutionResourceFailure(
                    phase="terminal-cleanup",
                    resource_kind="volume",
                    logical_name="trial",
                    error=RuntimeError(str(self.fixture.root / "secret")),
                ),
            ),
        )
        with mock.patch.object(
            binder, "resume_authorized_cleanup", side_effect=failure
        ):
            pending = provider.resume_cleanup(**self.coordinates)
        self.assertEqual(
            pending,
            LocalAttemptCleanupResult(
                **self.coordinates,
                state="pending",
                code="provider_cleanup_pending",
            ),
        )
        self.assertNotIn(private_root, canonical_json_bytes(pending.__dict__))
        # Resource cleanup can commit before private launch retirement.  Exact
        # retry must finish redaction without needing the raw proof from memory.
        with mock.patch.object(
            launcher,
            "_retire_terminal",
            side_effect=OSError(str(self.fixture.root / "retirement-secret")),
        ):
            retirement_pending = provider.resume_cleanup(**self.coordinates)
        self.assertEqual(retirement_pending.state, "pending")
        self.assertEqual(provider.resume_cleanup(**self.coordinates).state, "cleaned")

        retained_provider_bytes = b"".join(
            path.read_bytes()
            for path in supervisor.root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(private_root, retained_provider_bytes)
        self.assertNotIn(b'"argv"', retained_provider_bytes)
        self.assertNotIn(b'"cwd"', retained_provider_bytes)
        self.assertNotIn(b'"env"', retained_provider_bytes)
        self.assertFalse(any((supervisor.root / "launches").iterdir()))

    def test_bound_passive_reservation_recovers_without_reserving_again(self) -> None:
        _supervisor, launcher, binder, _provider = self._provider()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        prepared._commit_reserved_launch(reservation)
        # Deliberately crash before control publication or start request.

        _restarted_supervisor, restarted_launcher, _restarted_binder, provider = (
            self._provider()
        )
        heartbeat = self._heartbeat()
        with mock.patch.object(
            restarted_launcher,
            "_reserve",
            side_effect=AssertionError("bound recovery must not reserve"),
        ):
            observation = provider.start_or_attach(
                heartbeat=heartbeat, **self.coordinates
            )
        self._confirm(observation)
        terminal = (
            observation
            if isinstance(observation, LocalAttemptTerminal)
            else provider.wait_terminal(timeout=10.0, **self.coordinates)
        )
        self.assertTrue(terminal.started)

    def test_terminalize_current_abandons_fresh_attempt_without_spawn(self) -> None:
        supervisor, _launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()

        with mock.patch.object(
            supervisor,
            "_spawn_wrapper",
            side_effect=AssertionError("terminalization must not spawn"),
        ) as spawn:
            terminal = provider.terminalize_current(
                heartbeat=heartbeat, **self.coordinates
            )

        self.assertFalse(spawn.called)
        self.assertFalse(terminal.started)
        self.assertEqual(terminal.disposition, "never_started")
        finalization = provider.finalize_terminal(
            terminal=terminal, **self.coordinates
        )
        self.assertEqual(finalization.effective_code, "worker_never_started")

    def test_bound_heartbeat_failure_before_compile_abandons_and_records(self) -> None:
        _supervisor, launcher, binder, _provider = self._provider()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        prepared._commit_reserved_launch(reservation)

        _s2, _l2, _b2, replacement = self._provider()
        heartbeat = _ControllableHeartbeat(self._heartbeat())
        heartbeat.failure = ValueError(str(self.fixture.root / "heartbeat-secret"))
        with self.assertRaises(LocalAttemptProviderError) as raised:
            replacement.start_or_attach(
                heartbeat=heartbeat, **self.coordinates  # type: ignore[arg-type]
            )
        self._assert_path_free_error(raised.exception)
        evidence = self._durable_terminal()
        self.assertFalse(evidence.started)
        self.assertEqual(evidence.disposition, "never_started")

    def test_start_intent_crash_replays_never_started_without_private_data(self) -> None:
        def crash(point: str) -> None:
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        _supervisor, _launcher, _binder, crashing = self._provider(
            fault_injector=crash
        )
        first_heartbeat = self._heartbeat()
        with self.assertRaises(_SimulatedParentCrash):
            crashing.start_or_attach(
                heartbeat=first_heartbeat, **self.coordinates
            )
        first_heartbeat.stop()

        _supervisor2, _launcher2, _binder2, recovered = self._provider()
        heartbeat = self._heartbeat()
        terminal = recovered.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertIsInstance(terminal, LocalAttemptTerminal)
        self.assertFalse(terminal.started)
        self.assertEqual(terminal.disposition, "never_started")
        finalization = recovered.finalize_terminal(
            terminal=terminal, **self.coordinates
        )
        self.assertEqual(finalization.effective_code, "worker_never_started")
        public = canonical_json_bytes(
            {
                "terminal": terminal.evidence.to_dict(),
                "finalization": finalization.to_dict(),
            }
        )
        self.assertNotIn(str(self.fixture.root).encode(), public)
        self.assertNotIn(b"backend_token", public)

    def test_coordinate_only_stop_abandons_passive_binding_without_recovery(self) -> None:
        _supervisor, launcher, binder, _provider = self._provider()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        prepared._commit_reserved_launch(reservation)

        _supervisor2, _launcher2, restarted_binder, replacement = self._provider()
        with mock.patch.object(
            restarted_binder,
            "recover",
            side_effect=AssertionError("stale stop must not reattach resources"),
        ):
            terminal = replacement.stop_and_wait_terminal(**self.coordinates)
        self.assertFalse(terminal.started)
        self.assertEqual(terminal.disposition, "never_started")

    def test_terminal_unbound_attempt_retires_crash_orphan_without_spawn(self) -> None:
        supervisor, launcher, binder, provider = self._provider()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        # Simulate process loss after the private passive reserve but before the
        # atomic Realm binding/launch receipt commit.

        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.coordinates["run_id"]
        )
        self.fixture.ledger.replace_run_controller(
            operation_id="provider-test/orphan/controller-replace",
            actor_principal_id="operator",
            run_id=snapshot.run.run_id,
            expected_controller_generation=snapshot.run.controller_generation,
            expected_controller_lease_id=snapshot.run.controller_lease_id,
            expected_controller_holder_id=snapshot.run.controller_holder_id,
            expected_controller_fencing_token=snapshot.run.controller_fencing_token,
            new_controller_holder_id="orphan-replacement-controller",
            controller_ttl_seconds=300,
        )
        replacement = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.coordinates["run_id"]
        )
        loss = self.fixture.ledger.reconcile_lost_run_attempt(
            operation_id="provider-test/orphan/reconcile",
            actor_principal_id="operator",
            expected_run_revision=replacement.revision.revision,
            expected_owner_revision=replacement.revision.owner_revision,
            controller_lease_id=replacement.run.controller_lease_id,
            controller_holder_id=replacement.run.controller_holder_id,
            controller_fencing_token=replacement.run.controller_fencing_token,
            **self.coordinates,
        )
        self.assertEqual(loss.attempt.state, "terminal")
        self.assertEqual(
            loss.attempt_transition.payload["binding_state"], "unbound"
        )

        cleanup = provider.resume_cleanup(**self.coordinates)
        self.assertEqual(cleanup.state, "cleaned")
        proof = supervisor.start_reserved(reservation).wait(timeout=1.0)
        self.assertEqual(proof.disposition, "never_started")
        self.assertFalse(any((supervisor.root / "launches").iterdir()))
        retained = b"".join(
            path.read_bytes()
            for path in supervisor.root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(str(self.fixture.root).encode("utf-8"), retained)
        self.assertNotIn(b'"argv"', retained)
        self.assertNotIn(b'"cwd"', retained)
        self.assertNotIn(b'"env"', retained)

    def test_heartbeat_failure_stops_live_writer_and_records_evidence(self) -> None:
        # Replace the fixture with a deliberately long-running evaluator.
        self.fixture = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(self.fixture.close)
        _supervisor, _launcher, _binder, provider = self._provider()
        coordinator = self._heartbeat()
        heartbeat = _ControllableHeartbeat(coordinator)
        observation = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates  # type: ignore[arg-type]
        )
        self.assertIsInstance(observation, LocalAttemptStarted)
        self._confirm(observation)
        heartbeat.failure = RuntimeError("lease heartbeat failed")
        with self.assertRaises(LocalAttemptProviderError) as raised:
            provider.wait_terminal(timeout=10.0, **self.coordinates)
        self._assert_path_free_error(raised.exception)
        evidence = self.fixture.ledger.read_run_attempt_terminal_evidence(
            actor_principal_id="operator", **self.coordinates
        )
        self.assertTrue(evidence.started)
        self.assertEqual(evidence.disposition, "killed")

    def test_compile_failure_retries_with_same_one_shot_heartbeat(self) -> None:
        _supervisor, launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        secret = self.fixture.root / "compile-secret"
        with mock.patch.object(
            launcher, "_compile", side_effect=ValueError(str(secret))
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(raised.exception)

        observation = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        terminal = (
            observation
            if isinstance(observation, LocalAttemptTerminal)
            else provider.wait_terminal(timeout=10.0, **self.coordinates)
        )
        self.assertTrue(terminal.started)

    def test_publish_failure_drains_and_exact_same_heartbeat_retry_converges(self) -> None:
        supervisor, launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        with mock.patch.object(
            launcher,
            "_publish",
            side_effect=OSError(str(self.fixture.root / "publish-secret")),
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(raised.exception)
        evidence = self._durable_terminal()
        self.assertFalse(evidence.started)
        self.assertEqual(evidence.disposition, "never_started")

        terminal = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertEqual(terminal, LocalAttemptTerminal(evidence=evidence))
        proof = supervisor.lookup_terminal_proof(
            launch_token=evidence.launch_token,
            binding_id=evidence.binding_id,
            evidence_fingerprint=evidence.evidence_fingerprint,
            launch_request_digest=evidence.launch_request_digest,
        )
        self.assertIsNotNone(proof)

    def test_failure_before_start_abandons_without_spawning(self) -> None:
        supervisor, launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        original = launcher._start_reserved
        calls = 0

        def fail_once(reservation):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(str(self.fixture.root / "before-start-secret"))
            return original(reservation)

        with mock.patch.object(launcher, "_start_reserved", side_effect=fail_once):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(raised.exception)
        evidence = self._durable_terminal()
        self.assertFalse(evidence.started)
        self.assertEqual(evidence.disposition, "never_started")
        self.assertIsNotNone(
            supervisor.lookup_terminal_proof(
                launch_token=evidence.launch_token,
                binding_id=evidence.binding_id,
                evidence_fingerprint=evidence.evidence_fingerprint,
                launch_request_digest=evidence.launch_request_digest,
            )
        )

    def test_start_response_failure_after_handshake_stops_without_respawn(self) -> None:
        self.fixture = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(self.fixture.close)
        supervisor, launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        original = launcher._start_reserved
        calls = 0

        def lose_started_response(reservation):
            nonlocal calls
            calls += 1
            process = original(reservation)
            if calls == 1:
                process.wait_started(timeout=5.0)
                raise OSError(str(self.fixture.root / "start-response-secret"))
            return process

        with mock.patch.object(
            launcher, "_start_reserved", side_effect=lose_started_response
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(raised.exception)
        evidence = self._durable_terminal()
        self.assertTrue(evidence.started)
        self.assertEqual(evidence.disposition, "killed")
        proof = supervisor.lookup_terminal_proof(
            launch_token=evidence.launch_token,
            binding_id=evidence.binding_id,
            evidence_fingerprint=evidence.evidence_fingerprint,
            launch_request_digest=evidence.launch_request_digest,
        )
        self.assertIsNotNone(proof)
        terminal = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertEqual(terminal.evidence, evidence)
        self.assertEqual(calls, 2)

    def test_attach_failure_after_spawn_drains_and_installs_terminal_retry(self) -> None:
        self.fixture = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(self.fixture.close)
        supervisor, launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        original = launcher._attach
        calls = 0

        def fail_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(str(self.fixture.root / "attach-secret"))
            return original(**kwargs)

        with mock.patch.object(launcher, "_attach", side_effect=fail_once):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(raised.exception)
        evidence = self._durable_terminal()
        self.assertIn(evidence.disposition, {"killed", "never_started"})
        self.assertIsNotNone(
            supervisor.lookup_terminal_proof(
                launch_token=evidence.launch_token,
                binding_id=evidence.binding_id,
                evidence_fingerprint=evidence.evidence_fingerprint,
                launch_request_digest=evidence.launch_request_digest,
            )
        )
        terminal = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertEqual(terminal.evidence, evidence)
        self.assertEqual(calls, 2)

    def test_blocking_wait_does_not_prevent_concurrent_stop(self) -> None:
        self.fixture = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(self.fixture.close)
        _supervisor, _launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        observation = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self.assertIsInstance(observation, LocalAttemptStarted)
        self._confirm(observation)

        entered = threading.Event()
        result: list[LocalAttemptTerminal] = []
        errors: list[BaseException] = []

        def wait() -> None:
            entered.set()
            try:
                result.append(
                    provider.wait_terminal(timeout=10.0, **self.coordinates)
                )
            except BaseException as error:  # pragma: no cover - assertion below
                errors.append(error)

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1.0))
        time.sleep(0.2)
        started_at = time.monotonic()
        stopped = provider.stop_and_wait_terminal(**self.coordinates)
        elapsed = time.monotonic() - started_at
        thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(stopped.disposition, "killed")
        self.assertEqual(result, [stopped])

    def test_prehandshake_wait_does_not_hold_coordinate_lock(self) -> None:
        self.fixture = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(self.fixture.close)
        _supervisor, _launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        entered = threading.Event()
        release = threading.Event()
        start_results: list[LocalAttemptStarted | LocalAttemptTerminal] = []
        stop_results: list[LocalAttemptTerminal] = []
        errors: list[BaseException] = []
        original = ManagedLocalAttempt.wait_started

        def blocked(handle, timeout=None):
            entered.set()
            release.wait(timeout=5.0)
            return original(handle, timeout=timeout)

        def start() -> None:
            try:
                start_results.append(
                    provider.start_or_attach(
                        heartbeat=heartbeat, **self.coordinates
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def stop() -> None:
            try:
                stop_results.append(
                    provider.stop_and_wait_terminal(**self.coordinates)
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch.object(ManagedLocalAttempt, "wait_started", blocked):
            start_thread = threading.Thread(target=start, daemon=True)
            start_thread.start()
            # Reaching the mocked handshake wait includes projection and
            # subprocess setup, which can be slow when the process-heavy test
            # suites run together.  The lock invariant is asserted by the
            # bounded stop join below, not by this setup deadline.
            self.assertTrue(entered.wait(10.0))
            stop_thread = threading.Thread(target=stop, daemon=True)
            stop_thread.start()
            stop_thread.join(3.0)
            stopped_without_release = not stop_thread.is_alive()
            release.set()
            start_thread.join(5.0)
            stop_thread.join(5.0)

        self.assertTrue(stopped_without_release)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(stop_results), 1)
        self.assertEqual(start_results, [stop_results[0]])

    def test_stale_stop_refuses_to_abandon_passive_canonical_running_attempt(self) -> None:
        supervisor, launcher, binder, _provider = self._provider()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        prepared._commit_reserved_launch(reservation)
        self._confirm(
            LocalAttemptStarted(
                **self.coordinates,
                binding_id=reservation.binding_id,
                launch_token=reservation.launch_token,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
            )
        )

        _s2, _l2, _b2, replacement = self._provider()
        with self.assertRaises(LocalAttemptProviderError) as raised:
            replacement.stop_and_wait_terminal(**self.coordinates)
        self._assert_path_free_error(
            raised.exception, code="provider_integrity_failure"
        )
        self.assertEqual(supervisor.reservation_state(reservation), "reserved")
        self.assertIsNone(
            supervisor.lookup_terminal_proof(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
            )
        )

    def test_nonrace_prepare_conflict_is_not_reinterpreted_as_recovery(self) -> None:
        _supervisor, _launcher, binder, provider = self._provider()
        heartbeat = self._heartbeat()
        with mock.patch.object(
            binder,
            "prepare_prepared",
            side_effect=RealmConflict("provider preflight rejected"),
        ), mock.patch.object(
            binder,
            "recover",
            side_effect=AssertionError("unbound conflict is not recovery"),
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.start_or_attach(
                    heartbeat=heartbeat, **self.coordinates
                )
        self._assert_path_free_error(
            raised.exception, code="provider_state_conflict"
        )

    def test_internal_value_errors_are_normalized_after_input_validation(self) -> None:
        _supervisor, _launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()

        with self.assertRaises(ValueError):
            provider.start_or_attach(
                run_id="",
                attempt_id=self.coordinates["attempt_id"],
                heartbeat=heartbeat,
            )
        with self.assertRaises(TypeError):
            provider.start_or_attach(
                heartbeat=object(),  # type: ignore[arg-type]
                **self.coordinates,
            )
        with self.assertRaises(TypeError):
            provider.finalize_terminal(
                terminal=object(),  # type: ignore[arg-type]
                **self.coordinates,
            )
        with self.assertRaises(ValueError):
            provider.wait_terminal(timeout=float("nan"), **self.coordinates)
        with self.assertRaises(ValueError):
            provider.stop_and_wait_terminal(
                grace_period=float("inf"), **self.coordinates
            )

        secret = self.fixture.root / "cleanup-value-secret"
        with mock.patch.object(
            provider._ledger, "read_run_attempt", side_effect=ValueError(str(secret))
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.resume_cleanup(**self.coordinates)
        self._assert_path_free_error(
            raised.exception, code="provider_operation_failed"
        )
        self.assertEqual(raised.exception.operation, "resume_cleanup")

    def test_finalize_internal_value_error_is_path_free(self) -> None:
        _supervisor, _launcher, _binder, provider = self._provider()
        heartbeat = self._heartbeat()
        observation = provider.start_or_attach(
            heartbeat=heartbeat, **self.coordinates
        )
        self._confirm(observation)
        terminal = (
            observation
            if isinstance(observation, LocalAttemptTerminal)
            else provider.wait_terminal(timeout=10.0, **self.coordinates)
        )
        secret = self.fixture.root / "finalizer-value-secret"
        with mock.patch.object(
            provider._finalizer, "finalize", side_effect=ValueError(str(secret))
        ):
            with self.assertRaises(LocalAttemptProviderError) as raised:
                provider.finalize_terminal(
                    terminal=terminal, **self.coordinates
                )
        self._assert_path_free_error(
            raised.exception, code="provider_operation_failed"
        )
        self.assertEqual(raised.exception.operation, "finalize_terminal")

    def test_constructor_rejects_split_launcher_verifiers(self) -> None:
        _supervisor, _launcher, binder, _provider = self._provider()
        other_supervisor = LocalProcessSupervisor(
            self.fixture.root / "other-process-provider"
        )
        other_launcher = RealmLocalAttemptLauncher(other_supervisor)
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        with self.assertRaisesRegex(ValueError, "exact reservation"):
            LocalProcessAttemptProvider(
                self.fixture.ledger,
                binder,
                other_launcher,
                finalizer,
                actor_principal_id="operator",
            )

    def test_constructor_rejects_wrong_bound_method_on_same_launcher(self) -> None:
        supervisor = LocalProcessSupervisor(
            self.fixture.root / "wrong-verifier-provider"
        )
        launcher = RealmLocalAttemptLauncher(supervisor)
        wrong = RealmProcessExecutionBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
            self.fixture.provider,
            launch_reservation_verifier=launcher.verify_launch_reservation,
            terminal_proof_verifier=launcher.expected_launch_request_digest,
        )
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        with self.assertRaisesRegex(ValueError, "exact reservation"):
            LocalProcessAttemptProvider(
                self.fixture.ledger,
                wrong,
                launcher,
                finalizer,
                actor_principal_id="operator",
            )


if __name__ == "__main__":
    unittest.main()
