"""The Assistant can open a component's interface, not only call it headlessly.

Two flagship packages are meant to be used through a web interface they ship --
the DEVS simulation generator, and the COOPA solve console. Studio could launch
those from the Catalog page; the Assistant had no tool for it. Asked to "launch
the DEVS Gen interface", it improvised towards things it did have (workspace
previews, baseline runs) instead of saying it could not.

Nothing in a listing said which entries even had one, so it could not have
known. That is fixed here too.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.agent import OPTPILOT_AGENT_TOOLS, OPTPILOT_AGENT_TOOL_SPECS
from optpilot_studio.ui.server import (
    ASSISTANT_CATALOG_LIST_FIELDS,
    ASSISTANT_PERMISSION_VALUES,
    DEFAULT_ASSISTANT_PERMISSIONS,
    UiState,
    _assistant_catalog_entry,
    _catalog_payload,
    _create_agent_session,
    _execute_agent_tool,
)

_ROOT = Path(__file__).resolve().parents[2]


class InterfaceToolsAdvertisedTest(unittest.TestCase):
    def test_both_tools_exist(self) -> None:
        names = {spec["name"] for spec in OPTPILOT_AGENT_TOOL_SPECS}
        for tool in ("optpilot_interface_launch", "optpilot_interface_status"):
            self.assertIn(tool, OPTPILOT_AGENT_TOOLS)
            self.assertIn(tool, names)

    def test_opening_an_interface_asks_first(self) -> None:
        # It starts a container running the component's own application with
        # the credentials that component declares -- the same exposure as
        # running a resource action.
        self.assertEqual(
            DEFAULT_ASSISTANT_PERMISSIONS["interface_launch"], "approval_required"
        )
        self.assertNotIn(
            "safe_without_approval", ASSISTANT_PERMISSION_VALUES["interface_launch"]
        )

    def test_a_listing_says_which_entries_have_one(self) -> None:
        self.assertIn("has_interface", ASSISTANT_CATALOG_LIST_FIELDS)

    def test_the_flag_reflects_the_entry(self) -> None:
        self.assertTrue(
            _assistant_catalog_entry({"id": "x", "summary": {"interface": {"command": "run"}}})[
                "has_interface"
            ]
        )
        self.assertFalse(
            _assistant_catalog_entry({"id": "x", "summary": {}})["has_interface"]
        )


@unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs the shipped packages")
class InterfaceLaunchGateTest(unittest.TestCase):
    def _state(self, tmp: Path) -> UiState:
        state = UiState(cwd=_ROOT, catalog_roots=[_ROOT / "catalog"], run_roots=[])
        for name in (
            "sessions_dir", "agent_sessions_dir", "jobs_dir",
            "workspaces_dir", "runtime_dir",
        ):
            setattr(state, name, tmp / name)
            getattr(state, name).mkdir(parents=True, exist_ok=True)
        state.settings_path = tmp / "settings.json"
        return state

    def test_the_shipped_generator_is_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            resources = [
                _assistant_catalog_entry(entry)
                for entry in _catalog_payload(state)["resources"]
            ]
        withui = [e["qualified_id"] for e in resources if e.get("has_interface")]
        self.assertIn("devs_gallery/resource/devs-gen-interface", withui)

    def test_launching_it_requests_approval_rather_than_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "iface"})
            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_interface_launch",
                {"config_kind": "resource", "uid": "devs-gen-interface"},
            )
        self.assertTrue(result["data"].get("approval_required"))
        self.assertIn("devs-gen-interface", result["summary"])

    def test_it_refuses_without_naming_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "iface"})
            with self.assertRaises(ValueError):
                _execute_agent_tool(
                    state, session["id"], "optpilot_interface_launch", {}
                )


if __name__ == "__main__":
    unittest.main()
