"""Focused checks for the bounded canonical Run Workbench read model."""

from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from optpilot.attempts import (
    AttemptEnvelope,
    AttemptFinalization,
    CapturedArtifact,
    OutputDeclaration,
)
from optpilot.realm.content import (
    AllowedFileSource,
    AllowedTreeSource,
    LocalContentStore,
)
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.run_reader import LocalRealmContext
from optpilot.realm.run_views import (
    RUN_WORKBENCH_HEAD_SCHEMA,
    RealmRunViewService,
    RunViewRef,
)
from optpilot.realm.run_attempt_records import RUN_ARTIFACT_ROLE
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_candidate_results import (
    RUN_CANDIDATE_RESULT_ORDER,
    RUN_CANDIDATE_RESULT_SCHEMA,
    CandidateResultIndex,
)
from optpilot.realm.run_overview import (
    RUN_OVERVIEW_MAX_OBJECTIVE_POINTS,
    RUN_OVERVIEW_OBJECTIVE_ORDER,
    RUN_OVERVIEW_OBJECTIVE_SERIES_SCHEMA,
    RUN_OVERVIEW_PROJECTION_SCHEMA,
    RunOverviewProjection,
)
from optpilot.realm.review_collection_ledger import REVIEW_ARTIFACT_ROLE
from optpilot.realm.review_collection_service import RealmReviewCollectionService
from optpilot.realm.review_collections import (
    REVIEW_COLLECTION_EXPORT_SCHEMA,
    REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA,
    REVIEW_COLLECTION_PUBLIC_SELECTION_SCHEMA,
    REVIEW_INSPECTION_OUTCOME_SCHEMA,
    ReviewCollectionEntryDraft,
    public_review_inspection_outcome,
)
from optpilot.realm.shortlist_service import (
    RealmShortlistService,
    ShortlistCardDraft,
    ShortlistDraft,
)
from optpilot.realm.run_candidate_comparison import (
    RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS,
    RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES,
    RUN_CANDIDATE_COMPARISON_SCHEMA,
    RunCandidateComparisonProjection,
)
from optpilot.realm.run_workbench import (
    RUN_WORKBENCH_ACTIONS,
    RUN_WORKBENCH_KINDS,
    RUN_WORKBENCH_MAX_CORRELATIONS,
    RUN_WORKBENCH_MAX_OBSERVATION_CONSTRAINTS,
    RUN_WORKBENCH_MAX_OBSERVATION_METRICS,
    RUN_WORKBENCH_MAX_PAGE_SIZE,
    RUN_WORKBENCH_MAX_TEXT_BYTES,
    RUN_WORKBENCH_PAGE_SCHEMA,
    RUN_WORKBENCH_SELECTION_SCHEMA,
    RunWorkbenchReadModel,
)
from optpilot.realm.run_timeline import RUN_TIMELINE_PAGE_SCHEMA
from optpilot.run_control_manifest import RetryPolicy
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunWorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="workbench/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="workbench/store/local-a",
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
            actor_principal_id="operator",
            prefix="run-workbench",
            candidate_contract={
                "format": "parameters",
                "validation": {
                    "implementation": "builtin.schema_validation",
                    "config": {
                        "enforceBounds": True,
                        "searchSpace": {
                            "x": {
                                "valueType": "int",
                                "type": "int",
                                "min": 0,
                                "max": 10,
                                "description": "Worker count",
                                "unit": "workers",
                            },
                            "shared": {
                                "valueType": "string",
                                "type": "string",
                            },
                            "removed": {
                                "valueType": "string",
                                "type": "string",
                            },
                        },
                        "constraints": [],
                    },
                },
            },
        )
        manifest = prepare_test_run_control_manifest(self.closure, max_trials=10)
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, manifest, closure_bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="workbench/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.operation_index = 0
        self.run_revision = 0
        self.owner_revision = 0
        self._admit_candidates()
        self.attempt_a = self._prepare("trial-a", "attempt-a1")
        self._confirm(self.attempt_a)
        self._adopt_attempt_a_with_artifact()
        self.attempt_b = self._prepare("trial-b", "attempt-b1")

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.operation_index += 1
        return f"workbench/{self.operation_index}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit_candidates(self) -> None:
        specs = {
            "a": {
                "x": 1,
                "shared": "steady",
                "removed": "baseline-only",
                "api_token": "candidate-secret",
                "private_large_shape": {"secret": "not-projected"},
                "large_payload": "z" * 9000,
            },
            "b": {
                "x": 2,
                "shared": "steady",
                "added": "comparison-only",
                "api_token": "candidate-secret",
                "private_large_shape": {"secret": "not-projected"},
                "large_payload": "z" * 9000,
            },
            "c": {"x": 3, "shared": "steady"},
        }
        candidates = tuple(
            CandidateAdmission(
                f"candidate-{suffix}",
                NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec=specs[suffix],
                ),
                lineage={"parents": []},
                generator={"method_id": "test-method"},
            )
            for suffix in ("a", "b", "c")
        )
        trials = tuple(
            LogicalTrialAdmission(f"trial-{suffix}", f"candidate-{suffix}")
            for suffix in ("a", "b", "c")
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        receipt = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=RunAdmissionPlan(candidates, trials),
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision

    def _prepare(self, trial_id: str, attempt_id: str):
        receipt = self.ledger.prepare_run_attempt(
            operation_id=self.op(f"prepare-{attempt_id}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id=trial_id,
            attempt_id=attempt_id,
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        return receipt

    def _confirm(self, prepared) -> None:
        # Workbench tests adopt directly from prepared; binding launch is
        # covered by the provider-backed execution-binding ledger suite.
        self.assertEqual(prepared.attempt.state, "prepared")

    def _terminalize(self, prepared) -> None:
        finalization = AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            effective_outcome="cancelled",
            effective_code="worker_cancelled",
            captured_artifacts=(),
            platform_error={
                "code": "worker_cancelled",
                "message": "test worker stopped before launch",
                "details": {},
            },
        )
        receipt = self.ledger.adopt_run_attempt(
            operation_id=self.op(f"terminalize-{prepared.attempt.attempt_id}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=prepared.attempt.capture_change_id,
            finalization=finalization,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision

    def _adopt_attempt_a_with_artifact(self) -> None:
        source = self.root / "attempt-output"
        source.mkdir()
        (source / "result.json").write_text('{"score":7.5}\n', encoding="utf-8")
        capture = self.store.capture(
            change_id=self.attempt_a.attempt.capture_change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=self.attempt_a.attempt.capture_change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_blob(
            source=AllowedFileSource(source, "result.json")
        )
        membership = OwnerMembership(
            self.store.store_id,
            sealed.blob_ref,
            RUN_ARTIFACT_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("hold-result"),
            actor_principal_id="operator",
            change_id=self.attempt_a.attempt.capture_change_id,
            memberships=(membership,),
        )
        declaration = OutputDeclaration(
            declaration_id="environment:result",
            name="result",
            path="result.json",
            media_type="application/json",
        )
        artifact = CapturedArtifact(
            declaration=declaration,
            content_ref=str(sealed.blob_ref),
            size_bytes=sealed.publication.logical_bytes,
            bindings=(
                {
                    "store_id": self.store.store_id,
                    "content_ref": str(sealed.blob_ref),
                },
            ),
            visibility="operator",
        )
        envelope = AttemptEnvelope(
            attempt_id=self.attempt_a.attempt.attempt_id,
            evaluation_spec_digest=self.attempt_a.attempt.evaluation_spec_digest,
            binding_id=self.attempt_a.attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.25,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {}, "metadata": {}},
            metric_values={"score": 7.5, "secondary": 2.0},
            constraint_results={"feasible": True},
            output_declarations=(declaration,),
            event_summary={"count": 1},
            execution_metadata={"worker": "test"},
        )
        finalization = AttemptFinalization(
            attempt_id=self.attempt_a.attempt.attempt_id,
            evaluation_spec_digest=self.attempt_a.attempt.evaluation_spec_digest,
            binding_id=self.attempt_a.attempt.binding_id,
            effective_outcome="success",
            effective_code=None,
            captured_artifacts=(artifact,),
            envelope=envelope,
        )
        receipt = self.ledger.adopt_run_attempt(
            operation_id=self.op("adopt-attempt-a1"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=self.attempt_a.attempt.attempt_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=self.attempt_a.attempt.capture_change_id,
            finalization=finalization,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision

    def model(self) -> RunWorkbenchReadModel:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        return RunWorkbenchReadModel.from_snapshot(snapshot)

    def test_pages_are_bounded_json_and_tokens_are_head_and_kind_scoped(self) -> None:
        model = self.model()
        first = model.page("candidate", limit=2)

        self.assertEqual(first["schema"], RUN_WORKBENCH_PAGE_SCHEMA)
        self.assertEqual([row["id"] for row in first["items"]], ["candidate-a", "candidate-b"])
        self.assertEqual(first["query"]["order"], RUN_CANDIDATE_RESULT_ORDER)
        self.assertEqual(
            first["capabilities"]["candidate_results"]["schema"],
            RUN_CANDIDATE_RESULT_SCHEMA,
        )
        self.assertTrue(first["capabilities"]["candidate_results"]["eligible"])
        ranking = first["capabilities"]["candidate_results"]["ranking"]
        self.assertTrue(ranking["supported"])
        self.assertFalse(ranking["eligible"])
        self.assertEqual(ranking["scope"], "within_run_evaluation_plan")
        self.assertEqual(ranking["reason"], "no_ranked_candidate_group")
        self.assertEqual(
            first["candidate_result_summary"]["counts"],
            {
                "rankable": 0,
                "aggregate_only": 1,
                "evidence_only": 2,
                "comparison_groups": 1,
                "ranked_groups": 0,
            },
        )
        self.assertTrue(first["page"]["has_more"])
        token = first["page"]["next_page_token"]
        second = model.page("candidate", page_token=token, limit=2)
        self.assertEqual([row["id"] for row in second["items"]], ["candidate-c"])
        self.assertFalse(second["page"]["has_more"])
        self.assertIsNone(second["page"]["next_page_token"])
        self.assertEqual(json.loads(json.dumps(first)), first)
        self.assertTrue(first["limitations"]["bounded_public_page"])
        self.assertTrue(
            first["limitations"]["internal_full_snapshot_materialization"]
        )
        self.assertEqual(
            first["limitations"]["max_page_size"], RUN_WORKBENCH_MAX_PAGE_SIZE
        )

        with self.assertRaisesRegex(ValueError, "between 1"):
            model.page("candidate", limit=RUN_WORKBENCH_MAX_PAGE_SIZE + 1)
        with self.assertRaisesRegex(ValueError, "different run head or kind"):
            model.page("logical_trial", page_token=token)
        with self.assertRaisesRegex(ValueError, "malformed"):
            model.page("candidate", page_token="not/a/token")

        padding = "=" * (-len(token) % 4)
        token_payload = json.loads(
            base64.urlsafe_b64decode(token + padding).decode("utf-8")
        )
        token_payload["order"] = "client-chosen-order.v1"
        altered_order_token = base64.urlsafe_b64encode(
            json.dumps(
                token_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        with self.assertRaisesRegex(ValueError, "kind or order"):
            model.page("candidate", page_token=altered_order_token)

        self._prepare("trial-c", "attempt-c1")
        newer = self.model()
        self.assertGreater(newer.summary.cursor.revision, model.summary.cursor.revision)
        with self.assertRaisesRegex(ValueError, "different run head or kind"):
            newer.page("candidate", page_token=token)

    def test_entity_identity_lookup_does_not_require_a_prior_page_selection(self) -> None:
        model = self.model()
        first = model.page("candidate", limit=1)
        self.assertNotEqual(first["items"][0]["id"], "candidate-c")

        resolved = model.entity_row("candidate", "candidate-c")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], "candidate-c")
        self.assertEqual(resolved["selection"]["entity_id"], "candidate-c")
        self.assertEqual(resolved["selection"]["revision"], first["head"]["revision"])
        self.assertEqual(resolved["selection"]["sequence"], first["head"]["sequence"])
        self.assertIsNone(model.entity_row("candidate", "candidate-missing"))
        with self.assertRaisesRegex(ValueError, "entity id"):
            model.entity_row("candidate", "")

    def test_selections_and_correlations_are_stable_exact_and_fixed_size(self) -> None:
        model = self.model()
        pages = {kind: model.page(kind) for kind in RUN_WORKBENCH_KINDS}
        candidates = {row["id"]: row for row in pages["candidate"]["items"]}
        trials = {row["id"]: row for row in pages["logical_trial"]["items"]}
        attempts = {row["id"]: row for row in pages["attempt"]["items"]}
        observations = {row["id"]: row for row in pages["observation"]["items"]}
        artifacts = {row["id"]: row for row in pages["artifact"]["items"]}

        head = pages["candidate"]["head"]
        candidate_selection = candidates["candidate-a"]["selection"]
        self.assertEqual(candidate_selection["schema"], RUN_WORKBENCH_SELECTION_SCHEMA)
        self.assertEqual(candidate_selection["revision"], head["revision"])
        self.assertEqual(candidate_selection["sequence"], head["sequence"])
        self.assertEqual(
            candidate_selection,
            model.page("candidate")["items"][0]["selection"],
        )
        resolved_candidate = model.selection_row(candidate_selection)
        self.assertIsNotNone(resolved_candidate)
        self.assertEqual(resolved_candidate["id"], "candidate-a")
        resolved_candidate["data"]["format"] = "client-tamper"
        self.assertNotEqual(
            model.selection_row(candidate_selection)["data"]["format"],
            "client-tamper",
        )
        self.assertEqual(candidates["candidate-a"]["data"]["logical_trial_count"], 1)
        self.assertNotIn("candidate_ref", candidates["candidate-a"]["data"])
        self.assertNotIn("logical_trials", candidates["candidate-a"]["data"])
        self.assertNotIn("private_large_shape", json.dumps(candidates["candidate-a"]))

        result_a = candidates["candidate-a"]["data"]["result"]
        self.assertEqual(result_a["schema"], RUN_CANDIDATE_RESULT_SCHEMA)
        self.assertEqual(result_a["status"], "aggregate_only")
        self.assertIsNone(result_a["reason"])
        self.assertEqual(result_a["aggregate"], {"value": 7.5, "sample_count": 1})
        self.assertEqual(
            result_a["comparison"]["reason"], "insufficient_comparators"
        )
        self.assertEqual(result_a["comparison"]["group_size"], 1)
        self.assertEqual(result_a["comparison"]["ranked_candidate_count"], 0)
        self.assertEqual(
            result_a["comparison"]["scope"], "within_evaluation_plan"
        )
        self.assertEqual(result_a["comparison"]["group_ordinal"], 1)
        self.assertEqual(
            candidates["candidate-b"]["data"]["result"]["reason"],
            "candidate_evaluation_active",
        )

        trial_candidate = trials["trial-a"]["correlations"][0]["selection"]
        self.assertEqual(trial_candidate, candidate_selection)
        attempt_relations = {
            item["relation"]: item["selection"]
            for item in attempts["attempt-a1"]["correlations"]
        }
        self.assertEqual(attempt_relations["candidate"], candidate_selection)
        self.assertEqual(
            attempt_relations["logical_trial"], trials["trial-a"]["selection"]
        )
        observation = next(iter(observations.values()))
        artifact = next(iter(artifacts.values()))
        artifact_relations = {
            item["relation"]: item["selection"]
            for item in artifact["correlations"]
        }
        self.assertEqual(artifact_relations["observation"], observation["selection"])
        self.assertEqual(artifact["data"]["attempt_id"], "attempt-a1")
        self.assertEqual(observation["data"]["objective_value"], 7.5)
        self.assertEqual(observation["data"]["metric_count"], 2)
        self.assertEqual(observation["data"]["metrics"]["total"], 2)
        self.assertEqual(
            {
                row["name"]: row["value"]
                for row in observation["data"]["metrics"]["rows"]
            },
            {"score": 7.5, "secondary": 2.0},
        )
        self.assertEqual(observation["data"]["constraints"]["total"], 1)
        self.assertEqual(
            observation["data"]["constraints"]["rows"],
            [
                {
                    "name": "feasible",
                    "name_truncated": False,
                    "value": True,
                    "supported": True,
                    "reason": None,
                }
            ],
        )
        for page in pages.values():
            for row in page["items"]:
                self.assertLessEqual(
                    len(row["correlations"]), RUN_WORKBENCH_MAX_CORRELATIONS
                )
        serialized_pages = json.dumps(pages, sort_keys=True)
        for forbidden_field in (
            "candidate_key",
            "candidate_ref",
            "content_ref",
            "store_id",
            "evaluation_spec_digest",
            "prepared_runtime_digest",
            "envelope_digest",
        ):
            self.assertNotIn(f'"{forbidden_field}"', serialized_pages)

        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        very_long = "x" * (RUN_WORKBENCH_MAX_TEXT_BYTES * 4)
        long_declaration = OutputDeclaration(
            declaration_id=very_long,
            name=very_long,
            path=f"{very_long}.json",
            media_type=very_long,
        )
        bounded_snapshot = replace(
            snapshot,
            artifacts=(
                replace(snapshot.artifacts[0], declaration=long_declaration),
            ),
        )
        bounded_artifact = RunWorkbenchReadModel.from_snapshot(
            bounded_snapshot
        ).page("artifact")["items"][0]["data"]
        self.assertTrue(bounded_artifact["presentation_text_truncated"])
        for field in ("declaration_id", "name", "path", "media_type"):
            self.assertLessEqual(
                len(bounded_artifact[field].encode("utf-8")),
                RUN_WORKBENCH_MAX_TEXT_BYTES,
            )

    def test_observation_measurements_are_deterministic_and_bounded(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )
        observation = snapshot.observations[0]
        metric_values = {
            f"metric_{index:02d}": float(index) for index in range(40)
        }
        metric_values["metric_00"] = 10**1000
        constraint_results = {
            f"constraint_{index:02d}": index % 2 == 0 for index in range(40)
        }
        envelope = replace(
            observation.envelope,
            metric_values=metric_values,
            constraint_results=constraint_results,
        )
        forged = replace(
            snapshot,
            observations=(replace(observation, envelope=envelope),),
        )
        data = RunWorkbenchReadModel.from_snapshot(forged).page("observation")[
            "items"
        ][0]["data"]

        metrics = data["metrics"]
        self.assertEqual(metrics["total"], 40)
        self.assertEqual(metrics["returned"], RUN_WORKBENCH_MAX_OBSERVATION_METRICS)
        self.assertEqual(metrics["omitted"], 24)
        self.assertTrue(metrics["truncated"])
        self.assertEqual(metrics["rows"][0]["name"], "metric_00")
        self.assertFalse(metrics["rows"][0]["supported"])
        self.assertIsNone(metrics["rows"][0]["value"])
        self.assertEqual(
            metrics["rows"][0]["reason"],
            "metric_result_not_finite_number_or_boolean",
        )
        self.assertEqual(metrics["rows"][-1]["name"], "metric_15")

        constraints = data["constraints"]
        self.assertEqual(constraints["semantics"], "boolean_satisfied")
        self.assertEqual(constraints["total"], 40)
        self.assertEqual(
            constraints["returned"], RUN_WORKBENCH_MAX_OBSERVATION_CONSTRAINTS
        )
        self.assertEqual(constraints["omitted"], 24)
        self.assertTrue(constraints["truncated"])
        self.assertEqual(constraints["rows"][0]["name"], "constraint_00")
        self.assertTrue(constraints["rows"][0]["value"])
        self.assertFalse(constraints["rows"][1]["value"])

    def test_parameter_candidate_comparison_is_bounded_exact_and_read_only(
        self,
    ) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )
        candidate_page = RunWorkbenchReadModel.from_snapshot(snapshot).page(
            "candidate"
        )
        candidate_rows = {row["id"]: row for row in candidate_page["items"]}
        baseline = candidate_rows["candidate-a"]["selection"]
        comparison = candidate_rows["candidate-b"]["selection"]

        pure = RunCandidateComparisonProjection.from_snapshot(
            snapshot,
            baseline_presentation_selection=baseline,
            comparison_presentation_selection=comparison,
        )
        payload = pure.to_dict()
        self.assertEqual(payload["schema"], RUN_CANDIDATE_COMPARISON_SCHEMA)
        self.assertEqual(payload["run_id"], self.created.run.run_id)
        self.assertEqual(payload["head"], candidate_page["head"])
        self.assertEqual(payload["mode"], "parameters")
        self.assertEqual(payload["eligibility"]["code"], "ready")
        self.assertEqual(
            [operand["role"] for operand in payload["operands"]],
            ["baseline", "comparison"],
        )
        self.assertEqual(payload["operands"][0]["selection"], baseline)
        self.assertEqual(payload["operands"][1]["selection"], comparison)
        self.assertEqual(
            payload["operands"][0]["result"],
            candidate_rows["candidate-a"]["data"]["result"],
        )
        self.assertEqual(
            payload["operands"][1]["result"],
            candidate_rows["candidate-b"]["data"]["result"],
        )
        self.assertEqual(
            payload["contract"],
            {"format": "parameters", "source": "retained_run_definition"},
        )
        self.assertEqual(
            payload["outcomes"]["schema"],
            "optpilot.run-candidate-outcome-comparison.v1",
        )
        self.assertTrue(payload["outcomes"]["eligibility"]["eligible"])
        constraints = payload["outcomes"]["constraints"]
        self.assertEqual(constraints["eligibility"]["code"], "ready")
        self.assertEqual(constraints["semantics"]["true"], "satisfied")
        self.assertEqual(len(constraints["rows"]), 1)
        feasible = constraints["rows"][0]
        self.assertEqual(feasible["name"], "feasible")
        self.assertEqual(feasible["baseline"]["status"], "complete")
        self.assertTrue(feasible["baseline"]["all_satisfied"])
        self.assertEqual(feasible["comparison"]["status"], "incomplete")
        self.assertEqual(
            feasible["comparison"]["reason"], "candidate_evaluation_active"
        )
        self.assertEqual(
            feasible["relation"]["reason"], "comparison_constraint_incomplete"
        )
        self.assertEqual(
            payload["candidate_input"]["schema"],
            "optpilot.run-candidate-input-comparison.v1",
        )
        self.assertTrue(payload["candidate_input"]["eligibility"]["eligible"])
        projected_rows = payload["candidate_input"]["parameters"]["rows"]
        rows = {
            row["name"]: row
            for row in projected_rows
            if not row["name_redacted"]
        }
        redacted_rows = [row for row in projected_rows if row["name_redacted"]]
        self.assertEqual(
            [row["name"] for row in projected_rows],
            [
                "removed",
                "shared",
                "x",
                "added",
                None,
                "large_payload",
                "private_large_shape",
            ],
        )
        self.assertEqual(len(redacted_rows), 1)
        self.assertIsNone(redacted_rows[0]["name"])
        self.assertFalse(redacted_rows[0]["declared"])
        self.assertEqual(
            redacted_rows[0]["definition"],
            {
                "value_type": None,
                "description": None,
                "description_truncated": False,
                "unit": None,
                "unit_truncated": False,
                "min": None,
                "max": None,
            },
        )
        self.assertTrue(all(not row["name_redacted"] for row in rows.values()))
        self.assertTrue(rows["x"]["declared"])
        self.assertEqual(rows["x"]["definition"]["value_type"], "int")
        self.assertEqual(rows["x"]["definition"]["unit"], "workers")
        self.assertEqual(rows["x"]["baseline"]["value"], 1)
        self.assertEqual(rows["x"]["comparison"]["value"], 2)
        self.assertEqual(rows["x"]["change"], "changed")
        self.assertEqual(rows["shared"]["change"], "same")
        self.assertEqual(rows["added"]["change"], "added")
        self.assertFalse(rows["added"]["declared"])
        self.assertEqual(rows["removed"]["change"], "removed")
        self.assertEqual(
            redacted_rows[0]["baseline"]["reason"],
            "private_presentation_material",
        )
        self.assertEqual(
            rows["large_payload"]["baseline"]["reason"],
            "value_preview_too_large",
        )
        self.assertEqual(
            rows["private_large_shape"]["comparison"]["reason"],
            "private_presentation_material",
        )
        self.assertEqual(
            payload["candidate_input"]["summary"],
            {
                "rows": 7,
                "same": 4,
                "changed": 1,
                "added": 1,
                "removed": 1,
                "hidden": 6,
            },
        )
        self.assertLessEqual(
            len(json.dumps(payload, sort_keys=True).encode("utf-8")),
            RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES,
        )
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "candidate_ref",
            "content_refs",
            str(self.root),
            self.created.run.owner_id,
            self.created.run.controller_lease_id,
            "not-projected",
            "candidate-secret",
            "api_token",
            "z" * 512,
        ):
            self.assertNotIn(forbidden, serialized)
        payload["operands"][0]["candidate"]["id"] = "tampered"
        self.assertEqual(
            pure.to_dict()["operands"][0]["candidate"]["id"],
            "candidate-a",
        )

        context = LocalRealmContext.open(ledger=self.ledger)
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-candidate-comparison"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        view = context.open_run_view(run_id=self.created.run.run_id)
        current_page = context.run_view_workbench_page(
            ref=view.ref,
            kind="candidate",
        )
        current_rows = {row["id"]: row for row in current_page["items"]}
        service_baseline = current_rows["candidate-a"]["selection"]
        service_comparison = current_rows["candidate-b"]["selection"]
        before_head = current_page["head"]
        with mock.patch.object(
            self.ledger,
            "read_run_snapshot",
            wraps=self.ledger.read_run_snapshot,
        ) as read_snapshot:
            service_result = context.compare_run_view_candidates(
                ref=view.ref,
                baseline_presentation_selection=service_baseline,
                comparison_presentation_selection=service_comparison,
            )
        read_snapshot.assert_called_once_with(
            actor_principal_id=context.principal_id,
            run_id=self.created.run.run_id,
        )
        self.assertEqual(service_result.to_dict()["head"], before_head)
        after = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )
        self.assertEqual(
            (after.revision.revision, after.revision.last_sequence),
            (before_head["revision"], before_head["sequence"]),
        )

        with self.assertRaisesRegex(ValueError, "must be distinct"):
            context.compare_run_view_candidates(
                ref=view.ref,
                baseline_presentation_selection=service_baseline,
                comparison_presentation_selection=service_baseline,
            )
        forged = dict(service_comparison)
        forged["entity_id"] = "candidate-forged"
        with self.assertRaisesRegex(ValueError, "integrity check"):
            context.compare_run_view_candidates(
                ref=view.ref,
                baseline_presentation_selection=service_baseline,
                comparison_presentation_selection=forged,
            )

        outsider = self.ledger.register_principal(
            operation_id=self.op("candidate-comparison-outsider"),
            principal_id="candidate-comparison-outsider",
            kind="human",
        )
        outsider_service = RealmRunViewService(self.ledger, outsider)
        with self.assertRaises(RealmNotFound):
            outsider_service.compare_candidates(
                ref=view.ref,
                baseline_presentation_selection={},
                comparison_presentation_selection={},
            )

        self._prepare("trial-c", "attempt-c1")
        with self.assertRaisesRegex(RealmConflict, "head changed"):
            context.compare_run_view_candidates(
                ref=view.ref,
                baseline_presentation_selection=service_baseline,
                comparison_presentation_selection=service_comparison,
            )
        context.close()

    def test_parameter_candidate_comparison_has_a_strict_row_limit(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )
        model = RunWorkbenchReadModel.from_snapshot(snapshot)
        rows = {row["id"]: row for row in model.page("candidate")["items"]}
        oversized_spec = {
            f"parameter_{index:04d}": index
            for index in range(RUN_CANDIDATE_COMPARISON_MAX_PARAMETERS + 1)
        }
        oversized_candidates = tuple(
            replace(
                candidate,
                admission=replace(
                    candidate.admission,
                    envelope=NormalizedCandidateEnvelope.build(
                        candidate_format="parameters",
                        spec=(
                            oversized_spec
                            if candidate.candidate_id in {"candidate-a", "candidate-b"}
                            else candidate.admission.envelope.spec
                        ),
                    ),
                ),
            )
            for candidate in snapshot.candidates
        )
        oversized_snapshot = replace(snapshot, candidates=oversized_candidates)
        projection = RunCandidateComparisonProjection.from_snapshot(
            oversized_snapshot,
            baseline_presentation_selection=rows["candidate-a"]["selection"],
            comparison_presentation_selection=rows["candidate-b"]["selection"],
        ).to_dict()
        self.assertTrue(projection["eligibility"]["eligible"])
        self.assertTrue(projection["eligibility"]["supported"])
        self.assertFalse(
            projection["candidate_input"]["eligibility"]["eligible"]
        )
        self.assertTrue(
            projection["candidate_input"]["eligibility"]["supported"]
        )
        self.assertEqual(
            projection["candidate_input"]["eligibility"]["code"],
            "candidate_input_comparison_parameter_limit_exceeded",
        )
        self.assertIsNone(projection["candidate_input"]["parameters"])
        self.assertTrue(projection["outcomes"]["eligibility"]["eligible"])

    def test_file_and_opaque_candidates_keep_generic_outcome_comparison(
        self,
    ) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )
        page = RunWorkbenchReadModel.from_snapshot(snapshot).page("candidate")
        rows = {row["id"]: row for row in page["items"]}
        content_ref = snapshot.artifacts[0].content_ref

        for candidate_format in ("files", "opaque"):
            candidates = tuple(
                replace(
                    candidate,
                    admission=replace(
                        candidate.admission,
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format=candidate_format,
                            spec={"label": candidate.candidate_id},
                            content_refs=(
                                (content_ref,)
                                if candidate_format == "files"
                                else ()
                            ),
                        ),
                    ),
                )
                for candidate in snapshot.candidates
            )
            environment = copy.copy(
                snapshot.evaluation_closure.environment_revision
            )
            object.__setattr__(
                environment,
                "candidate_contract",
                MappingProxyType({"format": candidate_format}),
            )
            closure = copy.copy(snapshot.evaluation_closure)
            object.__setattr__(closure, "environment_revision", environment)
            definition = copy.copy(snapshot.definition)
            object.__setattr__(definition, "evaluation_closure", closure)
            forged = copy.copy(snapshot)
            object.__setattr__(forged, "definition", definition)
            object.__setattr__(forged, "candidates", candidates)

            projection = RunCandidateComparisonProjection.from_snapshot(
                forged,
                baseline_presentation_selection=rows["candidate-a"]["selection"],
                comparison_presentation_selection=rows["candidate-b"]["selection"],
            ).to_dict()
            self.assertEqual(projection["mode"], candidate_format)
            self.assertTrue(projection["eligibility"]["eligible"])
            self.assertTrue(projection["outcomes"]["eligibility"]["eligible"])
            if candidate_format == "files":
                self.assertFalse(
                    projection["candidate_input"]["eligibility"]["eligible"]
                )
                self.assertEqual(
                    projection["candidate_input"]["eligibility"]["code"],
                    "candidate_file_manifest_unavailable",
                )
                self.assertIsNone(projection["candidate_input"]["files"])
            else:
                self.assertEqual(
                    projection["candidate_input"]["eligibility"]["code"], "ready"
                )
                self.assertEqual(
                    projection["candidate_input"]["summary"]["changed"], 1
                )
                metadata = projection["candidate_input"]["metadata"]
                self.assertEqual(metadata["rows"][0]["name"], "label")
                self.assertEqual(metadata["rows"][0]["change"], "changed")
            self.assertIsNone(projection["candidate_input"]["parameters"])
            serialized = json.dumps(projection, sort_keys=True)
            self.assertNotIn(str(content_ref), serialized)
            self.assertNotIn("content_ref", serialized)

    def test_file_candidate_comparison_uses_only_path_free_manifest_facts(self) -> None:
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )
        page = RunWorkbenchReadModel.from_snapshot(snapshot).page("candidate")
        rows = {row["id"]: row for row in page["items"]}
        content_ref = snapshot.artifacts[0].content_ref
        contract = {
            "format": "files",
            "validation": {
                "implementation": "builtin.workspace_policy",
                "config": {},
            },
            "materialization": {
                "implementation": "builtin.workspace_bundle",
                "config": {},
            },
        }
        baseline_spec = {
            "schema": "optpilot.sealed-file-candidate-spec.v1",
            "directories": [],
            "entrypoint": None,
            "files": [
                {
                    "path": "main.py",
                    "sha256": "a" * 64,
                    "sizeBytes": 10,
                    "executable": False,
                }
            ],
            "options": {"candidateRoot": "."},
        }
        comparison_spec = {
            "schema": "optpilot.sealed-file-candidate-spec.v1",
            "directories": [],
            "entrypoint": None,
            "files": [
                {
                    "path": "extra.txt",
                    "sha256": "c" * 64,
                    "sizeBytes": 3,
                    "executable": False,
                },
                {
                    "path": "main.py",
                    "sha256": "b" * 64,
                    "sizeBytes": 12,
                    "executable": True,
                },
            ],
            "options": {"candidateRoot": "."},
        }
        candidates = tuple(
            replace(
                candidate,
                admission=replace(
                    candidate.admission,
                    envelope=NormalizedCandidateEnvelope.build(
                        candidate_format="files",
                        spec=(
                            comparison_spec
                            if candidate.candidate_id == "candidate-b"
                            else baseline_spec
                        ),
                        content_refs=(content_ref,),
                    ),
                ),
            )
            for candidate in snapshot.candidates
        )
        environment = copy.copy(snapshot.evaluation_closure.environment_revision)
        object.__setattr__(
            environment, "candidate_contract", MappingProxyType(contract)
        )
        closure = copy.copy(snapshot.evaluation_closure)
        object.__setattr__(closure, "environment_revision", environment)
        definition = copy.copy(snapshot.definition)
        object.__setattr__(definition, "evaluation_closure", closure)
        forged = copy.copy(snapshot)
        object.__setattr__(forged, "definition", definition)
        object.__setattr__(forged, "candidates", candidates)

        candidate_input = RunCandidateComparisonProjection.from_snapshot(
            forged,
            baseline_presentation_selection=rows["candidate-a"]["selection"],
            comparison_presentation_selection=rows["candidate-b"]["selection"],
        ).to_dict()["candidate_input"]
        self.assertEqual(candidate_input["eligibility"]["code"], "ready")
        self.assertIsNone(candidate_input["parameters"])
        self.assertEqual(candidate_input["summary"]["rows"], 2)
        self.assertEqual(candidate_input["summary"]["added"], 1)
        self.assertEqual(candidate_input["summary"]["changed"], 1)
        files = {row["path"]: row for row in candidate_input["files"]["rows"]}
        self.assertEqual(files["extra.txt"]["change"], "added")
        self.assertEqual(files["main.py"]["change"], "changed")
        self.assertFalse(files["main.py"]["content_equal"])
        self.assertFalse(files["main.py"]["executable_equal"])
        self.assertEqual(files["main.py"]["baseline"]["size_bytes"], 10)
        self.assertEqual(files["main.py"]["comparison"]["size_bytes"], 12)
        serialized = json.dumps(candidate_input, sort_keys=True)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("content_ref", serialized)
        self.assertNotIn(str(content_ref), serialized)

    def test_snapshot_data_is_immutable_and_provider_actions_are_honestly_gated(self) -> None:
        model = self.model()
        page = model.page("candidate")
        capabilities = {
            item["action"]: item for item in page["capabilities"]["actions"]
        }
        self.assertEqual(tuple(capabilities), RUN_WORKBENCH_ACTIONS)
        self.assertTrue(capabilities["select"]["supported"])
        self.assertTrue(capabilities["select"]["eligible"])
        self.assertIsNone(capabilities["select"]["reason"])
        for action in RUN_WORKBENCH_ACTIONS[1:]:
            self.assertFalse(capabilities[action]["supported"])
            self.assertFalse(capabilities[action]["eligible"])
            self.assertTrue(capabilities[action]["reason"].endswith("_unavailable"))

        eligibility = {
            item["action"]: item for item in page["items"][0]["eligibility"]
        }
        self.assertTrue(eligibility["select"]["supported"])
        self.assertTrue(eligibility["select"]["eligible"])
        for action in RUN_WORKBENCH_ACTIONS[1:]:
            self.assertFalse(eligibility[action]["supported"])
            self.assertFalse(eligibility[action]["eligible"])
            self.assertEqual(eligibility[action]["reason"], capabilities[action]["reason"])

        original_selection = page["items"][0]["selection"].copy()
        original_format = page["items"][0]["data"]["format"]
        page["items"][0]["data"]["format"] = "tampered"
        page["items"][0]["selection"]["entity_id"] = "tampered"
        page["capabilities"]["actions"][0]["supported"] = False
        fresh = model.page("candidate")
        self.assertEqual(
            fresh["items"][0]["data"]["format"], original_format
        )
        self.assertEqual(fresh["items"][0]["selection"], original_selection)
        self.assertTrue(fresh["capabilities"]["actions"][0]["supported"])
        with self.assertRaises(TypeError):
            model._rows["candidate"][0]["data"]["format"] = "tampered"  # type: ignore[index]

        old_head = model.summary.cursor
        self._terminalize(self.attempt_b)
        newer = self.model()
        self.assertEqual(model.summary.cursor, old_head)
        old_attempt = {
            row["id"]: row for row in model.page("attempt")["items"]
        }["attempt-b1"]
        new_attempt = {
            row["id"]: row for row in newer.page("attempt")["items"]
        }["attempt-b1"]
        self.assertEqual(old_attempt["data"]["state"], "prepared")
        self.assertEqual(new_attempt["data"]["state"], "terminal")

    def test_borrowed_view_bridge_mints_retained_artifact_selection(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-borrowed-view"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        view = context.open_run_view(run_id=self.created.run.run_id)
        page = context.run_view_workbench_page(ref=view.ref, kind="artifact")
        self.assertEqual(len(page["items"]), 1)
        result = context.mint_run_view_selection(
            ref=view.ref,
            presentation_selection=page["items"][0]["selection"],
        )
        self.assertTrue(result.eligibility.eligible)
        self.assertEqual(result.selection.kind, "artifact")
        self.assertEqual(result.selection.entity_id, page["items"][0]["id"])
        context.close()

    def test_live_head_uses_one_snapshot_and_is_path_free_and_truthful(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-live-head"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        ref = RunViewRef(run_id=self.created.run.run_id)
        with mock.patch.object(
            self.ledger,
            "read_run_snapshot",
            wraps=self.ledger.read_run_snapshot,
        ) as read_snapshot:
            live = context.run_view_workbench_head(ref=ref)
        read_snapshot.assert_called_once_with(
            actor_principal_id=context.principal_id,
            run_id=self.created.run.run_id,
        )

        payload = live.to_dict()
        self.assertEqual(payload["schema"], RUN_WORKBENCH_HEAD_SCHEMA)
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["head"], payload["view"]["head"])
        self.assertEqual(payload["head"], payload["summary"]["cursor"])
        self.assertEqual(live.view.status, live.summary.run_status)
        self.assertEqual(
            live.view.retention_state,
            live.summary.retention_state,
        )
        self.assertTrue(payload["capabilities"]["entity_pages"]["supported"])
        timeline = payload["capabilities"]["timeline"]
        self.assertTrue(timeline["supported"])
        self.assertTrue(timeline["eligible"])
        self.assertIsNone(timeline["reason"])
        actions = {
            item["action"]: item
            for item in payload["capabilities"]["actions"]
        }
        self.assertTrue(actions["select"]["supported"])
        for action in RUN_WORKBENCH_ACTIONS[1:]:
            self.assertFalse(actions[action]["supported"])

        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            str(self.root),
            str(self.ledger.database_path),
            self.created.run.owner_id,
            context.principal_id,
            self.created.run.controller_lease_id,
            self.created.run.controller_holder_id,
        ):
            self.assertNotIn(forbidden, serialized)
        for growing_array in (
            '"candidates": [',
            '"logical_trials": [',
            '"attempts": [',
            '"observations": [',
            '"artifacts": [',
            '"events": [',
        ):
            self.assertNotIn(growing_array, serialized)
        context.close()

    def test_timeline_is_authorized_bounded_exact_head_and_path_free(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-timeline"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        ref = RunViewRef(run_id=self.created.run.run_id)
        head = context.run_view_workbench_head(ref=ref)
        first = context.run_view_timeline_page(
            ref=ref,
            expected_run_revision=head.head["revision"],
            expected_head_sequence=head.head["sequence"],
            limit=2,
        )
        payload = first.to_dict()

        self.assertEqual(payload["schema"], RUN_TIMELINE_PAGE_SCHEMA)
        self.assertEqual(payload["head"], head.head)
        self.assertLessEqual(len(payload["items"]), 2)
        self.assertTrue(payload["items"])
        self.assertEqual(
            [item["sequence"] for item in payload["items"]],
            sorted(item["sequence"] for item in payload["items"]),
        )
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("payload_json", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.ledger.database_path), serialized)
        self.assertTrue(
            all(
                item["payload_digest"].startswith("sha256:")
                for item in payload["items"]
            )
        )
        if first.has_more:
            second = context.run_view_timeline_page(
                ref=ref,
                expected_run_revision=head.head["revision"],
                expected_head_sequence=head.head["sequence"],
                after_sequence=first.next_after_sequence,
                limit=2,
            )
            self.assertGreater(
                second.items[0].sequence,
                first.items[-1].sequence,
            )

        self._prepare("trial-c", "attempt-c1")
        with self.assertRaisesRegex(RealmConflict, "head changed"):
            context.run_view_timeline_page(
                ref=ref,
                expected_run_revision=head.head["revision"],
                expected_head_sequence=head.head["sequence"],
            )

        self.ledger.register_principal(
            operation_id=self.op("outsider-principal"),
            principal_id="timeline-outsider",
            kind="human",
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_timeline_page(
                actor_principal_id="timeline-outsider",
                run_id=self.created.run.run_id,
                expected_run_revision=self.run_revision,
                expected_head_sequence=(
                    self.ledger.read_run_snapshot(
                        actor_principal_id="operator",
                        run_id=self.created.run.run_id,
                    ).revision.last_sequence
                ),
            )
        context.close()

    def test_review_collection_is_revisioned_exact_and_reuses_retained_content(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("review-principal"),
            principal_id="operator",
            kind="human",
        )
        service = RealmReviewCollectionService(self.ledger, principal)
        candidate_page = self.model().page("candidate")
        rows = {row["id"]: row for row in candidate_page["items"]}

        first = service.add_candidate(
            operation_id=self.op("review-add-a"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
            note="Promising baseline",
        )
        self.assertEqual(first.revision, 1)
        self.assertEqual(len(first.items), 1)
        self.assertEqual(first.items[0].note, "Promising baseline")
        self.assertEqual(first.items[0].evidence["retention"]["artifact_content_count"], 1)
        self.assertTrue(
            first.items[0].evidence["retention"]["content_reused_without_copy"]
        )
        self.assertFalse(
            first.items[0].evidence["retention"]["runnable_closure_retained"]
        )

        connection = self.ledger._connect()
        try:
            source_artifact = connection.execute(
                "SELECT content_ref FROM run_artifacts WHERE run_id = ?",
                (self.created.run.run_id,),
            ).fetchone()["content_ref"]
            review_membership = connection.execute(
                "SELECT store_id, content_ref FROM owner_memberships "
                "WHERE owner_id = ? AND role = ? AND removed_revision IS NULL",
                (first.owner_id, REVIEW_ARTIFACT_ROLE),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(review_membership["content_ref"], source_artifact)
        self.assertEqual(review_membership["store_id"], self.store.store_id)

        duplicate = service.add_candidate(
            operation_id=self.op("review-add-a-again"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
        )
        self.assertEqual(duplicate.revision, 1)
        self.assertEqual(duplicate.revision_digest, first.revision_digest)

        second = service.add_candidate(
            operation_id=self.op("review-add-b"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-b"]["selection"],
        )
        self.assertEqual(second.revision, 2)
        self.assertEqual(
            [item.selection.entity_id for item in second.items],
            ["candidate-a", "candidate-b"],
        )

        edited = service.save_revision(
            operation_id=self.op("review-edit"),
            run_id=self.created.run.run_id,
            collection_id=second.collection_id,
            expected_revision=second.revision,
            title="Final candidates",
            entries=(
                ReviewCollectionEntryDraft(
                    second.items[1].selection.selection_digest,
                    "Inspect visually before deciding",
                ),
                ReviewCollectionEntryDraft(
                    second.items[0].selection.selection_digest,
                    second.items[0].note,
                ),
            ),
        )
        self.assertEqual(edited.revision, 3)
        self.assertEqual(edited.title, "Final candidates")
        self.assertEqual(
            [item.selection.entity_id for item in edited.items],
            ["candidate-b", "candidate-a"],
        )
        self.assertEqual(
            edited.items[0].note, "Inspect visually before deciding"
        )
        with self.assertRaisesRegex(RealmConflict, "changed"):
            service.save_revision(
                operation_id=self.op("review-stale-edit"),
                run_id=self.created.run.run_id,
                collection_id=second.collection_id,
                expected_revision=second.revision,
                title="Stale title",
                entries=(
                    ReviewCollectionEntryDraft(
                        second.items[0].selection.selection_digest
                    ),
                ),
            )

        historical = service.read_for_run(
            run_id=self.created.run.run_id,
            revision=1,
        )
        self.assertIsNotNone(historical)
        self.assertEqual(historical.revision_digest, first.revision_digest)
        exported = historical.export_dict()
        self.assertEqual(exported["schema"], REVIEW_COLLECTION_EXPORT_SCHEMA)
        self.assertEqual(exported["revision"], 1)
        self.assertEqual(len(exported["items"]), 1)
        self.assertNotIn(str(self.root), json.dumps(exported, sort_keys=True))

        history = service.history_for_run(run_id=self.created.run.run_id, limit=2)
        self.assertIsNotNone(history)
        self.assertEqual(history.current_revision, 3)
        self.assertEqual([item.revision for item in history.items], [3, 2])
        self.assertEqual([item.item_count for item in history.items], [2, 2])
        self.assertTrue(history.has_more)
        self.assertEqual(history.next_before_revision, 2)
        older = service.history_for_run(
            run_id=self.created.run.run_id,
            before_revision=history.next_before_revision,
            limit=2,
        )
        self.assertIsNotNone(older)
        self.assertEqual([item.revision for item in older.items], [1])
        self.assertFalse(older.has_more)
        self.assertIsNone(older.next_before_revision)
        with self.assertRaisesRegex(ValueError, "inspection outcome is too large"):
            ReviewCollectionEntryDraft(
                "a" * 64,
                inspection_outcomes=({"detail": "x" * (128 * 1024)},),
            )

    def test_shortlist_add_commits_the_complete_dirty_draft_atomically(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-principal"),
            principal_id="operator",
            kind="human",
        )
        reviews = RealmReviewCollectionService(self.ledger, principal)
        shortlists = RealmShortlistService(reviews)
        rows = {row["id"]: row for row in self.model().page("candidate")["items"]}

        first_operation = self.op("shortlist-add-a")
        first = shortlists.save_candidate(
            operation_id=first_operation,
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
            draft=ShortlistDraft.empty(),
            note="Initial note",
        )
        replayed_first = shortlists.save_candidate(
            operation_id=first_operation,
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
            draft=ShortlistDraft.empty(),
            note="Initial note",
        )
        self.assertEqual(replayed_first.revision_digest, first.revision_digest)
        dirty = ShortlistDraft(
            shortlist_id=first.shortlist_id,
            expected_revision=first.revision,
            title="Decision finalists",
            cards=(
                ShortlistCardDraft(
                    first.cards[0].selection.selection_digest,
                    "Unsaved note kept during Add",
                ),
            ),
        )
        second_operation = self.op("shortlist-add-b-with-dirty-draft")
        second = shortlists.save_candidate(
            operation_id=second_operation,
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-b"]["selection"],
            draft=dirty,
            note="Compare with A",
        )
        replayed_second = shortlists.save_candidate(
            operation_id=second_operation,
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-b"]["selection"],
            draft=dirty,
            note="Compare with A",
        )
        self.assertEqual(replayed_second.revision_digest, second.revision_digest)

        self.assertEqual(second.title, "Decision finalists")
        self.assertEqual(
            [card.candidate_id for card in second.cards],
            ["candidate-a", "candidate-b"],
        )
        self.assertEqual(
            [card.note for card in second.cards],
            ["Unsaved note kept during Add", "Compare with A"],
        )
        self.assertEqual(
            second.cards[0].to_dict()["schema"],
            "optpilot.run-shortlist-card.v1",
        )

        with self.assertRaisesRegex(RealmConflict, "changed"):
            shortlists.save_candidate(
                operation_id=self.op("shortlist-stale-add-c"),
                run_id=self.created.run.run_id,
                presentation_selection=rows["candidate-c"]["selection"],
                draft=dirty,
            )
        current = shortlists.read_for_run(run_id=self.created.run.run_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.revision, second.revision)
        self.assertEqual(
            [card.candidate_id for card in current.cards],
            ["candidate-a", "candidate-b"],
        )

    def test_shortlist_retains_heterogeneous_candidate_content_refs(self) -> None:
        """Blob and tree candidate roots share one deterministic owner order."""

        self.ledger.create_owner(
            operation_id=self.op("mixed-source-owner"),
            owner_id="mixed-source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "mixed-candidate-source"
        source.mkdir()
        (source / "run.py").write_text("print('mixed')\n", encoding="utf-8")
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("mixed-source-begin"),
            actor_principal_id="operator",
            owner_id="mixed-source-owner",
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        capture = self.store.capture(
            change_id=source_change.change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=source_change.change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed_tree = capture.seal_tree(source=AllowedTreeSource(source))
        source_membership = OwnerMembership(
            self.store.store_id,
            sealed_tree.snapshot_ref,
            "workspace-root",
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("mixed-source-hold"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            memberships=(source_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("mixed-source-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )

        # A file Candidate may retain more than one physical root.  Pair the
        # new tree with the already retained result blob so the Review owner
        # must canonicalize two distinct PhysicalContentRef implementations.
        artifact_blob = self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        ).artifacts[0].content_ref
        candidate_bindings = (
            OwnerMembership(
                self.store.store_id,
                artifact_blob,
                RUN_CANDIDATE_ROLE,
            ),
            OwnerMembership(
                self.store.store_id,
                sealed_tree.snapshot_ref,
                RUN_CANDIDATE_ROLE,
            ),
        )
        run_change = self.ledger.begin_owner_change(
            operation_id=self.op("mixed-candidate-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op("mixed-candidate-hold"),
            actor_principal_id="operator",
            change_id=run_change.change_id,
            memberships=candidate_bindings,
            source_owner_id="mixed-source-owner",
        )
        envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec={"entrypoint": "run.py"},
            content_refs=(sealed_tree.snapshot_ref, artifact_blob),
        )
        admission = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("mixed-candidate-admit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=run_change.change_id,
            plan=RunAdmissionPlan(
                (CandidateAdmission("candidate-mixed", envelope),),
                (LogicalTrialAdmission("trial-mixed", "candidate-mixed"),),
            ),
            content_bindings=candidate_bindings,
            **self.controller_arguments(),
        )
        self.run_revision = admission.revision.revision
        self.owner_revision = admission.owner_commit.owner_revision

        principal = self.ledger.register_principal(
            operation_id=self.op("mixed-shortlist-principal"),
            principal_id="operator",
            kind="human",
        )
        shortlists = RealmShortlistService(
            RealmReviewCollectionService(self.ledger, principal)
        )
        rows = {
            row["id"]: row
            for row in self.model().page("candidate", limit=10)["items"]
        }
        saved = shortlists.save_candidate(
            operation_id=self.op("mixed-shortlist-save"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-mixed"]["selection"],
            draft=ShortlistDraft.empty(),
        )

        self.assertEqual(saved.cards[0].candidate_id, "candidate-mixed")
        retention = saved.cards[0].evidence["retention"]
        self.assertEqual(retention["candidate_content_count"], 2)
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=saved.shortlist_id,
        )
        retained_candidate_refs = {
            membership.content_ref
            for membership in memberships
            if membership.role == "review-candidate"
        }
        self.assertEqual(
            retained_candidate_refs,
            {artifact_blob, sealed_tree.snapshot_ref},
        )

    def test_shortlist_committed_save_replays_after_run_head_advances(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-replay-principal"),
            principal_id="operator",
            kind="human",
        )
        shortlists = RealmShortlistService(
            RealmReviewCollectionService(self.ledger, principal)
        )
        presented = {
            row["id"]: row
            for row in self.model().page("candidate")["items"]
        }["candidate-a"]["selection"]
        operation_id = self.op("shortlist-save-before-head-advance")
        draft = ShortlistDraft.empty()
        with mock.patch.object(
            RealmShortlistService,
            "_presented_candidate_snapshot",
            side_effect=RuntimeError("injected failure after intent binding"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                shortlists.save_candidate(
                    operation_id=operation_id,
                    run_id=self.created.run.run_id,
                    presentation_selection=presented,
                    draft=draft,
                    note="Keep this result",
                )
        saved = shortlists.save_candidate(
            operation_id=operation_id,
            run_id=self.created.run.run_id,
            presentation_selection=presented,
            draft=draft,
            note="Keep this result",
        )

        self._terminalize(self.attempt_b)
        current_head = self.model().summary.cursor
        self.assertNotEqual(current_head.revision, presented["revision"])

        stale_operation_id = self.op("new-shortlist-save-at-stale-head")
        with self.assertRaisesRegex(RealmConflict, "presentation head changed"):
            shortlists.save_candidate(
                operation_id=stale_operation_id,
                run_id=self.created.run.run_id,
                presentation_selection=presented,
                draft=ShortlistDraft.from_revision(saved),
            )
        current_presented = {
            row["id"]: row
            for row in self.model().page("candidate")["items"]
        }["candidate-a"]["selection"]
        with self.assertRaisesRegex(RealmConflict, "different request"):
            shortlists.save_candidate(
                operation_id=stale_operation_id,
                run_id=self.created.run.run_id,
                presentation_selection=current_presented,
                draft=ShortlistDraft.from_revision(saved),
            )

        reopened_ledger = RealmLedger(self.root / "realm.sqlite3")
        try:
            reopened_shortlists = RealmShortlistService(
                RealmReviewCollectionService(reopened_ledger, principal)
            )
            replayed = reopened_shortlists.save_candidate(
                operation_id=operation_id,
                run_id=self.created.run.run_id,
                presentation_selection=presented,
                draft=draft,
                note="Keep this result",
            )
            self.assertEqual(replayed.revision, saved.revision)
            self.assertEqual(replayed.revision_digest, saved.revision_digest)
            self.assertEqual(replayed.cards[0].candidate_id, "candidate-a")

            with self.assertRaisesRegex(RealmConflict, "different request"):
                reopened_shortlists.save_candidate(
                    operation_id=operation_id,
                    run_id=self.created.run.run_id,
                    presentation_selection=presented,
                    draft=draft,
                    note="A different request",
                )
        finally:
            reopened_ledger.close()

    def test_shortlist_public_projection_redacts_exact_internal_evidence(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-public-principal"),
            principal_id="operator",
            kind="human",
        )
        reviews = RealmReviewCollectionService(self.ledger, principal)
        shortlists = RealmShortlistService(reviews)
        row = next(
            item
            for item in self.model().page("candidate")["items"]
            if item["id"] == "candidate-a"
        )
        saved = shortlists.save_candidate(
            operation_id=self.op("shortlist-public-save"),
            run_id=self.created.run.run_id,
            presentation_selection=row["selection"],
            draft=ShortlistDraft.empty(),
        )

        payload = saved.to_dict()
        card = payload["cards"][0]
        self.assertEqual(
            set(card["selection"]),
            {
                "schema",
                "kind",
                "source_kind",
                "source_id",
                "source_revision",
                "source_sequence",
                "entity_id",
                "selection_digest",
            },
        )
        self.assertEqual(
            card["selection"]["schema"],
            REVIEW_COLLECTION_PUBLIC_SELECTION_SCHEMA,
        )
        evidence = card["saved_evidence"]
        self.assertEqual(
            evidence["schema"], REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA
        )
        self.assertEqual(
            evidence["candidate"], {"id": "candidate-a", "format": "parameters"}
        )
        self.assertEqual(
            evidence["candidate_result"]["objective"]["metric"],
            self.closure.evaluation_template.objective["primaryMetric"]["name"],
        )
        self.assertIn("comparison", evidence["candidate_result"])
        self.assertEqual(evidence["retention"]["policy"], "decision")
        self.assertFalse(evidence["retention"]["runnable_closure_retained"])
        self.assertFalse(evidence["artifacts"]["details_included"])

        exported = reviews.read_for_run(
            run_id=self.created.run.run_id
        ).export_dict()
        self.assertEqual(exported["schema"], REVIEW_COLLECTION_EXPORT_SCHEMA)
        self.assertNotIn("created_by", exported)
        self.assertEqual(
            exported["items"][0]["evidence"]["schema"],
            REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA,
        )

        serialized = json.dumps({"shortlist": payload, "export": exported})
        for forbidden in (
            "candidate-secret",
            "api_token",
            "private_large_shape",
            "not-projected",
            "content_ref",
            "source_owner_id",
            "owner_revision",
            "entity_ref",
            "context_digest",
            "relative_path",
            str(self.root),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_public_inspection_projection_whitelists_metrics_and_counts(self) -> None:
        public = public_review_inspection_outcome(
            {
                "schema": REVIEW_INSPECTION_OUTCOME_SCHEMA,
                "kind": "operator_job",
                "operator_job_id": "operator-job-" + "a" * 32,
                "job_kind": "candidate-debug-run",
                "plan_digest": "b" * 64,
                "target": {
                    "content_ref": "tree:sha256:" + "c" * 64,
                    "owner_id": "private-owner",
                },
                "execution_policy": {
                    "entrypoint_profile": "/private/launch.sh",
                    "requested_secret": "do-not-export",
                },
                "outcome": {"status": "succeeded", "code": "completed"},
                "result": {
                    "result_kind": "evaluation",
                    "status": "succeeded",
                    "metrics": {
                        "total": 3,
                        "returned": 3,
                        "truncated": False,
                        "values": {
                            "score": 1.5,
                            "feasible": True,
                            "secret": "do-not-export",
                        },
                    },
                    "details": {"path": "/private/result.json"},
                    "declared_outputs": {
                        "total": 1,
                        "returned": 1,
                        "truncated": False,
                        "rows": [
                            {
                                "name": "result",
                                "path": "private/result.json",
                                "content_ref": "blob:sha256:" + "d" * 64,
                            }
                        ],
                    },
                },
                "completed_at": 123.0,
            }
        )
        self.assertEqual(
            public["result"]["metrics"]["values"],
            {"score": 1.5, "feasible": True},
        )
        self.assertEqual(public["result"]["declared_outputs"]["total"], 1)
        serialized = json.dumps(public, sort_keys=True)
        for forbidden in (
            "do-not-export",
            "content_ref",
            "owner_id",
            "entrypoint_profile",
            "/private",
            "plan_digest",
            '"details":',
            '"rows":',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_shortlist_explicit_snapshot_update_is_immutable_and_keeps_card_edits(
        self,
    ) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-refresh-principal"),
            principal_id="operator",
            kind="human",
        )
        shortlists = RealmShortlistService(
            RealmReviewCollectionService(self.ledger, principal)
        )
        rows = {row["id"]: row for row in self.model().page("candidate")["items"]}
        first = shortlists.save_candidate(
            operation_id=self.op("shortlist-refresh-add-a"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
            draft=ShortlistDraft.empty(),
            note="Keep this reason",
        )
        second = shortlists.save_candidate(
            operation_id=self.op("shortlist-refresh-add-b"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-b"]["selection"],
            draft=ShortlistDraft.from_revision(first),
        )
        old_a = second.cards[0]
        self.assertIn("captured_at", old_a.evidence)
        self.assertEqual(old_a.saved_result_at, old_a.evidence["captured_at"])

        # Advance the Run evidence head, then explicitly select that newer
        # snapshot while committing a reordered, dirty draft.
        self._terminalize(self.attempt_b)
        fresh_rows = {
            row["id"]: row for row in self.model().page("candidate")["items"]
        }
        unchanged = shortlists.save_candidate(
            operation_id=self.op("shortlist-save-a-without-refresh"),
            run_id=self.created.run.run_id,
            presentation_selection=fresh_rows["candidate-a"]["selection"],
            draft=ShortlistDraft.from_revision(second),
        )
        self.assertEqual(unchanged.revision, second.revision)
        self.assertEqual(unchanged.cards[0].evidence_digest, old_a.evidence_digest)
        self.assertEqual(
            sum(card.candidate_id == "candidate-a" for card in unchanged.cards),
            1,
        )
        dirty = ShortlistDraft(
            shortlist_id=second.shortlist_id,
            expected_revision=second.revision,
            title="Ordered finalists",
            cards=(
                ShortlistCardDraft(
                    second.cards[1].selection.selection_digest,
                    "B stays first",
                ),
                ShortlistCardDraft(
                    second.cards[0].selection.selection_digest,
                    "A note survives refresh",
                ),
            ),
        )
        refreshed = shortlists.save_candidate(
            operation_id=self.op("shortlist-refresh-a"),
            run_id=self.created.run.run_id,
            presentation_selection=fresh_rows["candidate-a"]["selection"],
            draft=dirty,
            update_saved_result=True,
        )

        self.assertEqual(refreshed.title, "Ordered finalists")
        self.assertEqual(
            [card.candidate_id for card in refreshed.cards],
            ["candidate-b", "candidate-a"],
        )
        refreshed_a = refreshed.cards[1]
        self.assertEqual(refreshed_a.note, "A note survives refresh")
        self.assertNotEqual(
            refreshed_a.selection.selection_digest,
            old_a.selection.selection_digest,
        )
        self.assertNotEqual(refreshed_a.evidence_digest, old_a.evidence_digest)
        self.assertGreaterEqual(refreshed_a.saved_result_at, old_a.saved_result_at)
        self.assertEqual(
            sum(card.candidate_id == "candidate-a" for card in refreshed.cards),
            1,
        )

        historical = shortlists.read_for_run(
            run_id=self.created.run.run_id,
            revision=second.revision,
        )
        self.assertIsNotNone(historical)
        self.assertEqual(historical.cards[0].evidence_digest, old_a.evidence_digest)
        self.assertNotEqual(
            historical.cards[0].evidence_digest,
            refreshed_a.evidence_digest,
        )

        # Asking to update again at the same exact Run head is an idempotent
        # no-op rather than a second public card or mutable evidence rewrite.
        replayed_head = shortlists.save_candidate(
            operation_id=self.op("shortlist-refresh-a-same-head"),
            run_id=self.created.run.run_id,
            presentation_selection=fresh_rows["candidate-a"]["selection"],
            draft=ShortlistDraft.from_revision(refreshed),
            update_saved_result=True,
        )
        self.assertEqual(replayed_head.revision, refreshed.revision)
        self.assertEqual(
            replayed_head.cards[1].evidence_digest,
            refreshed_a.evidence_digest,
        )

    def test_shortlist_attach_inspection_commits_dirty_title_notes_and_order(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-attach-principal"),
            principal_id="operator",
            kind="human",
        )
        reviews = RealmReviewCollectionService(self.ledger, principal)
        shortlists = RealmShortlistService(reviews)
        rows = {row["id"]: row for row in self.model().page("candidate")["items"]}
        first = shortlists.save_candidate(
            operation_id=self.op("shortlist-attach-add-a"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-a"]["selection"],
            draft=ShortlistDraft.empty(),
        )
        second = shortlists.save_candidate(
            operation_id=self.op("shortlist-attach-add-b"),
            run_id=self.created.run.run_id,
            presentation_selection=rows["candidate-b"]["selection"],
            draft=ShortlistDraft.from_revision(first),
        )
        dirty = ShortlistDraft(
            shortlist_id=second.shortlist_id,
            expected_revision=second.revision,
            title="Inspected finalists",
            cards=(
                ShortlistCardDraft(
                    second.cards[1].selection.selection_digest,
                    "Unsaved B note",
                ),
                ShortlistCardDraft(
                    second.cards[0].selection.selection_digest,
                    "Unsaved A note",
                ),
            ),
        )
        outcome = {
            "schema": REVIEW_INSPECTION_OUTCOME_SCHEMA,
            "kind": "operator_job",
            "operator_job_id": "job-for-candidate-a",
            "completed_at": 123.0,
        }
        with mock.patch.object(
            RealmReviewCollectionService,
            "_operator_job_inspection_outcome",
            return_value=outcome,
        ):
            attached = shortlists.attach_inspection(
                operation_id=self.op("shortlist-attach-inspection-a"),
                run_id=self.created.run.run_id,
                candidate_id="candidate-a",
                operator_job_id="job-for-candidate-a",
                draft=dirty,
            )

        self.assertEqual(attached.title, "Inspected finalists")
        self.assertEqual(
            [card.candidate_id for card in attached.cards],
            ["candidate-b", "candidate-a"],
        )
        self.assertEqual(
            [card.note for card in attached.cards],
            ["Unsaved B note", "Unsaved A note"],
        )
        self.assertEqual(
            attached.cards[1].inspection_outcomes[0]["operator_job_id"],
            "job-for-candidate-a",
        )

    def test_shortlist_public_inspection_reference_rehydrates_stored_outcome(self) -> None:
        principal = self.ledger.register_principal(
            operation_id=self.op("shortlist-public-outcome-principal"),
            principal_id="operator",
            kind="human",
        )
        reviews = RealmReviewCollectionService(self.ledger, principal)
        shortlists = RealmShortlistService(reviews)
        row = next(
            item
            for item in self.model().page("candidate")["items"]
            if item["id"] == "candidate-a"
        )
        first = shortlists.save_candidate(
            operation_id=self.op("shortlist-public-outcome-add"),
            run_id=self.created.run.run_id,
            presentation_selection=row["selection"],
            draft=ShortlistDraft.empty(),
        )
        authority_outcome = {
            "schema": REVIEW_INSPECTION_OUTCOME_SCHEMA,
            "kind": "operator_job",
            "operator_job_id": "job-for-candidate-a",
            "job_kind": "candidate-debug-run",
            "plan_digest": "a" * 64,
            "target": {"private": "must-survive-but-never-trust-the-browser"},
            "outcome": {"status": "succeeded", "code": "completed"},
            "result": None,
            "completed_at": 123.0,
        }
        with mock.patch.object(
            RealmReviewCollectionService,
            "_operator_job_inspection_outcome",
            return_value=authority_outcome,
        ):
            attached = shortlists.attach_inspection(
                operation_id=self.op("shortlist-public-outcome-attach"),
                run_id=self.created.run.run_id,
                candidate_id="candidate-a",
                operator_job_id="job-for-candidate-a",
                draft=ShortlistDraft.from_revision(first),
            )

        public_outcome = attached.to_dict()["cards"][0]["inspection_outcomes"][0]
        public_draft = ShortlistDraft(
            shortlist_id=attached.shortlist_id,
            expected_revision=attached.revision,
            title=attached.title,
            cards=(
                ShortlistCardDraft(
                    attached.cards[0].selection.selection_digest,
                    attached.cards[0].note,
                    (public_outcome,),
                ),
            ),
        )
        unchanged = shortlists.save_changes(
            operation_id=self.op("shortlist-public-outcome-no-op"),
            run_id=self.created.run.run_id,
            draft=public_draft,
        )
        self.assertEqual(unchanged.revision, attached.revision)

        tampered_reference = {
            **public_outcome,
            "plan_digest": "0" * 64,
            "target": {"injected": "must-not-be-stored"},
        }
        edited = shortlists.save_changes(
            operation_id=self.op("shortlist-public-outcome-note"),
            run_id=self.created.run.run_id,
            draft=ShortlistDraft(
                shortlist_id=attached.shortlist_id,
                expected_revision=attached.revision,
                title=attached.title,
                cards=(
                    ShortlistCardDraft(
                        attached.cards[0].selection.selection_digest,
                        "A note saved after a public round-trip",
                        (tampered_reference,),
                    ),
                ),
            ),
        )
        self.assertEqual(edited.revision, attached.revision + 1)
        self.assertEqual(
            edited.cards[0].inspection_outcomes[0],
            authority_outcome,
        )

        forged = {**public_outcome, "operator_job_id": "unknown-job"}
        with self.assertRaisesRegex(RealmConflict, "completed job"):
            shortlists.save_changes(
                operation_id=self.op("shortlist-public-outcome-forged"),
                run_id=self.created.run.run_id,
                draft=ShortlistDraft(
                    shortlist_id=edited.shortlist_id,
                    expected_revision=edited.revision,
                    title=edited.title,
                    cards=(
                        ShortlistCardDraft(
                            edited.cards[0].selection.selection_digest,
                            edited.cards[0].note,
                            (forged,),
                        ),
                    ),
                ),
            )


class RealmCandidateResultProjectionTest(unittest.TestCase):
    """Decision-surface checks for complete and incomplete candidate plans."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.principal = self.ledger.register_principal(
            operation_id="candidate-results/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="candidate-results/store/local-a",
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
            actor_principal_id="operator",
            prefix="candidate-results",
        )
        manifest = replace(
            prepare_test_run_control_manifest(self.closure, max_trials=12),
            retry_policy=RetryPolicy(
                max_attempts=2,
                retryable_outcomes=("failed",),
            ),
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            manifest,
            closure_bindings,
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="candidate-results/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="candidate-result-run",
            owner_id="candidate-result-owner",
        )
        self.operation_index = 0
        self.run_revision = 0
        self.owner_revision = 0
        self._admit_candidate_plans()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.operation_index += 1
        return f"candidate-results/{self.operation_index}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit_candidate_plans(self) -> None:
        # D is deliberately accepted before B. Result ordering must still put
        # ranked B/A first and retain accepted order only for the evidence tail.
        candidate_suffixes = ("a", "d", "b", "c", "e", "f")
        candidates = tuple(
            CandidateAdmission(
                f"candidate-{suffix}",
                NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec={"name": suffix},
                ),
                lineage={"parents": []},
                generator={"method_id": "test-method"},
            )
            for suffix in candidate_suffixes
        )
        trials = []
        for suffix in candidate_suffixes:
            seeds = (33, 44) if suffix in {"c", "f"} else (11, 22)
            for repetition_index, seed in enumerate(seeds):
                trials.append(
                    LogicalTrialAdmission(
                        f"trial-{suffix}{repetition_index}",
                        f"candidate-{suffix}",
                        seed=seed,
                        repetition_index=repetition_index,
                    )
                )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        receipt = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=RunAdmissionPlan(candidates, tuple(trials)),
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision

    def _prepare(self, trial_id: str, attempt_id: str):
        receipt = self.ledger.prepare_run_attempt(
            operation_id=self.op(f"prepare-{attempt_id}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id=trial_id,
            attempt_id=attempt_id,
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        return receipt

    def _adopt(
        self,
        prepared,
        *,
        outcome: str,
        metric: float | None,
        extra_metrics: dict[str, object] | None = None,
        constraints: dict[str, object] | None = None,
        code: str | None = None,
        platform_error: bool = False,
    ):
        if platform_error:
            finalization = AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome=outcome,
                effective_code=code,
                captured_artifacts=(),
                platform_error={
                    "code": code,
                    "message": "The test attempt ended before evaluation.",
                    "details": {},
                },
            )
        else:
            envelope = AttemptEnvelope(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                outcome=outcome,
                phase="environment_evaluation",
                wall_clock_seconds=0.1,
                validation={"accepted": True, "errors": []},
                materialization={"runtime_spec": {}, "metadata": {}},
                metric_values={
                    **({} if metric is None else {"score": metric}),
                    **(extra_metrics or {}),
                },
                constraint_results=constraints or {},
                output_declarations=(),
                event_summary={},
                execution_metadata={"worker": "test"},
                error=(
                    {}
                    if outcome == "success"
                    else {
                        "phase": "environment_evaluation",
                        "type": "RuntimeError",
                        "message": "evaluation failed",
                    }
                ),
            )
            finalization = AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome=outcome,
                effective_code=code,
                captured_artifacts=(),
                envelope=envelope,
            )
        receipt = self.ledger.adopt_run_attempt(
            operation_id=self.op(f"adopt-{prepared.attempt.attempt_id}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=prepared.attempt.capture_change_id,
            finalization=finalization,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision
        return receipt

    def _complete(
        self,
        trial_id: str,
        attempt_id: str,
        metric: float | None,
        *,
        outcome: str = "success",
        extra_metrics: dict[str, object] | None = None,
        constraints: dict[str, object] | None = None,
        code: str | None = None,
        platform_error: bool = False,
    ):
        return self._adopt(
            self._prepare(trial_id, attempt_id),
            outcome=outcome,
            metric=metric,
            extra_metrics=extra_metrics,
            constraints=constraints,
            code=code,
            platform_error=platform_error,
        )

    def _complete_pair(self, suffix: str, values: tuple[float, float]) -> None:
        for index, value in enumerate(values):
            self._complete(
                f"trial-{suffix}{index}",
                f"attempt-{suffix}{index}",
                value,
            )

    def snapshot(self):
        return self.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
        )

    @staticmethod
    def _with_aggregation(snapshot, mode: str):
        template = replace(
            snapshot.evaluation_closure.evaluation_template,
            objective={
                "primaryMetric": {"name": "score", "direction": "maximize"},
                "aggregation": {"mode": mode},
            },
        )
        closure = replace(
            snapshot.evaluation_closure,
            evaluation_template=template,
        )
        definition = replace(snapshot.definition, evaluation_closure=closure)
        return replace(snapshot, definition=definition)

    @staticmethod
    def _with_secondary_metrics(snapshot, names: tuple[str, ...]):
        objective = snapshot.evaluation_closure.evaluation_template.objective
        template = replace(
            snapshot.evaluation_closure.evaluation_template,
            objective={
                "primaryMetric": {
                    "name": snapshot.control.manifest.objective_metric,
                    "direction": snapshot.control.manifest.objective_direction,
                },
                "secondaryMetrics": list(names),
                "aggregation": {
                    "mode": objective.get("aggregation", {}).get("mode")
                },
            },
        )
        closure = replace(
            snapshot.evaluation_closure,
            evaluation_template=template,
        )
        definition = replace(snapshot.definition, evaluation_closure=closure)
        return replace(snapshot, definition=definition)

    @staticmethod
    def _results_by_candidate(model: RunWorkbenchReadModel) -> dict[str, dict]:
        return {
            row["id"]: row["data"]["result"]
            for row in model.page("candidate")["items"]
        }

    def _complete_decision_surface(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        first_b = self._complete(
            "trial-b0",
            "attempt-b0-first",
            999.0,
            outcome="failed",
            code="evaluation_failed",
        )
        self.assertEqual(first_b.logical_transition.to_state, "retrying")
        self._complete("trial-b0", "attempt-b0-final", 8.0)
        self._complete("trial-b1", "attempt-b1", 4.0)
        self._complete_pair("c", (10.0, 10.0))
        self._complete(
            "trial-e0",
            "attempt-e0",
            None,
            outcome="invalid",
            code="candidate_invalid",
            platform_error=True,
        )
        self._complete("trial-e1", "attempt-e1", 3.0)
        self._complete("trial-f0", "attempt-f0", 5.0)
        self._complete("trial-f1", "attempt-f1", None)

    def test_candidate_results_rank_complete_plans_and_keep_other_evidence(self) -> None:
        self._complete_decision_surface()
        model = RunWorkbenchReadModel.from_snapshot(self.snapshot())
        page = model.page("candidate", limit=6)
        self.assertEqual(
            [row["id"] for row in page["items"]],
            [
                "candidate-b",
                "candidate-a",
                "candidate-d",
                "candidate-e",
                "candidate-c",
                "candidate-f",
            ],
        )
        results = self._results_by_candidate(model)

        self.assertEqual(results["candidate-b"]["status"], "rankable")
        self.assertEqual(
            results["candidate-b"]["aggregate"],
            {"value": 6.0, "sample_count": 2},
        )
        self.assertEqual(results["candidate-b"]["comparison"]["rank"], 1)
        self.assertEqual(
            results["candidate-b"]["comparison"]["ranked_candidate_count"], 2
        )
        self.assertEqual(results["candidate-b"]["comparison"]["group_size"], 2)
        self.assertEqual(results["candidate-b"]["comparison"]["group_ordinal"], 1)
        self.assertEqual(
            results["candidate-b"]["counts"],
            {
                "logical_trials": 2,
                "active": 0,
                "terminal": 2,
                "successful": 2,
                "terminal_failures": 0,
                "usable_objectives": 2,
                "attempts": 3,
                "retries": 1,
            },
        )
        self.assertEqual(results["candidate-a"]["comparison"]["rank"], 2)
        self.assertEqual(results["candidate-a"]["aggregate"]["value"], 4.0)
        self.assertEqual(
            results["candidate-a"]["evaluation_plan"]["digest"],
            results["candidate-b"]["evaluation_plan"]["digest"],
        )

        candidate_c = results["candidate-c"]
        self.assertEqual(candidate_c["status"], "aggregate_only")
        self.assertIsNone(candidate_c["reason"])
        self.assertEqual(
            candidate_c["comparison"]["reason"], "evaluation_plan_mismatch"
        )
        self.assertEqual(candidate_c["comparison"]["group_size"], 1)
        self.assertEqual(candidate_c["comparison"]["ranked_candidate_count"], 0)
        self.assertEqual(candidate_c["comparison"]["group_ordinal"], 2)
        self.assertNotEqual(
            candidate_c["evaluation_plan"]["digest"],
            results["candidate-a"]["evaluation_plan"]["digest"],
        )
        self.assertEqual(
            results["candidate-d"]["reason"], "candidate_evaluation_active"
        )
        self.assertEqual(
            results["candidate-e"]["reason"], "terminal_result_not_successful"
        )
        self.assertEqual(
            results["candidate-f"]["reason"],
            "primary_objective_missing_or_nonfinite",
        )
        self.assertEqual(
            page["candidate_result_summary"]["counts"],
            {
                "rankable": 2,
                "aggregate_only": 1,
                "evidence_only": 3,
                "comparison_groups": 2,
                "ranked_groups": 1,
            },
        )
        self.assertEqual(
            results["candidate-b"]["comparison"]["finality"],
            "provisional_at_head",
        )
        ranking = page["capabilities"]["candidate_results"]["ranking"]
        self.assertTrue(ranking["eligible"])
        self.assertIsNone(ranking["reason"])

        observations = model.page("observation")["items"]
        failed_retry = next(
            row for row in observations if row["data"]["attempt_id"] == "attempt-b0-first"
        )
        self.assertEqual(failed_retry["data"]["objective_value"], 999.0)
        self.assertEqual(
            results["candidate-b"]["representative"]["logical_trial_id"],
            "trial-b1",
        )

    def test_candidate_comparison_uses_complete_plan_secondary_metrics(self) -> None:
        self._complete(
            "trial-a0",
            "attempt-a0",
            2.0,
            extra_metrics={"cost": 10.0, "latency": 1.0},
        )
        self._complete(
            "trial-a1",
            "attempt-a1",
            6.0,
            extra_metrics={"cost": 14.0, "latency": 2.0},
        )
        retry = self._complete(
            "trial-b0",
            "attempt-b0-failed",
            999.0,
            outcome="failed",
            extra_metrics={"cost": 999.0, "latency": 999.0},
            code="evaluation_failed",
        )
        self.assertEqual(retry.logical_transition.to_state, "retrying")
        self._complete(
            "trial-b0",
            "attempt-b0-final",
            8.0,
            extra_metrics={"cost": 8.0, "latency": True},
        )
        self._complete(
            "trial-b1",
            "attempt-b1",
            4.0,
            extra_metrics={"cost": 10.0, "latency": True},
        )
        snapshot = self._with_secondary_metrics(
            self.snapshot(), ("cost", "latency")
        )
        page = RunWorkbenchReadModel.from_snapshot(snapshot).page("candidate")
        candidates = {row["id"]: row for row in page["items"]}
        projection = RunCandidateComparisonProjection.from_snapshot(
            snapshot,
            baseline_presentation_selection=candidates["candidate-a"]["selection"],
            comparison_presentation_selection=candidates["candidate-b"]["selection"],
        ).to_dict()

        self.assertEqual(
            projection["schema"], "optpilot.run-candidate-comparison.v3"
        )
        self.assertTrue(projection["eligibility"]["eligible"])
        outcomes = projection["outcomes"]
        self.assertEqual(outcomes["evaluation_plan"]["relation"], "matching")
        self.assertEqual(outcomes["metrics"]["total"], 3)
        self.assertFalse(outcomes["metrics"]["truncated"])
        metrics = {row["name"]: row for row in outcomes["metrics"]["rows"]}

        primary = metrics["score"]
        self.assertEqual(primary["role"], "primary")
        self.assertEqual(primary["direction"], "maximize")
        self.assertEqual(primary["baseline"]["aggregate"]["value"], 4.0)
        self.assertEqual(primary["comparison"]["aggregate"]["value"], 6.0)
        self.assertEqual(primary["relation"]["delta"], 2.0)
        self.assertEqual(primary["relation"]["numeric"], "higher")
        self.assertEqual(
            primary["relation"]["preferred_operand"], "comparison"
        )
        self.assertEqual(
            primary["baseline"]["aggregate"],
            projection["operands"][0]["result"]["aggregate"],
        )

        cost = metrics["cost"]
        self.assertEqual(cost["role"], "secondary")
        self.assertIsNone(cost["direction"])
        self.assertEqual(cost["baseline"]["aggregate"]["value"], 12.0)
        self.assertEqual(cost["comparison"]["aggregate"]["value"], 9.0)
        self.assertEqual(cost["relation"]["delta"], -3.0)
        self.assertEqual(cost["relation"]["numeric"], "lower")
        self.assertIsNone(cost["relation"]["preferred_operand"])

        latency = metrics["latency"]
        self.assertEqual(latency["baseline"]["aggregate"]["value"], 1.5)
        self.assertIsNone(latency["comparison"]["aggregate"])
        self.assertEqual(
            latency["comparison"]["reason"], "metric_missing_or_nonfinite"
        )
        self.assertEqual(
            latency["relation"]["reason"], "comparison_metric_incomplete"
        )
        self.assertEqual(
            outcomes["constraints"]["eligibility"]["code"],
            "no_constraint_results",
        )
        self.assertEqual(outcomes["constraints"]["rows"], [])
        self.assertLessEqual(
            len(json.dumps(projection, sort_keys=True).encode("utf-8")),
            RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES,
        )

    def test_boolean_constraint_comparison_reports_coverage_and_feasibility(self) -> None:
        for suffix, constraints in (
            ("a", {"capacity": True, "safety": False}),
            ("b", {"capacity": True, "safety": True}),
        ):
            for index, score in enumerate((2.0, 4.0)):
                self._complete(
                    f"trial-{suffix}{index}",
                    f"attempt-{suffix}{index}",
                    score,
                    constraints=constraints,
                )
        snapshot = self.snapshot()
        candidates = {
            row["id"]: row
            for row in RunWorkbenchReadModel.from_snapshot(snapshot).page(
                "candidate"
            )["items"]
        }
        outcomes = RunCandidateComparisonProjection.from_snapshot(
            snapshot,
            baseline_presentation_selection=candidates["candidate-a"]["selection"],
            comparison_presentation_selection=candidates["candidate-b"]["selection"],
        ).to_dict()["outcomes"]

        constraints = outcomes["constraints"]
        self.assertEqual(constraints["eligibility"]["code"], "ready")
        rows = {row["name"]: row for row in constraints["rows"]}
        self.assertEqual(rows["capacity"]["relation"]["relation"], "both_satisfied")
        self.assertEqual(rows["capacity"]["relation"]["preferred_operand"], "tie")
        self.assertEqual(rows["safety"]["baseline"]["coverage"]["violated"], 2)
        self.assertEqual(rows["safety"]["comparison"]["coverage"]["satisfied"], 2)
        self.assertEqual(
            rows["safety"]["relation"]["relation"],
            "comparison_only_satisfied",
        )
        self.assertEqual(
            rows["safety"]["relation"]["preferred_operand"], "comparison"
        )

    def test_outcome_comparison_is_metric_bounded_and_plan_scoped(self) -> None:
        secondary_names = tuple(f"secondary_{index:02d}" for index in range(40))
        extra = {name: float(index) for index, name in enumerate(secondary_names)}
        for suffix, score in (("a", 2.0), ("c", 5.0)):
            for index in range(2):
                self._complete(
                    f"trial-{suffix}{index}",
                    f"attempt-{suffix}{index}",
                    score,
                    extra_metrics=extra,
                )
        snapshot = self._with_secondary_metrics(
            self.snapshot(), secondary_names
        )
        page = RunWorkbenchReadModel.from_snapshot(snapshot).page("candidate")
        candidates = {row["id"]: row for row in page["items"]}
        projection = RunCandidateComparisonProjection.from_snapshot(
            snapshot,
            baseline_presentation_selection=candidates["candidate-a"]["selection"],
            comparison_presentation_selection=candidates["candidate-c"]["selection"],
        ).to_dict()

        outcomes = projection["outcomes"]
        self.assertEqual(outcomes["evaluation_plan"]["relation"], "different")
        self.assertEqual(outcomes["metrics"]["total"], 41)
        self.assertEqual(outcomes["metrics"]["returned"], 32)
        self.assertEqual(outcomes["metrics"]["omitted"], 9)
        self.assertTrue(outcomes["metrics"]["truncated"])
        self.assertEqual(
            [row["name"] for row in outcomes["metrics"]["rows"][:3]],
            ["score", "secondary_00", "secondary_01"],
        )
        for row in outcomes["metrics"]["rows"]:
            self.assertFalse(row["relation"]["eligible"])
            self.assertEqual(
                row["relation"]["reason"], "evaluation_plan_mismatch"
            )
        self.assertLessEqual(
            len(json.dumps(projection, sort_keys=True).encode("utf-8")),
            RUN_CANDIDATE_COMPARISON_MAX_RESPONSE_BYTES,
        )

    def test_irreversible_evidence_wins_over_active_trials(self) -> None:
        self._complete(
            "trial-d0",
            "attempt-d0",
            None,
            outcome="invalid",
            code="candidate_invalid",
            platform_error=True,
        )
        self._complete("trial-f0", "attempt-f0", None)
        results = self._results_by_candidate(
            RunWorkbenchReadModel.from_snapshot(self.snapshot())
        )

        failed_and_active = results["candidate-d"]
        self.assertEqual(failed_and_active["counts"]["active"], 1)
        self.assertEqual(
            failed_and_active["reason"], "terminal_result_not_successful"
        )
        missing_and_active = results["candidate-f"]
        self.assertEqual(missing_and_active["counts"]["active"], 1)
        self.assertEqual(
            missing_and_active["reason"],
            "primary_objective_missing_or_nonfinite",
        )

    def test_two_ranked_plan_groups_order_and_page_by_group_then_rank(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        self._complete_pair("b", (8.0, 4.0))
        self._complete_pair("c", (10.0, 10.0))
        self._complete_pair("f", (7.0, 7.0))
        model = RunWorkbenchReadModel.from_snapshot(self.snapshot())

        first = model.page("candidate", limit=3)
        self.assertEqual(
            [row["id"] for row in first["items"]],
            ["candidate-b", "candidate-a", "candidate-d"],
        )
        self.assertTrue(first["page"]["has_more"])
        second = model.page(
            "candidate",
            page_token=first["page"]["next_page_token"],
            limit=3,
        )
        self.assertEqual(
            [row["id"] for row in second["items"]],
            ["candidate-e", "candidate-c", "candidate-f"],
        )
        self.assertFalse(second["page"]["has_more"])
        self.assertEqual(first["query"]["order"], RUN_CANDIDATE_RESULT_ORDER)

        results = self._results_by_candidate(model)
        for candidate_id in ("candidate-a", "candidate-b"):
            comparison = results[candidate_id]["comparison"]
            self.assertEqual(comparison["group_ordinal"], 1)
            self.assertEqual(comparison["group_size"], 2)
            self.assertEqual(comparison["ranked_candidate_count"], 2)
            self.assertEqual(comparison["scope"], "within_evaluation_plan")
        for candidate_id in ("candidate-c", "candidate-f"):
            comparison = results[candidate_id]["comparison"]
            self.assertEqual(comparison["group_ordinal"], 2)
            self.assertEqual(comparison["group_size"], 2)
            self.assertEqual(comparison["ranked_candidate_count"], 2)
        self.assertEqual(
            first["candidate_result_summary"]["counts"],
            {
                "rankable": 4,
                "aggregate_only": 0,
                "evidence_only": 2,
                "comparison_groups": 2,
                "ranked_groups": 2,
            },
        )
        ranking = first["capabilities"]["candidate_results"]["ranking"]
        self.assertTrue(ranking["supported"])
        self.assertTrue(ranking["eligible"])
        self.assertEqual(ranking["scope"], "within_run_evaluation_plan")
        self.assertEqual(ranking["finality"], "provisional_at_head")
        self.assertIsNone(ranking["reason"])

    def test_overview_projects_complete_candidate_best_counts_and_series(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        self._complete_pair("b", (8.0, 4.0))

        projection = RunOverviewProjection.from_snapshot(self.snapshot())
        payload = projection.to_dict()

        self.assertEqual(payload["schema"], RUN_OVERVIEW_PROJECTION_SCHEMA)
        self.assertEqual(payload["run_id"], self.created.run.run_id)
        self.assertEqual(payload["head"], projection.head)
        self.assertEqual(
            payload["counts"]["candidates"],
            {
                "accepted": 6,
                "complete": 2,
                "incomplete": 4,
                "comparison_groups": 1,
                "ranked_groups": 1,
            },
        )
        self.assertEqual(payload["counts"]["logical_trials"]["accepted"], 12)
        self.assertEqual(payload["counts"]["logical_trials"]["terminal"], 4)
        self.assertEqual(payload["counts"]["logical_trials"]["stopped"], 0)
        self.assertEqual(payload["failure_count"], 0)
        self.assertEqual(
            payload["best_candidate"],
            {
                "available": True,
                "reason": None,
                "candidate_id": "candidate-b",
                "value": 6.0,
                "sample_count": 2,
                "rank": 1,
                "tie_count": 1,
                "evaluation_plan_group": 1,
            },
        )
        series = payload["objective_series"]
        self.assertEqual(series["schema"], RUN_OVERVIEW_OBJECTIVE_SERIES_SCHEMA)
        self.assertEqual(series["order"], RUN_OVERVIEW_OBJECTIVE_ORDER)
        self.assertEqual(series["total_complete_candidates"], 2)
        self.assertEqual(series["returned"], 2)
        self.assertFalse(series["truncated"])
        self.assertEqual(
            [point["candidate_id"] for point in series["points"]],
            ["candidate-a", "candidate-b"],
        )
        self.assertEqual(
            series["summary"],
            {"minimum": 4.0, "maximum": 6.0, "last_in_order": 6.0},
        )
        self.assertEqual(
            payload["limitations"]["max_objective_points"],
            RUN_OVERVIEW_MAX_OBJECTIVE_POINTS,
        )
        self.assertTrue(payload["limitations"]["entity_page_size_independent"])

    def test_overview_refuses_to_invent_a_best_across_incomplete_or_different_plans(
        self,
    ) -> None:
        waiting = RunOverviewProjection.from_snapshot(self.snapshot()).to_dict()
        self.assertFalse(waiting["best_candidate"]["available"])
        self.assertEqual(
            waiting["best_candidate"]["reason"], "no_complete_candidate_yet"
        )

        self._complete_pair("a", (2.0, 6.0))
        one_complete = RunOverviewProjection.from_snapshot(self.snapshot()).to_dict()
        self.assertEqual(
            one_complete["best_candidate"]["reason"],
            "only_one_complete_candidate",
        )

        self._complete_pair("c", (10.0, 10.0))
        different_plans = RunOverviewProjection.from_snapshot(self.snapshot()).to_dict()
        self.assertEqual(
            different_plans["best_candidate"]["reason"],
            "complete_candidates_use_different_evaluation_plans",
        )

        self._complete(
            "trial-d0",
            "attempt-d0-first",
            None,
            outcome="failed",
            code="worker_failed",
        )
        self._complete(
            "trial-d0",
            "attempt-d0-final",
            None,
            outcome="failed",
            code="worker_failed",
        )
        with_failure = RunOverviewProjection.from_snapshot(self.snapshot()).to_dict()
        self.assertEqual(with_failure["failure_count"], 1)
        self.assertEqual(with_failure["counts"]["logical_trials"]["failed"], 1)

    def test_workbench_overview_is_invariant_to_entity_page_size(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        self._complete_pair("b", (8.0, 4.0))
        service = RealmRunViewService(self.ledger, self.principal)
        ref = RunViewRef(run_id=self.created.run.run_id)

        one_row = service.workbench_bundle(ref=ref, limit=1)
        all_rows = service.workbench_bundle(ref=ref, limit=6)

        self.assertEqual(len(one_row.pages["candidate"]["items"]), 1)
        self.assertEqual(len(all_rows.pages["candidate"]["items"]), 6)
        self.assertEqual(
            one_row.head.overview.to_dict(),
            all_rows.head.overview.to_dict(),
        )
        self.assertEqual(
            one_row.head.to_dict()["overview"]["head"],
            one_row.head.to_dict()["head"],
        )

    def test_aggregation_modes_missing_and_nonfinite_are_explicit(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        self._complete_pair("b", (8.0, 4.0))
        snapshot = self.snapshot()
        expected = {
            "mean": 4.0,
            "median": 4.0,
            "min": 2.0,
            "max": 6.0,
            "sum": 8.0,
            "last": 6.0,
            "weighted_mean": 4.0,
        }
        candidate_key = next(
            candidate.candidate_key
            for candidate in snapshot.candidates
            if candidate.candidate_id == "candidate-a"
        )
        for mode, value in expected.items():
            index = CandidateResultIndex.from_snapshot(
                self._with_aggregation(snapshot, mode)
            )
            self.assertEqual(index.for_candidate_key(candidate_key)["aggregate"]["value"], value)

        candidate_d_key = next(
            candidate.candidate_key
            for candidate in snapshot.candidates
            if candidate.candidate_id == "candidate-d"
        )
        unsupported = CandidateResultIndex.from_snapshot(
            self._with_aggregation(snapshot, "unsupported")
        ).for_candidate_key(candidate_d_key)
        self.assertEqual(
            unsupported["reason"], "objective_aggregation_not_supported"
        )

        attempt_a0 = next(
            attempt
            for attempt in snapshot.attempts
            if attempt.logical_trial_id == "trial-a0"
        )
        without_observation = replace(
            snapshot,
            observations=tuple(
                observation
                for observation in snapshot.observations
                if observation.attempt_id != attempt_a0.attempt_id
            ),
        )
        missing = CandidateResultIndex.from_snapshot(
            without_observation
        ).for_candidate_key(candidate_key)
        self.assertEqual(missing["reason"], "terminal_observation_missing")

        observation_a0 = next(
            observation
            for observation in snapshot.observations
            if observation.attempt_id == attempt_a0.attempt_id
        )
        poisoned_envelope = copy.copy(observation_a0.envelope)
        object.__setattr__(
            poisoned_envelope,
            "metric_values",
            MappingProxyType({"score": float("inf")}),
        )
        poisoned_observation = replace(
            observation_a0,
            envelope=poisoned_envelope,
        )
        poisoned_snapshot = replace(
            snapshot,
            observations=tuple(
                poisoned_observation
                if observation.attempt_id == attempt_a0.attempt_id
                else observation
                for observation in snapshot.observations
            ),
        )
        nonfinite = CandidateResultIndex.from_snapshot(
            poisoned_snapshot
        ).for_candidate_key(candidate_key)
        self.assertEqual(
            nonfinite["reason"], "primary_objective_missing_or_nonfinite"
        )

    def test_ties_use_competition_rank_and_terminal_run_marks_results_final(self) -> None:
        self._complete_pair("a", (2.0, 6.0))
        self._complete_pair("d", (0.0, 2.0))
        self._complete_pair("b", (2.0, 6.0))
        self._complete_pair("c", (10.0, 10.0))
        self._complete_pair("e", (-1.0, 1.0))
        self._complete_pair("f", (-2.0, 0.0))
        finished = self.ledger.finish_run(
            operation_id=self.op("finish"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = finished.revision.revision

        model = RunWorkbenchReadModel.from_snapshot(self.snapshot())
        page = model.page("candidate")
        results = self._results_by_candidate(model)
        self.assertEqual(
            [row["id"] for row in page["items"][:2]],
            ["candidate-a", "candidate-b"],
        )
        for candidate_id in ("candidate-a", "candidate-b"):
            comparison = results[candidate_id]["comparison"]
            self.assertEqual(comparison["rank"], 1)
            self.assertEqual(comparison["tie_count"], 2)
            self.assertEqual(comparison["group_size"], 4)
            self.assertEqual(comparison["ranked_candidate_count"], 4)
            self.assertEqual(comparison["finality"], "final")
        self.assertEqual(results["candidate-d"]["comparison"]["rank"], 3)
        self.assertEqual(page["candidate_result_summary"]["finality"], "final")


if __name__ == "__main__":
    unittest.main()
