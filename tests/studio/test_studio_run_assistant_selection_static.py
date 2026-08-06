"""Browser-client contracts for carrying exact Run selections to the Assistant."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP = (
    Path(__file__).resolve().parents[2]
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


class StudioRunAssistantSelectionStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_every_bounded_workbench_row_can_seed_assistant_context(self) -> None:
        render_item = _function_source(
            self.source, "renderWorkbenchItem", "actionCapability"
        )
        binding = _function_source(
            self.source, "bindWorkbenchEntityActions", "entityStateTags"
        )

        self.assertIn("data-workbench-ask-assistant", render_item)
        self.assertIn("Open this exact Run selection in the named Conversation", render_item)
        self.assertIn("askAssistantAboutWorkbenchSelection", binding)

    def test_assistant_receives_exact_selection_without_automatic_execution(self) -> None:
        selection = _function_source(
            self.source,
            "askAssistantAboutWorkbenchSelection",
            "performWorkbenchAction",
        )
        context = _function_source(
            self.source, "assistantSelectedRunContext", "persistAssistantMessage"
        )
        context_summary = _function_source(
            self.source, "assistantContextSummary", "selectedRunSummary"
        )

        self.assertIn("/run-selection", selection)
        self.assertIn("presentation_selection: item.selection", selection)
        self.assertIn("handle: payload.handle", selection)
        self.assertNotIn("data: item.data", selection)
        self.assertNotIn("correlations:", selection)
        self.assertIn('state.assistantMode = "chat"', selection)
        self.assertIn("els.agentInput.value", selection)
        self.assertNotIn("persistAssistantMessage", selection)
        self.assertIn("selection_handle = selected.handle", context)
        self.assertIn("selected.run_id === runId", context)
        self.assertNotIn("presentation_selection", context)
        self.assertNotIn("correlations", context)
        self.assertIn("Selection: ${selected.kind} ${selected.id}", context_summary)


if __name__ == "__main__":
    unittest.main()
