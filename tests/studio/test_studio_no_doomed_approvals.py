"""Nobody is asked to approve something already known to fail.

A conversation raised the same approval six times in four minutes: each
approved run failed for three environment values that were never set, and the
Assistant asked again. A later one did the same for an interface launch. From
the person's side it is a prompt that reappears with nothing to act on, so they
keep approving, and nothing ever changes.

Studio already computes whether these can run, with a reason, and the Catalog
page shows it. The tools went straight to the approval card without consulting
it. Approving something is a decision; being asked to make the same doomed
decision repeatedly is not a decision at all.

Adding context to the second card -- which was the first attempt at this -- did
not help, because this path records no tool result to draw the reason from. The
check has to happen before the question is asked.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _create_agent_session,
    _execute_agent_tool,
)

_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs the shipped packages")
class NoDoomedApprovalTest(unittest.TestCase):
    _DEVS_RESOURCE = "devs_gallery/resource/devs-gen-interface"

    def _state(self, tmp: Path) -> UiState:
        # No environment values configured, so the shipped generator cannot run.
        state = UiState(
            cwd=_ROOT,
            catalog_roots=[_ROOT / "catalog" / "devs_gallery"],
            run_roots=[],
        )
        for name in (
            "sessions_dir", "agent_sessions_dir", "jobs_dir",
            "workspaces_dir", "runtime_dir",
        ):
            setattr(state, name, tmp / name)
            getattr(state, name).mkdir(parents=True, exist_ok=True)
        state.settings_path = tmp / "settings.json"
        return state

    def _run(self, tool: str, arguments: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "doomed"})
            previous = os.environ.pop("OPENROUTER_API_KEY", None)
            try:
                return _execute_agent_tool(state, session["id"], tool, arguments)
            finally:
                if previous is not None:
                    os.environ["OPENROUTER_API_KEY"] = previous

    def test_an_interface_that_cannot_launch_is_not_put_up_for_approval(self) -> None:
        result = self._run(
            "optpilot_interface_launch",
            {"config_kind": "resource", "uid": self._DEVS_RESOURCE},
        )
        self.assertFalse(result["data"].get("approval_required"))
        self.assertFalse(result["ok"])
        self.assertIn("Studio Settings", result["summary"])

    def test_an_action_that_cannot_run_is_not_put_up_for_approval(self) -> None:
        result = self._run(
            "optpilot_resource_action_run",
            {"resource_uid": self._DEVS_RESOURCE, "action_id": "generate"},
        )
        self.assertFalse(result["data"].get("approval_required"))
        self.assertFalse(result["ok"])
        self.assertIn("Studio Settings", result["summary"])

    def test_the_refusal_names_what_is_missing(self) -> None:
        # Not just "cannot run" -- the person has to know what to add.
        result = self._run(
            "optpilot_resource_action_run",
            {"resource_uid": self._DEVS_RESOURCE, "action_id": "generate"},
        )
        self.assertIn("OPENROUTER_API_KEY", result["summary"])

    def test_the_refusal_carries_a_remedy_the_assistant_can_act_on(self) -> None:
        result = self._run(
            "optpilot_interface_launch",
            {"config_kind": "resource", "uid": self._DEVS_RESOURCE},
        )
        remedy = result["data"].get("remedy") or {}
        self.assertEqual(remedy.get("kind"), "configure_environment")
        self.assertTrue(remedy.get("reason"))


if __name__ == "__main__":
    unittest.main()
