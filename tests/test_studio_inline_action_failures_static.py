"""Browser contracts for inline Catalog, Study, and Setup action feedback."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_STYLES = (
    _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "styles.css"
)


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


class StudioInlineActionFailuresStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_catalog_source_open_is_pending_guarded_and_fails_inline(self) -> None:
        configured = _between(
            self.source,
            "function renderConfiguredCatalogSources(",
            "async function openConfiguredCatalogSourceWorkspace(",
        )
        action = _between(
            self.source,
            "async function openComponentSession(",
            "async function resumeStoredInterfaceLaunch(",
        )
        status = _between(
            self.source,
            "function catalogComponentActionStatus(",
            "function renderComponentDetail(",
        )
        detail = _between(
            self.source,
            "function renderComponentDetail(",
            "function componentEditableWorkspaceCapability(",
        )

        self.assertIn("if (currentAction && currentAction.pending) return", action)
        self.assertLess(
            action.index("pending: true"),
            action.index("await postJson"),
        )
        self.assertIn("pending: false", action)
        self.assertIn("boundedPublicActionError", action)
        self.assertNotIn("pushAssistantMessage", action)
        self.assertNotIn("setAssistantOpen", action)
        self.assertIn('role="status"', status)
        self.assertIn('role="alert"', status)
        self.assertIn("Use the action above to try again", status)
        self.assertIn("Try opening source again", detail)
        self.assertIn("Try opening Workspace again", detail)
        self.assertIn("Try opening again", configured)
        self.assertIn('role="alert"', configured)

    def test_catalog_interface_failure_stays_inline_and_is_retryable(self) -> None:
        launch = _between(
            self.source,
            "async function launchComponentInterface(",
            "async function pollComponentInterfaceLaunch(",
        )
        status = _between(
            self.source,
            "function interfaceLaunchStatus(",
            "function workspaceInterfaceLaunchStatus(",
        )
        compact = _between(
            self.source,
            "function compactInterfaceLaunchStatus(",
            "function interfaceLaunchStatus(",
        )
        stop = _between(
            self.source,
            "async function performInterfaceStop(",
            "async function stopWorkspaceInterface(",
        )

        self.assertIn("retryingFailedLaunch", launch)
        self.assertIn('status: "failed"', launch)
        self.assertIn("boundedPublicActionError", launch)
        self.assertNotIn("pushAssistantMessage", launch)
        self.assertIn("launchState.error", status)
        self.assertIn("launchState.error_detail", status)
        self.assertIn("Last process error", compact)
        self.assertIn('class="interface-launch-visible-errors" role="alert"', compact)
        self.assertLess(
            compact.index("interface-launch-visible-errors"),
            compact.index('data-interface-drawer-panel="details"'),
        )
        self.assertIn("failed || cleanupPending", compact)
        self.assertIn("stop_error", stop)
        self.assertNotIn("pushAssistantMessage", stop)

    def test_study_save_and_preflight_are_single_submit_inline_actions(self) -> None:
        render = _between(
            self.source,
            "function renderPlanDetail(",
            "function studyLaunchForPlan(",
        )
        status = _between(
            self.source,
            "function renderStudyActionStatus(",
            "function renderPlanDetail(",
        )
        save = _between(
            self.source,
            "async function generatePlanDraft(",
            "async function savePlanDraft(",
        )
        save_request = _between(
            self.source,
            "async function savePlanDraft(",
            "async function refreshStudyDraftWorkspace(",
        )
        foreground_save = _between(
            self.source,
            "async function savePlanDraft(",
            "function reconcileStudyDraftAfterSave(",
        )
        launch = _between(
            self.source,
            "async function launchPlan(",
            "function persistActiveStudyLaunch(",
        )
        edits = _between(
            self.source,
            "function updatePlanField(",
            "function convertSavedPlanToDraft(",
        )

        self.assertIn("plan.savePending || plan.launchPending", save)
        self.assertIn("plan.savePending = true", save)
        self.assertIn("finally", save)
        self.assertIn("plan.savePending = false", save)
        self.assertIn("setStudyActionError", save_request)
        self.assertNotIn("pushAssistantMessage", save_request)
        self.assertNotIn("await loadStudyDrafts()", foreground_save)
        self.assertIn("reconcileStudyDraftAfterSave(result)", foreground_save)
        self.assertIn("Promise.allSettled", save_request)
        self.assertIn("plan.launchPending", launch)
        self.assertIn("failLaunch", launch)
        self.assertNotIn("pushAssistantMessage", launch)
        self.assertIn("plan.draftSaveRequestId = null", edits)
        self.assertIn("plan.launchPreparationRequestId = null", edits)
        self.assertIn("plan.draftActionId = null", edits)
        self.assertIn("actionPending", render)
        self.assertIn("saveDisabled", render)
        self.assertIn("Try saving again", render)
        self.assertIn('role="status"', status)
        self.assertIn('role="alert"', status)

    def test_setup_check_and_test_disable_reentry_until_finally(self) -> None:
        hierarchy = _between(
            self.source,
            "function registrationActionHierarchyHtml(",
            "function planCanApply(",
        )
        bindings = _between(
            self.source,
            "function bindRegistrationMenu(",
            "function findPackagePlanTarget(",
        )

        self.assertIn('pendingAction === "check"', hierarchy)
        self.assertIn('pendingAction === "test"', hierarchy)
        self.assertIn("Checking files…", hierarchy)
        self.assertIn("Testing…", hierarchy)
        self.assertIn("Try checking again", hierarchy)
        self.assertIn("Try required test again", hierarchy)
        self.assertIn("state.registrationActionPending", bindings)
        self.assertIn('state.registrationActionPending = "check"', bindings)
        self.assertIn('state.registrationActionPending = "test"', bindings)
        self.assertGreaterEqual(bindings.count("} finally {"), 2)
        self.assertGreaterEqual(
            bindings.count('state.registrationActionPending = "";'),
            2,
        )

    def test_inline_status_surfaces_have_error_styling(self) -> None:
        for selector in (
            ".component-action-status",
            ".component-action-status.component-action-failed",
            ".interface-launch-status.interface-launch-failed",
            ".study-action-status",
            ".study-action-status.study-action-failed",
        ):
            self.assertIn(selector, self.styles)


if __name__ == "__main__":
    unittest.main()
