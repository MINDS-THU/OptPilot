"""Focused browser-client contracts for managed workspace lifecycle actions."""

from __future__ import annotations

import unittest
from pathlib import Path


_STATIC = (
    Path(__file__).resolve().parents[1]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
)


def _function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def _css_rule(source: str, selector: str) -> str:
    start = source.index(f"{selector} {{")
    return source[start : source.index("}", start)]


class StudioWorkspaceLifecycleStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (_STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (_STATIC / "index.html").read_text(encoding="utf-8")
        cls.styles = (_STATIC / "styles.css").read_text(encoding="utf-8")

    def test_closed_managed_workspace_reopens_through_the_same_workspace_list(self) -> None:
        mapping = _function_source(
            self.source, "uiWorkspaceSession", "mergeUiWorkspace"
        )
        attach = _function_source(
            self.source, "attachWorkspaceToCurrent", "reopenManagedWorkspace"
        )
        reopen = _function_source(
            self.source, "reopenManagedWorkspace", "keepWorkspaceSelected"
        )
        card = _function_source(self.source, "sessionCard", "workspaceSubtitle")

        self.assertIn('realmManaged: workspace.ownership === "realm-managed"', mapping)
        self.assertIn("reopenRequired: Boolean(workspace.reopen_required)", mapping)
        self.assertIn("workspace.reopenRequired", attach)
        self.assertIn("await reopenManagedWorkspace(workspace)", attach)
        self.assertIn("/reopen", reopen)
        self.assertIn("expected_workspace_revision: workspace.workspaceRevision", reopen)
        self.assertIn('class="session-card-more"', card)
        self.assertIn("assistantSessionLabel(assistantSession)", card)
        self.assertIn("Ask in ${escapeHtml(assistantLabel)}", card)
        self.assertIn("Remove from ${escapeHtml(assistantLabel)}", card)
        self.assertNotIn("session.reopenRequired", card)

    def test_workspace_selection_is_independent_of_assistant_attachment(self) -> None:
        select = _function_source(
            self.source, "selectSession", "attachWorkspaceAndRender"
        )
        rebuild = _function_source(
            self.source, "rebuildDerivedState", "ensureAgentSessions"
        )
        create = _function_source(
            self.source, "createBlankSession", "nextDraftWorkspaceTitle"
        )
        current = _function_source(
            self.source, "currentSession", "currentPlan"
        )

        self.assertNotIn("attachWorkspaceAndRender(sessionId)", select)
        self.assertIn("setSelectedWorkspace(sessionId", select)
        self.assertNotIn("attachedIds", rebuild)
        self.assertIn("state.sessions[0]", rebuild)
        self.assertIn("attached_sessions: []", create)
        self.assertNotIn("attachWorkspaceToCurrent(session.id)", create)
        self.assertNotIn("attachedWorkspaceIds", current)
        self.assertIn("state.selectedSessionId", current)
        self.assertIn("No Workspace selected", self.source)
        self.assertIn("Workspace ready", self.source)
        self.assertIn("Editable work", self.html)
        self.assertIn('data-view="workspace"', self.html)

    def test_workspace_card_separates_storage_catalog_and_named_assistant_access(
        self,
    ) -> None:
        mapping = _function_source(
            self.source, "uiWorkspaceSession", "mergeUiWorkspace"
        )
        rendering = _function_source(
            self.source, "renderWorkspace", "runWorkspaceAction"
        )
        card = _function_source(
            self.source, "sessionCard", "workspaceSubtitle"
        )
        badges = _function_source(
            self.source, "workspaceBadges", "workspaceCatalogBadgeLabel"
        )
        catalog = _function_source(
            self.source,
            "workspaceCatalogStatusFromValues",
            "assistantSessionLabel",
        )
        assistant = _function_source(
            self.source, "workspaceAssistantAccessLabel", "agentSessionCard"
        )

        self.assertIn(
            "catalogPublications: workspace.catalog_publications || []", mapping
        )
        self.assertIn("catalogOrigin: workspace.catalog_origin || null", mapping)
        self.assertIn('["Storage", workspaceStorageLabel(session)]', rendering)
        self.assertIn('["Catalog", workspaceCatalogStatus(session)]', rendering)
        self.assertIn(
            '["Assistant", workspaceAssistantAccessLabel(session)]', rendering
        )
        self.assertIn("Catalog · ${escapeHtml(", badges)
        self.assertNotIn("saved Workspace", badges)
        self.assertIn(
            "attachedWorkspaceIds(agentSession.id).includes(session.id)",
            badges,
        )
        self.assertIn('return "Published version in Catalog"', catalog)
        self.assertIn('return "Based on a Catalog version"', catalog)
        self.assertIn('return "Not published to Catalog"', catalog)
        self.assertIn("assistantSessionLabel(assistantSession)", card)
        self.assertIn("Ask in ${escapeHtml(assistantLabel)}", card)
        self.assertIn("Remove from ${escapeHtml(assistantLabel)}", card)
        self.assertIn("Available to ${assistantSessionLabel(agentSession)}", assistant)
        self.assertIn(
            "Not available to ${assistantSessionLabel(agentSession)}", assistant
        )

    def test_assistant_workspace_notice_is_scoped_to_the_active_conversation(
        self,
    ) -> None:
        attach = _function_source(
            self.source, "attachWorkspaceAndRender", "startAssistantResize"
        )
        selection = _function_source(
            self.source, "setSelectedAgentSessionState", "selectAgentSession"
        )
        select = _function_source(
            self.source, "selectAgentSession", "createAgentSession"
        )
        create = _function_source(
            self.source, "createAgentSession", "closeWorkspaceFromCurrentSession"
        )
        render = _function_source(
            self.source, "renderWorkspace", "workspaceNoticeForCurrentContext"
        )
        notice = _function_source(
            self.source, "workspaceNoticeForCurrentContext", "runWorkspaceAction"
        )
        detach = _function_source(
            self.source,
            "detachWorkspaceFromSession",
            "renderWorkspaceCleanupModal",
        )

        self.assertIn(
            "const agentSession = await attachWorkspaceToCurrent(workspaceId)",
            attach,
        )
        self.assertIn("agentSession.id === state.selectedAgentSessionId", attach)
        self.assertIn("assistantSessionId: agentSession.id", attach)
        self.assertIn("workspaceNoticeForCurrentContext(session)", render)
        self.assertIn(
            "notice.assistantSessionId !== state.selectedAgentSessionId", notice
        )
        self.assertIn("state.workspaceNotice = null", selection)
        self.assertIn("setSelectedAgentSessionState(sessionId)", select)
        self.assertIn("setSelectedAgentSessionState(payload.session.id)", create)
        self.assertIn("setSelectedAgentSessionState(id)", create)
        self.assertIn(
            "options.announce && agentSession.id === state.selectedAgentSessionId",
            detach,
        )
        self.assertIn("assistantSessionId: agentSession.id", detach)

    def test_read_only_catalog_source_is_a_hidden_support_view(self) -> None:
        mapping = _function_source(
            self.source, "uiWorkspaceSession", "mergeUiWorkspace"
        )
        ordering = _function_source(
            self.source, "orderedWorkspaceSessions", "isCatalogSourceView"
        )
        rendering = _function_source(
            self.source, "renderWorkspace", "runWorkspaceAction"
        )
        workbench = _function_source(
            self.source, "renderWorkbenchMode", "renderWorkspaceWorkbenchToolbar"
        )

        self.assertIn(
            "visibleInWorkspaces: workspace.visible_in_workspaces !== false",
            mapping,
        )
        self.assertIn("session.visibleInWorkspaces !== false", ordering)
        self.assertIn("Read-only Catalog item", rendering)
        self.assertIn("not editable or listed in Workspaces", rendering)
        self.assertIn("Back to item", rendering)
        self.assertIn("Edit in Workspace", rendering)
        self.assertIn('buttonMode === "setup"', workbench)
        self.assertIn('buttonMode === "preview"', workbench)

    def test_managed_workspace_rename_uses_a_metadata_revision_fence(self) -> None:
        mapping = _function_source(
            self.source, "uiWorkspaceSession", "mergeUiWorkspace"
        )
        toolbar = _function_source(
            self.source,
            "renderWorkspaceWorkbenchToolbar",
            "handleWorkspaceTitleKeydown",
        )
        rename = _function_source(
            self.source,
            "saveWorkspaceTitleFromInput",
            "commitManagedWorkspace",
        )

        self.assertIn(
            "workspaceMetadataRevision: "
            "Number(workspace.realm_workspace_metadata_revision || 0) || null",
            mapping,
        )
        self.assertIn(
            'session.mode === "read-only"', toolbar
        )
        self.assertNotIn("session.realmManaged ||", toolbar)
        self.assertIn(
            'schema: "optpilot.studio-workspace-rename-request.v1"', rename
        )
        self.assertIn("request_id: newRequestId()", rename)
        self.assertIn("expected_title: session.title", rename)
        self.assertIn(
            "expected_metadata_revision: session.realmManaged", rename
        )
        self.assertIn("session.workspaceMetadataRevision", rename)
        self.assertIn("await loadUiWorkspaces()", rename)
        self.assertIn("state.workspaceNotice", rename)
        self.assertNotIn("pushAssistantMessage", rename)

    def test_internal_commit_is_guarded_and_workspace_deletion_is_plainly_named(self) -> None:
        commit = _function_source(
            self.source, "commitManagedWorkspace", "renderCodeWorkspacePlaceholder"
        )
        destructive = _function_source(
            self.source, "workspaceDestructiveLabel", "renderCodeServerCard"
        )

        self.assertNotIn('id="workspaceCommitButton"', self.html)
        self.assertIn("expected_workspace_revision: session.workspaceRevision", commit)
        self.assertIn("mergeUiWorkspace(payload.workspace)", commit)
        self.assertIn(': "Delete Workspace";', destructive)
        self.assertIn("Its published Catalog version was not changed.", self.source)

    def test_detach_removes_only_named_assistant_access_without_cleanup_prompt(
        self,
    ) -> None:
        close = _function_source(
            self.source,
            "closeWorkspaceFromCurrentSession",
            "detachWorkspaceFromSession",
        )
        detach = _function_source(
            self.source,
            "detachWorkspaceFromSession",
            "renderWorkspaceCleanupModal",
        )

        self.assertIn(
            "await detachWorkspaceFromSession(workspaceId, agentSession.id",
            close,
        )
        self.assertNotIn("pendingWorkspaceCleanup", close)
        self.assertNotIn("renderWorkspaceCleanupModal", close)
        self.assertIn("/detach-workspace", detach)
        self.assertIn("remains in Workspaces with all of its files", detach)
        self.assertIn("Removed from ${assistantSessionLabel(agentSession)}", detach)
        self.assertNotIn("pushAssistantMessage", detach)
        self.assertNotIn("deleteWorkspaceDraft", detach)

    def test_sidebar_workspace_controls_stay_inside_the_clipped_rail(self) -> None:
        panel = _css_rule(self.styles, ".sidebar-workspaces")
        header = _css_rule(self.styles, ".sidebar-workspaces-header")
        header_actions = _css_rule(
            self.styles,
            ".sidebar-workspaces-header .header-actions",
        )
        workspace_list = _css_rule(
            self.styles,
            ".sidebar-workspaces .session-list",
        )
        card = _css_rule(self.styles, ".sidebar-workspaces .session-card")
        more = _css_rule(
            self.styles,
            ".sidebar-workspaces .session-card-more",
        )
        more_summary = _css_rule(
            self.styles,
            ".sidebar-workspaces .session-card-more > summary",
        )
        card_markup = _function_source(
            self.source,
            "sessionCard",
            "workspaceSubtitle",
        )

        self.assertIn("grid-template-columns: minmax(0, 1fr)", panel)
        self.assertIn("min-width: 0", panel)
        self.assertIn("flex-wrap: nowrap", header)
        self.assertIn("padding: 6px 8px", header)
        self.assertIn("display: flex", header_actions)
        self.assertIn("flex: 0 0 auto", header_actions)
        self.assertIn("margin-left: auto", header_actions)
        self.assertIn("width: 100%", workspace_list)
        self.assertIn("min-width: 0", workspace_list)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", card)
        self.assertIn("width: 100%", card)
        self.assertIn("max-width: 100%", more)
        self.assertIn("width: 100%", more_summary)
        self.assertIn("min-height: 24px", more_summary)
        self.assertIn("list-style: none", more_summary)
        self.assertIn(
            '<summary aria-label="Actions for '
            '${escapeHtml(session.title)}">Actions</summary>',
            card_markup,
        )
        self.assertIn(
            ".sidebar-workspaces .session-card-more > summary:focus-visible",
            self.styles,
        )
        self.assertIn('aria-label="Create Workspace"', self.html)


if __name__ == "__main__":
    unittest.main()
