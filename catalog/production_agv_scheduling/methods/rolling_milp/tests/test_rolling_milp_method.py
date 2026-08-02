from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


METHOD_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = METHOD_ROOT.parents[3]
for import_root in (REPO_ROOT / "src", METHOD_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from method import RollingMILPMethod  # noqa: E402


EXPECTED_PATHS = {
    "scheduler.py",
    "param_estimator.py",
    "policy/__init__.py",
    "policy/command_dispatcher.py",
    "policy/compat.py",
    "policy/controller.py",
    "policy/event_monitor.py",
    "policy/model_solver.py",
    "policy/path_timing.py",
    "policy/rescheduling_engine.py",
    "policy/settings.py",
    "policy/state_extractor.py",
    "policy/types.py",
}


class RollingMILPMethodTests(unittest.TestCase):
    def _method(self, **settings):
        config = {
            "variants": ["monolithic", "two_stage"],
            "solverTimeLimitSeconds": 2.0,
            "minReplanIntervalMinutes": 8.0,
            "fallbackMode": "heuristic",
        }
        config.update(settings)
        definition = {"id": "rolling-milp-baselines", "config": config}
        study_spec = SimpleNamespace(
            candidate={
                "context": {
                    "files": {
                        "allow": ["scheduler.py", "param_estimator.py", "policy/**"]
                    }
                }
            }
        )
        return RollingMILPMethod(definition, study_spec, rng=None)

    def test_stages_both_variants_without_gurobi(self):
        method = self._method()
        with tempfile.TemporaryDirectory() as staging_dir:
            candidates = method.propose(
                2, {"runtime_context": {"candidate_staging_dir": staging_dir}}
            )

            self.assertEqual(
                [candidate["candidate_id"] for candidate in candidates],
                ["rolling-milp-monolithic", "rolling-milp-two-stage"],
            )
            self.assertEqual(method.propose(2, {}), [])

            for candidate in candidates:
                self.assertEqual(candidate["format"], "files")
                self.assertEqual(
                    {entry["path"] for entry in candidate["spec"]["files"]},
                    EXPECTED_PATHS,
                )
                bundle_root = Path(candidate["spec"]["bundleRef"])
                self.assertTrue((bundle_root / "scheduler.py").is_file())
                settings_text = (bundle_root / "policy" / "settings.py").read_text(
                    encoding="utf-8"
                )
                compile(settings_text, "policy/settings.py", "exec")
                self.assertIn("'solver_time_limit_sec': 2.0", settings_text)
                self.assertIn("'min_replan_interval_sec': 8.0", settings_text)
                self.assertIn("'fallback_mode': 'heuristic'", settings_text)
                if candidate["candidate_id"].endswith("monolithic"):
                    self.assertIn("'use_two_stage_decomposition': False", settings_text)
                    self.assertIn("'adaptive_task_cap': False", settings_text)
                else:
                    self.assertIn("'use_two_stage_decomposition': True", settings_text)
                    self.assertIn("'adaptive_task_cap': True", settings_text)

    def test_staged_candidate_is_import_safe_without_gurobi(self):
        method = self._method(variants=["monolithic"])
        with tempfile.TemporaryDirectory() as staging_dir:
            candidate = method.propose(
                1, {"runtime_context": {"candidate_staging_dir": staging_dir}}
            )[0]
            bundle_root = Path(candidate["spec"]["bundleRef"])

            for path in sorted(bundle_root.rglob("*.py")):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

            self._purge_candidate_modules()
            sys.path.insert(0, str(bundle_root))
            try:
                spec = importlib.util.spec_from_file_location(
                    "rolling_milp_candidate_scheduler", bundle_root / "scheduler.py"
                )
                module = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(module)
                self.assertTrue(callable(module.create_controller))

                from policy import model_solver, settings as candidate_settings

                self.assertEqual(candidate_settings.SETTINGS["fallback_mode"], "heuristic")
                self.assertEqual(
                    Path(candidate_settings.__file__).resolve(),
                    (bundle_root / "policy" / "settings.py").resolve(),
                )

                original_gp, original_grb = model_solver.gp, model_solver.GRB
                try:
                    model_solver.gp = None
                    model_solver.GRB = None
                    with self.assertRaisesRegex(
                        model_solver.GurobiUnavailableError, "requires gurobipy"
                    ):
                        model_solver.MIPTaskScheduler()

                    model_solver.gp = object()
                    model_solver.GRB = object()
                    strict = model_solver.MIPTaskScheduler(fallback_mode="error")
                    with self.assertRaisesRegex(model_solver.MILPSolveError, "rejected"):
                        strict._failure_or_fallback(
                            [], {}, 0.0, "Solver status rejected", {"solver_status": "TIME_LIMIT"}
                        )
                    diagnostic = model_solver.MIPTaskScheduler(fallback_mode="heuristic")
                    plan = diagnostic._failure_or_fallback(
                        [], {}, 0.0, "Solver status rejected", {"solver_status": "TIME_LIMIT"}
                    )
                    self.assertEqual(plan.status, "fallback_heuristic")
                    self.assertFalse(plan.diagnostics["milp_solved"])
                    self.assertTrue(plan.diagnostics["fallback_explicitly_enabled"])
                finally:
                    model_solver.gp, model_solver.GRB = original_gp, original_grb
            finally:
                sys.path.remove(str(bundle_root))
                self._purge_candidate_modules()

    def test_rejects_silent_or_unknown_fallback_modes(self):
        method = self._method(fallbackMode="silent")
        with tempfile.TemporaryDirectory() as staging_dir:
            with self.assertRaisesRegex(ValueError, "fallbackMode"):
                method.propose(
                    1, {"runtime_context": {"candidate_staging_dir": staging_dir}}
                )

    @staticmethod
    def _purge_candidate_modules():
        for name in list(sys.modules):
            if name == "policy" or name.startswith("policy."):
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
