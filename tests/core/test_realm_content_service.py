from __future__ import annotations

import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import (
    AllowedFileSource,
    AllowedTreeSource,
    CompletedTreeCapture,
    LocalContentStore,
)
from optpilot.realm.errors import ContentCorrupt, RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import BlobRef, request_digest
from optpilot.realm.service import RealmContentService, TreeCompositionSource
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


class RealmContentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="owner",
            owner_id="workspace-a",
            owner_kind="workspace",
            principal_id="operator",
        )
        self.service = RealmContentService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def begin(self, label: str):
        return self.ledger.begin_owner_change(
            operation_id=f"{label}/begin",
            actor_principal_id="operator",
            owner_id="workspace-a",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )

    def abort(self, label: str, change_id: str) -> None:
        self.ledger.abort_owner_change(
            operation_id=f"{label}/abort",
            actor_principal_id="operator",
            change_id=change_id,
        )

    def retain_source_tree(self):
        source = self.root / "composition-source"
        source.mkdir()
        (source / "payload.txt").write_text("source payload", encoding="utf-8")
        self.ledger.create_owner(
            operation_id="composition/source-owner/create",
            owner_id="composition-source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        change = self.ledger.begin_owner_change(
            operation_id="composition/source-owner/begin",
            actor_principal_id="operator",
            owner_id="composition-source-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        sealed = self.service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source),
            operation_id="composition/source-owner/seal",
        )
        membership = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            "composition-source-root",
        )
        self.ledger.hold_owner_content(
            operation_id="composition/source-owner/hold",
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        committed = self.ledger.commit_owner_change(
            operation_id="composition/source-owner/commit",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return membership, committed.owner_revision, sealed.manifest

    def content_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.ledger.database_path) as connection:
            return {
                kind: int(count)
                for kind, count in connection.execute(
                    "SELECT kind, COUNT(*) FROM content_objects GROUP BY kind"
                )
            }

    def test_prepared_visible_orphan_is_rehomed_and_completed_end_to_end(self) -> None:
        payload = b"prepared visible orphan"
        (self.source / "payload.bin").write_bytes(payload)
        change = self.begin("prepared")
        capture = self.service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        with mock.patch.object(
            self.ledger,
            "_record_publication",
            side_effect=RuntimeError("record response lost before commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "record response"):
                capture.seal_blob(
                    source=AllowedFileSource(self.source, "payload.bin"),
                )

        staging_id = next(path.name for path in self.store.staging.iterdir())
        publication = self.store.load_prepared_publication(staging_id)
        self.assertTrue(self.store.has_object(publication.content_ref))
        self.abort("prepared", change.change_id)

        receipt = self.service.reconcile_abandoned_staging(
            operation_id="prepared/reconcile",
            store_id=self.store.store_id,
            staging_id=staging_id,
        )
        self.assertEqual(receipt.cleanup.state, "cleaned")
        self.assertFalse(receipt.already_complete)
        self.assertTrue(receipt.physical.live_orphan_rehomed)  # type: ignore[union-attr]
        self.assertFalse(self.store.has_object(publication.content_ref))
        self.assertFalse((self.store.staging / staging_id).exists())

        replay = self.service.reconcile_abandoned_staging(
            operation_id="prepared/reconcile",
            store_id=self.store.store_id,
            staging_id=staging_id,
        )
        self.assertTrue(replay.already_complete)
        self.assertEqual(replay.cleanup.cleanup_token, receipt.cleanup.cleanup_token)

    def test_keyed_tree_capture_recovers_exact_receipt_without_rescanning(self) -> None:
        (self.source / "a.txt").write_text("first", encoding="utf-8")
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "b.txt").write_text("second", encoding="utf-8")
        change = self.begin("keyed-tree")
        capture = self.service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )

        with mock.patch.object(
            self.store, "_seal_tree", wraps=self.store._seal_tree
        ) as physical_seal:
            first = capture.seal_tree(
                source=AllowedTreeSource(self.source),
                operation_id="keyed-tree/seal",
            )
            (self.source / "a.txt").write_text("changed live bytes", encoding="utf-8")
            with mock.patch.object(
                self.store, "verify_tree", wraps=self.store.verify_tree
            ) as verify_tree:
                replay = capture.seal_tree(
                    source=AllowedTreeSource(self.source),
                    operation_id="keyed-tree/seal",
                )

        self.assertEqual(physical_seal.call_count, 1)
        self.assertEqual(replay, first)
        verify_tree.assert_called_once_with(first.snapshot_ref, verify_children=True)

    def test_manifest_only_composition_is_fenced_and_replays_without_sources(self) -> None:
        membership, source_revision, source_manifest = self.retain_source_tree()
        source_file = next(
            entry for entry in source_manifest.entries if entry.kind == "file"
        )
        assert source_file.blob_ref is not None
        composed_manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "renamed.txt",
                    blob_ref=source_file.blob_ref,
                    size=source_file.size,
                    executable=source_file.executable,
                ),
            )
        )
        composed_membership = OwnerMembership(
            self.store.store_id,
            composed_manifest.snapshot_ref,
            "composition-target-root",
        )
        target_change = self.begin("composition-target")
        source = TreeCompositionSource(
            owner_id="composition-source-owner",
            owner_revision=source_revision,
            membership=membership,
        )
        before = self.content_counts()

        with mock.patch.object(
            self.store,
            "_publish_blob_from_fd",
            side_effect=AssertionError("composition must not publish blob bytes"),
        ), mock.patch.object(
            self.store,
            "_publish_tree_manifest",
            wraps=self.store._publish_tree_manifest,
        ) as publish_tree:
            first = self.service.compose_tree(
                operation_id="composition/manifest-only",
                actor_principal_id="operator",
                change_id=target_change.change_id,
                store_id=self.store.store_id,
                sources=(source,),
                manifest=composed_manifest,
                hold_membership=composed_membership,
            )
            self.ledger.register_principal(
                operation_id="composition/reader/register",
                principal_id="reader",
                kind="human",
            )
            self.ledger.grant_owner_permission(
                operation_id="composition/source-owner/grant",
                actor_principal_id="operator",
                owner_id="composition-source-owner",
                principal_id="reader",
                permission=OwnerPermission.METADATA_READ,
            )
            replay = self.service.compose_tree(
                operation_id="composition/manifest-only",
                actor_principal_id="operator",
                change_id=target_change.change_id,
                store_id=self.store.store_id,
                sources=(source,),
                manifest=composed_manifest,
                hold_membership=composed_membership,
            )

        self.assertEqual(first, replay)
        self.assertEqual(first.manifest, composed_manifest)
        self.assertEqual(len(first.publications), 1)
        self.assertEqual(publish_tree.call_count, 1)
        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT store_id, content_ref, role "
                    "FROM owner_transaction_additions WHERE change_id = ?",
                    (target_change.change_id,),
                ).fetchall(),
                [
                    (
                        composed_membership.store_id,
                        str(composed_membership.content_ref),
                        composed_membership.role,
                    )
                ],
            )
        after = self.content_counts()
        self.assertEqual(after.get("blob", 0), before.get("blob", 0))
        self.assertEqual(after.get("tree", 0), before.get("tree", 0) + 1)

        changed = TreeManifest.build(
            (
                TreeEntry.file(
                    "changed.txt",
                    blob_ref=source_file.blob_ref,
                    size=source_file.size,
                    executable=source_file.executable,
                ),
            )
        )
        with self.assertRaisesRegex(RealmConflict, "different request"):
            self.service.compose_tree(
                operation_id="composition/manifest-only",
                actor_principal_id="operator",
                change_id=target_change.change_id,
                store_id=self.store.store_id,
                sources=(source,),
                manifest=changed,
            )

    def test_composition_can_use_a_blob_captured_by_the_exact_target_change(
        self,
    ) -> None:
        membership, source_revision, source_manifest = self.retain_source_tree()
        target_change = self.begin("composition-with-change-blob")
        (self.source / "study.yaml").write_text(
            "config: study\n", encoding="utf-8"
        )
        blob = self.service.capture(
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
        ).seal_blob(
            source=AllowedFileSource(self.source, "study.yaml"),
        )
        manifest = TreeManifest.build(
            (
                *source_manifest.entries,
                TreeEntry.file(
                    "study.yaml",
                    blob_ref=blob.blob_ref,
                    size=blob.publication.logical_bytes,
                    executable=False,
                ),
            )
        )

        sealed = self.service.compose_tree(
            operation_id="composition/with-change-blob",
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
            sources=(
                TreeCompositionSource(
                    owner_id="composition-source-owner",
                    owner_revision=source_revision,
                    membership=membership,
                ),
            ),
            manifest=manifest,
            change_publications=(blob.publication,),
        )

        self.assertEqual(sealed.manifest, manifest)
        self.assertEqual(len(sealed.publications), 1)

    def test_composition_rejects_a_change_publication_from_another_owner_change(
        self,
    ) -> None:
        membership, source_revision, _source_manifest = self.retain_source_tree()
        self.ledger.create_owner(
            operation_id="composition/foreign-owner/create",
            owner_id="composition-foreign-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        foreign_change = self.ledger.begin_owner_change(
            operation_id="composition/foreign-owner/begin",
            actor_principal_id="operator",
            owner_id="composition-foreign-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        (self.source / "foreign.txt").write_text("foreign", encoding="utf-8")
        foreign_blob = self.service.capture(
            actor_principal_id="operator",
            change_id=foreign_change.change_id,
            store_id=self.store.store_id,
        ).seal_blob(
            source=AllowedFileSource(self.source, "foreign.txt"),
        )
        target_change = self.begin("composition-foreign-change-blob")
        manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "foreign.txt",
                    blob_ref=foreign_blob.blob_ref,
                    size=foreign_blob.publication.logical_bytes,
                    executable=False,
                ),
            )
        )

        with self.assertRaisesRegex(RealmConflict, "exact owner change"):
            self.service.compose_tree(
                operation_id="composition/foreign-change-blob",
                actor_principal_id="operator",
                change_id=target_change.change_id,
                store_id=self.store.store_id,
                sources=(
                    TreeCompositionSource(
                        owner_id="composition-source-owner",
                        owner_revision=source_revision,
                        membership=membership,
                    ),
                ),
                manifest=manifest,
                change_publications=(foreign_blob.publication,),
            )

    def test_ordinary_capture_cannot_publish_an_authority_free_manifest(self) -> None:
        membership, _source_revision, source_manifest = self.retain_source_tree()
        target_change = self.begin("composition-bypass")
        capture = self.service.capture(
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
        )
        with self.assertRaisesRegex(RealmConflict, "composition authority"):
            capture.publish_composed_tree_manifest(
                manifest=source_manifest,
                composition_request_digest=request_digest(
                    {"attempt": "authority-free"}
                ),
                operation_id="composition/authority-free",
            )
        self.assertTrue(self.store.has_object(membership.content_ref))

    def test_generic_lease_api_cannot_mint_composition_source_authority(self) -> None:
        membership, _source_revision, _source_manifest = self.retain_source_tree()

        with self.assertRaisesRegex(RealmConflict, "typed transaction"):
            self.ledger.acquire_lease(
                operation_id="composition/generic-lease-bypass",
                actor_principal_id="operator",
                owner_id="composition-source-owner",
                lease_kind="tree-composition-source",
                audience="realm-content-service",
                holder_id="forged-holder",
                scope_key="tree-composition:forged:source:0",
                ttl_seconds=TEST_LEASE_TTL_SECONDS,
                metadata={
                    "composition_request_digest": request_digest(
                        {"forged": "composition"}
                    ),
                    "source_index": 0,
                    "source_owner_revision": 1,
                },
                content_roots=(membership,),
            )

    def test_unbound_composition_digest_cannot_mint_capture_authority(self) -> None:
        membership, _source_revision, _source_manifest = self.retain_source_tree()
        target_change = self.begin("composition-unbound")
        ordinary_lease = self.ledger.acquire_lease(
            operation_id="composition/unbound/ordinary-lease",
            actor_principal_id="operator",
            owner_id="composition-source-owner",
            lease_kind="inspection",
            audience="test",
            holder_id="test-holder",
            scope_key="composition-unbound",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
            content_roots=(membership,),
        )

        with self.assertRaises(RealmNotFound):
            self.ledger.content_composition_capture_handle(
                actor_principal_id="operator",
                change_id=target_change.change_id,
                store_id=self.store.store_id,
                composition_request_digest=request_digest(
                    {"unbound": "composition request"}
                ),
                source_leases=(ordinary_lease,),
            )

    def test_typed_composition_lease_rejects_wrong_actor_and_source_index(self) -> None:
        membership, source_revision, source_manifest = self.retain_source_tree()
        source_file = next(
            entry for entry in source_manifest.entries if entry.kind == "file"
        )
        assert source_file.blob_ref is not None
        manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "renamed.txt",
                    blob_ref=source_file.blob_ref,
                    size=source_file.size,
                    executable=source_file.executable,
                ),
            )
        )
        target_change = self.begin("composition-typed-lease")
        composition_request = {
            "change_id": target_change.change_id,
            "manifest_ref": str(manifest.snapshot_ref),
            "schema": "optpilot.tree-composition-request.v1",
            "sources": [
                {
                    "membership": membership.to_dict(),
                    "owner_id": "composition-source-owner",
                    "owner_revision": source_revision,
                }
            ],
            "store_id": self.store.store_id,
        }
        digest = self.ledger.bind_content_composition_request(
            operation_id="composition/typed-lease/bind",
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
            composition_request=composition_request,
        )

        with self.assertRaises(RealmNotFound):
            self.ledger.acquire_content_composition_source_lease(
                operation_id="composition/typed-lease/wrong-actor",
                actor_principal_id="intruder",
                composition_request_digest=digest,
                source_index=0,
                holder_id="test-holder",
                ttl_seconds=TEST_LEASE_TTL_SECONDS,
            )
        with self.assertRaisesRegex(ValueError, "out of range"):
            self.ledger.acquire_content_composition_source_lease(
                operation_id="composition/typed-lease/wrong-index",
                actor_principal_id="operator",
                composition_request_digest=digest,
                source_index=1,
                holder_id="test-holder",
                ttl_seconds=TEST_LEASE_TTL_SECONDS,
            )

    def test_composed_manifest_with_false_child_size_is_never_published(self) -> None:
        membership, source_revision, source_manifest = self.retain_source_tree()
        source_file = next(
            entry for entry in source_manifest.entries if entry.kind == "file"
        )
        assert source_file.blob_ref is not None
        assert source_file.size is not None
        forged_manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "renamed.txt",
                    blob_ref=source_file.blob_ref,
                    size=source_file.size + 1,
                    executable=source_file.executable,
                ),
            )
        )
        target_change = self.begin("composition-false-size")
        composition_request = {
            "change_id": target_change.change_id,
            "manifest_ref": str(forged_manifest.snapshot_ref),
            "schema": "optpilot.tree-composition-request.v1",
            "sources": [
                {
                    "membership": membership.to_dict(),
                    "owner_id": "composition-source-owner",
                    "owner_revision": source_revision,
                }
            ],
            "store_id": self.store.store_id,
        }
        digest = self.ledger.bind_content_composition_request(
            operation_id="composition/false-size/bind",
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
            composition_request=composition_request,
        )
        lease = self.ledger.acquire_content_composition_source_lease(
            operation_id="composition/false-size/source-lease",
            actor_principal_id="operator",
            composition_request_digest=digest,
            source_index=0,
            holder_id="test-holder",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        authority = self.ledger.content_composition_capture_handle(
            actor_principal_id="operator",
            change_id=target_change.change_id,
            store_id=self.store.store_id,
            composition_request_digest=digest,
            source_leases=(lease,),
        )
        capture = self.store.capture(
            change_id=target_change.change_id,
            authority=authority,
        )
        before = self.content_counts()

        with self.assertRaises(ContentCorrupt):
            capture.publish_composed_tree_manifest(
                manifest=forged_manifest,
                composition_request_digest=digest,
                operation_id="composition/false-size/publish",
            )

        self.assertFalse(self.store.has_object(forged_manifest.snapshot_ref))
        self.assertEqual(self.content_counts(), before)

    def test_tree_capture_operation_id_cannot_cross_owner_changes(self) -> None:
        (self.source / "payload.txt").write_text("payload", encoding="utf-8")
        first_change = self.begin("capture-binding")
        first = self.service.capture(
            actor_principal_id="operator",
            change_id=first_change.change_id,
            store_id=self.store.store_id,
        )
        first.seal_tree(
            source=AllowedTreeSource(self.source),
            operation_id="shared-tree-operation",
        )

        self.ledger.create_owner(
            operation_id="capture-binding/other-owner",
            owner_id="workspace-b",
            owner_kind="workspace",
            principal_id="operator",
        )
        second_change = self.ledger.begin_owner_change(
            operation_id="capture-binding/other-begin",
            actor_principal_id="operator",
            owner_id="workspace-b",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        second = self.service.capture(
            actor_principal_id="operator",
            change_id=second_change.change_id,
            store_id=self.store.store_id,
        )
        with mock.patch.object(
            self.store, "_seal_tree", wraps=self.store._seal_tree
        ) as physical_seal:
            with self.assertRaisesRegex(RealmConflict, "another change or store"):
                second.seal_tree(
                    source=AllowedTreeSource(self.source),
                    operation_id="shared-tree-operation",
                )
        physical_seal.assert_not_called()

    def test_recovered_tree_rejects_publication_facts_that_differ_from_bytes(self) -> None:
        (self.source / "payload.txt").write_text("payload", encoding="utf-8")
        change = self.begin("capture-tamper")
        capture = self.service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        receipt = capture.seal_tree(
            source=AllowedTreeSource(self.source),
            operation_id="capture-tamper/seal",
        )
        publications = list(receipt.publications)
        publications[0] = replace(
            publications[0], physical_bytes=publications[0].physical_bytes + 1
        )
        tampered = CompletedTreeCapture(
            snapshot_ref=receipt.snapshot_ref,
            publications=tuple(publications),
        )

        with self.assertRaisesRegex(ContentCorrupt, "differ from its staged bytes"):
            self.store._verified_tree_seal_receipt(tampered)

    def test_lost_finalization_response_cleans_stage_without_deleting_registered_bytes(self) -> None:
        payload = b"registered before finalization failed"
        (self.source / "registered.bin").write_bytes(payload)
        change = self.begin("published")
        capture = self.service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        with mock.patch.object(
            self.ledger,
            "_complete_staging_publication",
            side_effect=RuntimeError("finalization response lost"),
        ):
            with self.assertRaisesRegex(RuntimeError, "finalization response"):
                capture.seal_blob(
                    source=AllowedFileSource(self.source, "registered.bin"),
                )

        expected_ref = BlobRef.from_bytes(payload)
        self.assertTrue(self.store.has_object(expected_ref))
        self.assertEqual(tuple(self.store.staging.iterdir()), ())
        self.abort("published", change.change_id)
        cleanup = self.ledger.list_abandoned_staging_cleanups(
            store_id=self.store.store_id,
            states=("abandoned",),
        )
        self.assertEqual(len(cleanup), 1)

        receipt = self.service.reconcile_abandoned_staging(
            operation_id="published/reconcile",
            store_id=self.store.store_id,
            staging_id=cleanup[0].staging_id,
        )
        self.assertEqual(receipt.cleanup.state, "cleaned")
        self.assertFalse(receipt.physical.live_orphan_rehomed)  # type: ignore[union-attr]
        self.assertTrue(self.store.has_object(expected_ref))

    def test_existing_cleaning_claim_resumes_without_reclaiming(self) -> None:
        change = self.begin("resume")
        authority = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        staging_id = "stage-" + "a" * 32
        authority.reserve_staging(
            change_id=change.change_id,
            staging_id=staging_id,
            store_id=self.store.store_id,
            object_kind="blob",
        )
        (self.store.staging / staging_id).mkdir(mode=0o700)
        self.abort("resume", change.change_id)
        claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id="resume/manual-claim",
            store_id=self.store.store_id,
            staging_id=staging_id,
        )

        receipt = self.service.reconcile_abandoned_staging(
            operation_id="resume/reconcile",
            store_id=self.store.store_id,
            staging_id=staging_id,
        )
        self.assertEqual(receipt.cleanup.state, "cleaned")
        self.assertEqual(receipt.cleanup.cleanup_token, claim.cleanup_token)
        self.assertTrue(receipt.physical.stage_removed)  # type: ignore[union-attr]
        self.assertFalse((self.store.staging / staging_id).exists())

    def test_batch_isolates_poison_cleanup_and_continues_later_work(self) -> None:
        change = self.begin("batch")
        authority = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        poisoned_id = "stage-" + "a" * 32
        healthy_id = "stage-" + "b" * 32
        for staging_id in (poisoned_id, healthy_id):
            authority.reserve_staging(
                change_id=change.change_id,
                staging_id=staging_id,
                store_id=self.store.store_id,
                object_kind="blob",
            )
            (self.store.staging / staging_id).mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("must not be touched", encoding="utf-8")
        (self.store.staging / poisoned_id / "unsafe-link").symlink_to(outside)
        self.abort("batch", change.change_id)

        outcomes = self.service.reconcile_all_abandoned_staging(
            operation_id="batch/reconcile",
            store_id=self.store.store_id,
        )
        self.assertEqual([item.staging_id for item in outcomes], [poisoned_id, healthy_id])
        self.assertFalse(outcomes[0].ok)
        self.assertEqual(outcomes[0].error_type, "RealmIntegrityError")
        self.assertTrue(outcomes[1].ok)
        self.assertFalse((self.store.staging / healthy_id).exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "must not be touched")


if __name__ == "__main__":
    unittest.main()
