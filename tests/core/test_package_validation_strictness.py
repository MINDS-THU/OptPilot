"""Package validation must not report an empty or broken package as green."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from optpilot.cli import main as cli_main
from optpilot.package_validation import validate_package
from tests.core.test_realm_local_study_package import (
    _write_package,
    _write_package_settings,
)


class PackageValidationStrictnessTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _write_environment(self) -> Path:
        path = self.root / "environments" / "demo" / "environment.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "environment",
                    "id": "demo",
                    "evaluator": {"python": "evaluator:evaluate"},
                    "candidate": {
                        "format": "parameters",
                        "parameters": {
                            "schema": {
                                "x": {
                                    "valueType": "float",
                                    "min": 0.0,
                                    "max": 1.0,
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
        return path

    def _write_method(self, root: Path | None = None) -> Path:
        base = root or self.root
        path = base / "methods" / "demo" / "method.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "method",
                    "id": "demo-method",
                    "entrypoint": {"python": "method:propose"},
                    "accepts": {"formats": ["parameters"]},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return path

    def _write_study(
        self,
        *,
        environment_config: str = "../environments/demo/environment.yaml",
        method_config: str = "../methods/demo/method.yaml",
    ) -> Path:
        path = self.root / "studies" / "study.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "study",
                    "name": "Demo study",
                    "environmentConfig": environment_config,
                    "methodConfig": method_config,
                    "objective": {"metric": "score", "direction": "maximize"},
                    "budget": {"maxTrials": 1},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_empty_directory_is_not_a_valid_package(self) -> None:
        result = validate_package(self.root)

        self.assertFalse(result["valid"])
        self.assertEqual(result["entries"], [])
        self.assertIn("no recognized", " ".join(result["errors"]).lower())

    def test_malformed_yaml_at_a_documented_config_path_is_an_error(self) -> None:
        path = self.root / "environments" / "demo" / "environment.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("candidate: [\n", encoding="utf-8")

        result = validate_package(self.root)

        self.assertFalse(result["valid"])
        self.assertIn(str(path), " ".join(result["errors"]))
        self.assertNotIn(str(path), result["ignored_yaml"])

    def test_domain_yaml_is_reported_without_invalidating_a_real_package(self) -> None:
        self._write_environment()
        domain = self.root / "environments" / "demo" / "cases" / "small.yaml"
        domain.parent.mkdir(parents=True)
        domain.write_text("demand: 3\n", encoding="utf-8")

        result = validate_package(self.root)

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["ignored_yaml"], [str(domain.resolve())])

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["package", "validate", str(self.root)])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ignored YAML", output.getvalue())
        self.assertIn(str(domain.resolve()), output.getvalue())

    @unittest.skipUnless(os.name == "posix", "symlink rejection requires POSIX")
    def test_external_yaml_config_symlink_is_rejected_without_indexing_target(self) -> None:
        self._write_environment()
        outside = self.root.parent / f"{self.root.name}-outside-resource.txt"
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "resource",
                    "id": "outside-resource",
                    "purpose": "reference",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        linked = (
            self.root
            / "resources"
            / "linked"
            / "optpilot.resource.yaml"
        )
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside)

        result = validate_package(self.root, check_dependencies=False)

        self.assertFalse(result["valid"])
        self.assertIn("symbolic link", " ".join(result["errors"]))
        self.assertNotIn(
            "outside-resource", {entry["id"] for entry in result["entries"]}
        )

    @unittest.skipUnless(os.name == "posix", "symlink rejection requires POSIX")
    def test_in_tree_yaml_config_symlink_is_also_rejected(self) -> None:
        self._write_environment()
        target = self.root / "method-source.txt"
        target.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "method",
                    "id": "aliased-method",
                    "entrypoint": {"python": "method:propose"},
                    "accepts": {"formats": ["parameters"]},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        linked = self.root / "methods" / "linked" / "method.yaml"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(target)

        result = validate_package(self.root, check_dependencies=False)

        self.assertFalse(result["valid"])
        self.assertIn("symbolic link", " ".join(result["errors"]))
        self.assertNotIn(
            "aliased-method", {entry["id"] for entry in result["entries"]}
        )

    @unittest.skipUnless(os.name == "posix", "symlink rejection requires POSIX")
    def test_symlink_package_root_is_rejected_before_resolution(self) -> None:
        self._write_environment()
        linked_root = self.root.parent / f"{self.root.name}-package-link"
        self.addCleanup(linked_root.unlink, missing_ok=True)
        linked_root.symlink_to(self.root, target_is_directory=True)

        result = validate_package(linked_root, check_dependencies=False)

        self.assertFalse(result["valid"])
        self.assertEqual(result["package"], str(linked_root))
        self.assertEqual(result["entries"], [])
        self.assertIn("root must not be a symbolic link", " ".join(result["errors"]))

    @unittest.skipUnless(os.name == "posix", "symlink rejection requires POSIX")
    def test_study_reference_through_symlink_directory_is_rejected_before_discovery(
        self,
    ) -> None:
        self._write_environment()
        outside_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temporary.cleanup)
        outside = Path(outside_temporary.name)
        outside_method = outside / "method.yaml"
        outside_method.write_text(
            "this malformed target must never be parsed: [\n", encoding="utf-8"
        )
        linked = self.root / "methods" / "linked"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside, target_is_directory=True)
        self._write_study(method_config="../methods/linked/method.yaml")

        result = validate_package(self.root, check_dependencies=False)

        self.assertFalse(result["valid"])
        self.assertEqual(result["entries"], [])
        encoded = " ".join(result["errors"])
        self.assertIn("Package trees must not contain symbolic links", encoded)
        self.assertIn(str(linked), encoded)
        self.assertNotIn("malformed target", encoded)

    def test_study_reference_outside_package_is_rejected_before_target_parse(self) -> None:
        self._write_environment()
        outside = self.root.parent / f"{self.root.name}-outside-method.yaml"
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text(
            "this malformed target must never be parsed: [\n", encoding="utf-8"
        )
        self._write_study(method_config=f"../../{outside.name}")

        result = validate_package(self.root, check_dependencies=False)

        self.assertFalse(result["valid"])
        study = next(
            entry for entry in result["entries"] if entry["config"] == "study"
        )
        self.assertFalse(study["valid"])
        encoded = " ".join(study["errors"])
        self.assertIn("methodConfig must stay inside package", encoded)
        self.assertNotIn("malformed target", encoded)

    def test_internal_study_references_remain_valid(self) -> None:
        self._write_environment()
        self._write_method()
        self._write_study()

        result = validate_package(self.root, check_dependencies=False)

        self.assertTrue(result["valid"], result)

    def test_package_container_conflicts_are_not_reported_green(self) -> None:
        package = self.root / "package"
        package.mkdir()
        _write_package(package)
        _write_package_settings(package)
        method = package / "configs" / "methods" / "method.yaml"
        method.write_text(
            method.read_text(encoding="utf-8")
            + "runtime:\n"
            + "  sandbox: process\n"
            + "  workdir: .\n",
            encoding="utf-8",
        )

        result = validate_package(package, check_dependencies=False)

        self.assertFalse(result["valid"])
        encoded = " ".join(
            error
            for entry in result["entries"]
            for error in entry.get("errors", [])
        )
        self.assertIn("process-only", encoded)

    def test_package_study_capability_compiles_with_inherited_container(self) -> None:
        package = self.root / "package"
        package.mkdir()
        _write_package(package)
        _write_package_settings(package)

        result = validate_package(package, check_dependencies=False)

        self.assertTrue(result["valid"], result)
        studies = result["capabilities"]["retained_execution"]["studies"]
        self.assertEqual(len(studies), 1)
        self.assertNotEqual(studies[0]["code"], "study_invalid")


if __name__ == "__main__":
    unittest.main()
