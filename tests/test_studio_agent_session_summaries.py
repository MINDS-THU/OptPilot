from __future__ import annotations

import tempfile
import unittest
import re
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _agent_session_by_id,
    _append_agent_message,
    _create_agent_session,
    _list_agent_session_summaries,
)


_ROOT = Path(__file__).resolve().parents[1]
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


class StudioAgentSessionSummaryTests(unittest.TestCase):
    def test_summary_list_omits_transcript_payloads_but_exact_read_remains_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            created = _create_agent_session(
                state,
                {"title": "Scheduling", "description": "Improve throughput"},
            )
            _append_agent_message(
                state,
                created["id"],
                {
                    "role": "user",
                    "title": "User",
                    "content": "Compare the available methods.",
                },
            )

            summaries = _list_agent_session_summaries(state)
            summary = next(item for item in summaries if item["id"] == created["id"])
            exact = _agent_session_by_id(state, created["id"])

        self.assertEqual(summary["title"], "Scheduling")
        self.assertIn("effective_status", summary)
        self.assertIn("pending_approval_count", summary)
        for transcript_field in ("messages", "events", "approvals"):
            self.assertNotIn(transcript_field, summary)
            self.assertIn(transcript_field, exact)
        self.assertTrue(
            any(message.get("content") == "Compare the available methods." for message in exact["messages"])
        )


class StudioAgentSessionSummaryStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_initial_load_merges_summaries_then_hydrates_the_exact_selection(self) -> None:
        load = _function_source(self.source, "loadAgentSessions")
        summaries = _function_source(self.source, "refreshAgentSessionSummaries")
        hydrate = _function_source(self.source, "hydrateAgentSessionById")

        self.assertIn("refreshAgentSessionSummaries()", load)
        self.assertIn("hydrateAgentSessionById(state.selectedAgentSessionId", load)
        self.assertIn('getJson("/api/agent-sessions?summary=1"', summaries)
        self.assertIn("getJson(", hydrate)
        self.assertIn(
            "`/api/agent-sessions/${encodeURIComponent(sessionId)}`",
            hydrate,
        )
        for destructive_reset in (
            "state.assistantMessagesBySession = {}",
            "state.agentApprovalsBySession = {}",
            "state.agentEventsBySession = {}",
        ):
            self.assertNotIn(destructive_reset, load)
            self.assertNotIn(destructive_reset, summaries)

    def test_summary_and_exact_requests_are_race_guarded(self) -> None:
        summaries = _function_source(self.source, "refreshAgentSessionSummaries")
        hydrate = _function_source(self.source, "hydrateAgentSessionById")

        self.assertIn("agentSessionSummaryRequestSeq", summaries)
        self.assertIn("requestSeq !== state.agentSessionSummaryRequestSeq", summaries)
        self.assertIn("agentSessionHydrationRequests", hydrate)
        self.assertIn("agentSessionTranscriptVersions", hydrate)
        self.assertIn("!== transcriptVersion", hydrate)

    def test_periodic_sync_uses_summaries_and_only_exact_active_sessions(self) -> None:
        sync = _function_source(self.source, "syncActiveAgentSession")

        self.assertIn("refreshAgentSessionSummaries()", sync)
        self.assertNotIn('getJson("/api/agent-sessions")', sync)
        self.assertIn("hydrateAgentSessionById(selectedSession.id", sync)
        self.assertIn("busySessions.map(syncAgentSessionById)", sync)

    def test_selecting_a_conversation_hydrates_without_blocking_continuity(self) -> None:
        select = _function_source(self.source, "selectAgentSession")
        merge = _function_source(self.source, "mergeAgentSessionPayload")

        self.assertLess(select.index("captureAssistantContinuity()"), select.index("setSelectedAgentSessionState(sessionId)"))
        self.assertLess(select.index("hydrateAgentSessionById(sessionId"), select.index("renderWorkspace()"))
        self.assertIn("await hydration", select)
        self.assertIn("Array.isArray(session.messages)", merge)
        self.assertIn("state.hydratedAgentSessionIds.add(session.id)", merge)


if __name__ == "__main__":
    unittest.main()
