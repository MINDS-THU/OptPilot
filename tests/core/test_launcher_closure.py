"""The code that runs inside a container needs only a Python interpreter.

The design promises that an image never contains OptPilot: the launcher is
mounted in as source, so it must import with nothing installed beyond the
standard library. That guarantee held in a stubbed check but failed in a
genuinely bare image, because one settings-reading module imported its parser at
module level -- the difference between "nothing uses it" and "nothing imports
it". These tests simulate true absence, so the closure cannot quietly grow a
dependency again.
"""

import subprocess
import sys
import unittest
from pathlib import Path

#: Everything OptPilot itself depends on. Inside a container none of it exists.
THIRD_PARTY = ("yaml", "jsonschema", "referencing")

#: The programs that run inside a container.
IN_CONTAINER_MODULES = (
    "optpilot.retained_batch_worker",
    "optpilot.realm._local_attempt_worker",
)

_PROBE = """
import importlib.abc, importlib.machinery, sys

BLOCKED = {blocked!r}

class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ModuleNotFoundError(
                f"No module named {{name!r}} (simulating a bare image)"
            )
        return None

for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]
sys.meta_path.insert(0, Absent())

import importlib
for module in {modules!r}:
    importlib.import_module(module)
print("closure is stdlib-only")
"""


class LauncherClosureTest(unittest.TestCase):
    def test_the_in_container_programs_import_with_no_third_party_packages(
        self,
    ) -> None:
        source_root = Path(__file__).resolve().parents[2] / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _PROBE.format(blocked=THIRD_PARTY, modules=IN_CONTAINER_MODULES),
            ],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(source_root), "PATH": ""},
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            "The in-container import closure reached a third-party package.\n"
            "Inside a container an image guarantees a Python interpreter and\n"
            "nothing else, so the import belongs inside the function that uses\n"
            f"it, not at module level.\n\n{result.stderr}",
        )
        self.assertIn("closure is stdlib-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
