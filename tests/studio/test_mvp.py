"""Studio-facing MVP tests.

This module imports ``optpilot_studio`` at import time, so it lives under
``tests/studio``. Extracting its core-only cases into ``tests/core`` is
follow-up work.
"""

from __future__ import annotations

import json
import hashlib
import contextlib
import io
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from copy import deepcopy
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from unittest.mock import patch

import yaml

from optpilot.candidate_materialization import BoundsCandidateValidator, FileCandidateManifestValidator, WorkspaceBundleMaterializer
from optpilot.adapters import ReadOnlySQLiteQuery
from optpilot_studio.agent import (
    FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT,
    OPTPILOT_AGENT_TOOL_SPECS,
    OpenHandsAdapter,
    OpenHandsRuntimeConfig,
    load_assistant_system_prompt,
)
from optpilot.cli import build_parser, main as cli_main
from optpilot.candidate_staging import CandidateBundleStager, stage_candidate_file
from optpilot.config import compile_authoring_config
from optpilot.evidence import EvidenceView
from optpilot.environment import build_environment_snapshot
from optpilot.execution import _aggregate_metric_values, _worker_process_env
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot.package_index import expand_package_roots
from optpilot.package_validation import validate_package
from optpilot.provenance import PromptStore, build_generator_record, build_model_record
from optpilot.runner import run_expanded_study_spec
from optpilot.schema_validation import validate_public_config_schema
from optpilot.spec import StudySpec, load_expanded_study_spec, load_study_spec
from optpilot.storage import LocalEvidenceStore
from optpilot_studio.ui.server import (
    CatalogWorkspaceCreationUnsupported,
    UiLaunchJob,
    UiState,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeOptions,
    _agent_context_packet,
    _assistant_run_detail,
    _assistant_response_texts,
    _agent_session_by_id,
    _agent_session_operation_lock,
    _append_agent_message,
    _append_jsonl,
    _catalog_detail,
    _agent_settings_payload,
    _approve_agent_action,
    _attach_agent_workspace,
    _catalog_payload,
    _compatibility_payload,
    _cancel_agent_session,
    _create_agent_session,
    _create_ui_workspace,
    _default_catalog_roots,
    _delete_ui_workspace,
    _detach_agent_workspace,
    _detach_workspace,
    _discover_workspace_configs,
    _draft_study,
    _apply_package_plan,
    _list_agent_sessions,
    _list_ui_workspaces,
    _list_runs,
    _launch_catalog_interface,
    _interface_launch_by_id,
    _retain_interface_launch_logs,
    _open_catalog_workspace,
    _open_study_workspace,
    _handler_factory,
    _read_agent_approvals,
    _read_agent_events,
    _read_agent_messages,
    _reject_agent_action,
    _rename_ui_workspace,
    _require_ui_workspace,
    _resolve_agent_or_allowed_path,
    _sync_agent_session,
    _update_agent_settings,
    _upsert_agent_session,
    _execute_agent_tool,
    _local_code_server_executable,
    _match_workspace_pattern,
    _start_catalog_interface_launch,
    _start_workspace_interface_launch,
    _stop_interface_launch,
    _validate_study,
    _require_declared_env_from_host,
    _prepare_package_plan,
    _registered_path_value,
    _run_detail,
    _shell_needs_approval,
    _smoke_package_plan,
    _smoke_summary_errors,
    _select_plan_study,
    _run_status,
    _update_package_plan,
    _validate_package_plan,
    _wait_for_preview_ready,
    _preview_proxy_handler_factory,
)
from optpilot_studio.ui.runtime_supervisor import StudioRuntimeSupervisorClaim
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


JsonDict = Dict[str, Any]


def _write_fake_workspace_container(tmp_path: Path) -> Path:
    executable = tmp_path / "fake_workspace_container.py"
    state_path = tmp_path / "fake_workspace_container_state.json"
    log_path = tmp_path / "fake_workspace_container_calls.jsonl"
    executable.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, pathlib, subprocess, sys",
                f"state_path = pathlib.Path({str(state_path)!r})",
                f"log_path = pathlib.Path({str(log_path)!r})",
                "args = sys.argv[1:]",
                "with log_path.open('a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps(args) + '\\n')",
                "if args == ['--version']:",
                "    print('fake-docker 1.0')",
                "    raise SystemExit(0)",
                "if args == ['info']:",
                "    print('fake daemon ready')",
                "    raise SystemExit(0)",
                "state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'running': {}}",
                "def save():",
                "    state_path.write_text(json.dumps(state), encoding='utf-8')",
                "if len(args) >= 3 and args[:2] == ['image', 'inspect']:",
                "    print(json.dumps([{'Id': 'sha256:' + ('1' * 64), 'Os': 'linux', 'Architecture': 'amd64'}]))",
                "    raise SystemExit(0)",
                "if len(args) >= 2 and args[0] == 'pull':",
                "    print(args[1])",
                "    raise SystemExit(0)",
                "if args[:3] == ['inspect', '-f', '{{.State.Running}}']:",
                "    name = args[3] if len(args) > 3 else ''",
                "    if state['running'].get(name):",
                "        print('true')",
                "        raise SystemExit(0)",
                "    print('false')",
                "    raise SystemExit(1)",
                "if args and args[0] == 'commit':",
                "    print('sha256:' + ('2' * 64))",
                "    raise SystemExit(0)",
                "if args and args[0] == 'rm':",
                "    for name in args[1:]:",
                "        if not name.startswith('-'):",
                "            state['running'].pop(name, None)",
                "    save()",
                "    raise SystemExit(0)",
                "if args and args[0] == 'run':",
                "    name = 'container'",
                "    if '--name' in args:",
                "        name = args[args.index('--name') + 1]",
                "    state['running'][name] = True",
                "    save()",
                "    print(name)",
                "    raise SystemExit(0)",
                "if args and args[0] == 'exec':",
                "    index = 1",
                "    detach = False",
                "    cwd = None",
                "    env = os.environ.copy()",
                "    while index < len(args) and args[index].startswith('-'):",
                "        flag = args[index]",
                "        if flag == '-d':",
                "            detach = True",
                "            index += 1",
                "            continue",
                "        if flag in {'-w', '--workdir'}:",
                "            cwd = args[index + 1]",
                "            index += 2",
                "            continue",
                "        if flag in {'-e', '--env'}:",
                "            key, value = args[index + 1].split('=', 1)",
                "            env[key] = value",
                "            index += 2",
                "            continue",
                "        if flag == '--env-file':",
                "            for line in pathlib.Path(args[index + 1]).read_text(encoding='utf-8').splitlines():",
                "                if line and not line.startswith('#'):",
                "                    key, value = line.split('=', 1)",
                "                    env[key] = value",
                "            index += 2",
                "            continue",
                "        index += 1",
                "    command = args[index + 1:]",
                "    if detach:",
                "        raise SystemExit(0)",
                "    if any('code-server' in item for item in command):",
                "        print('12345')",
                "        raise SystemExit(0)",
                "    if command and command[0] in {'python', 'python3'}:",
                "        command[0] = sys.executable",
                "    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)",
                "    sys.stdout.write(completed.stdout)",
                "    sys.stderr.write(completed.stderr)",
                "    raise SystemExit(completed.returncode)",
                "raise SystemExit(2)",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _fake_workspace_container_calls(tmp_path: Path) -> List[List[str]]:
    log_path = tmp_path / "fake_workspace_container_calls.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _owned_terminal_workspace_runtime(
    state: UiState, workspace_id: str
) -> Path:
    """Create a runtime fixture that the provider can safely reclaim."""

    runtime_root = state.workspace_runtime._ensure_workspace_runtime_dir(workspace_id)
    state.workspace_runtime._write_record(
        workspace_id,
        {
            "status": "stopped",
            "terminal_proof": {
                "terminal_confirmed": True,
                "state": "absent",
            },
        },
    )
    return runtime_root


def _stub_workspace_preview_open(state: UiState) -> None:
    def open_preview(
        folder: Optional[Path],
        port: int,
        *,
        extra_ports: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        root = Path(folder or state.cwd).resolve()
        allowed_ports = sorted({int(port), *[int(item) for item in (extra_ports or [])]})
        return {
            "workspace_id": root.parent.name,
            "folder": str(root),
            "port": int(port),
            "preview_url": "http://127.0.0.1:29999/?__optpilot_presentation_token=test",
            "proxy": "studio",
            "proxy_target": f"http://127.0.0.1:29998/proxy/{int(port)}/",
            "allowed_ports": allowed_ports,
            "code_server": {},
        }

    state.workspace_preview_open = open_preview  # type: ignore[method-assign]

    def open_transient_preview(
        workspace: Dict[str, Any],
        port: int,
        *,
        extra_ports: Optional[Iterable[int]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        if should_stop is not None and should_stop():
            raise RuntimeError("Interface launch was stopped.")
        root = Path(str(workspace["root"])).resolve()
        allowed_ports = sorted({int(port), *[int(item) for item in (extra_ports or [])]})
        return {
            "workspace_id": str(workspace["id"]),
            "folder": str(root),
            "port": int(port),
            "preview_url": "http://127.0.0.1:29999/?__optpilot_presentation_token=test",
            "proxy": "studio",
            "proxy_target": f"http://127.0.0.1:29998/proxy/{int(port)}/",
            "allowed_ports": allowed_ports,
            "code_server": {},
        }

    state.transient_workspace_preview_open = open_transient_preview  # type: ignore[method-assign]


def _loopback_tcp_bind_available() -> bool:
    """Return whether this process may bind an ephemeral loopback TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            return False
    return True


_LOOPBACK_TCP_BIND_AVAILABLE = _loopback_tcp_bind_available()


def _write_retained_fixed_method(method_dir: Path) -> None:
    """Write a package-owned batch method suitable for retained smoke runs."""

    (method_dir / "method.py").write_text(
        "\n".join(
            [
                "class FixedMethod:",
                "    def __init__(self, definition, study_spec, rng):",
                "        self._done = False",
                "",
                "    def propose(self, n_candidates, study_state, evidence_view):",
                "        if self._done:",
                "            return []",
                "        self._done = True",
                "        return [{'candidate_id': 'fixed-candidate', 'format': 'parameters', 'spec': {'x': 0.5}}]",
                "",
                "    def observe(self, observations):",
                "        return None",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _retained_fixed_method_config() -> Dict[str, Any]:
    return {
        "apiVersion": "optpilot.io/v1",
        "config": "method",
        "id": "fixed-method",
        "entrypoint": {
            "python": "method:FixedMethod",
            "pythonPath": ["."],
            "protocol": "batch",
        },
        "settings": {"batchSize": 1},
        "accepts": {
            "formats": ["parameters"],
            "requires": {"context": ["candidate.parameters.schema"]},
        },
    }


def _publish_exact_study_builder_fixture(
    state: UiState,
    *,
    include_preview_resource: bool = False,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Publish one exact Realm catalog revision for Study Builder tests."""

    suffix = uuid.uuid4().hex[:12]
    source = state.cwd / f"realm-catalog-source-{suffix}"
    environment_dir = source / "environments" / "toy"
    fixed_method_dir = source / "methods" / "fixed"
    files_method_dir = source / "methods" / "files"
    environment_dir.mkdir(parents=True)
    fixed_method_dir.mkdir(parents=True)
    files_method_dir.mkdir(parents=True)
    if include_preview_resource:
        preview_resource_dir = source / "resources" / "preview_tool"
        preview_resource_dir.mkdir(parents=True)
        (preview_resource_dir / "README.md").write_text(
            "# Preview Tool\n\nHas a local frontend.\n",
            encoding="utf-8",
        )
        (preview_resource_dir / "index.html").write_text(
            "<h1>Preview</h1>\n",
            encoding="utf-8",
        )
        (preview_resource_dir / "optpilot.resource.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: optpilot.io/v1",
                    "config: resource",
                    "id: preview-tool",
                    "name: Preview Tool",
                    "interface:",
                    "  label: Preview UI",
                    "  command: [python, -m, http.server, '5173', --bind, 0.0.0.0]",
                    "  runtime:",
                    "    sandbox: process",
                    "    setup:",
                    "      steps:",
                    "        - uses: command",
                    "          command:",
                    "            - python",
                    "            - -c",
                    "            - \"from pathlib import Path; p=Path('setup-count.txt'); n=int(p.read_text() if p.exists() else '0')+1; p.write_text(str(n))\"",
                    "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    (environment_dir / "environment.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "optpilot.io/v1",
                "config": "environment",
                "id": "toy-environment",
                "evaluator": {
                    "python": "evaluator:evaluate",
                    "pythonPath": ["."],
                },
                "candidate": {
                    "format": "parameters",
                    "parameters": {
                        "schema": {
                            "x": {
                                "valueType": "float",
                                "min": 0,
                                "max": 1,
                            }
                        }
                    },
                },
                "metrics": {"source": "return", "keys": ["score", "cost"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (environment_dir / "evaluator.py").write_text(
        "def evaluate(candidate_runtime, context):\n"
        "    value = float(candidate_runtime.get('x', 0))\n"
        "    return {'status': 'success', 'metric_values': {'score': value, 'cost': 1 - value}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
        encoding="utf-8",
    )
    (fixed_method_dir / "method.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "optpilot.io/v1",
                "config": "method",
                "id": "fixed-method",
                "entrypoint": {
                    "python": "method:Method",
                    "pythonPath": ["."],
                    "protocol": "batch",
                },
                "accepts": {
                    "formats": ["parameters"],
                    "requires": {
                        "context": ["candidate.parameters.schema"]
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (fixed_method_dir / "method.py").write_text(
        "class Method:\n"
        "    def __init__(self, definition, study_spec, rng): pass\n"
        "    def propose(self, n_candidates, study_state, evidence_view): return []\n"
        "    def observe(self, observations): pass\n",
        encoding="utf-8",
    )
    (files_method_dir / "method.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "optpilot.io/v1",
                "config": "method",
                "id": "files-method",
                "entrypoint": {
                    "python": "method:Method",
                    "pythonPath": ["."],
                    "protocol": "batch",
                },
                "accepts": {"formats": ["files"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (files_method_dir / "method.py").write_text(
        "class Method:\n"
        "    def __init__(self, definition, study_spec, rng): pass\n"
        "    def propose(self, n_candidates, study_state, evidence_view): return []\n"
        "    def observe(self, observations): pass\n",
        encoding="utf-8",
    )

    runtime = state.realm_runtime
    if runtime is None or getattr(runtime, "closed", False):
        runtime = LocalRealmRuntime.open(
            realm_root=state.cwd / ".optpilot-ui" / "realm",
            actor_principal_id=f"local-user:mvp-{suffix}",
        )
        state.realm_runtime = runtime
    actor = runtime.actor_principal_id
    owner_id = f"mvp-study-builder-artifact-{suffix}"
    runtime.ledger.create_owner(
        operation_id=f"mvp/{suffix}/owner",
        owner_id=owner_id,
        owner_kind="package-plan-artifact",
        principal_id=actor,
    )
    change = runtime.ledger.begin_owner_change(
        operation_id=f"mvp/{suffix}/begin",
        actor_principal_id=actor,
        owner_id=owner_id,
        expected_owner_revision=0,
        ttl_seconds=TEST_LEASE_TTL_SECONDS,
    )
    sealed = runtime.content_service.capture(
        actor_principal_id=actor,
        change_id=change.change_id,
        store_id=runtime.content_store.store_id,
    ).seal_tree(
        source=AllowedTreeSource(source),
        operation_id=f"mvp/{suffix}/seal",
    )
    membership = OwnerMembership(
        runtime.content_store.store_id,
        sealed.snapshot_ref,
        "package-plan-artifact",
    )
    runtime.ledger.hold_owner_content(
        operation_id=f"mvp/{suffix}/hold",
        actor_principal_id=actor,
        change_id=change.change_id,
        memberships=(membership,),
    )
    committed = runtime.ledger.commit_owner_change(
        operation_id=f"mvp/{suffix}/commit",
        actor_principal_id=actor,
        change_id=change.change_id,
        expected_owner_revision=0,
        additions=(membership,),
    )
    package_id = f"mvp-study-builder-{suffix}"
    identity = {"package_id": package_id, "artifact": str(sealed.snapshot_ref)}
    owned_paths = ["environments/toy", "methods/fixed", "methods/files"]
    if include_preview_resource:
        owned_paths.append("resources/preview_tool")
    runtime.catalog.publish(
        operation_id=f"mvp/{suffix}/publish",
        package_id=package_id,
        publisher_id=f"publisher/mvp/{suffix}",
        source_owner_id=owner_id,
        expected_source_owner_revision=committed.owner_revision,
        source_store_id=membership.store_id,
        source_role=membership.role,
        root_ref=membership.content_ref,
        owned_paths=tuple(owned_paths),
        plan_digest=request_digest({"plan": identity}),
        validation_digest=request_digest({"validation": identity}),
        smoke_digest=request_digest({"smoke": identity}),
        expected_head=None,
    )
    catalog = _catalog_payload(state)
    environment = next(
        item for item in catalog["environments"] if item["id"] == "toy-environment"
    )
    fixed_method = next(
        item for item in catalog["methods"] if item["id"] == "fixed-method"
    )
    files_method = next(
        item for item in catalog["methods"] if item["id"] == "files-method"
    )
    return environment, fixed_method, files_method


class MvpIntegrationTest(unittest.TestCase):
    def test_openai_file_editor_rejects_empty_edit_payloads(self) -> None:
        from test_catalog.example_package.methods.openai_file_editor.method import _extract_edited_files

        with self.assertRaisesRegex(ValueError, "non-empty `files` list"):
            _extract_edited_files({"summary": "No changes."}, ["dispatch_rule.py"])

        with self.assertRaisesRegex(ValueError, "not editable"):
            _extract_edited_files(
                {"files": [{"path": "other.py", "content": "print('nope')\n"}]},
                ["dispatch_rule.py"],
            )

        self.assertEqual(
            _extract_edited_files(
                {"files": [{"path": "dispatch_rule.py", "content": ""}]},
                ["dispatch_rule.py"],
            ),
            {"dispatch_rule.py": ""},
        )




    def test_job_shop_rl_uses_environment_owned_training_context(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        spec = compile_authoring_config(
            repo_root / "test_catalog" / "example_package" / "studies" / "job_shop_rl_stable_baselines.yaml"
        )
        method_config = spec["method"]["config"]
        references = spec["candidate"]["context"]["methodContext"]["references"]
        capabilities = {item["id"] for item in spec["candidate"]["context"]["capabilities"]}

        self.assertNotIn("trainInstances", method_config)
        self.assertIn("job-shop-rl-training-context", capabilities)
        self.assertEqual(
            {reference["name"] for reference in references if reference.get("type") == "job_shop_training_case"},
            {"train_tiny_a", "train_tiny_b"},
        )
        adapter = next(reference for reference in references if reference["name"] == "rl_env_adapter")
        self.assertEqual(adapter["type"], "python_module")
        self.assertTrue(Path(adapter["path"]).exists())


    def test_documented_objective_aggregation_modes(self) -> None:
        metric_results = [
            {"metric_values": {"score": 1.0}},
            {"metric_values": {"score": 3.0}},
            {"metric_values": {"score": 7.0}},
            {"metric_values": {"score": 9.0}},
        ]
        expected = {
            "mean": 5.0,
            "median": 5.0,
            "min": 1.0,
            "max": 9.0,
            "sum": 20.0,
            "last": 9.0,
        }

        for mode, value in expected.items():
            with self.subTest(mode=mode):
                objective = {
                    "primaryMetric": {"name": "score", "direction": "maximize"},
                    "aggregation": {"mode": mode},
                }
                self.assertEqual(_aggregate_metric_values(metric_results, objective)["score"], value)

    def test_authoring_config_accepts_weighted_mean_aggregation(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp_dir:
            study_path = Path(tmp_dir) / "weighted_mean_study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "weighted-mean-study",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                        "objective": {
                            "metric": "throughput",
                            "direction": "maximize",
                            "aggregation": "weighted_mean",
                        },
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            spec = compile_authoring_config(study_path)

        self.assertEqual(spec["objective"]["aggregation"]["mode"], "weighted_mean")

    def test_study_config_rejects_top_level_instances(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp_dir:
            study_path = Path(tmp_dir) / "instances_study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "instances-study",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "instances": {"source": "files", "paths": ["unused.yaml"]},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "instances"):
                compile_authoring_config(study_path)

    def test_job_shop_case_settings_match_method_references(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        spec = compile_authoring_config(repo_root / "test_catalog" / "example_package" / "studies" / "job_shop_ortools_cpsat.yaml")

        settings_cases = {
            case["id"]
            for case in spec["environment"]["adapter"]["config"]["evaluate"]["config"]["cases"]
        }
        reference_cases = {
            reference["name"]
            for reference in spec["candidate"]["context"]["methodContext"]["references"]
            if reference.get("type") == "job_shop_case"
        }

        self.assertEqual(reference_cases, settings_cases)
        self.assertEqual(settings_cases, {"ft06_small", "ft06_standard", "la01_tiny"})

    def test_weighted_mean_supports_per_result_weights(self) -> None:
        metric_results = [
            {"metric_values": {"score": 1.0}},
            {"metric_values": {"score": 3.0}},
            {"metric_values": {"score": 7.0}},
            {"metric_values": {"score": 9.0}},
        ]
        objective = {
            "primaryMetric": {"name": "score", "direction": "maximize"},
            "aggregation": {"mode": "weighted_mean", "weights": {"score": [1, 1, 2, 2]}},
        }

        self.assertEqual(_aggregate_metric_values(metric_results, objective)["score"], 6.0)

    def test_expanded_study_execution_is_removed(self) -> None:
        with self.assertRaisesRegex(ValueError, "removed by the Realm cutover"):
            run_expanded_study_spec("expanded-study.yaml", output_root="runs")

    def test_environment_snapshot_hashes_dependency_files_near_study_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            project = root / "project"
            studies = project / "studies"
            studies.mkdir(parents=True)
            pyproject = project / "pyproject.toml"
            lockfile = project / "uv.lock"
            requirements = studies / "requirements.txt"
            spec_path = studies / "study.yaml"
            pyproject.write_text("[project]\nname = 'demo'\n", encoding="utf-8")
            lockfile.write_text("version = 1\n", encoding="utf-8")
            requirements.write_text("pyyaml\n", encoding="utf-8")
            spec_path.write_text("config: run_spec\n", encoding="utf-8")

            snapshot = build_environment_snapshot(study_spec_path=spec_path)
            dependencies = {Path(item["path"]).name: item for item in snapshot["dependency_files"]}

            self.assertEqual(dependencies["pyproject.toml"]["sha256"], self._sha256(pyproject))
            self.assertEqual(dependencies["uv.lock"]["kind"], "lockfile")
            self.assertEqual(dependencies["requirements.txt"]["sha256"], self._sha256(requirements))

    def test_bounds_validator_rejects_out_of_range_candidates(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"
        raw_spec = compile_authoring_config(spec_path)
        study_spec = StudySpec(path=spec_path, raw=raw_spec)
        validator = BoundsCandidateValidator(
            raw_spec["candidate"]["validation"],
            study_spec,
        )

        report = validator.validate(
            {
                "candidate_id": "candidate-invalid",
                "format": "parameters",
                "spec": {"x": 99.0, "y": 7, "mode": "balanced"},
            },
            {},
        )

        self.assertFalse(report.accepted)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("above maximum", report.errors[0])

    def test_bounds_validator_uses_environment_contract_not_method_search_space(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        spec_path = (
            repo_root
            / "tests"
            / "fixtures"
            / "catalog"
            / "studies"
            / "toy_random_search.yaml"
        )
        raw_spec = compile_authoring_config(spec_path)
        environment_max = raw_spec["candidate"]["validation"]["config"][
            "searchSpace"
        ]["x"]["max"]
        raw_spec["method"]["config"]["searchSpace"] = {
            "x": {"valueType": "float", "min": -1000.0, "max": 1000.0}
        }
        study_spec = StudySpec(path=spec_path, raw=raw_spec)
        validator = BoundsCandidateValidator(
            raw_spec["candidate"]["validation"],
            study_spec,
        )

        report = validator.validate(
            {
                "candidate_id": "candidate-invalid-for-environment",
                "format": "parameters",
                "spec": {"x": 99.0, "y": 7, "mode": "balanced"},
            },
            {},
        )

        self.assertFalse(report.accepted)
        self.assertIn("above maximum", report.errors[0])
        self.assertEqual(
            raw_spec["candidate"]["validation"]["config"]["searchSpace"]["x"]["max"],
            environment_max,
        )

    def test_nested_parameter_candidate_compiles_and_enforces_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            environment_path = root / "environment.yaml"
            method_path = root / "method.yaml"
            study_path = root / "study.yaml"
            environment_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "nested-parameters",
                        "description": "Nested parameter contract.",
                        "evaluator": {"python": "tests.fixtures.bad_targets:non_numeric_metric"},
                        "candidate": {
                            "format": "parameters",
                            "description": "Parameters accepted by the evaluator.",
                            "parameters": {
                                "schema": {
                                    "x": {"valueType": "float", "min": 0.0, "max": 10.0},
                                    "mode": {"valueType": "categorical", "values": ["safe", "fast"]},
                                },
                                "constraints": [
                                    {
                                        "id": "fast_requires_large_x",
                                        "description": "Fast mode requires x >= 5.",
                                        "expr": {
                                            "any": [
                                                {
                                                    "compare": {
                                                        "op": "!=",
                                                        "left": {"param": "mode"},
                                                        "right": {"const": "fast"},
                                                    }
                                                },
                                                {
                                                    "compare": {
                                                        "op": ">=",
                                                        "left": {"param": "x"},
                                                        "right": {"const": 5.0},
                                                    }
                                                },
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                        "metrics": {"source": "return", "keys": ["throughput"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            method_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "parameter-method",
                        "description": "Parameter method.",
                        "entrypoint": {
                            "python": "optpilot.methods:ReferenceRandomSearchMethod",
                            "protocol": "batch",
                        },
                        "accepts": {
                            "formats": ["parameters"],
                            "requires": {"context": ["candidate.parameters.schema"]},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "nested-parameter-study",
                        "environmentConfig": "environment.yaml",
                        "methodConfig": "method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            raw_spec = compile_authoring_config(study_path)
            study_spec = StudySpec(path=study_path, raw=raw_spec)
            validator = BoundsCandidateValidator(raw_spec["candidate"]["validation"], study_spec)
            report = validator.validate(
                {
                    "candidate_id": "candidate-constrained",
                    "format": "parameters",
                    "spec": {"x": 2.0, "mode": "fast"},
                },
                {},
            )

            self.assertEqual(raw_spec["method"]["config"]["searchSpace"]["x"]["max"], 10.0)
            self.assertEqual(raw_spec["candidate"]["context"]["parameters"]["schema"]["mode"]["values"], ["safe", "fast"])
            self.assertFalse(report.accepted)
            self.assertTrue(any("fast_requires_large_x" in error for error in report.errors))

    def test_nested_file_candidate_exposes_context_and_checks_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "solver.py").write_text("def solve():\n    return 1\n", encoding="utf-8")
            instructions = root / "instructions.md"
            instructions.write_text("Edit only solver.py.", encoding="utf-8")
            database = root / "history.db"
            database.write_text("not a real db for this compiler test", encoding="utf-8")
            environment_path = root / "environment.yaml"
            method_path = root / "method.yaml"
            study_path = root / "study.yaml"

            environment_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "nested-files",
                        "description": "Nested file contract.",
                        "evaluator": {"command": ["python", "-c", "print('{}')"]},
                        "trialWorkspace": [
                            {"from": "source", "to": "candidate"},
                            {"from": "history.db", "to": "database.db"},
                        ],
                        "capabilities": [
                            {
                                "id": "historical_db_query",
                                "description": "Read-only SQL access.",
                            }
                        ],
                        "candidate": {
                            "format": "files",
                            "description": "Editable solver file.",
                            "files": {
                                "editable": [{"path": "solver.py"}],
                                "required": ["solver.py"],
                                "allow": ["solver.py"],
                                "deny": ["database.db"],
                            },
                            "materialize": {"root": "candidate"},
                        },
                        "methodContext": {
                            "instructions": ["instructions.md"],
                            "references": [
                                {
                                    "name": "historical_database",
                                    "path": "history.db",
                                    "type": "sqlite",
                                    "description": "Historical evaluation rows for prompt context.",
                                    "mimeType": "application/vnd.sqlite3",
                                }
                            ],
                        },
                        "metrics": {"source": "stdout", "keys": ["throughput"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            method_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "file-editor",
                        "description": "File editor.",
                        "entrypoint": {
                            "python": "tests.fixtures.catalog.user_methods.file_candidate_method:FileCandidateMethod",
                            "protocol": "batch",
                        },
                        "accepts": {
                            "formats": ["files"],
                            "requires": {
                                "context": ["candidate.files.editable", "methodContext.references"],
                                "capabilities": ["historical_db_query"],
                            },
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "nested-file-study",
                        "environmentConfig": "environment.yaml",
                        "methodConfig": "method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            raw_spec = compile_authoring_config(study_path)
            candidate_context = raw_spec["candidate"]["context"]
            adapter_config = raw_spec["environment"]["adapter"]["config"]

            self.assertEqual(raw_spec["candidate"]["format"], "files")
            self.assertEqual(candidate_context["files"]["editable"][0]["path"], "solver.py")
            self.assertEqual(candidate_context["files"]["root"], "candidate")
            self.assertEqual(candidate_context["methodContext"]["instructions"], [str(instructions.resolve())])
            self.assertEqual(candidate_context["methodContext"]["references"][0]["path"], str(database.resolve()))
            self.assertEqual(candidate_context["methodContext"]["references"][0]["type"], "sqlite")
            self.assertEqual(
                candidate_context["methodContext"]["references"][0]["description"],
                "Historical evaluation rows for prompt context.",
            )
            self.assertEqual(candidate_context["capabilities"][0]["id"], "historical_db_query")
            self.assertEqual(adapter_config["workspace"]["copy"][1]["from"], str(database.resolve()))
            self.assertEqual(adapter_config["workspace"]["copy"][1]["to"], "database.db")
            self.assertEqual(
                raw_spec["candidate"]["validation"]["config"]["requiredFiles"],
                ["solver.py"],
            )

    def test_readonly_sqlite_query_interface_rejects_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "history.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute("create table events (id integer, name text)")
                connection.execute("insert into events values (1, 'queued')")
                connection.commit()

            query = ReadOnlySQLiteQuery({"config": {"path": str(db_path), "maxRows": 10}})
            result = query.query("select * from events")

            self.assertEqual(result["rows"], [{"id": 1, "name": "queued"}])
            with self.assertRaisesRegex(ValueError, "Only SELECT/WITH"):
                query.query("delete from events")

    def test_file_candidate_manifest_validator_accepts_file_refs_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bundle_dir = tmp_path / "candidates" / "candidate-code-001" / "files"
            bundle_dir.mkdir(parents=True)
            solver_path = bundle_dir / "solver.py"
            helper_path = bundle_dir / "utils" / "helper.py"
            helper_path.parent.mkdir()
            solver_path.write_text("from utils.helper import score\n\ndef solve(x):\n    return score(x)\n", encoding="utf-8")
            helper_path.write_text("def score(x):\n    return x + 1\n", encoding="utf-8")
            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = FileCandidateManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )

            report = validator.validate(
                {
                    "candidate_id": "candidate-code-001",
                    "format": "files",
                    "spec": {
                        "bundleRef": "candidates/candidate-code-001/files",
                        "files": [
                            {
                                "path": "solver.py",
                                "contentRef": "candidates/candidate-code-001/files/solver.py",
                                "sha256": self._sha256(solver_path),
                            },
                            {
                                "path": "utils/helper.py",
                                "contentRef": "candidates/candidate-code-001/files/utils/helper.py",
                                "sha256": self._sha256(helper_path),
                            },
                        ],
                        "entrypoint": "solver:solve",
                    },
                },
                {},
            )

            self.assertTrue(report.accepted, report.errors)
            self.assertEqual(report.metadata["file_count"], 2)

    def test_code_manifest_validator_rejects_inline_content_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_path = tmp_path / "candidates" / "candidate-code-002" / "files" / "solver.py"
            source_path.parent.mkdir(parents=True)
            source_path.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = FileCandidateManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )

            report = validator.validate(
                {
                    "candidate_id": "candidate-code-002",
                    "format": "files",
                    "spec": {
                        "files": [
                            {
                                "path": "../solver.py",
                                "content": "def solve(x): return x",
                                "contentRef": "candidates/candidate-code-002/files/solver.py",
                                "sha256": self._sha256(source_path),
                            }
                        ],
                    },
                },
                {},
            )

            self.assertFalse(report.accepted)
            self.assertTrue(any("Inline source content is not allowed" in error for error in report.errors))
            self.assertTrue(any("safe relative POSIX path" in error for error in report.errors))

    def test_candidate_bundle_stager_creates_manifest_without_inline_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "generated"
            generated.mkdir()
            (generated / "solver.py").write_text("from utils.helper import score\n", encoding="utf-8")
            (generated / "utils").mkdir()
            (generated / "utils" / "helper.py").write_text("def score(x):\n    return x + 1\n", encoding="utf-8")
            (generated / "__pycache__").mkdir()
            (generated / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
            candidate_staging_root = tmp_path / "candidate-staging"
            stager = CandidateBundleStager(candidate_staging_root)

            candidate = stager.stage_directory(
                generated,
                candidate_id="candidate-generated-001",
                generator={"method_id": "llm_method", "strategy": "unit_test"},
            )

            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = FileCandidateManifestValidator(
                {
                    "implementation": "builtin.workspace_policy",
                    "config": {"allowAbsoluteContentRefs": True},
                },
                study_spec,
            )
            report = validator.validate(candidate, {})

            self.assertTrue(report.accepted, report.errors)
            self.assertEqual(candidate["format"], "files")
            self.assertEqual(len(candidate["spec"]["files"]), 2)
            self.assertFalse(self._contains_key(candidate, "content"))
            helper_ref = next(
                item["contentRef"]
                for item in candidate["spec"]["files"]
                if item["path"] == "utils/helper.py"
            )
            self.assertTrue(Path(helper_ref).exists())
            stored_relative = Path(helper_ref).resolve().relative_to(candidate_staging_root.resolve())
            self.assertTrue(stored_relative.parts[0].startswith("bundle-"))

    def test_candidate_bundle_stager_supports_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "solver.py"
            generated.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            candidate = stage_candidate_file(
                generated,
                tmp_path / "candidates",
                candidate_id="candidate-single-file",
                path="solver.py",
            )

            self.assertEqual(candidate["format"], "files")
            self.assertEqual(candidate["spec"]["files"][0]["path"], "solver.py")
            self.assertEqual(
                Path(candidate["spec"]["files"][0]["contentRef"]),
                Path(candidate["spec"]["bundleRef"]) / "solver.py",
            )
            self.assertTrue(Path(candidate["spec"]["bundleRef"]).is_absolute())

    def test_candidate_bundle_stager_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "solver.py"
            source.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            stager = CandidateBundleStager(tmp_path / "candidates")

            with self.assertRaisesRegex(ValueError, "Unsafe candidate file path"):
                stager.stage_files(
                    [{"source": source, "path": "../solver.py"}],
                    candidate_id="candidate-unsafe",
                )

            self.assertEqual(list((tmp_path / "candidates").glob("*")), [])

    def test_candidate_bundle_stager_never_uses_candidate_id_as_a_storage_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "solver.py"
            source.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            store_root = tmp_path / "candidates"
            stager = CandidateBundleStager(store_root)

            candidate = stager.stage_file(
                source,
                candidate_id="semantic/label",
                path="solver.py",
            )

            self.assertEqual(candidate["candidate_id"], "semantic/label")
            self.assertFalse((tmp_path / "escaped").exists())
            stored = Path(candidate["spec"]["files"][0]["contentRef"])
            self.assertTrue(stored.is_file())
            stored_relative = stored.resolve().relative_to(store_root.resolve())
            self.assertTrue(stored_relative.parts[0].startswith("bundle-"))

    def test_candidate_bundle_stager_rejects_symlink_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "outside.py"
            target.write_text("secret = True\n", encoding="utf-8")
            source_root = root / "source"
            source_root.mkdir()
            link = source_root / "solver.py"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                return

            store_root = root / "candidates"
            with self.assertRaisesRegex(ValueError, "symlink"):
                CandidateBundleStager(store_root).stage_directory(
                    source_root,
                    candidate_id="candidate-symlink",
                )

            self.assertEqual(list(store_root.glob("*")), [])

    def test_prompt_store_builds_prompt_and_model_generator_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = PromptStore(
                tmp_path / "prompts",
                content_ref_mode="relative",
                content_ref_base=tmp_path,
            )

            prompt_record = store.store_prompt(
                prompt_record_id="prompt-unit",
                messages=[
                    {"role": "system", "content": "Improve the solver."},
                    {"role": "user", "content": "Return a valid code bundle."},
                ],
                metadata={"task": "unit"},
            )
            model_record = build_model_record(
                provider="openai",
                model="gpt-5",
                parameters={"temperature": 0.2},
                invocation_id="invocation-001",
            )
            generator = build_generator_record(
                method_id="llm_method",
                strategy="code_evolution",
                prompt_record=prompt_record,
                model_record=model_record,
                extra={"owned_by": "user"},
            )

            prompt_path = tmp_path / prompt_record["contentRef"]
            self.assertTrue(prompt_path.exists())
            self.assertEqual(prompt_record["sha256"], self._sha256(prompt_path))
            self.assertEqual(generator["prompt_record_id"], "prompt-unit")
            self.assertEqual(generator["model_record"]["model"], "gpt-5")
            self.assertNotIn("Improve the solver", json.dumps(generator))

    def test_prompt_store_never_uses_record_id_as_a_storage_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = PromptStore(root / "prompts")

            record = store.store_prompt(
                prompt_record_id="../escaped",
                messages=[{"role": "user", "content": "bounded"}],
            )

            self.assertEqual(record["prompt_record_id"], "../escaped")
            self.assertFalse((root / "escaped").exists())
            stored = Path(record["contentRef"])
            relative = stored.resolve().relative_to((root / "prompts").resolve())
            self.assertTrue(relative.parts[0].startswith("record-"))

    def test_workspace_bundle_materializer_writes_candidate_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_dir = tmp_path / "candidates" / "candidate-code-003" / "files"
            source_dir.mkdir(parents=True)
            solver_path = source_dir / "solver.py"
            solver_path.write_text("def solve(x):\n    return x * 2\n", encoding="utf-8")
            seed_path = tmp_path / "seed_database.db"
            seed_path.write_text("seed", encoding="utf-8")
            protected_path = tmp_path / "protected.txt"
            protected_path.write_text("do not change", encoding="utf-8")
            workspace = tmp_path / "trial-workspace"
            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            materializer = WorkspaceBundleMaterializer(
                {
                    "implementation": "builtin.workspace_bundle",
                    "config": {
                        "candidateRoot": "candidate",
                        "seedFiles": [
                            {"source": "seed_database.db", "destination": "database.db"},
                            {"source": "protected.txt", "destination": "protected.txt"},
                        ],
                        "readonlyFiles": ["protected.txt"],
                    },
                },
                study_spec,
            )

            record = materializer.materialize(
                {
                    "candidate_id": "candidate-code-003",
                    "format": "files",
                    "spec": {
                        "bundleRef": "candidates/candidate-code-003/files",
                        "files": [
                            {
                                "path": "solver.py",
                                "contentRef": "candidates/candidate-code-003/files/solver.py",
                                "sha256": self._sha256(solver_path),
                            }
                        ],
                        "entrypoint": "solver:solve",
                    },
                },
                workspace,
                {},
            )

            manifest_path = Path(record.runtime_spec["manifestPath"])
            materialized_solver = workspace / "candidate" / "solver.py"
            materialized_seed = workspace / "database.db"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertTrue(materialized_solver.exists())
            self.assertTrue(materialized_seed.exists())
            self.assertEqual(materialized_solver.read_text(encoding="utf-8"), solver_path.read_text(encoding="utf-8"))
            self.assertEqual(record.runtime_spec["entrypoint"], "solver:solve")
            self.assertEqual(manifest["candidate_files"][0]["sha256"], self._sha256(solver_path))
            self.assertEqual(manifest["seed_files"][0]["sha256"], self._sha256(seed_path))
            self.assertEqual(manifest["readonly_files"][0]["sha256"], self._sha256(protected_path))
            self.assertEqual(record.metadata["candidate_file_count"], 1)
            self.assertEqual(record.metadata["seed_file_count"], 2)
            self.assertEqual(record.metadata["readonly_file_count"], 1)

    def test_cli_run_requires_package_root_and_rejects_output_root(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing_package:
                parser.parse_args(["run", "study.yaml"])
            with self.assertRaises(SystemExit) as removed_output:
                parser.parse_args(
                    [
                        "run",
                        "study.yaml",
                        "--package-root",
                        "package",
                        "--output-root",
                        "runs",
                    ]
                )

        self.assertEqual(missing_package.exception.code, 2)
        self.assertEqual(removed_output.exception.code, 2)
        args = parser.parse_args(
            ["run", "study.yaml", "--package-root", "package", "--realm-root", "realm"]
        )
        self.assertEqual(args.package_root, "package")
        self.assertEqual(args.realm_root, "realm")








    def test_process_runtimes_only_receive_declared_host_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPTPILOT_DECLARED_TOKEN": "visible",
                "OPTPILOT_UNDECLARED_TOKEN": "hidden",
                "PATH": os.environ.get("PATH", ""),
            },
            clear=False,
        ):
            worker_env = _worker_process_env({"envFromHost": ["OPTPILOT_DECLARED_TOKEN"], "env": {"STATIC_VALUE": "1"}})

        self.assertEqual(worker_env["OPTPILOT_DECLARED_TOKEN"], "visible")
        self.assertEqual(worker_env["STATIC_VALUE"], "1")
        self.assertNotIn("OPTPILOT_UNDECLARED_TOKEN", worker_env)

    def test_method_config_rejects_unimplemented_shapes(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for implementation in [
            {"service": "http://127.0.0.1:9999"},
            {"command": ["python", "method.py"], "protocol": "session"},
        ]:
            with self.subTest(implementation=implementation):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_path = Path(tmp_dir)
                    (tmp_path / "unsupported_method.yaml").write_text(
                        yaml.safe_dump(
                            {
                                "apiVersion": "optpilot.io/v1",
                                "config": "method",
                                "id": "unsupported-method",
                                "entrypoint": implementation,
                                "accepts": {"formats": ["parameters"]},
                            },
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    study_path = tmp_path / "unsupported_method_study.yaml"
                    study_path.write_text(
                        yaml.safe_dump(
                            {
                                "apiVersion": "optpilot.io/v1",
                                "config": "study",
                                "name": "unsupported-method-shape",
                                "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                                "methodConfig": "unsupported_method.yaml",
                                "objective": {"metric": "throughput", "direction": "maximize"},
                                "budget": {"maxTrials": 1},
                            },
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "entrypoint|command entrypoints"):
                        compile_authoring_config(study_path)




    def test_environment_config_rejects_malformed_custom_hook_refs(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        cases = [
            (
                {"adapter": "python:tests.fixtures.bad_targets:CustomAdapter"},
                {"source": "return", "keys": ["throughput"]},
                [],
                "evaluator.adapter",
            ),
            (
                {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
                {"source": "custom", "extractor": "python:tests.fixtures.bad_targets:custom_metrics", "keys": ["throughput"]},
                [],
                "metrics.extractor",
            ),
            (
                {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
                {"source": "return", "keys": ["throughput"]},
                [{"name": "events", "source": "custom", "extractor": "python:tests.fixtures.bad_targets:CustomRecordExtractor"}],
                "records.*extractor",
            ),
        ]
        for evaluator, metrics, records, error in cases:
            with self.subTest(error=error):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_path = Path(tmp_dir)
                    (tmp_path / "malformed_env.yaml").write_text(
                        yaml.safe_dump(
                            {
                                "apiVersion": "optpilot.io/v1",
                                "config": "environment",
                                "id": "malformed-hook-env",
                                "evaluator": evaluator,
                                "candidate": {
                                    "format": "parameters",
                                    "description": "Toy parameters.",
                                    "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 8.0}}},
                                },
                                "metrics": metrics,
                                "records": records,
                            },
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    study_path = Path(tmp_dir) / "malformed_environment_hook.yaml"
                    study_path.write_text(
                        yaml.safe_dump(
                            {
                                "apiVersion": "optpilot.io/v1",
                                "config": "study",
                                "name": "malformed-environment-hook",
                                "environmentConfig": "malformed_env.yaml",
                                "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                                "objective": {"metric": "throughput", "direction": "maximize"},
                                "budget": {"maxTrials": 1},
                            },
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, error):
                        compile_authoring_config(study_path)

    def test_study_config_rejects_removed_execution_fields(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        cases = [
            (
                {
                    "execution": {"backend": "local", "parallelism": 1},
                },
                "Additional properties.*backend",
            ),
            (
                {
                    "execution": {"runtime": {"sandbox": "process"}},
                },
                "Additional properties.*runtime",
            ),
        ]
        for overrides, error in cases:
            with self.subTest(error=error):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    payload = {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "unsupported-runtime-shape",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    }
                    payload.update(overrides)
                    study_path = Path(tmp_dir) / "unsupported_runtime_shape.yaml"
                    study_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, error):
                        compile_authoring_config(study_path)

    def test_method_config_rejects_removed_public_contract_fields(self) -> None:
        base_method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "removed-fields-method",
            "entrypoint": {"python": "tests.fixtures.catalog.user_methods.fixed_parameter_method:FixedParameterMethod"},
            "accepts": {"formats": ["parameters"]},
        }
        cases = [
            {"produces": {"format": "parameters", "parameters": {"schema": {"x": {"valueType": "float"}}}}},
            {"resourceProfile": {"cpu": 2}},
        ]
        for override in cases:
            raw = {**base_method, **override}
            result = validate_public_config_schema(raw)
            self.assertFalse(result.valid)
            self.assertTrue(any("Additional properties" in issue.message for issue in result.errors))

    def test_runtime_schema_uses_process_sandbox_and_setup(self) -> None:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "process-runtime-env",
            "evaluator": {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
            "runtime": {
                "sandbox": "process",
                "setup": {
                    "steps": [
                        {"uses": "command", "command": ["python", "--version"]},
                    ],
                    "timeoutSeconds": 30,
                },
            },
            "candidate": {
                "format": "parameters",
                "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 1.0}}},
            },
            "metrics": {"source": "return", "keys": ["throughput"]},
        }
        self.assertTrue(validate_public_config_schema(environment).valid)

        host_runtime = deepcopy(environment)
        host_runtime["runtime"] = {"sandbox": "host"}
        self.assertFalse(validate_public_config_schema(host_runtime).valid)

        container_on_process = deepcopy(environment)
        container_on_process["runtime"] = {"sandbox": "process", "container": {"image": "python:3.12"}}
        self.assertFalse(validate_public_config_schema(container_on_process).valid)

    def test_environment_container_sandbox_compiles_to_the_container_backend(
        self,
    ) -> None:
        # A container environment compiles into the record as one: container
        # backend, container sandbox, and the declaration kept as its own
        # sub-map -- never silently the process backend, which would run an
        # evaluator on the host that its author asked to be isolated.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "evaluator.py").write_text(
                "def evaluate(context):\n    return {'metric_values': {'throughput': 1.0}}\n",
                encoding="utf-8",
            )
            environment_path = tmp_path / "environment.yaml"
            environment_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "container-env",
                        "runtime": {
                            "sandbox": "container",
                            # Pinned and platform-bearing, so the declaration is
                            # well formed and the test reaches the refusal it is
                            # about: environments do not run in containers yet.
                            "container": {
                                "image": "ghcr.io/example/img@sha256:" + "a" * 64,
                                "platform": "linux/amd64",
                            },
                        },
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 1.0}}},
                        },
                        "metrics": {"source": "return", "keys": ["throughput"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            method_path = tmp_path / "method.yaml"
            method_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "container-env-method",
                        "entrypoint": {"python": "tests.fixtures.catalog.user_methods.fixed_parameter_method:FixedParameterMethod"},
                        "settings": {"batchSize": 1, "values": {"x": 0.5}},
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = tmp_path / "study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "container-env-study",
                        "environmentConfig": "environment.yaml",
                        "methodConfig": "method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            compiled = compile_authoring_config(study_path)

        execution = compiled["execution"]
        self.assertEqual(execution["backend"]["type"], "container")
        self.assertEqual(
            execution["backend"]["implementation"],
            "builtin.local_container_backend",
        )
        self.assertEqual(
            execution["defaults"]["sandboxSpec"]["runtimeType"], "container"
        )
        runtime = compiled["environment"]["runtime"]
        self.assertEqual(runtime["type"], "container")
        self.assertEqual(
            sorted(runtime["container"]), ["image", "network", "platform"]
        )

    def test_public_schema_rejects_unimplemented_runtime_and_candidate_shapes(self) -> None:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "schema-contract-env",
            "evaluator": {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
            "candidate": {
                "format": "files",
                "files": {
                    "editable": [{"path": "solver.py"}],
                },
            },
            "metrics": {"source": "return", "keys": ["throughput"]},
        }
        self.assertTrue(validate_public_config_schema(environment).valid)

        missing_editable = deepcopy(environment)
        missing_editable["candidate"]["files"] = {"required": ["solver.py"]}
        self.assertFalse(validate_public_config_schema(missing_editable).valid)

        empty_editable = deepcopy(environment)
        empty_editable["candidate"]["files"]["editable"] = []
        self.assertFalse(validate_public_config_schema(empty_editable).valid)

        # An image must already exist and be named by fingerprint: building
        # fetches software from the network, and what it fetches can differ
        # between builds, so a record naming a build would not describe what ran.
        container_build = deepcopy(environment)
        container_build["runtime"] = {
            "sandbox": "container",
            "container": {
                "build": {
                    "tag": "optpilot-test-env:latest",
                    "args": {"PYTHON_VERSION": "3.12"},
                }
            },
        }
        self.assertFalse(validate_public_config_schema(container_build).valid)

        pinned_digest = "sha256:" + "a" * 64
        container_image = deepcopy(environment)
        container_image["runtime"] = {
            "sandbox": "container",
            "container": {
                "image": f"ghcr.io/example/env@{pinned_digest}",
                "platform": "linux/amd64",
            },
        }
        self.assertTrue(validate_public_config_schema(container_image).valid)

        tagged_image = deepcopy(container_image)
        tagged_image["runtime"]["container"]["image"] = "ghcr.io/example/env:latest"
        self.assertFalse(validate_public_config_schema(tagged_image).valid)

        missing_platform = deepcopy(container_image)
        del missing_platform["runtime"]["container"]["platform"]
        self.assertFalse(validate_public_config_schema(missing_platform).valid)

        command_session_method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "command-session-method",
            "entrypoint": {"command": ["python", "method.py"], "protocol": "session"},
            "accepts": {"formats": ["parameters"]},
        }
        self.assertFalse(validate_public_config_schema(command_session_method).valid)

    def test_compile_maps_public_retry_to_scheduler_attempts(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp_dir:
            study_path = Path(tmp_dir) / "retry.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "retry-policy",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                        "execution": {"retry": {"maxRetries": 2}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            compiled = compile_authoring_config(study_path)

        self.assertEqual(compiled["execution"]["scheduler"]["config"]["retryPolicy"]["maxAttempts"], 3)
        self.assertEqual(compiled["execution"]["defaults"]["retryPolicy"]["maxRetries"], 2)






    def test_study_spec_rejects_unknown_environment_policy(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["environment"]["accessPolicy"] = "MagicAccess"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "bad_policy.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported environment.accessPolicy"):
                load_expanded_study_spec(str(spec_path))












    def test_local_evidence_store_read_api_and_summary_view(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalEvidenceStore(Path(tmp_dir), "evidence-read-api")
            extracted_dir = store.run_dir / "trials" / "trial-a" / "extracted_records"
            extracted_dir.mkdir(parents=True)
            machine_events_path = extracted_dir / "machine_events.jsonl"
            machine_events_path.write_text(
                "\n".join(
                    [
                        json.dumps({"event": "queued", "machine": "m1"}),
                        json.dumps({"event": "completed", "machine": "m1"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            store.record_observation(
                {
                    "trial_id": "trial-a",
                    "candidate_id": "candidate-a",
                    "status": "success",
                    "metric_values": {"throughput": 12.5},
                    "event_summary": {
                        "records": {
                            "streams": [
                                {
                                    "name": "machine_events",
                                    "source": "csv",
                                    "path": "events.csv",
                                    "record_count": 2,
                                    "contentRef": str(machine_events_path),
                                }
                            ]
                        }
                    },
                }
            )
            store.record_observation(
                {
                    "trial_id": "trial-b",
                    "candidate_id": "candidate-b",
                    "status": "failed",
                    "metric_values": {},
                    "event_summary": {"errors": [{"phase": "environment_evaluation"}]},
                }
            )
            store.record_candidate({"candidate_id": "candidate-a"})
            store.record_method_call({"method_id": "method-a", "event": "proposed"})
            store.record_scheduler_event({"event": "batch_submitted"})
            store.record_method_event({"method_id": "method-a", "event": "debug"})
            store.write_environment_snapshot({"python": {"version": "test"}, "packages": []})

            evidence_view = EvidenceView(store, study_spec)
            summary = evidence_view.summary()
            context = evidence_view.decision_context()
            failed_events = evidence_view.query_events("observation", status="failed")
            method_events = evidence_view.query_events(["method_call", "method_event"], method_id="method-a")
            scheduler_events = evidence_view.query_events("scheduler_event", event="batch_submitted")
            record_streams = evidence_view.record_streams("machine_events")
            extracted_records = evidence_view.records("machine_events")

            self.assertEqual(len(store.read_observations()), 2)
            self.assertEqual(summary.observation_count, 2)
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(summary.method_call_count, 1)
            self.assertEqual(summary.scheduler_event_count, 1)
            self.assertEqual(summary.method_event_count, 1)
            self.assertEqual(summary.status_counts["success"], 1)
            self.assertEqual(summary.status_counts["failed"], 1)
            self.assertEqual(summary.best_metric, 12.5)
            self.assertEqual(context["recent_failure_count"], 1)
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["event_type"], "observation")
            self.assertEqual(failed_events[0]["record"]["trial_id"], "trial-b")
            self.assertEqual(len(method_events), 2)
            self.assertEqual({event["event_type"] for event in method_events}, {"method_call", "method_event"})
            self.assertEqual(scheduler_events[0]["record"]["event"], "batch_submitted")
            self.assertEqual(len(record_streams), 1)
            self.assertEqual(record_streams[0]["trial_id"], "trial-a")
            self.assertEqual(record_streams[0]["record_count"], 2)
            self.assertEqual([row["record"]["event"] for row in extracted_records], ["queued", "completed"])
            self.assertEqual({row["trial_id"] for row in extracted_records}, {"trial-a"})
            self.assertEqual(extracted_records[0]["source"], "csv")
            self.assertEqual(store.read_environment_snapshot()["python"]["version"], "test")

    def test_ui_catalog_scans_authoring_configs_and_validates_study(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        state = UiState(cwd=repo_root, catalog_roots=[repo_root / "test_catalog" / "example_package"], run_roots=[])

        catalog = _catalog_payload(state)
        validation = _validate_study(repo_root / "test_catalog" / "example_package" / "studies" / "job_shop_rule_parameters_baseline.yaml")

        job_shop_parameter_environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-rule-parameters")
        job_shop_solution_environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-schedule-solution")
        job_shop_file_environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-dispatch-rule")
        method_ids = {item["id"] for item in catalog["methods"]}
        self.assertEqual(job_shop_parameter_environment["summary"]["candidate_format"], "parameters")
        self.assertEqual(job_shop_solution_environment["summary"]["candidate_format"], "parameters")
        self.assertEqual(job_shop_file_environment["summary"]["candidate_format"], "files")
        self.assertIn("dispatch_rule.py", job_shop_file_environment["summary"]["editable_files"])
        openai_method = next(item for item in catalog["methods"] if item["id"] == "openai-file-editor")
        self.assertEqual(openai_method["summary"]["candidate_formats"], ["files"])
        self.assertIn("baseline-file-copy", method_ids)
        self.assertIn("fixed-rule-parameters", method_ids)
        self.assertIn("job-shop-lib-dispatching-rule", method_ids)
        self.assertIn("job-shop-lib-simulated-annealing", method_ids)
        self.assertIn("job-shop-lib-ortools-cpsat", method_ids)
        self.assertIn("job-shop-rl-stable-baselines", method_ids)
        self.assertIn("openai-file-editor", method_ids)
        self.assertTrue(any(item["label"] == "job-shop-lib-dispatching-rule" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-lib-simulated-annealing" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-lib-ortools-cpsat" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-rl-stable-baselines" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-openai-dispatch-rule" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-rule-parameters-baseline" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-dispatch-rule-baseline" for item in catalog["studies"]))
        self.assertTrue(any(item["label"] == "job-shop-solver-code-baseline" for item in catalog["studies"]))
        self.assertIn("builtin.reference_random_search", catalog["builtins"]["method"])
        self.assertTrue(validation["valid"], validation)
        self.assertEqual(validation["environment_id"], "job-shop-rule-parameters")

    def test_core_package_validate_indexes_example_package(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        package = repo_root / "test_catalog" / "example_package"

        result = validate_package(package)
        entry_ids = {(entry["config"], entry["id"]) for entry in result["entries"]}

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["package_id"], "example_package")
        self.assertGreaterEqual(result["counts"]["environment"], 3)
        self.assertGreaterEqual(result["counts"]["method"], 6)
        self.assertGreaterEqual(result["counts"]["study"], 6)
        self.assertIn(("environment", "job-shop-rule-parameters"), entry_ids)
        self.assertIn(("method", "tune-dispatch-weights"), entry_ids)

    def test_core_package_validate_indexes_devs_gen_interface_resource(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        package = repo_root / "catalog" / "devs_gallery"

        result = validate_package(package)
        entry_ids = {(entry["config"], entry["id"]) for entry in result["entries"]}

        self.assertTrue(result["valid"], result)
        self.assertGreaterEqual(result["counts"]["resource"], 1)
        self.assertIn(("resource", "devs-gen-interface"), entry_ids)

    def test_core_package_roots_expand_catalog_folder_to_packages(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]

        roots = expand_package_roots([repo_root / "catalog"])

        self.assertIn(repo_root / "catalog" / "production_agv_scheduling", roots)
        # test_catalog/ is deliberately not a default root: it holds test-only
        # fixtures that must never be offered to users.
        self.assertNotIn(repo_root / "test_catalog" / "example_package", roots)

    def test_cli_package_validate_json_output(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(["package", "validate", str(repo_root / "test_catalog" / "example_package"), "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["valid"], payload)
        self.assertEqual(payload["package_id"], "example_package")
        self.assertIn("entries", payload)

    def test_cli_package_setup_check_reports_missing_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            env_dir = package / "environments" / "setup-env"
            env_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "setup-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "runtime": {"setup": {"steps": [{"uses": "python-venv", "requirements": ["missing.txt"]}]}},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(["package", "setup-check", str(package), "--json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["valid"])
        self.assertIn("requirements[0] does not exist", payload["entries"][0]["errors"][0])

    def test_cli_package_setup_check_runs_dot_optpilot_resource_setup_from_resource_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            resource = package / "resources" / "tool"
            manifest_dir = resource / ".optpilot"
            manifest_dir.mkdir(parents=True)
            (resource / "README.md").write_text("# Tool\n", encoding="utf-8")
            (manifest_dir / "resource.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "resource",
                        "id": "tool",
                        "name": "Tool",
                        "interface": {
                            "launchProfiles": [
                                {
                                    "id": "default",
                                    "command": ["python3", "-m", "http.server", "5173"],
                                    "runtime": {
                                        "setup": {
                                            "steps": [
                                                {
                                                    "uses": "command",
                                                    "command": [
                                                        "python3",
                                                        "-c",
                                                        "from pathlib import Path; Path('setup-root-marker.txt').write_text('ok')",
                                                    ],
                                                }
                                            ]
                                        }
                                    },
                                    "presentation": {"kind": "web", "port": 5173},
                                    "accepts": {"selectionKinds": ["workspace"]},
                                }
                            ]
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(["package", "setup-check", str(package), "--run-setup", "--json"])
            payload = json.loads(stdout.getvalue())
            marker_created = (resource / "setup-root-marker.txt").is_file()
            marker_in_manifest_dir = (manifest_dir / "setup-root-marker.txt").exists()

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(payload["valid"], payload)
        self.assertTrue(marker_created)
        self.assertFalse(marker_in_manifest_dir)

    def test_package_setup_validation_ignores_inline_python_command_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            env_dir = package / "environments" / "inline-setup"
            env_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "inline-setup",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "runtime": {
                            "setup": {
                                "steps": [
                                    {
                                        "uses": "command",
                                        "command": [
                                            "python3",
                                            "-c",
                                            "from pathlib import Path; Path('generated/setup_dep.py').write_text('VALUE = 1\\n')",
                                        ],
                                    }
                                ]
                            }
                        },
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(package, check_setup_files=True)

        self.assertTrue(result["valid"], result)

    def test_cli_package_smoke_runs_selected_study(self) -> None:
        self._require_retained_worker_transport()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package = tmp_path / "local_package"
            env_dir = package / "environments" / "toy-env"
            method_dir = package / "methods" / "random-method"
            study_dir = package / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': float(candidate_runtime.get('x', 0))}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "description": "Test candidate.",
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            _write_retained_fixed_method(method_dir)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    _retained_fixed_method_config(),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "smoke",
                        "environmentConfig": "../environments/toy-env/environment.yaml",
                        "methodConfig": "../methods/random-method/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(["package", "smoke", str(package), "--study", "studies/smoke.yaml", "--json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(payload["valid"], payload)

    def test_cli_package_smoke_rejects_mutating_component_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package = tmp_path / "local_package"
            env_dir = package / "environments" / "setup-env"
            method_dir = package / "methods" / "random-method"
            study_dir = package / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "import setup_dep\n\n"
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': float(setup_dep.VALUE)}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "setup-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "runtime": {
                            "setup": {
                                "steps": [
                                    {
                                        "uses": "command",
                                        "command": [
                                            "python3",
                                            "-c",
                                            "from pathlib import Path; Path('setup_dep.py').write_text('VALUE = 0.5\\n')",
                                        ],
                                    }
                                ]
                            }
                        },
                        "candidate": {
                            "description": "Test candidate.",
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            _write_retained_fixed_method(method_dir)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    _retained_fixed_method_config(),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "setup-smoke",
                        "environmentConfig": "../environments/setup-env/environment.yaml",
                        "methodConfig": "../methods/random-method/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(["package", "smoke", str(package), "--study", "studies/smoke.yaml", "--json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 1, payload)
        self.assertFalse(payload["valid"], payload)
        self.assertIn(
            "Study dependencies require runtime.setup.cache: prepared",
            " ".join(payload["errors"]),
        )

    def test_core_package_validate_checks_source_imports_and_setup_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            env_dir = package / "environments" / "missing-reference"
            env_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "missing-reference",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "runtime": {
                            "setup": {
                                "steps": [
                                    {"uses": "python-venv", "requirements": ["requirements.txt"]},
                                ],
                            },
                        },
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "methodContext": {"references": [{"name": "missing", "path": "missing.md"}]},
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            schema_only = validate_package(package)
            source_checked = validate_package(package, check_source=True)
            setup_checked = validate_package(package, check_setup_files=True)

        self.assertTrue(schema_only["valid"], schema_only)
        self.assertFalse(source_checked["valid"], source_checked)
        self.assertIn("methodContext.references[0].path does not exist", source_checked["entries"][0]["errors"][0])
        self.assertFalse(setup_checked["valid"], setup_checked)
        self.assertIn("requirements[0] does not exist", setup_checked["entries"][0]["errors"][0])

    def test_core_package_import_check_isolates_same_named_local_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            for name, class_name in (("alpha", "AlphaMethod"), ("beta", "BetaMethod")):
                method_dir = package / "methods" / name
                method_dir.mkdir(parents=True)
                (method_dir / "method.py").write_text(
                    f"class {class_name}:\n"
                    "    pass\n",
                    encoding="utf-8",
                )
                (method_dir / "method.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "optpilot.io/v1",
                            "config": "method",
                            "id": name,
                            "entrypoint": {"python": f"method:{class_name}", "pythonPath": ["."]},
                            "accepts": {"formats": ["parameters"]},
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )

            result = validate_package(package, check_imports=True, check_source=True)

        self.assertTrue(result["valid"], result)

    def test_core_package_source_check_rejects_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package = tmp_path / "local_package"
            env_dir = package / "environments" / "outside-ref"
            outside = tmp_path / "outside" / "prompt.md"
            env_dir.mkdir(parents=True)
            outside.parent.mkdir(parents=True)
            outside.write_text("not portable", encoding="utf-8")
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "outside-ref",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "methodContext": {"instructions": [str(outside)]},
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(package, check_source=True)

        self.assertFalse(result["valid"], result)
        self.assertIn("must stay inside package", result["entries"][0]["errors"][0])

    def test_core_package_import_check_ignores_ambient_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package = tmp_path / "local_package"
            method_dir = package / "methods" / "ambient"
            ambient_dir = tmp_path / "ambient"
            method_dir.mkdir(parents=True)
            ambient_dir.mkdir()
            (ambient_dir / "ambient_only.py").write_text("class AmbientMethod:\n    pass\n", encoding="utf-8")
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "ambient",
                        "entrypoint": {"python": "ambient_only:AmbientMethod", "pythonPath": ["."]},
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"PYTHONPATH": str(ambient_dir)}):
                result = validate_package(package, check_imports=True)

        self.assertFalse(result["valid"], result)
        self.assertIn("Could not import", result["entries"][0]["errors"][0])

    def test_ui_catalog_exposes_complete_component_config_yaml(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        state = UiState(cwd=repo_root, catalog_roots=[repo_root / "test_catalog" / "example_package"], run_roots=[])

        catalog = _catalog_payload(state)
        environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-rule-parameters")
        method = next(item for item in catalog["methods"] if item["id"] == "tune-dispatch-weights")

        self.assertIn("config: environment", environment["yaml"])
        self.assertIn("candidate:", environment["yaml"])
        self.assertIn("metrics:", environment["yaml"])
        self.assertIn("config: method", method["yaml"])
        self.assertIn("entrypoint:", method["yaml"])
        self.assertIn("accepts:", method["yaml"])
        resource = next(item for item in catalog["resources"] if item["id"] == "devs-gen-interface")
        self.assertIn("config: resource", resource["yaml"])
        self.assertIn("interface:", resource["yaml"])

        environment_detail = _catalog_detail(state, "environment", environment["uid"])
        method_detail = _catalog_detail(state, "method", method["uid"])
        resource_detail = _catalog_detail(state, "resource", resource["uid"])

        self.assertTrue(environment_detail["validation"]["valid"], environment_detail)
        self.assertTrue(method_detail["validation"]["valid"], method_detail)
        self.assertEqual(environment_detail["config"]["config"], "environment")
        self.assertEqual(method_detail["config"]["config"], "method")
        self.assertEqual(resource_detail["config"]["config"], "resource")
        self.assertIn("config: environment", environment_detail["yaml"])
        self.assertIn("config: method", method_detail["yaml"])
        self.assertIn("config: resource", resource_detail["yaml"])

        environment_by_id = _catalog_detail(state, "environment", environment["id"])
        method_by_qualified_id = _catalog_detail(state, "method", method["qualified_id"])
        self.assertEqual(environment_by_id["config"]["id"], environment["id"])
        self.assertEqual(method_by_qualified_id["config"]["id"], method["id"])

    def test_ui_catalog_edit_requires_published_realm_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[repo_root / "test_catalog" / "example_package"],
                run_roots=[],
            )
            catalog = _catalog_payload(state)
            environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-rule-parameters")
            capability = environment["actions"]["create_editable_workspace"]
            with self.assertRaises(
                CatalogWorkspaceCreationUnsupported
            ) as raised:
                _open_catalog_workspace(
                    state,
                    "environment",
                    environment["uid"],
                    editable=True,
                    request_id="c0000000-0000-4000-8000-000000000001",
                )
            runtime = state.realm_runtime
            self.assertIsNotNone(runtime)
            realm_workspaces = runtime.editable_workspaces.list_workspaces()
            checkout_entries = list(state.workspaces_dir.iterdir())

        self.assertFalse(capability["eligible"])
        self.assertEqual(capability["code"], "catalog_source_unpublished")
        self.assertEqual(
            capability["reason"],
            "Open this local source folder as a Workspace, then check and register "
            "the version you want to reuse.",
        )
        self.assertEqual(raised.exception.code, "catalog_source_unpublished")
        self.assertEqual(
            str(raised.exception),
            "Open this local source folder as a Workspace, then check and register "
            "the version you want to reuse.",
        )
        self.assertEqual(realm_workspaces, ())
        self.assertEqual(checkout_entries, [])

    def test_ui_default_catalog_roots_are_catalog_packages(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        roots = _default_catalog_roots(repo_root)
        state = UiState(cwd=repo_root, catalog_roots=[], run_roots=[])

        catalog = _catalog_payload(state)

        self.assertIn(repo_root / "catalog" / "production_agv_scheduling", roots)
        self.assertNotIn(repo_root / "test_catalog" / "example_package", roots)
        self.assertEqual(state.catalog_roots, roots)
        environment_ids = {item["id"] for item in catalog["environments"]}
        method_ids = {item["id"] for item in catalog["methods"]}
        # Identify Run setups the way environments and methods are identified
        # above. This used to read `label`, which is now the readable name a
        # person sees and therefore free to change; the id is the identity.
        study_ids = {item["id"] for item in catalog["studies"]}

        self.assertIn("production-agv-scheduling-smoke", environment_ids)
        self.assertIn("or-problem", environment_ids)
        self.assertIn("exhaustive-rule-grid", method_ids)
        self.assertIn("coopa-solver", method_ids)
        self.assertIn("production-agv-scheduling-smoke", study_ids)
        # test_catalog/ fixtures must never reach the user-facing catalog.
        self.assertNotIn("job-shop-rule-parameters", environment_ids)
        self.assertNotIn("fixed-rule-parameters", method_ids)
        self.assertTrue(catalog["environments"])
        self.assertTrue(catalog["methods"])
        self.assertTrue(catalog["studies"])
        self.assertTrue(catalog["resources"])

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_static_files_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(state))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                ok_response = urlopen(f"{base_url}/static/app.js", timeout=5)
                self.assertEqual(
                    ok_response.headers.get("Cache-Control"),
                    "no-store, max-age=0",
                )
                ok_response.read()
                health_response = urlopen(f"{base_url}/api/health", timeout=5)
                self.assertEqual(
                    health_response.headers.get("Cache-Control"),
                    "no-store, max-age=0",
                )
                health_response.read()
                with self.assertRaises(HTTPError) as captured:
                    urlopen(f"{base_url}/static/%2e%2e/server.py", timeout=5)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

        self.assertEqual(captured.exception.code, 404)

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_workspace_preview_proxy_strips_private_headers_only(self) -> None:
        seen_headers: List[JsonDict] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                payload = {
                    "authorization": self.headers.get("Authorization"),
                    "cookie": self.headers.get("Cookie"),
                    "x_optpilot_preview_token": self.headers.get("X-OptPilot-Preview-Token"),
                    "x_test": self.headers.get("X-Test"),
                }
                seen_headers.append(payload)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _preview_proxy_handler_factory(f"http://127.0.0.1:{upstream.server_port}", token="preview-secret", allowed_ports=[]),
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{proxy.server_port}/?__optpilot_presentation_token=preview-secret",
                headers={
                    "Authorization": "Bearer should-not-forward",
                    "Cookie": "optpilot_preview_token=preview-secret; session=private",
                    "X-OptPilot-Preview-Token": "preview-secret",
                    "X-Test": "forward-me",
                },
            )
            payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()
            proxy_thread.join(timeout=1)
            upstream_thread.join(timeout=1)

        self.assertEqual(payload["x_test"], "forward-me")
        self.assertIsNone(payload["authorization"])
        self.assertEqual(payload["cookie"], "session=private")
        self.assertIsNone(payload["x_optpilot_preview_token"])
        self.assertEqual(seen_headers, [payload])

    def test_ui_catalog_scans_user_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "devs_display_new"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text(
                "# DEVS Display Generator\n\nReusable simulation codebase for DEVS displays.\n",
                encoding="utf-8",
            )
            (resource / "tool.py").write_text("print('ready')\n", encoding="utf-8")
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])

            catalog = _catalog_payload(state)

        self.assertEqual(len(catalog["resources"]), 1)
        self.assertEqual(catalog["resources"][0]["id"], "devs-display-new")
        self.assertEqual(catalog["resources"][0]["qualified_id"], "local_package/resource/devs-display-new")
        self.assertEqual(catalog["resources"][0]["label"], "DEVS Display Generator")
        self.assertIn("simulation", catalog["resources"][0]["tags"])

    def test_ui_catalog_rejects_duplicate_ids_inside_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package = tmp_path / "catalog" / "local_package"
            first = package / "environments" / "first"
            second = package / "environments" / "second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            for path in [first / "environment.yaml", second / "environment.yaml"]:
                path.write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "optpilot.io/v1",
                            "config": "environment",
                            "id": "duplicate-env",
                            "evaluator": {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
                            "candidate": {
                                "format": "parameters",
                                "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                            },
                            "metrics": {"source": "return", "keys": ["throughput"]},
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
            state = UiState(cwd=tmp_path, catalog_roots=[package], run_roots=[])

            with self.assertRaisesRegex(ValueError, "Duplicate catalog id"):
                _catalog_payload(state)

    def test_resource_manifest_declares_launchable_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "ui_tool"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# UI Tool\n\nReusable graphical helper.\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                "\n".join(
                    [
                        "apiVersion: optpilot.io/v1",
                        "config: resource",
                        "id: ui-tool",
                        "name: UI Tool",
                        "tags: [simulation, frontend]",
                        "interface:",
                        "  label: Demo UI",
                        "  command: [python, -m, http.server, '5173', --bind, 0.0.0.0]",
                        "  runtime: {sandbox: process}",
                        "  grants: {network: disabled, envFromHost: [DEMO_UI_MODEL], secretsFromHost: [DEMO_UI_TOKEN]}",
                        "  presentation:",
                        "    kind: web",
                        "    port: 5173",
                        "    extraPorts: [8000]",
                        "    readyPath: /health",
                        "    readyTimeoutSeconds: 30",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])

            with patch.dict(
                os.environ,
                {
                    "DEMO_UI_MODEL": "openrouter/example/model",
                    "DEMO_UI_TOKEN": "never-echo-this",
                },
            ):
                catalog = _catalog_payload(state)

        self.assertEqual(len(catalog["resources"]), 1)
        entry = catalog["resources"][0]
        self.assertEqual(entry["id"], "ui-tool")
        self.assertEqual(entry["interface"]["defaultProfileId"], "default")
        profile = entry["interface"]["profiles"][0]
        self.assertEqual(profile["id"], "default")
        self.assertEqual(profile["label"], "Demo UI")
        self.assertEqual(profile["grants"]["envFromHost"], ["DEMO_UI_MODEL"])
        self.assertEqual(profile["grants"]["secretsFromHost"], ["DEMO_UI_TOKEN"])
        self.assertNotIn("openrouter/example/model", json.dumps(entry))
        self.assertNotIn("never-echo-this", json.dumps(entry))
        self.assertEqual(profile["presentation"]["port"], 5173)
        self.assertEqual(profile["presentation"]["extraPorts"], [8000])
        self.assertEqual(profile["presentation"]["readyPath"], "/health")
        self.assertEqual(profile["presentation"]["readyTimeoutSeconds"], 30)

    def test_ui_requires_profile_id_for_multiple_named_interfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "multi_ui"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# Multi UI\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "resource",
                        "id": "multi-ui",
                        "interface": {
                            "launchProfiles": [
                                {
                                    "id": "dashboard",
                                    "command": ["python", "-m", "http.server", "5173"],
                                    "presentation": {"kind": "web", "port": 5173, "readyTimeoutSeconds": 0},
                                },
                                {
                                    "id": "inspector",
                                    "command": ["python", "-m", "http.server", "5174"],
                                    "presentation": {"kind": "web", "port": 5174, "readyTimeoutSeconds": 0},
                                },
                            ]
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[tmp_path / "catalog" / "local_package"],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19170,
                ),
            )
            _stub_workspace_preview_open(state)
            entry = _catalog_payload(state)["resources"][0]

            self.assertEqual(entry["interface"]["defaultProfileId"], "")
            self.assertEqual(
                [profile["id"] for profile in entry["interface"]["profiles"]],
                ["dashboard", "inspector"],
            )
            with self.assertRaisesRegex(ValueError, "profile_id is required"):
                _start_catalog_interface_launch(state, "resource", entry["uid"])
            selected = _launch_catalog_interface(
                state,
                "resource",
                entry["uid"],
                profile_id="inspector",
            )
            state.workspace_runtime.delete(selected["runtime"]["runtime_id"])

        self.assertEqual(selected["interface"]["id"], "inspector")
        self.assertEqual(selected["interface"]["presentation"]["port"], 5174)

    def test_ui_does_not_accept_flat_legacy_interface_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "legacy_ui"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# Legacy UI\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "resource",
                        "id": "legacy-ui",
                        "interface": {
                            "command": ["python", "-m", "http.server", "5173"],
                            "port": 5173,
                            "readyPath": "/health",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[tmp_path / "catalog" / "local_package"],
                run_roots=[],
            )
            entry = _catalog_payload(state)["resources"][0]

        self.assertNotIn("interface", entry)
        self.assertIn("missing=['presentation']", entry["interface_error"])
        self.assertIn("port", entry["interface_error"])

    def test_ui_launches_catalog_resource_interface_without_copying_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "preview_tool"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# Preview Tool\n\nHas a local frontend.\n", encoding="utf-8")
            (resource / "index.html").write_text("<h1>Preview</h1>\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                "\n".join(
                    [
                        "apiVersion: optpilot.io/v1",
                        "config: resource",
                        "id: preview-tool",
                        "name: Preview Tool",
                        "interface:",
                        "  label: Preview UI",
                        "  outputs: true",
                        "  command: [python, -m, http.server, '5173', --bind, 0.0.0.0]",
                        "  runtime: {sandbox: process}",
                        "  grants: {network: disabled, secretsFromHost: [PREVIEW_TOKEN]}",
                        "  presentation:",
                        "    kind: web",
                        "    port: 5173",
                        "    extraPorts: [8000]",
                        "    readyTimeoutSeconds: 0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[tmp_path / "catalog" / "local_package"],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19180,
                ),
            )
            _stub_workspace_preview_open(state)
            resource_entry = _catalog_payload(state)["resources"][0]

            with patch.dict(os.environ, {"PREVIEW_TOKEN": "private-preview-value"}):
                launched = _launch_catalog_interface(state, "resource", resource_entry["uid"])
            calls = _fake_workspace_container_calls(tmp_path)
            runtime_id = launched["runtime"]["runtime_id"]
            runtime_root = state.workspace_runtime._workspace_runtime_dir(runtime_id)
            output_file_exists = (runtime_root / "control" / "outputs.jsonl").is_file()
            no_draft_copies = not any(state.workspaces_dir.iterdir())
            runtime_deleted = state.workspace_runtime.delete(runtime_id)
            source_exists_after_delete = resource.exists()

        self.assertEqual(launched["source"]["mode"], "read-only")
        self.assertEqual(launched["runtime"]["scope"], "launch")
        self.assertEqual(launched["runtime"]["source_mount"], "read-only")
        self.assertTrue(output_file_exists)
        self.assertTrue(no_draft_copies)
        self.assertTrue(runtime_deleted)
        self.assertTrue(source_exists_after_delete)
        self.assertEqual(launched["interface"]["id"], "default")
        self.assertEqual(launched["interface"]["presentation"]["port"], 5173)
        self.assertNotIn("private-preview-value", json.dumps(launched))
        self.assertEqual(launched["preview"]["workspace_id"], runtime_id)
        self.assertEqual(launched["preview"]["allowed_ports"], [5173, 8000])
        preview_url = urlparse(launched["preview"]["preview_url"])
        self.assertIn("__optpilot_presentation_token", parse_qs(preview_url.query))
        detached_execs = [call for call in calls if call and call[0] == "exec" and "-d" in call]
        self.assertTrue(detached_execs, calls)
        self.assertTrue(any("--env-file" in call for call in detached_execs), detached_execs)
        self.assertFalse(
            any("private-preview-value" in item for call in detached_execs for item in call),
            detached_execs,
        )
        self.assertFalse(
            any(item in {"-e", "--env"} for call in detached_execs for item in call),
            detached_execs,
        )
        self.assertTrue(
            any("$OPTPILOT_PROCESS_STDOUT" in item for call in detached_execs for item in call),
            detached_execs,
        )
        self.assertTrue(
            any("$OPTPILOT_PROCESS_EXIT_CODE" in item for call in detached_execs for item in call),
            detached_execs,
        )
        self.assertTrue(any("http.server" in " ".join(call) for call in detached_execs), detached_execs)
        run_calls = [call for call in calls if call and call[0] == "run"]
        source_mount = f"{resource.resolve()}:{resource.resolve()}:ro"
        self.assertTrue(any(source_mount in call for call in run_calls), run_calls)

    def test_ui_tracks_catalog_interface_launch_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "preview_tool"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# Preview Tool\n\nHas a local frontend.\n", encoding="utf-8")
            (resource / "index.html").write_text("<h1>Preview</h1>\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                "\n".join(
                    [
                        "apiVersion: optpilot.io/v1",
                        "config: resource",
                        "id: preview-tool",
                        "name: Preview Tool",
                        "interface:",
                        "  label: Preview UI",
                        "  command: [python, -m, http.server, '5173', --bind, 0.0.0.0]",
                        "  runtime: {sandbox: process}",
                        "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[tmp_path / "catalog" / "local_package"],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19190,
                ),
            )
            _stub_workspace_preview_open(state)
            resource_entry = _catalog_payload(state)["resources"][0]

            created = _start_catalog_interface_launch(state, "resource", resource_entry["uid"])
            launch_id = created["launch"]["launch_id"]
            for _ in range(240):
                current = _interface_launch_by_id(state, launch_id)
                if current["status"] in {"ready", "failed"}:
                    break
                time.sleep(0.05)
            else:
                self.fail("interface launch job did not finish")
            with state._lock:
                internal_handles = dict(state.interface_launches[launch_id].runtime_handles)
            public_launch_json = json.dumps(current, sort_keys=True)
            runtime_id = current["result"]["runtime"]["runtime_id"]
            runtime_root = state.workspace_runtime._workspace_runtime_dir(runtime_id)
            stopped = _stop_interface_launch(state, launch_id)

        self.assertEqual(current["status"], "ready")
        self.assertTrue(current["can_stop"])
        self.assertIn("OPTPILOT_INTERFACE_RUNTIME_ROOT", internal_handles)
        self.assertIn("OPTPILOT_INTERFACE_EPHEMERAL_ROOT", internal_handles)
        # This profile is view-only: it does not declare `outputs`, so Studio must
        # not carve out an output area or control file for it. Per
        # docs/configuration.md, `outputs: true` declares the producing capability
        # and is omitted for view-only interfaces; the opted-in side is covered by
        # test_ui_launches_catalog_resource_interface_without_copying_source.
        self.assertNotIn("OPTPILOT_INTERFACE_OUTPUT_ROOT", internal_handles)
        self.assertNotIn("OPTPILOT_INTERFACE_OUTPUTS_FILE", internal_handles)
        self.assertNotIn("OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT", internal_handles)
        self.assertNotIn("OPTPILOT_INTERFACE_VENV", internal_handles)
        ephemeral_root = internal_handles["OPTPILOT_INTERFACE_EPHEMERAL_ROOT"]
        self.assertTrue(ephemeral_root.startswith("/tmp/optpilot-interface/interface-launch-"))
        self.assertNotIn(str(tmp_path.resolve()), ephemeral_root)
        self.assertNotIn("runtime_handles", current)
        self.assertNotIn(str(tmp_path.resolve()), public_launch_json)
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(stopped["can_stop"])
        self.assertTrue(stopped["result"]["cleanup"]["cleaned"])
        self.assertFalse(runtime_root.exists())
        step_titles = [step["title"] for step in current["steps"]]
        self.assertIn("Preparing transient runtime", step_titles)
        self.assertIn("Starting isolated runtime", step_titles)
        self.assertIn("Waiting for preview port", step_titles)
        self.assertIn("Preview ready", step_titles)
        self.assertEqual(current["launch_scope"], "catalog-transient")
        self.assertEqual(current["result"]["source"]["mode"], "read-only")
        self.assertEqual(current["result"]["interface"]["id"], "default")
        self.assertEqual(current["result"]["interface"]["presentation"]["port"], 5173)

    def test_ui_catalog_interface_failure_removes_transient_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            resource = tmp_path / "catalog" / "local_package" / "resources" / "failing_preview"
            resource.mkdir(parents=True)
            (resource / "README.md").write_text("# Failing Preview\n", encoding="utf-8")
            (resource / "optpilot.resource.yaml").write_text(
                "\n".join(
                    [
                        "apiVersion: optpilot.io/v1",
                        "config: resource",
                        "id: failing-preview",
                        "interface:",
                        "  command: [python, -m, http.server, '5173']",
                        "  runtime: {sandbox: process}",
                        "  presentation: {kind: web, port: 5173, readyTimeoutSeconds: 0}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[tmp_path / "catalog" / "local_package"],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19192,
                ),
            )
            entry = _catalog_payload(state)["resources"][0]

            def fail_preview(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
                raise RuntimeError("preview broker failed")

            state.transient_workspace_preview_open = fail_preview  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "preview broker failed"):
                _launch_catalog_interface(state, "resource", entry["uid"])

            transient_runtime_dirs = list(state.runtime_dir.glob("interface-launch-*"))
            durable_workspace_entries = list(state.workspaces_dir.iterdir())
            source_exists = resource.exists()

        self.assertEqual(transient_runtime_dirs, [])
        self.assertEqual(durable_workspace_entries, [])
        self.assertTrue(source_exists)

    def test_ui_preview_readiness_fails_immediately_when_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            exit_code = root / "interface.exit-code"
            stdout = root / "interface.stdout.log"
            stderr = root / "interface.stderr.log"
            exit_code.write_text("17\n", encoding="utf-8")
            stdout.write_text("starting\n", encoding="utf-8")
            stderr.write_text("dependency install failed\n", encoding="utf-8")
            started = time.monotonic()

            readiness = _wait_for_preview_ready(
                "http://127.0.0.1:1/",
                "/",
                30,
                launch={
                    "exit_code_file": str(exit_code),
                    "stdout_log": str(stdout),
                    "stderr_log": str(stderr),
                },
            )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["processExited"])
        self.assertEqual(readiness["exitCode"], 17)
        self.assertIn("dependency install failed", readiness["error"])
        self.assertEqual(readiness["diagnostic"], "dependency install failed")

    def test_workspace_exec_hides_environment_values_from_process_arguments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(root)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            runtime = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / ".optpilot-ui" / "runtime",
                options=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                ),
            )
            workspace = {
                "id": "env-file-test",
                "root": str(workspace_root),
                "source_root": str(workspace_root),
                "mode": "editable",
                "source_type": "test",
            }
            secret = "test-secret-not-in-process-args"

            completed, _status = runtime.exec(
                workspace,
                [
                    "python3",
                    "-c",
                    "import os; print(os.environ['VISIBLE_TEST_SECRET'])",
                ],
                cwd=workspace_root,
                env={"VISIBLE_TEST_SECRET": secret},
            )
            calls = _fake_workspace_container_calls(root)
            exec_calls = [call for call in calls if call and call[0] == "exec"]
            env_files = [
                Path(call[call.index("--env-file") + 1])
                for call in exec_calls
                if "--env-file" in call
            ]
            runtime.delete(str(workspace["id"]))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), secret)
        self.assertTrue(env_files)
        self.assertTrue(all(secret not in argument for call in exec_calls for argument in call))
        self.assertTrue(all(not path.exists() for path in env_files))

    def test_ui_launch_retains_bounded_logs_after_runtime_files_disappear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            stdout = root / "interface.stdout.log"
            stderr = root / "interface.stderr.log"
            stdout.write_text(("x" * 13_000) + "tail\n", encoding="utf-8")
            stderr.write_text("failure evidence\n", encoding="utf-8")
            job = UiLaunchJob(
                launch_id="launch-test",
                kind="resource",
                uid="resource-test",
                label="Test",
                port=3000,
                log_paths={"stdout": str(stdout), "stderr": str(stderr)},
            )

            _retain_interface_launch_logs(job)
            stdout.unlink()
            stderr.unlink()
            payload = job.to_dict()

        self.assertEqual(len(payload["logs"]["stdout"]), 12_000)
        self.assertTrue(payload["logs"]["stdout"].endswith("tail\n"))
        self.assertEqual(payload["logs"]["stderr"], "failure evidence\n")

    def test_ui_startup_removes_orphaned_catalog_interface_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            runtime_manager = WorkspaceRuntimeManager(
                studio_root=tmp_path,
                runtime_root=tmp_path / ".optpilot-ui" / "runtime",
                options=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                ),
            )
            orphan_id = "interface-launch-orphan"
            orphan = runtime_manager._ensure_workspace_runtime_dir(orphan_id)
            runtime_manager._write_record(orphan_id, {"status": "running"})

            claim = StudioRuntimeSupervisorClaim.acquire(tmp_path)
            try:
                UiState(
                    cwd=tmp_path,
                    catalog_roots=[tmp_path / "catalog" / "local_package"],
                    run_roots=[],
                    workspace_runtime=WorkspaceRuntimeOptions(
                        executable=str(fake_container),
                        image="fake-code-server:latest",
                    ),
                    runtime_supervisor_claim=claim,
                )
            finally:
                claim.close()
            calls = _fake_workspace_container_calls(tmp_path)
            orphan_exists = orphan.exists()

        self.assertFalse(orphan_exists)
        self.assertTrue(
            any(
                call[:2] == ["rm", "-f"]
                and any(
                    str(item).startswith("optpilot-ws-interface-launch-orphan-")
                    for item in call[2:]
                )
                for call in calls
            ),
            calls,
        )

    def test_ui_relaunches_workspace_interface_with_fresh_runtime_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19195,
                ),
            )
            _stub_workspace_preview_open(state)
            _publish_exact_study_builder_fixture(
                state,
                include_preview_resource=True,
            )
            resource_entry = next(
                item
                for item in _catalog_payload(state)["resources"]
                if item["id"] == "preview-tool"
            )

            workspace = _open_catalog_workspace(
                state,
                "resource",
                resource_entry["uid"],
                editable=True,
                request_id="c0000000-0000-4000-8000-000000000002",
            )
            self.assertEqual(resource_entry["ref"]["source_kind"], "realm-catalog")
            self.assertEqual(workspace["ownership"], "realm-managed")
            self.assertEqual(
                workspace["catalog_origin"]["selection"]["context_digest"],
                resource_entry["ref"]["source_digest"],
            )
            workspace_id = workspace["id"]

            def launch_workspace_interface() -> tuple[str, Dict[str, Any]]:
                created = _start_workspace_interface_launch(
                    state, workspace_id, setup_policy="auto"
                )
                launch_id = str(created["launch"]["launch_id"])
                for _ in range(240):
                    current = _interface_launch_by_id(state, launch_id)
                    if current["status"] in {"ready", "failed"}:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("workspace interface launch job did not finish")
                self.assertEqual(current["status"], "ready", current)
                return launch_id, current["result"]

            first_launch_id, launched = launch_workspace_interface()
            # The editable catalog workspace is rooted at the *package*, not at the
            # selected component. The interface declares `cwd: .`, which resolves
            # against the component source root -- the same path the workspace
            # reports as catalog_origin.source_root_relative_path -- so runtime
            # setup runs there, not at the workspace root.
            component_root = (
                Path(workspace["root"])
                / workspace["catalog_origin"]["source_root_relative_path"]
            )
            setup_counter = component_root / "setup-count.txt"
            workspace_after_first_launch = _require_ui_workspace(state, workspace_id)
            first_stopped = _stop_interface_launch(state, first_launch_id)
            second_launch_id, relaunched = launch_workspace_interface()
            setup_count = setup_counter.read_text(encoding="utf-8")
            calls = _fake_workspace_container_calls(tmp_path)
            second_stopped = _stop_interface_launch(state, second_launch_id)
            deleted = _delete_ui_workspace(state, workspace_id)

        self.assertEqual(setup_count, "2")
        self.assertTrue(workspace_after_first_launch["setup"]["ran"])
        self.assertTrue(relaunched["setup"]["ran"])
        self.assertNotIn("previous", relaunched["setup"])
        self.assertNotEqual(relaunched["preview"]["workspace_id"], workspace_id)
        self.assertEqual(
            relaunched["preview"]["workspace_id"],
            relaunched["runtime"]["runtime_id"],
        )
        self.assertNotEqual(
            launched["runtime"]["runtime_id"],
            relaunched["runtime"]["runtime_id"],
        )
        self.assertEqual(first_stopped["status"], "stopped")
        self.assertEqual(second_stopped["status"], "stopped")
        detached_execs = [call for call in calls if call and call[0] == "exec" and "-d" in call]
        self.assertGreaterEqual(len(detached_execs), 2, calls)
        self.assertTrue(deleted["files_deleted"])

    def test_public_config_schema_allows_environment_and_method_interfaces(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        environment = yaml.safe_load((repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml").read_text(encoding="utf-8"))
        environment["interface"] = {
            "command": ["python", "-m", "http.server", "5173", "--bind", "0.0.0.0"],
            "grants": {
                "envFromHost": [],
                "network": "disabled",
                "secretsFromHost": [],
            },
            "presentation": {
                "kind": "web",
                "port": 5173,
                "readyPath": "/",
                "readyTimeoutSeconds": 10,
            },
            "accepts": {"selectionKinds": ["candidate", "trial"]},
        }
        method = yaml.safe_load((repo_root / "tests" / "fixtures" / "catalog" / "methods" / "fixed_parameter_method.yaml").read_text(encoding="utf-8"))
        method["interface"] = {
            "command": ["python", "-m", "http.server", "5174", "--bind", "0.0.0.0"],
            "presentation": {"kind": "web", "port": 5174},
            "accepts": {"selectionKinds": ["workspace"]},
        }

        self.assertTrue(validate_public_config_schema(environment).valid)
        self.assertTrue(validate_public_config_schema(method).valid)

    def test_ui_compatibility_payload_and_study_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            environment, method, incompatible_method = (
                _publish_exact_study_builder_fixture(state)
            )

            compatibility = _compatibility_payload(state)
            toy_pair = next(
                item
                for item in compatibility["pairs"]
                if item["environment"]["uid"] == environment["uid"]
                and item["method"]["uid"] == method["uid"]
            )

            self.assertTrue(toy_pair["compatible"], toy_pair)

            base_payload = {
                "environment_ref": environment["ref"],
                "method_ref": method["ref"],
            }
            draft = _draft_study(
                state,
                {
                    **base_payload,
                    "request_id": "d0000000-0000-4000-8000-000000000001",
                    "name": "ui-draft-toy",
                    "description": "Draft created through the full Studio study form.",
                    "tags": ["ui", "draft"],
                    "metric": "score",
                    "direction": "maximize",
                    "aggregation": "mean",
                    "secondaryMetrics": ["cost"],
                    "maxTrials": 1,
                    "maxWallClockSeconds": 3600,
                    "maxFailures": 2,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                    "maxRetries": 1,
                    "evidenceLevel": "full",
                    "seed": 123,
                },
            )

            self.assertTrue(draft["validation"]["valid"], draft)
            self.assertEqual(
                set(draft),
                {
                    "draft_id",
                    "draft_revision",
                    "saved_as_draft",
                    "workspace_id",
                    "workspace_revision",
                    "study_relative_path",
                    "draft",
                    "yaml",
                    "compatibility",
                    "validation",
                },
            )
            self.assertGreaterEqual(draft["workspace_revision"], 2)
            workspace = _require_ui_workspace(state, draft["workspace_id"])
            study_path = Path(workspace["root"]) / draft["study_relative_path"]
            self.assertTrue(study_path.is_file())
            self.assertEqual(workspace["ownership"], "realm-managed")
            environment_origin = next(
                component
                for component in workspace["catalog_origin"]["components"]
                if component["kind"] == "environment"
            )
            self.assertEqual(environment_origin["ref"], environment["ref"])
            self.assertNotIn(str(tmp_path), json.dumps(draft, sort_keys=True))
            draft_doc = draft["draft"]
            self.assertEqual(draft_doc["name"], "ui-draft-toy")
            self.assertEqual(draft_doc["description"], "Draft created through the full Studio study form.")
            self.assertEqual(draft_doc["tags"], ["ui", "draft"])
            self.assertEqual(draft_doc["objective"]["secondaryMetrics"], ["cost"])
            self.assertEqual(draft_doc["budget"]["maxWallClockSeconds"], 3600)
            self.assertEqual(draft_doc["budget"]["maxFailures"], 2)
            self.assertEqual(draft_doc["execution"]["retry"], {"maxRetries": 1})
            self.assertEqual(draft_doc["evidence"], {"level": "full"})
            self.assertEqual(draft_doc["reproducibility"], {"seed": 123})
            for legacy_field, value in (
                ("outputFileStorage", "copy"),
                ("outputDir", "runs/ui-draft-toy"),
            ):
                with self.subTest(legacy_evidence_field=legacy_field):
                    legacy_draft = deepcopy(draft_doc)
                    legacy_draft["evidence"][legacy_field] = value
                    self.assertFalse(validate_public_config_schema(legacy_draft).valid)
            for legacy_field, value in (
                ("evidenceStorage", "copy"),
                ("evidenceOutputDir", "runs/ui-draft-toy"),
            ):
                with self.subTest(legacy_studio_field=legacy_field):
                    with self.assertRaisesRegex(ValueError, "managed Realm retention"):
                        _draft_study(
                            state,
                            {
                                **base_payload,
                                "request_id": (
                                    "d0000000-0000-4000-8000-000000000003"
                                    if legacy_field == "evidenceStorage"
                                    else "d0000000-0000-4000-8000-000000000004"
                                ),
                                legacy_field: value,
                            },
                        )
            no_failure_limit_draft = _draft_study(
                state,
                {
                    **base_payload,
                    "request_id": "d0000000-0000-4000-8000-000000000002",
                    "name": "ui-draft-no-failure-limit",
                    "metric": "score",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "maxFailures": 0,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                },
            )
            self.assertTrue(no_failure_limit_draft["validation"]["valid"], no_failure_limit_draft)
            self.assertNotIn("maxFailures", no_failure_limit_draft["draft"]["budget"])

            incompatible_draft = _draft_study(
                state,
                {
                    "request_id": "d1111111-1111-4111-8111-111111111111",
                    "environment_ref": environment["ref"],
                    "method_ref": incompatible_method["ref"],
                    "name": "incompatible-draft",
                    "metric": "score",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                },
            )
            self.assertFalse(incompatible_draft["compatibility"]["compatible"])
            self.assertFalse(incompatible_draft["validation"]["valid"])
            self.assertIn(
                "is incompatible",
                " ".join(incompatible_draft["validation"]["errors"]),
            )
            self.assertTrue(incompatible_draft["compatibility"]["reasons"])

    def test_ui_study_plan_workspace_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            environment, method, _incompatible = (
                _publish_exact_study_builder_fixture(state)
            )

            workspace = _open_study_workspace(
                state,
                {
                    "request_id": "d2222222-2222-4222-8222-222222222222",
                    "environment_ref": environment["ref"],
                    "method_ref": method["ref"],
                    "name": "ui-study-workspace",
                    "metric": "score",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "parallelism": 1,
                    "save_as_draft": True,
                    "draft_action_id": "d2222222-2222-4222-8222-222222222223",
                },
            )
            root = Path(workspace["root"])
            # Study drafts have their own Studies-page collection. Their
            # backing checkout is durable but intentionally does not clutter
            # the user's general Workspaces list.
            indexed = _list_ui_workspaces(state, include_support=True)
            persisted = next(
                item for item in indexed if item["id"] == workspace["id"]
            )

            self.assertEqual(workspace["mode"], "editable")
            self.assertEqual(workspace["ownership"], "realm-managed")
            self.assertEqual(workspace["catalog_origin"]["kind"], "study-builder")
            self.assertFalse(workspace["visible_in_workspaces"])
            study_path = root / workspace["catalog_origin"]["study_relative_path"]
            self.assertTrue(study_path.is_file())
            self.assertIn(
                "ui-study-workspace", study_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                workspace["realm_workspace_revision"],
                persisted["realm_workspace_revision"],
            )

    def test_ui_package_plan_registers_resource_and_updates_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Reusable Tool",
                    "description": "Resource draft",
                    "focus_paths": ["README.md"],
                },
            )
            plan = _prepare_package_plan(
                state,
                workspace["id"],
                {"kind": "resource", "resource_id": "reusable-tool"},
            )["package_plan"]
            applied = _apply_package_plan(state, workspace["id"], plan["id"])

            destination = tmp_path / "catalog" / "local_package" / "resources" / "reusable-tool"
            catalog = _catalog_payload(state)
            indexed = _list_ui_workspaces(state)

            self.assertTrue(applied["applied"])
            # Registering writes the package into an editable folder.
            self.assertTrue(destination.exists(), destination)
            written = sorted(p for p in destination.rglob("*") if p.is_file())
            self.assertTrue(written, destination)
            written[0].write_text(written[0].read_text() + "\n# edited\n")
            entry = next(
                entry
                for entry in catalog["resources"]
                if entry["id"] == "reusable-tool"
            )
            self.assertEqual(entry["ref"]["schema"], "optpilot.catalog-entry-ref.v1")
            self.assertEqual(entry["ref"]["source_kind"], "realm-catalog")
            self.assertEqual(
                entry["ref"]["source_revision"],
                applied["catalog"]["head"]["revision"],
            )
            self.assertEqual(
                applied["package_plan"]["publication"]["published_head"],
                applied["catalog"]["head"],
            )
            self.assertEqual(applied["catalog"]["realization"]["status"], "ready")
            self.assertTrue(any(entry["kind"] == "resource" for entry in applied["workspace"]["registered_entries"]))
            self.assertTrue(any(item["id"] == workspace["id"] and item["registered_entries"] for item in indexed))

    def test_ui_package_plan_discovers_configs_inside_managed_workspace(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Draft Workspace"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "toy_factory"
            method_dir = root / "optpilot_configs" / "methods" / "reference_random_search"
            study_dir = root / "optpilot_configs" / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            shutil.copyfile(
                repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml",
                env_dir / "environment.yaml",
            )
            shutil.copyfile(
                repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml",
                method_dir / "method.yaml",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "toy-smoke",
                        "environmentConfig": "../environments/toy_factory/environment.yaml",
                        "methodConfig": "../methods/reference_random_search/method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            discovered = _discover_workspace_configs(state, workspace["id"])
            plan = _prepare_package_plan(state, workspace["id"], {})["package_plan"]

        configs = discovered["configs"]
        self.assertEqual(
            [(item["kind"], item["relative_path"], item["valid"]) for item in configs],
            [
                ("environment", "optpilot_configs/environments/toy_factory/environment.yaml", True),
                ("method", "optpilot_configs/methods/reference_random_search/method.yaml", True),
                ("study", "optpilot_configs/studies/smoke.yaml", True),
            ],
        )
        self.assertEqual(len(plan["components"]), 2)
        self.assertEqual(len(plan["studies"]), 1)

    def test_ui_package_plan_normalizes_component_config_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "External Project"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "factory"
            method_dir = root / "optpilot_configs" / "methods" / "fixed"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "prompt.md").write_text("Use the local evaluator.", encoding="utf-8")
            (method_dir / "method.py").write_text(
                "class FixedMethod:\n"
                "    def propose(self, context, count):\n"
                "        return []\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "factory-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "methodContext": {"instructions": ["prompt.md"]},
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "fixed-method",
                        "entrypoint": {"python": "method:FixedMethod", "pythonPath": ["."]},
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            plan = _prepare_package_plan(state, workspace["id"], {})[
                "package_plan"
            ]
            validated = _validate_package_plan(
                state, workspace["id"], plan["id"]
            )["package_plan"]

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertTrue(validated["artifact"]["content_ref"].startswith("tree:sha256:"))
        self.assertEqual(
            validated["validation"]["artifact_ref"],
            validated["artifact"]["content_ref"],
        )

    def test_ui_package_plan_validates_smokes_and_applies_pair(self) -> None:
        self._require_retained_worker_transport()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "External Pair"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "toy"
            method_dir = root / "optpilot_configs" / "methods" / "random"
            study_dir = root / "optpilot_configs" / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': float(candidate_runtime.get('x', 0))}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "description": "Test candidate.",
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            _write_retained_fixed_method(method_dir)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    _retained_fixed_method_config(),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "toy-smoke",
                        "environmentConfig": "../environments/toy/environment.yaml",
                        "methodConfig": "../methods/random/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], prepared["id"])["package_plan"]
            blocked = _apply_package_plan(state, workspace["id"], prepared["id"])
            smoke = _smoke_package_plan(state, workspace["id"], prepared["id"], {"max_trials": 1, "timeout_seconds": 120})["smoke"]
            smoke_by_id = _smoke_package_plan(
                state,
                workspace["id"],
                prepared["id"],
                {"study": "toy-smoke", "max_trials": 1, "timeout_seconds": 120},
            )["smoke"]
            applied = _apply_package_plan(state, workspace["id"], prepared["id"])
            package_root = tmp_path / "catalog" / "local_package"
            catalog = _catalog_payload(state)
            catalog_study = next(
                item for item in catalog["studies"] if item["id"] == "toy-smoke"
            )
            study_detail = _catalog_detail(state, "study", catalog_study["uid"])

        self.assertEqual(prepared["classification"], "environment-plus-method")
        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertEqual(validated["readiness"], "component-ready")
        self.assertFalse(blocked["applied"], blocked)
        self.assertTrue(smoke["valid"], smoke)
        self.assertTrue(smoke_by_id["valid"], smoke_by_id)
        self.assertTrue(smoke_by_id["study"].endswith("studies/toy-smoke.yaml"), smoke_by_id)
        self.assertTrue(applied["applied"])
        self.assertFalse(package_root.exists())
        self.assertEqual(
            study_detail["config"]["environmentConfig"],
            "../environments/toy-env/environment.yaml",
        )
        self.assertEqual(
            study_detail["config"]["methodConfig"],
            "../methods/fixed-method/method.yaml",
        )
        self.assertEqual(
            study_detail["entry"]["summary"]["environmentRef"]["source_revision"],
            applied["catalog"]["head"]["revision"],
        )
        self.assertEqual(
            study_detail["entry"]["summary"]["methodRef"]["source_digest"],
            applied["catalog"]["head"]["manifest_digest"],
        )

    def test_ui_package_plan_smoke_rejects_non_reproducible_host_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Secret Smoke"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "secret"
            method_dir = root / "optpilot_configs" / "methods" / "random"
            study_dir = root / "optpilot_configs" / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "import os\n\n"
                "def evaluate(candidate_runtime, context):\n"
                "    if os.environ.get('CURATION_SMOKE_TOKEN') != 'secret-value':\n"
                "        raise RuntimeError('missing curation smoke token')\n"
                "    return {'status': 'success', 'metric_values': {'score': 1.0}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "secret-env",
                        "runtime": {"sandbox": "process", "envFromHost": ["CURATION_SMOKE_TOKEN"]},
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "description": "Test candidate.",
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            _write_retained_fixed_method(method_dir)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    _retained_fixed_method_config(),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "secret-smoke",
                        "environmentConfig": "../environments/secret/environment.yaml",
                        "methodConfig": "../methods/random/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            # Smoke tests no longer ask by default; this case is about what
            # an *approved* smoke run refuses, so ask for the approval.
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    "permissions": {"smoke_test": "approval_required"},
                },
            )
            session = _create_agent_session(state, {"title": "Secret smoke"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)
            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], prepared["id"])["package_plan"]
            requested = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_smoke",
                {"workspace_id": workspace["id"], "plan_id": prepared["id"], "max_trials": 1, "timeout_seconds": 120},
            )
            approvals = _read_agent_approvals(state, session["id"])
            missing = _approve_agent_action(state, session["id"], approvals[0]["id"])["result"]
            state.settings_path.parent.mkdir(parents=True, exist_ok=True)
            state.settings_path.write_text(
                json.dumps({"environment": {"variables": {"CURATION_SMOKE_TOKEN": "secret-value"}}}),
                encoding="utf-8",
            )
            smoke = _smoke_package_plan(state, workspace["id"], prepared["id"], {"max_trials": 1, "timeout_seconds": 120})["smoke"]

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertTrue(requested["data"]["approval_required"])
        self.assertFalse(missing["ok"], missing)
        self.assertIn(
            "environment.runtime.envFromHost is not a reproducible process-runtime input",
            missing["summary"],
        )
        self.assertIn("Repair the failing config", missing["summary"])
        self.assertFalse(smoke["valid"], smoke)
        self.assertIn(
            "environment.runtime.envFromHost is not a reproducible process-runtime input",
            " ".join(smoke["errors"]),
        )

    def test_ui_package_plan_materializes_source_hints_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Opaque Source Hint"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "factory"
            env_dir.mkdir(parents=True)
            (root / "src").mkdir()
            (root / "src" / "layout.yml").write_text("layout: demo\n", encoding="utf-8")
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "factory-env",
                        "evaluator": {
                            "python": "evaluator:evaluate",
                            "pythonPath": ["."],
                            "settings": {"layoutConfig": "src/layout.yml"},
                        },
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            target = prepared["components"][0]
            updated = _update_package_plan(
                state,
                workspace["id"],
                prepared["id"],
                {
                    "components": [
                        {
                            "target_id": target["target_id"],
                            "source_hints": [{"path": "src/layout.yml", "reason": "opaque evaluator setting"}],
                            "path_rewrites": [{"from": "src/layout.yml", "to": "data/layout.yml"}],
                        }
                    ]
                },
            )["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], updated["id"])["package_plan"]
            applied = _apply_package_plan(state, workspace["id"], updated["id"])
            package_root = tmp_path / "catalog" / "local_package"
            entry = next(
                item
                for item in _catalog_payload(state)["environments"]
                if item["id"] == "factory-env"
            )
            inspection = _open_catalog_workspace(
                state,
                "environment",
                entry["uid"],
                editable=False,
            )
            projected_hint = Path(inspection["root"]) / "data" / "layout.yml"
            projected_hint_content = projected_hint.read_text(encoding="utf-8")

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertTrue(applied["applied"], applied)
        self.assertFalse(package_root.exists())
        self.assertEqual(projected_hint_content, "layout: demo\n")
        self.assertEqual(entry["ref"]["source_kind"], "realm-catalog")

    def test_ui_package_plan_resource_only_publication_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Reference Notes"})
            root = Path(workspace["root"])
            (root / "README.md").write_text("Useful notes", encoding="utf-8")

            prepared = _prepare_package_plan(
                state,
                workspace["id"],
                {"kind": "resource", "resource_id": "reference-notes"},
            )["package_plan"]
            first = _apply_package_plan(state, workspace["id"], prepared["id"])
            repeated = _apply_package_plan(state, workspace["id"], prepared["id"])
            destination = tmp_path / "catalog" / "local_package" / "resources" / "reference-notes"
            entry = next(
                item
                for item in _catalog_payload(state)["resources"]
                if item["id"] == "reference-notes"
            )

        self.assertEqual(prepared["classification"], "resource-only")
        self.assertTrue(first["applied"])
        self.assertTrue(repeated["applied"])
        self.assertEqual(first["catalog"]["head"], repeated["catalog"]["head"])
        self.assertFalse(destination.exists())
        self.assertEqual(entry["ref"]["source_kind"], "realm-catalog")
        self.assertEqual(
            entry["ref"]["source_digest"],
            first["catalog"]["head"]["manifest_digest"],
        )

    def test_ui_agent_package_plan_tools_require_approval_for_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Resource Draft"})
            Path(workspace["root"], "README.md").write_text("Draft", encoding="utf-8")
            session = _create_agent_session(state, {"title": "Package plan"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            prepared = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_prepare",
                {"workspace_id": workspace["id"], "kind": "resource", "resource_id": "resource-draft"},
            )
            plan_id = prepared["data"]["package_plan"]["id"]
            approval = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_apply",
                {"workspace_id": workspace["id"], "plan_id": plan_id},
            )

        self.assertTrue(prepared["ok"], prepared)
        self.assertFalse(approval["ok"], approval)
        self.assertTrue(approval["data"]["approval_required"])

    def test_ui_agent_config_validate_summary_names_repairable_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Missing Evaluator"})
            root = Path(workspace["root"])
            config_dir = root / "optpilot_configs" / "environments" / "broken"
            config_dir.mkdir(parents=True)
            (config_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            session = _create_agent_session(state, {"title": "Validate config"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_config_validate",
                {"workspace_id": workspace["id"], "path": "optpilot_configs/environments/broken/environment.yaml"},
            )

        self.assertFalse(result["ok"], result)
        self.assertIn("Config validation failed:", result["summary"])
        self.assertIn("id", result["summary"])
        self.assertIn("Repair", result["summary"])

    def test_ui_agent_relative_config_paths_default_to_selected_workspace_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Selected Workspace"})
            root = Path(workspace["root"])
            config_path = root / "optpilot_configs" / "methods" / "method.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("config: method\n", encoding="utf-8")
            session = _create_agent_session(state, {"title": "Resolve config"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            resolved = _resolve_agent_or_allowed_path(
                state,
                session["id"],
                {"path": "optpilot_configs/methods/method.yaml"},
            )

        self.assertEqual(resolved, config_path.resolve())

    def test_ui_agent_package_plan_validate_summary_names_missing_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Missing Adapters"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "factory"
            method_dir = root / "optpilot_configs" / "methods" / "weighted"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "factory-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"weight": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "weighted-method",
                        "entrypoint": {"python": "method:WeightedMethod", "pythonPath": ["."], "protocol": "batch"},
                        "accepts": {"formats": ["parameters"], "requires": {"context": []}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            session = _create_agent_session(state, {"title": "Validate package"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)
            prepared = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_prepare",
                {"workspace_id": workspace["id"]},
            )
            plan_id = prepared["data"]["package_plan"]["id"]

            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_validate",
                {"workspace_id": workspace["id"], "plan_id": plan_id},
            )

        self.assertFalse(result["ok"], result)
        self.assertIn("Package plan validation failed:", result["summary"])
        self.assertIn("evaluator", result["summary"])
        self.assertIn("Repair missing adapters", result["summary"])

    def test_ui_agent_package_plan_summary_prompts_smoke_study_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Component Ready Pair"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "toy"
            method_dir = root / "optpilot_configs" / "methods" / "random"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': float(candidate_runtime.get('x', 0))}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "random-method",
                        "entrypoint": {"python": "optpilot.methods:ReferenceRandomSearchMethod", "protocol": "batch"},
                        "settings": {"batchSize": 1},
                        "accepts": {"formats": ["parameters"], "requires": {"context": ["candidate.parameters.schema"]}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            session = _create_agent_session(state, {"title": "Validate package"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)
            prepared = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_prepare",
                {"workspace_id": workspace["id"]},
            )
            plan_id = prepared["data"]["package_plan"]["id"]

            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_validate",
                {"workspace_id": workspace["id"], "plan_id": plan_id},
            )

        self.assertTrue(result["ok"], result)
        self.assertIn("readiness is component-ready", result["summary"])
        self.assertIn("Next draft a minimal smoke study", result["summary"])

    def test_ui_package_plan_static_validation_defers_method_signature_execution_to_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Bad Method Signature"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "toy"
            method_dir = root / "optpilot_configs" / "methods" / "bad"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1.0}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (method_dir / "method.py").write_text(
                "class BadMethod:\n"
                "    def __init__(self, settings):\n"
                "        self.settings = settings\n"
                "    def propose(self, n_candidates, study_state):\n"
                "        return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "bad-method",
                        "entrypoint": {"python": "method:BadMethod", "pythonPath": ["."], "protocol": "batch"},
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], prepared["id"])["package_plan"]

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertEqual(validated["status"], "validated")
        methods = validated["validation"]["capabilities"]["retained_execution"][
            "methods"
        ]
        self.assertEqual(len(methods), 1)
        self.assertEqual(methods[0]["code"], "method_callable_unchecked")
        self.assertTrue(methods[0]["smoke_eligible"])

    def test_ui_package_plan_validation_catches_missing_local_source_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Missing Source Closure"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "factory"
            env_dir.mkdir(parents=True)
            (root / "src").mkdir()
            (root / "src" / "factory_core.py").write_text("class Factory: pass\n", encoding="utf-8")
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    from src.factory_core import Factory\n"
                "    _factory = Factory()\n"
                "    return {'status': 'success', 'metric_values': {'score': 1.0}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "factory-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], prepared["id"])["package_plan"]

        self.assertFalse(validated["validation"]["valid"], validated)
        self.assertIn("local import 'src.factory_core'", " ".join(validated["validation"]["errors"]))
        self.assertIn("source_hints", " ".join(validated["validation"]["errors"]))

    def test_ui_workspace_patterns_reject_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "workspace"
            root.mkdir()
            secret = Path(tmp_dir) / "secret.txt"
            secret.write_text("secret", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not.*traverse|escapes"):
                _match_workspace_pattern(root, "../secret.txt")

            link = root / "linked-secret.txt"
            try:
                link.symlink_to(secret)
            except (OSError, NotImplementedError):
                return
            with self.assertRaisesRegex(ValueError, "symlink"):
                _match_workspace_pattern(root, "linked-secret.txt")

    def test_ui_package_plan_update_rejects_escaping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Unsafe package plan"})
            workspace_root = Path(workspace["root"])
            (workspace_root / "README.md").write_text("# Resource\n", encoding="utf-8")
            plan = _prepare_package_plan(
                state,
                workspace["id"],
                {"kind": "resource", "resource_id": "unsafe-resource"},
            )["package_plan"]
            target_id = plan["resources"][0]["target_id"]

            with self.assertRaisesRegex(ValueError, "traversal"):
                _update_package_plan(
                    state,
                    workspace["id"],
                    plan["id"],
                    {
                        "resources": [
                            {
                                "target_id": target_id,
                                "path_rewrites": [
                                    {"from": "README.md", "to": "../../escaped.txt"}
                                ],
                            }
                        ]
                    },
                )

            with self.assertRaisesRegex(ValueError, "traverse"):
                _update_package_plan(
                    state,
                    workspace["id"],
                    plan["id"],
                    {
                        "resources": [
                            {
                                "target_id": target_id,
                                "include": ["../secret.txt"],
                            }
                        ]
                    },
                )

            with self.assertRaisesRegex(ValueError, "absolute"):
                _update_package_plan(
                    state,
                    workspace["id"],
                    plan["id"],
                    {
                        "resources": [
                            {
                                "target_id": target_id,
                                "include": ["/etc/passwd"],
                            }
                        ]
                    },
                )

            with self.assertRaisesRegex(ValueError, "absolute"):
                _update_package_plan(
                    state,
                    workspace["id"],
                    plan["id"],
                    {
                        "resources": [
                            {
                                "target_id": target_id,
                                "path_rewrites": [
                                    {"from": "README.md", "to": "C:/outside.txt"}
                                ],
                            }
                        ]
                    },
                )

    def test_ui_package_plan_ids_cannot_escape_workspace_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Artifact IDs"})
            workspace_root = Path(workspace["root"])
            (workspace_root / "README.md").write_text("# Resource\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "path component"):
                _prepare_package_plan(
                    state,
                    workspace["id"],
                    {"id": "../../escaped-plan", "kind": "resource", "resource_id": "demo"},
                )
            self.assertFalse((root / "escaped-plan.json").exists())

    def test_ui_persisted_package_plan_revalidates_root_and_rewrite_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Tampered plan"})
            workspace_root = Path(workspace["root"])
            (workspace_root / "README.md").write_text("# Resource\n", encoding="utf-8")
            plan = _prepare_package_plan(
                state,
                workspace["id"],
                {"kind": "resource", "resource_id": "demo"},
            )["package_plan"]
            plan_path = state.workspaces_dir / workspace["id"] / "package_plans" / f"{plan['id']}.json"

            tampered = json.loads(plan_path.read_text(encoding="utf-8"))
            tampered["resources"][0]["path_rewrites"] = [
                {"from": "not-included.txt", "to": "../../escaped.txt"}
            ]
            plan_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "traversal"):
                _validate_package_plan(state, workspace["id"], plan["id"])

            external = root / "external"
            external.mkdir()
            (external / "README.md").write_text("secret\n", encoding="utf-8")
            tampered = deepcopy(plan)
            tampered["source_root"] = str(external)
            plan_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "no longer matches"):
                _validate_package_plan(state, workspace["id"], plan["id"])

            self.assertFalse((root / "catalog" / "escaped.txt").exists())

    def test_ui_study_selection_and_registered_paths_stay_workspace_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            package_root = root / "package"
            studies = package_root / "studies"
            studies.mkdir(parents=True)
            safe_study = studies / "safe.yaml"
            safe_study.write_text("config: study\n", encoding="utf-8")
            plan = {
                "studies": [
                    {
                        "id": "safe",
                        "registered_config_path": "studies/safe.yaml",
                        "smoke": True,
                    }
                ]
            }
            secret = root / "secret.yaml"
            secret.write_text("secret: true\n", encoding="utf-8")

            self.assertEqual(_select_plan_study(package_root, plan, "safe"), safe_study.resolve())
            with self.assertRaisesRegex(ValueError, "absolute|traversal"):
                _select_plan_study(package_root, plan, str(secret))
            with self.assertRaisesRegex(ValueError, "traversal"):
                _select_plan_study(package_root, plan, "../secret.yaml")

            linked_study = studies / "linked.yaml"
            try:
                linked_study.symlink_to(secret)
            except (OSError, NotImplementedError):
                linked_study = None
            if linked_study is not None:
                with self.assertRaisesRegex(ValueError, "escapes|symlink"):
                    _select_plan_study(package_root, plan, "studies/linked.yaml")

            workspace_root = root / "workspace"
            workspace_root.mkdir()
            config_source = workspace_root / "environment.yaml"
            config_source.write_text("config: environment\n", encoding="utf-8")
            assets = workspace_root / "assets"
            assets.mkdir()
            (assets / "local.txt").write_text("local\n", encoding="utf-8")
            destination = package_root / "environments" / "demo"
            self.assertEqual(
                _registered_path_value(
                    "assets/local.txt",
                    workspace_root,
                    config_source,
                    destination,
                    None,
                ),
                "assets/local.txt",
            )
            with self.assertRaisesRegex(ValueError, "escapes"):
                _registered_path_value(
                    str(secret),
                    workspace_root,
                    config_source,
                    destination,
                    None,
                )
            with self.assertRaisesRegex(ValueError, "escapes"):
                _registered_path_value(
                    "../secret.yaml",
                    workspace_root,
                    config_source,
                    destination,
                    None,
                )

    def test_ui_package_plan_smoke_rejects_zero_exit_run_with_failed_trials(self) -> None:
        self._require_retained_worker_transport()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Failing Smoke"})
            root = Path(workspace["root"])
            env_dir = root / "optpilot_configs" / "environments" / "failing"
            method_dir = root / "optpilot_configs" / "methods" / "random"
            study_dir = root / "optpilot_configs" / "studies"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            study_dir.mkdir(parents=True)
            (env_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    raise RuntimeError('intentional evaluator failure')\n",
                encoding="utf-8",
            )
            (env_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "failing-env",
                        "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
                        "candidate": {
                            "description": "Test candidate.",
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            _write_retained_fixed_method(method_dir)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    _retained_fixed_method_config(),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (study_dir / "smoke.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "failing-smoke",
                        "environmentConfig": "../environments/failing/environment.yaml",
                        "methodConfig": "../methods/random/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})["package_plan"]
            validated = _validate_package_plan(state, workspace["id"], prepared["id"])["package_plan"]
            smoke = _smoke_package_plan(state, workspace["id"], prepared["id"], {"max_trials": 1, "timeout_seconds": 120})["smoke"]

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertFalse(smoke["valid"], smoke)
        self.assertIn("final_logical_failures=1", " ".join(smoke["errors"]))
        self.assertEqual(smoke["summary"]["run_status"], "failed")
        self.assertEqual(smoke["summary"]["stop_code"], "no_successful_observation")
        self.assertEqual(
            smoke["summary"]["counts"]["logical_trials"]["final_failures"],
            1,
        )

    def test_ui_agent_study_draft_uses_exact_catalog_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            environment, method, _incompatible = (
                _publish_exact_study_builder_fixture(state)
            )
            session = _create_agent_session(state, {"title": "Draft smoke study"})
            arguments = {
                "_openhands_tool_call_id": "call-study-draft-1",
                "environment_ref": environment["ref"],
                "method_ref": method["ref"],
                "name": "toy-smoke",
                "metric": "score",
                "direction": "maximize",
                "maxTrials": 1,
                "timeoutSeconds": 30,
            }
            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_study_draft",
                arguments,
            )
            replay = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_study_draft",
                arguments,
            )
            independent = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_study_draft",
                {**arguments, "_openhands_tool_call_id": "call-study-draft-2"},
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["validation"]["valid"], result)
        self.assertEqual(result["data"]["draft"]["name"], "toy-smoke")
        self.assertTrue(result["data"]["workspace_id"])
        self.assertEqual(
            replay["data"]["workspace_id"], result["data"]["workspace_id"]
        )
        self.assertEqual(
            replay["data"]["workspace_revision"],
            result["data"]["workspace_revision"],
        )
        self.assertNotEqual(
            independent["data"]["workspace_id"], result["data"]["workspace_id"]
        )
        self.assertGreaterEqual(result["data"]["workspace_revision"], 2)
        self.assertNotIn("path", result["data"])

    def test_ui_agent_sessions_persist_workspace_context_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace_root = tmp_path / "scratch"

            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Scratch tool workspace",
                    "root": str(workspace_root),
                    "source_type": "tool",
                    "description": "Local codebase used as an agent add-on.",
                    "focus_paths": ["README.md"],
                },
            )
            session = _create_agent_session(state, {"title": "Design session", "description": "Catalog work"})
            attached = _attach_agent_workspace(state, session["id"], workspace["id"], select=True)
            message_result = _append_agent_message(
                state,
                session["id"],
                {
                    "role": "user",
                    "title": "User",
                    "content": "Inspect this workspace and prepare registration.",
                    "ui_context": {
                        "current_page": "catalog",
                        "selected_catalog_entry": {"kind": "environment", "id": "toy-factory", "path": "toy_factory.yaml"},
                        "selected_study_plan": {"id": "plan-1", "title": "Toy plan"},
                        "selected_run": {"id": "run-1", "name": "Toy run"},
                        "code_editor": {"status": "ready", "folder": str(workspace_root)},
                        "registration_menu": {"status": "draft", "selected_configs": [{"path": "environment.yaml"}]},
                    },
                },
            )
            tool_message = _append_agent_message(
                state,
                session["id"],
                {
                    "role": "tool",
                    "title": "Workspace detached",
                    "content": "Scratch tool workspace was detached from this assistant session.",
                    "source": "studio_ui",
                    "memory_scope": "ui_history",
                },
            )
            studio_status = _append_agent_message(
                state,
                session["id"],
                {
                    "role": "assistant",
                    "title": "Registration opened",
                    "content": "Prepared catalog registration for Scratch tool workspace.",
                    "source": "studio_ui",
                    "memory_scope": "ui_history",
                },
            )
            detached = _detach_agent_workspace(state, session["id"], workspace["id"])

            reloaded = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            sessions = _list_agent_sessions(reloaded)
            persisted = next(item for item in sessions if item["id"] == session["id"])
            response_texts = _assistant_response_texts(reloaded, session["id"])

        self.assertEqual(attached["selected_workspace_id"], workspace["id"])
        # The Assistant is not configured here, so nothing was dispatched and
        # nothing will arrive. The turn is therefore over. This used to assert
        # "waiting_for_agent", which is how conversations came to show
        # "Working" forever, surviving restarts, with no one on the other end.
        self.assertEqual(message_result["session"]["status"], "idle")
        self.assertEqual(
            message_result["message"]["context"]["selected_workspace"]["id"],
            workspace["id"],
        )
        self.assertEqual(message_result["message"]["context"]["current_page"], "catalog")
        self.assertEqual(message_result["message"]["context"]["selected_catalog_entry"]["id"], "toy-factory")
        self.assertIsNone(message_result["message"]["context"]["selected_study_plan"])
        self.assertIsNone(message_result["message"]["context"]["selected_run"])
        self.assertIsNone(message_result["message"]["context"]["code_editor"])
        self.assertIsNone(message_result["message"]["context"]["registration_menu"])
        self.assertEqual(message_result["message"]["context"]["runtime"]["runtime"], "openhands")
        self.assertIn("optpilot_workspace_list", message_result["message"]["context"]["available_tools"])
        self.assertEqual(tool_message["message"]["role"], "tool")
        self.assertEqual(tool_message["message"]["source"], "studio_ui")
        self.assertEqual(tool_message["message"]["memory_scope"], "ui_history")
        self.assertEqual(studio_status["message"]["source"], "studio_ui")
        self.assertEqual(studio_status["message"]["memory_scope"], "ui_history")
        self.assertEqual(detached["attached_workspace_ids"], [])
        self.assertEqual(persisted["attached_workspace_ids"], [])
        self.assertTrue(any(message["content"].startswith("Inspect this workspace") for message in persisted["messages"]))
        self.assertTrue(any(message["role"] == "tool" and message["title"] == "Workspace detached" for message in persisted["messages"]))
        self.assertTrue(any(message["role"] == "assistant" and message["title"] == "Registration opened" for message in persisted["messages"]))
        self.assertNotIn("Prepared catalog registration for Scratch tool workspace.", response_texts)
        self.assertTrue(any(event["type"] == "workspace_detached" for event in persisted["events"]))

    def test_ui_delete_managed_draft_workspace_removes_files_and_session_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Scratch Draft"})
            workspace_root = Path(workspace["root"])
            workspace_container = workspace_root.parent
            runtime_root = _owned_terminal_workspace_runtime(state, workspace["id"])
            (runtime_root / "runtime.log").write_text("cached runtime state\n", encoding="utf-8")
            session = _create_agent_session(state, {"title": "Draft cleanup"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            deleted = _delete_ui_workspace(state, workspace["id"])
            sessions = _list_agent_sessions(state)

            self.assertTrue(workspace["managed_by_studio"])
            self.assertEqual(workspace["delete_action"], "delete_draft")
            self.assertTrue(deleted["deleted"])
            self.assertTrue(deleted["files_deleted"])
            self.assertTrue(deleted["runtime_deleted"])
            self.assertFalse(workspace_container.exists())
            self.assertFalse(runtime_root.exists())
            self.assertFalse(any(item["id"] == workspace["id"] for item in _list_ui_workspaces(state)))
            self.assertEqual(sessions[0]["attached_workspace_ids"], [])

    def test_ui_workspace_can_be_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Scratch Draft"})

            renamed = _rename_ui_workspace(
                state,
                workspace["id"],
                {
                    "schema": "optpilot.studio-workspace-rename-request.v1",
                    "request_id": "11111111-1111-4111-8111-111111111111",
                    "title": "  Solver prototype  ",
                    "expected_title": "Scratch Draft",
                    "expected_metadata_revision": None,
                },
            )
            persisted = _require_ui_workspace(state, workspace["id"])

        self.assertEqual(renamed["title"], "Solver prototype")
        self.assertEqual(persisted["title"], "Solver prototype")

    def test_ui_remove_external_draft_reference_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            external_root = tmp_path / "external-tool"
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "External Tool",
                    "root": str(external_root),
                    "source_type": "local",
                },
            )
            runtime_root = _owned_terminal_workspace_runtime(state, workspace["id"])
            (runtime_root / "runtime.log").write_text("cached runtime state\n", encoding="utf-8")
            session = _create_agent_session(state, {"title": "External cleanup"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            deleted = _delete_ui_workspace(state, workspace["id"])
            sessions = _list_agent_sessions(state)

            self.assertFalse(workspace["managed_by_studio"])
            self.assertEqual(workspace["ownership"], "external-reference")
            self.assertEqual(workspace["delete_action"], "remove_reference")
            self.assertTrue(deleted["deleted"])
            self.assertFalse(deleted["files_deleted"])
            self.assertTrue(deleted["runtime_deleted"])
            self.assertTrue(external_root.exists())
            self.assertTrue((external_root / "README.md").exists())
            self.assertFalse(runtime_root.exists())
            self.assertFalse(any(item["id"] == workspace["id"] for item in _list_ui_workspaces(state)))
            self.assertEqual(sessions[0]["attached_workspace_ids"], [])

    def test_ui_detach_read_only_workspace_removes_last_reference_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            catalog_root = tmp_path / "catalog-entry"
            catalog_root.mkdir()
            (catalog_root / "README.md").write_text("catalog source\n", encoding="utf-8")
            state = UiState(cwd=tmp_path, catalog_roots=[catalog_root.parent], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Inspect Catalog Entry",
                    "root": str(catalog_root),
                    "mode": "read-only",
                    "source_type": "catalog",
                    "registration_enabled": False,
                },
            )
            runtime_root = _owned_terminal_workspace_runtime(state, workspace["id"])
            (runtime_root / "runtime.log").write_text("cached runtime state\n", encoding="utf-8")
            session = _create_agent_session(state, {"title": "Catalog inspection"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            detached = _detach_agent_workspace(state, session["id"], workspace["id"])
            sessions = _list_agent_sessions(state)

            self.assertEqual(detached["attached_workspace_ids"], [])
            self.assertEqual(sessions[0]["attached_workspace_ids"], [])
            self.assertFalse(any(item["id"] == workspace["id"] for item in _list_ui_workspaces(state)))
            self.assertTrue(catalog_root.exists())
            self.assertTrue((catalog_root / "README.md").exists())
            self.assertFalse(runtime_root.exists())

    def test_ui_workspace_detach_keeps_read_only_reference_until_last_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            catalog_root = tmp_path / "catalog-entry"
            catalog_root.mkdir()
            state = UiState(cwd=tmp_path, catalog_roots=[catalog_root.parent], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Inspect Catalog Entry",
                    "root": str(catalog_root),
                    "mode": "read-only",
                    "source_type": "catalog",
                    "registration_enabled": False,
                },
            )
            first = _create_agent_session(state, {"title": "First inspection"})
            second = _create_agent_session(state, {"title": "Second inspection"})
            _attach_agent_workspace(state, first["id"], workspace["id"], select=True)
            _attach_agent_workspace(state, second["id"], workspace["id"], select=True)

            kept = _detach_workspace(state, workspace["id"], first["id"])
            removed = _detach_workspace(state, workspace["id"], second["id"])

            self.assertFalse(kept.get("deleted", False))
            self.assertEqual(kept["attached_sessions"], [second["id"]])
            self.assertTrue(removed["deleted"])
            self.assertFalse(removed["files_deleted"])
            self.assertFalse(any(item["id"] == workspace["id"] for item in _list_ui_workspaces(state)))
            self.assertTrue(catalog_root.exists())

    def test_ui_list_prunes_unattached_read_only_workspace_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            catalog_root = tmp_path / "catalog-entry"
            catalog_root.mkdir()
            state = UiState(cwd=tmp_path, catalog_roots=[catalog_root.parent], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Detached Catalog Entry",
                    "root": str(catalog_root),
                    "mode": "read-only",
                    "source_type": "catalog",
                    "registration_enabled": False,
                    "created_at": "2000-01-01T00:00:00Z",
                },
            )
            runtime_root = _owned_terminal_workspace_runtime(state, workspace["id"])

            listed = _list_ui_workspaces(state)

            self.assertFalse(any(item["id"] == workspace["id"] for item in listed))
            self.assertTrue(catalog_root.exists())
            self.assertFalse(runtime_root.exists())

    def test_ui_list_keeps_read_only_reference_when_runtime_stop_is_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            catalog_root = tmp_path / "catalog-entry"
            catalog_root.mkdir()
            state = UiState(cwd=tmp_path, catalog_roots=[catalog_root.parent], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Live Catalog Entry",
                    "root": str(catalog_root),
                    "mode": "read-only",
                    "source_type": "catalog",
                    "registration_enabled": False,
                    "created_at": "2000-01-01T00:00:00Z",
                },
            )
            runtime_root = state.workspace_runtime._ensure_workspace_runtime_dir(
                workspace["id"]
            )
            state.workspace_runtime._write_record(
                workspace["id"], {"status": "running"}
            )

            with patch.object(
                state.workspace_runtime,
                "_remove_container",
                return_value={"terminal_confirmed": False},
            ):
                listed = _list_ui_workspaces(state, include_support=True)

            self.assertTrue(any(item["id"] == workspace["id"] for item in listed))
            self.assertTrue(runtime_root.exists())
            self.assertTrue(catalog_root.exists())

    def test_ui_workspace_attachments_are_derived_from_agent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Stale Attachment Cache",
                    "attached_sessions": ["stale-session"],
                },
            )

            indexed = _list_ui_workspaces(state)
            normalized = next(item for item in indexed if item["id"] == workspace["id"])

            self.assertEqual(normalized["attached_sessions"], [])

    def test_ui_agent_session_can_attach_multiple_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            first = _create_ui_workspace(state, {"title": "First Draft"})
            second = _create_ui_workspace(state, {"title": "Second Draft"})
            session = _create_agent_session(state, {"title": "Multi workspace"})

            attached_first = _attach_agent_workspace(state, session["id"], first["id"], select=True)
            attached_second = _attach_agent_workspace(state, session["id"], second["id"], select=False)
            reloaded = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            persisted = _agent_session_by_id(reloaded, session["id"])
            context = _agent_context_packet(
                reloaded,
                persisted,
                {
                    "current_page": "workspace",
                    "selected_workspace": {"id": first["id"]},
                },
            )

        self.assertEqual(attached_first["attached_workspace_ids"], [first["id"]])
        self.assertEqual(attached_second["attached_workspace_ids"], [first["id"], second["id"]])
        self.assertEqual(attached_second["selected_workspace_id"], first["id"])
        self.assertEqual(persisted["attached_workspace_ids"], [first["id"], second["id"]])
        self.assertEqual(context["selected_workspace"]["id"], first["id"])
        self.assertEqual([item["id"] for item in context["attached_workspaces"]], [first["id"], second["id"]])

    def test_ui_agent_context_uses_conversation_default_workspace_off_editor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            first = _create_ui_workspace(state, {"title": "Default project"})
            second = _create_ui_workspace(state, {"title": "Other project"})
            session = _create_agent_session(state, {"title": "Default context"})
            _attach_agent_workspace(state, session["id"], first["id"], select=True)
            session = _attach_agent_workspace(
                state, session["id"], second["id"], select=False
            )

            context = _agent_context_packet(
                state,
                session,
                {
                    "current_page": "catalog",
                    "selected_catalog_entry": {
                        "kind": "resource",
                        "id": "viewer",
                    },
                },
            )

        self.assertEqual(context["current_page"], "catalog")
        self.assertEqual(context["selected_workspace"]["id"], first["id"])
        self.assertEqual(context["selected_catalog_entry"]["id"], "viewer")

    def test_ui_agent_context_prefers_visible_attached_editor_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            default = _create_ui_workspace(state, {"title": "Default project"})
            visible = _create_ui_workspace(state, {"title": "Visible project"})
            session = _create_agent_session(state, {"title": "Editor context"})
            _attach_agent_workspace(state, session["id"], default["id"], select=True)
            session = _attach_agent_workspace(
                state, session["id"], visible["id"], select=False
            )

            context = _agent_context_packet(
                state,
                session,
                {
                    "current_page": "workspace",
                    "selected_workspace": {"id": visible["id"]},
                },
            )

        self.assertEqual(context["selected_workspace"]["id"], visible["id"])

    def test_ui_new_agent_session_starts_detached_in_browser_client(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        app_js = repo_root / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
        source = app_js.read_text(encoding="utf-8")
        start = source.index("async function createAgentSession()")
        end = source.index("async function closeWorkspaceFromCurrentSession", start)
        body = source[start:end]

        self.assertIn("attached_workspace_ids: []", body)
        self.assertIn('selected_workspace_id: ""', body)
        self.assertNotIn("attached_workspace_ids: attached", body)
        self.assertNotIn("currentAttachedIds.slice()", body)

        # The create path no longer seeds the attachment maps itself; it hands the
        # server's response to updateAgentSessionFromPayload, which rebuilds them
        # via mergeAgentSessionPayload. Detachment therefore has to hold there.
        self.assertIn("updateAgentSessionFromPayload(payload.session)", body)

        merge_start = source.index("function mergeAgentSessionPayload(session)")
        merge_end = source.index("\nfunction ", merge_start + 1)
        merge_body = source[merge_start:merge_end]
        self.assertIn(
            "state.agentWorkspaceAttachments[session.id] = nextAttachments",
            merge_body,
        )
        self.assertIn(
            "state.selectedWorkspaceByAgentSession[session.id] = "
            "session.selected_workspace_id || null",
            merge_body,
        )

    def test_ui_agent_session_list_does_not_probe_workspace_runtimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Runtime-heavy draft"})
            session = _create_agent_session(state, {"title": "Fast session list"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            class FailingWorkspaceRuntime:
                def status(self, workspace: JsonDict) -> JsonDict:
                    raise AssertionError("agent session listing must not probe workspace runtimes")

            state.workspace_runtime = FailingWorkspaceRuntime()  # type: ignore[assignment]
            lock = _agent_session_operation_lock(state, session["id"])
            lock_ready = threading.Event()
            lock_release = threading.Event()

            def hold_session_lock() -> None:
                with lock:
                    lock_ready.set()
                    lock_release.wait(timeout=2)

            thread = threading.Thread(target=hold_session_lock, daemon=True)
            thread.start()
            self.assertTrue(lock_ready.wait(timeout=0.5))
            started_at = time.monotonic()
            try:
                sessions = _list_agent_sessions(state)
                elapsed = time.monotonic() - started_at
            finally:
                lock_release.set()
                thread.join(timeout=1)

        listed = next(item for item in sessions if item["id"] == session["id"])
        self.assertEqual(listed["attached_workspace_ids"], [workspace["id"]])
        self.assertLess(elapsed, 0.5)

    def test_ui_agent_context_uses_user_facing_page_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Page names"})

            stale_tab_state = {
                "current_page": "workspace",
                "assistant_mode": "chat",
                "selected_catalog_entry": {"kind": "environment", "id": "default-catalog-selection"},
                "selected_study_plan": {"id": "default-plan"},
                "selected_run": {"id": "default-run"},
                "registration_menu": {"status": "draft"},
                "code_editor": {"status": "ready", "folder": str(tmp_path)},
                "workspace_preview": {"status": "ready", "port": 5173, "url": "http://127.0.0.1:18766/proxy/5173/"},
            }
            editor_context = _agent_context_packet(state, session, stale_tab_state)
            studies_context = _agent_context_packet(state, session, {"current_page": "experiments"})

        self.assertEqual(editor_context["current_page"], "editor")
        self.assertEqual(studies_context["current_page"], "studies")
        self.assertIsNone(editor_context["selected_catalog_entry"])
        self.assertIsNone(editor_context["selected_study_plan"])
        self.assertIsNone(editor_context["selected_run"])
        self.assertIsNone(editor_context["registration_menu"])
        self.assertEqual(editor_context["code_editor"]["status"], "ready")
        self.assertEqual(editor_context["workspace_preview"]["port"], 5173)
        self.assertNotIn("studies", editor_context["catalog_counts"])
        self.assertEqual(editor_context["study_plan_count"], 0)
        self.assertNotIn("current_page", editor_context["visible_state"])
        self.assertNotIn("workspace_preview", editor_context["visible_state"])

    def test_ui_agent_tools_enforce_workspace_boundaries_and_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19000,
                ),
            )
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    "permissions": {"shell_run": "safe_without_approval"},
                },
            )
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Editable assistant workspace",
                    "root": str(tmp_path / "editable"),
                    "source_type": "tool",
                },
            )
            read_only = _create_ui_workspace(
                state,
                {
                    "title": "Read-only assistant workspace",
                    "root": str(tmp_path / "read-only"),
                    "mode": "read-only",
                    "source_type": "catalog",
                },
            )
            unattached = _create_ui_workspace(
                state,
                {
                    "title": "Unattached workspace",
                    "root": str(tmp_path / "unattached"),
                },
            )
            session = _create_agent_session(state, {"title": "Tool safety"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)
            _attach_agent_workspace(state, session["id"], read_only["id"], select=False)

            written = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_write",
                {"path": "configs/demo.yaml", "content": "config: note\n"},
            )
            read = _execute_agent_tool(state, session["id"], "optpilot_file_read", {"path": "configs/demo.yaml"})
            diff = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_diff",
                {"path": "configs/demo.yaml", "content": "config: changed\n"},
            )
            tree = _execute_agent_tool(state, session["id"], "optpilot_file_tree", {"path": ".", "max_files": 20})
            tree_default = _execute_agent_tool(state, session["id"], "optpilot_file_tree", {"path": None, "max_files": 20})
            viewed = _execute_agent_tool(state, session["id"], "optpilot_file_editor", {"command": "view", "path": "configs/demo.yaml", "view_range": [1, 1]})
            edited = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_editor",
                {"command": "str_replace", "path": "configs/demo.yaml", "old_str": "config: note\n", "new_str": "config: edited\n"},
            )
            inserted = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_editor",
                {"command": "insert", "path": "configs/demo.yaml", "insert_line": 1, "new_str": "added: true"},
            )
            created = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_editor",
                {"command": "create", "path": "configs/created.yaml", "file_text": "created: true\n"},
            )
            shell = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": [sys.executable, "-c", "print('assistant ok')"]},
            )
            terminal = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_terminal",
                {"command": f"{shlex.quote(sys.executable)} -c \"print('terminal ok')\""},
            )
            approval = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": ["uv", "pip", "install", "demo-package"]},
            )
            approvals = _read_agent_approvals(state, session["id"])
            rejected = _reject_agent_action(state, session["id"], approvals[-1]["id"], "Unit test rejection.")

            self.assertTrue(written["ok"], written)
            self.assertTrue(written["data"]["created"])
            self.assertEqual(read["data"]["content"], "config: note\n")
            self.assertIn("-config: note", diff["data"]["diff"])
            self.assertTrue(any(item["path"] == "configs/demo.yaml" for item in tree["data"]["files"]))
            self.assertTrue(any(item["path"] == "configs/demo.yaml" for item in tree_default["data"]["files"]))
            self.assertEqual(viewed["data"]["content"], "config: note")
            self.assertIn("-config: note", edited["data"]["diff"])
            self.assertIn("+config: edited", edited["data"]["diff"])
            self.assertIn("+added: true", inserted["data"]["diff"])
            self.assertTrue(created["ok"], created)
            self.assertEqual((Path(workspace["root"]) / "configs" / "demo.yaml").read_text(encoding="utf-8"), "config: edited\nadded: true\n")
            self.assertTrue(shell["ok"], shell)
            self.assertIn("assistant ok", shell["data"]["stdout"])
            self.assertTrue(terminal["ok"], terminal)
            self.assertIn("terminal ok", terminal["data"]["stdout"])
            self.assertTrue(shell["data"]["runtime"]["containerized"])
            self.assertEqual(shell["data"]["runtime"]["executor"], "container")
            self.assertFalse(approval["ok"])
            self.assertTrue(approval["data"]["approval_required"])
            self.assertEqual(rejected["approval"]["status"], "rejected")
            calls = _fake_workspace_container_calls(tmp_path)
            self.assertTrue(any(call and call[0] == "run" for call in calls), calls)
            self.assertTrue(any(call and call[0] == "exec" and sys.executable in call for call in calls), calls)
            with self.assertRaises(PermissionError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_write",
                    {"path": "../outside.txt", "content": "escape\n"},
                )
            with self.assertRaises(PermissionError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_write",
                    {"workspace_id": read_only["id"], "path": "blocked.txt", "content": "nope\n"},
                )
            with self.assertRaises(PermissionError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_read",
                    {"workspace_id": unattached["id"], "path": "README.md"},
                )
            with self.assertRaises(FileExistsError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_editor",
                    {"command": "create", "path": "configs/demo.yaml", "file_text": "overwrite: no\n"},
                )
            with self.assertRaises(ValueError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_editor",
                    {"command": "str_replace", "path": "configs/demo.yaml", "old_str": "missing", "new_str": "replacement"},
                )
            with self.assertRaises(PermissionError):
                _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_file_editor",
                    {"workspace_id": read_only["id"], "command": "str_replace", "path": "blocked.txt", "old_str": "a", "new_str": "b"},
                )

    def test_ui_agent_permission_settings_gate_mutating_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    "permissions": {
                        "file_write": "approval_required",
                        "shell_run": "disabled",
                        "catalog_registration": "disabled",
                        "study_launch": "disabled",
                        "job_stop": "disabled",
                        "smoke_test": "disabled",
                    },
                },
            )
            workspace = _create_ui_workspace(state, {"title": "Permission workspace", "root": str(tmp_path / "workspace")})
            session = _create_agent_session(state, {"title": "Permissions"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            write = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_write",
                {"path": "configs/demo.yaml", "content": "ok: true\n"},
            )
            approvals = _read_agent_approvals(state, session["id"])
            approved = _approve_agent_action(state, session["id"], approvals[0]["id"])
            shell = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": [sys.executable, "-c", "print('blocked')"]},
            )
            registration = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_apply",
                {"workspace_id": workspace["id"], "plan_id": "missing-plan"},
            )
            smoke = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_package_plan_smoke",
                {"workspace_id": workspace["id"], "plan_id": "missing-plan"},
            )
            stopped = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_job_stop",
                {"job_id": "missing-job"},
            )
            written_content = (Path(workspace["root"]) / "configs" / "demo.yaml").read_text(encoding="utf-8")

        self.assertFalse(write["ok"])
        self.assertTrue(write["data"]["approval_required"])
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approved["result"]["ok"], approved)
        self.assertEqual(written_content, "ok: true\n")
        for result, permission in [
            (shell, "shell_run"),
            (registration, "catalog_registration"),
            (smoke, "smoke_test"),
        ]:
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["data"]["permission"], permission)
            self.assertEqual(result["data"]["permission_status"], "disabled")
        self.assertFalse(stopped["ok"], stopped)
        self.assertEqual(stopped["data"]["permission"], "job_stop")
        self.assertEqual(stopped["data"]["permission_status"], "disabled")

    def test_ui_agent_approval_bypass_argument_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    "permissions": {"file_write": "approval_required", "shell_run": "approval_required"},
                },
            )
            workspace = _create_ui_workspace(state, {"title": "Bypass workspace", "root": str(tmp_path / "workspace")})
            session = _create_agent_session(state, {"title": "Bypass"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            write = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_file_write",
                {"path": "demo.txt", "content": "bad\n", "approved": True},
            )
            shell = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": [sys.executable, "-c", "print('bad')"], "approved": True},
            )
            persisted = _agent_session_by_id(state, session["id"])

        self.assertFalse(write["ok"], write)
        self.assertFalse(shell["ok"], shell)
        self.assertEqual(write["data"]["policy_error"], "approval_bypass_rejected")
        self.assertEqual(shell["data"]["policy_error"], "approval_bypass_rejected")
        self.assertFalse((Path(workspace["root"]) / "demo.txt").exists())
        self.assertEqual(persisted["effective_status"], "idle")

    def test_ui_agent_approval_dedupes_and_forwards_approved_tool_result(self) -> None:
        forwarded: List[JsonDict] = []

        class ForwardingAdapter:
            def submit_tool_result(self, conversation_id: str, name: str, call_id: str, result: JsonDict) -> JsonDict:
                forwarded.append({"conversation_id": conversation_id, "name": name, "call_id": call_id, "result": result})
                return {"sent": True, "conversation_id": conversation_id, "tool_call_id": call_id}

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19100,
                ),
            )
            state.agent_adapter = ForwardingAdapter()
            workspace = _create_ui_workspace(state, {"title": "Approval workspace", "root": str(tmp_path / "approval")})
            fake_pip = Path(workspace["root"]) / "pip"
            fake_pip.write_text("#!/bin/sh\necho approved-pip\n", encoding="utf-8")
            fake_pip.chmod(0o755)
            session = _create_agent_session(
                state,
                {"title": "Approval forwarding", "openhands_conversation_id": "oh-approval-conversation"},
            )
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            first = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": ["./pip", "--version"], "_openhands_tool_call_id": "call-install-1", "description": "Check pip"},
            )
            second = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": ["./pip", "--version"], "_openhands_tool_call_id": "call-install-2", "description": "Inspect pip version"},
            )
            approvals = _read_agent_approvals(state, session["id"])
            approved = _approve_agent_action(state, session["id"], approvals[0]["id"])
            events = _read_agent_events(state, session["id"])
            persisted = _agent_session_by_id(state, session["id"])

        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(first["data"]["approval"]["id"], second["data"]["approval"]["id"])
        self.assertEqual(len([approval for approval in approvals if approval["status"] == "pending"]), 1)
        self.assertEqual(approved["approval"]["status"], "approved")
        self.assertTrue(approved["result"]["ok"], approved)
        self.assertEqual(forwarded[0]["conversation_id"], "oh-approval-conversation")
        self.assertEqual(forwarded[0]["call_id"], "call-install-1")
        self.assertEqual(forwarded[1]["conversation_id"], "oh-approval-conversation")
        self.assertEqual(forwarded[1]["call_id"], "call-install-2")
        self.assertIn("approved-pip", forwarded[0]["result"]["data"]["stdout"])
        self.assertEqual(persisted["status"], "waiting_for_agent")
        self.assertEqual(persisted["effective_status"], "waiting_for_agent")
        self.assertTrue(any(event["type"] == "openhands_tool_result_forwarded" for event in events))

    def test_ui_agent_shell_approval_detects_shell_wrapped_install_commands(self) -> None:
        self.assertTrue(_shell_needs_approval(["sh", "-lc", ".venv/bin/pip install -e ."]))
        self.assertTrue(_shell_needs_approval(["bash", "-c", "python -m pip install demo-package"]))
        self.assertTrue(_shell_needs_approval(["zsh", "-lc", "echo setup && uv pip install -r requirements.txt"]))
        self.assertFalse(_shell_needs_approval(["sh", "-lc", "python -c \"print('ok')\""]))
        self.assertFalse(_shell_needs_approval(["sh", "-lc", "grep -R \"pip install\" README.md"]))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            workspace = _create_ui_workspace(state, {"title": "Approval workspace", "root": str(tmp_path / "approval")})
            session = _create_agent_session(state, {"title": "Terminal approval"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_terminal",
                {
                    "command": ".venv/bin/pip install -e .",
                    "description": "Install the editable project",
                    "_openhands_tool_call_id": "call-terminal-install",
                },
            )
            approvals = _read_agent_approvals(state, session["id"])

        self.assertFalse(result["ok"])
        self.assertTrue(result["data"]["approval_required"])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["status"], "pending")
        self.assertEqual(approvals[0]["tool"], "optpilot_terminal")
        self.assertIn("pip install", approvals[0]["summary"])
        self.assertIn("call-terminal-install", approvals[0]["openhands_tool_call_ids"])

    def test_ui_agent_docs_and_smoke_tools_are_available(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        study_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=repo_root, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[])
            state.sessions_dir = tmp_path / "sessions"
            state.agent_sessions_dir = tmp_path / "agent_sessions"
            state.jobs_dir = tmp_path / "jobs"
            state.workspaces_dir = tmp_path / "workspaces"
            state.runtime_dir = tmp_path / "runtime"
            for isolated_dir in (state.sessions_dir, state.agent_sessions_dir, state.jobs_dir, state.workspaces_dir, state.runtime_dir):
                isolated_dir.mkdir(parents=True, exist_ok=True)
            # This state's cwd is the repository itself, so keep settings in
            # the temporary directory rather than writing into the checkout.
            state.settings_path = tmp_path / "settings.json"
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    # Smoke tests run without asking by default; this case is
                    # about the approval card, so ask for it explicitly.
                    "permissions": {"smoke_test": "approval_required"},
                },
            )
            session = _create_agent_session(state, {"title": "Docs and smoke"})
            study_entry = next(
                item
                for item in _catalog_payload(state)["studies"]
                if item["id"] == "toy-random-search"
            )

            docs = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_docs_search",
                {"query": "methodContext references", "limit": 3},
            )
            study_detail = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_catalog_detail",
                {"config_kind": "studies", "uid": study_entry["uid"]},
            )
            smoke = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_smoke_test_study",
                {"study_path": str(study_path), "max_trials": 1},
            )

        self.assertTrue(docs["ok"], docs)
        self.assertTrue(docs["data"]["results"])
        self.assertTrue(study_detail["ok"], study_detail)
        self.assertEqual(study_detail["data"]["entry"]["config"], "study")
        self.assertTrue(study_detail["data"]["validation"]["valid"], study_detail)
        self.assertFalse(smoke["ok"], smoke)
        self.assertTrue(smoke["data"]["approval_required"])

    def test_ui_agent_tool_schema_requires_exact_study_builder_refs(self) -> None:
        by_name = {str(tool.get("name")): tool for tool in OPTPILOT_AGENT_TOOL_SPECS}
        study_draft = by_name["optpilot_study_draft"]["parameters"]["properties"]
        smoke_description = str(by_name["optpilot_package_plan_smoke"].get("description") or "")

        self.assertIn("workspace_id", study_draft)
        self.assertIn("expected_workspace_revision", study_draft)
        self.assertIn("environment_ref", study_draft)
        self.assertIn("method_ref", study_draft)
        self.assertNotIn("environment_path", study_draft)
        self.assertNotIn("method_path", study_draft)
        self.assertNotIn("evidenceStorage", study_draft)
        self.assertNotIn("evidenceOutputDir", study_draft)
        self.assertNotIn("approved=true", smoke_description)
        self.assertIn("approve or reject", smoke_description)

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_session_dispatches_to_openhands_http_bridge(self) -> None:
        requests = []

        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                requests.append((self.path, body))
                if self.path == "/api/conversations":
                    self._send_json({"id": "oh-test-conversation"})
                    return
                if self.path == "/api/conversations/oh-test-conversation/events":
                    self._send_json({"success": True})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-test-conversation/events/search"):
                    self._send_json({
                        "items": [
                            {
                                "kind": "ActionEvent",
                                "source": "agent",
                                "tool_name": "finish",
                                "tool_call_id": "finish-oh-test",
                                "action": {
                                    "kind": "FinishAction",
                                    "message": "OpenHands saw the Catalog context.",
                                },
                            }
                        ],
                        "next_page_id": None,
                    })
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        }
                    },
                )
                session = _create_agent_session(state, {"title": "Live OpenHands"})
                result = _append_agent_message(
                    state,
                    session["id"],
                    {
                        "role": "user",
                        "title": "User",
                        "content": "What catalog item is selected?",
                        "ui_context": {
                            "current_page": "catalog",
                            "selected_catalog_entry": {"kind": "environment", "id": "toy-factory"},
                        },
                    },
                )
                persisted = _agent_session_by_id(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(result["session"]["status"], "idle")
        self.assertEqual(result["session"]["openhands_conversation_id"], "oh-test-conversation")
        self.assertTrue(any(message["content"] == "OpenHands saw the Catalog context." for message in persisted["messages"]))
        self.assertTrue(any(event["type"] == "openhands_dispatch_completed" for event in persisted["events"]))
        start_payload = next(body for path, body in requests if path == "/api/conversations")
        event_payload = next(body for path, body in requests if path.endswith("/events"))
        self.assertEqual(start_payload["agent"]["llm"]["model"], "openrouter/deepseek/deepseek-v4-flash")
        self.assertEqual(start_payload["confirmation_policy"], {"kind": "NeverConfirm"})
        self.assertIn("OptPilot Assistant", start_payload["agent"]["agent_context"]["system_message_suffix"])
        native_tool_names = {tool["name"] for tool in start_payload["agent"]["tools"]}
        self.assertIn("grep", native_tool_names)
        self.assertIn("glob", native_tool_names)
        self.assertIn("task_tracker", native_tool_names)
        self.assertNotIn("terminal", native_tool_names)
        self.assertNotIn("file_editor", native_tool_names)
        self.assertNotIn("optpilot_terminal", native_tool_names)
        self.assertNotIn("optpilot_file_editor", native_tool_names)
        client_tool_names = {tool["name"] for tool in start_payload["client_tools"]}
        self.assertIn("optpilot_catalog_list", client_tool_names)
        self.assertIn("optpilot_terminal", client_tool_names)
        self.assertIn("optpilot_file_editor", client_tool_names)
        self.assertNotIn("terminal", client_tool_names)
        self.assertNotIn("file_editor", client_tool_names)
        preview_tool = next(tool for tool in start_payload["client_tools"] if tool["name"] == "optpilot_workspace_preview_open")
        self.assertIn("extra_ports", preview_tool["parameters"]["properties"])
        for tool in start_payload["client_tools"]:
            self.assertNotIn("kind", tool.get("parameters", {}).get("properties", {}))
        self.assertIn("\"current_page\": \"catalog\"", event_payload["content"][0]["text"])
        self.assertIn("\"id\": \"toy-factory\"", event_payload["content"][0]["text"])

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_session_reports_openhands_tool_schema_conflict_clearly(self) -> None:
        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/api/conversations":
                    self._send_json(
                        {
                            "detail": (
                                "Client tool 'optpilot_workspace_preview_open' is already registered "
                                "with a different parameters schema. Client tool names must map to a "
                                "single, stable schema within a process."
                            )
                        },
                        status=422,
                    )
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        }
                    },
                )
                session = _create_agent_session(state, {"title": "Schema conflict"})
                result = _append_agent_message(
                    state,
                    session["id"],
                    {
                        "role": "user",
                        "title": "User",
                        "content": "Hello",
                        "ui_context": {"current_page": "catalog"},
                    },
                )
                persisted = _agent_session_by_id(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(result["session"]["status"], "error")
        assistant_messages = [message for message in persisted["messages"] if message["role"] == "assistant"]
        self.assertTrue(assistant_messages)
        self.assertEqual(assistant_messages[-1]["title"], "OpenHands tool schema changed")
        self.assertIn("Restart the OpenHands agent server", assistant_messages[-1]["content"])
        self.assertTrue(any(event["type"] == "openhands_tool_schema_conflict" for event in persisted["events"]))

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_session_executes_openhands_client_tool_requests(self) -> None:
        requests = []
        server_state = {"user_message_seen": False, "tool_result_seen": False}

        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                requests.append((self.path, body))
                if self.path == "/api/conversations":
                    self._send_json({"id": "oh-tool-conversation"})
                    return
                if self.path == "/api/conversations/oh-tool-conversation/events":
                    text = body.get("content", [{}])[0].get("text", "") if isinstance(body.get("content"), list) else ""
                    if "OptPilot tool result for optpilot_catalog_list" in text:
                        server_state["tool_result_seen"] = True
                    else:
                        server_state["user_message_seen"] = True
                    self._send_json({"success": True})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-tool-conversation/events/search"):
                    if server_state["tool_result_seen"]:
                        self._send_json(
                            {
                                "items": [
                                    {
                                        "kind": "ActionEvent",
                                        "source": "agent",
                                        "tool_name": "finish",
                                        "tool_call_id": "finish-catalog-1",
                                        "action": {
                                            "kind": "FinishAction",
                                            "message": "Catalog tool result received.",
                                        },
                                    }
                                ],
                            }
                        )
                    elif server_state["user_message_seen"]:
                        self._send_json(
                            {
                                "items": [
                                    {
                                        "kind": "ActionEvent",
                                        "tool_name": "optpilot_catalog_list",
                                        "tool_call_id": "call-catalog-1",
                                        "action": {"kind": "optpilot_catalog_list"},
                                    }
                                ],
                            }
                        )
                    else:
                        self._send_json({"items": []})
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        }
                    },
                )
                session = _create_agent_session(state, {"title": "Tool OpenHands"})
                result = _append_agent_message(
                    state,
                    session["id"],
                    {
                        "role": "user",
                        "title": "User",
                        "content": "List catalog entries.",
                        "ui_context": {"current_page": "catalog"},
                    },
                )
                persisted = _agent_session_by_id(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(result["session"]["status"], "idle")
        self.assertTrue(server_state["tool_result_seen"])
        self.assertTrue(any(message["content"] == "Catalog tool result received." for message in persisted["messages"]))
        tool_call_event = next(event for event in persisted["events"] if event.get("payload", {}).get("tool") == "optpilot_catalog_list" and event["type"] == "openhands_event")
        tool_result_event = next(event for event in persisted["events"] if event["type"] == "optpilot_tool_result")
        self.assertEqual(tool_call_event["payload"]["category"], "tool_call")
        self.assertIn("arguments_preview", tool_call_event["payload"])
        self.assertIn("result_preview", tool_result_event["payload"])
        self.assertIn('"ok": true', tool_result_event["payload"]["result_preview"])
        tool_result_payload = next(body for _path, body in requests if "OptPilot tool result for optpilot_catalog_list" in json.dumps(body))
        self.assertIn('"ok": true', tool_result_payload["content"][0]["text"])

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_openhands_approval_tool_pauses_without_forwarding_result(self) -> None:
        requests = []
        server_state = {"user_message_seen": False, "tool_result_seen": False}

        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                requests.append((self.path, body))
                if self.path == "/api/conversations":
                    self._send_json({"id": "oh-approval-pause"})
                    return
                if self.path == "/api/conversations/oh-approval-pause/events":
                    text = body.get("content", [{}])[0].get("text", "") if isinstance(body.get("content"), list) else ""
                    if "OptPilot tool result for optpilot_package_plan_smoke" in text:
                        server_state["tool_result_seen"] = True
                    else:
                        server_state["user_message_seen"] = True
                    self._send_json({"success": True})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-approval-pause/events/search"):
                    if server_state["user_message_seen"]:
                        self._send_json(
                            {
                                "items": [
                                    {
                                        "kind": "ActionEvent",
                                        "tool_name": "optpilot_package_plan_smoke",
                                        "tool_call_id": "call-smoke-approval",
                                        "action": {
                                            "kind": "optpilot_package_plan_smoke",
                                            "workspace_id": "workspace-id",
                                            "plan_id": "plan-id",
                                        },
                                    }
                                ],
                            }
                        )
                    else:
                        self._send_json({"items": []})
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        },
                        # This case is about the approval *pause* mechanism and
                        # drives it through the smoke tool, which no longer
                        # asks by default. Ask for it, or there is no pause to
                        # observe.
                        "permissions": {"smoke_test": "approval_required"},
                    },
                )
                session = _create_agent_session(state, {"title": "Approval pause"})
                result = _append_agent_message(
                    state,
                    session["id"],
                    {
                        "role": "user",
                        "title": "User",
                        "content": "Run the package plan smoke test.",
                        "ui_context": {"current_page": "workspace"},
                    },
                )
                persisted = _agent_session_by_id(state, session["id"])
                approvals = _read_agent_approvals(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(result["session"]["effective_status"], "awaiting_user_approval")
        self.assertEqual(persisted["effective_status"], "awaiting_user_approval")
        self.assertEqual(len([approval for approval in approvals if approval["status"] == "pending"]), 1)
        self.assertEqual(approvals[0]["openhands_tool_call_ids"], ["call-smoke-approval"])
        self.assertFalse(server_state["tool_result_seen"])
        self.assertFalse(any("OptPilot tool result for optpilot_package_plan_smoke" in json.dumps(body) for _path, body in requests))
        self.assertTrue(any(event["type"] == "optpilot_approval_pause" for event in persisted["events"]))

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_http_bridge_ignores_previous_assistant_events(self) -> None:
        server_state = {"message_count": 0}

        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if self.path == "/api/conversations":
                    self._send_json({"id": "oh-stale-conversation"})
                    return
                if self.path == "/api/conversations/oh-stale-conversation/events":
                    text = body.get("content", [{}])[0].get("text", "") if isinstance(body.get("content"), list) else ""
                    if "Second question" in text:
                        server_state["message_count"] = 2
                    elif "First question" in text:
                        server_state["message_count"] = 1
                    self._send_json({"success": True})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-stale-conversation/events/search"):
                    items = []
                    if server_state["message_count"] >= 1:
                        items.append(
                            {
                                "id": "evt-old-answer",
                                "kind": "ActionEvent",
                                "source": "agent",
                                "tool_name": "finish",
                                "tool_call_id": "finish-first",
                                "action": {
                                    "kind": "FinishAction",
                                    "message": "First answer.",
                                },
                            }
                        )
                    if server_state["message_count"] >= 2:
                        items.append(
                            {
                                "id": "evt-new-answer",
                                "kind": "ActionEvent",
                                "source": "agent",
                                "tool_name": "finish",
                                "tool_call_id": "finish-second",
                                "action": {
                                    "kind": "FinishAction",
                                    "message": "Second answer.",
                                },
                            }
                        )
                    self._send_json({"items": items})
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        }
                    },
                )
                session = _create_agent_session(state, {"title": "Stale event guard"})
                _append_agent_message(
                    state,
                    session["id"],
                    {"role": "user", "title": "User", "content": "First question", "ui_context": {"current_page": "catalog"}},
                )
                _append_agent_message(
                    state,
                    session["id"],
                    {"role": "user", "title": "User", "content": "Second question", "ui_context": {"current_page": "catalog"}},
                )
                persisted = _agent_session_by_id(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        contents = [message["content"] for message in persisted["messages"] if message["role"] == "assistant"]
        self.assertEqual(contents.count("First answer."), 1)
        self.assertEqual(contents.count("Second answer."), 1)

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_agent_http_bridge_ignores_agent_final_response_endpoint(self) -> None:
        server_state = {"message_count": 0, "search_count": 0, "final_response_count": 0}

        class FakeOpenHandsHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if self.path == "/api/conversations":
                    self._send_json({"id": "oh-cached-final-conversation"})
                    return
                if self.path == "/api/conversations/oh-cached-final-conversation/events":
                    text = body.get("content", [{}])[0].get("text", "") if isinstance(body.get("content"), list) else ""
                    if "Second question" in text:
                        server_state["message_count"] = 2
                    elif "First question" in text:
                        server_state["message_count"] = 1
                    self._send_json({"success": True})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-cached-final-conversation/events/search"):
                    server_state["search_count"] += 1
                    if server_state["message_count"] >= 2 and server_state["search_count"] > 4:
                        self._send_json(
                            {
                                "items": [
                                    {
                                        "id": "evt-fresh-answer",
                                        "kind": "ActionEvent",
                                        "source": "agent",
                                        "tool_name": "finish",
                                        "tool_call_id": "finish-second",
                                        "action": {
                                            "kind": "FinishAction",
                                            "message": "Second answer.",
                                        },
                                    }
                                ]
                            }
                        )
                    else:
                        self._send_json({"items": []})
                    return
                if self.path == "/api/conversations/oh-cached-final-conversation/agent_final_response":
                    server_state["final_response_count"] += 1
                    self._send_json({"response": "First answer."})
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_json(self, payload: JsonDict, status: int = 200) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenHandsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
                _update_agent_settings(
                    state,
                    {
                        "openhands": {
                            "enabled": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}",
                            "session_endpoint": "/api/conversations",
                            "model": "deepseek/deepseek-v4-flash",
                            "api_key": "sk-test-secret",
                        }
                    },
                )
                session = _create_agent_session(state, {"title": "Cached final response guard"})
                first = _append_agent_message(
                    state,
                    session["id"],
                    {"role": "user", "title": "User", "content": "First question", "ui_context": {"current_page": "catalog"}},
                )
                second = _append_agent_message(
                    state,
                    session["id"],
                    {"role": "user", "title": "User", "content": "Second question", "ui_context": {"current_page": "catalog"}},
                )
                synced = _sync_agent_session(state, session["id"])
                persisted = _agent_session_by_id(state, session["id"])
        finally:
            server.shutdown()
            server.server_close()

        contents = [message["content"] for message in persisted["messages"] if message["role"] == "assistant"]
        self.assertEqual(first["session"]["status"], "waiting_for_agent")
        self.assertEqual(second["session"]["status"], "idle")
        self.assertEqual(synced["status"], "idle")
        self.assertEqual(server_state["final_response_count"], 0)
        self.assertEqual(contents.count("First answer."), 0)
        self.assertEqual(contents.count("Second answer."), 1)

    def test_ui_agent_openhands_parser_does_not_treat_user_llm_message_as_assistant(self) -> None:
        adapter = OpenHandsAdapter(OpenHandsRuntimeConfig(enabled=False))
        user_event = {
            "id": "evt-user",
            "kind": "MessageEvent",
            "source": "user",
            "llm_message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": 'User request:\nhello\n\nVisible OptPilot Studio context packet:\n{"current_page": "runs"}',
                    }
                ],
            },
        }
        assistant_event = {
            "id": "evt-assistant",
            "kind": "MessageEvent",
            "source": "agent",
            "llm_message": {
                "role": "assistant",
                "reasoning_content": "The user greeted me, so I should greet back and offer OptPilot help.",
                "content": [{"type": "text", "text": "Hello from assistant."}],
            },
        }
        tool_call_event = {
            "id": "evt-tool-call",
            "kind": "ActionEvent",
            "tool_name": "optpilot_catalog_list",
            "tool_call_id": "call-1",
            "action": {"kind": "optpilot_catalog_list", "config_kind": "method"},
        }
        tool_feedback_event = {
            "id": "evt-tool-feedback",
            "kind": "MessageEvent",
            "source": "user",
            "llm_message": {
                "role": "user",
                "content": [{"type": "text", "text": "OptPilot tool result for optpilot_catalog_list (call-1).\n```json\n{}\n```"}],
            },
        }

        self.assertEqual(adapter._event_assistant_text(user_event), "")
        self.assertEqual(adapter._event_assistant_text(assistant_event), "Hello from assistant.")
        self.assertIn("greet back", adapter._event_reasoning_text(assistant_event))
        self.assertEqual(adapter._compact_openhands_event_summary(user_event), "User request sent to OpenHands: hello")
        self.assertNotIn("current_page", adapter._event_payload_preview(user_event))
        self.assertIn("Studio context packet redacted", adapter._event_payload_preview(user_event))
        reasoning_payload = adapter._openhands_event_trace(assistant_event)["payload"]
        self.assertEqual(reasoning_payload["category"], "reasoning")
        self.assertIn("greet back", reasoning_payload["reasoning"])
        tool_payload = adapter._openhands_event_trace(tool_call_event)["payload"]
        self.assertEqual(tool_payload["category"], "tool_call")
        self.assertEqual(tool_payload["tool"], "optpilot_catalog_list")
        self.assertIn('"config_kind": "method"', tool_payload["arguments_preview"])
        self.assertEqual(adapter._openhands_event_trace(tool_feedback_event)["payload"]["category"], "tool_result_feedback")

    def test_ui_agent_openhands_only_treats_finish_message_as_final(self) -> None:
        adapter = OpenHandsAdapter(OpenHandsRuntimeConfig(enabled=False))
        finish_event = {
            "id": "evt-finish",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "finish",
            "tool_call_id": "functions.finish:1",
            "action": {
                "kind": "FinishAction",
                "message": "Install failed in the workspace runtime because Python and Node are unavailable.",
            },
        }
        plain_message_event = {
            "id": "evt-plain-message",
            "kind": "MessageEvent",
            "source": "agent",
            "llm_message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "I need to wait for the file read results before proceeding.",
                    }
                ],
            },
        }

        answer = adapter._best_finish_text([plain_message_event, finish_event], set(), set())

        self.assertEqual(answer, "Install failed in the workspace runtime because Python and Node are unavailable.")
        self.assertEqual(adapter._best_finish_text([plain_message_event], set(), set()), "")
        self.assertEqual(adapter._event_assistant_text(plain_message_event), "I need to wait for the file read results before proceeding.")

    def test_ui_agent_openhands_finish_suppresses_pending_tool_execution(self) -> None:
        finish_event = {
            "id": "evt-finish",
            "kind": "ActionEvent",
            "source": "agent",
            "action": {
                "kind": "FinishAction",
                "message": "Done before stale tool calls arrive.",
            },
        }
        stale_tool_event = {
            "id": "evt-stale-tool",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "optpilot_file_read",
            "tool_call_id": "tool-stale-read",
            "tool_call": {
                "id": "tool-stale-read",
                "name": "optpilot_file_read",
                "arguments": json.dumps({"path": "stale.py"}),
            },
        }

        class FinishFirstAdapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(OpenHandsRuntimeConfig(enabled=False))

            def _request_json(self, method: str, url: str, *, payload: object = None, timeout: float = 10.0) -> tuple[JsonDict, JsonDict]:
                if method == "GET" and "events/search" in url:
                    return {"items": [finish_event, stale_tool_event]}, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = FinishFirstAdapter()
        executed_tools: List[str] = []

        answer, events, runtime_error, paused_approval_id = adapter._poll_openhands_answer(
            "http://openhands.example/api/conversations",
            "conversation-1",
            tool_executor=lambda tool_name, arguments: executed_tools.append(tool_name) or {"ok": True},
            poll_seconds=0.2,
        )

        self.assertEqual(answer, "Done before stale tool calls arrive.")
        self.assertEqual(runtime_error, "")
        self.assertEqual(paused_approval_id, "")
        self.assertEqual(executed_tools, [])
        self.assertTrue(any(event.get("payload", {}).get("tool") == "optpilot_file_read" for event in events))

    def test_ui_agent_openhands_runtime_error_is_terminal(self) -> None:
        error_event = {
            "id": "evt-error",
            "kind": "ConversationErrorEvent",
            "code": "APIError",
            "detail": "litellm.APIError: Cannot connect to host openrouter.ai:443",
        }
        status_event = {
            "id": "evt-status-error",
            "kind": "ConversationStateUpdateEvent",
            "key": "execution_status",
            "value": "error",
        }

        class ErrorAdapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(OpenHandsRuntimeConfig(enabled=False))

            def _request_json(self, method: str, url: str, *, payload: object = None, timeout: float = 10.0, **kwargs: object) -> tuple[JsonDict, JsonDict]:
                if method == "GET" and "events/search" in url:
                    return {"items": [error_event, status_event]}, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = ErrorAdapter()

        answer, events, runtime_error, paused_approval_id = adapter._poll_openhands_answer(
            "http://openhands.example/api/conversations",
            "conversation-1",
            tool_executor=None,
            poll_seconds=0.2,
        )

        self.assertEqual(answer, "")
        self.assertIn("Cannot connect to host openrouter.ai:443", runtime_error)
        self.assertEqual(paused_approval_id, "")
        self.assertTrue(any(event.get("payload", {}).get("category") == "error" for event in events))
        self.assertTrue(any("Cannot connect to host openrouter.ai:443" in event.get("payload", {}).get("summary", "") for event in events))

    def test_ui_agent_openhands_tool_result_delivery_timeout_is_recorded(self) -> None:
        class TimeoutPostingAdapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(OpenHandsRuntimeConfig(enabled=False))

            def _request_json(self, method: str, url: str, *, payload: object = None, timeout: float = 10.0, **kwargs: object) -> tuple[JsonDict, JsonDict]:
                if method == "POST" and url.endswith("/events"):
                    raise TimeoutError("timed out while posting tool result")
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = TimeoutPostingAdapter()
        handled: set[str] = set()
        executed: List[str] = []
        tool_call_event = {
            "id": "evt-tool-call",
            "kind": "ActionEvent",
            "source": "agent",
            "tool_name": "optpilot_workspace_list",
            "tool_call_id": "tool-call-timeout",
            "action": {"kind": "optpilot_workspace_list"},
        }

        events, paused_approval_id = adapter._execute_openhands_client_tools(
            [tool_call_event],
            "http://openhands.example/api/conversations",
            "conversation-1",
            lambda tool_name, arguments: executed.append(tool_name) or {"ok": True, "summary": "Listed workspaces."},
            handled,
        )

        self.assertEqual(executed, ["optpilot_workspace_list"])
        self.assertEqual(handled, {"tool-call-timeout"})
        self.assertEqual(paused_approval_id, "")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["tool_call_id"], "tool-call-timeout")
        self.assertEqual(events[0]["payload"]["delivery_status"], "timeout")
        self.assertIn("timed out", events[0]["payload"]["delivery_error"])
        self.assertEqual(events[0]["payload"]["result"]["summary"], "Listed workspaces.")

    def test_ui_agent_submit_tool_result_confirms_delivery_after_timeout(self) -> None:
        class ConfirmedTimeoutAdapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(
                    OpenHandsRuntimeConfig(
                        enabled=True,
                        base_url="http://openhands.example",
                        session_endpoint="/api/conversations",
                        model="test/model",
                        api_key="sk-test",
                    )
                )

            def status(self) -> JsonDict:
                return {"dispatch": "openhands_http"}

            def _request_json(self, method: str, url: str, *, payload: object = None, timeout: float = 10.0, **kwargs: object) -> tuple[JsonDict, JsonDict]:
                if method == "POST" and url.endswith("/events"):
                    raise TimeoutError("timed out while posting approved tool result")
                if method == "GET" and "events/search" in url:
                    return {
                        "items": [
                            {
                                "kind": "MessageEvent",
                                "source": "user",
                                "llm_message": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "OptPilot tool result for optpilot_package_plan_smoke (call-smoke-1). Use this structured result to continue the task.",
                                        }
                                    ],
                                },
                            }
                        ]
                    }, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = ConfirmedTimeoutAdapter()

        result = adapter.submit_tool_result("conversation-1", "optpilot_package_plan_smoke", "call-smoke-1", {"ok": False})

        self.assertTrue(result["sent"])
        self.assertEqual(result["delivery_status"], "confirmed_after_timeout")

    def test_ui_agent_openhands_tool_result_timeout_can_confirm_delivery(self) -> None:
        class ConfirmedToolTimeoutAdapter(OpenHandsAdapter):
            def __init__(self) -> None:
                super().__init__(OpenHandsRuntimeConfig(enabled=False))

            def _request_json(self, method: str, url: str, *, payload: object = None, timeout: float = 10.0, **kwargs: object) -> tuple[JsonDict, JsonDict]:
                if method == "POST" and url.endswith("/events"):
                    raise TimeoutError("timed out while posting tool result")
                if method == "GET" and "events/search" in url:
                    return {
                        "items": [
                            {
                                "kind": "MessageEvent",
                                "source": "user",
                                "llm_message": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "OptPilot tool result for optpilot_workspace_list (tool-call-confirmed). Use this structured result to continue the task.",
                                        }
                                    ],
                                },
                            }
                        ]
                    }, {}
                raise AssertionError(f"unexpected request: {method} {url}")

        adapter = ConfirmedToolTimeoutAdapter()
        events, paused_approval_id = adapter._execute_openhands_client_tools(
            [
                {
                    "id": "evt-tool-call",
                    "kind": "ActionEvent",
                    "source": "agent",
                    "tool_name": "optpilot_workspace_list",
                    "tool_call_id": "tool-call-confirmed",
                    "action": {"kind": "optpilot_workspace_list"},
                }
            ],
            "http://openhands.example/api/conversations",
            "conversation-1",
            lambda tool_name, arguments: {"ok": True, "tool": tool_name, "summary": "Listed workspaces."},
            set(),
        )

        self.assertEqual(paused_approval_id, "")
        self.assertEqual(events[0]["payload"]["delivery_status"], "confirmed_after_timeout")
        self.assertNotIn("result", events[0]["payload"])

    def test_ui_agent_session_retries_timed_out_tool_result_forwarding(self) -> None:
        class RetryAdapter:
            def __init__(self) -> None:
                self.forwarded: List[JsonDict] = []

            def submit_tool_result(self, conversation_id: str, name: str, call_id: str, result: JsonDict) -> JsonDict:
                self.forwarded.append(
                    {
                        "conversation_id": conversation_id,
                        "name": name,
                        "call_id": call_id,
                        "result": result,
                    }
                )
                return {"sent": True, "conversation_id": conversation_id, "tool_call_id": call_id}

            def sync_conversation(self, conversation_id: str, **kwargs: object) -> JsonDict:
                return {
                    "status": "answered",
                    "conversation_id": conversation_id,
                    "assistant_message": {
                        "role": "assistant",
                        "title": "OpenHands",
                        "content": "Recovered after retrying the stored tool result.",
                    },
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            adapter = RetryAdapter()
            state.agent_adapter = adapter
            session = _create_agent_session(state, {"title": "Retry forwarding"})
            session["status"] = "waiting_for_agent"
            session["openhands_conversation_id"] = "conversation-retry"
            session["active_turn_id"] = "turn-retry"
            session["active_turn_started_at"] = "2026-07-06T08:20:03Z"
            _upsert_agent_session(state, session)
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "id": "optpilot-tool-result-call-retry",
                    "type": "optpilot_tool_result",
                    "created_at": "2026-07-06T08:20:04Z",
                    "payload": {
                        "tool": "optpilot_file_read",
                        "tool_call_id": "call-retry",
                        "ok": True,
                        "summary": "Read file.",
                        "delivery_status": "timeout",
                        "delivery_error": "timed out",
                        "result": {"ok": True, "tool": "optpilot_file_read", "summary": "Read file.", "data": {"content": "hello"}},
                    },
                },
            )

            synced = _sync_agent_session(state, session["id"])
            events = _read_agent_events(state, session["id"])
            messages = _read_agent_messages(state, session["id"])

        self.assertEqual(synced["status"], "idle")
        self.assertEqual(adapter.forwarded[0]["call_id"], "call-retry")
        self.assertEqual(adapter.forwarded[0]["result"]["data"]["content"], "hello")
        self.assertTrue(
            any(
                event["type"] == "openhands_tool_result_forwarded"
                and event.get("payload", {}).get("retry")
                and event.get("payload", {}).get("tool_call_id") == "call-retry"
                for event in events
            )
        )
        self.assertTrue(any(message["content"] == "Recovered after retrying the stored tool result." for message in messages))

    def test_ui_agent_messages_hide_malformed_context_echoes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Malformed echo"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {
                    "role": "assistant",
                    "title": "OpenHands",
                    "content": 'User request: hello\n\nVisible OptPilot Studio context packet:\n{"current_page": "runs"}',
                },
            )
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {"role": "assistant", "title": "OpenHands", "content": "Real answer."},
            )

            messages = _read_agent_messages(state, session["id"])

        self.assertFalse(any("Visible OptPilot Studio context packet" in message["content"] for message in messages))
        self.assertTrue(any(message["content"] == "Real answer." for message in messages))

    def test_ui_agent_session_recovers_finish_message_from_openhands_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Recovered finish"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {
                    "role": "assistant",
                    "title": "OpenHands",
                    "content": "The user still hasn't sent a new message. </think> (Waiting for your next message.)",
                },
            )
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "id": "openhands-event-finish",
                    "type": "openhands_event",
                    "created_at": "2026-06-24T05:22:59Z",
                    "payload": {
                        "category": "tool_call",
                        "tool": "finish",
                        "arguments_preview": json.dumps({"message": "Use the host runtime for this project."}),
                    },
                },
            )

            persisted = _agent_session_by_id(state, session["id"])

        contents = [message["content"] for message in persisted["messages"] if message["role"] == "assistant"]
        self.assertIn("Use the host runtime for this project.", contents)

    def test_ui_agent_events_hide_internal_context_packet_previews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Step redaction"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "type": "openhands_event",
                    "payload": {
                        "event_type": "MessageEvent",
                        "summary": 'User request:\nhello\n\nVisible OptPilot Studio context packet:\n{"current_page": "runs"}',
                        "raw_preview": '"text": "User request:\\nhello\\n\\nVisible OptPilot Studio context packet:\\n{\\"current_page\\": \\"runs\\"}"',
                    },
                },
            )

            events = _read_agent_events(state, session["id"])

        self.assertEqual(events[1]["payload"]["summary"], "User request sent to OpenHands: hello")
        self.assertNotIn("current_page", events[1]["payload"]["summary"])
        self.assertNotIn("current_page", events[1]["payload"]["raw_preview"])

    def test_ui_agent_events_hide_plain_openhands_assistant_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Plain message"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "type": "openhands_event",
                    "payload": {
                        "category": "assistant_message",
                        "summary": "I need to wait for the three file read results before proceeding.",
                        "assistant_preview": "I need to wait for the three file read results before proceeding.",
                    },
                },
            )

            events = _read_agent_events(state, session["id"])

        self.assertFalse(
            any(
                event.get("payload", {}).get("summary")
                == "I need to wait for the three file read results before proceeding."
                for event in events
            )
        )

    def test_ui_agent_events_redact_internal_sync_state_from_result_previews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Tool result preview"})
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "type": "optpilot_tool_result",
                    "payload": {
                        "tool": "optpilot_workspace_list",
                        "result_preview": json.dumps(
                            {
                                "ok": True,
                                "data": {
                                    "sessions": [
                                        {
                                            "id": "as_old",
                                            "openhands_pending_sync": {
                                                "ignored_response_texts": [
                                                    "I need to wait for the three file read results before proceeding."
                                                ]
                                            },
                                        }
                                    ]
                                },
                            }
                        ),
                    },
                },
            )
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "events.jsonl",
                {
                    "type": "openhands_event",
                    "payload": {
                        "category": "tool_result_feedback",
                        "raw_preview": (
                            'OptPilot tool result: {\\"openhands_pending_sync\\": {'
                            '\\"ignored_response_texts\\": ['
                            '\\"I need to wait for the three file read results before proceeding.\\"'
                            "]}, \\\"status\\\": \\\"waiting_for_agent\\\"}"
                        ),
                    },
                },
            )

            events = _read_agent_events(state, session["id"])

        preview = events[1]["payload"]["result_preview"]
        raw_preview = events[2]["payload"]["raw_preview"]
        self.assertNotIn("openhands_pending_sync", preview)
        self.assertNotIn("I need to wait for the three file read results before proceeding.", preview)
        self.assertIn('"id": "as_old"', preview)
        self.assertNotIn("openhands_pending_sync", raw_preview)
        self.assertNotIn("I need to wait for the three file read results before proceeding.", raw_preview)

    def test_ui_agent_session_payload_hides_internal_sync_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Internal sync"})
            session["openhands_pending_sync"] = {
                "ignored_response_texts": ["I need to wait for the file read results before proceeding."]
            }
            _upsert_agent_session(state, session)

            payload = _agent_session_by_id(state, session["id"])

        self.assertNotIn("openhands_pending_sync", payload)
        self.assertNotIn("I need to wait for the file read results before proceeding.", json.dumps(payload))

    def test_ui_agent_session_running_dispatch_does_not_store_placeholder_answer(self) -> None:
        class SlowAdapter:
            def status(self) -> JsonDict:
                return {"runtime": "openhands", "available_tools": []}

            def context_packet(self, **kwargs: object) -> JsonDict:
                return dict(kwargs)

            def dispatch_message(self, **kwargs: object) -> JsonDict:
                return {
                    "status": "running",
                    "dispatch": "openhands_http",
                    "conversation_id": "slow-conversation",
                    "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                    "events": [{"type": "openhands_dispatch_started", "payload": {"conversation_id": "slow-conversation"}}],
                }

            def sync_conversation(self, conversation_id: str, **kwargs: object) -> JsonDict:
                return {
                    "status": "answered",
                    "conversation_id": conversation_id,
                    "assistant_message": {"role": "assistant", "title": "OpenHands", "content": "Late OpenHands answer."},
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            state.agent_adapter = SlowAdapter()
            session = _create_agent_session(state, {"title": "Slow OpenHands"})
            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "title": "User", "content": "Slow question", "ui_context": {"current_page": "workspace"}},
            )
            _append_jsonl(
                state.agent_sessions_dir / session["id"] / "messages.jsonl",
                {"role": "assistant", "title": "OpenHands", "content": "Message sent to OpenHands. Refresh the assistant session to see later events."},
            )
            messages_after_dispatch = _read_agent_messages(state, session["id"])
            synced = _sync_agent_session(state, session["id"])
            messages_after_sync = _read_agent_messages(state, session["id"])

        self.assertEqual(result["session"]["status"], "waiting_for_agent")
        self.assertFalse(any("Message sent to OpenHands" in message["content"] for message in messages_after_dispatch))
        self.assertFalse(any(message["role"] == "assistant" and message["content"] == "" for message in messages_after_dispatch))
        self.assertEqual(synced["status"], "idle")
        self.assertTrue(any(message["content"] == "Late OpenHands answer." for message in messages_after_sync))

    def test_ui_agent_session_sync_marks_openhands_error_terminal(self) -> None:
        class ErrorSyncAdapter:
            def sync_conversation(self, conversation_id: str, **kwargs: object) -> JsonDict:
                return {
                    "status": "failed",
                    "conversation_id": conversation_id,
                    "assistant_message": {
                        "role": "assistant",
                        "title": "OpenHands error",
                        "content": "OpenHands reported an error: APIError: Cannot connect to host openrouter.ai:443",
                    },
                    "events": [
                        {
                            "type": "openhands_event",
                            "payload": {
                                "category": "error",
                                "summary": "APIError: Cannot connect to host openrouter.ai:443",
                            },
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            state.agent_adapter = ErrorSyncAdapter()
            session = _create_agent_session(state, {"title": "OpenHands error"})
            session["status"] = "waiting_for_agent"
            session["openhands_conversation_id"] = "conversation-error"
            session["active_turn_id"] = "turn-error"
            session["active_turn_started_at"] = "2026-07-06T08:20:03Z"
            _upsert_agent_session(state, session)

            synced = _sync_agent_session(state, session["id"])
            messages = _read_agent_messages(state, session["id"])
            events = _read_agent_events(state, session["id"])

        self.assertEqual(synced["status"], "error")
        self.assertNotIn("active_turn_id", synced)
        self.assertTrue(any(message["title"] == "OpenHands error" for message in messages))
        self.assertTrue(any(event.get("payload", {}).get("category") == "error" for event in events))

    def test_ui_agent_sync_serializes_tool_execution_for_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Concurrent sync"})
            session["status"] = "waiting_for_agent"
            session["openhands_conversation_id"] = "conversation-with-tool-call"
            session["active_turn_id"] = "turn-active"
            session["active_turn_started_at"] = "2026-06-30T00:00:00Z"
            _upsert_agent_session(state, session)

            class ReplayToolAdapter:
                def __init__(self) -> None:
                    self.lock = threading.Lock()
                    self.active = 0
                    self.max_active = 0
                    self.ignored_snapshots: List[set[str]] = []
                    self.tool_executions = 0
                    self.first_sync_entered = threading.Event()

                def sync_conversation(self, conversation_id: str, **kwargs: object) -> JsonDict:
                    with self.lock:
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                        ignored = set(kwargs.get("ignored_tool_calls") or set())
                        self.ignored_snapshots.append(ignored)
                        self.first_sync_entered.set()
                    try:
                        time.sleep(0.15)
                        events = []
                        if "tool-call-1" not in ignored:
                            tool_executor = kwargs["tool_executor"]
                            tool_executor("optpilot_workspace_list", {"_openhands_tool_call_id": "tool-call-1"})
                            self.tool_executions += 1
                            events.append(
                                {
                                    "type": "optpilot_tool_result",
                                    "payload": {
                                        "tool": "optpilot_workspace_list",
                                        "tool_call_id": "tool-call-1",
                                        "ok": True,
                                        "summary": "Listed workspaces.",
                                    },
                                }
                            )
                        return {
                            "status": "running",
                            "conversation_id": conversation_id,
                            "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                            "events": events,
                        }
                    finally:
                        with self.lock:
                            self.active -= 1

            adapter = ReplayToolAdapter()
            state.agent_adapter = adapter
            results: List[JsonDict] = []
            errors: List[BaseException] = []

            def run_sync() -> None:
                try:
                    results.append(_sync_agent_session(state, session["id"]))
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            first = threading.Thread(target=run_sync)
            second = threading.Thread(target=run_sync)
            first.start()
            self.assertTrue(adapter.first_sync_entered.wait(timeout=1.0))
            second.start()
            first.join(timeout=2.0)
            second.join(timeout=2.0)

            events = _read_agent_events(state, session["id"])

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(adapter.max_active, 1)
        self.assertEqual(adapter.tool_executions, 1)
        self.assertEqual(adapter.ignored_snapshots[0], set())
        self.assertIn("tool-call-1", adapter.ignored_snapshots[1])
        tool_result_events = [
            event
            for event in events
            if event.get("type") == "optpilot_tool_result"
            and event.get("payload", {}).get("tool_call_id") == "tool-call-1"
        ]
        self.assertEqual(len(tool_result_events), 1)

    def test_ui_agent_session_cancel_interrupts_and_ignores_late_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Cancel OpenHands"})

            class CancellingAdapter:
                def status(self) -> JsonDict:
                    return {"runtime": "openhands", "dispatch": "openhands_http", "available_tools": []}

                def context_packet(self, **kwargs: object) -> JsonDict:
                    return dict(kwargs)

                def dispatch_message(self, **kwargs: object) -> JsonDict:
                    return {
                        "status": "running",
                        "dispatch": "openhands_http",
                        "conversation_id": "slow-conversation",
                        "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                        "events": [{"type": "openhands_dispatch_started", "payload": {"conversation_id": "slow-conversation"}}],
                    }

                def sync_conversation(self, conversation_id: str, **kwargs: object) -> JsonDict:
                    _cancel_agent_session(state, session["id"])
                    return {
                        "status": "answered",
                        "conversation_id": conversation_id,
                        "assistant_message": {"role": "assistant", "title": "OpenHands", "content": "Late answer after cancel."},
                        "events": [{"type": "openhands_dispatch_completed", "payload": {"conversation_id": conversation_id}}],
                    }

                def cancel_conversation(self, conversation_id: str) -> JsonDict:
                    return {"cancelled": True, "action": "interrupt", "conversation_id": conversation_id}

            state.agent_adapter = CancellingAdapter()
            result = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "title": "User", "content": "Please do a slow task.", "ui_context": {"current_page": "workspace"}},
            )
            synced = _sync_agent_session(state, session["id"])
            messages_after_sync = _read_agent_messages(state, session["id"])
            events_after_sync = _read_agent_events(state, session["id"])

        self.assertEqual(result["session"]["status"], "waiting_for_agent")
        self.assertEqual(synced["status"], "idle")
        self.assertFalse(any(message["content"] == "Late answer after cancel." for message in messages_after_sync))
        self.assertTrue(any(event["type"] == "openhands_dispatch_cancelled" for event in events_after_sync))

    def test_ui_agent_session_cancel_returns_before_remote_openhands_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Nonblocking cancel"})
            remote_cancel_started = threading.Event()
            remote_cancel_release = threading.Event()

            class BlockingCancelAdapter:
                def status(self) -> JsonDict:
                    return {"runtime": "openhands", "dispatch": "openhands_http", "available_tools": []}

                def context_packet(self, **kwargs: object) -> JsonDict:
                    return dict(kwargs)

                def dispatch_message(self, **kwargs: object) -> JsonDict:
                    return {
                        "status": "running",
                        "dispatch": "openhands_http",
                        "conversation_id": "blocking-conversation",
                        "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                        "events": [{"type": "openhands_dispatch_started", "payload": {"conversation_id": "blocking-conversation"}}],
                    }

                def cancel_conversation(self, conversation_id: str) -> JsonDict:
                    remote_cancel_started.set()
                    remote_cancel_release.wait(timeout=2)
                    return {"cancelled": True, "action": "interrupt", "conversation_id": conversation_id}

            state.agent_adapter = BlockingCancelAdapter()
            dispatched = _append_agent_message(
                state,
                session["id"],
                {"role": "user", "title": "User", "content": "Please do a slow task.", "ui_context": {"current_page": "workspace"}},
            )

            started_at = time.monotonic()
            try:
                cancelled = _cancel_agent_session(state, session["id"])
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.5)
                self.assertEqual(dispatched["session"]["status"], "waiting_for_agent")
                self.assertEqual(cancelled["status"], "idle")
                self.assertEqual(cancelled.get("cancelled_turn_id"), dispatched["session"].get("active_turn_id"))
                self.assertTrue(remote_cancel_started.wait(timeout=0.5))
                events = _read_agent_events(state, session["id"])
                cancel_events = [event for event in events if event["type"] == "openhands_dispatch_cancelled"]
                self.assertTrue(cancel_events)
                self.assertTrue(cancel_events[-1]["payload"]["remote_cancel_scheduled"])
            finally:
                remote_cancel_release.set()

            for _ in range(20):
                events = _read_agent_events(state, session["id"])
                if any(event["type"] == "openhands_cancel_acknowledged" for event in events):
                    break
                time.sleep(0.01)
            self.assertTrue(any(event["type"] == "openhands_cancel_acknowledged" for event in events))

    def test_optpilot_assistant_prompt_is_loaded_from_agent_folder(self) -> None:
        prompt = load_assistant_system_prompt()

        self.assertIn("OptPilot Assistant", prompt)
        self.assertIn("evaluator.settings", prompt)
        self.assertIn("methodContext.references", prompt)
        self.assertIn("Catalog list and search results as evidence", prompt)
        self.assertIn("Conversation's default Workspace", prompt)
        self.assertRegex(prompt, r"bounded recent Conversation\s+context")
        self.assertIn("Studio-backed workspace tools", prompt)
        self.assertIn("emit UI cards only", FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT)
        self.assertRegex(
            FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT,
            r"Conversation's default\s+Workspace",
        )
        self.assertRegex(
            FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT,
            r"bounded recent\s+Conversation context",
        )

    def test_packaged_release_assets_mirror_source_docs_and_agent_files(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        docs_root = repo_root / "docs"
        docs_assets_root = repo_root / "studio" / "src" / "optpilot_studio" / "docs_assets"
        source_docs = sorted(
            path
            for path in docs_root.glob("*.md")
            if path.is_file()
            and path.stem == path.stem.lower()
            and path.stem.replace("-", "").isalnum()
        )
        packaged_docs = sorted(path.name for path in docs_assets_root.glob("*.md"))

        self.assertEqual([path.name for path in source_docs], packaged_docs)
        for source in source_docs:
            packaged = docs_assets_root / source.name
            self.assertEqual(source.read_text(encoding="utf-8"), packaged.read_text(encoding="utf-8"), source.name)

        agent_asset_pairs = [
            (repo_root / ".agents" / "optpilot-assistant" / "README.md", repo_root / "studio" / "src" / "optpilot_studio" / "assistant_assets" / "README.md"),
            (
                repo_root / ".agents" / "optpilot-assistant" / "prompts" / "system.md",
                repo_root / "studio" / "src" / "optpilot_studio" / "assistant_assets" / "prompts" / "system.md",
            ),
            (
                repo_root / ".agents" / "optpilot-assistant" / "implementation" / "bridge.md",
                repo_root / "studio" / "src" / "optpilot_studio" / "assistant_assets" / "implementation" / "bridge.md",
            ),
        ]
        for source, packaged in agent_asset_pairs:
            self.assertEqual(source.read_text(encoding="utf-8"), packaged.read_text(encoding="utf-8"), source.name)

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_openhands_status_reports_reachable_agent_server(self) -> None:
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{\"title\":\"OpenHands Agent Server\"}")

            def log_message(self, format, *args):  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = OpenHandsAdapter(
                OpenHandsRuntimeConfig(
                    enabled=True,
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    session_endpoint="/api/conversations",
                    model="gpt-test",
                    api_key="sk-test",
                )
            )
            status = adapter.status()
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(status["dispatch"], "openhands_http")
        self.assertTrue(status["connected"])

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_openhands_cancel_conversation_uses_interrupt_endpoint(self) -> None:
        calls: List[str] = []

        class CancelHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):  # noqa: N802
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{\"success\":true}")

            def log_message(self, format, *args):  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), CancelHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = OpenHandsAdapter(
                OpenHandsRuntimeConfig(
                    enabled=True,
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    session_endpoint="/api/conversations",
                    model="gpt-test",
                    api_key="sk-test",
                )
            )
            result = adapter.cancel_conversation("12345678-1234-5678-1234-567812345678")
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(result["cancelled"])
        self.assertEqual(result["action"], "interrupt")
        self.assertEqual(calls, ["/api/conversations/12345678-1234-5678-1234-567812345678/interrupt"])

    def test_ui_agent_settings_store_openhands_config_without_echoing_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPTPILOT_OPENHANDS_API_KEY": "",
                "LLM_API_KEY": "",
                "OPENAI_API_KEY": "",
                "OPTPILOT_OPENHANDS_URL": "",
                "OPTPILOT_OPENHANDS_MODEL": "",
                "LLM_MODEL": "",
            },
        ), tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])

            result = _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:3000/",
                        "session_endpoint": "/api/conversations",
                        "model": "gpt-test",
                        "api_key": "sk-test-secret",
                    }
                },
            )
            settings = _agent_settings_payload(state)
            stored = json.loads((tmp_path / ".optpilot-ui" / "settings.json").read_text(encoding="utf-8"))

        openhands = result["settings"]["assistant"]["openhands"]
        self.assertTrue(openhands["api_key_configured"])
        self.assertNotIn("api_key", openhands)
        self.assertEqual(result["status"]["mode"], "configured")
        self.assertEqual(settings["status"]["model"], "gpt-test")
        self.assertEqual(stored["assistant"]["openhands"]["api_key"], "sk-test-secret")

    def test_ui_agent_settings_can_clear_openhands_api_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPTPILOT_OPENHANDS_API_KEY": "",
                "LLM_API_KEY": "",
                "OPENAI_API_KEY": "",
                "OPTPILOT_OPENHANDS_URL": "",
                "OPTPILOT_OPENHANDS_MODEL": "",
                "LLM_MODEL": "",
            },
        ), tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:3000",
                        "model": "gpt-test",
                        "api_key": "sk-test-secret",
                    }
                },
            )
            result = _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:3000",
                        "model": "gpt-test",
                        "clear_api_key": True,
                    }
                },
            )
            stored = json.loads((tmp_path / ".optpilot-ui" / "settings.json").read_text(encoding="utf-8"))

        self.assertFalse(result["settings"]["assistant"]["openhands"]["api_key_configured"])
        self.assertEqual(result["status"]["mode"], "missing API key")
        self.assertEqual(stored["assistant"]["openhands"]["api_key"], "")

    def test_ui_settings_store_environment_variables_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])

            result = _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False, "base_url": "", "model": ""},
                    "environment": {
                        "set": [{"name": "OPENROUTER_API_KEY", "value": "sk-secret"}],
                    },
                },
            )
            payload = _agent_settings_payload(state)
            stored = json.loads((tmp_path / ".optpilot-ui" / "settings.json").read_text(encoding="utf-8"))

            variable = result["settings"]["environment"]["variables"][0]
            self.assertEqual(variable, {"name": "OPENROUTER_API_KEY", "configured": True})
            self.assertNotIn("sk-secret", json.dumps(payload))
            self.assertEqual(stored["environment"]["variables"]["OPENROUTER_API_KEY"], "sk-secret")

            cleared = _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False, "base_url": "", "model": ""},
                    "environment": {"clear": ["OPENROUTER_API_KEY"]},
                },
            )

        self.assertEqual(cleared["settings"]["environment"]["variables"], [])

    def test_ui_resolves_declared_env_from_studio_settings_before_host(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "host-value"}, clear=False), tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False, "base_url": "", "model": ""},
                    "environment": {
                        "set": [{"name": "OPENROUTER_API_KEY", "value": "studio-value"}],
                    },
                },
            )

            resolved = _require_declared_env_from_host(state, ["OPENROUTER_API_KEY"], action="test")

        self.assertEqual(resolved["OPENROUTER_API_KEY"], "studio-value")

    def test_ui_agent_settings_persist_openhands_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            session = _create_agent_session(state, {"title": "Capabilities"})

            result = _update_agent_settings(
                state,
                {
                    "openhands": {
                        "enabled": False,
                        "native_tools": ["grep", "terminal", "file_editor", "glob", "grep", "task_tracker"],
                    },
                    "capabilities": {
                        "skills": [
                            {
                                "name": "connect-github-integration",
                                "source": ".agents/skills/connect-github-integration",
                                "triggers": ["github", "integration"],
                                "enabled": True,
                            }
                        ],
                        "mcp_servers": [
                            {"name": "Notion", "url": "https://mcp.notion.com/mcp", "auth": "oauth"}
                        ],
                        "mcp_filter_regex": "^(notion|optpilot)_",
                        "custom_tools": [
                            {
                                "name": "grep",
                                "module": "optpilot.tools.grep",
                                "factory": "GrepTool",
                                "tool_name": "grep",
                                "approval_required": True,
                            }
                        ],
                    },
                    "permissions": {
                        "file_write": "approval_required",
                        "shell_run": "disabled",
                        "catalog_registration": "approval_required",
                        "study_launch": "approval_required",
                        "job_stop": "approval_required",
                    },
                },
            )
            list_result = _execute_agent_tool(state, session["id"], "optpilot_capability_list", {})
            detail_result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_capability_detail",
                {"capability_kind": "custom_tool", "id": "grep"},
            )
            context = _agent_context_packet(state, session, {"current_page": "workspace"})
            stored = json.loads((tmp_path / ".optpilot-ui" / "settings.json").read_text(encoding="utf-8"))

        assistant = result["settings"]["assistant"]
        self.assertEqual(assistant["openhands"]["native_tools"], ["grep", "glob", "task_tracker"])
        self.assertEqual(assistant["capabilities"]["skills"][0]["source"], ".agents/skills/connect-github-integration")
        self.assertEqual(assistant["capabilities"]["mcp_servers"][0]["url"], "https://mcp.notion.com/mcp")
        self.assertEqual(assistant["capabilities"]["mcp_filter_regex"], "^(notion|optpilot)_")
        self.assertEqual(assistant["capabilities"]["custom_tools"][0]["factory"], "GrepTool")
        self.assertEqual(assistant["permissions"]["shell_run"], "disabled")
        self.assertTrue(list_result["ok"])
        self.assertIn("custom_tools", list_result["data"]["capabilities"])
        self.assertEqual(detail_result["data"]["capability"]["module"], "optpilot.tools.grep")
        self.assertEqual(context["assistant_capabilities"]["counts"]["skills"]["enabled"], 1)
        self.assertEqual(context["assistant_capabilities"]["permissions"]["file_write"], "approval_required")
        self.assertEqual(stored["assistant"]["capabilities"]["custom_tools"][0]["tool_name"], "grep")

    def test_ui_code_server_detects_standalone_install_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            executable = tmp_path / ".optpilot-ui" / "code-server-standalone" / "lib" / "code-server-4.125.0" / "bin" / "code-server"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")

            detected = _local_code_server_executable(tmp_path)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])

        self.assertEqual(detected.resolve(), executable.resolve())
        self.assertEqual(Path(state.code_server.options.executable or "").resolve(), executable.resolve())

    def test_ui_workspace_runtime_defaults_to_packaged_dev_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
            status = state.workspace_runtime.global_status()

        self.assertEqual(status["image"], "optpilot/workspace-dev:latest")
        self.assertTrue(status["build_image"])
        self.assertTrue(status["dockerfile"].endswith("workspace_runtime/Dockerfile"))
        self.assertEqual(status["runtime"]["cpu_limit"], "2")
        self.assertEqual(status["runtime"]["memory_limit"], "4g")
        self.assertEqual(status["runtime"]["pids_limit"], 1024)

    @unittest.skipUnless(_LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind")
    def test_ui_code_server_status_rejects_non_code_server_port_conflict(self) -> None:
        class FakeOptPilotHandler(BaseHTTPRequestHandler):
            server_version = "OptPilotUI/0.1"

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:
                return

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOptPilotHandler)
            port = fake_server.server_address[1]
            thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
            thread.start()
            try:
                state = UiState(
                    cwd=tmp_path,
                    catalog_roots=[],
                    run_roots=[],
                    workspace_runtime=WorkspaceRuntimeOptions(port_start=port),
                )
                status = state.code_server_status()
            finally:
                fake_server.shutdown()
                fake_server.server_close()

        self.assertFalse(status["running"])
        self.assertTrue(status["port_conflict"])

    def test_ui_code_server_starts_inside_workspace_container_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19100,
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Runtime workspace", "root": str(tmp_path / "runtime-ws")})

            result = state.start_code_server(Path(workspace["root"]))
            settings = json.loads((Path(result["user_data_dir"]) / "User" / "settings.json").read_text(encoding="utf-8"))
            calls = _fake_workspace_container_calls(tmp_path)

        self.assertTrue(result["managed"], result)
        self.assertTrue(result["containerized"], result)
        self.assertEqual(result["workspace_id"], workspace["id"])
        self.assertEqual(result["runtime"]["executor"], "container")
        self.assertIn("?folder=", result["open_url"])
        self.assertTrue(result["layout_persistent"], result)
        self.assertIn(workspace["id"], result["user_data_dir"])
        self.assertEqual(settings["window.menuBarVisibility"], "classic")
        self.assertEqual(settings["workbench.activityBar.location"], "hidden")
        self.assertEqual(settings["workbench.panel.defaultLocation"], "bottom")
        self.assertFalse(settings["workbench.statusBar.visible"])
        self.assertTrue(any(call and call[0] == "run" and "fake-code-server:latest" in call for call in calls), calls)
        self.assertTrue(any(call and call[0] == "run" and any(item.endswith(":rw") for item in call) for call in calls), calls)
        self.assertTrue(any(call and call[0] == "run" and "--cpus" in call and "2" in call for call in calls), calls)
        self.assertTrue(any(call and call[0] == "run" and "--memory" in call and "4g" in call for call in calls), calls)
        self.assertTrue(any(call and call[0] == "run" and "--pids-limit" in call and "1024" in call for call in calls), calls)
        self.assertTrue(any(call and call[0] == "run" and "no-new-privileges" in call for call in calls), calls)
        self.assertTrue(any(call and call[0] == "exec" and "code-server" in " ".join(call) for call in calls), calls)
        self.assertTrue(any(call and call[0] == "exec" and "--user-data-dir" in " ".join(call) for call in calls), calls)

    def test_ui_code_server_stop_does_not_stop_workspace_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19120,
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Runtime workspace", "root": str(tmp_path / "runtime-ws")})

            state.start_code_server(Path(workspace["root"]))
            calls_after_start = _fake_workspace_container_calls(tmp_path)
            state.stop_code_server()
            calls = _fake_workspace_container_calls(tmp_path)

        new_calls = calls[len(calls_after_start):]
        self.assertFalse(any(call and call[0] == "rm" for call in new_calls), calls)

    def test_ui_workspace_preview_uses_workspace_code_server_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19130,
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Preview workspace", "root": str(tmp_path / "preview-ws")})

            lease = type(
                "FakePresentationLease",
                (),
                {
                    "preview_url": "http://127.0.0.1:29999/?__optpilot_presentation_token=test",
                },
            )()
            with patch.object(state.presentation_broker, "open", return_value=lease):
                result = state.workspace_preview_open(Path(workspace["root"]), 5173, extra_ports=[8000])
            calls = _fake_workspace_container_calls(tmp_path)

        self.assertEqual(result["workspace_id"], workspace["id"])
        self.assertEqual(result["port"], 5173)
        self.assertEqual(result["proxy"], "studio")
        preview_url = urlparse(result["preview_url"])
        self.assertEqual(preview_url.scheme, "http")
        self.assertEqual(preview_url.hostname, "127.0.0.1")
        self.assertIn("__optpilot_presentation_token", parse_qs(preview_url.query))
        self.assertIn("/proxy/5173/", result["proxy_target"])
        self.assertEqual(result["allowed_ports"], [5173, 8000])
        self.assertTrue(result["code_server"]["layout_persistent"])
        self.assertTrue(any(call and call[0] == "exec" and "code-server" in " ".join(call) for call in calls), calls)

    def test_ui_agent_tool_opens_workspace_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19160,
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Agent preview workspace", "root": str(tmp_path / "agent-preview-ws")})
            session = _create_agent_session(state, {"title": "Preview agent"})
            _attach_agent_workspace(state, session["id"], workspace["id"], select=True)

            lease = type(
                "FakePresentationLease",
                (),
                {
                    "preview_url": "http://127.0.0.1:29999/?__optpilot_presentation_token=test",
                },
            )()
            with patch.object(state.presentation_broker, "open", return_value=lease):
                result = _execute_agent_tool(
                    state,
                    session["id"],
                    "optpilot_workspace_preview_open",
                    {"workspace_id": workspace["id"], "port": 3000, "extra_ports": [8000]},
                )
            calls = _fake_workspace_container_calls(tmp_path)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tool"], "optpilot_workspace_preview_open")
        self.assertEqual(result["data"]["workspace_id"], workspace["id"])
        self.assertEqual(result["data"]["port"], 3000)
        preview_url = urlparse(result["data"]["preview_url"])
        self.assertEqual(preview_url.scheme, "http")
        self.assertIn("__optpilot_presentation_token", parse_qs(preview_url.query))
        self.assertIn("/proxy/3000/", result["data"]["proxy_target"])
        self.assertEqual(result["data"]["allowed_ports"], [3000, 8000])
        self.assertTrue(any(call and call[0] == "exec" and "code-server" in " ".join(call) for call in calls), calls)

    def test_workspace_runtime_rejects_images_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="untrusted/workspace:latest",
                    image_allowlist_patterns=["trusted/*"],
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Policy workspace", "root": str(tmp_path / "policy-ws")})

            with self.assertRaisesRegex(RuntimeError, "not allowed"):
                state.workspace_runtime.start(workspace)
            health = state.workspace_runtime.health()

        self.assertFalse(health["ok"])
        self.assertIn("not allowed", health["error"])

    def test_workspace_runtime_garbage_collects_idle_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    idle_timeout_seconds=1,
                    port_start=19110,
                ),
            )
            workspace = _create_ui_workspace(state, {"title": "Idle workspace", "root": str(tmp_path / "idle-ws")})
            state.workspace_runtime.start(workspace)
            record = state.workspace_runtime._read_record(workspace["id"])
            record["last_used_at"] = "2000-01-01T00:00:00Z"
            state.workspace_runtime._write_record(workspace["id"], record)

            result = state.workspace_runtime.garbage_collect([workspace])
            calls = _fake_workspace_container_calls(tmp_path)
            record = state.workspace_runtime._read_record(workspace["id"])

        self.assertEqual(len(result["stopped"]), 1, result)
        self.assertTrue(any(call and call[0] == "rm" and "-f" in call for call in calls), calls)
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["idle_stopped"])

    def test_runtime_health_rate_limits_container_garbage_collection(self) -> None:
        from optpilot_studio.ui.server import _rate_limited_runtime_gc

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[], run_roots=[])
            with patch.object(
                type(state.workspace_runtime),
                "garbage_collect",
                return_value={"stopped": [], "skipped": [], "idle_timeout_seconds": 1},
            ) as gc:
                first = _rate_limited_runtime_gc(state)
                second = _rate_limited_runtime_gc(state)
        self.assertEqual(gc.call_count, 1)
        self.assertEqual(first, second)

    def test_ui_workspace_runtime_marks_old_image_container_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            options = WorkspaceRuntimeOptions(
                executable=str(fake_container),
                image="old-runtime:latest",
                port_start=19120,
            )
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=options,
            )
            workspace = _create_ui_workspace(state, {"title": "Stale runtime", "root": str(tmp_path / "runtime-ws")})

            state.workspace_runtime.start(workspace)
            state.workspace_runtime.options.image = "new-runtime:latest"
            status = state.workspace_runtime.status(workspace)

        self.assertEqual(status["status"], "stale")
        self.assertFalse(status["image_matches"])
        self.assertEqual(status["current_image"], "old-runtime:latest")

    def test_ui_workspace_runtime_reserves_ports_across_workspace_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = _write_fake_workspace_container(tmp_path)
            state = UiState(
                cwd=tmp_path,
                catalog_roots=[],
                run_roots=[],
                workspace_runtime=WorkspaceRuntimeOptions(
                    executable=str(fake_container),
                    image="fake-code-server:latest",
                    port_start=19140,
                ),
            )
            first = _create_ui_workspace(state, {"title": "First", "root": str(tmp_path / "first")})
            second = _create_ui_workspace(state, {"title": "Second", "root": str(tmp_path / "second")})

            first_status = state.workspace_runtime.start(first)
            second_status = state.workspace_runtime.start(second)

        self.assertEqual(first_status["port"], 19140)
        self.assertEqual(second_status["port"], 19141)

    def test_ui_run_listing_summarizes_existing_evidence_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = LocalEvidenceStore(tmp_path, "ui-run")
            store.write_spec(study_spec.raw)
            store.record_observation(
                {
                    "trial_id": "trial-ok",
                    "candidate_id": "candidate-ok",
                    "status": "success",
                    "metric_values": {"throughput": 10.0},
                }
            )
            store.write_summary(
                {
                    "study_id": "study-ui",
                    "run_dir": str(store.run_dir),
                    "completed_trials": 1,
                    "best_metric": 10.0,
                    "best_trial_id": "trial-ok",
                    "best_candidate_id": "candidate-ok",
                    "failure_count": 0,
                }
            )
            state = UiState(cwd=repo_root, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[tmp_path])

            runs = _list_runs(state)

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["name"], "toy-random-search")
            self.assertEqual(runs[0]["completed_trials"], 1)
            self.assertEqual(runs[0]["best_metric"], 10.0)
            self.assertEqual(runs[0]["status"], "completed")

    def test_ui_run_listing_reads_controller_summary_v2_counts_and_status(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = LocalEvidenceStore(tmp_path, "ui-controller-run")
            store.write_spec(study_spec.raw)
            store.record_observation(
                {
                    "trial_id": "trial-failed",
                    "candidate_id": "candidate-failed",
                    "status": "timeout",
                    "metric_values": {},
                }
            )
            store.write_summary(
                {
                    "schema_version": "optpilot.run.summary.v2",
                    "study_id": "study-controller-ui",
                    "run_dir": str(store.run_dir),
                    "run_status": "failed",
                    "stop_code": "no_successful_observation",
                    "completed_trials": 1,
                    "accepted_trials": 1,
                    "terminal_trials": 1,
                    "attempt_count": 2,
                    "observation_count": 2,
                    "failure_count": 1,
                    "final_failure_count": 1,
                    "best_metric": None,
                    "best_trial_id": None,
                    "best_candidate_id": None,
                }
            )
            state = UiState(cwd=repo_root, catalog_roots=[], run_roots=[tmp_path])

            runs = _list_runs(state)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["summary_schema_version"], "optpilot.run.summary.v2")
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["run_status"], "failed")
        self.assertEqual(runs[0]["stop_code"], "no_successful_observation")
        self.assertEqual(runs[0]["accepted_trials"], 1)
        self.assertEqual(runs[0]["terminal_trials"], 1)
        self.assertEqual(runs[0]["attempt_count"], 2)
        self.assertEqual(runs[0]["observation_count"], 2)
        self.assertEqual(runs[0]["final_failure_count"], 1)

    def test_ui_run_status_uses_terminal_job_state_over_stale_live_summary(self) -> None:
        live_summary = {"run_status": "running"}

        self.assertEqual(_run_status(live_summary, {"status": "cancelled"}), "cancelled")
        self.assertEqual(_run_status(live_summary, {"status": "failed"}), "failed")
        self.assertEqual(_run_status(live_summary, {"status": "completed"}), "failed")
        self.assertEqual(_run_status(live_summary, {"status": "running"}), "running")
        self.assertEqual(_run_status({"run_status": "succeeded"}, {"status": "running"}), "completed")

    def test_ui_legacy_run_list_has_no_process_local_job_status_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = LocalEvidenceStore(root, "cancelled-live-summary")
            store.write_spec({"metadata": {"name": "cancelled-live-summary"}})
            store.write_summary(
                {
                    "schema_version": "optpilot.run.summary.v2",
                    "run_status": "running",
                    "completed_trials": 0,
                    "accepted_trials": 0,
                    "terminal_trials": 0,
                    "attempt_count": 0,
                    "observation_count": 0,
                }
            )
            state = UiState(cwd=root, catalog_roots=[], run_roots=[root])

            listed = _list_runs(state)[0]
            detailed = _run_detail(store.run_dir, state)["run"]

        self.assertFalse(hasattr(state, "jobs"))
        self.assertEqual(listed["status"], "running")
        self.assertEqual(detailed["status"], "running")

    def test_ui_assistant_best_observation_uses_final_retry_and_no_false_fallback(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = LocalEvidenceStore(Path(tmp_dir), "retry-best")
            store.write_spec(study_spec.raw)
            for attempt, status, metric in ((1, "failed", None), (2, "success", 9.0)):
                store.record_candidate(
                    {
                        "trial_id": "trial-retry",
                        "candidate_id": "candidate-retry",
                        "status": status,
                    }
                )
                store.record_trial(
                    {
                        "trial_id": "trial-retry",
                        "candidate_id": "candidate-retry",
                        "status": status,
                        "attempt_index": attempt,
                    }
                )
                store.record_observation(
                    {
                        "trial_id": "trial-retry",
                        "candidate_id": "candidate-retry",
                        "status": status,
                        "metric_values": {"throughput": metric} if metric is not None else {},
                        "provenance": {"attempt_index": attempt},
                    }
                )
            summary = {
                "schema_version": "optpilot.run.summary.v2",
                "run_status": "succeeded",
                "stop_code": "max_trials",
                "completed_trials": 1,
                "accepted_trials": 1,
                "terminal_trials": 1,
                "attempt_count": 2,
                "observation_count": 2,
                "failure_count": 0,
                "final_failure_count": 0,
                "best_metric": 9.0,
                "best_trial_id": "trial-retry",
                "best_candidate_id": "candidate-retry",
            }
            store.write_summary(summary)

            detail = _assistant_run_detail(store.run_dir)
            self.assertEqual(detail["best"]["observation"]["status"], "success")
            self.assertEqual(detail["best"]["observation"]["metric_values"], {"throughput": 9.0})

            store.write_summary(
                {
                    **summary,
                    "run_status": "failed",
                    "stop_code": "no_successful_observation",
                    "best_metric": None,
                    "best_trial_id": None,
                    "best_candidate_id": None,
                }
            )
            failed_detail = _assistant_run_detail(store.run_dir)

        self.assertEqual(failed_detail["best"]["observation"], {})
        self.assertEqual(failed_detail["best"]["candidate"], {})

    def test_ui_package_smoke_rejects_nonzero_final_failures_even_if_status_succeeded(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        errors = _smoke_summary_errors(
            completed,
            {
                "run_status": "succeeded",
                "stop_code": "max_trials",
                "completed_trials": 1,
                "failure_count": 1,
                "final_failure_count": 1,
            },
            Path("missing-smoke-study.yaml"),
        )

        self.assertIn("final_failure_count=1", " ".join(errors))



    def test_cli_parser_accepts_ui_command(self) -> None:
        args = build_parser().parse_args(
            [
                "ui",
                "--port",
                "9001",
                "--catalog",
                "test_catalog/example_package",
                "--workspace-runtime-bin",
                "podman",
                "--workspace-runtime-image",
                "custom/workspace:latest",
                "--workspace-runtime-network",
                "bridge",
                "--workspace-runtime-port-start",
                "19000",
            ]
        )

        self.assertEqual(args.command, "ui")
        self.assertEqual(args.port, 9001)
        self.assertEqual(args.catalog, ["test_catalog/example_package"])
        self.assertEqual(args.workspace_runtime_bin, "podman")
        self.assertEqual(args.workspace_runtime_image, "custom/workspace:latest")
        self.assertEqual(args.workspace_runtime_network, "bridge")
        self.assertEqual(args.workspace_runtime_port_start, 19000)

    def test_cli_ui_forwards_workspace_runtime_options(self) -> None:
        with patch("optpilot_studio.cli.run_ui") as run_ui_mock:
            exit_code = cli_main(
                [
                    "ui",
                    "--workspace-runtime-bin",
                    "podman",
                    "--workspace-runtime-image",
                    "custom/workspace:latest",
                    "--workspace-runtime-network",
                    "bridge",
                    "--workspace-runtime-port-start",
                    "19000",
                ]
            )

        self.assertEqual(exit_code, 0)
        run_ui_mock.assert_called_once()
        kwargs = run_ui_mock.call_args.kwargs
        self.assertEqual(kwargs["workspace_runtime_executable"], "podman")
        self.assertEqual(kwargs["workspace_runtime_image"], "custom/workspace:latest")
        self.assertEqual(kwargs["workspace_runtime_network"], "bridge")
        self.assertEqual(kwargs["workspace_runtime_port_start"], 19000)

    @staticmethod
    def _read_jsonl(path: Path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _require_retained_worker_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            socket_path = Path(tmp_dir) / "retained-worker.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(str(socket_path))
                except PermissionError as error:
                    if error.errno == 1:
                        self.skipTest("sandbox denies retained-worker AF_UNIX bind")
                    raise

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def _contains_key(cls, value, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(cls._contains_key(child, key) for child in value.values())
        if isinstance(value, list):
            return any(cls._contains_key(child, key) for child in value)
        return False

    @staticmethod
    def _metric_signature(entry):
        return tuple(sorted(entry["metric_values"].items()))

    @staticmethod
    def _process_count_with_marker(marker: str) -> int:
        result = subprocess.run(
            ["ps", "-Ao", "command"],
            capture_output=True,
            text=True,
            check=True,
        )
        return sum(1 for line in result.stdout.splitlines() if marker in line)


if __name__ == "__main__":
    unittest.main()
