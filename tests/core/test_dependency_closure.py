"""Undeclared run-time imports must be visible before a package travels.

The retained worker leaves the OptPilot interpreter's own site-packages on
``sys.path``, so a component that imports an undeclared third-party package
runs perfectly on the machine that authored it.  These tests pin the static
closure scan that names that dependency on the authoring machine instead of on
the machine that receives the package.

Every fixture is synthesized in a temporary directory, and no test may depend
on what happens to be installed on the host.
"""

from __future__ import annotations

import hashlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from optpilot.dependency_closure import (
    DEPENDENCY_DECLARED_CODE,
    DEPENDENCY_HOST_PROVISIONED_CODE,
    DEPENDENCY_UNSCANNED_CODE,
    locked_runtime_modules,
)
from optpilot.package_validation import validate_package


_LOCKED_MODULE = "optpilot_locked_dependency_fixture"


def _write_pure_python_wheel(path: Path, *, module: str) -> str:
    """Vendor a wheel without a package index or a build tool."""

    path.parent.mkdir(parents=True, exist_ok=True)
    files = (
        (f"{module}/__init__.py", b"VALUE = 3\n"),
        (
            "locked_fixture-1.0.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: locked-fixture\nVersion: 1.0.0\n",
        ),
        (
            "locked_fixture-1.0.0.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        ),
        ("locked_fixture-1.0.0.dist-info/top_level.txt", f"{module}\n".encode("utf-8")),
        ("locked_fixture-1.0.0.dist-info/RECORD", b""),
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files:
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.date_time = (2020, 1, 1, 0, 0, 0)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _method_config(
    *, method_id: str, python_path: list[str] | None = None, locked: bool = False
) -> str:
    payload = {
        "apiVersion": "optpilot.io/v1",
        "config": "method",
        "id": method_id,
        "entrypoint": {
            "python": "method:Method",
            "pythonPath": python_path or ["."],
            "protocol": "batch",
        },
        "accepts": {"formats": ["parameters"]},
    }
    if locked:
        payload["runtime"] = {
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
        }
    return yaml.safe_dump(payload, sort_keys=False)


_METHOD_BODY = (
    "class Method:\n"
    "    def __init__(self, definition, study_spec, rng): pass\n"
    "    def propose(self, n_candidates, study_state): return []\n"
)


class DependencyClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.package = Path(temporary.name) / "local_package"

    def _method_dir(self, method_id: str) -> Path:
        path = self.package / "methods" / method_id
        path.mkdir(parents=True)
        return path

    def _write_method(
        self,
        method_id: str,
        source: str,
        *,
        python_path: list[str] | None = None,
        locked: bool = False,
    ) -> Path:
        method_dir = self._method_dir(method_id)
        (method_dir / "method.py").write_text(source, encoding="utf-8")
        (method_dir / "method.yaml").write_text(
            _method_config(method_id=method_id, python_path=python_path, locked=locked),
            encoding="utf-8",
        )
        return method_dir

    def _closure(self, result: dict, index: int = 0) -> dict:
        return result["entries"][index]["capabilities"]["dependency_closure"]

    def test_stdlib_only_component_declares_its_whole_closure(self) -> None:
        self._write_method(
            "stdlib-only",
            "import json\nimport os.path\nfrom pathlib import Path\n\n" + _METHOD_BODY,
        )

        result = validate_package(self.package)

        self.assertTrue(result["valid"], result)
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)
        self.assertTrue(closure["declared"])
        self.assertEqual(closure["undeclared"], [])
        self.assertEqual(result["entries"][0]["warnings"], [])

    def test_optpilot_itself_is_not_reported_as_a_host_dependency(self) -> None:
        self._write_method(
            "imports-optpilot",
            "from optpilot.parameter_values import apply_parameter_defaults\n\n"
            + _METHOD_BODY,
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)

    def test_locked_wheel_provides_the_import_it_vendors(self) -> None:
        method_dir = self._write_method(
            "locked",
            f"import {_LOCKED_MODULE}\n\n" + _METHOD_BODY,
            locked=True,
        )
        digest = _write_pure_python_wheel(
            method_dir / "vendor" / "locked_fixture-1.0.0-py3-none-any.whl",
            module=_LOCKED_MODULE,
        )
        (method_dir / "requirements.lock").write_text(
            f"vendor/locked_fixture-1.0.0-py3-none-any.whl --hash=sha256:{digest}\n",
            encoding="utf-8",
        )

        result = validate_package(self.package, check_setup_files=True)

        self.assertTrue(result["valid"], result)
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)
        self.assertEqual(closure["locked_distributions"], ["locked-fixture"])
        self.assertEqual(result["entries"][0]["warnings"], [])

    def test_locked_component_still_reports_imports_the_lock_omits(self) -> None:
        """The gap ``--check-imports`` leaves: a locked component is not exempt."""

        method_dir = self._write_method(
            "locked-and-leaky",
            f"import {_LOCKED_MODULE}\nimport pandas\n\n" + _METHOD_BODY,
            locked=True,
        )
        digest = _write_pure_python_wheel(
            method_dir / "vendor" / "locked_fixture-1.0.0-py3-none-any.whl",
            module=_LOCKED_MODULE,
        )
        (method_dir / "requirements.lock").write_text(
            f"vendor/locked_fixture-1.0.0-py3-none-any.whl --hash=sha256:{digest}\n",
            encoding="utf-8",
        )

        result = validate_package(self.package, check_setup_files=True)

        self.assertTrue(result["valid"], result)
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertEqual(
            [item["module"] for item in closure["undeclared"]], ["pandas"]
        )

    def test_vendored_in_tree_source_dependency_is_not_a_host_dependency(self) -> None:
        method_dir = self._write_method(
            "vendored",
            "import vendored_sim\nfrom vendored_sim.core import Engine\n\n"
            + _METHOD_BODY,
        )
        package_dir = method_dir / "vendored_sim"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text(
            "from .core import Engine\n", encoding="utf-8"
        )
        (package_dir / "core.py").write_text(
            "import heapq\n\n\nclass Engine:\n    pass\n", encoding="utf-8"
        )

        result = validate_package(self.package)

        self.assertTrue(result["valid"], result)
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)
        self.assertIn("methods/vendored/vendored_sim/core.py", closure["scanned_files"])

    def test_undeclared_third_party_import_is_reported(self) -> None:
        self._write_method(
            "undeclared",
            "import scipy.optimize\n\n" + _METHOD_BODY,
        )

        result = validate_package(self.package)

        self.assertTrue(result["valid"], result)
        self.assertFalse(result["entries"][0]["errors"])
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertFalse(closure["declared"])
        finding = closure["undeclared"][0]
        self.assertEqual(finding["module"], "scipy")
        self.assertEqual(finding["files"], ["methods/undeclared/method.py"])
        warning = result["entries"][0]["warnings"][0]
        self.assertIn(DEPENDENCY_HOST_PROVISIONED_CODE, warning)
        self.assertIn("scipy", warning)

    def test_import_deferred_inside_propose_is_reported(self) -> None:
        """A host import check only imports the entry module; this does not."""

        self._write_method(
            "deferred",
            "class Method:\n"
            "    def __init__(self, definition, study_spec, rng): pass\n"
            "    def propose(self, n_candidates, study_state):\n"
            "        from torch import nn\n"
            "        return []\n",
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertEqual([item["module"] for item in closure["undeclared"]], ["torch"])

    def test_transitive_in_package_import_carries_the_scan(self) -> None:
        method_dir = self._write_method(
            "transitive",
            "from helper import solve\n\n" + _METHOD_BODY,
        )
        (method_dir / "helper.py").write_text(
            "def solve():\n    import cvxpy\n    return cvxpy\n", encoding="utf-8"
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        finding = closure["undeclared"][0]
        self.assertEqual(finding["module"], "cvxpy")
        self.assertEqual(finding["files"], ["methods/transitive/helper.py"])

    def test_shared_python_path_root_resolves_sibling_module(self) -> None:
        method_dir = self._write_method(
            "shared-root",
            "from shared_solvers import solve\n\n" + _METHOD_BODY,
            python_path=[".", ".."],
        )
        (method_dir.parent / "shared_solvers.py").write_text(
            "def solve():\n    return 1\n", encoding="utf-8"
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)
        self.assertIn("methods/shared_solvers.py", closure["scanned_files"])

    def test_payload_not_imported_by_the_component_is_left_alone(self) -> None:
        """Candidate templates run in a trial workspace, not in the component."""

        method_dir = self._write_method("payload-owner", _METHOD_BODY)
        templates = method_dir / "templates"
        templates.mkdir()
        (templates / "candidate.py").write_text(
            "import lightgbm\n", encoding="utf-8"
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_DECLARED_CODE)
        self.assertNotIn(
            "methods/payload-owner/templates/candidate.py", closure["scanned_files"]
        )

    def test_command_entrypoint_script_is_scanned(self) -> None:
        method_dir = self._method_dir("command-method")
        (method_dir / "worker.py").write_text("import requests\n", encoding="utf-8")
        (method_dir / "method.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "method",
                    "id": "command-method",
                    "entrypoint": {
                        "command": ["python", "worker.py"],
                        "protocol": "batch",
                    },
                    "accepts": {"formats": ["parameters"]},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        closure = self._closure(validate_package(self.package))

        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertEqual(closure["scanned_files"], ["methods/command-method/worker.py"])

    def test_unreadable_lock_reports_unknown_rather_than_accusing(self) -> None:
        method_dir = self._write_method(
            "broken-lock",
            "import some_prepared_dependency\n\n" + _METHOD_BODY,
            locked=True,
        )
        (method_dir / "requirements.lock").write_text(
            "vendor/absent-1.0.0-py3-none-any.whl --hash=sha256:" + "a" * 64 + "\n",
            encoding="utf-8",
        )

        result = validate_package(self.package)

        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_UNSCANNED_CODE)
        self.assertIn("could not be read", closure["reason"])
        self.assertEqual(result["entries"][0]["warnings"], [])

    def test_package_summary_names_every_host_provisioned_module(self) -> None:
        self._write_method("first", "import scipy\n\n" + _METHOD_BODY)
        self._write_method("second", "import polars\n\n" + _METHOD_BODY)

        summary = validate_package(self.package)["capabilities"]["dependency_closure"]

        self.assertEqual(summary["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertFalse(summary["declared"])
        self.assertEqual(summary["undeclared_modules"], ["polars", "scipy"])
        self.assertEqual(len(summary["components"]), 2)

    def test_dependency_check_can_be_turned_off(self) -> None:
        self._write_method("undeclared", "import scipy\n\n" + _METHOD_BODY)

        result = validate_package(self.package, check_dependencies=False)

        self.assertNotIn("dependency_closure", result["entries"][0]["capabilities"])
        self.assertNotIn("dependency_closure", result["capabilities"])
        self.assertEqual(result["entries"][0]["warnings"], [])

    def test_environment_evaluator_and_published_module_are_both_scanned(self) -> None:
        environment_dir = self.package / "environments" / "sample"
        environment_dir.mkdir(parents=True)
        (environment_dir / "evaluator.py").write_text(
            "def evaluate(candidate, context):\n    return {'score': 1.0}\n",
            encoding="utf-8",
        )
        (environment_dir / "policy_adapter.py").write_text(
            "import gymnasium\n", encoding="utf-8"
        )
        (environment_dir / "starting_point.py").write_text(
            "import lightgbm\n", encoding="utf-8"
        )
        (environment_dir / "environment.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "environment",
                    "id": "sample",
                    "evaluator": {
                        "python": "evaluator:evaluate",
                        "pythonPath": ["."],
                        "settings": {},
                    },
                    "candidate": {
                        "format": "parameters",
                        "parameters": {
                            "schema": {
                                "rate": {"valueType": "float", "min": 0.0, "max": 1.0}
                            }
                        },
                    },
                    "metrics": {"source": "return", "keys": ["score"]},
                    "methodContext": {
                        "references": [
                            {
                                "name": "policy_adapter",
                                "path": "policy_adapter.py",
                                "type": "python_module",
                            },
                            {
                                "name": "starting_point.py",
                                "path": "starting_point.py",
                                "type": "candidate_template",
                            },
                        ]
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        result = validate_package(self.package)

        self.assertTrue(result["valid"], result)
        closure = self._closure(result)
        self.assertEqual(closure["code"], DEPENDENCY_HOST_PROVISIONED_CODE)
        self.assertEqual(
            [item["module"] for item in closure["undeclared"]], ["gymnasium"]
        )


class LockedRuntimeProvisionTest(unittest.TestCase):
    def test_wheel_top_level_names_are_read_out_of_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = Path(tmp_dir) / "local_package"
            component = package / "methods" / "locked"
            component.mkdir(parents=True)
            digest = _write_pure_python_wheel(
                component / "vendor" / "locked_fixture-1.0.0-py3-none-any.whl",
                module=_LOCKED_MODULE,
            )
            (component / "requirements.lock").write_text(
                f"vendor/locked_fixture-1.0.0-py3-none-any.whl --hash=sha256:{digest}\n",
                encoding="utf-8",
            )

            provision = locked_runtime_modules(
                {
                    "cache": "prepared",
                    "steps": [
                        {
                            "uses": "python-venv",
                            "cwd": ".",
                            "requirements": ["requirements.lock"],
                        }
                    ],
                },
                config_dir=component,
                package_root=package,
            )

        self.assertEqual(provision.modules, frozenset({_LOCKED_MODULE}))
        self.assertEqual(provision.distributions, ("locked-fixture",))
        self.assertEqual(provision.unreadable, ())

    def test_unsupported_setup_provides_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            provision = locked_runtime_modules(
                {"cache": "prepared", "steps": [{"uses": "uv", "cwd": "."}]},
                config_dir=Path(tmp_dir),
                package_root=Path(tmp_dir),
            )

        self.assertEqual(provision.modules, frozenset())
        self.assertEqual(provision.unreadable, ())


if __name__ == "__main__":
    unittest.main()
