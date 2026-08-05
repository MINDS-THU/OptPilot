"""Continuity and recovery contracts for conversation-first Studio work."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_HTML = _STATIC / "index.html"


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


class StudioConversationContinuityStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.html = _HTML.read_text(encoding="utf-8")

    def test_each_conversation_preserves_its_draft_and_scroll_state(self) -> None:
        capture = _function_source(self.source, "captureAssistantContinuity")
        restore = _function_source(self.source, "restoreAssistantContinuity")

        self.assertIn("assistantDrafts", self.source)
        self.assertIn("assistantDraftsBySession", capture)
        self.assertIn("assistantScrollBySession", capture)
        self.assertIn("assistantDraftsBySession", restore)
        self.assertIn("assistantScrollBySession", restore)
        self.assertIn("sessionStorage", self.source)

    def test_timeline_replacement_is_guarded_by_a_content_signature(self) -> None:
        render = _function_source(self.source, "renderAssistant")

        self.assertIn("assistantTimelineSignatures", self.source)
        self.assertIn("assistantTimelineSignatures", render)
        self.assertIn("els.agentTimeline.innerHTML", render)
        self.assertRegex(
            render,
            r"if\s*\([^)]*(?:signature|timelineChanged|timelineUnchanged)[^)]*\)",
        )

    def test_blank_conversation_space_is_not_a_workspace_navigation_target(self) -> None:
        assistant = _function_source(self.source, "renderAssistant")
        workspace = _function_source(self.source, "renderWorkspace")

        self.assertIn("dataset.timelineSessionId", assistant)
        self.assertNotIn("els.agentTimeline.dataset.sessionId", assistant)
        self.assertIn(
            'els.sessionList.querySelectorAll("[data-session-id]")', workspace
        )
        self.assertNotIn(
            'document.querySelectorAll("[data-session-id]")', workspace
        )

    def test_active_work_is_a_projection_over_running_process_state(self) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        renderer = _function_source(self.source, "renderOpenWork")

        self.assertIn('id="openWorkButton"', self.html)
        self.assertIn('id="openWorkShelf"', self.html)
        self.assertIn('id="openWorkItems"', self.html)
        for canonical_state in (
            "state.interfaceLaunch",
            "state.studyLaunch",
            "state.runs",
        ):
            self.assertIn(canonical_state, projection)
        for durable_or_conversation_state in (
            "state.plans",
            "state.sessions",
            "state.agentSessions",
        ):
            self.assertNotIn(durable_or_conversation_state, projection)
        self.assertIn("buildOpenWorkItems()", renderer)
        self.assertIn("Saved work remains in Studies, Runs, and Workspaces", renderer)
        for persistence_or_network in ("localStorage", "sessionStorage", "fetch("):
            self.assertNotIn(persistence_or_network, projection)

    def test_active_work_uses_existing_process_coordinates(self) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        opening = _function_source(self.source, "openOpenWorkItem")
        interface_opening = _function_source(
            self.source, "openExactOpenWorkInterface"
        )

        for coordinate_prefix in (
            "interface:",
            "study-launch:",
            "run:",
        ):
            self.assertIn(coordinate_prefix, projection)
        for excluded_prefix in ("approval:", "plan:", "workspace:"):
            self.assertNotIn(excluded_prefix, projection)
        self.assertIn("canonicalRunId(run)", projection)
        self.assertNotIn("activity_id", projection)
        self.assertNotIn("open_work_id", projection)
        for existing_opener in (
            "openExactOpenWorkInterface",
            "openExactOpenWorkStudyLaunch",
            "openContentSurface",
            "loadRunDetail",
        ):
            self.assertIn(existing_opener, opening)
        for excluded_opener in ("openConversationSurface", "selectSession"):
            self.assertNotIn(excluded_opener, opening)
        self.assertIn("openLaunchInterfaceSession", interface_opening)
        self.assertIn("openFailedInterfaceSource", interface_opening)
        self.assertNotIn("openActiveInterfaceLocation()", interface_opening)


if __name__ == "__main__":
    unittest.main()
