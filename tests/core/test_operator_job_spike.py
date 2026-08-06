"""Crash and concurrency checks for the disposable Operator Job spike."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.spikes.operator_job_spike import (
    DurableFakeAdmission,
    DurableFakeBackend,
    OperatorJobConflict,
    OperatorJobLedgerSpike,
    OperatorJobSupervisorSpike,
)


class OperatorJobSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.realm_db = root / "realm" / "realm.sqlite3"
        self.authority_db = root / "external" / "authority.sqlite3"
        self.ledger = OperatorJobLedgerSpike(self.realm_db)
        self.admission = DurableFakeAdmission(self.authority_db)
        self.backend = DurableFakeBackend(self.authority_db)
        self.supervisor = OperatorJobSupervisorSpike(self.ledger, self.admission, self.backend)
        self.plan = {
            "kind": "study_launch",
            "source_digest": "sha256:source-a",
            "runtime_digest": "sha256:runtime-a",
            "resources": {"cpu": 2, "memory_mb": 512},
            "timeout_seconds": 300,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _approved_job(self, suffix: str = "a") -> dict:
        job = self.ledger.create_job(request_id=f"request-{suffix}", plan=self.plan, job_id=f"job-{suffix}")
        self.ledger.request_approval(job["job_id"])
        return self.ledger.approve(job["job_id"], expected_plan_digest=job["plan_digest"])

    def _to_backend_running(self, suffix: str = "a") -> dict:
        job = self._approved_job(suffix)
        self.supervisor.reconcile_once(job["job_id"])
        return self.supervisor.reconcile_once(job["job_id"])

    def _to_handoff(self, suffix: str = "a") -> dict:
        job = self._to_backend_running(suffix)
        return self.supervisor.reconcile_once(job["job_id"])

    def _crash(
        self,
        job_id: str,
        crash_at: str,
        *,
        action: str = "reconcile",
        run_id: str = "",
        startup_token: str = "",
        controller_id: str = "controller-a",
    ) -> subprocess.CompletedProcess[str]:
        fixture = Path(__file__).parent.parent / "fixtures" / "operator_job_spike_worker.py"
        command = [
            sys.executable,
            str(fixture),
            "--realm-db",
            str(self.realm_db),
            "--authority-db",
            str(self.authority_db),
            "--job-id",
            job_id,
            "--action",
            action,
            "--crash-at",
            crash_at,
        ]
        if action == "heartbeat":
            command.extend(
                [
                    "--run-id",
                    run_id,
                    "--startup-token",
                    startup_token,
                    "--controller-id",
                    controller_id,
                    "--fencing-token",
                    "1",
                ]
            )
        return subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_happy_path_transfers_one_lease_and_heartbeat_completes_job(self) -> None:
        job = self._approved_job()
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["startup_token"])
        self.assertEqual(job["admission_token"], f"{job['startup_token']}:admission")
        self.assertEqual(job["backend_token"], f"{job['startup_token']}:backend")

        starting = self.supervisor.reconcile_once(job["job_id"])
        running = self.supervisor.reconcile_once(job["job_id"])
        handed_off = self.supervisor.reconcile_once(job["job_id"])
        self.assertEqual((starting["state"], starting["phase"]), ("starting", "ensure_backend"))
        self.assertEqual((running["state"], running["phase"]), ("running", "create_run_startup"))
        self.assertEqual(handed_off["phase"], "await_controller_heartbeat")
        self.assertEqual(handed_off["admission"]["owner_kind"], "run")
        self.assertEqual(handed_off["admission"]["owner_id"], handed_off["run_id"])

        completed = self.ledger.controller_heartbeat(
            run_id=handed_off["run_id"],
            startup_token=handed_off["startup_token"],
            controller_id="controller-a",
            fencing_token=1,
        )
        self.assertEqual((completed["state"], completed["phase"]), ("succeeded", "done"))
        self.assertEqual(completed["run_startup"]["state"], "active")
        self.assertEqual(self.admission.inspect(job["admission_token"])["created_count"], 1)
        self.assertEqual(self.backend.inspect(job["backend_token"])["created_count"], 1)
        self.assertEqual(self.ledger.assert_invariants(), {"jobs": 1, "handoffs": 1, "integrity": ["ok"]})

    def test_hard_crash_before_and_after_admission_side_effect_recovers_one_allocation(self) -> None:
        before = self._approved_job("before-admission")
        crashed = self._crash(before["job_id"], "before_admission_external")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertIsNone(self.admission.inspect(before["admission_token"]))
        self.assertEqual(self.ledger.snapshot(before["job_id"])["state"], "queued")
        recovered = self.supervisor.reconcile_once(before["job_id"])
        self.assertEqual(recovered["state"], "starting")
        self.assertEqual(self.admission.inspect(before["admission_token"])["created_count"], 1)

        after = self._approved_job("after-admission")
        crashed = self._crash(after["job_id"], "after_admission_external_commit")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertEqual(self.ledger.snapshot(after["job_id"])["state"], "queued")
        external = self.admission.inspect(after["admission_token"])
        self.assertEqual((external["state"], external["created_count"]), ("held", 1))
        recovered = self.supervisor.reconcile_once(after["job_id"])
        self.assertEqual(recovered["state"], "starting")
        external = self.admission.inspect(after["admission_token"])
        self.assertEqual(external["created_count"], 1)
        self.assertGreaterEqual(external["ensure_calls"], 2)

    def test_hard_crash_before_and_after_backend_side_effect_recovers_one_execution(self) -> None:
        before = self._approved_job("before-backend")
        self.supervisor.reconcile_once(before["job_id"])
        crashed = self._crash(before["job_id"], "before_backend_external")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertIsNone(self.backend.inspect(before["backend_token"]))
        recovered = self.supervisor.reconcile_once(before["job_id"])
        self.assertEqual(recovered["state"], "running")
        self.assertEqual(self.backend.inspect(before["backend_token"])["created_count"], 1)

        after = self._approved_job("after-backend")
        self.supervisor.reconcile_once(after["job_id"])
        crashed = self._crash(after["job_id"], "after_backend_external_commit")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertEqual(self.ledger.snapshot(after["job_id"])["state"], "starting")
        external = self.backend.inspect(after["backend_token"])
        self.assertEqual((external["state"], external["created_count"]), ("live", 1))
        recovered = self.supervisor.reconcile_once(after["job_id"])
        self.assertEqual(recovered["state"], "running")
        external = self.backend.inspect(after["backend_token"])
        self.assertEqual(external["created_count"], 1)
        self.assertGreaterEqual(external["ensure_calls"], 2)

    def test_concurrent_reconcilers_create_one_admission_backend_and_handoff(self) -> None:
        job = self._approved_job("concurrent")

        def reconcile(_: int) -> dict:
            ledger = OperatorJobLedgerSpike(self.realm_db)
            admission = DurableFakeAdmission(self.authority_db)
            backend = DurableFakeBackend(self.authority_db)
            return OperatorJobSupervisorSpike(ledger, admission, backend).reconcile_once(job["job_id"])

        for _ in range(3):
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(reconcile, range(2)))

        snapshot = self.ledger.snapshot(job["job_id"])
        self.assertEqual(snapshot["phase"], "await_controller_heartbeat")
        self.assertEqual(self.admission.inspect(job["admission_token"])["created_count"], 1)
        self.assertEqual(self.backend.inspect(job["backend_token"])["created_count"], 1)
        self.assertEqual(self.ledger.assert_invariants()["handoffs"], 1)

    def test_handoff_transaction_rolls_back_on_hard_crash_then_commits_once(self) -> None:
        running = self._to_backend_running("handoff-crash")
        crashed = self._crash(running["job_id"], "handoff_after_run_startup_insert")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        rolled_back = self.ledger.snapshot(running["job_id"])
        self.assertEqual(rolled_back["phase"], "create_run_startup")
        self.assertIsNone(rolled_back["run_startup"])
        self.assertIsNone(rolled_back["handoff"])
        self.assertEqual(rolled_back["admission"]["owner_kind"], "operator_job")

        crashed = self._crash(running["job_id"], "after_handoff_ledger_commit")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        committed = self.ledger.snapshot(running["job_id"])
        self.assertEqual(committed["phase"], "await_controller_heartbeat")
        self.assertEqual(committed["admission"]["authority_lease_id"], rolled_back["admission"]["authority_lease_id"])
        self.assertEqual(committed["admission"]["owner_kind"], "run")
        self.assertIsNotNone(committed["handoff"])
        stable = self.supervisor.reconcile_once(running["job_id"])
        self.assertEqual(stable["revision"], committed["revision"])
        self.assertEqual(self.ledger.assert_invariants()["handoffs"], 1)

    def test_hard_crash_inside_and_after_heartbeat_is_exactly_once(self) -> None:
        handed_off = self._to_handoff("heartbeat-crash")
        crashed = self._crash(
            handed_off["job_id"],
            "heartbeat_after_controller_activation",
            action="heartbeat",
            run_id=handed_off["run_id"],
            startup_token=handed_off["startup_token"],
        )
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        rolled_back = self.ledger.snapshot(handed_off["job_id"])
        self.assertEqual(rolled_back["state"], "running")
        self.assertEqual(rolled_back["run_startup"]["state"], "awaiting_heartbeat")

        crashed = self._crash(
            handed_off["job_id"],
            "after_heartbeat_ledger_commit",
            action="heartbeat",
            run_id=handed_off["run_id"],
            startup_token=handed_off["startup_token"],
        )
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        committed = self.ledger.snapshot(handed_off["job_id"])
        self.assertEqual(committed["state"], "succeeded")
        revision = committed["revision"]
        replay = self.ledger.controller_heartbeat(
            run_id=handed_off["run_id"],
            startup_token=handed_off["startup_token"],
            controller_id="controller-a",
            fencing_token=1,
        )
        self.assertEqual(replay["revision"], revision)
        terminal = self.supervisor.reconcile_once(handed_off["job_id"])
        self.assertEqual(terminal["revision"], revision)

    def test_stop_confirms_backend_before_admission_and_recovers_external_crashes(self) -> None:
        running = self._to_backend_running("stop")
        stop = self.ledger.request_stop(running["job_id"])
        self.assertIsNone(stop["redirect"])
        self.assertEqual(stop["job"]["phase"], "stop_backend")

        crashed = self._crash(running["job_id"], "after_backend_stop_external_commit")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        snapshot = self.ledger.snapshot(running["job_id"])
        self.assertEqual(snapshot["phase"], "stop_backend")
        self.assertEqual(self.backend.inspect(running["backend_token"])["state"], "stopped")
        self.assertEqual(self.admission.inspect(running["admission_token"])["state"], "held")

        releasing = self.supervisor.reconcile_once(running["job_id"])
        self.assertEqual(releasing["phase"], "release_admission")
        self.assertEqual(self.admission.inspect(running["admission_token"])["state"], "held")
        crashed = self._crash(running["job_id"], "after_admission_release_external_commit")
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertEqual(self.ledger.snapshot(running["job_id"])["state"], "stopping")
        self.assertEqual(self.admission.inspect(running["admission_token"])["state"], "released")

        cancelled = self.supervisor.reconcile_once(running["job_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        revision = cancelled["revision"]
        self.assertEqual(self.supervisor.reconcile_once(running["job_id"])["revision"], revision)
        self.assertEqual(self.backend.inspect(running["backend_token"])["created_count"], 1)
        self.assertEqual(self.admission.inspect(running["admission_token"])["created_count"], 1)
        self.assertEqual(self.ledger.assert_invariants()["integrity"], ["ok"])

    def test_post_handoff_stop_redirects_without_releasing_run_resources(self) -> None:
        handed_off = self._to_handoff("redirect")
        admission_before = self.admission.inspect(handed_off["admission_token"])
        backend_before = self.backend.inspect(handed_off["backend_token"])
        result = self.ledger.request_stop(handed_off["job_id"])
        self.assertEqual(result["redirect"], "run")
        self.assertEqual(result["run_id"], handed_off["run_id"])
        after = self.ledger.snapshot(handed_off["job_id"])
        self.assertEqual((after["state"], after["phase"]), ("running", "await_controller_heartbeat"))
        self.assertEqual(self.admission.inspect(handed_off["admission_token"]), admission_before)
        self.assertEqual(self.backend.inspect(handed_off["backend_token"]), backend_before)

        completed = self.ledger.controller_heartbeat(
            run_id=handed_off["run_id"],
            startup_token=handed_off["startup_token"],
            controller_id="controller-a",
            fencing_token=1,
        )
        redirected = self.ledger.request_stop(handed_off["job_id"])
        self.assertEqual(redirected["run_id"], handed_off["run_id"])
        self.assertEqual(completed["admission"]["state"], "transferred")
        self.assertEqual(self.admission.inspect(handed_off["admission_token"])["state"], "held")
        self.assertEqual(self.backend.inspect(handed_off["backend_token"])["state"], "live")

    def test_preapproval_stop_terminalizes_without_resource_intents(self) -> None:
        planned = self.ledger.create_job(
            request_id="request-stop-planned",
            plan=self.plan,
            job_id="job-stop-planned",
        )
        awaiting = self.ledger.create_job(
            request_id="request-stop-awaiting",
            plan=self.plan,
            job_id="job-stop-awaiting",
        )
        awaiting = self.ledger.request_approval(awaiting["job_id"])

        for job in (planned, awaiting):
            stopped = self.ledger.request_stop(job["job_id"])["job"]
            self.assertEqual((stopped["state"], stopped["phase"]), ("cancelled", "done"))
            self.assertEqual(stopped["terminal"]["resources_acquired"], False)
            self.assertIsNone(stopped["admission"])
            self.assertIsNone(stopped["backend"])
            revision = stopped["revision"]
            replay = self.ledger.request_stop(job["job_id"])["job"]
            self.assertEqual(replay["revision"], revision)
            self.assertEqual(self.supervisor.reconcile_once(job["job_id"])["revision"], revision)
            self.assertIsNone(self.admission.inspect(job["admission_token"]))
            self.assertIsNone(self.backend.inspect(job["backend_token"]))

        self.assertEqual(self.ledger.assert_invariants(), {"jobs": 2, "handoffs": 0, "integrity": ["ok"]})

    def test_stop_records_replay_without_revisions_or_phase_conflicts(self) -> None:
        running = self._to_backend_running("stop-replay")
        stopping = self.ledger.request_stop(running["job_id"])["job"]
        binding = stopping["backend"]
        stopped_observation = self.backend.ensure_stopped(
            backend_token=binding["backend_token"],
            launch_digest=binding["launch_digest"],
            launch=binding["launch"],
        )
        releasing = self.ledger.record_backend_stopped(running["job_id"], stopped_observation)
        replayed_backend = self.ledger.record_backend_stopped(running["job_id"], stopped_observation)
        self.assertEqual(replayed_backend["revision"], releasing["revision"])
        self.assertEqual(replayed_backend["phase"], "release_admission")

        claim = replayed_backend["admission"]
        released_observation = self.admission.ensure_released(
            admission_token=claim["admission_token"],
            request_digest=claim["request_digest"],
            request=claim["request"],
        )
        cancelled = self.ledger.record_admission_released(running["job_id"], released_observation)
        replayed_release = self.ledger.record_admission_released(running["job_id"], released_observation)
        replayed_backend_after_terminal = self.ledger.record_backend_stopped(running["job_id"], stopped_observation)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(replayed_release["revision"], cancelled["revision"])
        self.assertEqual(replayed_backend_after_terminal["revision"], cancelled["revision"])

    def test_queued_and_starting_stop_cleanup_matches_existing_resources(self) -> None:
        queued = self._approved_job("stop-queued")
        self.ledger.request_stop(queued["job_id"])
        self.supervisor.reconcile_once(queued["job_id"])
        queued_cancelled = self.supervisor.reconcile_once(queued["job_id"])
        self.assertEqual(queued_cancelled["state"], "cancelled")
        self.assertEqual(self.backend.inspect(queued["backend_token"])["created_count"], 0)
        self.assertEqual(self.admission.inspect(queued["admission_token"])["created_count"], 0)

        starting = self._approved_job("stop-starting")
        starting = self.supervisor.reconcile_once(starting["job_id"])
        self.assertEqual(starting["state"], "starting")
        self.ledger.request_stop(starting["job_id"])
        self.supervisor.reconcile_once(starting["job_id"])
        starting_cancelled = self.supervisor.reconcile_once(starting["job_id"])
        self.assertEqual(starting_cancelled["state"], "cancelled")
        self.assertEqual(self.backend.inspect(starting["backend_token"])["created_count"], 0)
        self.assertEqual(self.admission.inspect(starting["admission_token"])["created_count"], 1)
        self.assertEqual(self.ledger.assert_invariants()["integrity"], ["ok"])

    def test_concurrent_stop_reconcilers_do_not_repeat_transitions(self) -> None:
        running = self._to_backend_running("stop-concurrent")
        stopping = self.ledger.request_stop(running["job_id"])["job"]
        expected_terminal_revision = stopping["revision"] + 2

        def reconcile(_: int) -> dict:
            ledger = OperatorJobLedgerSpike(self.realm_db)
            admission = DurableFakeAdmission(self.authority_db)
            backend = DurableFakeBackend(self.authority_db)
            supervisor = OperatorJobSupervisorSpike(ledger, admission, backend)
            first = supervisor.reconcile_once(running["job_id"])
            return supervisor.reconcile_once(running["job_id"]) if first["state"] != "cancelled" else first

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(reconcile, range(12)))

        terminal = self.ledger.snapshot(running["job_id"])
        self.assertTrue(all(result["state"] in {"stopping", "cancelled"} for result in results))
        self.assertEqual((terminal["state"], terminal["phase"]), ("cancelled", "done"))
        self.assertEqual(terminal["revision"], expected_terminal_revision)
        self.assertEqual(self.supervisor.reconcile_once(running["job_id"])["revision"], expected_terminal_revision)
        self.assertEqual(self.backend.inspect(running["backend_token"])["created_count"], 1)
        self.assertEqual(self.admission.inspect(running["admission_token"])["created_count"], 1)
        self.assertEqual(self.ledger.assert_invariants()["integrity"], ["ok"])

    def test_token_and_request_digest_conflicts_are_rejected(self) -> None:
        job = self._approved_job("conflict")
        self.supervisor.reconcile_once(job["job_id"])
        claim = self.ledger.snapshot(job["job_id"])["admission"]
        with self.assertRaises(OperatorJobConflict):
            self.admission.ensure_acquired(
                admission_token=claim["admission_token"],
                request_digest="sha256:different",
                request=claim["request"],
            )

        self.supervisor.reconcile_once(job["job_id"])
        binding = self.ledger.snapshot(job["job_id"])["backend"]
        with self.assertRaises(OperatorJobConflict):
            self.backend.ensure_started(
                backend_token=binding["backend_token"],
                launch_digest="sha256:different",
                launch=binding["launch"],
                admission_lease_id=claim["authority_lease_id"],
            )
        with self.assertRaises(OperatorJobConflict):
            self.ledger.create_job(
                request_id="request-conflict",
                plan={**self.plan, "runtime_digest": "sha256:changed"},
            )


if __name__ == "__main__":
    unittest.main()
