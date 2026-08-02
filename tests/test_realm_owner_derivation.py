from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.owner_derivation import (
    MAX_DERIVATION_BINDINGS,
    MAX_DERIVATION_SOURCES,
    Binding,
    OwnerDerivationManifest,
    SourceAnchor,
)
from optpilot.realm.refs import BlobRef, CandidateRef, SnapshotRef


MIGRATION_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "optpilot"
    / "realm"
    / "migrations"
)


def _manifest(*, target_owner_id: str = "target-owner") -> OwnerDerivationManifest:
    alpha_ref = BlobRef.from_bytes(b"alpha")
    beta_ref = SnapshotRef("b" * 64)
    return OwnerDerivationManifest(
        target_owner_id=target_owner_id,
        target_owner_kind="workspace",
        # Deliberately reversed: construction must canonicalize order.
        sources=(
            SourceAnchor("source-z", 3, "d" * 64),
            SourceAnchor("source-a", 0, "a" * 64),
        ),
        bindings=(
            Binding("source-z", "store-z", beta_ref, "output", "workspace-root"),
            Binding("source-a", "store-a", alpha_ref, "artifact", "supporting-file"),
        ),
    )


class OwnerDerivationRecordTest(unittest.TestCase):
    def test_manifest_is_canonical_immutable_and_round_trips_exactly(self) -> None:
        manifest = _manifest()
        self.assertEqual(
            tuple(source.owner_id for source in manifest.sources),
            ("source-a", "source-z"),
        )
        self.assertEqual(
            tuple(binding.source_owner_id for binding in manifest.bindings),
            ("source-a", "source-z"),
        )
        self.assertEqual(OwnerDerivationManifest.from_bytes(manifest.to_bytes()), manifest)
        self.assertEqual(
            OwnerDerivationManifest.from_dict(manifest.to_dict()), manifest
        )
        self.assertEqual(manifest.manifest_digest, manifest.digest)
        self.assertEqual(
            [membership.role for membership in manifest.target_memberships],
            ["supporting-file", "workspace-root"],
        )

        reordered = OwnerDerivationManifest(
            target_owner_id=manifest.target_owner_id,
            target_owner_kind=manifest.target_owner_kind,
            sources=tuple(reversed(manifest.sources)),
            bindings=tuple(reversed(manifest.bindings)),
        )
        self.assertEqual(reordered.to_bytes(), manifest.to_bytes())
        self.assertEqual(reordered.digest, manifest.digest)

    def test_manifest_requires_an_exact_one_to_one_source_mapping(self) -> None:
        source = SourceAnchor("source-a", 0, "a" * 64)
        binding = Binding(
            "source-a",
            "store-a",
            BlobRef.from_bytes(b"payload"),
            "artifact",
            "workspace-root",
        )
        with self.assertRaisesRegex(ValueError, "equal the binding source owners"):
            OwnerDerivationManifest(
                "target", "workspace", (source, SourceAnchor("unused", 0, "b" * 64)), (binding,)
            )
        with self.assertRaisesRegex(ValueError, "cannot also be"):
            OwnerDerivationManifest(
                "source-a", "workspace", (source,), (binding,)
            )
        with self.assertRaisesRegex(ValueError, "exactly one source membership"):
            OwnerDerivationManifest(
                "target",
                "workspace",
                (source,),
                (
                    binding,
                    Binding(
                        "source-a",
                        "store-a",
                        binding.content_ref,
                        "another-source-role",
                        "workspace-root",
                    ),
                ),
            )

        candidate = CandidateRef.build(
            candidate_format="parameters", spec={"x": 1}, content_refs=[]
        )
        with self.assertRaisesRegex(ValueError, "physical blob or tree"):
            Binding(
                "source-a",
                "store-a",
                candidate,  # type: ignore[arg-type]
                "candidate",
                "workspace-root",
            )

    def test_persisted_shape_and_count_bounds_are_strict(self) -> None:
        manifest = _manifest()
        value = manifest.to_dict()
        value["unexpected"] = True
        with self.assertRaises(RealmIntegrityError):
            OwnerDerivationManifest.from_dict(value)
        with self.assertRaisesRegex(RealmIntegrityError, "canonical JSON"):
            OwnerDerivationManifest.from_bytes(b'{"schema": "not-canonical"}')

        too_many_sources = tuple(
            SourceAnchor(f"source-{index}", 0, f"{index:064x}")
            for index in range(MAX_DERIVATION_SOURCES + 1)
        )
        with self.assertRaisesRegex(ValueError, "maximum count"):
            OwnerDerivationManifest(
                "target",
                "workspace",
                too_many_sources,
                (
                    Binding(
                        "source-0",
                        "store-a",
                        BlobRef.from_bytes(b"payload"),
                        "source",
                        "target",
                    ),
                ),
            )

        source = SourceAnchor("source", 0, "a" * 64)
        content_ref = BlobRef.from_bytes(b"shared")
        too_many_bindings = tuple(
            Binding(
                "source",
                "store-a",
                content_ref,
                "source",
                f"target-{index}",
            )
            for index in range(MAX_DERIVATION_BINDINGS + 1)
        )
        with self.assertRaisesRegex(ValueError, "maximum count"):
            OwnerDerivationManifest(
                "target", "workspace", (source,), too_many_bindings
            )


class OwnerDerivationSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in sorted(MIGRATION_DIRECTORY.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))
        self._seed_source()

    def tearDown(self) -> None:
        self.connection.close()

    def _transaction(self, operation_id: str, operation_kind: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ledger_transactions("
            "operation_id, operation_kind, request_digest, receipt_json, committed_at"
            ") VALUES (?, ?, ?, '{}', 1.0)",
            (operation_id, operation_kind, "0" * 64),
        )
        return int(cursor.lastrowid)

    def _seed_source(self) -> None:
        txn_id = self._transaction("source/create", "owner.create")
        self.connection.execute(
            "INSERT INTO principals(principal_id, kind, created_at) "
            "VALUES ('operator', 'human', 1.0)"
        )
        self.connection.execute(
            "INSERT INTO stores(store_id, backend_kind, root_marker, state, created_at) "
            "VALUES ('store-a', 'local-cas', 'marker-a', 'active', 1.0)"
        )
        self.content_ref = BlobRef.from_bytes(b"source payload")
        self.connection.execute(
            "INSERT INTO content_objects("
            "store_id, content_ref, kind, digest, logical_bytes, physical_bytes, "
            "lifecycle_state, trust_state, metadata_json, created_at, verified_at"
            ") VALUES ('store-a', ?, 'blob', ?, 14, 14, 'live', "
            "'verified_local', '{}', 1.0, 1.0)",
            (str(self.content_ref), self.content_ref.digest),
        )
        self.connection.execute(
            "INSERT INTO owners("
            "owner_id, owner_kind, principal_id, revision, state, created_at, updated_at"
            ") VALUES ('source-owner', 'run', 'operator', 0, 'active', 1.0, 1.0)"
        )
        self.connection.execute(
            "INSERT INTO owner_memberships("
            "owner_id, store_id, content_ref, role, added_revision, removed_revision, "
            "added_txn_id, removed_txn_id"
            ") VALUES ('source-owner', 'store-a', ?, 'artifact', 0, NULL, ?, NULL)",
            (str(self.content_ref), txn_id),
        )
        self.connection.execute(
            "INSERT INTO owner_revisions("
            "owner_id, revision, txn_id, manifest_digest, created_at"
            ") VALUES ('source-owner', 0, ?, ?, 1.0)",
            (txn_id, "a" * 64),
        )
        self.connection.commit()

    def _schema_manifest(self, target_owner_id: str = "target-owner") -> OwnerDerivationManifest:
        return OwnerDerivationManifest(
            target_owner_id=target_owner_id,
            target_owner_kind="workspace",
            sources=(SourceAnchor("source-owner", 0, "a" * 64),),
            bindings=(
                Binding(
                    "source-owner",
                    "store-a",
                    self.content_ref,
                    "artifact",
                    "workspace-root",
                ),
            ),
        )

    def _insert_plan(self, manifest: OwnerDerivationManifest, txn_id: int) -> None:
        self.connection.execute(
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
        self.connection.executemany(
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
        self.connection.executemany(
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

    def _insert_target(
        self,
        manifest: OwnerDerivationManifest,
        txn_id: int,
        *,
        include_membership: bool = True,
    ) -> None:
        self.connection.execute(
            "INSERT INTO owners("
            "owner_id, owner_kind, principal_id, revision, state, created_at, updated_at"
            ") VALUES (?, ?, 'operator', 0, 'active', 2.0, 2.0)",
            (manifest.target_owner_id, manifest.target_owner_kind),
        )
        if include_membership:
            self.connection.executemany(
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
                    for membership in manifest.target_memberships
                ),
            )
        self.connection.execute(
            "INSERT INTO owner_revisions("
            "owner_id, revision, txn_id, manifest_digest, created_at"
            ") VALUES (?, 0, ?, ?, 2.0)",
            (manifest.target_owner_id, txn_id, "f" * 64),
        )

    def test_derivation_commits_no_copy_provenance_and_exact_target_memberships(self) -> None:
        manifest = self._schema_manifest()
        content_count = self.connection.execute(
            "SELECT COUNT(*) FROM content_objects"
        ).fetchone()[0]
        self.connection.execute("BEGIN")
        txn_id = self._transaction("derive/valid", "owner.derive")
        self._insert_plan(manifest, txn_id)
        self._insert_target(manifest, txn_id)
        self.connection.commit()

        self.assertEqual(
            self.connection.execute(
                "SELECT source_owner_id, source_owner_revision "
                "FROM owner_derivation_sources WHERE target_owner_id = ?",
                (manifest.target_owner_id,),
            ).fetchall(),
            [("source-owner", 0)],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT store_id, content_ref, role FROM owner_memberships "
                "WHERE owner_id = ?",
                (manifest.target_owner_id,),
            ).fetchall(),
            [("store-a", str(self.content_ref), "workspace-root")],
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0],
            content_count,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM owner_edges").fetchone()[0],
            0,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "UPDATE owner_derivation_manifests SET target_owner_kind = 'run' "
                "WHERE target_owner_id = ?",
                (manifest.target_owner_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "DELETE FROM owner_derivation_bindings WHERE target_owner_id = ?",
                (manifest.target_owner_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.connection.execute(
                "DELETE FROM owner_memberships WHERE owner_id = 'source-owner'"
            )

    def test_revision_zero_gate_rejects_missing_or_extra_target_memberships(self) -> None:
        for suffix, include_membership in (("missing", False),):
            with self.subTest(case=suffix):
                manifest = self._schema_manifest(f"target-{suffix}")
                self.connection.execute("BEGIN")
                txn_id = self._transaction(f"derive/{suffix}", "owner.derive")
                self._insert_plan(manifest, txn_id)
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "exact sources, bindings"
                ):
                    self._insert_target(
                        manifest, txn_id, include_membership=include_membership
                    )
                self.connection.rollback()

        manifest = self._schema_manifest("target-extra")
        extra_ref = BlobRef.from_bytes(b"extra")
        self.connection.execute("BEGIN")
        txn_id = self._transaction("derive/extra", "owner.derive")
        self.connection.execute(
            "INSERT INTO content_objects("
            "store_id, content_ref, kind, digest, logical_bytes, physical_bytes, "
            "lifecycle_state, trust_state, metadata_json, created_at, verified_at"
            ") VALUES ('store-a', ?, 'blob', ?, 5, 5, 'live', "
            "'verified_local', '{}', 2.0, 2.0)",
            (str(extra_ref), extra_ref.digest),
        )
        self._insert_plan(manifest, txn_id)
        self.connection.execute(
            "INSERT INTO owners("
            "owner_id, owner_kind, principal_id, revision, state, created_at, updated_at"
            ") VALUES (?, 'workspace', 'operator', 0, 'active', 2.0, 2.0)",
            (manifest.target_owner_id,),
        )
        for store_id, content_ref, role in (
            ("store-a", str(self.content_ref), "workspace-root"),
            ("store-a", str(extra_ref), "unplanned"),
        ):
            self.connection.execute(
                "INSERT INTO owner_memberships("
                "owner_id, store_id, content_ref, role, added_revision, removed_revision, "
                "added_txn_id, removed_txn_id"
                ") VALUES (?, ?, ?, ?, 0, NULL, ?, NULL)",
                (manifest.target_owner_id, store_id, content_ref, role, txn_id),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "exact sources, bindings"):
            self.connection.execute(
                "INSERT INTO owner_revisions("
                "owner_id, revision, txn_id, manifest_digest, created_at"
                ") VALUES (?, 0, ?, ?, 2.0)",
                (manifest.target_owner_id, txn_id, "f" * 64),
            )
        self.connection.rollback()

    def test_plan_rejects_wrong_operation_stale_anchor_and_source_role(self) -> None:
        manifest = self._schema_manifest("target-wrong-operation")
        self.connection.execute("BEGIN")
        txn_id = self._transaction("derive/wrong-operation", "owner.create")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "authorized domain operation"
        ):
            self._insert_plan(manifest, txn_id)
        self.connection.rollback()

        manifest = self._schema_manifest("target-wrong-role")
        value = manifest.to_dict()
        value["bindings"][0]["source_role"] = "absent-role"
        invalid_binding_manifest = OwnerDerivationManifest.from_dict(value)
        self.connection.execute("BEGIN")
        txn_id = self._transaction("derive/wrong-role", "owner.derive")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "source membership"):
            self._insert_plan(invalid_binding_manifest, txn_id)
        self.connection.rollback()

        self.connection.execute(
            "INSERT INTO owner_revisions("
            "owner_id, revision, txn_id, manifest_digest, created_at"
            ") VALUES ('source-owner', 1, NULL, ?, 3.0)",
            ("b" * 64,),
        )
        self.connection.execute(
            "UPDATE owners SET revision = 1, updated_at = 3.0 "
            "WHERE owner_id = 'source-owner'"
        )
        self.connection.commit()
        manifest = self._schema_manifest("target-stale")
        self.connection.execute("BEGIN")
        txn_id = self._transaction("derive/stale", "owner.derive")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "stale or invalid"):
            self._insert_plan(manifest, txn_id)
        self.connection.rollback()


if __name__ == "__main__":
    unittest.main()
