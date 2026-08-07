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

    def test_method_interface_rejects_container_only_fields_kindly(self) -> None:
        bad = {
            "label": "x",
            "command": ["python", "app.py"],
            "presentation": {"kind": "web", "port": 0},
        }
        with self.assertRaises((TypeError, ValueError)):
            compile_interface_launch_profiles(bad, component_kind="method")


if __name__ == "__main__":
    unittest.main()
