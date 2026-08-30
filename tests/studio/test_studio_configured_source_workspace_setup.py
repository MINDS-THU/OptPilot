"""Configured Catalog sources use the ordinary durable Workspace Setup flow."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import yaml

from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.refs import SnapshotRef
from optpilot_studio.ui.server import (
    CONFIGURED_SOURCE_PLAN_SCOPE,
    CONFIGURED_SOURCE_WORKSPACE_SCHEMA,
    UiState,
    _apply_package_plan,
    _catalog_payload,
    _configured_source_publisher_id,
    _create_ui_workspace,
    _handler_factory,
    _iter_yaml_files,
    _list_ui_workspaces,
    _open_configured_catalog_source_workspace,
    _prepare_package_plan,
    _public_studio_payload,
    _require_ui_workspace,
    _studio_actor_id,
    _validate_package_plan,
    _workspace_file_tree,
)


class StudioConfiguredSourceWorkspaceSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "configured" / "mutable-package"
        viewer = self.package / "resources" / "viewer"
        viewer.mkdir(parents=True)
        (viewer / "optpilot.resource.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "resource",
                    "id": "viewer",
                    "name": "Mutable viewer",
                    "purpose": "viewer",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (viewer / "README.md").write_text("viewer\n", encoding="utf-8")
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:configured-source-workspace-setup",
        )
        self.addCleanup(self.runtime.close)
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[self.package],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(self.state.close_catalog_projections)
        self.addCleanup(self.state.close_coordination)
        self.source_id = _catalog_payload(self.state)["sources"][0]["source_id"]

    def _open_and_check(self):
        opened = _open_configured_catalog_source_workspace(
            self.state, self.source_id
        )
        workspace_id = opened["id"]
        plan = _prepare_package_plan(self.state, workspace_id, {})["package_plan"]
        checked = _validate_package_plan(
            self.state, workspace_id, plan["id"]
        )
        return opened, checked["package_plan"], checked["setup"]

    def _handler(self):
        handler = object.__new__(_handler_factory(self.state))
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        )
        return handler, responses

    def _publish_neighbor(self) -> dict[str, object]:
        workspace = _create_ui_workspace(self.state, {"title": "Neighbor"})
        plan = _prepare_package_plan(
            self.state,
            workspace["id"],
            {
                "package_id": "mutable-package",
                "kind": "resource",
                "resource_id": "neighbor",
                "description": "Neighbor resource",
            },
        )["package_plan"]
        _validate_package_plan(self.state, workspace["id"], plan["id"])
        return _apply_package_plan(self.state, workspace["id"], plan["id"])

    def test_open_reuses_one_external_workspace_without_public_path_or_copy(self) -> None:
        first = _open_configured_catalog_source_workspace(
            self.state, self.source_id
        )
        second = _open_configured_catalog_source_workspace(
            self.state, self.source_id
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(first["ownership"], "external-reference")
        self.assertEqual(first["source_type"], "configured-catalog-source")
        self.assertNotIn("root", first)
        self.assertNotIn("source_root", first)
        internal = _require_ui_workspace(self.state, first["id"])
        self.assertEqual(Path(internal["root"]), self.package.resolve())
        self.assertEqual(
            len(
                [
                    item
                    for item in _list_ui_workspaces(self.state)
                    if item["id"] == first["id"]
                ]
            ),
            1,
        )
        serialized = json.dumps(
            _public_studio_payload({"workspaces": _list_ui_workspaces(self.state)}),
            sort_keys=True,
        )
        self.assertNotIn(str(self.package), serialized)
        self.assertTrue((self.package / "resources" / "viewer").is_dir())

    def test_http_action_accepts_only_the_opaque_source_and_schema(self) -> None:
        handler, responses = self._handler()
        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": CONFIGURED_SOURCE_WORKSPACE_SCHEMA
        }

        handler._handle_configured_catalog_source_post(
            f"/api/catalog/sources/{self.source_id}/workspace"
        )

        payload, status = responses[-1]
        self.assertEqual(status, HTTPStatus.CREATED)
        self.assertNotIn(str(self.package), json.dumps(payload, sort_keys=True))
        self.assertNotIn("root", payload["workspace"])

        handler._read_json_body = lambda: {  # type: ignore[method-assign]
            "schema": CONFIGURED_SOURCE_WORKSPACE_SCHEMA,
            "path": str(self.package),
        }
        with self.assertRaisesRegex(ValueError, "fields differ"):
            handler._handle_configured_catalog_source_post(
                f"/api/catalog/sources/{self.source_id}/workspace"
            )

    def test_setup_keeps_publisher_authority_and_replays_one_publication(self) -> None:
        opened, plan, setup = self._open_and_check()

        self.assertEqual(plan["publication_scope"], CONFIGURED_SOURCE_PLAN_SCOPE)
        self.assertEqual(plan["package_id"], "mutable-package")
        self.assertEqual(
            plan["publisher_id"],
            _configured_source_publisher_id("mutable-package", self.source_id),
        )
        self.assertTrue(setup["check"]["accepted"])
        self.assertEqual(plan["validation"]["test_policy"], "static-only")
        self.assertEqual(plan["artifact"]["owned_paths"], ["resources"])

        first = _apply_package_plan(self.state, opened["id"], plan["id"])
        replay = _apply_package_plan(self.state, opened["id"], plan["id"])
        actions = self.state.coordination.list_actions(
            actor_id=_studio_actor_id(self.state), action_kind="catalog-publication"
        )

        self.assertEqual(first["catalog"]["head"], replay["catalog"]["head"])
        self.assertEqual(first["catalog"]["head"]["revision"], 1)
        self.assertEqual(replay["setup"]["state"], "registered")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].state.value, "succeeded")
        catalog = _catalog_payload(self.state)
        source = next(
            item
            for item in catalog["sources"]
            if item["source_id"] == self.source_id
        )
        self.assertEqual(source["realm_head"]["revision"], 1)
        viewer = next(
            item for item in catalog["resources"] if item["id"] == "viewer"
        )
        self.assertEqual(viewer["ref"]["source_kind"], "realm-catalog")
        public = _public_studio_payload(first)
        self.assertNotIn(str(self.package), json.dumps(public, sort_keys=True))
        self.assertNotIn("source_root", public["package_plan"])
        self.assertNotIn("publisher_id", public["package_plan"])
        self.assertNotIn(str(self.package), json.dumps(catalog, sort_keys=True))

    def test_setup_excludes_generated_local_directories_from_checked_package(
        self,
    ) -> None:
        generated = (
            self.package
            / "resources"
            / "viewer"
            / "frontend"
            / "node_modules"
            / "@types"
            / "node"
            / "ts5.6"
        )
        generated.mkdir(parents=True)
        (generated / "index.d.ts").write_text("generated\n", encoding="utf-8")
        root_cache = self.package / ".pytest_cache"
        root_cache.mkdir()
        (root_cache / "README.md").write_text("generated\n", encoding="utf-8")
        for directory_name in (".runtime", ".uv-cache"):
            local_state = self.package / "resources" / "viewer" / directory_name
            local_state.mkdir()
            (local_state / "state.bin").write_text(
                "machine-local\n", encoding="utf-8"
            )

        opened, plan, setup = self._open_and_check()

        self.assertTrue(setup["check"]["accepted"])
        manifest = self.runtime.content_store.verify_tree(
            SnapshotRef.parse(plan["artifact"]["content_ref"]),
            verify_children=False,
        )
        paths = {entry.path for entry in manifest.entries}
        self.assertIn("resources/viewer/README.md", paths)
        self.assertFalse(any("node_modules" in path.split("/") for path in paths))
        self.assertFalse(any(".pytest_cache" in path.split("/") for path in paths))
        for directory_name in (".runtime", ".uv-cache"):
            self.assertFalse(
                any(directory_name in path.split("/") for path in paths)
            )

        # Machine-local dependency churn does not invalidate the exact authored
        # package selected by Check.
        (generated / "index.d.ts").write_text(
            "updated generated bytes\n", encoding="utf-8"
        )
        published = _apply_package_plan(self.state, opened["id"], plan["id"])
        self.assertEqual(published["catalog"]["head"]["revision"], 1)

    def test_runtime_boundaries_are_also_hidden_from_source_scans(self) -> None:
        for directory_name in (".runtime", ".uv-cache"):
            local_state = self.package / "resources" / "viewer" / directory_name
            local_state.mkdir()
            (local_state / "generated.yaml").write_text(
                "apiVersion: optpilot.io/v1\nconfig: resource\nid: generated\n",
                encoding="utf-8",
            )

        yaml_paths = {
            path.relative_to(self.package.resolve()).as_posix()
            for path in _iter_yaml_files(self.package)
        }
        workspace_paths = {
            item["path"]
            for item in _workspace_file_tree(
                self.package.resolve(), self.package.resolve(), max_files=100
            )
        }

        self.assertEqual(
            yaml_paths,
            {"resources/viewer/optpilot.resource.yaml"},
        )
        self.assertFalse(any(".runtime" in path.split("/") for path in workspace_paths))
        self.assertFalse(any(".uv-cache" in path.split("/") for path in workspace_paths))

    def test_catalog_yaml_scan_does_not_follow_symlinks(self) -> None:
        outside = self.root / "outside.yaml"
        outside.write_text(
            "apiVersion: optpilot.io/v1\nconfig: resource\nid: outside\n",
            encoding="utf-8",
        )
        external_link = self.package / "resources" / "viewer" / "outside.yaml"
        external_link.symlink_to(outside)
        real = self.package / "resources" / "viewer" / "real.yaml"
        real.write_text("domain: data\n", encoding="utf-8")
        in_tree_link = self.package / "resources" / "viewer" / "alias.yaml"
        in_tree_link.symlink_to(real)

        paths = {
            path.relative_to(self.package.resolve()).as_posix()
            for path in _iter_yaml_files(self.package)
        }

        self.assertNotIn("resources/viewer/outside.yaml", paths)
        self.assertNotIn("resources/viewer/alias.yaml", paths)
        self.assertIn("resources/viewer/real.yaml", paths)

    def test_setup_check_rejects_zero_recognized_entries_without_publishing(
        self,
    ) -> None:
        shutil.rmtree(self.package / "resources")
        environments = self.package / "environments"
        environments.mkdir()
        (environments / "README.txt").write_text(
            "not a config\n", encoding="utf-8"
        )
        (self.package / "notes.yaml").write_text(
            "notes: ignored\n", encoding="utf-8"
        )

        opened = _open_configured_catalog_source_workspace(
            self.state, self.source_id
        )
        plan = _prepare_package_plan(
            self.state, opened["id"], {}
        )["package_plan"]
        checked = _validate_package_plan(
            self.state, opened["id"], plan["id"]
        )

        validation = checked["package_plan"]["validation"]
        self.assertFalse(validation["valid"])
        self.assertFalse(checked["setup"]["check"]["accepted"])
        facts = validation["configured_source_validation"]["facts"]
        codes = {fact["code"] for fact in facts}
        self.assertIn("no_recognized_entries", codes)
        self.assertIn("ignored_yaml", codes)
        self.assertIsNone(
            self.runtime.catalog.read_head(package_id="mutable-package")
        )

    def test_setup_registers_a_later_checked_version_with_the_same_authority(
        self,
    ) -> None:
        opened, first_plan, _setup = self._open_and_check()
        first = _apply_package_plan(
            self.state, opened["id"], first_plan["id"]
        )
        self.assertEqual(first["catalog"]["head"]["revision"], 1)
        (self.package / "resources" / "viewer" / "README.md").write_text(
            "updated through Workspace Setup\n", encoding="utf-8"
        )

        plan = _prepare_package_plan(
            self.state,
            opened["id"],
            {"refresh": True},
        )["package_plan"]
        checked = _validate_package_plan(
            self.state, opened["id"], plan["id"]
        )
        plan = checked["package_plan"]
        updated = _apply_package_plan(self.state, opened["id"], plan["id"])

        self.assertEqual(updated["catalog"]["head"]["revision"], 2)
        manifest = self.runtime.catalog.read_revision(
            package_id="mutable-package", revision=2
        )
        self.assertEqual(len(manifest.applications), 1)
        self.assertEqual(
            manifest.applications[0].publisher_id,
            _configured_source_publisher_id("mutable-package", self.source_id),
        )

    def test_lost_response_reconciles_the_existing_catalog_result(self) -> None:
        opened, plan, _setup = self._open_and_check()
        from optpilot_studio.ui import server

        original = server._complete_registration_publication
        with mock.patch.object(
            server,
            "_complete_registration_publication",
            side_effect=RuntimeError("response connection lost"),
        ):
            with self.assertRaisesRegex(RuntimeError, "response connection lost"):
                _apply_package_plan(self.state, opened["id"], plan["id"])

        with mock.patch.object(
            server, "_complete_registration_publication", wraps=original
        ):
            replay = _apply_package_plan(
                self.state, opened["id"], plan["id"]
            )

        self.assertEqual(replay["catalog"]["head"]["revision"], 1)
        self.assertEqual(replay["setup"]["state"], "registered")
        actions = self.state.coordination.list_actions(
            actor_id=_studio_actor_id(self.state), action_kind="catalog-publication"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].state.value, "succeeded")

    def test_head_change_after_check_requires_checking_again(self) -> None:
        opened, plan, _setup = self._open_and_check()
        self._publish_neighbor()

        with self.assertRaises(RealmConflict) as raised:
            _apply_package_plan(self.state, opened["id"], plan["id"])

        self.assertEqual(
            getattr(raised.exception, "code", ""),
            "configured_package_head_changed",
        )

        head = self.runtime.catalog.read_head(package_id="mutable-package")
        self.assertIsNotNone(head)
        self.assertEqual(head.revision, 1)

    def test_overlapping_existing_publisher_is_not_silently_replaced(self) -> None:
        self._publish_neighbor()
        opened, plan, setup = self._open_and_check()

        self.assertTrue(setup["check"]["accepted"])
        with self.assertRaises(RealmConflict) as raised:
            _apply_package_plan(self.state, opened["id"], plan["id"])

        self.assertEqual(
            getattr(raised.exception, "code", ""),
            "configured_package_ownership_conflict",
        )

        head = self.runtime.catalog.read_head(package_id="mutable-package")
        self.assertIsNotNone(head)
        self.assertEqual(head.revision, 1)

    def test_register_never_reads_changed_bytes_after_check(self) -> None:
        opened, plan, _setup = self._open_and_check()
        (self.package / "resources" / "viewer" / "README.md").write_text(
            "changed after Check\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(RealmConflict, "changed after validation"):
            _apply_package_plan(self.state, opened["id"], plan["id"])

        self.assertIsNone(self.runtime.catalog.read_head(package_id="mutable-package"))


if __name__ == "__main__":
    unittest.main()
