"""The Run page is built around Candidates and their trial evidence.

Candidates are proposed solutions and therefore form the upper loop. Each
Candidate groups the Trials that evaluate it; Trials then reveal attempts,
observations, metrics, constraints, and artifacts.
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
        cls.render = cls.app[start : start + 5000]

    def _position(self, pattern: str) -> int:
        match = re.search(pattern, self.render)
        self.assertIsNotNone(match, f"not found in the Run page render: {pattern}")
        return match.start()

    def test_general_run_context_precedes_candidate_specific_content(self) -> None:
        trace = self._position(r"runTrialMapHtml\(detail\)")
        for name, pattern in (("lineage", r"runLineageHtml"), ("progress guidance", r"progressGuidance")):
            with self.subTest(after=name):
                self.assertLess(
                    self._position(pattern),
                    trace,
                    f"the {name} must come before Candidate-specific content",
                )
        self.assertLess(trace, self._position(r"run-secondary-navigation"))

    def test_the_explorer_never_silently_omits_candidates_or_trials(self) -> None:
        self.assertIn("candidatePaging.has_more", self.app)
        self.assertIn('data-run-page-more="candidate"', self.app)
        self.assertIn("trialPaging.has_more", self.app)
        self.assertIn('data-run-page-more="logical_trial"', self.app)

    def test_the_page_opens_on_the_running_candidate(self) -> None:
        self.assertIn("liveGroup", self.app)
        self.assertRegex(self.app, r'liveGroup\s*=\s*\[\.\.\.groups\]\.reverse\(\)\.find')

    def test_a_finished_page_opens_on_the_best_candidate(self) -> None:
        self.assertIn("(best ? best.id", self.app)
        self.assertIn("selectedRunCandidateIds", self.app)

    def test_candidates_group_trials_and_each_trial_keeps_its_evidence(self) -> None:
        for evidence in (
            'workbenchPage(detail, "candidate")',
            'workbenchPage(detail, "attempt")',
            'workbenchPage(detail, "observation")',
            "observation.metrics.rows",
            "observation.constraints.rows",
            "observation.artifact_count",
        ):
            self.assertIn(evidence, self.app)
        self.assertIn("group.trials.push(node)", self.app)
        self.assertIn("Each Candidate is a proposed solution", self.app)
        self.assertIn("Trials for this Candidate", self.app)

    def test_trial_value_is_not_replaced_by_candidate_aggregate(self) -> None:
        self.assertIn("candidateAggregateValue", self.app)
        self.assertRegex(
            self.app,
            r'value:\s*typeof observation\.objective_value === "number"',
        )
        self.assertIn("Overall ${escapeHtml(group.metric", self.app)

    def test_a_running_trial_says_what_is_happening(self) -> None:
        self.assertIn("run-trial-live-note", self.app)
        self.assertIn("Running now", self.app)
        self.assertIn("Still running", self.app)

    def test_a_failed_trial_does_not_promise_a_result_later(self) -> None:
        self.assertIn("No usable result", self.app)
        self.assertIn("No Candidate has a usable aggregate result yet", self.app)


if __name__ == "__main__":
    unittest.main()
