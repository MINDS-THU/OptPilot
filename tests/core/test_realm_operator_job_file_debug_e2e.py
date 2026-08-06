from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from optpilot.realm._validation import thaw_json
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.operator_job_records import (
    OperatorJobCleanupState,
    OperatorJobState,
)
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_records import RUN_CANDIDATE_ROLE
from optpilot.realm_study_runner import run_local_realm_study
from optpilot.runtime_binding import (
    CANDIDATE_PROJECTION_PARTITION,
    PortableAttemptRuntimeSpec,
)
from tests.core.test_realm_retained_file_vertical_e2e import (
    _ORIGINAL_SOLVER,
    _write_file_candidate_package,
)


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class FileCandidateDebugRunE2ETest(unittest.TestCase):
    def test_retained_file_selection_plans_and_executes_in_a_fresh_trial(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package_root = root / "package"
        package_root.mkdir()
        study = _write_file_candidate_package(package_root)
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)

        run = run_local_realm_study(
            runtime=runtime,
            package_root=package_root,
            study_config_path=study,
            operation_id="retained-file-debug-e2e/source-run",
            controller_ttl_seconds=60,
            attempt_ttl_seconds=60,
            method_start_timeout=20,
            method_request_timeout=20,
        )
        self.assertEqual(run.run_status, "succeeded")
        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=run.run_id,
        )
        candidate = snapshot.candidates[0]
        candidate_snapshot = candidate.admission.envelope.content_refs[0]
        self.assertIsInstance(candidate_snapshot, SnapshotRef)
        selection = runtime.ledger.mint_run_selection(
            actor_principal_id=runtime.actor_principal_id,
            run_id=run.run_id,
            kind="candidate",
            entity_id=candidate.admission.candidate_id,
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )

        planned = runtime.operator_jobs.plan_candidate_debug_run(
            operation_id="retained-file-debug-e2e/debug-run",
            selection=selection,
        )

        self.assertEqual(planned.state, OperatorJobState.QUEUED)
        self.assertIsNotNone(planned.approval)
        self.assertEqual(planned.approval.plan_digest, planned.plan_digest)
        derived_memberships = runtime.ledger.list_owner_memberships(
            actor_principal_id=runtime.actor_principal_id,
            owner_id=planned.owner_id,
        )
        self.assertEqual(
            tuple(
                membership.content_ref
                for membership in derived_memberships
                if membership.role == RUN_CANDIDATE_ROLE
            ),
            (candidate_snapshot,),
        )
        portable_spec = PortableAttemptRuntimeSpec.from_dict(
            thaw_json(planned.plan.input_facts["portable_runtime_spec"])
        )
        self.assertIsNotNone(portable_spec.file_materialization)
        self.assertEqual(
            portable_spec.file_materialization.root.relative_path,
            "candidate",
        )
        candidate_mapping = next(
            mapping
            for mapping in portable_spec.projection_spec.mappings
            if mapping.destination == CANDIDATE_PROJECTION_PARTITION
        )
        self.assertEqual(candidate_mapping.snapshot_ref, candidate_snapshot)

        # Execute through a fresh service graph so recovery reconstructs the
        # approved path-free file runtime plan instead of reusing plan-time
        # process-local state.
        runtime.close()
        runtime = LocalRealmRuntime.open(
            realm_root=root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(runtime.close)
        terminal = runtime.operator_jobs.execute(job_id=planned.job_id)

        self.assertEqual(
            terminal.state,
            OperatorJobState.SUCCEEDED,
            terminal.to_dict(),
        )
        self.assertEqual(terminal.cleanup_state, OperatorJobCleanupState.COMPLETE)
        self.assertEqual(terminal.result.result.status, "success")
        self.assertEqual(dict(terminal.result.result.metrics), {"score": 1.0})
        self.assertTrue(
            terminal.result.result.event_summary["fresh_before_mutation"]
        )
        self.assertTrue(terminal.result.result.event_summary["mutated_in_trial"])
        self.assertFalse(
            terminal.result.result.details["materialization"]["metadata"][
                "copy_performed"
            ]
        )
        self.assertEqual(
            runtime.operator_jobs.execute(job_id=planned.job_id),
            terminal,
        )

        manifest = runtime.content_store.verify_tree(candidate_snapshot)
        solver_entry = next(
            entry for entry in manifest.entries if entry.path == "solver.py"
        )
        self.assertEqual(
            solver_entry.blob_ref,
            BlobRef.from_bytes(_ORIGINAL_SOLVER.encode("utf-8")),
        )
        self.assertEqual(
            (
                package_root
                / "local_package"
                / "candidate_template"
                / "solver.py"
            ).read_text(encoding="utf-8"),
            _ORIGINAL_SOLVER,
        )


if __name__ == "__main__":
    unittest.main()
