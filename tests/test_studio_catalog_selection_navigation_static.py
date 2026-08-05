"""Focused contracts for exact Catalog-item navigation through active filters."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"


def _function_source(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioCatalogSelectionNavigationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_exact_target_clears_only_filters_that_hide_it(self) -> None:
        reveal = _function_source(self.source, "revealCatalogComponent")

        self.assertIn(
            'state.componentFilter !== "all" && state.componentFilter !== component.kind',
            reveal,
        )
        self.assertIn('state.componentFilter = "all";', reveal)
        self.assertIn(
            'state.componentPackageFilter !== componentPackageId(component)', reveal
        )
        self.assertIn('state.componentPackageFilter = "all";', reveal)
        self.assertIn("!catalogSearchText(component).includes(query)", reveal)
        self.assertIn('state.componentSearch = "";', reveal)

    def test_registration_and_assistant_reveal_before_selecting_target(self) -> None:
        registration = _function_source(
            self.source, "openRegisteredCatalogResult"
        )
        assistant = _function_source(self.source, "executeAssistantUiCardAction")

        for function in (registration, assistant):
            self.assertLess(
                function.index("revealCatalogComponent(component);"),
                function.index("state.selectedComponentKey = component.key;"),
            )

    def test_catalog_deep_link_reveals_the_exact_target(self) -> None:
        route = _function_source(self.source, "applyStudioRoute")
        catalog_branch = route[
            route.index('route.view === "catalog"') : route.index(
                'route.view === "experiments"'
            )
        ]

        self.assertIn(
            "allComponents().find((item) => item.key === route.componentKey)",
            catalog_branch,
        )
        self.assertLess(
            catalog_branch.index("revealCatalogComponent(component);"),
            catalog_branch.index("state.selectedComponentKey = component.key;"),
        )


if __name__ == "__main__":
    unittest.main()
