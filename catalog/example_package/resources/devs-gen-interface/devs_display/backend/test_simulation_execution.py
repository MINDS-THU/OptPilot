import json
import hashlib
import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from default_tools.interface_output_action import (
    OutputActionResult,
    OutputActionUnavailable,
)
from devs_tools.devs_construct_recon.tools.simulation.result_summary_contract import (
    require_event_trace_contract,
    require_result_summary_contract,
)

from devs_display.backend.simulation_execution import (
    ExecutionStateError,
    SIMULATION_SCHEMA,
    XDEVS_LICENSE_PATH,
    XDEVS_NOTICE_PATH,
    XDEVS_REQUIREMENTS_LOCK,
    XDEVS_WHEEL_NAME,
    XDEVS_WHEEL_PATH,
    SimulationBundleError,
    SimulationExecutionService,
    SimulationManifestError,
    assess_behavior_smoke,
    ensure_simulation_manifest,
    simulation_metadata,
)


def write_manifest(bundle: Path, *, arguments=None, timeout=5, result_files=None):
    (bundle / "simulation.json").write_text(
        json.dumps(
            {
                "schema_version": SIMULATION_SCHEMA,
                "entrypoint": "run.py",
                "timeout_seconds": timeout,
                "arguments": arguments or [],
                "result_files": result_files or [],
            }
        ),
        encoding="utf-8",
    )


def write_bundle(root: Path, run_source: str, *, arguments=None, timeout=5, result_files=None) -> Path:
    bundle = root / "simulator"
    bundle.mkdir()
    (bundle / "run.py").write_text(run_source, encoding="utf-8")
    write_manifest(
        bundle,
        arguments=arguments,
        timeout=timeout,
        result_files=result_files,
    )
    return bundle


def write_behavior_evidence(
    result_root: Path,
    components: list[str],
    *,
    truncated: bool = False,
    malformed: bool = False,
) -> None:
    result_root.mkdir(parents=True, exist_ok=True)
    (result_root / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "devs.simulation-result.v1",
                "metrics": {},
                "run": {"completed": True, "simulated_time": 10.0},
            }
        ),
        encoding="utf-8",
    )
    rows = [
        json.dumps(
            {
                "record_type": "event",
                "component": component,
                "port": "out",
                "time": float(index + 1),
                "value": index,
            }
        )
        for index, component in enumerate(components)
    ]
    if malformed:
        rows.append("{not-json")
    else:
        rows.append(
            json.dumps(
                {
                    "record_type": "summary",
                    "recorded_events": len(components),
                    "dropped_events": 1 if truncated else 0,
                    "truncated": truncated,
                }
            )
        )
    (result_root / "event_trace.jsonl").write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def write_behavior_bundle(root: Path, topology: str) -> Path:
    """Write tiny source-only DEVS topology fixtures for the local gate."""

    bundle = root / "bundle"
    project = bundle / "devs_project"
    project.mkdir(parents=True)
    (project / "Source.py").write_text(
        "from xdevs.models import Atomic, Port\n"
        "class Source(Atomic):\n"
        "    def __init__(self, name='source'):\n"
        "        super().__init__(name)\n"
        "        self.add_out_port(Port(dict, 'out'))\n",
        encoding="utf-8",
    )
    (project / "Worker.py").write_text(
        "from xdevs.models import Atomic, Port\n"
        "class Worker(Atomic):\n"
        "    def __init__(self, name='worker'):\n"
        "        super().__init__(name)\n"
        "        self.add_in_port(Port(dict, 'in'))\n"
        "        self.add_out_port(Port(dict, 'out'))\n",
        encoding="utf-8",
    )
    (project / "Sink.py").write_text(
        "from xdevs.models import Atomic, Port\n"
        "class Sink(Atomic):\n"
        "    def __init__(self, name='sink'):\n"
        "        super().__init__(name)\n"
        "        self.add_in_port(Port(dict, 'in'))\n",
        encoding="utf-8",
    )
    if topology == "single":
        (project / "Root.py").write_text(
            "from xdevs.models import Atomic, Port\n"
            "class Root(Atomic):\n"
            "    def __init__(self, name='root'):\n"
            "        super().__init__(name)\n"
            "        self.add_out_port(Port(dict, 'out'))\n",
            encoding="utf-8",
        )
        return bundle

    middle = "self.worker" if topology in {"pipeline", "dynamic"} else "self.sink"
    root_source = (
        "from xdevs.models import Coupled\n"
        "from .Source import Source\n"
        "from .Worker import Worker\n"
        "from .Sink import Sink\n"
        "class Root(Coupled):\n"
        "    def __init__(self, name='root'):\n"
        "        super().__init__(name)\n"
        "        self.source = Source(name='source')\n"
        f"        {middle} = "
        + ("Worker(name='worker')\n" if middle == "self.worker" else "Sink(name='sink')\n")
        + "        self.add_component(self.source)\n"
        f"        self.add_component({middle})\n"
        f"        self.add_coupling(self.source.output['out'], {middle}.input['in'])\n"
    )
    if topology == "dynamic":
        root_source += (
            "        children = []\n"
            "        for child in children:\n"
            "            self.add_component(child)\n"
        )
    (project / "Root.py").write_text(root_source, encoding="utf-8")
    return bundle


class SimulationExecutionTests(unittest.TestCase):
    def make_service(self, root: Path, **overrides):
        settings = {
            "allowed_bundle_root": root / "bundles",
            "maximum_timeout_seconds": 10,
            "queue_timeout_seconds": 2,
            "stop_grace_seconds": 0.1,
            "execution_mode": "process",
            "allow_trusted_process": True,
        }
        settings.update(overrides)
        return SimulationExecutionService(
            root / "executions",
            sys.executable,
            **settings,
        )

    @staticmethod
    def _package_minimal_runtime(bundle: Path) -> None:
        wheel = bundle / XDEVS_WHEEL_PATH
        wheel.parent.mkdir(parents=True, exist_ok=True)
        payload = b"test-only-pure-python-wheel"
        wheel.write_bytes(payload)
        lock = bundle / XDEVS_REQUIREMENTS_LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            "vendor/"
            + XDEVS_WHEEL_NAME
            + " --hash=sha256:"
            + hashlib.sha256(payload).hexdigest()
            + "\n",
            encoding="utf-8",
        )

    @unittest.skipUnless(
        os.environ.get("DEVS_RUN_CONTAINER_SMOKE") == "1",
        "set DEVS_RUN_CONTAINER_SMOKE=1 to exercise the local Docker/Podman boundary",
    )
    def test_live_container_boundary_is_offline_read_only_and_credential_free(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "must-not-cross-container-boundary"},
        ):
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(
                bundles,
                "import json, os, socket\n"
                "from pathlib import Path\n"
                "import xdevs\n"
                "def denied(action):\n"
                "    try:\n"
                "        action()\n"
                "        return False\n"
                "    except OSError:\n"
                "        return True\n"
                "network_blocked = denied(lambda: socket.create_connection(('203.0.113.1', 9), timeout=0.2))\n"
                "root_read_only = denied(lambda: Path('/forbidden').write_text('x'))\n"
                "source_read_only = denied(lambda: Path('forbidden').write_text('x'))\n"
                "payload = {\n"
                "  'credential': os.getenv('OPENROUTER_API_KEY', 'missing'),\n"
                "  'network_blocked': network_blocked,\n"
                "  'root_read_only': root_read_only,\n"
                "  'source_read_only': source_read_only,\n"
                "  'xdevs_imported': bool(xdevs),\n"
                "}\n"
                "target = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR']) / 'probe.json'\n"
                "target.write_text(json.dumps(payload), encoding='utf-8')\n"
                "print(json.dumps(payload, sort_keys=True))\n",
                result_files=["probe.json"],
            )
            service = SimulationExecutionService(
                root / "executions",
                sys.executable,
                allowed_bundle_root=bundles,
                maximum_timeout_seconds=10,
                execution_mode="container",
                container_engine="docker",
                container_image="optpilot/workspace-dev:latest",
            )
            record = service.execute(bundle)
            self.assertEqual(record["status"], "succeeded", record)
            result_path = (
                root
                / "executions"
                / record["execution_id"]
                / "results"
                / "probe.json"
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["credential"], "missing")
            self.assertTrue(payload["network_blocked"])
            self.assertTrue(payload["root_read_only"])
            self.assertTrue(payload["source_read_only"])
            self.assertTrue(payload["xdevs_imported"])

    def test_managed_launch_uses_output_action_and_retains_required_results(self):
        class FakeOutputAction:
            def __init__(self):
                self.calls = []

            def execute(self, **kwargs):
                self.calls.append(kwargs)
                result_root = Path(kwargs["results_directory"])
                result_root.joinpath("summary.json").write_text(
                    '{"completed": true}\n',
                    encoding="utf-8",
                )
                return OutputActionResult(
                    request_id=kwargs["request_id"],
                    action_id="run-simulation",
                    snapshot_ref="snapshot:test",
                    status="succeeded",
                    exit_code=0,
                    duration_seconds=0.25,
                    stdout="simulation complete\n",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    result_files=(),
                    failure_code=None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(
                bundles,
                "raise RuntimeError('the broker, not this process, must execute')\n",
                arguments=[
                    {
                        "name": "seed",
                        "type": "integer",
                        "required": True,
                    }
                ],
                result_files=["summary.json"],
            )
            broker = FakeOutputAction()
            with patch(
                "devs_display.backend.simulation_execution._package_xdevs_runtime",
                self._package_minimal_runtime,
            ):
                service = self.make_service(
                    root,
                    output_action_executor=broker,
                )
                record = service.execute(bundle, {"seed": 7})

            self.assertEqual(record["status"], "succeeded", record)
            self.assertEqual(record["duration_seconds"], 0.25)
            self.assertEqual(record["stdout"], "simulation complete\n")
            self.assertEqual(
                [item["path"] for item in record["result_files"]],
                ["summary.json"],
            )
            self.assertEqual(len(broker.calls), 1)
            self.assertEqual(broker.calls[0]["arguments"], ("--seed", "7"))
            self.assertEqual(broker.calls[0]["timeout_seconds"], 5)
            self.assertEqual(
                broker.calls[0]["request_id"], record["execution_id"]
            )
            staged_run = Path(broker.calls[0]["source_directory"]) / "run.py"
            self.assertIn(
                "the broker, not this process",
                staged_run.read_text(encoding="utf-8"),
            )

    def test_output_action_statuses_keep_existing_failure_semantics(self):
        base = dict(
            request_id="exec_0123456789abcdef0123456789abcdef",
            action_id="run-simulation",
            snapshot_ref="snapshot:test",
            exit_code=0,
            duration_seconds=0.1,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            result_files=(),
            failure_code=None,
        )
        truncated = OutputActionResult(
            **{**base, "status": "succeeded", "stdout_truncated": True}
        )
        self.assertEqual(
            SimulationExecutionService._translate_output_action_result(truncated),
            (
                "failed",
                "output_limit",
                "Execution exceeded the stdout or stderr limit.",
            ),
        )
        infrastructure = OutputActionResult(
            **{
                **base,
                "status": "infrastructure_failed",
                "exit_code": None,
                "failure_code": "container_provider_failed",
            }
        )
        status, failure_kind, message = (
            SimulationExecutionService._translate_output_action_result(
                infrastructure
            )
        )
        self.assertEqual(status, "failed")
        self.assertEqual(failure_kind, "execution_boundary")
        self.assertIn("container_provider_failed", message)

    def test_broker_failure_never_falls_back_to_local_execution(self):
        class UnavailableOutputAction:
            def execute(self, **_kwargs):
                raise OutputActionUnavailable("test broker is unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            marker = root / "local-fallback-ran"
            bundle = write_bundle(
                bundles,
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
            )
            with patch(
                "devs_display.backend.simulation_execution._package_xdevs_runtime",
                self._package_minimal_runtime,
            ):
                record = self.make_service(
                    root,
                    output_action_executor=UnavailableOutputAction(),
                ).execute(bundle)

            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["failure_kind"], "execution_boundary")
            self.assertIn("test broker is unavailable", record["message"])
            self.assertFalse(marker.exists())

    def test_manifest_is_derived_from_inner_argparse_without_importing_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "simulator"
            runner = bundle / "devs_project" / "run_demo.py"
            runner.parent.mkdir(parents=True)
            (bundle / "run.py").write_text(
                'SIM_MODULE = "devs_project.run_demo"\n', encoding="utf-8"
            )
            runner.write_text(
                "import argparse\n"
                "raise RuntimeError('must not be imported')\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--seed', type=int, default=4, help='Random seed')\n"
                "parser.add_argument('--fast-mode', action='store_true')\n"
                "parser.add_argument(dynamic_flag, type=float)\n",
                encoding="utf-8",
            )

            manifest_path = ensure_simulation_manifest(bundle)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema_version"], SIMULATION_SCHEMA)
            self.assertEqual(manifest["entrypoint"], "run.py")
            self.assertEqual(
                [argument["name"] for argument in manifest["arguments"]],
                ["seed", "fast_mode"],
            )
            self.assertEqual(manifest["arguments"][0]["type"], "integer")
            self.assertEqual(manifest["arguments"][0]["default"], 4)
            self.assertEqual(manifest["arguments"][1]["flag"], "--fast-mode")
            self.assertEqual(manifest["arguments"][1]["action"], "store_true")
            metadata = simulation_metadata(bundle)
            self.assertEqual(metadata["parameters"], manifest["arguments"])

    def test_generated_runner_declares_and_writes_a_numeric_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundles" / "simulator"
            runner = bundle / "devs_project" / "run_demo.py"
            runner.parent.mkdir(parents=True)
            (bundle / "run.py").write_text(
                "import runpy\n"
                'SIM_MODULE = "devs_project.run_demo"\n'
                "runpy.run_module(SIM_MODULE, run_name='__main__')\n",
                encoding="utf-8",
            )
            runner.write_text(
                "import json\n"
                "import math\n"
                "import os\n"
                "from pathlib import Path\n"
                "OPTPILOT_RESULT_FILE = 'summary.json'\n"
                "def write_simulation_summary(metrics, simulated_time, metric_note=None):\n"
                "    result_root = os.environ.get('OPTPILOT_SIMULATION_RESULTS_DIR')\n"
                "    if not result_root:\n"
                "        return\n"
                "    checked = {}\n"
                "    for name, value in metrics.items():\n"
                "        if isinstance(value, (bool, int)):\n"
                "            checked[name] = value\n"
                "        elif isinstance(value, float) and math.isfinite(value):\n"
                "            checked[name] = value\n"
                "        else:\n"
                "            raise ValueError('metric is not finite numeric data')\n"
                "    target = Path(result_root) / OPTPILOT_RESULT_FILE\n"
                "    target.parent.mkdir(parents=True, exist_ok=True)\n"
                "    temporary = target.with_suffix('.json.tmp')\n"
                "    temporary.write_text(json.dumps({\n"
                "        'schema_version': 'devs.simulation-result.v1',\n"
                "        'metrics': checked,\n"
                "        'run': {'completed': True, 'simulated_time': simulated_time},\n"
                "    }), encoding='utf-8')\n"
                "    temporary.replace(target)\n"
                "if __name__ == '__main__':\n"
                "    write_simulation_summary(\n"
                "        {'completed_customers': 7, 'utilization': 0.75}, 12.0\n"
                "    )\n",
                encoding="utf-8",
            )

            manifest_path = ensure_simulation_manifest(bundle)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["result_files"], ["summary.json"])

            service = self.make_service(root, maximum_timeout_seconds=30)
            record = service.execute(bundle)
            self.assertEqual(record["status"], "succeeded", record)
            self.assertEqual(
                [item["path"] for item in record["result_files"]],
                ["summary.json"],
            )
            retained = (
                root
                / "executions"
                / record["execution_id"]
                / "results"
                / "summary.json"
            )
            payload = json.loads(retained.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["metrics"],
                {"completed_customers": 7, "utilization": 0.75},
            )

    def test_result_file_is_not_inferred_from_an_incomplete_or_existing_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incomplete = root / "incomplete"
            runner = incomplete / "devs_project" / "run_demo.py"
            runner.parent.mkdir(parents=True)
            (incomplete / "run.py").write_text(
                'SIM_MODULE = "devs_project.run_demo"\n', encoding="utf-8"
            )
            runner.write_text(
                "OPTPILOT_RESULT_FILE = 'summary.json'\n"
                "def write_simulation_summary(metrics, simulated_time):\n"
                "    return\n"
                "write_simulation_summary({}, 1)\n",
                encoding="utf-8",
            )
            incomplete_manifest = json.loads(
                ensure_simulation_manifest(incomplete).read_text(encoding="utf-8")
            )
            self.assertEqual(incomplete_manifest["result_files"], [])

            existing = root / "existing"
            existing_runner = existing / "devs_project" / "run_demo.py"
            existing_runner.parent.mkdir(parents=True)
            (existing / "run.py").write_text(
                'SIM_MODULE = "devs_project.run_demo"\n', encoding="utf-8"
            )
            existing_runner.write_text(
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                "OPTPILOT_RESULT_FILE = 'summary.json'\n"
                "def write_simulation_summary(metrics, simulated_time):\n"
                "    result_root = os.environ.get('OPTPILOT_SIMULATION_RESULTS_DIR')\n"
                "    target = Path(result_root) / OPTPILOT_RESULT_FILE\n"
                "    temporary = target.with_suffix('.json.tmp')\n"
                "    temporary.write_text(json.dumps({'metrics': metrics}))\n"
                "    temporary.replace(target)\n"
                "write_simulation_summary({'count': 1}, 1)\n",
                encoding="utf-8",
            )
            write_manifest(existing, result_files=[])
            ensure_simulation_manifest(existing)
            existing_manifest = json.loads(
                (existing / "simulation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(existing_manifest["result_files"], [])

    def test_generator_prompts_and_reference_keep_the_result_contract(self):
        resource_root = Path(__file__).resolve().parents[2]
        materials = (
            resource_root
            / "devs_tools"
            / "devs_construct_recon"
            / "materials"
            / "devs_project"
            / "runner_example.py"
        )
        source = materials.read_text(encoding="utf-8")
        compile(source, str(materials), "exec")
        require_result_summary_contract(source, filename=str(materials))
        require_event_trace_contract(source, filename=str(materials))
        with self.assertRaisesRegex(ValueError, "summary.json contract"):
            require_result_summary_contract("print('no summary')\n")
        helper_without_run = source.split('if __name__ == "__main__":', 1)[0]
        with self.assertRaisesRegex(ValueError, "summary.json contract"):
            require_result_summary_contract(helper_without_run)
        with self.assertRaisesRegex(ValueError, "event_trace.jsonl contract"):
            require_event_trace_contract(helper_without_run)
        for creator_name in (
            "top_simulation_creator.py",
            "top_simulation_creator_fast.py",
        ):
            prompt_source = (
                resource_root
                / "devs_tools"
                / "devs_construct_recon"
                / "tools"
                / "simulation"
                / creator_name
            ).read_text(encoding="utf-8")
            self.assertIn("### 5. Result Summary (Required)", prompt_source)
            self.assertIn(
                'OPTPILOT_RESULT_FILE = "summary.json"', prompt_source
            )
            self.assertIn(
                "attach_event_trace(sim, model)", prompt_source
            )
            self.assertIn(
                "Do not use reflection, attribute-name guessing", prompt_source
            )

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            runner = bundle / "devs_project" / "run_demo.py"
            runner.parent.mkdir()
            runner.write_text(source, encoding="utf-8")
            (bundle / "run.py").write_text(
                'SIM_MODULE = "devs_project.run_demo"\n', encoding="utf-8"
            )
            manifest = json.loads(
                ensure_simulation_manifest(bundle).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["result_files"],
                ["summary.json", "event_trace.jsonl"],
            )

            # A repair can update the generated runner while an older manifest
            # still advertises only its pre-trace result. The exact generated
            # attachment refreshes standard results without replacing custom ones.
            manifest["result_files"] = ["reports/custom.csv", "summary.json"]
            (bundle / "simulation.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            ensure_simulation_manifest(bundle)
            refreshed = json.loads(
                (bundle / "simulation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                refreshed["result_files"],
                ["reports/custom.csv", "summary.json", "event_trace.jsonl"],
            )

    def test_event_trace_contract_requires_attachment_before_initialize(self):
        resource_root = Path(__file__).resolve().parents[2]
        runner = (
            resource_root
            / "devs_tools"
            / "devs_construct_recon"
            / "materials"
            / "devs_project"
            / "runner_example.py"
        ).read_text(encoding="utf-8")

        missing = runner.replace("    attach_event_trace(sim, model)\n", "")
        with self.assertRaisesRegex(ValueError, "event_trace.jsonl contract"):
            require_event_trace_contract(missing)

        late = missing.replace(
            "    sim.initialize()\n",
            "    sim.initialize()\n    attach_event_trace(sim, model)\n",
        )
        with self.assertRaisesRegex(ValueError, "event_trace.jsonl contract"):
            require_event_trace_contract(late)

        no_exit = runner.replace("    sim.exit()\n", "")
        with self.assertRaisesRegex(ValueError, "event_trace.jsonl contract"):
            require_event_trace_contract(no_exit)

        dormant = (
            "from xdevs.sim import Coordinator\n"
            "from devs_project.devs_utils.event_trace import attach_event_trace\n"
            "def never_called():\n"
            "    sim = Coordinator(model, clock)\n"
            "    attach_event_trace(sim, model)\n"
            "    sim.initialize()\n"
            "    sim.exit()\n"
            "print('runner does not call the helper')\n"
        )
        with self.assertRaisesRegex(ValueError, "event_trace.jsonl contract"):
            require_event_trace_contract(dormant)

        reachable_main = dormant.replace(
            "def never_called():",
            "def main():",
        ).replace(
            "print('runner does not call the helper')",
            "if __name__ == '__main__':\n    main()",
        )
        require_event_trace_contract(reachable_main)

    def test_behavior_smoke_detects_a_deadlocked_multistage_trace_without_rerunning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = write_behavior_bundle(root, "pipeline")
            results = root / "results"
            write_behavior_evidence(results, ["Runtime.source"] * 3)

            with (
                patch(
                    "devs_display.backend.simulation_execution.subprocess.run",
                    side_effect=AssertionError("behavior check must not run a process"),
                ),
                patch(
                    "devs_display.backend.graph_parser.litellm.completion",
                    side_effect=AssertionError("behavior check must not call an LLM"),
                ),
            ):
                assessment = assess_behavior_smoke(bundle, results)

            self.assertEqual(assessment.status, "stalled")
            self.assertEqual(assessment.recorded_events, 3)
            self.assertEqual(assessment.observed_components, ("Runtime.source",))
            self.assertEqual(
                assessment.expected_downstream_components,
                ("Root.worker",),
            )

    def test_behavior_smoke_passes_when_connected_downstream_output_is_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = write_behavior_bundle(root, "pipeline")
            results = root / "results"
            write_behavior_evidence(
                results,
                ["Runtime.source"] * 3 + ["Runtime.worker"],
            )

            assessment = assess_behavior_smoke(bundle, results)

            self.assertEqual(assessment.status, "passed")
            self.assertEqual(assessment.recorded_events, 4)

    def test_behavior_smoke_does_not_reject_single_or_terminal_sink_models(self):
        for topology in ("single", "terminal"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = write_behavior_bundle(root, topology)
                results = root / "results"
                write_behavior_evidence(results, ["Runtime.source"] * 3)

                assessment = assess_behavior_smoke(bundle, results)

                self.assertEqual(assessment.status, "not_applicable")

    def test_behavior_smoke_is_inconclusive_for_lossy_or_dynamic_evidence(self):
        cases = (
            ("pipeline", {"truncated": True}),
            ("pipeline", {"malformed": True}),
            ("dynamic", {}),
        )
        for topology, evidence_options in cases:
            with self.subTest(
                topology=topology,
                evidence_options=evidence_options,
            ), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = write_behavior_bundle(root, topology)
                results = root / "results"
                write_behavior_evidence(
                    results,
                    ["Runtime.source"] * 3,
                    **evidence_options,
                )

                assessment = assess_behavior_smoke(bundle, results)

                self.assertEqual(assessment.status, "inconclusive")

    def test_result_contract_rejects_xdevs_port_singular_value(self):
        resource_root = Path(__file__).resolve().parents[2]
        runner = (
            resource_root
            / "devs_tools"
            / "devs_construct_recon"
            / "materials"
            / "devs_project"
            / "runner_example.py"
        ).read_text(encoding="utf-8")
        invalid_runner = (
            runner
            + "\nBROKEN_METRIC = model.output['completed'].value\n"
        )

        with self.assertRaisesRegex(ValueError, "invalid xDEVS Port.value API"):
            require_result_summary_contract(invalid_runner)

    def test_result_contract_allows_unrelated_value_attribute(self):
        resource_root = Path(__file__).resolve().parents[2]
        runner = (
            resource_root
            / "devs_tools"
            / "devs_construct_recon"
            / "materials"
            / "devs_project"
            / "runner_example.py"
        ).read_text(encoding="utf-8")

        require_result_summary_contract(
            runner + "\nORDINARY_VALUE = simulation_report.value\n"
        )

    def test_fast_runner_generator_fails_closed_after_three_invalid_generations(self):
        """Exercise retries without importing optional agent dependencies or calling an LLM."""

        module_name = (
            "devs_tools.devs_construct_recon.tools.simulation."
            "top_simulation_creator_fast"
        )
        devs_execute_name = (
            "devs_tools.devs_construct_recon.tools.simulation.devs_execute"
        )

        class FakeTool:
            def __init__(self, *args, **kwargs):
                pass

        fake_smolagents = types.ModuleType("smolagents")
        fake_smolagents.Tool = FakeTool
        fake_smolagents.CodeAgent = object
        fake_smolagents.LiteLLMModel = object

        fake_litellm = types.ModuleType("litellm")
        fake_litellm.completion = lambda *args, **kwargs: None
        fake_litellm.drop_params = False

        fake_devs_execute = types.ModuleType(devs_execute_name)
        fake_devs_execute.DEVSExecute = object

        previous_creator = sys.modules.pop(module_name, None)
        try:
            with patch.dict(
                sys.modules,
                {
                    "smolagents": fake_smolagents,
                    "litellm": fake_litellm,
                    devs_execute_name: fake_devs_execute,
                },
            ):
                creator_module = importlib.import_module(module_name)
                invalid_response = (
                    "<python_code>\n"
                    "metric = model.output['completed'].value\n"
                    "</python_code>"
                )
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    system_info = root / "system_model_info.json"
                    system_info.write_text("{}\n", encoding="utf-8")
                    creator = creator_module.TopSimulationCreatorFast(
                        read_file_tool=FakeTool(),
                        working_directory=str(root),
                    )
                    with patch.object(
                        creator_module,
                        "completion",
                        return_value=object(),
                    ) as completion_mock, patch.object(
                        creator_module,
                        "get_content_strict",
                        return_value=invalid_response,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "invalid xDEVS Port.value API",
                        ):
                            creator.forward(
                                model_file_path="devs_project/Demo.py",
                                model_class_name="Demo",
                                model_spec="Demo model",
                                system_info_file_path=str(system_info),
                                simulation_scenario="Run for one second",
                                save_path="devs_project/run_demo.py",
                                stdout_save_path="stdout.txt",
                                stderr_save_path="stderr.txt",
                            )

                    self.assertEqual(completion_mock.call_count, 3)
                    self.assertFalse(
                        (root / "devs_project" / "run_demo.py").exists()
                    )
        finally:
            sys.modules.pop(module_name, None)
            if previous_creator is not None:
                sys.modules[module_name] = previous_creator

    def test_existing_manifest_gains_one_locked_portable_xdevs_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "run.py").write_text("pass\n", encoding="utf-8")
            write_manifest(bundle)
            ensure_simulation_manifest(bundle)
            first = (bundle / "simulation.json").read_bytes()
            manifest = json.loads(first)
            self.assertEqual(
                manifest["python_runtime"],
                {"requirements_lock": XDEVS_REQUIREMENTS_LOCK},
            )
            wheel = bundle / XDEVS_WHEEL_PATH
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual(
                (bundle / XDEVS_REQUIREMENTS_LOCK).read_text(encoding="utf-8"),
                f"vendor/{XDEVS_WHEEL_NAME} --hash=sha256:{digest}\n",
            )
            self.assertIn(
                "GNU GENERAL PUBLIC LICENSE",
                (bundle / XDEVS_LICENSE_PATH).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "GNU General Public License, version 3",
                (bundle / XDEVS_NOTICE_PATH).read_text(encoding="utf-8"),
            )
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                self.assertIn("xdevs/models.py", names)
                self.assertIn("xdevs/sim.py", names)
                self.assertIn("xdevs-3.0.0.dist-info/LICENSE.txt", names)
                self.assertIn(
                    "xdevs-3.0.0.dist-info/THIRD_PARTY_NOTICES.md", names
                )
                self.assertIn("xdevs-3.0.0.dist-info/RECORD", names)
                wheel_metadata = archive.read(
                    "xdevs-3.0.0.dist-info/WHEEL"
                ).decode("utf-8")
                self.assertIn("Root-Is-Purelib: true", wheel_metadata)
                self.assertIn("Tag: py3-none-any", wheel_metadata)
                installed = bundle / "installed-wheel"
                archive.extractall(installed)
            imported = subprocess.run(
                [
                    sys.executable,
                    "-S",
                    "-c",
                    "from xdevs.models import Coupled; print(Coupled.__name__)",
                ],
                env={
                    "PATH": str(Path(sys.executable).parent),
                    "PYTHONPATH": str(installed),
                    "PYTHONNOUSERSITE": "1",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(imported.stdout.strip(), "Coupled")

            ensure_simulation_manifest(bundle)
            self.assertEqual((bundle / "simulation.json").read_bytes(), first)

            (bundle / "simulation.json").write_text(
                '{"schema_version":"wrong","entrypoint":"run.py"}',
                encoding="utf-8",
            )
            with self.assertRaises(SimulationManifestError):
                ensure_simulation_manifest(bundle)

    def test_tampered_portable_dependency_lock_is_repaired_before_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "run.py").write_text("pass\n", encoding="utf-8")
            write_manifest(bundle)
            ensure_simulation_manifest(bundle)
            (bundle / XDEVS_REQUIREMENTS_LOCK).write_text(
                f"vendor/{XDEVS_WHEEL_NAME} --hash=sha256:{'0' * 64}\n",
                encoding="utf-8",
            )

            ensure_simulation_manifest(bundle)

            wheel = bundle / XDEVS_WHEEL_PATH
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertIn(
                digest,
                (bundle / XDEVS_REQUIREMENTS_LOCK).read_text(encoding="utf-8"),
            )

    def test_typed_arguments_snapshot_and_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(
                bundles,
                "import argparse, json, os\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--seed', type=int, required=True)\n"
                "parser.add_argument('--rate', type=float, default=1.5)\n"
                "args = parser.parse_args()\n"
                "result_root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
                "result_root.joinpath('summary.json').write_text(json.dumps({\n"
                "  'seed': args.seed, 'rate': args.rate,\n"
                "  'secret': os.environ.get('OPENROUTER_API_KEY'),\n"
                "  'proxy': os.environ.get('HTTPS_PROXY'),\n"
                "  'marker': 'before',\n"
                "}))\n"
                "print('simulation complete')\n",
                arguments=[
                    {"name": "seed", "type": "integer", "required": True, "minimum": 0},
                    {"name": "rate", "type": "number", "default": 1.5, "minimum": 0, "maximum": 2},
                ],
                result_files=["summary.json"],
            )
            service = self.make_service(root)
            old_secret = os.environ.get("OPENROUTER_API_KEY")
            old_proxy = os.environ.get("HTTPS_PROXY")
            os.environ["OPENROUTER_API_KEY"] = "must-not-leak"
            os.environ["HTTPS_PROXY"] = "http://must-not-leak"
            try:
                queued = service.prepare(bundle, {"seed": 7})
                # The source can change after prepare; run must use the exact snapshot.
                changed = (bundle / "run.py").read_text(encoding="utf-8").replace("before", "after")
                (bundle / "run.py").write_text(changed, encoding="utf-8")
                record = service.run(queued["execution_id"])
            finally:
                if old_secret is None:
                    os.environ.pop("OPENROUTER_API_KEY", None)
                else:
                    os.environ["OPENROUTER_API_KEY"] = old_secret
                if old_proxy is None:
                    os.environ.pop("HTTPS_PROXY", None)
                else:
                    os.environ["HTTPS_PROXY"] = old_proxy

            self.assertEqual(record["status"], "succeeded")
            self.assertEqual(record["arguments"], {"seed": 7, "rate": 1.5})
            self.assertEqual(record["stdout"], "simulation complete\n")
            self.assertRegex(record["snapshot_digest"], r"^[a-f0-9]{64}$")
            self.assertEqual(record["bundle_digest"], record["snapshot_digest"])
            self.assertEqual(len(record["result_files"]), 1)
            result_path = root / "executions" / record["execution_id"] / "results" / "summary.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result, {"seed": 7, "rate": 1.5, "secret": None, "proxy": None, "marker": "before"})
            persisted = json.loads(
                (root / "executions" / record["execution_id"] / "execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "succeeded")

    def test_invalid_argument_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(
                bundles,
                "raise RuntimeError('must not execute')\n",
                arguments=[
                    {"name": "count", "type": "integer", "required": True, "minimum": 1, "maximum": 5}
                ],
            )
            service = self.make_service(root)
            with self.assertRaises(SimulationManifestError):
                service.prepare(bundle, {"count": "2"})
            with self.assertRaises(SimulationManifestError):
                service.prepare(bundle, {"count": 8})
            with self.assertRaises(SimulationManifestError):
                service.prepare(bundle, {"count": 2, "shell": "; rm -rf /"})

    def test_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            marker = root / "child-survived"
            bundle = write_bundle(
                bundles,
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(1); open({str(marker)!r}, 'w').write('bad')\"])\n"
                "time.sleep(20)\n",
                timeout=1,
            )
            service = self.make_service(root)
            record = service.execute(bundle)
            self.assertEqual(record["status"], "timed_out")
            self.assertEqual(record["failure_kind"], "timeout")
            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_stop_terminates_a_running_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(bundles, "import time\nprint('started', flush=True)\ntime.sleep(20)\n")
            service = self.make_service(root)
            queued = service.prepare(bundle)
            result = {}

            def run():
                result.update(service.run(queued["execution_id"]))

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 3
            while service.get_record(queued["execution_id"])["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertTrue(service.stop(queued["execution_id"]))
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result["status"], "stopped")
            self.assertTrue(result["stop_requested"])

    def test_successful_runner_cannot_leave_an_ordinary_child_process_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            marker = root / "child-survived"
            bundle = write_bundle(
                bundles,
                "import subprocess, sys\n"
                f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(0.3); open({str(marker)!r}, 'w').write('bad')\"])\n"
                "print('parent complete')\n",
            )
            service = self.make_service(root)

            record = service.execute(bundle)

            self.assertEqual(record["status"], "succeeded")
            time.sleep(0.6)
            self.assertFalse(marker.exists())

    def test_completed_execution_storage_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(bundles, "print('ok')\n")
            service = self.make_service(root, max_retained_executions=2)

            first = service.execute(bundle)
            second = service.execute(bundle)
            third = service.execute(bundle)

            self.assertFalse(
                (root / "executions" / first["execution_id"]).exists()
            )
            self.assertTrue(
                (root / "executions" / second["execution_id"]).is_dir()
            )
            self.assertTrue(
                (root / "executions" / third["execution_id"]).is_dir()
            )
            with self.assertRaises(ExecutionStateError):
                service.get_record(first["execution_id"])

    def test_output_and_result_limits_fail_boundedly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            noisy = write_bundle(bundles, "print('x' * 10000)\n")
            service = self.make_service(root, max_stdout_bytes=128)
            output_record = service.execute(noisy)
            self.assertEqual(output_record["status"], "failed")
            self.assertEqual(output_record["failure_kind"], "output_limit")
            self.assertTrue(output_record["stdout_truncated"])
            self.assertLessEqual(len(output_record["stdout"].encode("utf-8")), 128)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            result_bundle = write_bundle(
                bundles,
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'], 'large.bin').write_bytes(b'x' * 4096)\n",
            )
            service = self.make_service(root, max_result_file_bytes=128, max_result_bytes=256)
            result_record = service.execute(result_bundle)
            self.assertEqual(result_record["status"], "failed")
            self.assertEqual(result_record["failure_kind"], "result_limit")

    def test_concurrency_limit_keeps_second_execution_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            first_bundle = write_bundle(bundles, "import time\ntime.sleep(0.5)\n")
            first_bundle.rename(bundles / "first")
            second_bundle = write_bundle(bundles, "print('second')\n")
            service = self.make_service(root, max_concurrency=1)
            first = service.prepare(bundles / "first")
            second = service.prepare(second_bundle)
            first_result = {}
            second_result = {}
            first_thread = threading.Thread(
                target=lambda: first_result.update(service.run(first["execution_id"]))
            )
            second_thread = threading.Thread(
                target=lambda: second_result.update(service.run(second["execution_id"]))
            )
            first_thread.start()
            deadline = time.monotonic() + 2
            while service.get_record(first["execution_id"])["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            second_thread.start()
            time.sleep(0.1)
            self.assertEqual(service.get_record(second["execution_id"])["status"], "queued")
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)
            self.assertEqual(first_result["status"], "succeeded")
            self.assertEqual(second_result["status"], "succeeded")

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are required")
    def test_symlinked_bundle_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundles = root / "bundles"
            bundles.mkdir()
            bundle = write_bundle(bundles, "print('ok')\n")
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            os.symlink(outside, bundle / "linked.txt")
            service = self.make_service(root)
            with self.assertRaises(SimulationBundleError):
                service.prepare(bundle)


if __name__ == "__main__":
    unittest.main()
