"""Dialog footers stay reachable however many of their children are showing.

The Settings dialog's Save button could not be seen or clicked. The dialog
laid its children out in numbered grid rows, but rows are handed to the
children that are actually *visible*, and the dialog hides its loading banner
once loading finishes. Every later child then moved up a row: the scrolling
body landed in a row sized to its content, and the footer landed in the
flexible row, squeezed to nothing, leaving the Save button below the dialog's
own edge -- clipped by overflow:hidden, with no scrollbar to reach it.

Two sibling dialogs had the same latent defect from the other direction: they
reveal an error line only when something failed, so their footers would be
pushed out exactly when a person most needs the buttons.

A column layout has no numbered rows to shift, so this asserts these dialogs
use one rather than asserting the symptom.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_CSS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "styles.css"
)

#: Every dialog that stacks a header, a scrolling body and a footer of buttons,
#: and that hides or reveals at least one child depending on state.
_COLUMN_DIALOGS = (
    "settings-modal",
    "registration-confirmation-modal",
    "child-run-confirmation-modal",
)


def _rule_body(css: str, selector: str) -> str:
    match = re.search(rf"^\.{re.escape(selector)} \{{(.*?)^\}}", css, re.M | re.S)
    assert match, f"no rule for .{selector}"
    return match.group(1)


class DialogLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _CSS.read_text(encoding="utf-8")

    def test_these_dialogs_are_columns(self) -> None:
        for selector in _COLUMN_DIALOGS:
            with self.subTest(dialog=selector):
                body = _rule_body(self.css, selector)
                self.assertIn("flex-direction: column", body)

    def test_none_of_them_assigns_children_to_numbered_rows(self) -> None:
        # This is the defect itself: a row template silently renumbers when a
        # child is hidden.
        for selector in _COLUMN_DIALOGS:
            with self.subTest(dialog=selector):
                body = _rule_body(self.css, selector)
                self.assertNotIn(
                    "grid-template-rows",
                    body,
                    f".{selector} must not position children by row; a hidden "
                    "child shifts every later one and clips the footer",
                )

    def test_the_scrolling_body_is_what_absorbs_leftover_height(self) -> None:
        for selector in ("settings-body", "registration-confirmation-body",
                         "child-run-confirmation-body"):
            with self.subTest(body=selector):
                body = _rule_body(self.css, selector)
                self.assertIn("min-height: 0", body)
                self.assertIn("overflow: auto", body)
        self.assertRegex(
            self.css,
            r"\.settings-body,\s*\n\s*\.registration-confirmation-body,\s*\n"
            r"\s*\.child-run-confirmation-body \{\s*\n\s*flex: 1 1 auto;",
        )

    def test_footers_keep_their_own_height(self) -> None:
        # Without this a tall body can shrink the footer and hide the buttons
        # again, by a different route.
        for selector in _COLUMN_DIALOGS:
            with self.subTest(dialog=selector):
                self.assertIn(f".{selector} > .modal-actions", self.css)


if __name__ == "__main__":
    unittest.main()
