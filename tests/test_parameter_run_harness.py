from __future__ import annotations

import copy
import inspect
import os
import types
import unittest
from contextlib import ExitStack
from dataclasses import replace
from typing import Any, Mapping
from unittest import mock

import optpilot.parameter_run_harness as harness_module
import tests.test_realm_local_attempt_launcher as launcher_test_module
from optpilot.attempts import AttemptEnvelope, OutputDeclaration
from optpilot.parameter_run_harness import (
    ParameterMethodObservation,
    ParameterObservationDelivery,
    ParameterRunHarness,
    ParameterRunHarnessResult,
    ParameterRunTerminationRequest,
)
from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_attempt_launcher import RealmLocalAttemptLauncher
from optpilot.realm.local_process_attempt_provider import (
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
    RunAttemptHeartbeatPolicy,
    RunAttemptScheduler,
)
from optpilot.run_authority import RetainedRunAuthority
from tests.test_realm_local_attempt_launcher import (
    _RetainedRuntimeFixture,
    _SimulatedParentCrash,
)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return copy.deepcopy(value)


def _normalizer_for(snapshot):
    contract = snapshot.evaluation_closure.environment_revision.candidate_contract
    validation = _thaw(contract["validation"])
    materialization = _thaw(contract["materialization"])
    method_id = snapshot.control.manifest.method_id

    def normalize(candidate: dict[str, object]) -> dict[str, object]:
        result = copy.deepcopy(candidate)
        result.setdefault("format", contract["format"])
        result.setdefault("spec", {})
        result.setdefault("lineage", {"parents": []})
        result.setdefault(
            "generator", {"method_id": method_id, "strategy": "external"}
        )
        result.setdefault("validation", copy.deepcopy(validation))
        result.setdefault("materialization", copy.deepcopy(materialization))
        return result

    return normalize


def _candidate(candidate_id: str, value: float) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
    }


class _ProviderProxy:
    def __init__(self, delegate: LocalProcessAttemptProvider, *, after_start=None):
        self.delegate = delegate
        self._ledger = delegate._ledger
        self._actor_principal_id = delegate._actor_principal_id
        self.after_start = after_start
        self.stop_calls = 0

    def start_or_attach(self, *, run_id: str, attempt_id: str, heartbeat):
        observation = self.delegate.start_or_attach(
            run_id=run_id, attempt_id=attempt_id, heartbeat=heartbeat
        )
        if self.after_start is not None:
            self.after_start(observation, heartbeat)
        return observation

    def wait_terminal(self, *, run_id: str, attempt_id: str, timeout=None):
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
        return self.delegate.resume_cleanup(run_id=run_id, attempt_id=attempt_id)


@unittest.skipUnless(os.name == "posix", "local attempt process is POSIX-only")
class ParameterRunHarnessTest(unittest.TestCase):
    def runtime(
        self,
        *,
        fresh: bool = False,
        max_retries: int = 0,
        evaluation_delay_seconds: float = 0.0,
    ) -> _RetainedRuntimeFixture:
        original_writer = launcher_test_module._write_package

        def write_package(root, *, method_protocol="batch"):
            study_path = original_writer(root, method_protocol=method_protocol)
            if max_retries:
                text = study_path.read_text(encoding="utf-8")
                study_path.write_text(
                    text.replace(
                        "execution:\n",
                        "execution:\n"
                        "  retry:\n"
                        f"    maxRetries: {max_retries}\n",
                        1,
                    ),
                    encoding="utf-8",
                )
            return study_path

        with ExitStack() as stack:
            if max_retries:
                stack.enter_context(
                    mock.patch.object(
                        launcher_test_module,
                        "_write_package",
                        side_effect=write_package,
                    )
                )
            if fresh:
                stack.enter_context(
                    mock.patch.object(
                        _RetainedRuntimeFixture, "_admit", return_value=None
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        RealmLedger,
                        "prepare_run_attempt",
                        autospec=True,
                        return_value=None,
                    )
                )
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
            candidate_normalizer=_normalizer_for(snapshot),
            normalizer_version=snapshot.control.manifest.normalizer_version,
        )

    def provider(
        self,
        fixture: _RetainedRuntimeFixture,
        *,
        provider_root: str = "harness-provider",
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
        authority: RetainedRunAuthority,
        provider,
        *,
        heartbeat_factory=None,
    ) -> RunAttemptScheduler:
        return RunAttemptScheduler(
            authority,
            provider,
            heartbeat_factory=heartbeat_factory,
            heartbeat_policy=RunAttemptHeartbeatPolicy(interval_seconds=30.0),
        )

    def harness(
        self,
        authority: RetainedRunAuthority,
        provider,
        *,
        propose,
        observe,
        terminate=None,
        heartbeat_factory=None,
    ) -> ParameterRunHarness:
        return ParameterRunHarness(
            authority,
            scheduler=self.scheduler(
                authority, provider, heartbeat_factory=heartbeat_factory
            ),
            propose=propose,
            observe=observe,
            terminate=terminate,
            attempt_ttl_seconds=60,
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
        evidence = (
            observation
            if isinstance(observation, LocalAttemptStarted)
            else observation.evidence
        )
        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        fixture.ledger.confirm_run_attempt_launch(
            operation_id="harness-test/manual-confirm",
            actor_principal_id="operator",
            run_id=fixture.created.run.run_id,
            attempt_id="attempt-a",
            launch_token=evidence.launch_token,
            binding_id=evidence.binding_id,
            evidence_fingerprint=evidence.evidence_fingerprint,
            launch_request_digest=evidence.launch_request_digest,
            expected_run_revision=snapshot.revision.revision,
            **fixture.controller_arguments(),
        )

    def test_method_observation_filters_operator_only_envelope_fields(self) -> None:
        envelope = AttemptEnvelope(
            attempt_id="attempt-filter",
            evaluation_spec_digest="sha256:" + "a" * 64,
            binding_id="binding-filter",
            outcome="failed",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {}, "metadata": {}},
            metric_values={"score": 1.5},
            constraint_results={"safe": False},
            output_declarations=(
                OutputDeclaration(
                    declaration_id="operator:secret",
                    name="secret",
                    path="secret.txt",
                ),
            ),
            event_summary={"secret": "event-summary-must-not-leak"},
            execution_metadata={"secret": "execution-metadata-must-not-leak"},
            error={
                "phase": "environment_evaluation",
                "type": "RuntimeError",
                "message": "public failure",
                "operator_details": "error-details-must-not-leak",
            },
        )

        filtered = ParameterMethodObservation.from_envelope(envelope)

        self.assertEqual(filtered.status, "failed")
        self.assertEqual(filtered.metric_values["score"], 1.5)
        self.assertEqual(filtered.constraint_results["safe"], False)
        self.assertEqual(
            dict(filtered.error),
            {
                "phase": "environment_evaluation",
                "type": "RuntimeError",
                "message": "public failure",
            },
        )
        payload = repr(filtered.to_dict())
        for forbidden in (
            "output_declarations",
            "execution-metadata-must-not-leak",
            "event-summary-must-not-leak",
            "error-details-must-not-leak",
        ):
            self.assertNotIn(forbidden, payload)

    def test_fresh_admission_runs_successfully_and_uses_typed_termination(self) -> None:
        fixture = self.runtime(fresh=True)
        _s, _l, _b, _f, provider = self.provider(fixture)
        authority = self.authority(fixture)
        proposals = 0
        deliveries: list[ParameterObservationDelivery] = []

        def propose(_request):
            nonlocal proposals
            proposals += 1
            return (
                [_candidate("candidate-fresh", 0.5)]
                if proposals == 1
                else []
            )

        result = self.harness(
            authority,
            provider,
            propose=propose,
            observe=deliveries.append,
        ).run()
        snapshot = authority.refresh_controller()

        self.assertEqual(result.proposals, 2)
        self.assertEqual(result.attempt_advances, 1)
        self.assertEqual(result.pending_cleanup_attempt_ids, ())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].outcome, "success")
        self.assertEqual(deliveries[0].observation.metric_values["score"], 0.5)
        self.assertIsInstance(
            result.termination_request, ParameterRunTerminationRequest
        )
        self.assertEqual(result.termination_request.reason, "method_completed")
        self.assertTrue(result.canonical_terminal)
        self.assertEqual(snapshot.run.state, "succeeded")

    def test_retry_recovers_never_started_attempt_then_runs_once(self) -> None:
        fixture = self.runtime(max_retries=1)
        crash_count = 0

        def crash(point: str) -> None:
            nonlocal crash_count
            crash_count += 1
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        _s1, _l1, _b1, _f1, crashing_provider = self.provider(
            fixture, fault_injector=crash
        )
        first_authority = self.authority(fixture)
        first = self.harness(
            first_authority,
            crashing_provider,
            propose=lambda _request: [],
            observe=lambda _delivery: self.fail("crash run must not deliver"),
        )
        with self.assertRaises(_SimulatedParentCrash):
            first.run()
        self.assertEqual(crash_count, 1)

        _s2, _l2, _b2, _f2, recovered_provider = self.provider(fixture)
        recovered_authority = self.authority(fixture)
        deliveries: list[ParameterObservationDelivery] = []
        result = self.harness(
            recovered_authority,
            recovered_provider,
            propose=lambda _request: [],
            observe=deliveries.append,
        ).run()
        snapshot = recovered_authority.refresh_controller()

        self.assertEqual(result.attempt_advances, 2)
        self.assertEqual(len(snapshot.attempts), 2)
        self.assertEqual(snapshot.attempts[0].code, "worker_never_started")
        self.assertEqual(snapshot.attempts[1].outcome, "success")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].attempt_count, 2)
        observation = snapshot.observations[0]
        self.assertEqual(observation.envelope.event_summary["evaluation_count"], 1)

    def test_hydrated_prepared_and_running_attempts_are_normal_work(self) -> None:
        with self.subTest(state="prepared"):
            fixture = self.runtime()
            _s, _l, _b, _f, provider = self.provider(fixture)
            authority = self.authority(fixture)
            result = self.harness(
                authority,
                provider,
                propose=lambda _request: [],
                observe=lambda _delivery: None,
            ).run()
            self.assertEqual(result.attempt_advances, 1)
            self.assertTrue(result.canonical_terminal)

        with self.subTest(state="running"):
            fixture = self.runtime(evaluation_delay_seconds=0.25)
            _s1, _l1, _b1, _f1, provider1 = self.provider(fixture)
            heartbeat = self.heartbeat(fixture)
            observation = provider1.start_or_attach(
                heartbeat=heartbeat, **self.coordinates(fixture)
            )
            self.confirm(fixture, observation)
            heartbeat.stop()
            _s2, _l2, _b2, _f2, provider2 = self.provider(fixture)
            authority = self.authority(fixture)

            result = self.harness(
                authority,
                provider2,
                propose=lambda _request: [],
                observe=lambda _delivery: None,
            ).run()
            self.assertEqual(result.attempt_advances, 1)
            self.assertTrue(result.canonical_terminal)

    def test_old_generation_attempt_is_reconciled_without_special_handoff(self) -> None:
        fixture = self.runtime()
        snapshot = fixture.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=fixture.created.run.run_id
        )
        fixture.ledger.replace_run_controller(
            operation_id="harness-test/controller/replace",
            actor_principal_id="operator",
            run_id=snapshot.run.run_id,
            expected_controller_generation=snapshot.run.controller_generation,
            expected_controller_lease_id=snapshot.run.controller_lease_id,
            expected_controller_holder_id=snapshot.run.controller_holder_id,
            expected_controller_fencing_token=snapshot.run.controller_fencing_token,
            new_controller_holder_id="replacement-controller",
            controller_ttl_seconds=300,
        )
        _s, _l, _b, _f, provider = self.provider(fixture)
        authority = self.authority(fixture)
        deliveries: list[ParameterObservationDelivery] = []

        result = self.harness(
            authority,
            provider,
            propose=lambda _request: [],
            observe=deliveries.append,
        ).run()
        final = authority.refresh_controller()

        self.assertEqual(result.attempt_advances, 1)
        self.assertTrue(result.canonical_terminal)
        self.assertEqual(final.attempts[0].code, "attempt_authority_lost")
        self.assertEqual(deliveries[0].outcome, "failed")
        self.assertIsNone(deliveries[0].observation)

    def test_crash_after_physical_start_restarts_without_duplicate_evaluation(
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

        crashing_provider = _ProviderProxy(
            provider1, after_start=crash_after_start
        )
        first_authority = self.authority(fixture)
        with self.assertRaises(_SimulatedParentCrash):
            self.harness(
                first_authority,
                crashing_provider,
                propose=lambda _request: [],
                observe=lambda _delivery: self.fail("crash must not deliver"),
                heartbeat_factory=heartbeat_factory,
            ).run()
        self.assertEqual(crashing_provider.stop_calls, 0)
        self.assertFalse(heartbeats[0].stopped)
        heartbeats[0].stop()

        _s2, _l2, _b2, _f2, provider2 = self.provider(fixture)
        recovered_authority = self.authority(fixture)
        deliveries: list[ParameterObservationDelivery] = []
        result = self.harness(
            recovered_authority,
            provider2,
            propose=lambda _request: [],
            observe=deliveries.append,
        ).run()
        snapshot = recovered_authority.refresh_controller()

        self.assertEqual(result.attempt_advances, 1)
        self.assertEqual(len(snapshot.attempts), 1)
        self.assertEqual(len(snapshot.observations), 1)
        self.assertEqual(
            snapshot.observations[0].envelope.event_summary["evaluation_count"],
            1,
        )
        self.assertEqual(len(deliveries), 1)

    def test_terminal_cleanup_pending_is_reported_and_retried_once_per_run(
        self,
    ) -> None:
        fixture = self.runtime(fresh=True)
        _s, _l, binder, _f, provider = self.provider(fixture)
        authority = self.authority(fixture)
        proposal_count = 0

        def propose(_request):
            nonlocal proposal_count
            proposal_count += 1
            return (
                [_candidate("candidate-cleanup", 0.25)]
                if proposal_count == 1
                else []
            )

        harness = self.harness(
            authority,
            provider,
            propose=propose,
            observe=lambda _delivery: None,
        )
        failure = ProcessExecutionResourceError(
            "private cleanup failure",
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
        ) as cleanup:
            first = harness.run()
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(first.attempt_advances, 1)
        self.assertEqual(len(first.pending_cleanup_attempt_ids), 1)
        pending_id = first.pending_cleanup_attempt_ids[0]
        self.assertEqual(first.state.run_status, "succeeded")

        second = harness.run()
        self.assertEqual(second.attempt_advances, 1)
        self.assertEqual(second.pending_cleanup_attempt_ids, ())
        self.assertEqual(second.state.run_status, "succeeded")
        self.assertEqual(
            authority.refresh_controller().attempts[0].attempt_id, pending_id
        )

    def test_hydrated_harness_redelivers_same_stable_delivery_id(self) -> None:
        fixture = self.runtime(fresh=True)
        _s1, _l1, _b1, _f1, provider1 = self.provider(fixture)
        authority1 = self.authority(fixture)
        proposal_count = 0
        first_deliveries: list[ParameterObservationDelivery] = []

        def propose(_request):
            nonlocal proposal_count
            proposal_count += 1
            return (
                [_candidate("candidate-delivery", 0.75)]
                if proposal_count == 1
                else []
            )

        self.harness(
            authority1,
            provider1,
            propose=propose,
            observe=first_deliveries.append,
        ).run()

        _s2, _l2, _b2, _f2, provider2 = self.provider(fixture)
        authority2 = self.authority(fixture)
        replayed: list[ParameterObservationDelivery] = []
        self.harness(
            authority2,
            provider2,
            propose=lambda _request: self.fail("terminal run must not propose"),
            observe=replayed.append,
        ).run()

        self.assertEqual(len(first_deliveries), 1)
        self.assertEqual(len(replayed), 1)
        self.assertEqual(
            replayed[0].delivery_id, first_deliveries[0].delivery_id
        )
        self.assertEqual(replayed[0], first_deliveries[0])

    def test_impossible_multiple_live_attempts_fail_closed(self) -> None:
        fixture = self.runtime()
        authority = self.authority(fixture)
        _s, _l, _b, _f, provider = self.provider(fixture)
        harness = self.harness(
            authority,
            provider,
            propose=lambda _request: [],
            observe=lambda _delivery: None,
        )
        snapshot = authority.refresh_controller()
        existing = snapshot.attempts[0]
        forged = replace(
            existing,
            attempt_id="forged-second-live-attempt",
            binding_id="forged-second-binding",
            launch_token="forged-second-launch",
            attempt_lease_id="forged-second-lease",
            capture_change_id="forged-second-change",
            attempt_index=2,
        )
        inconsistent = types.SimpleNamespace(
            logical_trials=snapshot.logical_trials,
            attempts=(existing, forged),
            control=snapshot.control,
        )

        with self.assertRaisesRegex(RuntimeError, "multiple nonterminal"):
            harness._next_active_attempt(inconsistent)

    def test_harness_api_and_result_are_path_free(self) -> None:
        signature = inspect.signature(ParameterRunHarness).parameters
        self.assertEqual(
            tuple(signature),
            (
                "authority",
                "scheduler",
                "propose",
                "observe",
                "terminate",
                "identity_source",
                "attempt_ttl_seconds",
            ),
        )
        self.assertFalse(hasattr(harness_module, "ParameterAttemptRequest"))
        self.assertFalse(hasattr(harness_module, "BindingFactory"))
        self.assertFalse(hasattr(harness_module, "SchedulerFactory"))
        self.assertFalse(
            hasattr(harness_module, "ParameterRunHarnessRecoveryRequired")
        )
        self.assertEqual(
            tuple(ParameterRunHarnessResult.__dataclass_fields__),
            (
                "state",
                "termination_request",
                "proposals",
                "attempt_advances",
                "pending_cleanup_attempt_ids",
                "delivered_logical_trials",
            ),
        )

        fixture = self.runtime(fresh=True)
        _s, _l, _b, _f, provider = self.provider(fixture)
        authority = self.authority(fixture)
        result = self.harness(
            authority,
            provider,
            propose=lambda _request: [],
            observe=lambda _delivery: None,
            terminate=lambda _authority, _request: None,
        ).run()
        public = canonical_json_bytes(
            {
                "run_id": result.state.run_id,
                "run_revision": result.state.run_revision,
                "run_status": result.state.run_status,
                "proposals": result.proposals,
                "attempt_advances": result.attempt_advances,
                "pending_cleanup_attempt_ids": list(
                    result.pending_cleanup_attempt_ids
                ),
                "delivered_logical_trials": list(
                    result.delivered_logical_trials
                ),
                "termination_reason": (
                    None
                    if result.termination_request is None
                    else result.termination_request.reason
                ),
            }
        )
        self.assertNotIn(str(fixture.root).encode(), public)
        for private in (
            b"workspace",
            b"binding",
            b"backend_token",
            b"operation_id",
            b"terminal_proof",
        ):
            self.assertNotIn(private, public)


if __name__ == "__main__":
    unittest.main()
