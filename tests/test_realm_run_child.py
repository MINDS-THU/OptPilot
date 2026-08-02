from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import CandidateRef, SnapshotRef
from optpilot.realm.run_child import (
    EXACT_PLAN_METHOD_POLICY,
    EXACT_PLAN_SOURCE_POLICY,
    MAX_EXACT_PLAN_COORDINATES,
    ChildRunCandidateAnchor,
    ChildRunEvaluationCoordinate,
    ChildRunEvaluationPlan,
    ExactPlanChildRunReceipt,
    ExactPlanChildRunLineage,
    ExactPlanChildRunRequest,
    build_exact_plan_child_run_request,
    exact_plan_child_run_identities,
)
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_projection import RunSummaryProjection
from optpilot.realm_study_runner import run_local_realm_study
from optpilot.realm_run_execution_service import (
    RUN_EXECUTION_MODE_EXACT_PLAN,
    RUN_EXECUTION_MODE_RETAINED_BATCH,
)
from optpilot.run_execution_profile import RunExecutionProfile
from optpilot.study_launch_service import _plan_context
from optpilot.study_realm_compiler import CANDIDATE_NORMALIZER_VERSION
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)
from tests.test_retained_study_service import _write_package
from tests.test_realm_retained_file_vertical_e2e import (
    _write_file_candidate_package,
)


class RealmExactPlanChildRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=(self.root / "runtime").resolve(),
            actor_principal_id="operator",
        )
        self.ledger = self.runtime.ledger
        self.store = self.runtime.content_store
        self.principal = self.runtime.principal
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="child-run",
        )
        manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=4),
            normalizer_version=CANDIDATE_NORMALIZER_VERSION,
        )
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="child-run/create-parent",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=600,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="parent-run",
            owner_id="parent-owner",
        )
        change = self.ledger.begin_owner_change(
            operation_id="child-run/admission-begin",
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        candidate_a = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 1}
        )
        candidate_b = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 2}
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id="child-run/admit-parent-plan",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission("candidate-a", candidate_a),
                    CandidateAdmission("candidate-b", candidate_b),
                ),
                logical_trials=(
                    LogicalTrialAdmission(
                        "trial-a-default", "candidate-a", seed=None, repetition_index=0
                    ),
                    LogicalTrialAdmission(
                        "trial-b", "candidate-b", seed={"fold": 2}, repetition_index=0
                    ),
                    LogicalTrialAdmission(
                        "trial-a-repeat", "candidate-a", seed=7, repetition_index=1
                    ),
                ),
            ),
        )
        revision = admitted.run.current_revision
        for index, trial_id in enumerate(
            ("trial-a-default", "trial-b", "trial-a-repeat"), start=1
        ):
            cancelled = self.ledger.cancel_run_logical_trial(
                operation_id=f"child-run/cancel/{index}",
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                logical_trial_id=trial_id,
                expected_run_revision=revision,
                controller_lease_id=self.created.controller_lease.lease_id,
                controller_holder_id=self.created.controller_lease.holder_id,
                controller_fencing_token=self.created.controller_lease.fencing_token,
                code="admin_cancelled",
            )
            revision = cancelled.run.current_revision
        draining = self.ledger.close_run_submissions(
            operation_id="child-run/close-parent",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )
        self.ledger.finish_run(
            operation_id="child-run/finish-parent",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )
        self.snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        assert self.snapshot.terminal_seal is not None
        self.parent = self.snapshot.terminal_seal.anchor
        self.anchors = tuple(
            ChildRunCandidateAnchor.from_record(item)
            for item in self.snapshot.candidates
        )
        self.child_runs = self.runtime.child_runs

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def test_builder_is_plural_exact_ordered_and_resolves_effective_seed(self) -> None:
        receipt = build_exact_plan_child_run_request(
            snapshot=self.snapshot,
            parent=self.parent,
            # Input order is not authority.  The canonical request follows the
            # immutable parent candidate acceptance order.
            selected_candidates=tuple(reversed(self.anchors)),
        )

        request = receipt.request
        self.assertEqual(request.candidates, self.anchors)
        self.assertEqual(request.method_policy, EXACT_PLAN_METHOD_POLICY)
        self.assertEqual(request.source_policy, EXACT_PLAN_SOURCE_POLICY)
        self.assertEqual(dict(request.config_overrides), {})
        self.assertEqual(request.max_trials, 3)
        self.assertEqual(
            [item.parent_budget_slot for item in request.evaluation_plan.coordinates],
            [1, 2, 3],
        )
        self.assertEqual(
            [item.seed for item in request.evaluation_plan.coordinates],
            [0, {"fold": 2}, 7],
        )
        self.assertEqual(
            [item.repetition_index for item in request.evaluation_plan.coordinates],
            [0, 0, 1],
        )
        self.assertEqual(
            receipt.parent_definition_digest, self.snapshot.definition.digest
        )
        self.assertEqual(receipt.plan_digest, request.evaluation_plan.digest)
        self.assertEqual(receipt.request_digest, request.digest)

        confirmation = request.to_internal_confirmation_dict()
        self.assertEqual(confirmation["candidate_count"], 2)
        self.assertEqual(confirmation["evaluation_plan"]["coordinate_count"], 3)
        self.assertEqual(len(confirmation["evaluation_plan"]["coordinates"]), 3)

    def test_builder_can_select_one_candidate_without_importing_parent_results(
        self,
    ) -> None:
        receipt = build_exact_plan_child_run_request(
            snapshot=self.snapshot,
            parent=self.parent,
            selected_candidates=(self.anchors[0],),
        )

        request = receipt.request
        self.assertEqual(len(request.candidates), 1)
        self.assertEqual(request.max_trials, 2)
        self.assertEqual(
            [item.parent_budget_slot for item in request.evaluation_plan.coordinates],
            [1, 3],
        )
        serialized = request.to_dict()
        self.assertNotIn("observations", serialized)
        self.assertNotIn("envelope", str(serialized))
        self.assertNotIn("content_refs", str(serialized))

    def test_atomic_create_seed_is_ordered_methodless_and_idempotent(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=self.anchors,
        )
        operation_id = "child-run/create-exact-plan"

        committed = self.child_runs.create_prepared_exact_plan(
            operation_id=operation_id,
            prepared=prepared,
        )

        self.assertEqual(committed.parent, self.parent)
        self.assertEqual(committed.request_digest, prepared.request_digest)
        self.assertLess(
            committed.creation.revision.txn_id,
            committed.admission.revision.txn_id,
        )
        self.assertEqual(committed.creation.run.current_revision, 0)
        self.assertEqual(committed.admission.run.current_revision, 1)
        self.assertEqual(
            committed.admission.run.run_id,
            exact_plan_child_run_identities(operation_id).run_id,
        )

        child = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.admission.run.run_id,
        )
        self.assertEqual(child.run.state, "running")
        self.assertEqual(child.run.current_revision, 1)
        self.assertEqual(child.run.max_trials, 3)
        self.assertEqual(child.run.accepted_logical_trials, 3)
        self.assertEqual(child.control.current_submission.state, "draining")
        self.assertEqual(child.control.current_submission.stop_code, "max_trials")
        self.assertEqual(
            [item.candidate_ref for item in child.candidates],
            [item.candidate_ref for item in self.snapshot.candidates],
        )
        self.assertEqual(
            [item.admission.seed for item in child.logical_trials],
            [0, {"fold": 2}, 7],
        )
        self.assertEqual(
            [item.admission.repetition_index for item in child.logical_trials],
            [0, 0, 1],
        )
        self.assertEqual([item.budget_slot for item in child.logical_trials], [1, 2, 3])
        self.assertEqual(child.attempts, ())
        self.assertEqual(child.observations, ())
        self.assertEqual(child.artifacts, ())
        self.assertEqual(child.method_exchange_preparations, ())
        self.assertEqual(child.method_exchange_completions, ())
        self.assertEqual(
            ExactPlanChildRunLineage.from_dict(child.definition.metadata["child_run"]),
            ExactPlanChildRunLineage.from_request(prepared.request),
        )

        replay = self.child_runs.create_prepared_exact_plan(
            operation_id=operation_id,
            prepared=prepared,
        )
        self.assertEqual(replay, committed)
        with sqlite3.connect(self.ledger.database_path) as connection:
            rows = connection.execute(
                "SELECT operation_id, operation_kind, txn_id, committed_at "
                "FROM ledger_transactions WHERE operation_id IN (?, ?) "
                "ORDER BY txn_id",
                (
                    operation_id,
                    exact_plan_child_run_identities(
                        operation_id
                    ).internal_admit_operation_id,
                ),
            ).fetchall()
            run_count = connection.execute(
                "SELECT COUNT(*) FROM run_namespaces WHERE run_id = ?",
                (child.run.run_id,),
            ).fetchone()[0]
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [
                (operation_id, "run.create"),
                (
                    exact_plan_child_run_identities(
                        operation_id
                    ).internal_admit_operation_id,
                    "run.admit",
                ),
            ],
        )
        self.assertLess(rows[0][2], rows[1][2])
        self.assertEqual(rows[0][3], rows[1][3])
        self.assertEqual(run_count, 1)

    def test_execution_profile_is_bound_to_child_request_and_lineage(self) -> None:
        profile = RunExecutionProfile(
            controller_ttl_seconds=19,
            heartbeat_interval_seconds=3,
            attempt_ttl_seconds=23,
            method_start_timeout_seconds=5,
            method_request_timeout_seconds=7,
        )
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
            execution_profile=profile,
        )
        operation_id = "child-run/profile-bound"
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id=operation_id,
            prepared=prepared,
        )

        self.assertEqual(
            prepared.request.to_dict()["execution_profile"], profile.to_dict()
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        lineage = ExactPlanChildRunLineage.from_dict(
            snapshot.definition.metadata["child_run"]
        )
        self.assertEqual(lineage.execution_profile, profile)
        self.assertAlmostEqual(
            committed.controller_lease.expires_at
            - committed.controller_lease.created_at,
            profile.controller_ttl_seconds,
        )

        changed = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
            execution_profile=replace(profile, attempt_ttl_seconds=24),
        )
        with self.assertRaises(RealmConflict):
            self.child_runs.create_prepared_exact_plan(
                operation_id=operation_id,
                prepared=changed,
            )

    def test_bootstrap_child_is_claimed_once_by_shared_dispatcher(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
        )
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/bootstrap-discovery",
            prepared=prepared,
        )

        bootstrap = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(bootstrap.run.controller_generation, 1)
        running_summary = RunSummaryProjection.from_snapshot(bootstrap)

        with mock.patch(
            "optpilot.realm_retained_batch_run_driver."
            "RealmRetainedBatchRunDriver.run",
            return_value=running_summary,
        ) as run:
            claimed = self.runtime.run_execution.execute(
                run_id=committed.run_id,
                dispatch_operation_id="child-run/bootstrap-discovery/dispatch-a",
            )
            self.assertEqual(claimed, running_summary)
            run.assert_called_once_with()

            after_claim = self.ledger.read_run_snapshot(
                actor_principal_id=self.principal.principal_id,
                run_id=committed.run_id,
            )
            self.assertEqual(after_claim.run.controller_generation, 2)
            with self.assertRaisesRegex(RealmConflict, "live fenced controller"):
                self.runtime.run_execution.execute(
                    run_id=committed.run_id,
                    dispatch_operation_id=(
                        "child-run/bootstrap-discovery/dispatch-other"
                    ),
                )

            with sqlite3.connect(self.ledger.database_path) as connection:
                transaction_count = connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions"
                ).fetchone()[0]
            replay = self.runtime.run_execution.execute(
                run_id=committed.run_id,
                dispatch_operation_id="child-run/bootstrap-discovery/dispatch-a",
            )
            self.assertEqual(replay, running_summary)
            with sqlite3.connect(self.ledger.database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM ledger_transactions"
                    ).fetchone()[0],
                    transaction_count,
                )

    def test_expired_child_controller_is_recovered_by_shared_dispatcher(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
            execution_profile=RunExecutionProfile(controller_ttl_seconds=0.5),
        )
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/expired-controller-recovery",
            prepared=prepared,
        )
        bootstrap = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )

        with mock.patch(
            "optpilot.realm_run_execution_service."
            "RealmRetainedBatchRunDriver.run",
            return_value=RunSummaryProjection.from_snapshot(bootstrap),
        ):
            self.runtime.run_execution.execute(
                run_id=committed.run_id,
                dispatch_operation_id="child-run/recovery/dispatch-a",
            )
            claimed = self.ledger.read_run_snapshot(
                actor_principal_id=self.principal.principal_id,
                run_id=committed.run_id,
            )
            self.assertEqual(claimed.run.controller_generation, 2)
            time.sleep(0.6)
            self.runtime.run_execution.execute(
                run_id=committed.run_id,
                dispatch_operation_id="child-run/recovery/dispatch-b",
            )

        recovered = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(recovered.run.controller_generation, 3)

    def test_shared_discovery_pages_study_and_child_execution_plans(self) -> None:
        child = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/shared-discovery/child",
            prepared=self.child_runs.prepare_exact_plan(
                parent_run_id=self.created.run.run_id,
                selected_candidates=(self.anchors[0],),
            ),
        )
        package = self.root / "shared-discovery-package"
        package.mkdir()
        study_path = _write_package(package)
        launch = self.runtime.study_launches.plan_local_package(
            operation_id="child-run/shared-discovery/study",
            package_root=package,
            study_config_path=study_path,
        )
        context = _plan_context(launch.job)
        starting = self.runtime.operator_jobs.begin_control_plane_start(
            job_id=launch.launch_id,
            binding_id=context["binding_id"],
            launch_token=context["launch_token"],
            evidence_fingerprint=context["evidence_fingerprint"],
            launch_request_digest=context["launch_request_digest"],
        )
        handoff = self.ledger.handoff_study_launch_to_run(
            operation_id="child-run/shared-discovery/study-handoff",
            actor_principal_id=self.principal.principal_id,
            job_id=launch.launch_id,
            expected_job_revision=starting.revision,
        ).handoff

        descriptors = self.runtime.run_execution.list_reconcilable(page_size=1)
        modes = {item.run_id: item.mode for item in descriptors}
        self.assertEqual(modes[child.run_id], RUN_EXECUTION_MODE_EXACT_PLAN)
        self.assertEqual(
            modes[handoff.run_id],
            RUN_EXECUTION_MODE_RETAINED_BATCH,
        )

    def test_two_dispatchers_race_one_bootstrap_controller_fence(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
        )
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/bootstrap-race",
            prepared=prepared,
        )
        bootstrap = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        running_summary = RunSummaryProjection.from_snapshot(bootstrap)
        barrier = threading.Barrier(2)

        original_claim = self.runtime.run_execution._claim_driver

        def synchronized_claim(**kwargs):
            barrier.wait(timeout=5)
            return original_claim(**kwargs)

        def dispatch(suffix):
            try:
                return self.runtime.run_execution.execute(
                    run_id=committed.run_id,
                    dispatch_operation_id=f"child-run/bootstrap-race/{suffix}",
                )
            except Exception as error:
                return error

        with (
            mock.patch.object(
                self.runtime.run_execution,
                "_claim_driver",
                side_effect=synchronized_claim,
            ),
            mock.patch(
                "optpilot.realm_retained_batch_run_driver."
                "RealmRetainedBatchRunDriver.run",
                return_value=running_summary,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            outcomes = tuple(executor.map(dispatch, ("dispatch-a", "dispatch-b")))

        self.assertEqual(
            sum(isinstance(item, RunSummaryProjection) for item in outcomes),
            1,
        )
        self.assertEqual(sum(isinstance(item, RealmConflict) for item in outcomes), 1)
        claimed = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(claimed.run.controller_generation, 2)

    def test_coordinate_mismatch_rolls_back_entire_child(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
        )
        request = prepared.request
        coordinates = list(request.evaluation_plan.coordinates)
        coordinates[0] = replace(coordinates[0], seed=99)
        changed = replace(
            request,
            evaluation_plan=ChildRunEvaluationPlan(
                tuple(coordinates), max_trials=len(coordinates)
            ),
        )
        operation_id = "child-run/reject-coordinate-mismatch"
        identities = exact_plan_child_run_identities(operation_id)

        with self.assertRaisesRegex(RealmConflict, "coordinates differ"):
            self.child_runs.create_exact_plan(
                operation_id=operation_id,
                request=changed,
            )

        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM run_namespaces WHERE run_id = ?",
                    (identities.run_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM owners WHERE owner_id = ?",
                    (identities.owner_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id IN (?, ?)",
                    (operation_id, identities.internal_admit_operation_id),
                ).fetchone()
            )

    def test_create_reauthorizes_derive_permission_under_write_lock(self) -> None:
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=self.created.run.run_id,
            selected_candidates=(self.anchors[0],),
        )
        delegate = self.ledger.register_principal(
            operation_id="child-run/register-unauthorized-delegate",
            principal_id="unauthorized-delegate",
            kind="agent",
        )
        operation_id = "child-run/reject-unauthorized-create"
        identities = exact_plan_child_run_identities(operation_id)

        with self.assertRaises(RealmNotFound):
            type(self.child_runs)(self.ledger, delegate).create_prepared_exact_plan(
                operation_id=operation_id,
                prepared=prepared,
            )

        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM run_namespaces WHERE run_id = ?",
                    (identities.run_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id IN (?, ?)",
                    (operation_id, identities.internal_admit_operation_id),
                ).fetchone()
            )

    def test_opaque_candidate_child_fails_closed_without_namespace(self) -> None:
        closure, bindings, source_owner_id, source_revision = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id=self.principal.principal_id,
            prefix="opaque-child-run",
            candidate_contract={"format": "opaque"},
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=2)
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        parent = self.ledger.create_run_namespace(
            operation_id="opaque-child-run/create-parent",
            actor_principal_id=self.principal.principal_id,
            controller_holder_id="opaque-child-controller",
            controller_ttl_seconds=600,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="opaque-child-parent",
            owner_id="opaque-child-parent-owner",
        )
        change = self.ledger.begin_owner_change(
            operation_id="opaque-child-run/begin-admission",
            actor_principal_id=self.principal.principal_id,
            owner_id=parent.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id="opaque-child-run/admit",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission(
                        "opaque-candidate",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="opaque",
                            spec={"token": "opaque"},
                        ),
                    ),
                ),
                logical_trials=(
                    LogicalTrialAdmission("opaque-trial", "opaque-candidate", seed=0),
                ),
            ),
        )
        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id="opaque-child-run/cancel",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            logical_trial_id="opaque-trial",
            expected_run_revision=admitted.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            code="admin_cancelled",
        )
        draining = self.ledger.close_run_submissions(
            operation_id="opaque-child-run/close",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=cancelled.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )
        self.ledger.finish_run(
            operation_id="opaque-child-run/finish",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
        )
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=parent.run.run_id,
            selected_candidates=(
                ChildRunCandidateAnchor.from_record(snapshot.candidates[0]),
            ),
        )
        operation_id = "opaque-child-run/reject-child"
        identities = exact_plan_child_run_identities(operation_id)

        with self.assertRaisesRegex(RealmConflict, "Opaque candidates"):
            self.child_runs.create_prepared_exact_plan(
                operation_id=operation_id,
                prepared=prepared,
            )

        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM run_namespaces WHERE run_id = ?",
                    (identities.run_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM owners WHERE owner_id = ?",
                    (identities.owner_id,),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM ledger_transactions WHERE operation_id IN (?, ?)",
                    (operation_id, identities.internal_admit_operation_id),
                ).fetchone()
            )

    def test_parameter_child_executes_through_normal_methodless_driver(self) -> None:
        package_root = self.root / "executable-package"
        package_root.mkdir()
        study_path = _write_package(package_root)
        preparation = self.runtime.retained_study_service.prepare_local_package(
            operation_id="child-run/executable-parent/prepare",
            actor_principal_id=self.principal.principal_id,
            store_id=self.store.store_id,
            package_root=package_root,
            study_config_path=study_path,
            source_owner_id="child-executable-source",
            study_definition_owner_id="child-executable-definition",
        )
        parent = self.runtime.retained_study_service.launch_definition_run(
            operation_id="child-run/executable-parent/launch",
            actor_principal_id=self.principal.principal_id,
            controller_holder_id="child-executable-controller",
            controller_ttl_seconds=600,
            preparation=preparation,
            run_id="child-executable-parent",
            owner_id="child-executable-parent-owner",
        )
        change = self.ledger.begin_owner_change(
            operation_id="child-run/executable-parent/begin-admission",
            actor_principal_id=self.principal.principal_id,
            owner_id=parent.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id="child-run/executable-parent/admit",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission(
                        "candidate-executable",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 0.5}
                        ),
                    ),
                ),
                logical_trials=(
                    LogicalTrialAdmission(
                        "trial-executable",
                        "candidate-executable",
                        seed=7,
                        repetition_index=0,
                    ),
                ),
            ),
        )
        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id="child-run/executable-parent/cancel-trial",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            logical_trial_id="trial-executable",
            expected_run_revision=admitted.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            code="admin_cancelled",
        )
        draining = self.ledger.close_run_submissions(
            operation_id="child-run/executable-parent/close",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=cancelled.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            stop_code="admin_cancelled",
        )
        self.ledger.finish_run(
            operation_id="child-run/executable-parent/finish",
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=parent.controller_lease.lease_id,
            controller_holder_id=parent.controller_lease.holder_id,
            controller_fencing_token=parent.controller_lease.fencing_token,
            terminal_state="cancelled",
            code="admin_cancelled",
        )
        parent_snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=parent.run.run_id,
        )
        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=parent.run.run_id,
            selected_candidates=(
                ChildRunCandidateAnchor.from_record(parent_snapshot.candidates[0]),
            ),
            execution_profile=RunExecutionProfile(
                controller_ttl_seconds=600,
                attempt_ttl_seconds=60,
            ),
        )
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/execute-parameter-plan",
            prepared=prepared,
        )

        summary = self.runtime.run_execution.execute(
            run_id=committed.run_id,
            dispatch_operation_id="child-run/execute-parameter-plan/dispatch",
        )

        self.assertEqual(summary.run_id, committed.run_id)
        self.assertEqual(summary.run_status, "succeeded")
        self.assertEqual(summary.accepted_logical_trials, 1)
        self.assertEqual(summary.successful_logical_trials, 1)
        self.assertEqual(summary.successful_objective_observations, 1)
        completed = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(completed.run.state, "succeeded")
        self.assertEqual(len(completed.attempts), 1)
        self.assertEqual(len(completed.observations), 1)
        self.assertEqual(completed.method_exchange_preparations, ())
        self.assertEqual(completed.method_exchange_completions, ())
        with sqlite3.connect(self.ledger.database_path) as connection:
            transaction_count = connection.execute(
                "SELECT COUNT(*) FROM ledger_transactions"
            ).fetchone()[0]

        replay = self.runtime.run_execution.execute(
            run_id=committed.run_id,
            dispatch_operation_id="child-run/execute-parameter-plan/replay",
        )

        self.assertEqual(replay, summary)
        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_transactions"
                ).fetchone()[0],
                transaction_count,
            )

    @unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
    def test_file_child_reuses_exact_snapshot_without_copying_content(self) -> None:
        package_root = self.root / "file-parent-package"
        package_root.mkdir()
        study_path = _write_file_candidate_package(package_root)
        parent_summary = run_local_realm_study(
            runtime=self.runtime,
            package_root=package_root,
            study_config_path=study_path,
            operation_id="child-run/file-parent",
            controller_ttl_seconds=60,
            attempt_ttl_seconds=60,
            method_start_timeout=20,
            method_request_timeout=20,
        )
        self.assertEqual(parent_summary.run_status, "succeeded")
        parent = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=parent_summary.run_id,
        )
        candidate = parent.candidates[0]
        self.assertEqual(candidate.admission.envelope.candidate_format, "files")
        self.assertEqual(len(candidate.admission.envelope.content_refs), 1)
        snapshot_ref = candidate.admission.envelope.content_refs[0]
        self.assertIsInstance(snapshot_ref, SnapshotRef)
        parent_candidate_memberships = tuple(
            item
            for item in self.ledger.list_owner_memberships(
                actor_principal_id=self.principal.principal_id,
                owner_id=parent.run.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            if item.role == RUN_CANDIDATE_ROLE and item.content_ref == snapshot_ref
        )
        self.assertEqual(len(parent_candidate_memberships), 1)
        expected_membership = parent_candidate_memberships[0]

        with sqlite3.connect(self.ledger.database_path) as connection:
            content_before = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(logical_bytes), 0), "
                "COALESCE(SUM(physical_bytes), 0) FROM content_objects"
            ).fetchone()
            edge_count_before = connection.execute(
                "SELECT COUNT(*) FROM content_edges"
            ).fetchone()[0]

        prepared = self.child_runs.prepare_exact_plan(
            parent_run_id=parent.run.run_id,
            selected_candidates=(ChildRunCandidateAnchor.from_record(candidate),),
        )
        committed = self.child_runs.create_prepared_exact_plan(
            operation_id="child-run/create-file-plan",
            prepared=prepared,
        )

        child_memberships = tuple(
            item
            for item in self.ledger.list_owner_memberships(
                actor_principal_id=self.principal.principal_id,
                owner_id=committed.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            if item.role == RUN_CANDIDATE_ROLE
        )
        self.assertEqual(
            child_memberships,
            (
                OwnerMembership(
                    expected_membership.store_id,
                    snapshot_ref,
                    RUN_CANDIDATE_ROLE,
                ),
            ),
        )
        with sqlite3.connect(self.ledger.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(logical_bytes), 0), "
                    "COALESCE(SUM(physical_bytes), 0) FROM content_objects"
                ).fetchone(),
                content_before,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM content_edges").fetchone()[0],
                edge_count_before,
            )
        child = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=committed.run_id,
        )
        self.assertEqual(
            child.candidates[0].admission.envelope.content_refs,
            (snapshot_ref,),
        )
        self.assertEqual(child.observations, ())
        self.assertEqual(child.artifacts, ())
        self.assertEqual(child.method_exchange_preparations, ())
        self.assertEqual(child.method_exchange_completions, ())

    def test_all_records_round_trip_with_stable_domain_digests(self) -> None:
        receipt = build_exact_plan_child_run_request(
            snapshot=self.snapshot,
            parent=self.parent,
            selected_candidates=self.anchors,
        )

        restored = ExactPlanChildRunReceipt.from_dict(receipt.to_dict())
        self.assertEqual(restored, receipt)
        self.assertEqual(restored.request.digest, receipt.request.digest)
        self.assertEqual(
            restored.request.evaluation_plan.digest,
            receipt.request.evaluation_plan.digest,
        )
        for before, after in zip(
            receipt.request.evaluation_plan.coordinates,
            restored.request.evaluation_plan.coordinates,
        ):
            self.assertEqual(after.digest, before.digest)

    def test_builder_rejects_unsealed_or_mismatched_parent_and_entity_anchors(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "sealed terminal parent"):
            build_exact_plan_child_run_request(
                snapshot=replace(self.snapshot, terminal_seal=None),
                parent=self.parent,
                selected_candidates=(self.anchors[0],),
            )
        with self.assertRaisesRegex(ValueError, "anchor differs"):
            build_exact_plan_child_run_request(
                snapshot=self.snapshot,
                parent=replace(self.parent, seal_digest="0" * 64),
                selected_candidates=(self.anchors[0],),
            )
        with self.assertRaisesRegex(ValueError, "candidate anchor differs"):
            build_exact_plan_child_run_request(
                snapshot=self.snapshot,
                parent=self.parent,
                selected_candidates=(
                    replace(self.anchors[0], candidate_id="different-candidate"),
                ),
            )

    def test_request_rejects_duplicate_refs_nonexact_policy_and_overrides(self) -> None:
        first = self.anchors[0]
        duplicate_ref = ChildRunCandidateAnchor(
            parent_run_id=first.parent_run_id,
            candidate_id="candidate-alias",
            candidate_ref=first.candidate_ref,
            accepted_sequence=first.accepted_sequence + 1,
        )
        coordinate = ChildRunEvaluationCoordinate(
            candidate_ref=first.candidate_ref,
            parent_logical_trial_id="trial-x",
            parent_budget_slot=1,
            seed=0,
            repetition_index=0,
        )
        plan = ChildRunEvaluationPlan((coordinate,), max_trials=1)

        with self.assertRaisesRegex(ValueError, "duplicate candidate refs"):
            ExactPlanChildRunRequest(
                parent=self.parent,
                candidates=(first, duplicate_ref),
                evaluation_plan=plan,
            )
        with self.assertRaisesRegex(ValueError, "method_policy='none'"):
            ExactPlanChildRunRequest(
                parent=self.parent,
                candidates=(first,),
                evaluation_plan=plan,
                method_policy="fresh",
            )
        with self.assertRaisesRegex(ValueError, "source_policy='reuse_exact'"):
            ExactPlanChildRunRequest(
                parent=self.parent,
                candidates=(first,),
                evaluation_plan=plan,
                source_policy="recapture",
            )
        with self.assertRaisesRegex(ValueError, "does not allow config overrides"):
            ExactPlanChildRunRequest(
                parent=self.parent,
                candidates=(first,),
                evaluation_plan=plan,
                config_overrides={"seed": 9},
            )

    def test_plan_is_nonempty_exact_bounded_ordered_and_coordinate_unique(self) -> None:
        candidate_ref = self.anchors[0].candidate_ref
        first = ChildRunEvaluationCoordinate(
            candidate_ref=candidate_ref,
            parent_logical_trial_id="trial-1",
            parent_budget_slot=1,
            seed={"fold": 1},
            repetition_index=0,
        )
        duplicate_coordinate = ChildRunEvaluationCoordinate(
            candidate_ref=candidate_ref,
            parent_logical_trial_id="trial-2",
            parent_budget_slot=2,
            seed={"fold": 1},
            repetition_index=0,
        )
        earlier = ChildRunEvaluationCoordinate(
            candidate_ref=candidate_ref,
            parent_logical_trial_id="trial-0",
            parent_budget_slot=0 + 1,
            seed=9,
            repetition_index=1,
        )

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            ChildRunEvaluationPlan((), max_trials=1)
        with self.assertRaisesRegex(ValueError, "must equal"):
            ChildRunEvaluationPlan((first,), max_trials=2)
        with self.assertRaisesRegex(ValueError, "duplicate candidate/seed/repetition"):
            ChildRunEvaluationPlan((first, duplicate_coordinate), max_trials=2)
        with self.assertRaisesRegex(ValueError, "parent budget order"):
            ChildRunEvaluationPlan(
                (replace(first, parent_budget_slot=2), earlier), max_trials=2
            )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            ChildRunEvaluationPlan(
                (first,) * (MAX_EXACT_PLAN_COORDINATES + 1),
                max_trials=MAX_EXACT_PLAN_COORDINATES + 1,
            )

    def test_contract_is_format_neutral(self) -> None:
        parameter_ref = CandidateRef.build(
            candidate_format="parameters", spec={"x": 1}, content_refs=()
        )
        opaque_ref = CandidateRef.build(
            candidate_format="opaque", spec={"token": "a"}, content_refs=()
        )
        self.assertIsInstance(parameter_ref, CandidateRef)
        self.assertIsInstance(opaque_ref, CandidateRef)

        # The public child contract carries identical typed coordinates for
        # every format; the actor-bound resolver owns format-specific content.
        for candidate_ref in (parameter_ref, opaque_ref):
            coordinate = ChildRunEvaluationCoordinate(
                candidate_ref=candidate_ref,
                parent_logical_trial_id="trial-format-neutral",
                parent_budget_slot=1,
                seed=None,
                repetition_index=0,
            )
            self.assertEqual(
                set(coordinate.to_dict()),
                {
                    "candidate_ref",
                    "parent_budget_slot",
                    "parent_logical_trial_id",
                    "repetition_index",
                    "schema",
                    "seed",
                },
            )


if __name__ == "__main__":
    unittest.main()
