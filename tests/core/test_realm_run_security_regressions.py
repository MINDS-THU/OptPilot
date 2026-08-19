from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    SessionHandleAdmission,
)
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE, WorkspaceLineage
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunSecurityRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="principal/operator",
            principal_id="operator",
            kind="human",
        )
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
            prefix="security",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=10)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.run = self.ledger.create_run_namespace(
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
        return f"security-regression/{self.counter}/{label}"

    def parameter_plan(self) -> RunAdmissionPlan:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        return RunAdmissionPlan(
            (CandidateAdmission("candidate-a", envelope),),
            (
                LogicalTrialAdmission(
                    logical_trial_id="trial-a",
                    candidate_id="candidate-a",
                    seed=7,
                ),
            ),
            (SessionHandleAdmission("handle-a", "trial-a"),),
        )

    def begin_run_change(self):
        return self.ledger.begin_owner_change(
            operation_id=self.op("run-change"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )

    def commit_plan(self, *, change_id: str, plan: RunAdmissionPlan, bindings=()):
        return self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admit"),
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            change_id=change_id,
            plan=plan,
            content_bindings=bindings,
        )

    def admit_parameter_candidate(self):
        change = self.begin_run_change()
        return self.commit_plan(change_id=change.change_id, plan=self.parameter_plan())

    def seal_source_tree(self):
        self.ledger.create_owner(
            operation_id=self.op("source-owner"),
            owner_id="source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "source"
        source.mkdir()
        (source / "run.py").write_text("print('ok')\n", encoding="utf-8")
        change = self.ledger.begin_owner_change(
            operation_id=self.op("source-change"),
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        capture = self.store.capture(
            change_id=change.change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change.change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_tree(source=AllowedTreeSource(source))
        membership = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, "source-revision"
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("source-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        commit = self.ledger.commit_owner_change(
            operation_id=self.op("source-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return sealed, commit

    def admit_file_candidate(self):
        sealed, source_commit = self.seal_source_tree()
        binding = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, RUN_CANDIDATE_ROLE
        )
        change = self.begin_run_change()
        self.ledger.hold_owner_content(
            operation_id=self.op("run-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(binding,),
            source_owner_id="source-owner",
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py", "contentRef": str(sealed.snapshot_ref)},
            content_refs=(sealed.snapshot_ref,),
        )
        plan = RunAdmissionPlan(
            (CandidateAdmission("files-a", envelope),),
            (
                LogicalTrialAdmission(
                    logical_trial_id="trial-files",
                    candidate_id="files-a",
                ),
            ),
            (SessionHandleAdmission("handle-files", "trial-files"),),
        )
        receipt = self.commit_plan(
            change_id=change.change_id, plan=plan, bindings=(binding,)
        )
        return sealed, source_commit, binding, receipt

    def test_active_run_and_workspace_memberships_reject_replace_with_new_revision(
        self,
    ) -> None:
        sealed, source_commit, binding, _ = self.admit_file_candidate()
        workspace_root = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, WORKSPACE_REVISION_ROLE
        )
        workspace = self.ledger.create_workspace_from_snapshot(
            operation_id=self.op("workspace-create"),
            actor_principal_id="operator",
            source_owner_id="source-owner",
            expected_source_owner_revision=source_commit.owner_revision,
            title="Kept simulator",
            root=workspace_root,
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id="source-owner",
                source_id="source-owner",
                source_revision=source_commit.owner_revision,
                source_store_id=self.store.store_id,
                source_ref=sealed.snapshot_ref,
            ),
            workspace_id="workspace-a",
            owner_id="workspace-owner-a",
        )

        connection = sqlite3.connect(self.database)
        try:
            for owner_id, membership, error in (
                (
                    self.run.run.owner_id,
                    binding,
                    "run candidate membership cannot be replaced",
                ),
                (
                    workspace.workspace.owner_id,
                    workspace_root,
                    "workspace revision membership cannot be replaced",
                ),
            ):
                row = connection.execute(
                    "SELECT added_revision, added_txn_id FROM owner_memberships "
                    "WHERE owner_id = ? AND store_id = ? AND content_ref = ? "
                    "AND role = ? AND removed_revision IS NULL",
                    (
                        owner_id,
                        membership.store_id,
                        str(membership.content_ref),
                        membership.role,
                    ),
                ).fetchone()
                assert row is not None
                with self.assertRaisesRegex(sqlite3.IntegrityError, error):
                    connection.execute(
                        "INSERT OR REPLACE INTO owner_memberships("
                        "owner_id, store_id, content_ref, role, added_revision, "
                        "removed_revision, added_txn_id, removed_txn_id) "
                        "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
                        (
                            owner_id,
                            membership.store_id,
                            str(membership.content_ref),
                            membership.role,
                            int(row[0]) + 100,
                            row[1],
                        ),
                    )
                connection.rollback()
                self.assertEqual(
                    connection.execute(
                        "SELECT added_revision, added_txn_id FROM owner_memberships "
                        "WHERE owner_id = ? AND store_id = ? AND content_ref = ? "
                        "AND role = ? AND removed_revision IS NULL",
                        (
                            owner_id,
                            membership.store_id,
                            str(membership.content_ref),
                            membership.role,
                        ),
                    ).fetchall(),
                    [row],
                )
        finally:
            connection.close()

    def test_sealed_admission_transaction_rejects_all_post_commit_appends(self) -> None:
        receipt = self.admit_parameter_candidate()
        txn_id = receipt.revision.txn_id
        candidate_key = receipt.candidates[0].candidate_key
        second = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 2}
        )
        connection = sqlite3.connect(self.database)
        try:
            initial_counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "run_candidates",
                    "run_logical_trials",
                    "run_submission_handles",
                    "run_events",
                )
            }
            appends = (
                (
                    "run_candidates",
                    "INSERT INTO run_candidates("
                    "run_id, candidate_key, candidate_id, candidate_ref, candidate_format, "
                    "spec_json, lineage_json, generator_json, accepted_run_revision, "
                    "accepted_owner_revision, accepted_sequence, accepted_txn_id, created_at) "
                    "VALUES ('run-a', 'forged-key', 'forged-candidate', ?, 'parameters', "
                    "'{}', '{}', '{}', 1, 0, 100, ?, 1.0)",
                    (str(second.candidate_ref), txn_id),
                ),
                (
                    "run_logical_trials",
                    "INSERT INTO run_logical_trials("
                    "run_id, logical_trial_id, candidate_key, seed_json, repetition_index, "
                    "submission_metadata_json, budget_slot, state, accepted_sequence, "
                    "accepted_run_revision, accepted_txn_id) VALUES "
                    "('run-a', 'forged-trial', ?, '99', 0, '{}', 2, 'accepted', "
                    "100, 1, ?)",
                    (candidate_key, txn_id),
                ),
                (
                    "run_submission_handles",
                    "INSERT INTO run_submission_handles("
                    "run_id, handle_id, logical_trial_id, accepted_sequence, "
                    "accepted_run_revision, accepted_txn_id) VALUES "
                    "('run-a', 'forged-handle', 'missing-trial', 100, 1, ?)",
                    (txn_id,),
                ),
                (
                    "run_events",
                    "INSERT INTO run_events("
                    "run_id, sequence, event_id, schema_version, producer, event, phase, "
                    "state, outcome, code, terminal, candidate_id, logical_trial_id, "
                    "session_handle, payload_json, run_revision, txn_id, created_at) VALUES "
                    "('run-a', 100, 'forged-event', '1', 'attacker', 'forged', NULL, "
                    "NULL, NULL, NULL, 0, NULL, NULL, NULL, '{}', 1, ?, 1.0)",
                    (txn_id,),
                ),
            )
            for table, statement, parameters in appends:
                with self.subTest(table=table):
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "requires its admission transaction"
                    ):
                        connection.execute(statement, parameters)
                    connection.rollback()
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        initial_counts[table],
                    )
        finally:
            connection.close()

    def test_sealed_file_candidate_rejects_additional_content_ref(self) -> None:
        _, _, _, receipt = self.admit_file_candidate()
        candidate_key = receipt.candidates[0].candidate_key
        connection = sqlite3.connect(self.database)
        try:
            extra = connection.execute(
                "SELECT child_ref FROM content_edges WHERE store_id = ? LIMIT 1",
                (self.store.store_id,),
            ).fetchone()
            assert extra is not None
            before = connection.execute(
                "SELECT content_ref FROM run_candidate_refs "
                "WHERE run_id = 'run-a' AND candidate_key = ? ORDER BY content_ref",
                (candidate_key,),
            ).fetchall()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run candidate ref admission is already sealed",
            ):
                connection.execute(
                    "INSERT INTO run_candidate_refs("
                    "run_id, candidate_key, content_ref, accepted_run_revision, "
                    "accepted_txn_id) VALUES ('run-a', ?, ?, 1, ?)",
                    (candidate_key, extra[0], receipt.revision.txn_id),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT content_ref FROM run_candidate_refs "
                    "WHERE run_id = 'run-a' AND candidate_key = ? ORDER BY content_ref",
                    (candidate_key,),
                ).fetchall(),
                before,
            )
        finally:
            connection.close()

    def test_run_admission_rejects_provisional_change_owned_by_another_owner(self) -> None:
        self.ledger.create_owner(
            operation_id=self.op("other-owner"),
            owner_id="other-owner",
            owner_kind="run",
            principal_id="operator",
        )
        wrong_change = self.ledger.begin_owner_change(
            operation_id=self.op("wrong-change"),
            actor_principal_id="operator",
            owner_id="other-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        operation_id = self.op("wrong-owner-admit")
        with self.assertRaises(RealmNotFound):
            self.ledger.commit_run_candidate_admissions(
                operation_id=operation_id,
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=0,
                expected_owner_revision=0,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                change_id=wrong_change.change_id,
                plan=self.parameter_plan(),
            )

        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM owner_transactions WHERE change_id = ?",
                    (wrong_change.change_id,),
                ).fetchone(),
                ("active",),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_candidates").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_generic_owner_commit_cannot_advance_or_consume_a_run_owner(self) -> None:
        change = self.begin_run_change()
        operation_id = self.op("generic-run-owner-commit")
        with self.assertRaisesRegex(
            RealmConflict, "fenced run domain transaction"
        ):
            self.ledger.commit_owner_change(
                operation_id=operation_id,
                actor_principal_id="operator",
                change_id=change.change_id,
                expected_owner_revision=0,
                additions=(),
            )

        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT revision FROM owners WHERE owner_id = ?",
                    (self.run.run.owner_id,),
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision FROM run_namespaces WHERE run_id = 'run-a'"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM owner_transactions WHERE change_id = ?",
                    (change.change_id,),
                ).fetchone(),
                ("active",),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
