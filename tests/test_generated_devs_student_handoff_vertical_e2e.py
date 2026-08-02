from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path

import yaml

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
)
from optpilot.realm.run_definition import RUN_METHOD_SOURCE_ROLE
from optpilot.runtime_scopes import ENVIRONMENT_PREPARED_PYTHON_SCOPE
from optpilot_studio.ui.server import (
    STUDY_LAUNCH_REQUEST_SCHEMA,
    UiState,
    _apply_package_plan,
    _catalog_payload,
    _configure_workspace_catalog_role,
    _execute_study_launch_request,
    _keep_selection_as_ui_workspace,
    _prepare_package_plan,
    _smoke_package_plan,
    _validate_package_plan,
)


_WHEEL_NAME = "xdevs-3.0.0-py3-none-any.whl"


def _wheel_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.date_time = (2020, 1, 1, 0, 0, 0)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    return entry


def _record_hash(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _write_minimal_xdevs_wheel(path: Path) -> str:
    """Write a deterministic local wheel without invoking a build tool or network."""

    path.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "xdevs/__init__.py": (
            b'"""Small deterministic xDEVS stand-in for the vertical test."""\n'
            b"__version__ = '3.0.0'\n"
            b"\n"
            b"def simulated_throughput(production_rate):\n"
            b"    return float(production_rate) * 3.0\n"
        ),
        "xdevs-3.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\n"
            b"Name: xdevs\n"
            b"Version: 3.0.0\n"
            b"Summary: Deterministic vertical-test runtime\n"
        ),
        "xdevs-3.0.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: optpilot-generated-devs-vertical-e2e\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        "xdevs-3.0.0.dist-info/LICENSE.txt": b"Vertical test fixture only.\n",
    }
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for name, payload in sorted(files.items()):
        writer.writerow((name, _record_hash(payload), len(payload)))
    record_name = "xdevs-3.0.0.dist-info/RECORD"
    writer.writerow((record_name, "", ""))
    files[record_name] = record_stream.getvalue().encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in sorted(files.items()):
            archive.writestr(_wheel_entry(name), payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_interface_generated_simulation(root: Path) -> bytes:
    """Create the exact portable bundle that the generator reports as an output."""

    root.mkdir(parents=True)
    devs_project = root / "devs_project"
    devs_project.mkdir()
    (devs_project / "__init__.py").write_text("", encoding="utf-8")
    (devs_project / "model.py").write_text(
        "from xdevs import simulated_throughput\n"
        "\n"
        "def run_model(production_rate):\n"
        "    return simulated_throughput(production_rate)\n",
        encoding="utf-8",
    )
    runner = (
        "import argparse\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "from devs_project.model import run_model\n"
        "\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--production-rate', type=float, default=4.0)\n"
        "args = parser.parse_args()\n"
        "throughput = run_model(args.production_rate)\n"
        "result_root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
        "result_root.mkdir(parents=True, exist_ok=True)\n"
        "result_root.joinpath('summary.json').write_text(\n"
        "    json.dumps({\n"
        "        'metrics': {'throughput': throughput},\n"
        "        'events': [\n"
        "            {'time': 0, 'event': 'factory-started'},\n"
        "            {'time': 1, 'event': 'shipment-completed'},\n"
        "        ],\n"
        "    }),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print(f'Simulated throughput: {throughput}')\n"
    ).encode("utf-8")
    (root / "run.py").write_bytes(runner)
    wheel = root / "runtime_dependencies" / "vendor" / _WHEEL_NAME
    wheel_digest = _write_minimal_xdevs_wheel(wheel)
    (wheel.parent.parent / "requirements.lock").write_text(
        f"vendor/{_WHEEL_NAME} --hash=sha256:{wheel_digest}\n",
        encoding="utf-8",
    )
    (root / "simulation.json").write_text(
        json.dumps(
            {
                "schema_version": "devs.simulation.v1",
                "entrypoint": "run.py",
                "timeout_seconds": 30,
                "arguments": [
                    {
                        "name": "production_rate",
                        "flag": "--production-rate",
                        "type": "number",
                        "default": 4.0,
                        "minimum": 1.0,
                        "maximum": 10.0,
                        "description": "Factory units produced per time step.",
                    }
                ],
                "result_files": ["summary.json"],
                "python_runtime": {
                    "requirements_lock": (
                        "runtime_dependencies/requirements.lock"
                    )
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return runner


def _finish_workspace_setup(workspace_root: Path) -> None:
    """Perform the small, explicit edits a student makes after Setup."""

    configs = workspace_root / "optpilot_configs"
    disabled = configs / "environment.template.yaml.disabled"
    environment = yaml.safe_load(disabled.read_text(encoding="utf-8"))
    environment["description"] = (
        "Run the generated factory simulation and maximize its throughput."
    )
    environment["metrics"] = {
        "source": "return",
        "keys": ["throughput"],
    }
    (configs / "environment.yaml").write_text(
        yaml.safe_dump(environment, sort_keys=False),
        encoding="utf-8",
    )
    disabled.unlink()

    (configs / "student_method.py").write_text(
        "class StudentMethod:\n"
        "    def __init__(self, definition, study_spec, rng):\n"
        "        self.definition = definition\n"
        "        self.emitted = False\n"
        "\n"
        "    def propose(self, n_candidates, study_state, evidence_view=None):\n"
        "        if self.emitted or n_candidates <= 0:\n"
        "            return []\n"
        "        self.emitted = True\n"
        "        return [{\n"
        "            'candidate_id': 'student-production-rate',\n"
        "            'format': 'parameters',\n"
        "            'spec': {'production_rate': 6.0},\n"
        "            'lineage': {'parents': []},\n"
        "            'generator': {\n"
        "                'method_id': self.definition['id'],\n"
        "                'strategy': 'one-course-example',\n"
        "            },\n"
        "        }]\n"
        "\n"
        "    def observe(self, observations):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (configs / "method.yaml").write_text(
        """\
apiVersion: optpilot.io/v1
config: method
id: student-one-candidate
description: Propose one understandable production rate for the course example.
entrypoint:
  python: student_method:StudentMethod
  pythonPath: [.]
  protocol: batch
settings:
  batchSize: 1
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
""",
        encoding="utf-8",
    )
    (configs / "study.yaml").write_text(
        """\
apiVersion: optpilot.io/v1
config: study
name: generated-simulation-student-smoke
description: Run the generated simulation through OptPilot's ordinary Study path.
environmentConfig: environment.yaml
methodConfig: method.yaml
objective:
  metric: throughput
  direction: maximize
budget:
  maxTrials: 1
execution:
  parallelism: 1
  timeoutSeconds: 30
reproducibility:
  seed: 23
""",
        encoding="utf-8",
    )


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class GeneratedDevsStudentHandoffVerticalE2ETest(unittest.TestCase):
    def test_generated_output_becomes_a_runnable_retained_study(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)
        studio_root = root / "studio"
        studio_root.mkdir()
        state = UiState(
            cwd=studio_root,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=runtime,
        )
        self.addCleanup(state.close_catalog_projections)
        self.addCleanup(state.close_coordination)

        output_root = root / "interface-outputs"
        generated_root = output_root / "student-factory-simulation"
        exact_runner = _write_interface_generated_simulation(generated_root)
        output_session = runtime.interface_outputs.create_session(
            operation_id="generated-devs-student/output-session",
            launch_id="generated-devs-student-interface",
            ttl_seconds=60,
        )
        captured = runtime.interface_outputs.capture_tree_selection(
            handle=output_session,
            label="Student factory simulation",
            relative_path="student-factory-simulation",
            root_handle="output",
            root_path=output_root,
        )
        self.assertIsNotNone(captured.ready_generation)
        assert captured.ready_generation is not None
        selection = captured.ready_generation.selection
        self.assertIsNotNone(selection)
        assert selection is not None

        # The mutable interface directory is no longer authoritative after capture.
        (generated_root / "run.py").write_text(
            "raise RuntimeError('changed after exact output publication')\n",
            encoding="utf-8",
        )
        kept = _keep_selection_as_ui_workspace(
            state,
            operation_id="generated-devs-student/save-as-workspace",
            selection=selection,
            title="Student factory simulation",
            description="Exact generated output kept for downstream optimization",
        )
        runtime.interface_outputs.retire_session(
            operation_id="generated-devs-student/retire-output-session",
            handle=output_session,
        )
        workspace = kept["workspace"]
        workspace_id = workspace["id"]
        workspace_root = Path(workspace["root"])
        self.assertEqual(workspace["ownership"], "realm-managed")
        self.assertEqual((workspace_root / "run.py").read_bytes(), exact_runner)

        configured = _configure_workspace_catalog_role(
            state,
            workspace_id,
            {
                "role": "environment",
                "id": "student-factory-environment",
                "description": "Evaluate the generated factory simulation.",
            },
        )
        detected = configured["configuration"]["detected_simulation"]
        self.assertEqual(detected["parameter_count"], 1)
        self.assertEqual(detected["result_file_count"], 1)
        self.assertEqual(detected["python_runtime"]["wheel_count"], 1)
        _finish_workspace_setup(workspace_root)

        prepared = _prepare_package_plan(
            state,
            workspace_id,
            {
                "package_id": "generated-devs-student-package",
                "refresh": True,
            },
        )
        plan = prepared["package_plan"]
        self.assertEqual(plan["classification"], "environment-plus-method")
        self.assertEqual(len(plan["studies"]), 1)
        checked = _validate_package_plan(state, workspace_id, plan["id"])
        plan = checked["package_plan"]
        self.assertTrue(plan["validation"]["valid"], plan)
        artifact_ref = plan["artifact"]["content_ref"]
        smoked = _smoke_package_plan(
            state,
            workspace_id,
            plan["id"],
            {"max_trials": 1, "timeout_seconds": 60},
        )
        self.assertTrue(smoked["smoke"]["valid"], smoked)
        self.assertEqual(smoked["smoke"]["artifact_ref"], artifact_ref)
        registered = _apply_package_plan(state, workspace_id, plan["id"])
        self.assertTrue(registered["applied"], registered)

        head = runtime.catalog.read_head(
            package_id="generated-devs-student-package"
        )
        self.assertIsNotNone(head)
        assert head is not None
        published = runtime.catalog.read_revision(
            package_id=head.package_id,
            revision=head.revision,
        )
        self.assertEqual(str(published.root_ref), artifact_ref)
        catalog_studies = _catalog_payload(state)["studies"]
        self.assertEqual(len(catalog_studies), 1)
        listed = catalog_studies[0]
        self.assertTrue(listed["validation"]["launch"]["eligible"], listed)

        response, status = _execute_study_launch_request(
            state,
            {
                "schema": STUDY_LAUNCH_REQUEST_SCHEMA,
                "request_id": "94a6cedd-c73e-44d8-8d3d-275eedfac7c1",
                "study_ref": listed["ref"],
            },
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        launch_id = response["launch"]["launch_id"]
        deadline = time.monotonic() + 60
        summary = None
        while time.monotonic() < deadline:
            launch = runtime.study_launches.read(launch_id=launch_id)
            if launch.run_id is not None:
                candidate = runtime.run_reader.summary(run_id=launch.run_id)
                if candidate.run_status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    summary = candidate
                    break
            if launch.job.state.terminal and launch.run_id is None:
                self.fail(f"Study launch failed before Run handoff: {launch}")
            time.sleep(0.05)
        self.assertIsNotNone(summary, "The retained Study did not become terminal.")
        assert summary is not None
        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.candidate_count, 1)
        self.assertEqual(summary.successful_logical_trials, 1)

        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=summary.run_id,
        )
        self.assertEqual(len(snapshot.observations), 1)
        observation = snapshot.observations[0].envelope
        self.assertEqual(observation.outcome, "success")
        self.assertEqual(observation.metric_values["throughput"], 18.0)
        self.assertEqual(len(snapshot.artifacts), 1)
        result = snapshot.artifacts[0]
        self.assertEqual(result.declaration.path, "simulation_results/summary.json")
        self.assertEqual(result.declaration.media_type, "application/json")
        result_manifest = runtime.content_store.verify_blob(result.content_ref)
        self.assertEqual(result_manifest.size, result.size_bytes)
        self.assertGreater(result.size_bytes, 0)

        definition = snapshot.definition
        self.assertEqual(
            definition.content_refs_by_role[RUN_ENVIRONMENT_SOURCE_ROLE],
            (published.root_ref,),
        )
        self.assertEqual(
            definition.content_refs_by_role[RUN_METHOD_SOURCE_ROLE],
            (published.root_ref,),
        )
        prepared_layers = (
            definition.evaluation_closure.prepared_runtime.prepared_layers
        )
        self.assertEqual(len(prepared_layers), 1)
        self.assertEqual(
            prepared_layers[0].scope,
            ENVIRONMENT_PREPARED_PYTHON_SCOPE,
        )
        self.assertEqual(
            definition.content_refs_by_role[RUN_PREPARED_RUNTIME_ROLE],
            (prepared_layers[0].snapshot_ref,),
        )
        trial_layers = (
            definition.evaluation_closure.environment_revision.attempt_input_layers
        )
        self.assertEqual(
            {
                layer.destination_subpath
                for layer in trial_layers
            },
            {
                "simulator/run.py",
                "simulator/simulation.json",
                "simulator/devs_project",
            },
        )


if __name__ == "__main__":
    unittest.main()
