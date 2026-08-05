"""Focused UI contracts for truthful Conversation switching and compact access."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_HTML = _STATIC / "index.html"
_CSS = _STATIC / "styles.css"
_SERVER = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "server.py"


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


class StudioConversationResilienceStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.html = _HTML.read_text(encoding="utf-8")
        cls.css = _CSS.read_text(encoding="utf-8")
        cls.server_source = _SERVER.read_text(encoding="utf-8")

    def test_workspace_panel_is_always_expanded(self) -> None:
        self.assertIn('class="conversation-workspace-shell"', self.html)
        self.assertNotIn("conversationWorkspaceDisclosure", self.html)

        render = _function_source(self.source, "renderConversationWorkspaceAccess")
        self.assertNotIn("conversationWorkspaceExpandedBySession", render)
        self.assertNotIn("conversationWorkspaceDisclosure", self.source)
        self.assertIn(".conversation-workspace-shell", self.css)
        self.assertNotIn(
            ".conversation-workspace-panel > details:not([open]) > .conversation-workspace-body",
            self.css,
        )

    def test_workspace_list_scrolls_within_the_panel(self) -> None:
        body = re.search(
            r"\.conversation-workspace-body\s*\{([^}]*)\}",
            self.css,
        )
        listing = re.search(
            r"\.conversation-workspace-list,\s*\n\.conversation-workspace-choices\s*\{([^}]*)\}",
            self.css,
        )
        self.assertIsNotNone(body)
        self.assertIsNotNone(listing)
        self.assertIn("minmax(0, 1fr)", body.group(1))
        self.assertIn("overflow-y: auto", listing.group(1))

    def test_hydration_records_a_public_error_for_the_exact_conversation(self) -> None:
        hydrate = _function_source(self.source, "hydrateAgentSessionById")

        self.assertIn("delete state.agentSessionHydrationErrors[sessionId]", hydrate)
        self.assertIn("state.agentSessionHydrationErrors[sessionId]", hydrate)
        self.assertIn("boundedPublicActionError", hydrate)
        self.assertIn("agentSessionHydrationRequests.get(sessionId) === requestToken", hydrate)
        self.assertIn("requestWasCurrent", hydrate)
        self.assertIn("state.agentSessionHydrationRequests.delete(sessionId)", hydrate)
        self.assertLess(
            hydrate.index("state.agentSessionHydrationRequests.delete(sessionId)"),
            hydrate.rindex("renderAssistant()"),
        )

    def test_exact_conversation_hydration_is_single_flight(self) -> None:
        hydrate = _function_source(self.source, "hydrateAgentSessionById")

        self.assertIn("agentSessionHydrationPromises: new Map()", self.source)
        self.assertIn(
            "state.agentSessionHydrationPromises.get(sessionId)", hydrate
        )
        self.assertIn("if (existingHydration) return existingHydration", hydrate)
        self.assertIn(
            "state.agentSessionHydrationPromises.set(sessionId, hydrationPromise)",
            hydrate,
        )
        self.assertIn(
            "state.agentSessionHydrationPromises.get(sessionId) === hydrationPromise",
            hydrate,
        )

    def test_periodic_conversation_sync_does_not_overlap_or_hide_load_errors(self) -> None:
        sync = _function_source(self.source, "syncActiveAgentSession")

        self.assertIn("if (state.agentSessionRefreshInFlight) return", sync)
        self.assertIn("state.agentSessionRefreshInFlight = true", sync)
        self.assertIn("state.agentSessionRefreshInFlight = false", sync)
        self.assertIn("state.agentSessionHydrationErrors[selectedSession.id]", sync)
        self.assertIn("!selectedHydrationFailed", sync)

    def test_aborted_get_does_not_attempt_a_second_json_response(self) -> None:
        disconnect = "except (BrokenPipeError, ConnectionResetError):"
        storage = "except (CoordinationStorageUnavailable, sqlite3.Error) as exc:"

        get_boundary = self.server_source[
            self.server_source.index("        def do_GET(self)") :
            self.server_source.index("        def do_POST(self)")
        ]
        self.assertIn(disconnect, get_boundary)
        self.assertLess(get_boundary.index(disconnect), get_boundary.index(storage))
        disconnect_body = get_boundary[
            get_boundary.index(disconnect) : get_boundary.index(storage)
        ]
        self.assertIn("return", disconnect_body)
        self.assertNotIn("_send_json", disconnect_body)

    def test_aborted_mutation_does_not_attempt_a_second_json_response(self) -> None:
        disconnect = "except (BrokenPipeError, ConnectionResetError):"
        storage = "except (CoordinationStorageUnavailable, sqlite3.Error) as exc:"

        post_boundary = self.server_source[
            self.server_source.index("        def do_POST(self)") :
            self.server_source.index("        def do_DELETE(self)")
        ]
        delete_boundary = self.server_source[
            self.server_source.index("        def do_DELETE(self)") :
            self.server_source.index("        def _handle_workspace_get(self")
        ]
        for boundary in (post_boundary, delete_boundary):
            self.assertIn(disconnect, boundary)
            self.assertLess(boundary.index(disconnect), boundary.index(storage))
            disconnect_body = boundary[
                boundary.index(disconnect) : boundary.index(storage)
            ]
            self.assertIn("return", disconnect_body)
            self.assertNotIn("_send_json", disconnect_body)

    def test_switch_renders_loading_before_waiting_for_hydration(self) -> None:
        select = _function_source(self.source, "selectAgentSession")
        render = _function_source(self.source, "renderAssistant")
        label = _function_source(self.source, "assistantSessionStatusLabel")

        hydration = select.index("hydrateAgentSessionById(sessionId")
        loading_render = select.index("renderWorkspace()", hydration)
        completion = select.index("await hydration", loading_render)
        ready_render = select.index("renderAssistant()", completion)
        self.assertLess(hydration, loading_render)
        self.assertLess(loading_render, completion)
        self.assertLess(completion, ready_render)
        self.assertIn("agentSessionHydrationState(session)", render)
        self.assertIn("agentSessionHydrationHtml(session, hydration)", render)
        self.assertIn('return "Loading"', label)
        self.assertIn('return "Load failed"', label)

    def test_failed_hydration_hides_cached_messages_and_offers_retry(self) -> None:
        status = _function_source(self.source, "agentSessionHydrationHtml")
        retry = _function_source(self.source, "retryAgentSessionHydration")

        self.assertIn("Loading Conversation", status)
        self.assertIn("Conversation could not be loaded", status)
        self.assertIn("Cached messages are hidden", status)
        self.assertIn("data-conversation-load-retry", status)
        self.assertIn("hydrateAgentSessionById(sessionId, { force: true })", retry)
        self.assertLess(retry.index("renderAssistant()"), retry.index("await hydration"))

    def test_assistant_activity_uses_plain_labels_and_hides_internal_reasoning(self) -> None:
        informative = _function_source(self.source, "assistantEventIsInformative")
        summary = _function_source(self.source, "assistantStepSummary")
        group = _function_source(self.source, "assistantStepGroupHtml")

        self.assertIn('title: "Planning the next step"', summary)
        self.assertIn("OptPilot is deciding what information or action is needed next.", summary)
        self.assertNotIn("payload.reasoning", summary)
        self.assertNotIn('title: "Reasoning"', summary)
        self.assertIn("assistantToolActivity(payload.tool", summary)
        self.assertIn("Technical details", group)
        self.assertNotIn("step.type", group)
        self.assertIn('payload.tool === "optpilot_conversation_title"', informative)

    def test_default_workspace_change_is_presented_as_a_future_action_target(self) -> None:
        summary = _function_source(self.source, "assistantStepSummary")

        self.assertIn('event.type === "assistant_workspace_changed"', summary)
        self.assertIn("Future file and command actions will use this Workspace.", summary)

    def test_conversation_list_distinguishes_loading_empty_and_failed_states(self) -> None:
        refresh = _function_source(self.source, "refreshAgentSessionSummaries")
        render = _function_source(self.source, "renderAssistantSessionList")
        retry = _function_source(self.source, "retryAgentSessionList")

        self.assertIn("state.agentSessionsError", refresh)
        self.assertIn("boundedPublicActionError", refresh)
        self.assertIn("Loading Conversations", render)
        self.assertIn("Conversations could not be refreshed", render)
        self.assertIn("The Conversations already shown have been kept", render)
        self.assertIn("state.agentSessionsLoaded", render)
        self.assertIn("No Conversations yet", render)
        self.assertIn("data-conversation-list-retry", render)
        self.assertIn("refreshAgentSessionSummaries()", retry)

    def test_conversation_list_rerenders_when_exact_loading_settles(self) -> None:
        render = _function_source(self.source, "renderAssistantSessionList")

        self.assertIn("agentSessionHydrationState(session).status", render)

    def test_conversation_requests_have_bounded_waits(self) -> None:
        fetch = _function_source(self.source, "fetchWithTimeout")
        refresh = _function_source(self.source, "refreshAgentSessionSummaries")
        hydrate = _function_source(self.source, "hydrateAgentSessionById")
        create = _function_source(self.source, "createAgentSessionForSurface")

        self.assertIn("AbortController", fetch)
        self.assertIn("Studio did not respond in time", fetch)
        self.assertIn("timeoutMs: 12000", refresh)
        self.assertIn("timeoutMs: 15000", hydrate)
        self.assertIn("timeoutMs: 15000", create)


if __name__ == "__main__":
    unittest.main()
