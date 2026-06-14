from __future__ import annotations

import json
import hashlib
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from optpilot.artifacts import BoundsArtifactValidator, CodeArtifactManifestValidator, WorkspaceBundleMaterializer
from optpilot.adapters import ReadOnlySQLiteQuery
from optpilot.cli import build_parser, main as cli_main
from optpilot.code_artifacts import CodeArtifactStore, store_code_file
from optpilot.config import compile_authoring_config
from optpilot.evidence import EvidenceView
from optpilot.environment import build_environment_snapshot
from optpilot.execution import _aggregate_metric_values
from optpilot.importers import build_frontier_initial_artifact, build_frontier_unified_study_config
from optpilot.provenance import PromptStore, build_generator_record, build_model_record
from optpilot.runner import run_expanded_study_spec, run_study
from optpilot.spec import StudySpec, load_expanded_study_spec, load_study_spec
from optpilot.storage import LocalEvidenceStore
from optpilot.ui.server import UiState, _catalog_payload, _list_runs, _validate_study


class MvpIntegrationTest(unittest.TestCase):
    def test_sample_study_runs_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_random_search.yaml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            self.assertEqual(summary.completed_trials, 12)
            self.assertIsNotNone(summary.best_metric)
            self.assertGreater(summary.best_metric, 80.0)

            run_dir = Path(summary.run_dir)
            self.assertTrue((run_dir / "study_spec.json").exists())
            self.assertTrue((run_dir / "observations.jsonl").exists())
            self.assertTrue((run_dir / "summary.json").exists())
            self.assertTrue((run_dir / "controller_decisions.jsonl").exists())
            self.assertTrue((run_dir / "scheduler_events.jsonl").exists())
            self.assertTrue((run_dir / "engine_snapshots.jsonl").exists())
            self.assertTrue((run_dir / "trials.jsonl").exists())
            self.assertTrue((run_dir / "artifacts.jsonl").exists())
            self.assertTrue((run_dir / "run_policy.json").exists())
            self.assertTrue((run_dir / "environment_snapshot.json").exists())

            summary_payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            environment_snapshot = json.loads((run_dir / "environment_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["completed_trials"], 12)
            self.assertEqual(summary_payload["policy"]["target"]["accessPolicy"], "SchemaAware")
            self.assertIn("python", environment_snapshot)
            self.assertIn("platform", environment_snapshot)
            self.assertIn("packages", environment_snapshot)
            self.assertIn("dependency_files", environment_snapshot)
            self.assertEqual(environment_snapshot["study_spec"]["sha256"], self._sha256(spec_path))
            self.assertTrue(any(item["name"] == "pyproject.toml" for item in environment_snapshot["dependency_files"]))

            observations = self._read_jsonl(run_dir / "observations.jsonl")
            trials = self._read_jsonl(run_dir / "trials.jsonl")
            decisions = self._read_jsonl(run_dir / "controller_decisions.jsonl")
            scheduler_events = self._read_jsonl(run_dir / "scheduler_events.jsonl")
            engine_snapshots = self._read_jsonl(run_dir / "engine_snapshots.jsonl")
            artifacts = self._read_jsonl(run_dir / "artifacts.jsonl")
            run_policy = json.loads((run_dir / "run_policy.json").read_text(encoding="utf-8"))
            self.assertEqual(len(observations), 12)
            self.assertEqual(len(trials), 12)
            self.assertEqual(len(decisions), 3)
            self.assertEqual(len(scheduler_events), 6)
            self.assertEqual(len(engine_snapshots), 6)
            self.assertEqual(len(artifacts), 12)
            self.assertEqual(run_policy["target"]["mutationPolicy"], "NoMutation")
            self.assertEqual(run_policy["execution"]["backend"]["implementation"], "builtin.local_backend")
            self.assertEqual(run_policy["execution"]["scheduler"]["implementation"], "builtin.local_scheduler")
            self.assertEqual(scheduler_events[0]["event"], "batch_submitted")
            self.assertEqual(scheduler_events[1]["event"], "batch_collected")
            self.assertEqual(scheduler_events[1]["observation_count"], 4)
            self.assertEqual(engine_snapshots[0]["event"], "proposed")
            self.assertEqual(engine_snapshots[1]["event"], "observed")
            self.assertEqual(artifacts[0]["validation"]["accepted"], True)
            self.assertEqual(artifacts[0]["materialization"]["runtime_spec"], artifacts[0]["spec"])
            self.assertIn("materialization_plan", artifacts[0])
            self.assertIn("validation_rules", artifacts[0])
            self.assertIn("backend_identity", trials[0])
            self.assertIn("scheduler_identity", trials[0])
            for observation in observations:
                self.assertIn("throughput", observation["metric_values"])
                self.assertGreaterEqual(observation["resource_usage"]["wallClockSeconds"], 0.0)
                self.assertEqual(observation["provenance"]["seed"], 7)
                self.assertEqual(
                    observation["provenance"]["backend_identity"]["implementation"],
                    "builtin.local_backend",
                )
                self.assertEqual(
                    observation["provenance"]["scheduler_identity"]["implementation"],
                    "builtin.local_scheduler",
                )
                self.assertEqual(observation["provenance"]["resource_profile"]["timeoutSeconds"], 120)
                self.assertEqual(observation["provenance"]["sandbox_spec"]["cleanupPolicy"], "always")
                for artifact in observation["artifacts"]:
                    self.assertTrue(Path(artifact["path"]).exists())

    def test_distribution_scope_is_reproducible(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        base_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        base_spec["metadata"]["name"] = "toy-distribution-repro"
        base_spec["evaluationScope"] = {
            "mode": "Distribution",
            "definition": {
                "sampleCount": 3,
                "sampler": {
                    "implementation": "builtin.parameter_sampler",
                    "config": {
                        "target_x": [3.5, 4.5],
                        "target_y": [6, 8],
                    },
                },
            },
        }
        base_spec["stopping"]["maxTrials"] = 8
        base_spec["engines"][0]["config"]["batchSize"] = 4

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "distribution.yaml"
            spec_path.write_text(yaml.safe_dump(base_spec, sort_keys=False), encoding="utf-8")

            first = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            second = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)

            first_observations = self._read_jsonl(Path(first.run_dir) / "observations.jsonl")
            second_observations = self._read_jsonl(Path(second.run_dir) / "observations.jsonl")

            self.assertEqual(first.best_metric, second.best_metric)
            self.assertEqual(
                sorted(self._metric_signature(entry) for entry in first_observations),
                sorted(self._metric_signature(entry) for entry in second_observations),
            )

    def test_instance_set_aggregation_and_minimize_direction(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        base_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        base_spec["metadata"]["name"] = "toy-instance-set-minimize"
        base_spec["objective"]["primaryMetric"] = {"name": "cycle_time", "direction": "minimize"}
        base_spec["evaluationScope"] = {
            "mode": "InstanceSet",
            "definition": {
                "instanceRefs": ["instance_a.yaml", "instance_b.yaml"],
            },
        }
        base_spec["stopping"]["maxTrials"] = 8
        base_spec["engines"][0]["config"]["batchSize"] = 4

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "instance_a.yaml").write_text("target_x: 4.2\ntarget_y: 7\n", encoding="utf-8")
            (tmp_path / "instance_b.yaml").write_text("target_x: 4.4\ntarget_y: 7\n", encoding="utf-8")
            spec_path = tmp_path / "instance_set.yaml"
            spec_path.write_text(yaml.safe_dump(base_spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            best_cycle_time = min(entry["metric_values"]["cycle_time"] for entry in observations)

            self.assertEqual(summary.best_metric, best_cycle_time)
            for observation in observations:
                self.assertEqual(observation["instance_descriptor"]["count"], 2)
                self.assertGreaterEqual(len(observation["artifacts"]), 2)

    def test_documented_objective_aggregation_modes(self) -> None:
        instance_results = [
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
                self.assertEqual(_aggregate_metric_values(instance_results, objective)["score"], value)

    def test_candidate_parallelism_reduces_elapsed_time(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        base_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        base_spec["metadata"]["name"] = "toy-parallel-check"
        base_spec["evaluationScope"] = {
            "mode": "FixedInstance",
            "definition": {
                "instance": {"target_x": 4.2, "target_y": 7, "sleep_seconds": 0.2},
            },
        }
        base_spec["stopping"]["maxTrials"] = 4
        base_spec["engines"][0]["config"]["batchSize"] = 4
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
            spec_path.write_text("kind: StudySpec\n", encoding="utf-8")

            snapshot = build_environment_snapshot(study_spec_path=spec_path)
            dependencies = {Path(item["path"]).name: item for item in snapshot["dependency_files"]}

            self.assertEqual(dependencies["pyproject.toml"]["sha256"], self._sha256(pyproject))
            self.assertEqual(dependencies["uv.lock"]["kind"], "lockfile")
            self.assertEqual(dependencies["requirements.txt"]["sha256"], self._sha256(requirements))

    def test_bounds_validator_rejects_out_of_range_artifacts(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_random_search.yaml"
        raw_spec = compile_authoring_config(spec_path)
        study_spec = StudySpec(path=spec_path, raw=raw_spec)
        validator = BoundsArtifactValidator(
            raw_spec["artifacts"]["primaryArtifact"]["validationRules"],
            study_spec,
        )

        report = validator.validate(
            {
                "artifact_id": "artifact-invalid",
                "artifact_kind": "parameter_spec",
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
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "EnvironmentConfig",
                        "id": "nested-parameters",
                        "description": "Nested parameter contract.",
                        "evaluate": {"type": "python", "callable": "tests.fixtures.bad_targets:non_numeric_metric"},
                        "candidate": {
                            "type": "parameters",
                            "artifactKind": "parameter_spec",
                            "description": "Parameters accepted by the evaluator.",
                            "parameters": {
                                "schema": {
                                    "x": {"type": "float", "min": 0.0, "max": 10.0},
                                    "mode": {"type": "categorical", "values": ["safe", "fast"]},
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
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "MethodConfig",
                        "id": "parameter-method",
                        "description": "Parameter method.",
                        "engine": {"implementation": "builtin.reference_random_search"},
                        "compatibility": {
                            "candidateTypes": ["parameters"],
                            "artifactKinds": ["parameter_spec"],
                            "requiredContext": ["parameters.schema"],
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "StudyConfig",
                        "name": "nested-parameter-study",
                        "environment": "environment.yaml",
                        "method": "method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            raw_spec = compile_authoring_config(study_path)
            study_spec = StudySpec(path=study_path, raw=raw_spec)
            validator = BoundsArtifactValidator(raw_spec["artifacts"]["primaryArtifact"]["validationRules"], study_spec)
            report = validator.validate(
                {
                    "artifact_id": "artifact-constrained",
                    "artifact_kind": "parameter_spec",
                    "spec": {"x": 2.0, "mode": "fast"},
                },
                {},
            )

            self.assertEqual(raw_spec["engines"][0]["config"]["searchSpace"]["x"]["max"], 10.0)
            self.assertEqual(raw_spec["artifacts"]["primaryArtifact"]["candidateContext"]["parameters"]["schema"]["mode"]["values"], ["safe", "fast"])
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
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "EnvironmentConfig",
                        "id": "nested-files",
                        "description": "Nested file contract.",
                        "evaluate": {"type": "command", "command": ["python", "-c", "print('{}')"]},
                        "workspace": {
                            "copy": [
                                {"from": "source", "to": "candidate", "role": "source"},
                                {"from": "history.db", "to": "database.db", "role": "data"},
                            ],
                            "readonly": ["database.db"],
                        },
                        "interfaces": [
                            {
                                "id": "historical_db_query",
                                "capability": "optpilot.sqlite_query.v1",
                                "description": "Read-only SQL access.",
                                "adapter": {
                                    "implementation": "builtin.sqlite_query",
                                    "config": {"path": "database.db"},
                                },
                            }
                        ],
                        "candidate": {
                            "type": "files",
                            "artifactKind": "code_bundle",
                            "description": "Editable solver file.",
                            "files": {
                                "root": "candidate",
                                "source": {"type": "workspace_copy", "root": "candidate"},
                                "editable": [{"path": "solver.py", "language": "python", "role": "solver"}],
                                "required": ["solver.py"],
                                "allow": ["solver.py"],
                                "deny": ["database.db"],
                            },
                            "exposure": {
                                "instructions": ["instructions.md"],
                                "contextArtifacts": [
                                    {
                                        "id": "historical_database",
                                        "path": "database.db",
                                        "role": "historical_data",
                                        "mediaType": "application/vnd.sqlite3",
                                        "readonly": True,
                                    }
                                ],
                            },
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
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "MethodConfig",
                        "id": "file-editor",
                        "description": "File editor.",
                        "engine": {"implementation": "python:examples.user_engines.code_artifact_engine:CodeArtifactEngine"},
                        "compatibility": {
                            "candidateTypes": ["files"],
                            "artifactKinds": ["code_bundle"],
                            "requiredContext": ["files.source", "files.editable", "exposure.contextArtifacts"],
                            "requiredCapabilities": ["optpilot.sqlite_query.v1"],
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v3alpha1",
                        "kind": "StudyConfig",
                        "name": "nested-file-study",
                        "environment": "environment.yaml",
                        "method": "method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            raw_spec = compile_authoring_config(study_path)
            candidate_context = raw_spec["artifacts"]["primaryArtifact"]["candidateContext"]
            adapter_config = raw_spec["target"]["adapter"]["config"]

            self.assertEqual(raw_spec["artifacts"]["primaryArtifact"]["kind"], "code_bundle")
            self.assertEqual(candidate_context["files"]["editable"][0]["path"], "solver.py")
            self.assertEqual(candidate_context["exposure"]["instructions"], [str(instructions.resolve())])
            self.assertEqual(candidate_context["exposure"]["contextArtifacts"][0]["path"], "database.db")
            self.assertEqual(adapter_config["interfaces"][0]["capability"], "optpilot.sqlite_query.v1")
            self.assertEqual(adapter_config["interfaces"][0]["adapter"]["config"]["path"], str(database.resolve()))
            self.assertEqual(adapter_config["interfaces"][0]["adapter"]["config"]["pathWorkspacePath"], "database.db")
            self.assertEqual(candidate_context["interfaces"][0]["adapter"]["config"]["path"], str(database.resolve()))
            self.assertEqual(
                raw_spec["artifacts"]["primaryArtifact"]["validationRules"]["config"]["requiredFiles"],
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

    def test_code_bundle_manifest_validator_accepts_file_refs_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bundle_dir = tmp_path / "artifacts" / "artifact-code-001" / "files"
            bundle_dir.mkdir(parents=True)
            solver_path = bundle_dir / "solver.py"
            helper_path = bundle_dir / "utils" / "helper.py"
            helper_path.parent.mkdir()
            solver_path.write_text("from utils.helper import score\n\ndef solve(x):\n    return score(x)\n", encoding="utf-8")
            helper_path.write_text("def score(x):\n    return x + 1\n", encoding="utf-8")
            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = CodeArtifactManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )

            report = validator.validate(
                {
                    "artifact_id": "artifact-code-001",
                    "artifact_kind": "code_bundle",
                    "spec": {
                        "bundleRef": "artifacts/artifact-code-001/files",
                        "files": [
                            {
                                "path": "solver.py",
                                "contentRef": "artifacts/artifact-code-001/files/solver.py",
                                "sha256": self._sha256(solver_path),
                            },
                            {
                                "path": "utils/helper.py",
                                "contentRef": "artifacts/artifact-code-001/files/utils/helper.py",
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
            source_path = tmp_path / "artifacts" / "artifact-code-002" / "files" / "solver.py"
            source_path.parent.mkdir(parents=True)
            source_path.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = CodeArtifactManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )

            report = validator.validate(
                {
                    "artifact_id": "artifact-code-002",
                    "artifact_kind": "code_file",
                    "spec": {
                        "path": "../solver.py",
                        "content": "def solve(x): return x",
                        "contentRef": "artifacts/artifact-code-002/files/solver.py",
                        "sha256": self._sha256(source_path),
                    },
                },
                {},
            )

            self.assertFalse(report.accepted)
            self.assertTrue(any("Inline source content is not allowed" in error for error in report.errors))
            self.assertTrue(any("safe relative POSIX path" in error for error in report.errors))

    def test_code_artifact_store_creates_bundle_manifest_without_inline_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "generated"
            generated.mkdir()
            (generated / "solver.py").write_text("from utils.helper import score\n", encoding="utf-8")
            (generated / "utils").mkdir()
            (generated / "utils" / "helper.py").write_text("def score(x):\n    return x + 1\n", encoding="utf-8")
            (generated / "__pycache__").mkdir()
            (generated / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
            artifact_root = tmp_path / "artifact-store"
            store = CodeArtifactStore(artifact_root, content_ref_mode="absolute")

            artifact = store.store_directory(
                generated,
                artifact_id="artifact-generated-001",
                entrypoint="solver:solve",
                generator_record={"engine_id": "llm_engine", "strategy": "unit_test"},
            )

            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = CodeArtifactManifestValidator(
                {
                    "implementation": "builtin.workspace_policy",
                    "config": {"allowAbsoluteContentRefs": True},
                },
                study_spec,
            )
            report = validator.validate(artifact, {})

            self.assertTrue(report.accepted, report.errors)
            self.assertEqual(artifact["artifact_kind"], "code_bundle")
            self.assertEqual(artifact["spec"]["entrypoint"], "solver:solve")
            self.assertEqual(len(artifact["spec"]["files"]), 2)
            self.assertFalse(self._contains_key(artifact, "content"))
            self.assertTrue((artifact_root / "artifact-generated-001" / "files" / "utils" / "helper.py").exists())

    def test_code_artifact_store_supports_single_file_relative_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated = tmp_path / "solver.py"
            generated.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            artifact = store_code_file(
                generated,
                tmp_path / "artifacts",
                artifact_id="artifact-single-file",
                path="solver.py",
                content_ref_mode="relative",
                content_ref_base=tmp_path,
            )

            study_spec = StudySpec(path=tmp_path / "study.yaml", raw={})
            validator = CodeArtifactManifestValidator(
                {"implementation": "builtin.workspace_policy"},
                study_spec,
            )
            report = validator.validate(artifact, {})

            self.assertTrue(report.accepted, report.errors)
            self.assertEqual(artifact["artifact_kind"], "code_file")
            self.assertEqual(artifact["spec"]["path"], "solver.py")
            self.assertEqual(
                artifact["spec"]["contentRef"],
                "artifacts/artifact-single-file/files/solver.py",
            )

    def test_code_artifact_store_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "solver.py"
            source.write_text("def solve(x):\n    return x\n", encoding="utf-8")
            store = CodeArtifactStore(tmp_path / "artifacts")

            with self.assertRaisesRegex(ValueError, "Unsafe code artifact path"):
                store.store_files(
                    [{"source": source, "path": "../solver.py"}],
                    artifact_id="artifact-unsafe",
                )

            self.assertFalse((tmp_path / "artifacts" / "artifact-unsafe").exists())

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
            generator_record = build_generator_record(
                engine_id="llm_engine",
                strategy="code_evolution",
                prompt_record=prompt_record,
                model_record=model_record,
                extra={"owned_by": "user"},
            )

            prompt_path = tmp_path / prompt_record["contentRef"]
            self.assertTrue(prompt_path.exists())
            self.assertEqual(prompt_record["sha256"], self._sha256(prompt_path))
            self.assertEqual(generator_record["prompt_record_id"], "prompt-unit")
            self.assertEqual(generator_record["model_record"]["model"], "gpt-5")
            self.assertNotIn("Improve the solver", json.dumps(generator_record))

    def test_workspace_bundle_materializer_writes_candidate_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_dir = tmp_path / "artifacts" / "artifact-code-003" / "files"
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
                    "artifact_id": "artifact-code-003",
                    "artifact_kind": "code_bundle",
                    "spec": {
                        "bundleRef": "artifacts/artifact-code-003/files",
                        "files": [
                            {
                                "path": "solver.py",
                                "contentRef": "artifacts/artifact-code-003/files/solver.py",
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

    def test_frontier_unified_importer_builds_valid_study_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            benchmark = root / "Frontier-Engineering" / "benchmarks" / "Robotics" / "PIDTuning"
            metadata = benchmark / "frontier_eval"
            scripts = benchmark / "scripts"
            metadata.mkdir(parents=True)
            scripts.mkdir()
            (benchmark / "README.md").write_text("benchmark instructions", encoding="utf-8")
            initial_program = scripts / "init.py"
            initial_program.write_text("def controller():\n    return 1\n", encoding="utf-8")
            (metadata / "initial_program.txt").write_text("scripts/init.py\n", encoding="utf-8")
            (metadata / "candidate_destination.txt").write_text("scripts/init.py\n", encoding="utf-8")
            (metadata / "eval_command.txt").write_text(
                "{python} frontier_eval/run_eval.py --candidate {candidate} --metrics-out metrics.json\n",
                encoding="utf-8",
            )
            (metadata / "eval_cwd.txt").write_text(".\n", encoding="utf-8")
            (metadata / "copy_files.txt").write_text(".\n", encoding="utf-8")
            (metadata / "readonly_files.txt").write_text("README.md\nfrontier_eval\n", encoding="utf-8")
            (metadata / "artifact_files.txt").write_text("# no extra artifacts\n", encoding="utf-8")
            (metadata / "agent_files.txt").write_text("README.md\nscripts/init.py\n", encoding="utf-8")
            (metadata / "constraints.txt").write_text("Only modify scripts/init.py.", encoding="utf-8")
            (metadata / "run_eval.py").write_text("print('placeholder')\n", encoding="utf-8")

            spec_dict = build_frontier_unified_study_config(benchmark, max_trials=3)
            spec_path = root / "frontier_study.yaml"
            spec_path.write_text(yaml.safe_dump(spec_dict, sort_keys=False), encoding="utf-8")
            study_spec = load_study_spec(str(spec_path))

            self.assertEqual(study_spec.name, "frontier-robotics-pidtuning")
            self.assertEqual(study_spec.primary_artifact["kind"], "code_bundle")
            self.assertEqual(study_spec.target["adapter"]["implementation"], "builtin.configured_environment")
            self.assertEqual(
                study_spec.target["adapter"]["config"]["candidate"]["files"]["required"],
                ["scripts/init.py"],
            )
            self.assertEqual(study_spec.engines[0]["implementation"], "python:my_lab.engines:FrontierCodeEngine")

            artifact = build_frontier_initial_artifact(benchmark)
            materializer = WorkspaceBundleMaterializer(
                study_spec.primary_artifact["materializationPlan"],
                study_spec,
            )
            workspace = root / "trial-workspace"
            record = materializer.materialize(artifact, workspace, {})
            manifest = json.loads(Path(record.runtime_spec["manifestPath"]).read_text(encoding="utf-8"))

            self.assertTrue((workspace / "README.md").exists())
            self.assertEqual((workspace / "scripts" / "init.py").read_text(encoding="utf-8"), initial_program.read_text(encoding="utf-8"))
            self.assertEqual(record.runtime_spec["files"][0]["path"], "scripts/init.py")
            self.assertGreaterEqual(len(manifest["readonly_files"]), 2)

    def test_cli_import_frontier_writes_study_config_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            benchmark = root / "Frontier-Engineering" / "benchmarks" / "Robotics" / "PIDTuning"
            metadata = benchmark / "frontier_eval"
            scripts = benchmark / "scripts"
            metadata.mkdir(parents=True)
            scripts.mkdir()
            (benchmark / "README.md").write_text("benchmark instructions", encoding="utf-8")
            (scripts / "init.py").write_text("def controller():\n    return 1\n", encoding="utf-8")
            (metadata / "initial_program.txt").write_text("scripts/init.py\n", encoding="utf-8")
            (metadata / "candidate_destination.txt").write_text("scripts/init.py\n", encoding="utf-8")
            (metadata / "eval_command.txt").write_text(
                "{python} frontier_eval/run_eval.py --candidate {candidate} --metrics-out metrics.json\n",
                encoding="utf-8",
            )
            (metadata / "copy_files.txt").write_text(".\n", encoding="utf-8")
            (metadata / "readonly_files.txt").write_text("README.md\n", encoding="utf-8")
            (metadata / "agent_files.txt").write_text("README.md\nscripts/init.py\n", encoding="utf-8")
            (metadata / "constraints.txt").write_text("Only modify scripts/init.py.", encoding="utf-8")
            output = root / "generated" / "frontier.yaml"

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = cli_main(
                    [
                        "import-frontier",
                        str(benchmark),
                        "--output",
                        str(output),
                        "--max-trials",
                        "5",
                    ]
                )
            study_spec = load_study_spec(str(output))

            self.assertEqual(exit_code, 0)
            self.assertEqual(study_spec.stopping["maxTrials"], 5)
            self.assertEqual(study_spec.target["adapter"]["implementation"], "builtin.configured_environment")
            self.assertEqual(study_spec.primary_artifact["kind"], "code_bundle")
            self.assertEqual(study_spec.engines[0]["config"]["candidateDestination"], "scripts/init.py")

    def test_cli_run_loads_user_owned_components_from_current_working_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_user_engine.yaml"
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

    def test_cli_target_adapter_runs_and_captures_process_evidence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_cli_random_search.yaml"
        raw_spec = compile_authoring_config(spec_path)
        raw_spec["stopping"]["maxTrials"] = 4
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            temp_spec = tmp_path / "toy_cli_random_search.yaml"
            temp_spec.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(temp_spec), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            artifacts = self._read_jsonl(run_dir / "artifacts.jsonl")

            self.assertEqual(summary.completed_trials, 4)
            self.assertEqual(len(observations), 4)
            self.assertEqual(len(artifacts), 4)
            first_observation = observations[0]
            self.assertEqual(first_observation["provenance"]["backend_identity"]["implementation"], "builtin.local_backend")
            artifact_names = {artifact["name"]: artifact for artifact in first_observation["artifacts"] if "name" in artifact}
            self.assertIn("candidate_payload", artifact_names)
            self.assertIn("instance", artifact_names)
            self.assertIn("metrics", artifact_names)
            self.assertIn("stdout", artifact_names)
            self.assertIn("stderr", artifact_names)
            stdout_path = Path(artifact_names["stdout"]["path"])
            self.assertIn("wrote", stdout_path.read_text(encoding="utf-8"))
            self.assertEqual(artifacts[0]["materialization"]["runtime_spec"], artifacts[0]["spec"])

    def test_user_owned_engine_loads_through_python_hook(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_user_engine.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            artifacts = self._read_jsonl(Path(summary.run_dir) / "artifacts.jsonl")

            self.assertEqual(summary.completed_trials, 3)
            self.assertEqual(summary.best_metric, max(item["metric_values"]["throughput"] for item in observations))
            self.assertEqual(artifacts[0]["generator_record"]["owned_by"], "user")
            self.assertEqual(
                observations[0]["provenance"]["generator_record"]["strategy"],
                "fixed_parameter_user_engine",
            )

    def test_run_can_resume_existing_evidence_store(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_user_engine.yaml")
        raw_spec["metadata"]["name"] = "toy-resume-run"
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["engines"][0]["config"]["batchSize"] = 1
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
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_user_engine.yaml")
        raw_spec["metadata"]["name"] = "toy-branch-run"
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["engines"][0]["config"]["batchSize"] = 1
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

    def test_user_owned_code_artifact_engine_uses_run_artifact_store(self) -> None:
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
                "apiVersion": "optpilot/v3alpha",
                "kind": "StudySpec",
                "metadata": {"name": "code-artifact-engine"},
                "target": {
                    "targetId": "code-artifact-evaluator",
                    "accessPolicy": "CodeAwareReadOnly",
                    "mutationPolicy": "StudyArtifactOnly",
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
                            "candidate": {"type": "files", "required": ["solver.py"]},
                            "metrics": {"source": "file", "path": "metrics.json"},
                        },
                    },
                    "runtimeContract": {"timeoutSeconds": 30},
                },
                "objective": {"primaryMetric": {"name": "score", "direction": "maximize"}},
                "evaluationScope": {"mode": "FixedInstance", "definition": {"instance": {}}},
                "artifacts": {
                    "primaryArtifact": {
                        "kind": "code_bundle",
                        "candidateContext": {
                            "type": "files",
                            "artifactKind": "code_bundle",
                            "description": "Generated code source.",
                            "files": {
                                "root": ".",
                                "source": {"type": "workspace_copy", "root": "."},
                                "editable": [
                                    {"path": "solver.py", "language": "python", "role": "solver"}
                                ],
                                "required": ["solver.py"],
                                "allow": ["solver.py"],
                                "deny": [],
                            },
                            "workspace": {
                                "copy": [
                                    {"from": str(source_dir), "to": ".", "role": "source"}
                                ]
                            },
                            "exposure": {},
                            "interfaces": [],
                        },
                        "validationRules": {
                            "implementation": "builtin.workspace_policy",
                            "config": {"allowAbsoluteContentRefs": True},
                        },
                        "materializationPlan": {
                            "implementation": "builtin.workspace_bundle",
                            "config": {
                                "candidateRoot": ".",
                                "allowAbsoluteContentRefs": True,
                            },
                        },
                    }
                },
                "controllers": [
                    {
                        "id": "controller",
                        "implementation": "builtin.single_engine_controller",
                        "config": {"engineId": "code_engine"},
                    }
                ],
                "engines": [
                    {
                        "id": "code_engine",
                        "implementation": "python:examples.user_engines.code_artifact_engine:CodeArtifactEngine",
                        "config": {
                            "entrypoint": "solver:solve",
                            "provider": "example",
                            "model": "example-code-model",
                            "promptMessages": [
                                {"role": "system", "content": "Store this generated solver."},
                            ],
                        },
                    }
                ],
                "execution": {
                    "backend": {"implementation": "builtin.local_backend", "config": {}},
                    "scheduler": {"implementation": "builtin.local_scheduler", "config": {}},
                    "parallelism": {"candidateParallelism": 1},
                },
                "evidence": {"store": {"implementation": "builtin.local_jsonl", "config": {}}},
                "reproducibility": {"seed": 0},
                "stopping": {"maxTrials": 1},
            }
            spec_path = tmp_path / "code_engine.yaml"
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            artifacts = self._read_jsonl(Path(summary.run_dir) / "artifacts.jsonl")
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertEqual(summary.best_metric, 42.0)
            self.assertEqual(observations[0]["metric_values"]["score"], 42.0)
            content_ref = artifacts[0]["spec"]["files"][0]["contentRef"]
            self.assertIn(str(Path(summary.run_dir) / "artifacts"), content_ref)
            self.assertTrue(Path(content_ref).exists())
            prompt_record = artifacts[0]["generator_record"]["prompt_record"]
            self.assertTrue(Path(prompt_record["contentRef"]).exists())
            self.assertEqual(artifacts[0]["generator_record"]["model_record"]["model"], "example-code-model")

    def test_user_owned_lifecycle_engine_loads_through_python_hook(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_lifecycle_engine.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            artifacts = self._read_jsonl(run_dir / "artifacts.jsonl")
            engine_snapshots = self._read_jsonl(run_dir / "engine_snapshots.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(artifacts), 2)
            self.assertEqual(
                [snapshot["event"] for snapshot in engine_snapshots],
                ["started", "polled", "finalized", "observed"],
            )
            self.assertEqual(engine_snapshots[0]["payload"]["interface"], "lifecycle")
            self.assertEqual(artifacts[0]["generator_record"]["owned_by"], "user")
            self.assertEqual(
                observations[0]["provenance"]["generator_record"]["strategy"],
                "lifecycle_fixed_parameter_user_engine",
            )

    def test_study_spec_rejects_unimplemented_builtin_docker_backend(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        raw_spec["execution"]["backend"]["implementation"] = "builtin.docker_backend"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "docker.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "builtin.docker_backend is not implemented"):
                load_expanded_study_spec(str(spec_path))

    def test_study_spec_rejects_unknown_target_policy(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        raw_spec["target"]["accessPolicy"] = "MagicAccess"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "bad_policy.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported target.accessPolicy"):
                load_expanded_study_spec(str(spec_path))

    def test_invalid_artifact_records_invalid_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_user_engine.yaml")
        raw_spec["metadata"]["name"] = "toy-invalid-artifact"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["engines"][0]["config"]["candidates"] = [{"x": 99.0, "y": 7, "mode": "balanced"}]
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "invalid.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            trials = self._read_jsonl(Path(summary.run_dir) / "trials.jsonl")
            artifacts = self._read_jsonl(Path(summary.run_dir) / "artifacts.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertIsNone(summary.best_metric)
            self.assertEqual(observations[0]["status"], "invalid")
            self.assertEqual(trials[0]["status"], "invalid")
            self.assertFalse(artifacts[0]["validation"]["accepted"])
            self.assertEqual(observations[0]["event_summary"]["error"]["phase"], "validation")

    def test_max_failures_stops_study_after_failed_trial(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_user_engine.yaml")
        raw_spec["metadata"]["name"] = "toy-max-failures"
        raw_spec["stopping"]["maxTrials"] = 3
        raw_spec["stopping"]["maxFailures"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["engines"][0]["config"]["candidates"] = [
            {"x": 99.0, "y": 7, "mode": "balanced"},
            {"x": 4.2, "y": 7, "mode": "balanced"},
        ]
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "max_failures.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            decisions = self._read_jsonl(run_dir / "controller_decisions.jsonl")
            summary_payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(summary.failure_count, 1)
            self.assertEqual(len(observations), 1)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(observations[0]["status"], "invalid")
            self.assertEqual(summary_payload["failure_count"], 1)

    def test_cli_nonzero_exit_records_failed_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-cli-failure"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["target"]["adapter"]["config"]["evaluate"]["command"] = [
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
            self.assertEqual(observations[0]["event_summary"]["errors"][0]["phase"], "target_evaluation")
            self.assertIn("exit code 3", observations[0]["event_summary"]["errors"][0]["message"])

    def test_invalid_target_output_records_failed_observation(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-invalid-target-output"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["target"]["adapter"]["config"]["evaluate"]["callable"] = "tests.fixtures.bad_targets:non_numeric_metric"

        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "invalid_target_output.yaml"
            spec_path.write_text(yaml.safe_dump(raw_spec, sort_keys=False), encoding="utf-8")
            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")

            self.assertIsNone(summary.best_metric)
            self.assertEqual(observations[0]["status"], "failed")
            self.assertEqual(observations[0]["event_summary"]["errors"][0]["phase"], "target_evaluation")
            self.assertIn("must be numeric", observations[0]["event_summary"]["errors"][0]["message"])

    def test_cli_timeout_records_timeout_observation_without_crashing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-cli-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["target"]["adapter"]["config"]["evaluate"]["timeoutSeconds"] = 1
        raw_spec["target"]["adapter"]["config"]["evaluate"]["command"] = [
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
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_cli_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-resource-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )
        raw_spec["execution"].setdefault("defaults", {})["resourceProfile"] = {"timeoutSeconds": 1}
        raw_spec["engines"][0].setdefault("resourceProfile", {})["timeoutSeconds"] = 1
        raw_spec["target"]["runtimeContract"] = {"timeoutSeconds": 30}
        raw_spec["target"]["adapter"]["config"]["evaluate"].pop("timeoutSeconds", None)
        raw_spec["target"]["adapter"]["config"]["evaluate"]["command"] = [
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

    def test_sa_example_evaluator_timeout_kills_simulator_process_group(self) -> None:
        from examples.opt_devs_gen_sims.sa_eval import evaluate

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            workspace = tmp_path / "workspace"
            simulator_root = workspace / "simulator"
            devs_project = simulator_root / "devs_project"
            devs_project.mkdir(parents=True)
            (devs_project / "__init__.py").write_text("", encoding="utf-8")

            marker = f"optpilot-sa-timeout-{time.time_ns()}"
            (simulator_root / "child_sleeper.py").write_text(
                "import time\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            (devs_project / "run_strategicairlift_d0.py").write_text(
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                f"subprocess.Popen([sys.executable, 'child_sleeper.py', '{marker}'])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            before = self._process_count_with_marker(marker)
            with self.assertRaises(subprocess.TimeoutExpired):
                evaluate(
                    {
                        "workspace": str(workspace),
                        "candidateRoot": str(simulator_root),
                    },
                    {
                        "duration": 600.0,
                        "num_aircraft": 2,
                        "pallet_interval": 20.0,
                        "pallet_expiration_time": 120.0,
                        "flight_time": 30.0,
                        "unload_time": 2.0,
                        "return_time": 30.0,
                        "maintenance_time": 10.0,
                        "timeoutSeconds": 1,
                    },
                    {
                        "workspace": str(workspace),
                        "instance_index": 0,
                        "trial_id": "trial-timeout",
                        "study_id": "study-timeout",
                    },
                )

            self.assertTrue((workspace / "sa_events.jsonl").exists())
            self.assertTrue((workspace / "sa_stderr.log").exists())

            for _ in range(20):
                if self._process_count_with_marker(marker) == before:
                    break
                time.sleep(0.1)
            self.assertEqual(self._process_count_with_marker(marker), before)

    def test_local_subprocess_backend_runs_successful_trial(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-subprocess-success"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["execution"]["backend"]["implementation"] = "builtin.local_subprocess_backend"
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )

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
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_random_search.yaml")
        raw_spec["metadata"]["name"] = "toy-subprocess-timeout"
        raw_spec["stopping"]["maxTrials"] = 1
        raw_spec["engines"][0]["config"]["batchSize"] = 1
        raw_spec["evaluationScope"] = {
            "mode": "FixedInstance",
            "definition": {
                "instance": {"target_x": 4.2, "target_y": 7, "sleep_seconds": 5.0},
            },
        }
        raw_spec["execution"]["backend"]["implementation"] = "builtin.local_subprocess_backend"
        raw_spec["execution"].setdefault("defaults", {})["resourceProfile"] = {"timeoutSeconds": 1}
        raw_spec["engines"][0].setdefault("resourceProfile", {})["timeoutSeconds"] = 1
        raw_spec["target"]["runtimeContract"] = {"timeoutSeconds": 30}

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
                "apiVersion": "optpilot/v3alpha",
                "kind": "StudySpec",
                "metadata": {"name": "retry-policy-check"},
                "target": {
                    "targetId": "flaky-target",
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
                            "candidate": {"type": "parameters"},
                            "metrics": {"source": "file", "path": "metrics.json"},
                        },
                    },
                    "runtimeContract": {"timeoutSeconds": 30},
                },
                "objective": {"primaryMetric": {"name": "score", "direction": "maximize"}},
                "evaluationScope": {"mode": "FixedInstance", "definition": {"instance": {}}},
                "artifacts": {
                    "primaryArtifact": {
                        "kind": "parameter_spec",
                        "validationRules": {
                            "implementation": "builtin.schema_validation",
                            "config": {"enforceBounds": False},
                        },
                        "materializationPlan": {"implementation": "builtin.parameter_to_config", "config": {}},
                    }
                },
                "controllers": [
                    {
                        "id": "controller",
                        "implementation": "builtin.single_engine_controller",
                        "config": {"engineId": "engine"},
                    }
                ],
                "engines": [
                    {
                        "id": "engine",
                        "implementation": "python:examples.user_engines.fixed_parameter_engine:FixedParameterEngine",
                        "config": {"batchSize": 1, "candidates": [{"x": 1}]},
                    }
                ],
                "execution": {
                    "backend": {"implementation": "builtin.local_backend", "config": {}},
                    "scheduler": {
                        "implementation": "builtin.local_scheduler",
                        "config": {"retryPolicy": {"maxAttempts": 2, "retryStatuses": ["failed"]}},
                    },
                    "parallelism": {"candidateParallelism": 1},
                },
                "evidence": {"store": {"implementation": "builtin.local_jsonl", "config": {}}},
                "reproducibility": {"seed": 0},
                "stopping": {"maxTrials": 1},
            }
            spec_path = tmp_path / "retry.yaml"
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

            summary = run_expanded_study_spec(str(spec_path), output_root=tmp_dir)
            observations = self._read_jsonl(Path(summary.run_dir) / "observations.jsonl")
            scheduler_events = self._read_jsonl(Path(summary.run_dir) / "scheduler_events.jsonl")

            self.assertEqual(summary.completed_trials, 1)
            self.assertEqual(summary.best_metric, 9.0)
            self.assertEqual([observation["status"] for observation in observations], ["failed", "success"])
            self.assertTrue(any(event["event"] == "trial_retried" for event in scheduler_events))
            collected_event = scheduler_events[-1]
            self.assertEqual(collected_event["handles"][0]["attempt_count"], 2)
            self.assertEqual(observations[-1]["provenance"]["backend_worker"]["backend"], "local_thread")
            self.assertIn("handle-", observations[-1]["provenance"]["backend_worker"]["handle"])

    def test_mixed_success_and_invalid_batch_continues_and_records_all_trials(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        raw_spec = compile_authoring_config(repo_root / "examples" / "studies" / "toy_user_engine.yaml")
        raw_spec["metadata"]["name"] = "toy-mixed-batch"
        raw_spec["stopping"]["maxTrials"] = 2
        raw_spec["engines"][0]["config"]["batchSize"] = 2
        raw_spec["engines"][0]["config"]["candidates"] = [
            {"x": 4.2, "y": 7, "mode": "balanced"},
            {"x": 99.0, "y": 7, "mode": "balanced"},
        ]
        raw_spec["evaluationScope"]["definition"]["instanceRef"] = str(
            repo_root / "examples" / "instances" / "toy_factory_case.yaml"
        )

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

    def test_user_owned_controller_reads_prior_evidence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = repo_root / "examples" / "studies" / "toy_evidence_aware_controller.yaml"

        with tempfile.TemporaryDirectory() as tmp_dir:
            summary = run_study(str(spec_path), output_root=tmp_dir)
            run_dir = Path(summary.run_dir)
            observations = self._read_jsonl(run_dir / "observations.jsonl")
            decisions = self._read_jsonl(run_dir / "controller_decisions.jsonl")

            self.assertEqual(summary.completed_trials, 2)
            self.assertEqual([observation["status"] for observation in observations], ["invalid", "success"])
            self.assertEqual(len(decisions), 2)
            first_context = decisions[0]["metadata"]["evidence_context"]
            second_context = decisions[1]["metadata"]["evidence_context"]
            self.assertEqual(first_context["summary"]["observation_count"], 0)
            self.assertEqual(second_context["summary"]["status_counts"]["invalid"], 1)
            self.assertEqual(second_context["recent_failure_count"], 1)
            self.assertEqual(decisions[1]["metadata"]["recent_failure_count"], 1)

    def test_local_evidence_store_read_api_and_summary_view(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_spec = load_study_spec(str(repo_root / "examples" / "studies" / "toy_random_search.yaml"))
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
                    "artifact_id": "artifact-a",
                    "status": "success",
                    "metric_values": {"throughput": 12.5},
                    "event_summary": {
                        "recordsToExtract": {
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
                    "artifact_id": "artifact-b",
                    "status": "failed",
                    "metric_values": {},
                    "event_summary": {"errors": [{"phase": "target_evaluation"}]},
                }
            )
            store.record_artifact({"artifact_id": "artifact-a"})
            store.record_controller_decision({"engine_id": "engine-a"})
            store.record_scheduler_event({"event": "batch_submitted"})
            store.record_engine_snapshot({"engine_id": "engine-a", "event": "proposed"})
            store.write_environment_snapshot({"python": {"version": "test"}, "packages": []})

            evidence_view = EvidenceView(store, study_spec)
            summary = evidence_view.summary()
            context = evidence_view.decision_context()
            failed_events = evidence_view.query_events("observation", status="failed")
            engine_events = evidence_view.query_events(["controller_decision", "engine_snapshot"], engine_id="engine-a")
            scheduler_events = evidence_view.query_events("scheduler_event", event="batch_submitted")
            record_streams = evidence_view.record_streams("machine_events")
            extracted_records = evidence_view.records("machine_events")

            self.assertEqual(len(store.read_observations()), 2)
            self.assertEqual(summary.observation_count, 2)
            self.assertEqual(summary.artifact_count, 1)
            self.assertEqual(summary.decision_count, 1)
            self.assertEqual(summary.scheduler_event_count, 1)
            self.assertEqual(summary.engine_snapshot_count, 1)
            self.assertEqual(summary.status_counts["success"], 1)
            self.assertEqual(summary.status_counts["failed"], 1)
            self.assertEqual(summary.best_metric, 12.5)
            self.assertEqual(context["recent_failure_count"], 1)
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["event_type"], "observation")
            self.assertEqual(failed_events[0]["record"]["trial_id"], "trial-b")
            self.assertEqual(len(engine_events), 2)
            self.assertEqual({event["event_type"] for event in engine_events}, {"controller_decision", "engine_snapshot"})
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
        state = UiState(cwd=repo_root, catalog_roots=[repo_root / "examples"], run_roots=[])

        catalog = _catalog_payload(state)
        validation = _validate_study(repo_root / "examples" / "studies" / "toy_random_search.yaml")

        self.assertTrue(any(item["id"] == "toy-factory" for item in catalog["environments"]))
        self.assertTrue(any(item["id"] == "reference-random-search" for item in catalog["methods"]))
        sa_environment = next(item for item in catalog["environments"] if item["id"] == "sa-simulator-code-edit")
        sa_method = next(item for item in catalog["methods"] if item["id"] == "openai-sa-file-editor")
        self.assertEqual(sa_environment["summary"]["artifact_kind"], "code_bundle")
        self.assertIn(
            "devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py",
            sa_environment["summary"]["editable_files"],
        )
        self.assertEqual(sa_method["summary"]["candidate_types"], ["files"])
        self.assertTrue(any(item["label"] == "toy-random-search" for item in catalog["studies"]))
        self.assertIn("builtin.reference_random_search", catalog["builtins"]["engine"])
        self.assertTrue(validation["valid"], validation)
        self.assertEqual(validation["target_id"], "toy-factory")

    def test_ui_run_listing_summarizes_existing_evidence_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        study_spec = load_study_spec(str(repo_root / "examples" / "studies" / "toy_random_search.yaml"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = LocalEvidenceStore(tmp_path, "ui-run")
            store.write_spec(study_spec.raw)
            store.record_observation(
                {
                    "trial_id": "trial-ok",
                    "artifact_id": "artifact-ok",
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
                    "best_artifact_id": "artifact-ok",
                    "failure_count": 0,
                }
            )
            state = UiState(cwd=repo_root, catalog_roots=[repo_root / "examples"], run_roots=[tmp_path])

            runs = _list_runs(state)

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["name"], "toy-random-search")
            self.assertEqual(runs[0]["completed_trials"], 1)
            self.assertEqual(runs[0]["best_metric"], 10.0)
            self.assertEqual(runs[0]["status"], "completed")

    def test_cli_parser_accepts_ui_command(self) -> None:
        args = build_parser().parse_args(["ui", "--port", "9001", "--catalog", "examples"])

        self.assertEqual(args.command, "ui")
        self.assertEqual(args.port, 9001)
        self.assertEqual(args.catalog, ["examples"])

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
