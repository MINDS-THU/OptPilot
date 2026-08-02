"""Focused browser contracts for refresh-safe primary Studio addresses."""

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


class StudioMinimalRoutingStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")

    def test_primary_entities_have_refresh_safe_hash_addresses(self) -> None:
        for route in (
            '#/workspaces/${segment(state.selectedSessionId)}',
            '#/catalog/${segment(state.selectedComponentKey)}',
            '#/studies/${segment(state.selectedPlanId)}',
            '#/runs/${segment(state.selectedRunId)}',
            '/candidates/${segment(state.routedCandidateId)}',
        ):
            self.assertIn(route, self.source)
        self.assertIn('window.addEventListener("hashchange"', self.source)
        self.assertIn("applyStudioRoute({ loadRun: false, render: false })", self.source)

    def test_candidate_address_uses_candidate_identity_not_a_ui_selection_handle(self) -> None:
        self.assertIn(
            'data-open-candidate-route="${escapeHtml(item.id || "")}"',
            self.source,
        )
        self.assertIn(
            "state.routedCandidateId = candidateId;",
            self.source,
        )
        self.assertNotIn("/candidates/${segment(selectionId)}", self.source)

    def test_candidate_route_is_resolved_by_the_server_after_refresh(self) -> None:
        self.assertIn(
            '`?candidate_id=${encodeURIComponent(state.routedCandidateId)}`',
            self.source,
        )
        self.assertIn(
            "state.routedCandidateResolution = preserveCandidateRoute",
            self.source,
        )
        self.assertIn(
            'candidateResolution.schema !== "optpilot.run-candidate-resolution.v1"',
            self.source,
        )
        self.assertIn("focused.candidate.selection.selection_id === selectionId", self.source)

    def test_candidate_route_has_truthful_missing_and_retired_states(self) -> None:
        self.assertIn('status === "not_found"', self.source)
        self.assertIn('status === "retired"', self.source)
        self.assertIn("Candidate not found", self.source)
        self.assertIn("Candidate from an unavailable Run", self.source)
        self.assertIn("Open Shortlist", self.source)
        self.assertIn('data-open-candidate-route="${escapeHtml(selection.entity_id)}"', self.source)


if __name__ == "__main__":
    unittest.main()
