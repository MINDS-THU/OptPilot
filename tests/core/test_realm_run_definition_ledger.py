from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_definition import RUN_METHOD_SOURCE_ROLE
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
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


def _downgrade_current_shape_to_v7(database: Path) -> None:
    """Reconstruct schema v7 so the safety migration can be replayed."""

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        # These v7 trigger names are deliberately replaced by later migrations.
        # Keep their current definitions long enough for the corresponding
        # DROP TRIGGER statements to replay; the empty downgraded fixture does
        # not execute domain writes between migrations.
        replay_replaced_v7_triggers = {
            "run_submission_control_insert_guard",
            "run_attempt_revision_consistency",
            "run_attempt_transition_insert_guard",
            "run_logical_transition_requires_open_transaction",
            "run_control_revision_consistency",
            "run_finish_terminal_policy_consistency",
        }
        for name, sql in triggers:
            if name in replay_replaced_v7_triggers:
                continue
            normalized = (sql or "").lower()
            if name in {
                "run_attempt_loss_derived_control_consistency",
                "run_stop_escalation_revision_consistency",
            } or any(
                marker in normalized
                for marker in (
                    "run_definition",
                    "method_revision",
                    "prepared_method_runtime",
                    "owner_derivation",
                    "study_definition",
                    "completed_tree_capture",
                    "ephemeral_volume",
                    "run_attempt_execution",
                    "run_method_exchange",
                    "operator_job",
                    "operator_capacity",
                    "interface_output",
                    "run_terminal_seal",
                )
            ):
                connection.execute(f'DROP TRIGGER "{name}"')
        for table in (
            "run_terminal_seals",
            "interface_output_generations",
            "interface_output_capture_attempts",
            "interface_output_sessions",
            "operator_capacity_reservations",
            "operator_capacity_fence_counters",
            "operator_capacity_pools",
            "operator_job_cleanup_receipts",
            "operator_job_results",
            "operator_job_outcomes",
            "operator_job_stop_requests",
            "operator_job_launch_intents",
            "operator_job_approvals",
            "operator_job_revisions",
            "operator_jobs",
            "run_method_exchange_completions",
            "run_method_exchange_preparations",
            "run_attempt_execution_cleanup_authorizations",
            "run_attempt_execution_terminal_evidence",
            "run_attempt_execution_launch_intents",
            "run_attempt_execution_volumes",
            "run_attempt_execution_projections",
            "run_attempt_execution_bindings",
            "ephemeral_volumes",
            "ephemeral_volume_roots",
            "completed_tree_capture_publications",
            "completed_tree_captures",
            "study_definition_refs",
            "study_definition_manifests",
            "owner_derivation_bindings",
            "owner_derivation_sources",
            "owner_derivation_manifests",
            "run_definition_refs",
            "run_definition_manifests",
            "prepared_method_runtimes",
            "method_revisions",
            "run_definition_roles",
        ):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute(
            "DELETE FROM run_revision_kinds "
            "WHERE operation_kind IN ("
            "'run.attempt.bind', 'run.attempt.reconcile', "
            "'run.method.exchange.prepare', 'run.method.observation.ack', "
            "'run.control.escalate')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
        connection.execute(
            "UPDATE realm_meta SET value = '7' WHERE key = 'schema_version'"
        )
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()


class RealmRunDefinitionLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="definition/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="definition/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            closure_bindings,
            self.source_owner_id,
            self.source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="definition",
        )
        self.control = prepare_test_run_control_manifest(self.closure, max_trials=4)
        self.definition, self.definition_bindings = prepare_test_run_definition(
            self.closure,
            self.control,
            closure_bindings,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_run(
        self,
        *,
        operation_id: str = "definition/run-create",
        definition=None,
        bindings=None,
    ):
        return self.ledger.create_run_namespace(
            operation_id=operation_id,
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=self.definition if definition is None else definition,
            definition_bindings=(
                self.definition_bindings if bindings is None else bindings
            ),
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )

    def test_creation_is_atomic_exactly_anchored_and_replays_exactly(self) -> None:
        created = self._create_run()
        self.assertEqual(self._create_run(), created)

        connection = self._connect()
        try:
            creation_txn = connection.execute(
                "SELECT created_txn_id FROM run_namespaces WHERE run_id = 'run-a'"
            ).fetchone()[0]
            anchored_rows = {
                "owner revision": connection.execute(
                    "SELECT txn_id FROM owner_revisions WHERE owner_id = 'run-owner-a'"
                ).fetchall(),
                "run revision": connection.execute(
                    "SELECT txn_id FROM run_revisions "
                    "WHERE run_id = 'run-a' AND revision = 0"
                ).fetchall(),
                "evaluation closure": connection.execute(
                    "SELECT created_txn_id FROM run_evaluation_templates "
                    "WHERE run_id = 'run-a'"
                ).fetchall(),
                "run control": connection.execute(
                    "SELECT created_txn_id FROM run_control_manifests "
                    "WHERE run_id = 'run-a'"
                ).fetchall(),
                "method revision": connection.execute(
                    "SELECT created_txn_id FROM method_revisions "
                    "WHERE revision_digest = ?",
                    (self.definition.method_revision.digest,),
                ).fetchall(),
                "method runtime": connection.execute(
                    "SELECT created_txn_id FROM prepared_method_runtimes "
                    "WHERE runtime_digest = ?",
                    (self.definition.prepared_method_runtime.digest,),
                ).fetchall(),
                "run definition": connection.execute(
                    "SELECT created_txn_id FROM run_definition_manifests "
                    "WHERE run_id = 'run-a'"
                ).fetchall(),
            }
            for label, rows in anchored_rows.items():
                with self.subTest(anchor=label):
                    self.assertEqual(rows, [(creation_txn,)])

            persisted_refs = connection.execute(
                "SELECT semantic_role, content_ref, created_txn_id "
                "FROM run_definition_refs WHERE run_id = 'run-a' "
                "ORDER BY semantic_role, content_ref"
            ).fetchall()
            self.assertEqual(
                persisted_refs,
                [
                    (role, str(content_ref), creation_txn)
                    for role, content_ref in self.definition.required_content_refs
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT role, content_ref, added_txn_id "
                    "FROM owner_memberships WHERE owner_id = 'run-owner-a' "
                    "ORDER BY role, content_ref"
                ).fetchall(),
                [
                    (binding.role, str(binding.content_ref), creation_txn)
                    for binding in self.definition_bindings
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions "
                    "WHERE operation_id = 'definition/run-create'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_reads_return_the_exact_definition_including_snapshot(self) -> None:
        self._create_run()

        self.assertEqual(
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            ),
            self.definition,
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.definition, self.definition)
        self.assertEqual(
            snapshot.control.manifest, self.definition.run_control_manifest
        )

    def test_missing_method_binding_rolls_back_every_run_record(self) -> None:
        incomplete = tuple(
            binding
            for binding in self.definition_bindings
            if binding.role != RUN_METHOD_SOURCE_ROLE
        )
        with self.assertRaisesRegex(ValueError, "definition|semantic content ref"):
            self._create_run(bindings=incomplete)

        connection = self._connect()
        try:
            for table in (
                "run_namespaces",
                "method_revisions",
                "prepared_method_runtimes",
                "run_definition_manifests",
                "run_definition_refs",
            ):
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                            0
                        ],
                        0,
                    )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM owners WHERE owner_id = 'run-owner-a'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions "
                    "WHERE operation_id = 'definition/run-create'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_same_operation_with_changed_definition_conflicts(self) -> None:
        self._create_run()
        changed = replace(self.definition, metadata={"variant": "changed"})

        with self.assertRaises(RealmConflict):
            self._create_run(definition=changed)

        self.assertEqual(
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            ),
            self.definition,
        )

    def test_read_rejects_tampered_normalized_closure(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "run_evaluation_templates")
            connection.execute(
                "UPDATE run_evaluation_templates SET closure_json = '{}' "
                "WHERE run_id = 'run-a'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_read_rejects_tampered_normalized_control(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "run_control_manifests")
            connection.execute(
                "UPDATE run_control_manifests SET manifest_digest = ? "
                "WHERE run_id = 'run-a'",
                ("0" * 64,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_read_rejects_tampered_normalized_method(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "method_revisions")
            connection.execute(
                "UPDATE method_revisions SET method_id = 'tampered-method' "
                "WHERE revision_digest = ?",
                (self.definition.method_revision.digest,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_read_rejects_tampered_normalized_method_runtime(self) -> None:
        self._create_run()
        changed_runtime = replace(
            self.definition.prepared_method_runtime,
            runtime_settings={"python": "tampered"},
        )
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "prepared_method_runtimes")
            connection.execute(
                "UPDATE prepared_method_runtimes SET manifest_json = ? "
                "WHERE runtime_digest = ?",
                (
                    changed_runtime.to_bytes().decode("utf-8"),
                    self.definition.prepared_method_runtime.digest,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_read_rejects_tampered_definition_anchor(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "run_definition_manifests")
            connection.execute(
                "UPDATE run_definition_manifests "
                "SET definition_digest = ? WHERE run_id = 'run-a'",
                ("0" * 64,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_read_rejects_tampered_definition_refs(self) -> None:
        self._create_run()
        connection = self._connect()
        try:
            _drop_table_triggers(connection, "run_definition_refs")
            connection.execute(
                "DELETE FROM run_definition_refs "
                "WHERE run_id = 'run-a' AND semantic_role = ?",
                (RUN_METHOD_SOURCE_ROLE,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            )

    def test_retirement_releases_definition_roles_but_preserves_definition(
        self,
    ) -> None:
        created = self._create_run()
        closed = self.ledger.close_run_submissions(
            operation_id="definition/close",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            stop_code="method_completed",
        )
        finished = self.ledger.finish_run(
            operation_id="definition/finish",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=closed.run.current_revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        change = self.ledger.begin_owner_change(
            operation_id="definition/retire-begin",
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        retired = self.ledger.retire_run(
            operation_id="definition/retire",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=finished.run.current_revision,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=change.change_id,
        )

        self.assertEqual(retired.run.retention_state, "retired")
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id=created.run.owner_id
            ),
            (),
        )
        self.assertEqual(
            self.ledger.read_run_definition(
                actor_principal_id="operator", run_id="run-a"
            ),
            self.definition,
        )
        self.assertEqual(
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id="run-a"
            ).definition,
            self.definition,
        )

        connection = self._connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT role, content_ref FROM owner_memberships "
                    "WHERE owner_id = 'run-owner-a' AND removed_revision IS NOT NULL "
                    "ORDER BY role, content_ref"
                ).fetchall(),
                [
                    (binding.role, str(binding.content_ref))
                    for binding in self.definition_bindings
                ],
            )
        finally:
            connection.close()

    def test_nonempty_v7_realm_refuses_unsafe_definition_migration(self) -> None:
        self._create_run()
        self.ledger.close()
        _downgrade_current_shape_to_v7(self.database)

        with self.assertRaisesRegex(
            RealmIntegrityError, "cannot infer complete method semantics"
        ):
            RealmLedger(self.database)


class RealmRunDefinitionMigrationTest(unittest.TestCase):
    def test_empty_v7_realm_upgrades_to_current_v37(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "realm.sqlite3"
            migration_directory = (
                Path(__file__).resolve().parents[2]
                / "src"
                / "optpilot"
                / "realm"
                / "migrations"
            )
            connection = sqlite3.connect(database)
            try:
                for version in range(1, 8):
                    path = next(migration_directory.glob(f"{version:04d}_*.sql"))
                    payload = path.read_bytes()
                    connection.executescript(payload.decode("utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations("
                        "version, migration_digest, applied_at) VALUES (?, ?, ?)",
                        (version, hashlib.sha256(payload).hexdigest(), float(version)),
                    )
                    if version == 1:
                        connection.executemany(
                            "INSERT INTO realm_meta(key, value) VALUES (?, ?)",
                            (("realm_id", "migration-test"), ("schema_version", "1")),
                        )
                    else:
                        connection.execute(
                            "UPDATE realm_meta SET value = ? "
                            "WHERE key = 'schema_version'",
                            (str(version),),
                        )
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
            finally:
                connection.close()

            upgraded = RealmLedger(database)
            upgraded.close()
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 37
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM realm_meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "37",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                    [(version,) for version in range(1, 38)],
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
