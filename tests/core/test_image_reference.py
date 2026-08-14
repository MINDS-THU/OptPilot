"""Tests for the single image-reference vocabulary.

The case worth its own test is ``matches_inspection``: a repository-qualified
reference names a manifest digest, a bare reference names a config digest, and they
are different values for the same image. Comparing the wrong pair rejects every
legitimately pinned image, which fails closed and looks like a trust problem.
"""

import unittest

from optpilot.image_reference import (
    ImageReference,
    is_pinned_image_reference,
    parse_image_reference,
)

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


class ParseImageReferenceTests(unittest.TestCase):
    def test_bare_digest_names_a_config_digest(self) -> None:
        ref = parse_image_reference(DIGEST)
        self.assertEqual(ref.digest, DIGEST)
        self.assertIsNone(ref.repository)
        self.assertFalse(ref.names_manifest_digest)

    def test_repository_qualified_reference_names_a_manifest_digest(self) -> None:
        ref = parse_image_reference(f"ghcr.io/example/or-solving@{DIGEST}")
        self.assertEqual(ref.digest, DIGEST)
        self.assertEqual(ref.repository, "ghcr.io/example/or-solving")
        self.assertTrue(ref.names_manifest_digest)

    def test_nested_repository_paths_are_accepted(self) -> None:
        # The registry allows several levels, which is how a package's image and its
        # per-component overrides sit under one path.
        ref = parse_image_reference(f"ghcr.io/example/or-solving/solver@{DIGEST}")
        self.assertEqual(ref.repository, "ghcr.io/example/or-solving/solver")

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(parse_image_reference(f"  {DIGEST}  ").digest, DIGEST)

    def test_a_tag_is_refused_and_the_message_says_why(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_image_reference("ghcr.io/example/or-solving:latest")
        message = str(caught.exception)
        self.assertIn("pinned by sha256", message)
        self.assertIn("different bytes", message)

    def test_uppercase_hex_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_reference("sha256:" + "A" * 64)

    def test_short_digest_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_image_reference("sha256:" + "a" * 63)

    def test_non_strings_and_blanks_are_refused(self) -> None:
        for value in (None, 42, b"x", "", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_image_reference(value)

    def test_subject_appears_in_the_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_image_reference("nope", subject="method container image")
        self.assertIn("method container image", str(caught.exception))

    def test_is_pinned_matches_parse(self) -> None:
        for value, expected in (
            (DIGEST, True),
            (f"ghcr.io/x/y@{DIGEST}", True),
            ("ghcr.io/x/y:latest", False),
            ("ghcr.io/x/y", False),
            (None, False),
        ):
            with self.subTest(value=value):
                self.assertEqual(is_pinned_image_reference(value), expected)


class MatchesInspectionTests(unittest.TestCase):
    """The trap: Id is a config digest, RepoDigests carry manifest digests."""

    def test_bare_reference_compares_against_the_config_digest(self) -> None:
        ref = parse_image_reference(DIGEST)
        self.assertTrue(ref.matches_inspection(image_id=DIGEST, repo_digests=()))
        self.assertFalse(ref.matches_inspection(image_id=OTHER, repo_digests=()))

    def test_repository_reference_compares_against_repo_digests(self) -> None:
        ref = parse_image_reference(f"ghcr.io/example/pkg@{DIGEST}")
        self.assertTrue(
            ref.matches_inspection(
                image_id=OTHER,  # deliberately different, as it is in reality
                repo_digests=(f"ghcr.io/example/pkg@{DIGEST}",),
            )
        )

    def test_repository_reference_is_not_satisfied_by_the_config_digest(self) -> None:
        # This is the failure the old plan called B7: checking Id for a
        # repository-qualified pin rejects every correctly pinned image.
        ref = parse_image_reference(f"ghcr.io/example/pkg@{DIGEST}")
        self.assertFalse(ref.matches_inspection(image_id=DIGEST, repo_digests=()))

    def test_repository_reference_matches_any_repository_carrying_the_digest(self) -> None:
        # The same bytes may be tagged into more than one repository; the digest is
        # what identifies them.
        ref = parse_image_reference(f"ghcr.io/example/pkg@{DIGEST}")
        self.assertTrue(
            ref.matches_inspection(
                image_id=None,
                repo_digests=(f"ghcr.io/mirror/pkg@{DIGEST}",),
            )
        )

    def test_a_different_digest_in_repo_digests_does_not_match(self) -> None:
        ref = parse_image_reference(f"ghcr.io/example/pkg@{DIGEST}")
        self.assertFalse(
            ref.matches_inspection(
                image_id=None,
                repo_digests=(f"ghcr.io/example/pkg@{OTHER}",),
            )
        )

    def test_missing_inspection_data_does_not_match(self) -> None:
        self.assertFalse(parse_image_reference(DIGEST).matches_inspection(None, ()))


class StdlibOnlyTests(unittest.TestCase):
    def test_module_imports_without_third_party_packages(self) -> None:
        # The module is reachable from code running inside a container, where an
        # image is guaranteed to supply a Python interpreter and nothing else.
        import sys
        import types

        blocked = {}
        for name in ("yaml", "jsonschema", "referencing"):
            class Loud(types.ModuleType):
                def __getattr__(self, attribute: str):  # pragma: no cover - defensive
                    raise AssertionError(
                        f"image_reference must not use {name}.{attribute}"
                    )

            blocked[name] = sys.modules.get(name)
            sys.modules[name] = Loud(name)
        try:
            import importlib

            importlib.reload(importlib.import_module("optpilot.image_reference"))
        finally:
            for name, previous in blocked.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


class ImageReferenceValueTests(unittest.TestCase):
    def test_reference_is_hashable_and_comparable(self) -> None:
        first = parse_image_reference(DIGEST)
        second = parse_image_reference(DIGEST)
        self.assertEqual(first, second)
        self.assertEqual(len({first, second}), 1)
        self.assertIsInstance(first, ImageReference)


if __name__ == "__main__":
    unittest.main()
