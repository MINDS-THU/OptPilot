from __future__ import annotations

import json
import hashlib
import contextlib
import io
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import yaml

from optpilot.candidate_materialization import BoundsCandidateValidator, FileCandidateManifestValidator, WorkspaceBundleMaterializer
from optpilot.adapters import ReadOnlySQLiteQuery
from optpilot_studio.agent import OpenHandsAdapter, OpenHandsRuntimeConfig, load_assistant_system_prompt
from optpilot.cli import build_parser, main as cli_main
from optpilot.candidate_files import CandidateFileStore, store_candidate_file
from optpilot.config import compile_authoring_config
from optpilot.evidence import EvidenceView
from optpilot.environment import build_environment_snapshot
from optpilot.execution import _aggregate_metric_values, _worker_process_env
from optpilot.method_runtime import _host_method_env
from optpilot.package_index import expand_package_roots
from optpilot.package_validation import validate_package
from optpilot.provenance import PromptStore, build_generator_record, build_model_record
from optpilot.runner import run_expanded_study_spec, run_study
from optpilot.schema_validation import validate_public_config_schema
from optpilot.spec import StudySpec, load_expanded_study_spec, load_study_spec
from optpilot.storage import LocalEvidenceStore
from optpilot_studio.ui.server import (
    CodeServerOptions,
    UiState,
    WorkspaceRuntimeOptions,
    _agent_context_packet,
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
    _create_registration_manifest,
    _create_ui_workspace,
    _default_catalog_roots,
    _delete_ui_workspace,
    _detach_agent_workspace,
    _detach_workspace,
    _draft_study,
    _apply_registration_manifest,
    _list_agent_sessions,
    _list_ui_workspaces,
    _list_runs,
    _launch_catalog_interface,
    _interface_launch_by_id,
    _open_catalog_workspace,
    _open_study_workspace,
    _read_agent_approvals,
    _read_agent_events,
    _read_agent_messages,
    _reject_agent_action,
    _rename_ui_workspace,
    _require_ui_workspace,
    _sync_agent_session,
    _update_agent_settings,
    _execute_agent_tool,
    _local_code_server_executable,
    _start_catalog_interface_launch,
    _validate_study,
    _require_declared_env_from_host,
)


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
                "        index += 1",
                "    command = args[index + 1:]",
                "    if detach:",
                "        raise SystemExit(0)",
                "    if any('code-server' in item for item in command):",
                "        print('12345')",
                "        raise SystemExit(0)",
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


def _stable_baselines3_stack_importable() -> bool:
    if importlib.util.find_spec("stable_baselines3") is None:
        return False
    if importlib.util.find_spec("gymnasium") is None:
        return False
    try:
        __import__("stable_baselines3")
        __import__("gymnasium")
    except Exception:
        return False
    return True


class MvpIntegrationTest(unittest.TestCase):
    def test_openai_file_editor_rejects_empty_edit_payloads(self) -> None:
        from catalog.example_package.methods.openai_file_editor.method import _extract_edited_files

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

    def test_sample_study_runs_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            self.assertEqual(summary.completed_trials, 12)
            self.assertIsNotNone(summary.best_metric)
            self.assertGreater(summary.best_metric, 80.0)

            run_dir = Path(summary.run_dir)
            self.assertTrue((run_dir / "study_spec.json").exists())
            self.assertTrue((run_dir / "observations.jsonl").exists())
            self.assertTrue((run_dir / "summary.json").exists())
            self.assertTrue((run_dir / "method_calls.jsonl").exists())
            self.assertTrue((run_dir / "scheduler_events.jsonl").exists())
            self.assertTrue((run_dir / "trials.jsonl").exists())
            self.assertTrue((run_dir / "candidates.jsonl").exists())
            self.assertTrue((run_dir / "run_policy.json").exists())
            self.assertTrue((run_dir / "environment_snapshot.json").exists())

            summary_payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            environment_snapshot = json.loads((run_dir / "environment_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["completed_trials"], 12)
            self.assertEqual(summary_payload["policy"]["environment"]["candidateAccess"], "candidate_schema")
            self.assertIn("python", environment_snapshot)
            self.assertIn("platform", environment_snapshot)
            self.assertIn("packages", environment_snapshot)
            self.assertIn("dependency_files", environment_snapshot)
            self.assertEqual(environment_snapshot["study_spec"]["sha256"], self._sha256(run_dir / "study_spec.json"))
            self.assertTrue((run_dir / "source" / "environment").exists())
            self.assertTrue((run_dir / "source" / "method").exists())
            self.assertTrue(any(item["name"] == "pyproject.toml" for item in environment_snapshot["dependency_files"]))

            observations = self._read_jsonl(run_dir / "observations.jsonl")
            trials = self._read_jsonl(run_dir / "trials.jsonl")
            scheduler_events = self._read_jsonl(run_dir / "scheduler_events.jsonl")
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")
            candidates = self._read_jsonl(run_dir / "candidates.jsonl")
            run_policy = json.loads((run_dir / "run_policy.json").read_text(encoding="utf-8"))
            self.assertEqual(len(observations), 12)
            self.assertEqual(len(trials), 12)
            self.assertEqual(len(scheduler_events), 6)
            self.assertEqual(len(method_calls), 6)
            self.assertEqual(len(candidates), 12)
            self.assertEqual(run_policy["environment"]["candidateWriteScope"], "none")
            self.assertEqual(run_policy["execution"]["parallelism"]["candidateEvaluations"], 4)
            self.assertEqual(run_policy["execution"]["backend"]["implementation"], "builtin.local_subprocess_backend")
            self.assertEqual(run_policy["execution"]["scheduler"]["implementation"], "builtin.local_scheduler")
            self.assertEqual(scheduler_events[0]["event"], "batch_submitted")
            self.assertEqual(scheduler_events[1]["event"], "batch_collected")
            self.assertEqual(scheduler_events[1]["observation_count"], 4)
            self.assertEqual(method_calls[0]["event"], "proposed")
            self.assertEqual(method_calls[1]["event"], "observed")
            self.assertEqual(candidates[0]["validation"]["accepted"], True)
            self.assertEqual(candidates[0]["materialization"]["runtime_spec"], candidates[0]["spec"])
            self.assertIn("materialization_spec", candidates[0])
            self.assertIn("validation_spec", candidates[0])
            self.assertIn("backend_identity", trials[0])
            self.assertIn("scheduler_identity", trials[0])
            for observation in observations:
                self.assertIn("throughput", observation["metric_values"])
                self.assertTrue(
                    any(Path(output_file["path"]).name == "metrics.csv" for output_file in observation["output_files"])
                )
                self.assertGreaterEqual(observation["resource_usage"]["wallClockSeconds"], 0.0)
                self.assertEqual(observation["provenance"]["seed"], 7)
                self.assertEqual(
                    observation["provenance"]["backend_identity"]["implementation"],
                    "builtin.local_subprocess_backend",
                )
                self.assertEqual(
                    observation["provenance"]["scheduler_identity"]["implementation"],
                    "builtin.local_scheduler",
                )
                self.assertEqual(observation["provenance"]["resource_profile"]["timeoutSeconds"], 120)
                self.assertEqual(observation["provenance"]["sandbox_spec"]["cleanupPolicy"], "always")
                for output_file in observation["output_files"]:
                    self.assertTrue(Path(output_file["path"]).exists())

    def test_job_shop_example_baselines_run_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_paths = [
            repo_root / "catalog" / "example_package" / "studies" / "job_shop_rule_parameters_baseline.yaml",
            repo_root / "catalog" / "example_package" / "studies" / "job_shop_dispatch_rule_baseline.yaml",
            repo_root / "catalog" / "example_package" / "studies" / "job_shop_solver_code_baseline.yaml",
            repo_root / "catalog" / "example_package" / "studies" / "job_shop_openai_dispatch_rule.yaml",
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            for study_path in study_paths:
                with self.subTest(study=study_path.name):
                    summary = run_study(str(study_path), output_root=tmp_dir)
                    self.assertEqual(summary.completed_trials, 1)
                    self.assertEqual(summary.failure_count, 0)
                    self.assertIsNotNone(summary.best_metric)
                    observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
                    self.assertEqual(observations[0]["status"], "success")
                    self.assertIn("normalized_makespan", observations[0]["metric_values"])

    def test_job_shop_tune_dispatch_weights_improves_over_fixed_baseline(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        baseline_path = repo_root / "catalog" / "example_package" / "studies" / "job_shop_rule_parameters_baseline.yaml"
        tuner_path = repo_root / "catalog" / "example_package" / "studies" / "job_shop_tune_dispatch_weights.yaml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline = run_study(str(baseline_path), output_root=tmp_dir)
            tuned = run_study(str(tuner_path), output_root=tmp_dir)

            self.assertEqual(baseline.completed_trials, 1)
            self.assertEqual(baseline.failure_count, 0)
            self.assertEqual(tuned.completed_trials, 12)
            self.assertEqual(tuned.failure_count, 0)
            self.assertIsNotNone(baseline.best_metric)
            self.assertIsNotNone(tuned.best_metric)
            self.assertLess(tuned.best_metric, baseline.best_metric)

    def test_job_shop_rl_uses_environment_owned_training_context(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec = compile_authoring_config(
            repo_root / "catalog" / "example_package" / "studies" / "job_shop_rl_stable_baselines.yaml"
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

    @unittest.skipUnless(_stable_baselines3_stack_importable(), "stable-baselines3 example stack is not importable")
    def test_job_shop_stable_baselines_example_runs_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_path = repo_root / "catalog" / "example_package" / "studies" / "job_shop_rl_stable_baselines.yaml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(study_path), output_root=tmp_dir)
            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(summary.failure_count, 0)
            self.assertIsNotNone(summary.best_metric)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            self.assertEqual(observations[0]["status"], "success")
            self.assertIn("normalized_makespan", observations[0]["metric_values"])

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
        repo_root = Path(__file__).resolve().parents[1]
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
        repo_root = Path(__file__).resolve().parents[1]
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
        repo_root = Path(__file__).resolve().parents[1]
        spec = compile_authoring_config(repo_root / "catalog" / "example_package" / "studies" / "job_shop_ortools_cpsat.yaml")

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

    def test_candidate_parallelism_reduces_elapsed_time(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        base_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        base_spec["metadata"]["name"] = "toy-parallel-check"
        base_spec["environment"]["adapter"]["config"]["evaluate"]["config"]["sleep_seconds"] = 0.2
        base_spec["stopping"]["maxTrials"] = 4
        base_spec["method"]["config"]["batchSize"] = 4
        base_spec["execution"]["parallelism"]["candidateParallelism"] = 4

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "parallel.yaml"
            spec_path.write_text(yaml.safe_dump(base_spec, sort_keys=False), encoding="utf-8")

            started = time.monotonic()
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            elapsed = time.monotonic() - started
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertLess(elapsed, 0.75)
            self.assertEqual(len(observations), 4)
            for observation in observations:
                self.assertGreaterEqual(observation["resource_usage"]["wallClockSeconds"], 0.18)

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
        repo_root = Path(__file__).resolve().parents[1]
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
                        "metrics": {"source": "return", "keys": ["score"]},
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
                        "objective": {"metric": "score", "direction": "maximize"},
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
                        "metrics": {"source": "stdout", "keys": ["score"]},
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
                        "objective": {"metric": "score", "direction": "maximize"},
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

    def test_candidate_file_store_creates_manifest_without_inline_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "generated"
            generated.mkdir()
            (generated / "solver.py").write_text("from utils.helper import score\n", encoding="utf-8")
            (generated / "utils").mkdir()
            (generated / "utils" / "helper.py").write_text("def score(x):\n    return x + 1\n", encoding="utf-8")
            (generated / "__pycache__").mkdir()
            (generated / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
            candidate_store_root = tmp_path / "candidate-store"
            store = CandidateFileStore(candidate_store_root, content_ref_mode="absolute")

            candidate = store.store_directory(
                generated,
                candidate_id="candidate-generated-001",
                entrypoint="solver:solve",
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
            self.assertEqual(candidate["spec"]["entrypoint"], "solver:solve")
            self.assertEqual(len(candidate["spec"]["files"]), 2)
            self.assertFalse(self._contains_key(candidate, "content"))
            self.assertTrue((candidate_store_root / "candidate-generated-001" / "files" / "utils" / "helper.py").exists())

    def test_candidate_file_store_supports_single_file_relative_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "solver.py"
            generated.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            candidate = store_candidate_file(
                generated,
                tmp_path / "candidates",
                candidate_id="candidate-single-file",
                path="solver.py",
                content_ref_mode="relative",
                content_ref_base=tmp_path,
            )

            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = FileCandidateManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )
            report = validator.validate(candidate, {})

            self.assertTrue(report.accepted, report.errors)
            self.assertEqual(candidate["format"], "files")
            self.assertEqual(candidate["spec"]["files"][0]["path"], "solver.py")
            self.assertEqual(
                candidate["spec"]["files"][0]["contentRef"],
                "candidates/candidate-single-file/files/solver.py",
            )

    def test_candidate_file_store_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "solver.py"
            source.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            store = CandidateFileStore(tmp_path / "candidates")

            with self.assertRaisesRegex(ValueError, "Unsafe candidate file path"):
                store.store_files(
                    [{"source": source, "path": "../solver.py"}],
                    candidate_id="candidate-unsafe",
                )

            self.assertFalse((tmp_path / "candidates" / "candidate-unsafe").exists())

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

    def test_cli_run_loads_user_owned_components_from_current_working_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml"
        original_cwd = Path.cwd()
        original_sys_path = list(sys.path)

        with tempfile.TemporaryDirectory() as tmp_dir:
            filtered_sys_path = []
            for entry in sys.path:
                if not entry:
                    continue
                try:
                    if Path(entry).resolve() == repo_root:
                        continue
                except OSError:
                    pass
                filtered_sys_path.append(entry)

            try:
                os.chdir(repo_root)
                sys.path[:] = filtered_sys_path
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = cli_main(["run", str(spec_path), "--output-root", tmp_dir])
            finally:
                os.chdir(original_cwd)
                sys.path[:] = original_sys_path

            self.assertEqual(exit_code, 0)

    def test_run_study_defaults_to_current_workspace_runs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        environment_path = repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"
        method_path = repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"
        original_cwd = Path.cwd()
        original_sys_path = list(sys.path)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            package_studies = tmp_path / "external_package" / "studies"
            workspace_root = tmp_path / "workspace"
            package_studies.mkdir(parents=True)
            workspace_root.mkdir()
            spec_path = package_studies / "toy_default_runs.yaml"
            spec_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "toy-default-runs",
                        "environmentConfig": str(environment_path),
                        "methodConfig": str(method_path),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                        "execution": {"parallelism": 1, "timeoutSeconds": 120},
                        "reproducibility": {"seed": 7},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            try:
                os.chdir(workspace_root)
                if str(repo_root) not in sys.path:
                    sys.path.insert(0, str(repo_root))
                summary = run_study(str(spec_path))
            finally:
                os.chdir(original_cwd)
                sys.path[:] = original_sys_path

            run_dir = Path(summary.run_dir)
            self.assertEqual(run_dir.parent, (workspace_root / "runs").resolve())
            self.assertTrue((run_dir / "observations.jsonl").exists())
            self.assertFalse((tmp_path / "external_package" / "runs").exists())

    def test_run_uses_copied_component_sources_and_runs_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_pkg = tmp_path / "demo_env_pkg"
            method_pkg = tmp_path / "demo_method_pkg"
            env_dir = env_pkg / "env"
            method_dir = method_pkg / "method"
            env_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            for path in [env_pkg / "__init__.py", env_dir / "__init__.py", method_pkg / "__init__.py", method_dir / "__init__.py"]:
                path.write_text("", encoding="utf-8")
            (env_dir / "helper.py").write_text("VALUE = 17.0\n", encoding="utf-8")
            (env_dir / "evaluator.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from .helper import VALUE",
                        "",
                        "def evaluate(candidate_runtime, context):",
                        "    root = Path(__file__).resolve().parent",
                        "    marker = root.parent / 'setup-marker.txt'",
                        "    copied = '/source/environment/' in str(root)",
                        "    ready = marker.exists() and marker.read_text(encoding='utf-8') == 'ready'",
                        "    score = VALUE if copied and ready else -1.0",
                        "    return {",
                        "        'metric_values': {'score': score},",
                        "        'event_summary': {'module_file': str(__file__), 'marker': str(marker)},",
                        "    }",
                    ]
                ),
                encoding="utf-8",
            )
            (method_dir / "method.py").write_text(
                "\n".join(
                    [
                        "class FixedMethod:",
                        "    def __init__(self, definition, study_spec, rng=None):",
                        "        self.definition = definition",
                        "        self._done = False",
                        "",
                        "    def propose(self, n_candidates, study_state):",
                        "        if self._done:",
                        "            return []",
                        "        self._done = True",
                        "        return [{'candidate_id': 'copy-test-candidate', 'format': 'parameters', 'spec': {'x': 1.0}}]",
                        "",
                        "    def observe(self, observations):",
                        "        return None",
                    ]
                ),
                encoding="utf-8",
            )
            env_config = env_dir / "environment.yaml"
            env_config.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "copy-source-env",
                        "evaluator": {"python": "demo_env_pkg.env.evaluator:evaluate"},
                        "runtime": {
                            "sandbox": "process",
                            "setup": {
                                "steps": [
                                    {
                                        "uses": "command",
                                        "command": [
                                            sys.executable,
                                            "-c",
                                            "from pathlib import Path; import sys; p = Path('setup-marker.txt'); sys.exit(7) if p.exists() else p.write_text('ready', encoding='utf-8')",
                                        ],
                                    }
                                ]
                            },
                        },
                        "candidate": {
                            "format": "parameters",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 2.0}}},
                        },
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            method_config = method_dir / "method.yaml"
            method_config.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "copy-source-method",
                        "entrypoint": {"python": "demo_method_pkg.method.method:FixedMethod", "protocol": "batch"},
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
                        "name": "copy-source-study",
                        "environmentConfig": str(env_config),
                        "methodConfig": str(method_config),
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=str(tmp_path / "runs"))
            run_dir = Path(summary.run_dir)
            status = json.loads(
                (run_dir / "source" / "environment" / "demo_env_pkg" / ".optpilot" / "setup-status.json").read_text(
                    encoding="utf-8"
                )
            )
            observation = self._read_jsonl(run_dir / "observations.jsonl")[0]
            compiled = json.loads((run_dir / "study_spec.json").read_text(encoding="utf-8"))
            first_setup_reused = compiled["extensions"]["runSource"]["environment"]["setupReused"]
            method_copied = (run_dir / "source" / "method" / "demo_method_pkg" / "method" / "method.py").exists()
            resumed = run_study(str(study_path), output_root=str(tmp_path / "runs"), resume_run_dir=str(run_dir))
            resumed_compiled = json.loads((run_dir / "study_spec.json").read_text(encoding="utf-8"))

        self.assertEqual(summary.best_metric, 17.0)
        self.assertEqual(resumed.completed_trials, 1)
        self.assertEqual(status["status"], "ready")
        self.assertIn("/source/environment/", observation["event_summary"]["module_file"])
        self.assertTrue(method_copied)
        self.assertIn("/source/environment", compiled["environment"]["adapter"]["config"]["evaluate"]["pythonPath"][0])
        self.assertIn("/source/method", compiled["method"]["implementation"]["pythonPath"][0])
        self.assertFalse(first_setup_reused)
        self.assertTrue(resumed_compiled["extensions"]["runSource"]["environment"]["setupReused"])

    def test_cli_environment_adapter_runs_and_captures_process_evidence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_cli_random_search.yaml"
        raw_spec = compile_authoring_config(spec_path)
        raw_spec["stopping"]["maxTrials"] = 4

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            temp_spec = tmp_path / "toy_cli_random_search.yaml"
            temp_spec.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(temp_spec), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            candidates = self._read_jsonl(run_dir / "candidates.jsonl")

            self.assertEqual(summary.completed_trials, 4)
            self.assertEqual(len(observations), 4)
            self.assertEqual(len(candidates), 4)
            first_observation = observations[0]
            self.assertEqual(first_observation["provenance"]["backend_identity"]["implementation"], "builtin.local_subprocess_backend")
            output_file_names = {candidate["name"]: candidate for candidate in first_observation["output_files"] if "name" in candidate}
            self.assertIn("candidate_payload", output_file_names)
            self.assertIn("settings", output_file_names)
            self.assertIn("metrics", output_file_names)
            self.assertIn("stdout", output_file_names)
            self.assertIn("stderr", output_file_names)
            stdout_path = Path(output_file_names["stdout"]["path"])
            self.assertIn("wrote", stdout_path.read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["materialization"]["runtime_spec"], candidates[0]["spec"])

    def test_user_owned_method_loads_through_python_hook(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            candidates = self._read_jsonl(Path(summary.run_dir) / "candidates.jsonl")

            self.assertEqual(summary.completed_trials, 3)
            self.assertEqual(summary.best_metric, max(item["metric_values"]["throughput"] for item in observations))
            self.assertEqual(candidates[0]["generator"]["owned_by"], "user")
            self.assertEqual(
                observations[0]["provenance"]["generator"]["strategy"],
                "fixed_parameter_user_method",
            )

    def test_command_method_reads_request_from_stdin_and_records_events(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            method_script = tmp_path / "command_method.py"
            method_script.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "request = json.loads(sys.stdin.read())",
                        "candidates = []",
                        "for index in range(int(request['n_candidates'])):",
                        "    candidates.append({",
                        "        'candidate_id': f\"cmd-stdin-{index}\",",
                        "        'format': 'parameters',",
                        "        'spec': {'x': 4.2, 'y': 7, 'mode': 'balanced'},",
                        "        'lineage': {'parents': []},",
                        "        'generator': {'method_id': 'command-stdin-method', 'strategy': 'stdin_command'},",
                        "    })",
                        "json.dump({",
                        "    'candidates': candidates,",
                        "    'method_events': [{'event': 'script_completed', 'n_candidates': len(candidates)}],",
                        "}, sys.stdout)",
                    ]
                ),
                encoding="utf-8",
            )
            study_path = tmp_path / "command_stdin_study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "command-stdin-study",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": "command_stdin_method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 2},
                        "execution": {"parallelism": 2, "timeoutSeconds": 120},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (tmp_path / "command_stdin_method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "command-stdin-method",
                        "entrypoint": {
                            "command": [sys.executable, str(method_script)],
                            "protocol": "batch",
                        },
                        "settings": {"batchSize": 2},
                        "accepts": {
                            "formats": ["parameters"],
                            "requires": {"context": ["candidate.parameters.schema"]},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")
            method_events = self._read_jsonl(run_dir / "method_events.jsonl")
            observations = self._read_jsonl(run_dir / "observations.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertTrue(all(observation["status"] == "success" for observation in observations))
            self.assertEqual([call["event"] for call in method_calls], ["completed", "observed"])
            self.assertEqual(method_events[0]["event"], "script_completed")
            self.assertTrue(Path(method_calls[0]["payload"]["input_path"]).exists())
            self.assertTrue(Path(method_calls[0]["payload"]["output_path"]).exists())

    def test_command_method_can_use_request_and_response_file_placeholders(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            method_script = tmp_path / "file_command_method.py"
            method_script.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "request_path = pathlib.Path(sys.argv[1])",
                        "response_path = pathlib.Path(sys.argv[2])",
                        "request = json.loads(request_path.read_text(encoding='utf-8'))",
                        "response_path.write_text(json.dumps({",
                        "    'candidates': [{",
                        "        'candidate_id': 'cmd-file-0',",
                        "        'format': 'parameters',",
                        "        'spec': {'x': 4.2, 'y': 7, 'mode': 'balanced'},",
                        "        'lineage': {'parents': []},",
                        "        'generator': {'method_id': 'command-file-method', 'strategy': request['request_id']},",
                        "    }],",
                        "}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            study_path = tmp_path / "command_file_study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "command-file-study",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": "command_file_method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                        "execution": {"parallelism": 1, "timeoutSeconds": 120},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (tmp_path / "command_file_method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "command-file-method",
                        "entrypoint": {
                            "command": [sys.executable, str(method_script), "{input_file}", "{output_file}"],
                            "protocol": "batch",
                        },
                        "settings": {"batchSize": 1},
                        "accepts": {
                            "formats": ["parameters"],
                            "requires": {"context": ["candidate.parameters.schema"]},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=tmp_dir)
            method_calls = self._read_jsonl(Path(summary.run_dir) / "method_calls.jsonl")
            candidates = self._read_jsonl(Path(summary.run_dir) / "candidates.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(method_calls[0]["event"], "completed")
            self.assertEqual(candidates[0]["generator"]["method_id"], "command-file-method")
            self.assertTrue(Path(method_calls[0]["payload"]["output_path"]).exists())

    def test_command_method_can_run_inside_container_runtime(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            method_script = tmp_path / "container_command_method.py"
            method_script.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "request_path = pathlib.Path(sys.argv[1])",
                        "response_path = pathlib.Path(sys.argv[2])",
                        "request = json.loads(request_path.read_text(encoding='utf-8'))",
                        "response_path.write_text(json.dumps({",
                        "    'candidates': [{",
                        "        'candidate_id': 'cmd-container-0',",
                        "        'format': 'parameters',",
                        "        'spec': {'x': 4.2, 'y': 7, 'mode': 'balanced'},",
                        "        'lineage': {'parents': []},",
                        "        'generator': {'method_id': 'command-container-method', 'strategy': request['request_id']},",
                        "    }],",
                        "    'method_events': [{'event': 'container_method_finished'}],",
                        "}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            fake_container = tmp_path / "fake_method_container.py"
            fake_log = tmp_path / "fake_container_invocations.jsonl"
            fake_container.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, os, pathlib, subprocess, sys",
                        "log_path = pathlib.Path(os.environ['OPTPILOT_FAKE_METHOD_CONTAINER_LOG'])",
                        "args = sys.argv[1:]",
                        "with log_path.open('a', encoding='utf-8') as handle:",
                        "    handle.write(json.dumps(args) + '\\n')",
                        "if not args or args[0] != 'run':",
                        "    raise SystemExit(0 if args and args[0] == 'build' else 2)",
                        "env = os.environ.copy()",
                        "cwd = None",
                        "index = 1",
                        "value_options = {'--name', '--network', '-v', '--volume'}",
                        "while index < len(args):",
                        "    arg = args[index]",
                        "    if arg in {'--rm', '-i'}:",
                        "        index += 1",
                        "        continue",
                        "    if arg in {'-w', '--workdir'}:",
                        "        cwd = args[index + 1]",
                        "        index += 2",
                        "        continue",
                        "    if arg in {'-e', '--env'}:",
                        "        key, value = args[index + 1].split('=', 1)",
                        "        env[key] = value",
                        "        index += 2",
                        "        continue",
                        "    if arg in value_options:",
                        "        index += 2",
                        "        continue",
                        "    if arg.startswith('-'):",
                        "        index += 1",
                        "        continue",
                        "    command = args[index + 1:]",
                        "    break",
                        "else:",
                        "    raise SystemExit(3)",
                        "completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)",
                        "sys.stdout.write(completed.stdout)",
                        "sys.stderr.write(completed.stderr)",
                        "raise SystemExit(completed.returncode)",
                    ]
                ),
                encoding="utf-8",
            )
            fake_container.chmod(0o755)
            (tmp_path / "Dockerfile.method").write_text("FROM scratch\n", encoding="utf-8")
            study_path = tmp_path / "container_method_study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "container-method-study",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": "container_method.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                        "execution": {"parallelism": 1, "timeoutSeconds": 120},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (tmp_path / "container_method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "command-container-method",
                        "entrypoint": {
                            "command": [sys.executable, str(method_script), "{input_file}", "{output_file}"],
                            "protocol": "batch",
                        },
                        "runtime": {
                            "sandbox": "container",
                            "container": {
                                "image": "optpilot-method-test-image",
                                "executable": str(fake_container),
                                "network": "disabled",
                                "build": {
                                    "context": str(tmp_path),
                                    "dockerfile": "Dockerfile.method",
                                    "tag": "optpilot-method-test-image",
                                    "args": {"METHOD": "test"},
                                },
                            },
                            "env": {"OPTPILOT_METHOD_STATIC_ENV": "static-value"},
                            "envFromHost": ["OPTPILOT_METHOD_TEST_TOKEN"],
                        },
                        "settings": {"batchSize": 1},
                        "accepts": {
                            "formats": ["parameters"],
                            "requires": {"context": ["candidate.parameters.schema"]},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            old_log_env = os.environ.get("OPTPILOT_FAKE_METHOD_CONTAINER_LOG")
            old_token_env = os.environ.get("OPTPILOT_METHOD_TEST_TOKEN")
            os.environ["OPTPILOT_FAKE_METHOD_CONTAINER_LOG"] = str(fake_log)
            os.environ["OPTPILOT_METHOD_TEST_TOKEN"] = "secret-token"
            try:
                summary = run_study(str(study_path), output_root=tmp_dir)
            finally:
                if old_log_env is None:
                    os.environ.pop("OPTPILOT_FAKE_METHOD_CONTAINER_LOG", None)
                else:
                    os.environ["OPTPILOT_FAKE_METHOD_CONTAINER_LOG"] = old_log_env
                if old_token_env is None:
                    os.environ.pop("OPTPILOT_METHOD_TEST_TOKEN", None)
                else:
                    os.environ["OPTPILOT_METHOD_TEST_TOKEN"] = old_token_env

            run_dir = Path(summary.run_dir)
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")
            method_events = self._read_jsonl(run_dir / "method_events.jsonl")
            candidates = self._read_jsonl(run_dir / "candidates.jsonl")
            fake_invocations = [json.loads(line) for line in fake_log.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual([call["event"] for call in method_calls], ["runtime_built", "completed", "observed"])
            self.assertEqual(method_calls[0]["payload"]["runtime"], "container")
            self.assertEqual(method_calls[1]["payload"]["runtime"]["container_image"], "optpilot-method-test-image")
            self.assertEqual(method_calls[1]["payload"]["runtime"]["build"]["status"], "built")
            self.assertEqual(method_events[0]["event"], "container_method_finished")
            self.assertEqual(candidates[0]["candidate_id"], "cmd-container-0")
            self.assertEqual(fake_invocations[0][0], "build")
            self.assertIn("--build-arg", fake_invocations[0])
            self.assertIn("optpilot-method-test-image", fake_invocations[-1])
            self.assertIn("--network", fake_invocations[-1])
            self.assertIn("OPTPILOT_METHOD_TEST_TOKEN=secret-token", fake_invocations[-1])

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
            method_env = _host_method_env({"envFromHost": ["OPTPILOT_DECLARED_TOKEN"], "env": {"STATIC_VALUE": "1"}})
            worker_env = _worker_process_env({"envFromHost": ["OPTPILOT_DECLARED_TOKEN"], "env": {"STATIC_VALUE": "1"}})

        self.assertEqual(method_env["OPTPILOT_DECLARED_TOKEN"], "visible")
        self.assertEqual(worker_env["OPTPILOT_DECLARED_TOKEN"], "visible")
        self.assertEqual(method_env["STATIC_VALUE"], "1")
        self.assertEqual(worker_env["STATIC_VALUE"], "1")
        self.assertNotIn("OPTPILOT_UNDECLARED_TOKEN", method_env)
        self.assertNotIn("OPTPILOT_UNDECLARED_TOKEN", worker_env)

    def test_method_config_rejects_unimplemented_shapes(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
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

    def test_python_session_method_protocol_runs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "session_method_config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "session-method",
                        "entrypoint": {
                            "python": "tests.fixtures.bad_targets:SessionMethod",
                            "protocol": "session",
                        },
                        "settings": {"batchSize": 2},
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = tmp_path / "session_method.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "session-method",
                        "environmentConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                        "methodConfig": "session_method_config.yaml",
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 2},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")
            method_events = self._read_jsonl(run_dir / "method_events.jsonl")

        self.assertEqual(summary.completed_trials, 2)
        self.assertEqual(method_calls[0]["payload"]["protocol"], "optpilot.method.session.v1")
        self.assertEqual(method_calls[0]["payload"]["interface"], "session")
        self.assertEqual(method_events[0]["event"], "session_started")

    def test_custom_environment_adapter_runs_through_component_registry(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "custom_adapter_env.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "custom-adapter-env",
                        "evaluator": {"adapter": "tests.fixtures.bad_targets:CustomAdapter"},
                        "candidate": {
                            "format": "parameters",
                            "description": "Toy parameters.",
                            "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 8.0}}},
                        },
                        "metrics": {"source": "return", "keys": ["throughput"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = Path(tmp_dir) / "custom_environment_adapter.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "custom-environment-adapter",
                        "environmentConfig": "custom_adapter_env.yaml",
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=tmp_dir)

        self.assertEqual(summary.best_metric, 12.5)

    def test_custom_metric_and_record_extractors_run(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "custom_extractors_env.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "custom-extractor-env",
                        "evaluator": {
                            "python": "tests.fixtures.catalog.toy_factory_env:evaluate",
                            "settings": {"target_x": 4.2, "target_y": 7},
                        },
                        "candidate": {
                            "format": "parameters",
                            "description": "Toy parameters.",
                            "parameters": {
                                "schema": {
                                    "x": {"valueType": "float", "min": 0.0, "max": 8.0},
                                    "y": {"valueType": "int", "min": 1, "max": 10},
                                },
                            },
                        },
                        "metrics": {
                            "source": "custom",
                            "extractor": "tests.fixtures.bad_targets:custom_metrics",
                            "keys": ["throughput"],
                        },
                        "records": [
                            {
                                "name": "custom_events",
                                "source": "custom",
                                "extractor": "tests.fixtures.bad_targets:CustomRecordExtractor",
                                "settings": {"value": "recorded"},
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = Path(tmp_dir) / "custom_extractors.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "custom-extractors",
                        "environmentConfig": "custom_extractors_env.yaml",
                        "methodConfig": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "fixed_parameter_method.yaml"),
                        "objective": {"metric": "throughput", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            summary = run_study(str(study_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            evidence = EvidenceView(LocalEvidenceStore.open_run_dir(Path(summary.run_dir)), load_study_spec(str(study_path)))
            records = evidence.records("custom_events")
            artifacts = evidence.artifacts(name="records_to_extract_report")
            decision_context = evidence.decision_context()

        self.assertEqual(summary.best_metric, 33.0)
        self.assertEqual(observations[0]["metric_values"]["throughput"], 33.0)
        self.assertEqual([row["record"]["value"] for row in records], ["recorded", "recorded"])
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["trial_id"], observations[0]["trial_id"])
        self.assertEqual(decision_context["record_streams"][0]["name"], "custom_events")
        self.assertTrue(any(item["name"] == "records_to_extract_report" for item in decision_context["recent_output_files"]))

    def test_environment_config_rejects_malformed_custom_hook_refs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
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
        repo_root = Path(__file__).resolve().parents[1]
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

    def test_container_build_dockerfile_resolves_relative_to_build_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            docker_dir = tmp_path / "docker"
            docker_dir.mkdir()
            dockerfile = docker_dir / "Dockerfile"
            dockerfile.write_text("FROM python:3.11-slim\n", encoding="utf-8")
            runtime = {
                "sandbox": "container",
                "container": {
                    "image": "optpilot-context-relative-test:latest",
                    "build": {
                        "context": "docker",
                        "dockerfile": "Dockerfile",
                        "tag": "optpilot-context-relative-test:latest",
                    },
                    "network": "disabled",
                },
            }
            environment_path = tmp_path / "environment.yaml"
            environment_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "container-context-env",
                        "evaluator": {"python": "tests.fixtures.catalog.toy_factory_env:evaluate"},
                        "runtime": runtime,
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
                        "id": "container-context-method",
                        "entrypoint": {"python": "tests.fixtures.catalog.user_methods.fixed_parameter_method:FixedParameterMethod"},
                        "runtime": runtime,
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
                        "name": "container-context-study",
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

        self.assertEqual(compiled["execution"]["backend"]["config"]["build"]["context"], str(docker_dir.resolve()))
        self.assertEqual(compiled["execution"]["backend"]["config"]["build"]["dockerfile"], str(dockerfile.resolve()))
        self.assertEqual(compiled["method"]["runtime"]["build"]["context"], str(docker_dir.resolve()))
        self.assertEqual(compiled["method"]["runtime"]["build"]["dockerfile"], str(dockerfile.resolve()))

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
        self.assertTrue(validate_public_config_schema(container_build).valid)

        missing_tag = deepcopy(container_build)
        del missing_tag["runtime"]["container"]["build"]["tag"]
        self.assertFalse(validate_public_config_schema(missing_tag).valid)

        non_string_build_arg = deepcopy(container_build)
        non_string_build_arg["runtime"]["container"]["build"]["args"]["PYTHON_VERSION"] = 3.12
        self.assertFalse(validate_public_config_schema(non_string_build_arg).valid)

        command_session_method = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "command-session-method",
            "entrypoint": {"command": ["python", "method.py"], "protocol": "session"},
            "accepts": {"formats": ["parameters"]},
        }
        self.assertFalse(validate_public_config_schema(command_session_method).valid)

    def test_compile_maps_public_retry_to_scheduler_attempts(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
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

    def test_run_can_resume_existing_evidence_store(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml")
        raw_spec["metadata"]["name"] = "toy-resume-run"
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["stopping"]["maxTrials"] = 1

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "resume.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            first = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)

            raw_spec["stopping"]["maxTrials"] = 2
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            resumed = run_expanded_study_spec(str(spec_path), output_root=tmp_dir, resume_run_dir=first.run_dir)
            run_dir = Path(resumed.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            lineage = json.loads((run_dir / "run_lineage.json").read_text(encoding="utf-8"))

            self.assertEqual(resumed.run_dir, first.run_dir)
            self.assertEqual(resumed.completed_trials, 2)
            self.assertEqual(len(observations), 2)
            self.assertEqual(lineage["mode"], "resume")
            self.assertEqual(len(lineage["resume_events"]), 1)

    def test_run_can_branch_from_existing_evidence_store(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml")
        raw_spec["metadata"]["name"] = "toy-branch-run"
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["stopping"]["maxTrials"] = 1

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "branch.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            parent = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            branch = run_expanded_study_spec(str(spec_path), output_root=tmp_dir, branch_from_run_dir=parent.run_dir)
            lineage = json.loads((Path(branch.run_dir) / "run_lineage.json").read_text(encoding="utf-8"))

            self.assertNotEqual(branch.run_dir, parent.run_dir)
            self.assertEqual(branch.completed_trials, 1)
            self.assertEqual(lineage["mode"], "branch")
            self.assertEqual(lineage["parent"]["run_dir"], parent.run_dir)

    def test_user_owned_file_candidate_method_uses_run_candidate_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_dir = tmp_path / "candidate_source"
            source_dir.mkdir()
            (source_dir / "solver.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
            eval_path = tmp_path / "eval_code.py"
            eval_path.write_text(
                "\n".join(
                    [
                        "import argparse, json, pathlib",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--candidate')",
                        "parser.add_argument('--metrics')",
                        "args = parser.parse_args()",
                        "text = pathlib.Path(args.candidate).read_text(encoding='utf-8')",
                        "score = 42.0 if 'return 42' in text else 0.0",
                        "pathlib.Path(args.metrics).write_text(json.dumps({'metric_values': {'score': score}}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            spec = {
                "apiVersion": "optpilot/v1",
                "config": "run_spec",
                "metadata": {"name": "code-candidate-method"},
                "environment": {
                    "environmentId": "file-candidate-evaluator",
                    "accessPolicy": "CodeAwareReadOnly",
                    "mutationPolicy": "TrialWorkspaceOnly",
                    "adapter": {
                        "implementation": "builtin.configured_environment",
                        "config": {
                            "evaluate": {
                                "type": "command",
                                "command": [
                                    "{python}",
                                    str(eval_path),
                                    "--candidate",
                                    "{candidate}",
                                    "--metrics",
                                    "{metrics_file}",
                                ],
                            },
                            "candidate": {"format": "files", "required": ["solver.py"]},
                            "metrics": {"source": "file", "path": "metrics.json"},
                        },
                    },
                    "runtimeContract": {"timeoutSeconds": 30},
                },
                "objective": {"primaryMetric": {"name": "score", "direction": "maximize"}},
                "candidate": {
                    "format": "files",
                    "context": {
                        "description": "Generated code source.",
                        "candidate": {"format": "files"},
                        "files": {
                            "root": ".",
                            "editable": [{"path": "solver.py", "role": "solver"}],
                            "required": ["solver.py"],
                            "allow": ["solver.py"],
                            "deny": [],
                        },
                        "workspace": {
                            "copy": [
                                {"from": str(source_dir), "to": "."}
                            ]
                        },
                    },
                    "validation": {
                        "implementation": "builtin.workspace_policy",
                        "config": {"allowAbsoluteContentRefs": True},
                    },
                    "materialization": {
                        "implementation": "builtin.workspace_bundle",
                        "config": {
                            "candidateRoot": ".",
                            "allowAbsoluteContentRefs": True,
                        },
                    },
                },
                "method": {
                    "id": "code_method",
                    "implementation": {
                        "type": "python",
                        "callable": "tests.fixtures.catalog.user_methods.file_candidate_method:FileCandidateMethod",
                        "protocol": "optpilot.method.batch.v1",
                    },
                    "config": {
                        "entrypoint": "solver:solve",
                        "provider": "example",
                        "model": "example-code-model",
                        "promptMessages": [
                            {"role": "system", "content": "Store this generated solver."},
                        ],
                    },
                },
                "execution": {
                    "backend": {"implementation": "builtin.local_backend", "config": {}},
                    "scheduler": {"implementation": "builtin.local_scheduler", "config": {}},
                    "parallelism": {"candidateParallelism": 1},
                },
                "evidence": {"store": {"metadataBackend": "local_json", "outputFileBackend": "local_fs"}},
                "reproducibility": {"seedPolicy": {"globalSeed": 0}},
                "stopping": {"maxTrials": 1},
            }
            spec_path = tmp_path / "code_method.yaml"
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            candidates = self._read_jsonl(Path(summary.run_dir) / "candidates.jsonl")
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertEqual(summary.best_metric, 42.0)
            self.assertEqual(observations[0]["metric_values"]["score"], 42.0)
            content_ref = candidates[0]["spec"]["files"][0]["contentRef"]
            self.assertIn(str(Path(summary.run_dir) / "candidates"), content_ref)
            self.assertTrue(Path(content_ref).exists())
            prompt_record = candidates[0]["generator"]["prompt_record"]
            self.assertTrue(Path(prompt_record["contentRef"]).exists())
            self.assertEqual(candidates[0]["generator"]["model_record"]["model"], "example-code-model")

    def test_user_owned_lifecycle_method_loads_through_python_hook(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_lifecycle_method.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            candidates = self._read_jsonl(run_dir / "candidates.jsonl")
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                [snapshot["event"] for snapshot in method_calls],
                ["started", "polled", "finalized", "observed"],
            )
            self.assertEqual(method_calls[0]["payload"]["interface"], "lifecycle")
            self.assertEqual(candidates[0]["generator"]["owned_by"], "user")
            self.assertEqual(
                observations[0]["provenance"]["generator"]["strategy"],
                "lifecycle_fixed_parameter_user_method",
            )

    def test_container_backend_runs_trial_through_container_cli(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-container-backend"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_container = tmp_path / "fake_container.py"
            fake_log = tmp_path / "fake_container_log.jsonl"
            fake_container.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, os, pathlib, subprocess, sys",
                        "log_path = pathlib.Path(os.environ['OPTPILOT_FAKE_CONTAINER_LOG'])",
                        "args = sys.argv[1:]",
                        "with log_path.open('a', encoding='utf-8') as handle:",
                        "    handle.write(json.dumps(args) + '\\n')",
                        "if args[:2] == ['rm', '-f']:",
                        "    raise SystemExit(0)",
                        "if args and args[0] == 'build':",
                        "    raise SystemExit(0)",
                        "if not args or args[0] != 'run':",
                        "    raise SystemExit(2)",
                        "env = os.environ.copy()",
                        "cwd = None",
                        "index = 1",
                        "value_options = {'--name', '--network', '-v', '--volume', '--cpus', '--memory'}",
                        "while index < len(args):",
                        "    arg = args[index]",
                        "    if arg == '--rm':",
                        "        index += 1",
                        "        continue",
                        "    if arg in {'-w', '--workdir'}:",
                        "        cwd = args[index + 1]",
                        "        index += 2",
                        "        continue",
                        "    if arg in {'-e', '--env'}:",
                        "        key, value = args[index + 1].split('=', 1)",
                        "        env[key] = value",
                        "        index += 2",
                        "        continue",
                        "    if arg in value_options:",
                        "        index += 2",
                        "        continue",
                        "    if arg.startswith('-'):",
                        "        index += 1",
                        "        continue",
                        "    image = arg",
                        "    command = args[index + 1:]",
                        "    break",
                        "else:",
                        "    raise SystemExit(3)",
                        "completed = subprocess.run(command, cwd=cwd, env=env, check=False)",
                        "raise SystemExit(completed.returncode)",
                    ]
                ),
                encoding="utf-8",
            )
            fake_container.chmod(0o755)
            (tmp_path / "Dockerfile.worker").write_text("FROM python:3.11-slim\n", encoding="utf-8")
            raw_spec["execution"]["backend"] = {
                "type": "container",
                "implementation": "builtin.container_backend",
                "config": {
                    "containerExecutable": str(fake_container),
                    "image": "optpilot-test-image",
                    "pythonExecutable": sys.executable,
                    "build": {
                        "context": str(tmp_path),
                        "dockerfile": "Dockerfile.worker",
                        "tag": "optpilot-test-image",
                        "args": {"WORKER": "test"},
                    },
                },
            }
            raw_spec["execution"]["defaults"]["sandboxSpec"]["runtimeType"] = "container"
            spec_path = tmp_path / "container.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            old_log_env = os.environ.get("OPTPILOT_FAKE_CONTAINER_LOG")
            os.environ["OPTPILOT_FAKE_CONTAINER_LOG"] = str(fake_log)
            try:
                summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            finally:
                if old_log_env is None:
                    os.environ.pop("OPTPILOT_FAKE_CONTAINER_LOG", None)
                else:
                    os.environ["OPTPILOT_FAKE_CONTAINER_LOG"] = old_log_env

            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            trials = self._read_jsonl(run_dir / "trials.jsonl")
            fake_invocations = [json.loads(line) for line in fake_log.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(observations[0]["status"], "success")
            self.assertEqual(observations[0]["provenance"]["backend_worker"]["backend"], "local_container")
            self.assertEqual(trials[0]["backend_worker"]["container_image"], "optpilot-test-image")
            self.assertEqual(trials[0]["backend_worker"]["container_build"]["status"], "built")
            self.assertEqual(trials[0]["sandbox_spec"]["runtimeType"], "container")
            self.assertEqual(fake_invocations[0][0], "build")
            self.assertIn("--build-arg", fake_invocations[0])
            self.assertIn("optpilot-test-image", fake_invocations[-1])
            self.assertIn("--network", fake_invocations[-1])

    def test_study_spec_rejects_unknown_environment_policy(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["environment"]["accessPolicy"] = "MagicAccess"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "bad_policy.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported environment.accessPolicy"):
                load_expanded_study_spec(str(spec_path))

    def test_invalid_candidate_records_invalid_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml")
        raw_spec["metadata"]["name"] = "toy-invalid-candidate"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["method"]["config"]["candidates"] = [{"x": 99.0, "y": 7, "mode": "balanced"}]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "invalid.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")
            candidates = self._read_jsonl(Path(summary.run_dir) / "candidates.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertIsNone(summary.best_metric)
            self.assertEqual(observations[0]["status"], "invalid")
            self.assertEqual(trials[0]["status"], "invalid")
            self.assertFalse(candidates[0]["validation"]["accepted"])
            self.assertEqual(observations[0]["event_summary"]["error"]["phase"], "validation")

    def test_max_failures_stops_study_after_failed_trial(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml")
        raw_spec["metadata"]["name"] = "toy-max-failures"
        raw_spec["stopping"]["maxTrials"] = 3
        raw_spec["stopping"]["maxFailures"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["method"]["config"]["candidates"] = [
            {"x": 99.0, "y": 7, "mode": "balanced"},
            {"x": 4.2, "y": 7, "mode": "balanced"},
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "max_failures.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")
            summary_payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(summary.failure_count, 1)
            self.assertEqual(len(observations), 1)
            self.assertEqual([call["event"] for call in method_calls], ["proposed", "observed"])
            self.assertEqual(observations[0]["status"], "invalid")
            self.assertEqual(summary_payload["failure_count"], 1)

    def test_cli_nonzero_exit_records_failed_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-cli-failure"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["command"] = [
            "python3",
            "-c",
            "import sys; sys.stderr.write('boom'); sys.exit(3)",
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "cli_failure.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(observations[0]["status"], "failed")
            self.assertEqual(trials[0]["status"], "failed")
            self.assertEqual(observations[0]["event_summary"]["errors"][0]["phase"], "environment_evaluation")
            self.assertIn("exit code 3", observations[0]["event_summary"]["errors"][0]["message"])

    def test_invalid_target_output_records_failed_observation(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-invalid-target-output"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["callable"] = "tests.fixtures.bad_targets:non_numeric_metric"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "invalid_target_output.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertIsNone(summary.best_metric)
            self.assertEqual(observations[0]["status"], "failed")
            self.assertEqual(observations[0]["event_summary"]["errors"][0]["phase"], "environment_evaluation")
            self.assertIn("must be numeric", observations[0]["event_summary"]["errors"][0]["message"])

    def test_cli_timeout_records_timeout_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-cli-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["timeoutSeconds"] = 1
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["command"] = [
            "python3",
            "-c",
            "import time; time.sleep(2)",
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "cli_timeout.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(observations[0]["status"], "timeout")
            self.assertEqual(observations[0]["event_summary"]["errors"][0]["type"], "TimeoutExpired")

    def test_resource_profile_timeout_is_used_when_adapter_timeout_is_absent(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-resource-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["execution"].setdefault("defaults", {})["resourceProfile"] = {"timeoutSeconds": 1}
        raw_spec["method"].setdefault("resourceProfile", {})["timeoutSeconds"] = 1
        raw_spec["environment"]["runtimeContract"] = {"timeoutSeconds": 30}
        raw_spec["environment"]["adapter"]["config"]["evaluate"].pop("timeoutSeconds", None)
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["command"] = [
            "{python}",
            "-c",
            "import time; time.sleep(2)",
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "resource_timeout.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertEqual(observations[0]["status"], "timeout")
            self.assertEqual(observations[0]["provenance"]["resource_profile"]["timeoutSeconds"], 1)

    def test_local_subprocess_backend_runs_successful_trial(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-subprocess-success"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["execution"]["backend"]["implementation"] = "builtin.local_subprocess_backend"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "subprocess_success.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(observations[0]["status"], "success")
            self.assertEqual(observations[0]["provenance"]["backend_worker"]["backend"], "local_subprocess")
            self.assertEqual(trials[0]["backend_worker"]["backend"], "local_subprocess")

    def test_local_subprocess_backend_hard_times_out_python_callable_target(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-subprocess-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["method"]["config"]["batchSize"] = 1
        raw_spec["environment"]["adapter"]["config"]["evaluate"]["config"]["sleep_seconds"] = 5.0
        raw_spec["execution"]["backend"]["implementation"] = "builtin.local_subprocess_backend"
        raw_spec["execution"].setdefault("defaults", {})["resourceProfile"] = {"timeoutSeconds": 1}
        raw_spec["method"].setdefault("resourceProfile", {})["timeoutSeconds"] = 1
        raw_spec["environment"]["runtimeContract"] = {"timeoutSeconds": 30}

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "subprocess_timeout.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(observations[-1]["status"], "timeout")
            self.assertEqual(observations[-1]["event_summary"]["errors"][0]["phase"], "backend_execution")
            self.assertEqual(observations[-1]["provenance"]["backend_worker"]["backend"], "local_subprocess")
            self.assertEqual(trials[-1]["status"], "timeout")

    def test_scheduler_retry_policy_retries_failed_attempt_and_records_worker_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            marker = tmp_path / "first_attempt_seen.txt"
            evaluator = tmp_path / "flaky_eval.py"
            evaluator.write_text(
                "\n".join(
                    [
                        "import argparse, json, pathlib, sys",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--marker')",
                        "parser.add_argument('--metrics')",
                        "args = parser.parse_args()",
                        "marker = pathlib.Path(args.marker)",
                        "if not marker.exists():",
                        "    marker.write_text('seen', encoding='utf-8')",
                        "    sys.stderr.write('intentional first-attempt failure')",
                        "    sys.exit(2)",
                        "pathlib.Path(args.metrics).write_text(json.dumps({'metric_values': {'score': 9.0}}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            spec = {
                "apiVersion": "optpilot/v1",
                "config": "run_spec",
                "metadata": {"name": "retry-policy-check"},
                "environment": {
                    "environmentId": "flaky-environment",
                    "accessPolicy": "InvocationOnly",
                    "mutationPolicy": "NoMutation",
                    "adapter": {
                        "implementation": "builtin.configured_environment",
                        "config": {
                            "evaluate": {
                                "type": "command",
                                "command": [
                                    "{python}",
                                    str(evaluator),
                                    "--marker",
                                    str(marker),
                                    "--metrics",
                                    "{metrics_file}",
                                ],
                            },
                            "candidate": {"format": "parameters"},
                            "metrics": {"source": "file", "path": "metrics.json"},
                        },
                    },
                    "runtimeContract": {"timeoutSeconds": 30},
                },
                "objective": {"primaryMetric": {"name": "score", "direction": "maximize"}},
                "candidate": {
                    "format": "parameters",
                    "context": {"candidate": {"format": "parameters"}},
                    "validation": {
                        "implementation": "builtin.schema_validation",
                        "config": {"enforceBounds": False},
                    },
                    "materialization": {"implementation": "builtin.parameter_to_config", "config": {}},
                },
                "method": {
                    "id": "method",
                    "implementation": {
                        "type": "python",
                        "callable": "tests.fixtures.catalog.user_methods.fixed_parameter_method:FixedParameterMethod",
                        "protocol": "optpilot.method.batch.v1",
                    },
                    "config": {"batchSize": 1, "candidates": [{"x": 1}]},
                },
                "execution": {
                    "backend": {"implementation": "builtin.local_backend", "config": {}},
                    "scheduler": {
                        "implementation": "builtin.local_scheduler",
                        "config": {"retryPolicy": {"maxAttempts": 2, "retryStatuses": ["failed"]}},
                    },
                    "parallelism": {"candidateParallelism": 1},
                },
                "evidence": {"store": {"metadataBackend": "local_json", "outputFileBackend": "local_fs"}},
                "reproducibility": {"seedPolicy": {"globalSeed": 0}},
                "stopping": {"maxTrials": 1},
            }
            spec_path = tmp_path / "retry.yaml"
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            scheduler_events = self._read_jsonl(run_dir / "scheduler_events.jsonl")
            logical_trial_id = observations[0]["trial_id"]

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(summary.best_metric, 9.0)
            self.assertEqual([observation["status"] for observation in observations], ["failed", "success"])
            self.assertEqual([observation["trial_id"] for observation in observations], [logical_trial_id, logical_trial_id])
            self.assertEqual([observation["provenance"]["attempt_index"] for observation in observations], [1, 2])
            self.assertTrue((run_dir / "trials" / logical_trial_id / "attempt-1").exists())
            self.assertTrue((run_dir / "trials" / logical_trial_id / "attempt-2").exists())
            self.assertTrue(any(event["event"] == "trial_retried" for event in scheduler_events))
            retry_event = next(event for event in scheduler_events if event["event"] == "trial_retried")
            self.assertEqual(retry_event["next_trial_id"], logical_trial_id)
            collected_event = scheduler_events[-1]
            self.assertEqual(collected_event["handles"][0]["attempt_count"], 2)
            self.assertEqual(observations[-1]["provenance"]["backend_worker"]["backend"], "local_thread")
            self.assertIn("handle-", observations[-1]["provenance"]["backend_worker"]["handle"])

    def test_mixed_success_and_invalid_batch_continues_and_records_all_trials(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_user_method.yaml")
        raw_spec["metadata"]["name"] = "toy-mixed-batch"
        raw_spec["stopping"]["maxTrials"] = 2
        raw_spec["method"]["config"]["batchSize"] = 2
        raw_spec["method"]["config"]["candidates"] = [
            {"x": 4.2, "y": 7, "mode": "balanced"},
            {"x": 99.0, "y": 7, "mode": "balanced"},
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "mixed.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertEqual(sorted(observation["status"] for observation in observations), ["invalid", "success"])
            self.assertEqual(len(trials), 2)
            self.assertIsNotNone(summary.best_metric)

    def test_user_owned_method_reads_prior_evidence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_evidence_aware_method.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            method_calls = self._read_jsonl(run_dir / "method_calls.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertEqual([observation["status"] for observation in observations], ["invalid", "success"])
            self.assertEqual([call["event"] for call in method_calls], ["proposed", "observed", "proposed", "observed"])
            self.assertEqual(method_calls[0]["payload"]["study_state"]["completed_trials"], 0)
            self.assertEqual(method_calls[2]["payload"]["study_state"]["completed_trials"], 1)

    def test_local_evidence_store_read_api_and_summary_view(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
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
        repo_root = Path(__file__).resolve().parents[1]
        state = UiState(cwd=repo_root, catalog_roots=[repo_root / "catalog" / "example_package"], run_roots=[])

        catalog = _catalog_payload(state)
        validation = _validate_study(repo_root / "catalog" / "example_package" / "studies" / "job_shop_rule_parameters_baseline.yaml")

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
        repo_root = Path(__file__).resolve().parents[1]
        package = repo_root / "catalog" / "example_package"

        result = validate_package(package)
        entry_ids = {(entry["config"], entry["id"]) for entry in result["entries"]}

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["package_id"], "example_package")
        self.assertGreaterEqual(result["counts"]["environment"], 3)
        self.assertGreaterEqual(result["counts"]["method"], 6)
        self.assertGreaterEqual(result["counts"]["study"], 6)
        self.assertIn(("environment", "job-shop-rule-parameters"), entry_ids)
        self.assertIn(("method", "tune-dispatch-weights"), entry_ids)
        self.assertEqual(result["counts"]["resource"], 0)

    def test_core_package_roots_expand_catalog_folder_to_packages(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        roots = expand_package_roots([repo_root / "catalog"])

        self.assertEqual(roots, [repo_root / "catalog" / "example_package"])

    def test_cli_package_validate_json_output(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(["package", "validate", str(repo_root / "catalog" / "example_package"), "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["valid"], payload)
        self.assertEqual(payload["package_id"], "example_package")
        self.assertIn("entries", payload)

    def test_ui_catalog_exposes_complete_component_config_yaml(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        state = UiState(cwd=repo_root, catalog_roots=[repo_root / "catalog" / "example_package"], run_roots=[])

        catalog = _catalog_payload(state)
        environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-rule-parameters")
        method = next(item for item in catalog["methods"] if item["id"] == "tune-dispatch-weights")

        self.assertIn("config: environment", environment["yaml"])
        self.assertIn("candidate:", environment["yaml"])
        self.assertIn("metrics:", environment["yaml"])
        self.assertIn("config: method", method["yaml"])
        self.assertIn("entrypoint:", method["yaml"])
        self.assertIn("accepts:", method["yaml"])
        self.assertEqual(catalog["resources"], [])

        environment_detail = _catalog_detail(state, "environment", environment["uid"])
        method_detail = _catalog_detail(state, "method", method["uid"])

        self.assertTrue(environment_detail["validation"]["valid"], environment_detail)
        self.assertTrue(method_detail["validation"]["valid"], method_detail)
        self.assertEqual(environment_detail["config"]["config"], "environment")
        self.assertEqual(method_detail["config"]["config"], "method")
        self.assertIn("config: environment", environment_detail["yaml"])
        self.assertIn("config: method", method_detail["yaml"])

    def test_ui_catalog_edit_copy_writes_overridden_config(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(
                cwd=repo_root,
                catalog_roots=[repo_root / "catalog" / "example_package"],
                run_roots=[],
            )
            state.workspaces_dir = (tmp_path / "workspaces").resolve()
            state.workspaces_dir.mkdir(parents=True)
            catalog = _catalog_payload(state)
            environment = next(item for item in catalog["environments"] if item["id"] == "job-shop-rule-parameters")
            edited = deepcopy(environment["raw_config"])
            edited["description"] = "Edited from Studio form."
            edited["runtime"] = {"sandbox": "process"}

            workspace = _open_catalog_workspace(
                state,
                "environment",
                environment["uid"],
                editable=True,
                config_override=edited,
            )
            config_path = Path(workspace["root"]) / workspace["registered_entries"][0]["config_path"]
            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["description"], "Edited from Studio form.")
        self.assertEqual(saved["runtime"]["sandbox"], "process")
        self.assertTrue(workspace["validation"]["valid"], workspace["validation"])

    def test_ui_default_catalog_roots_are_catalog_packages(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        roots = _default_catalog_roots(repo_root)
        state = UiState(cwd=repo_root, catalog_roots=[], run_roots=[])

        catalog = _catalog_payload(state)

        self.assertEqual(
            roots,
            [repo_root / "catalog" / "example_package"],
        )
        self.assertEqual(state.catalog_roots, roots)
        environment_ids = {item["id"] for item in catalog["environments"]}
        method_ids = {item["id"] for item in catalog["methods"]}
        study_labels = {item["label"] for item in catalog["studies"]}

        self.assertIn("job-shop-rule-parameters", environment_ids)
        self.assertIn("job-shop-dispatch-rule", environment_ids)
        self.assertIn("openai-file-editor", method_ids)
        self.assertIn("fixed-rule-parameters", method_ids)
        self.assertIn("job-shop-rule-parameters-baseline", study_labels)
        self.assertTrue(catalog["environments"])
        self.assertTrue(catalog["methods"])
        self.assertTrue(catalog["studies"])

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
                        "  port: 5173",
                        "  extraPorts: [8000]",
                        "  readyPath: /health",
                        "  readyTimeoutSeconds: 30",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            state = UiState(cwd=tmp_path, catalog_roots=[tmp_path / "catalog" / "local_package"], run_roots=[])

            catalog = _catalog_payload(state)

        self.assertEqual(len(catalog["resources"]), 1)
        entry = catalog["resources"][0]
        self.assertEqual(entry["id"], "ui-tool")
        self.assertEqual(entry["interface"]["label"], "Demo UI")
        self.assertEqual(entry["interface"]["port"], 5173)
        self.assertEqual(entry["summary"]["interface"]["extraPorts"], [8000])
        self.assertEqual(entry["summary"]["interface"]["readyPath"], "/health")
        self.assertEqual(entry["summary"]["interface"]["readyTimeoutSeconds"], 30)

    def test_ui_launches_catalog_resource_interface_in_workspace_runtime(self) -> None:
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
                        "  port: 5173",
                        "  extraPorts: [8000]",
                        "  readyTimeoutSeconds: 0",
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
            resource_entry = _catalog_payload(state)["resources"][0]

            launched = _launch_catalog_interface(state, "resource", resource_entry["uid"])
            calls = _fake_workspace_container_calls(tmp_path)
            copied_index_exists = Path(launched["workspace"]["root"], "index.html").exists()
            deleted = _delete_ui_workspace(state, launched["workspace"]["id"])
            copied_root_exists_after_delete = Path(launched["workspace"]["root"]).exists()
            source_exists_after_delete = resource.exists()

        self.assertEqual(launched["workspace"]["mode"], "editable")
        self.assertEqual(launched["workspace"]["source_type"], "catalog-copy")
        self.assertEqual(launched["workspace"]["delete_label"], "Delete Copy")
        self.assertTrue(launched["workspace"]["title"].startswith("Launch Preview Tool"))
        self.assertTrue(copied_index_exists)
        self.assertTrue(deleted["files_deleted"])
        self.assertEqual(deleted["delete_label"], "Delete Copy")
        self.assertFalse(copied_root_exists_after_delete)
        self.assertTrue(source_exists_after_delete)
        self.assertEqual(launched["interface"]["port"], 5173)
        self.assertEqual(launched["preview"]["workspace_id"], launched["workspace"]["id"])
        self.assertEqual(launched["preview"]["allowed_ports"], [5173, 8000])
        preview_url = urlparse(launched["preview"]["preview_url"])
        self.assertIn("__optpilot_preview_token", parse_qs(preview_url.query))
        detached_execs = [call for call in calls if call and call[0] == "exec" and "-d" in call]
        self.assertTrue(detached_execs, calls)
        self.assertTrue(any("http.server" in " ".join(call) for call in detached_execs), detached_execs)

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
                        "  port: 5173",
                        "  readyTimeoutSeconds: 0",
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
            deleted = _delete_ui_workspace(state, current["result"]["workspace"]["id"])

        self.assertEqual(current["status"], "ready")
        self.assertTrue(deleted["files_deleted"])
        step_titles = [step["title"] for step in current["steps"]]
        self.assertIn("Creating editable workspace", step_titles)
        self.assertIn("Starting workspace runtime", step_titles)
        self.assertIn("Waiting for preview port", step_titles)
        self.assertIn("Preview ready", step_titles)
        self.assertEqual(current["result"]["workspace"]["mode"], "editable")
        self.assertEqual(current["result"]["interface"]["port"], 5173)

    def test_public_config_schema_allows_environment_and_method_interfaces(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        environment = yaml.safe_load((repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml").read_text(encoding="utf-8"))
        environment["interface"] = {
            "command": ["python", "-m", "http.server", "5173", "--bind", "0.0.0.0"],
            "port": 5173,
            "readyPath": "/",
            "readyTimeoutSeconds": 10,
        }
        method = yaml.safe_load((repo_root / "tests" / "fixtures" / "catalog" / "methods" / "fixed_parameter_method.yaml").read_text(encoding="utf-8"))
        method["interface"] = {
            "command": ["python", "-m", "http.server", "5174", "--bind", "0.0.0.0"],
            "port": 5174,
        }

        self.assertTrue(validate_public_config_schema(environment).valid)
        self.assertTrue(validate_public_config_schema(method).valid)

    def test_ui_compatibility_payload_and_study_draft(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = UiState(cwd=repo_root, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[])
            state.jobs_dir = Path(tmp_dir) / "jobs"
            state.jobs_dir.mkdir(parents=True, exist_ok=True)

            compatibility = _compatibility_payload(state)
            toy_pair = next(
                item
                for item in compatibility["pairs"]
                if item["environment"]["id"] == "toy-factory"
                and item["method"]["id"] == "reference-random-search"
            )

            self.assertTrue(toy_pair["compatible"], toy_pair)

            draft = _draft_study(
                state,
                {
                    "environment_path": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                    "method_path": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                    "name": "ui-draft-toy",
                    "description": "Draft created through the full Studio study form.",
                    "tags": ["ui", "draft"],
                    "metric": "throughput",
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
                    "evidenceStorage": "copy",
                    "evidenceOutputDir": "runs/ui-draft-toy",
                    "seed": 123,
                },
            )

            self.assertTrue(draft["validation"]["valid"], draft)
            self.assertTrue(Path(draft["path"]).exists())
            draft_doc = draft["draft"]
            self.assertEqual(draft_doc["name"], "ui-draft-toy")
            self.assertEqual(draft_doc["description"], "Draft created through the full Studio study form.")
            self.assertEqual(draft_doc["tags"], ["ui", "draft"])
            self.assertEqual(draft_doc["objective"]["secondaryMetrics"], ["cost"])
            self.assertEqual(draft_doc["budget"]["maxWallClockSeconds"], 3600)
            self.assertEqual(draft_doc["budget"]["maxFailures"], 2)
            self.assertEqual(draft_doc["execution"]["retry"], {"maxRetries": 1})
            self.assertEqual(draft_doc["evidence"]["level"], "full")
            self.assertEqual(draft_doc["evidence"]["outputFileStorage"], "copy")
            self.assertEqual(draft_doc["evidence"]["outputDir"], "runs/ui-draft-toy")
            self.assertEqual(draft_doc["reproducibility"], {"seed": 123})
            no_failure_limit_draft = _draft_study(
                state,
                {
                    "environment_path": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                    "method_path": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                    "name": "ui-draft-no-failure-limit",
                    "metric": "throughput",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "maxFailures": 0,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                },
            )
            self.assertTrue(no_failure_limit_draft["validation"]["valid"], no_failure_limit_draft)
            self.assertNotIn("maxFailures", no_failure_limit_draft["draft"]["budget"])

            examples_state = UiState(cwd=repo_root, catalog_roots=[repo_root / "catalog" / "example_package"], run_roots=[])
            examples_state.jobs_dir = Path(tmp_dir) / "example-jobs"
            examples_state.jobs_dir.mkdir(parents=True, exist_ok=True)
            openai_file_draft = _draft_study(
                examples_state,
                {
                    "environment_path": str(repo_root / "catalog" / "example_package" / "environments" / "job_shop_scheduling" / "environment_dispatch_rule.yaml"),
                    "method_path": str(repo_root / "catalog" / "example_package" / "methods" / "openai_file_editor" / "method.yaml"),
                    "name": "ui-draft-openai-file",
                    "metric": "normalized_makespan",
                    "direction": "minimize",
                    "maxTrials": 1,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                },
            )

            self.assertTrue(openai_file_draft["validation"]["valid"], openai_file_draft)
            self.assertNotIn("instances", openai_file_draft["draft"])
            incompatible_schedule_draft = _draft_study(
                examples_state,
                {
                    "environment_path": str(repo_root / "catalog" / "example_package" / "environments" / "job_shop_scheduling" / "environment_rule_parameters.yaml"),
                    "method_path": str(repo_root / "catalog" / "example_package" / "methods" / "ortools_cpsat_solver" / "method.yaml"),
                    "name": "bad-schedule-draft",
                    "metric": "makespan",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "parallelism": 1,
                    "timeoutSeconds": 120,
                },
            )
            self.assertFalse(incompatible_schedule_draft["compatibility"]["compatible"])
            self.assertFalse(incompatible_schedule_draft["validation"]["valid"])
            self.assertIn("is incompatible", " ".join(incompatible_schedule_draft["validation"]["errors"]))
            self.assertTrue(incompatible_schedule_draft["compatibility"]["reasons"])

    def test_ui_study_plan_workspace_is_persisted(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state = UiState(cwd=tmp_path, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[])
            state.jobs_dir = tmp_path / "jobs"
            state.jobs_dir.mkdir(parents=True, exist_ok=True)
            state.workspaces_dir = tmp_path / "workspaces"
            state.workspaces_dir.mkdir(parents=True, exist_ok=True)

            workspace = _open_study_workspace(
                state,
                {
                    "environment_path": str(repo_root / "tests" / "fixtures" / "catalog" / "environments" / "toy_factory.yaml"),
                    "method_path": str(repo_root / "tests" / "fixtures" / "catalog" / "methods" / "reference_random_search.yaml"),
                    "name": "ui-study-workspace",
                    "metric": "throughput",
                    "direction": "maximize",
                    "maxTrials": 1,
                    "parallelism": 1,
                },
            )
            root = Path(workspace["root"])
            indexed = _list_ui_workspaces(state)

            self.assertEqual(workspace["source_type"], "study-plan")
            self.assertEqual(workspace["mode"], "editable")
            self.assertTrue((root / "study.yaml").exists())
            self.assertTrue((root / "README.md").exists())
            self.assertIn("ui-study-workspace", (root / "study.yaml").read_text(encoding="utf-8"))
            self.assertTrue(any(item["id"] == workspace["id"] for item in indexed))
            self.assertFalse(workspace["registration_enabled"])

    def test_ui_registration_skips_studies_and_registers_resources(self) -> None:
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
            root = Path(workspace["root"])
            (root / "study.yaml").write_text(
                "apiVersion: optpilot.io/v1\nconfig: study\nname: should-not-register\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "No environment or method"):
                _create_registration_manifest(state, workspace["id"], {"config_paths": ["study.yaml"]})

            created = _create_registration_manifest(
                state,
                workspace["id"],
                {"kind": "resource", "resource_id": "reusable-tool"},
            )
            applied = _apply_registration_manifest(state, workspace["id"], created["registration"]["id"])

            destination = tmp_path / "catalog" / "local_package" / "resources" / "reusable-tool"
            catalog = _catalog_payload(state)
            indexed = _list_ui_workspaces(state)

            self.assertTrue(applied["applied"])
            self.assertTrue((destination / "README.md").exists())
            self.assertIn((tmp_path / "catalog" / "local_package").resolve(), state.catalog_roots)
            self.assertTrue(any(entry["id"] == "reusable-tool" for entry in catalog["resources"]))
            self.assertTrue(any(entry["kind"] == "resource" for entry in applied["workspace"]["registered_entries"]))
            self.assertTrue(any(item["id"] == workspace["id"] and item["registered_entries"] for item in indexed))

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
        self.assertEqual(message_result["session"]["status"], "waiting_for_agent")
        self.assertIsNone(message_result["message"]["context"]["selected_workspace"])
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
            runtime_root = state.runtime_dir / workspace["id"]
            runtime_root.mkdir(parents=True, exist_ok=True)
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

            renamed = _rename_ui_workspace(state, workspace["id"], "  Solver prototype  ")
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
            runtime_root = state.runtime_dir / workspace["id"]
            runtime_root.mkdir(parents=True, exist_ok=True)
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
            runtime_root = state.runtime_dir / workspace["id"]
            runtime_root.mkdir(parents=True, exist_ok=True)
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
            runtime_root = state.runtime_dir / workspace["id"]
            runtime_root.mkdir(parents=True, exist_ok=True)

            listed = _list_ui_workspaces(state)

            self.assertFalse(any(item["id"] == workspace["id"] for item in listed))
            self.assertTrue(catalog_root.exists())
            self.assertFalse(runtime_root.exists())

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
            shell = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": [sys.executable, "-c", "print('assistant ok')"]},
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
            self.assertTrue(shell["ok"], shell)
            self.assertIn("assistant ok", shell["data"]["stdout"])
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
                {"command": ["./pip", "--version"], "_openhands_tool_call_id": "call-install-1"},
            )
            second = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_shell_run",
                {"command": ["./pip", "--version"], "_openhands_tool_call_id": "call-install-1"},
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
        self.assertIn("approved-pip", forwarded[0]["result"]["data"]["stdout"])
        self.assertEqual(persisted["status"], "waiting_for_agent")
        self.assertTrue(any(event["type"] == "openhands_tool_result_forwarded" for event in events))

    def test_ui_agent_docs_and_smoke_tools_are_available(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
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
            session = _create_agent_session(state, {"title": "Docs and smoke"})

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
                {"config_kind": "studies", "path": str(study_path)},
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
                if self.path == "/api/conversations/oh-test-conversation/ask_agent":
                    self._send_json({"response": "OpenHands saw the Catalog context."})
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/conversations/oh-test-conversation/events/search"):
                    self._send_json({
                        "items": [
                            {
                                "kind": "MessageEvent",
                                "source": "agent",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "OpenHands saw the Catalog context."}],
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
        self.assertIn("OptPilot Assistant", start_payload["agent"]["agent_context"]["system_message_suffix"])
        self.assertTrue(any(tool["name"] == "optpilot_catalog_list" for tool in start_payload["client_tools"]))
        preview_tool = next(tool for tool in start_payload["client_tools"] if tool["name"] == "optpilot_workspace_preview_open")
        self.assertIn("extra_ports", preview_tool["parameters"]["properties"])
        for tool in start_payload["client_tools"]:
            self.assertNotIn("kind", tool.get("parameters", {}).get("properties", {}))
        self.assertIn("\"current_page\": \"catalog\"", event_payload["content"][0]["text"])
        self.assertIn("\"id\": \"toy-factory\"", event_payload["content"][0]["text"])

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
                                        "kind": "MessageEvent",
                                        "source": "agent",
                                        "message": {
                                            "role": "assistant",
                                            "content": [{"type": "text", "text": "Catalog tool result received."}],
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
                                "kind": "MessageEvent",
                                "source": "agent",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "First answer."}],
                                },
                            }
                        )
                    if server_state["message_count"] >= 2:
                        items.append(
                            {
                                "id": "evt-new-answer",
                                "kind": "MessageEvent",
                                "source": "agent",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "Second answer."}],
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

    def test_ui_agent_http_bridge_rejects_cached_final_response_on_reused_conversation(self) -> None:
        server_state = {"message_count": 0, "search_count": 0}

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
                                        "kind": "MessageEvent",
                                        "source": "agent",
                                        "llm_message": {
                                            "role": "assistant",
                                            "content": [{"type": "text", "text": "Second answer."}],
                                        },
                                    }
                                ]
                            }
                        )
                    else:
                        self._send_json({"items": []})
                    return
                if self.path == "/api/conversations/oh-cached-final-conversation/agent_final_response":
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
        self.assertEqual(first["session"]["status"], "idle")
        self.assertEqual(second["session"]["status"], "waiting_for_agent")
        self.assertEqual(synced["status"], "idle")
        self.assertEqual(contents.count("First answer."), 1)
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

    def test_ui_agent_openhands_prefers_finish_message_and_ignores_waiting_text(self) -> None:
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
        waiting_event = {
            "id": "evt-waiting",
            "kind": "MessageEvent",
            "source": "agent",
            "llm_message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "The user still hasn't sent a new message. </think> (Waiting for your next message.)",
                    }
                ],
            },
        }

        answer = adapter._best_user_facing_answer([waiting_event, finish_event], set(), set())

        self.assertEqual(answer, "Install failed in the workspace runtime because Python and Node are unavailable.")
        self.assertEqual(adapter._event_assistant_text(waiting_event), "")
        self.assertEqual(
            adapter._user_facing_assistant_text(
                "The user did not send another message. </think> The task is complete on my side: waiting for the next request."
            ),
            "",
        )

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
        self.assertFalse(any("Waiting for your next message" in content for content in contents))

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

    def test_packaged_release_assets_mirror_source_docs_and_agent_files(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_root = repo_root / "docs"
        docs_assets_root = repo_root / "studio" / "src" / "optpilot_studio" / "docs_assets"
        source_docs = sorted(path for path in docs_root.glob("*.md") if path.is_file())
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
                    "openhands": {"enabled": False},
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

            result = state.workspace_preview_open(Path(workspace["root"]), 5173, extra_ports=[8000])
            calls = _fake_workspace_container_calls(tmp_path)

        self.assertEqual(result["workspace_id"], workspace["id"])
        self.assertEqual(result["port"], 5173)
        self.assertEqual(result["proxy"], "studio")
        preview_url = urlparse(result["preview_url"])
        self.assertEqual(preview_url.scheme, "http")
        self.assertEqual(preview_url.hostname, "127.0.0.1")
        self.assertIn("__optpilot_preview_token", parse_qs(preview_url.query))
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
        self.assertIn("__optpilot_preview_token", parse_qs(preview_url.query))
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
        repo_root = Path(__file__).resolve().parents[1]
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

    def test_ui_agent_run_tools_return_compact_evidence_payloads(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = LocalEvidenceStore(tmp_path, "assistant-run")
            store.write_spec(study_spec.raw)
            trial_workspace = store.create_trial_workspace("trial-ok")
            metrics_file = trial_workspace / "metrics.json"
            metrics_file.write_text(json.dumps({"throughput": 10.0}), encoding="utf-8")
            store.record_candidate(
                {
                    "candidate_id": "candidate-ok",
                    "method_id": "toy-random-search",
                    "format": "parameters",
                    "status": "success",
                }
            )
            store.record_trial(
                {
                    "trial_id": "trial-ok",
                    "candidate_id": "candidate-ok",
                    "status": "success",
                    "method_id": "toy-random-search",
                }
            )
            store.record_observation(
                {
                    "trial_id": "trial-ok",
                    "candidate_id": "candidate-ok",
                    "status": "success",
                    "metric_values": {"throughput": 10.0},
                    "output_files": [{"name": "metrics", "path": str(metrics_file), "type": "json"}],
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
            state = UiState(cwd=tmp_path, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[tmp_path])
            session = _create_agent_session(state, {"title": "Run tools"})

            detail = _execute_agent_tool(state, session["id"], "optpilot_run_detail", {"path": str(store.run_dir)})
            read = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_run_file_read",
                {"run_id": str(store.run_dir), "path": "observations.jsonl"},
            )
            missing = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_run_file_read",
                {"run_id": str(store.run_dir), "path": "run_summary.json"},
            )

        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["data"]["summary"]["completed_trials"], 1)
        self.assertEqual(detail["data"]["summary"]["failure_count"], 0)
        self.assertEqual(detail["data"]["best"]["candidate_id"], "candidate-ok")
        self.assertEqual(detail["data"]["observations"]["metric_keys"], ["throughput"])
        evidence_paths = {item["relative_path"] for item in detail["data"]["evidence_files"]}
        self.assertIn("summary.json", evidence_paths)
        self.assertIn("observations.jsonl", evidence_paths)
        self.assertIn("trials/trial-ok/metrics.json", evidence_paths)
        self.assertNotIn("study_spec", detail["data"])
        self.assertTrue(read["ok"], read)
        self.assertIn('"metric_values"', read["data"]["content"])
        self.assertFalse(missing["ok"], missing)
        self.assertIn("summary.json", missing["data"]["suggested_paths"])
        self.assertTrue(missing["data"]["available_files"])

    def test_ui_agent_run_workspaces_use_unique_ids_and_auto_attach(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_spec = load_study_spec(str(repo_root / "tests" / "fixtures" / "catalog" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            first_store = LocalEvidenceStore(tmp_path, "first-run")
            second_store = LocalEvidenceStore(tmp_path, "second-run")
            for store, candidate_id in ((first_store, "candidate-a"), (second_store, "candidate-b")):
                store.write_spec(study_spec.raw)
                store.record_observation(
                    {
                        "trial_id": f"trial-{candidate_id}",
                        "candidate_id": candidate_id,
                        "status": "success",
                        "metric_values": {"throughput": 1.0},
                    }
                )
                store.write_summary({"completed_trials": 1, "best_metric": 1.0, "best_candidate_id": candidate_id, "failure_count": 0})
            state = UiState(cwd=tmp_path, catalog_roots=[repo_root / "tests" / "fixtures" / "catalog"], run_roots=[tmp_path])
            session = _create_agent_session(state, {"title": "Run workspaces"})

            first = _execute_agent_tool(state, session["id"], "optpilot_run_open_workspace", {"path": str(first_store.run_dir)})
            second = _execute_agent_tool(state, session["id"], "optpilot_run_open_workspace", {"path": str(second_store.run_dir)})

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        first_id = first["data"]["workspace"]["id"]
        second_id = second["data"]["workspace"]["id"]
        self.assertNotEqual(first_id, second_id)
        self.assertIn(first_id, second["data"]["session"]["attached_workspace_ids"])
        self.assertIn(second_id, second["data"]["session"]["attached_workspace_ids"])

    def test_cli_parser_accepts_ui_command(self) -> None:
        args = build_parser().parse_args(
            [
                "ui",
                "--port",
                "9001",
                "--catalog",
                "catalog/example_package",
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
        self.assertEqual(args.catalog, ["catalog/example_package"])
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
