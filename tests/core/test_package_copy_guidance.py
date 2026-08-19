"""Copying a package is a thing people do, and the file told them not to.

Every package carries a value identifying it, so that moving or renaming its
folder still updates the same package instead of creating a second one. The
comment above it said "Do not edit", full stop -- correct for moving, wrong for
the other reason someone opens that file, which is to copy a package and make a
variant of it.

Following that instruction while copying leaves two packages claiming to be the
same one. Following it the other natural way -- deleting the line, since you
must not edit it -- broke the package with "Package identity must be a string",
which names a type rather than a fix.

Neither is dangerous today: a copied folder under a different name registers
and appears in the Catalog perfectly well, which is why the tool once planned
for this was dropped. What was left was the wording. The wording itself is
asserted in test_package_settings.py, beside the other settings-file tests;
these cover the error a person hits and the copying path that already works.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from optpilot.package_settings import (
    ensure_package_identity,
    new_package_identity,
    package_identity,
    validate_package_identity,
)

_ROOT = Path(__file__).resolve().parents[2]


class IdentityErrorTest(unittest.TestCase):
    def test_a_missing_identity_says_how_to_add_one(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_package_identity(None)
        message = str(caught.exception)
        self.assertIn("no identity line", message)
        self.assertIn("identity:", message)
        self.assertIn("copied", message)

    def test_a_wrong_length_identity_says_the_length(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_package_identity("abc")
        self.assertIn("is 3", str(caught.exception))

    def test_a_well_formed_identity_is_returned(self) -> None:
        value = new_package_identity()
        self.assertEqual(validate_package_identity(f"  {value} "), value)


@unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs the shipped packages")
class ShippedPackagesTest(unittest.TestCase):
    def test_every_shipped_package_carries_the_current_wording(self) -> None:
        for settings in sorted((_ROOT / "catalog").glob("*/optpilot.package.yaml")):
            with self.subTest(package=settings.parent.name):
                text = settings.read_text(encoding="utf-8")
                self.assertIn("Do not delete the line", text)
                self.assertNotIn("Do not edit:", text)

    def test_every_shipped_package_still_has_a_valid_identity(self) -> None:
        for settings in sorted((_ROOT / "catalog").glob("*/optpilot.package.yaml")):
            with self.subTest(package=settings.parent.name):
                value = yaml.safe_load(settings.read_text(encoding="utf-8"))["identity"]
                self.assertEqual(validate_package_identity(value), value)

    def test_shipped_identities_are_all_different(self) -> None:
        values = [
            yaml.safe_load(path.read_text(encoding="utf-8"))["identity"]
            for path in sorted((_ROOT / "catalog").glob("*/optpilot.package.yaml"))
        ]
        self.assertEqual(len(values), len(set(values)))


class CopyingAPackageTest(unittest.TestCase):
    """The path that already works, pinned so the dropped tool stays dropped."""

    @unittest.skipUnless((_ROOT / "catalog").is_dir(), "needs a real package")
    def test_a_copy_given_a_fresh_identity_is_its_own_package(self) -> None:
        source = next((_ROOT / "catalog").glob("*/optpilot.package.yaml")).parent
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / f"{source.name}_variant"
            shutil.copytree(source, copy)
            self.assertEqual(package_identity(copy), package_identity(source))

            settings = copy / "optpilot.package.yaml"
            text = settings.read_text(encoding="utf-8")
            fresh = new_package_identity()
            settings.write_text(
                text.replace(package_identity(source), fresh), encoding="utf-8"
            )

            self.assertEqual(package_identity(copy), fresh)
            self.assertNotEqual(package_identity(copy), package_identity(source))
            # And nothing overwrites it afterwards.
            self.assertEqual(ensure_package_identity(copy), fresh)


if __name__ == "__main__":
    unittest.main()
