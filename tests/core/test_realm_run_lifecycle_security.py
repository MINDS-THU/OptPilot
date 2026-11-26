from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerPermission
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


class RealmRunLifecycleSecurityTest(unittest.TestCase):
    """Adversarial checks spanning finalization, controller recovery, and retirement."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="lifecycle-security/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="lifecycle-security/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="lifecycle-security",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=2)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.run = self.ledger.create_run_namespace(
            operation_id="lifecycle-security/run-create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=600,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        change = self.ledger.begin_owner_change(
            operation_id="lifecycle-security/admission-begin",
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        self.admission = self.ledger.commit_run_candidate_admissions(
            operation_id="lifecycle-security/admit",
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("candidate-a", envelope),),
                (LogicalTrialAdmission("trial-a", "candidate-a"),),
            ),
        )
        self.selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            candidate_id="candidate-a",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _terminalize_and_finish(self):
        transitioned = self.ledger.cancel_run_logical_trial(
            operation_id="lifecycle-security/terminalize",
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            logical_trial_id="trial-a",
            expected_run_revision=self.admission.run.current_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            code="admin_cancelled",
        )
        draining = self._close_submissions(
            operation_id="lifecycle-security/close-submissions",
            expected_run_revision=transitioned.run.current_revision,
        )
        return self.ledger.finish_run(
            operation_id="lifecycle-security/finish",
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )

    def _close_submissions(
        self, *, operation_id: str, expected_run_revision: int
    ):
        return self.ledger.close_run_submissions(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )

    @staticmethod
    def _insert_terminal_submission_control(
        connection: sqlite3.Connection,
        *,
        draining_record,
        run_revision: int,
        txn_id: int,
        now: float,
    ) -> None:
        terminal_record = draining_record.transition(
            state="terminal",
            run_revision=run_revision,
            stop_code=draining_record.stop_code,
        )
        connection.execute(
            "INSERT INTO run_submission_control_records("
            "run_id, control_index, state, stop_code, run_revision, "
            "previous_run_revision, previous_state, previous_record_digest, "
            "record_digest, record_json, txn_id, created_at"
            ") VALUES ('run-a', 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                terminal_record.state,
                terminal_record.stop_code,
                terminal_record.run_revision,
                terminal_record.previous_run_revision,
                terminal_record.previous_state,
                terminal_record.previous_record_digest,
                terminal_record.digest,
                terminal_record.to_bytes().decode("utf-8"),
                txn_id,
                now,
            ),
        )

    def _replace_controller(self, *, operation_id: str):
        return self.ledger.replace_run_controller(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_controller_generation=self.run.run.controller_generation,
            expected_controller_lease_id=self.run.controller_lease.lease_id,
            expected_controller_holder_id=self.run.controller_lease.holder_id,
            expected_controller_fencing_token=(
                self.run.controller_lease.fencing_token
            ),
            new_controller_holder_id="controller-b",
            controller_ttl_seconds=600,
        )

    def _retire_with_replacement(self, replacement, *, operation_id: str):
        owner = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        change = self.ledger.begin_owner_change(
            operation_id=f"{operation_id}/begin",
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=owner.revision,
            ttl_seconds=60,
        )
        receipt = self.ledger.retire_run(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=replacement.run.current_revision,
            expected_owner_revision=owner.revision,
            controller_lease_id=replacement.controller_lease.lease_id,
            controller_holder_id=replacement.controller_lease.holder_id,
            controller_fencing_token=replacement.controller_lease.fencing_token,
            change_id=change.change_id,
        )
        return receipt, change, owner.revision

    def test_released_post_finish_controller_can_be_recovered_and_retired(self) -> None:
        finished = self._terminalize_and_finish()
        released = self.ledger.release_lease(
            operation_id="lifecycle-security/controller-release",
            actor_principal_id="operator",
            lease_id=self.run.controller_lease.lease_id,
            holder_id=self.run.controller_lease.holder_id,
            fencing_token=self.run.controller_lease.fencing_token,
        )
        self.assertEqual(released.state.value, "released")

        replace_operation = "lifecycle-security/controller-recover-released"
        replacement = self._replace_controller(operation_id=replace_operation)
        self.assertEqual(replacement.run.state, finished.run.state)
        self.assertEqual(replacement.run.retention_state, "active")
        self.assertEqual(replacement.run.controller_generation, 2)
        self.assertEqual(
            self._replace_controller(operation_id=replace_operation), replacement
        )
        replaced_snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.run.run.run_id
        )
        self.assertEqual(replaced_snapshot.run.current_revision, 5)
        self.assertEqual(replaced_snapshot.control.current_submission.state, "terminal")
        self.assertEqual(
            replaced_snapshot.control.current_submission.run_revision,
            replaced_snapshot.finalization.run_revision,
        )

        retire_operation = "lifecycle-security/retire-after-release"
        retired, retirement_change, owner_revision = self._retire_with_replacement(
            replacement, operation_id=retire_operation
        )
        self.assertEqual(retired.run.retention_state, "retired")
        retired_snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.run.run.run_id
        )
        self.assertEqual(retired_snapshot.run.retention_state, "retired")
        self.assertLess(
            retired_snapshot.control.current_submission.run_revision,
            retired_snapshot.run.current_revision,
        )
        self.assertEqual(
            self.ledger.retire_run(
                operation_id=retire_operation,
                actor_principal_id="operator",
                run_id=self.run.run.run_id,
                expected_run_revision=replacement.run.current_revision,
                expected_owner_revision=owner_revision,
                controller_lease_id=replacement.controller_lease.lease_id,
                controller_holder_id=replacement.controller_lease.holder_id,
                controller_fencing_token=(
                    replacement.controller_lease.fencing_token
                ),
                change_id=retirement_change.change_id,
            ),
            retired,
        )

        resolved = self.ledger.resolve_run_candidate_evaluation(
            actor_principal_id="operator", selection=self.selection
        )
        self.assertEqual(resolved.candidate.availability, "available")
        self.assertEqual(resolved.evaluation.availability, "unavailable")
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM leases WHERE lease_id = ?",
                    (replacement.controller_lease.lease_id,),
                ).fetchone(),
                ("released",),
            )
        finally:
            connection.close()

    def test_expired_post_finish_controller_can_be_recovered_and_retired(self) -> None:
        self._terminalize_and_finish()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (self.run.controller_lease.lease_id,),
            )
            connection.commit()
        finally:
            connection.close()

        replacement = self._replace_controller(
            operation_id="lifecycle-security/controller-recover-expired"
        )
        self.assertEqual(replacement.previous_controller_lease.state.value, "expired")
        retired, _, _ = self._retire_with_replacement(
            replacement,
            operation_id="lifecycle-security/retire-after-expiry",
        )
        self.assertEqual(retired.run.retention_state, "retired")

    def test_raw_finalization_cannot_seal_a_run_with_nonterminal_trials(self) -> None:
        draining = self._close_submissions(
            operation_id="lifecycle-security/raw-finish-close-submissions",
            expected_run_revision=self.admission.run.current_revision,
        )
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
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
            cursor = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES ('lifecycle-security/raw-finish', 'run.finish', ?, '{}', ?)",
                ("0" * 64, now),
            )
            txn_id = cursor.lastrowid
            run_revision = run[0] + 1
            sequence = run[1]
            self._insert_terminal_submission_control(
                connection,
                draining_record=draining.record,
                run_revision=run_revision,
                txn_id=txn_id,
                now=now,
            )
            connection.execute(
                "INSERT INTO run_finalizations(run_id, terminal_state, code, "
                "run_revision, txn_id, created_at) "
                "VALUES ('run-a', 'cancelled', 'admin_cancelled', ?, ?, ?)",
                (run_revision, txn_id, now),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, txn_id, "
                "created_at) VALUES ('run-a', ?, 'forged-finish', "
                "'optpilot.run-event.v1', 'controller', 'run_finished', 'run', "
                "'cancelled', NULL, 'admin_cancelled', 1, NULL, NULL, NULL, "
                "'{\"terminal_state\":\"cancelled\"}', ?, ?, ?)",
                (sequence, run_revision, txn_id, now),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run finalization revision is inconsistent",
            ):
                connection.execute(
                    "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                    "last_sequence, next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, created_at) "
                    "VALUES ('run-a', ?, ?, ?, ?, ?, ?, ?, ?, 'run.finish', ?, ?)",
                    (
                        run_revision,
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
            self.assertEqual(
                connection.execute(
                    "SELECT state, current_revision FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                ("running", draining.run.current_revision),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_finalizations WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.rollback()
            connection.close()

    def test_exact_raw_finish_without_terminal_seal_is_rejected(self) -> None:
        transitioned = self.ledger.cancel_run_logical_trial(
            operation_id="lifecycle-security/raw-finish-terminalize",
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            logical_trial_id="trial-a",
            expected_run_revision=self.admission.run.current_revision,
            controller_lease_id=self.run.controller_lease.lease_id,
            controller_holder_id=self.run.controller_lease.holder_id,
            controller_fencing_token=self.run.controller_lease.fencing_token,
            code="admin_cancelled",
        )
        draining = self._close_submissions(
            operation_id="lifecycle-security/exact-raw-finish-close-submissions",
            expected_run_revision=transitioned.run.current_revision,
        )
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            cursor = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES ('lifecycle-security/exact-raw-finish', 'run.finish', ?, '{}', ?)",
                ("2" * 64, now),
            )
            txn_id = cursor.lastrowid
            revision = draining.run.current_revision + 1
            sequence = draining.run.next_sequence
            self._insert_terminal_submission_control(
                connection,
                draining_record=draining.record,
                run_revision=revision,
                txn_id=txn_id,
                now=now,
            )
            connection.execute(
                "INSERT INTO run_finalizations(run_id, terminal_state, code, "
                "run_revision, txn_id, created_at) "
                "VALUES ('run-a', 'cancelled', 'admin_cancelled', ?, ?, ?)",
                (revision, txn_id, now),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, txn_id, "
                "created_at) VALUES ('run-a', ?, 'exact-raw-finish-event', "
                "'optpilot.run-event.v1', 'controller', 'run_finished', 'run', "
                "'cancelled', NULL, 'admin_cancelled', 1, NULL, NULL, NULL, "
                "'{\"terminal_state\":\"cancelled\"}', ?, ?, ?)",
                (sequence, revision, txn_id, now),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run finish revision requires its terminal evidence seal",
            ):
                connection.execute(
                    "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                    "last_sequence, next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, created_at) "
                    "VALUES ('run-a', ?, 0, ?, ?, 1, 1, ?, ?, 'run.finish', ?, ?)",
                    (
                        revision,
                        sequence,
                        sequence + 1,
                        self.run.controller_lease.lease_id,
                        self.run.controller_lease.fencing_token,
                        txn_id,
                        now,
                    ),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT state, current_revision FROM run_namespaces "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                ("running", draining.run.current_revision),
            )
        finally:
            connection.rollback()
            connection.close()

    def test_exact_raw_retirement_derives_retention_and_controller_release(self) -> None:
        finished = self._terminalize_and_finish()
        change = self.ledger.begin_owner_change(
            operation_id="lifecycle-security/exact-raw-retirement-begin",
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=600,
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            cursor = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES ('lifecycle-security/exact-raw-retire', 'run.retire', ?, '{}', ?)",
                ("3" * 64, now),
            )
            txn_id = cursor.lastrowid
            revision = finished.run.current_revision + 1
            owner_revision = 1
            sequence = finished.run.next_sequence
            connection.execute(
                "INSERT INTO run_retirements(run_id, run_revision, owner_revision, "
                "txn_id, created_at) VALUES ('run-a', ?, ?, ?, ?)",
                (revision, owner_revision, txn_id, now),
            )
            connection.execute(
                "UPDATE owner_memberships SET removed_revision = ?, removed_txn_id = ? "
                "WHERE owner_id = 'run-owner-a' AND removed_revision IS NULL "
                "AND role IN ('run-candidate', 'run-environment-source', "
                "'run-method-source', 'run-attempt-input', "
                "'run-prepared-runtime', 'run-prepared-method-runtime')",
                (owner_revision, txn_id),
            )
            connection.execute(
                "UPDATE owners SET revision = ?, updated_at = ? "
                "WHERE owner_id = 'run-owner-a'",
                (owner_revision, now),
            )
            manifest_digest = self.ledger._owner_manifest_digest(
                connection, "run-owner-a"
            )
            connection.execute(
                "INSERT INTO owner_revisions(owner_id, revision, txn_id, "
                "manifest_digest, created_at) VALUES ('run-owner-a', ?, ?, ?, ?)",
                (owner_revision, txn_id, manifest_digest, now),
            )
            connection.execute(
                "UPDATE owner_transactions SET state = 'committed', "
                "committed_txn_id = ?, updated_at = ? WHERE change_id = ?",
                (txn_id, now, change.change_id),
            )
            connection.execute(
                "UPDATE leases SET state = 'released', updated_at = ? "
                "WHERE lease_id = ?",
                (now, change.retention_lease_id),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, txn_id, "
                "created_at) VALUES ('run-a', ?, 'exact-raw-retirement-event', "
                "'optpilot.run-event.v1', 'controller', 'run_retired', 'retention', "
                "'terminal', NULL, NULL, 1, NULL, NULL, NULL, "
                "'{\"released_memberships\":2}', ?, ?, ?)",
                (sequence, revision, txn_id, now),
            )
            connection.execute(
                "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                "last_sequence, next_sequence, accepted_logical_trials, "
                "controller_generation, writer_controller_lease_id, "
                "writer_controller_fencing_token, operation_kind, txn_id, created_at) "
                "VALUES ('run-a', ?, ?, ?, ?, 1, 1, ?, ?, 'run.retire', ?, ?)",
                (
                    revision,
                    owner_revision,
                    sequence,
                    sequence + 1,
                    self.run.controller_lease.lease_id,
                    self.run.controller_lease.fencing_token,
                    txn_id,
                    now,
                ),
            )
            # No direct namespace transition or controller release follows.
            connection.commit()
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT retention_state, current_revision FROM run_namespaces "
                        "WHERE run_id = 'run-a'"
                    ).fetchone()
                ),
                ("retired", revision),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT state FROM leases WHERE lease_id = ?",
                        (self.run.controller_lease.lease_id,),
                    ).fetchone()
                ),
                ("released",),
            )
        finally:
            connection.rollback()
            connection.close()

    def test_raw_retirement_cannot_bypass_an_active_worker_lease(self) -> None:
        finished = self._terminalize_and_finish()
        worker = self.ledger.acquire_lease(
            operation_id="lifecycle-security/worker-acquire",
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            parent_lease_id=self.run.controller_lease.lease_id,
            lease_kind="attempt-worker",
            audience="realm-ledger",
            holder_id="worker-a",
            scope_key="run:run-a/attempt:a",
            ttl_seconds=600,
        )
        change = self.ledger.begin_owner_change(
            operation_id="lifecycle-security/raw-retirement-begin",
            actor_principal_id="operator",
            owner_id=self.run.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=600,
        )

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            cursor = connection.execute(
                "INSERT INTO ledger_transactions(operation_id, operation_kind, "
                "request_digest, receipt_json, committed_at) "
                "VALUES ('lifecycle-security/raw-retire', 'run.retire', ?, '{}', ?)",
                ("1" * 64, now),
            )
            txn_id = cursor.lastrowid
            run_revision = finished.run.current_revision + 1
            owner_revision = 1
            sequence = finished.run.next_sequence
            connection.execute(
                "INSERT INTO run_retirements(run_id, run_revision, owner_revision, "
                "txn_id, created_at) VALUES ('run-a', ?, ?, ?, ?)",
                (run_revision, owner_revision, txn_id, now),
            )
            connection.execute(
                "UPDATE owner_memberships SET removed_revision = ?, removed_txn_id = ? "
                "WHERE owner_id = 'run-owner-a' AND removed_revision IS NULL "
                "AND role IN ('run-candidate', 'run-environment-source', "
                "'run-method-source', 'run-attempt-input', "
                "'run-prepared-runtime', 'run-prepared-method-runtime')",
                (owner_revision, txn_id),
            )
            connection.execute(
                "UPDATE owners SET revision = ?, updated_at = ? "
                "WHERE owner_id = 'run-owner-a'",
                (owner_revision, now),
            )
            manifest_digest = self.ledger._owner_manifest_digest(
                connection, "run-owner-a"
            )
            connection.execute(
                "INSERT INTO owner_revisions(owner_id, revision, txn_id, "
                "manifest_digest, created_at) VALUES ('run-owner-a', ?, ?, ?, ?)",
                (owner_revision, txn_id, manifest_digest, now),
            )
            connection.execute(
                "UPDATE owner_transactions SET state = 'committed', "
                "committed_txn_id = ?, updated_at = ? WHERE change_id = ?",
                (txn_id, now, change.change_id),
            )
            connection.execute(
                "UPDATE leases SET state = 'released', updated_at = ? "
                "WHERE lease_id = ?",
                (now, change.retention_lease_id),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_id, schema_version, "
                "producer, event, phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, txn_id, "
                "created_at) VALUES ('run-a', ?, 'forged-retirement', "
                "'optpilot.run-event.v1', 'controller', 'run_retired', 'retention', "
                "'terminal', NULL, NULL, 1, NULL, NULL, NULL, "
                "'{\"released_memberships\":2}', ?, ?, ?)",
                (sequence, run_revision, txn_id, now),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run retirement revision is inconsistent",
            ):
                connection.execute(
                    "INSERT INTO run_revisions(run_id, revision, owner_revision, "
                    "last_sequence, next_sequence, accepted_logical_trials, "
                    "controller_generation, writer_controller_lease_id, "
                    "writer_controller_fencing_token, operation_kind, txn_id, created_at) "
                    "VALUES ('run-a', ?, ?, ?, ?, ?, ?, ?, ?, 'run.retire', ?, ?)",
                    (
                        run_revision,
                        owner_revision,
                        sequence,
                        sequence + 1,
                        finished.run.accepted_logical_trials,
                        finished.run.controller_generation,
                        self.run.controller_lease.lease_id,
                        self.run.controller_lease.fencing_token,
                        txn_id,
                        now,
                    ),
                )
            connection.rollback()
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT state FROM leases WHERE lease_id = ?",
                        (worker.lease_id,),
                    ).fetchone()
                ),
                ("active",),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT retention_state, current_revision FROM run_namespaces "
                        "WHERE run_id = 'run-a'"
                    ).fetchone()
                ),
                ("active", finished.run.current_revision),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_retirements WHERE run_id = 'run-a'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
