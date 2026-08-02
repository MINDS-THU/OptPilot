"""Bounded, path-free presentation records for the canonical run timeline.

The Realm stores a complete ordered event stream for recovery, but Studio does
not need raw recovery payloads.  This module exposes only stable event metadata
and a digest of the omitted payload.  Pages are anchored to an exact run head;
callers refresh the head before requesting a newer live page.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ._validation import finite_time, nonnegative_int, positive_int, required_text


RUN_TIMELINE_PAGE_SCHEMA = "optpilot.run-timeline-page.v1"
RUN_TIMELINE_EVENT_SCHEMA = "optpilot.run-timeline-event.v1"
RUN_TIMELINE_DEFAULT_PAGE_SIZE = 50
RUN_TIMELINE_MAX_PAGE_SIZE = 100


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return required_text(value, label, max_bytes=512)


@dataclass(frozen=True)
class RunTimelineEvent:
    """Small operator-facing metadata for one canonical run event."""

    sequence: int
    event_id: str
    source_schema: str
    producer: str
    event: str
    phase: str | None
    state: str | None
    outcome: str | None
    code: str | None
    terminal: bool
    candidate_id: str | None
    logical_trial_id: str | None
    attempt_id: str | None
    attempt_index: int | None
    session_handle: str | None
    method_exchange_id: str | None
    method_round_index: int | None
    method_exchange_kind: str | None
    run_revision: int
    created_at: float
    payload_digest: str

    def __post_init__(self) -> None:
        positive_int(self.sequence, "timeline event sequence")
        required_text(self.event_id, "timeline event id", max_bytes=512)
        required_text(self.source_schema, "timeline source schema", max_bytes=128)
        required_text(self.producer, "timeline producer", max_bytes=128)
        required_text(self.event, "timeline event", max_bytes=256)
        for value, label in (
            (self.phase, "timeline phase"),
            (self.state, "timeline state"),
            (self.outcome, "timeline outcome"),
            (self.code, "timeline code"),
            (self.candidate_id, "timeline candidate id"),
            (self.logical_trial_id, "timeline logical trial id"),
            (self.attempt_id, "timeline attempt id"),
            (self.session_handle, "timeline session handle"),
            (self.method_exchange_id, "timeline method exchange id"),
            (self.method_exchange_kind, "timeline method exchange kind"),
        ):
            _optional_text(value, label)
        if not isinstance(self.terminal, bool):
            raise TypeError("timeline terminal must be a boolean.")
        if (self.attempt_id is None) != (self.attempt_index is None):
            raise ValueError("timeline attempt id and index must be present together.")
        if self.attempt_index is not None:
            positive_int(self.attempt_index, "timeline attempt index")
        method_coordinates = (
            self.method_exchange_id,
            self.method_round_index,
            self.method_exchange_kind,
        )
        if any(value is not None for value in method_coordinates):
            if any(value is None for value in method_coordinates):
                raise ValueError(
                    "timeline method exchange coordinates must be present together."
                )
            assert self.method_round_index is not None
            positive_int(self.method_round_index, "timeline method round index")
            if self.method_exchange_kind not in {"proposal", "observation"}:
                raise ValueError("timeline method exchange kind is invalid.")
        positive_int(self.run_revision, "timeline run revision")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "timeline event created_at"),
        )
        if (
            not isinstance(self.payload_digest, str)
            or len(self.payload_digest) != 71
            or not self.payload_digest.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.payload_digest[7:]
            )
        ):
            raise ValueError("timeline payload digest is invalid.")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RunTimelineEvent":
        payload = row["payload_json"]
        if not isinstance(payload, str):
            raise TypeError("timeline payload_json must be text.")
        digest = hashlib.sha256(
            b"optpilot/run-timeline-payload/v1\0" + payload.encode("utf-8")
        ).hexdigest()
        terminal = row["terminal"]
        if terminal not in {0, 1}:
            raise ValueError("timeline terminal flag is invalid.")
        method_exchange_id: str | None = None
        method_round_index: int | None = None
        method_exchange_kind: str | None = None
        if row["event"] in {
            "method_exchange_prepared",
            "method_exchange_completed",
            "method_exchange_abandoned",
        }:
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise TypeError("timeline method payload must be an object.")
            method_exchange_id = decoded.get("exchange_id")
            method_round_index = decoded.get("round_index")
            method_exchange_kind = decoded.get("kind")
        return cls(
            sequence=row["sequence"],
            event_id=row["event_id"],
            source_schema=row["schema_version"],
            producer=row["producer"],
            event=row["event"],
            phase=row["phase"],
            state=row["state"],
            outcome=row["outcome"],
            code=row["code"],
            terminal=bool(terminal),
            candidate_id=row["candidate_id"],
            logical_trial_id=row["logical_trial_id"],
            attempt_id=row["attempt_id"],
            attempt_index=row["attempt"],
            session_handle=row["session_handle"],
            method_exchange_id=method_exchange_id,
            method_round_index=method_round_index,
            method_exchange_kind=method_exchange_kind,
            run_revision=row["run_revision"],
            created_at=row["created_at"],
            payload_digest=f"sha256:{digest}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_TIMELINE_EVENT_SCHEMA,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "source_schema": self.source_schema,
            "producer": self.producer,
            "event": self.event,
            "phase": self.phase,
            "state": self.state,
            "outcome": self.outcome,
            "code": self.code,
            "terminal": self.terminal,
            "candidate_id": self.candidate_id,
            "logical_trial_id": self.logical_trial_id,
            "attempt_id": self.attempt_id,
            "attempt_index": self.attempt_index,
            "session_handle": self.session_handle,
            "method_exchange_id": self.method_exchange_id,
            "method_round_index": self.method_round_index,
            "method_exchange_kind": self.method_exchange_kind,
            "run_revision": self.run_revision,
            "created_at": self.created_at,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class RunTimelinePage:
    """One bounded event page fixed to a canonical run revision/head."""

    run_id: str
    revision: int
    head_sequence: int
    after_sequence: int
    limit: int
    items: tuple[RunTimelineEvent, ...]
    next_after_sequence: int | None

    def __post_init__(self) -> None:
        required_text(self.run_id, "timeline run id", max_bytes=512)
        nonnegative_int(self.revision, "timeline revision")
        nonnegative_int(self.head_sequence, "timeline head sequence")
        nonnegative_int(self.after_sequence, "timeline after sequence")
        if self.after_sequence > self.head_sequence:
            raise ValueError("timeline after sequence exceeds the exact head.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
            or self.limit > RUN_TIMELINE_MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"timeline limit must be between 1 and {RUN_TIMELINE_MAX_PAGE_SIZE}."
            )
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, RunTimelineEvent) for item in self.items
        ):
            raise TypeError("timeline items must be RunTimelineEvent values.")
        if len(self.items) > self.limit:
            raise ValueError("timeline page exceeds its limit.")
        sequences = tuple(item.sequence for item in self.items)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("timeline event sequences must be unique and ordered.")
        if any(
            sequence <= self.after_sequence or sequence > self.head_sequence
            for sequence in sequences
        ):
            raise ValueError("timeline event lies outside the requested head range.")
        if self.next_after_sequence is not None:
            positive_int(
                self.next_after_sequence,
                "timeline next after sequence",
            )
            if not sequences or self.next_after_sequence != sequences[-1]:
                raise ValueError("timeline continuation differs from the page tail.")

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        revision: int,
        head_sequence: int,
        after_sequence: int,
        limit: int,
        rows: Sequence[Mapping[str, Any]],
    ) -> "RunTimelinePage":
        selected = tuple(rows[:limit])
        items = tuple(RunTimelineEvent.from_row(row) for row in selected)
        has_more = len(rows) > limit
        return cls(
            run_id=run_id,
            revision=revision,
            head_sequence=head_sequence,
            after_sequence=after_sequence,
            limit=limit,
            items=items,
            next_after_sequence=(items[-1].sequence if has_more else None),
        )

    @property
    def has_more(self) -> bool:
        return self.next_after_sequence is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_TIMELINE_PAGE_SCHEMA,
            "run_id": self.run_id,
            "head": {
                "revision": self.revision,
                "sequence": self.head_sequence,
            },
            "query": {
                "after_sequence": self.after_sequence,
                "limit": self.limit,
            },
            "items": [item.to_dict() for item in self.items],
            "page": {
                "count": len(self.items),
                "has_more": self.has_more,
                "next_after_sequence": self.next_after_sequence,
            },
            "limitations": {
                "bounded_public_page": True,
                "max_page_size": RUN_TIMELINE_MAX_PAGE_SIZE,
                "exact_head_required": True,
                "recovery_payload_omitted": True,
                "payload_digest_available": True,
            },
        }


__all__ = [
    "RUN_TIMELINE_DEFAULT_PAGE_SIZE",
    "RUN_TIMELINE_EVENT_SCHEMA",
    "RUN_TIMELINE_MAX_PAGE_SIZE",
    "RUN_TIMELINE_PAGE_SCHEMA",
    "RunTimelineEvent",
    "RunTimelinePage",
]
