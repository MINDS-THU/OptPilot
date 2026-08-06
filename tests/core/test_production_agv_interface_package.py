from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from optpilot.config import (
    compile_interface_launch_profiles,
    validate_authoring_config,
)
from optpilot.package_validation import validate_package
from optpilot.realm.environment_preview import compile_environment_preview_plan
from optpilot.realm.environment_preview_binding import (
    RealmEnvironmentPreviewBinder,
)
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
)
from tests.core.test_realm_local_attempt_launcher import _RetainedRuntimeFixture


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPOSITORY_ROOT / "catalog" / "production_agv_scheduling"
_ENVIRONMENT_ROOT = (
    _PACKAGE_ROOT / "environments" / "production_agv_scheduling"
)
_CANDIDATE_IMAGE = (
    "python@sha256:"
    "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)
_ENVIRONMENT_VARIANTS = {
    "environment_baselines.yaml",
    "environment_faults.yaml",
    "environment_llm.yaml",
    "environment_long_horizon.yaml",
    "environment_meta.yaml",
    "environment_smoke.yaml",
    "environment_variable_arrivals.yaml",
}


class ProductionAgvInterfacePackageTest(unittest.TestCase):
    def test_every_environment_variant_declares_closed_interface_profiles(
        self,
    ) -> None:
        actual = {
            path.name for path in _ENVIRONMENT_ROOT.glob("environment_*.yaml")
        }
        self.assertEqual(actual, _ENVIRONMENT_VARIANTS)

        for filename in sorted(_ENVIRONMENT_VARIANTS):
            path = _ENVIRONMENT_ROOT / filename
            with self.subTest(path=path):
                validation = validate_authoring_config(path)
                self.assertTrue(validation["valid"], validation)
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                profiles = {
                    profile.profile_id: profile
                    for profile in compile_interface_launch_profiles(
                        raw["interface"], component_kind="environment"
                    )
                }

                self.assertEqual(set(profiles), {"candidate", "default"})
                catalog = profiles["default"]
                candidate = profiles["candidate"]
                self.assertEqual(catalog.runtime.sandbox, "process")
                self.assertIsNone(catalog.runtime.container)
                self.assertEqual(candidate.runtime.sandbox, "container")
                self.assertIsNotNone(candidate.runtime.container)
                self.assertEqual(
                    candidate.runtime.container.image, _CANDIDATE_IMAGE
                )
                self.assertIsNone(candidate.runtime.container.platform)

                for profile in profiles.values():
                    self.assertFalse(profile.outputs)
                    self.assertEqual(
                        profile.command,
                        (
                            "python",
                            "interface_server.py",
                            "--host",
                            "0.0.0.0",
                            "--port",
                            "8080",
                        ),
                    )
                    self.assertEqual(profile.cwd, ".")
                    self.assertEqual(profile.grants.network, "disabled")
                    self.assertEqual(profile.grants.env_from_host, ())
                    self.assertEqual(profile.grants.secrets_from_host, ())
                    self.assertEqual(profile.presentation.port, 8080)
                    self.assertEqual(profile.presentation.extra_ports, ())
                    self.assertEqual(
                        profile.presentation.readiness.path, "/ready"
                    )
                    self.assertEqual(
                        profile.accepts.selection_kinds, ("candidate",)
                    )
                    self.assertEqual(
                        profile.accepts.media_types,
                        ("application/vnd.optpilot.candidate+json",),
                    )

    def test_interface_source_and_webgl_export_are_inside_package(self) -> None:
        required = {
            "interface_server.py",
            "interface_runtime.py",
            "interface_worker.py",
            "interface_web/app.js",
            "interface_web/index.html",
            "interface_web/styles.css",
            "mqtt_bridge.py",
            "factory_sim/config/factory_layout_multi.json",
            "unity_webgl/index.html",
            "unity_webgl/Build/SimPy.loader.js",
            "unity_webgl/Build/SimPy.framework.js.unityweb",
            "unity_webgl/Build/SimPy.wasm.unityweb",
            "unity_webgl/Build/SimPy.data.unityweb",
            "unity_webgl/StreamingAssets/MQTTBroker.json",
        }
        for relative_path in sorted(required):
            path = _ENVIRONMENT_ROOT / relative_path
            with self.subTest(path=relative_path):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
                self.assertTrue(path.resolve().is_relative_to(_PACKAGE_ROOT))

        broker_config = (
            _ENVIRONMENT_ROOT
            / "unity_webgl"
            / "StreamingAssets"
            / "MQTTBroker.json"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn("optpilot_offline", broker_config)
        self.assertNotIn("minions", broker_config)
        for public_endpoint in (
            "broker.emqx.io",
            "broker.hivemq.com",
            "183.172.218.153",
        ):
            self.assertNotIn(public_endpoint, broker_config)

    def test_candidate_profile_is_preview_eligible_with_exact_operator_trust(
        self,
    ) -> None:
        environment = yaml.safe_load(
            (_ENVIRONMENT_ROOT / "environment_smoke.yaml").read_text(
                encoding="utf-8"
            )
        )
        interface_yaml = yaml.safe_dump(
            {"interface": environment["interface"]}, sort_keys=False
        )
        fixture = _RetainedRuntimeFixture(
            environment_interface=interface_yaml
        )
        try:
            snapshot = fixture.ledger.read_run_snapshot(
                actor_principal_id="operator",
                run_id=fixture.created.run.run_id,
            )
            selection = fixture.ledger.mint_run_selection(
                actor_principal_id="operator",
                run_id=fixture.created.run.run_id,
                kind="candidate",
                entity_id="candidate-a",
                expected_run_revision=snapshot.revision.revision,
                expected_head_sequence=snapshot.revision.last_sequence,
            )
            target = fixture.ledger.resolve_candidate_inspection_target(
                actor_principal_id="operator",
                selection=selection,
            )
            plan = compile_environment_preview_plan(
                target, profile_id="candidate"
            )
            provider = LocalContainerWebProvider(
                executable="docker",
                control_root=fixture.root / "preview-control",
                broker_authority=object(),
                trusted_gateway_images=(
                    ContainerGatewayImageTrust(_CANDIDATE_IMAGE),
                ),
            )
            binder = RealmEnvironmentPreviewBinder(
                fixture.ledger,
                fixture.projection_service,
                fixture.volume_service,
                provider,
            )

            binder.validate_plan(plan)

            self.assertEqual(plan.runtime.image_ref, _CANDIDATE_IMAGE)
            self.assertIsNone(plan.runtime.platform)
            self.assertEqual(plan.runtime.engine, "docker")
            self.assertEqual(plan.presentation.port, 8080)
            self.assertEqual(
                plan.invocation.environment[
                    "OPTPILOT_INTERFACE_PROFILE_ID"
                ],
                "candidate",
            )
        finally:
            fixture.close()

    def test_package_source_closure_validation_includes_interface(self) -> None:
        result = validate_package(
            _PACKAGE_ROOT,
            check_imports=False,
            check_source=True,
            check_setup_files=True,
        )
        self.assertTrue(result["valid"], result)


if __name__ == "__main__":
    unittest.main()
