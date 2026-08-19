from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_child_service import (
    EVALUATION_PLAN_UNAVAILABLE_CODE,
    RealmChildRunService,
)
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    RunCandidateSelection,
)
from optpilot.realm.selections import SelectionRef
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmChildRunSelectionPreparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.principal = self.ledger.register_principal(
            operation_id="child-service/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="child-service/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id=self.principal.principal_id,
            prefix="child-service",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=3)
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id="child-service/create-parent",
            actor_principal_id=self.principal.principal_id,
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="parent-run",
            owner_id="parent-owner",
        )
        change = self.ledger.begin_owner_change(
            operation_id="child-service/admission-begin",
            actor_principal_id=self.principal.principal_id,
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        admission = self.ledger.commit_run_candidate_admissions(
            operation_id="child-service/admit-parent-plan",
            actor_principal_id=self.principal.principal_id,
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission(
                        "evaluated",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 1}
                        ),
                    ),
                    CandidateAdmission(
                        "unevaluated",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 2}
                        ),
                    ),
                ),
                logical_trials=(
                    LogicalTrialAdmission(
                        "trial-evaluated",
                        "evaluated",
                        seed=None,
                        repetition_index=0,
                    ),
                    LogicalTrialAdmission(
                        "trial-not-in-plan",
                        "unevaluated",
                        seed=None,
                        repetition_index=0,
                    ),
                ),
            ),
        )
        revision = admission.run.current_revision
        for index, trial_id in enumerate(
            ("trial-evaluated", "trial-not-in-plan"), start=1
        ):
            cancelled = self.ledger.cancel_run_logical_trial(
                operation_id=f"child-service/cancel/{index}",
                actor_principal_id=self.principal.principal_id,
                run_id=created.run.run_id,
                logical_trial_id=trial_id,
                expected_run_revision=revision,
                controller_lease_id=created.controller_lease.lease_id,
                controller_holder_id=created.controller_lease.holder_id,
                controller_fencing_token=created.controller_lease.fencing_token,
                code="admin_cancelled",
            )
            revision = cancelled.run.current_revision
        draining = self.ledger.close_run_submissions(
            operation_id="child-service/close",
            actor_principal_id=self.principal.principal_id,
            run_id=created.run.run_id,
            expected_run_revision=revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )
        self.ledger.finish_run(
            operation_id="child-service/finish",
            actor_principal_id=self.principal.principal_id,
            run_id=created.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )
        self.snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=created.run.run_id,
        )
        template_digest = (
            self.snapshot.definition.evaluation_closure.evaluation_template.digest
        )
        self.selections = tuple(
            SelectionRef.from_run_candidate(
                RunCandidateSelection.build(
                    run_id=self.snapshot.run.run_id,
                    evaluation_template_digest=template_digest,
                    run_revision=self.snapshot.revision.revision,
                    owner_revision=self.snapshot.revision.owner_revision,
                    sequence=candidate.accepted_sequence,
                    candidate_id=candidate.candidate_id,
                    candidate_ref=candidate.candidate_ref,
                ),
                source_owner_id=self.snapshot.run.owner_id,
                source_sequence=self.snapshot.revision.last_sequence,
            )
            for candidate in self.snapshot.candidates
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def test_batch_keeps_ready_neighbor_when_one_candidate_has_no_plan(self) -> None:
        # RunAdmissionPlan currently requires at least one trial per admitted
        # candidate. Model the valid read shape the batch seam must tolerate for
        # future/legacy snapshots without weakening that write-side invariant.
        presentation_snapshot = copy.copy(self.snapshot)
        object.__setattr__(
            presentation_snapshot,
            "logical_trials",
            (self.snapshot.logical_trials[0],),
        )
        results = RealmChildRunService.prepare_exact_plan_selection_batch_from_snapshot(
            snapshot=presentation_snapshot,
            selections=self.selections,
        )

        evaluated = results[self.selections[0].selection_digest]
        unevaluated = results[self.selections[1].selection_digest]
        self.assertTrue(evaluated.eligibility.eligible)
        self.assertIsNotNone(evaluated.prepared)
        assert evaluated.prepared is not None
        self.assertEqual(evaluated.prepared.request.max_trials, 1)
        self.assertFalse(unevaluated.eligibility.eligible)
        self.assertEqual(
            unevaluated.eligibility.code,
            EVALUATION_PLAN_UNAVAILABLE_CODE,
        )
        self.assertIsNone(unevaluated.prepared)

if __name__ == "__main__":
    unittest.main()
