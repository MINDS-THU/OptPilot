from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import signal
import shutil
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

from optpilot.attempts import (
    AttemptExecutor,
    AttemptWorkspaceBinding,
    EvaluationSpec,
)
from optpilot.candidate_materialization import (
    ParameterPassthroughMaterializer,
    ValidationReport,
)
from optpilot.realm.errors import RealmConflict
from optpilot_studio.ui.server import (
    UiState,
    _copy_plan_target,
    _configure_workspace_catalog_role,
    _connect_local_folder,
    _delete_ui_workspace,
    _discover_workspace_configs,
    _list_ui_workspaces,
    _prepare_package_plan,
    _simulation_environment_adapter_starter,
    _workspace_simulation_handoff,
)


class _AcceptingValidator:
    def validate(self, candidate, context):
        return ValidationReport(
            accepted=True,
            metadata={"implementation": "test.accepting"},
        )


class _ModuleEnvironmentAdapter:
    def __init__(self, callback):
        self.callback = callback

    def evaluate(self, candidate_runtime, context):
        return self.callback(candidate_runtime, context)


def _wheel_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.date_time = (2020, 1, 1, 0, 0, 0)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    return entry


def _add_python_runtime(root: Path, manifest: dict) -> dict:
    devs_project = root / "devs_project"
    devs_project.mkdir(exist_ok=True)
    devs_project.joinpath("__init__.py").write_text("", encoding="utf-8")
    runtime = root / "runtime_dependencies"
    vendor = runtime / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    wheel_name = "xdevs-3.0.0-py3-none-any.whl"
    wheel = vendor / wheel_name
    with zipfile.ZipFile(wheel, "w") as archive:
        files = {
            "xdevs/__init__.py": b"__version__ = '3.0.0'\n",
            "xdevs-3.0.0.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: xdevs\nVersion: 3.0.0\n"
            ),
            "xdevs-3.0.0.dist-info/WHEEL": (
                b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
            ),
            "xdevs-3.0.0.dist-info/LICENSE.txt": b"GNU GPL version 3\n",
            "xdevs-3.0.0.dist-info/RECORD": b"",
        }
        for name, payload in files.items():
            archive.writestr(_wheel_entry(name), payload)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (runtime / "requirements.lock").write_text(
        f"vendor/{wheel_name} --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    return {
        **manifest,
        "python_runtime": {
            "requirements_lock": "runtime_dependencies/requirements.lock"
        },
    }


class StudioLocalFolderWorkspaceTest(unittest.TestCase):
    def test_setup_writes_complete_resource_or_disabled_semantic_starter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            reference_root = root / "reference"
            environment_root = root / "environment"
            reference_root.mkdir()
            environment_root.mkdir()
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)
            reference = _connect_local_folder(
                state, {"path": str(reference_root)}
            )
            environment = _connect_local_folder(
                state, {"path": str(environment_root)}
            )

            configured = _configure_workspace_catalog_role(
                state,
                reference["id"],
                {
                    "role": "generator",
                    "id": "my-generator",
                    "description": "Creates models",
                },
            )
            starter = _configure_workspace_catalog_role(
                state,
                environment["id"],
                {"role": "environment", "id": "my-environment"},
            )

            self.assertEqual(configured["configuration"]["next_action"], "check")
            self.assertEqual(
                _discover_workspace_configs(state, reference["id"])["configs"][0][
                    "kind"
                ],
                "resource",
            )
            manifest = (reference_root / "optpilot.resource.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn("purpose: generator", manifest)
            self.assertTrue(starter["configuration"]["needs_editing"])
            self.assertEqual(
                _discover_workspace_configs(state, environment["id"])["configs"],
                [],
            )
            self.assertTrue(
                (environment_root / "optpilot_configs/environment.template.yaml.disabled").is_file()
            )
            self.assertTrue(
                (environment_root / "optpilot_configs/optpilot_adapter.py").is_file()
            )
            with self.assertRaisesRegex(RealmConflict, "did not overwrite"):
                _configure_workspace_catalog_role(
                    state,
                    reference["id"],
                    {"role": "generator", "id": "another-generator"},
                )

    def test_connect_retry_reuses_reference_and_removal_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            project = root / "existing-project"
            project.mkdir()
            source = project / "model.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)

            first = _connect_local_folder(
                state, {"path": str(project), "title": "Existing project"}
            )
            replay = _connect_local_folder(
                state, {"path": str(project), "title": "Existing project"}
            )

            self.assertEqual(replay["id"], first["id"])
            self.assertEqual(replay["ownership"], "external-reference")
            self.assertEqual(replay["purpose"], "user-project")
            self.assertEqual(
                [item["id"] for item in _list_ui_workspaces(state)], [first["id"]]
            )

            removed = _delete_ui_workspace(state, first["id"])

            self.assertFalse(removed["files_deleted"])
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual(_list_ui_workspaces(state), [])

    def test_generated_simulation_inputs_prefill_environment_starter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            simulation_root = root / "generated-simulation"
            simulation_root.mkdir()
            (simulation_root / "run.py").write_text(
                "print('ready')\n", encoding="utf-8"
            )
            (simulation_root / "simulation.json").write_text(
                json.dumps(
                    _add_python_runtime(simulation_root, {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 30,
                        "arguments": [
                            {
                                "name": "fleet_size",
                                "flag": "--fleet-size",
                                "type": "integer",
                                "default": 4,
                                "minimum": 1,
                                "maximum": 20,
                                "description": "Number of vehicles.",
                            },
                            {
                                "name": "policy",
                                "type": "string",
                                "choices": ["fifo", "priority"],
                            },
                        ],
                        "result_files": ["summary.json"],
                    })
                ),
                encoding="utf-8",
            )
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)
            workspace = _connect_local_folder(
                state,
                {"path": str(simulation_root), "title": "Generated simulation"},
            )

            configured = _configure_workspace_catalog_role(
                state,
                workspace["id"],
                {"role": "environment", "id": "generated-environment"},
            )

            detected = configured["configuration"]["detected_simulation"]
            self.assertEqual(detected["parameter_count"], 2)
            self.assertEqual(detected["result_file_count"], 1)
            self.assertEqual(detected["python_runtime"]["wheel_count"], 1)
            starter = yaml.safe_load(
                (
                    simulation_root
                    / "optpilot_configs"
                    / "environment.template.yaml.disabled"
                ).read_text(encoding="utf-8")
            )
            schema = starter["candidate"]["parameters"]["schema"]
            self.assertEqual(
                schema["fleet_size"],
                {
                    "valueType": "int",
                    "min": 1,
                    "max": 20,
                    "default": 4,
                    "description": "Number of vehicles.",
                },
            )
            self.assertEqual(
                schema["policy"],
                {
                    "valueType": "categorical",
                    "values": ["fifo", "priority"],
                },
            )
            self.assertEqual(
                starter["runtime"]["setup"],
                {
                    "cache": "prepared",
                    "timeoutSeconds": 300,
                    "steps": [
                        {
                            "uses": "python-venv",
                            "cwd": "..",
                            "requirements": [
                                "runtime_dependencies/requirements.lock"
                            ],
                        }
                    ],
                },
            )
            self.assertEqual(
                starter["trialWorkspace"],
                [
                    {"from": "../run.py", "to": "simulator/run.py"},
                    {
                        "from": "../simulation.json",
                        "to": "simulator/simulation.json",
                    },
                    {
                        "from": "../devs_project",
                        "to": "simulator/devs_project",
                    },
                ],
            )
            adapter = (
                simulation_root / "optpilot_configs" / "optpilot_adapter.py"
            ).read_text(encoding="utf-8")
            self.assertIn("simulation.json", adapter)
            self.assertIn("metric_values", adapter)
            self.assertIn('os.environ.get("PYTHONPATH")', adapter)
            self.assertIn(
                "_EXPECTED_REQUIREMENTS_LOCK = "
                "'runtime_dependencies/requirements.lock'",
                adapter,
            )

            enabled = simulation_root / "optpilot_configs" / "environment.yaml"
            (
                simulation_root
                / "optpilot_configs"
                / "environment.template.yaml.disabled"
            ).replace(enabled)
            prepared = _prepare_package_plan(
                state, workspace["id"], {"refresh": True}
            )
            target = prepared["package_plan"]["components"][0]
            self.assertEqual(target["kind"], "environment")
            self.assertTrue(
                {
                    "run.py",
                    "simulation.json",
                    "devs_project",
                    "runtime_dependencies/requirements.lock",
                    "runtime_dependencies/vendor/xdevs-3.0.0-py3-none-any.whl",
                }.issubset(set(target["include"]))
            )
            registered = root / "registered-environment"
            _copy_plan_target(simulation_root.resolve(), target, registered)
            registered_config = yaml.safe_load(
                (registered / "environment.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                registered_config["runtime"]["setup"]["steps"][0],
                {
                    "uses": "python-venv",
                    "cwd": ".",
                    "requirements": [
                        "runtime_dependencies/requirements.lock"
                    ],
                },
            )
            self.assertEqual(
                registered_config["trialWorkspace"],
                [
                    {"from": "run.py", "to": "simulator/run.py"},
                    {
                        "from": "simulation.json",
                        "to": "simulator/simulation.json",
                    },
                    {
                        "from": "devs_project",
                        "to": "simulator/devs_project",
                    },
                ],
            )
            self.assertTrue((registered / "run.py").is_file())
            self.assertTrue((registered / "simulation.json").is_file())
            self.assertTrue((registered / "devs_project" / "__init__.py").is_file())
            self.assertTrue(
                (
                    registered
                    / "runtime_dependencies"
                    / "vendor"
                    / "xdevs-3.0.0-py3-none-any.whl"
                ).is_file()
            )

    def test_generated_simulation_handoff_rejects_unsafe_or_inconsistent_manifests(
        self,
    ) -> None:
        valid_manifest = {
            "schema_version": "devs.simulation.v1",
            "entrypoint": "run.py",
            "timeout_seconds": 30,
            "arguments": [],
            "result_files": [],
        }
        cases = (
            (
                "missing runner",
                valid_manifest,
                False,
                "missing run.py",
            ),
            (
                "wrong typed default",
                {
                    **valid_manifest,
                    "arguments": [
                        {
                            "name": "count",
                            "type": "integer",
                            "default": "four",
                        }
                    ],
                },
                True,
                "does not match type integer",
            ),
            (
                "result traversal",
                {
                    **valid_manifest,
                    "result_files": ["../outside.json"],
                },
                True,
                "canonical relative path",
            ),
        )
        for label, manifest, include_runner, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                if include_runner:
                    (root / "run.py").write_text(
                        "print('ready')\n", encoding="utf-8"
                    )
                    manifest = _add_python_runtime(root, manifest)
                (root / "simulation.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, message):
                    _workspace_simulation_handoff(root)

    def test_generated_simulation_handoff_rejects_a_tampered_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "run.py").write_text("print('ready')\n", encoding="utf-8")
            manifest = _add_python_runtime(
                root,
                {
                    "schema_version": "devs.simulation.v1",
                    "entrypoint": "run.py",
                    "timeout_seconds": 30,
                    "arguments": [],
                    "result_files": [],
                },
            )
            (root / "simulation.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            wheel = (
                root
                / "runtime_dependencies"
                / "vendor"
                / "xdevs-3.0.0-py3-none-any.whl"
            )
            wheel.write_bytes(wheel.read_bytes() + b"tampered")

            with self.assertRaisesRegex(ValueError, "hash does not match"):
                _workspace_simulation_handoff(root)

    def test_generated_simulation_adapter_runs_through_attempt_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            simulation_root = root / "generated-simulation"
            simulation_root.mkdir()
            (simulation_root / "run.py").write_text(
                "import argparse, json, os\n"
                "import retained_test_dependency\n"
                "from pathlib import Path\n"
                "assert 'OPENROUTER_API_KEY' not in os.environ\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--fleet-size', type=int, default=4)\n"
                "args = parser.parse_args()\n"
                "result_root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
                "result_root.joinpath('summary.json').write_text(\n"
                "    json.dumps({'metrics': {'score': args.fleet_size * retained_test_dependency.MULTIPLIER}})\n"
                ")\n",
                encoding="utf-8",
            )
            retained_layer = root / "retained-layer"
            retained_layer.mkdir()
            retained_layer.joinpath("retained_test_dependency.py").write_text(
                "MULTIPLIER = 2\n", encoding="utf-8"
            )
            (simulation_root / "simulation.json").write_text(
                json.dumps(
                    _add_python_runtime(simulation_root, {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 30,
                        "arguments": [
                            {
                                "name": "fleet_size",
                                "flag": "--fleet-size",
                                "type": "integer",
                                "default": 4,
                                "minimum": 1,
                                "maximum": 20,
                            }
                        ],
                        "result_files": ["summary.json"],
                    })
                ),
                encoding="utf-8",
            )
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)
            workspace = _connect_local_folder(
                state,
                {"path": str(simulation_root), "title": "Generated simulation"},
            )
            _configure_workspace_catalog_role(
                state,
                workspace["id"],
                {"role": "environment", "id": "generated-environment"},
            )

            adapter_path = (
                simulation_root / "optpilot_configs" / "optpilot_adapter.py"
            )
            module_spec = importlib.util.spec_from_file_location(
                "generated_simulation_adapter", adapter_path
            )
            self.assertIsNotNone(module_spec)
            assert module_spec is not None and module_spec.loader is not None
            adapter_module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(adapter_module)

            attempt_workspace = root / "attempt"
            attempt_workspace.mkdir()
            attempt_simulator = attempt_workspace / "simulator"
            attempt_simulator.mkdir()
            shutil.copy2(
                simulation_root / "run.py", attempt_simulator / "run.py"
            )
            shutil.copy2(
                simulation_root / "simulation.json",
                attempt_simulator / "simulation.json",
            )
            shutil.copytree(
                simulation_root / "devs_project",
                attempt_simulator / "devs_project",
                dirs_exist_ok=True,
            )
            evaluation = EvaluationSpec(
                environment_id="generated-environment",
                environment_revision_digest="a" * 64,
                prepared_runtime_digest="b" * 64,
                candidate_ref="candidate:sha256:" + "c" * 64,
                candidate={
                    "candidate_id": "candidate-1",
                    "format": "parameters",
                    "spec": {"fleet_size": 6},
                    "lineage": {"parents": []},
                    "generator": {
                        "method_id": "test-method",
                        "strategy": "test",
                    },
                    "validation": {
                        "implementation": "test.accepting",
                        "config": {},
                    },
                    "materialization": {
                        "implementation": "builtin.parameter_to_config",
                        "config": {},
                    },
                },
                objective={
                    "primaryMetric": {
                        "name": "score",
                        "direction": "maximize",
                    }
                },
                resource_profile={
                    "cpu": 1,
                    "memoryGiB": 1,
                    "timeoutSeconds": 30,
                },
                sandbox_spec={
                    "runtimeType": "process",
                    "networkPolicy": "disabled",
                },
                seed=1,
                repetition_index=0,
            )
            binding = AttemptWorkspaceBinding(
                binding_id="binding-generated",
                workspace=attempt_workspace,
                backend_identity={"implementation": "test.backend"},
                backend_worker={"handle": "test-worker"},
            )
            with patch.dict(
                "os.environ",
                {
                    "PYTHONPATH": str(retained_layer),
                    "OPENROUTER_API_KEY": "must-not-reach-simulator",
                },
                clear=False,
            ):
                envelope = AttemptExecutor(
                    _AcceptingValidator(),
                    ParameterPassthroughMaterializer({}, None),
                    _ModuleEnvironmentAdapter(adapter_module.evaluate),
                ).execute(
                    evaluation,
                    binding,
                    attempt_id="attempt-generated",
                )

            self.assertEqual(envelope.outcome, "success")
            self.assertEqual(dict(envelope.metric_values), {"score": 12})
            self.assertEqual(len(envelope.output_declarations), 1)
            output = envelope.output_declarations[0]
            self.assertEqual(
                output.path, "simulation_results/summary.json"
            )
            self.assertEqual(output.kind, "file")
            self.assertEqual(output.media_type, "application/json")

    def test_generated_simulation_adapter_stops_on_bounded_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            simulator = root / "simulator"
            simulator.mkdir()
            (simulator / "run.py").write_text(
                "import sys\n"
                "sys.stdout.write('x' * (1024 * 1024 + 1))\n"
                "sys.stdout.flush()\n",
                encoding="utf-8",
            )
            (simulator / "simulation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 30,
                        "arguments": [],
                        "result_files": [],
                        "python_runtime": {
                            "requirements_lock": "runtime_dependencies/requirements.lock"
                        },
                    }
                ),
                encoding="utf-8",
            )
            adapter_namespace = {}
            exec(
                _simulation_environment_adapter_starter(
                    "runtime_dependencies/requirements.lock"
                ),
                adapter_namespace,
            )

            with self.assertRaisesRegex(RuntimeError, "output limit"):
                adapter_namespace["evaluate"](
                    {},
                    {"workspace": str(root)},
                )

    def test_generated_simulation_adapter_rejects_changed_dependency_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            simulator = root / "simulator"
            simulator.mkdir()
            (simulator / "run.py").write_text(
                "raise AssertionError('manifest mismatch must fail before execution')\n",
                encoding="utf-8",
            )
            (simulator / "simulation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 30,
                        "arguments": [],
                        "result_files": [],
                        "python_runtime": {
                            "requirements_lock": "changed/requirements.lock"
                        },
                    }
                ),
                encoding="utf-8",
            )
            adapter_namespace = {}
            exec(
                _simulation_environment_adapter_starter(
                    "runtime_dependencies/requirements.lock"
                ),
                adapter_namespace,
            )

            with self.assertRaisesRegex(ValueError, "changed after Workspace Setup"):
                adapter_namespace["evaluate"](
                    {},
                    {"workspace": str(root)},
                )

    def test_generated_simulation_adapter_requires_canonical_bound_lock(self) -> None:
        for value in ("", "../requirements.lock", "/requirements.lock", "bad\\lock"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "requirements lock"
            ):
                _simulation_environment_adapter_starter(value)

    @unittest.skipUnless(
        os.name == "posix",
        "Process-group descendant cleanup is a POSIX runtime guarantee.",
    )
    def test_generated_simulation_adapter_stops_descendants_after_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            simulator = root / "simulator"
            simulator.mkdir()
            (simulator / "run.py").write_text(
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "result_root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
                "heartbeat = result_root / 'child-heartbeat.txt'\n"
                "child_code = (\n"
                "    \"import sys, time\\n\"\n"
                "    \"path = sys.argv[1]\\n\"\n"
                "    \"with open(path, 'a', encoding='utf-8') as stream:\\n\"\n"
                "    \"    while True:\\n\"\n"
                "    \"        stream.write('tick\\\\n')\\n\"\n"
                "    \"        stream.flush()\\n\"\n"
                "    \"        time.sleep(0.02)\\n\"\n"
                ")\n"
                "child = subprocess.Popen(\n"
                "    [sys.executable, '-c', child_code, str(heartbeat)],\n"
                "    stdin=subprocess.DEVNULL,\n"
                "    stdout=subprocess.DEVNULL,\n"
                "    stderr=subprocess.DEVNULL,\n"
                ")\n"
                "(result_root / 'child.pid').write_text(str(child.pid))\n"
                "deadline = time.monotonic() + 2\n"
                "while (not heartbeat.exists() or heartbeat.stat().st_size == 0):\n"
                "    if time.monotonic() >= deadline:\n"
                "        raise RuntimeError('child did not start')\n"
                "    time.sleep(0.01)\n"
                "(result_root / 'summary.json').write_text(\n"
                "    json.dumps({'metrics': {'score': 1}}), encoding='utf-8'\n"
                ")\n",
                encoding="utf-8",
            )
            (simulator / "simulation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 30,
                        "arguments": [],
                        "result_files": ["summary.json"],
                        "python_runtime": {
                            "requirements_lock": "runtime_dependencies/requirements.lock"
                        },
                    }
                ),
                encoding="utf-8",
            )
            adapter_namespace = {}
            exec(
                _simulation_environment_adapter_starter(
                    "runtime_dependencies/requirements.lock"
                ),
                adapter_namespace,
            )
            child_pid = None
            try:
                result = adapter_namespace["evaluate"](
                    {},
                    {"workspace": str(root)},
                )
                self.assertEqual(result["metric_values"], {"score": 1})
                result_root = root / "simulation_results"
                child_pid = int(
                    (result_root / "child.pid").read_text(encoding="utf-8")
                )
                heartbeat = result_root / "child-heartbeat.txt"
                settled_size = heartbeat.stat().st_size
                time.sleep(0.2)
                self.assertEqual(
                    heartbeat.stat().st_size,
                    settled_size,
                    "A simulator descendant survived the completed evaluation.",
                )
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_connect_rejects_missing_and_outside_authorized_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)

            with self.assertRaisesRegex(ValueError, "does not exist"):
                _connect_local_folder(state, {"path": "missing"})
            with self.assertRaises(PermissionError):
                _connect_local_folder(state, {"path": outside_dir})


if __name__ == "__main__":
    unittest.main()
