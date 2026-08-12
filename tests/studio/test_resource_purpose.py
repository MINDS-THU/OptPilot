from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import validate_authoring_config
from optpilot_studio.ui.server import _resource_catalog_entry


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ResourcePurposeTest(unittest.TestCase):
    def _write_manifest(self, root: Path, **updates: object) -> Path:
        resource = root / "resources" / "example"
        resource.mkdir(parents=True)
        manifest = {
            "apiVersion": "optpilot.io/v1",
            "config": "resource",
            "id": "example",
            "name": "Example Resource",
            **updates,
        }
        path = resource / "optpilot.resource.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        return path

    def test_schema_accepts_only_the_four_declared_resource_purposes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for purpose in ("generator", "viewer", "template", "reference"):
                with self.subTest(purpose=purpose):
                    path = self._write_manifest(root / purpose, purpose=purpose)
                    self.assertTrue(validate_authoring_config(path)["valid"])

            invalid = self._write_manifest(root / "invalid", purpose="dashboard")
            result = validate_authoring_config(invalid)
            self.assertFalse(result["valid"])
            self.assertIn("purpose", " ".join(result["errors"]))

    def test_catalog_projects_only_declared_valid_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            declared = self._write_manifest(root / "declared", purpose="viewer").parent
            declared_entry = _resource_catalog_entry(declared)
            self.assertEqual(declared_entry["purpose"], "viewer")
            self.assertEqual(declared_entry["summary"]["purpose"], "viewer")

            fallback = self._write_manifest(
                root / "fallback",
                tags=["generator"],
                interface={
                    "command": ["python", "-m", "generator"],
                    "presentation": {"kind": "web", "port": 8000},
                },
            ).parent
            fallback_entry = _resource_catalog_entry(fallback)
            self.assertIsNone(fallback_entry["purpose"])
            self.assertIsNone(fallback_entry["summary"]["purpose"])

    def test_devs_resource_declares_generator_purpose(self) -> None:
        manifest_path = (
            _REPOSITORY_ROOT
            / "catalog"
            / "devs_gallery"
            / "resources"
            / "devs-gen-interface"
            / "optpilot.resource.yaml"
        )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["purpose"], "generator")
        self.assertTrue(validate_authoring_config(manifest_path)["valid"])

    def test_catalog_ui_uses_declared_label_with_resource_fallback(self) -> None:
        app = (
            _REPOSITORY_ROOT
            / "studio"
            / "src"
            / "optpilot_studio"
            / "ui"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('if (kind === "resource") return resourcePurposeLabel(item);', app)
        self.assertIn('generator: "Generator"', app)
        self.assertIn('viewer: "Viewer"', app)
        self.assertIn('template: "Template"', app)
        self.assertIn('reference: "Reference"', app)
        self.assertIn('return labels[declared] || "Resource";', app)


if __name__ == "__main__":
    unittest.main()
