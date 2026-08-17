"""Studio finds the packages a person has, not only ones beside the project.

Before this, Studio looked only in a catalog folder next to the working
directory. An installed OptPilot started onto an empty catalog no matter what
it shipped with, which is the defect that made the product installable only by
cloning the repository.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.ui.server import _default_catalog_roots


def _make_package(root: Path, name: str) -> Path:
    package = root / name
    (package / "studies").mkdir(parents=True)
    (package / "studies" / "s.yaml").write_text("id: s\n", encoding="utf-8")
    return package


class UserPackagesRootTest(unittest.TestCase):
    def test_packages_are_found_with_no_catalog_beside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            user_packages = root / "packages"
            _make_package(user_packages, "mine")
            with patch(
                "optpilot.realm.config.default_packages_root",
                return_value=user_packages,
            ):
                roots = _default_catalog_roots(project)
        self.assertEqual([p.name for p in roots], ["mine"])

    def test_a_project_catalog_and_the_persons_packages_both_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            _make_package(project / "catalog", "beside_project")
            user_packages = root / "packages"
            _make_package(user_packages, "mine")
            with patch(
                "optpilot.realm.config.default_packages_root",
                return_value=user_packages,
            ):
                roots = _default_catalog_roots(project)
        self.assertEqual(
            sorted(p.name for p in roots), ["beside_project", "mine"]
        )

    def test_starting_studio_cannot_fail_on_an_unwritable_home(self) -> None:
        # Copying the examples out is a convenience. If the home directory is
        # read-only or full, Studio must still start.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            with (
                patch(
                    "optpilot.realm.config.default_packages_root",
                    return_value=Path(tmp) / "packages",
                ),
                patch(
                    "optpilot.example_packages.install_example_packages",
                    side_effect=OSError("read-only file system"),
                ),
            ):
                roots = _default_catalog_roots(project)
        self.assertEqual(roots, [project])


if __name__ == "__main__":
    unittest.main()
