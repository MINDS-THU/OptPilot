"""The Assistant's guidance file exists twice, and both copies must agree.

Studio reads the copy inside the installed package; a source checkout also
carries one under `.agents/`, which is what a developer opens and edits. They
are byte-identical by intention and nothing checked it, so editing the one in
front of you left the one actually in use saying the opposite -- a failure
that shows up only as the Assistant behaving from instructions no one can
find in the file they just read.

The same trap has been hit repeatedly in this repository: two copies of docs,
two copies of a method, two copies of this file. Each time the fix looked
complete because one copy was verified.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "assistant_assets"
    / "prompts"
    / "system.md"
)
_CHECKOUT = _ROOT / ".agents" / "optpilot-assistant" / "prompts" / "system.md"


class GuidanceMirrorTest(unittest.TestCase):
    def test_both_copies_exist(self) -> None:
        self.assertTrue(_SHIPPED.is_file(), _SHIPPED)
        self.assertTrue(_CHECKOUT.is_file(), _CHECKOUT)

    def test_both_copies_are_identical(self) -> None:
        shipped = _SHIPPED.read_text(encoding="utf-8")
        checkout = _CHECKOUT.read_text(encoding="utf-8")
        self.assertEqual(
            shipped,
            checkout,
            "the two copies of the Assistant guidance have drifted; edit the "
            "one under studio/src/optpilot_studio/assistant_assets/ and copy "
            "it to .agents/optpilot-assistant/prompts/system.md",
        )


if __name__ == "__main__":
    unittest.main()
