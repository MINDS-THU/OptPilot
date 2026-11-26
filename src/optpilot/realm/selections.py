"""Immutable selection anchors and typed content-derivation results.

Selections identify domain evidence, not filesystem paths and not bearer
credentials.  Every use must resolve the selection again through
``RealmLedger`` with the permission required by that use.  The initial
executable slice resolves run candidates and retained artifacts to a single
immutable tree.  Other kinds remain representable so callers receive an
explicit capability result instead of a guessed projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Mapping

from ._validation import (
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .owners import OwnerMembership
from .refs import SnapshotRef, request_digest
from .run_records import RunCandidateSelection


SELECTION_REF_SCHEMA = "optpilot.selection-ref.v1"
READ_ONLY_VIEW_SCHEMA = "optpilot.read-only-selection-view.v1"
if TYPE_CHECKING:
    from .study_definition import StudyDefinitionReceipt


SELECTION_KINDS = frozenset(
    {
        "candidate",
        "trial",
        "attempt",
        "artifact",
        "workspace",
        "study-definition",
        "catalog-package",
    }
)
SELECTION_SOURCE_KINDS = frozenset(
    {
        "run",
        "workspace",
        "interface-output",
        "operator-job",
        "study-definition",
        "catalog",
    }
)
_MAX_RELATIVE_PATH_BYTES = 4 * 1024


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("selection relative_path must be a non-empty string or null.")
    if len(value.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES:
        raise ValueError("selection relative_path is too long.")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise ValueError("selection relative_path must be a safe POSIX relative path.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("selection relative_path must be canonical and traversal-free.")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise ValueError("selection relative_path must be canonical and relative.")
    return value


def _selection_payload(
    *,
    kind: str,
    source_kind: str,
    source_id: str,
    source_owner_id: str,
    source_revision: int,
    owner_revision: int,
    source_sequence: int | None,
    entity_sequence: int | None,
    entity_id: str,
    entity_ref: str,
    context_digest: str | None,
    relative_path: str | None,
) -> dict[str, Any]:
    return {
        "schema": SELECTION_REF_SCHEMA,
        "kind": kind,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_owner_id": source_owner_id,
        "source_revision": source_revision,
        "owner_revision": owner_revision,
        "source_sequence": source_sequence,
        "entity_sequence": entity_sequence,
        "entity_id": entity_id,
        "entity_ref": entity_ref,
        "context_digest": context_digest,
        "relative_path": relative_path,
    }


@dataclass(frozen=True)
class SelectionRef:
    """One immutable domain selection anchored to an authority revision."""

    kind: str
    source_kind: str
    source_id: str
    source_owner_id: str
    source_revision: int
    owner_revision: int
    source_sequence: int | None
    entity_sequence: int | None
    entity_id: str
    entity_ref: str
    context_digest: str | None
    relative_path: str | None
    selection_digest: str

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        source_kind: str,
        source_id: str,
        source_owner_id: str,
        source_revision: int,
        owner_revision: int,
        source_sequence: int | None,
        entity_sequence: int | None,
        entity_id: str,
        entity_ref: str,
        context_digest: str | None = None,
        relative_path: str | None = None,
    ) -> "SelectionRef":
        relative_path = _relative_path(relative_path)
        payload = _selection_payload(
            kind=kind,
            source_kind=source_kind,
            source_id=source_id,
            source_owner_id=source_owner_id,
            source_revision=source_revision,
            owner_revision=owner_revision,
            source_sequence=source_sequence,
            entity_sequence=entity_sequence,
            entity_id=entity_id,
            entity_ref=entity_ref,
            context_digest=context_digest,
            relative_path=relative_path,
        )
        return cls(selection_digest=request_digest(payload), **{
            key: value for key, value in payload.items() if key != "schema"
        })

    @classmethod
    def from_run_candidate(
        cls,
        selection: RunCandidateSelection,
        *,
        source_owner_id: str,
        source_sequence: int,
    ) -> "SelectionRef":
        if not isinstance(selection, RunCandidateSelection):
            raise TypeError("selection must be a RunCandidateSelection.")
        return cls.build(
            kind="candidate",
            source_kind="run",
            source_id=selection.run_id,
            source_owner_id=source_owner_id,
            source_revision=selection.run_revision,
            owner_revision=selection.owner_revision,
            source_sequence=source_sequence,
            entity_sequence=selection.sequence,
            entity_id=selection.candidate_id,
            entity_ref=str(selection.candidate_ref),
            context_digest=selection.evaluation_template_digest,
        )

    @classmethod
    def from_study_definition(
        cls,
        definition: "StudyDefinitionReceipt",
    ) -> "SelectionRef":
        """Anchor one exact retained study definition without a filesystem ref."""

        # Keep the import local: study-definition records are a higher-level
        # domain concept, while the generic selection module is imported by
        # several lower-level record modules.
        from .study_definition import StudyDefinitionReceipt

        if not isinstance(definition, StudyDefinitionReceipt):
            raise TypeError("definition must be a StudyDefinitionReceipt.")
        manifest = definition.manifest
        return cls.build(
            kind="study-definition",
            source_kind="study-definition",
            source_id=manifest.owner_id,
            source_owner_id=manifest.owner_id,
            source_revision=manifest.owner_revision,
            owner_revision=definition.owner.revision,
            source_sequence=None,
            entity_sequence=None,
            entity_id=manifest.owner_id,
            entity_ref=f"study-definition:sha256:{manifest.manifest_digest}",
            context_digest=manifest.run_definition_digest,
        )

    def __post_init__(self) -> None:
        required_text(self.kind, "selection kind", max_bytes=128)
        if self.kind not in SELECTION_KINDS:
            raise ValueError("selection kind is unsupported.")
        required_text(self.source_kind, "selection source kind", max_bytes=128)
        if self.source_kind not in SELECTION_SOURCE_KINDS:
            raise ValueError("selection source kind is unsupported.")
        required_text(self.source_id, "selection source id")
        required_text(self.source_owner_id, "selection source owner id")
        nonnegative_int(self.source_revision, "selection source revision")
        nonnegative_int(self.owner_revision, "selection owner revision")
        if self.source_kind == "run":
            if self.source_sequence is None or self.entity_sequence is None:
                raise ValueError(
                    "run selections require source_sequence and entity_sequence."
                )
            positive_int(self.source_sequence, "selection source sequence")
            positive_int(self.entity_sequence, "selection entity sequence")
            if self.entity_sequence > self.source_sequence:
                raise ValueError(
                    "selection entity sequence cannot exceed its source head sequence."
                )
        elif self.source_sequence is not None or self.entity_sequence is not None:
            raise ValueError("non-run selections cannot carry run sequences.")
        required_text(self.entity_id, "selection entity id")
        required_text(self.entity_ref, "selection entity ref")
        if self.context_digest is not None:
            lower_hex_digest(self.context_digest, "selection context digest")
        if self.kind == "candidate" and self.context_digest is None:
            raise ValueError("candidate selections require a context digest.")
        if self.kind == "study-definition":
            if self.source_kind != "study-definition":
                raise ValueError(
                    "study-definition selections require a study-definition source."
                )
            if (
                self.source_revision != 0
                or self.owner_revision != 0
                or self.source_id != self.source_owner_id
                or self.entity_id != self.source_owner_id
                or self.relative_path is not None
                or self.context_digest is None
            ):
                raise ValueError(
                    "study-definition selection anchors are not exact revision zero."
                )
            prefix = "study-definition:sha256:"
            if not self.entity_ref.startswith(prefix):
                raise ValueError(
                    "study-definition entity ref must carry its manifest digest."
                )
            lower_hex_digest(
                self.entity_ref[len(prefix) :],
                "study-definition manifest digest",
            )
        elif self.source_kind == "study-definition":
            raise ValueError(
                "study-definition sources require a study-definition selection."
            )
        if self.kind == "catalog-package":
            if (
                self.source_kind != "catalog"
                or self.source_revision <= 0
                or self.owner_revision != 0
                or self.context_digest is None
                or self.relative_path is not None
            ):
                raise ValueError(
                    "catalog-package selections require one exact package revision root."
                )
            try:
                SnapshotRef.parse(self.entity_ref)
            except ValueError as error:
                raise ValueError(
                    "catalog-package selection entity_ref must be a tree ref."
                ) from error
        elif self.source_kind == "catalog":
            raise ValueError("catalog sources require a catalog-package selection.")
        if self.kind == "workspace":
            if (
                self.source_kind != "workspace"
                or self.source_revision <= 0
                or self.owner_revision <= 0
                or self.source_id != self.entity_id
                or self.relative_path is not None
                or self.context_digest is None
            ):
                raise ValueError(
                    "workspace selections require one exact workspace revision root."
                )
            try:
                SnapshotRef.parse(self.entity_ref)
            except ValueError as error:
                raise ValueError(
                    "workspace selection entity_ref must be a tree ref."
                ) from error
        elif self.source_kind == "workspace":
            raise ValueError("workspace sources require a workspace selection.")
        canonical_path = _relative_path(self.relative_path)
        object.__setattr__(self, "relative_path", canonical_path)
        expected = request_digest(
            _selection_payload(
                kind=self.kind,
                source_kind=self.source_kind,
                source_id=self.source_id,
                source_owner_id=self.source_owner_id,
                source_revision=self.source_revision,
                owner_revision=self.owner_revision,
                source_sequence=self.source_sequence,
                entity_sequence=self.entity_sequence,
                entity_id=self.entity_id,
                entity_ref=self.entity_ref,
                context_digest=self.context_digest,
                relative_path=self.relative_path,
            )
        )
        if self.selection_digest != expected:
            raise ValueError("selection digest differs from its immutable anchor.")

    def to_dict(self) -> dict[str, Any]:
        result = _selection_payload(
            kind=self.kind,
            source_kind=self.source_kind,
            source_id=self.source_id,
            source_owner_id=self.source_owner_id,
            source_revision=self.source_revision,
            owner_revision=self.owner_revision,
            source_sequence=self.source_sequence,
            entity_sequence=self.entity_sequence,
            entity_id=self.entity_id,
            entity_ref=self.entity_ref,
            context_digest=self.context_digest,
            relative_path=self.relative_path,
        )
        result["selection_digest"] = self.selection_digest
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SelectionRef":
        try:
            _exact_keys(
                payload,
                {
                    "schema",
                    "kind",
                    "source_kind",
                    "source_id",
                    "source_owner_id",
                    "source_revision",
                    "owner_revision",
                    "source_sequence",
                    "entity_sequence",
                    "entity_id",
                    "entity_ref",
                    "context_digest",
                    "relative_path",
                    "selection_digest",
                },
                "selection ref",
            )
            if payload["schema"] != SELECTION_REF_SCHEMA:
                raise ValueError("selection ref schema is unsupported.")
            result = cls(
                kind=payload["kind"],
                source_kind=payload["source_kind"],
                source_id=payload["source_id"],
                source_owner_id=payload["source_owner_id"],
                source_revision=payload["source_revision"],
                owner_revision=payload["owner_revision"],
                source_sequence=payload["source_sequence"],
                entity_sequence=payload["entity_sequence"],
                entity_id=payload["entity_id"],
                entity_ref=payload["entity_ref"],
                context_digest=payload["context_digest"],
                relative_path=payload["relative_path"],
                selection_digest=payload["selection_digest"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("selection ref is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Selection ref is invalid: {error}") from error


@dataclass(frozen=True)
class SelectionEligibility:
    """Machine-readable capability answer for one selection action."""

    supported: bool
    eligible: bool
    code: str
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.supported, bool) or not isinstance(self.eligible, bool):
            raise TypeError("selection eligibility flags must be booleans.")
        if self.eligible and not self.supported:
            raise ValueError("an eligible selection action must be supported.")
        required_text(self.code, "selection eligibility code", max_bytes=128)
        if self.eligible:
            if self.reason is not None:
                raise ValueError("eligible selection actions cannot have a reason.")
        else:
            required_text(self.reason, "selection eligibility reason", max_bytes=1024)

    @classmethod
    def ready(cls) -> "SelectionEligibility":
        return cls(True, True, "ready", None)

    @classmethod
    def unavailable(cls, code: str, reason: str) -> "SelectionEligibility":
        return cls(True, False, code, reason)

    @classmethod
    def unsupported(cls, code: str, reason: str) -> "SelectionEligibility":
        return cls(False, False, code, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "eligible": self.eligible,
            "code": self.code,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SelectionEligibility":
        try:
            _exact_keys(
                payload,
                {"supported", "eligible", "code", "reason"},
                "selection eligibility",
            )
            result = cls(
                supported=payload["supported"],
                eligible=payload["eligible"],
                code=payload["code"],
                reason=payload["reason"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("selection eligibility is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Selection eligibility is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class ResolvedSelection:
    """Authorized resolution of a selection to zero or one immutable tree."""

    selection: SelectionRef
    source_current_owner_revision: int
    eligibility: SelectionEligibility
    root: OwnerMembership | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        nonnegative_int(
            self.source_current_owner_revision,
            "selection source current owner revision",
        )
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible:
            if self.root is None or not isinstance(self.root.content_ref, SnapshotRef):
                raise ValueError("eligible content selection requires one tree root.")
        elif self.root is not None:
            raise ValueError("ineligible content selection cannot expose a root.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "source_current_owner_revision": self.source_current_owner_revision,
            "eligibility": self.eligibility.to_dict(),
            "root": None if self.root is None else self.root.to_dict(),
        }


@dataclass(frozen=True)
class ResolvedSelectionContent:
    """Authorized resolution to zero or one immutable physical content root.

    This is deliberately distinct from :class:`ResolvedSelection`.  The
    latter is the tree-only authority used by read projections and editable
    workspace derivation.  Direct content inspection may also read a retained
    blob, but must never make that blob eligible for projection or Keep.
    """

    selection: SelectionRef
    source_current_owner_revision: int
    eligibility: SelectionEligibility
    root: OwnerMembership | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        nonnegative_int(
            self.source_current_owner_revision,
            "selection source current owner revision",
        )
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible:
            if self.root is None:
                raise ValueError(
                    "eligible content inspection requires one physical root."
                )
        elif self.root is not None:
            raise ValueError(
                "ineligible content inspection cannot expose a physical root."
            )

@dataclass(frozen=True)
class ReadOnlySelectionView:
    """Non-durable descriptor for protected read-only access to one tree.

    The descriptor is not a bearer credential.  A projection/content provider
    must resolve ``selection`` again before exposing bytes.
    """

    view_id: str
    selection: SelectionRef
    resolved_owner_revision: int
    root_store_id: str
    root_ref: SnapshotRef
    mode: str = "read_only"
    writable: bool = False
    durable: bool = False

    @classmethod
    def build(cls, resolution: ResolvedSelection) -> "ReadOnlySelectionView":
        if not isinstance(resolution, ResolvedSelection):
            raise TypeError("resolution must be a ResolvedSelection.")
        if not resolution.eligibility.eligible or resolution.root is None:
            raise ValueError("only an eligible tree selection can be opened.")
        payload = {
            "schema": READ_ONLY_VIEW_SCHEMA,
            "selection_digest": resolution.selection.selection_digest,
            "resolved_owner_revision": resolution.source_current_owner_revision,
            "root_store_id": resolution.root.store_id,
            "root_ref": str(resolution.root.content_ref),
        }
        return cls(
            view_id=request_digest(payload),
            selection=resolution.selection,
            resolved_owner_revision=resolution.source_current_owner_revision,
            root_store_id=resolution.root.store_id,
            root_ref=resolution.root.content_ref,
        )

    def __post_init__(self) -> None:
        lower_hex_digest(self.view_id, "read-only view id")
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        nonnegative_int(self.resolved_owner_revision, "resolved owner revision")
        required_text(self.root_store_id, "read-only view root store id", max_bytes=128)
        if not isinstance(self.root_ref, SnapshotRef):
            raise TypeError("read-only view root_ref must be a SnapshotRef.")
        if self.mode != "read_only" or self.writable or self.durable:
            raise ValueError("read-only selection view policy is immutable.")
        expected = request_digest(
            {
                "schema": READ_ONLY_VIEW_SCHEMA,
                "selection_digest": self.selection.selection_digest,
                "resolved_owner_revision": self.resolved_owner_revision,
                "root_store_id": self.root_store_id,
                "root_ref": str(self.root_ref),
            }
        )
        if self.view_id != expected:
            raise ValueError("read-only view id differs from its resolved selection.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": READ_ONLY_VIEW_SCHEMA,
            "view_id": self.view_id,
            "selection": self.selection.to_dict(),
            "resolved_owner_revision": self.resolved_owner_revision,
            "root_store_id": self.root_store_id,
            "root_ref": str(self.root_ref),
            "mode": self.mode,
            "writable": self.writable,
            "durable": self.durable,
            "authorization": "resolve_selection_again",
        }


__all__ = [
    "READ_ONLY_VIEW_SCHEMA",
    "SELECTION_KINDS",
    "SELECTION_REF_SCHEMA",
    "SELECTION_SOURCE_KINDS",
    "ReadOnlySelectionView",
    "ResolvedSelection",
    "ResolvedSelectionContent",
    "SelectionEligibility",
    "SelectionRef",
]
