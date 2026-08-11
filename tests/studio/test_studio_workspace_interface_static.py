"""Focused browser-client contracts for transient workspace interfaces."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_APP_JS = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_INDEX_HTML = (
    _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "index.html"
)
_STYLES_CSS = _INDEX_HTML.with_name("styles.css")


def _source_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


class StudioWorkspaceInterfaceStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")
        cls.html = _INDEX_HTML.read_text(encoding="utf-8")
        cls.styles = _STYLES_CSS.read_text(encoding="utf-8")

    def test_workspace_launch_is_scoped_to_existing_workspace(self) -> None:
        launch = _source_between(
            self.source,
            "async function launchWorkspaceInterface(",
            "async function pollWorkspaceInterfaceLaunch(",
        )

        self.assertIn('launch_scope: "workspace-transient"', launch)
        self.assertIn("source_workspace_id: workspaceId", launch)
        self.assertIn(
            "preview.port = Number(workspaceInterface.presentation.port", launch
        )
        self.assertIn(
            "mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey)",
            launch,
        )
        self.assertIn(
            "workspaceInterfaceLaunchCapability(", launch
        )
        self.assertIn("if (capability.eligible !== true) return;", launch)
        self.assertIn("did not preserve the source workspace boundary", launch)
        self.assertIn("previousLaunch && !retryingFailedLaunch", launch)
        self.assertIn("openLaunchInterfaceSession(previousLaunch)", launch)
        self.assertIn("renderWorkspace()", launch)

    def test_active_interface_coordinate_is_isolated_to_one_browser_tab(self) -> None:
        state_source = _source_between(
            self.source,
            "const state = {",
            "const els = {};",
        )
        persist = _source_between(
            self.source,
            "function persistActiveInterfaceLaunch(",
            "function mergeInterfaceLaunchPayload(",
        )

        self.assertIn(
            "loadSessionStoredJson(STORAGE_KEYS.activeInterfaceLaunch)",
            state_source,
        )
        self.assertIn(
            "storeSessionValue(STORAGE_KEYS.activeInterfaceLaunch",
            persist,
        )
        self.assertNotIn(
            "storeValue(STORAGE_KEYS.activeInterfaceLaunch",
            persist,
        )

    def test_running_interface_affordance_lives_in_open_work(self) -> None:
        viewing = _source_between(
            self.source,
            "function isViewingActiveInterface(",
            "function activeInterfaceSource(",
        )
        indicator = _source_between(
            self.source,
            "function renderActiveInterfaceIndicator(",
            "function buildOpenWorkItems(",
        )
        shelf = _source_between(
            self.source,
            "function buildOpenWorkItems(",
            "function interfaceOutputsNeedAttention(",
        )
        return_action = _source_between(
            self.source,
            "async function openActiveInterfaceLocation(",
            "async function stopActiveInterfaceFromGlobalControl(",
        )
        stop_action = _source_between(
            self.source,
            "async function stopActiveInterfaceFromGlobalControl(",
            "function workspaceSortMs(",
        )

        # The legacy shell's global interface bar was retired with U7; the
        # Open work shelf is the persistent running-interface affordance.
        for element_id in (
            "activeInterfaceBar",
            "activeInterfaceOpenButton",
            "activeInterfaceLabel",
            "activeInterfaceSubtitle",
            "activeInterfaceStopButton",
        ):
            self.assertNotIn(f'id="{element_id}"', self.html)
        self.assertNotIn("active-interface-bar", self.styles)
        self.assertIn("renderOpenWork();", indicator)
        self.assertIn("isActiveInterfaceLaunch(launch)", shelf)
        self.assertIn(
            "activeInterfaceStatusText(launch, isViewingActiveInterface(launch))",
            shelf,
        )
        self.assertIn('"cleanup_pending"', shelf)
        self.assertIn("state.view !== \"workspace\"", viewing)
        self.assertIn("state.workbenchMode !== \"preview\"", viewing)
        self.assertIn("currentWorkspaceInterfaceLaunch(session) === launch", viewing)
        self.assertIn("currentCatalogInterfaceLaunch(session) === launch", viewing)
        self.assertIn("workspaceSessionByBackendId(workspaceId)", return_action)
        self.assertIn("/api/workspaces/${encodeURIComponent(workspaceId)}", return_action)
        self.assertIn("catalogSourceSessionByKey(launch.key)", return_action)
        self.assertIn("catalogComponentForActiveLaunch(launch, session)", return_action)
        self.assertIn('openComponentSession(component, "inspect", { workbenchMode: "preview" })', return_action)
        self.assertIn("state.interfaceReturnError", return_action)
        self.assertIn("state.interfaceReturnFallbackUrl", return_action)
        self.assertIn("stopInterfaceLaunch(launch.key)", stop_action)
        self.assertIn("renderActiveInterfaceIndicator();", self.source)
        self.assertIn(
            'on(els.returnToActiveInterfaceButton, "click", openActiveInterfaceLocation)',
            self.source,
        )
        self.assertIn(
            'on(els.stopActiveInterfaceButton, "click", stopActiveInterfaceFromGlobalControl)',
            self.source,
        )

    def test_workbench_tab_change_refreshes_active_interface_location(self) -> None:
        workbench = _source_between(
            self.source,
            "function renderWorkbenchMode(",
            "function renderWorkspaceWorkbenchToolbar(",
        )

        self.assertIn("renderActiveInterfaceIndicator();", workbench)

    def test_relaunch_updates_the_exact_catalog_source_owner(self) -> None:
        open_interface = _source_between(
            self.source,
            "async function openComponentInterface(",
            "async function resumeStoredInterfaceLaunch(",
        )
        launch_interface = _source_between(
            self.source,
            "async function launchComponentInterface(",
            "async function pollComponentInterfaceLaunch(",
        )

        self.assertIn("openLaunchInterfaceSession(launch)", open_interface)
        self.assertIn("catalogSourceSessionByKey(launchKey)", launch_interface)
        self.assertIn("sourceSession.catalogLaunchId = String(state.interfaceLaunch.launch_id)", launch_interface)

    def test_interface_conflict_exposes_return_and_safe_stop(self) -> None:
        conflict = _source_between(
            self.source,
            "function renderInterfaceConflictActions(",
            "async function openActiveInterfaceLocation(",
        )
        workspace = _source_between(
            self.source,
            "function renderPreviewWorkbench(",
            "function renderCatalogInterfaceWorkbench(",
        )
        catalog = _source_between(
            self.source,
            "function renderCatalogInterfaceWorkbench(",
            "function renderCatalogInterfaceLaunchPanel(",
        )

        for element_id in (
            "workspaceInterfaceConflictActions",
            "returnToActiveInterfaceButton",
            "stopActiveInterfaceButton",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("Return to ${label}", conflict)
        self.assertIn("Stop ${label}", conflict)
        self.assertIn("renderInterfaceConflictActions(otherInterfaceLaunch)", workspace)
        self.assertIn("renderInterfaceConflictActions(otherLaunch)", catalog)
        self.assertIn("hidden = Boolean(otherInterfaceLaunch)", workspace)
        self.assertIn("Boolean(otherLaunch)", catalog)
        self.assertIn("stopActiveInterfaceFromGlobalControl", self.source)

    def test_new_window_is_reserved_before_waiting_for_code_server(self) -> None:
        opener = _source_between(
            self.source,
            "async function openCodeServerFull(",
            "function reloadEmbeddedCodeWorkspace(",
        )
        reserve = _source_between(
            self.source,
            "function reserveExternalWindow(",
            "function navigateExternalWindow(",
        )
        preview = _source_between(
            self.source,
            "function openWorkspacePreviewExternal(",
            "async function openActiveWorkspaceExternal(",
        )

        self.assertIn('window.open("about:blank", "_blank")', reserve)
        self.assertLess(opener.index("reserveExternalWindow()"), opener.index("await postJson"))
        self.assertIn("New window blocked", opener)
        self.assertIn("New window blocked", preview)
        self.assertIn("setWorkspaceActionNotice", opener)

    def test_full_stage_interface_new_window_reports_popup_failures(self) -> None:
        events = _source_between(
            self.source,
            "function bindEvents(",
            "function nextCoreRequest(",
        )
        opener = _source_between(
            self.source,
            "function openCurrentInterfaceSessionExternal(",
            "function interfaceSessionOutputLaunch(",
        )
        renderer = _source_between(
            self.source,
            "function renderInterfaceSession(",
            "function setInterfaceSessionActionError(",
        )

        self.assertIn("interfaceSessionOpenButton", events)
        self.assertIn("openCurrentInterfaceSessionExternal", events)
        self.assertIn("reserveExternalWindow()", opener)
        self.assertIn("browser blocked the new window", opener)
        self.assertIn("navigateExternalWindow(externalWindow, model.openUrl)", opener)
        self.assertIn("closeReservedExternalWindow(externalWindow)", opener)
        self.assertIn("setInterfaceSessionActionError", opener)
        self.assertIn("state.interfaceSessionActionError", renderer)
        self.assertIn('role",', renderer)
        self.assertIn(
            'id="interfaceSessionOpenButton" class="ghost-button" type="button"',
            self.html,
        )
        self.assertNotIn('id="interfaceSessionOpenButton" class="ghost-button" target="_blank"', self.html)

    def test_workspace_interface_uses_server_launch_capability(self) -> None:
        renderer = _source_between(
            self.source,
            "function renderPreviewWorkbench(",
            "function renderWorkspaceInterfaceLaunchPanel(",
        )

        self.assertIn("workspaceInterfaceCapability.eligible !== true", renderer)
        self.assertIn("workspaceInterfaceReason", renderer)
        self.assertIn(
            "els.launchWorkspaceInterfaceButton.disabled = workspaceInterfaceUnavailable",
            renderer,
        )

    def test_catalog_inspector_reuses_code_and_interface_tabs_without_publish(self) -> None:
        modes = _source_between(
            self.source,
            "function renderWorkbenchMode(",
            "function renderWorkspaceWorkbenchToolbar(",
        )
        opener = _source_between(
            self.source,
            "async function openComponentSession(",
            "async function openComponentInterface(",
        )
        interface_opener = _source_between(
            self.source,
            "async function openComponentInterface(",
            "async function resumeStoredInterfaceLaunch(",
        )
        dispatcher = _source_between(
            self.source,
            "async function launchActiveWorkbenchInterface(",
            "async function launchWorkspaceInterface(",
        )
        catalog_launch = _source_between(
            self.source,
            "async function launchComponentInterface(",
            "async function pollComponentInterfaceLaunch(",
        )

        self.assertIn('buttonMode === "setup"', modes)
        self.assertIn('buttonMode === "preview" && !hasWorkingInterface', modes)
        self.assertNotIn('buttonMode !== "code"', modes)
        self.assertIn('options.workbenchMode === "preview"', opener)
        self.assertIn('setView("workspace", { allowSupportView: mode !== "edit" })', opener)
        self.assertIn("openLaunchInterfaceSession(launch)", interface_opener)
        self.assertIn("launchComponentInterface(component)", interface_opener)
        self.assertIn("isCatalogSourceView()", dispatcher)
        self.assertIn("launchComponentInterface(component)", dispatcher)
        self.assertIn("launchWorkspaceInterface()", dispatcher)
        self.assertIn("/api/catalog/${encodeURIComponent(component.kind)}", catalog_launch)
        self.assertNotIn("/api/workspaces/", catalog_launch)

    def test_catalog_interface_opens_full_inspector_without_starting_code_server(self) -> None:
        opener = _source_between(
            self.source,
            "async function openComponentInterface(",
            "async function resumeStoredInterfaceLaunch(",
        )
        workbench = _source_between(
            self.source,
            "function renderCatalogInterfaceWorkbench(",
            "function renderCatalogInterfaceLaunchPanel(",
        )
        polling = _source_between(
            self.source,
            "async function pollComponentInterfaceLaunch(",
            "async function loadInterfaceOutputTreeChoices(",
        )

        self.assertIn("openLaunchInterfaceSession(launch)", opener)
        self.assertNotIn("openCodeServerEmbedded", opener)
        self.assertIn("workspacePreviewFrame", workbench)
        self.assertIn("catalogInterfacePreviewUrl(session)", workbench)
        self.assertIn("renderInterfaceLaunchSurface(state.interfaceLaunch)", polling)
        self.assertNotIn("mergeUiWorkspace", polling)

    def test_catalog_code_reopens_exact_source_and_code_server_atomically(self) -> None:
        helper = _source_between(
            self.source,
            "async function openCatalogSourceCode(",
            "async function openCodeServerEmbedded(",
        )
        embedded = _source_between(
            self.source,
            "async function openCodeServerEmbedded(",
            "function setWorkspaceActionNotice(",
        )
        separate = _source_between(
            self.source,
            "async function openCodeServerFull(",
            "function reloadEmbeddedCodeWorkspace(",
        )

        self.assertIn("catalogSourceComponent(session)", helper)
        self.assertIn("component.entry.uid", helper)
        self.assertIn("/open-code`,", helper)
        self.assertIn("payload.workspace", helper)
        self.assertIn("payload.code_server", helper)
        self.assertIn("codeServer.workspace_id", helper)
        self.assertIn("payload.workspace.id", helper)
        self.assertIn("mergeUiWorkspace(payload.workspace)", helper)
        self.assertIn("refreshed.catalogComponentKey = componentKey", helper)
        self.assertIn("isCatalogSourceView(refreshed)", helper)
        self.assertNotIn("/api/workspaces/", helper)
        self.assertIn("openCatalogSourceCode(session, requestSeq)", embedded)
        self.assertIn("openCatalogSourceCode(session, requestSeq)", separate)
        self.assertIn("This published Catalog version could not be opened", embedded)
        self.assertIn("This published Catalog version could not be opened", separate)

    def test_catalog_source_resolution_retains_only_an_exact_launch_owner(self) -> None:
        resolver = _source_between(
            self.source,
            "function catalogSourceComponent(",
            "function currentCatalogInterfaceLaunch(",
        )
        launch_resolver = _source_between(
            self.source,
            "function catalogComponentForActiveLaunch(",
            "function catalogSourceComponent(",
        )

        self.assertIn("session.catalogComponentKey", resolver)
        self.assertIn("if (!preferredKey) return null", resolver)
        self.assertIn("catalogSourceComponentByKey(preferredKey)", resolver)
        self.assertIn("catalogComponentForActiveLaunch(state.interfaceLaunch, session)", resolver)
        self.assertNotIn("state.selectedComponentKey", resolver)
        self.assertNotIn("candidates", resolver)
        self.assertIn('launch.launch_scope === "workspace-transient"', launch_resolver)
        self.assertIn('new Set(["environment", "method", "resource"])', launch_resolver)
        self.assertIn('launchKey !== `${kind}:${uid}`', launch_resolver)
        self.assertIn('session.catalogComponentKey', launch_resolver)
        self.assertNotIn("state.selectedComponentKey", launch_resolver)

    def test_return_to_interface_reopens_historical_owner_without_substitution(self) -> None:
        return_action = _source_between(
            self.source,
            "async function openActiveInterfaceLocation(",
            "function renderActiveInterfaceReturnState(",
        )
        session_helpers = _source_between(
            self.source,
            "function existingCatalogSourceSession(",
            "async function openComponentSession(",
        )
        persist = _source_between(
            self.source,
            "function persistActiveInterfaceLaunch(",
            "function mergeInterfaceLaunchPayload(",
        )

        self.assertIn("catalogSourceSessionByKey(launch.key)", return_action)
        self.assertIn("catalogComponentForActiveLaunch(launch, session)", return_action)
        self.assertIn('openComponentSession(component, "inspect", { workbenchMode: "preview" })', return_action)
        self.assertIn("session.catalogLaunchId = launchId", return_action)
        self.assertIn("activeInterfaceReturnStillCurrent", return_action)
        self.assertIn("current.launch_id", return_action)
        self.assertIn("interfaceReturnPending", return_action)
        self.assertIn("interfaceReturnFallbackUrl", return_action)
        self.assertIn("reserveExternalWindow()", return_action)
        self.assertIn("browser blocked the new window", return_action)
        self.assertIn("showCatalogSourceSession", session_helpers)
        self.assertIn('setView("workspace", { allowSupportView: true })', session_helpers)
        self.assertIn("kind: String(launch.kind", persist)
        self.assertIn("uid: String(launch.uid", persist)

    def test_delayed_code_open_cannot_replace_a_new_selected_source(self) -> None:
        state_source = _source_between(
            self.source,
            "const state = {",
            "const els = {};",
        )
        helper = _source_between(
            self.source,
            "async function openCatalogSourceCode(",
            "async function openCodeServerEmbedded(",
        )
        embedded = _source_between(
            self.source,
            "async function openCodeServerEmbedded(",
            "function setWorkspaceActionNotice(",
        )
        separate = _source_between(
            self.source,
            "async function openCodeServerFull(",
            "function reloadEmbeddedCodeWorkspace(",
        )

        self.assertIn("codeWorkspaceRequestSeq: 0", state_source)
        self.assertIn("codeWorkspaceRequestIsCurrent(requestSeq, session.id)", helper)
        self.assertLess(
            helper.index("codeWorkspaceRequestIsCurrent(requestSeq, session.id)"),
            helper.index("mergeUiWorkspace(payload.workspace)"),
        )
        self.assertIn("++state.codeWorkspaceRequestSeq", embedded)
        self.assertIn("requestedSessionId", embedded)
        self.assertIn("settleSupersededCodeWorkspaceRequest(requestSeq)", embedded)
        self.assertIn("++state.codeWorkspaceRequestSeq", separate)
        self.assertIn("requestedSessionId", separate)
        self.assertIn("closeReservedExternalWindow(externalWindow)", separate)

    def test_ready_poll_retains_launch_and_applies_preview_without_switching(
        self,
    ) -> None:
        poll = _source_between(
            self.source,
            "async function pollWorkspaceInterfaceLaunch(",
            "async function openWorkspacePreview(",
        )
        ready_branch = _source_between(
            poll, 'if (status === "ready") {', 'if (status === "cleanup_pending") {'
        )

        self.assertIn("workspaceSessionByBackendId(sourceWorkspaceId)", poll)
        self.assertIn(
            "applyWorkspacePreviewPayload(sourceSession, result.preview, result.interface)",
            ready_branch,
        )
        self.assertIn(
            "updateInterfaceOutputPanel(state.interfaceLaunch, els.workspaceInterfaceLaunchStatus)",
            ready_branch,
        )
        self.assertIn("await sleep(1000);", ready_branch)
        self.assertNotIn("state.interfaceLaunch = null", ready_branch)
        self.assertNotIn("state.selectedSessionId", poll)
        self.assertNotIn('setView("workspace")', poll)

    def test_workspace_status_reuses_generic_output_and_stop_controls(self) -> None:
        status = _source_between(
            self.source,
            "function workspaceInterfaceLaunchStatus(",
            "function interfaceOutputStatusLabel(",
        )
        compact = _source_between(
            self.source,
            "function compactInterfaceLaunchStatus(",
            "function interfaceLaunchStatus(",
        )
        binding = _source_between(
            self.source,
            "function bindWorkspaceInterfaceLaunchControls(",
            "function applyWorkspacePreviewPayload(",
        )

        self.assertIn('id="workspaceInterfaceLaunchStatus"', self.html)
        self.assertIn("compactInterfaceLaunchStatus({", status)
        self.assertIn("renderInterfaceOutputs(launchState", compact)
        self.assertIn("interface-launch-summary-row", compact)
        self.assertIn('data-interface-drawer-panel="details"', compact)
        self.assertIn("workspace-stop-interface", status)
        self.assertIn("Retry cleanup", status)
        self.assertIn("stopWorkspaceInterface(launchState.key)", binding)
        self.assertIn("bindInterfaceLaunchDisclosureControls(root)", binding)
        self.assertIn("bindInterfaceOutputControls(root)", binding)

    def test_ready_status_collapses_routine_details_and_hides_empty_outputs(self) -> None:
        compact = _source_between(
            self.source,
            "function compactInterfaceLaunchStatus(",
            "function interfaceLaunchStatus(",
        )
        outputs = _source_between(
            self.source,
            "function renderInterfaceOutputs(",
            "function renderInterfaceOutputList(",
        )
        workbench_css = (
            Path(__file__).resolve().parents[2]
            / "studio"
            / "src"
            / "optpilot_studio"
            / "ui"
            / "static"
            / "styles.css"
        ).read_text(encoding="utf-8")

        self.assertIn('failed || cleanupPending', compact)
        self.assertIn('defaultPanel === "details" ? "" : "hidden"', compact)
        self.assertIn('data-interface-drawer-toggle="details"', compact)
        self.assertIn('>Launch details</button>', compact)
        self.assertIn('>Outputs (${escapeHtml(String(outputs.length))})</button>', compact)
        self.assertIn('if (!outputs.length) return "";', outputs)
        self.assertIn(
            ".preview-workbench.interface-launch-active .preview-toolbar",
            workbench_css,
        )
        self.assertNotIn("max-height: min(42vh, 360px)", workbench_css)

    def test_catalog_notice_and_interface_share_the_available_workbench_height(
        self,
    ) -> None:
        focused_body = _source_between(
            self.styles,
            ".workspace-grid.workbench-focused .workspace-body {",
            ".workspace-grid.workbench-focused .workspace-surface {",
        )
        focused_surface = _source_between(
            self.styles,
            ".workspace-grid.workbench-focused .workspace-surface {",
            ".workspace-grid.workbench-focused .workspace-context-notice {",
        )
        focused_notice = _source_between(
            self.styles,
            ".workspace-grid.workbench-focused .workspace-context-notice {",
            ".workspace-grid.workbench-focused .editor-panel {",
        )
        focused_editor = _source_between(
            self.styles,
            ".workspace-grid.workbench-focused .editor-panel {",
            ".workspace-grid.workbench-focused .workbench-mode-bar {",
        )
        preview = _source_between(
            self.styles,
            ".preview-workbench {",
            ".preview-workbench.interface-launch-active .preview-toolbar {",
        )
        stage = _source_between(
            self.styles,
            ".live-preview-stage {",
            ".workspace-preview-frame {",
        )
        frame = _source_between(
            self.styles,
            ".workspace-preview-frame {",
            ".workspace-preview-frame:not([src]) {",
        )

        self.assertIn("flex: 1 1 auto;", focused_body)
        self.assertIn("height: auto;", focused_body)
        self.assertNotIn("height: 100%;", focused_body)
        self.assertIn("overflow: hidden;", focused_body)
        self.assertIn("display: flex;", focused_surface)
        self.assertIn("flex-direction: column;", focused_surface)
        self.assertIn("min-height: 0;", focused_surface)
        self.assertIn("flex: 0 0 auto;", focused_notice)
        self.assertIn("height: 100%;", focused_editor)
        self.assertIn("min-height: 0;", focused_editor)
        self.assertIn("overflow: hidden;", focused_editor)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr);", preview)
        self.assertIn("height: 100%;", preview)
        self.assertIn("overflow: hidden;", preview)
        self.assertIn("height: auto;", stage)
        self.assertNotIn("height: 100%;", stage)
        self.assertIn("overflow: hidden;", stage)
        self.assertIn("position: absolute;", frame)
        self.assertIn("inset: 0;", frame)
        self.assertIn("height: 100%;", frame)
        self.assertIn("min-height: 0;", frame)

        mobile = self.styles[self.styles.index("@media (max-width: 820px)") :]
        mobile_preview = _source_between(
            mobile,
            ".workspace-grid.preview-focused {",
            ".workspace-files {",
        )
        self.assertIn("height: calc(100svh - 28px);", mobile_preview)
        self.assertIn("min-height: 640px;", mobile_preview)

    def test_catalog_and_workspace_share_the_compact_status_renderer(self) -> None:
        catalog = _source_between(
            self.source,
            "function interfaceLaunchStatus(",
            "function workspaceInterfaceLaunchStatus(",
        )
        workspace = _source_between(
            self.source,
            "function workspaceInterfaceLaunchStatus(",
            "function interfaceOutputStatusLabel(",
        )

        self.assertIn("return compactInterfaceLaunchStatus({", catalog)
        self.assertIn("return compactInterfaceLaunchStatus({", workspace)
        self.assertIn("component-retry-interface", catalog)
        self.assertIn("workspace-retry-interface", workspace)
        self.assertIn("Stop interface", catalog)
        self.assertIn("Stop interface", workspace)

    def test_manual_output_picker_submits_only_label_and_relative_choice(self) -> None:
        renderer = _source_between(
            self.source,
            "function renderInterfaceOutputTreePicker(",
            "function renderInterfaceOutputCard(",
        )
        picker_actions = _source_between(
            self.source,
            "async function loadInterfaceOutputTreeChoices(",
            "function interfaceContentContextId(",
        )

        self.assertIn('select name="path"', renderer)
        self.assertIn('input name="label"', renderer)
        self.assertIn("Output name", renderer)
        self.assertIn("Add output", renderer)
        self.assertNotIn("Project name", renderer)
        self.assertNotIn("Add project", renderer)
        self.assertIn(
            "This adds a read-only output card. It does not create a Workspace.",
            renderer,
        )
        self.assertIn("/outputs/tree-choices", picker_actions)
        self.assertIn("/outputs/capture-tree", picker_actions)
        self.assertIn("{ label, path }", picker_actions)
        self.assertNotIn("root:", picker_actions)
        self.assertNotIn("kind:", picker_actions)
        self.assertNotIn("output_id", picker_actions)

    def test_stop_offers_explicit_choices_for_ready_unsaved_outputs(self) -> None:
        stop = _source_between(
            self.source,
            "function unsavedReadyInterfaceOutputs(",
            "function applyWorkspacePreviewPayload(",
        )

        self.assertIn('output.status === "ready"', stop)
        self.assertIn("!output.kept_workspace_id", stop)
        self.assertIn("openInterfaceStopConfirmation", stop)
        self.assertIn("savePendingInterfaceOutputAndContinueStop", stop)
        self.assertIn("discardPendingInterfaceOutputsAndStop", stop)
        self.assertNotIn("window.confirm", stop)
        self.assertIn(
            "/api/interface-launches/${encodeURIComponent(launch.launch_id)}/stop", stop
        )
        self.assertIn("clearWorkspaceInterfacePreview", stop)
        self.assertIn('id="interfaceStopModal"', self.html)
        self.assertIn("Save as Workspace", self.html)
        self.assertIn("Stop without saving", self.html)
        self.assertIn(">Cancel</button>", self.html)

    def test_launch_status_shows_activity_and_stop_during_preparation(self) -> None:
        status = _source_between(
            self.source,
            "function interfaceLaunchStatus(",
            "function workspaceInterfaceLaunchStatus(",
        )
        compact = _source_between(
            self.source,
            "function compactInterfaceLaunchStatus(",
            "function interfaceLaunchStatus(",
        )
        activity = _source_between(
            self.source,
            "function interfaceLaunchActivityHtml(",
            "function interfaceLaunchStatus(",
        )

        self.assertIn("Current stage", activity)
        self.assertIn("Elapsed", activity)
        self.assertIn("Last activity", activity)
        self.assertIn("launchState.updated_at", activity)
        self.assertIn("canStop", status)
        self.assertIn("component-stop-interface", status)
        self.assertIn('stopping ? "Stopping…" : "Stop interface"', status)
        self.assertIn("View log", compact)

    def test_interface_preview_does_not_mutate_shared_code_server(self) -> None:
        apply_preview = _source_between(
            self.source,
            "function applyWorkspacePreviewPayload(",
            "async function createBlankSession(",
        )

        self.assertIn("preview.url", apply_preview)
        self.assertNotIn("state.codeServer", apply_preview)
        self.assertNotIn("state.embeddedCodeUrl", apply_preview)
        self.assertNotIn("state.embeddedCodeFolder", apply_preview)

    def test_static_validation_truthfully_defers_execution_to_smoke(self) -> None:
        validation = _source_between(
            self.source,
            "function packagePlanValidationHtml(",
            "function packagePlanSmokeHtml(",
        )

        self.assertIn("Workspace code was not executed", validation)
        self.assertIn("Run Test to verify executable behavior", validation)
        self.assertIn("smoke_eligible === false", validation)
        self.assertNotIn("Python imports passed", validation)


if __name__ == "__main__":
    unittest.main()
