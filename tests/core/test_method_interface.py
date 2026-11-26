"""Method-declared launchable interfaces (the COOPA Solve Console shape)."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from optpilot.config import (
    compile_interface_launch_profiles,
    validate_authoring_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COOPA_METHOD = (
    REPO_ROOT / "catalog" / "or_solving" / "methods" / "coopa_solver" / "method.yaml"
)
COOPA_CONSOLE = COOPA_METHOD.with_name("solve_console.html")


class MethodInterfaceTest(unittest.TestCase):
    def test_bundled_coopa_method_with_interface_validates(self) -> None:
        result = validate_authoring_config(COOPA_METHOD)
        self.assertTrue(result.get("valid"), result.get("errors"))

    def test_method_interface_compiles_to_one_web_profile(self) -> None:
        raw = yaml.safe_load(COOPA_METHOD.read_text(encoding="utf-8"))
        profiles = compile_interface_launch_profiles(
            raw["interface"], component_kind="method"
        )
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(profile.presentation.port, 8000)
        self.assertEqual(profile.runtime.sandbox, "process")
        self.assertIn("COOPA_HOME", profile.grants.env_from_host)
        self.assertIn("OPENROUTER_API_KEY", profile.grants.secrets_from_host)
        declaration = next(
            item
            for item in profile.grants.env_from_host_declarations
            if item.name == "COOPA_HOME"
        )
        self.assertEqual(declaration.default, "")
        self.assertEqual(raw["runtime"]["envFromHost"], ["OPENROUTER_API_KEY"])

    def test_method_interface_rejects_container_only_fields_kindly(self) -> None:
        bad = {
            "label": "x",
            "command": ["python", "app.py"],
            "presentation": {"kind": "web", "port": 0},
        }
        with self.assertRaises((TypeError, ValueError)):
            compile_interface_launch_profiles(bad, component_kind="method")

    def test_coopa_console_uses_the_full_stage_without_losing_advanced_options(self) -> None:
        console = COOPA_CONSOLE.read_text(encoding="utf-8")

        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", console)
        self.assertIn("#input-card { display: grid", console)
        self.assertIn("#problem { height: 100%", console)
        self.assertIn("#input-card:has(details.adv[open])", console)
        self.assertIn("details.adv[open] { overflow: auto", console)
        self.assertIn("#run-card { overflow: auto", console)
        self.assertIn("@media (max-height: 680px)", console)
        self.assertIn("@media (max-width: 900px)", console)


if __name__ == "__main__":
    unittest.main()
