"""Environment capability callables and policyValidation declarations (F5)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import compile_authoring_config, validate_authoring_config
from optpilot.policy_validation import (
    validate_policy_declaration,
    validate_policy_sources,
)


_POLICY = {
    "entrypoint": {
        "file": "scheduler.py",
        "callable": "create_scheduler",
        "maxArguments": 0,
    },
    "forbiddenImports": ["os", "subprocess", "evaluator"],
    "forbiddenNames": ["create_controller"],
    "lints": [
        {
            "id": "battery-field",
            "forbiddenConstant": "battery",
            "message": "use 'battery_level' for AGV records.",
        }
    ],
}

_CONFORMING_SCHEDULER = """
def create_scheduler():
    def run(snapshot):
        return {"assignments": []}
    return run
"""


class ValidatePolicyDeclarationTest(unittest.TestCase):
    def test_accepts_the_full_declared_shape(self) -> None:
        validate_policy_declaration(_POLICY, "<test>")

    def test_rejects_malformed_declarations(self) -> None:
        cases = (
            ("not an object", [], "must be an object"),
            ("empty", {}, "at least one check"),
            ("unknown key", {"mystery": True}, "may contain only"),
            (
                "entrypoint missing callable",
                {"entrypoint": {"file": "a.py"}},
                "entrypoint",
            ),
            (
                "absolute entrypoint file",
                {"entrypoint": {"file": "/etc/a.py", "callable": "f"}},
                "portable relative",
            ),
            (
                "non-identifier callable",
                {"entrypoint": {"file": "a.py", "callable": "not a name"}},
                "identifier",
            ),
            (
                "negative maxArguments",
                {
                    "entrypoint": {
                        "file": "a.py",
                        "callable": "f",
                        "maxArguments": -1,
                    }
                },
                "maxArguments",
            ),
            (
                "non-identifier import",
                {"forbiddenImports": ["os.path"]},
                "identifiers",
            ),
            (
                "malformed lint",
                {"lints": [{"id": "x"}]},
                "exactly id, forbiddenConstant, and message",
            ),
            (
                "duplicate lint ids",
                {
                    "lints": [
                        {"id": "x", "forbiddenConstant": "a", "message": "m"},
                        {"id": "x", "forbiddenConstant": "b", "message": "m"},
                    ]
                },
                "duplicated",
            ),
        )
        for label, policy, needle in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as raised:
                    validate_policy_declaration(policy, "<test>")
                self.assertIn(needle, str(raised.exception))


class ValidatePolicySourcesTest(unittest.TestCase):
    def test_conforming_sources_pass(self) -> None:
        sources = {
            "scheduler.py": _CONFORMING_SCHEDULER,
            "param_estimator.py": "VALUE = 1\n",
        }
        self.assertEqual(validate_policy_sources(sources, _POLICY), [])

    def test_each_declared_check_reports_a_violation(self) -> None:
        cases = (
            ("missing file", {"other.py": "x = 1\n"}, "Missing required policy file"),
            (
                "syntax error",
                {"scheduler.py": "def broken(:\n"},
                "not valid Python",
            ),
            (
                "missing entrypoint",
                {"scheduler.py": "x = 1\n"},
                "exactly one top-level create_scheduler()",
            ),
            (
                "duplicate entrypoint",
                {
                    "scheduler.py": (
                        "def create_scheduler():\n    return None\n"
                        "def create_scheduler():\n    return None\n"
                    )
                },
                "exactly one top-level create_scheduler()",
            ),
            (
                "async entrypoint",
                {"scheduler.py": "async def create_scheduler():\n    return None\n"},
                "must be synchronous",
            ),
            (
                "arguments on entrypoint",
                {"scheduler.py": "def create_scheduler(x):\n    return x\n"},
                "at most 0 argument",
            ),
            (
                "starargs on entrypoint",
                {"scheduler.py": "def create_scheduler(*args):\n    return None\n"},
                "at most 0 argument",
            ),
            (
                "rebound entrypoint",
                {
                    "scheduler.py": (
                        _CONFORMING_SCHEDULER + "\ncreate_scheduler = None\n"
                    )
                },
                "must not rebind or shadow",
            ),
            (
                "forbidden import",
                {"scheduler.py": _CONFORMING_SCHEDULER + "\nimport os\n"},
                "forbidden module 'os'",
            ),
            (
                "forbidden from-import",
                {
                    "scheduler.py": (
                        _CONFORMING_SCHEDULER + "\nfrom subprocess import run\n"
                    )
                },
                "forbidden module 'subprocess'",
            ),
            (
                "aliased forbidden import binding",
                {
                    "scheduler.py": (
                        _CONFORMING_SCHEDULER
                        + "\nimport json as create_controller\n"
                    )
                },
                "forbidden identifier 'create_controller'",
            ),
            (
                "forbidden name binding",
                {
                    "scheduler.py": (
                        _CONFORMING_SCHEDULER + "\ncreate_controller = 1\n"
                    )
                },
                "forbidden identifier 'create_controller'",
            ),
            (
                "lint constant",
                {
                    "scheduler.py": (
                        _CONFORMING_SCHEDULER + "\nFIELD = 'battery'\n"
                    )
                },
                "battery_level",
            ),
        )
        for label, sources, needle in cases:
            with self.subTest(case=label):
                errors = validate_policy_sources(sources, _POLICY)
                self.assertTrue(errors, label)
                self.assertIn(needle, " ".join(errors))

    def test_violations_cover_every_file(self) -> None:
        sources = {
            "scheduler.py": _CONFORMING_SCHEDULER,
            "param_estimator.py": "import os\n",
        }
        errors = validate_policy_sources(sources, _POLICY)
        self.assertEqual(len(errors), 1)
        self.assertIn("param_estimator.py", errors[0])


class EnvironmentDeclarationValidationTest(unittest.TestCase):
    def _environment(self, **overrides) -> dict:
        config = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "capability-environment",
            "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
            "candidate": {
                "format": "parameters",
                "parameters": {
                    "schema": {"x": {"valueType": "float", "min": 0, "max": 1}}
                },
            },
            "metrics": {"source": "return", "keys": ["score"]},
        }
        config.update(overrides)
        return config

    def _validate(self, config: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "environment.yaml"
            path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            return validate_authoring_config(path)

    def test_capability_callable_and_policy_validation_are_accepted(self) -> None:
        result = self._validate(
            self._environment(
                capabilities=[
                    {
                        "id": "exact_seed_replay",
                        "description": "replay",
                        "callable": "evaluator:replay_candidate",
                    }
                ],
                policyValidation=_POLICY,
            )
        )
        self.assertTrue(result["valid"], result)

    def test_capability_and_policy_failures_are_reported(self) -> None:
        cases = (
            (
                "malformed callable",
                {"capabilities": [{"id": "replay", "callable": "no-colon"}]},
                "callable",
            ),
            (
                "duplicate capability ids",
                {
                    "capabilities": [
                        {"id": "replay"},
                        {"id": "replay"},
                    ]
                },
                "duplicated",
            ),
            (
                "empty policy",
                {"policyValidation": {}},
                "at least one check",
            ),
            (
                "unknown policy key",
                {"policyValidation": {"mystery": []}},
                "policyValidation",
            ),
        )
        for label, overrides, needle in cases:
            with self.subTest(case=label):
                result = self._validate(self._environment(**overrides))
                self.assertFalse(result["valid"], result)
                self.assertIn(needle, " ".join(result["errors"]))

    def test_policy_validation_travels_in_the_candidate_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            environment = self._environment(policyValidation=_POLICY)
            (root / "environment.yaml").write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )
            (root / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "capability-method",
                        "entrypoint": {
                            "python": "method:Method",
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = root / "study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "capability-study",
                        "environmentConfig": "environment.yaml",
                        "methodConfig": "method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            compiled = compile_authoring_config(study_path)
        context = compiled["candidate"]["context"]
        self.assertEqual(context["policyValidation"], _POLICY)
        self.assertEqual(
            compiled["environment"]["adapter"]["config"]["context"][
                "policyValidation"
            ],
            _POLICY,
        )

    def test_context_without_policy_is_byte_identical_to_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "environment.yaml").write_text(
                yaml.safe_dump(self._environment(), sort_keys=False),
                encoding="utf-8",
            )
            (root / "method.yaml").write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "method",
                        "id": "capability-method",
                        "entrypoint": {
                            "python": "method:Method",
                            "protocol": "batch",
                        },
                        "accepts": {"formats": ["parameters"]},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            study_path = root / "study.yaml"
            study_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "optpilot.io/v1",
                        "config": "study",
                        "name": "capability-study",
                        "environmentConfig": "environment.yaml",
                        "methodConfig": "method.yaml",
                        "objective": {"metric": "score", "direction": "maximize"},
                        "budget": {"maxTrials": 1},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            compiled = compile_authoring_config(study_path)
        self.assertNotIn("policyValidation", compiled["candidate"]["context"])


if __name__ == "__main__":
    unittest.main()
