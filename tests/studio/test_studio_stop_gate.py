"""A turn may end only in a shape the protocol recognises, never on wording.

OpenHands finishes a run on ANY plain assistant message. A model that
narrates "I'm awaiting the tool results" therefore ends its turn, which the
person reads as a hang -- reported live four times, each with fresh wording,
which is why the previous lexical detector lost. The Stop-hook gate replaces
it: the stop is allowed when the current run ended with the `finish` tool or
when any dispatched OptPilot call in the turn is still owed its result
(paired by call id), and denied with corrective feedback -- at most twice
per turn -- otherwise.

The gate reads the agent-server's on-disk event files because Stop hooks
execute on the server's only event loop -- an HTTP callback would deadlock.
The adapter-side pieces reviewed here: the gate registration in the
conversation payload (with the fail-open readability guard, since a missing
script would otherwise make python itself exit 2, the DENY code), the
recreation of pre-hook conversations, the recency guards that keep stale
finishes and stale stuck statuses from leaking across runs, and the
retirement of a finish batched with a dispatch.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot_studio import stop_gate
from optpilot_studio.agent import (
    OpenHandsAdapter,
    OpenHandsConversationNotFound,
    OpenHandsRuntimeConfig,
)


def _user_message(text: str, timestamp: str = "") -> dict:
    event = {
        "kind": "MessageEvent",
        "source": "user",
        "llm_message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    if timestamp:
        event["timestamp"] = timestamp
    return event


def _agent_message(text: str, timestamp: str = "") -> dict:
    event = {
        "kind": "MessageEvent",
        "source": "agent",
        "llm_message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if timestamp:
        event["timestamp"] = timestamp
    return event


def _feedback_message() -> dict:
    return {
        "kind": "MessageEvent",
        "source": "environment",
        "llm_message": {
            "role": "user",
            "content": [{"type": "text", "text": "[Stop hook feedback] continue"}],
        },
    }


def _action(tool_name: str, action_kind: str = "", call_id: str = "") -> dict:
    event = {
        "kind": "ActionEvent",
        "source": "agent",
        "tool_name": tool_name,
        "action": {"kind": action_kind or "Action", "message": "done"},
    }
    if call_id:
        event["tool_call_id"] = call_id
    return event


def _result(tool_name: str, call_id: str) -> dict:
    return _user_message(
        f"OptPilot tool result for {tool_name} ({call_id}). "
        "Use this structured result to continue the task.\n```json\n{}\n```"
    )


class DecideTest(unittest.TestCase):
    def test_the_live_stall_is_denied(self) -> None:
        # The dispatched call got its result (paired by call id) and the
        # model answered it with a waiting narration and no tool call.
        events = [
            _user_message("Generate a restaurant simulation."),
            _action("optpilot_catalog_search", call_id="call-1"),
            _result("optpilot_catalog_search", "call-1"),
            _agent_message(
                "The workspace restaurant-sim is created. I'm awaiting the "
                "tool results to proceed with running the generation there."
            ),
        ]
        allow, feedback = stop_gate.decide(events)
        self.assertFalse(allow)
        self.assertIn("finish", feedback)

    def test_a_finish_call_is_allowed(self) -> None:
        events = [
            _user_message("Generate a restaurant simulation."),
            _action("finish", "FinishAction"),
        ]
        self.assertEqual(stop_gate.decide(events), (True, ""))

    def test_finish_recognised_by_action_kind_alone(self) -> None:
        events = [_user_message("hi"), _action("", "FinishAction")]
        self.assertEqual(stop_gate.decide(events), (True, ""))

    def test_a_finish_from_an_earlier_run_does_not_bless_this_stop(self) -> None:
        # The finish ended the run BEFORE the tool result arrived; the run
        # that the result started owes its own ending.
        events = [
            _user_message("Generate a restaurant simulation."),
            _action("optpilot_catalog_search", call_id="call-1"),
            _action("finish", "FinishAction"),
            _result("optpilot_catalog_search", "call-1"),
            _agent_message("Let me get back to you once things settle."),
        ]
        allow, _feedback = stop_gate.decide(events)
        self.assertFalse(allow)

    def test_a_pending_dispatch_is_allowed(self) -> None:
        # The model dispatched a Studio tool; no result for it has been
        # posted, so Studio still owes the wake-up. Approval pauses produce
        # exactly this shape too.
        events = [
            _user_message("Generate a restaurant simulation."),
            _action("optpilot_resource_action_run", call_id="call-1"),
            _agent_message("The generation has been requested."),
        ]
        self.assertEqual(stop_gate.decide(events), (True, ""))

    def test_a_partially_answered_multi_dispatch_is_allowed(self) -> None:
        # Three dispatches, one result so far: the result post must not hide
        # the two calls still owed their results (live trace 05cd7ec2 shape).
        events = [
            _user_message("Check compatibility and validate the configs."),
            _action("optpilot_compatibility_check", call_id="call-1"),
            _action("optpilot_config_validate", call_id="call-2"),
            _action("optpilot_config_validate", call_id="call-3"),
            _result("optpilot_compatibility_check", "call-1"),
            _agent_message("I still need the config validation results."),
        ]
        self.assertEqual(stop_gate.decide(events), (True, ""))

    def test_a_fully_answered_turn_is_denied_on_a_plain_stop(self) -> None:
        events = [
            _user_message("Check compatibility."),
            _action("optpilot_compatibility_check", call_id="call-1"),
            _result("optpilot_compatibility_check", "call-1"),
            _agent_message("Let me wait for things to settle."),
        ]
        allow, _feedback = stop_gate.decide(events)
        self.assertFalse(allow)

    def test_a_background_outcome_post_does_not_close_the_turn(self) -> None:
        # The background outcome arrives as a user-role post; the turn stays
        # open and the fully answered dispatch no longer excuses a plain stop.
        events = [
            _user_message("Run the generation."),
            _action("optpilot_resource_action_run", call_id="call-1"),
            _result("optpilot_resource_action_run", "call-1"),
            _user_message(
                "OptPilot background action result for generate "
                "(request req-1). Status: succeeded."
            ),
            _agent_message("I'll summarise once everything is in."),
        ]
        allow, _feedback = stop_gate.decide(events)
        self.assertFalse(allow)

    def test_a_native_tool_call_does_not_excuse_the_stop(self) -> None:
        events = [
            _user_message("hi"),
            _action("think", call_id="call-1"),
            _agent_message("Working on it."),
        ]
        allow, _feedback = stop_gate.decide(events)
        self.assertFalse(allow)

    def test_two_denials_is_the_cap_and_result_posts_do_not_reset_it(self) -> None:
        events = [
            _user_message("hi"),
            _agent_message("I'll wait."),
            _feedback_message(),
            _action("optpilot_catalog_search", call_id="call-1"),
            _result("optpilot_catalog_search", "call-1"),
            _agent_message("Still waiting."),
        ]
        self.assertFalse(stop_gate.decide(events)[0])
        events.append(_feedback_message())
        events.append(_agent_message("Waiting even so."))
        self.assertEqual(stop_gate.decide(events), (True, ""))

    def test_a_new_person_message_resets_the_denial_count(self) -> None:
        events = [
            _user_message("hi"),
            _feedback_message(),
            _feedback_message(),
            _user_message("And another question."),
            _agent_message("I'll wait."),
        ]
        allow, _feedback = stop_gate.decide(events)
        self.assertFalse(allow)

    def test_garbage_events_are_tolerated(self) -> None:
        events = [None, "text", {"kind": "MessageEvent"}, {}, _agent_message("hm")]
        allow, feedback = stop_gate.decide(events)  # no crash is the point
        self.assertIsInstance(allow, bool)
        self.assertIsInstance(feedback, str)


class MainTest(unittest.TestCase):
    def _run(self, stdin_payload, conversations_root: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch.object(
            stop_gate.sys, "stdin", io.StringIO(json.dumps(stdin_payload))
        ), contextlib.redirect_stderr(stderr):
            code = stop_gate.main(["--conversations-root", conversations_root])
        return code, stderr.getvalue()

    def _write_events(self, root: Path, conversation_id: str, events) -> None:
        events_dir = root / conversation_id.replace("-", "") / "events"
        events_dir.mkdir(parents=True)
        for index, event in enumerate(events):
            path = events_dir / f"event-{index:05d}-e{index}.json"
            path.write_text(json.dumps(event), encoding="utf-8")

    def test_a_stall_earns_exit_2_with_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conversation_id = "f24b7677-22cb-4d0c-9703-0f0c522a7e03"
            self._write_events(
                Path(tmp),
                conversation_id,
                [_user_message("go"), _agent_message("I'll wait for the results.")],
            )
            code, err = self._run(
                {"event_type": "Stop", "session_id": conversation_id}, tmp
            )
        self.assertEqual(code, 2)
        self.assertIn("finish", err)

    def test_a_finish_ending_earns_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conversation_id = "f24b7677-22cb-4d0c-9703-0f0c522a7e03"
            self._write_events(
                Path(tmp),
                conversation_id,
                [_user_message("go"), _action("finish", "FinishAction")],
            )
            code, _err = self._run(
                {"event_type": "Stop", "session_id": conversation_id}, tmp
            )
        self.assertEqual(code, 0)

    def test_event_files_are_read_as_utf_8(self) -> None:
        # The server writes raw UTF-8; a locale-default read on another host
        # would drop the finish event and deny a legitimate stop.
        with tempfile.TemporaryDirectory() as tmp:
            conversation_id = "f24b7677-22cb-4d0c-9703-0f0c522a7e03"
            finish = _action("finish", "FinishAction")
            finish["action"]["message"] = "Done — c'est fini ✅"
            self._write_events(Path(tmp), conversation_id, [_user_message("go"), finish])
            code, _err = self._run(
                {"event_type": "Stop", "session_id": conversation_id}, tmp
            )
        self.assertEqual(code, 0)

    def test_everything_unexpected_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for payload in (
                {"event_type": "PreToolUse", "session_id": "abc"},
                {"event_type": "Stop", "session_id": ""},
                {"event_type": "Stop", "session_id": "no-such-conversation"},
                "not a dict",
            ):
                code, _err = self._run(payload, tmp)
                self.assertEqual(code, 0, payload)


class PayloadWiringTest(unittest.TestCase):
    """The conversation-creation payload registers the gate as a Stop hook."""

    def _payload(self, tmp: str) -> dict:
        adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())
        with mock.patch.dict(
            "os.environ",
            {"OPTPILOT_OPENHANDS_CONVERSATIONS_DIR": str(Path(tmp) / "conversations")},
        ):
            return adapter._start_conversation_payload({})

    def test_hook_config_points_at_a_stable_gate_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(tmp)
            hook_config = payload.get("hook_config")
            self.assertIsInstance(hook_config, dict)
            matchers = hook_config["stop"]
            self.assertEqual(matchers[0]["matcher"], "*")
            hook = matchers[0]["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertLessEqual(hook["timeout"], 15)
            command = hook["command"]
            # The command survives this checkout disappearing: the gate is
            # copied next to the conversations it reads, and an unreadable
            # gate allows the stop instead of deny-looping (python itself
            # exits 2 -- the deny code -- on a missing script).
            self.assertTrue(command.startswith("[ -r "))
            self.assertIn("|| exit 0; ", command)
            self.assertIn("--conversations-root", command)
            stable_copy = Path(tmp) / "optpilot_stop_gate.py"
            self.assertTrue(stable_copy.exists())
            self.assertIn(str(stable_copy), command)
            self.assertEqual(payload.get("max_iterations"), 40)


class StaleEventGuardsTest(unittest.TestCase):
    """Output from an earlier run never masquerades as the current one."""

    def setUp(self) -> None:
        self.adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())

    def test_a_finish_older_than_the_newest_user_message_is_stale(self) -> None:
        finish = {
            "id": "evt-1",
            "kind": "ActionEvent",
            "source": "agent",
            "timestamp": "2026-08-24T10:00:01",
            "action": {"kind": "FinishAction", "message": "Here is what I found."},
        }
        result_post = _user_message("OptPilot tool result for x (call-1). {}")
        result_post["timestamp"] = "2026-08-24T10:00:05"
        self.assertEqual(
            self.adapter._best_finish_text([result_post, finish], set(), set()), ""
        )
        finish["timestamp"] = "2026-08-24T10:00:09"
        self.assertEqual(
            self.adapter._best_finish_text([finish, result_post], set(), set()),
            "Here is what I found.",
        )

    def test_a_stale_stuck_status_does_not_fail_a_resumed_turn(self) -> None:
        stuck = {
            "id": "evt-1",
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": "stuck",
            "timestamp": "2026-08-24T10:00:01",
        }
        wake = _user_message(
            "OptPilot background action result for generate (request r1)."
        )
        wake["timestamp"] = "2026-08-24T10:00:05"
        running = {
            "id": "evt-3",
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": "running",
            "timestamp": "2026-08-24T10:00:06",
        }
        self.assertEqual(
            self.adapter._best_runtime_error([running, wake, stuck], set()), ""
        )

    def test_a_current_stuck_status_is_a_terminal_error(self) -> None:
        prompt = _user_message("go")
        prompt["timestamp"] = "2026-08-24T10:00:01"
        stuck = {
            "id": "evt-2",
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": "stuck",
            "timestamp": "2026-08-24T10:00:09",
        }
        error = self.adapter._best_runtime_error([stuck, prompt], set())
        self.assertIn("repeating itself", error)

    def test_hook_execution_events_never_fail_the_turn(self) -> None:
        hook_event = {
            "id": "evt-1",
            "kind": "HookExecutionEvent",
            "error": "Hook timed out after 10 seconds",
        }
        self.assertEqual(self.adapter._best_runtime_error([hook_event], set()), "")


class RetirementPersistsTest(unittest.TestCase):
    """A finish batched with a dispatch is retired in the caller's own set."""

    def test_the_retired_finish_id_lands_in_the_callers_set(self) -> None:
        finish_event = {
            "id": "evt-finish",
            "kind": "ActionEvent",
            "source": "agent",
            "action": {"kind": "FinishAction", "message": "Premature done."},
        }
        tool_event = {
            "id": "evt-tool",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "optpilot_file_read",
            "tool_call_id": "call-1",
            "tool_call": {
                "id": "call-1",
                "name": "optpilot_file_read",
                "arguments": json.dumps({"path": "a.py"}),
            },
        }

        class Adapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(OpenHandsRuntimeConfig(enabled=False))

            def _request_json(self, method, url, *, payload=None, timeout=10.0):
                if method == "GET" and "events/search" in url:
                    return {"items": [finish_event, tool_event]}, {}
                if method == "POST" and url.endswith("/events"):
                    return {}, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = Adapter()
        caller_ignored: set = set()
        caller_handled: set = set()
        adapter._poll_openhands_answer(
            "http://openhands.example/api/conversations",
            "conversation-1",
            tool_executor=lambda name, arguments: {"ok": True},
            ignored_tool_calls=caller_handled,
            ignored_event_ids=caller_ignored,
            poll_seconds=0.2,
        )
        self.assertIn("evt-finish", caller_ignored)
        self.assertIn("call-1", caller_handled)


class PreHookConversationTest(unittest.TestCase):
    """A conversation stored without the Stop hook is recreated on dispatch."""

    def test_dispatch_raises_not_found_for_a_gateless_conversation(self) -> None:
        class Adapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(
                    OpenHandsRuntimeConfig(
                        enabled=True,
                        model="m",
                        api_key="k",
                        base_url="http://127.0.0.1:1",
                    )
                )

            def _request_json(self, method, url, *, payload=None, timeout=10.0):
                if method == "GET" and url.endswith("/conversation-old"):
                    return {"id": "conversation-old", "hook_config": None}, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = Adapter()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "OPTPILOT_OPENHANDS_CONVERSATIONS_DIR": str(
                        Path(tmp) / "conversations"
                    )
                },
            ):
                with self.assertRaises(OpenHandsConversationNotFound):
                    adapter._dispatch_openhands_agent_server(
                        "prompt", {}, "conversation-old", None, set()
                    )

    def test_an_unreachable_record_fails_open_to_normal_dispatch(self) -> None:
        calls = []

        class Adapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(
                    OpenHandsRuntimeConfig(
                        enabled=True,
                        model="m",
                        api_key="k",
                        base_url="http://127.0.0.1:1",
                    )
                )

            def _request_json(self, method, url, *, payload=None, timeout=10.0):
                calls.append((method, url))
                raise OSError("connection refused")

        adapter = Adapter()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {
                    "OPTPILOT_OPENHANDS_CONVERSATIONS_DIR": str(
                        Path(tmp) / "conversations"
                    )
                },
            ):
                with self.assertRaises(OSError):
                    # The gate-state probe swallows its error and dispatch
                    # proceeds to the events fetch, which then fails on its
                    # own terms -- the probe never converts an outage into a
                    # conversation reset.
                    adapter._dispatch_openhands_agent_server(
                        "prompt", {}, "conversation-old", None, set()
                    )
        self.assertTrue(any("events" in url for _method, url in calls))


if __name__ == "__main__":
    unittest.main()
