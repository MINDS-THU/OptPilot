from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ephemeral_volume_records import EphemeralVolumeState
from optpilot.realm.ephemeral_volume_service import RealmEphemeralVolumeService
from optpilot.realm.errors import RealmConflict, RealmExpired, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_process_supervisor import (
    ProcessLaunchSealReceipt,
    WorkerTerminalProof,
)
from optpilot.realm.operator_attempt_binding import (
    RealmOperatorAttemptBinder,
    _projection_metadata,
    _resource_holder_id,
    _resource_operation_id,
    _resolve_exact_projection_store,
)
from optpilot.realm.operator_job_records import (
    OperatorJobLaunchPlan,
    OperatorJobTarget,
)
from optpilot.realm.owner_derivation import Binding, OwnerDerivationManifest
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection_records import ProjectionRealizationState
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.refs import SnapshotRef, canonical_json_bytes, request_digest
from optpilot.realm.run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
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
from optpilot.realm.selections import SelectionRef
from optpilot.realm.service import RealmContentService
from optpilot.retained_study_service import RetainedStudyService
from optpilot.runtime_binding import compile_retained_process_attempt_runtime
from tests.core.test_retained_study_service import _write_package
from tests.core.test_runtime_binding import (
    _definition,
    _definition_with_prepared_python,
    _evaluation_spec,
    _file_definition_and_candidate_input,
)
from tests.core.test_retained_study_compiler import (
    _provider,
    _study_with_trial_workspace,
)


class RealmOperatorAttemptBinderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        study_path = _write_package(self.package_root)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.counter = 0
        self.ledger.register_principal(
            operation_id=self.op("principal"),
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id=self.op("store"),
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
        retained = RetainedStudyService(
            self.ledger,
            self.content_service,
            self.projection_service,
            self.provider,
        )
        package = retained.prepare_local_package(
            operation_id=self.op("package/prepare"),
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=study_path,
            source_owner_id="operator-binding-source-owner",
            study_definition_owner_id="operator-binding-definition-owner",
        )
        self.definition = package.study_definition.manifest.run_definition
        self.created = retained.launch_definition_run(
            operation_id=self.op("run/launch"),
            actor_principal_id="operator",
            controller_holder_id="operator-binding-controller",
            controller_ttl_seconds=300,
            preparation=package,
            run_id="operator-binding-source-run",
            owner_id="operator-binding-source-run-owner",
        )
        self.candidate_envelope = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 0.5}
        )
        self._admit_candidate()
        preparation = self.ledger.prepare_run_attempt(
            operation_id=self.op("attempt/prepare"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="canonical-source-attempt",
            expected_run_revision=1,
            attempt_ttl_seconds=300,
            **self.controller_arguments(),
        )
        self.evaluation_spec = preparation.attempt.evaluation_spec

        source_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        environment_memberships = tuple(
            item
            for item in source_memberships
            if item.role == RUN_ENVIRONMENT_SOURCE_ROLE
        )
        self.assertEqual(len(environment_memberships), 1)
        self.environment_membership = environment_memberships[0]
        self.derivation = OwnerDerivationManifest(
            target_owner_id="operator-binding-job-owner",
            target_owner_kind="operator-job",
            sources=(
                self.ledger.read_owner_source_anchor(
                    actor_principal_id="operator",
                    owner_id=self.created.run.owner_id,
                ),
            ),
            bindings=(
                Binding(
                    source_owner_id=self.created.run.owner_id,
                    source_store_id=self.environment_membership.store_id,
                    content_ref=self.environment_membership.content_ref,
                    source_role=self.environment_membership.role,
                    target_role=RUN_ENVIRONMENT_SOURCE_ROLE,
                ),
            ),
        )
        self.ledger.derive_owner(
            operation_id=self.op("job-owner/derive"),
            actor_principal_id="operator",
            manifest=self.derivation,
        )
        self.portable_spec = compile_retained_process_attempt_runtime(
            owner_id=self.derivation.target_owner_id,
            run_definition=self.definition,
            evaluation_spec=self.evaluation_spec,
            provider=self.provider,
        )
        selection = SelectionRef.build(
            kind="candidate",
            source_kind="run",
            source_id=self.created.run.run_id,
            source_owner_id=self.created.run.owner_id,
            source_revision=1,
            owner_revision=1,
            source_sequence=1,
            entity_sequence=1,
            entity_id="candidate-a",
            entity_ref=str(self.candidate_envelope.candidate_ref),
            context_digest=(
                self.definition.evaluation_closure.evaluation_template.digest
            ),
        )
        input_facts = {
            "evaluation_spec_digest": self.evaluation_spec.digest,
            "portable_spec_digest": self.portable_spec.digest,
        }
        self.plan = OperatorJobLaunchPlan(
            job_kind="candidate-debug-run",
            target=OperatorJobTarget(
                kind="candidate-evaluation", selection=selection
            ),
            input_facts=input_facts,
            input_facts_digest=hashlib.sha256(
                canonical_json_bytes(input_facts)
            ).hexdigest(),
            owner_derivation_manifest_digest=self.derivation.digest,
            source_fingerprints=tuple(
                sorted(
                    {
                        self.definition.digest,
                        self.evaluation_spec.environment_revision_digest,
                        self.portable_spec.digest,
                    }
                )
            ),
            runtime_fingerprint=self.portable_spec.digest,
            entrypoint_profile="default",
            projection_contract_digest=self.portable_spec.projection_spec.digest,
            backend_kind="local-process",
            backend_realm="local-host",
            resource_claims={"cpu_millis": 1000, "memory_bytes": 1024},
            timeout_seconds=self.portable_spec.timeout_seconds or 30,
            network_policy="denied",
            network_enforcement="advisory",
            requested_secret_names=(),
            grants_digest="b" * 64,
            evidence_sink_kind="operator-job-result",
            evidence_sink_id="operator-binding-debug-attempt",
            evidence_sink_digest="c" * 64,
            cancellation_guarantee="confirmed",
            priority_class="interactive",
        )
        planned = self.ledger.plan_operator_job(
            operation_id=self.op("job/plan"),
            actor_principal_id="operator",
            job_owner_id=self.derivation.target_owner_id,
            plan=self.plan,
            job_id="operator-binding-job",
        )
        awaiting = self.ledger.request_operator_job_approval(
            operation_id=self.op("job/request-approval"),
            actor_principal_id="operator",
            job_id=planned.job_id,
            expected_revision=planned.revision,
        )
        self.job = self.ledger.approve_operator_job(
            operation_id=self.op("job/approve"),
            actor_principal_id="operator",
            job_id=planned.job_id,
            expected_revision=awaiting.revision,
            expected_plan_digest=self.plan.digest,
            approval_scope_digest="d" * 64,
        )
        self.admission = self.ledger.acquire_lease(
            operation_id=self.op("job/admission"),
            actor_principal_id="operator",
            owner_id=self.derivation.target_owner_id,
            lease_kind="operator-job-admission",
            audience="operator-job",
            holder_id=_logical_id(
                "operator-job-holder", {"job_id": self.job.job_id}
            ),
            scope_key=f"operator-job-admission:{self.job.job_id}",
            ttl_seconds=300,
            metadata={"job_id": self.job.job_id, "plan_digest": self.job.plan_digest},
            lease_id=_logical_id(
                "operator-job-admission", {"job_id": self.job.job_id}
            ),
        )
        self.binder = RealmOperatorAttemptBinder(
            self.ledger, self.projection_service, self.volume_service
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"operator-binding/{self.counter}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit_candidate(self) -> None:
        plan = RunAdmissionPlan(
            candidates=(
                CandidateAdmission(
                    "candidate-a",
                    self.candidate_envelope,
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

    def binding_arguments(self) -> dict[str, object]:
        return {
            "actor_principal_id": "operator",
            "job_id": self.job.job_id,
            "owner_id": self.derivation.target_owner_id,
            "admission_lease": self.admission,
            "attempt_id": "operator-binding-debug-attempt",
            "binding_id": "operator-binding-debug-binding",
            "launch_token": "operator-binding-debug-launch",
            "evidence_fingerprint": "e" * 64,
            "evaluation_spec": self.evaluation_spec,
            "portable_spec": self.portable_spec,
            "ttl_seconds": 300,
        }

    def test_projection_store_resolver_accepts_complete_file_source_mirrors(self) -> None:
        definition, evaluation_spec, candidate_input, _, _ = (
            _file_definition_and_candidate_input()
        )
        spec = compile_retained_process_attempt_runtime(
            owner_id="file-debug-job-owner",
            run_definition=definition,
            evaluation_spec=evaluation_spec,
            provider=_provider(),
            candidate_input=candidate_input,
        )
        environment_ref, candidate_ref = (
            item.snapshot_ref for item in spec.projection_spec.mappings
        )
        memberships = tuple(
            OwnerMembership(store_id, content_ref, role)
            for store_id in ("local-a", "local-b")
            for content_ref, role in (
                (environment_ref, RUN_ENVIRONMENT_SOURCE_ROLE),
                (candidate_ref, RUN_CANDIDATE_ROLE),
            )
        )

        self.assertEqual(
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships,
                available_store_ids=("local-b", "local-a"),
                require_available=True,
            ),
            "local-a",
        )
        self.assertEqual(
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships,
                available_store_ids=("local-b",),
                require_available=True,
            ),
            "local-b",
        )

    def test_projection_store_resolver_authenticates_prepared_runtime_role(self) -> None:
        definition = _definition_with_prepared_python()
        spec = compile_retained_process_attempt_runtime(
            owner_id="prepared-debug-job-owner",
            run_definition=definition,
            evaluation_spec=_evaluation_spec(definition),
            provider=_provider(),
        )
        environment_ref, prepared_ref = (
            item.snapshot_ref for item in spec.projection_spec.mappings
        )
        memberships = (
            OwnerMembership(
                "local-a", environment_ref, RUN_ENVIRONMENT_SOURCE_ROLE
            ),
            OwnerMembership(
                "local-a", prepared_ref, RUN_PREPARED_RUNTIME_ROLE
            ),
        )

        self.assertEqual(
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships,
                available_store_ids=("local-a",),
                require_available=True,
            ),
            "local-a",
        )
        with self.assertRaisesRegex(RealmConflict, "derived owner authority"):
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships[:1],
                available_store_ids=("local-a",),
                require_available=True,
            )

    def test_projection_store_resolver_rejects_inexact_file_sources(self) -> None:
        definition, evaluation_spec, candidate_input, _, _ = (
            _file_definition_and_candidate_input()
        )
        spec = compile_retained_process_attempt_runtime(
            owner_id="file-debug-job-owner",
            run_definition=definition,
            evaluation_spec=evaluation_spec,
            provider=_provider(),
            candidate_input=candidate_input,
        )
        environment_ref, candidate_ref = (
            item.snapshot_ref for item in spec.projection_spec.mappings
        )
        substituted_ref = SnapshotRef.from_manifest_bytes(b"substituted-candidate")
        exact = (
            OwnerMembership(
                "local-a", environment_ref, RUN_ENVIRONMENT_SOURCE_ROLE
            ),
            OwnerMembership("local-a", candidate_ref, RUN_CANDIDATE_ROLE),
        )
        cases = (
            ("missing", exact[:1], ("local-a",), "derived owner authority"),
            (
                "extra",
                (*exact, OwnerMembership("local-a", substituted_ref, RUN_CANDIDATE_ROLE)),
                ("local-a",),
                "derived owner authority",
            ),
            (
                "substituted-role",
                (
                    exact[0],
                    OwnerMembership(
                        "local-a", candidate_ref, RUN_ENVIRONMENT_SOURCE_ROLE
                    ),
                ),
                ("local-a",),
                "derived owner authority",
            ),
            (
                "split-stores",
                (
                    exact[0],
                    OwnerMembership("local-b", candidate_ref, RUN_CANDIDATE_ROLE),
                ),
                ("local-a", "local-b"),
                "no common authorized",
            ),
            (
                "not-local",
                tuple(
                    OwnerMembership("remote-a", item.content_ref, item.role)
                    for item in exact
                ),
                ("local-a",),
                "no common locally available",
            ),
        )
        for label, memberships, available_store_ids, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                RealmConflict, message
            ):
                _resolve_exact_projection_store(
                    spec=spec,
                    memberships=memberships,
                    available_store_ids=available_store_ids,
                    require_available=True,
                )

    def test_projection_store_resolver_authenticates_retained_attempt_input_role(
        self,
    ) -> None:
        study, manifest, package = _study_with_trial_workspace()
        definition = _definition(study=study, package=package, manifest=manifest)
        spec = compile_retained_process_attempt_runtime(
            owner_id="seeded-debug-job-owner",
            run_definition=definition,
            evaluation_spec=_evaluation_spec(definition),
            provider=_provider(),
        )
        environment_ref = spec.projection_spec.mappings[0].snapshot_ref
        memberships = (
            OwnerMembership(
                "local-a", environment_ref, RUN_ENVIRONMENT_SOURCE_ROLE
            ),
            OwnerMembership("local-a", environment_ref, RUN_ATTEMPT_INPUT_ROLE),
        )

        self.assertEqual(
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships,
                available_store_ids=("local-a",),
                require_available=True,
            ),
            "local-a",
        )
        with self.assertRaisesRegex(RealmConflict, "derived owner authority"):
            _resolve_exact_projection_store(
                spec=spec,
                memberships=memberships[:1],
                available_store_ids=("local-a",),
                require_available=True,
            )

    def test_layered_publication_is_fenced_by_current_job_authority(self) -> None:
        self.binder._refresh_initialization_authority(
            actor_principal_id="operator",
            expected_job=self.job,
            admission_lease=self.admission,
            attempt_id="operator-binding-debug-attempt",
            evaluation_spec=self.evaluation_spec,
            portable_spec=self.portable_spec,
        )

        self.cancel_unlaunched()

        with self.assertRaisesRegex(
            RealmConflict, "authority changed during provider initialization"
        ):
            self.binder._refresh_initialization_authority(
                actor_principal_id="operator",
                expected_job=self.job,
                admission_lease=self.admission,
                attempt_id="operator-binding-debug-attempt",
                evaluation_spec=self.evaluation_spec,
                portable_spec=self.portable_spec,
            )

    def projection_records(self):
        records = self.ledger.list_projection_realizations(
            actor_principal_id=self.projection_service.maintenance_principal_id,
            projection_root_id=self.projection_service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        return tuple(
            item
            for item in records
            if item.owner_id == self.derivation.target_owner_id
        )

    def volume_records(self):
        return self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.volume_service.maintenance_principal_id,
            volume_root_id=self.volume_service.root_binding.volume_root_id,
            states=tuple(EphemeralVolumeState),
        )

    def terminal_proof(self, **changes: object) -> WorkerTerminalProof:
        values = {
            "launch_token": "operator-binding-debug-launch",
            "binding_id": "operator-binding-debug-binding",
            "evidence_fingerprint": "e" * 64,
            "backend_token": "f" * 64,
            "launch_request_digest": "1" * 64,
            "disposition": "exited",
            "provider_generation": 1,
            "terminal_at": 1.0,
        }
        values.update(changes)
        return WorkerTerminalProof(**values)

    def cancel_unlaunched(self):
        return self.ledger.request_operator_job_stop(
            operation_id=self.op("job/cancel"),
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            reason_code="test_cancelled",
        )

    def cleanup_arguments(self, *, admission=True, authority=None):
        arguments = self.binding_arguments()
        arguments.pop("admission_lease")
        return {
            **arguments,
            "admission_lease": self.admission if admission else None,
            "operator_plan_digest": self.job.plan_digest,
            "authority": (
                ProcessLaunchSealReceipt(
                    launch_token="operator-binding-debug-launch",
                    binding_id="operator-binding-debug-binding",
                    prior_state="absent",
                )
                if authority is None
                else authority
            ),
        }

    def test_realize_uses_exact_job_owned_projection_and_admission_volumes(self) -> None:
        binding = self.binder.realize(**self.binding_arguments())
        binding.validate()
        self.assertEqual(
            self.portable_spec.projection_spec.owner_id,
            self.derivation.target_owner_id,
        )
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=self.derivation.target_owner_id,
                permission=OwnerPermission.DERIVE,
            ),
            (self.environment_membership,),
        )
        self.assertEqual(set(binding.scope_paths), {"environment-source", "trial", "control"})
        self.assertTrue(
            binding.scope_paths["environment-source"]
            .resolve()
            .is_relative_to(self.projection_root.resolve())
        )
        self.assertTrue(
            binding.scope_paths["trial"]
            .resolve()
            .is_relative_to(self.volume_root.resolve())
        )
        self.assertTrue(
            binding.scope_paths["control"]
            .resolve()
            .is_relative_to(self.volume_root.resolve())
        )

        projections = self.projection_records()
        volumes = self.volume_records()
        self.assertEqual(len(projections), 1)
        self.assertEqual(len(volumes), len(self.portable_spec.writable_volumes))
        self.assertEqual(projections[0].owner_id, self.derivation.target_owner_id)
        self.assertEqual(projections[0].store_id, self.environment_membership.store_id)
        self.assertEqual(projections[0].spec_digest, self.portable_spec.projection_spec.digest)
        self.assertEqual(
            projections[0].availability_resolution["realization_sharing"]["policy"],
            "private",
        )
        self.assertTrue(
            all(item.owner_id == self.derivation.target_owner_id for item in volumes)
        )
        self.assertTrue(
            all(item.parent_lease_id == self.admission.lease_id for item in volumes)
        )

        durable = json.dumps(
            {
                "admission": self.admission.to_dict(),
                "derivation": self.derivation.to_dict(),
                "job": self.job.to_dict(),
                "projection": projections[0].to_dict(),
                "volumes": [item.to_dict() for item in volumes],
            },
            sort_keys=True,
        )
        self.assertNotIn(str(self.root), durable)
        self.assertNotIn(str(self.projection_root), durable)
        self.assertNotIn(str(self.volume_root), durable)

    def test_recover_existing_never_creates_missing_and_reuses_exact_resources(self) -> None:
        with mock.patch.object(
            self.projection_service,
            "project_read_only",
            wraps=self.projection_service.project_read_only,
        ) as create_projection:
            with self.assertRaises(RealmNotFound):
                self.binder.recover_existing(**self.binding_arguments())
            create_projection.assert_not_called()
        self.assertEqual(self.projection_records(), ())
        self.assertEqual(self.volume_records(), ())

        first = self.binder.realize(**self.binding_arguments())
        projection_ids = tuple(item.realization_id for item in self.projection_records())
        volume_ids = tuple(item.volume_id for item in self.volume_records())
        restarted_projection = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.projection_root,
        )
        restarted_volume = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.volume_root
        )
        restarted_binder = RealmOperatorAttemptBinder(
            self.ledger, restarted_projection, restarted_volume
        )
        with mock.patch.object(
            restarted_volume,
            "recover_existing",
            side_effect=RealmNotFound("injected missing volume"),
        ), mock.patch.object(
            restarted_volume,
            "create",
            wraps=restarted_volume.create,
        ) as create_volume:
            with self.assertRaises(RealmNotFound):
                restarted_binder.recover_existing(**self.binding_arguments())
            create_volume.assert_not_called()

        recovered = restarted_binder.recover_existing(**self.binding_arguments())
        self.assertEqual(recovered.scope_paths, first.scope_paths)
        self.assertEqual(
            tuple(item.realization_id for item in self.projection_records()),
            projection_ids,
        )
        self.assertEqual(
            tuple(item.volume_id for item in self.volume_records()), volume_ids
        )

    def test_mismatched_job_owner_lease_and_portable_spec_are_rejected(self) -> None:
        base = self.binding_arguments()
        cases = (
            ("job", {**base, "job_id": "missing-operator-job"}, RealmNotFound),
            (
                "owner",
                {**base, "owner_id": "different-job-owner"},
                RealmConflict,
            ),
            (
                "lease",
                {
                    **base,
                    "admission_lease": replace(
                        self.admission,
                        metadata={
                            "job_id": self.job.job_id,
                            "plan_digest": "0" * 64,
                        },
                    ),
                },
                RealmConflict,
            ),
            (
                "portable-spec",
                {
                    **base,
                    "portable_spec": replace(
                        self.portable_spec,
                        timeout_seconds=(self.portable_spec.timeout_seconds or 30) + 1,
                    ),
                },
                RealmConflict,
            ),
        )
        for label, arguments, error_type in cases:
            with self.subTest(label=label), self.assertRaises(error_type):
                self.binder.realize(**arguments)
        self.assertEqual(self.projection_records(), ())
        self.assertEqual(self.volume_records(), ())

    def test_worker_terminal_proof_releases_resources_only_for_exact_binding(self) -> None:
        binding = self.binder.realize(**self.binding_arguments())
        with self.assertRaises(RealmConflict):
            binding.release_after_execution_terminalized(
                self.terminal_proof(binding_id="different-debug-binding")
            )
        self.assertFalse(binding.released)
        binding.validate()

        binding.release_after_execution_terminalized(self.terminal_proof())
        self.assertTrue(binding.released)
        with self.assertRaises(RealmConflict):
            binding.validate()
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self.projection_records()
            )
        )
        self.assertTrue(
            all(item.state is EphemeralVolumeState.CLEANED for item in self.volume_records())
        )

    def test_negative_launch_seal_releases_only_when_no_launch_exists(self) -> None:
        binding = self.binder.realize(**self.binding_arguments())
        existing = ProcessLaunchSealReceipt(
            launch_token="operator-binding-debug-launch",
            binding_id="operator-binding-debug-binding",
            prior_state="existing",
        )
        with self.assertRaises(RealmConflict):
            binding.release_after_execution_terminalized(existing)
        self.assertFalse(binding.released)

        sealed = ProcessLaunchSealReceipt(
            launch_token="operator-binding-debug-launch",
            binding_id="operator-binding-debug-binding",
            prior_state="absent",
        )
        binding.release_after_execution_terminalized(sealed)
        self.assertTrue(binding.released)
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self.projection_records()
            )
        )
        self.assertTrue(
            all(item.state is EphemeralVolumeState.CLEANED for item in self.volume_records())
        )

    def test_cleanup_proves_absent_resources_and_seal_replay_is_stable(self) -> None:
        cancelled = self.cancel_unlaunched()
        self.assertIsNone(cancelled.launch_intent)

        first = self.binder.cleanup_after_terminal(**self.cleanup_arguments())
        replay = self.binder.cleanup_after_terminal(
            **self.cleanup_arguments(
                authority=ProcessLaunchSealReceipt(
                    launch_token="operator-binding-debug-launch",
                    binding_id="operator-binding-debug-binding",
                    prior_state="sealed",
                )
            )
        )

        self.assertEqual(first, replay)
        self.assertEqual(self.projection_records(), ())
        self.assertEqual(self.volume_records(), ())
        self.assertEqual(
            len(first.volume_cleanup_digests),
            len(self.portable_spec.writable_volumes),
        )
        encoded = json.dumps(first.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), encoded)

    def test_cleanup_adopts_matching_cleaned_projection_after_validation_race(
        self,
    ) -> None:
        self.binder.realize(**self.binding_arguments())
        ready = self.projection_records()[0]
        self.cancel_unlaunched()
        first = self.binder.cleanup_after_terminal(**self.cleanup_arguments())

        operation_id = _resource_operation_id(
            job_id=self.job.job_id,
            binding_id="operator-binding-debug-binding",
            resource_kind="projection",
            logical_name=self.portable_spec.projection_name,
        )
        list_realizations = self.ledger.list_projection_realizations
        list_calls = 0

        def stale_then_current(**kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                return (ready,)
            return list_realizations(**kwargs)

        with mock.patch.object(
            self.ledger,
            "list_projection_realizations",
            side_effect=stale_then_current,
        ), mock.patch.object(
            self.ledger,
            "validate_private_projection_operation",
            side_effect=RealmNotFound("concurrent cleanup released operation authority"),
        ):
            replay_digest = self.binder._cleanup_projection_operation(
                actor_principal_id="operator",
                operation_id=operation_id,
                owner_id=self.derivation.target_owner_id,
                store_id=self.environment_membership.store_id,
                spec=self.portable_spec.projection_spec,
                holder_id=_resource_holder_id(
                    job_id=self.job.job_id,
                    binding_id="operator-binding-debug-binding",
                ),
                ttl_seconds=300.0,
                metadata=_projection_metadata(
                    job_id=self.job.job_id,
                    binding_id="operator-binding-debug-binding",
                    logical_name=self.portable_spec.projection_name,
                ),
                allow_absent=True,
            )

        self.assertEqual(replay_digest, first.projection_cleanup_digest)

    def test_cleanup_adopts_exact_closing_projection_after_validation_race(
        self,
    ) -> None:
        self.binder.realize(**self.binding_arguments())
        self.cancel_unlaunched()
        operation_id = _resource_operation_id(
            job_id=self.job.job_id,
            binding_id="operator-binding-debug-binding",
            resource_kind="projection",
            logical_name=self.portable_spec.projection_name,
        )

        def retire_then_report_absent(**kwargs):
            closing = self.ledger.retire_private_projection_operation(
                operation_id=self.op("projection/concurrent-retire"),
                **kwargs,
            )
            self.assertEqual(
                closing.state, ProjectionRealizationState.CLOSING
            )
            raise RealmNotFound(
                "concurrent cleanup retired operation authority"
            )

        with mock.patch.object(
            self.ledger,
            "validate_private_projection_operation",
            side_effect=retire_then_report_absent,
        ):
            evidence = self.binder.cleanup_after_terminal(
                **self.cleanup_arguments()
            )

        self.assertTrue(evidence.projection_cleanup_digest)
        self.assertEqual(
            self.projection_records()[0].state,
            ProjectionRealizationState.CLEANED,
        )
        self.assertFalse(
            (
                self.projection_root
                / self.projection_records()[0].relative_name
            ).exists()
        )

    def test_cleanup_missing_validation_still_rejects_live_projection(
        self,
    ) -> None:
        self.binder.realize(**self.binding_arguments())
        ready = self.projection_records()[0]
        self.cancel_unlaunched()
        operation_id = _resource_operation_id(
            job_id=self.job.job_id,
            binding_id="operator-binding-debug-binding",
            resource_kind="projection",
            logical_name=self.portable_spec.projection_name,
        )

        with mock.patch.object(
            self.ledger,
            "validate_private_projection_operation",
            side_effect=RealmNotFound("injected missing validation"),
        ), self.assertRaisesRegex(RealmNotFound, "injected missing validation"):
            self.binder._cleanup_projection_operation(
                actor_principal_id="operator",
                operation_id=operation_id,
                owner_id=self.derivation.target_owner_id,
                store_id=self.environment_membership.store_id,
                spec=self.portable_spec.projection_spec,
                holder_id=_resource_holder_id(
                    job_id=self.job.job_id,
                    binding_id="operator-binding-debug-binding",
                ),
                ttl_seconds=300.0,
                metadata=_projection_metadata(
                    job_id=self.job.job_id,
                    binding_id="operator-binding-debug-binding",
                    logical_name=self.portable_spec.projection_name,
                ),
                allow_absent=True,
            )

        current = self.projection_records()[0]
        self.assertEqual(current.state, ProjectionRealizationState.READY)
        self.assertEqual(current.realization_id, ready.realization_id)
        self.assertTrue(
            (self.projection_root / current.relative_name).is_dir()
        )

    def test_cleanup_reconciles_partial_realization_independently(self) -> None:
        original_create = self.volume_service.create
        created = 0

        def fail_second_create(**kwargs):
            nonlocal created
            created += 1
            if created == 2:
                raise RuntimeError("injected second volume realization crash")
            return original_create(**kwargs)

        with mock.patch.object(
            self.volume_service, "create", side_effect=fail_second_create
        ):
            with self.assertRaisesRegex(RuntimeError, "second volume"):
                self.binder.realize(**self.binding_arguments())
        self.assertEqual(len(self.projection_records()), 1)
        self.assertEqual(len(self.volume_records()), 1)
        self.cancel_unlaunched()

        evidence = self.binder.cleanup_after_terminal(**self.cleanup_arguments())

        self.assertEqual(
            len(evidence.volume_cleanup_digests),
            len(self.portable_spec.writable_volumes),
        )
        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self.projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.CLEANED
                for item in self.volume_records()
            )
        )

    def test_cleanup_retires_live_creating_projection_without_consumer(self) -> None:
        create = self.ledger.create_projection_realization
        receipts = []

        def capture_create(**kwargs):
            receipt = create(**kwargs)
            receipts.append(receipt)
            return receipt

        with mock.patch.object(
            self.ledger,
            "create_projection_realization",
            side_effect=capture_create,
        ), mock.patch.object(
            self.ledger,
            "claim_projection_materialization",
            side_effect=RuntimeError("crash before materialization claim"),
        ):
            with self.assertRaisesRegex(RuntimeError, "materialization claim"):
                self.binder.realize(**self.binding_arguments())

        self.assertEqual(len(receipts), 1)
        records = self.projection_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, ProjectionRealizationState.CREATING)
        self.assertEqual(
            self.ledger.list_projection_consumers(
                actor_principal_id="operator",
                realization_id=records[0].realization_id,
            ),
            (),
        )
        owner = receipts[0].owner_lease
        self.assertEqual(
            self.ledger.validate_lease(
                actor_principal_id="operator",
                lease_id=owner.lease_id,
                holder_id=owner.holder_id,
                fencing_token=owner.fencing_token,
            ),
            owner,
        )
        self.cancel_unlaunched()

        first = self.binder.cleanup_after_terminal(**self.cleanup_arguments())
        replay = self.binder.cleanup_after_terminal(
            **self.cleanup_arguments(
                authority=ProcessLaunchSealReceipt(
                    launch_token="operator-binding-debug-launch",
                    binding_id="operator-binding-debug-binding",
                    prior_state="sealed",
                )
            )
        )

        self.assertEqual(first, replay)
        self.assertEqual(
            self.projection_records()[0].state,
            ProjectionRealizationState.CLEANED,
        )
        self.assertFalse(
            (self.projection_root / records[0].relative_name).exists()
        )

    def test_cleanup_retires_live_ready_projection_before_consumer(self) -> None:
        create = self.ledger.create_projection_realization
        receipts = []

        def capture_create(**kwargs):
            receipt = create(**kwargs)
            receipts.append(receipt)
            return receipt

        with mock.patch.object(
            self.ledger,
            "create_projection_realization",
            side_effect=capture_create,
        ), mock.patch.object(
            self.ledger,
            "acquire_projection_consumer",
            side_effect=RuntimeError("crash before consumer acquisition"),
        ):
            with self.assertRaisesRegex(RuntimeError, "consumer acquisition"):
                self.binder.realize(**self.binding_arguments())

        self.assertEqual(len(receipts), 1)
        records = self.projection_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, ProjectionRealizationState.READY)
        self.assertEqual(
            self.ledger.list_projection_consumers(
                actor_principal_id="operator",
                realization_id=records[0].realization_id,
            ),
            (),
        )
        owner = receipts[0].owner_lease
        current_owner = self.ledger.validate_lease(
            actor_principal_id="operator",
            lease_id=owner.lease_id,
            holder_id=owner.holder_id,
            fencing_token=owner.fencing_token,
        )
        self.assertEqual(
            (
                current_owner.lease_id,
                current_owner.owner_id,
                current_owner.parent_lease_id,
                current_owner.lease_kind,
                current_owner.audience,
                current_owner.holder_id,
                current_owner.scope_key,
                current_owner.fencing_token,
                current_owner.state,
                current_owner.created_at,
                current_owner.metadata,
            ),
            (
                owner.lease_id,
                owner.owner_id,
                owner.parent_lease_id,
                owner.lease_kind,
                owner.audience,
                owner.holder_id,
                owner.scope_key,
                owner.fencing_token,
                owner.state,
                owner.created_at,
                owner.metadata,
            ),
        )
        self.assertGreater(current_owner.heartbeat_revision, owner.heartbeat_revision)
        self.assertGreater(current_owner.expires_at, owner.expires_at)
        wrapper = self.projection_root / records[0].relative_name
        self.assertTrue(wrapper.exists())
        self.cancel_unlaunched()

        first = self.binder.cleanup_after_terminal(**self.cleanup_arguments())
        replay = self.binder.cleanup_after_terminal(
            **self.cleanup_arguments(
                authority=ProcessLaunchSealReceipt(
                    launch_token="operator-binding-debug-launch",
                    binding_id="operator-binding-debug-binding",
                    prior_state="sealed",
                )
            )
        )

        self.assertEqual(first, replay)
        self.assertEqual(
            self.projection_records()[0].state,
            ProjectionRealizationState.CLEANED,
        )
        self.assertFalse(wrapper.exists())

    def test_cleanup_reconciles_after_admission_expiry_without_live_binding(self) -> None:
        self.binder.realize(**self.binding_arguments())
        with sqlite3.connect(self.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (self.admission.lease_id,),
            )
        with self.assertRaises(RealmExpired):
            self.ledger.validate_lease(
                actor_principal_id="operator",
                lease_id=self.admission.lease_id,
                holder_id=self.admission.holder_id,
                fencing_token=self.admission.fencing_token,
            )
        self.cancel_unlaunched()

        self.binder.cleanup_after_terminal(
            **self.cleanup_arguments(admission=False)
        )

        self.assertTrue(
            all(
                item.state is ProjectionRealizationState.CLEANED
                for item in self.projection_records()
            )
        )
        self.assertTrue(
            all(
                item.state is EphemeralVolumeState.CLEANED
                for item in self.volume_records()
            )
        )

    def test_managed_binding_detaches_only_after_exact_cleanup_evidence(self) -> None:
        binding = self.binder.realize(**self.binding_arguments())
        self.cancel_unlaunched()
        evidence = self.binder.cleanup_after_terminal(**self.cleanup_arguments())

        with self.assertRaises(RealmConflict):
            binding.detach_after_terminal_cleanup(
                replace(evidence, binding_id="different-debug-binding")
            )
        self.assertFalse(binding.released)

        binding.detach_after_terminal_cleanup(evidence)

        self.assertTrue(binding.released)
        with self.assertRaises(RealmConflict):
            binding.validate()
        binding.detach_after_terminal_cleanup(evidence)


def _logical_id(prefix: str, payload: dict[str, object]) -> str:
    digest = request_digest(
        {"payload": payload, "schema": f"optpilot.{prefix}.v1"}
    )
    return f"{prefix}-{digest[:40]}"


if __name__ == "__main__":
    unittest.main()
