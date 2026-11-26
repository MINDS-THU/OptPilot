"""Process setup steps for editable component copies."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot.setup import run_process_setup, setup_commands_for_step


class SetupCommandInterpreterTest(unittest.TestCase):
    """A python/python3 head is a logical name, not a host executable."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_python_head_maps_to_the_running_interpreter(self) -> None:
        for name in ("python", "python3"):
            with self.subTest(interpreter=name):
                commands = setup_commands_for_step(
                    {"uses": "command", "command": [name, "-c", "pass"]}, self.root
                )
                self.assertEqual(
                    commands, [[sys.executable, "-c", "pass"]]
                )

    def test_other_heads_and_later_arguments_are_left_verbatim(self) -> None:
        commands = setup_commands_for_step(
            {"uses": "command", "command": ["make", "build", "python"]}, self.root
        )
        self.assertEqual(commands, [["make", "build", "python"]])

    def test_python_venv_step_still_defaults_to_the_running_interpreter(self) -> None:
        commands = setup_commands_for_step({"uses": "python-venv"}, self.root)
        self.assertEqual(
            commands[0], [sys.executable, "-m", "venv", str(self.root / ".venv")]
        )

    def test_python_venv_step_honours_an_explicit_interpreter(self) -> None:
        commands = setup_commands_for_step(
            {"uses": "python-venv", "python": "/opt/pythons/3.11/bin/python"},
            self.root,
        )
        self.assertEqual(commands[0][0], "/opt/pythons/3.11/bin/python")


class RunProcessSetupInterpreterTest(unittest.TestCase):
    """The mapping must survive the scrubbed environment setup steps run in."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.empty_path = self.root / "empty-bin"
        self.empty_path.mkdir()

    def test_command_step_runs_on_a_host_without_python_on_path(self) -> None:
        setup = {
            "steps": [
                {
                    "uses": "command",
                    "command": [
                        "python",
                        "-c",
                        "import pathlib; pathlib.Path('marker.txt')"
                        ".write_text('ready')",
                    ],
                }
            ]
        }
        # Setup steps inherit only a minimal host env, so a bare "python" is
        # resolved against whatever PATH the host happens to have. Hosts that
        # ship only python3 (macOS with a uv-managed interpreter) used to fail
        # this step with FileNotFoundError.
        with mock.patch.dict(os.environ, {"PATH": str(self.empty_path)}):
            summary = run_process_setup(setup, self.root)

        self.assertTrue(summary["ran"])
        command = summary["steps"][0]["commands"][0]
        self.assertEqual(command["returncode"], 0)
        self.assertEqual(command["command"][0], sys.executable)
        self.assertEqual(
            (self.root / "marker.txt").read_text(encoding="utf-8"), "ready"
        )


if __name__ == "__main__":
    unittest.main()
