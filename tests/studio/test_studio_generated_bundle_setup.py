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


class SetupStepFollowsTheBundleTest(unittest.TestCase):
    """The venv is built where the requirements lock actually is.

    The generated environment's setup step ran with cwd "..", the Workspace
    root, while the bundle's runtime_dependencies/requirements.lock sits
    inside the bundle. Registration then failed validation on a lock file
    that was present the whole time.
    """

    def test_the_generated_setup_step_uses_the_bundle_as_its_cwd(self) -> None:
        import inspect

        from optpilot_studio.ui import server

        source = inspect.getsource(server._configure_workspace_catalog_role)
        self.assertIn('"cwd": bundle_root_ref', source)
        self.assertNotIn('"cwd": "..",', source)



class PolicyAdapterReplayResolutionTest(unittest.TestCase):
    """The starter adapter's replay path finds the simulator it evaluates.

    Evaluation materializes simulator/ into the trial workspace, so it never
    exercises the adapter's fallback. Replay starts from a bare workspace and
    must assemble the tree itself -- and the adapter is written into
    optpilot_configs/, so a fallback that only looks beside the adapter file
    finds nothing and every replay fails after a perfectly good evaluation.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        from optpilot_studio.ui.server import _simulation_policy_adapter_starter

        self.code = _simulation_policy_adapter_starter(
            "devs_project/RestaurantSystem_libs/SeatingCoordinator.py",
            "devs_project.run_restaurantsystem",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load(self, adapter_path: Path):
        import types

        module = types.ModuleType("policy_adapter_under_test")
        module.__file__ = str(adapter_path)
        exec(compile(self.code, str(adapter_path), "exec"), module.__dict__)
        return module

    def members(self, base: Path) -> None:
        (base / "devs_project").mkdir(parents=True, exist_ok=True)
        (base / "devs_project" / "model.py").write_text("x = 1\n")
        (base / "run.py").write_text("print('run')\n")
        (base / "simulation.json").write_text("{}\n")

    def test_the_adapter_written_into_optpilot_configs_finds_the_root(self) -> None:
        # The layout catalog_setup itself writes: adapter one level below the
        # simulator members. This is the case that used to fail.
        base = self.root / "workspace"
        (base / "optpilot_configs").mkdir(parents=True)
        self.members(base)
        module = self.load(base / "optpilot_configs" / "adapter.py")

        self.assertEqual(module._environment_source_simulator(), base)

    def test_a_generated_bundle_layout_still_resolves(self) -> None:
        base = self.root / "bundle"
        simulator = base / "resource-action-output" / "req-1" / "simulator"
        self.members(simulator)
        module = self.load(base / "adapter.py")

        self.assertEqual(module._environment_source_simulator(), simulator)

    def test_a_sibling_simulator_directory_still_resolves(self) -> None:
        base = self.root / "plain"
        self.members(base / "simulator")
        module = self.load(base / "adapter.py")

        self.assertEqual(module._environment_source_simulator(), base / "simulator")

    def test_replay_assembles_the_trial_shape_and_overlays_the_candidate(self) -> None:
        base = self.root / "workspace"
        (base / "optpilot_configs").mkdir(parents=True)
        self.members(base)
        workspace = self.root / "replay-ws"
        workspace.mkdir()
        candidate = self.root / "candidate"
        candidate.mkdir()
        (candidate / "SeatingCoordinator.py").write_text("policy = True\n")
        module = self.load(base / "optpilot_configs" / "adapter.py")

        simulator = module._prepared_simulator(workspace, candidate)

        self.assertTrue((simulator / "simulation.json").is_file())
        self.assertTrue((simulator / "devs_project" / "model.py").is_file())
        self.assertEqual(
            (
                simulator
                / "devs_project"
                / "RestaurantSystem_libs"
                / "SeatingCoordinator.py"
            ).read_text(),
            "policy = True\n",
        )

    def test_a_missing_tree_raises_a_path_free_layout_error(self) -> None:
        base = self.root / "empty"
        (base / "optpilot_configs").mkdir(parents=True)
        module = self.load(base / "optpilot_configs" / "adapter.py")

        with self.assertRaises(FileNotFoundError) as caught:
            module._environment_source_simulator()
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertIn("simulator tree", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
