from __future__ import annotations

import unittest
from dataclasses import replace

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_closure import (
    EnvironmentRevisionManifest,
    InterfaceAcceptsSpec,
    InterfaceContainerBuildSpec,
    InterfaceContainerSpec,
    InterfaceGrantSpec,
    InterfaceLaunchProfile,
    InterfaceOutputActionSpec,
    InterfaceProcessInvocation,
    InterfaceReadinessSpec,
    InterfaceResourceSpec,
    InterfaceRuntimeSpec,
    InterfaceSetupSpec,
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
    ScopeLayer,
    ScopePath,
    WebPresentationSpec,
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
)


def _tree(label: str) -> SnapshotRef:
    return SnapshotRef.from_manifest_bytes(label.encode("utf-8"))


def _interface_profile(profile_id: str = "dashboard") -> InterfaceLaunchProfile:
    return InterfaceLaunchProfile(
        profile_id=profile_id,
        label="Dashboard",
        description="Inspect a frozen candidate.",
        process=InterfaceProcessInvocation(
            command=("python", "-m", "factory_env.viewer"),
            cwd="viewer",
            env={"VIEW_MODE": "inspect"},
        ),
        runtime=InterfaceRuntimeSpec(
            setup=InterfaceSetupSpec(
                steps=(
                    {
                        "uses": "command",
                        "command": ["python", "-m", "pip", "--version"],
                    },
                ),
                timeout_seconds=30,
            )
        ),
        grants=InterfaceGrantSpec(
            network="disabled",
            env_from_host=("FACTORY_VIEW_MODEL",),
            secrets_from_host=("FACTORY_VIEW_TOKEN",),
        ),
        resources=InterfaceResourceSpec(cpu=1, memory_mib=512, gpus=0),
        timeout_seconds=300,
        presentation=WebPresentationSpec(
            port=5173,
            extra_ports=(5174,),
            readiness=InterfaceReadinessSpec(path="/ready", timeout_seconds=15),
        ),
        accepts=InterfaceAcceptsSpec(selection_kinds=("candidate", "trial")),
    )


def _closure() -> RunEvaluationClosure:
    environment = EnvironmentRevisionManifest(
        environment_id="factory-simulator",
        compiler_id="optpilot.environment-compiler",
        compiler_version="1",
        authored_config=ScopePath("environment-source", "environment.yaml"),
        source_layers=(
            ScopeLayer("environment-source", _tree("environment-source")),
        ),
        evaluator_contract={
            "implementation": "builtin.configured_environment",
            "evaluate": {
                "type": "python",
                "callable": "factory_env.evaluate:evaluate",
                "settings": {"layout": "/tmp/opaque-not-a-dependency.json"},
            },
        },
        candidate_contract={
            "format": "parameters",
            "validation": {"implementation": "builtin.schema_validation"},
            "materialization": {"implementation": "builtin.parameter_to_config"},
        },
        attempt_input_layers=(
            ScopeLayer(
                "attempt-workspace",
                _tree("seed-inputs"),
                destination_subpath="inputs",
            ),
        ),
        projection_contract={
            "writable_volumes": [
                {"name": "attempt-workspace", "policy": "ephemeral"}
            ]
        },
        interface_profiles=(
            _interface_profile(),
        ),
    )
    runtime = PreparedEnvironmentRuntimeManifest(
        environment_revision_digest=environment.digest,
        runtime_kind="process",
        runtime_settings={
            "network_policy": "disabled",
            "host_env_names": ["FACTORY_TOKEN"],
        },
        prepared_layers=(
            ScopeLayer("environment-runtime", _tree("prepared-runtime")),
        ),
        workdir=ScopePath("environment-source", "."),
        platform="darwin-arm64",
        builder_fingerprint="1" * 64,
    )
    template = RunEvaluationTemplate(
        environment_revision_digest=environment.digest,
        runtime_revision_digest=runtime.digest,
        objective={
            "primaryMetric": {"name": "score", "direction": "maximize"}
        },
        resource_profile={"cpu": 1, "memoryGiB": 2, "timeoutSeconds": 60},
        sandbox_spec={"runtimeType": "process", "networkPolicy": "disabled"},
        default_seed={"global": 7},
    )
    return RunEvaluationClosure(environment, runtime, template)


class RunClosureValueObjectTest(unittest.TestCase):
    def test_closure_is_canonical_immutable_and_round_trips(self) -> None:
        closure = _closure()

        restored = RunEvaluationClosure.from_dict(closure.to_dict())

        self.assertEqual(restored, closure)
        self.assertEqual(restored.digest, closure.digest)
        self.assertEqual(
            restored.to_dict()["environment_revision_digest"],
            restored.environment_revision.digest,
        )
        with self.assertRaises((TypeError, AttributeError)):
            closure.environment_revision.evaluator_contract["new"] = True  # type: ignore[index]
        with self.assertRaises((TypeError, AttributeError)):
            closure.evaluation_template.default_seed["global"] = 9  # type: ignore[index]

    def test_interface_profile_is_typed_path_free_and_strictly_persisted(self) -> None:
        profile = _interface_profile()

        restored = InterfaceLaunchProfile.from_dict(profile.to_dict())

        self.assertEqual(restored, profile)
        self.assertEqual(restored.process.command, ("python", "-m", "factory_env.viewer"))
        self.assertEqual(restored.presentation.readiness.path, "/ready")
        self.assertEqual(restored.runtime.setup.timeout_seconds, 30)  # type: ignore[union-attr]
        encoded = str(profile.to_dict())
        self.assertNotIn("/tmp/", encoded)
        payload = profile.to_dict()
        payload["unexpected"] = True
        with self.assertRaises(RealmIntegrityError):
            InterfaceLaunchProfile.from_dict(payload)

    def test_interface_profile_rejects_host_paths_and_invalid_web_contracts(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            InterfaceGrantSpec(
                env_from_host=("VIEW_MODEL",),
                secrets_from_host=("VIEW_MODEL",),
            )
        with self.assertRaisesRegex(ValueError, "absolute host path"):
            replace(
                _interface_profile(),
                process=InterfaceProcessInvocation(
                    command=("/usr/bin/python", "viewer.py"),
                ),
            )
        with self.assertRaisesRegex(ValueError, "portable relative path"):
            InterfaceProcessInvocation(command=("python",), cwd="../outside")
        with self.assertRaisesRegex(ValueError, "absolute host path"):
            InterfaceProcessInvocation(
                command=("python",),
                env={"VIEW_ROOT": "/tmp/live-checkout"},
            )
        with self.assertRaisesRegex(ValueError, "local HTTP path"):
            InterfaceReadinessSpec(path="https://example.com/ready")
        with self.assertRaisesRegex(ValueError, "ports must be unique"):
            WebPresentationSpec(port=5173, extra_ports=(5173,))

    def test_interface_public_host_defaults_survive_retention(self) -> None:
        payload = _interface_profile().to_dict()
        payload["grants"]["envFromHost"] = [
            {
                "name": "FACTORY_VIEW_MODEL",
                "default": "provider/default-model",
                "description": "Model used by the viewer.",
            }
        ]

        authored = InterfaceLaunchProfile.from_authoring_dict(payload)
        restored = InterfaceLaunchProfile.from_dict(authored.to_dict())

        self.assertEqual(restored, authored)
        self.assertEqual(
            restored.grants.env_from_host_declarations[0].default,
            "provider/default-model",
        )
        self.assertEqual(
            restored.to_dict()["grants"]["envFromHost"],
            payload["grants"]["envFromHost"],
        )

    def test_interface_host_defaults_participate_in_equality(self) -> None:
        base = {
            "network": "disabled",
            "secretsFromHost": [],
        }
        required = InterfaceGrantSpec.from_authoring_dict(
            {**base, "envFromHost": ["VIEW_MODEL"]}
        )
        defaulted = InterfaceGrantSpec.from_authoring_dict(
            {
                **base,
                "envFromHost": [{"name": "VIEW_MODEL", "default": "model-a"}],
            }
        )
        other_default = InterfaceGrantSpec.from_authoring_dict(
            {
                **base,
                "envFromHost": [{"name": "VIEW_MODEL", "default": "model-b"}],
            }
        )

        self.assertNotEqual(required, defaulted)
        self.assertNotEqual(defaulted, other_default)
        self.assertEqual(
            defaulted,
            InterfaceGrantSpec.from_dict(defaulted.to_dict()),
        )

    def test_interface_output_actions_are_registered_and_canonical(self) -> None:
        payload = _interface_profile().to_dict()
        payload["outputs"] = {
            "actions": [
                {
                    "acceptsArguments": False,
                    "command": ["python", "run.py"],
                    "cwd": ".",
                    "id": "run",
                    "label": "Run simulation",
                    "runtime": "originating-interface",
                    "timeoutSeconds": 60,
                }
            ]
        }

        profile = InterfaceLaunchProfile.from_authoring_dict(payload)
        restored = InterfaceLaunchProfile.from_dict(profile.to_dict())

        self.assertEqual(restored, profile)
        self.assertTrue(profile.outputs)
        self.assertEqual(
            profile.output_actions,
            (
                InterfaceOutputActionSpec(
                    action_id="run",
                    label="Run simulation",
                    command=("python", "run.py"),
                    timeout_seconds=60,
                ),
            ),
        )
        self.assertEqual(profile.to_dict()["outputs"], payload["outputs"])

    def test_interface_output_actions_reject_unsafe_or_ambiguous_specs(self) -> None:
        base = {
            "id": "run",
            "label": "Run simulation",
            "command": ["python", "run.py"],
            "timeoutSeconds": 60,
        }
        with self.assertRaisesRegex(ValueError, "absolute host path"):
            InterfaceOutputActionSpec.from_authoring_dict(
                {**base, "command": ["/usr/bin/python", "run.py"]}
            )
        with self.assertRaisesRegex(ValueError, "portable relative path"):
            InterfaceOutputActionSpec.from_authoring_dict(
                {**base, "cwd": "../outside"}
            )
        with self.assertRaisesRegex(ValueError, "originating-interface"):
            InterfaceOutputActionSpec.from_authoring_dict(
                {**base, "runtime": "live-interface"}
            )
        with self.assertRaisesRegex(ValueError, "one hour"):
            InterfaceOutputActionSpec.from_authoring_dict(
                {**base, "timeoutSeconds": 3_601}
            )
        hidden = InterfaceOutputActionSpec.from_authoring_dict(
            {**base, "showInOutputCard": False}
        )
        self.assertFalse(hidden.show_in_output_card)
        self.assertEqual(hidden.to_dict()["showInOutputCard"], False)
        self.assertNotIn(
            "showInOutputCard",
            InterfaceOutputActionSpec.from_authoring_dict(base).to_dict(),
        )
        with self.assertRaisesRegex(TypeError, "show_in_output_card"):
            InterfaceOutputActionSpec.from_authoring_dict(
                {**base, "showInOutputCard": "false"}
            )
        action = InterfaceOutputActionSpec.from_authoring_dict(base)
        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            replace(
                _interface_profile(),
                outputs=True,
                output_actions=(action, action),
            )

    def test_interface_runtime_separates_realization_from_launch_authority(self) -> None:
        runtime = InterfaceRuntimeSpec(
            sandbox="container",
            setup=InterfaceSetupSpec(
                steps=({"uses": "npm", "cwd": "viewer", "install": "ci"},)
            ),
            container=InterfaceContainerSpec(
                build=InterfaceContainerBuildSpec(
                    tag="factory/viewer:local",
                    context="viewer",
                    dockerfile="Containerfile",
                    target="preview",
                    args={"BUILD_MODE": "release"},
                ),
                platform="linux/amd64",
                engine="podman",
            ),
        )

        restored = InterfaceRuntimeSpec.from_dict(runtime.to_dict())

        self.assertEqual(restored, runtime)
        self.assertNotIn("network", str(runtime.to_dict()))
        for forbidden in (
            {"env": {"TOKEN": "value"}},
            {"envFromHost": ["TOKEN"]},
            {"network": "enabled"},
            {"filesystem": {"read": ["source"]}},
            {"resources": {"cpu": 1}},
            {"timeoutSeconds": 60},
        ):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                ValueError, "only sandbox, setup, and container"
            ):
                InterfaceRuntimeSpec.from_authoring_dict(forbidden)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            InterfaceContainerSpec(image="viewer:1", build=runtime.container.build)
        with self.assertRaisesRegex(ValueError, "not an executable path"):
            InterfaceContainerSpec(image="viewer:1", engine="bin/docker")

    def test_interface_profiles_are_canonical_by_id_and_unique(self) -> None:
        base = _closure().environment_revision
        first = _interface_profile("alpha")
        second = _interface_profile("zeta")

        left = replace(base, interface_profiles=(second, first))
        right = replace(base, interface_profiles=(first, second))

        self.assertEqual(left.interface_profiles, right.interface_profiles)
        self.assertEqual(left.digest, right.digest)
        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            replace(base, interface_profiles=(first, first))

    def test_typed_paths_reject_host_absolute_traversal_and_backslashes(self) -> None:
        self.assertEqual(ScopePath("environment-source").relative_path, ".")
        self.assertEqual(
            ScopePath("environment-source", "src/evaluator.py").relative_path,
            "src/evaluator.py",
        )
        for value in (
            "/tmp/evaluator.py",
            "../evaluator.py",
            "src/../evaluator.py",
            "C:\\Users\\person\\evaluator.py",
            "src//evaluator.py",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ScopePath("environment-source", value)
        with self.assertRaises(ValueError):
            ScopeLayer(
                "environment-source",
                _tree("source"),
                destination_subpath="/host/path",
            )

    def test_opaque_settings_do_not_infer_paths_or_content_refs(self) -> None:
        hidden_blob = BlobRef.from_bytes(b"opaque")
        closure = _closure()
        changed_environment = replace(
            closure.environment_revision,
            evaluator_contract={
                "implementation": "custom",
                "settings": {
                    "contentRef": str(hidden_blob),
                    "posix": "/tmp/live.db",
                    "windows": "C:\\Users\\person\\live.db",
                },
            },
        )
        runtime = replace(
            closure.prepared_runtime,
            environment_revision_digest=changed_environment.digest,
        )
        template = replace(
            closure.evaluation_template,
            environment_revision_digest=changed_environment.digest,
            runtime_revision_digest=runtime.digest,
        )
        changed = RunEvaluationClosure(changed_environment, runtime, template)

        self.assertEqual(
            changed.environment_revision.evaluator_contract["settings"]["posix"],
            "/tmp/live.db",
        )
        self.assertNotIn(
            hidden_blob,
            {content_ref for _, content_ref in changed.required_content_refs},
        )

    def test_required_refs_are_store_neutral_deduplicated_and_role_scoped(self) -> None:
        closure = _closure()
        values = closure.required_content_refs

        self.assertEqual(
            {role for role, _ in values},
            {
                RUN_ENVIRONMENT_SOURCE_ROLE,
                RUN_ATTEMPT_INPUT_ROLE,
                RUN_PREPARED_RUNTIME_ROLE,
            },
        )
        self.assertEqual(len(values), 3)
        self.assertNotIn("store_id", closure.to_dict())
        self.assertEqual(
            tuple(closure.content_refs_by_role[RUN_ENVIRONMENT_SOURCE_ROLE]),
            (closure.environment_revision.source_layers[0].snapshot_ref,),
        )
        with self.assertRaises(TypeError):
            closure.content_refs_by_role["new-role"] = ()  # type: ignore[index]

    def test_explicit_precedence_makes_layer_order_canonical(self) -> None:
        first = ScopeLayer(
            "environment-source", _tree("lower"), precedence=0
        )
        second = ScopeLayer(
            "environment-source", _tree("upper"), precedence=1
        )
        base = _closure().environment_revision

        left = replace(base, source_layers=(second, first))
        right = replace(base, source_layers=(first, second))

        self.assertEqual(left.source_layers, right.source_layers)
        self.assertEqual(left.digest, right.digest)
        with self.assertRaisesRegex(ValueError, "same scope, destination, and precedence"):
            replace(base, source_layers=(first, replace(second, precedence=0)))

    def test_child_digest_or_manifest_substitution_is_rejected(self) -> None:
        closure = _closure()
        payload = closure.to_dict()
        payload["environment_revision_digest"] = "0" * 64
        with self.assertRaisesRegex(RealmIntegrityError, "digest"):
            RunEvaluationClosure.from_dict(payload)

        payload = closure.to_dict()
        payload["evaluation_template"]["objective"]["primaryMetric"]["name"] = "other"
        with self.assertRaisesRegex(RealmIntegrityError, "digest"):
            RunEvaluationClosure.from_dict(payload)

    def test_cross_revision_links_are_rejected(self) -> None:
        closure = _closure()
        with self.assertRaisesRegex(ValueError, "different environment revision"):
            RunEvaluationClosure(
                closure.environment_revision,
                replace(
                    closure.prepared_runtime,
                    environment_revision_digest="0" * 64,
                ),
                closure.evaluation_template,
            )
        with self.assertRaisesRegex(ValueError, "different runtime revision"):
            RunEvaluationClosure(
                closure.environment_revision,
                closure.prepared_runtime,
                replace(
                    closure.evaluation_template,
                    runtime_revision_digest="0" * 64,
                ),
            )

    def test_runtime_requires_immutable_container_identity_and_logical_workdir(self) -> None:
        closure = _closure()
        with self.assertRaisesRegex(ValueError, "immutable OCI image digest"):
            PreparedEnvironmentRuntimeManifest(
                environment_revision_digest=closure.environment_revision.digest,
                runtime_kind="container",
                runtime_settings={},
            )
        container = PreparedEnvironmentRuntimeManifest(
            environment_revision_digest=closure.environment_revision.digest,
            runtime_kind="container",
            runtime_settings={"network_policy": "disabled"},
            workdir=ScopePath("environment-source", "app"),
            oci_image_digest="sha256:" + "a" * 64,
            platform="linux/amd64",
        )
        self.assertEqual(
            PreparedEnvironmentRuntimeManifest.from_dict(container.to_dict()),
            container,
        )
        with self.assertRaisesRegex(ValueError, "cannot define an OCI"):
            replace(
                closure.prepared_runtime,
                oci_image_digest="sha256:" + "a" * 64,
            )

    def test_nonfinite_json_and_unknown_persisted_fields_are_rejected(self) -> None:
        closure = _closure()
        with self.assertRaises(ValueError):
            replace(
                closure.environment_revision,
                evaluator_contract={"implementation": "custom", "x": float("nan")},
            )

        payload = closure.to_dict()
        payload["unexpected"] = True
        with self.assertRaises(RealmIntegrityError):
            RunEvaluationClosure.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
