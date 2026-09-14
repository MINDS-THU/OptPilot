import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from default_tools.generated_execution import (
    ExecutionBoundaryError,
    GeneratedExecutionBoundary,
)
from default_tools.interface_output_action import OutputActionResult


class GeneratedExecutionBoundaryTests(unittest.TestCase):
    def test_agent_execute_passes_exact_generated_bundle_to_managed_action(self):
        class FakeOutputAction:
            def __init__(self):
                self.calls = []

            def execute(self, **kwargs):
                self.calls.append(kwargs)
                return OutputActionResult(
                    request_id="devs_exact_bundle_test",
                    action_id="run-simulation",
                    snapshot_ref="snapshot:exact-bundle-test",
                    status="succeeded",
                    exit_code=0,
                    duration_seconds=0.1,
                    stdout='{"type":"RESULT","status":"ok"}\n',
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    result_files=(),
                    failure_code=None,
                )

        fake_smolagents = types.ModuleType("smolagents")

        class FakeTool:
            def __init__(self):
                pass

        fake_smolagents.Tool = FakeTool
        module_name = (
            "devs_tools.devs_construct_recon.tools.simulation.devs_execute"
        )
        with patch.dict(sys.modules, {"smolagents": fake_smolagents}):
            module = importlib.import_module(module_name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "supply_chain_sim"
            model_package = project / "devs_project"
            model_package.mkdir(parents=True)
            (project / "run.py").write_text(
                "from devs_project.model import RESULT\nprint(RESULT)\n",
                encoding="utf-8",
            )
            (model_package / "__init__.py").write_text("", encoding="utf-8")
            (model_package / "model.py").write_text(
                "RESULT = 'ok'\n",
                encoding="utf-8",
            )
            broker = FakeOutputAction()
            tool = module.DEVSExecute(
                str(root),
                execution_mode="process",
                allow_trusted_process=True,
                output_action_executor=broker,
            )

            response = tool.forward(
                "supply_chain_sim",
                main_file="run.py",
                command_args='--seed 7 --label "supply chain"',
                stdin_content="",
            )

            self.assertIn("STATUS: SUCCESS", response)
            self.assertEqual(len(broker.calls), 1)
            self.assertEqual(
                Path(broker.calls[0]["source_directory"]),
                project.resolve(),
            )
            self.assertEqual(
                broker.calls[0]["arguments"],
                ["--seed", "7", "--label", "supply chain"],
            )
            self.assertTrue((project / "run.py").is_file())
            self.assertTrue((project / "devs_project" / "model.py").is_file())
            self.assertFalse((project / "devs_project" / "devs_project").exists())

    def test_agent_execute_prefers_managed_output_action(self):
        class FakeOutputAction:
            def __init__(self):
                self.calls = []

            def execute(self, **kwargs):
                self.calls.append(kwargs)
                return OutputActionResult(
                    request_id="devs_test",
                    action_id="run-simulation",
                    snapshot_ref="snapshot:test",
                    status="succeeded",
                    exit_code=0,
                    duration_seconds=0.2,
                    stdout='{"type":"RESULT","count":3}\n',
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    result_files=(),
                    failure_code=None,
                )

        fake_smolagents = types.ModuleType("smolagents")

        class FakeTool:
            def __init__(self):
                pass

        fake_smolagents.Tool = FakeTool
        module_name = (
            "devs_tools.devs_construct_recon.tools.simulation.devs_execute"
        )
        with patch.dict(sys.modules, {"smolagents": fake_smolagents}):
            module = importlib.import_module(module_name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "raise RuntimeError('must run through broker')\n",
                encoding="utf-8",
            )
            broker = FakeOutputAction()
            tool = module.DEVSExecute(
                str(root),
                execution_mode="process",
                allow_trusted_process=True,
                output_action_executor=broker,
            )
            response = tool.forward(
                "simulator",
                main_file="main.py",
                command_args="--seed 7",
            )

        self.assertIn("STATUS: SUCCESS", response)
        self.assertIn('"count": 3', response)
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(broker.calls[0]["arguments"], ["--seed", "7"])
        self.assertEqual(broker.calls[0]["timeout_seconds"], 30)
        self.assertEqual(
            Path(broker.calls[0]["source_directory"], "run.py").name,
            "run.py",
        )

    def test_container_command_has_the_complete_isolation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            results = root / "results"
            workspace.mkdir()
            results.mkdir()
            (workspace / "run.py").write_text("print('ok')\n", encoding="utf-8")
            with patch(
                "default_tools.generated_execution.shutil.which",
                return_value="/usr/local/bin/docker",
            ), patch.dict(
                os.environ,
                {
                    "OPENROUTER_API_KEY": "must-not-cross",
                    "OPTPILOT_INTERFACE_OUTPUTS_TOKEN": "must-not-cross-either",
                    "HOME": str(root / "trusted-client-home"),
                    "DOCKER_CONTEXT": "desktop-linux",
                },
            ):
                boundary = GeneratedExecutionBoundary(mode="container")
                launch = boundary.build_python_launch(
                    workspace,
                    ("-u", "run.py", "--seed", "7"),
                    results_directory=results,
                )

            command = list(launch.argv)
            joined = "\n".join(command)
            self.assertEqual(launch.mode, "container")
            self.assertIn("--network", command)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertEqual(
                command[command.index("--security-opt") + 1],
                "no-new-privileges:true",
            )
            self.assertIn("--pids-limit", command)
            self.assertIn("--memory", command)
            self.assertIn("--memory-swap", command)
            self.assertIn("--cpus", command)
            self.assertIn("fsize=33554432:33554432", command)
            self.assertIn("--user", command)
            self.assertIn("--tmpfs", command)
            self.assertIn(f"{workspace.resolve()}:/workspace:ro", command)
            self.assertIn(f"{results.resolve()}:/results:rw", command)
            self.assertIn("--entrypoint", command)
            self.assertEqual(command[command.index("--entrypoint") + 1], "python")
            self.assertIn("--pull", command)
            self.assertEqual(command[command.index("--pull") + 1], "never")
            self.assertNotIn("OPENROUTER_API_KEY", joined)
            self.assertNotIn("must-not-cross", joined)
            self.assertNotIn("OPTPILOT_INTERFACE_OUTPUTS_TOKEN", joined)
            self.assertNotIn("OPENROUTER_API_KEY", launch.environment)
            self.assertEqual(launch.environment["DOCKER_CONTEXT"], "desktop-linux")
            self.assertEqual(
                launch.environment["HOME"], str(root / "trusted-client-home")
            )

    def test_missing_container_engine_fails_closed_without_process_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "default_tools.generated_execution.shutil.which", return_value=None
        ):
            workspace = Path(tmp)
            (workspace / "run.py").write_text("print('never')\n", encoding="utf-8")
            boundary = GeneratedExecutionBoundary(mode="container")
            with self.assertRaisesRegex(
                ExecutionBoundaryError, "requires Docker or Podman"
            ):
                boundary.build_python_launch(workspace, ("-u", "run.py"))

    def test_process_mode_requires_explicit_trusted_local_opt_in(self):
        with self.assertRaisesRegex(ExecutionBoundaryError, "Process execution is disabled"):
            GeneratedExecutionBoundary(mode="process")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "not-in-child"},
        ):
            workspace = Path(tmp)
            launch = GeneratedExecutionBoundary(
                mode="process",
                allow_trusted_process=True,
                python_executable=sys.executable,
            ).build_python_launch(workspace, ("-u", "run.py"))
            self.assertEqual(launch.mode, "process")
            self.assertEqual(launch.argv[0], str(Path(sys.executable).resolve()))
            self.assertNotIn("OPENROUTER_API_KEY", launch.environment)

    def test_symlink_workspace_and_result_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            results = root / "results"
            workspace.mkdir()
            results.mkdir()
            workspace_link = root / "workspace-link"
            results_link = root / "results-link"
            try:
                workspace_link.symlink_to(workspace, target_is_directory=True)
                results_link.symlink_to(results, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable")
            boundary = GeneratedExecutionBoundary(
                mode="process",
                allow_trusted_process=True,
            )
            with self.assertRaisesRegex(ExecutionBoundaryError, "regular directory"):
                boundary.build_python_launch(workspace_link, ("-u", "run.py"))
            with self.assertRaisesRegex(ExecutionBoundaryError, "regular directory"):
                boundary.build_python_launch(
                    workspace,
                    ("-u", "run.py"),
                    results_directory=results_link,
                )

    @unittest.skipUnless(
        os.environ.get("DEVS_RUN_CONTAINER_SMOKE") == "1",
        "set DEVS_RUN_CONTAINER_SMOKE=1 to exercise the local agent container",
    )
    def test_live_agent_execute_uses_the_same_container_boundary(self):
        from devs_tools.devs_construct_recon.tools.simulation.devs_execute import (
            DEVSExecute,
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "must-not-cross-agent-boundary"},
        ):
            root = Path(tmp)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "import json, os, socket\n"
                "from pathlib import Path\n"
                "import xdevs\n"
                "def denied(action):\n"
                "    try:\n"
                "        action()\n"
                "        return False\n"
                "    except OSError:\n"
                "        return True\n"
                "payload = {\n"
                "  'type': 'RESULT',\n"
                "  'credential': os.getenv('OPENROUTER_API_KEY', 'missing'),\n"
                "  'network_blocked': denied(lambda: socket.create_connection(('203.0.113.1', 9), timeout=0.2)),\n"
                "  'source_read_only': denied(lambda: Path('forbidden').write_text('x')),\n"
                "  'xdevs_imported': bool(xdevs),\n"
                "}\n"
                "print(json.dumps(payload, sort_keys=True))\n",
                encoding="utf-8",
            )
            response = DEVSExecute(
                str(root),
                execution_mode="container",
                container_engine="docker",
                container_image="optpilot/workspace-dev:latest",
            ).forward("simulator", main_file="main.py")
            self.assertIn("STATUS: SUCCESS", response)
            self.assertIn('"credential": "missing"', response)
            self.assertIn('"network_blocked": true', response)
            self.assertIn('"source_read_only": true', response)
            self.assertIn('"xdevs_imported": true', response)

    @unittest.skipUnless(
        os.environ.get("DEVS_RUN_CONTAINER_SMOKE") == "1",
        "set DEVS_RUN_CONTAINER_SMOKE=1 to exercise the validator container",
    )
    def test_live_generated_validator_uses_the_same_container_boundary(self):
        from devs_tools.devs_construct_recon.tools.simulation.verifier_execute import (
            PythonScriptExecutor,
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "must-not-cross-validator-boundary"},
        ):
            root = Path(tmp)
            (root / "validator.py").write_text(
                "import json, os, socket, sys\n"
                "from pathlib import Path\n"
                "def denied(action):\n"
                "    try:\n"
                "        action()\n"
                "        return False\n"
                "    except OSError:\n"
                "        return True\n"
                "print(json.dumps({\n"
                "  'credential': os.getenv('OPENROUTER_API_KEY', 'missing'),\n"
                "  'input': sys.stdin.read(),\n"
                "  'network_blocked': denied(lambda: socket.create_connection(('203.0.113.1', 9), timeout=0.2)),\n"
                "  'source_read_only': denied(lambda: Path('forbidden').write_text('x')),\n"
                "}, sort_keys=True))\n",
                encoding="utf-8",
            )
            result = PythonScriptExecutor(
                str(root),
                execution_mode="container",
                container_engine="docker",
                container_image="optpilot/workspace-dev:latest",
            ).execute("validator.py", [], stdin_content="student-log")
            self.assertEqual(result.return_code, 0, result.error_message)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["credential"], "missing")
            self.assertEqual(payload["input"], "student-log")
            self.assertTrue(payload["network_blocked"])
            self.assertTrue(payload["source_read_only"])


if __name__ == "__main__":
    unittest.main()
