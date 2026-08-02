from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock, patch

from optpilot.realm.interface_outputs import INTERFACE_OUTPUT_SCHEMA
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot_studio.ui import server as studio_server
from optpilot_studio.ui.runtime_supervisor import StudioRuntimeSupervisorClaim
from optpilot_studio.ui.server import (
    UiState,
    _capture_interface_output_tree,
    _capture_interface_outputs_once,
    _catalog_detail,
    _catalog_payload,
    _create_ui_workspace,
    _delete_ui_workspace,
    _interface_launch_by_id,
    _interface_output_failure_reason,
    _interface_output_tree_choices,
    _keep_interface_output_as_workspace,
    _close_selection_content_view,
    _reopen_managed_workspace,
    _selection_content_byte_range,
    _start_catalog_interface_launch,
    _start_workspace_interface_launch,
    _stop_interface_launch,
    _view_interface_output,
)


class InterfaceOutputFailureCopyTest(unittest.TestCase):
    def test_content_rejection_is_explained_without_exposing_internal_code(self) -> None:
        reason = _interface_output_failure_reason("content_rejected")

        self.assertIn("unsupported filesystem content or metadata", reason)
        self.assertNotIn("content_rejected", reason)

    def test_unknown_failure_uses_safe_actionable_fallback(self) -> None:
        reason = _interface_output_failure_reason(
            "/private/path/from-an-untrusted-exception"
        )

        self.assertIn("generation is complete", reason)
        self.assertNotIn("/private/path", reason)


def _output_record(
    *,
    output_id: str = "generated-simulator",
    label: str = "Generated simulator",
    kind: str = "tree",
    path: str = "generation",
) -> dict[str, str]:
    return {
        "schema_version": INTERFACE_OUTPUT_SCHEMA,
        "id": output_id,
        "label": label,
        "kind": kind,
        "root": "output",
        "path": path,
    }


class _FakeWorkspaceRuntime:
    """Small execution-lifecycle fake; Realm remains entirely real."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.records: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self.executed_workspace_ids: list[str] = []
        self.executed_cwds: list[Path] = []
        self.executed_commands: list[list[str]] = []
        self.stopped_workspace_ids: list[str] = []
        self.deleted_workspace_ids: list[str] = []
        self.confirm_stop = True
        self.on_exec: Callable[[dict[str, Any]], None] | None = None
        self.on_stop: Callable[[dict[str, Any]], None] | None = None
        self.on_delete: Callable[[str], None] | None = None
        self.health_result: dict[str, Any] = {
            "ok": True,
            "available": True,
            "engine": "test",
        }

    def health(self) -> dict[str, Any]:
        return dict(self.health_result)

    def _workspace_runtime_dir(self, workspace_id: str) -> Path:
        return self.runtime_root / workspace_id

    def _ensure_workspace_runtime_dir(self, workspace_id: str) -> Path:
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir

    def _record_path(self, workspace_id: str) -> Path:
        return self._workspace_runtime_dir(workspace_id) / "runtime.json"

    def _write_record(self, workspace_id: str, record: dict[str, Any]) -> None:
        self.records[workspace_id] = dict(record)
        record_path = self._record_path(workspace_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def _read_record(self, workspace_id: str) -> dict[str, Any]:
        return dict(self.records.get(workspace_id, {}))

    def status(self, workspace: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(workspace.get("id") or "")
        record = self._read_record(workspace_id)
        return {
            "status": str(record.get("status") or "stopped"),
            "containerized": True,
            "executor": "test",
        }

    def exec_detached(
        self,
        workspace: dict[str, Any],
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        name: str = "process",
        timeout: int = 15,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        del env, timeout
        if should_stop is not None and should_stop():
            raise RuntimeError("Interface launch was stopped.")
        workspace_id = str(workspace["id"])
        self.events.append("exec")
        self.executed_workspace_ids.append(workspace_id)
        self.executed_cwds.append(Path(cwd).resolve())
        self.executed_commands.append(list(command))
        if self.on_exec is not None:
            self.on_exec(workspace)
        if should_stop is not None and should_stop():
            raise RuntimeError("Interface launch was stopped.")
        self._write_record(
            workspace_id,
            {
                "status": "running",
                "launched_processes": [{"name": name, "command": list(command)}],
            },
        )
        return {
            "name": name,
            "started_at": "2026-07-14T00:00:00Z",
            "returncode": 0,
            "runtime": {
                "status": "running",
                "containerized": True,
                "executor": "test",
            },
        }

    def stop(self, workspace: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(workspace["id"])
        self.events.append("stop")
        self.stopped_workspace_ids.append(workspace_id)
        if self.on_stop is not None:
            self.on_stop(workspace)
        if not self.confirm_stop:
            raise RuntimeError("runtime stop is unconfirmed")
        self._write_record(
            workspace_id,
            {
                "status": "stopped",
                "terminal_proof": {
                    "terminal_confirmed": True,
                    "state": "absent",
                },
            },
        )
        return {"status": "stopped"}

    def delete(self, workspace_id: str) -> bool:
        self.events.append("delete")
        self.deleted_workspace_ids.append(workspace_id)
        if self.on_delete is not None:
            self.on_delete(workspace_id)
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        existed = runtime_dir.exists()
        if existed:
            shutil.rmtree(runtime_dir)
        self.records.pop(workspace_id, None)
        return existed and not runtime_dir.exists()


class InterfaceOutputControlFileRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="interface-output-test",
        )
        self.service = self.runtime.interface_outputs
        self.handle = self.service.create_session(
            operation_id="test/interface-output/create",
            launch_id="control-file-launch",
        )
        self.output_root = self.root / "output"
        self.output_root.mkdir()
        self.control_file = self.root / "outputs.jsonl"

    def tearDown(self) -> None:
        try:
            if self.handle is not None:
                self.service.retire_session(
                    operation_id="test/interface-output/retire",
                    handle=self.handle,
                )
        except Exception:
            pass
        self.runtime.close()
        self.temporary.cleanup()

    def _write_generation(self, path: str = "generation") -> None:
        generation = self.output_root / path
        generation.mkdir()
        (generation / "simulator.py").write_text("MODEL = 'ready'\n", encoding="utf-8")

    def test_malformed_complete_record_does_not_starve_later_generation(self) -> None:
        self._write_generation()
        malformed = '{"private_path":"' + str(self.root / "do-not-leak") + '"'
        self.control_file.write_text(
            malformed + "\n" + json.dumps(_output_record()) + "\n",
            encoding="utf-8",
        )
        rejected: list[Any] = []

        captured = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control_file,
            root_handles={"output": self.output_root},
            rejected_records=rejected,
        )

        self.assertEqual([item.output_id for item in captured], ["generated-simulator"])
        diagnostics = [item.to_dict() for item in rejected]
        self.assertEqual(diagnostics, [{"line": 1, "code": "invalid_json"}])
        serialized = json.dumps(diagnostics, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("do-not-leak", serialized)

    def test_failed_generation_requires_an_explicit_retry(self) -> None:
        self.control_file.write_text(
            json.dumps(_output_record()) + "\n", encoding="utf-8"
        )

        first = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control_file,
            root_handles={"output": self.output_root},
        )
        failed = self.service.list_statuses(handle=self.handle)[0]
        self.assertEqual(first, ())
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.attempt_number, 1)

        self._write_generation()
        implicit = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control_file,
            root_handles={"output": self.output_root},
        )
        still_failed = self.service.list_statuses(handle=self.handle)[0]
        self.assertEqual(implicit, ())
        self.assertEqual(still_failed.state.value, "failed")
        self.assertEqual(still_failed.attempt_number, 1)

        explicit = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control_file,
            root_handles={"output": self.output_root},
            retry_failed=True,
        )
        ready = self.service.list_statuses(handle=self.handle)[0]
        self.assertEqual([item.output_id for item in explicit], ["generated-simulator"])
        self.assertEqual(ready.state.value, "ready")
        self.assertEqual(ready.attempt_number, 2)


class StudioInterfaceOutputLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.catalog_root = self.root / "catalog" / "local_package"
        resource = self.catalog_root / "resources" / "generated_tool"
        resource.mkdir(parents=True)
        (resource / "README.md").write_text("# Generated tool\n", encoding="utf-8")
        (resource / "optpilot.resource.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: generated-tool",
                    "name: Generated Tool",
                    "interface:",
                    "  label: Generated Tool UI",
                    "  outputs:",
                    "    actions:",
                    "      - id: run",
                    "        label: Run simulation",
                    "        command: [python, simulator.py]",
                    "        timeoutSeconds: 30",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        viewer = self.catalog_root / "resources" / "viewer_tool"
        viewer.mkdir(parents=True)
        (viewer / "README.md").write_text("# Viewer tool\n", encoding="utf-8")
        (viewer / "optpilot.resource.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: viewer-tool",
                    "name: Viewer Tool",
                    "interface:",
                    "  label: Viewer UI",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        environment = self.catalog_root / "environments" / "generated_environment"
        environment.mkdir(parents=True)
        (environment / "environment.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: environment",
                    "id: generated-environment",
                    "description: Environment with a launchable inspection UI",
                    "interface:",
                    "  label: Generated Environment UI",
                    "  outputs: true",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "evaluator:",
                    "  python: tests.fixtures.catalog.toy_factory_env:evaluate",
                    "candidate:",
                    "  format: parameters",
                    "  parameters:",
                    "    schema:",
                    "      x: {valueType: float, min: 0.0, max: 8.0}",
                    "      y: {valueType: int, min: 1, max: 10}",
                    "metrics:",
                    "  source: return",
                    "  keys: [throughput, cycle_time]",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        method = self.catalog_root / "methods" / "generated_method"
        method.mkdir(parents=True)
        (method / "method.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: method",
                    "id: generated-method",
                    "description: Method with a launchable inspection UI",
                    "interface:",
                    "  label: Generated Method UI",
                    "  outputs: true",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "entrypoint:",
                    "  python: tests.fixtures.catalog.user_methods.fixed_parameter_method:FixedParameterMethod",
                    "  protocol: batch",
                    "accepts:",
                    "  formats: [parameters]",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.realm = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="studio-interface-output-test",
        )
        self.runtime_supervisor_claim = StudioRuntimeSupervisorClaim.acquire(
            self.root / "studio"
        )
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[self.catalog_root],
            run_roots=[],
            realm_runtime=self.realm,
            runtime_supervisor_claim=self.runtime_supervisor_claim,
        )
        self.fake_runtime = _FakeWorkspaceRuntime(self.state.runtime_dir)
        self.state.workspace_runtime = self.fake_runtime  # type: ignore[assignment]
        self.state.transient_workspace_preview_open = (  # type: ignore[method-assign]
            lambda workspace, port, **_kwargs: {
                "workspace_id": str(workspace["id"]),
                "port": int(port),
                "preview_url": "http://127.0.0.1/preview",
                "proxy": "studio",
                "proxy_target": "http://127.0.0.1:5173",
                "allowed_ports": [int(port)],
            }
        )
        self.state._stop_workspace_preview_proxy = (  # type: ignore[method-assign]
            lambda _key: (
                self.fake_runtime.events.append("close-preview") or True
            )
        )
        catalog = _catalog_payload(self.state)
        self.interface_uids = {
            kind: next(
                item["uid"]
                for item in catalog[f"{kind}s"]
                if item["id"] == f"generated-{kind if kind != 'resource' else 'tool'}"
            )
            for kind in ("environment", "method", "resource")
        }
        self.uid = self.interface_uids["resource"]
        self.viewer_uid = next(
            item["uid"]
            for item in catalog["resources"]
            if item["id"] == "viewer-tool"
        )

    def tearDown(self) -> None:
        for launch_id, job in tuple(self.state.interface_launches.items()):
            if job.output_session is None:
                continue
            try:
                self.fake_runtime.confirm_stop = True
                _stop_interface_launch(self.state, launch_id)
            except Exception:
                pass
        self.realm.close()
        self.runtime_supervisor_claim.close()
        self.temporary.cleanup()

    def test_interface_runtime_and_output_storage_are_outside_the_checkout(
        self,
    ) -> None:
        self.state.runtime_dir.relative_to(self.realm.root)
        with self.assertRaises(ValueError):
            self.state.runtime_dir.relative_to(self.state.cwd)

    def _start_launch(self, kind: str = "resource") -> tuple[str, dict[str, Any]]:
        session_visible_at_exec: list[bool] = []
        handles_visible_at_exec: list[bool] = []

        def inspect_exec(workspace: dict[str, Any]) -> None:
            launch_id = str(workspace["launch_id"])
            with self.state._lock:
                job = self.state.interface_launches[launch_id]
                session_visible_at_exec.append(job.output_session is not None)
                handles_visible_at_exec.append(
                    bool(
                        job.runtime_handles.get("OPTPILOT_INTERFACE_OUTPUT_ROOT")
                        and job.runtime_handles.get(
                            "OPTPILOT_INTERFACE_OUTPUTS_FILE"
                        )
                    )
                )

        self.fake_runtime.on_exec = inspect_exec
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_wait_for_preview_ready",
                return_value={"ready": True, "skipped": False},
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state,
                kind,
                self.interface_uids[kind],
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {"ready", "failed"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("catalog interface launch did not finish")

        self.assertEqual(current["status"], "ready", current)
        self.assertEqual(session_visible_at_exec, [True])
        self.assertEqual(handles_visible_at_exec, [True])
        heartbeat_deadline = time.monotonic() + 5
        while time.monotonic() < heartbeat_deadline:
            with self.state._lock:
                handle = self.state.interface_launches[launch_id].output_session
                heartbeat_revision = (
                    handle.lease.heartbeat_revision if handle is not None else -1
                )
            if heartbeat_revision >= 1:
                break
            time.sleep(0.01)
        else:
            self.fail("catalog interface output watcher did not begin heartbeating")
        return launch_id, current

    def _create_editable_workspace(self) -> tuple[dict[str, Any], Path]:
        workspace_id = "editable-interface-source"
        workspace_root = self.state.workspaces_dir / workspace_id / "workspace"
        workspace_root.mkdir(parents=True)
        config_path = workspace_root / "optpilot.resource.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: editable-interface-source",
                    "name: Editable Interface Source",
                    "interface:",
                    "  label: Editable Source UI",
                    "  outputs: true",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        workspace = _create_ui_workspace(
            self.state,
            {
                "id": workspace_id,
                "title": "Editable Interface Source",
                "root": str(workspace_root),
                "source_root": str(workspace_root),
                "source_type": "blank",
                "mode": "editable",
                "initialize_if_empty": False,
                "registered_entries": [
                    {
                        "kind": "resource",
                        "id": "editable-interface-source",
                        "config_path": "optpilot.resource.yaml",
                    }
                ],
            },
        )
        source_runtime_dir = self.fake_runtime._ensure_workspace_runtime_dir(
            workspace_id
        )
        sentinel = source_runtime_dir / "code-server" / "authoring.sentinel"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("authoring-runtime-stays-alive\n", encoding="utf-8")
        self.fake_runtime._write_record(
            workspace_id,
            {
                "status": "running",
                "workspace_root": str(workspace_root),
                "container_name": "source-authoring-container",
                "code_server_started_at": 1,
            },
        )
        return workspace, sentinel

    def _create_nested_editable_workspace(
        self,
    ) -> tuple[dict[str, Any], Path]:
        workspace_id = "editable-nested-interface-source"
        workspace_root = self.state.workspaces_dir / workspace_id / "workspace"
        component_root = (
            workspace_root / "resources" / "nested-interface"
        )
        component_root.mkdir(parents=True)
        (component_root / "_launch.sh").write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
        )
        (component_root / "optpilot.resource.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: editable-nested-interface-source",
                    "name: Editable Nested Interface Source",
                    "interface:",
                    "  label: Editable Nested UI",
                    "  command: [./_launch.sh]",
                    "  runtime: {sandbox: process}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        workspace = _create_ui_workspace(
            self.state,
            {
                "id": workspace_id,
                "title": "Editable Nested Interface Source",
                "root": str(workspace_root),
                "source_root": str(workspace_root),
                "source_type": "blank",
                "mode": "editable",
                "initialize_if_empty": False,
                "registered_entries": [
                    {
                        "kind": "resource",
                        "id": "editable-nested-interface-source",
                        "config_path": (
                            "resources/nested-interface/"
                            "optpilot.resource.yaml"
                        ),
                    }
                ],
            },
        )
        return workspace, component_root

    def _start_editable_workspace_launch(
        self, workspace_id: str
    ) -> tuple[str, dict[str, Any]]:
        with patch.object(
            studio_server,
            "_wait_for_preview_ready",
            return_value={"ready": True, "skipped": False},
        ):
            created = _start_workspace_interface_launch(self.state, workspace_id)
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {
                    "ready",
                    "failed",
                    "cleanup_pending",
                    "stopped",
                }:
                    break
                time.sleep(0.01)
            else:
                self.fail("editable workspace interface launch did not finish")
        self.assertEqual(current["status"], "ready", current)
        return launch_id, current

    def _publish_output(
        self,
        launch_id: str,
        *,
        output_id: str = "generated-simulator",
        path: str = "generation",
        content: str = "MODEL = 'generated'\n",
    ) -> Path:
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            output_root = Path(job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"])
            control_file = Path(job.runtime_handles["OPTPILOT_INTERFACE_OUTPUTS_FILE"])
        generation = output_root / path
        generation.mkdir()
        (generation / "simulator.py").write_text(content, encoding="utf-8")
        with control_file.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(_output_record(output_id=output_id, path=path)) + "\n"
            )
        return generation

    def _expire_output_session_lease(self, handle: Any) -> None:
        with sqlite3.connect(self.realm.ledger.database_path) as connection:
            cursor = connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (handle.lease.lease_id,),
            )
        self.assertEqual(cursor.rowcount, 1)

    def test_view_only_interface_has_no_output_runtime_or_actions(self) -> None:
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_wait_for_preview_ready",
                return_value={"ready": True, "skipped": False},
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.viewer_uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {"ready", "failed"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("view-only interface launch did not finish")

        self.assertEqual(current["status"], "ready", current)
        self.assertFalse(current["outputs_enabled"])
        self.assertFalse(
            current["actions"]["capture_output_tree"]["supported"]
        )
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertIsNone(job.output_session)
            self.assertNotIn("OPTPILOT_INTERFACE_OUTPUT_ROOT", job.runtime_handles)
            self.assertNotIn("OPTPILOT_INTERFACE_OUTPUTS_FILE", job.runtime_handles)
        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")

    def test_shared_output_contract_covers_every_catalog_component_kind(
        self,
    ) -> None:
        roots: set[str] = set()
        for kind in ("environment", "method", "resource"):
            with self.subTest(kind=kind):
                launch_id, current = self._start_launch(kind)
                self.assertEqual(current["launch_scope"], "catalog-transient")
                self.assertEqual(current["result"]["source"]["kind"], kind)
                self.assertEqual(
                    current["result"]["runtime"]["output_protocol"],
                    "optpilot.interface.output.v1",
                )
                with self.state._lock:
                    job = self.state.interface_launches[launch_id]
                    roots.add(
                        job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"]
                    )
                output_id = f"{kind}-output"
                self._publish_output(
                    launch_id,
                    output_id=output_id,
                    path=f"{kind}-result",
                )
                outputs = _capture_interface_outputs_once(self.state, launch_id)
                output = next(item for item in outputs if item["id"] == output_id)
                self.assertEqual(output["status"], "ready")
                self.assertTrue(output["actions"]["view_read_only"]["eligible"])
                self.assertTrue(output["actions"]["keep_as_workspace"]["eligible"])
                self.assertEqual(
                    _stop_interface_launch(self.state, launch_id)["status"],
                    "stopped",
                )
        self.assertEqual(len(roots), 3)

    def test_authored_output_action_gets_a_private_broker_not_browser_authority(
        self,
    ) -> None:
        launch_id, current = self._start_launch()

        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            action_root = Path(
                job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT"]
            )
            paths = studio_server._interface_output_action_paths(job)

        self.assertEqual(paths["root"], action_root)
        self.assertTrue(paths["requests"].is_file())
        for name in ("inputs", "responses", "results", "cancellations"):
            self.assertTrue(paths[name].is_dir())
        serialized = json.dumps(current, sort_keys=True)
        self.assertNotIn(str(action_root), serialized)
        self.assertNotIn("simulator.py", serialized)
        self.assertNotIn("OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT", serialized)

        self._publish_output(launch_id)
        output = _capture_interface_outputs_once(self.state, launch_id)[0]
        execute = output["actions"]["execute"]
        self.assertTrue(execute["supported"])
        self.assertFalse(execute["eligible"])
        self.assertEqual(
            execute["items"],
            [
                {
                    "id": "run",
                    "label": "Run simulation",
                    "accepts_arguments": False,
                    "eligible": False,
                    "code": "prepared_runtime_unavailable",
                    "reason": (
                        "This interface did not prepare an isolated runtime that "
                        "can be reused to run its output."
                    ),
                }
            ],
        )

    def test_output_action_http_boundary_selects_only_an_authored_action(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        self._publish_output(launch_id)
        output = _capture_interface_outputs_once(self.state, launch_id)[0]
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.prepared_runtime_lease = Mock()
        captured: list[Any] = []

        def schedule(
            _state: UiState,
            _job: Any,
            request: Any,
            **kwargs: Any,
        ) -> bool:
            captured.append((request, kwargs))
            return True

        try:
            with patch.object(
                studio_server,
                "_schedule_interface_output_action",
                side_effect=schedule,
            ):
                response = studio_server._run_ready_interface_output_action(
                    self.state,
                    launch_id,
                    output["id"],
                    "run",
                    {
                        "schema_version": (
                            "optpilot.interface-output-action-run-request.v1"
                        ),
                        "request_id": "request-from-browser",
                        "arguments": [],
                    },
                )
        finally:
            with self.state._lock:
                job.prepared_runtime_lease = None

        self.assertEqual(response["output"]["id"], output["id"])
        request, kwargs = captured[0]
        self.assertEqual(request.request_id, "request-from-browser")
        self.assertEqual(request.action_id, "run")
        self.assertEqual(request.output_path, ".")
        self.assertEqual(request.arguments, ())
        self.assertIsNone(request.timeout_seconds)
        self.assertEqual(kwargs["output_id"], output["id"])
        self.assertNotIn("command", request.to_dict())
        self.assertNotIn("image", request.to_dict())
        self.assertNotIn("env", request.to_dict())

        with self.assertRaisesRegex(ValueError, "fields differ"):
            studio_server._run_ready_interface_output_action(
                self.state,
                launch_id,
                output["id"],
                "run",
                {
                    "schema_version": (
                        "optpilot.interface-output-action-run-request.v1"
                    ),
                    "request_id": "untrusted-command",
                    "arguments": [],
                    "command": ["sh", "-c", "id"],
                },
            )

    def test_broker_only_action_is_not_presented_on_output_card(self) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            profile = job.interface_profile
            assert profile is not None
            hidden = replace(
                profile.output_actions[0],
                show_in_output_card=False,
            )
            job.interface_profile = replace(
                profile,
                output_actions=(hidden,),
            )
            self.assertIn(
                "OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT",
                job.runtime_handles,
            )

        self._publish_output(launch_id)
        output = _capture_interface_outputs_once(self.state, launch_id)[0]
        execute = output["actions"]["execute"]
        self.assertFalse(execute["supported"])
        self.assertEqual(execute["code"], "output_card_actions_not_declared")
        self.assertEqual(execute["items"], [])
        self.assertEqual(
            studio_server._interface_output_action_for_job(job, "run"),
            hidden,
        )

        with self.assertRaisesRegex(
            studio_server.RealmConflict,
            "does not present an action",
        ):
            studio_server._run_ready_interface_output_action(
                self.state,
                launch_id,
                output["id"],
                "run",
                {
                    "schema_version": (
                        "optpilot.interface-output-action-run-request.v1"
                    ),
                    "request_id": "hidden-browser-action",
                    "arguments": [],
                },
            )

    def test_output_action_post_route_returns_accepted(self) -> None:
        handler = object.__new__(studio_server._handler_factory(self.state))
        handler.path = (
            "/api/interface-launches/launch-1/outputs/output-1/"
            "actions/run-simulation/run"
        )
        payload = {
            "schema_version": "optpilot.interface-output-action-run-request.v1",
            "request_id": "route-request",
            "arguments": [],
        }
        handler._read_json_body = Mock(return_value=payload)
        responses: list[tuple[Any, Any]] = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda body, status=studio_server.HTTPStatus.OK: responses.append(
                (body, status)
            )
        )

        with patch.object(
            studio_server,
            "_run_ready_interface_output_action",
            return_value={"execution": {"status": "queued"}},
        ) as run:
            handler.do_POST()

        run.assert_called_once_with(
            self.state,
            "launch-1",
            "output-1",
            "run-simulation",
            payload,
        )
        self.assertEqual(responses[0][1], studio_server.HTTPStatus.ACCEPTED)

    def test_output_action_request_cannot_extend_the_authored_timeout(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
        action = studio_server._interface_output_action_for_job(job, "run")
        request = studio_server.InterfaceOutputExecutionRequest(
            request_id="timeout-too-long",
            action_id="run",
            output_path=".",
            timeout_seconds=action.timeout_seconds + 1,
        )

        with self.assertRaisesRegex(
            studio_server.InterfaceOutputExecutionRejected,
            "authored action maximum",
        ):
            studio_server._schedule_interface_output_action(
                self.state,
                job,
                request,
                output_id="",
                source_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("rejected timeout must not snapshot")
                ),
            )
        with self.state._lock:
            self.assertNotIn(request.request_id, job.output_action_threads)
            self.assertNotIn(
                request.request_id, job.output_action_request_digests
            )

    def test_broker_child_replacement_is_rejected_by_initial_identity(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            paths = studio_server._interface_output_action_paths(job)

        outside = self.root / "outside-broker"
        outside.mkdir()
        original_responses = paths["root"] / "original-responses"
        paths["responses"].rename(original_responses)
        paths["responses"].symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(
            studio_server.RealmConflict, "identity changed"
        ):
            studio_server._interface_output_action_paths(job)
        paths["responses"].unlink()
        original_responses.rename(paths["responses"])

        original_inputs = paths["root"] / "original-inputs"
        paths["inputs"].rename(original_inputs)
        paths["inputs"].symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(
            studio_server.RealmConflict, "identity changed"
        ):
            studio_server._interface_output_action_paths(job)
        paths["inputs"].unlink()
        original_inputs.rename(paths["inputs"])

        original_requests = paths["root"] / "original-requests.jsonl"
        paths["requests"].rename(original_requests)
        paths["requests"].write_text("", encoding="utf-8")
        with self.assertRaisesRegex(
            studio_server.RealmConflict, "identity changed"
        ):
            studio_server._read_interface_output_action_requests(job)
        paths["requests"].unlink()
        original_requests.rename(paths["requests"])

    def test_broker_requests_snapshot_only_the_dedicated_input_namespace(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            watcher = job.output_watcher
        assert watcher is not None
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive())
        paths = studio_server._interface_output_action_paths(job)
        staged = paths["inputs"] / "staged-request"
        staged.mkdir()
        (staged / "run.py").write_text("print('staged')\n", encoding="utf-8")
        request = {
            "schema_version": (
                "optpilot.interface-output-execution-request.v1"
            ),
            "request_id": "staged-request",
            "action_id": "run",
            "output_path": "staged-request",
            "arguments": [],
            "timeout_seconds": 5,
        }
        paths["requests"].write_text(
            json.dumps(request) + "\n",
            encoding="utf-8",
        )
        captured: list[Any] = []

        def schedule(
            _state: UiState,
            _job: Any,
            selected_request: Any,
            **kwargs: Any,
        ) -> bool:
            captured.append((selected_request, kwargs))
            return True

        with patch.object(
            studio_server,
            "_schedule_interface_output_action",
            side_effect=schedule,
        ):
            studio_server._process_interface_output_action_requests(
                self.state, launch_id
            )

        self.assertEqual(len(captured), 1)
        source, release = captured[0][1]["source_factory"]()
        try:
            self.assertEqual(
                (source.root_path / "run.py").read_text(encoding="utf-8"),
                "print('staged')\n",
            )
        finally:
            release()
            source.cleanup()
        output_root = Path(
            job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"]
        )
        self.assertEqual(list(output_root.iterdir()), [])

    def test_response_publication_stays_on_bound_directory_during_swap(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            paths = studio_server._interface_output_action_paths(job)
        request = studio_server.InterfaceOutputExecutionRequest(
            request_id="bound-response",
            action_id="run",
            output_path=".",
        )
        result = studio_server._interface_output_action_rejected_result(
            request,
            failure_code="test_rejected",
            detail="bounded test",
        )
        outside = self.root / "outside-response"
        outside.mkdir()
        displaced = paths["root"] / "displaced-responses"
        original_write = studio_server.write_execution_result_at

        def swap_then_write(*args: Any, **kwargs: Any) -> None:
            paths["responses"].rename(displaced)
            paths["responses"].symlink_to(outside, target_is_directory=True)
            original_write(*args, **kwargs)

        try:
            with patch.object(
                studio_server,
                "write_execution_result_at",
                side_effect=swap_then_write,
            ):
                studio_server._publish_interface_output_action_result(job, result)
            self.assertFalse((outside / "bound-response.json").exists())
            self.assertTrue((displaced / "bound-response.json").is_file())
        finally:
            if paths["responses"].is_symlink():
                paths["responses"].unlink()
            if displaced.exists():
                displaced.rename(paths["responses"])

    def test_browser_result_file_is_bounded_authorized_and_revalidated(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        request_id = "browser-result"
        relative_path = "reports/summary.txt"
        content = b"completed simulation\n"
        digest = studio_server.hashlib.sha256(content).hexdigest()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            paths = studio_server._interface_output_action_paths(job)
            job.output_action_executions[request_id] = {
                "request_id": request_id,
                "action_id": "run",
                "label": "Run simulation",
                "output_id": "generated-simulator",
                "status": "succeeded",
                "started_at": time.time(),
                "updated_at": time.time(),
                "result": {
                    "schema_version": (
                        "optpilot.interface-output-execution-result.v1"
                    ),
                    "request_id": request_id,
                    "action_id": "run",
                    "snapshot_ref": None,
                    "status": "succeeded",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "result_files": [
                        {
                            "path": relative_path,
                            "size": len(content),
                            "sha256": digest,
                        }
                    ],
                    "failure_code": None,
                },
            }
            record = dict(job.output_action_executions[request_id])
        result_root = paths["results"] / request_id / "reports"
        result_root.mkdir(parents=True)
        result_file = result_root / "summary.txt"
        result_file.write_bytes(content)

        public = studio_server._public_interface_output_execution(
            record, job=job
        )
        public_result = public["result"]
        assert public_result is not None
        self.assertEqual(public_result["result_file_count"], 1)
        access = public_result["result_files"][0]["access"]
        self.assertTrue(access["eligible"])
        self.assertTrue(access["preview_eligible"])
        self.assertIn("/output-action-executions/", access["open_url"])
        self.assertNotIn(str(paths["results"]), json.dumps(public))

        data, media_type, name = (
            studio_server._interface_output_action_result_file_bytes(
                self.state,
                launch_id=launch_id,
                request_id=request_id,
                relative_path=relative_path,
            )
        )
        self.assertEqual(data, content)
        self.assertEqual(media_type, "text/plain")
        self.assertEqual(name, "summary.txt")

        result_file.write_bytes(b"changed simulation!\n")
        with self.assertRaisesRegex(
            studio_server.RealmConflict, "evidence"
        ):
            studio_server._interface_output_action_result_file_bytes(
                self.state,
                launch_id=launch_id,
                request_id=request_id,
                relative_path=relative_path,
            )

    def test_browser_result_file_get_route_is_exact(self) -> None:
        handler = object.__new__(studio_server._handler_factory(self.state))
        handler.path = (
            "/api/interface-launches/launch-1/"
            "output-action-executions/request-1/files/content"
            "?path=reports%2Fsummary.txt&download=1"
        )
        handler._send_interface_output_action_result_file = Mock()

        handler.do_GET()

        handler._send_interface_output_action_result_file.assert_called_once_with(
            launch_id="launch-1",
            request_id="request-1",
            relative_path="reports/summary.txt",
            download=True,
        )

    def test_terminal_broker_rejection_cannot_execute_after_capacity_frees(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            watcher = job.output_watcher
        assert watcher is not None
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive())
        paths = studio_server._interface_output_action_paths(job)
        request = {
            "schema_version": (
                "optpilot.interface-output-execution-request.v1"
            ),
            "request_id": "capacity-rejected",
            "action_id": "run",
            "output_path": ".",
            "arguments": [],
            "timeout_seconds": None,
        }
        with paths["requests"].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(request) + "\n")
        with self.state._lock:
            job.output_action_threads.update(
                {"busy-one": Mock(), "busy-two": Mock()}
            )

        studio_server._process_interface_output_action_requests(
            self.state, launch_id
        )
        response = json.loads(
            (paths["responses"] / "capacity-rejected.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(response["status"], "rejected")
        with self.state._lock:
            self.assertIn(
                "capacity-rejected", job.output_action_request_digests
            )
            job.output_action_threads.clear()

        studio_server._process_interface_output_action_requests(
            self.state, launch_id
        )
        with self.state._lock:
            self.assertNotIn(
                "capacity-rejected", job.output_action_threads
            )
            self.assertEqual(
                job.output_action_executions["capacity-rejected"]["status"],
                "rejected",
            )

    def test_broker_file_capacity_supports_every_bounded_request(self) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            watcher = job.output_watcher
        assert watcher is not None
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive())
        paths = studio_server._interface_output_action_paths(job)
        arguments = ["x" * 4000 for _ in range(8)]
        records = [
            {
                "schema_version": (
                    "optpilot.interface-output-execution-request.v1"
                ),
                "request_id": f"large-request-{index}",
                "action_id": "run",
                "output_path": ".",
                "arguments": arguments,
                "timeout_seconds": None,
            }
            for index in range(40)
        ]
        payload = "".join(
            json.dumps(record, separators=(",", ":")) + "\n"
            for record in records
        )
        self.assertGreater(len(payload.encode("utf-8")), 1024 * 1024)
        self.assertTrue(
            all(
                len(line.encode("utf-8"))
                <= studio_server._MAX_INTERFACE_OUTPUT_ACTION_REQUEST_LINE_BYTES
                for line in payload.splitlines(keepends=True)
            )
        )
        paths["requests"].write_text(payload, encoding="utf-8")

        lines = studio_server._read_interface_output_action_requests(job)

        self.assertEqual(len(lines), len(records))
        for line in lines:
            studio_server.InterfaceOutputExecutionRequest.from_dict(
                json.loads(line)
            )

    def test_launch_failure_waiting_on_action_settles_after_action_quiesces(
        self,
    ) -> None:
        active_request_id = "active-during-launch-failure"

        def fail_with_active_action(workspace: dict[str, Any]) -> None:
            launch_id = str(workspace["launch_id"])
            with self.state._lock:
                job = self.state.interface_launches[launch_id]
                job.output_action_threads[active_request_id] = Mock()
                job.output_actions_quiesced.clear()
            raise RuntimeError("injected launch failure")

        self.fake_runtime.on_exec = fail_with_active_action
        with patch.object(
            studio_server,
            "_run_component_setup_in_workspace_runtime",
            return_value={"ran": False, "skipped": True, "reason": "test"},
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.state._lock:
                    job = self.state.interface_launches[launch_id]
                    worker_done = job.worker_done.is_set()
                if worker_done:
                    break
                time.sleep(0.01)
            else:
                self.fail("failed launch worker did not reach its action fence")

        pending = _interface_launch_by_id(self.state, launch_id)
        self.assertEqual(pending["status"], "stopping")
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertEqual(job.terminal_target, "failed")
            self.assertFalse(job.output_actions_quiesced.is_set())
            self.assertIsNotNone(job.output_session)
        with self.assertRaisesRegex(
            studio_server.RealmConflict, "stopping"
        ):
            studio_server._schedule_interface_output_action(
                self.state,
                job,
                studio_server.InterfaceOutputExecutionRequest(
                    request_id="late-request",
                    action_id="run",
                    output_path=".",
                ),
                output_id="",
                source_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("closing launch must not snapshot")
                ),
            )

        studio_server._complete_interface_output_action_thread(
            self.state,
            job,
            active_request_id,
        )

        settled = _interface_launch_by_id(self.state, launch_id)
        self.assertEqual(settled["status"], "failed")
        self.assertTrue(settled["result"]["cleanup"]["cleaned"])
        with self.state._lock:
            self.assertTrue(job.output_actions_quiesced.is_set())
            self.assertIsNone(job.output_session)

    def test_file_output_is_viewable_but_not_an_editable_workspace(self) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            output_root = Path(
                job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"]
            )
            control_file = Path(
                job.runtime_handles["OPTPILOT_INTERFACE_OUTPUTS_FILE"]
            )
        result_file = output_root / "summary.txt"
        result_file.write_text("interface result\n", encoding="utf-8")
        with control_file.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    _output_record(
                        output_id="summary",
                        label="Summary",
                        kind="file",
                        path="summary.txt",
                    )
                )
                + "\n"
            )

        outputs = _capture_interface_outputs_once(self.state, launch_id)
        output = next(item for item in outputs if item["id"] == "summary")
        self.assertEqual(output["kind"], "file")
        self.assertTrue(output["actions"]["view_read_only"]["eligible"])
        self.assertFalse(output["actions"]["keep_as_workspace"]["supported"])

        opened = _view_interface_output(
            self.state,
            launch_id,
            "summary",
            requested_session_id=None,
        )["content_view"]
        preview = _selection_content_byte_range(
            self.state,
            handle=opened["handle"],
            session_id=opened["content_session_id"],
            relative_path=None,
            offset=0,
            limit=1024,
        )
        self.assertEqual(preview["encoding"], "utf-8")
        self.assertEqual(preview["text"], "interface result\n")
        _close_selection_content_view(
            self.state,
            handle=opened["handle"],
            session_id=opened["content_session_id"],
        )

    def test_ready_payload_is_private_and_keep_is_idempotent_and_reopenable(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        expected_content = "MODEL = 'same-content'\n"
        self._publish_output(launch_id, content=expected_content)
        outputs = _capture_interface_outputs_once(self.state, launch_id)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["status"], "ready")
        current = _interface_launch_by_id(self.state, launch_id)
        serialized = json.dumps(current, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        for private_name in (
            "runtime_handles",
            "runtime_workspace",
            "output_session",
            "output_watcher",
            "OPTPILOT_INTERFACE_OUTPUT_ROOT",
            "OPTPILOT_INTERFACE_OUTPUTS_FILE",
        ):
            self.assertNotIn(private_name, serialized)

        first = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="11111111-1111-4111-8111-111111111111",
        )
        second = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="11111111-1111-4111-8111-111111111111",
        )
        refreshed_browser_retry = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="55555555-5555-4555-8555-555555555555",
        )
        self.assertEqual(first["keep"], second["keep"])
        workspace_id = str(first["keep"]["workspace_id"])
        self.assertEqual(workspace_id, second["workspace"]["id"])
        self.assertEqual(
            workspace_id, refreshed_browser_retry["workspace"]["id"]
        )
        self.assertEqual(first["output"]["keep_state"], "saved")
        self.assertEqual(first["output"]["kept_workspace_id"], workspace_id)
        refreshed = _capture_interface_outputs_once(self.state, launch_id)
        self.assertEqual(refreshed[0]["keep_state"], "saved")
        self.assertEqual(refreshed[0]["kept_workspace_id"], workspace_id)
        self.assertEqual(
            len(self.realm.editable_workspaces.list_workspaces()),
            1,
        )

        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")
        with self.state._lock:
            self.state.interface_launches.pop(launch_id)
        recovered_launch = _interface_launch_by_id(self.state, launch_id)
        self.assertTrue(recovered_launch["recovered"])
        self.assertEqual(recovered_launch["status"], "stopped")
        recovered_output = recovered_launch["result"]["outputs"][0]
        self.assertEqual(recovered_output["keep_state"], "saved")
        self.assertEqual(recovered_output["kept_workspace_id"], workspace_id)
        self.assertEqual(
            (Path(first["workspace"]["root"]) / "simulator.py").read_text(
                encoding="utf-8"
            ),
            expected_content,
        )

        reopened = _reopen_managed_workspace(
            self.state,
            workspace_id,
            {"expected_workspace_revision": 1},
        )
        self.assertEqual(reopened["ownership"], "realm-managed")
        self.assertEqual(
            (Path(reopened["root"]) / "simulator.py").read_text(encoding="utf-8"),
            expected_content,
        )

    def test_unchanged_output_poll_does_not_claim_new_launch_activity(self) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.result["outputs"] = []
            job.updated_at = 123.0

        self.assertEqual(_capture_interface_outputs_once(self.state, launch_id), [])
        with self.state._lock:
            self.assertEqual(self.state.interface_launches[launch_id].updated_at, 123.0)

        self._publish_output(launch_id)
        outputs = _capture_interface_outputs_once(self.state, launch_id)
        self.assertEqual([item["status"] for item in outputs], ["ready"])
        with self.state._lock:
            self.assertGreater(self.state.interface_launches[launch_id].updated_at, 123.0)

    def test_expired_output_session_resumes_only_under_the_live_studio_runtime(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            stale = self.state.interface_launches[launch_id].output_session
        assert stale is not None
        self._expire_output_session_lease(stale)

        with patch.object(
            self.runtime_supervisor_claim,
            "assert_active_for",
            wraps=self.runtime_supervisor_claim.assert_active_for,
        ) as assert_active:
            resumed = studio_server._resume_interface_outputs_after_expiry(
                self.state,
                launch_id,
                stale,
            )

        assert_active.assert_called_once_with(self.state.cwd)
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.session.session_id, stale.session.session_id)
        self.assertNotEqual(resumed.lease.lease_id, stale.lease.lease_id)
        self.assertGreater(
            resumed.lease.fencing_token,
            stale.lease.fencing_token,
        )
        with self.state._lock:
            current = self.state.interface_launches[launch_id].output_session
        self.assertEqual(current, resumed)

        with self.assertRaisesRegex(
            studio_server.RealmConflict,
            "lease fence is stale",
        ):
            self.realm.interface_outputs.heartbeat_session(
                operation_id="test/studio-output-resume/stale-heartbeat",
                handle=stale,
                ttl_seconds=90,
            )

        renewed = self.realm.interface_outputs.heartbeat_session(
            operation_id="test/studio-output-resume/current-heartbeat",
            handle=resumed,
            ttl_seconds=90,
        )
        self.assertEqual(renewed.lease.lease_id, resumed.lease.lease_id)
        self.assertGreater(
            renewed.lease.heartbeat_revision,
            resumed.lease.heartbeat_revision,
        )

    def test_unchanged_declaration_is_retried_after_expired_fence_recovery(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            original_watcher = job.output_watcher
        assert original_watcher is not None
        original_watcher.join(timeout=5)
        self.assertFalse(original_watcher.is_alive())

        self._publish_output(launch_id)
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            stale = job.output_session
            control_file = Path(
                job.runtime_handles["OPTPILOT_INTERFACE_OUTPUTS_FILE"]
            )
            job.output_watcher_stop = threading.Event()
        assert stale is not None
        self.assertEqual(
            self.realm.interface_outputs.list_statuses(handle=stale),
            (),
        )
        declaration_stat = control_file.stat()
        declaration_signature = (
            int(declaration_stat.st_dev),
            int(declaration_stat.st_ino),
            int(declaration_stat.st_size),
            int(declaration_stat.st_mtime_ns),
            int(declaration_stat.st_ctime_ns),
        )
        self._expire_output_session_lease(stale)

        watcher = threading.Thread(
            target=studio_server._watch_interface_outputs,
            args=(self.state, launch_id),
        )
        with self.state._lock:
            self.state.interface_launches[launch_id].output_watcher = watcher
        watcher.start()
        ready = False
        statuses: tuple[Any, ...] = ()
        current = stale
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.state._lock:
                    job = self.state.interface_launches[launch_id]
                    current = job.output_session or stale
                statuses = self.realm.interface_outputs.list_statuses(
                    handle=current
                )
                if statuses and statuses[0].state.value == "ready":
                    ready = True
                    break
                time.sleep(0.01)
        finally:
            with self.state._lock:
                self.state.interface_launches[
                    launch_id
                ].output_watcher_stop.set()
            watcher.join(timeout=5)

        self.assertFalse(watcher.is_alive())
        self.assertTrue(ready, statuses)
        self.assertNotEqual(current.lease.lease_id, stale.lease.lease_id)
        self.assertGreater(
            current.lease.fencing_token,
            stale.lease.fencing_token,
        )
        final_stat = control_file.stat()
        self.assertEqual(
            (
                int(final_stat.st_dev),
                int(final_stat.st_ino),
                int(final_stat.st_size),
                int(final_stat.st_mtime_ns),
                int(final_stat.st_ctime_ns),
            ),
            declaration_signature,
        )

    def test_swapped_output_handle_cannot_use_another_launch_runtime_proof(
        self,
    ) -> None:
        first_launch_id, _first = self._start_launch()
        second_launch_id, _second = self._start_launch()
        with self.state._lock:
            first_job = self.state.interface_launches[first_launch_id]
            second_job = self.state.interface_launches[second_launch_id]
            first_handle = first_job.output_session
            second_handle = second_job.output_session
            first_job.output_session = second_handle
        assert first_handle is not None
        assert second_handle is not None

        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "launch identity changed",
            ):
                studio_server._resume_interface_outputs_after_expiry(
                    self.state,
                    first_launch_id,
                    second_handle,
                )
        finally:
            with self.state._lock:
                self.state.interface_launches[
                    first_launch_id
                ].output_session = first_handle

        first_current = self.realm.interface_outputs.recover_session(
            launch_id=first_launch_id
        )
        second_current = self.realm.interface_outputs.recover_session(
            launch_id=second_launch_id
        )
        self.assertEqual(first_current.lease.lease_id, first_handle.lease.lease_id)
        self.assertEqual(
            second_current.lease.lease_id,
            second_handle.lease.lease_id,
        )

    def test_expired_output_session_does_not_resume_for_terminal_runtime(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            stale = job.output_session
            runtime_id = str(job.runtime_workspace["id"])
        assert stale is not None
        self._expire_output_session_lease(stale)
        self.fake_runtime._write_record(
            runtime_id,
            {
                "status": "stopped",
                "terminal_proof": {
                    "terminal_confirmed": True,
                    "state": "absent",
                },
            },
        )

        with self.assertRaisesRegex(RuntimeError, "runtime is terminal"):
            studio_server._resume_interface_outputs_after_expiry(
                self.state,
                launch_id,
                stale,
            )

        recovered = self.realm.interface_outputs.recover_session(
            launch_id=launch_id
        )
        self.assertEqual(recovered.lease.lease_id, stale.lease.lease_id)
        self.assertEqual(
            recovered.lease.fencing_token,
            stale.lease.fencing_token,
        )
        with self.state._lock:
            self.assertIs(
                self.state.interface_launches[launch_id].output_session,
                stale,
            )

    def test_stop_request_prevents_expired_output_session_resume(self) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            stale = job.output_session
            job.stop_requested = True
            job.output_watcher_stop.set()
        assert stale is not None
        self._expire_output_session_lease(stale)

        resumed = studio_server._resume_interface_outputs_after_expiry(
            self.state,
            launch_id,
            stale,
        )

        self.assertIsNone(resumed)
        recovered = self.realm.interface_outputs.recover_session(
            launch_id=launch_id
        )
        self.assertEqual(recovered.lease.lease_id, stale.lease.lease_id)
        self.assertEqual(
            recovered.lease.fencing_token,
            stale.lease.fencing_token,
        )

    def test_stop_racing_after_resume_commit_retires_replacement_fence(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            original_watcher = job.output_watcher
            stale = job.output_session
        assert original_watcher is not None
        assert stale is not None
        original_watcher.join(timeout=5)
        self.assertFalse(original_watcher.is_alive())
        with self.state._lock:
            self.state.interface_launches[
                launch_id
            ].output_watcher_stop = threading.Event()
        self._expire_output_session_lease(stale)

        resume_committed = threading.Event()
        allow_resume_return = threading.Event()
        replacement_handles: list[Any] = []
        recovery_result: dict[str, Any] = {}
        stop_result: dict[str, Any] = {}
        original_resume = self.realm.interface_outputs.resume_expired_session

        def pause_after_commit(*args: Any, **kwargs: Any) -> Any:
            replacement = original_resume(*args, **kwargs)
            replacement_handles.append(replacement)
            resume_committed.set()
            if not allow_resume_return.wait(timeout=5):
                raise RuntimeError("test did not release committed resume")
            return replacement

        def recover() -> None:
            try:
                recovery_result["value"] = (
                    studio_server._resume_interface_outputs_after_expiry(
                        self.state,
                        launch_id,
                        stale,
                    )
                )
            except BaseException as error:
                recovery_result["error"] = error

        def stop() -> None:
            try:
                stop_result["value"] = _stop_interface_launch(
                    self.state,
                    launch_id,
                )
            except BaseException as error:
                stop_result["error"] = error

        recovery_thread = threading.Thread(target=recover)
        stop_thread: threading.Thread | None = None
        with patch.object(
            self.realm.interface_outputs,
            "resume_expired_session",
            side_effect=pause_after_commit,
        ):
            recovery_thread.start()
            try:
                self.assertTrue(resume_committed.wait(timeout=5))
                stop_thread = threading.Thread(target=stop)
                stop_thread.start()
                deadline = time.monotonic() + 5
                stop_published = False
                while time.monotonic() < deadline:
                    with self.state._lock:
                        stop_published = self.state.interface_launches[
                            launch_id
                        ].stop_requested
                    if stop_published:
                        break
                    time.sleep(0.01)
                self.assertTrue(stop_published)
            finally:
                allow_resume_return.set()
            recovery_thread.join(timeout=5)
            if stop_thread is not None:
                stop_thread.join(timeout=5)

        self.assertFalse(recovery_thread.is_alive())
        assert stop_thread is not None
        self.assertFalse(stop_thread.is_alive())
        self.assertNotIn("error", recovery_result)
        self.assertNotIn("error", stop_result)
        self.assertIsNone(recovery_result["value"])
        self.assertEqual(len(replacement_handles), 1)
        replacement = replacement_handles[0]
        self.assertEqual(replacement.lease.state.value, "active")
        stopped = stop_result["value"]
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(stopped["result"]["cleanup"]["session_retired"])

        with sqlite3.connect(self.realm.ledger.database_path) as connection:
            session_row = connection.execute(
                "SELECT state, session_lease_id "
                "FROM interface_output_sessions WHERE session_id = ?",
                (replacement.session.session_id,),
            ).fetchone()
            replacement_row = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ?",
                (replacement.lease.lease_id,),
            ).fetchone()
        self.assertEqual(
            session_row,
            ("retired", replacement.lease.lease_id),
        )
        self.assertEqual(replacement_row, ("released",))
        with self.assertRaises(
            (studio_server.RealmConflict, studio_server.RealmNotFound)
        ):
            self.realm.interface_outputs.heartbeat_session(
                operation_id="test/studio-output-resume/retired-heartbeat",
                handle=replacement,
                ttl_seconds=90,
            )

    def test_transient_control_file_stat_error_does_not_stop_output_watcher(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop.set()
            original_watcher = job.output_watcher
        assert original_watcher is not None
        original_watcher.join(timeout=5)
        self.assertFalse(original_watcher.is_alive())

        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.output_watcher_stop = threading.Event()
        original_paths = studio_server._interface_output_paths
        poll_failed = threading.Event()
        capture_retried = threading.Event()
        path_calls = 0

        class TransientlyUnavailableControlFile:
            def stat(self) -> Any:
                poll_failed.set()
                raise OSError("temporary synchronized-filesystem failure")

        def output_paths(current_job: Any) -> Any:
            nonlocal path_calls
            path_calls += 1
            if path_calls == 1:
                return TransientlyUnavailableControlFile(), {}
            return original_paths(current_job)

        def capture_once(_state: UiState, _launch_id: str) -> list[dict[str, Any]]:
            capture_retried.set()
            with self.state._lock:
                self.state.interface_launches[
                    launch_id
                ].output_watcher_stop.set()
            return []

        with (
            patch.object(
                studio_server,
                "_interface_output_paths",
                side_effect=output_paths,
            ),
            patch.object(
                studio_server,
                "_capture_interface_outputs_once",
                side_effect=capture_once,
            ),
        ):
            watcher = threading.Thread(
                target=studio_server._watch_interface_outputs,
                args=(self.state, launch_id),
            )
            watcher.start()
            self.assertTrue(poll_failed.wait(timeout=5))
            self.assertTrue(capture_retried.wait(timeout=5))
            watcher.join(timeout=5)

        self.assertFalse(watcher.is_alive())
        self.assertGreaterEqual(path_calls, 2)
        with self.state._lock:
            self.assertNotIn(
                "output_capture_error",
                self.state.interface_launches[launch_id].result,
            )

    def test_keep_recovers_after_uncertain_completion_without_duplicate_workspace(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        self._publish_output(launch_id)
        _capture_interface_outputs_once(self.state, launch_id)

        with patch.object(
            self.state.coordination,
            "complete_action",
            side_effect=RuntimeError("injected response loss before receipt"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected response loss"):
                _keep_interface_output_as_workspace(
                    self.state,
                    launch_id,
                    "generated-simulator",
                    request_id="66666666-6666-4666-8666-666666666666",
                )

        self.assertEqual(
            len(self.realm.editable_workspaces.list_workspaces()),
            1,
        )
        uncertain = _capture_interface_outputs_once(self.state, launch_id)[0]
        self.assertEqual(uncertain["keep_state"], "retryable")
        self.assertIsNone(uncertain["kept_workspace_id"])

        recovered = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            # A refreshed browser is allowed to carry a new transport UUID.
            request_id="77777777-7777-4777-8777-777777777777",
        )
        workspace_id = recovered["workspace"]["id"]
        self.assertEqual(recovered["output"]["keep_state"], "saved")
        self.assertEqual(recovered["output"]["kept_workspace_id"], workspace_id)
        self.assertEqual(
            len(self.realm.editable_workspaces.list_workspaces()),
            1,
        )

    def test_saved_output_workspace_link_survives_coordination_store_reopen(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        self._publish_output(launch_id)
        _capture_interface_outputs_once(self.state, launch_id)
        kept = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="88888888-8888-4888-8888-888888888888",
        )
        workspace_id = kept["workspace"]["id"]

        store_type = type(self.state.coordination)
        database_path = self.state.coordination.database_path
        self.state.coordination.close()
        self.state.coordination = store_type(database_path)

        refreshed = _capture_interface_outputs_once(self.state, launch_id)[0]
        self.assertEqual(refreshed["keep_state"], "saved")
        self.assertEqual(refreshed["kept_workspace_id"], workspace_id)
        replay = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="99999999-9999-4999-8999-999999999999",
        )
        self.assertEqual(replay["workspace"]["id"], workspace_id)
        self.assertEqual(
            len(self.realm.editable_workspaces.list_workspaces()),
            1,
        )

    def test_retired_launch_reconciles_workspace_created_before_missing_receipt(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        self._publish_output(launch_id)
        _capture_interface_outputs_once(self.state, launch_id)
        with patch.object(
            self.state.coordination,
            "complete_action",
            side_effect=RuntimeError("injected completion interruption"),
        ):
            with self.assertRaisesRegex(RuntimeError, "completion interruption"):
                _keep_interface_output_as_workspace(
                    self.state,
                    launch_id,
                    "generated-simulator",
                    request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                )
        workspace_id = self.realm.editable_workspaces.list_workspaces()[0].workspace_id

        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")
        store_type = type(self.state.coordination)
        database_path = self.state.coordination.database_path
        self.state.coordination.close()
        self.state.coordination = store_type(database_path)
        with self.state._lock:
            self.state.interface_launches.pop(launch_id)

        recovered = _interface_launch_by_id(self.state, launch_id)
        output = recovered["result"]["outputs"][0]
        self.assertTrue(recovered["recovered"])
        self.assertEqual(output["keep_state"], "saved")
        self.assertEqual(output["kept_workspace_id"], workspace_id)
        self.assertEqual(
            len(self.realm.editable_workspaces.list_workspaces()),
            1,
        )

    def test_manual_tree_picker_captures_and_keeps_through_the_same_lifecycle(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            output_root = Path(job.runtime_handles["OPTPILOT_INTERFACE_OUTPUT_ROOT"])
        generated = output_root / "manual-generation"
        generated.mkdir()
        (generated / "simulator.py").write_text(
            "MODEL = 'manual'\n",
            encoding="utf-8",
        )
        outside = self.root / "outside-picker"
        outside.mkdir()
        (output_root / "outside-link").symlink_to(
            outside,
            target_is_directory=True,
        )

        choices = _interface_output_tree_choices(self.state, launch_id)
        self.assertTrue(choices["action"]["eligible"])
        self.assertIn(".", choices["paths"])
        self.assertIn("manual-generation", choices["paths"])
        self.assertNotIn("outside-link", choices["paths"])

        with self.assertRaisesRegex(
            ValueError,
            "exactly a label and a relative path",
        ):
            _capture_interface_output_tree(
                self.state,
                launch_id,
                {
                    "label": "Invalid",
                    "path": "manual-generation",
                    "root": str(output_root),
                },
            )
        with self.assertRaisesRegex(ValueError, "canonical and relative"):
            _capture_interface_output_tree(
                self.state,
                launch_id,
                {"label": "Invalid", "path": str(outside)},
            )

        first = _capture_interface_output_tree(
            self.state,
            launch_id,
            {"label": "Manually selected simulator", "path": "manual-generation"},
        )
        replay = _capture_interface_output_tree(
            self.state,
            launch_id,
            {"label": "Manually selected simulator", "path": "manual-generation"},
        )
        self.assertEqual(first["output"], replay["output"])
        self.assertEqual(first["output"]["status"], "ready")
        self.assertEqual(first["output"]["kind"], "tree")
        self.assertTrue(first["output"]["actions"]["keep_as_workspace"]["eligible"])
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("runtime_handles", serialized)

        kept = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            str(first["output"]["id"]),
            request_id="22222222-2222-4222-8222-222222222222",
        )
        self.assertEqual(
            (Path(kept["workspace"]["root"]) / "simulator.py").read_text(
                encoding="utf-8"
            ),
            "MODEL = 'manual'\n",
        )

        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")
        public = _interface_launch_by_id(self.state, launch_id)
        action = public["actions"]["capture_output_tree"]
        self.assertFalse(action["eligible"])
        self.assertEqual(action["code"], "launch_stopped")
        stopped_choices = _interface_output_tree_choices(self.state, launch_id)
        self.assertEqual(stopped_choices["paths"], [])
        self.assertEqual(stopped_choices["action"]["code"], "launch_stopped")

    def test_code_server_allows_only_registered_realm_checkout_root(self) -> None:
        launch_id, _current = self._start_launch()
        self._publish_output(launch_id)
        _capture_interface_outputs_once(self.state, launch_id)
        kept = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="33333333-3333-4333-8333-333333333333",
        )
        workspace = kept["workspace"]
        workspace_root = Path(workspace["root"]).resolve()
        child = workspace_root / "nested"
        child.mkdir()
        unrelated_checkout_path = (
            self.realm.editable_workspaces.checkout_root
            / "not-a-registered-checkout"
            / "root"
        )
        unrelated_checkout_path.mkdir(parents=True)

        code_server = {
            "workspace_id": workspace["id"],
            "folder": str(workspace_root),
            "open_url": "http://127.0.0.1:18766/",
        }
        with patch.object(
            self.fake_runtime,
            "start_code_server",
            create=True,
            return_value=code_server,
        ) as start_code_server:
            opened = self.state.start_code_server(workspace_root)
            self.assertEqual(opened, code_server)
            started_workspace = start_code_server.call_args.args[0]
            self.assertEqual(started_workspace["ownership"], "realm-managed")

            for rejected in (child, unrelated_checkout_path):
                with self.subTest(path=rejected):
                    with self.assertRaisesRegex(
                        ValueError, "outside the OptPilot workspace"
                    ):
                        self.state.start_code_server(rejected)

        self.assertEqual(start_code_server.call_count, 1)

    def test_stop_captures_final_output_before_runtime_delete_and_session_retire(
        self,
    ) -> None:
        launch_id, current = self._start_launch()
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        runtime_dir = self.fake_runtime._workspace_runtime_dir(runtime_id)
        with self.state._lock:
            session_id = self.state.interface_launches[
                launch_id
            ].output_session.session.session_id  # type: ignore[union-attr]

        self.fake_runtime.on_stop = lambda _workspace: self._publish_output(
            launch_id,
            output_id="last-output",
            path="last-generation",
            content="MODEL = 'last'\n",
        )
        observed_before_delete: list[bool] = []

        def inspect_delete(_workspace_id: str) -> None:
            payload = _interface_launch_by_id(self.state, launch_id)
            outputs = payload["result"].get("outputs", [])
            observed_before_delete.append(
                runtime_dir.exists()
                and any(
                    item.get("id") == "last-output" and item.get("status") == "ready"
                    for item in outputs
                )
            )

        self.fake_runtime.on_delete = inspect_delete
        stopped = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(observed_before_delete, [True])
        self.assertLess(
            self.fake_runtime.events.index("stop"),
            self.fake_runtime.events.index("delete"),
        )
        self.assertFalse(runtime_dir.exists())
        self.assertTrue(
            any(
                item.get("id") == "last-output" and item.get("status") == "ready"
                for item in stopped["result"]["outputs"]
            )
        )
        session = self.realm.ledger.read_interface_output_session(
            actor_principal_id=self.realm.actor_principal_id,
            session_id=session_id,
        )
        self.assertEqual(session.state.value, "retired")
        with self.state._lock:
            self.assertIsNone(
                self.state.interface_launches[launch_id].output_session
            )

    def test_unavailable_runtime_disables_and_rejects_interface_before_launch(self) -> None:
        self.fake_runtime.health_result = {
            "ok": False,
            "available": True,
            "error": "Cannot connect to a private test socket path",
        }

        entry = _catalog_payload(self.state)["resources"][0]
        profile_action = entry["interface"]["profiles"][0]["launch"]
        aggregate_action = entry["interface"]["actions"]["launch"]
        detail_action = _catalog_detail(
            self.state, "resource", str(entry["uid"])
        )["entry"]["interface"]["profiles"][0]["launch"]

        self.assertEqual(profile_action, aggregate_action)
        self.assertEqual(profile_action, detail_action)
        self.assertFalse(profile_action["eligible"])
        self.assertEqual(profile_action["code"], "interface_runtime_unavailable")
        self.assertIn("Start Docker or Podman", profile_action["reason"])
        self.assertNotIn(str(self.root), json.dumps(profile_action))

        with self.assertRaises(studio_server.InterfaceLaunchRuntimeUnavailable):
            _start_catalog_interface_launch(
                self.state, "resource", str(entry["uid"])
            )

        self.assertEqual(self.state.interface_launches, {})
        self.assertEqual(list(self.state.runtime_dir.glob("interface-launch-*")), [])

    def test_prestart_launch_failure_cleans_runtime_session_and_projection(self) -> None:
        error = "Workspace runtime image build failed: injected builder error"
        self.fake_runtime.on_exec = lambda _workspace: (_ for _ in ()).throw(
            RuntimeError(error)
        )
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_wait_for_preview_ready",
                return_value={"ready": True, "skipped": False},
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {
                    "failed",
                    "cleanup_pending",
                    "stopped",
                }:
                    break
                time.sleep(0.01)
            else:
                self.fail("pre-start interface failure did not become terminal")

        self.assertEqual(current["status"], "failed", current)
        self.assertEqual(current["error_code"], "interface_runtime_failed")
        self.assertEqual(
            current["error"],
            "The isolated interface runtime could not be prepared.",
        )
        self.assertNotIn(error, json.dumps(current))
        self.assertFalse(current["can_stop"])
        self.assertTrue(current["result"]["cleanup"]["cleaned"])
        self.assertTrue(
            current["result"]["cleanup"]["runtime_terminal_confirmed"]
        )
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            runtime_id = str(job.runtime_workspace["id"])
            self.assertIsNone(job.output_session)
            self.assertIsNone(job.source_projection)
        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(runtime_id).exists()
        )
        self.assertIn("delete", self.fake_runtime.events)
        self.assertIn("close-preview", self.fake_runtime.events)
        self.assertNotIn("stop", self.fake_runtime.events)

    def test_process_exit_is_not_reported_as_a_readiness_timeout(self) -> None:
        private_diagnostic = (
            "touch: "
            + str(self.state.runtime_dir / "prepared" / "install.marker")
            + ": Read-only file system"
        )
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_wait_for_preview_ready",
                return_value={
                    "ready": False,
                    "skipped": False,
                    "processExited": True,
                    "exitCode": 23,
                    "timeoutSeconds": 30,
                    "error": "launch process exited before preview readiness",
                    "diagnostic": private_diagnostic,
                },
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {"failed", "cleanup_pending"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("process-exit interface failure did not become terminal")

        self.assertEqual(current["status"], "failed", current)
        self.assertEqual(current["error_code"], "interface_process_exited")
        self.assertEqual(
            current["error"],
            "The interface process exited with code 23 before it became ready.",
        )
        self.assertIn("Read-only file system", current["error_detail"])
        self.assertIn("[private]", current["error_detail"])
        self.assertNotIn(str(self.state.runtime_dir), json.dumps(current))
        self.assertNotIn("timeout", current["error"].lower())
        self.assertLessEqual(len(current["error_detail"]), 1000)

    def test_live_process_readiness_failure_remains_a_timeout(self) -> None:
        failure = studio_server._preview_readiness_failure(
            5173,
            {
                "ready": False,
                "skipped": False,
                "timeoutSeconds": 11,
                "error": "connection refused",
            },
        )

        code, message = studio_server._public_interface_launch_failure(failure)

        self.assertIsInstance(
            failure, studio_server.InterfaceLaunchReadinessTimeout
        )
        self.assertEqual(code, "interface_not_reachable")
        self.assertIn("kept running", message)
        self.assertIn("11s", message)

    def test_stop_during_startup_waits_for_worker_before_cleanup(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def pause_exec(_workspace: dict[str, Any]) -> None:
            entered.set()
            release.wait(timeout=10)

        self.fake_runtime.on_exec = pause_exec
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_wait_for_preview_ready",
                return_value={"ready": True, "skipped": False},
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            self.assertTrue(entered.wait(timeout=5))
            with self.state._lock:
                job = self.state.interface_launches[launch_id]
                runtime_id = str(job.runtime_workspace["id"])
                session = job.output_session
                projection = job.source_projection

            stopping = _stop_interface_launch(self.state, launch_id)
            self.assertEqual(stopping["status"], "stopping")
            self.assertIsNone(stopping["finished_at"])
            self.assertTrue(
                self.fake_runtime._workspace_runtime_dir(runtime_id).is_dir()
            )
            with self.state._lock:
                job = self.state.interface_launches[launch_id]
                self.assertIsNotNone(job.output_session)
                self.assertEqual(
                    job.output_session.session.session_id,  # type: ignore[union-attr]
                    session.session.session_id,  # type: ignore[union-attr]
                )
                self.assertIs(job.source_projection, projection)
            self.assertNotIn("delete", self.fake_runtime.events)
            self.assertNotIn("stop", self.fake_runtime.events)

            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                terminal = _interface_launch_by_id(self.state, launch_id)
                if terminal["status"] in {"stopped", "cleanup_pending"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("stopped launch did not settle")

        self.assertEqual(terminal["status"], "stopped", terminal)
        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(runtime_id).exists()
        )
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertIsNone(job.output_session)
            self.assertIsNone(job.source_projection)
            self.assertTrue(job.worker_done.is_set())

    def test_public_failure_and_logs_redact_source_paths_and_secrets(self) -> None:
        secret = "q7"
        nonsecret = "a"
        config = self.catalog_root / "resources" / "generated_tool" / "optpilot.resource.yaml"
        config.write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: generated-tool",
                    "name: Generated Tool",
                    "interface:",
                    "  label: Generated Tool UI",
                    "  command: [python, -m, http.server, '5173']",
                    "  runtime: {sandbox: process}",
                    "  grants: {envFromHost: [PUBLIC_MODE], secretsFromHost: [PRIVATE_INTERFACE_TOKEN]}",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        raw_error = f"provider failed at {self.root} using {secret}"
        self.fake_runtime.on_exec = lambda _workspace: (_ for _ in ()).throw(
            RuntimeError(raw_error)
        )
        with (
            patch.dict(
                os.environ,
                {
                    "PRIVATE_INTERFACE_TOKEN": secret,
                    "PUBLIC_MODE": nonsecret,
                },
            ),
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _interface_launch_by_id(self.state, launch_id)
                if current["status"] in {"failed", "cleanup_pending"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("injected failure did not settle")

        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertIn(secret, job.private_error)
            log_path = self.root / "private-interface.log"
            log_path.write_text(raw_error + "\n", encoding="utf-8")
            job.log_paths["stderr"] = str(log_path)
            job.public_path_redactions.add(str(log_path.parent))
        public = _interface_launch_by_id(self.state, launch_id)
        serialized = json.dumps(public, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(raw_error, serialized)
        self.assertEqual(public["error_code"], "interface_launch_failed")
        self.assertEqual(public["label"], "Generated Tool UI")
        self.assertEqual(public["logs"]["stderr"], "")
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertEqual(job.public_secret_redactions, {secret})

    def test_public_log_reader_refuses_a_container_writable_symlink(self) -> None:
        log_dir = self.root / "mutable-logs"
        log_dir.mkdir()
        private_file = self.root / "host-private.txt"
        private_text = "host-secret-that-container-cannot-read"
        private_file.write_text(private_text, encoding="utf-8")
        linked_log = log_dir / "interface.stderr.log"
        linked_log.symlink_to(private_file)
        job = studio_server.UiLaunchJob(
            launch_id="launch-log-symlink",
            kind="resource",
            uid="resource",
            label="Resource",
            port=5173,
            log_paths={"stderr": str(linked_log)},
        )

        public = job.to_dict()

        self.assertNotIn(private_text, json.dumps(public, sort_keys=True))
        self.assertNotIn("logs", public)

    def test_public_launch_log_io_runs_after_the_state_snapshot_unlocks(self) -> None:
        launch_id = "launch-public-snapshot"
        job = studio_server.UiLaunchJob(
            launch_id=launch_id,
            kind="resource",
            uid="resource",
            label="Snapshot",
            port=5173,
            launch_scope="catalog-transient",
        )
        with self.state._lock:
            self.state.interface_launches[launch_id] = job
        entered = threading.Event()
        release = threading.Event()
        result: list[dict[str, Any]] = []
        original = studio_server._launch_log_tail

        def blocked_tail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            release.wait(timeout=5)
            return original(*args, **kwargs)

        with patch.object(studio_server, "_launch_log_tail", side_effect=blocked_tail):
            worker = threading.Thread(
                target=lambda: result.append(
                    _interface_launch_by_id(self.state, launch_id)
                )
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            self.assertTrue(self.state._lock.acquire(timeout=1))
            try:
                job.steps.append(
                    {
                        "time": "later",
                        "status": "queued",
                        "title": "Later mutation",
                    }
                )
                job.public_path_redactions.add(str(self.root / "later"))
            finally:
                self.state._lock.release()
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["steps"], [])
        with self.state._lock:
            self.state.interface_launches.pop(launch_id, None)

    def test_settlement_exception_is_cleanup_debt_and_restores_launch_failure(
        self,
    ) -> None:
        raw_error = "provider launch failed"
        self.fake_runtime.on_exec = lambda _workspace: (_ for _ in ()).throw(
            RuntimeError(raw_error)
        )
        with (
            patch.object(
                studio_server,
                "_run_component_setup_in_workspace_runtime",
                return_value={"ran": False, "skipped": True, "reason": "test"},
            ),
            patch.object(
                studio_server,
                "_mark_prestart_interface_runtime_terminal",
                side_effect=RuntimeError("cleanup provider unavailable"),
            ),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pending = _interface_launch_by_id(self.state, launch_id)
                if pending["status"] == "cleanup_pending":
                    break
                time.sleep(0.01)
            else:
                self.fail("cleanup exception did not become cleanup_pending")

        self.assertEqual(pending["error_code"], "interface_cleanup_pending")
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertTrue(job.worker_done.is_set())
            self.assertEqual(job.terminal_error_code, "interface_launch_failed")
            self.assertIn("cleanup provider unavailable", job.private_error)

        settled = studio_server._settle_interface_launch(
            self.state,
            launch_id,
            terminal_target="failed",
        )

        self.assertEqual(settled["status"], "failed")
        self.assertEqual(settled["error_code"], "interface_launch_failed")
        self.assertNotIn("cleanup could not", settled["error"].lower())

    def test_prepared_builder_cleanup_debt_retains_all_borrowed_resources(
        self,
    ) -> None:
        launch_id, current = self._start_launch()
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        builder_id = "prepared-build-" + "b" * 24
        builder_workspace = {
            "id": builder_id,
            "root": str(self.root),
            "source_root": str(self.root),
            "source_type": "prepared-runtime-build",
            "mode": "read-only",
        }
        self.fake_runtime._write_record(
            builder_id,
            {
                "status": "running",
                "workspace_root": str(self.root),
                "source_type": "prepared-runtime-build",
                "mode": "read-only",
                "owner_launch_id": launch_id,
            },
        )
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            job.prepared_runtime_builders[builder_id] = builder_workspace
            session = job.output_session
            projection = job.source_projection

        def fail_builder_delete(workspace_id: str) -> None:
            if workspace_id == builder_id:
                raise RuntimeError("builder stop unconfirmed")

        self.fake_runtime.on_delete = fail_builder_delete
        pending = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(
            pending["result"]["cleanup"]["error"],
            "prepared_runtime_builder_cleanup_failed",
        )
        self.assertTrue(
            self.fake_runtime._workspace_runtime_dir(builder_id).exists()
        )
        self.assertTrue(
            self.fake_runtime._workspace_runtime_dir(runtime_id).exists()
        )
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertEqual(
                job.output_session.session.session_id,  # type: ignore[union-attr]
                session.session.session_id,  # type: ignore[union-attr]
            )
            self.assertIs(job.source_projection, projection)
            self.assertIn(builder_id, job.prepared_runtime_builders)

        self.fake_runtime.on_delete = None
        stopped = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(builder_id).exists()
        )
        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(runtime_id).exists()
        )

    def test_cancelled_cache_acquire_is_adopted_before_finalizer_release(self) -> None:
        launch_id, _current = self._start_launch()

        class Projection:
            source_path = (
                self.catalog_root / "resources" / "generated_tool"
            ).resolve()
            package_id = "local-package"
            revision = 1
            publisher_id = "test-publisher"
            relative_path = "resources/generated_tool"
            selection = type(
                "Selection",
                (),
                {"selection_digest": "b" * 64},
            )()

            def close(self) -> None:
                return None

        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            projection = job.source_projection or Projection()
            job.source_projection = projection  # type: ignore[assignment]
            workspace = dict(job.runtime_workspace)
        profile = studio_server._component_interface_profiles_for_uid(
            "resource",
            studio_server._encode_id(projection.source_path),
        )[0]
        entry_root = self.root / "prepared-entry"
        lease = studio_server.PreparedRuntimeLease(
            cache_key="a" * 64,
            launch_id=launch_id,
            entry_root=entry_root,
            payload_root=entry_root / "payload",
            lease_path=entry_root / "leases" / f"{launch_id}.json",
            cache_status="hit",
            manifest={},
            lease_id="lease-test",
        )
        stopped = {"value": False}

        def acquire(**_kwargs: Any) -> Any:
            stopped["value"] = True
            return lease

        with (
            patch.object(
                self.fake_runtime,
                "prepared_runtime_provider_identity",
                create=True,
                return_value={"imageDigest": "sha256:" + "b" * 64},
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "key_payload",
                return_value={"schema": "test"},
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "acquire",
                side_effect=acquire,
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "release",
            ) as release,
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                studio_server._run_cached_component_setup_in_workspace_runtime(
                    self.state,
                    workspace,
                    {},
                    Path(str(workspace["source_root"])),
                    lambda *_args, **_kwargs: None,
                    profile=profile,
                    setup_label="Interface setup",
                    setup={"cache": "prepared", "steps": []},
                    launch_env={},
                    should_stop=lambda: stopped["value"],
                )

        release.assert_not_called()
        with self.state._lock:
            self.assertIs(
                self.state.interface_launches[launch_id].prepared_runtime_lease,
                lease,
            )
            # The synthetic lease has no backing cache record; leave teardown
            # to exercise only ordinary launch cleanup.
            self.state.interface_launches[launch_id].prepared_runtime_lease = None

    def test_prepared_setup_receives_build_access_and_launch_receives_read_only_access(
        self,
    ) -> None:
        launch_id, _current = self._start_launch()

        class Projection:
            source_path = (
                self.catalog_root / "resources" / "generated_tool"
            ).resolve()
            package_id = "local-package"
            revision = 1
            publisher_id = "test-publisher"
            relative_path = "resources/generated_tool"
            selection = type(
                "Selection",
                (),
                {"selection_digest": "c" * 64},
            )()

            def close(self) -> None:
                return None

        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            projection = job.source_projection or Projection()
            job.source_projection = projection  # type: ignore[assignment]
            workspace = dict(job.runtime_workspace)
        profile = studio_server._component_interface_profiles_for_uid(
            "resource",
            studio_server._encode_id(projection.source_path),
        )[0]
        entry_root = self.root / "prepared-access-entry"
        payload_root = entry_root / "payload"
        lease = studio_server.PreparedRuntimeLease(
            cache_key="d" * 64,
            launch_id=launch_id,
            entry_root=entry_root,
            payload_root=payload_root,
            lease_path=entry_root / "leases" / f"{launch_id}.json",
            cache_status="built",
            manifest={},
            lease_id="lease-access-test",
        )
        build_environments: list[dict[str, str]] = []

        def execute_setup(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            build_environments.append(dict(kwargs["launch_env"]))
            return {"ran": True, "results": []}

        def acquire(*, build: Callable[[Path, Path], None], **_kwargs: Any) -> Any:
            payload_root.mkdir(parents=True)
            build(entry_root, payload_root)
            return lease

        launch_environment: dict[str, str] = {}
        with (
            patch.object(
                self.fake_runtime,
                "prepared_runtime_provider_identity",
                create=True,
                return_value={"imageDigest": "sha256:" + "e" * 64},
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "key_payload",
                return_value={"schema": "test"},
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "cache_key",
                return_value="f" * 64,
            ),
            patch.object(
                self.state.prepared_runtime_cache,
                "acquire",
                side_effect=acquire,
            ),
            patch.object(
                studio_server,
                "_execute_component_setup_specs",
                side_effect=execute_setup,
            ),
        ):
            result = studio_server._run_cached_component_setup_in_workspace_runtime(
                self.state,
                workspace,
                {},
                Path(str(workspace["source_root"])),
                lambda *_args, **_kwargs: None,
                profile=profile,
                setup_label="Interface setup",
                setup={"cache": "prepared", "steps": []},
                launch_env=launch_environment,
                should_stop=None,
            )

        self.assertEqual(len(build_environments), 1)
        build_environment = build_environments[0]
        self.assertEqual(
            build_environment["OPTPILOT_PREPARED_RUNTIME_ROOT"],
            str(payload_root),
        )
        self.assertEqual(
            build_environment["OPTPILOT_PREPARED_RUNTIME_ACCESS"],
            "build",
        )
        self.assertEqual(build_environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertTrue(
            build_environment["OPTPILOT_INTERFACE_EPHEMERAL_ROOT"].startswith(
                "/tmp/optpilot-prepared-runtime/prepared-build-"
            )
        )
        self.assertEqual(
            launch_environment,
            {
                "OPTPILOT_PREPARED_RUNTIME_ROOT": str(payload_root),
                "OPTPILOT_PREPARED_RUNTIME_ACCESS": "read-only",
            },
        )
        self.assertTrue(result and result["cache"]["readOnlyAtRuntime"])
        with self.state._lock:
            self.state.interface_launches[launch_id].prepared_runtime_lease = None

    def test_stop_racing_session_creation_keeps_failed_retirement_owned(self) -> None:
        service = self.realm.interface_outputs
        original_create = service.create_session
        original_retire = service.retire_session
        create_entered = threading.Event()
        allow_create = threading.Event()
        created_handles: list[Any] = []
        retire_attempts = 0

        def blocked_create(*args: Any, **kwargs: Any) -> Any:
            create_entered.set()
            allow_create.wait(timeout=5)
            handle = original_create(*args, **kwargs)
            created_handles.append(handle)
            return handle

        def fail_first_retire(*args: Any, **kwargs: Any) -> Any:
            nonlocal retire_attempts
            retire_attempts += 1
            if retire_attempts == 1:
                raise RuntimeError("retirement temporarily unavailable")
            return original_retire(*args, **kwargs)

        with (
            patch.object(service, "create_session", side_effect=blocked_create),
            patch.object(service, "retire_session", side_effect=fail_first_retire),
        ):
            created = _start_catalog_interface_launch(
                self.state, "resource", self.uid
            )
            launch_id = str(created["launch"]["launch_id"])
            self.assertTrue(create_entered.wait(timeout=5))
            stopping = _stop_interface_launch(self.state, launch_id)
            self.assertEqual(stopping["status"], "stopping")
            allow_create.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pending = _interface_launch_by_id(self.state, launch_id)
                if pending["status"] == "cleanup_pending":
                    break
                time.sleep(0.01)
            else:
                self.fail("failed session retirement did not remain cleanup debt")

        self.assertEqual(len(created_handles), 1)
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertIsNotNone(job.output_session)
            self.assertEqual(
                job.output_session.session.session_id,  # type: ignore[union-attr]
                created_handles[0].session.session_id,
            )
        session = self.realm.ledger.read_interface_output_session(
            actor_principal_id=self.realm.actor_principal_id,
            session_id=created_handles[0].session.session_id,
        )
        self.assertEqual(session.state.value, "active")

        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")

    def test_unconfirmed_runtime_stop_preserves_runtime_and_output_session(
        self,
    ) -> None:
        launch_id, current = self._start_launch()
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        runtime_dir = self.fake_runtime._workspace_runtime_dir(runtime_id)
        with self.state._lock:
            handle = self.state.interface_launches[launch_id].output_session
        assert handle is not None

        self.fake_runtime.confirm_stop = False
        pending = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(
            pending["result"]["cleanup"]["error"], "runtime_stop_unconfirmed"
        )
        self.assertTrue(runtime_dir.exists())
        self.assertNotIn("delete", self.fake_runtime.events)
        with self.state._lock:
            self.assertIs(
                self.state.interface_launches[launch_id].output_session, handle
            )
        session = self.realm.ledger.read_interface_output_session(
            actor_principal_id=self.realm.actor_principal_id,
            session_id=handle.session.session_id,
        )
        self.assertEqual(session.state.value, "active")

        self.fake_runtime.confirm_stop = True
        completed = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(completed["status"], "stopped")

    def test_unsettled_presentation_retains_runtime_and_borrowed_resources(
        self,
    ) -> None:
        launch_id, current = self._start_launch()
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        runtime_dir = self.fake_runtime._workspace_runtime_dir(runtime_id)
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            handle = job.output_session
            existing_projection = job.source_projection
            projection = Mock(
                root_path=getattr(
                    existing_projection, "root_path", self.root
                )
            )
            if existing_projection is not None:
                projection.close.side_effect = existing_projection.close
            job.source_projection = projection
        assert handle is not None

        with patch.object(
            self.state,
            "_stop_workspace_preview_proxy",
            return_value=False,
        ):
            pending = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(
            pending["result"]["cleanup"]["error"],
            "presentation_close_failed",
        )
        self.assertFalse(
            pending["result"]["cleanup"]["presentation_closed"]
        )
        self.assertTrue(runtime_dir.exists())
        self.assertNotIn("delete", self.fake_runtime.events)
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            self.assertIsNotNone(job.output_session)
            self.assertEqual(
                job.output_session.session.session_id,  # type: ignore[union-attr]
                handle.session.session_id,
            )
            self.assertIs(job.source_projection, projection)
        session = self.realm.ledger.read_interface_output_session(
            actor_principal_id=self.realm.actor_principal_id,
            session_id=handle.session.session_id,
        )
        self.assertEqual(session.state.value, "active")

        completed = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(completed["status"], "stopped")
        self.assertFalse(runtime_dir.exists())
        projection.close.assert_called_once_with()

    def test_editable_workspace_launch_reuses_tree_but_not_authoring_runtime(
        self,
    ) -> None:
        workspace, source_runtime_sentinel = self._create_editable_workspace()
        source_workspace_id = str(workspace["id"])
        session_visible_at_exec: list[bool] = []

        def inspect_exec(runtime_workspace: dict[str, Any]) -> None:
            launch_id = str(runtime_workspace["launch_id"])
            with self.state._lock:
                job = self.state.interface_launches[launch_id]
                session_visible_at_exec.append(job.output_session is not None)

        self.fake_runtime.on_exec = inspect_exec
        launch_id, current = self._start_editable_workspace_launch(source_workspace_id)
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        with self.state._lock:
            job = self.state.interface_launches[launch_id]
            runtime_workspace = dict(job.runtime_workspace)

        self.assertEqual(current["launch_scope"], "workspace-transient")
        self.assertTrue(current["can_stop"])
        self.assertEqual(session_visible_at_exec, [True])
        self.assertNotEqual(runtime_id, source_workspace_id)
        self.assertEqual(self.fake_runtime.executed_workspace_ids, [runtime_id])
        self.assertEqual(
            Path(runtime_workspace["root"]).resolve(),
            Path(workspace["root"]).resolve(),
        )
        self.assertEqual(runtime_workspace["mode"], "editable")
        self.assertEqual(runtime_workspace["source_workspace_id"], source_workspace_id)
        self.assertEqual(current["result"]["runtime"]["source_mount"], "read-write")
        self.assertEqual(current["result"]["preview"]["workspace_id"], runtime_id)
        self.assertEqual(
            sorted(self.state.workspaces_dir.glob("*/workspace")),
            [Path(workspace["root"]).resolve()],
        )

        self._publish_output(launch_id)
        outputs = _capture_interface_outputs_once(self.state, launch_id)
        self.assertEqual(outputs[0]["status"], "ready")
        kept = _keep_interface_output_as_workspace(
            self.state,
            launch_id,
            "generated-simulator",
            request_id="44444444-4444-4444-8444-444444444444",
        )
        self.assertEqual(
            (Path(kept["workspace"]["root"]) / "simulator.py").read_text(
                encoding="utf-8"
            ),
            "MODEL = 'generated'\n",
        )

        with self.assertRaisesRegex(
            studio_server.RealmConflict,
            "Stop the workspace interface launch before deleting",
        ):
            _delete_ui_workspace(self.state, source_workspace_id)

        stopped = _stop_interface_launch(self.state, launch_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.fake_runtime._workspace_runtime_dir(runtime_id).exists())
        self.assertTrue(source_runtime_sentinel.is_file())
        self.assertEqual(
            self.fake_runtime._read_record(source_workspace_id)["container_name"],
            "source-authoring-container",
        )
        self.assertNotIn(source_workspace_id, self.fake_runtime.stopped_workspace_ids)
        self.assertNotIn(source_workspace_id, self.fake_runtime.deleted_workspace_ids)

    def test_nested_workspace_interface_runs_from_its_component_folder(
        self,
    ) -> None:
        workspace, component_root = self._create_nested_editable_workspace()

        launch_id, current = self._start_editable_workspace_launch(
            str(workspace["id"])
        )

        self.assertEqual(current["status"], "ready")
        self.assertEqual(self.fake_runtime.executed_cwds, [component_root.resolve()])
        self.assertEqual(self.fake_runtime.executed_commands, [["./_launch.sh"]])
        with self.state._lock:
            runtime_workspace = dict(
                self.state.interface_launches[launch_id].runtime_workspace
            )
        self.assertEqual(
            Path(runtime_workspace["root"]).resolve(),
            Path(workspace["root"]).resolve(),
        )
        self.assertEqual(
            _stop_interface_launch(self.state, launch_id)["status"],
            "stopped",
        )

    def test_editable_workspace_unconfirmed_cleanup_is_recoverable(self) -> None:
        workspace, source_runtime_sentinel = self._create_editable_workspace()
        source_workspace_id = str(workspace["id"])
        launch_id, current = self._start_editable_workspace_launch(source_workspace_id)
        runtime_id = str(current["result"]["runtime"]["runtime_id"])
        with self.state._lock:
            handle = self.state.interface_launches[launch_id].output_session
        assert handle is not None

        self.fake_runtime.confirm_stop = False
        pending = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(
            pending["result"]["cleanup"]["error"],
            "runtime_stop_unconfirmed",
        )
        self.assertTrue(self.fake_runtime._workspace_runtime_dir(runtime_id).exists())
        self.assertTrue(source_runtime_sentinel.is_file())
        with self.state._lock:
            self.assertIs(
                self.state.interface_launches[launch_id].output_session,
                handle,
            )
        with self.assertRaisesRegex(
            studio_server.RealmConflict,
            "Stop the workspace interface launch before deleting",
        ):
            _delete_ui_workspace(self.state, source_workspace_id)

        self.fake_runtime.confirm_stop = True
        completed = _stop_interface_launch(self.state, launch_id)

        self.assertEqual(completed["status"], "stopped")
        self.assertFalse(self.fake_runtime._workspace_runtime_dir(runtime_id).exists())
        self.assertTrue(source_runtime_sentinel.is_file())
        self.assertNotIn(source_workspace_id, self.fake_runtime.stopped_workspace_ids)
        self.assertNotIn(source_workspace_id, self.fake_runtime.deleted_workspace_ids)

    def test_persisted_unconfirmed_launch_blocks_source_deletion_after_restart(
        self,
    ) -> None:
        workspace, source_runtime_sentinel = self._create_editable_workspace()
        source_workspace_id = str(workspace["id"])
        runtime_id = "interface-launch-restarted"
        self.fake_runtime._write_record(
            runtime_id,
            {
                "status": "running",
                "workspace_root": str(workspace["root"]),
                "source_type": "workspace-interface",
                "mode": "editable",
                "source_workspace_id": source_workspace_id,
                "transient": True,
            },
        )

        # There is deliberately no in-memory UiLaunchJob: this is the state a
        # new Studio process sees when prior cleanup could not be confirmed.
        self.assertEqual(self.state.interface_launches, {})
        with self.assertRaisesRegex(
            studio_server.RealmConflict,
            "Stop the workspace interface launch before deleting",
        ):
            _delete_ui_workspace(self.state, source_workspace_id)
        self.assertTrue(source_runtime_sentinel.is_file())
        self.assertTrue(self.fake_runtime._workspace_runtime_dir(runtime_id).is_dir())

        self.fake_runtime.confirm_stop = True
        self.state._cleanup_orphaned_interface_runtimes()
        self.assertFalse(self.fake_runtime._workspace_runtime_dir(runtime_id).exists())
        deleted = _delete_ui_workspace(self.state, source_workspace_id)
        self.assertTrue(deleted["files_deleted"])

    def test_restart_reclaims_prestart_interface_without_container_engine(self) -> None:
        runtime_id = "interface-launch-prestart-orphan"
        self.fake_runtime._write_record(
            runtime_id,
            {
                "status": "preparing",
                "workspace_root": str(self.root),
                "source_type": "catalog",
                "mode": "read-only",
                "transient": True,
            },
        )
        self.fake_runtime.confirm_stop = False

        self.state._cleanup_orphaned_interface_runtimes()

        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(runtime_id).exists()
        )
        self.assertIn(runtime_id, self.fake_runtime.deleted_workspace_ids)
        self.assertNotIn(runtime_id, self.fake_runtime.stopped_workspace_ids)

    def test_restart_reclaims_an_orphaned_prepared_builder_namespace(self) -> None:
        builder_id = "prepared-build-" + "c" * 24
        self.fake_runtime._write_record(
            builder_id,
            {
                "status": "preparing",
                "workspace_root": str(self.root),
                "source_type": "prepared-runtime-build",
                "mode": "read-only",
                "transient": True,
                "owner_launch_id": "launch-prior-process",
            },
        )
        self.fake_runtime.confirm_stop = False

        self.state._cleanup_orphaned_interface_runtimes()

        self.assertFalse(
            self.fake_runtime._workspace_runtime_dir(builder_id).exists()
        )
        self.assertIn(builder_id, self.fake_runtime.deleted_workspace_ids)
        self.assertNotIn(builder_id, self.fake_runtime.stopped_workspace_ids)

    def test_studio_orphan_reclaimer_does_not_retire_operator_job_sessions(
        self,
    ) -> None:
        launch_id = "operator-job-" + "a" * 32
        handle = self.realm.interface_outputs.create_session(
            operation_id="test/operator-job-output-session/create",
            launch_id=launch_id,
        )
        self.realm.ledger.release_interface_output_session_lease(
            operation_id="test/operator-job-output-session/release",
            actor_principal_id=self.realm.actor_principal_id,
            session_id=handle.session.session_id,
            lease_id=handle.lease.lease_id,
            holder_id=handle.lease.holder_id,
            fencing_token=handle.lease.fencing_token,
        )

        self.state._cleanup_orphaned_interface_runtimes()

        recovered = self.realm.interface_outputs.recover_session(launch_id=launch_id)
        self.assertEqual(recovered.session.state.value, "active")
        self.realm.interface_outputs.retire_session(
            operation_id="test/operator-job-output-session/retire",
            handle=recovered,
        )


if __name__ == "__main__":
    unittest.main()
