"""Scaffolding between Studio and the model never appears as the reply.

OptPilot's tools run in Studio, so their results are posted back into the
conversation as a message the model reads: "OptPilot tool result for <tool>
(<call id>)" followed by the raw JSON. That text is addressed to the model.

Surfacing one as the Assistant's answer puts a wall of JSON where a reply
belongs. It stayed hidden while a separate defect made the model stop before
it ever got that far; fixing that exposed this immediately.
"""

from __future__ import annotations

import unittest

from optpilot_studio.agent import OpenHandsAdapter, OpenHandsRuntimeConfig


class ToolFeedbackNeverShownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())

    def test_a_tool_result_message_is_not_an_answer(self) -> None:
        feedback = (
            "OptPilot tool result for optpilot_catalog_detail (call_abc123). "
            "Use this structured result to continue the task.\n"
            '```json\n{"data": {"id": "solve-or-problem"}}\n```'
        )
        self.assertEqual(self.adapter._user_facing_assistant_text(feedback), "")

    def test_leading_whitespace_does_not_smuggle_it_through(self) -> None:
        self.assertEqual(
            self.adapter._user_facing_assistant_text(
                "\n  OptPilot tool result for optpilot_run_list (call_1). ..."
            ),
            "",
        )

    def test_a_real_answer_still_comes_through(self) -> None:
        answer = "The run setup needs one input: the problem statement."
        self.assertEqual(self.adapter._user_facing_assistant_text(answer), answer)

    def test_an_answer_that_merely_mentions_a_tool_result_survives(self) -> None:
        # The guard keys on the message Studio itself posts, not on the words.
        answer = "I read the OptPilot tool result for the catalog lookup: it needs one input."
        self.assertEqual(self.adapter._user_facing_assistant_text(answer), answer)


if __name__ == "__main__":
    unittest.main()


class ToolFeedbackDoesNotHoldTheTurnOpenTest(unittest.TestCase):
    """A tool result must not look like a fresh request from the person.

    A turn is treated as finished when the agent's "finished" status is newer
    than the latest user message -- a guard so a leftover status from the
    previous turn cannot close the new one. But Studio posts every tool result
    into the conversation AS a user message, so a result landing after the
    agent had already finished made that comparison unwinnable. The session sat
    on "Working" indefinitely with the finished answer sitting unread beside
    it: observed live, agent reporting execution_status=finished and a written
    reply present, while Studio showed nothing for twelve minutes.
    """

    def setUp(self) -> None:
        self.adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())

    @staticmethod
    def _status(value: str, at: str) -> dict:
        return {
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": value,
            "timestamp": at,
        }

    @staticmethod
    def _user(text: str, at: str) -> dict:
        return {
            "kind": "MessageEvent",
            "source": "user",
            "timestamp": at,
            "llm_message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }

    def test_a_result_posted_after_the_finish_does_not_reopen_the_turn(self) -> None:
        events = [
            self._user(
                "OptPilot tool result for optpilot_catalog_list (call_1). ...",
                "2026-08-20T00:00:05Z",
            ),
            self._status("finished", "2026-08-20T00:00:03Z"),
            self._user("Solve one problem with COOPA.", "2026-08-20T00:00:01Z"),
        ]
        self.assertTrue(self.adapter._execution_finished(events))

    def test_a_real_new_request_still_holds_the_turn_open(self) -> None:
        # The guard's original purpose: a stale finish must not close a turn
        # whose request was only just posted.
        events = [
            self._user("Now launch it please.", "2026-08-20T00:00:05Z"),
            self._status("finished", "2026-08-20T00:00:03Z"),
        ]
        self.assertFalse(self.adapter._execution_finished(events))
