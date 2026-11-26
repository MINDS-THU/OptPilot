"""Security and no-copy proofs for stable SelectionRef Open/Keep derivation."""

from __future__ import annotations

from dataclasses import replace
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from optpilot.attempts import (
    AttemptEnvelope,
    AttemptFinalization,
    CapturedArtifact,
    OutputDeclaration,
)
from optpilot.realm.content import (
    AllowedFileSource,
    AllowedTreeSource,
    LocalContentStore,
)
from optpilot.realm.errors import RealmConflict, RealmExpired, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.projection_service import (
    RealmProjectionService,
    SelectionProjectionUnavailable,
)
from optpilot.realm.run_attempt_records import RUN_ARTIFACT_ROLE
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.selection_service import RealmSelectionActionService
from optpilot.realm.selections import SelectionRef
from optpilot.realm.workspaces import (
    WORKSPACE_REVISION_ROLE,
    WorkspaceSelectionLineage,
)
from tests.realm_run_support import (
    TEST_EXPIRY_TTL_SECONDS,
    TEST_EXPIRY_WAIT_SECONDS,
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmSelectionDerivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.principals = {}
        for principal in ("operator", "other"):
            self.principals[principal] = self.ledger.register_principal(
                operation_id=f"selection/principal/{principal}",
                principal_id=principal,
                kind="human",
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="selection/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            closure_bindings,
            source_owner_id,
            source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="selection-files",
            candidate_contract={"format": "files"},
        )
        manifest = prepare_test_run_control_manifest(self.closure, max_trials=10)
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, manifest, closure_bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="selection/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-files",
            owner_id="run-files-owner",
        )
        self.operation_index = 0
        self.run_revision = 0
        self.owner_revision = 0
        self.candidate_root, admission = self._admit_file_candidate()
        self.admission_head = (
            admission.revision.revision,
            admission.revision.last_sequence,
        )
        self.tree_artifact_id, self.file_artifact_id = self._adopt_artifacts()
        self.service = RealmSelectionActionService(
            self.ledger, self.principals["operator"]
        )
        self.other_service = RealmSelectionActionService(
            self.ledger, self.principals["other"]
        )
        self.projection_service = None
        self.projections = []

    def tearDown(self) -> None:
        for projection in reversed(self.projections):
            if not projection.closed:
                projection.close()
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _projection_service(self) -> RealmProjectionService:
        if self.projection_service is None:
            self.projection_service = RealmProjectionService(
                self.ledger,
                local_stores={self.store.store_id: self.store},
                projection_root=self.root / "projections",
            )
        return self.projection_service

    def _table_count(self, table: str) -> int:
        if table not in {"managed_workspaces", "content_objects", "owners"}:
            raise AssertionError("unsupported test table")
        connection = self.ledger._connect()
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()

    def op(self, label: str) -> str:
        self.operation_index += 1
        return f"selection/{self.operation_index}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _retire_source_run(self) -> None:
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("projection-retire-close"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            stop_code="method_completed",
            **self.controller_arguments(),
        )
        finished = self.ledger.finish_run(
            operation_id=self.op("projection-retire-finish"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=closed.revision.revision,
            terminal_state="succeeded",
            code="method_completed",
            **self.controller_arguments(),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("projection-retire-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        retired = self.ledger.retire_run(
            operation_id=self.op("projection-retire-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=finished.revision.revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            **self.controller_arguments(),
        )
        self.run_revision = retired.revision.revision
        self.owner_revision = retired.owner_commit.owner_revision

    def _publish_source_tree(
        self, *, owner_id: str, directory_name: str, role: str
    ) -> OwnerMembership:
        self.ledger.create_owner(
            operation_id=self.op(f"create-{owner_id}"),
            owner_id=owner_id,
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / directory_name
        source.mkdir()
        (source / "run.py").write_text("print('candidate')\n", encoding="utf-8")
        change = self.ledger.begin_owner_change(
            operation_id=self.op(f"begin-{owner_id}"),
            actor_principal_id="operator",
            owner_id=owner_id,
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
        membership = OwnerMembership(self.store.store_id, sealed.snapshot_ref, role)
        self.ledger.hold_owner_content(
            operation_id=self.op(f"hold-{owner_id}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op(f"commit-{owner_id}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return membership

    def _admit_file_candidate(self):
        source = self._publish_source_tree(
            owner_id="candidate-source-owner",
            directory_name="candidate-source",
            role="candidate-source",
        )
        binding = OwnerMembership(
            source.store_id, source.content_ref, RUN_CANDIDATE_ROLE
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("admission-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(binding,),
            source_owner_id="candidate-source-owner",
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py"},
            content_refs=(source.content_ref,),
        )
        receipt = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("candidate-files", envelope),),
                (LogicalTrialAdmission("trial-files", "candidate-files"),),
            ),
            content_bindings=(binding,),
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision
        return source.content_ref, receipt

    def _capture_tree_artifact(self, change_id: str):
        tree = self.root / "artifact-tree"
        tree.mkdir()
        (tree / "model.json").write_text('{"ok":true}\n', encoding="utf-8")
        capture = self.store.capture(
            change_id=change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_tree(source=AllowedTreeSource(tree))
        declaration = OutputDeclaration(
            declaration_id="environment:bundle",
            name="bundle",
            path="bundle",
            kind="tree",
            media_type="application/vnd.optpilot.tree",
        )
        captured = CapturedArtifact(
            declaration=declaration,
            content_ref=str(sealed.snapshot_ref),
            size_bytes=next(
                item.logical_bytes
                for item in sealed.publications
                if item.content_ref == sealed.snapshot_ref
            ),
            bindings=(
                {
                    "store_id": self.store.store_id,
                    "content_ref": str(sealed.snapshot_ref),
                },
            ),
            visibility="operator",
        )
        return declaration, captured, OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, RUN_ARTIFACT_ROLE
        )

    def _capture_file_artifact(self, change_id: str):
        directory = self.root / "artifact-file"
        directory.mkdir()
        (directory / "report.txt").write_text("report\n", encoding="utf-8")
        capture = self.store.capture(
            change_id=change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_blob(
            source=AllowedFileSource(directory, "report.txt")
        )
        declaration = OutputDeclaration(
            declaration_id="environment:report",
            name="report",
            path="report.txt",
            kind="file",
            media_type="text/plain",
        )
        captured = CapturedArtifact(
            declaration=declaration,
            content_ref=str(sealed.blob_ref),
            size_bytes=sealed.publication.logical_bytes,
            bindings=(
                {
                    "store_id": self.store.store_id,
                    "content_ref": str(sealed.blob_ref),
                },
            ),
            visibility="operator",
        )
        return declaration, captured, OwnerMembership(
            self.store.store_id, sealed.blob_ref, RUN_ARTIFACT_ROLE
        )

    def _adopt_artifacts(self) -> tuple[str, str]:
        prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("attempt-prepare"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-files",
            attempt_id="attempt-files-1",
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = prepared.revision.revision
        # Selection derivation depends on adoption, not process launch.  The
        # strict provider-backed launch path has its own binding-ledger tests.
        tree = self._capture_tree_artifact(prepared.attempt.capture_change_id)
        file = self._capture_file_artifact(prepared.attempt.capture_change_id)
        self.ledger.hold_owner_content(
            operation_id=self.op("artifact-hold"),
            actor_principal_id="operator",
            change_id=prepared.attempt.capture_change_id,
            memberships=(tree[2], file[2]),
        )
        declarations = (tree[0], file[0])
        envelope = AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {}, "metadata": {}},
            metric_values={"score": 1.0},
            constraint_results={},
            output_declarations=declarations,
            event_summary={},
            execution_metadata={},
        )
        receipt = self.ledger.adopt_run_attempt(
            operation_id=self.op("attempt-adopt"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=prepared.attempt.capture_change_id,
            finalization=AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome="success",
                effective_code=None,
                captured_artifacts=(tree[1], file[1]),
                envelope=envelope,
            ),
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision
        by_declaration = {
            item.declaration_id: item.artifact_id for item in receipt.artifacts
        }
        return (
            by_declaration["environment:bundle"],
            by_declaration["environment:report"],
        )

    def _select(self, kind: str, entity_id: str) -> SelectionRef:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        return self.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            kind=kind,
            entity_id=entity_id,
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

    def test_stale_presentation_is_fenced_and_head_and_entity_sequences_are_distinct(self) -> None:
        with self.assertRaisesRegex(RealmConflict, "presentation head changed"):
            self.ledger.mint_run_selection(
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                kind="candidate",
                entity_id="candidate-files",
                expected_run_revision=self.admission_head[0],
                expected_head_sequence=self.admission_head[1],
            )

        selection = self._select("candidate", "candidate-files")
        self.assertGreater(selection.source_sequence, selection.entity_sequence)
        self.assertEqual(selection.entity_sequence, self.admission_head[1] - 1)
        self.assertEqual(SelectionRef.from_dict(selection.to_dict()), selection)
        with self.assertRaisesRegex(ValueError, "digest differs"):
            replace(selection, entity_id="candidate-other")

    def test_open_is_protected_read_only_and_creates_no_durable_state(self) -> None:
        selection = self._select("candidate", "candidate-files")
        before_refs = tuple(self.store.iter_live_refs())
        before_objects = self._table_count("content_objects")
        before_owners = self._table_count("owners")
        before_workspaces = self._table_count("managed_workspaces")
        with self.assertRaises(RealmNotFound):
            self.other_service.open_read_only(selection=selection)

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-bytes"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        result = self.other_service.open_read_only(selection=selection)
        self.assertTrue(result.eligibility.eligible)
        self.assertIsNotNone(result.view)
        self.assertFalse(result.view.writable)
        self.assertFalse(result.view.durable)
        self.assertEqual(result.view.root_ref, self.candidate_root)
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(self._table_count("content_objects"), before_objects)
        self.assertEqual(self._table_count("owners"), before_owners)
        self.assertEqual(self._table_count("managed_workspaces"), before_workspaces)

        # The descriptor is not a bearer capability.  A later Open resolves
        # the immutable selection through current authority again.
        self.ledger.revoke_owner_permission(
            operation_id=self.op("revoke-bytes"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        with self.assertRaises(RealmNotFound):
            self.other_service.open_read_only(selection=selection)
        with self.assertRaises(RealmNotFound):
            self.other_service.keep_as_editable_workspace(
                operation_id=self.op("unauthorized-keep"),
                selection=selection,
                title="Forbidden",
            )

    def test_exact_keep_replay_survives_permission_revocation(self) -> None:
        selection = self._select("candidate", "candidate-files")
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-other-derive"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = self.op("other-keep")
        kept = self.other_service.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title="Delegated keep",
            workspace_id="workspace-delegated",
            owner_id="workspace-delegated-owner",
        )
        self.assertTrue(kept.eligibility.eligible)
        self.ledger.revoke_owner_permission(
            operation_id=self.op("revoke-other-derive"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.DERIVE,
        )
        replay = self.other_service.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title="Delegated keep",
            workspace_id="workspace-delegated",
            owner_id="workspace-delegated-owner",
        )
        self.assertEqual(replay, kept)
        with self.assertRaises(RealmNotFound):
            self.other_service.keep_as_editable_workspace(
                operation_id=self.op("new-other-keep"),
                selection=selection,
                title="No longer delegated",
            )

    def test_keep_reuses_bytes_is_idempotent_and_survives_later_head_and_retirement(self) -> None:
        selection = self._select("candidate", "candidate-files")
        self.ledger.grant_owner_permission(
            operation_id=self.op("advance-owner-revision"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.METADATA_READ,
        )
        self.owner_revision = self.ledger.read_owner(
            actor_principal_id="operator", owner_id=self.created.run.owner_id
        ).revision
        self.assertGreater(self.owner_revision, selection.owner_revision)
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("close-submissions"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            stop_code="method_completed",
            **self.controller_arguments(),
        )
        self.run_revision = closed.revision.revision
        self.assertGreater(self.run_revision, selection.source_revision)
        before_refs = tuple(self.store.iter_live_refs())
        before_objects = self._table_count("content_objects")
        before_owners = self._table_count("owners")
        before_workspaces = self._table_count("managed_workspaces")
        operation_id = self.op("keep-candidate")
        first = self.service.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title="Kept candidate",
            workspace_id="workspace-candidate",
            owner_id="workspace-candidate-owner",
        )
        replay = self.service.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title="Kept candidate",
            workspace_id="workspace-candidate",
            owner_id="workspace-candidate-owner",
        )
        self.assertEqual(replay, first)
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(self._table_count("content_objects"), before_objects)
        self.assertEqual(self._table_count("owners"), before_owners + 1)
        self.assertEqual(
            self._table_count("managed_workspaces"), before_workspaces + 1
        )
        revision = first.workspace.revision
        self.assertNotEqual(
            first.workspace.workspace.owner_id, selection.source_owner_id
        )
        self.assertEqual(revision.root_ref, self.candidate_root)
        self.assertIsInstance(revision.lineage, WorkspaceSelectionLineage)
        self.assertEqual(revision.lineage.selection, selection)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id="workspace-candidate-owner",
            ),
            (
                OwnerMembership(
                    self.store.store_id,
                    self.candidate_root,
                    WORKSPACE_REVISION_ROLE,
                ),
            ),
        )
        source_after_keep = self.service.open_read_only(selection=selection)
        self.assertTrue(source_after_keep.eligibility.eligible)
        self.assertEqual(source_after_keep.view.root_ref, self.candidate_root)

        finished = self.ledger.finish_run(
            operation_id=self.op("finish"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            terminal_state="succeeded",
            code="method_completed",
            **self.controller_arguments(),
        )
        self.run_revision = finished.revision.revision
        change = self.ledger.begin_owner_change(
            operation_id=self.op("retire-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        retired = self.ledger.retire_run(
            operation_id=self.op("retire"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            **self.controller_arguments(),
        )
        self.owner_revision = retired.owner_commit.owner_revision
        replay_after_retirement = self.service.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title="Kept candidate",
            workspace_id="workspace-candidate",
            owner_id="workspace-candidate-owner",
        )
        self.assertEqual(replay_after_retirement, first)
        new_after_retirement = self.service.keep_as_editable_workspace(
            operation_id=self.op("new-keep-after-retirement"),
            selection=selection,
            title="Unavailable candidate",
        )
        self.assertFalse(new_after_retirement.eligibility.eligible)
        self.assertEqual(
            new_after_retirement.eligibility.code,
            "selection_content_unavailable",
        )
        self.assertIsNone(new_after_retirement.workspace)
        unavailable = self.ledger.resolve_selection(
            actor_principal_id="operator",
            selection=selection,
            permission=OwnerPermission.BYTES_READ,
        )
        self.assertFalse(unavailable.eligibility.eligible)
        self.assertEqual(
            unavailable.eligibility.code, "selection_content_unavailable"
        )
        workspace, kept_revision = self.ledger.read_workspace(
            actor_principal_id="operator", workspace_id="workspace-candidate"
        )
        self.assertEqual(workspace.owner_id, "workspace-candidate-owner")
        self.assertEqual(kept_revision.root_ref, self.candidate_root)
        self.assertIsInstance(kept_revision.lineage, WorkspaceSelectionLineage)
        self.assertEqual(kept_revision.lineage.selection, selection)
        self.assertTrue(
            self.ledger.resolve_content_closure(
                actor_principal_id="operator",
                owner_id=workspace.owner_id,
                store_id=kept_revision.root_store_id,
                root_ref=kept_revision.root_ref,
            )
        )

    def test_tree_artifact_uses_same_open_keep_path_and_file_is_explicitly_unsupported(self) -> None:
        tree_selection = self._select("artifact", self.tree_artifact_id)
        tree_open = self.service.open_read_only(selection=tree_selection)
        self.assertTrue(tree_open.eligibility.eligible)
        kept = self.service.keep_as_editable_workspace(
            operation_id=self.op("keep-tree-artifact"),
            selection=tree_selection,
            title="Kept artifact",
            workspace_id="workspace-artifact",
            owner_id="workspace-artifact-owner",
        )
        self.assertTrue(kept.eligibility.eligible)
        self.assertEqual(
            kept.workspace.revision.root_ref, tree_open.view.root_ref
        )

        file_selection = self._select("artifact", self.file_artifact_id)
        file_open = self.service.open_read_only(selection=file_selection)
        self.assertFalse(file_open.eligibility.supported)
        self.assertFalse(file_open.eligibility.eligible)
        self.assertEqual(file_open.eligibility.code, "file_artifact_not_tree")
        self.assertIsNone(file_open.view)
        file_keep = self.service.keep_as_editable_workspace(
            operation_id=self.op("keep-file-artifact"),
            selection=file_selection,
            title="Cannot keep file",
        )
        self.assertEqual(file_keep.eligibility, file_open.eligibility)
        self.assertIsNone(file_keep.workspace)

    def test_concurrent_keep_and_source_retirement_are_serialized_safely(self) -> None:
        selection = self._select("candidate", "candidate-files")
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("race-close"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            stop_code="method_completed",
            **self.controller_arguments(),
        )
        finished = self.ledger.finish_run(
            operation_id=self.op("race-finish"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=closed.revision.revision,
            terminal_state="succeeded",
            code="method_completed",
            **self.controller_arguments(),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("race-retire-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        keep_operation = self.op("race-keep")
        retire_operation = self.op("race-retire")
        barrier = threading.Barrier(3)
        keeps = []
        retirements = []
        errors = []

        def keep() -> None:
            barrier.wait()
            try:
                keeps.append(
                    self.service.keep_as_editable_workspace(
                        operation_id=keep_operation,
                        selection=selection,
                        title="Raced keep",
                        workspace_id="workspace-race",
                        owner_id="workspace-race-owner",
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def retire() -> None:
            barrier.wait()
            try:
                retirements.append(
                    self.ledger.retire_run(
                        operation_id=retire_operation,
                        actor_principal_id="operator",
                        run_id=self.created.run.run_id,
                        expected_run_revision=finished.revision.revision,
                        expected_owner_revision=self.owner_revision,
                        change_id=change.change_id,
                        **self.controller_arguments(),
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=keep), threading.Thread(target=retire)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(keeps), 1)
        self.assertEqual(len(retirements), 1)
        if keeps[0].eligibility.eligible:
            self.assertIsNotNone(keeps[0].workspace)
            self.assertEqual(
                keeps[0].workspace.revision.root_ref, self.candidate_root
            )
        else:
            self.assertEqual(
                keeps[0].eligibility.code, "selection_content_unavailable"
            )
            self.assertIsNone(keeps[0].workspace)

    @unittest.skipIf(os.name == "nt", "The secure local projection is POSIX-only.")
    def test_selection_projection_accepts_bytes_read_without_workspace_or_reingest(self) -> None:
        selection = self._select("candidate", "candidate-files")
        self.ledger.grant_owner_permission(
            operation_id=self.op("projection-grant-bytes"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        service = self._projection_service()
        before_refs = tuple(self.store.iter_live_refs())
        before_objects = self._table_count("content_objects")
        before_owners = self._table_count("owners")
        before_workspaces = self._table_count("managed_workspaces")
        with mock.patch.object(
            service._provider, "project", wraps=service._provider.project
        ) as provider:
            projection = service.project_selection_read_only(
                operation_id=self.op("projection-bytes-open"),
                actor_principal_id="other",
                selection=selection,
                holder_id="selection-viewer-bytes",
                ttl_seconds=TEST_LEASE_TTL_SECONDS,
            )
        self.projections.append(projection)

        self.assertEqual(
            (projection.root_path / "run.py").read_text(encoding="utf-8"),
            "print('candidate')\n",
        )
        self.assertEqual(provider.call_count, 1)
        portable = projection.portable_record()
        self.assertNotIn("provider_kind", portable)
        self.assertEqual(
            projection.realization.provider_kind, service._provider.PROVIDER_KIND
        )
        self.assertEqual(portable["copied_file_count"], 1)
        self.assertGreater(portable["copied_logical_bytes"], 0)
        consumers = self.ledger.list_projection_consumers(
            actor_principal_id="other",
            realization_id=projection.realization.realization_id,
        )
        self.assertEqual(len(consumers), 1)
        self.assertEqual(
            consumers[0].to_dict()["metadata"]["selection_ref"],
            selection.to_dict(),
        )
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(self._table_count("content_objects"), before_objects)
        self.assertEqual(self._table_count("owners"), before_owners)
        self.assertEqual(self._table_count("managed_workspaces"), before_workspaces)
        with self.assertRaises(RealmNotFound):
            self.other_service.keep_as_editable_workspace(
                operation_id=self.op("projection-bytes-cannot-keep"),
                selection=selection,
                title="Forbidden keep",
            )

    @unittest.skipIf(os.name == "nt", "The secure local projection is POSIX-only.")
    def test_selection_projection_accepts_direct_derive_without_bytes_grant(self) -> None:
        selection = self._select("artifact", self.tree_artifact_id)
        self.ledger.grant_owner_permission(
            operation_id=self.op("projection-grant-derive"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.DERIVE,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.resolve_selection(
                actor_principal_id="other",
                selection=selection,
                permission=OwnerPermission.BYTES_READ,
            )
        descriptor = self.other_service.open_read_only(selection=selection)
        self.assertTrue(descriptor.eligibility.eligible)

        projection = self._projection_service().project_selection_read_only(
            operation_id=self.op("projection-derive-open"),
            actor_principal_id="other",
            selection=selection,
            holder_id="selection-viewer-derive",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.projections.append(projection)
        self.assertEqual(
            (projection.root_path / "model.json").read_text(encoding="utf-8"),
            '{"ok":true}\n',
        )

    @unittest.skipIf(os.name == "nt", "The secure local projection is POSIX-only.")
    def test_selection_projection_hides_unauthorized_and_stale_refs_identically(self) -> None:
        selection = self._select("candidate", "candidate-files")
        stale = SelectionRef.build(
            kind=selection.kind,
            source_kind=selection.source_kind,
            source_id=selection.source_id,
            source_owner_id=selection.source_owner_id,
            source_revision=selection.source_revision + 1000,
            owner_revision=selection.owner_revision,
            source_sequence=selection.source_sequence,
            entity_sequence=selection.entity_sequence,
            entity_id=selection.entity_id,
            entity_ref=selection.entity_ref,
            context_digest=selection.context_digest,
            relative_path=selection.relative_path,
        )
        service = self._projection_service()
        with mock.patch.object(
            service._provider, "project", wraps=service._provider.project
        ) as provider:
            with self.assertRaises(RealmNotFound) as unauthorized:
                service.project_selection_read_only(
                    operation_id=self.op("projection-unauthorized"),
                    actor_principal_id="other",
                    selection=selection,
                    holder_id="unauthorized-viewer",
                )
            with self.assertRaises(RealmNotFound) as missing:
                service.project_selection_read_only(
                    operation_id=self.op("projection-stale"),
                    actor_principal_id="operator",
                    selection=stale,
                    holder_id="stale-viewer",
                )
        self.assertEqual(type(unauthorized.exception), type(missing.exception))
        self.assertEqual(str(unauthorized.exception), str(missing.exception))
        self.assertEqual(provider.call_count, 0)

    @unittest.skipIf(os.name == "nt", "The secure local projection is POSIX-only.")
    def test_selection_projection_consumer_close_and_ttl_are_enforced(self) -> None:
        selection = self._select("candidate", "candidate-files")
        service = self._projection_service()
        closed = service.project_selection_read_only(
            operation_id=self.op("projection-close"),
            actor_principal_id="operator",
            selection=selection,
            holder_id="closing-viewer",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.projections.append(closed)
        closed.close()
        with self.assertRaises(RealmExpired):
            _ = closed.root_path

        expiring = service.project_selection_read_only(
            operation_id=self.op("projection-expire"),
            actor_principal_id="operator",
            selection=selection,
            holder_id="expiring-viewer",
            ttl_seconds=TEST_EXPIRY_TTL_SECONDS,
        )
        self.projections.append(expiring)
        time.sleep(TEST_EXPIRY_WAIT_SECONDS)
        with self.assertRaises(RealmExpired):
            expiring.validate()
        expiring.close()
        self.assertTrue(expiring.closed)

    @unittest.skipIf(os.name == "nt", "The secure local projection is POSIX-only.")
    def test_source_retirement_makes_new_selection_projection_unavailable(self) -> None:
        selection = self._select("candidate", "candidate-files")
        service = self._projection_service()
        with mock.patch.object(
            service._provider, "project", wraps=service._provider.project
        ) as provider:
            active = service.project_selection_read_only(
                operation_id=self.op("projection-before-retirement"),
                actor_principal_id="operator",
                selection=selection,
                holder_id="retained-viewer",
                ttl_seconds=TEST_EXPIRY_TTL_SECONDS,
            )
            self.projections.append(active)
            active.close()
            time.sleep(TEST_EXPIRY_WAIT_SECONDS)
            self._retire_source_run()
            with self.assertRaises(SelectionProjectionUnavailable) as unavailable:
                service.project_selection_read_only(
                    operation_id=self.op("projection-after-retirement"),
                    actor_principal_id="operator",
                    selection=selection,
                    holder_id="late-viewer",
                    ttl_seconds=TEST_LEASE_TTL_SECONDS,
                )
        self.assertEqual(
            unavailable.exception.eligibility.code,
            "selection_content_unavailable",
        )
        self.assertEqual(provider.call_count, 1)

    def test_parameter_candidate_returns_typed_not_tree_reason(self) -> None:
        closure, bindings, source_owner, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="selection-parameters",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=1)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id=self.op("parameter-run-create"),
            actor_principal_id="operator",
            controller_holder_id="controller-parameters",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner,
            expected_source_owner_revision=source_revision,
            run_id="run-parameters",
            owner_id="run-parameters-owner",
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("parameter-admission-begin"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("parameter-admission"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        "candidate-parameters",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 1}
                        ),
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        "trial-parameters", "candidate-parameters"
                    ),
                ),
            ),
        )
        selection = self.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            kind="candidate",
            entity_id="candidate-parameters",
            expected_run_revision=admitted.revision.revision,
            expected_head_sequence=admitted.revision.last_sequence,
        )
        result = self.service.open_read_only(selection=selection)
        self.assertFalse(result.eligibility.supported)
        self.assertEqual(
            result.eligibility.code, "parameter_candidate_not_tree"
        )
        self.assertIsNone(result.view)


if __name__ == "__main__":
    unittest.main()
