from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.agent import (
    OpenHandsAdapter,
    OpenHandsConversationNotFound,
    OpenHandsRuntimeConfig,
)
from optpilot_studio.ui.server import (
    UiState,
    _agent_session_by_id,
    _append_agent_message,
    _append_jsonl,
    _create_agent_session,
    _read_agent_events,
    _read_agent_messages,
    _require_agent_session,
    _sync_agent_session,
    _upsert_agent_session,
)


class _RecoveredAdapter:
    def __init__(self) -> None:
        self.calls = []

    def status(self):
        return {
            "runtime": "openhands",
            "dispatch": "openhands_http",
            "available_tools": [],
        }

    def context_packet(self, **kwargs):
        return dict(kwargs)

    def dispatch_message(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "answered",
            "dispatch": "openhands_http",
            "conversation_id": "oh-recreated",
            "assistant_message": {
                "role": "assistant",
                "title": "OpenHands",
                "content": "Continued in a fresh Assistant runtime.",
            },
            "events": [],
        }


class StudioOpenHandsStaleRecoveryTests(unittest.TestCase):
    def test_adapter_reports_missing_bound_conversation_to_studio_dispatch(self) -> None:
        adapter = OpenHandsAdapter(
            OpenHandsRuntimeConfig(
                enabled=True,
                base_url="http://openhands.example",
                session_endpoint="/api/conversations",
                model="test-model",
                api_key="test-key",
            )
        )
        configured = {
            "configured": True,
            "dispatch": "openhands_http",
            "mode": "configured",
        }
        with (
            patch.object(adapter, "status", return_value=configured),
            patch.object(
                adapter,
                "_request_json",
                side_effect=RuntimeError(
                    "HTTP 404 from http://openhands.example/api/conversations/oh-old/events/search: Conversation not found"
                ),
            ) as request_json,
        ):
            result = adapter.dispatch_message(
                message="Continue.",
                context={},
                conversation_id="oh-old",
            )

        self.assertEqual(result["status"], "conversation_missing")
        self.assertEqual(result["conversation_id"], "oh-old")
        # Two calls, no retries: the stop-gate probe (which swallows its
        # error and fails open) and the events fetch that reports the
        # conversation missing.
        self.assertEqual(request_json.call_count, 2)

    def test_adapter_stops_polling_as_soon_as_bound_conversation_is_missing(self) -> None:
        adapter = OpenHandsAdapter(
            OpenHandsRuntimeConfig(
                enabled=True,
                base_url="http://openhands.example",
                session_endpoint="/api/conversations",
                model="test-model",
                api_key="test-key",
            )
        )
        configured = {
            "configured": True,
            "dispatch": "openhands_http",
            "mode": "configured",
        }
        started = time.monotonic()
        with (
            patch.object(adapter, "status", return_value=configured),
            patch.object(
                adapter,
                "_request_json",
                side_effect=RuntimeError(
                    "HTTP 404 from http://openhands.example/api/conversations/oh-old/events/search: Conversation not found"
                ),
            ) as request_json,
        ):
            with self.assertRaises(OpenHandsConversationNotFound):
                adapter.sync_conversation("oh-old", poll_seconds=30.0)

        self.assertEqual(request_json.call_count, 1)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_sync_clears_only_stale_runtime_binding_and_keeps_studio_history(self) -> None:
        class MissingSyncAdapter:
            def sync_conversation(self, conversation_id: str, **kwargs):
                raise OpenHandsConversationNotFound(conversation_id)

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            state.agent_adapter = MissingSyncAdapter()
            session = _create_agent_session(state, {"title": "Retained history"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {
                    "id": "msg-before-restart",
                    "role": "user",
                    "title": "User",
                    "content": "Please keep this request in Studio history.",
                    "source": "user",
                },
            )
            retained = _require_agent_session(state, session["id"])
            retained.update(
                {
                    "status": "waiting_for_agent",
                    "openhands_conversation_id": "oh-old",
                    "openhands_workspace_id": "ws-old",
                    "openhands_pending_sync": {"ignored_event_ids": ["evt-old"]},
                    "active_turn_id": "turn-old",
                    "active_turn_started_at": "2026-08-05T01:00:00Z",
                }
            )
            _upsert_agent_session(state, retained)

            synced = _sync_agent_session(state, session["id"])
            stored = _require_agent_session(state, session["id"])
            messages = _read_agent_messages(state, session["id"])
            events = _read_agent_events(state, session["id"])

        self.assertEqual(synced["status"], "idle")
        self.assertEqual(stored.get("openhands_conversation_id"), "")
        self.assertNotIn("openhands_workspace_id", stored)
        self.assertNotIn("openhands_pending_sync", stored)
        self.assertNotIn("active_turn_id", stored)
        self.assertTrue(
            any(
                message.get("content")
                == "Please keep this request in Studio history."
                for message in messages
            )
        )
        self.assertTrue(
            any(message.get("title") == "Assistant restarted" for message in messages)
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in events
                    if event.get("type") == "openhands_conversation_missing"
                ]
            ),
            1,
        )

    def test_next_message_recreates_runtime_with_bounded_studio_history(self) -> None:
        class MissingSyncAdapter:
            def sync_conversation(self, conversation_id: str, **kwargs):
                raise OpenHandsConversationNotFound(conversation_id)

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Continue after restart"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {
                    "id": "msg-original",
                    "role": "user",
                    "title": "User",
                    "content": "Original retained request.",
                    "source": "user",
                },
            )
            retained = _require_agent_session(state, session["id"])
            retained.update(
                {
                    "status": "waiting_for_agent",
                    "openhands_conversation_id": "oh-old",
                    "active_turn_id": "turn-old",
                }
            )
            _upsert_agent_session(state, retained)
            state.agent_adapter = MissingSyncAdapter()
            _sync_agent_session(state, session["id"])

            adapter = _RecoveredAdapter()
            state.agent_adapter = adapter
            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Continue the request."},
            )
            stored = _require_agent_session(state, session["id"])
            messages = _read_agent_messages(state, session["id"])

        self.assertEqual(result["session"]["status"], "idle")
        self.assertEqual(stored["openhands_conversation_id"], "oh-recreated")
        self.assertEqual(len(adapter.calls), 1)
        self.assertIsNone(adapter.calls[0]["conversation_id"])
        recent = adapter.calls[0]["context"]["conversation"]["recent_messages"]
        self.assertTrue(
            any(item["content"] == "Original retained request." for item in recent)
        )
        self.assertFalse(any("Assistant service restarted" in item["content"] for item in recent))
        self.assertTrue(
            any(
                message.get("content") == "Continued in a fresh Assistant runtime."
                for message in messages
            )
        )

    def test_idle_stale_binding_is_recreated_during_same_user_send(self) -> None:
        class RetryAdapter(_RecoveredAdapter):
            def dispatch_message(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "status": "conversation_missing",
                        "dispatch": "openhands_http",
                        "conversation_id": "oh-old",
                        "assistant_message": {"content": ""},
                        "events": [],
                    }
                return {
                    "status": "answered",
                    "dispatch": "openhands_http",
                    "conversation_id": "oh-recreated",
                    "assistant_message": {
                        "role": "assistant",
                        "title": "OpenHands",
                        "content": "Recovered without losing the Conversation.",
                    },
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            adapter = RetryAdapter()
            state.agent_adapter = adapter
            session = _create_agent_session(state, {"title": "Idle stale binding"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {
                    "id": "msg-prior",
                    "role": "user",
                    "title": "User",
                    "content": "Earlier Studio message.",
                    "source": "user",
                },
            )
            retained = _require_agent_session(state, session["id"])
            retained["openhands_conversation_id"] = "oh-old"
            retained["openhands_workspace_id"] = ""
            _upsert_agent_session(state, retained)

            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "New request after restart."},
            )
            stored = _require_agent_session(state, session["id"])
            messages = _read_agent_messages(state, session["id"])
            events = _read_agent_events(state, session["id"])

        self.assertEqual(result["session"]["status"], "idle")
        self.assertEqual(stored["openhands_conversation_id"], "oh-recreated")
        self.assertEqual(
            [call["conversation_id"] for call in adapter.calls], ["oh-old", None]
        )
        retry_recent = adapter.calls[1]["context"]["conversation"]["recent_messages"]
        self.assertTrue(any(item["content"] == "Earlier Studio message." for item in retry_recent))
        self.assertFalse(any(item["content"] == "New request after restart." for item in retry_recent))
        self.assertEqual(
            len(
                [
                    message
                    for message in messages
                    if message.get("content") == "New request after restart."
                ]
            ),
            1,
        )
        self.assertTrue(
            any(event.get("type") == "openhands_conversation_recreated" for event in events)
        )


if __name__ == "__main__":
    unittest.main()
