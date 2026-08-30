"""The shipped browser client must carry Studio's per-process mutation token."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_NODE = shutil.which("node")


def _function_source(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


class StudioHttpMutationClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_every_client_mutation_path_uses_the_security_context(self) -> None:
        post = _function_source(self.source, "postJson")
        delete = _function_source(self.source, "deleteJson")
        unload = _function_source(self.source, "releaseSelectionContentViewOnUnload")

        self.assertIn("await studioMutationHeaders()", post)
        self.assertIn("await studioMutationHeaders()", delete)
        self.assertIn("studioMutationHeadersIfReady()", unload)
        self.assertIn('"/api/security-context"', self.source)
        self.assertIn('header !== "X-OptPilot-CSRF-Token"', self.source)

    @unittest.skipUnless(_NODE, "node is required to evaluate the client header builder")
    def test_header_builder_emits_json_content_type_and_server_token(self) -> None:
        builder = _function_source(self.source, "studioMutationHeadersIfReady")
        harness = f"""
"use strict";
let studioSecurityContext = {{
  csrf_header: "X-OptPilot-CSRF-Token",
  csrf_token: "process-local-token",
}};
{builder}
process.stdout.write(JSON.stringify(studioMutationHeadersIfReady()));
"""
        completed = subprocess.run(
            [str(_NODE), "-e", harness],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "Content-Type": "application/json",
                "X-OptPilot-CSRF-Token": "process-local-token",
            },
        )


if __name__ == "__main__":
    unittest.main()
