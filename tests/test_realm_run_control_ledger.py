from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
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


class RealmRunControlLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        for principal in ("operator", "other"):
            self.ledger.register_principal(
                operation_id=f"run-control/principal/{principal}",
                principal_id=principal,
                kind="human",
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="run-control/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            self.closure_bindings,
            self.source_owner_id,
            self.source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="run-control",
        )
        self.manifest = prepare_test_run_control_manifest(
            self.closure, max_trials=3
        )
        self.created = self.create_run()
        self.counter = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"run-control/{self.counter}/{label}"

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_run(
        self,
        *,
        operation_id: str = "run-control/run/create",
        run_id: str = "run-a",
        owner_id: str = "run-owner-a",
        manifest=None,
    ):
        selected_manifest = self.manifest if manifest is None else manifest
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, selected_manifest, self.closure_bindings
        )
        return self.ledger.create_run_namespace(
            operation_id=operation_id,
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id=run_id,
            owner_id=owner_id,
        )

    def close_submissions(
        self,
        *,
        operation_id: str,
        expected_run_revision: int = 0,
        stop_code: str = "method_completed",
    ):
        return self.ledger.close_run_submissions(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            stop_code=stop_code,
        )

    def finish(
        self,
        *,
        operation_id: str,
        expected_run_revision: int,
        terminal_state: str | None = None,
        code: str | None = None,
    ):
        return self.ledger.finish_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            terminal_state=terminal_state,
            code=code,
        )

    def escalate_stop(
        self,
        *,
        operation_id: str,
        expected_run_revision: int,
        stop_code: str = "protocol_error",
    ):
        return self.ledger.escalate_run_stop(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=(
                self.created.controller_lease.fencing_token
            ),
            stop_code=stop_code,
        )

    @staticmethod
    def parameter_plan() -> RunAdmissionPlan:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        return RunAdmissionPlan(
            (CandidateAdmission("candidate-a", envelope),),
            (LogicalTrialAdmission("trial-a", "candidate-a", seed=7),),
        )

    def test_creation_persists_canonical_manifest_and_initial_accepting_state(self) -> None:
        replay = self.create_run()
        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )

        self.assertEqual(replay, self.created)
        self.assertEqual(snapshot.manifest, self.manifest)
        self.assertEqual(snapshot.manifest.digest, self.manifest.digest)
        self.assertEqual(len(snapshot.submission_records), 1)
        initial = snapshot.current_submission
        self.assertEqual(initial.state, "accepting")
        self.assertEqual(initial.run_revision, 0)
        self.assertEqual(initial.manifest_digest, self.manifest.digest)
        self.assertIsNone(initial.previous_record_digest)
        self.assertEqual(self.created.run.max_trials, self.manifest.max_trials)

        connection = self.connection()
        try:
            manifest_row = connection.execute(
                "SELECT manifest_digest, manifest_json, created_txn_id "
                "FROM run_control_manifests WHERE run_id = 'run-a'"
            ).fetchone()
            initial_row = connection.execute(
                "SELECT control_index, record_digest, record_json, txn_id "
                "FROM run_submission_control_records WHERE run_id = 'run-a'"
            ).fetchone()
            self.assertEqual(
                manifest_row,
                (
                    self.manifest.digest,
                    self.manifest.to_bytes().decode("utf-8"),
                    self.created.revision.txn_id,
                ),
            )
            self.assertEqual(
                initial_row,
                (
                    0,
                    initial.digest,
                    initial.to_bytes().decode("utf-8"),
                    self.created.revision.txn_id,
                ),
            )
        finally:
            connection.close()

    def test_read_run_control_obeys_run_owner_acl(self) -> None:
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_control(
                actor_principal_id="other", run_id=self.created.run.run_id
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_control(
                actor_principal_id="operator", run_id="missing-run"
            )

    def test_create_rejects_manifest_that_disagrees_with_evaluation_closure(self) -> None:
        mismatches = (
            replace(self.manifest, candidate_contract_digest="0" * 64),
            replace(self.manifest, objective_metric="loss"),
            replace(self.manifest, objective_direction="minimize"),
        )
        for index, manifest in enumerate(mismatches, start=1):
            with self.subTest(manifest=manifest):
                with self.assertRaises(ValueError):
                    self.create_run(
                        operation_id=f"run-control/mismatch/{index}",
                        run_id=f"mismatch-run-{index}",
                        owner_id=f"mismatch-owner-{index}",
                        manifest=manifest,
                    )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_namespaces WHERE run_id LIKE 'mismatch-run-%'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_control_manifests "
                    "WHERE run_id LIKE 'mismatch-run-%'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_run_controller_compiler_version_is_not_the_environment_compiler(self) -> None:
        manifest = replace(
            self.manifest, compiler_version="run-controller-compiler-v2"
        )
        created = self.create_run(
            operation_id="run-control/distinct-compiler/create",
            run_id="distinct-compiler-run",
            owner_id="distinct-compiler-owner",
            manifest=manifest,
        )

        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=created.run.run_id
        )
        self.assertEqual(snapshot.manifest, manifest)
        self.assertEqual(
            snapshot.manifest.compiler_version, "run-controller-compiler-v2"
        )
        self.assertEqual(
            self.closure.environment_revision.compiler_version, "1"
        )

    def test_close_appends_draining_once_and_replays_exactly(self) -> None:
        operation_id = self.op("close")
        closed = self.close_submissions(
            operation_id=operation_id, stop_code="wall_clock_budget"
        )
        replay = self.close_submissions(
            operation_id=operation_id, stop_code="wall_clock_budget"
        )

        self.assertEqual(replay, closed)
        self.assertEqual(closed.control_index, 1)
        self.assertEqual(closed.record.state, "draining")
        self.assertEqual(closed.record.stop_code, "wall_clock_budget")
        self.assertEqual(closed.record.run_revision, 1)
        self.assertEqual(closed.run.current_revision, 1)
        self.assertEqual(closed.revision.operation_kind, "run.control")
        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(
            [record.state for record in snapshot.submission_records],
            ["accepting", "draining"],
        )
        self.assertEqual(
            snapshot.current_submission.previous_record_digest,
            snapshot.submission_records[0].digest,
        )

        with self.assertRaises(RealmConflict):
            self.close_submissions(
                operation_id=operation_id, stop_code="method_failed"
            )

    def test_soft_drain_escalates_to_one_immutable_hard_stop(self) -> None:
        closed = self.close_submissions(
            operation_id=self.op("soft-close"), stop_code="method_completed"
        )
        operation_id = self.op("escalate")
        escalated = self.escalate_stop(
            operation_id=operation_id,
            expected_run_revision=closed.run.current_revision,
        )
        replay = self.escalate_stop(
            operation_id=operation_id,
            expected_run_revision=closed.run.current_revision,
        )

        self.assertEqual(replay, escalated)
        self.assertEqual(escalated.revision.operation_kind, "run.control.escalate")
        self.assertEqual(escalated.record.state, "draining")
        self.assertEqual(escalated.record.stop_code, "protocol_error")
        control = self.ledger.read_run_control(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(
            [(item.state, item.stop_code) for item in control.submission_records],
            [
                ("accepting", None),
                ("draining", "method_completed"),
                ("draining", "protocol_error"),
            ],
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=escalated.run.current_revision,
            expected_head_sequence=escalated.run.next_sequence - 1,
        )
        event = next(
            item for item in timeline.items if item.event == "run_stop_escalated"
        )
        self.assertEqual(event.code, "protocol_error")
        connection = self.connection()
        try:
            payload = connection.execute(
                "SELECT payload_json FROM run_events "
                "WHERE run_id = 'run-a' AND event = 'run_stop_escalated'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(
            json.loads(payload),
            {
                "previous_stop_code": "method_completed",
                "stop_code": "protocol_error",
            },
        )

        with self.assertRaisesRegex(ValueError, "hard stop code"):
            self.escalate_stop(
                operation_id=self.op("soft-rewrite"),
                expected_run_revision=escalated.run.current_revision,
                stop_code="method_completed",
            )
        with self.assertRaisesRegex(RealmConflict, "cannot be rewritten"):
            self.escalate_stop(
                operation_id=self.op("hard-rewrite"),
                expected_run_revision=escalated.run.current_revision,
                stop_code="admin_cancelled",
            )

    def test_sql_guards_reject_soft_rewrite_and_unanchored_escalation_event(
        self,
    ) -> None:
        closed = self.close_submissions(
            operation_id=self.op("soft-close-for-forgery"),
            stop_code="method_completed",
        )
        connection = self.connection()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES (?, 'run.control.escalate', ?, '{}', ?)",
                (self.op("raw-soft-rewrite"), "0" * 64, now),
            ).lastrowid
            forged = {
                **closed.record.to_dict(),
                "previous_record_digest": closed.record.digest,
                "previous_run_revision": closed.record.run_revision,
                "previous_state": "draining",
                "run_revision": closed.run.current_revision + 1,
                "state": "draining",
                "stop_code": "method_completed",
            }
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "submission control record requires its open typed transaction",
            ):
                connection.execute(
                    "INSERT INTO run_submission_control_records("
                    "run_id, control_index, state, stop_code, run_revision, "
                    "previous_run_revision, previous_state, previous_record_digest, "
                    "record_digest, record_json, txn_id, created_at) "
                    "VALUES ('run-a', 2, 'draining', 'method_completed', "
                    "?, ?, 'draining', ?, ?, ?, ?, ?)",
                    (
                        closed.run.current_revision + 1,
                        closed.record.run_revision,
                        closed.record.digest,
                        "1" * 64,
                        json.dumps(forged, sort_keys=True, separators=(",", ":")),
                        txn_id,
                        now,
                    ),
                )
            connection.rollback()

            connection.execute("BEGIN IMMEDIATE")
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES (?, 'run.control.escalate', ?, '{}', ?)",
                (self.op("raw-bad-event"), "2" * 64, now),
            ).lastrowid
            escalated = closed.record.transition(
                state="draining",
                run_revision=closed.run.current_revision + 1,
                stop_code="protocol_error",
            )
            connection.execute(
                "INSERT INTO run_submission_control_records("
                "run_id, control_index, state, stop_code, run_revision, "
                "previous_run_revision, previous_state, previous_record_digest, "
                "record_digest, record_json, txn_id, created_at) "
                "VALUES ('run-a', 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    escalated.state,
                    escalated.stop_code,
                    escalated.run_revision,
                    escalated.previous_run_revision,
                    escalated.previous_state,
                    escalated.previous_record_digest,
                    escalated.digest,
                    escalated.to_bytes().decode("utf-8"),
                    txn_id,
                    now,
                ),
            )
            sequence = closed.run.next_sequence
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, "
                "candidate_id, logical_trial_id, session_handle, payload_json, "
                "run_revision, txn_id, created_at) VALUES ('run-a', ?, ?, "
                "'optpilot.run-event.v1', 'controller', 'run_stop_escalated', "
                "'run', 'draining', NULL, 'protocol_error', 0, NULL, NULL, "
                "NULL, ?, ?, ?, ?)",
                (
                    sequence,
                    self.op("raw-bad-event-id"),
                    '{"previous_stop_code":"wrong","stop_code":"protocol_error"}',
                    escalated.run_revision,
                    txn_id,
                    now,
                ),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run stop escalation revision is inconsistent",
            ):
                connection.execute(
                    "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                    "last_sequence, next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, "
                    "created_at) VALUES ('run-a', ?, 0, ?, ?, 0, 1, ?, ?, "
                    "'run.control.escalate', ?, ?)",
                    (
                        escalated.run_revision,
                        sequence,
                        sequence + 1,
                        self.created.controller_lease.lease_id,
                        self.created.controller_lease.fencing_token,
                        txn_id,
                        now,
                    ),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_admission_is_rejected_after_submissions_start_draining(self) -> None:
        closed = self.close_submissions(operation_id=self.op("close-before-admit"))
        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-admission"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )

        with self.assertRaises(RealmConflict):
            self.ledger.commit_run_candidate_admissions(
                operation_id=self.op("admit-while-draining"),
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                expected_run_revision=closed.run.current_revision,
                expected_owner_revision=0,
                controller_lease_id=self.created.controller_lease.lease_id,
                controller_holder_id=self.created.controller_lease.holder_id,
                controller_fencing_token=(
                    self.created.controller_lease.fencing_token
                ),
                change_id=change.change_id,
                plan=self.parameter_plan(),
            )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_candidates WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT accepted_logical_trials FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_explicit_close_rejects_unclassified_and_derived_reasons(self) -> None:
        for stop_code in ("manual_stop", "max_trials", "max_failures", "converged"):
            with self.subTest(stop_code=stop_code):
                with self.assertRaisesRegex(ValueError, "derived atomically"):
                    self.close_submissions(
                        operation_id=self.op(f"unsupported-{stop_code}"),
                        stop_code=stop_code,
                    )
        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(snapshot.current_submission.state, "accepting")
        self.assertEqual(len(snapshot.submission_records), 1)

    def test_finish_requires_submissions_to_be_draining(self) -> None:
        with self.assertRaises(RealmConflict):
            self.finish(
                operation_id=self.op("finish-while-accepting"),
                expected_run_revision=0,
            )

        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(snapshot.current_submission.state, "accepting")
        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_finalizations WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT current_revision, state FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (0, "running"),
            )
        finally:
            connection.close()

    def test_terminal_submission_control_is_atomic_with_finish(self) -> None:
        closed = self.close_submissions(
            operation_id=self.op("close-before-finish"), stop_code="method_completed"
        )
        connection = self.connection()
        try:
            connection.execute(
                "CREATE TRIGGER inject_run_control_finish_failure "
                "BEFORE INSERT ON run_revisions "
                "WHEN NEW.operation_kind = 'run.finish' "
                "BEGIN SELECT RAISE(ABORT, 'injected run-control finish failure'); END"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected run-control finish failure"
        ):
            self.finish(
                operation_id=self.op("injected-finish"),
                expected_run_revision=closed.run.current_revision,
            )

        connection = self.connection()
        try:
            connection.execute("DROP TRIGGER inject_run_control_finish_failure")
            connection.commit()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_finalizations WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_submission_control_records "
                    "WHERE run_id = 'run-a'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()
        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(snapshot.current_submission.state, "draining")

        finished = self.finish(
            operation_id=self.op("finish"),
            expected_run_revision=closed.run.current_revision,
        )
        snapshot = self.ledger.read_run_control(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(finished.run.current_revision, 2)
        self.assertEqual(
            [record.state for record in snapshot.submission_records],
            ["accepting", "draining", "terminal"],
        )
        self.assertEqual(
            [record.run_revision for record in snapshot.submission_records],
            [0, 1, 2],
        )
        self.assertEqual(snapshot.current_submission.stop_code, "method_completed")
        self.assertEqual(finished.finalization.terminal_state, "failed")
        self.assertEqual(finished.finalization.code, "no_successful_observation")
        self.assertEqual(
            snapshot.current_submission.previous_record_digest,
            snapshot.submission_records[-2].digest,
        )

        connection = self.connection()
        try:
            finalization_txn_id = connection.execute(
                "SELECT txn_id FROM run_finalizations WHERE run_id = 'run-a'"
            ).fetchone()[0]
            terminal_control_txn_id = connection.execute(
                "SELECT txn_id FROM run_submission_control_records "
                "WHERE run_id = 'run-a' ORDER BY control_index DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(terminal_control_txn_id, finalization_txn_id)
        finally:
            connection.close()

    def test_raw_finish_cannot_forge_a_contradictory_terminal_pair(self) -> None:
        draining = self.close_submissions(
            operation_id=self.op("close-before-raw-finish"),
            stop_code="method_completed",
        )
        connection = self.connection()
        try:
            run = connection.execute(
                "SELECT current_revision, next_sequence, accepted_logical_trials, "
                "controller_generation, controller_lease_id, controller_fencing_token "
                "FROM run_namespaces WHERE run_id = 'run-a'"
            ).fetchone()
            owner_revision = connection.execute(
                "SELECT revision FROM owners WHERE owner_id = 'run-owner-a'"
            ).fetchone()[0]
            now = time.time()
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES (?, 'run.finish', ?, '{}', ?)",
                (self.op("raw-contradictory-finish"), "0" * 64, now),
            ).lastrowid
            revision = run[0] + 1
            sequence = run[1]
            terminal = draining.record.transition(
                state="terminal",
                run_revision=revision,
                stop_code=draining.record.stop_code,
            )
            connection.execute(
                "INSERT INTO run_submission_control_records("
                "run_id, control_index, state, stop_code, run_revision, "
                "previous_run_revision, previous_state, previous_record_digest, "
                "record_digest, record_json, txn_id, created_at"
                ") VALUES ('run-a', 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    terminal.state,
                    terminal.stop_code,
                    terminal.run_revision,
                    terminal.previous_run_revision,
                    terminal.previous_state,
                    terminal.previous_record_digest,
                    terminal.digest,
                    terminal.to_bytes().decode("utf-8"),
                    txn_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO run_finalizations(run_id, terminal_state, code, "
                "run_revision, txn_id, created_at) "
                "VALUES ('run-a', 'succeeded', 'method_completed', ?, ?, ?)",
                (revision, txn_id, now),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, txn_id, "
                "created_at) VALUES ('run-a', ?, ?, 'optpilot.run-event.v1', "
                "'controller', 'run_finished', 'run', 'succeeded', NULL, "
                "'method_completed', 1, NULL, NULL, NULL, "
                "'{\"terminal_state\":\"succeeded\"}', ?, ?, ?)",
                (sequence, self.op("raw-finish-event"), revision, txn_id, now),
            )
            # Schema v21 rejects a raw finish before it can become a canonical
            # terminal head unless the same transaction also carries the
            # immutable evidence seal.  The older terminal-policy trigger is
            # still defense in depth, but an unsealed forged pair now fails at
            # the stronger outer invariant first.
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run finish revision requires its terminal evidence seal",
            ):
                connection.execute(
                    "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                    "last_sequence, next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, created_at) "
                    "VALUES ('run-a', ?, ?, ?, ?, ?, ?, ?, ?, 'run.finish', ?, ?)",
                    (
                        revision,
                        owner_revision,
                        sequence,
                        sequence + 1,
                        run[2],
                        run[3],
                        run[4],
                        run[5],
                        txn_id,
                        now,
                    ),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_run_control_history_is_immutable_and_replace_safe(self) -> None:
        self.close_submissions(operation_id=self.op("close-before-mutation"))
        connection = self.connection()
        try:
            for table in (
                "run_control_manifests",
                "run_submission_control_records",
            ):
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE run_id = 'run-a' LIMIT 1"
                ).fetchone()
                assert row is not None
                placeholders = ", ".join("?" for _ in row)
                with self.subTest(table=table, operation="update"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"UPDATE {table} SET run_id = run_id "
                            "WHERE run_id = 'run-a'"
                        )
                    connection.rollback()
                with self.subTest(table=table, operation="delete"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"DELETE FROM {table} WHERE run_id = 'run-a'"
                        )
                    connection.rollback()
                with self.subTest(table=table, operation="replace"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})",
                            row,
                        )
                    connection.rollback()
        finally:
            connection.close()

    def test_read_rejects_noncanonical_persisted_manifest(self) -> None:
        connection = self.connection()
        try:
            trigger_names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'run_control_manifests'"
            ).fetchall()
            for (trigger_name,) in trigger_names:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(
                "UPDATE run_control_manifests SET manifest_json = '{}' "
                "WHERE run_id = 'run-a'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_control(
                actor_principal_id="operator", run_id=self.created.run.run_id
            )


if __name__ == "__main__":
    unittest.main()
