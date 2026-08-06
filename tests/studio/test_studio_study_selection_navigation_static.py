"""Focused contracts for coherent Study list/detail navigation."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"


def _function_source(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioStudySelectionNavigationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_search_clears_a_selected_study_that_is_no_longer_visible(self) -> None:
        reconcile = _function_source(
            self.source, "reconcileStudySelectionWithVisiblePlans"
        )
        render = _function_source(self.source, "renderExperiments")

        self.assertIn(
            "plans.some((plan) => plan.id === state.selectedPlanId)", reconcile
        )
        self.assertIn("state.selectedPlanId = null;", reconcile)
        self.assertIn('state.view === "experiments"', reconcile)
        self.assertIn("syncStudioRoute();", reconcile)
        self.assertLess(
            render.index("reconcileStudySelectionWithVisiblePlans(plans);"),
            render.index("renderPlanDetail();"),
        )

    def test_exact_study_clears_only_a_search_that_hides_it(self) -> None:
        reveal = _function_source(self.source, "revealStudyPlan")

        self.assertIn("planSearchText(plan).includes(query)", reveal)
        self.assertIn('state.planSearch = "";', reveal)
        self.assertIn('els.planSearch.value = "";', reveal)

    def test_study_deep_link_reveals_the_exact_target(self) -> None:
        route = _function_source(self.source, "applyStudioRoute")
        study_start = route.index('route.view === "experiments"')
        study_end = route.index('route.view === "runs"', study_start)
        study_branch = route[study_start:study_end]

        self.assertIn(
            "state.plans.find((item) => item.id === route.planId)", study_branch
        )
        self.assertLess(
            study_branch.index("revealStudyPlan(plan);"),
            study_branch.index("state.selectedPlanId = route.planId;"),
        )

    def test_assistant_exact_study_actions_reveal_before_selecting(self) -> None:
        assistant = _function_source(self.source, "executeAssistantUiCardAction")
        branch_starts = [
            assistant.index('if (operation === "open-catalog")'),
            assistant.index('if (operation === "configure-run")'),
            assistant.index('if (operation === "start-run")'),
        ]
        branch_ends = branch_starts[1:] + [assistant.index('if (operation === "open-launch")')]

        for start, end in zip(branch_starts, branch_ends):
            branch = assistant[start:end]
            self.assertLess(
                branch.index("revealStudyPlan(plan);"),
                branch.index("state.selectedPlanId = plan.id;"),
            )

    def test_open_work_study_handoff_reveals_the_exact_target(self) -> None:
        handoff = _function_source(self.source, "openExactOpenWorkStudyLaunch")

        self.assertIn(
            "state.plans.find((candidate) => String(candidate.id || \"\") === planId)",
            handoff,
        )
        self.assertLess(
            handoff.index("revealStudyPlan(plan);"),
            handoff.index("state.selectedPlanId = planId;"),
        )


if __name__ == "__main__":
    unittest.main()
