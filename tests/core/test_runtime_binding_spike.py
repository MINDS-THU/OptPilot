"""Conformance checks for the disposable runtime-binding/package spike."""

from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace
from pathlib import Path

from scripts.spikes.runtime_binding_spike import (
    ArtifactSubstitution,
    CatalogPublisher,
    ContainerProvider,
    CredentialBroker,
    CredentialScopeError,
    DebugSelectionError,
    InterfaceLaunchProfile,
    InvalidPortableSpec,
    LogicalScope,
    NativeProcessProvider,
    OverlayFactory,
    PackageArtifactStore,
    PackageCompiler,
    PackagePhaseError,
    PackagePipeline,
    PortableRunSpec,
    PreviewLauncher,
    RelativeEntrypoint,
    ScopedGrant,
    ScopedSecret,
    StaleWorkspaceGeneration,
    TerminalDebugRun,
    TreePlan,
    WorkspaceRegistry,
    WorkspaceRevision,
    fake_ref,
)


class PortableRuntimeBindingSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = PortableRunSpec(
            role="environment",
            scopes=(
                LogicalScope("source", fake_ref("environment-source")),
                LogicalScope(
                    "candidate",
                    fake_ref("candidate-tree"),
                    subpath="candidate/current",
                ),
            ),
            entrypoint=RelativeEntrypoint("source", "bin/evaluate.py"),
        )

    def test_one_path_free_spec_binds_to_native_and_container_providers(self) -> None:
        native = NativeProcessProvider(Path("/realm-a/native-checkouts")).bind(
            self.spec,
            invocation_id="native-a",
        )
        container = ContainerProvider(Path("/realm-b/container-mounts")).bind(
            self.spec,
            invocation_id="container-a",
        )

        self.assertEqual(native.spec_identity, self.spec.identity)
        self.assertEqual(container.spec_identity, self.spec.identity)
        self.assertIn("/realm-a/native-checkouts", native.entrypoint_path)
        self.assertEqual(container.entrypoint_path, "/opt/optpilot/scopes/source/bin/evaluate.py")

        persisted_spec = json.dumps(self.spec.to_record(), sort_keys=True)
        native_evidence = json.dumps(native.portable_evidence(self.spec), sort_keys=True)
        container_evidence = json.dumps(container.portable_evidence(self.spec), sort_keys=True)
        for forbidden in (
            "/realm-a/native-checkouts",
            "/realm-b/container-mounts",
            "/opt/optpilot/scopes",
        ):
            self.assertNotIn(forbidden, persisted_spec)
            self.assertNotIn(forbidden, native_evidence)
            self.assertNotIn(forbidden, container_evidence)

        self.assertEqual(
            native.portable_evidence(self.spec)["logical_map"],
            container.portable_evidence(self.spec)["logical_map"],
        )
        self.assertEqual(native.provider_kind, "native-process")
        self.assertEqual(container.provider_kind, "container")

    def test_moving_native_realization_does_not_change_spec_or_evidence_identity(self) -> None:
        first = NativeProcessProvider(Path("/store-a/runtime")).bind(
            self.spec,
            invocation_id="one",
        )
        moved = NativeProcessProvider(Path("/store-z/runtime")).bind(
            self.spec,
            invocation_id="two",
        )

        self.assertNotEqual(first.entrypoint_path, moved.entrypoint_path)
        self.assertEqual(first.spec_identity, moved.spec_identity)
        self.assertEqual(
            first.portable_evidence(self.spec),
            moved.portable_evidence(self.spec),
        )

    def test_scope_declaration_order_is_not_part_of_portable_identity(self) -> None:
        reordered = PortableRunSpec(
            role=self.spec.role,
            scopes=tuple(reversed(self.spec.scopes)),
            entrypoint=self.spec.entrypoint,
        )

        self.assertEqual(reordered.identity, self.spec.identity)
        self.assertEqual(reordered.to_record(), self.spec.to_record())

    def test_absolute_and_traversing_portable_paths_are_rejected(self) -> None:
        with self.assertRaises(InvalidPortableSpec):
            RelativeEntrypoint("source", "/tmp/evaluate.py")
        with self.assertRaises(InvalidPortableSpec):
            RelativeEntrypoint("source", "../evaluate.py")
        with self.assertRaises(InvalidPortableSpec):
            LogicalScope("source", fake_ref("source"), subpath="a/../../escape")


class DebugPreviewBindingSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.overlays = OverlayFactory(Path("/realm/ephemeral-uppers"))
        self.evaluator_upper = self.overlays.create(
            logical_name="trial",
            purpose="debug-run",
        )
        self.debug_run = TerminalDebugRun(
            debug_attempt_id="debug-42",
            state="terminal",
            environment_ref=fake_ref("environment"),
            candidate_ref=fake_ref("candidate"),
            terminal_view_ref=fake_ref("sealed-debug-view"),
            evaluator_upper=self.evaluator_upper,
        )
        self.credentials = CredentialBroker(
            secrets=(
                ScopedSecret(
                    "preview-token",
                    "preview-secret-value",
                    frozenset({"preview"}),
                ),
                ScopedSecret(
                    "evaluator-token",
                    "evaluator-secret-value",
                    frozenset({"evaluator"}),
                ),
            ),
            grants=(
                ScopedGrant("render-ui", frozenset({"preview"})),
                ScopedGrant("write-canonical-metrics", frozenset({"evaluator"})),
            ),
        )
        self.launcher = PreviewLauncher(
            provider=ContainerProvider(Path("/realm/container-mounts")),
            overlays=self.overlays,
            credentials=self.credentials,
        )

    def test_terminal_debug_selection_previews_with_fresh_upper_and_narrow_credentials(
        self,
    ) -> None:
        selection = self.debug_run.select()
        profile = InterfaceLaunchProfile(
            source_ref=fake_ref("interface-source"),
            entrypoint="ui/server.py",
            required_secret_names=("preview-token",),
            required_grants=("render-ui",),
        )
        preview = self.launcher.open(
            selection,
            profile,
            invocation_id="preview-42",
        )

        # Selection has immutable refs only and cannot carry the evaluator upper.
        self.assertNotIn("evaluator_upper", {item.name for item in fields(selection)})
        self.assertEqual(preview.selection.terminal_view_ref, self.debug_run.terminal_view_ref)
        self.assertNotEqual(preview.upper.overlay_id, self.evaluator_upper.overlay_id)
        self.assertNotEqual(preview.upper.host_path, self.evaluator_upper.host_path)
        self.assertEqual(preview.upper.purpose, "preview")
        self.assertNotIn(
            self.evaluator_upper.overlay_id,
            {scope.overlay_id for scope in preview.binding.scopes},
        )
        target = next(scope for scope in preview.binding.scopes if scope.logical_name == "target")
        self.assertEqual(target.content, self.debug_run.terminal_view_ref)
        self.assertFalse(target.writable)

        self.assertEqual(preview.binding.credentials.secret_names, ("preview-token",))
        self.assertEqual(preview.binding.credentials.grants, frozenset({"render-ui"}))
        evidence = json.dumps(preview.binding.portable_evidence(preview.spec), sort_keys=True)
        self.assertNotIn("preview-secret-value", evidence)
        self.assertNotIn("evaluator-secret-value", evidence)
        self.assertNotIn("evaluator-token", evidence)
        self.assertNotIn("write-canonical-metrics", evidence)

    def test_preview_cannot_request_evaluator_only_secret_or_grant(self) -> None:
        selection = self.debug_run.select()
        with self.assertRaises(CredentialScopeError):
            self.launcher.open(
                selection,
                InterfaceLaunchProfile(
                    source_ref=fake_ref("interface-source"),
                    entrypoint="ui/server.py",
                    required_secret_names=("evaluator-token",),
                ),
                invocation_id="bad-secret",
            )
        with self.assertRaises(CredentialScopeError):
            self.launcher.open(
                selection,
                InterfaceLaunchProfile(
                    source_ref=fake_ref("interface-source"),
                    entrypoint="ui/server.py",
                    required_grants=("write-canonical-metrics",),
                ),
                invocation_id="bad-grant",
            )

    def test_nonterminal_or_unsealed_debug_run_is_not_selectable(self) -> None:
        for state, view in (("running", fake_ref("view")), ("terminal", None)):
            debug_run = replace(self.debug_run, state=state, terminal_view_ref=view)
            with self.assertRaises(DebugSelectionError):
                debug_run.select()

class ImmutablePackagePipelineSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspaces = WorkspaceRegistry()
        self.workspaces.put(
            WorkspaceRevision(
                workspace_id="workspace-a",
                generation=7,
                snapshot_ref=fake_ref("workspace-a-generation-7"),
            )
        )
        self.compiler = PackageCompiler(version="compiler-test-v1")
        self.artifacts = PackageArtifactStore()
        self.publisher = CatalogPublisher()
        self.pipeline = PackagePipeline(
            workspaces=self.workspaces,
            compiler=self.compiler,
            artifacts=self.artifacts,
            publisher=self.publisher,
        )
        self.plan = TreePlan(
            include=("environments/devs", "methods/search"),
            exclude=("environments/devs/__pycache__",),
        )

    def _prepare(self):
        return self.pipeline.prepare(
            workspace_id="workspace-a",
            expected_generation=7,
            tree_plan=self.plan,
        )

    def test_prepare_validate_smoke_apply_pin_one_artifact_without_recompile_or_recopy(
        self,
    ) -> None:
        workflow = self._prepare()
        artifact = workflow.artifact_ref

        receipts = (
            workflow.validate(artifact),
            workflow.smoke(artifact),
            workflow.apply(artifact),
        )

        self.assertEqual([item.phase for item in receipts], ["validate", "smoke", "apply"])
        self.assertEqual({item.artifact_digest for item in receipts}, {artifact.digest})
        self.assertEqual(self.compiler.compile_count, 1)
        self.assertEqual(self.compiler.source_projection_count, 1)
        self.assertEqual(self.artifacts.publish_count, 1)
        self.assertEqual(self.artifacts.lease_digests, [artifact.digest] * 3)
        self.assertEqual(len(set(self.artifacts.lease_payload_object_ids)), 1)
        self.assertEqual(self.publisher.published_refs, [artifact])
        self.assertEqual(
            artifact.source_snapshot_ref,
            fake_ref("workspace-a-generation-7"),
        )
        self.assertEqual(workflow.source_revision.generation, 7)
        self.assertEqual(artifact.tree_plan_digest, self.plan.digest)

    def test_prepare_rejects_stale_workspace_generation_before_compilation(self) -> None:
        with self.assertRaises(StaleWorkspaceGeneration):
            self.pipeline.prepare(
                workspace_id="workspace-a",
                expected_generation=6,
                tree_plan=self.plan,
            )

        self.assertEqual(self.compiler.compile_count, 0)
        self.assertEqual(self.compiler.source_projection_count, 0)
        self.assertEqual(self.artifacts.publish_count, 0)

    def test_artifact_identity_depends_on_snapshot_not_workspace_owner_coordinates(self) -> None:
        first = self._prepare()
        self.workspaces.put(
            WorkspaceRevision(
                workspace_id="workspace-b",
                generation=103,
                snapshot_ref=fake_ref("workspace-a-generation-7"),
            )
        )
        second = self.pipeline.prepare(
            workspace_id="workspace-b",
            expected_generation=103,
            tree_plan=self.plan,
        )

        self.assertEqual(first.artifact_ref, second.artifact_ref)
        self.assertNotEqual(first.source_revision.workspace_id, second.source_revision.workspace_id)
        self.assertEqual(self.artifacts.publish_count, 1)

    def test_apply_rejects_workspace_that_changed_after_smoke(self) -> None:
        workflow = self._prepare()
        artifact = workflow.artifact_ref
        workflow.validate(artifact)
        workflow.smoke(artifact)
        self.workspaces.put(
            WorkspaceRevision(
                workspace_id="workspace-a",
                generation=8,
                snapshot_ref=fake_ref("workspace-a-generation-8"),
            )
        )

        with self.assertRaises(StaleWorkspaceGeneration):
            workflow.apply(artifact)

        self.assertEqual(self.compiler.compile_count, 1)
        self.assertEqual(self.artifacts.publish_count, 1)
        self.assertEqual(self.publisher.published_refs, [])
        self.assertEqual([item.phase for item in workflow.phase_receipts], ["validate", "smoke"])

    def test_digest_or_provenance_substitution_is_rejected_without_advancing_phase(self) -> None:
        workflow = self._prepare()
        artifact = workflow.artifact_ref
        wrong_digest = replace(artifact, digest=fake_ref("other-package").digest)
        wrong_compiler = replace(artifact, compiler_version="substituted-compiler")

        for substituted in (wrong_digest, wrong_compiler):
            with self.assertRaises(ArtifactSubstitution):
                workflow.validate(substituted)
        self.assertEqual(self.artifacts.lease_digests, [])
        self.assertEqual(workflow.phase_receipts, [])

        workflow.validate(artifact)
        self.assertEqual([item.phase for item in workflow.phase_receipts], ["validate"])

    def test_package_phase_order_is_explicit_and_recoverable(self) -> None:
        workflow = self._prepare()
        artifact = workflow.artifact_ref

        with self.assertRaises(PackagePhaseError):
            workflow.smoke(artifact)
        workflow.validate(artifact)
        workflow.smoke(artifact)
        workflow.apply(artifact)
        with self.assertRaises(PackagePhaseError):
            workflow.apply(artifact)


if __name__ == "__main__":
    unittest.main()
