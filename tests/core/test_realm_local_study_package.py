from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from optpilot.config import compile_authoring_config
from optpilot.config_errors import StudyLaunchInputsError
from optpilot.realm.local_study_package import (
    LocalStudyPackagePlanError,
    plan_local_study_package,
)
from optpilot.runtime_limits import MAX_ATTEMPT_INPUT_LAYERS
from optpilot.spec import StudySpec


_ENVIRONMENT = """\
apiVersion: optpilot.io/v1
config: environment
id: clean-local-environment
description: Clean local package fixture
evaluator:
  python: local_package.evaluate:evaluate
  pythonPath:
    - ../..
    - .
    - ../../support/environment
    - ../..
  settings: {}
candidate:
  format: parameters
  parameters:
    schema:
      x:
        valueType: float
        min: 0.0
        max: 1.0
metrics:
  source: return
  keys: [score]
"""

_METHOD = """\
apiVersion: optpilot.io/v1
config: method
id: clean-local-method
description: Clean local package fixture
entrypoint:
  python: local_package.method:CleanMethod
  pythonPath:
    - ../../support/method
    - ../..
    - .
    - ../../support/method
  protocol: batch
settings: {}
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
"""

_STUDY = """\
apiVersion: optpilot.io/v1
config: study
name: clean-local-study
description: Clean nested package fixture
environmentConfig: ../environments/environment.yaml
methodConfig: ../methods/method.yaml
objective:
  metric: score
  direction: maximize
budget:
  maxTrials: 2
execution:
  parallelism: 1
  timeoutSeconds: 30
reproducibility:
  seed: 7
"""


def _write_package(root: Path) -> Path:
    study = root / "configs" / "studies" / "study.yaml"
    environment = root / "configs" / "environments" / "environment.yaml"
    method = root / "configs" / "methods" / "method.yaml"
    study.parent.mkdir(parents=True)
    environment.parent.mkdir(parents=True)
    method.parent.mkdir(parents=True)
    study.write_text(_STUDY, encoding="utf-8")
    environment.write_text(_ENVIRONMENT, encoding="utf-8")
    method.write_text(_METHOD, encoding="utf-8")
    (root / "local_package").mkdir()
    (root / "support" / "environment").mkdir(parents=True)
    (root / "support" / "method").mkdir(parents=True)
    (root / "local_package" / "evaluate.py").write_text(
        "def evaluate(candidate, context):\n    return {'score': candidate['x']}\n",
        encoding="utf-8",
    )
    (root / "local_package" / "method.py").write_text(
        "class CleanMethod:\n    pass\n",
        encoding="utf-8",
    )
    return study


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = None if path.is_dir() else path.read_bytes()
    return result


class LocalStudyPackagePlanTest(unittest.TestCase):
    def test_trial_workspace_sources_cross_as_portable_package_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            environment = root / "configs" / "environments" / "environment.yaml"
            environment.write_text(
                _ENVIRONMENT
                + """
trialWorkspace:
  - from: seed.json
    to: inputs/seed.json
  - from: fixtures
    to: data
""",
                encoding="utf-8",
            )
            (environment.parent / "seed.json").write_text(
                '{"seed": 7}\n', encoding="utf-8"
            )
            (environment.parent / "fixtures").mkdir()
            (environment.parent / "fixtures" / "case.txt").write_text(
                "case", encoding="utf-8"
            )

            plan = plan_local_study_package(study, root)

            self.assertEqual(
                plan.trial_workspace_mappings,
                (
                    ("configs/environments/seed.json", "inputs/seed.json"),
                    ("configs/environments/fixtures", "data"),
                ),
            )
            self.assertNotIn(str(root), repr(plan.trial_workspace_mappings))

    def test_trial_workspace_missing_and_outside_sources_are_rejected(self) -> None:
        for case, source, code in (
            ("missing", "missing.json", "trial_workspace_missing"),
            ("outside", "../../../outside.json", "trial_workspace_outside_package"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                parent = Path(raw)
                root = parent / "package"
                root.mkdir()
                study = _write_package(root)
                if case == "outside":
                    (parent / "outside.json").write_text("outside", encoding="utf-8")
                environment = root / "configs" / "environments" / "environment.yaml"
                environment.write_text(
                    _ENVIRONMENT
                    + f"""
trialWorkspace:
  - from: {source}
    to: input.json
""",
                    encoding="utf-8",
                )

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)

                self.assertEqual(raised.exception.code, code)

    def test_trial_workspace_layer_count_is_bounded_at_host_boundary(self) -> None:
        for count, accepted in (
            (MAX_ATTEMPT_INPUT_LAYERS, True),
            (MAX_ATTEMPT_INPUT_LAYERS + 1, False),
        ):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                environment = root / "configs" / "environments" / "environment.yaml"
                (environment.parent / "seed.txt").write_text("seed", encoding="utf-8")
                declarations = "\n".join(
                    f"  - from: seed.txt\n    to: seeds/{index}.txt"
                    for index in range(count)
                )
                environment.write_text(
                    _ENVIRONMENT + "\ntrialWorkspace:\n" + declarations + "\n",
                    encoding="utf-8",
                )

                if accepted:
                    plan = plan_local_study_package(study, root)
                    self.assertEqual(len(plan.trial_workspace_mappings), count)
                else:
                    with self.assertRaises(LocalStudyPackagePlanError) as raised:
                        plan_local_study_package(study, root)
                    self.assertEqual(raised.exception.code, "trial_workspace_too_large")

    def test_method_context_paths_cross_the_host_boundary_as_package_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            environment = root / "configs" / "environments" / "environment.yaml"
            environment.write_text(
                _ENVIRONMENT
                + """
methodContext:
  instructions: [prompt.md]
  references:
    - name: cases
      path: data/cases.yaml
      type: dataset
""",
                encoding="utf-8",
            )
            (environment.parent / "prompt.md").write_text(
                "optimize", encoding="utf-8"
            )
            (environment.parent / "data").mkdir()
            (environment.parent / "data" / "cases.yaml").write_text(
                "cases: []\n", encoding="utf-8"
            )

            plan = plan_local_study_package(study, root)

            self.assertEqual(
                plan.method_context_instruction_paths,
                ("configs/environments/prompt.md",),
            )
            self.assertEqual(
                plan.method_context_reference_paths,
                ("configs/environments/data/cases.yaml",),
            )
            encoded = repr(plan.method_context_instruction_paths) + repr(
                plan.method_context_reference_paths
            )
            self.assertNotIn(str(root), encoded)

    def test_method_context_inputs_must_be_existing_package_files(self) -> None:
        cases = (
            ("missing.md", "method_context_missing"),
            ("../../../outside.md", "method_context_outside_package"),
            ("directory", "compiled_method_context_invalid"),
        )
        for source, code in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as raw:
                parent = Path(raw)
                root = parent / "package"
                root.mkdir()
                study = _write_package(root)
                environment = root / "configs" / "environments" / "environment.yaml"
                if source == "../../../outside.md":
                    (parent / "outside.md").write_text("outside", encoding="utf-8")
                elif source == "directory":
                    (environment.parent / "directory").mkdir()
                environment.write_text(
                    _ENVIRONMENT
                    + f"""
methodContext:
  instructions: [{source}]
""",
                    encoding="utf-8",
                )

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)

                self.assertEqual(raised.exception.code, code)

    def test_method_context_symlinked_file_or_ancestor_is_rejected(self) -> None:
        for case in ("file", "ancestor"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                environment = root / "configs" / "environments" / "environment.yaml"
                (environment.parent / "real.md").write_text("context", encoding="utf-8")
                if case == "file":
                    (environment.parent / "linked.md").symlink_to("real.md")
                    source = "linked.md"
                else:
                    (environment.parent / "real-context").mkdir()
                    (environment.parent / "real-context" / "prompt.md").write_text(
                        "context", encoding="utf-8"
                    )
                    (environment.parent / "linked-context").symlink_to(
                        "real-context", target_is_directory=True
                    )
                    source = "linked-context/prompt.md"
                environment.write_text(
                    _ENVIRONMENT
                    + f"""
methodContext:
  instructions: [{source}]
""",
                    encoding="utf-8",
                )

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)

                self.assertEqual(raised.exception.code, "symlink_rejected")

    def test_valid_nested_package_compiles_once_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            before = _tree_bytes(root)

            with patch(
                "optpilot.realm.local_study_package.compile_authoring_config",
                wraps=compile_authoring_config,
            ) as compiler:
                plan = plan_local_study_package(study, root)

            self.assertEqual(compiler.call_count, 1)
            self.assertIsInstance(plan.study_spec, StudySpec)
            self.assertEqual(plan.study_spec.name, "clean-local-study")
            self.assertTrue(plan.package_root.is_absolute())
            self.assertEqual(plan.package_root, root.resolve())
            self.assertEqual(plan.study_config_path, "configs/studies/study.yaml")
            self.assertEqual(
                plan.environment_config_path,
                "configs/environments/environment.yaml",
            )
            self.assertEqual(plan.method_config_path, "configs/methods/method.yaml")
            self.assertEqual(
                plan.environment_python_import_roots,
                ("configs/environments", ".", "support/environment"),
            )
            self.assertEqual(
                plan.method_python_import_roots,
                ("configs/methods", "support/method", "."),
            )
            self.assertFalse(hasattr(plan, "to_dict"))
            with self.assertRaises(FrozenInstanceError):
                plan.package_root = root  # type: ignore[misc]
            self.assertEqual(_tree_bytes(root), before)

    def test_relative_package_root_is_made_absolute_without_root_inference(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            relative_root = root.relative_to(Path.cwd())
            relative_study = study.relative_to(Path.cwd())

            plan = plan_local_study_package(relative_study, relative_root)

            self.assertEqual(plan.package_root, root.resolve())

    def test_config_at_package_root_uses_single_logical_dot_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            nested_study = _write_package(root)
            nested_environment = (
                root / "configs" / "environments" / "environment.yaml"
            )
            nested_method = root / "configs" / "methods" / "method.yaml"
            study = root / "study.yaml"
            environment = root / "environment.yaml"
            method = root / "method.yaml"
            study.write_text(
                _STUDY.replace(
                    "../environments/environment.yaml", "environment.yaml"
                ).replace("../methods/method.yaml", "method.yaml"),
                encoding="utf-8",
            )
            environment.write_text(
                _ENVIRONMENT.replace(
                    "  pythonPath:\n"
                    "    - ../..\n"
                    "    - .\n"
                    "    - ../../support/environment\n"
                    "    - ../..\n",
                    "  pythonPath: [.]\n",
                ),
                encoding="utf-8",
            )
            method.write_text(
                _METHOD.replace(
                    "  pythonPath:\n"
                    "    - ../../support/method\n"
                    "    - ../..\n"
                    "    - .\n"
                    "    - ../../support/method\n",
                    "  pythonPath: [.]\n",
                ),
                encoding="utf-8",
            )
            nested_study.unlink()
            nested_environment.unlink()
            nested_method.unlink()

            plan = plan_local_study_package(study, root)

            self.assertEqual(plan.environment_python_import_roots, (".",))
            self.assertEqual(plan.method_python_import_roots, (".",))

    def test_outside_and_ambiguous_paths_are_rejected_before_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            root = parent / "package"
            root.mkdir()
            study = _write_package(root)
            outside = parent / "outside.yaml"
            outside.write_text(_ENVIRONMENT, encoding="utf-8")

            cases = (
                (outside, root, "config_outside_package"),
                (
                    root / "configs" / ".." / "configs" / "studies" / "study.yaml",
                    root,
                    "package_boundary_ambiguous",
                ),
            )
            for candidate, package_root, code in cases:
                with self.subTest(code=code), patch(
                    "optpilot.realm.local_study_package.compile_authoring_config"
                ) as compiler:
                    with self.assertRaises(LocalStudyPackagePlanError) as raised:
                        plan_local_study_package(candidate, package_root)
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(compiler.call_count, 0)

            study.write_text(
                _STUDY.replace(
                    "../environments/environment.yaml",
                    "../../../outside.yaml",
                ),
                encoding="utf-8",
            )
            with patch(
                "optpilot.realm.local_study_package.compile_authoring_config"
            ) as compiler:
                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)
                self.assertEqual(raised.exception.code, "config_outside_package")
                self.assertEqual(compiler.call_count, 0)

    def test_absolute_and_windows_authored_references_are_not_portable(self) -> None:
        for reference in ("/tmp/environment.yaml", r"C:\\temp\\environment.yaml"):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                study.write_text(
                    _STUDY.replace("../environments/environment.yaml", reference),
                    encoding="utf-8",
                )
                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)
                self.assertEqual(
                    raised.exception.code, "config_reference_not_portable"
                )

    def test_all_three_configs_must_be_existing_regular_files(self) -> None:
        cases = ("study_missing", "environment_missing", "method_directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                expected = "config_missing"
                if case == "study_missing":
                    study.unlink()
                elif case == "environment_missing":
                    (root / "configs" / "environments" / "environment.yaml").unlink()
                else:
                    method = root / "configs" / "methods" / "method.yaml"
                    method.unlink()
                    method.mkdir()
                    expected = "config_not_regular_file"

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)
                self.assertEqual(raised.exception.code, expected)

    def test_symlinked_root_config_or_unrelated_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            root = parent / "package"
            root.mkdir()
            study = _write_package(root)
            root_link = parent / "package-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(LocalStudyPackagePlanError) as raised:
                plan_local_study_package(root_link / study.relative_to(root), root_link)
            self.assertEqual(raised.exception.code, "symlink_rejected")

        for target in ("config", "unrelated"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                if target == "config":
                    method = root / "configs" / "methods" / "method.yaml"
                    real_method = root / "configs" / "methods" / "real-method.yaml"
                    method.rename(real_method)
                    method.symlink_to(real_method.name)
                else:
                    (root / "alias.py").symlink_to("local_package/evaluate.py")

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)
                self.assertEqual(raised.exception.code, "symlink_rejected")

    def test_filenames_are_not_guessed_as_capture_or_secret_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            for directory_name in (
                ".git",
                ".venv",
                "node_modules",
                "runs",
                "__pycache__",
            ):
                (root / "ordinary" / directory_name).mkdir(parents=True)
            for file_name in (
                ".env.local",
                "id_rsa",
                "service.key",
                "service.pem",
            ):
                (root / "ordinary" / file_name).write_text(
                    "ordinary package bytes", encoding="utf-8"
                )

            plan = plan_local_study_package(study, root)

            self.assertEqual(plan.study_spec.name, "clean-local-study")

    def test_non_directory_root_and_special_package_entry_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            root_file = parent / "package"
            root_file.write_text("not a package", encoding="utf-8")
            with self.assertRaises(LocalStudyPackagePlanError) as raised:
                plan_local_study_package(root_file, root_file)
            self.assertEqual(raised.exception.code, "package_root_not_directory")

            root = parent / "real-package"
            root.mkdir()
            study = _write_package(root)
            fifo = root / "events.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(LocalStudyPackagePlanError) as raised:
                plan_local_study_package(study, root)
            self.assertEqual(raised.exception.code, "package_entry_unsupported")

    def test_a_coded_compiler_rejection_keeps_its_own_code(self) -> None:
        # Every retained launch route (catalog ref, managed workspace) funnels
        # through here. Re-coding an already-typed rejection as a generic
        # config_compile_failed would hide which inputs the caller must
        # collect, which is the whole point of the code.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            study.write_text(
                _STUDY
                + "inputs:\n"
                + "  problem:\n"
                + "    valueType: string\n"
                + "    description: the problem\n",
                encoding="utf-8",
            )

            with self.assertRaises(StudyLaunchInputsError) as raised:
                plan_local_study_package(study, root, launch_inputs=None)

        self.assertEqual(raised.exception.code, "study_inputs_required")
        self.assertEqual(raised.exception.missing_inputs, ["problem"])
        self.assertEqual(
            raised.exception.declarations["problem"]["valueType"], "string"
        )

    def test_public_compiler_failure_is_a_typed_single_call_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            study.write_text(
                _STUDY.replace("metric: score", "metric: undeclared"),
                encoding="utf-8",
            )

            with patch(
                "optpilot.realm.local_study_package.compile_authoring_config",
                wraps=compile_authoring_config,
            ) as compiler:
                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)
            self.assertEqual(raised.exception.code, "config_compile_failed")
            self.assertEqual(compiler.call_count, 1)

    def test_compiled_authoring_paths_must_match_the_validated_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            calls = 0

            def mismatched_compiler(path: Path, **kwargs):
                nonlocal calls
                calls += 1
                compiled = compile_authoring_config(path, **kwargs)
                compiled["extensions"]["authoringConfig"][
                    "environmentConfigPath"
                ] = str(root / "different.yaml")
                return compiled

            with patch(
                "optpilot.realm.local_study_package.compile_authoring_config",
                side_effect=mismatched_compiler,
            ):
                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)

            self.assertEqual(raised.exception.code, "compiled_config_path_mismatch")
            self.assertEqual(calls, 1)

    def test_python_import_root_outside_package_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "package"
            root.mkdir()
            study = _write_package(root)
            environment = root / "configs" / "environments" / "environment.yaml"
            environment.write_text(
                _ENVIRONMENT.replace("    - ../..\n", "    - ../../..\n", 1),
                encoding="utf-8",
            )

            with self.assertRaises(LocalStudyPackagePlanError) as raised:
                plan_local_study_package(study, root)

            self.assertEqual(
                raised.exception.code, "python_import_root_outside_package"
            )

    def test_python_import_root_must_exist_and_be_a_directory(self) -> None:
        cases = (
            ("../../support/missing", "python_import_root_missing"),
            ("../../support/not-a-directory", "python_import_root_not_directory"),
        )
        for replacement, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "package"
                root.mkdir()
                study = _write_package(root)
                (root / "support" / "not-a-directory").write_text(
                    "not a directory", encoding="utf-8"
                )
                environment = (
                    root / "configs" / "environments" / "environment.yaml"
                )
                environment.write_text(
                    _ENVIRONMENT.replace(
                        "../../support/environment", replacement
                    ),
                    encoding="utf-8",
                )

                with self.assertRaises(LocalStudyPackagePlanError) as raised:
                    plan_local_study_package(study, root)

                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
