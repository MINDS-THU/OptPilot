from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.attempts import AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ephemeral_volume_records import EphemeralVolumeState
from optpilot.realm.ephemeral_volume_service import RealmEphemeralVolumeService
from optpilot.realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from optpilot.realm.execution_binding_records import (
    ExecutionProjectionHandle,
    ExecutionVolumeHandle,
    projection_private_coordinate_digest,
    run_attempt_binding_operation_id,
    run_attempt_projection_operation_id,
    run_attempt_resource_holder_id,
    run_attempt_terminal_evidence_operation_id,
    run_attempt_volume_operation_id,
)
from optpilot.realm.ledger import (
    RealmLedger,
    _authenticate_attempt_projection_sources,
)
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection import TreeMapping
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.refs import SnapshotRef
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.service import RealmContentService
from optpilot.retained_study_service import RetainedStudyService
from optpilot.runtime_binding import (
    CANDIDATE_PROJECTION_PARTITION,
    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
    ENVIRONMENT_PROJECTION_PARTITION,
    CandidateRuntimeInput,
    compile_retained_process_attempt_runtime,
)
from tests.test_retained_study_service import _write_package


class RealmExecutionBindingLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.study_path = _write_package(self.package_root)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        for principal in ("operator", "other"):
            self.ledger.register_principal(
                operation_id=f"binding/principal/{principal}",
                principal_id=principal,
                kind="human",
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="binding/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content_service = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.projection_service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.volume_service = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )
        self.provider = ProcessProviderIdentity(
            builder_fingerprint="a" * 64,
            platform="test-platform",
        )
        service = RetainedStudyService(
            self.ledger,
            self.content_service,
            self.projection_service,
            self.provider,
        )
        preparation = service.prepare_local_package(
            operation_id="binding/prepare-package",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id="binding-source-owner",
            study_definition_owner_id="binding-definition-owner",
        )
        self.definition = preparation.study_definition.manifest.run_definition
        self.created = service.launch_definition_run(
            operation_id="binding/launch-run",
            actor_principal_id="operator",
            controller_holder_id="controller",
            controller_ttl_seconds=300,
            preparation=preparation,
            run_id="binding-run",
            owner_id="binding-run-owner",
        )
        self._managed_projection = None
        self._managed_volumes = []
        self.launch_request_digest = "d" * 64
        self._counter = 0
        self._admit()
        self.prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("attempt-prepare"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
            expected_run_revision=1,
            attempt_ttl_seconds=300,
            **self.controller_arguments(),
        )

    def tearDown(self) -> None:
        for volume in reversed(self._managed_volumes):
            try:
                volume.close()
            except Exception:
                pass
        if self._managed_projection is not None:
            try:
                self._managed_projection.close()
            except Exception:
                pass
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self._counter += 1
        return f"binding/{self._counter}/{label}"

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
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=300,
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            change_id=change.change_id,
            plan=plan,
            **self.controller_arguments(),
        )

    def realize(
        self,
        *,
        resource_holder_override: str | None = None,
        resource_ttl_override: float | None = None,
    ):
        attempt = self.prepared.attempt
        spec = compile_retained_process_attempt_runtime(
            owner_id=self.created.run.owner_id,
            run_definition=self.definition,
            evaluation_spec=attempt.evaluation_spec,
            provider=self.provider,
        )
        resource_holder = run_attempt_resource_holder_id(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding_id=attempt.binding_id,
        )
        resource_ttl = (
            self.prepared.attempt_lease.expires_at
            - self.prepared.attempt_lease.created_at
        )
        if resource_holder_override is not None:
            resource_holder = resource_holder_override
        if resource_ttl_override is not None:
            resource_ttl = resource_ttl_override
        projection_operation = run_attempt_projection_operation_id(
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            binding_id=attempt.binding_id,
            logical_name=spec.projection_name,
        )
        projection = self.projection_service.project_read_only(
            operation_id=projection_operation,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec.projection_spec,
            holder_id=resource_holder,
            ttl_seconds=resource_ttl,
            consumer_kind="run-attempt",
            consumer_metadata={
                "attempt_id": attempt.attempt_id,
                "binding_id": attempt.binding_id,
                "logical_name": spec.projection_name,
                "run_id": attempt.run_id,
                "schema": "optpilot.run-attempt-projection-consumer.v1",
            },
            sharing_policy="private",
        )
        self._managed_projection = projection
        projection_handle = ExecutionProjectionHandle(
            logical_name=spec.projection_name,
            provider_kind=projection.realization.provider_kind,
            realization_id=projection.realization.realization_id,
            consumer_id=projection.consumer_id,
            consumer_lease_id=projection.consumer_lease.lease_id,
            consumer_fencing_token=projection.consumer_lease.fencing_token,
        )
        volume_handles = []
        for requirement in spec.writable_volumes:
            volume = self.volume_service.create(
                operation_id=run_attempt_volume_operation_id(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    binding_id=attempt.binding_id,
                    logical_name=requirement.name,
                ),
                actor_principal_id="operator",
                parent_lease=self.prepared.attempt_lease,
                holder_id=resource_holder,
                quota=requirement.quota,
                quota_enforcement=requirement.quota_enforcement,
                ttl_seconds=resource_ttl,
            )
            self._managed_volumes.append(volume)
            volume_handles.append(
                ExecutionVolumeHandle(
                    logical_name=requirement.name,
                    provider_kind=volume.record.provider_kind,
                    volume_id=volume.record.volume_id,
                    usage_lease_id=volume.lease.lease_id,
                    usage_fencing_token=volume.lease.fencing_token,
                )
            )
        return spec, projection_handle, tuple(volume_handles)

    def bind(self, *, operation_id: str | None = None):
        spec, projection, volumes = self.realize()
        if operation_id is None:
            operation_id = run_attempt_binding_operation_id(
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                binding_id=self.prepared.attempt.binding_id,
            )
        draft = self.ledger.preflight_run_attempt_binding(
            actor_principal_id="operator",
            run_id=self.prepared.attempt.run_id,
            attempt_id=self.prepared.attempt.attempt_id,
            run_definition_digest=self.definition.digest,
            provider=self.provider,
            projections=(projection,),
            writable_volumes=volumes,
            resource_ttl_seconds=(
                self.prepared.attempt_lease.expires_at
                - self.prepared.attempt_lease.created_at
            ),
            expected_run_revision=self.prepared.run.current_revision,
            **self.controller_arguments(),
        )
        self.binding_draft = draft
        receipt = self.ledger.commit_run_attempt_binding(
            operation_id=operation_id,
            actor_principal_id="operator",
            draft=draft,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=self.prepared.run.current_revision,
            **self.controller_arguments(),
        )
        return spec, receipt

    def replace_controller(self):
        previous = self.created.controller_lease
        return self.ledger.replace_run_controller(
            operation_id=self.op("replace-controller"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_controller_generation=self.created.run.controller_generation,
            expected_controller_lease_id=previous.lease_id,
            expected_controller_holder_id=previous.holder_id,
            expected_controller_fencing_token=previous.fencing_token,
            new_controller_holder_id="replacement-controller",
            controller_ttl_seconds=300,
        )

    def retain_terminal_evidence(
        self,
        receipt,
        *,
        started: bool,
        disposition: str,
        proof_fingerprint: str = "e" * 64,
    ):
        return self.ledger.commit_run_attempt_terminal_evidence(
            operation_id=run_attempt_terminal_evidence_operation_id(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                binding_id=receipt.binding.binding_id,
                proof_fingerprint=proof_fingerprint,
            ),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            binding_id=receipt.binding.binding_id,
            launch_token=receipt.attempt.launch_token,
            provider_kind=receipt.binding.portable_spec.provider.kind,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            proof_fingerprint=proof_fingerprint,
            started=started,
            disposition=disposition,
        )

    @staticmethod
    def replacement_arguments(replacement) -> dict[str, object]:
        lease = replacement.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def reconcile_loss(self, replacement, *, operation_id: str):
        return self.ledger.reconcile_lost_run_attempt(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.prepared.attempt.run_id,
            attempt_id=self.prepared.attempt.attempt_id,
            expected_run_revision=replacement.run.current_revision,
            expected_owner_revision=self.prepared.revision.owner_revision,
            **self.replacement_arguments(replacement),
        )

    def test_preflight_derives_candidate_runtime_input_from_retained_admission(
        self,
    ) -> None:
        _spec, projection, volumes = self.realize()
        with mock.patch(
            "optpilot.realm.ledger.compile_retained_process_attempt_runtime",
            wraps=compile_retained_process_attempt_runtime,
        ) as compiler:
            self.ledger.preflight_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                run_definition_digest=self.definition.digest,
                provider=self.provider,
                projections=(projection,),
                writable_volumes=volumes,
                resource_ttl_seconds=(
                    self.prepared.attempt_lease.expires_at
                    - self.prepared.attempt_lease.created_at
                ),
                expected_run_revision=self.prepared.run.current_revision,
                **self.controller_arguments(),
            )

        candidate_input = compiler.call_args.kwargs["candidate_input"]
        self.assertIsInstance(candidate_input, CandidateRuntimeInput)
        self.assertEqual(candidate_input.candidate_format, "parameters")
        self.assertEqual(
            str(candidate_input.candidate_ref),
            self.prepared.attempt.evaluation_spec.candidate_ref,
        )
        self.assertIsNone(candidate_input.snapshot_ref)

    def test_bind_read_replay_and_confirm_are_fenced_and_canonical(self) -> None:
        spec, receipt = self.bind()
        replay = self.ledger.commit_run_attempt_binding(
            operation_id=run_attempt_binding_operation_id(
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                binding_id=self.prepared.attempt.binding_id,
            ),
            actor_principal_id="operator",
            draft=self.binding_draft,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=self.prepared.run.current_revision,
            **self.controller_arguments(),
        )

        self.assertEqual(replay, receipt)
        self.assertEqual(receipt.binding.portable_spec, spec)
        self.assertEqual(
            receipt.binding.resource_ttl_seconds,
            self.prepared.attempt_lease.expires_at
            - self.prepared.attempt_lease.created_at,
        )
        self.assertEqual(receipt.revision.operation_kind, "run.attempt.bind")
        self.assertEqual(receipt.attempt.state, "prepared")
        self.assertEqual(
            receipt.launch_intent.created_txn_id,
            receipt.binding.created_txn_id,
        )
        self.assertEqual(
            receipt.launch_intent.created_at,
            receipt.binding.created_at,
        )
        self.assertEqual(
            receipt.launch_intent.launch_request_digest,
            self.launch_request_digest,
        )
        self.assertEqual(
            self.ledger.read_run_attempt_binding(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
            ),
            receipt.binding,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_binding(
                actor_principal_id="other",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
            )
        encoded = json.dumps(receipt.binding.portable_record(), sort_keys=True)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(receipt.binding.projections[0].realization_id, encoded)
        authority = self.ledger.validate_run_attempt_binding_authority(
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertEqual(authority.binding, receipt.binding)
        self.assertEqual(authority.attempt_lease, self.prepared.attempt_lease)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=receipt.binding.run_id
        )
        self.assertEqual(snapshot.execution_bindings, (receipt.binding,))
        self.assertEqual(
            snapshot.execution_launch_intents,
            (receipt.launch_intent,),
        )

        launched = self.ledger.confirm_run_attempt_launch(
            operation_id="binding/attempt-confirm",
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            launch_token=receipt.attempt.launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=receipt.run.current_revision,
            **self.controller_arguments(),
        )
        self.assertEqual(launched.attempt.state, "running")

    def test_bind_rejects_substitution_stale_fence_host_path_and_wrong_digest(self) -> None:
        _spec, projection, volumes = self.realize()
        swapped = tuple(
            replace(item, logical_name=volumes[1 - index].logical_name)
            for index, item in enumerate(volumes)
        )
        common = {
            "actor_principal_id": "operator",
            "run_id": self.prepared.attempt.run_id,
            "attempt_id": self.prepared.attempt.attempt_id,
            "run_definition_digest": self.definition.digest,
            "provider": self.provider,
            "resource_ttl_seconds": (
                self.prepared.attempt_lease.expires_at
                - self.prepared.attempt_lease.created_at
            ),
            "expected_run_revision": self.prepared.run.current_revision,
            **self.controller_arguments(),
        }
        with self.assertRaises(RealmConflict):
            self.ledger.preflight_run_attempt_binding(
                projections=(projection,),
                writable_volumes=swapped,
                **common,
            )
        with self.assertRaises((RealmConflict, RealmExpired)):
            self.ledger.preflight_run_attempt_binding(
                projections=(
                    replace(
                        projection,
                        consumer_fencing_token=projection.consumer_fencing_token + 1,
                    ),
                ),
                writable_volumes=volumes,
                **common,
            )
        with self.assertRaisesRegex(ValueError, "path-free"):
            self.ledger.preflight_run_attempt_binding(
                projections=(
                    replace(projection, realization_id="/tmp/stolen-projection"),
                ),
                writable_volumes=volumes,
                **common,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.preflight_run_attempt_binding(
                projections=(projection,),
                writable_volumes=volumes,
                **{**common, "run_definition_digest": "f" * 64},
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
            )

    def test_confirm_requires_exact_binding_and_bound_resources_are_retained(self) -> None:
        _spec, receipt = self.bind()
        arguments = {
            "operation_id": self.op("confirm"),
            "actor_principal_id": "operator",
            "run_id": receipt.binding.run_id,
            "attempt_id": receipt.binding.attempt_id,
            "launch_token": receipt.attempt.launch_token,
            "binding_id": receipt.binding.binding_id,
            "evidence_fingerprint": receipt.binding.evidence_fingerprint,
            "launch_request_digest": self.launch_request_digest,
            "expected_run_revision": receipt.run.current_revision,
            **self.controller_arguments(),
        }
        with self.assertRaises(RealmNotFound):
            self.ledger.confirm_run_attempt_launch(
                **{**arguments, "evidence_fingerprint": "f" * 64}
            )
        projection = receipt.binding.projections[0]
        with self.assertRaises(RealmConflict):
            self.ledger.release_projection_consumer(
                operation_id=self.op("release-projection"),
                actor_principal_id="operator",
                realization_id=projection.realization_id,
                consumer_id=projection.consumer_id,
                consumer_holder_id=run_attempt_resource_holder_id(
                    run_id=receipt.binding.run_id,
                    attempt_id=receipt.binding.attempt_id,
                    binding_id=receipt.binding.binding_id,
                ),
                consumer_fencing_token=projection.consumer_fencing_token,
            )
        volume = receipt.binding.writable_volumes[0]
        with self.assertRaises(RealmConflict):
            self.ledger.release_ephemeral_volume(
                operation_id=self.op("release-volume"),
                actor_principal_id="operator",
                volume_id=volume.volume_id,
                holder_id=run_attempt_resource_holder_id(
                    run_id=receipt.binding.run_id,
                    attempt_id=receipt.binding.attempt_id,
                    binding_id=receipt.binding.binding_id,
                ),
                fencing_token=volume.usage_fencing_token,
            )
        with self.assertRaises(RealmConflict):
            self.ledger.retire_private_projection_consumer(
                operation_id=self.op("retire-live-projection"),
                actor_principal_id="operator",
                projection_root_id=(
                    self.projection_service.root_binding.projection_root_id
                ),
                realization_id=projection.realization_id,
                consumer_id=projection.consumer_id,
                consumer_holder_id=run_attempt_resource_holder_id(
                    run_id=receipt.binding.run_id,
                    attempt_id=receipt.binding.attempt_id,
                    binding_id=receipt.binding.binding_id,
                ),
                consumer_fencing_token=projection.consumer_fencing_token,
                expected_operation_coordinate_digest=(
                    projection_private_coordinate_digest(
                        realm_id=self.ledger.realm_id,
                        operation_id=run_attempt_projection_operation_id(
                            run_id=receipt.binding.run_id,
                            attempt_id=receipt.binding.attempt_id,
                            binding_id=receipt.binding.binding_id,
                            logical_name=projection.logical_name,
                        ),
                    )
                ),
            )
        with self.assertRaises(RealmConflict):
            self.ledger.acquire_projection_consumer(
                operation_id=self.op("acquire-sibling-consumer"),
                actor_principal_id="operator",
                realization_id=projection.realization_id,
                consumer_holder_id="sibling-viewer",
                consumer_ttl_seconds=60,
                consumer_kind="inspection",
                metadata={"surface": "debug"},
            )
        launched = self.ledger.confirm_run_attempt_launch(**arguments)
        self.assertEqual(launched.attempt.state, "running")
        durable = self.ledger.read_run_attempt_binding(
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertEqual(durable, receipt.binding)

    def test_resource_heartbeats_after_bind_do_not_invalidate_confirm(self) -> None:
        _spec, receipt = self.bind()
        assert self._managed_projection is not None
        self._managed_projection.heartbeat(
            operation_id=self.op("projection-heartbeat"), ttl_seconds=120
        )
        for index, volume in enumerate(self._managed_volumes):
            volume.heartbeat(
                operation_id=self.op(f"volume-heartbeat-{index}"),
                ttl_seconds=120,
            )

        launched = self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("confirm-after-heartbeat"),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            launch_token=receipt.attempt.launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=receipt.run.current_revision,
            **self.controller_arguments(),
        )

        self.assertEqual(launched.attempt.state, "running")
        authority = self.ledger.validate_run_attempt_binding_authority(
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
        )
        self.assertEqual(authority.binding, receipt.binding)

    def test_terminal_attempt_releases_cleanup_retention(self) -> None:
        _spec, receipt = self.bind()
        projection = receipt.binding.projections[0]
        finalization = AttemptFinalization(
            attempt_id=receipt.attempt.attempt_id,
            evaluation_spec_digest=receipt.attempt.evaluation_spec_digest,
            binding_id=receipt.attempt.binding_id,
            effective_outcome="failed",
            effective_code="worker_cancelled",
            captured_artifacts=(),
            platform_error={
                "code": "worker_cancelled",
                "message": "test worker stopped",
                "details": {},
            },
        )
        terminal_fingerprint = "e" * 64
        terminal = self.ledger.commit_run_attempt_terminal_evidence(
            operation_id=run_attempt_terminal_evidence_operation_id(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                binding_id=receipt.binding.binding_id,
                proof_fingerprint=terminal_fingerprint,
            ),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            binding_id=receipt.binding.binding_id,
            launch_token=receipt.attempt.launch_token,
            provider_kind=receipt.binding.portable_spec.provider.kind,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            proof_fingerprint=terminal_fingerprint,
            started=False,
            disposition="never_started",
        )
        terminal_payload = terminal.to_dict()
        self.assertEqual(
            set(terminal_payload),
            {
                "attempt_id",
                "binding_id",
                "created_at",
                "created_by_principal_id",
                "created_txn_id",
                "disposition",
                "evidence_fingerprint",
                "launch_request_digest",
                "launch_token",
                "proof_fingerprint",
                "provider_kind",
                "run_id",
                "started",
            },
        )
        encoded_terminal = json.dumps(terminal_payload, sort_keys=True)
        self.assertNotIn(str(self.root), encoded_terminal)
        self.assertNotIn("backend_token", encoded_terminal)
        self.assertNotIn("raw_proof", encoded_terminal)
        adopted = self.ledger.adopt_run_attempt(
            operation_id=self.op("adopt-terminal"),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            change_id=receipt.attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=receipt.run.current_revision,
            expected_owner_revision=receipt.revision.owner_revision,
            **self.controller_arguments(),
        )
        self.assertEqual(adopted.attempt.state, "terminal")
        cleanup_authorization = (
            self.ledger.read_run_attempt_cleanup_authorization(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
            )
        )
        self.assertEqual(
            cleanup_authorization.terminal_evidence_fingerprint,
            terminal.proof_fingerprint,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            for statement in (
                "UPDATE run_attempt_execution_terminal_evidence "
                "SET disposition = 'exited' WHERE run_id = ? AND attempt_id = ?",
                "DELETE FROM run_attempt_execution_terminal_evidence "
                "WHERE run_id = ? AND attempt_id = ?",
                "UPDATE run_attempt_execution_cleanup_authorizations "
                "SET authorized_by_principal_id = 'other' "
                "WHERE run_id = ? AND attempt_id = ?",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        statement,
                        (receipt.binding.run_id, receipt.binding.attempt_id),
                    )
                connection.rollback()
        finally:
            connection.close()

        coordinate_digest = projection_private_coordinate_digest(
            realm_id=self.ledger.realm_id,
            operation_id=run_attempt_projection_operation_id(
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                binding_id=receipt.binding.binding_id,
                logical_name=projection.logical_name,
            ),
        )
        retirement_arguments = {
            "operation_id": self.op("retire-terminal-projection"),
            "actor_principal_id": "operator",
            "projection_root_id": (
                self.projection_service.root_binding.projection_root_id
            ),
            "realization_id": projection.realization_id,
            "consumer_id": projection.consumer_id,
            "consumer_holder_id": run_attempt_resource_holder_id(
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                binding_id=receipt.binding.binding_id,
            ),
            "consumer_fencing_token": projection.consumer_fencing_token,
            "expected_operation_coordinate_digest": coordinate_digest,
        }
        with self.assertRaises(RealmConflict):
            self.ledger.retire_private_projection_consumer(
                operation_id=self.op("retire-wrong-coordinate"),
                actor_principal_id="operator",
                projection_root_id=(
                    self.projection_service.root_binding.projection_root_id
                ),
                realization_id=projection.realization_id,
                consumer_id=projection.consumer_id,
                consumer_holder_id=run_attempt_resource_holder_id(
                    run_id=receipt.binding.run_id,
                    attempt_id=receipt.binding.attempt_id,
                    binding_id=receipt.binding.binding_id,
                ),
                consumer_fencing_token=projection.consumer_fencing_token,
                expected_operation_coordinate_digest="f" * 64,
            )
        closing = self.ledger.retire_private_projection_consumer(
            **retirement_arguments
        )
        self.assertEqual(closing.state.value, "closing")
        cleaned_projection = self.projection_service.reconcile_projection(
            operation_id=self.op("reconcile-terminal-projection"),
            realization_id=projection.realization_id,
        )
        self.assertEqual(cleaned_projection.realization.state.value, "cleaned")
        self.assertEqual(
            self.ledger.retire_private_projection_consumer(
                **retirement_arguments
            ),
            closing,
        )

        for index, volume in enumerate(receipt.binding.writable_volumes):
            cleaned = self.volume_service.reconcile_volume(
                operation_id=self.op(f"reconcile-terminal-volume-{index}"),
                volume_id=volume.volume_id,
            )
            self.assertEqual(cleaned.volume.state, EphemeralVolumeState.CLEANED)

    def test_bound_adoption_requires_terminal_evidence_and_creates_no_authority(
        self,
    ) -> None:
        _spec, receipt = self.bind()
        finalization = AttemptFinalization(
            attempt_id=receipt.attempt.attempt_id,
            evaluation_spec_digest=receipt.attempt.evaluation_spec_digest,
            binding_id=receipt.attempt.binding_id,
            effective_outcome="failed",
            effective_code="worker_cancelled",
            captured_artifacts=(),
            platform_error={
                "code": "worker_cancelled",
                "message": "test worker stopped",
                "details": {},
            },
        )
        with self.assertRaisesRegex(RealmConflict, "terminal evidence"):
            self.ledger.adopt_run_attempt(
                operation_id=self.op("adopt-without-terminal-evidence"),
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                change_id=receipt.attempt.capture_change_id,
                finalization=finalization,
                expected_run_revision=receipt.run.current_revision,
                expected_owner_revision=receipt.revision.owner_revision,
                **self.controller_arguments(),
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_cleanup_authorization(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
            )

    def test_running_attempt_rejects_never_started_terminal_evidence(self) -> None:
        _spec, receipt = self.bind()
        launched = self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("confirm-before-never-started-evidence"),
            actor_principal_id="operator",
            run_id=receipt.binding.run_id,
            attempt_id=receipt.binding.attempt_id,
            launch_token=receipt.attempt.launch_token,
            binding_id=receipt.binding.binding_id,
            evidence_fingerprint=receipt.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=receipt.run.current_revision,
            **self.controller_arguments(),
        )
        self.assertEqual(launched.attempt.state, "running")
        fingerprint = "e" * 64
        with self.assertRaisesRegex(RealmConflict, "confirmed-running"):
            self.ledger.commit_run_attempt_terminal_evidence(
                operation_id=run_attempt_terminal_evidence_operation_id(
                    actor_principal_id="operator",
                    run_id=receipt.binding.run_id,
                    attempt_id=receipt.binding.attempt_id,
                    binding_id=receipt.binding.binding_id,
                    proof_fingerprint=fingerprint,
                ),
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
                binding_id=receipt.binding.binding_id,
                launch_token=receipt.attempt.launch_token,
                provider_kind=receipt.binding.portable_spec.provider.kind,
                evidence_fingerprint=receipt.binding.evidence_fingerprint,
                launch_request_digest=self.launch_request_digest,
                proof_fingerprint=fingerprint,
                started=False,
                disposition="never_started",
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_terminal_evidence(
                actor_principal_id="operator",
                run_id=receipt.binding.run_id,
                attempt_id=receipt.binding.attempt_id,
            )

    def test_bind_rejects_nondeterministic_resource_holder(self) -> None:
        _spec, projection, volumes = self.realize(
            resource_holder_override="caller-chosen-worker"
        )
        with self.assertRaises(RealmConflict):
            self.ledger.preflight_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                run_definition_digest=self.definition.digest,
                provider=self.provider,
                projections=(projection,),
                writable_volumes=volumes,
                resource_ttl_seconds=(
                    self.prepared.attempt_lease.expires_at
                    - self.prepared.attempt_lease.created_at
                ),
                expected_run_revision=self.prepared.run.current_revision,
                **self.controller_arguments(),
            )

    def test_bind_rejects_changed_resource_lease_duration(self) -> None:
        original_ttl = (
            self.prepared.attempt_lease.expires_at
            - self.prepared.attempt_lease.created_at
        )
        _spec, projection, volumes = self.realize(
            resource_ttl_override=original_ttl - 10
        )
        with self.assertRaises(RealmConflict):
            self.ledger.preflight_run_attempt_binding(
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                run_definition_digest=self.definition.digest,
                provider=self.provider,
                projections=(projection,),
                writable_volumes=volumes,
                resource_ttl_seconds=original_ttl,
                expected_run_revision=self.prepared.run.current_revision,
                **self.controller_arguments(),
            )

    def test_binding_tables_are_immutable_and_schema_is_typed(self) -> None:
        _spec, receipt = self.bind()
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            for statement in (
                "UPDATE run_attempt_execution_bindings "
                "SET evidence_fingerprint = ? WHERE run_id = ? AND attempt_id = ?",
                "DELETE FROM run_attempt_execution_projections "
                "WHERE run_id = ? AND attempt_id = ?",
                "DELETE FROM run_attempt_execution_volumes "
                "WHERE run_id = ? AND attempt_id = ?",
                "UPDATE projection_realizations SET state = 'closing' "
                "WHERE realization_id = ?",
                "UPDATE ephemeral_volumes SET state = 'cleanup_pending' "
                "WHERE volume_id = ?",
            ):
                if statement.startswith(
                    "UPDATE run_attempt_execution_bindings"
                ):
                    parameters = (
                        "f" * 64,
                        receipt.binding.run_id,
                        receipt.binding.attempt_id,
                    )
                elif "projection_realizations" in statement:
                    parameters = (receipt.binding.projections[0].realization_id,)
                elif "ephemeral_volumes" in statement and statement.startswith(
                    "UPDATE"
                ):
                    parameters = (receipt.binding.writable_volumes[0].volume_id,)
                else:
                    parameters = (
                        receipt.binding.run_id,
                        receipt.binding.attempt_id,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement, parameters)
                connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT emits_events FROM run_revision_kinds "
                    "WHERE operation_kind = 'run.attempt.bind'"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_reconcile_unbound_prepared_attempt_is_fenced_replayable_and_cleans_capture(
        self,
    ) -> None:
        with self.assertRaisesRegex(RealmConflict, "strictly older"):
            self.ledger.reconcile_lost_run_attempt(
                operation_id=self.op("reconcile-current-generation"),
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                expected_run_revision=self.prepared.run.current_revision,
                expected_owner_revision=self.prepared.revision.owner_revision,
                **self.controller_arguments(),
            )

        staging_id = "stage-" + "a" * 32
        capture = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=self.prepared.attempt.capture_change_id,
            store_id=self.store.store_id,
        )
        capture.reserve_staging(
            change_id=self.prepared.attempt.capture_change_id,
            staging_id=staging_id,
            store_id=self.store.store_id,
            object_kind="blob",
        )
        replacement = self.replace_controller()
        tamper = sqlite3.connect(self.database_path)
        tamper.execute("PRAGMA foreign_keys = ON")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                tamper.execute(
                    "UPDATE run_attempts SET controller_generation = ? "
                    "WHERE run_id = ? AND attempt_id = ?",
                    (
                        replacement.run.controller_generation + 1,
                        self.prepared.attempt.run_id,
                        self.prepared.attempt.attempt_id,
                    ),
                )
            tamper.rollback()
            tamper.execute("BEGIN")
            tamper.execute(
                "UPDATE owner_transactions SET state = 'aborted' "
                "WHERE change_id = ?",
                (self.prepared.attempt.capture_change_id,),
            )
            cursor = tamper.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'run.attempt.reconcile', ?, '{}', ?)",
                (self.op("tampered-reconcile"), "f" * 64, 1.0),
            )
            bad_payload = {
                "binding_state": "unbound",
                "lost_controller_generation": 1,
                "replacement_controller_generation": 2,
                "started": True,
                "terminal_disposition": "exited",
            }
            with self.assertRaises(sqlite3.IntegrityError):
                tamper.execute(
                    "INSERT INTO run_attempt_transitions("
                    "run_id, attempt_id, transition_index, from_state, to_state, "
                    "outcome, code, payload_json, payload_digest, sequence, "
                    "run_revision, txn_id, created_at"
                    ") VALUES (?, ?, 2, 'prepared', 'terminal', 'failed', "
                    "'attempt_authority_lost', ?, ?, ?, ?, ?, ?)",
                    (
                        self.prepared.attempt.run_id,
                        self.prepared.attempt.attempt_id,
                        json.dumps(bad_payload, separators=(",", ":"), sort_keys=True),
                        "e" * 64,
                        replacement.run.next_sequence,
                        replacement.run.current_revision + 1,
                        cursor.lastrowid,
                        1.0,
                    ),
                )
            tamper.rollback()
        finally:
            tamper.close()
        with self.assertRaises(RealmConflict):
            self.ledger.reconcile_lost_run_attempt(
                operation_id=self.op("reconcile-old-fence"),
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                expected_run_revision=replacement.run.current_revision,
                expected_owner_revision=self.prepared.revision.owner_revision,
                **self.controller_arguments(),
            )

        operation_id = self.op("reconcile-unbound")
        reconciled = self.reconcile_loss(
            replacement, operation_id=operation_id
        )
        replay = self.reconcile_loss(replacement, operation_id=operation_id)

        self.assertEqual(replay, reconciled)
        self.assertEqual(reconciled.revision.operation_kind, "run.attempt.reconcile")
        self.assertEqual(reconciled.attempt.outcome, "failed")
        self.assertEqual(reconciled.attempt.code, "attempt_authority_lost")
        self.assertEqual(reconciled.logical_transition.to_state, "terminal")
        self.assertEqual(reconciled.revision.owner_revision, 0)
        self.assertEqual(
            dict(reconciled.attempt_transition.payload)["binding_state"],
            "unbound",
        )
        with self.assertRaises(RealmConflict):
            self.ledger.reconcile_lost_run_attempt(
                operation_id=operation_id,
                actor_principal_id="operator",
                run_id=self.prepared.attempt.run_id,
                attempt_id=self.prepared.attempt.attempt_id,
                expected_run_revision=replacement.run.current_revision,
                expected_owner_revision=self.prepared.revision.owner_revision + 1,
                **self.replacement_arguments(replacement),
            )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM owner_transactions WHERE change_id = ?",
                    (self.prepared.attempt.capture_change_id,),
                ).fetchone(),
                ("aborted",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM staging_allocations WHERE staging_id = ?",
                    (staging_id,),
                ).fetchone(),
                ("abandoned",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM leases WHERE lease_id = ?",
                    (self.prepared.attempt.attempt_lease_id,),
                ).fetchone(),
                ("revoked",),
            )
        finally:
            connection.close()

    def test_reconcile_bound_prepared_requires_evidence_and_authorizes_cleanup_atomically(
        self,
    ) -> None:
        _spec, bound = self.bind()
        replacement = self.replace_controller()
        with self.assertRaisesRegex(RealmConflict, "terminal evidence"):
            self.reconcile_loss(
                replacement, operation_id=self.op("reconcile-missing-evidence")
            )
        unchanged = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=bound.run.run_id
        )
        self.assertEqual(unchanged.run, replacement.run)
        self.assertEqual(unchanged.attempts[0].state, "prepared")
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_attempt_cleanup_authorization(
                actor_principal_id="operator",
                run_id=bound.run.run_id,
                attempt_id=bound.attempt.attempt_id,
            )

        evidence = self.retain_terminal_evidence(
            bound, started=False, disposition="never_started"
        )
        reconciled = self.reconcile_loss(
            replacement, operation_id=self.op("reconcile-bound-never-started")
        )
        cleanup = self.ledger.read_run_attempt_cleanup_authorization(
            actor_principal_id="operator",
            run_id=bound.run.run_id,
            attempt_id=bound.attempt.attempt_id,
        )
        exact_launch = self.ledger.read_run_attempt_binding_launch(
            actor_principal_id="operator",
            run_id=bound.run.run_id,
            attempt_id=bound.attempt.attempt_id,
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=bound.run.run_id
        )

        self.assertEqual(cleanup.created_txn_id, reconciled.revision.txn_id)
        self.assertEqual(
            cleanup.terminal_evidence_fingerprint, evidence.proof_fingerprint
        )
        self.assertEqual(exact_launch.attempt, reconciled.attempt)
        self.assertEqual(exact_launch.binding, bound.binding)
        self.assertEqual(exact_launch.launch_intent, bound.launch_intent)
        self.assertEqual(snapshot.observations, ())
        self.assertEqual(snapshot.artifacts, ())
        encoded = json.dumps(reconciled.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), encoded)

    def test_reconcile_running_bound_attempt_requires_started_evidence(self) -> None:
        _spec, bound = self.bind()
        launched = self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("confirm-before-loss"),
            actor_principal_id="operator",
            run_id=bound.run.run_id,
            attempt_id=bound.attempt.attempt_id,
            launch_token=bound.attempt.launch_token,
            binding_id=bound.binding.binding_id,
            evidence_fingerprint=bound.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=bound.run.current_revision,
            **self.controller_arguments(),
        )
        self.assertEqual(launched.attempt.state, "running")
        self.retain_terminal_evidence(
            bound, started=True, disposition="exited"
        )
        replacement = self.replace_controller()

        reconciled = self.reconcile_loss(
            replacement, operation_id=self.op("reconcile-running")
        )

        self.assertEqual(reconciled.attempt_transition.from_state, "running")
        self.assertTrue(reconciled.attempt_transition.payload["started"])
        self.assertEqual(
            reconciled.attempt_transition.payload["terminal_disposition"],
            "exited",
        )

    def test_reconcile_rejects_running_attempt_with_never_started_evidence(self) -> None:
        _spec, bound = self.bind()
        self.retain_terminal_evidence(
            bound, started=False, disposition="never_started"
        )
        launched = self.ledger.confirm_run_attempt_launch(
            operation_id=self.op("confirm-after-never-started-evidence"),
            actor_principal_id="operator",
            run_id=bound.run.run_id,
            attempt_id=bound.attempt.attempt_id,
            launch_token=bound.attempt.launch_token,
            binding_id=bound.binding.binding_id,
            evidence_fingerprint=bound.binding.evidence_fingerprint,
            launch_request_digest=self.launch_request_digest,
            expected_run_revision=bound.run.current_revision,
            **self.controller_arguments(),
        )
        replacement = self.replace_controller()

        with self.assertRaisesRegex(RealmIntegrityError, "never-started"):
            self.reconcile_loss(
                replacement,
                operation_id=self.op("reconcile-running-never-started"),
            )
        unchanged = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=bound.run.run_id
        )
        self.assertEqual(unchanged.run, replacement.run)
        self.assertEqual(unchanged.attempts[0], launched.attempt)


class AttemptProjectionSourceAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment_snapshot = SnapshotRef.from_manifest_bytes(b"environment")
        self.candidate_snapshot = SnapshotRef.from_manifest_bytes(b"candidate")
        self.other_snapshot = SnapshotRef.from_manifest_bytes(b"other")
        self.prepared_snapshot = SnapshotRef.from_manifest_bytes(b"prepared")
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"schema": "test-file-candidate.v1"},
            content_refs=(self.candidate_snapshot,),
        )
        self.candidate_input = CandidateRuntimeInput.from_envelope(envelope)
        self.mappings = (
            TreeMapping(
                self.environment_snapshot,
                destination=ENVIRONMENT_PROJECTION_PARTITION,
            ),
            TreeMapping(
                self.candidate_snapshot,
                destination=CANDIDATE_PROJECTION_PARTITION,
            ),
        )
        self.binding = OwnerMembership(
            "local-a", self.candidate_snapshot, RUN_CANDIDATE_ROLE
        )

    def authenticate(self, mappings=None, bindings=None, store_id="local-a"):
        return _authenticate_attempt_projection_sources(
            mappings=self.mappings if mappings is None else mappings,
            candidate_input=self.candidate_input,
            candidate_content_bindings=(
                (self.binding,) if bindings is None else bindings
            ),
            store_id=store_id,
        )

    def test_two_source_projection_returns_canonical_snapshot_set(self) -> None:
        self.assertEqual(
            self.authenticate(),
            tuple(
                sorted(
                    {self.environment_snapshot, self.candidate_snapshot},
                    key=str,
                )
            ),
        )

    def test_prepared_python_subtree_is_an_authenticated_projection_source(self) -> None:
        mappings = (
            self.mappings[0],
            TreeMapping(
                self.prepared_snapshot,
                destination=ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                source_subpath="site-packages",
            ),
            self.mappings[1],
        )

        self.assertEqual(
            self.authenticate(mappings=mappings),
            tuple(
                sorted(
                    {
                        self.environment_snapshot,
                        self.prepared_snapshot,
                        self.candidate_snapshot,
                    },
                    key=str,
                )
            ),
        )
        with self.assertRaisesRegex(RealmIntegrityError, "immutable subtrees"):
            self.authenticate(
                mappings=(
                    self.mappings[0],
                    replace(mappings[1], source_subpath="."),
                    self.mappings[1],
                )
            )

    def test_missing_extra_and_substituted_candidate_sources_are_rejected(
        self,
    ) -> None:
        cases = (
            ("missing", self.mappings[:1]),
            (
                "extra",
                self.mappings
                + (TreeMapping(self.other_snapshot, destination="unexpected"),),
            ),
            (
                "substituted",
                (
                    self.mappings[0],
                    TreeMapping(
                        self.other_snapshot,
                        destination=CANDIDATE_PROJECTION_PARTITION,
                    ),
                ),
            ),
        )
        for label, mappings in cases:
            with self.subTest(label=label), self.assertRaises(RealmIntegrityError):
                self.authenticate(mappings=mappings)

    def test_candidate_requires_exact_role_and_selected_store_placement(self) -> None:
        with self.assertRaisesRegex(RealmIntegrityError, "candidate bindings"):
            self.authenticate(
                bindings=(
                    OwnerMembership(
                        "local-a", self.candidate_snapshot, "unrelated-role"
                    ),
                )
            )
        with self.assertRaisesRegex(RealmConflict, "projection store"):
            self.authenticate(
                bindings=(
                    OwnerMembership(
                        "local-b", self.candidate_snapshot, RUN_CANDIDATE_ROLE
                    ),
                )
            )
        with self.assertRaisesRegex(RealmIntegrityError, "candidate bindings"):
            self.authenticate(
                bindings=(
                    self.binding,
                    OwnerMembership(
                        "local-a", self.other_snapshot, RUN_CANDIDATE_ROLE
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
