"""Canonical provenance for no-copy owner derivation.

These records describe one internal Realm operation which gives a new semantic
owner its own memberships over content already retained by exact source-owner
revisions.  They do not carry paths, leases, ACL grants, owner edges, or a
provider copy plan.  Authorization and the atomic owner creation operation stay
in :mod:`optpilot.realm.ledger`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from ._validation import lower_hex_digest, nonnegative_int, required_text
from .errors import RealmIntegrityError
from .owners import OwnerMembership, OwnerRecord, OwnerState
from .refs import (
    BlobRef,
    PhysicalContentRef,
    SnapshotRef,
    canonical_json_bytes,
    parse_physical_content_ref,
    request_digest,
)
from .selections import SelectionEligibility, SelectionRef


JsonDict = Dict[str, Any]

OWNER_DERIVATION_MANIFEST_SCHEMA = "optpilot.owner-derivation-manifest.v1"

# These are authority-record bounds, not content-store bounds.  Keeping them
# explicit prevents a single provenance record from becoming an unbounded SQL
# or JSON workload while leaving enough room for composite study definitions.
MAX_DERIVATION_SOURCES = 256
MAX_DERIVATION_BINDINGS = 2048
MAX_DERIVATION_MANIFEST_BYTES = 1024 * 1024


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _utf8(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


@dataclass(frozen=True, order=True)
class SourceAnchor:
    """One exact immutable owner revision used as derivation authority."""

    owner_id: str
    owner_revision: int
    owner_manifest_digest: str

    def __post_init__(self) -> None:
        required_text(self.owner_id, "source owner_id")
        nonnegative_int(self.owner_revision, "source owner revision")
        lower_hex_digest(
            self.owner_manifest_digest, "source owner manifest digest"
        )

    def to_dict(self) -> JsonDict:
        return {
            "owner_id": self.owner_id,
            "owner_manifest_digest": self.owner_manifest_digest,
            "owner_revision": self.owner_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceAnchor":
        try:
            _exact_keys(
                payload,
                {"owner_id", "owner_manifest_digest", "owner_revision"},
                "source anchor",
            )
            return cls(
                owner_id=payload["owner_id"],
                owner_revision=payload["owner_revision"],
                owner_manifest_digest=payload["owner_manifest_digest"],
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted owner derivation source anchor is invalid: {error}"
            ) from error


@dataclass(frozen=True, order=True)
class Binding:
    """Map one exact source membership to one target membership role.

    ``source_store_id`` and ``content_ref`` are intentionally preserved on the
    target.  A binding therefore changes semantic ownership only; it cannot ask
    the Realm to copy or relocate bytes.
    """

    source_owner_id: str
    source_store_id: str
    content_ref: PhysicalContentRef
    source_role: str
    target_role: str

    def __post_init__(self) -> None:
        required_text(self.source_owner_id, "binding source owner_id")
        required_text(
            self.source_store_id, "binding source store_id", max_bytes=128
        )
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("binding content_ref must be a physical blob or tree reference.")
        required_text(self.source_role, "binding source role", max_bytes=128)
        required_text(self.target_role, "binding target role", max_bytes=128)

    def to_dict(self) -> JsonDict:
        return {
            "content_ref": str(self.content_ref),
            "source_owner_id": self.source_owner_id,
            "source_role": self.source_role,
            "source_store_id": self.source_store_id,
            "target_role": self.target_role,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Binding":
        try:
            _exact_keys(
                payload,
                {
                    "content_ref",
                    "source_owner_id",
                    "source_role",
                    "source_store_id",
                    "target_role",
                },
                "owner derivation binding",
            )
            return cls(
                source_owner_id=payload["source_owner_id"],
                source_store_id=payload["source_store_id"],
                content_ref=parse_physical_content_ref(payload["content_ref"]),
                source_role=payload["source_role"],
                target_role=payload["target_role"],
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted owner derivation binding is invalid: {error}"
            ) from error


def _source_sort_key(value: SourceAnchor) -> tuple[object, ...]:
    return (
        _utf8(value.owner_id),
        value.owner_revision,
        value.owner_manifest_digest,
    )


def _binding_sort_key(value: Binding) -> tuple[object, ...]:
    return (
        _utf8(value.source_owner_id),
        _utf8(value.source_store_id),
        _utf8(str(value.content_ref)),
        _utf8(value.source_role),
        _utf8(value.target_role),
    )


def _canonical_sources(values: Sequence[SourceAnchor]) -> Tuple[SourceAnchor, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("derivation sources must be a sequence of SourceAnchor values.")
    sources = tuple(values)
    if not sources:
        raise ValueError("derivation sources must not be empty.")
    if len(sources) > MAX_DERIVATION_SOURCES:
        raise ValueError(
            f"derivation sources exceed the maximum count {MAX_DERIVATION_SOURCES}."
        )
    if any(not isinstance(item, SourceAnchor) for item in sources):
        raise TypeError("derivation sources must contain SourceAnchor values.")
    owner_ids = [item.owner_id for item in sources]
    if len(set(owner_ids)) != len(owner_ids):
        raise ValueError("derivation sources must anchor each owner exactly once.")
    return tuple(sorted(sources, key=_source_sort_key))


def _canonical_bindings(values: Sequence[Binding]) -> Tuple[Binding, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("derivation bindings must be a sequence of Binding values.")
    bindings = tuple(values)
    if not bindings:
        raise ValueError("derivation bindings must not be empty.")
    if len(bindings) > MAX_DERIVATION_BINDINGS:
        raise ValueError(
            f"derivation bindings exceed the maximum count {MAX_DERIVATION_BINDINGS}."
        )
    if any(not isinstance(item, Binding) for item in bindings):
        raise TypeError("derivation bindings must contain Binding values.")
    if len(set(bindings)) != len(bindings):
        raise ValueError("derivation bindings must not contain duplicates.")
    target_memberships = [
        (item.source_store_id, item.content_ref, item.target_role)
        for item in bindings
    ]
    if len(set(target_memberships)) != len(target_memberships):
        raise ValueError(
            "each derived target membership must map to exactly one source membership."
        )
    return tuple(sorted(bindings, key=_binding_sort_key))


@dataclass(frozen=True)
class OwnerDerivationManifest:
    """Complete, path-free provenance for one newly derived semantic owner."""

    target_owner_id: str
    target_owner_kind: str
    sources: Tuple[SourceAnchor, ...]
    bindings: Tuple[Binding, ...]

    def __post_init__(self) -> None:
        required_text(self.target_owner_id, "derivation target owner_id")
        required_text(
            self.target_owner_kind, "derivation target owner kind", max_bytes=128
        )
        sources = _canonical_sources(self.sources)
        bindings = _canonical_bindings(self.bindings)
        source_ids = {item.owner_id for item in sources}
        binding_source_ids = {item.source_owner_id for item in bindings}
        if self.target_owner_id in source_ids:
            raise ValueError("a derivation target cannot also be one of its sources.")
        if source_ids != binding_source_ids:
            raise ValueError(
                "derivation sources must equal the binding source owners exactly."
            )
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "bindings", bindings)
        if len(self.to_bytes()) > MAX_DERIVATION_MANIFEST_BYTES:
            raise ValueError("owner derivation manifest exceeds the maximum encoded size.")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def manifest_digest(self) -> str:
        return self.digest

    @property
    def target_memberships(self) -> Tuple[OwnerMembership, ...]:
        """The exact initial membership set the target owner must receive."""

        return tuple(
            OwnerMembership(
                store_id=item.source_store_id,
                content_ref=item.content_ref,
                role=item.target_role,
            )
            for item in self.bindings
        )

    def source_anchor(self, source_owner_id: str) -> SourceAnchor:
        required_text(source_owner_id, "source owner_id")
        for source in self.sources:
            if source.owner_id == source_owner_id:
                return source
        raise KeyError(source_owner_id)

    def to_dict(self) -> JsonDict:
        return {
            "bindings": [item.to_dict() for item in self.bindings],
            "schema": OWNER_DERIVATION_MANIFEST_SCHEMA,
            "sources": [item.to_dict() for item in self.sources],
            "target_owner_id": self.target_owner_id,
            "target_owner_kind": self.target_owner_kind,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerDerivationManifest":
        try:
            _exact_keys(
                payload,
                {
                    "bindings",
                    "schema",
                    "sources",
                    "target_owner_id",
                    "target_owner_kind",
                },
                "owner derivation manifest",
            )
            if payload["schema"] != OWNER_DERIVATION_MANIFEST_SCHEMA:
                raise ValueError("owner derivation manifest schema is unsupported.")
            if not isinstance(payload["sources"], list):
                raise TypeError("owner derivation sources must be a list.")
            if not isinstance(payload["bindings"], list):
                raise TypeError("owner derivation bindings must be a list.")
            result = cls(
                target_owner_id=payload["target_owner_id"],
                target_owner_kind=payload["target_owner_kind"],
                sources=tuple(
                    SourceAnchor.from_dict(item) for item in payload["sources"]
                ),
                bindings=tuple(
                    Binding.from_dict(item) for item in payload["bindings"]
                ),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("owner derivation manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted owner derivation manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "OwnerDerivationManifest":
        if not isinstance(payload, bytes):
            raise TypeError("owner derivation manifest bytes must be bytes.")
        if len(payload) > MAX_DERIVATION_MANIFEST_BYTES:
            raise RealmIntegrityError(
                "owner derivation manifest exceeds the maximum encoded size."
            )
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(
                f"Owner derivation manifest is not valid UTF-8 JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RealmIntegrityError(
                "Owner derivation manifest must encode a JSON object."
            )
        if canonical_json_bytes(value) != payload:
            raise RealmIntegrityError(
                "Owner derivation manifest bytes are not canonical JSON."
            )
        return cls.from_dict(value)


@dataclass(frozen=True)
class OwnerDerivationReceipt:
    """The independent target owner and its exact immutable provenance."""

    owner: OwnerRecord
    manifest: OwnerDerivationManifest

    def __post_init__(self) -> None:
        if not isinstance(self.owner, OwnerRecord):
            raise TypeError("derived owner must be an OwnerRecord.")
        if not isinstance(self.manifest, OwnerDerivationManifest):
            raise TypeError("derivation manifest must be an OwnerDerivationManifest.")
        if (
            self.owner.owner_id != self.manifest.target_owner_id
            or self.owner.owner_kind != self.manifest.target_owner_kind
            or self.owner.revision != 0
            or self.owner.state is not OwnerState.ACTIVE
        ):
            raise ValueError("derived owner and derivation manifest anchors differ.")

    def to_dict(self) -> JsonDict:
        return {
            "manifest": self.manifest.to_dict(),
            "owner": self.owner.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerDerivationReceipt":
        try:
            value = dict(payload)
            version = value.pop("receipt_version", 1)
            if version != 1:
                raise ValueError("owner derivation receipt_version is unsupported.")
            _exact_keys(value, {"manifest", "owner"}, "owner derivation receipt")
            return cls(
                owner=OwnerRecord.from_dict(value["owner"]),
                manifest=OwnerDerivationManifest.from_dict(value["manifest"]),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted owner derivation receipt is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class SelectionOwnerAdoptionReceipt:
    """Replayable result of adopting one exact selection as a new owner.

    The selection remains part of the canonical operation receipt.  The
    derivation manifest records the independently retained source membership,
    while this record preserves which stable user-facing coordinate authorized
    that no-copy ownership change.
    """

    selection: SelectionRef
    eligibility: SelectionEligibility
    derivation: OwnerDerivationReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.derivation is not None):
            raise ValueError("selection adoption differs from its eligibility.")
        if self.derivation is None:
            return
        manifest = self.derivation.manifest
        binding = manifest.bindings[0] if len(manifest.bindings) == 1 else None
        if (
            len(manifest.sources) != 1
            or binding is None
            or manifest.sources[0].owner_id != self.selection.source_owner_id
            or binding.source_owner_id != self.selection.source_owner_id
            or (
                self.selection.entity_ref.startswith("tree:sha256:")
                and str(binding.content_ref) != self.selection.entity_ref
            )
        ):
            raise ValueError(
                "selection adoption derivation differs from its source selection."
            )

    def to_dict(self) -> JsonDict:
        return {
            "derivation": (
                None if self.derivation is None else self.derivation.to_dict()
            ),
            "eligibility": self.eligibility.to_dict(),
            "selection": self.selection.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "SelectionOwnerAdoptionReceipt":
        try:
            value = dict(payload)
            version = value.pop("receipt_version", 1)
            if version != 1:
                raise ValueError(
                    "selection owner adoption receipt_version is unsupported."
                )
            _exact_keys(
                value,
                {"derivation", "eligibility", "selection"},
                "selection owner adoption receipt",
            )
            derivation = value["derivation"]
            if derivation is not None and not isinstance(derivation, Mapping):
                raise TypeError(
                    "selection owner adoption derivation must be an object or null."
                )
            return cls(
                selection=SelectionRef.from_dict(value["selection"]),
                eligibility=SelectionEligibility.from_dict(value["eligibility"]),
                derivation=(
                    None
                    if derivation is None
                    else OwnerDerivationReceipt.from_dict(derivation)
                ),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted selection owner adoption receipt is invalid: {error}"
            ) from error


__all__ = [
    "Binding",
    "MAX_DERIVATION_BINDINGS",
    "MAX_DERIVATION_MANIFEST_BYTES",
    "MAX_DERIVATION_SOURCES",
    "OWNER_DERIVATION_MANIFEST_SCHEMA",
    "OwnerDerivationManifest",
    "OwnerDerivationReceipt",
    "SelectionOwnerAdoptionReceipt",
    "SourceAnchor",
]
