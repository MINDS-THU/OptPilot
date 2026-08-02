"""Focused static contracts for the compact Catalog filter toolbar."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_INDEX = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "index.html"
)
_STYLES = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "styles.css"
)


class StudioCatalogFilterLayoutStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = _INDEX.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_item_types_and_package_share_one_catalog_toolbar(self) -> None:
        start = self.index.index('<div class="catalog-filter-toolbar">')
        end = self.index.index('<div id="catalogSources"', start)
        toolbar = self.index[start:end]

        self.assertIn('<div class="catalog-kind-filters"', toolbar)
        for item_type in ("all", "environment", "method", "resource"):
            self.assertIn(f'data-component-filter="{item_type}"', toolbar)
        self.assertIn('id="componentPackageFilter"', toolbar)
        self.assertLess(
            toolbar.index('data-component-filter="resource"'),
            toolbar.index('id="componentPackageFilter"'),
        )

    def test_catalog_toolbar_is_compact_without_changing_global_tabs(self) -> None:
        self.assertIn('class="entity-layout catalog-entity-layout"', self.index)
        self.assertIn(".catalog-entity-layout {", self.styles)
        self.assertIn(
            "grid-template-columns: minmax(450px, 0.7fr) minmax(360px, 1fr);",
            self.styles,
        )
        self.assertIn(".catalog-filter-toolbar {", self.styles)
        self.assertIn(".catalog-kind-filters {", self.styles)
        self.assertIn("flex-wrap: nowrap;", self.styles)
        self.assertIn("flex: 0 0 auto;", self.styles)
        self.assertIn("white-space: nowrap;", self.styles)
        self.assertIn(".catalog-kind-filters .tab {", self.styles)
        self.assertIn("flex: 1 1 140px;", self.styles)
        self.assertIn("min-width: 128px;", self.styles)
        self.assertIn("max-width: 150px;", self.styles)
        self.assertIn("@media (max-width: 460px)", self.styles)
        self.assertIn(".tabs {", self.styles)
        self.assertIn("flex-wrap: wrap;", self.styles)


if __name__ == "__main__":
    unittest.main()
