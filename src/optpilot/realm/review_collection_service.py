"""Actor-bound product service for Review Collection decision workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ._validation import thaw_json
from .errors import RealmConflict, RealmNotFound
from .ledger import PrincipalRecord, RealmLedger
from .operator_job_records import OperatorJobRecord
from .refs import canonical_json_bytes, request_digest
from .review_collections import (
    REVIEW_COLLECTION_ITEM_EVIDENCE_SCHEMA,
    REVIEW_INSPECTION_OUTCOME_SCHEMA,
    ReviewCollectionDeletionReceipt,
    ReviewCollectionEntryDraft,
    ReviewCollectionHistoryPage,
    ReviewCollectionNewItem,
    ReviewCollectionRevision,
)
from .run_candidate_results import CandidateResultIndex
from .run_comparability import RunComparabilityProjection
from .run_workbench import (
    _bounded_observation_constraints,
    _bounded_observation_metrics,
    validate_run_workbench_selection,
)
from .run_views import RunViewRef
from .selections import SelectionRef


REVIEW_COLLECTION_MAX_FROZEN_OBSERVATIONS = 100
REVIEW_COLLECTION_MAX_FROZEN_ARTIFACTS = 100
REVIEW_COLLECTION_MAX_FROZEN_JOB_FIELDS = 64
REVIEW_COLLECTION_MAX_FROZEN_JOB_OUTPUTS = 100
REVIEW_COLLECTION_MAX_FROZEN_JOB_MAPPING_BYTES = 32 * 1024
_REVIEW_OPERATOR_JOB_KINDS = frozenset(
    {"candidate-debug-run", "environment-preview"}
)


def _draft_matches_revision(
    revision: ReviewCollectionRevision,
    *,
    title: str,
    entries: Sequence[ReviewCollectionEntryDraft],
) -> bool:
    """Return whether a form draft would make no user-visible change."""

    draft = tuple(entries)
    if title != revision.title or len(draft) != len(revision.items):
        return False
    return all(
        entry.selection_digest == item.selection.selection_digest
        and entry.note == item.note
        and tuple(
            canonical_json_bytes(thaw_json(outcome))
            for outcome in entry.inspection_outcomes
        )
        == tuple(
            canonical_json_bytes(thaw_json(outcome))
            for outcome in item.inspection_outcomes
        )
        for entry, item in zip(draft, revision.items)
    )


def _rehydrate_attached_inspection_outcomes(
    revision: ReviewCollectionRevision,
    *,
    entries: Sequence[ReviewCollectionEntryDraft],
) -> tuple[ReviewCollectionEntryDraft, ...]:
    """Replace public inspection references with their stored authority records.

    Studio never receives the target or execution authority held in an Operator
    Job outcome.  A normal browser round-trip therefore cannot reproduce the
    stored mapping byte-for-byte.  Treat an outcome supplied in a draft as an
    opaque reference by ``operator_job_id`` and resolve it only against an
    outcome already attached to the same saved selection.  New evidence still
    has to enter through :meth:`_operator_job_inspection_outcome`.
    """

    existing: dict[str, dict[str, Mapping[str, Any]]] = {}
    for item in revision.items:
        by_job_id: dict[str, Mapping[str, Any]] = {}
        for outcome in item.inspection_outcomes:
            if outcome.get("schema") != REVIEW_INSPECTION_OUTCOME_SCHEMA:
                continue
            job_id = outcome.get("operator_job_id")
            if isinstance(job_id, str) and job_id:
                by_job_id[job_id] = outcome
        existing[item.selection.selection_digest] = by_job_id

    normalized: list[ReviewCollectionEntryDraft] = []
    for entry in entries:
        by_job_id = existing.get(entry.selection_digest, {})
        seen_job_ids: set[str] = set()
        outcomes: list[Mapping[str, Any]] = []
        for outcome in entry.inspection_outcomes:
            if outcome.get("schema") != REVIEW_INSPECTION_OUTCOME_SCHEMA:
                outcomes.append(outcome)
                continue
            job_id = outcome.get("operator_job_id")
            authoritative = by_job_id.get(job_id) if isinstance(job_id, str) else None
            if authoritative is None or job_id in seen_job_ids:
                raise RealmConflict(
                    "Operator Job inspection evidence must be attached by job id."
                )
            seen_job_ids.add(job_id)
            outcomes.append(authoritative)
        normalized.append(
            ReviewCollectionEntryDraft(
                selection_digest=entry.selection_digest,
                note=entry.note,
                inspection_outcomes=tuple(outcomes),
            )
        )
    return tuple(normalized)


def _bounded_job_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Project one portable mapping without allowing a Review revision to balloon."""

    plain = thaw_json(value)
    selected: dict[str, Any] = {}
    for key in sorted(plain):
        if len(selected) >= REVIEW_COLLECTION_MAX_FROZEN_JOB_FIELDS:
            break
        candidate = {**selected, key: plain[key]}
        if len(canonical_json_bytes(candidate)) > (
            REVIEW_COLLECTION_MAX_FROZEN_JOB_MAPPING_BYTES
        ):
            continue
        selected[key] = plain[key]
    return {
        "digest": request_digest(plain),
        "total": len(plain),
        "returned": len(selected),
        "truncated": len(selected) < len(plain),
        "values": selected,
    }


def _default_collection_id(
    *, actor_principal_id: str, run_id: str, creation_operation_id: str
) -> str:
    digest = request_digest(
        {
            "schema": "optpilot.default-run-review-collection.v2",
            "actor_principal_id": actor_principal_id,
            "run_id": run_id,
            "creation_operation_id": creation_operation_id,
        }
    )
    return f"review-{digest[:32]}"


@dataclass(frozen=True)
class RealmReviewCollectionService:
    """One trusted principal's revisioned decision-record operations."""

    _ledger: RealmLedger
    _principal: PrincipalRecord

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")

    @property
    def actor_principal_id(self) -> str:
        return self._principal.principal_id

    def read_for_run(
        self, *, run_id: str, revision: int | None = None
    ) -> ReviewCollectionRevision | None:
        return self._ledger.read_review_collection_for_source(
            actor_principal_id=self.actor_principal_id,
            source_kind="run",
            source_id=RunViewRef(run_id=run_id).run_id,
            revision=revision,
        )

    def add_candidate(
        self,
        *,
        operation_id: str,
        run_id: str,
        presentation_selection: Mapping[str, Any],
        note: str = "",
        inspection_outcomes: Sequence[Mapping[str, Any]] = (),
        operator_job_id: str | None = None,
    ) -> ReviewCollectionRevision:
        """Add one exact-head candidate, creating the default collection once."""

        inspection_values = tuple(inspection_outcomes)
        if any(
            value.get("schema") == REVIEW_INSPECTION_OUTCOME_SCHEMA
            for value in inspection_values
        ):
            raise RealmConflict(
                "Operator Job inspection evidence must be attached by job id."
            )
        ref = RunViewRef(run_id=run_id)
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.actor_principal_id,
            run_id=ref.run_id,
        )
        presented = validate_run_workbench_selection(presentation_selection)
        if (
            presented["run_id"] != ref.run_id
            or presented["kind"] != "candidate"
        ):
            raise ValueError("Add to Review requires a candidate from this run.")
        if (
            presented["revision"] != snapshot.revision.revision
            or presented["sequence"] != snapshot.revision.last_sequence
        ):
            raise RealmConflict("Run presentation head changed; refresh before saving.")
        candidate = next(
            (
                item
                for item in snapshot.candidates
                if item.candidate_id == presented["entity_id"]
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Review selection does not identify a candidate.")
        selection = self._ledger.mint_run_selection(
            actor_principal_id=self.actor_principal_id,
            run_id=ref.run_id,
            kind="candidate",
            entity_id=candidate.candidate_id,
            expected_run_revision=presented["revision"],
            expected_head_sequence=presented["sequence"],
        )
        current = self.read_for_run(run_id=ref.run_id)
        if current is not None:
            for item in current.items:
                if item.selection.selection_digest == selection.selection_digest:
                    if operator_job_id is None:
                        return current
                    return self.attach_operator_job(
                        operation_id=operation_id,
                        run_id=ref.run_id,
                        collection_id=current.collection_id,
                        expected_revision=current.revision,
                        title=current.title,
                        entries=tuple(
                            ReviewCollectionEntryDraft(
                                selection_digest=value.selection.selection_digest,
                                note=value.note,
                                inspection_outcomes=value.inspection_outcomes,
                            )
                            for value in current.items
                        ),
                        selection_digest=item.selection.selection_digest,
                        operator_job_id=operator_job_id,
                    )
        evidence = self._candidate_evidence(
            snapshot=snapshot,
            selection=selection,
            candidate_key=candidate.candidate_key,
        )
        entries = [] if current is None else [
            ReviewCollectionEntryDraft(
                selection_digest=item.selection.selection_digest,
                note=item.note,
                inspection_outcomes=item.inspection_outcomes,
            )
            for item in current.items
        ]
        outcomes = list(inspection_values)
        if operator_job_id is not None:
            outcomes.append(
                self._operator_job_inspection_outcome(
                    run_id=ref.run_id,
                    review_selection=selection,
                    operator_job_id=operator_job_id,
                )
            )
        entries.append(
            ReviewCollectionEntryDraft(
                selection_digest=selection.selection_digest,
                note=note,
                inspection_outcomes=tuple(outcomes),
            )
        )
        collection_id = (
            current.collection_id
            if current is not None
            else _default_collection_id(
                actor_principal_id=self.actor_principal_id,
                run_id=ref.run_id,
                creation_operation_id=operation_id,
            )
        )
        return self._ledger.save_review_collection_revision(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=collection_id,
            primary_source_kind="run",
            primary_source_id=ref.run_id,
            expected_revision=None if current is None else current.revision,
            title=("Review shortlist" if current is None else current.title),
            retention_policy="decision",
            entries=entries,
            new_items=(
                ReviewCollectionNewItem(selection=selection, evidence=evidence),
            ),
        )

    def history_for_run(
        self,
        *,
        run_id: str,
        before_revision: int | None = None,
        limit: int = 50,
    ) -> ReviewCollectionHistoryPage | None:
        return self._ledger.list_review_collection_history_for_source(
            actor_principal_id=self.actor_principal_id,
            source_kind="run",
            source_id=RunViewRef(run_id=run_id).run_id,
            before_revision=before_revision,
            limit=limit,
        )

    def save_revision(
        self,
        *,
        operation_id: str,
        run_id: str,
        collection_id: str,
        expected_revision: int,
        title: str,
        entries: Sequence[ReviewCollectionEntryDraft],
    ) -> ReviewCollectionRevision:
        canonical_run_id = RunViewRef(run_id=run_id).run_id
        current = self.read_for_run(run_id=canonical_run_id)
        if current is None or current.collection_id != collection_id:
            raise RealmNotFound("Review Collection was not found.")
        if current.revision != expected_revision:
            raise RealmConflict(
                "Review Collection changed; reload before saving your edits."
            )
        entries = _rehydrate_attached_inspection_outcomes(
            current,
            entries=entries,
        )
        if _draft_matches_revision(current, title=title, entries=entries):
            return current
        return self._commit_revision(
            operation_id=operation_id,
            run_id=canonical_run_id,
            collection_id=collection_id,
            expected_revision=expected_revision,
            title=title,
            entries=entries,
        )

    def delete_for_run(
        self,
        *,
        operation_id: str,
        run_id: str,
        collection_id: str,
        expected_revision: int,
        expected_revision_digest: str,
    ) -> ReviewCollectionDeletionReceipt:
        """Delete a complete collection after fencing its exact current head."""

        canonical_run_id = RunViewRef(run_id=run_id).run_id
        current = self.read_for_run(run_id=canonical_run_id)
        if current is None or current.collection_id != collection_id:
            raise RealmNotFound("Entity not found.")
        return self._ledger.delete_review_collection(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=collection_id,
            primary_source_kind="run",
            primary_source_id=canonical_run_id,
            expected_revision=expected_revision,
            expected_revision_digest=expected_revision_digest,
        )

    def _commit_revision(
        self,
        *,
        operation_id: str,
        run_id: str,
        collection_id: str,
        expected_revision: int,
        title: str,
        entries: Sequence[ReviewCollectionEntryDraft],
    ) -> ReviewCollectionRevision:
        return self._ledger.save_review_collection_revision(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=collection_id,
            primary_source_kind="run",
            primary_source_id=run_id,
            expected_revision=expected_revision,
            title=title,
            retention_policy="decision",
            entries=tuple(entries),
        )

    def attach_operator_job(
        self,
        *,
        operation_id: str,
        run_id: str,
        collection_id: str,
        expected_revision: int,
        title: str,
        entries: Sequence[ReviewCollectionEntryDraft],
        selection_digest: str,
        operator_job_id: str,
    ) -> ReviewCollectionRevision:
        """Save one terminal Debug/Preview result in the next decision revision."""

        canonical_run_id = RunViewRef(run_id=run_id).run_id
        current = self.read_for_run(run_id=canonical_run_id)
        if current is None or current.collection_id != collection_id:
            raise RealmNotFound("Review Collection was not found.")
        if current.revision != expected_revision:
            raise RealmConflict(
                "Review Collection changed; reload before saving your edits."
            )
        review_item = next(
            (
                item
                for item in current.items
                if item.selection.selection_digest == selection_digest
            ),
            None,
        )
        if review_item is None:
            raise RealmConflict(
                "The inspected candidate is not in the current Review shortlist."
            )
        outcome = self._operator_job_inspection_outcome(
            run_id=canonical_run_id,
            review_selection=review_item.selection,
            operator_job_id=operator_job_id,
        )
        draft = _rehydrate_attached_inspection_outcomes(
            current,
            entries=entries,
        )
        if not any(item.selection_digest == selection_digest for item in draft):
            raise RealmConflict(
                "Keep the inspected candidate in the draft before attaching evidence."
            )
        updated: list[ReviewCollectionEntryDraft] = []
        for item in draft:
            if item.selection_digest != selection_digest:
                updated.append(item)
                continue
            already_attached = any(
                value.get("schema") == REVIEW_INSPECTION_OUTCOME_SCHEMA
                and value.get("operator_job_id") == operator_job_id
                for value in item.inspection_outcomes
            )
            updated.append(
                item
                if already_attached
                else ReviewCollectionEntryDraft(
                    selection_digest=item.selection_digest,
                    note=item.note,
                    inspection_outcomes=(*item.inspection_outcomes, outcome),
                )
            )
        updated_entries = tuple(updated)
        if _draft_matches_revision(current, title=title, entries=updated_entries):
            return current
        return self._commit_revision(
            operation_id=operation_id,
            run_id=canonical_run_id,
            collection_id=collection_id,
            expected_revision=expected_revision,
            title=title,
            entries=updated_entries,
        )

    def export_revision(
        self, *, run_id: str, revision: int | None = None
    ) -> Mapping[str, Any] | None:
        collection = self.read_for_run(run_id=run_id, revision=revision)
        return None if collection is None else collection.export_dict()

    def _operator_job_inspection_outcome(
        self,
        *,
        run_id: str,
        review_selection: SelectionRef,
        operator_job_id: str,
    ) -> Mapping[str, Any]:
        record = self._ledger.read_operator_job(
            actor_principal_id=self.actor_principal_id,
            job_id=operator_job_id,
        )
        self._validate_operator_job_target(
            record=record,
            run_id=run_id,
            review_selection=review_selection,
        )
        if not record.state.terminal or record.outcome is None:
            raise RealmConflict(
                "Only a terminal Debug Run or Environment Preview can be saved "
                "as Review evidence."
            )
        result = None
        if record.result is not None:
            value = record.result.result
            metrics = dict(sorted(value.metrics.items()))
            selected_metrics = dict(
                list(metrics.items())[:REVIEW_COLLECTION_MAX_FROZEN_JOB_FIELDS]
            )
            outputs = value.declared_outputs[
                :REVIEW_COLLECTION_MAX_FROZEN_JOB_OUTPUTS
            ]
            result = {
                "result_digest": record.result.result_digest,
                "result_kind": value.result_kind,
                "status": value.status,
                "metrics": {
                    "total": len(metrics),
                    "returned": len(selected_metrics),
                    "truncated": len(selected_metrics) < len(metrics),
                    "values": selected_metrics,
                },
                "constraints": _bounded_job_mapping(value.constraint_results),
                "events": _bounded_job_mapping(value.event_summary),
                "details": _bounded_job_mapping(value.details),
                "declared_outputs": {
                    "total": len(value.declared_outputs),
                    "returned": len(outputs),
                    "truncated": len(outputs) < len(value.declared_outputs),
                    "rows": [
                        {
                            "declaration_id": item.declaration_id,
                            "name": item.name,
                            "kind": item.kind,
                            "size_bytes": item.size_bytes,
                            "identity_digest": item.identity_digest,
                            "media_type": item.media_type,
                        }
                        for item in outputs
                    ],
                },
                "logs": [item.to_dict() for item in value.logs],
            }
        target = record.plan.target.selection
        return {
            "schema": REVIEW_INSPECTION_OUTCOME_SCHEMA,
            "kind": "operator_job",
            "operator_job_id": record.job_id,
            "job_kind": record.plan.job_kind,
            "plan_digest": record.plan_digest,
            "observed_job_revision": record.revision,
            "target": {
                "selection_digest": target.selection_digest,
                "run_id": target.source_id,
                "candidate_id": target.entity_id,
                "candidate_ref": target.entity_ref,
                "source_revision": target.source_revision,
                "source_sequence": target.source_sequence,
            },
            "execution_policy": {
                "network_policy": record.plan.network_policy,
                "network_enforcement": record.plan.network_enforcement,
                "runtime_fingerprint": record.plan.runtime_fingerprint,
                "entrypoint_profile": record.plan.entrypoint_profile,
            },
            "outcome": record.outcome.outcome.to_dict(),
            "result": result,
            "completed_at": record.outcome.created_at,
        }

    @staticmethod
    def _validate_operator_job_target(
        *,
        record: OperatorJobRecord,
        run_id: str,
        review_selection: SelectionRef,
    ) -> None:
        target = record.plan.target.selection
        if record.plan.job_kind not in _REVIEW_OPERATOR_JOB_KINDS:
            raise RealmConflict(
                "Only Debug Run and Environment Preview jobs are Review inspections."
            )
        if (
            target.source_kind != "run"
            or target.source_id != run_id
            or target.kind != "candidate"
            or target.source_owner_id != review_selection.source_owner_id
            or target.entity_id != review_selection.entity_id
            or target.entity_ref != review_selection.entity_ref
            or target.context_digest != review_selection.context_digest
        ):
            raise RealmConflict(
                "Operator Job targets a different run or candidate."
            )

    @staticmethod
    def _candidate_evidence(
        *,
        snapshot: Any,
        selection: SelectionRef,
        candidate_key: str,
    ) -> Mapping[str, Any]:
        candidate = next(
            item for item in snapshot.candidates if item.candidate_key == candidate_key
        )
        trial_ids = {
            item.admission.logical_trial_id
            for item in snapshot.logical_trials
            if item.candidate_key == candidate_key
        }
        attempt_ids = {
            item.attempt_id
            for item in snapshot.attempts
            if item.logical_trial_id in trial_ids
        }
        observations = [
            item for item in snapshot.observations if item.attempt_id in attempt_ids
        ]
        artifacts = [
            item for item in snapshot.artifacts if item.attempt_id in attempt_ids
        ]
        selected_observations = observations[
            :REVIEW_COLLECTION_MAX_FROZEN_OBSERVATIONS
        ]
        selected_artifacts = artifacts[:REVIEW_COLLECTION_MAX_FROZEN_ARTIFACTS]
        result = CandidateResultIndex.from_snapshot(snapshot).for_candidate_key(
            candidate_key
        )
        comparability = RunComparabilityProjection.from_snapshot(snapshot)
        seal = snapshot.terminal_seal
        return {
            "schema": REVIEW_COLLECTION_ITEM_EVIDENCE_SCHEMA,
            "selection_digest": selection.selection_digest,
            # This is the timestamp of the exact Run head represented by this
            # snapshot.  Unlike wall-clock time sampled in this service it is
            # stable across retries, so the evidence payload and operation
            # request remain deterministic.
            "captured_at": snapshot.revision.created_at,
            "source_anchor": {
                "run_id": snapshot.run.run_id,
                "revision": snapshot.revision.revision,
                "sequence": snapshot.revision.last_sequence,
                "terminal_seal_digest": (
                    None if seal is None else seal.digest
                ),
            },
            "candidate": candidate.to_dict(),
            "candidate_result": thaw_json(result),
            "observations": {
                "total": len(observations),
                "returned": len(selected_observations),
                "truncated": len(selected_observations) < len(observations),
                "rows": [
                    {
                        "observation_id": item.observation_id,
                        "attempt_id": item.attempt_id,
                        "outcome": item.status,
                        "envelope_digest": item.envelope_digest,
                        "metrics": _bounded_observation_metrics(
                            item.envelope.metric_values
                        ),
                        "constraints": _bounded_observation_constraints(
                            item.envelope.constraint_results
                        ),
                        "output_declarations": [
                            declaration.to_dict()
                            for declaration in item.envelope.output_declarations
                        ],
                        "adopted_sequence": item.adopted_sequence,
                        "created_at": item.created_at,
                    }
                    for item in selected_observations
                ],
            },
            "artifacts": {
                "total": len(artifacts),
                "returned": len(selected_artifacts),
                "truncated": len(selected_artifacts) < len(artifacts),
                "rows": [
                    {
                        "artifact_id": item.artifact_id,
                        "attempt_id": item.attempt_id,
                        "observation_id": item.observation_id,
                        "declaration": item.declaration.to_dict(),
                        "content_ref": str(item.content_ref),
                        "size_bytes": item.size_bytes,
                        "visibility": item.visibility,
                        "adopted_sequence": item.adopted_sequence,
                    }
                    for item in selected_artifacts
                ],
            },
            "comparability": comparability.to_dict(),
            "lineage": thaw_json(candidate.admission.lineage),
        }


__all__ = [
    "REVIEW_COLLECTION_MAX_FROZEN_ARTIFACTS",
    "REVIEW_COLLECTION_MAX_FROZEN_JOB_FIELDS",
    "REVIEW_COLLECTION_MAX_FROZEN_JOB_OUTPUTS",
    "REVIEW_COLLECTION_MAX_FROZEN_OBSERVATIONS",
    "RealmReviewCollectionService",
]
