"""Tests for typed input declarations (settingsSchema / resource inputs)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import validate_authoring_config
from optpilot.parameter_values import apply_parameter_defaults, validate_parameter_values
from optpilot.schema_validation import validate_public_config_schema


def _write_config(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _method_config(**overrides) -> dict:
    config = {
        "apiVersion": "optpilot.io/v1",
        "config": "method",
        "id": "typed-method",
        "entrypoint": {"python": "method:propose", "protocol": "batch"},
        "accepts": {"formats": ["parameters"]},
    }
    config.update(overrides)
    return config


def _environment_config(**evaluator_overrides) -> dict:
    evaluator = {"python": "evaluator:evaluate", "pythonPath": ["."]}
    evaluator.update(evaluator_overrides)
    return {
        "apiVersion": "optpilot.io/v1",
        "config": "environment",
        "id": "typed-environment",
        "evaluator": evaluator,
        "candidate": {
            "format": "parameters",
            "parameters": {"schema": {"x": {"valueType": "float", "min": 0, "max": 1}}},
        },
        "metrics": {"source": "return", "keys": ["score"]},
    }


class ParameterValueValidationTest(unittest.TestCase):
    SCHEMA = {
        "iterations": {"valueType": "int", "min": 1, "max": 10, "default": 3},
        "model": {"valueType": "string"},
        "mode": {"valueType": "categorical", "values": ["fast", "thorough"], "default": "fast"},
        "weights": {
            "valueType": "array",
            "items": {"valueType": "float", "min": 0},
            "minItems": 1,
            "default": [1.0],
        },
        "advanced": {
            "valueType": "object",
            "properties": {"seed": {"valueType": "int"}},
            "required": ["seed"],
            "default": {"seed": 0},
        },
    }

    def test_accepts_conforming_values(self) -> None:
        errors = validate_parameter_values(
            {
                "iterations": 5,
                "model": "gpt-test",
                "mode": "thorough",
                "weights": [0.2, 0.8],
                "advanced": {"seed": 42},
            },
            self.SCHEMA,
            location="settings",
        )
        self.assertEqual(errors, [])

    def test_missing_required_value_is_reported(self) -> None:
        errors = validate_parameter_values({}, self.SCHEMA, location="settings")
        self.assertEqual(len(errors), 1)
        self.assertIn("settings.model is required", errors[0])

    def test_undeclared_key_is_rejected(self) -> None:
        errors = validate_parameter_values(
            {"model": "m", "mystery": 1}, self.SCHEMA, location="settings"
        )
        self.assertTrue(any("settings.mystery is not declared" in error for error in errors))

    def test_type_bound_and_membership_violations(self) -> None:
        errors = validate_parameter_values(
            {
                "iterations": 99,
                "model": 7,
                "mode": "wrong",
                "weights": [],
                "advanced": {"seed": "x", "extra": 1},
            },
            self.SCHEMA,
            location="settings",
        )
        text = " ".join(errors)
        self.assertIn("settings.iterations=99 is above maximum 10", text)
        self.assertIn("settings.model must be a string", text)
        self.assertIn("settings.mode must be one of", text)
        self.assertIn("settings.weights must have at least 1 item(s)", text)
        self.assertIn("settings.advanced.seed must be an integer", text)
        self.assertIn("settings.advanced.extra is not declared", text)

    def test_bool_is_not_a_number(self) -> None:
        errors = validate_parameter_values(
            {"iterations": True, "model": "m"}, self.SCHEMA, location="settings"
        )
        self.assertTrue(any("settings.iterations must be an integer" in error for error in errors))

    def test_apply_parameter_defaults(self) -> None:
        merged = apply_parameter_defaults({"model": "m"}, self.SCHEMA)
        self.assertEqual(merged["iterations"], 3)
        self.assertEqual(merged["mode"], "fast")
        self.assertEqual(merged["weights"], [1.0])
        self.assertEqual(merged["advanced"], {"seed": 0})
        self.assertEqual(merged["model"], "m")


class MethodSettingsSchemaTest(unittest.TestCase):
    def test_method_with_conforming_settings_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "method.yaml",
                _method_config(
                    settingsSchema={
                        "iterations": {"valueType": "int", "min": 1, "default": 3},
                        "model": {"valueType": "string"},
                    },
                    settings={"model": "gpt-test"},
                ),
            )
            result = validate_authoring_config(path)
        self.assertTrue(result["valid"], result)

    def test_method_settings_violating_schema_fail_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "method.yaml",
                _method_config(
                    settingsSchema={"iterations": {"valueType": "int", "min": 1}},
                    settings={"iterations": 0},
                ),
            )
            result = validate_authoring_config(path)
        self.assertFalse(result["valid"], result)
        self.assertIn("below minimum", " ".join(result["errors"]))

    def test_method_undeclared_setting_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "method.yaml",
                _method_config(
                    settingsSchema={"iterations": {"valueType": "int", "default": 1}},
                    settings={"unknown": 5},
                ),
            )
            result = validate_authoring_config(path)
        self.assertFalse(result["valid"], result)
        self.assertIn("is not declared", " ".join(result["errors"]))

    def test_method_without_settings_schema_keeps_untyped_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "method.yaml",
                _method_config(settings={"anything": {"nested": True}}),
            )
            result = validate_authoring_config(path)
        self.assertTrue(result["valid"], result)

    def test_invalid_settings_schema_definition_is_rejected(self) -> None:
        raw = _method_config(settingsSchema={"iterations": {"valueType": "mystery"}})
        result = validate_public_config_schema(raw)
        self.assertFalse(result.valid)


class EnvironmentEvaluatorSettingsSchemaTest(unittest.TestCase):
    def test_evaluator_settings_validated_against_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "evaluator.py").write_text("def evaluate(c, x):\n    return {}\n", encoding="utf-8")
            path = _write_config(
                directory,
                "environment.yaml",
                _environment_config(
                    settingsSchema={"scenario": {"valueType": "categorical", "values": ["a", "b"]}},
                    settings={"scenario": "c"},
                ),
            )
            result = validate_authoring_config(path)
        self.assertFalse(result["valid"], result)
        self.assertIn("must be one of", " ".join(result["errors"]))

    def test_evaluator_conforming_settings_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "evaluator.py").write_text("def evaluate(c, x):\n    return {}\n", encoding="utf-8")
            path = _write_config(
                directory,
                "environment.yaml",
                _environment_config(
                    settingsSchema={"scenario": {"valueType": "categorical", "values": ["a", "b"]}},
                    settings={"scenario": "a"},
                ),
            )
            result = validate_authoring_config(path)
        self.assertTrue(result["valid"], result)


class ResourceInputsTest(unittest.TestCase):
    def _resource_config(self, **overrides) -> dict:
        config = {
            "apiVersion": "optpilot.io/v1",
            "config": "resource",
            "id": "typed-resource",
            "purpose": "generator",
        }
        config.update(overrides)
        return config

    def test_resource_inputs_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "resource.yaml",
                self._resource_config(
                    inputs={
                        "specification": {"valueType": "string", "description": "NL system spec."},
                        "horizon": {"valueType": "int", "min": 1, "default": 100},
                    }
                ),
            )
            result = validate_authoring_config(path)
        self.assertTrue(result["valid"], result)

    def test_resource_inputs_with_bad_definition_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                "resource.yaml",
                self._resource_config(inputs={"specification": {"valueType": "mystery"}}),
            )
            result = validate_authoring_config(path)
        self.assertFalse(result["valid"], result)

    def test_resource_inputs_empty_map_rejected_by_schema(self) -> None:
        raw = self._resource_config(inputs={})
        result = validate_public_config_schema(raw)
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
