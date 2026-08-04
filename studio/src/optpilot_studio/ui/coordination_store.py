"""Durable, typed coordination state for the local Studio product.

This database is deliberately smaller in authority than the Realm ledger.  It
does not retain content, grant execution authority, or duplicate Run evidence.
It records the product-facing links Studio needs in order to survive a browser
refresh or process restart: why a Workspace exists, which explicitly saved
Study draft it backs, which durable action a click started, and where a
Workspace's registration setup stopped.

All mutations are serialized with ``BEGIN IMMEDIATE`` and replayed by an
operation id.  An operation id may be reused only with the exact same canonical
request.  This is important at the Studio/Core boundary: after an uncertain
response, Studio can recover the intent and reuse its stable Core operation id
instead of manufacturing a second durable object.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, TypeVar

from optpilot.realm.catalog_publication import (
    CatalogPackageHead,
    canonical_catalog_paths,
)
from optpilot.realm.config import prepare_private_directory
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.refs import SnapshotRef, canonical_json_bytes, request_digest


COORDINATION_DATABASE_NAME = "studio-coordination.sqlite3"
COORDINATION_STORAGE_UNAVAILABLE_MESSAGE = (
    "Studio local storage is temporarily unavailable."
)
COORDINATION_RECORD_SCHEMA = "optpilot.studio-coordination-record.v1"
ENTITY_COORDINATE_SCHEMA = "optpilot.studio-entity-coordinate.v1"
WORKSPACE_PURPOSE_SCHEMA = "optpilot.studio-workspace-purpose.v1"
STUDY_DRAFT_SCHEMA = "optpilot.studio-study-draft.v1"
ACTION_INTENT_SCHEMA = "optpilot.studio-action-intent.v1"
REGISTRATION_CHECK_SCHEMA = "optpilot.studio-registration-check.v1"
REGISTRATION_TEST_SCHEMA = "optpilot.studio-registration-test.v1"
REGISTRATION_SETUP_DATA_SCHEMA = "optpilot.studio-registration-setup-data.v1"
REGISTRATION_SETUP_SCHEMA = "optpilot.studio-registration-setup.v1"

_CURRENT_SCHEMA_VERSION = 1
_MAX_OPERATION_ID_BYTES = 512
_MAX_OPERATION_KIND_BYTES = 128
_MAX_OPERATION_REQUEST_BYTES = 1 << 20
_MAX_OPERATION_RECEIPT_BYTES = 2 << 20
_MAX_RECORD_BYTES = 1 << 20
_MAX_ACTION_PARAMETERS_BYTES = 256 << 10
_MAX_ACTION_RECEIPT_BYTES = 512 << 10
_MAX_SUMMARY_BYTES = 256 << 10
_MAX_STUDY_DRAFTS_PER_ACTOR = 10_000
_MAX_LIST_PAGE_SIZE = 500
_KIND_RE = re.compile(r"^[a-z][a-z0-9.-]{0,127}$")
_LOWER_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class CoordinationConflict(RealmConflict):
    """A replay, optimistic revision, or state precondition conflicted."""


class CoordinationNotFound(RealmNotFound):
    """A requested Studio coordination record does not exist."""


class CoordinationIntegrityError(RealmIntegrityError):
    """Persisted Studio coordination metadata failed validation."""


class CoordinationStorageUnavailable(RealmConflict):
    """The OS-local Studio coordination store cannot currently serve requests."""


class WorkspacePurpose(str, Enum):
    USER_PROJECT = "user-project"
    STUDY_DRAFT_BACKING = "study-draft-backing"
    READ_ONLY_SUPPORT = "read-only-support"


class StudyDraftState(str, Enum):
    ACTIVE = "active"
    DISCARDED = "discarded"


class ActionState(str, Enum):
    PENDING = "pending"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RegistrationSetupState(str, Enum):
    CONFIGURING = "configuring"
    CHECK_FAILED = "check-failed"
    CHECKED = "checked"
    REGISTERED = "registered"


def _canonical_project_path(studio_root: Path) -> str:
    selected = Path(studio_root).expanduser().absolute().resolve(strict=False)
    return os.path.normcase(str(selected))


def studio_project_state_directory(
    studio_root: Path, *, authority_root: Path
) -> Path:
    """Return the deterministic OS-local state directory for one project.

    The project path is used only to derive an opaque key.  Mutable Studio
    state therefore lives under the injected local authority rather than in
    the user's project tree, while aliases that resolve to the same project
    select the same state directory.
    """

    authority = Path(authority_root).expanduser().absolute()
    project_key = request_digest(
        {
            "project_path": _canonical_project_path(studio_root),
            "schema": "optpilot.studio-project-local-state.v1",
        }
    )
    return authority / "studio" / "projects" / project_key


def coordination_database_path(
    studio_root: Path, *, authority_root: Path | None = None
) -> Path:
    """Return the coordination database for one Studio project.

    Omitting ``authority_root`` retains the legacy project-local location for
    realm-less callers.  Production callers inject their Realm authority root
    and receive an OS-local, per-project location beneath that authority.
    """

    if authority_root is None:
        root = Path(studio_root).expanduser().absolute() / ".optpilot-ui"
    else:
        root = studio_project_state_directory(
            studio_root, authority_root=authority_root
        )
    return root / COORDINATION_DATABASE_NAME


def _required_text(value: Any, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8 text.") from error
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _optional_text(
    value: Any, label: str, *, max_bytes: int = 512
) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, max_bytes=max_bytes)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _finite_time(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite timestamp.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite timestamp.")
    return result


def _lower_hex_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be a 64-character lowercase hexadecimal digest."
        )
    return value


def _kind(value: Any, label: str) -> str:
    if not isinstance(value, str) or _KIND_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical kind.")
    return value


def _portable_relative_path(value: Any, label: str) -> str:
    result = _required_text(value, label, max_bytes=4096)
    path = PurePosixPath(result)
    if (
        "\\" in result
        or path.is_absolute()
        or result != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be one canonical portable relative path.")
    return result


def _package_id(value: Any) -> str:
    result = _required_text(value, "registration package id", max_bytes=256)
    if "/" in result or "\\" in result or result.startswith((".", "~")):
        raise ValueError("registration package id must be one portable component.")
    return result


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _freeze_json(value: Any, *, label: str, depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError(f"{label} exceeds the maximum nesting depth.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = _required_text(raw_key, f"{label} key", max_bytes=256)
            frozen[key] = _freeze_json(child, label=label, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(child, label=label, depth=depth + 1) for child in value
        )
    raise ValueError(f"{label} contains a non-JSON value of type {type(value).__name__}.")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _bounded_json_object(
    value: Mapping[str, Any], *, label: str, max_bytes: int
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    frozen = _freeze_json(value, label=label)
    encoded = canonical_json_bytes(_thaw_json(frozen))
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes.")
    return frozen


def _canonical_json_text(value: Any, *, label: str, max_bytes: int) -> str:
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must contain canonical JSON values.") from error
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes.")
    return encoded.decode("utf-8")


def _load_canonical_object(value: Any, *, label: str, max_bytes: int) -> dict[str, Any]:
    if not isinstance(value, str):
        raise CoordinationIntegrityError(f"Persisted {label} is not text.")
    if len(value.encode("utf-8")) > max_bytes:
        raise CoordinationIntegrityError(f"Persisted {label} is too large.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise CoordinationIntegrityError(f"Persisted {label} is invalid JSON.") from error
    if not isinstance(parsed, dict):
        raise CoordinationIntegrityError(f"Persisted {label} is not an object.")
    if canonical_json_bytes(parsed).decode("utf-8") != value:
        raise CoordinationIntegrityError(f"Persisted {label} is not canonical JSON.")
    return parsed


@dataclass(frozen=True)
class EntityCoordinate:
    """A bounded logical pointer; it never conveys content or access authority."""

    kind: str
    entity_id: str
    revision: int | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _kind(self.kind, "entity coordinate kind"))
        object.__setattr__(
            self,
            "entity_id",
            _required_text(self.entity_id, "entity coordinate id", max_bytes=512),
        )
        if self.revision is not None:
            _nonnegative_int(self.revision, "entity coordinate revision")
        if self.digest is not None:
            _lower_hex_digest(self.digest, "entity coordinate digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "revision": self.revision,
            "schema": ENTITY_COORDINATE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EntityCoordinate":
        _exact_keys(
            payload,
            {"digest", "entity_id", "kind", "revision", "schema"},
            "entity coordinate",
        )
        if payload["schema"] != ENTITY_COORDINATE_SCHEMA:
            raise ValueError("entity coordinate schema is unsupported.")
        result = cls(
            kind=payload["kind"],
            entity_id=payload["entity_id"],
            revision=payload["revision"],
            digest=payload["digest"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("entity coordinate is not canonical.")
        return result


@dataclass(frozen=True)
class WorkspacePurposeRecord:
    workspace_id: str
    purpose: WorkspacePurpose
    subject: EntityCoordinate | None
    label: str | None
    revision: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _required_text(self.workspace_id, "workspace id", max_bytes=512)
        if not isinstance(self.purpose, WorkspacePurpose):
            raise TypeError("workspace purpose is invalid.")
        if self.subject is not None and not isinstance(self.subject, EntityCoordinate):
            raise TypeError("workspace purpose subject is invalid.")
        _optional_text(self.label, "workspace product label", max_bytes=512)
        _positive_int(self.revision, "workspace purpose revision")
        created = _finite_time(self.created_at, "workspace purpose created_at")
        updated = _finite_time(self.updated_at, "workspace purpose updated_at")
        if updated < created:
            raise ValueError("workspace purpose updated_at precedes created_at.")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "purpose": self.purpose.value,
            "subject": None if self.subject is None else self.subject.to_dict(),
            "workspace_id": self.workspace_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "created_at": self.created_at,
            "revision": self.revision,
            "schema": WORKSPACE_PURPOSE_SCHEMA,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspacePurposeRecord":
        _exact_keys(
            payload,
            {
                "created_at",
                "label",
                "purpose",
                "revision",
                "schema",
                "subject",
                "updated_at",
                "workspace_id",
            },
            "workspace purpose record",
        )
        if payload["schema"] != WORKSPACE_PURPOSE_SCHEMA:
            raise ValueError("workspace purpose schema is unsupported.")
        raw_subject = payload["subject"]
        if raw_subject is not None and not isinstance(raw_subject, Mapping):
            raise TypeError("workspace purpose subject is invalid.")
        result = cls(
            workspace_id=payload["workspace_id"],
            purpose=WorkspacePurpose(payload["purpose"]),
            subject=(
                None if raw_subject is None else EntityCoordinate.from_dict(raw_subject)
            ),
            label=payload["label"],
            revision=payload["revision"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace purpose record is not canonical.")
        return result


@dataclass(frozen=True)
class StudyDraftRecord:
    draft_id: str
    actor_id: str
    title: str
    workspace_id: str
    workspace_revision: int
    study_relative_path: str
    config_digest: str
    state: StudyDraftState
    revision: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _required_text(self.draft_id, "Study draft id", max_bytes=512)
        _required_text(self.actor_id, "Study draft actor id", max_bytes=512)
        _required_text(self.title, "Study draft title", max_bytes=512)
        _required_text(self.workspace_id, "Study draft workspace id", max_bytes=512)
        _positive_int(self.workspace_revision, "Study draft workspace revision")
        object.__setattr__(
            self,
            "study_relative_path",
            _portable_relative_path(
                self.study_relative_path, "Study draft relative path"
            ),
        )
        _lower_hex_digest(self.config_digest, "Study draft config digest")
        if not isinstance(self.state, StudyDraftState):
            raise TypeError("Study draft state is invalid.")
        _positive_int(self.revision, "Study draft revision")
        created = _finite_time(self.created_at, "Study draft created_at")
        updated = _finite_time(self.updated_at, "Study draft updated_at")
        if updated < created:
            raise ValueError("Study draft updated_at precedes created_at.")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "config_digest": self.config_digest,
            "draft_id": self.draft_id,
            "state": self.state.value,
            "study_relative_path": self.study_relative_path,
            "title": self.title,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "created_at": self.created_at,
            "revision": self.revision,
            "schema": STUDY_DRAFT_SCHEMA,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudyDraftRecord":
        _exact_keys(
            payload,
            {
                "actor_id",
                "config_digest",
                "created_at",
                "draft_id",
                "revision",
                "schema",
                "state",
                "study_relative_path",
                "title",
                "updated_at",
                "workspace_id",
                "workspace_revision",
            },
            "Study draft record",
        )
        if payload["schema"] != STUDY_DRAFT_SCHEMA:
            raise ValueError("Study draft schema is unsupported.")
        result = cls(
            draft_id=payload["draft_id"],
            actor_id=payload["actor_id"],
            title=payload["title"],
            workspace_id=payload["workspace_id"],
            workspace_revision=payload["workspace_revision"],
            study_relative_path=payload["study_relative_path"],
            config_digest=payload["config_digest"],
            state=StudyDraftState(payload["state"]),
            revision=payload["revision"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("Study draft record is not canonical.")
        return result


@dataclass(frozen=True)
class ActionIntentRecord:
    """One durable user action and its stable downstream operation identity."""

    intent_id: str
    actor_id: str
    action_kind: str
    source: EntityCoordinate
    parameters: Mapping[str, Any]
    intent_digest: str
    core_operation_id: str
    state: ActionState
    result: EntityCoordinate | None
    core_receipt: Mapping[str, Any] | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    revision: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _required_text(self.intent_id, "action intent id", max_bytes=512)
        _required_text(self.actor_id, "action actor id", max_bytes=512)
        object.__setattr__(
            self, "action_kind", _kind(self.action_kind, "action kind")
        )
        if not isinstance(self.source, EntityCoordinate):
            raise TypeError("action source is invalid.")
        parameters = _bounded_json_object(
            self.parameters,
            label="action parameters",
            max_bytes=_MAX_ACTION_PARAMETERS_BYTES,
        )
        object.__setattr__(self, "parameters", parameters)
        _lower_hex_digest(self.intent_digest, "action intent digest")
        _required_text(
            self.core_operation_id,
            "action Core operation id",
            max_bytes=_MAX_OPERATION_ID_BYTES,
        )
        if not isinstance(self.state, ActionState):
            raise TypeError("action state is invalid.")
        if self.result is not None and not isinstance(self.result, EntityCoordinate):
            raise TypeError("action result is invalid.")
        if self.core_receipt is not None:
            receipt = _bounded_json_object(
                self.core_receipt,
                label="action Core receipt",
                max_bytes=_MAX_ACTION_RECEIPT_BYTES,
            )
            object.__setattr__(self, "core_receipt", receipt)
        _optional_text(self.error_code, "action error code", max_bytes=128)
        _optional_text(
            self.error_message, "action error message", max_bytes=16 * 1024
        )
        _positive_int(self.attempt_count, "action attempt count")
        _positive_int(self.revision, "action revision")
        created = _finite_time(self.created_at, "action created_at")
        updated = _finite_time(self.updated_at, "action updated_at")
        if updated < created:
            raise ValueError("action updated_at precedes created_at.")
        expected_digest = self.compute_intent_digest(
            intent_id=self.intent_id,
            actor_id=self.actor_id,
            action_kind=self.action_kind,
            source=self.source,
            parameters=self.parameters,
        )
        if self.intent_digest != expected_digest:
            raise ValueError("action intent digest does not match its immutable request.")
        if self.state is ActionState.SUCCEEDED:
            if (
                self.result is None
                or self.error_code is not None
                or self.error_message is not None
            ):
                raise ValueError("successful action state is incomplete or contradictory.")
        elif self.state is ActionState.FAILED:
            if self.result is not None or self.core_receipt is not None:
                raise ValueError("failed action cannot claim a result or Core receipt.")
            if self.error_code is None or self.error_message is None:
                raise ValueError("failed action must retain a bounded error.")
        else:
            if self.result is not None or self.core_receipt is not None:
                raise ValueError("unfinished action cannot claim a result or Core receipt.")
            if self.error_code is not None:
                raise ValueError("unfinished action cannot claim a definite error code.")
            if self.state is ActionState.PENDING and self.error_message is not None:
                raise ValueError("pending action cannot retain an uncertainty message.")

    @staticmethod
    def compute_intent_digest(
        *,
        intent_id: str,
        actor_id: str,
        action_kind: str,
        source: EntityCoordinate,
        parameters: Mapping[str, Any],
    ) -> str:
        return request_digest(
            {
                "action_kind": action_kind,
                "actor_id": actor_id,
                "intent_id": intent_id,
                "parameters": _thaw_json(parameters),
                "schema": "optpilot.studio-action-request.v1",
                "source": source.to_dict(),
            }
        )

    @staticmethod
    def default_core_operation_id(*, actor_id: str, intent_id: str) -> str:
        identity = request_digest(
            {
                "actor_id": actor_id,
                "intent_id": intent_id,
                "schema": "optpilot.studio-action-operation.v1",
            }
        )
        return f"studio/action/v1/{identity}"

    def immutable_dict(self) -> dict[str, Any]:
        return {
            "action_kind": self.action_kind,
            "actor_id": self.actor_id,
            "core_operation_id": self.core_operation_id,
            "intent_digest": self.intent_digest,
            "intent_id": self.intent_id,
            "parameters": _thaw_json(self.parameters),
            "source": self.source.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.immutable_dict(),
            "attempt_count": self.attempt_count,
            "core_receipt": (
                None if self.core_receipt is None else _thaw_json(self.core_receipt)
            ),
            "created_at": self.created_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "result": None if self.result is None else self.result.to_dict(),
            "revision": self.revision,
            "schema": ACTION_INTENT_SCHEMA,
            "state": self.state.value,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionIntentRecord":
        _exact_keys(
            payload,
            {
                "action_kind",
                "actor_id",
                "attempt_count",
                "core_operation_id",
                "core_receipt",
                "created_at",
                "error_code",
                "error_message",
                "intent_digest",
                "intent_id",
                "parameters",
                "result",
                "revision",
                "schema",
                "source",
                "state",
                "updated_at",
            },
            "action intent record",
        )
        if payload["schema"] != ACTION_INTENT_SCHEMA:
            raise ValueError("action intent schema is unsupported.")
        if not isinstance(payload["source"], Mapping):
            raise TypeError("action source is invalid.")
        if not isinstance(payload["parameters"], Mapping):
            raise TypeError("action parameters are invalid.")
        raw_result = payload["result"]
        raw_receipt = payload["core_receipt"]
        if raw_result is not None and not isinstance(raw_result, Mapping):
            raise TypeError("action result is invalid.")
        if raw_receipt is not None and not isinstance(raw_receipt, Mapping):
            raise TypeError("action Core receipt is invalid.")
        result = cls(
            intent_id=payload["intent_id"],
            actor_id=payload["actor_id"],
            action_kind=payload["action_kind"],
            source=EntityCoordinate.from_dict(payload["source"]),
            parameters=payload["parameters"],
            intent_digest=payload["intent_digest"],
            core_operation_id=payload["core_operation_id"],
            state=ActionState(payload["state"]),
            result=(
                None if raw_result is None else EntityCoordinate.from_dict(raw_result)
            ),
            core_receipt=raw_receipt,
            error_code=payload["error_code"],
            error_message=payload["error_message"],
            attempt_count=payload["attempt_count"],
            revision=payload["revision"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("action intent record is not canonical.")
        return result


@dataclass(frozen=True)
class RegistrationCheck:
    workspace_revision: int
    store_id: str
    artifact_ref: SnapshotRef
    owned_paths: tuple[str, ...]
    accepted: bool
    validation_digest: str
    summary: Mapping[str, Any]
    checked_at: float

    def __post_init__(self) -> None:
        _positive_int(self.workspace_revision, "checked Workspace revision")
        _required_text(self.store_id, "checked artifact store id", max_bytes=128)
        if not isinstance(self.artifact_ref, SnapshotRef):
            raise TypeError("checked artifact ref must be a SnapshotRef.")
        paths = canonical_catalog_paths(tuple(self.owned_paths))
        object.__setattr__(self, "owned_paths", paths)
        if not isinstance(self.accepted, bool):
            raise TypeError("registration check accepted must be a boolean.")
        _lower_hex_digest(self.validation_digest, "registration validation digest")
        summary = _bounded_json_object(
            self.summary,
            label="registration check summary",
            max_bytes=_MAX_SUMMARY_BYTES,
        )
        object.__setattr__(self, "summary", summary)
        _finite_time(self.checked_at, "registration checked_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "artifact_ref": str(self.artifact_ref),
            "checked_at": self.checked_at,
            "owned_paths": list(self.owned_paths),
            "schema": REGISTRATION_CHECK_SCHEMA,
            "store_id": self.store_id,
            "summary": _thaw_json(self.summary),
            "validation_digest": self.validation_digest,
            "workspace_revision": self.workspace_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegistrationCheck":
        _exact_keys(
            payload,
            {
                "accepted",
                "artifact_ref",
                "checked_at",
                "owned_paths",
                "schema",
                "store_id",
                "summary",
                "validation_digest",
                "workspace_revision",
            },
            "registration check",
        )
        if payload["schema"] != REGISTRATION_CHECK_SCHEMA:
            raise ValueError("registration check schema is unsupported.")
        if not isinstance(payload["owned_paths"], list) or not isinstance(
            payload["summary"], Mapping
        ):
            raise TypeError("registration check collections are invalid.")
        result = cls(
            workspace_revision=payload["workspace_revision"],
            store_id=payload["store_id"],
            artifact_ref=SnapshotRef.parse(payload["artifact_ref"]),
            owned_paths=tuple(payload["owned_paths"]),
            accepted=payload["accepted"],
            validation_digest=payload["validation_digest"],
            summary=payload["summary"],
            checked_at=payload["checked_at"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("registration check is not canonical.")
        return result


@dataclass(frozen=True)
class RegistrationTestResult:
    accepted: bool
    result_digest: str
    summary: Mapping[str, Any]
    tested_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("registration test accepted must be a boolean.")
        _lower_hex_digest(self.result_digest, "registration test result digest")
        summary = _bounded_json_object(
            self.summary,
            label="registration test summary",
            max_bytes=_MAX_SUMMARY_BYTES,
        )
        object.__setattr__(self, "summary", summary)
        _finite_time(self.tested_at, "registration tested_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "result_digest": self.result_digest,
            "schema": REGISTRATION_TEST_SCHEMA,
            "summary": _thaw_json(self.summary),
            "tested_at": self.tested_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegistrationTestResult":
        _exact_keys(
            payload,
            {"accepted", "result_digest", "schema", "summary", "tested_at"},
            "registration test result",
        )
        if payload["schema"] != REGISTRATION_TEST_SCHEMA:
            raise ValueError("registration test schema is unsupported.")
        if not isinstance(payload["summary"], Mapping):
            raise TypeError("registration test summary is invalid.")
        result = cls(
            accepted=payload["accepted"],
            result_digest=payload["result_digest"],
            summary=payload["summary"],
            tested_at=payload["tested_at"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("registration test result is not canonical.")
        return result


@dataclass(frozen=True)
class RegistrationSetupData:
    """The resumable product state for one Workspace registration checklist."""

    state: RegistrationSetupState = RegistrationSetupState.CONFIGURING
    package_id: str | None = None
    catalog_roles: tuple[str, ...] = ()
    publisher_id: str | None = None
    source_lineage: EntityCoordinate | None = None
    check: RegistrationCheck | None = None
    test: RegistrationTestResult | None = None
    expected_catalog_head: CatalogPackageHead | None = None
    publication_intent_id: str | None = None
    publication_result: EntityCoordinate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, RegistrationSetupState):
            raise TypeError("registration setup state is invalid.")
        if self.package_id is not None:
            object.__setattr__(self, "package_id", _package_id(self.package_id))
        roles = tuple(self.catalog_roles)
        allowed_roles = {"environment", "method", "resource", "study"}
        if (
            any(role not in allowed_roles for role in roles)
            or roles != tuple(sorted(set(roles), key=lambda item: item.encode("utf-8")))
        ):
            raise ValueError("registration Catalog roles are not canonical.")
        object.__setattr__(self, "catalog_roles", roles)
        _optional_text(
            self.publisher_id, "registration publisher id", max_bytes=512
        )
        if self.source_lineage is not None and not isinstance(
            self.source_lineage, EntityCoordinate
        ):
            raise TypeError("registration source lineage is invalid.")
        if (self.publisher_id is None) != (self.source_lineage is None):
            raise ValueError(
                "registration publisher and source lineage must be recorded together."
            )
        if self.check is not None and not isinstance(self.check, RegistrationCheck):
            raise TypeError("registration check is invalid.")
        if self.test is not None and not isinstance(self.test, RegistrationTestResult):
            raise TypeError("registration test is invalid.")
        if self.expected_catalog_head is not None:
            if not isinstance(self.expected_catalog_head, CatalogPackageHead):
                raise TypeError("registration expected Catalog head is invalid.")
            if self.package_id != self.expected_catalog_head.package_id:
                raise ValueError("registration expected head belongs to another package.")
        _optional_text(
            self.publication_intent_id,
            "registration publication intent id",
            max_bytes=512,
        )
        if self.publication_result is not None and not isinstance(
            self.publication_result, EntityCoordinate
        ):
            raise TypeError("registration publication result is invalid.")

        configured_identity = (
            self.package_id is not None
            and self.publisher_id is not None
            and self.source_lineage is not None
        )
        if self.state is RegistrationSetupState.CONFIGURING:
            if any(
                item is not None
                for item in (
                    self.check,
                    self.test,
                    self.publication_intent_id,
                    self.publication_result,
                )
            ):
                raise ValueError("configuring registration setup cannot claim later evidence.")
        else:
            identity_complete = configured_identity and (
                self.state is RegistrationSetupState.CHECK_FAILED
                or bool(self.catalog_roles)
            )
            if not identity_complete or self.check is None:
                raise ValueError("checked registration setup lacks its exact identity.")
            if self.state is RegistrationSetupState.CHECK_FAILED:
                if self.check.accepted:
                    raise ValueError("failed registration check cannot be accepted.")
                if any(
                    item is not None
                    for item in (
                        self.test,
                        self.publication_intent_id,
                        self.publication_result,
                    )
                ):
                    raise ValueError("failed registration check cannot claim later evidence.")
            else:
                if not self.check.accepted:
                    raise ValueError("checked registration setup requires accepted evidence.")
                if self.publication_intent_id is None and self.publication_result is not None:
                    raise ValueError("registration result lacks its publication intent.")
                if (
                    self.publication_intent_id is not None
                    and self.test is not None
                    and not self.test.accepted
                ):
                    raise ValueError(
                        "registration publication cannot follow a failed Test."
                    )
                if self.state is RegistrationSetupState.CHECKED:
                    if self.publication_result is not None:
                        raise ValueError("checked setup cannot claim a published result.")
                elif self.state is RegistrationSetupState.REGISTERED:
                    if (
                        self.publication_intent_id is None
                        or self.publication_result is None
                    ):
                        raise ValueError("registered setup lacks its publication receipt link.")
                    if self.test is not None and not self.test.accepted:
                        raise ValueError("registered setup cannot retain a failed test result.")
        _canonical_json_text(
            self.to_dict(), label="registration setup data", max_bytes=_MAX_RECORD_BYTES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_roles": list(self.catalog_roles),
            "check": None if self.check is None else self.check.to_dict(),
            "expected_catalog_head": (
                None
                if self.expected_catalog_head is None
                else self.expected_catalog_head.to_dict()
            ),
            "package_id": self.package_id,
            "publication_intent_id": self.publication_intent_id,
            "publication_result": (
                None
                if self.publication_result is None
                else self.publication_result.to_dict()
            ),
            "publisher_id": self.publisher_id,
            "schema": REGISTRATION_SETUP_DATA_SCHEMA,
            "source_lineage": (
                None if self.source_lineage is None else self.source_lineage.to_dict()
            ),
            "state": self.state.value,
            "test": None if self.test is None else self.test.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegistrationSetupData":
        _exact_keys(
            payload,
            {
                "catalog_roles",
                "check",
                "expected_catalog_head",
                "package_id",
                "publication_intent_id",
                "publication_result",
                "publisher_id",
                "schema",
                "source_lineage",
                "state",
                "test",
            },
            "registration setup data",
        )
        if payload["schema"] != REGISTRATION_SETUP_DATA_SCHEMA:
            raise ValueError("registration setup data schema is unsupported.")
        if not isinstance(payload["catalog_roles"], list):
            raise TypeError("registration Catalog roles are invalid.")
        raw_check = payload["check"]
        raw_test = payload["test"]
        raw_head = payload["expected_catalog_head"]
        raw_lineage = payload["source_lineage"]
        raw_result = payload["publication_result"]
        for value, label in (
            (raw_check, "check"),
            (raw_test, "test"),
            (raw_head, "expected Catalog head"),
            (raw_lineage, "source lineage"),
            (raw_result, "publication result"),
        ):
            if value is not None and not isinstance(value, Mapping):
                raise TypeError(f"registration {label} is invalid.")
        result = cls(
            state=RegistrationSetupState(payload["state"]),
            package_id=payload["package_id"],
            catalog_roles=tuple(payload["catalog_roles"]),
            publisher_id=payload["publisher_id"],
            source_lineage=(
                None if raw_lineage is None else EntityCoordinate.from_dict(raw_lineage)
            ),
            check=(
                None if raw_check is None else RegistrationCheck.from_dict(raw_check)
            ),
            test=(
                None if raw_test is None else RegistrationTestResult.from_dict(raw_test)
            ),
            expected_catalog_head=(
                None if raw_head is None else CatalogPackageHead.from_dict(raw_head)
            ),
            publication_intent_id=payload["publication_intent_id"],
            publication_result=(
                None if raw_result is None else EntityCoordinate.from_dict(raw_result)
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("registration setup data is not canonical.")
        return result


@dataclass(frozen=True)
class RegistrationSetupRecord:
    setup_id: str
    actor_id: str
    workspace_id: str
    data: RegistrationSetupData
    revision: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _required_text(self.setup_id, "registration setup id", max_bytes=512)
        _required_text(self.actor_id, "registration setup actor id", max_bytes=512)
        _required_text(
            self.workspace_id, "registration setup Workspace id", max_bytes=512
        )
        if not isinstance(self.data, RegistrationSetupData):
            raise TypeError("registration setup data is invalid.")
        _positive_int(self.revision, "registration setup revision")
        created = _finite_time(self.created_at, "registration setup created_at")
        updated = _finite_time(self.updated_at, "registration setup updated_at")
        if updated < created:
            raise ValueError("registration setup updated_at precedes created_at.")

    @staticmethod
    def stable_id(*, actor_id: str, workspace_id: str) -> str:
        digest = request_digest(
            {
                "actor_id": actor_id,
                "schema": "optpilot.studio-registration-setup-identity.v1",
                "workspace_id": workspace_id,
            }
        )
        return f"setup_{digest}"

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "data": self.data.to_dict(),
            "setup_id": self.setup_id,
            "workspace_id": self.workspace_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "created_at": self.created_at,
            "revision": self.revision,
            "schema": REGISTRATION_SETUP_SCHEMA,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegistrationSetupRecord":
        _exact_keys(
            payload,
            {
                "actor_id",
                "created_at",
                "data",
                "revision",
                "schema",
                "setup_id",
                "updated_at",
                "workspace_id",
            },
            "registration setup record",
        )
        if payload["schema"] != REGISTRATION_SETUP_SCHEMA:
            raise ValueError("registration setup schema is unsupported.")
        if not isinstance(payload["data"], Mapping):
            raise TypeError("registration setup data is invalid.")
        result = cls(
            setup_id=payload["setup_id"],
            actor_id=payload["actor_id"],
            workspace_id=payload["workspace_id"],
            data=RegistrationSetupData.from_dict(payload["data"]),
            revision=payload["revision"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )
        if result.setup_id != cls.stable_id(
            actor_id=result.actor_id, workspace_id=result.workspace_id
        ):
            raise ValueError("registration setup id is not derived from its Workspace.")
        if result.to_dict() != dict(payload):
            raise ValueError("registration setup record is not canonical.")
        return result


_SCHEMA_V1 = r"""
CREATE TABLE coordination_schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_digest TEXT NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE coordination_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE coordination_operations (
    operation_id TEXT PRIMARY KEY CHECK(
        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 512
        AND operation_id = trim(operation_id)
    ),
    operation_kind TEXT NOT NULL CHECK(
        length(CAST(operation_kind AS BLOB)) BETWEEN 1 AND 128
        AND operation_kind = trim(operation_kind)
    ),
    request_digest TEXT NOT NULL CHECK(
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_json TEXT NOT NULL CHECK(
        length(CAST(receipt_json AS BLOB)) BETWEEN 2 AND 2097152
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
        AND receipt_json = json(receipt_json)
    ),
    committed_at REAL NOT NULL
);

CREATE TABLE workspace_purpose_records (
    workspace_id TEXT PRIMARY KEY CHECK(
        length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 512
        AND workspace_id = trim(workspace_id)
    ),
    purpose TEXT NOT NULL CHECK(
        purpose IN ('user-project', 'study-draft-backing', 'read-only-support')
    ),
    revision INTEGER NOT NULL CHECK(revision > 0),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    record_json TEXT NOT NULL CHECK(
        length(CAST(record_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND record_json = json(record_json)
        AND json_extract(record_json, '$.schema') IS
            'optpilot.studio-workspace-purpose.v1'
        AND json_extract(record_json, '$.workspace_id') IS workspace_id
        AND json_extract(record_json, '$.purpose') IS purpose
        AND json_extract(record_json, '$.revision') IS revision
        AND record_digest = studio_request_digest(record_json)
    ),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX workspace_purpose_kind_updated
ON workspace_purpose_records(purpose, updated_at DESC, workspace_id);

CREATE TABLE study_draft_records (
    draft_id TEXT PRIMARY KEY CHECK(
        length(CAST(draft_id AS BLOB)) BETWEEN 1 AND 512
        AND draft_id = trim(draft_id)
    ),
    actor_id TEXT NOT NULL CHECK(
        length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 512
        AND actor_id = trim(actor_id)
    ),
    workspace_id TEXT NOT NULL UNIQUE REFERENCES workspace_purpose_records(workspace_id),
    state TEXT NOT NULL CHECK(state IN ('active', 'discarded')),
    revision INTEGER NOT NULL CHECK(revision > 0),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    record_json TEXT NOT NULL CHECK(
        length(CAST(record_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND record_json = json(record_json)
        AND json_extract(record_json, '$.schema') IS
            'optpilot.studio-study-draft.v1'
        AND json_extract(record_json, '$.draft_id') IS draft_id
        AND json_extract(record_json, '$.actor_id') IS actor_id
        AND json_extract(record_json, '$.workspace_id') IS workspace_id
        AND json_extract(record_json, '$.state') IS state
        AND json_extract(record_json, '$.revision') IS revision
        AND record_digest = studio_request_digest(record_json)
    ),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX study_drafts_actor_state_updated
ON study_draft_records(actor_id, state, updated_at DESC, draft_id);

CREATE TABLE action_intent_records (
    intent_id TEXT PRIMARY KEY CHECK(
        length(CAST(intent_id AS BLOB)) BETWEEN 1 AND 512
        AND intent_id = trim(intent_id)
    ),
    actor_id TEXT NOT NULL CHECK(
        length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 512
        AND actor_id = trim(actor_id)
    ),
    action_kind TEXT NOT NULL CHECK(
        length(CAST(action_kind AS BLOB)) BETWEEN 1 AND 128
        AND action_kind = trim(action_kind)
    ),
    intent_digest TEXT NOT NULL CHECK(
        length(intent_digest) = 64
        AND intent_digest NOT GLOB '*[^0-9a-f]*'
    ),
    core_operation_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(core_operation_id AS BLOB)) BETWEEN 1 AND 512
        AND core_operation_id = trim(core_operation_id)
    ),
    state TEXT NOT NULL CHECK(
        state IN ('pending', 'uncertain', 'succeeded', 'failed')
    ),
    revision INTEGER NOT NULL CHECK(revision > 0),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    record_json TEXT NOT NULL CHECK(
        length(CAST(record_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND record_json = json(record_json)
        AND json_extract(record_json, '$.schema') IS
            'optpilot.studio-action-intent.v1'
        AND json_extract(record_json, '$.intent_id') IS intent_id
        AND json_extract(record_json, '$.actor_id') IS actor_id
        AND json_extract(record_json, '$.action_kind') IS action_kind
        AND json_extract(record_json, '$.intent_digest') IS intent_digest
        AND json_extract(record_json, '$.core_operation_id') IS core_operation_id
        AND json_extract(record_json, '$.state') IS state
        AND json_extract(record_json, '$.revision') IS revision
        AND record_digest = studio_request_digest(record_json)
    ),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX action_intents_actor_state_updated
ON action_intent_records(actor_id, state, updated_at DESC, intent_id);
CREATE INDEX action_intents_actor_kind_updated
ON action_intent_records(actor_id, action_kind, updated_at DESC, intent_id);

CREATE TABLE registration_setup_records (
    setup_id TEXT PRIMARY KEY CHECK(
        length(CAST(setup_id AS BLOB)) BETWEEN 1 AND 512
        AND setup_id = trim(setup_id)
    ),
    actor_id TEXT NOT NULL CHECK(
        length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 512
        AND actor_id = trim(actor_id)
    ),
    workspace_id TEXT NOT NULL REFERENCES workspace_purpose_records(workspace_id),
    state TEXT NOT NULL CHECK(
        state IN ('configuring', 'check-failed', 'checked', 'registered')
    ),
    revision INTEGER NOT NULL CHECK(revision > 0),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    record_json TEXT NOT NULL CHECK(
        length(CAST(record_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND record_json = json(record_json)
        AND json_extract(record_json, '$.schema') IS
            'optpilot.studio-registration-setup.v1'
        AND json_extract(record_json, '$.setup_id') IS setup_id
        AND json_extract(record_json, '$.actor_id') IS actor_id
        AND json_extract(record_json, '$.workspace_id') IS workspace_id
        AND json_extract(record_json, '$.data.state') IS state
        AND json_extract(record_json, '$.revision') IS revision
        AND record_digest = studio_request_digest(record_json)
    ),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(actor_id, workspace_id)
);

CREATE INDEX registration_setups_actor_updated
ON registration_setup_records(actor_id, updated_at DESC, setup_id);
"""

_MIGRATIONS = ((_CURRENT_SCHEMA_VERSION, _SCHEMA_V1),)


def _sqlite_request_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
        if canonical_json_bytes(parsed).decode("utf-8") != value:
            return None
        return request_digest(parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _storage_unavailable(_error: BaseException) -> CoordinationStorageUnavailable:
    return CoordinationStorageUnavailable(COORDINATION_STORAGE_UNAVAILABLE_MESSAGE)


def _validate_coordination_database(
    connection: sqlite3.Connection,
) -> str:
    """Validate a complete coordination database and return its identity."""

    quick_check = tuple(
        str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
    )
    if quick_check != ("ok",):
        raise CoordinationIntegrityError(
            "Studio coordination database failed its integrity check."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise CoordinationIntegrityError(
            "Studio coordination database has invalid foreign-key references."
        )
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if not (1 <= version <= _CURRENT_SCHEMA_VERSION):
        raise CoordinationIntegrityError(
            "Studio coordination database has an unsupported schema version."
        )
    table_names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required_tables = {"coordination_schema_migrations", "coordination_meta"}
    if not required_tables.issubset(table_names):
        raise CoordinationIntegrityError(
            "Studio coordination database has no valid schema history."
        )
    rows = connection.execute(
        "SELECT version, migration_digest FROM coordination_schema_migrations "
        "ORDER BY version"
    ).fetchall()
    if tuple(int(row[0]) for row in rows) != tuple(range(1, version + 1)):
        raise CoordinationIntegrityError(
            "Studio coordination migration history is incomplete."
        )
    for migration_version, script in _MIGRATIONS[:version]:
        expected = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if str(rows[migration_version - 1][1]) != expected:
            raise CoordinationIntegrityError(
                f"Studio coordination migration {migration_version} changed."
            )
    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT key, value FROM coordination_meta WHERE key IN "
            "('instance_id', 'schema_version')"
        ).fetchall()
    }
    if metadata.get("schema_version") != str(version):
        raise CoordinationIntegrityError(
            "Studio coordination schema metadata is inconsistent."
        )
    try:
        return _lower_hex_digest(
            metadata.get("instance_id"), "coordination instance id"
        )
    except ValueError as error:
        raise CoordinationIntegrityError(
            "Studio coordination identity is missing or invalid."
        ) from error


def _connect_read_only_database(path: Path) -> sqlite3.Connection:
    uri = f"{Path(path).expanduser().absolute().as_uri()}?mode=ro&nofollow=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "studio_request_digest", 1, _sqlite_request_digest, deterministic=True
    )
    return connection


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows has no directory fsync
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_migration_temporary_files(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-shm"),
        Path(f"{path}-wal"),
    ):
        try:
            candidate.unlink()
        except OSError:
            # Cleanup is best-effort.  In particular, do not let a secondary
            # unlink failure mask the stable typed migration outcome.
            pass


def prepare_coordination_database(
    studio_root: Path,
    *,
    authority_root: Path,
    legacy_path: Path | None = None,
) -> Path:
    """Prepare the OS-local database, safely adopting legacy state once.

    An existing target always wins.  Otherwise a valid legacy SQLite database
    is copied through SQLite's online-backup API, which includes committed WAL
    state.  Promotion is atomic and non-overwriting, and the legacy database is
    never renamed, truncated, or deleted.
    """

    target = coordination_database_path(
        studio_root, authority_root=authority_root
    )
    try:
        target_exists = target.exists()
    except OSError as error:
        raise _storage_unavailable(error) from error
    if target_exists:
        return target
    source_path = (
        coordination_database_path(studio_root)
        if legacy_path is None
        else Path(legacy_path).expanduser().absolute()
    )
    try:
        prepared_parent = prepare_private_directory(target.parent)
    except OSError as error:
        raise _storage_unavailable(error) from error
    target = prepared_parent / target.name
    try:
        target_exists = target.exists()
        source_exists = source_path.exists()
    except OSError as error:
        raise _storage_unavailable(error) from error
    if target_exists or not source_exists:
        return target
    temporary = target.with_name(
        f".{target.name}.migrate-{secrets.token_hex(16)}.tmp"
    )
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = None
        source = _connect_read_only_database(source_path)
        source_identity = _validate_coordination_database(source)
        destination = sqlite3.connect(
            temporary, isolation_level=None, timeout=10.0
        )
        destination.row_factory = sqlite3.Row
        destination.create_function(
            "studio_request_digest", 1, _sqlite_request_digest, deterministic=True
        )
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        destination.execute("PRAGMA journal_mode = DELETE")
        destination.close()
        destination = None
        source.close()
        source = None
        copied = _connect_read_only_database(temporary)
        try:
            copied_identity = _validate_coordination_database(copied)
        finally:
            copied.close()
        if copied_identity != source_identity:
            raise CoordinationIntegrityError(
                "Copied Studio coordination identity did not match its source."
            )
        _fsync_file(temporary)
        try:
            os.link(temporary, target)
        except FileExistsError:
            return target
        _fsync_directory(prepared_parent)
        temporary.unlink()
        _fsync_directory(prepared_parent)
        return target
    except sqlite3.OperationalError as error:
        raise _storage_unavailable(error) from error
    except sqlite3.DatabaseError as error:
        raise CoordinationIntegrityError(
            "Legacy Studio coordination database could not be migrated."
        ) from error
    except OSError as error:
        raise _storage_unavailable(error) from error
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if descriptor is not None:
            os.close(descriptor)
        _remove_migration_temporary_files(temporary)


RecordT = TypeVar("RecordT")


class StudioCoordinationStore:
    """Single local SQLite authority for Studio-only product coordination."""

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer.")
        if not callable(clock):
            raise TypeError("coordination clock must be callable.")
        selected = Path(database_path).expanduser().absolute()
        if selected.exists() and selected.is_dir():
            selected = selected / COORDINATION_DATABASE_NAME
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self.root = prepare_private_directory(selected.parent)
        self.database_path = self.root / selected.name
        self._root_fd: int | None = None
        self._database_fd: int | None = None
        self._instance_id: str | None = None
        self._pin_authority_files()
        try:
            connection = self._connect()
            try:
                mode = str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                ).lower()
                if mode != "wal":
                    raise CoordinationIntegrityError(
                        f"Studio coordination store requires WAL mode, got {mode!r}."
                    )
                connection.execute("PRAGMA synchronous = FULL")
                self._migrate(connection)
                row = connection.execute(
                    "SELECT value FROM coordination_meta WHERE key = 'instance_id'"
                ).fetchone()
                if row is None:
                    raise CoordinationIntegrityError(
                        "Studio coordination identity is missing after migration."
                    )
                self._instance_id = _required_text(
                    row["value"], "coordination instance id", max_bytes=128
                )
            except sqlite3.OperationalError as error:
                raise _storage_unavailable(error) from error
            except sqlite3.DatabaseError as error:
                raise CoordinationIntegrityError(
                    "Studio coordination database could not be initialized."
                ) from error
            finally:
                connection.close()
            self._assert_authority_path()
        except BaseException:
            self.close()
            raise

    @property
    def instance_id(self) -> str:
        self._assert_authority_path()
        if self._instance_id is None:  # pragma: no cover - construction invariant
            raise CoordinationIntegrityError("Studio coordination identity is absent.")
        return self._instance_id

    def __enter__(self) -> "StudioCoordinationStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        for attribute in ("_database_fd", "_root_fd"):
            descriptor = getattr(self, attribute, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)

    def _pin_authority_files(self) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        database_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        database_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(self.root, directory_flags)
            self._database_fd = os.open(self.database_path, database_flags, 0o600)
        except OSError as error:
            self.close()
            raise CoordinationIntegrityError(
                "Could not safely pin Studio coordination files."
            ) from error
        root_info = os.fstat(self._root_fd)
        database_info = os.fstat(self._database_fd)
        if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISREG(database_info.st_mode):
            self.close()
            raise CoordinationIntegrityError(
                "Studio coordination paths have unsafe filesystem types."
            )
        if database_info.st_nlink != 1:
            self.close()
            raise CoordinationIntegrityError(
                "Studio coordination database must have exactly one link."
            )
        if os.name != "nt":
            os.fchmod(self._root_fd, 0o700)
            os.fchmod(self._database_fd, 0o600)
            os.fsync(self._database_fd)
            os.fsync(self._root_fd)
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._database_identity = (database_info.st_dev, database_info.st_ino)

    def _assert_authority_path(self) -> None:
        if self._root_fd is None or self._database_fd is None:
            raise CoordinationIntegrityError("Studio coordination store is closed.")
        pinned_root = os.fstat(self._root_fd)
        pinned_database = os.fstat(self._database_fd)
        try:
            path_root = os.stat(self.root, follow_symlinks=False)
            path_database = os.stat(self.database_path, follow_symlinks=False)
        except OSError as error:
            raise CoordinationIntegrityError(
                "Studio coordination path was removed or replaced."
            ) from error
        if (
            not stat.S_ISDIR(path_root.st_mode)
            or (path_root.st_dev, path_root.st_ino) != self._root_identity
            or (pinned_root.st_dev, pinned_root.st_ino) != self._root_identity
            or not stat.S_ISREG(path_database.st_mode)
            or (path_database.st_dev, path_database.st_ino) != self._database_identity
            or (pinned_database.st_dev, pinned_database.st_ino)
            != self._database_identity
            or path_database.st_nlink != 1
            or pinned_database.st_nlink != 1
        ):
            raise CoordinationIntegrityError(
                "Studio coordination path identity changed."
            )

    def _connect(self) -> sqlite3.Connection:
        self._assert_authority_path()
        uri = f"{self.database_path.as_uri()}?mode=rwc&nofollow=1"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                isolation_level=None,
                timeout=self.busy_timeout_ms / 1000,
            )
            connection.row_factory = sqlite3.Row
            connection.create_function(
                "studio_request_digest", 1, _sqlite_request_digest, deterministic=True
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
            connection.execute("PRAGMA synchronous = FULL")
            self._assert_authority_path()
            if self._instance_id is not None:
                row = connection.execute(
                    "SELECT value FROM coordination_meta WHERE key = 'instance_id'"
                ).fetchone()
                if row is None or row["value"] != self._instance_id:
                    raise CoordinationIntegrityError(
                        "Connected coordination database has another identity."
                    )
            return connection
        except sqlite3.OperationalError as error:
            if connection is not None:
                connection.close()
            raise _storage_unavailable(error) from error
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _CURRENT_SCHEMA_VERSION:
                raise CoordinationIntegrityError(
                    f"Studio coordination schema {version} is newer than supported."
                )
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if existing:
                    raise CoordinationIntegrityError(
                        "Refusing to migrate an unversioned non-empty coordination database."
                    )
            else:
                rows = connection.execute(
                    "SELECT version, migration_digest FROM "
                    "coordination_schema_migrations ORDER BY version"
                ).fetchall()
                if tuple(int(row["version"]) for row in rows) != tuple(
                    range(1, version + 1)
                ):
                    raise CoordinationIntegrityError(
                        "Studio coordination migration history is incomplete."
                    )
                for migration_version, script in _MIGRATIONS[:version]:
                    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
                    if str(rows[migration_version - 1]["migration_digest"]) != digest:
                        raise CoordinationIntegrityError(
                            f"Studio coordination migration {migration_version} changed."
                        )
            for migration_version, script in _MIGRATIONS[version:]:
                for statement in script.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                now = self._now()
                digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO coordination_schema_migrations("
                    "version, migration_digest, applied_at) VALUES (?, ?, ?)",
                    (migration_version, digest, now),
                )
                if migration_version == 1:
                    identity = request_digest(
                        {
                            "created_at": now,
                            "database_path": str(self.database_path),
                            "entropy": os.urandom(32).hex(),
                            "schema": "optpilot.studio-coordination-instance.v1",
                        }
                    )
                    connection.executemany(
                        "INSERT INTO coordination_meta(key, value) VALUES (?, ?)",
                        (
                            ("instance_id", identity),
                            ("schema_version", "1"),
                        ),
                    )
                else:  # pragma: no cover - exercised by future migrations
                    updated = connection.execute(
                        "UPDATE coordination_meta SET value = ? "
                        "WHERE key = 'schema_version'",
                        (str(migration_version),),
                    )
                    if updated.rowcount != 1:
                        raise CoordinationIntegrityError(
                            "Studio coordination schema metadata is missing."
                        )
                connection.execute(f"PRAGMA user_version = {migration_version}")
            meta = connection.execute(
                "SELECT value FROM coordination_meta WHERE key = 'schema_version'"
            ).fetchone()
            if meta is None or meta["value"] != str(_CURRENT_SCHEMA_VERSION):
                raise CoordinationIntegrityError(
                    "Studio coordination schema metadata is inconsistent."
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _now(self) -> float:
        return _finite_time(self._clock(), "coordination clock")

    def _operate(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        request: Mapping[str, Any],
        body: Callable[[sqlite3.Connection, float], Mapping[str, Any]],
    ) -> dict[str, Any]:
        operation_id = _required_text(
            operation_id, "coordination operation id", max_bytes=_MAX_OPERATION_ID_BYTES
        )
        operation_kind = _kind(operation_kind, "coordination operation kind")
        request_value = {"kind": operation_kind, "request": dict(request)}
        _canonical_json_text(
            request_value,
            label="coordination operation request",
            max_bytes=_MAX_OPERATION_REQUEST_BYTES,
        )
        digest = request_digest(request_value)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT operation_kind, request_digest, receipt_json "
                "FROM coordination_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["operation_kind"] != operation_kind
                    or existing["request_digest"] != digest
                ):
                    raise CoordinationConflict(
                        "Coordination operation id was reused for another request."
                    )
                receipt = _load_canonical_object(
                    existing["receipt_json"],
                    label="coordination operation receipt",
                    max_bytes=_MAX_OPERATION_RECEIPT_BYTES,
                )
                if receipt.get("receipt_version") != 1:
                    raise CoordinationIntegrityError(
                        "Coordination operation receipt version is unsupported."
                    )
                connection.commit()
                return receipt
            now = self._now()
            connection.execute(
                "INSERT INTO coordination_operations("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, ?, ?, '{}', ?)",
                (operation_id, operation_kind, digest, now),
            )
            receipt = dict(body(connection, now))
            receipt.setdefault("receipt_version", 1)
            receipt_json = _canonical_json_text(
                receipt,
                label="coordination operation receipt",
                max_bytes=_MAX_OPERATION_RECEIPT_BYTES,
            )
            connection.execute(
                "UPDATE coordination_operations SET receipt_json = ? "
                "WHERE operation_id = ?",
                (receipt_json, operation_id),
            )
            connection.commit()
            return receipt
        except sqlite3.OperationalError as error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise _storage_unavailable(error) from error
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _record_receipt(record: Any) -> dict[str, Any]:
        return {"receipt_version": 1, "record": record.to_dict()}

    @staticmethod
    def _decode_receipt(
        receipt: Mapping[str, Any], parser: Callable[[Mapping[str, Any]], RecordT]
    ) -> RecordT:
        if set(receipt) != {"receipt_version", "record"} or receipt.get(
            "receipt_version"
        ) != 1:
            raise CoordinationIntegrityError(
                "Coordination record receipt is malformed."
            )
        raw = receipt.get("record")
        if not isinstance(raw, Mapping):
            raise CoordinationIntegrityError(
                "Coordination record receipt has no record."
            )
        try:
            return parser(raw)
        except (KeyError, TypeError, ValueError) as error:
            raise CoordinationIntegrityError(
                "Coordination record receipt contains an invalid record."
            ) from error

    @staticmethod
    def _record_storage(record: Any) -> tuple[str, str]:
        payload = record.to_dict()
        record_json = _canonical_json_text(
            payload, label="coordination record", max_bytes=_MAX_RECORD_BYTES
        )
        return record_json, request_digest(payload)

    @staticmethod
    def _parse_row(
        row: sqlite3.Row,
        *,
        parser: Callable[[Mapping[str, Any]], RecordT],
        label: str,
        identity_fields: Mapping[str, str],
    ) -> RecordT:
        payload = _load_canonical_object(
            row["record_json"], label=label, max_bytes=_MAX_RECORD_BYTES
        )
        expected_digest = request_digest(payload)
        if row["record_digest"] != expected_digest:
            raise CoordinationIntegrityError(
                f"Persisted {label} digest does not match its record."
            )
        try:
            record = parser(payload)
        except (KeyError, TypeError, ValueError, RealmIntegrityError) as error:
            raise CoordinationIntegrityError(
                f"Persisted {label} is invalid."
            ) from error
        for column, attribute in identity_fields.items():
            value = getattr(record, attribute)
            if isinstance(value, Enum):
                value = value.value
            if row[column] != value:
                raise CoordinationIntegrityError(
                    f"Persisted {label} columns disagree with its record."
                )
        if int(row["revision"]) != int(getattr(record, "revision")):
            raise CoordinationIntegrityError(
                f"Persisted {label} revision disagrees with its record."
            )
        if (
            float(row["created_at"]) != float(getattr(record, "created_at"))
            or float(row["updated_at"]) != float(getattr(record, "updated_at"))
        ):
            raise CoordinationIntegrityError(
                f"Persisted {label} timestamps disagree with its record."
            )
        return record

    @classmethod
    def _workspace_from_row(cls, row: sqlite3.Row) -> WorkspacePurposeRecord:
        return cls._parse_row(
            row,
            parser=WorkspacePurposeRecord.from_dict,
            label="Workspace purpose record",
            identity_fields={"workspace_id": "workspace_id", "purpose": "purpose"},
        )

    @classmethod
    def _draft_from_row(cls, row: sqlite3.Row) -> StudyDraftRecord:
        return cls._parse_row(
            row,
            parser=StudyDraftRecord.from_dict,
            label="Study draft record",
            identity_fields={
                "draft_id": "draft_id",
                "actor_id": "actor_id",
                "workspace_id": "workspace_id",
                "state": "state",
            },
        )

    @classmethod
    def _action_from_row(cls, row: sqlite3.Row) -> ActionIntentRecord:
        record = cls._parse_row(
            row,
            parser=ActionIntentRecord.from_dict,
            label="action intent record",
            identity_fields={
                "intent_id": "intent_id",
                "actor_id": "actor_id",
                "action_kind": "action_kind",
                "intent_digest": "intent_digest",
                "core_operation_id": "core_operation_id",
                "state": "state",
            },
        )
        return record

    @classmethod
    def _setup_from_row(cls, row: sqlite3.Row) -> RegistrationSetupRecord:
        record = cls._parse_row(
            row,
            parser=RegistrationSetupRecord.from_dict,
            label="registration setup record",
            identity_fields={
                "setup_id": "setup_id",
                "actor_id": "actor_id",
                "workspace_id": "workspace_id",
            },
        )
        if row["state"] != record.data.state.value:
            raise CoordinationIntegrityError(
                "Persisted registration setup state disagrees with its record."
            )
        return record

    @classmethod
    def _write_workspace(
        cls, connection: sqlite3.Connection, record: WorkspacePurposeRecord
    ) -> None:
        record_json, digest = cls._record_storage(record)
        connection.execute(
            "INSERT INTO workspace_purpose_records("
            "workspace_id, purpose, revision, record_digest, record_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET "
            "purpose = excluded.purpose, revision = excluded.revision, "
            "record_digest = excluded.record_digest, record_json = excluded.record_json, "
            "created_at = excluded.created_at, updated_at = excluded.updated_at",
            (
                record.workspace_id,
                record.purpose.value,
                record.revision,
                digest,
                record_json,
                record.created_at,
                record.updated_at,
            ),
        )

    @classmethod
    def _write_draft(
        cls, connection: sqlite3.Connection, record: StudyDraftRecord
    ) -> None:
        record_json, digest = cls._record_storage(record)
        connection.execute(
            "INSERT INTO study_draft_records("
            "draft_id, actor_id, workspace_id, state, revision, record_digest, "
            "record_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(draft_id) DO UPDATE SET actor_id = excluded.actor_id, "
            "workspace_id = excluded.workspace_id, state = excluded.state, "
            "revision = excluded.revision, record_digest = excluded.record_digest, "
            "record_json = excluded.record_json, created_at = excluded.created_at, "
            "updated_at = excluded.updated_at",
            (
                record.draft_id,
                record.actor_id,
                record.workspace_id,
                record.state.value,
                record.revision,
                digest,
                record_json,
                record.created_at,
                record.updated_at,
            ),
        )

    @classmethod
    def _write_action(
        cls, connection: sqlite3.Connection, record: ActionIntentRecord
    ) -> None:
        record_json, digest = cls._record_storage(record)
        connection.execute(
            "INSERT INTO action_intent_records("
            "intent_id, actor_id, action_kind, intent_digest, core_operation_id, "
            "state, revision, record_digest, record_json, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(intent_id) DO UPDATE SET actor_id = excluded.actor_id, "
            "action_kind = excluded.action_kind, intent_digest = excluded.intent_digest, "
            "core_operation_id = excluded.core_operation_id, state = excluded.state, "
            "revision = excluded.revision, record_digest = excluded.record_digest, "
            "record_json = excluded.record_json, created_at = excluded.created_at, "
            "updated_at = excluded.updated_at",
            (
                record.intent_id,
                record.actor_id,
                record.action_kind,
                record.intent_digest,
                record.core_operation_id,
                record.state.value,
                record.revision,
                digest,
                record_json,
                record.created_at,
                record.updated_at,
            ),
        )

    @classmethod
    def _write_setup(
        cls, connection: sqlite3.Connection, record: RegistrationSetupRecord
    ) -> None:
        record_json, digest = cls._record_storage(record)
        connection.execute(
            "INSERT INTO registration_setup_records("
            "setup_id, actor_id, workspace_id, state, revision, record_digest, "
            "record_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(setup_id) DO UPDATE SET actor_id = excluded.actor_id, "
            "workspace_id = excluded.workspace_id, state = excluded.state, "
            "revision = excluded.revision, record_digest = excluded.record_digest, "
            "record_json = excluded.record_json, created_at = excluded.created_at, "
            "updated_at = excluded.updated_at",
            (
                record.setup_id,
                record.actor_id,
                record.workspace_id,
                record.data.state.value,
                record.revision,
                digest,
                record_json,
                record.created_at,
                record.updated_at,
            ),
        )

    def _read_one(
        self,
        *,
        table: str,
        predicate: str,
        parameters: Sequence[Any],
        parser: Callable[[sqlite3.Row], RecordT],
        label: str,
    ) -> RecordT:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {predicate}", tuple(parameters)
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise _storage_unavailable(error) from error
        except sqlite3.DatabaseError as error:
            raise CoordinationIntegrityError(
                f"Could not read {label} from Studio coordination."
            ) from error
        finally:
            connection.close()
        if row is None:
            raise CoordinationNotFound(f"{label} was not found.")
        return parser(row)

    @staticmethod
    def _page_size(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not (
            1 <= value <= _MAX_LIST_PAGE_SIZE
        ):
            raise ValueError(
                f"coordination list limit must be from 1 to {_MAX_LIST_PAGE_SIZE}."
            )
        return value

    def _list_rows(
        self,
        *,
        table: str,
        where: str,
        parameters: Sequence[Any],
        parser: Callable[[sqlite3.Row], RecordT],
        limit: int,
    ) -> tuple[RecordT, ...]:
        limit = self._page_size(limit)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {where} "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (*tuple(parameters), limit),
            ).fetchall()
        except sqlite3.OperationalError as error:
            raise _storage_unavailable(error) from error
        except sqlite3.DatabaseError as error:
            raise CoordinationIntegrityError(
                "Could not list Studio coordination records."
            ) from error
        finally:
            connection.close()
        return tuple(parser(row) for row in rows)

    # -- Workspace product purpose --------------------------------------

    def put_workspace_purpose(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        purpose: WorkspacePurpose,
        subject: EntityCoordinate | None = None,
        label: str | None = None,
        expected_revision: int | None = None,
    ) -> WorkspacePurposeRecord:
        workspace_id = _required_text(workspace_id, "workspace id", max_bytes=512)
        if not isinstance(purpose, WorkspacePurpose):
            raise TypeError("workspace purpose is invalid.")
        if subject is not None and not isinstance(subject, EntityCoordinate):
            raise TypeError("workspace purpose subject is invalid.")
        label = _optional_text(label, "workspace product label", max_bytes=512)
        if expected_revision is not None:
            _positive_int(expected_revision, "expected Workspace purpose revision")
        request = {
            "expected_revision": expected_revision,
            "label": label,
            "purpose": purpose.value,
            "subject": None if subject is None else subject.to_dict(),
            "workspace_id": workspace_id,
        }

        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            row = connection.execute(
                "SELECT * FROM workspace_purpose_records WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise CoordinationConflict(
                        "Workspace purpose does not exist at the expected revision."
                    )
                record = WorkspacePurposeRecord(
                    workspace_id=workspace_id,
                    purpose=purpose,
                    subject=subject,
                    label=label,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            else:
                current = self._workspace_from_row(row)
                candidate = replace(
                    current, purpose=purpose, subject=subject, label=label
                )
                if candidate.semantic_dict() == current.semantic_dict():
                    return self._record_receipt(current)
                if expected_revision != current.revision:
                    raise CoordinationConflict(
                        "Workspace purpose revision changed; reload before updating it."
                    )
                record = replace(
                    candidate,
                    revision=current.revision + 1,
                    updated_at=max(now, current.updated_at),
                )
            self._write_workspace(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="workspace-purpose.put",
            request=request,
            body=body,
        )
        return self._decode_receipt(receipt, WorkspacePurposeRecord.from_dict)

    def get_workspace_purpose(self, workspace_id: str) -> WorkspacePurposeRecord:
        workspace_id = _required_text(workspace_id, "workspace id", max_bytes=512)
        return self._read_one(
            table="workspace_purpose_records",
            predicate="workspace_id = ?",
            parameters=(workspace_id,),
            parser=self._workspace_from_row,
            label="Workspace purpose",
        )

    def list_workspace_purposes(
        self,
        *,
        purpose: WorkspacePurpose | None = None,
        limit: int = 200,
    ) -> tuple[WorkspacePurposeRecord, ...]:
        if purpose is not None and not isinstance(purpose, WorkspacePurpose):
            raise TypeError("workspace purpose filter is invalid.")
        return self._list_rows(
            table="workspace_purpose_records",
            where="1 = 1" if purpose is None else "purpose = ?",
            parameters=() if purpose is None else (purpose.value,),
            parser=self._workspace_from_row,
            limit=limit,
        )

    # -- explicitly saved Study drafts ---------------------------------

    def save_study_draft(
        self,
        *,
        operation_id: str,
        draft_id: str,
        actor_id: str,
        title: str,
        workspace_id: str,
        workspace_revision: int,
        study_relative_path: str,
        config_digest: str,
        expected_revision: int | None = None,
    ) -> StudyDraftRecord:
        draft_id = _required_text(draft_id, "Study draft id", max_bytes=512)
        actor_id = _required_text(actor_id, "Study draft actor id", max_bytes=512)
        title = _required_text(title, "Study draft title", max_bytes=512)
        workspace_id = _required_text(
            workspace_id, "Study draft Workspace id", max_bytes=512
        )
        _positive_int(workspace_revision, "Study draft Workspace revision")
        study_relative_path = _portable_relative_path(
            study_relative_path, "Study draft relative path"
        )
        config_digest = _lower_hex_digest(
            config_digest, "Study draft config digest"
        )
        if expected_revision is not None:
            _positive_int(expected_revision, "expected Study draft revision")
        request = {
            "actor_id": actor_id,
            "config_digest": config_digest,
            "draft_id": draft_id,
            "expected_revision": expected_revision,
            "study_relative_path": study_relative_path,
            "title": title,
            "workspace_id": workspace_id,
            "workspace_revision": workspace_revision,
        }

        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            row = connection.execute(
                "SELECT * FROM study_draft_records WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise CoordinationConflict(
                        "Study draft does not exist at the expected revision."
                    )
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM study_draft_records WHERE actor_id = ?",
                        (actor_id,),
                    ).fetchone()[0]
                )
                if count >= _MAX_STUDY_DRAFTS_PER_ACTOR:
                    raise CoordinationConflict(
                        "Study draft limit reached for this local user."
                    )
                workspace_draft = connection.execute(
                    "SELECT draft_id FROM study_draft_records WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if workspace_draft is not None:
                    raise CoordinationConflict(
                        "Study draft Workspace already backs another draft."
                    )
                record = StudyDraftRecord(
                    draft_id=draft_id,
                    actor_id=actor_id,
                    title=title,
                    workspace_id=workspace_id,
                    workspace_revision=workspace_revision,
                    study_relative_path=study_relative_path,
                    config_digest=config_digest,
                    state=StudyDraftState.ACTIVE,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            else:
                current = self._draft_from_row(row)
                if current.actor_id != actor_id or current.workspace_id != workspace_id:
                    raise CoordinationConflict(
                        "Study draft identity was reused for another actor or Workspace."
                    )
                if current.state is StudyDraftState.DISCARDED:
                    raise CoordinationConflict(
                        "Discarded Study drafts cannot be reactivated; save a new draft."
                    )
                candidate = replace(
                    current,
                    title=title,
                    workspace_revision=workspace_revision,
                    study_relative_path=study_relative_path,
                    config_digest=config_digest,
                )
                if candidate.semantic_dict() == current.semantic_dict():
                    return self._record_receipt(current)
                if expected_revision != current.revision:
                    raise CoordinationConflict(
                        "Study draft revision changed; reload before saving."
                    )
                record = replace(
                    candidate,
                    revision=current.revision + 1,
                    updated_at=max(now, current.updated_at),
                )

            subject = EntityCoordinate(kind="study-draft", entity_id=draft_id)
            purpose_row = connection.execute(
                "SELECT * FROM workspace_purpose_records WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if purpose_row is None:
                purpose = WorkspacePurposeRecord(
                    workspace_id=workspace_id,
                    purpose=WorkspacePurpose.STUDY_DRAFT_BACKING,
                    subject=subject,
                    label=title,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            else:
                current_purpose = self._workspace_from_row(purpose_row)
                if (
                    current_purpose.purpose is not WorkspacePurpose.STUDY_DRAFT_BACKING
                    or current_purpose.subject != subject
                ):
                    raise CoordinationConflict(
                        "Study draft Workspace already has another product purpose."
                    )
                purpose = current_purpose
                if purpose.label != title:
                    purpose = replace(
                        purpose,
                        label=title,
                        revision=purpose.revision + 1,
                        updated_at=max(now, purpose.updated_at),
                    )
            self._write_workspace(connection, purpose)
            self._write_draft(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="study-draft.save",
            request=request,
            body=body,
        )
        return self._decode_receipt(receipt, StudyDraftRecord.from_dict)

    def get_study_draft(self, draft_id: str) -> StudyDraftRecord:
        draft_id = _required_text(draft_id, "Study draft id", max_bytes=512)
        return self._read_one(
            table="study_draft_records",
            predicate="draft_id = ?",
            parameters=(draft_id,),
            parser=self._draft_from_row,
            label="Study draft",
        )

    def list_study_drafts(
        self,
        *,
        actor_id: str,
        include_discarded: bool = False,
        limit: int = 200,
    ) -> tuple[StudyDraftRecord, ...]:
        actor_id = _required_text(actor_id, "Study draft actor id", max_bytes=512)
        if not isinstance(include_discarded, bool):
            raise TypeError("include_discarded must be a boolean.")
        return self._list_rows(
            table="study_draft_records",
            where=("actor_id = ?" if include_discarded else "actor_id = ? AND state = 'active'"),
            parameters=(actor_id,),
            parser=self._draft_from_row,
            limit=limit,
        )

    def discard_study_draft(
        self,
        *,
        operation_id: str,
        draft_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> StudyDraftRecord:
        draft_id = _required_text(draft_id, "Study draft id", max_bytes=512)
        actor_id = _required_text(actor_id, "Study draft actor id", max_bytes=512)
        _positive_int(expected_revision, "expected Study draft revision")

        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            row = connection.execute(
                "SELECT * FROM study_draft_records WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise CoordinationNotFound("Study draft was not found.")
            current = self._draft_from_row(row)
            if current.actor_id != actor_id:
                raise CoordinationNotFound("Study draft was not found.")
            if current.state is StudyDraftState.DISCARDED:
                return self._record_receipt(current)
            if current.revision != expected_revision:
                raise CoordinationConflict(
                    "Study draft revision changed; reload before discarding."
                )
            record = replace(
                current,
                state=StudyDraftState.DISCARDED,
                revision=current.revision + 1,
                updated_at=max(now, current.updated_at),
            )
            self._write_draft(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="study-draft.discard",
            request={
                "actor_id": actor_id,
                "draft_id": draft_id,
                "expected_revision": expected_revision,
            },
            body=body,
        )
        return self._decode_receipt(receipt, StudyDraftRecord.from_dict)

    # -- durable action intents and receipts ----------------------------

    def begin_action(
        self,
        *,
        operation_id: str,
        intent_id: str,
        actor_id: str,
        action_kind: str,
        source: EntityCoordinate,
        parameters: Mapping[str, Any],
        core_operation_id: str | None = None,
    ) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)
        actor_id = _required_text(actor_id, "action actor id", max_bytes=512)
        action_kind = _kind(action_kind, "action kind")
        if not isinstance(source, EntityCoordinate):
            raise TypeError("action source is invalid.")
        parameters = _bounded_json_object(
            parameters,
            label="action parameters",
            max_bytes=_MAX_ACTION_PARAMETERS_BYTES,
        )
        selected_core_operation_id = (
            ActionIntentRecord.default_core_operation_id(
                actor_id=actor_id, intent_id=intent_id
            )
            if core_operation_id is None
            else _required_text(
                core_operation_id,
                "action Core operation id",
                max_bytes=_MAX_OPERATION_ID_BYTES,
            )
        )
        intent_digest = ActionIntentRecord.compute_intent_digest(
            intent_id=intent_id,
            actor_id=actor_id,
            action_kind=action_kind,
            source=source,
            parameters=parameters,
        )
        request = {
            "action_kind": action_kind,
            "actor_id": actor_id,
            "core_operation_id": selected_core_operation_id,
            "intent_digest": intent_digest,
            "intent_id": intent_id,
            "parameters": _thaw_json(parameters),
            "source": source.to_dict(),
        }

        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            row = connection.execute(
                "SELECT * FROM action_intent_records WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is not None:
                current = self._action_from_row(row)
                if current.immutable_dict() != request:
                    raise CoordinationConflict(
                        "Action intent id was reused for another request."
                    )
                return self._record_receipt(current)
            operation_owner = connection.execute(
                "SELECT intent_id FROM action_intent_records "
                "WHERE core_operation_id = ?",
                (selected_core_operation_id,),
            ).fetchone()
            if operation_owner is not None:
                raise CoordinationConflict(
                    "Core operation id already belongs to another action intent."
                )
            record = ActionIntentRecord(
                intent_id=intent_id,
                actor_id=actor_id,
                action_kind=action_kind,
                source=source,
                parameters=parameters,
                intent_digest=intent_digest,
                core_operation_id=selected_core_operation_id,
                state=ActionState.PENDING,
                result=None,
                core_receipt=None,
                error_code=None,
                error_message=None,
                attempt_count=1,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            self._write_action(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="action.begin",
            request=request,
            body=body,
        )
        return self._decode_receipt(receipt, ActionIntentRecord.from_dict)

    def get_action(self, intent_id: str) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)
        return self._read_one(
            table="action_intent_records",
            predicate="intent_id = ?",
            parameters=(intent_id,),
            parser=self._action_from_row,
            label="action intent",
        )

    def list_actions(
        self,
        *,
        actor_id: str,
        action_kind: str | None = None,
        state: ActionState | None = None,
        limit: int = 200,
    ) -> tuple[ActionIntentRecord, ...]:
        actor_id = _required_text(actor_id, "action actor id", max_bytes=512)
        clauses = ["actor_id = ?"]
        parameters: list[Any] = [actor_id]
        if action_kind is not None:
            clauses.append("action_kind = ?")
            parameters.append(_kind(action_kind, "action kind"))
        if state is not None:
            if not isinstance(state, ActionState):
                raise TypeError("action state filter is invalid.")
            clauses.append("state = ?")
            parameters.append(state.value)
        return self._list_rows(
            table="action_intent_records",
            where=" AND ".join(clauses),
            parameters=tuple(parameters),
            parser=self._action_from_row,
            limit=limit,
        )

    def mark_action_uncertain(
        self,
        *,
        operation_id: str,
        intent_id: str,
        message: str | None = None,
    ) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)
        message = _optional_text(
            message, "action uncertainty message", max_bytes=16 * 1024
        )

        def transition(current: ActionIntentRecord, now: float) -> ActionIntentRecord:
            if current.state in {
                ActionState.UNCERTAIN,
                ActionState.SUCCEEDED,
                ActionState.FAILED,
            }:
                return current
            return replace(
                current,
                state=ActionState.UNCERTAIN,
                error_message=message,
                revision=current.revision + 1,
                updated_at=max(now, current.updated_at),
            )

        return self._transition_action(
            operation_id=operation_id,
            operation_kind="action.mark-uncertain",
            intent_id=intent_id,
            request={"intent_id": intent_id, "message": message},
            transition=transition,
        )

    def retry_action(
        self, *, operation_id: str, intent_id: str
    ) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)

        def transition(current: ActionIntentRecord, now: float) -> ActionIntentRecord:
            if current.state in {ActionState.PENDING, ActionState.SUCCEEDED}:
                return current
            return replace(
                current,
                state=ActionState.PENDING,
                error_code=None,
                error_message=None,
                attempt_count=current.attempt_count + 1,
                revision=current.revision + 1,
                updated_at=max(now, current.updated_at),
            )

        return self._transition_action(
            operation_id=operation_id,
            operation_kind="action.retry",
            intent_id=intent_id,
            request={"intent_id": intent_id},
            transition=transition,
        )

    def complete_action(
        self,
        *,
        operation_id: str,
        intent_id: str,
        result: EntityCoordinate,
        core_receipt: Mapping[str, Any] | None = None,
    ) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)
        if not isinstance(result, EntityCoordinate):
            raise TypeError("action result is invalid.")
        frozen_receipt = (
            None
            if core_receipt is None
            else _bounded_json_object(
                core_receipt,
                label="action Core receipt",
                max_bytes=_MAX_ACTION_RECEIPT_BYTES,
            )
        )

        def transition(current: ActionIntentRecord, now: float) -> ActionIntentRecord:
            if current.state is ActionState.SUCCEEDED:
                if current.result != result or current.core_receipt != frozen_receipt:
                    raise CoordinationConflict(
                        "Action intent already succeeded with another result."
                    )
                return current
            if current.state is ActionState.FAILED:
                raise CoordinationConflict(
                    "A definitely failed action must be retried before completion."
                )
            return replace(
                current,
                state=ActionState.SUCCEEDED,
                result=result,
                core_receipt=frozen_receipt,
                error_code=None,
                error_message=None,
                revision=current.revision + 1,
                updated_at=max(now, current.updated_at),
            )

        return self._transition_action(
            operation_id=operation_id,
            operation_kind="action.complete",
            intent_id=intent_id,
            request={
                "core_receipt": (
                    None if frozen_receipt is None else _thaw_json(frozen_receipt)
                ),
                "intent_id": intent_id,
                "result": result.to_dict(),
            },
            transition=transition,
        )

    def fail_action(
        self,
        *,
        operation_id: str,
        intent_id: str,
        error_code: str,
        error_message: str,
    ) -> ActionIntentRecord:
        intent_id = _required_text(intent_id, "action intent id", max_bytes=512)
        error_code = _required_text(error_code, "action error code", max_bytes=128)
        error_message = _required_text(
            error_message, "action error message", max_bytes=16 * 1024
        )

        def transition(current: ActionIntentRecord, now: float) -> ActionIntentRecord:
            if current.state is ActionState.SUCCEEDED:
                raise CoordinationConflict("Successful action cannot later fail.")
            if current.state is ActionState.FAILED:
                if (
                    current.error_code != error_code
                    or current.error_message != error_message
                ):
                    raise CoordinationConflict(
                        "Action intent already failed with another disposition."
                    )
                return current
            return replace(
                current,
                state=ActionState.FAILED,
                error_code=error_code,
                error_message=error_message,
                revision=current.revision + 1,
                updated_at=max(now, current.updated_at),
            )

        return self._transition_action(
            operation_id=operation_id,
            operation_kind="action.fail",
            intent_id=intent_id,
            request={
                "error_code": error_code,
                "error_message": error_message,
                "intent_id": intent_id,
            },
            transition=transition,
        )

    def _transition_action(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        intent_id: str,
        request: Mapping[str, Any],
        transition: Callable[[ActionIntentRecord, float], ActionIntentRecord],
    ) -> ActionIntentRecord:
        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            row = connection.execute(
                "SELECT * FROM action_intent_records WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise CoordinationNotFound("Action intent was not found.")
            current = self._action_from_row(row)
            record = transition(current, now)
            if record != current:
                self._write_action(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind=operation_kind,
            request=request,
            body=body,
        )
        return self._decode_receipt(receipt, ActionIntentRecord.from_dict)

    # -- reopenable Workspace registration setup -----------------------

    def save_registration_setup(
        self,
        *,
        operation_id: str,
        actor_id: str,
        workspace_id: str,
        data: RegistrationSetupData,
        expected_revision: int | None = None,
    ) -> RegistrationSetupRecord:
        actor_id = _required_text(
            actor_id, "registration setup actor id", max_bytes=512
        )
        workspace_id = _required_text(
            workspace_id, "registration setup Workspace id", max_bytes=512
        )
        if not isinstance(data, RegistrationSetupData):
            raise TypeError("registration setup data is invalid.")
        if expected_revision is not None:
            _positive_int(expected_revision, "expected registration setup revision")
        setup_id = RegistrationSetupRecord.stable_id(
            actor_id=actor_id, workspace_id=workspace_id
        )
        request = {
            "actor_id": actor_id,
            "data": data.to_dict(),
            "expected_revision": expected_revision,
            "setup_id": setup_id,
            "workspace_id": workspace_id,
        }

        def body(connection: sqlite3.Connection, now: float) -> Mapping[str, Any]:
            purpose_row = connection.execute(
                "SELECT * FROM workspace_purpose_records WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if purpose_row is None:
                purpose = WorkspacePurposeRecord(
                    workspace_id=workspace_id,
                    purpose=WorkspacePurpose.USER_PROJECT,
                    subject=None,
                    label=None,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                self._write_workspace(connection, purpose)
            else:
                purpose = self._workspace_from_row(purpose_row)
                if purpose.purpose is not WorkspacePurpose.USER_PROJECT:
                    raise CoordinationConflict(
                        "Registration Setup belongs only to an editable user Workspace."
                    )

            if data.publication_intent_id is not None:
                action_row = connection.execute(
                    "SELECT * FROM action_intent_records WHERE intent_id = ?",
                    (data.publication_intent_id,),
                ).fetchone()
                if action_row is None:
                    raise CoordinationConflict(
                        "Registration Setup publication intent is not durable."
                    )
                action = self._action_from_row(action_row)
                if (
                    action.actor_id != actor_id
                    or action.action_kind != "catalog-publication"
                    or action.source.kind != "workspace"
                    or action.source.entity_id != workspace_id
                    or data.check is None
                    or action.source.revision != data.check.workspace_revision
                    or action.parameters.get("package_id") != data.package_id
                    or action.parameters.get("artifact_ref")
                    != str(data.check.artifact_ref)
                ):
                    raise CoordinationConflict(
                        "Registration Setup publication intent belongs elsewhere."
                    )
                if data.state is RegistrationSetupState.REGISTERED and (
                    action.state is not ActionState.SUCCEEDED
                    or action.result != data.publication_result
                ):
                    raise CoordinationConflict(
                        "Registered Setup does not match its durable action receipt."
                    )

            row = connection.execute(
                "SELECT * FROM registration_setup_records WHERE setup_id = ?",
                (setup_id,),
            ).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise CoordinationConflict(
                        "Registration Setup does not exist at the expected revision."
                    )
                record = RegistrationSetupRecord(
                    setup_id=setup_id,
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    data=data,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            else:
                current = self._setup_from_row(row)
                if current.data == data:
                    return self._record_receipt(current)
                if expected_revision != current.revision:
                    raise CoordinationConflict(
                        "Registration Setup revision changed; reload before saving."
                    )
                record = replace(
                    current,
                    data=data,
                    revision=current.revision + 1,
                    updated_at=max(now, current.updated_at),
                )
            self._write_setup(connection, record)
            return self._record_receipt(record)

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="registration-setup.save",
            request=request,
            body=body,
        )
        return self._decode_receipt(receipt, RegistrationSetupRecord.from_dict)

    def get_registration_setup(
        self, *, actor_id: str, workspace_id: str
    ) -> RegistrationSetupRecord:
        actor_id = _required_text(
            actor_id, "registration setup actor id", max_bytes=512
        )
        workspace_id = _required_text(
            workspace_id, "registration setup Workspace id", max_bytes=512
        )
        return self._read_one(
            table="registration_setup_records",
            predicate="actor_id = ? AND workspace_id = ?",
            parameters=(actor_id, workspace_id),
            parser=self._setup_from_row,
            label="registration setup",
        )

    def get_registration_setup_by_id(
        self, setup_id: str
    ) -> RegistrationSetupRecord:
        setup_id = _required_text(
            setup_id, "registration setup id", max_bytes=512
        )
        return self._read_one(
            table="registration_setup_records",
            predicate="setup_id = ?",
            parameters=(setup_id,),
            parser=self._setup_from_row,
            label="registration setup",
        )

    def list_registration_setups(
        self, *, actor_id: str, limit: int = 200
    ) -> tuple[RegistrationSetupRecord, ...]:
        actor_id = _required_text(
            actor_id, "registration setup actor id", max_bytes=512
        )
        return self._list_rows(
            table="registration_setup_records",
            where="actor_id = ?",
            parameters=(actor_id,),
            parser=self._setup_from_row,
            limit=limit,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive storage facts for health checks and tests."""

        connection = self._connect()
        try:
            counts = {
                label: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for label, table in (
                    ("workspace_purposes", "workspace_purpose_records"),
                    ("study_drafts", "study_draft_records"),
                    ("action_intents", "action_intent_records"),
                    ("registration_setups", "registration_setup_records"),
                    ("operations", "coordination_operations"),
                )
            }
            return {
                "database": str(self.database_path),
                "foreign_keys": int(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                ),
                "instance_id": self.instance_id,
                "journal_mode": str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower(),
                "records": counts,
                "schema_version": int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                ),
            }
        except sqlite3.OperationalError as error:
            raise _storage_unavailable(error) from error
        finally:
            connection.close()


__all__ = [
    "ActionIntentRecord",
    "ActionState",
    "COORDINATION_DATABASE_NAME",
    "COORDINATION_STORAGE_UNAVAILABLE_MESSAGE",
    "CoordinationConflict",
    "CoordinationIntegrityError",
    "CoordinationNotFound",
    "CoordinationStorageUnavailable",
    "EntityCoordinate",
    "RegistrationCheck",
    "RegistrationSetupData",
    "RegistrationSetupRecord",
    "RegistrationSetupState",
    "RegistrationTestResult",
    "StudioCoordinationStore",
    "StudyDraftRecord",
    "StudyDraftState",
    "WorkspacePurpose",
    "WorkspacePurposeRecord",
    "coordination_database_path",
    "prepare_coordination_database",
    "studio_project_state_directory",
]
