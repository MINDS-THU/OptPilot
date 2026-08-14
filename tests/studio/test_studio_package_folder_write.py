"""Registering a package writes its folder.

Until now registration only filed work away in the permanent store and built a
display-only copy. These cover the folder write itself: that it mirrors the
whole published package rather than one registration's contribution, that the
packages shipped with OptPilot are refused as targets, and that the folder is
left editable.

The code doing the copying already existed and had never been called, so it had
no coverage at all; several of these are its first tests.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot_studio.ui import server
from optpilot_studio.ui.server import (
    CATALOG_FOLDER_OWNER_PLAN,
    CATALOG_FOLDER_OWNER_WORKSPACE,
    _apply_package_artifact_transaction,
    _is_bundled_catalog_package,
    _mirror_published_package_to_folder,
    _package_plan_catalog_folder,
)
from optpilot.package_settings import package_identity
from optpilot.realm.errors import RealmConflict


def _published_tree(root: Path) -> None:
    """A stand-in for a published package, as it appears when projected."""

    (root / "environments" / "sim").mkdir(parents=True)
    (root / "environments" / "sim" / "environment.yaml").write_text(
        "apiVersion: optpilot.io/v1\nconfig: environment\nid: sim\n"
    )
    (root / "methods" / "search").mkdir(parents=True)
    (root / "methods" / "search" / "method.yaml").write_text(
        "apiVersion: optpilot.io/v1\nconfig: method\nid: search\n"
    )


class _FakeState:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.catalog_roots: list = []
        self._configured_catalog_source_roots: dict = {}


class BundledPackagesAreReadOnlyTests(unittest.TestCase):
    def test_a_shipped_package_is_recognised(self) -> None:
        self.assertTrue(
            _is_bundled_catalog_package(Path("catalog/production_agv_scheduling"))
        )

    def test_an_unrelated_folder_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(_is_bundled_catalog_package(Path(tmp) / "mine"))

    def test_registering_into_a_shipped_package_is_refused(self) -> None:
        bundled_parent = server._bundled_catalog_root().parent
        state = _FakeState(bundled_parent)
        with self.assertRaises(RealmConflict) as caught:
            _package_plan_catalog_folder(
                state, {"id": "p"}, "production_agv_scheduling"
            )
        message = str(caught.exception)
        self.assertIn("ships with OptPilot", message)
        self.assertIn("package of your own", message)

    def test_a_users_own_package_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _FakeState(Path(tmp))
            folder = _package_plan_catalog_folder(state, {"id": "p"}, "my_package")
            self.assertEqual(folder.name, "my_package")
            self.assertEqual(folder.parent.name, "catalog")

    def test_a_folder_backed_whole_package_plan_writes_nothing(self) -> None:
        # Its folder is already the source; writing one back would be circular.
        with tempfile.TemporaryDirectory() as tmp:
            state = _FakeState(Path(tmp))
            with patch.object(
                server, "_is_configured_whole_package_plan", return_value=True
            ):
                self.assertIsNone(
                    _package_plan_catalog_folder(state, {"id": "p"}, "pkg")
                )


class MirroringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.published = self.base / "published"
        self.published.mkdir()
        _published_tree(self.published)
        self.state = _FakeState(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mirror(self) -> Path:
        with patch.object(server, "_refresh_catalog_package_roots", lambda state: None):
            return _mirror_published_package_to_folder(
                self.state,
                plan={"id": "plan-1", "workspace_id": "ws-1"},
                package_id="my_package",
                projection_root=self.published,
            )

    def test_the_whole_published_package_lands_in_the_folder(self) -> None:
        folder = self._mirror()
        self.assertTrue((folder / "environments" / "sim" / "environment.yaml").is_file())
        self.assertTrue((folder / "methods" / "search" / "method.yaml").is_file())

    def test_the_folder_is_editable(self) -> None:
        folder = self._mirror()
        target = folder / "environments" / "sim" / "environment.yaml"
        target.write_text("edited\n")  # must not raise
        self.assertEqual(target.read_text(), "edited\n")

    def test_the_folder_gets_a_durable_identity(self) -> None:
        folder = self._mirror()
        self.assertIsNotNone(package_identity(folder))

    def test_identity_is_not_replaced_on_a_second_registration(self) -> None:
        first = package_identity(self._mirror())
        (self.published / "methods" / "second").mkdir()
        (self.published / "methods" / "second" / "method.yaml").write_text(
            "apiVersion: optpilot.io/v1\nconfig: method\nid: second\n"
        )
        self.assertEqual(package_identity(self._mirror()), first)

    def test_a_second_registration_mirrors_the_whole_package_again(self) -> None:
        # The failure this guards against: a folder holding only the most recent
        # registration's files while the published package holds more.
        self._mirror()
        (self.published / "resources").mkdir()
        (self.published / "resources" / "tool.yaml").write_text(
            "apiVersion: optpilot.io/v1\nconfig: resource\nid: tool\n"
        )
        folder = self._mirror()
        self.assertTrue((folder / "resources" / "tool.yaml").is_file())
        self.assertTrue((folder / "environments" / "sim" / "environment.yaml").is_file())

    def test_files_removed_from_the_package_are_removed_from_the_folder(self) -> None:
        folder = self._mirror()
        self.assertTrue((folder / "methods" / "search").is_dir())
        shutil.rmtree(self.published / "methods")
        folder = self._mirror()
        self.assertFalse((folder / "methods").exists())

    def test_a_file_the_author_added_survives_registration(self) -> None:
        folder = self._mirror()
        note = folder / "NOTES.md"
        note.write_text("mine\n")
        self._mirror()
        self.assertTrue(note.is_file())
        self.assertEqual(note.read_text(), "mine\n")

    def test_an_empty_published_package_is_refused(self) -> None:
        for child in list(self.published.iterdir()):
            shutil.rmtree(child)
        with self.assertRaises(ValueError):
            self._mirror()

    def test_no_bookkeeping_is_left_loose_at_the_package_root(self) -> None:
        """The folder must be able to seal to the same bytes as the package.

        Anything OptPilot writes for its own use has to live under `.optpilot`,
        which sealing already skips. A loose file at the root is captured as
        package content, so a run launched from the folder would record
        something different from what was registered -- and sealing can exclude
        directories only, never individual files, so it could not be patched
        around afterwards.
        """

        folder = self._mirror()
        from optpilot.package_settings import PACKAGE_SETTINGS_FILENAMES

        allowed = {".optpilot", *PACKAGE_SETTINGS_FILENAMES}
        content = {
            entry.name for entry in folder.iterdir() if entry.name not in allowed
        }
        stray = {
            name
            for name in content
            if name.startswith(".") or "package-plan" in name
        }
        self.assertEqual(stray, set(), f"bookkeeping left at the package root: {stray}")

    def test_ownership_is_recorded_once_per_package(self) -> None:
        # Per-plan ownership is what would let a folder drift from the package.
        self.assertEqual(CATALOG_FOLDER_OWNER_WORKSPACE, "catalog")
        self.assertEqual(CATALOG_FOLDER_OWNER_PLAN, "published-head")


class FailedWriteLeavesTheFolderIntactTests(unittest.TestCase):
    def test_a_failure_mid_install_restores_the_previous_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            published = base / "published"
            published.mkdir()
            _published_tree(published)
            state = _FakeState(base)

            with patch.object(server, "_refresh_catalog_package_roots", lambda s: None):
                folder = _mirror_published_package_to_folder(
                    state,
                    plan={"id": "p", "workspace_id": "w"},
                    package_id="pkg",
                    projection_root=published,
                )
            marker = folder / "environments" / "sim" / "environment.yaml"
            original = marker.read_text()

            # A source path that vanishes part-way through the copy.
            with patch.object(
                server, "_replace_package_plan_path", side_effect=OSError("disk full")
            ):
                with self.assertRaises(Exception):
                    _apply_package_artifact_transaction(
                        state,
                        plan={
                            "workspace_id": CATALOG_FOLDER_OWNER_WORKSPACE,
                            "id": CATALOG_FOLDER_OWNER_PLAN,
                        },
                        package_root=folder,
                        owned_paths=["environments", "methods"],
                        artifact_root=published,
                    )
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(), original)


if __name__ == "__main__":
    unittest.main()
