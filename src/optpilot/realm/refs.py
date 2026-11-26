"""Frozen content identities for the internal realm service."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Union


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically for identity and request hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_digest(value: Any) -> str:
    return hashlib.sha256(b"optpilot/request/v1\0" + canonical_json_bytes(value)).hexdigest()


def _domain_digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


@dataclass(frozen=True, order=True)
class BlobRef:
    digest: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("BlobRef digest must be 64 lowercase hexadecimal characters.")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "BlobRef":
        return cls(_domain_digest(b"optpilot/blob/v1", payload))

    @classmethod
    def parse(cls, value: str) -> "BlobRef":
        prefix = "blob:sha256:"
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ValueError(f"Invalid blob ref: {value!r}.")
        return cls(value[len(prefix) :])

    def __str__(self) -> str:
        return f"blob:sha256:{self.digest}"


@dataclass(frozen=True, order=True)
class SnapshotRef:
    digest: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("SnapshotRef digest must be 64 lowercase hexadecimal characters.")

    @classmethod
    def from_manifest_bytes(cls, payload: bytes) -> "SnapshotRef":
        return cls(_domain_digest(b"optpilot/tree/v1", payload))

    @classmethod
    def parse(cls, value: str) -> "SnapshotRef":
        prefix = "tree:sha256:"
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ValueError(f"Invalid snapshot ref: {value!r}.")
        return cls(value[len(prefix) :])

    def __str__(self) -> str:
        return f"tree:sha256:{self.digest}"


@dataclass(frozen=True, order=True)
class CandidateRef:
    digest: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("CandidateRef digest must be 64 lowercase hexadecimal characters.")

    @classmethod
    def build(
        cls,
        *,
        candidate_format: str,
        spec: Mapping[str, Any],
        content_refs: Sequence["PhysicalContentRef"],
    ) -> "CandidateRef":
        if not isinstance(candidate_format, str) or not candidate_format.strip():
            raise ValueError("Candidate format must be a non-empty string.")
        if not isinstance(spec, Mapping):
            raise TypeError("Candidate spec must be a mapping.")
        if any(not isinstance(item, (BlobRef, SnapshotRef)) for item in content_refs):
            raise TypeError("Candidate content refs must be blob or tree refs.")
        envelope = {
            "schema": "optpilot.candidate.v1",
            "format": candidate_format,
            "spec": dict(spec),
            # A content closure is a set.  Reordering or repeating the same ref
            # cannot create a distinct candidate identity.
            "content_refs": sorted({str(item) for item in content_refs}),
        }
        return cls(_domain_digest(b"optpilot/candidate/v1", canonical_json_bytes(envelope)))

    @classmethod
    def parse(cls, value: str) -> "CandidateRef":
        prefix = "candidate:sha256:"
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ValueError(f"Invalid candidate ref: {value!r}.")
        return cls(value[len(prefix) :])

    def __str__(self) -> str:
        return f"candidate:sha256:{self.digest}"


PhysicalContentRef = Union[BlobRef, SnapshotRef]
ContentRef = Union[BlobRef, SnapshotRef, CandidateRef]


def parse_content_ref(value: str) -> ContentRef:
    if not isinstance(value, str):
        raise ValueError(f"Unsupported content ref: {value!r}.")
    if value.startswith("blob:sha256:"):
        return BlobRef.parse(value)
    if value.startswith("tree:sha256:"):
        return SnapshotRef.parse(value)
    if value.startswith("candidate:sha256:"):
        return CandidateRef.parse(value)
    raise ValueError(f"Unsupported content ref: {value!r}.")


def parse_physical_content_ref(value: str) -> PhysicalContentRef:
    reference = parse_content_ref(value)
    if not isinstance(reference, (BlobRef, SnapshotRef)):
        raise ValueError(f"Reference is not physical stored content: {value!r}.")
    return reference
