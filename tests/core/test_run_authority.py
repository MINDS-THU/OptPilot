from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.method_exchange_records import (
    MethodProposalExchangeInput,
    method_worker_response_digest,
)
from optpilot.realm.run_records import RunCandidateSelection
from optpilot.run_authority import RetainedRunAuthority
from optpilot.run_controller import MethodProtocolError
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


def _candidate(candidate_id: str, value: int) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
    }


def _candidate_normalizer(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result.setdefault("format", "parameters")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault(
        "generator", {"method_id": "method-a", "strategy": "external"}
    )
    result.setdefault("validation", {})
    result.setdefault("materialization", {})
    return result


class RetainedRunAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="authority/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="authority/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="authority",
        )
        self.manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=4),
            method_id="method-a",
            proposal_width=2,
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, self.manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="authority/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def authority(
        self, candidate_normalizer=_candidate_normalizer
    ) -> RetainedRunAuthority:
        return RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=self.created,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=self.manifest.normalizer_version,
        )

    def test_commit_precedes_controller_apply_and_advances_canonical_cursor(self) -> None:
        authority = self.authority()

        self.assertEqual(authority.controller.method_id, self.manifest.method_id)
        self.assertEqual(
            authority.controller.next_proposal_width,
            self.manifest.proposal_width,
        )

        accepted = authority.admit(
            [_candidate("candidate-a", 1), _candidate("candidate-b", 2)],
            admission_id="batch-a",
        )

        self.assertEqual(authority.run_revision, 1)
        self.assertEqual(authority.owner_revision, 0)
        self.assertEqual(authority.controller.accepted_logical_trials, 2)
        self.assertEqual(
            tuple(item.candidate["candidate_id"] for item in accepted),
            ("candidate-a", "candidate-b"),
        )
        selection = self.ledger.mint_run_candidate_selection(
            actor_principal_id="operator",
            run_id="run-a",
            candidate_id="candidate-a",
        )
        self.assertIsInstance(selection, RunCandidateSelection)
        self.assertEqual(selection.run_revision, 1)

    def test_create_authority_requires_the_manifest_normalizer_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "version does not match"):
            RetainedRunAuthority.from_create_receipt(
                ledger=self.ledger,
                actor_principal_id="operator",
                receipt=self.created,
                candidate_normalizer=_candidate_normalizer,
                normalizer_version="wrong-normalizer.v1",
            )

    def test_failed_canonical_commit_leaves_controller_and_cursor_unchanged(self) -> None:
        authority = self.authority()
        prepared = authority.preflight(
            [_candidate("candidate-a", 1)], admission_id="stale-fence"
        )
        authority.controller_fencing_token += 1

        with self.assertRaises(RealmConflict):
            authority.commit_and_apply(prepared)

        self.assertEqual(authority.run_revision, 0)
        self.assertEqual(authority.owner_revision, 0)
        self.assertEqual(authority.controller.accepted_logical_trials, 0)
        self.assertEqual(authority.controller.controller_events, ())

    def test_commit_before_cache_failure_can_replay_after_controller_restart(self) -> None:
        first = self.authority()
        prepared = first.preflight(
            [_candidate("candidate-a", 1)], admission_id="crash-window"
        )
        with mock.patch.object(
            first.controller,
            "apply_admission",
            side_effect=RuntimeError("simulated cache crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated cache crash"):
                first.commit_and_apply(prepared)
        self.assertEqual(first.run_revision, 1)
        self.assertEqual(first.controller.accepted_logical_trials, 0)

        with self.assertRaisesRegex(ValueError, "hydrate the authority"):
            self.authority()
        restarted = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_candidate_normalizer,
            normalizer_version="test-normalizer.v1",
        )

        self.assertEqual(restarted.run_revision, 1)
        self.assertEqual(restarted.owner_revision, 0)
        self.assertEqual(restarted.controller.accepted_logical_trials, 1)
        self.assertEqual(
            restarted.controller.logical_trials[0].candidate["candidate_id"],
            "candidate-a",
        )
        self.assertEqual(restarted.controller.controller_events, ())

    def test_method_proposal_completion_commits_exchange_and_admission_atomically(
        self,
    ) -> None:
        authority = self.authority()
        preparation = self.ledger.prepare_run_method_exchange(
            operation_id="authority/method/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
            exchange_input=MethodProposalExchangeInput(
                requested_width=2,
                study_state={},
                evidence={},
            ),
        )
        response_digest = method_worker_response_digest(
            {
                "schema": "optpilot.retained-python-batch-response.v2",
                "exchange_id": preparation.exchange_id,
                "ok": True,
                "result": {
                    "candidates": [
                        _candidate("candidate-a", 1),
                        _candidate("candidate-b", 2),
                    ]
                },
            }
        )
        authority.refresh_controller()

        receipt = authority.complete_method_proposal(
            preparation,
            candidates=(
                _candidate("candidate-a", 1),
                _candidate("candidate-b", 2),
            ),
            response_digest=response_digest,
        )
        snapshot = authority.refresh_controller()

        self.assertEqual(receipt.completion.outcome, "admitted")
        self.assertEqual(receipt.completion.response_digest, response_digest)
        self.assertIsNotNone(receipt.admission)
        self.assertEqual(
            receipt.completion.completed_txn_id,
            receipt.admission.revision.txn_id,
        )
        self.assertEqual(authority.run_revision, 2)
        self.assertEqual(authority.controller.accepted_logical_trials, 2)
        self.assertEqual(len(snapshot.method_exchange_completions), 1)
        self.assertEqual(
            snapshot.method_exchange_completions[0].logical_trial_ids,
            tuple(
                item.admission.logical_trial_id
                for item in receipt.admission.logical_trials
            ),
        )

    def test_method_proposal_cache_failure_hydrates_exact_atomic_completion(
        self,
    ) -> None:
        authority = self.authority()
        preparation = self.ledger.prepare_run_method_exchange(
            operation_id="authority/method-crash/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
            exchange_input=MethodProposalExchangeInput(2, {}, {}),
        )
        response_digest = method_worker_response_digest(
            {"exchange_id": preparation.exchange_id, "candidates": ["candidate-a"]}
        )
        authority.refresh_controller()

        with mock.patch.object(
            authority.controller,
            "apply_admission",
            side_effect=RuntimeError("simulated method cache crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "method cache crash"):
                authority.complete_method_proposal(
                    preparation,
                    candidates=(_candidate("candidate-a", 1),),
                    response_digest=response_digest,
                )

        restarted = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_candidate_normalizer,
            normalizer_version=self.manifest.normalizer_version,
        )
        snapshot = restarted.refresh_controller()
        self.assertEqual(restarted.controller.accepted_logical_trials, 1)
        self.assertEqual(len(snapshot.method_exchange_completions), 1)
        self.assertEqual(
            snapshot.method_exchange_completions[0].response_digest,
            response_digest,
        )

    def test_parameter_proposal_commit_response_loss_replays_stable_change(self) -> None:
        authority = self.authority()
        preparation = self.ledger.prepare_run_method_exchange(
            operation_id="authority/method-response-loss/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=authority.controller_lease_id,
            controller_holder_id=authority.controller_holder_id,
            controller_fencing_token=authority.controller_fencing_token,
            exchange_input=MethodProposalExchangeInput(2, {}, {}),
        )
        candidates = (_candidate("candidate-a", 1),)
        response_digest = method_worker_response_digest(
            {"exchange_id": preparation.exchange_id, "candidates": candidates}
        )
        authority.refresh_controller()
        original = self.ledger.complete_run_method_proposal_exchange
        committed = []

        def commit_then_lose_response(**kwargs):
            committed.append(original(**kwargs))
            raise RuntimeError("simulated parameter commit response loss")

        with mock.patch.object(
            self.ledger,
            "complete_run_method_proposal_exchange",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "response loss"):
                authority.complete_method_proposal(
                    preparation,
                    candidates=candidates,
                    response_digest=response_digest,
                )

        replay = authority.complete_method_proposal(
            preparation,
            candidates=candidates,
            response_digest=response_digest,
        )
        self.assertEqual(replay, committed[0])
        self.assertEqual(authority.controller.accepted_logical_trials, 1)
        snapshot = authority.refresh_controller()
        self.assertEqual(len(snapshot.method_exchange_completions), 1)

    def test_file_candidate_is_rejected_before_a_provisional_change(self) -> None:
        authority = self.authority()
        before = authority.controller.summary()

        with self.assertRaisesRegex(
            MethodProtocolError, "differs from the retained environment contract"
        ) as raised:
            authority.preflight(
                [
                    {
                        "candidate_id": "candidate-files",
                        "format": "files",
                        "spec": {"files": []},
                    }
                ],
                admission_id="files",
            )

        self.assertEqual(raised.exception.code, "candidate_malformed")
        self.assertEqual(raised.exception.details, {"candidate_index": 0})
        self.assertEqual(authority.controller.summary(), before)
        self.assertEqual(authority.run_revision, 0)

    def test_preflight_rejects_noncanonical_or_contract_overridden_fields(self) -> None:
        authority = self.authority()
        with self.assertRaisesRegex(
            MethodProtocolError, "exactly the standard"
        ) as extra_raised:
            authority.preflight(
                [
                    {
                        **_candidate("candidate-extra", 1),
                        "ad_hoc_runtime_override": {"unsafe": True},
                    }
                ],
                admission_id="extra-field",
            )
        self.assertEqual(extra_raised.exception.code, "candidate_malformed")
        self.assertEqual(
            extra_raised.exception.details, {"candidate_index": 0}
        )

        def overriding_normalizer(candidate):
            normalized = _candidate_normalizer(candidate)
            normalized["validation"] = {"implementation": "mutable-override"}
            return normalized

        mismatched_authority = self.authority(overriding_normalizer)
        with self.assertRaisesRegex(
            MethodProtocolError, "retained environment contract"
        ) as override_raised:
            mismatched_authority.preflight(
                [_candidate("candidate-override", 2)],
                admission_id="contract-override",
            )
        self.assertEqual(
            override_raised.exception.code, "candidate_malformed"
        )
        self.assertEqual(
            override_raised.exception.details, {"candidate_index": 0}
        )

        self.assertEqual(authority.run_revision, 0)
        self.assertEqual(mismatched_authority.run_revision, 0)


if __name__ == "__main__":
    unittest.main()
