"""Focused browser contracts for truthful Catalog hydration and refresh."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_STYLES = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "styles.css"
)


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


class StudioCatalogHydrationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_hash_route_and_loading_shell_render_before_startup_requests(self) -> None:
        state = _between(self.source, "const state = {", "const els = {};")
        initialize = _between(
            self.source,
            "function initializeApp()",
            'if (document.readyState === "loading")',
        )

        self.assertIn("catalogLoaded: false", state)
        self.assertIn("catalogLoading: true", state)
        self.assertLess(
            initialize.index("applyStudioRoute({ loadRun: false, render: false })"),
            initialize.index("renderAll()"),
        )
        self.assertLess(initialize.index("renderAll()"), initialize.index("void loadAll()"))

    def test_startup_is_resilient_but_action_callers_remain_strict(self) -> None:
        load_all = _between(
            self.source,
            "async function loadAll()",
            "async function refreshPlatformStatus()",
        )
        loader = _between(
            self.source,
            "async function loadCatalogAndCompatibility(",
            "async function loadUiWorkspaces()",
        )

        self.assertIn("loadCatalogAndCompatibility({ strict: false })", load_all)
        self.assertIn("const strict = options.strict !== false", loader)
        self.assertIn("const settle = (promise) => promise.then(", loader)
        self.assertIn("const catalogResult = await catalogResultPromise", loader)
        self.assertIn(
            "const compatibilityResult = await compatibilityResultPromise",
            loader,
        )
        self.assertIn('getJson("/api/catalog", { timeoutMs: CORE_REQUEST_TIMEOUT_MS })', loader)
        self.assertIn('getJson("/api/compatibility", { timeoutMs: CORE_REQUEST_TIMEOUT_MS })', loader)
        self.assertIn('catalogResult.status === "fulfilled"', loader)
        self.assertIn("state.catalog = catalogResult.value", loader)
        self.assertIn(
            "applyStudioRoute({ loadRun: false, render: false })",
            loader,
        )
        self.assertIn('compatibilityResult.status === "fulfilled"', loader)
        self.assertIn("state.compatibility = compatibilityResult.value", loader)
        self.assertIn("if (strict && failure) throw failure", loader)
        self.assertLess(
            loader.index("state.catalog = catalogResult.value"),
            loader.index("const compatibilityResult = await compatibilityResultPromise"),
        )
        self.assertLess(
            loader.index("state.catalog = catalogResult.value"),
            loader.index("if (strict && failure) throw failure"),
        )

    def test_refresh_preserves_items_and_catalog_failure_is_independent(self) -> None:
        loader = _between(
            self.source,
            "async function loadCatalogAndCompatibility(",
            "async function loadUiWorkspaces()",
        )

        before_results = loader[: loader.index("const catalogResult = await")]
        self.assertNotIn("state.catalog =", before_results)
        self.assertIn("state.catalogLoading = true", before_results)
        self.assertIn("state.catalogError = \"\"", before_results)
        self.assertIn("state.catalogError =", loader)
        self.assertIn("state.compatibilityError =", loader)
        self.assertIn("state.catalogLoading = false", loader)
        self.assertIn("const requestSeq = ++state.catalogRequestSeq", loader)
        self.assertGreaterEqual(
            loader.count("requestSeq === state.catalogRequestSeq"),
            2,
        )

    def test_catalog_distinguishes_every_user_visible_load_state(self) -> None:
        renderer = _between(
            self.source,
            "function renderCatalog()",
            "function renderConfiguredCatalogSources()",
        )
        notice = _between(
            self.source,
            "function catalogLoadNotice()",
            "function renderConfiguredCatalogSources()",
        )

        self.assertIn("state.catalogLoading && !state.catalogLoaded", renderer)
        self.assertIn("No Catalog items have been published.", renderer)
        self.assertIn("No Catalog items match these filters.", renderer)
        self.assertIn(
            'componentHtml || (state.catalogError ? "" : emptyInline(emptyMessage))',
            renderer,
        )
        self.assertIn("Loading Catalog…", notice)
        self.assertIn("Refreshing Catalog…", notice)
        self.assertIn('role="status"', notice)
        self.assertIn('role="alert"', notice)
        self.assertIn("Catalog could not be loaded.", notice)
        self.assertEqual(notice.count("catalog-load-retry"), 1)
        self.assertIn("void loadCatalogAndCompatibility({ strict: false })", renderer)
        self.assertIn(".catalog-load-notice", self.styles)
        self.assertIn(".catalog-load-notice.error", self.styles)

    def test_workspace_refresh_failure_preserves_the_last_loaded_records(self) -> None:
        loader = _between(
            self.source,
            "async function loadUiWorkspaces()",
            "async function loadStudyDrafts()",
        )
        success, failure = loader.split("} catch (error) {", 1)

        self.assertIn(
            'const workspaces = requireArrayField(payload, "workspaces", "Workspace list")',
            success,
        )
        self.assertIn("state.uiWorkspaces = workspaces", success)
        self.assertIn("state.uiWorkspacesLoaded = true", success)
        self.assertIn('state.uiWorkspacesError = ""', success)
        self.assertIn("state.uiWorkspacesError = boundedPublicActionError", failure)
        self.assertNotIn("state.uiWorkspaces =", failure)
        self.assertNotIn("state.uiWorkspacesLoaded =", failure)

    def test_workspace_refresh_failure_is_retryable_on_both_surfaces(self) -> None:
        conversation = _between(
            self.source,
            "function renderConversationWorkspaceAccess()",
            "async function handleConversationWorkspaceAction(event)",
        )
        workspace_page = _between(
            self.source,
            "function renderWorkspace()",
            "function workspaceNoticeForCurrentContext(session)",
        )

        for renderer, notice_class in (
            (conversation, "conversation-workspace-refresh-notice"),
            (workspace_page, "workspace-list-refresh-notice"),
        ):
            self.assertIn("state.uiWorkspacesError", renderer)
            self.assertIn(notice_class, renderer)
            self.assertIn('role="alert"', renderer)
            self.assertIn("Showing the last loaded Workspace list.", renderer)
            self.assertIn("Try again", renderer)

    def test_successful_workspace_retry_rebuilds_cards_and_clears_the_notice(self) -> None:
        conversation_action = _between(
            self.source,
            "async function handleConversationWorkspaceAction(event)",
            "function renderAssistant()",
        )
        workspace_page = _between(
            self.source,
            "function renderWorkspace()",
            "function workspaceNoticeForCurrentContext(session)",
        )
        loader = _between(
            self.source,
            "async function loadUiWorkspaces()",
            "async function loadStudyDrafts()",
        )

        self.assertIn('state.uiWorkspacesError = ""', loader)
        retries = (
            conversation_action[conversation_action.index('if (action === "refresh")') :],
            workspace_page[workspace_page.index("const retryWorkspaces") :],
        )
        for retry in retries:
            self.assertIn("await loadUiWorkspaces()", retry)
            self.assertIn("rebuildDerivedState()", retry)
            self.assertLess(
                retry.index("await loadUiWorkspaces()"),
                retry.index("rebuildDerivedState()"),
            )
            self.assertLess(
                retry.index("rebuildDerivedState()"),
                retry.index("renderWorkspace()"),
            )


if __name__ == "__main__":
    unittest.main()
