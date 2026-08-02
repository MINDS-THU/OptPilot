"""Configured-source discovery, validation, and removed-adapter contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import yaml

from optpilot.realm.errors import RealmNotFound
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _configured_catalog_source_id,
    _configured_package_static_validator,
    _handler_factory,
    _reauthorized_configured_catalog_source,
)


class StudioConfiguredSourceValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "catalog" / "mutable-package"
        resource = self.package / "resources" / "viewer"
        resource.mkdir(parents=True)
        (resource / "optpilot.resource.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "resource",
                    "id": "viewer",
                    "name": "Mutable viewer",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[self.package],
            run_roots=[],
        )
        self.addCleanup(self.state.close_catalog_projections)

    @staticmethod
    def _handler(state: UiState):
        handler = object.__new__(_handler_factory(state))
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        )
        return handler, responses

    def test_catalog_exposes_a_separate_path_free_configured_source(self) -> None:
        payload = _catalog_payload(self.state)

        self.assertEqual(len(payload["sources"]), 1)
        source = payload["sources"][0]
        self.assertEqual(
            source["source_id"],
            _configured_catalog_source_id(self.state.catalog_roots[0]),
        )
        self.assertEqual(source["package_id"], "mutable-package")
        self.assertEqual(source["publication_scope"], "whole-package")
        self.assertTrue(source["mutable"])
        capability = source["actions"]["open_workspace"]
        self.assertTrue(capability["eligible"])
        self.assertEqual(capability["code"], "ready")
        self.assertFalse(capability["copies_source"])
        self.assertFalse(capability["executes_authored_code"])
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('"path":', json.dumps(source, sort_keys=True))

    def test_opaque_source_reauthorization_does_not_probe_mutable_tree(self) -> None:
        source_id = _configured_catalog_source_id(self.state.catalog_roots[0])

        with (
            mock.patch.object(Path, "resolve", side_effect=AssertionError("resolve")),
            mock.patch.object(Path, "is_dir", side_effect=AssertionError("is_dir")),
            mock.patch(
                "optpilot_studio.ui.server._looks_like_catalog_package",
                side_effect=AssertionError("probe"),
            ),
        ):
            root, package_id = _reauthorized_configured_catalog_source(
                self.state, source_id
            )

        self.assertEqual(root, self.state.catalog_roots[0])
        self.assertEqual(package_id, "mutable-package")

    def test_unknown_source_is_path_free_not_found(self) -> None:
        with self.assertRaises(RealmNotFound) as raised:
            _reauthorized_configured_catalog_source(
                self.state, "import_" + "0" * 40
            )

        self.assertEqual(
            str(raised.exception), "Configured catalog source is not available."
        )
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_obsolete_direct_publish_action_is_not_exposed(self) -> None:
        handler, responses = self._handler(self.state)
        source_id = _configured_catalog_source_id(self.state.catalog_roots[0])
        read_body = mock.Mock(
            side_effect=AssertionError("request body was read")
        )
        handler._read_json_body = read_body  # type: ignore[method-assign]

        handler._handle_configured_catalog_source_post(
            f"/api/catalog/sources/{source_id}/publish"
        )

        read_body.assert_not_called()
        payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(
            payload["error"], "Unknown configured catalog source action"
        )

    def test_static_validator_rejects_zero_recognized_entries(self) -> None:
        projected = self.root / "projected-empty-package"
        (projected / "environments").mkdir(parents=True)
        (projected / "environments" / "README.txt").write_text(
            "not a config\n", encoding="utf-8"
        )

        result = _configured_package_static_validator(projected)

        self.assertFalse(result.accepted)
        facts = [fact.to_dict() for fact in result.facts]
        self.assertIn(
            {"code": "no_recognized_entries", "count": 1, "severity": "error"},
            facts,
        )
        self.assertEqual(
            facts,
            sorted(
                facts,
                key=lambda item: (
                    item["code"],
                    item["severity"],
                    item["count"],
                ),
            ),
        )

    def test_invalid_and_ignored_yaml_return_canonical_rejection(self) -> None:
        projected = self.root / "projected-invalid-package"
        (projected / "environments").mkdir(parents=True)
        (projected / "environments" / "invalid.yaml").write_text(
            "apiVersion: optpilot.io/v1\nconfig: environment\nid: invalid\n",
            encoding="utf-8",
        )
        (projected / "notes.yaml").write_text(
            "notes: ignored\n", encoding="utf-8"
        )

        result = _configured_package_static_validator(projected)

        self.assertFalse(result.accepted)
        codes = {fact.code for fact in result.facts}
        self.assertIn("invalid_environment", codes)
        self.assertIn("ignored_yaml", codes)
        self.assertEqual(
            result.facts,
            tuple(
                sorted(
                    result.facts,
                    key=lambda fact: (
                        fact.code,
                        fact.severity,
                        fact.count,
                    ),
                )
            ),
        )

    def test_static_validator_rejects_yaml_alias_graphs_before_loading(self) -> None:
        projected = self.root / "projected-aliased-package"
        projected.mkdir()
        (projected / "notes.yaml").write_text(
            "anchor: &shared\n  value: 1\ncopy: *shared\n",
            encoding="utf-8",
        )

        with mock.patch(
            "optpilot_studio.ui.server.validate_package",
            side_effect=AssertionError("unsafe YAML reached package loading"),
        ):
            result = _configured_package_static_validator(projected)

        self.assertFalse(result.accepted)
        self.assertEqual(result.facts[0].code, "yaml_alias_unsupported")

    def test_static_validator_rejects_deep_yaml_before_loading(self) -> None:
        projected = self.root / "projected-deep-package"
        projected.mkdir()
        depth = 130
        (projected / "notes.yaml").write_text(
            "[" * depth + "value" + "]" * depth + "\n",
            encoding="utf-8",
        )

        with mock.patch(
            "optpilot_studio.ui.server.validate_package",
            side_effect=AssertionError("deep YAML reached package loading"),
        ):
            result = _configured_package_static_validator(projected)

        self.assertFalse(result.accepted)
        self.assertEqual(result.facts[0].code, "yaml_nesting_exceeded")

    def test_validator_exception_never_exposes_provider_path(self) -> None:
        with mock.patch(
            "optpilot_studio.ui.server.validate_package",
            side_effect=RuntimeError("parser failed at /secret/path/config.yaml"),
        ):
            result = _configured_package_static_validator(self.package)

        self.assertFalse(result.accepted)
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertEqual(result.facts[0].code, "static_validation_failed")
        self.assertNotIn("/secret/path", serialized)


if __name__ == "__main__":
    unittest.main()
