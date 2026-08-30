"""Container authoring must never accept fields the compiler would discard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import compile_authoring_config, validate_authoring_config
from optpilot.package_validation import validate_package
from optpilot.realm.local_study_package import (
    LocalStudyPackagePlanError,
    plan_local_study_package,
)
from optpilot.schema_validation import validate_public_config_schema
from tests.core.test_realm_local_study_package import (
    _write_package,
    _write_package_settings,
)


IMAGE = "ghcr.io/example/runtime@sha256:" + "a" * 64


def _environment(runtime: dict | None = None) -> dict:
    raw = {
        "apiVersion": "optpilot.io/v1",
        "config": "environment",
        "id": "runtime-environment",
        "evaluator": {"python": "example:evaluate"},
        "candidate": {
            "format": "parameters",
            "parameters": {
                "schema": {"x": {"valueType": "float", "min": 0.0, "max": 1.0}}
            },
        },
        "metrics": {"source": "return", "keys": ["score"]},
    }
    if runtime is not None:
        raw["runtime"] = runtime
    return raw


def _method(runtime: dict | None = None) -> dict:
    raw = {
        "apiVersion": "optpilot.io/v1",
        "config": "method",
        "id": "runtime-method",
        "entrypoint": {"python": "example:Method", "protocol": "batch"},
        "accepts": {"formats": ["parameters"]},
    }
    if runtime is not None:
        raw["runtime"] = runtime
    return raw


def _container_runtime(**extra) -> dict:
    return {
        "sandbox": "container",
        "container": {
            "image": IMAGE,
            "platform": "linux/amd64",
            "network": "enabled",
            "limits": {"memory": "2g"},
        },
        **extra,
    }


PROCESS_ONLY_VALUES = {
    "setup": {"steps": [{"uses": "command", "command": ["python", "-V"]}]},
    "workdir": ".",
    "env": {"MODE": "local"},
    "envFromHost": ["MODEL_TOKEN"],
}


class StandaloneContainerRuntimeValidationTest(unittest.TestCase):
    def _write(self, raw: dict, root: Path) -> Path:
        path = root / f"{raw['config']}.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return path

    def test_environment_container_process_fields_fail_schema_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for field, value in PROCESS_ONLY_VALUES.items():
                with self.subTest(field=field):
                    raw = _environment(_container_runtime(**{field: value}))
                    self.assertFalse(validate_public_config_schema(raw).valid)
                    result = validate_authoring_config(self._write(raw, root))
                    self.assertFalse(result["valid"], result)

    def test_method_container_rejects_setup_and_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for field in ("setup", "workdir"):
                with self.subTest(field=field):
                    raw = _method(
                        _container_runtime(**{field: PROCESS_ONLY_VALUES[field]})
                    )
                    self.assertFalse(validate_public_config_schema(raw).valid)
                    result = validate_authoring_config(self._write(raw, root))
                    self.assertFalse(result["valid"], result)

    def test_study_compile_rejects_container_method_workdir_instead_of_dropping_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._write(_environment(), root)
            method = self._write(
                _method(_container_runtime(workdir="method-root")), root
            )
            study = root / "study.yaml"
            study.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "invalid-container-method-study",
                        "environmentConfig": environment.name,
                        "methodConfig": method.name,
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "schema validation"):
                compile_authoring_config(study)

    def test_legitimate_method_container_environment_and_limits_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._write(_environment(), root)
            method = self._write(
                _method(
                    _container_runtime(
                        env={"MODEL": "demo"},
                        envFromHost=["MODEL_TOKEN"],
                    )
                ),
                root,
            )
            study = root / "study.yaml"
            study.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "container-method-study",
                        "environmentConfig": environment.name,
                        "methodConfig": method.name,
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            compiled = compile_authoring_config(study)

        runtime = compiled["method"]["runtime"]
        self.assertEqual(runtime["env"], {"MODEL": "demo"})
        self.assertEqual(runtime["envFromHost"], ["MODEL_TOKEN"])
        self.assertEqual(
            runtime["container"],
            {
                "image": IMAGE,
                "platform": "linux/amd64",
                "network": "enabled",
                "limits": {"memory": "2g"},
            },
        )


class PackageContainerRuntimeValidationTest(unittest.TestCase):
    def _write_component_package(
        self,
        root: Path,
        *,
        component_kind: str,
        runtime: dict,
        package_container: bool = False,
    ) -> None:
        if package_container:
            _write_package_settings(root)
        raw = (
            _environment(runtime)
            if component_kind == "environment"
            else _method(runtime)
        )
        path = root / f"{component_kind}s" / "demo" / f"{component_kind}.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    def test_explicit_container_environment_package_cannot_publish_dropped_fields(
        self,
    ) -> None:
        for field, value in PROCESS_ONLY_VALUES.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._write_component_package(
                    root,
                    component_kind="environment",
                    runtime=_container_runtime(**{field: value}),
                )

                result = validate_package(root, check_dependencies=False)

                self.assertFalse(result["valid"], result)
                self.assertFalse(result["entries"][0]["valid"])

    def test_package_container_inheritance_rejects_each_process_only_field(self) -> None:
        cases = [
            ("environment", field, value)
            for field, value in PROCESS_ONLY_VALUES.items()
        ] + [
            ("method", field, PROCESS_ONLY_VALUES[field])
            for field in ("setup", "workdir")
        ]
        for component_kind, field, value in cases:
            with (
                self.subTest(component_kind=component_kind, field=field),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                self._write_component_package(
                    root,
                    component_kind=component_kind,
                    runtime={"sandbox": "process", field: value},
                    package_container=True,
                )

                result = validate_package(root, check_dependencies=False)

                self.assertFalse(result["valid"], result)
                errors = " ".join(result["entries"][0]["errors"])
                self.assertIn("process-only", errors)
                self.assertIn(field, errors)

    def test_local_package_plan_rejects_inherited_environment_host_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            study = _write_package(root)
            _write_package_settings(root)
            environment = root / "configs" / "environments" / "environment.yaml"
            raw = yaml.safe_load(environment.read_text(encoding="utf-8"))
            raw["runtime"] = {"sandbox": "process", "env": {"MODE": "local"}}
            environment.write_text(
                yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
            )

            with self.assertRaises(LocalStudyPackagePlanError) as caught:
                plan_local_study_package(study, root)

        self.assertEqual(caught.exception.code, "config_compile_failed")
        self.assertIn("process-only", str(caught.exception))
        self.assertIn("env", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
