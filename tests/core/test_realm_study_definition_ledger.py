from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import optpilot.realm.ledger as ledger_module
from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owner_derivation import (
    Binding,
    OwnerDerivationManifest,
    SourceAnchor,
)
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.run_closure import ScopePath
from optpilot.realm.study_definition import (
    STUDY_DEFINITION_OWNER_KIND,
    StudyDefinitionManifest,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


def _drop_table_triggers(connection: sqlite3.Connection, table: str) -> None:
    names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
        (table,),
    ).fetchall()
    for (name,) in names:
        connection.execute(f'DROP TRIGGER "{name}"')


class RealmStudyDefinitionLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.counter = 0
        for principal_id in ("operator", "delegate"):
            self.ledger.register_principal(
                operation_id=self.op(f"principal-{principal_id}"),
                principal_id=principal_id,
                kind="human",
            )
        self.ledger.register_store(
            operation_id=self.op("store"),
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            closure_bindings,
            self.source_owner_id,
            _source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="study-definition-ledger",
        )
        self.control = prepare_test_run_control_manifest(self.closure, max_trials=4)
        self.run_definition, self.definition_bindings = prepare_test_run_definition(
            self.closure,
            self.control,
            closure_bindings,
        )
        source_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=self.source_owner_id
        )
        self.assertEqual(len(source_memberships), 1)
        self.source_membership = source_memberships[0]
        self.source_anchor = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id=self.source_owner_id
        )
        self.derivation, self.manifest = self.build_definition("study-a")

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"study-definition-ledger/{self.counter}/{label}"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def build_definition(
        self,
        owner_id: str,
        *,
        source_anchor: SourceAnchor | None = None,
        source_role: str | None = None,
        run_definition=None,
    ) -> tuple[OwnerDerivationManifest, StudyDefinitionManifest]:
        selected_definition = (
            self.run_definition if run_definition is None else run_definition
        )
        derivation = OwnerDerivationManifest(
            target_owner_id=owner_id,
            target_owner_kind=STUDY_DEFINITION_OWNER_KIND,
            sources=(self.source_anchor if source_anchor is None else source_anchor,),
            bindings=tuple(
                Binding(
                    source_owner_id=self.source_owner_id,
                    source_store_id=binding.store_id,
                    content_ref=binding.content_ref,
                    source_role=(
                        self.source_membership.role
                        if source_role is None
                        else source_role
                    ),
                    target_role=binding.role,
                )
                for binding in self.definition_bindings
            ),
        )
        return derivation, StudyDefinitionManifest(
            owner_id=owner_id,
            owner_derivation_manifest_digest=derivation.digest,
            authored_study_config=ScopePath(
                self.closure.environment_revision.source_layers[0].scope,
                "study.yaml",
            ),
            run_definition=selected_definition,
        )

    def create_definition(
        self,
        *,
        operation_id: str = "study-definition/create",
        actor_principal_id: str = "operator",
        derivation: OwnerDerivationManifest | None = None,
        manifest: StudyDefinitionManifest | None = None,
    ):
        return self.ledger.create_study_definition(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            derivation=self.derivation if derivation is None else derivation,
            manifest=self.manifest if manifest is None else manifest,
        )

    def launch(
        self,
        *,
        operation_id: str = "study-definition/launch",
        actor_principal_id: str = "operator",
        controller_holder_id: str = "controller-a",
        controller_ttl_seconds: float = 600,
        study_definition_owner_id: str | None = None,
        expected_owner_revision: int = 0,
        expected_definition_digest: str | None = None,
        run_id: str = "run-a",
        owner_id: str = "run-owner-a",
    ):
        return self.ledger.create_run_from_study_definition(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            controller_holder_id=controller_holder_id,
            controller_ttl_seconds=controller_ttl_seconds,
            study_definition_owner_id=(
                self.manifest.owner_id
                if study_definition_owner_id is None
                else study_definition_owner_id
            ),
            expected_study_definition_owner_revision=expected_owner_revision,
            expected_run_definition_digest=(
                self.run_definition.digest
                if expected_definition_digest is None
                else expected_definition_digest
            ),
            run_id=run_id,
            owner_id=owner_id,
        )

    def assert_failed_target_rolled_back(
        self, *, owner_id: str, operation_id: str
    ) -> None:
        connection = self.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM owners WHERE owner_id = ?", (owner_id,)
                ).fetchone()[0],
                0,
            )
            for table, column in (
                ("owner_derivation_manifests", "target_owner_id"),
                ("study_definition_manifests", "owner_id"),
                ("study_definition_refs", "owner_id"),
            ):
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                            (owner_id,),
                        ).fetchone()[0],
                        0,
                    )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_create_read_replay_and_conflict_are_atomic_and_no_copy(self) -> None:
        before_objects = self.connect()
        try:
            content_object_count = before_objects.execute(
                "SELECT COUNT(*) FROM content_objects"
            ).fetchone()[0]
        finally:
            before_objects.close()
        live_refs = tuple(self.store.iter_live_refs())

        created = self.create_definition()
        self.assertEqual(self.create_definition(), created)
        self.assertEqual(created.manifest, self.manifest)
        self.assertEqual(
            self.ledger.read_study_definition(
                actor_principal_id="operator", owner_id=self.manifest.owner_id
            ),
            self.manifest,
        )
        self.assertEqual(
            set(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id=self.manifest.owner_id
                )
            ),
            set(self.definition_bindings),
        )

        connection = self.connect()
        try:
            creation_txn = connection.execute(
                "SELECT created_txn_id FROM study_definition_manifests "
                "WHERE owner_id = ?",
                (self.manifest.owner_id,),
            ).fetchone()[0]
            self.assertEqual(
                connection.execute(
                    "SELECT txn_id FROM owner_revisions "
                    "WHERE owner_id = ? AND revision = 0",
                    (self.manifest.owner_id,),
                ).fetchall(),
                [(creation_txn,)],
            )
            for table in (
                "owner_derivation_manifests",
                "owner_derivation_sources",
                "owner_derivation_bindings",
                "study_definition_refs",
            ):
                owner_column = (
                    "target_owner_id"
                    if table.startswith("owner_derivation")
                    else "owner_id"
                )
                with self.subTest(transaction_anchor=table):
                    self.assertEqual(
                        connection.execute(
                            f"SELECT DISTINCT created_txn_id FROM {table} "
                            f"WHERE {owner_column} = ?",
                            (self.manifest.owner_id,),
                        ).fetchall(),
                        [(creation_txn,)],
                    )
            self.assertEqual(
                connection.execute(
                    "SELECT semantic_role, store_id, content_ref "
                    "FROM study_definition_refs WHERE owner_id = ? "
                    "ORDER BY semantic_role, content_ref",
                    (self.manifest.owner_id,),
                ).fetchall(),
                [
                    (binding.role, binding.store_id, str(binding.content_ref))
                    for binding in self.definition_bindings
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT added_txn_id FROM owner_memberships "
                    "WHERE owner_id = ? AND added_revision = 0",
                    (self.manifest.owner_id,),
                ).fetchall(),
                [(creation_txn,)],
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ],
                content_object_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions "
                    "WHERE operation_id = 'study-definition/create'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()
        self.assertEqual(tuple(self.store.iter_live_refs()), live_refs)

        changed_definition = replace(
            self.run_definition, metadata={"variant": "changed"}
        )
        changed_derivation, changed_manifest = self.build_definition(
            self.manifest.owner_id, run_definition=changed_definition
        )
        with self.assertRaises(RealmConflict):
            self.create_definition(
                derivation=changed_derivation, manifest=changed_manifest
            )
        self.assertEqual(
            self.ledger.read_study_definition(
                actor_principal_id="operator", owner_id=self.manifest.owner_id
            ),
            self.manifest,
        )

    def test_equivalent_semantics_keep_independent_authorized_owners_no_copy(
        self,
    ) -> None:
        before_connection = self.connect()
        try:
            content_object_count = before_connection.execute(
                "SELECT COUNT(*) FROM content_objects"
            ).fetchone()[0]
        finally:
            before_connection.close()
        live_refs = tuple(self.store.iter_live_refs())

        first = self.create_definition(operation_id=self.op("first-equivalent"))
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate-source-derive"),
            actor_principal_id="operator",
            owner_id=self.source_owner_id,
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        current_source = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id=self.source_owner_id
        )
        second_derivation, second_manifest = self.build_definition(
            "study-equivalent-delegate", source_anchor=current_source
        )
        second = self.create_definition(
            operation_id=self.op("second-equivalent"),
            actor_principal_id="delegate",
            derivation=second_derivation,
            manifest=second_manifest,
        )

        self.assertNotEqual(first.owner.owner_id, second.owner.owner_id)
        self.assertEqual(first.owner.principal_id, "operator")
        self.assertEqual(second.owner.principal_id, "delegate")
        self.assertEqual(
            first.manifest.run_definition_digest,
            second.manifest.run_definition_digest,
        )
        self.assertNotEqual(first.manifest.digest, second.manifest.digest)
        with self.assertRaises(RealmNotFound):
            self.ledger.read_study_definition(
                actor_principal_id="delegate", owner_id=first.owner.owner_id
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_study_definition(
                actor_principal_id="operator", owner_id=second.owner.owner_id
            )

        connection = self.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT owner_id FROM study_definition_manifests "
                    "WHERE run_definition_digest = ? ORDER BY owner_id",
                    (self.run_definition.digest,),
                ).fetchall(),
                [(first.owner.owner_id,), (second.owner.owner_id,)],
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ],
                content_object_count,
            )
            index_rows = connection.execute(
                "PRAGMA index_list('study_definition_manifests')"
            ).fetchall()
            digest_index = next(
                row
                for row in index_rows
                if row[1] == "study_definition_manifests_run_definition_digest_index"
            )
            self.assertEqual(digest_index[2], 0)
        finally:
            connection.close()
        self.assertEqual(tuple(self.store.iter_live_refs()), live_refs)

    def test_source_acl_stale_anchor_and_wrong_role_fail_closed(self) -> None:
        unauthorized_derivation, unauthorized_manifest = self.build_definition(
            "study-unauthorized"
        )
        unauthorized_operation = self.op("unauthorized-create")
        with self.assertRaises(RealmNotFound):
            self.create_definition(
                operation_id=unauthorized_operation,
                actor_principal_id="delegate",
                derivation=unauthorized_derivation,
                manifest=unauthorized_manifest,
            )
        self.assert_failed_target_rolled_back(
            owner_id=unauthorized_manifest.owner_id,
            operation_id=unauthorized_operation,
        )

        stale_anchor = self.source_anchor
        self.ledger.grant_owner_permission(
            operation_id=self.op("advance-source-revision"),
            actor_principal_id="operator",
            owner_id=self.source_owner_id,
            principal_id="delegate",
            permission=OwnerPermission.METADATA_READ,
        )
        stale_derivation, stale_manifest = self.build_definition(
            "study-stale", source_anchor=stale_anchor
        )
        stale_operation = self.op("stale-create")
        with self.assertRaises(RealmConflict):
            self.create_definition(
                operation_id=stale_operation,
                derivation=stale_derivation,
                manifest=stale_manifest,
            )
        self.assert_failed_target_rolled_back(
            owner_id=stale_manifest.owner_id, operation_id=stale_operation
        )

        current_anchor = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id=self.source_owner_id
        )
        wrong_role_derivation, wrong_role_manifest = self.build_definition(
            "study-wrong-role",
            source_anchor=current_anchor,
            source_role="not-an-active-source-role",
        )
        wrong_role_operation = self.op("wrong-role-create")
        with self.assertRaises(RealmNotFound):
            self.create_definition(
                operation_id=wrong_role_operation,
                derivation=wrong_role_derivation,
                manifest=wrong_role_manifest,
            )
        self.assert_failed_target_rolled_back(
            owner_id=wrong_role_manifest.owner_id,
            operation_id=wrong_role_operation,
        )

    def test_sql_completeness_and_immutability_fail_closed(self) -> None:
        incomplete_derivation, _incomplete_manifest = self.build_definition(
            "incomplete-study"
        )
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES ('manual-incomplete-study', 'study-definition.create', ?, '{}', 1)",
                ("0" * 64,),
            ).lastrowid
            connection.execute(
                "INSERT INTO owner_derivation_manifests("
                "target_owner_id, target_owner_kind, target_owner_revision, "
                "manifest_digest, manifest_json, created_txn_id"
                ") VALUES (?, ?, 0, ?, ?, ?)",
                (
                    incomplete_derivation.target_owner_id,
                    incomplete_derivation.target_owner_kind,
                    incomplete_derivation.digest,
                    incomplete_derivation.to_bytes().decode("utf-8"),
                    txn_id,
                ),
            )
            connection.executemany(
                "INSERT INTO owner_derivation_sources("
                "target_owner_id, source_owner_id, source_owner_revision, "
                "source_owner_manifest_digest, created_txn_id"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        incomplete_derivation.target_owner_id,
                        source.owner_id,
                        source.owner_revision,
                        source.owner_manifest_digest,
                        txn_id,
                    )
                    for source in incomplete_derivation.sources
                ),
            )
            connection.executemany(
                "INSERT INTO owner_derivation_bindings("
                "target_owner_id, source_owner_id, source_store_id, content_ref, "
                "source_role, target_role, created_txn_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        incomplete_derivation.target_owner_id,
                        binding.source_owner_id,
                        binding.source_store_id,
                        str(binding.content_ref),
                        binding.source_role,
                        binding.target_role,
                        txn_id,
                    )
                    for binding in incomplete_derivation.bindings
                ),
            )
            connection.execute(
                "INSERT INTO owners("
                "owner_id, owner_kind, principal_id, revision, state, created_at, updated_at"
                ") VALUES ('incomplete-study', 'study-definition', 'operator', 0, "
                "'active', 1, 1)"
            )
            connection.executemany(
                "INSERT INTO owner_memberships("
                "owner_id, store_id, content_ref, role, added_revision, "
                "removed_revision, added_txn_id, removed_txn_id"
                ") VALUES ('incomplete-study', ?, ?, ?, 0, NULL, ?, NULL)",
                (
                    (
                        membership.store_id,
                        str(membership.content_ref),
                        membership.role,
                        txn_id,
                    )
                    for membership in incomplete_derivation.target_memberships
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO owner_revisions("
                    "owner_id, revision, txn_id, manifest_digest, created_at"
                    ") VALUES ('incomplete-study', 0, ?, ?, 1)",
                    (txn_id, "0" * 64),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM owners WHERE owner_id = 'incomplete-study'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

        self.create_definition()
        connection = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE study_definition_manifests "
                    "SET run_definition_digest = ? WHERE owner_id = ?",
                    ("0" * 64, self.manifest.owner_id),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM study_definition_refs WHERE owner_id = ?",
                    (self.manifest.owner_id,),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_read_rejects_tampered_normalized_refs(self) -> None:
        self.create_definition()
        connection = self.connect()
        try:
            _drop_table_triggers(connection, "study_definition_refs")
            connection.execute(
                "DELETE FROM study_definition_refs "
                "WHERE owner_id = ? AND semantic_role = ?",
                (self.manifest.owner_id, self.definition_bindings[0].role),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_study_definition(
                actor_principal_id="operator", owner_id=self.manifest.owner_id
            )

    def test_launch_uses_only_stored_semantics_and_exact_expected_anchors(self) -> None:
        self.create_definition()

        with self.assertRaises(RealmConflict):
            self.launch(
                operation_id=self.op("wrong-owner-revision"),
                expected_owner_revision=1,
            )
        with self.assertRaises(RealmConflict):
            self.launch(
                operation_id=self.op("wrong-definition-digest"),
                expected_definition_digest="0" * 64,
            )

        signature = inspect.signature(RealmLedger.create_run_from_study_definition)
        for client_semantics_parameter in (
            "run_definition",
            "definition_bindings",
            "source_owner_id",
        ):
            with self.subTest(parameter=client_semantics_parameter):
                self.assertNotIn(client_semantics_parameter, signature.parameters)
        with self.assertRaises(TypeError):
            self.ledger.create_run_from_study_definition(
                operation_id=self.op("client-semantics"),
                actor_principal_id="operator",
                controller_holder_id="controller-a",
                controller_ttl_seconds=600,
                study_definition_owner_id=self.manifest.owner_id,
                expected_study_definition_owner_revision=0,
                expected_run_definition_digest=self.run_definition.digest,
                run_id="run-a",
                owner_id="run-owner-a",
                run_definition=replace(
                    self.run_definition, metadata={"client": "override"}
                ),
            )

        connection = self.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM run_namespaces").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_launch_retains_exact_refs_in_an_independent_run_without_copy(self) -> None:
        self.create_definition()
        connection = self.connect()
        try:
            content_object_count = connection.execute(
                "SELECT COUNT(*) FROM content_objects"
            ).fetchone()[0]
        finally:
            connection.close()
        live_refs = tuple(self.store.iter_live_refs())

        launched = self.launch()
        self.assertNotEqual(launched.run.owner_id, self.manifest.owner_id)
        self.assertEqual(launched.definition_digest, self.run_definition.digest)
        self.assertEqual(
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id=launched.run.run_id
            ),
            self.run_definition,
        )
        self.assertEqual(
            set(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id=launched.run.owner_id
                )
            ),
            set(self.definition_bindings),
        )

        connection = self.connect()
        try:
            study_placements = connection.execute(
                "SELECT semantic_role, store_id, content_ref "
                "FROM study_definition_refs WHERE owner_id = ? "
                "ORDER BY semantic_role, store_id, content_ref",
                (self.manifest.owner_id,),
            ).fetchall()
            run_placements = connection.execute(
                "SELECT role, store_id, content_ref FROM owner_memberships "
                "WHERE owner_id = ? AND removed_revision IS NULL "
                "ORDER BY role, store_id, content_ref",
                (launched.run.owner_id,),
            ).fetchall()
            self.assertEqual(run_placements, study_placements)
            self.assertEqual(
                connection.execute(
                    "SELECT semantic_role, content_ref FROM run_definition_refs "
                    "WHERE run_id = ? ORDER BY semantic_role, content_ref",
                    (launched.run.run_id,),
                ).fetchall(),
                [
                    (role, str(content_ref))
                    for role, content_ref in self.run_definition.required_content_refs
                ],
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ],
                content_object_count,
            )
        finally:
            connection.close()
        self.assertEqual(tuple(self.store.iter_live_refs()), live_refs)

    def test_exact_launch_replays_after_definition_membership_and_state_change(
        self,
    ) -> None:
        self.create_definition()
        operation_id = self.op("launch-replay")
        launched = self.launch(operation_id=operation_id)
        change = self.ledger.begin_owner_change(
            operation_id=self.op("launch-replay-remove-begin"),
            actor_principal_id="operator",
            owner_id=self.manifest.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("launch-replay-remove-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(),
            removals=self.definition_bindings,
        )
        connection = self.connect()
        try:
            connection.execute(
                "UPDATE owners SET state = 'closed' WHERE owner_id = ?",
                (self.manifest.owner_id,),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(
            self.launch(operation_id=operation_id),
            launched,
        )

    def test_launch_replay_rejects_every_changed_request_anchor(self) -> None:
        self.create_definition()
        operation_id = self.op("launch-replay-mismatch")
        self.launch(operation_id=operation_id)

        mismatches = (
            {"actor_principal_id": "delegate"},
            {"controller_holder_id": "controller-b"},
            {"controller_ttl_seconds": 601},
            {"study_definition_owner_id": self.source_owner_id},
            {"expected_owner_revision": 1},
            {"expected_definition_digest": "0" * 64},
            {"run_id": "run-b"},
            {"owner_id": "run-owner-b"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with self.assertRaises(RealmConflict):
                    self.launch(operation_id=operation_id, **mismatch)

    def test_first_launch_still_fails_for_stale_or_missing_definition_state(
        self,
    ) -> None:
        self.create_definition()
        change = self.ledger.begin_owner_change(
            operation_id=self.op("first-launch-remove-begin"),
            actor_principal_id="operator",
            owner_id=self.manifest.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("first-launch-remove-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(),
            removals=self.definition_bindings,
        )

        with self.assertRaises(RealmConflict):
            self.launch(
                operation_id=self.op("first-launch-stale"),
                expected_owner_revision=0,
                run_id="run-stale",
                owner_id="run-owner-stale",
            )
        with self.assertRaises(RealmNotFound):
            self.launch(
                operation_id=self.op("first-launch-missing"),
                expected_owner_revision=1,
                run_id="run-missing",
                owner_id="run-owner-missing",
            )

    def test_removing_study_memberships_after_launch_does_not_invalidate_run(
        self,
    ) -> None:
        self.create_definition()
        launched = self.launch()
        change = self.ledger.begin_owner_change(
            operation_id=self.op("remove-study-memberships-begin"),
            actor_principal_id="operator",
            owner_id=self.manifest.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        committed = self.ledger.commit_owner_change(
            operation_id=self.op("remove-study-memberships-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(),
            removals=self.definition_bindings,
        )
        self.assertEqual(committed.owner_revision, 1)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id=self.manifest.owner_id
            ),
            (),
        )

        self.assertEqual(
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id=launched.run.run_id
            ),
            self.run_definition,
        )
        self.assertEqual(
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id=launched.run.run_id
            ).definition,
            self.run_definition,
        )
        self.assertEqual(
            set(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id=launched.run.owner_id
                )
            ),
            set(self.definition_bindings),
        )
        with self.assertRaises(RealmNotFound):
            self.launch(
                operation_id=self.op("launch-after-study-removal"),
                expected_owner_revision=1,
                run_id="run-b",
                owner_id="run-owner-b",
            )


class RealmStudyDefinitionMigrationTest(unittest.TestCase):
    def test_populated_v21_upgrades_without_changing_existing_owner(self) -> None:
        def authorize_owner_without_v26_authority_bindings(
            _ledger: RealmLedger,
            connection: sqlite3.Connection,
            *,
            actor_principal_id: str,
            owner_id: str,
            permissions,
        ) -> sqlite3.Row:
            del permissions
            row = connection.execute(
                "SELECT * FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None or row["principal_id"] != actor_principal_id:
                raise RealmNotFound("Owner not found.")
            return row

        def require_mutable_owner_without_v26_authority_bindings(
            _ledger: RealmLedger,
            connection: sqlite3.Connection,
            owner_id: str,
        ) -> None:
            if (
                connection.execute(
                    "SELECT 1 FROM owners WHERE owner_id = ?", (owner_id,)
                ).fetchone()
                is None
            ):
                raise RealmNotFound("Owner not found.")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "realm.sqlite3"
            store = LocalContentStore(root / "store", store_id="local-a")
            ledger: RealmLedger | None = None
            upgraded: RealmLedger | None = None
            try:
                with (
                    mock.patch.object(ledger_module, "_CURRENT_SCHEMA_VERSION", 21),
                    mock.patch.object(
                        ledger_module,
                        "_MIGRATIONS",
                        ledger_module._MIGRATIONS[:21],
                    ),
                ):
                    ledger = RealmLedger(database)
                ledger.register_principal(
                    operation_id="migration-v21/principal",
                    principal_id="operator",
                    kind="human",
                )
                ledger.register_store(
                    operation_id="migration-v21/store",
                    store_id=store.store_id,
                    backend_kind=store.BACKEND_KIND,
                    root_marker=store.root_marker,
                )
                with (
                    mock.patch.object(
                        RealmLedger,
                        "_authorize_owner_any",
                        authorize_owner_without_v26_authority_bindings,
                    ),
                    mock.patch.object(
                        RealmLedger,
                        "_require_mutable_owner",
                        require_mutable_owner_without_v26_authority_bindings,
                    ),
                ):
                    closure, closure_bindings, source_owner_id, _source_revision = (
                        prepare_test_run_closure(
                            ledger=ledger,
                            store=store,
                            root=root,
                            actor_principal_id="operator",
                            prefix="study-definition-v21-migration",
                        )
                    )
                    run_definition, definition_bindings = prepare_test_run_definition(
                        closure,
                        prepare_test_run_control_manifest(closure, max_trials=2),
                        closure_bindings,
                    )
                    source_membership = ledger.list_owner_memberships(
                        actor_principal_id="operator", owner_id=source_owner_id
                    )[0]
                    source_anchor = ledger.read_owner_source_anchor(
                        actor_principal_id="operator", owner_id=source_owner_id
                    )

                def build(owner_id: str):
                    derivation = OwnerDerivationManifest(
                        target_owner_id=owner_id,
                        target_owner_kind=STUDY_DEFINITION_OWNER_KIND,
                        sources=(source_anchor,),
                        bindings=tuple(
                            Binding(
                                source_owner_id=source_owner_id,
                                source_store_id=binding.store_id,
                                content_ref=binding.content_ref,
                                source_role=source_membership.role,
                                target_role=binding.role,
                            )
                            for binding in definition_bindings
                        ),
                    )
                    return derivation, StudyDefinitionManifest(
                        owner_id=owner_id,
                        owner_derivation_manifest_digest=derivation.digest,
                        authored_study_config=ScopePath(
                            closure.environment_revision.source_layers[0].scope,
                            "study.yaml",
                        ),
                        run_definition=run_definition,
                    )

                with (
                    mock.patch.object(
                        RealmLedger,
                        "_authorize_owner_any",
                        authorize_owner_without_v26_authority_bindings,
                    ),
                    mock.patch.object(
                        RealmLedger,
                        "_require_mutable_owner",
                        require_mutable_owner_without_v26_authority_bindings,
                    ),
                ):
                    derivation, manifest = build("study-definition-v21")
                    created = ledger.create_study_definition(
                        operation_id="migration-v21/create-definition",
                        actor_principal_id="operator",
                        derivation=derivation,
                        manifest=manifest,
                    )
                connection = sqlite3.connect(database)
                try:
                    content_object_count = connection.execute(
                        "SELECT COUNT(*) FROM content_objects"
                    ).fetchone()[0]
                finally:
                    connection.close()
                live_refs = tuple(store.iter_live_refs())
                ledger.close()
                ledger = None

                upgraded = RealmLedger(database)
                self.assertEqual(
                    upgraded.read_study_definition(
                        actor_principal_id="operator",
                        owner_id=created.owner.owner_id,
                    ),
                    created.manifest,
                )
                second_derivation, second_manifest = build("study-definition-after-v22")
                second = upgraded.create_study_definition(
                    operation_id="migration-v22/create-equivalent-definition",
                    actor_principal_id="operator",
                    derivation=second_derivation,
                    manifest=second_manifest,
                )
                self.assertNotEqual(created.owner.owner_id, second.owner.owner_id)
                self.assertEqual(
                    created.manifest.run_definition_digest,
                    second.manifest.run_definition_digest,
                )

                connection = sqlite3.connect(database)
                try:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        35,
                    )
                    self.assertEqual(
                        connection.execute("PRAGMA foreign_key_check").fetchall(),
                        [],
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM content_objects"
                        ).fetchone()[0],
                        content_object_count,
                    )
                    index_rows = connection.execute(
                        "PRAGMA index_list('study_definition_manifests')"
                    ).fetchall()
                    digest_index = next(
                        row
                        for row in index_rows
                        if row[1]
                        == "study_definition_manifests_run_definition_digest_index"
                    )
                    self.assertEqual(digest_index[2], 0)
                finally:
                    connection.close()
                self.assertEqual(tuple(store.iter_live_refs()), live_refs)
            finally:
                if ledger is not None:
                    ledger.close()
                if upgraded is not None:
                    upgraded.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
