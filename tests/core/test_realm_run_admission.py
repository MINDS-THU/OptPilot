from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    RunCandidateSelection,
    SessionHandleAdmission,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        for principal in ("operator", "other"):
            self.ledger.register_principal(
                operation_id=f"principal/{principal}",
                principal_id=principal,
                kind="human",
            )
        self.ledger.register_store(
            operation_id="store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.run_closure,
            self.run_closure_bindings,
            self.run_source_owner_id,
            self.run_source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="run-admission",
        )
        self.run_control_manifest = prepare_test_run_control_manifest(
            self.run_closure, max_trials=3
        )
        self.run_definition, self.run_definition_bindings = (
            prepare_test_run_definition(
                self.run_closure,
                self.run_control_manifest,
                self.run_closure_bindings,
            )
        )
        self.run = self.ledger.create_run_namespace(
            operation_id="run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=self.run_definition,
            definition_bindings=self.run_definition_bindings,
            source_owner_id=self.run_source_owner_id,
            expected_source_owner_revision=self.run_source_owner_revision,
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
        return f"run-test/{self.counter}/{label}"

    def parameter_plan(
        self,
        *,
        candidate_id: str = "candidate-a",
        logical_trial_id: str = "trial-a",
        handle_id: str | None = "handle-a",
        value: int = 1,
        submission_metadata=None,
    ) -> RunAdmissionPlan:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": value}
        )
        return RunAdmissionPlan(
            (
                CandidateAdmission(
                    candidate_id,
                    envelope,
                    lineage={"parents": []},
                    generator={"method_id": "method-a"},
                ),
            ),
            (
                LogicalTrialAdmission(
                    logical_trial_id=logical_trial_id,
                    candidate_id=candidate_id,
                    seed=7,
                    submission_metadata=submission_metadata or {},
                ),
            ),
            ()
            if handle_id is None
            else (SessionHandleAdmission(handle_id, logical_trial_id),),
        )

    def begin_run_change(self, *, expected_owner_revision: int):
        return self.ledger.begin_owner_change(
            operation_id=self.op("begin"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=expected_owner_revision,
            ttl_seconds=60,
        )

    def commit_plan(
        self,
        plan: RunAdmissionPlan,
        *,
        change_id: str,
        expected_run_revision: int,
        expected_owner_revision: int,
        operation_id: str,
        bindings=(),
    ):
        return self.ledger.commit_run_candidate_admissions(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=expected_run_revision,
            expected_owner_revision=expected_owner_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            change_id=change_id,
            plan=plan,
            content_bindings=bindings,
        )

    def test_parameter_admission_commits_budget_handle_events_and_selection_once(self) -> None:
        self.assertEqual(
            self.ledger.create_run_namespace(
                operation_id="run/create",
                actor_principal_id="operator",
                controller_holder_id="controller-a",
                controller_ttl_seconds=60,
                run_definition=self.run_definition,
                definition_bindings=self.run_definition_bindings,
                source_owner_id=self.run_source_owner_id,
                expected_source_owner_revision=self.run_source_owner_revision,
                run_id="run-a",
                owner_id="run-owner-a",
            ),
            self.run,
        )
        change = self.begin_run_change(expected_owner_revision=0)
        plan = self.parameter_plan()
        operation_id = self.op("admit")
        receipt = self.commit_plan(
            plan,
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=operation_id,
        )
        replay = self.commit_plan(
            plan,
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=operation_id,
        )

        self.assertEqual(replay, receipt)
        self.assertEqual(receipt.run.current_revision, 1)
        self.assertEqual(receipt.run.accepted_logical_trials, 1)
        self.assertEqual(receipt.owner_commit.owner_revision, 0)
        self.assertEqual(receipt.logical_trials[0].budget_slot, 1)
        self.assertEqual(receipt.run.next_sequence, 3)
        selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator",
            run_id="run-a",
            candidate_id="candidate-a",
        )
        resolved = self.ledger.resolve_run_candidate_selection(
            actor_principal_id="operator",
            selection=selection,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(resolved.record.admission.envelope.spec["x"], 1)
        self.assertEqual(resolved.content_bindings, ())
        resolved_evaluation = self.ledger.resolve_run_candidate_evaluation(
            actor_principal_id="operator",
            selection=selection,
        )
        self.assertEqual(
            resolved_evaluation.evaluation.closure,
            self.run_closure,
        )
        self.assertEqual(
            resolved_evaluation.evaluation.content_bindings,
            self.run_closure_bindings,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.mint_run_candidate_selection(
                actor_principal_id="other",
                run_id="run-a",
                candidate_id="candidate-a",
            )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT event, sequence FROM run_events ORDER BY sequence"
                ).fetchall(),
                [("candidate_accepted", 1), ("logical_trial_accepted", 2)],
            )
            self.assertEqual(
                connection.execute("SELECT handle_id FROM run_submission_handles").fetchall(),
                [("handle-a",)],
            )
        finally:
            connection.close()

    def test_batch_admission_has_no_session_handle_and_keeps_trial_metadata(self) -> None:
        change = self.begin_run_change(expected_owner_revision=0)
        receipt = self.commit_plan(
            self.parameter_plan(
                handle_id=None,
                submission_metadata={"source": "batch", "priority": 2},
            ),
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("batch-without-handle"),
        )

        self.assertEqual(receipt.logical_trials[0].admission.candidate_id, "candidate-a")
        self.assertEqual(
            dict(receipt.logical_trials[0].admission.submission_metadata),
            {"source": "batch", "priority": 2},
        )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_submission_handles"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT session_handle FROM run_events "
                    "WHERE event = 'logical_trial_accepted'"
                ).fetchone(),
                (None,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT submission_metadata_json FROM run_logical_trials"
                ).fetchone(),
                ('{"priority":2,"source":"batch"}',),
            )
        finally:
            connection.close()

    def test_duplicate_candidate_refs_and_repeated_exact_evaluations_are_distinct(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            (
                CandidateAdmission("candidate-a", envelope),
                CandidateAdmission("candidate-b", envelope),
            ),
            (
                LogicalTrialAdmission("trial-a0", "candidate-a", seed=7),
                LogicalTrialAdmission("trial-a1", "candidate-a", seed=7),
                LogicalTrialAdmission("trial-b0", "candidate-b", seed=7),
            ),
        )
        change = self.begin_run_change(expected_owner_revision=0)
        receipt = self.commit_plan(
            plan,
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("duplicate-refs-repeated-evaluations"),
        )

        self.assertEqual(len(receipt.candidates), 2)
        self.assertEqual(
            {item.candidate_ref for item in receipt.candidates},
            {envelope.candidate_ref},
        )
        self.assertEqual(len(receipt.logical_trials), 3)
        self.assertEqual(
            [item.budget_slot for item in receipt.logical_trials],
            [1, 2, 3],
        )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT candidate_ref, COUNT(*) FROM run_candidates "
                    "GROUP BY candidate_ref"
                ).fetchall(),
                [(str(envelope.candidate_ref), 2)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trials WHERE seed_json = '7' "
                    "AND repetition_index = 0"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_submission_handles"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_file_admission_retains_exact_content_and_rejects_history_removal(self) -> None:
        self.ledger.create_owner(
            operation_id="source-owner",
            owner_id="source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "source"
        source.mkdir()
        (source / "run.py").write_text("print('ok')\n", encoding="utf-8")
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("source-begin"),
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        capture = self.store.capture(
            change_id=source_change.change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=source_change.change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_tree(source=AllowedTreeSource(source))
        source_membership = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, "source"
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("source-hold"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            memberships=(source_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("source-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )

        binding = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, RUN_CANDIDATE_ROLE
        )
        target_change = self.begin_run_change(expected_owner_revision=0)
        self.ledger.hold_owner_content(
            operation_id=self.op("target-hold"),
            actor_principal_id="operator",
            change_id=target_change.change_id,
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
            plan,
            change_id=target_change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("admit-files"),
            bindings=(binding,),
        )
        self.assertEqual(receipt.owner_commit.owner_revision, 1)
        selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator", run_id="run-a", candidate_id="files-a"
        )
        resolved = self.ledger.resolve_run_candidate_selection(
            actor_principal_id="operator",
            selection=selection,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(resolved.content_bindings, (binding,))
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-bytes"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        self.assertEqual(
            self.ledger.resolve_run_candidate_selection(
                actor_principal_id="other",
                selection=selection,
                permission=OwnerPermission.BYTES_READ,
            ).record.candidate_id,
            "files-a",
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.resolve_run_candidate_selection(
                actor_principal_id="other",
                selection=selection,
                permission=OwnerPermission.DERIVE,
            )

        removal = self.ledger.begin_owner_change(
            operation_id=self.op("remove-begin"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=2,
            ttl_seconds=60,
        )
        with self.assertRaisesRegex(
            RealmConflict, "fenced run domain transaction"
        ):
            self.ledger.commit_owner_change(
                operation_id=self.op("remove-commit"),
                actor_principal_id="operator",
                change_id=removal.change_id,
                expected_owner_revision=2,
                additions=(),
                removals=(binding,),
            )

    def test_one_store_neutral_ref_can_resolve_through_two_stores(self) -> None:
        second_store = LocalContentStore(self.root / "store-b", store_id="local-b")
        try:
            self.ledger.register_store(
                operation_id=self.op("store-b"),
                store_id=second_store.store_id,
                backend_kind=second_store.BACKEND_KIND,
                root_marker=second_store.root_marker,
            )
            self.ledger.create_owner(
                operation_id=self.op("two-store-source-owner"),
                owner_id="two-store-source-owner",
                owner_kind="workspace",
                principal_id="operator",
            )
            source = self.root / "two-store-source"
            source.mkdir()
            (source / "run.py").write_text("print('two stores')\n", encoding="utf-8")
            source_change = self.ledger.begin_owner_change(
                operation_id=self.op("two-store-source-begin"),
                actor_principal_id="operator",
                owner_id="two-store-source-owner",
                expected_owner_revision=0,
                ttl_seconds=60,
            )
            sealed = []
            for store in (self.store, second_store):
                capture = store.capture(
                    change_id=source_change.change_id,
                    authority=self.ledger.content_capture_handle(
                        actor_principal_id="operator",
                        change_id=source_change.change_id,
                        store_id=store.store_id,
                    ),
                )
                sealed.append(capture.seal_tree(source=AllowedTreeSource(source)))
            self.assertEqual(sealed[0].snapshot_ref, sealed[1].snapshot_ref)
            source_memberships = tuple(
                OwnerMembership(store.store_id, sealed[0].snapshot_ref, "source")
                for store in (self.store, second_store)
            )
            self.ledger.hold_owner_content(
                operation_id=self.op("two-store-source-hold"),
                actor_principal_id="operator",
                change_id=source_change.change_id,
                memberships=source_memberships,
            )
            self.ledger.commit_owner_change(
                operation_id=self.op("two-store-source-commit"),
                actor_principal_id="operator",
                change_id=source_change.change_id,
                expected_owner_revision=0,
                additions=source_memberships,
            )

            bindings = tuple(
                OwnerMembership(
                    store.store_id, sealed[0].snapshot_ref, RUN_CANDIDATE_ROLE
                )
                for store in (self.store, second_store)
            )
            target_change = self.begin_run_change(expected_owner_revision=0)
            self.ledger.hold_owner_content(
                operation_id=self.op("two-store-target-hold"),
                actor_principal_id="operator",
                change_id=target_change.change_id,
                memberships=bindings,
                source_owner_id="two-store-source-owner",
            )
            envelope = NormalizedCandidateEnvelope.build(
                candidate_format="files",
                spec={
                    "entrypoint": "run.py",
                    "contentRef": str(sealed[0].snapshot_ref),
                },
                content_refs=(sealed[0].snapshot_ref,),
            )
            plan = RunAdmissionPlan(
                (CandidateAdmission("files-two-store", envelope),),
                (LogicalTrialAdmission("trial-two-store", "files-two-store"),),
            )
            self.commit_plan(
                plan,
                change_id=target_change.change_id,
                expected_run_revision=0,
                expected_owner_revision=0,
                operation_id=self.op("two-store-admit"),
                bindings=bindings,
            )
            selection = self.ledger.mint_run_candidate_selection(
                actor_principal_id="operator",
                run_id="run-a",
                candidate_id="files-two-store",
            )
            resolved = self.ledger.resolve_run_candidate_selection(
                actor_principal_id="operator",
                selection=selection,
                permission=OwnerPermission.DERIVE,
            )
            self.assertEqual(resolved.content_bindings, tuple(sorted(bindings)))
            connection = sqlite3.connect(self.root / "realm.sqlite3")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM run_candidate_refs"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM owner_memberships "
                        "WHERE owner_id = ? AND role = ? AND removed_revision IS NULL",
                        (self.run.run.owner_id, RUN_CANDIDATE_ROLE),
                    ).fetchone()[0],
                    2,
                )
            finally:
                connection.close()
        finally:
            second_store.close()

    def test_duplicate_and_budget_failure_leave_run_and_owner_change_uncommitted(self) -> None:
        first_change = self.begin_run_change(expected_owner_revision=0)
        first = self.commit_plan(
            self.parameter_plan(),
            change_id=first_change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("first"),
        )
        duplicate_change = self.begin_run_change(expected_owner_revision=0)
        with self.assertRaises(RealmConflict):
            self.commit_plan(
                self.parameter_plan(
                    logical_trial_id="trial-b", handle_id="handle-b"
                ),
                change_id=duplicate_change.change_id,
                expected_run_revision=1,
                expected_owner_revision=0,
                operation_id=self.op("duplicate"),
            )
        selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator", run_id="run-a", candidate_id="candidate-a"
        )
        tampered = selection.to_dict()
        tampered["sequence"] += 1
        with self.assertRaises(Exception):
            RunCandidateSelection.from_dict(tampered)
        self.assertEqual(first.run.accepted_logical_trials, 1)

    def test_multi_candidate_plan_reserves_logical_slots_not_candidate_count(self) -> None:
        first = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        second = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 2}
        )
        plan = RunAdmissionPlan(
            (
                CandidateAdmission("candidate-a", first),
                CandidateAdmission("candidate-b", second),
            ),
            (
                LogicalTrialAdmission("trial-a0", "candidate-a", seed=0),
                LogicalTrialAdmission("trial-a1", "candidate-a", seed=1),
                LogicalTrialAdmission("trial-b0", "candidate-b", seed=0),
            ),
            (
                SessionHandleAdmission("handle-a0", "trial-a0"),
                SessionHandleAdmission("handle-a1", "trial-a1"),
                SessionHandleAdmission("handle-b0", "trial-b0"),
            ),
        )
        change = self.begin_run_change(expected_owner_revision=0)
        receipt = self.commit_plan(
            plan,
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("batch-admit"),
        )
        self.assertEqual(len(receipt.candidates), 2)
        self.assertEqual(len(receipt.logical_trials), 3)
        self.assertEqual(
            [item.budget_slot for item in receipt.logical_trials], [1, 2, 3]
        )
        self.assertEqual(receipt.run.accepted_logical_trials, 3)
        self.assertEqual(receipt.run.next_sequence, 7)
        control = self.ledger.read_run_control(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(control.current_submission.state, "draining")
        self.assertEqual(control.current_submission.stop_code, "max_trials")
        self.assertEqual(control.current_submission.run_revision, 1)

        exhausted_change = self.begin_run_change(expected_owner_revision=0)
        with self.assertRaisesRegex(RealmConflict, "submissions are closed"):
            self.commit_plan(
                self.parameter_plan(
                    candidate_id="candidate-c",
                    logical_trial_id="trial-c",
                    handle_id="handle-c",
                    value=3,
                ),
                change_id=exhausted_change.change_id,
                expected_run_revision=1,
                expected_owner_revision=0,
                operation_id=self.op("exhausted"),
            )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision, accepted_logical_trials FROM run_namespaces"
                ).fetchone(),
                (1, 3),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_candidates").fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_released_controller_fence_cannot_admit_any_state(self) -> None:
        change = self.begin_run_change(expected_owner_revision=0)
        self.ledger.release_lease(
            operation_id=self.op("release-controller"),
            actor_principal_id="operator",
            lease_id=self.run.controller_lease.lease_id,
            holder_id=self.run.controller_lease.holder_id,
            fencing_token=self.run.controller_lease.fencing_token,
        )
        with self.assertRaises(RealmConflict):
            self.commit_plan(
                self.parameter_plan(),
                change_id=change.change_id,
                expected_run_revision=0,
                expected_owner_revision=0,
                operation_id=self.op("stale-controller"),
            )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_candidates").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision, accepted_logical_trials FROM run_namespaces"
                ).fetchone(),
                (0, 0),
            )
        finally:
            connection.close()

    def test_domain_insert_failure_rolls_back_change_lease_events_and_operation(self) -> None:
        change = self.begin_run_change(expected_owner_revision=0)
        failed_operation = self.op("injected-failure")
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER fail_run_revision BEFORE INSERT ON run_revisions "
                "WHEN NEW.revision > 0 BEGIN "
                "SELECT RAISE(ABORT, 'injected run revision failure'); END"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected run revision failure"
        ):
            self.commit_plan(
                self.parameter_plan(),
                change_id=change.change_id,
                expected_run_revision=0,
                expected_owner_revision=0,
                operation_id=failed_operation,
            )

        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM owner_transactions WHERE change_id = ?",
                    (change.change_id,),
                ).fetchone()[0],
                "active",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM leases WHERE lease_id = ?",
                    (change.retention_lease_id,),
                ).fetchone()[0],
                "active",
            )
            for table in (
                "run_candidates",
                "run_logical_trials",
                "run_submission_handles",
                "run_events",
            ):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id = ?",
                    (failed_operation,),
                ).fetchone()
            )
            connection.execute("DROP TRIGGER fail_run_revision")
            connection.commit()
        finally:
            connection.close()

        receipt = self.commit_plan(
            self.parameter_plan(),
            change_id=change.change_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            operation_id=self.op("recovered-admission"),
        )
        self.assertEqual(receipt.run.current_revision, 1)

    def test_concurrent_admissions_have_one_run_revision_winner(self) -> None:
        first_change = self.begin_run_change(expected_owner_revision=0)
        second_change = self.begin_run_change(expected_owner_revision=0)
        plans = (
            self.parameter_plan(),
            self.parameter_plan(
                candidate_id="candidate-b",
                logical_trial_id="trial-b",
                handle_id="handle-b",
                value=2,
            ),
        )
        barrier = threading.Barrier(3)
        receipts = []
        errors = []

        def admit(index: int) -> None:
            barrier.wait()
            try:
                receipts.append(
                    self.commit_plan(
                        plans[index],
                        change_id=(first_change, second_change)[index].change_id,
                        expected_run_revision=0,
                        expected_owner_revision=0,
                        operation_id=f"concurrent-admission/{index}",
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=admit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RealmConflict)
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_candidates").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_revisions").fetchone()[0],
                2,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
