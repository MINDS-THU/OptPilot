"""User-facing error reporting at the OptPilot command boundary."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from optpilot.cli import main
from optpilot.method_launch_environment import MethodLaunchEnvironmentError


class CliErrorReportingTests(unittest.TestCase):
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
