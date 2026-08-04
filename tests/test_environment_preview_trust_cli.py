from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot.cli import build_parser, main as cli_main


_IMAGE = "example.invalid/optpilot-preview@sha256:" + ("a" * 64)


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class EnvironmentPreviewTrustCliTest(unittest.TestCase):
    def test_parser_exposes_approve_revoke_and_list(self) -> None:
        parser = build_parser()

        approve = parser.parse_args(
            [
                "environment-preview",
                "trust",
                "approve",
                _IMAGE,
                "--realm-root",
                "/tmp/private-realm",
                "--yes",
                "--json",
            ]
        )
        revoke = parser.parse_args(
            ["environment-preview", "trust", "revoke", _IMAGE, "--yes"]
        )
        listed = parser.parse_args(
            ["environment-preview", "trust", "list", "--json"]
        )

        self.assertEqual(approve.environment_preview_trust_command, "approve")
        self.assertEqual(approve.image, _IMAGE)
        self.assertEqual(approve.realm_root, "/tmp/private-realm")
        self.assertTrue(approve.yes)
        self.assertTrue(approve.json)
        self.assertEqual(revoke.environment_preview_trust_command, "revoke")
        self.assertEqual(listed.environment_preview_trust_command, "list")

    def test_noninteractive_change_requires_yes_before_opening_realm(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "realm"
            with (
                patch("sys.stdin", io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = cli_main(
                    [
                        "environment-preview",
                        "trust",
                        "approve",
                        _IMAGE,
                        "--realm-root",
                        str(root),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("re-run with --yes", stderr.getvalue())
            self.assertFalse(root.exists())

    def test_interactive_change_requires_exact_confirmation_word(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "realm"
            with (
                patch("sys.stdin", _InteractiveInput("yes\n")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = cli_main(
                    [
                        "environment-preview",
                        "trust",
                        "approve",
                        _IMAGE,
                        "--realm-root",
                        str(root),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Type APPROVE", stderr.getvalue())
            self.assertIn("No Environment Preview trust change", stdout.getvalue())
            self.assertFalse(root.exists())

    def test_relative_realm_root_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = cli_main(
                [
                    "environment-preview",
                    "trust",
                    "list",
                    "--realm-root",
                    "relative-realm",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("--realm-root must be an absolute path", stderr.getvalue())

    def test_approve_list_and_revoke_persist_in_selected_realm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "realm"

            approved = self._run_json(
                [
                    "environment-preview",
                    "trust",
                    "approve",
                    _IMAGE,
                    "--realm-root",
                    str(root),
                    "--yes",
                    "--json",
                ]
            )
            listed = self._run_json(
                [
                    "environment-preview",
                    "trust",
                    "list",
                    "--realm-root",
                    str(root),
                    "--json",
                ]
            )

            self.assertEqual(approved["action"], "approve")
            self.assertTrue(approved["studio_restart_required"])
            self.assertEqual(approved["active"]["image_ref"], _IMAGE)
            self.assertEqual(approved["active"]["python_executable"], "python3")
            self.assertEqual(
                approved["active"]["contract"],
                "optpilot-stdlib-gateway-v1",
            )
            self.assertNotIn("actor_principal_id", json.dumps(approved))
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["active"][0]["image_ref"], _IMAGE)

            revoked = self._run_json(
                [
                    "environment-preview",
                    "trust",
                    "revoke",
                    _IMAGE,
                    "--realm-root",
                    str(root),
                    "--yes",
                    "--json",
                ]
            )
            after_revoke = self._run_json(
                [
                    "environment-preview",
                    "trust",
                    "list",
                    "--realm-root",
                    str(root),
                    "--json",
                ]
            )

            self.assertEqual(revoked["action"], "revoke")
            self.assertTrue(revoked["studio_restart_required"])
            self.assertIsNone(revoked["active"])
            self.assertEqual(after_revoke["active"], [])
            self.assertEqual(after_revoke["count"], 0)

    def test_invalid_image_is_an_actionable_cli_error(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir:
            with contextlib.redirect_stderr(stderr):
                exit_code = cli_main(
                    [
                        "environment-preview",
                        "trust",
                        "approve",
                        "example.invalid/preview:latest",
                        "--realm-root",
                        str(Path(tmp_dir) / "realm"),
                        "--yes",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("sha256", stderr.getvalue())

    def _run_json(self, argv: list[str]) -> dict:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli_main(argv)
        self.assertEqual(exit_code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
