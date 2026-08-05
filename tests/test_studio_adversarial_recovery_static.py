"""Cross-surface recovery and mental-model contracts for Studio."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)
_ASSISTANT_DOC = _ROOT / "docs" / "assistant.md"
_HISTORICAL_DESIGN = _ROOT / "resource" / "platform-ui-design.md"


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


class StudioAdversarialRecoveryStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_background_conversation_sync_is_bounded_and_independent(self) -> None:
        active = _function_source(self.source, "syncActiveAgentSession")
        exact = _function_source(self.source, "syncAgentSessionById")

        self.assertIn("Promise.allSettled", active)
        self.assertNotIn("await Promise.all([", active)
        self.assertIn("ASSISTANT_MUTATION_TIMEOUT_MS", exact)

    def test_failed_send_restores_the_message_and_truthful_status(self) -> None:
        send = _function_source(self.source, "sendAgentMessage")
        push = _function_source(self.source, "pushAssistantMessage")
        persist = _function_source(self.source, "persistAssistantMessage")

        self.assertIn(
            "pushAssistantMessage(userMessage, { persist: false })", send
        )
        self.assertIn("const priorStatus = session.status", send)
        self.assertIn("const priorEffectiveStatus = session.effective_status", send)
        self.assertIn("removeLocalAssistantMessage(sessionId, localUserMessage)", send)
        self.assertIn("restoreUnsentAssistantDraft(sessionId, message)", send)
        self.assertIn("session.status = priorStatus", send)
        self.assertIn("session.effective_status = priorEffectiveStatus", send)
        self.assertIn("ASSISTANT_MUTATION_TIMEOUT_MS", send)
        self.assertIn("return localMessage", push)
        self.assertIn("timeoutMs: options.timeoutMs", persist)

    def test_stop_can_time_out_without_locking_the_composer_forever(self) -> None:
        cancel = _function_source(self.source, "cancelAgentMessage")

        self.assertIn("ASSISTANT_MUTATION_TIMEOUT_MS", cancel)
        self.assertIn("state.cancellingAgentSessionIds.delete(session.id)", cancel)
        self.assertIn('{ persist: false }', cancel)
        self.assertIn("Try Stop again", cancel)

    def test_successful_conversation_refresh_is_authoritative_without_racing_creation(self) -> None:
        refresh = _function_source(self.source, "refreshAgentSessionSummaries")

        self.assertIn("sessionIdsAtRequestStart", refresh)
        self.assertIn("const concurrentlyAdded", refresh)
        self.assertIn("const removedIds", refresh)
        self.assertIn("removedIds.forEach(forgetAgentSessionLocalState)", refresh)
        self.assertIn("state.agentSessionsError", refresh)

    def test_missing_conversation_selects_and_renders_a_clean_fallback(self) -> None:
        hydrate = _function_source(self.source, "hydrateAgentSessionById")
        forget = _function_source(self.source, "forgetAgentSessionLocalState")

        self.assertIn("[404, 410].includes", hydrate)
        self.assertIn("const wasSelected", hydrate)
        self.assertIn("forgetAgentSessionLocalState(sessionId)", hydrate)
        self.assertIn("restoreAssistantContinuity(state.selectedAgentSessionId)", hydrate)
        self.assertIn("syncStudioRoute()", hydrate)
        self.assertIn("renderAssistant()", hydrate)
        self.assertIn("cancellingAgentSessionIds.delete(sessionId)", forget)
        self.assertIn("syncingAgentSessionIds.delete(sessionId)", forget)
        self.assertIn("returnConversationId === sessionId", forget)
        self.assertIn("assistantRunSelection.session_id === sessionId", forget)

    def test_late_conversation_sync_cannot_recreate_a_removed_card(self) -> None:
        sync = _function_source(self.source, "syncAgentSessionById")

        self.assertIn("state.agentSessions.some((item) => item.id === session.id)", sync)
        self.assertIn("updateAgentSessionFromPayload(payload.session)", sync)

    def test_conversation_default_does_not_switch_the_workspace_page(self) -> None:
        action = _function_source(self.source, "handleConversationWorkspaceAction")

        self.assertIn('action === "current"', action)
        self.assertIn("state.selectedWorkspaceByAgentSession", action)
        self.assertIn("syncSelectedWorkspaceToBackend", action)
        self.assertNotIn("setSelectedWorkspace(workspaceId)", action)

    def test_make_available_stays_an_attachment_action(self) -> None:
        attach = _function_source(self.source, "attachWorkspaceAndRender")

        self.assertIn("attachWorkspaceToCurrent", attach)
        self.assertIn("loadUiWorkspaces", attach)
        self.assertIn("renderWorkspace", attach)
        self.assertIn("renderAssistant", attach)
        self.assertNotIn("setView", attach)
        self.assertNotIn("setAssistantOpen", attach)
        self.assertNotIn("agentInput.focus", attach)

    def test_read_only_shell_uses_the_catalog_item_term(self) -> None:
        shell = _function_source(self.source, "renderShell")

        self.assertIn('? "Catalog item"', shell)
        self.assertNotIn('? "Catalog source"', shell)

    def test_read_only_result_drawer_moves_and_restores_focus(self) -> None:
        open_view = _function_source(self.source, "openSelectionContentView")
        close_view = _function_source(self.source, "closeSelectionContentView")
        render_host = _function_source(self.source, "renderSelectionContentHost")
        drawer = _function_source(self.source, "renderSelectionContentDrawer")

        self.assertIn("document.activeElement", open_view)
        self.assertIn("selectionContentReturnFocus", open_view)
        self.assertIn("selectionContentFocusPending", open_view)
        self.assertIn("returnFocus.isConnected", close_view)
        self.assertIn("returnFocus.focus()", close_view)
        self.assertIn("closeButton || drawer", render_host)
        self.assertIn("target.focus()", render_host)
        self.assertIn('tabindex="-1"', drawer)

    def test_docs_describe_page_context_and_open_work_consistently(self) -> None:
        assistant = _ASSISTANT_DOC.read_text(encoding="utf-8")
        historical = _HISTORICAL_DESIGN.read_text(encoding="utf-8")

        self.assertIn("with requests sent while that page is open", assistant)
        self.assertNotIn("bounded read-only context for one request", assistant)
        self.assertIn("Open work contains only", historical)
        self.assertNotIn("Active work contains only", historical)


if __name__ == "__main__":
    unittest.main()
