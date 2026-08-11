"""Focused checks for exact-head run comparability facts."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.refs import canonical_json_bytes
from optpilot.realm.run_closure import RunEvaluationClosure
from optpilot.realm.run_comparability import (
    RUN_COMPARABILITY_MAX_RESPONSE_BYTES,
    RUN_COMPARABILITY_PROJECTION_SCHEMA,
    RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA,
    RUN_OBJECTIVE_FINGERPRINT_SCHEMA,
    RUN_REPRODUCIBILITY_REPORT_SCHEMA,
    RunComparabilityProjection,
)
from optpilot.realm.run_views import (
    RealmRunViewService,
    RunViewRef,
    RunWorkbenchHead,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunComparabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.principal = self.ledger.register_principal(
            operation_id="comparability/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="comparability/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            closure_bindings,
            source_owner_id,
            source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id=self.principal.principal_id,
            prefix="run-comparability",
        )
        self.control = prepare_test_run_control_manifest(
            self.closure,
            max_trials=2,
        )
        self.definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            self.control,
            closure_bindings,
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="comparability/run/create",
            actor_principal_id=self.principal.principal_id,
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=self.definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=self.created.run.run_id,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _database_row_counts(self) -> dict[str, int]:
        connection = sqlite3.connect(self.ledger.database_path)
        try:
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            )
            return {
                table: int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                for table in tables
            }
        finally:
            connection.close()

    def test_projection_is_deterministic_bounded_and_opaque(self) -> None:
        first = RunComparabilityProjection.from_snapshot(self.snapshot)
        second = RunComparabilityProjection.from_snapshot(self.snapshot)

        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        payload = first.to_dict()
        self.assertEqual(payload["schema"], RUN_COMPARABILITY_PROJECTION_SCHEMA)
        self.assertLessEqual(
            len(canonical_json_bytes(payload)),
            RUN_COMPARABILITY_MAX_RESPONSE_BYTES,
        )
        self.assertRegex(
            payload["fingerprints"]["environment_evaluation"]["digest"],
            re.compile(r"^[0-9a-f]{64}$"),
        )
        self.assertRegex(
            payload["fingerprints"]["objective"]["digest"],
            re.compile(r"^[0-9a-f]{64}$"),
        )
        serialized = json.dumps(payload, sort_keys=True)
        for marker in (
            str(self.root),
            "tree:sha256:",
            "blob:sha256:",
            "candidate:sha256:",
            '"content_ref"',
            '"owner_id"',
            '"store_id"',
            '"lease_id"',
            '"path"',
            '"manifest"',
        ):
            self.assertNotIn(marker, serialized)
        self.assertNotIn("score", serialized)

    def test_environment_fingerprint_excludes_method_identity(self) -> None:
        method = replace(
            self.definition.method_revision,
            compiler_version="2",
            method_contract={
                **dict(self.definition.method_revision.method_contract),
                "compatibility": {"variant": "different-method-semantics"},
            },
        )
        method_runtime = replace(
            self.definition.prepared_method_runtime,
            method_revision_digest=method.digest,
            runtime_settings={"python": "different-managed-runtime"},
        )
        changed_definition = replace(
            self.definition,
            method_revision=method,
            prepared_method_runtime=method_runtime,
        )
        changed_snapshot = replace(
            self.snapshot,
            definition=changed_definition,
        )

        baseline = RunComparabilityProjection.from_snapshot(self.snapshot)
        changed = RunComparabilityProjection.from_snapshot(changed_snapshot)
        self.assertNotEqual(self.definition.digest, changed_definition.digest)
        self.assertNotEqual(
            self.definition.method_revision.digest,
            changed_definition.method_revision.digest,
        )
        self.assertEqual(
            baseline.environment_evaluation_fingerprint,
            changed.environment_evaluation_fingerprint,
        )
        self.assertEqual(baseline.objective_fingerprint, changed.objective_fingerprint)

    def test_objective_aggregation_changes_only_objective_fingerprint(self) -> None:
        changed_template = replace(
            self.closure.evaluation_template,
            objective={
                "primaryMetric": {"name": "score", "direction": "maximize"},
                "aggregation": {"mode": "median"},
            },
        )
        changed_closure = RunEvaluationClosure(
            self.closure.environment_revision,
            self.closure.prepared_runtime,
            changed_template,
        )
        changed_definition = replace(
            self.definition,
            evaluation_closure=changed_closure,
        )
        changed_snapshot = replace(
            self.snapshot,
            definition=changed_definition,
        )

        baseline = RunComparabilityProjection.from_snapshot(self.snapshot)
        changed = RunComparabilityProjection.from_snapshot(changed_snapshot)
        self.assertEqual(
            baseline.environment_evaluation_fingerprint,
            changed.environment_evaluation_fingerprint,
        )
        self.assertNotEqual(
            baseline.objective_fingerprint,
            changed.objective_fingerprint,
        )

    def test_projection_labels_conservative_whole_package_source_scope(self) -> None:
        projection = RunComparabilityProjection.from_snapshot(self.snapshot).to_dict()
        environment = projection["fingerprints"]["environment_evaluation"]
        objective = projection["fingerprints"]["objective"]

        self.assertEqual(
            environment["schema"],
            RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA,
        )
        self.assertEqual(environment["source_granularity"], "whole_package")
        self.assertEqual(environment["comparison_strength"], "conservative")
        self.assertFalse(environment["method_identity_included"])
        self.assertEqual(objective["schema"], RUN_OBJECTIVE_FINGERPRINT_SCHEMA)
        self.assertEqual(
            objective["scope"],
            "primary_metric_direction_and_aggregation",
        )

    def test_reproducibility_dimensions_do_not_overclaim(self) -> None:
        payload = RunComparabilityProjection.from_snapshot(self.snapshot).to_dict()
        report = payload["reproducibility"]
        self.assertEqual(report["schema"], RUN_REPRODUCIBILITY_REPORT_SCHEMA)
        self.assertEqual(
            {key: value["status"] for key, value in report["dimensions"].items()},
            {
                "semantic_inputs": "identified",
                "bytes_available_now": "not_assessed",
                "runtime_identity": "identified",
                "runtime_available_now": "not_assessed",
                "isolation": "unverified",
                "external_replayability": "unverified",
                "seed_repetition_plan": "provisional_at_head",
                "terminal_evidence": "not_terminal",
            },
        )
        self.assertEqual(report["operator_attestation"]["status"], "absent")
        self.assertFalse(report["operator_attestation"]["upgrades_verified_dimensions"])
        ranking = payload["automatic_ranking"]
        self.assertFalse(ranking["eligible"])
        self.assertIn(
            "automatic_cross_run_ranking_not_implemented",
            ranking["blocking_reasons"],
        )
        self.assertIn("run_not_terminal", ranking["blocking_reasons"])
        self.assertIn(
            "seed_derivation_not_verified",
            ranking["blocking_reasons"],
        )

    def test_terminal_seal_upgrades_only_terminal_evidence_dimension(self) -> None:
        draining = self.ledger.close_run_submissions(
            operation_id="comparability/run/close",
            actor_principal_id=self.principal.principal_id,
            run_id=self.created.run.run_id,
            expected_run_revision=self.created.run.current_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            stop_code="method_completed",
        )
        self.ledger.finish_run(
            operation_id="comparability/run/finish",
            actor_principal_id=self.principal.principal_id,
            run_id=self.created.run.run_id,
            expected_run_revision=draining.run.current_revision,
            controller_lease_id=self.created.controller_lease.lease_id,
            controller_holder_id=self.created.controller_lease.holder_id,
            controller_fencing_token=self.created.controller_lease.fencing_token,
            terminal_state="failed",
            code="no_successful_observation",
        )
        terminal = self.ledger.read_run_snapshot(
            actor_principal_id=self.principal.principal_id,
            run_id=self.created.run.run_id,
        )
        self.assertIsNotNone(terminal.terminal_seal)

        payload = RunComparabilityProjection.from_snapshot(terminal).to_dict()
        dimensions = payload["reproducibility"]["dimensions"]
        self.assertEqual(dimensions["terminal_evidence"]["status"], "verified")
        self.assertEqual(
            dimensions["seed_repetition_plan"]["status"], "complete_at_head"
        )
        blockers = payload["automatic_ranking"]["blocking_reasons"]
        self.assertNotIn("run_not_terminal", blockers)
        self.assertNotIn("terminal_run_seal_unavailable", blockers)
        self.assertIn("seed_derivation_not_verified", blockers)
        self.assertFalse(payload["automatic_ranking"]["eligible"])

        legacy = RunComparabilityProjection.from_snapshot(
            replace(terminal, terminal_seal=None)
        ).to_dict()
        self.assertEqual(
            legacy["reproducibility"]["dimensions"]["terminal_evidence"]["status"],
            "unsealed",
        )
        self.assertIn(
            "terminal_run_seal_unavailable",
            legacy["automatic_ranking"]["blocking_reasons"],
        )

    def test_workbench_reuses_one_snapshot_and_fences_comparability_head(self) -> None:
        service = RealmRunViewService(self.ledger, self.principal)
        with mock.patch.object(
            self.ledger,
            "read_run_snapshot",
            wraps=self.ledger.read_run_snapshot,
        ) as read_snapshot:
            bundle = service.workbench_bundle(
                ref=RunViewRef(run_id=self.created.run.run_id),
            )

        read_snapshot.assert_called_once_with(
            actor_principal_id=self.principal.principal_id,
            run_id=self.created.run.run_id,
        )
        head = bundle.head
        self.assertEqual(head.comparability.head, head.head)
        self.assertEqual(head.to_dict()["comparability"]["head"], head.head)
        stale = replace(head.comparability, sequence=head.comparability.sequence + 1)
        with self.assertRaisesRegex(ValueError, "comparability facts differ"):
            RunWorkbenchHead(
                view=head.view,
                summary=head.summary,
                comparability=stale,
                overview=head.overview,
            )

    def test_derivation_creates_no_state_or_authority(self) -> None:
        before = self._database_row_counts()
        projection = RunComparabilityProjection.from_snapshot(self.snapshot)
        payload = projection.to_dict()
        after = self._database_row_counts()

        self.assertEqual(after, before)
        self.assertFalse(hasattr(RunComparabilityProjection, "from_dict"))
        self.assertEqual(
            set(payload),
            {
                "schema",
                "run_id",
                "head",
                "fingerprints",
                "reproducibility",
                "automatic_ranking",
            },
        )


if __name__ == "__main__":
    unittest.main()
