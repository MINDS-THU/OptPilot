from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _agent_session_by_id,
    _agent_title_from_request,
    _append_agent_message,
    _attach_agent_workspace,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _normalized_agent_sessions,
    _read_agent_events,
    _read_agent_session_index,
    _sanitize_agent_conversation_title,
    _select_agent_workspace,
    _write_agent_session_index,
)


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"


class _QuietAdapter:
    def status(self):
        return {"runtime": "test", "dispatch": "test", "available_tools": []}

    def context_packet(self, **kwargs):
        return dict(kwargs)

    def dispatch_message(self, **kwargs):
        return {
            "status": "answered",
            "dispatch": "test",
            "assistant_message": {
                "role": "assistant",
                "title": "Assistant",
                "content": "Done.",
            },
            "events": [],
        }


class StudioConversationTitleTests(unittest.TestCase):
    def _state(self, root: str) -> UiState:
        state = UiState(cwd=Path(root), catalog_roots=[], run_roots=[])
        state.agent_adapter = _QuietAdapter()
        return state

    def test_first_substantive_request_gets_an_immediate_fallback_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            greeting = _create_agent_session(state, {})
            _append_agent_message(
                state,
                greeting["id"],
                {"role": "user", "content": "Hello"},
            )
            substantive = _create_agent_session(state, {})
            _append_agent_message(
                state,
                substantive["id"],
                {
                    "role": "user",
                    "content": "Please help me improve the Conversation list layout.",
                },
            )

            greeting_after = _agent_session_by_id(state, greeting["id"])
            substantive_after = _agent_session_by_id(state, substantive["id"])

        self.assertEqual(greeting_after["title"], "Untitled conversation")
        self.assertEqual(
            substantive_after["title"], "Improve the Conversation list layout"
        )
        self.assertEqual(substantive_after["title_origin"], "fallback")

    def test_existing_user_title_is_never_automatically_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            session = _create_agent_session(state, {"title": "Scheduling research"})
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Now investigate the DEVS interface."},
            )
            tool_result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_conversation_title",
                {"title": "DEVS Interface"},
            )
            after = _agent_session_by_id(state, session["id"])

        self.assertTrue(tool_result["ok"])
        self.assertIn("user-defined", tool_result["summary"])
        self.assertEqual(after["title"], "Scheduling research")
        self.assertEqual(after["title_origin"], "user")

    def test_existing_assistant_turn_can_refine_title_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            session = _create_agent_session(state, {})
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Improve the Conversation list layout."},
            )
            first = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_conversation_title",
                {"title": "Conversation List UX"},
            )
            second = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_conversation_title",
                {"title": "A Different Name"},
            )
            after = _agent_session_by_id(state, session["id"])

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(after["title"], "Conversation List UX")
        self.assertEqual(after["title_origin"], "assistant")

    def test_title_tool_update_survives_the_dispatch_final_write(self) -> None:
        class NamingAdapter(_QuietAdapter):
            def dispatch_message(self, **kwargs):
                result = kwargs["tool_executor"](
                    "optpilot_conversation_title",
                    {"title": "DEVS Generator UX"},
                )
                self.assert_tool_result = result
                return super().dispatch_message(**kwargs)

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            adapter = NamingAdapter()
            state.agent_adapter = adapter
            session = _create_agent_session(state, {})
            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Improve the DEVS Generator experience."},
            )

        self.assertTrue(adapter.assert_tool_result["ok"])
        self.assertEqual(result["session"]["title"], "DEVS Generator UX")
        self.assertEqual(result["session"]["status"], "idle")

    def test_workspace_attachment_and_title_survive_the_dispatch_final_write(self) -> None:
        class WorkspaceNamingAdapter(_QuietAdapter):
            def dispatch_message(self, **kwargs):
                self.workspace_result = kwargs["tool_executor"](
                    "optpilot_workspace_create",
                    {"title": "Queue simulator"},
                )
                self.title_result = kwargs["tool_executor"](
                    "optpilot_conversation_title",
                    {"title": "Queue Simulator Work"},
                )
                return super().dispatch_message(**kwargs)

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            adapter = WorkspaceNamingAdapter()
            state.agent_adapter = adapter
            session = _create_agent_session(state, {})
            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Build a restaurant queue simulator."},
            )

        workspace_id = adapter.workspace_result["data"]["workspace"]["id"]
        self.assertTrue(adapter.workspace_result["ok"])
        self.assertTrue(adapter.title_result["ok"])
        self.assertEqual(result["session"]["title"], "Queue Simulator Work")
        self.assertEqual(result["session"]["attached_workspace_ids"], [workspace_id])
        self.assertEqual(result["session"]["selected_workspace_id"], workspace_id)

    def test_changing_default_workspace_rebinds_runtime_and_keeps_recent_context(self) -> None:
        class RebindingAdapter(_QuietAdapter):
            def __init__(self) -> None:
                self.calls = []

            def dispatch_message(self, **kwargs):
                self.calls.append(kwargs)
                sequence = len(self.calls)
                return {
                    "status": "answered",
                    "dispatch": "openhands_http",
                    "conversation_id": f"openhands-{sequence}",
                    "assistant_message": {
                        "role": "assistant",
                        "title": "Assistant",
                        "content": f"Finished turn {sequence}.",
                    },
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            adapter = RebindingAdapter()
            state.agent_adapter = adapter
            first_workspace = _create_ui_workspace(
                state, {"title": "First project"}
            )
            second_workspace = _create_ui_workspace(
                state, {"title": "Second project"}
            )
            session = _create_agent_session(state, {})
            _attach_agent_workspace(
                state, session["id"], first_workspace["id"], select=True
            )

            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Inspect the first project."},
            )
            _attach_agent_workspace(
                state, session["id"], second_workspace["id"], select=False
            )
            _select_agent_workspace(state, session["id"], second_workspace["id"])
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Continue in the second project."},
            )
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Now inspect its README."},
            )
            final_session = _agent_session_by_id(state, session["id"])
            events = _read_agent_events(state, session["id"])

        self.assertEqual(
            [call.get("conversation_id") for call in adapter.calls],
            [None, None, "openhands-2"],
        )
        self.assertEqual(
            adapter.calls[0]["context"]["selected_workspace"]["id"],
            first_workspace["id"],
        )
        self.assertEqual(
            adapter.calls[1]["context"]["selected_workspace"]["id"],
            second_workspace["id"],
        )
        recent = adapter.calls[1]["context"]["conversation"]["recent_messages"]
        self.assertEqual(
            [(item["role"], item["content"]) for item in recent],
            [
                ("user", "Inspect the first project."),
                ("assistant", "Finished turn 1."),
            ],
        )
        self.assertEqual(
            final_session["openhands_workspace_id"], second_workspace["id"]
        )
        workspace_changes = [
            event for event in events if event.get("type") == "assistant_workspace_changed"
        ]
        self.assertEqual(len(workspace_changes), 1)
        self.assertEqual(
            workspace_changes[0]["payload"]["workspace_id"], second_workspace["id"]
        )

    def test_legacy_generic_title_is_migrated_from_first_substantive_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            session = _create_agent_session(state, {"title": "Session 12"})
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Hello"},
            )
            _append_agent_message(
                state,
                session["id"],
                {"role": "user", "content": "Review the scheduling Run results."},
            )
            stored = _read_agent_session_index(state)
            for item in stored:
                if item.get("id") == session["id"]:
                    item["title"] = "Conversation 12"
                    item.pop("title_origin", None)
            _write_agent_session_index(state, stored)

            normalized = _normalized_agent_sessions(state)
            migrated = next(item for item in normalized if item["id"] == session["id"])

        self.assertEqual(migrated["title"], "Review the scheduling Run results")
        self.assertEqual(migrated["title_origin"], "fallback")

    def test_legacy_generated_title_is_cleaned_when_conversations_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = self._state(tmp_dir)
            session = _create_agent_session(state, {})
            stored = _read_agent_session_index(state)
            for item in stored:
                if item.get("id") == session["id"]:
                    item["title"] = (
                        "Open and explore a simulator, adjust its inputs,"
                    )
                    item["title_origin"] = "fallback"
            _write_agent_session_index(state, stored)

            normalized = _normalized_agent_sessions(state)
            migrated = next(item for item in normalized if item["id"] == session["id"])

        self.assertEqual(
            migrated["title"], "Open and explore a simulator, adjust its inputs"
        )

    def test_title_text_is_bounded_and_markup_free(self) -> None:
        self.assertEqual(
            _sanitize_agent_conversation_title("Title: <b>DEVS</b> Generator UX\n"),
            "DEVS Generator UX",
        )
        self.assertEqual(_sanitize_agent_conversation_title("Conversation 9"), "")
        self.assertLessEqual(
            len(
                _sanitize_agent_conversation_title(
                    "one two three four five six seven eight nine ten eleven"
                )
            ),
            64,
        )
        self.assertEqual(_agent_title_from_request("continue"), "")
        self.assertEqual(
            _sanitize_agent_conversation_title(
                "Open and explore a simulator, adjust its inputs, and inspect results"
            ),
            "Open and explore a simulator, adjust its inputs",
        )


class StudioConversationListStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (_STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (_STATIC / "index.html").read_text(encoding="utf-8")
        cls.styles = (_STATIC / "styles.css").read_text(encoding="utf-8")

    def test_list_has_no_total_badge_and_uses_two_compact_rows(self) -> None:
        self.assertNotIn('id="conversationCount"', self.html)
        self.assertNotIn('"conversationCount"', self.app)
        self.assertIn('class="agent-session-title"', self.app)
        self.assertIn('class="agent-session-meta"', self.app)
        self.assertIn("font-family: inherit", self.styles)

    def test_internal_title_tool_is_not_shown_as_activity(self) -> None:
        self.assertIn(
            'if (payload.tool === "optpilot_conversation_title") return false;',
            self.app,
        )


if __name__ == "__main__":
    unittest.main()
