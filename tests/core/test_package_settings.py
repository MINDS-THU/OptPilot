"""Tests for a package's own settings, and the identity that lives in them.

The case these exist for: a package that is moved to a different directory must
stay the same package. Before this file existed, its published update authority
was anchored to the folder's absolute path, so moving it forked the package in
two with no warning and no way back.
"""

import tempfile
import unittest
from pathlib import Path

from optpilot.image_reference import parse_image_reference
from optpilot.package_settings import (
    PACKAGE_SETTINGS_FILENAMES,
    ContainerImageDeclaration,
    resolve_component_image,
    ensure_package_identity,
    find_package_settings_path,
    load_package_settings,
    new_package_identity,
    package_identity,
    PackageSettings,
    validate_package_identity,
    write_package_settings,
)


class IdentityValueTests(unittest.TestCase):
    def test_generated_identities_are_well_formed_and_distinct(self) -> None:
        first, second = new_package_identity(), new_package_identity()
        self.assertEqual(len(first), 32)
        self.assertEqual(validate_package_identity(first), first)
        self.assertNotEqual(first, second)

    def test_malformed_identities_are_refused(self) -> None:
        for value in (None, 42, "", "xyz", "A" * 32, "a" * 31, "a" * 33, "g" * 32):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_package_identity(value)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        identity = new_package_identity()
        self.assertEqual(validate_package_identity(f"  {identity}  "), identity)


class PackageSettingsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "my_package"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_absent_file_reads_as_no_settings(self) -> None:
        self.assertIsNone(find_package_settings_path(self.root))
        self.assertIsNone(load_package_settings(self.root))
        self.assertIsNone(package_identity(self.root))

    def test_reading_never_writes(self) -> None:
        before = sorted(p.name for p in self.root.iterdir())
        load_package_settings(self.root)
        package_identity(self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_written_settings_round_trip(self) -> None:
        identity = new_package_identity()
        path = write_package_settings(
            self.root, identity=identity, description="A package."
        )
        self.assertEqual(path.name, PACKAGE_SETTINGS_FILENAMES[0])
        loaded = load_package_settings(self.root)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.identity, identity)
        self.assertEqual(loaded.description, "A package.")
        self.assertEqual(loaded.package_root, self.root)

    def test_the_file_explains_when_to_change_the_identity_and_when_not(
        self,
    ) -> None:
        """Both cases, because only warning about one of them misleads.

        This used to assert a flat "Do not edit", which is right for moving a
        package and wrong for the other reason anyone opens this file: copying
        one to make a variant. Obeying it while copying leaves two packages
        claiming to be the same one -- and obeying it the other natural way,
        by deleting the line, made the package unreadable.
        """

        write_package_settings(self.root, identity=new_package_identity())
        text = (self.root / PACKAGE_SETTINGS_FILENAMES[0]).read_text()
        self.assertIn("moving or renaming", text.lower())
        self.assertIn("SEPARATE package", text)
        self.assertIn("Do not delete the line", text)

    def test_alternate_filename_is_accepted(self) -> None:
        identity = new_package_identity()
        (self.root / PACKAGE_SETTINGS_FILENAMES[1]).write_text(
            f"apiVersion: optpilot.io/v1\nconfig: package\nidentity: {identity}\n"
        )
        self.assertEqual(package_identity(self.root), identity)

    def test_descriptions_needing_quotes_round_trip(self) -> None:
        tricky = "Scheduling: vehicles, routes & {rules}"
        write_package_settings(
            self.root, identity=new_package_identity(), description=tricky
        )
        self.assertEqual(load_package_settings(self.root).description, tricky)


class MalformedFileTests(unittest.TestCase):
    """A broken file must not degrade quietly to the old path-based anchor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "pkg"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str) -> None:
        (self.root / PACKAGE_SETTINGS_FILENAMES[0]).write_text(text)

    def test_missing_identity_raises(self) -> None:
        self._write("apiVersion: optpilot.io/v1\nconfig: package\n")
        with self.assertRaises(ValueError):
            load_package_settings(self.root)

    def test_wrong_config_kind_raises(self) -> None:
        self._write(
            f"apiVersion: optpilot.io/v1\nconfig: study\nidentity: {new_package_identity()}\n"
        )
        with self.assertRaises(ValueError) as caught:
            load_package_settings(self.root)
        self.assertIn("config: package", str(caught.exception))

    def test_wrong_api_version_raises(self) -> None:
        self._write(
            f"apiVersion: optpilot.io/v2\nconfig: package\nidentity: {new_package_identity()}\n"
        )
        with self.assertRaises(ValueError):
            load_package_settings(self.root)

    def test_invalid_yaml_raises_naming_the_file(self) -> None:
        self._write("apiVersion: [unclosed\n")
        with self.assertRaises(ValueError) as caught:
            load_package_settings(self.root)
        self.assertIn(PACKAGE_SETTINGS_FILENAMES[0], str(caught.exception))

    def test_non_mapping_raises(self) -> None:
        self._write("- a\n- b\n")
        with self.assertRaises(ValueError):
            load_package_settings(self.root)


class EnsureIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "pkg"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_an_identity_when_there_is_none(self) -> None:
        identity = ensure_package_identity(self.root)
        self.assertEqual(package_identity(self.root), identity)

    def test_is_idempotent_and_never_replaces_an_identity(self) -> None:
        first = ensure_package_identity(self.root)
        for _ in range(3):
            self.assertEqual(ensure_package_identity(self.root), first)

    def test_existing_description_is_not_clobbered(self) -> None:
        write_package_settings(
            self.root, identity=new_package_identity(), description="Original."
        )
        ensure_package_identity(self.root, description="Replacement.")
        self.assertEqual(load_package_settings(self.root).description, "Original.")


class MovingAPackageTests(unittest.TestCase):
    """The behaviour this whole file exists to guarantee."""

    def test_identity_survives_moving_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "somewhere" / "my_package"
            identity = ensure_package_identity(origin)

            destination = base / "elsewhere" / "deeper" / "my_package"
            destination.parent.mkdir(parents=True)
            origin.rename(destination)

            self.assertEqual(package_identity(destination), identity)

    def test_identity_survives_renaming_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "old_name"
            identity = ensure_package_identity(origin)
            destination = base / "new_name"
            origin.rename(destination)
            self.assertEqual(package_identity(destination), identity)

    def test_two_packages_never_share_an_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertNotEqual(
                ensure_package_identity(base / "one"),
                ensure_package_identity(base / "two"),
            )

    def test_copying_a_package_copies_its_identity(self) -> None:
        # Worth pinning as known behaviour rather than a surprise: a copied
        # folder is the same package until someone gives the copy a new
        # identity. Publishing both would be a conflict, not a silent fork.
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "original"
            identity = ensure_package_identity(origin)
            copy = base / "copy"
            shutil.copytree(origin, copy)
            self.assertEqual(package_identity(copy), identity)


if __name__ == "__main__":
    unittest.main()


DIGEST = "sha256:" + "c" * 64


class PackageImageTests(unittest.TestCase):
    """The package-wide image: declared once, used by every component in it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "pkg"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, body: str) -> None:
        (self.root / PACKAGE_SETTINGS_FILENAMES[0]).write_text(
            "apiVersion: optpilot.io/v1\nconfig: package\n"
            f"identity: {new_package_identity()}\n" + body
        )

    def test_a_package_may_declare_no_image(self) -> None:
        self._write("")
        self.assertIsNone(load_package_settings(self.root).container)

    def test_a_declared_image_is_read(self) -> None:
        self._write(
            f"runtime:\n  container:\n    image: ghcr.io/example/or-solving@{DIGEST}\n"
            "    platform: linux/amd64\n"
        )
        container = load_package_settings(self.root).container
        self.assertEqual(container.platform, "linux/amd64")
        self.assertEqual(container.image.digest, DIGEST)
        self.assertEqual(container.image.repository, "ghcr.io/example/or-solving")

    def test_a_tag_is_refused(self) -> None:
        self._write(
            "runtime:\n  container:\n    image: ghcr.io/example/or-solving:latest\n"
            "    platform: linux/amd64\n"
        )
        with self.assertRaises(ValueError) as caught:
            load_package_settings(self.root)
        self.assertIn("pinned by sha256", str(caught.exception))

    def test_platform_is_required(self) -> None:
        # The same reference on a different architecture is different bytes.
        self._write(
            f"runtime:\n  container:\n    image: ghcr.io/example/x@{DIGEST}\n"
        )
        with self.assertRaises(ValueError) as caught:
            load_package_settings(self.root)
        self.assertIn("platform is required", str(caught.exception))

    def test_unknown_keys_are_refused(self) -> None:
        self._write(
            f"runtime:\n  container:\n    image: ghcr.io/example/x@{DIGEST}\n"
            "    platform: linux/amd64\n    build: ./Dockerfile\n"
        )
        with self.assertRaises(ValueError) as caught:
            load_package_settings(self.root)
        self.assertIn("build", str(caught.exception))

    def test_an_image_survives_rewriting_the_file(self) -> None:
        declared = ContainerImageDeclaration(
            image=parse_image_reference(f"ghcr.io/example/x@{DIGEST}"),
            platform="linux/arm64",
        )
        identity = new_package_identity()
        write_package_settings(self.root, identity=identity, container=declared)
        reloaded = load_package_settings(self.root)
        self.assertEqual(reloaded.container.image.raw, declared.image.raw)
        self.assertEqual(reloaded.container.platform, "linux/arm64")


class ImageResolutionTests(unittest.TestCase):
    """Its own if it names one, else its package's, else OptPilot's default."""

    def setUp(self) -> None:
        self.package_image = ContainerImageDeclaration(
            image=parse_image_reference(f"ghcr.io/example/pkg@{DIGEST}"),
            platform="linux/amd64",
        )
        self.component_image = ContainerImageDeclaration(
            image=parse_image_reference("sha256:" + "d" * 64),
            platform="linux/amd64",
        )

    def _settings(self, container):
        return PackageSettings(
            path=Path("/tmp/x/optpilot.package.yaml"),
            identity=new_package_identity(),
            container=container,
        )

    def test_a_component_uses_its_own_image_when_it_names_one(self) -> None:
        resolved = resolve_component_image(
            self.component_image, self._settings(self.package_image)
        )
        self.assertIs(resolved, self.component_image)

    def test_a_component_falls_back_to_the_package_image(self) -> None:
        resolved = resolve_component_image(None, self._settings(self.package_image))
        self.assertIs(resolved, self.package_image)

    def test_neither_declared_means_the_default_image(self) -> None:
        self.assertIsNone(resolve_component_image(None, self._settings(None)))
        self.assertIsNone(resolve_component_image(None, None))

    def test_an_override_does_not_inherit_the_package_platform(self) -> None:
        # Resolution stops at the first declaration; nothing is merged, so a
        # component override is read exactly as written.
        override = ContainerImageDeclaration(
            image=parse_image_reference("sha256:" + "e" * 64), platform="linux/arm64"
        )
        resolved = resolve_component_image(
            override, self._settings(self.package_image)
        )
        self.assertEqual(resolved.platform, "linux/arm64")
