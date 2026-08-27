"""Naming the wrong Workspace says so, and an update hands back the revision.

Drafting a Run setup, the assistant passed the Workspace it had attached --
the simulator's -- and was told "expected_workspace_revision is required
when updating a draft". That workspace was not a Run-setup draft at all, so
the number it was sent to find could never have helped. The check order put
the requirement before the identity.

Identity first now: a Workspace that is not a Run-setup draft says so and
points at drafting a new one, and a genuine update carries the draft's
current revision in its remedy so the caller has the value it needs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot_studio.ui.server import UiState, _create_ui_workspace, _draft_study


def _state(tmp: Path) -> UiState:
    state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
    for name in ("sessions_dir", "workspaces_dir", "runtime_dir", "agent_sessions_dir"):
        setattr(state, name, tmp / name)
        getattr(state, name).mkdir(parents=True, exist_ok=True)
    state.settings_path = tmp / "settings.json"
    return state


class DraftWorkspaceIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = _state(self.root)
        self.addCleanup(self.state.close_coordination)
        self.workspace = _create_ui_workspace(
            self.state,
            {"title": "restaurant-simulator", "root": str(self.root / "ws")},
        )

    def _draft(self, **payload):
        # Catalog resolution runs first and is not what this pins; stub it so
        # the workspace-identity check is reached.
        with mock.patch(
            "optpilot_studio.ui.server._study_builder_sources",
            return_value=mock.Mock(
                environment_ref=mock.Mock(),
                method_ref=mock.Mock(),
                environment_path=Path("environment.yaml"),
                method_path=Path("method.yaml"),
            ),
        ), mock.patch(
            "optpilot_studio.ui.server._study_builder_component_origins",
            return_value={},
        ):
            return _draft_study(self.state, payload)

    def test_an_ordinary_workspace_is_not_a_draft_to_update(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._draft(
                workspace_id=self.workspace["id"],
                environment_ref="restaurant_sim/environment/restaurant-simulator-policy",
                method_ref="production_agv_scheduling/method/process-aware-llm-heuristic-design",
            )
        message = str(caught.exception)
        self.assertIn("not a Run-setup draft", message)
        self.assertNotIn("expected_workspace_revision", message)
        remedy = getattr(caught.exception, "remedy", None) or getattr(
            caught.exception, "optpilot_remedy", None
        )
        self.assertIsNotNone(remedy)
        self.assertEqual(
            remedy.get("details", {}).get("reason"), "not_a_run_setup_draft"
        )

    def test_a_real_draft_missing_its_revision_is_handed_the_number(self) -> None:
        managed = dict(self.workspace)
        managed["ownership"] = "realm-managed"
        managed["realm_workspace_revision"] = 7
        with mock.patch(
            "optpilot_studio.ui.server._require_ui_workspace", return_value=managed
        ):
            with self.assertRaises(ValueError) as caught:
                self._draft(
                    workspace_id=self.workspace["id"],
                    environment_ref="restaurant_sim/environment/restaurant-simulator-policy",
                    method_ref="production_agv_scheduling/method/process-aware-llm-heuristic-design",
                )
        self.assertIn("current revision", str(caught.exception))
        remedy = getattr(caught.exception, "remedy", None) or getattr(
            caught.exception, "optpilot_remedy", None
        )
        self.assertEqual(remedy.get("details", {}).get("current_revision"), 7)


if __name__ == "__main__":
    unittest.main()
