"""What a newcomer meets first is work, not test material.

Nine shipped entries exist to check that the machinery runs, and they sat in
the Catalog beside the flagship packages as equals -- indistinguishable except
by a name ending in "-smoke". They stay listed and findable; they simply stop
competing for a newcomer's attention.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_CATALOG = _ROOT / "catalog"

#: Everything shipped whose purpose is checking rather than doing.
_TEST_MATERIAL = {
    "factory-design-smoke",
    "clinic-baseline-smoke",
    "dispatch-baseline-smoke",
    "queue-demo-baseline-smoke",
    "production-agv-scheduling-parallel-smoke",
    "production-agv-scheduling-smoke",
    "direct-designer-seed",
    "ga-weighted-rule-search-smoke",
    "default-rule-policy-smoke",
}


class CatalogEmphasisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _APP.read_text(encoding="utf-8")

    def test_every_piece_of_test_material_declares_itself(self) -> None:
        # The browser sorts and labels on this marker, so an unmarked fixture
        # silently returns to competing with the real packages.
        unmarked = []
        for path in sorted(_CATALOG.rglob("*.yaml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(document, dict):
                continue
            identity = str(document.get("id") or document.get("name") or "")
            if identity not in _TEST_MATERIAL:
                continue
            tags = [str(tag).lower() for tag in document.get("tags") or []]
            if "smoke" not in tags:
                unmarked.append(identity)
        self.assertEqual(unmarked, [], "test material not marked as such")

    def test_the_catalog_sinks_test_material(self) -> None:
        self.assertIn("isTestMaterial", self.app)
        self.assertIn(
            "components.sort((left, right) => isTestMaterial(left) - isTestMaterial(right));",
            self.app,
        )

    def test_test_material_is_labelled_not_merely_muted(self) -> None:
        self.assertIn("catalog-test-chip", self.app)

    def test_the_catalog_search_reads_the_description_on_the_card(self) -> None:
        # It read summary.description, which entries do not populate, so the
        # text a person can see was never searched.
        start = self.app.index("function catalogSearchText(")
        body = self.app[start : self.app.index("\n}", start)]
        self.assertIn("component.entry && component.entry.description", body)
        self.assertIn("component.entry.tasks", body)


if __name__ == "__main__":
    unittest.main()
