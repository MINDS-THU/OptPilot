"""Assistant smoke tests are bounded, copied, and still approval-gated.

The temporary copy, trial limit, wall-clock cap, and disposable Realm protect
OptPilot's own records. They do not confine filesystem or network side effects
from a process-runtime evaluator or Method, so authored code must never execute
without the person's explicit approval.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.core.test_realm_local_study_package import _write_package
from optpilot_studio.ui.server import (
    ASSISTANT_PERMISSION_VALUES,
    ASSISTANT_SMOKE_DEFAULT_TRIALS,
    ASSISTANT_SMOKE_MAX_TRIALS,
    DEFAULT_ASSISTANT_PERMISSIONS,
    UiState,
    _attach_agent_workspace,
    _assistant_smoke_trial_limit,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _normalize_assistant_permissions,
    _prepare_assistant_smoke_package,
    _read_agent_approvals,
    _study_env_requirements,
    _study_subprocess_env,
)
import yaml


class SmokePermissionTest(unittest.TestCase):
    def test_a_smoke_test_requires_approval_by_default(self) -> None:
        self.assertEqual(
            DEFAULT_ASSISTANT_PERMISSIONS["smoke_test"], "approval_required"
        )

    def test_the_person_can_require_approval_or_forbid_it(self) -> None:
        self.assertEqual(
            ASSISTANT_PERMISSION_VALUES["smoke_test"],
            {"approval_required", "disabled"},
        )

    def test_a_legacy_direct_run_setting_is_tightened_on_read(self) -> None:
        normalized = _normalize_assistant_permissions(
            {
                "smoke_test": "safe_without_approval",
                "shell_run": "safe_without_approval",
            }
        )
        self.assertEqual(normalized["smoke_test"], "approval_required")
        self.assertEqual(normalized["shell_run"], "approval_required")

    def test_smoke_and_study_launch_permissions_remain_independent(self) -> None:
        normalized = _normalize_assistant_permissions({"smoke_test": "disabled"})
        self.assertEqual(normalized["smoke_test"], "disabled")
        self.assertEqual(
            normalized["study_launch"],
            DEFAULT_ASSISTANT_PERMISSIONS["study_launch"],
        )


class SmokeBoundsTest(unittest.TestCase):
    def test_an_unstated_trial_count_still_gets_a_limit(self) -> None:
        for unstated in (None, 0, "", "not a number", -4):
            with self.subTest(unstated=unstated):
                self.assertEqual(
                    _assistant_smoke_trial_limit(unstated),
                    ASSISTANT_SMOKE_DEFAULT_TRIALS,
                )

    def test_a_large_request_is_capped(self) -> None:
        self.assertEqual(
            _assistant_smoke_trial_limit(10_000), ASSISTANT_SMOKE_MAX_TRIALS
        )

    def test_a_modest_request_is_honoured(self) -> None:
        self.assertEqual(_assistant_smoke_trial_limit(5), 5)

    def test_the_smoke_runs_a_copy_and_leaves_the_package_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            package = tmp / "package"
            (package / "studies").mkdir(parents=True)
            study = package / "studies" / "demo.yaml"
            original = {"kind": "study", "budget": {"maxTrials": 500}}
            study.write_text(yaml.safe_dump(original), encoding="utf-8")

            workspace = tmp / "work"
            workspace.mkdir()
            copied_package, copied_study = _prepare_assistant_smoke_package(
                package_root=package,
                study_path=study,
                temporary_root=workspace,
                max_trials=_assistant_smoke_trial_limit(None),
            )

            self.assertNotEqual(copied_package.resolve(), package.resolve())
            self.assertEqual(
                yaml.safe_load(study.read_text(encoding="utf-8")),
                original,
                "the person's own study must be untouched",
            )
            self.assertEqual(
                yaml.safe_load(copied_study.read_text(encoding="utf-8"))["budget"][
                    "maxTrials"
                ],
                ASSISTANT_SMOKE_DEFAULT_TRIALS,
            )

    def test_smoke_env_ignores_interface_only_host_grants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            package = root / "package"
            package.mkdir()
            study = _write_package(package)

            method_path = package / "configs" / "methods" / "method.yaml"
            method = yaml.safe_load(method_path.read_text(encoding="utf-8"))
            method["runtime"] = {
                "sandbox": "process",
                "envFromHost": ["SMOKE_METHOD_TOKEN"],
            }
            method_path.write_text(
                yaml.safe_dump(method, sort_keys=False), encoding="utf-8"
            )

            environment_path = (
                package / "configs" / "environments" / "environment.yaml"
            )
            environment = yaml.safe_load(
                environment_path.read_text(encoding="utf-8")
            )
            environment["interface"] = {
                "command": ["python", "viewer.py"],
                "presentation": {"kind": "web", "port": 8080},
                "grants": {
                    "envFromHost": [
                        {
                            "name": "SMOKE_INTERFACE_MODEL",
                            "default": "provider/default-model",
                        },
                        "SMOKE_INTERFACE_REQUIRED",
                    ]
                },
            }
            environment_path.write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )

            state = UiState(cwd=root, catalog_roots=[package], run_roots=[])
            self.addCleanup(state.close_coordination)
            with mock.patch.dict(
                "os.environ", {"SMOKE_METHOD_TOKEN": "method-value"}, clear=True
            ):
                subprocess_env = _study_subprocess_env(state, study)

        self.assertEqual(subprocess_env["SMOKE_METHOD_TOKEN"], "method-value")
        self.assertNotIn("SMOKE_INTERFACE_MODEL", subprocess_env)
        self.assertNotIn("SMOKE_INTERFACE_REQUIRED", subprocess_env)

    def test_required_study_inputs_are_collected_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            package = root / "package"
            package.mkdir()
            study = _write_package(package)
            authored = yaml.safe_load(study.read_text(encoding="utf-8"))
            authored["inputs"] = {
                "problem": {
                    "valueType": "string",
                    "description": "Problem to solve.",
                }
            }
            study.write_text(
                yaml.safe_dump(authored, sort_keys=False), encoding="utf-8"
            )

            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_coordination)
            workspace = _create_ui_workspace(
                state, {"title": "Required inputs", "root": str(package)}
            )
            session = _create_agent_session(state, {"title": "Smoke inputs"})
            _attach_agent_workspace(
                state, session["id"], workspace["id"], select=True
            )
            arguments = {
                "workspace_id": workspace["id"],
                "study_path": str(study),
            }

            missing = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_smoke_test_study",
                arguments,
            )
            self.assertFalse(missing["ok"], missing)
            self.assertEqual(
                missing["data"]["capability"]["code"], "study_inputs_required"
            )
            self.assertEqual(
                missing["remedy"]["tool"], "optpilot_smoke_test_study"
            )
            self.assertEqual(_read_agent_approvals(state, session["id"]), [])

            requested = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_smoke_test_study",
                {**arguments, "inputs": {"problem": "Minimize total delay."}},
            )
            approvals = _read_agent_approvals(state, session["id"])

        self.assertTrue(requested["data"]["approval_required"], requested)
        self.assertEqual(len(approvals), 1)
        self.assertIn("problem=Minimize total delay.", approvals[0]["summary"])
        self.assertEqual(
            approvals[0]["arguments"]["inputs"],
            {"problem": "Minimize total delay."},
        )

    def test_shipped_required_input_study_env_preflight_does_not_bind_inputs(
        self,
    ) -> None:
        study = (
            Path(__file__).resolve().parents[2]
            / "catalog"
            / "or_solving"
            / "studies"
            / "solve_or_problem.yaml"
        )
        self.assertEqual(_study_env_requirements(study), ["OPENROUTER_API_KEY"])


if __name__ == "__main__":
    unittest.main()
