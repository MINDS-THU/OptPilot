"""Searching by what you want to do finds work that does it.

Someone new searches "optimize", "solver", "layout" -- none of which appeared
in any shipped component, so the honest answer to most first searches was
nothing at all.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from optpilot.task_vocabulary import (
    KNOWN_TASK_SLUGS,
    expand_search_terms,
    is_task_slug,
    task_search_words,
)

_CATALOG = Path(__file__).resolve().parents[2] / "catalog"


class TaskVocabularyTest(unittest.TestCase):
    def test_an_ordinary_word_reaches_the_work_it_names(self) -> None:
        self.assertIn("optimize-policy", expand_search_terms("optimize"))
        self.assertIn("optimize-policy", expand_search_terms("staffing"))
        self.assertIn("solve-or-problem", expand_search_terms("solver"))
        self.assertIn("evaluate-design", expand_search_terms("layout"))
        self.assertIn("generate-simulator", expand_search_terms("simulator"))

    def test_a_word_that_means_nothing_expands_to_itself(self) -> None:
        self.assertEqual(expand_search_terms("wombat"), {"wombat"})
        self.assertEqual(expand_search_terms("  "), set())

    def test_a_slug_is_findable_by_its_own_words(self) -> None:
        words = task_search_words(["solve-or-problem"])
        self.assertIn("solve", words)
        self.assertIn("problem", words)
        self.assertIn("solve-or-problem", words)

    def test_shape_is_checked_but_unknown_work_is_allowed(self) -> None:
        # A package may describe work this release never heard of; refusing it
        # would make the field useless outside this repository.
        self.assertTrue(is_task_slug("optimize-policy"))
        self.assertTrue(is_task_slug("fold-proteins"))
        for bad in ("Optimize-Policy", "optimize_policy", "-leading", "", 7, None):
            with self.subTest(bad=bad):
                self.assertFalse(is_task_slug(bad))


class ShippedComponentTasksTest(unittest.TestCase):
    def _components(self) -> list[Path]:
        return sorted(
            path
            for path in _CATALOG.rglob("*.yaml")
            if re.match(r"(environment|method|optpilot\.resource)", path.name)
        )

    def test_every_shipped_component_says_what_it_is_for(self) -> None:
        missing = []
        for path in self._components():
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not document.get("tasks"):
                missing.append(str(path.relative_to(_CATALOG)))
        self.assertEqual(missing, [], "shipped components that declare no tasks")

    def test_shipped_components_use_vocabulary_the_product_understands(self) -> None:
        # Third parties may invent slugs; what OptPilot ships should not, or
        # the synonym table silently stops covering its own packages.
        unknown = {}
        for path in self._components():
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for slug in document.get("tasks") or []:
                self.assertTrue(is_task_slug(slug), f"malformed slug {slug!r}")
                if slug not in KNOWN_TASK_SLUGS:
                    unknown.setdefault(slug, []).append(path.name)
        self.assertEqual(unknown, {}, "shipped slugs missing from the vocabulary")


if __name__ == "__main__":
    unittest.main()
