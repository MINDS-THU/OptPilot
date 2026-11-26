from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.review_collection_ledger import REVIEW_CANDIDATE_ROLE
from optpilot.realm.review_collection_service import RealmReviewCollectionService
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_terminal_seal import RunTerminalAnchor, RunTerminalSeal
from optpilot.realm.run_workbench import RunWorkbenchReadModel
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunRetirementTest(unittest.TestCase):
    """Run retirement releases bytes without erasing reproducibility metadata."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
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
            prefix="run-retirement",
        )
        manifest = prepare_test_run_control_manifest(
            self.run_closure, max_trials=2
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            self.run_closure, manifest, self.run_closure_bindings
        )
        self.run = self.ledger.create_run_namespace(
            operation_id="run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.run_source_owner_id,
            expected_source_owner_revision=self.run_source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.counter = 0
        self.candidate_binding = self._admit_file_candidate()
        self.selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            candidate_id="candidate-files",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"run-retirement/{self.counter}/{label}"

    def _admit_file_candidate(self) -> OwnerMembership:
        self.ledger.create_owner(
            operation_id="candidate-source/create",
            owner_id="candidate-source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "candidate-source"
        source.mkdir()
        (source / "run.py").write_text("print('candidate')\n", encoding="utf-8")
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("candidate-source-begin"),
            actor_principal_id="operator",
            owner_id="candidate-source-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
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
            self.store.store_id,
            sealed.snapshot_ref,
            "candidate-source",
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("candidate-source-hold"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            memberships=(source_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("candidate-source-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )

        binding = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            RUN_CANDIDATE_ROLE,
        )
        run_change = self.ledger.begin_owner_change(
            operation_id=self.op("run-admission-begin"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("run-admission-hold"),
            actor_principal_id="operator",
            change_id=run_change.change_id,
            memberships=(binding,),
            source_owner_id="candidate-source-owner",
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py"},
            content_refs=(sealed.snapshot_ref,),
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("run-admit"),
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            change_id=run_change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("candidate-files", envelope),),
                (LogicalTrialAdmission("trial-files", "candidate-files"),),
            ),
            content_bindings=(binding,),
        )
        return binding

    def terminalize_trial(self) -> None:
        receipt = self.ledger.cancel_run_logical_trial(
            operation_id=self.op("terminalize-trial"),
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            logical_trial_id="trial-files",
            expected_run_revision=1,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            code="admin_cancelled",
        )
        self.assertEqual(receipt.run.current_revision, 2)
        self.assertEqual(receipt.transition.outcome, "cancelled")

    def close_submissions(
        self, *, operation_id: str, expected_run_revision: int = 2
    ):
        return self.ledger.close_run_submissions(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )

    def finish(self, *, operation_id: str, expected_run_revision: int = 3):
        return self.ledger.finish_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )

    def begin_retirement(self):
        return self.ledger.begin_owner_change(
            operation_id=self.op("retirement-begin"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=1,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )

    def retire(self, *, operation_id: str, change_id: str):
        return self.ledger.retire_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=4,
            expected_owner_revision=1,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            change_id=change_id,
        )

    def test_finish_and_retire_are_replayable_and_preserve_selection_metadata(self) -> None:
        self.terminalize_trial()
        close_operation = self.op("close-submissions")
        draining = self.close_submissions(operation_id=close_operation)
        self.assertEqual(
            self.close_submissions(operation_id=close_operation), draining
        )
        finish_operation = self.op("finish")
        finished = self.finish(operation_id=finish_operation)
        self.assertEqual(self.finish(operation_id=finish_operation), finished)
        self.assertEqual(finished.run.state, "cancelled")
        self.assertEqual(finished.run.retention_state, "active")
        self.assertEqual(finished.revision.operation_kind, "run.finish")
        self.assertEqual(finished.finalization.code, "admin_cancelled")
        seal = finished.terminal_seal
        self.assertEqual(
            self.ledger.read_run_terminal_seal(
                actor_principal_id="operator", run_id="run-a"
            ),
            seal,
        )
        self.assertEqual(RunTerminalSeal.from_dict(seal.to_dict()), seal)
        self.assertEqual(
            RunTerminalAnchor.from_dict(seal.anchor.to_dict()), seal.anchor
        )

        change = self.begin_retirement()
        retire_operation = self.op("retire")
        retired = self.retire(
            operation_id=retire_operation,
            change_id=change.change_id,
        )
        self.assertEqual(
            self.retire(operation_id=retire_operation, change_id=change.change_id),
            retired,
        )
        self.assertEqual(retired.run.state, "cancelled")
        self.assertEqual(retired.run.retention_state, "retired")
        self.assertEqual(retired.run.current_revision, 5)
        self.assertEqual(retired.revision.operation_kind, "run.retire")
        self.assertEqual(retired.owner_commit.owner_revision, 2)
        self.assertEqual(retired.retirement.run_revision, 5)
        self.assertEqual(retired.retirement.owner_revision, 2)
        self.assertEqual(
            self.ledger.read_run_terminal_seal(
                actor_principal_id="operator", run_id="run-a"
            ),
            seal,
        )
        self.assertEqual(
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id="run-a"
            ).terminal_seal,
            seal,
        )

        resolved = self.ledger.resolve_run_candidate_selection(
            actor_principal_id="operator",
            selection=self.selection,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(resolved.record.candidate_id, "candidate-files")
        self.assertEqual(resolved.content_bindings, ())
        self.assertEqual(resolved.availability, "unavailable")
        resolved_evaluation = self.ledger.resolve_run_candidate_evaluation(
            actor_principal_id="operator",
            selection=self.selection,
        )
        self.assertEqual(resolved_evaluation.candidate.availability, "unavailable")
        self.assertEqual(resolved_evaluation.evaluation.availability, "unavailable")
        self.assertEqual(resolved_evaluation.evaluation.content_bindings, ())
        self.assertEqual(resolved_evaluation.evaluation.closure, self.run_closure)

        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id="candidate-source-owner",
            ),
            (
                OwnerMembership(
                    self.store.store_id,
                    self.candidate_binding.content_ref,
                    "candidate-source",
                ),
            ),
        )
        environment_source_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=self.run_source_owner_id,
        )
        self.assertEqual(len(environment_source_memberships), 1)
        self.assertEqual(
            environment_source_memberships[0].content_ref,
            self.run_closure_bindings[0].content_ref,
        )
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM owner_memberships "
                    "WHERE owner_id = ? AND removed_revision IS NULL",
                    (self.run.run.owner_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT event, code FROM run_events "
                    "WHERE event IN ('run_submissions_closed', 'run_finished', "
                    "'run_retired') ORDER BY sequence"
                ).fetchall(),
                [
                    ("run_submissions_closed", "admin_cancelled"),
                    ("run_finished", "admin_cancelled"),
                    ("run_retired", None),
                ],
            )
        finally:
            connection.close()

    def test_review_decision_content_survives_source_run_retirement(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("review-principal"),
            principal_id="operator",
            kind="human",
        )
        service = RealmReviewCollectionService(self.ledger, principal)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
        )
        candidate = RunWorkbenchReadModel.from_snapshot(snapshot).page(
            "candidate"
        )["items"][0]
        saved = service.add_candidate(
            operation_id=self.op("review-add"),
            run_id=self.run.run.run_id,
            presentation_selection=candidate["selection"],
            note="Retain this decision after the run is retired.",
        )

        # Release the original ingress owner so the Review owner is the only
        # authority left after the Run owner retires. The physical tree is not
        # copied: all three memberships refer to the same CAS object.
        source_membership = OwnerMembership(
            self.store.store_id,
            self.candidate_binding.content_ref,
            "candidate-source",
        )
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("candidate-source-release-begin"),
            actor_principal_id="operator",
            owner_id="candidate-source-owner",
            expected_owner_revision=1,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("candidate-source-release-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=(source_membership,),
        )

        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close-submissions"))
        self.finish(operation_id=self.op("finish"))
        retirement = self.begin_retirement()
        self.retire(
            operation_id=self.op("retire"),
            change_id=retirement.change_id,
        )

        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=self.run.run.owner_id,
            ),
            (),
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id="candidate-source-owner",
            ),
            (),
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=saved.owner_id,
            ),
            (
                OwnerMembership(
                    self.store.store_id,
                    self.candidate_binding.content_ref,
                    REVIEW_CANDIDATE_ROLE,
                ),
            ),
        )
        self.store.verify_tree(self.candidate_binding.content_ref)
        retained = service.read_for_run(run_id=self.run.run.run_id, revision=1)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.revision_digest, saved.revision_digest)
        self.assertEqual(
            retained.items[0].note,
            "Retain this decision after the run is retired.",
        )

    def test_review_collection_deletion_releases_owner_and_allows_new_review(
        self,
    ) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("review-delete-principal"),
            principal_id="operator",
            kind="human",
        )
        service = RealmReviewCollectionService(self.ledger, principal)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
        )
        candidate = RunWorkbenchReadModel.from_snapshot(snapshot).page(
            "candidate"
        )["items"][0]
        saved = service.add_candidate(
            operation_id=self.op("review-delete-add"),
            run_id=self.run.run.run_id,
            presentation_selection=candidate["selection"],
        )
        other_principal = self.ledger.register_principal(
            operation_id=self.op("review-delete-other-principal"),
            principal_id="other-operator",
            kind="human",
        )
        other_service = RealmReviewCollectionService(
            self.ledger,
            other_principal,
        )
        with self.assertRaises(RealmNotFound):
            other_service.delete_for_run(
                operation_id=self.op("review-delete-unauthorized"),
                run_id=self.run.run.run_id,
                collection_id=saved.collection_id,
                expected_revision=saved.revision,
                expected_revision_digest=saved.revision_digest,
            )
        delete_operation = self.op("review-delete")
        deleted = service.delete_for_run(
            operation_id=delete_operation,
            run_id=self.run.run.run_id,
            collection_id=saved.collection_id,
            expected_revision=saved.revision,
            expected_revision_digest=saved.revision_digest,
        )
        replay = self.ledger.delete_review_collection(
            operation_id=delete_operation,
            actor_principal_id="operator",
            collection_id=saved.collection_id,
            primary_source_kind="run",
            primary_source_id=self.run.run.run_id,
            expected_revision=saved.revision,
            expected_revision_digest=saved.revision_digest,
        )

        self.assertEqual(replay, deleted)
        self.assertEqual(deleted.released_memberships, 1)
        self.assertEqual(
            deleted.previous_revision_digest,
            saved.revision_digest,
        )
        self.assertIsNone(service.read_for_run(run_id=self.run.run.run_id))
        self.assertIsNone(service.history_for_run(run_id=self.run.run.run_id))
        with self.assertRaises(RealmNotFound):
            self.ledger.read_review_collection(
                actor_principal_id="operator",
                collection_id=saved.collection_id,
            )

        replacement = service.add_candidate(
            operation_id=self.op("review-delete-readd"),
            run_id=self.run.run.run_id,
            presentation_selection=candidate["selection"],
        )
        self.assertNotEqual(replacement.collection_id, saved.collection_id)
        self.assertEqual(replacement.revision, 1)

    def test_terminal_seal_ignores_post_finish_controller_authority(self) -> None:
        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close-submissions"))
        finished = self.finish(operation_id=self.op("finish"))
        seal = finished.terminal_seal

        replacement = self.ledger.replace_run_controller(
            operation_id=self.op("replace-terminal-controller"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_controller_generation=1,
            expected_controller_lease_id=self.run.controller_lease.lease_id,
            expected_controller_holder_id=self.run.controller_lease.holder_id,
            expected_controller_fencing_token=(
                self.run.controller_lease.fencing_token
            ),
            new_controller_holder_id="controller-b",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )

        self.assertEqual(replacement.run.current_revision, 5)
        self.assertEqual(
            self.ledger.read_run_terminal_seal(
                actor_principal_id="operator", run_id="run-a"
            ),
            seal,
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.terminal_seal, seal)
        self.assertEqual(snapshot.terminal_seal.anchor, seal.anchor)

    def test_finish_and_retire_reject_stale_revisions_and_fences(self) -> None:
        with self.assertRaises(RealmConflict):
            self.ledger.finish_run(
                operation_id=self.op("finish-nonterminal"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=1,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                terminal_state="succeeded",
                code="completed",
            )
        self.terminalize_trial()
        with self.assertRaises(ValueError):
            self.ledger.finish_run(
                operation_id=self.op("finish-invalid-state"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=2,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                terminal_state="running",
                code="not-terminal",
            )
        with self.assertRaises(RealmConflict):
            self.ledger.finish_run(
                operation_id=self.op("finish-stale-fence"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=2,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token + 1,
                terminal_state="failed",
                code="failed",
            )
        with self.assertRaises(RealmConflict):
            self.finish(
                operation_id=self.op("finish-stale-revision"),
                expected_run_revision=1,
            )
        with self.assertRaises(RealmConflict):
            self.finish(
                operation_id=self.op("finish-before-close"),
                expected_run_revision=2,
            )
        self.close_submissions(operation_id=self.op("close-submissions"))
        finished = self.finish(operation_id=self.op("finish-current"))
        self.assertEqual(finished.run.current_revision, 4)

        change = self.begin_retirement()
        with self.assertRaises(RealmConflict):
            self.ledger.retire_run(
                operation_id=self.op("retire-stale-fence"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=4,
                expected_owner_revision=1,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token + 1,
                change_id=change.change_id,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.retire_run(
                operation_id=self.op("retire-stale-revision"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=3,
                expected_owner_revision=1,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                change_id=change.change_id,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.retire_run(
                operation_id=self.op("retire-stale-owner-revision"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=4,
                expected_owner_revision=0,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                change_id=change.change_id,
            )

        retired = self.retire(
            operation_id=self.op("retire-current"),
            change_id=change.change_id,
        )
        self.assertEqual(retired.run.retention_state, "retired")

    def test_retire_rejects_running_run_and_nonempty_provisional_change(self) -> None:
        running_change = self.begin_retirement()
        with self.assertRaises(RealmConflict):
            self.ledger.retire_run(
                operation_id=self.op("retire-running"),
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=1,
                expected_owner_revision=1,
                controller_lease_id=self.run.controller_lease.lease_id,
                controller_holder_id=self.run.controller_lease.holder_id,
                controller_fencing_token=self.run.controller_lease.fencing_token,
                change_id=running_change.change_id,
            )
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-running-retirement"),
            actor_principal_id="operator",
            change_id=running_change.change_id,
        )

        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close-submissions"))
        self.finish(operation_id=self.op("finish"))
        nonempty_change = self.begin_retirement()
        self.ledger.hold_owner_content(
            operation_id=self.op("retirement-hold"),
            actor_principal_id="operator",
            change_id=nonempty_change.change_id,
            memberships=(self.candidate_binding,),
            source_owner_id="candidate-source-owner",
        )
        with self.assertRaises(RealmConflict):
            self.retire(
                operation_id=self.op("retire-nonempty-change"),
                change_id=nonempty_change.change_id,
            )

    def test_retire_rejects_an_active_noncontroller_child_lease(self) -> None:
        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close-submissions"))
        self.finish(operation_id=self.op("finish"))
        child = self.ledger.acquire_lease(
            operation_id=self.op("worker-lease"),
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            parent_lease_id=self.run.controller_lease.lease_id,
            lease_kind="attempt-worker",
            audience="realm-ledger",
            holder_id="worker-a",
            scope_key=f"run:{self.run.run.run_id}/attempt:1",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        change = self.begin_retirement()
        with self.assertRaises(RealmConflict):
            self.retire(
                operation_id=self.op("retire-active-worker"),
                change_id=change.change_id,
            )
        self.ledger.release_lease(
            operation_id=self.op("release-worker"),
            actor_principal_id="operator",
            lease_id=child.lease_id,
            holder_id=child.holder_id,
            fencing_token=child.fencing_token,
        )
        retired = self.retire(
            operation_id=self.op("retire-after-worker"),
            change_id=change.change_id,
        )
        self.assertEqual(retired.run.retention_state, "retired")

    def test_direct_run_state_retention_and_membership_mutation_is_rejected(self) -> None:
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE run_namespaces SET state = 'failed' WHERE run_id = 'run-a'"
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE run_namespaces SET retention_state = 'retired' "
                    "WHERE run_id = 'run-a'"
                )
            connection.rollback()
            membership_key = connection.execute(
                "SELECT store_id, content_ref, role, added_revision "
                "FROM owner_memberships "
                "WHERE owner_id = ? AND role = ? AND removed_revision IS NULL",
                (self.run.run.owner_id, RUN_CANDIDATE_ROLE),
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE owner_memberships SET removed_revision = 99 "
                    "WHERE owner_id = ? AND store_id = ? AND content_ref = ? "
                    "AND role = ? AND added_revision = ?",
                    (self.run.run.owner_id, *membership_key),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM owner_memberships WHERE owner_id = ? "
                    "AND store_id = ? AND content_ref = ? AND role = ? "
                    "AND added_revision = ?",
                    (self.run.run.owner_id, *membership_key),
                )
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
