"""Configured-source discovery, validation, and removed-adapter contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import yaml

from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _configured_catalog_source_id,
    _configured_package_static_validator,
    _handler_factory,
    _open_configured_catalog_source_workspace,
    _reauthorized_configured_catalog_source,
    _resolve_catalog_identifier,
)


class StudioConfiguredSourceValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "catalog" / "mutable-package"
        environment = self.package / "environments"
        environment.mkdir(parents=True)
        (environment / "sim.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "environment",
                    "id": "sim",
                    "name": "Mutable sim",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
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

    def test_a_package_name_is_refused_as_an_entry_with_its_entries_named(
        self,
    ) -> None:
        # A person says "republish mutable-package" and a model reaches for
        # catalog detail. The old error ("resource config not found:
        # mutable-package") named the failure and taught nothing, and a live
        # turn burned itself repeating the same call.
        with self.assertRaises(FileNotFoundError) as caught:
            _resolve_catalog_identifier(self.state, "resource", "mutable-package")
        message = str(caught.exception)
        self.assertIn("catalog package", message)
        self.assertIn("mutable-package", message)
        remedy = getattr(caught.exception, "optpilot_remedy", None) or getattr(
            caught.exception, "remedy", None
        )
        self.assertIsNotNone(remedy, "the error must carry a remedy")
        self.assertEqual(remedy.get("tool"), "optpilot_catalog_list")
        self.assertEqual(
            remedy.get("details", {}).get("reason"),
            "package_id_is_not_an_entry_uid",
        )

    def test_the_worked_example_is_valid_for_the_kind_that_was_asked(self) -> None:
        # The example has to fit the lookup that produced it. Offering an
        # environment uid to a resource lookup buys a second refusal -- and
        # that one carries no remedy to escape by.
        with self.assertRaises(FileNotFoundError) as caught:
            _resolve_catalog_identifier(self.state, "resource", "mutable-package")
        message = str(caught.exception)
        example = message.split("such as ")[1].strip(" '\".")
        self.assertIn("/resource/", example, message)
        # Following the message verbatim resolves, instead of dead-ending.
        self.assertTrue(
            _resolve_catalog_identifier(self.state, "resource", example).exists()
        )

    def test_a_kind_the_package_lacks_is_said_plainly(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            _resolve_catalog_identifier(self.state, "study", "mutable-package")
        message = str(caught.exception)
        self.assertIn("no study entry", message)
        self.assertNotIn("such as", message)

    def test_a_source_entry_reports_whether_its_workspace_is_open(self) -> None:
        sources = _catalog_payload(self.state)["sources"]
        entry = next(
            item for item in sources if item["package_id"] == "mutable-package"
        )
        self.assertEqual(entry["workspace_id"], "")
        opened = _open_configured_catalog_source_workspace(
            self.state, entry["source_id"]
        )
        refreshed = next(
            item
            for item in _catalog_payload(self.state)["sources"]
            if item["package_id"] == "mutable-package"
        )
        self.assertEqual(refreshed["workspace_id"], opened["id"])

    def test_an_unknown_name_says_so_without_claiming_a_package(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            _resolve_catalog_identifier(self.state, "resource", "no-such-thing")
        message = str(caught.exception)
        self.assertIn("no-such-thing", message)
        self.assertNotIn("catalog package", message)

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




class BundledPackageSourceTest(unittest.TestCase):
    """OptPilot's own packages are read-only registration targets.

    Registration refuses them outright ("ships with OptPilot"). Advertising
    the first step as ready sent a person -- and a model -- all the way to a
    refusal at the end, after opening an EDITABLE workspace over OptPilot's
    own tracked files for a job that could never finish.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "catalog" / "bundled-package"
        resource = self.package / "resources" / "viewer"
        resource.mkdir(parents=True)
        (resource / "optpilot.resource.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "resource",
                    "id": "viewer",
                    "name": "Bundled viewer",
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
        patcher = mock.patch(
            "optpilot_studio.ui.server._bundled_catalog_root",
            return_value=self.root / "catalog",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_source_card_says_it_cannot_be_registered_into(self) -> None:
        sources = _catalog_payload(self.state)["sources"]
        entry = next(
            item for item in sources if item["package_id"] == "bundled-package"
        )
        action = entry["actions"]["open_workspace"]
        self.assertFalse(action["eligible"])
        self.assertEqual(action["code"], "ships_with_optpilot")
        self.assertIn("ships with OptPilot", action["reason"])

    def test_opening_it_as_a_workspace_is_refused(self) -> None:
        source_id = _configured_catalog_source_id(self.package.resolve())
        with self.assertRaises(RealmConflict) as caught:
            _open_configured_catalog_source_workspace(self.state, source_id)
        self.assertIn("ships with OptPilot", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
