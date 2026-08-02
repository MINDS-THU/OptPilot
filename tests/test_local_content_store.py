from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import optpilot.realm.content as content_module
from optpilot.realm.content import (
    AllowedFileSource,
    AllowedTreeSource,
    LocalContentStore,
    PublishedObject,
    publication_digest,
)
from optpilot.realm.errors import (
    ContentCorrupt,
    ContentRejected,
    RealmConflict,
    RealmIntegrityError,
    RealmNotFound,
    SourceChanged,
)
from optpilot.realm.gc import (
    AbandonedStagingCleanupDecision,
    LocalAbandonedStagingBackend,
    LocalGcBackend,
    RegisteredContentEdge,
    RegisteredContentFact,
    RegisteredStagingAllocation,
    RegisteredTombstone,
    fsck_local_store,
    new_deletion_token,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.manifests import (
    SealLimits,
    TreeEntry,
    TreeManifest,
    validate_portable_path,
    validate_portable_paths,
)
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import BlobRef, canonical_json_bytes


class _RecordingRetention:
    def __init__(self, *, store_id: str, root_marker: str) -> None:
        self.store_id = store_id
        self.root_marker = root_marker
        self.states: dict[str, str] = {}
        self.publications: dict[str, PublishedObject] = {}
        self.prepared: dict[str, PublishedObject] = {}
        self.rollback_batches: list[tuple[str, ...]] = []
        self.fail_record_once = False

    def validate_capture_binding(
        self, *, change_id, store_id, backend_kind, root_marker
    ) -> None:
        if not change_id:
            raise AssertionError("change id is required")
        if (
            store_id != self.store_id
            or backend_kind != LocalContentStore.BACKEND_KIND
            or root_marker != self.root_marker
        ):
            raise RealmIntegrityError("test retention authority is bound to another store")

    def reserve_staging(self, *, change_id, staging_id, store_id, object_kind) -> None:
        del change_id, store_id, object_kind
        if not re.fullmatch(r"stage-[0-9a-f]{32}", staging_id):
            raise AssertionError(f"unsafe staging id: {staging_id}")
        if staging_id in self.states:
            raise AssertionError(f"duplicate staging id: {staging_id}")
        self.states[staging_id] = "allocated"

    def record_publication(self, *, change_id, publication) -> None:
        del change_id
        if self.states.get(publication.staging_id) not in {"prepared", "published"}:
            raise AssertionError("publication has no allocation")
        if self.fail_record_once:
            self.fail_record_once = False
            raise RuntimeError("injected failure after durable filesystem publication")
        self.states[publication.staging_id] = "published"
        self.publications[publication.staging_id] = publication
        self.prepared.pop(publication.staging_id, None)

    def complete_staging_publication(self, *, change_id, staging_id) -> None:
        del change_id
        if self.states.get(staging_id) not in {"published", "finalized"}:
            raise AssertionError("only a published staging allocation can be finalized")
        self.states[staging_id] = "finalized"

    def prepare_publication(self, *, change_id, publication) -> None:
        del change_id
        if self.states.get(publication.staging_id) not in {"allocated", "prepared"}:
            raise AssertionError("prepared publication has no allocation")
        self.states[publication.staging_id] = "prepared"
        self.prepared[publication.staging_id] = publication

    def abandon_staging(self, *, change_id, staging_id) -> None:
        del change_id
        self.states[staging_id] = "abandoned"
        self.publications.pop(staging_id, None)
        self.prepared.pop(staging_id, None)

    def rollback_capture(self, *, change_id, staging_ids) -> None:
        del change_id
        batch = tuple(staging_ids)
        self.rollback_batches.append(batch)
        for staging_id in batch:
            self.states[staging_id] = "abandoned"
            self.publications.pop(staging_id, None)
            self.prepared.pop(staging_id, None)

    def expected_live_refs(self, *, store_id):
        return {
            publication.content_ref
            for publication in self.publications.values()
            if publication.store_id == store_id
        }

    def registered_edges(self, *, store_id):
        return tuple(
            RegisteredContentEdge(edge.parent_ref, edge.child_ref, edge.canonical_path)
            for publication in self.publications.values()
            if publication.store_id == store_id
            for edge in publication.edges
        )

    def registered_content(self, *, store_id):
        return tuple(
            RegisteredContentFact(
                content_ref=publication.content_ref,
                kind=publication.kind,
                logical_bytes=publication.logical_bytes,
                physical_bytes=publication.physical_bytes,
                metadata=publication.metadata,
            )
            for publication in self.publications.values()
            if publication.store_id == store_id
        )

    def expected_staging_allocations(self, *, store_id):
        if store_id != self.store_id:
            return ()
        allocations = []
        for staging_id, state in self.states.items():
            if state == "allocated":
                allocations.append(RegisteredStagingAllocation(staging_id, state))
            elif state == "prepared":
                publication = self.prepared[staging_id]
                allocations.append(
                    RegisteredStagingAllocation(
                        staging_id,
                        state,
                        publication.content_ref,
                        publication_digest(publication),
                    )
                )
            elif state == "published":
                publication = self.publications[staging_id]
                allocations.append(
                    RegisteredStagingAllocation(
                        staging_id,
                        state,
                        publication.content_ref,
                        publication_digest(publication),
                    )
                )
        return tuple(allocations)

    def expected_tombstones(self, *, store_id):
        del store_id
        return ()

    def expected_staging_cleanups(self, *, store_id):
        del store_id
        return ()

    def transitional_gc_refs(self, *, store_id):
        del store_id
        return ()


class _Inventory:
    def __init__(
        self,
        refs,
        edges=(),
        staging=(),
        facts=(),
        staging_allocations=None,
        tombstones=(),
        cleanup=(),
        staging_cleanups=(),
    ) -> None:
        self.refs = tuple(refs)
        self.edges = tuple(edges)
        self.staging = tuple(staging)
        self.facts = tuple(facts)
        self.staging_allocations = (
            None if staging_allocations is None else tuple(staging_allocations)
        )
        self.tombstones = tuple(tombstones)
        self.cleanup = tuple(cleanup)
        self.staging_cleanups = tuple(staging_cleanups)

    def expected_live_refs(self, *, store_id):
        del store_id
        return self.refs

    def registered_edges(self, *, store_id):
        del store_id
        return self.edges

    def registered_content(self, *, store_id):
        del store_id
        return self.facts

    def expected_staging_allocations(self, *, store_id):
        del store_id
        if self.staging_allocations is None:
            return tuple(RegisteredStagingAllocation(item, "allocated") for item in self.staging)
        return self.staging_allocations

    def expected_tombstones(self, *, store_id):
        del store_id
        return self.tombstones

    def expected_staging_cleanups(self, *, store_id):
        del store_id
        return self.staging_cleanups

    def transitional_gc_refs(self, *, store_id):
        del store_id
        return tuple(
            {
                *self.cleanup,
                *(item.content_ref for item in self.tombstones if item.state == "deleting"),
            }
        )


class LocalContentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = LocalContentStore(
            self.root / "store",
            store_id="local-a",
        )
        self.retention = _RecordingRetention(
            store_id=self.store.store_id,
            root_marker=self.store.root_marker,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _representative_tree(self) -> None:
        (self.source / "empty").mkdir()
        nested = self.source / "nested"
        nested.mkdir()
        (self.source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        executable = nested / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    def capture(self, change_id: str):
        return self.store.capture(change_id=change_id, authority=self.retention)

    def _prepare_abandoned_blob_stage(self, receipt, staging_id: str) -> PublishedObject:
        original = receipt.publication
        publication = PublishedObject(
            staging_id=staging_id,
            store_id=self.store.store_id,
            content_ref=original.content_ref,
            kind=original.kind,
            logical_bytes=original.logical_bytes,
            physical_bytes=original.physical_bytes,
            metadata=original.metadata,
            edges=original.edges,
        )
        stage = self.store.staging / staging_id
        stage.mkdir(mode=0o700)
        marker = stage / "publication.json"
        marker.write_bytes(content_module._publication_bytes(publication))
        marker.chmod(0o400)
        return publication

    def _abandoned_cleanup_decision(
        self,
        *,
        staging_id: str,
        cleanup_token: str,
        publication,
        remove_live_orphan: bool,
    ) -> AbandonedStagingCleanupDecision:
        return AbandonedStagingCleanupDecision(
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
            staging_id=staging_id,
            cleanup_token=cleanup_token,
            content_ref=(publication.content_ref if publication is not None else None),
            publication_digest=(
                publication_digest(publication) if publication is not None else None
            ),
            remove_live_orphan=remove_live_orphan,
        )

    def test_deterministic_blob_tree_manifests_and_generated_names(self) -> None:
        self._representative_tree()
        first = self.capture("change-a").seal_tree(
            source=AllowedTreeSource(self.source),
        )
        entries = {entry.path: entry for entry in first.manifest.entries}
        self.assertEqual(entries["empty"].kind, "directory")
        self.assertEqual(entries["nested"].kind, "directory")
        self.assertTrue(entries["nested/run.sh"].executable)
        self.assertEqual(first.manifest, self.store.verify_tree(first.snapshot_ref))
        self.assertEqual(list(self.store.staging.iterdir()), [])
        self.assertTrue(
            all(re.fullmatch(r"stage-[0-9a-f]{32}", item.staging_id) for item in first.publications)
        )
        for publication in first.publications:
            object_directory = self.store._object_directory(publication.content_ref)
            self.assertEqual(object_directory.name, publication.content_ref.digest)
            self.assertEqual(object_directory.parent.name, publication.content_ref.digest[:2])

        for index, path in enumerate([self.source, *self.source.rglob("*")], start=10):
            os.utime(path, (index * 100, index * 100), follow_symlinks=False)
        second_store = LocalContentStore(
            self.root / "second-store",
            store_id="local-b",
        )
        second_retention = _RecordingRetention(
            store_id=second_store.store_id,
            root_marker=second_store.root_marker,
        )
        second = second_store.capture(
            change_id="change-b", authority=second_retention
        ).seal_tree(
            source=AllowedTreeSource(self.source),
        )
        self.assertEqual(second.snapshot_ref, first.snapshot_ref)
        self.assertEqual(second.manifest.to_bytes(), first.manifest.to_bytes())

    def test_real_ledger_retains_tree_until_exact_owner_commit(self) -> None:
        ledger = RealmLedger(self.root / "realm.sqlite3")
        ledger.register_principal(
            operation_id="principal-register",
            principal_id="principal-a",
            kind="human",
        )
        store = LocalContentStore(
            self.root / "integrated-store",
            store_id="integrated-local",
        )
        ledger.register_store(
            operation_id="store-register",
            store_id=store.store_id,
            backend_kind="local-cas",
            root_marker=store.root_marker,
        )
        ledger.create_owner(
            operation_id="owner-create",
            owner_id="workspace-a",
            owner_kind="workspace",
            principal_id="principal-a",
        )
        change = ledger.begin_owner_change(
            operation_id="owner-change-begin",
            actor_principal_id="principal-a",
            owner_id="workspace-a",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        authority = ledger.content_capture_handle(
            actor_principal_id="principal-a",
            change_id=change.change_id,
            store_id=store.store_id,
        )
        (self.source / "payload.txt").write_text("ledger-integrated", encoding="utf-8")
        receipt = store.capture(
            change_id=change.change_id,
            authority=authority,
        ).seal_tree(
            source=AllowedTreeSource(self.source),
        )
        membership = OwnerMembership(
            store_id=store.store_id,
            content_ref=receipt.snapshot_ref,
            role="workspace-base",
        )
        planned = ledger.hold_owner_content(
            operation_id="owner-change-hold",
            actor_principal_id="principal-a",
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.assertEqual(planned, (membership,))
        committed = ledger.commit_owner_change(
            operation_id="owner-change-commit",
            actor_principal_id="principal-a",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        self.assertEqual(committed.owner_revision, 1)
        self.assertEqual(
            ledger.list_owner_memberships(
                actor_principal_id="principal-a",
                owner_id="workspace-a",
            ),
            (membership,),
        )
        self.assertEqual(store.verify_tree(receipt.snapshot_ref), receipt.manifest)
        clean = fsck_local_store(
            store,
            inventory_factory=lambda: ledger.content_inventory_snapshot(
                store_id=store.store_id
            ),
        )
        self.assertTrue(clean.ok, clean.issues)

        connection = sqlite3.connect(ledger.database_path)
        try:
            connection.execute(
                "UPDATE content_edges SET canonical_path = 'wrong.txt' "
                "WHERE store_id = ? AND parent_ref = ?",
                (store.store_id, str(receipt.snapshot_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        edge_report = fsck_local_store(
            store,
            inventory_factory=lambda: ledger.content_inventory_snapshot(
                store_id=store.store_id
            ),
        )
        self.assertIn(
            "registered_edge_mismatch",
            {issue.code for issue in edge_report.issues},
        )

        connection = sqlite3.connect(ledger.database_path)
        try:
            connection.execute(
                "UPDATE content_edges SET canonical_path = 'payload.txt' "
                "WHERE store_id = ? AND parent_ref = ?",
                (store.store_id, str(receipt.snapshot_ref)),
            )
            connection.execute(
                "UPDATE content_objects SET logical_bytes = logical_bytes + 1 "
                "WHERE store_id = ? AND content_ref = ?",
                (store.store_id, str(receipt.snapshot_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        fact_report = fsck_local_store(
            store,
            inventory_factory=lambda: ledger.content_inventory_snapshot(
                store_id=store.store_id
            ),
        )
        self.assertIn(
            "registered_fact_mismatch",
            {issue.code for issue in fact_report.issues},
        )

    def test_capture_handle_rejects_same_store_id_at_another_physical_root(self) -> None:
        ledger = RealmLedger(self.root / "binding-realm.sqlite3")
        ledger.register_principal(
            operation_id="binding-principal",
            principal_id="operator",
            kind="human",
        )
        registered = LocalContentStore(
            self.root / "registered-store",
            store_id="shared-logical-id",
        )
        impostor = LocalContentStore(
            self.root / "impostor-store",
            store_id="shared-logical-id",
        )
        try:
            ledger.register_store(
                operation_id="binding-store",
                store_id=registered.store_id,
                backend_kind=registered.BACKEND_KIND,
                root_marker=registered.root_marker,
            )
            ledger.create_owner(
                operation_id="binding-owner",
                owner_id="binding-owner",
                owner_kind="workspace",
                principal_id="operator",
            )
            change = ledger.begin_owner_change(
                operation_id="binding-change",
                actor_principal_id="operator",
                owner_id="binding-owner",
                expected_owner_revision=0,
                ttl_seconds=60,
            )
            authority = ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change.change_id,
                store_id=registered.store_id,
            )
            (self.source / "binding.txt").write_text("payload", encoding="utf-8")
            with self.assertRaises(RealmNotFound):
                impostor.capture(
                    change_id=change.change_id,
                    authority=authority,
                ).seal_blob(source=AllowedFileSource(self.source, "binding.txt"))
            self.assertEqual(tuple(impostor.staging.iterdir()), ())
            self.assertEqual(tuple(impostor.iter_live_refs()), ())
        finally:
            registered.close()
            impostor.close()

    def test_standalone_blob_capture_is_descriptor_rooted_and_bounded(self) -> None:
        nested = self.source / "nested"
        nested.mkdir()
        payload = nested / "payload.bin"
        payload.write_bytes(b"standalone blob")
        receipt = self.capture("blob-change").seal_blob(
            source=AllowedFileSource(self.source, "nested/payload.bin"),
        )
        self.assertEqual(receipt.blob_ref, BlobRef.from_bytes(b"standalone blob"))
        self.assertEqual(self.store.verify_blob(receipt.blob_ref).size, len(b"standalone blob"))

        with self.assertRaises(ContentRejected):
            self.capture("traversal").seal_blob(
                source=AllowedFileSource(self.source, "../outside"),
            )
        with self.assertRaises(ContentRejected):
            self.capture("too-large").seal_blob(
                source=AllowedFileSource(self.source, "nested/payload.bin"),
                limits=SealLimits(max_file_bytes=2),
            )

    def test_expected_generation_fails_closed_until_coordinator_exists(self) -> None:
        (self.source / "a.txt").write_text("a", encoding="utf-8")
        with self.assertRaisesRegex(ContentRejected, "revision coordinator"):
            self.capture("tree-generation").seal_tree(
                source=AllowedTreeSource(self.source, expected_generation=1),
            )
        with self.assertRaisesRegex(ContentRejected, "revision coordinator"):
            self.capture("blob-generation").seal_blob(
                source=AllowedFileSource(self.source, "a.txt", expected_generation=1),
            )
        self.assertEqual(self.retention.states, {})

    def test_prepared_marker_recovers_failure_after_rename_and_chmod(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("prepared payload", encoding="utf-8")
        original_finalize = content_module._finalize_visible_object_fd

        def finalize_then_fail(prefix_fd: int, digest: str) -> None:
            original_finalize(prefix_fd, digest)
            raise OSError("injected failure after rename, chmod, and object fsync")

        with mock.patch.object(
            content_module,
            "_finalize_visible_object_fd",
            side_effect=finalize_then_fail,
        ):
            with self.assertRaisesRegex(OSError, "after rename"):
                self.capture("prepared-fsync").seal_blob(
                    source=AllowedFileSource(self.source, "payload.txt"),
                )

        prepared_ids = [
            staging_id
            for staging_id, state in self.retention.states.items()
            if state == "prepared"
        ]
        self.assertEqual(len(prepared_ids), 1)
        staging_id = prepared_ids[0]
        publication = self.store.load_prepared_publication(staging_id)
        self.assertTrue(self.store.has_object(publication.content_ref))
        self.assertTrue((self.store.staging / staging_id / "publication.json").is_file())
        recovered = self.capture("prepared-fsync").recover_prepared_publication(
            staging_id=staging_id,
        )
        self.assertEqual(recovered, publication)
        self.assertEqual(self.retention.states[staging_id], "finalized")
        self.assertFalse((self.store.staging / staging_id).exists())
        self.store.verify_blob(recovered.content_ref)  # type: ignore[arg-type]

    def test_record_failure_after_full_fsync_leaves_recoverable_prepared_state(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("lost ledger response", encoding="utf-8")
        self.retention.fail_record_once = True
        with self.assertRaisesRegex(RuntimeError, "after durable filesystem"):
            self.capture("prepared-record").seal_blob(
                source=AllowedFileSource(self.source, "payload.txt"),
            )
        staging_id = next(
            staging_id
            for staging_id, state in self.retention.states.items()
            if state == "prepared"
        )
        publication = self.store.load_prepared_publication(staging_id)
        self.capture("prepared-record").recover_prepared_publication(
            staging_id=staging_id,
        )
        self.assertEqual(self.retention.states[staging_id], "finalized")
        self.assertTrue(self.store.has_object(publication.content_ref))

    def test_recovery_replays_ledger_prepare_after_marker_only_crash(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("marker was durable", encoding="utf-8")

        # Model process death after publication.json is fsynced but before the
        # ledger observes prepare.  Normal exception cleanup is disabled only
        # to preserve the exact on-disk crash state for recovery.
        with (
            mock.patch.object(
                self.retention,
                "prepare_publication",
                side_effect=RuntimeError("injected marker-only crash"),
            ),
            mock.patch.object(self.store, "_abandon_staging", return_value=None),
            mock.patch.object(content_module, "_remove_private_tree_at", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "marker-only crash"):
                self.capture("marker-only").seal_blob(
                    source=AllowedFileSource(self.source, "payload.txt"),
                )

        staging_id = next(
            staging_id
            for staging_id, state in self.retention.states.items()
            if state == "allocated"
        )
        self.assertTrue((self.store.staging / staging_id / "publication.json").is_file())

        publication = self.capture("marker-only").recover_prepared_publication(
            staging_id=staging_id,
        )
        self.assertEqual(self.retention.states[staging_id], "finalized")
        self.assertTrue(self.store.has_object(publication.content_ref))
        self.assertFalse((self.store.staging / staging_id).exists())

    def test_recovery_rejects_corrupted_marker_only_staging(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("marker was durable", encoding="utf-8")
        with (
            mock.patch.object(
                self.retention,
                "prepare_publication",
                side_effect=RuntimeError("injected marker-only crash"),
            ),
            mock.patch.object(self.store, "_abandon_staging", return_value=None),
            mock.patch.object(content_module, "_remove_private_tree_at", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "marker-only crash"):
                self.capture("corrupt-marker-only").seal_blob(
                    source=AllowedFileSource(self.source, "payload.txt"),
                )

        staging_id = next(
            staging_id
            for staging_id, state in self.retention.states.items()
            if state == "allocated"
        )
        publication = self.store.load_prepared_publication(staging_id)
        staged_data = self.store.staging / staging_id / "object" / "data"
        staged_data.chmod(0o600)
        staged_data.write_bytes(b"corrupted after crash")
        staged_data.chmod(0o400)

        with self.assertRaises(ContentCorrupt):
            self.capture("corrupt-marker-only").recover_prepared_publication(
                staging_id=staging_id,
            )
        self.assertFalse(self.store.has_object(publication.content_ref))
        self.assertEqual(self.retention.states[staging_id], "prepared")

    @unittest.skipIf(os.name == "nt", "exact directory-mode recovery is POSIX-only")
    def test_recovery_adopts_verified_object_after_crash_before_finalize(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("renamed but not chmodded", encoding="utf-8")
        with mock.patch.object(
            content_module,
            "_finalize_visible_object_fd",
            side_effect=RuntimeError("injected crash before finalize"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before finalize"):
                self.capture("pre-finalize").seal_blob(
                    source=AllowedFileSource(self.source, payload.name),
                )

        staging_id = next(
            staging_id
            for staging_id, state in self.retention.states.items()
            if state == "prepared"
        )
        publication = self.store.load_prepared_publication(staging_id)
        visible = self.store._object_directory(publication.content_ref)
        self.assertEqual(stat.S_IMODE(visible.stat().st_mode), 0o700)

        recovered = self.capture("pre-finalize").recover_prepared_publication(
            staging_id=staging_id,
        )
        self.assertEqual(recovered, publication)
        self.assertEqual(stat.S_IMODE(visible.stat().st_mode), 0o500)
        self.assertEqual(self.retention.states[staging_id], "finalized")

    def test_store_and_object_metadata_reads_reject_symlinks(self) -> None:
        external_marker = self.root / "external-store.json"
        external_marker.write_bytes(
            canonical_json_bytes(
                {
                    "format": LocalContentStore.STORE_FORMAT,
                    "root_marker": "a" * 32,
                    "store_id": "evil-store",
                }
            )
        )
        evil_root = self.root / "evil-store"
        evil_root.mkdir(mode=0o700)
        os.symlink(external_marker, evil_root / "store.json")
        with self.assertRaisesRegex(RealmIntegrityError, "unreadable"):
            LocalContentStore(
                evil_root,
                store_id="evil-store",
            )

        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("manifest-symlink").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        object_directory = self.store._object_directory(receipt.blob_ref)
        manifest = object_directory / "manifest.json"
        external_manifest = self.root / "external-manifest.json"
        os.chmod(object_directory, 0o700)
        os.rename(manifest, external_manifest)
        os.symlink(external_manifest, manifest)
        try:
            with self.assertRaises(ContentCorrupt):
                self.store.verify_blob(receipt.blob_ref)
        finally:
            manifest.unlink()
            os.rename(external_manifest, manifest)
            os.chmod(object_directory, 0o500)

        prefix = object_directory.parent
        saved_prefix = prefix.with_name(prefix.name + "-saved")
        os.rename(prefix, saved_prefix)
        os.symlink(saved_prefix, prefix)
        try:
            with self.assertRaises(ContentCorrupt):
                self.store.verify_blob(receipt.blob_ref)
            with self.assertRaises(RealmIntegrityError):
                self.store.has_object(receipt.blob_ref)
        finally:
            prefix.unlink()
            os.rename(saved_prefix, prefix)

    def test_manifest_rejects_nonportable_paths_topology_and_limits(self) -> None:
        blob = BlobRef.from_bytes(b"x")
        for path in (
            "../escape",
            "a/../escape",
            "CON.txt",
            "trailing.",
            "bad\\name",
            "cafe\u0301.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ContentRejected):
                    validate_portable_path(path)
        with self.assertRaisesRegex(ContentRejected, "case-insensitive"):
            validate_portable_paths(["Alpha", "alpha"])
        with self.assertRaisesRegex(ContentRejected, "maximum depth"):
            validate_portable_path("a/b/c", limits=SealLimits(max_depth=2))
        with self.assertRaisesRegex(ContentRejected, "entry count"):
            validate_portable_paths(["a", "b"], limits=SealLimits(max_entries=1))
        with self.assertRaisesRegex(ContentRejected, "unrepresented parent"):
            TreeManifest.build([TreeEntry.file("a/b", blob_ref=blob, size=1, executable=False)])
        with self.assertRaisesRegex(ContentRejected, "descends through file"):
            TreeManifest.build(
                [
                    TreeEntry.file("a", blob_ref=blob, size=1, executable=False),
                    TreeEntry.file("a/b", blob_ref=blob, size=1, executable=False),
                ]
            )
        with self.assertRaisesRegex(ContentRejected, "maximum size"):
            TreeManifest.build(
                [TreeEntry.file("large", blob_ref=blob, size=3, executable=False)],
                limits=SealLimits(max_file_bytes=2),
            )

        oversized = canonical_json_bytes(
            {
                "entries": [
                    {
                        "blob": str(blob),
                        "executable": False,
                        "path": "large",
                        "size": SealLimits().max_file_bytes + 1,
                        "type": "file",
                    }
                ],
                "format": "optpilot.tree.v1",
            }
        )
        with self.assertRaises(ContentCorrupt):
            TreeManifest.from_bytes(oversized)

    def test_selection_and_tree_nodes_reject_symlinks_and_special_files(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        os.symlink(outside, self.source / "gateway")
        with self.assertRaises(ContentRejected):
            self.capture("selection-symlink").seal_tree(
                source=AllowedTreeSource(self.root, "source/gateway"),
            )
        with self.assertRaises(ContentRejected):
            self.capture("selection-traversal").seal_tree(
                source=AllowedTreeSource(self.root, "source/../outside"),
            )
        with self.assertRaisesRegex(ContentRejected, "Symlinks"):
            self.capture("tree-symlink").seal_tree(
                source=AllowedTreeSource(self.source),
            )
        (self.source / "gateway").unlink()

        if hasattr(os, "mkfifo"):
            os.mkfifo(self.source / "pipe")
            with self.assertRaisesRegex(ContentRejected, "Special filesystem node"):
                self.capture("tree-fifo").seal_tree(
                    source=AllowedTreeSource(self.source),
                )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_standalone_fifo_is_rejected_without_waiting_for_a_writer(self) -> None:
        os.mkfifo(self.source / "standalone-pipe")
        with self.assertRaisesRegex(ContentRejected, "regular file"):
            self.capture("blob-fifo").seal_blob(
                source=AllowedFileSource(self.source, "standalone-pipe"),
            )

    def test_noncontent_darwin_marker_is_narrow_and_other_xattrs_reject(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        custom_name = "com.example.optpilot-test" if sys.platform == "darwin" else "user.optpilot-test"
        if hasattr(os, "setxattr"):
            try:
                os.setxattr(payload, custom_name.encode(), b"present")  # type: ignore[attr-defined]
            except OSError:
                self.skipTest("filesystem does not permit a test xattr")
        elif sys.platform == "darwin":
            process = subprocess.run(
                ["/usr/bin/xattr", "-w", custom_name, "present", str(payload)],
                capture_output=True,
                text=True,
                check=False,
            )
            if process.returncode:
                self.skipTest("filesystem does not permit a test xattr")
        else:
            self.skipTest("platform has no xattr writer")
        with self.assertRaisesRegex(ContentRejected, "Extended attributes"):
            self.capture("tree-xattr").seal_tree(
                source=AllowedTreeSource(self.source),
            )

    def test_capture_can_omit_declared_generated_directory_names(self) -> None:
        (self.source / "source.py").write_text("value = 1\n", encoding="utf-8")
        dependency = self.source / "frontend" / "node_modules" / "package"
        dependency.mkdir(parents=True)
        (dependency / "index.js").write_text("generated\n", encoding="utf-8")
        root_dependency = self.source / "node_modules" / "root-package"
        root_dependency.mkdir(parents=True)
        (root_dependency / "index.js").write_text("generated\n", encoding="utf-8")

        original_reject_xattrs = content_module._reject_xattrs

        def reject_generated_xattrs(fd, path, *, ignored):
            if "node_modules" in path.split("/"):
                raise ContentRejected(
                    f"Extended attributes are unsupported in v1 trees: {path!r}."
                )
            return original_reject_xattrs(fd, path, ignored=ignored)

        with mock.patch.object(
            content_module,
            "_reject_xattrs",
            side_effect=reject_generated_xattrs,
        ):
            with self.assertRaisesRegex(ContentRejected, "Extended attributes"):
                self.capture("tree-generated-default").seal_tree(
                    source=AllowedTreeSource(self.source),
                )
            first = self.capture("tree-generated-excluded").seal_tree(
                source=AllowedTreeSource(
                    self.source,
                    excluded_directory_names=("node_modules",),
                ),
            )

        paths = {entry.path for entry in first.manifest.entries}
        self.assertIn("source.py", paths)
        self.assertIn("frontend", paths)
        self.assertFalse(any("node_modules" in path.split("/") for path in paths))

        (dependency / "index.js").write_text(
            "changed but still generated\n", encoding="utf-8"
        )
        second = self.capture("tree-generated-excluded-replay").seal_tree(
            source=AllowedTreeSource(
                self.source,
                excluded_directory_names=("node_modules",),
            ),
        )
        self.assertEqual(second.snapshot_ref, first.snapshot_ref)

    def test_excluded_directory_names_are_canonical_basenames(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be a tuple"):
            AllowedTreeSource(  # type: ignore[arg-type]
                self.source,
                excluded_directory_names=["node_modules"],
            )
        with self.assertRaisesRegex(ValueError, "single directory names"):
            AllowedTreeSource(
                self.source,
                excluded_directory_names=("frontend/node_modules",),
            )
        with self.assertRaisesRegex(ValueError, "unique and sorted"):
            AllowedTreeSource(
                self.source,
                excluded_directory_names=("node_modules", "node_modules"),
            )

    def test_matching_darwin_docker_ownership_xattr_is_redundant(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        payload.chmod(0o640)
        descriptor = os.open(payload, os.O_RDONLY)
        try:
            with (
                mock.patch.object(content_module.sys, "platform", "darwin"),
                mock.patch.object(
                    content_module,
                    "_fd_xattrs",
                    return_value=("com.docker.grpcfuse.ownership",),
                ),
                mock.patch.object(
                    content_module,
                    "_fd_xattr_value",
                    return_value=b'{"UID":-1,"GID":-1,"mode":640}',
                ),
            ):
                content_module._reject_xattrs(
                    descriptor,
                    "payload.txt",
                    ignored=content_module.DARWIN_IGNORED_NONCONTENT_XATTRS,
                )
        finally:
            os.close(descriptor)

    def test_mismatched_darwin_docker_ownership_mode_rejects(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        payload.chmod(0o640)
        descriptor = os.open(payload, os.O_RDONLY)
        try:
            with (
                mock.patch.object(content_module.sys, "platform", "darwin"),
                mock.patch.object(
                    content_module,
                    "_fd_xattrs",
                    return_value=("com.docker.grpcfuse.ownership",),
                ),
                mock.patch.object(
                    content_module,
                    "_fd_xattr_value",
                    return_value=b'{"UID":-1,"GID":-1,"mode":600}',
                ),
                self.assertRaisesRegex(ContentRejected, "Extended attributes"),
            ):
                content_module._reject_xattrs(
                    descriptor,
                    "payload.txt",
                    ignored=content_module.DARWIN_IGNORED_NONCONTENT_XATTRS,
                )
        finally:
            os.close(descriptor)

    def test_malformed_darwin_docker_ownership_xattr_rejects(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        payload.chmod(0o640)
        malformed_values = (
            b"not-json",
            b'{"UID":-1,"GID":-1,"mode":"640"}',
            b'{"UID":-1,"GID":-1,"mode":640,"future":1}',
            b'{"UID":-1,"GID":-1,"mode":640,"mode":640}',
        )
        descriptor = os.open(payload, os.O_RDONLY)
        try:
            with (
                mock.patch.object(content_module.sys, "platform", "darwin"),
                mock.patch.object(
                    content_module,
                    "_fd_xattrs",
                    return_value=("com.docker.grpcfuse.ownership",),
                ),
            ):
                for value in malformed_values:
                    with (
                        self.subTest(value=value),
                        mock.patch.object(
                            content_module,
                            "_fd_xattr_value",
                            return_value=value,
                        ),
                        self.assertRaisesRegex(ContentRejected, "Extended attributes"),
                    ):
                        content_module._reject_xattrs(
                            descriptor,
                            "payload.txt",
                            ignored=content_module.DARWIN_IGNORED_NONCONTENT_XATTRS,
                        )
        finally:
            os.close(descriptor)

    def test_redundant_darwin_docker_xattr_does_not_hide_unrelated_xattr(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        payload.chmod(0o640)
        descriptor = os.open(payload, os.O_RDONLY)
        try:
            with (
                mock.patch.object(content_module.sys, "platform", "darwin"),
                mock.patch.object(
                    content_module,
                    "_fd_xattrs",
                    return_value=(
                        "com.docker.grpcfuse.ownership",
                        "com.example.optpilot-test",
                    ),
                ),
                mock.patch.object(
                    content_module,
                    "_fd_xattr_value",
                    return_value=b'{"UID":-1,"GID":-1,"mode":640}',
                ),
                self.assertRaisesRegex(ContentRejected, "Extended attributes"),
            ):
                content_module._reject_xattrs(
                    descriptor,
                    "payload.txt",
                    ignored=content_module.DARWIN_IGNORED_NONCONTENT_XATTRS,
                )
        finally:
            os.close(descriptor)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin xattr policy")
    def test_darwin_file_provider_recency_marker_is_noncontent(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        process = subprocess.run(
            [
                "/usr/bin/xattr",
                "-w",
                "com.apple.lastuseddate#PS",
                "provider-owned-recency",
                str(payload),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            self.skipTest("filesystem does not permit the file-provider xattr")
        sealed = self.capture("tree-file-provider-recency").seal_tree(
            source=AllowedTreeSource(self.source),
        )
        self.assertEqual(sealed.manifest.entries[-1].path, "payload.txt")

    def test_sparse_files_are_rejected(self) -> None:
        sparse = self.source / "sparse.bin"
        with sparse.open("wb") as stream:
            stream.seek(64 * 1024 * 1024)
            stream.write(b"x")
        info = sparse.stat()
        if not hasattr(info, "st_blocks") or info.st_blocks * 512 >= info.st_size:
            self.skipTest("filesystem did not create a sparse file")
        with self.assertRaisesRegex(ContentRejected, "Sparse"):
            self.capture("tree-sparse").seal_tree(
                source=AllowedTreeSource(self.source),
            )

    def test_post_publication_source_change_rolls_back_exact_known_hold(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("original", encoding="utf-8")
        original_publish = self.store._publish_blob_from_fd

        def publish_then_mutate(**kwargs):
            publication = original_publish(**kwargs)
            payload.write_text("changed after publication", encoding="utf-8")
            return publication

        with mock.patch.object(self.store, "_publish_blob_from_fd", side_effect=publish_then_mutate):
            with self.assertRaises(SourceChanged):
                self.capture("changing-tree").seal_tree(
                    source=AllowedTreeSource(self.source),
                )

        self.assertEqual(len(self.retention.rollback_batches), 1)
        self.assertEqual(len(self.retention.rollback_batches[0]), 1)
        self.assertEqual(self.retention.publications, {})
        report = fsck_local_store(self.store, inventory_factory=lambda: self.retention)
        self.assertIn("physical_orphan", {issue.code for issue in report.issues})

    def test_fsck_composes_physical_and_ledger_inventory_without_mutation(self) -> None:
        self._representative_tree()
        receipt = self.capture("fsck-tree").seal_tree(
            source=AllowedTreeSource(self.source),
        )
        clean = fsck_local_store(self.store, inventory_factory=lambda: self.retention)
        self.assertTrue(clean.ok, clean.issues)

        physical_refs = set(self.retention.expected_live_refs(store_id=self.store.store_id))
        blob_ref = next(item for item in physical_refs if isinstance(item, BlobRef))
        missing_ref = BlobRef.from_bytes(b"registered but absent")
        stage_id = "stage-" + "a" * 32
        (self.store.staging / stage_id).mkdir(mode=0o700)
        edge = RegisteredContentEdge(receipt.snapshot_ref, blob_ref, "alpha.txt")
        inventory = _Inventory(
            refs=(receipt.snapshot_ref, missing_ref),
            edges=(edge,),
            staging=(),
        )
        before = set(path.relative_to(self.store.root) for path in self.store.root.rglob("*"))
        report = fsck_local_store(self.store, inventory_factory=lambda: inventory)
        after = set(path.relative_to(self.store.root) for path in self.store.root.rglob("*"))
        self.assertEqual(after, before)
        codes = {issue.code for issue in report.issues}
        self.assertIn("physical_orphan", codes)
        self.assertIn("registered_bytes_missing", codes)
        self.assertIn("dangling_registered_closure", codes)
        self.assertIn("stale_staging", codes)

    def test_fsck_reconciles_tombstone_tokens_and_prepared_staging_state(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("fsck-transitions").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        deleting_token = new_deletion_token()
        orphan_token = new_deletion_token()
        (self.store.trash / orphan_token).mkdir(mode=0o700)
        prepared_id = "stage-" + "a" * 32
        (self.store.staging / prepared_id).mkdir(mode=0o700)
        inventory = _Inventory(
            refs=(),
            staging_allocations=(
                RegisteredStagingAllocation(
                    prepared_id,
                    "prepared",
                    receipt.blob_ref,
                    "0" * 64,
                ),
            ),
            tombstones=(
                RegisteredTombstone(receipt.blob_ref, "deleting", deleting_token),
            ),
        )

        report = fsck_local_store(self.store, inventory_factory=lambda: inventory)
        codes = {issue.code for issue in report.issues}
        self.assertNotIn("physical_orphan", codes)
        self.assertIn("expected_tombstone_missing", codes)
        self.assertIn("orphan_tombstone", codes)
        self.assertIn("invalid_prepared_staging", codes)
        self.assertFalse(report.ok)

    def test_fsck_rejects_empty_trash_even_for_registered_token(self) -> None:
        blob_ref = BlobRef.from_bytes(b"expected trash payload")
        token = new_deletion_token()
        (self.store.trash / token).mkdir(mode=0o700)
        inventory = _Inventory(
            refs=(),
            tombstones=(RegisteredTombstone(blob_ref, "deleting", token),),
        )
        report = fsck_local_store(self.store, inventory_factory=lambda: inventory)
        self.assertIn("corrupt_tombstone", {issue.code for issue in report.issues})

    def test_fsck_compares_prepared_marker_to_exact_ledger_digest(self) -> None:
        payload = self.source / "prepared.txt"
        payload.write_text("prepared bytes", encoding="utf-8")
        self.retention.fail_record_once = True
        with self.assertRaisesRegex(RuntimeError, "durable filesystem"):
            self.capture("fsck-prepared-digest").seal_blob(
                source=AllowedFileSource(self.source, payload.name),
            )
        staging_id, publication = next(iter(self.retention.prepared.items()))
        inventory = _Inventory(
            refs=(),
            staging_allocations=(
                RegisteredStagingAllocation(
                    staging_id,
                    "prepared",
                    publication.content_ref,
                    "0" * 64,
                ),
            ),
        )
        report = fsck_local_store(self.store, inventory_factory=lambda: inventory)
        self.assertIn(
            "prepared_staging_mismatch",
            {issue.code for issue in report.issues},
        )

    def test_fsck_handles_published_stage_and_remove_before_finalize_gap(self) -> None:
        payload = self.source / "published-gap.txt"
        payload.write_text("published gap", encoding="utf-8")
        original_remove = self.store._remove_staging
        with mock.patch.object(
            self.store,
            "_remove_staging",
            side_effect=RuntimeError("injected before stage removal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before stage removal"):
                self.capture("published-gap").seal_blob(
                    source=AllowedFileSource(self.source, payload.name),
                )
        staging_id = next(
            item for item, state in self.retention.states.items() if state == "published"
        )
        present = fsck_local_store(
            self.store,
            inventory_factory=lambda: self.retention,
        )
        self.assertTrue(present.ok, present.issues)
        self.assertNotIn(
            "prepared_staging_mismatch",
            {issue.code for issue in present.issues},
        )

        original_remove(staging_id)
        missing = fsck_local_store(
            self.store,
            inventory_factory=lambda: self.retention,
        )
        self.assertTrue(missing.ok, missing.issues)
        self.assertIn(
            "published_staging_missing",
            {issue.code for issue in missing.issues},
        )

    def test_fsck_reports_corruption_but_does_not_repair_it(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("corrupt-blob").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        object_directory = self.store._object_directory(receipt.blob_ref)
        data = object_directory / "data"
        os.chmod(object_directory, 0o700)
        os.chmod(data, 0o600)
        data.write_bytes(b"corrupt")
        os.chmod(data, 0o400)
        os.chmod(object_directory, 0o500)
        manifest_path = object_directory / "manifest.json"
        os.chmod(manifest_path, 0o600)
        report = fsck_local_store(self.store, inventory_factory=lambda: self.retention)
        codes = {issue.code for issue in report.issues}
        self.assertIn("corrupt_blob", codes)
        self.assertIn("writable_managed_file", codes)
        self.assertTrue(data.exists())
        self.assertEqual(data.read_bytes(), b"corrupt")

    def test_fsck_fully_verifies_tree_children_and_entry_sizes(self) -> None:
        (self.source / "payload.txt").write_text("payload", encoding="utf-8")
        receipt = self.capture("missing-child").seal_tree(
            source=AllowedTreeSource(self.source),
        )
        blob_ref = next(
            entry.blob_ref
            for entry in receipt.manifest.entries
            if entry.kind == "file"
        )
        assert blob_ref is not None
        LocalGcBackend(self.store).tombstone(
            blob_ref,
            deletion_token=new_deletion_token(),
            still_eligible=lambda: True,
        )
        report = fsck_local_store(self.store, inventory_factory=lambda: self.retention)
        codes = {issue.code for issue in report.issues}
        self.assertIn("missing_tree_blob", codes)
        self.assertIn("corrupt_tree", codes)
        self.assertIn("registered_bytes_missing", codes)

    def test_fsck_allows_gc_to_delete_child_before_pending_parent(self) -> None:
        (self.source / "payload.txt").write_text("payload", encoding="utf-8")
        receipt = self.capture("gc-ordering").seal_tree(
            source=AllowedTreeSource(self.source),
        )
        blob_ref = next(
            entry.blob_ref
            for entry in receipt.manifest.entries
            if entry.kind == "file"
        )
        assert blob_ref is not None
        backend = LocalGcBackend(self.store)
        token = new_deletion_token()
        backend.reconcile(
            blob_ref,
            deletion_token=token,
            desired_state="deleted",
            recheck=lambda: True,
        )
        tree_publication = next(
            item for item in receipt.publications if item.content_ref == receipt.snapshot_ref
        )
        inventory = _Inventory(
            refs=(receipt.snapshot_ref,),
            facts=(
                RegisteredContentFact(
                    content_ref=receipt.snapshot_ref,
                    kind=tree_publication.kind,
                    logical_bytes=tree_publication.logical_bytes,
                    physical_bytes=tree_publication.physical_bytes,
                    metadata=tree_publication.metadata,
                ),
            ),
            cleanup=(receipt.snapshot_ref,),
        )
        report = fsck_local_store(self.store, inventory_factory=lambda: inventory)
        self.assertTrue(report.ok, report.issues)
        codes = {issue.code for issue in report.issues}
        self.assertNotIn("missing_tree_blob", codes)
        self.assertNotIn("registered_edge_missing", codes)

    def test_gc_hooks_recheck_under_store_lock_and_reconcile_idempotently(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("gc-blob").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        backend = LocalGcBackend(self.store)
        token = new_deletion_token()
        cancelled = backend.tombstone(
            receipt.blob_ref,
            deletion_token=token,
            still_eligible=lambda: False,
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertTrue(self.store.has_object(receipt.blob_ref))

        checked = threading.Event()
        completed = threading.Event()

        def move() -> None:
            backend.tombstone(
                receipt.blob_ref,
                deletion_token=token,
                still_eligible=lambda: checked.set() or True,
            )
            completed.set()

        with self.store.exclusive_lock():
            thread = threading.Thread(target=move)
            thread.start()
            self.assertFalse(checked.wait(timeout=0.1))
            self.assertFalse(completed.is_set())
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(checked.is_set())

        replay = backend.tombstone(
            receipt.blob_ref,
            deletion_token=token,
            still_eligible=lambda: True,
        )
        self.assertFalse(replay.moved)
        cancelled_delete = backend.delete(
            receipt.blob_ref,
            deletion_token=token,
            still_deletable=lambda: False,
        )
        self.assertEqual(cancelled_delete.state, "cancelled")
        deleted = backend.delete(
            receipt.blob_ref,
            deletion_token=token,
            still_deletable=lambda: True,
        )
        self.assertTrue(deleted.moved)
        replay_delete = backend.delete(
            receipt.blob_ref,
            deletion_token=token,
            still_deletable=lambda: True,
        )
        self.assertFalse(replay_delete.moved)

    def test_gc_cleanup_retries_after_partial_private_trash_deletion(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        backend = LocalGcBackend(self.store)

        first = self.capture("partial-delete").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        first_token = new_deletion_token()
        backend.tombstone(
            first.blob_ref,
            deletion_token=first_token,
            still_eligible=lambda: True,
        )
        (self.store.trash / first_token / "data").unlink()
        deleted = backend.reconcile(
            first.blob_ref,
            deletion_token=first_token,
            desired_state="deleted",
            recheck=lambda: True,
        )
        self.assertTrue(deleted.moved)
        self.assertFalse((self.store.trash / first_token).exists())

        second = self.capture("partial-cancelled-trash").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        second_token = new_deletion_token()
        backend.tombstone(
            second.blob_ref,
            deletion_token=second_token,
            still_eligible=lambda: True,
        )
        republished = self.capture("republish-partial-cancelled-trash").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        self.assertEqual(republished.blob_ref, second.blob_ref)
        (self.store.trash / second_token / "data").unlink()
        discarded = backend.discard_cancelled_tombstone(
            second.blob_ref,
            deletion_token=second_token,
            still_cancelled=lambda: True,
        )
        self.assertTrue(discarded.moved)
        self.assertFalse((self.store.trash / second_token).exists())

    def test_abandoned_staging_stage_only_cleanup_and_stale_claim(self) -> None:
        backend = LocalAbandonedStagingBackend(self.store)
        stale_id = "stage-" + "a" * 32
        stale = self.store.staging / stale_id
        stale.mkdir(mode=0o700)
        (stale / "partial").write_bytes(b"partial")
        stale_token = "cleanup-" + "a" * 32
        with self.assertRaisesRegex(RealmConflict, "stale"):
            backend.cleanup(
                staging_id=stale_id,
                cleanup_token=stale_token,
                validate=lambda: (_ for _ in ()).throw(
                    RealmConflict("cleanup claim is stale")
                ),
            )
        self.assertTrue(stale.is_dir())

        decision = self._abandoned_cleanup_decision(
            staging_id=stale_id,
            cleanup_token=stale_token,
            publication=None,
            remove_live_orphan=False,
        )
        receipt = backend.cleanup(
            staging_id=stale_id,
            cleanup_token=stale_token,
            validate=lambda: decision,
        )
        self.assertTrue(receipt.stage_removed)
        self.assertFalse(receipt.live_orphan_rehomed)
        self.assertFalse(stale.exists())

    def test_abandoned_staging_preserves_registered_or_protected_live_object(self) -> None:
        payload = self.source / "protected.txt"
        payload.write_text("protected", encoding="utf-8")
        captured = self.capture("protected-live").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "b" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        token = "cleanup-" + "b" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=False,
        )
        receipt = LocalAbandonedStagingBackend(self.store).cleanup(
            staging_id=staging_id,
            cleanup_token=token,
            validate=lambda: decision,
        )
        self.assertTrue(receipt.stage_removed)
        self.assertFalse(receipt.live_orphan_rehomed)
        self.assertTrue(self.store.has_object(captured.blob_ref))
        self.store.verify_blob(captured.blob_ref)

    def test_abandoned_staging_rehomes_live_orphan_before_private_delete(self) -> None:
        payload = self.source / "orphan.txt"
        payload.write_text("orphan", encoding="utf-8")
        captured = self.capture("orphan-live").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "c" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        collision = self.store.staging / staging_id / "object"
        collision.mkdir(mode=0o700)
        (collision / "partial").write_bytes(b"stale staged copy")
        token = "cleanup-" + "c" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=True,
        )
        receipt = LocalAbandonedStagingBackend(self.store).cleanup(
            staging_id=staging_id,
            cleanup_token=token,
            validate=lambda: decision,
        )
        self.assertTrue(receipt.stage_removed)
        self.assertTrue(receipt.live_orphan_rehomed)
        self.assertFalse(self.store.has_object(captured.blob_ref))
        self.assertFalse((self.store.staging / staging_id).exists())

    def test_abandoned_staging_cleanup_resumes_after_partial_rehome_delete(self) -> None:
        payload = self.source / "partial-rehome.txt"
        payload.write_text("partial rehome", encoding="utf-8")
        captured = self.capture("partial-rehome").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "d" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        stage = self.store.staging / staging_id
        object_path = self.store._object_directory(captured.blob_ref)
        object_path.chmod(0o700)
        os.rename(object_path, stage / "object")
        (stage / "publication.json").unlink()
        private_object = stage / "object"
        private_object.chmod(0o700)
        (private_object / "data").unlink()
        token = "cleanup-" + "d" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=True,
        )
        receipt = LocalAbandonedStagingBackend(self.store).cleanup(
            staging_id=staging_id,
            cleanup_token=token,
            validate=lambda: decision,
        )
        self.assertTrue(receipt.stage_removed)
        self.assertFalse(receipt.live_orphan_rehomed)
        self.assertFalse(stage.exists())

    def test_abandoned_staging_rename_failure_restores_live_immutable_mode(self) -> None:
        payload = self.source / "abandoned-rename-failure.txt"
        payload.write_text("rename failure", encoding="utf-8")
        captured = self.capture("abandoned-rename-failure").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "9" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        token = "cleanup-" + "9" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=True,
        )
        live = self.store._object_directory(captured.blob_ref)
        with mock.patch.object(content_module.os, "rename", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                LocalAbandonedStagingBackend(self.store).cleanup(
                    staging_id=staging_id,
                    cleanup_token=token,
                    validate=lambda: decision,
                )
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o500)
        self.store.verify_blob(captured.blob_ref)
        self.assertTrue((self.store.staging / staging_id).is_dir())

    def test_abandoned_staging_corrupt_marker_and_wrong_root_fail_closed(self) -> None:
        payload = self.source / "fail-closed.txt"
        payload.write_text("fail closed", encoding="utf-8")
        captured = self.capture("fail-closed").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "e" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        marker = self.store.staging / staging_id / "publication.json"
        marker.chmod(0o600)
        marker.write_bytes(b"{}")
        marker.chmod(0o400)
        token = "cleanup-" + "e" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=True,
        )
        with self.assertRaises(RealmIntegrityError):
            LocalAbandonedStagingBackend(self.store).cleanup(
                staging_id=staging_id,
                cleanup_token=token,
                validate=lambda: decision,
            )
        self.assertTrue(self.store.has_object(captured.blob_ref))
        self.assertTrue((self.store.staging / staging_id).is_dir())

        other = LocalContentStore(self.root / "other-same-id", store_id=self.store.store_id)
        try:
            other_stage = other.staging / staging_id
            other_stage.mkdir(mode=0o700)
            with self.assertRaisesRegex(RealmIntegrityError, "bound elsewhere"):
                LocalAbandonedStagingBackend(other).cleanup(
                    staging_id=staging_id,
                    cleanup_token=token,
                    validate=lambda: decision,
                )
            self.assertTrue(other_stage.is_dir())
        finally:
            other.close()

    def test_abandoned_cleanup_serializes_concurrent_republication(self) -> None:
        payload = self.source / "concurrent-republish.txt"
        payload.write_text("same bytes", encoding="utf-8")
        captured = self.capture("concurrent-old").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        staging_id = "stage-" + "f" * 32
        publication = self._prepare_abandoned_blob_stage(captured, staging_id)
        token = "cleanup-" + "f" * 32
        decision = self._abandoned_cleanup_decision(
            staging_id=staging_id,
            cleanup_token=token,
            publication=publication,
            remove_live_orphan=True,
        )
        prepared = threading.Event()
        result = []
        errors = []
        original_prepare = self.retention.prepare_publication

        def signal_prepare(**kwargs):
            original_prepare(**kwargs)
            prepared.set()

        def republish() -> None:
            try:
                result.append(
                    self.capture("concurrent-new").seal_blob(
                        source=AllowedFileSource(self.source, payload.name),
                    )
                )
            except BaseException as error:  # surfaced in the main test thread
                errors.append(error)

        thread = threading.Thread(target=republish)

        def validate():
            thread.start()
            self.assertTrue(prepared.wait(timeout=2))
            return decision

        with mock.patch.object(
            self.retention,
            "prepare_publication",
            side_effect=signal_prepare,
        ):
            cleanup = LocalAbandonedStagingBackend(self.store).cleanup(
                staging_id=staging_id,
                cleanup_token=token,
                validate=validate,
            )
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(cleanup.live_orphan_rehomed)
        self.assertEqual(result[0].blob_ref, captured.blob_ref)
        self.assertTrue(self.store.has_object(captured.blob_ref))
        self.store.verify_blob(captured.blob_ref)

    def test_real_ledger_abandoned_prepared_publication_cleanup_end_to_end(self) -> None:
        ledger = RealmLedger(self.root / "abandoned-cleanup.sqlite3")
        ledger.register_principal(
            operation_id="cleanup-principal",
            principal_id="operator",
            kind="human",
        )
        store = LocalContentStore(
            self.root / "abandoned-cleanup-store",
            store_id="abandoned-cleanup-store",
        )
        try:
            ledger.register_store(
                operation_id="cleanup-store",
                store_id=store.store_id,
                backend_kind=store.BACKEND_KIND,
                root_marker=store.root_marker,
            )
            ledger.create_owner(
                operation_id="cleanup-owner",
                owner_id="cleanup-owner",
                owner_kind="workspace",
                principal_id="operator",
            )
            change = ledger.begin_owner_change(
                operation_id="cleanup-begin",
                actor_principal_id="operator",
                owner_id="cleanup-owner",
                expected_owner_revision=0,
                ttl_seconds=60,
            )
            authority = ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change.change_id,
                store_id=store.store_id,
            )
            payload = self.source / "ledger-orphan.txt"
            payload.write_text("ledger orphan", encoding="utf-8")
            blob_ref = BlobRef.from_bytes(payload.read_bytes())
            with mock.patch.object(
                ledger,
                "_record_publication",
                side_effect=RuntimeError("lost before ledger record"),
            ):
                with self.assertRaisesRegex(RuntimeError, "before ledger record"):
                    store.capture(
                        change_id=change.change_id,
                        authority=authority,
                    ).seal_blob(source=AllowedFileSource(self.source, payload.name))
            self.assertTrue(store.has_object(blob_ref))
            self.assertEqual(len(tuple(store.staging.iterdir())), 1)

            ledger.abort_owner_change(
                operation_id="cleanup-abort",
                actor_principal_id="operator",
                change_id=change.change_id,
            )
            abandoned = ledger.list_abandoned_staging_cleanups(
                store_id=store.store_id,
            )
            self.assertEqual(len(abandoned), 1)
            debt_report = fsck_local_store(
                store,
                inventory_factory=lambda: ledger.content_inventory_snapshot(
                    store_id=store.store_id
                ),
            )
            debt_codes = {issue.code for issue in debt_report.issues}
            self.assertTrue(debt_report.ok, debt_report.issues)
            self.assertIn("staging_cleanup_debt", debt_codes)
            self.assertNotIn("stale_staging", debt_codes)
            self.assertNotIn("physical_orphan", debt_codes)

            stage_path = store.staging / abandoned[0].staging_id
            marker_path = stage_path / "publication.json"
            marker_bytes = marker_path.read_bytes()
            marker_path.chmod(0o600)
            marker_path.write_bytes(b"{}")
            marker_path.chmod(0o400)
            corrupt_report = fsck_local_store(
                store,
                inventory_factory=lambda: ledger.content_inventory_snapshot(
                    store_id=store.store_id
                ),
            )
            corrupt_codes = {issue.code for issue in corrupt_report.issues}
            self.assertFalse(corrupt_report.ok)
            self.assertIn("invalid_cleanup_stage_authority", corrupt_codes)
            self.assertIn("physical_orphan", corrupt_codes)

            marker_path.chmod(0o600)
            marker_path.write_bytes(marker_bytes)
            marker_path.chmod(0o400)
            shutil.rmtree(stage_path)
            missing_report = fsck_local_store(
                store,
                inventory_factory=lambda: ledger.content_inventory_snapshot(
                    store_id=store.store_id
                ),
            )
            missing_codes = {issue.code for issue in missing_report.issues}
            self.assertFalse(missing_report.ok)
            self.assertIn("cleanup_stage_missing_for_live_orphan", missing_codes)
            self.assertIn("physical_orphan", missing_codes)

            stage_path.mkdir(mode=0o700)
            marker_path.write_bytes(marker_bytes)
            marker_path.chmod(0o400)
            claim = ledger.claim_abandoned_staging_cleanup(
                operation_id="cleanup-claim",
                store_id=store.store_id,
                staging_id=abandoned[0].staging_id,
            )
            assert claim.cleanup_token is not None
            local_receipt = LocalAbandonedStagingBackend(store).cleanup(
                staging_id=claim.staging_id,
                cleanup_token=claim.cleanup_token,
                validate=lambda: ledger.validate_abandoned_staging_cleanup(
                    store_id=store.store_id,
                    staging_id=claim.staging_id,
                    cleanup_token=claim.cleanup_token,
                ),
            )
            self.assertTrue(local_receipt.live_orphan_rehomed)
            self.assertFalse(store.has_object(blob_ref))
            self.assertEqual(tuple(store.staging.iterdir()), ())
            cleaning_report = fsck_local_store(
                store,
                inventory_factory=lambda: ledger.content_inventory_snapshot(
                    store_id=store.store_id
                ),
            )
            self.assertTrue(cleaning_report.ok, cleaning_report.issues)
            self.assertIn(
                "staging_cleanup_debt",
                {issue.code for issue in cleaning_report.issues},
            )

            completed = ledger.complete_abandoned_staging_cleanup(
                operation_id="cleanup-complete",
                store_id=store.store_id,
                staging_id=claim.staging_id,
                cleanup_token=claim.cleanup_token,
            )
            replayed = ledger.complete_abandoned_staging_cleanup(
                operation_id="cleanup-complete",
                store_id=store.store_id,
                staging_id=claim.staging_id,
                cleanup_token=claim.cleanup_token,
            )
            self.assertEqual(replayed, completed)
            self.assertEqual(completed.state, "cleaned")
            clean_report = fsck_local_store(
                store,
                inventory_factory=lambda: ledger.content_inventory_snapshot(
                    store_id=store.store_id
                ),
            )
            self.assertTrue(clean_report.ok, clean_report.issues)
            self.assertNotIn(
                "staging_cleanup_debt",
                {issue.code for issue in clean_report.issues},
            )
            self.assertEqual(
                ledger.list_abandoned_staging_cleanups(
                    store_id=store.store_id,
                    states=("cleaned",),
                ),
                (completed,),
            )

            published_change = ledger.begin_owner_change(
                operation_id="published-cleanup-begin",
                actor_principal_id="operator",
                owner_id="cleanup-owner",
                expected_owner_revision=0,
                ttl_seconds=60,
            )
            published_authority = ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=published_change.change_id,
                store_id=store.store_id,
            )
            published_payload = self.source / "published-cleanup.txt"
            published_payload.write_text("published cleanup", encoding="utf-8")
            published_ref = BlobRef.from_bytes(published_payload.read_bytes())
            with mock.patch.object(
                ledger,
                "_complete_staging_publication",
                side_effect=RuntimeError("lost finalize response"),
            ):
                with self.assertRaisesRegex(RuntimeError, "finalize response"):
                    store.capture(
                        change_id=published_change.change_id,
                        authority=published_authority,
                    ).seal_blob(
                        source=AllowedFileSource(self.source, published_payload.name)
                    )
            self.assertTrue(store.has_object(published_ref))
            self.assertEqual(tuple(store.staging.iterdir()), ())
            ledger.abort_owner_change(
                operation_id="published-cleanup-abort",
                actor_principal_id="operator",
                change_id=published_change.change_id,
            )
            published_work = ledger.list_abandoned_staging_cleanups(
                store_id=store.store_id,
            )
            self.assertEqual(len(published_work), 1)
            published_claim = ledger.claim_abandoned_staging_cleanup(
                operation_id="published-cleanup-claim",
                store_id=store.store_id,
                staging_id=published_work[0].staging_id,
            )
            assert published_claim.cleanup_token is not None
            published_local = LocalAbandonedStagingBackend(store).cleanup(
                staging_id=published_claim.staging_id,
                cleanup_token=published_claim.cleanup_token,
                validate=lambda: ledger.validate_abandoned_staging_cleanup(
                    store_id=store.store_id,
                    staging_id=published_claim.staging_id,
                    cleanup_token=published_claim.cleanup_token,
                ),
            )
            self.assertFalse(published_local.stage_removed)
            self.assertFalse(published_local.live_orphan_rehomed)
            self.assertTrue(store.has_object(published_ref))
            ledger.complete_abandoned_staging_cleanup(
                operation_id="published-cleanup-complete",
                store_id=store.store_id,
                staging_id=published_claim.staging_id,
                cleanup_token=published_claim.cleanup_token,
            )
        finally:
            store.close()

    def test_tombstone_rejects_unsafe_mode_and_restores_mode_after_rename_failure(self) -> None:
        payload = self.source / "rename-failure.txt"
        payload.write_text("rename failure", encoding="utf-8")
        captured = self.capture("rename-failure").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        live = self.store._object_directory(captured.blob_ref)
        live.chmod(0o755)
        backend = LocalGcBackend(self.store)
        with self.assertRaisesRegex(ContentCorrupt, "unsafe recovery mode"):
            backend.tombstone(
                captured.blob_ref,
                deletion_token=new_deletion_token(),
                still_eligible=lambda: True,
            )
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o755)

        live.chmod(0o500)
        with mock.patch.object(content_module.os, "rename", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                backend.tombstone(
                    captured.blob_ref,
                    deletion_token=new_deletion_token(),
                    still_eligible=lambda: True,
                )
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o500)
        self.store.verify_blob(captured.blob_ref)

    def test_fsck_inventory_factory_runs_inside_store_lock(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("fsck-lock").seal_blob(
            source=AllowedFileSource(self.source, payload.name),
        )
        backend = LocalGcBackend(self.store)
        token = new_deletion_token()
        callback_entered = threading.Event()
        thread_holder = []

        def inventory_factory():
            thread = threading.Thread(
                target=lambda: backend.tombstone(
                    receipt.blob_ref,
                    deletion_token=token,
                    still_eligible=lambda: callback_entered.set() or True,
                )
            )
            thread_holder.append(thread)
            thread.start()
            self.assertFalse(callback_entered.wait(timeout=0.1))
            return self.retention

        report = fsck_local_store(self.store, inventory_factory=inventory_factory)
        self.assertTrue(report.ok, report.issues)
        thread_holder[0].join(timeout=2)
        self.assertFalse(thread_holder[0].is_alive())
        self.assertTrue(callback_entered.is_set())

    def test_republish_reconciles_cancelled_trash_before_future_gc(self) -> None:
        ledger = RealmLedger(self.root / "gc-republish.sqlite3")
        ledger.register_principal(
            operation_id="gc-principal",
            principal_id="operator",
            kind="human",
        )
        store = LocalContentStore(
            self.root / "gc-republish-store",
            store_id="gc-republish",
        )
        ledger.register_store(
            operation_id="gc-store",
            store_id=store.store_id,
            backend_kind="local-cas",
            root_marker=store.root_marker,
        )
        ledger.create_owner(
            operation_id="gc-owner",
            owner_id="gc-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        payload = self.source / "gc-republish.txt"
        payload.write_text("same immutable bytes", encoding="utf-8")

        first_change = ledger.begin_owner_change(
            operation_id="gc-first-begin",
            actor_principal_id="operator",
            owner_id="gc-owner",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        first_authority = ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=first_change.change_id,
            store_id=store.store_id,
        )
        first = store.capture(
            change_id=first_change.change_id,
            authority=first_authority,
        ).seal_blob(source=AllowedFileSource(self.source, payload.name))
        ledger.abort_owner_change(
            operation_id="gc-first-abort",
            actor_principal_id="operator",
            change_id=first_change.change_id,
        )

        epoch = ledger.start_gc_epoch(operation_id="gc-first-epoch", store_id=store.store_id)
        ledger.finish_gc_epoch(
            operation_id="gc-first-finish",
            store_id=store.store_id,
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        claim = ledger.claim_tombstone(
            operation_id="gc-first-claim",
            store_id=store.store_id,
            content_ref=first.blob_ref,
        )
        token = claim.deletion_token
        assert token is not None

        claimed_report = fsck_local_store(
            store,
            inventory_factory=lambda: ledger.content_inventory_snapshot(
                store_id=store.store_id
            ),
        )
        self.assertTrue(claimed_report.ok, claimed_report.issues)
        self.assertIn(
            "expected_tombstone_missing",
            {issue.code for issue in claimed_report.issues},
        )

        def deleting_is_current() -> bool:
            ledger.validate_tombstone_claim(
                store_id=store.store_id,
                content_ref=first.blob_ref,
                deletion_token=token,
            )
            return True

        backend = LocalGcBackend(store)
        backend.tombstone(
            first.blob_ref,
            deletion_token=token,
            still_eligible=deleting_is_current,
        )
        moved_report = fsck_local_store(
            store,
            inventory_factory=lambda: ledger.content_inventory_snapshot(
                store_id=store.store_id
            ),
        )
        self.assertTrue(moved_report.ok, moved_report.issues)
        self.assertNotIn(
            "expected_tombstone_missing",
            {issue.code for issue in moved_report.issues},
        )

        second_change = ledger.begin_owner_change(
            operation_id="gc-second-begin",
            actor_principal_id="operator",
            owner_id="gc-owner",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        second_authority = ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=second_change.change_id,
            store_id=store.store_id,
        )
        second = store.capture(
            change_id=second_change.change_id,
            authority=second_authority,
        ).seal_blob(source=AllowedFileSource(self.source, payload.name))
        self.assertEqual(second.blob_ref, first.blob_ref)
        ledger.abort_owner_change(
            operation_id="gc-second-abort",
            actor_principal_id="operator",
            change_id=second_change.change_id,
        )

        blocked_epoch = ledger.start_gc_epoch(
            operation_id="gc-blocked-epoch", store_id=store.store_id
        )
        self.assertEqual(
            ledger.finish_gc_epoch(
                operation_id="gc-blocked-finish",
                store_id=store.store_id,
                epoch=blocked_epoch.epoch,
                grace_seconds=0,
            ),
            (),
        )

        def cancelled_is_current() -> bool:
            ledger.validate_cancelled_tombstone(
                store_id=store.store_id,
                content_ref=first.blob_ref,
                deletion_token=token,
            )
            return True

        cleanup = backend.discard_cancelled_tombstone(
            first.blob_ref,
            deletion_token=token,
            still_cancelled=cancelled_is_current,
        )
        self.assertTrue(cleanup.moved)
        completed = ledger.complete_cancelled_tombstone_cleanup(
            operation_id="gc-cancelled-complete",
            store_id=store.store_id,
            content_ref=first.blob_ref,
            deletion_token=token,
        )
        self.assertIsNone(completed.deletion_token)

        final_epoch = ledger.start_gc_epoch(
            operation_id="gc-final-epoch", store_id=store.store_id
        )
        final = ledger.finish_gc_epoch(
            operation_id="gc-final-finish",
            store_id=store.store_id,
            epoch=final_epoch.epoch,
            grace_seconds=0,
        )
        self.assertEqual({item.content_ref for item in final}, {first.blob_ref})

    def test_gc_rejects_symlinked_live_or_tombstone_paths(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("gc-symlink").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        live = self.store._object_directory(receipt.blob_ref)
        saved = live.with_name(live.name + "-saved")
        os.chmod(live, 0o700)
        os.rename(live, saved)
        os.symlink(saved, live)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "symlink"):
                LocalGcBackend(self.store).tombstone(
                    receipt.blob_ref,
                    deletion_token=new_deletion_token(),
                    still_eligible=lambda: True,
                )
        finally:
            live.unlink()
            os.rename(saved, live)
            os.chmod(live, 0o500)

    @unittest.skipIf(os.name == "nt", "descriptor-rooted namespace test requires POSIX")
    def test_staging_parent_symlink_swap_fails_without_touching_external_tree(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        saved = self.root / "saved-staging"
        external = self.root / "external-staging"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("untouched", encoding="utf-8")
        os.rename(self.store.staging, saved)
        os.symlink(external, self.store.staging, target_is_directory=True)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "namespace"):
                self.capture("staging-parent-swap").seal_blob(
                    source=AllowedFileSource(self.source, "payload.txt"),
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
            self.assertEqual({path.name for path in external.iterdir()}, {"sentinel"})
        finally:
            self.store.staging.unlink()
            os.rename(saved, self.store.staging)

    @unittest.skipIf(os.name == "nt", "descriptor-rooted namespace test requires POSIX")
    def test_trash_parent_symlink_swap_cannot_move_or_delete_external_content(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("trash-parent-swap").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        saved = self.root / "saved-trash"
        external = self.root / "external-trash"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("untouched", encoding="utf-8")
        os.rename(self.store.trash, saved)
        os.symlink(external, self.store.trash, target_is_directory=True)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "namespace"):
                LocalGcBackend(self.store).tombstone(
                    receipt.blob_ref,
                    deletion_token=new_deletion_token(),
                    still_eligible=lambda: True,
                )
            self.assertTrue(self.store.has_object(receipt.blob_ref))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        finally:
            self.store.trash.unlink()
            os.rename(saved, self.store.trash)

    def test_existing_object_adoption_rejects_writable_files_and_hardlinks(self) -> None:
        payload = self.source / "payload.txt"
        payload.write_text("immutable payload", encoding="utf-8")
        receipt = self.capture("adoption-base").seal_blob(
            source=AllowedFileSource(self.source, "payload.txt"),
        )
        object_directory = self.store._object_directory(receipt.blob_ref)
        data = object_directory / "data"

        os.chmod(data, 0o600)
        with self.assertRaisesRegex(ContentCorrupt, "immutable single-link"):
            self.capture("adoption-writable").seal_blob(
                source=AllowedFileSource(self.source, "payload.txt"),
            )

        os.chmod(object_directory, 0o700)
        data.unlink()
        os.link(payload, data)
        os.chmod(data, 0o400)
        os.chmod(object_directory, 0o500)
        self.assertEqual(data.stat().st_nlink, 2)
        with self.assertRaisesRegex(ContentCorrupt, "immutable single-link"):
            self.capture("adoption-hardlink").seal_blob(
                source=AllowedFileSource(self.source, "payload.txt"),
            )

    @unittest.skipIf(os.name == "nt", "descriptor-rooted cleanup test requires POSIX")
    def test_private_cleanup_rejects_hardlink_before_chmod_or_unlink(self) -> None:
        outside = self.root / "outside-hardlink"
        outside.write_text("outside", encoding="utf-8")
        outside.chmod(0o400)
        staging_id = "stage-" + "a" * 32
        stage = self.store.staging / staging_id
        stage.mkdir(mode=0o700)
        os.link(outside, stage / "linked")
        staging_fd = self.store._open_namespace_fd("staging")
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "identity changed"):
                content_module._remove_private_tree_at(staging_fd, staging_id)
        finally:
            os.close(staging_fd)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o400)
        self.assertEqual(outside.stat().st_nlink, 2)
        (stage / "linked").unlink()
        stage.rmdir()
        outside.chmod(0o600)

    def test_store_root_identity_cannot_drift_after_open(self) -> None:
        original = self.store.root
        saved = self.root / "saved-store-root"
        os.rename(original, saved)
        original.mkdir(mode=0o700)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "identity changed"):
                self.store.has_object(BlobRef.from_bytes(b"missing"))
        finally:
            shutil.rmtree(original)
            os.rename(saved, original)

    @unittest.skipIf(os.name == "nt", "lock identity test requires POSIX rename semantics")
    def test_store_lock_replacement_is_rejected(self) -> None:
        lock_path = self.store.root / ".store.lock"
        saved = self.store.root / ".store.lock.saved"
        with self.store.exclusive_lock():
            os.rename(lock_path, saved)
            lock_path.write_bytes(b"replacement")
            lock_path.chmod(0o600)
            try:
                with self.assertRaisesRegex(RealmIntegrityError, "lock identity changed"):
                    with self.store.exclusive_lock():
                        self.fail("replacement lock must never be acquired")
            finally:
                lock_path.unlink()
                os.rename(saved, lock_path)

    @unittest.skipIf(os.name == "nt", "namespace identity test requires POSIX rename semantics")
    def test_objects_namespace_replacement_is_rejected(self) -> None:
        payload = self.source / "objects-identity.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("objects-identity").seal_blob(
            source=AllowedFileSource(self.source, payload.name)
        )
        saved = self.root / "saved-objects"
        os.rename(self.store.objects, saved)
        self.store.objects.mkdir(mode=0o700)
        (self.store.objects / "blobs").mkdir(mode=0o700)
        (self.store.objects / "trees").mkdir(mode=0o700)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "namespace identity changed"):
                self.store.has_object(receipt.blob_ref)
        finally:
            shutil.rmtree(self.store.objects)
            os.rename(saved, self.store.objects)

    @unittest.skipIf(os.name == "nt", "namespace identity test requires POSIX rename semantics")
    def test_blob_namespace_replacement_is_rejected(self) -> None:
        payload = self.source / "blob-identity.txt"
        payload.write_text("payload", encoding="utf-8")
        receipt = self.capture("blob-identity").seal_blob(
            source=AllowedFileSource(self.source, payload.name)
        )
        saved = self.root / "saved-blobs"
        os.rename(self.store.blobs, saved)
        self.store.blobs.mkdir(mode=0o700)
        try:
            report = fsck_local_store(
                self.store,
                inventory_factory=lambda: self.retention,
            )
            self.assertIn(
                "unsafe_object_namespace",
                {issue.code for issue in report.issues},
            )
            with self.assertRaisesRegex(RealmIntegrityError, "object-kind namespace identity"):
                self.store.has_object(receipt.blob_ref)
        finally:
            self.store.blobs.rmdir()
            os.rename(saved, self.store.blobs)


if __name__ == "__main__":
    unittest.main()
