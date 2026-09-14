from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from devs_display.backend.remote_finalizer import RemoteCodexFinalizerClient
from devs_display.backend.server import DEVSBackendService
from headless_generate import (
    _HeadlessProgress,
    _report_headless_collection,
    _run_automatic_check,
)


class HeadlessAutomaticCheckTests(unittest.TestCase):
    def test_disabled_check_does_not_require_host_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ, {"DEVS_HEADLESS_CODEX_FINALIZER": "0"}, clear=False
        ):
            self.assertIsNone(
                _run_automatic_check(Path(tmp_dir), "generated_simulator")
            )

    def test_enabled_check_uses_and_reads_the_shared_finalizer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle = Path(tmp_dir)
            result_dir = bundle / "devs_project" / "_analysis_logs"
            result_dir.mkdir(parents=True)

            def fake_run(_client, **kwargs):
                result_dir.joinpath("codex_finalizer_result.json").write_text(
                    json.dumps(
                        {
                            "review_id": kwargs["review_id"],
                            "verdict": "pass",
                            "fixed": False,
                            "confidence": "high",
                            "summary": "Checked",
                            "likely_source": "unknown",
                            "issues": [],
                            "files_modified": [],
                            "evidence": ["The bundle ran."],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"returncode": 0, "result_present": True}

            with patch.dict(
                os.environ,
                {
                    "DEVS_HEADLESS_CODEX_FINALIZER": "1",
                    "DEVS_HEADLESS_CODEX_FINALIZER_URL": "http://127.0.0.1/finalize",
                    "DEVS_COLLECTOR_INGEST_TOKEN": "test-token",
                },
                clear=False,
            ), patch.object(RemoteCodexFinalizerClient, "run", fake_run):
                result = _run_automatic_check(bundle, "generated_simulator")

            self.assertEqual(result["verdict"], "pass")
            self.assertFalse(result["fixed"])

    def test_progress_is_written_without_private_tool_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {
                "OPTPILOT_RESOURCE_ACTION_PROGRESS_FILE": str(
                    Path(tmp_dir) / "progress.jsonl"
                )
            },
            clear=False,
        ):
            progress = _HeadlessProgress()
            progress(
                {
                    "activity_key": "plan_structure",
                    "activity_state": "running",
                    "title": "Planning structure",
                    "detail": "Safe public detail",
                    "technical_name": "private tool name",
                    "file_changes": [{"path": "secret.py", "change": "added"}],
                }
            )
            payload = json.loads(progress.path.read_text(encoding="utf-8"))

        self.assertEqual(payload["activity_key"], "plan_structure")
        self.assertNotIn("technical_name", payload)
        self.assertNotIn("file_changes", payload)

    def test_collection_links_resource_action_and_assistant_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            progress_path = root / "progress.jsonl"
            bundle = root / "bundle"
            bundle.mkdir()
            bundle.joinpath("run.py").write_text("print('ok')\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "OPTPILOT_RESOURCE_ACTION_PROGRESS_FILE": str(progress_path),
                    "OPTPILOT_ACTION_REQUEST_ID": "request-123",
                    "OPTPILOT_ASSISTANT_SESSION_ID": "session-456",
                    "OPTPILOT_ACCOUNT_ID": "student-7",
                    "OPTPILOT_WORKSPACE_ID": "workspace-8",
                    "DEVS_HEADLESS_COLLECTOR_URL": "http://127.0.0.1:8010",
                    "DEVS_COLLECTOR_INGEST_TOKEN": "test-token",
                    "DEVS_COLLECTOR_SOURCE": "devs-gen-interface-v3-headless",
                },
                clear=False,
            ), patch.object(
                DEVSBackendService,
                "_collector_project_archive",
                return_value=b"snapshot",
            ), patch.object(
                DEVSBackendService, "_collector_llm_usage", return_value={"calls": 2}
            ), patch("headless_generate.urllib.request.urlopen") as urlopen:
                response = MagicMock(status=200)
                response.__enter__.return_value = response
                urlopen.return_value = response
                progress = _HeadlessProgress()
                progress.emit("build", "Building")
                saved = _report_headless_collection(
                    bundle=bundle,
                    specification="A small queue.",
                    root_model_name="QueueSystem",
                    metadata={"schema_version": "devs.simulation.v2"},
                    finalizer_result={"verdict": "pass", "fixed": False},
                    progress=progress,
                )
                request = urlopen.call_args.args[0]
                payload = json.loads(request.data)

        self.assertTrue(saved)
        self.assertEqual(payload["participant_id"], "student-7")
        self.assertEqual(payload["session"]["session_id"], "session-456")
        self.assertEqual(payload["requests"][0]["request_id"], "request-123")
        self.assertEqual(
            payload["projects"][0]["project"]["origin"]["workspace_id"],
            "workspace-8",
        )
        self.assertEqual(payload["messages"], [])


if __name__ == "__main__":
    unittest.main()
