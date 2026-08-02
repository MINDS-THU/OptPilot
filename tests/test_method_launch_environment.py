from __future__ import annotations

import json
import unittest

from optpilot.method_launch_environment import (
    MethodLaunchEnvironment,
    MethodLaunchEnvironmentDescriptor,
    MethodLaunchEnvironmentError,
    method_environment_names,
)
from optpilot.retained_study_compiler import compile_retained_process_study
from tests.test_retained_study_compiler import (
    _manifest,
    _package,
    _provider,
    _study,
)


def _definition(*names: str):
    study = _study()
    study.method["runtime"]["envFromHost"] = list(names)
    return compile_retained_process_study(
        study,
        package=_package(),
        package_manifest=_manifest(),
        provider=_provider(),
        target_owner_id="method-launch-environment-test",
    ).run_definition


class MethodLaunchEnvironmentTest(unittest.TestCase):
    def test_selects_only_declared_values_and_never_represents_values(self) -> None:
        definition = _definition("OPENROUTER_API_KEY", "OPTPILOT_LLM_MODEL")
        binding = MethodLaunchEnvironment.for_definition(
            definition,
            {
                "OPENROUTER_API_KEY": "private-api-key-value",
                "OPTPILOT_LLM_MODEL": "provider/model",
                "UNDECLARED_HOST_VALUE": "must-not-cross",
            },
            binding_revision="settings-revision-1",
        )

        self.assertEqual(
            method_environment_names(definition),
            ("OPENROUTER_API_KEY", "OPTPILOT_LLM_MODEL"),
        )
        self.assertEqual(
            binding.process_environment(),
            {
                "OPENROUTER_API_KEY": "private-api-key-value",
                "OPTPILOT_LLM_MODEL": "provider/model",
            },
        )
        representation = repr(binding)
        self.assertIn("OPENROUTER_API_KEY", representation)
        self.assertNotIn("private-api-key-value", representation)
        self.assertNotIn("UNDECLARED_HOST_VALUE", representation)
        self.assertIn("settings-revision-1", representation)
        with self.assertRaises(TypeError):
            json.dumps(binding)
        self.assertEqual(
            binding.descriptor.to_dict(),
            {
                "binding_revision": "settings-revision-1",
                "names": [
                    "OPENROUTER_API_KEY",
                    "OPTPILOT_LLM_MODEL",
                ],
            },
        )
        self.assertIsInstance(
            binding.descriptor, MethodLaunchEnvironmentDescriptor
        )

    def test_missing_values_report_names_without_disclosing_other_values(self) -> None:
        definition = _definition("OPENROUTER_API_KEY", "OPTPILOT_LLM_MODEL")

        with self.assertRaises(MethodLaunchEnvironmentError) as raised:
            MethodLaunchEnvironment.for_definition(
                definition,
                {"OPENROUTER_API_KEY": "private-api-key-value"},
                binding_revision="settings-revision-1",
            )

        self.assertEqual(raised.exception.code, "method_environment_missing")
        self.assertEqual(raised.exception.names, ("OPTPILOT_LLM_MODEL",))
        self.assertIn("OPTPILOT_LLM_MODEL", str(raised.exception))
        self.assertNotIn("private-api-key-value", str(raised.exception))

    def test_binding_is_scoped_to_the_exact_method_declaration(self) -> None:
        first = _definition("OPENROUTER_API_KEY")
        second = _definition("OTHER_API_KEY")
        binding = MethodLaunchEnvironment.for_definition(
            first,
            {"OPENROUTER_API_KEY": "value"},
            binding_revision="settings-revision-1",
        )

        self.assertTrue(binding.matches(first))
        self.assertFalse(binding.matches(second))

    def test_values_are_one_use_and_descriptor_remains_safe(self) -> None:
        definition = _definition("OPENROUTER_API_KEY")
        binding = MethodLaunchEnvironment.for_definition(
            definition,
            {"OPENROUTER_API_KEY": "one-use-secret"},
            binding_revision="settings-revision-1",
        )

        self.assertEqual(
            binding.take_process_environment(),
            {"OPENROUTER_API_KEY": "one-use-secret"},
        )
        self.assertFalse(binding.values_available)
        self.assertNotIn("one-use-secret", repr(binding))
        self.assertNotIn("one-use-secret", repr(binding.descriptor))
        with self.assertRaises(MethodLaunchEnvironmentError) as raised:
            binding.take_process_environment()
        self.assertEqual(
            raised.exception.code, "method_environment_values_unavailable"
        )


if __name__ == "__main__":
    unittest.main()
