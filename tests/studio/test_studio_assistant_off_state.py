"""When the Assistant cannot answer, it says so instead of appearing to work.

The home page's main invitation is a chat box. On a fresh install nothing is
configured behind it, and a message sent there used to produce a note headed
"Queued locally" saying the message "was stored" but "the OpenHands runtime is
disabled" -- naming a component the person has never heard of, giving no way
forward, and leaving the conversation marked as waiting for a reply that could
never come. It stayed that way across restarts.

Two things are pinned here: the wording a person actually reads, and the fact
that the turn is finished rather than pending.
"""

from __future__ import annotations

import unittest

from optpilot_studio.agent import OpenHandsAdapter, OpenHandsRuntimeConfig


def _notice(config: OpenHandsRuntimeConfig) -> dict:
    adapter = OpenHandsAdapter(config=config)
    return adapter._queued_result(adapter.status())


class AssistantOffNoticeTest(unittest.TestCase):
    CASES = {
        "disabled": OpenHandsRuntimeConfig(enabled=False),
        "missing model": OpenHandsRuntimeConfig(enabled=True, model="", api_key="k"),
        "missing API key": OpenHandsRuntimeConfig(enabled=True, model="m", api_key=""),
    }

    def test_each_reason_gets_its_own_notice(self) -> None:
        seen = set()
        for expected_mode, config in self.CASES.items():
            with self.subTest(mode=expected_mode):
                result = _notice(config)
                self.assertEqual(result["mode"], expected_mode)
                seen.add(result["assistant_message"]["content"])
        self.assertEqual(len(seen), len(self.CASES), "the reasons must differ")

    def test_the_notice_says_nothing_is_coming(self) -> None:
        for mode, config in self.CASES.items():
            with self.subTest(mode=mode):
                content = _notice(config)["assistant_message"]["content"]
                self.assertIn("no one is reading this", content)

    def test_the_notice_says_what_to_do(self) -> None:
        for mode, config in self.CASES.items():
            with self.subTest(mode=mode):
                content = _notice(config)["assistant_message"]["content"]
                self.assertIn("Settings", content)
                self.assertIn("send your message again", content.lower())

    def test_the_notice_avoids_words_only_we_know(self) -> None:
        # The person did not install "OpenHands" and cannot act on its name.
        for mode, config in self.CASES.items():
            with self.subTest(mode=mode):
                message = _notice(config)["assistant_message"]
                text = f"{message['title']} {message['content']}".lower()
                for jargon in ("openhands", "runtime", "dispatch", "queued locally"):
                    self.assertNotIn(jargon, text)

    def test_an_unknown_reason_still_gets_a_useful_notice(self) -> None:
        adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig(enabled=False))
        content = adapter._queued_result({"mode": "something new"})[
            "assistant_message"
        ]["content"]
        self.assertIn("Settings", content)
        self.assertNotIn("something new", content)


class QueuedTurnFinishesTest(unittest.TestCase):
    """A message nothing will answer must not leave the conversation busy."""

    def test_the_send_path_treats_queued_as_finished(self) -> None:
        from pathlib import Path

        server = (
            Path(__file__).resolve().parents[2]
            / "studio"
            / "src"
            / "optpilot_studio"
            / "ui"
            / "server.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'if dispatch_status in {"answered", "dispatched", "queued"}:',
            server,
            "a queued dispatch must settle the session, not leave it waiting",
        )


if __name__ == "__main__":
    unittest.main()
