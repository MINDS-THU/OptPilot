"""devs.simulation.v2: declared metrics close the generate-then-optimize loop.

A v2 bundle that names its metrics produces a launch-ready Environment —
enabled config, declared metric keys, no manual editing. v1 bundles keep the
exact previous behavior (disabled template with the placeholder ``score``).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot_studio.ui.server import (
    UiState,
    _configure_workspace_catalog_role,
    _create_ui_workspace,
    _workspace_simulation_handoff,
)

from tests.studio.test_generated_devs_student_handoff_vertical_e2e import (
    _write_interface_generated_simulation,
)

_METRICS_BLOCK = {
    "keys": ["throughput", "queue_length"],
    "objective": {"metric": "throughput", "direction": "maximize"},
    "descriptions": {"throughput": "Units completed per time step."},
}


def _upgrade_bundle_to_v2(root: Path, metrics: dict | None = None) -> None:
    manifest_path = root / "simulation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "devs.simulation.v2"
    manifest["metrics"] = dict(_METRICS_BLOCK) if metrics is None else metrics
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class DevsSimulationV2MetricsTest(unittest.TestCase):
    def _bundle(self, tmp_path: Path) -> Path:
        root = tmp_path / "bundle"
        _write_interface_generated_simulation(root)
        return root

    def test_v2_handoff_carries_declared_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            _upgrade_bundle_to_v2(root)
            handoff = _workspace_simulation_handoff(root)
            self.assertIsNotNone(handoff)
            self.assertEqual(handoff["schema_version"], "devs.simulation.v2")
            self.assertEqual(
                handoff["metrics"]["keys"], ["throughput", "queue_length"]
            )
            self.assertEqual(
                handoff["metrics"]["objective"],
                {"metric": "throughput", "direction": "maximize"},
            )

    def test_v1_handoff_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            handoff = _workspace_simulation_handoff(root)
            self.assertIsNotNone(handoff)
            self.assertEqual(handoff["schema_version"], "devs.simulation.v1")
            self.assertNotIn("metrics", handoff)

    def test_metrics_on_v1_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            manifest_path = root / "simulation.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["metrics"] = dict(_METRICS_BLOCK)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "devs.simulation.v2"):
                _workspace_simulation_handoff(root)

    def test_invalid_objective_direction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            _upgrade_bundle_to_v2(
                root,
                metrics={
                    "keys": ["throughput"],
                    "objective": {"metric": "throughput", "direction": "upward"},
                },
            )
            with self.assertRaisesRegex(ValueError, "maximize or minimize"):
                _workspace_simulation_handoff(root)

    def test_undeclared_description_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            _upgrade_bundle_to_v2(
                root,
                metrics={
                    "keys": ["throughput"],
                    "descriptions": {"latency": "Not a declared key."},
                },
            )
            with self.assertRaisesRegex(ValueError, "descriptions"):
                _workspace_simulation_handoff(root)

    def test_v2_bundle_configures_a_launch_ready_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            root = tmp_path / "workspace"
            _write_interface_generated_simulation(root)
            _upgrade_bundle_to_v2(root)
            workspace = _create_ui_workspace(
                state, {"title": "Generated simulator", "root": str(root)}
            )
            result = _configure_workspace_catalog_role(
                state,
                workspace["id"],
                {"role": "environment", "id": "generated-sim"},
            )
            configuration = result["configuration"]
            self.assertFalse(configuration["needs_editing"])
            self.assertEqual(configuration["next_action"], "check")
            self.assertIn(
                "optpilot_configs/environment.yaml",
                configuration["created_paths"],
            )
            self.assertEqual(
                configuration["detected_simulation"]["metrics"]["keys"],
                ["throughput", "queue_length"],
            )
            environment = yaml.safe_load(
                (root / "optpilot_configs" / "environment.yaml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                environment["metrics"],
                {"source": "return", "keys": ["throughput", "queue_length"]},
            )

    def test_policy_bundle_emits_the_file_candidate_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            root = tmp_path / "workspace"
            _write_interface_generated_simulation(root)
            (root / "devs_project" / "policy.py").write_text(
                '"""snapshot: {queue: [...]}"""\n'
                "def create_policy():\n    return None\n",
                encoding="utf-8",
            )
            manifest_path = root / "simulation.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "devs.simulation.v2"
            manifest["metrics"] = dict(_METRICS_BLOCK)
            manifest["policy"] = {
                "file": "devs_project/policy.py",
                "entrypoint": "create_policy",
                "description": "Dispatch decisions.",
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            handoff = _workspace_simulation_handoff(root)
            self.assertEqual(
                handoff["policy"]["entrypoint"], "create_policy"
            )
            workspace = _create_ui_workspace(
                state, {"title": "Policy simulator", "root": str(root)}
            )
            result = _configure_workspace_catalog_role(
                state,
                workspace["id"],
                {"role": "environment", "id": "generated-sim"},
            )
            created = set(result["configuration"]["created_paths"])
            self.assertIn(
                "optpilot_configs/environment_policy.template.yaml.disabled",
                created,
            )
            self.assertIn(
                "optpilot_configs/optpilot_adapter_policy.py", created
            )
            self.assertIn("optpilot_configs/policy_instructions.md", created)
            variant = yaml.safe_load(
                (
                    root
                    / "optpilot_configs"
                    / "environment_policy.template.yaml.disabled"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(variant["candidate"]["format"], "files")
            self.assertEqual(
                variant["policyValidation"]["entrypoint"]["callable"],
                "create_policy",
            )
            self.assertEqual(
                variant["capabilities"][0]["callable"],
                "optpilot_adapter_policy:replay_candidate",
            )

    def test_missing_policy_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = self._bundle(Path(tmp_dir))
            manifest_path = root / "simulation.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "devs.simulation.v2"
            manifest["policy"] = {
                "file": "devs_project/policy.py",
                "entrypoint": "create_policy",
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "missing from the bundle"):
                _workspace_simulation_handoff(root)

    def test_v1_bundle_still_configures_a_disabled_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            root = tmp_path / "workspace"
            _write_interface_generated_simulation(root)
            workspace = _create_ui_workspace(
                state, {"title": "Generated simulator", "root": str(root)}
            )
            result = _configure_workspace_catalog_role(
                state,
                workspace["id"],
                {"role": "environment", "id": "generated-sim"},
            )
            configuration = result["configuration"]
            self.assertTrue(configuration["needs_editing"])
            self.assertIn(
                "optpilot_configs/environment.template.yaml.disabled",
                configuration["created_paths"],
            )
            template = yaml.safe_load(
                Path(
                    root,
                    "optpilot_configs",
                    "environment.template.yaml.disabled",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                template["metrics"], {"source": "return", "keys": ["score"]}
            )


if __name__ == "__main__":
    unittest.main()
