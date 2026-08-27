"""A recoverable slip does not end the conversation, and does not happen here.

The assistant called optpilot_package_plan_validate without plan_id. The
agent-server marked that AgentErrorEvent {"kind": "agent_action",
"retryable": true} and handed it back to the model -- the conversation went
on and finished normally. Studio's error extractor sniffed the event's
`error` field anyway and reported the whole turn as failed, so a slip the
system had already absorbed was shown to the person as a dead session.

The cause is removed as well: a plan id is deterministic in (actor,
workspace), so requiring the model to repeat one only invented a way to get
the call wrong. It defaults to the Workspace's own plan.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.agent import OpenHandsAdapter, OpenHandsRuntimeConfig
from optpilot_studio.ui.server import (
    UiState,
    _agent_package_plan_id,
    _package_plan_id_for_workspace,
)

RETRYABLE = {
    "kind": "AgentErrorEvent",
    "tool_name": "optpilot_package_plan_validate",
    "error": (
        "Error validating tool 'optpilot_package_plan_validate': 1 validation "
        "error for ClientAction_optpilot_package_plan_validate plan_id Field required"
    ),
    "classification": {"kind": "agent_action", "retryable": True},
}
FATAL = {
    "kind": "ConversationErrorEvent",
    "code": "LLMAuthenticationError",
    "detail": "OpenrouterException 401",
    "classification": {"kind": "auth", "retryable": False},
}


class RetryableErrorsDoNotFailTheTurnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())

    def test_a_retryable_step_error_is_not_the_turn_outcome(self) -> None:
        self.assertEqual(self.adapter._event_runtime_error_text(RETRYABLE), "")
        self.assertEqual(self.adapter._best_runtime_error([RETRYABLE], set()), "")

    def test_a_fatal_error_still_surfaces(self) -> None:
        self.assertIn(
            "OpenrouterException", self.adapter._event_runtime_error_text(FATAL)
        )

    def test_a_retryable_event_beside_a_fatal_one_does_not_mask_it(self) -> None:
        self.assertIn(
            "OpenrouterException",
            self.adapter._best_runtime_error([RETRYABLE, FATAL], set()),
        )


class PlanIdDefaultsToTheWorkspacePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = UiState(
            cwd=Path(temporary.name), catalog_roots=[], run_roots=[]
        )
        self.addCleanup(self.state.close_coordination)

    def test_an_omitted_plan_id_resolves_to_the_workspace_plan(self) -> None:
        expected = _package_plan_id_for_workspace(self.state, "ws_1")
        self.assertTrue(expected.startswith("pkg_plan_"))
        self.assertEqual(
            _agent_package_plan_id(self.state, {"workspace_id": "ws_1"}), expected
        )

    def test_a_named_plan_id_still_wins(self) -> None:
        self.assertEqual(
            _agent_package_plan_id(
                self.state, {"workspace_id": "ws_1", "plan_id": "pkg_plan_named"}
            ),
            "pkg_plan_named",
        )

    def test_distinct_workspaces_get_distinct_plans(self) -> None:
        self.assertNotEqual(
            _package_plan_id_for_workspace(self.state, "ws_1"),
            _package_plan_id_for_workspace(self.state, "ws_2"),
        )

    def test_no_workspace_yields_no_plan(self) -> None:
        self.assertEqual(_agent_package_plan_id(self.state, {}), "")


class PlanIdIsNotRequiredByTheToolsTest(unittest.TestCase):
    def test_the_package_plan_tools_no_longer_demand_a_plan_id(self) -> None:
        from optpilot_studio.agent import OPTPILOT_AGENT_TOOL_SPECS

        for spec in OPTPILOT_AGENT_TOOL_SPECS:
            name = spec.get("name", "")
            if not name.startswith("optpilot_package_plan_"):
                continue
            required = (spec.get("parameters") or {}).get("required") or []
            self.assertNotIn("plan_id", required, name)


if __name__ == "__main__":
    unittest.main()
