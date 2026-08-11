"""Focused checks for WP1A summary and controller-event persistence."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from optpilot.storage import LocalEvidenceStore


class LocalEvidenceStoreAtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LocalEvidenceStore(Path(self.temporary.name), "atomic-summary")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_summary_replacement_is_atomic_and_leaves_no_temporary_file(self) -> None:
        self.store.write_summary({"schema_version": "v2", "revision": 1})
        self.store.write_summary({"schema_version": "v2", "revision": 2})

        self.assertEqual(self.store.read_summary()["revision"], 2)
        self.assertEqual(list(self.store.run_dir.glob(".summary.json.*.tmp")), [])

    def test_failed_replace_preserves_previous_summary_and_cleans_staging(self) -> None:
        self.store.write_summary({"revision": 1})
        with mock.patch("optpilot.storage.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.store.write_summary({"revision": 2})

        self.assertEqual(self.store.read_summary(), {"revision": 1})
        self.assertEqual(list(self.store.run_dir.glob(".summary.json.*.tmp")), [])

    def test_concurrent_summary_writes_always_leave_one_complete_document(self) -> None:
        payloads = [{"schema_version": "v2", "revision": revision} for revision in range(20)]
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(self.store.write_summary, payloads))

        self.assertIn(self.store.read_summary(), payloads)
        self.assertEqual(list(self.store.run_dir.glob(".summary.json.*.tmp")), [])

    def test_controller_events_have_a_separate_append_only_stream(self) -> None:
        self.store.record_controller_event({"event": "proposal.accepted", "revision": 1})
        self.store.record_controller_event({"event": "run.terminal", "revision": 2})

        self.assertEqual(
            [event["event"] for event in self.store.read_controller_events()],
            ["proposal.accepted", "run.terminal"],
        )

    def test_study_name_cannot_escape_run_root(self) -> None:
        root = Path(self.temporary.name) / "bounded-runs"
        escaped = LocalEvidenceStore(root, "../../outside/unsafe study")

        self.assertEqual(escaped.run_dir.parent, root.resolve())
        self.assertTrue(escaped.run_dir.name.startswith("outside-unsafe-study-"))
        self.assertFalse((Path(self.temporary.name) / "outside").exists())

    def test_trial_ids_are_validated_before_becoming_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe internal path key"):
            self.store.create_trial_workspace("../escaped", attempt_index=1)
        with self.assertRaisesRegex(ValueError, "safe internal path key"):
            self.store.create_trial_workspace("CON")
        with self.assertRaisesRegex(ValueError, "safe internal path key"):
            self.store.create_trial_workspace("trial.")
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.store.create_trial_workspace("trial-safe", attempt_index=0)

        workspace = self.store.create_trial_workspace("trial-safe", attempt_index=2)
        self.assertEqual(workspace.relative_to(self.store.run_dir).as_posix(), "trials/trial-safe/attempt-2")


if __name__ == "__main__":
    unittest.main()
