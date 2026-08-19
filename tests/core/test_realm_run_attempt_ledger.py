from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.attempts import (
    AttemptEnvelope,
    AttemptFinalization,
    CapturedArtifact,
    EvaluationSpec,
    OutputDeclaration,
)
from optpilot.realm.content import (
    AllowedFileSource,
    AllowedTreeSource,
    LocalContentStore,
)
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.run_attempt_records import (
    RUN_ARTIFACT_ROLE,
    RunAttemptHeartbeatAuthorityReceipt,
)
from optpilot.realm.run_closure import (
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
)
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_snapshot import RunLedgerSnapshot
from optpilot.run_control_manifest import RetryPolicy
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunAttemptLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        for principal in ("operator", "other"):
            self.ledger.register_principal(
                operation_id=f"attempt/principal/{principal}",
                principal_id=principal,
                kind="human",
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="attempt/store/local-a",
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
            prefix="run-attempt",
        )
        self.manifest = replace(
            prepare_test_run_control_manifest(self.closure, max_trials=4),
            retry_policy=RetryPolicy(
                max_attempts=2,
                retryable_outcomes=("failed",),
            ),
        )
        _, self.definition_bindings = prepare_test_run_definition(
            self.closure, self.manifest, self.closure_bindings
        )
        self.created = self._create_run()
        self.counter = 0
        self.admission = self._admit_trials()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"run-attempt/{self.counter}/{label}"

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_run(
        self,
        *,
        operation_id: str = "run-attempt/run/create",
        run_id: str = "run-a",
        owner_id: str = "run-owner-a",
        closure: RunEvaluationClosure | None = None,
        manifest=None,
        closure_bindings: tuple[OwnerMembership, ...] | None = None,
        source_owner_id: str | None = None,
        source_owner_revision: int | None = None,
    ):
        selected_closure = self.closure if closure is None else closure
        selected_manifest = self.manifest if manifest is None else manifest
        selected_bindings = (
            self.closure_bindings
            if closure_bindings is None
            else closure_bindings
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            selected_closure, selected_manifest, selected_bindings
        )
        return self.ledger.create_run_namespace(
            operation_id=operation_id,
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=(
                self.source_owner_id
                if source_owner_id is None
                else source_owner_id
            ),
            expected_source_owner_revision=(
                self.source_owner_revision
                if source_owner_revision is None
                else source_owner_revision
            ),
            run_id=run_id,
            owner_id=owner_id,
        )

    def _admit_trials(self):
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        plan = RunAdmissionPlan(
            (
                CandidateAdmission(
                    "candidate-a",
                    envelope,
                    lineage={"parents": []},
                    generator={"method_id": "method-a"},
                ),
            ),
            (
                LogicalTrialAdmission(
                    "trial-default",
                    "candidate-a",
                    seed=None,
                    repetition_index=0,
                ),
                LogicalTrialAdmission(
                    "trial-explicit",
                    "candidate-a",
                    seed=17,
                    repetition_index=2,
                ),
            ),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        return self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
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

    def _prepare_file_candidate_attempt(self):
        (
            closure,
            closure_bindings,
            closure_source_owner_id,
            closure_source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="run-attempt-file",
            candidate_contract={"format": "files"},
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=1)
        created = self._create_run(
            operation_id=self.op("file-run-create"),
            run_id="run-files",
            owner_id="run-files-owner",
            closure=closure,
            manifest=manifest,
            closure_bindings=closure_bindings,
            source_owner_id=closure_source_owner_id,
            source_owner_revision=closure_source_owner_revision,
        )

        candidate_source_owner_id = "run-attempt-file-candidate-source"
        self.ledger.create_owner(
            operation_id=self.op("file-candidate-source-owner"),
            owner_id=candidate_source_owner_id,
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "run-attempt-file-candidate"
        source.mkdir()
        (source / "run.py").write_text(
            "print('file candidate')\n", encoding="utf-8"
        )
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("file-candidate-source-begin"),
            actor_principal_id="operator",
            owner_id=candidate_source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        capture = self.store.capture(
            change_id=source_change.change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=source_change.change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_tree(source=AllowedTreeSource(source))
        source_membership = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            "candidate-source",
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("file-candidate-source-hold"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            memberships=(source_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("file-candidate-source-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )

        candidate_binding = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            RUN_CANDIDATE_ROLE,
        )
        run_change = self.ledger.begin_owner_change(
            operation_id=self.op("file-candidate-run-begin"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("file-candidate-run-hold"),
            actor_principal_id="operator",
            change_id=run_change.change_id,
            memberships=(candidate_binding,),
            source_owner_id=candidate_source_owner_id,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py"},
            content_refs=(sealed.snapshot_ref,),
        )
        admission = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("file-candidate-admit"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=run_change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        "candidate-files",
                        envelope,
                        lineage={"parents": []},
                        generator={"method_id": "method-files"},
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        "trial-files", "candidate-files"
                    ),
                ),
            ),
            content_bindings=(candidate_binding,),
        )
        prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("file-attempt-prepare"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            logical_trial_id="trial-files",
            attempt_id="attempt-files",
            expected_run_revision=1,
            attempt_ttl_seconds=60,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        return created, admission, prepared, candidate_binding

    def _controller_arguments(self) -> dict[str, object]:
        controller = self.created.controller_lease
        return {
            "controller_lease_id": controller.lease_id,
            "controller_holder_id": controller.holder_id,
            "controller_fencing_token": controller.fencing_token,
        }

    def prepare(
        self,
        *,
        operation_id: str,
        expected_run_revision: int,
        logical_trial_id: str = "trial-default",
        attempt_id: str = "attempt-1",
        attempt_ttl_seconds: float | None = 60,
    ):
        arguments: dict[str, object] = {
            "operation_id": operation_id,
            "actor_principal_id": "operator",
            "run_id": self.created.run.run_id,
            "logical_trial_id": logical_trial_id,
            "attempt_id": attempt_id,
            "expected_run_revision": expected_run_revision,
            **self._controller_arguments(),
        }
        if attempt_ttl_seconds is not None:
            arguments["attempt_ttl_seconds"] = attempt_ttl_seconds
        return self.ledger.prepare_run_attempt(**arguments)

    def confirm(
        self,
        prepared,
        *,
        operation_id: str,
        expected_run_revision: int,
        launch_token: str | None = None,
    ):
        return self.ledger.confirm_run_attempt_launch(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=expected_run_revision,
            launch_token=(
                prepared.attempt.launch_token
                if launch_token is None
                else launch_token
            ),
            binding_id=prepared.attempt.binding_id,
            evidence_fingerprint="f" * 64,
            launch_request_digest="d" * 64,
            **self._controller_arguments(),
        )

    def adopt(
        self,
        prepared,
        finalization: AttemptFinalization,
        *,
        operation_id: str,
        expected_run_revision: int,
        expected_owner_revision: int,
        change_id: str | None = None,
    ):
        return self.ledger.adopt_run_attempt(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=expected_run_revision,
            expected_owner_revision=expected_owner_revision,
            change_id=(
                prepared.attempt.capture_change_id
                if change_id is None
                else change_id
            ),
            finalization=finalization,
            **self._controller_arguments(),
        )

    @staticmethod
    def envelope(
        prepared,
        *,
        outcome: str = "success",
        declarations: tuple[OutputDeclaration, ...] = (),
    ) -> AttemptEnvelope:
        return AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome=outcome,
            phase="environment_evaluation",
            wall_clock_seconds=0.25,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 1}, "metadata": {}},
            metric_values={"score": 3.5} if outcome == "success" else {},
            constraint_results={},
            output_declarations=declarations,
            event_summary={"count": 1},
            execution_metadata={"worker": "test"},
            error=(
                {}
                if outcome == "success"
                else {
                    "phase": "environment_evaluation",
                    "type": "RuntimeError",
                    "message": "evaluation failed",
                }
            ),
        )

    def finalization(
        self,
        prepared,
        *,
        outcome: str = "success",
        code: str | None = None,
        declarations: tuple[OutputDeclaration, ...] = (),
        captures: tuple[CapturedArtifact, ...] = (),
    ) -> AttemptFinalization:
        envelope = self.envelope(
            prepared,
            outcome=outcome,
            declarations=declarations,
        )
        return AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            effective_outcome=outcome,
            effective_code=code,
            captured_artifacts=captures,
            envelope=envelope,
        )

    def capture_blob_artifact(
        self,
        prepared,
        *,
        name: str,
        contents: str,
        visibility: str = "operator",
    ) -> tuple[OutputDeclaration, CapturedArtifact, OwnerMembership]:
        source = self.root / f"artifact-{name}"
        source.mkdir()
        filename = f"{name}.json"
        (source / filename).write_text(contents, encoding="utf-8")
        capture = self.store.capture(
            change_id=prepared.attempt.capture_change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=prepared.attempt.capture_change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_blob(
            source=AllowedFileSource(source, filename)
        )
        membership = OwnerMembership(
            self.store.store_id,
            sealed.blob_ref,
            RUN_ARTIFACT_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op(f"hold-{name}"),
            actor_principal_id="operator",
            change_id=prepared.attempt.capture_change_id,
            memberships=(membership,),
        )
        declaration = OutputDeclaration(
            declaration_id=f"environment:{name}",
            name=name,
            path=filename,
            kind="file",
            media_type="application/json",
            metadata={"kind": "result"},
        )
        artifact = CapturedArtifact(
            declaration=declaration,
            content_ref=str(sealed.blob_ref),
            size_bytes=sealed.publication.logical_bytes,
            bindings=(
                {
                    "store_id": self.store.store_id,
                    "content_ref": str(sealed.blob_ref),
                },
            ),
            visibility=visibility,
            metadata={"capture": "verified"},
        )
        return declaration, artifact, membership

    def test_prepare_derives_canonical_evaluation_specs_and_is_replayable(self) -> None:
        operation_id = self.op("prepare-default")
        prepared = self.prepare(
            operation_id=operation_id,
            expected_run_revision=1,
            attempt_ttl_seconds=None,
        )
        replay = self.prepare(
            operation_id=operation_id,
            expected_run_revision=1,
            attempt_ttl_seconds=None,
        )

        expected_spec = EvaluationSpec(
            environment_id=self.closure.environment_revision.environment_id,
            environment_revision_digest=self.closure.environment_revision.digest,
            prepared_runtime_digest=self.closure.prepared_runtime.digest,
            candidate={
                "candidate_id": "candidate-a",
                "format": "parameters",
                "spec": {"x": 1},
                "lineage": {"parents": []},
                "generator": {"method_id": "method-a"},
                "validation": {},
                "materialization": {},
            },
            objective=self.closure.evaluation_template.objective,
            resource_profile=self.closure.evaluation_template.resource_profile,
            sandbox_spec=self.closure.evaluation_template.sandbox_spec,
            candidate_ref=str(
                self.admission.candidates[0].admission.envelope.candidate_ref
            ),
            seed=self.closure.evaluation_template.default_seed,
            repetition_index=0,
        )
        self.assertEqual(replay, prepared)
        self.assertEqual(prepared.attempt.evaluation_spec, expected_spec)
        self.assertEqual(
            prepared.attempt.prepared_runtime_digest,
            self.closure.prepared_runtime.digest,
        )
        self.assertEqual(prepared.attempt.state, "prepared")
        self.assertEqual(prepared.attempt.attempt_index, 1)
        self.assertEqual(prepared.attempt_transition.to_state, "prepared")
        self.assertEqual(prepared.logical_transition.to_state, "queued")
        self.assertEqual(prepared.revision.operation_kind, "run.attempt.prepare")
        self.assertEqual(prepared.run.next_sequence, prepared.revision.next_sequence)
        self.assertEqual(
            prepared.attempt_lease.parent_lease_id,
            self.created.controller_lease.lease_id,
        )
        self.assertEqual(prepared.attempt_lease.audience, "realm-ledger")

        explicit = self.prepare(
            operation_id=self.op("prepare-explicit"),
            expected_run_revision=2,
            logical_trial_id="trial-explicit",
            attempt_id="attempt-explicit-1",
        )
        self.assertEqual(explicit.attempt.evaluation_spec.seed, 17)
        self.assertEqual(explicit.attempt.evaluation_spec.repetition_index, 2)
        self.assertEqual(
            self.ledger.read_run_attempt(
                actor_principal_id="operator",
                run_id="run-a",
                attempt_id="attempt-1",
            ),
            prepared.attempt,
        )
        self.assertEqual(
            self.ledger.list_run_attempts(
                actor_principal_id="operator", run_id="run-a"
            ),
            (prepared.attempt, explicit.attempt),
        )
        self.assertEqual(
            self.ledger.list_run_attempts(
                actor_principal_id="operator",
                run_id="run-a",
                logical_trial_id="trial-default",
            ),
            (prepared.attempt,),
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt(
                actor_principal_id="other",
                run_id="run-a",
                attempt_id="attempt-1",
            )

    def test_heartbeat_authority_is_exact_recoverable_and_parent_capped(
        self,
    ) -> None:
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_heartbeat_authority(
                actor_principal_id="other",
                run_id="run-a",
                attempt_id="attempt-1",
            )
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-recovery"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.DERIVE,
        )
        prepared = self.prepare(
            operation_id=self.op("prepare-heartbeat"),
            expected_run_revision=1,
            attempt_ttl_seconds=5,
        )
        self.ledger.register_principal(
            operation_id=self.op("register-stranger"),
            principal_id="stranger",
            kind="human",
        )
        self.assertEqual(
            prepared.controller_lease.lease_id,
            prepared.run.controller_lease_id,
        )
        self.assertEqual(
            prepared.capture_change.change_id,
            prepared.attempt.capture_change_id,
        )
        self.assertEqual(
            prepared.capture_retention_lease.lease_id,
            prepared.capture_change.retention_lease_id,
        )
        recovered = self.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="other",
            run_id="run-a",
            attempt_id="attempt-1",
        )
        self.assertEqual(recovered.run, prepared.run)
        self.assertEqual(recovered.attempt, prepared.attempt)
        self.assertEqual(recovered.controller_lease, prepared.controller_lease)
        self.assertEqual(recovered.attempt_lease, prepared.attempt_lease)
        self.assertEqual(recovered.capture_change, prepared.capture_change)
        self.assertEqual(
            recovered.capture_retention_lease,
            prepared.capture_retention_lease,
        )
        self.assertEqual(recovered.candidate, self.admission.candidates[0])
        self.assertEqual(recovered.candidate_content_bindings, ())
        self.assertEqual(
            RunAttemptHeartbeatAuthorityReceipt.from_dict(recovered.to_dict()),
            recovered,
        )

        capture = recovered.capture_retention_lease
        with self.assertRaises(RealmNotFound):
            self.ledger.heartbeat_owner_change(
                operation_id=self.op("unauthorized-capture-heartbeat"),
                actor_principal_id="stranger",
                change_id=recovered.capture_change.change_id,
                retention_lease_id=capture.lease_id,
                holder_id=capture.holder_id,
                fencing_token=capture.fencing_token,
                ttl_seconds=60,
            )
        with self.assertRaisesRegex(
            RealmConflict, "typed owner-change heartbeat"
        ):
            self.ledger.heartbeat_lease(
                operation_id=self.op("generic-capture-heartbeat"),
                actor_principal_id="other",
                lease_id=capture.lease_id,
                holder_id=capture.holder_id,
                fencing_token=capture.fencing_token,
                ttl_seconds=60,
            )
        for field, value in (
            ("holder_id", "wrong-holder"),
            ("fencing_token", capture.fencing_token + 1),
        ):
            arguments = {
                "operation_id": self.op(f"wrong-{field}"),
                "actor_principal_id": "other",
                "change_id": recovered.capture_change.change_id,
                "retention_lease_id": capture.lease_id,
                "holder_id": capture.holder_id,
                "fencing_token": capture.fencing_token,
                "ttl_seconds": 60,
            }
            arguments[field] = value
            with self.subTest(field=field), self.assertRaises(RealmConflict):
                self.ledger.heartbeat_owner_change(**arguments)
        with self.assertRaises(RealmNotFound):
            self.ledger.heartbeat_owner_change(
                operation_id=self.op("wrong-retention-id"),
                actor_principal_id="other",
                change_id=recovered.capture_change.change_id,
                retention_lease_id="not-the-retention-lease",
                holder_id=capture.holder_id,
                fencing_token=capture.fencing_token,
                ttl_seconds=60,
            )

        first_operation = self.op("capture-round-before-parents")
        first = self.ledger.heartbeat_owner_change(
            operation_id=first_operation,
            actor_principal_id="other",
            change_id=recovered.capture_change.change_id,
            retention_lease_id=capture.lease_id,
            holder_id=capture.holder_id,
            fencing_token=capture.fencing_token,
            ttl_seconds=120,
        )
        self.assertEqual(
            first.retention_lease.expires_at,
            recovered.attempt_lease.expires_at,
        )
        self.assertEqual(
            self.ledger.heartbeat_owner_change(
                operation_id=first_operation,
                actor_principal_id="other",
                change_id=recovered.capture_change.change_id,
                retention_lease_id=capture.lease_id,
                holder_id=capture.holder_id,
                fencing_token=capture.fencing_token,
                ttl_seconds=120,
            ),
            first,
        )

        controller = self.ledger.heartbeat_lease(
            operation_id=self.op("controller-heartbeat"),
            actor_principal_id="other",
            lease_id=recovered.controller_lease.lease_id,
            holder_id=recovered.controller_lease.holder_id,
            fencing_token=recovered.controller_lease.fencing_token,
            ttl_seconds=180,
        )
        attempt = self.ledger.heartbeat_lease(
            operation_id=self.op("attempt-heartbeat"),
            actor_principal_id="other",
            lease_id=recovered.attempt_lease.lease_id,
            holder_id=recovered.attempt_lease.holder_id,
            fencing_token=recovered.attempt_lease.fencing_token,
            ttl_seconds=120,
        )
        second = self.ledger.heartbeat_owner_change(
            operation_id=self.op("capture-round-after-parents"),
            actor_principal_id="other",
            change_id=recovered.capture_change.change_id,
            retention_lease_id=capture.lease_id,
            holder_id=capture.holder_id,
            fencing_token=capture.fencing_token,
            ttl_seconds=90,
        )
        self.assertLessEqual(attempt.expires_at, controller.expires_at)
        self.assertLessEqual(second.change.expires_at, attempt.expires_at)
        self.assertGreater(second.change.expires_at, first.change.expires_at)
        self.assertEqual(second.retention_lease.heartbeat_revision, 2)

    def test_heartbeat_authority_resolves_exact_live_file_candidate_binding(
        self,
    ) -> None:
        created, admission, prepared, candidate_binding = (
            self._prepare_file_candidate_attempt()
        )
        recovered = self.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
        )
        self.assertEqual(recovered.candidate, admission.candidates[0])
        self.assertEqual(
            recovered.candidate_content_bindings, (candidate_binding,)
        )
        self.assertEqual(
            recovered.candidate.admission.envelope.content_refs,
            (candidate_binding.content_ref,),
        )

        connection = self.connection()
        try:
            connection.execute(
                "UPDATE content_objects SET lifecycle_state = 'corrupt' "
                "WHERE store_id = ? AND content_ref = ?",
                (candidate_binding.store_id, str(candidate_binding.content_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            RealmConflict, "candidate content is unavailable"
        ):
            self.ledger.read_run_attempt_heartbeat_authority(
                actor_principal_id="operator",
                run_id=created.run.run_id,
                attempt_id=prepared.attempt.attempt_id,
            )

    def test_heartbeat_keeps_attempt_capture_live_past_original_ttl(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare-short-heartbeat"),
            expected_run_revision=1,
            attempt_ttl_seconds=0.5,
        )
        original_expiry = prepared.capture_change.expires_at
        controller = prepared.controller_lease
        self.ledger.heartbeat_lease(
            operation_id=self.op("short-controller-heartbeat"),
            actor_principal_id="operator",
            lease_id=controller.lease_id,
            holder_id=controller.holder_id,
            fencing_token=controller.fencing_token,
            ttl_seconds=2,
        )
        attempt = prepared.attempt_lease
        self.ledger.heartbeat_lease(
            operation_id=self.op("short-attempt-heartbeat"),
            actor_principal_id="operator",
            lease_id=attempt.lease_id,
            holder_id=attempt.holder_id,
            fencing_token=attempt.fencing_token,
            ttl_seconds=2,
        )
        capture = prepared.capture_retention_lease
        renewed = self.ledger.heartbeat_owner_change(
            operation_id=self.op("short-capture-heartbeat"),
            actor_principal_id="operator",
            change_id=prepared.capture_change.change_id,
            retention_lease_id=capture.lease_id,
            holder_id=capture.holder_id,
            fencing_token=capture.fencing_token,
            ttl_seconds=2,
        )
        self.assertGreater(renewed.change.expires_at, original_expiry)
        time.sleep(max(0.0, original_expiry - time.time()) + 0.05)
        now = time.time()
        self.assertGreater(now, original_expiry)
        self.assertLess(now, renewed.change.expires_at)
        declaration, artifact, membership = self.capture_blob_artifact(
            prepared,
            name="post-heartbeat",
            contents='{"status":"captured"}',
        )
        self.assertEqual(declaration.name, "post-heartbeat")
        self.assertEqual(artifact.content_ref, str(membership.content_ref))

    def test_prepare_rejects_candidate_contract_mismatch_without_mutation(self) -> None:
        environment = replace(
            self.closure.environment_revision,
            candidate_contract={"format": "files"},
        )
        runtime = PreparedEnvironmentRuntimeManifest(
            environment_revision_digest=environment.digest,
            runtime_kind=self.closure.prepared_runtime.runtime_kind,
            runtime_settings=self.closure.prepared_runtime.runtime_settings,
            workdir=self.closure.prepared_runtime.workdir,
            portability=self.closure.prepared_runtime.portability,
        )
        template = RunEvaluationTemplate(
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
            objective=self.closure.evaluation_template.objective,
            resource_profile=self.closure.evaluation_template.resource_profile,
            sandbox_spec=self.closure.evaluation_template.sandbox_spec,
            default_seed=self.closure.evaluation_template.default_seed,
        )
        mismatched_closure = RunEvaluationClosure(environment, runtime, template)
        mismatched_manifest = replace(
            prepare_test_run_control_manifest(mismatched_closure, max_trials=1),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        mismatched = self._create_run(
            operation_id=self.op("mismatch-run"),
            run_id="run-mismatch",
            owner_id="run-owner-mismatch",
            closure=mismatched_closure,
            manifest=mismatched_manifest,
        )
        admission_change = self.ledger.begin_owner_change(
            operation_id=self.op("mismatch-admission-begin"),
            actor_principal_id="operator",
            owner_id=mismatched.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("mismatch-admission"),
            actor_principal_id="operator",
            run_id=mismatched.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=mismatched.controller_lease.lease_id,
            controller_holder_id=mismatched.controller_lease.holder_id,
            controller_fencing_token=mismatched.controller_lease.fencing_token,
            change_id=admission_change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("candidate-mismatch", envelope),),
                (LogicalTrialAdmission("trial-mismatch", "candidate-mismatch"),),
            ),
        )
        with self.assertRaises(RealmIntegrityError):
            self.ledger.prepare_run_attempt(
                operation_id=self.op("mismatch-prepare"),
                actor_principal_id="operator",
                run_id=mismatched.run.run_id,
                logical_trial_id="trial-mismatch",
                attempt_id="attempt-mismatch",
                expected_run_revision=1,
                controller_lease_id=mismatched.controller_lease.lease_id,
                controller_holder_id=mismatched.controller_lease.holder_id,
                controller_fencing_token=mismatched.controller_lease.fencing_token,
            )
        self.assertEqual(
            self.ledger.list_run_attempts(
                actor_principal_id="operator", run_id=mismatched.run.run_id
            ),
            (),
        )

    def test_launch_confirmation_requires_a_durable_execution_binding(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        with self.assertRaises(RealmNotFound):
            self.confirm(
                prepared,
                operation_id=self.op("wrong-token"),
                expected_run_revision=2,
                launch_token="not-the-launch-token",
            )
        with self.assertRaisesRegex(RealmConflict, "durable execution binding"):
            self.confirm(
                prepared,
                operation_id=self.op("unbound-confirm"),
                expected_run_revision=2,
            )
        current = self.ledger.read_run_attempt(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
        )
        self.assertEqual(current.state, "prepared")
        with self.assertRaises(RealmConflict):
            self.prepare(
                operation_id=self.op("stale-prepare"), expected_run_revision=2
            )

    def test_successful_adoption_commits_one_observation_and_terminal_trial(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        finalization = self.finalization(prepared)
        operation_id = self.op("adopt")
        adopted = self.adopt(
            prepared,
            finalization,
            operation_id=operation_id,
            expected_run_revision=2,
            expected_owner_revision=0,
        )
        replay = self.adopt(
            prepared,
            finalization,
            operation_id=operation_id,
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        self.assertEqual(replay, adopted)
        self.assertEqual(adopted.attempt.state, "terminal")
        self.assertEqual(adopted.attempt.outcome, "success")
        self.assertIsNone(adopted.attempt.code)
        self.assertEqual(adopted.logical_transition.to_state, "terminal")
        self.assertEqual(adopted.logical_transition.outcome, "success")
        self.assertIsNotNone(adopted.observation)
        self.assertEqual(adopted.observation.envelope, finalization.envelope)
        self.assertEqual(adopted.artifacts, ())
        self.assertEqual(adopted.owner_commit.previous_revision, 0)
        self.assertEqual(adopted.owner_commit.owner_revision, 0)
        self.assertEqual(
            self.ledger.read_run_attempt(
                actor_principal_id="operator",
                run_id="run-a",
                attempt_id=prepared.attempt.attempt_id,
            ),
            adopted.attempt,
        )

    def test_final_failure_atomically_closes_accepting_run_and_finishes_deterministically(self) -> None:
        manifest = replace(
            self.manifest,
            max_trials=2,
            max_failures=1,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        created = self._create_run(
            operation_id=self.op("failure-limit-create"),
            run_id="run-failure-limit",
            owner_id="run-owner-failure-limit",
            manifest=manifest,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 9}
        )
        admission_change = self.ledger.begin_owner_change(
            operation_id=self.op("failure-limit-admission-begin"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("failure-limit-admit"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=admission_change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("failure-candidate", envelope),),
                (LogicalTrialAdmission("failure-trial", "failure-candidate"),),
            ),
        )
        prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("failure-limit-prepare"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            logical_trial_id="failure-trial",
            attempt_id="failure-attempt",
            expected_run_revision=admitted.run.current_revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        adopted = self.ledger.adopt_run_attempt(
            operation_id=self.op("failure-limit-adopt"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            change_id=prepared.attempt.capture_change_id,
            finalization=self.finalization(
                prepared, outcome="failed", code="evaluation_failed"
            ),
            expected_run_revision=prepared.run.current_revision,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )

        self.assertIsNotNone(adopted.submission_control)
        self.assertEqual(adopted.submission_control.stop_code, "max_failures")
        self.assertEqual(adopted.revision.last_sequence, adopted.logical_transition.sequence + 1)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=created.run.run_id
        )
        self.assertEqual(snapshot.control.current_submission.state, "draining")
        self.assertEqual(snapshot.control.current_submission.stop_code, "max_failures")

        finished = self.ledger.finish_run(
            operation_id=self.op("failure-limit-finish"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=adopted.run.current_revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        self.assertEqual(
            (finished.finalization.terminal_state, finished.finalization.code),
            ("failed", "max_failures"),
        )

    def test_run_snapshot_reads_one_typed_head_for_recovery_and_readers(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("snapshot-prepare"), expected_run_revision=1
        )
        adopted = self.adopt(
            prepared,
            self.finalization(prepared),
            operation_id=self.op("snapshot-adopt"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        self.assertEqual(snapshot.run.current_revision, 3)
        self.assertEqual(snapshot.revision, adopted.revision)
        self.assertEqual(snapshot.control.manifest, self.manifest)
        self.assertEqual(snapshot.evaluation_closure, self.closure)
        self.assertEqual(
            tuple(item.candidate_id for item in snapshot.candidates),
            ("candidate-a",),
        )
        self.assertEqual(
            tuple(item.admission.logical_trial_id for item in snapshot.logical_trials),
            ("trial-default", "trial-explicit"),
        )
        self.assertEqual(snapshot.attempts, (adopted.attempt,))
        self.assertEqual(snapshot.observations, (adopted.observation,))
        self.assertEqual(snapshot.artifacts, ())
        self.assertEqual(RunLedgerSnapshot.from_dict(snapshot.to_dict()), snapshot)
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_snapshot(
                actor_principal_id="other", run_id="run-a"
            )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_observations WHERE run_id = 'run-a'"
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM leases WHERE lease_id = ?",
                    (prepared.attempt.attempt_lease_id,),
                ).fetchone(),
                ("released",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state, committed_txn_id FROM owner_transactions "
                    "WHERE change_id = ?",
                    (prepared.attempt.capture_change_id,),
                ).fetchone(),
                ("committed", adopted.revision.txn_id),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT event, state, outcome, attempt_id, attempt "
                    "FROM run_events WHERE txn_id = ? ORDER BY sequence",
                    (adopted.revision.txn_id,),
                ).fetchall(),
                [
                    ("attempt_transitioned", "terminal", "success", "attempt-1", 1),
                    ("logical_trial_transitioned", "terminal", "success", "attempt-1", 1),
                ],
            )
        finally:
            connection.close()

    def test_retryable_failure_returns_to_retrying_then_second_attempt_succeeds(self) -> None:
        first = self.prepare(
            operation_id=self.op("prepare-first"), expected_run_revision=1
        )
        first_failed = self.adopt(
            first,
            self.finalization(first, outcome="failed", code="evaluation_failed"),
            operation_id=self.op("adopt-first"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        self.assertEqual(first_failed.attempt.state, "terminal")
        self.assertEqual(first_failed.attempt.outcome, "failed")
        self.assertEqual(first_failed.logical_transition.to_state, "retrying")
        self.assertIsNotNone(first_failed.observation)
        second = self.prepare(
            operation_id=self.op("prepare-second"),
            expected_run_revision=3,
            attempt_id="attempt-2",
        )
        self.assertEqual(second.attempt.attempt_index, 2)
        second_succeeded = self.adopt(
            second,
            self.finalization(second),
            operation_id=self.op("adopt-second"),
            expected_run_revision=4,
            expected_owner_revision=0,
        )
        self.assertEqual(second_succeeded.logical_transition.to_state, "terminal")
        self.assertEqual(second_succeeded.logical_transition.outcome, "success")
        self.assertEqual(
            tuple(item.attempt_index for item in self.ledger.list_run_attempts(
                actor_principal_id="operator", run_id="run-a"
            )),
            (1, 2),
        )

    def test_lost_attempt_retries_but_exhausted_loss_closes_max_failures(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("loss-retry-prepare"), expected_run_revision=1
        )
        previous = self.created.controller_lease
        replacement = self.ledger.replace_run_controller(
            operation_id=self.op("loss-retry-replace"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_controller_generation=1,
            expected_controller_lease_id=previous.lease_id,
            expected_controller_holder_id=previous.holder_id,
            expected_controller_fencing_token=previous.fencing_token,
            new_controller_holder_id="loss-retry-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        retrying = self.ledger.reconcile_lost_run_attempt(
            operation_id=self.op("loss-retry-reconcile"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=replacement.run.current_revision,
            expected_owner_revision=prepared.revision.owner_revision,
            controller_lease_id=replacement.controller_lease.lease_id,
            controller_holder_id=replacement.controller_lease.holder_id,
            controller_fencing_token=replacement.controller_lease.fencing_token,
        )
        self.assertEqual(retrying.logical_transition.to_state, "retrying")
        self.assertIsNone(retrying.logical_transition.outcome)
        self.assertIsNone(retrying.submission_control)

        exhausted_manifest = replace(
            self.manifest,
            max_trials=2,
            max_failures=1,
            retry_policy=RetryPolicy(
                max_attempts=1,
                retryable_outcomes=("failed",),
            ),
        )
        exhausted_created = self._create_run(
            operation_id=self.op("loss-exhausted-create"),
            run_id="run-loss-exhausted",
            owner_id="run-owner-loss-exhausted",
            manifest=exhausted_manifest,
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 5}
        )
        admission_change = self.ledger.begin_owner_change(
            operation_id=self.op("loss-exhausted-admission-begin"),
            actor_principal_id="operator",
            owner_id=exhausted_created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("loss-exhausted-admit"),
            actor_principal_id="operator",
            run_id=exhausted_created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=exhausted_created.controller_lease.lease_id,
            controller_holder_id=exhausted_created.controller_lease.holder_id,
            controller_fencing_token=(
                exhausted_created.controller_lease.fencing_token
            ),
            change_id=admission_change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("loss-candidate", envelope),),
                (LogicalTrialAdmission("loss-trial", "loss-candidate"),),
            ),
        )
        exhausted_prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("loss-exhausted-prepare"),
            actor_principal_id="operator",
            run_id=exhausted_created.run.run_id,
            logical_trial_id="loss-trial",
            attempt_id="loss-attempt",
            expected_run_revision=admitted.run.current_revision,
            controller_lease_id=exhausted_created.controller_lease.lease_id,
            controller_holder_id=exhausted_created.controller_lease.holder_id,
            controller_fencing_token=(
                exhausted_created.controller_lease.fencing_token
            ),
        )
        exhausted_replacement = self.ledger.replace_run_controller(
            operation_id=self.op("loss-exhausted-replace"),
            actor_principal_id="operator",
            run_id=exhausted_created.run.run_id,
            expected_controller_generation=1,
            expected_controller_lease_id=(
                exhausted_created.controller_lease.lease_id
            ),
            expected_controller_holder_id=(
                exhausted_created.controller_lease.holder_id
            ),
            expected_controller_fencing_token=(
                exhausted_created.controller_lease.fencing_token
            ),
            new_controller_holder_id="loss-exhausted-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        exhausted = self.ledger.reconcile_lost_run_attempt(
            operation_id=self.op("loss-exhausted-reconcile"),
            actor_principal_id="operator",
            run_id=exhausted_created.run.run_id,
            attempt_id=exhausted_prepared.attempt.attempt_id,
            expected_run_revision=exhausted_replacement.run.current_revision,
            expected_owner_revision=exhausted_prepared.revision.owner_revision,
            controller_lease_id=exhausted_replacement.controller_lease.lease_id,
            controller_holder_id=exhausted_replacement.controller_lease.holder_id,
            controller_fencing_token=(
                exhausted_replacement.controller_lease.fencing_token
            ),
        )

        self.assertEqual(exhausted.logical_transition.to_state, "terminal")
        self.assertEqual(exhausted.logical_transition.outcome, "failed")
        self.assertIsNotNone(exhausted.submission_control)
        assert exhausted.submission_control is not None
        self.assertEqual(exhausted.submission_control.stop_code, "max_failures")
        self.assertEqual(
            exhausted.revision.last_sequence,
            exhausted.logical_transition.sequence + 1,
        )

    def test_run_snapshot_rejects_cross_run_candidate_and_trial_records(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

        with self.assertRaisesRegex(ValueError, "candidate refers outside"):
            replace(
                snapshot,
                candidates=(replace(snapshot.candidates[0], run_id="run-other"),),
            )
        with self.assertRaisesRegex(ValueError, "logical trial refers outside"):
            replace(
                snapshot,
                logical_trials=(
                    replace(snapshot.logical_trials[0], run_id="run-other"),
                    snapshot.logical_trials[1],
                ),
            )

        candidate_payload = snapshot.to_dict()
        candidate_payload["candidates"][0]["run_id"] = "run-other"
        with self.assertRaisesRegex(ValueError, "candidate refers outside"):
            RunLedgerSnapshot.from_dict(candidate_payload)

        trial_payload = snapshot.to_dict()
        trial_payload["logical_trials"][0]["run_id"] = "run-other"
        with self.assertRaisesRegex(ValueError, "logical trial refers outside"):
            RunLedgerSnapshot.from_dict(trial_payload)

    def test_retry_waiting_to_schedule_can_be_administratively_cancelled(self) -> None:
        first = self.prepare(
            operation_id=self.op("prepare-first"), expected_run_revision=1
        )
        first_failed = self.adopt(
            first,
            self.finalization(first, outcome="failed", code="evaluation_failed"),
            operation_id=self.op("adopt-first"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )
        self.assertEqual(first_failed.logical_transition.to_state, "retrying")

        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id=self.op("cancel-retry"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-default",
            expected_run_revision=3,
            code="admin_cancelled",
            **self._controller_arguments(),
        )

        self.assertEqual(cancelled.transition.from_state, "retrying")
        self.assertEqual(cancelled.transition.to_state, "terminal")
        self.assertEqual(cancelled.transition.outcome, "cancelled")
        self.assertIsNone(cancelled.transition.attempt_id)
        with self.assertRaises(RealmConflict):
            self.prepare(
                operation_id=self.op("prepare-after-cancel"),
                expected_run_revision=4,
                attempt_id="attempt-2",
            )

    def test_platform_failure_has_no_observation_and_is_not_retryable(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        finalization = AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            effective_outcome="cancelled",
            effective_code="worker_cancelled",
            captured_artifacts=(),
            platform_error={
                "code": "worker_cancelled",
                "message": "The worker ended before evaluation began.",
                "details": {"launched": False},
            },
        )
        adopted = self.adopt(
            prepared,
            finalization,
            operation_id=self.op("adopt-platform-failure"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        self.assertIsNone(adopted.observation)
        self.assertEqual(adopted.artifacts, ())
        self.assertEqual(adopted.attempt.outcome, "cancelled")
        self.assertEqual(adopted.attempt.code, "worker_cancelled")
        self.assertEqual(adopted.logical_transition.to_state, "terminal")
        self.assertEqual(adopted.logical_transition.outcome, "cancelled")
        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_observations WHERE run_id = 'run-a'"
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_pre_evaluation_envelope_is_terminal_without_an_observation(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        envelope = AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome="invalid",
            phase="validation",
            wall_clock_seconds=0.01,
            validation={"accepted": False, "errors": ["x is out of range"]},
            materialization={"runtime_spec": {}, "metadata": {"skipped": True}},
            metric_values={},
            constraint_results={},
            output_declarations=(),
            event_summary={},
            execution_metadata={"worker": "test"},
            error={
                "phase": "validation",
                "type": "ValidationError",
                "message": "x is out of range",
            },
        )
        finalization = AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            effective_outcome="invalid",
            effective_code="candidate_invalid",
            captured_artifacts=(),
            envelope=envelope,
        )
        adopted = self.adopt(
            prepared,
            finalization,
            operation_id=self.op("adopt-validation-failure"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        self.assertIsNone(adopted.observation)
        self.assertEqual(adopted.attempt.outcome, "invalid")
        self.assertEqual(adopted.logical_transition.to_state, "terminal")
        self.assertEqual(adopted.logical_transition.outcome, "invalid")
        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM run_observations WHERE run_id = 'run-a'"
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_artifact_adoption_commits_content_owner_and_evidence_atomically(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        declaration, capture, membership = self.capture_blob_artifact(
            prepared,
            name="result",
            contents='{"score":3.5}\n',
            visibility="method",
        )
        adopted = self.adopt(
            prepared,
            self.finalization(
                prepared,
                declarations=(declaration,),
                captures=(capture,),
            ),
            operation_id=self.op("adopt"),
            expected_run_revision=2,
            expected_owner_revision=0,
        )

        self.assertEqual(adopted.owner_commit.previous_revision, 0)
        self.assertEqual(adopted.owner_commit.owner_revision, 1)
        self.assertEqual(adopted.owner_commit.additions, (membership,))
        self.assertEqual(len(adopted.artifacts), 1)
        artifact = adopted.artifacts[0]
        self.assertEqual(artifact.declaration, declaration)
        self.assertEqual(artifact.content_ref, membership.content_ref)
        self.assertEqual(artifact.visibility, "method")
        self.assertEqual(artifact.capture_metadata, {"capture": "verified"})
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator", owner_id=self.created.run.owner_id
            ),
            tuple(
                sorted(
                    (*self.definition_bindings, membership),
                    key=lambda item: (
                        item.store_id,
                        str(item.content_ref),
                        item.role,
                    ),
                )
            ),
        )

        connection = self.connection()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT content_ref, visibility, adopted_txn_id FROM run_artifacts "
                    "WHERE run_id = 'run-a'"
                ).fetchone(),
                (str(membership.content_ref), "method", adopted.revision.txn_id),
            )
        finally:
            connection.close()

    def test_adoption_rejects_identity_change_and_stale_fences_without_mutation(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        finalization = self.finalization(prepared)
        with self.assertRaises(RealmNotFound):
            self.adopt(
                prepared,
                finalization,
                operation_id=self.op("wrong-change"),
                expected_run_revision=2,
                expected_owner_revision=0,
                change_id="change-does-not-belong-to-attempt",
            )
        with self.assertRaises(RealmConflict):
            self.adopt(
                prepared,
                finalization,
                operation_id=self.op("stale-run"),
                expected_run_revision=1,
                expected_owner_revision=0,
            )
        with self.assertRaises(RealmConflict):
            self.adopt(
                prepared,
                finalization,
                operation_id=self.op("stale-owner"),
                expected_run_revision=2,
                expected_owner_revision=1,
            )
        mismatched = AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id="different-binding",
            effective_outcome="failed",
            effective_code="worker_lost",
            captured_artifacts=(),
            platform_error={
                "code": "worker_lost",
                "message": "wrong binding",
                "details": {},
            },
        )
        with self.assertRaises(RealmConflict):
            self.adopt(
                prepared,
                mismatched,
                operation_id=self.op("wrong-finalization"),
                expected_run_revision=2,
                expected_owner_revision=0,
            )
        current = self.ledger.read_run_attempt(
            actor_principal_id="operator", run_id="run-a", attempt_id="attempt-1"
        )
        self.assertEqual(current.state, "prepared")

    def test_parallel_attempt_artifacts_rebase_additions_only_capture_changes(self) -> None:
        first = self.prepare(
            operation_id=self.op("prepare-first"),
            expected_run_revision=1,
            logical_trial_id="trial-default",
            attempt_id="attempt-first",
        )
        second = self.prepare(
            operation_id=self.op("prepare-second"),
            expected_run_revision=2,
            logical_trial_id="trial-explicit",
            attempt_id="attempt-second",
        )
        first_declaration, first_capture, first_membership = self.capture_blob_artifact(
            first, name="first", contents='{"trial":1}\n'
        )
        second_declaration, second_capture, second_membership = self.capture_blob_artifact(
            second, name="second", contents='{"trial":2}\n'
        )
        generic_stale = self.ledger.begin_owner_change(
            operation_id=self.op("begin-generic-stale"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        first_adopted = self.adopt(
            first,
            self.finalization(
                first,
                declarations=(first_declaration,),
                captures=(first_capture,),
            ),
            operation_id=self.op("adopt-first"),
            expected_run_revision=3,
            expected_owner_revision=0,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.commit_owner_change(
                operation_id=self.op("commit-generic-stale"),
                actor_principal_id="operator",
                change_id=generic_stale.change_id,
                expected_owner_revision=0,
                additions=(),
            )
        second_capture_lease = second.capture_retention_lease
        renewed_second = self.ledger.heartbeat_owner_change(
            operation_id=self.op("heartbeat-second-after-first-adoption"),
            actor_principal_id="operator",
            change_id=second.capture_change.change_id,
            retention_lease_id=second_capture_lease.lease_id,
            holder_id=second_capture_lease.holder_id,
            fencing_token=second_capture_lease.fencing_token,
            ttl_seconds=60,
        )
        self.assertEqual(renewed_second.change.base_owner_revision, 0)
        second_adopted = self.adopt(
            second,
            self.finalization(
                second,
                declarations=(second_declaration,),
                captures=(second_capture,),
            ),
            operation_id=self.op("adopt-second"),
            expected_run_revision=4,
            expected_owner_revision=1,
        )

        self.assertEqual(first_adopted.owner_commit.owner_revision, 1)
        self.assertEqual(second_adopted.owner_commit.previous_revision, 1)
        self.assertEqual(second_adopted.owner_commit.owner_revision, 2)
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=self.created.run.owner_id
        )
        self.assertIn(first_membership, memberships)
        self.assertIn(second_membership, memberships)

    def test_finish_is_blocked_while_an_attempt_is_active(self) -> None:
        prepared = self.prepare(
            operation_id=self.op("prepare"), expected_run_revision=1
        )
        with self.assertRaises(RealmConflict):
            self.ledger.cancel_run_logical_trial(
                operation_id=self.op("cancel-active-attempt"),
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                logical_trial_id="trial-default",
                expected_run_revision=2,
                code="admin_cancelled",
                **self._controller_arguments(),
            )
        self.ledger.cancel_run_logical_trial(
            operation_id=self.op("cancel-unrelated-trial"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-explicit",
            expected_run_revision=2,
            code="not_selected",
            **self._controller_arguments(),
        )
        draining = self.ledger.close_run_submissions(
            operation_id=self.op("close-submissions"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=3,
            stop_code="admin_cancelled",
            **self._controller_arguments(),
        )
        with self.assertRaises(RealmConflict):
            self.ledger.finish_run(
                operation_id=self.op("finish"),
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                expected_run_revision=draining.run.current_revision,
                terminal_state="cancelled",
                code="admin_cancelled",
                **self._controller_arguments(),
            )
        self.assertEqual(
            self.ledger.read_run_attempt(
                actor_principal_id="operator",
                run_id="run-a",
                attempt_id=prepared.attempt.attempt_id,
            ).state,
            "prepared",
        )


if __name__ == "__main__":
    unittest.main()
