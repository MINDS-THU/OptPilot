"""Focused authority and transaction tests for the production RealmLedger."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from optpilot.realm.content import ContentEdge, PublishedObject, publication_digest
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import BlobRef, SnapshotRef


class RealmLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.counter = 0
        for principal in ("alice", "bob"):
            self.ledger.register_principal(
                operation_id=self.op(f"principal-{principal}"),
                principal_id=principal,
                kind="user",
            )
        for store_id in ("store-a", "store-b"):
            self.ledger.register_store(
                operation_id=self.op(f"store-{store_id}"),
                store_id=store_id,
                backend_kind="local-cas",
                root_marker=f"marker-{store_id}",
            )
        self.ledger.create_owner(
            operation_id=self.op("owner-a"),
            owner_id="owner-a",
            owner_kind="workspace",
            principal_id="alice",
        )
        self.ledger.create_owner(
            operation_id=self.op("owner-b"),
            owner_id="owner-b",
            owner_kind="workspace",
            principal_id="bob",
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"test/{self.counter}/{label}"

    def test_root_claims_survive_new_attachment_observations(self) -> None:
        volume_root_id = "volume-root-remount"
        volume_nonce = "a" * 64
        volume_marker = self.ledger.ephemeral_volume_root_marker_digest(
            volume_root_id=volume_root_id,
            backend_kind="local-private-directory-v1",
            claim_nonce=volume_nonce,
        )
        volume_facts = {
            "volume_root_id": volume_root_id,
            "canonical_path": str(self.database.parent / "volumes"),
            "backend_kind": "local-private-directory-v1",
            "marker_digest": volume_marker,
            "claim_nonce": volume_nonce,
        }
        registered_volume = self.ledger.register_ephemeral_volume_root(
            operation_id=self.op("volume-root-register"),
            actor_principal_id="alice",
            device_id=11,
            inode=101,
            **volume_facts,
        )
        replayed_volume = self.ledger.register_ephemeral_volume_root(
            operation_id=self.op("volume-root-reopen"),
            actor_principal_id="alice",
            device_id=22,
            inode=202,
            **volume_facts,
        )
        validated_volume = self.ledger.validate_ephemeral_volume_root(
            device_id=33,
            inode=303,
            **volume_facts,
        )
        self.assertEqual(replayed_volume, registered_volume)
        self.assertEqual(validated_volume, registered_volume)

        projection_root_id = "projection-root-remount"
        projection_nonce = "b" * 64
        projection_marker = self.ledger.projection_root_marker_digest(
            projection_root_id=projection_root_id,
            backend_kind="verified-copy-v1",
            claim_nonce=projection_nonce,
        )
        projection_facts = {
            "projection_root_id": projection_root_id,
            "canonical_path": str(self.database.parent / "projections"),
            "backend_kind": "verified-copy-v1",
            "marker_digest": projection_marker,
            "claim_nonce": projection_nonce,
        }
        registered_projection = self.ledger.register_projection_root(
            operation_id=self.op("projection-root-register"),
            actor_principal_id="alice",
            device_id=44,
            inode=404,
            **projection_facts,
        )
        replayed_projection = self.ledger.register_projection_root(
            operation_id=self.op("projection-root-reopen"),
            actor_principal_id="alice",
            device_id=55,
            inode=505,
            **projection_facts,
        )
        validated_projection = self.ledger.validate_projection_root(
            device_id=66,
            inode=606,
            **projection_facts,
        )
        self.assertEqual(replayed_projection, registered_projection)
        self.assertEqual(validated_projection, registered_projection)

    def test_root_claim_marker_mismatch_remains_rejected(self) -> None:
        root_id = "volume-root-marker-mismatch"
        original_nonce = "c" * 64
        path = str(self.database.parent / "volumes-marker-mismatch")
        self.ledger.register_ephemeral_volume_root(
            operation_id=self.op("root-marker-original"),
            actor_principal_id="alice",
            volume_root_id=root_id,
            canonical_path=path,
            backend_kind="local-private-directory-v1",
            marker_digest=self.ledger.ephemeral_volume_root_marker_digest(
                volume_root_id=root_id,
                backend_kind="local-private-directory-v1",
                claim_nonce=original_nonce,
            ),
            claim_nonce=original_nonce,
            device_id=1,
            inode=2,
        )
        changed_nonce = "d" * 64
        changed = {
            "volume_root_id": root_id,
            "canonical_path": path,
            "backend_kind": "local-private-directory-v1",
            "marker_digest": self.ledger.ephemeral_volume_root_marker_digest(
                volume_root_id=root_id,
                backend_kind="local-private-directory-v1",
                claim_nonce=changed_nonce,
            ),
            "claim_nonce": changed_nonce,
            "device_id": 9,
            "inode": 10,
        }
        with self.assertRaises(RealmConflict):
            self.ledger.register_ephemeral_volume_root(
                operation_id=self.op("root-marker-changed"),
                actor_principal_id="alice",
                **changed,
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.validate_ephemeral_volume_root(**changed)

    def begin(self, owner_id: str, actor: str, revision: int = 0):
        return self.ledger.begin_owner_change(
            operation_id=self.op("begin"),
            actor_principal_id=actor,
            owner_id=owner_id,
            expected_owner_revision=revision,
            ttl_seconds=60,
        )

    def publication(
        self,
        *,
        store_id: str,
        content_ref,
        edges=(),
        byte_count: int = 1,
    ) -> PublishedObject:
        self.counter += 1
        kind = "blob" if isinstance(content_ref, BlobRef) else "tree"
        return PublishedObject(
            staging_id=f"stage-{self.counter:032x}",
            store_id=store_id,
            content_ref=content_ref,
            kind=kind,
            logical_bytes=byte_count,
            physical_bytes=byte_count + 7,
            metadata={"format": f"test-{kind}"},
            edges=tuple(edges),
        )

    def publish(
        self, change_id: str, publication: PublishedObject, *, actor: str = "alice"
    ) -> None:
        capture = self.ledger.content_capture_handle(
            actor_principal_id=actor,
            change_id=change_id,
            store_id=publication.store_id,
        )
        capture.reserve_staging(
            change_id=change_id,
            staging_id=publication.staging_id,
            store_id=publication.store_id,
            object_kind=publication.kind,
        )
        capture.prepare_publication(change_id=change_id, publication=publication)
        capture.record_publication(change_id=change_id, publication=publication)
        capture.complete_staging_publication(
            change_id=change_id,
            staging_id=publication.staging_id,
        )

    def commit_root(
        self,
        *,
        change,
        actor: str,
        membership: OwnerMembership,
    ):
        self.ledger.hold_owner_content(
            operation_id=self.op("hold"),
            actor_principal_id=actor,
            change_id=change.change_id,
            memberships=(membership,),
        )
        return self.ledger.commit_owner_change(
            operation_id=self.op("commit"),
            actor_principal_id=actor,
            change_id=change.change_id,
            expected_owner_revision=change.base_owner_revision,
            additions=(membership,),
        )

    def test_global_replay_conflict_and_migration_checksum(self) -> None:
        operation_id = self.op("replay")
        first = self.ledger.register_principal(
            operation_id=operation_id, principal_id="worker", kind="service"
        )
        replay = self.ledger.register_principal(
            operation_id=operation_id, principal_id="worker", kind="service"
        )
        self.assertEqual(first, replay)
        with self.assertRaises(RealmConflict):
            self.ledger.register_principal(
                operation_id=operation_id, principal_id="worker", kind="user"
            )
        self.assertEqual(self.ledger.integrity_check()["journal_mode"], "wal")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE schema_migrations SET migration_digest = ? WHERE version = 1",
                ("0" * 64,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RealmIntegrityError, "checksum"):
            RealmLedger(self.database)

    def test_root_membership_retains_exact_transitive_tree_closure(self) -> None:
        change = self.begin("owner-a", "alice")
        blob = BlobRef.from_bytes(b"tree child")
        tree = SnapshotRef.from_manifest_bytes(b"tree manifest")
        blob_publication = self.publication(
            store_id="store-a", content_ref=blob, byte_count=10
        )
        tree_publication = self.publication(
            store_id="store-a",
            content_ref=tree,
            byte_count=10,
            edges=(ContentEdge(tree, blob, "tree-file", "child.txt"),),
        )
        self.publish(change.change_id, blob_publication)
        self.publish(change.change_id, tree_publication)
        root = OwnerMembership("store-a", tree, "workspace-root")
        receipt = self.commit_root(change=change, actor="alice", membership=root)
        self.assertEqual(receipt.additions, (root,))
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="alice", owner_id="owner-a"
            ),
            (root,),
        )
        closure = self.ledger.resolve_content_closure(
            actor_principal_id="alice",
            owner_id="owner-a",
            store_id="store-a",
            root_ref=tree,
        )
        self.assertEqual({item.content_ref for item in closure}, {tree, blob})
        inventory = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertEqual(set(inventory.live_refs), {tree, blob})
        self.assertEqual(len(inventory.edges), 1)

    def test_digest_knowledge_is_not_authority_and_missing_is_indistinguishable(self) -> None:
        source_change = self.begin("owner-a", "alice")
        source_blob = BlobRef.from_bytes(b"source")
        self.publish(
            source_change.change_id,
            self.publication(store_id="store-a", content_ref=source_blob, byte_count=6),
        )
        source_root = OwnerMembership("store-a", source_blob, "root")
        self.commit_root(change=source_change, actor="alice", membership=source_root)

        target_change = self.begin("owner-b", "bob")
        desired = OwnerMembership("store-a", source_blob, "borrowed")
        failures = []
        for membership in (
            desired,
            OwnerMembership("store-a", BlobRef.from_bytes(b"absent"), "borrowed"),
        ):
            with self.assertRaises(RealmNotFound) as caught:
                self.ledger.hold_owner_content(
                    operation_id=self.op("unauthorized-hold"),
                    actor_principal_id="bob",
                    change_id=target_change.change_id,
                    memberships=(membership,),
                )
            failures.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(failures[0], failures[1])
        with self.assertRaises(RealmNotFound):
            self.ledger.hold_owner_content(
                operation_id=self.op("unauthorized-source"),
                actor_principal_id="bob",
                change_id=target_change.change_id,
                memberships=(desired,),
                source_owner_id="owner-a",
            )
        self.ledger.grant_owner_permission(
            operation_id=self.op("source-derive"),
            actor_principal_id="alice",
            owner_id="owner-a",
            principal_id="bob",
            permission=OwnerPermission.DERIVE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("authorized-source"),
            actor_principal_id="bob",
            change_id=target_change.change_id,
            memberships=(desired,),
            source_owner_id="owner-a",
        )
        receipt = self.ledger.commit_owner_change(
            operation_id=self.op("target-commit"),
            actor_principal_id="bob",
            change_id=target_change.change_id,
            expected_owner_revision=0,
            additions=(desired,),
        )
        self.assertEqual(receipt.additions, (desired,))

    def test_lease_scope_is_owner_bound_and_child_is_parent_bounded(self) -> None:
        parent = self.ledger.acquire_lease(
            operation_id=self.op("parent-lease"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="worker-a",
            scope_key="runtime/session-a",
            ttl_seconds=60,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.acquire_lease(
                operation_id=self.op("foreign-scope"),
                actor_principal_id="bob",
                owner_id="owner-b",
                lease_kind="inspection",
                audience="runtime",
                holder_id="worker-b",
                scope_key="runtime/session-a",
                ttl_seconds=60,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.acquire_lease(
                operation_id=self.op("child-same-scope"),
                actor_principal_id="alice",
                owner_id="owner-a",
                parent_lease_id=parent.lease_id,
                lease_kind="worker",
                audience="runtime",
                holder_id="child",
                scope_key="runtime/session-a",
                ttl_seconds=60,
            )
        child = self.ledger.acquire_lease(
            operation_id=self.op("child"),
            actor_principal_id="alice",
            owner_id="owner-a",
            parent_lease_id=parent.lease_id,
            lease_kind="worker",
            audience="runtime",
            holder_id="child",
            scope_key="runtime/session-a/child",
            ttl_seconds=600,
        )
        self.assertLessEqual(child.expires_at, parent.expires_at)
        self.ledger.release_lease(
            operation_id=self.op("release-parent"),
            actor_principal_id="alice",
            lease_id=parent.lease_id,
            holder_id=parent.holder_id,
            fencing_token=parent.fencing_token,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.validate_lease(
                actor_principal_id="alice",
                lease_id=child.lease_id,
                holder_id=child.holder_id,
                fencing_token=child.fencing_token,
            )
        current = self.ledger.acquire_lease(
            operation_id=self.op("replace-current"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="old-worker",
            scope_key="runtime/replacement",
            ttl_seconds=60,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.acquire_lease(
                operation_id=self.op("implicit-replacement"),
                actor_principal_id="alice",
                owner_id="owner-a",
                lease_kind="inspection",
                audience="runtime",
                holder_id="new-worker",
                scope_key="runtime/replacement",
                ttl_seconds=60,
            )
        replacement = self.ledger.acquire_lease(
            operation_id=self.op("explicit-replacement"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="new-worker",
            scope_key="runtime/replacement",
            ttl_seconds=60,
            replace_lease_id=current.lease_id,
            replace_fencing_token=current.fencing_token,
        )
        self.assertGreater(replacement.fencing_token, current.fencing_token)

    def test_content_root_lease_retains_closure_and_child_is_subset_bounded(self) -> None:
        change = self.begin("owner-a", "alice")
        child = BlobRef.from_bytes(b"leased child")
        tree = SnapshotRef.from_manifest_bytes(b"leased tree")
        outside = BlobRef.from_bytes(b"owned but outside parent lease")
        self.publish(
            change.change_id,
            self.publication(store_id="store-a", content_ref=child),
        )
        self.publish(
            change.change_id,
            self.publication(
                store_id="store-a",
                content_ref=tree,
                edges=(ContentEdge(tree, child, "tree-file", "child.txt"),),
            ),
        )
        self.publish(
            change.change_id,
            self.publication(store_id="store-a", content_ref=outside),
        )
        roots = (
            OwnerMembership("store-a", outside, "artifact"),
            OwnerMembership("store-a", tree, "workspace-root"),
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("hold-lease-roots"),
            actor_principal_id="alice",
            change_id=change.change_id,
            memberships=roots,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("commit-lease-roots"),
            actor_principal_id="alice",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=roots,
        )

        parent = self.ledger.acquire_lease(
            operation_id=self.op("content-parent"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="parent-worker",
            scope_key="content/session",
            ttl_seconds=60,
            content_roots=(OwnerMembership("store-a", tree, "inspection-root"),),
        )
        child_lease = self.ledger.acquire_lease(
            operation_id=self.op("content-child"),
            actor_principal_id="alice",
            owner_id="owner-a",
            parent_lease_id=parent.lease_id,
            lease_kind="worker",
            audience="runtime",
            holder_id="child-worker",
            scope_key="content/session/child",
            ttl_seconds=60,
            content_roots=(OwnerMembership("store-a", child, "inspection-input"),),
        )
        with self.assertRaisesRegex(RealmConflict, "subset"):
            self.ledger.acquire_lease(
                operation_id=self.op("content-outside-child"),
                actor_principal_id="alice",
                owner_id="owner-a",
                parent_lease_id=parent.lease_id,
                lease_kind="worker",
                audience="runtime",
                holder_id="outside-worker",
                scope_key="content/session/outside",
                ttl_seconds=60,
                content_roots=(
                    OwnerMembership("store-a", outside, "inspection-input"),
                ),
            )

        removal = self.begin("owner-a", "alice", revision=1)
        self.ledger.commit_owner_change(
            operation_id=self.op("remove-owned-roots"),
            actor_principal_id="alice",
            change_id=removal.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=roots,
        )
        delegated_after_removal = self.ledger.acquire_lease(
            operation_id=self.op("delegated-after-owner-removal"),
            actor_principal_id="alice",
            owner_id="owner-a",
            parent_lease_id=parent.lease_id,
            lease_kind="worker",
            audience="runtime",
            holder_id="late-child-worker",
            scope_key="content/session/late-child",
            ttl_seconds=60,
            content_roots=(OwnerMembership("store-a", child, "inspection-input"),),
        )
        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("lease-gc-start"), store_id="store-a"
        )
        tombstones = self.ledger.finish_gc_epoch(
            operation_id=self.op("lease-gc-finish"),
            store_id="store-a",
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        self.assertEqual({item.content_ref for item in tombstones}, {outside})
        self.ledger.validate_lease(
            actor_principal_id="alice",
            lease_id=child_lease.lease_id,
            holder_id=child_lease.holder_id,
            fencing_token=child_lease.fencing_token,
        )
        self.ledger.validate_lease(
            actor_principal_id="alice",
            lease_id=delegated_after_removal.lease_id,
            holder_id=delegated_after_removal.holder_id,
            fencing_token=delegated_after_removal.fencing_token,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE leases SET expires_at = 0 WHERE lease_id = ?",
                (parent.lease_id,),
            )
            connection.commit()
        finally:
            connection.close()
        next_epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("expired-parent-gc-start"), store_id="store-a"
        )
        after_parent_expiry = self.ledger.finish_gc_epoch(
            operation_id=self.op("expired-parent-gc-finish"),
            store_id="store-a",
            epoch=next_epoch.epoch,
            grace_seconds=0,
        )
        self.assertEqual(
            {item.content_ref for item in after_parent_expiry},
            {tree, child},
        )

    def test_terminal_changes_abandon_incomplete_staging_and_inventory_fails_closed(self) -> None:
        terminal_staging = []
        for terminal in ("abort", "commit"):
            change = self.begin("owner-a", "alice")
            publication = self.publication(
                store_id="store-a",
                content_ref=BlobRef.from_bytes(terminal.encode("utf-8")),
            )
            capture = self.ledger.content_capture_handle(
                actor_principal_id="alice",
                change_id=change.change_id,
                store_id="store-a",
            )
            capture.reserve_staging(
                change_id=change.change_id,
                staging_id=publication.staging_id,
                store_id="store-a",
                object_kind="blob",
            )
            capture.prepare_publication(change_id=change.change_id, publication=publication)
            terminal_staging.append(publication.staging_id)
            if terminal == "abort":
                self.ledger.abort_owner_change(
                    operation_id=self.op("abandon-on-abort"),
                    actor_principal_id="alice",
                    change_id=change.change_id,
                )
            else:
                self.ledger.commit_owner_change(
                    operation_id=self.op("abandon-on-commit"),
                    actor_principal_id="alice",
                    change_id=change.change_id,
                    expected_owner_revision=0,
                    additions=(),
                )

        expiring = self.begin("owner-a", "alice")
        expiring_publication = self.publication(
            store_id="store-a", content_ref=BlobRef.from_bytes(b"expiry")
        )
        expiring_capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=expiring.change_id,
            store_id="store-a",
        )
        expiring_capture.reserve_staging(
            change_id=expiring.change_id,
            staging_id=expiring_publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        terminal_staging.append(expiring_publication.staging_id)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE owner_transactions SET expires_at = 0 WHERE change_id = ?",
                (expiring.change_id,),
            )
            connection.execute(
                "UPDATE leases SET expires_at = 0 WHERE lease_id = ?",
                (expiring.retention_lease_id,),
            )
            connection.commit()
        finally:
            connection.close()

        before_sweep = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertNotIn(
            expiring_publication.staging_id,
            {item.staging_id for item in before_sweep.staging_allocations},
        )
        self.ledger.sweep_expired_leases(operation_id=self.op("expire-and-abandon"))
        after_sweep = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertEqual(after_sweep.staging_allocations, ())
        connection = sqlite3.connect(self.database)
        try:
            states = dict(
                connection.execute(
                    "SELECT staging_id, state FROM staging_allocations "
                    "WHERE staging_id IN (?, ?, ?)",
                    tuple(terminal_staging),
                )
            )
        finally:
            connection.close()
        self.assertEqual(states, {item: "abandoned" for item in terminal_staging})

    def test_abandoned_prepared_cleanup_is_fenced_replayable_and_discoverable(self) -> None:
        change = self.begin("owner-a", "alice")
        content_ref = BlobRef.from_bytes(b"prepared cleanup orphan")
        publication = self.publication(store_id="store-a", content_ref=content_ref)
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id="store-a",
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        capture.prepare_publication(change_id=change.change_id, publication=publication)
        self.ledger.abort_owner_change(
            operation_id=self.op("abandon-prepared-cleanup"),
            actor_principal_id="alice",
            change_id=change.change_id,
        )

        claim_operation = self.op("claim-prepared-cleanup")
        claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=claim_operation,
            store_id="store-a",
            staging_id=publication.staging_id,
        )
        replayed_claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=claim_operation,
            store_id="store-a",
            staging_id=publication.staging_id,
        )
        self.assertEqual(claim, replayed_claim)
        self.assertEqual(claim.state, "cleaning")
        self.assertEqual(claim.backend_kind, "local-cas")
        self.assertEqual(claim.root_marker, "marker-store-a")
        self.assertEqual(claim.content_ref, content_ref)
        self.assertEqual(claim.publication_digest, publication_digest(publication))
        self.assertIsNotNone(claim.cleanup_token)
        self.assertEqual(
            self.ledger.list_abandoned_staging_cleanups(store_id="store-a"),
            (claim,),
        )
        self.assertEqual(
            tuple(
                self.ledger.content_inventory_snapshot(
                    store_id="store-a"
                ).expected_staging_cleanups(store_id="store-a")
            ),
            (claim,),
        )
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "staging cleanup token is immutable"
            ):
                connection.execute(
                    "UPDATE staging_allocations SET cleanup_token = ? WHERE staging_id = ?",
                    ("cleanup-" + "f" * 32, publication.staging_id),
                )
            connection.rollback()
        finally:
            connection.close()
        validation = self.ledger.validate_abandoned_staging_cleanup(
            store_id="store-a",
            staging_id=publication.staging_id,
            cleanup_token=claim.cleanup_token or "",
        )
        self.assertEqual(validation.staging_id, claim.staging_id)
        self.assertEqual(validation.backend_kind, claim.backend_kind)
        self.assertEqual(validation.root_marker, claim.root_marker)
        self.assertTrue(validation.remove_live_orphan)
        stale_token = "cleanup-" + "0" * 32
        with self.assertRaises(RealmConflict):
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=publication.staging_id,
                cleanup_token=stale_token,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.complete_abandoned_staging_cleanup(
                operation_id=self.op("stale-cleanup-complete"),
                store_id="store-a",
                staging_id=publication.staging_id,
                cleanup_token=stale_token,
            )

        complete_operation = self.op("complete-prepared-cleanup")
        completed = self.ledger.complete_abandoned_staging_cleanup(
            operation_id=complete_operation,
            store_id="store-a",
            staging_id=publication.staging_id,
            cleanup_token=claim.cleanup_token or "",
        )
        replayed_complete = self.ledger.complete_abandoned_staging_cleanup(
            operation_id=complete_operation,
            store_id="store-a",
            staging_id=publication.staging_id,
            cleanup_token=claim.cleanup_token or "",
        )
        self.assertEqual(completed, replayed_complete)
        self.assertEqual(completed.state, "cleaned")
        self.assertEqual(
            self.ledger.list_abandoned_staging_cleanups(store_id="store-a"),
            (),
        )
        self.assertEqual(
            self.ledger.content_inventory_snapshot(store_id="store-a").staging_cleanups,
            (),
        )
        self.assertEqual(
            self.ledger.list_abandoned_staging_cleanups(
                store_id="store-a", states=("cleaned",)
            ),
            (completed,),
        )
        connection = sqlite3.connect(self.database)
        try:
            prepared = connection.execute(
                "SELECT content_ref, publication_digest FROM prepared_staging_publications "
                "WHERE staging_id = ?",
                (publication.staging_id,),
            ).fetchone()
            registered = connection.execute(
                "SELECT 1 FROM content_objects WHERE store_id = ? AND content_ref = ?",
                ("store-a", str(content_ref)),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(prepared, (str(content_ref), publication_digest(publication)))
        self.assertIsNone(registered)

    def test_unfinalized_published_stage_becomes_safe_terminal_cleanup_debt(self) -> None:
        change = self.begin("owner-a", "alice")
        content_ref = BlobRef.from_bytes(b"lost record response")
        publication = self.publication(store_id="store-a", content_ref=content_ref)
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id="store-a",
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        capture.prepare_publication(change_id=change.change_id, publication=publication)
        capture.record_publication(change_id=change.change_id, publication=publication)
        transient = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertEqual(len(transient.staging_allocations), 1)
        self.assertEqual(transient.staging_allocations[0].state, "published")
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-unfinalized-published"),
            actor_principal_id="alice",
            change_id=change.change_id,
        )
        claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=self.op("claim-unfinalized-published"),
            store_id="store-a",
            staging_id=publication.staging_id,
        )
        self.assertEqual(claim.content_ref, content_ref)
        self.assertFalse(
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=publication.staging_id,
                cleanup_token=claim.cleanup_token or "",
            ).remove_live_orphan
        )
        self.ledger.complete_abandoned_staging_cleanup(
            operation_id=self.op("complete-unfinalized-published"),
            store_id="store-a",
            staging_id=publication.staging_id,
            cleanup_token=claim.cleanup_token or "",
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM content_objects WHERE store_id = ? AND content_ref = ?",
                    ("store-a", str(content_ref)),
                ).fetchone()
            )
        finally:
            connection.close()

        race_change = self.begin("owner-a", "alice")
        race_publication = self.publication(
            store_id="store-a", content_ref=BlobRef.from_bytes(b"terminal finalize race")
        )
        race_capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=race_change.change_id,
            store_id="store-a",
        )
        race_capture.reserve_staging(
            change_id=race_change.change_id,
            staging_id=race_publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        race_capture.prepare_publication(
            change_id=race_change.change_id, publication=race_publication
        )
        race_capture.record_publication(
            change_id=race_change.change_id, publication=race_publication
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-before-finalize-callback"),
            actor_principal_id="alice",
            change_id=race_change.change_id,
        )
        race_capture.complete_staging_publication(
            change_id=race_change.change_id,
            staging_id=race_publication.staging_id,
        )
        race_capture.record_publication(
            change_id=race_change.change_id,
            publication=race_publication,
        )
        self.assertNotIn(
            race_publication.staging_id,
            {
                item.staging_id
                for item in self.ledger.list_abandoned_staging_cleanups(
                    store_id="store-a"
                )
            },
        )
        connection = sqlite3.connect(self.database)
        try:
            state = connection.execute(
                "SELECT state FROM staging_allocations WHERE staging_id = ?",
                (race_publication.staging_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(state, "finalized")
        race_capture.rollback_capture(
            change_id=race_change.change_id,
            staging_ids=(race_publication.staging_id,),
        )
        connection = sqlite3.connect(self.database)
        try:
            rolled_back = connection.execute(
                "SELECT state FROM staging_allocations WHERE staging_id = ?",
                (race_publication.staging_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(rolled_back, "rolled_back")

    def test_abandoned_cleanup_never_removes_registered_or_actively_prepared_content(self) -> None:
        def abandoned_prepared(payload: bytes):
            change = self.begin("owner-a", "alice")
            content_ref = BlobRef.from_bytes(payload)
            publication = self.publication(store_id="store-a", content_ref=content_ref)
            capture = self.ledger.content_capture_handle(
                actor_principal_id="alice",
                change_id=change.change_id,
                store_id="store-a",
            )
            capture.reserve_staging(
                change_id=change.change_id,
                staging_id=publication.staging_id,
                store_id="store-a",
                object_kind="blob",
            )
            capture.prepare_publication(change_id=change.change_id, publication=publication)
            self.ledger.abort_owner_change(
                operation_id=self.op("abandon-prepared"),
                actor_principal_id="alice",
                change_id=change.change_id,
            )
            return content_ref, publication

        registered_ref, registered_abandoned = abandoned_prepared(b"registered-content")
        publisher = self.begin("owner-a", "alice")
        self.publish(
            publisher.change_id,
            self.publication(store_id="store-a", content_ref=registered_ref),
        )
        registered_claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=self.op("claim-registered-preservation"),
            store_id="store-a",
            staging_id=registered_abandoned.staging_id,
        )
        registered_validation = self.ledger.validate_abandoned_staging_cleanup(
            store_id="store-a",
            staging_id=registered_abandoned.staging_id,
            cleanup_token=registered_claim.cleanup_token or "",
        )
        self.assertFalse(registered_validation.remove_live_orphan)
        self.ledger.complete_abandoned_staging_cleanup(
            operation_id=self.op("complete-registered-preservation"),
            store_id="store-a",
            staging_id=registered_abandoned.staging_id,
            cleanup_token=registered_claim.cleanup_token or "",
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM content_objects WHERE store_id = ? AND content_ref = ?",
                    ("store-a", str(registered_ref)),
                ).fetchone()
            )
        finally:
            connection.close()

        prepared_ref, first_prepared = abandoned_prepared(b"other-active-prepared")
        other_change = self.begin("owner-a", "alice")
        other_publication = self.publication(
            store_id="store-a", content_ref=prepared_ref
        )
        other_capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=other_change.change_id,
            store_id="store-a",
        )
        other_capture.reserve_staging(
            change_id=other_change.change_id,
            staging_id=other_publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        other_capture.prepare_publication(
            change_id=other_change.change_id, publication=other_publication
        )
        prepared_claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=self.op("claim-other-prepared-preservation"),
            store_id="store-a",
            staging_id=first_prepared.staging_id,
        )
        self.assertFalse(
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=first_prepared.staging_id,
                cleanup_token=prepared_claim.cleanup_token or "",
            ).remove_live_orphan
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abandon-other-prepared"),
            actor_principal_id="alice",
            change_id=other_change.change_id,
        )
        self.assertTrue(
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=first_prepared.staging_id,
                cleanup_token=prepared_claim.cleanup_token or "",
            ).remove_live_orphan
        )

    def test_deleted_content_history_does_not_protect_a_new_live_orphan(self) -> None:
        content_ref = BlobRef.from_bytes(b"deleted-history-live-orphan")
        original_change = self.begin("owner-a", "alice")
        self.publish(
            original_change.change_id,
            self.publication(store_id="store-a", content_ref=content_ref),
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("release-original-publication"),
            actor_principal_id="alice",
            change_id=original_change.change_id,
        )
        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("start-original-gc"), store_id="store-a"
        )
        self.ledger.finish_gc_epoch(
            operation_id=self.op("finish-original-gc"),
            store_id="store-a",
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        tombstone = self.ledger.claim_tombstone(
            operation_id=self.op("claim-original-gc"),
            store_id="store-a",
            content_ref=content_ref,
        )
        self.ledger.complete_tombstone(
            operation_id=self.op("complete-original-gc"),
            store_id="store-a",
            content_ref=content_ref,
            deletion_token=tombstone.deletion_token or "",
        )

        orphan_change = self.begin("owner-a", "alice")
        orphan_publication = self.publication(
            store_id="store-a", content_ref=content_ref
        )
        orphan_capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=orphan_change.change_id,
            store_id="store-a",
        )
        orphan_capture.reserve_staging(
            change_id=orphan_change.change_id,
            staging_id=orphan_publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        orphan_capture.prepare_publication(
            change_id=orphan_change.change_id, publication=orphan_publication
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abandon-new-live-orphan"),
            actor_principal_id="alice",
            change_id=orphan_change.change_id,
        )
        cleanup = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=self.op("claim-new-live-orphan"),
            store_id="store-a",
            staging_id=orphan_publication.staging_id,
        )
        self.assertTrue(
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=orphan_publication.staging_id,
                cleanup_token=cleanup.cleanup_token or "",
            ).remove_live_orphan
        )

    def test_allocated_abandoned_cleanup_is_stage_only(self) -> None:
        change = self.begin("owner-a", "alice")
        publication = self.publication(
            store_id="store-a", content_ref=BlobRef.from_bytes(b"allocated-only")
        )
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id="store-a",
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abandon-allocated-only"),
            actor_principal_id="alice",
            change_id=change.change_id,
        )
        claim = self.ledger.claim_abandoned_staging_cleanup(
            operation_id=self.op("claim-allocated-only"),
            store_id="store-a",
            staging_id=publication.staging_id,
        )
        self.assertIsNone(claim.content_ref)
        self.assertFalse(
            self.ledger.validate_abandoned_staging_cleanup(
                store_id="store-a",
                staging_id=publication.staging_id,
                cleanup_token=claim.cleanup_token or "",
            ).remove_live_orphan
        )

    def test_pinned_database_identity_rejects_terminal_symlink_swap(self) -> None:
        other_path = Path(self.temporary.name) / "other.sqlite3"
        other = RealmLedger(other_path)
        try:
            self.database.unlink()
            self.database.symlink_to(other_path)
            with self.assertRaisesRegex(RealmIntegrityError, "authority path"):
                self.ledger.integrity_check()
        finally:
            other.close()

    def test_one_owner_can_commit_roots_from_multiple_stores(self) -> None:
        change = self.begin("owner-a", "alice")
        roots = []
        for store_id, payload in (("store-a", b"a"), ("store-b", b"b")):
            reference = BlobRef.from_bytes(payload)
            self.publish(
                change.change_id,
                self.publication(store_id=store_id, content_ref=reference),
            )
            roots.append(OwnerMembership(store_id, reference, "artifact"))
        roots_tuple = tuple(sorted(roots, key=lambda item: item.store_id))
        self.ledger.hold_owner_content(
            operation_id=self.op("multi-hold"),
            actor_principal_id="alice",
            change_id=change.change_id,
            memberships=roots_tuple,
        )
        receipt = self.ledger.commit_owner_change(
            operation_id=self.op("multi-commit"),
            actor_principal_id="alice",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=roots_tuple,
        )
        self.assertEqual(receipt.additions, roots_tuple)

    def test_competing_owner_commits_have_exactly_one_revision_winner(self) -> None:
        changes = (self.begin("owner-a", "alice"), self.begin("owner-a", "alice"))
        memberships = []
        for index, change in enumerate(changes):
            reference = BlobRef.from_bytes(f"concurrent-{index}".encode())
            self.publish(
                change.change_id,
                self.publication(store_id="store-a", content_ref=reference),
            )
            membership = OwnerMembership("store-a", reference, "artifact")
            self.ledger.hold_owner_content(
                operation_id=self.op(f"concurrent-hold-{index}"),
                actor_principal_id="alice",
                change_id=change.change_id,
                memberships=(membership,),
            )
            memberships.append(membership)

        operation_ids = (self.op("concurrent-commit-a"), self.op("concurrent-commit-b"))
        barrier = threading.Barrier(3)
        receipts = []
        errors = []

        def commit(index: int) -> None:
            barrier.wait()
            try:
                receipts.append(
                    self.ledger.commit_owner_change(
                        operation_id=operation_ids[index],
                        actor_principal_id="alice",
                        change_id=changes[index].change_id,
                        expected_owner_revision=0,
                        additions=(memberships[index],),
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=commit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RealmConflict)
        self.assertEqual(receipts[0].owner_revision, 1)

    def test_competing_explicit_lease_replacements_have_one_fencing_winner(self) -> None:
        current = self.ledger.acquire_lease(
            operation_id=self.op("concurrent-lease-current"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="holder-a",
            scope_key="inspection/current",
            ttl_seconds=60,
        )
        operation_ids = (self.op("replace-a"), self.op("replace-b"))
        barrier = threading.Barrier(3)
        replacements = []
        errors = []

        def replace(index: int) -> None:
            barrier.wait()
            try:
                replacements.append(
                    self.ledger.acquire_lease(
                        operation_id=operation_ids[index],
                        actor_principal_id="alice",
                        owner_id="owner-a",
                        lease_kind="inspection",
                        audience="runtime",
                        holder_id=f"holder-{index}",
                        scope_key=current.scope_key,
                        ttl_seconds=60,
                        replace_lease_id=current.lease_id,
                        replace_fencing_token=current.fencing_token,
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=replace, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(replacements), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RealmConflict)
        self.assertGreater(replacements[0].fencing_token, current.fencing_token)

    def test_prepared_publication_and_gc_stale_token_reactivation(self) -> None:
        change = self.begin("owner-a", "alice")
        orphan = BlobRef.from_bytes(b"orphan")
        publication = self.publication(
            store_id="store-a", content_ref=orphan, byte_count=6
        )
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id="store-a",
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        capture.prepare_publication(change_id=change.change_id, publication=publication)
        prepared_inventory = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertEqual(len(prepared_inventory.staging_allocations), 1)
        self.assertEqual(
            prepared_inventory.staging_allocations[0].content_ref,
            orphan,
        )
        self.assertEqual(
            prepared_inventory.staging_allocations[0].publication_digest,
            publication_digest(publication),
        )
        connection = sqlite3.connect(self.database)
        try:
            state, content_ref = connection.execute(
                "SELECT state, content_ref FROM staging_allocations WHERE staging_id = ?",
                (publication.staging_id,),
            ).fetchone()
            self.assertEqual((state, content_ref), ("prepared", None))
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM content_objects WHERE store_id = ? AND content_ref = ?",
                    ("store-a", str(orphan)),
                ).fetchone()
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE staging_allocations SET state = 'published', content_ref = ?, "
                    "retention_role = 'staging', publication_digest = ? WHERE staging_id = ?",
                    (str(orphan), "0" * 64, publication.staging_id),
                )
            connection.rollback()
        finally:
            connection.close()
        capture.record_publication(change_id=change.change_id, publication=publication)
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-orphan"),
            actor_principal_id="alice",
            change_id=change.change_id,
        )
        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("gc-start"), store_id="store-a"
        )
        tombstones = self.ledger.finish_gc_epoch(
            operation_id=self.op("gc-finish"),
            store_id="store-a",
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        self.assertEqual([item.content_ref for item in tombstones], [orphan])
        grace_inventory = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertIn(orphan, grace_inventory.live_refs)
        self.assertEqual(grace_inventory.cleanup_refs, (orphan,))
        claimed = self.ledger.claim_tombstone(
            operation_id=self.op("gc-claim"),
            store_id="store-a",
            content_ref=orphan,
        )
        self.assertIsNotNone(claimed.deletion_token)
        deleting_inventory = self.ledger.content_inventory_snapshot(store_id="store-a")
        self.assertNotIn(orphan, deleting_inventory.live_refs)
        self.assertEqual(deleting_inventory.cleanup_refs, (orphan,))
        self.assertIn(
            orphan,
            {item.content_ref for item in deleting_inventory.content_facts},
        )
        self.assertEqual(deleting_inventory.tombstones, (claimed,))
        self.assertEqual(self.ledger.list_gc_tombstones(store_id="store-a"), (claimed,))
        replacement = self.begin("owner-a", "alice")
        republished = self.publication(
            store_id="store-a", content_ref=orphan, byte_count=6
        )
        self.publish(replacement.change_id, republished)
        cancelled = self.ledger.content_inventory_snapshot(store_id="store-a").tombstones
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].deletion_token, claimed.deletion_token)
        self.assertEqual(cancelled[0].state, "cancelled")
        with self.assertRaises(RealmConflict):
            self.ledger.validate_tombstone_claim(
                store_id="store-a",
                content_ref=orphan,
                deletion_token=claimed.deletion_token or "",
            )
        with self.assertRaises(RealmConflict):
            self.ledger.complete_tombstone(
                operation_id=self.op("stale-complete"),
                store_id="store-a",
                content_ref=orphan,
                deletion_token=claimed.deletion_token or "",
            )


if __name__ == "__main__":
    unittest.main()
