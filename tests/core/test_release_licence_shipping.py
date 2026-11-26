"""Checks the core packaging licence declarations."""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11")
class LicenceShippingTest(unittest.TestCase):
    def test_core_declares_its_licence(self) -> None:
        project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license"], "Apache-2.0")

    def test_core_ships_the_licence_text(self) -> None:
        project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
        self.assertIn("LICENSE", project["license-files"])
        for entry in project["license-files"]:
            self.assertTrue((_ROOT / entry).is_file(), entry)
