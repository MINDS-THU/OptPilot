"""Untouched conversations stay out of the sidebar and get reaped.

A conversation the user never wrote to (and never attached a Workspace to)
must not appear in the Conversations list as an "Untitled conversation"
entry, and sufficiently old ones are deleted outright.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _agent_session_dir,
    _append_agent_message,
    _create_agent_session,
    _list_agent_session_summaries,
    _read_agent_session_index,
    _upsert_agent_session,
)


class UntouchedConversationTest(unittest.TestCase):
    def _state(self, tmp_path: Path) -> UiState:
        return UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])

    def test_untouched_conversation_is_hidden_from_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            _create_agent_session(state, {"title": "Untitled conversation"})
            listed = _list_agent_session_summaries(state)
            self.assertEqual(listed, [])

    def test_conversation_with_user_message_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "Untitled conversation"})
            try:
                _append_agent_message(
                    state,
                    session["id"],
                    {"role": "user", "content": "hello"},
                )
            except Exception:
                # Dispatch to the (absent) agent runtime may fail; the
                # message and turn state are recorded before dispatch.
                pass
            listed = _list_agent_session_summaries(state)
            self.assertEqual(
                [item["id"] for item in listed], [session["id"]]
            )

    def test_old_untouched_conversation_is_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "Untitled conversation"})
            record = next(
                item
                for item in _read_agent_session_index(state)
                if item.get("id") == session["id"]
            )
            record["created_at"] = "2020-01-01T00:00:00Z"
            _upsert_agent_session(state, record)
            _list_agent_session_summaries(state)
            remaining = [
                item.get("id") for item in _read_agent_session_index(state)
            ]
            self.assertNotIn(session["id"], remaining)
            self.assertFalse(_agent_session_dir(state, session["id"]).exists())

    def test_recent_untouched_conversation_survives_reap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "Untitled conversation"})
            _list_agent_session_summaries(state)
            remaining = [
                item.get("id") for item in _read_agent_session_index(state)
            ]
            self.assertIn(session["id"], remaining)


if __name__ == "__main__":
    unittest.main()
