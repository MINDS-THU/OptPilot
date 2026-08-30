"""The key for the configured model never crosses the network in the clear.

Studio lets a person point the Assistant at any address, and can send the key
for the configured model either inside the OpenHands conversation body or as a
bearer token. Both channels must be guarded before a remote request is made.

Addresses on this machine are exempt on purpose. Nothing is on the wire there,
and demanding certificates for a locally-run service would only teach people to
turn the check off.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request

from optpilot_studio.agent import (
    OpenHandsAdapter,
    OpenHandsRuntimeConfig,
    _RejectCredentialRedirects,
    require_encrypted_transport_for_secret,
)


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

    def test_a_model_key_in_the_conversation_body_is_guarded_before_io(self) -> None:
        adapter = OpenHandsAdapter(
            OpenHandsRuntimeConfig(
                enabled=True,
                base_url="http://agent.example.com:8781",
                session_endpoint="/api/conversations",
                model="provider/model",
                api_key="sk-body-secret",
            )
        )
        payload = adapter._start_conversation_payload({})

        with patch("optpilot_studio.agent.urlopen") as request:
            with self.assertRaises(RuntimeError) as caught:
                adapter._request_json(
                    "POST",
                    "http://agent.example.com:8781/api/conversations",
                    payload=payload,
                )

        self.assertIn("https", str(caught.exception))
        request.assert_not_called()

    def test_https_to_http_redirect_cannot_receive_authorization(self) -> None:
        request = Request(
            "https://agent.example.com/api/conversations",
            headers={"Authorization": "Bearer sk-redirect-secret"},
        )

        with self.assertRaises(RuntimeError) as caught:
            _RejectCredentialRedirects().redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://agent.example.com/api/conversations",
            )

        message = str(caught.exception)
        self.assertIn("https://agent.example.com", message)
        self.assertIn("http://agent.example.com", message)
        self.assertIn("non-redirecting", message)

    def test_cross_origin_redirect_cannot_receive_api_key(self) -> None:
        request = Request(
            "https://agent.example.com/api/conversations",
            data=b'{"api_key":"sk-redirect-secret"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(RuntimeError) as caught:
            _RejectCredentialRedirects().redirect_request(
                request,
                None,
                307,
                "Temporary Redirect",
                {},
                "https://other.example.net/api/conversations",
            )

        message = str(caught.exception)
        self.assertIn("agent.example.com", message)
        self.assertIn("other.example.net", message)

    def test_valid_direct_authenticated_call_still_works(self) -> None:
        adapter = OpenHandsAdapter(OpenHandsRuntimeConfig(enabled=True))
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = [("X-Test", "direct")]

        with patch(
            "optpilot_studio.agent._open_credential_request",
            return_value=response,
        ) as opener:
            data, headers = adapter._request_json(
                "POST",
                "https://agent.example.com/api/conversations",
                payload={"model": {"api_key": "sk-direct-secret"}},
                bearer_token="bridge-token",
            )

        self.assertEqual(data, {"ok": True})
        self.assertEqual(headers, {"X-Test": "direct"})
        opened_request = opener.call_args.args[0]
        self.assertEqual(opened_request.full_url, "https://agent.example.com/api/conversations")
        self.assertEqual(opened_request.get_header("Authorization"), "Bearer bridge-token")
        opener.assert_called_once_with(opened_request, timeout=60.0)


if __name__ == "__main__":
    unittest.main()
