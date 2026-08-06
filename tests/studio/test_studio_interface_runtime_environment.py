from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from optpilot_studio.ui import server as studio_server


_RESOURCE_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "example_package"
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


if __name__ == "__main__":
    unittest.main()
