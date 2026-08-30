"""A component may raise its containers' resource limits (design §Limits).

The chain has five hops -- authoring validation, compile, the retained
declaration, the contract gates, and the launch merge -- and each is asserted
separately so losing one is a visible failure, not a silently ignored raise.
A component that raises nothing must compile byte-identically to one written
before limits existed; that stability is what keeps existing digests intact.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from optpilot.config import (
    _compile_environment_runtime,
    _compile_method_runtime,
    _validate_container_limits,
    _validate_runtime,
)
from optpilot.container_launch import ContainerLimits

IMAGE = "ghcr.io/example/pkg@sha256:" + "a" * 64


def _container_runtime(limits=None):
    container = {"image": IMAGE, "platform": "linux/amd64"}
    if limits is not None:
        container["limits"] = limits
    return {"sandbox": "container", "container": container}


class AuthoringValidationTest(unittest.TestCase):
    def test_a_raised_limit_is_accepted(self) -> None:
        _validate_runtime(
            _container_runtime({"cpus": "8", "memory": "16g", "pids": 2048}),
            "environment.runtime",
            component_kind="environment",
        )

    def test_each_malformed_shape_is_refused(self) -> None:
        for limits in (
            {"cpus": "eight"},
            {"memory": "lots"},
            {"pids": 0},
            {"pids": True},
            {"gpu": "1"},
            "8g",
        ):
            with self.subTest(limits=limits):
                with self.assertRaises(ValueError):
                    _validate_container_limits(limits, "environment.runtime")

    def test_memory_accepts_both_common_spellings(self) -> None:
        for memory in ("8g", "8G", "512m", "8GiB", "2.5g"):
            with self.subTest(memory=memory):
                _validate_container_limits({"memory": memory}, "x")


class CompileShapeTest(unittest.TestCase):
    def test_an_unraised_component_compiles_exactly_as_before(self) -> None:
        compiled = _compile_environment_runtime(_container_runtime(), Path("."))
        self.assertNotIn("limits", compiled["container"])
        self.assertEqual(
            set(compiled["container"]), {"image", "platform", "network"}
        )

    def test_raised_limits_ride_the_compiled_container(self) -> None:
        compiled = _compile_environment_runtime(
            _container_runtime({"memory": "16g"}), Path(".")
        )
        self.assertEqual(compiled["container"]["limits"], {"memory": "16g"})
        method = _compile_method_runtime(
            _container_runtime({"cpus": "8", "pids": 2048}), Path(".")
        )
        self.assertEqual(
            method["container"]["limits"], {"cpus": "8", "pids": 2048}
        )


class RetainedDeclarationTest(unittest.TestCase):
    def test_the_declaration_carries_validated_limits(self) -> None:
        from optpilot.retained_study_compiler import (
            _environment_container_declaration,
        )

        environment = {
            "runtime": {
                "type": "container",
                "container": {
                    "image": IMAGE,
                    "platform": "linux/amd64",
                    "limits": {"memory": "16g"},
                },
            }
        }
        image, platform, network, limits = _environment_container_declaration(
            environment
        )
        self.assertEqual(limits, {"memory": "16g"})

    def test_a_malformed_retained_limit_fails_closed(self) -> None:
        from optpilot.retained_study_compiler import (
            RetainedStudyCompileError,
            _environment_container_declaration,
        )

        environment = {
            "runtime": {
                "type": "container",
                "container": {
                    "image": IMAGE,
                    "platform": "linux/amd64",
                    "limits": {"memory": "unbounded"},
                },
            }
        }
        with self.assertRaises(RetainedStudyCompileError):
            _environment_container_declaration(environment)


class ContractGateTest(unittest.TestCase):
    def test_the_rederived_requirement_includes_a_raise(self) -> None:
        from optpilot.runtime_binding import (
            _expected_container_runtime_requirements,
        )

        settings = {
            "container_image_reference": IMAGE,
            "container_platform": "linux/amd64",
            "container_network": "disabled",
            "container_limits": {"pids": 2048},
        }
        derived = _expected_container_runtime_requirements(settings)
        self.assertEqual(derived["container"]["limits"], {"pids": 2048})
        del settings["container_limits"]
        self.assertNotIn(
            "limits", _expected_container_runtime_requirements(settings)["container"]
        )


class LaunchMergeTest(unittest.TestCase):
    def test_the_attempt_launcher_merges_raises_over_defaults(self) -> None:
        from optpilot.realm.local_attempt_launcher import (
            RealmLocalAttemptLauncher,
        )

        plan = SimpleNamespace(limits=(("memory", "16g"),))
        merged = RealmLocalAttemptLauncher._container_limits_from_plan(plan)
        self.assertEqual(merged.memory, "16g")
        self.assertEqual(merged.cpus, ContainerLimits().cpus)
        self.assertEqual(merged.pids, ContainerLimits().pids)

    def test_the_method_worker_merges_raises_over_defaults(self) -> None:
        from optpilot.retained_batch_runtime import _method_container_limits

        merged = _method_container_limits({"cpus": "8", "pids": 2048})
        self.assertEqual(merged.cpus, "8")
        self.assertEqual(merged.pids, 2048)
        self.assertEqual(merged.memory, ContainerLimits().memory)


class ApprovalDisplayTest(unittest.TestCase):
    def test_the_approval_prompt_names_a_component_that_raises(self) -> None:
        from optpilot.cli import _raised_limits_for_image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "catalog" / "pkg" / "environments" / "environment.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "id: heavy-sim\n"
                "runtime:\n"
                "  container:\n"
                f"    image: {IMAGE}\n"
                "    platform: linux/amd64\n"
                "    limits:\n"
                "      memory: 16g\n",
                encoding="utf-8",
            )
            lines = _raised_limits_for_image(root, IMAGE)
        self.assertEqual(len(lines), 1)
        self.assertIn("heavy-sim", lines[0])
        self.assertIn("memory 16g", lines[0])

    def test_no_catalog_means_no_lines(self) -> None:
        from optpilot.cli import _raised_limits_for_image

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_raised_limits_for_image(Path(tmp), IMAGE), [])


if __name__ == "__main__":
    unittest.main()
