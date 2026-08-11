from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import SnapshotRef
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    EnvironmentRevisionManifest,
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.run_definition import RUN_METHOD_SOURCE_ROLE
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    RunCandidateSelection,
)
from optpilot.realm.selections import SelectionRef
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunClosureLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store-a", store_id="local-a")
        self.ledger.register_principal(
            operation_id="closure-ledger/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="closure-ledger/store-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            self.bindings,
            self.source_owner_id,
            self.source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="closure-ledger",
        )
        self.source_path = self.root / "closure-ledger-environment-source"

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_run(
        self,
        *,
        operation_id: str = "closure-ledger/run-create",
        run_id: str = "run-a",
        owner_id: str = "run-owner-a",
        closure: RunEvaluationClosure | None = None,
        bindings: tuple[OwnerMembership, ...] | None = None,
        source_revision: int | None = None,
    ):
        selected_closure = closure or self.closure
        selected_bindings = self.bindings if bindings is None else bindings
        manifest = prepare_test_run_control_manifest(
            selected_closure, max_trials=4
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            selected_closure, manifest, selected_bindings
        )
        return self.ledger.create_run_namespace(
            operation_id=operation_id,
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=(
                self.source_owner_revision
                if source_revision is None
                else source_revision
            ),
            run_id=run_id,
            owner_id=owner_id,
        )

    def _admit_parameter_candidate(self, created, *, suffix: str = "a"):
        change = self.ledger.begin_owner_change(
            operation_id=f"closure-ledger/{suffix}/begin-admission",
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        plan = RunAdmissionPlan(
            candidates=(
                CandidateAdmission(
                    candidate_id=f"candidate-{suffix}",
                    envelope=NormalizedCandidateEnvelope.build(
                        candidate_format="parameters", spec={"x": 3}
                    ),
                    lineage={"parents": []},
                    generator={"method_id": "test-method"},
                ),
            ),
            logical_trials=(
                LogicalTrialAdmission(
                    logical_trial_id=f"trial-{suffix}",
                    candidate_id=f"candidate-{suffix}",
                    seed=11,
                ),
            ),
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id=f"closure-ledger/{suffix}/admit",
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )
        return self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            candidate_id=f"candidate-{suffix}",
        )

    @staticmethod
    def _closure_for_snapshot(
        snapshot_ref: SnapshotRef, *, environment_id: str
    ) -> RunEvaluationClosure:
        environment = EnvironmentRevisionManifest(
            environment_id=environment_id,
            compiler_id="closure-ledger-test",
            compiler_version="1",
            authored_config=ScopePath("source", "environment.yaml"),
            source_layers=(ScopeLayer("source", snapshot_ref),),
            evaluator_contract={"adapter": "python", "callable": "evaluate.evaluate"},
            candidate_contract={"format": "parameters"},
        )
        runtime = PreparedEnvironmentRuntimeManifest(
            environment_revision_digest=environment.digest,
            runtime_kind="process",
            runtime_settings={"python": "managed"},
            workdir=ScopePath("source", "."),
        )
        template = RunEvaluationTemplate(
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
            objective={
                "primaryMetric": {"name": "score", "direction": "maximize"}
            },
            resource_profile={},
            sandbox_spec={},
            default_seed=0,
        )
        return RunEvaluationClosure(environment, runtime, template)

    def test_creation_is_atomic_retains_exact_closure_and_replays_exactly(self) -> None:
        created = self._create_run()
        replay = self._create_run()

        self.assertEqual(replay, created)
        connection = self._connect()
        try:
            run = connection.execute(
                "SELECT owner_id, created_txn_id FROM run_namespaces WHERE run_id = 'run-a'"
            ).fetchone()
            self.assertEqual(run[0], "run-owner-a")
            txn_id = run[1]
            self.assertEqual(
                connection.execute(
                    "SELECT revision, txn_id FROM owner_revisions "
                    "WHERE owner_id = 'run-owner-a'"
                ).fetchall(),
                [(0, txn_id)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT store_id, content_ref, role, added_revision, added_txn_id "
                    "FROM owner_memberships WHERE owner_id = 'run-owner-a'"
                ).fetchall(),
                [
                    (
                        self.bindings[0].store_id,
                        str(self.bindings[0].content_ref),
                        RUN_ENVIRONMENT_SOURCE_ROLE,
                        0,
                        txn_id,
                    ),
                    (
                        self.bindings[0].store_id,
                        str(self.bindings[0].content_ref),
                        RUN_METHOD_SOURCE_ROLE,
                        0,
                        txn_id,
                    ),
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT created_txn_id FROM run_evaluation_templates "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                txn_id,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT semantic_role, content_ref, created_txn_id "
                    "FROM run_evaluation_refs WHERE run_id = 'run-a'"
                ).fetchall(),
                [
                    (
                        RUN_ENVIRONMENT_SOURCE_ROLE,
                        str(self.bindings[0].content_ref),
                        txn_id,
                    )
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions "
                    "WHERE operation_id = 'closure-ledger/run-create'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_wrong_source_revision_or_ref_rolls_back_every_run_record(self) -> None:
        with self.assertRaises(RealmConflict):
            self._create_run(
                operation_id="closure-ledger/wrong-revision",
                run_id="wrong-revision-run",
                owner_id="wrong-revision-owner",
                source_revision=self.source_owner_revision + 1,
            )

        missing_ref = SnapshotRef.parse("tree:sha256:" + "f" * 64)
        missing_closure = self._closure_for_snapshot(
            missing_ref, environment_id="missing-source-environment"
        )
        with self.assertRaises(RealmNotFound):
            self._create_run(
                operation_id="closure-ledger/wrong-ref",
                run_id="wrong-ref-run",
                owner_id="wrong-ref-owner",
                closure=missing_closure,
                bindings=(
                    OwnerMembership(
                        self.store.store_id,
                        missing_ref,
                        RUN_ENVIRONMENT_SOURCE_ROLE,
                    ),
                ),
            )

        connection = self._connect()
        try:
            for table, identity_column, identities in (
                (
                    "run_namespaces",
                    "run_id",
                    ("wrong-revision-run", "wrong-ref-run"),
                ),
                (
                    "owners",
                    "owner_id",
                    ("wrong-revision-owner", "wrong-ref-owner"),
                ),
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE {identity_column} IN (?, ?)",
                        identities,
                    ).fetchone()[0],
                    0,
                )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM environment_revisions WHERE revision_digest = ?",
                    (missing_closure.environment_revision.digest,),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions WHERE operation_id IN "
                    "('closure-ledger/wrong-revision', 'closure-ledger/wrong-ref')"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_closure_rows_and_retained_membership_are_immutable_even_via_replace(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            environment = connection.execute(
                "SELECT * FROM environment_revisions"
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM prepared_environment_runtimes"
            ).fetchone()
            template = connection.execute(
                "SELECT * FROM run_evaluation_templates WHERE run_id = 'run-a'"
            ).fetchone()
            evaluation_ref = connection.execute(
                "SELECT * FROM run_evaluation_refs WHERE run_id = 'run-a'"
            ).fetchone()
            membership = connection.execute(
                "SELECT * FROM owner_memberships WHERE owner_id = 'run-owner-a' "
                "AND role = ?",
                (RUN_ENVIRONMENT_SOURCE_ROLE,),
            ).fetchone()

            immutable_statements = (
                (
                    "UPDATE environment_revisions SET environment_id = 'changed' "
                    "WHERE revision_digest = ?",
                    (environment[0],),
                ),
                (
                    "DELETE FROM prepared_environment_runtimes WHERE runtime_digest = ?",
                    (runtime[0],),
                ),
                (
                    "UPDATE run_evaluation_templates SET closure_json = '{}' "
                    "WHERE run_id = 'run-a'",
                    (),
                ),
                (
                    "DELETE FROM run_evaluation_refs WHERE run_id = 'run-a'",
                    (),
                ),
                (
                    "UPDATE owner_memberships SET removed_revision = 99 "
                    "WHERE owner_id = 'run-owner-a'",
                    (),
                ),
                (
                    "DELETE FROM owner_memberships WHERE owner_id = 'run-owner-a'",
                    (),
                ),
            )
            for statement, parameters in immutable_statements:
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
                    connection.rollback()

            replacement_statements = (
                (
                    "INSERT OR REPLACE INTO environment_revisions VALUES (?, ?, ?, ?)",
                    tuple(environment),
                ),
                (
                    "INSERT OR REPLACE INTO prepared_environment_runtimes VALUES (?, ?, ?, ?)",
                    tuple(runtime),
                ),
                (
                    "INSERT OR REPLACE INTO run_evaluation_templates VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(template),
                ),
                (
                    "INSERT OR REPLACE INTO run_evaluation_refs VALUES (?, ?, ?, ?)",
                    tuple(evaluation_ref),
                ),
                (
                    "INSERT OR REPLACE INTO owner_memberships(" 
                    "owner_id, store_id, content_ref, role, added_revision, "
                    "removed_revision, added_txn_id, removed_txn_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        membership[0],
                        membership[1],
                        membership[2],
                        membership[3],
                        999,
                        membership[5],
                        membership[6],
                        membership[7],
                    ),
                ),
            )
            for statement, parameters in replacement_statements:
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
                    connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT added_revision FROM owner_memberships "
                    "WHERE owner_id = 'run-owner-a'"
                ).fetchall(),
                [(0,), (0,)],
            )
        finally:
            connection.close()

    def test_selection_cannot_substitute_or_tamper_with_evaluation_template(self) -> None:
        created = self._create_run()
        selection = self._admit_parameter_candidate(created)
        resolved = self.ledger.resolve_run_candidate_evaluation(
            actor_principal_id="operator", selection=selection
        )
        self.assertEqual(resolved.evaluation.closure, self.closure)

        payload = selection.to_dict()
        payload["evaluation_template_digest"] = "0" * 64
        with self.assertRaises(RealmIntegrityError):
            RunCandidateSelection.from_dict(payload)

        substituted = RunCandidateSelection.build(
            run_id=selection.run_id,
            evaluation_template_digest="0" * 64,
            run_revision=selection.run_revision,
            owner_revision=selection.owner_revision,
            sequence=selection.sequence,
            candidate_id=selection.candidate_id,
            candidate_ref=selection.candidate_ref,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.resolve_run_candidate_evaluation(
                actor_principal_id="operator", selection=substituted
            )

    def test_source_tree_can_move_or_disappear_after_seal_and_run_still_resolves(self) -> None:
        created = self._create_run()
        selection = self._admit_parameter_candidate(created)
        moved = self.root / "moved-away-source"
        self.source_path.rename(moved)
        shutil.rmtree(moved)

        resolved = self.ledger.resolve_run_candidate_evaluation(
            actor_principal_id="operator",
            selection=selection,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(resolved.evaluation.closure, self.closure)
        self.assertEqual(resolved.evaluation.content_bindings, self.bindings)

    def test_run_definition_pins_one_store_placement_when_source_has_a_replica(self) -> None:
        store_b = LocalContentStore(self.root / "store-b", store_id="local-b")
        try:
            self.ledger.register_store(
                operation_id="closure-ledger/store-b",
                store_id=store_b.store_id,
                backend_kind=store_b.BACKEND_KIND,
                root_marker=store_b.root_marker,
            )
            change = self.ledger.begin_owner_change(
                operation_id="closure-ledger/source-add-store-b",
                actor_principal_id="operator",
                owner_id=self.source_owner_id,
                expected_owner_revision=self.source_owner_revision,
                ttl_seconds=60,
            )
            capture = store_b.capture(
                change_id=change.change_id,
                authority=self.ledger.content_capture_handle(
                    actor_principal_id="operator",
                    change_id=change.change_id,
                    store_id=store_b.store_id,
                ),
            )
            sealed = capture.seal_tree(source=AllowedTreeSource(self.source_path))
            self.assertEqual(sealed.snapshot_ref, self.bindings[0].content_ref)
            source_binding = OwnerMembership(
                store_b.store_id,
                sealed.snapshot_ref,
                "test-environment-source-b",
            )
            self.ledger.hold_owner_content(
                operation_id="closure-ledger/source-hold-store-b",
                actor_principal_id="operator",
                change_id=change.change_id,
                memberships=(source_binding,),
            )
            committed = self.ledger.commit_owner_change(
                operation_id="closure-ledger/source-commit-store-b",
                actor_principal_id="operator",
                change_id=change.change_id,
                expected_owner_revision=self.source_owner_revision,
                additions=(source_binding,),
            )
            created = self._create_run(
                source_revision=committed.owner_revision,
                bindings=self.bindings,
            )
            selection = self._admit_parameter_candidate(created)

            resolved = self.ledger.resolve_run_candidate_evaluation(
                actor_principal_id="operator", selection=selection
            )
            self.assertEqual(resolved.evaluation.content_bindings, self.bindings)
            self.assertEqual(
                {item.store_id for item in resolved.evaluation.content_bindings},
                {"local-a"},
            )
            self.assertEqual(
                {
                    item.store_id
                    for item in self.ledger.list_owner_memberships(
                        actor_principal_id="operator",
                        owner_id=self.source_owner_id,
                    )
                },
                {"local-a", "local-b"},
            )
        finally:
            store_b.close()

    def test_selection_ref_resolves_one_no_copy_inspection_target(self) -> None:
        created = self._create_run()
        legacy = self._admit_parameter_candidate(created)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=created.run.run_id
        )
        selection = self.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            kind="candidate",
            entity_id=legacy.candidate_id,
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

        target = self.ledger.resolve_candidate_inspection_target(
            actor_principal_id="operator", selection=selection
        )

        self.assertEqual(target.selection, selection)
        self.assertEqual(target.candidate.candidate_id, legacy.candidate_id)
        self.assertEqual(target.candidate_bindings, ())
        self.assertEqual(target.evaluation.closure, self.closure)
        self.assertEqual(
            target.run_definition.evaluation_closure,
            target.evaluation.closure,
        )
        self.assertTrue(target.runnable)
        connection = self._connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM managed_workspaces"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_inspection_and_canonical_trial_share_evaluation_compiler(self) -> None:
        created = self._create_run()
        legacy = self._admit_parameter_candidate(created)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=created.run.run_id
        )
        selection = self.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            kind="candidate",
            entity_id=legacy.candidate_id,
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        target = self.ledger.resolve_candidate_inspection_target(
            actor_principal_id="operator", selection=selection
        )
        inspection_spec = target.compile_evaluation_spec(
            seed=11, repetition_index=0
        )

        prepared = self.ledger.prepare_run_attempt(
            operation_id="closure-ledger/inspection-parity/prepare",
            actor_principal_id="operator",
            run_id=created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="attempt-a1",
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        self.assertEqual(inspection_spec, prepared.attempt.evaluation_spec)
        self.assertNotIn("run_id", inspection_spec.to_dict())
        self.assertNotIn("workspace", inspection_spec.to_dict())

    def test_inspection_resolver_rejects_a_validly_redigested_wrong_head(self) -> None:
        created = self._create_run()
        legacy = self._admit_parameter_candidate(created)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=created.run.run_id
        )
        selection = self.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            kind="candidate",
            entity_id=legacy.candidate_id,
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        self.assertLess(selection.entity_sequence, selection.source_sequence)
        wrong_head = SelectionRef.build(
            kind=selection.kind,
            source_kind=selection.source_kind,
            source_id=selection.source_id,
            source_owner_id=selection.source_owner_id,
            source_revision=selection.source_revision,
            owner_revision=selection.owner_revision,
            source_sequence=selection.entity_sequence,
            entity_sequence=selection.entity_sequence,
            entity_id=selection.entity_id,
            entity_ref=selection.entity_ref,
            context_digest=selection.context_digest,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.resolve_candidate_inspection_target(
                actor_principal_id="operator", selection=wrong_head
            )


if __name__ == "__main__":
    unittest.main()
