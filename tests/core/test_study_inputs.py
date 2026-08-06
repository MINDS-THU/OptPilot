"""Tests for per-launch Study inputs (study ``inputs`` + launch binding)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import compile_authoring_config, validate_authoring_config
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.service import RealmContentService
from optpilot.retained_study_service import RetainedStudyService
from optpilot.schema_validation import validate_public_config_schema


_INPUTS_DECLARATION = {
    "problem": {"valueType": "string", "description": "Natural-language problem statement."},
    "budget_hint": {"valueType": "int", "min": 1, "default": 60},
}


def _write_config(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _environment_config(**overrides) -> dict:
    config = {
        "apiVersion": "optpilot.io/v1",
        "config": "environment",
        "id": "inputs-environment",
        "evaluator": {"python": "local_package.evaluate:evaluate", "pythonPath": ["../.."]},
        "candidate": {
            "format": "parameters",
            "parameters": {"schema": {"x": {"valueType": "float", "min": 0.0, "max": 1.0}}},
        },
        "metrics": {"source": "return", "keys": ["score"]},
    }
    config.update(overrides)
    return config


def _method_config(**overrides) -> dict:
    config = {
        "apiVersion": "optpilot.io/v1",
        "config": "method",
        "id": "inputs-method",
        "entrypoint": {
            "python": "local_package.method:RetainedMethod",
            "pythonPath": ["../.."],
            "protocol": "batch",
        },
        "settings": {"batchSize": 1},
        "accepts": {"formats": ["parameters"]},
    }
    config.update(overrides)
    return config


def _study_config(**overrides) -> dict:
    config = {
        "apiVersion": "optpilot.io/v1",
        "config": "study",
        "name": "inputs-study",
        "environmentConfig": "../environments/environment.yaml",
        "methodConfig": "../methods/method.yaml",
        "objective": {"metric": "score", "direction": "maximize"},
        "budget": {"maxTrials": 1},
        "execution": {"parallelism": 1, "timeoutSeconds": 30},
    }
    config.update(overrides)
    return config


def _write_package(
    root: Path,
    *,
    study_overrides: dict | None = None,
    environment_overrides: dict | None = None,
    method_overrides: dict | None = None,
) -> Path:
    study = _write_config(
        root, "configs/studies/study.yaml", _study_config(**(study_overrides or {}))
    )
    _write_config(
        root,
        "configs/environments/environment.yaml",
        _environment_config(**(environment_overrides or {})),
    )
    _write_config(
        root, "configs/methods/method.yaml", _method_config(**(method_overrides or {}))
    )
    package = root / "local_package"
    package.mkdir(exist_ok=True)
    (package / "evaluate.py").write_text(
        "def evaluate(candidate, context):\n"
        "    inputs = context['settings']['inputs']\n"
        "    return {'score': float(len(inputs['problem']))}\n",
        encoding="utf-8",
    )
    (package / "method.py").write_text(
        "class RetainedMethod:\n    pass\n", encoding="utf-8"
    )
    return study


class StudyInputsSchemaTest(unittest.TestCase):
    def test_valid_inputs_declaration_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(
                Path(tmp), study_overrides={"inputs": dict(_INPUTS_DECLARATION)}
            )
            result = validate_authoring_config(study)
        self.assertTrue(result["valid"], result)

    def test_invalid_parameter_definition_fails(self) -> None:
        raw = _study_config(inputs={"problem": {"valueType": "mystery"}})
        result = validate_public_config_schema(raw)
        self.assertFalse(result.valid)

    def test_empty_inputs_map_fails_schema(self) -> None:
        raw = _study_config(inputs={})
        result = validate_public_config_schema(raw)
        self.assertFalse(result.valid)


class StudyInputsCompileBindingTest(unittest.TestCase):
    def _compile(self, tmp: Path, launch_inputs=None, **kwargs):
        study = _write_package(
            tmp, study_overrides={"inputs": dict(_INPUTS_DECLARATION)}, **kwargs
        )
        return compile_authoring_config(study, launch_inputs=launch_inputs)

    def test_defaults_merge_and_values_embed_in_both_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._compile(Path(tmp), launch_inputs={"problem": "maximize throughput"})
        expected = {"problem": "maximize throughput", "budget_hint": 60}
        evaluate_config = spec["environment"]["adapter"]["config"]["evaluate"]["config"]
        self.assertEqual(evaluate_config["inputs"], expected)
        self.assertEqual(spec["method"]["config"]["inputs"], expected)
        self.assertEqual(spec["method"]["settings"]["inputs"], expected)
        self.assertEqual(
            spec["extensions"]["authoringConfig"]["inputs"],
            {"declaration": _INPUTS_DECLARATION, "values": expected},
        )

    def test_supplied_value_overrides_default_and_yaml_types_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._compile(
                Path(tmp), launch_inputs={"problem": "p", "budget_hint": 30}
            )
        self.assertEqual(
            spec["method"]["settings"]["inputs"],
            {"problem": "p", "budget_hint": 30},
        )

    def test_bound_violation_raises_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "below minimum"):
                self._compile(Path(tmp), launch_inputs={"problem": "p", "budget_hint": 0})

    def test_type_violation_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "must be a string"):
                self._compile(Path(tmp), launch_inputs={"problem": 7})

    def test_undeclared_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "is not declared"):
                self._compile(Path(tmp), launch_inputs={"problem": "p", "mystery": 1})


class StudyInputsFailClosedTest(unittest.TestCase):
    def test_inputs_supplied_without_declaration_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(Path(tmp))
            with self.assertRaisesRegex(ValueError, "declares no inputs"):
                compile_authoring_config(study, launch_inputs={"problem": "p"})

    def test_missing_required_input_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(
                Path(tmp), study_overrides={"inputs": dict(_INPUTS_DECLARATION)}
            )
            with self.assertRaisesRegex(ValueError, "problem is required"):
                compile_authoring_config(study)

    def test_all_defaults_launch_without_supplied_inputs(self) -> None:
        declaration = {"budget_hint": {"valueType": "int", "min": 1, "default": 60}}
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(Path(tmp), study_overrides={"inputs": declaration})
            spec = compile_authoring_config(study)
        self.assertEqual(spec["method"]["settings"]["inputs"], {"budget_hint": 60})

    def test_reserved_key_in_method_settings_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(
                Path(tmp),
                study_overrides={"inputs": dict(_INPUTS_DECLARATION)},
                method_overrides={"settings": {"batchSize": 1, "inputs": {"a": 1}}},
            )
            with self.assertRaisesRegex(ValueError, "reserved"):
                compile_authoring_config(study, launch_inputs={"problem": "p"})

    def test_reserved_key_in_evaluator_settings_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(
                Path(tmp),
                study_overrides={"inputs": dict(_INPUTS_DECLARATION)},
                environment_overrides={
                    "evaluator": {
                        "python": "local_package.evaluate:evaluate",
                        "pythonPath": ["../.."],
                        "settings": {"inputs": {"a": 1}},
                    }
                },
            )
            with self.assertRaisesRegex(ValueError, "reserved"):
                compile_authoring_config(study, launch_inputs={"problem": "p"})

    def test_reserved_key_without_declared_inputs_stays_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(
                Path(tmp),
                method_overrides={"settings": {"batchSize": 1, "inputs": {"a": 1}}},
            )
            spec = compile_authoring_config(study)
        self.assertEqual(spec["method"]["settings"]["inputs"], {"a": 1})


class StudyInputsNoInputsRegressionTest(unittest.TestCase):
    def test_study_without_inputs_compiles_without_any_inputs_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study = _write_package(Path(tmp))
            spec = compile_authoring_config(study)
            again = compile_authoring_config(study, launch_inputs=None)
        self.assertEqual(spec, again)
        self.assertNotIn(
            "inputs", spec["environment"]["adapter"]["config"]["evaluate"]["config"]
        )
        self.assertNotIn("inputs", spec["method"]["config"])
        self.assertNotIn("inputs", spec["method"]["settings"])
        self.assertNotIn("inputs", spec["extensions"]["authoringConfig"])


class StudyInputsCliParsingTest(unittest.TestCase):
    def _parse(self, argv: list[str]):
        from optpilot.cli import _parse_launch_inputs, build_parser

        args = build_parser().parse_args(argv)
        return _parse_launch_inputs(args)

    def test_no_flags_is_none(self) -> None:
        self.assertIsNone(self._parse(["run", "s.yaml", "--package-root", "p"]))

    def test_values_parse_as_yaml_scalars(self) -> None:
        parsed = self._parse(
            [
                "run",
                "s.yaml",
                "--package-root",
                "p",
                "--input",
                "problem=maximize throughput",
                "--input",
                "budget_hint=30",
                "--input",
                "flag=true",
                "--input",
                'quoted="30"',
            ]
        )
        self.assertEqual(
            parsed,
            {
                "problem": "maximize throughput",
                "budget_hint": 30,
                "flag": True,
                "quoted": "30",
            },
        )

    def test_inputs_file_merges_and_input_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inputs_file = Path(tmp) / "inputs.yaml"
            inputs_file.write_text(
                yaml.safe_dump({"problem": "from file", "budget_hint": 5}),
                encoding="utf-8",
            )
            parsed = self._parse(
                [
                    "run",
                    "s.yaml",
                    "--package-root",
                    "p",
                    "--inputs-file",
                    str(inputs_file),
                    "--input",
                    "budget_hint=30",
                ]
            )
        self.assertEqual(parsed, {"problem": "from file", "budget_hint": 30})

    def test_malformed_input_flag_fails(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse(
                ["run", "s.yaml", "--package-root", "p", "--input", "no-equals"]
            )


class StudyInputsRetainedDefinitionTest(unittest.TestCase):
    """Launch-service-level proof that inputs reach the retained definition.

    The retained ``evaluator_contract`` field asserted below is exactly what
    ``runtime_binding`` lifts into the attempt request's ``evaluator_settings``
    (delivered to the evaluator as ``context["settings"]``), and the retained
    ``method_contract`` ``config``/``settings`` are what the retained batch
    worker exposes to the authored method as its settings.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.study_path = _write_package(
            self.package_root,
            study_overrides={"inputs": dict(_INPUTS_DECLARATION)},
        )
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.addCleanup(self.ledger.close)
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.addCleanup(self.store.close)
        self.ledger.register_principal(
            operation_id="study-inputs/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="study-inputs/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        content_service = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        projection_service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.service = RetainedStudyService(
            self.ledger,
            content_service,
            projection_service,
            ProcessProviderIdentity(
                builder_fingerprint="a" * 64,
                platform="test-platform",
            ),
        )

    def prepare(self, *, tag: str, launch_inputs=None):
        return self.service.prepare_local_package(
            operation_id=f"study-inputs/{tag}",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id=f"source-owner-{tag}",
            study_definition_owner_id=f"definition-owner-{tag}",
            launch_inputs=launch_inputs,
        )

    def test_definition_retains_inputs_in_both_contracts(self) -> None:
        receipt = self.prepare(
            tag="retained", launch_inputs={"problem": "maximize throughput"}
        )
        expected = {"problem": "maximize throughput", "budget_hint": 60}
        run_definition = receipt.study_definition.manifest.run_definition
        evaluator_contract = (
            run_definition.evaluation_closure.environment_revision.evaluator_contract
        )
        self.assertEqual(
            dict(evaluator_contract["adapter"]["config"]["evaluate"]["config"]["inputs"]),
            expected,
        )
        method_contract = run_definition.method_revision.method_contract
        self.assertEqual(dict(method_contract["config"]["inputs"]), expected)
        self.assertEqual(dict(method_contract["settings"]["inputs"]), expected)

    def test_same_inputs_reproduce_and_different_inputs_diverge(self) -> None:
        first = self.prepare(tag="repro-a", launch_inputs={"problem": "same"})
        second = self.prepare(tag="repro-b", launch_inputs={"problem": "same"})
        third = self.prepare(tag="repro-c", launch_inputs={"problem": "different"})
        self.assertEqual(
            first.study_definition.manifest.run_definition_digest,
            second.study_definition.manifest.run_definition_digest,
        )
        self.assertNotEqual(
            first.study_definition.manifest.run_definition_digest,
            third.study_definition.manifest.run_definition_digest,
        )

    def test_missing_required_input_fails_before_any_definition(self) -> None:
        with self.assertRaisesRegex(Exception, "problem is required"):
            self.prepare(tag="fail-closed")
        # The launch failed during compilation; no study definition owner was
        # committed for this operation.
        from optpilot.realm.errors import RealmNotFound
        from optpilot.realm.owners import OwnerPermission

        with self.assertRaises(RealmNotFound):
            self.ledger.read_owner(
                actor_principal_id="operator",
                owner_id="definition-owner-fail-closed",
                permission=OwnerPermission.DERIVE,
            )


if __name__ == "__main__":
    unittest.main()
