"""The key for the configured model never crosses the network in the clear.

Studio lets a person point the Assistant at any address, and sends the key for
the configured model to it as a bearer token. Nothing checked that the address
was encrypted, so pointing it at a plain-HTTP service on another machine put a
working credential on the wire in readable form -- and a key taken off the wire
is valid everywhere, not only here.

Addresses on this machine are exempt on purpose. Nothing is on the wire there,
and demanding certificates for a locally-run service would only teach people to
turn the check off.
"""

from __future__ import annotations

import unittest

from optpilot_studio.agent import require_encrypted_transport_for_secret


class KeyTransportTest(unittest.TestCase):
    def test_a_remote_plain_address_is_refused(self) -> None:
        for url in (
            "http://openrouter.ai/api/v1/chat/completions",
            "http://192.168.1.50:8080/v1",
            "http://example.com/agent",
        ):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError):
                    require_encrypted_transport_for_secret(url)

    def test_the_refusal_explains_the_risk_and_the_fix(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            require_encrypted_transport_for_secret("http://example.com/agent")
        message = str(caught.exception)
        self.assertIn("another machine", message)
        self.assertIn("readable form", message)
        self.assertIn("https", message)

    def test_an_encrypted_remote_address_is_allowed(self) -> None:
        require_encrypted_transport_for_secret("https://openrouter.ai/api/v1")

    def test_this_machine_is_allowed_without_encryption(self) -> None:
        for url in (
            "http://localhost:3000/api",
            "http://127.0.0.1:8000/v1",
            "http://[::1]:8000/v1",
            "http://agent.localhost:9000/x",
        ):
            with self.subTest(url=url):
                require_encrypted_transport_for_secret(url)

    def test_the_check_sits_where_the_key_is_attached(self) -> None:
        # A check somewhere else could be bypassed by a new call site; this
        # one runs immediately before the Authorization header is set.
        from pathlib import Path

        import optpilot_studio.agent as agent

        source = Path(agent.__file__).read_text(encoding="utf-8")
        guard = source.index("require_encrypted_transport_for_secret(url)")
        header = source.index('headers["Authorization"] = f"Bearer {bearer_token}"')
        self.assertLess(guard, header)


if __name__ == "__main__":
    unittest.main()
