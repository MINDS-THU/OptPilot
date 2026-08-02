"""Immutable single-tree catalog package publication records."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .manifests import TreeEntry, TreeManifest, validate_portable_paths
from .owners import OwnerRecord, OwnerState
from .refs import SnapshotRef, canonical_json_bytes, request_digest


CATALOG_PACKAGE_SCHEMA = "optpilot.catalog-package.v1"
CATALOG_PACKAGE_REVISION_SCHEMA = "optpilot.catalog-package-revision.v2"
CATALOG_PACKAGE_HEAD_SCHEMA = "optpilot.catalog-package-head.v1"
CATALOG_PACKAGE_HEAD_PAGE_SCHEMA = "optpilot.catalog-package-head-page.v1"
CATALOG_PACKAGE_PUBLICATION_REQUEST_SCHEMA = (
    "optpilot.catalog-package-publication-request.v1"
)
CATALOG_PACKAGE_PUBLICATION_PROOF_SCHEMA = (
    "optpilot.catalog-package-publication-proof.v1"
)
CATALOG_PACKAGE_GOVERNANCE_OWNER_KIND = "catalog-package"
CATALOG_PACKAGE_REVISION_OWNER_KIND = "catalog-package-revision"
# Retained as the source-compatible name for the revision/content owner.  New
# code should use the explicit governance/revision names above.
CATALOG_PACKAGE_OWNER_KIND = CATALOG_PACKAGE_REVISION_OWNER_KIND
CATALOG_PACKAGE_ROOT_ROLE = "catalog-package-root"
CATALOG_PUBLICATION_ATTEMPT_OWNER_KIND = "catalog-publication-attempt"
CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE = "catalog-publication-final-root"
CATALOG_COMPOSITION_OWNER_KIND = "catalog-tree-composition"
CATALOG_COMPOSITION_ROOT_ROLE = "catalog-composed-root"
MAX_CATALOG_APPLICATIONS = 256
MAX_CATALOG_OWNED_PATHS = 2048
MAX_CATALOG_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CATALOG_HEAD_PAGE_SIZE = 200
MAX_CATALOG_PUBLICATION_REQUEST_BYTES = 1_000_000


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _catalog_package_id(value: Any, label: str = "catalog package id") -> str:
    result = required_text(value, label, max_bytes=256)
    if (
        "/" in result
        or "\\" in result
        or result.startswith((".", "~"))
    ):
        raise ValueError(
            f"{label} must be one portable, non-hidden path component."
        )
    return result


def _path_parts_casefold(path: str) -> tuple[str, ...]:
    return tuple(component.casefold() for component in path.split("/"))


def catalog_paths_overlap(left: str, right: str) -> bool:
    left_parts = _path_parts_casefold(left)
    right_parts = _path_parts_casefold(right)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def canonical_catalog_paths(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("catalog owned paths must be a sequence of strings.")
    paths = validate_portable_paths(values)
    if not paths:
        raise ValueError("catalog publication must own at least one path.")
    if len(paths) > MAX_CATALOG_OWNED_PATHS:
        raise ValueError("catalog publication owns too many paths.")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if catalog_paths_overlap(path, other):
                raise ValueError(
                    f"Catalog owned paths overlap: {path!r} and {other!r}."
                )
    return paths


def _path_within(path: str, root: str) -> bool:
    path_parts = path.split("/")
    root_parts = root.split("/")
    return (
        len(path_parts) >= len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


def _path_related_to_claim(path: str, claim: str) -> bool:
    return _path_within(path, claim) or _path_within(claim, path)


def validate_catalog_claimed_tree(
    manifest: TreeManifest,
    *,
    claims: tuple[str, ...],
    label: str,
) -> None:
    """Require one exact tree to contain only its canonical claimed paths."""

    if not isinstance(manifest, TreeManifest):
        raise TypeError("catalog claimed tree must be a TreeManifest.")
    claims = canonical_catalog_paths(claims)
    entries = {entry.path: entry for entry in manifest.entries}
    missing = tuple(path for path in claims if path not in entries)
    if missing:
        raise ValueError(
            f"{label} claims paths absent from its immutable tree: "
            + ", ".join(missing)
        )
    unclaimed = tuple(
        entry.path
        for entry in manifest.entries
        if (
            entry.kind == "file"
            and not any(_path_within(entry.path, claim) for claim in claims)
        )
        or (
            entry.kind == "directory"
            and not any(_path_related_to_claim(entry.path, claim) for claim in claims)
        )
    )
    if unclaimed:
        raise ValueError(
            f"{label} contains paths outside its ownership claims: "
            + ", ".join(unclaimed[:8])
        )


@dataclass(frozen=True)
class CatalogPackageRecord:
    """The immutable identity and stable governance anchor of one package."""

    package_id: str
    governance_owner_id: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        _catalog_package_id(self.package_id)
        required_text(
            self.governance_owner_id,
            "catalog package governance owner id",
            max_bytes=512,
        )
        required_text(
            self.created_by_principal_id,
            "catalog package creator principal id",
            max_bytes=512,
        )
        positive_int(self.created_txn_id, "catalog package creation transaction")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "catalog package created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "governance_owner_id": self.governance_owner_id,
            "package_id": self.package_id,
            "schema": CATALOG_PACKAGE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageRecord":
        try:
            _exact_keys(
                payload,
                {
                    "created_at",
                    "created_by_principal_id",
                    "created_txn_id",
                    "governance_owner_id",
                    "package_id",
                    "schema",
                },
                "catalog package record",
            )
            if payload["schema"] != CATALOG_PACKAGE_SCHEMA:
                raise ValueError("catalog package schema is unsupported.")
            result = cls(
                package_id=payload["package_id"],
                governance_owner_id=payload["governance_owner_id"],
                created_by_principal_id=payload["created_by_principal_id"],
                created_txn_id=payload["created_txn_id"],
                created_at=payload["created_at"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("catalog package record is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package record is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CatalogPackageApplication:
    """One plan publisher's exact artifact provenance and path claims."""

    publisher_id: str
    origin_revision: int
    source_owner_id: str
    source_owner_revision: int
    source_owner_manifest_digest: str
    artifact_ref: SnapshotRef
    owned_paths: tuple[str, ...]
    plan_digest: str
    validation_digest: str
    smoke_digest: str | None

    def __post_init__(self) -> None:
        required_text(self.publisher_id, "catalog publisher id", max_bytes=512)
        positive_int(self.origin_revision, "catalog application origin revision")
        required_text(
            self.source_owner_id, "catalog application source owner id", max_bytes=512
        )
        nonnegative_int(
            self.source_owner_revision, "catalog application source owner revision"
        )
        lower_hex_digest(
            self.source_owner_manifest_digest,
            "catalog application source owner manifest digest",
        )
        if not isinstance(self.artifact_ref, SnapshotRef):
            raise TypeError("catalog application artifact_ref must be a SnapshotRef.")
        object.__setattr__(self, "owned_paths", canonical_catalog_paths(self.owned_paths))
        lower_hex_digest(self.plan_digest, "catalog application plan digest")
        lower_hex_digest(
            self.validation_digest, "catalog application validation digest"
        )
        if self.smoke_digest is not None:
            lower_hex_digest(self.smoke_digest, "catalog application smoke digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_ref": str(self.artifact_ref),
            "origin_revision": self.origin_revision,
            "owned_paths": list(self.owned_paths),
            "plan_digest": self.plan_digest,
            "publisher_id": self.publisher_id,
            "smoke_digest": self.smoke_digest,
            "source_owner_id": self.source_owner_id,
            "source_owner_manifest_digest": self.source_owner_manifest_digest,
            "source_owner_revision": self.source_owner_revision,
            "validation_digest": self.validation_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageApplication":
        try:
            _exact_keys(
                payload,
                {
                    "artifact_ref",
                    "origin_revision",
                    "owned_paths",
                    "plan_digest",
                    "publisher_id",
                    "smoke_digest",
                    "source_owner_id",
                    "source_owner_manifest_digest",
                    "source_owner_revision",
                    "validation_digest",
                },
                "catalog package application",
            )
            if not isinstance(payload["owned_paths"], list):
                raise TypeError("catalog application owned_paths must be a list.")
            result = cls(
                publisher_id=payload["publisher_id"],
                origin_revision=payload["origin_revision"],
                source_owner_id=payload["source_owner_id"],
                source_owner_revision=payload["source_owner_revision"],
                source_owner_manifest_digest=payload[
                    "source_owner_manifest_digest"
                ],
                artifact_ref=SnapshotRef.parse(payload["artifact_ref"]),
                owned_paths=tuple(payload["owned_paths"]),
                plan_digest=payload["plan_digest"],
                validation_digest=payload["validation_digest"],
                smoke_digest=payload["smoke_digest"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("catalog package application is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package application is invalid: {error}"
            ) from error


def _canonical_applications(
    values: Sequence[CatalogPackageApplication],
) -> tuple[CatalogPackageApplication, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("catalog applications must be a sequence.")
    applications = tuple(values)
    if not applications or len(applications) > MAX_CATALOG_APPLICATIONS:
        raise ValueError(
            f"catalog applications must contain 1 to {MAX_CATALOG_APPLICATIONS} entries."
        )
    if any(not isinstance(item, CatalogPackageApplication) for item in applications):
        raise TypeError("catalog applications contain an invalid entry.")
    ordered = tuple(
        sorted(applications, key=lambda item: item.publisher_id.encode("utf-8"))
    )
    if applications != ordered:
        raise ValueError("catalog applications must be canonically sorted.")
    if len({item.publisher_id for item in applications}) != len(applications):
        raise ValueError("catalog applications contain a duplicate publisher.")
    claims: list[tuple[str, str]] = []
    for application in applications:
        for path in application.owned_paths:
            for publisher_id, claimed in claims:
                if catalog_paths_overlap(path, claimed):
                    raise ValueError(
                        "Catalog applications contain overlapping path claims: "
                        f"{path!r} ({application.publisher_id}) and {claimed!r} "
                        f"({publisher_id})."
                    )
            claims.append((application.publisher_id, path))
    return applications


def catalog_application_or_none(
    manifest: "CatalogPackageRevisionManifest",
    publisher_id: str,
) -> CatalogPackageApplication | None:
    try:
        return manifest.application(publisher_id)
    except KeyError:
        return None


def next_catalog_applications(
    *,
    previous_manifest: "CatalogPackageRevisionManifest | None",
    application: CatalogPackageApplication,
) -> tuple[CatalogPackageApplication, ...]:
    """Replace one publisher while preserving every other immutable origin."""

    if not isinstance(application, CatalogPackageApplication):
        raise TypeError("application must be a CatalogPackageApplication.")
    previous = () if previous_manifest is None else previous_manifest.applications
    unchanged = tuple(
        item for item in previous if item.publisher_id != application.publisher_id
    )
    for item in unchanged:
        for new_path in application.owned_paths:
            for existing_path in item.owned_paths:
                if catalog_paths_overlap(new_path, existing_path):
                    raise ValueError(
                        "Catalog applications contain overlapping path claims: "
                        f"{new_path!r} ({application.publisher_id}) and "
                        f"{existing_path!r} ({item.publisher_id})."
                    )
    return tuple(
        sorted(
            (*unchanged, application),
            key=lambda item: item.publisher_id.encode("utf-8"),
        )
    )


def compose_catalog_package_tree(
    *,
    previous_tree: TreeManifest | None,
    previous_application: CatalogPackageApplication | None,
    artifact_tree: TreeManifest,
    applications: tuple[CatalogPackageApplication, ...],
) -> TreeManifest:
    """Compute the only valid package tree for an application replacement."""

    if previous_tree is not None and not isinstance(previous_tree, TreeManifest):
        raise TypeError("previous_tree must be a TreeManifest or None.")
    if previous_application is not None and not isinstance(
        previous_application, CatalogPackageApplication
    ):
        raise TypeError(
            "previous_application must be a CatalogPackageApplication or None."
        )
    if not isinstance(artifact_tree, TreeManifest):
        raise TypeError("artifact_tree must be a TreeManifest.")
    applications = _canonical_applications(applications)
    entries: dict[str, TreeEntry] = {}
    previous_claims = (
        () if previous_application is None else previous_application.owned_paths
    )
    if previous_tree is not None:
        entries.update(
            (entry.path, entry)
            for entry in previous_tree.entries
            if not any(_path_within(entry.path, claim) for claim in previous_claims)
        )
    entries.update((entry.path, entry) for entry in artifact_tree.entries)
    active_claims = tuple(
        path for application in applications for path in application.owned_paths
    )
    pruned = tuple(
        entry
        for entry in entries.values()
        if (
            entry.kind == "file"
            and any(_path_within(entry.path, claim) for claim in active_claims)
        )
        or (
            entry.kind == "directory"
            and any(
                _path_related_to_claim(entry.path, claim) for claim in active_claims
            )
        )
    )
    result = TreeManifest.build(pruned)
    validate_catalog_claimed_tree(
        result,
        claims=active_claims,
        label="Composed catalog package",
    )
    return result


@dataclass(frozen=True)
class CatalogPackageRevisionManifest:
    """One immutable, single-tree package revision."""

    package_id: str
    governance_owner_id: str
    revision: int
    owner_id: str
    owner_derivation_manifest_digest: str
    previous_manifest_digest: str | None
    root_ref: SnapshotRef
    applications: tuple[CatalogPackageApplication, ...]
    owner_revision: int = 0

    def __post_init__(self) -> None:
        _catalog_package_id(self.package_id)
        required_text(
            self.governance_owner_id,
            "catalog package governance owner id",
            max_bytes=512,
        )
        positive_int(self.revision, "catalog package revision")
        required_text(self.owner_id, "catalog package owner id", max_bytes=512)
        if nonnegative_int(self.owner_revision, "catalog owner revision") != 0:
            raise ValueError("catalog package revision owner must remain at revision zero.")
        lower_hex_digest(
            self.owner_derivation_manifest_digest,
            "catalog owner derivation manifest digest",
        )
        if self.previous_manifest_digest is None:
            if self.revision != 1:
                raise ValueError("Only catalog revision one may omit its predecessor.")
        else:
            lower_hex_digest(
                self.previous_manifest_digest, "previous catalog manifest digest"
            )
            if self.revision == 1:
                raise ValueError("Catalog revision one cannot name a predecessor.")
        if not isinstance(self.root_ref, SnapshotRef):
            raise TypeError("catalog package root_ref must be a SnapshotRef.")
        object.__setattr__(
            self, "applications", _canonical_applications(self.applications)
        )
        if any(
            application.origin_revision > self.revision
            for application in self.applications
        ):
            raise ValueError(
                "catalog application origin revision cannot follow its manifest."
            )
        if len(self.to_bytes()) > MAX_CATALOG_MANIFEST_BYTES:
            raise ValueError("catalog package revision manifest is too large.")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def application(self, publisher_id: str) -> CatalogPackageApplication:
        for application in self.applications:
            if application.publisher_id == publisher_id:
                return application
        raise KeyError(publisher_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applications": [item.to_dict() for item in self.applications],
            "governance_owner_id": self.governance_owner_id,
            "owner_derivation_manifest_digest": self.owner_derivation_manifest_digest,
            "owner_id": self.owner_id,
            "owner_revision": self.owner_revision,
            "package_id": self.package_id,
            "previous_manifest_digest": self.previous_manifest_digest,
            "revision": self.revision,
            "root_ref": str(self.root_ref),
            "schema": CATALOG_PACKAGE_REVISION_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageRevisionManifest":
        try:
            _exact_keys(
                payload,
                {
                    "applications",
                    "governance_owner_id",
                    "owner_derivation_manifest_digest",
                    "owner_id",
                    "owner_revision",
                    "package_id",
                    "previous_manifest_digest",
                    "revision",
                    "root_ref",
                    "schema",
                },
                "catalog package revision manifest",
            )
            if payload["schema"] != CATALOG_PACKAGE_REVISION_SCHEMA:
                raise ValueError("catalog package revision schema is unsupported.")
            if not isinstance(payload["applications"], list):
                raise TypeError("catalog package applications must be a list.")
            result = cls(
                package_id=payload["package_id"],
                governance_owner_id=payload["governance_owner_id"],
                revision=payload["revision"],
                owner_id=payload["owner_id"],
                owner_revision=payload["owner_revision"],
                owner_derivation_manifest_digest=payload[
                    "owner_derivation_manifest_digest"
                ],
                previous_manifest_digest=payload["previous_manifest_digest"],
                root_ref=SnapshotRef.parse(payload["root_ref"]),
                applications=tuple(
                    CatalogPackageApplication.from_dict(item)
                    for item in payload["applications"]
                ),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("catalog package revision manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package revision manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "CatalogPackageRevisionManifest":
        if not isinstance(payload, bytes) or len(payload) > MAX_CATALOG_MANIFEST_BYTES:
            raise RealmIntegrityError("Catalog manifest bytes are invalid or too large.")
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealmIntegrityError("Catalog manifest is not valid UTF-8 JSON.") from error
        if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
            raise RealmIntegrityError("Catalog manifest bytes are not canonical JSON.")
        return cls.from_dict(value)


@dataclass(frozen=True)
class CatalogPackageHead:
    package_id: str
    revision: int
    owner_id: str
    manifest_digest: str
    updated_txn_id: int
    updated_at: float

    def __post_init__(self) -> None:
        _catalog_package_id(self.package_id)
        positive_int(self.revision, "catalog package head revision")
        required_text(self.owner_id, "catalog package head owner id", max_bytes=512)
        lower_hex_digest(self.manifest_digest, "catalog package head digest")
        positive_int(self.updated_txn_id, "catalog package head transaction")
        object.__setattr__(
            self, "updated_at", finite_time(self.updated_at, "catalog head updated_at")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "owner_id": self.owner_id,
            "package_id": self.package_id,
            "revision": self.revision,
            "schema": CATALOG_PACKAGE_HEAD_SCHEMA,
            "updated_at": self.updated_at,
            "updated_txn_id": self.updated_txn_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageHead":
        try:
            value = dict(payload)
            _exact_keys(
                value,
                {
                    "manifest_digest",
                    "owner_id",
                    "package_id",
                    "revision",
                    "schema",
                    "updated_at",
                    "updated_txn_id",
                },
                "catalog package head",
            )
            if value["schema"] != CATALOG_PACKAGE_HEAD_SCHEMA:
                raise ValueError("catalog package head schema is unsupported.")
            result = cls(
                package_id=value["package_id"],
                revision=value["revision"],
                owner_id=value["owner_id"],
                manifest_digest=value["manifest_digest"],
                updated_txn_id=value["updated_txn_id"],
                updated_at=value["updated_at"],
            )
            if result.to_dict() != value:
                raise ValueError("catalog package head is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package head is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CatalogPackagePublicationRequest:
    """Canonical semantic inputs for one replayable package publication."""

    actor_principal_id: str
    package_id: str
    publisher_id: str
    artifact_owner_id: str
    artifact_owner_revision: int
    artifact_store_id: str
    artifact_role: str
    artifact_ref: SnapshotRef
    owned_paths: tuple[str, ...]
    plan_digest: str
    validation_digest: str
    smoke_digest: str | None
    expected_head: CatalogPackageHead | None
    revision_owner_id: str

    def __post_init__(self) -> None:
        required_text(
            self.actor_principal_id,
            "catalog publication actor principal id",
            max_bytes=512,
        )
        _catalog_package_id(self.package_id)
        required_text(self.publisher_id, "catalog publisher id", max_bytes=512)
        required_text(
            self.artifact_owner_id,
            "catalog artifact owner id",
            max_bytes=512,
        )
        nonnegative_int(
            self.artifact_owner_revision,
            "catalog artifact owner revision",
        )
        required_text(
            self.artifact_store_id,
            "catalog artifact store id",
            max_bytes=128,
        )
        required_text(
            self.artifact_role,
            "catalog artifact role",
            max_bytes=128,
        )
        if not isinstance(self.artifact_ref, SnapshotRef):
            raise TypeError("catalog artifact_ref must be a SnapshotRef.")
        object.__setattr__(
            self,
            "owned_paths",
            canonical_catalog_paths(self.owned_paths),
        )
        lower_hex_digest(self.plan_digest, "catalog publication plan digest")
        lower_hex_digest(
            self.validation_digest,
            "catalog publication validation digest",
        )
        if self.smoke_digest is not None:
            lower_hex_digest(
                self.smoke_digest,
                "catalog publication smoke digest",
            )
        if self.expected_head is not None:
            if not isinstance(self.expected_head, CatalogPackageHead):
                raise TypeError(
                    "catalog expected_head must be a CatalogPackageHead or None."
                )
            if self.expected_head.package_id != self.package_id:
                raise ValueError("catalog expected_head belongs to another package.")
        required_text(
            self.revision_owner_id,
            "catalog revision owner id",
            max_bytes=512,
        )
        if self.revision_owner_id == self.artifact_owner_id or (
            self.expected_head is not None
            and self.revision_owner_id == self.expected_head.owner_id
        ):
            raise ValueError(
                "catalog revision owner must be independent of its source owners."
            )
        if (
            len(canonical_json_bytes(self.to_dict()))
            > MAX_CATALOG_PUBLICATION_REQUEST_BYTES
        ):
            raise ValueError("catalog publication request is too large.")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_principal_id": self.actor_principal_id,
            "artifact_owner_id": self.artifact_owner_id,
            "artifact_owner_revision": self.artifact_owner_revision,
            "artifact_ref": str(self.artifact_ref),
            "artifact_role": self.artifact_role,
            "artifact_store_id": self.artifact_store_id,
            "expected_head": (
                None if self.expected_head is None else self.expected_head.to_dict()
            ),
            "owned_paths": list(self.owned_paths),
            "package_id": self.package_id,
            "plan_digest": self.plan_digest,
            "publisher_id": self.publisher_id,
            "revision_owner_id": self.revision_owner_id,
            "schema": CATALOG_PACKAGE_PUBLICATION_REQUEST_SCHEMA,
            "smoke_digest": self.smoke_digest,
            "validation_digest": self.validation_digest,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "CatalogPackagePublicationRequest":
        try:
            _exact_keys(
                payload,
                {
                    "actor_principal_id",
                    "artifact_owner_id",
                    "artifact_owner_revision",
                    "artifact_ref",
                    "artifact_role",
                    "artifact_store_id",
                    "expected_head",
                    "owned_paths",
                    "package_id",
                    "plan_digest",
                    "publisher_id",
                    "revision_owner_id",
                    "schema",
                    "smoke_digest",
                    "validation_digest",
                },
                "catalog package publication request",
            )
            if payload["schema"] != CATALOG_PACKAGE_PUBLICATION_REQUEST_SCHEMA:
                raise ValueError(
                    "catalog package publication request schema is unsupported."
                )
            if not isinstance(payload["owned_paths"], list):
                raise TypeError("catalog publication owned_paths must be a list.")
            raw_expected_head = payload["expected_head"]
            if raw_expected_head is not None and not isinstance(
                raw_expected_head, Mapping
            ):
                raise TypeError(
                    "catalog publication expected_head must be an object or null."
                )
            result = cls(
                actor_principal_id=payload["actor_principal_id"],
                package_id=payload["package_id"],
                publisher_id=payload["publisher_id"],
                artifact_owner_id=payload["artifact_owner_id"],
                artifact_owner_revision=payload["artifact_owner_revision"],
                artifact_store_id=payload["artifact_store_id"],
                artifact_role=payload["artifact_role"],
                artifact_ref=SnapshotRef.parse(payload["artifact_ref"]),
                owned_paths=tuple(payload["owned_paths"]),
                plan_digest=payload["plan_digest"],
                validation_digest=payload["validation_digest"],
                smoke_digest=payload["smoke_digest"],
                expected_head=(
                    None
                    if raw_expected_head is None
                    else CatalogPackageHead.from_dict(raw_expected_head)
                ),
                revision_owner_id=payload["revision_owner_id"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError(
                    "catalog package publication request is not canonical."
                )
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package publication request is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CatalogPackagePublicationProof:
    """Exact durable facts produced by one publication preparation attempt."""

    request_digest: str
    attempt_id: str
    owner_id: str
    change_id: str
    artifact_ref: SnapshotRef
    previous_ref: SnapshotRef | None
    final_ref: SnapshotRef
    application_set_digest: str
    mode: str
    composition_request_digest: str | None

    def __post_init__(self) -> None:
        lower_hex_digest(
            self.request_digest,
            "catalog publication request digest",
        )
        required_text(self.attempt_id, "catalog publication attempt id", max_bytes=512)
        required_text(
            self.owner_id,
            "catalog publication attempt owner id",
            max_bytes=512,
        )
        required_text(
            self.change_id,
            "catalog publication attempt change id",
            max_bytes=512,
        )
        if not isinstance(self.artifact_ref, SnapshotRef):
            raise TypeError("catalog publication artifact_ref must be a SnapshotRef.")
        if self.previous_ref is not None and not isinstance(
            self.previous_ref, SnapshotRef
        ):
            raise TypeError(
                "catalog publication previous_ref must be a SnapshotRef or None."
            )
        if not isinstance(self.final_ref, SnapshotRef):
            raise TypeError("catalog publication final_ref must be a SnapshotRef.")
        lower_hex_digest(
            self.application_set_digest,
            "catalog publication application set digest",
        )
        if self.mode not in {"artifact", "previous", "composed"}:
            raise ValueError("catalog publication proof mode is unsupported.")
        if self.composition_request_digest is not None:
            lower_hex_digest(
                self.composition_request_digest,
                "catalog publication composition request digest",
            )
        if self.mode == "artifact":
            if (
                self.final_ref != self.artifact_ref
                or self.composition_request_digest is not None
            ):
                raise ValueError(
                    "artifact publication proof must adopt the exact artifact root."
                )
        elif self.mode == "previous":
            if (
                self.previous_ref is None
                or self.final_ref != self.previous_ref
                or self.composition_request_digest is not None
            ):
                raise ValueError(
                    "previous publication proof must adopt the exact previous root."
                )
        elif (
            self.composition_request_digest is None
            or self.final_ref == self.artifact_ref
            or (
                self.previous_ref is not None
                and self.final_ref == self.previous_ref
            )
        ):
            raise ValueError(
                "composed publication proof must name one distinct composed root."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_set_digest": self.application_set_digest,
            "artifact_ref": str(self.artifact_ref),
            "attempt_id": self.attempt_id,
            "change_id": self.change_id,
            "composition_request_digest": self.composition_request_digest,
            "final_ref": str(self.final_ref),
            "mode": self.mode,
            "owner_id": self.owner_id,
            "previous_ref": (
                None if self.previous_ref is None else str(self.previous_ref)
            ),
            "request_digest": self.request_digest,
            "schema": CATALOG_PACKAGE_PUBLICATION_PROOF_SCHEMA,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "CatalogPackagePublicationProof":
        try:
            _exact_keys(
                payload,
                {
                    "application_set_digest",
                    "artifact_ref",
                    "attempt_id",
                    "change_id",
                    "composition_request_digest",
                    "final_ref",
                    "mode",
                    "owner_id",
                    "previous_ref",
                    "request_digest",
                    "schema",
                },
                "catalog package publication proof",
            )
            if payload["schema"] != CATALOG_PACKAGE_PUBLICATION_PROOF_SCHEMA:
                raise ValueError(
                    "catalog package publication proof schema is unsupported."
                )
            previous_ref = payload["previous_ref"]
            result = cls(
                request_digest=payload["request_digest"],
                attempt_id=payload["attempt_id"],
                owner_id=payload["owner_id"],
                change_id=payload["change_id"],
                artifact_ref=SnapshotRef.parse(payload["artifact_ref"]),
                previous_ref=(
                    None
                    if previous_ref is None
                    else SnapshotRef.parse(previous_ref)
                ),
                final_ref=SnapshotRef.parse(payload["final_ref"]),
                application_set_digest=payload["application_set_digest"],
                mode=payload["mode"],
                composition_request_digest=payload[
                    "composition_request_digest"
                ],
            )
            if result.to_dict() != dict(payload):
                raise ValueError(
                    "catalog package publication proof is not canonical."
                )
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package publication proof is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CatalogPackageHeadPage:
    """One bounded, canonically ordered page of authorized package heads."""

    heads: tuple[CatalogPackageHead, ...]
    next_after_package_id: str | None

    def __post_init__(self) -> None:
        if isinstance(self.heads, (str, bytes)) or not isinstance(
            self.heads, Sequence
        ):
            raise TypeError("catalog package heads must be a sequence.")
        heads = tuple(self.heads)
        if any(not isinstance(item, CatalogPackageHead) for item in heads):
            raise TypeError("catalog package head page contains an invalid head.")
        if len(heads) > MAX_CATALOG_HEAD_PAGE_SIZE:
            raise ValueError(
                "catalog package head page exceeds the maximum page size."
            )
        ordered = tuple(
            sorted(heads, key=lambda item: item.package_id.encode("utf-8"))
        )
        if heads != ordered or len({item.package_id for item in heads}) != len(heads):
            raise ValueError(
                "catalog package head page must be uniquely and canonically ordered."
            )
        object.__setattr__(self, "heads", heads)
        if self.next_after_package_id is not None:
            cursor = _catalog_package_id(
                self.next_after_package_id,
                "next catalog package page cursor",
            )
            if not heads or cursor != heads[-1].package_id:
                raise ValueError(
                    "next catalog package page cursor must name the last returned head."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "heads": [item.to_dict() for item in self.heads],
            "next_after_package_id": self.next_after_package_id,
            "schema": CATALOG_PACKAGE_HEAD_PAGE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageHeadPage":
        try:
            _exact_keys(
                payload,
                {"heads", "next_after_package_id", "schema"},
                "catalog package head page",
            )
            if payload["schema"] != CATALOG_PACKAGE_HEAD_PAGE_SCHEMA:
                raise ValueError("catalog package head page schema is unsupported.")
            if not isinstance(payload["heads"], list):
                raise TypeError("catalog package head page heads must be a list.")
            result = cls(
                heads=tuple(
                    CatalogPackageHead.from_dict(item) for item in payload["heads"]
                ),
                next_after_package_id=payload["next_after_package_id"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("catalog package head page is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog package head page is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class CatalogPackageRevisionReceipt:
    package: CatalogPackageRecord
    owner: OwnerRecord
    manifest: CatalogPackageRevisionManifest
    head: CatalogPackageHead

    def __post_init__(self) -> None:
        if not isinstance(self.package, CatalogPackageRecord):
            raise TypeError("catalog package must be a CatalogPackageRecord.")
        if not isinstance(self.owner, OwnerRecord):
            raise TypeError("catalog revision owner must be an OwnerRecord.")
        if not isinstance(self.manifest, CatalogPackageRevisionManifest):
            raise TypeError("catalog revision manifest is invalid.")
        if not isinstance(self.head, CatalogPackageHead):
            raise TypeError("catalog revision head is invalid.")
        if (
            self.owner.owner_id != self.manifest.owner_id
            or self.owner.owner_kind != CATALOG_PACKAGE_OWNER_KIND
            or self.owner.revision != 0
            or self.owner.state is not OwnerState.ACTIVE
            or self.head.package_id != self.manifest.package_id
            or self.head.revision != self.manifest.revision
            or self.head.owner_id != self.manifest.owner_id
            or self.head.manifest_digest != self.manifest.digest
            or self.package.package_id != self.manifest.package_id
            or self.package.package_id != self.head.package_id
            or self.package.governance_owner_id
            != self.manifest.governance_owner_id
        ):
            raise ValueError("catalog revision receipt anchors differ.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head.to_dict(),
            "manifest": self.manifest.to_dict(),
            "owner": self.owner.to_dict(),
            "package": self.package.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CatalogPackageRevisionReceipt":
        try:
            value = dict(payload)
            version = value.pop("receipt_version", 1)
            if version != 1:
                raise ValueError("catalog revision receipt_version is unsupported.")
            _exact_keys(
                value,
                {"head", "manifest", "owner", "package"},
                "catalog revision receipt",
            )
            result = cls(
                package=CatalogPackageRecord.from_dict(value["package"]),
                owner=OwnerRecord.from_dict(value["owner"]),
                manifest=CatalogPackageRevisionManifest.from_dict(value["manifest"]),
                head=CatalogPackageHead.from_dict(value["head"]),
            )
            if result.to_dict() != value:
                raise ValueError("catalog revision receipt is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted catalog revision receipt is invalid: {error}"
            ) from error


__all__ = [
    "CATALOG_COMPOSITION_OWNER_KIND",
    "CATALOG_COMPOSITION_ROOT_ROLE",
    "CATALOG_PACKAGE_GOVERNANCE_OWNER_KIND",
    "CATALOG_PACKAGE_HEAD_PAGE_SCHEMA",
    "CATALOG_PACKAGE_HEAD_SCHEMA",
    "CATALOG_PACKAGE_OWNER_KIND",
    "CATALOG_PACKAGE_PUBLICATION_PROOF_SCHEMA",
    "CATALOG_PACKAGE_PUBLICATION_REQUEST_SCHEMA",
    "CATALOG_PACKAGE_REVISION_OWNER_KIND",
    "CATALOG_PACKAGE_REVISION_SCHEMA",
    "CATALOG_PACKAGE_ROOT_ROLE",
    "CATALOG_PACKAGE_SCHEMA",
    "CATALOG_PUBLICATION_ATTEMPT_OWNER_KIND",
    "CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE",
    "MAX_CATALOG_HEAD_PAGE_SIZE",
    "MAX_CATALOG_PUBLICATION_REQUEST_BYTES",
    "CatalogPackageApplication",
    "CatalogPackageHead",
    "CatalogPackageHeadPage",
    "CatalogPackagePublicationProof",
    "CatalogPackagePublicationRequest",
    "CatalogPackageRecord",
    "CatalogPackageRevisionManifest",
    "CatalogPackageRevisionReceipt",
    "catalog_application_or_none",
    "canonical_catalog_paths",
    "catalog_paths_overlap",
    "compose_catalog_package_tree",
    "next_catalog_applications",
    "validate_catalog_claimed_tree",
]
