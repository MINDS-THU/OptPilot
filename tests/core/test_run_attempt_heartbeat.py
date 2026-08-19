from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerChangeHeartbeatReceipt
from optpilot.realm.run_attempt_records import (
    validate_run_attempt_heartbeat_expiry_chain,
)
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.run_attempt_heartbeat import (
    RunAttemptHeartbeatCoordinator,
    RunAttemptHeartbeatError,
    RunAttemptHeartbeatStateError,
)
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class _RecordingHeartbeatLedger:
    def __init__(self, *, fail_phase: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_phase = fail_phase
        self.failure: BaseException | None = None
        self.phase_entered = threading.Event()
        # This is deliberately not part of the typed heartbeat calls.  Tests
        # can advance it to model an unrelated append-only owner commit.
        self.unrelated_owner_revision = 0

    def heartbeat_lease(self, **arguments):
        lease = arguments["lease_id"]
        phase = "controller" if "controller" in arguments["operation_id"] else "attempt"
        self.calls.append((phase, arguments["operation_id"], arguments["actor_principal_id"]))
        if self.fail_phase == phase:
            self.phase_entered.set()
            raise self.failure or RuntimeError(f"{phase} failed")
        source = arguments.pop("_source", None)
        if source is not None:
            raise AssertionError("unexpected source override")
        # The concrete record is supplied by the coordinator's identity facts.
        current = self._lease_by_id[lease]
        increment = 100.0 if phase == "controller" else 50.0
        updated = replace(
            current,
            heartbeat_revision=current.heartbeat_revision + 1,
            expires_at=current.expires_at + increment,
            updated_at=current.updated_at + 0.001,
        )
        self._lease_by_id[lease] = updated
        return updated

    def heartbeat_owner_change(self, **arguments):
        self.calls.append(
            (
                "capture",
                arguments["operation_id"],
                arguments["actor_principal_id"],
            )
        )
        self.phase_entered.set()
        if self.fail_phase == "capture":
            raise self.failure or RuntimeError("capture failed")
        current_change = self._change
        current_lease = self._lease_by_id[arguments["retention_lease_id"]]
        expires_at = current_change.expires_at + 25.0
        change = replace(current_change, expires_at=expires_at)
        lease = replace(
            current_lease,
            heartbeat_revision=current_lease.heartbeat_revision + 1,
            expires_at=expires_at,
            updated_at=current_lease.updated_at + 0.001,
        )
        self._change = change
        self._lease_by_id[lease.lease_id] = lease
        return OwnerChangeHeartbeatReceipt(change=change, retention_lease=lease)

    def anchor(self, receipt) -> None:
        self._lease_by_id = {
            receipt.controller_lease.lease_id: receipt.controller_lease,
            receipt.attempt_lease.lease_id: receipt.attempt_lease,
            receipt.capture_retention_lease.lease_id: receipt.capture_retention_lease,
        }
        self._change = receipt.capture_change


class _RecordingBinding:
    def __init__(self, receipt, calls: list[tuple[str, str, str]]) -> None:
        self.receipt = SimpleNamespace(
            binding=SimpleNamespace(
                run_id=receipt.run.run_id,
                attempt_id=receipt.attempt.attempt_id,
            )
        )
        self.calls = calls

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        del ttl_seconds
        self.calls.append(("binding", operation_id, ""))


class _RecordingPreparedBinding:
    """Provider resources before their atomic binding/launch-intent commit."""

    def __init__(self, receipt, calls: list[tuple[str, str, str]]) -> None:
        self.run_id = receipt.run.run_id
        self.attempt_id = receipt.attempt.attempt_id
        self.calls = calls

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        del ttl_seconds
        self.calls.append(("binding", operation_id, ""))


class _ConcurrentControllerRenewalLedger:
    """The real ledger, plus one foreign renewal of the shared controller.

    A run has exactly one controller lease and every live attempt renews it,
    as does the run controller watchdog.  This stands in for whichever of
    them renews that shared lease in the window between a supervisor's own
    controller renewal and the child renewal that follows it, without
    needing two live attempts or a loaded machine to hit the window.
    """

    def __init__(
        self,
        ledger,
        *,
        controller_lease,
        actor_principal_id: str,
        ttl_seconds: float,
    ) -> None:
        self._ledger = ledger
        self._controller_lease = controller_lease
        self._actor_principal_id = actor_principal_id
        self._ttl_seconds = ttl_seconds
        self.foreign_renewals: list[float] = []

    def heartbeat_lease(self, **arguments):
        if "/attempt" in arguments["operation_id"]:
            foreign = self._ledger.heartbeat_lease(
                operation_id=(
                    "concurrent-controller-renewal/"
                    f"{len(self.foreign_renewals) + 1:016d}"
                ),
                actor_principal_id=self._actor_principal_id,
                lease_id=self._controller_lease.lease_id,
                holder_id=self._controller_lease.holder_id,
                fencing_token=self._controller_lease.fencing_token,
                ttl_seconds=self._ttl_seconds,
            )
            self.foreign_renewals.append(foreign.expires_at)
        return self._ledger.heartbeat_lease(**arguments)

    def heartbeat_owner_change(self, **arguments):
        return self._ledger.heartbeat_owner_change(**arguments)


class RunAttemptHeartbeatCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="heartbeat/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="heartbeat/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, closure_bindings, source_owner_id, source_owner_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="heartbeat",
            )
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=1)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, closure_bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="heartbeat/run/create",
            actor_principal_id="operator",
            controller_holder_id="heartbeat-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="heartbeat-run",
            owner_id="heartbeat-run-owner",
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            candidates=(
                CandidateAdmission(
                    "candidate-a",
                    envelope,
                    lineage={"parents": []},
                    generator={"method_id": "method-a"},
                ),
            ),
            logical_trials=(
                LogicalTrialAdmission(
                    "trial-a", "candidate-a", seed=None, repetition_index=0
                ),
            ),
        )
        admission_change = self.ledger.begin_owner_change(
            operation_id="heartbeat/admission/begin",
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id="heartbeat/admission/commit",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            change_id=admission_change.change_id,
            plan=plan,
        )
        self.preparation = self.ledger.prepare_run_attempt(
            operation_id="heartbeat/attempt/prepare",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
            expected_run_revision=1,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            attempt_ttl_seconds=1.0,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def fake(self, *, fail_phase: str | None = None) -> _RecordingHeartbeatLedger:
        result = _RecordingHeartbeatLedger(fail_phase=fail_phase)
        result.anchor(self.preparation)
        return result

    def coordinator(self, ledger, **arguments) -> RunAttemptHeartbeatCoordinator:
        return RunAttemptHeartbeatCoordinator(
            ledger,
            actor_principal_id=arguments.pop("actor_principal_id", "operator"),
            receipt=self.preparation,
            ttl_seconds=10,
            interval_seconds=60,
            session_id=arguments.pop("session_id", "test-session"),
            **arguments,
        )

    def test_one_round_uses_parent_to_child_order_and_updates_receipt(self) -> None:
        ledger = self.fake()
        binding = _RecordingBinding(self.preparation, ledger.calls)
        coordinator = self.coordinator(ledger, binding=binding)

        receipt = coordinator.heartbeat_once()

        self.assertEqual(
            [item[0] for item in ledger.calls],
            ["controller", "attempt", "capture", "binding"],
        )
        self.assertGreater(
            receipt.controller_lease.heartbeat_revision,
            self.preparation.controller_lease.heartbeat_revision,
        )
        self.assertGreater(
            receipt.attempt_lease.heartbeat_revision,
            self.preparation.attempt_lease.heartbeat_revision,
        )
        self.assertGreater(
            receipt.capture_retention_lease.heartbeat_revision,
            self.preparation.capture_retention_lease.heartbeat_revision,
        )
        self.assertEqual(
            receipt.capture_change.expires_at,
            receipt.capture_retention_lease.expires_at,
        )
        self.assertEqual(coordinator.completed_rounds, 1)

    def test_precommit_provider_resources_are_the_single_heartbeat_attachment(
        self,
    ) -> None:
        ledger = self.fake()
        prepared = _RecordingPreparedBinding(self.preparation, ledger.calls)
        coordinator = self.coordinator(ledger)

        coordinator.attach_binding(prepared)
        coordinator.heartbeat_once()

        self.assertEqual(
            [item[0] for item in ledger.calls],
            ["controller", "attempt", "capture", "binding"],
        )
        with self.assertRaises(RunAttemptHeartbeatStateError):
            coordinator.attach_binding(prepared)

    def test_partial_failure_is_retained_and_propagated(self) -> None:
        ledger = self.fake(fail_phase="attempt")
        coordinator = self.coordinator(ledger)

        with self.assertRaisesRegex(RunAttemptHeartbeatError, "during attempt"):
            coordinator.heartbeat_once()

        self.assertEqual([item[0] for item in ledger.calls], ["controller", "attempt"])
        self.assertGreater(
            coordinator.receipt.controller_lease.heartbeat_revision,
            self.preparation.controller_lease.heartbeat_revision,
        )
        self.assertEqual(
            coordinator.receipt.attempt_lease,
            self.preparation.attempt_lease,
        )
        with self.assertRaisesRegex(RunAttemptHeartbeatError, "during attempt"):
            coordinator.raise_if_failed()
        with self.assertRaises(RunAttemptHeartbeatStateError):
            coordinator.heartbeat_once()

    def test_failure_message_does_not_expose_cause_paths(self) -> None:
        ledger = self.fake(fail_phase="attempt")
        cause = RuntimeError("failed below /private/realm/attempt-a")
        ledger.failure = cause
        coordinator = self.coordinator(ledger)

        with self.assertRaises(RunAttemptHeartbeatError) as raised:
            coordinator.heartbeat_once()

        self.assertNotIn("/private/realm", str(raised.exception))
        self.assertIn("RuntimeError", str(raised.exception))
        self.assertIs(raised.exception.failure.cause, cause)

    def test_binding_can_be_attached_once_after_prepare(self) -> None:
        ledger = self.fake()
        coordinator = self.coordinator(ledger)
        coordinator.heartbeat_once()
        binding = _RecordingBinding(self.preparation, ledger.calls)

        coordinator.attach_binding(binding)
        coordinator.heartbeat_once()

        self.assertEqual([item[0] for item in ledger.calls].count("binding"), 1)
        with self.assertRaisesRegex(
            RunAttemptHeartbeatStateError, "already attached"
        ):
            coordinator.attach_binding(binding)

    def test_operation_ids_are_unique_by_actor_session_and_round(self) -> None:
        ledger_a = self.fake()
        first = self.coordinator(ledger_a, session_id="session-a")
        first.heartbeat_once()
        first.heartbeat_once()
        ledger_b = self.fake()
        second = self.coordinator(
            ledger_b,
            actor_principal_id="other-operator",
            session_id="session-a",
        )
        second.heartbeat_once()

        first_ids = [item[1] for item in ledger_a.calls]
        second_ids = [item[1] for item in ledger_b.calls]
        self.assertEqual(len(first_ids), len(set(first_ids)))
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assertTrue(all("session-a" in item for item in first_ids + second_ids))

    def test_unrelated_owner_revision_drift_is_not_a_coordinator_input(self) -> None:
        ledger = self.fake()
        coordinator = self.coordinator(ledger)
        first = coordinator.heartbeat_once()

        ledger.unrelated_owner_revision += 1
        second = coordinator.heartbeat_once()

        self.assertEqual(coordinator.completed_rounds, 2)
        self.assertEqual(
            second.capture_change.base_owner_revision,
            first.capture_change.base_owner_revision,
        )
        self.assertGreater(
            second.capture_retention_lease.heartbeat_revision,
            first.capture_retention_lease.heartbeat_revision,
        )

    def test_recovery_authority_receipt_keeps_its_receipt_shape(self) -> None:
        authority = self.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=self.preparation.run.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )
        ledger = _RecordingHeartbeatLedger()
        ledger.anchor(authority)
        coordinator = RunAttemptHeartbeatCoordinator(
            ledger,
            actor_principal_id="operator",
            receipt=authority,
            ttl_seconds=10,
            interval_seconds=60,
            session_id="recovery-authority",
        )

        updated = coordinator.heartbeat_once()

        self.assertIs(type(updated), type(authority))
        self.assertEqual(updated.run, authority.run)
        self.assertEqual(updated.attempt, authority.attempt)
        self.assertGreater(
            updated.capture_retention_lease.heartbeat_revision,
            authority.capture_retention_lease.heartbeat_revision,
        )

    def test_background_failure_surfaces_and_stop_is_idempotent(self) -> None:
        ledger = self.fake(fail_phase="capture")
        coordinator = self.coordinator(ledger).start()
        self.assertTrue(ledger.phase_entered.wait(2))

        coordinator.stop()
        coordinator.stop()

        self.assertTrue(coordinator.stopped)
        with self.assertRaisesRegex(RunAttemptHeartbeatError, "during capture"):
            coordinator.raise_if_failed()

    def test_stop_fences_future_heartbeats(self) -> None:
        ledger = self.fake()
        coordinator = self.coordinator(ledger).start()
        self.assertTrue(ledger.phase_entered.wait(2))

        coordinator.stop()
        call_count = len(ledger.calls)
        coordinator.stop()

        self.assertEqual(len(ledger.calls), call_count)
        with self.assertRaises(RunAttemptHeartbeatStateError):
            coordinator.heartbeat_once()
        self.assertEqual(len(ledger.calls), call_count)

    def test_real_ledger_renews_capture_change_and_retention_together(self) -> None:
        initial_expiry = self.preparation.capture_change.expires_at
        coordinator = RunAttemptHeartbeatCoordinator(
            self.ledger,
            actor_principal_id="operator",
            receipt=self.preparation,
            ttl_seconds=5,
            interval_seconds=60,
            session_id="realm-integration",
        )

        receipt = coordinator.heartbeat_once()

        self.assertGreater(receipt.capture_change.expires_at, initial_expiry)
        self.assertEqual(
            receipt.capture_change.expires_at,
            receipt.capture_retention_lease.expires_at,
        )
        recovered = self.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=receipt.run.run_id,
            attempt_id=receipt.attempt.attempt_id,
        )
        self.assertEqual(recovered.capture_change, receipt.capture_change)
        self.assertEqual(
            recovered.capture_retention_lease,
            receipt.capture_retention_lease,
        )

    def test_concurrent_controller_renewal_does_not_break_a_round(self) -> None:
        """A shared-controller renewal mid-round is routine, not a failure.

        The supervisor renews the controller lease, caches that record, and
        then renews its attempt lease.  The ledger clamps the attempt lease
        to the controller's *current* expiry, which a concurrent renewal has
        already moved past the cached record.  Comparing the two across that
        window used to abort the round -- and, through the scheduler, killed
        the run with an opaque provider error.
        """

        ledger = _ConcurrentControllerRenewalLedger(
            self.ledger,
            controller_lease=self.preparation.controller_lease,
            actor_principal_id="operator",
            ttl_seconds=10,
        )
        coordinator = RunAttemptHeartbeatCoordinator(
            ledger,
            actor_principal_id="operator",
            receipt=self.preparation,
            ttl_seconds=10,
            interval_seconds=60,
            session_id="concurrent-controller",
        )

        receipt = coordinator.heartbeat_once()

        self.assertEqual(len(ledger.foreign_renewals), 1)
        # The window really did open: the attempt lease this round renewed
        # outlives the controller record the same round cached one step
        # earlier.  Both facts are current; neither is authoritative about
        # the other.
        self.assertGreater(
            receipt.attempt_lease.expires_at,
            receipt.controller_lease.expires_at,
        )
        self.assertEqual(coordinator.completed_rounds, 1)
        self.assertIsNone(coordinator.failure)

        # One coherent read still orders the whole chain parent-first.
        recovered = self.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=receipt.run.run_id,
            attempt_id=receipt.attempt.attempt_id,
        )
        self.assertLessEqual(
            recovered.attempt_lease.expires_at,
            recovered.controller_lease.expires_at,
        )

    def test_background_rounds_survive_concurrent_controller_renewal(self) -> None:
        ledger = _ConcurrentControllerRenewalLedger(
            self.ledger,
            controller_lease=self.preparation.controller_lease,
            actor_principal_id="operator",
            ttl_seconds=10,
        )
        coordinator = RunAttemptHeartbeatCoordinator(
            ledger,
            actor_principal_id="operator",
            receipt=self.preparation,
            ttl_seconds=10,
            interval_seconds=0.01,
            session_id="concurrent-background",
        ).start()
        self.addCleanup(coordinator.stop)

        deadline = time.monotonic() + 5.0
        while coordinator.completed_rounds < 3 and coordinator.failure is None:
            if time.monotonic() >= deadline:
                self.fail("background heartbeat did not complete three rounds")
            time.sleep(0.01)

        coordinator.stop()
        coordinator.raise_if_failed()
        self.assertGreaterEqual(coordinator.completed_rounds, 3)

    def test_coherently_read_chain_still_requires_parent_first_expiry(self) -> None:
        """The ordering check survives where it is actually meaningful."""

        validate_run_attempt_heartbeat_expiry_chain(
            controller_lease=self.preparation.controller_lease,
            attempt_lease=self.preparation.attempt_lease,
            capture_retention_lease=self.preparation.capture_retention_lease,
        )

        outlives_controller = replace(
            self.preparation.attempt_lease,
            expires_at=self.preparation.controller_lease.expires_at + 1.0,
        )
        with self.assertRaisesRegex(ValueError, "expiry chain"):
            validate_run_attempt_heartbeat_expiry_chain(
                controller_lease=self.preparation.controller_lease,
                attempt_lease=outlives_controller,
                capture_retention_lease=self.preparation.capture_retention_lease,
            )

        outlives_attempt = replace(
            self.preparation.capture_retention_lease,
            expires_at=self.preparation.attempt_lease.expires_at + 1.0,
        )
        with self.assertRaisesRegex(ValueError, "expiry chain"):
            validate_run_attempt_heartbeat_expiry_chain(
                controller_lease=self.preparation.controller_lease,
                attempt_lease=self.preparation.attempt_lease,
                capture_retention_lease=outlives_attempt,
            )


if __name__ == "__main__":
    unittest.main()
