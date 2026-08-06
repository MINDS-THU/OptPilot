from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.package_validation import validate_package


class PackageValidationCapabilityTest(unittest.TestCase):
    def test_import_validation_does_not_write_bytecode_into_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            method_dir = package / "methods" / "read-only-check"
            method_dir.mkdir(parents=True)
            (method_dir / "method.py").write_text(
                "class Method:\n"
                "    def __init__(self, definition, study_spec, rng): pass\n"
                "    def propose(self, n_candidates, study_state): return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "read-only-check",
                        "entrypoint": {
                            "python": "method:Method",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(package, check_imports=True)

            self.assertTrue(result["valid"], result)
            self.assertFalse(any(package.rglob("*.pyc")))
            self.assertFalse(any(package.rglob("__pycache__")))

    def test_locked_method_dependencies_are_verified_by_retained_test_not_host_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            method_dir = package / "methods" / "prepared"
            method_dir.mkdir(parents=True)
            (method_dir / "requirements.lock").write_text(
                "vendor/example-1.0.0-py3-none-any.whl "
                "--hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            (method_dir / "method.py").write_text(
                "import dependency_only_available_in_the_prepared_layer\n"
                "class Method:\n"
                "    def __init__(self, definition, study_spec, rng): pass\n"
                "    def propose(self, n_candidates, study_state): return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "prepared",
                        "entrypoint": {
                            "python": "method:Method",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                        "runtime": {
                            "setup": {
                                "cache": "prepared",
                                "steps": [
                                    {
                                        "uses": "python-venv",
                                        "cwd": ".",
                                        "requirements": ["requirements.lock"],
                                    }
                                ],
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(
                package,
                check_imports=True,
                check_source=True,
                check_setup_files=True,
            )

        self.assertTrue(result["valid"], result)
        self.assertFalse(result["entries"][0]["errors"])
        capability = result["entries"][0]["capabilities"][
            "retained_execution"
        ]
        self.assertFalse(capability["eligible"])
        self.assertTrue(capability["smoke_eligible"])
        self.assertEqual(capability["code"], "runtime_verification_required")
        self.assertIn("Static checks accepted", capability["reason"])
        self.assertIn("real execution runtime", capability["reason"])

    def test_unsupported_dependency_recipe_is_not_presented_as_testable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            method_dir = package / "methods" / "unlocked"
            method_dir.mkdir(parents=True)
            (method_dir / "method.py").write_text(
                "class Method:\n"
                "    def __init__(self, definition, study_spec, rng): pass\n"
                "    def propose(self, n_candidates, study_state): return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "unlocked",
                        "entrypoint": {
                            "python": "method:Method",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                        "runtime": {
                            "setup": {
                                "cache": "prepared",
                                "steps": [{"uses": "uv", "cwd": "."}],
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(package, check_imports=False)

        self.assertTrue(result["valid"], result)
        capability = result["entries"][0]["capabilities"][
            "retained_execution"
        ]
        self.assertFalse(capability["supported"])
        self.assertFalse(capability["eligible"])
        self.assertFalse(capability["smoke_eligible"])
        self.assertEqual(capability["code"], "dependency_setup_unsupported")
        self.assertIn("vendored hash-locked pure-Python wheels", capability["reason"])

    def test_static_validation_never_imports_workspace_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            method_dir = package / "methods" / "side-effect"
            method_dir.mkdir(parents=True)
            marker = Path(tmp_dir) / "imported.txt"
            (method_dir / "method.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
                "class Method:\n"
                "    def __init__(self, definition, study_spec, rng): pass\n"
                "    def propose(self, n_candidates, study_state): return []\n",
                encoding="utf-8",
            )
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "side-effect",
                        "entrypoint": {
                            "python": "method:Method",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(
                package,
                check_imports=False,
                check_source=True,
            )
            marker_exists = marker.exists()

        self.assertTrue(result["valid"], result)
        self.assertFalse(marker_exists)
        capability = result["capabilities"]["retained_execution"]
        self.assertEqual(capability["code"], "method_callable_unchecked")
        self.assertTrue(capability["smoke_eligible"])

    def test_exact_study_preflight_is_not_masked_by_an_unreferenced_method(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            environment_dir = package / "environments" / "toy"
            batch_dir = package / "methods" / "batch"
            session_dir = package / "methods" / "session"
            studies_dir = package / "studies"
            for path in (environment_dir, batch_dir, session_dir, studies_dir):
                path.mkdir(parents=True)
            (environment_dir / "evaluator.py").write_text(
                "def evaluate(candidate_runtime, context): return {'score': 1.0}\n",
                encoding="utf-8",
            )
            (environment_dir / "environment.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "environment",
                        "id": "toy",
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
            for method_dir, method_id, protocol in (
                (batch_dir, "batch", "batch"),
                (session_dir, "session", "session"),
            ):
                (method_dir / "method.py").write_text(
                    "class Method:\n"
                    "    def __init__(self, definition, study_spec, rng): pass\n"
                    "    def propose(self, n_candidates, study_state): return []\n",
                    encoding="utf-8",
                )
                (method_dir / "method.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "optpilot.io/v1",
                            "config": "method",
                            "id": method_id,
                            "entrypoint": {
                                "python": "method:Method",
                                "pythonPath": ["."],
                                "protocol": protocol,
                            },
                            "accepts": {"formats": ["parameters"]},
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
            (studies_dir / "session-study.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "session-study",
                        "environmentConfig": "../environments/toy/environment.yaml",
                        "methodConfig": "../methods/session/method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(
                package,
                check_imports=False,
                check_source=True,
            )

        self.assertTrue(result["valid"], result)
        capability = result["capabilities"]["retained_execution"]
        self.assertFalse(capability["eligible"])
        self.assertFalse(capability["smoke_eligible"])
        self.assertEqual(capability["code"], "method_mode_unsupported")
        self.assertEqual(capability["studies"][0]["method_id"], "session")
        self.assertEqual(len(capability["methods"]), 2)

    def test_retained_execution_reports_supported_and_unsupported_method_shapes(self) -> None:
        cases = (
            (
                "python-batch",
                {"python": "method:Method", "pythonPath": ["."], "protocol": "batch"},
                """
class Method:
    def __init__(self, definition, study_spec, rng):
        pass

    def propose(self, n_candidates, study_state):
        return []
""",
                True,
                "ready",
            ),
            (
                "python-session",
                {"python": "method:Method", "pythonPath": ["."], "protocol": "session"},
                """
class Method:
    def __init__(self, definition, study_spec, rng):
        pass

    def run(self, session):
        return None
""",
                False,
                "method_mode_unsupported",
            ),
            (
                "command-batch",
                {"command": ["python", "method.py"], "protocol": "batch"},
                "",
                False,
                "method_mode_unsupported",
            ),
            (
                "legacy-lifecycle",
                {"python": "method:Method", "pythonPath": ["."], "protocol": "batch"},
                """
class Method:
    def __init__(self, definition, study_spec, rng):
        pass

    def start(self, request):
        return "handle"

    def poll(self, handle):
        return {"done": True}

    def finalize(self, handle):
        return []
""",
                False,
                "method_propose_required",
            ),
            (
                "python-factory",
                {"python": "method:create_method", "pythonPath": ["."], "protocol": "batch"},
                """
class Method:
    def propose(self, n_candidates, study_state):
        return []

def create_method(definition, study_spec, rng):
    return Method()
""",
                False,
                "method_factory_requires_smoke",
            ),
        )

        for method_id, entrypoint, source, eligible, code in cases:
            with self.subTest(method_id=method_id), tempfile.TemporaryDirectory() as tmp_dir:
                package = Path(tmp_dir) / "local_package"
                method_dir = package / "methods" / method_id
                method_dir.mkdir(parents=True)
                if source:
                    (method_dir / "method.py").write_text(source, encoding="utf-8")
                (method_dir / "method.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "apiVersion": "optpilot.io/v1",
                            "config": "method",
                            "id": method_id,
                            "entrypoint": entrypoint,
                            "accepts": {"formats": ["parameters"]},
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )

                result = validate_package(package, check_imports=True)

                self.assertTrue(result["valid"], result)
                capability = result["entries"][0]["capabilities"][
                    "retained_execution"
                ]
                self.assertEqual(capability["eligible"], eligible, capability)
                self.assertEqual(capability["code"], code, capability)
                self.assertEqual(
                    result["capabilities"]["retained_execution"]["eligible"],
                    eligible,
                    result,
                )

    def test_schema_only_validation_does_not_claim_python_batch_is_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            method_dir = package / "methods" / "unchecked"
            method_dir.mkdir(parents=True)
            (method_dir / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "unchecked",
                        "entrypoint": {
                            "python": "method:Method",
                            "pythonPath": ["."],
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = validate_package(package)

        self.assertTrue(result["valid"], result)
        capability = result["entries"][0]["capabilities"]["retained_execution"]
        self.assertFalse(capability["eligible"])
        self.assertEqual(capability["code"], "method_callable_unchecked")


if __name__ == "__main__":
    unittest.main()
