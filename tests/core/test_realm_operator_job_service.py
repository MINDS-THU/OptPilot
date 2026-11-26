from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock

from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.errors import RealmCapacityUnavailable, RealmConflict
from optpilot.realm.environment_preview_binding import RealmEnvironmentPreviewBinder
from optpilot.realm.inspection_service import RealmInspectionTargetService
from optpilot.realm.interface_output_service import (
    RealmInterfaceOutputSessionService,
)
from optpilot.realm.interface_outputs import InterfaceOutputRecord
from optpilot.realm.local_attempt_launcher import RealmLocalAttemptLauncher
from optpilot.realm.local_process_supervisor import LocalProcessSupervisor
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
)
from optpilot.realm.operator_attempt_binding import RealmOperatorAttemptBinder
from optpilot.realm.operator_capacity_records import (
    OperatorCapacityReservationState,
    operator_capacity_reservation_id,
)
from optpilot.realm.operator_job_records import OperatorJobCleanupState, OperatorJobState
from optpilot.realm.operator_job_service import (
    EnvironmentPreviewFinalCapturePending,
    RealmOperatorJobService,
    _operation,
    _output_change_id,
    _normalize_result_tracebacks,
    _preview_output_change_id,
    _preview_output_memberships,
)
from optpilot.realm.refs import request_digest
from tests.core.test_realm_local_attempt_launcher import _RetainedRuntimeFixture
from tests.core.test_local_container_web_provider import _FakeContainerEngine


class _SimulatedServiceCrash(BaseException):
    pass


_OUTPUT_EVALUATOR_SOURCE = """\
from pathlib import Path
import sys

def evaluate(candidate, context):
    output = Path(context['workspace']) / 'shared-output.txt'
    output.write_text('shared output\\n', encoding='utf-8')
    print('stdout-start')
    print('x' * 70000)
    print('stderr-line', file=sys.stderr)
    declarations = [
        {
            'declaration_id': 'environment:primary',
            'name': 'primary',
            'path': output.name,
            'kind': 'file',
            'media_type': 'text/plain',
            'metadata': {},
        },
        {
            'declaration_id': 'environment:alias',
            'name': 'alias',
            'path': output.name,
            'kind': 'file',
            'media_type': 'text/plain',
            'metadata': {},
        },
    ]
    return {'score': candidate['x'], 'output_files': declarations}
"""

_PREVIEW_IMAGE = "example/viewer@sha256:" + "c" * 64
_PREVIEW_INTERFACE = f"""\
interface:
  label: Candidate Preview
  description: Inspect one exact candidate.
  outputs: true
  command: [python, -m, local_package.viewer]
  cwd: .
  env: {{}}
  runtime:
    sandbox: container
    container:
      engine: docker
      image: {_PREVIEW_IMAGE}
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
    extraPorts: [5174]
    readyPath: /ready
    readyTimeoutSeconds: 10
  accepts:
    selectionKinds: [candidate]
    mediaTypes: [application/vnd.optpilot.candidate+json]
"""


@unittest.skipUnless(os.name == "posix", "native process Operator Jobs require POSIX")
class RealmOperatorJobServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)
        self.supervisor_root = self.fixture.root / "operator-job-provider"
        self.service = self._service()
        self.selection = self._selection()

    def _service(self, *, preview_execution: bool = False) -> RealmOperatorJobService:
        principal = self.fixture.ledger.register_principal(
            operation_id="local-attempt/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="operator-service/capacity/local-host",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={
                "cpu_millis": 8000,
                "gpu_count": 0,
                "memory_bytes": 16 * 1024**3,
            },
        )
        inspection = RealmInspectionTargetService(
            self.fixture.ledger, principal
        )
        supervisor = LocalProcessSupervisor(self.supervisor_root)
        launcher = RealmLocalAttemptLauncher(supervisor)
        binder = RealmOperatorAttemptBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
        )
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        interface_outputs = RealmInterfaceOutputSessionService(
            self.fixture.ledger,
            self.fixture.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )
        preview_binder = None
        preview_provider = None
        preview_authority = None
        if preview_execution:
            preview_authority = object()
            self.preview_engine = _FakeContainerEngine()
            preview_provider = LocalContainerWebProvider(
                executable="fake-container",
                control_root=self.fixture.root / "preview-provider",
                broker_authority=preview_authority,
                trusted_gateway_images=(
                    ContainerGatewayImageTrust(_PREVIEW_IMAGE),
                ),
                run_command=self.preview_engine,
                gateway_probe=(
                    lambda _routes, _token, _primary, _path, _timeout: True
                ),
            )
            preview_binder = RealmEnvironmentPreviewBinder(
                self.fixture.ledger,
                self.fixture.projection_service,
                self.fixture.volume_service,
                preview_provider,
            )
        service = RealmOperatorJobService(
            self.fixture.ledger,
            principal,
            inspection,
            self.fixture.provider,
            binder,
            launcher,
            finalizer,
            interface_output_service=interface_outputs,
            environment_preview_binder=preview_binder,
            container_web_provider=preview_provider,
            container_web_broker_authority=preview_authority,
        )
        self.supervisor = supervisor
        self.launcher = launcher
        return service

    def _selection(self):
        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
        )
        return self.fixture.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            kind="candidate",
            entity_id="candidate-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

    def _plan(self, operation_id: str = "operator-service/debug-a"):
        return self.service.plan_candidate_debug_run(
            operation_id=operation_id,
            selection=self.selection,
        )

    def _use_output_fixture(self) -> None:
        fixture = _RetainedRuntimeFixture(
            evaluator_source=_OUTPUT_EVALUATOR_SOURCE
        )
        self.addCleanup(fixture.close)
        self.fixture = fixture
        self.supervisor_root = fixture.root / "operator-job-provider"
        self.service = self._service()
        self.selection = self._selection()

    def _use_preview_fixture(
        self, *, execution: bool = False, outputs: bool = True
    ) -> None:
        fixture = _RetainedRuntimeFixture(
            environment_interface=(
                _PREVIEW_INTERFACE
                if outputs
                else _PREVIEW_INTERFACE.replace("  outputs: true\n", "")
            )
        )
        self.addCleanup(fixture.close)
        self.fixture = fixture
        self.supervisor_root = fixture.root / "operator-job-provider"
        self.service = self._service(preview_execution=execution)
        self.selection = self._selection()

    def _prepare_preview_launch(
        self, *, running: bool, expire_admission: bool = True
    ):
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id=(
                "operator-service/preview-expired-running"
                if running
                else "operator-service/preview-expired-starting"
            ),
            selection=self.selection,
        )
        capacity = self.service._ensure_capacity(queued)
        admission = self.service._ensure_admission(queued)
        managed = self.service._realize_preview_binding(
            record=queued,
            admission=admission,
            recover_only=False,
        )
        self.service._ensure_preview_output_session(queued)
        context = self.service._preview_context_for_record(queued)
        starting = self.fixture.ledger.begin_operator_job_start(
            operation_id=f"operator-service/{queued.job_id}/test-begin-start",
            actor_principal_id="operator",
            job_id=queued.job_id,
            expected_revision=queued.revision,
            admission_lease_id=admission.lease_id,
            admission_holder_id=admission.holder_id,
            admission_fencing_token=admission.fencing_token,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            provider_kind="local-container-web",
            evidence_fingerprint=context.evidence_fingerprint,
            launch_request_digest=managed.request.digest,
        )
        record = starting
        if running:
            self.service._container_web_provider.start_or_adopt(managed.request)
            record = self.fixture.ledger.mark_operator_job_running(
                operation_id=f"operator-service/{queued.job_id}/test-mark-running",
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=starting.revision,
                launch_token=context.launch_token,
                admission_lease_id=admission.lease_id,
                admission_fencing_token=admission.fencing_token,
            )
        if expire_admission:
            with sqlite3.connect(self.fixture.ledger.database_path) as connection:
                connection.execute(
                    "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                    (admission.lease_id,),
                )
        return queued, record, capacity, admission, managed

    def test_environment_preview_plan_is_retained_path_free_and_idempotent(
        self,
    ) -> None:
        self._use_preview_fixture()

        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-a",
            selection=self.selection,
        )

        self.assertEqual(queued.state, OperatorJobState.QUEUED)
        self.assertEqual(queued.plan.job_kind, "environment-preview")
        self.assertEqual(queued.plan.target.kind, "environment-interface")
        self.assertEqual(queued.plan.backend_kind, "local-container-web")
        self.assertEqual(queued.plan.network_policy, "denied")
        self.assertEqual(queued.plan.network_enforcement, "enforced")
        self.assertEqual(queued.plan.requested_secret_names, ())
        self.assertEqual(
            dict(queued.plan.resource_claims),
            {"cpu_millis": 1000, "memory_bytes": 512 * 1024**2},
        )
        retained = queued.plan.input_facts["preview_plan"]
        self.assertEqual(retained["runtime"]["imageRef"], _PREVIEW_IMAGE)
        self.assertEqual(retained["presentation"]["readyPath"], "/ready")
        self.assertEqual(
            self.service.plan_environment_preview(
                operation_id="operator-service/preview-a",
                selection=self.selection,
                profile_id="default",
            ),
            queued,
        )
        encoded = json.dumps(queued.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.fixture.root), encoded)
        self.assertNotIn("host_path", encoded)
        self.assertNotIn("store_id", encoded)

    def test_environment_preview_runs_presents_and_stops_with_durable_cleanup(
        self,
    ) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-execute",
            selection=self.selection,
        )
        failures: list[BaseException] = []

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:  # surfaced below with the durable head
                failures.append(error)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            current = self.service.read(job_id=queued.job_id)
            if current.state is OperatorJobState.RUNNING:
                break
            if current.state.terminal or failures:
                break
            time.sleep(0.02)
        current = self.service.read(job_id=queued.job_id)
        self.assertEqual(current.state, OperatorJobState.RUNNING, failures)

        broker_binding = self.service.acquire_environment_preview_broker_binding(
            job_id=queued.job_id
        )
        self.assertEqual(broker_binding.owner_id, queued.job_id)
        self.assertEqual(broker_binding.primary_port, 5173)
        self.assertEqual(set(broker_binding.routes), {5173, 5174})
        self.assertNotIn(
            next(iter(broker_binding.authorization_headers.values())),
            repr(broker_binding),
        )

        stopped = self.service.request_stop(
            operation_id="operator-service/preview-execute/stop",
            job_id=queued.job_id,
        )
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(stopped.state, OperatorJobState.CANCELLED)
        self.assertEqual(stopped.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(stopped.result.result.result_kind, "environment-preview")
        self.assertEqual(stopped.result.result.status, "cancelled")
        self.assertEqual(self.preview_engine.containers, {})
        self.assertEqual(self.preview_engine.networks, {})

    def test_environment_preview_adopts_natural_terminal_success(self) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-natural-exit",
            selection=self.selection,
        )
        failures: list[BaseException] = []

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            current = self.service.read(job_id=queued.job_id)
            if current.state is OperatorJobState.RUNNING:
                self.preview_engine.role("app")["State"] = {
                    "Running": False,
                    "ExitCode": 0,
                }
                break
            if current.state.terminal or failures:
                break
            time.sleep(0.02)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        terminal = self.service.read(job_id=queued.job_id)
        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(terminal.result.result.status, "success")
        self.assertEqual(
            dict(terminal.result.result.details),
            {"exit_code": 0, "profile_id": "default"},
        )

    def test_view_only_environment_preview_creates_no_output_session(self) -> None:
        self._use_preview_fixture(execution=True, outputs=False)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-view-only",
            selection=self.selection,
        )
        retained = queued.plan.input_facts["preview_plan"]
        self.assertFalse(retained["outputsEnabled"])
        invocation_env = retained["invocation"]["environment"]
        self.assertNotIn("OPTPILOT_INTERFACE_OUTPUT_ROOT", invocation_env)
        self.assertNotIn("OPTPILOT_INTERFACE_OUTPUTS_FILE", invocation_env)
        output_service = self.service._preview_output_service()
        failures: list[BaseException] = []

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(
            output_service,
            "create_session",
            wraps=output_service.create_session,
        ) as create_session:
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                current = self.service.read(job_id=queued.job_id)
                if current.state is OperatorJobState.RUNNING:
                    self.preview_engine.role("app")["State"] = {
                        "Running": False,
                        "ExitCode": 0,
                    }
                    break
                if current.state.terminal or failures:
                    break
                time.sleep(0.02)
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(create_session.call_count, 0)
        terminal = self.service.read(job_id=queued.job_id)
        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(terminal.result.result.declared_outputs, ())
        self.assertEqual(
            self.service.list_environment_preview_output_statuses(
                job_id=queued.job_id
            ),
            (),
        )

    def test_environment_preview_adopts_declared_tree_without_recapturing_bytes(
        self,
    ) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-declared-tree",
            selection=self.selection,
        )
        failures: list[BaseException] = []

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(
            self.fixture.content,
            "capture",
            wraps=self.fixture.content.capture,
        ) as capture:
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            deadline = time.time() + 10
            captured_generation = None
            while time.time() < deadline:
                current = self.service.read(job_id=queued.job_id)
                if current.state is OperatorJobState.RUNNING:
                    with self.service._preview_output_state_lock:
                        managed = self.service._active_preview_bindings.get(
                            queued.job_id
                        )
                    if managed is not None:
                        descriptor = managed.output_capture_descriptor
                        generated = descriptor.source_root / "generated"
                        generated.mkdir()
                        (generated / "simulator.py").write_text(
                            "print('preview output')\n", encoding="utf-8"
                        )
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
                        break
                if current.state.terminal or failures:
                    break
                time.sleep(0.02)
            else:
                self.fail("Environment Preview did not expose its output binding.")

            deadline = time.time() + 10
            while time.time() < deadline:
                statuses = self.service.list_environment_preview_output_statuses(
                    job_id=queued.job_id
                )
                if statuses and statuses[0].ready_generation is not None:
                    captured_generation = statuses[0].ready_generation
                    self.preview_engine.role("app")["State"] = {
                        "Running": False,
                        "ExitCode": 0,
                    }
                    break
                if failures:
                    break
                time.sleep(0.02)
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertIsNotNone(captured_generation)
        assert captured_generation is not None
        terminal = self.service.read(job_id=queued.job_id)
        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(capture.call_count, 1)
        outputs = terminal.result.result.declared_outputs
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].declaration_id, "generated-simulator")
        self.assertEqual(outputs[0].content_ref, str(captured_generation.content_ref))
        retained = tuple(
            item
            for item in self.fixture.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=terminal.owner_id,
            )
            if item.role == "operator-job-output"
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(str(retained[0].content_ref), outputs[0].content_ref)
        handle = self.service._preview_output_service().recover_session(
            launch_id=queued.job_id
        )
        self.assertEqual(handle.session.state.value, "retired")
        selection = self.fixture.ledger.mint_operator_job_output_selection(
            actor_principal_id="operator",
            job_id=terminal.job_id,
            output_id="generated-simulator",
        )
        self.assertEqual(selection.source_kind, "operator-job")
        resolved = self.fixture.ledger.resolve_selection_for_read_projection(
            actor_principal_id="operator",
            selection=selection,
        )
        self.assertTrue(resolved.eligibility.eligible)

    def test_environment_preview_failed_output_retry_is_live_and_exact(self) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-output-retry",
            selection=self.selection,
        )
        failures: list[BaseException] = []

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        deadline = time.time() + 10
        managed = None
        while time.time() < deadline:
            current = self.service.read(job_id=queued.job_id)
            if current.state is OperatorJobState.RUNNING:
                with self.service._preview_output_state_lock:
                    managed = self.service._active_preview_bindings.get(
                        queued.job_id
                    )
                if managed is not None:
                    break
            if current.state.terminal or failures:
                break
            time.sleep(0.02)
        self.assertIsNotNone(managed)
        assert managed is not None
        descriptor = managed.output_capture_descriptor
        descriptor.control_file.write_text(
            json.dumps(
                {
                    "schema_version": "optpilot.interface.output.v1",
                    "id": "retry-tree",
                    "label": "Retry tree",
                    "kind": "tree",
                    "root": "output",
                    "path": "retry-tree",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            statuses = self.service.list_environment_preview_output_statuses(
                job_id=queued.job_id
            )
            if statuses and statuses[0].state.value == "failed":
                break
            if failures:
                break
            time.sleep(0.02)
        self.assertEqual(statuses[0].state.value, "failed")
        generated = descriptor.source_root / "retry-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("ready\n", encoding="utf-8")
        retried = self.service.retry_environment_preview_output(
            job_id=queued.job_id,
            output_id="retry-tree",
        )
        self.assertEqual(retried.state.value, "ready")
        self.assertEqual(retried.attempt_number, 2)
        self.preview_engine.role("app")["State"] = {
            "Running": False,
            "ExitCode": 0,
        }
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        terminal = self.service.read(job_id=queued.job_id)
        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(
            [item.declaration_id for item in terminal.result.result.declared_outputs],
            ["retry-tree"],
        )
        with self.assertRaises(RealmConflict):
            self.service.retry_environment_preview_output(
                job_id=queued.job_id,
                output_id="retry-tree",
            )

    def test_environment_preview_terminal_waits_for_persisted_sealing_output(
        self,
    ) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "terminal-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("ready\n", encoding="utf-8")
        payload = {
            "schema_version": "optpilot.interface.output.v1",
            "id": "terminal-tree",
            "label": "Terminal tree",
            "kind": "tree",
            "root": "output",
            "path": "terminal-tree",
        }
        descriptor.control_file.write_text(
            json.dumps(payload) + "\n",
            encoding="utf-8",
        )
        record = InterfaceOutputRecord.from_dict(payload)
        output_service = self.service._preview_output_service()
        handle = output_service.recover_session(launch_id=queued.job_id)
        sealing = self.fixture.ledger.begin_interface_output_capture(
            operation_id=f"operator-service/{queued.job_id}/test-sealing-begin",
            actor_principal_id="operator",
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            record=record,
            attempt_ttl_seconds=60,
            attempt_id="ioa-terminal-pending",
            operation_prefix="iop-terminal-pending",
        )
        self.assertEqual(sealing.state.value, "sealing")
        terminal_proof = self.service._container_web_provider.stop(managed.request)

        with mock.patch.object(
            output_service,
            "resume_generation",
            side_effect=RealmConflict("another adopter is sealing"),
        ):
            with self.assertRaises(EnvironmentPreviewFinalCapturePending):
                self.service._finish_preview_observation(
                    running,
                    terminal=terminal_proof,
                    managed=managed,
                    admission=admission,
                )

        pending = self.service.read(job_id=queued.job_id)
        self.assertFalse(pending.state.terminal)
        self.assertTrue(descriptor.source_root.exists())
        active_handle = output_service.recover_session(launch_id=queued.job_id)
        self.assertEqual(active_handle.session.state.value, "active")

        finished = self.service._finish_preview_observation(
            pending,
            terminal=terminal_proof,
            managed=managed,
            admission=admission,
        )
        self.assertTrue(finished.state.terminal)
        self.assertEqual(
            [item.declaration_id for item in finished.result.result.declared_outputs],
            ["terminal-tree"],
        )
        self.assertEqual(finished.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertFalse(descriptor.source_root.exists())
        retired = output_service.recover_session(launch_id=queued.job_id)
        self.assertEqual(retired.session.state.value, "retired")

    def test_terminal_binding_error_does_not_force_fail_other_supervisor_capture(
        self,
    ) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "binding-error-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("ready\n", encoding="utf-8")
        payload = {
            "schema_version": "optpilot.interface.output.v1",
            "id": "binding-error-tree",
            "label": "Binding error tree",
            "kind": "tree",
            "root": "output",
            "path": "binding-error-tree",
        }
        descriptor.control_file.write_text(
            json.dumps(payload) + "\n",
            encoding="utf-8",
        )
        output_service = self.service._preview_output_service()
        handle = output_service.recover_session(launch_id=queued.job_id)
        self.fixture.ledger.begin_interface_output_capture(
            operation_id=f"operator-service/{queued.job_id}/binding-error-begin",
            actor_principal_id="operator",
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            record=InterfaceOutputRecord.from_dict(payload),
            attempt_ttl_seconds=60,
            attempt_id="ioa-binding-error-pending",
            operation_prefix="iop-binding-error-pending",
        )
        terminal_proof = self.service._container_web_provider.stop(managed.request)
        binder = self.service._environment_preview_binder
        self.assertIsNotNone(binder)

        with mock.patch.object(
            binder,
            "recover_terminal_output_capture",
            side_effect=RealmConflict("terminal binding temporarily unavailable"),
        ):
            with self.assertRaises(EnvironmentPreviewFinalCapturePending):
                self.service._finish_preview_observation(
                    running,
                    terminal=terminal_proof,
                    managed=managed,
                    admission=admission,
                )

        persisted = output_service.list_statuses(handle=handle)
        self.assertEqual([item.state.value for item in persisted], ["sealing"])
        active = output_service.recover_session(launch_id=queued.job_id)
        self.assertEqual(active.lease.state.value, "active")
        self.assertTrue(descriptor.source_root.exists())

        finished = self.service._finish_preview_observation(
            self.service.read(job_id=queued.job_id),
            terminal=terminal_proof,
            managed=managed,
            admission=admission,
        )
        self.assertTrue(finished.state.terminal)
        self.assertEqual(
            [item.declaration_id for item in finished.result.result.declared_outputs],
            ["binding-error-tree"],
        )

    def test_environment_preview_replays_after_final_capture_before_job_commit(
        self,
    ) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "crash-window-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("stable\n", encoding="utf-8")
        descriptor.control_file.write_text(
            json.dumps(
                {
                    "schema_version": "optpilot.interface.output.v1",
                    "id": "crash-window-tree",
                    "label": "Crash window tree",
                    "kind": "tree",
                    "root": "output",
                    "path": "crash-window-tree",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        terminal_proof = self.service._container_web_provider.stop(managed.request)

        with mock.patch.object(
            self.fixture.content,
            "capture",
            wraps=self.fixture.content.capture,
        ) as capture:
            handle, statuses, _diagnostics, _truncated = (
                self.service._final_preview_output_state(
                    running,
                    terminal=terminal_proof,
                )
            )
            generation = statuses[0].ready_generation
            self.assertIsNotNone(generation)
            self.assertEqual(handle.lease.state.value, "released")
            self.assertFalse(self.service.read(job_id=queued.job_id).state.terminal)

            finished = self.service._finish_preview_observation(
                self.service.read(job_id=queued.job_id),
                terminal=terminal_proof,
                managed=managed,
                admission=admission,
            )

        self.assertEqual(capture.call_count, 1)
        self.assertTrue(finished.state.terminal)
        self.assertEqual(
            finished.result.result.declared_outputs[0].content_ref,
            str(generation.content_ref),  # type: ignore[union-attr]
        )
        self.assertEqual(finished.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_crash_after_unknown_terminal_close_replays_without_rebinding(self) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        terminal_proof = self.service._container_web_provider.stop(managed.request)
        binder = self.service._environment_preview_binder
        self.assertIsNotNone(binder)
        output_service = self.service._preview_output_service()

        with mock.patch.object(
            binder,
            "recover_terminal_output_capture",
            side_effect=RealmConflict("terminal binding unavailable"),
        ):
            handle, statuses, diagnostics, _truncated = (
                self.service._final_preview_output_state(
                    running,
                    terminal=terminal_proof,
                )
            )
        self.assertEqual(handle.lease.state.value, "released")
        self.assertEqual(statuses, ())
        self.assertEqual(diagnostics[0]["code"], "final_capture_unavailable")
        self.assertFalse(self.service.read(job_id=queued.job_id).state.terminal)

        # A crash after the durable close must not reconstruct a new control
        # snapshot or replay the deterministic close operation with a
        # different final-record request payload.
        with mock.patch.object(
            binder,
            "recover_terminal_output_capture",
            side_effect=AssertionError("closed capture was rebound"),
        ), mock.patch.object(
            output_service,
            "close_capture",
            wraps=output_service.close_capture,
        ) as close_capture:
            finished = self.service._finish_preview_observation(
                self.service.read(job_id=queued.job_id),
                terminal=terminal_proof,
                managed=managed,
                admission=admission,
            )

        self.assertEqual(close_capture.call_count, 1)
        self.assertIn(
            "preview-output-session/retire-release-lease",
            close_capture.call_args.kwargs["operation_id"],
        )
        self.assertTrue(finished.state.terminal)
        self.assertEqual(finished.result.result.declared_outputs, ())
        self.assertEqual(finished.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_environment_preview_starting_recovers_stop_after_admission_expiry(self) -> None:
        queued, starting, _capacity, _admission, managed = (
            self._prepare_preview_launch(running=False)
        )

        terminal = self.service.execute(job_id=starting.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(terminal.outcome.outcome.code, "operator_job_admission_lost")
        self.assertFalse(terminal.outcome.outcome.started)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertFalse(managed.request.mounts[0].host_path.exists())
        reservation = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                queued.plan.backend_realm, queued.job_id
            ),
        )
        self.assertEqual(reservation.state, OperatorCapacityReservationState.RELEASED)

    def test_environment_preview_running_recovers_stop_after_admission_expiry(self) -> None:
        queued, running, _capacity, _admission, managed = (
            self._prepare_preview_launch(running=True)
        )
        self.assertTrue(self.preview_engine.containers)

        terminal = self.service.execute(job_id=running.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(terminal.outcome.outcome.code, "operator_job_admission_lost")
        self.assertTrue(terminal.outcome.outcome.started)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(self.preview_engine.containers, {})
        self.assertEqual(self.preview_engine.networks, {})
        self.assertFalse(managed.request.mounts[0].host_path.exists())
        reservation = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                queued.plan.backend_realm, queued.job_id
            ),
        )
        self.assertEqual(reservation.state, OperatorCapacityReservationState.RELEASED)

    def test_environment_preview_admission_loss_final_captures_terminal_volume(
        self,
    ) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "admission-loss-tree"
        generated.mkdir()
        (generated / "simulator.py").write_text(
            "print('retained after admission loss')\n",
            encoding="utf-8",
        )
        descriptor.control_file.write_text(
            json.dumps(
                {
                    "schema_version": "optpilot.interface.output.v1",
                    "id": "admission-loss-tree",
                    "label": "Admission loss tree",
                    "kind": "tree",
                    "root": "output",
                    "path": "admission-loss-tree",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (admission.lease_id,),
            )

        with mock.patch.object(
            self.fixture.content,
            "capture",
            wraps=self.fixture.content.capture,
        ) as capture:
            terminal = self.service.execute(job_id=running.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(terminal.outcome.outcome.code, "operator_job_admission_lost")
        self.assertEqual(capture.call_count, 1)
        outputs = terminal.result.result.declared_outputs
        self.assertEqual([item.declaration_id for item in outputs], ["admission-loss-tree"])
        retained = tuple(
            item
            for item in self.fixture.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=terminal.owner_id,
            )
            if item.role == "operator-job-output"
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(str(retained[0].content_ref), outputs[0].content_ref)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertFalse(descriptor.source_root.exists())
        retired = self.service._preview_output_service().recover_session(
            launch_id=queued.job_id
        )
        self.assertEqual(retired.session.state.value, "retired")

    def test_environment_preview_invalid_recovered_binding_is_detached_after_identity_cleanup(self) -> None:
        queued, starting, _capacity, _admission, original = (
            self._prepare_preview_launch(
                running=False,
                expire_admission=False,
            )
        )
        realize = self.service._realize_preview_binding
        recovered = []

        def recover_then_invalidate(**kwargs):
            binding = realize(**kwargs)
            recovered.append(binding)
            binding.validate = mock.Mock(
                side_effect=RealmConflict("recovered binding validation failed")
            )
            return binding

        with mock.patch.object(
            self.service,
            "_realize_preview_binding",
            side_effect=recover_then_invalidate,
        ):
            terminal = self.service.execute(job_id=starting.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "environment_preview_binding_lost"
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].released)
        self.service._reconcile_terminal_cleanup(terminal, managed=original)
        self.assertTrue(original.released)

    def test_environment_preview_cleanup_crash_replays_after_resources_before_capacity(self) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-cleanup-order-crash",
            selection=self.selection,
        )
        binder = self.service._environment_preview_binder
        assert binder is not None
        cleanup = binder.cleanup_after_terminal
        events: list[str] = []
        failures: list[BaseException] = []

        def cleanup_then_record(**kwargs):
            result = cleanup(**kwargs)
            events.append("resources")
            return result

        def crash_before_authority_release(**_kwargs):
            events.append("authority")
            raise _SimulatedServiceCrash()

        def execute() -> None:
            try:
                self.service.execute(job_id=queued.job_id)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(
            binder, "cleanup_after_terminal", side_effect=cleanup_then_record
        ), mock.patch.object(
            self.service,
            "_release_admission_identity",
            side_effect=crash_before_authority_release,
        ):
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                current = self.service.read(job_id=queued.job_id)
                if current.state is OperatorJobState.RUNNING:
                    self.preview_engine.role("app")["State"] = {
                        "Running": False,
                        "ExitCode": 0,
                    }
                    break
                if current.state.terminal or failures:
                    break
                time.sleep(0.02)
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], _SimulatedServiceCrash)
        self.assertEqual(events, ["resources", "authority"])
        pending = self.service.read(job_id=queued.job_id)
        self.assertEqual(pending.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(pending.cleanup_state, OperatorJobCleanupState.PENDING)
        self.assertEqual(self.preview_engine.containers, {})
        active_capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                queued.plan.backend_realm, queued.job_id
            ),
        )
        self.assertEqual(
            active_capacity.state, OperatorCapacityReservationState.ACTIVE
        )

        restarted = self._service(preview_execution=True)
        completed = restarted.execute(job_id=queued.job_id)

        self.assertEqual(completed.cleanup_state, OperatorJobCleanupState.COMPLETE)
        released_capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=active_capacity.reservation_id,
        )
        self.assertEqual(
            released_capacity.state, OperatorCapacityReservationState.RELEASED
        )

    def test_environment_preview_begin_start_stop_releases_open_binding(self) -> None:
        self._use_preview_fixture(execution=True)
        queued = self.service.plan_environment_preview(
            operation_id="operator-service/preview-begin-start-stop",
            selection=self.selection,
        )
        begin_start = self.fixture.ledger.begin_operator_job_start
        realize = self.service._realize_preview_binding
        bindings = []

        def capture_binding(**kwargs):
            binding = realize(**kwargs)
            bindings.append(binding)
            return binding

        def stop_after_start(**kwargs):
            starting = begin_start(**kwargs)
            self.fixture.ledger.request_operator_job_stop(
                operation_id="operator-service/preview-begin-start-stop/cancel",
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=starting.revision,
                reason_code="cancelled_during_begin_start",
            )
            raise RealmConflict("concurrent stop won begin-start")

        with mock.patch.object(
            self.service,
            "_realize_preview_binding",
            side_effect=capture_binding,
        ), mock.patch.object(
            self.fixture.ledger,
            "begin_operator_job_start",
            side_effect=stop_after_start,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0].released)
        self.assertEqual(self.preview_engine.containers, {})
        self.assertEqual(self.preview_engine.networks, {})

    def _assert_capture_aborted_without_live_holds(
        self, *, job_id: str, owner_id: str
    ) -> None:
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            output_change = connection.execute(
                "SELECT txn.state, lease.state FROM owner_transactions txn "
                "JOIN leases lease ON lease.lease_id = txn.retention_lease_id "
                "WHERE txn.change_id = ?",
                (_output_change_id(job_id),),
            ).fetchone()
            active_holds = connection.execute(
                "SELECT COUNT(*) FROM owner_transactions txn "
                "JOIN leases lease ON lease.lease_id = txn.retention_lease_id "
                "WHERE txn.owner_id = ? AND lease.state = 'active'",
                (owner_id,),
            ).fetchone()[0]
            adopted_outputs = connection.execute(
                "SELECT COUNT(*) FROM owner_memberships WHERE owner_id = ? "
                "AND role = 'operator-job-output' AND removed_revision IS NULL",
                (owner_id,),
            ).fetchone()[0]
        self.assertEqual(output_change, ("aborted", "released"))
        self.assertEqual(active_holds, 0)
        self.assertEqual(adopted_outputs, 0)

    def test_debug_run_executes_without_copy_and_replays_terminal_result(self) -> None:
        queued = self._plan()
        self.assertEqual(queued.state, OperatorJobState.QUEUED)
        self.assertEqual(queued.plan.network_policy, "denied")
        self.assertEqual(queued.plan.network_enforcement, "advisory")

        terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(terminal.result.result.status, "success")
        self.assertEqual(dict(terminal.result.result.metrics), {"score": 0.5})
        self.assertEqual(terminal.result.result.declared_outputs, ())
        self.assertEqual(
            self.service.execute(job_id=queued.job_id), terminal
        )
        self.assertEqual(
            self._plan(), terminal
        )
        self.assertEqual(
            self.service.list_for_run(
                source_owner_id=self.selection.source_owner_id,
                run_id=self.selection.source_id,
            ),
            (terminal,),
        )

        manifest = self.fixture.ledger.read_owner_derivation(
            actor_principal_id="operator", owner_id=terminal.owner_id
        )
        self.assertEqual(
            {binding.source_owner_id for binding in manifest.bindings},
            {self.selection.source_owner_id},
        )
        self.assertEqual(
            {
                (item.source_store_id, str(item.content_ref), item.target_role)
                for item in manifest.bindings
            },
            {
                (item.store_id, str(item.content_ref), item.role)
                for item in manifest.target_memberships
            },
        )
        encoded = json.dumps(terminal.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.fixture.root), encoded)
        with sqlite3.connect(self.supervisor.database_path) as connection:
            row = connection.execute(
                "SELECT retired, request_json FROM process_launches "
                "WHERE launch_token = ?",
                (terminal.launch_intent.launch_token,),
            ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(
            set(json.loads(row[1])), {"launch_request_digest", "schema"}
        )

        capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                terminal.plan.backend_realm, terminal.job_id
            ),
        )
        self.assertEqual(
            capacity.state, OperatorCapacityReservationState.RELEASED
        )
        self.assertEqual(dict(capacity.claims), dict(terminal.plan.resource_claims))
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_debug_failure_retains_portable_normalized_error_evidence(self) -> None:
        fixture = _RetainedRuntimeFixture(
            evaluator_source=(
                "def evaluate(candidate, context):\n"
                "    raise RuntimeError('expected evaluator failure')\n"
            )
        )
        self.addCleanup(fixture.close)
        self.fixture = fixture
        self.supervisor_root = fixture.root / "operator-job-provider"
        self.service = self._service()
        self.selection = self._selection()

        queued = self._plan("operator-service/debug-evaluator-failure")
        terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(terminal.outcome.outcome.code, "evaluation_failed")
        result = terminal.result.result
        self.assertEqual(result.status, "failed")
        error = dict(result.event_summary["error"])
        self.assertEqual(error["phase"], "environment_evaluation")
        self.assertEqual(error["type"], "RuntimeError")
        self.assertEqual(error["message"], "expected evaluator failure")
        self.assertTrue(error["traceback"].startswith("Traceback"))
        self.assertFalse(
            any(character in error["traceback"] for character in "\r\n\t")
        )
        self.assertEqual(
            dict(result.event_summary["errors"][0]),
            error,
        )
        self.assertEqual(dict(result.details["error"]), error)
        self.assertEqual(
            _normalize_result_tracebacks(
                {
                    "error": {
                        "message": "still retained",
                        "traceback": " \n\t ",
                        "type": "RuntimeError",
                    }
                }
            ),
            {
                "error": {
                    "message": "still retained",
                    "type": "RuntimeError",
                }
            },
        )

    def test_debug_starting_recovers_provider_after_admission_expiry(self) -> None:
        queued = self._plan("operator-service/debug-expired-starting")
        with mock.patch.object(
            self.launcher,
            "start_noncanonical",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)
        starting = self.service.read(job_id=queued.job_id)
        self.assertEqual(starting.state, OperatorJobState.STARTING)
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (starting.launch_intent.admission_lease_id,),
            )

        restarted = self._service()
        terminal = restarted.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_admission_lost"
        )
        self.assertFalse(terminal.outcome.outcome.started)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                terminal.plan.backend_realm, terminal.job_id
            ),
        )
        self.assertEqual(capacity.state, OperatorCapacityReservationState.RELEASED)

    def test_debug_restart_recover_stop_race_releases_open_binding(self) -> None:
        queued = self._plan("operator-service/recover-stop-race")
        with mock.patch.object(
            self.launcher,
            "start_noncanonical",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)
        starting = self.service.read(job_id=queued.job_id)
        self.assertEqual(starting.state, OperatorJobState.STARTING)

        restarted = self._service()
        recover_binding = restarted._attempt_binder.recover_existing
        bindings = []

        def capture_binding(**kwargs):
            binding = recover_binding(**kwargs)
            bindings.append(binding)
            return binding

        def stop_then_fail_recovery(**_kwargs):
            current = restarted.read(job_id=queued.job_id)
            self.fixture.ledger.request_operator_job_stop(
                operation_id="operator-service/recover-stop-race/cancel",
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=current.revision,
                reason_code="cancelled_during_recovery",
            )
            raise RuntimeError("provider recovery lost the stop race")

        with mock.patch.object(
            restarted._attempt_binder,
            "recover_existing",
            side_effect=capture_binding,
        ), mock.patch.object(
            restarted._launcher,
            "recover_noncanonical",
            side_effect=stop_then_fail_recovery,
        ):
            terminal = restarted.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0].released)

    def test_debug_running_recovers_provider_after_admission_expiry(self) -> None:
        slow = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(slow.close)
        self.fixture = slow
        self.supervisor_root = slow.root / "operator-job-provider"
        service = self._service()
        selection = self._selection()
        queued = service.plan_candidate_debug_run(
            operation_id="operator-service/debug-expired-running",
            selection=selection,
        )
        outcomes: list[object] = []

        def execute() -> None:
            try:
                outcomes.append(service.execute(job_id=queued.job_id))
            except BaseException as error:
                outcomes.append(error)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            running = service.read(job_id=queued.job_id)
            if running.state is OperatorJobState.RUNNING:
                break
            time.sleep(0.02)
        else:
            self.fail("Debug Run did not reach running state.")
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (running.launch_intent.admission_lease_id,),
            )

        restarted = self._service()
        terminal = restarted.execute(job_id=queued.job_id)
        thread.join(timeout=10.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_admission_lost"
        )
        self.assertTrue(terminal.outcome.outcome.started)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(isinstance(outcomes[0], BaseException), outcomes[0])
        self.assertEqual(outcomes[0], terminal)

    def test_debug_cleanup_crash_replays_resources_before_capacity(self) -> None:
        queued = self._plan("operator-service/debug-cleanup-order-crash")
        binder = self.service._attempt_binder
        cleanup = binder.cleanup_after_terminal
        events: list[str] = []

        def cleanup_then_record(**kwargs):
            result = cleanup(**kwargs)
            events.append("resources")
            return result

        def crash_before_authority_release(**_kwargs):
            events.append("authority")
            raise _SimulatedServiceCrash()

        with mock.patch.object(
            binder, "cleanup_after_terminal", side_effect=cleanup_then_record
        ), mock.patch.object(
            self.service,
            "_release_admission_identity",
            side_effect=crash_before_authority_release,
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)

        self.assertEqual(events, ["resources", "authority"])
        pending = self.service.read(job_id=queued.job_id)
        self.assertEqual(pending.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(pending.cleanup_state, OperatorJobCleanupState.PENDING)
        capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                pending.plan.backend_realm, pending.job_id
            ),
        )
        self.assertEqual(capacity.state, OperatorCapacityReservationState.ACTIVE)

        restarted = self._service()
        completed = restarted.execute(job_id=pending.job_id)

        self.assertEqual(completed.cleanup_state, OperatorJobCleanupState.COMPLETE)
        released = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=capacity.reservation_id,
        )
        self.assertEqual(released.state, OperatorCapacityReservationState.RELEASED)

    def test_complete_cleanup_replay_only_detaches_live_local_binding(self) -> None:
        queued = self._plan("operator-service/complete-cleanup-live-binding")
        self.service._ensure_capacity(queued)
        admission = self.service._ensure_admission(queued)
        context = self.service._context_for_record(queued)
        shadow = self.service._attempt_binder.realize(
            actor_principal_id="operator",
            job_id=queued.job_id,
            owner_id=queued.owner_id,
            admission_lease=admission,
            attempt_id=context.attempt_id,
            binding_id=context.binding_id,
            launch_token=context.launch_token,
            evidence_fingerprint=context.evidence_fingerprint,
            evaluation_spec=context.evaluation_spec,
            portable_spec=context.portable_spec,
            ttl_seconds=queued.plan.timeout_seconds + 3600.0,
        )
        terminal = self.service.execute(job_id=queued.job_id)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertFalse(shadow.released)

        with mock.patch.object(
            self.service,
            "_release_admission_identity",
            side_effect=AssertionError("completed admission cleanup replayed"),
        ), mock.patch.object(
            self.service,
            "_complete_cleanup",
            side_effect=AssertionError("completed accounting cleanup replayed"),
        ):
            self.service._reconcile_terminal_cleanup(terminal, managed=shadow)

        self.assertTrue(shadow.released)

    def test_unlaunched_passive_reservation_cleanup_needs_no_live_admission(self) -> None:
        queued = self._plan("operator-service/unlaunched-passive-race")
        begin_start = self.fixture.ledger.begin_operator_job_start
        realize = self.service._attempt_binder.realize
        bindings = []

        def capture_binding(**kwargs):
            binding = realize(**kwargs)
            bindings.append(binding)
            return binding

        def cancel_and_expire_before_intent(**kwargs):
            self.fixture.ledger.request_operator_job_stop(
                operation_id="operator-service/unlaunched-passive-race/cancel",
                actor_principal_id="operator",
                job_id=queued.job_id,
                expected_revision=kwargs["expected_revision"],
                reason_code="cancelled_before_intent",
            )
            with sqlite3.connect(self.fixture.ledger.database_path) as connection:
                connection.execute(
                    "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                    (kwargs["admission_lease_id"],),
                )
            return begin_start(**kwargs)

        orphan_reconcile = self.launcher.reconcile_unbound_noncanonical_terminal
        with mock.patch.object(
            self.fixture.ledger,
            "begin_operator_job_start",
            side_effect=cancel_and_expire_before_intent,
        ), mock.patch.object(
            self.service._attempt_binder,
            "realize",
            side_effect=capture_binding,
        ), mock.patch.object(
            self.launcher,
            "reconcile_unbound_noncanonical_terminal",
            wraps=orphan_reconcile,
        ) as reconcile_spy:
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
        self.assertIsNone(terminal.launch_intent)
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        reconcile_spy.assert_called_once()
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0].released)
        capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                terminal.plan.backend_realm, terminal.job_id
            ),
        )
        self.assertEqual(capacity.state, OperatorCapacityReservationState.RELEASED)

    def test_capacity_rejection_happens_before_content_admission(self) -> None:
        queued = self._plan("operator-service/capacity-rejected")
        self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="operator-service/capacity/too-small",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={
                "cpu_millis": 1,
                "gpu_count": 0,
                "memory_bytes": 1,
            },
        )

        with self.assertRaises(RealmCapacityUnavailable):
            self.service.execute(job_id=queued.job_id)

        current = self.service.read(job_id=queued.job_id)
        self.assertEqual(current.state, OperatorJobState.QUEUED)
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            admission_count = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE owner_id = ? "
                "AND lease_kind = 'operator-job-admission'",
                (queued.owner_id,),
            ).fetchone()[0]
        self.assertEqual(admission_count, 0)

    def test_live_worker_renews_and_releases_its_capacity_fence(self) -> None:
        queued = self._plan("operator-service/capacity-heartbeat")
        renew = self.fixture.ledger.renew_operator_capacity_reservation
        with mock.patch(
            "optpilot.realm.operator_job_service."
            "_CAPACITY_HEARTBEAT_MAX_INTERVAL_SECONDS",
            0.005,
        ), mock.patch.object(
            self.fixture.ledger,
            "renew_operator_capacity_reservation",
            wraps=renew,
        ) as renew_spy:
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertGreaterEqual(renew_spy.call_count, 1)
        capacity = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                terminal.plan.backend_realm, terminal.job_id
            ),
        )
        self.assertGreaterEqual(capacity.heartbeat_revision, 1)
        self.assertEqual(
            capacity.state, OperatorCapacityReservationState.RELEASED
        )

    def test_lost_capacity_stops_worker_and_records_a_bounded_failure(self) -> None:
        queued = self._plan("operator-service/capacity-lost")
        with mock.patch(
            "optpilot.realm.operator_job_service."
            "_CAPACITY_HEARTBEAT_MAX_INTERVAL_SECONDS",
            0.005,
        ), mock.patch.object(
            self.fixture.ledger,
            "renew_operator_capacity_reservation",
            side_effect=RealmCapacityUnavailable("capacity fenced"),
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_capacity_lost"
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_capacity_fenced_during_provider_start_cannot_commit_success(self) -> None:
        queued = self._plan("operator-service/capacity-fenced-at-start")
        start = self.launcher.start_noncanonical

        def start_after_fence(**kwargs):
            self.fixture.ledger.ensure_operator_capacity_pool(
                operation_id="operator-service/capacity/fence-at-start",
                actor_principal_id="operator",
                pool_name="local-host",
                limits={
                    "cpu_millis": 4000,
                    "gpu_count": 0,
                    "memory_bytes": 8 * 1024**3,
                },
            )
            return start(**kwargs)

        with mock.patch.object(
            self.launcher,
            "start_noncanonical",
            side_effect=start_after_fence,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_capacity_lost"
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        with sqlite3.connect(self.supervisor.database_path) as connection:
            retired = connection.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (terminal.launch_intent.launch_token,),
            ).fetchone()[0]
        self.assertEqual(retired, 1)
        self.assertEqual(
            self.fixture.ledger.read_operator_capacity_pool(
                pool_name="local-host"
            ).state.value,
            "ready",
        )

    def test_capacity_fenced_at_running_commit_is_classified_as_capacity_loss(self) -> None:
        queued = self._plan("operator-service/capacity-fenced-at-running")
        mark_running = self.fixture.ledger.mark_operator_job_running
        fenced = False

        def mark_running_after_fence(**kwargs):
            nonlocal fenced
            if not fenced:
                fenced = True
                self.fixture.ledger.ensure_operator_capacity_pool(
                    operation_id="operator-service/capacity/fence-at-running",
                    actor_principal_id="operator",
                    pool_name="local-host",
                    limits={
                        "cpu_millis": 4000,
                        "gpu_count": 0,
                        "memory_bytes": 8 * 1024**3,
                    },
                )
            return mark_running(**kwargs)

        with mock.patch.object(
            self.fixture.ledger,
            "mark_operator_job_running",
            side_effect=mark_running_after_fence,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertTrue(fenced)
        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_capacity_lost"
        )
        self.assertEqual(terminal.result.result.details["stage"], "capacity")
        self.assertEqual(
            terminal.result.result.details["failure_type"],
            "RealmCapacityUnavailable",
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_capacity_fenced_at_success_commit_is_downgraded_to_failure(self) -> None:
        queued = self._plan("operator-service/capacity-fenced-at-finish")
        finish = self.fixture.ledger.finish_operator_job
        fenced = False

        def finish_after_fence(**kwargs):
            nonlocal fenced
            if not fenced and kwargs["outcome"].status.value == "succeeded":
                fenced = True
                self.fixture.ledger.ensure_operator_capacity_pool(
                    operation_id="operator-service/capacity/fence-at-finish",
                    actor_principal_id="operator",
                    pool_name="local-host",
                    limits={
                        "cpu_millis": 4000,
                        "gpu_count": 0,
                        "memory_bytes": 8 * 1024**3,
                    },
                )
            return finish(**kwargs)

        with mock.patch.object(
            self.fixture.ledger,
            "finish_operator_job",
            side_effect=finish_after_fence,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertTrue(fenced)
        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_capacity_lost"
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_invalid_capacity_on_restart_retires_passive_launch(self) -> None:
        queued = self._plan("operator-service/restart-capacity-fenced")
        with mock.patch.object(
            self.launcher,
            "start_noncanonical",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)

        starting = self.service.read(job_id=queued.job_id)
        self.assertEqual(starting.state, OperatorJobState.STARTING)
        self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="operator-service/capacity/fence-before-restart",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={
                "cpu_millis": 4000,
                "gpu_count": 0,
                "memory_bytes": 8 * 1024**3,
            },
        )

        restarted = self._service()
        terminal = restarted.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_capacity_lost"
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        with sqlite3.connect(self.supervisor.database_path) as connection:
            retired = connection.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (starting.launch_intent.launch_token,),
            ).fetchone()[0]
        self.assertEqual(retired, 1)

    def test_prestart_failure_releases_capacity_and_retry_reacquires(self) -> None:
        queued = self._plan("operator-service/prestart-capacity-release")
        with mock.patch.object(
            self.service,
            "_ensure_admission",
            side_effect=RuntimeError("prestart failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "prestart failed"):
                self.service.execute(job_id=queued.job_id)

        pending = self.service.read(job_id=queued.job_id)
        self.assertEqual(pending.state, OperatorJobState.QUEUED)
        released = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=operator_capacity_reservation_id(
                pending.plan.backend_realm, pending.job_id
            ),
        )
        self.assertEqual(
            released.state, OperatorCapacityReservationState.RELEASED
        )

        terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        reacquired = self.fixture.ledger.read_operator_capacity_reservation(
            actor_principal_id="operator",
            reservation_id=released.reservation_id,
        )
        self.assertEqual(reacquired.generation, released.generation + 1)
        self.assertEqual(
            reacquired.state, OperatorCapacityReservationState.RELEASED
        )

    def test_planned_job_executes_without_reresolving_the_source_run(self) -> None:
        queued = self._plan("operator-service/source-independent")
        with mock.patch.object(
            RealmInspectionTargetService,
            "resolve_candidate",
            side_effect=AssertionError("source selection was resolved again"),
        ):
            terminal = self.service.execute(job_id=queued.job_id)
            replayed_plan = self.service.plan_candidate_debug_run(
                operation_id="operator-service/source-independent",
                selection=self.selection,
            )

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(dict(terminal.result.result.metrics), {"score": 0.5})
        self.assertEqual(replayed_plan, terminal)

    def test_terminal_replay_retries_cleanup_after_commit_crash(self) -> None:
        queued = self._plan("operator-service/cleanup-replay")
        with mock.patch.object(
            self.service,
            "_cleanup_after_terminal",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)

        committed = self.service.read(job_id=queued.job_id)
        self.assertEqual(committed.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(committed.cleanup_state, OperatorJobCleanupState.PENDING)
        self.assertEqual(
            self.service.list_for_run(
                source_owner_id=self.selection.source_owner_id,
                run_id=self.selection.source_id,
                cleanup_states=(OperatorJobCleanupState.PENDING,),
            ),
            (committed,),
        )
        with sqlite3.connect(self.supervisor.database_path) as connection:
            retired_before = connection.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (committed.launch_intent.launch_token,),
            ).fetchone()[0]
        self.assertEqual(retired_before, 0)

        replayed = self.service.execute(job_id=queued.job_id)

        self.assertEqual(replayed.state, committed.state)
        self.assertEqual(replayed.outcome, committed.outcome)
        self.assertEqual(replayed.result, committed.result)
        self.assertEqual(replayed.revision, committed.revision + 1)
        self.assertEqual(replayed.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(
            self.service.list_for_run(
                source_owner_id=self.selection.source_owner_id,
                run_id=self.selection.source_id,
                cleanup_states=(OperatorJobCleanupState.PENDING,),
            ),
            (),
        )
        with sqlite3.connect(self.supervisor.database_path) as connection:
            retired_after = connection.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (committed.launch_intent.launch_token,),
            ).fetchone()[0]
        self.assertEqual(retired_after, 1)

    def test_restart_commits_cleanup_after_external_release_lost_response(self) -> None:
        queued = self._plan("operator-service/cleanup-receipt-cut")
        with mock.patch.object(
            self.fixture.ledger,
            "complete_operator_job_cleanup",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)

        pending = self.service.read(job_id=queued.job_id)
        self.assertEqual(pending.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(pending.cleanup_state, OperatorJobCleanupState.PENDING)
        with sqlite3.connect(self.supervisor.database_path) as connection:
            retired = connection.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (pending.launch_intent.launch_token,),
            ).fetchone()[0]
        self.assertEqual(retired, 1)

        restarted = self._service()
        completed = restarted.execute(job_id=pending.job_id)
        self.assertEqual(completed.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(completed.revision, pending.revision + 1)
        self.assertEqual(restarted.execute(job_id=pending.job_id), completed)

    def test_restart_after_durable_launch_intent_starts_exact_passive_reservation(self) -> None:
        queued = self._plan("operator-service/recover-passive")
        with mock.patch.object(
            self.launcher,
            "start_noncanonical",
            side_effect=_SimulatedServiceCrash(),
        ):
            with self.assertRaises(_SimulatedServiceCrash):
                self.service.execute(job_id=queued.job_id)

        starting = self.service.read(job_id=queued.job_id)
        self.assertEqual(starting.state, OperatorJobState.STARTING)
        self.assertIsNotNone(starting.launch_intent)
        restarted = self._service()
        terminal = restarted.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(dict(terminal.result.result.metrics), {"score": 0.5})
        self.assertEqual(
            restarted.execute(job_id=queued.job_id), terminal
        )

    def test_queued_stop_seals_launch_without_realizing_resources(self) -> None:
        queued = self._plan("operator-service/stop-queued")
        terminal = self.service.request_stop(
            operation_id="operator-service/stop-queued/request",
            job_id=queued.job_id,
        )

        self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
        self.assertIsNone(terminal.launch_intent)
        seal = self.launcher.seal_noncanonical_launch_if_absent(
            launch_token=self.service._context_for_record(terminal).launch_token,
            binding_id=self.service._context_for_record(terminal).binding_id,
        )
        self.assertTrue(seal.sealed)
        self.assertEqual(seal.prior_state, "sealed")
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            projection_count = connection.execute(
                "SELECT COUNT(*) FROM projection_consumers "
                "WHERE consumer_kind = 'operator-job-attempt'"
            ).fetchone()[0]
            volume_count = connection.execute(
                "SELECT COUNT(*) FROM ephemeral_volumes"
            ).fetchone()[0]
        self.assertEqual(projection_count, 0)
        self.assertEqual(volume_count, 0)

    def test_running_stop_converges_both_callers_on_one_cancelled_result(self) -> None:
        slow = _RetainedRuntimeFixture(evaluation_delay_seconds=5.0)
        self.addCleanup(slow.close)
        original = self.fixture
        original_root = self.supervisor_root
        self.fixture = slow
        self.supervisor_root = slow.root / "operator-job-provider"
        try:
            service = self._service()
            selection = self._selection()
            queued = service.plan_candidate_debug_run(
                operation_id="operator-service/stop-running",
                selection=selection,
            )
            outcomes: list[object] = []

            def execute() -> None:
                try:
                    outcomes.append(service.execute(job_id=queued.job_id))
                except BaseException as error:  # test captures thread failures
                    outcomes.append(error)

            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                current = service.read(job_id=queued.job_id)
                if current.state is OperatorJobState.RUNNING:
                    break
                time.sleep(0.02)
            else:
                self.fail("Debug Run did not reach running state.")

            terminal = service.request_stop(
                operation_id="operator-service/stop-running/request",
                job_id=queued.job_id,
            )
            thread.join(timeout=10.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
            self.assertEqual(terminal.result.result.status, "cancelled")
            self.assertEqual(len(outcomes), 1)
            self.assertFalse(isinstance(outcomes[0], BaseException), outcomes[0])
            self.assertEqual(outcomes[0], terminal)
        finally:
            self.fixture = original
            self.supervisor_root = original_root

    def test_starting_stop_converges_executor_after_resources_are_released(self) -> None:
        queued = self._plan("operator-service/stop-starting")
        entered = threading.Event()
        release = threading.Event()
        original_start = self.launcher.start_noncanonical
        original_realize = self.service._attempt_binder.realize
        outcomes: list[object] = []
        bindings = []

        def capture_binding(**kwargs):
            binding = original_realize(**kwargs)
            bindings.append(binding)
            return binding

        def paused_start(**kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=10.0))
            return original_start(**kwargs)

        def execute() -> None:
            try:
                outcomes.append(self.service.execute(job_id=queued.job_id))
            except BaseException as error:
                outcomes.append(error)

        with mock.patch.object(
            self.launcher, "start_noncanonical", side_effect=paused_start
        ), mock.patch.object(
            self.service._attempt_binder,
            "realize",
            side_effect=capture_binding,
        ):
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(timeout=10.0))
            self.assertEqual(
                self.service.read(job_id=queued.job_id).state,
                OperatorJobState.STARTING,
            )
            terminal = self.service.request_stop(
                operation_id="operator-service/stop-starting/request",
                job_id=queued.job_id,
            )
            release.set()
            thread.join(timeout=10.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(terminal.state, OperatorJobState.CANCELLED)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(isinstance(outcomes[0], BaseException), outcomes[0])
        self.assertEqual(outcomes[0], terminal)
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0].released)

    def test_resource_lease_covers_timeout_and_restart_margin(self) -> None:
        queued = self._plan("operator-service/resource-ttl")
        captured: list[float] = []
        original_realize = self.service._attempt_binder.realize

        def capture_ttl(**kwargs):
            captured.append(float(kwargs["ttl_seconds"]))
            return original_realize(**kwargs)

        with mock.patch.object(
            self.service._attempt_binder, "realize", side_effect=capture_ttl
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        self.assertEqual(len(captured), 1)
        self.assertGreaterEqual(
            captured[0], queued.plan.timeout_seconds + 3600.0
        )

    def test_duplicate_content_outputs_share_membership_and_retain_bounded_logs(
        self,
    ) -> None:
        self._use_output_fixture()
        queued = self._plan("operator-service/duplicate-output-and-logs")

        terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.SUCCEEDED)
        outputs = terminal.result.result.declared_outputs
        self.assertEqual(
            {item.declaration_id for item in outputs},
            {"environment:primary", "environment:alias"},
        )
        self.assertEqual(len({item.content_ref for item in outputs}), 1)
        memberships = self.fixture.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=terminal.owner_id
        )
        retained_outputs = tuple(
            item for item in memberships if item.role == "operator-job-output"
        )
        self.assertEqual(len(retained_outputs), 1)
        logs = {item.stream: item for item in terminal.result.result.logs}
        self.assertEqual(set(logs), {"stdout", "stderr"})
        self.assertGreater(logs["stdout"].byte_count, 64 * 1024)
        self.assertEqual(logs["stdout"].line_count, 2)
        self.assertTrue(logs["stdout"].truncated)
        self.assertEqual(logs["stderr"].line_count, 1)
        self.assertFalse(logs["stderr"].truncated)
        self.assertEqual(len(logs["stdout"].content_digest), 64)

        restarted = self._service()
        replayed = restarted.read(job_id=terminal.job_id)
        self.assertEqual(replayed.result.result.declared_outputs, outputs)
        self.assertEqual(replayed.result.result.logs, terminal.result.result.logs)

    def test_capture_failure_aborts_output_change_and_releases_holds(self) -> None:
        self._use_output_fixture()
        queued = self._plan("operator-service/capture-failure-cleanup")
        original = self.service._finalizer.capture_declared_outputs

        def capture_then_fail(**kwargs):
            captured = original(**kwargs)
            self.assertEqual(len(captured), 2)
            raise RuntimeError("injected capture failure")

        with mock.patch.object(
            self.service._finalizer,
            "capture_declared_outputs",
            side_effect=capture_then_fail,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(terminal.outcome.outcome.code, "output_capture_failed")
        self._assert_capture_aborted_without_live_holds(
            job_id=terminal.job_id, owner_id=terminal.owner_id
        )

    def test_result_construction_failure_aborts_capture_before_fallback(self) -> None:
        self._use_output_fixture()
        queued = self._plan("operator-service/result-failure-cleanup")

        with mock.patch(
            "optpilot.realm.operator_job_service._declared_output",
            side_effect=RuntimeError("injected result construction failure"),
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self.assertEqual(
            terminal.outcome.outcome.code, "operator_job_execution_failed"
        )
        self._assert_capture_aborted_without_live_holds(
            job_id=terminal.job_id, owner_id=terminal.owner_id
        )

    def test_terminal_commit_failure_aborts_capture_before_fallback(self) -> None:
        self._use_output_fixture()
        queued = self._plan("operator-service/commit-failure-cleanup")
        original = self.fixture.ledger.finish_operator_job
        failed = False

        def fail_first_output_commit(**kwargs):
            nonlocal failed
            if not failed and kwargs["change_id"] == _output_change_id(queued.job_id):
                failed = True
                raise RuntimeError("injected terminal commit failure")
            return original(**kwargs)

        with mock.patch.object(
            self.fixture.ledger,
            "finish_operator_job",
            side_effect=fail_first_output_commit,
        ):
            terminal = self.service.execute(job_id=queued.job_id)

        self.assertTrue(failed)
        self.assertEqual(terminal.state, OperatorJobState.FAILED)
        self._assert_capture_aborted_without_live_holds(
            job_id=terminal.job_id, owner_id=terminal.owner_id
        )

    def test_preview_terminal_change_advances_after_expiry_and_replays(self) -> None:
        queued, running, _capacity, admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "expired-change-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("retained\n", encoding="utf-8")
        descriptor.control_file.write_text(
            json.dumps(
                {
                    "schema_version": "optpilot.interface.output.v1",
                    "id": "expired-change-tree",
                    "label": "Expired change tree",
                    "kind": "tree",
                    "root": "output",
                    "path": "expired-change-tree",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        terminal_proof = self.service._container_web_provider.stop(managed.request)

        # Simulate a supervisor that closed the output session, began the
        # terminal owner change, held the retained output, and then crashed.
        # Its immutable begin/hold receipts must not pin every later retry to
        # this generation after the retention lease expires.
        handle, statuses, _diagnostics, _truncated = (
            self.service._final_preview_output_state(
                running,
                terminal=terminal_proof,
            )
        )
        ready = tuple(
            status.ready_generation
            for status in statuses
            if status.ready_generation is not None
        )
        additions = _preview_output_memberships(ready)
        base_change_id = _preview_output_change_id(queued.job_id, "failed")
        stale = self.service._begin_terminal_owner_change(
            running,
            operation_phase="preview-output-change/failed",
            base_change_id=base_change_id,
        )
        self.assertEqual(stale.change_id, base_change_id)
        held = self.fixture.ledger.hold_owner_content(
            operation_id=_operation(
                queued.job_id,
                "preview-output-hold/failed/"
                f"{request_digest({'change_id': stale.change_id})[:16]}",
            ),
            actor_principal_id="operator",
            change_id=stale.change_id,
            memberships=additions,
            source_owner_id=handle.session.owner_id,
        )
        self.assertEqual(tuple(held), additions)
        with mock.patch(
            "optpilot.realm.ledger.time.time",
            return_value=stale.expires_at + 1.0,
        ):
            self.fixture.ledger.sweep_expired_leases(
                operation_id=(
                    f"operator-service/{queued.job_id}/expire-terminal-change"
                )
            )

        finished = self.service._finish_preview_observation(
            self.service.read(job_id=queued.job_id),
            terminal=terminal_proof,
            managed=managed,
            admission=admission,
        )

        self.assertEqual(finished.state, OperatorJobState.FAILED)
        self.assertEqual(finished.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(
            [item.declaration_id for item in finished.result.result.declared_outputs],
            ["expired-change-tree"],
        )
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            changes = connection.execute(
                "SELECT change_id, state FROM owner_transactions "
                "WHERE owner_id = ? ORDER BY created_at, change_id",
                (running.owner_id,),
            ).fetchall()
        self.assertIn((base_change_id, "expired"), changes)
        self.assertEqual(
            sum(
                change_id != base_change_id and state == "committed"
                for change_id, state in changes
            ),
            1,
        )
        replayed = self.service.execute(job_id=queued.job_id)
        self.assertEqual(replayed, finished)
        self.assertEqual(replayed.cleanup_state, OperatorJobCleanupState.COMPLETE)

    def test_preview_pending_retry_detaches_only_local_binding_resources(self) -> None:
        queued, _running, _capacity, _admission, managed = (
            self._prepare_preview_launch(
                running=True,
                expire_admission=False,
            )
        )
        descriptor = managed.output_capture_descriptor
        generated = descriptor.source_root / "pending-retry-tree"
        generated.mkdir()
        (generated / "result.txt").write_text("ready\n", encoding="utf-8")
        payload = {
            "schema_version": "optpilot.interface.output.v1",
            "id": "pending-retry-tree",
            "label": "Pending retry tree",
            "kind": "tree",
            "root": "output",
            "path": "pending-retry-tree",
        }
        descriptor.control_file.write_text(
            json.dumps(payload) + "\n",
            encoding="utf-8",
        )
        record = InterfaceOutputRecord.from_dict(payload)
        output_service = self.service._preview_output_service()
        handle = output_service.recover_session(launch_id=queued.job_id)
        self.fixture.ledger.begin_interface_output_capture(
            operation_id=f"operator-service/{queued.job_id}/pending-retry-begin",
            actor_principal_id="operator",
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
            record=record,
            attempt_ttl_seconds=60,
            attempt_id="ioa-pending-retry",
            operation_prefix="iop-pending-retry",
        )
        self.service._container_web_provider.stop(managed.request)
        realize = self.service._realize_preview_binding
        recovered_bindings = []

        def capture_binding(**kwargs):
            binding = realize(**kwargs)
            recovered_bindings.append(binding)
            return binding

        with mock.patch.object(
            self.service,
            "_realize_preview_binding",
            side_effect=capture_binding,
        ), mock.patch.object(
            output_service,
            "resume_generation",
            side_effect=RealmConflict("another adopter is sealing"),
        ):
            for _ in range(3):
                with self.assertRaises(EnvironmentPreviewFinalCapturePending):
                    self.service.execute(job_id=queued.job_id)
                self.assertTrue(recovered_bindings[-1].released)

        self.assertEqual(len(recovered_bindings), 3)
        self.assertTrue(descriptor.source_root.exists())
        finished = self.service.execute(job_id=queued.job_id)
        self.assertTrue(finished.state.terminal)
        self.assertEqual(finished.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(
            [item.declaration_id for item in finished.result.result.declared_outputs],
            ["pending-retry-tree"],
        )


if __name__ == "__main__":
    unittest.main()
