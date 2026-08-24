"""A model that promises to wait for delivered results gets one nudge.

The tool acknowledgement already tells the model, in as many words, that every
result arrives in the same turn and nothing will prompt it again. A small
model read that and still ended its turn with "give me a moment -- let me
confirm which resource to use". To the person this is indistinguishable from
a hang, and it was reported live three separate times.

The words are the only signal there is: turn finished, results delivered,
message promising future work. So the dispatch sends one -- exactly one --
continuation telling the model the results are already above, and takes its
next answer. Approval waits never match: stopping for a person is correct.
"""

from __future__ import annotations

import unittest
from unittest import mock

from optpilot_studio.agent import OpenHandsAdapter, OpenHandsRuntimeConfig


class WaitingNarrationDetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())

    def test_the_live_narrations_match(self) -> None:
        for text in (
            "I'm checking the available simulator-generation resource and "
            "setting up the workspace for output. Give me a moment — let me "
            "confirm which resource to use.",
            "The results are being dispatched. Let me wait for the actual "
            "results — they should arrive shortly.",
            "I've dispatched searches for the COOPA method; the search "
            "results haven't come back to me in this exchange.",
        ):
            with self.subTest(text=text[:40]):
                self.assertTrue(self.adapter._looks_like_waiting_narration(text))

    def test_legitimate_stops_do_not_match(self) -> None:
        for text in (
            "I need your approval before launching this Run.",
            "The launch is awaiting your approval in the card above.",
            "Please approve the generate action so I can continue.",
            "The Run is queued and will start when capacity frees up.",
            "Here is the best candidate compared to the baseline.",
            "",
            None,
        ):
            with self.subTest(text=str(text)[:40]):
                self.assertFalse(self.adapter._looks_like_waiting_narration(text))


class NudgeIsBoundedTest(unittest.TestCase):
    """One nudge, and a second narration is surfaced rather than looped on."""

    def _dispatch(self, answers):
        adapter = OpenHandsAdapter(
            config=OpenHandsRuntimeConfig(
                enabled=True,
                model="m",
                api_key="k",
                base_url="http://127.0.0.1:1",
            )
        )
        polls = []
        for text in answers:
            polls.append(
                (
                    text,
                    [{"type": "optpilot_tool_result", "payload": {"ok": True}}],
                    "",
                    "",
                )
            )
        sent = []

        def fake_request(method, url, payload=None, **kwargs):
            if method == "POST" and url.endswith("/events") and payload:
                sent.append(payload)
            return {}, {}

        with mock.patch.object(
            adapter, "_poll_openhands_answer", side_effect=polls
        ) as poll, mock.patch.object(
            adapter, "_request_json", side_effect=fake_request
        ), mock.patch.object(
            adapter, "_existing_openhands_events", return_value=[]
        ):
            result = adapter._dispatch_openhands_agent_server(
                "prompt", {}, "conv-1", None, set()
            )
        return result, poll.call_count, sent

    def test_a_waiting_answer_is_nudged_once_and_replaced(self) -> None:
        result, poll_count, sent = self._dispatch(
            [
                "Give me a moment — let me confirm which resource to use.",
                "Done: the workspace is ready and generation has been requested.",
            ]
        )
        self.assertEqual(poll_count, 2)
        nudges = [
            p for p in sent
            if "already been returned above" in str(p.get("content"))
        ]
        self.assertEqual(len(nudges), 1)
        self.assertIn(
            "generation has been requested",
            result["assistant_message"]["content"],
        )

    def test_a_second_narration_is_surfaced_not_looped(self) -> None:
        result, poll_count, sent = self._dispatch(
            [
                "Give me a moment — let me confirm which resource to use.",
                "Let me wait for the actual results before continuing.",
            ]
        )
        self.assertEqual(poll_count, 2)  # never a third poll
        nudges = [
            p for p in sent
            if "already been returned above" in str(p.get("content"))
        ]
        self.assertEqual(len(nudges), 1)

    def test_a_real_answer_is_never_nudged(self) -> None:
        result, poll_count, sent = self._dispatch(
            ["Here is the plan: three candidates were evaluated."]
        )
        self.assertEqual(poll_count, 1)
        self.assertEqual(
            [p for p in sent if "already been returned" in str(p)], []
        )


if __name__ == "__main__":
    unittest.main()
