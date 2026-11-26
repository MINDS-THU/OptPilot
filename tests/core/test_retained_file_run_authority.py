from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.method_exchange_records import (
    MethodProposalExchangeInput,
    method_exchange_sequence,
)
from optpilot.realm.refs import BlobRef, request_digest
from optpilot.realm.service import RealmContentService
from optpilot.retained_file_candidates import (
    FileCandidateDraft,
    FileCandidateDraftSelection,
    FileCandidateStagingBinding,
    file_candidate_declaration_digest,
    file_candidate_draft_token,
)
from optpilot.run_authority import RetainedRunAuthority
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


_FILE_CONTRACT = {
    "format": "files",
    "context": {},
    "materialization": {
        "implementation": "builtin.workspace_bundle",
        "config": {"candidateRoot": "candidate", "entrypoint": "solver.py"},
    },
    "validation": {
        "implementation": "builtin.workspace_policy",
        "config": {
            "requiredFiles": ["solver.py"],
            "allow": ["solver.py"],
            "deny": [],
        },
    },
}


def _file_normalizer(candidate):
    result = copy.deepcopy(candidate)
    result.setdefault("format", "files")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault(
        "generator", {"method_id": "method-a", "strategy": "test"}
    )
    result.setdefault(
        "validation", copy.deepcopy(_FILE_CONTRACT["validation"])
    )
    result.setdefault(
        "materialization", copy.deepcopy(_FILE_CONTRACT["materialization"])
    )
    return result


class RetainedFileRunAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.content = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.ledger.register_principal(
            operation_id="file-authority/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="file-authority/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="file-authority",
                candidate_contract=_FILE_CONTRACT,
            )
        )
        self.manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=2),
            method_id="method-a",
            proposal_width=1,
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, self.manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="file-authority/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-file-a",
            owner_id="run-file-owner-a",
        )
        self.authority = RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=self.created,
            candidate_normalizer=_file_normalizer,
            normalizer_version=self.manifest.normalizer_version,
        )
        self.preparation = self.ledger.prepare_run_method_exchange(
            operation_id="file-authority/method/prepare",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=self.authority.controller_lease_id,
            controller_holder_id=self.authority.controller_holder_id,
            controller_fencing_token=self.authority.controller_fencing_token,
            exchange_input=MethodProposalExchangeInput(1, {}, {}),
        )
        self.authority.refresh_controller()
        self.source = self.root / "candidate-source"
        self.source.mkdir()
        self.payload = b"VALUE = 7\n"
        (self.source / "solver.py").write_bytes(self.payload)
        self.draft = self._draft()
        self.response_digest = request_digest(
            {
                "candidates": [self.draft.to_candidate()],
                "exchange_id": self.preparation.exchange_id,
                "format": "optpilot.test.file-response.v1",
            }
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _draft(self) -> FileCandidateDraft:
        lineage = {"parents": []}
        generator = {"method_id": "method-a", "strategy": "test"}
        declaration = file_candidate_declaration_digest(
            candidate_id="candidate-a",
            lineage=lineage,
            generator=generator,
            directories=(),
            files=(
                {
                    "path": "solver.py",
                    "sha256": BlobRef.from_bytes(self.payload).digest,
                    "sizeBytes": len(self.payload),
                    "executable": False,
                },
            ),
        )
        staging = self.root / "operational-staging"
        staging.mkdir()
        binding = FileCandidateStagingBinding(
            run_id=self.created.run.run_id,
            controller_generation=1,
            volume_id="candidate-volume-a",
            usage_lease_id="candidate-volume-lease-a",
            usage_fencing_token=1,
            root_path=str(staging),
        )
        token = file_candidate_draft_token(
            binding=binding,
            exchange_id=self.preparation.exchange_id,
            exchange_sequence=method_exchange_sequence(
                round_index=self.preparation.round_index,
                kind=self.preparation.kind,
            ),
            ordinal=0,
            declaration_digest=declaration,
        )
        return FileCandidateDraft(
            "candidate-a",
            FileCandidateDraftSelection(token, "candidate-00000000/files"),
            lineage,
            generator,
        )

    def _complete(self, source_resolver):
        return self.authority.complete_staged_file_method_proposal(
            self.preparation,
            candidates=(self.draft,),
            response_digest=self.response_digest,
            content_service=self.content,
            store_id=self.store.store_id,
            source_resolver=source_resolver,
            change_ttl_seconds=30,
            heartbeat_interval_seconds=5,
        )

    def test_commit_response_loss_recovers_before_touching_staging(self) -> None:
        original = self.ledger.complete_run_method_proposal_exchange
        committed = []

        def commit_then_lose_response(**kwargs):
            committed.append(original(**kwargs))
            raise RuntimeError("simulated commit response loss")

        with mock.patch.object(
            self.ledger,
            "complete_run_method_proposal_exchange",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                self._complete(lambda _ordinal, _draft: AllowedTreeSource(self.source))

        self.assertEqual(len(committed), 1)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(len(snapshot.method_exchange_completions), 1)
        self.assertEqual(len(snapshot.candidates), 1)

        untouched_resolver = mock.Mock(
            side_effect=AssertionError("replay must not touch staging")
        )
        replay = self._complete(untouched_resolver)

        self.assertEqual(replay, committed[0])
        untouched_resolver.assert_not_called()
        self.assertEqual(self.authority.run_revision, snapshot.revision.revision)
        self.assertEqual(self.authority.controller.accepted_logical_trials, 1)

    def test_failed_capture_aborts_only_that_attempt_and_retry_succeeds(self) -> None:
        with mock.patch.object(
            self.ledger,
            "hold_owner_content",
            side_effect=RuntimeError("simulated failure after seal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "failure after seal"):
                self._complete(lambda _ordinal, _draft: AllowedTreeSource(self.source))

        failed = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(failed.method_exchange_completions, ())
        self.assertEqual(failed.candidates, ())
        self.assertEqual(failed.revision.owner_revision, 0)

        receipt = self._complete(
            lambda _ordinal, _draft: AllowedTreeSource(self.source)
        )
        self.assertEqual(receipt.completion.outcome, "admitted")
        self.assertIsNotNone(receipt.admission)
        final = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        self.assertEqual(len(final.method_exchange_completions), 1)
        self.assertEqual(len(final.candidates), 1)
        self.assertEqual(final.revision.owner_revision, 1)


if __name__ == "__main__":
    unittest.main()
