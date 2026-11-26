from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.leases import LeaseState
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunControllerTermsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        self.ledger.register_principal(
            operation_id="principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="controller",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=4)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"controller-test/{self.counter}/{label}"

    def replace(
        self,
        *,
        operation_id: str,
        holder_id: str = "controller-b",
        generation: int | None = None,
        lease_id: str | None = None,
        previous_holder_id: str | None = None,
        fencing_token: int | None = None,
        ttl_seconds: float = 60,
    ):
        run = self.created.run
        lease = self.created.controller_lease
        return self.ledger.replace_run_controller(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=run.run_id,
            expected_controller_generation=(
                run.controller_generation if generation is None else generation
            ),
            expected_controller_lease_id=(lease.lease_id if lease_id is None else lease_id),
            expected_controller_holder_id=(
                lease.holder_id
                if previous_holder_id is None
                else previous_holder_id
            ),
            expected_controller_fencing_token=(
                lease.fencing_token if fencing_token is None else fencing_token
            ),
            new_controller_holder_id=holder_id,
            controller_ttl_seconds=ttl_seconds,
        )

    @staticmethod
    def parameter_plan() -> RunAdmissionPlan:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        return RunAdmissionPlan(
            (CandidateAdmission("candidate-a", envelope),),
            (
                LogicalTrialAdmission(
                    "trial-a", "candidate-a", seed=7
                ),
            ),
        )

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def test_active_handoff_rejects_old_fence_and_new_controller_can_admit(self) -> None:
        replacement = self.replace(operation_id=self.op("active-handoff"))

        self.assertEqual(replacement.run.controller_generation, 2)
        self.assertEqual(replacement.term.generation, 2)
        self.assertEqual(replacement.previous_controller_lease.state, LeaseState.REVOKED)
        self.assertEqual(replacement.controller_lease.state, LeaseState.ACTIVE)
        self.assertGreater(
            replacement.controller_lease.fencing_token,
            self.created.controller_lease.fencing_token,
        )
        self.assertEqual(replacement.run.current_revision, 1)
        self.assertEqual(replacement.run.next_sequence, 2)

        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-admission"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        common = {
            "actor_principal_id": "operator",
            "run_id": self.created.run.run_id,
            "expected_run_revision": 1,
            "expected_owner_revision": 0,
            "change_id": change.change_id,
            "plan": self.parameter_plan(),
        }
        with self.assertRaises(RealmConflict):
            self.ledger.commit_run_candidate_admissions(
                operation_id=self.op("old-controller-admission"),
                controller_lease_id=self.created.controller_lease.lease_id,
                controller_holder_id=self.created.controller_lease.holder_id,
                controller_fencing_token=self.created.controller_lease.fencing_token,
                **common,
            )

        admission = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("new-controller-admission"),
            controller_lease_id=replacement.controller_lease.lease_id,
            controller_holder_id=replacement.controller_lease.holder_id,
            controller_fencing_token=replacement.controller_lease.fencing_token,
            **common,
        )
        self.assertEqual(admission.run.current_revision, 2)
        self.assertEqual(admission.candidates[0].admission.candidate_id, "candidate-a")

    def test_released_controller_can_be_replaced(self) -> None:
        released = self.ledger.release_lease(
            operation_id=self.op("release"),
            actor_principal_id="operator",
            lease_id=self.created.controller_lease.lease_id,
            holder_id=self.created.controller_lease.holder_id,
            fencing_token=self.created.controller_lease.fencing_token,
        )
        self.assertEqual(released.state, LeaseState.RELEASED)

        replacement = self.replace(operation_id=self.op("released-takeover"))

        self.assertEqual(
            replacement.previous_controller_lease.state, LeaseState.RELEASED
        )
        self.assertEqual(replacement.controller_lease.state, LeaseState.ACTIVE)
        self.assertEqual(replacement.run.controller_generation, 2)

    def test_unswept_expired_controller_can_be_replaced(self) -> None:
        connection = self.connection()
        try:
            connection.execute(
                "UPDATE leases SET expires_at = created_at "
                "WHERE lease_id = ?",
                (self.created.controller_lease.lease_id,),
            )
            connection.commit()
        finally:
            connection.close()

        replacement = self.replace(operation_id=self.op("expired-takeover"))

        self.assertEqual(
            replacement.previous_controller_lease.state, LeaseState.EXPIRED
        )
        self.assertEqual(replacement.controller_lease.state, LeaseState.ACTIVE)
        self.assertEqual(replacement.run.controller_generation, 2)

    def test_stale_controller_tuple_does_not_mutate_authority(self) -> None:
        stale_values = (
            {"generation": 2},
            {"lease_id": "lease-does-not-exist"},
            {"previous_holder_id": "stale-holder"},
            {"fencing_token": self.created.controller_lease.fencing_token + 1},
        )
        for index, overrides in enumerate(stale_values):
            with self.subTest(overrides=overrides):
                with self.assertRaises(RealmConflict):
                    self.replace(
                        operation_id=self.op(f"stale-{index}"), **overrides
                    )

        connection = self.connection()
        try:
            namespace = connection.execute(
                "SELECT controller_generation, controller_lease_id, "
                "controller_holder_id, controller_fencing_token "
                "FROM run_namespaces WHERE run_id = 'run-a'"
            ).fetchone()
            self.assertEqual(
                namespace,
                (
                    1,
                    self.created.controller_lease.lease_id,
                    self.created.controller_lease.holder_id,
                    self.created.controller_lease.fencing_token,
                ),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_controller_terms WHERE run_id = 'run-a'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE scope_key = 'run:run-a'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_replacement_replays_exactly_and_changed_request_conflicts(self) -> None:
        operation_id = self.op("replay")
        replacement = self.replace(operation_id=operation_id)
        self.assertEqual(self.replace(operation_id=operation_id), replacement)

        with self.assertRaises(RealmConflict):
            self.replace(operation_id=operation_id, holder_id="controller-c")

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_controller_terms WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE scope_key = 'run:run-a'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_snapshot_uses_resulting_controller_term_not_revision_writer(self) -> None:
        replacement = self.replace(operation_id=self.op("snapshot-handoff"))

        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertEqual(snapshot.run, replacement.run)
        self.assertEqual(snapshot.controller_term, replacement.term)
        self.assertEqual(snapshot.controller_lease, replacement.controller_lease)
        self.assertEqual(
            snapshot.revision.writer_controller_lease_id,
            self.created.controller_lease.lease_id,
        )
        self.assertNotEqual(
            snapshot.revision.writer_controller_lease_id,
            snapshot.controller_lease.lease_id,
        )

    def test_generic_lease_acquisition_cannot_claim_reserved_controller_scope(self) -> None:
        self.ledger.release_lease(
            operation_id=self.op("release-for-generic"),
            actor_principal_id="operator",
            lease_id=self.created.controller_lease.lease_id,
            holder_id=self.created.controller_lease.holder_id,
            fencing_token=self.created.controller_lease.fencing_token,
        )

        with self.assertRaisesRegex(RealmConflict, "typed controller transaction"):
            self.ledger.acquire_lease(
                operation_id=self.op("generic-controller-claim"),
                actor_principal_id="operator",
                owner_id=self.created.run.owner_id,
                lease_kind="run-controller",
                audience="realm-ledger",
                holder_id="rogue-controller",
                scope_key="run:run-a",
                ttl_seconds=60,
            )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE scope_key = 'run:run-a'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT controller_generation FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_raw_controller_history_and_namespace_are_immutable(self) -> None:
        replacement = self.replace(operation_id=self.op("history"))
        connection = self.connection()
        try:
            first = connection.execute(
                "SELECT generation, lease_id, holder_id, fencing_token, txn_id, created_at "
                "FROM run_controller_terms WHERE run_id = 'run-a' AND generation = 1"
            ).fetchone()
            self.assertIsNotNone(first)

            attacks = (
                (
                    "UPDATE run_namespaces SET controller_generation = 1, "
                    "controller_lease_id = ?, controller_holder_id = ?, "
                    "controller_fencing_token = ?, controller_txn_id = ? "
                    "WHERE run_id = 'run-a'",
                    (first[1], first[2], first[3], first[4]),
                ),
                (
                    "UPDATE run_controller_terms SET holder_id = 'forged' "
                    "WHERE run_id = 'run-a' AND generation = 1",
                    (),
                ),
                (
                    "DELETE FROM run_controller_terms "
                    "WHERE run_id = 'run-a' AND generation = 1",
                    (),
                ),
                (
                    "INSERT OR REPLACE INTO run_controller_terms("
                    "run_id, generation, lease_id, holder_id, fencing_token, txn_id, created_at"
                    ") VALUES ('run-a', 999, ?, ?, ?, ?, ?)",
                    (first[1], first[2], first[3], first[4], first[5]),
                ),
            )
            for statement, parameters in attacks:
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
                    connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT generation, lease_id, holder_id, fencing_token, txn_id, created_at "
                    "FROM run_controller_terms WHERE run_id = 'run-a' "
                    "ORDER BY generation"
                ).fetchall(),
                [
                    first,
                    (
                        replacement.term.generation,
                        replacement.term.lease_id,
                        replacement.term.holder_id,
                        replacement.term.fencing_token,
                        replacement.term.txn_id,
                        replacement.term.created_at,
                    ),
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT controller_generation, controller_lease_id "
                    "FROM run_namespaces WHERE run_id = 'run-a'"
                ).fetchone(),
                (2, replacement.controller_lease.lease_id),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
