from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import PublishedObject
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.operator_job_ledger import OperatorJobActorCursor, _json_object
from optpilot.realm.operator_capacity_records import operator_capacity_reservation_id
from optpilot.realm.operator_job_records import (
    OPERATOR_JOB_OUTPUT_ROLE,
    OperatorJobCleanupComponentEvidence,
    OperatorJobCleanupComponentState,
    OperatorJobCleanupEvidence,
    OperatorJobCleanupState,
    OperatorJobDeclaredOutput,
    OperatorJobLaunchPlan,
    OperatorJobLogMetadata,
    OperatorJobOutcome,
    OperatorJobReconciliationState,
    OperatorJobResult,
    OperatorJobState,
    OperatorJobTarget,
    OperatorJobTerminalDisposition,
    OperatorJobTerminalStatus,
)
from optpilot.realm.owner_derivation import (
    Binding,
    OwnerDerivationManifest,
)
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import (
    BlobRef,
    SnapshotRef,
    canonical_json_bytes,
    request_digest,
)
from optpilot.realm.selections import SelectionRef


class RealmOperatorJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.counter = 0
        self.ledger.register_principal(
            operation_id=self.op("principal"),
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_principal(
            operation_id=self.op("other-principal"),
            principal_id="other",
            kind="human",
        )
        self.ledger.register_store(
            operation_id=self.op("store"),
            store_id="store-a",
            backend_kind="local-cas",
            root_marker="operator-job-test-store",
        )
        self.ledger.create_owner(
            operation_id=self.op("source-owner"),
            owner_id="source-run-owner",
            owner_kind="run",
            principal_id="operator",
        )
        self.input_ref = BlobRef.from_bytes(b"immutable candidate input")
        change = self.ledger.begin_owner_change(
            operation_id=self.op("source-change"),
            actor_principal_id="operator",
            owner_id="source-run-owner",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        publication = PublishedObject(
            staging_id=f"stage-{1:032x}",
            store_id="store-a",
            content_ref=self.input_ref,
            kind="blob",
            logical_bytes=25,
            physical_bytes=25,
            metadata={"format": "operator-job-test"},
            edges=(),
        )
        capture = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id="store-a",
        )
        capture.reserve_staging(
            change_id=change.change_id,
            staging_id=publication.staging_id,
            store_id="store-a",
            object_kind="blob",
        )
        capture.prepare_publication(
            change_id=change.change_id, publication=publication
        )
        capture.record_publication(
            change_id=change.change_id, publication=publication
        )
        capture.complete_staging_publication(
            change_id=change.change_id, staging_id=publication.staging_id
        )
        self.source_membership = OwnerMembership(
            "store-a", self.input_ref, "run-candidate"
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("source-hold"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(self.source_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("source-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(self.source_membership,),
        )
        anchor = self.ledger.read_owner_source_anchor(
            actor_principal_id="operator", owner_id="source-run-owner"
        )
        self.job_derivation = OwnerDerivationManifest(
            target_owner_id="job-owner",
            target_owner_kind="operator-job",
            sources=(anchor,),
            bindings=(
                Binding(
                    source_owner_id="source-run-owner",
                    source_store_id="store-a",
                    content_ref=self.input_ref,
                    source_role="run-candidate",
                    target_role="operator-job-input",
                ),
            ),
        )
        self.ledger.derive_owner(
            operation_id=self.op("job-owner-derive"),
            actor_principal_id="operator",
            manifest=self.job_derivation,
        )
        self.selection = SelectionRef.build(
            kind="candidate",
            source_kind="run",
            source_id="run-a",
            source_owner_id="source-run-owner",
            source_revision=7,
            owner_revision=1,
            source_sequence=19,
            entity_sequence=11,
            entity_id="candidate-a",
            entity_ref="candidate:parameters:sha256:" + "a" * 64,
            context_digest="b" * 64,
            relative_path=None,
        )
        input_facts = {
            "debug_override_digest": "0" * 64,
            "evaluation_spec_digest": "f" * 64,
            "evaluation_seed": 17,
            "repetition_index": 0,
        }
        self.plan = OperatorJobLaunchPlan(
            job_kind="candidate-debug-run",
            target=OperatorJobTarget(
                kind="candidate-evaluation", selection=self.selection
            ),
            input_facts=input_facts,
            input_facts_digest=hashlib.sha256(
                canonical_json_bytes(input_facts)
            ).hexdigest(),
            owner_derivation_manifest_digest=self.job_derivation.digest,
            source_fingerprints=("2" * 64, "3" * 64),
            runtime_fingerprint="4" * 64,
            entrypoint_profile="default",
            projection_contract_digest="5" * 64,
            backend_kind="local-process",
            backend_realm="local-host",
            resource_claims={"cpu_millis": 1000, "memory_bytes": 1024},
            timeout_seconds=30,
            network_policy="denied",
            network_enforcement="advisory",
            requested_secret_names=(),
            grants_digest="6" * 64,
            evidence_sink_kind="operator-job-result",
            evidence_sink_id="debug-attempt-a",
            evidence_sink_digest="7" * 64,
            cancellation_guarantee="confirmed",
            priority_class="interactive",
        )
        self.ledger.ensure_operator_capacity_pool(
            operation_id=self.op("capacity-pool"),
            actor_principal_id="operator",
            pool_name="local-host",
            limits={"cpu_millis": 8000, "memory_bytes": 16 * 1024**3},
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"operator-job-test/{self.counter}/{label}"

    def plan_job(self, label: str = "a"):
        return self.ledger.plan_operator_job(
            operation_id=self.op(f"plan-{label}"),
            actor_principal_id="operator",
            job_owner_id="job-owner",
            plan=self.plan,
            job_id=f"job-{label}",
        )

    def plan_additional_job(self, label: str):
        job_owner_id = f"job-owner-{label}"
        derivation = OwnerDerivationManifest(
            target_owner_id=job_owner_id,
            target_owner_kind="operator-job",
            sources=self.job_derivation.sources,
            bindings=self.job_derivation.bindings,
        )
        self.ledger.derive_owner(
            operation_id=self.op(f"derive-{label}"),
            actor_principal_id="operator",
            manifest=derivation,
        )
        return self.ledger.plan_operator_job(
            operation_id=self.op(f"plan-{label}"),
            actor_principal_id="operator",
            job_owner_id=job_owner_id,
            plan=replace(
                self.plan,
                owner_derivation_manifest_digest=derivation.digest,
            ),
            job_id=f"job-{label}",
        )

    def approve_job(self, job):
        awaiting = self.ledger.request_operator_job_approval(
            operation_id=self.op("request-approval"),
            actor_principal_id="operator",
            job_id=job.job_id,
            expected_revision=job.revision,
        )
        return self.ledger.approve_operator_job(
            operation_id=self.op("approve"),
            actor_principal_id="operator",
            job_id=job.job_id,
            expected_revision=awaiting.revision,
            expected_plan_digest=self.plan.digest,
            approval_scope_digest="8" * 64,
        )

    def admission(self, job):
        self.ledger.acquire_operator_capacity_reservation(
            operation_id=self.op("capacity"),
            actor_principal_id="operator",
            pool_name="local-host",
            job_id=job.job_id,
            holder_id=f"capacity-{job.job_id}",
            ttl_seconds=60,
        )
        return self.ledger.acquire_lease(
            operation_id=self.op("admission"),
            actor_principal_id="operator",
            owner_id="job-owner",
            lease_kind="operator-job-admission",
            audience="operator-job",
            holder_id="operator-job-supervisor",
            scope_key=f"operator-job-admission:{job.job_id}",
            ttl_seconds=60,
            metadata={"job_id": job.job_id, "plan_digest": job.plan_digest},
        )

    def begin_job_capture(
        self,
        label: str,
        additions: tuple[OwnerMembership, ...] = (),
    ):
        owner = self.ledger.read_owner(
            actor_principal_id="operator", owner_id="job-owner"
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op(f"capture-{label}"),
            actor_principal_id="operator",
            owner_id="job-owner",
            expected_owner_revision=owner.revision,
            ttl_seconds=60,
        )
        if additions:
            self.ledger.hold_owner_content(
                operation_id=self.op(f"capture-hold-{label}"),
                actor_principal_id="operator",
                change_id=change.change_id,
                memberships=additions,
            )
        return change, owner.revision

    def start_running_job(self, label: str):
        queued = self.approve_job(self.plan_job(label))
        admission = self.admission(queued)
        launch_token = f"launch-{label}"
        starting = self.ledger.begin_operator_job_start(
            operation_id=self.op(f"begin-{label}"),
            actor_principal_id="operator",
            job_id=queued.job_id,
            expected_revision=queued.revision,
            admission_lease_id=admission.lease_id,
            admission_holder_id=admission.holder_id,
            admission_fencing_token=admission.fencing_token,
            binding_id=f"binding-{label}",
            launch_token=launch_token,
            provider_kind="local-process",
            evidence_fingerprint="9" * 64,
            launch_request_digest="a" * 64,
        )
        running = self.ledger.mark_operator_job_running(
            operation_id=self.op(f"running-{label}"),
            actor_principal_id="operator",
            job_id=starting.job_id,
            expected_revision=starting.revision,
            launch_token=launch_token,
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
        )
        return running, admission, launch_token

    def finish_job_with_declared_output(self, *, kind: str):
        running, admission, launch_token = self.start_running_job(
            f"selection-{kind}"
        )
        capture, owner_revision = self.begin_job_capture(f"selection-{kind}")
        if kind == "tree":
            manifest = b"operator job retained tree"
            content_ref = SnapshotRef.from_manifest_bytes(manifest)
            publication = PublishedObject(
                staging_id=f"stage-{2:032x}",
                store_id="store-a",
                content_ref=content_ref,
                kind="tree",
                logical_bytes=len(manifest),
                physical_bytes=len(manifest),
                metadata={"format": "operator-job-selection-test"},
                edges=(),
            )
            content_capture = self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=capture.change_id,
                store_id="store-a",
            )
            content_capture.reserve_staging(
                change_id=capture.change_id,
                staging_id=publication.staging_id,
                store_id="store-a",
                object_kind="tree",
            )
            content_capture.prepare_publication(
                change_id=capture.change_id, publication=publication
            )
            content_capture.record_publication(
                change_id=capture.change_id, publication=publication
            )
            content_capture.complete_staging_publication(
                change_id=capture.change_id,
                staging_id=publication.staging_id,
            )
            size_bytes = len(manifest)
        elif kind == "file":
            content_ref = self.input_ref
            size_bytes = 25
        else:
            raise AssertionError(f"unsupported test output kind: {kind}")
        addition = OwnerMembership(
            "store-a", content_ref, OPERATOR_JOB_OUTPUT_ROLE
        )
        self.ledger.hold_owner_content(
            operation_id=self.op(f"selection-{kind}-hold"),
            actor_principal_id="operator",
            change_id=capture.change_id,
            memberships=(addition,),
        )
        output = OperatorJobDeclaredOutput(
            declaration_id="primary-output",
            name="retained-output",
            kind=kind,
            content_ref=str(content_ref),
            size_bytes=size_bytes,
            identity_digest=request_digest(
                {"kind": kind, "content_ref": str(content_ref)}
            ),
            media_type=None,
        )
        result = OperatorJobResult(
            result_kind="environment-preview",
            status="success",
            metrics={},
            constraint_results={},
            event_summary={"termination": "completed"},
            declared_outputs=(output,),
            logs=(),
            details={},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="d" * 64,
            evidence_digest=result.digest,
        )
        terminal = self.ledger.finish_operator_job(
            operation_id=self.op(f"selection-{kind}-finish"),
            actor_principal_id="operator",
            job_id=running.job_id,
            expected_revision=running.revision,
            launch_token=launch_token,
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
            change_id=capture.change_id,
            expected_owner_revision=owner_revision,
            additions=(addition,),
            outcome=outcome,
            result=result,
        )
        return terminal, addition

    def cleanup_evidence(
        self,
        terminal,
        *,
        resources: bool,
        admission: bool,
        capacity: bool = False,
    ) -> OperatorJobCleanupEvidence:
        complete = OperatorJobCleanupComponentState.COMPLETE
        not_applicable = OperatorJobCleanupComponentState.NOT_APPLICABLE
        return OperatorJobCleanupEvidence(
            terminal_revision=terminal.revision,
            terminal_outcome_digest=hashlib.sha256(
                canonical_json_bytes(terminal.outcome.to_dict())
            ).hexdigest(),
            provider=OperatorJobCleanupComponentEvidence(
                state=complete, evidence_digest="1" * 64
            ),
            resources=OperatorJobCleanupComponentEvidence(
                state=complete if resources else not_applicable,
                evidence_digest="2" * 64 if resources else None,
            ),
            capacity=OperatorJobCleanupComponentEvidence(
                state=complete if capacity else not_applicable,
                evidence_digest="4" * 64 if capacity else None,
            ),
            admission=OperatorJobCleanupComponentEvidence(
                state=complete if admission else not_applicable,
                evidence_digest="3" * 64 if admission else None,
            ),
        )

    def test_actor_job_scan_pages_equal_timestamps_without_gaps(self) -> None:
        with mock.patch("optpilot.realm.ledger.time.time", return_value=1_000.0):
            records = (
                self.plan_job("a"),
                self.plan_additional_job("b"),
                self.plan_additional_job("c"),
                self.plan_additional_job("d"),
                self.plan_additional_job("e"),
            )

        expected_ids = tuple(sorted(record.job_id for record in records))
        seen_ids = []
        cursor = None
        pages = []
        while True:
            page = self.ledger.list_operator_jobs_for_actor_page(
                actor_principal_id="operator",
                job_kind="candidate-debug-run",
                cursor=cursor,
                limit=2,
            )
            pages.append(page)
            seen_ids.extend(item.job_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(tuple(seen_ids), expected_ids)
        self.assertEqual(tuple(len(page.items) for page in pages), (2, 2, 1))
        self.assertEqual(len(set(seen_ids)), len(seen_ids))
        self.assertTrue(all(record.updated_at == 1_000.0 for record in records))
        self.assertEqual(
            tuple(
                item.job_id
                for item in self.ledger.list_operator_jobs_for_actor(
                    actor_principal_id="operator",
                    job_kind="candidate-debug-run",
                    limit=2,
                )
            ),
            expected_ids[:2],
        )

        first_cursor = pages[0].next_cursor
        assert first_cursor is not None
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.list_operator_jobs_for_actor_page(
                actor_principal_id="operator",
                job_kind="candidate-debug-run",
                states=(OperatorJobState.QUEUED,),
                cursor=first_cursor,
                limit=2,
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.list_operator_jobs_for_actor_page(
                actor_principal_id="other",
                job_kind="candidate-debug-run",
                cursor=first_cursor,
                limit=2,
            )
        with self.assertRaisesRegex(ValueError, "scope digest"):
            OperatorJobActorCursor(
                updated_at=1_000.0,
                job_id="job-b",
                scope_digest="not-a-digest",
            )

    def test_full_selection_and_bounded_result_survive_restart(self) -> None:
        planned = self.plan_job()
        self.assertEqual(planned.owner_id, "job-owner")
        self.assertNotEqual(planned.owner_id, planned.plan.target.source_owner_id)
        self.assertEqual(planned.plan.target.selection, self.selection)
        queued = self.approve_job(planned)
        admission = self.admission(queued)
        starting = self.ledger.begin_operator_job_start(
            operation_id=self.op("begin-start"),
            actor_principal_id="operator",
            job_id=queued.job_id,
            expected_revision=queued.revision,
            admission_lease_id=admission.lease_id,
            admission_holder_id=admission.holder_id,
            admission_fencing_token=admission.fencing_token,
            binding_id="job-binding-a",
            launch_token="job-launch-a",
            provider_kind="local-process",
            evidence_fingerprint="9" * 64,
            launch_request_digest="a" * 64,
        )
        running = self.ledger.mark_operator_job_running(
            operation_id=self.op("running"),
            actor_principal_id="operator",
            job_id=starting.job_id,
            expected_revision=starting.revision,
            launch_token="job-launch-a",
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
        )
        result = OperatorJobResult(
            result_kind="evaluation",
            status="success",
            metrics={"objective": 1.25},
            constraint_results={"feasible": True},
            event_summary={"steps": 12, "termination": "completed"},
            declared_outputs=(),
            logs=(
                OperatorJobLogMetadata(
                    stream="stdout",
                    byte_count=120,
                    line_count=4,
                    truncated=False,
                    content_digest="c" * 64,
                ),
            ),
            details={"evaluation_spec_digest": "d" * 64},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="e" * 64,
            evidence_digest=result.digest,
            detail_digest="f" * 64,
        )
        capture, owner_revision = self.begin_job_capture("success")
        terminal = self.ledger.finish_operator_job(
            operation_id=self.op("finish"),
            actor_principal_id="operator",
            job_id=running.job_id,
            expected_revision=running.revision,
            launch_token="job-launch-a",
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
            change_id=capture.change_id,
            expected_owner_revision=owner_revision,
            additions=(),
            outcome=outcome,
            result=result,
        )
        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(
            terminal.reconciliation_state,
            OperatorJobReconciliationState.CONFIRMED,
        )
        self.assertEqual(terminal.result.result, result)

        self.ledger.close()
        self.ledger = RealmLedger(self.database)
        recovered = self.ledger.read_operator_job(
            actor_principal_id="operator", job_id=terminal.job_id
        )
        self.assertEqual(recovered.plan.target.selection, self.selection)
        self.assertEqual(recovered.plan.target.selection.to_dict(), self.selection.to_dict())
        self.assertEqual(recovered.result.result, result)
        self.assertEqual(recovered.outcome.outcome, outcome)
        self.assertNotIn(str(self.temporary.name), repr(recovered.to_dict()))
        revisions = self.ledger.list_operator_job_revisions(
            actor_principal_id="operator", job_id=recovered.job_id
        )
        self.assertEqual(
            tuple(item.state for item in revisions),
            (
                OperatorJobState.PLANNED,
                OperatorJobState.AWAITING_APPROVAL,
                OperatorJobState.QUEUED,
                OperatorJobState.STARTING,
                OperatorJobState.RUNNING,
                OperatorJobState.SUCCEEDED,
            ),
        )
        self.assertEqual(
            self.ledger.list_operator_jobs(
                actor_principal_id="operator",
                owner_id="job-owner",
                states=(OperatorJobState.SUCCEEDED,),
            ),
            (recovered,),
        )
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="operator",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
                job_kind="candidate-debug-run",
                states=(OperatorJobState.SUCCEEDED,),
                limit=10,
            ),
            (recovered,),
        )
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="operator",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="another-run",
            ),
            (),
        )
        with self.assertRaisesRegex(ValueError, "limit"):
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="operator",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
                limit=201,
            )
        connection = self.ledger._connect()
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exactly one"):
                connection.execute(
                    "UPDATE operator_jobs SET state = 'running' WHERE job_id = ?",
                    (recovered.job_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE operator_jobs SET plan_digest = ?, revision = revision + 1 "
                    "WHERE job_id = ?",
                    ("0" * 64, recovered.job_id),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE operator_job_results SET result_digest = ? WHERE job_id = ?",
                    ("0" * 64, recovered.job_id),
                )
        finally:
            connection.close()

    def test_terminal_sql_binds_declared_output_to_captured_size(self) -> None:
        running, admission, launch_token = self.start_running_job("false-size")
        addition = OwnerMembership(
            "store-a", self.input_ref, OPERATOR_JOB_OUTPUT_ROLE
        )
        capture, owner_revision = self.begin_job_capture(
            "false-size", (addition,)
        )
        result = OperatorJobResult(
            result_kind="evaluation",
            status="success",
            metrics={},
            constraint_results={},
            event_summary={"termination": "completed"},
            declared_outputs=(
                OperatorJobDeclaredOutput(
                    declaration_id="false-size-output",
                    name="result.json",
                    kind="file",
                    content_ref=str(self.input_ref),
                    size_bytes=26,
                    identity_digest="e" * 64,
                ),
            ),
            logs=(),
            details={},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="f" * 64,
            evidence_digest=result.digest,
        )
        operation_id = self.op("finish-false-size-raw")

        def body(connection, txn_id, now):
            self.ledger._commit_owner_change_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                operation_id=operation_id,
                actor_principal_id="operator",
                change_id=capture.change_id,
                expected_owner_revision=owner_revision,
                additions=(addition,),
                removals=(),
            )
            self.ledger._insert_operator_job_outcome(
                connection,
                job_id=running.job_id,
                outcome=outcome,
                actor_principal_id="operator",
                txn_id=txn_id,
                now=now,
            )
            self.ledger._insert_operator_job_result(
                connection,
                job_id=running.job_id,
                result=result,
                actor_principal_id="operator",
                txn_id=txn_id,
                now=now,
            )
            self.ledger._update_operator_job_head(
                connection,
                job_id=running.job_id,
                expected_revision=running.revision,
                revision=running.revision + 1,
                state=OperatorJobState.SUCCEEDED,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                now=now,
            )
            self.ledger._insert_operator_job_revision(
                connection,
                job_id=running.job_id,
                revision=running.revision + 1,
                state=OperatorJobState.SUCCEEDED,
                reconciliation_state=OperatorJobReconciliationState.CONFIRMED,
                operation_kind="operator-job.finish",
                txn_id=txn_id,
                now=now,
            )
            return {}

        with self.assertRaisesRegex(sqlite3.IntegrityError, "exact lifecycle"):
            self.ledger._operate(
                operation_id=operation_id,
                operation_kind="operator-job.finish",
                request={"job_id": running.job_id, "false_size": True},
                body=body,
            )
        recovered = self.ledger.read_operator_job(
            actor_principal_id="operator", job_id=running.job_id
        )
        self.assertEqual(recovered.state, OperatorJobState.RUNNING)
        connection = self.ledger._connect()
        try:
            capture_row = connection.execute(
                "SELECT state FROM owner_transactions WHERE change_id = ?",
                (capture.change_id,),
            ).fetchone()
            self.assertEqual(capture_row["state"], "active")
            output_count = connection.execute(
                "SELECT COUNT(*) FROM owner_memberships "
                "WHERE owner_id = 'job-owner' AND role = ?",
                (OPERATOR_JOB_OUTPUT_ROLE,),
            ).fetchone()[0]
            self.assertEqual(output_count, 0)
        finally:
            connection.close()

    def test_prelaunch_stop_is_terminal_without_fabricated_launch_or_result(self) -> None:
        planned = self.plan_job("cancel")
        cancelled = self.ledger.request_operator_job_stop(
            operation_id=self.op("cancel"),
            actor_principal_id="operator",
            job_id=planned.job_id,
            expected_revision=planned.revision,
            reason_code="user_cancelled",
        )
        self.assertEqual(cancelled.state, OperatorJobState.CANCELLED)
        self.assertIsNone(cancelled.launch_intent)
        self.assertIsNone(cancelled.result)
        self.assertFalse(cancelled.outcome.outcome.started)
        self.assertIsNone(cancelled.outcome.outcome.terminal_proof_digest)

    def test_terminal_cleanup_is_separate_pending_debt_and_idempotent_event(self) -> None:
        planned = self.plan_job("cleanup-debt")
        cancelled = self.ledger.request_operator_job_stop(
            operation_id=self.op("cancel-cleanup-debt"),
            actor_principal_id="operator",
            job_id=planned.job_id,
            expected_revision=planned.revision,
            reason_code="operator_requested",
        )
        self.assertEqual(cancelled.state, OperatorJobState.CANCELLED)
        self.assertEqual(cancelled.cleanup_state, OperatorJobCleanupState.PENDING)
        self.assertIsNone(cancelled.cleanup)
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="operator",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
                cleanup_states=(OperatorJobCleanupState.PENDING,),
            ),
            (cancelled,),
        )

        operation_id = self.op("complete-cleanup-debt")
        arguments = {
            "operation_id": operation_id,
            "actor_principal_id": "operator",
            "job_id": cancelled.job_id,
            "expected_revision": cancelled.revision,
            "evidence": self.cleanup_evidence(
                cancelled, resources=True, admission=False
            ),
        }
        completed = self.ledger.complete_operator_job_cleanup(**arguments)
        replayed = self.ledger.complete_operator_job_cleanup(**arguments)
        self.assertEqual(replayed, completed)
        self.assertEqual(completed.state, OperatorJobState.CANCELLED)
        self.assertEqual(completed.revision, cancelled.revision + 1)
        self.assertEqual(completed.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertIsNotNone(completed.cleanup)
        self.assertEqual(completed.cleanup.evidence.terminal_revision, cancelled.revision)
        revisions = self.ledger.list_operator_job_revisions(
            actor_principal_id="operator", job_id=completed.job_id
        )
        self.assertEqual(revisions[-1].state, OperatorJobState.CANCELLED)
        self.assertEqual(revisions[-1].cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(
            revisions[-1].operation_kind, "operator-job.complete-cleanup"
        )
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="operator",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
                cleanup_states=(OperatorJobCleanupState.PENDING,),
            ),
            (),
        )

        self.ledger.close()
        self.ledger = RealmLedger(self.database)
        recovered = self.ledger.read_operator_job(
            actor_principal_id="operator", job_id=completed.job_id
        )
        self.assertEqual(recovered, completed)

    def test_launched_cleanup_requires_released_exact_admission_fence(self) -> None:
        running, admission, launch_token = self.start_running_job("cleanup-fence")
        result = OperatorJobResult(
            result_kind="evaluation",
            status="success",
            metrics={},
            constraint_results={},
            event_summary={},
            declared_outputs=(),
            logs=(),
            details={},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="4" * 64,
            evidence_digest=result.digest,
        )
        capture, owner_revision = self.begin_job_capture("cleanup-fence")
        terminal = self.ledger.finish_operator_job(
            operation_id=self.op("finish-cleanup-fence"),
            actor_principal_id="operator",
            job_id=running.job_id,
            expected_revision=running.revision,
            launch_token=launch_token,
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
            change_id=capture.change_id,
            expected_owner_revision=owner_revision,
            additions=(),
            outcome=outcome,
            result=result,
        )
        capacity = self.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=terminal.launch_intent.capacity_reservation_id,
        )
        evidence = self.cleanup_evidence(
            terminal, resources=True, admission=True, capacity=True
        )
        with self.assertRaisesRegex(RealmConflict, "incomplete"):
            self.ledger.complete_operator_job_cleanup(
                operation_id=self.op("cleanup-before-release"),
                actor_principal_id="operator",
                job_id=terminal.job_id,
                expected_revision=terminal.revision,
                evidence=evidence,
                launch_token=launch_token,
                admission_lease_id=admission.lease_id,
                admission_holder_id=admission.holder_id,
                admission_fencing_token=admission.fencing_token,
                capacity_reservation_id=capacity.reservation_id,
                capacity_holder_id=capacity.holder_id,
                capacity_fencing_token=capacity.fencing_token,
            )
        released_capacity = self.ledger.release_operator_capacity_reservation(
            operation_id=self.op("release-cleanup-capacity"),
            actor_principal_id="operator",
            reservation_id=capacity.reservation_id,
            holder_id=capacity.holder_id,
            fencing_token=capacity.fencing_token,
        )
        released = self.ledger.release_lease(
            operation_id=self.op("release-cleanup-admission"),
            actor_principal_id="operator",
            lease_id=admission.lease_id,
            holder_id=admission.holder_id,
            fencing_token=admission.fencing_token,
        )
        with self.assertRaisesRegex(RealmConflict, "stale"):
            self.ledger.complete_operator_job_cleanup(
                operation_id=self.op("cleanup-stale-fence"),
                actor_principal_id="operator",
                job_id=terminal.job_id,
                expected_revision=terminal.revision,
                evidence=evidence,
                launch_token=launch_token,
                admission_lease_id=released.lease_id,
                admission_holder_id=released.holder_id,
                admission_fencing_token=released.fencing_token + 1,
                capacity_reservation_id=released_capacity.reservation_id,
                capacity_holder_id=released_capacity.holder_id,
                capacity_fencing_token=released_capacity.fencing_token,
            )
        completed = self.ledger.complete_operator_job_cleanup(
            operation_id=self.op("cleanup-exact-fence"),
            actor_principal_id="operator",
            job_id=terminal.job_id,
            expected_revision=terminal.revision,
            evidence=evidence,
            launch_token=launch_token,
            admission_lease_id=released.lease_id,
            admission_holder_id=released.holder_id,
            admission_fencing_token=released.fencing_token,
            capacity_reservation_id=released_capacity.reservation_id,
            capacity_holder_id=released_capacity.holder_id,
            capacity_fencing_token=released_capacity.fencing_token,
        )
        self.assertEqual(completed.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_job_owner_is_unique_and_source_listing_checks_both_acls(self) -> None:
        planned = self.plan_job("acl")
        with self.assertRaisesRegex(RealmConflict, "already bound"):
            self.plan_job("owner-reuse")

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-source-read"),
            actor_principal_id="operator",
            owner_id="source-run-owner",
            principal_id="other",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="other",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
            ),
            (),
        )

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-job-read"),
            actor_principal_id="operator",
            owner_id="job-owner",
            principal_id="other",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(
            self.ledger.list_operator_jobs_for_source(
                actor_principal_id="other",
                source_owner_id="source-run-owner",
                source_kind="run",
                source_id="run-a",
            ),
            (planned,),
        )

    def test_planning_requires_explicit_job_owner_admin_authority(self) -> None:
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-job-derive"),
            actor_principal_id="operator",
            owner_id="job-owner",
            principal_id="other",
            permission=OwnerPermission.DERIVE,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.plan_operator_job(
                operation_id=self.op("derive-only-plan"),
                actor_principal_id="other",
                job_owner_id="job-owner",
                plan=self.plan,
                job_id="job-delegated",
            )

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-job-admin"),
            actor_principal_id="operator",
            owner_id="job-owner",
            principal_id="other",
            permission=OwnerPermission.ADMIN,
        )
        planned = self.ledger.plan_operator_job(
            operation_id=self.op("admin-plan"),
            actor_principal_id="other",
            job_owner_id="job-owner",
            plan=self.plan,
            job_id="job-delegated",
        )
        self.assertEqual(planned.created_by_principal_id, "other")

    def test_launch_trigger_rejects_forged_creator_and_backend(self) -> None:
        queued = self.approve_job(self.plan_job("launch-sql"))
        admission = self.admission(queued)
        capacity = self.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                queued.plan.backend_realm, queued.job_id
            ),
        )

        def attempt_raw_launch(*, actor: str, provider: str, label: str) -> None:
            def body(connection, txn_id, now):
                connection.execute(
                    "INSERT INTO operator_job_launch_intents("
                    "job_id, plan_digest, capacity_reservation_id, "
                    "capacity_holder_id, capacity_fencing_token, admission_lease_id, "
                    "admission_holder_id, admission_fencing_token, binding_id, "
                    "launch_token, provider_kind, "
                    "evidence_fingerprint, launch_request_digest, "
                    "created_by_principal_id, created_txn_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        queued.job_id,
                        queued.plan_digest,
                        capacity.reservation_id,
                        capacity.holder_id,
                        capacity.fencing_token,
                        admission.lease_id,
                        admission.holder_id,
                        admission.fencing_token,
                        f"raw-binding-{label}",
                        f"raw-launch-{label}",
                        provider,
                        "9" * 64,
                        "a" * 64,
                        actor,
                        txn_id,
                        now,
                    ),
                )
                return {}

            with self.assertRaisesRegex(sqlite3.IntegrityError, "exact admission"):
                self.ledger._operate(
                    operation_id=self.op(f"raw-launch-{label}"),
                    operation_kind="operator-job.begin-start",
                    request={"label": label},
                    body=body,
                )

        attempt_raw_launch(
            actor="other", provider="local-process", label="creator"
        )
        attempt_raw_launch(
            actor="operator", provider="forged-provider", label="provider"
        )
        recovered = self.ledger.read_operator_job(
            actor_principal_id="operator", job_id=queued.job_id
        )
        self.assertEqual(recovered.state, OperatorJobState.QUEUED)
        self.assertIsNone(recovered.launch_intent)

    def test_terminal_side_facts_cannot_commit_without_terminal_revision(self) -> None:
        running, admission, launch_token = self.start_running_job("orphan")
        capture, owner_revision = self.begin_job_capture("orphan")
        result = OperatorJobResult(
            result_kind="evaluation",
            status="success",
            metrics={},
            constraint_results={},
            event_summary={"termination": "completed"},
            declared_outputs=(),
            logs=(),
            details={},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="b" * 64,
            evidence_digest=result.digest,
        )
        operation_id = self.op("orphan-terminal-facts")

        def body(connection, txn_id, now):
            self.ledger._commit_owner_change_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                operation_id=operation_id,
                actor_principal_id="operator",
                change_id=capture.change_id,
                expected_owner_revision=owner_revision,
                additions=(),
                removals=(),
            )
            self.ledger._insert_operator_job_outcome(
                connection,
                job_id=running.job_id,
                outcome=outcome,
                actor_principal_id="operator",
                txn_id=txn_id,
                now=now,
            )
            self.ledger._insert_operator_job_result(
                connection,
                job_id=running.job_id,
                result=result,
                actor_principal_id="operator",
                txn_id=txn_id,
                now=now,
            )
            return {}

        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            self.ledger._operate(
                operation_id=operation_id,
                operation_kind="operator-job.finish",
                request={"job_id": running.job_id},
                body=body,
            )
        recovered = self.ledger.read_operator_job(
            actor_principal_id="operator", job_id=running.job_id
        )
        self.assertEqual(recovered.state, OperatorJobState.RUNNING)
        self.assertIsNone(recovered.outcome)
        self.assertIsNone(recovered.result)
        connection = self.ledger._connect()
        try:
            capture_row = connection.execute(
                "SELECT state, committed_txn_id FROM owner_transactions "
                "WHERE change_id = ?",
                (capture.change_id,),
            ).fetchone()
            self.assertEqual(capture_row["state"], "active")
            self.assertIsNone(capture_row["committed_txn_id"])
        finally:
            connection.close()

    def test_finish_atomically_commits_exact_outputs_and_replays(self) -> None:
        running, admission, launch_token = self.start_running_job("outputs")
        addition = OwnerMembership(
            "store-a", self.input_ref, OPERATOR_JOB_OUTPUT_ROLE
        )
        capture, owner_revision = self.begin_job_capture("outputs", (addition,))
        result = OperatorJobResult(
            result_kind="evaluation",
            status="success",
            metrics={"objective": 2.0},
            constraint_results={"feasible": True},
            event_summary={"termination": "completed"},
            declared_outputs=(
                OperatorJobDeclaredOutput(
                    declaration_id="primary-output",
                    name="result.json",
                    kind="file",
                    content_ref=str(self.input_ref),
                    size_bytes=25,
                    identity_digest="c" * 64,
                    media_type="application/json",
                ),
            ),
            logs=(),
            details={},
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.SUCCEEDED,
            code="completed",
            started=True,
            disposition=OperatorJobTerminalDisposition.EXITED,
            terminal_proof_digest="d" * 64,
            evidence_digest=result.digest,
        )
        operation_id = self.op("finish-outputs")
        arguments = {
            "operation_id": operation_id,
            "actor_principal_id": "operator",
            "job_id": running.job_id,
            "expected_revision": running.revision,
            "launch_token": launch_token,
            "admission_lease_id": admission.lease_id,
            "admission_fencing_token": admission.fencing_token,
            "change_id": capture.change_id,
            "expected_owner_revision": owner_revision,
            "additions": (addition,),
            "outcome": outcome,
            "result": result,
        }
        with self.assertRaisesRegex(ValueError, OPERATOR_JOB_OUTPUT_ROLE):
            self.ledger.finish_operator_job(
                **{
                    **arguments,
                    "operation_id": self.op("finish-wrong-output-role"),
                    "additions": (
                        OwnerMembership(
                            "store-a", self.input_ref, "unrelated-output"
                        ),
                    ),
                }
            )
        terminal = self.ledger.finish_operator_job(**arguments)
        replayed = self.ledger.finish_operator_job(**arguments)
        self.assertEqual(replayed, terminal)
        owner = self.ledger.read_owner(
            actor_principal_id="operator", owner_id="job-owner"
        )
        self.assertEqual(owner.revision, owner_revision + 1)

        connection = self.ledger._connect()
        try:
            anchors = connection.execute(
                "SELECT capture.state, capture.committed_txn_id, "
                "outcome.created_txn_id, revision.txn_id "
                "FROM owner_transactions capture "
                "JOIN operator_job_outcomes outcome ON outcome.job_id = ? "
                "JOIN operator_job_revisions revision "
                "ON revision.job_id = outcome.job_id "
                "AND revision.state = outcome.status "
                "WHERE capture.change_id = ?",
                (running.job_id, capture.change_id),
            ).fetchone()
            self.assertEqual(anchors["state"], "committed")
            self.assertEqual(
                anchors["committed_txn_id"], anchors["created_txn_id"]
            )
            self.assertEqual(anchors["created_txn_id"], anchors["txn_id"])
            membership_count = connection.execute(
                "SELECT COUNT(*) FROM owner_memberships "
                "WHERE owner_id = 'job-owner' AND store_id = 'store-a' "
                "AND content_ref = ? AND role = ? AND removed_revision IS NULL",
                (str(self.input_ref), OPERATOR_JOB_OUTPUT_ROLE),
            ).fetchone()[0]
            self.assertEqual(membership_count, 1)
        finally:
            connection.close()

        removal = self.ledger.begin_owner_change(
            operation_id=self.op("remove-expired-output"),
            actor_principal_id="operator",
            owner_id="job-owner",
            expected_owner_revision=owner.revision,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("commit-expired-output-removal"),
            actor_principal_id="operator",
            change_id=removal.change_id,
            expected_owner_revision=owner.revision,
            additions=(),
            removals=(addition,),
        )
        self.assertEqual(
            self.ledger.read_operator_job(
                actor_principal_id="operator", job_id=running.job_id
            ),
            terminal,
        )
        self.assertEqual(self.ledger.finish_operator_job(**arguments), terminal)

    def test_terminal_tree_output_selection_resolves_and_keeps_without_paths(
        self,
    ) -> None:
        terminal, addition = self.finish_job_with_declared_output(kind="tree")
        selection = self.ledger.mint_operator_job_output_selection(
            actor_principal_id="operator",
            job_id=terminal.job_id,
            output_id="primary-output",
        )
        self.assertEqual(selection.source_kind, "operator-job")
        self.assertEqual(selection.source_id, terminal.job_id)
        self.assertEqual(selection.source_owner_id, terminal.owner_id)
        self.assertEqual(selection.source_revision, terminal.revision)
        self.assertEqual(selection.entity_id, "primary-output")
        self.assertEqual(selection.entity_ref, str(addition.content_ref))
        self.assertNotIn(str(self.temporary.name), json.dumps(selection.to_dict()))

        self.ledger.close()
        self.ledger = RealmLedger(self.database)
        resolution = self.ledger.resolve_selection(
            actor_principal_id="operator", selection=selection
        )
        self.assertTrue(resolution.eligibility.eligible)
        self.assertEqual(resolution.root, addition)
        kept = self.ledger.keep_selection_as_workspace(
            operation_id=self.op("keep-operator-job-tree"),
            actor_principal_id="operator",
            selection=selection,
            title="Retained preview output",
        )
        self.assertIsNotNone(kept.workspace)
        self.assertEqual(
            kept.workspace.revision.root_store_id, addition.store_id
        )
        self.assertEqual(
            kept.workspace.revision.root_ref, addition.content_ref
        )

    def test_terminal_file_output_selection_is_not_an_editable_workspace(
        self,
    ) -> None:
        terminal, addition = self.finish_job_with_declared_output(kind="file")
        selection = self.ledger.mint_operator_job_output_selection(
            actor_principal_id="operator",
            job_id=terminal.job_id,
            output_id="primary-output",
        )
        content_resolution = self.ledger.resolve_selection_for_content_read(
            actor_principal_id="operator",
            selection=selection,
        )
        self.assertTrue(content_resolution.eligibility.eligible)
        self.assertEqual(content_resolution.root, addition)
        resolution = self.ledger.resolve_selection(
            actor_principal_id="operator", selection=selection
        )
        self.assertFalse(resolution.eligibility.supported)
        self.assertFalse(resolution.eligibility.eligible)
        self.assertEqual(
            resolution.eligibility.code,
            "operator_job_file_output_not_tree",
        )
        self.assertEqual(
            resolution.eligibility.reason,
            "This saved file is result evidence, not an editable project folder.",
        )
        kept = self.ledger.keep_selection_as_workspace(
            operation_id=self.op("keep-operator-job-file"),
            actor_principal_id="operator",
            selection=selection,
            title="File output",
        )
        self.assertIsNone(kept.workspace)
        self.assertEqual(kept.eligibility, resolution.eligibility)

    def test_operator_job_output_selection_fails_closed_on_forgery_and_loss(
        self,
    ) -> None:
        terminal, addition = self.finish_job_with_declared_output(kind="tree")
        selection = self.ledger.mint_operator_job_output_selection(
            actor_principal_id="operator",
            job_id=terminal.job_id,
            output_id="primary-output",
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.mint_operator_job_output_selection(
                actor_principal_id="other",
                job_id=terminal.job_id,
                output_id="primary-output",
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.mint_operator_job_output_selection(
                actor_principal_id="operator",
                job_id=terminal.job_id,
                output_id="another-output",
            )

        values = {
            "kind": selection.kind,
            "source_kind": selection.source_kind,
            "source_id": selection.source_id,
            "source_owner_id": selection.source_owner_id,
            "source_revision": selection.source_revision,
            "owner_revision": selection.owner_revision,
            "source_sequence": selection.source_sequence,
            "entity_sequence": selection.entity_sequence,
            "entity_id": selection.entity_id,
            "entity_ref": selection.entity_ref,
            "context_digest": selection.context_digest,
            "relative_path": selection.relative_path,
        }

        def forged(**overrides):
            return SelectionRef.build(**{**values, **overrides})

        forged_selections = (
            forged(source_id="another-job"),
            forged(source_revision=selection.source_revision + 1),
            forged(owner_revision=selection.owner_revision + 1),
            forged(entity_id="another-output"),
            forged(
                entity_ref=str(SnapshotRef.from_manifest_bytes(b"other tree"))
            ),
            forged(context_digest="f" * 64),
        )
        for forged_selection in forged_selections:
            with self.subTest(selection=forged_selection.to_dict()):
                with self.assertRaises(RealmNotFound):
                    self.ledger.resolve_selection(
                        actor_principal_id="operator",
                        selection=forged_selection,
                    )

        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE content_objects SET trust_state = 'unverified' "
                "WHERE store_id = ? AND content_ref = ?",
                (addition.store_id, str(addition.content_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        unavailable = self.ledger.resolve_selection(
            actor_principal_id="operator", selection=selection
        )
        self.assertFalse(unavailable.eligibility.eligible)
        self.assertEqual(
            unavailable.eligibility.code, "selection_content_unavailable"
        )

        connection = self.ledger._connect()
        try:
            connection.execute(
                "UPDATE content_objects SET trust_state = 'verified_local' "
                "WHERE store_id = ? AND content_ref = ?",
                (addition.store_id, str(addition.content_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        owner = self.ledger.read_owner(
            actor_principal_id="operator", owner_id=terminal.owner_id
        )
        removal = self.ledger.begin_owner_change(
            operation_id=self.op("remove-operator-job-output"),
            actor_principal_id="operator",
            owner_id=terminal.owner_id,
            expected_owner_revision=owner.revision,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("commit-operator-job-output-removal"),
            actor_principal_id="operator",
            change_id=removal.change_id,
            expected_owner_revision=owner.revision,
            additions=(),
            removals=(addition,),
        )
        unavailable = self.ledger.resolve_selection(
            actor_principal_id="operator", selection=selection
        )
        self.assertFalse(unavailable.eligibility.eligible)
        self.assertIsNone(unavailable.root)

    def test_revision_admission_and_launch_fences_reject_stale_transitions(self) -> None:
        queued = self.approve_job(self.plan_job("fenced"))
        admission = self.admission(queued)
        with self.assertRaisesRegex(RealmConflict, "revision"):
            self.ledger.begin_operator_job_start(
                operation_id=self.op("stale-revision"),
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=queued.revision - 1,
                admission_lease_id=admission.lease_id,
                admission_holder_id=admission.holder_id,
                admission_fencing_token=admission.fencing_token,
                binding_id="binding-fenced",
                launch_token="launch-fenced",
                provider_kind="local-process",
                evidence_fingerprint="9" * 64,
                launch_request_digest="a" * 64,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.begin_operator_job_start(
                operation_id=self.op("stale-admission"),
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=queued.revision,
                admission_lease_id=admission.lease_id,
                admission_holder_id=admission.holder_id,
                admission_fencing_token=admission.fencing_token + 1,
                binding_id="binding-fenced",
                launch_token="launch-fenced",
                provider_kind="local-process",
                evidence_fingerprint="9" * 64,
                launch_request_digest="a" * 64,
            )

    def test_stop_stays_visible_until_exact_terminal_proof(self) -> None:
        queued = self.approve_job(self.plan_job("stop"))
        admission = self.admission(queued)
        starting = self.ledger.begin_operator_job_start(
            operation_id=self.op("begin-stop-job"),
            actor_principal_id="operator",
            job_id=queued.job_id,
            expected_revision=queued.revision,
            admission_lease_id=admission.lease_id,
            admission_holder_id=admission.holder_id,
            admission_fencing_token=admission.fencing_token,
            binding_id="binding-stop",
            launch_token="launch-stop",
            provider_kind="local-process",
            evidence_fingerprint="9" * 64,
            launch_request_digest="a" * 64,
        )
        running = self.ledger.mark_operator_job_running(
            operation_id=self.op("running-stop-job"),
            actor_principal_id="operator",
            job_id=starting.job_id,
            expected_revision=starting.revision,
            launch_token="launch-stop",
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
        )
        stopping = self.ledger.request_operator_job_stop(
            operation_id=self.op("request-stop-job"),
            actor_principal_id="operator",
            job_id=running.job_id,
            expected_revision=running.revision,
            reason_code="user_cancelled",
        )
        self.assertEqual(stopping.state, OperatorJobState.STOPPING)
        self.assertIsNone(stopping.outcome)
        unconfirmed = self.ledger.mark_operator_job_stopping_unconfirmed(
            operation_id=self.op("unconfirmed-stop-job"),
            actor_principal_id="operator",
            job_id=stopping.job_id,
            expected_revision=stopping.revision,
            launch_token="launch-stop",
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
        )
        self.assertEqual(unconfirmed.state, OperatorJobState.STOPPING)
        self.assertEqual(
            unconfirmed.reconciliation_state,
            OperatorJobReconciliationState.UNCONFIRMED,
        )
        result = OperatorJobResult(
            result_kind="evaluation",
            status="cancelled",
            metrics={},
            constraint_results={},
            event_summary={"termination": "confirmed"},
            declared_outputs=(),
            logs=(),
            details={},
        )
        capture, owner_revision = self.begin_job_capture("stop")
        cancelled = self.ledger.finish_operator_job(
            operation_id=self.op("finish-stop-job"),
            actor_principal_id="operator",
            job_id=unconfirmed.job_id,
            expected_revision=unconfirmed.revision,
            launch_token="launch-stop",
            admission_lease_id=admission.lease_id,
            admission_fencing_token=admission.fencing_token,
            change_id=capture.change_id,
            expected_owner_revision=owner_revision,
            additions=(),
            outcome=OperatorJobOutcome(
                status=OperatorJobTerminalStatus.CANCELLED,
                code="user_cancelled",
                started=True,
                disposition=OperatorJobTerminalDisposition.KILLED,
                terminal_proof_digest="b" * 64,
                evidence_digest=result.digest,
            ),
            result=result,
        )
        self.assertEqual(cancelled.state, OperatorJobState.CANCELLED)
        self.assertEqual(
            cancelled.reconciliation_state,
            OperatorJobReconciliationState.CONFIRMED,
        )

    def test_result_rejects_host_paths_and_unbounded_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "path field"):
            OperatorJobResult(
                result_kind="evaluation",
                status="failed",
                metrics={},
                constraint_results={},
                event_summary={"cwd": "/private/tmp/worker"},
                declared_outputs=(),
                logs=(),
                details={},
            )
        payload = self.plan.to_dict()
        payload["target"]["selection"]["source_revision"] += 1
        with self.assertRaises(RealmIntegrityError):
            OperatorJobLaunchPlan.from_dict(payload)

    def test_sql_normalized_plan_key_order_reads_semantically(self) -> None:
        reordered = dict(reversed(tuple(self.plan.to_dict().items())))
        raw = json.dumps(
            reordered,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        connection = self.ledger._connect()
        try:
            normalized = connection.execute("SELECT json(?)", (raw,)).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(
            normalized, canonical_json_bytes(self.plan.to_dict()).decode("utf-8")
        )
        self.assertEqual(
            OperatorJobLaunchPlan.from_dict(
                _json_object(normalized, "operator job plan")
            ),
            self.plan,
        )


if __name__ == "__main__":
    unittest.main()
