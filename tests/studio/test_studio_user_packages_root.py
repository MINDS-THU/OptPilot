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
from types import SimpleNamespace
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


class FirstStartRegistrationTest(unittest.TestCase):
    """A package must be launchable, not merely listed.

    A Run setup can only be launched from a published version, because a run
    records exactly which bytes produced its result. Nothing published the
    examples, so a fresh install showed five packages and refused to run any.
    """

    def test_nothing_is_published_without_a_realm(self) -> None:
        from optpilot_studio.ui.server import _register_user_packages

        state = SimpleNamespace(realm_runtime=None)
        self.assertEqual(_register_user_packages(state), [])

    def test_already_published_packages_are_not_republished(self) -> None:
        # Every start calls this. Re-sealing published packages each time
        # would add seconds to startup for nothing.
        from optpilot_studio.ui.server import _register_user_packages

        with tempfile.TemporaryDirectory() as tmp:
            packages = Path(tmp) / "packages"
            _make_package(packages, "already_here")
            published = SimpleNamespace(revision=1)
            calls = []

            runtime = SimpleNamespace(
                catalog=SimpleNamespace(read_head=lambda **_k: published),
                configured_package_ingress=SimpleNamespace(
                    publish=lambda **kwargs: calls.append(kwargs)
                ),
            )
            with patch(
                "optpilot.realm.config.default_packages_root",
                return_value=packages,
            ):
                result = _register_user_packages(
                    SimpleNamespace(realm_runtime=runtime)
                )
            self.assertEqual(result, [])
            self.assertEqual(calls, [], "a published package was published again")

    def test_one_bad_package_does_not_stop_the_others(self) -> None:
        from optpilot.realm.configured_package_ingress import (
            ConfiguredPackageIngressOutcome,
        )
        from optpilot_studio.ui.server import _register_user_packages

        with tempfile.TemporaryDirectory() as tmp:
            packages = Path(tmp) / "packages"
            _make_package(packages, "aaa_broken")
            _make_package(packages, "zzz_fine")

            def publish(**kwargs):
                if kwargs["package_id"] == "aaa_broken":
                    raise RuntimeError("cannot seal this one")
                return SimpleNamespace(
                    outcome=ConfiguredPackageIngressOutcome.PUBLISHED
                )

            runtime = SimpleNamespace(
                catalog=SimpleNamespace(read_head=lambda **_k: None),
                configured_package_ingress=SimpleNamespace(publish=publish),
            )
            with (
                patch(
                    "optpilot.realm.config.default_packages_root",
                    return_value=packages,
                ),
                patch("sys.stderr"),
            ):
                result = _register_user_packages(
                    SimpleNamespace(realm_runtime=runtime)
                )
        self.assertEqual(result, ["zzz_fine"])

    def test_the_publishing_identity_follows_the_package_not_its_folder(self) -> None:
        # This is what makes re-installing a package an update rather than a
        # collision: the identity travels with the package.
        from optpilot.package_settings import new_package_identity, write_package_settings
        from optpilot_studio.ui.server import (
            _configured_package_source_identity_digest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = new_package_identity()
            first = _make_package(root / "here", "pkg")
            write_package_settings(first, identity=identity)
            second = _make_package(root / "moved_elsewhere", "pkg")
            write_package_settings(second, identity=identity)
            self.assertEqual(
                _configured_package_source_identity_digest(first),
                _configured_package_source_identity_digest(second),
            )
            self.assertEqual(
                len(_configured_package_source_identity_digest(first)), 64
            )


class ProjectShadowsUserPackagesTest(unittest.TestCase):
    """A name clash must not take the whole Catalog down.

    Two folders claiming one package id is refused, and that refusal fails the
    entire scan rather than one entry -- so a copy in the person's own folder
    sharing a name with something in the open project would leave them with no
    Catalog at all. That is easy to reach: the copies OptPilot makes are named
    after the packages they came from.
    """

    def test_the_open_project_wins_over_a_copy_of_the_same_name(self) -> None:
        from optpilot_studio.ui.server import _user_package_roots_not_shadowed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project" / "catalog"
            _make_package(project, "devs_gallery")
            user_packages = root / "packages"
            _make_package(user_packages, "devs_gallery")
            _make_package(user_packages, "only_mine")

            kept = _user_package_roots_not_shadowed(
                user_packages, [project / "devs_gallery"]
            )
        self.assertEqual([p.name for p in kept], ["only_mine"])

    def test_nothing_is_dropped_when_there_is_no_clash(self) -> None:
        from optpilot_studio.ui.server import _user_package_roots_not_shadowed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_packages = root / "packages"
            _make_package(user_packages, "alpha")
            _make_package(user_packages, "beta")
            kept = _user_package_roots_not_shadowed(user_packages, [])
        self.assertEqual(sorted(p.name for p in kept), ["alpha", "beta"])
