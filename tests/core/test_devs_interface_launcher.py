from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from optpilot.config import compile_interface_launch_profiles
from optpilot.resource_actions import compile_resource_actions, find_resource_action

CATALOG_RESOURCE = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "devs_gallery"
    / "resources"
    / "devs-gen-interface"
)


class DevsInterfaceLauncherTest(unittest.TestCase):
    @staticmethod
    def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
        snapshot: dict[str, tuple[int, int, str]] = {}
        for path in [root, *sorted(root.rglob("*"))]:
            metadata = path.lstat()
            relative = "." if path == root else path.relative_to(root).as_posix()
            digest = ""
            if stat.S_ISREG(metadata.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            elif stat.S_ISLNK(metadata.st_mode):
                digest = os.readlink(path)
            snapshot[relative] = (
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                digest,
            )
        return snapshot

    @staticmethod
    def _seal_tree(root: Path) -> None:
        for path in [*root.rglob("*"), root]:
            path.chmod(stat.S_IMODE(path.lstat().st_mode) & ~0o222)

    @staticmethod
    def _make_tree_writable(root: Path) -> None:
        if not root.exists():
            return
        for path in [root, *root.rglob("*")]:
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.lstat().st_mode) | 0o200)

    def _runnable_resource(self) -> Path:
        """Copy the interface resource somewhere ordinary and return the copy.

        A developer checkout often sits in a file-provider-backed sync folder
        (Synology Drive, iCloud, Dropbox), and there open() of a catalog file
        occasionally blocks for milliseconds while the provider services it —
        measured at ~1.7 per 100k opens, against nothing above 400us in 600k
        opens on a plain volume. The launcher reaches
        `exec ./_start_frontend.sh` with a backgrounded _start_backend.sh
        still outstanding, so a child exiting during one of those blocked
        opens interrupts it: bash reports "Interrupted system call" against
        its own script argument and exits 126, failing whichever assertion
        consumed that run. Executing from a temporary directory removes the
        blocking window instead of tolerating the resulting flake.
        """
        temporary = tempfile.mkdtemp(prefix="devs-gen-interface-")
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        destination = Path(temporary) / CATALOG_RESOURCE.name
        shutil.copytree(CATALOG_RESOURCE, destination, symlinks=True)
        return destination

    def test_model_roles_are_selected_from_host_environment(self) -> None:
        resource = CATALOG_RESOURCE
        environment = dict(os.environ)
        environment.update(
            {
                "DEVS_INTERFACE_MODEL_ID": "openrouter/example/routine",
                "DEVS_INTERFACE_STRONG_MODEL_ID": "openrouter/example/strong",
                "DEVS_DISPLAY_MODEL_ID": "openrouter/example/graph",
                "PYTHONPATH": str(resource),
            }
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from devs_settings import agent_model_id, "
                    "agent_strong_model_id, visualizer_model_id; "
                    "print(agent_model_id()); print(agent_strong_model_id()); "
                    "print(visualizer_model_id())"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "openrouter/example/routine",
                "openrouter/example/strong",
                "openrouter/example/graph",
            ],
        )

    def test_preparation_and_launch_require_distinct_runtime_access(self) -> None:
        resource = self._runnable_resource()

        cases = (
            (
                resource / "_optpilot_launch_interface.sh",
                [],
                "build",
                "expected 'read-only'",
            ),
            (
                resource / "_optpilot_launch_interface.sh",
                ["--prepare-only"],
                "read-only",
                "expected 'build'",
            ),
            (
                resource / "_start_frontend.sh",
                [],
                "build",
                "expected 'read-only'",
            ),
            (
                resource / "_start_frontend.sh",
                ["--prepare-only"],
                "read-only",
                "expected 'build'",
            ),
        )
        for script, arguments, access, expected_error in cases:
            with self.subTest(
                script=script.name,
                arguments=arguments,
                access=access,
            ):
                completed = subprocess.run(
                    ["bash", str(script), *arguments],
                    cwd=resource,
                    env={
                        **os.environ,
                        "OPTPILOT_PREPARED_RUNTIME_ACCESS": access,
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(expected_error, completed.stderr)

    def test_explicit_runtime_contract_rejects_dependency_paths_outside_payload(
        self,
    ) -> None:
        resource = self._runnable_resource()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared_root = root / "prepared"
            cases = (
                (
                    resource / "_optpilot_launch_interface.sh",
                    {
                        "OPTPILOT_INTERFACE_VENV": str(root / "outside-venv"),
                    },
                    "OPTPILOT_INTERFACE_VENV must be",
                ),
                (
                    resource / "_optpilot_launch_interface.sh",
                    {
                        "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT": str(
                            root / "outside-frontend"
                        ),
                    },
                    "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT must be",
                ),
                (
                    resource / "_start_frontend.sh",
                    {
                        "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT": str(
                            root / "outside-frontend"
                        ),
                    },
                    "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT must be",
                ),
            )
            for script, override, expected_error in cases:
                with self.subTest(script=script.name, override=override):
                    completed = subprocess.run(
                        ["bash", str(script)],
                        cwd=resource,
                        env={
                            **os.environ,
                            "OPTPILOT_PREPARED_RUNTIME_ACCESS": "read-only",
                            "OPTPILOT_PREPARED_RUNTIME_ROOT": str(prepared_root),
                            **override,
                        },
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )

                    self.assertEqual(completed.returncode, 2)
                    self.assertIn(expected_error, completed.stderr)

    def test_interface_declares_dependency_preparation_before_readiness(self) -> None:
        resource = CATALOG_RESOURCE
        raw = yaml.safe_load(
            (resource / "optpilot.resource.yaml").read_text(encoding="utf-8")
        )

        profile = compile_interface_launch_profiles(
            raw["interface"], component_kind="resource"
        )[0]
        setup = profile.runtime.setup

        self.assertIsNotNone(setup)
        assert setup is not None
        self.assertEqual(setup.cache, "prepared")
        self.assertEqual(setup.timeout_seconds, 1800)
        self.assertEqual(
            setup.steps[0]["command"],
            ["bash", "./_optpilot_launch_interface.sh", "--prepare-only"],
        )
        self.assertEqual(
            profile.grants.env_from_host,
            (
                "DEVS_DISPLAY_MODEL_ID",
                "DEVS_INTERFACE_MODEL_ID",
                "DEVS_INTERFACE_STRONG_MODEL_ID",
            ),
        )
        self.assertEqual(
            profile.grants.secrets_from_host,
            ("OPENROUTER_API_KEY",),
        )

    def test_standalone_launch_keeps_runtime_state_out_of_source_tree(self) -> None:
        resource = CATALOG_RESOURCE
        launcher = (resource / "_optpilot_launch_interface.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'DEVS_DISPLAY_WORKING_DIRS_ROOT="$RUNTIME_ROOT/working-dirs"',
            launcher,
        )
        self.assertIn(
            'DEVS_INTERFACE_PERSISTENT_STORAGE_ROOT="$RUNTIME_ROOT/persistent-storage"',
            launcher,
        )
        self.assertIn('BACKEND_LOG="$RUNTIME_ROOT/backend.run.log"', launcher)
        self.assertNotIn("mkdir -p devs_app/working_dirs", launcher)
        self.assertNotIn('BACKEND_LOG="$ROOT/backend.run.log"', launcher)

    def test_direct_python_entrypoints_share_the_runtime_boundary(self) -> None:
        resource = CATALOG_RESOURCE
        agent_entrypoint = (resource / "devs_app" / "run.py").read_text(
            encoding="utf-8"
        )
        display_server = (
            resource / "devs_display" / "backend" / "server.py"
        ).read_text(encoding="utf-8")

        for source in (agent_entrypoint, display_server):
            self.assertIn("OPTPILOT_INTERFACE_RUNTIME_ROOT", source)
            self.assertIn('".runtime"', source)
        self.assertNotIn('"devs_app/working_dirs"', agent_entrypoint)
        self.assertNotIn('"devs_display/.storage"', display_server)

    def test_managed_launch_respects_platform_runtime_handles(self) -> None:
        resource = self._runnable_resource()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "output"
            runtime_root = root / "runtime"
            ephemeral_root = root / "ephemeral"
            prepared_root = root / "prepared runtime"
            frontend_root = prepared_root / "frontend"
            venv_root = prepared_root / "python-venv"
            control_file = root / "control" / "outputs.jsonl"
            fake_bin = root / "bin"
            python_log = root / "python.log"
            vite_log = root / "vite.log"
            agent_url_log = root / "agent-url.log"
            output_root.mkdir()
            control_file.parent.mkdir()
            control_file.touch()
            fake_bin.mkdir()

            fake_python = fake_bin / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_PYTHON_LOG\"\n"
                "if [ \"${1:-}\" = '-m' ] && [ \"${2:-}\" = 'venv' ]; then\n"
                "  target=\"$3\"\n"
                "  mkdir -p \"$target/bin\"\n"
                "  cp \"$0\" \"$target/bin/python\"\n"
                "  chmod +x \"$target/bin/python\"\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            fake_npm = fake_bin / "npm"
            fake_npm.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "prefix=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = '--prefix' ]; then\n"
                "    shift\n"
                "    prefix=\"$1\"\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "test -n \"$prefix\"\n"
                "mkdir -p \"$prefix/node_modules/.bin\"\n"
                "printf '%s\\n' '#!/usr/bin/env bash' "
                "'printf \"%s\\n\" \"$VITE_AGENT_API_URL\" > \"$FAKE_AGENT_URL_LOG\"' "
                "'printf \"%s\\n\" \"$*\" > \"$FAKE_VITE_LOG\"' "
                "'exit 0' > \"$prefix/node_modules/.bin/vite\"\n"
                "chmod +x \"$prefix/node_modules/.bin/vite\"\n",
                encoding="utf-8",
            )
            fake_npm.chmod(0o755)

            environment = dict(os.environ)
            environment.update(
                {
                    "FAKE_PYTHON_LOG": str(python_log),
                    "FAKE_VITE_LOG": str(vite_log),
                    "FAKE_AGENT_URL_LOG": str(agent_url_log),
                    "OPTPILOT_INTERFACE_EPHEMERAL_ROOT": str(ephemeral_root),
                    "OPTPILOT_PREPARED_RUNTIME_ROOT": str(prepared_root),
                    "OPTPILOT_INTERFACE_OUTPUT_ROOT": str(output_root),
                    "OPTPILOT_INTERFACE_OUTPUTS_FILE": str(control_file),
                    "OPTPILOT_INTERFACE_PYTHON": str(fake_python),
                    "OPTPILOT_INTERFACE_RUNTIME_ROOT": str(runtime_root),
                    "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
                }
            )

            prepare_environment = {
                **environment,
                "OPTPILOT_PREPARED_RUNTIME_ACCESS": "build",
            }
            launch_environment = {
                **environment,
                "OPTPILOT_PREPARED_RUNTIME_ACCESS": "read-only",
            }
            prepared = subprocess.run(
                [
                    "bash",
                    str(resource / "_optpilot_launch_interface.sh"),
                    "--prepare-only",
                ],
                cwd=resource,
                env=prepare_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            python_after_prepare = python_log.read_text(encoding="utf-8")
            python_marker = venv_root / ".optpilot-interface-deps-installed"
            frontend_marker = (
                frontend_root / "node_modules" / ".optpilot-interface-deps-installed"
            )

            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertTrue(python_marker.read_text(encoding="utf-8").startswith(
                "optpilot-devs-python-runtime-v1:"
            ))
            self.assertTrue(frontend_marker.read_text(encoding="utf-8").startswith(
                "optpilot-devs-frontend-runtime-v1:"
            ))

            # Managed setup receives a dedicated prepared root while the
            # Catalog source itself is read-only. It must not eagerly create
            # ordinary launch/session state in the source-local fallback.
            source_runtime = resource / ".runtime"
            source_runtime_existed = source_runtime.exists()
            source_runtime_snapshot = (
                self._tree_snapshot(source_runtime)
                if source_runtime_existed
                else None
            )
            setup_only_root = root / "setup-only prepared runtime"
            setup_only_python_log = root / "setup-only-python.log"
            setup_only_environment = dict(environment)
            for name in (
                "OPTPILOT_INTERFACE_EPHEMERAL_ROOT",
                "OPTPILOT_INTERFACE_OUTPUT_ROOT",
                "OPTPILOT_INTERFACE_OUTPUTS_FILE",
                "OPTPILOT_INTERFACE_RUNTIME_ROOT",
            ):
                setup_only_environment.pop(name, None)
            setup_only_environment.update(
                {
                    "FAKE_PYTHON_LOG": str(setup_only_python_log),
                    "OPTPILOT_PREPARED_RUNTIME_ACCESS": "build",
                    "OPTPILOT_PREPARED_RUNTIME_ROOT": str(setup_only_root),
                }
            )
            setup_only = subprocess.run(
                [
                    "bash",
                    str(resource / "_optpilot_launch_interface.sh"),
                    "--prepare-only",
                ],
                cwd=resource,
                env=setup_only_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(setup_only.returncode, 0, setup_only.stderr)
            if source_runtime_existed:
                self.assertEqual(
                    self._tree_snapshot(source_runtime),
                    source_runtime_snapshot,
                )
            else:
                self.assertFalse(source_runtime.exists())

            # Reproduce the projection-ordering bug that prompted this contract:
            # source mtimes may be newer even though dependency content is identical.
            dependency_files = [
                resource / "requirements-interface.txt",
                resource / "devs_display" / "frontend" / "package.json",
                resource / "devs_display" / "frontend" / "package-lock.json",
            ]
            original_times = {
                path: (path.stat().st_atime_ns, path.stat().st_mtime_ns)
                for path in dependency_files
            }
            self.addCleanup(
                lambda: [
                    os.utime(path, ns=times)
                    for path, times in original_times.items()
                ]
            )
            future = time.time_ns() + 10_000_000_000
            for path in dependency_files:
                os.utime(path, ns=(path.stat().st_atime_ns, future))

            self._seal_tree(prepared_root)
            self.addCleanup(self._make_tree_writable, prepared_root)
            prepared_snapshot = self._tree_snapshot(prepared_root)

            completed = subprocess.run(
                ["bash", str(resource / "_optpilot_launch_interface.sh")],
                cwd=resource,
                env=launch_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(self._tree_snapshot(prepared_root), prepared_snapshot)
            self.assertFalse((output_root / ".runtime").exists())
            self.assertTrue((runtime_root / "backend.run.log").is_file())
            self.assertTrue((frontend_root / "app" / "package.json").is_file())
            self.assertIn("-m pip install", python_after_prepare)
            self.assertNotIn("-m devs_app.run", python_after_prepare)
            self.assertEqual(
                python_log.read_text(encoding="utf-8").count("-m pip install"),
                1,
            )
            self.assertIn("-m devs_app.run", python_log.read_text(encoding="utf-8"))
            self.assertIn(str(frontend_root / "app"), vite_log.read_text(encoding="utf-8"))
            self.assertIn(
                "--configLoader runner", vite_log.read_text(encoding="utf-8")
            )
            self.assertEqual(
                agent_url_log.read_text(encoding="utf-8").strip(),
                "/__optpilot_port/8000",
            )

            for missing_relative, expected_error in (
                (
                    Path("python-venv/.optpilot-interface-deps-installed"),
                    "Prepared Python dependency marker is missing or stale",
                ),
                (
                    Path("frontend/node_modules/.optpilot-interface-deps-installed"),
                    "Prepared frontend dependency marker is missing or stale",
                ),
            ):
                with self.subTest(missing_marker=missing_relative.as_posix()):
                    missing_root = root / f"missing-{missing_relative.parent.name}"
                    shutil.copytree(prepared_root, missing_root, symlinks=True)
                    self._make_tree_writable(missing_root)
                    (missing_root / missing_relative).unlink()
                    self._seal_tree(missing_root)
                    missing_snapshot = self._tree_snapshot(missing_root)
                    missing_environment = {
                        **launch_environment,
                        "OPTPILOT_PREPARED_RUNTIME_ROOT": str(missing_root),
                    }

                    rejected = subprocess.run(
                        ["bash", str(resource / "_optpilot_launch_interface.sh")],
                        cwd=resource,
                        env=missing_environment,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )

                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(expected_error, rejected.stderr)
                    self.assertIn("launch phase is read-only", rejected.stderr)
                    self.assertEqual(
                        self._tree_snapshot(missing_root),
                        missing_snapshot,
                    )
                    self.assertEqual(
                        python_log.read_text(encoding="utf-8").count(
                            "-m pip install"
                        ),
                        1,
                    )
                    self._make_tree_writable(missing_root)

            # Unset access is the documented manual two-command flow. It uses
            # build for preparation and read-only for launch, with writable
            # Vite state kept outside the prepared dependency tree.
            default_output = root / "default-output"
            default_control = root / "default-control" / "outputs.jsonl"
            default_runtime = root / "default-runtime"
            default_python_log = root / "default-python.log"
            default_vite_log = root / "default-vite.log"
            default_agent_url_log = root / "default-agent-url.log"
            default_output.mkdir()
            default_control.parent.mkdir()
            default_control.touch()
            default_environment = dict(environment)
            for name in (
                "OPTPILOT_INTERFACE_EPHEMERAL_ROOT",
                "OPTPILOT_PREPARED_RUNTIME_ACCESS",
                "OPTPILOT_PREPARED_RUNTIME_ROOT",
            ):
                default_environment.pop(name, None)
            default_environment.update(
                {
                    "FAKE_PYTHON_LOG": str(default_python_log),
                    "FAKE_VITE_LOG": str(default_vite_log),
                    "FAKE_AGENT_URL_LOG": str(default_agent_url_log),
                    "OPTPILOT_INTERFACE_OUTPUT_ROOT": str(default_output),
                    "OPTPILOT_INTERFACE_OUTPUTS_FILE": str(default_control),
                    "OPTPILOT_INTERFACE_RUNTIME_ROOT": str(default_runtime),
                }
            )

            default_prepared = subprocess.run(
                [
                    "bash",
                    str(resource / "_optpilot_launch_interface.sh"),
                    "--prepare-only",
                ],
                cwd=resource,
                env=default_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            default_launched = subprocess.run(
                ["bash", str(resource / "_optpilot_launch_interface.sh")],
                cwd=resource,
                env=default_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                default_prepared.returncode,
                0,
                default_prepared.stderr,
            )
            self.assertEqual(
                default_launched.returncode,
                0,
                default_launched.stderr,
            )
            self.assertTrue(
                (
                    default_runtime
                    / "prepared"
                    / "python-venv"
                    / ".optpilot-interface-deps-installed"
                ).is_file()
            )
            self.assertTrue((default_runtime / "vite-cache").is_dir())
            self.assertEqual(
                default_python_log.read_text(encoding="utf-8").count(
                    "-m pip install"
                ),
                1,
            )


class DevsGenerateActionRuntimeTest(unittest.TestCase):
    """The headless `generate` action declares its own dependency closure.

    Its imports (smolagents, litellm, pydantic and ~40 transitive packages)
    have native wheels, so no vendored pure-wheel lock is possible. The action
    must therefore declare a `python-venv` runtime built from the same
    requirements file as the interface — never rely on whichever packages
    happen to sit in the host installation running optpilot.
    """

    def setUp(self) -> None:
        self.resource = CATALOG_RESOURCE

    def test_generate_action_declares_its_python_runtime(self) -> None:
        raw = yaml.safe_load(
            (self.resource / "optpilot.resource.yaml").read_text(encoding="utf-8")
        )
        actions = compile_resource_actions(raw)
        action = find_resource_action(actions, "generate")

        self.assertEqual(action.runtime.get("sandbox"), "process")
        steps = action.runtime["setup"]["steps"]
        self.assertEqual([step["uses"] for step in steps], ["python-venv"])
        self.assertEqual(steps[0]["requirements"], ["requirements-interface.txt"])
        self.assertTrue((self.resource / "requirements-interface.txt").is_file())
        # Declared runtime state stays inside the ignored .runtime/ boundary.
        self.assertTrue(steps[0]["venv"].startswith(".runtime/"))
        # Setup installs from PyPI and generation calls the provider.
        self.assertEqual(action.network, "enabled")

    def test_generate_action_runtime_is_not_lockable_as_pure_wheels(self) -> None:
        # Guards the reason the action installs from a requirements file
        # instead of a vendored lock: these distributions publish only
        # platform wheels, which locked_python_runtime rejects.
        requirements = (self.resource / "requirements-interface.txt").read_text(
            encoding="utf-8"
        )
        for distribution in ("litellm", "numpy", "scipy", "pillow"):
            self.assertIn(distribution, requirements.lower())

    def test_missing_dependencies_fail_with_a_typed_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs_file = root / "inputs.json"
            inputs_file.write_text(
                '{"specification": "a barbershop", "rootModelName": "Shop"}',
                encoding="utf-8",
            )
            output_root = root / "out"
            output_root.mkdir()

            # -S drops site-packages, reproducing an installation without the
            # action's declared runtime; -E keeps PYTHONPATH from restoring it.
            completed = subprocess.run(
                [sys.executable, "-S", "-E", "headless_generate.py"],
                cwd=self.resource,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "OPTPILOT_RESOURCE_ACTION_INPUTS_FILE": str(inputs_file),
                    "OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT": str(output_root),
                },
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("resource_action_dependencies_missing", completed.stderr)
        self.assertIn("requirements-interface.txt", completed.stderr)
        self.assertIn(sys.executable, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
