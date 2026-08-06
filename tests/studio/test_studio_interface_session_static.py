"""Browser-client contracts for the shared full-page Interface Session view."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP_JS = _STATIC / "app.js"
_INDEX_HTML = _STATIC / "index.html"
_STYLES = _STATIC / "styles.css"


def _function_source(source: str, name: str) -> str:
    """Return one top-level JavaScript function without coupling to its successor."""

    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(",
        source,
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioInterfaceSessionStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")
        cls.html = _INDEX_HTML.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_candidate_interface_route_carries_exact_run_candidate_and_job(self) -> None:
        parser = _function_source(self.source, "parseStudioRoute")
        serializer = _function_source(self.source, "studioRouteHash")
        application = _function_source(self.source, "applyStudioRoute")

        self.assertIn('page === "interfaces"', parser)
        self.assertIn('kind === "candidate"', parser)
        self.assertIn('view: "interface"', parser)
        self.assertIn("if (!parts[2] || !parts[3] || !parts[4]) return null;", parser)
        for coordinate in ("runId", "candidateId", "jobId"):
            self.assertIn(coordinate, parser)
            self.assertIn(coordinate, serializer)
        self.assertIn("#/interfaces/candidate/", serializer)
        self.assertIn("state.interfaceSessionRoute", serializer)
        self.assertIn("state.interfaceSessionRoute", application)
        self.assertIn("loadOperatorJobDetail", application)

    def test_candidate_route_never_serializes_presentation_credentials(self) -> None:
        parser = _function_source(self.source, "parseStudioRoute")
        serializer = _function_source(self.source, "studioRouteHash")
        candidate = _function_source(self.source, "candidateInterfaceSessionModel")
        catalog_persistence = _function_source(
            self.source,
            "persistActiveInterfaceLaunch",
        )

        for routing_source in (parser, serializer):
            self.assertNotIn("open_url", routing_source)
            self.assertNotIn("presentation", routing_source)
        self.assertIn("presentation.open_url", candidate)
        self.assertIn("state.selectedOperatorJob", candidate)
        self.assertIn('String(job.job_id || "") !== jobId', candidate)
        self.assertIn("targetRunId !== runId", candidate)
        self.assertIn("targetCandidateId !== candidateId", candidate)
        self.assertNotIn("sessionStorage", candidate)
        self.assertNotIn("localStorage", candidate)
        # Catalog/Workspace launch recovery remains independently persisted;
        # a Candidate's authenticated presentation URL must never enter it.
        self.assertNotIn("selectedOperatorJob", catalog_persistence)
        self.assertNotIn("interfaceSessionRoute", catalog_persistence)

    def test_shared_interface_uses_one_stable_iframe_host(self) -> None:
        renderer = _function_source(self.source, "renderInterfaceSession")

        self.assertEqual(self.html.count('id="interfaceSessionFrame"'), 1)
        self.assertIn("els.interfaceSessionFrame", renderer)
        self.assertIn('getAttribute("src")', renderer)
        self.assertIn("model.openUrl", renderer)
        self.assertIn("dataset.sessionKey", renderer)
        self.assertIn("retainCurrentFrame", renderer)
        self.assertIn('["failed", "stopped", "cleanup_pending"]', renderer)
        self.assertNotIn("<iframe", renderer)
        self.assertNotIn("els.interfaceSessionView.innerHTML", renderer)
        self.assertNotIn("els.interfaceSessionFrame.outerHTML", renderer)

    def test_full_stage_interface_has_an_accessible_nonmodal_outputs_panel(self) -> None:
        self.assertEqual(self.html.count('id="interfaceSessionOutputsButton"'), 1)
        self.assertIn('aria-controls="interfaceSessionOutputsDrawer"', self.html)
        self.assertNotIn('aria-haspopup="dialog"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('id="interfaceSessionOutputsCount"', self.html)
        self.assertIn('id="interfaceSessionOutputsScrim"', self.html)
        self.assertIn('id="interfaceSessionOutputsDrawer"', self.html)
        outputs_panel = self.html.split('id="interfaceSessionOutputsDrawer"', 1)[1].split("</aside>", 1)[0]
        self.assertIn('role="complementary"', outputs_panel)
        self.assertNotIn('aria-modal="true"', outputs_panel)
        self.assertIn('aria-labelledby="interfaceSessionOutputsTitle"', self.html)
        self.assertIn('id="interfaceSessionOutputsCloseButton"', self.html)
        self.assertIn('aria-label="Close Outputs"', self.html)
        self.assertIn('id="interfaceSessionOutputsBody"', self.html)
        self.assertIn("Save as Workspace", self.html)

    def test_outputs_drawer_overlays_the_interface_and_is_responsive(self) -> None:
        self.assertIn(".interface-session-outputs-scrim {", self.styles)
        self.assertIn(".interface-session-outputs-drawer {", self.styles)
        drawer_start = self.styles.index(".interface-session-outputs-drawer {")
        drawer_end = self.styles.index("}", drawer_start)
        drawer = self.styles[drawer_start:drawer_end]
        self.assertIn("position: absolute", drawer)
        self.assertIn("inset-block: 0", drawer)
        self.assertIn("inset-inline-end: 0", drawer)
        self.assertIn("width: min(440px, calc(100% - 48px))", drawer)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", drawer)
        self.assertIn(".interface-session-outputs-body {", self.styles)
        self.assertIn("overflow: auto", self.styles)
        responsive = self.styles[self.styles.index("@media (max-width: 760px)", drawer_end):]
        self.assertIn(".interface-session-outputs-drawer {", responsive)
        self.assertIn("inset-block: auto 0", responsive)
        self.assertIn("width: 100%", responsive)
        self.assertIn("height: min(72%, 680px)", responsive)

    def test_candidate_iframe_has_bounded_navigation_and_referrer_policy(self) -> None:
        frame_match = re.search(
            r'<iframe\b[^>]*\bid="interfaceSessionFrame"[^>]*>',
            self.html,
        )
        self.assertIsNotNone(frame_match)
        frame = frame_match.group(0)
        self.assertIn('loading="eager"', frame)
        self.assertIn('referrerpolicy="no-referrer"', frame)
        sandbox_match = re.search(r'\bsandbox="([^"]*)"', frame)
        self.assertIsNotNone(sandbox_match)
        self.assertEqual(
            set(sandbox_match.group(1).split()),
            {
                "allow-downloads",
                "allow-forms",
                "allow-modals",
                "allow-popups",
                "allow-same-origin",
                "allow-scripts",
            },
        )
        self.assertNotIn("allow-top-navigation", frame)

    def test_candidate_and_catalog_sessions_keep_separate_lifecycle_actions(self) -> None:
        candidate = _function_source(self.source, "candidateInterfaceSessionModel")
        launch = _function_source(self.source, "launchInterfaceSessionModel")

        self.assertIn("stopOperatorJob", candidate)
        self.assertNotIn("stopInterfaceLaunch", candidate)
        self.assertIn("stopInterfaceLaunch", launch)
        self.assertNotIn("stopOperatorJob", launch)
        self.assertIn("state.selectedOperatorJob", candidate)
        self.assertIn("state.interfaceLaunch", launch)

    def test_catalog_and_workspace_launches_open_the_shared_interface_view(self) -> None:
        active_bar = _function_source(self.source, "openActiveInterfaceLocation")
        catalog_launch = _function_source(self.source, "launchComponentInterface")
        workspace_launch = _function_source(self.source, "launchWorkspaceInterface")

        self.assertIn("openLaunchInterfaceSession(launch)", active_bar)
        self.assertIn("openLaunchInterfaceSession(state.interfaceLaunch)", catalog_launch)
        self.assertIn("openLaunchInterfaceSession(state.interfaceLaunch)", workspace_launch)

    def test_open_interface_does_not_create_a_hidden_source_workspace(self) -> None:
        action = _function_source(self.source, "openComponentInterface")

        self.assertIn("launchComponentInterface(component)", action)
        self.assertNotIn("openComponentSession", action)
        self.assertIn("openLaunchInterfaceSession(launch)", action)
        self.assertIn("componentSelectedInterfaceProfile(component)", action)
        self.assertIn("capability.reason", action)

    def test_catalog_interface_action_disables_unavailable_profiles_and_returns_to_active_work(self) -> None:
        detail = _function_source(self.source, "renderComponentDetail")
        action = _function_source(self.source, "openComponentInterface")
        launch = _function_source(self.source, "launchComponentInterface")

        self.assertIn("interfaceCapability.eligible !== true", detail)
        self.assertIn("const interfaceDisabled =", detail)
        self.assertIn('disabled aria-disabled="true"', detail)
        self.assertIn('"Interface unavailable"', detail)
        self.assertIn("`Return to ${otherInterfaceLaunch.label", detail)
        self.assertIn("if (isActiveInterfaceLaunch(launch))", action)
        self.assertIn("openLaunchInterfaceSession(launch)", action)
        self.assertIn("state.catalogComponentActions", action)
        self.assertIn("capability && capability.reason", action)
        self.assertIn("openLaunchInterfaceSession(previousLaunch)", launch)
        self.assertIn("capability.reason", launch)

    def test_interface_outputs_return_to_the_originating_conversation(self) -> None:
        catalog_launch = _function_source(self.source, "launchComponentInterface")
        workspace_launch = _function_source(self.source, "launchWorkspaceInterface")
        persistence = _function_source(self.source, "persistActiveInterfaceLaunch")
        keep = _function_source(self.source, "keepInterfaceOutput")
        card = _function_source(self.source, "renderInterfaceOutputCard")

        for launch in (catalog_launch, workspace_launch):
            self.assertIn("origin_conversation_id", launch)
            self.assertIn("origin_conversation_title", launch)
        self.assertIn("origin_conversation_id", persistence)
        self.assertIn("origin_conversation_title", persistence)
        self.assertIn("attachWorkspaceToAgentSession", keep)
        self.assertIn("kept_conversation_title", keep)
        self.assertIn("Made available to the originating Conversation", card)
        self.assertIn("originating Conversation", card)

    def test_stopping_protects_outputs_that_are_still_being_prepared(self) -> None:
        at_risk = _function_source(self.source, "interfaceOutputsAtRisk")
        stop = _function_source(self.source, "stopInterfaceLaunch")
        confirmation = _function_source(self.source, "renderInterfaceStopConfirmation")

        self.assertIn("unsavedReadyInterfaceOutputs", at_risk)
        self.assertIn("sealingInterfaceOutputs", at_risk)
        self.assertIn("interfaceOutputsAtRisk(launch)", stop)
        self.assertIn("still preparing", confirmation)
        self.assertIn("Keep running", confirmation)
        self.assertIn("Stop anyway", confirmation)

    def test_interactive_try_opens_shared_view_and_keeps_compact_history(self) -> None:
        action = _function_source(self.source, "performWorkbenchAction")
        focused = _function_source(self.source, "renderFocusedCandidatePage")
        history = _function_source(self.source, "operatorJobsPanelBody")
        history_row = _function_source(self.source, "renderOperatorJobRow")
        history_presentation = _function_source(
            self.source,
            "renderOperatorJobInterfaceAction",
        )
        candidate = _function_source(self.source, "candidateInterfaceSessionModel")

        self.assertIn('actionName === "environment_preview"', action)
        self.assertIn("interfaceSessionRoute", action)
        self.assertIn("job.job_id", action)
        self.assertIn("syncStudioRoute", action)
        self.assertIn("operatorJobsSection(", focused)
        self.assertIn("Candidate tries", history)
        self.assertIn("data-operator-job-id", history_row)
        self.assertIn("data-open-operator-interface", history_presentation)
        self.assertNotIn("<iframe", history_presentation)
        self.assertIn("#/runs/", candidate)
        self.assertIn("/candidates/", candidate)


if __name__ == "__main__":
    unittest.main()
