import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from devs_display.backend.routes import create_app
from devs_display.backend.server import DEVSBackendService


class _DummyAgent:
    def run(self, prompt, reset=False):
        return "unused"


class SimulationResultFileTests(unittest.TestCase):
    def setUp(self):
        self._trusted_execution = patch.dict(
            os.environ,
            {
                "DEVS_GENERATED_EXECUTION_MODE": "process",
                "DEVS_GENERATED_EXECUTION_TRUSTED_LOCAL": "1",
            },
        )
        self._trusted_execution.start()
        self.addCleanup(self._trusted_execution.stop)

    def _completed_run(self, root: str):
        service = DEVSBackendService(_DummyAgent(), root, start_worker=False)
        session_id = service.list_sessions()[0]["session_id"]
        project = service.upload_project(
            session_id,
            "result viewer demo",
            {
                "run.py": (
                    "import os\n"
                    "from pathlib import Path\n"
                    "root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
                    "(root / 'report.json').write_text('{\\\"score\\\":7}', encoding='utf-8')\n"
                    "(root / 'notes.txt').write_text('student result\\n', encoding='utf-8')\n"
                    "(root / 'chart.bin').write_bytes(b'\\x00\\x01\\x02')\n"
                ),
                "README.md": "# Result viewer demo\n",
                "system_model_info.json": "{}",
                "simulation.json": json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 5,
                        "arguments": [],
                        "result_files": [
                            "report.json",
                            "notes.txt",
                            "chart.bin",
                        ],
                    }
                ),
            },
        )
        with service.lock:
            queued = service._prepare_simulation_execution_unlocked(
                session_id,
                project["project_id"],
                {},
                purpose="inspection",
            )
        execution_id = queued["execution_id"]
        record = service.simulation_execution_service.run(execution_id)
        self.assertEqual(record["status"], "succeeded", record)
        return service, session_id, project, execution_id

    def test_preview_and_download_use_the_retained_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id, project, execution_id = self._completed_run(tmp)

            preview = service.get_simulation_result_file(
                session_id,
                project["project_id"],
                execution_id,
                "report.json",
            )
            self.assertEqual(preview["content"], '{"score":7}')
            self.assertEqual(preview["media_type"], "application/json")
            self.assertTrue(preview["previewable"])

            with self.assertRaises(TypeError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "chart.bin",
                )
            downloaded = service.get_simulation_result_file(
                session_id,
                project["project_id"],
                execution_id,
                "chart.bin",
                download=True,
            )
            self.assertEqual(downloaded["content"], b"\x00\x01\x02")

            public = service.get_simulation_run(
                session_id, project["project_id"], execution_id
            )
            described = {item["path"]: item for item in public["result_files"]}
            self.assertTrue(described["report.json"]["previewable"])
            self.assertFalse(described["chart.bin"]["previewable"])
            self.assertTrue(described["chart.bin"]["downloadable"])

            result_root = (
                service.simulation_execution_service.execution_root
                / execution_id
                / "results"
            )
            (result_root / "not-recorded.txt").write_text(
                "not in the run record", encoding="utf-8"
            )
            with self.assertRaises(KeyError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "not-recorded.txt",
                )
            with self.assertRaises(KeyError):
                service.get_simulation_result_file(
                    session_id,
                    "another-project",
                    execution_id,
                    "report.json",
                )
            with self.assertRaises(KeyError):
                service.get_simulation_result_file(
                    "another-session",
                    project["project_id"],
                    execution_id,
                    "report.json",
                )
            with self.assertRaises(ValueError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "../report.json",
                )

    def test_changed_symlink_and_special_results_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id, project, execution_id = self._completed_run(tmp)
            result_root = (
                service.simulation_execution_service.execution_root
                / execution_id
                / "results"
            )

            (result_root / "report.json").write_text('{"score":8}', encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "report.json",
                )

            chart = result_root / "chart.bin"
            chart.unlink()
            try:
                chart.symlink_to(result_root / "notes.txt")
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")
            with self.assertRaises(FileNotFoundError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "chart.bin",
                    download=True,
                )

            notes = result_root / "notes.txt"
            notes.unlink()
            try:
                os.mkfifo(notes)
            except (AttributeError, NotImplementedError, OSError) as exc:
                self.skipTest(f"special files are unavailable: {exc}")
            with self.assertRaises(FileNotFoundError):
                service.get_simulation_result_file(
                    session_id,
                    project["project_id"],
                    execution_id,
                    "notes.txt",
                )

    def test_preview_and_download_have_independent_byte_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id, project, execution_id = self._completed_run(tmp)
            arguments = (
                session_id,
                project["project_id"],
                execution_id,
                "notes.txt",
            )
            with patch(
                "devs_display.backend.server.MAX_SIMULATION_RESULT_PREVIEW_BYTES",
                4,
            ):
                with self.assertRaises(OverflowError):
                    service.get_simulation_result_file(*arguments)
            with patch(
                "devs_display.backend.server.MAX_SIMULATION_RESULT_DOWNLOAD_BYTES",
                4,
            ):
                with self.assertRaises(OverflowError):
                    service.get_simulation_result_file(*arguments, download=True)

    def test_http_api_previews_text_and_downloads_binary_as_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id, project, execution_id = self._completed_run(tmp)
            base = (
                f"/sessions/{session_id}/projects/{project['project_id']}"
                f"/simulation-runs/{execution_id}/result-files"
            )
            with patch.dict(os.environ, {"DEVS_DISPLAY_PASSWORD": ""}):
                with TestClient(create_app(service)) as client:
                    preview = client.get(f"{base}/report.json")
                    self.assertEqual(preview.status_code, 200, preview.text)
                    self.assertEqual(preview.json()["content"], '{"score":7}')
                    self.assertEqual(preview.headers.get("cache-control"), "no-store")

                    binary_preview = client.get(f"{base}/chart.bin")
                    self.assertEqual(binary_preview.status_code, 415)

                    download = client.get(f"{base}/chart.bin?download=true")
                    self.assertEqual(download.status_code, 200, download.text)
                    self.assertEqual(download.content, b"\x00\x01\x02")
                    self.assertIn(
                        "attachment",
                        download.headers.get("content-disposition", ""),
                    )
                    self.assertEqual(
                        download.headers.get("x-content-type-options"), "nosniff"
                    )


if __name__ == "__main__":
    unittest.main()
