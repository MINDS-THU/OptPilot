"""A smoke test is the one execution the Assistant may start without asking.

Writing a config, checking it, running it small, and fixing what broke is a
single loop. Every turn of it used to raise an approval card, because a smoke
test borrowed the permission meant for real launches -- so the person was
interrupted to authorise the very step that tells the Assistant whether its
last edit worked.

Letting it run unasked is only defensible because the run cannot outlive the
question it answers, and that is what these tests hold: a throwaway copy of
the package, a handful of trials, a wall-clock cap, and a Realm that is
deleted afterwards. Before this, an absent trial count meant *no* limit and
*no* copy -- the least bounded behaviour was the default one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    ASSISTANT_PERMISSION_VALUES,
    ASSISTANT_SMOKE_DEFAULT_TRIALS,
    ASSISTANT_SMOKE_MAX_TRIALS,
    DEFAULT_ASSISTANT_PERMISSIONS,
    _assistant_smoke_trial_limit,
    _prepare_assistant_smoke_package,
)
import yaml


class SmokePermissionTest(unittest.TestCase):
    def test_a_smoke_test_does_not_ask_by_default(self) -> None:
        self.assertEqual(
            DEFAULT_ASSISTANT_PERMISSIONS["smoke_test"], "safe_without_approval"
        )

    def test_the_person_can_still_require_being_asked_or_forbid_it(self) -> None:
        self.assertEqual(
            ASSISTANT_PERMISSION_VALUES["smoke_test"],
            {"approval_required", "safe_without_approval", "disabled"},
        )

    def test_it_no_longer_borrows_the_launch_permission(self) -> None:
        # A real launch writes into the person's permanent record; a smoke
        # test does not. They must be separately settable, or turning one off
        # turns off the other.
        self.assertIn("study_launch", DEFAULT_ASSISTANT_PERMISSIONS)
        self.assertNotEqual(
            DEFAULT_ASSISTANT_PERMISSIONS["study_launch"],
            DEFAULT_ASSISTANT_PERMISSIONS["smoke_test"],
        )


class SmokeBoundsTest(unittest.TestCase):
    def test_an_unstated_trial_count_still_gets_a_limit(self) -> None:
        for unstated in (None, 0, "", "not a number", -4):
            with self.subTest(unstated=unstated):
                self.assertEqual(
                    _assistant_smoke_trial_limit(unstated),
                    ASSISTANT_SMOKE_DEFAULT_TRIALS,
                )

    def test_a_large_request_is_capped(self) -> None:
        self.assertEqual(
            _assistant_smoke_trial_limit(10_000), ASSISTANT_SMOKE_MAX_TRIALS
        )

    def test_a_modest_request_is_honoured(self) -> None:
        self.assertEqual(_assistant_smoke_trial_limit(5), 5)

    def test_the_smoke_runs_a_copy_and_leaves_the_package_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            package = tmp / "package"
            (package / "studies").mkdir(parents=True)
            study = package / "studies" / "demo.yaml"
            original = {"kind": "study", "budget": {"maxTrials": 500}}
            study.write_text(yaml.safe_dump(original), encoding="utf-8")

            workspace = tmp / "work"
            workspace.mkdir()
            copied_package, copied_study = _prepare_assistant_smoke_package(
                package_root=package,
                study_path=study,
                temporary_root=workspace,
                max_trials=_assistant_smoke_trial_limit(None),
            )

            self.assertNotEqual(copied_package.resolve(), package.resolve())
            self.assertEqual(
                yaml.safe_load(study.read_text(encoding="utf-8")),
                original,
                "the person's own study must be untouched",
            )
            self.assertEqual(
                yaml.safe_load(copied_study.read_text(encoding="utf-8"))["budget"][
                    "maxTrials"
                ],
                ASSISTANT_SMOKE_DEFAULT_TRIALS,
            )


if __name__ == "__main__":
    unittest.main()
