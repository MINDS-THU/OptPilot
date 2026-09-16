"""The Assistant's optional result rating stays explicit and unobtrusive."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"


class StudioAssistantActionRatingStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = (_STATIC / "app.js").read_text(encoding="utf-8")
        cls.styles = (_STATIC / "styles.css").read_text(encoding="utf-8")

    def test_rating_is_a_direct_user_action_not_an_assistant_prompt(self) -> None:
        self.assertIn("function bindAssistantResourceActionRatings()", self.app)
        self.assertIn('data-resource-action-rating-toggle', self.app)
        self.assertIn('addEventListener("submit"', self.app)
        self.assertIn("/resource-actions/${encodeURIComponent(requestId)}/evaluation", self.app)
        self.assertNotIn("ask the assistant to rate", self.app.lower())

    def test_compact_form_keeps_extra_dimensions_optional(self) -> None:
        self.assertIn("How was this result?", self.app)
        self.assertIn("Rate result", self.app)
        self.assertIn("More feedback", self.app)
        for dimension in (
            "overall",
            "correctness",
            "completeness",
            "runnability",
            "clarity",
        ):
            self.assertIn(f'"{dimension}"', self.app)
        self.assertIn(".resource-action-rating-form", self.styles)

    def test_read_only_conversation_does_not_offer_mutation(self) -> None:
        renderer_start = self.app.index("function assistantResourceActionResultsHtml(")
        renderer = self.app[renderer_start : renderer_start + 2600]
        self.assertIn("session.read_only", renderer)


if __name__ == "__main__":
    unittest.main()
