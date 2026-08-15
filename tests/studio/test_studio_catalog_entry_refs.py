"""Adversarial contracts for Studio's public catalog entry coordinates."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from http import HTTPStatus
from pathlib import Path
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
    _catalog_detail,
    _catalog_payload,
    _compatibility_payload,
    _encode_id,
    _handler_factory,
    _open_catalog_workspace,
    _remove_ui_workspace_reference,
    _resolve_catalog_identifier,
    _start_catalog_interface_launch,
)


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


@unittest.skipUnless(os.name == "posix", "local Realm projections are POSIX-only")
class StudioCatalogEntryRefTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:studio-catalog-entry-ref-test",
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
        self._counter = 0

    @staticmethod
    def _handler(state: UiState):
        handler = object.__new__(_handler_factory(state))
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        )
        return handler, responses

    def _operation(self, label: str) -> str:
        self._counter += 1
        return f"studio-catalog-entry-ref/{self._counter}/{label}"

    def _publish(
        self,
        *,
        package_id: str,
        publisher_id: str,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
    ) -> Any:
        suffix = f"{self._counter}-{uuid.uuid4().hex[:8]}"
        owner_id = f"studio-entry-ref-artifact-{suffix}"
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

    @staticmethod
    def _resource_manifest(*, name: str, marker: str) -> str:
        return yaml.safe_dump(
            {
                "apiVersion": "optpilot.io/v1",
                "config": "resource",
                "id": "viewer",
                "name": name,
                "interface": {
                    "label": f"Viewer {marker}",
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
            },
            sort_keys=False,
        )

    def _publish_resource_revision(
        self,
        *,
        package_id: str,
        publisher_id: str,
        name: str,
        marker: str,
    ) -> Any:
        return self._publish(
            package_id=package_id,
            publisher_id=publisher_id,
            files={
                "resources/viewer/optpilot.resource.yaml": self._resource_manifest(
                    name=name,
                    marker=marker,
                ),
                "resources/viewer/README.md": f"viewer bytes {marker}\n",
            },
            owned_paths=("resources/viewer",),
        )

    def _publish_compatible_pair(self) -> None:
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
        self._publish(
            package_id="public-contract-package",
            publisher_id="publisher/public-contract",
            files={
                "environments/toy/environment.yaml": yaml.safe_dump(
                    environment, sort_keys=False
                ),
                "environments/toy/evaluator.py": (
                    "def evaluate(candidate_runtime, context):\n"
                    "    return {'score': float(candidate_runtime['x'])}\n"
                ),
                "methods/fixed/method.yaml": yaml.safe_dump(method, sort_keys=False),
                "methods/fixed/method.py": "class Method:\n    pass\n",
            },
            owned_paths=("environments/toy", "methods/fixed"),
        )

    def test_public_list_detail_and_compatibility_are_provider_path_free(self) -> None:
        self._publish_compatible_pair()
        catalog = _catalog_payload(self.state)
        environment = catalog["environments"][0]
        method = catalog["methods"][0]
        payloads = (
            catalog,
            _catalog_detail(self.state, "environment", environment["uid"]),
            _catalog_detail(self.state, "method", method["uid"]),
            _compatibility_payload(self.state),
        )

        for payload in payloads:
            self._assert_public_path_free(payload)
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn(str(self.root), serialized)
            self.assertNotIn("projection_root", serialized)
            self.assertNotIn("_source_path", serialized)

    def test_catalog_reuses_one_workspace_listing_for_multiple_realm_entries(
        self,
    ) -> None:
        self._publish_compatible_pair()
        initial = _catalog_payload(self.state)
        environment = initial["environments"][0]
        workspace = _open_catalog_workspace(
            self.state,
            "environment",
            environment["uid"],
            editable=True,
            request_id="66666666-6666-4666-8666-666666666666",
        )

        with mock.patch.object(
            studio_server,
            "_list_ui_workspaces",
            wraps=studio_server._list_ui_workspaces,
        ) as list_workspaces:
            refreshed = _catalog_payload(self.state)

        self.assertEqual(list_workspaces.call_count, 1)
        linked_environment = refreshed["environments"][0]
        unlinked_method = refreshed["methods"][0]
        linked_action = linked_environment["actions"][
            "create_editable_workspace"
        ]
        unlinked_action = unlinked_method["actions"][
            "create_editable_workspace"
        ]
        self.assertEqual(linked_action["code"], "workspace_exists")
        self.assertEqual(linked_action["workspace_id"], workspace["id"])
        self.assertEqual(linked_action["workspace_title"], workspace["title"])
        self.assertEqual(unlinked_action["code"], "ready")
        self.assertNotIn("workspace_id", unlinked_action)

    def test_arbitrary_base64_host_path_is_not_a_catalog_coordinate(self) -> None:
        outside = self.root / "outside" / "optpilot.resource.yaml"
        outside.parent.mkdir()
        outside.write_text(
            self._resource_manifest(name="Outside", marker="outside"),
            encoding="utf-8",
        )
        encoded_host_path = _encode_id(outside)

        actions = (
            lambda: _resolve_catalog_identifier(
                self.state, "resource", encoded_host_path
            ),
            lambda: _catalog_detail(self.state, "resource", encoded_host_path),
            lambda: _open_catalog_workspace(
                self.state,
                "resource",
                encoded_host_path,
                editable=False,
            ),
            lambda: _start_catalog_interface_launch(
                self.state,
                "resource",
                encoded_host_path,
            ),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises((FileNotFoundError, ValueError)):
                    action()

    def test_old_ref_pins_open_edit_and_interface_preparation_to_revision_one(
        self,
    ) -> None:
        package_id = "revision-pinning-package"
        publisher_id = "publisher/viewer"
        first = self._publish_resource_revision(
            package_id=package_id,
            publisher_id=publisher_id,
            name="Revision One Viewer",
            marker="revision-one",
        )
        self.assertEqual(first.head.revision, 1)
        first_catalog = _catalog_payload(self.state)
        old_entry = next(
            item for item in first_catalog["resources"] if item["id"] == "viewer"
        )
        old_ref = old_entry["uid"]

        second = self._publish_resource_revision(
            package_id=package_id,
            publisher_id=publisher_id,
            name="Revision Two Viewer",
            marker="revision-two",
        )
        self.assertEqual(second.head.revision, 2)
        current = _catalog_payload(self.state)["resources"][0]
        self.assertNotEqual(current["uid"], old_ref)
        self.assertEqual(current["raw_config"]["name"], "Revision Two Viewer")

        old_detail = _catalog_detail(self.state, "resource", old_ref)
        self.assertEqual(old_detail["config"]["name"], "Revision One Viewer")
        self.assertIn("Viewer revision-one", old_detail["yaml"])

        read_only = _open_catalog_workspace(
            self.state,
            "resource",
            old_ref,
            editable=False,
        )
        self.addCleanup(
            _remove_ui_workspace_reference,
            self.state,
            str(read_only["id"]),
        )
        read_only_root = Path(str(read_only["root"]))
        self.assertEqual(
            (read_only_root / "README.md").read_text(encoding="utf-8"),
            "viewer bytes revision-one\n",
        )
        self.assertEqual(
            yaml.safe_load(
                (read_only_root / "optpilot.resource.yaml").read_text(
                    encoding="utf-8"
                )
            )["name"],
            "Revision One Viewer",
        )

        request_id = "11111111-1111-4111-8111-111111111111"
        editable = _open_catalog_workspace(
            self.state,
            "resource",
            old_ref,
            editable=True,
            request_id=request_id,
        )
        replayed = _open_catalog_workspace(
            self.state,
            "resource",
            old_ref,
            editable=True,
            request_id=request_id,
        )
        retried_after_refresh = _open_catalog_workspace(
            self.state,
            "resource",
            old_ref,
            editable=True,
            request_id="22222222-2222-4222-8222-222222222222",
        )
        self.assertEqual(replayed["id"], editable["id"])
        self.assertEqual(retried_after_refresh["id"], editable["id"])
        editable_root = Path(str(editable["root"]))
        editable_resource = editable_root / "resources" / "viewer"
        self.assertEqual(
            (editable_resource / "README.md").read_text(encoding="utf-8"),
            "viewer bytes revision-one\n",
        )
        self.assertEqual(
            yaml.safe_load(
                (editable_resource / "optpilot.resource.yaml").read_text(
                    encoding="utf-8"
                )
            )["name"],
            "Revision One Viewer",
        )
        (editable_root / "workspace-note.txt").write_text(
            "advanced workspace\n", encoding="utf-8"
        )
        advanced = self.runtime.editable_workspaces.commit_workspace(
            operation_id=self._operation("advance-kept-workspace"),
            workspace_id=str(editable["id"]),
            expected_workspace_revision=int(editable["realm_workspace_revision"]),
        )
        replayed_after_advance = _open_catalog_workspace(
            self.state,
            "resource",
            old_ref,
            editable=True,
            request_id=request_id,
        )
        self.assertEqual(replayed_after_advance["id"], editable["id"])
        self.assertEqual(
            replayed_after_advance["realm_workspace_revision"],
            advanced.current_revision,
        )
        self.assertTrue(
            (Path(replayed_after_advance["root"]) / "workspace-note.txt").is_file()
        )

        restarted = UiState(
            cwd=self.state.cwd,
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(restarted.close_catalog_projections)
        replayed_after_restart = _open_catalog_workspace(
            restarted,
            "resource",
            old_ref,
            editable=True,
            request_id="33333333-3333-4333-8333-333333333333",
        )
        self.assertEqual(replayed_after_restart["id"], editable["id"])
        self.assertEqual(
            replayed_after_restart["realm_workspace_revision"],
            advanced.current_revision,
        )
        self.assertTrue(
            (Path(replayed_after_restart["root"]) / "workspace-note.txt").is_file()
        )
        with mock.patch.object(
            studio_server.threading.Thread,
            "start",
            return_value=None,
        ):
            started = _start_catalog_interface_launch(
                self.state,
                "resource",
                old_ref,
            )
        launch_id = str(started["launch"]["launch_id"])
        job = self.state.interface_launches[launch_id]
        self.addCleanup(self._release_prepared_launch, launch_id)
        launch_root = Path(str(job.runtime_workspace["source_root"]))
        self.assertEqual(job.label, "Viewer revision-one")
        self.assertEqual(
            (launch_root / "README.md").read_text(encoding="utf-8"),
            "viewer bytes revision-one\n",
        )
        self.assertEqual(
            yaml.safe_load(
                (launch_root / "optpilot.resource.yaml").read_text(encoding="utf-8")
            )["name"],
            "Revision One Viewer",
        )

    def test_configured_filesystem_entry_exposes_unpublished_capability(self) -> None:
        configured = self.root / "configured-catalog"
        resource = configured / "resources" / "viewer"
        resource.mkdir(parents=True)
        (resource / "optpilot.resource.yaml").write_text(
            self._resource_manifest(name="Mutable Viewer", marker="mutable"),
            encoding="utf-8",
        )
        (resource / "README.md").write_text("mutable viewer\n", encoding="utf-8")
        self.state.catalog_roots = [configured]

        with mock.patch.object(
            studio_server,
            "_list_ui_workspaces",
            wraps=studio_server._list_ui_workspaces,
        ) as list_workspaces:
            entry = _catalog_payload(self.state)["resources"][0]
        self.assertEqual(list_workspaces.call_count, 0)
        capability = entry["actions"]["create_editable_workspace"]
        self.assertFalse(capability["eligible"])
        self.assertEqual(capability["code"], "catalog_source_unpublished")
        self.assertIn("register", capability["reason"])

        with self.assertRaises(
            studio_server.CatalogWorkspaceCreationUnsupported
        ) as raised:
            _open_catalog_workspace(
                self.state,
                "resource",
                entry["uid"],
                editable=True,
                request_id="33333333-3333-4333-8333-333333333333",
            )
        self.assertEqual(raised.exception.code, "catalog_source_unpublished")
        self.assertEqual(self.runtime.editable_workspaces.list_workspaces(), ())

    def test_catalog_http_actions_reject_browser_supplied_config(self) -> None:
        self._publish_resource_revision(
            package_id="exact-http-package",
            publisher_id="publisher/exact-http",
            name="Exact Viewer",
            marker="exact",
        )
        entry = _catalog_payload(self.state)["resources"][0]
        path = f"/api/catalog/resource/{entry['uid']}"
        attempted_config = dict(entry["raw_config"])
        attempted_config["name"] = "Browser mutation"

        requests = (
            ("open-workspace", {"config": attempted_config}),
            ("open-code", {"config": attempted_config}),
            (
                "edit-copy",
                {
                    "request_id": "55555555-5555-4555-8555-555555555555",
                    "config": attempted_config,
                },
            ),
            (
                "launch-interface-job",
                {"profile_id": "default", "config": attempted_config},
            ),
        )
        for action, request in requests:
            with self.subTest(action=action):
                handler, responses = self._handler(self.state)
                handler._read_json_body = lambda request=request: request  # type: ignore[method-assign]
                with self.assertRaisesRegex(ValueError, "fields differ|empty JSON"):
                    handler._handle_catalog_workspace_post(f"{path}/{action}")
                self.assertEqual(responses, [])

    def test_catalog_open_code_atomically_starts_exact_read_only_revision(self) -> None:
        package_id = "exact-code-package"
        publisher_id = "publisher/exact-code"
        self._publish_resource_revision(
            package_id=package_id,
            publisher_id=publisher_id,
            name="Revision One Viewer",
            marker="revision-one",
        )
        old_entry = _catalog_payload(self.state)["resources"][0]
        old_uid = str(old_entry["uid"])
        self._publish_resource_revision(
            package_id=package_id,
            publisher_id=publisher_id,
            name="Revision Two Viewer",
            marker="revision-two",
        )

        def start_code_server(workspace: dict[str, object], **_kwargs: object) -> dict[str, object]:
            return {
                "workspace_id": workspace["id"],
                "folder": workspace["root"],
                "open_url": "http://127.0.0.1:18766/",
            }

        handler, responses = self._handler(self.state)
        handler._read_json_body = lambda: {}  # type: ignore[method-assign]
        with mock.patch.object(
            self.state.workspace_runtime,
            "start_code_server",
            side_effect=start_code_server,
        ) as runtime_start, mock.patch.object(
            self.state,
            "_ensure_code_workspace",
            side_effect=AssertionError("exact Catalog Code must not re-resolve by path"),
        ):
            handler._handle_catalog_workspace_post(
                f"/api/catalog/resource/{old_uid}/open-code"
            )

        self.assertEqual(len(responses), 1)
        response, status = responses[0]
        self.assertEqual(status, HTTPStatus.CREATED)
        workspace = response["workspace"]
        code_server = response["code_server"]
        self.assertIsInstance(workspace, dict)
        self.assertIsInstance(code_server, dict)
        assert isinstance(workspace, dict)
        assert isinstance(code_server, dict)
        self.assertTrue(str(workspace["id"]).startswith("ws_catalog_"))
        self.assertEqual(workspace["mode"], "read-only")
        self.assertEqual(workspace["source_type"], "catalog")
        self.assertEqual(workspace["catalog_origin"]["revision"], 1)
        self.assertEqual(code_server["workspace_id"], workspace["id"])
        self.assertEqual(
            (Path(str(workspace["root"])) / "README.md").read_text(
                encoding="utf-8"
            ),
            "viewer bytes revision-one\n",
        )
        started_workspace = runtime_start.call_args.args[0]
        self.assertEqual(started_workspace["id"], workspace["id"])
        self.assertEqual(started_workspace["root"], workspace["root"])
        self.assertEqual(
            self.state.workspace_runtime._mount_mode(started_workspace), "ro"
        )

        alias_handler, alias_responses = self._handler(self.state)
        alias_handler._read_json_body = lambda: {}  # type: ignore[method-assign]
        with self.assertRaisesRegex(ValueError, "exact Catalog entry ref"):
            alias_handler._handle_catalog_workspace_post(
                "/api/catalog/resource/viewer/open-code"
            )
        self.assertEqual(alias_responses, [])

    def test_catalog_code_start_is_fenced_from_pruning_and_serialized(self) -> None:
        self._publish_resource_revision(
            package_id="slow-code-package",
            publisher_id="publisher/slow-code",
            name="Slow Viewer",
            marker="slow",
        )
        entry = _catalog_payload(self.state)["resources"][0]
        workspace = _open_catalog_workspace(
            self.state,
            "resource",
            str(entry["uid"]),
            editable=False,
        )
        workspace_id = str(workspace["id"])
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        runtime_calls: list[str] = []
        results: list[dict[str, object]] = []

        def slow_start(source: dict[str, object], **_kwargs: object) -> dict[str, object]:
            runtime_calls.append(str(source["id"]))
            if len(runtime_calls) == 1:
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=5))
            else:
                second_entered.set()
            return {
                "workspace_id": source["id"],
                "folder": source["root"],
                "open_url": "http://127.0.0.1:18766/",
            }

        def open_code() -> None:
            results.append(self.state.start_workspace_code_server(workspace))

        with mock.patch.object(
            self.state.workspace_runtime,
            "start_code_server",
            side_effect=slow_start,
        ), mock.patch.object(
            studio_server,
            "_workspace_created_within",
            return_value=False,
        ):
            first = threading.Thread(target=open_code)
            first.start()
            self.assertTrue(first_entered.wait(timeout=5))
            second = threading.Thread(target=open_code)
            second.start()
            self.assertFalse(
                second_entered.wait(timeout=0.1),
                "same-workspace Code starts must serialize",
            )

            listed = studio_server._list_ui_workspaces(
                self.state, include_support=True
            )
            self.assertIn(workspace_id, {item["id"] for item in listed})
            self.assertIn(
                workspace_id,
                self.state._catalog_workspace_projections,
                "the exact Catalog projection must remain leased during startup",
            )

            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(runtime_calls, [workspace_id, workspace_id])
        self.assertEqual(len(results), 2)

    def test_stale_catalog_support_id_opens_code_without_self_pruning(self) -> None:
        self._publish_resource_revision(
            package_id="stale-code-package",
            publisher_id="publisher/stale-code",
            name="Stale Viewer",
            marker="stale",
        )
        entry = _catalog_payload(self.state)["resources"][0]
        workspace = _open_catalog_workspace(
            self.state,
            "resource",
            str(entry["uid"]),
            editable=False,
        )
        workspace_id = str(workspace["id"])

        def start_code_server(source: dict[str, object], **_kwargs: object) -> dict[str, object]:
            return {
                "workspace_id": source["id"],
                "folder": source["root"],
                "open_url": "http://127.0.0.1:18766/",
            }

        handler, responses = self._handler(self.state)
        with mock.patch.object(
            studio_server,
            "_workspace_created_within",
            return_value=False,
        ), mock.patch.object(
            self.state.workspace_runtime,
            "start_code_server",
            side_effect=start_code_server,
        ) as runtime_start, mock.patch.object(
            self.state,
            "_ensure_code_workspace",
            side_effect=AssertionError("Workspace Code must retain its exact descriptor"),
        ):
            handler._handle_workspace_post(
                f"/api/workspaces/{workspace_id}/open-code"
            )

        self.assertEqual(len(responses), 1)
        response, status = responses[0]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(response["workspace_id"], workspace_id)
        self.assertEqual(runtime_start.call_args.args[0]["id"], workspace_id)
        index = json.loads(
            self.state.workspace_index_path.read_text(encoding="utf-8")
        )
        self.assertIn(workspace_id, {item["id"] for item in index["workspaces"]})

    def test_realm_catalog_edit_preserves_exact_source_and_replay_is_unchanged(
        self,
    ) -> None:
        self._publish_resource_revision(
            package_id="editable-resource-package",
            publisher_id="publisher/editable-resource",
            name="Original Viewer",
            marker="original",
        )
        entry = _catalog_payload(self.state)["resources"][0]
        request_id = "44444444-4444-4444-8444-444444444444"

        first = _open_catalog_workspace(
            self.state,
            "resource",
            entry["uid"],
            editable=True,
            request_id=request_id,
        )
        replayed = _open_catalog_workspace(
            self.state,
            "resource",
            entry["uid"],
            editable=True,
            request_id=request_id,
        )

        self.assertEqual(first["id"], replayed["id"])
        self.assertEqual(first["realm_workspace_revision"], 1)
        self.assertEqual(replayed["realm_workspace_revision"], 1)
        refreshed_entry = next(
            item
            for item in _catalog_payload(self.state)["resources"]
            if item["id"] == "viewer"
        )
        edit_action = refreshed_entry["actions"]["create_editable_workspace"]
        self.assertEqual(edit_action["code"], "workspace_exists")
        self.assertEqual(edit_action["workspace_id"], first["id"])
        self.runtime.editable_workspaces.delete_checkout(
            operation_id=self._operation("delete-edited-checkout"),
            workspace_id=str(first["id"]),
        )
        reopened = self.runtime.editable_workspaces.open_workspace(
            operation_id=self._operation("reopen-edited-checkout"),
            workspace_id=str(first["id"]),
            expected_workspace_revision=1,
        )
        saved = yaml.safe_load(
            (
                reopened.root_path
                / "resources"
                / "viewer"
                / "optpilot.resource.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(saved["name"], "Original Viewer")

    def test_bare_id_is_ambiguous_across_packages_but_exact_refs_are_independent(
        self,
    ) -> None:
        for package_id, label in (("package-alpha", "Alpha"), ("package-beta", "Beta")):
            self._publish_resource_revision(
                package_id=package_id,
                publisher_id=f"publisher/{label.lower()}",
                name=f"{label} Viewer",
                marker=label.lower(),
            )
        resources = sorted(
            _catalog_payload(self.state)["resources"],
            key=lambda item: item["package_id"],
        )
        self.assertEqual([item["id"] for item in resources], ["viewer", "viewer"])

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            _resolve_catalog_identifier(self.state, "resource", "viewer")

        opened = []
        try:
            for entry, expected in zip(resources, ("alpha", "beta"), strict=True):
                detail = _catalog_detail(self.state, "resource", entry["uid"])
                self.assertEqual(
                    detail["config"]["name"],
                    f"{expected.title()} Viewer",
                )
                workspace = _open_catalog_workspace(
                    self.state,
                    "resource",
                    entry["uid"],
                    editable=False,
                )
                opened.append(str(workspace["id"]))
                self.assertEqual(
                    (Path(str(workspace["root"])) / "README.md").read_text(
                        encoding="utf-8"
                    ),
                    f"viewer bytes {expected}\n",
                )
            self.assertNotEqual(opened[0], opened[1])
        finally:
            for workspace_id in opened:
                _remove_ui_workspace_reference(self.state, workspace_id)

    def _release_prepared_launch(self, launch_id: str) -> None:
        job = self.state.interface_launches.pop(launch_id, None)
        if job is not None and job.source_projection is not None:
            job.source_projection.close()
            job.source_projection = None

    def _assert_public_path_free(self, value: Any) -> None:
        if isinstance(value, dict):
            self.assertFalse(
                any(str(key).startswith("_") for key in value),
                f"Private provider field leaked into public payload: {value}",
            )
            for child in value.values():
                self._assert_public_path_free(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_public_path_free(child)
        elif isinstance(value, str):
            self.assertFalse(
                Path(value).is_absolute(),
                f"Absolute provider path leaked into public payload: {value}",
            )


if __name__ == "__main__":
    unittest.main()
