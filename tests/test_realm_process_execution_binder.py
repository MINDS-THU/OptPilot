from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ephemeral_volume_records import EphemeralVolumeState
from optpilot.realm.ephemeral_volume_service import (
    ManagedEphemeralVolume,
    RealmEphemeralVolumeService,
)
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.local_process_supervisor import (
    LocalProcessSupervisor,
    ProcessLaunchReservation,
    ProcessLaunchRequest,
)
from optpilot.realm.process_execution_binder import (
    ProcessExecutionResourceError,
    RealmProcessExecutionBinder,
    _InitializationLeasePulse,
)
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection_records import ProjectionRealizationState
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
)
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.service import RealmContentService
from optpilot.retained_study_service import RetainedStudyService
from optpilot.runtime_binding import compile_retained_process_attempt_runtime
from tests.test_retained_study_service import _write_package
from tests.test_runtime_binding import (
    _compile as _compile_runtime,
    _definition_with_prepared_python,
    _file_definition_and_candidate_input,
)


class RealmProcessExecutionBinderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.study_path = _write_package(self.package_root)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="process-binder/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_principal(
            operation_id="process-binder/principal/delegate",
            principal_id="delegate",
            kind="agent",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="process-binder/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content_service = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.projection_root = self.root / "projections"
        self.volume_root = self.root / "volumes"
        self.projection_service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.projection_root,
        )
        self.volume_service = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.volume_root
        )
        self.provider = ProcessProviderIdentity(
            builder_fingerprint="a" * 64,
            platform="test-platform",
        )
        self.supervisor_root = self.root / "process-supervisor"
        self.supervisor = LocalProcessSupervisor(self.supervisor_root)
        retained = RetainedStudyService(
            self.ledger,
            self.content_service,
            self.projection_service,
            self.provider,
        )
        package = retained.prepare_local_package(
            operation_id="process-binder/package/prepare",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id="process-binder-source-owner",
            study_definition_owner_id="process-binder-definition-owner",
        )
        self.definition = package.study_definition.manifest.run_definition
        self.created = retained.launch_definition_run(
            operation_id="process-binder/run/launch",
            actor_principal_id="operator",
            controller_holder_id="process-binder-controller",
            controller_ttl_seconds=300,
            preparation=package,
            run_id="process-binder-run",
            owner_id="process-binder-run-owner",
        )
        self.counter = 0
        self._admit()
        self.preparation = self.ledger.prepare_run_attempt(
            operation_id=self.op("attempt/prepare"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
            expected_run_revision=1,
            attempt_ttl_seconds=300,
            **self.controller_arguments(),
        )
        self.binder = RealmProcessExecutionBinder(
            self.ledger,
            self.projection_service,
            self.volume_service,
            self.provider,
            launch_reservation_verifier=self._verify_launch_reservation,
            terminal_proof_verifier=self.supervisor.validate_terminal_proof,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"process-binder/{self.counter}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit(self) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 0.5}
        )
        plan = RunAdmissionPlan(
            candidates=(
                CandidateAdmission(
                    "candidate-a",
                    envelope,
                    lineage={"parents": []},
                    generator={"method_id": "external"},
                ),
            ),
            logical_trials=(
                LogicalTrialAdmission(
                    "trial-a", "candidate-a", seed=None, repetition_index=0
                ),
            ),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission/begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=300,
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission/commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            change_id=change.change_id,
            plan=plan,
            **self.controller_arguments(),
        )

    def _launch_request(self, prepared) -> ProcessLaunchRequest:
        return ProcessLaunchRequest(
            argv=(sys.executable, "-c", "pass"),
            cwd=str(prepared.workdir),
            env={},
        )

    def _verify_launch_reservation(
        self,
        reservation: object,
        prepared,
        *,
        supervisor: LocalProcessSupervisor | None = None,
    ) -> str:
        if not isinstance(reservation, ProcessLaunchReservation):
            raise TypeError("reservation must be a ProcessLaunchReservation")
        request = self._launch_request(prepared)
        registry = supervisor or self.supervisor
        durable = registry.lookup_reservation(
            launch_token=prepared.attempt.launch_token,
            binding_id=prepared.draft.binding_id,
            evidence_fingerprint=prepared.draft.evidence_fingerprint,
            launch_request_digest=request.digest,
        )
        if durable != reservation:
            raise RealmConflict("reservation differs from provider registry")
        return request.digest

    def _reserve(self, prepared) -> ProcessLaunchReservation:
        return self.supervisor.reserve(
            launch_token=prepared.attempt.launch_token,
            binding_id=prepared.draft.binding_id,
            evidence_fingerprint=prepared.draft.evidence_fingerprint,
            request=self._launch_request(prepared),
        )

    def _commit_prepared(self, prepared):
        return prepared._commit_reserved_launch(self._reserve(prepared))

    def bind(self):
        prepared = self.binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.preparation,
        )
        return self._commit_prepared(prepared)

    def _confirm(self, binding):
        receipt = binding.receipt
        launch_intent = binding.launch_intent
        if launch_intent is None:
            raise AssertionError("test launch intent was not committed")
        return self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("attempt/confirm"),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            launch_token=receipt.attempt.launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=launch_intent.launch_request_digest,
            expected_run_revision=receipt.run.current_revision,
            **self.controller_arguments(),
        )

    def _terminal_proof(
        self,
        binding,
        *,
        launch_token: str | None = None,
        evidence_fingerprint: str | None = None,
    ):
        receipt = binding.receipt
        request = self._launch_request(binding)
        effective_launch_token = launch_token or receipt.attempt.launch_token
        effective_evidence = (
            evidence_fingerprint or receipt.binding.evidence_fingerprint
        )
        if (
            effective_launch_token == receipt.attempt.launch_token
            and effective_evidence != receipt.binding.evidence_fingerprint
        ):
            effective_launch_token = f"{effective_launch_token}-forged"
        reservation = self.supervisor.reserve(
            launch_token=effective_launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=effective_evidence,
            request=request,
        )
        return self.supervisor.start_reserved(reservation).wait(timeout=10)

    def _adopt(self, binding, launched) -> None:
        receipt = binding.receipt
        envelope = AttemptEnvelope(
            attempt_id=receipt.attempt.attempt_id,
            evaluation_spec_digest=receipt.attempt.evaluation_spec_digest,
            binding_id=receipt.binding.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 0.5}, "metadata": {}},
            metric_values={"score": 0.5},
            constraint_results={},
            output_declarations=(),
            event_summary={"count": 1},
            execution_metadata={"worker": "test"},
            error={},
        )
        finalization = AttemptFinalization(
            attempt_id=envelope.attempt_id,
            evaluation_spec_digest=envelope.evaluation_spec_digest,
            binding_id=envelope.binding_id,
            effective_outcome="success",
            effective_code=None,
            captured_artifacts=(),
            envelope=envelope,
        )
        owner = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=launched.run.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        self.ledger.adopt_run_attempt(
            operation_id=self.op("attempt/adopt"),
            actor_principal_id="operator",
            run_id=launched.attempt.run_id,
            attempt_id=launched.attempt.attempt_id,
            change_id=launched.attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=launched.run.current_revision,
            expected_owner_revision=owner.revision,
            **self.controller_arguments(),
        )

    def _terminalize(self, binding) -> None:
        self._adopt(binding, self._confirm(binding))

    def _restart_binder(self):
        projection = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.projection_root,
        )
        volume = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.volume_root
        )
        supervisor = LocalProcessSupervisor(self.supervisor_root)
        binder = RealmProcessExecutionBinder(
            self.ledger,
            projection,
            volume,
            self.provider,
            launch_reservation_verifier=(
                lambda reservation, prepared: self._verify_launch_reservation(
                    reservation,
                    prepared,
                    supervisor=supervisor,
                )
            ),
            terminal_proof_verifier=supervisor.validate_terminal_proof,
        )
        return binder, projection, volume

    def _all_projection_records(self):
        return self.ledger.list_projection_realizations(
            actor_principal_id=self.projection_service.maintenance_principal_id,
            projection_root_id=self.projection_service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )

    def _private_projection_records(self):
        return tuple(
            item
            for item in self._all_projection_records()
            if item.availability_resolution.get("realization_sharing", {}).get(
                "policy"
            )
            == "private"
        )

    def _all_volume_records(self):
        return self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.volume_service.maintenance_principal_id,
            volume_root_id=self.volume_service.root_binding.volume_root_id,
            states=tuple(EphemeralVolumeState),
        )

    def test_bind_builds_path_free_evidence_and_typed_private_scope_paths(self) -> None:
        binding = self.bind()
        receipt = binding.receipt
        encoded = json.dumps(receipt.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), encoded)
        self.assertEqual(receipt.binding.portable_spec, binding.portable_spec)
        self.assertEqual(
            receipt.binding.evidence.projections[0].access_enforcement,
            "advisory",
        )
        self.assertEqual(
            receipt.binding.evidence.provider.sandbox_enforcement,
            "advisory",
        )
        self.assertEqual(
            set(binding.scope_paths),
            {"environment-source", "trial", "control"},
        )
        self.assertEqual(binding.workdir, binding.scope_paths["trial"])
        self.assertTrue(all(path.is_absolute() for path in binding.scope_paths.values()))
        self.assertTrue(all(path.exists() for path in binding.scope_paths.values()))
        self.assertEqual(
            self._private_projection_records()[0].availability_resolution[
                "realization_sharing"
            ]["policy"],
            "private",
        )
        self.assertFalse(hasattr(binding, "close"))
        self.assertFalse(hasattr(binding, "__enter__"))

    def test_exact_replay_across_service_restart_reattaches_same_resources(self) -> None:
        first = self.bind()
        restarted_projection = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.projection_root,
        )
        restarted_volume = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.volume_root
        )
        restarted = RealmProcessExecutionBinder(
            self.ledger,
            restarted_projection,
            restarted_volume,
            self.provider,
        ).recover(
            actor_principal_id="operator",
            run_id=self.preparation.attempt.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )
        self.assertEqual(restarted.receipt.binding, first.receipt.binding)
        self.assertEqual(restarted.receipt.attempt, first.receipt.attempt)
        self.assertEqual(restarted.scope_paths, first.scope_paths)
        self.assertEqual(
            restarted.receipt.binding.projections,
            first.receipt.binding.projections,
        )
        self.assertEqual(
            restarted.receipt.binding.writable_volumes,
            first.receipt.binding.writable_volumes,
        )

    def test_prepare_prepared_uses_current_persisted_authority_without_receipt(self) -> None:
        restarted, _projection, _volume = self._restart_binder()
        prepared = restarted.prepare_prepared(
            actor_principal_id="operator",
            run_id=self.preparation.attempt.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )
        bound = self._commit_prepared(prepared)
        replay = restarted.recover(
            actor_principal_id="operator",
            run_id=self.preparation.attempt.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )

        self.assertEqual(replay.receipt.binding, bound.receipt.binding)
        self.assertEqual(replay.scope_paths, bound.scope_paths)
        self.assertEqual(
            bound.receipt.binding.resource_ttl_seconds,
            self.preparation.resource_ttl_seconds,
        )

    def test_prepare_prepared_recovers_resources_left_before_binding_commit(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        prepared = self.binder.prepare_prepared(
            actor_principal_id="operator",
            run_id=self.preparation.attempt.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )
        reservation = self._reserve(prepared)
        original_commit = self.ledger.commit_run_attempt_binding

        def crash_before_commit(**_arguments):
            raise SimulatedCrash()

        self.ledger.commit_run_attempt_binding = crash_before_commit  # type: ignore[method-assign]
        try:
            with self.assertRaises(SimulatedCrash):
                prepared._commit_reserved_launch(reservation)
        finally:
            self.ledger.commit_run_attempt_binding = original_commit  # type: ignore[method-assign]

        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.preparation.attempt.run_id,
                attempt_id=self.preparation.attempt.attempt_id,
            )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )

        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/grant-after-precommit-crash"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/grant-metadata-after-precommit-crash"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.METADATA_READ,
        )
        restarted, _projection, _volume = self._restart_binder()
        recovered_prepared = restarted.prepare_prepared(
            actor_principal_id="delegate",
            run_id=self.preparation.attempt.run_id,
            attempt_id=self.preparation.attempt.attempt_id,
        )
        recovered = self._commit_prepared(recovered_prepared)
        self.assertEqual(
            recovered.receipt.binding.resource_ttl_seconds,
            self.preparation.resource_ttl_seconds,
        )
        self.assertEqual(len(self._private_projection_records()), 1)
        self.assertEqual(len(self._all_volume_records()), 2)

    def test_recover_running_binding_reattaches_as_a_new_authorized_actor(self) -> None:
        initial = self.bind()
        receipt = initial.receipt
        intent = initial.launch_intent
        self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("attempt/confirm-for-recovery"),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            launch_token=receipt.attempt.launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=intent.launch_request_digest,
            expected_run_revision=receipt.run.current_revision,
            **self.controller_arguments(),
        )
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/grant"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        restarted_projection = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.projection_root,
        )
        restarted_volume = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.volume_root
        )
        recovered = RealmProcessExecutionBinder(
            self.ledger,
            restarted_projection,
            restarted_volume,
            self.provider,
        ).recover(
            actor_principal_id="delegate",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertIsNotNone(recovered.authority_receipt)
        self.assertIsNone(recovered.commit_receipt)
        self.assertEqual(recovered.receipt.binding, receipt.binding)
        self.assertEqual(recovered.scope_paths, initial.scope_paths)

    def test_substituted_resource_holder_is_rejected_and_retained(self) -> None:
        original = self.volume_service.create

        def substituted(**arguments):
            arguments["holder_id"] = "substituted-holder"
            return original(**arguments)

        self.volume_service.create = substituted  # type: ignore[method-assign]
        try:
            with self.assertRaises(RealmConflict):
                self.bind()
        finally:
            self.volume_service.create = original  # type: ignore[method-assign]
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.preparation.attempt.run_id,
                attempt_id=self.preparation.attempt.attempt_id,
            )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )

    def test_definite_precommit_failure_retains_resources_for_recovery(self) -> None:
        original = self.ledger.commit_run_attempt_binding

        def fail_before_commit(**_arguments):
            raise RealmConflict("injected precommit rejection")

        self.ledger.commit_run_attempt_binding = fail_before_commit  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RealmConflict, "injected precommit"):
                self.bind()
        finally:
            self.ledger.commit_run_attempt_binding = original  # type: ignore[method-assign]
        self.assertEqual(len(self._private_projection_records()), 1)
        self.assertEqual(len(self._all_volume_records()), 2)
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )

    def test_committed_response_loss_is_proven_then_replayed_without_cleanup(self) -> None:
        original = self.ledger.commit_run_attempt_binding
        calls = 0

        def lose_first_response(**arguments):
            nonlocal calls
            calls += 1
            receipt = original(**arguments)
            if calls == 1:
                raise OSError("injected committed response loss")
            return receipt

        self.ledger.commit_run_attempt_binding = lose_first_response  # type: ignore[method-assign]
        try:
            binding = self.bind()
        finally:
            self.ledger.commit_run_attempt_binding = original  # type: ignore[method-assign]
        self.assertEqual(calls, 1)
        self.assertEqual(
            self.ledger.read_run_attempt_binding(
                actor_principal_id="operator",
                run_id=binding.receipt.binding.run_id,
                attempt_id=binding.receipt.binding.attempt_id,
            ),
            binding.receipt.binding,
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )

    def test_release_requires_terminal_then_cleans_all_paths_and_is_retryable(self) -> None:
        binding = self.bind()
        paths = tuple(binding.scope_paths.values())
        proof = self._terminal_proof(binding)
        binding.authenticate_and_record_terminal(proof)
        with self.assertRaisesRegex(RealmConflict, "before the attempt is terminal"):
            binding.release_after_worker_stopped(proof)
        self.assertTrue(all(path.exists() for path in paths))

        self._terminalize(binding)
        authorization = self.ledger.read_run_attempt_cleanup_authorization(
            actor_principal_id="operator",
            run_id=binding.receipt.binding.run_id,
            attempt_id=binding.receipt.binding.attempt_id,
        )
        self.assertEqual(authorization.authorized_by_principal_id, "operator")
        binding.release_after_worker_stopped(proof)
        binding.release_after_worker_stopped(proof)
        self.assertTrue(binding.released)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.CLEANED
                for item in self._all_volume_records()
            )
        )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self._private_projection_records()
            )
        )

    def test_worker_terminal_validation_rejects_forgery_before_finalization(self) -> None:
        binding = self.bind()
        proof = self._terminal_proof(binding)
        self._confirm(binding)
        forged = replace(proof, terminal_at=proof.terminal_at + 0.001)

        with self.assertRaises(RealmIntegrityError):
            binding.authenticate_and_record_terminal(forged)

        evidence = binding.authenticate_and_record_terminal(proof)
        self.assertEqual(evidence.binding_id, binding.receipt.binding.binding_id)
        self.assertEqual(evidence.launch_token, binding.receipt.attempt.launch_token)
        self.assertTrue(all(path.exists() for path in binding.scope_paths.values()))
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )

    def test_worker_terminal_validation_rejects_wrong_launch_and_evidence(self) -> None:
        binding = self.bind()
        receipt = binding.receipt
        wrong_launch = self._terminal_proof(
            binding, launch_token=f"{receipt.attempt.launch_token}-other"
        )
        with self.assertRaisesRegex(RealmConflict, "differs"):
            binding.authenticate_and_record_terminal(wrong_launch)

        wrong_evidence = self._terminal_proof(
            binding,
            evidence_fingerprint=(
                "b" * 64
                if receipt.binding.evidence_fingerprint != "b" * 64
                else "c" * 64
            ),
        )
        with self.assertRaisesRegex(RealmConflict, "differs"):
            binding.authenticate_and_record_terminal(wrong_evidence)

        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )

    def test_worker_terminal_validation_rejects_substituted_request_digest(self) -> None:
        binding = self.bind()
        proof = self._terminal_proof(binding)
        substituted = ProcessLaunchRequest(
            argv=(sys.executable, "-c", "pass"),
            cwd=str(binding.workdir),
            env={"REQUEST_VARIANT": "substituted"},
        )
        forged = replace(proof, launch_request_digest=substituted.digest)

        with self.assertRaisesRegex(RealmConflict, "differs"):
            binding.authenticate_and_record_terminal(forged)
        self.assertTrue(all(path.exists() for path in binding.scope_paths.values()))

    def test_metadata_only_cleanup_authority_mutates_nothing(self) -> None:
        binding = self.bind()
        paths = tuple(binding.scope_paths.values())
        proof = self._terminal_proof(binding)
        launched = self._confirm(binding)
        binding.authenticate_and_record_terminal(proof)
        self._adopt(binding, launched)
        self.ledger.register_principal(
            operation_id=self.op("metadata/register"),
            principal_id="metadata-reader",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id=self.op("metadata/grant"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="metadata-reader",
            permission=OwnerPermission.METADATA_READ,
        )
        with self.assertRaises(RealmNotFound):
            self.binder.cleanup_terminal_binding(
                actor_principal_id="metadata-reader",
                run_id=binding.receipt.binding.run_id,
                attempt_id=binding.receipt.binding.attempt_id,
                terminal_proof=proof,
            )
        authorization = self.ledger.read_run_attempt_cleanup_authorization(
            actor_principal_id="operator",
            run_id=binding.receipt.binding.run_id,
            attempt_id=binding.receipt.binding.attempt_id,
        )
        self.assertEqual(authorization.authorized_by_principal_id, "operator")
        self.assertTrue(all(path.exists() for path in paths))
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.READY
                for item in self._private_projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.ACTIVE
                for item in self._all_volume_records()
            )
        )

    def test_launch_intent_and_cleanup_authorization_replay_across_actors(self) -> None:
        binding = self.bind()
        receipt = binding.receipt
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/grant-cross-actor"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/grant-cross-actor-metadata"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.METADATA_READ,
        )
        proof = self._terminal_proof(binding)
        intended = binding.launch_intent
        if intended is None:
            raise AssertionError("launch intent was not committed")
        recovered, _projection, _volume = self._restart_binder()
        delegate_binding = recovered.recover(
            actor_principal_id="delegate",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        replayed = delegate_binding.launch_intent
        self.assertEqual(replayed.launch_request_digest, intended.launch_request_digest)
        self.assertEqual(replayed.created_txn_id, intended.created_txn_id)

        launched = self._confirm(binding)
        binding.authenticate_and_record_terminal(proof)
        self._adopt(binding, launched)
        self.ledger.grant_owner_permission(
            operation_id=self.op("delegate/regrant-after-adoption"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        operator_cleanup = self.binder.cleanup_terminal_binding(
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            terminal_proof=proof,
        )
        delegate_cleanup = recovered.resume_authorized_cleanup(
            actor_principal_id="delegate",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertEqual(delegate_cleanup, operator_cleanup)
        authorization = self.ledger.read_run_attempt_cleanup_authorization(
            actor_principal_id="delegate",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertEqual(authorization.authorized_by_principal_id, "operator")

    def test_post_adoption_restart_cleans_without_live_reattachment(self) -> None:
        binding = self.bind()
        paths = tuple(binding.scope_paths.values())
        proof = self._terminal_proof(binding)
        launched = self._confirm(binding)
        binding.authenticate_and_record_terminal(proof)
        self._adopt(binding, launched)
        restarted, _projection, _volume = self._restart_binder()

        cleaned = restarted.cleanup_terminal_binding(
            actor_principal_id="operator",
            run_id=binding.receipt.binding.run_id,
            attempt_id=binding.receipt.binding.attempt_id,
            terminal_proof=proof,
        )
        replay = restarted.cleanup_terminal_binding(
            actor_principal_id="operator",
            run_id=binding.receipt.binding.run_id,
            attempt_id=binding.receipt.binding.attempt_id,
            terminal_proof=proof,
        )

        self.assertEqual(replay, cleaned)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.CLEANED
                for item in self._all_volume_records()
            )
        )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self._private_projection_records()
            )
        )

    def test_terminal_cleanup_attempts_every_resource_and_retries_partial_failure(
        self,
    ) -> None:
        binding = self.bind()
        proof = self._terminal_proof(binding)
        launched = self._confirm(binding)
        binding.authenticate_and_record_terminal(proof)
        self._adopt(binding, launched)
        restarted, projection, volume = self._restart_binder()
        failed_volume_id = binding.receipt.binding.writable_volumes[0].volume_id
        real_reconcile = volume.reconcile_volume
        injected = False

        def fail_one_volume_once(**arguments):
            nonlocal injected
            if arguments["volume_id"] == failed_volume_id and not injected:
                injected = True
                raise RuntimeError("injected terminal cleanup failure")
            return real_reconcile(**arguments)

        with mock.patch.object(
            volume, "reconcile_volume", side_effect=fail_one_volume_once
        ):
            with self.assertRaises(ProcessExecutionResourceError) as raised:
                restarted.cleanup_terminal_binding(
                    actor_principal_id="operator",
                    run_id=binding.receipt.binding.run_id,
                    attempt_id=binding.receipt.binding.attempt_id,
                    terminal_proof=proof,
                )

        self.assertTrue(injected)
        self.assertEqual(len(raised.exception.failures), 1)
        self.assertEqual(raised.exception.failures[0].resource_kind, "volume")
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self._private_projection_records()
            )
        )
        states = {
            item.volume_id: item.state for item in self._all_volume_records()
        }
        self.assertNotEqual(states[failed_volume_id], EphemeralVolumeState.CLEANED)
        self.assertTrue(
            all(
                state is EphemeralVolumeState.CLEANED
                for volume_id, state in states.items()
                if volume_id != failed_volume_id
            )
        )

        proofless = RealmProcessExecutionBinder(
            self.ledger,
            projection,
            volume,
            self.provider,
        )
        proofless.resume_authorized_cleanup(
            actor_principal_id="operator",
            run_id=binding.receipt.binding.run_id,
            attempt_id=binding.receipt.binding.attempt_id,
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.CLEANED
                for item in self._all_volume_records()
            )
        )

    def test_store_resolution_selects_available_replica_and_rejects_unavailable(self) -> None:
        spec = compile_retained_process_attempt_runtime(
            owner_id=self.created.run.owner_id,
            run_definition=self.definition,
            evaluation_spec=self.preparation.attempt.evaluation_spec,
            provider=self.provider,
        )
        original = self.ledger.list_owner_memberships

        def ambiguous(**arguments):
            memberships = original(**arguments)
            source = next(
                item
                for item in memberships
                if item.role == RUN_ENVIRONMENT_SOURCE_ROLE
            )
            return memberships + (
                OwnerMembership(
                    "another-local-store",
                    source.content_ref,
                    RUN_ENVIRONMENT_SOURCE_ROLE,
                ),
            )

        self.ledger.list_owner_memberships = ambiguous  # type: ignore[method-assign]
        try:
            self.assertEqual(
                self.binder._resolve_input_store(
                    actor_principal_id="operator",
                    spec=spec,
                    candidate_bindings=(),
                ),
                self.store.store_id,
            )
        finally:
            self.ledger.list_owner_memberships = original  # type: ignore[method-assign]

        unavailable_projection = RealmProjectionService(
            self.ledger,
            local_stores={},
            projection_root=self.root / "unavailable-projections",
        )
        unavailable = RealmProcessExecutionBinder(
            self.ledger,
            unavailable_projection,
            self.volume_service,
            self.provider,
        )
        with self.assertRaisesRegex(RealmConflict, "no common locally available"):
            unavailable._resolve_input_store(
                actor_principal_id="operator",
                spec=spec,
                candidate_bindings=(),
            )


class InitializationLeasePulseTest(unittest.TestCase):
    @staticmethod
    def _lease(*, expires_at: float, revision: int):
        return SimpleNamespace(
            expires_at=expires_at,
            heartbeat_revision=revision,
        )

    def test_uses_each_returned_expiry_instead_of_one_global_ttl_cadence(
        self,
    ) -> None:
        clock = {"wall": 100.0, "monotonic": 10.0}
        projection = mock.Mock()
        projection.consumer_lease = self._lease(
            expires_at=110.0, revision=0
        )
        projection.heartbeat_initialization.side_effect = (
            self._lease(expires_at=110.0, revision=1),
        )
        early = mock.Mock(spec=ManagedEphemeralVolume)
        early.lease = self._lease(expires_at=110.0, revision=0)
        early.heartbeat_initialization.side_effect = (
            self._lease(expires_at=100.3, revision=1),
            self._lease(expires_at=110.0, revision=2),
        )
        later = mock.Mock(spec=ManagedEphemeralVolume)
        later.lease = self._lease(expires_at=110.0, revision=0)
        later.heartbeat_initialization.side_effect = (
            self._lease(expires_at=110.0, revision=1),
        )

        with (
            mock.patch(
                "optpilot.realm.process_execution_binder.time.time",
                side_effect=lambda: clock["wall"],
            ),
            mock.patch(
                "optpilot.realm.process_execution_binder.time.monotonic",
                side_effect=lambda: clock["monotonic"],
            ),
        ):
            pulse = _InitializationLeasePulse(
                projection=projection,
                volumes=(),
                operation_prefix="test/initialization",
                ttl_seconds=3.0,
            )
            pulse.add_volume("early", early)
            pulse.add_volume("later", later)
            pulse.pulse(force=True)

            clock["wall"] += 0.11
            clock["monotonic"] += 0.11
            pulse.pulse()

        self.assertEqual(early.heartbeat_initialization.call_count, 2)
        self.assertEqual(later.heartbeat_initialization.call_count, 1)
        self.assertEqual(projection.heartbeat_initialization.call_count, 1)

    def test_stale_overlapping_completion_cannot_replace_newer_schedule(
        self,
    ) -> None:
        clock = {"wall": 100.0, "monotonic": 10.0}
        projection = mock.Mock()
        projection.consumer_lease = self._lease(
            expires_at=110.0, revision=0
        )
        with (
            mock.patch(
                "optpilot.realm.process_execution_binder.time.time",
                side_effect=lambda: clock["wall"],
            ),
            mock.patch(
                "optpilot.realm.process_execution_binder.time.monotonic",
                side_effect=lambda: clock["monotonic"],
            ),
        ):
            pulse = _InitializationLeasePulse(
                projection=projection,
                volumes=(),
                operation_prefix="test/overlap",
                ttl_seconds=3.0,
            )
            pulse._record_heartbeat(
                ("projection", ""),
                self._lease(expires_at=110.0, revision=2),
            )
            clock["wall"] = 101.0
            clock["monotonic"] = 11.0
            pulse._record_heartbeat(
                ("projection", ""),
                self._lease(expires_at=100.5, revision=1),
            )

        self.assertEqual(
            pulse._heartbeat_revisions[("projection", "")],
            2,
        )
        self.assertEqual(pulse._next_due[("projection", "")], 11.0)


class RealmProcessInputStoreSelectionTest(unittest.TestCase):
    def test_prepared_runtime_requires_a_common_authorized_store(self) -> None:
        definition = _definition_with_prepared_python()
        spec = _compile_runtime(definition)
        environment_snapshot, prepared_snapshot = (
            item.snapshot_ref for item in spec.projection_spec.mappings
        )
        environment_binding = OwnerMembership(
            "store-b", environment_snapshot, RUN_ENVIRONMENT_SOURCE_ROLE
        )
        prepared_bindings = (
            OwnerMembership(
                "store-a", prepared_snapshot, RUN_PREPARED_RUNTIME_ROLE
            ),
            OwnerMembership(
                "store-b", prepared_snapshot, RUN_PREPARED_RUNTIME_ROLE
            ),
        )
        binder = object.__new__(RealmProcessExecutionBinder)
        binder._ledger = mock.Mock()
        binder._ledger.list_owner_memberships.return_value = (
            environment_binding,
            *prepared_bindings,
        )
        binder._projection_service = mock.Mock(
            available_store_ids=("store-a", "store-b")
        )

        self.assertEqual(
            binder._resolve_input_store(
                actor_principal_id="operator",
                spec=spec,
                candidate_bindings=(),
            ),
            "store-b",
        )

        binder._ledger.list_owner_memberships.return_value = (
            environment_binding,
            prepared_bindings[0],
        )
        with self.assertRaisesRegex(RealmConflict, "no common locally available"):
            binder._resolve_input_store(
                actor_principal_id="operator",
                spec=spec,
                candidate_bindings=(),
            )

    def test_replicated_candidate_selects_common_local_store_and_fails_on_revocation(
        self,
    ) -> None:
        definition, evaluation, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )
        spec = _compile_runtime(
            definition,
            evaluation,
            candidate_input=candidate_input,
        )
        environment_snapshot = spec.projection_spec.mappings[0].snapshot_ref
        candidate_snapshot = candidate_input.snapshot_ref
        self.assertIsNotNone(candidate_snapshot)
        environment_binding = OwnerMembership(
            "store-b", environment_snapshot, RUN_ENVIRONMENT_SOURCE_ROLE
        )
        candidate_bindings = (
            OwnerMembership("store-a", candidate_snapshot, RUN_CANDIDATE_ROLE),
            OwnerMembership("store-b", candidate_snapshot, RUN_CANDIDATE_ROLE),
        )
        memberships = (environment_binding, *candidate_bindings)
        binder = object.__new__(RealmProcessExecutionBinder)
        binder._ledger = mock.Mock()
        binder._ledger.list_owner_memberships.return_value = memberships
        binder._projection_service = mock.Mock(
            available_store_ids=("store-a", "store-b")
        )

        self.assertEqual(
            binder._resolve_input_store(
                actor_principal_id="operator",
                spec=spec,
                candidate_bindings=candidate_bindings,
            ),
            "store-b",
        )

        binder._ledger.list_owner_memberships.return_value = (
            environment_binding,
            candidate_bindings[0],
        )
        with self.assertRaisesRegex(RealmConflict, "not authorized"):
            binder._resolve_input_store(
                actor_principal_id="operator",
                spec=spec,
                candidate_bindings=candidate_bindings,
            )


if __name__ == "__main__":
    unittest.main()
