"""Both ways to improve a simulated system know the other exists.

OptPilot ships two: search for a good operating rule by trial and error, or
state the problem mathematically and solve it exactly. Nothing connected them
-- the page about optimising a generated simulator never mentioned the solver
route once -- so half the product's headline story was unreachable by anyone
following the documentation.

Nothing derives one from the other automatically; that is a modelling decision
a person makes. What must not break is the written path between them.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _ROOT / "studio" / "src" / "optpilot_studio" / "docs_assets"
_SEARCH_PAGE = _DOCS / "generate-and-optimize.md"
_SOLVER_PAGE = _DOCS / "or-solving.md"


class SolverBridgeTest(unittest.TestCase):
    def test_each_route_points_at_the_other(self) -> None:
        self.assertIn("or-solving.md", _SEARCH_PAGE.read_text(encoding="utf-8"))
        self.assertIn(
            "generate-and-optimize.md", _SOLVER_PAGE.read_text(encoding="utf-8")
        )

    def test_the_walkthrough_names_the_input_a_person_must_fill(self) -> None:
        study = yaml.safe_load(
            (_ROOT / "catalog/or_solving/studies/solve_or_problem.yaml").read_text(
                encoding="utf-8"
            )
        )
        inputs = study.get("inputs") or {}
        required = [
            name for name, spec in inputs.items() if "default" not in (spec or {})
        ]
        self.assertEqual(
            required,
            ["problem"],
            "the walkthrough tells people to fill exactly this input",
        )
        page = _SEARCH_PAGE.read_text(encoding="utf-8")
        self.assertIn("`problem`", page)
        self.assertIn("solve-or-problem", page)

    def test_the_walkthrough_gives_a_usable_example(self) -> None:
        page = _SEARCH_PAGE.read_text(encoding="utf-8")
        example = re.search(r"```text\n(Minimise.*?)```", page, re.S)
        self.assertIsNotNone(example, "the worked example is missing")
        body = example.group(1).lower()
        # The three things the solver needs stated, which is the whole point
        # of the section.
        self.assertIn("minimise", body)
        self.assertIn("decide", body)
        self.assertIn("constraint", body)

    def test_it_says_which_route_suits_which_question(self) -> None:
        page = _SEARCH_PAGE.read_text(encoding="utf-8")
        self.assertIn("Which route to use", page)


if __name__ == "__main__":
    unittest.main()
