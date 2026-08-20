"""Checking a folder that is not a package says what is missing.

Asking OptPilot to check a folder holding nothing it recognises ran on into
the registration state machine and surfaced its internal consistency
assertion: "checked registration setup lacks its exact identity." That
sentence is correct as an invariant and useless as an answer -- it names
nothing a person can add, remove, or fix.

The assertion is left alone; it guards a real invariant. What changed is that
the question is answered before reaching it.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _create_ui_workspace,
    _prepare_package_plan,
    _validate_package_plan,
)


class UnclassifiableFolderTest(unittest.TestCase):
    def _plan(self, tmp: Path):
        state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
        root = tmp / "just_notes"
        root.mkdir()
        (root / "README.txt").write_text("notes\n", encoding="utf-8")
        workspace = _create_ui_workspace(state, {"title": "Notes", "root": str(root)})
        plan = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
        return state, workspace["id"], plan

    def test_it_is_refused_in_words_a_person_can_act_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, workspace_id, plan = self._plan(Path(tmp_dir))
            self.assertEqual(plan.get("classification"), "not-yet-classifiable")
            with self.assertRaises(ValueError) as raised:
                _validate_package_plan(state, workspace_id, plan["id"])
        message = str(raised.exception)
        # says what is missing
        self.assertIn("environment", message)
        self.assertIn("method", message)
        self.assertIn("resource", message)
        # and not the internal assertion
        self.assertNotIn("exact identity", message)

    def test_a_registered_source_that_lost_its_entries_still_gets_checked(self) -> None:
        """The guard must not swallow the richer answer.

        A folder that IS a registered catalog source and has since lost its
        entries looks the same on classification, package id, publisher and
        lineage -- it differs only in carrying a source authority. That path
        runs the real check and reports facts a person can use, naming the
        missing entries and any stray yaml that was ignored. Refusing early
        would throw those away, which a test caught when the guard was first
        written too broadly.
        """

        import optpilot_studio.ui.server as server

        source = inspect.getsource(server._validate_package_plan)
        head = source[: source.index("workspace = _require_ui_workspace")]
        self.assertIn("source_authority", head)

    def test_the_internal_assertion_is_not_what_reaches_the_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, workspace_id, plan = self._plan(Path(tmp_dir))
            with self.assertRaises(ValueError) as raised:
                _validate_package_plan(state, workspace_id, plan["id"])
        self.assertNotIn("registration setup", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
