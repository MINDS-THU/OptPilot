"""Focused Studio checks for canonical, path-free Realm run APIs."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import yaml

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.errors import (
    RealmCapacityUnavailable,
    RealmConflict,
    RealmIntegrityError,
)
from optpilot.realm.environment_preview_binding import RealmEnvironmentPreviewBinder
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
)
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.operator_job_records import OperatorJobState
from optpilot.realm.operator_job_service import (
    EnvironmentPreviewFinalCapturePending,
    RealmOperatorJobService,
)
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_views import RealmRunViewService, RunViewRef
from optpilot.realm.selection_content_service import (
    SelectionByteRead,
    SelectionContentSummary,
    SelectionTreeEntry,
    SelectionTreePage,
)
from optpilot.realm.selections import SelectionEligibility
from optpilot.realm.shortlist_service import ShortlistDraft
from optpilot.retained_file_candidates import sealed_file_candidate_spec
from optpilot.realm.workspaces import (
    WORKSPACE_REVISION_ROLE,
    WorkspaceLineage,
    WorkspaceState,
)
from optpilot.realm_study_runner import local_study_run_id_for_operation
from optpilot.realm_run_execution_service import RUN_EXECUTION_MODE_EXACT_PLAN
from optpilot.study_launch_service import (
    METHOD_ENVIRONMENT_BINDING_SCHEMA,
    _plan_context,
)
from optpilot_studio.ui.server import (
    UiState,
    _agent_context_packet,
    _agent_session_by_id,
    _candidate_debug_runtime_capability,
    _catalog_payload,
    _configured_study_package_root,
    _canonical_study_launch_request,
    _create_agent_session,
    _execute_run_workbench_action,
    _execute_agent_tool,
    _handler_factory,
    _mint_assistant_run_selection,
    _prepare_assistant_smoke_package,
    _realm_compat_run_row,
    _realm_run_detail,
    _realm_runs_payload,
    _reconcile_visible_run_executions,
    _row_workbench_action_capabilities,
    _reconcile_visible_operator_jobs,
    _run_temporary_realm_smoke,
    _schedule_operator_job_execution,
    _schedule_run_execution,
    _schedule_study_launch_execution,
    run_ui,
)
from optpilot_studio.ui.runtime_supervisor import (
    StudioRuntimeSupervisorBusy,
    StudioRuntimeSupervisorClaim,
)
from optpilot_studio.ui.coordination_store import (
    studio_project_state_directory,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)
from tests.test_local_container_web_provider import _FakeContainerEngine
from tests.test_retained_study_service import _write_package


_STUDIO_PREVIEW_IMAGE = "example/studio-viewer@sha256:" + "d" * 64
_STUDIO_PREVIEW_INTERFACE = f"""\
interface:
  label: Candidate Preview
  description: Inspect one exact candidate.
  command: [python, -m, local_package.viewer]
  cwd: .
  env: {{}}
  runtime:
    sandbox: container
    container:
      engine: docker
      image: {_STUDIO_PREVIEW_IMAGE}
      platform: linux/amd64
  grants:
    network: disabled
    secretsFromHost: []
  resources:
    cpu: 1
    memoryMiB: 512
    gpus: 0
  timeoutSeconds: 120
  presentation:
    kind: web
    port: 5173
    readyPath: /ready
    readyTimeoutSeconds: 10
  accepts:
    selectionKinds: [candidate]
    mediaTypes: [application/vnd.optpilot.candidate+json]
"""
_STUDIO_OUTPUT_PREVIEW_INTERFACE = _STUDIO_PREVIEW_INTERFACE.replace(
    "  grants:\n",
    "  outputs: true\n  grants:\n",
)


class StudioRealmRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.realm_root = self.root / "private-realm"
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.addCleanup(self.runtime.close)

        self.package = self.root / "catalog" / "package-a"
        studies = self.package / "studies"
        studies.mkdir(parents=True)
        self.study_path = studies / "study.yaml"
        self.study_path.write_text(
            "apiVersion: optpilot.io/v1\nconfig: study\nid: studio-test\n",
            encoding="utf-8",
        )
        self.operation_id = "studio-test/launch-one"
        self.run_id = local_study_run_id_for_operation(self.operation_id)
        self._create_run()
        self.state = UiState(
            cwd=self.root,
            catalog_roots=[self.package],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(self.state.close_coordination)

    def _create_run(self) -> None:
        closure, bindings, source_owner_id, source_owner_revision = (
            prepare_test_run_closure(
                ledger=self.runtime.ledger,
                store=self.runtime.content_store,
                root=self.root,
                actor_principal_id=self.runtime.actor_principal_id,
                prefix="studio-realm",
            )
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=10)
        definition, definition_bindings = prepare_test_run_definition(
            closure,
            manifest,
            bindings,
        )
        self.created = self.runtime.ledger.create_run_namespace(
            operation_id="studio-realm/run/create",
            actor_principal_id=self.runtime.actor_principal_id,
            controller_holder_id="studio-controller",
            controller_ttl_seconds=120,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id=self.run_id,
            owner_id="studio-run-owner",
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-realm/run/admission/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=120,
        )
        candidates = tuple(
            CandidateAdmission(
                candidate_id=f"candidate-{index}",
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec={"x": index},
                ),
                lineage={"parents": []},
                generator={"method_id": "test-method"},
            )
            for index in range(3)
        )
        trials = tuple(
            LogicalTrialAdmission(
                logical_trial_id=f"trial-{index}",
                candidate_id=f"candidate-{index}",
            )
            for index in range(3)
        )
        lease = self.created.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id="studio-realm/run/admission/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(candidates, trials),
        )

    def _complete_default_trial(
        self,
        *,
        index: int = 0,
        metric: float = 1.25,
    ) -> None:
        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        owner = self.runtime.ledger.read_owner(
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
        )
        lease = snapshot.controller_lease
        prepared = self.runtime.ledger.prepare_run_attempt(
            operation_id=f"studio-realm/run/attempt-{index}/prepare",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            logical_trial_id=f"trial-{index}",
            attempt_id=f"attempt-{index}",
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
        )
        envelope = AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {}, "metadata": {}},
            metric_values={"score": metric},
            constraint_results={},
            output_declarations=(),
            event_summary={},
            execution_metadata={"worker": "studio-test"},
            error={},
        )
        self.runtime.ledger.adopt_run_attempt(
            operation_id=f"studio-realm/run/attempt-{index}/adopt",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            attempt_id=prepared.attempt.attempt_id,
            change_id=prepared.attempt.capture_change_id,
            finalization=AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome="success",
                effective_code=None,
                captured_artifacts=(),
                envelope=envelope,
            ),
            expected_run_revision=prepared.revision.revision,
            expected_owner_revision=owner.revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
        )

    def _create_runnable_operator_run(self, *, environment_interface: str = "") -> str:
        package_root = self.root / "runnable-operator-package"
        package_root.mkdir()
        study_path = _write_package(package_root)
        if environment_interface:
            environment_path = (
                package_root / "configs" / "environments" / "environment.yaml"
            )
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8")
                + "\n"
                + environment_interface,
                encoding="utf-8",
            )
        preparation = self.runtime.retained_study_service.prepare_local_package(
            operation_id="studio-operator/prepare",
            actor_principal_id=self.runtime.actor_principal_id,
            store_id=self.runtime.content_store.store_id,
            package_root=package_root,
            study_config_path=study_path,
            source_owner_id="studio-operator-source",
            study_definition_owner_id="studio-operator-definition",
        )
        run_id = "studio-operator-run"
        created = self.runtime.retained_study_service.launch_definition_run(
            operation_id="studio-operator/launch",
            actor_principal_id=self.runtime.actor_principal_id,
            controller_holder_id="studio-operator-controller",
            controller_ttl_seconds=300,
            preparation=preparation,
            run_id=run_id,
            owner_id="studio-operator-run-owner",
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-operator/admission/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=300,
        )
        lease = created.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id="studio-operator/admission/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        candidate_id="candidate-debug",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="parameters",
                            spec={"x": 0.5},
                        ),
                        lineage={"parents": []},
                        generator={"method_id": "test-method"},
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        logical_trial_id="trial-debug",
                        candidate_id="candidate-debug",
                    ),
                ),
            ),
        )
        return run_id

    def _create_file_candidate_run(self, *, sealed_specs: bool = False) -> str:
        prefix = "studio-file-capability"
        candidate_contract = (
            {
                "format": "files",
                "validation": {
                    "implementation": "builtin.workspace_policy",
                    "config": {"requiredFiles": ["run.py"]},
                },
                "materialization": {
                    "implementation": "builtin.workspace_bundle",
                    "config": {"entrypoint": "run.py"},
                },
            }
            if sealed_specs
            else {"format": "files"}
        )
        closure, bindings, source_owner_id, source_owner_revision = (
            prepare_test_run_closure(
                ledger=self.runtime.ledger,
                store=self.runtime.content_store,
                root=self.root,
                actor_principal_id=self.runtime.actor_principal_id,
                prefix=prefix,
                candidate_contract=candidate_contract,
            )
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=4)
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        run_id = "studio-file-capability-run"
        created = self.runtime.ledger.create_run_namespace(
            operation_id=f"{prefix}/run/create",
            actor_principal_id=self.runtime.actor_principal_id,
            controller_holder_id=f"{prefix}-controller",
            controller_ttl_seconds=120,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id=run_id,
            owner_id=f"{prefix}-run-owner",
        )

        candidate_source_owner = f"{prefix}-candidate-source-owner"
        self.runtime.ledger.create_owner(
            operation_id=f"{prefix}/candidate/source-owner",
            owner_id=candidate_source_owner,
            owner_kind="workspace",
            principal_id=self.runtime.actor_principal_id,
        )
        sources = {}
        for suffix, text in (
            ("a", "print('baseline')\n"),
            ("b", "print('comparison')\nprint('extra')\n"),
        ):
            source = self.root / f"{prefix}-candidate-source-{suffix}"
            source.mkdir()
            (source / "run.py").write_text(text, encoding="utf-8")
            sources[suffix] = source
        source_change = self.runtime.ledger.begin_owner_change(
            operation_id=f"{prefix}/candidate/source-begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=candidate_source_owner,
            expected_owner_revision=0,
            ttl_seconds=120,
        )
        capture = self.runtime.content_store.capture(
            change_id=source_change.change_id,
            authority=self.runtime.ledger.content_capture_handle(
                actor_principal_id=self.runtime.actor_principal_id,
                change_id=source_change.change_id,
                store_id=self.runtime.content_store.store_id,
            ),
        )
        sealed = {
            suffix: capture.seal_tree(source=AllowedTreeSource(source))
            for suffix, source in sources.items()
        }
        source_memberships = tuple(
            OwnerMembership(
                self.runtime.content_store.store_id,
                value.snapshot_ref,
                "candidate-source",
            )
            for value in sealed.values()
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=f"{prefix}/candidate/source-hold",
            actor_principal_id=self.runtime.actor_principal_id,
            change_id=source_change.change_id,
            memberships=source_memberships,
        )
        self.runtime.ledger.commit_owner_change(
            operation_id=f"{prefix}/candidate/source-commit",
            actor_principal_id=self.runtime.actor_principal_id,
            change_id=source_change.change_id,
            expected_owner_revision=0,
            additions=source_memberships,
        )

        candidate_bindings = tuple(
            OwnerMembership(
                self.runtime.content_store.store_id,
                sealed[suffix].snapshot_ref,
                RUN_CANDIDATE_ROLE,
            )
            for suffix in ("a", "b")
        )
        run_change = self.runtime.ledger.begin_owner_change(
            operation_id=f"{prefix}/admission/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=120,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=f"{prefix}/admission/hold",
            actor_principal_id=self.runtime.actor_principal_id,
            change_id=run_change.change_id,
            memberships=candidate_bindings,
            source_owner_id=candidate_source_owner,
        )
        lease = created.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id=f"{prefix}/admission/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=run_change.change_id,
            plan=RunAdmissionPlan(
                tuple(
                    CandidateAdmission(
                        candidate_id=f"candidate-files-{suffix}",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="files",
                            spec=(
                                sealed_file_candidate_spec(
                                    sealed[suffix].manifest,
                                    candidate_contract,
                                )
                                if sealed_specs
                                else {"entrypoint": "run.py", "variant": suffix}
                            ),
                            content_refs=(sealed[suffix].snapshot_ref,),
                        ),
                    )
                    for suffix in ("a", "b")
                ),
                tuple(
                    LogicalTrialAdmission(
                        logical_trial_id=f"trial-files-{suffix}",
                        candidate_id=f"candidate-files-{suffix}",
                    )
                    for suffix in ("a", "b")
                ),
            ),
            content_bindings=candidate_bindings,
        )
        return run_id

    def _enable_fake_environment_preview(
        self,
        *,
        trusted_images: tuple[str, ...] = (_STUDIO_PREVIEW_IMAGE,),
    ) -> _FakeContainerEngine:
        authority = object()
        engine = _FakeContainerEngine()
        provider = LocalContainerWebProvider(
            executable="docker",
            control_root=self.runtime.root / "studio-preview-provider",
            broker_authority=authority,
            trusted_gateway_images=tuple(
                ContainerGatewayImageTrust(image_ref)
                for image_ref in trusted_images
            ),
            run_command=engine,
            gateway_probe=lambda _routes, _token, _primary, _path, _timeout: True,
        )
        binder = RealmEnvironmentPreviewBinder(
            self.runtime.ledger,
            self.runtime.projection_service,
            self.runtime.volume_service,
            provider,
        )
        service = RealmOperatorJobService(
            self.runtime.ledger,
            self.runtime.principal,
            self.runtime.inspection_targets,
            self.runtime.process_provider,
            self.runtime.operator_attempt_binder,
            self.runtime.attempt_launcher,
            self.runtime.attempt_finalizer,
            interface_output_service=self.runtime.interface_outputs,
            environment_preview_binder=binder,
            container_web_provider=provider,
            container_web_broker_authority=authority,
        )
        self.runtime.environment_preview_binder = binder
        self.runtime.container_web_provider = provider
        self.runtime.container_web_broker_authority = authority
        self.runtime.operator_jobs = service
        return engine

    def _handler(self):
        handler = object.__new__(_handler_factory(self.state))
        responses = []
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append(  # type: ignore[method-assign]
            (payload, status)
        )
        return handler, responses

    def _await_study_launch_request(
        self,
        handler,
        responses,
        request_id: str,
        *,
        terminal_states: set[str],
    ):
        deadline = time.monotonic() + 10
        while True:
            handler.path = f"/api/studies/launch-requests/{request_id}"
            handler.do_GET()
            payload, status = responses[-1]
            self.assertEqual(status, HTTPStatus.OK, payload)
            if payload.get("state") in terminal_states:
                return payload
            if time.monotonic() >= deadline:
                self.fail(
                    "Study launch preparation did not reach "
                    f"{sorted(terminal_states)!r}: {payload!r}"
                )
            time.sleep(0.01)

    def _create_managed_workspace(self):
        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        source_owner = self.runtime.ledger.read_owner(
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
        )
        source_membership = next(
            item
            for item in self.runtime.ledger.list_owner_memberships(
                actor_principal_id=self.runtime.actor_principal_id,
                owner_id=snapshot.run.owner_id,
            )
            if isinstance(item.content_ref, SnapshotRef)
        )
        return self.runtime.ledger.create_workspace_from_snapshot(
            operation_id="studio-realm/workspace/create",
            actor_principal_id=self.runtime.actor_principal_id,
            source_owner_id=snapshot.run.owner_id,
            expected_source_owner_revision=source_owner.revision,
            title="Retained candidate workspace",
            root=OwnerMembership(
                source_membership.store_id,
                source_membership.content_ref,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id=snapshot.run.owner_id,
                source_id=snapshot.run.owner_id,
                source_revision=source_owner.revision,
                source_store_id=source_membership.store_id,
                source_ref=source_membership.content_ref,
            ),
            workspace_id="studio-managed-workspace",
            owner_id="studio-managed-workspace-owner",
        )

    def _realm_counts(self) -> dict[str, int]:
        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "managed_workspaces",
                    "workspace_revisions",
                    "content_objects",
                    "owner_memberships",
                    "leases",
                )
            }
        finally:
            connection.close()

    def _seal_default_run(self):
        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        lease = snapshot.controller_lease
        revision = snapshot.revision.revision
        for index, trial in enumerate(snapshot.logical_trials, start=1):
            cancelled = self.runtime.ledger.cancel_run_logical_trial(
                operation_id=f"studio-realm/run/seal/cancel-{index}",
                actor_principal_id=self.runtime.actor_principal_id,
                run_id=self.run_id,
                logical_trial_id=trial.admission.logical_trial_id,
                expected_run_revision=revision,
                controller_lease_id=lease.lease_id,
                controller_holder_id=lease.holder_id,
                controller_fencing_token=lease.fencing_token,
                code="admin_cancelled",
            )
            revision = cancelled.run.current_revision
        draining = self.runtime.ledger.close_run_submissions(
            operation_id="studio-realm/run/seal/close",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            stop_code="admin_cancelled",
        )
        return self.runtime.ledger.finish_run(
            operation_id="studio-realm/run/seal/finish",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )

    def test_launch_derives_exact_package_and_path_free_deterministic_job(self) -> None:
        study_path = _write_package(self.package)
        operation_id = "studio-test/launch-durable-core"
        run_id = local_study_run_id_for_operation(operation_id)
        with (
            mock.patch(
                "optpilot_studio.ui.server.subprocess.Popen",
                side_effect=AssertionError("Studio must not spawn a study process."),
            ) as popen,
            mock.patch(
                "optpilot_studio.ui.server._validate_study",
                return_value={
                    "valid": True,
                    "launch": {
                        "eligible": True,
                        "code": "ready",
                        "reason": None,
                    },
                },
            ),
            mock.patch(
                "optpilot_studio.ui.server._schedule_study_launch_execution",
                return_value=True,
            ) as schedule,
        ):
            job = self.state.launch_study(
                study_path,
                study_name="Studio test",
                environment_id="test-environment",
                operation_id=operation_id,
            )

        popen.assert_not_called()
        schedule.assert_called_once_with(self.state, launch_id=job.launch_id)
        self.assertIsNone(job.run_id)
        self.assertEqual(
            job.job.plan.input_facts["run"]["run_id"],
            run_id,
        )
        self.assertEqual(
            job.job.plan.input_facts["execution_profile"][
                "method_request_timeout_seconds"
            ],
            10.0,
        )

        public = job.to_dict()
        serialized = json.dumps(public, sort_keys=True)
        self.assertEqual(public["job_id"], job.launch_id)
        self.assertIsNone(public["run_id"])
        self.assertEqual(public["launch_state"], "queued")
        self.assertTrue(public["can_stop"])
        for private_path in (self.root, self.runtime.root, study_path):
            self.assertNotIn(str(private_path), serialized)

        stopped = self.state.stop_job(job.launch_id)
        self.assertEqual(stopped["launch_state"], "cancelled")
        self.assertFalse(stopped["can_stop"])

    def test_launch_uses_exchange_timeout_from_selected_method_revision(self) -> None:
        study_path = _write_package(self.package)
        method_path = self.package / "configs" / "methods" / "method.yaml"
        method_path.write_text(
            method_path.read_text(encoding="utf-8").replace(
                "  protocol: batch\n",
                "  protocol: batch\n  exchangeTimeoutSeconds: 37\n",
            ),
            encoding="utf-8",
        )

        with (
            mock.patch(
                "optpilot_studio.ui.server._validate_study",
                return_value={
                    "valid": True,
                    "launch": {"eligible": True, "code": "ready", "reason": None},
                },
            ),
            mock.patch(
                "optpilot_studio.ui.server._schedule_study_launch_execution",
                return_value=True,
            ),
        ):
            launch = self.state.launch_study(
                study_path,
                operation_id="studio-test/method-owned-exchange-timeout",
            )

        self.assertEqual(
            launch.job.plan.input_facts["execution_profile"][
                "method_request_timeout_seconds"
            ],
            37.0,
        )

    def test_http_study_launch_recovers_lost_response_without_second_job(self) -> None:
        study_path = _write_package(self.package)
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": "12345678-1234-4234-8234-123456789abc",
            "study_path": str(study_path),
            "method_request_timeout_seconds": 2000,
        }
        handler, responses = self._handler()
        handler.path = "/api/studies/launch"
        handler._read_json_body = lambda: request  # type: ignore[method-assign]
        completion_attempted = threading.Event()

        def lose_first_completion(*args, **kwargs):
            completion_attempted.set()
            raise OSError("simulated response-loss boundary")

        with (
            mock.patch(
                "optpilot_studio.ui.server._schedule_study_launch_execution",
                return_value=True,
            ) as schedule,
            mock.patch.object(
                self.state.coordination,
                "complete_action",
                side_effect=lose_first_completion,
            ),
        ):
            handler.do_POST()
            accepted, accepted_status = responses[-1]
            self.assertEqual(accepted_status, HTTPStatus.ACCEPTED)
            self.assertEqual(accepted["state"], "preparing")
            self.assertTrue(
                completion_attempted.wait(5),
                "The background preparation did not reach durable completion.",
            )

        schedule.assert_called_once()

        recovered = self._await_study_launch_request(
            handler,
            responses,
            request["request_id"],
            terminal_states={"ready"},
        )
        self.assertEqual(
            recovered["schema"],
            "optpilot.studio-study-launch-preparation.v1",
        )
        self.assertEqual(recovered["request_id"], request["request_id"])
        launch = recovered["launch"]
        self.assertIsNotNone(launch)

        handler.path = "/api/studies/launch"
        with mock.patch(
            "optpilot_studio.ui.server._schedule_study_launch_execution",
            return_value=True,
        ) as replay_schedule:
            handler.do_POST()

        replayed, replayed_status = responses[-1]
        self.assertEqual(replayed_status, HTTPStatus.OK)
        self.assertEqual(
            replayed["schema"],
            "optpilot.studio-study-launch-preparation.v1",
        )
        self.assertEqual(replayed["state"], "ready")
        self.assertEqual(replayed["request_id"], request["request_id"])
        launch = replayed["launch"]
        self.assertIsNone(launch["run_id"])
        self.assertEqual(launch["stage"], "Queued")
        self.assertGreaterEqual(launch["elapsed_seconds"], 0)
        replay_schedule.assert_not_called()
        retained = self.runtime.study_launches.read(launch_id=launch["launch_id"])
        self.assertEqual(
            retained.job.plan.input_facts["execution_profile"][
                "method_request_timeout_seconds"
            ],
            2000.0,
        )

        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM operator_jobs WHERE job_kind = 'study-launch'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(count, 1)
        self.assertNotIn(str(self.root), json.dumps(replayed, sort_keys=True))

        handler.path = f"/api/studies/launches/{launch['launch_id']}"
        handler.do_GET()
        status_payload, status_code = responses[-1]
        self.assertEqual(status_code, HTTPStatus.OK)
        self.assertEqual(status_payload["launch"]["launch_id"], launch["launch_id"])

        handler.path = f"/api/studies/launches/{launch['launch_id']}/stop"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.studio-study-launch-stop-request.v1",
            "request_id": "87654321-4321-4321-8321-cba987654321",
        }
        handler.do_POST()
        stopped, stopped_status = responses[-1]
        self.assertEqual(stopped_status, HTTPStatus.OK)
        self.assertEqual(stopped["launch"]["status"], "cancelled")
        self.assertFalse(stopped["launch"]["can_stop"])

    def test_http_study_launch_rejects_request_id_reuse_for_another_source(self) -> None:
        study_path = _write_package(self.package)
        request_id = "22345678-1234-4234-8234-123456789abc"
        handler, responses = self._handler()
        handler.path = "/api/studies/launch"
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": request_id,
            "study_path": str(study_path),
            "method_request_timeout_seconds": 20,
        }
        handler._read_json_body = lambda: request  # type: ignore[method-assign]
        with mock.patch(
            "optpilot_studio.ui.server._schedule_study_launch_execution",
            return_value=True,
        ):
            handler.do_POST()
            self.assertEqual(responses[-1][1], HTTPStatus.ACCEPTED)

            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                **request,
                "method_request_timeout_seconds": 21,
            }
            handler.do_POST()
            rejected, rejected_status = responses[-1]
            self.assertEqual(rejected_status, HTTPStatus.CONFLICT)
            self.assertEqual(rejected["type"], "CoordinationConflict")
            self.assertIn("another request", rejected["error"])

            self._await_study_launch_request(
                handler,
                responses,
                request_id,
                terminal_states={"ready", "failed"},
            )

    def test_study_launch_request_bounds_method_callback_timeout(self) -> None:
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": "27345678-1234-4234-8234-123456789abc",
            "study_path": "/package/studies/study.yaml",
            "method_request_timeout_seconds": 2000,
        }
        canonical = _canonical_study_launch_request(request)
        self.assertEqual(canonical["method_request_timeout_seconds"], 2000.0)

        for invalid in (True, 0, -1, float("inf"), 86400.1, "2000"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "method_request_timeout_seconds"
            ):
                _canonical_study_launch_request(
                    {**request, "method_request_timeout_seconds": invalid}
                )

    def test_http_study_launch_failure_is_durable_and_replayable(self) -> None:
        study_path = _write_package(self.package, method_protocol="session")
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": "32345678-1234-4234-8234-123456789abc",
            "study_path": str(study_path),
        }
        handler, responses = self._handler()
        handler.path = "/api/studies/launch"
        handler._read_json_body = lambda: request  # type: ignore[method-assign]

        handler.do_POST()
        accepted, accepted_status = responses[-1]
        self.assertEqual(accepted_status, HTTPStatus.ACCEPTED)
        self.assertEqual(accepted["state"], "preparing")
        first = self._await_study_launch_request(
            handler,
            responses,
            request["request_id"],
            terminal_states={"failed"},
        )
        handler.path = "/api/studies/launch"
        handler.do_POST()
        replay, replay_status = responses[-1]

        self.assertEqual(replay_status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(replay["state"], "failed")
        self.assertEqual(replay["request_id"], first["request_id"])
        self.assertEqual(replay["failure"], first["failure"])
        self.assertEqual(first["failure"]["code"], "method_mode_unsupported")
        self.assertIn("Python", first["failure"]["message"])
        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM operator_jobs WHERE job_kind = 'study-launch'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_handed_off_run_stop_uses_durable_run_cancellation_route(self) -> None:
        study_path = _write_package(self.package)
        operation_id = "studio-test/run-side-cancellation"
        with (
            mock.patch(
                "optpilot_studio.ui.server._validate_study",
                return_value={
                    "valid": True,
                    "launch": {"eligible": True, "code": "ready", "reason": None},
                },
            ),
            mock.patch(
                "optpilot_studio.ui.server._schedule_study_launch_execution",
                return_value=True,
            ),
        ):
            launch = self.state.launch_study(
                study_path,
                study_name="Run-side cancellation",
                operation_id=operation_id,
            )
        record = self.runtime.operator_jobs.read(job_id=launch.launch_id)
        context = _plan_context(record)
        starting = self.runtime.operator_jobs.begin_control_plane_start(
            job_id=launch.launch_id,
            binding_id=context["binding_id"],
            launch_token=context["launch_token"],
            evidence_fingerprint=context["evidence_fingerprint"],
            launch_request_digest=context["launch_request_digest"],
        )
        handoff = self.runtime.ledger.handoff_study_launch_to_run(
            operation_id=f"test/handoff/{launch.launch_id}",
            actor_principal_id=self.runtime.actor_principal_id,
            job_id=launch.launch_id,
            expected_job_revision=starting.revision,
        ).handoff

        row = next(
            item
            for item in _realm_runs_payload(self.state)["runs"]
            if item["run_id"] == handoff.run_id
        )
        self.assertTrue(row["can_stop"])
        self.assertEqual(row["status"], "running")

        handler, responses = self._handler()
        handler.path = f"/api/runs/{handoff.run_id}/cancel"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-cancel-request.v1",
            "request_id": "77777777-7777-4777-8777-777777777777",
        }
        handler.do_POST()

        payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["run_id"], handoff.run_id)
        self.assertEqual(payload["launch"]["status"], "stopping")
        self.assertTrue(payload["launch"]["cancellation_requested"])
        self.assertFalse(payload["run"]["can_stop"])
        request = self.runtime.ledger.read_run_cancellation_request(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=handoff.run_id,
        )
        self.assertIsNotNone(request)

    def test_package_selection_is_most_specific_and_rejects_unbounded_study(
        self,
    ) -> None:
        nested = self.package / "nested-package"
        nested_studies = nested / "studies"
        nested_studies.mkdir(parents=True)
        nested_study = nested_studies / "nested.yaml"
        nested_study.write_text("config: study\n", encoding="utf-8")
        state = UiState(
            cwd=self.root,
            catalog_roots=[self.package, nested],
            run_roots=[],
            realm_runtime=self.runtime,
        )

        self.assertEqual(
            _configured_study_package_root(state, nested_study),
            nested.resolve(),
        )
        outside = self.root / "outside.yaml"
        outside.write_text("config: study\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configured catalog package"):
            _configured_study_package_root(state, outside)

    def test_run_catalog_detail_children_and_timeline_are_bounded_and_path_free(
        self,
    ) -> None:
        listing = _realm_runs_payload(self.state, limit=1)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        serialized = json.dumps(
            {"listing": listing, "detail": detail},
            sort_keys=True,
        )
        self.assertEqual(listing["catalog"]["query"]["limit"], 1)
        self.assertEqual(listing["runs"][0]["id"], self.run_id)
        self.assertEqual(listing["runs"][0]["budget"]["max_trials"], 10)
        self.assertNotIn("path", listing["runs"][0])
        self.assertEqual(detail["run"]["run_id"], self.run_id)
        selected_head = detail["workbench"]["head"]
        comparability = detail["workbench"]["comparability"]
        overview = detail["workbench"]["overview"]
        self.assertEqual(
            listing["runs"][0]["best_comparable_candidate"],
            overview["best_candidate"],
        )
        self.assertEqual(listing["runs"][0]["head"], selected_head)
        self.assertEqual(overview["schema"], "optpilot.run-overview-projection.v1")
        self.assertEqual(overview["run_id"], self.run_id)
        self.assertEqual(overview["head"], selected_head)
        self.assertEqual(overview["counts"]["candidates"]["accepted"], 3)
        self.assertEqual(overview["counts"]["candidates"]["complete"], 0)
        self.assertEqual(overview["failure_count"], 0)
        self.assertFalse(overview["best_candidate"]["available"])
        self.assertEqual(
            overview["best_candidate"]["reason"], "no_complete_candidate_yet"
        )
        self.assertTrue(
            overview["limitations"]["entity_page_size_independent"]
        )
        self.assertEqual(overview["objective_series"]["points"], [])
        self.assertEqual(
            comparability["schema"],
            "optpilot.run-comparability-projection.v1",
        )
        self.assertEqual(comparability["run_id"], self.run_id)
        self.assertEqual(comparability["head"], selected_head)
        self.assertEqual(
            comparability["fingerprints"]["environment_evaluation"][
                "source_granularity"
            ],
            "whole_package",
        )
        self.assertEqual(
            comparability["fingerprints"]["environment_evaluation"][
                "comparison_strength"
            ],
            "conservative",
        )
        self.assertFalse(comparability["automatic_ranking"]["eligible"])
        self.assertTrue(comparability["automatic_ranking"]["blocking_reasons"])
        self.assertEqual(
            set(comparability["reproducibility"]["dimensions"]),
            {
                "semantic_inputs",
                "bytes_available_now",
                "runtime_identity",
                "runtime_available_now",
                "isolation",
                "external_replayability",
                "seed_repetition_plan",
                "terminal_evidence",
            },
        )
        comparability_json = json.dumps(comparability, sort_keys=True)
        overview_json = json.dumps(overview, sort_keys=True)
        for forbidden in ("content_ref", "owner_id", "source_ref", "runtime_ref"):
            self.assertNotIn(forbidden, comparability_json)
            self.assertNotIn(forbidden, overview_json)
        for private_path in (self.root, self.runtime.root):
            self.assertNotIn(str(private_path), comparability_json)
            self.assertNotIn(str(private_path), overview_json)
        self.assertEqual(detail["timeline"]["head"], selected_head)
        for kind, page in detail["pages"].items():
            self.assertEqual(page["query"]["kind"], kind)
            self.assertEqual(page["head"], selected_head)
            self.assertLessEqual(page["page"]["count"], page["query"]["limit"])
            self.assertTrue(page["limitations"]["bounded_public_page"])
        for private_path in (self.root, self.runtime.root):
            self.assertNotIn(str(private_path), serialized)

        handler, responses = self._handler()
        handler.path = "/api/runs?limit=1"
        handler.do_GET()
        routed_listing, routed_status = responses[-1]
        self.assertEqual(routed_status, HTTPStatus.OK)
        self.assertEqual(routed_listing["runs"][0]["id"], self.run_id)
        self.assertEqual(routed_listing["catalog"]["query"]["limit"], 1)

        handler._handle_run_get(
            f"/api/runs/{self.run_id}/candidate",
            {"limit": ["1"]},
        )
        candidate_page = responses[-1][0]
        self.assertEqual(candidate_page["query"]["limit"], 1)
        self.assertEqual(candidate_page["page"]["count"], 1)
        self.assertTrue(candidate_page["page"]["has_more"])

        handler._handle_run_get(
            f"/api/runs/{self.run_id}/timeline",
            {
                "revision": [str(selected_head["revision"])],
                "head_sequence": [str(selected_head["sequence"])],
                "after_sequence": ["0"],
                "limit": ["1"],
            },
        )
        timeline = responses[-1][0]
        self.assertEqual(timeline["query"]["limit"], 1)
        self.assertLessEqual(timeline["page"]["count"], 1)
        with self.assertRaises(RealmConflict):
            handler._handle_run_get(
                f"/api/runs/{self.run_id}/timeline",
                {
                    "revision": [str(selected_head["revision"] - 1)],
                    "head_sequence": [str(selected_head["sequence"])],
                },
            )
        handler.path = (
            f"/api/runs/{self.run_id}/timeline"
            f"?revision={selected_head['revision'] - 1}"
            f"&head_sequence={selected_head['sequence']}"
        )
        handler.do_GET()
        self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)

    def test_run_list_keeps_results_when_old_study_metadata_is_unsupported(
        self,
    ) -> None:
        service = mock.Mock()
        service.read_for_run.side_effect = RealmIntegrityError(
            "Study launch retained input facts are unsupported."
        )
        with mock.patch(
            "optpilot_studio.ui.server._study_launch_service_for_state",
            return_value=service,
        ):
            payload = _realm_runs_payload(self.state)

        self.assertEqual([item["run_id"] for item in payload["runs"]], [self.run_id])
        self.assertEqual(payload["unavailable"]["count"], 1)
        self.assertEqual(payload["unavailable"]["limited_count"], 1)
        self.assertEqual(payload["unavailable"]["hidden_count"], 0)
        self.assertEqual(
            payload["unavailable"]["items"][0]["code"],
            "older_study_metadata_unsupported",
        )
        self.assertNotIn(
            "input facts",
            json.dumps(payload["unavailable"], sort_keys=True),
        )

    def test_run_list_isolates_one_unverifiable_projection(self) -> None:
        with mock.patch.object(
            type(self.runtime.run_views),
            "workbench_head",
            side_effect=RealmIntegrityError("private diagnostic"),
        ):
            payload = _realm_runs_payload(self.state)

        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["unavailable"]["count"], 1)
        self.assertEqual(payload["unavailable"]["limited_count"], 0)
        self.assertEqual(payload["unavailable"]["hidden_count"], 1)
        self.assertEqual(
            payload["unavailable"]["items"][0]["code"],
            "run_projection_unavailable",
        )
        self.assertNotIn(
            "private diagnostic",
            json.dumps(payload["unavailable"], sort_keys=True),
        )

    def test_run_list_and_detail_share_singleton_best_candidate_semantics(
        self,
    ) -> None:
        self._complete_default_trial(metric=2.5)

        listing = _realm_runs_payload(self.state)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        list_best = listing["runs"][0]["best_comparable_candidate"]
        detail_best = detail["workbench"]["overview"]["best_candidate"]

        self.assertEqual(
            listing["runs"][0]["head"],
            detail["workbench"]["head"],
        )
        self.assertEqual(list_best, detail_best)
        self.assertFalse(list_best["available"])
        self.assertEqual(
            list_best["reason"],
            "only_one_complete_candidate",
        )

    def test_run_list_and_detail_share_available_best_comparable_candidate(
        self,
    ) -> None:
        self._complete_default_trial(index=0, metric=2.5)
        self._complete_default_trial(index=1, metric=1.5)

        listing = _realm_runs_payload(self.state)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        list_best = listing["runs"][0]["best_comparable_candidate"]
        detail_best = detail["workbench"]["overview"]["best_candidate"]

        self.assertEqual(
            listing["runs"][0]["head"],
            detail["workbench"]["head"],
        )
        self.assertEqual(list_best, detail_best)
        self.assertTrue(list_best["available"])
        self.assertIsNone(list_best["reason"])
        self.assertEqual(list_best["candidate_id"], "candidate-0")
        self.assertEqual(list_best["value"], 2.5)

    def test_run_row_rejects_a_spliced_overview_head(self) -> None:
        head = self.runtime.run_views.workbench_head(
            ref=RunViewRef(run_id=self.run_id)
        )
        overview = head.overview.to_dict()
        overview["head"]["sequence"] += 1

        with self.assertRaisesRegex(ValueError, "same Run and exact head"):
            _realm_compat_run_row(
                self.state,
                head.summary.to_dict(),
                overview=overview,
            )

    def test_candidate_address_resolves_from_run_and_shortlist_without_prior_page_state(
        self,
    ) -> None:
        handler, responses = self._handler()
        handler.path = (
            f"/api/runs/{self.run_id}?candidate_id=candidate-2"
        )
        with mock.patch(
            "optpilot_studio.ui.server.RUN_WORKBENCH_DEFAULT_PAGE_SIZE",
            1,
        ):
            handler.do_GET()

        detail, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertNotIn(
            "candidate-2",
            [item["id"] for item in detail["pages"]["candidate"]["items"]],
        )
        resolution = detail["candidate_resolution"]
        self.assertEqual(
            resolution["schema"],
            "optpilot.run-candidate-resolution.v1",
        )
        self.assertEqual(resolution["run_id"], self.run_id)
        self.assertEqual(resolution["candidate_id"], "candidate-2")
        self.assertEqual(resolution["status"], "available")
        self.assertEqual(resolution["source"], "run_head")
        self.assertEqual(resolution["candidate"]["id"], "candidate-2")
        self.assertEqual(resolution["head"], detail["workbench"]["head"])
        self.assertIsNone(resolution["shortlist_card"])

        saved = self.runtime.shortlists.save_candidate(
            operation_id="studio-realm/shortlist/candidate-address",
            run_id=self.run_id,
            presentation_selection=resolution["candidate"]["selection"],
            draft=ShortlistDraft.empty(),
            note="Keep this decision.",
        )
        self.assertEqual(saved.cards[0].candidate_id, "candidate-2")

        handler.path = f"/api/runs/{self.run_id}?candidate_id=candidate-2"
        handler.do_GET()
        shortlisted = responses[-1][0]["candidate_resolution"]
        self.assertEqual(shortlisted["status"], "available")
        self.assertEqual(shortlisted["shortlist_card"]["candidate_id"], "candidate-2")
        self.assertEqual(shortlisted["shortlist_card"]["note"], "Keep this decision.")

        handler.path = f"/api/runs/{self.run_id}?candidate_id=missing-candidate"
        handler.do_GET()
        missing = responses[-1][0]["candidate_resolution"]
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(missing["source"], "none")
        self.assertIsNone(missing["candidate"])
        self.assertIsNone(missing["shortlist_card"])
        self.assertIn("not found", missing["message"])

        serialized = json.dumps(
            {"shortlisted": shortlisted, "missing": missing},
            sort_keys=True,
        )
        for forbidden in (
            "owner_id",
            "owner_revision",
            "candidate_ref",
            "content_ref",
            str(self.root),
            str(self.runtime.root),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_candidate_address_explains_retired_run_and_saved_snapshot(self) -> None:
        live = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
            focused_candidate_id="candidate-1",
        )
        self.runtime.shortlists.save_candidate(
            operation_id="studio-realm/shortlist/retired-candidate-address",
            run_id=self.run_id,
            presentation_selection=live["candidate_resolution"]["candidate"][
                "selection"
            ],
            draft=ShortlistDraft.empty(),
            note="Retained decision.",
        )
        finished = self._seal_default_run()
        owner = self.runtime.ledger.read_owner(
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=finished.run.owner_id,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-realm/run/retire-begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=finished.run.owner_id,
            expected_owner_revision=owner.revision,
            ttl_seconds=60,
        )
        self.runtime.ledger.retire_run(
            operation_id="studio-realm/run/retire",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=finished.run.current_revision,
            expected_owner_revision=owner.revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            change_id=change.change_id,
        )

        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}?candidate_id=candidate-1"
        handler.do_GET()
        detail, status = responses[-1]

        self.assertEqual(status, HTTPStatus.OK)
        resolution = detail["candidate_resolution"]
        self.assertEqual(resolution["status"], "retired")
        self.assertEqual(resolution["candidate"]["id"], "candidate-1")
        self.assertEqual(
            resolution["shortlist_card"]["note"],
            "Retained decision.",
        )
        self.assertIn("retired", resolution["message"])
        self.assertIn("Shortlist", resolution["message"])

    def test_workbench_bundle_uses_one_snapshot_for_head_and_first_pages(self) -> None:
        with mock.patch.object(
            self.runtime.ledger,
            "read_run_snapshot",
            wraps=self.runtime.ledger.read_run_snapshot,
        ) as read_snapshot:
            bundle = self.runtime.run_views.workbench_bundle(
                ref=RunViewRef(run_id=self.run_id),
                limit=2,
            )

        read_snapshot.assert_called_once_with(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        self.assertEqual(
            tuple(bundle.pages),
            ("candidate", "logical_trial", "attempt", "observation", "artifact"),
        )
        self.assertEqual(bundle.head.comparability.run_id, self.run_id)
        self.assertEqual(bundle.head.comparability.head, bundle.head.head)
        for kind, page in bundle.pages.items():
            self.assertEqual(page["run_id"], self.run_id)
            self.assertEqual(page["head"], bundle.head.head)
            self.assertEqual(page["query"]["kind"], kind)
            self.assertEqual(page["query"]["limit"], 2)
            self.assertIsInstance(page["query"]["order"], str)
            self.assertLessEqual(page["page"]["count"], 2)

    def test_candidate_comparison_route_returns_core_projection_verbatim(self) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        baseline, comparison = detail["pages"]["candidate"]["items"][:2]
        head_actions = {
            item["action"]: item
            for item in detail["workbench"]["capabilities"]["actions"]
        }
        self.assertTrue(head_actions["compare"]["supported"])
        self.assertFalse(head_actions["compare"]["eligible"])
        self.assertEqual(
            head_actions["compare"]["reason"],
            "candidate_comparison_selection_required",
        )
        for candidate in (baseline, comparison):
            row_actions = {item["action"]: item for item in candidate["eligibility"]}
            self.assertTrue(row_actions["compare"]["supported"])
            self.assertTrue(row_actions["compare"]["eligible"])
            self.assertIsNone(row_actions["compare"]["reason"])
        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/candidate-comparison"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-candidate-comparison-request.v2",
            "baseline_selection": baseline["selection"],
            "comparison_selection": comparison["selection"],
            "text_diff_path": None,
        }
        handler.do_POST()

        projection, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(
            projection["schema"],
            "optpilot.run-candidate-comparison.v3",
        )
        self.assertEqual(projection["run_id"], self.run_id)
        self.assertEqual(projection["head"], detail["workbench"]["head"])
        self.assertEqual(projection["mode"], "parameters")
        self.assertEqual(
            [item["role"] for item in projection["operands"]],
            ["baseline", "comparison"],
        )
        self.assertEqual(
            [item["candidate"]["id"] for item in projection["operands"]],
            [baseline["id"], comparison["id"]],
        )
        self.assertTrue(projection["eligibility"]["supported"])
        self.assertTrue(projection["eligibility"]["eligible"])
        outcomes = projection["outcomes"]
        self.assertEqual(
            outcomes["schema"],
            "optpilot.run-candidate-outcome-comparison.v1",
        )
        self.assertTrue(outcomes["eligibility"]["eligible"])
        self.assertEqual(outcomes["metrics"]["returned"], 1)
        metric = outcomes["metrics"]["rows"][0]
        self.assertEqual(metric["name"], "score")
        self.assertEqual(metric["role"], "primary")
        self.assertIn("coverage", metric["baseline"])
        self.assertIn("relation", metric)
        self.assertFalse(outcomes["constraints"]["eligibility"]["eligible"])
        candidate_input = projection["candidate_input"]
        self.assertEqual(candidate_input["summary"]["rows"], 1)
        self.assertEqual(candidate_input["summary"]["changed"], 1)
        parameter = candidate_input["parameters"]["rows"][0]
        self.assertEqual(parameter["name"], "x")
        self.assertEqual(parameter["baseline"]["value"], 0)
        self.assertEqual(parameter["comparison"]["value"], 1)
        self.assertEqual(parameter["change"], "changed")
        serialized = json.dumps(projection, sort_keys=True)
        self.assertNotIn("content_ref", serialized)
        self.assertNotIn("owner_id", serialized)
        self.assertNotIn(str(self.realm_root), serialized)

    def test_shortlist_route_saves_the_complete_pending_draft_atomically(self) -> None:
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=self.run_id))
        candidates = detail["pages"]["candidate"]["items"]
        self.assertGreaterEqual(len(candidates), 2)
        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/shortlist"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-shortlist-command.v1",
            "request_id": "01010101-0101-4101-8101-010101010101",
            "command": "save_candidate",
            "presentation_selection": candidates[0]["selection"],
            "draft": {
                "shortlist_id": None,
                "expected_revision": None,
                "title": "Shortlist",
                "cards": [],
            },
            "parameters": {
                "candidate_id": None,
                "note": "",
                "operator_job_id": None,
                "update_saved_result": False,
            },
        }
        handler.do_POST()
        first, first_status = responses[-1]
        self.assertEqual(first_status, HTTPStatus.CREATED)
        self.assertEqual(first["schema"], "optpilot.run-shortlist-response.v1")
        self.assertEqual(first["shortlist"]["revision"], 1)
        self.assertEqual(len(first["shortlist"]["cards"]), 1)
        self.assertEqual(
            first["collection"]["items"][0]["saved_result_at"],
            first["shortlist"]["cards"][0]["saved_result_at"],
        )
        self.assertEqual(
            first["collection"]["items"][0]["evidence_digest"],
            first["shortlist"]["cards"][0]["saved_evidence_digest"],
        )

        first_card = first["collection"]["items"][0]
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-shortlist-command.v1",
            "request_id": "02020202-0202-4202-8202-020202020202",
            "command": "save_candidate",
            "presentation_selection": candidates[1]["selection"],
            "draft": {
                "shortlist_id": first["shortlist"]["shortlist_id"],
                "expected_revision": first["shortlist"]["revision"],
                "title": "Promising candidates",
                "cards": [
                    {
                        "selection_digest": first_card["selection"][
                            "selection_digest"
                        ],
                        "note": "Keep this pending note.",
                        "inspection_outcomes": [],
                    }
                ],
            },
            "parameters": {
                "candidate_id": None,
                "note": "Second candidate",
                "operator_job_id": None,
                "update_saved_result": False,
            },
        }
        handler.do_POST()
        second, second_status = responses[-1]
        self.assertEqual(second_status, HTTPStatus.CREATED)
        self.assertEqual(second["shortlist"]["revision"], 2)
        self.assertEqual(second["shortlist"]["title"], "Promising candidates")
        self.assertEqual(
            [card["candidate_id"] for card in second["shortlist"]["cards"]],
            [candidates[0]["id"], candidates[1]["id"]],
        )
        self.assertEqual(
            [card["note"] for card in second["shortlist"]["cards"]],
            ["Keep this pending note.", "Second candidate"],
        )

        # The UI uses the same atomic save-candidate command when explicitly
        # refreshing a saved result.  The complete pending draft must still be
        # committed, so notes and order cannot be lost around the refresh.
        first_saved, second_saved = second["shortlist"]["cards"]
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-shortlist-command.v1",
            "request_id": "03030303-0303-4303-8303-030303030303",
            "command": "save_candidate",
            "presentation_selection": candidates[0]["selection"],
            "draft": {
                "shortlist_id": second["shortlist"]["shortlist_id"],
                "expected_revision": second["shortlist"]["revision"],
                "title": "Ordered finalists",
                "cards": [
                    {
                        "selection_digest": second_saved["selection"][
                            "selection_digest"
                        ],
                        "note": "Second candidate stays first.",
                        "inspection_outcomes": [],
                    },
                    {
                        "selection_digest": first_saved["selection"][
                            "selection_digest"
                        ],
                        "note": "Pending note survives result refresh.",
                        "inspection_outcomes": [],
                    },
                ],
            },
            "parameters": {
                "candidate_id": None,
                "note": "",
                "operator_job_id": None,
                "update_saved_result": True,
            },
        }
        handler.do_POST()
        refreshed, refreshed_status = responses[-1]
        self.assertEqual(refreshed_status, HTTPStatus.CREATED)
        self.assertEqual(refreshed["shortlist"]["revision"], 3)
        self.assertEqual(
            [card["candidate_id"] for card in refreshed["shortlist"]["cards"]],
            [candidates[1]["id"], candidates[0]["id"]],
        )
        self.assertEqual(
            [card["note"] for card in refreshed["shortlist"]["cards"]],
            [
                "Second candidate stays first.",
                "Pending note survives result refresh.",
            ],
        )
        self.assertEqual(
            sum(
                card["candidate_id"] == candidates[0]["id"]
                for card in refreshed["shortlist"]["cards"]
            ),
            1,
        )

    def test_review_collection_route_adds_edits_and_exports_exact_revision(self) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        self.assertIsNone(detail["review_collection"])
        candidate = detail["pages"]["candidate"]["items"][0]
        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/review-collection"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "11111111-1111-4111-8111-111111111111",
            "command": "add",
            "presentation_selection": candidate["selection"],
            "draft": {
                "note": "",
                "inspection_outcomes": [],
                "operator_job_id": None,
            },
        }
        handler.do_POST()

        added, status = responses[-1]
        self.assertEqual(status, HTTPStatus.CREATED)
        collection = added["collection"]
        self.assertEqual(collection["revision"], 1)
        self.assertEqual(added["history"]["current_revision"], 1)
        self.assertEqual(
            [item["revision"] for item in added["history"]["items"]],
            [1],
        )
        self.assertEqual(collection["retention_policy"], "decision")
        self.assertEqual(collection["items"][0]["selection"]["entity_id"], candidate["id"])
        self.assertFalse(
            collection["items"][0]["evidence"]["retention"][
                "runnable_closure_retained"
            ]
        )

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "22222222-2222-4222-8222-222222222222",
            "command": "save",
            "presentation_selection": None,
            "draft": {
                "collection_id": collection["collection_id"],
                "expected_revision": 1,
                "title": "Visual inspection shortlist",
                "items": [
                    {
                        "selection_digest": collection["items"][0]["selection"][
                            "selection_digest"
                        ],
                        "note": "Open the environment preview before choosing.",
                        "inspection_outcomes": [],
                    }
                ],
            },
        }
        handler.do_POST()
        saved, saved_status = responses[-1]
        self.assertEqual(saved_status, HTTPStatus.CREATED)
        self.assertEqual(saved["collection"]["revision"], 2)
        self.assertEqual(
            [item["revision"] for item in saved["history"]["items"]],
            [2, 1],
        )
        self.assertEqual(
            saved["collection"]["items"][0]["note"],
            "Open the environment preview before choosing.",
        )

        handler._handle_run_get(
            f"/api/runs/{self.run_id}/review-collection",
            {"revision": ["1"], "format": ["export"]},
        )
        exported = responses[-1][0]["collection"]
        self.assertEqual(exported["schema"], "optpilot.review-decision-export.v1")
        self.assertEqual(exported["revision"], 1)
        self.assertEqual(exported["revision_digest"], collection["revision_digest"])

        handler._handle_run_get(
            f"/api/runs/{self.run_id}/review-collection",
            {"revision": ["1"]},
        )
        historical = responses[-1][0]
        self.assertEqual(historical["collection"]["revision"], 1)
        self.assertEqual(
            [item["revision"] for item in historical["history"]["items"]],
            [2, 1],
        )

        refreshed = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        self.assertEqual(refreshed["review_collection"]["revision"], 2)
        self.assertEqual(
            [
                item["revision"]
                for item in refreshed["review_collection_history"]["items"]
            ],
            [2, 1],
        )
        serialized = json.dumps(refreshed["review_collection"], sort_keys=True)
        self.assertNotIn(str(self.realm_root), serialized)

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "33333333-3333-4333-8333-333333333333",
            "command": "save",
            "presentation_selection": None,
            "draft": {
                "collection_id": collection["collection_id"],
                "expected_revision": 1,
                "title": "Stale",
                "items": [],
            },
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)
        self.assertIn("changed", responses[-1][0]["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "44444444-4444-4444-8444-444444444443",
            "command": "delete",
            "presentation_selection": None,
            "draft": {
                "collection_id": collection["collection_id"],
                "expected_revision": 1,
                "expected_revision_digest": collection["revision_digest"],
                "confirmation": "delete_review_collection",
            },
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)
        self.assertIn("changed", responses[-1][0]["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "44444444-4444-4444-8444-444444444444",
            "command": "delete",
            "presentation_selection": None,
            "draft": {
                "collection_id": collection["collection_id"],
                "expected_revision": saved["collection"]["revision"],
                "expected_revision_digest": saved["collection"][
                    "revision_digest"
                ],
                "confirmation": "delete_review_collection",
            },
        }
        handler.do_POST()
        deleted, deleted_status = responses[-1]
        self.assertEqual(deleted_status, HTTPStatus.OK)
        self.assertEqual(
            deleted["schema"], "optpilot.review-collection-response.v2"
        )
        self.assertIsNone(deleted["collection"])
        self.assertIsNone(deleted["history"])
        self.assertEqual(
            deleted["deletion"]["schema"],
            "optpilot.review-collection-deletion.v1",
        )
        self.assertEqual(
            deleted["deletion"]["previous_revision_digest"],
            saved["collection"]["revision_digest"],
        )
        self.assertGreaterEqual(deleted["deletion"]["released_memberships"], 0)

        after_delete = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        self.assertIsNone(after_delete["review_collection"])
        self.assertIsNone(after_delete["review_collection_history"])

    def test_terminal_operator_job_outcomes_attach_to_review_revisions(self) -> None:
        run_id = self._create_runnable_operator_run()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        candidate = detail["pages"]["candidate"]["items"][0]
        minted = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=run_id),
            presentation_selection=candidate["selection"],
        )
        self.assertIsNotNone(minted.selection)

        first_job = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-review/inspection/first",
            selection=minted.selection,
        )
        first_terminal = self.runtime.operator_jobs.execute(job_id=first_job.job_id)
        self.assertTrue(first_terminal.state.terminal)

        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/review-collection"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "77777777-7777-4777-8777-777777777777",
            "command": "add",
            "presentation_selection": candidate["selection"],
            "draft": {
                "note": "",
                "inspection_outcomes": [],
                "operator_job_id": first_job.job_id,
            },
        }
        handler.do_POST()

        added, added_status = responses[-1]
        self.assertEqual(added_status, HTTPStatus.CREATED)
        first_revision = added["collection"]
        inspection = first_revision["items"][0]["inspection_outcomes"][0]
        self.assertEqual(
            inspection["schema"], "optpilot.review-inspection-outcome.v1"
        )
        self.assertEqual(inspection["operator_job_id"], first_job.job_id)
        self.assertEqual(inspection["job_kind"], "candidate-debug-run")
        self.assertEqual(
            first_revision["items"][0]["selection"]["entity_id"],
            "candidate-debug",
        )
        self.assertIn(inspection["outcome"]["status"], {"succeeded", "failed"})
        self.assertNotIn("target", inspection)
        self.assertNotIn("execution_policy", inspection)
        self.assertNotIn("plan_digest", inspection)
        self.assertNotIn(
            "content_ref", json.dumps(inspection, sort_keys=True)
        )

        forged = {**inspection, "operator_job_id": "operator-job-" + "0" * 32}
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "77777777-7777-4777-8777-777777777778",
            "command": "save",
            "presentation_selection": None,
            "draft": {
                "collection_id": first_revision["collection_id"],
                "expected_revision": 1,
                "title": first_revision["title"],
                "items": [
                    {
                        "selection_digest": first_revision["items"][0][
                            "selection"
                        ]["selection_digest"],
                        "note": "",
                        "inspection_outcomes": [forged],
                    }
                ],
            },
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)
        self.assertIn("attached by job id", responses[-1][0]["error"])

        second_job = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-review/inspection/second",
            selection=minted.selection,
        )
        second_terminal = self.runtime.operator_jobs.execute(job_id=second_job.job_id)
        self.assertTrue(second_terminal.state.terminal)
        item = first_revision["items"][0]
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "88888888-8888-4888-8888-888888888888",
            "command": "attach_inspection",
            "presentation_selection": None,
            "draft": {
                "collection_id": first_revision["collection_id"],
                "expected_revision": 1,
                "title": "Inspected candidates",
                "items": [
                    {
                        "selection_digest": item["selection"]["selection_digest"],
                        "note": "The current unsaved note is committed with the inspection.",
                        "inspection_outcomes": item["inspection_outcomes"],
                    }
                ],
                "selection_digest": item["selection"]["selection_digest"],
                "operator_job_id": second_job.job_id,
            },
        }
        handler.do_POST()

        attached, attached_status = responses[-1]
        self.assertEqual(attached_status, HTTPStatus.CREATED)
        self.assertEqual(attached["collection"]["revision"], 2)
        self.assertEqual(attached["collection"]["title"], "Inspected candidates")
        attached_item = attached["collection"]["items"][0]
        self.assertEqual(
            attached_item["note"],
            "The current unsaved note is committed with the inspection.",
        )
        self.assertEqual(
            [
                value["operator_job_id"]
                for value in attached_item["inspection_outcomes"]
            ],
            [first_job.job_id, second_job.job_id],
        )

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
            "command": "attach_inspection",
            "presentation_selection": None,
            "draft": {
                "collection_id": attached["collection"]["collection_id"],
                "expected_revision": 2,
                "title": attached["collection"]["title"],
                "items": [
                    {
                        "selection_digest": attached_item["selection"][
                            "selection_digest"
                        ],
                        "note": attached_item["note"],
                        "inspection_outcomes": attached_item[
                            "inspection_outcomes"
                        ],
                    }
                ],
                "selection_digest": attached_item["selection"][
                    "selection_digest"
                ],
                "operator_job_id": second_job.job_id,
            },
        }
        handler.do_POST()
        replayed, replayed_status = responses[-1]
        self.assertEqual(replayed_status, HTTPStatus.CREATED)
        self.assertEqual(replayed["collection"]["revision"], 2)

        pending_job = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-review/inspection/pending",
            selection=minted.selection,
        )
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.review-collection-command.v3",
            "request_id": "99999999-9999-4999-8999-999999999999",
            "command": "attach_inspection",
            "presentation_selection": None,
            "draft": {
                "collection_id": attached["collection"]["collection_id"],
                "expected_revision": 2,
                "title": attached["collection"]["title"],
                "items": [
                    {
                        "selection_digest": attached_item["selection"][
                            "selection_digest"
                        ],
                        "note": attached_item["note"],
                        "inspection_outcomes": attached_item[
                            "inspection_outcomes"
                        ],
                    }
                ],
                "selection_digest": attached_item["selection"][
                    "selection_digest"
                ],
                "operator_job_id": pending_job.job_id,
            },
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)
        self.assertIn("terminal", responses[-1][0]["error"])
        current = self.runtime.review_collections.read_for_run(run_id=run_id)
        self.assertEqual(current.revision, 2)

    def test_file_candidates_compare_outcomes_when_legacy_manifest_is_unavailable(
        self,
    ) -> None:
        run_id = self._create_file_candidate_run()
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        baseline, comparison = detail["pages"]["candidate"]["items"][:2]
        for candidate in (baseline, comparison):
            compare = {item["action"]: item for item in candidate["eligibility"]}[
                "compare"
            ]
            self.assertTrue(compare["supported"])
            self.assertTrue(compare["eligible"])
            self.assertIsNone(compare["reason"])

        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/candidate-comparison"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-candidate-comparison-request.v2",
            "baseline_selection": baseline["selection"],
            "comparison_selection": comparison["selection"],
            "text_diff_path": None,
        }
        handler.do_POST()

        projection, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(
            projection["schema"],
            "optpilot.run-candidate-comparison.v3",
        )
        self.assertEqual(projection["mode"], "files")
        self.assertTrue(projection["outcomes"]["eligibility"]["eligible"])
        self.assertEqual(projection["outcomes"]["metrics"]["returned"], 1)
        candidate_input = projection["candidate_input"]
        self.assertTrue(candidate_input["eligibility"]["supported"])
        self.assertFalse(candidate_input["eligibility"]["eligible"])
        self.assertEqual(
            candidate_input["eligibility"]["code"],
            "candidate_file_manifest_unavailable",
        )
        self.assertIsNone(candidate_input["parameters"])
        self.assertIsNone(candidate_input["files"])
        serialized = json.dumps(projection, sort_keys=True)
        self.assertNotIn("content_ref", serialized)
        self.assertNotIn("owner_id", serialized)
        self.assertNotIn(str(self.realm_root), serialized)

    def test_file_candidate_comparison_loads_one_bounded_text_diff(self) -> None:
        run_id = self._create_file_candidate_run(sealed_specs=True)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        baseline, comparison = detail["pages"]["candidate"]["items"][:2]
        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/candidate-comparison"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-candidate-comparison-request.v2",
            "baseline_selection": baseline["selection"],
            "comparison_selection": comparison["selection"],
            "text_diff_path": "run.py",
        }
        handler.do_POST()

        projection, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(
            projection["schema"], "optpilot.run-candidate-comparison.v3"
        )
        candidate_input = projection["candidate_input"]
        self.assertTrue(candidate_input["eligibility"]["eligible"])
        text_diff = candidate_input["files"]["text_diff"]
        self.assertEqual(
            text_diff["schema"], "optpilot.candidate-file-text-diff.v1"
        )
        self.assertTrue(text_diff["eligibility"]["eligible"])
        self.assertEqual(text_diff["relative_path"], "run.py")
        self.assertFalse(text_diff["diff"]["truncated"])
        self.assertIn("-print('baseline')", text_diff["diff"]["text"])
        self.assertIn("+print('comparison')", text_diff["diff"]["text"])
        self.assertIn("+print('extra')", text_diff["diff"]["text"])
        serialized = json.dumps(projection, sort_keys=True)
        self.assertNotIn("content_ref", serialized)
        self.assertNotIn("owner_id", serialized)
        self.assertNotIn(str(self.realm_root), serialized)

    def test_candidate_comparison_route_is_strict_and_not_a_generic_action(
        self,
    ) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        baseline, comparison = detail["pages"]["candidate"]["items"][:2]
        request = {
            "schema": "optpilot.run-candidate-comparison-request.v2",
            "baseline_selection": baseline["selection"],
            "comparison_selection": comparison["selection"],
            "text_diff_path": None,
        }
        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/candidate-comparison"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            **request,
            "browser_rows": [{"name": "x", "change": "same"}],
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)
        self.assertIn("fields differ", responses[-1][0]["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            **request,
            "schema": "optpilot.run-candidate-comparison-request.v0",
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)
        self.assertIn("schema is unsupported", responses[-1][0]["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            **request,
            "text_diff_path": "../run.py",
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)
        self.assertIn("canonical relative path", responses[-1][0]["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            **request,
            "comparison_selection": baseline["selection"],
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)

        handler.path = f"/api/runs/{self.run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "action": "compare",
            "presentation_selection": baseline["selection"],
            "parameters": {},
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.BAD_REQUEST)
        self.assertIn("action is unsupported", responses[-1][0]["error"])

    def test_workbench_capability_batch_rejects_a_forged_bundle_snapshot(
        self,
    ) -> None:
        bundle = self.runtime.run_views.workbench_bundle(
            ref=RunViewRef(run_id=self.run_id),
            limit=2,
        )
        candidate = bundle.snapshot.candidates[0]
        forged_candidate = replace(
            candidate,
            admission=replace(
                candidate.admission,
                lineage={"parents": [], "forged": True},
            ),
        )
        forged_snapshot = replace(
            bundle.snapshot,
            candidates=(forged_candidate, *bundle.snapshot.candidates[1:]),
        )
        forged_bundle = replace(bundle, snapshot=forged_snapshot)
        selection = bundle.pages["candidate"]["items"][0]["selection"]

        with self.assertRaisesRegex(RealmIntegrityError, "not canonical"):
            self.runtime.run_views.workbench_capability_batch(
                ref=RunViewRef(run_id=self.run_id),
                presentation_selections=(selection,),
                bundle=forged_bundle,
            )

    def test_run_detail_composes_head_and_pages_from_one_bundle_snapshot(self) -> None:
        with (
            mock.patch.object(
                self.runtime.ledger,
                "read_run_snapshot",
                wraps=self.runtime.ledger.read_run_snapshot,
            ) as read_snapshot,
            mock.patch(
                "optpilot_studio.ui.server._enrich_workbench_page",
                side_effect=lambda _state, page, **_kwargs: page,
            ),
            mock.patch(
                "optpilot_studio.ui.server._realm_operator_jobs_payload",
                return_value={"jobs": []},
            ),
        ):
            detail = _realm_run_detail(
                self.state,
                ref=RunViewRef(run_id=self.run_id),
            )

        read_snapshot.assert_called_once_with(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        selected_head = detail["workbench"]["head"]
        self.assertEqual(detail["timeline"]["head"], selected_head)
        self.assertTrue(detail["pages"])
        self.assertTrue(
            all(page["head"] == selected_head for page in detail["pages"].values())
        )

    def test_run_detail_full_snapshot_calls_are_constant_as_rows_grow(
        self,
    ) -> None:
        def detail_snapshot_reads():
            with mock.patch.object(
                self.runtime.ledger,
                "read_run_snapshot",
                wraps=self.runtime.ledger.read_run_snapshot,
            ) as read_snapshot:
                detail = _realm_run_detail(
                    self.state,
                    ref=RunViewRef(run_id=self.run_id),
                )
            return read_snapshot.call_count, detail

        small_reads, small = detail_snapshot_reads()
        small_candidate = small["pages"]["candidate"]["items"][0]
        small_actions = {
            item["action"]: item for item in small_candidate["eligibility"]
        }
        self.assertTrue(small_actions["inspect"]["eligible"])
        self.assertEqual(
            small_actions["open_read_only"]["reason"],
            "parameter_candidate_semantic_only",
        )
        self.assertEqual(
            small_actions["keep_editable"]["reason"],
            "parameter_candidate_not_tree",
        )

        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-realm/query-count/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
            expected_owner_revision=snapshot.revision.owner_revision,
            ttl_seconds=120,
        )
        additions = tuple(
            CandidateAdmission(
                candidate_id=f"candidate-query-count-{index}",
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec={"x": 100 + index},
                ),
            )
            for index in range(6)
        )
        trials = tuple(
            LogicalTrialAdmission(
                logical_trial_id=f"trial-query-count-{index}",
                candidate_id=f"candidate-query-count-{index}",
            )
            for index in range(6)
        )
        lease = snapshot.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id="studio-realm/query-count/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=snapshot.revision.revision,
            expected_owner_revision=snapshot.revision.owner_revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(additions, trials),
        )

        large_reads, large = detail_snapshot_reads()
        self.assertGreater(
            large["pages"]["candidate"]["page"]["count"],
            small["pages"]["candidate"]["page"]["count"],
        )
        self.assertEqual(large_reads, small_reads)
        self.assertEqual(large_reads, 2)

    def test_assistant_selection_handle_is_session_bound_and_server_resolved(
        self,
    ) -> None:
        session = _create_agent_session(self.state, {"title": "Run selection context"})
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        row = detail["pages"]["logical_trial"]["items"][0]
        handler, responses = self._handler()
        handler.path = f"/api/agent-sessions/{session['id']}/run-selection"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.assistant-run-selection-request.v1",
            "presentation_selection": row["selection"],
        }
        handler.do_POST()

        minted, status = responses[-1]
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertRegex(minted["handle"], r"^ars_[0-9a-f]{32}$")
        self.assertEqual(
            minted["selection"],
            {"kind": "logical_trial", "entity_id": row["id"]},
        )
        self.assertNotIn("data", minted)
        self.assertNotIn("correlations", minted)
        record = self.state.assistant_run_selections[minted["handle"]]
        self.assertEqual(record.session_id, session["id"])
        self.assertFalse(hasattr(record, "data"))
        self.assertFalse(hasattr(record, "correlations"))

        context = _agent_context_packet(
            self.state,
            session,
            {
                "current_page": "runs",
                "selected_run": {
                    "run_id": self.run_id,
                    "selection_handle": minted["handle"],
                },
            },
        )
        selected = context["selected_run"]
        self.assertEqual(selected["run_id"], self.run_id)
        self.assertEqual(selected["head"], minted["head"])
        self.assertEqual(selected["selection"]["kind"], "logical_trial")
        self.assertEqual(selected["selection"]["entity_id"], row["id"])
        self.assertEqual(selected["selection"]["data"], row["data"])
        self.assertEqual(
            selected["selection"]["correlations"][0]["kind"],
            "candidate",
        )
        self.assertNotIn(minted["handle"], json.dumps(context, sort_keys=True))

        other_session = _create_agent_session(self.state, {"title": "Other session"})
        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            _agent_context_packet(
                self.state,
                other_session,
                {
                    "current_page": "runs",
                    "selected_run": {
                        "run_id": self.run_id,
                        "selection_handle": minted["handle"],
                    },
                },
            )
        with self.assertRaisesRegex(ValueError, "accepts only"):
            _agent_context_packet(
                self.state,
                session,
                {
                    "current_page": "runs",
                    "selected_run": {
                        "run_id": self.run_id,
                        "selection_handle": minted["handle"],
                        "data": {"objective_value": 10**9},
                        "correlations": [],
                    },
                },
            )

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.assistant-run-selection-request.v1",
            "presentation_selection": row["selection"],
            "data": {"objective_value": 10**9},
        }
        handler.do_POST()
        rejected, rejected_status = responses[-1]
        self.assertEqual(rejected_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("accept exactly", rejected["error"])

    def test_assistant_selection_handle_rejects_a_stale_run_head(self) -> None:
        session = _create_agent_session(
            self.state, {"title": "Stale selection context"}
        )
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        row = detail["pages"]["candidate"]["items"][0]
        minted = _mint_assistant_run_selection(
            self.state,
            session_id=session["id"],
            payload={
                "schema": "optpilot.assistant-run-selection-request.v1",
                "presentation_selection": row["selection"],
            },
        )

        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-realm/assistant-stale/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
            expected_owner_revision=snapshot.revision.owner_revision,
            ttl_seconds=120,
        )
        lease = snapshot.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id="studio-realm/assistant-stale/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=snapshot.revision.revision,
            expected_owner_revision=snapshot.revision.owner_revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        candidate_id="candidate-assistant-new-head",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="parameters",
                            spec={"x": 101},
                        ),
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        logical_trial_id="trial-assistant-new-head",
                        candidate_id="candidate-assistant-new-head",
                    ),
                ),
            ),
        )

        with self.assertRaisesRegex(RealmConflict, "stale or unavailable"):
            _agent_context_packet(
                self.state,
                session,
                {
                    "current_page": "runs",
                    "selected_run": {
                        "run_id": self.run_id,
                        "selection_handle": minted["handle"],
                    },
                },
            )
        self.assertNotIn(minted["handle"], self.state.assistant_run_selections)
        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            _agent_context_packet(
                self.state,
                session,
                {
                    "current_page": "runs",
                    "selected_run": {
                        "run_id": self.run_id,
                        "selection_handle": minted["handle"],
                    },
                },
            )

    def test_workbench_actions_are_strict_current_head_and_no_copy(self) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        actions = {item["action"]: item for item in candidate["eligibility"]}
        self.assertTrue(actions["inspect"]["eligible"])
        self.assertFalse(actions["open_read_only"]["supported"])
        self.assertFalse(actions["open_read_only"]["eligible"])
        self.assertEqual(
            actions["open_read_only"]["reason"],
            "parameter_candidate_semantic_only",
        )
        self.assertFalse(actions["keep_editable"]["supported"])
        self.assertFalse(actions["keep_editable"]["eligible"])
        self.assertEqual(
            actions["keep_editable"]["reason"],
            "parameter_candidate_not_tree",
        )
        self.assertFalse(actions["debug_run"]["eligible"])
        self.assertEqual(
            actions["debug_run"]["reason"], "debug_run_compiler_unsupported"
        )
        self.assertFalse(actions["environment_preview"]["supported"])
        trial_actions = {
            item["action"]: item
            for item in detail["pages"]["logical_trial"]["items"][0]["eligibility"]
        }
        self.assertEqual(
            trial_actions["inspect"]["reason"], "candidate_selection_required"
        )

        before_counts = self._realm_counts()
        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "11111111-1111-4111-8111-111111111111",
            "action": "inspect",
            "presentation_selection": candidate["selection"],
            "parameters": {},
        }
        handler.do_POST()

        inspected, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(inspected["action"], "inspect")
        self.assertEqual(
            inspected["inspection"]["candidate"]["candidate_id"],
            candidate["id"],
        )
        self.assertEqual(inspected["inspection"]["candidate"]["spec"], {"x": 0})
        self.assertEqual(
            inspected["inspection"]["realization"],
            {
                "workspace_created": False,
                "content_copied": False,
                "process_started": False,
            },
        )
        self.assertEqual(self._realm_counts(), before_counts)
        serialized = json.dumps(inspected, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("source_owner_id", serialized)
        self.assertNotIn("selection_digest", serialized)

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "22222222-2222-4222-8222-222222222222",
            "action": "environment_preview",
            "presentation_selection": candidate["selection"],
            "parameters": {"profile_id": "default"},
        }
        handler.do_POST()
        disabled, disabled_status = responses[-1]
        self.assertEqual(disabled_status, HTTPStatus.CONFLICT)
        self.assertIn("retained profile", disabled["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "33333333-3333-4333-8333-333333333333",
            "action": "debug_run",
            "presentation_selection": candidate["selection"],
            "parameters": {},
            "owner_id": "untrusted-owner",
        }
        handler.do_POST()
        rejected, rejected_status = responses[-1]
        self.assertEqual(rejected_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("extra=['owner_id']", rejected["error"])

    def test_exact_plan_child_capability_is_batched_redacted_and_bounded(
        self,
    ) -> None:
        active = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        active_action = {
            item["action"]: item
            for item in active["pages"]["candidate"]["items"][0]["eligibility"]
        }["evaluate_child_run"]
        self.assertTrue(active_action["supported"])
        self.assertFalse(active_action["eligible"])
        self.assertEqual(active_action["reason"], "terminal_parent_unavailable")
        self.assertIsNone(active_action["preset"])

        sealed = self._seal_default_run()
        blocked = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        blocked_action = {
            item["action"]: item
            for item in blocked["pages"]["candidate"]["items"][0]["eligibility"]
        }["evaluate_child_run"]
        self.assertFalse(blocked_action["eligible"])
        self.assertEqual(blocked_action["reason"], "child_run_compiler_unsupported")
        self.assertIsNone(blocked_action["preset"])
        with (
            mock.patch.object(
                self.runtime.ledger,
                "read_run_snapshot",
                wraps=self.runtime.ledger.read_run_snapshot,
            ) as read_snapshot,
            mock.patch(
                "optpilot.realm.run_child_service."
                "RealmChildRunService.prepare_exact_plan_selection_batch_from_snapshot",
                wraps=(
                    self.runtime.child_runs.prepare_exact_plan_selection_batch_from_snapshot
                ),
            ) as prepare_batch,
            mock.patch(
                "optpilot_studio.ui.server._candidate_child_run_runtime_capability",
                return_value=(True, None),
            ),
        ):
            detail = _realm_run_detail(
                self.state,
                ref=RunViewRef(run_id=self.run_id),
            )
        self.assertEqual(read_snapshot.call_count, 2)
        prepare_batch.assert_called_once()
        self.assertEqual(
            len(prepare_batch.call_args.kwargs["selections"]),
            3,
        )

        candidate = detail["pages"]["candidate"]["items"][0]
        action = {item["action"]: item for item in candidate["eligibility"]}[
            "evaluate_child_run"
        ]
        self.assertTrue(action["supported"])
        self.assertTrue(action["eligible"])
        self.assertIsNone(action["reason"])
        self.assertEqual(candidate["context"]["environment"]["id"], "test-environment")
        self.assertRegex(
            candidate["context"]["environment"]["revision"],
            r"^[0-9a-f]{64}$",
        )
        preset = action["preset"]
        self.assertEqual(
            set(preset),
            {
                "schema",
                "id",
                "parent_run_id",
                "parent_seal_digest",
                "plan_digest",
                "candidate",
                "environment",
                "objective",
                "coordinates",
                "logical_trials",
                "max_trials",
                "method_proposals",
            },
        )
        self.assertEqual(preset["schema"], "optpilot.re-evaluate-exact-plan-preset.v1")
        self.assertEqual(preset["parent_run_id"], self.run_id)
        self.assertEqual(
            preset["parent_seal_digest"], sealed.terminal_seal.anchor.seal_digest
        )
        self.assertEqual(
            preset["candidate"], {"id": candidate["id"], "format": "parameters"}
        )
        self.assertEqual(preset["environment"]["id"], "test-environment")
        self.assertRegex(preset["environment"]["revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            preset["objective"], {"metric": "score", "direction": "maximize"}
        )
        self.assertEqual(preset["coordinates"], [{"seed": 0, "repetition_index": 0}])
        self.assertEqual(preset["logical_trials"], 1)
        self.assertEqual(preset["max_trials"], 1)
        self.assertIs(preset["method_proposals"], False)
        serialized = json.dumps(preset, sort_keys=True)
        for forbidden in (
            "candidate_ref",
            "content_ref",
            "owner_id",
            "parent_budget_slot",
            "parent_logical_trial_id",
            "selection_digest",
            str(self.root),
        ):
            self.assertNotIn(forbidden, serialized)

        bundle = self.runtime.run_views.workbench_bundle(
            ref=RunViewRef(run_id=self.run_id)
        )
        row = bundle.pages["candidate"]["items"][0]
        facts_by_id = self.runtime.run_views.workbench_capability_batch(
            ref=RunViewRef(run_id=self.run_id),
            presentation_selections=(row["selection"],),
            bundle=bundle,
        )
        facts = facts_by_id[row["selection"]["selection_id"]]
        preparations = (
            self.runtime.child_runs.prepare_exact_plan_selection_batch_from_snapshot(
                snapshot=bundle.snapshot,
                selections=(facts.selection,),
            )
        )
        preparation = preparations[facts.selection.selection_digest]
        oversized = {
            **preset,
            "coordinates": preset["coordinates"] * 101,
            "logical_trials": 101,
            "max_trials": 101,
        }
        with mock.patch(
            "optpilot_studio.ui.server._public_exact_plan_preset",
            return_value=oversized,
        ):
            capped = {
                item["action"]: item
                for item in _row_workbench_action_capabilities(
                    self.state,
                    run_id=self.run_id,
                    row=row,
                    capability_facts=facts,
                    child_run_preparation=preparation,
                )
            }["evaluate_child_run"]
        self.assertTrue(capped["supported"])
        self.assertFalse(capped["eligible"])
        self.assertEqual(
            capped["reason"],
            "exact_plan_confirmation_supports_at_most_100_evaluations",
        )
        self.assertIsNone(capped["preset"])

    def test_exact_plan_child_post_is_strict_idempotent_and_minimal(self) -> None:
        self._seal_default_run()
        preflight = mock.patch(
            "optpilot_studio.ui.server._candidate_child_run_runtime_capability",
            return_value=(True, None),
        )
        preflight.start()
        self.addCleanup(preflight.stop)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        preset = {item["action"]: item for item in candidate["eligibility"]}[
            "evaluate_child_run"
        ]["preset"]
        request = {
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "e1111111-1111-4111-8111-111111111111",
            "action": "evaluate_child_run",
            "presentation_selection": candidate["selection"],
            "parameters": {
                "schema": "optpilot.re-evaluate-exact-plan-confirmation.v1",
                "preset": "re_evaluate_exact_plan",
                "expected_parent_seal_digest": preset["parent_seal_digest"],
                "expected_plan_digest": preset["plan_digest"],
            },
        }
        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            operator_jobs_before = int(
                connection.execute("SELECT COUNT(*) FROM operator_jobs").fetchone()[0]
            )
        finally:
            connection.close()

        response, status = _execute_run_workbench_action(
            self.state,
            run_id=self.run_id,
            payload=request,
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(
            set(response["child_run"]),
            {"schema", "run_id", "parent_run_id", "status"},
        )
        child = response["child_run"]
        self.assertEqual(child["schema"], "optpilot.child-run-public.v1")
        self.assertEqual(child["parent_run_id"], self.run_id)
        self.assertNotEqual(child["run_id"], self.run_id)
        self.assertEqual(child["status"], "running")
        serialized = json.dumps(response, sort_keys=True)
        for forbidden in (
            "candidate_ref",
            "controller_lease",
            "owner_id",
            "request_digest",
            "selection_digest",
            str(self.root),
        ):
            self.assertNotIn(forbidden, serialized)

        child_snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=child["run_id"],
        )
        self.assertEqual(child_snapshot.run.accepted_logical_trials, 1)
        self.assertEqual(child_snapshot.run.max_trials, 1)
        self.assertEqual(child_snapshot.control.current_submission.state, "draining")
        self.assertEqual(
            child_snapshot.control.current_submission.stop_code, "max_trials"
        )
        self.assertIn("child_run", child_snapshot.definition.metadata)
        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            self.assertEqual(
                int(
                    connection.execute("SELECT COUNT(*) FROM operator_jobs").fetchone()[
                        0
                    ]
                ),
                operator_jobs_before,
            )
        finally:
            connection.close()

        replay, replay_status = _execute_run_workbench_action(
            self.state,
            run_id=self.run_id,
            payload=request,
        )
        self.assertEqual(replay_status, HTTPStatus.ACCEPTED)
        self.assertEqual(replay["child_run"], child)

        parent_detail = _realm_run_detail(
            self.state, ref=RunViewRef(run_id=self.run_id)
        )
        child_detail = _realm_run_detail(
            self.state, ref=RunViewRef(run_id=child["run_id"])
        )
        self.assertEqual(
            parent_detail["lineage"],
            {
                "schema": "optpilot.run-lineage-summary.v1",
                "run_id": self.run_id,
                "origin": {"kind": "study", "study_name": ""},
                "re_evaluation_runs": [
                    {
                        "run_id": child["run_id"],
                        "candidate_id": candidate["id"],
                    }
                ],
                "truncated": False,
            },
        )
        self.assertEqual(
            child_detail["lineage"],
            {
                "schema": "optpilot.run-lineage-summary.v1",
                "run_id": child["run_id"],
                "origin": {
                    "kind": "exact-reevaluation",
                    "parent_run_id": self.run_id,
                    "candidate_id": candidate["id"],
                },
                "re_evaluation_runs": [],
                "truncated": False,
            },
        )

        with self.assertRaisesRegex(ValueError, "fields differ"):
            _execute_run_workbench_action(
                self.state,
                run_id=self.run_id,
                payload={
                    **request,
                    "parameters": {
                        **request["parameters"],
                        "max_trials": 999,
                    },
                },
            )
        with self.assertRaisesRegex(
            RealmConflict, "seal or exact evaluation plan changed"
        ):
            _execute_run_workbench_action(
                self.state,
                run_id=self.run_id,
                payload={
                    **request,
                    "request_id": "e2222222-2222-4222-8222-222222222222",
                    "parameters": {
                        **request["parameters"],
                        "expected_plan_digest": "0" * 64,
                    },
                },
            )
        with self.assertRaisesRegex(
            RealmConflict, "seal or exact evaluation plan changed"
        ):
            _execute_run_workbench_action(
                self.state,
                run_id=self.run_id,
                payload={
                    **request,
                    "request_id": "e2444444-4444-4444-8444-444444444444",
                    "parameters": {
                        **request["parameters"],
                        "expected_parent_seal_digest": "0" * 64,
                    },
                },
            )
        with (
            mock.patch(
                "optpilot_studio.ui.server._candidate_child_run_runtime_capability",
                return_value=(False, "child_run_provider_unsupported"),
            ),
            self.assertRaisesRegex(RealmConflict, "child_run_provider_unsupported"),
        ):
            _execute_run_workbench_action(
                self.state,
                run_id=self.run_id,
                payload={
                    **request,
                    "request_id": "e2555555-5555-4555-8555-555555555555",
                },
            )

    def test_exact_plan_child_route_responds_before_core_dispatch(self) -> None:
        self._seal_default_run()
        preflight = mock.patch(
            "optpilot_studio.ui.server._candidate_child_run_runtime_capability",
            return_value=(True, None),
        )
        preflight.start()
        self.addCleanup(preflight.stop)
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        preset = {item["action"]: item for item in candidate["eligibility"]}[
            "evaluate_child_run"
        ]["preset"]
        handler, _responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "e3333333-3333-4333-8333-333333333333",
            "action": "evaluate_child_run",
            "presentation_selection": candidate["selection"],
            "parameters": {
                "schema": "optpilot.re-evaluate-exact-plan-confirmation.v1",
                "preset": "re_evaluate_exact_plan",
                "expected_parent_seal_digest": preset["parent_seal_digest"],
                "expected_plan_digest": preset["plan_digest"],
            },
        }
        events = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: events.append(
                ("response", status, payload["child_run"]["run_id"])
            )
        )
        with mock.patch(
            "optpilot_studio.ui.server._schedule_run_execution",
            side_effect=lambda _state, *, run_id: events.append(
                ("dispatch", run_id)
            ),
        ) as schedule:
            handler.do_POST()

        self.assertEqual(events[0][0:2], ("response", HTTPStatus.ACCEPTED))
        self.assertEqual(events[1], ("dispatch", events[0][2]))
        schedule.assert_called_once_with(
            self.state,
            run_id=events[0][2],
        )

    def test_exact_plan_child_dispatch_has_only_an_in_process_duplicate_guard(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        def execute(*, run_id, dispatch_operation_id):
            self.assertEqual(run_id, "run-child-dispatch-test")
            self.assertTrue(
                dispatch_operation_id.startswith("run-execution-dispatch/")
            )
            started.set()
            release.wait(timeout=5)
            return mock.Mock(run_status="succeeded")

        with (
            mock.patch.object(
                self.runtime.run_execution,
                "describe",
                return_value=mock.Mock(
                    run_id="run-child-dispatch-test",
                    mode=RUN_EXECUTION_MODE_EXACT_PLAN,
                ),
            ),
            mock.patch.object(
                self.runtime.run_execution,
                "execute",
                side_effect=execute,
            ) as execute_run,
        ):
            self.assertTrue(
                _schedule_run_execution(
                    self.state,
                    run_id="run-child-dispatch-test",
                )
            )
            self.assertTrue(started.wait(timeout=5))
            thread = self.state._run_execution_threads["run-child-dispatch-test"]
            self.assertFalse(
                _schedule_run_execution(
                    self.state,
                    run_id="run-child-dispatch-test",
                )
            )
            release.set()
            thread.join(timeout=5)

        call = execute_run.call_args
        self.assertEqual(call.kwargs["run_id"], "run-child-dispatch-test")
        self.assertTrue(
            call.kwargs["dispatch_operation_id"].startswith(
                "run-execution-dispatch/"
            )
        )
        self.assertNotIn("run-child-dispatch-test", self.state._run_execution_threads)

    def test_study_dispatch_failure_after_handoff_transfers_to_run_guard(
        self,
    ) -> None:
        launch_id = "study-launch-transfer-test"
        run_id = "run-study-transfer-test"
        entered = threading.Event()
        release = threading.Event()
        empty_environment_binding = {
            "binding_revision": "method-environment-none",
            "recoverability": "none",
            "requirements": [],
            "schema": METHOD_ENVIRONMENT_BINDING_SCHEMA,
        }
        before_handoff = mock.Mock(
            launch_id=launch_id,
            run_id=None,
            job=mock.Mock(
                plan=mock.Mock(
                    input_facts={
                        "method_environment_binding": empty_environment_binding
                    }
                )
            ),
        )
        after_handoff = mock.Mock(launch_id=launch_id, run_id=run_id)
        service = mock.Mock()
        service.read.side_effect = (before_handoff, after_handoff)

        def fail_after_handoff(*, launch_id, method_environment):
            self.assertEqual(launch_id, "study-launch-transfer-test")
            self.assertEqual(method_environment.names, ())
            self.assertEqual(
                method_environment.binding_revision,
                "method-environment-none",
            )
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            raise OSError("simulated post-handoff provider failure")

        service.execute.side_effect = fail_after_handoff
        with (
            mock.patch(
                "optpilot_studio.ui.server._study_launch_service_for_state",
                return_value=service,
            ),
            mock.patch(
                "optpilot_studio.ui.server._schedule_run_execution",
                return_value=True,
            ) as schedule_run,
        ):
            self.assertTrue(
                _schedule_study_launch_execution(
                    self.state,
                    launch_id=launch_id,
                )
            )
            self.assertTrue(entered.wait(timeout=5))
            thread = self.state._study_launch_threads[launch_id]
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        schedule_run.assert_called_once_with(self.state, run_id=run_id)
        self.assertNotIn(launch_id, self.state._study_launch_threads)

    def test_run_dispatch_retries_failure_with_same_identity_until_terminal(
        self,
    ) -> None:
        run_id = "run-retry-dispatch-test"
        second_entered = threading.Event()
        release = threading.Event()
        dispatch_ids = []

        def execute(*, run_id, dispatch_operation_id):
            self.assertEqual(run_id, "run-retry-dispatch-test")
            dispatch_ids.append(dispatch_operation_id)
            if len(dispatch_ids) == 1:
                raise OSError("simulated recoverable provider failure")
            second_entered.set()
            self.assertTrue(release.wait(timeout=5))
            return mock.Mock(run_status="succeeded")

        with (
            mock.patch.object(
                self.runtime.run_execution,
                "describe",
                return_value=mock.Mock(
                    run_id=run_id,
                    mode=RUN_EXECUTION_MODE_EXACT_PLAN,
                ),
            ),
            mock.patch.object(
                self.runtime.run_execution,
                "execute",
                side_effect=execute,
            ) as execute_run,
            mock.patch.object(
                type(self.runtime.run_reader),
                "summary",
                return_value=mock.Mock(run_status="running"),
            ) as read_summary,
            mock.patch(
                "optpilot_studio.ui.server.time.sleep",
                return_value=None,
            ),
        ):
            self.assertTrue(
                _schedule_run_execution(self.state, run_id=run_id)
            )
            self.assertTrue(second_entered.wait(timeout=5))
            thread = self.state._run_execution_threads[run_id]
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(execute_run.call_count, 2)
        self.assertEqual(len(set(dispatch_ids)), 1)
        self.assertTrue(dispatch_ids[0].startswith("run-execution-dispatch/"))
        read_summary.assert_called_once_with(run_id=run_id)
        self.assertNotIn(run_id, self.state._run_execution_threads)

    def test_run_dispatch_stops_reconciliation_when_runtime_closes(self) -> None:
        run_id = "run-close-dispatch-test"
        entered = threading.Event()
        release = threading.Event()
        closing = threading.Event()

        def execute(*, run_id, dispatch_operation_id):
            self.assertEqual(run_id, "run-close-dispatch-test")
            self.assertTrue(
                dispatch_operation_id.startswith("run-execution-dispatch/")
            )
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            raise OSError("simulated shutdown interruption")

        with (
            mock.patch.object(
                self.runtime.run_execution,
                "describe",
                return_value=mock.Mock(
                    run_id=run_id,
                    mode=RUN_EXECUTION_MODE_EXACT_PLAN,
                ),
            ),
            mock.patch.object(
                self.runtime.run_execution,
                "execute",
                side_effect=execute,
            ) as execute_run,
            mock.patch.object(
                type(self.runtime),
                "closed",
                new_callable=mock.PropertyMock,
                side_effect=lambda: closing.is_set(),
            ),
            mock.patch.object(
                type(self.runtime.run_reader),
                "summary",
            ) as read_summary,
        ):
            self.assertTrue(
                _schedule_run_execution(self.state, run_id=run_id)
            )
            self.assertTrue(entered.wait(timeout=5))
            thread = self.state._run_execution_threads[run_id]
            closing.set()
            release.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        execute_run.assert_called_once()
        read_summary.assert_not_called()
        self.assertNotIn(run_id, self.state._run_execution_threads)

    def test_startup_run_reconciliation_schedules_all_core_descriptors(
        self,
    ) -> None:
        descriptors = (
            mock.Mock(run_id="run-child-older-a"),
            mock.Mock(run_id="run-study-older-b"),
        )
        events = []

        def schedule(_state, *, run_id):
            events.append(("schedule", run_id))
            return True

        with (
            mock.patch.object(
                self.runtime.run_execution,
                "list_reconcilable",
                return_value=descriptors,
            ) as list_runs,
            mock.patch(
                "optpilot_studio.ui.server._schedule_run_execution",
                side_effect=schedule,
            ) as schedule_run,
        ):
            _reconcile_visible_run_executions(self.state)

        self.assertEqual(
            events,
            [
                ("schedule", "run-child-older-a"),
                ("schedule", "run-study-older-b"),
            ],
        )
        list_runs.assert_called_once_with()
        self.assertEqual(schedule_run.call_count, 2)

    def test_startup_rediscovers_child_committed_before_dispatch(self) -> None:
        self._seal_default_run()
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        presentation = detail["pages"]["candidate"]["items"][0]["selection"]
        selection = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=self.run_id),
            presentation_selection=presentation,
        ).selection
        assert selection is not None
        prepared = self.runtime.child_runs.prepare_exact_plan_selections(
            selections=(selection,)
        )
        committed = self.runtime.child_runs.create_prepared_exact_plan(
            operation_id="studio-child/crash-before-dispatch",
            prepared=prepared,
        )
        bootstrap = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(bootstrap.run.controller_generation, 1)

        with mock.patch(
            "optpilot_studio.ui.server._schedule_run_execution",
            return_value=True,
        ) as schedule:
            UiState(
                cwd=self.root,
                catalog_roots=[self.package],
                run_roots=[],
                realm_runtime=self.runtime,
            )

        schedule.assert_called_once_with(
            mock.ANY,
            run_id=committed.run_id,
        )

    def test_content_view_is_opaque_session_bound_bounded_and_explicitly_closed(
        self,
    ) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        selected = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=self.run_id),
            presentation_selection=candidate["selection"],
        ).selection
        assert selected is not None
        ready = SelectionEligibility.ready()
        described = SelectionContentSummary(
            selected.selection_digest,
            ready,
            "tree",
            1,
            len("hello view\n".encode("utf-8")),
        )
        tree_page = SelectionTreePage(
            selected.selection_digest,
            ready,
            (SelectionTreeEntry("README.txt", "file", 11, False),),
            1,
            None,
        )
        byte_range = SelectionByteRead(
            selected.selection_digest,
            ready,
            "README.txt",
            0,
            11,
            b"hello view\n",
            True,
        )
        handler, responses = self._handler()

        with (
            mock.patch.object(
                self.runtime.selection_content,
                "describe",
                return_value=described,
            ),
            mock.patch.object(
                self.runtime.selection_content,
                "list_tree",
                return_value=tree_page,
            ),
            mock.patch.object(
                self.runtime.selection_content,
                "read_range",
                return_value=byte_range,
            ),
        ):
            handler.path = f"/api/runs/{self.run_id}/actions"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "a1111111-1111-4111-8111-111111111111",
                "action": "open_read_only",
                "presentation_selection": candidate["selection"],
                "parameters": {"content_session_id": None},
            }
            handler.do_POST()
            opened, opened_status = responses[-1]
            self.assertEqual(opened_status, HTTPStatus.CREATED)
            view = opened["content_view"]
            self.assertRegex(view["handle"], r"^scv_[0-9a-f]{32}$")
            self.assertRegex(view["content_session_id"], r"^scs_[0-9a-f]{32}$")
            self.assertEqual(view["content_kind"], "tree")
            self.assertEqual(
                view["selection"],
                {"kind": "candidate", "entity_id": candidate["id"]},
            )

            serialized_open = json.dumps(opened, sort_keys=True)
            for forbidden in (
                "selection_digest",
                "content_ref",
                "root_ref",
                "store_id",
                str(self.root),
                str(self.runtime.root),
            ):
                self.assertNotIn(forbidden, serialized_open)

            handler.path = (
                f"/api/content-views/{view['handle']}/tree?"
                f"content_session_id={view['content_session_id']}"
                "&content_ref=tree%3Asha256%3Auntrusted"
            )
            handler.do_GET()
            rejected_query, rejected_query_status = responses[-1]
            self.assertEqual(rejected_query_status, HTTPStatus.BAD_REQUEST)
            self.assertIn("unsupported query parameters", rejected_query["error"])

            handler.path = (
                f"/api/content-views/{view['handle']}/tree?"
                f"content_session_id={view['content_session_id']}&limit=1"
            )
            handler.do_GET()
            tree, tree_status = responses[-1]
            self.assertEqual(tree_status, HTTPStatus.OK)
            self.assertEqual(
                tree["entries"],
                [
                    {
                        "relative_path": "README.txt",
                        "kind": "file",
                        "size_bytes": 11,
                        "executable": False,
                    }
                ],
            )
            self.assertEqual(
                tree["page"],
                {"count": 1, "has_more": False, "next_page_token": None},
            )

            handler.path = (
                f"/api/content-views/{view['handle']}/content?"
                f"content_session_id={view['content_session_id']}"
                "&relative_path=README.txt&offset=0&limit=11"
            )
            handler.do_GET()
            preview, preview_status = responses[-1]
            self.assertEqual(preview_status, HTTPStatus.OK)
            self.assertEqual(preview["encoding"], "utf-8")
            self.assertEqual(preview["text"], "hello view\n")
            self.assertEqual(preview["next_offset"], 11)
            self.assertFalse(preview["has_more"])
            self.assertNotIn("data", preview)

            # A second server-minted tab session cannot use the first tab's
            # opaque handle even though both belong to the same Realm actor.
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "a2222222-2222-4222-8222-222222222222",
                "action": "open_read_only",
                "presentation_selection": candidate["selection"],
                "parameters": {"content_session_id": None},
            }
            handler.path = f"/api/runs/{self.run_id}/actions"
            handler.do_POST()
            second_view = responses[-1][0]["content_view"]
            self.assertNotEqual(
                second_view["content_session_id"], view["content_session_id"]
            )
            handler.path = (
                f"/api/content-views/{view['handle']}/tree?"
                f"content_session_id={second_view['content_session_id']}"
            )
            handler.do_GET()
            self.assertEqual(responses[-1][1], HTTPStatus.NOT_FOUND)

            # A tab may retain its ephemeral session id past the server TTL.
            # Open replaces that well-formed stale id instead of requiring a
            # page reload, while all old views are pruned.
            stale_session_id = second_view["content_session_id"]
            session_record = self.state.selection_content_sessions[stale_session_id]
            self.state.selection_content_sessions[stale_session_id] = replace(
                session_record,
                created_at=session_record.created_at - 10_000,
            )
            handler.path = f"/api/runs/{self.run_id}/actions"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "a4444444-4444-4444-8444-444444444444",
                "action": "open_read_only",
                "presentation_selection": candidate["selection"],
                "parameters": {"content_session_id": stale_session_id},
            }
            handler.do_POST()
            reopened, reopened_status = responses[-1]
            self.assertEqual(reopened_status, HTTPStatus.CREATED)
            self.assertNotEqual(
                reopened["content_view"]["content_session_id"],
                stale_session_id,
            )

            handler.path = f"/api/content-views/{view['handle']}/close"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.selection-content-view-close-request.v1",
                "content_session_id": view["content_session_id"],
            }
            handler.do_POST()
            self.assertEqual(responses[-1][1], HTTPStatus.OK)
            self.assertTrue(responses[-1][0]["closed"])
            self.assertNotIn(view["handle"], self.state.selection_content_views)

            handler.path = (
                f"/api/content-views/{view['handle']}/tree?"
                f"content_session_id={view['content_session_id']}"
            )
            handler.do_GET()
            self.assertEqual(responses[-1][1], HTTPStatus.NOT_FOUND)

    def test_content_view_revalidates_authority_and_handles_utf8_chunk_boundaries(
        self,
    ) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        selected = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=self.run_id),
            presentation_selection=candidate["selection"],
        ).selection
        assert selected is not None
        ready = SelectionEligibility.ready()
        described = SelectionContentSummary(
            selected.selection_digest,
            ready,
            "tree",
            1,
            5,
        )
        split_utf8 = SelectionByteRead(
            selected.selection_digest,
            ready,
            "message.txt",
            0,
            5,
            b"A\xe4\xbd",
            False,
        )
        handler, responses = self._handler()
        with mock.patch.object(
            self.runtime.selection_content,
            "describe",
            return_value=described,
        ):
            handler.path = f"/api/runs/{self.run_id}/actions"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "a3333333-3333-4333-8333-333333333333",
                "action": "open_read_only",
                "presentation_selection": candidate["selection"],
                "parameters": {"content_session_id": None},
            }
            handler.do_POST()
            view = responses[-1][0]["content_view"]

            with mock.patch.object(
                self.runtime.selection_content,
                "read_range",
                return_value=split_utf8,
            ):
                handler.path = (
                    f"/api/content-views/{view['handle']}/content?"
                    f"content_session_id={view['content_session_id']}"
                    "&relative_path=message.txt&offset=0&limit=3"
                )
                handler.do_GET()
            bounded, bounded_status = responses[-1]
            self.assertEqual(bounded_status, HTTPStatus.OK)
            self.assertEqual(bounded["encoding"], "utf-8")
            self.assertEqual(bounded["text"], "A")
            self.assertEqual(bounded["next_offset"], 1)
            self.assertTrue(bounded["has_more"])

            # An already-open immutable selection remains stable when the run
            # head advances; content reads no longer remint presentation
            # coordinates against the newest head.
            tree_page = SelectionTreePage(
                selected.selection_digest,
                ready,
                (SelectionTreeEntry("message.txt", "file", 5, False),),
                1,
                None,
            )
            with (
                mock.patch.object(
                    type(self.runtime.run_views),
                    "mint_selection",
                    side_effect=RealmConflict("Run presentation head changed."),
                ),
                mock.patch.object(
                    self.runtime.selection_content,
                    "list_tree",
                    return_value=tree_page,
                ),
            ):
                handler.path = (
                    f"/api/content-views/{view['handle']}/tree?"
                    f"content_session_id={view['content_session_id']}"
                )
                handler.do_GET()
            self.assertEqual(responses[-1][1], HTTPStatus.OK)
            self.assertIn(view["handle"], self.state.selection_content_views)

            # Revoked/lost byte authority or immutable-content corruption in
            # the bounded read itself still fails closed and discards the
            # process-local view handle.
            with mock.patch.object(
                self.runtime.selection_content,
                "list_tree",
                side_effect=RealmIntegrityError("Immutable content changed."),
            ):
                handler.path = (
                    f"/api/content-views/{view['handle']}/tree?"
                    f"content_session_id={view['content_session_id']}"
                )
                handler.do_GET()
            self.assertEqual(responses[-1][1], HTTPStatus.CONFLICT)
            self.assertNotIn(view["handle"], self.state.selection_content_views)

    def test_artifact_capabilities_separate_view_from_tree_only_keep(self) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        template = detail["pages"]["candidate"]["items"][0]
        artifact_row = {
            **template,
            "kind": "artifact",
            "selection": {
                **template["selection"],
                "kind": "artifact",
                "entity_id": "artifact-test",
            },
        }
        blob_facts = mock.Mock()
        blob_facts.content.eligibility = SelectionEligibility.ready()
        blob_facts.content.root.content_ref = BlobRef("b" * 64)
        blob_facts.tree.eligibility = SelectionEligibility.unsupported(
            "file_artifact_not_tree",
            "A retained file artifact is not an editable workspace tree.",
        )
        blob_actions = {
            item["action"]: item
            for item in _row_workbench_action_capabilities(
                self.state,
                run_id=self.run_id,
                row=artifact_row,
                capability_facts=blob_facts,
            )
        }
        self.assertTrue(blob_actions["open_read_only"]["eligible"])
        self.assertTrue(blob_actions["open_read_only"]["supported"])
        self.assertEqual(blob_actions["open_read_only"]["content_kind"], "blob")
        self.assertFalse(blob_actions["inspect"]["supported"])
        self.assertFalse(blob_actions["debug_run"]["supported"])
        self.assertFalse(blob_actions["environment_preview"]["supported"])
        self.assertFalse(blob_actions["keep_editable"]["supported"])
        self.assertFalse(blob_actions["keep_editable"]["eligible"])
        self.assertEqual(
            blob_actions["keep_editable"]["reason"], "file_artifact_not_tree"
        )
        self.assertFalse(blob_actions["debug_run"]["eligible"])
        self.assertEqual(
            blob_actions["debug_run"]["reason"], "candidate_selection_required"
        )

        tree_facts = mock.Mock()
        tree_facts.content.eligibility = SelectionEligibility.ready()
        tree_facts.content.root.content_ref = SnapshotRef("c" * 64)
        tree_facts.tree.eligibility = SelectionEligibility.ready()
        tree_actions = {
            item["action"]: item
            for item in _row_workbench_action_capabilities(
                self.state,
                run_id=self.run_id,
                row=artifact_row,
                capability_facts=tree_facts,
            )
        }
        self.assertTrue(tree_actions["open_read_only"]["eligible"])
        self.assertTrue(tree_actions["open_read_only"]["supported"])
        self.assertTrue(tree_actions["keep_editable"]["eligible"])
        self.assertTrue(tree_actions["keep_editable"]["supported"])
        self.assertFalse(tree_actions["inspect"]["supported"])
        self.assertFalse(tree_actions["debug_run"]["supported"])
        self.assertFalse(tree_actions["environment_preview"]["supported"])
        self.assertFalse(tree_actions["environment_preview"]["eligible"])

    def test_file_candidate_debug_capability_compiles_exact_candidate_input(
        self,
    ) -> None:
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"schema": "test-file-candidate"},
            content_refs=(SnapshotRef("a" * 64),),
        )
        target = mock.Mock()
        target.candidate.admission.envelope = envelope
        target.compile_evaluation_spec.return_value = mock.sentinel.evaluation_spec
        target.evaluation.closure.evaluation_template.to_dict.return_value = {
            "default_seed": None,
        }
        target.run_definition = mock.sentinel.run_definition
        target.candidate_available = True
        target.evaluation.availability = "available"
        target.runnable = True

        with mock.patch(
            "optpilot_studio.ui.server.compile_retained_process_attempt_runtime",
            return_value=mock.sentinel.portable_spec,
        ) as compile_runtime:
            supported, reason = _candidate_debug_runtime_capability(
                self.state,
                target=target,
            )

            template = _realm_run_detail(
                self.state,
                ref=RunViewRef(run_id=self.run_id),
            )["pages"]["candidate"]["items"][0]
            file_row = {
                **template,
                "data": {**template["data"], "format": "files"},
            }
            file_facts = mock.Mock()
            file_facts.candidate_target = target
            file_facts.content.eligibility = SelectionEligibility.ready()
            file_facts.content.root.content_ref = SnapshotRef("a" * 64)
            file_facts.tree.eligibility = SelectionEligibility.ready()
            actions = {
                item["action"]: item
                for item in _row_workbench_action_capabilities(
                    self.state,
                    run_id=self.run_id,
                    row=file_row,
                    capability_facts=file_facts,
                )
            }

        self.assertTrue(supported)
        self.assertIsNone(reason)
        candidate_input = compile_runtime.call_args.kwargs["candidate_input"]
        self.assertEqual(candidate_input.candidate_format, "files")
        self.assertEqual(candidate_input.snapshot_ref, SnapshotRef("a" * 64))
        self.assertTrue(actions["inspect"]["eligible"])
        self.assertTrue(actions["open_read_only"]["eligible"])
        self.assertTrue(actions["keep_editable"]["eligible"])
        self.assertTrue(actions["debug_run"]["eligible"])
        self.assertTrue(actions["compare"]["supported"])
        self.assertTrue(actions["compare"]["eligible"])
        self.assertIsNone(actions["compare"]["reason"])

        opaque_actions = {
            item["action"]: item
            for item in _row_workbench_action_capabilities(
                self.state,
                run_id=self.run_id,
                row={
                    **file_row,
                    "data": {**file_row["data"], "format": "opaque"},
                },
                capability_facts=file_facts,
            )
        }
        self.assertTrue(opaque_actions["compare"]["supported"])
        self.assertTrue(opaque_actions["compare"]["eligible"])
        self.assertIsNone(opaque_actions["compare"]["reason"])

    def test_file_candidate_batch_separates_delegated_open_and_keep_authority(
        self,
    ) -> None:
        run_id = self._create_file_candidate_run()
        owner_detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        owner_row = owner_detail["pages"]["candidate"]["items"][0]
        owner_actions = {item["action"]: item for item in owner_row["eligibility"]}
        self.assertTrue(owner_actions["inspect"]["eligible"])
        self.assertTrue(owner_actions["open_read_only"]["eligible"])
        self.assertTrue(owner_actions["keep_editable"]["eligible"])

        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=run_id,
        )
        principals = {}
        for name in ("bytes", "derive"):
            principal_id = f"studio-file-{name}-delegate"
            principals[name] = self.runtime.ledger.register_principal(
                operation_id=f"studio-file-capability/principal/{name}",
                principal_id=principal_id,
                kind="human",
            )
            self.runtime.ledger.grant_owner_permission(
                operation_id=f"studio-file-capability/grant/{name}/metadata",
                actor_principal_id=self.runtime.actor_principal_id,
                owner_id=snapshot.run.owner_id,
                principal_id=principal_id,
                permission=OwnerPermission.METADATA_READ,
            )
            self.runtime.ledger.grant_owner_permission(
                operation_id=f"studio-file-capability/grant/{name}/{name}",
                actor_principal_id=self.runtime.actor_principal_id,
                owner_id=snapshot.run.owner_id,
                principal_id=principal_id,
                permission=(
                    OwnerPermission.BYTES_READ
                    if name == "bytes"
                    else OwnerPermission.DERIVE
                ),
            )

        resolved = {}
        for name, principal in principals.items():
            service = RealmRunViewService(self.runtime.ledger, principal)
            bundle = service.workbench_bundle(
                ref=RunViewRef(run_id=run_id),
                limit=1,
            )
            presentation = bundle.pages["candidate"]["items"][0]["selection"]
            resolved[name] = service.workbench_capability_batch(
                ref=RunViewRef(run_id=run_id),
                presentation_selections=(presentation,),
                bundle=bundle,
            )[presentation["selection_id"]]

        self.assertTrue(resolved["bytes"].content.eligibility.eligible)
        self.assertFalse(resolved["bytes"].tree.eligibility.eligible)
        self.assertEqual(
            resolved["bytes"].tree.eligibility.code,
            "selection_content_unavailable",
        )
        self.assertIsNone(resolved["bytes"].candidate_target)

        self.assertFalse(resolved["derive"].content.eligibility.eligible)
        self.assertEqual(
            resolved["derive"].content.eligibility.code,
            "selection_content_unavailable",
        )
        self.assertTrue(resolved["derive"].tree.eligibility.eligible)
        self.assertIsNotNone(resolved["derive"].candidate_target)

    def test_candidate_edit_reopens_one_source_linked_workspace(self) -> None:
        run_id = self._create_file_candidate_run()
        first_detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        first_candidate = first_detail["pages"]["candidate"]["items"][0]
        first_capability = {
            item["action"]: item for item in first_candidate["eligibility"]
        }["keep_editable"]
        self.assertTrue(first_capability["eligible"])
        self.assertEqual(first_capability["workspace_state"], "not-created")
        self.assertIsNone(first_capability["workspace_id"])

        first, first_status = _execute_run_workbench_action(
            self.state,
            run_id=run_id,
            payload={
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "10101010-1010-4010-8010-101010101010",
                "action": "keep_editable",
                "presentation_selection": first_candidate["selection"],
                "parameters": {},
            },
        )
        self.assertEqual(first_status, HTTPStatus.CREATED)
        workspace_id = first["workspace"]["id"]
        self.assertEqual(
            len(self.runtime.editable_workspaces.list_workspaces()),
            1,
        )

        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=run_id,
        )
        trial = snapshot.logical_trials[0]
        lease = snapshot.controller_lease
        self.runtime.ledger.cancel_run_logical_trial(
            operation_id="studio-file-capability/advance-head",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=run_id,
            logical_trial_id=trial.admission.logical_trial_id,
            expected_run_revision=snapshot.revision.revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            code="test_head_advance",
        )

        refreshed = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        refreshed_candidate = next(
            item
            for item in refreshed["pages"]["candidate"]["items"]
            if item["id"] == first_candidate["id"]
        )
        refreshed_capability = {
            item["action"]: item for item in refreshed_candidate["eligibility"]
        }["keep_editable"]
        self.assertNotEqual(
            first_candidate["selection"]["selection_id"],
            refreshed_candidate["selection"]["selection_id"],
        )
        self.assertEqual(refreshed_capability["workspace_state"], "created")
        self.assertEqual(refreshed_capability["workspace_id"], workspace_id)
        self.assertEqual(
            refreshed_capability["workspace_title"],
            first["workspace"]["title"],
        )

        replayed, replayed_status = _execute_run_workbench_action(
            self.state,
            run_id=run_id,
            payload={
                "schema": "optpilot.run-workbench-action-request.v1",
                "request_id": "20202020-2020-4020-8020-202020202020",
                "action": "keep_editable",
                "presentation_selection": refreshed_candidate["selection"],
                "parameters": {},
            },
        )
        self.assertEqual(replayed_status, HTTPStatus.OK)
        self.assertEqual(replayed["request_id"], "20202020-2020-4020-8020-202020202020")
        self.assertEqual(replayed["workspace"]["id"], workspace_id)
        self.assertEqual(
            len(self.runtime.editable_workspaces.list_workspaces()),
            1,
        )
        actions = self.state.coordination.list_actions(
            actor_id=self.runtime.actor_principal_id,
            action_kind="workspace-creation",
        )
        self.assertEqual(len(actions), 1)
        self.assertTrue(
            actions[0].intent_id.startswith("run-selection-workspace-")
        )
        self.assertNotIn(
            actions[0].intent_id,
            {
                "10101010-1010-4010-8010-101010101010",
                "20202020-2020-4020-8020-202020202020",
            },
        )

        store_type = type(self.state.coordination)
        database_path = self.state.coordination.database_path
        self.state.coordination.close()
        self.state.coordination = store_type(database_path)
        reopened = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        reopened_candidate = next(
            item
            for item in reopened["pages"]["candidate"]["items"]
            if item["id"] == first_candidate["id"]
        )
        reopened_capability = {
            item["action"]: item for item in reopened_candidate["eligibility"]
        }["keep_editable"]
        self.assertEqual(reopened_capability["workspace_state"], "created")
        self.assertEqual(reopened_capability["workspace_id"], workspace_id)

    def test_candidate_workspace_retry_reconciles_interrupted_receipt(self) -> None:
        run_id = self._create_file_candidate_run()
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        candidate = detail["pages"]["candidate"]["items"][0]
        first_request = {
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "30303030-3030-4030-8030-303030303030",
            "action": "keep_editable",
            "presentation_selection": candidate["selection"],
            "parameters": {},
        }
        with mock.patch.object(
            self.state.coordination,
            "complete_action",
            side_effect=RuntimeError("injected Candidate receipt interruption"),
        ):
            with self.assertRaisesRegex(RuntimeError, "receipt interruption"):
                _execute_run_workbench_action(
                    self.state,
                    run_id=run_id,
                    payload=first_request,
                )

        created = self.runtime.editable_workspaces.list_workspaces()
        self.assertEqual(len(created), 1)
        workspace_id = created[0].workspace_id
        actions = self.state.coordination.list_actions(
            actor_id=self.runtime.actor_principal_id,
            action_kind="workspace-creation",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].state.value, "uncertain")

        retried, retried_status = _execute_run_workbench_action(
            self.state,
            run_id=run_id,
            payload={
                **first_request,
                "request_id": "40404040-4040-4040-8040-404040404040",
            },
        )
        self.assertEqual(retried_status, HTTPStatus.CREATED)
        self.assertEqual(retried["workspace"]["id"], workspace_id)
        self.assertEqual(
            len(self.runtime.editable_workspaces.list_workspaces()),
            1,
        )
        actions = self.state.coordination.list_actions(
            actor_id=self.runtime.actor_principal_id,
            action_kind="workspace-creation",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].state.value, "succeeded")

    def test_debug_run_operator_job_api_is_durable_bounded_and_stoppable(self) -> None:
        run_id = self._create_runnable_operator_run()
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=run_id),
        )
        selection = detail["pages"]["candidate"]["items"][0]["selection"]
        capabilities = {
            item["action"]: item
            for item in detail["pages"]["candidate"]["items"][0]["eligibility"]
        }
        self.assertTrue(capabilities["debug_run"]["eligible"])
        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "44444444-4444-4444-8444-444444444444",
            "action": "debug_run",
            "presentation_selection": selection,
            "parameters": {},
        }

        scheduling_order = []

        def schedule(_state, *, job_id):
            scheduling_order.append((responses[-1][1], job_id))
            return True

        with mock.patch(
            "optpilot_studio.ui.server._schedule_operator_job_execution",
            side_effect=schedule,
        ):
            handler.do_POST()

        launched, launched_status = responses[-1]
        self.assertEqual(launched_status, HTTPStatus.ACCEPTED)
        job = launched["job"]
        self.assertEqual(scheduling_order, [(HTTPStatus.ACCEPTED, job["job_id"])])
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["cleanup_state"], "not_required")
        self.assertTrue(job["can_stop"])
        self.assertEqual(
            job["execution_policy"],
            {"network_policy": "denied", "network_enforcement": "advisory"},
        )
        inspection_plan = job["inspection_plan"]
        self.assertEqual(
            inspection_plan["schema"], "optpilot.candidate-try-plan.v1"
        )
        self.assertEqual(inspection_plan["mode"], "try_once")
        self.assertEqual(
            inspection_plan["candidate_id"], job["target"]["candidate_id"]
        )
        self.assertTrue(inspection_plan["environment"]["id"])
        self.assertTrue(inspection_plan["environment"]["revision"])
        self.assertEqual(
            inspection_plan,
            capabilities["debug_run"]["inspection_plan"],
        )
        self.assertEqual(inspection_plan["settings"]["seed"], 7)
        self.assertEqual(
            inspection_plan["settings"]["repetition_index"], 0
        )
        self.assertIsNone(
            inspection_plan["settings"]["interface_profile_id"]
        )
        encoded = json.dumps(job, sort_keys=True)
        for forbidden in (
            "owner_id",
            "principal_id",
            "lease_id",
            "binding_id",
            "launch_token",
            "content_ref",
            "backend_kind",
            "backend_realm",
            str(self.root),
        ):
            self.assertNotIn(forbidden, encoded)

        handler._handle_run_get(
            f"/api/runs/{run_id}/operator-jobs",
            {"limit": ["1"]},
        )
        listing, listing_status = responses[-1]
        self.assertEqual(listing_status, HTTPStatus.OK)
        self.assertEqual(listing["schema"], "optpilot.operator-job-list.v1")
        self.assertEqual(listing["jobs"], [job])

        handler.path = f"/api/operator-jobs/{job['job_id']}"
        handler.do_GET()
        fetched, fetched_status = responses[-1]
        self.assertEqual(fetched_status, HTTPStatus.OK)
        self.assertEqual(fetched["job"], job)

        handler.path = f"/api/operator-jobs/{job['job_id']}/stop"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-stop-request.v1",
            "request_id": "55555555-5555-4555-8555-555555555555",
        }
        handler.do_POST()
        stopped, stopped_status = responses[-1]
        self.assertEqual(stopped_status, HTTPStatus.OK)
        self.assertEqual(stopped["job"]["state"], "cancelled")
        self.assertEqual(stopped["job"]["cleanup_state"], "complete")
        self.assertFalse(stopped["job"]["can_stop"])
        self.assertTrue(stopped["job"]["stop_requested"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-stop-request.v1",
            "request_id": "66666666-6666-4666-8666-666666666666",
            "reason_code": "untrusted",
        }
        handler.do_POST()
        rejected, rejected_status = responses[-1]
        self.assertEqual(rejected_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("reason_code", rejected["error"])

    def test_environment_preview_uses_retained_profile_and_revocable_presentation(
        self,
    ) -> None:
        run_id = self._create_runnable_operator_run(
            environment_interface=_STUDIO_PREVIEW_INTERFACE
        )
        engine = self._enable_fake_environment_preview()
        self.addCleanup(self.state.stop_workspace_preview_proxies)
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        candidate = detail["pages"]["candidate"]["items"][0]
        capability = next(
            item
            for item in candidate["eligibility"]
            if item["action"] == "environment_preview"
        )
        self.assertTrue(capability["supported"])
        self.assertTrue(capability["eligible"])
        self.assertEqual(capability["selected_profile_id"], "default")
        self.assertEqual([item["id"] for item in capability["profiles"]], ["default"])

        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "88888888-8888-4888-8888-888888888888",
            "action": "environment_preview",
            "presentation_selection": candidate["selection"],
            "parameters": {"profile_id": "default"},
        }
        handler.do_POST()
        launched, launched_status = responses[-1]
        self.assertEqual(launched_status, HTTPStatus.ACCEPTED)
        job_id = launched["job"]["job_id"]
        self.assertEqual(launched["job"]["job_kind"], "environment-preview")
        self.assertEqual(launched["job"]["presentation"]["status"], "pending")

        deadline = time.time() + 10
        while time.time() < deadline:
            current = self.runtime.operator_jobs.read(job_id=job_id)
            if current.state is OperatorJobState.RUNNING:
                break
            if current.state.terminal:
                break
            # Do not turn the observer into a high-frequency SQLite reader
            # that starves the background ledger writer on slower filesystems.
            time.sleep(0.2)
        self.assertEqual(
            self.runtime.operator_jobs.read(job_id=job_id).state,
            OperatorJobState.RUNNING,
        )

        handler.path = f"/api/operator-jobs/{job_id}"
        handler.do_GET()
        presented, presented_status = responses[-1]
        self.assertEqual(presented_status, HTTPStatus.OK)
        public_job = presented["job"]
        if public_job["presentation"] == {
            "status": "reconciling",
            "reason": "presentation_origin_unavailable",
        }:
            # Some managed test sandboxes deny loopback listen sockets.  The
            # broker's socket/auth/revocation contract is covered separately;
            # this integration still proves the durable Preview reached
            # RUNNING, reports capacity without a 500, and cleans up exactly.
            handler.path = f"/api/operator-jobs/{job_id}/stop"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.operator-job-stop-request.v1",
                "request_id": "77777777-7777-4777-8777-777777777777",
            }
            handler.do_POST()
            stopped, stopped_status = responses[-1]
            self.assertEqual(stopped_status, HTTPStatus.OK)
            self.assertEqual(stopped["job"]["cleanup_state"], "complete")
            self.assertEqual(engine.containers, {})
            self.assertEqual(engine.networks, {})
            return
        self.assertEqual(public_job["presentation"]["status"], "available")
        self.assertTrue(public_job["presentation"]["open_url"].startswith("http://"))
        self.assertEqual(public_job["target"]["interface_profile_id"], "default")
        self.assertEqual(
            public_job["inspection_plan"]["schema"],
            "optpilot.candidate-try-plan.v1",
        )
        self.assertEqual(
            public_job["inspection_plan"]["mode"], "try_interactively"
        )
        self.assertEqual(
            public_job["inspection_plan"]["settings"]["interface_profile_id"],
            "default",
        )
        self.assertTrue(
            public_job["inspection_plan"]["environment"]["revision"]
        )
        serialized = json.dumps(public_job, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("authorization", serialized.casefold())
        self.assertNotIn("launch_token", serialized)

        handler.path = f"/api/operator-jobs/{job_id}/stop"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-stop-request.v1",
            "request_id": "99999999-9999-4999-8999-999999999999",
        }
        handler.do_POST()
        stopped, stopped_status = responses[-1]
        self.assertEqual(stopped_status, HTTPStatus.OK)
        self.assertEqual(stopped["job"]["state"], "cancelled")
        self.assertEqual(stopped["job"]["cleanup_state"], "complete")
        self.assertEqual(stopped["job"]["presentation"]["status"], "closed")
        self.assertEqual(engine.containers, {})
        self.assertEqual(engine.networks, {})
        self.assertEqual(self.state.presentation_broker._leases, {})

    def test_environment_preview_ignores_process_profile_and_reports_untrusted_image(
        self,
    ) -> None:
        candidate_profile = yaml.safe_load(_STUDIO_PREVIEW_INTERFACE)["interface"]
        process_profile = {
            **candidate_profile,
            "label": "Catalog interface",
            "runtime": {"sandbox": "process"},
        }
        interface = yaml.safe_dump(
            {
                "interface": {
                    "launchProfiles": [
                        {"id": "default", **process_profile},
                        {"id": "candidate", **candidate_profile},
                    ]
                }
            },
            sort_keys=False,
        )
        run_id = self._create_runnable_operator_run(
            environment_interface=interface
        )
        self._enable_fake_environment_preview(trusted_images=())

        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        candidate = detail["pages"]["candidate"]["items"][0]
        capability = next(
            item
            for item in candidate["eligibility"]
            if item["action"] == "environment_preview"
        )

        self.assertTrue(capability["supported"])
        self.assertFalse(capability["eligible"])
        self.assertEqual(
            capability["reason"], "environment_preview_image_untrusted"
        )
        self.assertEqual(capability["profiles"], [])
        diagnostics = {
            item["id"]: item for item in capability["profile_diagnostics"]
        }
        self.assertFalse(diagnostics["default"]["applicable"])
        self.assertEqual(
            diagnostics["default"]["eligibility_detail"]["category"],
            "not_applicable",
        )
        self.assertEqual(
            diagnostics["default"]["eligibility_detail"]["code"],
            "profile_process_runtime_not_applicable",
        )
        self.assertTrue(diagnostics["candidate"]["applicable"])
        self.assertEqual(
            diagnostics["candidate"]["remediation"],
            {
                "kind": "approve_container_gateway_image",
                "image_ref": _STUDIO_PREVIEW_IMAGE,
            },
        )
        self.assertEqual(
            capability["eligibility_detail"]["remediation"],
            diagnostics["candidate"]["remediation"],
        )
        self.assertIn(
            capability["eligibility_detail"].get("trust_source"),
            {"realm", "session"},
        )
        self.assertTrue(
            capability["eligibility_detail"].get("trust_generation")
        )

        self._enable_fake_environment_preview()
        refreshed = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        refreshed_candidate = refreshed["pages"]["candidate"]["items"][0]
        refreshed_capability = next(
            item
            for item in refreshed_candidate["eligibility"]
            if item["action"] == "environment_preview"
        )
        self.assertTrue(refreshed_capability["eligible"])
        self.assertEqual(refreshed_capability["selected_profile_id"], "candidate")
        self.assertEqual(
            [item["id"] for item in refreshed_capability["profiles"]],
            ["candidate"],
        )

    def test_environment_preview_reports_the_exact_selected_profile(self) -> None:
        base_profile = yaml.safe_load(_STUDIO_PREVIEW_INTERFACE)["interface"]
        interface = yaml.safe_dump(
            {
                "interface": {
                    "launchProfiles": [
                        {"id": "default", **base_profile},
                        {
                            "id": "secondary",
                            **base_profile,
                            "label": "Secondary Candidate Preview",
                        },
                    ],
                },
            },
            sort_keys=False,
        )
        run_id = self._create_runnable_operator_run(
            environment_interface=interface
        )
        self._enable_fake_environment_preview()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        candidate = detail["pages"]["candidate"]["items"][0]
        capability = next(
            item
            for item in candidate["eligibility"]
            if item["action"] == "environment_preview"
        )
        self.assertEqual(
            [item["id"] for item in capability["profiles"]],
            ["default", "secondary"],
        )
        self.assertEqual(
            capability["inspection_plan"]["settings"]["interface_profile_id"],
            "default",
        )

        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "34343434-3434-4434-8434-343434343434",
            "action": "environment_preview",
            "presentation_selection": candidate["selection"],
            "parameters": {"profile_id": "secondary"},
        }
        with mock.patch(
            "optpilot_studio.ui.server._schedule_operator_job_execution",
            return_value=True,
        ):
            handler.do_POST()

        launched, launched_status = responses[-1]
        self.assertEqual(launched_status, HTTPStatus.ACCEPTED)
        self.assertEqual(
            launched["job"]["inspection_plan"]["settings"][
                "interface_profile_id"
            ],
            "secondary",
        )
        self.assertIsNone(
            launched["job"]["inspection_plan"]["environment"]["id"]
        )
        self.assertEqual(
            launched["job"]["inspection_plan"]["environment"]["revision"],
            capability["inspection_plan"]["environment"]["revision"],
        )

    def test_environment_preview_outputs_are_live_then_keepable_from_terminal_job(
        self,
    ) -> None:
        run_id = self._create_runnable_operator_run(
            environment_interface=_STUDIO_OUTPUT_PREVIEW_INTERFACE
        )
        engine = self._enable_fake_environment_preview()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        candidate = detail["pages"]["candidate"]["items"][0]
        handler, responses = self._handler()
        handler.path = f"/api/runs/{run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "12121212-1212-4212-8212-121212121212",
            "action": "environment_preview",
            "presentation_selection": candidate["selection"],
            "parameters": {"profile_id": "default"},
        }
        handler.do_POST()
        launched, launched_status = responses[-1]
        self.assertEqual(launched_status, HTTPStatus.ACCEPTED)
        job_id = launched["job"]["job_id"]

        deadline = time.time() + 10
        managed = None
        while time.time() < deadline:
            current = self.runtime.operator_jobs.read(job_id=job_id)
            if current.state is OperatorJobState.RUNNING:
                with self.runtime.operator_jobs._preview_output_state_lock:
                    managed = self.runtime.operator_jobs._active_preview_bindings.get(
                        job_id
                    )
                if managed is not None:
                    break
            if current.state.terminal:
                break
            time.sleep(0.1)
        self.assertIsNotNone(managed)
        assert managed is not None
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "generated"
        generated.mkdir()
        (generated / "simulator.py").write_text(
            "print('studio preview output')\n", encoding="utf-8"
        )
        summary_file = descriptor.source_root / "summary.json"
        summary_file.write_text('{"ok": true}\n', encoding="utf-8")
        descriptor.control_file.write_text(
            json.dumps(
                {
                    "schema_version": "optpilot.interface.output.v1",
                    "id": "generated-simulator",
                    "label": "Generated simulator",
                    "kind": "tree",
                    "root": "output",
                    "path": "generated",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with descriptor.control_file.open("a", encoding="utf-8") as control:
            control.write(
                json.dumps(
                    {
                        "schema_version": "optpilot.interface.output.v1",
                        "id": "summary",
                        "label": "summary.json",
                        "kind": "file",
                        "root": "output",
                        "path": "summary.json",
                    }
                )
                + "\n"
            )

        deadline = time.time() + 10
        live_job = None
        while time.time() < deadline:
            handler.path = f"/api/operator-jobs/{job_id}"
            handler.do_GET()
            payload, status = responses[-1]
            self.assertEqual(status, HTTPStatus.OK)
            live_job = payload["job"]
            outputs = live_job["interface_outputs"]["outputs"]
            if len(outputs) == 2 and all(
                item["status"] == "ready" for item in outputs
            ):
                break
            time.sleep(0.1)
        self.assertIsNotNone(live_job)
        live_output = live_job["interface_outputs"]["outputs"][0]
        self.assertEqual(live_job["interface_outputs"]["lifecycle"], "live")
        self.assertFalse(live_output["actions"]["keep_as_workspace"]["eligible"])
        self.assertFalse(live_output["actions"]["view_read_only"]["eligible"])
        self.assertIsNone(live_output["selection"])
        public_json = json.dumps(live_job, sort_keys=True)
        self.assertNotIn(str(self.root), public_json)
        self.assertNotIn("content_ref", public_json)

        handler.path = (
            f"/api/operator-jobs/{job_id}/outputs/generated-simulator/view"
        )
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-output-content-view-request.v1",
            "content_session_id": None,
        }
        handler.do_POST()
        live_view_rejected, live_view_status = responses[-1]
        self.assertEqual(live_view_status, HTTPStatus.CONFLICT)
        self.assertIn("terminal Candidate try", live_view_rejected["error"])

        engine.role("app")["State"] = {"Running": False, "ExitCode": 0}
        deadline = time.time() + 10
        while time.time() < deadline:
            current = self.runtime.operator_jobs.read(job_id=job_id)
            if current.state.terminal and current.cleanup_state.value == "complete":
                break
            time.sleep(0.1)
        self.assertEqual(current.state, OperatorJobState.SUCCEEDED)

        handler.path = f"/api/operator-jobs/{job_id}"
        handler.do_GET()
        terminal_payload, terminal_status = responses[-1]
        self.assertEqual(terminal_status, HTTPStatus.OK)
        terminal_job = terminal_payload["job"]
        terminal_outputs = {
            item["id"]: item
            for item in terminal_job["interface_outputs"]["outputs"]
        }
        terminal_output = terminal_outputs["generated-simulator"]
        terminal_file = terminal_outputs["summary"]
        self.assertEqual(terminal_job["interface_outputs"]["lifecycle"], "retained")
        self.assertTrue(terminal_output["actions"]["view_read_only"]["eligible"])
        self.assertTrue(terminal_output["actions"]["keep_as_workspace"]["eligible"])
        self.assertTrue(terminal_file["actions"]["view_read_only"]["eligible"])
        self.assertFalse(terminal_file["actions"]["keep_as_workspace"]["supported"])
        self.assertFalse(terminal_output["actions"]["retry_capture"]["eligible"])
        self.assertIsNone(terminal_output["selection"])
        self.assertIsNone(terminal_file["selection"])

        view_path = (
            f"/api/operator-jobs/{job_id}/outputs/generated-simulator/view"
        )
        handler.path = view_path
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-output-content-view-request.v1",
            "content_session_id": None,
            "content_ref": "tree:sha256:" + "0" * 64,
        }
        handler.do_POST()
        rejected_view, rejected_view_status = responses[-1]
        self.assertEqual(rejected_view_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("fields differ", rejected_view["error"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-output-content-view-request.v1",
            "content_session_id": None,
        }
        handler.do_POST()
        viewed_tree, viewed_tree_status = responses[-1]
        self.assertEqual(viewed_tree_status, HTTPStatus.CREATED)
        tree_view = viewed_tree["content_view"]
        self.assertEqual(tree_view["schema"], "optpilot.selection-content-view.v1")
        self.assertEqual(tree_view["content_kind"], "tree")
        self.assertEqual(
            tree_view["selection"],
            {"kind": "artifact", "entity_id": "generated-simulator"},
        )
        self.assertIsNone(tree_view["head"]["sequence"])
        serialized_tree_view = json.dumps(viewed_tree, sort_keys=True)
        for forbidden in (
            "selection_digest",
            "content_ref",
            "store_id",
            "owner_id",
            str(self.root),
            str(self.runtime.root),
        ):
            self.assertNotIn(forbidden, serialized_tree_view)

        handler.path = (
            f"/api/content-views/{tree_view['handle']}/tree?"
            f"content_session_id={tree_view['content_session_id']}"
        )
        handler.do_GET()
        tree_page, tree_page_status = responses[-1]
        self.assertEqual(tree_page_status, HTTPStatus.OK)
        self.assertEqual(
            [
                item["relative_path"]
                for item in tree_page["entries"]
                if item["kind"] == "file"
            ],
            ["simulator.py"],
        )

        handler.path = f"/api/content-views/{tree_view['handle']}/close"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.selection-content-view-close-request.v1",
            "content_session_id": tree_view["content_session_id"],
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.OK)

        handler.path = f"/api/operator-jobs/{job_id}/outputs/summary/view"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-output-content-view-request.v1",
            "content_session_id": tree_view["content_session_id"],
        }
        handler.do_POST()
        viewed_file, viewed_file_status = responses[-1]
        self.assertEqual(viewed_file_status, HTTPStatus.CREATED)
        file_view = viewed_file["content_view"]
        self.assertEqual(file_view["content_kind"], "blob")
        self.assertEqual(
            file_view["content_session_id"], tree_view["content_session_id"]
        )
        self.assertEqual(
            file_view["selection"],
            {"kind": "artifact", "entity_id": "summary"},
        )

        handler.path = (
            f"/api/content-views/{file_view['handle']}/content?"
            f"content_session_id={file_view['content_session_id']}"
        )
        handler.do_GET()
        file_content, file_content_status = responses[-1]
        self.assertEqual(file_content_status, HTTPStatus.OK)
        self.assertEqual(file_content["media_type"], "application/json")
        self.assertEqual(file_content["encoding"], "utf-8")
        self.assertEqual(file_content["text"], '{"ok": true}\n')

        handler.path = f"/api/operator-jobs/{job_id}/outputs/missing/view"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.operator-job-output-content-view-request.v1",
            "content_session_id": None,
        }
        handler.do_POST()
        self.assertEqual(responses[-1][1], HTTPStatus.NOT_FOUND)

        handler.path = f"/api/operator-jobs/{job_id}/outputs/summary/keep"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "request_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        }
        handler.do_POST()
        file_keep_rejected, file_keep_status = responses[-1]
        self.assertEqual(file_keep_status, HTTPStatus.CONFLICT)
        self.assertIn(
            "operator_job_file_output_not_tree", file_keep_rejected["error"]
        )

        handler.path = f"/api/operator-jobs/{job_id}/outputs/generated-simulator/keep"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "content_ref": "client-authored"
        }
        handler.do_POST()
        rejected, rejected_status = responses[-1]
        self.assertEqual(rejected_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("fields differ", rejected["error"])

        keep_request = {
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        }
        handler._read_json_body = lambda: keep_request  # type: ignore[method-assign]
        handler.do_POST()
        kept, kept_status = responses[-1]
        self.assertEqual(kept_status, HTTPStatus.CREATED)
        self.assertEqual(kept["output"]["id"], "generated-simulator")
        self.assertEqual(kept["workspace"]["mode"], "editable")
        self.assertTrue(Path(kept["workspace"]["root"]).is_dir())

        handler.do_POST()
        replayed, replayed_status = responses[-1]
        self.assertEqual(replayed_status, HTTPStatus.CREATED)
        self.assertEqual(replayed["workspace"]["id"], kept["workspace"]["id"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        }
        handler.do_POST()
        independent, independent_status = responses[-1]
        self.assertEqual(independent_status, HTTPStatus.CREATED)
        self.assertNotEqual(
            independent["workspace"]["id"], kept["workspace"]["id"]
        )

    def test_operator_job_scheduler_retries_temporary_capacity_shortage(self) -> None:
        run_id = self._create_runnable_operator_run()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        selection = detail["pages"]["candidate"]["items"][0]["selection"]
        minted = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=run_id),
            presentation_selection=selection,
        )
        self.assertIsNotNone(minted.selection)
        record = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-operator/scheduler-capacity-retry",
            selection=minted.selection,
        )
        execute = self.runtime.operator_jobs.execute
        calls = 0

        def execute_after_shortage(*, job_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RealmCapacityUnavailable("temporarily full")
            return execute(job_id=job_id)

        with mock.patch.object(
            self.runtime.operator_jobs,
            "execute",
            side_effect=execute_after_shortage,
        ):
            self.assertTrue(
                _schedule_operator_job_execution(self.state, job_id=record.job_id)
            )
            thread = self.state._operator_job_threads[record.job_id]
            thread.join(timeout=15)

        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(calls, 2)
        terminal = self.runtime.operator_jobs.read(job_id=record.job_id)
        self.assertTrue(terminal.state.terminal)
        self.assertNotIn(record.job_id, self.state._operator_job_threads)

    def test_operator_job_scheduler_backs_off_while_stopping_capture_is_pending(
        self,
    ) -> None:
        run_id = self._create_runnable_operator_run()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        selection = detail["pages"]["candidate"]["items"][0]["selection"]
        minted = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=run_id),
            presentation_selection=selection,
        )
        self.assertIsNotNone(minted.selection)
        record = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-operator/scheduler-stopping-capture-pending",
            selection=minted.selection,
        )
        stopping = mock.Mock()
        stopping.state = OperatorJobState.STOPPING

        with (
            mock.patch.object(
                self.runtime.operator_jobs,
                "read",
                return_value=stopping,
            ),
            mock.patch.object(
                self.runtime.operator_jobs,
                "execute",
                side_effect=[
                    EnvironmentPreviewFinalCapturePending("capture pending"),
                    None,
                ],
            ) as execute,
            mock.patch.object(
                self.state._background_execution_closing,
                "wait",
                return_value=False,
            ) as wait_for_close,
        ):
            self.assertTrue(
                _schedule_operator_job_execution(self.state, job_id=record.job_id)
            )
            thread = self.state._operator_job_threads[record.job_id]
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(execute.call_count, 2)
        wait_for_close.assert_called_once_with(0.25)
        self.assertNotIn(record.job_id, self.state._operator_job_threads)

    def test_startup_operator_job_reconciliation_paginates_past_100_runs(
        self,
    ) -> None:
        run_id = self._create_runnable_operator_run()
        detail = _realm_run_detail(self.state, ref=RunViewRef(run_id=run_id))
        selection = detail["pages"]["candidate"]["items"][0]["selection"]
        minted = self.runtime.run_views.mint_selection(
            ref=RunViewRef(run_id=run_id),
            presentation_selection=selection,
        )
        self.assertIsNotNone(minted.selection)
        record = self.runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="studio-operator/startup-pagination",
            selection=minted.selection,
        )
        self.assertEqual(record.state, OperatorJobState.QUEUED)
        self.assertEqual(record.plan.target.selection.source_id, run_id)

        first_page = mock.Mock(
            items=tuple(
                mock.Mock(run_id=f"newer-run-{index:03d}") for index in range(100)
            ),
            next_page_token="older-runs-page",
        )
        second_page = mock.Mock(
            items=(mock.Mock(run_id=run_id),),
            next_page_token=None,
        )
        run_reader = mock.Mock()
        run_reader.list_runs.side_effect = (first_page, second_page)

        with (
            mock.patch.object(self.runtime, "run_reader", run_reader),
            mock.patch(
                "optpilot_studio.ui.server._schedule_operator_job_execution",
                return_value=True,
            ) as schedule,
        ):
            _reconcile_visible_operator_jobs(self.state)

        self.assertEqual(
            run_reader.list_runs.call_args_list,
            [
                mock.call(page_token=None, limit=100),
                mock.call(page_token="older-runs-page", limit=100),
            ],
        )
        schedule.assert_called_once_with(self.state, job_id=record.job_id)

    def test_run_action_rejects_a_stale_presentation_head(self) -> None:
        detail = _realm_run_detail(
            self.state,
            ref=RunViewRef(run_id=self.run_id),
        )
        stale_selection = detail["pages"]["candidate"]["items"][0]["selection"]
        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id="studio-realm/run/stale-action/begin",
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=snapshot.run.owner_id,
            expected_owner_revision=snapshot.revision.owner_revision,
            ttl_seconds=120,
        )
        lease = snapshot.controller_lease
        self.runtime.ledger.commit_run_candidate_admissions(
            operation_id="studio-realm/run/stale-action/commit",
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=snapshot.revision.revision,
            expected_owner_revision=snapshot.revision.owner_revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        candidate_id="candidate-new-head",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="parameters",
                            spec={"x": 99},
                        ),
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        logical_trial_id="trial-new-head",
                        candidate_id="candidate-new-head",
                    ),
                ),
            ),
        )

        handler, responses = self._handler()
        handler.path = f"/api/runs/{self.run_id}/actions"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.run-workbench-action-request.v1",
            "request_id": "77777777-7777-4777-8777-777777777777",
            "action": "inspect",
            "presentation_selection": stale_selection,
            "parameters": {},
        }
        handler.do_POST()
        rejected, status = responses[-1]
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(rejected["type"], "RealmConflict")
        self.assertIn("head changed", rejected["error"])

    def test_run_workspace_adapter_and_raw_file_route_are_disabled(
        self,
    ) -> None:
        before_counts = self._realm_counts()
        before_files = {
            path.relative_to(self.state.workspaces_dir)
            for path in self.state.workspaces_dir.rglob("*")
        }
        handler, responses = self._handler()
        handler._read_json_body = lambda: {}  # type: ignore[method-assign]

        handler.path = f"/api/runs/{self.run_id}/open-workspace"
        handler.do_POST()

        disabled_workspace, workspace_status = responses[-1]
        self.assertEqual(workspace_status, HTTPStatus.NOT_FOUND)
        self.assertEqual(disabled_workspace["error"], "Unknown run action")
        self.assertEqual(self._realm_counts(), before_counts)
        self.assertEqual(
            {
                path.relative_to(self.state.workspaces_dir)
                for path in self.state.workspaces_dir.rglob("*")
            },
            before_files,
        )

        handler._handle_run_get(
            f"/api/runs/{self.run_id}/file",
            {"path": ["summary.json"]},
        )
        disabled, disabled_status = responses[-1]
        self.assertEqual(disabled_status, HTTPStatus.NOT_FOUND)
        self.assertIn("unavailable", disabled["error"])
        self.assertNotIn("content", disabled)

        handler.path = "/api/studies/launch"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": "42345678-1234-4234-8234-123456789abc",
            "study_path": str(self.study_path),
            "output_root": str(self.root / "untrusted-output"),
        }
        handler.do_POST()
        rejected, rejected_status = responses[-1]
        self.assertEqual(rejected_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("extra", rejected["error"])

    def test_managed_workspace_lists_reopens_commits_and_retires_without_touching_source(
        self,
    ) -> None:
        created = self._create_managed_workspace()
        source_owner_before = self.runtime.ledger.read_owner(
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=self.created.run.owner_id,
        )
        source_memberships_before = self.runtime.ledger.list_owner_memberships(
            actor_principal_id=self.runtime.actor_principal_id,
            owner_id=self.created.run.owner_id,
        )
        handler, responses = self._handler()

        handler.path = "/api/workspaces"
        handler.do_GET()
        listed, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        dormant = next(
            item
            for item in listed["workspaces"]
            if item["id"] == created.workspace.workspace_id
        )
        self.assertTrue(dormant["reopen_required"])
        self.assertEqual(dormant["realization_state"], "closed")
        self.assertEqual(dormant["root"], "")
        self.assertEqual(dormant["realm_workspace_revision"], 1)

        handler.path = f"/api/workspaces/{created.workspace.workspace_id}/reopen"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "expected_workspace_revision": 1
        }
        handler.do_POST()
        reopened_payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        reopened = reopened_payload["workspace"]
        checkout_root = Path(reopened["root"])
        self.assertFalse(reopened["reopen_required"])
        self.assertTrue(checkout_root.is_dir())
        self.assertTrue(
            checkout_root.is_relative_to(self.runtime.editable_workspaces.checkout_root)
        )

        (checkout_root / "studio-note.txt").write_text(
            "retained edit\n", encoding="utf-8"
        )
        handler.path = f"/api/workspaces/{created.workspace.workspace_id}/commit"
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "expected_workspace_revision": 1
        }
        handler.do_POST()
        committed_payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(committed_payload["commit"]["status"], "committed")
        self.assertEqual(committed_payload["commit"]["current_revision"], 2)
        self.assertEqual(committed_payload["workspace"]["realm_workspace_revision"], 2)

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "expected_workspace_revision": 1
        }
        handler.do_POST()
        stale, status = responses[-1]
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(stale["type"], "RealmConflict")

        handler.path = f"/api/workspaces/{created.workspace.workspace_id}"
        handler.do_DELETE()
        deleted_payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        deleted = deleted_payload["workspace"]
        self.assertTrue(deleted["workspace_retired"])
        self.assertTrue(deleted["checkout_absent"])
        self.assertTrue(deleted["source_content_unchanged"])
        self.assertFalse(checkout_root.exists())
        retired_workspace, _revision = self.runtime.ledger.read_workspace(
            actor_principal_id=self.runtime.actor_principal_id,
            workspace_id=created.workspace.workspace_id,
        )
        self.assertIs(retired_workspace.state, WorkspaceState.DELETED)
        self.assertEqual(
            self.runtime.ledger.read_owner(
                actor_principal_id=self.runtime.actor_principal_id,
                owner_id=self.created.run.owner_id,
            ),
            source_owner_before,
        )
        self.assertEqual(
            self.runtime.ledger.list_owner_memberships(
                actor_principal_id=self.runtime.actor_principal_id,
                owner_id=self.created.run.owner_id,
            ),
            source_memberships_before,
        )

        handler.path = "/api/workspaces"
        handler.do_GET()
        listed_after, status = responses[-1]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertNotIn(
            created.workspace.workspace_id,
            {item["id"] for item in listed_after["workspaces"]},
        )

    def test_run_ui_owns_exactly_one_injected_realm_runtime_lifecycle(self) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "production-realm"
        fake_runtime.closed = False
        fake_server = mock.Mock()
        fake_server.server_port = 8765
        configured_root = self.root / "configured-by-environment"

        with (
            mock.patch(
                "optpilot_studio.ui.server.default_realm_root",
                return_value=configured_root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ) as open_runtime,
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer",
                return_value=fake_server,
            ),
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
        ):
            run_ui(
                catalog_roots=[str(self.package)],
                run_roots=[],
            )

        open_runtime.assert_called_once_with(
            realm_root=configured_root.resolve(),
            container_web_executable=None,
            trusted_container_gateway_images=(),
        )
        fake_server.serve_forever.assert_called_once_with()
        fake_server.server_close.assert_called_once_with()
        self.assertIs(fake_server.daemon_threads, False)
        fake_runtime.close.assert_called_once_with()

    def test_busy_runtime_supervisor_prevents_realm_and_server_construction(
        self,
    ) -> None:
        realm_root = self.root / "busy-supervisor-realm"
        claim = StudioRuntimeSupervisorClaim.acquire(
            self.root,
            control_root=studio_project_state_directory(
                self.root,
                authority_root=realm_root,
            ),
        )
        try:
            with (
                mock.patch(
                    "optpilot_studio.ui.server.Path.cwd",
                    return_value=self.root,
                ),
                mock.patch(
                    "optpilot_studio.ui.server.default_realm_root",
                    return_value=realm_root,
                ),
                mock.patch(
                    "optpilot_studio.ui.server.LocalRealmRuntime.open"
                ) as open_runtime,
                mock.patch(
                    "optpilot_studio.ui.server.UiState"
                ) as state_constructor,
                mock.patch(
                    "optpilot_studio.ui.server.ThreadingHTTPServer"
                ) as server_constructor,
            ):
                with self.assertRaises(StudioRuntimeSupervisorBusy):
                    run_ui(catalog_roots=[], run_roots=[])
        finally:
            claim.close()

        open_runtime.assert_not_called()
        state_constructor.assert_not_called()
        server_constructor.assert_not_called()

    def test_run_ui_rejects_unsafe_realm_root_before_creating_claims(self) -> None:
        unsafe_target = self.root / "unsafe-realm-target"
        unsafe_target.mkdir()
        unsafe_root = self.root / "unsafe-realm-link"
        unsafe_root.symlink_to(unsafe_target, target_is_directory=True)
        with (
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.default_realm_root",
                return_value=unsafe_root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open"
            ) as open_runtime,
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer"
            ) as server_constructor,
        ):
            with self.assertRaises(RealmIntegrityError):
                run_ui(catalog_roots=[], run_roots=[])

        open_runtime.assert_not_called()
        server_constructor.assert_not_called()
        self.assertFalse(
            (self.root / ".optpilot-ui" / "runtime-supervisor.lock").exists()
        )
        self.assertFalse((unsafe_target / "studio").exists())

    def test_run_ui_releases_supervisor_when_realm_open_fails(self) -> None:
        fake_claim = mock.Mock()
        with (
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                return_value=fake_claim,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                side_effect=RuntimeError("injected Realm open failure"),
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState"
            ) as state_constructor,
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer"
            ) as server_constructor,
        ):
            with self.assertRaisesRegex(RuntimeError, "Realm open failure"):
                run_ui(catalog_roots=[], run_roots=[])

        fake_claim.close.assert_called_once_with()
        state_constructor.assert_not_called()
        server_constructor.assert_not_called()

    def test_run_ui_closes_realm_and_supervisor_when_state_construction_fails(
        self,
    ) -> None:
        fake_claim = mock.Mock()
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "state-construction-failure-realm"
        with (
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                return_value=fake_claim,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState",
                side_effect=RuntimeError("injected UiState construction failure"),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer"
            ) as server_constructor,
        ):
            with self.assertRaisesRegex(RuntimeError, "UiState construction failure"):
                run_ui(catalog_roots=[], run_roots=[])

        fake_runtime.close.assert_called_once_with()
        fake_claim.close.assert_called_once_with()
        server_constructor.assert_not_called()

    def test_run_ui_closes_listener_and_handlers_before_studio_state(self) -> None:
        events = []
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "shutdown-order-realm"
        fake_runtime.close.side_effect = lambda: events.append("realm-close")
        fake_state = mock.Mock()
        fake_state.stop_transient_interface_launches.side_effect = lambda: (
            events.append("stop-interfaces") or True
        )
        fake_state.stop_workspace_preview_proxies.side_effect = lambda: (
            events.append("stop-previews") or True
        )
        fake_state.close_catalog_projections.side_effect = lambda: events.append(
            "close-projections"
        )

        def close_coordination(*, close_storage):
            self.assertTrue(close_storage)
            events.append("close-coordination")
            return True

        fake_state.close_coordination.side_effect = close_coordination
        fake_server = mock.Mock()
        fake_server.server_port = 8765
        fake_server.serve_forever.side_effect = lambda: events.append("serve")
        fake_server.server_close.side_effect = lambda: events.append("server-close")
        fake_claim = mock.Mock()
        fake_claim.close.side_effect = lambda: events.append("claim-close")

        with (
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState",
                return_value=fake_state,
            ),
            mock.patch(
                "optpilot_studio.ui.server._handler_factory",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer",
                return_value=fake_server,
            ),
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                side_effect=lambda _root, **_kwargs: (
                    events.append("claim-acquire") or fake_claim
                ),
            ),
        ):
            run_ui(catalog_roots=[], run_roots=[])

        self.assertIs(fake_server.daemon_threads, False)
        self.assertEqual(
            events,
            [
                "claim-acquire",
                "serve",
                "server-close",
                "stop-interfaces",
                "stop-previews",
                "close-projections",
                "close-coordination",
                "realm-close",
                "claim-close",
            ],
        )

    def test_run_ui_leaves_realm_open_when_background_execution_does_not_quiesce(
        self,
    ) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "unquiesced-realm"
        fake_state = mock.Mock()
        fake_state.stop_transient_interface_launches.return_value = True
        fake_state.stop_workspace_preview_proxies.return_value = True
        fake_state.close_coordination.return_value = False
        fake_server = mock.Mock()
        fake_server.server_port = 8765
        fake_claim = mock.Mock()

        with (
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState",
                return_value=fake_state,
            ),
            mock.patch(
                "optpilot_studio.ui.server._handler_factory",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer",
                return_value=fake_server,
            ),
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                return_value=fake_claim,
            ),
            mock.patch("optpilot_studio.ui.server.print") as studio_print,
        ):
            run_ui(catalog_roots=[], run_roots=[])

        fake_server.server_close.assert_called_once_with()
        fake_state.close_coordination.assert_called_once_with(close_storage=True)
        fake_runtime.close.assert_not_called()
        fake_claim.close.assert_not_called()
        fake_claim.retain_until_process_exit.assert_called_once_with()
        self.assertTrue(
            any(
                "background execution is still unwinding" in str(call.args[0])
                and call.kwargs.get("file") is sys.stderr
                for call in studio_print.call_args_list
            ),
            studio_print.call_args_list,
        )

    def test_run_ui_keeps_coordination_open_for_unquiesced_interface_work(
        self,
    ) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "unquiesced-interface-realm"
        fake_state = mock.Mock()
        fake_state.stop_transient_interface_launches.return_value = False
        fake_state.close_coordination.return_value = True
        fake_server = mock.Mock()
        fake_server.server_port = 8765
        fake_claim = mock.Mock()

        with (
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState",
                return_value=fake_state,
            ),
            mock.patch(
                "optpilot_studio.ui.server._handler_factory",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer",
                return_value=fake_server,
            ),
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                return_value=fake_claim,
            ),
            mock.patch("optpilot_studio.ui.server.print"),
        ):
            run_ui(catalog_roots=[], run_roots=[])

        fake_state.stop_workspace_preview_proxies.assert_not_called()
        fake_state.close_catalog_projections.assert_not_called()
        fake_state.close_coordination.assert_called_once_with(close_storage=False)
        fake_runtime.close.assert_not_called()
        fake_claim.close.assert_not_called()
        fake_claim.retain_until_process_exit.assert_called_once_with()

    def test_run_ui_retains_runtime_ownership_for_live_preview_handlers(
        self,
    ) -> None:
        fake_runtime = mock.Mock()
        fake_runtime.root = self.root / "unquiesced-presentation-realm"
        fake_state = mock.Mock()
        fake_state.stop_transient_interface_launches.return_value = True
        fake_state.stop_workspace_preview_proxies.return_value = False
        fake_state.close_coordination.return_value = True
        fake_server = mock.Mock()
        fake_server.server_port = 8765
        fake_claim = mock.Mock()

        with (
            mock.patch(
                "optpilot_studio.ui.server.LocalRealmRuntime.open",
                return_value=fake_runtime,
            ),
            mock.patch(
                "optpilot_studio.ui.server._environment_preview_runtime_options",
                return_value=(None, ()),
            ),
            mock.patch(
                "optpilot_studio.ui.server.UiState",
                return_value=fake_state,
            ),
            mock.patch(
                "optpilot_studio.ui.server._handler_factory",
                return_value=mock.Mock(),
            ),
            mock.patch(
                "optpilot_studio.ui.server.ThreadingHTTPServer",
                return_value=fake_server,
            ),
            mock.patch(
                "optpilot_studio.ui.server.Path.cwd",
                return_value=self.root,
            ),
            mock.patch(
                "optpilot_studio.ui.server.StudioRuntimeSupervisorClaim.acquire",
                return_value=fake_claim,
            ),
            mock.patch("optpilot_studio.ui.server.print"),
        ):
            run_ui(catalog_roots=[], run_roots=[])

        fake_state.stop_workspace_preview_proxies.assert_called_once_with()
        fake_state.close_catalog_projections.assert_not_called()
        fake_state.close_coordination.assert_called_once_with(
            close_storage=False
        )
        fake_runtime.close.assert_not_called()
        fake_claim.close.assert_not_called()
        fake_claim.retain_until_process_exit.assert_called_once_with()

    def test_assistant_run_tools_use_only_realm_ids_and_direct_run_views(self) -> None:
        session = _create_agent_session(self.state, {"title": "Realm run tools"})
        before_session = _agent_session_by_id(self.state, session["id"])
        before_counts = self._realm_counts()
        before_workspace_files = {
            path.relative_to(self.state.workspaces_dir)
            for path in self.state.workspaces_dir.rglob("*")
        }

        listing = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_run_list",
            {},
        )
        detail = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_run_detail",
            {"run_id": self.run_id},
        )
        removed_workspace_tool = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_run_open_workspace",
            {"run_id": self.run_id},
        )
        compared = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_run_compare",
            {"runs": [self.run_id]},
        )
        file_read = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_run_file_read",
            {"run_id": self.run_id, "path": "summary.json"},
        )
        stopped = _execute_agent_tool(
            self.state,
            session["id"],
            "optpilot_job_stop",
            {"job_id": "any-job"},
        )

        self.assertTrue(listing["ok"], listing)
        listed_run = listing["data"]["runs"][0]
        self.assertEqual(listed_run["run_id"], self.run_id)
        self.assertIn("best_comparable_candidate", listed_run)
        self.assertIn("catalog", listing["data"])
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["data"]["workbench"]["summary"]["run_id"], self.run_id)
        self.assertIn("candidate", detail["data"]["pages"])
        self.assertIn("timeline", detail["data"])
        self.assertFalse(removed_workspace_tool["ok"], removed_workspace_tool)
        self.assertIn("Unknown OptPilot assistant tool", removed_workspace_tool["summary"])
        self.assertNotIn(
            "optpilot_run_open_workspace",
            removed_workspace_tool["data"]["known_tools"],
        )
        self.assertTrue(compared["ok"], compared)
        compared_run = compared["data"]["runs"][0]
        self.assertEqual(compared_run["run_id"], self.run_id)
        self.assertIn("best_comparable_candidate", compared_run)
        self.assertNotIn("path", compared_run)
        for public_run in (listed_run, compared_run):
            for observation_level_field in (
                "best_metric",
                "best_trial_id",
                "best_candidate_id",
            ):
                self.assertNotIn(observation_level_field, public_run)
        for assistant_payload in (listing["data"], compared["data"]):
            serialized_payload = json.dumps(assistant_payload, sort_keys=True)
            for observation_level_field in (
                "best_metric",
                "best_trial_id",
                "best_candidate_id",
            ):
                self.assertNotIn(
                    f'"{observation_level_field}"',
                    serialized_payload,
                )
        self.assertFalse(file_read["ok"])
        self.assertEqual(file_read["data"]["reason"], "raw_run_file_access_removed")
        self.assertFalse(stopped["ok"])
        self.assertFalse(stopped["data"]["can_stop"])
        self.assertEqual(stopped["data"]["reason"], "study_launch_not_found")
        self.assertEqual(
            _agent_session_by_id(self.state, session["id"]), before_session
        )
        self.assertEqual(self._realm_counts(), before_counts)
        self.assertEqual(
            {
                path.relative_to(self.state.workspaces_dir)
                for path in self.state.workspaces_dir.rglob("*")
            },
            before_workspace_files,
        )
        serialized = json.dumps(
            {
                "listing": listing,
                "detail": detail,
                "compared": compared,
            },
            sort_keys=True,
        )
        self.assertNotIn(str(self.root), serialized)
        with self.assertRaisesRegex(ValueError, "canonical Realm run_id"):
            _execute_agent_tool(
                self.state,
                session["id"],
                "optpilot_run_detail",
                {"path": str(self.root / "legacy-run")},
            )

    def test_assistant_launch_uses_server_package_and_realm_authority(self) -> None:
        session = _create_agent_session(self.state, {"title": "Realm launch"})
        _write_package(self.package)
        study_ref = _catalog_payload(self.state)["studies"][0]["ref"]
        operation_id = "studio-test/assistant-durable-launch"
        with (
            mock.patch(
                "optpilot_studio.ui.server._agent_permission_gate",
                return_value=None,
            ),
            mock.patch(
                "optpilot_studio.ui.server._validate_study",
                return_value={
                    "valid": True,
                    "name": "Realm launch",
                    "environment_id": "test-environment",
                    "launch": {
                        "eligible": True,
                        "code": "ready",
                        "reason": None,
                    },
                },
            ),
            mock.patch(
                "optpilot_studio.ui.server.new_local_study_operation_id",
                return_value=operation_id,
            ),
            mock.patch(
                "optpilot_studio.ui.server.subprocess.Popen",
                side_effect=AssertionError("Studio must not spawn a study process."),
            ) as popen,
            mock.patch(
                "optpilot_studio.ui.server._schedule_study_launch_execution",
                return_value=True,
            ) as schedule,
        ):
            launched = _execute_agent_tool(
                self.state,
                session["id"],
                "optpilot_study_launch",
                {"study_ref": study_ref},
            )

        self.assertTrue(launched["ok"], launched)
        job = launched["data"]["job"]
        view = self.runtime.study_launches.read(launch_id=job["job_id"])
        self.assertEqual(
            view.job.plan.input_facts["run"]["run_id"],
            local_study_run_id_for_operation(operation_id),
        )
        self.assertIsNone(job["run_id"])
        self.assertTrue(job["can_stop"])
        popen.assert_not_called()
        schedule.assert_called_once_with(self.state, launch_id=view.launch_id)
        self.assertNotIn(str(self.package), json.dumps(job, sort_keys=True))
        with self.assertRaisesRegex(ValueError, "server-derived"):
            _execute_agent_tool(
                self.state,
                session["id"],
                "optpilot_study_launch",
                {
                    "study_ref": study_ref,
                    "output_root": str(self.root / "legacy-output"),
                },
            )

    def test_temporary_smoke_uses_current_realm_cli_without_output_directory(
        self,
    ) -> None:
        operation_id = "local-study-run/studio-smoke"
        run_id = local_study_run_id_for_operation(operation_id)
        summary = {
            "schema": "optpilot.run-summary-projection.v1",
            "run_id": run_id,
            "run_status": "succeeded",
            "submission_state": "terminal",
            "stop_code": "max_trials",
            "retention_state": "active",
            "objective": {"metric": "score", "direction": "maximize"},
            "budget": {"max_trials": 1, "remaining_trials": 0},
            "counts": {
                "candidates": 1,
                "logical_trials": {
                    "total": 1,
                    "active": 0,
                    "terminal": 1,
                    "successful": 1,
                    "successful_objective_observations": 1,
                    "final_failures": 0,
                    "no_improvement": 0,
                    "by_state": {
                        "accepted": 0,
                        "queued": 0,
                        "running": 0,
                        "retrying": 0,
                        "terminal": 1,
                    },
                },
                "attempts": {
                    "total": 1,
                    "retries": 0,
                    "by_state": {"prepared": 0, "running": 0, "terminal": 1},
                },
                "observations": {
                    "total": 1,
                    "by_outcome": {
                        "success": 1,
                        "invalid": 0,
                        "failed": 0,
                        "timeout": 0,
                        "partial": 0,
                        "cancelled": 0,
                    },
                },
            },
            "best": {
                "metric": 1.0,
                "candidate_id": "candidate-1",
                "logical_trial_id": "trial-1",
                "attempt_id": "attempt-1",
                "observation_id": "observation-1",
            },
            "cursor": {"revision": 4, "sequence": 8},
        }
        temporary_root = self.root / "temporary-smoke"
        temporary_root.mkdir()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(summary),
            stderr="",
        )
        with (
            mock.patch(
                "optpilot_studio.ui.server.new_local_study_operation_id",
                return_value=operation_id,
            ),
            mock.patch(
                "optpilot_studio.ui.server._study_subprocess_env",
                return_value={},
            ),
            mock.patch(
                "optpilot_studio.ui.server.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            result = _run_temporary_realm_smoke(
                self.state,
                package_root=self.package,
                study_path=self.study_path,
                temporary_root=temporary_root,
                timeout_seconds=30,
            )

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["run_id"], run_id)
        command = run.call_args.args[0]
        self.assertEqual(command[3:5], ["run", str(self.study_path)])
        self.assertEqual(
            command[5:],
            [
                "--package-root",
                str(self.package),
                "--realm-root",
                str(temporary_root / "realm"),
                "--operation-id",
                operation_id,
            ],
        )
        self.assertNotIn("--output-root", command)
        self.assertNotIn(str(temporary_root), json.dumps(result, sort_keys=True))

        original = self.study_path.read_text(encoding="utf-8")
        unchanged_package, unchanged_study = _prepare_assistant_smoke_package(
            package_root=self.package,
            study_path=self.study_path,
            temporary_root=temporary_root,
            max_trials=0,
        )
        self.assertEqual(unchanged_package, self.package)
        self.assertEqual(unchanged_study, self.study_path)

        copy_root = self.root / "copy-smoke"
        copy_root.mkdir()
        copied_package, copied_study = _prepare_assistant_smoke_package(
            package_root=self.package,
            study_path=self.study_path,
            temporary_root=copy_root,
            max_trials=2,
        )
        self.assertNotEqual(copied_package, self.package)
        self.assertEqual(self.study_path.read_text(encoding="utf-8"), original)
        self.assertEqual(
            yaml.safe_load(copied_study.read_text(encoding="utf-8"))["budget"][
                "maxTrials"
            ],
            2,
        )

    def test_temporary_smoke_reports_compact_path_free_compiler_failure(self) -> None:
        operation_id = "local-study-run/studio-smoke-failure"
        temporary_root = self.root / "temporary-smoke-failure"
        temporary_root.mkdir()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Traceback (most recent call last):\n"
                f"  File {temporary_root / 'package' / 'method.py'}, line 1\n"
                "RetainedStudyCompileError: method callable is outside declared retained Python roots.\n"
            ),
        )
        with (
            mock.patch(
                "optpilot_studio.ui.server.new_local_study_operation_id",
                return_value=operation_id,
            ),
            mock.patch(
                "optpilot_studio.ui.server._study_subprocess_env",
                return_value={},
            ),
            mock.patch(
                "optpilot_studio.ui.server.subprocess.run",
                return_value=completed,
            ),
        ):
            result = _run_temporary_realm_smoke(
                self.state,
                package_root=self.package,
                study_path=self.study_path,
                temporary_root=temporary_root,
                timeout_seconds=30,
            )

        self.assertFalse(result["valid"], result)
        self.assertEqual(
            result["errors"],
            [
                "RetainedStudyCompileError: method callable is outside declared retained Python roots."
            ],
        )
        self.assertEqual(result["errors"][0], result["stderr"])
        self.assertNotIn(str(temporary_root), json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
