from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from devs_display.backend.remote_finalizer import (
    RemoteCodexFinalizerClient,
    RemoteFinalizerError,
    build_remote_finalizer_archive,
)


class RemoteFinalizerTests(unittest.TestCase):
    def test_archive_keeps_project_relative_layout_and_skips_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "model"
            (bundle / "devs_project" / "_analysis_logs" / "llm_calls").mkdir(
                parents=True
            )
            (bundle / "run.py").write_text("print('ok')\n", encoding="utf-8")
            (bundle / "devs_project" / "model.py").write_text(
                "MODEL = True\n", encoding="utf-8"
            )
            (bundle / "devs_project" / "_analysis_logs" / "llm_calls" / "large.txt").write_text(
                "private log", encoding="utf-8"
            )
            archive = build_remote_finalizer_archive(
                bundle_root=bundle,
                project_rel="generated/model",
            )
            with zipfile.ZipFile(io.BytesIO(archive)) as opened:
                names = set(opened.namelist())
            self.assertIn("generated/model/run.py", names)
            self.assertIn("generated/model/devs_project/model.py", names)
            self.assertNotIn(
                "generated/model/devs_project/_analysis_logs/llm_calls/large.txt",
                names,
            )

    def test_apply_changes_is_atomic_and_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "model"
            bundle.mkdir()
            (bundle / "run.py").write_text("old\n", encoding="utf-8")
            RemoteCodexFinalizerClient._apply_changes(
                bundle_root=bundle,
                payload={
                    "changed_files": [
                        {
                            "path": "run.py",
                            "content_base64": base64.b64encode(b"new\n").decode("ascii"),
                        }
                    ],
                    "deleted_paths": [],
                },
            )
            self.assertEqual((bundle / "run.py").read_text(), "new\n")
            with self.assertRaises(RemoteFinalizerError):
                RemoteCodexFinalizerClient._apply_changes(
                    bundle_root=bundle,
                    payload={
                        "changed_files": [
                            {
                                "path": "../outside.py",
                                "content_base64": base64.b64encode(b"bad").decode("ascii"),
                            }
                        ],
                        "deleted_paths": [],
                    },
                )


if __name__ == "__main__":
    unittest.main()
