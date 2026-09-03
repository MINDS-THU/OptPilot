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


if __name__ == "__main__":
    unittest.main()
