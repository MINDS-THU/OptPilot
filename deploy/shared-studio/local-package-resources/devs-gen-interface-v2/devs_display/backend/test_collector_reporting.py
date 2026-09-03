from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from .collector_reporting import CollectorReporter


class CollectorReporterTests(unittest.TestCase):
    def test_unconfigured_reporter_is_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(CollectorReporter.from_environment(lambda *_: {}))

    def test_reports_are_coalesced_and_snapshot_request_is_preserved(self) -> None:
        built: list[tuple[str, bool]] = []
        sent: list[dict] = []

        def build(session_id: str, include_snapshots: bool) -> dict:
            built.append((session_id, include_snapshots))
            return {
                "participant_id": "participant_test",
                "session": {"session_id": session_id},
            }

        reporter = CollectorReporter(
            endpoint="http://collector.invalid",
            token="secret",
            source="optpilot",
            bundle_factory=build,
            debounce_seconds=0.05,
            sender=sent.append,
        )
        reporter.schedule("sess_one")
        reporter.schedule("sess_one", include_snapshots=True)
        reporter.schedule("sess_one")
        self.assertTrue(reporter.wait_until_idle())
        self.assertEqual(built, [("sess_one", True)])
        self.assertEqual(sent[0]["source"], "optpilot")


if __name__ == "__main__":
    unittest.main()
