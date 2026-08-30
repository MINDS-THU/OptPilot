"""Named, typed, headless resource actions (F4)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from optpilot.cli import main as cli_main
from optpilot.config import validate_authoring_config
from optpilot.resource_actions import (
    compile_resource_actions,
    find_resource_action,
    run_resource_action,
)


def _resource(actions: list[dict] | None = None, **overrides) -> dict:
    raw: dict = {
        "apiVersion": "optpilot.io/v1",
        "config": "resource",
        "id": "demo-generator",
        "purpose": "generator",
    }
    if actions is not None:
        raw["actions"] = actions
    raw.update(overrides)
    return raw


def _action(**overrides) -> dict:
    raw: dict = {
        "id": "generate",
        "label": "Generate a bundle",
        "command": ["python", "generate.py"],
        # The direct-process executor cannot enforce network isolation. Tests
        # that execute a command opt into the host-network contract explicitly;
        # the dedicated fail-closed test below covers the safer default.
        "grants": {"network": "enabled"},
        "timeoutSeconds": 60,
    }
    raw.update(overrides)
    return raw


def _setup_runtime(*, cache: bool = False) -> dict:
    setup = {
        "timeoutSeconds": 321,
        "env": {"SETUP_MODE": "private-value"},
        "steps": [
            {
                "uses": "command",
                "cwd": "scripts",
                "command": ["sh", "-c", "printf done > marker"],
                "env": {"COMMAND_MODE": "hidden-command"},
            },
            {
                "uses": "uv",
                "cwd": "python",
                "extras": ["gpu"],
                "groups": ["dev"],
                "frozen": True,
                "env": {"UV_MODE": "hidden-uv"},
            },
            {
                "uses": "python-venv",
                "cwd": "worker",
                "python": "python3",
                "venv": ".venv-worker",
                "requirements": ["requirements.lock"],
                "installProject": True,
                "env": {"PIP_MODE": "hidden-pip"},
            },
            {
                "uses": "npm",
                "cwd": "web",
                "install": "install",
                "env": {"NPM_MODE": "hidden-npm"},
            },
        ],
    }
    if cache:
        setup["cache"] = "prepared"
    return {"sandbox": "process", "setup": setup}


class CompileResourceActionsTest(unittest.TestCase):
    def test_compiles_declared_actions_with_typed_inputs(self) -> None:
        actions = compile_resource_actions(
            _resource(
                [
                    _action(
                        command=[
                            "python",
                            "generate.py",
                            "--name",
                            "{input:name}",
                            "--inputs",
                            "{inputs_file}",
                            "--out",
                            "{output_root}",
                        ],
                        inputs={
                            "name": {"valueType": "string"},
                            "count": {"valueType": "int", "default": 2},
                        },
                        grants={"envFromHost": ["MODEL_ID"], "network": "enabled"},
                    )
                ]
            )
        )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action.action_id, "generate")
        self.assertEqual(action.input_placeholders(), ("name",))
        self.assertEqual(action.env_from_host, ("MODEL_ID",))
        self.assertEqual(action.network, "enabled")

    def test_compile_failures_are_specific(self) -> None:
        cases = (
            ("undeclared input", _action(command=["python", "{input:missing}"]), "undeclared input 'missing'"),
            (
                "non-scalar placeholder",
                _action(
                    command=["python", "{input:matrix}"],
                    inputs={
                        "matrix": {
                            "valueType": "array",
                            "items": {"valueType": "int"},
                        }
                    },
                ),
                "must be a scalar value type",
            ),
            (
                "unterminated placeholder",
                _action(command=["python", "{input:name"]),
                "unterminated",
            ),
            ("absolute cwd", _action(cwd="/etc"), "portable directory"),
            ("traversal cwd", _action(cwd="../outside"), "portable directory"),
            (
                "container runtime",
                _action(
                    runtime={
                        "sandbox": "container",
                        "container": {"image": "example:latest"},
                    }
                ),
                "container",
            ),
            (
                "timeout bounds",
                _action(timeoutSeconds=0),
                "timeoutSeconds",
            ),
        )
        for label, action, needle in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as raised:
                    compile_resource_actions(_resource([action]))
                self.assertIn(needle, str(raised.exception))

    def test_duplicate_action_ids_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            compile_resource_actions(_resource([_action(), _action()]))

    def test_resource_without_actions_compiles_to_empty(self) -> None:
        self.assertEqual(compile_resource_actions(_resource()), [])

    def test_network_defaults_to_disabled(self) -> None:
        action = _action()
        action.pop("grants")
        compiled = compile_resource_actions(_resource([action]))

        self.assertEqual(compiled[0].network, "disabled")

    def test_find_resource_action_names_known_ids(self) -> None:
        actions = compile_resource_actions(_resource([_action()]))
        self.assertEqual(
            find_resource_action(actions, "generate").action_id, "generate"
        )
        with self.assertRaisesRegex(ValueError, r"\['generate'\]"):
            find_resource_action(actions, "absent")


class ResourceActionConfigValidationTest(unittest.TestCase):
    def _write(self, raw: dict) -> Path:
        root = Path(tempfile.mkdtemp(prefix="optpilot-resource-config-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        path = root / "optpilot.resource.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return path

    def test_valid_actions_pass_public_validation(self) -> None:
        path = self._write(
            _resource(
                [
                    _action(
                        inputs={"name": {"valueType": "string", "default": "x"}}
                    )
                ]
            )
        )
        result = validate_authoring_config(path)
        self.assertTrue(result["valid"], result)

    def test_schema_rejects_unknown_action_fields(self) -> None:
        path = self._write(_resource([_action(unexpected=True)]))
        result = validate_authoring_config(path)
        self.assertFalse(result["valid"])

    def test_semantic_validation_rejects_bad_placeholder(self) -> None:
        path = self._write(_resource([_action(command=["python", "{input:x}"])]))
        result = validate_authoring_config(path)
        self.assertFalse(result["valid"])
        self.assertIn("undeclared input", " ".join(result["errors"]))

    def test_action_inputs_declaration_is_validated(self) -> None:
        path = self._write(
            _resource([_action(inputs={"bad": {"valueType": "mystery"}})])
        )
        result = validate_authoring_config(path)
        self.assertFalse(result["valid"])
        self.assertIn("valueType", " ".join(result["errors"]))


class RunResourceActionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resource_dir = self.root / "generator"
        self.resource_dir.mkdir()
        self.output_dir = self.root / "out"

    def _write_resource(self, actions: list[dict]) -> Path:
        path = self.resource_dir / "optpilot.resource.yaml"
        path.write_text(
            yaml.safe_dump(_resource(actions), sort_keys=False), encoding="utf-8"
        )
        return path

    def _write_script(self, source: str, name: str = "generate.py") -> None:
        (self.resource_dir / name).write_text(source, encoding="utf-8")

    def test_executes_with_substitution_env_contract_and_output_listing(self) -> None:
        self._write_script(
            """
import json, os, pathlib, sys
inputs = json.loads(
    pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"]).read_text()
)
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "nested").mkdir()
(out / "nested" / "bundle.json").write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "inputs": inputs,
            "model": os.environ.get("DEMO_MODEL_ID"),
            "leaked": "UNRELATED_SECRET" in os.environ,
            "extra_env": os.environ.get("DEMO_STATIC"),
            "cwd": os.getcwd(),
        }
    )
)
print("generated one bundle")
"""
        )
        resource = self._write_resource(
            [
                _action(
                    command=[
                        "python",
                        "generate.py",
                        "--name",
                        "{input:name}",
                        "--flag",
                        "{input:enabled}",
                    ],
                    env={"DEMO_STATIC": "static-value"},
                    inputs={
                        "name": {"valueType": "string"},
                        "enabled": {"valueType": "bool", "default": True},
                        "count": {"valueType": "int", "default": 3},
                    },
                    grants={
                        "envFromHost": ["DEMO_MODEL_ID"],
                        "network": "enabled",
                    },
                )
            ]
        )
        summary = run_resource_action(
            resource,
            "generate",
            input_values={"name": "Ada"},
            output_root=self.output_dir,
            host_env={
                "PATH": __import__("os").environ["PATH"],
                "DEMO_MODEL_ID": "demo-model",
                "UNRELATED_SECRET": "must-not-leak",
            },
        )

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["returncode"], 0)
        self.assertEqual(summary["resource_id"], "demo-generator")
        self.assertEqual(
            summary["inputs"], {"count": 3, "enabled": True, "name": "Ada"}
        )
        self.assertEqual(
            [item["path"] for item in summary["outputs"]], ["nested/bundle.json"]
        )
        self.assertIn("generated one bundle", summary["stdout_tail"])
        bundle = json.loads(
            (self.output_dir / "nested" / "bundle.json").read_text(encoding="utf-8")
        )
        self.assertEqual(bundle["argv"], ["--name", "Ada", "--flag", "true"])
        self.assertEqual(
            bundle["inputs"], {"count": 3, "enabled": True, "name": "Ada"}
        )
        self.assertEqual(bundle["model"], "demo-model")
        self.assertEqual(bundle["extra_env"], "static-value")
        self.assertFalse(bundle["leaked"])
        self.assertEqual(bundle["cwd"], str(self.resource_dir.resolve()))

    def test_input_validation_fails_closed(self) -> None:
        self._write_script("print('never runs')\n")
        resource = self._write_resource(
            [_action(inputs={"name": {"valueType": "string"}})]
        )
        with self.assertRaisesRegex(ValueError, "required"):
            run_resource_action(
                resource, "generate", input_values={}, output_root=self.output_dir
            )
        with self.assertRaisesRegex(ValueError, "not declared"):
            run_resource_action(
                resource,
                "generate",
                input_values={"name": "x", "mystery": 1},
                output_root=self.output_dir,
            )
        self.assertFalse(self.output_dir.exists())

    def test_missing_host_environment_fails_before_execution(self) -> None:
        self._write_script("print('never runs')\n")
        resource = self._write_resource(
            [
                _action(
                    grants={
                        "network": "enabled",
                        "secretsFromHost": ["ABSENT_TOKEN"],
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "ABSENT_TOKEN"):
            run_resource_action(
                resource,
                "generate",
                output_root=self.output_dir,
                host_env={"PATH": __import__("os").environ["PATH"]},
            )
        self.assertFalse(self.output_dir.exists())

    def test_network_disabled_fails_closed_before_execution(self) -> None:
        self._write_script("print('never runs')\n")
        action = _action()
        action["grants"] = {"network": "disabled"}
        resource = self._write_resource([action])

        with self.assertRaisesRegex(ValueError, "cannot enforce network isolation"):
            run_resource_action(
                resource,
                "generate",
                output_root=self.output_dir,
            )

        self.assertFalse(self.output_dir.exists())

    def test_injected_secret_is_redacted_from_returned_logs(self) -> None:
        self._write_script(
            "import os, sys\n"
            "value = os.environ['ACTION_SECRET']\n"
            "print('stdout=' + value)\n"
            "print('stderr=' + value, file=sys.stderr)\n"
        )
        resource = self._write_resource(
            [
                _action(
                    grants={
                        "network": "enabled",
                        "secretsFromHost": ["ACTION_SECRET"],
                    }
                )
            ]
        )

        summary = run_resource_action(
            resource,
            "generate",
            output_root=self.output_dir,
            host_env={
                "PATH": os.environ["PATH"],
                "ACTION_SECRET": "sk-resource-action-audit",
            },
        )

        encoded = json.dumps(summary, sort_keys=True)
        self.assertNotIn("sk-resource-action-audit", encoded)
        self.assertIn("[REDACTED]", summary["stdout_tail"])
        self.assertIn("[REDACTED]", summary["stderr_tail"])

    def test_nonzero_exit_and_timeout_are_reported(self) -> None:
        self._write_script(
            "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"
        )
        resource = self._write_resource([_action()])
        failure = run_resource_action(
            resource, "generate", output_root=self.output_dir
        )
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["returncode"], 3)
        self.assertIn("boom", failure["stderr_tail"])

        self._write_script("import time\ntime.sleep(30)\n")
        slow_output = self.root / "slow-out"
        resource = self._write_resource([_action(timeoutSeconds=1)])
        timeout = run_resource_action(
            resource, "generate", output_root=slow_output
        )
        self.assertFalse(timeout["ok"])
        self.assertTrue(timeout["timed_out"])
        self.assertIn("timed out", timeout["error"])

    def test_output_root_must_be_fresh(self) -> None:
        self._write_script("print('ok')\n")
        resource = self._write_resource([_action()])
        self.output_dir.mkdir()
        (self.output_dir / "existing.txt").write_text("old", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not empty"):
            run_resource_action(resource, "generate", output_root=self.output_dir)

    def test_runtime_setup_runs_before_the_command_unless_skipped(self) -> None:
        self._write_script(
            """
import os, pathlib, sys
marker = pathlib.Path("setup-marker.txt")
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "result.txt").write_text("setup-ran" if marker.exists() else "no-setup")
"""
        )
        action = _action(
            runtime={
                "sandbox": "process",
                "setup": {
                    "steps": [
                        {
                            "uses": "command",
                            "command": [
                                "python",
                                "-c",
                                "import pathlib; pathlib.Path('setup-marker.txt')"
                                ".write_text('ready')",
                            ],
                        }
                    ]
                },
            }
        )
        resource = self._write_resource([action])
        summary = run_resource_action(
            resource, "generate", output_root=self.output_dir
        )
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["setup"], {"ran": True})
        self.assertEqual(
            (self.output_dir / "result.txt").read_text(encoding="utf-8"),
            "setup-ran",
        )

        (self.resource_dir / "setup-marker.txt").unlink()
        skipped_output = self.root / "skipped-out"
        skipped = run_resource_action(
            resource,
            "generate",
            output_root=skipped_output,
            run_setup=False,
        )
        self.assertTrue(skipped["ok"], skipped)
        self.assertNotIn("setup", skipped)
        self.assertEqual(
            (skipped_output / "result.txt").read_text(encoding="utf-8"),
            "no-setup",
        )


class DeclaredPythonRuntimeTest(unittest.TestCase):
    """An action that declares a Python runtime imports what it declares."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resource_dir = self.root / "generator"
        self.resource_dir.mkdir()
        self.output_dir = self.root / "out"
        self.venv_dir = (self.resource_dir / ".runtime" / "action-venv").resolve()
        self.action = _action(
            command=["python", "generate.py"],
            timeoutSeconds=600,
            runtime={
                "sandbox": "process",
                "setup": {
                    "timeoutSeconds": 600,
                    "steps": [
                        {
                            "uses": "python-venv",
                            "venv": ".runtime/action-venv",
                        }
                    ],
                },
            },
        )
        (self.resource_dir / "optpilot.resource.yaml").write_text(
            yaml.safe_dump(_resource([self.action]), sort_keys=False),
            encoding="utf-8",
        )
        (self.resource_dir / "generate.py").write_text(
            """
import json, os, pathlib, sys
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "runtime.json").write_text(
    json.dumps({"executable": sys.executable, "prefix": sys.prefix,
                "path": os.environ.get("PATH", "")})
)
""",
            encoding="utf-8",
        )
        self.resource_path = self.resource_dir / "optpilot.resource.yaml"

    def test_declared_runtime_owns_the_python_command_head(self) -> None:
        summary = run_resource_action(
            self.resource_path, "generate", output_root=self.output_dir
        )

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["setup"], {"ran": True})
        # The command runs on the declared interpreter, not on the one running
        # optpilot — otherwise the declaration would be decorative and the
        # action would silently import host site-packages.
        self.assertNotEqual(summary["command"][0], sys.executable)
        # Compared unresolved: a venv's bin/python is a symlink to the base
        # interpreter, so resolving it would assert about the wrong thing.
        self.assertEqual(Path(summary["command"][0]).parent.parent, self.venv_dir)
        observed = json.loads(
            (self.output_dir / "runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(Path(observed["prefix"]), self.venv_dir)
        self.assertNotEqual(Path(observed["prefix"]), Path(sys.prefix))
        self.assertIn(
            str(Path(summary["command"][0]).parent),
            observed["path"].split(os.pathsep),
        )

    def test_missing_declared_runtime_fails_closed_before_any_output(self) -> None:
        with self.assertRaises(ValueError) as raised:
            run_resource_action(
                self.resource_path,
                "generate",
                output_root=self.output_dir,
                run_setup=False,
            )

        message = str(raised.exception)
        self.assertIn("--skip-setup", message)
        self.assertIn(str(self.venv_dir), message)
        self.assertFalse(self.output_dir.exists())


class ResourceCliTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resource_dir = self.root / "generator"
        self.resource_dir.mkdir()
        (self.resource_dir / "generate.py").write_text(
            """
import os, pathlib
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "bundle.txt").write_text("done")
""",
            encoding="utf-8",
        )
        self.resource_path = self.resource_dir / "optpilot.resource.yaml"
        self.resource_path.write_text(
            yaml.safe_dump(
                _resource(
                    [
                        _action(
                            inputs={
                                "name": {"valueType": "string", "default": "x"}
                            }
                        )
                    ]
                ),
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli_main(argv)
        return code, stream.getvalue()

    def test_resource_list_json(self) -> None:
        self.resource_path.write_text(
            yaml.safe_dump(
                _resource(
                    [
                        _action(
                            command=[
                                "python",
                                "generate.py",
                                "--name",
                                "{input:name}",
                            ],
                            inputs={"name": {"valueType": "string"}},
                            grants={
                                "envFromHost": [
                                    "REQUIRED_MODEL",
                                    {"name": "OPTIONAL_PROFILE", "default": "local"},
                                ],
                                "secretsFromHost": ["PROVIDER_TOKEN"],
                                "network": "enabled",
                            },
                            runtime=_setup_runtime(cache=True),
                            timeoutSeconds=45,
                        )
                    ]
                ),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        code, output = self._run(
            ["resource", "list", str(self.resource_path), "--json"]
        )
        self.assertEqual(code, 0)
        listing = json.loads(output)
        self.assertEqual(listing["resource_id"], "demo-generator")
        action = listing["actions"][0]
        self.assertEqual(action["id"], "generate")
        self.assertIn("name", action["inputs"])
        self.assertEqual(
            action["command"],
            ["python", "generate.py", "--name", "{input:name}"],
        )
        self.assertEqual(action["network"], "enabled")
        self.assertEqual(action["timeout_seconds"], 45)
        self.assertEqual(action["requires_env_from_host"], ["REQUIRED_MODEL"])
        self.assertEqual(
            action["defaulted_env_from_host"], {"OPTIONAL_PROFILE": "local"}
        )
        self.assertEqual(
            action["requires_secrets_from_host"], ["PROVIDER_TOKEN"]
        )
        self.assertEqual(
            action["runtime_setup"],
            {
                "cache": "prepared",
                "timeout_seconds": 321,
                "environment_names": ["SETUP_MODE"],
                "steps": [
                    {
                        "uses": "command",
                        "cwd": "scripts",
                        "command": ["sh", "-c", "printf done > marker"],
                        "environment_names": ["COMMAND_MODE"],
                    },
                    {
                        "uses": "uv",
                        "cwd": "python",
                        "extras": ["gpu"],
                        "groups": ["dev"],
                        "frozen": True,
                        "environment_names": ["UV_MODE"],
                    },
                    {
                        "uses": "python-venv",
                        "cwd": "worker",
                        "python": "python3",
                        "venv": ".venv-worker",
                        "requirements": ["requirements.lock"],
                        "installProject": True,
                        "environment_names": ["PIP_MODE"],
                    },
                    {
                        "uses": "npm",
                        "cwd": "web",
                        "install": "install",
                        "environment_names": ["NPM_MODE"],
                    },
                ],
            },
        )
        self.assertNotIn("private-value", output)
        self.assertNotIn("hidden-command", output)
        self.assertNotIn("hidden-uv", output)
        self.assertNotIn("hidden-pip", output)
        self.assertNotIn("hidden-npm", output)
        # Compatibility projections retain the original authored-name fields.
        self.assertEqual(
            action["envFromHost"], ["REQUIRED_MODEL", "OPTIONAL_PROFILE"]
        )
        self.assertEqual(action["secretsFromHost"], ["PROVIDER_TOKEN"])
        self.assertEqual(action["timeoutSeconds"], 45)

    def test_resource_list_text_discloses_execution_contract(self) -> None:
        self.resource_path.write_text(
            yaml.safe_dump(
                _resource(
                    [
                        _action(
                            command=["python", "generate.py", "--mode", "dry run"],
                            grants={
                                "envFromHost": [
                                    "REQUIRED_MODEL",
                                    {"name": "OPTIONAL_PROFILE", "default": "local"},
                                ],
                                "secretsFromHost": ["PROVIDER_TOKEN"],
                                "network": "enabled",
                            },
                            runtime=_setup_runtime(),
                            timeoutSeconds=45,
                        )
                    ]
                ),
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        code, output = self._run(["resource", "list", str(self.resource_path)])

        self.assertEqual(code, 0)
        self.assertIn(
            'command: ["python", "generate.py", "--mode", "dry run"]', output
        )
        self.assertIn("network: enabled", output)
        self.assertIn("timeout: 45s", output)
        self.assertIn("runtime setup: timeout=321s", output)
        self.assertIn("setup environment names: SETUP_MODE", output)
        self.assertIn(
            'setup step 1: command argv=["sh", "-c", '
            '"printf done > marker"] options={"cwd": "scripts"}',
            output,
        )
        self.assertIn("environment names: COMMAND_MODE", output)
        self.assertIn(
            'setup step 2: uv options={"cwd": "python", "extras": ["gpu"], '
            '"frozen": true, "groups": ["dev"]}',
            output,
        )
        self.assertIn(
            'setup step 3: python-venv options={"cwd": "worker", '
            '"installProject": true, "python": "python3", '
            '"requirements": ["requirements.lock"], "venv": ".venv-worker"}',
            output,
        )
        self.assertIn(
            'setup step 4: npm options={"cwd": "web", "install": "install"}',
            output,
        )
        self.assertIn("required host env: REQUIRED_MODEL", output)
        self.assertIn('defaulted host env: {"OPTIONAL_PROFILE": "local"}', output)
        self.assertIn("required host secrets: PROVIDER_TOKEN", output)
        self.assertNotIn("private-value", output)
        self.assertNotIn("hidden-command", output)
        self.assertNotIn("hidden-uv", output)
        self.assertNotIn("hidden-pip", output)
        self.assertNotIn("hidden-npm", output)

    def test_resource_list_reports_effective_default_setup_timeout(self) -> None:
        runtime = _setup_runtime()
        runtime["setup"].pop("timeoutSeconds")
        self.resource_path.write_text(
            yaml.safe_dump(
                _resource([_action(runtime=runtime)]),
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        code, output = self._run(
            ["resource", "list", str(self.resource_path), "--json"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output)["actions"][0]["runtime_setup"]["timeout_seconds"],
            600,
        )

    def test_resource_run_json_and_failure_exit_code(self) -> None:
        output_dir = self.root / "out"
        code, output = self._run(
            [
                "resource",
                "run",
                str(self.resource_path),
                "generate",
                "--input",
                "name=Ada",
                "--output-dir",
                str(output_dir),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads(output)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["inputs"]["name"], "Ada")
        self.assertEqual(
            (output_dir / "bundle.txt").read_text(encoding="utf-8"), "done"
        )

        code, _output = self._run(
            [
                "resource",
                "run",
                str(self.resource_path),
                "absent",
                "--output-dir",
                str(self.root / "out2"),
            ]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
