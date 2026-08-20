"""The Assistant's first look at the Catalog must not cost the conversation.

Listing the Catalog returned every field of every entry -- including each
settings file's complete text and its parsed contents -- which for the five
shipped packages is roughly 348,000 characters, or the better part of a
hundred thousand tokens, spent before the Assistant has done anything at all.
On a smaller model it simply does not fit.

A listing exists to *choose* an entry. Everything needed to use one is a
single detail call away, and these tests hold that split: the listing stays
small, keeps what choosing needs, and keeps the token that detail is looked
up by.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    ASSISTANT_CATALOG_LIST_FIELDS,
    UiState,
    _assistant_catalog_entry,
    _create_agent_session,
    _execute_agent_tool,
)

_ROOT = Path(__file__).resolve().parents[2]

#: Comfortably above what the shipped packages produce (measured ~60,000) and
#: far below what they produced before (~348,000). A failure here means an
#: entry started carrying something heavy again.
_LISTING_CHARACTER_BUDGET = 120_000


class SlimEntryTest(unittest.TestCase):
    def test_the_heavy_fields_are_dropped(self) -> None:
        entry = {
            "id": "demo",
            "qualified_id": "demo_pkg/method/demo",
            "uid": "cref_x",
            "label": "Demo",
            "description": "d",
            "yaml": "x" * 5000,
            "raw_config": {"big": ["y"] * 500},
            "ref": {"expanded": True},
            "summary": {"derived": True},
            "actions": {"create_editable_workspace": {}},
        }
        slim = _assistant_catalog_entry(entry)
        for dropped in ("yaml", "raw_config", "ref", "summary", "actions"):
            self.assertNotIn(dropped, slim)
        self.assertEqual(slim["id"], "demo")
        self.assertEqual(slim["qualified_id"], "demo_pkg/method/demo")
        # The ~490-character ref token goes too: offering it invites a model to
        # copy it back wrongly, which is exactly how lookups were failing.
        self.assertNotIn("uid", slim)

    def test_choosing_an_entry_still_has_what_it_needs(self) -> None:
        for needed in ("id", "label", "description", "tags", "tasks", "package"):
            self.assertIn(needed, ASSISTANT_CATALOG_LIST_FIELDS)

    def test_the_listing_carries_what_lookups_are_made_with(self) -> None:
        # Whatever names an entry to the other tools has to be in the listing,
        # or acting on a result needs a second search. That used to be the ref
        # token; it is now the short qualified_id, which a model can actually
        # reproduce.
        self.assertIn("qualified_id", ASSISTANT_CATALOG_LIST_FIELDS)
        self.assertNotIn("uid", ASSISTANT_CATALOG_LIST_FIELDS)

    def test_a_malformed_entry_does_not_raise(self) -> None:
        self.assertEqual(_assistant_catalog_entry("not an entry"), {})


@unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs the shipped packages")
class ShippedCatalogListingTest(unittest.TestCase):
    def test_listing_every_shipped_package_stays_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state = UiState(
                cwd=_ROOT, catalog_roots=[_ROOT / "catalog"], run_roots=[]
            )
            for name in (
                "sessions_dir",
                "agent_sessions_dir",
                "jobs_dir",
                "workspaces_dir",
                "runtime_dir",
            ):
                setattr(state, name, tmp / name)
                getattr(state, name).mkdir(parents=True, exist_ok=True)
            state.settings_path = tmp / "settings.json"
            session = _create_agent_session(state, {"title": "listing"})
            result = _execute_agent_tool(
                state, session["id"], "optpilot_catalog_list", {}
            )

        size = len(json.dumps(result))
        self.assertLess(size, _LISTING_CHARACTER_BUDGET, f"listing is {size:,} chars")
        entries = result["data"]["environments"]
        self.assertTrue(entries)
        for entry in entries:
            self.assertLessEqual(set(entry), set(ASSISTANT_CATALOG_LIST_FIELDS))


if __name__ == "__main__":
    unittest.main()
