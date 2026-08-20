"""Refusing a Workspace location says where one can go.

The refusal named the folder that was rejected and stopped there: a person was
told where they could not put a Workspace and never where they could.

Listing the whole allowlist is not the fix either -- it runs to every catalog
package plus Studio's own storage, and steering someone into those is worse
than saying nothing. Two locations are actually theirs: the project folder
Studio was started in, and the folder it keeps Workspaces in.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import UiState, _safe_workspace_root


class WorkspaceRootRefusalTest(unittest.TestCase):
    def test_it_names_the_rejected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            with self.assertRaises(PermissionError) as raised:
                _safe_workspace_root(state, Path("/System/nowhere"))
        self.assertIn("/System/nowhere", str(raised.exception))

    def test_it_names_somewhere_that_would_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
            with self.assertRaises(PermissionError) as raised:
                _safe_workspace_root(state, Path("/System/nowhere"))
            message = str(raised.exception)
        self.assertIn(str(tmp), message)
        self.assertIn(str(state.workspaces_dir), message)

    def test_it_does_not_recite_the_whole_allowlist(self) -> None:
        # Catalog packages and Studio's private storage are in the allowlist
        # but are not places to send someone.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state = UiState(
                cwd=tmp,
                catalog_roots=[tmp / "pkg_a", tmp / "pkg_b"],
                run_roots=[],
            )
            with self.assertRaises(PermissionError) as raised:
                _safe_workspace_root(state, Path("/System/nowhere"))
            message = str(raised.exception)
        self.assertNotIn("pkg_a", message)
        self.assertNotIn("pkg_b", message)
        self.assertNotIn(str(state.sessions_dir), message)

    def test_an_allowed_location_is_still_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
            inside = tmp / "project"
            inside.mkdir()
            self.assertEqual(_safe_workspace_root(state, inside), inside.resolve())


if __name__ == "__main__":
    unittest.main()
