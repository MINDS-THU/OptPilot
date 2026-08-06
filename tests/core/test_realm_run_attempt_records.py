from __future__ import annotations

import unittest
from dataclasses import replace

from optpilot.attempts import AttemptEnvelope, EvaluationSpec, OutputDeclaration
from optpilot.realm.leases import LeaseRecord, LeaseState
from optpilot.realm.owners import (
    OwnerChange,
    OwnerChangeState,
    OwnerCommitReceipt,
    OwnerMembership,
)
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_attempt_records import (
    RUN_ARTIFACT_ROLE,
    RunArtifactRecord,
    RunAttemptAdoptionReceipt,
    RunAttemptHeartbeatAuthorityReceipt,
    RunAttemptLaunchReceipt,
    RunAttemptLossReceipt,
    RunAttemptPreparationReceipt,
    RunAttemptRecord,
    RunAttemptTransitionRecord,
    RunObservationRecord,
)
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialTransitionRecord,
    NormalizedCandidateEnvelope,
    RunCandidateRecord,
    RunNamespaceRecord,
    RunRevisionRecord,
)


def _evaluation_spec() -> EvaluationSpec:
    return EvaluationSpec(
        environment_id="environment-a",
        environment_revision_digest="a" * 64,
        prepared_runtime_digest="b" * 64,
        candidate={
            "candidate_id": "candidate-a",
            "format": "parameters",
            "spec": {"x": 2},
        },
        objective={"primaryMetric": {"name": "score", "direction": "maximize"}},
        resource_profile={"cpu": 1},
        sandbox_spec={"network": "none"},
        candidate_ref="candidate:sha256:" + "c" * 64,
        seed=7,
    )


def _candidate_record(
    *,
    candidate_format: str = "parameters",
    content_refs=(),
) -> RunCandidateRecord:
    envelope = NormalizedCandidateEnvelope.build(
        candidate_format=candidate_format,
        spec={"x": 2},
        content_refs=content_refs,
    )
    return RunCandidateRecord(
        run_id="run-a",
        candidate_key="candidate-key-a",
        admission=CandidateAdmission(
            candidate_id="candidate-a",
            envelope=envelope,
            lineage={"parents": []},
            generator={"method_id": "method-a"},
        ),
        accepted_run_revision=1,
        accepted_owner_revision=0,
        accepted_sequence=1,
        accepted_txn_id=10,
        created_at=2.0,
    )


def _candidate_evaluation_spec(candidate: RunCandidateRecord) -> EvaluationSpec:
    return replace(
        _evaluation_spec(),
        candidate_ref=str(candidate.candidate_ref),
        candidate={
            "candidate_id": candidate.candidate_id,
            "format": candidate.admission.envelope.candidate_format,
            "spec": {"x": 2},
            "lineage": {"parents": []},
            "generator": {"method_id": "method-a"},
            "validation": {},
            "materialization": {},
        },
    )


def _declaration() -> OutputDeclaration:
    return OutputDeclaration(
        declaration_id="output-a",
        name="trace",
        path="outputs/trace.json",
        kind="file",
        media_type="application/json",
        metadata={"preview": True},
    )


def _envelope(*, phase: str = "environment_evaluation") -> AttemptEnvelope:
    return AttemptEnvelope(
        attempt_id="attempt-a",
        evaluation_spec_digest=_evaluation_spec().digest,
        binding_id="binding-a",
        outcome="success",
        phase=phase,
        wall_clock_seconds=1.25,
        validation={"accepted": True},
        materialization={"runtime": "python"},
        metric_values={"score": 0.75},
        constraint_results={},
        output_declarations=(_declaration(),),
        event_summary={"count": 3},
        execution_metadata={"worker": "worker-a"},
        error={},
    )


def _run(*, revision: int, next_sequence: int) -> RunNamespaceRecord:
    return RunNamespaceRecord(
        run_id="run-a",
        owner_id="owner-a",
        state="running",
        retention_state="active",
        current_revision=revision,
        next_sequence=next_sequence,
        max_trials=10,
        accepted_logical_trials=1,
        controller_lease_id="controller-lease-a",
        controller_holder_id="controller-a",
        controller_fencing_token=2,
        controller_generation=1,
        controller_txn_id=10,
        created_txn_id=1,
        created_at=1.0,
        updated_at=float(revision + 1),
    )


def _revision(
    *, revision: int, last_sequence: int, txn_id: int, kind: str, owner_revision: int = 2
) -> RunRevisionRecord:
    return RunRevisionRecord(
        run_id="run-a",
        revision=revision,
        owner_revision=owner_revision,
        last_sequence=last_sequence,
        next_sequence=last_sequence + 1,
        accepted_logical_trials=1,
        controller_generation=1,
        writer_controller_lease_id="controller-lease-a",
        writer_controller_fencing_token=2,
        operation_kind=kind,
        txn_id=txn_id,
        created_at=float(revision + 1),
    )


def _attempt(
    *,
    state: str,
    head: int,
    updated_at: float,
    outcome: str | None = None,
    code: str | None = None,
) -> RunAttemptRecord:
    return RunAttemptRecord(
        run_id="run-a",
        attempt_id="attempt-a",
        logical_trial_id="trial-a",
        attempt_index=1,
        controller_generation=1,
        evaluation_spec=_evaluation_spec(),
        prepared_runtime_digest="b" * 64,
        binding_id="binding-a",
        launch_token="launch-a",
        attempt_lease_id="attempt-lease-a",
        capture_change_id="capture-a",
        state=state,
        outcome=outcome,
        code=code,
        head_transition_index=head,
        prepared_run_revision=3,
        prepared_sequence=5,
        prepared_txn_id=30,
        prepared_at=4.0,
        updated_at=updated_at,
    )


def _heartbeat_receipt(
    *,
    candidate: RunCandidateRecord,
    candidate_content_bindings: tuple[OwnerMembership, ...],
) -> RunAttemptHeartbeatAuthorityReceipt:
    return RunAttemptHeartbeatAuthorityReceipt(
        run=_run(revision=3, next_sequence=7),
        attempt=replace(
            _attempt(state="prepared", head=1, updated_at=4.0),
            evaluation_spec=_candidate_evaluation_spec(candidate),
        ),
        controller_lease=LeaseRecord(
            lease_id="controller-lease-a",
            owner_id="owner-a",
            parent_lease_id=None,
            lease_kind="run-controller",
            audience="realm-ledger",
            holder_id="controller-a",
            scope_key="run:run-a",
            fencing_token=2,
            heartbeat_revision=0,
            state=LeaseState.ACTIVE,
            expires_at=30.0,
            created_at=1.0,
            updated_at=1.0,
            metadata={},
        ),
        attempt_lease=LeaseRecord(
            lease_id="attempt-lease-a",
            owner_id="owner-a",
            parent_lease_id="controller-lease-a",
            lease_kind="run-attempt",
            audience="realm-ledger",
            holder_id="controller-a",
            scope_key="run-attempt:run-a:attempt-a",
            fencing_token=1,
            heartbeat_revision=0,
            state=LeaseState.ACTIVE,
            expires_at=20.0,
            created_at=4.0,
            updated_at=4.0,
            metadata={"resource_ttl_seconds": 16.0},
        ),
        capture_change=OwnerChange(
            change_id="capture-a",
            owner_id="owner-a",
            base_owner_revision=2,
            retention_lease_id="capture-retention-a",
            expires_at=20.0,
            state=OwnerChangeState.ACTIVE,
        ),
        capture_retention_lease=LeaseRecord(
            lease_id="capture-retention-a",
            owner_id="owner-a",
            parent_lease_id="attempt-lease-a",
            lease_kind="owner-change-retention",
            audience="realm-ledger",
            holder_id="operator-a",
            scope_key="owner-change:capture-a",
            fencing_token=1,
            heartbeat_revision=0,
            state=LeaseState.ACTIVE,
            expires_at=20.0,
            created_at=4.0,
            updated_at=4.0,
            metadata={},
        ),
        candidate=candidate,
        candidate_content_bindings=candidate_content_bindings,
    )


def _attempt_transition(
    *,
    index: int,
    from_state: str | None,
    to_state: str,
    sequence: int,
    revision: int,
    txn_id: int,
    created_at: float,
    outcome: str | None = None,
    code: str | None = None,
) -> RunAttemptTransitionRecord:
    return RunAttemptTransitionRecord(
        run_id="run-a",
        attempt_id="attempt-a",
        transition_index=index,
        from_state=from_state,
        to_state=to_state,
        outcome=outcome,
        code=code,
        payload={"scheduler": "one-attempt"},
        sequence=sequence,
        run_revision=revision,
        txn_id=txn_id,
        created_at=created_at,
    )


def _logical_transition(
    *,
    index: int,
    from_state: str,
    to_state: str,
    sequence: int,
    revision: int,
    txn_id: int,
    created_at: float,
    outcome: str | None = None,
) -> LogicalTrialTransitionRecord:
    return LogicalTrialTransitionRecord(
        run_id="run-a",
        logical_trial_id="trial-a",
        transition_index=index,
        from_state=from_state,
        to_state=to_state,
        outcome=outcome,
        code=None,
        attempt_id="attempt-a",
        sequence=sequence,
        run_revision=revision,
        txn_id=txn_id,
        created_at=created_at,
    )


class RunAttemptCanonicalRecordTest(unittest.TestCase):
    def test_loss_receipt_round_trip_and_terminal_evidence_shape(self) -> None:
        run = replace(
            _run(revision=6, next_sequence=13), controller_generation=2
        )
        revision = replace(
            _revision(
                revision=6,
                last_sequence=12,
                txn_id=60,
                kind="run.attempt.reconcile",
            ),
            controller_generation=2,
        )
        attempt = _attempt(
            state="terminal",
            head=2,
            updated_at=7.0,
            outcome="failed",
            code="attempt_authority_lost",
        )
        transition = replace(
            _attempt_transition(
                index=2,
                from_state="prepared",
                to_state="terminal",
                sequence=11,
                revision=6,
                txn_id=60,
                created_at=7.0,
                outcome="failed",
                code="attempt_authority_lost",
            ),
            payload={
                "binding_state": "bound",
                "lost_controller_generation": 1,
                "replacement_controller_generation": 2,
                "started": False,
                "terminal_disposition": "never_started",
            },
        )
        logical = replace(
            _logical_transition(
                index=4,
                from_state="queued",
                to_state="terminal",
                sequence=12,
                revision=6,
                txn_id=60,
                created_at=7.0,
                outcome="failed",
            ),
            code="attempt_authority_lost",
        )
        receipt = RunAttemptLossReceipt(
            run=run,
            revision=revision,
            attempt=attempt,
            attempt_transition=transition,
            logical_transition=logical,
        )

        self.assertEqual(RunAttemptLossReceipt.from_dict(receipt.to_dict()), receipt)
        with self.assertRaisesRegex(ValueError, "terminal evidence"):
            replace(
                receipt,
                attempt_transition=replace(
                    transition,
                    from_state="running",
                    payload={
                        **dict(transition.payload),
                        "started": False,
                        "terminal_disposition": "never_started",
                    },
                ),
                logical_transition=replace(logical, from_state="running"),
            )

    def test_attempt_and_transition_round_trip_with_derived_digests(self) -> None:
        attempt = _attempt(state="prepared", head=1, updated_at=4.0)
        transition = _attempt_transition(
            index=1,
            from_state=None,
            to_state="prepared",
            sequence=5,
            revision=3,
            txn_id=30,
            created_at=4.0,
        )

        self.assertEqual(RunAttemptRecord.from_dict(attempt.to_dict()), attempt)
        self.assertEqual(
            RunAttemptTransitionRecord.from_dict(transition.to_dict()), transition
        )
        self.assertEqual(attempt.to_dict()["evaluation_spec_digest"], _evaluation_spec().digest)
        self.assertEqual(len(transition.to_dict()["payload_digest"]), 64)

        tampered = transition.to_dict()
        tampered["payload"]["scheduler"] = "different"
        with self.assertRaisesRegex(ValueError, "payload_digest"):
            RunAttemptTransitionRecord.from_dict(tampered)

    def test_attempt_state_and_runtime_anchors_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "prepared_runtime_digest"):
            replace(
                _attempt(state="prepared", head=1, updated_at=4.0),
                prepared_runtime_digest="d" * 64,
            )
        with self.assertRaisesRegex(ValueError, "Nonterminal"):
            _attempt(
                state="running",
                head=2,
                updated_at=5.0,
                outcome="failed",
                code="worker_failed",
            )
        with self.assertRaisesRegex(ValueError, "requires a code"):
            _attempt(
                state="terminal",
                head=3,
                updated_at=6.0,
                outcome="failed",
            )

    def test_observation_round_trip_rejects_digest_and_non_environment_phase(self) -> None:
        observation = RunObservationRecord(
            run_id="run-a",
            observation_id="observation-a",
            attempt_id="attempt-a",
            envelope=_envelope(),
            adopted_run_revision=5,
            adopted_sequence=9,
            adopted_txn_id=50,
            created_at=6.0,
        )
        self.assertEqual(
            RunObservationRecord.from_dict(observation.to_dict()), observation
        )
        tampered = observation.to_dict()
        tampered["envelope_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "envelope_digest"):
            RunObservationRecord.from_dict(tampered)
        with self.assertRaisesRegex(ValueError, "environment_evaluation"):
            replace(observation, envelope=_envelope(phase="validation"))

    def test_artifact_round_trip_uses_typed_physical_refs(self) -> None:
        content_ref = BlobRef.from_bytes(b"trace")
        artifact = RunArtifactRecord(
            run_id="run-a",
            artifact_id="artifact-a",
            attempt_id="attempt-a",
            observation_id="observation-a",
            declaration=_declaration(),
            content_ref=content_ref,
            size_bytes=5,
            visibility="operator",
            capture_metadata={"store_id": "store-a"},
            adopted_run_revision=5,
            adopted_sequence=9,
            adopted_txn_id=50,
            created_at=6.0,
        )
        restored = RunArtifactRecord.from_dict(artifact.to_dict())
        self.assertEqual(restored, artifact)
        self.assertIsInstance(restored.content_ref, BlobRef)
        self.assertEqual(RUN_ARTIFACT_ROLE, "run-artifact")

        with self.assertRaisesRegex(ValueError, "File artifacts"):
            replace(
                artifact,
                content_ref=SnapshotRef.from_manifest_bytes(b"tree manifest"),
            )


class RunAttemptReceiptTest(unittest.TestCase):
    def test_heartbeat_authority_round_trip_includes_exact_parameter_candidate(
        self,
    ) -> None:
        candidate = _candidate_record()
        receipt = _heartbeat_receipt(
            candidate=candidate,
            candidate_content_bindings=(),
        )

        stored = {"receipt_version": 1, **receipt.to_dict()}
        self.assertEqual(
            RunAttemptHeartbeatAuthorityReceipt.from_dict(stored), receipt
        )
        self.assertEqual(receipt.candidate, candidate)
        self.assertEqual(receipt.candidate_content_bindings, ())
        self.assertEqual(
            set(receipt.to_dict()),
            set(RunAttemptHeartbeatAuthorityReceipt.__dataclass_fields__),
        )

        tree = SnapshotRef.from_manifest_bytes(b"parameter-cannot-own-tree")
        with self.assertRaisesRegex(ValueError, "exact refs"):
            replace(
                receipt,
                candidate_content_bindings=(
                    OwnerMembership("store-a", tree, RUN_CANDIDATE_ROLE),
                ),
            )
        with self.assertRaisesRegex(ValueError, "authority differs"):
            replace(
                receipt,
                attempt=replace(
                    receipt.attempt,
                    evaluation_spec=replace(
                        receipt.attempt.evaluation_spec,
                        candidate_ref="candidate:sha256:" + "d" * 64,
                    ),
                ),
            )
        payload = receipt.to_dict()
        del payload["candidate"]
        with self.assertRaisesRegex(ValueError, "fields differ"):
            RunAttemptHeartbeatAuthorityReceipt.from_dict(payload)

    def test_heartbeat_authority_rejects_file_ref_role_and_spec_tampering(
        self,
    ) -> None:
        tree = SnapshotRef.from_manifest_bytes(b"candidate-tree")
        other_tree = SnapshotRef.from_manifest_bytes(b"other-tree")
        candidate = _candidate_record(
            candidate_format="files", content_refs=(tree,)
        )
        bindings = (
            OwnerMembership("store-a", tree, RUN_CANDIDATE_ROLE),
            OwnerMembership("store-b", tree, RUN_CANDIDATE_ROLE),
        )
        receipt = _heartbeat_receipt(
            candidate=candidate,
            candidate_content_bindings=bindings,
        )
        self.assertEqual(
            RunAttemptHeartbeatAuthorityReceipt.from_dict(receipt.to_dict()),
            receipt,
        )

        for bad_bindings, message in (
            ((), "exact refs"),
            (
                (OwnerMembership("store-a", tree, "wrong-role"),),
                "role run-candidate",
            ),
            (
                (
                    OwnerMembership(
                        "store-a", other_tree, RUN_CANDIDATE_ROLE
                    ),
                ),
                "exact refs",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    replace(
                        receipt,
                        candidate_content_bindings=bad_bindings,
                    )

        for invalid_candidate in (
            _candidate_record(
                candidate_format="files",
                content_refs=(tree, other_tree),
            ),
            _candidate_record(
                candidate_format="files",
                content_refs=(BlobRef.from_bytes(b"not-a-tree"),),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "one tree snapshot"):
                _heartbeat_receipt(
                    candidate=invalid_candidate,
                    candidate_content_bindings=(),
                )

        evaluation_candidate = dict(receipt.attempt.evaluation_spec.candidate)
        for field_name, bad_value in (
            ("candidate_id", "substituted"),
            ("spec", {"x": 999}),
            ("lineage", {"parents": ["substituted"]}),
        ):
            tampered = dict(evaluation_candidate)
            tampered[field_name] = bad_value
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, "authority differs"):
                    replace(
                        receipt,
                        attempt=replace(
                            receipt.attempt,
                            evaluation_spec=replace(
                                receipt.attempt.evaluation_spec,
                                candidate=tampered,
                            ),
                        ),
                    )

        missing_field = dict(evaluation_candidate)
        del missing_field["generator"]
        with self.assertRaisesRegex(ValueError, "fields differ"):
            replace(
                receipt,
                attempt=replace(
                    receipt.attempt,
                    evaluation_spec=replace(
                        receipt.attempt.evaluation_spec,
                        candidate=missing_field,
                    ),
                ),
            )
        invalid_contract = dict(evaluation_candidate)
        invalid_contract["validation"] = "not-a-mapping"
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            replace(
                receipt,
                attempt=replace(
                    receipt.attempt,
                    evaluation_spec=replace(
                        receipt.attempt.evaluation_spec,
                        candidate=invalid_contract,
                    ),
                ),
            )

    def test_preparation_receipt_round_trip_and_receipt_version(self) -> None:
        receipt = RunAttemptPreparationReceipt(
            run=_run(revision=3, next_sequence=7),
            revision=_revision(
                revision=3,
                last_sequence=6,
                txn_id=30,
                kind="run.attempt.prepare",
            ),
            controller_lease=LeaseRecord(
                lease_id="controller-lease-a",
                owner_id="owner-a",
                parent_lease_id=None,
                lease_kind="run-controller",
                audience="realm-ledger",
                holder_id="controller-a",
                scope_key="run:run-a",
                fencing_token=2,
                heartbeat_revision=0,
                state=LeaseState.ACTIVE,
                expires_at=30.0,
                created_at=1.0,
                updated_at=1.0,
                metadata={},
            ),
            attempt_lease=LeaseRecord(
                lease_id="attempt-lease-a",
                owner_id="owner-a",
                parent_lease_id="controller-lease-a",
                lease_kind="run-attempt",
                audience="realm-ledger",
                holder_id="controller-a",
                scope_key="run-attempt:run-a:attempt-a",
                fencing_token=1,
                heartbeat_revision=0,
                state=LeaseState.ACTIVE,
                expires_at=20.0,
                created_at=4.0,
                updated_at=4.0,
                metadata={"resource_ttl_seconds": 16.0},
            ),
            capture_change=OwnerChange(
                change_id="capture-a",
                owner_id="owner-a",
                base_owner_revision=2,
                retention_lease_id="capture-retention-a",
                expires_at=20.0,
                state=OwnerChangeState.ACTIVE,
            ),
            capture_retention_lease=LeaseRecord(
                lease_id="capture-retention-a",
                owner_id="owner-a",
                parent_lease_id="attempt-lease-a",
                lease_kind="owner-change-retention",
                audience="realm-ledger",
                holder_id="operator-a",
                scope_key="owner-change:capture-a",
                fencing_token=1,
                heartbeat_revision=0,
                state=LeaseState.ACTIVE,
                expires_at=20.0,
                created_at=4.0,
                updated_at=4.0,
                metadata={},
            ),
            attempt=_attempt(state="prepared", head=1, updated_at=4.0),
            attempt_transition=_attempt_transition(
                index=1,
                from_state=None,
                to_state="prepared",
                sequence=5,
                revision=3,
                txn_id=30,
                created_at=4.0,
            ),
            logical_transition=_logical_transition(
                index=2,
                from_state="accepted",
                to_state="queued",
                sequence=6,
                revision=3,
                txn_id=30,
                created_at=4.0,
            ),
        )
        stored = {"receipt_version": 1, **receipt.to_dict()}
        self.assertEqual(RunAttemptPreparationReceipt.from_dict(stored), receipt)
        stored["receipt_version"] = 2
        with self.assertRaisesRegex(ValueError, "receipt_version"):
            RunAttemptPreparationReceipt.from_dict(stored)

    def test_launch_receipt_round_trip_and_cross_checks_attempt_head(self) -> None:
        receipt = RunAttemptLaunchReceipt(
            run=_run(revision=4, next_sequence=9),
            revision=_revision(
                revision=4,
                last_sequence=8,
                txn_id=40,
                kind="run.attempt.confirm",
            ),
            attempt=_attempt(state="running", head=2, updated_at=5.0),
            attempt_transition=_attempt_transition(
                index=2,
                from_state="prepared",
                to_state="running",
                sequence=7,
                revision=4,
                txn_id=40,
                created_at=5.0,
            ),
            logical_transition=_logical_transition(
                index=3,
                from_state="queued",
                to_state="running",
                sequence=8,
                revision=4,
                txn_id=40,
                created_at=5.0,
            ),
        )
        self.assertEqual(RunAttemptLaunchReceipt.from_dict(receipt.to_dict()), receipt)
        with self.assertRaisesRegex(ValueError, "transition anchors"):
            replace(receipt, attempt=replace(receipt.attempt, head_transition_index=3))

    def test_adoption_receipt_round_trip_anchors_retention_and_evidence(self) -> None:
        content_ref = BlobRef.from_bytes(b"trace")
        observation = RunObservationRecord(
            run_id="run-a",
            observation_id="observation-a",
            attempt_id="attempt-a",
            envelope=_envelope(),
            adopted_run_revision=5,
            adopted_sequence=9,
            adopted_txn_id=50,
            created_at=6.0,
        )
        artifact = RunArtifactRecord(
            run_id="run-a",
            artifact_id="artifact-a",
            attempt_id="attempt-a",
            observation_id="observation-a",
            declaration=_declaration(),
            content_ref=content_ref,
            size_bytes=5,
            visibility="operator",
            capture_metadata={"store_id": "store-a"},
            adopted_run_revision=5,
            adopted_sequence=9,
            adopted_txn_id=50,
            created_at=6.0,
        )
        receipt = RunAttemptAdoptionReceipt(
            owner_commit=OwnerCommitReceipt(
                operation_id="adopt-a:owner",
                change_id="capture-a",
                owner_id="owner-a",
                previous_revision=2,
                owner_revision=3,
                manifest_digest="e" * 64,
                additions=(
                    OwnerMembership("store-a", content_ref, RUN_ARTIFACT_ROLE),
                ),
                removals=(),
            ),
            run=_run(revision=5, next_sequence=11),
            revision=_revision(
                revision=5,
                last_sequence=10,
                txn_id=50,
                kind="run.attempt.adopt",
                owner_revision=3,
            ),
            attempt=_attempt(
                state="terminal", head=3, updated_at=6.0, outcome="success"
            ),
            attempt_transition=_attempt_transition(
                index=3,
                from_state="running",
                to_state="terminal",
                outcome="success",
                sequence=9,
                revision=5,
                txn_id=50,
                created_at=6.0,
            ),
            logical_transition=_logical_transition(
                index=4,
                from_state="running",
                to_state="terminal",
                outcome="success",
                sequence=10,
                revision=5,
                txn_id=50,
                created_at=6.0,
            ),
            observation=observation,
            artifacts=(artifact,),
        )
        stored = {"receipt_version": 1, **receipt.to_dict()}
        self.assertEqual(RunAttemptAdoptionReceipt.from_dict(stored), receipt)

        unrelated_commit = replace(
            receipt.owner_commit,
            additions=(OwnerMembership("store-a", content_ref, "other-role"),),
        )
        self.assertEqual(
            replace(receipt, owner_commit=unrelated_commit).artifacts, (artifact,)
        )

    def test_adoption_allows_zero_addition_platform_failure(self) -> None:
        content_ref = BlobRef.from_bytes(b"already retained trace")
        artifact = RunArtifactRecord(
            run_id="run-a",
            artifact_id="artifact-platform-log",
            attempt_id="attempt-a",
            observation_id=None,
            declaration=_declaration(),
            content_ref=content_ref,
            size_bytes=22,
            visibility="operator",
            capture_metadata={"already_retained": True},
            adopted_run_revision=5,
            adopted_sequence=9,
            adopted_txn_id=50,
            created_at=6.0,
        )
        receipt = RunAttemptAdoptionReceipt(
            owner_commit=OwnerCommitReceipt(
                operation_id="adopt-failure:owner",
                change_id="capture-a",
                owner_id="owner-a",
                previous_revision=2,
                owner_revision=2,
                manifest_digest="e" * 64,
                additions=(),
                removals=(),
            ),
            run=_run(revision=5, next_sequence=11),
            revision=_revision(
                revision=5,
                last_sequence=10,
                txn_id=50,
                kind="run.attempt.adopt",
                owner_revision=2,
            ),
            attempt=_attempt(
                state="terminal",
                head=3,
                updated_at=6.0,
                outcome="failed",
                code="worker_lost",
            ),
            attempt_transition=_attempt_transition(
                index=3,
                from_state="running",
                to_state="terminal",
                outcome="failed",
                code="worker_lost",
                sequence=9,
                revision=5,
                txn_id=50,
                created_at=6.0,
            ),
            logical_transition=_logical_transition(
                index=4,
                from_state="running",
                to_state="retrying",
                sequence=10,
                revision=5,
                txn_id=50,
                created_at=6.0,
            ),
            observation=None,
            artifacts=(artifact,),
        )
        self.assertEqual(RunAttemptAdoptionReceipt.from_dict(receipt.to_dict()), receipt)


if __name__ == "__main__":
    unittest.main()
