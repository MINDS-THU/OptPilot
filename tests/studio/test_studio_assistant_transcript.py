"""Client transcript stability: a send must never flash the onboarding page.

A concurrent session payload (sync poll, workspace-attachment event, session
create) can be built server-side before a just-sent message's append lands on
disk. These source assertions pin the two client behaviors that keep the
conversation surface stable through that race:

1. ``mergeAgentSessionPayload`` merges server messages with pending local
   (unconfirmed) messages instead of replacing the transcript wholesale.
2. ``conversationHasStarted`` is sticky per session, so a transient
   transcript refresh cannot flip an active conversation back to onboarding.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _function_source(source: str, declaration: str, next_declaration: str) -> str:
    start = source.index(declaration)
    end = source.index(next_declaration, start)
    return source[start:end]


class AssistantTranscriptStabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _APP_JS.read_text(encoding="utf-8")

    def test_session_payload_merge_preserves_pending_local_messages(self) -> None:
        merge = _function_source(
            self.source,
            "function mergeAgentSessionPayload(",
            "function adoptWorkspacePreviewToolResults(",
        )
        self.assertIn('id.startsWith("local-")', merge)
        self.assertIn("pendingLocal", merge)
        self.assertIn("[...serverMessages, ...pendingLocal]", merge)
        self.assertNotIn(
            "state.assistantMessagesBySession[session.id] = session.messages.map(",
            merge,
            "Server payloads must not replace the transcript wholesale.",
        )

    def test_conversation_started_is_sticky_per_session(self) -> None:
        started = _function_source(
            self.source,
            "function conversationHasStarted(",
            "function renderConversationOnboarding(",
        )
        self.assertIn("state.startedAgentSessionIds.has(session.id)", started)
        self.assertIn("state.startedAgentSessionIds.add(session.id)", started)
        self.assertIn("startedAgentSessionIds: new Set()", self.source)
        start = self.source.index("function forgetAgentSessionLocalState(")
        end = self.source.index("function ", start + 1)
        forget = self.source[start:end]
        self.assertIn("state.startedAgentSessionIds.delete(sessionId)", forget)


if __name__ == "__main__":
    unittest.main()
