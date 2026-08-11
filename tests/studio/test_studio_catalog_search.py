"""Assistant catalog search (U4): free-text query + tag filtering."""

from __future__ import annotations

import unittest
from unittest import mock

from optpilot_studio.ui.server import (
    _catalog_search_tags,
    _execute_agent_tool,
    _search_catalog_entries,
)


def _entry(**overrides) -> dict:
    base = {
        "uid": "cref_exact",
        "config": "environment",
        "id": "job-shop-dispatch-rule",
        "label": "Job shop dispatching",
        "description": "Evaluate generated Python dispatching rules.",
        "package_id": "example_package",
        "tags": ["job-shop", "scheduling"],
        "summary": {"candidate_format": "files"},
    }
    base.update(overrides)
    return base


class CatalogSearchFilterTest(unittest.TestCase):
    def test_every_query_term_must_match_some_identity_field(self) -> None:
        entries = [
            _entry(),
            _entry(
                id="seird-epidemic",
                label="SEIRD epidemic",
                description="DEVS-Gen generated epidemic simulator.",
                package_id="devs_gallery",
                tags=["devs", "simulation"],
            ),
        ]
        self.assertEqual(
            [item["id"] for item in _search_catalog_entries(entries, query="epidemic")],
            ["seird-epidemic"],
        )
        # Terms AND together and may match different fields.
        self.assertEqual(
            [
                item["id"]
                for item in _search_catalog_entries(
                    entries, query="devs_gallery simulator"
                )
            ],
            ["seird-epidemic"],
        )
        self.assertEqual(
            _search_catalog_entries(entries, query="epidemic job-shop"), []
        )
        # Case-insensitive, and tags are part of the haystack.
        self.assertEqual(
            [
                item["id"]
                for item in _search_catalog_entries(entries, query="SCHEDULING")
            ],
            ["job-shop-dispatch-rule"],
        )

    def test_purpose_is_searchable_from_entry_and_summary(self) -> None:
        entries = [
            _entry(purpose="generator"),
            _entry(id="viewer", summary={"purpose": "trace viewer"}),
        ]
        self.assertEqual(
            [item["id"] for item in _search_catalog_entries(entries, query="generator")],
            ["job-shop-dispatch-rule"],
        )
        self.assertEqual(
            [item["id"] for item in _search_catalog_entries(entries, query="viewer")],
            ["viewer"],
        )

    def test_all_requested_tags_must_be_declared_exactly(self) -> None:
        entries = [
            _entry(),
            _entry(id="untagged", tags=[]),
        ]
        self.assertEqual(
            [
                item["id"]
                for item in _search_catalog_entries(
                    entries, tags=("scheduling", "job-shop")
                )
            ],
            ["job-shop-dispatch-rule"],
        )
        # A tag filter is exact membership, not substring.
        self.assertEqual(_search_catalog_entries(entries, tags=("job",)), [])

    def test_query_and_tags_compose(self) -> None:
        entries = [
            _entry(),
            _entry(
                id="other-scheduler",
                label="Other scheduler",
                description="Another scheduler.",
            ),
        ]
        self.assertEqual(
            [
                item["id"]
                for item in _search_catalog_entries(
                    entries, query="dispatching", tags=("scheduling",)
                )
            ],
            ["job-shop-dispatch-rule"],
        )

    def test_tag_argument_normalization_fails_closed(self) -> None:
        self.assertEqual(_catalog_search_tags(None), ())
        self.assertEqual(_catalog_search_tags("DEVS"), ("devs",))
        self.assertEqual(
            _catalog_search_tags([" Devs ", "simulation", "devs"]),
            ("devs", "simulation"),
        )
        with self.assertRaises(ValueError):
            _catalog_search_tags([1, "devs"])
        with self.assertRaises(ValueError):
            _catalog_search_tags({"tag": "devs"})


class CatalogListToolSearchTest(unittest.TestCase):
    _CATALOG = {
        "environments": [
            _entry(),
            _entry(
                id="seird-epidemic",
                label="SEIRD epidemic",
                description="DEVS-Gen generated epidemic simulator.",
                package_id="devs_gallery",
                tags=["devs", "simulation"],
            ),
        ],
        "methods": [
            _entry(
                config="method",
                id="coopa-solver",
                label="COOPA solver",
                description="Natural-language OR solving through COOPA.",
                package_id="or_solving",
                tags=["or", "llm"],
            )
        ],
        "studies": [],
        "resources": [],
        "packages": [{"id": "devs_gallery"}],
    }

    def _run(self, arguments: dict) -> dict:
        with mock.patch(
            "optpilot_studio.ui.server._catalog_payload",
            return_value={
                key: (list(value) if isinstance(value, list) else value)
                for key, value in self._CATALOG.items()
            },
        ):
            return _execute_agent_tool(
                object(), "session-1", "optpilot_catalog_list", arguments
            )

    def test_query_filters_across_all_kinds_and_reports_counts(self) -> None:
        result = self._run({"query": "epidemic"})
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["id"] for item in result["data"]["environments"]],
            ["seird-epidemic"],
        )
        self.assertEqual(result["data"]["methods"], [])
        self.assertIn("matched 1 of 3", result["summary"])
        # Non-entry catalog keys survive for unfiltered context.
        self.assertEqual(result["data"]["packages"], [{"id": "devs_gallery"}])

    def test_kind_and_query_compose(self) -> None:
        result = self._run({"config_kind": "method", "query": "or solving"})
        self.assertEqual(set(result["data"]), {"methods"})
        self.assertEqual(
            [item["id"] for item in result["data"]["methods"]], ["coopa-solver"]
        )
        self.assertIn("matched 1 of 1", result["summary"])

    def test_unfiltered_listing_keeps_original_summary(self) -> None:
        result = self._run({})
        self.assertEqual(
            result["summary"], "Catalog entries and saved study plans listed."
        )
        self.assertEqual(len(result["data"]["environments"]), 2)

    def test_tags_filter_and_invalid_tags_error(self) -> None:
        result = self._run({"tags": ["DEVS"]})
        self.assertEqual(
            [item["id"] for item in result["data"]["environments"]],
            ["seird-epidemic"],
        )
        with self.assertRaises(ValueError):
            self._run({"tags": [3]})


if __name__ == "__main__":
    unittest.main()
