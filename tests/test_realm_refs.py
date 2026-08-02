from __future__ import annotations

import os
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from optpilot.realm.config import REALM_ROOT_ENV, default_realm_root, prepare_private_directory
from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.refs import (
    BlobRef,
    CandidateRef,
    SnapshotRef,
    canonical_json_bytes,
    parse_content_ref,
    parse_physical_content_ref,
    request_digest,
)


class RealmRefTests(unittest.TestCase):
    def test_canonical_json_and_request_digest_are_order_independent(self) -> None:
        left = {"z": [1, True], "a": {"b": "value"}}
        right = {"a": {"b": "value"}, "z": [1, True]}

        self.assertEqual(canonical_json_bytes(left), canonical_json_bytes(right))
        self.assertEqual(request_digest(left), request_digest(right))

    def test_content_ref_domains_do_not_collide(self) -> None:
        payload = b"same-payload"
        blob = BlobRef.from_bytes(payload)
        tree = SnapshotRef.from_manifest_bytes(payload)

        self.assertNotEqual(blob.digest, tree.digest)
        self.assertEqual(parse_content_ref(str(blob)), blob)
        self.assertEqual(parse_content_ref(str(tree)), tree)

    def test_candidate_identity_excludes_display_id_and_storage_location(self) -> None:
        tree = SnapshotRef.from_manifest_bytes(b"manifest")
        semantic_spec = {"entrypoint": "src/main.py", "parameters": {"seed": 7}}

        first = CandidateRef.build(
            candidate_format="files",
            spec=semantic_spec,
            content_refs=[tree],
        )
        # Candidate ids and host/store paths are intentionally not accepted by
        # the identity constructor, so relocating the same semantic envelope
        # cannot affect the ref.
        second = CandidateRef.build(
            candidate_format="files",
            spec={"parameters": {"seed": 7}, "entrypoint": "src/main.py"},
            content_refs=[tree],
        )

        self.assertEqual(first, second)
        self.assertEqual(parse_content_ref(str(first)), first)
        with self.assertRaises(ValueError):
            parse_physical_content_ref(str(first))

    def test_candidate_content_closure_is_ordered_as_a_set(self) -> None:
        blob = BlobRef.from_bytes(b"payload")
        tree = SnapshotRef.from_manifest_bytes(b"manifest")

        first = CandidateRef.build(
            candidate_format="files",
            spec={"entrypoint": "main.py"},
            content_refs=[blob, tree, blob],
        )
        second = CandidateRef.build(
            candidate_format="files",
            spec={"entrypoint": "main.py"},
            content_refs=[tree, blob],
        )

        self.assertEqual(first, second)

    def test_candidate_constructor_rejects_ambiguous_envelopes(self) -> None:
        with self.assertRaises(ValueError):
            CandidateRef.build(candidate_format="", spec={}, content_refs=[])
        with self.assertRaises(TypeError):
            CandidateRef.build(  # type: ignore[arg-type]
                candidate_format="files",
                spec={},
                content_refs=[CandidateRef("0" * 64)],
            )

    def test_ref_parsers_reject_wrong_domains_and_malformed_digests(self) -> None:
        with self.assertRaises(ValueError):
            BlobRef.parse("tree:sha256:" + "0" * 64)
        with self.assertRaises(ValueError):
            SnapshotRef.parse("tree:sha256:ABC")
        with self.assertRaises(ValueError):
            parse_content_ref("sha256:" + "0" * 64)
        with self.assertRaises(ValueError):
            parse_content_ref(None)  # type: ignore[arg-type]


class RealmConfigTests(unittest.TestCase):
    def test_override_does_not_resolve_away_terminal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)

            with patch.dict(os.environ, {REALM_ROOT_ENV: str(alias)}):
                configured = default_realm_root()

            self.assertEqual(configured, alias.absolute())
            with self.assertRaises(RealmIntegrityError):
                prepare_private_directory(configured)

    @unittest.skipIf(os.name == "nt", "POSIX permission assertion")
    def test_private_directory_is_created_without_group_or_other_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "realm"
            prepared = prepare_private_directory(path)
            self.assertEqual(prepared.stat().st_mode & 0o777, 0o700)

    def test_realm_migration_is_packaged_and_production_has_no_spike_dependency(self) -> None:
        migration = resources.files("optpilot.realm.migrations").joinpath("0001_realm_core.sql")
        self.assertIn("CREATE TABLE ledger_transactions", migration.read_text(encoding="utf-8"))

        realm_root = Path(__file__).resolve().parents[1] / "src" / "optpilot" / "realm"
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in realm_root.rglob("*.py")
        )
        self.assertNotIn("scripts.spikes", production)


if __name__ == "__main__":
    unittest.main()
