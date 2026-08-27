"""A conversation that lost its credential is recreated, not blamed on the key.

The agent-server persists a conversation but not its API key -- meta.json
records agent/llm/api_key = null. So after the agent-server restarts, every
turn in an existing conversation is sent unauthenticated and OpenRouter
answers 401 "No cookie auth credentials found". Verified live: the key was
valid (HTTP 200 against OpenRouter, no cap), a brand-new conversation
answered "pong" immediately, switch_llm returned 200 yet repaired nothing,
and the person was told to check a key that was never the problem.

An unusable conversation is reported the way a deleted one is, so the caller
recreates it with the turn's context. The recreated conversation carries the
credential, so a genuinely bad key still surfaces as an authentication
failure rather than looping.
"""

from __future__ import annotations

import unittest
from unittest import mock

from optpilot_studio.agent import (
    OpenHandsAdapter,
    OpenHandsConversationNotFound,
    OpenHandsRuntimeConfig,
)


def _adapter() -> OpenHandsAdapter:
    return OpenHandsAdapter(
        config=OpenHandsRuntimeConfig(
            enabled=True, model="z-ai/glm-4.7", api_key="k",
            base_url="http://127.0.0.1:1",
        )
    )


AUTH_EVENT = {
    "kind": "ConversationErrorEvent",
    "code": "LLMAuthenticationError",
    "classification": {"kind": "auth", "retryable": False},
    "detail": 'OpenrouterException - {"error":{"message":"No cookie auth credentials found","code":401}}',
}
OTHER_EVENT = {
    "kind": "ConversationErrorEvent",
    "code": "MaxIterationsReached",
    "classification": {"kind": "limit", "retryable": False},
    "detail": "Agent reached maximum iterations limit (40).",
}


class CredentialLossDetectionTest(unittest.TestCase):
    def test_an_auth_classified_error_is_recognised(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter, "_request_json", return_value=({"items": [AUTH_EVENT]}, {})
        ):
            self.assertTrue(
                adapter._conversation_lost_its_credential("http://x/api/conversations", "c1")
            )

    def test_other_failures_are_not_credential_loss(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter, "_request_json", return_value=({"items": [OTHER_EVENT]}, {})
        ):
            self.assertFalse(
                adapter._conversation_lost_its_credential("http://x/api/conversations", "c1")
            )

    def test_an_unreadable_conversation_is_never_recreated_on_a_guess(self) -> None:
        # Recreating discards server-side history; doubt must answer False.
        adapter = _adapter()
        with mock.patch.object(
            adapter, "_request_json", side_effect=OSError("connection refused")
        ):
            self.assertFalse(
                adapter._conversation_lost_its_credential("http://x/api/conversations", "c1")
            )


class DispatchRecoveryTest(unittest.TestCase):
    def _dispatch(self, adapter, conversation_id):
        return adapter._dispatch_openhands_agent_server(
            "prompt", {}, conversation_id, None, set()
        )

    def test_an_existing_conversation_that_lost_its_key_is_reported_missing(self) -> None:
        adapter = _adapter()
        with mock.patch.object(
            adapter, "_poll_openhands_answer",
            return_value=("", [], "LLMAuthenticationError: OpenrouterException", ""),
        ), mock.patch.object(
            adapter, "_request_json", return_value=({"items": [AUTH_EVENT]}, {})
        ), mock.patch.object(
            adapter, "_existing_openhands_events", return_value=[]
        ), mock.patch.object(
            adapter, "_conversation_stop_gate_state", return_value=True
        ):
            with self.assertRaises(OpenHandsConversationNotFound) as caught:
                self._dispatch(adapter, "conversation-old")
        self.assertEqual(caught.exception.conversation_id, "conversation-old")

    def test_a_fresh_conversation_surfaces_the_auth_failure_instead_of_looping(self) -> None:
        # conversation_id=None means this call created it, so the credential
        # was supplied: a 401 here is a real bad key and must be reported.
        adapter = _adapter()
        created = {"id": "conversation-new"}
        with mock.patch.object(
            adapter, "_poll_openhands_answer",
            return_value=("", [], "LLMAuthenticationError: OpenrouterException", ""),
        ), mock.patch.object(
            adapter, "_request_json", return_value=(created, {})
        ), mock.patch.object(
            adapter, "_existing_openhands_events", return_value=[]
        ):
            result = self._dispatch(adapter, None)
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
