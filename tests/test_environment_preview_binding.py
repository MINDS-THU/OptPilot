from __future__ import annotations

import hashlib
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from optpilot.realm._validation import thaw_json
from optpilot.realm.attempt_finalizer import RealmAttemptFinalizer
from optpilot.realm.environment_preview import EnvironmentPreviewPlan
from optpilot.realm.environment_preview_binding import (
    ENVIRONMENT_PREVIEW_PIDS_LIMIT,
    EnvironmentPreviewBindingEvidence,
    EnvironmentPreviewCleanupEvidence,
    RealmEnvironmentPreviewBinder,
    _projection_spec,
    _validate_retained_prepared_layers,
)
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.inspection_service import RealmInspectionTargetService
from optpilot.realm.local_attempt_launcher import RealmLocalAttemptLauncher
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
    LocalContainerWebTerminal,
)
from optpilot.realm.local_process_supervisor import LocalProcessSupervisor
from optpilot.realm.operator_attempt_binding import RealmOperatorAttemptBinder
from optpilot.realm.operator_job_service import RealmOperatorJobService
from optpilot.realm.operator_job_records import (
    OperatorJobOutcome,
    OperatorJobResult,
    OperatorJobTerminalDisposition,
    OperatorJobTerminalStatus,
)
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.ephemeral_volume_service import RealmEphemeralVolumeService
from optpilot.realm.run_closure import RunEvaluationClosure
from optpilot.realm.refs import request_digest
from tests.test_realm_local_attempt_launcher import _RetainedRuntimeFixture


_IMAGE = "example/viewer@sha256:" + "c" * 64
_INTERFACE = f"""\
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
      image: {_IMAGE}
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


@unittest.skipUnless(os.name == "posix", "local container binding requires POSIX")
class EnvironmentPreviewBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture(environment_interface=_INTERFACE)
        self.addCleanup(self.fixture.close)
        principal = self.fixture.ledger.register_principal(
            operation_id="local-attempt/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.fixture.ledger.ensure_operator_capacity_pool(
            operation_id="preview-binding/capacity/local-host",
            actor_principal_id="operator",
            pool_name="local-host",
            limits={
                "cpu_millis": 8000,
                "gpu_count": 0,
                "memory_bytes": 16 * 1024**3,
            },
        )
        self.inspection = RealmInspectionTargetService(
            self.fixture.ledger, principal
        )
        supervisor = LocalProcessSupervisor(self.fixture.root / "preview-process")
        launcher = RealmLocalAttemptLauncher(supervisor)
        attempt_binder = RealmOperatorAttemptBinder(
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
        self.service = RealmOperatorJobService(
            self.fixture.ledger,
            principal,
            self.inspection,
            self.fixture.provider,
            attempt_binder,
            launcher,
            finalizer,
        )
        snapshot = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
        )
        selection = self.fixture.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            kind="candidate",
            entity_id="candidate-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        self.job = self.service.plan_environment_preview(
            operation_id="preview-binding/job",
            selection=selection,
        )
        self.target = self.inspection.resolve_candidate(selection=selection)
        self.preview_plan = EnvironmentPreviewPlan.from_dict(
            thaw_json(self.job.plan.input_facts["preview_plan"])
        )
        self.capacity = self.service._ensure_capacity(self.job)
        self.admission = self.service._ensure_admission(self.job)
        self.provider = LocalContainerWebProvider(
            executable="docker",
            control_root=self.fixture.root / "preview-control",
            broker_authority=object(),
            trusted_gateway_images=(ContainerGatewayImageTrust(_IMAGE),),
        )
        self.binder = RealmEnvironmentPreviewBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
            self.provider,
        )

    def arguments(self) -> dict[str, object]:
        return {
            "actor_principal_id": "operator",
            "job_id": self.job.job_id,
            "owner_id": self.job.owner_id,
            "admission_lease": self.admission,
            "operator_plan_digest": self.job.plan_digest,
            "binding_id": "preview-binding-a",
            "launch_token": "preview-launch-a",
            "target": self.target,
            "preview_plan": self.preview_plan,
        }

    def terminalize(
        self,
        *,
        binding,
        terminal: LocalContainerWebTerminal,
    ) -> None:
        starting = self.fixture.ledger.begin_operator_job_start(
            operation_id="preview-binding/cleanup-start",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            admission_lease_id=self.admission.lease_id,
            admission_holder_id=self.admission.holder_id,
            admission_fencing_token=self.admission.fencing_token,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            provider_kind="local-container-web",
            evidence_fingerprint=binding.evidence.digest,
            launch_request_digest=binding.request.digest,
        )
        owner = self.fixture.ledger.read_owner(
            actor_principal_id="operator", owner_id=self.job.owner_id
        )
        change = self.fixture.ledger.begin_owner_change(
            operation_id="preview-binding/cleanup-capture",
            actor_principal_id="operator",
            owner_id=self.job.owner_id,
            expected_owner_revision=owner.revision,
            ttl_seconds=300,
        )
        details = {"disposition": "injected-terminal"}
        result = OperatorJobResult(
            result_kind="environment-preview",
            status="failed",
            metrics={},
            constraint_results={},
            event_summary={},
            declared_outputs=(),
            logs=(),
            details=details,
        )
        outcome = OperatorJobOutcome(
            status=OperatorJobTerminalStatus.FAILED,
            code="injected-terminal",
            started=False,
            disposition=OperatorJobTerminalDisposition.NEVER_STARTED,
            terminal_proof_digest=hashlib.sha256(
                terminal.canonical_bytes
            ).hexdigest(),
            evidence_digest=result.digest,
            detail_digest=request_digest(details),
        )
        self.fixture.ledger.finish_operator_job(
            operation_id="preview-binding/cleanup-finish",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=starting.revision,
            launch_token=binding.request.launch_token,
            admission_lease_id=self.admission.lease_id,
            admission_fencing_token=self.admission.fencing_token,
            change_id=change.change_id,
            expected_owner_revision=owner.revision,
            additions=(),
            outcome=outcome,
            result=result,
        )

    def launch_and_release_admission(self, *, binding) -> LocalContainerWebTerminal:
        self.fixture.ledger.begin_operator_job_start(
            operation_id="preview-binding/terminal-output-start",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            admission_lease_id=self.admission.lease_id,
            admission_holder_id=self.admission.holder_id,
            admission_fencing_token=self.admission.fencing_token,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            provider_kind="local-container-web",
            evidence_fingerprint=binding.evidence.digest,
            launch_request_digest=binding.request.digest,
        )
        self.fixture.ledger.release_lease(
            operation_id="preview-binding/terminal-output-release-admission",
            actor_principal_id="operator",
            lease_id=self.admission.lease_id,
            holder_id=self.admission.holder_id,
            fencing_token=self.admission.fencing_token,
        )
        return LocalContainerWebTerminal(
            job_id=binding.request.job_id,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            launch_request_digest=binding.request.digest,
            container_id="preview-container-a",
            exit_code=0,
            disposition="exited",
        )

    def test_realize_and_restart_recover_exact_request_and_path_free_evidence(self) -> None:
        first = self.binder.realize(**self.arguments())
        first.validate()
        request = first.request

        self.assertEqual(request.portable_plan_digest, self.job.plan_digest)
        self.assertEqual(request.image_ref, _IMAGE)
        self.assertEqual(request.platform, "linux/amd64")
        self.assertEqual(request.command, ("python", "-m", "local_package.viewer"))
        self.assertEqual(request.workdir, "/optpilot/interface/app")
        self.assertEqual(request.ports, (5173, 5174))
        self.assertEqual(request.primary_port, 5173)
        self.assertEqual(request.ready_path, "/ready")
        self.assertEqual(request.ready_timeout_seconds, 10)
        self.assertEqual(request.network_policy, "denied")
        self.assertEqual(request.run_identity.uid, os.geteuid())
        self.assertEqual(request.run_identity.gid, os.getegid())
        self.assertEqual(request.pids_limit, ENVIRONMENT_PREVIEW_PIDS_LIMIT)
        self.assertEqual(
            {item.container_path: item.mode for item in request.mounts},
            {
                "/optpilot/interface/app": "read-only",
                "/optpilot/interface/artifacts": "read-only",
                "/optpilot/interface/context.json": "read-only",
                "/optpilot/interface/control/outputs.jsonl": "read-write",
                "/optpilot/interface/output": "read-write",
                "/optpilot/interface/prepared_outputs": "read-only",
                "/optpilot/interface/runtime_env": "read-only",
                "/optpilot/interface/workspace": "read-write",
            },
        )
        context_mount = next(
            item
            for item in request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        self.assertEqual(context_mount.host_path.read_bytes(), self.preview_plan.context.canonical_bytes)
        output_mount = next(
            item
            for item in request.mounts
            if item.container_path == "/optpilot/interface/output"
        )
        control_mount = next(
            item
            for item in request.mounts
            if item.container_path == "/optpilot/interface/control/outputs.jsonl"
        )
        self.assertTrue(output_mount.host_path.is_dir())
        self.assertTrue(control_mount.host_path.is_file())
        self.assertEqual(control_mount.host_path.read_bytes(), b"")
        self.assertNotEqual(output_mount.host_path, control_mount.host_path.parent)
        descriptor = first.output_capture_descriptor
        self.assertEqual(descriptor.source_root, output_mount.host_path)
        self.assertEqual(descriptor.control_file, control_mount.host_path)
        self.assertEqual(
            set(vars(descriptor)),
            {"source_root", "control_file"},
        )
        self.assertFalse(hasattr(descriptor, "to_dict"))
        workspace_mount = next(
            item
            for item in request.mounts
            if item.container_path == "/optpilot/interface/workspace"
        )
        self.assertNotEqual(descriptor.source_root, workspace_mount.host_path)
        replay = self.binder.realize(**self.arguments())
        self.assertEqual(replay.request.digest, request.digest)
        self.assertEqual(replay.evidence, first.evidence)

        # Interface declarations may append to the exact private file without
        # changing the provider binding or its restart identity.
        control_mount.host_path.write_text("{}\n", encoding="utf-8")

        restarted_projection = RealmProjectionService(
            self.fixture.ledger,
            local_stores={self.fixture.store.store_id: self.fixture.store},
            projection_root=self.fixture.root / "projections",
        )
        restarted_volume = RealmEphemeralVolumeService(
            self.fixture.ledger,
            volume_root=self.fixture.root / "volumes",
        )
        recovered = RealmEnvironmentPreviewBinder(
            self.fixture.ledger,
            restarted_projection,
            restarted_volume,
            self.provider,
        ).recover_existing(**self.arguments())
        recovered.validate()
        self.assertEqual(recovered.request.digest, request.digest)
        self.assertEqual(recovered.evidence, first.evidence)
        self.assertEqual(
            recovered.output_capture_descriptor,
            descriptor,
        )
        self.assertEqual(
            EnvironmentPreviewBindingEvidence.from_dict(first.evidence.to_dict()),
            first.evidence,
        )
        encoded = json.dumps(first.evidence.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.fixture.root), encoded)
        self.assertNotIn("host_path", encoded)
        self.assertNotIn("run_identity", encoded)
        self.assertNotIn("platform", encoded)

    def test_recover_missing_creates_nothing_and_stale_admission_fails_before_realization(self) -> None:
        with mock.patch.object(
            self.fixture.projection_service,
            "project_read_only",
            wraps=self.fixture.projection_service.project_read_only,
        ) as create_projection, mock.patch.object(
            self.fixture.volume_service,
            "create",
            wraps=self.fixture.volume_service.create,
        ) as create_volume:
            with self.assertRaises(RealmNotFound):
                self.binder.recover_existing(**self.arguments())
            create_projection.assert_not_called()
            create_volume.assert_not_called()

        self.fixture.ledger.heartbeat_lease(
            operation_id="preview-binding/admission-heartbeat",
            actor_principal_id="operator",
            lease_id=self.admission.lease_id,
            holder_id=self.admission.holder_id,
            fencing_token=self.admission.fencing_token,
            ttl_seconds=300,
        )
        with mock.patch.object(
            self.fixture.projection_service,
            "project_read_only",
            wraps=self.fixture.projection_service.project_read_only,
        ) as create_projection:
            with self.assertRaisesRegex(RealmConflict, "admission authority"):
                self.binder.realize(**self.arguments())
            create_projection.assert_not_called()

    def test_recovery_rejects_context_tamper_and_symlinks(self) -> None:
        binding = self.binder.realize(**self.arguments())
        context = next(
            item.host_path
            for item in binding.request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        context.chmod(0o600)
        context.write_bytes(b"{}")
        with self.assertRaisesRegex(RealmIntegrityError, "context"):
            self.binder.recover_existing(**self.arguments())

        # Restore the exact file, then prove that a symlink in any mounted
        # subdirectory is rejected by the managed-volume identity check.
        context.write_bytes(self.preview_plan.context.canonical_bytes)
        context.chmod(0o400)
        runtime_env = next(
            item.host_path
            for item in binding.request.mounts
            if item.container_path == "/optpilot/interface/runtime_env"
        )
        runtime_env.rmdir()
        runtime_env.symlink_to(self.fixture.root, target_is_directory=True)
        with self.assertRaises(RealmIntegrityError):
            self.binder.recover_existing(**self.arguments())

    def test_recovery_rejects_output_control_symlink_and_regular_replacement(self) -> None:
        binding = self.binder.realize(**self.arguments())
        control = binding.output_capture_descriptor.control_file
        control.unlink()
        control.symlink_to(self.fixture.root / "outside-control.jsonl")
        with self.assertRaises(RealmIntegrityError):
            self.binder.recover_existing(**self.arguments())

    def test_recovery_rejects_output_control_file_identity_replacement(self) -> None:
        binding = self.binder.realize(**self.arguments())
        control = binding.output_capture_descriptor.control_file
        control.unlink()
        control.write_text("{}\n", encoding="utf-8")
        control.chmod(0o600)
        with self.assertRaisesRegex(RealmIntegrityError, "identity was replaced"):
            self.binder.recover_existing(**self.arguments())

    def test_recovery_rejects_output_control_directory_identity_replacement(self) -> None:
        binding = self.binder.realize(**self.arguments())
        control = binding.output_capture_descriptor.control_file
        old_directory = self.fixture.root / "replaced-output-control"
        control.parent.rename(old_directory)
        control.parent.mkdir(mode=0o700)
        (old_directory / control.name).rename(control)
        with self.assertRaisesRegex(RealmIntegrityError, "identity was replaced"):
            self.binder.recover_existing(**self.arguments())

    def test_restart_rebinds_control_observations_from_exact_layout_claim(self) -> None:
        binding = self.binder.realize(**self.arguments())
        descriptor = binding.output_capture_descriptor
        descriptor.control_file.write_text('{"path":"result.json"}\n', encoding="utf-8")
        terminal = self.launch_and_release_admission(binding=binding)
        binding.detach_for_recovery()

        # Model a filesystem reattachment assigning fresh directory and file
        # observations while preserving the exact enclosing volume and
        # nonce-bound layout claims.
        control_directory = descriptor.control_file.parent
        old_directory = self.fixture.root / "old-preview-control-attachment"
        control_bytes = descriptor.control_file.read_bytes()
        control_directory.rename(old_directory)
        control_directory.mkdir(mode=0o700)
        descriptor.control_file.write_bytes(control_bytes)
        descriptor.control_file.chmod(0o600)
        (old_directory / descriptor.control_file.name).unlink()
        old_directory.rmdir()

        arguments = self.arguments()
        arguments.pop("admission_lease")
        with self.assertRaisesRegex(RealmIntegrityError, "identity was replaced"):
            self.binder.recover_terminal_output_capture(
                **arguments,
                terminal=terminal,
            )

        restarted_projection = RealmProjectionService(
            self.fixture.ledger,
            local_stores={self.fixture.store.store_id: self.fixture.store},
            projection_root=self.fixture.root / "projections",
        )
        restarted_volume = RealmEphemeralVolumeService(
            self.fixture.ledger,
            volume_root=self.fixture.root / "volumes",
        )
        restarted_binder = RealmEnvironmentPreviewBinder(
            self.fixture.ledger,
            restarted_projection,
            restarted_volume,
            self.provider,
        )
        recovered = restarted_binder.recover_terminal_output_capture(
            **arguments,
            terminal=terminal,
        )
        self.assertEqual(recovered, descriptor)
        self.assertEqual(recovered.control_file.read_bytes(), control_bytes)

    def test_recovery_finishes_a_crash_before_layout_publication(self) -> None:
        binding = self.binder.realize(**self.arguments())
        context = next(
            item.host_path
            for item in binding.request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        volume_root = context.parents[1]
        marker = volume_root / ".environment-preview-layout"
        layout = volume_root / "preview"
        marker.unlink()
        context.unlink()
        for child in layout.iterdir():
            if child.name == "control":
                (child / "outputs.jsonl").unlink()
            child.rmdir()
        layout.rmdir()
        staging = volume_root / ".environment-preview-initializing"
        staging.mkdir()
        (staging / "partial-context").write_bytes(b"partial")

        recovered = self.binder.recover_existing(**self.arguments())
        recovered.validate()
        recovered_context = next(
            item.host_path
            for item in recovered.request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        self.assertEqual(
            recovered_context.read_bytes(),
            self.preview_plan.context.canonical_bytes,
        )
        self.assertTrue(marker.is_file())

    def test_cleanup_requires_exact_terminal_and_is_idempotent(self) -> None:
        binding = self.binder.realize(**self.arguments())
        app_path = binding.request.mounts[0].host_path
        context_path = binding.request.mounts[1].host_path
        wrong = LocalContainerWebTerminal(
            job_id=binding.request.job_id,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            launch_request_digest="f" * 64,
            container_id="none",
            exit_code=0,
            disposition="never_started",
        )
        with mock.patch.object(self.provider, "cleanup") as cleanup:
            with self.assertRaisesRegex(RealmConflict, "terminal evidence"):
                binding.release_after_terminal(wrong)
            cleanup.assert_not_called()
            self.assertTrue(app_path.exists())
            self.assertTrue(context_path.exists())

            terminal = LocalContainerWebTerminal(
                job_id=binding.request.job_id,
                binding_id=binding.request.binding_id,
                launch_token=binding.request.launch_token,
                launch_request_digest=binding.request.digest,
                container_id="none",
                exit_code=0,
                disposition="never_started",
            )
            binding.release_after_terminal(terminal)
            binding.release_after_terminal(terminal)
            cleanup.assert_called_once_with(binding.request)
        self.assertTrue(binding.released)
        self.assertFalse(app_path.exists())
        self.assertFalse(context_path.exists())

    def test_terminal_cleanup_replays_after_projection_only_partial_cleanup(self) -> None:
        binding = self.binder.realize(**self.arguments())
        app_path = binding.request.mounts[0].host_path
        context_path = next(
            item.host_path
            for item in binding.request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        terminal = LocalContainerWebTerminal(
            job_id=binding.request.job_id,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            launch_request_digest=binding.request.digest,
            container_id="none",
            exit_code=0,
            disposition="never_started",
        )
        self.terminalize(binding=binding, terminal=terminal)

        with mock.patch.object(self.provider, "cleanup") as cleanup, mock.patch.object(
            self.provider, "stop", return_value=terminal
        ) as stop:
            with mock.patch.object(
                binding._volume,
                "close",
                side_effect=RuntimeError("injected crash after projection cleanup"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    binding.release_after_terminal(terminal)
            self.assertFalse(app_path.exists())
            self.assertTrue(context_path.exists())

            evidence = self.binder.cleanup_after_terminal(
                **self.arguments(), terminal=terminal
            )
            with self.assertRaises(RealmConflict):
                binding.detach_after_terminal_cleanup(
                    replace(evidence, binding_id="different-preview-binding")
                )
            binding.detach_after_terminal_cleanup(evidence)
            self.assertTrue(binding.released)
            self.fixture.ledger.release_lease(
                operation_id="preview-binding/release-admission-before-replay",
                actor_principal_id="operator",
                lease_id=self.admission.lease_id,
                holder_id=self.admission.holder_id,
                fencing_token=self.admission.fencing_token,
            )
            replay_arguments = self.arguments()
            replay_arguments.pop("admission_lease")
            replay = self.binder.cleanup_after_terminal(**replay_arguments)

        self.assertFalse(context_path.exists())
        self.assertEqual(replay, evidence)
        self.assertEqual(
            EnvironmentPreviewCleanupEvidence.from_dict(evidence.to_dict()),
            evidence,
        )
        encoded = json.dumps(evidence.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.fixture.root), encoded)
        self.assertNotIn("host_path", encoded)
        self.assertEqual(cleanup.call_count, 3)
        stop.assert_called_once()

    def test_stop_request_recovers_after_launched_admission_is_released(self) -> None:
        binding = self.binder.realize(**self.arguments())
        self.fixture.ledger.begin_operator_job_start(
            operation_id="preview-binding/expired-stop-start",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            admission_lease_id=self.admission.lease_id,
            admission_holder_id=self.admission.holder_id,
            admission_fencing_token=self.admission.fencing_token,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            provider_kind="local-container-web",
            evidence_fingerprint=binding.evidence.digest,
            launch_request_digest=binding.request.digest,
        )
        self.fixture.ledger.release_lease(
            operation_id="preview-binding/expired-stop-release-admission",
            actor_principal_id="operator",
            lease_id=self.admission.lease_id,
            holder_id=self.admission.holder_id,
            fencing_token=self.admission.fencing_token,
        )

        arguments = self.arguments()
        arguments.pop("admission_lease")
        recovered = self.binder.recover_stop_request(**arguments)

        self.assertEqual(recovered, binding.request)
        self.assertEqual(recovered.digest, binding.request.digest)

    def test_terminal_output_capture_recovers_after_admission_release_without_mutation(self) -> None:
        binding = self.binder.realize(**self.arguments())
        live_descriptor = binding.output_capture_descriptor
        live_descriptor.source_root.joinpath("result.json").write_text(
            '{"metric": 7}\n', encoding="utf-8"
        )
        live_descriptor.control_file.write_text(
            '{"path":"result.json"}\n', encoding="utf-8"
        )
        terminal = self.launch_and_release_admission(binding=binding)
        arguments = self.arguments()
        arguments.pop("admission_lease")

        volume_root = live_descriptor.source_root.parents[1]
        marker = volume_root / ".environment-preview-layout"
        before = {
            "marker": (
                marker.stat().st_dev,
                marker.stat().st_ino,
                marker.stat().st_mtime_ns,
                marker.stat().st_ctime_ns,
            ),
            "control": (
                live_descriptor.control_file.stat().st_dev,
                live_descriptor.control_file.stat().st_ino,
                live_descriptor.control_file.stat().st_mtime_ns,
                live_descriptor.control_file.stat().st_ctime_ns,
            ),
            "root_entries": tuple(sorted(item.name for item in volume_root.iterdir())),
        }
        with mock.patch.object(
            self.fixture.projection_service, "project_read_only"
        ) as create_projection, mock.patch.object(
            self.fixture.projection_service, "recover_existing_private_read_only"
        ) as recover_projection, mock.patch.object(
            self.fixture.volume_service, "create"
        ) as create_volume, mock.patch.object(
            self.fixture.volume_service, "recover_existing"
        ) as recover_volume, mock.patch.object(
            self.fixture.volume_service, "reattach"
        ) as reattach_volume, mock.patch.object(
            self.fixture.ledger, "heartbeat_lease"
        ) as heartbeat_admission, mock.patch.object(
            self.fixture.ledger, "heartbeat_ephemeral_volume"
        ) as heartbeat_volume, mock.patch.object(
            self.fixture.ledger, "heartbeat_projection_consumer"
        ) as heartbeat_projection:
            recovered = self.binder.recover_terminal_output_capture(
                **arguments,
                terminal=terminal,
            )

        self.assertEqual(recovered, live_descriptor)
        self.assertEqual(set(vars(recovered)), {"source_root", "control_file"})
        self.assertEqual(
            recovered.source_root.joinpath("result.json").read_text(encoding="utf-8"),
            '{"metric": 7}\n',
        )
        after = {
            "marker": (
                marker.stat().st_dev,
                marker.stat().st_ino,
                marker.stat().st_mtime_ns,
                marker.stat().st_ctime_ns,
            ),
            "control": (
                recovered.control_file.stat().st_dev,
                recovered.control_file.stat().st_ino,
                recovered.control_file.stat().st_mtime_ns,
                recovered.control_file.stat().st_ctime_ns,
            ),
            "root_entries": tuple(sorted(item.name for item in volume_root.iterdir())),
        }
        self.assertEqual(after, before)
        for operation in (
            create_projection,
            recover_projection,
            create_volume,
            recover_volume,
            reattach_volume,
            heartbeat_admission,
            heartbeat_volume,
            heartbeat_projection,
        ):
            operation.assert_not_called()

    def test_terminal_output_capture_requires_exact_terminal_before_layout_access(self) -> None:
        binding = self.binder.realize(**self.arguments())
        terminal = self.launch_and_release_admission(binding=binding)
        wrong_terminal = replace(terminal, launch_request_digest="f" * 64)
        arguments = self.arguments()
        arguments.pop("admission_lease")

        with mock.patch(
            "optpilot.realm.environment_preview_binding.attach_ephemeral_volume_namespace"
        ) as attach_namespace:
            with self.assertRaisesRegex(RealmConflict, "terminal evidence"):
                self.binder.recover_terminal_output_capture(
                    **arguments,
                    terminal=wrong_terminal,
                )
        attach_namespace.assert_not_called()

    def test_terminal_output_capture_rejects_missing_layout_marker_without_repair(self) -> None:
        binding = self.binder.realize(**self.arguments())
        descriptor = binding.output_capture_descriptor
        terminal = self.launch_and_release_admission(binding=binding)
        marker = descriptor.source_root.parents[1] / ".environment-preview-layout"
        marker.unlink()
        arguments = self.arguments()
        arguments.pop("admission_lease")

        with mock.patch(
            "optpilot.realm.environment_preview_binding._prepare_or_validate_layout"
        ) as repair_layout:
            with self.assertRaisesRegex(RealmIntegrityError, "layout is incomplete"):
                self.binder.recover_terminal_output_capture(
                    **arguments,
                    terminal=terminal,
                )
        repair_layout.assert_not_called()
        self.assertFalse(marker.exists())

    def test_terminal_output_capture_rejects_control_inode_replacement(self) -> None:
        binding = self.binder.realize(**self.arguments())
        descriptor = binding.output_capture_descriptor
        terminal = self.launch_and_release_admission(binding=binding)
        descriptor.control_file.unlink()
        descriptor.control_file.write_text('{}\n', encoding="utf-8")
        descriptor.control_file.chmod(0o600)
        arguments = self.arguments()
        arguments.pop("admission_lease")

        with self.assertRaisesRegex(RealmIntegrityError, "identity was replaced"):
            self.binder.recover_terminal_output_capture(
                **arguments,
                terminal=terminal,
            )

    def test_unlaunched_cancellation_cleans_resources_after_admission_release(self) -> None:
        binding = self.binder.realize(**self.arguments())
        cancelled = self.fixture.ledger.request_operator_job_stop(
            operation_id="preview-binding/unlaunched-cancel",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            reason_code="cancelled-by-test",
        )
        self.assertIsNone(cancelled.launch_intent)
        self.fixture.ledger.release_lease(
            operation_id="preview-binding/unlaunched-release-admission",
            actor_principal_id="operator",
            lease_id=self.admission.lease_id,
            holder_id=self.admission.holder_id,
            fencing_token=self.admission.fencing_token,
        )
        terminal = LocalContainerWebTerminal(
            job_id=binding.request.job_id,
            binding_id=binding.request.binding_id,
            launch_token=binding.request.launch_token,
            launch_request_digest=binding.request.digest,
            container_id="none",
            exit_code=0,
            disposition="never_started",
        )
        arguments = self.arguments()
        arguments.pop("admission_lease")
        context_path = next(
            item.host_path
            for item in binding.request.mounts
            if item.container_path == "/optpilot/interface/context.json"
        )
        with mock.patch.object(
            self.provider, "stop", return_value=terminal
        ) as stop, mock.patch.object(self.provider, "cleanup") as cleanup:
            evidence = self.binder.cleanup_after_terminal(**arguments)
            replay = self.binder.cleanup_after_terminal(**arguments)

        self.assertEqual(replay, evidence)
        self.assertFalse(context_path.exists())
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(cleanup.call_count, 2)

    def test_unlaunched_cancellation_proves_absent_resources_with_provider_seal(self) -> None:
        cancelled = self.fixture.ledger.request_operator_job_stop(
            operation_id="preview-binding/unrealized-cancel",
            actor_principal_id="operator",
            job_id=self.job.job_id,
            expected_revision=self.job.revision,
            reason_code="cancelled-before-realization",
        )
        self.assertIsNone(cancelled.launch_intent)
        self.fixture.ledger.release_lease(
            operation_id="preview-binding/unrealized-release-admission",
            actor_principal_id="operator",
            lease_id=self.admission.lease_id,
            holder_id=self.admission.holder_id,
            fencing_token=self.admission.fencing_token,
        )
        arguments = self.arguments()
        arguments.pop("admission_lease")
        def never_started(request):
            return LocalContainerWebTerminal(
                job_id=request.job_id,
                binding_id=request.binding_id,
                launch_token=request.launch_token,
                launch_request_digest=request.digest,
                container_id="none",
                exit_code=0,
                disposition="never_started",
            )

        with mock.patch.object(
            self.provider, "stop", side_effect=never_started
        ) as stop, mock.patch.object(self.provider, "cleanup") as cleanup:
            evidence = self.binder.cleanup_after_terminal(**arguments)
            replay = self.binder.cleanup_after_terminal(**arguments)
        self.assertEqual(replay, evidence)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(cleanup.call_count, 2)

    def test_provider_validation_and_prepared_layer_attachment_fail_closed(self) -> None:
        self.binder.validate_plan(self.preview_plan)
        context = self.preview_plan.context
        wrong_engine = EnvironmentPreviewPlan.build(
            profile_id=self.preview_plan.profile_id,
            accepts=self.preview_plan.accepts,
            selection=self.preview_plan.selection,
            invocation=self.preview_plan.invocation,
            runtime=replace(self.preview_plan.runtime, engine="podman"),
            resources=self.preview_plan.resources,
            timeout_seconds=self.preview_plan.timeout_seconds,
            presentation=self.preview_plan.presentation,
            paths=self.preview_plan.paths,
            fingerprints=self.preview_plan.fingerprints,
            candidate_format=context.candidate_format,
            candidate_ref=context.candidate_ref,
            parameter_spec=thaw_json(context.parameter_spec),
            outputs_enabled=self.preview_plan.outputs_enabled,
        )
        with self.assertRaisesRegex(RealmConflict, "engine"):
            self.binder.validate_plan(wrong_engine)

        closure = self.target.evaluation.closure
        prepared_layer = closure.environment_revision.source_layers[0]
        provider_scoped = replace(
            closure.prepared_runtime,
            prepared_layers=(prepared_layer,),
            portability="provider-scoped",
        )
        provider_target = SimpleNamespace(
            evaluation=SimpleNamespace(
                closure=SimpleNamespace(prepared_runtime=provider_scoped)
            )
        )
        with self.assertRaisesRegex(RealmConflict, "provider-scoped"):
            _validate_retained_prepared_layers(
                target=provider_target, plan=self.preview_plan
            )

        wrong_platform = replace(
            provider_scoped,
            portability="portable",
            platform="linux/arm64",
        )
        platform_target = SimpleNamespace(
            evaluation=SimpleNamespace(
                closure=SimpleNamespace(prepared_runtime=wrong_platform)
            )
        )
        with self.assertRaisesRegex(RealmConflict, "platform"):
            _validate_retained_prepared_layers(
                target=platform_target, plan=self.preview_plan
            )

    def test_precedence_duplicate_mapping_and_multi_store_fail_before_allocation(self) -> None:
        def with_closure(closure: RunEvaluationClosure):
            return SimpleNamespace(evaluation=SimpleNamespace(closure=closure))

        closure = self.target.evaluation.closure
        source = closure.environment_revision.source_layers[0]
        environment = replace(
            closure.environment_revision,
            source_layers=(replace(source, precedence=1),),
        )
        runtime = replace(
            closure.prepared_runtime,
            environment_revision_digest=environment.digest,
        )
        template = replace(
            closure.evaluation_template,
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
        )
        precedence_target = with_closure(
            RunEvaluationClosure(environment, runtime, template)
        )
        with self.assertRaisesRegex(RealmConflict, "precedence"):
            _projection_spec(owner_id=self.job.owner_id, target=precedence_target)

        duplicate_runtime = replace(
            closure.prepared_runtime,
            prepared_layers=(source,),
        )
        duplicate_template = replace(
            closure.evaluation_template,
            runtime_revision_digest=duplicate_runtime.digest,
        )
        duplicate_target = with_closure(
            RunEvaluationClosure(
                closure.environment_revision,
                duplicate_runtime,
                duplicate_template,
            )
        )
        with self.assertRaisesRegex(RealmConflict, "duplicate mapping"):
            _projection_spec(owner_id=self.job.owner_id, target=duplicate_target)

        memberships = self.fixture.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=self.job.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        environment_membership = next(
            item for item in memberships if item.role == "run-environment-source"
        )
        ambiguous = (
            *memberships,
            OwnerMembership(
                store_id="remote-b",
                content_ref=environment_membership.content_ref,
                role=environment_membership.role,
            ),
        )
        with mock.patch.object(
            self.fixture.ledger,
            "list_owner_memberships",
            return_value=ambiguous,
        ), mock.patch.object(
            self.fixture.projection_service,
            "project_read_only",
            wraps=self.fixture.projection_service.project_read_only,
        ) as project:
            with self.assertRaisesRegex(RealmConflict, "exactly one"):
                self.binder.realize(**self.arguments())
            project.assert_not_called()


if __name__ == "__main__":
    unittest.main()
