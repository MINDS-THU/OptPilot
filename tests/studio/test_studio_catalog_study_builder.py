"""Contracts for building studies from exact Realm catalog entries.

The Study Builder must not turn catalog projection paths into a second public
coordinate system.  It assembles one durable editable workspace from exact
whole-package selections, writes a package-relative study there, and launches
that workspace by durable identity plus relative path.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest import mock

import yaml

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot_studio.ui import server as studio_server
from optpilot_studio.ui.coordination_store import (
    CoordinationConflict,
    StudyDraftState,
)
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _draft_study,
    _list_saved_study_drafts,
    _list_ui_workspaces,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"
EXPECTED_DRAFT_KEYS = {
    "workspace_id",
    "study_relative_path",
    "workspace_revision",
    "draft_id",
    "draft",
    "yaml",
    "compatibility",
    "validation",
    "saved_as_draft",
    "draft_revision",
}


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class StudioCatalogStudyBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.realm_root = self.root / "realm"
        self.studio_root = self.root / "studio"
        self.runtime = self._open_runtime()
        self.state = self._open_state(self.runtime)
        self._counter = 0
        self.package_id = "catalog-study-builder-package"
        self.publisher_id = "publisher/catalog-study-builder"

    def _open_runtime(self) -> LocalRealmRuntime:
        runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="local-user:studio-catalog-study-builder-test",
        )
        self.addCleanup(runtime.close)
        return runtime

    def _open_state(self, runtime: LocalRealmRuntime) -> UiState:
        state = UiState(
            cwd=self.studio_root,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=runtime,
        )
        self.addCleanup(state.close_catalog_projections)
        return state

    def _operation(self, label: str) -> str:
        self._counter += 1
        return f"studio-catalog-study-builder/{self._counter}/{label}"

    def _publish_component_revision(self, marker: str) -> Any:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "toy-environment",
            "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
            "candidate": {
                "format": "parameters",
                "parameters": {
                    "schema": {
                        "x": {"valueType": "float", "min": 0, "max": 1}
                    }
                },
            },
            "metrics": {"source": "return", "keys": ["score"]},
        }
        method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "fixed-method",
            "entrypoint": {
                "python": "method:Method",
                "pythonPath": ["."],
                "protocol": "batch",
            },
            "accepts": {"formats": ["parameters"]},
        }
        return self._publish(
            files={
                "environments/toy/environment.yaml": yaml.safe_dump(
                    environment, sort_keys=False
                ),
                "environments/toy/evaluator.py": (
                    f"REVISION = {marker!r}\n"
                    "def evaluate(candidate_runtime, context):\n"
                    "    return {'score': float(candidate_runtime['x'])}\n"
                ),
                "methods/fixed/method.yaml": yaml.safe_dump(
                    method, sort_keys=False
                ),
                "methods/fixed/method.py": (
                    f"REVISION = {marker!r}\n"
                    "class Method:\n"
                    "    def __init__(self, definition, study_spec, rng): pass\n"
                    "    def propose(self, n_candidates, study_state): return []\n"
                ),
            },
            owned_paths=("environments/toy", "methods/fixed"),
        )

    def _publish(
        self,
        *,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
        package_id: str | None = None,
        publisher_id: str | None = None,
    ) -> Any:
        package_id = package_id or self.package_id
        publisher_id = publisher_id or self.publisher_id
        suffix = uuid.uuid4().hex[:12]
        owner_id = f"studio-study-builder-artifact-{suffix}"
        source = self.root / f"source-{suffix}"
        source.mkdir()
        for relative, content in files.items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        actor = self.runtime.actor_principal_id
        self.runtime.ledger.create_owner(
            operation_id=self._operation(f"create-{suffix}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id=actor,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self._operation(f"begin-{suffix}"),
            actor_principal_id=actor,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
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
        identity = {
            "package_id": package_id,
            "publisher_id": publisher_id,
            "artifact": str(membership.content_ref),
        }
        return self.runtime.catalog.publish(
            operation_id=self._operation(f"publish-{suffix}"),
            package_id=package_id,
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
            expected_head=self.runtime.catalog.read_head(package_id=package_id),
        )

    def _component_entries(self) -> tuple[dict[str, Any], dict[str, Any]]:
        catalog = _catalog_payload(self.state)
        environment = next(
            item
            for item in catalog["environments"]
            if item["id"] == "toy-environment"
        )
        method = next(
            item for item in catalog["methods"] if item["id"] == "fixed-method"
        )
        return environment, method

    def _draft_payload(
        self,
        environment: dict[str, Any],
        method: dict[str, Any],
        **overrides: Any,
    ) -> dict[str, Any]:
        payload = {
            "environment_ref": environment["ref"],
            "method_ref": method["ref"],
            "name": "managed-catalog-study",
            "metric": "score",
            "maxTrials": 3,
            **overrides,
        }
        if not payload.get("workspace_id"):
            payload.setdefault("request_id", str(uuid.uuid4()))
        return payload

    def test_draft_creates_and_updates_one_durable_managed_workspace(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()

        creation_payload = self._draft_payload(environment, method)
        first = _draft_study(self.state, creation_payload)

        self.assertEqual(set(first), EXPECTED_DRAFT_KEYS)
        self._assert_public_path_free(first)
        self.assertEqual(first["workspace_revision"], 2)
        self.assertTrue(first["draft_id"])
        self.assertTrue(first["validation"]["valid"], first["validation"])
        study_relative_path = self._assert_portable_study_path(
            first["study_relative_path"]
        )
        workspace = self._workspace(first["workspace_id"])
        self.assertEqual(workspace["ownership"], "realm-managed")
        self.assertEqual(
            workspace["realm_workspace_revision"], first["workspace_revision"]
        )
        origin = workspace["catalog_origin"]
        self.assertEqual(origin["assembly_outcome"], "adopt")
        self.assertRegex(origin["assembly_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [component["kind"] for component in origin["components"]],
            ["environment", "method"],
        )
        study_path = Path(str(workspace["root"])) / study_relative_path
        first_yaml = yaml.safe_load(study_path.read_text(encoding="utf-8"))
        self.assertEqual(
            first_yaml["environmentConfig"],
            "../environments/toy/environment.yaml",
        )
        self.assertEqual(
            first_yaml["methodConfig"],
            "../methods/fixed/method.yaml",
        )
        self.assertFalse(Path(first_yaml["environmentConfig"]).is_absolute())
        self.assertFalse(Path(first_yaml["methodConfig"]).is_absolute())

        replay = _draft_study(self.state, creation_payload)
        self.assertEqual(replay["workspace_id"], first["workspace_id"])
        self.assertEqual(replay["workspace_revision"], first["workspace_revision"])
        self.assertEqual(replay["draft_id"], first["draft_id"])
        self.assertEqual(replay["study_relative_path"], first["study_relative_path"])
        self.assertEqual(replay["yaml"], first["yaml"])
        self.assertEqual(len(self.runtime.editable_workspaces.list_workspaces()), 1)

        first_revision = int(first["workspace_revision"])
        updated = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                workspace_id=first["workspace_id"],
                expected_workspace_revision=first_revision,
                maxTrials=9,
            ),
        )

        self.assertEqual(set(updated), EXPECTED_DRAFT_KEYS)
        self._assert_public_path_free(updated)
        self.assertEqual(updated["workspace_id"], first["workspace_id"])
        self.assertEqual(
            updated["study_relative_path"], first["study_relative_path"]
        )
        self.assertEqual(updated["draft_id"], first["draft_id"])
        self.assertEqual(updated["workspace_revision"], first_revision + 1)
        updated_workspace = self._workspace(updated["workspace_id"])
        updated_path = Path(str(updated_workspace["root"])) / study_relative_path
        updated_yaml = yaml.safe_load(updated_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_yaml["budget"]["maxTrials"], 9)

        with self.assertRaises(RealmConflict):
            _draft_study(
                self.state,
                self._draft_payload(
                    environment,
                    method,
                    workspace_id=first["workspace_id"],
                    expected_workspace_revision=first_revision,
                    maxTrials=11,
                ),
            )
        unchanged = self.runtime.editable_workspaces.read_workspace(
            workspace_id=first["workspace_id"]
        )
        self.assertEqual(
            unchanged.workspace_revision, updated["workspace_revision"]
        )
        self.assertEqual(
            yaml.safe_load(updated_path.read_text(encoding="utf-8"))["budget"]
            ["maxTrials"],
            9,
        )

    def test_only_explicitly_saved_draft_is_reopenable_and_hidden_from_workspaces(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        saved = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="abababab-abab-4bab-8bab-abababababab",
            ),
        )

        self.assertTrue(saved["saved_as_draft"])
        self.assertEqual(saved["draft_revision"], 1)
        self.assertFalse(
            any(
                item["id"] == saved["workspace_id"]
                for item in _list_ui_workspaces(self.state)
            )
        )
        backing = next(
            item
            for item in _list_ui_workspaces(
                self.state, include_support=True
            )
            if item["id"] == saved["workspace_id"]
        )
        self.assertEqual(backing["purpose"], "study-draft-backing")
        drafts = _list_saved_study_drafts(self.state)["drafts"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["draft_id"], saved["draft_id"])
        self.assertEqual(drafts[0]["workspace_revision"], saved["workspace_revision"])
        self.assertEqual(drafts[0]["config"]["name"], "managed-catalog-study")

    def test_saved_draft_listing_refreshes_dynamic_launch_readiness(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        saved = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="aeaeaeae-aeae-4eae-8eae-aeaeaeaeaeae",
            ),
        )
        refreshed_validation = {
            "valid": True,
            "launch": {
                "eligible": True,
                "code": "ready",
                "reason": None,
            },
            "runtime_environment": {
                "missing_names": [],
                "ready": True,
                "requirements": [],
            },
        }

        with mock.patch.object(
            studio_server,
            "_validate_study",
            return_value=refreshed_validation,
        ) as validate:
            [listed] = _list_saved_study_drafts(self.state)["drafts"]

        self.assertEqual(listed["draft_id"], saved["draft_id"])
        self.assertEqual(listed["validation"], refreshed_validation)
        validate.assert_called_once()
        validated_path = Path(validate.call_args.args[0])
        self.assertEqual(
            validated_path.name,
            Path(saved["study_relative_path"]).name,
        )
        self.assertIs(validate.call_args.kwargs["state"], self.state)

    def test_study_save_never_invokes_the_full_checkout_commit(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()

        with mock.patch.object(
            self.runtime.editable_workspaces,
            "commit_workspace",
            side_effect=AssertionError(
                "Study save must not recapture the complete component tree"
            ),
        ):
            first = _draft_study(
                self.state,
                self._draft_payload(
                    environment,
                    method,
                    save_as_draft=True,
                    draft_action_id="acacacac-acac-4cac-8cac-acacacacacac",
                ),
            )
            updated = _draft_study(
                self.state,
                self._draft_payload(
                    environment,
                    method,
                    workspace_id=first["workspace_id"],
                    expected_workspace_revision=first["workspace_revision"],
                    expected_draft_revision=first["draft_revision"],
                    save_as_draft=True,
                    draft_action_id="adadadad-adad-4dad-8dad-adadadadadad",
                    maxTrials=7,
                ),
            )

        self.assertEqual(
            updated["workspace_revision"], int(first["workspace_revision"]) + 1
        )
        self.assertEqual(updated["draft_revision"], 2)
        self.assertEqual(updated["draft"]["budget"]["maxTrials"], 7)
        with self.assertRaisesRegex(RealmConflict, "Study draft changed"):
            _draft_study(
                self.state,
                self._draft_payload(
                    environment,
                    method,
                    workspace_id=updated["workspace_id"],
                    expected_workspace_revision=updated["workspace_revision"],
                    expected_draft_revision=first["draft_revision"],
                    save_as_draft=True,
                    draft_action_id="ae0ae0ae-ae0a-4e0a-8e0a-ae0ae0ae0ae0",
                    maxTrials=13,
                ),
            )
        unchanged = self.runtime.editable_workspaces.read_workspace(
            workspace_id=updated["workspace_id"]
        )
        self.assertEqual(
            unchanged.workspace_revision, updated["workspace_revision"]
        )
        study_path = (
            Path(str(self._workspace(updated["workspace_id"])["root"]))
            / self._assert_portable_study_path(updated["study_relative_path"])
        )
        self.assertEqual(
            yaml.safe_load(study_path.read_text(encoding="utf-8"))["budget"][
                "maxTrials"
            ],
            7,
        )

    def test_concurrent_saves_cannot_capture_another_requests_yaml(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        first = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="aeaeaeae-aeae-4eae-8eae-aeaeaeaeaeae",
            ),
        )
        workspace = self._workspace(first["workspace_id"])
        study_path = (
            Path(str(workspace["root"]))
            / self._assert_portable_study_path(first["study_relative_path"])
        )
        first_update = self._draft_payload(
            environment,
            method,
            workspace_id=first["workspace_id"],
            expected_workspace_revision=first["workspace_revision"],
            expected_draft_revision=first["draft_revision"],
            save_as_draft=True,
            draft_action_id="afafafaf-afaf-4faf-8faf-afafafafafaf",
            maxTrials=7,
        )
        competing_update = self._draft_payload(
            environment,
            method,
            workspace_id=first["workspace_id"],
            expected_workspace_revision=first["workspace_revision"],
            expected_draft_revision=first["draft_revision"],
            save_as_draft=True,
            draft_action_id="b0b0b0b0-b0b0-40b0-80b0-b0b0b0b0b0b0",
            maxTrials=11,
        )
        key = studio_server._study_draft_mutation_key(
            self.state, payload=first_update
        )
        write_entered = threading.Event()
        release_write = threading.Event()
        original_write = studio_server._atomic_write_text
        study_write_count = 0
        count_guard = threading.Lock()

        def gated_write(path: Path, content: str) -> None:
            nonlocal study_write_count
            if Path(path) == study_path:
                with count_guard:
                    study_write_count += 1
                write_entered.set()
                if not release_write.wait(timeout=10):
                    raise TimeoutError("concurrent Study save gate timed out")
            original_write(path, content)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        first_future: concurrent.futures.Future[Any]
        second_future: concurrent.futures.Future[Any]
        try:
            with mock.patch.object(
                studio_server, "_atomic_write_text", side_effect=gated_write
            ):
                first_future = executor.submit(
                    _draft_study, self.state, first_update
                )
                self.assertTrue(write_entered.wait(timeout=5))
                second_future = executor.submit(
                    _draft_study, self.state, competing_update
                )
                self._wait_for_study_mutation_users(key, expected=2)
                with count_guard:
                    self.assertEqual(study_write_count, 1)
                release_write.set()
                saved = first_future.result(timeout=10)
                with self.assertRaises(RealmConflict):
                    second_future.result(timeout=10)
        finally:
            release_write.set()
            executor.shutdown(wait=True)

        self.assertEqual(saved["draft"]["budget"]["maxTrials"], 7)
        self.assertEqual(
            yaml.safe_load(study_path.read_text(encoding="utf-8"))["budget"][
                "maxTrials"
            ],
            7,
        )
        record = self.state.coordination.get_study_draft(first["draft_id"])
        self.assertEqual(record.workspace_revision, saved["workspace_revision"])
        self.assertEqual(record.revision, saved["draft_revision"])

    def test_concurrent_save_and_discard_share_the_same_draft_fence(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        first = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1",
            ),
        )
        workspace = self._workspace(first["workspace_id"])
        study_path = (
            Path(str(workspace["root"]))
            / self._assert_portable_study_path(first["study_relative_path"])
        )
        update = self._draft_payload(
            environment,
            method,
            workspace_id=first["workspace_id"],
            expected_workspace_revision=first["workspace_revision"],
            expected_draft_revision=first["draft_revision"],
            save_as_draft=True,
            draft_action_id="b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2",
            maxTrials=8,
        )
        key = studio_server._study_draft_mutation_key(self.state, payload=update)
        write_entered = threading.Event()
        release_write = threading.Event()
        original_write = studio_server._atomic_write_text

        def gated_write(path: Path, content: str) -> None:
            if Path(path) == study_path:
                write_entered.set()
                if not release_write.wait(timeout=10):
                    raise TimeoutError("concurrent Study discard gate timed out")
            original_write(path, content)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            with mock.patch.object(
                studio_server, "_atomic_write_text", side_effect=gated_write
            ):
                save_future = executor.submit(_draft_study, self.state, update)
                self.assertTrue(write_entered.wait(timeout=5))
                discard_future = executor.submit(
                    studio_server._discard_saved_study_draft,
                    self.state,
                    first["draft_id"],
                    {
                        "request_id": "b3b3b3b3-b3b3-43b3-83b3-b3b3b3b3b3b3",
                        "expected_revision": first["draft_revision"],
                    },
                )
                self._wait_for_study_mutation_users(key, expected=2)
                release_write.set()
                saved = save_future.result(timeout=10)
                with self.assertRaises(CoordinationConflict):
                    discard_future.result(timeout=10)
        finally:
            release_write.set()
            executor.shutdown(wait=True)

        record = self.state.coordination.get_study_draft(first["draft_id"])
        self.assertIs(record.state, StudyDraftState.ACTIVE)
        self.assertEqual(record.revision, saved["draft_revision"])
        self.assertEqual(record.workspace_revision, saved["workspace_revision"])
        self.assertEqual(
            yaml.safe_load(study_path.read_text(encoding="utf-8"))["budget"][
                "maxTrials"
            ],
            8,
        )

    def test_draft_survives_catalog_head_advance_and_process_restart(self) -> None:
        first_head = self._publish_component_revision("revision-one").head
        environment, method = self._component_entries()
        draft = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc",
            ),
        )
        workspace_id = str(draft["workspace_id"])
        workspace_revision = int(draft["workspace_revision"])
        relative_path = self._assert_portable_study_path(
            draft["study_relative_path"]
        )

        second_head = self._publish_component_revision("revision-two").head
        self.assertGreater(second_head.revision, first_head.revision)
        current_environment, _current_method = self._component_entries()
        self.assertEqual(
            current_environment["ref"]["source_revision"], second_head.revision
        )

        self.runtime.editable_workspaces.delete_checkout(
            operation_id=self._operation("delete-checkout-before-restart"),
            workspace_id=workspace_id,
        )
        self.state.close_catalog_projections()
        self.runtime.close()

        restarted_runtime = self._open_runtime()
        restarted_state = self._open_state(restarted_runtime)
        [listed] = _list_saved_study_drafts(restarted_state)["drafts"]
        self.assertEqual(
            listed["availability"],
            {"available": True, "code": "ready", "reason": None},
        )
        reopened = next(
            item
            for item in _list_ui_workspaces(
                restarted_state, include_support=True
            )
            if item["id"] == workspace_id
        )
        reopened_root = Path(str(reopened["root"]))
        reopened_draft = yaml.safe_load(
            (reopened_root / relative_path).read_text(encoding="utf-8")
        )

        self.assertEqual(reopened["realm_workspace_revision"], workspace_revision)
        self.assertEqual(
            reopened_draft["environmentConfig"],
            "../environments/toy/environment.yaml",
        )
        self.assertIn(
            "revision-one",
            (reopened_root / "environments/toy/evaluator.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertNotIn(
            "revision-two",
            (reopened_root / "environments/toy/evaluator.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_saved_draft_listing_repairs_a_missing_checkout_at_the_exact_revision(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        saved = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
            ),
        )
        workspace_id = str(saved["workspace_id"])
        workspace_revision = int(saved["workspace_revision"])
        original = self._workspace(workspace_id)
        original_root = Path(str(original["root"]))
        self.runtime.editable_workspaces.delete_checkout(
            operation_id=self._operation("delete-saved-draft-checkout"),
            workspace_id=workspace_id,
        )
        self.assertFalse(original_root.exists())

        # Workspace discovery must preserve the Studio metadata while the
        # provider checkout is closed.  In particular, it must not replace the
        # indexed Study origin with a generic phantom Workspace.
        self.assertFalse(
            any(item["id"] == workspace_id for item in _list_ui_workspaces(self.state))
        )
        closed = next(
            item
            for item in _list_ui_workspaces(self.state, include_support=True)
            if item["id"] == workspace_id
        )
        self.assertTrue(closed["reopen_required"])
        self.assertEqual(closed["root"], "")
        self.assertEqual(closed["purpose"], "study-draft-backing")
        self.assertEqual(closed["catalog_origin"]["draft_id"], saved["draft_id"])

        listed = _list_saved_study_drafts(self.state)["drafts"]
        self.assertEqual(len(listed), 1)
        recovered = listed[0]
        self.assertEqual(
            recovered["availability"],
            {"available": True, "code": "ready", "reason": None},
        )
        self.assertEqual(recovered["workspace_revision"], workspace_revision)
        self.assertEqual(recovered["config"]["name"], "managed-catalog-study")
        self.assertEqual(recovered["yaml"], saved["yaml"])
        self.assertEqual(recovered["environment_ref"], environment["ref"])
        self.assertEqual(recovered["method_ref"], method["ref"])

        reopened = self._workspace(workspace_id)
        self.assertFalse(reopened["reopen_required"])
        self.assertEqual(reopened["realm_workspace_revision"], workspace_revision)
        self.assertTrue(Path(str(reopened["root"])).is_dir())
        self.assertFalse(
            any(item["id"] == workspace_id for item in _list_ui_workspaces(self.state))
        )

    def test_saved_draft_revision_conflict_is_visible_without_loading_newer_files(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        saved = _draft_study(
            self.state,
            self._draft_payload(
                environment,
                method,
                save_as_draft=True,
                draft_action_id="dededede-dede-4ede-8ede-dededededede",
            ),
        )
        workspace = self._workspace(str(saved["workspace_id"]))
        root = Path(str(workspace["root"]))
        (root / "later-edit.txt").write_text("newer work\n", encoding="utf-8")
        committed = self.runtime.editable_workspaces.commit_workspace(
            operation_id=self._operation("advance-beyond-saved-draft"),
            workspace_id=str(saved["workspace_id"]),
            expected_workspace_revision=int(saved["workspace_revision"]),
        )
        self.assertGreater(
            committed.current_revision, int(saved["workspace_revision"])
        )

        [listed] = _list_saved_study_drafts(self.state)["drafts"]
        self.assertFalse(listed["availability"]["available"])
        self.assertEqual(
            listed["availability"]["code"], "workspace-revision-changed"
        )
        self.assertEqual(listed["config"], {})
        self.assertEqual(listed["yaml"], "")
        self.assertEqual(listed["workspace_revision"], saved["workspace_revision"])

    def test_draft_retry_finishes_the_same_target_after_post_create_failure(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        payload = self._draft_payload(environment, method)

        with mock.patch.object(
            studio_server,
            "_atomic_write_text",
            side_effect=OSError("injected draft write failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected draft write failure"):
                _draft_study(self.state, payload)

        created = self.runtime.editable_workspaces.list_workspaces()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].workspace_revision, 1)

        recovered = _draft_study(self.state, payload)
        self.assertEqual(recovered["workspace_id"], created[0].workspace_id)
        self.assertEqual(recovered["workspace_revision"], 2)
        self.assertEqual(len(self.runtime.editable_workspaces.list_workspaces()), 1)

    def test_draft_retry_reopens_current_head_after_commit_response_loss(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        payload = self._draft_payload(environment, method)
        commit = studio_server._commit_managed_workspace_file

        def commit_then_lose_response(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise OSError("injected response loss after draft commit")

        with mock.patch.object(
            studio_server,
            "_commit_managed_workspace_file",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(OSError, "injected response loss"):
                _draft_study(self.state, payload)

        committed = self.runtime.editable_workspaces.list_workspaces()
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].workspace_revision, 2)

        replayed = _draft_study(self.state, payload)
        self.assertEqual(replayed["workspace_id"], committed[0].workspace_id)
        self.assertEqual(replayed["workspace_revision"], 2)
        self.assertEqual(len(self.runtime.editable_workspaces.list_workspaces()), 1)

    def test_launch_commits_and_adopts_the_exact_managed_workspace_without_copying(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        draft = _draft_study(
            self.state,
            self._draft_payload(environment, method),
        )
        workspace = self._workspace(draft["workspace_id"])
        workspace_root = Path(str(workspace["root"])).resolve()
        (workspace_root / "launch-time-edit.txt").write_text(
            "must be part of the committed selection\n",
            encoding="utf-8",
        )

        self._publish_component_revision("revision-two")
        self._component_entries()  # Refresh the discovery projection to revision two.
        preparation = SimpleNamespace(
            study_definition=SimpleNamespace(
                manifest=SimpleNamespace(run_definition=object())
            )
        )
        planned = SimpleNamespace(launch_id="launch-managed-catalog-draft")

        with (
            mock.patch.object(
                studio_server,
                "_commit_managed_workspace",
                wraps=studio_server._commit_managed_workspace,
            ) as commit,
            mock.patch.object(
                self.runtime.retained_study_service,
                "prepare_selected_package",
                return_value=preparation,
            ) as prepare,
            mock.patch.object(
                self.runtime.retained_study_service,
                "prepare_local_package",
                side_effect=AssertionError(
                    "an exact Workspace selection must not be recaptured"
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
                    "an exact Workspace selection must not use local-package planning"
                ),
            ),
            mock.patch.object(
                self.runtime.projection_service,
                "project_selection_read_only",
                side_effect=AssertionError(
                    "Studio must not project a Workspace before retained preparation"
                ),
            ) as project,
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
            mock.patch.object(
                studio_server,
                "_borrow_catalog_entry_ref_projection",
                side_effect=AssertionError(
                    "managed-workspace launch must not borrow a catalog projection"
                ),
            ),
        ):
            result = self.state.launch_study(
                workspace_id=draft["workspace_id"],
                study_relative_path=draft["study_relative_path"],
                expected_workspace_revision=draft["workspace_revision"],
                operation_id="studio-catalog-study-builder/launch-managed",
                method_request_timeout_seconds=2000,
            )

        self.assertIs(result, planned)
        commit.assert_called_once_with(
            self.state,
            draft["workspace_id"],
            {"expected_workspace_revision": draft["workspace_revision"]},
            operation_id=(
                "studio-catalog-study-builder/launch-managed/"
                "commit-study-workspace"
            ),
        )
        prepare.assert_called_once()
        prepared = prepare.call_args.kwargs
        selection = prepared["package_selection"]
        committed_revision = int(draft["workspace_revision"]) + 1
        self.assertEqual(selection.source_kind, "workspace")
        self.assertEqual(selection.source_id, draft["workspace_id"])
        self.assertEqual(selection.source_revision, committed_revision)
        self.assertEqual(
            prepared["study_config_relative_path"],
            draft["study_relative_path"],
        )
        self.assertEqual(
            prepared["operation_id"],
            "studio-catalog-study-builder/launch-managed",
        )
        plan.assert_called_once()
        self.assertIs(plan.call_args.kwargs["preparation"], preparation)
        self.assertEqual(
            plan.call_args.kwargs[
                "execution_profile"
            ].method_request_timeout_seconds,
            2000.0,
        )
        schedule.assert_called_once_with(self.state, launch_id=planned.launch_id)
        project.assert_not_called()
        summary = self.runtime.editable_workspaces.read_workspace(
            workspace_id=draft["workspace_id"]
        )
        self.assertEqual(summary.workspace_revision, committed_revision)

    def test_launch_retry_reuses_workspace_commit_after_revision_advance(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()
        draft = _draft_study(
            self.state,
            self._draft_payload(environment, method),
        )
        workspace = self._workspace(draft["workspace_id"])
        root = Path(str(workspace["root"]))
        (root / "launch-time-edit.txt").write_text(
            "must be committed once\n", encoding="utf-8"
        )
        planned = SimpleNamespace(launch_id="launch-managed-replay")
        preparation = SimpleNamespace(
            study_definition=SimpleNamespace(
                manifest=SimpleNamespace(run_definition=object())
            )
        )
        operation_id = "studio-catalog-study-builder/launch-replay"

        with (
            mock.patch.object(
                self.runtime.retained_study_service,
                "prepare_selected_package",
                return_value=preparation,
            ) as prepare,
            mock.patch.object(
                self.runtime.study_launches,
                "plan_definition",
                return_value=planned,
            ) as plan,
            mock.patch.object(
                studio_server,
                "method_environment_names",
                return_value=(),
            ),
            mock.patch.object(
                studio_server,
                "_schedule_study_launch_execution",
                return_value=True,
            ),
        ):
            first = self.state.launch_study(
                workspace_id=draft["workspace_id"],
                study_relative_path=draft["study_relative_path"],
                expected_workspace_revision=draft["workspace_revision"],
                operation_id=operation_id,
            )
            replay = self.state.launch_study(
                workspace_id=draft["workspace_id"],
                study_relative_path=draft["study_relative_path"],
                expected_workspace_revision=draft["workspace_revision"],
                operation_id=operation_id,
            )

        self.assertIs(first, planned)
        self.assertIs(replay, planned)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(plan.call_count, 2)
        summary = self.runtime.editable_workspaces.read_workspace(
            workspace_id=draft["workspace_id"]
        )
        self.assertEqual(
            summary.workspace_revision, int(draft["workspace_revision"]) + 1
        )

    def test_draft_rejects_legacy_paths_and_logical_catalog_coordinates(self) -> None:
        self._publish_component_revision("revision-one")
        environment, method = self._component_entries()

        rejected = (
            {
                "environment_path": environment["path"],
                "method_path": method["path"],
            },
            {
                "environment_ref": environment["path"],
                "method_ref": method["ref"],
            },
            {
                "environment_ref": environment["ref"],
                "method_ref": method["path"],
            },
            {
                "environment_ref": environment["ref"],
                "method_ref": method["ref"],
                "environment_path": environment["path"],
            },
        )
        for payload in rejected:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ValueError,
                    "exact|environment_ref|method_ref|removed|unsupported",
                ):
                    _draft_study(
                        self.state,
                        {
                            "request_id": "99999999-9999-4999-8999-999999999999",
                            **payload,
                        },
                    )

    def test_draft_rejects_component_refs_from_different_package_revisions(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        old_environment, _old_method = self._component_entries()
        self._publish_component_revision("revision-two")
        _current_environment, current_method = self._component_entries()
        before = self.runtime.editable_workspaces.list_workspaces()

        with self.assertRaisesRegex(
            ValueError,
            "whole-tree union rejected|conflict",
        ):
            _draft_study(
                self.state,
                self._draft_payload(old_environment, current_method),
            )

        self.assertEqual(self.runtime.editable_workspaces.list_workspaces(), before)

    def test_update_requires_both_exact_component_refs_from_base_assembly(
        self,
    ) -> None:
        self._publish_component_revision("revision-one")
        old_environment, old_method = self._component_entries()
        draft = _draft_study(
            self.state,
            self._draft_payload(old_environment, old_method),
        )
        self._publish_component_revision("revision-two")
        current_environment, current_method = self._component_entries()

        mismatched_components = (
            (current_environment, old_method),
            (old_environment, current_method),
        )
        for environment, method in mismatched_components:
            with self.subTest(
                environment_revision=environment["ref"]["source_revision"],
                method_revision=method["ref"]["source_revision"],
            ):
                with self.assertRaisesRegex(
                    RealmConflict,
                    "component refs differ",
                ):
                    _draft_study(
                        self.state,
                        self._draft_payload(
                            environment,
                            method,
                            workspace_id=draft["workspace_id"],
                            expected_workspace_revision=draft[
                                "workspace_revision"
                            ],
                        ),
                    )

        summary = self.runtime.editable_workspaces.read_workspace(
            workspace_id=draft["workspace_id"]
        )
        self.assertEqual(summary.workspace_revision, draft["workspace_revision"])

    def test_draft_unions_non_overlapping_environment_and_method_packages(
        self,
    ) -> None:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "toy-environment",
            "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
            "candidate": {
                "format": "parameters",
                "parameters": {
                    "schema": {
                        "x": {"valueType": "float", "min": 0, "max": 1}
                    }
                },
            },
            "metrics": {"source": "return", "keys": ["score"]},
        }
        method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "fixed-method",
            "entrypoint": {
                "python": "method:Method",
                "pythonPath": ["."],
                "protocol": "batch",
            },
            "accepts": {"formats": ["parameters"]},
        }
        environment_package_id = "catalog-study-builder-environment-package"
        method_package_id = "catalog-study-builder-method-package"
        self._publish(
            files={
                "environments/toy/environment.yaml": yaml.safe_dump(
                    environment, sort_keys=False
                ),
                "environments/toy/evaluator.py": (
                    "def evaluate(candidate_runtime, context):\n"
                    "    return {'score': float(candidate_runtime['x'])}\n"
                ),
            },
            owned_paths=("environments/toy",),
            package_id=environment_package_id,
            publisher_id="publisher/catalog-study-builder-environment",
        )
        self._publish(
            files={
                "methods/fixed/method.yaml": yaml.safe_dump(
                    method, sort_keys=False
                ),
                "methods/fixed/method.py": (
                    "class Method:\n"
                    "    def __init__(self, definition, study_spec, rng): pass\n"
                    "    def propose(self, n_candidates, study_state): return []\n"
                ),
            },
            owned_paths=("methods/fixed",),
            package_id=method_package_id,
            publisher_id="publisher/catalog-study-builder-method",
        )
        environment_entry, method_entry = self._component_entries()

        result = _draft_study(
            self.state,
            self._draft_payload(environment_entry, method_entry),
        )

        self._assert_public_path_free(result)
        workspace = self._workspace(result["workspace_id"])
        root = Path(str(workspace["root"]))
        self.assertTrue((root / "environments/toy/environment.yaml").is_file())
        self.assertTrue((root / "methods/fixed/method.yaml").is_file())
        origin = workspace["catalog_origin"]
        self.assertEqual(origin["assembly_outcome"], "union")
        self.assertRegex(origin["assembly_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [
                component["ref"]["source_id"]
                for component in origin["components"]
            ],
            [environment_package_id, method_package_id],
        )
        self.assertFalse(
            any(
                Path(component["ref"]["focus_path"]).is_absolute()
                for component in origin["components"]
            )
        )

    def _workspace(self, workspace_id: str) -> dict[str, Any]:
        return next(
            workspace
            for workspace in _list_ui_workspaces(
                self.state, include_support=True
            )
            if workspace["id"] == workspace_id
        )

    def _wait_for_study_mutation_users(self, key: str, *, expected: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.state._study_draft_mutation_locks_guard:
                entry = self.state._study_draft_mutation_locks.get(key)
                users = 0 if entry is None else entry.users
            if users == expected:
                return
            time.sleep(0.01)
        self.fail(
            f"Study mutation lock {key} did not reach {expected} users."
        )

    def _assert_portable_study_path(self, value: Any) -> Path:
        text = str(value or "")
        posix = PurePosixPath(text)
        self.assertTrue(text)
        self.assertFalse(posix.is_absolute())
        self.assertNotIn("..", posix.parts)
        self.assertEqual(posix.suffix, ".yaml")
        self.assertEqual(posix.parts[0], "studies")
        return Path(*posix.parts)

    def _assert_public_path_free(self, value: Any) -> None:
        serialized = json.dumps(value, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        if isinstance(value, dict):
            self.assertFalse(
                any(str(key).startswith("_") for key in value),
                f"Private provider field leaked into draft response: {value}",
            )
            for child in value.values():
                self._assert_public_path_free(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_public_path_free(child)
        elif isinstance(value, str):
            self.assertFalse(
                Path(value).is_absolute(),
                f"Absolute provider path leaked into draft response: {value}",
            )


if __name__ == "__main__":
    unittest.main()
