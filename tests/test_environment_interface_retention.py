from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.config import (
    compile_authoring_config,
    compile_interface_launch_profiles,
    validate_authoring_config,
)


_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "catalog"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class EnvironmentInterfaceRetentionTest(unittest.TestCase):
    def test_example_resource_interfaces_use_the_target_profile_contract(self) -> None:
        resources = _REPOSITORY_ROOT / "catalog" / "example_package" / "resources"
        for path in resources.glob("*/optpilot.resource.yaml"):
            with self.subTest(path=path):
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                validation = validate_authoring_config(path)
                profiles = compile_interface_launch_profiles(
                    raw["interface"], component_kind="resource"
                )

                self.assertTrue(validation["valid"], validation)
                self.assertEqual(len(profiles), 1)
                self.assertEqual(profiles[0].profile_id, "default")
                self.assertTrue(profiles[0].outputs)
                self.assertEqual(profiles[0].accepts.selection_kinds, ("workspace",))
                self.assertEqual(profiles[0].grants.network, "enabled")
                self.assertEqual(
                    profiles[0].grants.env_from_host,
                    (
                        "DEVS_DISPLAY_MODEL_ID",
                        "DEVS_INTERFACE_MODEL_ID",
                        "DEVS_INTERFACE_STRONG_MODEL_ID",
                    ),
                )
                self.assertEqual(
                    profiles[0].grants.secrets_from_host,
                    ("OPENROUTER_API_KEY",),
                )

    def test_public_environment_interface_compiles_to_one_contextual_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment_dir = root / "environments"
            method_dir = root / "methods"
            study_dir = root / "studies"
            environment_dir.mkdir()
            method_dir.mkdir()
            study_dir.mkdir()

            environment = yaml.safe_load(
                (_FIXTURE_ROOT / "environments" / "toy_factory.yaml").read_text(
                    encoding="utf-8"
                )
            )
            environment["interface"] = {
                "label": "Toy Viewer",
                "description": "Inspect one frozen candidate.",
                "outputs": True,
                "command": ["python", "-m", "toy_viewer"],
                "cwd": "viewer",
                "env": {"VIEW_MODE": "inspect"},
                "runtime": {
                    "sandbox": "container",
                    "container": {
                        "engine": "podman",
                        "image": "toy/viewer:1",
                        "platform": "linux/amd64",
                    },
                },
                "grants": {
                    "network": "enabled",
                    "secretsFromHost": ["TOY_VIEW_TOKEN"],
                },
                "resources": {"cpu": 3, "memoryMiB": 3072, "gpus": 1},
                "timeoutSeconds": 1234,
                "presentation": {
                    "kind": "web",
                    "port": 5173,
                    "extraPorts": [5174],
                    "readyPath": "/ready",
                    "readyTimeoutSeconds": 12,
                },
                "accepts": {
                    "selectionKinds": ["candidate"],
                    "mediaTypes": ["application/vnd.optpilot.candidate+json"],
                },
            }
            (environment_dir / "toy_factory.yaml").write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )
            (method_dir / "reference_random_search.yaml").write_bytes(
                (
                    _FIXTURE_ROOT
                    / "methods"
                    / "reference_random_search.yaml"
                ).read_bytes()
            )
            (study_dir / "toy_random_search.yaml").write_bytes(
                (
                    _FIXTURE_ROOT / "studies" / "toy_random_search.yaml"
                ).read_bytes()
            )

            compiled = compile_authoring_config(
                study_dir / "toy_random_search.yaml"
            )
            profiles = compiled["environment"]["adapter"]["config"]["interfaces"]

            self.assertEqual(len(profiles), 1)
            profile = profiles[0]
            self.assertEqual(profile["id"], "default")
            self.assertEqual(profile["command"], ["python", "-m", "toy_viewer"])
            self.assertEqual(profile["cwd"], "viewer")
            self.assertTrue(profile["outputs"])
            self.assertEqual(
                profile["grants"],
                {
                    "envFromHost": [],
                    "network": "enabled",
                    "secretsFromHost": ["TOY_VIEW_TOKEN"],
                },
            )
            self.assertEqual(
                profile["runtime"],
                {
                    "sandbox": "container",
                    "container": {
                        "engine": "podman",
                        "image": "toy/viewer:1",
                        "platform": "linux/amd64",
                    },
                },
            )
            self.assertEqual(
                profile["resources"],
                {"cpu": 3, "memoryMiB": 3072, "gpus": 1},
            )
            self.assertEqual(profile["timeoutSeconds"], 1234)
            self.assertEqual(
                profile["presentation"],
                {
                    "kind": "web",
                    "port": 5173,
                    "extraPorts": [5174],
                    "readyPath": "/ready",
                    "readyTimeoutSeconds": 12,
                },
            )
            self.assertEqual(
                profile["accepts"],
                {
                    "selectionKinds": ["candidate"],
                    "mediaTypes": ["application/vnd.optpilot.candidate+json"],
                },
            )

    def test_output_action_allowlist_compiles_for_every_interface_kind(self) -> None:
        interface = {
            "outputs": {
                "actions": [
                    {
                        "id": "run",
                        "label": "Run simulation",
                        "command": ["python", "run.py"],
                        "timeoutSeconds": 45,
                    }
                ]
            },
            "command": ["python", "-m", "viewer"],
            "presentation": {"kind": "web", "port": 5173},
        }

        for component_kind in ("environment", "method", "resource"):
            with self.subTest(component_kind=component_kind):
                profile = compile_interface_launch_profiles(
                    interface,
                    component_kind=component_kind,
                )[0]
                self.assertTrue(profile.outputs)
                self.assertEqual(profile.output_actions[0].action_id, "run")
                self.assertEqual(
                    profile.to_dict()["outputs"],
                    {
                        "actions": [
                            {
                                "acceptsArguments": False,
                                "command": ["python", "run.py"],
                                "cwd": ".",
                                "id": "run",
                                "label": "Run simulation",
                                "runtime": "originating-interface",
                                "timeoutSeconds": 45,
                            }
                        ]
                    },
                )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment.yaml"
            environment = yaml.safe_load(
                (_FIXTURE_ROOT / "environments" / "toy_factory.yaml").read_text(
                    encoding="utf-8"
                )
            )
            environment["interface"] = interface
            path.write_text(
                yaml.safe_dump(environment, sort_keys=False),
                encoding="utf-8",
            )
            validation = validate_authoring_config(path)
            self.assertTrue(validation["valid"], validation)

    def test_named_launch_profiles_are_complete_independent_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment_dir = root / "environments"
            method_dir = root / "methods"
            study_dir = root / "studies"
            environment_dir.mkdir()
            method_dir.mkdir()
            study_dir.mkdir()
            environment = yaml.safe_load(
                (_FIXTURE_ROOT / "environments" / "toy_factory.yaml").read_text(
                    encoding="utf-8"
                )
            )
            environment["interface"] = {
                "launchProfiles": [
                    {
                        "id": "inspect",
                        "label": "Inspect",
                        "outputs": True,
                        "command": ["python", "-m", "toy_viewer"],
                        "presentation": {"kind": "web", "port": 5173},
                        "accepts": {"selectionKinds": ["candidate"]},
                    },
                    {
                        "id": "replay",
                        "command": ["python", "-m", "toy_replay"],
                        "runtime": {
                            "sandbox": "container",
                            "container": {
                                "build": {
                                    "context": "viewer",
                                    "dockerfile": "Containerfile",
                                    "tag": "toy/replay:local",
                                }
                            },
                        },
                        "grants": {"network": "disabled"},
                        "presentation": {
                            "kind": "web",
                            "port": 6173,
                            "readyPath": "/health",
                        },
                        "accepts": {"selectionKinds": ["trial"]},
                    },
                ]
            }
            (environment_dir / "toy_factory.yaml").write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )
            (method_dir / "reference_random_search.yaml").write_bytes(
                (
                    _FIXTURE_ROOT
                    / "methods"
                    / "reference_random_search.yaml"
                ).read_bytes()
            )
            (study_dir / "toy_random_search.yaml").write_bytes(
                (
                    _FIXTURE_ROOT / "studies" / "toy_random_search.yaml"
                ).read_bytes()
            )

            compiled = compile_authoring_config(
                study_dir / "toy_random_search.yaml"
            )
            profiles = compiled["environment"]["adapter"]["config"]["interfaces"]

            self.assertEqual([profile["id"] for profile in profiles], ["inspect", "replay"])
            self.assertTrue(profiles[0]["outputs"])
            self.assertNotIn("outputs", profiles[1])
            self.assertEqual(
                profiles[0]["grants"],
                {
                    "envFromHost": [],
                    "network": "disabled",
                    "secretsFromHost": [],
                },
            )
            self.assertEqual(
                profiles[0]["resources"],
                {"cpu": 1, "memoryMiB": 2048, "gpus": 0},
            )
            self.assertEqual(profiles[0]["timeoutSeconds"], 3600)
            self.assertEqual(profiles[0]["presentation"]["readyPath"], "/")
            self.assertEqual(
                profiles[1]["runtime"]["container"]["build"],
                {
                    "args": {},
                    "context": "viewer",
                    "dockerfile": "Containerfile",
                    "tag": "toy/replay:local",
                },
            )
            self.assertEqual(
                profiles[1]["runtime"]["container"]["engine"], "docker"
            )

    def test_old_flat_and_mixed_profile_grammars_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "environment.yaml"
            environment = yaml.safe_load(
                (_FIXTURE_ROOT / "environments" / "toy_factory.yaml").read_text(
                    encoding="utf-8"
                )
            )
            forbidden_runtime_values = {
                "env": {"TOKEN": "value"},
                "envFromHost": ["TOKEN"],
                "filesystem": {},
                "network": "enabled",
                "resources": {},
                "timeoutSeconds": 60,
            }
            invalid_interfaces = [
                {
                    "command": ["python", "-m", "toy_viewer"],
                    "outputs": False,
                    "presentation": {"kind": "web", "port": 5173},
                },
                {
                    "command": ["python", "-m", "toy_viewer"],
                    "port": 5173,
                },
                {
                    "command": ["python", "-m", "toy_viewer"],
                    "presentation": {"kind": "web", "port": 5173},
                    "launchProfiles": [],
                },
                {
                    "command": ["python", "-m", "toy_viewer"],
                    "runtime": {
                        "setup": {
                            "steps": [
                                {
                                    "uses": "command",
                                    "command": ["python", "-m", "pip", "--version"],
                                }
                            ],
                            "envFromHost": ["TOKEN"],
                        }
                    },
                    "presentation": {"kind": "web", "port": 5173},
                },
                *[
                    {
                        "command": ["python", "-m", "toy_viewer"],
                        "runtime": {forbidden: value},
                        "presentation": {"kind": "web", "port": 5173},
                    }
                    for forbidden, value in forbidden_runtime_values.items()
                ],
            ]
            for interface in invalid_interfaces:
                environment["interface"] = interface
                path.write_text(
                    yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
                )
                with self.subTest(interface=interface):
                    result = validate_authoring_config(path)
                    self.assertFalse(result["valid"])
                    self.assertIn("$.interface", " ".join(result["errors"]))

            profile = {
                "id": "duplicate",
                "command": ["python", "-m", "toy_viewer"],
                "presentation": {"kind": "web", "port": 5173},
            }
            environment["interface"] = {"launchProfiles": [profile, profile]}
            path.write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )
            result = validate_authoring_config(path)
            self.assertFalse(result["valid"])
            self.assertIn("ids must be unique", " ".join(result["errors"]))

            environment["interface"] = {
                "command": ["python", "-m", "toy_viewer"],
                "presentation": {"kind": "web", "port": 5173},
                "accepts": {"selectionKinds": ["workspace"]},
            }
            path.write_text(
                yaml.safe_dump(environment, sort_keys=False), encoding="utf-8"
            )
            result = validate_authoring_config(path)
            self.assertFalse(result["valid"])
            self.assertIn("unsupported selection kinds", " ".join(result["errors"]))


if __name__ == "__main__":
    unittest.main()
