from __future__ import annotations

import json
import os
import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import optpilot.realm.process_provider as process_provider
from optpilot.realm.process_provider import (
    ProcessProviderIdentity,
    build_process_provider_identity,
    current_process_provider_identity,
)


def _facts() -> dict[str, str]:
    return {
        "os_name": "posix",
        "platform_machine": "arm64",
        "platform_release": "24.5.0",
        "platform_system": "Darwin",
        "python_cache_tag": "cpython-312",
        "python_implementation": "cpython",
        "python_version": "3.12.10",
        "sys_platform": "darwin",
    }


class ProcessProviderIdentityTest(unittest.TestCase):
    def test_builder_is_deterministic_and_canonicalizes_the_distribution_lock(self) -> None:
        first = build_process_provider_identity(
            _facts(), (("Zope.Interface", "7.2"), ("Alpha_pkg", "1.0"))
        )
        second = build_process_provider_identity(
            _facts(), (("alpha-pkg", "1.0"), ("zope-interface", "7.2"))
        )

        self.assertEqual(first, second)
        self.assertEqual(first.platform, "darwin-arm64")
        self.assertRegex(first.builder_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(
            first.to_dict(),
            {
                "builder_fingerprint": first.builder_fingerprint,
                "platform": "darwin-arm64",
            },
        )

    def test_every_runtime_fact_and_distribution_version_affects_the_fingerprint(self) -> None:
        baseline = build_process_provider_identity(_facts(), (("alpha", "1.0"),))
        replacements = {
            "os_name": "nt",
            "platform_machine": "x86_64",
            "platform_release": "24.6.0",
            "platform_system": "Linux",
            "python_cache_tag": "cpython-313",
            "python_implementation": "pypy",
            "python_version": "3.12.11",
            "sys_platform": "linux",
        }
        for key, value in replacements.items():
            changed = dict(_facts())
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(
                    build_process_provider_identity(
                        changed, (("alpha", "1.0"),)
                    ).builder_fingerprint,
                    baseline.builder_fingerprint,
                )
        self.assertNotEqual(
            build_process_provider_identity(
                _facts(), (("alpha", "1.1"),)
            ).builder_fingerprint,
            baseline.builder_fingerprint,
        )

    def test_identity_is_frozen_and_rejects_noncanonical_or_path_like_values(self) -> None:
        identity = ProcessProviderIdentity("a" * 64, "linux-x86_64")
        with self.assertRaises(FrozenInstanceError):
            identity.platform = "darwin-arm64"  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "64-character"):
            ProcessProviderIdentity("A" * 64, "linux-x86_64")
        with self.assertRaisesRegex(ValueError, "path-free"):
            ProcessProviderIdentity("a" * 64, "/tmp/provider")

    def test_runtime_facts_are_exact_bounded_and_path_free(self) -> None:
        for mutation in (
            lambda facts: facts.pop("python_cache_tag"),
            lambda facts: facts.update({"sys_executable": "/secret/python"}),
        ):
            facts = _facts()
            mutation(facts)
            with self.assertRaisesRegex(ValueError, "fields differ"):
                build_process_provider_identity(facts, ())

        facts = _facts()
        facts["platform_release"] = "/private/kernel"
        with self.assertRaisesRegex(ValueError, "path-free"):
            build_process_provider_identity(facts, ())

        facts = _facts()
        facts["platform_release"] = "x" * 257
        with self.assertRaisesRegex(ValueError, "exceeds 256"):
            build_process_provider_identity(facts, ())

        with self.assertRaises(TypeError):
            build_process_provider_identity([], ())  # type: ignore[arg-type]

    def test_distribution_lock_rejects_duplicates_invalid_records_and_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate name"):
            build_process_provider_identity(
                _facts(), (("My_Pkg", "1.0"), ("my-pkg", "1.0"))
            )
        for distributions in (
            (("../secret", "1.0"),),
            (("alpha", "/tmp/version"),),
        ):
            with self.subTest(distributions=distributions), self.assertRaisesRegex(
                ValueError, "invalid|path-free"
            ):
                build_process_provider_identity(_facts(), distributions)
        with self.assertRaises(TypeError):
            build_process_provider_identity(_facts(), ("not-a-pair",))
        with patch.object(process_provider, "MAX_PROCESS_PROVIDER_DISTRIBUTIONS", 1):
            with self.assertRaisesRegex(ValueError, "maximum count"):
                build_process_provider_identity(
                    _facts(), (("alpha", "1"), ("beta", "1"))
                )
        with patch.object(process_provider, "MAX_PROCESS_PROVIDER_INPUT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "maximum encoded size"):
                build_process_provider_identity(_facts(), ())

    def test_current_identity_does_not_depend_on_executable_or_environment_values(self) -> None:
        with patch.object(sys, "executable", "/private/secret/python"), patch.dict(
            os.environ, {"OPTPILOT_SECRET": "first-secret"}
        ):
            first = current_process_provider_identity()
        with patch.object(sys, "executable", "/other/private/python"), patch.dict(
            os.environ, {"OPTPILOT_SECRET": "second-secret"}
        ):
            second = current_process_provider_identity()

        self.assertEqual(first, second)
        encoded = json.dumps(first.to_dict(), sort_keys=True)
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret", encoded)
        self.assertRegex(first.builder_fingerprint, re.compile(r"^[0-9a-f]{64}$"))


if __name__ == "__main__":
    unittest.main()
