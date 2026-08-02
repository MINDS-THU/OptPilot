"""Run-local Shortlist facade over revisioned Review Collections.

The Review Collection aggregate remains the persistence and retention
authority.  This module supplies the narrower product contract used by Studio:

* every mutation carries the complete user draft;
* one current card identifies one Candidate in one Run;
* adding, attaching, and refreshing commit pending title/note/order edits in
  the same optimistic transaction; and
* refreshing a saved result creates a new immutable selection/evidence item
  while historical revisions continue to reference the old snapshot.

There is intentionally no second Shortlist ledger or content owner here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ._validation import (
    finite_time,
    freeze_json,
    lower_hex_digest,
    positive_int,
    required_text,
)
from .errors import RealmConflict, RealmIntegrityError, RealmNotFound
from .review_collection_service import (
    RealmReviewCollectionService,
    _default_collection_id,
    _rehydrate_attached_inspection_outcomes,
)
from .review_collections import (
    REVIEW_INSPECTION_OUTCOME_SCHEMA,
    ReviewCollectionEntryDraft,
    ReviewCollectionNewItem,
    ReviewCollectionRevision,
    ReviewCollectionRevisionItem,
    public_review_evidence,
    public_review_inspection_outcome,
    public_review_selection,
)
from .run_workbench import validate_run_workbench_selection
from .run_views import RunViewRef
from .selections import SelectionRef


RUN_SHORTLIST_SCHEMA = "optpilot.run-shortlist.v1"
RUN_SHORTLIST_CARD_SCHEMA = "optpilot.run-shortlist-card.v1"
RUN_SHORTLIST_COMMAND_SCHEMA = "optpilot.run-shortlist-command.v1"
DEFAULT_SHORTLIST_TITLE = "Shortlist"


def _canonical_title(value: str) -> str:
    value = required_text(value, "shortlist title", max_bytes=512)
    if value.strip() != value:
        raise ValueError("shortlist title must not have surrounding whitespace.")
    return value


def _command_draft(draft: "ShortlistDraft") -> dict[str, Any]:
    if not isinstance(draft, ShortlistDraft):
        raise TypeError("draft must be a ShortlistDraft.")
    return {
        "shortlist_id": draft.shortlist_id,
        "expected_revision": draft.expected_revision,
        "title": draft.title,
        "cards": [card.to_review().to_dict() for card in draft.cards],
    }


def _canonical_note(value: str) -> str:
    return ReviewCollectionEntryDraft("0" * 64, note=value).note


@dataclass(frozen=True)
class ShortlistDraft:
    """The complete editable state on which one mutation is based.

    ``shortlist_id`` and ``expected_revision`` are both null only before the
    first card is saved.  The facade translates cards to the existing Review
    entry record, keeping one canonical persistence and validation path.
    """

    shortlist_id: str | None
    expected_revision: int | None
    title: str = DEFAULT_SHORTLIST_TITLE
    cards: tuple["ShortlistCardDraft", ...] = ()

    def __post_init__(self) -> None:
        if (self.shortlist_id is None) != (self.expected_revision is None):
            raise ValueError(
                "shortlist id and expected revision must both be present or absent."
            )
        if self.shortlist_id is not None:
            required_text(self.shortlist_id, "shortlist id", max_bytes=512)
            positive_int(self.expected_revision, "expected shortlist revision")
        cards = tuple(self.cards)
        if any(not isinstance(card, ShortlistCardDraft) for card in cards):
            raise TypeError("shortlist cards must be ShortlistCardDraft values.")
        digests = tuple(card.selection_digest for card in cards)
        if len(digests) != len(set(digests)):
            raise ValueError("shortlist draft contains duplicate saved selections.")
        object.__setattr__(self, "title", _canonical_title(self.title))
        object.__setattr__(self, "cards", cards)

    @classmethod
    def empty(cls, *, title: str = DEFAULT_SHORTLIST_TITLE) -> "ShortlistDraft":
        return cls(shortlist_id=None, expected_revision=None, title=title)

    @classmethod
    def from_revision(
        cls, revision: "ShortlistRevision | ReviewCollectionRevision"
    ) -> "ShortlistDraft":
        if isinstance(revision, ShortlistRevision):
            return cls(
                shortlist_id=revision.shortlist_id,
                expected_revision=revision.revision,
                title=revision.title,
                cards=tuple(card.to_draft() for card in revision.cards),
            )
        if not isinstance(revision, ReviewCollectionRevision):
            raise TypeError("revision must be a Shortlist or Review revision.")
        return cls(
            shortlist_id=revision.collection_id,
            expected_revision=revision.revision,
            title=revision.title,
            cards=tuple(
                ShortlistCardDraft(
                    selection_digest=item.selection.selection_digest,
                    note=item.note,
                    inspection_outcomes=item.inspection_outcomes,
                )
                for item in revision.items
            ),
        )


@dataclass(frozen=True)
class ShortlistCardDraft:
    """Editable fields for one already-saved Candidate card."""

    selection_digest: str
    note: str = ""
    inspection_outcomes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        normalized = ReviewCollectionEntryDraft(
            selection_digest=self.selection_digest,
            note=self.note,
            inspection_outcomes=tuple(self.inspection_outcomes),
        )
        object.__setattr__(self, "selection_digest", normalized.selection_digest)
        object.__setattr__(self, "note", normalized.note)
        object.__setattr__(
            self, "inspection_outcomes", normalized.inspection_outcomes
        )

    def to_review(self) -> ReviewCollectionEntryDraft:
        return ReviewCollectionEntryDraft(
            selection_digest=self.selection_digest,
            note=self.note,
            inspection_outcomes=self.inspection_outcomes,
        )


@dataclass(frozen=True)
class ShortlistCard:
    """One public Candidate card backed by one immutable evidence snapshot."""

    position: int
    candidate_id: str
    selection: SelectionRef
    note: str
    inspection_outcomes: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    evidence_digest: str
    saved_result_at: float
    first_revision: int

    def __post_init__(self) -> None:
        positive_int(self.position, "shortlist card position")
        required_text(self.candidate_id, "shortlist candidate id", max_bytes=512)
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("shortlist selection must be a SelectionRef.")
        if (
            self.selection.source_kind != "run"
            or self.selection.kind != "candidate"
            or self.selection.entity_id != self.candidate_id
        ):
            raise ValueError("shortlist card selection identifies another Candidate.")
        if not isinstance(self.note, str):
            raise TypeError("shortlist note must be a string.")
        outcomes = tuple(self.inspection_outcomes)
        evidence = freeze_json(self.evidence, label="shortlist saved evidence")
        if not isinstance(evidence, Mapping):
            raise TypeError("shortlist saved evidence must be a mapping.")
        lower_hex_digest(self.evidence_digest, "shortlist evidence digest")
        object.__setattr__(self, "inspection_outcomes", outcomes)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(
            self,
            "saved_result_at",
            finite_time(self.saved_result_at, "shortlist saved result time"),
        )
        positive_int(self.first_revision, "shortlist first revision")

    def to_draft(self) -> ShortlistCardDraft:
        return ShortlistCardDraft(
            selection_digest=self.selection.selection_digest,
            note=self.note,
            inspection_outcomes=self.inspection_outcomes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_SHORTLIST_CARD_SCHEMA,
            "position": self.position,
            "candidate_id": self.candidate_id,
            "selection": public_review_selection(self.selection),
            "note": self.note,
            "inspection_outcomes": [
                public_review_inspection_outcome(value)
                for value in self.inspection_outcomes
            ],
            "saved_evidence": public_review_evidence(self.evidence),
            "saved_evidence_digest": self.evidence_digest,
            "saved_result_at": self.saved_result_at,
            "first_revision": self.first_revision,
        }


@dataclass(frozen=True)
class ShortlistRevision:
    """Public projection of one immutable Review Collection revision."""

    shortlist_id: str
    run_id: str
    revision: int
    revision_digest: str
    title: str
    cards: tuple[ShortlistCard, ...]
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.shortlist_id, "shortlist id", max_bytes=512)
        RunViewRef(run_id=self.run_id)
        positive_int(self.revision, "shortlist revision")
        lower_hex_digest(self.revision_digest, "shortlist revision digest")
        object.__setattr__(self, "title", _canonical_title(self.title))
        cards = tuple(self.cards)
        if any(not isinstance(card, ShortlistCard) for card in cards):
            raise TypeError("shortlist cards must be ShortlistCard values.")
        if tuple(card.position for card in cards) != tuple(
            range(1, len(cards) + 1)
        ):
            raise ValueError("shortlist card positions must be contiguous.")
        if any(card.selection.source_id != self.run_id for card in cards):
            raise RealmIntegrityError(
                "A Shortlist card identifies a Candidate from another Run."
            )
        identities = tuple(card.candidate_id for card in cards)
        if len(identities) != len(set(identities)):
            raise RealmIntegrityError(
                "The Review Collection contains more than one saved snapshot for "
                "the same Candidate and cannot be presented as a Shortlist."
            )
        object.__setattr__(self, "cards", cards)
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "shortlist created_at")
        )

    @classmethod
    def from_review(cls, revision: ReviewCollectionRevision) -> "ShortlistRevision":
        if not isinstance(revision, ReviewCollectionRevision):
            raise TypeError("revision must be a ReviewCollectionRevision.")
        cards = tuple(_card_from_review(item, revision) for item in revision.items)
        return cls(
            shortlist_id=revision.collection_id,
            run_id=revision.primary_source_id,
            revision=revision.revision,
            revision_digest=revision.revision_digest,
            title=revision.title,
            cards=cards,
            created_at=revision.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_SHORTLIST_SCHEMA,
            "shortlist_id": self.shortlist_id,
            "run_id": self.run_id,
            "revision": self.revision,
            "revision_digest": self.revision_digest,
            "title": self.title,
            "cards": [card.to_dict() for card in self.cards],
            "created_at": self.created_at,
        }


def _card_from_review(
    item: ReviewCollectionRevisionItem,
    revision: ReviewCollectionRevision,
) -> ShortlistCard:
    captured_at = item.evidence.get("captured_at", revision.created_at)
    return ShortlistCard(
        position=item.position,
        candidate_id=item.selection.entity_id,
        selection=item.selection,
        note=item.note,
        inspection_outcomes=item.inspection_outcomes,
        evidence=item.evidence,
        evidence_digest=item.evidence_digest,
        saved_result_at=captured_at,
        first_revision=item.first_revision,
    )


@dataclass(frozen=True)
class RealmShortlistService:
    """Safe product facade over one actor-bound Review Collection service."""

    _reviews: RealmReviewCollectionService

    def __post_init__(self) -> None:
        if not isinstance(self._reviews, RealmReviewCollectionService):
            raise TypeError("reviews must be a RealmReviewCollectionService.")

    @property
    def actor_principal_id(self) -> str:
        return self._reviews.actor_principal_id

    def read_for_run(
        self, *, run_id: str, revision: int | None = None
    ) -> ShortlistRevision | None:
        value = self._reviews.read_for_run(run_id=run_id, revision=revision)
        return None if value is None else ShortlistRevision.from_review(value)

    def save_changes(
        self,
        *,
        operation_id: str,
        run_id: str,
        draft: ShortlistDraft,
    ) -> ShortlistRevision:
        """Commit exactly the supplied full draft with optimistic fencing."""

        canonical_run_id = RunViewRef(run_id=run_id).run_id
        intent_digest, replayed = self._bind_command(
            operation_id=operation_id,
            command_kind="save-changes",
            run_id=canonical_run_id,
            command_request={"draft": _command_draft(draft)},
        )
        if replayed is not None:
            return replayed
        canonical_run_id, base = self._load_base(run_id=run_id, draft=draft)
        if base is None:
            raise RealmConflict("Save a Candidate before saving an empty Shortlist.")
        self._validate_cards(base=base, cards=draft.cards)
        cards = self._rehydrate_authority_outcomes(base=base, cards=draft.cards)
        saved = self._reviews._ledger.save_review_collection_revision(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=base.collection_id,
            primary_source_kind="run",
            primary_source_id=canonical_run_id,
            expected_revision=draft.expected_revision,
            title=draft.title,
            retention_policy="decision",
            entries=tuple(card.to_review() for card in cards),
            command_intent_digest=intent_digest,
        )
        return ShortlistRevision.from_review(saved)

    def save_candidate(
        self,
        *,
        operation_id: str,
        run_id: str,
        presentation_selection: Mapping[str, Any],
        draft: ShortlistDraft,
        note: str = "",
        operator_job_id: str | None = None,
        update_saved_result: bool = False,
    ) -> ShortlistRevision:
        """Save one Candidate and the complete pending Shortlist draft atomically.

        When the Candidate already has a card, its snapshot changes only when
        ``update_saved_result`` is true.  The replacement remains in the same
        position and keeps the draft's note and inspection outcomes.
        """

        if not isinstance(update_saved_result, bool):
            raise TypeError("update_saved_result must be boolean.")
        canonical_run_id = RunViewRef(run_id=run_id).run_id
        presented = validate_run_workbench_selection(presentation_selection)
        if presented["run_id"] != canonical_run_id or presented["kind"] != "candidate":
            raise ValueError("Save to Shortlist requires a Candidate from this Run.")
        note = _canonical_note(note)
        if operator_job_id is not None:
            operator_job_id = required_text(
                operator_job_id, "operator job id", max_bytes=512
            )
        intent_digest, replayed = self._bind_command(
            operation_id=operation_id,
            command_kind="save-candidate",
            run_id=canonical_run_id,
            command_request={
                "presentation_selection": presented,
                "draft": _command_draft(draft),
                "note": note,
                "operator_job_id": operator_job_id,
                "update_saved_result": update_saved_result,
            },
        )
        if replayed is not None:
            return replayed
        canonical_run_id, base = self._load_base(run_id=run_id, draft=draft)
        cards_by_digest = self._validate_cards(base=base, cards=draft.cards)
        cards = list(
            self._rehydrate_authority_outcomes(base=base, cards=draft.cards)
        )
        try:
            selection, evidence = self._presented_candidate_snapshot(
                run_id=canonical_run_id,
                presentation_selection=presented,
            )
        except RealmConflict:
            # An identical concurrent command may have committed after the first
            # replay check and before this exact-head resolution.
            replayed = self._replay_bound_command(
                operation_id=operation_id,
                command_intent_digest=intent_digest,
                run_id=canonical_run_id,
            )
            if replayed is not None:
                return replayed
            raise

        candidate_id = selection.entity_id
        matching_index = next(
            (
                index
                for index, card in enumerate(cards)
                if cards_by_digest[card.selection_digest].selection.entity_id
                == candidate_id
            ),
            None,
        )
        new_items: tuple[ReviewCollectionNewItem, ...] = ()
        target_selection = selection
        if matching_index is None:
            outcomes: tuple[Mapping[str, Any], ...] = ()
            cards.append(
                ShortlistCardDraft(
                    selection_digest=selection.selection_digest,
                    note=note,
                    inspection_outcomes=outcomes,
                )
            )
            if selection.selection_digest not in cards_by_digest:
                new_items = (ReviewCollectionNewItem(selection, evidence),)
            matching_index = len(cards) - 1
        else:
            existing = cards[matching_index]
            existing_item = cards_by_digest[existing.selection_digest]
            target_selection = existing_item.selection
            if update_saved_result and (
                existing.selection_digest != selection.selection_digest
            ):
                cards[matching_index] = ShortlistCardDraft(
                    selection_digest=selection.selection_digest,
                    note=existing.note,
                    inspection_outcomes=existing.inspection_outcomes,
                )
                target_selection = selection
                new_items = (ReviewCollectionNewItem(selection, evidence),)

        if operator_job_id is not None:
            outcome = self._reviews._operator_job_inspection_outcome(
                run_id=canonical_run_id,
                review_selection=target_selection,
                operator_job_id=operator_job_id,
            )
            card = cards[matching_index]
            if not any(
                value.get("schema") == REVIEW_INSPECTION_OUTCOME_SCHEMA
                and value.get("operator_job_id") == operator_job_id
                for value in card.inspection_outcomes
            ):
                cards[matching_index] = ShortlistCardDraft(
                    selection_digest=card.selection_digest,
                    note=card.note,
                    inspection_outcomes=(*card.inspection_outcomes, outcome),
                )

        collection_id = (
            base.collection_id
            if base is not None
            else _default_collection_id(
                actor_principal_id=self.actor_principal_id,
                run_id=canonical_run_id,
                creation_operation_id=operation_id,
            )
        )
        saved = self._reviews._ledger.save_review_collection_revision(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=collection_id,
            primary_source_kind="run",
            primary_source_id=canonical_run_id,
            expected_revision=draft.expected_revision,
            title=draft.title,
            retention_policy="decision",
            entries=tuple(card.to_review() for card in cards),
            new_items=new_items,
            command_intent_digest=intent_digest,
        )
        return ShortlistRevision.from_review(saved)

    def attach_inspection(
        self,
        *,
        operation_id: str,
        run_id: str,
        candidate_id: str,
        operator_job_id: str,
        draft: ShortlistDraft,
    ) -> ShortlistRevision:
        """Attach one terminal inspection while committing every pending edit."""

        candidate_id = required_text(
            candidate_id, "shortlist candidate id", max_bytes=512
        )
        operator_job_id = required_text(
            operator_job_id, "operator job id", max_bytes=512
        )
        canonical_run_id = RunViewRef(run_id=run_id).run_id
        intent_digest, replayed = self._bind_command(
            operation_id=operation_id,
            command_kind="attach-inspection",
            run_id=canonical_run_id,
            command_request={
                "candidate_id": candidate_id,
                "operator_job_id": operator_job_id,
                "draft": _command_draft(draft),
            },
        )
        if replayed is not None:
            return replayed
        canonical_run_id, base = self._load_base(run_id=run_id, draft=draft)
        if base is None:
            raise RealmConflict("Save the Candidate before attaching an inspection.")
        cards_by_digest = self._validate_cards(base=base, cards=draft.cards)
        draft_cards = self._rehydrate_authority_outcomes(
            base=base,
            cards=draft.cards,
        )
        matching = [
            card
            for card in draft_cards
            if cards_by_digest[card.selection_digest].selection.entity_id
            == candidate_id
        ]
        if not matching:
            raise RealmConflict(
                "The inspected Candidate is not in this Shortlist draft."
            )
        selected_card = matching[0]
        selected_item = cards_by_digest[selected_card.selection_digest]
        outcome = self._reviews._operator_job_inspection_outcome(
            run_id=canonical_run_id,
            review_selection=selected_item.selection,
            operator_job_id=operator_job_id,
        )
        cards: list[ShortlistCardDraft] = []
        for card in draft_cards:
            if card.selection_digest != selected_card.selection_digest:
                cards.append(card)
                continue
            already_attached = any(
                value.get("schema") == REVIEW_INSPECTION_OUTCOME_SCHEMA
                and value.get("operator_job_id") == operator_job_id
                for value in card.inspection_outcomes
            )
            cards.append(
                card
                if already_attached
                else ShortlistCardDraft(
                    selection_digest=card.selection_digest,
                    note=card.note,
                    inspection_outcomes=(*card.inspection_outcomes, outcome),
                )
            )
        saved = self._reviews._ledger.save_review_collection_revision(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            collection_id=base.collection_id,
            primary_source_kind="run",
            primary_source_id=canonical_run_id,
            expected_revision=draft.expected_revision,
            title=draft.title,
            retention_policy="decision",
            entries=tuple(card.to_review() for card in cards),
            command_intent_digest=intent_digest,
        )
        return ShortlistRevision.from_review(saved)

    def _bind_command(
        self,
        *,
        operation_id: str,
        command_kind: str,
        run_id: str,
        command_request: Mapping[str, Any],
    ) -> tuple[str, ShortlistRevision | None]:
        request = {
            "schema": RUN_SHORTLIST_COMMAND_SCHEMA,
            "run_id": run_id,
            **dict(command_request),
        }
        intent_digest = (
            self._reviews._ledger.bind_review_collection_command_intent(
                operation_id=operation_id,
                actor_principal_id=self.actor_principal_id,
                command_kind=f"shortlist.{command_kind}",
                command_request=request,
            )
        )
        return intent_digest, self._replay_bound_command(
            operation_id=operation_id,
            command_intent_digest=intent_digest,
            run_id=run_id,
        )

    def _replay_bound_command(
        self,
        *,
        operation_id: str,
        command_intent_digest: str,
        run_id: str,
    ) -> ShortlistRevision | None:
        saved = (
            self._reviews._ledger.replay_review_collection_revision_for_intent(
                operation_id=operation_id,
                actor_principal_id=self.actor_principal_id,
                command_intent_digest=command_intent_digest,
            )
        )
        if saved is None:
            return None
        if saved.primary_source_kind != "run" or saved.primary_source_id != run_id:
            raise RealmIntegrityError(
                "Committed Shortlist command identifies another Run."
            )
        return ShortlistRevision.from_review(saved)

    def _load_base(
        self, *, run_id: str, draft: ShortlistDraft
    ) -> tuple[str, ReviewCollectionRevision | None]:
        if not isinstance(draft, ShortlistDraft):
            raise TypeError("draft must be a ShortlistDraft.")
        canonical_run_id = RunViewRef(run_id=run_id).run_id
        if draft.shortlist_id is None:
            if draft.cards:
                raise ValueError("a new Shortlist draft cannot contain saved cards.")
            return canonical_run_id, None
        try:
            base = self._reviews.read_for_run(
                run_id=canonical_run_id,
                revision=draft.expected_revision,
            )
        except RealmNotFound:
            raise RealmConflict("Shortlist changed; reload before saving.") from None
        if base is None or base.collection_id != draft.shortlist_id:
            raise RealmConflict("Shortlist changed; reload before saving.")
        return canonical_run_id, base

    @staticmethod
    def _validate_cards(
        *,
        base: ReviewCollectionRevision | None,
        cards: Sequence[ShortlistCardDraft],
    ) -> dict[str, ReviewCollectionRevisionItem]:
        cards = tuple(cards)
        if base is None:
            if cards:
                raise ValueError("a new Shortlist draft cannot contain saved cards.")
            return {}
        base_by_digest = {
            item.selection.selection_digest: item for item in base.items
        }
        unknown = [
            card.selection_digest
            for card in cards
            if card.selection_digest not in base_by_digest
        ]
        if unknown:
            raise RealmConflict("Shortlist draft contains an unknown saved Candidate.")
        candidate_ids = tuple(
            base_by_digest[card.selection_digest].selection.entity_id
            for card in cards
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RealmConflict(
                "A Shortlist can contain only one card for each Candidate."
            )
        return base_by_digest

    @staticmethod
    def _rehydrate_authority_outcomes(
        *,
        base: ReviewCollectionRevision | None,
        cards: Sequence[ShortlistCardDraft],
    ) -> tuple[ShortlistCardDraft, ...]:
        cards = tuple(cards)
        if base is None:
            if any(
                outcome.get("schema") == REVIEW_INSPECTION_OUTCOME_SCHEMA
                for card in cards
                for outcome in card.inspection_outcomes
            ):
                raise RealmConflict(
                    "Inspection evidence must be attached from its completed job."
                )
            return cards
        try:
            entries = _rehydrate_attached_inspection_outcomes(
                base,
                entries=tuple(card.to_review() for card in cards),
            )
        except RealmConflict:
            raise RealmConflict(
                "Inspection evidence must be attached from its completed job."
            ) from None
        return tuple(
            ShortlistCardDraft(
                selection_digest=entry.selection_digest,
                note=entry.note,
                inspection_outcomes=entry.inspection_outcomes,
            )
            for entry in entries
        )

    def _presented_candidate_snapshot(
        self,
        *,
        run_id: str,
        presentation_selection: Mapping[str, Any],
    ) -> tuple[SelectionRef, Mapping[str, Any]]:
        snapshot = self._reviews._ledger.read_run_snapshot(
            actor_principal_id=self.actor_principal_id,
            run_id=run_id,
        )
        presented = validate_run_workbench_selection(presentation_selection)
        if presented["run_id"] != run_id or presented["kind"] != "candidate":
            raise ValueError("Save to Shortlist requires a Candidate from this Run.")
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
            raise ValueError("Shortlist selection does not identify a Candidate.")
        selection = self._reviews._ledger.mint_run_selection(
            actor_principal_id=self.actor_principal_id,
            run_id=run_id,
            kind="candidate",
            entity_id=candidate.candidate_id,
            expected_run_revision=presented["revision"],
            expected_head_sequence=presented["sequence"],
        )
        evidence = self._reviews._candidate_evidence(
            snapshot=snapshot,
            selection=selection,
            candidate_key=candidate.candidate_key,
        )
        return selection, evidence


__all__ = [
    "DEFAULT_SHORTLIST_TITLE",
    "RUN_SHORTLIST_CARD_SCHEMA",
    "RUN_SHORTLIST_COMMAND_SCHEMA",
    "RUN_SHORTLIST_SCHEMA",
    "RealmShortlistService",
    "ShortlistCard",
    "ShortlistCardDraft",
    "ShortlistDraft",
    "ShortlistRevision",
]
