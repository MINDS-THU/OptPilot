"""Focused contracts for Studio's borrowed Realm catalog views.

The catalog index is only a swappable discovery projection.  Every operation
that keeps using catalog bytes must own a separate consumer lease for exactly
as long as those bytes remain in use.  These tests deliberately advance the
catalog head while each operation is active so borrowing the index projection
cannot accidentally pass.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import yaml

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot_studio.ui import server as studio_server
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _list_ui_workspaces,
    _open_catalog_workspace,
    _prepare_transient_interface_runtime_handles,
    _refresh_realm_catalog_projections,
    _remove_ui_workspace_reference,
    _resolve_catalog_identifier,
    _start_catalog_interface_launch,
    _stop_interface_launch,
)


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


@unittest.skipUnless(os.name == "posix", "local Realm projections are POSIX-only")
class StudioCatalogProjectionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:studio-projection-lifecycle-test",
        )
        self.addCleanup(self.runtime.close)
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.state.workspace_runtime.health = lambda: {  # type: ignore[method-assign]
            "ok": True,
            "available": True,
            "engine": "test",
        }
        self.addCleanup(self.state.close_catalog_projections)
        self.package_id = "projection-lifecycle-package"
        self._counter = 0

    def _operation(self, label: str) -> str:
        self._counter += 1
        return f"studio-projection-lifecycle/{self._counter}/{label}"

    def _publish(
        self,
        *,
        publisher_id: str,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
    ) -> Any:
        self._counter += 1
        suffix = f"{self._counter}-{uuid.uuid4().hex[:8]}"
        owner_id = f"studio-projection-artifact-{suffix}"
        source = self.root / f"source-{suffix}"
        source.mkdir()
        for relative, content in files.items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        actor = self.runtime.actor_principal_id
        self.runtime.ledger.create_owner(
            operation_id=self._operation(f"owner-{suffix}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id=actor,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self._operation(f"begin-{suffix}"),
            actor_principal_id=actor,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=actor,
            change_id=change.change_id,
            store_id=self.runtime.content_store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source),
            operation_id=self._operation(f"seal-{suffix}"),
        )
        membership = OwnerMembership(
            self.runtime.content_store.store_id,
            sealed.snapshot_ref,
            PACKAGE_ARTIFACT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self._operation(f"hold-{suffix}"),
            actor_principal_id=actor,
            change_id=change.change_id,
            memberships=(membership,),
        )
        committed = self.runtime.ledger.commit_owner_change(
            operation_id=self._operation(f"commit-{suffix}"),
            actor_principal_id=actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        expected_head = self.runtime.catalog.read_head(package_id=self.package_id)
        identity = {
            "package_id": self.package_id,
            "publisher_id": publisher_id,
            "artifact": str(membership.content_ref),
        }
        return self.runtime.catalog.publish(
            operation_id=self._operation(f"publish-{suffix}"),
            package_id=self.package_id,
            publisher_id=publisher_id,
            source_owner_id=owner_id,
            expected_source_owner_revision=committed.owner_revision,
            source_store_id=membership.store_id,
            source_role=membership.role,
            root_ref=membership.content_ref,
            owned_paths=owned_paths,
            plan_digest=request_digest({"plan": identity}),
            validation_digest=request_digest({"validation": identity}),
            smoke_digest=request_digest({"smoke": identity}),
            expected_head=expected_head,
        )

    def _publish_interface_resource(self) -> dict[str, Any]:
        manifest = {
            "apiVersion": "optpilot.io/v1",
            "config": "resource",
            "id": "viewer",
            "name": "Catalog Viewer",
            "interface": {
                "label": "Viewer",
                "command": ["python", "-m", "http.server", "8123"],
                "cwd": ".",
                "runtime": {"sandbox": "process"},
                "grants": {
                    "envFromHost": [],
                    "network": "disabled",
                    "secretsFromHost": [],
                },
                "presentation": {"kind": "web", "port": 8123},
                "accepts": {
                    "selectionKinds": ["workspace"],
                    "mediaTypes": [],
                },
            },
        }
        self._publish(
            publisher_id="publisher/viewer",
            files={
                "resources/viewer/optpilot.resource.yaml": yaml.safe_dump(
                    manifest, sort_keys=False
                ),
                "resources/viewer/README.md": "viewer revision one\n",
            },
            owned_paths=("resources/viewer",),
        )
        catalog = _catalog_payload(self.state)
        return next(item for item in catalog["resources"] if item["id"] == "viewer")

    def _publish_study_package(self) -> dict[str, Any]:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "toy",
            "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
            "candidate": {
                "format": "parameters",
                "parameters": {
                    "schema": {"x": {"valueType": "float", "min": 0, "max": 1}}
                },
            },
            "metrics": {"source": "return", "keys": ["score"]},
        }
        method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "fixed",
            "entrypoint": {
                "python": "method:Method",
                "pythonPath": ["."],
                "protocol": "batch",
            },
            "accepts": {"formats": ["parameters"]},
        }
        study = {
            "apiVersion": "optpilot.io/v1",
            "config": "study",
            "name": "projection-study",
            "environmentConfig": "../environments/toy/environment.yaml",
            "methodConfig": "../methods/fixed/method.yaml",
            "objective": {"metric": "score", "direction": "maximize"},
            "budget": {"maxTrials": 1},
        }
        self._publish(
            publisher_id="publisher/study",
            files={
                "environments/toy/environment.yaml": yaml.safe_dump(
                    environment, sort_keys=False
                ),
                "environments/toy/evaluator.py": (
                    "def evaluate(candidate_runtime, context):\n"
                    "    return {'score': float(candidate_runtime['x'])}\n"
                ),
                "methods/fixed/method.yaml": yaml.safe_dump(method, sort_keys=False),
                "methods/fixed/method.py": (
                    "class Method:\n"
                    "    def __init__(self, definition, study_spec, rng): pass\n"
                    "    def propose(self, n_candidates, study_state): return []\n"
                ),
                "studies/projection-study.yaml": yaml.safe_dump(study, sort_keys=False),
            },
            owned_paths=(
                "environments/toy",
                "methods/fixed",
                "studies/projection-study.yaml",
            ),
        )
        catalog = _catalog_payload(self.state)
        return next(
            item for item in catalog["studies"] if item["label"] == "projection-study"
        )

    def _advance_catalog_head(self, state: UiState | None = None) -> None:
        self._publish(
            publisher_id=f"publisher/marker/{self._counter}",
            files={f"resources/marker-{self._counter}/README.md": "new catalog head\n"},
            owned_paths=(f"resources/marker-{self._counter}",),
        )
        _refresh_realm_catalog_projections(state or self.state)

    def _active_projection_consumers(self) -> dict[str, dict[str, Any]]:
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT consumer.lease_id, consumer.consumer_id, "
                "consumer.realization_id, consumer.consumer_kind, "
                "consumer.metadata_json "
                "FROM projection_consumers AS consumer "
                "JOIN leases AS lease ON lease.lease_id = consumer.lease_id "
                "WHERE lease.state = 'active'"
            ).fetchall()
        return {
            str(row["lease_id"]): {
                "consumer_id": str(row["consumer_id"]),
                "realization_id": str(row["realization_id"]),
                "consumer_kind": str(row["consumer_kind"]),
                "metadata": json.loads(str(row["metadata_json"])),
            }
            for row in rows
        }

    def _lease_state(self, lease_id: str) -> str:
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            row = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        self.assertIsNotNone(row, f"projection consumer lease disappeared: {lease_id}")
        assert row is not None
        return str(row[0])

    def _new_action_lease_ids(self, before: dict[str, dict[str, Any]]) -> set[str]:
        after = self._active_projection_consumers()
        action_ids = set(after).difference(before)
        self.assertTrue(
            action_ids,
            "The action reused the catalog index without acquiring its own consumer.",
        )
        return action_ids

    def test_catalog_interface_owns_projection_until_terminal_cleanup(self) -> None:
        resource = self._publish_interface_resource()
        before = self._active_projection_consumers()

        with mock.patch.object(
            studio_server.threading.Thread, "start", return_value=None
        ):
            started = _start_catalog_interface_launch(
                self.state, "resource", resource["uid"]
            )

        launch_id = str(started["launch"]["launch_id"])
        action_ids = self._new_action_lease_ids(before)
        job = self.state.interface_launches[launch_id]
        source_root = Path(str(job.runtime_workspace["source_root"]))
        self.assertEqual(
            (source_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )

        self._advance_catalog_head()
        self.assertEqual(
            (source_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )
        self.assertTrue(
            all(self._lease_state(lease_id) == "active" for lease_id in action_ids)
        )

        _prepare_transient_interface_runtime_handles(self.state, job.runtime_workspace)
        runtime_id = str(job.runtime_workspace["id"])
        runtime_record = self.state.workspace_runtime._read_record(runtime_id)
        runtime_record.update(
            {
                "status": "running",
                "container_name": "injected-live-interface",
                "container_may_exist": True,
            }
        )
        self.state.workspace_runtime._write_record(runtime_id, runtime_record)
        # Thread.start was deliberately replaced above, so no execution frame
        # can still submit runtime work. Publish that quiescence proof before
        # exercising terminal cleanup and its retry debt.
        job.execution_quiesced.set()
        job.worker_done.set()
        with mock.patch.object(
            self.state.workspace_runtime,
            "stop",
            side_effect=RuntimeError("injected unconfirmed stop"),
        ):
            pending = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertTrue(
            all(self._lease_state(lease_id) == "active" for lease_id in action_ids),
            "Cleanup debt must retain the catalog source projection.",
        )

        shutil.rmtree(
            self.state.workspace_runtime._workspace_runtime_dir(
                str(job.runtime_workspace["id"])
            )
        )
        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(
            all(self._lease_state(lease_id) != "active" for lease_id in action_ids),
            "The catalog source projection must be released after proven cleanup.",
        )

    def test_read_only_open_owns_projection_until_reference_is_removed(self) -> None:
        resource = self._publish_interface_resource()
        before = self._active_projection_consumers()

        workspace = _open_catalog_workspace(
            self.state,
            "resource",
            resource["uid"],
            editable=False,
        )

        action_ids = self._new_action_lease_ids(before)
        source_root = Path(str(workspace["source_root"]))
        self._advance_catalog_head()
        self.assertEqual(
            (source_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )
        self.assertTrue(
            all(self._lease_state(lease_id) == "active" for lease_id in action_ids)
        )

        removed = _remove_ui_workspace_reference(self.state, str(workspace["id"]))
        self.assertTrue(removed["deleted"])
        self.assertTrue(
            all(self._lease_state(lease_id) != "active" for lease_id in action_ids),
            "Removing the borrowed view must release its projection consumer.",
        )

    def test_read_only_reference_rehydrates_exact_revision_without_provider_path(
        self,
    ) -> None:
        resource = self._publish_interface_resource()
        before_open = self._active_projection_consumers()
        workspace = _open_catalog_workspace(
            self.state,
            "resource",
            resource["uid"],
            editable=False,
        )
        workspace_id = str(workspace["id"])
        first_consumer_ids = self._new_action_lease_ids(before_open)
        first_root = Path(str(workspace["source_root"]))
        self.assertEqual(
            (first_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )

        index_path = self.state.workspace_index_path
        stored_payload = json.loads(index_path.read_text(encoding="utf-8"))
        stored = next(
            item for item in stored_payload["workspaces"] if item["id"] == workspace_id
        )
        self.assertEqual(stored["root"], "")
        self.assertEqual(stored["source_root"], "")
        self.assertEqual(stored["realization_state"], "closed")
        self.assertEqual(stored["catalog_origin"]["revision"], 1)
        self.assertEqual(stored["catalog_origin"]["selection"]["source_revision"], 1)
        self.assertEqual(
            stored["catalog_origin"]["selection"]["selection_digest"],
            self.state._catalog_workspace_projections[
                workspace_id
            ].selection.selection_digest,
        )
        self._assert_path_free(stored)
        self.assertNotIn(str(first_root), json.dumps(stored, sort_keys=True))

        # Simulate the process-local half of a Studio restart: every catalog
        # projection is released while the durable, path-free index remains.
        self.state.close_catalog_projections()
        self.assertTrue(
            all(
                self._lease_state(lease_id) != "active"
                for lease_id in first_consumer_ids
            )
        )

        restarted = UiState(
            cwd=self.state.cwd,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(restarted.close_catalog_projections)
        before_rehydrate = self._active_projection_consumers()
        self.assertNotIn(
            workspace_id,
            {item["id"] for item in _list_ui_workspaces(restarted)},
            "Read-only Catalog support views must not appear in Workspaces.",
        )
        listed = _list_ui_workspaces(restarted, include_support=True)
        reopened = next(item for item in listed if item["id"] == workspace_id)
        self.assertFalse(reopened["visible_in_workspaces"])
        reopened_consumer_ids = self._new_action_lease_ids(before_rehydrate)
        reopened_root = Path(str(reopened["source_root"]))
        self.assertEqual(
            (reopened_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )
        self.assertEqual(
            restarted._catalog_workspace_projections[workspace_id].revision,
            1,
        )

        # A new catalog head must only swap the discovery index.  The reopened
        # workspace remains authorized by, and projected from, its historical
        # SelectionRef.
        self._advance_catalog_head(restarted)
        current_head = self.runtime.catalog.read_head(package_id=self.package_id)
        self.assertIsNotNone(current_head)
        assert current_head is not None
        self.assertEqual(current_head.revision, 2)
        self.assertEqual(
            (reopened_root / "README.md").read_text(encoding="utf-8"),
            "viewer revision one\n",
        )
        self.assertTrue(
            all(
                self._lease_state(lease_id) == "active"
                for lease_id in reopened_consumer_ids
            )
        )

        persisted_again = json.loads(index_path.read_text(encoding="utf-8"))
        reopened_stored = next(
            item for item in persisted_again["workspaces"] if item["id"] == workspace_id
        )
        self.assertEqual(reopened_stored["root"], "")
        self.assertEqual(reopened_stored["source_root"], "")
        self._assert_path_free(reopened_stored)
        self.assertNotIn(
            str(reopened_root), json.dumps(reopened_stored, sort_keys=True)
        )

        removed = _remove_ui_workspace_reference(restarted, workspace_id)
        self.assertTrue(removed["deleted"])
        self.assertTrue(
            all(
                self._lease_state(lease_id) != "active"
                for lease_id in reopened_consumer_ids
            ),
            "Removing the rehydrated reference must close its consumer.",
        )
        self.assertFalse(index_path.exists() and workspace_id in index_path.read_text())

    def test_study_planning_seals_from_an_action_owned_projection(self) -> None:
        study = self._publish_study_package()
        study_path = _resolve_catalog_identifier(self.state, "study", str(study["uid"]))
        before = self._active_projection_consumers()
        action_ids: set[str] = set()
        observed: dict[str, Path] = {}
        planned = SimpleNamespace(launch_id="launch-projection-study")

        def plan_local_package(**kwargs: Any) -> Any:
            action_ids.update(self._new_action_lease_ids(before))
            observed["package_root"] = Path(kwargs["package_root"])
            observed["study_path"] = Path(kwargs["study_config_path"])
            self._advance_catalog_head()
            self.assertTrue(observed["package_root"].is_dir())
            self.assertEqual(
                yaml.safe_load(observed["study_path"].read_text(encoding="utf-8"))[
                    "name"
                ],
                "projection-study",
            )
            self.assertTrue(
                all(self._lease_state(lease_id) == "active" for lease_id in action_ids),
                "Catalog index advancement invalidated the study sealing source.",
            )
            return planned

        with (
            mock.patch.object(
                self.runtime.study_launches,
                "plan_local_package",
                side_effect=plan_local_package,
            ),
            mock.patch.object(
                studio_server,
                "_schedule_study_launch_execution",
                return_value=True,
            ) as schedule,
        ):
            result = self.state.launch_study(
                study_path,
                operation_id="studio-projection-study/launch",
            )

        self.assertIs(result, planned)
        schedule.assert_called_once_with(self.state, launch_id=planned.launch_id)
        self.assertTrue(action_ids)
        self.assertTrue(
            all(self._lease_state(lease_id) != "active" for lease_id in action_ids),
            "Study planning must release the borrowed source after Core seals it.",
        )

    def test_exact_catalog_study_ref_is_adopted_without_preliminary_projection(
        self,
    ) -> None:
        study = self._publish_study_package()
        preparation = SimpleNamespace(
            study_definition=SimpleNamespace(
                manifest=SimpleNamespace(run_definition=object())
            )
        )
        planned = SimpleNamespace(launch_id="launch-selected-catalog-study")

        with (
            mock.patch.object(
                self.runtime.retained_study_service,
                "prepare_selected_package",
                return_value=preparation,
            ) as prepare,
            mock.patch.object(
                self.runtime.retained_study_service,
                "prepare_local_package",
                side_effect=AssertionError(
                    "an exact Catalog selection must not be recaptured"
                ),
            ),
            mock.patch.object(
                self.runtime.study_launches,
                "plan_definition",
                return_value=planned,
            ) as plan,
            mock.patch.object(
                self.runtime.study_launches,
                "plan_local_package",
                side_effect=AssertionError(
                    "an exact Catalog selection must not use local-package planning"
                ),
            ),
            mock.patch.object(
                self.runtime.projection_service,
                "project_selection_read_only",
                side_effect=AssertionError(
                    "Studio must not project a Catalog selection before preparation"
                ),
            ) as project,
            mock.patch.object(
                studio_server,
                "_borrow_catalog_entry_ref_projection",
                side_effect=AssertionError(
                    "exact Catalog Study launch must not borrow a filesystem view"
                ),
            ),
            mock.patch.object(
                studio_server,
                "_validate_study",
                side_effect=AssertionError(
                    "selected Study compilation owns validation"
                ),
            ),
            mock.patch.object(
                studio_server,
                "method_environment_names",
                return_value=(),
            ),
            mock.patch.object(
                studio_server,
                "_schedule_study_launch_execution",
                return_value=True,
            ) as schedule,
        ):
            result = self.state.launch_study(
                catalog_entry=study["ref"],
                operation_id="studio-projection-study/launch-exact-ref",
            )

        self.assertIs(result, planned)
        prepare.assert_called_once()
        prepared = prepare.call_args.kwargs
        selection = prepared["package_selection"]
        self.assertEqual(selection.source_kind, "catalog")
        self.assertEqual(selection.source_id, self.package_id)
        self.assertEqual(selection.source_revision, study["ref"]["source_revision"])
        self.assertEqual(
            prepared["study_config_relative_path"],
            study["ref"]["focus_path"],
        )
        self.assertEqual(
            prepared["operation_id"],
            "studio-projection-study/launch-exact-ref",
        )
        plan.assert_called_once()
        self.assertIs(plan.call_args.kwargs["preparation"], preparation)
        self.assertEqual(plan.call_args.kwargs["display_name"], "projection-study")
        schedule.assert_called_once_with(self.state, launch_id=planned.launch_id)
        project.assert_not_called()

    def _assert_path_free(self, value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                self._assert_path_free(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_path_free(child)
        elif isinstance(value, str):
            self.assertFalse(
                Path(value).is_absolute(),
                f"Absolute provider path leaked into Workspace metadata: {value}",
            )


if __name__ == "__main__":
    unittest.main()
