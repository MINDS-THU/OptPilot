from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from optpilot_studio.ui import server as studio_server


_RESOURCE_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "devs_gallery"
    / "resources"
    / "devs-gen-interface"
    / "optpilot.resource.yaml"
)


class StudioInterfaceRuntimeEnvironmentTest(unittest.TestCase):
    def _interface(self) -> dict[str, object]:
        raw = yaml.safe_load(_RESOURCE_CONFIG.read_text(encoding="utf-8"))
        return copy.deepcopy(raw["interface"])

    def test_ordinary_component_environment_remains_supported(self) -> None:
        profiles = studio_server._compile_component_interface_profiles(
            self._interface(),
            component_kind="resource",
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].env["DEVS_INTERFACE_CONCURRENCY"], "8")

    def test_package_model_defaults_satisfy_interface_preflight(self) -> None:
        profiles = studio_server._compile_component_interface_profiles(
            self._interface(),
            component_kind="resource",
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = studio_server.UiState(
            cwd=Path(temporary.name), catalog_roots=[], run_roots=[]
        )
        self.addCleanup(state.close_coordination)
        names = {
            "DEVS_INTERFACE_MODEL_ID",
            "DEVS_INTERFACE_STRONG_MODEL_ID",
            "DEVS_DISPLAY_MODEL_ID",
            "OPENROUTER_API_KEY",
        }
        clean_environment = {key: value for key, value in os.environ.items() if key not in names}

        with mock.patch.dict(os.environ, clean_environment, clear=True):
            resolved, missing = studio_server._resolve_declared_env_from_host(
                state,
                studio_server._interface_launch_env_requirements(profiles[0]),
            )

        self.assertEqual(missing, ["OPENROUTER_API_KEY"])
        self.assertEqual(
            resolved["DEVS_INTERFACE_MODEL_ID"],
            "openrouter/openai/gpt-5.4",
        )
        self.assertEqual(
            resolved["DEVS_INTERFACE_STRONG_MODEL_ID"],
            "openrouter/openai/gpt-5.4",
        )
        self.assertEqual(
            resolved["DEVS_DISPLAY_MODEL_ID"],
            "openrouter/openai/gpt-5.4",
        )

    def test_studio_owned_handles_are_rejected_in_every_authored_env_layer(
        self,
    ) -> None:
        mutations = (
            (
                "process",
                lambda interface: interface["env"].update({"PORT": "9999"}),
                "PORT",
            ),
            (
                "grant",
                lambda interface: interface["grants"]["envFromHost"].append(
                    "OPTPILOT_PREPARED_RUNTIME_ROOT"
                ),
                "OPTPILOT_PREPARED_RUNTIME_ROOT",
            ),
            (
                "setup",
                lambda interface: interface["runtime"]["setup"].update(
                    {"env": {"OPTPILOT_PREPARED_RUNTIME_ACCESS": "build"}}
                ),
                "OPTPILOT_PREPARED_RUNTIME_ACCESS",
            ),
            (
                "setup step",
                lambda interface: interface["runtime"]["setup"]["steps"][0].update(
                    {"env": {"OPTPILOT_INTERFACE_OUTPUT_ROOT": "/tmp/output"}}
                ),
                "OPTPILOT_INTERFACE_OUTPUT_ROOT",
            ),
        )

        for label, mutate, expected_name in mutations:
            with self.subTest(layer=label):
                interface = self._interface()
                mutate(interface)
                with self.assertRaisesRegex(ValueError, expected_name):
                    studio_server._compile_component_interface_profiles(
                        interface,
                        component_kind="resource",
                    )

    def test_runtime_defense_rejects_reserved_setup_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "OPTPILOT_INTERFACE_RUNTIME_ROOT"):
            studio_server._reject_interface_runtime_env_overrides(
                {"OPTPILOT_INTERFACE_RUNTIME_ROOT": "/tmp/not-studio-owned"}
            )

    def test_catalog_does_not_call_an_unsaved_host_value_missing(self) -> None:
        app_path = (
            Path(__file__).resolve().parents[2]
            / "studio"
            / "src"
            / "optpilot_studio"
            / "ui"
            / "static"
            / "app.js"
        )
        source = app_path.read_text(encoding="utf-8")
        start = source.index("function componentEnvRequirementsPanel(")
        end = source.index("function componentEnvRequirements(", start)
        panel = source[start:end]

        self.assertIn("not saved in Studio Settings", panel)
        self.assertIn("exported value in the Studio process may still satisfy launch", panel)
        self.assertIn("this panel never reads or displays that value", panel)
        self.assertIn("Launch availability checks are authoritative", panel)
        self.assertNotIn(': "missing"', panel)
        self.assertIn(
            '"not saved in Studio Settings"].includes(value)',
            source,
        )


if __name__ == "__main__":
    unittest.main()
