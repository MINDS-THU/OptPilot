from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot.candidate_materialization import _snapshot_readonly_ref


class CandidateMaterializationSecurityTests(unittest.TestCase):
    def test_readonly_glob_rejects_traversal_before_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / "secret.txt").write_text("secret", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "safe relative"):
                _snapshot_readonly_ref(workspace, "../*.txt")

    def test_readonly_glob_rejects_external_symlink_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            link = workspace / "linked.txt"
            try:
                link.symlink_to(secret)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable")

            with self.assertRaisesRegex(ValueError, "escapes trial workspace"):
                _snapshot_readonly_ref(workspace, "*.txt")

    def test_readonly_glob_keeps_contained_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            nested = workspace / "nested"
            nested.mkdir(parents=True)
            (nested / "input.txt").write_text("input", encoding="utf-8")

            records = _snapshot_readonly_ref(workspace, "**/*.txt")

            self.assertEqual([record["path"] for record in records], ["nested/input.txt"])


if __name__ == "__main__":
    unittest.main()
