"""Focused projection checks retained after the Realm runner cutover."""

from __future__ import annotations

import json
import unittest

from optpilot.runner import _method_observation_payload


class RunnerControllerIntegrationTests(unittest.TestCase):
    def test_method_view_does_not_receive_raw_unsupported_status(self) -> None:
        operator_payload = {
            "status": "failed",
            "event_summary": {
                "error": {
                    "code": "unsupported_observation_status",
                    "raw_status": "provider-specific-mystery",
                    "message": "unsupported 'provider-specific-mystery'",
                },
                "errors": [
                    {
                        "code": "unsupported_observation_status",
                        "raw_status": "provider-specific-mystery",
                        "message": "unsupported 'provider-specific-mystery'",
                    }
                ],
            },
        }
        method_payload = _method_observation_payload(operator_payload)

        self.assertNotIn("provider-specific-mystery", json.dumps(method_payload))
        self.assertEqual(
            method_payload["event_summary"]["error"]["code"],
            "unsupported_observation_status",
        )
        self.assertEqual(len(method_payload["event_summary"]["errors"]), 1)
        self.assertEqual(
            method_payload["event_summary"]["errors"][0]["code"],
            "unsupported_observation_status",
        )


if __name__ == "__main__":
    unittest.main()
