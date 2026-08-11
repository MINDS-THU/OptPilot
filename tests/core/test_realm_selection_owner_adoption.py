"""Focused contracts for exact SelectionRef-to-owner adoption."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import SnapshotRef, canonical_json_bytes
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE, WorkspaceLineage


class RealmSelectionOwnerAdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="selection-adoption-operator",
        )
        self.addCleanup(self.runtime.close)
        self.ledger = self.runtime.ledger
        self.store = self.runtime.content_store
        self.actor = self.runtime.actor_principal_id
        self.workspace_id = "selection-adoption-workspace"
        self.workspace_owner_id = "selection-adoption-workspace-owner"
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "value.txt").write_text("revision one\n", encoding="utf-8")
        self._create_workspace()
        self.selection_one = self.ledger.mint_workspace_selection(
            actor_principal_id=self.actor,
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        checkout = self.runtime.editable_workspaces.open_workspace(
            operation_id="selection-adoption/open",
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        (checkout.root_path / "value.txt").write_text(
            "revision two\n", encoding="utf-8"
        )
        self.runtime.editable_workspaces.commit_workspace(
            operation_id="selection-adoption/commit-two",
            workspace_id=self.workspace_id,
            expected_workspace_revision=1,
        )
        self.selection_two = self.ledger.mint_workspace_selection(
            actor_principal_id=self.actor,
            workspace_id=self.workspace_id,
            expected_workspace_revision=2,
        )

    def _create_workspace(self) -> None:
        source_owner_id = "selection-adoption-source-owner"
        self.ledger.create_owner(
            operation_id="selection-adoption/create-source",
            owner_id=source_owner_id,
            owner_kind="resource",
            principal_id=self.actor,
        )
        change = self.ledger.begin_owner_change(
            operation_id="selection-adoption/begin-source",
            actor_principal_id=self.actor,
            owner_id=source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=self.actor,
            change_id=change.change_id,
            store_id=self.store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(self.source),
            operation_id="selection-adoption/seal-source",
        )
        source_membership = OwnerMembership(
            self.store.store_id, sealed.snapshot_ref, "resource-source"
        )
        self.ledger.hold_owner_content(
            operation_id="selection-adoption/hold-source",
            actor_principal_id=self.actor,
            change_id=change.change_id,
            memberships=(source_membership,),
        )
        source_commit = self.ledger.commit_owner_change(
            operation_id="selection-adoption/commit-source",
            actor_principal_id=self.actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )
        self.ledger.create_workspace_from_snapshot(
            operation_id="selection-adoption/create-workspace",
            actor_principal_id=self.actor,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_commit.owner_revision,
            title="Selection adoption workspace",
            root=OwnerMembership(
                self.store.store_id,
                sealed.snapshot_ref,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id=source_owner_id,
                source_id=source_owner_id,
                source_revision=source_commit.owner_revision,
                source_store_id=self.store.store_id,
                source_ref=sealed.snapshot_ref,
            ),
            workspace_id=self.workspace_id,
            owner_id=self.workspace_owner_id,
        )

    def _content_object_count(self) -> int:
        connection = self.ledger._connect()
        try:
            return int(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ]
            )
        finally:
            connection.close()

    def test_historical_selection_adoption_is_atomic_independent_and_no_copy(
        self,
    ) -> None:
        before_objects = self._content_object_count()
        before_refs = tuple(self.store.iter_live_refs())

        receipt = self.ledger.adopt_selection_as_owner(
            operation_id="selection-adoption/adopt-old",
            actor_principal_id=self.actor,
            selection=self.selection_one,
            target_owner_id="retained-old",
            target_owner_kind="retained-study-source",
            target_role="study-package-source",
        )

        self.assertEqual(receipt.selection, self.selection_one)
        self.assertTrue(receipt.eligibility.eligible)
        self.assertIsNotNone(receipt.derivation)
        assert receipt.derivation is not None
        expected_ref = SnapshotRef.parse(self.selection_one.entity_ref)
        self.assertNotEqual(expected_ref, SnapshotRef.parse(self.selection_two.entity_ref))
        self.assertEqual(
            receipt.derivation.manifest.target_memberships,
            (
                OwnerMembership(
                    self.store.store_id,
                    expected_ref,
                    "study-package-source",
                ),
            ),
        )
        self.assertEqual(
            receipt.derivation.manifest.bindings[0].source_role,
            WORKSPACE_REVISION_ROLE,
        )
        self.assertEqual(self._content_object_count(), before_objects)
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(
            self.ledger.read_owner_selection_provenance(
                actor_principal_id=self.actor, owner_id="retained-old"
            ),
            self.selection_one,
        )
        reopened = RealmLedger(
            self.root / "realm" / "authority" / "realm.sqlite3"
        )
        try:
            self.assertEqual(
                reopened.read_owner_selection_provenance(
                    actor_principal_id=self.actor, owner_id="retained-old"
                ),
                self.selection_one,
            )
        finally:
            reopened.close()

        self.ledger.retire_workspace(
            operation_id="selection-adoption/retire-workspace",
            actor_principal_id=self.actor,
            workspace_id=self.workspace_id,
            expected_workspace_revision=2,
        )
        self.assertEqual(
            self.ledger.adopt_selection_as_owner(
                operation_id="selection-adoption/adopt-old",
                actor_principal_id=self.actor,
                selection=self.selection_one,
                target_owner_id="retained-old",
                target_owner_kind="retained-study-source",
                target_role="study-package-source",
            ),
            receipt,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id=self.actor, owner_id="retained-old"
            ),
            receipt.derivation.manifest.target_memberships,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.adopt_selection_as_owner(
                operation_id="selection-adoption/adopt-old",
                actor_principal_id=self.actor,
                selection=self.selection_one,
                target_owner_id="retained-old",
                target_owner_kind="retained-study-source",
                target_role="changed-role",
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.adopt_selection_as_owner(
                operation_id="selection-adoption/new-after-retirement",
                actor_principal_id=self.actor,
                selection=self.selection_one,
                target_owner_id="retained-after-retirement",
                target_owner_kind="retained-study-source",
                target_role="study-package-source",
            )

    def test_adoption_requires_derive_permission_not_byte_read(self) -> None:
        other = "selection-adoption-other"
        self.ledger.register_principal(
            operation_id="selection-adoption/register-other",
            principal_id=other,
            kind="human",
        )
        self.ledger.grant_owner_permission(
            operation_id="selection-adoption/grant-read",
            actor_principal_id=self.actor,
            owner_id=self.workspace_owner_id,
            principal_id=other,
            permission=OwnerPermission.BYTES_READ,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.adopt_selection_as_owner(
                operation_id="selection-adoption/read-is-insufficient",
                actor_principal_id=other,
                selection=self.selection_one,
                target_owner_id="read-only-target",
                target_owner_kind="retained-study-source",
                target_role="study-package-source",
            )

        self.ledger.grant_owner_permission(
            operation_id="selection-adoption/grant-derive",
            actor_principal_id=self.actor,
            owner_id=self.workspace_owner_id,
            principal_id=other,
            permission=OwnerPermission.DERIVE,
        )
        receipt = self.ledger.adopt_selection_as_owner(
            operation_id="selection-adoption/derive-succeeds",
            actor_principal_id=other,
            selection=self.selection_one,
            target_owner_id="derived-target",
            target_owner_kind="retained-study-source",
            target_role="study-package-source",
        )
        self.assertTrue(receipt.eligibility.eligible)
        assert receipt.derivation is not None
        self.assertEqual(receipt.derivation.owner.principal_id, other)

    def test_persisted_selection_provenance_detects_cross_table_tampering(self) -> None:
        self.ledger.adopt_selection_as_owner(
            operation_id="selection-adoption/adopt-for-tamper",
            actor_principal_id=self.actor,
            selection=self.selection_one,
            target_owner_id="tamper-target",
            target_owner_kind="retained-study-source",
            target_role="study-package-source",
        )
        database = self.root / "realm" / "authority" / "realm.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "DROP TRIGGER selection_owner_adoption_update_immutable"
            )
            connection.execute(
                "UPDATE selection_owner_adoptions "
                "SET selection_digest = ?, selection_json = ? "
                "WHERE target_owner_id = 'tamper-target'",
                (
                    self.selection_two.selection_digest,
                    canonical_json_bytes(self.selection_two.to_dict()).decode("utf-8"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmConflict):
            # A changed request remains an idempotency conflict even if a
            # malicious writer bypassed the immutable provenance trigger.
            self.ledger.adopt_selection_as_owner(
                operation_id="selection-adoption/adopt-for-tamper",
                actor_principal_id=self.actor,
                selection=self.selection_two,
                target_owner_id="tamper-target",
                target_owner_kind="retained-study-source",
                target_role="study-package-source",
            )
        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_owner_selection_provenance(
                actor_principal_id=self.actor, owner_id="tamper-target"
            )


if __name__ == "__main__":
    unittest.main()
