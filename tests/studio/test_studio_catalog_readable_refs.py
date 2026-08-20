"""A catalog entry can be named the way a person would name it.

Every tool that touches a catalog entry used to demand the entry's reference
token echoed back exactly -- around 490 characters of base64 -- or, worse, a
structured object carrying a digest. A language model will not reliably
reproduce either. One decoded a token, re-encoded it, dropped a field, and sent
485 characters that were not even a prefix of the original; the tool answered
"Catalog entry ref is invalid" and the conversation stopped with nothing on
screen to explain it.

The readable identifiers already in every listing are now accepted:
`or_solving/method/coopa-solver`, or `coopa-solver` when only one entry of that
kind has that name. They name the entry as it stands now, which is what
clicking it in the Catalog does too.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from optpilot_studio.agent import (
    CATALOG_ENTRY_REF_SCHEMA,
    OPTPILOT_AGENT_TOOL_SPECS,
)
from optpilot_studio.ui.server import (
    ASSISTANT_CATALOG_LIST_FIELDS,
    UiState,
    _catalog_detail,
    _catalog_entry_ref_from_value,
)

_ROOT = Path(__file__).resolve().parents[2]


class ReferenceSchemaTest(unittest.TestCase):
    def test_a_structured_reference_may_be_a_plain_name(self) -> None:
        kinds = [option.get("type") for option in CATALOG_ENTRY_REF_SCHEMA["anyOf"]]
        self.assertIn("string", kinds)
        self.assertIn("object", kinds)

    def test_the_listing_stops_handing_out_the_long_token(self) -> None:
        # Offering it invites the model to copy it, which is the failure.
        self.assertNotIn("uid", ASSISTANT_CATALOG_LIST_FIELDS)
        self.assertIn("qualified_id", ASSISTANT_CATALOG_LIST_FIELDS)

    def test_every_reference_parameter_explains_the_short_form(self) -> None:
        for spec in OPTPILOT_AGENT_TOOL_SPECS:
            props = (spec.get("parameters") or {}).get("properties") or {}
            for field in ("uid", "resource_uid"):
                if field not in props:
                    continue
                with self.subTest(tool=spec["name"], field=field):
                    self.assertIn(
                        "qualified_id",
                        str(props[field].get("description") or ""),
                    )


@unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs the shipped packages")
class ResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = UiState(
            cwd=_ROOT, catalog_roots=[_ROOT / "catalog"], run_roots=[]
        )

    def test_a_qualified_name_resolves(self) -> None:
        ref = _catalog_entry_ref_from_value(
            self.state,
            "method",
            "or_solving/method/coopa-solver",
            allow_readable_id=True,
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.entry_id, "coopa-solver")

    def test_a_plain_id_resolves_when_it_is_unambiguous(self) -> None:
        ref = _catalog_entry_ref_from_value(
            self.state, "method", "coopa-solver", allow_readable_id=True
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.entry_id, "coopa-solver")

    def test_naming_by_hand_stays_off_unless_asked_for(self) -> None:
        # Two callers -- opening an entry's source read-only, and launching its
        # interface -- treat "did this resolve?" as "was this an exact
        # reference?". Both pin one revision, so a name that resolves to
        # whatever is current must not satisfy them.
        self.assertIsNone(
            _catalog_entry_ref_from_value(self.state, "method", "coopa-solver")
        )

    def test_the_full_token_still_works(self) -> None:
        detail = _catalog_detail(self.state, "method", "coopa-solver")
        token = detail["entry"]["uid"]
        ref = _catalog_entry_ref_from_value(self.state, "method", token)
        self.assertEqual(ref.entry_id, "coopa-solver")

    def test_a_detail_lookup_by_name_returns_the_entry(self) -> None:
        detail = _catalog_detail(self.state, "method", "coopa-solver")
        self.assertEqual(detail["entry"]["id"], "coopa-solver")

    def test_an_unknown_name_is_not_silently_resolved(self) -> None:
        self.assertIsNone(
            _catalog_entry_ref_from_value(
                self.state, "method", "no-such-method", allow_readable_id=True
            )
        )

    def test_asking_for_the_wrong_kind_does_not_match(self) -> None:
        self.assertIsNone(
            _catalog_entry_ref_from_value(
                self.state, "environment", "coopa-solver", allow_readable_id=True
            )
        )


if __name__ == "__main__":
    unittest.main()
