from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SharedStudioLaunchdRenderTests(unittest.TestCase):
    def test_job_contains_only_paths_and_a_bounded_environment(self) -> None:
        root = Path(__file__).resolve().parents[2]
        renderer = root / "deploy" / "shared-studio" / "render_launchd.py"
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            script = private / "studio.sh"
            config = private / "deploy.env"
            log = private / "studio.log"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            config.write_text("PUBLIC_HOST=studio.example.edu\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(renderer),
                    "--label",
                    "io.optpilot.shared-studio",
                    "--script",
                    str(script),
                    "--working-directory",
                    str(private),
                    "--deploy-config",
                    str(config),
                    "--log",
                    str(log),
                ],
                env={**os.environ, "OPENROUTER_API_KEY": "must-not-be-serialized"},
                capture_output=True,
                check=True,
            )
        payload = plistlib.loads(completed.stdout)
        self.assertEqual(payload["Label"], "io.optpilot.shared-studio")
        self.assertEqual(
            payload["ProgramArguments"], ["/bin/bash", str(script.resolve())]
        )
        environment = payload["EnvironmentVariables"]
        self.assertEqual(
            set(environment), {"HOME", "PATH", "OPTPILOT_DEPLOY_CONFIG"}
        )
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertEqual(payload["Umask"], 0o077)
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})

    def test_studio_launcher_uses_the_private_catalog_as_packages_root(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "deploy" / "shared-studio" / "studio.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'export OPTPILOT_PACKAGES_ROOT="${OPTPILOT_CATALOG_ROOT}"', source
        )
        self.assertIn("export OPTPILOT_REALM_ROOT", source)
        self.assertNotIn("source_catalog_args", source)
        self.assertIn('--catalog "${OPTPILOT_CATALOG_ROOT}"', source)

    def test_catalog_install_is_an_explicit_activation_step(self) -> None:
        root = Path(__file__).resolve().parents[2]
        deployment = root / "deploy" / "shared-studio"
        preflight = (deployment / "preflight.sh").read_text(encoding="utf-8")
        launcher = (deployment / "deploy.sh").read_text(encoding="utf-8")
        installer = (deployment / "install_catalog_packages.sh").read_text(
            encoding="utf-8"
        )

        source_validation = preflight.split(
            'if [ "${mode}" = "source" ]; then', 1
        )[1].split("\nelse\n", 1)[0]
        self.assertIn("OPTPILOT_INSTALL_TARGET_ROOT", source_validation)
        self.assertIn("install_catalog_packages.sh", source_validation)
        self.assertIn('activate_prepared_sources()', launcher)
        self.assertIn('bash "${DEPLOY_DIR}/install_catalog_packages.sh"', launcher)
        self.assertLess(
            launcher.index('"$0" stop'),
            launcher.index("activate_prepared_sources", launcher.index('"$0" stop')),
        )
        self.assertIn('"${SOURCE_ROOT}/catalog"/*', installer)
        self.assertIn('OPTPILOT_SOURCE_CATALOG_EXCLUDES', installer)
        self.assertIn('"${install_root}/${package_name}"', installer)
        self.assertIn('rsync -a --delete --delete-excluded', installer)
        self.assertIn('optpilot package validate "${target}" --check-source', installer)

    def test_check_uses_temporary_nginx_configuration(self) -> None:
        root = Path(__file__).resolve().parents[2]
        deployment = root / "deploy" / "shared-studio"
        gateway = (deployment / "nginx.sh").read_text(encoding="utf-8")

        self.assertIn("optpilot-nginx-check.XXXXXX", gateway)
        self.assertIn('render_root="${NGINX_ROOT}"', gateway)
        self.assertIn('if [ "${action}" = "check" ]', gateway)

    def test_workspace_image_is_pinned_and_content_checked(self) -> None:
        root = Path(__file__).resolve().parents[2]
        deployment = root / "deploy" / "shared-studio"
        library = (deployment / "_lib.sh").read_text(encoding="utf-8")
        image = (deployment / "workspace_image.sh").read_text(encoding="utf-8")

        self.assertIn("code-server-4.137.0-node-22.19.0-uv-0.12.15", library)
        self.assertIn("@sha256:", library)
        self.assertIn("io.optpilot.workspace-runtime.revision", image)
        self.assertIn('"${WORKSPACE_RUNTIME_BIN}" build --pull', image)

    def test_preflight_checks_the_pinned_openhands_environment(self) -> None:
        root = Path(__file__).resolve().parents[2]
        deployment = root / "deploy" / "shared-studio"
        requirements = (
            deployment / "requirements-openhands.txt"
        ).read_text(encoding="utf-8").splitlines()
        preflight = (deployment / "preflight.sh").read_text(encoding="utf-8")

        self.assertEqual(
            requirements,
            [
                "openhands-agent-server==1.40.1",
                "openhands-sdk==1.40.1",
                "openhands-tools==1.40.1",
                "openhands-workspace==1.40.1",
            ],
        )
        self.assertIn('requirements-openhands.txt', preflight)
        self.assertIn('OpenHands package version mismatch', preflight)

    def test_openhands_skips_unused_browser_tool_preload(self) -> None:
        root = Path(__file__).resolve().parents[2]
        launcher = (
            root / "deploy" / "shared-studio" / "openhands.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("export OH_PRELOAD_TOOLS=0", launcher)


if __name__ == "__main__":
    unittest.main()
