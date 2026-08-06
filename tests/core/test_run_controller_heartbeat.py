from __future__ import annotations

import math
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.leases import LeaseRecord, LeaseState
from optpilot.run_controller_heartbeat import (
    RunControllerHeartbeatCoordinator,
    RunControllerHeartbeatError,
    RunControllerHeartbeatStateError,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class _CrashCut(BaseException):
    pass


def _controller_lease(
    *,
    run_id: str = "run-a",
    lease_id: str = "controller-a",
    holder_id: str = "holder-a",
    fencing_token: int = 1,
    generation: int = 1,
) -> LeaseRecord:
    now = time.time()
    return LeaseRecord(
        lease_id=lease_id,
        owner_id="run-owner-a",
        parent_lease_id=None,
        lease_kind="run-controller",
        audience="realm-ledger",
        holder_id=holder_id,
        scope_key=f"run:{run_id}",
        fencing_token=fencing_token,
        heartbeat_revision=0,
        state=LeaseState.ACTIVE,
        expires_at=now + 100.0,
        created_at=now - 10.0,
        updated_at=now - 1.0,
        metadata={"run_id": run_id, "controller_generation": generation},
    )


class _RecordingLedger:
    def __init__(
        self,
        lease: LeaseRecord,
        calls: list[tuple[str, str, float]],
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.lease = lease
        self.calls = calls
        self.failure = failure
        self.entered = threading.Event()

    def heartbeat_lease(self, **arguments: object) -> LeaseRecord:
        operation_id = arguments["operation_id"]
        ttl_seconds = arguments["ttl_seconds"]
        assert isinstance(operation_id, str)
        assert isinstance(ttl_seconds, float)
        self.calls.append(("controller", operation_id, ttl_seconds))
        self.entered.set()
        if self.failure is not None:
            raise self.failure
        self.assert_identity(arguments)
        self.lease = replace(
            self.lease,
            heartbeat_revision=self.lease.heartbeat_revision + 1,
            expires_at=self.lease.expires_at + ttl_seconds,
            updated_at=self.lease.updated_at + 1.0,
        )
        return self.lease

    def assert_identity(self, arguments: dict[str, object]) -> None:
        if arguments["lease_id"] != self.lease.lease_id:
            raise AssertionError("wrong lease")
        if arguments["holder_id"] != self.lease.holder_id:
            raise AssertionError("wrong holder")
        if arguments["fencing_token"] != self.lease.fencing_token:
            raise AssertionError("wrong fence")


class _RecordingTarget:
    def __init__(
        self,
        name: str,
        calls: list[tuple[str, str, float]],
        *,
        failure: BaseException | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.failure = failure
        self.release = release
        self.entered = threading.Event()

    def heartbeat(self, *, operation_id: str, ttl_seconds: float) -> None:
        self.calls.append((self.name, operation_id, ttl_seconds))
        self.entered.set()
        if self.release is not None:
            if not self.release.wait(2):
                raise AssertionError("test did not release blocked target")
        if self.failure is not None:
            raise self.failure


class RunControllerHeartbeatCoordinatorTest(unittest.TestCase):
    def coordinator(
        self,
        *,
        run_id: str = "run-a",
        lease: LeaseRecord | None = None,
        calls: list[tuple[str, str, float]] | None = None,
        ledger: _RecordingLedger | None = None,
        **arguments: object,
    ) -> tuple[
        RunControllerHeartbeatCoordinator,
        _RecordingLedger,
        list[tuple[str, str, float]],
    ]:
        selected_lease = lease or _controller_lease(run_id=run_id)
        selected_calls = [] if calls is None else calls
        selected_ledger = ledger or _RecordingLedger(selected_lease, selected_calls)
        result = RunControllerHeartbeatCoordinator(
            selected_ledger,
            actor_principal_id=arguments.pop("actor_principal_id", "operator"),
            run_id=run_id,
            controller_lease=selected_lease,
            ttl_seconds=arguments.pop("ttl_seconds", 12.0),
            interval_seconds=arguments.pop("interval_seconds", 60.0),
            session_id=arguments.pop("session_id", "test-session"),
            **arguments,
        )
        return result, selected_ledger, selected_calls

    def test_round_renews_controller_then_targets_in_named_order(self) -> None:
        calls: list[tuple[str, str, float]] = []
        zeta = _RecordingTarget("zeta", calls)
        alpha = _RecordingTarget("alpha", calls)
        coordinator, _ledger, _calls = self.coordinator(
            calls=calls,
            targets={"zeta": zeta, "alpha": alpha},
        )

        snapshot = coordinator.heartbeat_once()

        self.assertEqual([item[0] for item in calls], ["controller", "alpha", "zeta"])
        self.assertTrue(snapshot.controller_renewed)
        self.assertEqual(snapshot.completed_target_names, ("alpha", "zeta"))
        self.assertEqual(snapshot.round_number, 1)
        self.assertEqual(snapshot.controller_lease.heartbeat_revision, 1)
        self.assertEqual(coordinator.completed_rounds, 1)
        self.assertTrue(all(item[2] == 12.0 for item in calls))

    def test_target_failure_preserves_the_successful_round_prefix(self) -> None:
        calls: list[tuple[str, str, float]] = []
        cause = RuntimeError("beta failed")
        coordinator, _ledger, _calls = self.coordinator(
            calls=calls,
            targets={
                "beta": _RecordingTarget("beta", calls, failure=cause),
                "alpha": _RecordingTarget("alpha", calls),
                "zeta": _RecordingTarget("zeta", calls),
            },
        )

        with self.assertRaisesRegex(
            RunControllerHeartbeatError, "target:beta.*round 1"
        ):
            coordinator.heartbeat_once()

        self.assertEqual([item[0] for item in calls], ["controller", "alpha", "beta"])
        self.assertTrue(coordinator.snapshot.controller_renewed)
        self.assertEqual(coordinator.snapshot.completed_target_names, ("alpha",))
        self.assertEqual(coordinator.controller_lease.heartbeat_revision, 1)
        self.assertEqual(coordinator.completed_rounds, 0)
        self.assertIs(coordinator.failure.cause, cause)  # type: ignore[union-attr]

    def test_operation_ids_are_unique_and_caller_identity_is_hashed(self) -> None:
        pathful_actor = "operator-/private/actor"
        pathful_run = "run-/private/workspace"
        lease = _controller_lease(run_id=pathful_run)
        calls: list[tuple[str, str, float]] = []
        target = _RecordingTarget("method", calls)
        coordinator, _ledger, _calls = self.coordinator(
            actor_principal_id=pathful_actor,
            run_id=pathful_run,
            lease=lease,
            calls=calls,
            targets={"method": target},
            session_id="stable-session",
        )

        coordinator.heartbeat_once()
        coordinator.heartbeat_once()

        operation_ids = [item[1] for item in calls]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertTrue(all("stable-session" in item for item in operation_ids))
        self.assertTrue(all("/private" not in item for item in operation_ids))
        self.assertTrue(operation_ids[0].endswith("/0000000000000001/controller"))
        self.assertTrue(operation_ids[2].endswith("/0000000000000002/controller"))

        next_term = _controller_lease(
            run_id=pathful_run,
            lease_id="controller-b",
            holder_id="holder-b",
            fencing_token=2,
            generation=2,
        )
        next_calls: list[tuple[str, str, float]] = []
        next_coordinator, _next_ledger, _ = self.coordinator(
            actor_principal_id=pathful_actor,
            run_id=pathful_run,
            lease=next_term,
            calls=next_calls,
            session_id="stable-session",
        )
        next_coordinator.heartbeat_once()
        self.assertTrue(set(operation_ids).isdisjoint(item[1] for item in next_calls))

    def test_error_surface_redacts_cause_and_has_no_exception_chain(self) -> None:
        private_path = "/private/realm/method-worker.py"
        cause = RuntimeError(f"failed below {private_path}")
        lease = _controller_lease(run_id="run-/private/workspace")
        calls: list[tuple[str, str, float]] = []
        ledger = _RecordingLedger(lease, calls, failure=cause)
        coordinator, _ledger, _calls = self.coordinator(
            actor_principal_id="actor-/private/home",
            run_id="run-/private/workspace",
            lease=lease,
            calls=calls,
            ledger=ledger,
        )

        with self.assertRaises(RunControllerHeartbeatError) as raised:
            coordinator.heartbeat_once()

        self.assertNotIn(private_path, str(raised.exception))
        self.assertNotIn("workspace", str(raised.exception))
        self.assertEqual(
            str(raised.exception),
            "Run-controller heartbeat failed during controller in round 1: RuntimeError.",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertIs(raised.exception.failure.cause, cause)
        with self.assertRaises(RunControllerHeartbeatError) as foreground:
            coordinator.raise_if_failed()
        self.assertNotIn(private_path, str(foreground.exception))

        class _CallerNamedError(RuntimeError):
            pass

        _CallerNamedError.__name__ = "/private/adversarial/error"
        adversarial_lease = _controller_lease(run_id="adversarial-run")
        adversarial_calls: list[tuple[str, str, float]] = []
        adversarial_ledger = _RecordingLedger(
            adversarial_lease,
            adversarial_calls,
            failure=_CallerNamedError("private details"),
        )
        adversarial, _ledger, _calls = self.coordinator(
            run_id="adversarial-run",
            lease=adversarial_lease,
            calls=adversarial_calls,
            ledger=adversarial_ledger,
        )
        with self.assertRaises(RunControllerHeartbeatError) as sanitized:
            adversarial.heartbeat_once()
        self.assertEqual(
            str(sanitized.exception),
            "Run-controller heartbeat failed during controller in round 1: BaseException.",
        )

    def test_failure_callback_runs_once_outside_the_round_and_state_locks(self) -> None:
        calls: list[tuple[str, str, float]] = []
        callback_calls: list[object] = []
        callback_entered = threading.Event()
        cause = RuntimeError("method target failed")
        coordinator: RunControllerHeartbeatCoordinator

        def callback(failure: object) -> None:
            # Accessing state and attempting attachment would deadlock if the
            # callback ran under either coordinator lock.
            callback_calls.append((failure, coordinator.snapshot))
            with self.assertRaises(RunControllerHeartbeatStateError):
                coordinator.attach_target("late", _RecordingTarget("late", calls))
            callback_entered.set()

        coordinator, _ledger, _calls = self.coordinator(
            calls=calls,
            targets={"method": _RecordingTarget("method", calls, failure=cause)},
            failure_callback=callback,
        )

        with self.assertRaises(RunControllerHeartbeatError):
            coordinator.heartbeat_once()
        self.assertTrue(callback_entered.is_set())
        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.heartbeat_once()
        with self.assertRaises(RunControllerHeartbeatError):
            coordinator.raise_if_failed()
        coordinator.stop()
        coordinator.stop()

        self.assertEqual(len(callback_calls), 1)

    def test_callback_failure_is_retained_without_replacing_heartbeat_failure(self) -> None:
        callback_cause = _CrashCut("callback crashed")
        heartbeat_cause = RuntimeError("controller failed")
        lease = _controller_lease()
        calls: list[tuple[str, str, float]] = []
        ledger = _RecordingLedger(lease, calls, failure=heartbeat_cause)

        def callback(_failure: object) -> None:
            raise callback_cause

        coordinator, _ledger, _calls = self.coordinator(
            lease=lease,
            calls=calls,
            ledger=ledger,
            failure_callback=callback,
        )

        with self.assertRaises(RunControllerHeartbeatError) as raised:
            coordinator.heartbeat_once()

        self.assertIs(raised.exception.failure.cause, heartbeat_cause)
        self.assertIs(coordinator.failure_callback_cause, callback_cause)

    def test_background_baseexception_stops_and_surfaces_in_foreground(self) -> None:
        calls: list[tuple[str, str, float]] = []
        crash = _CrashCut("simulated crash cut")
        callback_entered = threading.Event()
        coordinator, _ledger, _calls = self.coordinator(
            calls=calls,
            targets={"method": _RecordingTarget("method", calls, failure=crash)},
            failure_callback=lambda _failure: callback_entered.set(),
        )

        coordinator.start()
        self.assertTrue(callback_entered.wait(2))
        coordinator.stop()

        self.assertTrue(coordinator.stopped)
        self.assertFalse(coordinator.running)
        self.assertIs(coordinator.failure.cause, crash)  # type: ignore[union-attr]
        with self.assertRaisesRegex(
            RunControllerHeartbeatError, "target:method.*_CrashCut"
        ):
            coordinator.raise_if_failed()

    def test_stop_waits_for_background_failure_callback_without_locking_it(self) -> None:
        calls: list[tuple[str, str, float]] = []
        callback_entered = threading.Event()
        release_callback = threading.Event()
        callback_finished = threading.Event()
        stop_finished = threading.Event()
        stop_errors: list[BaseException] = []
        coordinator: RunControllerHeartbeatCoordinator

        def callback(_failure: object) -> None:
            callback_entered.set()
            # These coordinator operations acquire its own locks.  They must
            # remain usable while stop() waits for this callback to finish.
            self.assertEqual(coordinator.snapshot.round_number, 1)
            self.assertEqual(coordinator.target_names, ("method",))
            if not release_callback.wait(2):
                raise AssertionError("test did not release callback")
            callback_finished.set()

        coordinator, ledger, _calls = self.coordinator(
            calls=calls,
            targets={
                "method": _RecordingTarget(
                    "method", calls, failure=RuntimeError("failed")
                )
            },
            failure_callback=callback,
        )
        coordinator.start()
        self.assertTrue(ledger.entered.wait(2))
        self.assertTrue(callback_entered.wait(2))

        def stop_coordinator() -> None:
            try:
                coordinator.stop()
            except BaseException as error:
                stop_errors.append(error)
            finally:
                stop_finished.set()

        stop_thread = threading.Thread(target=stop_coordinator)
        stop_thread.start()
        self.assertFalse(stop_finished.wait(0.05))
        release_callback.set()
        stop_thread.join(2)

        self.assertTrue(callback_finished.is_set())
        self.assertTrue(stop_finished.is_set())
        self.assertEqual(stop_errors, [])
        self.assertTrue(coordinator.stopped)

    def test_stop_waits_for_entered_manual_round_and_fences_future_rounds(self) -> None:
        calls: list[tuple[str, str, float]] = []
        release = threading.Event()
        target = _RecordingTarget("method", calls, release=release)
        coordinator, _ledger, _calls = self.coordinator(
            calls=calls, targets={"method": target}
        )
        round_done = threading.Event()
        stop_done = threading.Event()
        errors: list[BaseException] = []

        def run_round() -> None:
            try:
                coordinator.heartbeat_once()
            except BaseException as error:
                errors.append(error)
            finally:
                round_done.set()

        def stop_coordinator() -> None:
            try:
                coordinator.stop()
            except BaseException as error:
                errors.append(error)
            finally:
                stop_done.set()

        round_thread = threading.Thread(target=run_round)
        round_thread.start()
        self.assertTrue(target.entered.wait(2))
        stop_thread = threading.Thread(target=stop_coordinator)
        stop_thread.start()
        self.assertFalse(stop_done.wait(0.05))

        release.set()
        round_thread.join(2)
        stop_thread.join(2)

        self.assertTrue(round_done.is_set())
        self.assertTrue(stop_done.is_set())
        self.assertEqual(errors, [])
        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.heartbeat_once()

    def test_attachment_is_one_time_and_closes_at_first_round(self) -> None:
        calls: list[tuple[str, str, float]] = []
        target = _RecordingTarget("original", calls)
        coordinator, _ledger, _calls = self.coordinator(calls=calls)
        coordinator.attach_target("method", target)

        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.attach_target("method", _RecordingTarget("other", calls))
        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.attach_target("alias", target)

        target.heartbeat = lambda **_arguments: calls.append(
            ("replacement", "replacement", 0.0)
        )
        coordinator.heartbeat_once()

        self.assertEqual([item[0] for item in calls], ["controller", "original"])
        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.attach_target("late", _RecordingTarget("late", calls))

    def test_start_closes_attachment_and_stop_leaves_no_live_thread(self) -> None:
        calls: list[tuple[str, str, float]] = []
        target = _RecordingTarget("method", calls)
        coordinator, _ledger, _calls = self.coordinator(
            calls=calls,
            targets={"method": target},
            session_id="thread-check",
        )
        coordinator.start()
        self.assertTrue(target.entered.wait(2))
        with self.assertRaises(RunControllerHeartbeatStateError):
            coordinator.attach_target("late", _RecordingTarget("late", calls))

        coordinator.stop()
        coordinator.stop()

        self.assertTrue(coordinator.stopped)
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name.startswith("optpilot-controller-heartbeat-")
                and "thread-c" in thread.name
                for thread in threading.enumerate()
            )
        )

    def test_invalid_arguments_and_controller_identity_fail_closed(self) -> None:
        lease = _controller_lease()
        invalid_numbers = (True, 0, -1, math.nan, math.inf, -math.inf, "1")
        for field in ("ttl_seconds", "interval_seconds"):
            for value in invalid_numbers:
                with self.subTest(field=field, value=value):
                    arguments = {field: value}
                    with self.assertRaises((TypeError, ValueError)):
                        self.coordinator(**arguments)

        invalid_arguments = (
            {"actor_principal_id": ""},
            {"actor_principal_id": "bad\x00actor"},
            {"run_id": ""},
            {"session_id": "bad/session"},
            {"session_id": ""},
            {"failure_callback": object()},
            {"targets": []},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    self.coordinator(**arguments)

        invalid_leases = (
            replace(lease, state=LeaseState.RELEASED),
            replace(lease, scope_key="run:other"),
            replace(lease, audience="other"),
            replace(lease, parent_lease_id="parent"),
            replace(lease, metadata={"run_id": "other", "controller_generation": 1}),
            replace(lease, metadata={"run_id": "run-a", "controller_generation": True}),
        )
        for invalid in invalid_leases:
            with self.subTest(lease=invalid):
                with self.assertRaises(ValueError):
                    self.coordinator(lease=invalid)

        coordinator, _ledger, _calls = self.coordinator()
        for name in ("", "bad/name", "x" * 65, "nonascii-你"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    coordinator.attach_target(name, _RecordingTarget(name, []))
        with self.assertRaises(TypeError):
            coordinator.attach_target("method", object())  # type: ignore[arg-type]

    def test_renewed_lease_identity_swap_fails_closed(self) -> None:
        lease = _controller_lease()
        calls: list[tuple[str, str, float]] = []

        class _SwappingLedger(_RecordingLedger):
            def heartbeat_lease(self, **arguments: object) -> LeaseRecord:
                renewed = super().heartbeat_lease(**arguments)
                return replace(renewed, holder_id="other-holder")

        ledger = _SwappingLedger(lease, calls)
        coordinator, _ledger, _calls = self.coordinator(
            lease=lease, calls=calls, ledger=ledger
        )

        with self.assertRaisesRegex(
            RunControllerHeartbeatError, "RunControllerHeartbeatStateError"
        ):
            coordinator.heartbeat_once()

        self.assertFalse(coordinator.snapshot.controller_renewed)
        self.assertEqual(coordinator.controller_lease, lease)

    def test_expired_or_time_regressed_renewal_fails_before_targets(self) -> None:
        now = time.time()
        lease = replace(
            _controller_lease(),
            created_at=now - 100.0,
            updated_at=now - 60.0,
            expires_at=now + 60.0,
        )
        invalid_renewals = (
            replace(
                lease,
                heartbeat_revision=1,
                updated_at=now,
                expires_at=now,
            ),
            replace(
                lease,
                heartbeat_revision=1,
                updated_at=now - 70.0,
                expires_at=now + 20.0,
            ),
            replace(
                lease,
                heartbeat_revision=1,
                updated_at=now - 50.0,
                expires_at=now - 1.0,
            ),
        )

        for invalid in invalid_renewals:
            with self.subTest(invalid=invalid):
                calls: list[tuple[str, str, float]] = []

                class _InvalidRenewalLedger(_RecordingLedger):
                    def heartbeat_lease(self, **arguments: object) -> LeaseRecord:
                        operation_id = arguments["operation_id"]
                        ttl_seconds = arguments["ttl_seconds"]
                        assert isinstance(operation_id, str)
                        assert isinstance(ttl_seconds, float)
                        self.calls.append(("controller", operation_id, ttl_seconds))
                        return invalid

                target = _RecordingTarget("method", calls)
                ledger = _InvalidRenewalLedger(lease, calls)
                coordinator, _ledger, _calls = self.coordinator(
                    lease=lease,
                    calls=calls,
                    ledger=ledger,
                    targets={"method": target},
                )

                with self.assertRaisesRegex(
                    RunControllerHeartbeatError,
                    "RunControllerHeartbeatStateError",
                ):
                    coordinator.heartbeat_once()

                self.assertEqual([item[0] for item in calls], ["controller"])
                self.assertFalse(target.entered.is_set())
                self.assertFalse(coordinator.snapshot.controller_renewed)

    def test_real_realm_renews_current_term_and_fences_replaced_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = RealmLedger(root / "realm.sqlite3")
            store = LocalContentStore(root / "store", store_id="local-a")
            try:
                ledger.register_principal(
                    operation_id="controller-heartbeat/principal",
                    principal_id="operator",
                    kind="human",
                )
                ledger.register_store(
                    operation_id="controller-heartbeat/store",
                    store_id=store.store_id,
                    backend_kind=store.BACKEND_KIND,
                    root_marker=store.root_marker,
                )
                closure, bindings, source_owner_id, source_revision = (
                    prepare_test_run_closure(
                        ledger=ledger,
                        store=store,
                        root=root,
                        actor_principal_id="operator",
                        prefix="controller-heartbeat",
                    )
                )
                manifest = prepare_test_run_control_manifest(closure, max_trials=1)
                definition, definition_bindings = prepare_test_run_definition(
                    closure, manifest, bindings
                )
                created = ledger.create_run_namespace(
                    operation_id="controller-heartbeat/run/create",
                    actor_principal_id="operator",
                    controller_holder_id="controller-a",
                    controller_ttl_seconds=60,
                    run_definition=definition,
                    definition_bindings=definition_bindings,
                    source_owner_id=source_owner_id,
                    expected_source_owner_revision=source_revision,
                    run_id="real-run",
                    owner_id="real-run-owner",
                )
                coordinator = RunControllerHeartbeatCoordinator(
                    ledger,
                    actor_principal_id="operator",
                    run_id=created.run.run_id,
                    controller_lease=created.controller_lease,
                    ttl_seconds=120,
                    interval_seconds=60,
                    session_id="real-realm",
                )

                renewed = coordinator.heartbeat_once()
                self.assertGreater(
                    renewed.controller_lease.heartbeat_revision,
                    created.controller_lease.heartbeat_revision,
                )
                self.assertGreater(
                    renewed.controller_lease.expires_at,
                    created.controller_lease.expires_at,
                )

                replacement = ledger.replace_run_controller(
                    operation_id="controller-heartbeat/run/replace",
                    actor_principal_id="operator",
                    run_id=created.run.run_id,
                    expected_controller_generation=created.run.controller_generation,
                    expected_controller_lease_id=created.controller_lease.lease_id,
                    expected_controller_holder_id=created.controller_lease.holder_id,
                    expected_controller_fencing_token=(
                        created.controller_lease.fencing_token
                    ),
                    new_controller_holder_id="controller-b",
                    controller_ttl_seconds=60,
                )
                with self.assertRaises(RunControllerHeartbeatError) as stale:
                    coordinator.heartbeat_once()
                self.assertEqual(stale.exception.failure.phase, "controller")
                self.assertIsInstance(stale.exception.failure.cause, RealmConflict)
                self.assertFalse(coordinator.snapshot.controller_renewed)

                current = RunControllerHeartbeatCoordinator(
                    ledger,
                    actor_principal_id="operator",
                    run_id=created.run.run_id,
                    controller_lease=replacement.controller_lease,
                    ttl_seconds=120,
                    interval_seconds=60,
                    session_id="real-realm",
                ).heartbeat_once()
                self.assertEqual(current.controller_lease.fencing_token, 2)
                self.assertEqual(current.controller_lease.heartbeat_revision, 1)
            finally:
                store.close()
                ledger.close()


if __name__ == "__main__":
    unittest.main()
