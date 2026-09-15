import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devs_display.backend.pi_finalizer_run import run_request


class PiFinalizerRunTests(unittest.TestCase):
    def test_run_request_uses_normal_service_and_keeps_raw_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            project = workspace / "generated"
            project.mkdir(parents=True)
            run_root = root / "runs"
            calls = []

            class FakeExecutionService:
                def __init__(self, storage_root, python_executable, **kwargs):
                    self.storage_root = Path(storage_root)
                    self.storage_root.mkdir(parents=True, exist_ok=True)
                    calls.append((Path(python_executable), kwargs))

                def execute(self, bundle, arguments, *, purpose):
                    calls.append((Path(bundle), arguments, purpose))
                    execution_id = "exec_" + "b" * 32
                    results = self.storage_root / execution_id / "results"
                    results.mkdir(parents=True)
                    (results / "summary.json").write_text("{}\n", encoding="utf-8")
                    return {
                        "execution_id": execution_id,
                        "status": "succeeded",
                        "exit_code": 0,
                        "duration_seconds": 0.5,
                        "stdout": "simulation succeeded\n",
                        "stderr": "one warning\n",
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "result_files": [{"path": "summary.json"}],
                        "failure_kind": None,
                        "message": None,
                    }

            with patch(
                "devs_display.backend.pi_finalizer_run.SimulationExecutionService",
                FakeExecutionService,
            ):
                result = run_request(
                    {"project_path": "generated", "parameters": {"seed": 7}},
                    {
                        "PI_DEVS_WORKSPACE_ROOT": str(workspace),
                        "PI_DEVS_PROJECT_PATH": "generated",
                        "PI_DEVS_RUN_ROOT": str(run_root),
                    },
                )

            self.assertEqual(
                calls[1],
                (project.resolve(), {"seed": 7}, "finalizer"),
            )
            self.assertEqual(
                calls[0][1]["allowed_bundle_root"], workspace.resolve()
            )
            self.assertEqual(
                Path(result["stdout"]).read_text(encoding="utf-8"),
                "simulation succeeded\n",
            )
            self.assertEqual(
                Path(result["stderr"]).read_text(encoding="utf-8"),
                "one warning\n",
            )
            self.assertEqual(
                result["result_files"],
                [
                    str(
                        (
                            run_root
                            / ("exec_" + "b" * 32)
                            / "results"
                            / "summary.json"
                        ).resolve()
                    )
                ],
            )

    def test_run_request_rejects_project_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / "generated").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                run_request(
                    {"project_path": "generated", "parameters": {}},
                    {
                        "PI_DEVS_WORKSPACE_ROOT": str(workspace),
                        "PI_DEVS_PROJECT_PATH": "generated",
                        "PI_DEVS_RUN_ROOT": str(root / "runs"),
                    },
                )

    def test_run_request_rejects_noncanonical_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            (workspace / "generated").mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "canonical relative path"):
                run_request(
                    {"project_path": "generated/", "parameters": {}},
                    {
                        "PI_DEVS_WORKSPACE_ROOT": str(workspace),
                        "PI_DEVS_PROJECT_PATH": "generated/",
                        "PI_DEVS_RUN_ROOT": str(root / "runs"),
                    },
                )

    def test_run_request_requires_configured_workspace(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            run_request(
                {"project_path": "generated", "parameters": {}},
                {
                    "PI_DEVS_PROJECT_PATH": "generated",
                    "PI_DEVS_RUN_ROOT": "/tmp/pi-runs",
                },
            )

    def test_run_request_rejects_another_workspace_project(self):
        with self.assertRaisesRegex(ValueError, "assigned simulation"):
            run_request(
                {"project_path": "another", "parameters": {}},
                {
                    "PI_DEVS_PROJECT_PATH": "generated",
                    "PI_DEVS_WORKSPACE_ROOT": "/unused",
                    "PI_DEVS_RUN_ROOT": "/unused",
                },
            )


if __name__ == "__main__":
    unittest.main()
