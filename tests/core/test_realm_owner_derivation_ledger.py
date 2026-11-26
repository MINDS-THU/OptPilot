from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedFileSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owner_derivation import (
    Binding,
    OwnerDerivationManifest,
    SourceAnchor,
)
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import BlobRef
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


class RealmOwnerDerivationLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.counter = 0
        for principal in ("operator", "delegate", "observer"):
            self.ledger.register_principal(
                operation_id=self.op(f"principal-{principal}"),
                principal_id=principal,
                kind="human",
            )
        self.ledger.register_store(
            operation_id=self.op("store"),
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.source_memberships: dict[str, OwnerMembership] = {}
        for owner_id, role, payload in (
            ("source-a", "source-a-root", b"alpha source\n"),
            ("source-b", "source-b-root", b"beta source\n"),
        ):
            self.ledger.create_owner(
                operation_id=self.op(f"create-{owner_id}"),
                owner_id=owner_id,
                owner_kind="resource",
                principal_id="operator",
            )
            self.source_memberships[owner_id] = self.publish_source(
                owner_id=owner_id, role=role, payload=payload
            )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"owner-derivation/{self.counter}/{label}"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def table_count(self, table: str) -> int:
        if table not in {"content_objects", "owners", "owner_edges"}:
            raise AssertionError("unsupported test table")
        connection = self.connect()
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()

    def publish_source(
        self, *, owner_id: str, role: str, payload: bytes
    ) -> OwnerMembership:
        source = self.root / f"{owner_id}.bin"
        source.write_bytes(payload)
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
        sealed = capture.seal_blob(
            source=AllowedFileSource(self.root, source.name)
        )
        membership = OwnerMembership(self.store.store_id, sealed.blob_ref, role)
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

    def manifest(
        self,
        target_owner_id: str,
        *,
        source_ids: tuple[str, ...] = ("source-a",),
        actor: str = "operator",
        anchors: dict[str, SourceAnchor] | None = None,
        source_roles: dict[str, str] | None = None,
    ) -> OwnerDerivationManifest:
        selected_anchors = anchors or {
            owner_id: self.ledger.read_owner_source_anchor(
                actor_principal_id=actor, owner_id=owner_id
            )
            for owner_id in source_ids
        }
        roles = source_roles or {}
        return OwnerDerivationManifest(
            target_owner_id=target_owner_id,
            target_owner_kind="workspace",
            sources=tuple(selected_anchors[owner_id] for owner_id in source_ids),
            bindings=tuple(
                Binding(
                    source_owner_id=owner_id,
                    source_store_id=self.store.store_id,
                    content_ref=self.source_memberships[owner_id].content_ref,
                    source_role=roles.get(
                        owner_id, self.source_memberships[owner_id].role
                    ),
                    target_role=f"imported-{owner_id}",
                )
                for owner_id in source_ids
            ),
        )

    def remove_source_membership(self, owner_id: str) -> None:
        revision = self.ledger.read_owner(
            actor_principal_id="operator", owner_id=owner_id
        ).revision
        change = self.ledger.begin_owner_change(
            operation_id=self.op(f"remove-begin-{owner_id}"),
            actor_principal_id="operator",
            owner_id=owner_id,
            expected_owner_revision=revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op(f"remove-commit-{owner_id}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=revision,
            additions=(),
            removals=(self.source_memberships[owner_id],),
        )

    def test_single_and_multiple_source_derivations_are_atomic_and_no_copy(self) -> None:
        before_objects = self.table_count("content_objects")
        before_refs = tuple(self.store.iter_live_refs())

        single = self.manifest("derived-single")
        single_receipt = self.ledger.derive_owner(
            operation_id=self.op("derive-single"),
            actor_principal_id="operator",
            manifest=single,
        )
        multiple = self.manifest(
            "derived-multiple", source_ids=("source-b", "source-a")
        )
        multiple_receipt = self.ledger.derive_owner(
            operation_id=self.op("derive-multiple"),
            actor_principal_id="operator",
            manifest=multiple,
        )

        self.assertEqual(single_receipt.manifest, single)
        self.assertEqual(multiple_receipt.manifest, multiple)
        self.assertEqual(single_receipt.owner.revision, 0)
        self.assertEqual(multiple_receipt.owner.revision, 0)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="derived-single"
            ),
            single.target_memberships,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="derived-multiple"
            ),
            multiple.target_memberships,
        )
        self.assertEqual(self.table_count("content_objects"), before_objects)
        self.assertEqual(tuple(self.store.iter_live_refs()), before_refs)
        self.assertEqual(self.table_count("owner_edges"), 0)

        connection = self.connect()
        try:
            creation_txn = connection.execute(
                "SELECT created_txn_id FROM owner_derivation_manifests "
                "WHERE target_owner_id = 'derived-multiple'"
            ).fetchone()[0]
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT created_txn_id FROM owner_derivation_sources "
                    "WHERE target_owner_id = 'derived-multiple'"
                ).fetchall(),
                [(creation_txn,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT created_txn_id FROM owner_derivation_bindings "
                    "WHERE target_owner_id = 'derived-multiple'"
                ).fetchall(),
                [(creation_txn,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT txn_id FROM owner_revisions "
                    "WHERE owner_id = 'derived-multiple' AND revision = 0"
                ).fetchone()[0],
                creation_txn,
            )
        finally:
            connection.close()

    def test_source_anchor_and_derivation_permissions_are_explicit(self) -> None:
        with self.assertRaises(RealmNotFound):
            self.ledger.read_owner_source_anchor(
                actor_principal_id="delegate", owner_id="source-a"
            )

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-delegate-derive"),
            actor_principal_id="operator",
            owner_id="source-a",
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_owner_source_anchor(
                actor_principal_id="delegate", owner_id="source-a"
            )
        delegated = self.manifest("delegated-target", actor="operator")
        receipt = self.ledger.derive_owner(
            operation_id=self.op("delegate-derive"),
            actor_principal_id="delegate",
            manifest=delegated,
        )
        self.assertEqual(receipt.owner.principal_id, "delegate")

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-delegate-metadata"),
            actor_principal_id="operator",
            owner_id="source-a",
            principal_id="delegate",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(
            self.ledger.read_owner_source_anchor(
                actor_principal_id="delegate", owner_id="source-a"
            ),
            self.ledger.read_owner_source_anchor(
                actor_principal_id="operator", owner_id="source-a"
            ),
        )

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-observer-metadata"),
            actor_principal_id="operator",
            owner_id="source-b",
            principal_id="observer",
            permission=OwnerPermission.METADATA_READ,
        )
        observer_manifest = self.manifest(
            "observer-target", source_ids=("source-b",), actor="observer"
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.derive_owner(
                operation_id=self.op("observer-cannot-derive"),
                actor_principal_id="observer",
                manifest=observer_manifest,
            )

    def test_exact_replay_survives_acl_revocation_and_changed_request_conflicts(self) -> None:
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-derive"),
            actor_principal_id="operator",
            owner_id="source-a",
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        manifest = self.manifest("replayed-target")
        operation_id = self.op("delegated-derive")
        first = self.ledger.derive_owner(
            operation_id=operation_id,
            actor_principal_id="delegate",
            manifest=manifest,
        )
        self.ledger.revoke_owner_permission(
            operation_id=self.op("revoke-derive"),
            actor_principal_id="operator",
            owner_id="source-a",
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )

        self.assertEqual(
            self.ledger.derive_owner(
                operation_id=operation_id,
                actor_principal_id="delegate",
                manifest=manifest,
            ),
            first,
        )
        changed = OwnerDerivationManifest(
            target_owner_id=manifest.target_owner_id,
            target_owner_kind=manifest.target_owner_kind,
            sources=manifest.sources,
            bindings=tuple(
                Binding(
                    item.source_owner_id,
                    item.source_store_id,
                    item.content_ref,
                    item.source_role,
                    "changed-target-role",
                )
                for item in manifest.bindings
            ),
        )
        with self.assertRaises(RealmConflict):
            self.ledger.derive_owner(
                operation_id=operation_id,
                actor_principal_id="delegate",
                manifest=changed,
            )
        unavailable = OwnerDerivationManifest(
            "new-target-after-revoke",
            "workspace",
            manifest.sources,
            manifest.bindings,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.derive_owner(
                operation_id=self.op("new-after-revoke"),
                actor_principal_id="delegate",
                manifest=unavailable,
            )

    def test_removed_source_membership_preserves_anchor_replay_and_target(self) -> None:
        manifest = self.manifest("independent-target")
        operation_id = self.op("derive-independent")
        receipt = self.ledger.derive_owner(
            operation_id=operation_id,
            actor_principal_id="operator",
            manifest=manifest,
        )
        before_objects = self.table_count("content_objects")
        self.remove_source_membership("source-a")

        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="source-a"
            ),
            (),
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="independent-target"
            ),
            manifest.target_memberships,
        )
        self.assertEqual(
            self.ledger.read_owner_derivation(
                actor_principal_id="operator", owner_id="independent-target"
            ),
            manifest,
        )
        self.assertEqual(
            self.ledger.read_owner_source_anchor(
                actor_principal_id="operator",
                owner_id="source-a",
                revision=manifest.sources[0].owner_revision,
            ),
            manifest.sources[0],
        )
        self.assertEqual(
            self.ledger.derive_owner(
                operation_id=operation_id,
                actor_principal_id="operator",
                manifest=manifest,
            ),
            receipt,
        )
        self.assertEqual(self.table_count("content_objects"), before_objects)
        self.assertIn(
            self.source_memberships["source-a"].content_ref,
            tuple(self.store.iter_live_refs()),
        )

        stale = OwnerDerivationManifest(
            "stale-after-removal", "workspace", manifest.sources, manifest.bindings
        )
        with self.assertRaises(RealmConflict):
            self.ledger.derive_owner(
                operation_id=self.op("stale-after-removal"),
                actor_principal_id="operator",
                manifest=stale,
            )
        current = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id="source-a"
        )
        missing_membership = OwnerDerivationManifest(
            "missing-after-removal",
            "workspace",
            (current,),
            manifest.bindings,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.derive_owner(
                operation_id=self.op("missing-after-removal"),
                actor_principal_id="operator",
                manifest=missing_membership,
            )

    def test_rejects_stale_or_wrong_anchors_roles_refs_and_unauthorized_sources(self) -> None:
        current = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id="source-a"
        )
        historical = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id="source-a", revision=0
        )
        binding = self.manifest("template").bindings[0]
        cases = (
            (
                "stale-revision",
                (historical,),
                (binding,),
                RealmConflict,
            ),
            (
                "wrong-digest",
                (SourceAnchor("source-a", current.owner_revision, "0" * 64),),
                (binding,),
                RealmConflict,
            ),
            (
                "missing-owner",
                (SourceAnchor("missing-source", 0, "1" * 64),),
                (
                    Binding(
                        "missing-source",
                        self.store.store_id,
                        binding.content_ref,
                        binding.source_role,
                        binding.target_role,
                    ),
                ),
                RealmNotFound,
            ),
            (
                "wrong-role",
                (current,),
                (
                    Binding(
                        "source-a",
                        self.store.store_id,
                        binding.content_ref,
                        "missing-source-role",
                        binding.target_role,
                    ),
                ),
                RealmNotFound,
            ),
            (
                "missing-ref",
                (current,),
                (
                    Binding(
                        "source-a",
                        self.store.store_id,
                        BlobRef.from_bytes(b"not retained"),
                        binding.source_role,
                        binding.target_role,
                    ),
                ),
                RealmNotFound,
            ),
        )
        for label, sources, bindings, error_type in cases:
            with self.subTest(case=label):
                invalid = OwnerDerivationManifest(
                    f"invalid-{label}", "workspace", sources, bindings
                )
                with self.assertRaises(error_type):
                    self.ledger.derive_owner(
                        operation_id=self.op(label),
                        actor_principal_id="operator",
                        manifest=invalid,
                    )

        unauthorized = self.manifest("unauthorized-target")
        with self.assertRaises(RealmNotFound):
            self.ledger.derive_owner(
                operation_id=self.op("unauthorized"),
                actor_principal_id="delegate",
                manifest=unauthorized,
            )

    def test_provenance_read_acl_and_cross_table_tamper_detection(self) -> None:
        manifest = self.manifest("provenance-target")
        self.ledger.derive_owner(
            operation_id=self.op("derive-provenance"),
            actor_principal_id="operator",
            manifest=manifest,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_owner_derivation(
                actor_principal_id="observer", owner_id="provenance-target"
            )
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-provenance-read"),
            actor_principal_id="operator",
            owner_id="provenance-target",
            principal_id="observer",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(
            self.ledger.read_owner_derivation(
                actor_principal_id="observer", owner_id="provenance-target"
            ),
            manifest,
        )

        connection = self.connect()
        try:
            connection.execute(
                "DROP TRIGGER owner_derivation_binding_update_immutable"
            )
            connection.execute(
                "UPDATE owner_derivation_bindings SET source_role = 'tampered-role' "
                "WHERE target_owner_id = 'provenance-target'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_owner_derivation(
                actor_principal_id="observer", owner_id="provenance-target"
            )

    def test_concurrent_derivation_and_source_removal_serialize_safely(self) -> None:
        manifest = self.manifest("raced-target")
        revision = manifest.sources[0].owner_revision
        change = self.ledger.begin_owner_change(
            operation_id=self.op("race-remove-begin"),
            actor_principal_id="operator",
            owner_id="source-a",
            expected_owner_revision=revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        barrier = threading.Barrier(3)
        derived = []
        removed = []
        errors: list[BaseException] = []

        def derive() -> None:
            barrier.wait()
            try:
                derived.append(
                    self.ledger.derive_owner(
                        operation_id=self.op("race-derive"),
                        actor_principal_id="operator",
                        manifest=manifest,
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def remove() -> None:
            barrier.wait()
            try:
                removed.append(
                    self.ledger.commit_owner_change(
                        operation_id=self.op("race-remove-commit"),
                        actor_principal_id="operator",
                        change_id=change.change_id,
                        expected_owner_revision=revision,
                        additions=(),
                        removals=(self.source_memberships["source-a"],),
                    )
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = (threading.Thread(target=derive), threading.Thread(target=remove))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(removed), 1)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id="source-a"
            ),
            (),
        )
        if derived:
            self.assertEqual(errors, [])
            self.assertEqual(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id="raced-target"
                ),
                manifest.target_memberships,
            )
            self.assertEqual(
                self.ledger.read_owner_derivation(
                    actor_principal_id="operator", owner_id="raced-target"
                ),
                manifest,
            )
        else:
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RealmConflict)
            with self.assertRaises(RealmNotFound):
                self.ledger.read_owner(
                    actor_principal_id="operator", owner_id="raced-target"
                )

    def _raw_transaction(self, connection: sqlite3.Connection, label: str) -> int:
        return int(
            connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'owner.derive', ?, '{}', 1.0)",
                (f"raw/{label}", "f" * 64),
            ).lastrowid
        )

    @staticmethod
    def _raw_insert_manifest(
        connection: sqlite3.Connection,
        manifest: OwnerDerivationManifest,
        txn_id: int,
    ) -> None:
        connection.execute(
            "INSERT INTO owner_derivation_manifests("
            "target_owner_id, target_owner_kind, manifest_digest, manifest_json, created_txn_id"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                manifest.target_owner_id,
                manifest.target_owner_kind,
                manifest.digest,
                manifest.to_bytes().decode("utf-8"),
                txn_id,
            ),
        )

    @staticmethod
    def _raw_insert_sources(
        connection: sqlite3.Connection,
        manifest: OwnerDerivationManifest,
        txn_id: int,
    ) -> None:
        connection.executemany(
            "INSERT INTO owner_derivation_sources("
            "target_owner_id, source_owner_id, source_owner_revision, "
            "source_owner_manifest_digest, created_txn_id"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                (
                    manifest.target_owner_id,
                    source.owner_id,
                    source.owner_revision,
                    source.owner_manifest_digest,
                    txn_id,
                )
                for source in manifest.sources
            ),
        )

    @staticmethod
    def _raw_insert_bindings(
        connection: sqlite3.Connection,
        manifest: OwnerDerivationManifest,
        txn_id: int,
    ) -> None:
        connection.executemany(
            "INSERT INTO owner_derivation_bindings("
            "target_owner_id, source_owner_id, source_store_id, content_ref, "
            "source_role, target_role, created_txn_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    manifest.target_owner_id,
                    binding.source_owner_id,
                    binding.source_store_id,
                    str(binding.content_ref),
                    binding.source_role,
                    binding.target_role,
                    txn_id,
                )
                for binding in manifest.bindings
            ),
        )

    @staticmethod
    def _raw_insert_target(
        connection: sqlite3.Connection,
        manifest: OwnerDerivationManifest,
        txn_id: int,
        memberships: tuple[OwnerMembership, ...],
    ) -> None:
        connection.execute(
            "INSERT INTO owners("
            "owner_id, owner_kind, principal_id, revision, state, created_at, updated_at"
            ") VALUES (?, ?, 'operator', 0, 'active', 1.0, 1.0)",
            (manifest.target_owner_id, manifest.target_owner_kind),
        )
        connection.executemany(
            "INSERT INTO owner_memberships("
            "owner_id, store_id, content_ref, role, added_revision, removed_revision, "
            "added_txn_id, removed_txn_id"
            ") VALUES (?, ?, ?, ?, 0, NULL, ?, NULL)",
            (
                (
                    manifest.target_owner_id,
                    membership.store_id,
                    str(membership.content_ref),
                    membership.role,
                    txn_id,
                )
                for membership in memberships
            ),
        )

    @staticmethod
    def _raw_insert_revision(
        connection: sqlite3.Connection,
        manifest: OwnerDerivationManifest,
        txn_id: int,
    ) -> None:
        connection.execute(
            "INSERT INTO owner_revisions("
            "owner_id, revision, txn_id, manifest_digest, created_at"
            ") VALUES (?, 0, ?, ?, 1.0)",
            (manifest.target_owner_id, txn_id, "e" * 64),
        )

    def test_direct_sql_cannot_omit_or_alter_any_derivation_plan_part(self) -> None:
        connection = self.connect()
        try:
            # No source or binding rows may be omitted even when the target
            # membership happens to match the JSON declaration.
            manifest = self.manifest("raw-missing-source")
            connection.execute("BEGIN IMMEDIATE")
            txn_id = self._raw_transaction(connection, "missing-source")
            self._raw_insert_manifest(connection, manifest, txn_id)
            self._raw_insert_target(
                connection, manifest, txn_id, manifest.target_memberships
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exact sources"):
                self._raw_insert_revision(connection, manifest, txn_id)
            connection.rollback()

            manifest = self.manifest("raw-missing-binding")
            connection.execute("BEGIN IMMEDIATE")
            txn_id = self._raw_transaction(connection, "missing-binding")
            self._raw_insert_manifest(connection, manifest, txn_id)
            self._raw_insert_sources(connection, manifest, txn_id)
            self._raw_insert_target(
                connection, manifest, txn_id, manifest.target_memberships
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exact sources"):
                self._raw_insert_revision(connection, manifest, txn_id)
            connection.rollback()

            manifest = self.manifest("raw-missing-target-membership")
            connection.execute("BEGIN IMMEDIATE")
            txn_id = self._raw_transaction(connection, "missing-target-membership")
            self._raw_insert_manifest(connection, manifest, txn_id)
            self._raw_insert_sources(connection, manifest, txn_id)
            self._raw_insert_bindings(connection, manifest, txn_id)
            self._raw_insert_target(connection, manifest, txn_id, ())
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exact sources"):
                self._raw_insert_revision(connection, manifest, txn_id)
            connection.rollback()

            manifest = self.manifest("raw-altered-plan")
            connection.execute("BEGIN IMMEDIATE")
            txn_id = self._raw_transaction(connection, "altered-plan")
            self._raw_insert_manifest(connection, manifest, txn_id)
            self._raw_insert_sources(connection, manifest, txn_id)
            self._raw_insert_bindings(connection, manifest, txn_id)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "source is immutable"):
                connection.execute(
                    "UPDATE owner_derivation_sources SET source_owner_manifest_digest = ? "
                    "WHERE target_owner_id = ?",
                    ("0" * 64, manifest.target_owner_id),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "binding is immutable"):
                connection.execute(
                    "UPDATE owner_derivation_bindings SET target_role = 'altered' "
                    "WHERE target_owner_id = ?",
                    (manifest.target_owner_id,),
                )
            connection.rollback()

            manifest = self.manifest("raw-altered-target-membership")
            connection.execute("BEGIN IMMEDIATE")
            txn_id = self._raw_transaction(connection, "altered-target-membership")
            self._raw_insert_manifest(connection, manifest, txn_id)
            self._raw_insert_sources(connection, manifest, txn_id)
            self._raw_insert_bindings(connection, manifest, txn_id)
            wrong = tuple(
                OwnerMembership(item.store_id, item.content_ref, "altered-target-role")
                for item in manifest.target_memberships
            )
            self._raw_insert_target(connection, manifest, txn_id, wrong)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exact sources"):
                self._raw_insert_revision(connection, manifest, txn_id)
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
