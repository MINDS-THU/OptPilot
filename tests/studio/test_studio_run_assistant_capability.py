"""Capability truth for Studio's path-free Run-to-Assistant handoff."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from optpilot_studio.ui.server import (
    _global_workbench_action_capabilities,
    _row_workbench_action_capabilities,
)


class StudioRunAssistantCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SimpleNamespace(realm_runtime=None)
        self.base = [
            {
                "action": "ask_assistant",
                "supported": False,
                "eligible": False,
                "reason": "assistant_selection_provider_unavailable",
            }
        ]

    def test_studio_globally_advertises_its_local_context_handoff(self) -> None:
        result = _global_workbench_action_capabilities(self.state, self.base)

        self.assertEqual(
            result,
            [
                {
                    "action": "ask_assistant",
                    "supported": True,
                    "eligible": True,
                    "reason": None,
                }
            ],
        )

    def test_every_exact_head_entity_row_is_eligible(self) -> None:
        result = _row_workbench_action_capabilities(
            self.state,
            run_id="run-assistant-context",
            row={
                "selection": {"kind": "observation"},
                "eligibility": self.base,
            },
        )

        self.assertEqual(result[0]["action"], "ask_assistant")
        self.assertTrue(result[0]["supported"])
        self.assertTrue(result[0]["eligible"])
        self.assertIsNone(result[0]["reason"])


if __name__ == "__main__":
    unittest.main()
