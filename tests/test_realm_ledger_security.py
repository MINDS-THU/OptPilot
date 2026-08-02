"""Adversarial security invariants for the production realm ledger.

These tests intentionally exercise hostile state transitions at the public
ledger boundary (and, for migration constraints, through a raw SQLite
connection).  They remain separate from the functional ledger suite so a
security regression is easy to identify.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import ContentEdge, PublishedObject
from optpilot.realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import BlobRef, CandidateRef, SnapshotRef


class RealmLedgerSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.counter = 0
        for principal_id in ("alice", "bob"):
            self.ledger.register_principal(
                operation_id=self.op(f"principal-{principal_id}"),
                principal_id=principal_id,
                kind="user",
            )
        self.ledger.register_store(
            operation_id=self.op("store"),
            store_id="store-a",
            backend_kind="local-cas",
            root_marker="security-test-store-marker",
        )
        for owner_id, principal_id in (("owner-a", "alice"), ("owner-b", "bob")):
            self.ledger.create_owner(
                operation_id=self.op(f"owner-{owner_id}"),
                owner_id=owner_id,
                owner_kind="workspace",
                principal_id=principal_id,
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"security/{self.counter}/{label}"

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
        content_ref,
        *,
        edges=(),
        logical_bytes: int = 1,
    ) -> PublishedObject:
        self.counter += 1
        kind = "blob" if isinstance(content_ref, BlobRef) else "tree"
        return PublishedObject(
            staging_id=f"stage-{self.counter:032x}",
            store_id="store-a",
            content_ref=content_ref,
            kind=kind,
            logical_bytes=logical_bytes,
            physical_bytes=logical_bytes + 7,
            metadata={"format": f"security-test-{kind}"},
            edges=tuple(edges),
        )

    def publish(self, change_id: str, publication: PublishedObject) -> None:
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
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

    def commit(self, change, actor: str, membership: OwnerMembership) -> None:
        self.ledger.hold_owner_content(
            operation_id=self.op("hold"),
            actor_principal_id=actor,
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("commit"),
            actor_principal_id=actor,
            change_id=change.change_id,
            expected_owner_revision=change.base_owner_revision,
            additions=(membership,),
        )

    @unittest.skipIf(os.name == "nt", "symlink swap test requires POSIX symlink semantics")
    def test_database_symlink_swap_is_fail_closed(self) -> None:
        """Replacing the initialized DB path must not redirect later writes."""

        victim = self.root / "victim.sqlite3"
        victim_connection = sqlite3.connect(victim)
        try:
            victim_connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            victim_connection.execute("INSERT INTO sentinel(value) VALUES ('untouched')")
            victim_connection.commit()
        finally:
            victim_connection.close()

        self.database.unlink()
        self.database.symlink_to(victim.name)

        with self.assertRaises((sqlite3.OperationalError, RealmIntegrityError)):
            self.ledger.register_principal(
                operation_id=self.op("after-symlink-swap"),
                principal_id="mallory",
                kind="user",
            )

        victim_connection = sqlite3.connect(victim)
        try:
            self.assertEqual(
                victim_connection.execute("SELECT value FROM sentinel").fetchall(),
                [("untouched",)],
            )
            self.assertIsNone(
                victim_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'principals'"
                ).fetchone()
            )
        finally:
            victim_connection.close()

    def test_content_capture_handle_is_bound_to_actor_change_and_store(self) -> None:
        change = self.begin("owner-a", "alice")
        with self.assertRaises(RealmNotFound):
            self.ledger.content_capture_handle(
                actor_principal_id="bob",
                change_id=change.change_id,
                store_id="store-a",
            )

        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id="store-a",
        )
        with self.assertRaises(AttributeError):
            capture.change_id = "another-change"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            capture.store_id = "store-b"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            capture.root_marker = "replacement"  # type: ignore[misc]
        capture.validate_capture_binding(
            change_id=change.change_id,
            store_id="store-a",
            backend_kind="local-cas",
            root_marker="security-test-store-marker",
        )
        for binding in (
            {
                "change_id": "another-change",
                "store_id": "store-a",
                "backend_kind": "local-cas",
                "root_marker": "security-test-store-marker",
            },
            {
                "change_id": change.change_id,
                "store_id": "store-a",
                "backend_kind": "remote-cas",
                "root_marker": "security-test-store-marker",
            },
            {
                "change_id": change.change_id,
                "store_id": "store-a",
                "backend_kind": "local-cas",
                "root_marker": "another-physical-root",
            },
        ):
            with self.assertRaises(RealmNotFound):
                capture.validate_capture_binding(**binding)
        staging_id = "stage-" + "f" * 32
        with self.assertRaises(RealmNotFound):
            capture.reserve_staging(
                change_id="another-change",
                staging_id=staging_id,
                store_id="store-a",
                object_kind="blob",
            )
        with self.assertRaises(ValueError):
            capture.reserve_staging(
                change_id=change.change_id,
                staging_id="stage-" + "e" * 32,
                store_id="store-a",
                object_kind="candidate",
            )
        with self.assertRaises(ValueError):
            capture.reserve_staging(
                change_id=change.change_id,
                staging_id="../unsafe-stage",
                store_id="store-a",
                object_kind="blob",
            )
        with self.assertRaises(RealmNotFound):
            capture.reserve_staging(
                change_id=change.change_id,
                staging_id=staging_id,
                store_id="store-b",
                object_kind="blob",
            )

        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM staging_allocations WHERE staging_id = ?",
                    (staging_id,),
                ).fetchone()
            )
        finally:
            connection.close()

        self.ledger.register_store(
            operation_id=self.op("unsupported-store"),
            store_id="store-remote",
            backend_kind="remote-cas",
            root_marker="remote-store-marker",
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.content_capture_handle(
                actor_principal_id="alice",
                change_id=change.change_id,
                store_id="store-remote",
            )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE stores SET state = 'disabled' WHERE store_id = 'store-a'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmNotFound):
            self.ledger.content_capture_handle(
                actor_principal_id="alice",
                change_id=change.change_id,
                store_id="store-a",
            )

    def test_change_state_is_not_disclosed_before_owner_authorization(self) -> None:
        active = self.begin("owner-a", "alice")
        aborted = self.begin("owner-a", "alice")
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-private-change"),
            actor_principal_id="alice",
            change_id=aborted.change_id,
        )
        committed = self.begin("owner-a", "alice")
        self.ledger.commit_owner_change(
            operation_id=self.op("commit-private-change"),
            actor_principal_id="alice",
            change_id=committed.change_id,
            expected_owner_revision=0,
            additions=(),
        )
        expired = self.begin("owner-a", "alice")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE owner_transactions SET state = 'expired' WHERE change_id = ?",
                (expired.change_id,),
            )
            connection.execute(
                "UPDATE leases SET state = 'expired' WHERE lease_id = ?",
                (expired.retention_lease_id,),
            )
            connection.commit()
        finally:
            connection.close()

        change_ids = (
            active.change_id,
            aborted.change_id,
            committed.change_id,
            expired.change_id,
            "missing-change",
        )
        for action in ("capture", "hold", "commit"):
            failures = []
            for change_id in change_ids:
                with self.assertRaises(RealmNotFound) as caught:
                    if action == "capture":
                        self.ledger.content_capture_handle(
                            actor_principal_id="bob",
                            change_id=change_id,
                            store_id="store-a",
                        )
                    elif action == "hold":
                        self.ledger.hold_owner_content(
                            operation_id=self.op("unauthorized-state-hold"),
                            actor_principal_id="bob",
                            change_id=change_id,
                            memberships=(),
                        )
                    else:
                        self.ledger.commit_owner_change(
                            operation_id=self.op("unauthorized-state-commit"),
                            actor_principal_id="bob",
                            change_id=change_id,
                            expected_owner_revision=0,
                            additions=(),
                        )
                failures.append((type(caught.exception), caught.exception.args))
            self.assertEqual(len(set(failures)), 1, action)

    def test_child_lease_is_invalid_immediately_after_parent_release(self) -> None:
        parent = self.ledger.acquire_lease(
            operation_id=self.op("parent"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="parent-worker",
            scope_key="inspection/session",
            ttl_seconds=60,
        )
        child = self.ledger.acquire_lease(
            operation_id=self.op("child"),
            actor_principal_id="alice",
            owner_id="owner-a",
            parent_lease_id=parent.lease_id,
            lease_kind="worker",
            audience="runtime",
            holder_id="child-worker",
            scope_key="inspection/session/child",
            ttl_seconds=60,
        )
        self.ledger.release_lease(
            operation_id=self.op("release-parent"),
            actor_principal_id="alice",
            lease_id=parent.lease_id,
            holder_id=parent.holder_id,
            fencing_token=parent.fencing_token,
        )

        with self.assertRaises((RealmConflict, RealmExpired)):
            self.ledger.validate_lease(
                actor_principal_id="alice",
                lease_id=child.lease_id,
                holder_id=child.holder_id,
                fencing_token=child.fencing_token,
            )

    def test_unexpired_scope_cannot_be_taken_over_without_explicit_cas(self) -> None:
        first = self.ledger.acquire_lease(
            operation_id=self.op("first-holder"),
            actor_principal_id="alice",
            owner_id="owner-a",
            lease_kind="inspection",
            audience="runtime",
            holder_id="worker-1",
            scope_key="exclusive/session",
            ttl_seconds=60,
        )

        with self.assertRaises(RealmConflict):
            self.ledger.acquire_lease(
                operation_id=self.op("takeover-without-cas"),
                actor_principal_id="alice",
                owner_id="owner-a",
                lease_kind="inspection",
                audience="runtime",
                holder_id="worker-2",
                scope_key="exclusive/session",
                ttl_seconds=60,
            )

        # A rejected takeover must leave the original fence valid.
        validated = self.ledger.validate_lease(
            actor_principal_id="alice",
            lease_id=first.lease_id,
            holder_id=first.holder_id,
            fencing_token=first.fencing_token,
        )
        self.assertEqual(validated.lease_id, first.lease_id)

    def test_unauthorized_and_missing_references_are_indistinguishable(self) -> None:
        source_change = self.begin("owner-a", "alice")
        source_ref = BlobRef.from_bytes(b"secret source content")
        self.publish(
            source_change.change_id,
            self.publication(source_ref, logical_bytes=len(b"secret source content")),
        )
        self.commit(
            source_change,
            "alice",
            OwnerMembership("store-a", source_ref, "workspace-root"),
        )

        target_change = self.begin("owner-b", "bob")
        missing_ref = BlobRef.from_bytes(b"content that was never published")
        failures = []
        for label, content_ref in (
            ("known-digest", source_ref),
            ("missing-digest", missing_ref),
        ):
            with self.assertRaises(RealmNotFound) as caught:
                self.ledger.hold_owner_content(
                    operation_id=self.op(label),
                    actor_principal_id="bob",
                    change_id=target_change.change_id,
                    memberships=(
                        OwnerMembership("store-a", content_ref, "workspace-root"),
                    ),
                )
            failures.append((type(caught.exception), caught.exception.args))

        self.assertEqual(failures[0], failures[1])

    def test_staging_migration_enforces_prepared_and_published_invariants(self) -> None:
        change = self.begin("owner-a", "alice")
        content_ref = BlobRef.from_bytes(b"prepared publication")
        publication = self.publication(
            content_ref,
            logical_bytes=len(b"prepared publication"),
        )
        capture = self.ledger.content_capture_handle(
            actor_principal_id="alice",
            change_id=change.change_id,
            store_id=publication.store_id,
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id=publication.store_id,
            object_kind=publication.kind,
        )

        # A prepared allocation keeps immutable publication facts in its
        # dedicated side table, never in the live/published columns.
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO staging_allocations("
                    "staging_id, change_id, store_id, object_kind, content_ref, "
                    "retention_role, publication_digest, state, expires_at, created_at, updated_at"
                    ") SELECT '../unsafe', change_id, store_id, object_kind, NULL, NULL, NULL, "
                    "'allocated', expires_at, created_at, updated_at "
                    "FROM staging_allocations WHERE staging_id = ?",
                    (publication.staging_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO prepared_staging_publications("
                    "staging_id, store_id, content_ref, retention_role, publication_digest"
                    ") VALUES (?, 'store-a', ?, 'staging', ?)",
                    (
                        publication.staging_id,
                        str(
                            CandidateRef.build(
                                candidate_format="parameters",
                                spec={"x": 1},
                                content_refs=(),
                            )
                        ),
                        "0" * 64,
                    ),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "staging allocation history is immutable",
            ):
                connection.execute(
                    "DELETE FROM staging_allocations WHERE staging_id = ?",
                    (publication.staging_id,),
                )
            connection.rollback()
            direct_staging_id = "stage-" + "a" * 32
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "staging allocations must be inserted as allocated",
            ):
                connection.execute(
                    "INSERT INTO staging_allocations("
                    "staging_id, change_id, store_id, object_kind, content_ref, "
                    "retention_role, publication_digest, state, expires_at, created_at, updated_at"
                    ") SELECT ?, change_id, store_id, object_kind, NULL, NULL, NULL, "
                    "'prepared', expires_at, created_at, updated_at "
                    "FROM staging_allocations WHERE staging_id = ?",
                    (direct_staging_id, publication.staging_id),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "prepared staging allocation requires intended facts",
            ):
                connection.execute(
                    "UPDATE staging_allocations SET state = 'prepared' WHERE staging_id = ?",
                    (publication.staging_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE staging_allocations SET state = 'prepared', content_ref = ?, "
                    "retention_role = 'staging', publication_digest = ? WHERE staging_id = ?",
                    (str(content_ref), "0" * 64, publication.staging_id),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "staging allocation id is immutable",
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO staging_allocations("
                    "staging_id, change_id, store_id, object_kind, content_ref, "
                    "retention_role, publication_digest, state, expires_at, created_at, updated_at"
                    ") SELECT staging_id, change_id, store_id, object_kind, NULL, NULL, NULL, "
                    "'allocated', expires_at, created_at, updated_at "
                    "FROM staging_allocations WHERE staging_id = ?",
                    (publication.staging_id,),
                )
            connection.rollback()
        finally:
            connection.close()

        capture.prepare_publication(change_id=change.change_id, publication=publication)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            allocation = connection.execute(
                "SELECT state, content_ref, retention_role, publication_digest "
                "FROM staging_allocations WHERE staging_id = ?",
                (publication.staging_id,),
            ).fetchone()
            self.assertEqual(allocation, ("prepared", None, None, None))
            prepared = connection.execute(
                "SELECT store_id, content_ref, retention_role, publication_digest "
                "FROM prepared_staging_publications WHERE staging_id = ?",
                (publication.staging_id,),
            ).fetchone()
            self.assertIsNotNone(prepared)
            assert prepared is not None
            self.assertEqual(prepared[:3], ("store-a", str(content_ref), "staging"))
            self.assertEqual(len(prepared[3]), 64)

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "prepared staging facts are immutable",
            ):
                connection.execute(
                    "UPDATE prepared_staging_publications SET publication_digest = ? "
                    "WHERE staging_id = ?",
                    ("f" * 64, publication.staging_id),
                )
            connection.rollback()

            # Even with internally consistent row shape, a direct transition
            # cannot publish metadata before the referenced content row exists.
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "published staging allocation requires matching prepared content",
            ):
                connection.execute(
                    "UPDATE staging_allocations SET state = 'published', content_ref = ?, "
                    "retention_role = 'staging', publication_digest = ? WHERE staging_id = ?",
                    (str(content_ref), prepared[3], publication.staging_id),
                )
            connection.rollback()
        finally:
            connection.close()

        capture.record_publication(change_id=change.change_id, publication=publication)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            allocation = connection.execute(
                "SELECT state, content_ref, retention_role FROM staging_allocations "
                "WHERE staging_id = ?",
                (publication.staging_id,),
            ).fetchone()
            self.assertEqual(allocation, ("published", str(content_ref), "staging"))
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM content_objects WHERE store_id = ? AND content_ref = ?",
                    ("store-a", str(content_ref)),
                ).fetchone()
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "staging publication facts are immutable",
            ):
                connection.execute(
                    "UPDATE staging_allocations SET publication_digest = ? "
                    "WHERE staging_id = ?",
                    ("e" * 64, publication.staging_id),
                )
            connection.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "staging allocations must be inserted as allocated",
            ):
                connection.execute(
                    "INSERT INTO staging_allocations("
                    "staging_id, change_id, store_id, object_kind, content_ref, "
                    "retention_role, publication_digest, state, expires_at, created_at, updated_at"
                    ") SELECT ?, change_id, store_id, object_kind, content_ref, "
                    "retention_role, publication_digest, 'published', expires_at, created_at, updated_at "
                    "FROM staging_allocations WHERE staging_id = ?",
                    ("stage-" + "b" * 32, publication.staging_id),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_gc_protects_the_transitive_closure_of_an_owned_root(self) -> None:
        owned_change = self.begin("owner-a", "alice")
        child_ref = BlobRef.from_bytes(b"owned tree child")
        root_ref = SnapshotRef.from_manifest_bytes(b"owned tree manifest")
        self.publish(
            owned_change.change_id,
            self.publication(child_ref, logical_bytes=len(b"owned tree child")),
        )
        self.publish(
            owned_change.change_id,
            self.publication(
                root_ref,
                logical_bytes=len(b"owned tree child"),
                edges=(ContentEdge(root_ref, child_ref, "tree-file", "child.txt"),),
            ),
        )
        root_membership = OwnerMembership("store-a", root_ref, "workspace-root")
        self.commit(owned_change, "alice", root_membership)

        # Add one genuinely unreachable object so this test proves GC ran,
        # rather than merely observing an empty sweep.
        orphan_change = self.begin("owner-a", "alice", revision=1)
        orphan_ref = BlobRef.from_bytes(b"unreachable object")
        self.publish(
            orphan_change.change_id,
            self.publication(orphan_ref, logical_bytes=len(b"unreachable object")),
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-orphan"),
            actor_principal_id="alice",
            change_id=orphan_change.change_id,
        )

        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("gc-start"),
            store_id="store-a",
        )
        tombstones = self.ledger.finish_gc_epoch(
            operation_id=self.op("gc-finish"),
            store_id="store-a",
            epoch=epoch.epoch,
            grace_seconds=0,
        )

        self.assertEqual({item.content_ref for item in tombstones}, {orphan_ref})
        closure = self.ledger.resolve_content_closure(
            actor_principal_id="alice",
            owner_id="owner-a",
            store_id="store-a",
            root_ref=root_ref,
        )
        self.assertEqual({item.content_ref for item in closure}, {root_ref, child_ref})
        connection = sqlite3.connect(self.database)
        try:
            lifecycle = dict(
                connection.execute(
                    "SELECT content_ref, lifecycle_state FROM content_objects "
                    "WHERE store_id = 'store-a'"
                ).fetchall()
            )
        finally:
            connection.close()
        self.assertEqual(lifecycle[str(root_ref)], "live")
        self.assertEqual(lifecycle[str(child_ref)], "live")
        self.assertEqual(lifecycle[str(orphan_ref)], "tombstoned")

    def test_gc_explicit_root_mark_protects_its_transitive_closure(self) -> None:
        change = self.begin("owner-a", "alice")
        child_ref = BlobRef.from_bytes(b"explicitly marked child")
        root_ref = SnapshotRef.from_manifest_bytes(b"explicitly marked tree")
        self.publish(
            change.change_id,
            self.publication(child_ref, logical_bytes=len(b"explicitly marked child")),
        )
        self.publish(
            change.change_id,
            self.publication(
                root_ref,
                logical_bytes=len(b"explicitly marked child"),
                edges=(ContentEdge(root_ref, child_ref, "tree-file", "child.txt"),),
            ),
        )
        self.ledger.abort_owner_change(
            operation_id=self.op("abort-marked"),
            actor_principal_id="alice",
            change_id=change.change_id,
        )

        epoch = self.ledger.start_gc_epoch(
            operation_id=self.op("mark-epoch"), store_id="store-a"
        )
        with self.assertRaises(RealmConflict):
            self.ledger.start_gc_epoch(
                operation_id=self.op("competing-mark-epoch"),
                store_id="store-a",
            )
        self.assertEqual(
            self.ledger.mark_gc_content(
                operation_id=self.op("mark-root"),
                store_id="store-a",
                epoch=epoch.epoch,
                content_refs=(root_ref,),
                reason="maintenance-pin",
            ),
            2,
        )
        self.assertEqual(
            self.ledger.finish_gc_epoch(
                operation_id=self.op("finish-marked"),
                store_id="store-a",
                epoch=epoch.epoch,
                grace_seconds=0,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
