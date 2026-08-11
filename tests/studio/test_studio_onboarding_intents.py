"""Dynamic onboarding intents derived from the catalog registry (U6)."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


class OnboardingIntentSourceTest(unittest.TestCase):
    """The welcome intents are registry-driven, not a static string list."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = _APP_JS.read_text(encoding="utf-8")

    def test_renderer_derives_intents_from_the_catalog_groups(self) -> None:
        self.assertIn("const intents = onboardingIntents(groups);", self.app_js)
        self.assertIn("function onboardingIntents(groups)", self.app_js)
        # Intent changes must invalidate the render signature.
        signature_start = self.app_js.index("const signature = stableJsonStringify({")
        signature_block = self.app_js[signature_start : signature_start + 400]
        self.assertIn("intents,", signature_block)

    def test_unbacked_intents_are_suppressed_once_catalog_loads(self) -> None:
        self.assertIn(
            '["Compare methods", "I want to compare how several methods perform '
            'on an environment and dataset.", (counts.method || 0) >= 2]',
            self.app_js,
        )
        self.assertIn(
            "(counts.environment || 0) > 0",
            self.app_js,
        )
        # Loading or errored catalogs keep every static intent visible.
        self.assertIn(
            "const catalogReady = !state.catalogLoading && !state.catalogError;",
            self.app_js,
        )
        self.assertIn("!catalogReady || backed", self.app_js)
        # Build/publish never depends on the registry.
        self.assertIn(
            '["Build or publish", "Help me build or publish an Environment, '
            'Method, or Resource for this problem.", true]',
            self.app_js,
        )

    def test_flagship_capabilities_surface_by_name_from_metadata(self) -> None:
        # Generator resources and one-time-solve methods become named chips.
        self.assertIn('purpose !== "generator"', self.app_js)
        self.assertIn('tags.includes("one-time-solve")', self.app_js)
        self.assertIn("`Generate with ${title}`", self.app_js)
        self.assertIn("`Solve with ${title}`", self.app_js)
        self.assertIn(
            "capabilityIntents.slice(0, ONBOARDING_CAPABILITY_INTENT_LIMIT)",
            self.app_js,
        )


if __name__ == "__main__":
    unittest.main()
