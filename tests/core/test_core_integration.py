"""Public configuration, candidate staging, package validation, and CLI integration."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import yaml

from optpilot.adapters import ReadOnlySQLiteQuery
from optpilot.candidate_materialization import (
    BoundsCandidateValidator,
    FileCandidateManifestValidator,
    WorkspaceBundleMaterializer,
)
from optpilot._retired_cli import build_parser, main as cli_main
from optpilot.candidate_staging import CandidateBundleStager, stage_candidate_file
from optpilot.config import compile_authoring_config
from optpilot.evidence import EvidenceView
from optpilot.environment import build_environment_snapshot
from optpilot.execution import _aggregate_metric_values, _worker_process_env
from optpilot.package_index import expand_package_roots
from optpilot.package_validation import validate_package
from optpilot.provenance import PromptStore, build_generator_record, build_model_record
from optpilot.runner import run_expanded_study_spec
from optpilot.schema_validation import validate_public_config_schema
from optpilot.spec import StudySpec, load_expanded_study_spec, load_study_spec
from optpilot.storage import LocalEvidenceStore


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


class CoreIntegrationTest(unittest.TestCase):
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
        self.assertIn(repo_root / "catalog" / "optpilot_tutorial", roots)
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
