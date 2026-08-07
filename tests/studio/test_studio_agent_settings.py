"""Agent-settings persistence: partial saves must not clobber the runtime.

Regression coverage for the defect where any ``/api/agent/settings`` payload
without an ``openhands`` section (e.g. an environment-variables-only save)
reset ``enabled`` to ``False`` and cleared ``base_url``/``model``, silently
turning the Assistant off.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _agent_settings_payload,
    _update_agent_settings,
)


class AgentSettingsPartialSaveTest(unittest.TestCase):
    def _state(self, tmp_path: Path) -> UiState:
        return UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])

    def _openhands(self, state: UiState) -> dict:
        payload = _agent_settings_payload(state)
        return payload["settings"]["assistant"]["openhands"]

    def test_environment_only_save_preserves_openhands_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:8781",
                        "model": "deepseek/deepseek-v4-pro",
                        "api_key": "secret-token",
                    }
                },
            )
            _update_agent_settings(
                state,
                {"environment": {"variables": {"COOPA_HOME": "/tmp/coopa"}}},
            )
            openhands = self._openhands(state)
            self.assertTrue(openhands["enabled"])
            self.assertEqual(openhands["base_url"], "http://127.0.0.1:8781")
            self.assertEqual(openhands["model"], "deepseek/deepseek-v4-pro")
            self.assertTrue(openhands["api_key_configured"])

    def test_partial_openhands_section_updates_only_named_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:8781",
                        "model": "deepseek/deepseek-v4-pro",
                    }
                },
            )
            _update_agent_settings(
                state, {"openhands": {"model": "anthropic/claude-opus-4.8"}}
            )
            openhands = self._openhands(state)
            self.assertTrue(openhands["enabled"])
            self.assertEqual(openhands["base_url"], "http://127.0.0.1:8781")
            self.assertEqual(openhands["model"], "anthropic/claude-opus-4.8")

    def test_full_save_still_overwrites_every_connection_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:8781",
                        "model": "deepseek/deepseek-v4-pro",
                    }
                },
            )
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": False,
                        "base_url": "",
                        "session_endpoint": "",
                        "model": "",
                    }
                },
            )
            openhands = self._openhands(state)
            self.assertFalse(openhands["enabled"])
            self.assertEqual(openhands["base_url"], "")
            self.assertEqual(openhands["model"], "")


if __name__ == "__main__":
    unittest.main()
