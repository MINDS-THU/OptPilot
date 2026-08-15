from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import BlobRef
from optpilot.realm.workspaces import (
    WORKSPACE_REVISION_ROLE,
    WorkspaceLineage,
    WorkspaceState,
)


class RealmWorkspaceLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
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
        self.ledger.create_owner(
            operation_id="source-owner",
            owner_id="source-owner",
            owner_kind="run",
            principal_id="operator",
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"workspace-test/{self.counter}/{label}"

    def seal_source_tree(self, payload: bytes, *, expected_revision: int):
        for child in tuple(self.source.iterdir()):
            child.unlink()
        (self.source / "payload.bin").write_bytes(payload)
        change = self.ledger.begin_owner_change(
            operation_id=self.op("source-begin"),
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=expected_revision,
            ttl_seconds=60,
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
            self.store.store_id,
            sealed.snapshot_ref,
            "source-revision",
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("source-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        committed = self.ledger.commit_owner_change(
            operation_id=self.op("source-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=expected_revision,
            additions=(membership,),
        )
        return sealed, committed

    def lineage(self, sealed, source_revision: int):
        return WorkspaceLineage(
            source_kind="owner-revision",
            source_owner_id="source-owner",
            source_id="source-owner",
            source_revision=source_revision,
            source_store_id=self.store.store_id,
            source_ref=sealed.snapshot_ref,
        )

    def create_workspace(self, sealed, source_revision: int, *, operation_id: str):
        root = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        return self.ledger.create_workspace_from_snapshot(
            operation_id=operation_id,
            actor_principal_id="operator",
            source_owner_id="source-owner",
            expected_source_owner_revision=source_revision,
            title="Kept simulator",
            root=root,
            lineage=self.lineage(sealed, source_revision),
            workspace_id="workspace-kept",
            owner_id="workspace-owner-kept",
        )

    def test_create_commits_owner_membership_and_workspace_together_without_reseal(
        self,
    ) -> None:
        sealed, source_commit = self.seal_source_tree(
            b"generated simulator", expected_revision=0
        )
        before_refs = tuple(self.store.iter_live_refs())
        operation_id = self.op("keep")

        receipt = self.create_workspace(
            sealed, source_commit.owner_revision, operation_id=operation_id
        )
        replay = self.create_workspace(
            sealed, source_commit.owner_revision, operation_id=operation_id
        )

        self.assertEqual(replay, receipt)
        self.assertEqual(receipt.workspace.current_revision, 1)
        self.assertEqual(receipt.owner_commit.owner_revision, 1)
        self.assertEqual(receipt.revision.root_ref, sealed.snapshot_ref)
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="workspace-owner-kept"
            ),
            (
                OwnerMembership(
                    self.store.store_id,
                    sealed.snapshot_ref,
                    WORKSPACE_REVISION_ROLE,
                ),
            ),
        )
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            ),
            (receipt.workspace, receipt.revision),
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_workspace(
                actor_principal_id="other", workspace_id="workspace-kept"
            )
        self.ledger.create_owner(
            operation_id=self.op("replacement-owner"),
            owner_id="replacement-workspace-owner",
            owner_kind="workspace",
            principal_id="other",
        )
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "managed workspace identity already exists"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO managed_workspaces("
                    "workspace_id, owner_id, title, state, current_revision, "
                    "created_txn_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'active', 1, ?, ?, ?)",
                    (
                        receipt.workspace.workspace_id,
                        "replacement-workspace-owner",
                        "Rebound workspace",
                        receipt.workspace.created_txn_id,
                        receipt.workspace.created_at,
                        receipt.workspace.updated_at,
                    ),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "managed workspace identity is immutable"
            ):
                connection.execute(
                    "DELETE FROM managed_workspaces WHERE workspace_id = ?",
                    (receipt.workspace.workspace_id,),
                )
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            ),
            (receipt.workspace, receipt.revision),
        )
        with self.assertRaises(RealmConflict):
            self.ledger.create_workspace_from_snapshot(
                operation_id=operation_id,
                actor_principal_id="operator",
                source_owner_id="source-owner",
                expected_source_owner_revision=source_commit.owner_revision,
                title="Changed replay",
                root=OwnerMembership(
                    self.store.store_id,
                    sealed.snapshot_ref,
                    WORKSPACE_REVISION_ROLE,
                ),
                lineage=self.lineage(sealed, source_commit.owner_revision),
                workspace_id="workspace-kept",
                owner_id="workspace-owner-kept",
            )

    def test_rename_is_durable_replayable_and_aba_fenced_from_content(self) -> None:
        sealed, source_commit = self.seal_source_tree(
            b"rename-stable-content", expected_revision=0
        )
        created = self.create_workspace(
            sealed, source_commit.owner_revision, operation_id=self.op("keep")
        )
        workspace_before, revision_before = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
        )
        owner_before = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
        )
        memberships_before = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
        )

        rename_operation = self.op("rename")
        renamed = self.ledger.rename_workspace(
            operation_id=rename_operation,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_metadata_revision=1,
            title="Solver prototype",
        )
        replayed = self.ledger.rename_workspace(
            operation_id=rename_operation,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_metadata_revision=1,
            title="Solver prototype",
        )

        self.assertEqual(replayed, renamed)
        self.assertEqual(renamed.title, "Solver prototype")
        self.assertEqual(renamed.metadata_revision, 2)
        self.assertEqual(renamed.current_revision, workspace_before.current_revision)
        self.assertEqual(
            self.ledger.read_owner(
                actor_principal_id="operator",
                owner_id=created.workspace.owner_id,
            ),
            owner_before,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=created.workspace.owner_id,
            ),
            memberships_before,
        )
        workspace_after, revision_after = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
        )
        self.assertEqual(workspace_after, renamed)
        self.assertEqual(revision_after, revision_before)

        no_op = self.ledger.rename_workspace(
            operation_id=self.op("rename-no-op"),
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_metadata_revision=2,
            title="Solver prototype",
        )
        self.assertEqual(no_op.metadata_revision, 2)
        self.assertEqual(no_op.current_revision, 1)

        restored = self.ledger.rename_workspace(
            operation_id=self.op("rename-back"),
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_metadata_revision=2,
            title=workspace_before.title,
        )
        self.assertEqual(restored.title, workspace_before.title)
        self.assertEqual(restored.metadata_revision, 3)
        self.assertEqual(restored.current_revision, 1)

        # The visible title and content revision now match the original
        # observation, but the monotonic metadata fence rejects that stale
        # observation rather than allowing an ABA overwrite.
        with self.assertRaisesRegex(RealmConflict, "name changed"):
            self.ledger.rename_workspace(
                operation_id=self.op("rename-stale-after-aba"),
                actor_principal_id="operator",
                workspace_id=created.workspace.workspace_id,
                expected_metadata_revision=1,
                title="Stale overwrite",
            )

        reopened = RealmLedger(self.database)
        try:
            durable_workspace, durable_revision = reopened.read_workspace(
                actor_principal_id="operator",
                workspace_id=created.workspace.workspace_id,
            )
        finally:
            reopened.close()
        self.assertEqual(durable_workspace, restored)
        self.assertEqual(durable_revision, revision_before)

    def test_authorized_list_and_retirement_release_only_workspace_owner(self) -> None:
        sealed, source_commit = self.seal_source_tree(
            b"independent workspace", expected_revision=0
        )
        created = self.create_workspace(
            sealed, source_commit.owner_revision, operation_id=self.op("keep")
        )
        source_before = self.ledger.read_owner(
            actor_principal_id="operator", owner_id="source-owner"
        )
        source_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id="source-owner"
        )

        self.assertEqual(
            [
                item[0].workspace_id
                for item in self.ledger.list_workspaces(actor_principal_id="operator")
            ],
            [created.workspace.workspace_id],
        )
        self.assertEqual(self.ledger.list_workspaces(actor_principal_id="other"), ())

        operation_id = self.op("retire")
        retired = self.ledger.retire_workspace(
            operation_id=operation_id,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_workspace_revision=1,
        )
        replay = self.ledger.retire_workspace(
            operation_id=operation_id,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_workspace_revision=1,
        )

        self.assertEqual(replay, retired)
        self.assertIs(retired.workspace.state, WorkspaceState.DELETED)
        self.assertEqual(retired.previous_owner_revision, 1)
        self.assertEqual(retired.owner_revision, 2)
        self.assertEqual(len(retired.released_memberships), 1)
        self.assertEqual(self.ledger.list_workspaces(actor_principal_id="operator"), ())
        self.assertEqual(
            self.ledger.list_workspaces(
                actor_principal_id="operator", include_deleted=True
            )[0][0].state,
            WorkspaceState.DELETED,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=created.workspace.owner_id,
            ),
            (),
        )
        self.assertEqual(
            self.ledger.read_owner(
                actor_principal_id="operator", owner_id=created.workspace.owner_id
            ).state.value,
            "deleted",
        )
        self.assertEqual(
            self.ledger.read_owner(
                actor_principal_id="operator", owner_id="source-owner"
            ),
            source_before,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="source-owner"
            ),
            source_memberships,
        )

    def test_failed_workspace_row_rolls_back_new_owner_membership_and_operation(
        self,
    ) -> None:
        sealed, source_commit = self.seal_source_tree(b"rollback", expected_revision=0)
        operation_id = self.op("failed-keep")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "CREATE TRIGGER fail_workspace_revision BEFORE INSERT ON workspace_revisions "
                "BEGIN SELECT RAISE(ABORT, 'injected workspace failure'); END"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected workspace failure"
        ):
            self.ledger.create_workspace_from_snapshot(
                operation_id=operation_id,
                actor_principal_id="operator",
                source_owner_id="source-owner",
                expected_source_owner_revision=source_commit.owner_revision,
                title="Must roll back",
                root=OwnerMembership(
                    self.store.store_id,
                    sealed.snapshot_ref,
                    WORKSPACE_REVISION_ROLE,
                ),
                lineage=self.lineage(sealed, source_commit.owner_revision),
                workspace_id="workspace-failed",
                owner_id="workspace-owner-failed",
            )

        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM owners WHERE owner_id = 'workspace-owner-failed'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM managed_workspaces WHERE workspace_id = 'workspace-failed'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            )
        finally:
            connection.close()

    def test_stale_source_wrong_root_and_competing_create_fail_closed(self) -> None:
        sealed, source_commit = self.seal_source_tree(b"source", expected_revision=0)
        root = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.create_workspace_from_snapshot(
                operation_id=self.op("stale-source"),
                actor_principal_id="operator",
                source_owner_id="source-owner",
                expected_source_owner_revision=0,
                title="Stale",
                root=root,
                lineage=self.lineage(sealed, 0),
            )
        with self.assertRaises(ValueError):
            self.ledger.create_workspace_from_snapshot(
                operation_id=self.op("blob-root"),
                actor_principal_id="operator",
                source_owner_id="source-owner",
                expected_source_owner_revision=source_commit.owner_revision,
                title="Blob",
                root=OwnerMembership(
                    self.store.store_id,
                    BlobRef.from_bytes(b"not a tree"),
                    WORKSPACE_REVISION_ROLE,
                ),
                lineage=self.lineage(sealed, source_commit.owner_revision),
            )

        barrier = threading.Barrier(3)
        receipts = []
        errors = []

        def create(index: int) -> None:
            barrier.wait()
            try:
                receipts.append(
                    self.ledger.create_workspace_from_snapshot(
                        operation_id=f"competing/{index}",
                        actor_principal_id="operator",
                        source_owner_id="source-owner",
                        expected_source_owner_revision=source_commit.owner_revision,
                        title="Competing",
                        root=root,
                        lineage=self.lineage(sealed, source_commit.owner_revision),
                        workspace_id="workspace-race",
                        owner_id="workspace-owner-race",
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RealmConflict)

    def test_workspace_revision_advances_atomically_and_survives_source_release(
        self,
    ) -> None:
        first, first_source = self.seal_source_tree(b"first", expected_revision=0)
        created = self.create_workspace(
            first, first_source.owner_revision, operation_id=self.op("keep-first")
        )
        second, second_source = self.seal_source_tree(
            b"second", expected_revision=first_source.owner_revision
        )
        target_change = self.ledger.begin_owner_change(
            operation_id=self.op("target-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            ttl_seconds=60,
        )
        new_root = OwnerMembership(
            self.store.store_id,
            second.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        old_root = OwnerMembership(
            self.store.store_id,
            first.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("target-hold"),
            actor_principal_id="operator",
            change_id=target_change.change_id,
            memberships=(new_root,),
            source_owner_id="source-owner",
        )
        target_commit_operation = self.op("target-commit")
        target_lineage = self.lineage(second, second_source.owner_revision)
        advanced = self.ledger.commit_workspace_revision(
            operation_id=target_commit_operation,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_workspace_revision=1,
            change_id=target_change.change_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            root=new_root,
            previous_root=old_root,
            lineage=target_lineage,
        )
        replay = self.ledger.commit_workspace_revision(
            operation_id=target_commit_operation,
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_workspace_revision=1,
            change_id=target_change.change_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            root=new_root,
            previous_root=old_root,
            lineage=target_lineage,
        )
        self.assertEqual(replay, advanced)
        with self.assertRaises(RealmConflict):
            self.ledger.commit_workspace_revision(
                operation_id=target_commit_operation,
                actor_principal_id="operator",
                workspace_id=created.workspace.workspace_id,
                expected_workspace_revision=2,
                change_id=target_change.change_id,
                expected_owner_revision=advanced.owner_commit.owner_revision,
                root=old_root,
                previous_root=new_root,
                lineage=self.lineage(first, first_source.owner_revision),
            )
        self.assertEqual(advanced.workspace.current_revision, 2)
        self.assertEqual(advanced.revision.root_ref, second.snapshot_ref)
        self.assertEqual(advanced.owner_commit.owner_revision, 2)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id=created.workspace.owner_id
            ),
            tuple(sorted((old_root, new_root))),
        )

        removal_change = self.ledger.begin_owner_change(
            operation_id=self.op("forbidden-history-removal-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=advanced.owner_commit.owner_revision,
            ttl_seconds=60,
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "workspace revision membership is immutable"
        ):
            self.ledger.commit_owner_change(
                operation_id=self.op("forbidden-history-removal-commit"),
                actor_principal_id="operator",
                change_id=removal_change.change_id,
                expected_owner_revision=advanced.owner_commit.owner_revision,
                additions=(),
                removals=(old_root,),
            )
        self.ledger.abort_owner_change(
            operation_id=self.op("forbidden-history-removal-abort"),
            actor_principal_id="operator",
            change_id=removal_change.change_id,
        )

        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "workspace revision membership is retained"
            ):
                connection.execute(
                    "DELETE FROM owner_memberships "
                    "WHERE owner_id = ? AND store_id = ? AND content_ref = ? "
                    "AND role = 'workspace-revision' AND removed_revision IS NULL",
                    (
                        created.workspace.owner_id,
                        old_root.store_id,
                        str(old_root.content_ref),
                    ),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "workspace revision membership is immutable",
            ):
                connection.execute(
                    "UPDATE owner_memberships SET role = 'rewritten-history' "
                    "WHERE owner_id = ? AND store_id = ? AND content_ref = ? "
                    "AND role = 'workspace-revision' AND removed_revision IS NULL",
                    (
                        created.workspace.owner_id,
                        old_root.store_id,
                        str(old_root.content_ref),
                    ),
                )
            connection.rollback()
            row = connection.execute(
                "SELECT owner_id, store_id, content_ref, role, added_revision, "
                "removed_revision, added_txn_id, removed_txn_id "
                "FROM owner_memberships WHERE owner_id = ? AND store_id = ? "
                "AND content_ref = ? AND role = 'workspace-revision' "
                "AND removed_revision IS NULL",
                (
                    created.workspace.owner_id,
                    old_root.store_id,
                    str(old_root.content_ref),
                ),
            ).fetchone()
            assert row is not None
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "workspace revision membership cannot be replaced",
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO owner_memberships("
                    "owner_id, store_id, content_ref, role, added_revision, "
                    "removed_revision, added_txn_id, removed_txn_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*row[:5], advanced.owner_commit.owner_revision, row[6], row[6]),
                )
            connection.rollback()
        finally:
            connection.close()

        auxiliary = OwnerMembership(
            old_root.store_id,
            old_root.content_ref,
            "workspace-note",
        )
        auxiliary_change = self.ledger.begin_owner_change(
            operation_id=self.op("auxiliary-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=advanced.owner_commit.owner_revision,
            ttl_seconds=60,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("auxiliary-hold"),
            actor_principal_id="operator",
            change_id=auxiliary_change.change_id,
            memberships=(auxiliary,),
            source_owner_id=created.workspace.owner_id,
        )
        auxiliary_commit = self.ledger.commit_owner_change(
            operation_id=self.op("auxiliary-commit"),
            actor_principal_id="operator",
            change_id=auxiliary_change.change_id,
            expected_owner_revision=advanced.owner_commit.owner_revision,
            additions=(auxiliary,),
        )
        auxiliary_removal = self.ledger.begin_owner_change(
            operation_id=self.op("auxiliary-removal-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=auxiliary_commit.owner_revision,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("auxiliary-removal-commit"),
            actor_principal_id="operator",
            change_id=auxiliary_removal.change_id,
            expected_owner_revision=auxiliary_commit.owner_revision,
            additions=(),
            removals=(auxiliary,),
        )

        release_source = self.ledger.begin_owner_change(
            operation_id=self.op("source-release-begin"),
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=second_source.owner_revision,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("source-release-commit"),
            actor_principal_id="operator",
            change_id=release_source.change_id,
            expected_owner_revision=second_source.owner_revision,
            additions=(),
            removals=tuple(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id="source-owner"
                )
            ),
        )
        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("gc-start"), store_id=self.store.store_id
        )
        tombstones = self.ledger.finish_gc_epoch(
            operation_id=self.op("gc-finish"),
            store_id=self.store.store_id,
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        tombstoned_refs = {item.content_ref for item in tombstones}
        self.assertNotIn(first.snapshot_ref, tombstoned_refs)
        self.assertNotIn(second.snapshot_ref, tombstoned_refs)
        self.assertEqual(
            self.ledger.read_workspace(
                actor_principal_id="operator", workspace_id="workspace-kept"
            )[1],
            advanced.revision,
        )

    def test_fabricated_lineage_rolls_back_without_consuming_target_change(
        self,
    ) -> None:
        first, first_source = self.seal_source_tree(b"first", expected_revision=0)
        created = self.create_workspace(
            first, first_source.owner_revision, operation_id=self.op("keep-first")
        )
        second, second_source = self.seal_source_tree(
            b"second", expected_revision=first_source.owner_revision
        )
        target_change = self.ledger.begin_owner_change(
            operation_id=self.op("target-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            ttl_seconds=60,
        )
        new_root = OwnerMembership(
            self.store.store_id,
            second.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        old_root = OwnerMembership(
            self.store.store_id,
            first.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("target-hold"),
            actor_principal_id="operator",
            change_id=target_change.change_id,
            memberships=(new_root,),
            source_owner_id="source-owner",
        )
        fabricated = WorkspaceLineage(
            source_kind="owner-revision",
            source_owner_id="missing-owner",
            source_id="missing-owner",
            source_revision=999,
            source_store_id=self.store.store_id,
            source_ref=second.snapshot_ref,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.commit_workspace_revision(
                operation_id=self.op("fabricated-commit"),
                actor_principal_id="operator",
                workspace_id=created.workspace.workspace_id,
                expected_workspace_revision=1,
                change_id=target_change.change_id,
                expected_owner_revision=created.owner_commit.owner_revision,
                root=new_root,
                previous_root=old_root,
                lineage=fabricated,
            )

        workspace, revision = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
        )
        self.assertEqual(workspace.current_revision, 1)
        self.assertEqual(revision.root_ref, first.snapshot_ref)
        committed = self.ledger.commit_workspace_revision(
            operation_id=self.op("valid-commit"),
            actor_principal_id="operator",
            workspace_id=created.workspace.workspace_id,
            expected_workspace_revision=1,
            change_id=target_change.change_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            root=new_root,
            previous_root=old_root,
            lineage=self.lineage(second, second_source.owner_revision),
        )
        self.assertEqual(committed.workspace.current_revision, 2)

    def test_revision_insert_rejects_an_unrelated_transaction_anchor(self) -> None:
        first, first_source = self.seal_source_tree(b"first", expected_revision=0)
        created = self.create_workspace(
            first, first_source.owner_revision, operation_id=self.op("keep-first")
        )
        second, second_source = self.seal_source_tree(
            b"second", expected_revision=first_source.owner_revision
        )
        second_root = OwnerMembership(
            self.store.store_id,
            second.snapshot_ref,
            WORKSPACE_REVISION_ROLE,
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("generic-root-begin"),
            actor_principal_id="operator",
            owner_id=created.workspace.owner_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            ttl_seconds=60,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("generic-root-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(second_root,),
            source_owner_id="source-owner",
        )
        commit_operation = self.op("generic-root-commit")
        owner_commit = self.ledger.commit_owner_change(
            operation_id=commit_operation,
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=created.owner_commit.owner_revision,
            additions=(second_root,),
            removals=(),
        )

        connection = sqlite3.connect(self.database)
        try:
            txn_id = connection.execute(
                "SELECT txn_id FROM ledger_transactions WHERE operation_id = ?",
                (commit_operation,),
            ).fetchone()[0]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "workspace revision requires its domain transaction",
            ):
                connection.execute(
                    "INSERT INTO workspace_revisions("
                    "workspace_id, revision, owner_revision, root_store_id, root_ref, "
                    "lineage_json, txn_id, created_at) VALUES (?, 2, ?, ?, ?, ?, ?, ?)",
                    (
                        created.workspace.workspace_id,
                        owner_commit.owner_revision,
                        second_root.store_id,
                        str(second_root.content_ref),
                        self.lineage(second, second_source.owner_revision).to_json(),
                        txn_id,
                        created.workspace.updated_at + 1,
                    ),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision FROM managed_workspaces WHERE workspace_id = ?",
                    (created.workspace.workspace_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()


class RealmWorkspaceMigrationTest(unittest.TestCase):
    def test_v1_database_upgrades_through_current_v36_with_all_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "realm.sqlite3"
            v1_path = (
                Path(__file__).resolve().parents[2]
                / "src"
                / "optpilot"
                / "realm"
                / "migrations"
                / "0001_realm_core.sql"
            )
            payload = v1_path.read_bytes()
            connection = sqlite3.connect(database)
            try:
                connection.executescript(payload.decode("utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, migration_digest, applied_at) "
                    "VALUES (1, ?, 1)",
                    (hashlib.sha256(payload).hexdigest(),),
                )
                connection.executemany(
                    "INSERT INTO realm_meta(key, value) VALUES (?, ?)",
                    (("realm_id", "migration-test"), ("schema_version", "1")),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()

            ledger = RealmLedger(database)
            ledger.close()
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 36
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                    [(version,) for version in range(1, 37)],
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'managed_workspaces'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'run_evaluation_templates'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'run_retirements'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'run_namespaces'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'run_attempts'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'run_definition_manifests'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'owner_derivation_manifests'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'study_definition_manifests'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'completed_tree_captures'"
                    ).fetchone()
                )
                for table in (
                    "workspace_assembly_requests",
                    "workspace_assembly_attempts",
                    "workspace_assembly_proofs",
                    "workspace_assembly_completions",
                ):
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'table' AND name = ?",
                            (table,),
                        ).fetchone()
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
