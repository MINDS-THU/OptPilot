from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot_studio.ui import server as studio_server
from optpilot_studio.ui.server import (
    UiState,
    _apply_package_plan,
    _catalog_payload,
    _create_ui_workspace,
    _prepare_package_plan,
    _read_package_plan,
    _write_package_plan,
    _validate_package_plan,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class StudioRealmCatalogCutoverTest(unittest.TestCase):
    """Contract tests for Studio's canonical Realm-backed catalog boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.studio_root = self.base / "studio"
        self.realm_root = self.base / "realm"

    def _open_state(self) -> tuple[UiState, LocalRealmRuntime]:
        runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="local-user:studio-catalog-cutover-test",
        )
        self.addCleanup(runtime.close)
        state = UiState(
            cwd=self.studio_root,
            catalog_roots=[self.studio_root / "configured-import-catalog"],
            run_roots=[],
            realm_runtime=runtime,
        )
        self.addCleanup(state.close_catalog_projections)
        return state, runtime

    def _resource_plan(
        self,
        state: UiState,
        *,
        package_id: str,
        resource_id: str,
        content: str,
        title: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        workspace = _create_ui_workspace(
            state,
            {"title": title or f"Workspace for {resource_id}"},
        )
        Path(str(workspace["root"]), "README.md").write_text(
            content,
            encoding="utf-8",
        )
        plan = _prepare_package_plan(
            state,
            workspace["id"],
            {
                "kind": "resource",
                "package_id": package_id,
                "resource_id": resource_id,
            },
        )["package_plan"]
        plan = _validate_package_plan(state, workspace["id"], plan["id"])[
            "package_plan"
        ]
        self.assertTrue(plan["validation"]["valid"], plan)
        return workspace, plan

    def _project_application_root(
        self,
        runtime: LocalRealmRuntime,
        *,
        package_id: str,
        publisher_id: str,
        revision: int,
    ) -> Any:
        selection = runtime.ledger.mint_catalog_package_application_selection(
            actor_principal_id=runtime.actor_principal_id,
            package_id=package_id,
            publisher_id=publisher_id,
            revision=revision,
        )
        coordinate = uuid.uuid4().hex
        return runtime.projection_service.project_selection_read_only(
            operation_id=f"test/studio-catalog-cutover/project/{coordinate}",
            actor_principal_id=runtime.actor_principal_id,
            selection=selection,
            holder_id=f"studio-catalog-cutover-test-{coordinate}",
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
            consumer_kind="studio-catalog-cutover-test",
        )

    def test_apply_writes_the_package_into_a_durable_editable_folder(self) -> None:
        state, runtime = self._open_state()
        package_id = "realm-only-package"
        workspace, plan = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="realm-tool",
            content="# Realm tool\n",
        )

        applied = _apply_package_plan(state, workspace["id"], plan["id"])
        head = runtime.catalog.read_head(package_id=package_id)
        catalog = _catalog_payload(state)

        self.assertTrue(applied["applied"], applied)
        self.assertIsNotNone(head)
        # A package is a folder you can open and edit; registering writes it.
        package_folder = self.studio_root / "catalog" / package_id
        self.assertTrue(
            package_folder.is_dir(),
            "Registering must write the package into its catalog folder.",
        )
        written = sorted(
            path for path in package_folder.rglob("*") if path.is_file()
        )
        self.assertTrue(written, "The package folder must not be empty.")
        # ...and it must be editable, not a read-only copy.
        editable = written[0]
        editable.write_text(editable.read_text() + "\n# edited\n")
        self.assertTrue(
            any(
                item["id"] == "realm-tool"
                and item.get("package_id") == package_id
                for item in catalog["resources"]
            ),
            catalog,
        )

    def test_restart_discovers_published_catalog_from_realm(self) -> None:
        first_state, first_runtime = self._open_state()
        package_id = "restart-package"
        workspace, plan = self._resource_plan(
            first_state,
            package_id=package_id,
            resource_id="survives-restart",
            content="# Survives restart\n",
        )
        _apply_package_plan(first_state, workspace["id"], plan["id"])
        first_state.close_catalog_projections()
        first_runtime.close()

        restarted_state, restarted_runtime = self._open_state()
        catalog = _catalog_payload(restarted_state)

        head = restarted_runtime.catalog.read_head(package_id=package_id)
        self.assertIsNotNone(head)
        self.assertTrue(
            any(
                item["id"] == "survives-restart"
                and item.get("package_id") == package_id
                for item in catalog["resources"]
            ),
            catalog,
        )
        self.assertTrue(
            (self.studio_root / "catalog" / package_id).is_dir(),
            "The package folder must survive a restart.",
        )

    def test_multi_plan_composition_replacement_and_collision(self) -> None:
        state, runtime = self._open_state()
        package_id = "composed-package"
        workspace_a, plan_a = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="tool-a",
            content="tool-a-v1\n",
        )
        first = _apply_package_plan(state, workspace_a["id"], plan_a["id"])
        self.assertEqual(first["catalog"]["head"]["revision"], 1)

        workspace_b, plan_b = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="tool-b",
            content="tool-b-v1\n",
        )
        second = _apply_package_plan(state, workspace_b["id"], plan_b["id"])
        self.assertEqual(second["catalog"]["head"]["revision"], 2)

        Path(str(workspace_a["root"]), "README.md").write_text(
            "tool-a-v2\n",
            encoding="utf-8",
        )
        plan_a = _validate_package_plan(state, workspace_a["id"], plan_a["id"])[
            "package_plan"
        ]
        replacement = _apply_package_plan(
            state,
            workspace_a["id"],
            plan_a["id"],
        )
        self.assertEqual(replacement["catalog"]["head"]["revision"], 3)

        head = runtime.catalog.read_head(package_id=package_id)
        assert head is not None
        projection = self._project_application_root(
            runtime,
            package_id=package_id,
            publisher_id=plan_a["publisher_id"],
            revision=head.revision,
        )
        try:
            self.assertEqual(
                (projection.root_path / "resources" / "tool-a" / "README.md")
                .read_text(encoding="utf-8"),
                "tool-a-v2\n",
            )
            self.assertEqual(
                (projection.root_path / "resources" / "tool-b" / "README.md")
                .read_text(encoding="utf-8"),
                "tool-b-v1\n",
            )
        finally:
            projection.close()

        # A workspace that started before the last registration landed is
        # working from an older version of the package, so registering it is
        # refused rather than replacing what it never saw. This replaces an
        # older expectation of a cross-owner collision: a catalog belongs to one
        # person, so a package has one owner and there is no one to collide with.
        # What can still go wrong is registering stale work, and that is what is
        # checked here.
        stale_workspace, stale_plan = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="tool-a",
            content="built-before-the-others\n",
            title="Stale workspace",
        )
        # As if this workspace had been prepared before tool-a existed: the part
        # of the package it changes has moved on since.
        stale_plan["catalog_selection_fingerprint"] = "0" * 64
        _write_package_plan(state, stale_workspace["id"], stale_plan)
        with self.assertRaises((ValueError, RealmConflict)):
            _apply_package_plan(
                state,
                stale_workspace["id"],
                stale_plan["id"],
            )
        unchanged_head = runtime.catalog.read_head(package_id=package_id)
        assert unchanged_head is not None
        self.assertEqual(unchanged_head.revision, 3)
        self.assertEqual(unchanged_head.manifest_digest, head.manifest_digest)

    def test_persisted_publication_metadata_is_path_free(self) -> None:
        state, _runtime = self._open_state()
        package_id = "path-free-package"
        workspace, plan = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="portable-tool",
            content="# Portable tool\n",
        )

        result = _apply_package_plan(state, workspace["id"], plan["id"])
        persisted = _read_package_plan(state, workspace["id"], plan["id"])
        metadata = {
            "catalog_base": persisted["catalog_base"],
            "publication": persisted["publication"],
        }
        serialized = json.dumps(metadata, sort_keys=True)
        forbidden_keys = {
            "config_path",
            "projection_root",
            "root_path",
            "source_path",
            "source_root",
        }

        self.assertNotIn(str(self.base), serialized)
        self._assert_keys_absent(metadata, forbidden_keys)
        self._assert_no_absolute_paths(metadata)
        self.assertEqual(
            persisted["publication"]["package_id"],
            package_id,
        )
        self.assertEqual(
            persisted["publication"]["published_head"],
            result["catalog"]["head"],
        )
        self.assertTrue(persisted["publication"]["operation_id"])
        self.assertNotIn("projection_root", result.get("catalog", {}))
        self._assert_keys_absent(
            result["workspace"].get("registered_entries", []),
            {"config_path", "projection_root", "root_path"},
        )

    def test_publication_success_survives_catalog_realization_failure(self) -> None:
        state, runtime = self._open_state()
        package_id = "published-before-realization"
        workspace, plan = self._resource_plan(
            state,
            package_id=package_id,
            resource_id="published-tool",
            content="# Published tool\n",
        )

        with mock.patch.object(
            studio_server,
            "_refresh_realm_catalog_projections",
            side_effect=RuntimeError("injected catalog realization failure"),
        ):
            result = _apply_package_plan(state, workspace["id"], plan["id"])

        head = runtime.catalog.read_head(package_id=package_id)
        persisted = _read_package_plan(state, workspace["id"], plan["id"])
        self.assertTrue(result["applied"], result)
        self.assertIsNotNone(head)
        assert head is not None
        self.assertEqual(persisted["status"], "applied")
        self.assertEqual(
            persisted["publication"]["published_head"],
            head.to_dict(),
        )
        self.assertNotIn("projection_root", result.get("catalog", {}))

    def _assert_keys_absent(
        self,
        value: Any,
        forbidden_keys: set[str],
    ) -> None:
        if isinstance(value, dict):
            self.assertFalse(
                forbidden_keys.intersection(value),
                f"Host-path fields leaked into persisted/public metadata: {value}",
            )
            for child in value.values():
                self._assert_keys_absent(child, forbidden_keys)
        elif isinstance(value, list):
            for child in value:
                self._assert_keys_absent(child, forbidden_keys)

    def _assert_no_absolute_paths(self, value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                self._assert_no_absolute_paths(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_no_absolute_paths(child)
        elif isinstance(value, str):
            self.assertFalse(
                Path(value).is_absolute(),
                f"Absolute host path leaked into persisted publication metadata: {value}",
            )


if __name__ == "__main__":
    unittest.main()
