from __future__ import annotations

import argparse
import io
import unittest
from unittest.mock import patch

from optpilot_studio.cli import add_ui_subcommand
from optpilot_studio.ui.runtime_supervisor import StudioRuntimeSupervisorBusy
from optpilot_studio.ui.server import main as studio_main


class StudioCliTest(unittest.TestCase):
    def test_busy_runtime_supervisor_is_a_plain_cli_error(self) -> None:
        parser = argparse.ArgumentParser(prog="optpilot")
        subparsers = parser.add_subparsers(dest="command", required=True)
        add_ui_subcommand(subparsers)
        options = parser.parse_args(["ui"])
        error = StudioRuntimeSupervisorBusy("another Studio owns this project")

        core_stderr = io.StringIO()
        with (
            patch("optpilot_studio.cli.run_ui", side_effect=error),
            patch("sys.stderr", core_stderr),
        ):
            core_exit = options.handler(options)

        standalone_stderr = io.StringIO()
        with (
            patch("optpilot_studio.ui.server.run_ui", side_effect=error),
            patch("sys.stderr", standalone_stderr),
        ):
            standalone_exit = studio_main([])

        self.assertEqual(core_exit, 2)
        self.assertEqual(standalone_exit, 2)
        self.assertIn("another Studio owns this project", core_stderr.getvalue())
        self.assertIn(
            "another Studio owns this project", standalone_stderr.getvalue()
        )

    def test_core_and_standalone_entrypoints_forward_identical_preview_options(
        self,
    ) -> None:
        preview_args = [
            "--environment-preview-container-bin",
            "podman",
            "--environment-preview-trusted-image",
            "example/preview@sha256:" + "a" * 64,
            "--environment-preview-trusted-image",
            "example/preview@sha256:" + "b" * 64,
        ]

        core_parser = argparse.ArgumentParser(prog="optpilot")
        subparsers = core_parser.add_subparsers(dest="command", required=True)
        add_ui_subcommand(subparsers)
        core_options = core_parser.parse_args(["ui", *preview_args])
        with patch("optpilot_studio.cli.run_ui") as core_run_ui:
            core_exit_code = core_options.handler(core_options)

        with patch("optpilot_studio.ui.server.run_ui") as standalone_run_ui:
            standalone_exit_code = studio_main(preview_args)

        self.assertEqual(core_exit_code, 0)
        self.assertEqual(standalone_exit_code, 0)
        core_run_ui.assert_called_once()
        standalone_run_ui.assert_called_once()
        self.assertEqual(
            core_run_ui.call_args.kwargs,
            standalone_run_ui.call_args.kwargs,
        )
        self.assertEqual(
            core_run_ui.call_args.kwargs[
                "environment_preview_container_executable"
            ],
            "podman",
        )
        self.assertEqual(
            core_run_ui.call_args.kwargs["environment_preview_trusted_images"],
            [
                "example/preview@sha256:" + "a" * 64,
                "example/preview@sha256:" + "b" * 64,
            ],
        )


if __name__ == "__main__":
    unittest.main()
