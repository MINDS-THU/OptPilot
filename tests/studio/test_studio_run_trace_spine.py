"""The Run page is built around the trace of trials.

The owner's description: present the run trace as the main visual component,
let people navigate from it to each trial's evidence, and show what the
currently running trial is doing. Before this the trace was the seventh
element on the page, it silently omitted trials beyond the first loaded page,
it drew already-finished trials as "Planned", and a running trial showed the
same empty facts as a finished one.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_APP = (
    Path(__file__).resolve().parents[2]
    / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
)


class RunTraceSpineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _APP.read_text(encoding="utf-8")
        # Three renders write into this element: an empty state, a loading
        # state, and the real page, which is the last of them.
        start = cls.app.rindex("els.runDetail.innerHTML = `")
        cls.render = cls.app[start : start + 3000]

    def _position(self, pattern: str) -> int:
        match = re.search(pattern, self.render)
        self.assertIsNotNone(match, f"not found in the Run page render: {pattern}")
        return match.start()

    def test_the_trace_comes_before_everything_except_the_heading(self) -> None:
        trace = self._position(r"runTrialMapHtml\(detail\)")
        for name, pattern in (
            ("lineage", r"runLineageHtml"),
            ("summary tiles", r'class="detail-stats'),
            ("progress guidance", r"\{progressGuidance\}"),
            ("result tabs", r'class="tabs"'),
        ):
            with self.subTest(after=name):
                self.assertLess(
                    trace,
                    self._position(pattern),
                    f"the trace must come before the {name}",
                )

    def test_the_trace_never_silently_omits_trials(self) -> None:
        self.assertIn("moreTrialsUnloaded", self.app)
        self.assertIn("Showing the first", self.app)
        self.assertIn('data-run-page-more="logical_trial"', self.app)

    def test_unloaded_trials_are_not_drawn_as_not_started(self) -> None:
        # Drawing planned chips from a partial page labelled trials that had
        # already run as though they had never started.
        self.assertIn(
            "const ghostCount = moreTrialsUnloaded ? 0 : Math.max(0, planned - nodes.length);",
            self.app,
        )

    def test_the_page_opens_on_the_running_trial(self) -> None:
        self.assertIn("liveNode", self.app)
        self.assertRegex(self.app, r'liveNode\s*=\s*\[\.\.\.nodes\]\.reverse\(\)\.find')

    def test_a_running_trial_says_what_is_happening(self) -> None:
        self.assertIn("run-trial-live-note", self.app)
        self.assertIn("Running now", self.app)
        self.assertIn("still running", self.app)

    def test_a_failed_trial_does_not_promise_a_result_later(self) -> None:
        self.assertIn("none — this trial failed", self.app)


if __name__ == "__main__":
    unittest.main()
