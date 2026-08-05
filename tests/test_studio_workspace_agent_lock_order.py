"""Regression contract for Studio's Conversation/Workspace lock order."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SERVER = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "server.py"


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"(?m)^def\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(f"Python function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?def\s+[A-Za-z_][\w]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioWorkspaceAgentLockOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _SERVER.read_text(encoding="utf-8")

    def test_workspace_list_reads_attachments_before_taking_workspace_lock(self) -> None:
        public = _function_source(self.source, "_list_ui_workspaces")
        unlocked = _function_source(self.source, "_list_ui_workspaces_unlocked")

        self.assertLess(
            public.index("attachment_map = _workspace_attachment_map(state)"),
            public.index("with state._workspace_index_lock"),
        )
        self.assertIn("attachment_map=attachment_map", public)
        self.assertIn("attachment_map: Optional[Mapping[str, List[str]]]", unlocked)
        self.assertNotIn("_workspace_attachment_map(state)", unlocked)


if __name__ == "__main__":
    unittest.main()
