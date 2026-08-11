from __future__ import annotations

import argparse
import io
import os
import unittest
from unittest.mock import patch

from optpilot_studio.cli import add_ui_subcommand
from optpilot_studio.ui.runtime_supervisor import StudioRuntimeSupervisorBusy
from optpilot_studio.ui.server import main as studio_main
from optpilot_studio.ui.server import _environment_preview_runtime_options


class StudioCliTest(unittest.TestCase):
    def test_preview_trust_source_is_exact_and_realm_is_the_default(self) -> None:
        image_a = "example/preview@sha256:" + "a" * 64
        image_b = "example/preview@sha256:" + "b" * 64
        env_name = "OPTPILOT_ENVIRONMENT_PREVIEW_TRUSTED_IMAGES"
        with (
            patch("optpilot_studio.ui.server.shutil.which", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {}, clear=True),
        ):
            _executable, realm_trust = _environment_preview_runtime_options(
                executable=None,
                trusted_images=None,
            )
            _executable, session_trust = _environment_preview_runtime_options(
                executable=None,
                trusted_images=[image_a],
            )
            _executable, disabled_trust = _environment_preview_runtime_options(
                executable=None,
                trusted_images=None,
                trust_source="disabled",
            )

        self.assertIsNone(realm_trust)
        self.assertEqual([item.image_ref for item in session_trust or ()], [image_a])
        self.assertEqual(disabled_trust, ())

        warning = io.StringIO()
        with (
            patch("optpilot_studio.ui.server.shutil.which", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {env_name: image_b}, clear=True),
            patch("sys.stderr", warning),
        ):
            _executable, environment_trust = _environment_preview_runtime_options(
                executable=None,
                trusted_images=None,
            )
            _executable, forced_realm = _environment_preview_runtime_options(
                executable=None,
                trusted_images=None,
                trust_source="realm",
            )

        self.assertEqual(
            [item.image_ref for item in environment_trust or ()],
            [image_b],
        )
        self.assertIsNone(forced_realm)
        self.assertIn("session-only trust override", warning.getvalue())

        with (
            patch("optpilot_studio.ui.server.shutil.which", return_value="/usr/bin/docker"),
            self.assertRaisesRegex(ValueError, "cannot be combined"),
        ):
            _environment_preview_runtime_options(
                executable="docker",
                trusted_images=[image_a],
                trust_source="realm",
            )

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
            "--environment-preview-trust-source",
            "session",
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
        self.assertEqual(
            core_run_ui.call_args.kwargs["environment_preview_trust_source"],
            "session",
        )


if __name__ == "__main__":
    unittest.main()
