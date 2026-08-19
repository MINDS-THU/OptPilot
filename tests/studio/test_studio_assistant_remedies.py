"""A refusal says what would fix it, in a form that needs no interpretation.

OptPilot refuses things on the Assistant's behalf all day: an image that has
not been approved for running code, a run setup waiting on values only the
person has, a config that does not validate, a tool that is simply the wrong
door. Each refusal carried one sentence written for a person, and the Assistant
had to work the fix out of that English. Often it managed; sometimes it
apologised and stopped, retried the identical call, or proposed a fix for a
different problem.

Each refusal now carries the same answer twice: the sentence, and a `remedy`
saying what to do -- a command only the person can run, or a tool call the
Assistant can make itself, plus the specifics either needs.

Two design points these tests hold. A remedy never appears on a success, where
it would read as "here is something else you should have done". And a remedy
rides on whatever exception the code already raises rather than a new type of
its own: the type carries meaning here, with thirty-five handlers branching on
one of them and an HTTP status derived from it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    REMEDY_SCHEMA,
    UiState,
    _attach_agent_workspace,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _remedy,
    _study_launch_block_remedy,
    _update_agent_settings,
    _with_remedy,
)
from optpilot.realm.errors import RealmConflict


class RemedyShapeTest(unittest.TestCase):
    def test_a_remedy_names_its_shape(self) -> None:
        self.assertEqual(_remedy("do the thing")["schema"], REMEDY_SCHEMA)

    def test_a_command_and_a_tool_call_are_distinguishable(self) -> None:
        by_command = _remedy("run it", command="optpilot image approve x")
        by_tool = _remedy("call it", tool="optpilot_run_detail", arguments={"run_id": "r"})
        self.assertIn("command", by_command)
        self.assertNotIn("tool", by_command)
        self.assertEqual(by_tool["tool"], "optpilot_run_detail")
        self.assertEqual(by_tool["arguments"], {"run_id": "r"})
        self.assertNotIn("command", by_tool)

    def test_a_remedy_rides_the_exception_type_already_in_use(self) -> None:
        # Changing the type would change which handler runs and which HTTP
        # status the caller sees.
        error = _with_remedy(RealmConflict("nope"), _remedy("do this"))
        self.assertIsInstance(error, RealmConflict)
        self.assertEqual(error.remedy["summary"], "do this")


class LaunchBlockRemedyTest(unittest.TestCase):
    """The two common blocks need opposite responses from the Assistant."""

    def test_missing_values_are_the_persons_to_supply(self) -> None:
        remedy = _study_launch_block_remedy(
            {
                "code": "study_inputs_required",
                "missing_inputs": ["problem"],
                "input_declarations": {"problem": {"valueType": "string"}},
            }
        )
        self.assertIn("problem", remedy["summary"])
        self.assertIn("never invent", remedy["summary"])
        self.assertEqual(remedy["tool"], "optpilot_study_launch")
        self.assertEqual(remedy["details"]["missing_inputs"], ["problem"])
        self.assertIn("problem", remedy["details"]["input_declarations"])

    def test_an_invalid_run_setup_is_something_to_go_and_fix(self) -> None:
        remedy = _study_launch_block_remedy({"code": "study_invalid"})
        self.assertIn("fix", remedy["summary"].lower())
        self.assertEqual(remedy["tool"], "optpilot_config_validate")

    def test_an_unfamiliar_block_still_produces_a_remedy(self) -> None:
        remedy = _study_launch_block_remedy(
            {"code": "something_new", "reason": "Not today."}
        )
        self.assertEqual(remedy["summary"], "Not today.")
        self.assertEqual(remedy["details"]["code"], "something_new")


class RefusalsCarryRemediesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        workspace_root = self.root / "workspace"
        workspace_root.mkdir()
        (workspace_root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

        self.state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(self.state.close_coordination)
        for name in (
            "sessions_dir",
            "agent_sessions_dir",
            "jobs_dir",
            "workspaces_dir",
            "runtime_dir",
        ):
            setattr(self.state, name, self.root / name)
            getattr(self.state, name).mkdir(parents=True, exist_ok=True)
        self.state.settings_path = self.root / "settings.json"
        _update_agent_settings(
            self.state,
            {
                "openhands": {"enabled": False},
                "permissions": {"shell_run": "disabled"},
            },
        )
        self.session = _create_agent_session(self.state, {"title": "Remedies"})
        workspace = _create_ui_workspace(
            self.state,
            {"title": "W", "root": str(workspace_root), "editable": True},
        )
        _attach_agent_workspace(
            self.state, self.session["id"], workspace["id"], select=True
        )

    def _call(self, tool: str, arguments: dict) -> dict:
        return _execute_agent_tool(self.state, self.session["id"], tool, arguments)

    def test_a_switched_off_permission_names_the_control_to_change(self) -> None:
        result = self._call("optpilot_shell_run", {"command": ["echo", "hello"]})
        self.assertFalse(result["ok"])
        remedy = result["remedy"]
        self.assertIn("Settings", remedy["summary"])
        self.assertIn("Shell commands", remedy["summary"])
        self.assertEqual(remedy["details"]["permission"], "shell_run")

    def test_a_switched_off_permission_says_not_to_retry(self) -> None:
        remedy = self._call("optpilot_shell_run", {"command": ["echo", "x"]})["remedy"]
        self.assertIn("Do not retry", remedy["summary"])

    def test_a_credentials_file_says_there_is_no_way_through(self) -> None:
        with self.assertRaises(PermissionError) as caught:
            self._call("optpilot_file_read", {"path": ".env"})
        remedy = caught.exception.remedy
        self.assertIn("no point retrying", remedy["summary"])
        self.assertEqual(remedy["details"]["reason"], "holds_credentials")

    def test_the_wrong_door_names_the_right_one(self) -> None:
        result = self._call("optpilot_run_file_read", {"run_id": "run-1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["remedy"]["tool"], "optpilot_run_detail")
        self.assertEqual(result["remedy"]["arguments"], {"run_id": "run-1"})

    def test_a_success_carries_no_remedy(self) -> None:
        result = self._call("optpilot_workspace_list", {})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("remedy", result)


class GuidanceTest(unittest.TestCase):
    def test_the_assistant_is_told_to_read_remedies_and_not_retry(self) -> None:
        prompt = (
            Path(__file__).resolve().parents[2]
            / "studio"
            / "src"
            / "optpilot_studio"
            / "assistant_assets"
            / "prompts"
            / "system.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`remedy`", prompt)
        self.assertIn("Never retry a refusal unchanged", prompt)


if __name__ == "__main__":
    unittest.main()
