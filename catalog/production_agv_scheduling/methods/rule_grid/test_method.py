from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from .method import AGV_RULES, LINE_RULES, TASK_RULES, RuleGridMethod
except ImportError:  # Direct unittest discovery from this method directory.
    from method import AGV_RULES, LINE_RULES, TASK_RULES, RuleGridMethod


class RuleGridMethodTests(unittest.TestCase):
    def test_full_grid_is_deterministic_and_stages_executable_bundles(self) -> None:
        method = RuleGridMethod({"id": "exhaustive-rule-grid", "config": {}}, object())
        with tempfile.TemporaryDirectory() as staging_dir:
            candidates = method.propose(
                200,
                {"runtime_context": {"candidate_staging_dir": staging_dir}},
            )
            self.assertEqual(len(candidates), 5 * 9 * 3)
            self.assertEqual(len({item["candidate_id"] for item in candidates}), 135)
            self.assertEqual(
                candidates[0]["generator"]["rule_triple"],
                {"line": "default", "task": "default", "agv": "default"},
            )
            self.assertTrue(candidates[0]["generator"]["is_initial_policy"])
            self.assertEqual(
                candidates[-1]["generator"]["rule_triple"],
                {"line": "random", "task": "random", "agv": "random"},
            )
            self.assertEqual(
                {entry["path"] for entry in candidates[0]["spec"]["files"]},
                {
                    "scheduler.py",
                    "param_estimator.py",
                    "policy/__init__.py",
                    "policy/rule_scheduler.py",
                },
            )
            scheduler = next(
                entry
                for entry in candidates[0]["spec"]["files"]
                if entry["path"] == "scheduler.py"
            )
            source = Path(scheduler["contentRef"]).read_text(encoding="utf-8")
            compile(source, "scheduler.py", "exec")
            bundle_root = Path(candidates[0]["spec"]["bundleRef"])
            sys.path.insert(0, str(bundle_root))
            try:
                spec = importlib.util.spec_from_file_location(
                    "rule_grid_candidate_test", bundle_root / "scheduler.py"
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                scheduler_instance = module.create_scheduler()
                self.assertEqual(scheduler_instance.line_selection_method, "default")
            finally:
                sys.path.remove(str(bundle_root))
                sys.modules.pop("policy.rule_scheduler", None)
                sys.modules.pop("policy", None)
            self.assertEqual(
                method.propose(
                    1,
                    {"runtime_context": {"candidate_staging_dir": staging_dir}},
                ),
                [],
            )

    def test_supported_rule_counts_match_paper(self) -> None:
        self.assertEqual((len(LINE_RULES), len(TASK_RULES), len(AGV_RULES)), (5, 9, 3))

    def test_candidate_policy_package_wins_over_an_installed_policy_package(self) -> None:
        method = RuleGridMethod({"id": "rule-grid-test", "config": {}}, object())
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
            (unrelated_policy / "rule_scheduler.py").write_text(
                "raise RuntimeError('unrelated policy package was imported')\n",
                encoding="utf-8",
            )

            self._purge_policy_modules()
            sys.path.insert(0, str(unrelated_root))
            sys.path.insert(0, str(bundle_root))
            try:
                spec = importlib.util.spec_from_file_location(
                    "rule_grid_collision_test", bundle_root / "scheduler.py"
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
            RuleGridMethod({"id": "rule-grid-test", "config": {}}, study_spec)

    @staticmethod
    def _purge_policy_modules() -> None:
        for name in list(sys.modules):
            if name == "policy" or name.startswith("policy."):
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
