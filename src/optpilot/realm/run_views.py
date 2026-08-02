"""Path-free borrowed Views over live canonical Realm runs.

A :class:`RunViewRef` is a live query identity, never content identity.  Opening
or refreshing it performs one authorized metadata read and returns a
non-authorizing descriptor for the current canonical head.  It creates no
workspace, lease, projection, checkout, or content copy.

Workbench selections are presentation values only.  The actor-bound service
validates their exact coordinates, fences them against the current head, and
asks :class:`RealmLedger` to mint a real immutable :class:`SelectionRef` for
the candidate/artifact kinds that the authority currently supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ._validation import nonnegative_int, required_text
from .errors import RealmConflict, RealmIntegrityError
from .ledger import (
    RUN_SELECTION_CAPABILITY_BATCH_MAX_ITEMS,
    PrincipalRecord,
    RealmLedger,
    RunSelectionCapabilityFacts,
)
from .refs import request_digest
from .run_candidate_comparison import (
    RunCandidateComparisonProjection,
    with_candidate_file_text_diff,
)
from .run_comparability import RunComparabilityProjection
from .run_overview import RunOverviewProjection
from .run_projection import RunSummaryProjection
from .run_timeline import (
    RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    RunTimelinePage,
)
from .run_records import RUN_STATES, RunCandidateSelection
from .run_snapshot import RunLedgerSnapshot
from .run_workbench import (
    RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    RUN_WORKBENCH_KINDS,
    RunWorkbenchReadModel,
    run_workbench_action_capabilities,
    validate_run_workbench_selection,
)
from .selections import SelectionEligibility, SelectionRef


RUN_VIEW_REF_SCHEMA = "optpilot.run-view-ref.v1"
BORROWED_RUN_VIEW_SCHEMA = "optpilot.borrowed-run-view.v1"
RUN_VIEW_SELECTION_RESULT_SCHEMA = "optpilot.run-view-selection-result.v1"
RUN_WORKBENCH_HEAD_SCHEMA = "optpilot.run-workbench-head.v2"
RUN_VIEW_MINTABLE_SELECTION_KINDS = frozenset({"candidate", "artifact"})


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


@dataclass(frozen=True)
class RunViewRef:
    """One path-free live reference to a canonical run identity."""

    run_id: str

    def __post_init__(self) -> None:
        required_text(self.run_id, "run View run id", max_bytes=512)

    def to_dict(self) -> dict[str, str]:
        return {"schema": RUN_VIEW_REF_SCHEMA, "run_id": self.run_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunViewRef":
        try:
            _exact_keys(payload, {"schema", "run_id"}, "run View ref")
            if payload["schema"] != RUN_VIEW_REF_SCHEMA:
                raise ValueError("run View ref schema is unsupported.")
            result = cls(run_id=payload["run_id"])
            if result.to_dict() != dict(payload):
                raise ValueError("run View ref is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Run View ref is invalid: {error}") from error


@dataclass(frozen=True)
class BorrowedRunView:
    """Non-durable, non-authorizing descriptor for one live run head."""

    ref: RunViewRef
    revision: int
    sequence: int
    status: str
    retention_state: str
    workbench: SelectionEligibility
    mode: str = "read_only"
    durable: bool = False
    authorizing: bool = False

    @classmethod
    def from_summary(
        cls, ref: RunViewRef, summary: RunSummaryProjection
    ) -> "BorrowedRunView":
        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        if not isinstance(summary, RunSummaryProjection):
            raise TypeError("summary must be a RunSummaryProjection.")
        if ref.run_id != summary.run_id:
            raise ValueError("run View ref and summary identify different runs.")
        return cls(
            ref=ref,
            revision=summary.cursor.revision,
            sequence=summary.cursor.sequence,
            status=summary.run_status,
            retention_state=summary.retention_state,
            workbench=SelectionEligibility.ready(),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        nonnegative_int(self.revision, "run View revision")
        nonnegative_int(self.sequence, "run View sequence")
        if self.status not in RUN_STATES:
            raise ValueError("run View status is unsupported.")
        if self.retention_state not in {"active", "retired"}:
            raise ValueError("run View retention_state is unsupported.")
        if not isinstance(self.workbench, SelectionEligibility):
            raise TypeError("workbench must be a SelectionEligibility.")
        if not self.workbench.supported or not self.workbench.eligible:
            raise ValueError("borrowed run Views require the built-in Workbench.")
        if self.mode != "read_only" or self.durable or self.authorizing:
            raise ValueError("borrowed run View policy is immutable.")

    @property
    def head(self) -> dict[str, int]:
        return {"revision": self.revision, "sequence": self.sequence}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BORROWED_RUN_VIEW_SCHEMA,
            "ref": self.ref.to_dict(),
            "head": self.head,
            "status": self.status,
            "retention_state": self.retention_state,
            "mode": self.mode,
            "durable": self.durable,
            "authorizing": self.authorizing,
            "capabilities": {"run_workbench": self.workbench.to_dict()},
        }


@dataclass(frozen=True)
class RunWorkbenchHead:
    """One transactionally selected, path-free Workbench head response.

    ``view``, ``summary``, ``overview``, and ``comparability`` are derived from
    the same immutable :class:`RunLedgerSnapshot`; this response never joins
    independently refreshed reads. Timeline pages consume this exact head
    through the actor-authorized RealmLedger event query.
    """

    view: BorrowedRunView
    summary: RunSummaryProjection
    comparability: RunComparabilityProjection
    overview: RunOverviewProjection

    def __post_init__(self) -> None:
        if not isinstance(self.view, BorrowedRunView):
            raise TypeError("view must be a BorrowedRunView.")
        if not isinstance(self.summary, RunSummaryProjection):
            raise TypeError("summary must be a RunSummaryProjection.")
        if not isinstance(self.comparability, RunComparabilityProjection):
            raise TypeError("comparability must be a RunComparabilityProjection.")
        if not isinstance(self.overview, RunOverviewProjection):
            raise TypeError("overview must be a RunOverviewProjection.")
        if (
            self.view.ref.run_id != self.summary.run_id
            or self.view.revision != self.summary.cursor.revision
            or self.view.sequence != self.summary.cursor.sequence
            or self.view.status != self.summary.run_status
            or self.view.retention_state != self.summary.retention_state
        ):
            raise ValueError("Workbench view and summary differ at their run head.")
        if (
            self.comparability.run_id != self.summary.run_id
            or self.comparability.revision != self.summary.cursor.revision
            or self.comparability.sequence != self.summary.cursor.sequence
        ):
            raise ValueError("Workbench comparability facts differ at their run head.")
        if (
            self.overview.run_id != self.summary.run_id
            or self.overview.revision != self.summary.cursor.revision
            or self.overview.sequence != self.summary.cursor.sequence
        ):
            raise ValueError("Workbench Overview differs at its run head.")

    @property
    def head(self) -> dict[str, int]:
        return self.summary.cursor.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_WORKBENCH_HEAD_SCHEMA,
            "view": self.view.to_dict(),
            "summary": self.summary.to_dict(),
            "comparability": self.comparability.to_dict(),
            "overview": self.overview.to_dict(),
            "head": self.head,
            "mode": "read_only",
            "capabilities": {
                "entity_pages": {
                    "supported": True,
                    "eligible": True,
                    "reason": None,
                },
                "timeline": {
                    "supported": True,
                    "eligible": True,
                    "reason": None,
                },
                "actions": run_workbench_action_capabilities(),
            },
        }


@dataclass(frozen=True)
class RunWorkbenchBundle:
    """One exact Workbench head and its bounded first entity pages.

    The service derives the head and every page from one authorized
    :class:`RunLedgerSnapshot`. The bundle is an internal composition value;
    callers still fetch later pages through the exact-head page-token API.
    """

    head: RunWorkbenchHead
    pages: Mapping[str, Mapping[str, Any]]
    snapshot: RunLedgerSnapshot = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.head, RunWorkbenchHead):
            raise TypeError("head must be a RunWorkbenchHead.")
        if not isinstance(self.snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        if (
            self.snapshot.run.run_id != self.head.summary.run_id
            or self.snapshot.revision.revision != self.head.summary.cursor.revision
            or self.snapshot.revision.last_sequence
            != self.head.summary.cursor.sequence
        ):
            raise ValueError("Workbench bundle snapshot differs from its head.")
        if not isinstance(self.pages, Mapping) or set(self.pages) != set(
            RUN_WORKBENCH_KINDS
        ):
            raise ValueError("Workbench bundle pages must contain every kind.")
        selected_head = self.head.head
        normalized: dict[str, Mapping[str, Any]] = {}
        for kind in RUN_WORKBENCH_KINDS:
            page = self.pages[kind]
            if not isinstance(page, Mapping):
                raise TypeError("Workbench bundle pages must be mappings.")
            query = page.get("query")
            if (
                page.get("run_id") != self.head.summary.run_id
                or page.get("head") != selected_head
                or not isinstance(query, Mapping)
                or query.get("kind") != kind
            ):
                raise ValueError(
                    "Workbench bundle page differs from its exact run head."
                )
            normalized[kind] = page
        object.__setattr__(self, "pages", MappingProxyType(normalized))

    def entity_row(self, *, kind: str, entity_id: str) -> dict[str, Any] | None:
        """Resolve one stable identity from this bundle's already-selected head."""

        return RunWorkbenchReadModel.from_snapshot(
            self.snapshot,
            summary=self.head.summary,
        ).entity_row(kind, entity_id)


@dataclass(frozen=True)
class RunViewSelectionResult:
    """Typed result of fencing one Workbench presentation selection."""

    eligibility: SelectionEligibility
    selection: SelectionRef | None

    def __post_init__(self) -> None:
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.selection is not None):
            raise ValueError("selection result differs from its eligibility.")
        if self.selection is not None and not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef or None.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_VIEW_SELECTION_RESULT_SCHEMA,
            "eligibility": self.eligibility.to_dict(),
            "selection": None if self.selection is None else self.selection.to_dict(),
        }


def _selection_eligibility(kind: str) -> SelectionEligibility:
    if kind in RUN_VIEW_MINTABLE_SELECTION_KINDS:
        return SelectionEligibility.ready()
    return SelectionEligibility.unsupported(
        "workbench_selection_kind_not_mintable",
        "Only candidate and artifact Workbench selections can currently be "
        "resolved as immutable SelectionRefs.",
    )


@dataclass(frozen=True)
class RealmRunViewService:
    """Borrowed-run operations bound once to a trusted server principal."""

    _ledger: RealmLedger
    _principal: PrincipalRecord
    _selection_content: Any | None = None

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")
        if self._selection_content is not None and not callable(
            getattr(self._selection_content, "read_range", None)
        ):
            raise TypeError("selection_content must provide read_range().")

    def open(self, *, run_id: str) -> BorrowedRunView:
        return self.refresh(ref=RunViewRef(run_id=run_id))

    def workbench_head(self, *, ref: RunViewRef) -> RunWorkbenchHead:
        """Read the borrowed descriptor and summary from one ledger snapshot."""

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        summary = RunSummaryProjection.from_snapshot(snapshot)
        comparability = RunComparabilityProjection.from_snapshot(snapshot)
        return RunWorkbenchHead(
            view=BorrowedRunView.from_summary(ref, summary),
            summary=summary,
            comparability=comparability,
            overview=RunOverviewProjection.from_snapshot(
                snapshot,
                summary=summary,
                comparability=comparability,
            ),
        )

    def workbench_bundle(
        self,
        *,
        ref: RunViewRef,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> RunWorkbenchBundle:
        """Derive one head and all bounded first pages from one snapshot."""

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        summary = RunSummaryProjection.from_snapshot(snapshot)
        model = RunWorkbenchReadModel.from_snapshot(snapshot, summary=summary)
        comparability = RunComparabilityProjection.from_snapshot(snapshot)
        head = RunWorkbenchHead(
            view=BorrowedRunView.from_summary(ref, summary),
            summary=summary,
            comparability=comparability,
            overview=RunOverviewProjection.from_snapshot(
                snapshot,
                summary=summary,
                comparability=comparability,
            ),
        )
        return RunWorkbenchBundle(
            head=head,
            pages={
                kind: model.page(kind, limit=limit)
                for kind in RUN_WORKBENCH_KINDS
            },
            snapshot=snapshot,
        )

    def workbench_capability_batch(
        self,
        *,
        ref: RunViewRef,
        presentation_selections: Sequence[Mapping[str, Any]],
        bundle: RunWorkbenchBundle | None = None,
    ) -> Mapping[str, RunSelectionCapabilityFacts]:
        """Resolve a bounded set of exact-head selection facts once.

        Supplying the internal first-page bundle reuses its authorized
        snapshot. Without a bundle the method performs one authorized snapshot
        read, which is useful for independently requested Workbench pages.
        """

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        values = tuple(presentation_selections)
        if len(values) > RUN_SELECTION_CAPABILITY_BATCH_MAX_ITEMS:
            raise ValueError(
                "Workbench capability batch exceeds its bounded item limit."
            )
        if bundle is None:
            snapshot = self._ledger.read_run_snapshot(
                actor_principal_id=self._principal.principal_id,
                run_id=ref.run_id,
            )
        else:
            if not isinstance(bundle, RunWorkbenchBundle):
                raise TypeError("bundle must be a RunWorkbenchBundle or None.")
            if bundle.head.view.ref != ref:
                raise ValueError("Workbench bundle belongs to a different run View.")
            snapshot = bundle.snapshot

        summary = RunSummaryProjection.from_snapshot(snapshot)
        model = RunWorkbenchReadModel.from_snapshot(snapshot, summary=summary)
        presented_values: list[dict[str, Any]] = []
        selections: list[SelectionRef] = []
        seen: set[str] = set()
        candidates = {item.candidate_id: item for item in snapshot.candidates}
        artifacts = {item.artifact_id: item for item in snapshot.artifacts}
        template_digest = snapshot.definition.evaluation_closure.evaluation_template.digest
        for raw in values:
            presented = validate_run_workbench_selection(raw)
            if presented["selection_id"] in seen:
                raise ValueError("Workbench capability batch contains duplicates.")
            seen.add(presented["selection_id"])
            if presented["run_id"] != ref.run_id:
                raise ValueError("Workbench selection belongs to a different run View.")
            if (
                presented["revision"] != snapshot.revision.revision
                or presented["sequence"] != snapshot.revision.last_sequence
            ):
                raise RealmConflict("Run presentation head changed.")
            if presented["kind"] not in RUN_VIEW_MINTABLE_SELECTION_KINDS:
                raise ValueError(
                    "Workbench capability batches accept candidates and artifacts."
                )
            if not model.contains_selection(presented):
                raise ValueError(
                    "Workbench selection does not identify an item at this run head."
                )
            if presented["kind"] == "candidate":
                candidate = candidates[presented["entity_id"]]
                selection = SelectionRef.from_run_candidate(
                    RunCandidateSelection.build(
                        run_id=ref.run_id,
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
            else:
                artifact = artifacts[presented["entity_id"]]
                selection = SelectionRef.build(
                    kind="artifact",
                    source_kind="run",
                    source_id=ref.run_id,
                    source_owner_id=snapshot.run.owner_id,
                    source_revision=snapshot.revision.revision,
                    owner_revision=snapshot.revision.owner_revision,
                    source_sequence=snapshot.revision.last_sequence,
                    entity_sequence=artifact.adopted_sequence,
                    entity_id=artifact.artifact_id,
                    entity_ref=str(artifact.content_ref),
                    context_digest=request_digest(artifact.declaration.to_dict()),
                )
            presented_values.append(presented)
            selections.append(selection)

        facts = self._ledger._resolve_run_selection_capability_batch(
            actor_principal_id=self._principal.principal_id,
            snapshot=snapshot,
            selections=selections,
        )
        return MappingProxyType(
            {
                presented["selection_id"]: item
                for presented, item in zip(presented_values, facts, strict=True)
            }
        )

    def refresh(self, *, ref: RunViewRef) -> BorrowedRunView:
        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        return BorrowedRunView.from_summary(
            ref, RunSummaryProjection.from_snapshot(snapshot)
        )

    def workbench_page(
        self,
        *,
        ref: RunViewRef,
        kind: str,
        page_token: str | None = None,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        summary = RunSummaryProjection.from_snapshot(snapshot)
        return RunWorkbenchReadModel.from_snapshot(
            snapshot, summary=summary
        ).page(kind, page_token=page_token, limit=limit)

    def timeline_page(
        self,
        *,
        ref: RunViewRef,
        expected_run_revision: int,
        expected_head_sequence: int,
        after_sequence: int = 0,
        limit: int = RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    ) -> RunTimelinePage:
        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        return self._ledger.read_run_timeline_page(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
            expected_run_revision=expected_run_revision,
            expected_head_sequence=expected_head_sequence,
            after_sequence=after_sequence,
            limit=limit,
        )

    def resolve_workbench_selection(
        self,
        *,
        ref: RunViewRef,
        presentation_selection: Mapping[str, Any],
    ) -> tuple[RunSummaryProjection, dict[str, Any]]:
        """Resolve exact presentation coordinates to one bounded Realm row.

        The actor-bound snapshot read happens before the untrusted coordinates
        are interpreted.  This method deliberately returns presentation data,
        not a :class:`SelectionRef`, so every Workbench entity kind can be
        carried into read-only UI context without manufacturing authority.
        """

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        presented = validate_run_workbench_selection(presentation_selection)
        if presented["run_id"] != ref.run_id:
            raise ValueError("Workbench selection belongs to a different run View.")
        if (
            presented["revision"] != snapshot.revision.revision
            or presented["sequence"] != snapshot.revision.last_sequence
        ):
            raise RealmConflict("Run presentation head changed.")
        summary = RunSummaryProjection.from_snapshot(snapshot)
        row = RunWorkbenchReadModel.from_snapshot(
            snapshot, summary=summary
        ).selection_row(presented)
        if row is None:
            raise ValueError(
                "Workbench selection does not identify an item at this run head."
            )
        return summary, row

    def compare_candidates(
        self,
        *,
        ref: RunViewRef,
        baseline_presentation_selection: Mapping[str, Any],
        comparison_presentation_selection: Mapping[str, Any],
        text_diff_path: str | None = None,
    ) -> RunCandidateComparisonProjection:
        """Compare two exact-head candidates from one authorized snapshot.

        The snapshot read intentionally precedes interpretation of either
        presentation selection. The default operation needs metadata-read
        authority only. An explicit file diff remints both selections and reads
        one bounded relative path. Neither path creates a derived owner,
        materialized projection, runtime, or other durable state.
        """

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        projection = RunCandidateComparisonProjection.from_snapshot(
            snapshot,
            baseline_presentation_selection=baseline_presentation_selection,
            comparison_presentation_selection=comparison_presentation_selection,
        )
        if text_diff_path is None:
            return projection
        if projection.mode != "files":
            raise ValueError("Candidate text diff requires file candidates.")
        selections = []
        for presented in (
            baseline_presentation_selection,
            comparison_presentation_selection,
        ):
            minted = self.mint_selection(
                ref=ref,
                presentation_selection=presented,
            )
            if minted.selection is None or minted.selection.kind != "candidate":
                raise RealmConflict(
                    "Candidate text diff requires retained candidate content."
                )
            selections.append(minted.selection)
        return with_candidate_file_text_diff(
            projection,
            selection_content=self._selection_content,
            baseline_selection=selections[0],
            comparison_selection=selections[1],
            relative_path=text_diff_path,
        )

    def mint_selection(
        self,
        *,
        ref: RunViewRef,
        presentation_selection: Mapping[str, Any],
    ) -> RunViewSelectionResult:
        """Fence presentation coordinates and mint authority only on exact match."""

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")
        # Authorize the borrowed View first.  Unknown and unauthorized run ids
        # therefore remain indistinguishable even when the selection is bad.
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
        )
        presented = validate_run_workbench_selection(presentation_selection)
        if presented["run_id"] != ref.run_id:
            raise ValueError("Workbench selection belongs to a different run View.")
        if (
            presented["revision"] != snapshot.revision.revision
            or presented["sequence"] != snapshot.revision.last_sequence
        ):
            raise RealmConflict("Run presentation head changed.")
        kind = presented["kind"]
        if kind not in RUN_WORKBENCH_KINDS:  # Defensive; validator already checks.
            raise ValueError("Workbench selection kind is unsupported.")
        summary = RunSummaryProjection.from_snapshot(snapshot)
        model = RunWorkbenchReadModel.from_snapshot(snapshot, summary=summary)
        if not model.contains_selection(presented):
            raise ValueError(
                "Workbench selection does not identify an item at this run head."
            )
        eligibility = _selection_eligibility(kind)
        if not eligibility.eligible:
            return RunViewSelectionResult(eligibility, None)
        selection = self._ledger.mint_run_selection(
            actor_principal_id=self._principal.principal_id,
            run_id=ref.run_id,
            kind=kind,
            entity_id=presented["entity_id"],
            expected_run_revision=presented["revision"],
            expected_head_sequence=presented["sequence"],
        )
        return RunViewSelectionResult(SelectionEligibility.ready(), selection)

    @staticmethod
    def detach(*, ref: RunViewRef) -> None:
        """Drop a borrowed descriptor; no Realm object exists to mutate."""

        if not isinstance(ref, RunViewRef):
            raise TypeError("ref must be a RunViewRef.")


__all__ = [
    "BORROWED_RUN_VIEW_SCHEMA",
    "RUN_VIEW_MINTABLE_SELECTION_KINDS",
    "RUN_VIEW_REF_SCHEMA",
    "RUN_VIEW_SELECTION_RESULT_SCHEMA",
    "RUN_WORKBENCH_HEAD_SCHEMA",
    "BorrowedRunView",
    "RealmRunViewService",
    "RunViewRef",
    "RunViewSelectionResult",
    "RunCandidateComparisonProjection",
    "RunComparabilityProjection",
    "RunWorkbenchBundle",
    "RunWorkbenchHead",
]
