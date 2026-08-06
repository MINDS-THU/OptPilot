"""Release contracts for the conversation-first Studio shell."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_HTML = _STATIC / "index.html"
_STYLES = _STATIC / "styles.css"


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


class StudioConversationShellStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.html = _HTML.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_conversation_is_a_refresh_safe_shell_surface(self) -> None:
        parser = _function_source(self.source, "parseStudioRoute")
        serializer = _function_source(self.source, "studioRouteHash")
        application = _function_source(self.source, "applyStudioRoute")

        self.assertIn('page === "conversations"', parser)
        self.assertIn('surface: "conversation"', parser)
        self.assertIn("#/conversations/", serializer)
        self.assertIn('state.shell.surface = "conversation"', application)
        self.assertIn("setSelectedAgentSessionState(route.conversationId)", application)
        self.assertIn("shell-v2", self.source)
        self.assertIn("shell-legacy", self.source)

    def test_existing_content_addresses_remain_supported(self) -> None:
        parser = _function_source(self.source, "parseStudioRoute")
        serializer = _function_source(self.source, "studioRouteHash")
        for page in ("catalog", "studies", "runs", "workspaces", "interfaces"):
            self.assertIn(f'page === "{page}"', parser)
        for route in (
            "#/catalog/",
            "#/studies/",
            "#/runs/",
            "#/workspaces/",
            "#/interfaces/",
        ):
            self.assertIn(route, serializer)

    def test_one_assistant_dom_serves_conversation_and_overlay(self) -> None:
        self.assertEqual(self.html.count('class="panel agent-panel"'), 1)
        for element_id in ("agentTimeline", "agentInput"):
            self.assertEqual(self.html.count(f'id="{element_id}"'), 1)

        for function_name in (
            "renderShell",
            "openConversationSurface",
            "openContentSurface",
            "setAssistantOverlayOpen",
        ):
            self.assertIn(f"function {function_name}(", self.source)

        self.assertIn('id="askOptPilotButton"', self.html)
        self.assertIn("assistant-overlay-open", self.source)
        self.assertIn("assistant-overlay-open", self.styles)

    def test_assistant_has_surface_appropriate_landmark_semantics(self) -> None:
        shell = _function_source(self.source, "renderShell")

        self.assertIn('setAttribute("role", "region")', shell)
        self.assertIn('setAttribute("role", "dialog")', shell)
        self.assertIn('setAttribute("aria-modal", "true")', shell)
        self.assertIn(
            'setAttribute("aria-labelledby", "assistantTitle")', shell
        )

    def test_conversation_exposes_workspace_access_in_a_right_hand_rail(self) -> None:
        render = _function_source(self.source, "renderConversationWorkspaceAccess")
        assistant = _function_source(self.source, "renderAssistant")
        actions = _function_source(self.source, "handleConversationWorkspaceAction")

        self.assertIn('id="conversationWorkspacePanel"', self.html)
        self.assertIn('aria-labelledby="conversationWorkspaceTitle"', self.html)
        self.assertIn("Workspaces in this conversation", self.html)
        self.assertIn("The default is used when your request does not name one", self.html)
        self.assertIn("renderConversationWorkspaceAccess()", assistant)
        self.assertIn("attachedWorkspaceIds(agentSession.id)", render)
        self.assertIn('workspace.mode !== "read-only"', render)
        self.assertIn("workspace.visibleInWorkspaces !== false", render)
        self.assertIn("isCatalogSourceView(workspace)", render)
        for action in (
            'action === "open"',
            'action === "current"',
            'action === "add"',
            'action === "remove"',
        ):
            self.assertIn(action, actions)
        self.assertIn("selectSession(workspaceId)", actions)
        self.assertIn("attachWorkspaceToCurrent(workspaceId)", actions)
        self.assertIn("closeWorkspaceFromCurrentSession(workspaceId)", actions)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 292px", self.styles)
        self.assertIn("grid-column: 2", self.styles)
        self.assertIn("@media (max-width: 1180px)", self.styles)

    def test_opening_a_conversation_workspace_does_not_silently_change_default(
        self,
    ) -> None:
        select = _function_source(self.source, "selectSession")
        set_selected = _function_source(self.source, "setSelectedWorkspace")
        keep_selected = _function_source(self.source, "keepWorkspaceSelected")
        actions = _function_source(
            self.source, "handleConversationWorkspaceAction"
        )

        self.assertIn("async function selectSession(sessionId)", select)
        self.assertNotIn("syncSelectedWorkspaceToBackend", select)
        self.assertNotIn("selectedWorkspaceByAgentSession", set_selected)
        self.assertNotIn("selectedWorkspaceByAgentSession", keep_selected)
        self.assertIn("selectSession(workspaceId)", actions)
        self.assertIn('action === "current"', actions)
        self.assertIn("selectedWorkspaceByAgentSession", actions)
        self.assertIn("syncSelectedWorkspaceToBackend", actions)

    def test_attaching_an_additional_workspace_preserves_the_existing_default(
        self,
    ) -> None:
        attach = _function_source(
            self.source, "attachWorkspaceToAgentSession"
        )

        self.assertIn("const persistedConversation", attach)
        self.assertIn(
            "!persistedConversation && !state.selectedWorkspaceByAgentSession[agentSession.id]",
            attach,
        )
        self.assertIn("mergeAgentSessionPayload(payload.session)", attach)

    def test_main_conversation_does_not_repeat_its_heading(self) -> None:
        self.assertIn(
            "body.shell-v2.shell-conversation .agent-panel > .panel-header",
            self.styles,
        )
        self.assertIn('aria-label="OptPilot Conversation"', self.html)

    def test_idle_conversation_cards_do_not_repeat_ready(self) -> None:
        card = _function_source(self.source, "agentSessionCard")
        self.assertIn('const statusVisible = statusLabel !== "Ready"', card)
        self.assertIn("statusVisible ? statusLabel", card)
        self.assertIn("metadata ?", card)

    def test_v2_primary_navigation_exposes_the_three_durable_destinations(self) -> None:
        self.assertEqual(self.html.count('class="nav-button" data-view="catalog"'), 1)
        for view in ("experiments", "runs"):
            self.assertEqual(
                self.html.count(
                    f'class="nav-button shell-primary-destination" data-view="{view}"'
                ),
                1,
            )
        nav_start = self.html.index("shell-primary-navigation")
        nav_end = self.html.index("</nav>", nav_start)
        primary_navigation = self.html[nav_start:nav_end]
        self.assertNotIn('data-view="workspace"', primary_navigation)
        self.assertIn("legacy-navigation", self.html)
        legacy_start = self.html.index("legacy-navigation")
        legacy_end = self.html.index("</nav>", legacy_start)
        legacy_navigation = self.html[legacy_start:legacy_end]
        self.assertIn('data-view="experiments"', legacy_navigation)
        self.assertIn('data-view="runs"', legacy_navigation)
        self.assertNotIn('data-view="catalog"', legacy_navigation)

    def test_catalog_configures_studies_before_runs_are_launched(self) -> None:
        detail = _function_source(self.source, "renderComponentDetail")
        compatibility = _function_source(self.source, "compatList")

        self.assertIn("for Run setup", detail)
        self.assertIn(">Configure Run setup</button>", compatibility)
        self.assertNotIn(">Configure Run</button>", compatibility)
        self.assertIn(">Configure Run setups</small>", self.html)

    def test_local_folders_use_one_no_copy_action_name(self) -> None:
        close_dialog = _function_source(self.source, "closeLocalFolderDialog")
        connect = _function_source(self.source, "connectLocalFolder")

        self.assertIn(">Link local folder</button>", self.html)
        self.assertIn(">Link local folder</h2>", self.html)
        self.assertNotIn(">Open local folder</", self.html)
        self.assertNotIn(">Open folder</button>", self.html)
        self.assertIn('textContent = "Link local folder"', close_dialog)
        self.assertIn('textContent = "Linking…"', connect)

    def test_platform_status_uses_user_concepts(self) -> None:
        code_editor = _function_source(self.source, "codeEditorService")
        assistant = _function_source(self.source, "openHandsService")

        self.assertIn('label: "Code editor"', code_editor)
        self.assertIn("code-server", code_editor)
        self.assertIn('label: "Assistant"', assistant)
        self.assertNotIn('label: "OptPilot"', assistant)
        self.assertIn("OpenHands", assistant)
        self.assertIn('aria-label="Resize Conversation panel"', self.html)

    def test_conversation_onboarding_uses_loaded_catalog_content(self) -> None:
        renderer = _function_source(self.source, "renderConversationOnboarding")

        self.assertIn('id="conversationOnboarding"', self.html)
        self.assertIn("state.catalog", renderer)
        for catalog_kind in ("environment", "method", "resource"):
            self.assertIn(catalog_kind, renderer.lower())
        self.assertNotIn("fetch(", renderer)

    def test_shell_surface_changes_do_not_stop_or_recreate_work(self) -> None:
        navigation = "\n".join(
            _function_source(self.source, name)
            for name in (
                "openConversationSurface",
                "openContentSurface",
                "setAssistantOverlayOpen",
            )
        )
        for destructive_call in (
            "stopInterfaceLaunch",
            "stopOperatorJob",
            "cancelAgent",
            'interfaceSessionFrame.src = ""',
            "removeAttribute(\"src\")",
        ):
            self.assertNotIn(destructive_call, navigation)

    def test_confirmed_missing_deep_links_clear_stale_selections(self) -> None:
        application = _function_source(self.source, "applyStudioRoute")

        self.assertIn("state.routeCollectionsReady", application)
        for loaded_flag in (
            "state.agentSessionsLoaded",
            "state.uiWorkspacesLoaded",
            "state.catalogLoaded",
            "state.studyDraftsLoaded",
        ):
            self.assertIn(loaded_flag, application)
        for cleared_selection in (
            "setSelectedAgentSessionState(null)",
            "state.selectedSessionId = null",
            "state.selectedComponentKey = null",
            "state.selectedPlanId = null",
        ):
            self.assertIn(cleared_selection, application)
        settle_run = _function_source(self.source, "settleMissingRunRoute")
        load_run = _function_source(self.source, "loadRunDetail")
        self.assertIn("state.selectedRunId = null", settle_run)
        self.assertIn("state.selectedRun = null", settle_run)
        self.assertIn("route.runId !== runId", settle_run)
        self.assertIn("Number(error && error.status || 0) === 404", load_run)
        self.assertIn("if (missingConversation) syncStudioRoute()", application)
        self.assertIn("if (missingEntity) syncStudioRoute()", application)

    def test_content_openers_focus_the_visible_shell_heading(self) -> None:
        focus = _function_source(self.source, "focusVisibleContentSurface")
        opening = _function_source(self.source, "openContentSurface")
        onboarding = _function_source(self.source, "renderConversationOnboarding")
        candidate_interface = _function_source(
            self.source, "openCandidateInterfaceSession"
        )
        launch_interface = _function_source(
            self.source, "openLaunchInterfaceSession"
        )
        workspace = _function_source(self.source, "selectSession")

        self.assertIn("els.shellSurfaceTitle", focus)
        self.assertIn("target.getClientRects().length", focus)
        self.assertIn("target.focus({ preventScroll: true })", focus)
        self.assertIn("focusVisibleContentSurface(view)", opening)
        self.assertIn('openContentSurface("catalog"', onboarding)
        self.assertIn('focusVisibleContentSurface("interface")', candidate_interface)
        self.assertIn('focusVisibleContentSurface("interface")', launch_interface)
        self.assertIn('openContentSurface("workspace"', workspace)
        self.assertIn(
            'id="shellSurfaceTitle" role="heading" aria-level="1" tabindex="-1"',
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
