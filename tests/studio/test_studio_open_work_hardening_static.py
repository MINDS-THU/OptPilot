"""Identity, scope, failure-retention, and focus contracts for Open work."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
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


class StudioOpenWorkHardeningStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_interface_card_opens_or_resolves_its_exact_launch(self) -> None:
        dispatch = _function_source(self.source, "openOpenWorkItem")
        exact = _function_source(self.source, "openExactOpenWorkInterface")

        self.assertIn("openExactOpenWorkInterface(item)", dispatch)
        self.assertIn("String(current.launch_id || \"\") === launchId", exact)
        self.assertIn(
            "/api/interface-launches/${encodeURIComponent(launchId)}", exact
        )
        self.assertIn("String(launch.launch_id || \"\") !== launchId", exact)
        self.assertIn("openLaunchInterfaceSession(exactLaunch)", exact)
        self.assertNotIn("openLaunchInterfaceSession(state.interfaceLaunch)", exact)

    def test_cards_name_the_process_type_and_exclude_saved_objects(self) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        renderer = _function_source(self.source, "renderOpenWork")

        for type_label in ('typeLabel: "Interface"', 'typeLabel: failed ? "Run setup" : "Run setup · preparing"', 'typeLabel: "Run"', 'typeLabel: "Approval"'):
            self.assertIn(type_label, projection)
        # Saved objects stay out. Conversations appear only through their
        # pending-approval affordance (U3), never as conversation cards.
        for saved_collection in ("state.sessions", "state.plans"):
            self.assertNotIn(saved_collection, projection)
        self.assertIn("const activeCount", renderer)
        self.assertIn("item.typeLabel", renderer)
        self.assertIn("No open work", renderer)

    def test_study_launch_card_resolves_identity_and_preserves_run_handoff(self) -> None:
        dispatch = _function_source(self.source, "openOpenWorkItem")
        exact = _function_source(self.source, "openExactOpenWorkStudyLaunch")

        self.assertIn("openExactOpenWorkStudyLaunch(item)", dispatch)
        self.assertIn(
            "/api/studies/launches/${encodeURIComponent(launchId)}", exact
        )
        self.assertIn("String(launch.launch_id || \"\") !== launchId", exact)
        self.assertIn("launch && launch.run_id", exact)
        self.assertIn('openContentSurface("runs"', exact)
        self.assertIn("loadRunDetail(runId", exact)

    def test_missing_study_target_becomes_visible_attention_instead_of_a_noop(self) -> None:
        exact = _function_source(self.source, "openExactOpenWorkStudyLaunch")

        self.assertNotIn("if (!planId || !plan) return", exact)
        self.assertIn("if (!planId)", exact)
        self.assertIn("if (!plan)", exact)
        self.assertIn("no longer identifies its Run setup", exact)
        self.assertIn("no longer available", exact)
        self.assertIn("state.openWorkErrors[itemKey]", exact)
        self.assertIn("renderOpenWork()", exact)

    def test_failed_interface_is_retained_as_non_active_attention_work(self) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        persistence = _function_source(self.source, "persistActiveInterfaceLaunch")
        active = _function_source(self.source, "isActiveInterfaceLaunch")

        self.assertIn('interfaceLaunchStatus === "failed"', projection)
        self.assertIn('"cleanup_pending", "failed"', projection)
        self.assertIn("active: !interfaceLaunchFailed", projection)
        self.assertIn("dismissible: interfaceLaunchFailed", projection)
        self.assertIn(
            "actionable: Boolean(launch.launch_id || launch.key || launch.source_workspace_id)",
            projection,
        )
        self.assertIn('status: String(launch.status || "")', persistence)
        self.assertIn('error: String(launch.error || "")', persistence)
        self.assertIn('!["failed", "stopped"].includes', active)

    def test_failed_interface_card_can_return_to_source_or_be_dismissed(self) -> None:
        renderer = _function_source(self.source, "renderOpenWork")
        dismiss = _function_source(self.source, "dismissOpenWorkItem")
        exact = _function_source(self.source, "openExactOpenWorkInterface")

        self.assertIn("data-dismiss-open-work-key", renderer)
        self.assertIn("Return to source", renderer)
        self.assertIn("dismissOpenWorkItem", renderer)
        self.assertIn(
            '!["failed", "stopped"].includes(String(item.status || ""))', dismiss
        )
        self.assertIn("state.interfaceLaunch = null", dismiss)
        self.assertIn("persistActiveInterfaceLaunch(null)", dismiss)
        self.assertIn("delete state.openWorkErrors[item.key]", dismiss)
        self.assertIn('String(item && item.status || "") === "failed"', exact)
        self.assertIn("await openFailedInterfaceSource(item, current)", exact)

    def test_failed_interface_without_launch_id_reopens_its_exact_source(self) -> None:
        exact = _function_source(self.source, "openExactOpenWorkInterface")
        source = _function_source(self.source, "openFailedInterfaceSource")

        self.assertIn("await openFailedInterfaceSource(item, current)", exact)
        self.assertIn("state.openWorkErrors[itemKey]", exact)
        self.assertIn("workspaceSessionByBackendId(sourceWorkspaceId)", source)
        self.assertIn(
            "/api/workspaces/${encodeURIComponent(sourceWorkspaceId)}", source
        )
        self.assertIn("catalogSourceComponentByKey(launchKey)", source)
        self.assertIn(
            'openComponentSession(component, "inspect", { workbenchMode: "preview" })',
            source,
        )
        self.assertNotIn("openActiveInterfaceLocation()", exact)

    def test_failed_launch_persistence_does_not_clear_the_inspectable_coordinate(self) -> None:
        resume = _function_source(self.source, "resumeStoredInterfaceLaunch")
        component_launch = _function_source(self.source, "launchComponentInterface")
        workspace_launch = _function_source(self.source, "launchWorkspaceInterface")
        failure = _function_source(self.source, "handleInterfaceLaunchPollingError")

        self.assertIn("persistActiveInterfaceLaunch(state.interfaceLaunch)", resume)
        self.assertIn("handleInterfaceLaunchPollingError", component_launch)
        self.assertIn("handleInterfaceLaunchPollingError", workspace_launch)
        self.assertIn("persistActiveInterfaceLaunch(state.interfaceLaunch)", failure)
        self.assertIn("retryingFailedLaunch", component_launch)
        self.assertIn("retryingFailedLaunch", workspace_launch)

    def test_status_rerenders_restore_the_focused_card_when_it_still_exists(self) -> None:
        renderer = _function_source(self.source, "renderOpenWork")

        self.assertIn("focusedKey", renderer)
        self.assertIn("requestAnimationFrame", renderer)
        self.assertIn("openWorkSignature !== signature", renderer)
        self.assertIn("button.dataset.openWorkKey === focusedKey", renderer)
        self.assertIn("replacement.focus({ preventScroll: true })", renderer)

    def test_stale_study_launch_recovery_is_bounded_and_actionable(self) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        persistence = _function_source(self.source, "persistActiveStudyLaunch")
        reconnect = _function_source(self.source, "handleStudyLaunchReconnectFailure")
        failure = _function_source(self.source, "failStudyLaunchRecovery")
        renderer = _function_source(self.source, "renderStudyLaunchStatus")
        dismiss = _function_source(self.source, "dismissActiveStudyLaunch")
        open_work_dismiss = _function_source(self.source, "dismissOpenWorkItem")
        get_json = _function_source(self.source, "getJson")

        self.assertIn("dismissible: failed", projection)
        self.assertIn("reconnectAttempts", persistence)
        self.assertIn("STUDY_LAUNCH_RECONNECT_LIMIT", reconnect)
        self.assertIn("[404, 410].includes(status)", reconnect)
        self.assertIn("Launch the Run setup again", failure)
        self.assertIn("study-launch-dismiss", renderer)
        self.assertIn("persistActiveStudyLaunch(null)", dismiss)
        self.assertIn('item.kind === "study-launch"', open_work_dismiss)
        self.assertIn("dismissActiveStudyLaunch()", open_work_dismiss)
        self.assertIn("error.status = response.status", get_json)

    def test_pending_approvals_surface_with_a_path_back_to_the_conversation(
        self,
    ) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        opener = _function_source(self.source, "openOpenWorkItem")
        refresher = _function_source(
            self.source, "updateAgentSessionFromPayload"
        )

        self.assertIn(
            "const pendingCount = Number(session.pending_approval_count || 0);",
            projection,
        )
        # pending_approval_count already covers the queued tail; adding
        # queued_approval_count to it double-counts. See
        # tests/studio/test_studio_open_work_approval_counts.py.
        self.assertNotIn("+ Number(session.queued_approval_count", projection)
        self.assertIn('=== "awaiting_user_approval"', projection)
        self.assertIn("key: `approval:${session.id}`", projection)
        self.assertIn('section: "Needs attention"', projection)
        # Approval cards route to the owning Conversation, never a new one.
        self.assertIn('item.kind === "approval"', opener)
        self.assertIn("selectAgentSession(item.session_id)", opener)
        # Session summary polling keeps the shelf current.
        self.assertIn("renderOpenWork()", refresher)

    def test_finished_interface_with_outputs_keeps_a_path_to_kept_outputs(
        self,
    ) -> None:
        projection = _function_source(self.source, "buildOpenWorkItems")
        attention = _function_source(
            self.source, "interfaceOutputsNeedAttention"
        )
        outputs_renderer = _function_source(
            self.source, "renderInterfaceSessionOutputs"
        )

        self.assertIn(
            'interfaceLaunchStatus === "stopped" && launchOutputs.length > 0',
            projection,
        )
        self.assertIn('"Finished"', projection)
        self.assertIn(
            "dismissible: interfaceLaunchFailed || interfaceLaunchFinishedWithOutputs",
            projection,
        )
        self.assertIn("interfaceOutputsNeedAttention(launchOutputs)", projection)
        # One shared attention predicate for the drawer and the shelf.
        self.assertIn("keep_as_workspace", attention)
        self.assertIn("kept_workspace_id", attention)
        self.assertIn(
            "interfaceOutputsNeedAttention(outputs)", outputs_renderer
        )
        # Finished section sorts after live work.
        self.assertIn(
            'sectionOrder = { "Needs attention": 0, Running: 1, Finished: 2 }',
            projection,
        )


if __name__ == "__main__":
    unittest.main()
