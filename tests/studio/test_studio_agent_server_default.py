"""OptPilot knows where its own helper listens; it does not ask.

The Settings dialog offered an empty "OpenHands server URL" box. Leaving it
empty did not mean "use the local one" -- it silently selected a different
mode in which the Assistant answers with no tools at all, so it could not read
the Catalog, prepare a Run, or do any of the work it exists for. Which port an
internal service uses is wiring, not a preference, and a person had no way to
know the answer.

The URL now defaults to the helper OptPilot starts alongside Studio. The
toolless mode still exists, but has to be asked for by name.
"""

from __future__ import annotations

import unittest

from optpilot_studio.agent import (
    DEFAULT_OPENHANDS_BASE_URL,
    OPENHANDS_NO_SERVER_VALUES,
    OpenHandsAdapter,
    OpenHandsRuntimeConfig,
    resolve_openhands_base_url,
)


class AgentServerDefaultTest(unittest.TestCase):
    def test_an_unset_url_means_the_local_helper(self) -> None:
        for empty in ("", None, "   "):
            with self.subTest(value=empty):
                self.assertEqual(
                    resolve_openhands_base_url(empty), DEFAULT_OPENHANDS_BASE_URL
                )

    def test_an_explicit_url_is_kept(self) -> None:
        self.assertEqual(
            resolve_openhands_base_url("http://elsewhere:9000/"),
            "http://elsewhere:9000",
        )

    def test_the_toolless_mode_must_be_asked_for_by_name(self) -> None:
        for value in OPENHANDS_NO_SERVER_VALUES:
            with self.subTest(value=value):
                self.assertEqual(resolve_openhands_base_url(value), "")
                self.assertEqual(resolve_openhands_base_url(value.upper()), "")

    def test_a_configured_assistant_with_no_url_uses_its_tools(self) -> None:
        config = OpenHandsRuntimeConfig.from_mapping(
            {"enabled": True, "model": "some/model", "api_key": "k", "base_url": ""}
        )
        status = OpenHandsAdapter(config=config).status()
        self.assertEqual(status["base_url"], DEFAULT_OPENHANDS_BASE_URL)
        self.assertEqual(status["dispatch"], "openhands_http")


class DefaultDoesNotSwitchTheAssistantOnTest(unittest.TestCase):
    """Filling the URL in must not look like the person configured anything.

    `enabled` is derived from whether anything was configured. Defaulting the
    URL before that check would make every fresh install look configured and
    switch the Assistant on with no model and no key.
    """

    def test_nothing_configured_stays_off(self) -> None:
        import os
        from unittest import mock

        cleared = {
            key: ""
            for key in os.environ
            if key.startswith(("OPTPILOT_OPENHANDS", "LLM_", "OPENAI_"))
        }
        with mock.patch.dict(os.environ, cleared, clear=False):
            for key in cleared:
                os.environ.pop(key, None)
            config = OpenHandsRuntimeConfig.from_env()
        self.assertFalse(config.enabled)


class UnreachableHelperTest(unittest.TestCase):
    """A helper that is not running says so, in words that name the fix."""

    def test_connection_failures_are_recognised(self) -> None:
        adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())
        for error in (
            ConnectionError("refused"),
            TimeoutError("timed out"),
            OSError("Max retries exceeded with url: /api/conversations"),
            Exception("[Errno 61] Connection refused"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(adapter._is_agent_server_unreachable(error))

    def test_unrelated_failures_are_not_mistaken_for_it(self) -> None:
        adapter = OpenHandsAdapter(config=OpenHandsRuntimeConfig())
        for error in (ValueError("bad model id"), KeyError("missing")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(adapter._is_agent_server_unreachable(error))


if __name__ == "__main__":
    unittest.main()
