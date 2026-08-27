"""Catalog setup finds the simulator a generate action actually wrote.

A generated bundle lands in resource-action-output/<request>/<name>/, never
at the Workspace root -- that is where a headless resource action writes its
results. Setup only looked at the root, found no simulation.json, and quietly
fell back to generic starter files: an environment.yaml stub and an adapter
that raises NotImplementedError. The declared policy hook was never turned
into an optimizable environment, so "generate a simulator, then optimize its
policy" had no path between its two halves. Observed on the restaurant
simulator, whose bundle declares a SeatingCoordinator policy hook.

The bundle is found where it is written, and the environment it produces
points at that location -- the config lives in optpilot_configs/, so every
reference to the simulator has to carry the bundle's own prefix.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot_studio.ui.server import (
    _generated_simulation_bundle_root,
    _simulation_policy_variant_starter_files,
    _workspace_simulation_handoff,
)

_SIMULATION = {
    "schema_version": "devs.simulation.v2",
    "entrypoint": "run.py",
    "arguments": [
        {
            "name": "random_seed", "flag": "--random_seed", "type": "integer",
            "required": False, "default": 42,
            "description": "Base random seed.",
        },
        {
            "name": "num_tables", "flag": "--num_tables", "type": "integer",
            "required": False, "default": 6,
            "description": "Number of dining tables.",
        },
    ],
    "result_files": ["summary.json"],
    "python_runtime": {"requirements_lock": "runtime_dependencies/requirements.lock"},
    "timeout_seconds": 600,
    "metrics": {
        "keys": ["avg_wait_before_seating_minutes"],
        "objective": {"metric": "avg_wait_before_seating_minutes", "direction": "minimize"},
    },
    "policy": {
        "file": "devs_project/RestaurantModel_libs/SeatingCoordinator.py",
        "entrypoint": "SeatingCoordinator",
        "description": "Replaceable seating-policy hook.",
    },
}


def _write_bundle(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "simulation.json").write_text(json.dumps(_SIMULATION), encoding="utf-8")
    (root / "run.py").write_text("# entrypoint\n", encoding="utf-8")
    lock = root / "runtime_dependencies"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "requirements.lock").write_text("", encoding="utf-8")
    policy = root / "devs_project" / "RestaurantModel_libs"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "SeatingCoordinator.py").write_text("class SeatingCoordinator: pass\n", encoding="utf-8")


class BundleDiscoveryTest(unittest.TestCase):
    def test_a_generated_bundle_is_found_where_the_action_writes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bundle = workspace / "resource-action-output" / "req-1" / "simulator"
            _write_bundle(bundle)
            self.assertEqual(_generated_simulation_bundle_root(workspace), bundle)

    def test_a_bundle_at_the_root_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_bundle(workspace)
            _write_bundle(workspace / "resource-action-output" / "req-1" / "simulator")
            self.assertEqual(_generated_simulation_bundle_root(workspace), workspace)

    def test_a_workspace_with_no_bundle_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_generated_simulation_bundle_root(Path(tmp)))

    def test_two_bundles_are_never_guessed_between(self) -> None:
        # Picking one would quietly register the wrong simulator.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_bundle(workspace / "resource-action-output" / "req-1" / "simulator")
            _write_bundle(workspace / "resource-action-output" / "req-2" / "simulator")
            with self.assertRaises(ValueError) as caught:
                _generated_simulation_bundle_root(workspace)
            self.assertIn("more than one", str(caught.exception))


class PolicyEnvironmentPathsTest(unittest.TestCase):
    """Every reference to the simulator carries the bundle's own prefix."""

    HANDOFF = {
        "schema_version": "devs.simulation.v2",
        "entrypoint": "run.py",
        "candidate_schema": {
            "random_seed": {"valueType": "int", "default": 42},
        },
        "metrics": {
            "keys": ["avg_wait_before_seating_minutes"],
            "objective": {
                "metric": "avg_wait_before_seating_minutes",
                "direction": "minimize",
            },
        },
        "policy": {
            "file": "devs_project/RestaurantModel_libs/SeatingCoordinator.py",
            "entrypoint": "SeatingCoordinator",
            "description": "Replaceable seating-policy hook.",
        },
    }

    def _environment(self, bundle_prefix: str) -> dict:
        files = _simulation_policy_variant_starter_files(
            "restaurant-sim", "Restaurant", dict(self.HANDOFF), bundle_prefix
        )
        for relative, body in files:
            if "environment" in relative.name:
                return yaml.safe_load(body)
        self.fail("no environment config was written")

    def test_paths_point_at_the_nested_bundle(self) -> None:
        prefix = "resource-action-output/req-1/simulator"
        config = self._environment(prefix)
        self.assertEqual(
            {item["from"] for item in config["trialWorkspace"]},
            {
                f"../{prefix}/run.py",
                f"../{prefix}/simulation.json",
                f"../{prefix}/devs_project",
            },
        )
        reference = next(
            item
            for item in config["methodContext"]["references"]
            if item["type"] == "candidate_template"
        )
        self.assertTrue(reference["path"].startswith(f"../{prefix}/"))

    def test_a_root_bundle_keeps_its_original_paths(self) -> None:
        config = self._environment("")
        self.assertEqual(
            {item["from"] for item in config["trialWorkspace"]},
            {"../run.py", "../simulation.json", "../devs_project"},
        )


if __name__ == "__main__":
    unittest.main()
