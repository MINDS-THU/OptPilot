"""Pure records and deterministic whole-tree workspace assembly.

This module deliberately has no ledger, filesystem, projection, or content-store
side effects.  It binds exact Realm selections to already verified immutable
tree manifests and compiles the content identity that a later transactional
workspace-creation service may retain.

V1 unions package roots at their existing relative paths.  Directory ancestors
may be shared, but every other overlap is rejected.  In particular, two files
at the same path conflict even when their bytes are identical; only an
identical immutable root is deduplicated before the union is compiled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._validation import lower_hex_digest, required_text
from .manifests import TreeEntry, TreeManifest, validate_portable_path
from .refs import SnapshotRef, canonical_json_bytes, request_digest
from .selections import SelectionRef


WORKSPACE_FOCUS_SCHEMA = "optpilot.workspace-focus.v1"
WORKSPACE_REQUEST_SOURCE_SCHEMA = "optpilot.workspace-request-source.v1"
WORKSPACE_SELECTION_SEED_SCHEMA = "optpilot.workspace-selection-seed.v1"
WORKSPACE_SOURCE_ANCHOR_SCHEMA = "optpilot.workspace-source-anchor.v1"
WORKSPACE_SEED_SOURCE_SCHEMA = "optpilot.workspace-seed-source.v1"
WORKSPACE_SEED_SCHEMA = "optpilot.workspace-seed.v1"
WORKSPACE_ASSEMBLY_REQUEST_SCHEMA = "optpilot.workspace-assembly-request.v1"
WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA = "optpilot.workspace-assembly-lineage.v1"
WORKSPACE_ASSEMBLY_RESULT_SCHEMA = "optpilot.workspace-assembly-result.v1"

WORKSPACE_ASSEMBLY_OUTCOMES = frozenset({"adopt", "union"})

# These command-level limits are deliberately no wider than the downstream
# content-composition and durable-lineage contracts.  The conservative lineage
# envelope below accounts for maximum-width store ids and one distinct root per
# source, so a semantic request accepted here cannot fail later merely because
# its canonical lineage does not fit the 64 KiB workspace record.
MAX_WORKSPACE_ASSEMBLY_SOURCES = 256
MAX_WORKSPACE_ASSEMBLY_FOCUSES = 256
MAX_WORKSPACE_ASSEMBLY_REQUEST_BYTES = 64 * 1024
MAX_WORKSPACE_ASSEMBLY_LINEAGE_BYTES = 64 * 1024

_STORE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _store_id(value: Any) -> str:
    if not isinstance(value, str) or not _STORE_ID_RE.fullmatch(value):
        raise ValueError(
            "workspace source store_id must use 1-128 ASCII letters, digits, "
            "dot, underscore, or dash."
        )
    return value


def _canonical_tuple(values: Sequence[Any], *, label: str) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence.")
    keyed: dict[bytes, Any] = {}
    for value in values:
        key = canonical_json_bytes(value.to_dict())
        keyed.setdefault(key, value)
    return tuple(keyed[key] for key in sorted(keyed))


def _tree_manifest_from_dict(payload: Mapping[str, Any]) -> TreeManifest:
    if not isinstance(payload, Mapping):
        raise TypeError("workspace source tree_manifest must be a mapping.")
    return TreeManifest.from_bytes(canonical_json_bytes(dict(payload)))


def _exact_tree_ref(selection: SelectionRef) -> SnapshotRef:
    if not isinstance(selection, SelectionRef):
        raise TypeError("workspace source selection must be a SelectionRef.")
    if selection.relative_path is not None:
        raise ValueError(
            "workspace assembly requires a whole-tree selection, not a nested path."
        )
    try:
        return SnapshotRef.parse(selection.entity_ref)
    except ValueError as error:
        raise ValueError(
            "workspace source selection must name an exact immutable tree root."
        ) from error


@dataclass(frozen=True)
class WorkspaceFocus:
    """One package-relative subject retained as source lineage.

    A focus does not narrow the assembled content.  Whole source roots are
    always adopted or unioned; focuses only explain which entries motivated
    the workspace (for example, the selected environment and method configs).
    """

    kind: str
    focus_id: str
    relative_path: str

    def __post_init__(self) -> None:
        required_text(self.kind, "workspace focus kind", max_bytes=128)
        required_text(self.focus_id, "workspace focus id", max_bytes=512)
        validate_portable_path(self.relative_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus_id": self.focus_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "schema": WORKSPACE_FOCUS_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceFocus":
        _exact_keys(
            payload,
            {"focus_id", "kind", "relative_path", "schema"},
            "workspace focus",
        )
        if payload["schema"] != WORKSPACE_FOCUS_SCHEMA:
            raise ValueError("workspace focus schema is unsupported.")
        result = cls(
            kind=payload["kind"],
            focus_id=payload["focus_id"],
            relative_path=payload["relative_path"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace focus is not canonical.")
        return result


@dataclass(frozen=True)
class WorkspaceRequestSource:
    """One exact source and its package-relative intent in a semantic request.

    This record is bindable before resolving content placement.  It contains
    neither a store choice nor manifest bytes.
    """

    selection: SelectionRef
    focuses: tuple[WorkspaceFocus, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        selection: SelectionRef,
        focuses: Sequence[WorkspaceFocus] = (),
    ) -> "WorkspaceRequestSource":
        return cls(
            selection=selection,
            focuses=_canonical_tuple(focuses, label="workspace request focuses"),
        )

    def __post_init__(self) -> None:
        _exact_tree_ref(self.selection)
        canonical_focuses = _canonical_tuple(
            self.focuses, label="workspace request focuses"
        )
        if self.focuses != canonical_focuses:
            raise ValueError("workspace request focuses must be canonical and unique.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "focuses": [focus.to_dict() for focus in self.focuses],
            "schema": WORKSPACE_REQUEST_SOURCE_SCHEMA,
            "selection": self.selection.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceRequestSource":
        _exact_keys(
            payload,
            {"focuses", "schema", "selection"},
            "workspace request source",
        )
        if payload["schema"] != WORKSPACE_REQUEST_SOURCE_SCHEMA:
            raise ValueError("workspace request source schema is unsupported.")
        if not isinstance(payload["focuses"], list):
            raise TypeError("workspace request source focuses must be a list.")
        result = cls(
            selection=SelectionRef.from_dict(payload["selection"]),
            focuses=tuple(
                WorkspaceFocus.from_dict(item) for item in payload["focuses"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace request source is not canonical.")
        return result


def _canonical_request_sources(
    sources: Sequence[WorkspaceRequestSource],
) -> tuple[WorkspaceRequestSource, ...]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("workspace selection seed sources must be a sequence.")
    by_selection: dict[str, WorkspaceRequestSource] = {}
    for source in sources:
        if not isinstance(source, WorkspaceRequestSource):
            raise TypeError(
                "workspace selection seed sources must be WorkspaceRequestSource records."
            )
        key = source.selection.selection_digest
        previous = by_selection.get(key)
        if previous is None:
            by_selection[key] = source
            continue
        if previous.selection != source.selection:
            raise ValueError(
                "one workspace selection digest identifies inconsistent request sources."
            )
        by_selection[key] = WorkspaceRequestSource(
            selection=source.selection,
            focuses=_canonical_tuple(
                (*previous.focuses, *source.focuses),
                label="workspace request focuses",
            ),
        )
    return tuple(
        sorted(
            by_selection.values(),
            key=lambda item: canonical_json_bytes(item.to_dict()),
        )
    )


@dataclass(frozen=True)
class WorkspaceSelectionSeed:
    """Canonical pre-resolution selections bound by an assembly request."""

    sources: tuple[WorkspaceRequestSource, ...]

    @classmethod
    def build(
        cls, sources: Sequence[WorkspaceRequestSource]
    ) -> "WorkspaceSelectionSeed":
        return cls(_canonical_request_sources(sources))

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("workspace selection seed requires at least one source.")
        if len(self.sources) > MAX_WORKSPACE_ASSEMBLY_SOURCES:
            raise ValueError(
                "workspace selection seed exceeds the 256-source assembly limit."
            )
        focus_count = sum(len(source.focuses) for source in self.sources)
        if focus_count > MAX_WORKSPACE_ASSEMBLY_FOCUSES:
            raise ValueError(
                "workspace selection seed exceeds the 256-focus lineage limit."
            )
        canonical = _canonical_request_sources(self.sources)
        if self.sources != canonical:
            raise ValueError(
                "workspace selection seed sources must be canonical and unique."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORKSPACE_SELECTION_SEED_SCHEMA,
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceSelectionSeed":
        _exact_keys(payload, {"schema", "sources"}, "workspace selection seed")
        if payload["schema"] != WORKSPACE_SELECTION_SEED_SCHEMA:
            raise ValueError("workspace selection seed schema is unsupported.")
        if not isinstance(payload["sources"], list):
            raise TypeError("workspace selection seed sources must be a list.")
        result = cls(
            tuple(WorkspaceRequestSource.from_dict(item) for item in payload["sources"])
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace selection seed is not canonical.")
        return result


@dataclass(frozen=True)
class WorkspaceSourceAnchor:
    """Provider-independent anchor for one exact immutable source root."""

    selection: SelectionRef
    store_id: str
    root_ref: SnapshotRef
    focuses: tuple[WorkspaceFocus, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        selection: SelectionRef,
        store_id: str,
        focuses: Sequence[WorkspaceFocus] = (),
    ) -> "WorkspaceSourceAnchor":
        root_ref = _exact_tree_ref(selection)
        return cls(
            selection=selection,
            store_id=store_id,
            root_ref=root_ref,
            focuses=_canonical_tuple(focuses, label="workspace source focuses"),
        )

    def __post_init__(self) -> None:
        selected_root = _exact_tree_ref(self.selection)
        _store_id(self.store_id)
        if not isinstance(self.root_ref, SnapshotRef):
            raise TypeError("workspace source root_ref must be a SnapshotRef.")
        if selected_root != self.root_ref:
            raise ValueError(
                "workspace source root_ref differs from its exact selection."
            )
        canonical_focuses = _canonical_tuple(
            self.focuses, label="workspace source focuses"
        )
        if self.focuses != canonical_focuses:
            raise ValueError("workspace source focuses must be canonical and unique.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "focuses": [focus.to_dict() for focus in self.focuses],
            "root_ref": str(self.root_ref),
            "schema": WORKSPACE_SOURCE_ANCHOR_SCHEMA,
            "selection": self.selection.to_dict(),
            "store_id": self.store_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceSourceAnchor":
        _exact_keys(
            payload,
            {"focuses", "root_ref", "schema", "selection", "store_id"},
            "workspace source anchor",
        )
        if payload["schema"] != WORKSPACE_SOURCE_ANCHOR_SCHEMA:
            raise ValueError("workspace source anchor schema is unsupported.")
        if not isinstance(payload["focuses"], list):
            raise TypeError("workspace source anchor focuses must be a list.")
        result = cls(
            selection=SelectionRef.from_dict(payload["selection"]),
            store_id=payload["store_id"],
            root_ref=SnapshotRef.parse(payload["root_ref"]),
            focuses=tuple(
                WorkspaceFocus.from_dict(item) for item in payload["focuses"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace source anchor is not canonical.")
        return result


@dataclass(frozen=True)
class WorkspaceSeedSource:
    """One exact source anchor paired with its verified immutable manifest."""

    anchor: WorkspaceSourceAnchor
    tree_manifest: TreeManifest

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, WorkspaceSourceAnchor):
            raise TypeError("workspace seed source anchor is invalid.")
        if not isinstance(self.tree_manifest, TreeManifest):
            raise TypeError("workspace seed source tree_manifest is invalid.")
        if self.tree_manifest.snapshot_ref != self.anchor.root_ref:
            raise ValueError(
                "workspace source tree manifest differs from its exact root_ref."
            )
        paths = {entry.path for entry in self.tree_manifest.entries}
        missing = tuple(
            focus.relative_path
            for focus in self.anchor.focuses
            if focus.relative_path not in paths
        )
        if missing:
            raise ValueError(
                "workspace source focuses are absent from the immutable tree: "
                + ", ".join(repr(path) for path in missing)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.to_dict(),
            "schema": WORKSPACE_SEED_SOURCE_SCHEMA,
            "tree_manifest": self.tree_manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceSeedSource":
        _exact_keys(
            payload,
            {"anchor", "schema", "tree_manifest"},
            "workspace seed source",
        )
        if payload["schema"] != WORKSPACE_SEED_SOURCE_SCHEMA:
            raise ValueError("workspace seed source schema is unsupported.")
        result = cls(
            anchor=WorkspaceSourceAnchor.from_dict(payload["anchor"]),
            tree_manifest=_tree_manifest_from_dict(payload["tree_manifest"]),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace seed source is not canonical.")
        return result


def _canonical_seed_sources(
    sources: Sequence[WorkspaceSeedSource],
) -> tuple[WorkspaceSeedSource, ...]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("workspace seed sources must be a sequence.")
    by_selection: dict[str, WorkspaceSeedSource] = {}
    for source in sources:
        if not isinstance(source, WorkspaceSeedSource):
            raise TypeError(
                "workspace seed sources must be WorkspaceSeedSource records."
            )
        key = source.anchor.selection.selection_digest
        previous = by_selection.get(key)
        if previous is None:
            by_selection[key] = source
            continue
        if (
            previous.anchor.store_id != source.anchor.store_id
            or previous.anchor.root_ref != source.anchor.root_ref
            or previous.tree_manifest != source.tree_manifest
        ):
            raise ValueError(
                "one exact workspace source selection has inconsistent root evidence."
            )
        focuses = _canonical_tuple(
            (*previous.anchor.focuses, *source.anchor.focuses),
            label="workspace source focuses",
        )
        by_selection[key] = WorkspaceSeedSource(
            anchor=WorkspaceSourceAnchor(
                selection=source.anchor.selection,
                store_id=source.anchor.store_id,
                root_ref=source.anchor.root_ref,
                focuses=focuses,
            ),
            tree_manifest=source.tree_manifest,
        )
    return tuple(
        sorted(
            by_selection.values(),
            key=lambda item: canonical_json_bytes(item.anchor.to_dict()),
        )
    )


@dataclass(frozen=True)
class WorkspaceSeed:
    """Canonical post-resolution manifest and placement evidence."""

    sources: tuple[WorkspaceSeedSource, ...]

    @classmethod
    def build(cls, sources: Sequence[WorkspaceSeedSource]) -> "WorkspaceSeed":
        return cls(_canonical_seed_sources(sources))

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("workspace seed requires at least one exact source.")
        canonical = _canonical_seed_sources(self.sources)
        if self.sources != canonical:
            raise ValueError("workspace seed sources must be canonical and unique.")
        stores = {source.anchor.store_id for source in self.sources}
        if len(stores) != 1:
            raise ValueError(
                "workspace assembly requires every exact source in one content store."
            )

    @property
    def store_id(self) -> str:
        return self.sources[0].anchor.store_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORKSPACE_SEED_SCHEMA,
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceSeed":
        _exact_keys(payload, {"schema", "sources"}, "workspace seed")
        if payload["schema"] != WORKSPACE_SEED_SCHEMA:
            raise ValueError("workspace seed schema is unsupported.")
        if not isinstance(payload["sources"], list):
            raise TypeError("workspace seed sources must be a list.")
        result = cls(
            tuple(WorkspaceSeedSource.from_dict(item) for item in payload["sources"])
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace seed is not canonical.")
        return result


@dataclass(frozen=True)
class WorkspaceAssemblyRequest:
    """Actor-bound semantic request for a future workspace creation command."""

    operation_id: str
    actor_principal_id: str
    workspace_id: str
    owner_id: str
    title: str
    seed: WorkspaceSelectionSeed

    def __post_init__(self) -> None:
        required_text(
            self.operation_id, "workspace assembly operation id", max_bytes=512
        )
        required_text(
            self.actor_principal_id,
            "workspace assembly actor principal id",
            max_bytes=512,
        )
        required_text(self.workspace_id, "target workspace id", max_bytes=512)
        required_text(self.owner_id, "target workspace owner id", max_bytes=512)
        required_text(self.title, "workspace title", max_bytes=512)
        if not isinstance(self.seed, WorkspaceSelectionSeed):
            raise TypeError("workspace assembly seed must be a WorkspaceSelectionSeed.")
        if self.owner_id in {
            source.selection.source_owner_id for source in self.seed.sources
        }:
            raise ValueError(
                "target workspace owner must be independent from every source owner."
            )
        encoded_request = canonical_json_bytes(self.to_dict())
        if len(encoded_request) > MAX_WORKSPACE_ASSEMBLY_REQUEST_BYTES:
            raise ValueError(
                "workspace assembly request exceeds the 64 KiB encoded-request limit."
            )
        if (
            len(_workspace_assembly_lineage_envelope_bytes(self))
            > MAX_WORKSPACE_ASSEMBLY_LINEAGE_BYTES
        ):
            raise ValueError(
                "workspace assembly seed cannot fit the 64 KiB durable-lineage limit."
            )

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_principal_id": self.actor_principal_id,
            "operation_id": self.operation_id,
            "owner_id": self.owner_id,
            "schema": WORKSPACE_ASSEMBLY_REQUEST_SCHEMA,
            "seed": self.seed.to_dict(),
            "title": self.title,
            "workspace_id": self.workspace_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceAssemblyRequest":
        _exact_keys(
            payload,
            {
                "actor_principal_id",
                "operation_id",
                "owner_id",
                "schema",
                "seed",
                "title",
                "workspace_id",
            },
            "workspace assembly request",
        )
        if payload["schema"] != WORKSPACE_ASSEMBLY_REQUEST_SCHEMA:
            raise ValueError("workspace assembly request schema is unsupported.")
        result = cls(
            operation_id=payload["operation_id"],
            actor_principal_id=payload["actor_principal_id"],
            workspace_id=payload["workspace_id"],
            owner_id=payload["owner_id"],
            title=payload["title"],
            seed=WorkspaceSelectionSeed.from_dict(payload["seed"]),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace assembly request is not canonical.")
        return result


def _workspace_assembly_lineage_envelope_bytes(
    request: WorkspaceAssemblyRequest,
) -> bytes:
    """Return a conservative canonical lineage encoding for semantic intent.

    Resolution may add only a bounded store id and one immutable root per
    source.  Assuming both at their maximum widths, and assuming every source
    root is distinct, produces an encoding at least as large as any lineage
    that can later be compiled from this request.
    """

    store_id = "s" * 128
    root_refs = tuple(
        f"tree:sha256:{index:064x}" for index in range(len(request.seed.sources))
    )
    sources = []
    for index, source in enumerate(request.seed.sources):
        sources.append(
            {
                "focuses": [focus.to_dict() for focus in source.focuses],
                "root_ref": root_refs[index],
                "schema": WORKSPACE_SOURCE_ANCHOR_SCHEMA,
                "selection": source.selection.to_dict(),
                "store_id": store_id,
            }
        )
    return canonical_json_bytes(
        {
            "assembly_digest": "0" * 64,
            "distinct_root_refs": list(root_refs),
            "final_root_ref": root_refs[0],
            "outcome": "adopt" if len(root_refs) == 1 else "union",
            "owner_id": request.owner_id,
            "request_digest": "0" * 64,
            "schema": WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA,
            "sources": sources,
            "store_id": store_id,
            "workspace_id": request.workspace_id,
        }
    )


def _assembly_lineage_payload(
    *,
    workspace_id: str,
    owner_id: str,
    request_digest_value: str,
    outcome: str,
    store_id: str,
    sources: tuple[WorkspaceSourceAnchor, ...],
    distinct_root_refs: tuple[SnapshotRef, ...],
    final_root_ref: SnapshotRef,
) -> dict[str, Any]:
    return {
        "distinct_root_refs": [str(root_ref) for root_ref in distinct_root_refs],
        "final_root_ref": str(final_root_ref),
        "outcome": outcome,
        "owner_id": owner_id,
        "request_digest": request_digest_value,
        "schema": WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA,
        "sources": [source.to_dict() for source in sources],
        "store_id": store_id,
        "workspace_id": workspace_id,
    }


@dataclass(frozen=True)
class WorkspaceAssemblyLineage:
    """Self-contained revision lineage for one assembled workspace root."""

    workspace_id: str
    owner_id: str
    request_digest: str
    outcome: str
    store_id: str
    sources: tuple[WorkspaceSourceAnchor, ...]
    distinct_root_refs: tuple[SnapshotRef, ...]
    final_root_ref: SnapshotRef
    assembly_digest: str

    @classmethod
    def build(
        cls,
        *,
        request: WorkspaceAssemblyRequest,
        seed: WorkspaceSeed,
        outcome: str,
        final_root_ref: SnapshotRef,
    ) -> "WorkspaceAssemblyLineage":
        if not isinstance(request, WorkspaceAssemblyRequest):
            raise TypeError("workspace assembly lineage request is invalid.")
        if not isinstance(seed, WorkspaceSeed):
            raise TypeError("workspace assembly lineage seed must be a WorkspaceSeed.")
        roots = tuple(
            sorted(
                {source.anchor.root_ref for source in seed.sources},
                key=lambda item: str(item).encode("utf-8"),
            )
        )
        sources = tuple(source.anchor for source in seed.sources)
        payload = _assembly_lineage_payload(
            workspace_id=request.workspace_id,
            owner_id=request.owner_id,
            request_digest_value=request.digest,
            outcome=outcome,
            store_id=seed.store_id,
            sources=sources,
            distinct_root_refs=roots,
            final_root_ref=final_root_ref,
        )
        return cls(
            workspace_id=request.workspace_id,
            owner_id=request.owner_id,
            request_digest=request.digest,
            outcome=outcome,
            store_id=seed.store_id,
            sources=sources,
            distinct_root_refs=roots,
            final_root_ref=final_root_ref,
            assembly_digest=request_digest(payload),
        )

    def __post_init__(self) -> None:
        required_text(self.workspace_id, "lineage workspace id", max_bytes=512)
        required_text(self.owner_id, "lineage workspace owner id", max_bytes=512)
        lower_hex_digest(self.request_digest, "workspace assembly request digest")
        if self.outcome not in WORKSPACE_ASSEMBLY_OUTCOMES:
            raise ValueError("workspace assembly lineage outcome is unsupported.")
        _store_id(self.store_id)
        if not self.sources:
            raise ValueError("workspace assembly lineage requires source anchors.")
        if any(
            not isinstance(source, WorkspaceSourceAnchor) for source in self.sources
        ):
            raise TypeError("workspace assembly lineage sources are invalid.")
        canonical_sources = tuple(
            sorted(
                self.sources,
                key=lambda item: canonical_json_bytes(item.to_dict()),
            )
        )
        if self.sources != canonical_sources or len(
            {source.selection.selection_digest for source in self.sources}
        ) != len(self.sources):
            raise ValueError(
                "workspace assembly lineage sources must be canonical and unique."
            )
        if any(source.store_id != self.store_id for source in self.sources):
            raise ValueError(
                "workspace assembly lineage spans multiple content stores."
            )
        expected_roots = tuple(
            sorted(
                {source.root_ref for source in self.sources},
                key=lambda item: str(item).encode("utf-8"),
            )
        )
        if self.distinct_root_refs != expected_roots:
            raise ValueError(
                "workspace assembly lineage roots must be canonical and complete."
            )
        if not isinstance(self.final_root_ref, SnapshotRef):
            raise TypeError("workspace assembly lineage final root is invalid.")
        expected_outcome = "adopt" if len(expected_roots) == 1 else "union"
        if self.outcome != expected_outcome:
            raise ValueError(
                "workspace assembly lineage outcome differs from its source roots."
            )
        if self.outcome == "adopt" and self.final_root_ref != expected_roots[0]:
            raise ValueError(
                "workspace assembly lineage adopted the wrong source root."
            )
        expected_digest = request_digest(
            _assembly_lineage_payload(
                workspace_id=self.workspace_id,
                owner_id=self.owner_id,
                request_digest_value=self.request_digest,
                outcome=self.outcome,
                store_id=self.store_id,
                sources=self.sources,
                distinct_root_refs=self.distinct_root_refs,
                final_root_ref=self.final_root_ref,
            )
        )
        if self.assembly_digest != expected_digest:
            raise ValueError(
                "workspace assembly lineage digest differs from its aggregate."
            )
        if (
            len(canonical_json_bytes(self.to_dict()))
            > MAX_WORKSPACE_ASSEMBLY_LINEAGE_BYTES
        ):
            raise ValueError("workspace assembly lineage exceeds 64 KiB.")

    @property
    def digest(self) -> str:
        return self.assembly_digest

    def to_dict(self) -> dict[str, Any]:
        result = _assembly_lineage_payload(
            workspace_id=self.workspace_id,
            owner_id=self.owner_id,
            request_digest_value=self.request_digest,
            outcome=self.outcome,
            store_id=self.store_id,
            sources=self.sources,
            distinct_root_refs=self.distinct_root_refs,
            final_root_ref=self.final_root_ref,
        )
        result["assembly_digest"] = self.assembly_digest
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceAssemblyLineage":
        _exact_keys(
            payload,
            {
                "assembly_digest",
                "distinct_root_refs",
                "final_root_ref",
                "outcome",
                "owner_id",
                "request_digest",
                "schema",
                "sources",
                "store_id",
                "workspace_id",
            },
            "workspace assembly lineage",
        )
        if payload["schema"] != WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA:
            raise ValueError("workspace assembly lineage schema is unsupported.")
        if not isinstance(payload["sources"], list) or not isinstance(
            payload["distinct_root_refs"], list
        ):
            raise TypeError("workspace assembly lineage arrays are invalid.")
        result = cls(
            workspace_id=payload["workspace_id"],
            owner_id=payload["owner_id"],
            request_digest=payload["request_digest"],
            outcome=payload["outcome"],
            store_id=payload["store_id"],
            sources=tuple(
                WorkspaceSourceAnchor.from_dict(item) for item in payload["sources"]
            ),
            distinct_root_refs=tuple(
                SnapshotRef.parse(item) for item in payload["distinct_root_refs"]
            ),
            final_root_ref=SnapshotRef.parse(payload["final_root_ref"]),
            assembly_digest=payload["assembly_digest"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace assembly lineage is not canonical.")
        return result


class WorkspaceAssemblyConflict(ValueError):
    """A deterministic whole-tree union conflict."""

    def __init__(
        self,
        *,
        code: str,
        path: str,
        other_path: str,
        left_root_ref: SnapshotRef,
        right_root_ref: SnapshotRef,
    ) -> None:
        self.code = code
        self.path = path
        self.other_path = other_path
        self.left_root_ref = left_root_ref
        self.right_root_ref = right_root_ref
        if code == "portable-path":
            detail = f"portable paths {other_path!r} and {path!r} collide"
        else:
            detail = f"path {path!r} has a {code} conflict"
        super().__init__(
            f"Workspace whole-tree union rejected: {detail} between "
            f"{left_root_ref} and {right_root_ref}."
        )


class WorkspaceAssemblyEvidenceMismatch(ValueError):
    """Resolved manifests or placements do not match the semantic request."""


@dataclass(frozen=True)
class WorkspaceAssemblyResult:
    """Pure compiled root and exact lineage for workspace creation."""

    outcome: str
    request_digest: str
    store_id: str
    root_ref: SnapshotRef
    tree_manifest: TreeManifest
    lineage: WorkspaceAssemblyLineage
    source_tree_manifests: tuple[TreeManifest, ...]

    def __post_init__(self) -> None:
        if self.outcome not in WORKSPACE_ASSEMBLY_OUTCOMES:
            raise ValueError("workspace assembly outcome is unsupported.")
        lower_hex_digest(self.request_digest, "workspace assembly request digest")
        _store_id(self.store_id)
        if not isinstance(self.root_ref, SnapshotRef):
            raise TypeError("workspace assembly root_ref must be a SnapshotRef.")
        if not isinstance(self.tree_manifest, TreeManifest):
            raise TypeError("workspace assembly tree_manifest must be a TreeManifest.")
        if self.tree_manifest.snapshot_ref != self.root_ref:
            raise ValueError("workspace assembly root_ref differs from its manifest.")
        if not isinstance(self.lineage, WorkspaceAssemblyLineage):
            raise TypeError("workspace assembly lineage is invalid.")
        if self.lineage.store_id != self.store_id:
            raise ValueError("workspace assembly result and lineage stores differ.")
        if self.lineage.request_digest != self.request_digest:
            raise ValueError("workspace assembly result and lineage requests differ.")
        if self.lineage.outcome != self.outcome:
            raise ValueError("workspace assembly result and lineage outcomes differ.")
        if self.lineage.final_root_ref != self.root_ref:
            raise ValueError("workspace assembly result and lineage roots differ.")
        expected_outcome = (
            "adopt" if len(self.lineage.distinct_root_refs) == 1 else "union"
        )
        if self.outcome != expected_outcome:
            raise ValueError(
                "workspace assembly outcome differs from its distinct source roots."
            )
        if (
            self.outcome == "adopt"
            and self.root_ref != self.lineage.distinct_root_refs[0]
        ):
            raise ValueError(
                "adopted workspace root differs from its exact source root."
            )
        if not self.source_tree_manifests or any(
            not isinstance(manifest, TreeManifest)
            for manifest in self.source_tree_manifests
        ):
            raise TypeError(
                "workspace assembly source_tree_manifests must contain trees."
            )
        canonical_source_manifests = tuple(
            sorted(
                self.source_tree_manifests,
                key=lambda manifest: str(manifest.snapshot_ref).encode("utf-8"),
            )
        )
        source_roots = tuple(
            manifest.snapshot_ref for manifest in canonical_source_manifests
        )
        if (
            self.source_tree_manifests != canonical_source_manifests
            or len(set(source_roots)) != len(source_roots)
            or source_roots != self.lineage.distinct_root_refs
        ):
            raise ValueError(
                "workspace assembly source manifests must be canonical and match "
                "every distinct source root."
            )

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage": self.lineage.to_dict(),
            "outcome": self.outcome,
            "request_digest": self.request_digest,
            "root_ref": str(self.root_ref),
            "schema": WORKSPACE_ASSEMBLY_RESULT_SCHEMA,
            "source_tree_manifests": [
                manifest.to_dict() for manifest in self.source_tree_manifests
            ],
            "store_id": self.store_id,
            "tree_manifest": self.tree_manifest.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceAssemblyResult":
        _exact_keys(
            payload,
            {
                "lineage",
                "outcome",
                "request_digest",
                "root_ref",
                "schema",
                "source_tree_manifests",
                "store_id",
                "tree_manifest",
            },
            "workspace assembly result",
        )
        if payload["schema"] != WORKSPACE_ASSEMBLY_RESULT_SCHEMA:
            raise ValueError("workspace assembly result schema is unsupported.")
        if not isinstance(payload["source_tree_manifests"], list):
            raise TypeError(
                "workspace assembly result source_tree_manifests must be a list."
            )
        result = cls(
            outcome=payload["outcome"],
            request_digest=payload["request_digest"],
            store_id=payload["store_id"],
            root_ref=SnapshotRef.parse(payload["root_ref"]),
            tree_manifest=_tree_manifest_from_dict(payload["tree_manifest"]),
            lineage=WorkspaceAssemblyLineage.from_dict(payload["lineage"]),
            source_tree_manifests=tuple(
                _tree_manifest_from_dict(item)
                for item in payload["source_tree_manifests"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("workspace assembly result is not canonical.")
        return result


def compile_workspace_assembly(
    request: WorkspaceAssemblyRequest,
    resolved_seed: WorkspaceSeed,
) -> WorkspaceAssemblyResult:
    """Compile verified evidence for a previously bindable semantic request.

    Identical ``(store_id, root_ref)`` inputs are deduplicated for content
    assembly while every distinct selection anchor remains in lineage.  The
    request/evidence comparison is exact and one-to-one; resolution cannot add,
    omit, replace, or retarget a requested selection or focus.
    """

    if not isinstance(request, WorkspaceAssemblyRequest):
        raise TypeError("request must be a WorkspaceAssemblyRequest.")
    if not isinstance(resolved_seed, WorkspaceSeed):
        raise TypeError("resolved_seed must be a WorkspaceSeed.")
    _match_request_to_resolved_seed(request.seed, resolved_seed)
    manifests_by_root: dict[SnapshotRef, TreeManifest] = {}
    for source in resolved_seed.sources:
        manifests_by_root.setdefault(source.anchor.root_ref, source.tree_manifest)
    ordered_roots = tuple(
        sorted(manifests_by_root, key=lambda item: str(item).encode("utf-8"))
    )
    if len(ordered_roots) == 1:
        manifest = manifests_by_root[ordered_roots[0]]
        outcome = "adopt"
    else:
        manifest = _compile_whole_tree_union(
            tuple((root_ref, manifests_by_root[root_ref]) for root_ref in ordered_roots)
        )
        outcome = "union"
    lineage = WorkspaceAssemblyLineage.build(
        request=request,
        seed=resolved_seed,
        outcome=outcome,
        final_root_ref=manifest.snapshot_ref,
    )
    return WorkspaceAssemblyResult(
        outcome=outcome,
        request_digest=request.digest,
        store_id=resolved_seed.store_id,
        root_ref=manifest.snapshot_ref,
        tree_manifest=manifest,
        lineage=lineage,
        source_tree_manifests=tuple(
            manifests_by_root[root_ref] for root_ref in ordered_roots
        ),
    )


def validate_workspace_assembly_result(
    request: WorkspaceAssemblyRequest,
    result: WorkspaceAssemblyResult,
) -> None:
    """Recompile exact source manifests and require the canonical whole-tree result.

    This is intentionally suitable for invocation at the trusted transactional
    finalization boundary.  A content-composition proof establishes that all
    referenced blobs were authorized, while this check independently proves
    their paths, directories, and executable metadata are the deterministic
    whole-root union rather than an arbitrary rearrangement.
    """

    if not isinstance(request, WorkspaceAssemblyRequest):
        raise TypeError("request must be a WorkspaceAssemblyRequest.")
    if not isinstance(result, WorkspaceAssemblyResult):
        raise TypeError("result must be a WorkspaceAssemblyResult.")
    manifests_by_root = {
        manifest.snapshot_ref: manifest for manifest in result.source_tree_manifests
    }
    try:
        resolved_seed = WorkspaceSeed.build(
            tuple(
                WorkspaceSeedSource(
                    anchor=anchor,
                    tree_manifest=manifests_by_root[anchor.root_ref],
                )
                for anchor in result.lineage.sources
            )
        )
    except KeyError as error:  # pragma: no cover - guarded by result validation
        raise WorkspaceAssemblyEvidenceMismatch(
            "workspace assembly result omitted an exact source manifest."
        ) from error
    expected = compile_workspace_assembly(request, resolved_seed)
    if expected != result:
        raise WorkspaceAssemblyEvidenceMismatch(
            "workspace assembly result is not the deterministic whole-tree union."
        )


def _match_request_to_resolved_seed(
    requested_seed: WorkspaceSelectionSeed,
    resolved_seed: WorkspaceSeed,
) -> None:
    requested = {
        source.selection.selection_digest: source for source in requested_seed.sources
    }
    resolved = {
        source.anchor.selection.selection_digest: source
        for source in resolved_seed.sources
    }
    if set(requested) != set(resolved):
        missing = sorted(set(requested) - set(resolved))
        extra = sorted(set(resolved) - set(requested))
        raise WorkspaceAssemblyEvidenceMismatch(
            "workspace resolution evidence does not match requested selections; "
            f"missing={missing!r}, extra={extra!r}."
        )
    for digest in sorted(requested):
        request_source = requested[digest]
        evidence_source = resolved[digest]
        if request_source.selection != evidence_source.anchor.selection:
            raise WorkspaceAssemblyEvidenceMismatch(
                "workspace resolution evidence changed an exact requested selection."
            )
        if request_source.focuses != evidence_source.anchor.focuses:
            raise WorkspaceAssemblyEvidenceMismatch(
                "workspace resolution evidence changed requested focus lineage for "
                f"selection {digest}."
            )


def _compile_whole_tree_union(
    roots: tuple[tuple[SnapshotRef, TreeManifest], ...],
) -> TreeManifest:
    entries_by_path: dict[str, tuple[TreeEntry, SnapshotRef]] = {}
    paths_by_portable_key: dict[str, str] = {}
    for root_ref, manifest in roots:
        for entry in manifest.entries:
            portable_key = "/".join(
                component.casefold() for component in entry.path.split("/")
            )
            previous_path = paths_by_portable_key.get(portable_key)
            if previous_path is not None and previous_path != entry.path:
                _, previous_root = entries_by_path[previous_path]
                raise WorkspaceAssemblyConflict(
                    code="portable-path",
                    path=entry.path,
                    other_path=previous_path,
                    left_root_ref=previous_root,
                    right_root_ref=root_ref,
                )
            previous = entries_by_path.get(entry.path)
            if previous is None:
                paths_by_portable_key[portable_key] = entry.path
                entries_by_path[entry.path] = (entry, root_ref)
                continue
            previous_entry, previous_root = previous
            if previous_entry.kind == "directory" and entry.kind == "directory":
                continue
            code = (
                "file-file"
                if previous_entry.kind == entry.kind == "file"
                else "file-directory"
            )
            raise WorkspaceAssemblyConflict(
                code=code,
                path=entry.path,
                other_path=entry.path,
                left_root_ref=previous_root,
                right_root_ref=root_ref,
            )
    return TreeManifest.build(tuple(value[0] for value in entries_by_path.values()))


__all__ = [
    "MAX_WORKSPACE_ASSEMBLY_FOCUSES",
    "MAX_WORKSPACE_ASSEMBLY_LINEAGE_BYTES",
    "MAX_WORKSPACE_ASSEMBLY_REQUEST_BYTES",
    "MAX_WORKSPACE_ASSEMBLY_SOURCES",
    "WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA",
    "WORKSPACE_ASSEMBLY_OUTCOMES",
    "WORKSPACE_ASSEMBLY_REQUEST_SCHEMA",
    "WORKSPACE_ASSEMBLY_RESULT_SCHEMA",
    "WORKSPACE_FOCUS_SCHEMA",
    "WORKSPACE_REQUEST_SOURCE_SCHEMA",
    "WORKSPACE_SELECTION_SEED_SCHEMA",
    "WORKSPACE_SEED_SCHEMA",
    "WORKSPACE_SEED_SOURCE_SCHEMA",
    "WORKSPACE_SOURCE_ANCHOR_SCHEMA",
    "WorkspaceAssemblyConflict",
    "WorkspaceAssemblyEvidenceMismatch",
    "WorkspaceAssemblyLineage",
    "WorkspaceAssemblyRequest",
    "WorkspaceAssemblyResult",
    "WorkspaceFocus",
    "WorkspaceRequestSource",
    "WorkspaceSelectionSeed",
    "WorkspaceSeed",
    "WorkspaceSeedSource",
    "WorkspaceSourceAnchor",
    "compile_workspace_assembly",
    "validate_workspace_assembly_result",
]
