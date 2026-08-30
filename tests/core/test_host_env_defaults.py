"""A declared host value may carry a default; a secret never may.

Declaring which environment values come from the person's machine used to be a
list of names, all required. That made a model id indistinguishable from an API
key: both had to be set before anything ran, even though the package knows a
working model id and only the person knows their key. Installing OptPilot meant
being asked for several values whose meaning was explained nowhere.

Two lines are drawn here. A default is a fallback, never an override -- a value
set in Settings or exported still wins. And secrets are excluded by
construction: a default secret is either useless or a credential written into a
settings file.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import yaml

from optpilot.host_env import (
    compile_host_env_declarations,
    host_env_defaults,
    host_env_names,
    host_env_required_names,
)
from optpilot.schema_validation import validate_public_config_schema
from optpilot.setup import setup_env

_ROOT = Path(__file__).resolve().parents[2]


class DeclarationReadingTest(unittest.TestCase):
    def test_a_plain_name_is_still_required(self) -> None:
        declared = compile_host_env_declarations(["PLAIN"])
        self.assertEqual(declared[0].name, "PLAIN")
        self.assertTrue(declared[0].required)

    def test_a_name_with_a_default_is_not_required(self) -> None:
        declared = compile_host_env_declarations(
            [{"name": "MODEL", "default": "some/model"}]
        )
        self.assertFalse(declared[0].required)
        self.assertEqual(declared[0].default, "some/model")

    def test_both_forms_mix(self) -> None:
        declared = ["KEY_A", {"name": "KEY_B", "default": "b"}]
        self.assertEqual(host_env_names(declared), ["KEY_A", "KEY_B"])
        self.assertEqual(host_env_required_names(declared), ["KEY_A"])
        self.assertEqual(host_env_defaults(declared), {"KEY_B": "b"})

    def test_malformed_declarations_are_refused(self) -> None:
        for bad, because in (
            ([{"name": "A"}], "no default and not a plain name"),
            ([{"name": "A", "default": 1}], "default is not text"),
            (["A", "A"], "the same name twice"),
            ([3], "not a name at all"),
            ([{"default": "x"}], "no name"),
        ):
            with self.subTest(because=because):
                with self.assertRaises(ValueError):
                    compile_host_env_declarations(bad)


class ResolutionTest(unittest.TestCase):
    def test_the_default_is_used_when_the_host_has_nothing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPTPILOT_TEST_MODEL", None)
            env = setup_env(
                {
                    "steps": [],
                    "envFromHost": [
                        {"name": "OPTPILOT_TEST_MODEL", "default": "fallback/model"}
                    ],
                }
            )
        self.assertEqual(env["OPTPILOT_TEST_MODEL"], "fallback/model")

    def test_the_host_always_wins(self) -> None:
        with mock.patch.dict(
            os.environ, {"OPTPILOT_TEST_MODEL": "chosen/model"}, clear=False
        ):
            env = setup_env(
                {
                    "steps": [],
                    "envFromHost": [
                        {"name": "OPTPILOT_TEST_MODEL", "default": "fallback/model"}
                    ],
                }
            )
        self.assertEqual(env["OPTPILOT_TEST_MODEL"], "chosen/model")

    def test_a_required_value_is_still_absent_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPTPILOT_TEST_REQUIRED", None)
            env = setup_env({"steps": [], "envFromHost": ["OPTPILOT_TEST_REQUIRED"]})
        self.assertNotIn("OPTPILOT_TEST_REQUIRED", env)


class SecretsTakeNoDefaultsTest(unittest.TestCase):
    def test_the_schema_keeps_secrets_a_plain_list(self) -> None:
        import json

        schema = json.loads(
            (_ROOT / "src/optpilot/schemas/defs/common.schema.json").read_text(
                encoding="utf-8"
            )
        )
        grants = schema["definitions"]["interfaceGrants"]["properties"]
        self.assertEqual(
            grants["secretsFromHost"]["$ref"], "#/definitions/stringList"
        )
        self.assertEqual(grants["envFromHost"]["$ref"], "#/definitions/hostEnvList")


class PublicSchemaBoundaryTest(unittest.TestCase):
    @staticmethod
    def _method(*, env_from_host: list[object]) -> dict:
        return {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": "schema-method",
            "entrypoint": {"python": "method:propose"},
            "accepts": {"formats": ["parameters"]},
            "runtime": {"envFromHost": env_from_host},
        }

    def test_method_runtime_accepts_plain_host_environment_names(self) -> None:
        result = validate_public_config_schema(
            self._method(env_from_host=["OPENROUTER_API_KEY"])
        )

        self.assertTrue(result.valid, result.to_dict())

    def test_method_runtime_rejects_a_default_bearing_declaration(self) -> None:
        result = validate_public_config_schema(
            self._method(
                env_from_host=[{"name": "MODEL", "default": "provider/model"}]
            )
        )

        self.assertFalse(result.valid)
        self.assertIn(
            "$.runtime.envFromHost[0]", {error.path for error in result.errors}
        )

    def test_interface_and_resource_action_grants_still_accept_defaults(self) -> None:
        defaulted = [{"name": "MODEL", "default": "provider/model"}]
        method = self._method(env_from_host=["OPENROUTER_API_KEY"])
        method["interface"] = {
            "command": ["python", "interface.py"],
            "presentation": {"kind": "web", "port": 8000},
            "grants": {"envFromHost": defaulted},
        }
        resource = {
            "apiVersion": "optpilot.io/v1",
            "config": "resource",
            "id": "schema-resource",
            "actions": [
                {
                    "id": "generate",
                    "label": "Generate",
                    "command": ["python", "generate.py"],
                    "grants": {"envFromHost": defaulted},
                    "timeoutSeconds": 60,
                }
            ],
        }

        for label, raw in (("interface", method), ("resource action", resource)):
            with self.subTest(label=label):
                result = validate_public_config_schema(raw)
                self.assertTrue(result.valid, result.to_dict())


class ShippedGeneratorTest(unittest.TestCase):
    """The package this was reported against asks for one thing now."""

    @unittest.skipUnless(
        (_ROOT / "catalog/devs_gallery").is_dir(), "needs the shipped packages"
    )
    def test_only_the_api_key_must_be_supplied(self) -> None:
        declared = yaml.safe_load(
            (
                _ROOT
                / "catalog/devs_gallery/resources/devs-gen-interface/optpilot.resource.yaml"
            ).read_text(encoding="utf-8")
        )
        for grants in (
            (declared.get("interface") or {}).get("grants") or {},
            (declared.get("actions") or [{}])[0].get("grants") or {},
        ):
            required = host_env_required_names(grants.get("envFromHost"))
            secrets = list(grants.get("secretsFromHost") or [])
            with self.subTest(secrets=secrets):
                self.assertEqual(required, [])
                self.assertEqual(secrets, ["OPENROUTER_API_KEY"])

    @unittest.skipUnless(
        (_ROOT / "catalog/devs_gallery").is_dir(), "needs the shipped packages"
    )
    def test_the_defaults_are_the_packages_own_documented_model(self) -> None:
        readme = (
            _ROOT
            / "catalog/devs_gallery/resources/devs-gen-interface/README.md"
        ).read_text(encoding="utf-8")
        declared = yaml.safe_load(
            (
                _ROOT
                / "catalog/devs_gallery/resources/devs-gen-interface/optpilot.resource.yaml"
            ).read_text(encoding="utf-8")
        )
        defaults = host_env_defaults(
            ((declared.get("interface") or {}).get("grants") or {}).get("envFromHost")
        )
        self.assertTrue(defaults)
        for value in defaults.values():
            self.assertIn(value, readme, "the default should be a model the package documents")


if __name__ == "__main__":
    unittest.main()
