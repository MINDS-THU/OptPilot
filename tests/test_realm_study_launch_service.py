from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

import optpilot.realm_run_execution_service as run_execution_service
from optpilot.realm._validation import thaw_json
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.operator_job_records import (
    OperatorJobCleanupState,
    OperatorJobResult,
    OperatorJobState,
    OperatorJobTerminalStatus,
)
from optpilot.realm.refs import request_digest
from optpilot.realm_retained_batch_run_driver import (
    RealmRetainedBatchRunDriver as _RealRetainedBatchRunDriver,
)
from optpilot.realm_run_execution_service import (
    RunExecutionDeferred,
    new_run_execution_dispatch_operation_id,
)
from optpilot.run_execution_profile import RunExecutionProfile
from optpilot.study_launch_service import _plan_context
from tests.test_retained_study_service import _write_package


class _ProjectionOnlyDriver:
    instances: list["_ProjectionOnlyDriver"] = []

    def __init__(self, runtime, authority) -> None:
        self.runtime = runtime
        self.authority = authority
        self.__class__.instances.append(self)

    @classmethod
    def take_over(cls, runtime, **kwargs):
        real = _RealRetainedBatchRunDriver.take_over(runtime, **kwargs)
        return cls(runtime, real.authority)

    def run(self):
        return self.runtime.run_reader.summary(run_id=self.authority.run_id)


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class RealmStudyLaunchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.study = _write_package(self.package)
        self.realm_root = self.root / "realm"
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.addCleanup(self.runtime.close)
        _ProjectionOnlyDriver.instances = []

    def plan(
        self,
        suffix: str,
        *,
        execution_profile: RunExecutionProfile | None = None,
    ):
        return self.runtime.study_launches.plan_local_package(
            operation_id=f"study-launch-test/{suffix}",
            package_root=self.package,
            study_config_path=self.study,
            display_name=f"Study {suffix}",
            execution_profile=execution_profile,
        )

    def _begin_start(self, launch_id: str):
        record = self.runtime.operator_jobs.read(job_id=launch_id)
        context = _plan_context(record)
        return self.runtime.operator_jobs.begin_control_plane_start(
            job_id=launch_id,
            binding_id=context["binding_id"],
            launch_token=context["launch_token"],
            evidence_fingerprint=context["evidence_fingerprint"],
            launch_request_digest=context["launch_request_digest"],
        )

    def test_plan_is_durable_path_free_and_creates_no_run(self) -> None:
        planned = self.plan("plan-only")

        self.assertEqual(planned.job.state, OperatorJobState.QUEUED)
        self.assertIsNone(planned.handoff)
        self.assertEqual(self.runtime.run_reader.list_runs(limit=10).items, ())
        payload = planned.to_dict()
        self.assertNotIn(str(self.root), repr(payload))
        self.assertEqual(payload["status"], "queued")
        self.assertTrue(payload["can_stop"])

        reopened = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.addCleanup(reopened.close)
        recovered = reopened.study_launches.read(launch_id=planned.launch_id)
        self.assertEqual(recovered.job.to_dict(), planned.job.to_dict())

    def test_plan_retains_only_non_secret_method_setting_revision(self) -> None:
        method_path = self.package / "configs" / "methods" / "method.yaml"
        method = yaml.safe_load(method_path.read_text(encoding="utf-8"))
        method["runtime"] = {"envFromHost": ["TEST_METHOD_API_KEY"]}
        method_path.write_text(
            yaml.safe_dump(method, sort_keys=False),
            encoding="utf-8",
        )
        binding = {
            "binding_revision": "method-environment-test-revision",
            "recoverability": "settings-revision",
            "requirements": [
                {
                    "name": "TEST_METHOD_API_KEY",
                    "revision_id": "environment-revision-0123456789abcdef0123456789abcdef",
                    "source": "studio-settings",
                }
            ],
            "schema": "optpilot.method-environment-binding.v1",
        }

        planned = self.runtime.study_launches.plan_local_package(
            operation_id="study-launch-test/private-method-setting",
            package_root=self.package,
            study_config_path=self.study,
            method_environment_binding=binding,
        )

        facts = thaw_json(_plan_context(planned.job)["facts"])
        self.assertEqual(facts["method_environment_binding"], binding)
        self.assertNotIn("private-api-key-value", repr(planned.job.to_dict()))
        with self.assertRaises(RealmConflict):
            self.runtime.study_launches.plan_local_package(
                operation_id="study-launch-test/private-method-setting",
                package_root=self.package,
                study_config_path=self.study,
                method_environment_binding={
                    **binding,
                    "binding_revision": "method-environment-another-revision",
                },
            )

    def test_invalid_execution_profile_has_no_durable_side_effects(self) -> None:
        tables = (
            "content_objects",
            "ledger_transactions",
            "operator_jobs",
            "owners",
        )
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            before = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            )

        with self.assertRaisesRegex(TypeError, "RunExecutionProfile"):
            self.runtime.study_launches.plan_local_package(
                operation_id="study-launch-test/invalid-execution-profile",
                package_root=self.package,
                study_config_path=self.study,
                execution_profile=object(),
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            after = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            )
        self.assertEqual(after, before)

    def test_handoff_replay_creates_one_run_and_one_bootstrap_term(self) -> None:
        planned = self.plan("handoff-replay")
        starting = self._begin_start(planned.launch_id)
        operation_id = f"test/handoff/{planned.launch_id}"

        first = self.runtime.ledger.handoff_study_launch_to_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            job_id=planned.launch_id,
            expected_job_revision=starting.revision,
        )
        second = self.runtime.ledger.handoff_study_launch_to_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            job_id=planned.launch_id,
            expected_job_revision=starting.revision,
        )

        self.assertEqual(second, first)
        self.assertEqual(first.handoff.created_txn_id, first.creation.run.created_txn_id)
        self.assertEqual(first.handoff.controller_generation, 1)
        page = self.runtime.run_reader.list_runs(limit=10)
        self.assertEqual([item.run_id for item in page.items], [first.handoff.run_id])
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM study_launch_handoffs WHERE job_id = ?",
                    (planned.launch_id,),
                ).fetchone()[0],
                1,
            )

    def test_pre_handoff_cancel_is_terminal_and_never_creates_run(self) -> None:
        planned = self.plan("cancel-before")

        cancelled = self.runtime.study_launches.request_cancel(
            operation_id=f"cancel/{uuid.uuid4().hex}",
            launch_id=planned.launch_id,
        )

        self.assertEqual(cancelled.job.state, OperatorJobState.CANCELLED)
        self.assertEqual(
            cancelled.job.cleanup_state,
            OperatorJobCleanupState.COMPLETE,
        )
        self.assertIsNone(cancelled.handoff)
        self.assertEqual(self.runtime.run_reader.list_runs(limit=10).items, ())
        with self.assertRaises(RealmNotFound):
            self.runtime.ledger.read_study_launch_handoff(
                actor_principal_id="operator",
                job_id=planned.launch_id,
            )

    def test_pre_handoff_cancel_replays_after_terminal_cleanup(self) -> None:
        planned = self.plan("cancel-replay")
        operation_id = f"cancel/{uuid.uuid4().hex}"

        first = self.runtime.study_launches.request_cancel(
            operation_id=operation_id,
            launch_id=planned.launch_id,
        )
        replay = self.runtime.study_launches.request_cancel(
            operation_id=operation_id,
            launch_id=planned.launch_id,
        )

        self.assertEqual(replay.job.to_dict(), first.job.to_dict())
        self.assertEqual(replay.job.state, OperatorJobState.CANCELLED)
        self.assertEqual(replay.job.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_cancel_retries_when_start_wins_the_initial_revision_race(self) -> None:
        planned = self.plan("cancel-start-race")
        original_stop = self.runtime.ledger.request_operator_job_stop
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def delayed_stop(**kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(timeout=5))
            return original_stop(**kwargs)

        def cancel() -> None:
            try:
                results.append(
                    self.runtime.study_launches.request_cancel(
                        operation_id=f"cancel/{uuid.uuid4().hex}",
                        launch_id=planned.launch_id,
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch.object(
            self.runtime.ledger,
            "request_operator_job_stop",
            side_effect=delayed_stop,
        ):
            thread = threading.Thread(target=cancel)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            self._begin_start(planned.launch_id)
            release.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].job.state, OperatorJobState.CANCELLED)
        self.assertEqual(results[0].job.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertIsNone(results[0].handoff)

    def test_cancel_reroutes_when_handoff_wins_the_stop_insert_race(self) -> None:
        planned = self.plan("cancel-handoff-race")
        starting = self._begin_start(planned.launch_id)
        original_stop = self.runtime.ledger.request_operator_job_stop
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def delayed_stop(**kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original_stop(**kwargs)

        def cancel() -> None:
            try:
                results.append(
                    self.runtime.study_launches.request_cancel(
                        operation_id=f"cancel/{uuid.uuid4().hex}",
                        launch_id=planned.launch_id,
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch.object(
            self.runtime.ledger,
            "request_operator_job_stop",
            side_effect=delayed_stop,
        ):
            thread = threading.Thread(target=cancel)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            handoff = self.runtime.ledger.handoff_study_launch_to_run(
                operation_id=f"test/handoff/{planned.launch_id}",
                actor_principal_id="operator",
                job_id=planned.launch_id,
                expected_job_revision=starting.revision,
            ).handoff
            release.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].run_id, handoff.run_id)
        request = self.runtime.ledger.read_run_cancellation_request(
            actor_principal_id="operator",
            run_id=handoff.run_id,
        )
        self.assertIsNotNone(request)

    def test_execute_returns_cancelled_when_stop_wins_stale_handoff(self) -> None:
        planned = self.plan("execute-stop-race")
        original_handoff = self.runtime.ledger.handoff_study_launch_to_run
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def delayed_handoff(**kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original_handoff(**kwargs)

        def execute() -> None:
            try:
                results.append(
                    self.runtime.study_launches.execute(
                        launch_id=planned.launch_id
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch.object(
            self.runtime.ledger,
            "handoff_study_launch_to_run",
            side_effect=delayed_handoff,
        ):
            thread = threading.Thread(target=execute)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            cancelled = self.runtime.study_launches.request_cancel(
                operation_id=f"cancel/{uuid.uuid4().hex}",
                launch_id=planned.launch_id,
            )
            release.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].job.to_dict(), cancelled.job.to_dict())
        self.assertEqual(results[0].job.state, OperatorJobState.CANCELLED)
        self.assertEqual(self.runtime.run_reader.list_runs(limit=10).items, ())

    def test_passive_cleanup_rejects_unrelated_provider_proof(self) -> None:
        planned = self.plan("passive-cleanup-proof")
        cancelled = self.runtime.ledger.request_operator_job_stop(
            operation_id=f"cancel/{uuid.uuid4().hex}",
            actor_principal_id="operator",
            job_id=planned.launch_id,
            expected_revision=planned.job.revision,
            reason_code="user_cancelled",
        )
        self.assertEqual(cancelled.cleanup_state, OperatorJobCleanupState.PENDING)

        with self.assertRaisesRegex(RealmConflict, "cleanup proof"):
            self.runtime.operator_jobs.complete_control_plane_cleanup(
                job_id=planned.launch_id,
                provider_evidence_digest="a" * 64,
            )
        completed = self.runtime.study_launches.request_cancel(
            operation_id=f"cancel-reconcile/{uuid.uuid4().hex}",
            launch_id=planned.launch_id,
        )
        self.assertEqual(completed.job.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_reconcilable_scan_filters_before_each_bounded_page(self) -> None:
        older = self.plan("older-reconcilable")
        for suffix in ("newer-terminal-one", "newer-terminal-two"):
            terminal = self.plan(suffix)
            self.runtime.study_launches.request_cancel(
                operation_id=f"cancel/{uuid.uuid4().hex}",
                launch_id=terminal.launch_id,
            )

        reconcilable = self.runtime.study_launches.list_reconcilable(page_size=1)

        self.assertEqual([view.launch_id for view in reconcilable], [older.launch_id])

    def test_post_handoff_cancel_is_routed_and_finishes_without_method_start(self) -> None:
        planned = self.plan("cancel-after")
        starting = self._begin_start(planned.launch_id)
        receipt = self.runtime.ledger.handoff_study_launch_to_run(
            operation_id=f"test/handoff/{planned.launch_id}",
            actor_principal_id="operator",
            job_id=planned.launch_id,
            expected_job_revision=starting.revision,
        )

        routed = self.runtime.study_launches.request_cancel(
            operation_id=f"cancel/{uuid.uuid4().hex}",
            launch_id=planned.launch_id,
        )
        request = self.runtime.ledger.read_run_cancellation_request(
            actor_principal_id="operator",
            run_id=receipt.handoff.run_id,
        )

        self.assertIsNotNone(request)
        self.assertEqual(request.reason_code, "user_cancelled")
        self.assertEqual(routed.job.state, OperatorJobState.STARTING)
        completed = self.runtime.study_launches.execute(
            launch_id=planned.launch_id
        )
        summary = self.runtime.run_reader.summary(run_id=receipt.handoff.run_id)
        self.assertEqual(completed.job.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(summary.run_status, "cancelled")
        self.assertEqual(summary.stop_code, "user_cancelled")

    def test_handed_off_job_rejects_generic_failed_terminal_result(self) -> None:
        planned = self.plan("reject-generic-terminal")
        starting = self._begin_start(planned.launch_id)
        self.runtime.ledger.handoff_study_launch_to_run(
            operation_id=f"test/handoff/{planned.launch_id}",
            actor_principal_id="operator",
            job_id=planned.launch_id,
            expected_job_revision=starting.revision,
        )
        running = self.runtime.operator_jobs.mark_control_plane_running(
            job_id=planned.launch_id
        )
        malicious = OperatorJobResult(
            result_kind="study-launch",
            status="failed",
            metrics={},
            constraint_results={},
            event_summary={"forged": True},
            declared_outputs=(),
            logs=(),
            details={"schema": "malicious.study-launch-result.v1"},
        )

        with self.assertRaises(RealmConflict):
            self.runtime.operator_jobs.finish_control_plane_job(
                job_id=planned.launch_id,
                result=malicious,
                status=OperatorJobTerminalStatus.FAILED,
                code="forged_failure",
                terminal_proof_digest=request_digest(
                    {"forged": True, "job_id": planned.launch_id}
                ),
            )

        unchanged = self.runtime.operator_jobs.read(job_id=planned.launch_id)
        self.assertEqual(unchanged.state, OperatorJobState.RUNNING)
        self.assertEqual(unchanged.revision, running.revision)
        self.assertIsNone(unchanged.outcome)
        self.assertIsNone(unchanged.result)
        with self.assertRaises(RealmNotFound):
            self.runtime.ledger.read_study_launch_controller_confirmation(
                actor_principal_id="operator",
                job_id=planned.launch_id,
            )
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "exact controller confirmation",
            ):
                connection.execute(
                    "UPDATE operator_jobs SET state = 'failed' WHERE job_id = ?",
                    (planned.launch_id,),
                )

    def test_live_claim_is_not_stolen_after_runtime_restart(self) -> None:
        planned = self.plan("active-claim")
        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            first = self.runtime.study_launches.execute(
                launch_id=planned.launch_id
            )
        before = self.runtime.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=first.run_id
        )
        self.assertEqual(before.run.controller_generation, 2)
        confirmation = (
            self.runtime.ledger.read_study_launch_controller_confirmation(
                actor_principal_id="operator",
                job_id=planned.launch_id,
            )
        )
        self.assertEqual(confirmation.run_id, first.run_id)
        self.assertEqual(confirmation.controller_generation, 2)
        self.assertEqual(
            confirmation.terminal_job_revision,
            first.job.revision - 1,
        )

        reopened = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.addCleanup(reopened.close)
        replay = reopened.study_launches.execute(launch_id=planned.launch_id)
        after = reopened.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=first.run_id
        )
        replayed_confirmation = (
            reopened.operator_jobs.confirm_study_launch_controller(
                job_id=planned.launch_id,
                controller_lease_id=confirmation.controller_lease_id,
                controller_holder_id=confirmation.controller_holder_id,
                controller_fencing_token=(
                    confirmation.controller_fencing_token
                ),
                controller_generation=confirmation.controller_generation,
            )
        )
        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(after.run.controller_generation, 2)
        self.assertEqual(replayed_confirmation.to_dict(), replay.job.to_dict())

    def test_stale_expired_snapshot_cannot_steal_actual_live_controller(self) -> None:
        planned = self.plan("transactional-live-fence")
        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            launched = self.runtime.study_launches.execute(
                launch_id=planned.launch_id
            )
        actual = self.runtime.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=launched.run_id,
        )
        stale = replace(
            actual,
            controller_lease=replace(
                actual.controller_lease,
                expires_at=time.time() - 0.001,
            ),
        )

        descriptor = self.runtime.run_execution.describe(run_id=launched.run_id)
        with self.assertRaises(RunExecutionDeferred):
            self.runtime.run_execution._claim_driver(
                descriptor=descriptor,
                snapshot=stale,
                dispatch_operation_id=(
                    new_run_execution_dispatch_operation_id()
                ),
            )

        unchanged = self.runtime.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=launched.run_id,
        )
        self.assertEqual(unchanged.run.controller_generation, 2)
        self.assertEqual(
            unchanged.run.controller_lease_id,
            actual.run.controller_lease_id,
        )

    def test_truly_expired_controller_advances_to_next_generation(self) -> None:
        planned = self.plan(
            "expired-controller-recovery",
            execution_profile=RunExecutionProfile(controller_ttl_seconds=0.5),
        )
        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            launched = self.runtime.study_launches.execute(
                launch_id=planned.launch_id
            )
            time.sleep(0.6)
            recovered = self.runtime.study_launches.execute(
                launch_id=planned.launch_id
            )

        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=launched.run_id,
        )
        self.assertEqual(recovered.run_id, launched.run_id)
        self.assertEqual(snapshot.run.controller_generation, 3)


if __name__ == "__main__":
    unittest.main()
