"""Canonical semantic identity for one retained study definition.

A study definition binds a newly derived, revision-zero Realm owner to the
complete immutable inputs needed to create runs.  The owner derivation itself
is retained separately; this record anchors its exact manifest digest and the
complete :class:`RunDefinitionManifest` without introducing host paths,
provider placement, leases, or mutable workspace state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from ._validation import lower_hex_digest, nonnegative_int, required_text
from .errors import RealmIntegrityError
from .owners import OwnerRecord, OwnerState
from .refs import (
    PhysicalContentRef,
    canonical_json_bytes,
    parse_physical_content_ref,
    request_digest,
)
from .run_definition import RunDefinitionManifest
from .run_closure import ScopePath


JsonDict = Dict[str, Any]

STUDY_DEFINITION_MANIFEST_SCHEMA = "optpilot.study-definition-manifest.v1"
STUDY_DEFINITION_OWNER_KIND = "study-definition"

# The embedded run definition is independently limited to one MiB.  Its
# required-content closure is deliberately repeated here so that a persisted
# study-definition row can be checked without trusting either representation.
# Two MiB leaves room for that redundancy while keeping authority records
# bounded independently of the content stores.
MAX_STUDY_DEFINITION_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_STUDY_DEFINITION_REQUIRED_CONTENT_REFS = 2048


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _content_ref_sort_key(
    value: Tuple[str, PhysicalContentRef],
) -> tuple[bytes, bytes]:
    role, content_ref = value
    return (
        role.encode("utf-8", errors="strict"),
        str(content_ref).encode("utf-8", errors="strict"),
    )


def _decode_required_content_refs(
    payload: Any,
) -> Tuple[Tuple[str, PhysicalContentRef], ...]:
    if not isinstance(payload, list):
        raise TypeError("study definition required_content_refs must be a list.")
    if len(payload) > MAX_STUDY_DEFINITION_REQUIRED_CONTENT_REFS:
        raise ValueError("study definition requires too many semantic content refs.")
    values: list[Tuple[str, PhysicalContentRef]] = []
    for item in payload:
        _exact_keys(item, {"content_ref", "role"}, "study definition content ref")
        role = required_text(
            item["role"], "study definition content role", max_bytes=128
        )
        content_ref = parse_physical_content_ref(item["content_ref"])
        values.append((role, content_ref))
    result = tuple(values)
    if len(set(result)) != len(result):
        raise ValueError(
            "study definition required_content_refs must not contain duplicates."
        )
    if result != tuple(sorted(result, key=_content_ref_sort_key)):
        raise ValueError(
            "study definition required_content_refs must be canonically sorted."
        )
    return result


@dataclass(frozen=True)
class StudyDefinitionManifest:
    """One revision-zero owner bound to complete runnable study semantics."""

    owner_id: str
    owner_derivation_manifest_digest: str
    authored_study_config: ScopePath
    run_definition: RunDefinitionManifest
    owner_revision: int = 0

    def __post_init__(self) -> None:
        required_text(self.owner_id, "study definition owner_id")
        revision = nonnegative_int(
            self.owner_revision, "study definition owner revision"
        )
        if revision != 0:
            raise ValueError("study definition owner revision must be zero.")
        lower_hex_digest(
            self.owner_derivation_manifest_digest,
            "study definition owner derivation manifest digest",
        )
        if not isinstance(self.authored_study_config, ScopePath):
            raise TypeError("authored_study_config must be a ScopePath.")
        if not isinstance(self.run_definition, RunDefinitionManifest):
            raise TypeError("run_definition must be a RunDefinitionManifest.")
        source_scopes = {
            layer.scope
            for layer in (
                *self.run_definition.evaluation_closure.environment_revision.source_layers,
                *self.run_definition.method_revision.source_layers,
            )
        }
        if self.authored_study_config.scope not in source_scopes:
            raise ValueError(
                "authored study config scope is absent from the run definition sources."
            )
        if (
            len(self.required_content_refs)
            > MAX_STUDY_DEFINITION_REQUIRED_CONTENT_REFS
        ):
            raise ValueError("study definition requires too many semantic content refs.")
        if len(self.to_bytes()) > MAX_STUDY_DEFINITION_MANIFEST_BYTES:
            raise ValueError("study definition manifest exceeds the maximum encoded size.")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def manifest_digest(self) -> str:
        return self.digest

    @property
    def run_definition_digest(self) -> str:
        return self.run_definition.digest

    @property
    def required_content_refs(
        self,
    ) -> Tuple[Tuple[str, PhysicalContentRef], ...]:
        return self.run_definition.required_content_refs

    def _required_content_refs_dict(self) -> list[JsonDict]:
        return [
            {"content_ref": str(content_ref), "role": role}
            for role, content_ref in self.required_content_refs
        ]

    def to_dict(self) -> JsonDict:
        return {
            "authored_study_config": self.authored_study_config.to_dict(),
            "owner_derivation_manifest_digest": self.owner_derivation_manifest_digest,
            "owner_id": self.owner_id,
            "owner_revision": self.owner_revision,
            "required_content_refs": self._required_content_refs_dict(),
            "run_definition": self.run_definition.to_dict(),
            "run_definition_digest": self.run_definition.digest,
            "schema": STUDY_DEFINITION_MANIFEST_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudyDefinitionManifest":
        try:
            _exact_keys(
                payload,
                {
                    "authored_study_config",
                    "owner_derivation_manifest_digest",
                    "owner_id",
                    "owner_revision",
                    "required_content_refs",
                    "run_definition",
                    "run_definition_digest",
                    "schema",
                },
                "study definition manifest",
            )
            if payload["schema"] != STUDY_DEFINITION_MANIFEST_SCHEMA:
                raise ValueError("study definition manifest schema is unsupported.")
            required_content_refs = _decode_required_content_refs(
                payload["required_content_refs"]
            )
            run_definition = RunDefinitionManifest.from_dict(
                payload["run_definition"]
            )
            if payload["run_definition_digest"] != run_definition.digest:
                raise ValueError("run definition digest does not match.")
            if required_content_refs != run_definition.required_content_refs:
                raise ValueError("run definition required content refs do not match.")
            result = cls(
                owner_id=payload["owner_id"],
                owner_revision=payload["owner_revision"],
                owner_derivation_manifest_digest=payload[
                    "owner_derivation_manifest_digest"
                ],
                authored_study_config=ScopePath.from_dict(
                    payload["authored_study_config"]
                ),
                run_definition=run_definition,
            )
            if result.to_dict() != dict(payload):
                raise ValueError("study definition manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted study definition manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "StudyDefinitionManifest":
        if not isinstance(payload, bytes):
            raise TypeError("study definition manifest bytes must be bytes.")
        if len(payload) > MAX_STUDY_DEFINITION_MANIFEST_BYTES:
            raise RealmIntegrityError(
                "study definition manifest exceeds the maximum encoded size."
            )
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(
                f"Study definition manifest is not valid UTF-8 JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RealmIntegrityError(
                "Study definition manifest must encode a JSON object."
            )
        if canonical_json_bytes(value) != payload:
            raise RealmIntegrityError(
                "Study definition manifest bytes are not canonical JSON."
            )
        return cls.from_dict(value)


@dataclass(frozen=True)
class StudyDefinitionReceipt:
    """A retained study definition and its matching independent owner."""

    owner: OwnerRecord
    manifest: StudyDefinitionManifest

    def __post_init__(self) -> None:
        if not isinstance(self.owner, OwnerRecord):
            raise TypeError("study definition owner must be an OwnerRecord.")
        if not isinstance(self.manifest, StudyDefinitionManifest):
            raise TypeError(
                "study definition manifest must be a StudyDefinitionManifest."
            )
        if (
            self.owner.owner_id != self.manifest.owner_id
            or self.owner.owner_kind != STUDY_DEFINITION_OWNER_KIND
            or self.owner.revision != self.manifest.owner_revision
            or self.owner.state is not OwnerState.ACTIVE
        ):
            raise ValueError("study definition owner and manifest anchors differ.")

    def to_dict(self) -> JsonDict:
        return {
            "manifest": self.manifest.to_dict(),
            "owner": self.owner.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudyDefinitionReceipt":
        try:
            value = dict(payload)
            version = value.pop("receipt_version", 1)
            if version != 1:
                raise ValueError("study definition receipt_version is unsupported.")
            _exact_keys(value, {"manifest", "owner"}, "study definition receipt")
            result = cls(
                owner=OwnerRecord.from_dict(value["owner"]),
                manifest=StudyDefinitionManifest.from_dict(value["manifest"]),
            )
            if result.to_dict() != value:
                raise ValueError("study definition receipt is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted study definition receipt is invalid: {error}"
            ) from error


__all__ = [
    "MAX_STUDY_DEFINITION_MANIFEST_BYTES",
    "MAX_STUDY_DEFINITION_REQUIRED_CONTENT_REFS",
    "STUDY_DEFINITION_MANIFEST_SCHEMA",
    "STUDY_DEFINITION_OWNER_KIND",
    "StudyDefinitionManifest",
    "StudyDefinitionReceipt",
]
