"""Focused contracts for portable selections of managed workspace revisions."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE, WorkspaceLineage
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


@unittest.skipUnless(os.name == "posix", "local Realm projections are POSIX-only")
class RealmWorkspaceSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:workspace-selection-test",
        )
        self.addCleanup(self.runtime.close)
        self.actor = self.runtime.actor_principal_id
        self.workspace_id = "workspace-selection-example"
        self.source_owner_id = "workspace-selection-source-owner"
        self.workspace_owner_id = "workspace-selection-owner"
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "simulator.py").write_text(
            "VALUE = 'revision-one'\n",
            encoding="utf-8",
        )
        self.created = self._create_workspace()

    def _create_workspace(self) -> Any:
        ledger = self.runtime.ledger
        store = self.runtime.content_store
        ledger.create_owner(
            operation_id="workspace-selection/create-source-owner",
            owner_id=self.source_owner_id,
            owner_kind="resource",
            principal_id=self.actor,
        )
        change = ledger.begin_owner_change(
            operation_id="workspace-selection/begin-source",
            actor_principal_id=self.actor,
            owner_id=self.source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=self.actor,
            change_id=change.change_id,
            store_id=store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(self.source),
            operation_id="workspace-selection/seal-source",
        )
        source_membership = OwnerMembership(
            store.store_id,
            sealed.snapshot_ref,
            "source-revision",
        )
        ledger.hold_owner_content(
            operation_id="workspace-selection/hold-source",
            actor_principal_id=self.actor,
            change_id=change.change_id,
            memberships=(source_membership,),
        )
        source_commit = ledger.commit_owner_change(
            operation_id="workspace-selection/commit-source",
            actor_principal_id=self.actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )
        return ledger.create_workspace_from_snapshot(
            operation_id="workspace-selection/create-workspace",
            actor_principal_id=self.actor,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=source_commit.owner_revision,
            title="Workspace selection example",
            root=OwnerMembership(
                store.store_id,
                sealed.snapshot_ref,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id=self.source_owner_id,
                source_id=self.source_owner_id,
                source_revision=source_commit.owner_revision,
                source_store_id=store.store_id,
                source_ref=sealed.snapshot_ref,
            ),
            workspace_id=self.workspace_id,
            owner_id=self.workspace_owner_id,
        )

    def _mint(self, revision: int):
        return self.runtime.ledger.mint_workspace_selection(
            actor_principal_id=self.actor,
            workspace_id=self.workspace_id,
            expected_workspace_revision=revision,
        )

    def _project(self, selection: Any, label: str):
        return self.runtime.projection_service.project_selection_read_only(
            operation_id=f"workspace-selection/project/{label}",
            actor_principal_id=self.actor,
            selection=selection,
            holder_id=f"workspace-selection-viewer-{label}",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
            consumer_kind="workspace-selection-test",
        )

    def test_current_workspace_selection_is_path_free_and_projects_exact_root(
        self,
    ) -> None:
        selection = self._mint(1)
        public = selection.to_dict()

        self.assertEqual(selection.kind, "workspace")
        self.assertEqual(selection.source_kind, "workspace")
        self.assertEqual(selection.source_id, self.workspace_id)
        self.assertEqual(selection.source_revision, 1)
        self.assertEqual(selection.owner_revision, 1)
        self.assertEqual(selection.entity_id, self.workspace_id)
        self._assert_no_provider_paths(public)
        serialized = json.dumps(public, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        for forbidden in (
            "root_path",
            "checkout_path",
            "projection_root",
            "provider_path",
            "store_id",
        ):
            self.assertNotIn(forbidden, public)

        projection = self._project(selection, "revision-one")
        try:
            self.assertEqual(
                (projection.root_path / "simulator.py").read_text(
                    encoding="utf-8"
                ),
                "VALUE = 'revision-one'\n",
            )
            self.assertNotEqual(projection.root_path, self.source)
            self.assertFalse(
                (projection.root_path / "simulator.py").stat().st_mode
                & stat.S_IWUSR
            )
        finally:
            projection.close()

    def test_commit_retains_old_selection_and_new_selection_reads_new_bytes(
        self,
    ) -> None:
        old_selection = self._mint(1)
        old_projection = self._project(old_selection, "before-commit")
        try:
            self.assertEqual(
                (old_projection.root_path / "simulator.py").read_text(
                    encoding="utf-8"
                ),
                "VALUE = 'revision-one'\n",
            )
        finally:
            old_projection.close()

        checkout = self.runtime.editable_workspaces.open_workspace(
            operation_id="workspace-selection/open-editable",
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        (checkout.root_path / "simulator.py").write_text(
            "VALUE = 'revision-two'\n",
            encoding="utf-8",
        )
        committed = self.runtime.editable_workspaces.commit_workspace(
            operation_id="workspace-selection/commit-edit",
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        self.assertEqual(committed.current_revision, 2)

        retained_projection = self._project(old_selection, "retained-revision-one")
        try:
            self.assertEqual(
                (retained_projection.root_path / "simulator.py").read_text(
                    encoding="utf-8"
                ),
                "VALUE = 'revision-one'\n",
            )
        finally:
            retained_projection.close()

        new_selection = self._mint(2)
        self.assertNotEqual(new_selection, old_selection)
        self.assertEqual(new_selection.source_revision, 2)
        self.assertGreater(new_selection.owner_revision, old_selection.owner_revision)
        self.assertNotEqual(new_selection.entity_ref, old_selection.entity_ref)
        self.assertNotEqual(
            new_selection.selection_digest,
            old_selection.selection_digest,
        )
        old_membership, old_manifest = (
            self.runtime.content_service.verify_selection_tree_manifest(
                actor_principal_id=self.actor,
                selection=old_selection,
            )
        )
        new_membership, new_manifest = (
            self.runtime.content_service.verify_selection_tree_manifest(
                actor_principal_id=self.actor,
                selection=new_selection,
            )
        )
        self.assertEqual(
            old_manifest.snapshot_ref,
            old_membership.content_ref,
        )
        self.assertEqual(
            new_manifest.snapshot_ref,
            new_membership.content_ref,
        )
        self.assertNotEqual(old_manifest.snapshot_ref, new_manifest.snapshot_ref)
        new_projection = self._project(new_selection, "revision-two")
        try:
            self.assertEqual(
                (new_projection.root_path / "simulator.py").read_text(
                    encoding="utf-8"
                ),
                "VALUE = 'revision-two'\n",
            )
        finally:
            new_projection.close()

    def test_wrong_revision_and_retired_workspace_cannot_mint_selection(self) -> None:
        with self.assertRaisesRegex(RealmConflict, "revision changed"):
            self._mint(2)

        selection = self._mint(1)
        retired = self.runtime.ledger.retire_workspace(
            operation_id="workspace-selection/retire",
            actor_principal_id=self.actor,
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        self.assertEqual(retired.workspace.state.value, "deleted")

        with self.assertRaisesRegex(RealmConflict, "not active"):
            self._mint(1)
        with self.assertRaises(RealmNotFound):
            self._project(selection, "after-retirement")

    def _assert_no_provider_paths(self, value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                self._assert_no_provider_paths(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_no_provider_paths(child)
        elif isinstance(value, str):
            self.assertFalse(
                Path(value).is_absolute(),
                f"Provider-local path leaked into selection: {value}",
            )


if __name__ == "__main__":
    unittest.main()
