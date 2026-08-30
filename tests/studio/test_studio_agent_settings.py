"""Agent-settings persistence: partial saves must not clobber the runtime.

Regression coverage for the defect where any ``/api/agent/settings`` payload
without an ``openhands`` section (e.g. an environment-variables-only save)
reset ``enabled`` to ``False`` and cleared ``base_url``/``model``, silently
turning the Assistant off.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.agent import (
    DEFAULT_OPENHANDS_BASE_URL,
    OPENHANDS_NO_SERVER_VALUES,
)
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
            stored = json.loads(state.settings_path.read_text(encoding="utf-8"))
            stored_openhands = stored["assistant"]["openhands"]

        self.assertFalse(openhands["enabled"])
        self.assertEqual(openhands["model"], "")
        # The point of this case is that a full save OVERWRITES rather than
        # merges, so check the stored value was really cleared. What the
        # projection reports for a cleared URL is a separate question: an
        # unset server URL now means "the helper OptPilot runs", so it reads
        # back as that address rather than as blank.
        self.assertEqual(stored_openhands["base_url"], "")
        self.assertEqual(stored_openhands["model"], "")
        self.assertEqual(openhands["base_url"], DEFAULT_OPENHANDS_BASE_URL)

    def test_explicit_no_server_survives_a_full_form_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "OFF",
                        "model": "openai/gpt-5",
                        "api_key": "secret-token",
                    }
                },
            )
            projected = self._openhands(state)
            self.assertEqual(projected["base_url"], "off")

            # This is the shape submitted by a full Settings form save.
            _update_agent_settings(state, {"openhands": projected})
            stored = json.loads(state.settings_path.read_text(encoding="utf-8"))
            final_payload = _agent_settings_payload(state)

        self.assertEqual(stored["assistant"]["openhands"]["base_url"], "off")
        self.assertEqual(
            final_payload["settings"]["assistant"]["openhands"]["base_url"],
            "off",
        )
        self.assertFalse(final_payload["status"]["server_configured"])
        self.assertEqual(final_payload["status"]["dispatch"], "openrouter_chat")

    def test_each_explicit_no_server_value_is_preserved(self) -> None:
        for sentinel in OPENHANDS_NO_SERVER_VALUES:
            with (
                self.subTest(sentinel=sentinel),
                tempfile.TemporaryDirectory() as tmp_dir,
            ):
                state = self._state(Path(tmp_dir))
                saved = _update_agent_settings(
                    state, {"openhands": {"base_url": sentinel.upper()}}
                )
                projected = saved["settings"]["assistant"]["openhands"]
                self.assertEqual(projected["base_url"], sentinel)

    def test_invalid_server_urls_are_refused_before_they_are_written(self) -> None:
        invalid = (
            0,
            {"url": "https://agent.example.com"},
            "agent.example.com:8781",
            "ftp://agent.example.com",
            "http:///missing-host",
            "http://agent.example.com:not-a-port",
            "https://agent example.com",
            "https://user:secret@agent.example.com",
            "https://agent.example.com/path#fragment",
        )
        for value in invalid:
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as tmp_dir,
            ):
                state = self._state(Path(tmp_dir))
                with self.assertRaises(ValueError):
                    _update_agent_settings(
                        state, {"openhands": {"base_url": value}}
                    )
                self.assertFalse(state.settings_path.exists())

    def test_http_and_https_server_urls_are_saved(self) -> None:
        for value in (
            "http://127.0.0.1:8781/",
            "https://agent.example.com/openhands/",
        ):
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as tmp_dir,
            ):
                state = self._state(Path(tmp_dir))
                saved = _update_agent_settings(
                    state, {"openhands": {"base_url": value}}
                )
                projected = saved["settings"]["assistant"]["openhands"]
                self.assertEqual(projected["base_url"], value.rstrip("/"))

    def test_an_invalid_url_does_not_replace_existing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "https://agent.example.com",
                        "model": "openai/gpt-5",
                    }
                },
            )
            before = state.settings_path.read_bytes()
            with self.assertRaises(ValueError):
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": False,
                            "base_url": "https://user:secret@agent.example.com",
                        }
                    },
                )
            self.assertEqual(state.settings_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
