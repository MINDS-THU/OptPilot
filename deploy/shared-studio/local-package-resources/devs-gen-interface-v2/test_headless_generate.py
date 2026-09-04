from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devs_display.backend.remote_finalizer import RemoteCodexFinalizerClient
from headless_generate import _run_automatic_check


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


if __name__ == "__main__":
    unittest.main()
