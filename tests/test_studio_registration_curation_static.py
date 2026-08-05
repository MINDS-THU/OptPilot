"""Browser-client contracts for the generic workspace curation handoff."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP = (
    Path(__file__).resolve().parents[1]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


class StudioRegistrationCurationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_configless_workspace_offers_deterministic_roles_and_optional_assistant(self) -> None:
        resource_panel = _function_source(
            self.source, "resourceRegistrationHtml", "registrationStep"
        )
        binding = _function_source(
            self.source, "bindRegistrationMenu", "requestWorkspaceCuration"
        )

        self.assertIn("assistantSessionLabel()", resource_panel)
        self.assertIn("Get help in ${escapeHtml(assistantLabel)}", resource_panel)
        self.assertIn("configure and publish without using the Conversation", resource_panel)
        self.assertIn("makes the Workspace available to", resource_panel)
        self.assertIn("What should this Workspace publish?", resource_panel)
        self.assertIn('value="environment"', resource_panel)
        self.assertIn('value="method"', resource_panel)
        self.assertIn('value="generator"', resource_panel)
        self.assertIn("Environment and Method starters deliberately require", resource_panel)
        self.assertIn("assistantIsBusy()", resource_panel)
        self.assertIn("assistantIsAwaitingApproval()", resource_panel)
        self.assertIn('class="primary-button registration-resource-apply"', resource_panel)
        self.assertIn('class="ghost-button registration-curate-assistant"', resource_panel)
        self.assertLess(
            resource_panel.index("registration-resource-apply"),
            resource_panel.index("registration-curate-assistant"),
        )
        self.assertIn("requestWorkspaceCuration", binding)

    def test_curation_preserves_semantic_choices_and_uses_setup_test_flow(self) -> None:
        curation = _function_source(
            self.source, "requestWorkspaceCuration", "splitLines"
        )

        self.assertIn("do not guess semantic choices", curation)
        self.assertIn("candidate contract, objectives, metrics", curation)
        self.assertIn("public config files under optpilot_configs/", curation)
        self.assertIn("existing Publish checks and Test workflow", curation)
        self.assertIn("explicitly confirm the final checked version", curation)
        self.assertNotIn("package-plan", curation)
        self.assertNotIn("register or apply", curation)
        self.assertIn("Do not publish the Workspace", curation)
        self.assertIn("assistantIsBusy()", curation)
        self.assertIn("assistantIsAwaitingApproval()", curation)
        self.assertIn(
            "!attachedWorkspaceIds(agentSession.id).includes(workspace.id)",
            curation,
        )
        self.assertIn("await attachWorkspaceToCurrent(workspace.id)", curation)
        self.assertIn("setAssistantOpen(true)", curation)
        self.assertIn("rethrowError: true", curation)

        attach = curation.index("await attachWorkspaceToCurrent")
        open_assistant = curation.index("setAssistantOpen(true)")
        persist = curation.index("await persistAssistantMessage")
        accepted = curation.index("if (!persisted) throw")
        leave_registration = curation.index('state.assistantMode = "chat"')
        self.assertLess(attach, open_assistant)
        self.assertLess(leave_registration, open_assistant)
        self.assertLess(open_assistant, persist)
        self.assertLess(persist, accepted)


if __name__ == "__main__":
    unittest.main()
