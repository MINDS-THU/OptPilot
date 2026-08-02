from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot.candidate_staging import CandidateBundleStager
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.filesystem_quota import FilesystemQuota
from optpilot.realm.refs import BlobRef
from optpilot.retained_batch_worker import (
    _create_candidate_exchange_inbox,
    _draft_tree_declaration,
    _freeze_file_candidate_exchange,
)
from optpilot.retained_file_candidates import (
    FileCandidateDraft,
    FileCandidateDraftSelection,
    FileCandidateStagingBinding,
    durable_worker_response_digest,
    file_candidate_declaration_digest,
    file_candidate_draft_token,
    sealed_file_candidate_declaration_digest,
    sealed_file_candidate_spec,
)


def _binding(root: Path, *, generation: int = 3) -> FileCandidateStagingBinding:
    return FileCandidateStagingBinding(
        run_id="run-a",
        controller_generation=generation,
        volume_id=f"volume-{generation}",
        usage_lease_id=f"lease-{generation}",
        usage_fencing_token=generation,
        root_path=str(root),
    )


def _contract() -> dict[str, object]:
    return {
        "format": "files",
        "materialization": {
            "implementation": "builtin.workspace_bundle",
            "config": {"candidateRoot": "candidate", "entrypoint": "solver.py"},
        },
        "validation": {
            "implementation": "builtin.workspace_policy",
            "config": {
                "requiredFiles": ["solver.py"],
                "allow": ["solver.py", "lib/*"],
                "deny": ["*.secret"],
            },
        },
    }


class RetainedFileCandidateContractTests(unittest.TestCase):
    def test_worker_declaration_enforces_file_limit_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp)
            payload = selected / "oversized.bin"
            payload.write_bytes(b"1234")
            quota = FilesystemQuota(
                max_entries=10,
                max_file_bytes=3,
                max_total_bytes=20,
            )
            with mock.patch(
                "optpilot.retained_batch_worker.CANDIDATE_STAGING_QUOTA", quota
            ), mock.patch.object(
                Path, "open", side_effect=AssertionError("must reject before hashing")
            ):
                with self.assertRaisesRegex(ValueError, "oversized file"):
                    _draft_tree_declaration(selected)

    def test_worker_declaration_enforces_whole_exchange_quota_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "active").mkdir()
            (root / "frozen").mkdir()
            exchange_root, inbox = _create_candidate_exchange_inbox(
                root,
                run_id="run-a",
                exchange_id="proposal-quota",
                exchange_sequence=1,
            )
            source_a = root / "a.bin"
            source_b = root / "b.bin"
            source_a.write_bytes(b"1234")
            source_b.write_bytes(b"5678")
            stager = CandidateBundleStager(inbox)
            candidates = [
                stager.stage_file(source_a, path="a.bin", candidate_id="a"),
                stager.stage_file(source_b, path="b.bin", candidate_id="b"),
            ]
            quota = FilesystemQuota(
                max_entries=2,
                max_file_bytes=4,
                max_total_bytes=7,
            )
            with mock.patch(
                "optpilot.retained_batch_worker.CANDIDATE_STAGING_QUOTA", quota
            ):
                with self.assertRaisesRegex(ValueError, "exchange exceeds its byte quota"):
                    _freeze_file_candidate_exchange(
                        candidates,
                        binding=_binding(root),
                        exchange_root=exchange_root,
                        inbox=inbox,
                        exchange_id="proposal-quota",
                        exchange_sequence=1,
                        method_id="method-a",
                    )

    def test_draft_detaches_and_freezes_nested_json_inputs(self) -> None:
        lineage = {"parents": [{"candidate_id": "parent-a"}]}
        generator = {"method_id": "method-a", "settings": {"seed": 1}}
        draft = FileCandidateDraft(
            "candidate-a",
            FileCandidateDraftSelection(
                f"draft-v1-{'1' * 64}-{'2' * 64}",
                "candidate-00000000/files",
            ),
            lineage,
            generator,
        )
        lineage["parents"][0]["candidate_id"] = "mutated"
        generator["settings"]["seed"] = 2
        rendered = draft.to_candidate()
        self.assertEqual(
            rendered["lineage"], {"parents": [{"candidate_id": "parent-a"}]}
        )
        self.assertEqual(
            rendered["generator"],
            {"method_id": "method-a", "settings": {"seed": 1}},
        )
        with self.assertRaises(TypeError):
            draft.lineage["parents"][0]["candidate_id"] = "blocked"

    def test_draft_rejects_echoed_host_paths_and_physical_refs(self) -> None:
        for metadata in (
            {"source": "/private/tmp/staging/candidate.py"},
            {"source": "copied from /private/tmp/staging/candidate.py"},
            {"source": r"C:\\work\\candidate.py"},
            {"source": "../candidate.py"},
            {"source": "tree:sha256:" + "1" * 64},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(ValueError, "host paths or physical refs"):
                    FileCandidateDraft(
                        "candidate-a",
                        FileCandidateDraftSelection(
                            f"draft-v1-{'1' * 64}-{'2' * 64}",
                            "candidate-00000000/files",
                        ),
                        {"parents": []},
                        metadata,
                    )

    def test_draft_rejects_host_path_candidate_ids(self) -> None:
        for candidate_id in (
            "/private/tmp/candidate-a",
            r"C:\work\candidate-a",
        ):
            with self.subTest(candidate_id=candidate_id):
                with self.assertRaisesRegex(
                    ValueError, "host paths or physical refs"
                ):
                    FileCandidateDraft(
                        candidate_id,
                        FileCandidateDraftSelection(
                            f"draft-v1-{'1' * 64}-{'2' * 64}",
                            "candidate-00000000/files",
                        ),
                        {"parents": []},
                        {"method_id": "method-a"},
                    )

    def test_token_binds_declaration_and_exact_staging_fence(self) -> None:
        declaration = file_candidate_declaration_digest(
            candidate_id="candidate-a",
            lineage={"parents": []},
            generator={"method_id": "method-a", "strategy": "test"},
            directories=(),
            files=(
                {
                    "path": "solver.py",
                    "sha256": "1" * 64,
                    "sizeBytes": 4,
                    "executable": False,
                },
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = file_candidate_draft_token(
                binding=_binding(root, generation=3),
                exchange_id="proposal-a",
                exchange_sequence=1,
                ordinal=0,
                declaration_digest=declaration,
            )
            changed = file_candidate_draft_token(
                binding=_binding(root, generation=4),
                exchange_id="proposal-a",
                exchange_sequence=1,
                ordinal=0,
                declaration_digest=declaration,
            )
        self.assertNotEqual(first, changed)
        draft = FileCandidateDraft(
            "candidate-a",
            FileCandidateDraftSelection(first, "candidate-00000000/files"),
            {"parents": []},
            {"method_id": "method-a", "strategy": "test"},
        )
        self.assertEqual(draft.declaration_digest, declaration)

    def test_durable_digest_excludes_only_valid_live_token_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = "2" * 64
            candidates = []
            for generation in (1, 2):
                token = file_candidate_draft_token(
                    binding=_binding(root, generation=generation),
                    exchange_id="proposal-a",
                    exchange_sequence=1,
                    ordinal=0,
                    declaration_digest=declaration,
                )
                candidates.append(
                    FileCandidateDraft(
                        "candidate-a",
                        FileCandidateDraftSelection(
                            token, "candidate-00000000/files"
                        ),
                        {"parents": []},
                        {"method_id": "method-a", "strategy": "test"},
                    ).to_candidate()
                )
        responses = [
            {
                "exchange_id": "proposal-a",
                "ok": True,
                "result": {"candidates": [candidate]},
                "schema": "optpilot.retained-python-batch-response.v1",
            }
            for candidate in candidates
        ]
        self.assertNotEqual(responses[0], responses[1])
        self.assertEqual(
            durable_worker_response_digest(responses[0]),
            durable_worker_response_digest(responses[1]),
        )

    def test_worker_rehomes_whole_exchange_before_returning_path_free_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "active").mkdir()
            (root / "frozen").mkdir()
            binding = _binding(root)
            exchange_root, inbox = _create_candidate_exchange_inbox(
                root,
                run_id="run-a",
                exchange_id="proposal-a",
                exchange_sequence=1,
            )
            source = root / "source"
            source.mkdir()
            (source / "solver.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "empty").mkdir()
            candidate = CandidateBundleStager(inbox).stage_directory(
                source,
                candidate_id="semantic/label",
                lineage={"parents": []},
                generator={"method_id": "method-a", "strategy": "test"},
            )
            response = _freeze_file_candidate_exchange(
                [candidate],
                binding=binding,
                exchange_root=exchange_root,
                inbox=inbox,
                exchange_id="proposal-a",
                exchange_sequence=1,
                method_id="method-a",
            )
            draft = FileCandidateDraft.from_candidate(response[0])
            frozen_exchange = root / "frozen" / exchange_root.name
            self.assertFalse(exchange_root.exists())
            self.assertTrue((frozen_exchange / draft.draft.selection).is_dir())
            self.assertTrue(
                (frozen_exchange / draft.draft.selection / "empty").is_dir()
            )
            self.assertNotIn(str(root), repr(response))
            self.assertNotIn("bundleRef", repr(response))
            self.assertNotIn("contentRef", repr(response))
            self.assertFalse((root / "semantic/label").exists())

    def test_sealed_spec_and_declaration_use_only_manifest_semantics(self) -> None:
        solver = BlobRef.from_bytes(b"solver")
        helper = BlobRef.from_bytes(b"helper")
        manifest = TreeManifest.build(
            (
                TreeEntry.directory("lib"),
                TreeEntry.file(
                    "lib/helper.py",
                    blob_ref=helper,
                    size=6,
                    executable=False,
                ),
                TreeEntry.file(
                    "solver.py",
                    blob_ref=solver,
                    size=6,
                    executable=True,
                ),
            )
        )
        declaration = file_candidate_declaration_digest(
            candidate_id="candidate-a",
            lineage={"parents": []},
            generator={
                "method_id": "method-a",
                "settings": {"seed": 1},
                "strategy": "test",
            },
            directories=["lib"],
            files=[
                {
                    "path": "lib/helper.py",
                    "sha256": helper.digest,
                    "sizeBytes": 6,
                    "executable": False,
                },
                {
                    "path": "solver.py",
                    "sha256": solver.digest,
                    "sizeBytes": 6,
                    "executable": True,
                },
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            token = file_candidate_draft_token(
                binding=_binding(Path(tmp)),
                exchange_id="proposal-a",
                exchange_sequence=1,
                ordinal=0,
                declaration_digest=declaration,
            )
        draft = FileCandidateDraft(
            "candidate-a",
            FileCandidateDraftSelection(token, "candidate-00000000/files"),
            {"parents": []},
            {
                "method_id": "method-a",
                "settings": {"seed": 1},
                "strategy": "test",
            },
        )
        self.assertEqual(
            sealed_file_candidate_declaration_digest(draft, manifest), declaration
        )
        spec = sealed_file_candidate_spec(manifest, _contract())
        self.assertEqual(spec["schema"], "optpilot.sealed-file-candidate-spec.v1")
        self.assertEqual(spec["entrypoint"], "solver.py")
        self.assertEqual(spec["options"], {"candidateRoot": "candidate"})
        self.assertTrue(spec["files"][1]["executable"])
        self.assertEqual(spec["files"][1]["sha256"], solver.digest)
        rendered = repr(spec)
        self.assertNotIn("blob:", rendered)
        self.assertNotIn("tree:", rendered)
        self.assertNotIn("contentRef", rendered)
        self.assertNotIn("snapshotRef", rendered)

    def test_candidate_options_cannot_override_contract_owned_root(self) -> None:
        manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "solver.py",
                    blob_ref=BlobRef.from_bytes(b"solver"),
                    size=6,
                    executable=False,
                ),
            )
        )
        contract = _contract()
        contract["materialization"]["config"]["candidateOptions"] = {
            "candidateRoot": "replacement"
        }
        with self.assertRaisesRegex(ValueError, "contract-owned root"):
            sealed_file_candidate_spec(manifest, contract)
        for options in (
            {"CandidateRoot": "replacement"},
            {"contentREF": "blob:sha256:" + "1" * 64},
            {"note": r"C:\\host\\candidate"},
            {"note": "copied from /private/tmp/candidate"},
            {"note": "../candidate"},
        ):
            contract = _contract()
            contract["materialization"]["config"]["candidateOptions"] = options
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    sealed_file_candidate_spec(manifest, contract)

    def test_candidate_entrypoint_must_name_a_sealed_regular_file(self) -> None:
        manifest = TreeManifest.build(
            (
                TreeEntry.directory("lib"),
                TreeEntry.file(
                    "solver.py",
                    blob_ref=BlobRef.from_bytes(b"solver"),
                    size=6,
                    executable=False,
                ),
            )
        )
        for entrypoint in ("missing.py", "lib"):
            contract = _contract()
            contract["materialization"]["config"]["entrypoint"] = entrypoint
            with self.subTest(entrypoint=entrypoint):
                with self.assertRaisesRegex(ValueError, "entrypoint is absent"):
                    sealed_file_candidate_spec(manifest, contract)


if __name__ == "__main__":
    unittest.main()
