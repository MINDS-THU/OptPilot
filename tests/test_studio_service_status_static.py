"""Static contracts for Studio's public service-status labels."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP_JS = (
    Path(__file__).resolve().parents[1]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start : source.index(f"function {next_name}(", start)]


class StudioServiceStatusStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")

    def test_assistant_launcher_uses_current_connected_status(self) -> None:
        label = _function(
            self.source,
            "assistantPublicRuntimeLabel",
            "assistantRuntimeLabel",
        )

        self.assertIn("status.connected || status.reachable", label)
        self.assertIn('return "Ready"', label)


if __name__ == "__main__":
    unittest.main()
