"""The examples OptPilot ships become the person's own folders.

The property that matters most here is that a second call changes nothing.
This runs on every start, so a bug that re-copied would quietly destroy edited
work -- the one failure that cannot be apologised for afterwards.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot.example_packages import (
    install_example_packages,
    installed_example_packages,
    shipped_example_packages,
)


class ExamplePackagesTest(unittest.TestCase):
    def _shipped(self, root: Path) -> Path:
        """A stand-in for the examples inside an installation."""

        shipped = root / "shipped"
        for name in ("alpha_package", "beta_package"):
            package = shipped / name
            (package / "studies").mkdir(parents=True)
            (package / "studies" / "s.yaml").write_text("id: s\n", encoding="utf-8")
            (package / "optpilot.package.yaml").write_text(
                "identity: " + "a" * 32 + "\n", encoding="utf-8"
            )
            # Things that must never be copied out.
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (shipped / "not_a_package").mkdir()
        return shipped

    def test_first_use_copies_every_package_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            packages = root / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                result = install_example_packages(packages)
            self.assertEqual(result.installed, ("alpha_package", "beta_package"))
            self.assertEqual(result.kept, ())
            self.assertEqual(
                [p.name for p in installed_example_packages(packages)],
                ["alpha_package", "beta_package"],
            )
            self.assertTrue((packages / "alpha_package" / "studies" / "s.yaml").exists())
            self.assertTrue(
                (packages / "alpha_package" / "optpilot.package.yaml").exists(),
                "the identity must travel, or the copy loses its lineage",
            )

    def test_a_folder_without_package_content_is_not_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                self.assertEqual(
                    [p.name for p in shipped_example_packages()],
                    ["alpha_package", "beta_package"],
                )

    def test_build_leavings_are_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            packages = root / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                install_example_packages(packages)
            self.assertEqual(list(packages.rglob("__pycache__")), [])
            self.assertEqual(list(packages.rglob("*.pyc")), [])

    def test_running_again_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            packages = root / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                install_example_packages(packages)
                again = install_example_packages(packages)
            self.assertEqual(again.installed, ())
            self.assertEqual(again.kept, ("alpha_package", "beta_package"))

    def test_the_persons_own_edits_are_never_overwritten(self) -> None:
        # This runs on every start. Overwriting here would destroy work.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            packages = root / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                install_example_packages(packages)
                edited = packages / "alpha_package" / "studies" / "s.yaml"
                edited.write_text("id: s\nmine: true\n", encoding="utf-8")
                added = packages / "alpha_package" / "NOTES.md"
                added.write_text("my notes\n", encoding="utf-8")
                install_example_packages(packages)
            self.assertIn("mine: true", edited.read_text(encoding="utf-8"))
            self.assertTrue(added.exists())

    def test_a_deleted_package_stays_deleted(self) -> None:
        # Deleting an example is a decision, not damage to be repaired.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = self._shipped(root)
            packages = root / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=shipped
            ):
                install_example_packages(packages)
                import shutil

                shutil.rmtree(packages / "beta_package")
                result = install_example_packages(packages, only=["alpha_package"])
            self.assertEqual(result.installed, ())
            self.assertFalse((packages / "beta_package").exists())

    def test_an_installation_without_examples_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packages = Path(tmp) / "packages"
            with patch(
                "optpilot.example_packages.shipped_examples_root", return_value=None
            ):
                result = install_example_packages(packages)
            self.assertEqual(result.installed, ())
            self.assertEqual(result.kept, ())

class SourceCheckoutTest(unittest.TestCase):
    """A checkout's own catalog is never copied anywhere.

    With an editable install the shipped examples resolve to the repository's
    own catalog directory, which Studio already finds where it sits. Copying
    it out produced a second copy of every package, and a catalog holding two
    of everything refuses to load at all -- so every developer working from a
    checkout would have had an unusable Catalog page.
    """

    def test_a_repository_catalog_is_not_treated_as_shipped(self) -> None:
        from optpilot.example_packages import _is_source_checkout

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "OptPilot"
            (repo / "src" / "optpilot").mkdir(parents=True)
            (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            catalog = repo / "catalog"
            catalog.mkdir()
            self.assertTrue(_is_source_checkout(catalog))

    def test_an_installed_copy_is_treated_as_shipped(self) -> None:
        from optpilot.example_packages import _is_source_checkout

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site-packages"
            examples = site / "optpilot_examples"
            examples.mkdir(parents=True)
            self.assertFalse(_is_source_checkout(examples))



if __name__ == "__main__":
    unittest.main()
