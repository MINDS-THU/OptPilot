"""Both distributions carry the licence they claim.

Both packages declare Apache-2.0, which requires that anyone receiving the
software also receives a copy of the licence. The core declared a licence file
and shipped it; Studio declared only the licence *name*, so its wheel and its
sdist went out with no licence text in them at all -- an obligation the project
states it is under and was not meeting.

This reads the packaging declarations rather than building, so it is fast and
runs everywhere; the build itself is checked by scripts/check_release_artifacts.py.
"""

from __future__ import annotations

import unittest

# tomllib arrived in Python 3.11, and OptPilot's floor is 3.10. On 3.10 this
# import error surfaced as a loader failure in CI -- the one interpreter in
# the matrix without the module. What these tests check is identical text on
# every interpreter, and the 3.11 and 3.12 jobs still check it, so on 3.10
# they skip by name rather than failing to even load.
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PROJECTS = {
    "optpilot": _ROOT / "pyproject.toml",
    "optpilot-studio": _ROOT / "studio" / "pyproject.toml",
}


@unittest.skipIf(
    tomllib is None,
    "tomllib is 3.11+; the 3.11 and 3.12 jobs cover these version-independent checks",
)
class LicenceShippingTest(unittest.TestCase):
    def test_each_distribution_names_a_licence(self) -> None:
        for name, path in _PROJECTS.items():
            with self.subTest(project=name):
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["project"]["license"], "Apache-2.0")

    def test_each_distribution_ships_the_licence_text(self) -> None:
        for name, path in _PROJECTS.items():
            with self.subTest(project=name):
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                declared = data["project"].get("license-files")
                self.assertTrue(
                    declared,
                    f"{name} declares Apache-2.0 but ships no licence text",
                )
                for entry in declared:
                    self.assertTrue(
                        (path.parent / entry).is_file(),
                        f"{name} points at a licence file that is not there: {entry}",
                    )

    def test_the_two_licence_files_are_the_same_text(self) -> None:
        core = (_ROOT / "LICENSE").read_text(encoding="utf-8")
        studio = (_ROOT / "studio" / "LICENSE").read_text(encoding="utf-8")
        self.assertEqual(core, studio, "the two copies of the licence have drifted")


if __name__ == "__main__":
    unittest.main()
