"""Focused accessibility contracts for release-critical Studio interactions."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_HTML = _STATIC / "index.html"
_STYLES = _STATIC / "styles.css"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start : source.index(f"function {next_name}(", start)]


class StudioAccessibilityStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.html = _HTML.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_dynamic_run_destinations_are_an_accessible_tab_interface(self) -> None:
        render = _function(self.source, "renderRunDetail", "selectRunActionContext")
        button = _function(self.source, "runTabButtonHtml", "runTabPanelHtml")
        panel = _function(self.source, "runTabPanelHtml", "activateRunTab")

        self.assertIn('role="tablist"', render)
        self.assertIn('aria-label="Run result sections"', render)
        self.assertIn('aria-orientation="horizontal"', render)
        self.assertIn('role="tab"', button)
        self.assertIn('aria-selected="${active ? "true" : "false"}"', button)
        self.assertIn('aria-controls="run-result-tabpanel"', button)
        self.assertIn("const keyboardAnchor", button)
        self.assertIn("const tabSemantics", button)
        self.assertIn('tabindex="${keyboardAnchor ? "0" : "-1"}"', button)
        self.assertIn('role="tabpanel"', panel)
        self.assertIn("aria-labelledby", panel)

    def test_conversation_workspace_access_has_named_controls(self) -> None:
        card = _function(
            self.source,
            "conversationWorkspaceCard",
            "conversationWorkspaceChoice",
        )
        choice = _function(
            self.source,
            "conversationWorkspaceChoice",
            "renderConversationWorkspaceAccess",
        )

        self.assertIn('aria-labelledby="conversationWorkspaceTitle"', self.html)
        self.assertIn('role="heading" aria-level="3"', self.html)
        self.assertIn('aria-label="Open ${escapeHtml(workspace.title)} Workspace"', card)
        self.assertIn('aria-label="Make ${escapeHtml(workspace.title)} the default Workspace for this Conversation"', card)
        self.assertIn('aria-label="Remove ${escapeHtml(workspace.title)} access from', card)
        self.assertIn(">Remove access</button>", card)
        self.assertIn('aria-label="Add ${escapeHtml(workspace.title)} to this Conversation"', choice)

    def test_run_tabs_wrap_with_arrow_keys_and_support_home_and_end(self) -> None:
        handler = _function(
            self.source,
            "handleRunTablistKeydown",
            "runTechnicalTabs",
        )
        activation = _function(
            self.source,
            "activateRunTab",
            "handleRunTablistKeydown",
        )

        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(f'"{key}"', handler)
        self.assertIn("% tabs.length", handler)
        self.assertIn("event.preventDefault()", handler)
        self.assertIn("activateRunTab(next.dataset.runTab", handler)
        self.assertIn("window.requestAnimationFrame", activation)
        self.assertIn("activeTab.focus()", activation)

    def test_interface_stop_dialog_traps_focus_and_restores_its_trigger(self) -> None:
        open_dialog = _function(
            self.source,
            "openInterfaceStopConfirmation",
            "closeInterfaceStopConfirmation",
        )
        close_dialog = _function(
            self.source,
            "closeInterfaceStopConfirmation",
            "handleInterfaceStopConfirmationKeydown",
        )
        keyboard = _function(
            self.source,
            "handleInterfaceStopConfirmationKeydown",
            "savePendingInterfaceOutputAndContinueStop",
        )

        self.assertIn('id="interfaceStopDialog"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn('aria-labelledby="interfaceStopTitle"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.interfaceStopReturnFocus", open_dialog)
        self.assertIn("returnFocus.isConnected", close_dialog)
        self.assertIn("target.focus()", close_dialog)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn('event.key !== "Tab"', keyboard)
        self.assertIn("const activeIsFocusable", keyboard)
        self.assertIn("last.focus()", keyboard)
        self.assertIn("first.focus()", keyboard)
        self.assertIn(
            'on(els.interfaceStopModal, "keydown", handleInterfaceStopConfirmationKeydown);',
            self.source,
        )

    def test_re_evaluation_dialog_traps_focus_and_restores_its_trigger(self) -> None:
        open_dialog = _function(
            self.source,
            "openChildRunConfirmation",
            "closeChildRunConfirmation",
        )
        close_dialog = _function(
            self.source,
            "closeChildRunConfirmation",
            "handleChildRunConfirmationKeydown",
        )
        keyboard = _function(
            self.source,
            "handleChildRunConfirmationKeydown",
            "renderChildRunConfirmation",
        )

        self.assertIn('id="childRunConfirmationDialog"', self.html)
        self.assertIn('aria-describedby="childRunConfirmationIntro"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.childRunReturnFocus", open_dialog)
        self.assertIn("childRunConfirmationSubmitButton.focus()", open_dialog)
        self.assertIn("returnFocus.isConnected", close_dialog)
        self.assertIn("target.focus()", close_dialog)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn("trapModalFocus(", keyboard)
        self.assertIn(
            'on(els.childRunConfirmationModal, "keydown", handleChildRunConfirmationKeydown);',
            self.source,
        )

    def test_open_local_folder_dialog_traps_focus_and_restores_its_trigger(
        self,
    ) -> None:
        open_dialog = _function(
            self.source,
            "openLocalFolderDialog",
            "closeLocalFolderDialog",
        )
        close_dialog = _function(
            self.source,
            "closeLocalFolderDialog",
            "handleLocalFolderDialogKeydown",
        )
        keyboard = _function(
            self.source,
            "handleLocalFolderDialogKeydown",
            "connectLocalFolder",
        )
        focus_trap = _function(
            self.source,
            "trapModalFocus",
            "openChildRunConfirmation",
        )

        self.assertIn('id="openLocalFolderDialog"', self.html)
        self.assertIn('aria-describedby="openLocalFolderIntro"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.localFolderReturnFocus", open_dialog)
        self.assertIn("openLocalFolderPath.focus()", open_dialog)
        self.assertIn("returnFocus.isConnected", close_dialog)
        self.assertIn("els.openLocalFolderButton", close_dialog)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn("trapModalFocus(", keyboard)
        self.assertIn("const activeIsFocusable", focus_trap)
        self.assertIn("element.getClientRects().length > 0", focus_trap)
        self.assertIn("last.focus()", focus_trap)
        self.assertIn("first.focus()", focus_trap)
        self.assertIn(
            'on(els.openLocalFolderModal, "keydown", handleLocalFolderDialogKeydown);',
            self.source,
        )

    def test_settings_opens_before_loading_and_has_retryable_modal_focus(self) -> None:
        open_dialog = _function(
            self.source,
            "openSettings",
            "loadSettingsForModal",
        )
        load = _function(
            self.source,
            "loadSettingsForModal",
            "retrySettingsLoad",
        )
        close_dialog = _function(
            self.source,
            "closeSettings",
            "handleSettingsModalKeydown",
        )
        keyboard = _function(
            self.source,
            "handleSettingsModalKeydown",
            "renderSettingsModal",
        )
        render = _function(
            self.source,
            "renderSettingsModal",
            "fillSettingsForm",
        )

        self.assertIn('id="settingsDialog"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn('id="settingsLoadStatus"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('id="settingsRetryButton"', self.html)
        self.assertLess(
            open_dialog.index("renderSettingsModal()"),
            open_dialog.index("await loadSettingsForModal()"),
        )
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.settingsReturnFocus", open_dialog)
        self.assertIn("state.settingsLoading = true", load)
        self.assertIn("state.settingsError", load)
        self.assertIn("settingsRetryButton.hidden", render)
        self.assertIn("settingsSaveButton.disabled", render)
        self.assertIn("returnFocus.isConnected", close_dialog)
        self.assertIn("target.focus()", close_dialog)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn("trapModalFocus(event, els.settingsModal, els.settingsDialog)", keyboard)
        self.assertIn(
            'on(els.settingsModal, "keydown", handleSettingsModalKeydown);',
            self.source,
        )
        self.assertIn(
            'on(els.settingsRetryButton, "click", retrySettingsLoad);',
            self.source,
        )
        self.assertIn(".settings-load-status", self.styles)
        self.assertIn(".settings-modal button:focus-visible", self.styles)

    def test_workspace_delete_dialog_traps_focus_and_restores_context(self) -> None:
        render = _function(
            self.source,
            "renderWorkspaceCleanupModal",
            "restoreWorkspaceCleanupFocus",
        )
        close_dialog = _function(
            self.source,
            "closeWorkspaceCleanupModal",
            "cancelPendingWorkspaceDelete",
        )
        restore_focus = _function(
            self.source,
            "restoreWorkspaceCleanupFocus",
            "closeWorkspaceCleanupModal",
        )
        keyboard = _function(
            self.source,
            "handleWorkspaceCleanupModalKeydown",
            "deletePendingWorkspaceDraft",
        )
        open_dialog = _function(
            self.source,
            "requestWorkspaceDelete",
            "deleteWorkspaceDraft",
        )
        inert = _function(
            self.source,
            "syncManagedModalBackgroundInert",
            "openSettings",
        )

        self.assertIn('id="workspaceCleanupDialog"', self.html)
        self.assertIn('aria-describedby="workspaceCleanupBody"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn("syncManagedModalBackgroundInert()", render)
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.workspaceCleanupReturnFocus", open_dialog)
        self.assertIn("target.focus()", open_dialog)
        self.assertIn("restoreWorkspaceCleanupFocus(returnFocus)", close_dialog)
        self.assertIn("returnFocus.isConnected", restore_focus)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn(
            "trapModalFocus(event, els.workspaceCleanupModal, els.workspaceCleanupDialog)",
            keyboard,
        )
        self.assertIn('document.querySelector(".app-shell")', inert)
        self.assertIn('appShell.toggleAttribute(', inert)
        self.assertIn(
            'on(els.workspaceCleanupModal, "keydown", handleWorkspaceCleanupModalKeydown);',
            self.source,
        )

    def test_candidate_try_dialog_traps_focus_and_restores_cancelled_focus(
        self,
    ) -> None:
        open_dialog = _function(
            self.source,
            "startCandidateTry",
            "renderCandidateTrySheet",
        )
        close_dialog = _function(
            self.source,
            "closeCandidateTrySheet",
            "confirmCandidateTry",
        )
        keyboard = _function(
            self.source,
            "handleCandidateTrySheetKeydown",
            "trapModalFocus",
        )

        self.assertIn('id="candidateTryDialog"', self.html)
        self.assertIn('aria-describedby="candidateTryIntro"', self.html)
        self.assertIn('tabindex="-1"', self.html)
        self.assertIn("document.activeElement", open_dialog)
        self.assertIn("state.candidateTryReturnFocus", open_dialog)
        self.assertIn("document.contains(target)", close_dialog)
        self.assertIn("target.focus()", close_dialog)
        self.assertIn('event.key === "Escape"', keyboard)
        self.assertIn(
            "trapModalFocus(event, els.candidateTryModal, els.candidateTryDialog)",
            keyboard,
        )
        self.assertNotIn("querySelectorAll", keyboard)

    def test_unavailable_candidate_try_is_an_informational_dialog(self) -> None:
        render = _function(
            self.source,
            "renderCandidateTrySheet",
            "updateCandidateTrySheet",
        )
        candidate_dialog = self.html[
            self.html.index('id="candidateTryModal"') : self.html.index(
                'id="childRunConfirmationModal"'
            )
        ]

        self.assertIn('"Candidate cannot be tried"', render)
        self.assertIn("The reasons are shown below", render)
        self.assertIn("candidateTrySubmitButton.hidden = !hasEligibleMode", render)
        self.assertIn("candidateTryCancelButton.hidden = !hasEligibleMode", render)
        self.assertIn("candidateTryActions.hidden = !hasEligibleMode", render)
        self.assertIn("Close unavailable Candidate explanation", render)
        self.assertIn('id="candidateTryActions"', candidate_dialog)

    def test_focused_candidate_routes_restore_keyboard_focus(self) -> None:
        binding = _function(
            self.source,
            "bindWorkbenchEntityActions",
            "updateReviewDraftTitle",
        )

        self.assertIn('querySelector("[data-clear-candidate-route]")', binding)
        self.assertIn("focusedBack.focus()", binding)
        self.assertIn('querySelectorAll("[data-open-candidate-route]")', binding)
        self.assertIn("control.dataset.openCandidateRoute === candidateId", binding)
        self.assertIn("target.focus()", binding)

    def test_candidate_try_rerenders_focus_status_retry_or_created_job(self) -> None:
        restore = _function(
            self.source,
            "restoreFocusedCandidateTryFocus",
            "handleCandidateTrySheetKeydown",
        )
        action = _function(
            self.source,
            "performWorkbenchAction",
            "renderCandidateInspection",
        )
        panel_refresh = _function(
            self.source,
            "renderOperatorJobsPanel",
            "operatorJobsPanelBody",
        )

        self.assertIn("[data-candidate-try-status]", restore)
        self.assertIn("[data-try-candidate]", restore)
        self.assertIn("[data-operator-job-id]", restore)
        self.assertIn("target.focus()", restore)
        self.assertIn('restoreFocusedCandidateTryFocus(selectionId, "status")', action)
        self.assertIn('createdOperatorJobId ? "job" : "retry"', action)
        self.assertIn("document.activeElement.closest", panel_refresh)
        self.assertIn("focusedJobId", panel_refresh)
        self.assertIn("target.focus()", panel_refresh)
        self.assertIn(".candidate-focused-action-help:focus", self.styles)

    def test_new_keyboard_targets_have_visible_focus_treatment(self) -> None:
        self.assertIn('.tabs [role="tab"]:focus-visible', self.styles)
        self.assertIn(".interface-stop-modal button:focus-visible", self.styles)
        self.assertIn(".child-run-confirmation-modal button:focus-visible", self.styles)
        self.assertIn(".cleanup-modal input:focus-visible", self.styles)
        self.assertIn("outline-offset", self.styles)

    def test_assistant_composer_and_timeline_have_bounded_live_semantics(self) -> None:
        render = _function(self.source, "renderAssistant", "queueAssistantStepAutoScroll")

        self.assertIn(
            'id="agentInput" spellcheck="false" aria-label="Message OptPilot"',
            self.html,
        )
        timeline_start = self.html.index('id="agentTimeline"')
        timeline_end = self.html.index("</div>", timeline_start)
        timeline = self.html[timeline_start:timeline_end]
        self.assertIn('role="log"', timeline)
        self.assertIn('aria-label="Conversation history"', timeline)
        self.assertIn('aria-live="polite"', timeline)
        self.assertIn('aria-relevant="additions"', timeline)
        self.assertIn('aria-atomic="false"', timeline)
        self.assertIn("timelineSessionChanged", render)
        self.assertIn('setAttribute("aria-live", "off")', render)
        self.assertIn('setAttribute("aria-live", "polite")', render)

    def test_local_environment_settings_do_not_imply_a_secret_vault(self) -> None:
        self.assertIn("Local environment variables", self.html)
        self.assertIn("stores them as plaintext", self.html)
        self.assertIn("This is not a secret vault", self.html)
        self.assertNotIn("Environment &amp; Secrets", self.html)


if __name__ == "__main__":
    unittest.main()
