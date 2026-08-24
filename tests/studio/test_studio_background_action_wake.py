"""A finished background action wakes the conversation that started it.

A generation runs for minutes; a model cannot hold a turn open that long, so
the turn ends while the work runs. Until now nothing showed the job existed
and nothing ever re-entered the loop, so the Assistant's "I'll continue when
the result arrives" was a promise the architecture could not keep -- reported
live: the person typed a request, was told to wait a moment, and nothing ever
happened again.

Now starting an action from a conversation records which conversation, posts a
visible transcript note that the work is running, and on completion -- success
or failure alike -- posts the outcome back into that conversation with the
loop re-entered, and marks the session as waiting so the page polls for what
the agent does next.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import optpilot_studio.ui.server as server
from optpilot_studio.ui.server import (
    UiState,
    _create_agent_session,
    _notify_agent_session_resource_action_done,
    _require_agent_session,
)


def _state(tmp: Path) -> UiState:
    state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
    for name in (
        "sessions_dir", "agent_sessions_dir", "jobs_dir",
        "workspaces_dir", "runtime_dir",
    ):
        setattr(state, name, tmp / name)
        getattr(state, name).mkdir(parents=True, exist_ok=True)
    state.settings_path = tmp / "settings.json"
    return state


def _record(session_id: str, conversation_id: str, status: str) -> dict:
    return {
        "request_id": "req-1",
        "resource_uid": "devs-gen-interface",
        "resource_id": "devs-gen-interface",
        "action_id": "generate",
        "status": status,
        "started_at": 1.0,
        "finished_at": 2.0,
        "summary": {"ok": status == "succeeded", "outputs": {}, "output_root": "x"},
        "error": None if status == "succeeded" else "boom",
        "agent_session_id": session_id,
        "agent_conversation_id": conversation_id,
    }


class BackgroundActionWakeTest(unittest.TestCase):
    def _run(self, status: str, conversation_id: str = "conv-1"):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "bg"})
            posted = []
            # A blanket Mock poisons the transcript write: appending a message
            # embeds the adapter-built context packet, and an unconfigured
            # Mock attribute returns a Mock, which is not JSON-serializable --
            # so the append, and with it the whole notification, dies before
            # posting anything. Both adapter methods the packet touches are
            # pinned to plain values.
            state.agent_adapter = mock.Mock(**{"status.return_value": {}, "context_packet.return_value": {}})
            state.agent_adapter.post_background_result = mock.Mock(
                side_effect=lambda cid, text: (
                    posted.append((cid, text)) or {"sent": True}
                )
            )
            _notify_agent_session_resource_action_done(
                state, _record(session["id"], conversation_id, status)
            )
            refreshed = _require_agent_session(state, session["id"])
            messages = server._read_agent_messages(state, session["id"])
            return refreshed, messages, posted

    def test_success_posts_the_result_and_marks_the_session_waiting(self) -> None:
        session, messages, posted = self._run("succeeded")
        self.assertEqual(len(posted), 1)
        conversation_id, text = posted[0]
        self.assertEqual(conversation_id, "conv-1")
        self.assertIn("generate", text)
        self.assertIn("nothing further is coming", text)
        self.assertEqual(session.get("status"), "waiting_for_agent")
        self.assertTrue(
            any("finished" in str(m.get("content", "")) for m in messages)
        )

    def test_failure_wakes_the_conversation_too(self) -> None:
        # A failed generation that stays silent is exactly the stall this
        # exists to prevent.
        _session, _messages, posted = self._run("failed")
        self.assertEqual(len(posted), 1)
        self.assertIn('"failed"', posted[0][1])

    def test_a_run_started_outside_any_conversation_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            state.agent_adapter = mock.Mock(**{"status.return_value": {}, "context_packet.return_value": {}})
            record = _record("", "", "succeeded")
            record.pop("agent_session_id")
            record.pop("agent_conversation_id")
            _notify_agent_session_resource_action_done(state, record)
            state.agent_adapter.post_background_result.assert_not_called()

    def test_an_unreachable_agent_server_still_leaves_the_transcript_note(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = _state(Path(tmp_dir))
            session = _create_agent_session(state, {"title": "bg"})
            state.agent_adapter = mock.Mock(**{"status.return_value": {}, "context_packet.return_value": {}})
            state.agent_adapter.post_background_result = mock.Mock(
                return_value={"sent": False, "reason": "down"}
            )
            _notify_agent_session_resource_action_done(
                state, _record(session["id"], "conv-1", "succeeded")
            )
            refreshed = _require_agent_session(state, session["id"])
            messages = server._read_agent_messages(state, session["id"])
        self.assertTrue(
            any("finished" in str(m.get("content", "")) for m in messages)
        )
        # but the session must NOT claim an agent is coming
        self.assertNotEqual(refreshed.get("status"), "waiting_for_agent")


if __name__ == "__main__":
    unittest.main()
