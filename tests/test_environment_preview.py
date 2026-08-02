from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from optpilot.realm._validation import thaw_json
from optpilot.realm.environment_preview import (
    ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE,
    ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
    EnvironmentPreviewPlan,
    compile_environment_preview_plan,
)
from optpilot.realm.environment_preview_binding import (
    _launch_request,
    _projection_spec,
)
from optpilot.realm.errors import RealmConflict, RealmIntegrityError
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.run_closure import (
    InterfaceAcceptsSpec,
    InterfaceContainerBuildSpec,
    InterfaceContainerSpec,
    InterfaceGrantSpec,
    InterfaceLaunchProfile,
    InterfaceProcessInvocation,
    InterfaceResourceSpec,
    InterfaceRuntimeSpec,
    InterfaceSetupSpec,
    RunEvaluationClosure,
    WebPresentationSpec,
)
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    NormalizedCandidateEnvelope,
)
from optpilot.realm.refs import SnapshotRef
from optpilot.realm.selections import SelectionRef
from optpilot.run_control_manifest import candidate_contract_digest
from tests.test_realm_local_attempt_launcher import _RetainedRuntimeFixture


_IMAGE = "registry.example/optpilot/viewer@sha256:" + "a" * 64


class EnvironmentPreviewCompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)
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
        self.target = self.fixture.ledger.resolve_candidate_inspection_target(
            actor_principal_id="operator",
            selection=selection,
        )

    def _profile(
        self,
        profile_id: str = "default",
        *,
        accepts: tuple[str, ...] = ("candidate",),
        media_types: tuple[str, ...] = (),
        runtime: InterfaceRuntimeSpec | None = None,
        grants: InterfaceGrantSpec | None = None,
        env: dict[str, str] | None = None,
        extra_ports: tuple[int, ...] = (),
        outputs: bool = False,
    ) -> InterfaceLaunchProfile:
        if runtime is None:
            runtime = InterfaceRuntimeSpec(
                sandbox="container",
                container=InterfaceContainerSpec(
                    image=_IMAGE,
                    platform="linux/amd64",
                    engine="docker",
                ),
            )
        return InterfaceLaunchProfile(
            profile_id=profile_id,
            label=f"{profile_id.title()} Preview",
            description="Inspect one exact parameter candidate.",
            process=InterfaceProcessInvocation(
                command=("python", "-m", "viewer"),
                cwd="viewer",
                env={"VIEW_MODE": "inspect"} if env is None else env,
            ),
            runtime=runtime,
            grants=InterfaceGrantSpec() if grants is None else grants,
            resources=InterfaceResourceSpec(cpu=2, memory_mib=768, gpus=0),
            timeout_seconds=300,
            presentation=WebPresentationSpec(
                port=5173,
                extra_ports=extra_ports,
            ),
            accepts=InterfaceAcceptsSpec(
                selection_kinds=accepts,
                media_types=media_types,
            ),
            outputs=outputs,
        )

    def _with_profiles(
        self,
        *profiles: InterfaceLaunchProfile,
        inherited_container: bool = False,
        embed_private_semantics: bool = False,
    ):
        old = self.target.evaluation.closure
        evaluator_contract = thaw_json(old.environment_revision.evaluator_contract)
        if embed_private_semantics:
            evaluator_contract["private_debug"] = {
                "credential": "never-serialize-evaluator-credential",
                "host_path": str(self.fixture.root / "private-evaluator"),
            }
        environment = replace(
            old.environment_revision,
            evaluator_contract=evaluator_contract,
            interface_profiles=profiles,
        )
        runtime_settings = thaw_json(old.prepared_runtime.runtime_settings)
        if embed_private_semantics:
            runtime_settings["private_debug"] = {
                "credential": "never-serialize-runtime-credential",
                "host_path": str(self.fixture.root / "private-runtime"),
            }
        runtime_changes: dict[str, Any] = {
            "environment_revision_digest": environment.digest,
            "portability": "portable",
            "runtime_settings": runtime_settings,
        }
        if inherited_container:
            runtime_changes.update(
                runtime_kind="container",
                oci_image_digest="sha256:" + "b" * 64,
                platform="linux/amd64",
            )
        runtime = replace(old.prepared_runtime, **runtime_changes)
        sandbox_spec = thaw_json(old.evaluation_template.sandbox_spec)
        if inherited_container:
            sandbox_spec["runtimeType"] = "container"
        template = replace(
            old.evaluation_template,
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
            sandbox_spec=sandbox_spec,
        )
        closure = RunEvaluationClosure(environment, runtime, template)

        definition_changes: dict[str, Any] = {"evaluation_closure": closure}
        if inherited_container:
            execution = thaw_json(self.target.run_definition.execution_policy)
            execution["backend"]["type"] = "container"
            execution["defaults"]["sandboxSpec"]["runtimeType"] = "container"
            definition_changes["execution_policy"] = execution
        run_definition = replace(self.target.run_definition, **definition_changes)
        evaluation = replace(self.target.evaluation, closure=closure)
        selection_payload = self.target.selection.to_dict()
        selection_payload.pop("schema")
        selection_payload.pop("selection_digest")
        selection_payload["context_digest"] = template.digest
        selection = SelectionRef.build(**selection_payload)
        return replace(
            self.target,
            selection=selection,
            run_definition=run_definition,
            evaluation=evaluation,
        )

    def _with_candidate_contract_format(self, target, candidate_format: str):
        old = target.evaluation.closure
        candidate_contract = thaw_json(old.environment_revision.candidate_contract)
        candidate_contract["format"] = candidate_format
        environment = replace(
            old.environment_revision,
            candidate_contract=candidate_contract,
        )
        runtime = replace(
            old.prepared_runtime,
            environment_revision_digest=environment.digest,
        )
        template = replace(
            old.evaluation_template,
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
        )
        closure = RunEvaluationClosure(environment, runtime, template)
        selection_payload = target.selection.to_dict()
        selection_payload.pop("schema")
        selection_payload.pop("selection_digest")
        selection_payload["context_digest"] = template.digest
        return replace(
            target,
            selection=SelectionRef.build(**selection_payload),
            run_definition=replace(
                target.run_definition,
                evaluation_closure=closure,
                run_control_manifest=replace(
                    target.run_definition.run_control_manifest,
                    candidate_contract_digest=candidate_contract_digest(
                        candidate_contract
                    ),
                ),
            ),
            evaluation=replace(target.evaluation, closure=closure),
        )

    def _with_candidate_spec(
        self,
        target,
        spec: dict[str, Any],
        *,
        format: str = "parameters",
        content_refs=(),
    ):
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format=format,
            spec=spec,
            content_refs=content_refs,
        )
        candidate = replace(
            target.candidate,
            admission=replace(target.candidate.admission, envelope=envelope),
        )
        selection_payload = target.selection.to_dict()
        selection_payload.pop("schema")
        selection_payload.pop("selection_digest")
        selection_payload["entity_ref"] = str(envelope.candidate_ref)
        selection = SelectionRef.build(**selection_payload)
        bindings = (
            ()
            if not content_refs
            else tuple(
                OwnerMembership(
                    self.fixture.store.store_id,
                    content_ref,
                    RUN_CANDIDATE_ROLE,
                )
                for content_ref in content_refs
            )
        )
        return replace(
            target,
            candidate=candidate,
            candidate_bindings=bindings,
            selection=selection,
        )

    def test_selects_default_explicit_named_and_only_named_profiles(self) -> None:
        default = self._profile("default")
        inspect = self._profile("inspect")
        target = self._with_profiles(default, inspect)

        default_plan = compile_environment_preview_plan(target)
        named_plan = compile_environment_preview_plan(target, "inspect")
        only_plan = compile_environment_preview_plan(
            self._with_profiles(inspect)
        )

        self.assertEqual(default_plan.profile_id, "default")
        self.assertEqual(named_plan.profile_id, "inspect")
        self.assertEqual(only_plan.profile_id, "inspect")
        self.assertNotEqual(default_plan.digest, named_plan.digest)

    def test_profile_resolution_and_candidate_compatibility_fail_closed(self) -> None:
        with self.subTest("missing"):
            with self.assertRaisesRegex(RealmConflict, "declares no Preview"):
                compile_environment_preview_plan(self._with_profiles())
        with self.subTest("ambiguous"):
            target = self._with_profiles(
                self._profile("dashboard"), self._profile("inspector")
            )
            with self.assertRaisesRegex(RealmConflict, "profile_id is required"):
                compile_environment_preview_plan(target)
        with self.subTest("unknown"):
            target = self._with_profiles(self._profile("default"))
            with self.assertRaisesRegex(RealmConflict, "Unknown retained"):
                compile_environment_preview_plan(target, "missing")
        with self.subTest("selection kind"):
            target = self._with_profiles(
                self._profile("default", accepts=("trial",))
            )
            with self.assertRaisesRegex(RealmConflict, "does not accept candidate"):
                compile_environment_preview_plan(target)
        with self.subTest("media type"):
            target = self._with_profiles(
                self._profile("default", media_types=("image/png",))
            )
            with self.assertRaisesRegex(RealmConflict, r"candidate\+json"):
                compile_environment_preview_plan(target)
        with self.subTest("opaque candidate"):
            target = self._with_candidate_spec(
                self._with_profiles(self._profile()),
                {"value": "opaque"},
                format="opaque",
            )
            with self.assertRaisesRegex(RealmConflict, "opaque candidates"):
                compile_environment_preview_plan(target)

    def test_first_release_runtime_and_authority_policy_fails_closed(self) -> None:
        cases = {
            "process": (
                self._profile(
                    runtime=InterfaceRuntimeSpec(sandbox="process")
                ),
                "requires an enforceable container",
            ),
            "mutable image": (
                self._profile(
                    runtime=InterfaceRuntimeSpec(
                        sandbox="container",
                        container=InterfaceContainerSpec(image="viewer:latest"),
                    )
                ),
                "pinned by a sha256",
            ),
            "setup": (
                self._profile(
                    runtime=InterfaceRuntimeSpec(
                        sandbox="container",
                        setup=InterfaceSetupSpec(
                            steps=(
                                {"uses": "command", "command": ["python", "-V"]},
                            )
                        ),
                        container=InterfaceContainerSpec(image=_IMAGE),
                    )
                ),
                "does not run profile setup",
            ),
            "build": (
                self._profile(
                    runtime=InterfaceRuntimeSpec(
                        sandbox="container",
                        container=InterfaceContainerSpec(
                            build=InterfaceContainerBuildSpec(tag="viewer:test")
                        ),
                    )
                ),
                "does not build container",
            ),
            "network": (
                self._profile(grants=InterfaceGrantSpec(network="enabled")),
                "requires denied container network",
            ),
            "host secret": (
                self._profile(
                    grants=InterfaceGrantSpec(
                        secrets_from_host=("VIEWER_TOKEN",)
                    )
                ),
                "does not support host secrets",
            ),
            "host environment": (
                self._profile(
                    grants=InterfaceGrantSpec(
                        env_from_host=("VIEWER_MODEL",)
                    )
                ),
                "does not support host environment variables",
            ),
            "credential-shaped fixed env": (
                self._profile(env={"API_KEY": "literal-value"}),
                "credential-shaped",
            ),
            "too many ports": (
                self._profile(extra_ports=tuple(range(5200, 5216))),
                "more than 16",
            ),
        }
        for name, (profile, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(RealmConflict, message):
                compile_environment_preview_plan(self._with_profiles(profile))

    def test_omitted_profile_runtime_inherits_exact_pinned_container(self) -> None:
        profile = self._profile(runtime=InterfaceRuntimeSpec())
        target = self._with_profiles(profile, inherited_container=True)

        plan = compile_environment_preview_plan(target)

        self.assertEqual(plan.runtime.source, "prepared-runtime")
        self.assertEqual(plan.runtime.image_ref, "sha256:" + "b" * 64)
        self.assertIsNone(plan.runtime.engine)
        self.assertEqual(plan.runtime.platform, "linux/amd64")

    def test_reserved_interface_output_variables_are_injected_and_cannot_be_authored(self) -> None:
        plan = compile_environment_preview_plan(
            self._with_profiles(self._profile(outputs=True))
        )
        expected = {
            "OPTPILOT_INTERFACE_CONTEXT": "/optpilot/interface/context.json",
            "OPTPILOT_INTERFACE_OUTPUT_ROOT": "/optpilot/interface/output",
            "OPTPILOT_INTERFACE_OUTPUTS_FILE": (
                "/optpilot/interface/control/outputs.jsonl"
            ),
            "OPTPILOT_INTERFACE_PROFILE_ID": "default",
        }
        for name, value in expected.items():
            self.assertEqual(plan.invocation.environment[name], value)
        self.assertEqual(plan.paths.output_root, expected["OPTPILOT_INTERFACE_OUTPUT_ROOT"])
        self.assertEqual(plan.paths.outputs_file, expected["OPTPILOT_INTERFACE_OUTPUTS_FILE"])

        for name in expected:
            with self.subTest(name=name), self.assertRaisesRegex(
                RealmConflict, "overrides reserved interface variables"
            ):
                compile_environment_preview_plan(
                    self._with_profiles(
                        self._profile(env={name: "authored"}, outputs=True)
                    )
                )

    def test_view_only_profile_does_not_receive_output_handles(self) -> None:
        plan = compile_environment_preview_plan(
            self._with_profiles(self._profile())
        )

        self.assertFalse(plan.outputs_enabled)
        self.assertFalse(plan.context.outputs_enabled)
        self.assertNotIn(
            "OPTPILOT_INTERFACE_OUTPUT_ROOT", plan.invocation.environment
        )
        self.assertNotIn(
            "OPTPILOT_INTERFACE_OUTPUTS_FILE", plan.invocation.environment
        )
        self.assertEqual(EnvironmentPreviewPlan.from_dict(plan.to_dict()), plan)

    def test_plan_context_round_trip_is_path_free_and_contains_no_credentials(self) -> None:
        accepts = InterfaceAcceptsSpec(
            selection_kinds=("candidate", "trial"),
            media_types=(ENVIRONMENT_PREVIEW_CANDIDATE_MEDIA_TYPE,),
        )
        profile = self._profile(
            accepts=accepts.selection_kinds,
            media_types=accepts.media_types,
        )
        target = self._with_profiles(
            profile,
            embed_private_semantics=True,
        )
        target = self._with_candidate_spec(
            target,
            {"x": 0.5, "password": "never-serialize-candidate-credential"},
        )

        plan = compile_environment_preview_plan(target)
        restored = EnvironmentPreviewPlan.from_dict(plan.to_dict())
        encoded = plan.canonical_bytes.decode("utf-8")

        self.assertEqual(restored, plan)
        self.assertEqual(plan.context.plan_digest, plan.digest)
        self.assertEqual(plan.accepts, accepts)
        self.assertEqual(plan.context.accepts, accepts)
        self.assertEqual(
            plan.to_dict()["grants"],
            {
                "networkEnforcement": "enforced",
                "networkPolicy": "denied",
                "requestedSecretNames": [],
            },
        )
        self.assertIsNone(plan.context.parameter_spec)
        self.assertNotIn(str(self.fixture.root), encoded)
        self.assertNotIn("never-serialize", encoded)
        self.assertNotIn("host_path", encoded)
        self.assertNotIn("store_id", encoded)
        for path in plan.paths.to_dict().values():
            self.assertTrue(path.startswith("/optpilot/interface/"), path)
            self.assertIn(path, encoded)
        with self.assertRaises(TypeError):
            plan.invocation.environment["NEW"] = "value"  # type: ignore[index]

        tampered = plan.to_dict()
        tampered["resources"]["cpu_millis"] += 1
        with self.assertRaisesRegex(RealmIntegrityError, "digest"):
            EnvironmentPreviewPlan.from_dict(tampered)

    def test_safe_bounded_parameter_spec_is_visible_and_digest_bound(self) -> None:
        target = self._with_profiles(self._profile())
        target = self._with_candidate_spec(
            target,
            {"x": 0.75, "strategy": "balanced"},
        )

        plan = compile_environment_preview_plan(target)

        self.assertEqual(
            thaw_json(plan.context.parameter_spec),
            {"strategy": "balanced", "x": 0.75},
        )
        payload = plan.to_dict()
        payload["context"]["candidate"]["parameters"]["x"] = 0.1
        with self.assertRaisesRegex(RealmIntegrityError, "digest"):
            EnvironmentPreviewPlan.from_dict(payload)
        self.assertEqual(EnvironmentPreviewPlan.from_dict(plan.to_dict()).digest, plan.digest)

    def test_file_context_names_only_the_fixed_read_only_logical_root(self) -> None:
        target = self._with_candidate_contract_format(
            self._with_profiles(self._profile()),
            "files",
        )
        snapshot = SnapshotRef.from_manifest_bytes(b"exact-file-candidate-tree")
        target = self._with_candidate_spec(
            target,
            {"sealed": "manifest"},
            format="files",
            content_refs=(snapshot,),
        )
        candidate_ref = target.candidate.candidate_ref
        plan = compile_environment_preview_plan(target)

        candidate = plan.to_dict()["context"]["candidate"]
        self.assertEqual(
            candidate,
            {
                "candidateRef": str(candidate_ref),
                "candidateRoot": ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
                "format": "files",
                "parameters": None,
            },
        )
        self.assertEqual(EnvironmentPreviewPlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(
            plan.invocation.environment["OPTPILOT_INTERFACE_CANDIDATE_ROOT"],
            ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT,
        )
        encoded = plan.canonical_bytes.decode("utf-8")
        self.assertNotIn(str(snapshot), encoded)
        self.assertNotIn("host_path", encoded)

        projection, placements = _projection_spec(
            owner_id="preview-file-owner",
            target=target,
        )
        candidate_mapping = next(
            item for item in projection.mappings if item.destination == "candidate"
        )
        self.assertEqual(candidate_mapping.snapshot_ref, snapshot)
        self.assertTrue(
            all(
                item.destination == "app" or item.destination.startswith("app/")
                for item in projection.mappings
                if item is not candidate_mapping
            )
        )
        self.assertIn((RUN_CANDIDATE_ROLE, snapshot), placements)
        layout = SimpleNamespace(
            context=Path("/provider/preview/context.json"),
            outputs_file=Path("/provider/preview/control/outputs.jsonl"),
            workspace=Path("/provider/preview/workspace"),
            artifacts=Path("/provider/preview/artifacts"),
            output=Path("/provider/preview/output"),
            runtime_env=Path("/provider/preview/runtime-env"),
            prepared_outputs=Path("/provider/preview/prepared-outputs"),
        )
        request = _launch_request(
            job_id="preview-file-job",
            binding_id="preview-file-binding",
            launch_token="preview-file-launch",
            operator_plan_digest="f" * 64,
            plan=plan,
            app_path=Path("/provider/projection/app"),
            layout=layout,
        )
        mount_modes = {
            item.container_path: (item.host_path, item.mode)
            for item in request.mounts
        }
        self.assertEqual(
            mount_modes[ENVIRONMENT_PREVIEW_FILE_CANDIDATE_ROOT],
            (Path("/provider/projection/candidate"), "read-only"),
        )
        self.assertEqual(
            mount_modes[plan.paths.app],
            (Path("/provider/projection/app"), "read-only"),
        )

        tampered = plan.to_dict()
        tampered["context"]["candidate"]["candidateRoot"] = "/private/tmp/candidate"
        with self.assertRaisesRegex(RealmIntegrityError, "candidate root"):
            EnvironmentPreviewPlan.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
