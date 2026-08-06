from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import mock

from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.errors import (
    RealmCapacityUnavailable,
    RealmConflict,
    RealmExpired,
    RealmNotFound,
)
from optpilot.realm.inspection_service import RealmInspectionTargetService
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_attempt_launcher import RealmLocalAttemptLauncher
from optpilot.realm.local_capacity import conservative_local_host_capacity_limits
from optpilot.realm.local_process_supervisor import LocalProcessSupervisor
from optpilot.realm.operator_attempt_binding import RealmOperatorAttemptBinder
from optpilot.realm.operator_capacity_records import (
    OperatorCapacityPoolState,
    OperatorCapacityReservationState,
)
from optpilot.realm.operator_job_service import RealmOperatorJobService
from tests.core.test_realm_local_attempt_launcher import _RetainedRuntimeFixture


class RealmOperatorCapacityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)
        principal = self.fixture.ledger.register_principal(
            operation_id="local-attempt/principal/operator",
            principal_id="operator",
            kind="human",
        )
        inspection = RealmInspectionTargetService(self.fixture.ledger, principal)
        supervisor = LocalProcessSupervisor(self.fixture.root / "capacity-provider")
        launcher = RealmLocalAttemptLauncher(supervisor)
        binder = RealmOperatorAttemptBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
        )
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        self.service = RealmOperatorJobService(
            self.fixture.ledger,
            principal,
            inspection,
            self.fixture.provider,
            binder,
            launcher,
            finalizer,
        )
        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
        )
        self.selection = self.fixture.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            kind="candidate",
            entity_id="candidate-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        self.pool = self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="capacity/pool/local-host",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={"cpu_millis": 1000, "memory_bytes": 2 * 1024**3, "gpu_count": 0},
        )

    def plan(self, label: str):
        return self.service.plan_candidate_debug_run(
            operation_id=f"capacity-debug-{label}",
            selection=self.selection,
        )

    def acquire(
        self,
        job_id: str,
        label: str,
        *,
        ledger: RealmLedger | None = None,
        holder_id: str = "operator-supervisor",
        ttl_seconds: float = 60,
    ):
        return (ledger or self.fixture.ledger).acquire_operator_capacity_reservation(
            operation_id=f"capacity/acquire/{label}",
            actor_principal_id="operator",
            pool_name="local-host",
            job_id=job_id,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
        )

    def test_exact_approved_plan_is_bound_and_survives_restart(self) -> None:
        job = self.plan("restart")
        reservation = self.acquire(job.job_id, "restart")

        self.assertEqual(dict(reservation.claims), dict(job.plan.resource_claims))
        self.assertEqual(reservation.plan_digest, job.plan_digest)
        self.assertEqual(reservation.pool_name, job.plan.backend_realm)
        self.assertEqual(reservation.state, OperatorCapacityReservationState.ACTIVE)
        self.assertNotIn(str(self.fixture.root), json.dumps(reservation.to_dict()))

        restarted = RealmLedger(self.fixture.ledger.database_path)
        self.addCleanup(restarted.close)
        recovered = restarted.validate_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
        )
        self.assertEqual(recovered, reservation)
        replay = self.acquire(
            job.job_id, "restart-second-operation", ledger=restarted
        )
        self.assertEqual(replay, reservation)

    def test_claim_binding_is_object_exact_not_dependent_on_json_key_order(self) -> None:
        original = self.service._launch_plan

        def reverse_claim_order(context):
            plan = original(context)
            return replace(
                plan,
                resource_claims=dict(reversed(tuple(plan.resource_claims.items()))),
            )

        with mock.patch.object(
            self.service, "_launch_plan", side_effect=reverse_claim_order
        ):
            job = self.plan("claim-order")
        reservation = self.acquire(job.job_id, "claim-order")

        self.assertEqual(dict(reservation.claims), dict(job.plan.resource_claims))

    def test_concurrent_acquisition_cannot_oversubscribe_one_realm_pool(self) -> None:
        jobs = (self.plan("concurrent-a"), self.plan("concurrent-b"))
        ledgers = (
            RealmLedger(self.fixture.ledger.database_path),
            RealmLedger(self.fixture.ledger.database_path),
        )
        for ledger in ledgers:
            self.addCleanup(ledger.close)
        barrier = threading.Barrier(2)

        def compete(index: int):
            barrier.wait(timeout=5)
            try:
                return self.acquire(
                    jobs[index].job_id,
                    f"concurrent-{index}",
                    ledger=ledgers[index],
                    holder_id=f"holder-{index}",
                )
            except RealmCapacityUnavailable as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(compete, range(2)))

        admitted = tuple(
            item for item in results if not isinstance(item, BaseException)
        )
        rejected = tuple(
            item for item in results if isinstance(item, RealmCapacityUnavailable)
        )
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(rejected), 1)
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            rows = connection.execute(
                "SELECT claims_json FROM operator_capacity_reservations "
                "WHERE pool_name = 'local-host' AND state = 'active'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0]), dict(admitted[0].claims))

    def test_expiry_reacquires_with_a_new_generation_and_fence(self) -> None:
        job = self.plan("expiry")
        first = self.acquire(
            job.job_id,
            "expiry-first",
            holder_id="first-holder",
            ttl_seconds=0.05,
        )
        time.sleep(0.1)
        with self.assertRaises(RealmExpired):
            self.fixture.ledger.validate_operator_capacity_reservation(
                actor_principal_id="operator",
                reservation_id=first.reservation_id,
                holder_id=first.holder_id,
                fencing_token=first.fencing_token,
            )

        second = self.acquire(
            job.job_id,
            "expiry-second",
            holder_id="replacement-holder",
        )
        self.assertEqual(second.reservation_id, first.reservation_id)
        self.assertEqual(second.generation, first.generation + 1)
        self.assertGreater(second.fencing_token, first.fencing_token)
        self.assertEqual(second.holder_id, "replacement-holder")
        with self.assertRaises(RealmConflict):
            self.fixture.ledger.validate_operator_capacity_reservation(
                actor_principal_id="operator",
                reservation_id=first.reservation_id,
                holder_id=first.holder_id,
                fencing_token=first.fencing_token,
            )

    def test_expired_job_cannot_reacquire_capacity_taken_by_another_job(self) -> None:
        first_job = self.plan("expired-capacity-a")
        second_job = self.plan("expired-capacity-b")
        self.acquire(
            first_job.job_id,
            "expired-capacity-first",
            ttl_seconds=0.05,
        )
        time.sleep(0.1)
        admitted = self.acquire(
            second_job.job_id,
            "expired-capacity-second",
            holder_id="second-holder",
        )
        self.assertEqual(admitted.state, OperatorCapacityReservationState.ACTIVE)
        with self.assertRaises(RealmCapacityUnavailable):
            self.acquire(
                first_job.job_id,
                "expired-capacity-reacquire",
                holder_id="replacement-holder",
            )

    def test_renew_release_and_authorization_are_exact(self) -> None:
        job = self.plan("renew")
        reservation = self.acquire(job.job_id, "renew")
        renewed = self.fixture.ledger.renew_operator_capacity_reservation(
            operation_id="capacity/renew/a",
            actor_principal_id="operator",
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
            ttl_seconds=120,
        )
        replayed = self.fixture.ledger.renew_operator_capacity_reservation(
            operation_id="capacity/renew/a",
            actor_principal_id="operator",
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
            ttl_seconds=120,
        )
        self.assertEqual(renewed.heartbeat_revision, 1)
        self.assertEqual(replayed, renewed)
        with self.assertRaises(RealmNotFound):
            self.fixture.ledger.read_operator_capacity_reservation(
                actor_principal_id="delegate",
                reservation_id=reservation.reservation_id,
            )
        unauthorized_job = self.plan("unauthorized-acquire")
        with self.assertRaises(RealmNotFound):
            self.fixture.ledger.acquire_operator_capacity_reservation(
                operation_id="capacity/acquire/unauthorized",
                actor_principal_id="delegate",
                pool_name="local-host",
                job_id=unauthorized_job.job_id,
                holder_id="delegate-holder",
                ttl_seconds=60,
            )

        released = self.fixture.ledger.release_operator_capacity_reservation(
            operation_id="capacity/release/a",
            actor_principal_id="operator",
            reservation_id=reservation.reservation_id,
            holder_id=reservation.holder_id,
            fencing_token=reservation.fencing_token,
        )
        self.assertEqual(released.state, OperatorCapacityReservationState.RELEASED)
        reacquired = self.acquire(job.job_id, "released-reacquire")
        self.assertEqual(reacquired.generation, released.generation + 1)
        self.assertGreater(reacquired.fencing_token, released.fencing_token)
        self.assertEqual(reacquired.state, OperatorCapacityReservationState.ACTIVE)

    def test_pool_limits_are_versioned_and_database_rejects_claim_tampering(self) -> None:
        same = self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="capacity/pool/replay-same-facts",
            actor_principal_id="operator",
            pool_name="local-host",
            limits=dict(self.pool.limits),
        )
        self.assertEqual(same, self.pool)
        reconfigured = self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="capacity/pool/different-limits",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={"cpu_millis": 2000, "memory_bytes": 2 * 1024**3},
        )
        self.assertEqual(reconfigured.revision, self.pool.revision + 1)
        self.assertEqual(reconfigured.state, OperatorCapacityPoolState.READY)
        with self.assertRaises(RealmNotFound):
            self.fixture.ledger.ensure_operator_capacity_pool(
                operation_id="capacity/pool/unauthorized-reconfigure",
                actor_principal_id="delegate",
                pool_name="local-host",
                limits=dict(reconfigured.limits),
            )

        job = self.plan("tamper")
        reservation = self.acquire(job.job_id, "tamper")
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE operator_capacity_reservations SET claims_json = ? "
                    "WHERE reservation_id = ?",
                    ('{"cpu_millis":1}', reservation.reservation_id),
                )

    def test_decreased_restart_limits_fence_old_authority_and_block_admission(self) -> None:
        first_job = self.plan("drift-first")
        first = self.acquire(first_job.job_id, "drift-first")
        restarted = RealmLedger(self.fixture.ledger.database_path)
        self.addCleanup(restarted.close)
        lower_limits = {
            "cpu_millis": 1000,
            "gpu_count": 0,
            "memory_bytes": 1024**3,
        }

        blocked = restarted.ensure_operator_capacity_pool(
            operation_id="capacity/pool/restart-drift/1",
            actor_principal_id="operator",
            pool_name="local-host",
            limits=lower_limits,
        )

        self.assertEqual(blocked.state, OperatorCapacityPoolState.BLOCKED)
        self.assertEqual(blocked.revision, self.pool.revision + 1)
        self.assertEqual(dict(blocked.limits), lower_limits)
        with self.assertRaises(RealmCapacityUnavailable):
            restarted.validate_operator_capacity_reservation(
                actor_principal_id="operator",
                reservation_id=first.reservation_id,
                holder_id=first.holder_id,
                fencing_token=first.fencing_token,
            )
        second_job = self.plan("drift-second")
        with self.assertRaises(RealmCapacityUnavailable):
            self.acquire(
                second_job.job_id,
                "drift-second-blocked",
                ledger=restarted,
            )

        restarted.release_operator_capacity_reservation(
            operation_id="capacity/release/restart-drift",
            actor_principal_id="operator",
            reservation_id=first.reservation_id,
            holder_id=first.holder_id,
            fencing_token=first.fencing_token,
        )
        ready = restarted.ensure_operator_capacity_pool(
            operation_id="capacity/pool/restart-drift/2",
            actor_principal_id="operator",
            pool_name="local-host",
            limits=lower_limits,
        )
        self.assertEqual(ready.state, OperatorCapacityPoolState.READY)
        self.assertGreater(ready.revision, blocked.revision)
        admitted = self.acquire(
            second_job.job_id,
            "drift-second-ready",
            ledger=restarted,
        )
        self.assertEqual(admitted.pool_revision, ready.revision)


class LocalCapacityDiscoveryTest(unittest.TestCase):
    def test_discovery_uses_the_most_restrictive_limits_and_never_claims_gpu(self) -> None:
        with (
            mock.patch("optpilot.realm.local_capacity.os.cpu_count", return_value=8),
            mock.patch(
                "optpilot.realm.local_capacity._affinity_cpu_millis",
                return_value=4000,
            ),
            mock.patch(
                "optpilot.realm.local_capacity._cgroup_cpu_quota_millis",
                return_value=2500,
            ),
            mock.patch(
                "optpilot.realm.local_capacity._physical_memory_bytes",
                return_value=16 * 1024**3,
            ),
            mock.patch(
                "optpilot.realm.local_capacity._cgroup_memory_limit_bytes",
                return_value=8 * 1024**3,
            ),
        ):
            limits = conservative_local_host_capacity_limits()

        self.assertEqual(
            dict(limits),
            {
                "cpu_millis": 2000,
                "gpu_count": 0,
                "memory_bytes": 6 * 1024**3,
            },
        )

    def test_unknown_capacity_fails_instead_of_inventing_a_fallback(self) -> None:
        with (
            mock.patch("optpilot.realm.local_capacity.os.cpu_count", return_value=None),
            mock.patch(
                "optpilot.realm.local_capacity._affinity_cpu_millis",
                return_value=None,
            ),
            mock.patch(
                "optpilot.realm.local_capacity._cgroup_cpu_quota_millis",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CPU capacity"):
                conservative_local_host_capacity_limits()


if __name__ == "__main__":
    unittest.main()
