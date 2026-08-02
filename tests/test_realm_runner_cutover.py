from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from optpilot.cli import (
    _package_smoke,
    _select_package_smoke_study,
    build_parser,
    main as cli_main,
)
from optpilot.runner import StudyRunner, run_expanded_study_spec, run_study


class _RuntimeContext:
    def __init__(self) -> None:
        self.runtime = object()

    def __enter__(self):
        return self.runtime

    def __exit__(self, _type, _value, _traceback) -> None:
        return None


class _CliSummary:
    run_status = "succeeded"

    def to_dict(self) -> dict[str, str]:
        return {"run_id": "run-a", "run_status": self.run_status}


class _SmokeSummary:
    run_id = "run-smoke"
    run_status = "succeeded"
    stop_code = "max_trials"
    final_logical_failures = 0

    def to_dict(self) -> dict:
        return {
            "schema": "optpilot.run-summary-projection.v1",
            "run_id": self.run_id,
            "run_status": self.run_status,
            "submission_state": "terminal",
            "stop_code": self.stop_code,
            "retention_state": "active",
            "objective": {"metric": "score", "direction": "maximize"},
            "budget": {"max_trials": 1, "remaining_trials": 0},
            "counts": {
                "candidates": 1,
                "logical_trials": {
                    "total": 1,
                    "active": 0,
                    "terminal": 1,
                    "successful": 1,
                    "successful_objective_observations": 1,
                    "final_failures": 0,
                    "no_improvement": 0,
                    "by_state": {"terminal": 1},
                },
                "attempts": {"total": 1, "retries": 0, "by_state": {"terminal": 1}},
                "observations": {"total": 1, "by_outcome": {"success": 1}},
            },
            "best": {"metric": 1.0, "candidate_id": "candidate-a"},
            "cursor": {"revision": 1, "sequence": 1},
        }


class RealmRunnerCutoverTest(unittest.TestCase):
    def test_public_runner_uses_explicit_package_and_owned_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            study = package / "studies" / "study.yaml"
            realm = root / "realm"
            study.parent.mkdir(parents=True)
            study.write_text("apiVersion: optpilot.io/v1\n", encoding="utf-8")
            expected = object()
            context = _RuntimeContext()
            with mock.patch(
                "optpilot.runner.LocalRealmRuntime.open",
                return_value=context,
            ) as opened, mock.patch(
                "optpilot.runner.run_local_realm_study",
                return_value=expected,
            ) as launched:
                actual = run_study(
                    str(study),
                    package_root=str(package),
                    realm_root=str(realm),
                    operation_id="public-run/one",
                )

        self.assertIs(actual, expected)
        opened.assert_called_once_with(realm_root=realm.absolute())
        launched.assert_called_once_with(
            runtime=context.runtime,
            package_root=package.absolute(),
            study_config_path=study.absolute(),
            operation_id="public-run/one",
            method_environment=os.environ,
            method_request_timeout=10.0,
        )

    def test_study_runner_keeps_one_operation_identity_for_replay(self) -> None:
        runner = StudyRunner(
            Path("/package/studies/study.yaml"),
            package_root=Path("/package"),
            realm_root=Path("/realm"),
        )
        self.assertTrue(runner.operation_id.startswith("local-study-run/"))
        self.assertEqual(runner.operation_id, runner.operation_id)

    def test_study_runner_uses_secure_default_realm_root(self) -> None:
        expected = object()
        context = _RuntimeContext()
        with mock.patch(
            "optpilot.runner.default_realm_root", return_value=Path("/private/realm")
        ), mock.patch(
            "optpilot.runner.LocalRealmRuntime.open", return_value=context
        ) as opened, mock.patch(
            "optpilot.runner.run_local_realm_study", return_value=expected
        ):
            runner = StudyRunner(
                Path("/package/studies/study.yaml"),
                package_root=Path("/package"),
            )
            actual = runner.run()

        self.assertIs(actual, expected)
        opened.assert_called_once_with(realm_root=Path("/private/realm"))

    def test_expanded_and_directory_authority_paths_are_removed(self) -> None:
        with self.assertRaisesRegex(ValueError, "removed by the Realm cutover"):
            run_expanded_study_spec("expanded.yaml", output_root="runs")

        parser = build_parser()
        parsed = parser.parse_args(
            [
                "run",
                "study.yaml",
                "--package-root",
                "package",
                "--realm-root",
                "realm",
                "--operation-id",
                "studio/job-a",
                "--method-request-timeout",
                "1200",
            ]
        )
        self.assertEqual(parsed.package_root, "package")
        self.assertEqual(parsed.realm_root, "realm")
        self.assertEqual(parsed.operation_id, "studio/job-a")
        self.assertEqual(parsed.method_request_timeout, 1200.0)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["run", "study.yaml", "--output-root", "runs"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["run", "study.yaml"])

    def test_cli_passes_only_package_and_realm_authority(self) -> None:
        with redirect_stdout(StringIO()), mock.patch(
            "optpilot.cli.run_study", return_value=_CliSummary()
        ) as run:
            exit_code = cli_main(
                [
                    "run",
                    "study.yaml",
                    "--package-root",
                    "package",
                    "--realm-root",
                    "realm",
                    "--operation-id",
                    "studio/job-a",
                    "--method-request-timeout",
                    "1200",
                ]
            )

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            "study.yaml",
            package_root="package",
            realm_root="realm",
            operation_id="studio/job-a",
            method_request_timeout=1200.0,
        )

    def test_package_smoke_uses_ephemeral_realm_and_path_free_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package"
            study = package / "studies" / "smoke.yaml"
            study.parent.mkdir(parents=True)
            study.write_text("apiVersion: optpilot.io/v1\n", encoding="utf-8")
            captured: dict[str, str] = {}

            def launch(spec_path: str, *, package_root: str, realm_root: str):
                captured.update(
                    spec_path=spec_path,
                    package_root=package_root,
                    realm_root=realm_root,
                )
                self.assertTrue(Path(realm_root).parent.is_dir())
                return _SmokeSummary()

            with mock.patch(
                "optpilot.cli.validate_package", return_value={"valid": True}
            ), mock.patch(
                "optpilot.cli.validate_authoring_config", return_value={"valid": True}
            ), mock.patch("optpilot.cli.run_study", side_effect=launch):
                result = _package_smoke(
                    str(package), study="studies/smoke.yaml", realm_root=None
                )

            ephemeral_parent = Path(captured["realm_root"]).parent
            self.assertFalse(ephemeral_parent.exists())

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["run_id"], "run-smoke")
        self.assertEqual(result["summary"]["schema"], "optpilot.run-summary-projection.v1")
        self.assertNotIn("run_dir", json.dumps(result["summary"]))

    def test_package_smoke_study_cannot_escape_explicit_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            outside = root / "outside.yaml"
            outside.write_text("apiVersion: optpilot.io/v1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inside the explicit package"):
                _select_package_smoke_study(package, "../outside.yaml")

            linked = package / "linked.yaml"
            try:
                linked.symlink_to(outside)
            except (OSError, NotImplementedError):
                return
            with self.assertRaisesRegex(ValueError, "inside the explicit package"):
                _select_package_smoke_study(package, "linked.yaml")


if __name__ == "__main__":
    unittest.main()
