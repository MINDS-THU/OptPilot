"""Every shipped Run setup is presented by name, not by its identifier.

A study's `name` field IS its identifier, so unlike environments and methods
it had nowhere to carry a human name -- and "Run setups" is a primary page.
A new install listed eighteen entries reading `production-agv-pso-weighted-rules`
and `abp-tune-timeout`, which say almost nothing about what they do.

Studies therefore take an optional `title`, and Studio prefers it. This pins
that every shipped Run setup uses one, since the field being optional is
exactly what lets it be forgotten.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = _ROOT / "catalog"
#: Fixtures used by tests, not things a person browses.
_TEST_PACKAGES = {"example_package", "local_package", "test_catalog"}


def _shipped_studies() -> list[tuple[Path, dict]]:
    found = []
    for path in sorted(_CATALOG.rglob("*.yaml")):
        if any(part in _TEST_PACKAGES for part in path.parts):
            continue
        package_root = _CATALOG / path.relative_to(_CATALOG).parts[0]
        settings_path = package_root / "optpilot.package.yaml"
        if not settings_path.is_file():
            continue
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if settings.get("category") not in {"research", "tutorial"}:
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(raw, dict) and raw.get("config") == "study":
            found.append((path, raw))
    return found


class RunSetupNamesTest(unittest.TestCase):
    def test_the_schema_allows_a_display_title(self) -> None:
        schema = json.loads(
            (_ROOT / "src/optpilot/schemas/study.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("title", schema["properties"])
        # Optional on purpose: a study written before this existed stays valid.
        self.assertNotIn("title", schema.get("required", []))

    def test_there_are_shipped_run_setups_to_check(self) -> None:
        self.assertGreaterEqual(len(_shipped_studies()), 10)

    def test_every_shipped_run_setup_has_one(self) -> None:
        missing = [
            str(path.relative_to(_ROOT))
            for path, raw in _shipped_studies()
            if not str(raw.get("title") or "").strip()
        ]
        self.assertEqual(missing, [], f"Run setups without a readable name: {missing}")

    def test_the_name_is_not_just_the_identifier_again(self) -> None:
        lazy = [
            str(path.relative_to(_ROOT))
            for path, raw in _shipped_studies()
            if str(raw.get("title") or "").strip() == str(raw.get("name") or "").strip()
        ]
        self.assertEqual(lazy, [], f"title merely repeats the identifier: {lazy}")

    def test_studio_prefers_the_title(self) -> None:
        server = (
            _ROOT / "studio/src/optpilot_studio/ui/server.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'label = raw.get("title") or raw.get("name")',
            server,
            "the displayed label must prefer the readable title",
        )


if __name__ == "__main__":
    unittest.main()
