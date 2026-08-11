from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot_studio.ui.coordination_store import (
    COORDINATION_STORAGE_UNAVAILABLE_MESSAGE,
    CoordinationStorageUnavailable,
    StudioCoordinationStore,
    WorkspacePurpose,
    coordination_database_path,
    studio_project_state_directory,
)
from optpilot_studio.ui.server import (
    UiState,
    _atomic_write_workspace_index_payload,
    _read_workspace_index,
    _upsert_ui_workspace,
    _write_workspace_index,
)


class StudioWorkspaceIndexStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.realm = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="studio-workspace-index-test",
        )
        self.states: list[UiState] = []

    def tearDown(self) -> None:
        for state in reversed(self.states):
            state.close_coordination()
        self.realm.close()
        self.temporary.cleanup()

    def _open_state(self, *, realm_backed: bool = True) -> UiState:
        state = UiState(
            cwd=self.project,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.realm if realm_backed else None,
        )
        self.states.append(state)
        return state

    def _legacy_path(self) -> Path:
        return self.project / ".optpilot-ui" / "workspaces" / "index.json"

    @staticmethod
    def _workspace(workspace_id: str, root: Path) -> dict[str, object]:
        return {
            "id": workspace_id,
            "title": workspace_id,
            "root": str(root),
            "source_type": "local-folder",
            "mode": "editable",
            "status": "ready",
        }

    def test_realm_backed_index_is_os_local_but_workspace_content_root_is_unchanged(
        self,
    ) -> None:
        state = self._open_state()

        self.assertEqual(
            state.workspace_index_path,
            studio_project_state_directory(
                self.project, authority_root=self.realm.root
            )
            / "workspace-index.json",
        )
        self.assertEqual(
            state.workspaces_dir,
            state.cwd / ".optpilot-ui" / "workspaces",
        )
        with self.assertRaises(ValueError):
            state.workspace_index_path.relative_to(self.project)

    def test_realm_backed_state_adopts_legacy_coordination_authority(self) -> None:
        legacy_path = coordination_database_path(self.project)
        legacy = StudioCoordinationStore(legacy_path)
        try:
            legacy.put_workspace_purpose(
                operation_id="legacy/workspace-purpose",
                workspace_id="legacy-workspace",
                purpose=WorkspacePurpose.USER_PROJECT,
            )
            legacy_instance_id = legacy.instance_id
        finally:
            legacy.close()

        state = self._open_state()

        self.assertEqual(
            state.coordination.database_path,
            studio_project_state_directory(
                self.project,
                authority_root=self.realm.root,
            )
            / "studio-coordination.sqlite3",
        )
        self.assertEqual(state.coordination.instance_id, legacy_instance_id)
        self.assertEqual(
            state.coordination.get_workspace_purpose("legacy-workspace").purpose,
            WorkspacePurpose.USER_PROJECT,
        )
        self.assertTrue(legacy_path.is_file())

    def test_realm_less_state_retains_the_legacy_index_location(self) -> None:
        state = self._open_state(realm_backed=False)

        self.assertEqual(
            state.workspace_index_path,
            state.cwd / ".optpilot-ui" / "workspaces" / "index.json",
        )
        workspace_root = self.root / "realm-less-workspace"
        workspace_root.mkdir()
        _write_workspace_index(
            state, [self._workspace("realm-less", workspace_root)]
        )
        self.assertTrue(self._legacy_path().is_file())

    def test_valid_legacy_index_migrates_once_without_modifying_legacy(self) -> None:
        workspace_root = self.root / "legacy-workspace"
        workspace_root.mkdir()
        legacy_payload = {
            "workspaces": [self._workspace("legacy", workspace_root)]
        }
        legacy_path = self._legacy_path()
        legacy_path.parent.mkdir(parents=True)
        legacy_text = json.dumps(legacy_payload, indent=1) + "\n"
        legacy_path.write_text(legacy_text, encoding="utf-8")

        state = self._open_state()

        self.assertEqual(
            [item["id"] for item in _read_workspace_index(state)], ["legacy"]
        )
        self.assertTrue(state.workspace_index_path.is_file())
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), legacy_text)

    def test_existing_target_wins_over_a_different_legacy_index(self) -> None:
        legacy_root = self.root / "legacy"
        target_root = self.root / "target"
        legacy_root.mkdir()
        target_root.mkdir()
        legacy_path = self._legacy_path()
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(
            json.dumps(
                {"workspaces": [self._workspace("legacy", legacy_root)]}
            ),
            encoding="utf-8",
        )
        target_path = (
            studio_project_state_directory(
                self.project, authority_root=self.realm.root
            )
            / "workspace-index.json"
        )
        target_path.parent.mkdir(parents=True)
        target_path.write_text(
            json.dumps(
                {"workspaces": [self._workspace("target", target_root)]}
            ),
            encoding="utf-8",
        )

        state = self._open_state()

        self.assertEqual(
            [item["id"] for item in _read_workspace_index(state)], ["target"]
        )

    def test_migration_publish_never_replaces_a_racing_target(self) -> None:
        target = self.root / "local-state" / "workspace-index.json"
        target.parent.mkdir()
        original = {"workspaces": [{"id": "target"}]}
        target.write_text(json.dumps(original), encoding="utf-8")

        published = _atomic_write_workspace_index_payload(
            target,
            {"workspaces": [{"id": "legacy"}]},
            replace_existing=False,
        )

        self.assertFalse(published)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)
        self.assertEqual(tuple(target.parent.glob(".*.tmp")), ())

    def test_malformed_legacy_index_fails_closed_with_stable_typed_error(
        self,
    ) -> None:
        legacy_path = self._legacy_path()
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("{malformed", encoding="utf-8")

        with self.assertRaises(CoordinationStorageUnavailable) as caught:
            self._open_state()

        self.assertEqual(
            str(caught.exception), COORDINATION_STORAGE_UNAVAILABLE_MESSAGE
        )
        target_path = (
            studio_project_state_directory(
                self.project, authority_root=self.realm.root
            )
            / "workspace-index.json"
        )
        self.assertFalse(target_path.exists())
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), "{malformed")

    def test_atomic_write_translates_os_error_and_preserves_previous_index(
        self,
    ) -> None:
        state = self._open_state(realm_backed=False)
        old_root = self.root / "old"
        new_root = self.root / "new"
        old_root.mkdir()
        new_root.mkdir()
        _write_workspace_index(state, [self._workspace("old", old_root)])
        before = state.workspace_index_path.read_bytes()

        with mock.patch.object(
            os,
            "replace",
            side_effect=OSError("disk I/O error that must stay private"),
        ), self.assertRaises(CoordinationStorageUnavailable) as caught:
            _write_workspace_index(
                state, [self._workspace("new", new_root)]
            )

        self.assertEqual(
            str(caught.exception), COORDINATION_STORAGE_UNAVAILABLE_MESSAGE
        )
        self.assertNotIn("disk I/O", str(caught.exception))
        self.assertEqual(state.workspace_index_path.read_bytes(), before)
        self.assertEqual(
            list(state.workspace_index_path.parent.glob(".index.json.*.tmp")),
            [],
        )

    def test_concurrent_upserts_do_not_lose_workspace_records(self) -> None:
        state = self._open_state(realm_backed=False)
        records = []
        for index in range(24):
            root = self.root / f"workspace-{index}"
            root.mkdir()
            records.append(self._workspace(f"workspace-{index}", root))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda workspace: _upsert_ui_workspace(state, workspace),
                    records,
                )
            )

        self.assertEqual(
            {item["id"] for item in _read_workspace_index(state)},
            {item["id"] for item in records},
        )


if __name__ == "__main__":
    unittest.main()
