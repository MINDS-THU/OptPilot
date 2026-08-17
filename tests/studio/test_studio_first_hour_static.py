"""Three browser defects a first-time user meets, pinned so they stay fixed.

Each of these failed silently rather than loudly, which is why they survived:
a list that stays empty forever, a panel that reports blockers about a package
nothing has checked, and a button that opens a blank page. None produces an
error anywhere, so only an assertion keeps them fixed.

These read the client source as text, the established pattern for browser
contracts in this suite.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_UI = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
)


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    # Start the brace matching after the parameter list: a default value such
    # as `options = {}` would otherwise close the count immediately.
    paren = source.index("(", start)
    depth = 0
    for index in range(paren, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                start = index
                break
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not bound function {name}")


class FirstHourStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (_UI / "app.js").read_text(encoding="utf-8")

    def test_a_later_catalog_load_rebuilds_the_run_setup_list(self) -> None:
        # The first catalog fetch can time out. Every later success must
        # rebuild the Run setup list, or all shipped Run setups stay invisible
        # until the page is reloaded.
        body = _function_source(self.app, "loadCatalogAndCompatibility")
        self.assertIn("state.plans = buildPlans();", body)
        self.assertIn('item.source === "draft config"', body)
        self.assertNotIn(
            "rebuildDerivedState()",
            body,
            "rebuildDerivedState also resets the person's current selection",
        )

    def test_the_run_setup_view_reports_a_catalog_failure(self) -> None:
        body = _function_source(self.app, "renderExperiments")
        self.assertIn("state.catalogError", body)
        self.assertIn("retry-catalog-plans", body)

    def test_an_unchecked_package_does_not_report_blockers(self) -> None:
        # A plan that has never been checked arrives as an empty object, which
        # is truthy in JavaScript -- so the panel used to announce blockers
        # about a package nothing had looked at.
        body = _function_source(self.app, "packagePlanValidationHtml")
        guard = body.index('typeof validation.valid !== "boolean"')
        blockers = body.index("found blockers")
        self.assertLess(
            guard,
            blockers,
            "the not-checked-yet guard must come before the failure wording",
        )
        self.assertIn("Not checked yet", body)

    def test_opening_a_candidate_from_the_trial_map_fetches_it(self) -> None:
        # Setting the route without loading renders an empty pane.
        body = _function_source(self.app, "bindRunTrialMap")
        self.assertIn("loadRunDetail(", body)
        self.assertIn("fromRoute: true", body)

    def test_every_run_setup_search_field_is_actually_searched(self) -> None:
        # Guards the same class of defect the catalog search had: a search
        # function reading a field the records do not carry.
        body = _function_source(self.app, "planSearchText")
        self.assertTrue(
            re.search(r"\bdescription\b", body),
            "Run setup search must include the description users can see",
        )


if __name__ == "__main__":
    unittest.main()


class RunHonestyStaticTest(unittest.TestCase):
    """A Run must not describe itself in ways that stop being true.

    Both defects here were observed live: four Runs showing a live "running"
    badge, one silent for 135 hours while still reporting that the method
    "may still be preparing another Candidate"; and failed Runs that never
    said why.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (_UI / "app.js").read_text(encoding="utf-8")

    def test_a_long_silence_is_reported_as_stuck(self) -> None:
        self.assertIn("RUN_STALLED_GUIDANCE_DELAY_MS", self.app)
        body = _function_source(self.app, "runProgressGuidance")
        self.assertIn("appears to be stuck", body)
        # The stalled branch must be tested and returned from before the
        # still-working wording can be reached. Compare against the LAST
        # occurrence: the explanation above the branch quotes that wording.
        guard = body.index("idleMilliseconds >= RUN_STALLED_GUIDANCE_DELAY_MS")
        still_preparing = body.rindex("may still be preparing")
        self.assertLess(
            guard,
            still_preparing,
            "the stalled check must come before the still-working wording",
        )

    def test_a_failed_run_names_its_reason(self) -> None:
        self.assertIn("RUN_STOP_REASONS", self.app)
        for code in (
            "max_failures",
            "method_failed",
            "protocol_error",
            "method_completed",
        ):
            with self.subTest(code=code):
                self.assertIn(f"{code}:", self.app)
        body = _function_source(self.app, "runCompletionMessage")
        self.assertIn("RUN_STOP_REASONS[stopCode]", body)
