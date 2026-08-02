from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunLogicalTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        self.ledger.register_principal(
            operation_id="principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="logical-transition",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=4)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.counter = 0
        self.admission = self._admit_trial()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"logical-transition/{self.counter}/{label}"

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _admit_trial(self):
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            (CandidateAdmission("candidate-a", envelope),),
            (LogicalTrialAdmission("trial-a", "candidate-a", seed=7),),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-admission"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        return self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )

    def cancel(
        self,
        *,
        operation_id: str,
        expected_run_revision: int,
        code: str = "admin_cancelled",
        controller_lease_id: str | None = None,
        controller_holder_id: str | None = None,
        controller_fencing_token: int | None = None,
    ):
        controller = self.created.controller_lease
        return self.ledger.cancel_run_logical_trial(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            expected_run_revision=expected_run_revision,
            controller_lease_id=(
                controller.lease_id
                if controller_lease_id is None
                else controller_lease_id
            ),
            controller_holder_id=(
                controller.holder_id
                if controller_holder_id is None
                else controller_holder_id
            ),
            controller_fencing_token=(
                controller.fencing_token
                if controller_fencing_token is None
                else controller_fencing_token
            ),
            code=code,
        )

    def test_public_api_has_no_generic_logical_transition_surface(self) -> None:
        self.assertFalse(hasattr(self.ledger, "transition_run_logical_trial"))
        controller = self.created.controller_lease
        with self.assertRaises(TypeError):
            self.ledger.cancel_run_logical_trial(
                operation_id=self.op("invalid-attempt-state"),
                actor_principal_id="operator",
                run_id="run-a",
                logical_trial_id="trial-a",
                expected_run_revision=1,
                controller_lease_id=controller.lease_id,
                controller_holder_id=controller.holder_id,
                controller_fencing_token=controller.fencing_token,
                code="admin_cancelled",
                to_state="queued",
            )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("accepted",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_cancel_before_dispatch_can_transition_directly_to_terminal(self) -> None:
        receipt = self.cancel(
            operation_id=self.op("cancel-before-dispatch"),
            expected_run_revision=1,
            code="user_cancelled",
        )

        self.assertEqual(receipt.transition.from_state, "accepted")
        self.assertEqual(receipt.transition.to_state, "terminal")
        self.assertEqual(receipt.transition.outcome, "cancelled")
        self.assertEqual(receipt.transition.code, "user_cancelled")
        self.assertIsNone(receipt.transition.attempt_id)
        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state, outcome, code, terminal, attempt_id, attempt "
                    "FROM run_events "
                    "WHERE run_id = 'run-a' AND sequence = 3"
                ).fetchone(),
                ("terminal", "cancelled", "user_cancelled", 1, None, None),
            )
            self.assertEqual(receipt.revision.operation_kind, "run.logical.cancel")
        finally:
            connection.close()

    def test_invalid_cancellation_codes_do_not_mutate(self) -> None:
        for index, code in enumerate((None, "", " admin_cancelled", "x" * 513)):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    self.cancel(
                        operation_id=self.op(f"invalid-values-{index}"),
                        expected_run_revision=1,
                        code=code,
                    )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("accepted",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision, next_sequence FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (1, 3),
            )
        finally:
            connection.close()

    def test_stale_revision_and_controller_fences_do_not_transition(self) -> None:
        stale_calls = (
            ({"expected_run_revision": 0}, RealmConflict),
            (
                {
                    "expected_run_revision": 1,
                    "controller_lease_id": "stale-lease",
                },
                RealmNotFound,
            ),
            (
                {
                    "expected_run_revision": 1,
                    "controller_holder_id": "stale-holder",
                },
                RealmConflict,
            ),
            (
                {
                    "expected_run_revision": 1,
                    "controller_fencing_token": (
                        self.created.controller_lease.fencing_token + 1
                    ),
                },
                RealmConflict,
            ),
        )
        for index, (overrides, error_type) in enumerate(stale_calls):
            with self.subTest(overrides=overrides):
                with self.assertRaises(error_type):
                    self.cancel(
                        operation_id=self.op(f"stale-{index}"),
                        code="admin_cancelled",
                        **overrides,
                    )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("accepted",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_revisions WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_exact_operation_replays_without_duplicate_history(self) -> None:
        operation_id = self.op("replay")
        receipt = self.cancel(
            operation_id=operation_id,
            expected_run_revision=1,
            code="admin_cancelled",
        )
        self.assertEqual(
            self.cancel(
                operation_id=operation_id,
                expected_run_revision=1,
                code="admin_cancelled",
            ),
            receipt,
        )
        with self.assertRaises(RealmConflict):
            self.cancel(
                operation_id=operation_id,
                expected_run_revision=1,
                code="user_cancelled",
            )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = 'run-a'"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_revisions WHERE run_id = 'run-a'"
                ).fetchone()[0],
                3,
            )
        finally:
            connection.close()

    def test_raw_sql_cannot_fabricate_attempt_lifecycle_transitions(self) -> None:
        cases = (
            ("queued", None, None, None),
            ("running", None, None, None),
            ("retrying", None, "retry_requested", "attempt-1"),
            ("terminal", "success", "evaluated", "attempt-1"),
            ("terminal", "cancelled", "admin_cancelled", "attempt-1"),
        )
        connection = self.connection()
        try:
            for index, (to_state, outcome, code, attempt_id) in enumerate(cases):
                with self.subTest(to_state=to_state, outcome=outcome):
                    connection.execute("SAVEPOINT fabricated_transition")
                    txn_id = connection.execute(
                        "INSERT INTO ledger_transactions("
                        "operation_id, operation_kind, request_digest, receipt_json, "
                        "committed_at) VALUES (?, 'run.logical.cancel', "
                        "'fixture', '{}', ?)",
                        (
                            self.op(f"raw-fabricated-{index}"),
                            self.admission.revision.created_at + index + 1,
                        ),
                    ).lastrowid
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError,
                        "run logical transition requires its open domain transaction",
                    ):
                        connection.execute(
                            "INSERT INTO run_logical_trial_transitions("
                            "run_id, logical_trial_id, transition_index, from_state, "
                            "to_state, outcome, code, attempt_id, sequence, "
                            "run_revision, txn_id, created_at) VALUES ("
                            "'run-a', 'trial-a', 2, 'accepted', ?, ?, ?, ?, "
                            "3, 2, ?, ?)",
                            (
                                to_state,
                                outcome,
                                code,
                                attempt_id,
                                txn_id,
                                self.admission.revision.created_at + index + 1,
                            ),
                        )
                    connection.execute("ROLLBACK TO fabricated_transition")
                    connection.execute("RELEASE fabricated_transition")

            legacy_txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, "
                "committed_at) VALUES (?, 'run.logical.transition', "
                "'fixture', '{}', ?)",
                (
                    self.op("raw-legacy-operation"),
                    self.admission.revision.created_at + len(cases) + 1,
                ),
            ).lastrowid
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run logical transition requires its open domain transaction",
            ):
                connection.execute(
                    "INSERT INTO run_logical_trial_transitions("
                    "run_id, logical_trial_id, transition_index, from_state, "
                    "to_state, outcome, code, attempt_id, sequence, "
                    "run_revision, txn_id, created_at) VALUES ("
                    "'run-a', 'trial-a', 2, 'accepted', 'terminal', "
                    "'cancelled', 'admin_cancelled', NULL, 3, 2, ?, ?)",
                    (
                        legacy_txn_id,
                        self.admission.revision.created_at + len(cases) + 1,
                    ),
                )
            connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("accepted",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.rollback()
            connection.close()

    def test_raw_state_mutation_replace_and_history_rewrites_are_rejected(self) -> None:
        self.cancel(
            operation_id=self.op("cancelled"),
            expected_run_revision=1,
            code="admin_cancelled",
        )
        connection = self.connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE run_logical_trials SET state = 'accepted' "
                    "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a'"
                )
            connection.rollback()

            row = connection.execute(
                "SELECT * FROM run_logical_trials "
                "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a'"
            ).fetchone()
            assert row is not None
            placeholders = ", ".join("?" for _ in row)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT OR REPLACE INTO run_logical_trials VALUES ({placeholders})",
                    row,
                )
            connection.rollback()

            transition_row = connection.execute(
                "SELECT * FROM run_logical_trial_transitions "
                "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a' "
                "AND transition_index = 2"
            ).fetchone()
            assert transition_row is not None
            transition_placeholders = ", ".join("?" for _ in transition_row)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT OR REPLACE INTO run_logical_trial_transitions "
                    f"VALUES ({transition_placeholders})",
                    transition_row,
                )
            connection.rollback()

            for table in (
                "run_logical_trial_transitions",
                "run_events",
                "run_revisions",
            ):
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"UPDATE {table} SET created_at = created_at")
                    connection.rollback()
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"DELETE FROM {table}")
                    connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("terminal",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_terminal_trial_cannot_append_another_terminal_transition(self) -> None:
        terminal = self.cancel(
            operation_id=self.op("terminal-cancelled"),
            expected_run_revision=1,
            code="user_cancelled",
        )
        connection = self.connection()
        try:
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, "
                "committed_at) VALUES (?, 'run.logical.cancel', "
                "'fixture', '{}', ?)",
                (self.op("terminal-fixture"), terminal.transition.created_at + 1),
            ).lastrowid
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run logical transition requires its open domain transaction",
            ):
                connection.execute(
                    "INSERT INTO run_logical_trial_transitions("
                    "run_id, logical_trial_id, transition_index, from_state, "
                    "to_state, outcome, code, attempt_id, sequence, "
                    "run_revision, txn_id, created_at) VALUES ("
                    "'run-a', 'trial-a', 3, 'terminal', 'terminal', 'success', "
                    "'changed', NULL, 4, 3, ?, ?)",
                    (txn_id, terminal.transition.created_at + 1),
                )
            connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT state, outcome, code FROM run_logical_trials "
                    "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a'"
                ).fetchone(),
                ("terminal", "cancelled", "user_cancelled"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions "
                    "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_admin_revision_rejects_fabricated_attempt_event_fields(self) -> None:
        connection = self.connection()
        try:
            created_at = self.admission.revision.created_at + 1
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, "
                "committed_at) VALUES (?, 'run.logical.cancel', "
                "'fixture', '{}', ?)",
                (self.op("forged-attempt-event"), created_at),
            ).lastrowid
            connection.execute(
                "INSERT INTO run_logical_trial_transitions("
                "run_id, logical_trial_id, transition_index, from_state, "
                "to_state, outcome, code, attempt_id, sequence, run_revision, "
                "txn_id, created_at) VALUES ("
                "'run-a', 'trial-a', 2, 'accepted', 'terminal', 'cancelled', "
                "'admin_cancelled', NULL, 3, 2, ?, ?)",
                (txn_id, created_at),
            )
            connection.execute(
                "INSERT INTO run_events("
                "run_id, sequence, event_id, schema_version, producer, event, "
                "phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, "
                "txn_id, created_at, attempt_id, attempt) VALUES ("
                "'run-a', 3, 'forged-attempt-event', 'optpilot.run-event.v1', "
                "'controller', 'logical_trial_transitioned', 'evaluation', "
                "'terminal', 'cancelled', 'admin_cancelled', 1, 'candidate-a', "
                "'trial-a', NULL, ?, 2, ?, ?, 'fabricated-attempt', 1)",
                (
                    '{"attempt_id":null,"from_state":"accepted",'
                    '"to_state":"terminal","transition_index":2}',
                    txn_id,
                    created_at,
                ),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run logical transition revision is inconsistent",
            ):
                connection.execute(
                    "INSERT INTO run_revisions("
                    "run_id, revision, owner_revision, last_sequence, "
                    "next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, "
                    "created_at) VALUES ('run-a', 2, 0, 3, 4, 1, 1, ?, ?, "
                    "'run.logical.cancel', ?, ?)",
                    (
                        self.created.controller_lease.lease_id,
                        self.created.controller_lease.fencing_token,
                        txn_id,
                        created_at,
                    ),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM run_logical_trials WHERE run_id = 'run-a'"
                ).fetchone(),
                ("accepted",),
            )
        finally:
            connection.rollback()
            connection.close()

    def test_logical_transition_revision_requires_exactly_one_event(self) -> None:
        connection = self.connection()
        try:
            created_at = self.admission.revision.created_at + 1
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, "
                "committed_at) VALUES (?, 'run.logical.cancel', "
                "'fixture', '{}', ?)",
                (self.op("two-event-fixture"), created_at),
            ).lastrowid
            connection.execute(
                "INSERT INTO run_logical_trial_transitions("
                "run_id, logical_trial_id, transition_index, from_state, "
                "to_state, outcome, code, attempt_id, sequence, run_revision, "
                "txn_id, created_at) VALUES ("
                "'run-a', 'trial-a', 2, 'accepted', 'terminal', 'cancelled', "
                "'admin_cancelled', "
                "NULL, 3, 2, ?, ?)",
                (txn_id, created_at),
            )
            payload = (
                '{"attempt_id":null,"from_state":"accepted",'
                '"to_state":"terminal","transition_index":2}'
            )
            for sequence in (3, 4):
                connection.execute(
                    "INSERT INTO run_events("
                    "run_id, sequence, event_id, schema_version, producer, event, "
                    "phase, state, outcome, code, terminal, candidate_id, "
                    "logical_trial_id, session_handle, payload_json, run_revision, "
                    "txn_id, created_at) VALUES ("
                    "'run-a', ?, ?, 'optpilot.run-event.v1', 'controller', "
                    "'logical_trial_transitioned', 'evaluation', 'terminal', "
                    "'cancelled', 'admin_cancelled', 1, 'candidate-a', "
                    "'trial-a', NULL, ?, 2, ?, ?)",
                    (
                        sequence,
                        f"fixture-event-{sequence}",
                        payload,
                        txn_id,
                        created_at,
                    ),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run logical transition revision is inconsistent",
            ):
                connection.execute(
                    "INSERT INTO run_revisions("
                    "run_id, revision, owner_revision, last_sequence, "
                    "next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, "
                    "created_at) VALUES ('run-a', 2, 0, 4, 5, 1, 1, ?, ?, "
                    "'run.logical.cancel', ?, ?)",
                    (
                        self.created.controller_lease.lease_id,
                        self.created.controller_lease.fencing_token,
                        txn_id,
                        created_at,
                    ),
                )
            connection.rollback()

            self.assertEqual(
                connection.execute(
                    "SELECT state, outcome, code FROM run_logical_trials "
                    "WHERE run_id = 'run-a' AND logical_trial_id = 'trial-a'"
                ).fetchone(),
                ("accepted", None, None),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_logical_trial_transitions"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision, next_sequence FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (1, 3),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
