"""Actor-bound creation service for sealed-parent exact-plan child runs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from ..run_execution_profile import RunExecutionProfile
from ._validation import required_text
from .errors import RealmNotFound
from .ledger import (
    RUN_SELECTION_CAPABILITY_BATCH_MAX_ITEMS,
    PrincipalRecord,
    RealmLedger,
)
from .run_child import (
    ChildRunCandidateAnchor,
    ExactPlanChildRunCommitReceipt,
    ExactPlanChildRunReceipt,
    ExactPlanChildRunRequest,
    build_exact_plan_child_run_request,
    exact_plan_child_run_identities,
)
from .run_records import RunCandidateSelection
from .run_snapshot import RunLedgerSnapshot
from .selections import SelectionEligibility, SelectionRef


EVALUATION_PLAN_UNAVAILABLE_CODE = "evaluation_plan_unavailable"


def _execution_profile(
    value: RunExecutionProfile | None,
) -> RunExecutionProfile:
    if value is None:
        return RunExecutionProfile()
    if not isinstance(value, RunExecutionProfile):
        raise TypeError("execution_profile must be a RunExecutionProfile or None.")
    return value


@dataclass(frozen=True)
class ExactPlanChildRunSelectionPreparation:
    """Advisory singleton preset outcome for one exact-head selection."""

    selection: SelectionRef
    eligibility: SelectionEligibility
    prepared: ExactPlanChildRunReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.prepared is not None):
            raise ValueError(
                "exact-plan child preparation differs from its eligibility."
            )
        if self.prepared is not None and not isinstance(
            self.prepared, ExactPlanChildRunReceipt
        ):
            raise TypeError("prepared must be an ExactPlanChildRunReceipt or None.")


def _candidate_anchors_from_snapshot(
    *,
    snapshot: RunLedgerSnapshot,
    selections: Sequence[SelectionRef],
    allow_empty: bool = False,
) -> tuple[ChildRunCandidateAnchor, ...]:
    """Resolve exact run-candidate selections without reading Realm again."""

    if not isinstance(snapshot, RunLedgerSnapshot):
        raise TypeError("snapshot must be a RunLedgerSnapshot.")
    values = tuple(selections)
    if not values and not allow_empty:
        raise ValueError("At least one parent candidate must be selected.")
    if any(not isinstance(item, SelectionRef) for item in values):
        raise TypeError("selections must contain SelectionRef values.")
    if len({item.selection_digest for item in values}) != len(values):
        raise ValueError("Exact-plan child selection set contains duplicates.")
    if any(item.source_kind != "run" or item.kind != "candidate" for item in values):
        raise ValueError("Exact-plan child runs accept only run candidate selections.")

    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in snapshot.candidates
    }
    template_digest = snapshot.definition.evaluation_closure.evaluation_template.digest
    anchors: list[ChildRunCandidateAnchor] = []
    for selection in values:
        candidate = candidate_by_id.get(selection.entity_id)
        if candidate is None:
            raise RealmNotFound("Entity not found.")
        expected = SelectionRef.from_run_candidate(
            RunCandidateSelection.build(
                run_id=snapshot.run.run_id,
                evaluation_template_digest=template_digest,
                run_revision=snapshot.revision.revision,
                owner_revision=snapshot.revision.owner_revision,
                sequence=candidate.accepted_sequence,
                candidate_id=candidate.candidate_id,
                candidate_ref=candidate.candidate_ref,
            ),
            source_owner_id=snapshot.run.owner_id,
            source_sequence=snapshot.revision.last_sequence,
        )
        if selection != expected:
            # Fail closed instead of revealing which immutable coordinate was
            # stale, forged, or belongs to another retained run.
            raise RealmNotFound("Entity not found.")
        anchors.append(ChildRunCandidateAnchor.from_record(candidate))
    return tuple(anchors)


def _selection_source_run_id(
    selections: Sequence[SelectionRef],
    *,
    allow_empty: bool = False,
) -> str | None:
    """Validate the request envelope before its single authorized read."""

    values = tuple(selections)
    if not values:
        if allow_empty:
            return None
        raise ValueError("At least one parent candidate must be selected.")
    if any(not isinstance(item, SelectionRef) for item in values):
        raise TypeError("selections must contain SelectionRef values.")
    if any(item.source_kind != "run" or item.kind != "candidate" for item in values):
        raise ValueError("Exact-plan child runs accept only run candidate selections.")
    if len({item.selection_digest for item in values}) != len(values):
        raise ValueError("Exact-plan child selection set contains duplicates.")
    source_ids = {item.source_id for item in values}
    if len(source_ids) != 1:
        raise ValueError("Exact-plan child selections must belong to one parent run.")
    return next(iter(source_ids))


def new_exact_plan_child_run_operation_id() -> str:
    """Return an opaque id for one fresh user-requested child run."""

    return f"exact-plan-child-run/{uuid.uuid4().hex}"


def exact_plan_child_run_id_for_operation(operation_id: str) -> str:
    """Return the deterministic child run id without mutating Realm."""

    return exact_plan_child_run_identities(operation_id).run_id


@dataclass(frozen=True)
class RealmChildRunService:
    """Prepare and atomically commit child runs under one bound principal.

    Preparation is read-only and suitable for a trusted confirmation layer.
    Creation reauthorizes the terminal parent, its seal, selected entities,
    exact coordinates, and retained content in RealmLedger's write transaction.
    The caller cannot substitute another actor at either seam.
    """

    _ledger: RealmLedger
    _principal: PrincipalRecord

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    def prepare_exact_plan(
        self,
        *,
        parent_run_id: str,
        selected_candidates: Sequence[ChildRunCandidateAnchor],
        execution_profile: RunExecutionProfile | None = None,
    ) -> ExactPlanChildRunReceipt:
        """Build a bounded request from the authorized terminal parent head."""

        profile = _execution_profile(execution_profile)
        parent_run_id = required_text(parent_run_id, "parent run id", max_bytes=512)
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=parent_run_id,
        )
        if snapshot.terminal_seal is None:
            raise ValueError("Exact-plan child runs require a sealed terminal parent.")
        return build_exact_plan_child_run_request(
            snapshot=snapshot,
            parent=snapshot.terminal_seal.anchor,
            selected_candidates=selected_candidates,
            execution_profile=profile,
        )

    def prepare_exact_plan_selections(
        self,
        *,
        selections: Sequence[SelectionRef],
        execution_profile: RunExecutionProfile | None = None,
    ) -> ExactPlanChildRunReceipt:
        """Prepare one plural preset from exact immutable selections.

        The source run id comes from the immutable selections. The service
        performs one actor-authorized snapshot read, then recomputes every
        selection anchor from that exact head before building the request.
        """

        profile = _execution_profile(execution_profile)
        values = tuple(selections)
        parent_run_id = _selection_source_run_id(values)
        assert parent_run_id is not None
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=parent_run_id,
        )
        return self.prepare_exact_plan_selections_from_snapshot(
            snapshot=snapshot,
            selections=values,
            execution_profile=profile,
        )

    def prepare_exact_plan_selection_batch(
        self,
        *,
        selections: Sequence[SelectionRef],
        execution_profile: RunExecutionProfile | None = None,
    ) -> Mapping[str, ExactPlanChildRunSelectionPreparation]:
        """Prepare singleton presets for a bounded batch with one read.

        This is the actor-bound seam for independently loaded Workbench pages.
        Callers that already hold an authorized ``RunWorkbenchBundle`` should
        use :meth:`prepare_exact_plan_selection_batch_from_snapshot` instead.
        """

        profile = _execution_profile(execution_profile)
        values = tuple(selections)
        if len(values) > RUN_SELECTION_CAPABILITY_BATCH_MAX_ITEMS:
            raise ValueError(
                "Exact-plan child selection batch exceeds its bounded item limit."
            )
        parent_run_id = _selection_source_run_id(values, allow_empty=True)
        if parent_run_id is None:
            return MappingProxyType({})
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=parent_run_id,
        )
        return self.prepare_exact_plan_selection_batch_from_snapshot(
            snapshot=snapshot,
            selections=values,
            execution_profile=profile,
        )

    @staticmethod
    def prepare_exact_plan_selections_from_snapshot(
        *,
        snapshot: RunLedgerSnapshot,
        selections: Sequence[SelectionRef],
        execution_profile: RunExecutionProfile | None = None,
    ) -> ExactPlanChildRunReceipt:
        """Prepare one plural preset from an already-authorized snapshot.

        This internal composition seam performs no Realm read and grants no
        authority. The caller must obtain ``snapshot`` through an actor-bound
        service and must not expose the resulting internal receipt directly to
        an untrusted browser.
        """

        profile = _execution_profile(execution_profile)
        anchors = _candidate_anchors_from_snapshot(
            snapshot=snapshot,
            selections=selections,
        )
        if snapshot.terminal_seal is None:
            raise ValueError("Exact-plan child runs require a sealed terminal parent.")
        return build_exact_plan_child_run_request(
            snapshot=snapshot,
            parent=snapshot.terminal_seal.anchor,
            selected_candidates=anchors,
            execution_profile=profile,
        )

    @staticmethod
    def prepare_exact_plan_selection_batch_from_snapshot(
        *,
        snapshot: RunLedgerSnapshot,
        selections: Sequence[SelectionRef],
        execution_profile: RunExecutionProfile | None = None,
    ) -> Mapping[str, ExactPlanChildRunSelectionPreparation]:
        """Prepare independent singleton outcomes from one authorized snapshot.

        Expected candidate-local ineligibility never hides ready neighbors.
        Exact selection mismatches and malformed snapshots still fail closed.
        """

        profile = _execution_profile(execution_profile)
        values = tuple(selections)
        if len(values) > RUN_SELECTION_CAPABILITY_BATCH_MAX_ITEMS:
            raise ValueError(
                "Exact-plan child selection batch exceeds its bounded item limit."
            )
        anchors = _candidate_anchors_from_snapshot(
            snapshot=snapshot,
            selections=values,
            allow_empty=True,
        )
        if not values:
            return MappingProxyType({})
        candidate_by_id = {
            candidate.candidate_id: candidate for candidate in snapshot.candidates
        }
        candidate_keys_with_trials = {
            trial.candidate_key for trial in snapshot.logical_trials
        }
        results: dict[str, ExactPlanChildRunSelectionPreparation] = {}
        for selection, anchor in zip(values, anchors, strict=True):
            candidate = candidate_by_id[selection.entity_id]
            if snapshot.terminal_seal is None:
                eligibility = SelectionEligibility.unavailable(
                    "terminal_parent_unavailable",
                    "Exact-plan re-evaluation requires a sealed terminal parent run.",
                )
                prepared = None
            elif candidate.admission.envelope.candidate_format not in {
                "parameters",
                "files",
            }:
                eligibility = SelectionEligibility.unsupported(
                    "candidate_format_unsupported",
                    "The selected candidate format cannot seed an exact-plan child run.",
                )
                prepared = None
            elif candidate.candidate_key not in candidate_keys_with_trials:
                eligibility = SelectionEligibility.unavailable(
                    EVALUATION_PLAN_UNAVAILABLE_CODE,
                    "The selected candidate has no parent evaluation coordinates to re-evaluate.",
                )
                prepared = None
            else:
                eligibility = SelectionEligibility.ready()
                prepared = build_exact_plan_child_run_request(
                    snapshot=snapshot,
                    parent=snapshot.terminal_seal.anchor,
                    selected_candidates=(anchor,),
                    execution_profile=profile,
                )
            results[selection.selection_digest] = ExactPlanChildRunSelectionPreparation(
                selection=selection,
                eligibility=eligibility,
                prepared=prepared,
            )
        return MappingProxyType(results)

    def create_exact_plan(
        self,
        *,
        operation_id: str,
        request: ExactPlanChildRunRequest,
    ) -> ExactPlanChildRunCommitReceipt:
        """Create and seed one methodless draining child in one SQLite commit."""

        required_text(operation_id, "child-run operation id", max_bytes=512)
        if not isinstance(request, ExactPlanChildRunRequest):
            raise TypeError("request must be an ExactPlanChildRunRequest.")
        identities = exact_plan_child_run_identities(operation_id)
        return self._ledger.create_exact_plan_child_run(
            operation_id=operation_id,
            actor_principal_id=self.principal_id,
            controller_holder_id=identities.controller_holder_id,
            request=request,
        )

    def create_prepared_exact_plan(
        self,
        *,
        operation_id: str,
        prepared: ExactPlanChildRunReceipt,
    ) -> ExactPlanChildRunCommitReceipt:
        """Commit the exact request previously produced for confirmation."""

        if not isinstance(prepared, ExactPlanChildRunReceipt):
            raise TypeError("prepared must be an ExactPlanChildRunReceipt.")
        if prepared.request_digest != prepared.request.digest:
            raise ValueError("Prepared exact-plan child request digest changed.")
        return self.create_exact_plan(
            operation_id=operation_id,
            request=prepared.request,
        )

__all__ = [
    "EVALUATION_PLAN_UNAVAILABLE_CODE",
    "ExactPlanChildRunSelectionPreparation",
    "RealmChildRunService",
    "exact_plan_child_run_id_for_operation",
    "new_exact_plan_child_run_operation_id",
]
