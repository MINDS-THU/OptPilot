from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from .method import EvolutionaryRuleSearchMethod, _one_hot_labels, _repair_vector
except ImportError:  # Direct unittest discovery from this method directory.
    from method import EvolutionaryRuleSearchMethod, _one_hot_labels, _repair_vector


def _definition(algorithm: str, generations: int = 1):
    return {
        "id": f"{algorithm}-test",
        "config": {
            "algorithm": algorithm,
            "seed": 42,
            "smokeMode": True,
            "populationSize": 4,
            "generations": generations,
            "stabilityLambda": 0.35,
        },
    }


def _observations(candidates):
    return [
        {
            "candidate_id": candidate["candidate_id"],
            "status": "success",
            "metric_values": {
                "mean_total_score": float(index + 1),
                "std_total_score": 0.1,
            },
        }
        for index, candidate in enumerate(candidates)
    ]


class EvolutionaryRuleSearchMethodTests(unittest.TestCase):
    def test_full_initial_population_contains_all_64_one_hot_policies(self) -> None:
        definition = {
            "id": "ga-test",
            "config": {"algorithm": "ga", "populationSize": 64, "generations": 0},
        }
        method = EvolutionaryRuleSearchMethod(definition, object())
        labels = [_one_hot_labels(plan.vector) for plan in method._plans]
        self.assertEqual(len(labels), 64)
        self.assertEqual(len({tuple(sorted(label.items())) for label in labels if label}), 64)
        self.assertEqual(
            labels[0],
            {"line": "default", "task": "default", "agv": "default"},
        )

    def test_each_algorithm_advances_one_generation_deterministically(self) -> None:
        for algorithm in ("ga", "de", "pso"):
            with self.subTest(algorithm=algorithm):
                first_weights = self._run_one_generation(algorithm)
                second_weights = self._run_one_generation(algorithm)
                self.assertEqual(first_weights, second_weights)

    def _run_one_generation(self, algorithm: str):
        method = EvolutionaryRuleSearchMethod(_definition(algorithm), object())
        with tempfile.TemporaryDirectory() as staging_dir:
            state = {"runtime_context": {"candidate_staging_dir": staging_dir}}
            initial = method.propose(4, state)
            self.assertEqual(len(initial), 4)
            self.assertEqual(
                {entry["path"] for entry in initial[0]["spec"]["files"]},
                {
                    "scheduler.py",
                    "param_estimator.py",
                    "policy/__init__.py",
                    "policy/weighted_rule_scheduler.py",
                },
            )
            bundle_root = Path(initial[0]["spec"]["bundleRef"])
            sys.path.insert(0, str(bundle_root))
            try:
                spec = importlib.util.spec_from_file_location(
                    f"weighted_{algorithm}_candidate_test",
                    bundle_root / "scheduler.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                scheduler_instance = module.create_scheduler()
                self.assertEqual(len(scheduler_instance.line_weights), 4)
                self.assertEqual(len(scheduler_instance.task_weights), 8)
                self.assertEqual(len(scheduler_instance.agv_weights), 2)
            finally:
                sys.path.remove(str(bundle_root))
                sys.modules.pop("policy.weighted_rule_scheduler", None)
                sys.modules.pop("policy", None)
            method.observe(_observations(initial))
            evolved = method.propose(4, state)
            self.assertEqual(len(evolved), 4)
            self.assertTrue(all(item["generator"]["generation"] == 1 for item in evolved))
            for candidate in evolved:
                weights = candidate["generator"]["weights"]
                self.assertAlmostEqual(sum(weights["line"]), 1.0)
                self.assertAlmostEqual(sum(weights["task"]), 1.0)
                self.assertAlmostEqual(sum(weights["agv"]), 1.0)
            return [item["generator"]["weights"] for item in evolved]

    def test_repair_clips_negative_values_without_clipping_large_positive_values(self) -> None:
        repaired = _repair_vector(
            [2.0, 1.0, -3.0, 0.0]
            + [4.0, 2.0, 1.0, 1.0, 0.0, 0.0, 0.0, -1.0]
            + [10.0, 5.0]
        )
        self.assertAlmostEqual(repaired[0], 2.0 / 3.0)
        self.assertAlmostEqual(repaired[1], 1.0 / 3.0)
        self.assertEqual(repaired[2], 0.0)
        self.assertAlmostEqual(repaired[4], 0.5)
        self.assertAlmostEqual(repaired[12], 2.0 / 3.0)

    def test_candidate_policy_package_wins_over_an_installed_policy_package(self) -> None:
        method = EvolutionaryRuleSearchMethod(_definition("ga", generations=0), object())
        with tempfile.TemporaryDirectory() as staging_dir:
            candidate = method.propose(
                1, {"runtime_context": {"candidate_staging_dir": staging_dir}}
            )[0]
            bundle_root = Path(candidate["spec"]["bundleRef"])
            unrelated_root = Path(staging_dir) / "unrelated-site-packages"
            unrelated_policy = unrelated_root / "policy"
            unrelated_policy.mkdir(parents=True)
            (unrelated_policy / "__init__.py").write_text(
                "SOURCE = 'unrelated'\n", encoding="utf-8"
            )
            (unrelated_policy / "weighted_rule_scheduler.py").write_text(
                "raise RuntimeError('unrelated policy package was imported')\n",
                encoding="utf-8",
            )

            self._purge_policy_modules()
            sys.path.insert(0, str(unrelated_root))
            sys.path.insert(0, str(bundle_root))
            try:
                spec = importlib.util.spec_from_file_location(
                    "weighted_rule_collision_test", bundle_root / "scheduler.py"
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                self.assertEqual(
                    Path(sys.modules["policy"].__file__).resolve(),
                    (bundle_root / "policy" / "__init__.py").resolve(),
                )
            finally:
                sys.path.remove(str(bundle_root))
                sys.path.remove(str(unrelated_root))
                self._purge_policy_modules()

    def test_rejects_environment_that_disallows_emitted_policy_files(self) -> None:
        study_spec = SimpleNamespace(
            candidate={
                "context": {
                    "files": {"allow": ["scheduler.py", "param_estimator.py"]}
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "policy/__init__.py"):
            EvolutionaryRuleSearchMethod(_definition("ga"), study_spec)

    @staticmethod
    def _purge_policy_modules() -> None:
        for name in list(sys.modules):
            if name == "policy" or name.startswith("policy."):
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
