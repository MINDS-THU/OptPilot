from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path, PurePosixPath

import yaml

from optpilot_studio.ui.server import (
    UiState,
    _apply_package_plan,
    _create_ui_workspace,
    _package_plan_readiness,
    _package_plan_warnings,
    _prepare_package_plan,
    _update_package_plan,
    _validate_package_plan,
    _studio_actor_id,
)


class StudioPackageCapabilityTruthTest(unittest.TestCase):
    def test_locked_setup_files_are_automatic_canonical_package_includes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_catalog_projections)
            self.addCleanup(state.close_coordination)
            workspace = _create_ui_workspace(
                state,
                {
                    "title": "Generated locked component",
                    "root": str(root / "workspace"),
                    "initialize_if_empty": False,
                },
            )
            workspace_root = Path(workspace["root"])
            component_root = workspace_root / "environments" / "generated"
            component_root.mkdir(parents=True)
            (component_root / "evaluate.py").write_text(
                "def evaluate(candidate, context): return {'score': 1.0}\n",
                encoding="utf-8",
            )
            local_wheel = component_root / "vendor" / "local-1-py3-none-any.whl"
            shared_wheel = workspace_root / "shared" / "shared-1-py3-none-any.whl"
            local_wheel.parent.mkdir()
            shared_wheel.parent.mkdir()
            local_wheel.write_bytes(b"local wheel")
            shared_wheel.write_bytes(b"shared wheel")
            outside_wheel = root / "outside-1-py3-none-any.whl"
            outside_wheel.write_bytes(b"outside wheel")
            (component_root / "requirements.lock").write_text(
                "vendor/local-1-py3-none-any.whl --hash=sha256:"
                + hashlib.sha256(local_wheel.read_bytes()).hexdigest()
                + "\n../../shared/shared-1-py3-none-any.whl --hash=sha256:"
                + hashlib.sha256(shared_wheel.read_bytes()).hexdigest()
                + "\n../../../outside-1-py3-none-any.whl --hash=sha256:"
                + hashlib.sha256(outside_wheel.read_bytes()).hexdigest()
                + "\n",
                encoding="utf-8",
            )
            (component_root / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "generated-locked",
                        "evaluator": {
                            "python": "evaluate:evaluate",
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
                        "metrics": {"source": "return", "keys": ["score"]},
                        "runtime": {
                            "sandbox": "process",
                            "setup": {
                                "cache": "prepared",
                                "steps": [
                                    {
                                        "uses": "python-venv",
                                        "cwd": ".",
                                        "requirements": ["requirements.lock"],
                                    }
                                ],
                            },
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            plan = _prepare_package_plan(
                state,
                workspace["id"],
                {"package_id": "generated-locked-package"},
            )["package_plan"]

        self.assertEqual(len(plan["components"]), 1)
        includes = set(plan["components"][0]["include"])
        self.assertTrue(
            {
                "environments/generated/requirements.lock",
                "environments/generated/vendor/local-1-py3-none-any.whl",
                "shared/shared-1-py3-none-any.whl",
            }.issubset(includes),
            includes,
        )
        self.assertTrue(
            all(".." not in PurePosixPath(value).parts for value in includes),
            includes,
        )
        self.assertFalse(
            any("outside-1-py3-none-any.whl" in value for value in includes),
            includes,
        )

    def test_checked_resource_registration_has_durable_replayable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            self.addCleanup(state.close_catalog_projections)
            self.addCleanup(state.close_coordination)
            workspace = _create_ui_workspace(state, {"title": "Reusable notes"})
            prepared = _prepare_package_plan(
                state,
                workspace["id"],
                {
                    "kind": "resource",
                    "resource_id": "reusable-notes",
                    "description": "Reusable notes",
                },
            )["package_plan"]
            checked = _validate_package_plan(
                state, workspace["id"], prepared["id"]
            )

            first = _apply_package_plan(state, workspace["id"], prepared["id"])
            replay = _apply_package_plan(state, workspace["id"], prepared["id"])
            actions = state.coordination.list_actions(
                actor_id=_studio_actor_id(state), action_kind="catalog-publication"
            )

        self.assertTrue(checked["setup"]["check"]["accepted"])
        self.assertEqual(first["setup"]["state"], "registered")
        self.assertEqual(replay["catalog"]["head"], first["catalog"]["head"])
        self.assertEqual(replay["setup"]["registered"], first["setup"]["registered"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].state.value, "succeeded")

    def test_unchanged_setup_sync_preserves_check_and_test_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(
                cwd=root,
                catalog_roots=[root / "catalog" / "local_package"],
                run_roots=[],
            )
            self.addCleanup(state.close_coordination)
            workspace = _create_ui_workspace(state, {"title": "Reference files"})
            prepared = _prepare_package_plan(
                state,
                workspace["id"],
                {
                    "kind": "resource",
                    "resource_id": "reference-files",
                    "description": "Reference files",
                },
            )["package_plan"]
            checked = _validate_package_plan(
                state, workspace["id"], prepared["id"]
            )["package_plan"]
            target = checked["resources"][0]

            replayed = _update_package_plan(
                state,
                workspace["id"],
                checked["id"],
                {
                    "resources": [
                        {
                            "target_id": target["target_id"],
                            "include": target["include"],
                            "exclude": target["exclude"],
                            "source_hints": target.get("source_hints", []),
                            "path_rewrites": target.get("path_rewrites", []),
                        }
                    ],
                    "studies": [],
                    "components": [],
                },
            )["package_plan"]

        self.assertEqual(replayed["artifact"], checked["artifact"])
        self.assertEqual(replayed["validation"], checked["validation"])
        self.assertEqual(replayed["status"], "validated")

    def test_opening_workspace_setup_reuses_one_stable_registration_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(
                cwd=root,
                catalog_roots=[root / "catalog" / "local_package"],
                run_roots=[],
            )
            self.addCleanup(state.close_coordination)
            workspace = _create_ui_workspace(state, {"title": "Generated project"})

            first = _prepare_package_plan(state, workspace["id"], {})[
                "package_plan"
            ]
            reopened = _prepare_package_plan(state, workspace["id"], {})[
                "package_plan"
            ]
            refreshed = _prepare_package_plan(
                state, workspace["id"], {"refresh": True}
            )["package_plan"]

        self.assertEqual(reopened, first)
        self.assertEqual(refreshed["id"], first["id"])
        self.assertEqual(refreshed["publisher_id"], first["publisher_id"])

    def test_static_validation_defers_callable_shape_to_explicit_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(
                cwd=root,
                catalog_roots=[root / "catalog" / "local_package"],
                run_roots=[],
            )
            workspace = _create_ui_workspace(state, {"title": "Legacy batch"})
            source = Path(workspace["root"]) / "optpilot_configs"
            environment_dir = source / "environments" / "toy"
            method_dir = source / "methods" / "legacy"
            environment_dir.mkdir(parents=True)
            method_dir.mkdir(parents=True)
            (environment_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context):\n"
                "    return {'status': 'success', 'metric_values': {'score': 1.0}, 'constraint_results': {}, 'output_files': [], 'event_summary': {}}\n",
                encoding="utf-8",
            )
            (environment_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy-env",
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
                        "metrics": {"source": "return", "keys": ["score"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (method_dir / "method.py").write_text(
                "class LegacyMethod:\n"
                "    def __init__(self, definition, study_spec, rng):\n"
                "        pass\n"
                "    def start(self, request):\n"
                "        return 'handle'\n"
                "    def poll(self, handle):\n"
                "        return {'done': True}\n"
                "    def finalize(self, handle):\n"
                "        return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "legacy-method",
                        "entrypoint": {
                            "python": "method:LegacyMethod",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            prepared = _prepare_package_plan(state, workspace["id"], {})[
                "package_plan"
            ]
            validated = _validate_package_plan(
                state, workspace["id"], prepared["id"]
            )["package_plan"]

        self.assertTrue(validated["validation"]["valid"], validated)
        self.assertEqual(validated["status"], "validated")
        self.assertEqual(validated["readiness"], "component-ready")
        capability = validated["validation"]["capabilities"][
            "retained_execution"
        ]
        self.assertFalse(capability["eligible"])
        self.assertTrue(capability["smoke_eligible"])
        self.assertEqual(capability["code"], "study_required")
        method = capability["methods"][0]
        self.assertEqual(method["code"], "method_callable_unchecked")
        self.assertIn("no study", " ".join(validated["warnings"]).lower())

    def test_paired_package_with_unsupported_method_is_not_component_or_run_ready(self) -> None:
        reason = (
            "The retained process-study runner does not support method protocol "
            "'session'; use a Python batch method for this execution slice."
        )
        validation = {
            "valid": True,
            "capabilities": {
                "retained_execution": {
                    "supported": False,
                    "eligible": False,
                    "code": "method_mode_unsupported",
                    "reason": reason,
                    "methods": [],
                }
            },
        }
        plan = {
            "classification": "environment-plus-method",
            "studies": [{"id": "smoke"}],
            "smoke": {},
        }

        self.assertEqual(
            _package_plan_readiness(plan, validation), "execution-unsupported"
        )
        warnings = _package_plan_warnings(plan, validation)
        self.assertEqual(warnings, [reason])
        self.assertNotIn("component-ready", " ".join(warnings))

    def test_paired_package_with_eligible_method_still_requires_smoke(self) -> None:
        validation = {
            "valid": True,
            "capabilities": {
                "retained_execution": {
                    "supported": True,
                    "eligible": True,
                    "code": "ready",
                    "reason": None,
                    "methods": [],
                }
            },
        }
        plan = {
            "classification": "environment-plus-method",
            "studies": [{"id": "smoke"}],
            "smoke": {},
        }

        self.assertEqual(_package_plan_readiness(plan, validation), "component-ready")
        self.assertIn(
            "run Test", " ".join(_package_plan_warnings(plan, validation))
        )

    def test_host_provisioned_dependency_is_warned_before_registration(self) -> None:
        """A package that only runs here must not register as quietly ready."""

        reason = (
            "Some components import packages that only the host interpreter "
            "provides: job_shop_lib."
        )
        validation = {
            "valid": True,
            "capabilities": {
                "retained_execution": {
                    "supported": True,
                    "eligible": True,
                    "code": "ready",
                    "reason": None,
                    "methods": [],
                },
                "dependency_closure": {
                    "declared": False,
                    "code": "dependency_host_provisioned",
                    "reason": reason,
                    "undeclared_modules": ["job_shop_lib"],
                    "components": [],
                },
            },
        }
        plan = {
            "classification": "environment-plus-method",
            "studies": [{"id": "smoke"}],
            "smoke": {"valid": True},
        }

        self.assertEqual(_package_plan_warnings(plan, validation), [reason])

    def test_declared_dependency_closure_adds_no_warning(self) -> None:
        validation = {
            "valid": True,
            "capabilities": {
                "retained_execution": {
                    "supported": True,
                    "eligible": True,
                    "code": "ready",
                    "reason": None,
                    "methods": [],
                },
                "dependency_closure": {
                    "declared": True,
                    "code": "dependency_closure_declared",
                    "reason": None,
                    "undeclared_modules": [],
                    "components": [],
                },
            },
        }
        plan = {
            "classification": "environment-plus-method",
            "studies": [{"id": "smoke"}],
            "smoke": {"valid": True},
        }

        self.assertEqual(_package_plan_warnings(plan, validation), [])


if __name__ == "__main__":
    unittest.main()
