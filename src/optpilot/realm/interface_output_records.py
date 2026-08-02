"""Durable, path-free records for interface-produced content generations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .interface_outputs import InterfaceOutputKind, InterfaceOutputRecord
from .owners import OwnerMembership
from .refs import (
    BlobRef,
    PhysicalContentRef,
    SnapshotRef,
    parse_physical_content_ref,
    request_digest,
)
from .selections import SelectionRef


INTERFACE_OUTPUT_SESSION_SCHEMA = "optpilot.interface-output-session.v1"
INTERFACE_OUTPUT_GENERATION_SCHEMA = "optpilot.interface-output-generation.v1"
INTERFACE_OUTPUT_SESSION_ROLE = "interface-output"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _identifier(value: Any, label: str, *, max_bytes: int = 512) -> str:
    result = required_text(value, label, max_bytes=max_bytes)
    if _IDENTIFIER_RE.fullmatch(result) is None:
        raise ValueError(
            f"{label} must contain only letters, digits, '.', '_', or '-'."
        )
    return result


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are not canonical.")


class InterfaceOutputSessionState(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class InterfaceOutputGenerationState(str, Enum):
    SEALING = "sealing"
    FAILED = "failed"
    READY = "ready"


@dataclass(frozen=True)
class InterfaceOutputSessionRecord:
    session_id: str
    owner_id: str
    launch_id: str
    session_lease_id: str
    state: InterfaceOutputSessionState
    current_revision: int
    max_generations: int
    max_logical_bytes: int
    created_txn_id: int
    updated_txn_id: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        for name in ("session_id", "owner_id", "launch_id", "session_lease_id"):
            _identifier(getattr(self, name), f"interface output {name}")
        if not isinstance(self.state, InterfaceOutputSessionState):
            raise TypeError("state must be an InterfaceOutputSessionState.")
        nonnegative_int(self.current_revision, "interface output session revision")
        positive_int(self.max_generations, "interface output session max_generations")
        positive_int(
            self.max_logical_bytes,
            "interface output session max_logical_bytes",
        )
        if self.current_revision > self.max_generations:
            raise ValueError("interface output session exceeds max_generations.")
        positive_int(self.created_txn_id, "interface output session created_txn_id")
        positive_int(self.updated_txn_id, "interface output session updated_txn_id")
        created = finite_time(self.created_at, "interface output session created_at")
        updated = finite_time(self.updated_at, "interface output session updated_at")
        if updated < created:
            raise ValueError("interface output session updated_at precedes created_at.")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": INTERFACE_OUTPUT_SESSION_SCHEMA,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "launch_id": self.launch_id,
            "session_lease_id": self.session_lease_id,
            "state": self.state.value,
            "current_revision": self.current_revision,
            "max_generations": self.max_generations,
            "max_logical_bytes": self.max_logical_bytes,
            "created_txn_id": self.created_txn_id,
            "updated_txn_id": self.updated_txn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterfaceOutputSessionRecord":
        try:
            _exact_keys(value, set(cls.__dataclass_fields__) | {"format"}, "interface output session")
            if value["format"] != INTERFACE_OUTPUT_SESSION_SCHEMA:
                raise ValueError("interface output session schema is unsupported.")
            result = cls(
                session_id=value["session_id"],
                owner_id=value["owner_id"],
                launch_id=value["launch_id"],
                session_lease_id=value["session_lease_id"],
                state=InterfaceOutputSessionState(value["state"]),
                current_revision=value["current_revision"],
                max_generations=value["max_generations"],
                max_logical_bytes=value["max_logical_bytes"],
                created_txn_id=value["created_txn_id"],
                updated_txn_id=value["updated_txn_id"],
                created_at=value["created_at"],
                updated_at=value["updated_at"],
            )
            if result.to_dict() != dict(value):
                raise ValueError("interface output session is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface output session is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceOutputGenerationRecord:
    session_id: str
    owner_id: str
    session_revision: int
    owner_revision: int
    output_id: str
    label: str
    kind: InterfaceOutputKind
    record_digest: str
    store_id: str
    content_ref: PhysicalContentRef
    logical_bytes: int
    committed_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        _identifier(self.session_id, "interface output session_id")
        _identifier(self.owner_id, "interface output owner_id")
        _identifier(self.output_id, "interface output id", max_bytes=128)
        required_text(self.label, "interface output label", max_bytes=512)
        if not isinstance(self.kind, InterfaceOutputKind):
            raise TypeError("kind must be an InterfaceOutputKind.")
        if self.kind is InterfaceOutputKind.TREE and not isinstance(
            self.content_ref, SnapshotRef
        ):
            raise TypeError("tree interface output must reference a SnapshotRef.")
        if self.kind is InterfaceOutputKind.FILE and not isinstance(
            self.content_ref, BlobRef
        ):
            raise TypeError("file interface output must reference a BlobRef.")
        positive_int(self.session_revision, "interface output session_revision")
        nonnegative_int(self.owner_revision, "interface output owner_revision")
        lower_hex_digest(self.record_digest, "interface output record_digest")
        required_text(self.store_id, "interface output store_id", max_bytes=128)
        nonnegative_int(self.logical_bytes, "interface output logical_bytes")
        positive_int(self.committed_txn_id, "interface output committed_txn_id")
        finite_time(self.created_at, "interface output created_at")

    @property
    def membership(self) -> OwnerMembership:
        return OwnerMembership(
            self.store_id, self.content_ref, INTERFACE_OUTPUT_SESSION_ROLE
        )

    @property
    def selection(self) -> SelectionRef | None:
        return SelectionRef.build(
            kind="artifact",
            source_kind="interface-output",
            source_id=self.session_id,
            source_owner_id=self.owner_id,
            source_revision=self.session_revision,
            owner_revision=self.owner_revision,
            source_sequence=None,
            entity_sequence=None,
            entity_id=self.output_id,
            entity_ref=str(self.content_ref),
            context_digest=self.record_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "format": INTERFACE_OUTPUT_GENERATION_SCHEMA,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "session_revision": self.session_revision,
            "owner_revision": self.owner_revision,
            "id": self.output_id,
            "label": self.label,
            "kind": self.kind.value,
            "record_digest": self.record_digest,
            "store_id": self.store_id,
            "content_ref": str(self.content_ref),
            "logical_bytes": self.logical_bytes,
            "committed_txn_id": self.committed_txn_id,
            "created_at": self.created_at,
            "status": "ready",
            "selection": None if self.selection is None else self.selection.to_dict(),
        }

    @classmethod
    def from_row(cls, value: Mapping[str, Any]) -> "InterfaceOutputGenerationRecord":
        try:
            return cls(
                session_id=value["session_id"],
                owner_id=value["owner_id"],
                session_revision=int(value["session_revision"]),
                owner_revision=int(value["owner_revision"]),
                output_id=value["output_id"],
                label=value["label"],
                kind=InterfaceOutputKind(value["kind"]),
                record_digest=value["record_digest"],
                store_id=value["store_id"],
                content_ref=parse_physical_content_ref(value["content_ref"]),
                logical_bytes=int(value["logical_bytes"]),
                committed_txn_id=int(value["committed_txn_id"]),
                created_at=float(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface output generation is invalid: {error}"
            ) from error

    def matches_record(self, record: InterfaceOutputRecord) -> bool:
        return (
            self.output_id == record.output_id
            and self.label == record.label
            and self.kind is record.kind
            and self.record_digest == request_digest(record.to_dict())
        )


@dataclass(frozen=True)
class InterfaceOutputGenerationStatusRecord:
    """Durable path-free state for one interface-declared generation."""

    session_id: str
    owner_id: str
    output_id: str
    label: str
    kind: InterfaceOutputKind
    root_handle: str
    relative_path: str
    record_digest: str
    state: InterfaceOutputGenerationState
    attempt_number: int
    attempt_id: str
    operation_prefix: str
    change_id: str
    retention_lease_id: str
    attempt_expires_at: float | None
    error_code: str | None
    session_revision: int | None
    owner_revision: int | None
    store_id: str | None
    content_ref: PhysicalContentRef | None
    logical_bytes: int | None
    committed_txn_id: int | None
    created_txn_id: int
    updated_txn_id: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _identifier(self.session_id, "interface output status session_id")
        _identifier(self.owner_id, "interface output status owner_id")
        _identifier(self.output_id, "interface output status id", max_bytes=128)
        required_text(self.label, "interface output status label", max_bytes=512)
        if not isinstance(self.kind, InterfaceOutputKind):
            raise TypeError("kind must be an InterfaceOutputKind.")
        _identifier(
            self.root_handle,
            "interface output status root_handle",
            max_bytes=128,
        )
        required_text(
            self.relative_path,
            "interface output status relative_path",
            max_bytes=4096,
        )
        lower_hex_digest(self.record_digest, "interface output status record_digest")
        if not isinstance(self.state, InterfaceOutputGenerationState):
            raise TypeError("state must be an InterfaceOutputGenerationState.")
        positive_int(self.attempt_number, "interface output status attempt_number")
        _identifier(self.attempt_id, "interface output status attempt_id")
        _identifier(
            self.operation_prefix,
            "interface output status operation_prefix",
        )
        _identifier(self.change_id, "interface output status change_id")
        _identifier(
            self.retention_lease_id,
            "interface output status retention_lease_id",
        )
        positive_int(self.created_txn_id, "interface output status created_txn_id")
        positive_int(self.updated_txn_id, "interface output status updated_txn_id")
        created = finite_time(self.created_at, "interface output status created_at")
        updated = finite_time(self.updated_at, "interface output status updated_at")
        if updated < created:
            raise ValueError("interface output status updated_at precedes created_at.")
        if self.state is InterfaceOutputGenerationState.SEALING:
            finite_time(
                self.attempt_expires_at,
                "interface output status attempt_expires_at",
            )
            if self.error_code is not None or self._has_ready_fields():
                raise ValueError("sealing interface output has terminal fields.")
        elif self.state is InterfaceOutputGenerationState.FAILED:
            if self.attempt_expires_at is not None or self._has_ready_fields():
                raise ValueError("failed interface output has invalid terminal fields.")
            _identifier(
                self.error_code,
                "interface output status error_code",
                max_bytes=128,
            )
        else:
            if self.attempt_expires_at is not None or self.error_code is not None:
                raise ValueError("ready interface output has capture error fields.")
            if any(
                value is None
                for value in (
                    self.session_revision,
                    self.owner_revision,
                    self.store_id,
                    self.content_ref,
                    self.logical_bytes,
                    self.committed_txn_id,
                )
            ):
                raise ValueError("ready interface output is missing content fields.")
            positive_int(
                self.session_revision,
                "interface output status session_revision",
            )
            nonnegative_int(
                self.owner_revision,
                "interface output status owner_revision",
            )
            required_text(self.store_id, "interface output status store_id", max_bytes=128)
            nonnegative_int(
                self.logical_bytes,
                "interface output status logical_bytes",
            )
            positive_int(
                self.committed_txn_id,
                "interface output status committed_txn_id",
            )
            if self.kind is InterfaceOutputKind.TREE and not isinstance(
                self.content_ref, SnapshotRef
            ):
                raise TypeError("ready tree interface output must reference a tree.")
            if self.kind is InterfaceOutputKind.FILE and not isinstance(
                self.content_ref, BlobRef
            ):
                raise TypeError("ready file interface output must reference a blob.")

    def _has_ready_fields(self) -> bool:
        return any(
            value is not None
            for value in (
                self.session_revision,
                self.owner_revision,
                self.store_id,
                self.content_ref,
                self.logical_bytes,
                self.committed_txn_id,
            )
        )

    @property
    def record(self) -> InterfaceOutputRecord:
        return InterfaceOutputRecord(
            output_id=self.output_id,
            label=self.label,
            kind=self.kind,
            root_handle=self.root_handle,
            relative_path=self.relative_path,
        )

    @property
    def ready_generation(self) -> InterfaceOutputGenerationRecord | None:
        if self.state is not InterfaceOutputGenerationState.READY:
            return None
        assert self.session_revision is not None
        assert self.owner_revision is not None
        assert self.store_id is not None
        assert self.content_ref is not None
        assert self.logical_bytes is not None
        assert self.committed_txn_id is not None
        return InterfaceOutputGenerationRecord(
            session_id=self.session_id,
            owner_id=self.owner_id,
            session_revision=self.session_revision,
            owner_revision=self.owner_revision,
            output_id=self.output_id,
            label=self.label,
            kind=self.kind,
            record_digest=self.record_digest,
            store_id=self.store_id,
            content_ref=self.content_ref,
            logical_bytes=self.logical_bytes,
            committed_txn_id=self.committed_txn_id,
            created_at=self.updated_at,
        )

    def to_dict(self) -> dict[str, object]:
        ready = self.ready_generation
        return {
            "format": "optpilot.interface-output-generation-status.v1",
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "id": self.output_id,
            "label": self.label,
            "kind": self.kind.value,
            "record_digest": self.record_digest,
            "status": self.state.value,
            "attempt": self.attempt_number,
            "error_code": self.error_code,
            "session_revision": self.session_revision,
            "owner_revision": self.owner_revision,
            "store_id": self.store_id,
            "content_ref": None if self.content_ref is None else str(self.content_ref),
            "logical_bytes": self.logical_bytes,
            "committed_txn_id": self.committed_txn_id,
            "created_txn_id": self.created_txn_id,
            "updated_txn_id": self.updated_txn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "selection": None if ready is None else ready.selection.to_dict(),
        }

    @classmethod
    def from_row(
        cls, value: Mapping[str, Any]
    ) -> "InterfaceOutputGenerationStatusRecord":
        try:
            content_ref = value["content_ref"]
            return cls(
                session_id=value["session_id"],
                owner_id=value["owner_id"],
                output_id=value["output_id"],
                label=value["label"],
                kind=InterfaceOutputKind(value["kind"]),
                root_handle=value["root_handle"],
                relative_path=value["relative_path"],
                record_digest=value["record_digest"],
                state=InterfaceOutputGenerationState(value["state"]),
                attempt_number=int(value["attempt_number"]),
                attempt_id=value["attempt_id"],
                operation_prefix=value["operation_prefix"],
                change_id=value["change_id"],
                retention_lease_id=value["retention_lease_id"],
                attempt_expires_at=(
                    None
                    if value["attempt_expires_at"] is None
                    else float(value["attempt_expires_at"])
                ),
                error_code=value["error_code"],
                session_revision=(
                    None
                    if value["session_revision"] is None
                    else int(value["session_revision"])
                ),
                owner_revision=(
                    None
                    if value["owner_revision"] is None
                    else int(value["owner_revision"])
                ),
                store_id=value["store_id"],
                content_ref=(
                    None
                    if content_ref is None
                    else parse_physical_content_ref(content_ref)
                ),
                logical_bytes=(
                    None
                    if value["logical_bytes"] is None
                    else int(value["logical_bytes"])
                ),
                committed_txn_id=(
                    None
                    if value["committed_txn_id"] is None
                    else int(value["committed_txn_id"])
                ),
                created_txn_id=int(value["created_txn_id"]),
                updated_txn_id=int(value["updated_txn_id"]),
                created_at=float(value["created_at"]),
                updated_at=float(value["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted interface output status is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class InterfaceOutputSessionRetirementReceipt:
    session: InterfaceOutputSessionRecord
    previous_owner_revision: int
    owner_revision: int
    released_memberships: int

    def __post_init__(self) -> None:
        if self.session.state is not InterfaceOutputSessionState.RETIRED:
            raise ValueError("interface output retirement session is not retired.")
        nonnegative_int(
            self.previous_owner_revision,
            "interface output retirement previous_owner_revision",
        )
        positive_int(
            self.owner_revision,
            "interface output retirement owner_revision",
        )
        if self.owner_revision != self.previous_owner_revision + 1:
            raise ValueError("interface output retirement must advance owner revision once.")
        nonnegative_int(
            self.released_memberships,
            "interface output retirement released_memberships",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "optpilot.interface-output-session-retirement.v1",
            "session": self.session.to_dict(),
            "previous_owner_revision": self.previous_owner_revision,
            "owner_revision": self.owner_revision,
            "released_memberships": self.released_memberships,
            "session_retired": True,
        }


__all__ = [
    "INTERFACE_OUTPUT_GENERATION_SCHEMA",
    "INTERFACE_OUTPUT_SESSION_ROLE",
    "INTERFACE_OUTPUT_SESSION_SCHEMA",
    "InterfaceOutputGenerationRecord",
    "InterfaceOutputGenerationState",
    "InterfaceOutputGenerationStatusRecord",
    "InterfaceOutputSessionRecord",
    "InterfaceOutputSessionRetirementReceipt",
    "InterfaceOutputSessionState",
]
