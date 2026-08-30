"""A setting OptPilot cannot honour is refused, not quietly dropped.

Saving `smoke_test: banana` returned success and stored the default instead,
so a person could change a setting, be told it saved, and have it stay exactly
as it was with nothing to notice. The value was normalised on the way in, and
normalising discards.

Normalising on the way OUT is a different thing and stays: a settings file
written by a newer version of OptPilot must not stop this one from starting.
So the same unknown value is refused when saved and tolerated when read.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    ASSISTANT_PERMISSION_VALUES,
    UiState,
    _agent_settings_payload,
    _update_agent_settings,
)


class RefuseUnhonourableSettingsTest(unittest.TestCase):
    def _state(self, tmp: Path) -> UiState:
        return UiState(cwd=tmp, catalog_roots=[], run_roots=[])

    def test_an_unknown_value_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            with self.assertRaises(ValueError) as raised:
                _update_agent_settings(
                    state, {"permissions": {"smoke_test": "banana"}}
                )
        message = str(raised.exception)
        self.assertIn("banana", message)
        self.assertIn("smoke_test", message)
        # and it says what WOULD work
        for allowed in ASSISTANT_PERMISSION_VALUES["smoke_test"]:
            self.assertIn(allowed, message)

    def test_an_unknown_permission_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            with self.assertRaises(ValueError) as raised:
                _update_agent_settings(
                    state, {"permissions": {"launch_rockets": "disabled"}}
                )
        self.assertIn("launch_rockets", str(raised.exception))

    def test_a_real_change_still_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state, {"permissions": {"smoke_test": "approval_required"}}
            )
            stored = _agent_settings_payload(state)["settings"]["assistant"]
        self.assertEqual(stored["permissions"]["smoke_test"], "approval_required")

    def test_reading_a_newer_file_still_falls_back(self) -> None:
        # The tolerance that matters: a file this build does not fully
        # understand must not stop it from starting.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state = self._state(tmp)
            state.settings_path.parent.mkdir(parents=True, exist_ok=True)
            state.settings_path.write_text(
                '{"assistant": {"permissions": {"smoke_test": "from_the_future"}}}',
                encoding="utf-8",
            )
            stored = _agent_settings_payload(state)["settings"]["assistant"]
        self.assertIn(
            stored["permissions"]["smoke_test"],
            ASSISTANT_PERMISSION_VALUES["smoke_test"],
        )


if __name__ == "__main__":
    unittest.main()


class RefuseUnusableCapabilitiesTest(unittest.TestCase):
    """The same silence, in the two other places a save discarded input.

    A tool name this build has no tool for was accepted and dropped. An MCP
    server with neither a command to start nor an address to reach was accepted
    and stored as an *enabled* entry with both fields blank -- which reads on
    screen as configured and can never work.
    """

    def _state(self, tmp: Path) -> UiState:
        return UiState(cwd=tmp, catalog_roots=[], run_roots=[])

    def test_settings_does_not_offer_inactive_capability_editors(self) -> None:
        import optpilot_studio

        page = (
            Path(optpilot_studio.__file__).parent
            / "ui"
            / "static"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("preview-only in this release", page)
        self.assertIn("intentionally not editable here", page)
        self.assertNotIn('id="assistantSkillsInput"', page)
        self.assertNotIn('id="assistantMcpServersInput"', page)
        self.assertNotIn('id="assistantCustomToolsInput"', page)

    def test_a_narrowed_tool_list_says_what_it_left_out(self) -> None:
        # Narrowing is deliberate. Native filesystem and shell tools execute
        # outside Studio's Workspace boundary; only task tracking is safe to
        # keep inside the agent-server process.
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            saved = _update_agent_settings(
                state,
                {"openhands": {"native_tools": ["grep", "terminal", "file_editor"]}},
            )
        self.assertEqual(
            saved["settings"]["assistant"]["openhands"]["native_tools"], []
        )
        notices = " ".join(saved.get("notices") or [])
        self.assertIn("grep", notices)
        self.assertIn("terminal", notices)
        self.assertIn("file_editor", notices)

    def test_unsafe_native_filesystem_tools_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            saved = _update_agent_settings(
                state, {"openhands": {"native_tools": ["grep", "glob"]}}
            )
        notices = " ".join(saved.get("notices") or [])
        self.assertIn("grep", notices)
        self.assertIn("glob", notices)
        self.assertIn("Workspace boundary", notices)

    def test_task_tracker_is_the_only_supported_native_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state, {"openhands": {"native_tools": ["task_tracker"]}}
            )
            stored = _agent_settings_payload(state)["settings"]["assistant"]
        self.assertEqual(stored["openhands"]["native_tools"], ["task_tracker"])

    def test_a_server_with_no_way_to_reach_it_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            with self.assertRaises(ValueError) as raised:
                _update_agent_settings(
                    state, {"capabilities": {"mcp_servers": [{"name": "broken"}]}}
                )
        self.assertIn("broken", str(raised.exception))

    def test_a_reachable_server_still_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "capabilities": {
                        "mcp_servers": [
                            {"name": "notion", "url": "https://mcp.notion.com/mcp"}
                        ]
                    }
                },
            )
            stored = _agent_settings_payload(state)["settings"]["assistant"]
        self.assertEqual(len(stored["capabilities"]["mcp_servers"]), 1)

    def test_legacy_mcp_auth_is_never_returned_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            state.settings_path.parent.mkdir(parents=True, exist_ok=True)
            state.settings_path.write_text(
                json.dumps(
                    {
                        "assistant": {
                            "capabilities": {
                                "mcp_servers": [
                                    {
                                        "id": "legacy",
                                        "name": "Legacy",
                                        "url": "https://mcp.example.com",
                                        "auth": "bearer-secret-value",
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            payload = _agent_settings_payload(state)

        serialized = json.dumps(payload)
        self.assertNotIn("bearer-secret-value", serialized)
        record = payload["settings"]["assistant"]["capabilities"]["mcp_servers"][0]
        self.assertNotIn("auth", record)
        self.assertTrue(record["auth_configured"])
