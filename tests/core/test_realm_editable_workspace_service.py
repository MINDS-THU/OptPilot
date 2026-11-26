from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.editable_workspace_service import (
    EditableWorkspaceCommitStatus,
    RealmEditableWorkspaceService,
)
from optpilot.realm.errors import ContentRejected, RealmConflict, RealmIntegrityError
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.refs import canonical_json_bytes
from optpilot.realm.service import RealmContentService
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE, WorkspaceLineage
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


class RealmEditableWorkspaceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "simulator.py").write_text(
            "VALUE = 'first'\n", encoding="utf-8"
        )
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="editable-test/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="editable-test/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="editable-test/source-owner",
            owner_id="source-owner",
            owner_kind="resource",
            principal_id="operator",
        )
        self.source_root, self.source_membership = self._seal_source()
        self.kept = self.ledger.create_workspace_from_snapshot(
            operation_id="editable-test/keep",
            actor_principal_id="operator",
            source_owner_id="source-owner",
            expected_source_owner_revision=1,
            title="Kept simulator",
            root=OwnerMembership(
                self.store.store_id,
                self.source_root,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id="source-owner",
                source_id="source-owner",
                source_revision=1,
                source_store_id=self.store.store_id,
                source_ref=self.source_root,
            ),
            workspace_id="workspace-kept",
            owner_id="workspace-owner-kept",
        )
        self.content = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.projections = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.checkout_root = self.root / "editable-checkouts"
        self.service = self._service()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _service(self) -> RealmEditableWorkspaceService:
        return RealmEditableWorkspaceService(
            self.ledger,
            self.content,
            self.projections,
            actor_principal_id="operator",
            checkout_root=self.checkout_root,
        )

    def _seal_source(self):
        change = self.ledger.begin_owner_change(
            operation_id="editable-test/source-begin",
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
        sealed = capture.seal_tree(source=AllowedTreeSource(self.source))
        membership = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, "source-revision"
        )
        self.ledger.hold_owner_content(
            operation_id="editable-test/source-hold",
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.ledger.commit_owner_change(
            operation_id="editable-test/source-commit",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return sealed.snapshot_ref, membership

    def _open(self, label: str = "open"):
        return self.service.open_workspace(
            operation_id=f"editable-test/{label}",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

    def test_kept_workspace_opens_as_persistent_provider_owned_checkout(self) -> None:
        before_refs = tuple(self.store.iter_live_refs())

        checkout = self._open()
        reopened = self.service.open_workspace(
            operation_id="editable-test/reopen",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

        self.assertEqual(
            (checkout.checkout_id, checkout.root_path),
            (reopened.checkout_id, reopened.root_path),
        )
        self.assertFalse(checkout.recovered)
        self.assertTrue(reopened.recovered)
        self.assertEqual(
            (checkout.root_path / "simulator.py").read_text(encoding="utf-8"),
            "VALUE = 'first'\n",
        )
        # Keeping and opening do not create a second durable content object.
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)

    def test_rename_survives_restart_without_realizing_or_revising_content(
        self,
    ) -> None:
        workspace_before, revision_before = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id="workspace-kept",
        )
        before_refs = tuple(self.store.iter_live_refs())
        self.assertFalse(any(self.checkout_root.glob("editable-*")))

        renamed = self.service.rename_workspace(
            operation_id="editable-test/rename",
            workspace_id="workspace-kept",
            expected_metadata_revision=1,
            title="Solver prototype",
        )
        replayed = self.service.rename_workspace(
            operation_id="editable-test/rename",
            workspace_id="workspace-kept",
            expected_metadata_revision=1,
            title="Solver prototype",
        )
        no_op = self.service.rename_workspace(
            operation_id="editable-test/rename-no-op",
            workspace_id="workspace-kept",
            expected_metadata_revision=2,
            title="Solver prototype",
        )

        self.assertEqual(replayed, renamed)
        self.assertEqual(no_op, renamed)
        self.assertEqual(renamed.title, "Solver prototype")
        self.assertEqual(renamed.metadata_revision, 2)
        self.assertEqual(
            renamed.workspace_revision,
            workspace_before.current_revision,
        )
        self.assertFalse(any(self.checkout_root.glob("editable-*")))
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)

        restored = self.service.rename_workspace(
            operation_id="editable-test/rename-back",
            workspace_id="workspace-kept",
            expected_metadata_revision=2,
            title=workspace_before.title,
        )
        self.assertEqual(restored.metadata_revision, 3)

        with self.assertRaisesRegex(RealmConflict, "name changed"):
            self.service.rename_workspace(
                operation_id="editable-test/rename-stale-after-aba",
                workspace_id="workspace-kept",
                expected_metadata_revision=1,
                title="Stale overwrite",
            )

        # Replaying an older operation returns the current summary rather than
        # regressing a client to that operation's historical receipt.
        current_after_old_replay = self.service.rename_workspace(
            operation_id="editable-test/rename",
            workspace_id="workspace-kept",
            expected_metadata_revision=1,
            title="Solver prototype",
        )
        self.assertEqual(current_after_old_replay, restored)

        restarted = self._service()
        self.assertEqual(
            restarted.read_workspace(workspace_id="workspace-kept"),
            restored,
        )
        self.assertEqual(restarted.list_workspaces(), (restored,))
        workspace_after, revision_after = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id="workspace-kept",
        )
        self.assertEqual(workspace_after.current_revision, 1)
        self.assertEqual(workspace_after.metadata_revision, 3)
        self.assertEqual(revision_after, revision_before)
        self.assertFalse(any(self.checkout_root.glob("editable-*")))
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)

    def test_edit_commit_and_restart_reopen_exact_checkout_identity(self) -> None:
        checkout = self._open()
        original_path = checkout.root_path
        (original_path / "simulator.py").write_text(
            "VALUE = 'edited'\n", encoding="utf-8"
        )

        committed = self.service.commit_workspace(
            operation_id="editable-test/commit-edited",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )
        restarted = self._service()
        reopened = restarted.open_workspace(
            operation_id="editable-test/open-after-restart",
            workspace_id="workspace-kept",
            expected_workspace_revision=2,
        )

        self.assertIs(committed.status, EditableWorkspaceCommitStatus.COMMITTED)
        self.assertEqual(
            (committed.previous_revision, committed.current_revision),
            (1, 2),
        )
        self.assertEqual(reopened.root_path, original_path)
        self.assertEqual(reopened.checkout_id, checkout.checkout_id)
        self.assertEqual(reopened.workspace_revision, 2)
        self.assertEqual(
            (reopened.root_path / "simulator.py").read_text(encoding="utf-8"),
            "VALUE = 'edited'\n",
        )
        workspace, revision = self.ledger.read_workspace(
            actor_principal_id="operator", workspace_id="workspace-kept"
        )
        self.assertEqual(workspace.current_revision, 2)
        self.assertNotEqual(revision.root_ref, self.source_root)

    def test_single_file_commit_reuses_base_tree_and_leaves_other_edits_dirty(
        self,
    ) -> None:
        checkout = self._open("open-for-single-file-commit")
        checkout_root = checkout.root_path
        studies = checkout_root / "studies"
        studies.mkdir()
        (studies / "draft.yaml").write_text(
            "config: study\nname: focused\n", encoding="utf-8"
        )
        (checkout_root / "unrelated.txt").write_text(
            "keep this edit local\n", encoding="utf-8"
        )

        with (
            mock.patch.object(
                self.store,
                "_publish_blob_from_fd",
                wraps=self.store._publish_blob_from_fd,
            ) as publish_blob,
            mock.patch.object(
                self.store,
                "_publish_tree_manifest",
                wraps=self.store._publish_tree_manifest,
            ) as publish_tree,
        ):
            committed = self.service.commit_workspace_file(
                operation_id="editable-test/commit-one-study-file",
                workspace_id="workspace-kept",
                expected_workspace_revision=1,
                relative_path="studies/draft.yaml",
            )

        self.assertIs(committed.status, EditableWorkspaceCommitStatus.COMMITTED)
        self.assertEqual((committed.previous_revision, committed.current_revision), (1, 2))
        self.assertEqual(publish_blob.call_count, 1)
        self.assertEqual(publish_tree.call_count, 1)
        self.assertTrue((checkout_root / "unrelated.txt").is_file())
        self.service.delete_checkout(
            operation_id="editable-test/drop-dirty-checkout",
            workspace_id="workspace-kept",
        )
        reopened = self.service.open_workspace(
            operation_id="editable-test/reopen-single-file-revision",
            workspace_id="workspace-kept",
            expected_workspace_revision=2,
        )
        self.assertEqual(
            (reopened.root_path / "studies/draft.yaml").read_text(encoding="utf-8"),
            "config: study\nname: focused\n",
        )
        self.assertFalse((reopened.root_path / "unrelated.txt").exists())
        self.assertEqual(
            (reopened.root_path / "simulator.py").read_text(encoding="utf-8"),
            "VALUE = 'first'\n",
        )

    def test_single_file_commit_is_recoverable_after_authority_response_loss(
        self,
    ) -> None:
        checkout = self._open("open-file-before-response-loss")
        (checkout.root_path / "study.yaml").write_text(
            "config: study\n", encoding="utf-8"
        )
        commit = self.ledger.commit_workspace_revision

        def commit_then_lose_response(**kwargs):
            commit(**kwargs)
            raise RuntimeError("simulated response loss")

        with mock.patch.object(
            self.ledger,
            "commit_workspace_revision",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                self.service.commit_workspace_file(
                    operation_id="editable-test/file-commit-response-loss",
                    workspace_id="workspace-kept",
                    expected_workspace_revision=1,
                    relative_path="study.yaml",
                )

        restarted = self._service()
        recovered = restarted.open_workspace(
            operation_id="editable-test/recover-file-response-loss",
            workspace_id="workspace-kept",
        )
        self.assertEqual(recovered.workspace_revision, 2)
        self.assertEqual(
            (recovered.root_path / "study.yaml").read_text(encoding="utf-8"),
            "config: study\n",
        )

    def test_restart_rebinds_checkout_observations_from_exact_claim(self) -> None:
        checkout = self._open("open-before-remount")
        claim = checkout.root_path.parent / "claim.json"
        payload = json.loads(claim.read_text(encoding="utf-8"))
        os.chmod(claim, 0o600)
        for field in (
            "wrapper_device_id",
            "wrapper_inode",
            "tree_device_id",
            "tree_inode",
        ):
            payload[field] = int(payload[field]) + 1000
        claim.write_bytes(canonical_json_bytes(payload))
        os.chmod(claim, 0o400)

        restarted = self._service()
        reopened = restarted.open_workspace(
            operation_id="editable-test/open-after-remount",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

        self.assertEqual(reopened.root_path, checkout.root_path)
        self.assertEqual(reopened.checkout_id, checkout.checkout_id)

    def test_same_service_rejects_wrapper_replacement_with_copied_claim(self) -> None:
        checkout = self._open("open-before-wrapper-replacement")
        wrapper = checkout.root_path.parent
        original = wrapper.with_name(wrapper.name + "-original")
        wrapper.rename(original)
        shutil.copytree(original, wrapper, copy_function=shutil.copy2)

        with self.assertRaisesRegex(RealmIntegrityError, "replaced while attached"):
            self.service.open_workspace(
                operation_id="editable-test/open-replaced-wrapper",
                workspace_id="workspace-kept",
                expected_workspace_revision=1,
            )

    def test_restart_recovers_commit_after_authority_response_loss(self) -> None:
        checkout = self._open()
        original_path = checkout.root_path
        (original_path / "simulator.py").write_text(
            "VALUE = 'response-lost'\n", encoding="utf-8"
        )
        commit = self.ledger.commit_workspace_revision

        def commit_then_lose_response(**kwargs):
            commit(**kwargs)
            raise RuntimeError("simulated response loss")

        with mock.patch.object(
            self.ledger,
            "commit_workspace_revision",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                self.service.commit_workspace(
                    operation_id="editable-test/commit-response-loss",
                    workspace_id="workspace-kept",
                    expected_workspace_revision=1,
                )

        restarted = self._service()
        recovered = restarted.open_workspace(
            operation_id="editable-test/recover-response-loss",
            workspace_id="workspace-kept",
        )

        self.assertEqual(recovered.root_path, original_path)
        self.assertEqual(recovered.workspace_revision, 2)
        self.assertEqual(
            (recovered.root_path / "simulator.py").read_text(encoding="utf-8"),
            "VALUE = 'response-lost'\n",
        )

    def test_unchanged_commit_is_clear_idempotent_no_op(self) -> None:
        self._open()

        first = self.service.commit_workspace(
            operation_id="editable-test/commit-unchanged",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )
        replay = self.service.commit_workspace(
            operation_id="editable-test/commit-unchanged",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

        self.assertEqual(first, replay)
        self.assertIs(first.status, EditableWorkspaceCommitStatus.UNCHANGED)
        self.assertEqual((first.previous_revision, first.current_revision), (1, 1))
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            )[0].current_revision,
            1,
        )

    def test_stale_commit_conflicts_without_overwriting_new_head(self) -> None:
        checkout = self._open()
        (checkout.root_path / "simulator.py").write_text(
            "VALUE = 'second'\n", encoding="utf-8"
        )
        committed = self.service.commit_workspace(
            operation_id="editable-test/commit-second",
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

        with self.assertRaisesRegex(RealmConflict, "revision changed"):
            self.service.commit_workspace(
                operation_id="editable-test/stale-commit",
                workspace_id="workspace-kept",
                expected_workspace_revision=1,
            )

        self.assertEqual(committed.current_revision, 2)
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            )[0].current_revision,
            2,
        )

    def test_checkout_identity_replacement_and_marker_tampering_fail_closed(self) -> None:
        checkout = self._open()
        checkout_path = checkout.root_path
        original = checkout_path.with_name("root-original")
        checkout_path.rename(original)
        checkout_path.mkdir(mode=0o700)

        with self.assertRaises(RealmIntegrityError):
            checkout.validate()
        with self.assertRaises(RealmIntegrityError):
            self.service.delete_checkout(
                operation_id="editable-test/delete-tampered",
                workspace_id="workspace-kept",
            )
        self.assertTrue(original.is_dir())
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            )[0].current_revision,
            1,
        )

        # Restore the exact tree, then prove the private identity marker is
        # canonical and cannot be edited into another checkout identity.
        checkout_path.rmdir()
        original.rename(checkout_path)
        claim = checkout_path.parent / "claim.json"
        payload = json.loads(claim.read_text(encoding="utf-8"))
        os.chmod(claim, 0o600)
        payload["checkout_id"] = "editable-forged"
        claim.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(claim, 0o400)
        with self.assertRaises(RealmIntegrityError):
            self.service.open_workspace(
                operation_id="editable-test/open-forged",
                workspace_id="workspace-kept",
            )

    def test_commit_rejects_symlink_escape_without_reading_outside_checkout(self) -> None:
        checkout = self._open()
        secret = self.root / "outside-secret.txt"
        secret.write_text("must not be captured", encoding="utf-8")
        (checkout.root_path / "escape").symlink_to(secret)

        with self.assertRaises(ContentRejected):
            self.service.commit_workspace(
                operation_id="editable-test/commit-symlink",
                workspace_id="workspace-kept",
                expected_workspace_revision=1,
            )

        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            )[0].current_revision,
            1,
        )
        self.assertEqual(secret.read_text(encoding="utf-8"), "must not be captured")

    def test_open_survives_source_checkout_and_source_membership_deletion(self) -> None:
        release = self.ledger.begin_owner_change(
            operation_id="editable-test/source-release-begin",
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=1,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.commit_owner_change(
            operation_id="editable-test/source-release-commit",
            actor_principal_id="operator",
            change_id=release.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=(self.source_membership,),
        )
        shutil.rmtree(self.source)

        checkout = self._open("open-after-source-delete")

        self.assertEqual(
            (checkout.root_path / "simulator.py").read_text(encoding="utf-8"),
            "VALUE = 'first'\n",
        )
        self.store.verify_tree(self.source_root, verify_children=True)

    def test_delete_removes_only_checkout_and_public_receipts_hide_copy_internals(self) -> None:
        checkout = self._open()
        open_record = checkout.portable_record()
        checkout_path = checkout.root_path

        deleted = self.service.delete_checkout(
            operation_id="editable-test/delete-checkout",
            workspace_id="workspace-kept",
        )
        replay = self.service.delete_checkout(
            operation_id="editable-test/delete-checkout-replay",
            workspace_id="workspace-kept",
        )

        self.assertTrue(deleted.checkout_removed)
        self.assertFalse(replay.checkout_removed)
        self.assertFalse(checkout_path.exists())
        self.assertTrue(deleted.to_dict()["durable_workspace_retained"])
        self.ledger.read_workspace(
            actor_principal_id="operator", workspace_id="workspace-kept"
        )
        self.store.verify_tree(self.source_root, verify_children=True)
        serialized = json.dumps(
            {"open": open_record, "delete": deleted.to_dict()}, sort_keys=True
        ).lower()
        for forbidden in (
            "copied",
            "copy_bytes",
            "store_id",
            "root_ref",
            "projection",
            "cas",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(open_record["ownership"], "realm-managed")

    def test_list_is_path_free_and_retire_removes_only_checkout_and_workspace_owner(self) -> None:
        source_owner_before = self.ledger.read_owner(
            actor_principal_id="operator", owner_id="source-owner"
        )
        source_memberships_before = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id="source-owner"
        )

        summaries = self.service.list_workspaces()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].workspace_id, "workspace-kept")
        self.assertEqual(summaries[0].workspace_revision, 1)
        self.assertNotIn("root", summaries[0].to_dict())
        self.assertFalse(any(self.checkout_root.glob("editable-*")))

        checkout = self._open()
        checkout_path = checkout.root_path
        operation_id = "editable-test/retire"
        retired = self.service.retire_workspace(
            operation_id=operation_id,
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )
        replay = self.service.retire_workspace(
            operation_id=operation_id,
            workspace_id="workspace-kept",
            expected_workspace_revision=1,
        )

        self.assertEqual(replay, retired)
        self.assertFalse(checkout_path.exists())
        self.assertEqual(self.service.list_workspaces(), ())
        self.assertTrue(retired.to_dict()["workspace_retired"])
        self.assertTrue(retired.to_dict()["checkout_absent"])
        self.assertTrue(retired.to_dict()["source_content_unchanged"])
        self.assertEqual(
            self.ledger.read_owner(
                actor_principal_id="operator", owner_id="source-owner"
            ),
            source_owner_before,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="source-owner"
            ),
            source_memberships_before,
        )
        self.assertTrue(self.source.is_dir())
        self.store.verify_tree(self.source_root, verify_children=True)


if __name__ == "__main__":
    unittest.main()
