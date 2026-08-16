"""The command-line surface for listing and deliberately deleting runs.

The confirmation rules matter more than the plumbing: deletion is always a
person's explicit act, so there is no --yes flag, a script gets a refusal, and
the confirmation is retyping the run id rather than pressing y.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot.cli import build_parser, main as cli_main


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class RunsCliTest(unittest.TestCase):
    def test_parser_exposes_list_and_delete(self) -> None:
        parser = build_parser()
        listed = parser.parse_args(["runs", "list"])
        deleted = parser.parse_args(
            ["runs", "delete", "run-1", "--realm-root", "/tmp/private-realm"]
        )
        self.assertEqual(listed.runs_command, "list")
        self.assertEqual(deleted.runs_command, "delete")
        self.assertEqual(deleted.run_id, "run-1")
        self.assertEqual(deleted.realm_root, "/tmp/private-realm")

    def test_delete_has_no_yes_flag(self) -> None:
        # A flag that skips the typed confirmation would let scripts delete
        # records; deletion must stay a person's explicit act.
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["runs", "delete", "run-1", "--yes"])

    def test_delete_refuses_without_a_terminal(self) -> None:
        stderr = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli_main(["runs", "delete", "run-1"])
        self.assertEqual(code, 2)
        self.assertIn("person", stderr.getvalue())

    def test_delete_stops_when_the_typed_id_differs(self) -> None:
        stdout = io.StringIO()
        with (
            patch("sys.stdin", _InteractiveInput("run-other\n")),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli_main(["runs", "delete", "run-1"])
        self.assertEqual(code, 1)
        self.assertIn("does not match", stdout.getvalue())

    def test_delete_reports_a_missing_run(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = str(Path(tmp_dir) / "realm")
            with (
                patch("sys.stdin", _InteractiveInput("run-missing\n")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = cli_main(
                    ["runs", "delete", "run-missing", "--realm-root", root]
                )
        self.assertEqual(code, 1)
        self.assertIn("No run named", stderr.getvalue())

    def test_list_reports_an_empty_archive(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = str(Path(tmp_dir) / "realm")
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["runs", "list", "--realm-root", root])
        self.assertEqual(code, 0)
        self.assertIn("No runs in the archive.", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
