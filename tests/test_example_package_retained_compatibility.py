"""Retained-execution truth for the bundled example package."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

import yaml

from optpilot.package_validation import validate_package
from optpilot.spec import load_study_spec


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPOSITORY_ROOT / "catalog" / "example_package"


class ExamplePackageRetainedCompatibilityTest(unittest.TestCase):
    def test_example_package_contains_no_duplicate_devs_archive(self) -> None:
        self.assertFalse(
            (_PACKAGE_ROOT / "resources" / "devs-gen-interface.zip").exists(),
            "The editable DEVS resource is canonical; do not capture a duplicate "
            "Finder archive (and its hidden macOS metadata) into every Study.",
        )

    def test_retained_study_capability_matrix_is_explicit(self) -> None:
        validation = validate_package(
            _PACKAGE_ROOT,
            check_imports=True,
            check_source=True,
            check_setup_files=True,
        )

        self.assertTrue(validation["valid"], validation)
        studies = {
            Path(item["path"]).name: item
            for item in validation["capabilities"]["retained_execution"][
                "studies"
            ]
        }
        self.assertEqual(
            {
                name
                for name, capability in studies.items()
                if capability["eligible"]
            },
            {
                "job_shop_dispatch_rule_baseline.yaml",
                "job_shop_lib_dispatching_rule.yaml",
                "job_shop_openai_dispatch_rule.yaml",
                "job_shop_ortools_cpsat.yaml",
                "job_shop_rl_stable_baselines.yaml",
                "job_shop_rule_parameters_baseline.yaml",
                "job_shop_simulated_annealing.yaml",
                "job_shop_solver_code_baseline.yaml",
                "job_shop_tune_dispatch_weights.yaml",
            },
        )
        self.assertEqual(
            {
                name: capability["code"]
                for name, capability in studies.items()
                if not capability["eligible"]
            },
            {},
        )

    def test_job_shop_evaluator_accepts_retained_immutable_settings(self) -> None:
        environment_root = (
            _PACKAGE_ROOT / "environments" / "job_shop_scheduling"
        )
        sys.path.insert(0, str(environment_root))
        try:
            from evaluator import evaluate
        finally:
            sys.path.remove(str(environment_root))

        cases = tuple(
            MappingProxyType(
                {
                    "id": case_id,
                    "path": f"cases/{case_id}.yaml",
                }
            )
            for case_id in ("ft06_small", "la01_tiny", "ft06_standard")
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate(
                {
                    "remaining_work_weight": 1.0,
                    "processing_time_weight": -1.0,
                    "machine_ready_weight": -0.1,
                    "job_ready_weight": -0.1,
                },
                {
                    "workspace": temporary,
                    "settings": MappingProxyType({"cases": cases}),
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event_summary"]["case_count"], 3)
        self.assertEqual(len(result["output_files"]), 7)
        self.assertTrue(
            all(
                set(output)
                == {
                    "declaration_id",
                    "kind",
                    "media_type",
                    "metadata",
                    "name",
                    "path",
                }
                for output in result["output_files"]
            )
        )

    def test_tuning_method_emits_only_canonical_candidate_input_fields(self) -> None:
        from catalog.example_package.methods.tune_dispatch_weights.method import (
            TuneDispatchWeightsMethod,
        )

        study = load_study_spec(
            _PACKAGE_ROOT / "studies" / "job_shop_tune_dispatch_weights.yaml"
        )
        method = TuneDispatchWeightsMethod(study.method, study, rng=None)

        candidates = method.propose(3, {})

        self.assertEqual(len(candidates), 3)
        self.assertTrue(
            all(
                set(candidate)
                == {"candidate_id", "format", "generator", "spec"}
                for candidate in candidates
            )
        )

    def test_retained_parameter_methods_replay_deterministic_candidates(self) -> None:
        from catalog.example_package.methods.fixed_rule_parameters.method import (
            FixedRuleParametersMethod,
        )
        from catalog.example_package.methods.tune_dispatch_weights.method import (
            TuneDispatchWeightsMethod,
        )

        fixtures = (
            (
                FixedRuleParametersMethod,
                "job_shop_rule_parameters_baseline.yaml",
                (1,),
            ),
            (
                TuneDispatchWeightsMethod,
                "job_shop_tune_dispatch_weights.yaml",
                (3, 3, 3, 3),
            ),
        )
        for method_type, study_name, batches in fixtures:
            with self.subTest(study=study_name):
                study = load_study_spec(_PACKAGE_ROOT / "studies" / study_name)
                first = method_type(study.method, study, rng=None)
                replay = method_type(study.method, study, rng=None)
                first_responses = [first.propose(size, {}) for size in batches]
                replay_responses = [replay.propose(size, {}) for size in batches]
                self.assertEqual(first_responses, replay_responses)

    def test_examples_do_not_use_ambient_uuid_candidate_ids(self) -> None:
        method_sources = sorted((_PACKAGE_ROOT / "methods").glob("*/method.py"))
        self.assertTrue(method_sources)
        for source in method_sources:
            with self.subTest(method=source.parent.name):
                self.assertNotIn("uuid.uuid4", source.read_text(encoding="utf-8"))

    def test_file_methods_emit_canonical_candidates_without_credentials(self) -> None:
        from catalog.example_package.methods.baseline_file_copy.method import (
            BaselineFileCopyMethod,
        )
        from catalog.example_package.methods.openai_file_editor.method import (
            OpenAIFileEditMethod,
        )

        fixtures = (
            (
                BaselineFileCopyMethod,
                "job_shop_dispatch_rule_baseline.yaml",
                "baseline-file-copy-baseline",
            ),
            (
                OpenAIFileEditMethod,
                "job_shop_openai_dispatch_rule.yaml",
                "openai-file-editor-baseline",
            ),
        )
        for method_type, study_name, expected_id in fixtures:
            with self.subTest(study=study_name):
                study = load_study_spec(_PACKAGE_ROOT / "studies" / study_name)
                method = method_type(study.method, study, rng=None)
                with tempfile.TemporaryDirectory() as candidate_store:
                    candidates = method.propose(
                        1,
                        {
                            "runtime_context": {
                                "candidate_staging_dir": candidate_store,
                            }
                        },
                    )
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["candidate_id"], expected_id)
                self.assertEqual(
                    set(candidates[0]),
                    {"candidate_id", "format", "generator", "lineage", "spec"},
                )

    def test_file_candidate_templates_are_method_context_not_trial_seeds(self) -> None:
        fixtures = (
            ("environment_dispatch_rule.yaml", "dispatch_rule.py"),
            ("environment_solver_code.yaml", "solver.py"),
        )
        environment_root = (
            _PACKAGE_ROOT / "environments" / "job_shop_scheduling"
        )
        for environment_name, candidate_path in fixtures:
            with self.subTest(environment=environment_name):
                payload = yaml.safe_load(
                    (environment_root / environment_name).read_text(encoding="utf-8")
                )
                self.assertNotIn("trialWorkspace", payload)
                templates = {
                    reference["name"]: reference
                    for reference in payload["methodContext"]["references"]
                    if reference.get("type") == "candidate_template"
                }
                self.assertEqual(set(templates), {candidate_path})
                self.assertTrue(
                    (environment_root / templates[candidate_path]["path"]).is_file()
                )

    def test_job_shop_environments_use_evaluator_artifact_declarations(self) -> None:
        environment_root = (
            _PACKAGE_ROOT / "environments" / "job_shop_scheduling"
        )
        for path in sorted(environment_root.glob("environment_*.yaml")):
            with self.subTest(environment=path.name):
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertNotIn("outputFiles", payload)


if __name__ == "__main__":
    unittest.main()
