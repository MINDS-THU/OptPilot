"""Responsive release contracts for the conversation-first Studio shell."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_STYLES = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "styles.css"
)


class StudioShellResponsiveStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_phone_toolbar_keeps_the_current_surface_title(self) -> None:
        phone = self.styles[self.styles.rindex("@media (max-width: 620px)") :]

        self.assertNotIn(
            "body.shell-v2 .shell-surface-copy,\n", phone
        )
        self.assertIn("body.shell-v2 .shell-surface-copy span {", phone)
        self.assertIn("display: none;", phone)
        self.assertIn("text-overflow: ellipsis;", self.styles)

    def test_short_narrow_viewports_can_shrink_interface_work_areas(self) -> None:
        tablet_start = self.styles.rindex("@media (max-width: 900px)")
        phone_start = self.styles.rindex("@media (max-width: 620px)")
        tablet = self.styles[tablet_start:phone_start]

        self.assertIn("body.shell-v2 #interfaceView.active-view,", tablet)
        self.assertIn(
            "body.shell-v2 .workspace-grid.preview-focused {", tablet
        )
        self.assertIn("height: 100%;", tablet)
        self.assertIn("min-height: 0;", tablet)


if __name__ == "__main__":
    unittest.main()
