"""Release-scale acceptance for bounded Run Overview and Workbench reads.

The ordinary examples intentionally stay small.  This fixture is large enough
to cross both public page and objective-series limits while remaining cheap and
deterministic in CI.  The budgets emphasize stable query-count and response-size
invariants; the wall-clock ceiling is only a generous regression backstop.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_overview import (
    RUN_OVERVIEW_MAX_OBJECTIVE_POINTS,
    RUN_OVERVIEW_MAX_RESPONSE_BYTES,
)
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_views import RealmRunViewService, RunViewRef
from optpilot.realm.run_workbench import (
    RUN_WORKBENCH_KINDS,
    RUN_WORKBENCH_MAX_PAGE_SIZE,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


SCALE_CANDIDATE_COUNT = 128
SCALE_PAGE_SIZE = RUN_WORKBENCH_MAX_PAGE_SIZE
MAX_SELECTS_FOR_BUNDLE_AND_NEXT_PAGE = (2 * SCALE_CANDIDATE_COUNT) + 160
MAX_SERIALIZED_PAGE_BYTES = 512 * 1024
MAX_SERIALIZED_FIRST_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_BOUNDED_READ_SECONDS = 15.0


class RealmRunQueryScaleAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.principal = self.ledger.register_principal(
            operation_id="run-query-scale/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="run-query-scale/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            closure,
            closure_bindings,
            source_owner_id,
            source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="run-query-scale",
        )
        manifest = prepare_test_run_control_manifest(
            closure,
            max_trials=SCALE_CANDIDATE_COUNT,
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            closure,
            manifest,
            closure_bindings,
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="run-query-scale/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=120,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-query-scale",
            owner_id="run-query-scale-owner",
        )
        self.operation_index = 0
        self.run_revision = 0
        self.owner_revision = 0
        self._populate_release_scale_run()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _operation(self, label: str) -> str:
        self.operation_index += 1
        return f"run-query-scale/{self.operation_index}/{label}"

    def _controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _populate_release_scale_run(self) -> None:
        candidates = tuple(
            CandidateAdmission(
                candidate_id=f"candidate-{index:04d}",
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec={"x": index},
                ),
                lineage={"parents": []},
                generator={"method_id": "test-method"},
            )
            for index in range(SCALE_CANDIDATE_COUNT)
        )
        trials = tuple(
            LogicalTrialAdmission(
                logical_trial_id=f"trial-{index:04d}",
                candidate_id=f"candidate-{index:04d}",
                seed=17,
                repetition_index=0,
            )
            for index in range(SCALE_CANDIDATE_COUNT)
        )
        change = self.ledger.begin_owner_change(
            operation_id=self._operation("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=120,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id=self._operation("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=RunAdmissionPlan(candidates, trials),
            **self._controller_arguments(),
        )
        self.run_revision = admitted.revision.revision
        self.owner_revision = admitted.owner_commit.owner_revision

        for index in range(SCALE_CANDIDATE_COUNT):
            attempt_id = f"attempt-{index:04d}"
            prepared = self.ledger.prepare_run_attempt(
                operation_id=self._operation(f"prepare-{index:04d}"),
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                logical_trial_id=f"trial-{index:04d}",
                attempt_id=attempt_id,
                expected_run_revision=self.run_revision,
                **self._controller_arguments(),
            )
            self.run_revision = prepared.revision.revision
            envelope = AttemptEnvelope(
                attempt_id=attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                outcome="success",
                phase="environment_evaluation",
                wall_clock_seconds=0.01,
                validation={"accepted": True, "errors": []},
                materialization={"runtime_spec": {}, "metadata": {}},
                metric_values={"score": float(index)},
                constraint_results={},
                output_declarations=(),
                event_summary={},
                execution_metadata={"worker": "scale-fixture"},
            )
            adopted = self.ledger.adopt_run_attempt(
                operation_id=self._operation(f"adopt-{index:04d}"),
                actor_principal_id="operator",
                run_id=self.created.run.run_id,
                attempt_id=attempt_id,
                expected_run_revision=self.run_revision,
                expected_owner_revision=self.owner_revision,
                change_id=prepared.attempt.capture_change_id,
                finalization=AttemptFinalization(
                    attempt_id=attempt_id,
                    evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                    binding_id=prepared.attempt.binding_id,
                    effective_outcome="success",
                    effective_code=None,
                    captured_artifacts=(),
                    envelope=envelope,
                ),
                **self._controller_arguments(),
            )
            self.run_revision = adopted.revision.revision
            self.owner_revision = adopted.owner_commit.owner_revision

    @staticmethod
    def _serialized_bytes(value: object) -> int:
        return len(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def test_release_scale_overview_and_pages_stay_bounded(self) -> None:
        service = RealmRunViewService(self.ledger, self.principal)
        ref = RunViewRef(run_id=self.created.run.run_id)
        select_statements: list[str] = []
        connect = self.ledger._connect

        def traced_connect():
            connection = connect()

            def trace(statement: str) -> None:
                normalized = statement.lstrip().upper()
                if normalized.startswith("SELECT") or normalized.startswith("WITH"):
                    select_statements.append(statement)

            connection.set_trace_callback(trace)
            return connection

        started = time.perf_counter()
        with mock.patch.object(self.ledger, "_connect", side_effect=traced_connect):
            bundle = service.workbench_bundle(ref=ref, limit=SCALE_PAGE_SIZE)
            candidate_first = bundle.pages["candidate"]
            candidate_second = service.workbench_page(
                ref=ref,
                kind="candidate",
                page_token=candidate_first["page"]["next_page_token"],
                limit=SCALE_PAGE_SIZE,
            )
        elapsed = time.perf_counter() - started

        overview = bundle.head.overview.to_dict()
        self.assertEqual(
            overview["counts"]["candidates"]["accepted"],
            SCALE_CANDIDATE_COUNT,
        )
        self.assertEqual(
            overview["counts"]["candidates"]["complete"],
            SCALE_CANDIDATE_COUNT,
        )
        self.assertEqual(
            overview["objective_series"]["total_complete_candidates"],
            SCALE_CANDIDATE_COUNT,
        )
        self.assertEqual(
            overview["objective_series"]["returned"],
            RUN_OVERVIEW_MAX_OBJECTIVE_POINTS,
        )
        self.assertTrue(overview["objective_series"]["truncated"])
        self.assertEqual(
            overview["best_candidate"]["candidate_id"],
            f"candidate-{SCALE_CANDIDATE_COUNT - 1:04d}",
        )
        self.assertEqual(
            overview["best_candidate"]["value"],
            float(SCALE_CANDIDATE_COUNT - 1),
        )
        self.assertLessEqual(
            self._serialized_bytes(overview),
            RUN_OVERVIEW_MAX_RESPONSE_BYTES,
        )

        for kind in RUN_WORKBENCH_KINDS:
            page = bundle.pages[kind]
            self.assertLessEqual(page["page"]["count"], SCALE_PAGE_SIZE)
            self.assertLessEqual(
                self._serialized_bytes(page),
                MAX_SERIALIZED_PAGE_BYTES,
            )
        for kind in ("candidate", "logical_trial", "attempt", "observation"):
            self.assertEqual(bundle.pages[kind]["page"]["count"], SCALE_PAGE_SIZE)
            self.assertTrue(bundle.pages[kind]["page"]["has_more"])
        self.assertEqual(bundle.pages["artifact"]["page"]["count"], 0)
        self.assertFalse(bundle.pages["artifact"]["page"]["has_more"])

        self.assertEqual(
            candidate_second["page"]["count"],
            SCALE_CANDIDATE_COUNT - SCALE_PAGE_SIZE,
        )
        self.assertFalse(candidate_second["page"]["has_more"])
        self.assertEqual(candidate_second["head"], candidate_first["head"])
        self.assertTrue(
            {item["id"] for item in candidate_first["items"]}.isdisjoint(
                item["id"] for item in candidate_second["items"]
            )
        )
        self.assertLessEqual(
            self._serialized_bytes(candidate_second),
            MAX_SERIALIZED_PAGE_BYTES,
        )

        first_bundle_payload = {
            "head": bundle.head.to_dict(),
            "pages": {kind: bundle.pages[kind] for kind in RUN_WORKBENCH_KINDS},
        }
        self.assertLessEqual(
            self._serialized_bytes(first_bundle_payload),
            MAX_SERIALIZED_FIRST_BUNDLE_BYTES,
        )

        # Two public reads are measured.  The current exact-snapshot loader has
        # one bounded candidate-ref lookup per Candidate plus a fixed set of
        # head/definition/entity queries.  This budget catches accidental
        # quadratic reads while leaving room for bounded schema evolution.
        self.assertGreaterEqual(
            len(select_statements),
            2 * SCALE_CANDIDATE_COUNT,
            "SQL tracing did not observe both exact-snapshot reads",
        )
        self.assertLessEqual(
            len(select_statements),
            MAX_SELECTS_FOR_BUNDLE_AND_NEXT_PAGE,
            f"bounded reads issued {len(select_statements)} SELECT statements",
        )
        self.assertLess(
            elapsed,
            MAX_BOUNDED_READ_SECONDS,
            f"bounded reads took {elapsed:.3f}s",
        )


if __name__ == "__main__":
    unittest.main()
