"""Named, typed, headless resource actions (F4)."""

from __future__ import annotations

import io
import json
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
        "timeoutSeconds": 60,
    }
    raw.update(overrides)
    return raw


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
                    grants={"envFromHost": ["DEMO_MODEL_ID"]},
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
            [_action(grants={"secretsFromHost": ["ABSENT_TOKEN"]})]
        )
        with self.assertRaisesRegex(ValueError, "ABSENT_TOKEN"):
            run_resource_action(
                resource,
                "generate",
                output_root=self.output_dir,
                host_env={"PATH": __import__("os").environ["PATH"]},
            )
        self.assertFalse(self.output_dir.exists())

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
        code, output = self._run(
            ["resource", "list", str(self.resource_path), "--json"]
        )
        self.assertEqual(code, 0)
        listing = json.loads(output)
        self.assertEqual(listing["resource_id"], "demo-generator")
        self.assertEqual(listing["actions"][0]["id"], "generate")
        self.assertIn("name", listing["actions"][0]["inputs"])

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
