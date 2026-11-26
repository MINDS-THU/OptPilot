"""Everything OptPilot ships is named for a person, not by its identifier.

Without a name the Catalog shows the identifier, so a first-time user browsed
"production-agv-scheduling-baselines" and the welcome page offered "Solve with
coopa-solver". The settings schemas forbid unknown fields and had no name
field at all, so this was not merely unset -- it could not be set.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

_CATALOG = Path(__file__).resolve().parents[2] / "catalog"


def _component_files() -> list[Path]:
    files = []
    for path in _CATALOG.rglob("*.yaml"):
        if not re.match(r"(environment|method)", path.name):
            continue
        package_root = _CATALOG / path.relative_to(_CATALOG).parts[0]
        settings_path = package_root / "optpilot.package.yaml"
        if not settings_path.is_file():
            continue
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if settings.get("category") in {"research", "tutorial"}:
            files.append(path)
    return sorted(files)


class ShippedComponentNamesTest(unittest.TestCase):
    def test_every_shipped_component_declares_a_name(self) -> None:
        missing = []
        for path in _component_files():
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not str(document.get("name") or "").strip():
                missing.append(str(path.relative_to(_CATALOG)))
        self.assertEqual(missing, [], "shipped components without a human name")

    def test_a_name_is_not_just_the_identifier_again(self) -> None:
        lazy = []
        for path in _component_files():
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            name = str(document.get("name") or "")
            identifier = str(document.get("id") or "")
            if name and identifier and name.strip().lower() == identifier.strip().lower():
                lazy.append(identifier)
        self.assertEqual(lazy, [], "these names only repeat the identifier")

    def test_the_schemas_allow_a_name(self) -> None:
        # The field had to be added: both schemas forbid unknown fields, so
        # without this a named component fails validation outright.
        import json

        schemas = Path(__file__).resolve().parents[2] / "src/optpilot/schemas"
        for kind in ("environment", "method", "study", "resource"):
            with self.subTest(kind=kind):
                document = json.loads((schemas / f"{kind}.schema.json").read_text())
                self.assertIn("name", document["properties"])


if __name__ == "__main__":
    unittest.main()
