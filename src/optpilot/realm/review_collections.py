"""Immutable, revisioned human-decision records over exact Realm selections.

A Review Collection is metadata authority, not a workspace or runtime.  Its
revision entries point at immutable :class:`SelectionRef` values and freeze the
bounded evidence that was visible when an item was added.  Content retention is
implemented separately by owner memberships, so preserving a decision reuses
CAS bytes instead of copying them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ._validation import (
    finite_time,
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .refs import canonical_json_bytes, request_digest
from .selections import SelectionRef


REVIEW_COLLECTION_OWNER_KIND = "review-collection"
REVIEW_COLLECTION_REVISION_SCHEMA = "optpilot.review-collection-revision.v1"
REVIEW_COLLECTION_ITEM_EVIDENCE_SCHEMA = "optpilot.review-item-evidence.v1"
REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA = (
    "optpilot.review-public-item-evidence.v1"
)
REVIEW_COLLECTION_PUBLIC_SELECTION_SCHEMA = "optpilot.review-public-selection.v1"
REVIEW_COLLECTION_REDACTED_INSPECTION_OUTCOME_SCHEMA = (
    "optpilot.review-redacted-inspection-outcome.v1"
)
REVIEW_INSPECTION_OUTCOME_SCHEMA = "optpilot.review-inspection-outcome.v1"
REVIEW_COLLECTION_EXPORT_SCHEMA = "optpilot.review-decision-export.v1"
REVIEW_COLLECTION_HISTORY_SCHEMA = "optpilot.review-collection-history.v1"
REVIEW_COLLECTION_DELETION_SCHEMA = "optpilot.review-collection-deletion.v1"
REVIEW_COLLECTION_POLICIES = frozenset({"decision", "runnable"})

REVIEW_COLLECTION_MAX_ITEMS = 64
REVIEW_COLLECTION_MAX_TITLE_BYTES = 512
REVIEW_COLLECTION_MAX_NOTE_BYTES = 64 * 1024
REVIEW_COLLECTION_MAX_INSPECTION_OUTCOMES = 32
REVIEW_COLLECTION_MAX_INSPECTION_OUTCOME_BYTES = 128 * 1024
REVIEW_COLLECTION_MAX_ITEM_EVIDENCE_BYTES = 512 * 1024
REVIEW_COLLECTION_MAX_REVISION_BYTES = 3 * 1024 * 1024
REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE = 100


def _bounded_text(value: str, label: str, *, max_bytes: int) -> str:
    value = required_text(value, label, max_bytes=max_bytes)
    if value.strip() != value:
        raise ValueError(f"{label} must not have leading or trailing whitespace.")
    return value


def _note(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("review note must be a string.")
    if len(value.encode("utf-8")) > REVIEW_COLLECTION_MAX_NOTE_BYTES:
        raise ValueError("review note is too large.")
    return value


def _frozen_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    frozen = freeze_json(value, label=label)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    return frozen


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _selected_mapping(
    value: Any,
    keys: Sequence[str],
) -> dict[str, Any]:
    source = _mapping(value)
    return {key: thaw_json(source.get(key)) for key in keys}


def public_review_selection(selection: SelectionRef) -> dict[str, Any]:
    """Project the coordinate needed by Shortlist edits without authority data."""

    if not isinstance(selection, SelectionRef):
        raise TypeError("selection must be a SelectionRef.")
    return {
        "schema": REVIEW_COLLECTION_PUBLIC_SELECTION_SCHEMA,
        "kind": selection.kind,
        "source_kind": selection.source_kind,
        "source_id": selection.source_id,
        "source_revision": selection.source_revision,
        "source_sequence": selection.source_sequence,
        "entity_id": selection.entity_id,
        "selection_digest": selection.selection_digest,
    }


def _public_candidate_result(value: Any) -> dict[str, Any]:
    result = _mapping(value)
    aggregate = result.get("aggregate")
    return {
        "schema": result.get("schema"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "objective": _selected_mapping(
            result.get("objective"),
            ("metric", "direction", "aggregation_mode"),
        ),
        "counts": _selected_mapping(
            result.get("counts"),
            (
                "logical_trials",
                "active",
                "terminal",
                "successful",
                "terminal_failures",
                "usable_objectives",
                "attempts",
                "retries",
            ),
        ),
        "aggregate": (
            None
            if not isinstance(aggregate, Mapping)
            else _selected_mapping(aggregate, ("value", "sample_count"))
        ),
        "comparison": _selected_mapping(
            result.get("comparison"),
            (
                "eligible",
                "rank",
                "group_size",
                "ranked_candidate_count",
                "tie_count",
                "finality",
                "reason",
                "group_ordinal",
                "scope",
            ),
        ),
    }


def _public_retention(value: Any) -> dict[str, Any]:
    return _selected_mapping(
        value,
        (
            "policy",
            "content_reused_without_copy",
            "candidate_content_count",
            "artifact_content_count",
            "artifact_logical_bytes",
            "runnable_closure_retained",
        ),
    )


def _public_frozen_count(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    rows = source.get("rows")
    return {
        "total": source.get("total"),
        "frozen_count": len(rows) if isinstance(rows, (list, tuple)) else 0,
        "truncated": bool(source.get("truncated")),
        "details_included": False,
    }


def public_review_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a strict public summary of an internally exact evidence record.

    Review evidence deliberately retains enough internal material to prove and
    retain the decision.  That record is not a presentation DTO: candidate
    specs can contain credentials, artifact rows contain content references,
    and selection records contain owner coordinates.  This whitelist keeps the
    useful result/comparison/retention facts while making those categories
    impossible to serialize accidentally.
    """

    evidence = _mapping(value)
    anchor = _mapping(evidence.get("source_anchor"))
    candidate = _mapping(evidence.get("candidate"))
    admission = _mapping(candidate.get("admission"))
    envelope = _mapping(admission.get("envelope"))
    return {
        "schema": REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA,
        "captured_at": evidence.get("captured_at"),
        "source": {
            "run_id": anchor.get("run_id"),
            "revision": anchor.get("revision"),
            "sequence": anchor.get("sequence"),
            "terminal_evidence_sealed": bool(anchor.get("terminal_seal_digest")),
        },
        "candidate": {
            "id": admission.get("candidate_id"),
            "format": envelope.get("format"),
        },
        "candidate_result": _public_candidate_result(
            evidence.get("candidate_result")
        ),
        "observations": _public_frozen_count(evidence.get("observations")),
        "artifacts": _public_frozen_count(evidence.get("artifacts")),
        "retention": _public_retention(evidence.get("retention")),
    }


def _public_metric_values(value: Any) -> dict[str, Any]:
    metrics = _mapping(value)
    raw_values = _mapping(metrics.get("values"))
    values: dict[str, int | float | bool] = {}
    for name, raw in raw_values.items():
        if not isinstance(name, str):
            continue
        if isinstance(raw, bool) or isinstance(raw, int):
            values[name] = raw
        elif isinstance(raw, float) and math.isfinite(raw):
            values[name] = raw
    return {
        "total": metrics.get("total"),
        "returned": len(values),
        "truncated": bool(metrics.get("truncated")) or len(values) < len(raw_values),
        "values": values,
    }


def public_review_inspection_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project terminal inspection evidence without target/runtime authority."""

    outcome = _mapping(value)
    if outcome.get("schema") != REVIEW_INSPECTION_OUTCOME_SCHEMA:
        return {
            "schema": REVIEW_COLLECTION_REDACTED_INSPECTION_OUTCOME_SCHEMA,
            "kind": "redacted",
            "details_included": False,
        }
    terminal = _mapping(outcome.get("outcome"))
    result_value = outcome.get("result")
    result = None
    if isinstance(result_value, Mapping):
        declared_outputs = _mapping(result_value.get("declared_outputs"))
        result = {
            "result_kind": result_value.get("result_kind"),
            "status": result_value.get("status"),
            "metrics": _public_metric_values(result_value.get("metrics")),
            "declared_outputs": {
                "total": declared_outputs.get("total"),
                "returned": declared_outputs.get("returned"),
                "truncated": bool(declared_outputs.get("truncated")),
                "details_included": False,
            },
        }
    return {
        "schema": REVIEW_INSPECTION_OUTCOME_SCHEMA,
        "kind": "operator_job",
        "operator_job_id": outcome.get("operator_job_id"),
        "job_kind": outcome.get("job_kind"),
        "outcome": {
            "status": terminal.get("status"),
            "code": terminal.get("code"),
        },
        "result": result,
        "completed_at": outcome.get("completed_at"),
    }


@dataclass(frozen=True)
class ReviewCollectionNewItem:
    """One exact selection plus evidence frozen on its first collection use."""

    selection: SelectionRef
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("review item selection must be a SelectionRef.")
        evidence = _frozen_mapping(self.evidence, "review item evidence")
        if evidence.get("schema") != REVIEW_COLLECTION_ITEM_EVIDENCE_SCHEMA:
            raise ValueError("review item evidence schema is unsupported.")
        if evidence.get("selection_digest") != self.selection.selection_digest:
            raise ValueError("review item evidence identifies another selection.")
        if len(canonical_json_bytes(thaw_json(evidence))) > (
            REVIEW_COLLECTION_MAX_ITEM_EVIDENCE_BYTES
        ):
            raise ValueError("review item evidence is too large.")
        object.__setattr__(self, "evidence", evidence)

    @property
    def evidence_digest(self) -> str:
        return request_digest(thaw_json(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "evidence": thaw_json(self.evidence),
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class ReviewCollectionEntryDraft:
    """Mutable form values that become immutable in a saved revision."""

    selection_digest: str
    note: str = ""
    inspection_outcomes: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        lower_hex_digest(self.selection_digest, "review selection digest")
        object.__setattr__(self, "note", _note(self.note))
        outcomes = tuple(self.inspection_outcomes)
        if len(outcomes) > REVIEW_COLLECTION_MAX_INSPECTION_OUTCOMES:
            raise ValueError("review item has too many inspection outcomes.")
        normalized: list[Mapping[str, Any]] = []
        for outcome in outcomes:
            frozen = _frozen_mapping(outcome, "review inspection outcome")
            if len(canonical_json_bytes(thaw_json(frozen))) > (
                REVIEW_COLLECTION_MAX_INSPECTION_OUTCOME_BYTES
            ):
                raise ValueError("review inspection outcome is too large.")
            normalized.append(frozen)
        object.__setattr__(self, "inspection_outcomes", tuple(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_digest": self.selection_digest,
            "note": self.note,
            "inspection_outcomes": [
                thaw_json(item) for item in self.inspection_outcomes
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewCollectionEntryDraft":
        try:
            if set(payload) != {
                "selection_digest",
                "note",
                "inspection_outcomes",
            }:
                raise ValueError("review entry fields differ")
            outcomes = payload["inspection_outcomes"]
            if not isinstance(outcomes, list):
                raise TypeError("inspection_outcomes must be a list")
            return cls(
                selection_digest=payload["selection_digest"],
                note=payload["note"],
                inspection_outcomes=tuple(outcomes),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted review entry is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class ReviewCollectionRevisionItem:
    """One fully resolved item in an immutable collection revision."""

    position: int
    selection: SelectionRef
    note: str
    inspection_outcomes: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    evidence_digest: str
    first_revision: int

    def __post_init__(self) -> None:
        positive_int(self.position, "review item position")
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("review item selection must be a SelectionRef.")
        object.__setattr__(self, "note", _note(self.note))
        outcomes = tuple(
            _frozen_mapping(item, "review inspection outcome")
            for item in self.inspection_outcomes
        )
        if len(outcomes) > REVIEW_COLLECTION_MAX_INSPECTION_OUTCOMES:
            raise ValueError("review item has too many inspection outcomes.")
        object.__setattr__(self, "inspection_outcomes", outcomes)
        evidence = _frozen_mapping(self.evidence, "review item evidence")
        if evidence.get("selection_digest") != self.selection.selection_digest:
            raise ValueError("review item evidence identifies another selection.")
        object.__setattr__(self, "evidence", evidence)
        lower_hex_digest(self.evidence_digest, "review evidence digest")
        if request_digest(thaw_json(evidence)) != self.evidence_digest:
            raise ValueError("review evidence digest is invalid.")
        positive_int(self.first_revision, "review item first revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "selection": self.selection.to_dict(),
            "note": self.note,
            "inspection_outcomes": [
                thaw_json(item) for item in self.inspection_outcomes
            ],
            "evidence": thaw_json(self.evidence),
            "evidence_digest": self.evidence_digest,
            "first_revision": self.first_revision,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return the portable decision view, never the retained authority row."""

        return {
            "position": self.position,
            "selection": public_review_selection(self.selection),
            "note": self.note,
            "inspection_outcomes": [
                public_review_inspection_outcome(item)
                for item in self.inspection_outcomes
            ],
            "evidence": public_review_evidence(self.evidence),
            "evidence_digest": self.evidence_digest,
            "first_revision": self.first_revision,
        }


def review_revision_digest(
    *,
    collection_id: str,
    revision: int,
    title: str,
    retention_policy: str,
    owner_revision: int,
    items: Sequence[ReviewCollectionRevisionItem],
) -> str:
    return request_digest(
        {
            "schema": REVIEW_COLLECTION_REVISION_SCHEMA,
            "collection_id": collection_id,
            "revision": revision,
            "title": title,
            "retention_policy": retention_policy,
            "owner_revision": owner_revision,
            "items": [item.to_dict() for item in items],
        }
    )


@dataclass(frozen=True)
class ReviewCollectionRevision:
    collection_id: str
    owner_id: str
    revision: int
    revision_digest: str
    title: str
    retention_policy: str
    owner_revision: int
    primary_source_kind: str
    primary_source_id: str
    items: tuple[ReviewCollectionRevisionItem, ...]
    created_by: str
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.collection_id, "review collection id", max_bytes=512)
        required_text(self.owner_id, "review owner id", max_bytes=512)
        positive_int(self.revision, "review collection revision")
        lower_hex_digest(self.revision_digest, "review revision digest")
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                "review collection title",
                max_bytes=REVIEW_COLLECTION_MAX_TITLE_BYTES,
            ),
        )
        if self.retention_policy not in REVIEW_COLLECTION_POLICIES:
            raise ValueError("review retention policy is unsupported.")
        if isinstance(self.owner_revision, bool) or not isinstance(
            self.owner_revision, int
        ) or self.owner_revision < 0:
            raise ValueError("review owner revision must be nonnegative.")
        required_text(self.primary_source_kind, "review primary source kind")
        required_text(self.primary_source_id, "review primary source id")
        items = tuple(self.items)
        if len(items) > REVIEW_COLLECTION_MAX_ITEMS:
            raise ValueError("review revision contains too many shortlist items.")
        if any(
            not isinstance(item, ReviewCollectionRevisionItem) for item in items
        ):
            raise TypeError("review revision items have the wrong type.")
        if tuple(item.position for item in items) != tuple(
            range(1, len(items) + 1)
        ):
            raise ValueError("review item positions must be contiguous.")
        digests = tuple(item.selection.selection_digest for item in items)
        if len(digests) != len(set(digests)):
            raise ValueError("review revision contains duplicate selections.")
        object.__setattr__(self, "items", items)
        required_text(self.created_by, "review revision creator")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "review created_at")
        )
        expected = review_revision_digest(
            collection_id=self.collection_id,
            revision=self.revision,
            title=self.title,
            retention_policy=self.retention_policy,
            owner_revision=self.owner_revision,
            items=items,
        )
        if expected != self.revision_digest:
            raise ValueError("review revision digest is invalid.")
        if len(canonical_json_bytes(self.to_dict())) > (
            REVIEW_COLLECTION_MAX_REVISION_BYTES
        ):
            raise ValueError("review collection revision is too large.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_COLLECTION_REVISION_SCHEMA,
            "collection_id": self.collection_id,
            "owner_id": self.owner_id,
            "revision": self.revision,
            "revision_digest": self.revision_digest,
            "title": self.title,
            "retention_policy": self.retention_policy,
            "owner_revision": self.owner_revision,
            "primary_source": {
                "kind": self.primary_source_kind,
                "id": self.primary_source_id,
            },
            "items": [item.to_dict() for item in self.items],
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    def export_dict(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_COLLECTION_EXPORT_SCHEMA,
            "collection_id": self.collection_id,
            "revision": self.revision,
            "revision_digest": self.revision_digest,
            "title": self.title,
            "retention_policy": self.retention_policy,
            "items": [item.to_public_dict() for item in self.items],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ReviewCollectionRevisionSummary:
    """Small, path-free metadata used to navigate immutable revisions."""

    revision: int
    revision_digest: str
    title: str
    retention_policy: str
    owner_revision: int
    item_count: int
    created_by: str
    created_at: float

    def __post_init__(self) -> None:
        positive_int(self.revision, "review collection revision")
        lower_hex_digest(self.revision_digest, "review revision digest")
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                "review collection title",
                max_bytes=REVIEW_COLLECTION_MAX_TITLE_BYTES,
            ),
        )
        if self.retention_policy not in REVIEW_COLLECTION_POLICIES:
            raise ValueError("review retention policy is unsupported.")
        for label, value in (
            ("review owner revision", self.owner_revision),
            ("review item count", self.item_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be nonnegative.")
        if self.item_count > REVIEW_COLLECTION_MAX_ITEMS:
            raise ValueError("review revision contains too many shortlist items.")
        required_text(self.created_by, "review revision creator")
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "review created_at")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "revision_digest": self.revision_digest,
            "title": self.title,
            "retention_policy": self.retention_policy,
            "owner_revision": self.owner_revision,
            "item_count": self.item_count,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ReviewCollectionHistoryPage:
    """Newest-first, bounded navigation over one collection's revisions."""

    collection_id: str
    current_revision: int
    items: tuple[ReviewCollectionRevisionSummary, ...]
    has_more: bool
    next_before_revision: int | None

    def __post_init__(self) -> None:
        required_text(self.collection_id, "review collection id", max_bytes=512)
        positive_int(self.current_revision, "review current revision")
        items = tuple(self.items)
        if len(items) > REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE:
            raise ValueError("review history page exceeds its fixed bound.")
        if any(not isinstance(item, ReviewCollectionRevisionSummary) for item in items):
            raise TypeError("review history items have the wrong type.")
        if tuple(item.revision for item in items) != tuple(
            sorted((item.revision for item in items), reverse=True)
        ):
            raise ValueError("review history must be newest first.")
        object.__setattr__(self, "items", items)
        if not isinstance(self.has_more, bool):
            raise TypeError("review history has_more must be boolean.")
        if self.has_more:
            if not items or self.next_before_revision != items[-1].revision:
                raise ValueError("review history continuation is invalid.")
        elif self.next_before_revision is not None:
            raise ValueError("complete review history cannot have a continuation.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_COLLECTION_HISTORY_SCHEMA,
            "collection_id": self.collection_id,
            "current_revision": self.current_revision,
            "items": [item.to_dict() for item in self.items],
            "page": {
                "has_more": self.has_more,
                "next_before_revision": self.next_before_revision,
            },
        }


@dataclass(frozen=True)
class ReviewCollectionDeletionReceipt:
    """Durable proof that one complete collection and its owner were retired."""

    collection_id: str
    primary_source_kind: str
    primary_source_id: str
    previous_revision: int
    previous_revision_digest: str
    previous_owner_revision: int
    owner_revision: int
    released_memberships: int
    deleted_at: float

    def __post_init__(self) -> None:
        required_text(self.collection_id, "review collection id", max_bytes=512)
        required_text(self.primary_source_kind, "review primary source kind")
        required_text(
            self.primary_source_id,
            "review primary source id",
            max_bytes=512,
        )
        positive_int(self.previous_revision, "review previous revision")
        lower_hex_digest(
            self.previous_revision_digest,
            "review previous revision digest",
        )
        nonnegative_int(
            self.previous_owner_revision,
            "review previous owner revision",
        )
        positive_int(self.owner_revision, "review deletion owner revision")
        if self.owner_revision != self.previous_owner_revision + 1:
            raise ValueError("review deletion owner revision must advance once.")
        nonnegative_int(
            self.released_memberships,
            "review released memberships",
        )
        object.__setattr__(
            self,
            "deleted_at",
            finite_time(self.deleted_at, "review deleted_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REVIEW_COLLECTION_DELETION_SCHEMA,
            "collection_id": self.collection_id,
            "primary_source": {
                "kind": self.primary_source_kind,
                "id": self.primary_source_id,
            },
            "previous_revision": self.previous_revision,
            "previous_revision_digest": self.previous_revision_digest,
            "previous_owner_revision": self.previous_owner_revision,
            "owner_revision": self.owner_revision,
            "released_memberships": self.released_memberships,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ReviewCollectionDeletionReceipt":
        try:
            required = {
                "schema",
                "collection_id",
                "primary_source",
                "previous_revision",
                "previous_revision_digest",
                "previous_owner_revision",
                "owner_revision",
                "released_memberships",
                "deleted_at",
            }
            if (
                not required.issubset(payload)
                or set(payload) - required - {"receipt_version"}
            ):
                raise ValueError("review deletion receipt fields differ")
            if payload.get("receipt_version", 1) != 1:
                raise ValueError("review deletion receipt version is unsupported")
            if payload["schema"] != REVIEW_COLLECTION_DELETION_SCHEMA:
                raise ValueError("review deletion receipt schema is unsupported")
            source = payload["primary_source"]
            if not isinstance(source, Mapping) or set(source) != {"kind", "id"}:
                raise ValueError("review deletion source is invalid")
            return cls(
                collection_id=payload["collection_id"],
                primary_source_kind=source["kind"],
                primary_source_id=source["id"],
                previous_revision=payload["previous_revision"],
                previous_revision_digest=payload["previous_revision_digest"],
                previous_owner_revision=payload["previous_owner_revision"],
                owner_revision=payload["owner_revision"],
                released_memberships=payload["released_memberships"],
                deleted_at=payload["deleted_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted review deletion receipt is invalid: {error}"
            ) from error


__all__ = [
    "REVIEW_COLLECTION_DELETION_SCHEMA",
    "REVIEW_COLLECTION_EXPORT_SCHEMA",
    "REVIEW_COLLECTION_HISTORY_SCHEMA",
    "REVIEW_COLLECTION_ITEM_EVIDENCE_SCHEMA",
    "REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE",
    "REVIEW_COLLECTION_MAX_INSPECTION_OUTCOME_BYTES",
    "REVIEW_COLLECTION_MAX_ITEMS",
    "REVIEW_COLLECTION_OWNER_KIND",
    "REVIEW_COLLECTION_POLICIES",
    "REVIEW_COLLECTION_PUBLIC_ITEM_EVIDENCE_SCHEMA",
    "REVIEW_COLLECTION_PUBLIC_SELECTION_SCHEMA",
    "REVIEW_COLLECTION_REDACTED_INSPECTION_OUTCOME_SCHEMA",
    "REVIEW_COLLECTION_REVISION_SCHEMA",
    "REVIEW_INSPECTION_OUTCOME_SCHEMA",
    "ReviewCollectionEntryDraft",
    "ReviewCollectionDeletionReceipt",
    "ReviewCollectionHistoryPage",
    "ReviewCollectionNewItem",
    "ReviewCollectionRevision",
    "ReviewCollectionRevisionItem",
    "ReviewCollectionRevisionSummary",
    "public_review_evidence",
    "public_review_inspection_outcome",
    "public_review_selection",
    "review_revision_digest",
]
