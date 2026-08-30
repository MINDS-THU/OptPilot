"""User-facing error reporting at the OptPilot command boundary."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from optpilot.cli import build_parser, main
from optpilot.method_launch_environment import MethodLaunchEnvironmentError


class CliErrorReportingTests(unittest.TestCase):
    def _help_for(self, *arguments: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args([*arguments, "--help"])
        self.assertEqual(caught.exception.code, 0)
        return stdout.getvalue()

    def test_validate_help_names_every_public_config_kind(self) -> None:
        output = self._help_for("validate")
        for kind in ("environment", "method", "study", "resource"):
            self.assertIn(kind, output)

    def test_import_check_help_discloses_host_code_execution(self) -> None:
        output = self._help_for("package", "validate")
        self.assertIn("host child process", output)
        self.assertIn("executes package code", output)
        self.assertIn("not a sandbox", output)

    def test_expected_run_error_is_concise_and_nonzero(self) -> None:
        stderr = io.StringIO()
        with mock.patch(
            "optpilot.cli.run_study",
            side_effect=MethodLaunchEnvironmentError(
                "method_environment_missing",
                "Missing Method environment variable: EXAMPLE_API_KEY."
            ),
        ), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "run",
                    "study.yaml",
                    "--package-root",
                    "package",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "Error: Missing Method environment variable: EXAMPLE_API_KEY.\n",
        )
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
