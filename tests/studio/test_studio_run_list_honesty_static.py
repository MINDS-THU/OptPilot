"""The Runs list does not show a live badge for a Run that stopped moving.

The Run page was taught to say when a Run has gone quiet, but the list beside
it was not, so four Runs sat behind a plain "running" badge for two hundred and
twenty-eight hours -- nine and a half days -- surviving every restart. Someone
scanning the list had no way to tell those from a Run doing work.

The list uses the same threshold as the Run page, so the two cannot disagree
about the same Run, and it says "no progress" rather than "failed": nothing has
proved the Run dead, only that nothing has happened.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_APP = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


class RunListHonestyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # app.js holds NUL bytes, so it must be read as text explicitly.
        cls.app = _APP.read_text(encoding="utf-8", errors="replace")

    def test_the_list_has_a_stalled_check(self) -> None:
        self.assertIn("function runLooksStalled(", self.app)

    def test_it_reuses_the_run_page_threshold(self) -> None:
        body = self.app[self.app.index("function runLooksStalled(") :][:900]
        self.assertIn("RUN_STALLED_GUIDANCE_DELAY_MS", body)

    def test_a_stalled_row_does_not_render_the_plain_status(self) -> None:
        row = self.app[self.app.index("function runRow(run) {") :][:2000]
        self.assertIn("runRowStatusPill(run)", row)
        self.assertNotIn("statusPill(runStatus(run))", row)

    def test_it_does_not_claim_the_run_failed(self) -> None:
        body = self.app[self.app.index("function runRowStatusPill(") :][:700]
        self.assertIn("no progress", body)
        for overclaim in ("failed", "dead", "crashed"):
            self.assertNotIn(f'>{overclaim}<', body)


class LaunchRefusalWordingTest(unittest.TestCase):
    """A blocked launch explains itself without quoting an internal code."""

    def test_no_internal_code_is_printed_at_the_person(self) -> None:
        # The code stays reachable on hover, because it is worth quoting in a
        # bug report -- it just no longer interrupts the sentence explaining
        # what to do. A sibling test pins that the rendering still carries it.
        app = _APP.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("Launch unavailable (${escapeHtml(launch.code", app)
        self.assertIn("Launch unavailable: ${escapeHtml(publicStudyLaunchReason", app)
        self.assertIn('title="Technical reason: ${escapeHtml(launch.code', app)


if __name__ == "__main__":
    unittest.main()
