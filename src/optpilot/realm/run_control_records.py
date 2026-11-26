"""Typed Realm receipts for immutable run-control persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from ..run_control_manifest import (
    RunControlManifest,
    SubmissionControlRecord,
    validate_submission_control_chain,
)
from .run_records import RunNamespaceRecord, RunRevisionRecord


def _exact_keys(
    payload: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if set(payload) != expected:
        raise ValueError(f"{label} fields differ from its canonical shape.")


def _without_receipt_version(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Remove the Realm operation envelope marker before typed decoding."""

    if payload.get("receipt_version") != 1:
        raise ValueError("receipt_version is unsupported.")
    return {key: value for key, value in payload.items() if key != "receipt_version"}


@dataclass(frozen=True)
class RunControlSnapshot:
    """One canonical manifest and its validated submission-control chain."""

    manifest: RunControlManifest
    submission_records: Tuple[SubmissionControlRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, RunControlManifest):
            raise TypeError("manifest must be a RunControlManifest.")
        records = tuple(self.submission_records)
        validate_submission_control_chain(
            records, manifest_digest=self.manifest.digest
        )
        object.__setattr__(self, "submission_records", records)

    @property
    def current_submission(self) -> SubmissionControlRecord:
        return self.submission_records[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "manifest_digest": self.manifest.digest,
            "submission_records": [
                {"digest": record.digest, "record": record.to_dict()}
                for record in self.submission_records
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunControlSnapshot":
        _exact_keys(
            payload,
            {"manifest", "manifest_digest", "submission_records"},
            "run control snapshot",
        )
        manifest = RunControlManifest.from_dict(
            payload["manifest"], expected_digest=payload["manifest_digest"]
        )
        raw_records = payload["submission_records"]
        if not isinstance(raw_records, list):
            raise TypeError("submission_records must be a list.")
        records = []
        for value in raw_records:
            _exact_keys(value, {"digest", "record"}, "submission record envelope")
            records.append(
                SubmissionControlRecord.from_dict(
                    value["record"], expected_digest=value["digest"]
                )
            )
        return cls(manifest, tuple(records))


@dataclass(frozen=True)
class RunSubmissionControlReceipt:
    """One fenced append to the submission-control chain."""

    run: RunNamespaceRecord
    revision: RunRevisionRecord
    control_index: int
    record: SubmissionControlRecord

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunNamespaceRecord):
            raise TypeError("run must be a RunNamespaceRecord.")
        if not isinstance(self.revision, RunRevisionRecord):
            raise TypeError("revision must be a RunRevisionRecord.")
        if isinstance(self.control_index, bool) or not isinstance(
            self.control_index, int
        ):
            raise TypeError("control_index must be an integer.")
        if self.control_index <= 0:
            raise ValueError("control_index must be positive.")
        if not isinstance(self.record, SubmissionControlRecord):
            raise TypeError("record must be a SubmissionControlRecord.")
        if (
            self.run.current_revision != self.revision.revision
            or self.revision.revision != self.record.run_revision
        ):
            raise ValueError("submission control receipt anchors do not agree.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "control_index": self.control_index,
            "record": self.record.to_dict(),
            "record_digest": self.record.digest,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunSubmissionControlReceipt":
        payload = _without_receipt_version(payload)
        _exact_keys(
            payload,
            {"run", "revision", "control_index", "record", "record_digest"},
            "submission control receipt",
        )
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            control_index=payload["control_index"],
            record=SubmissionControlRecord.from_dict(
                payload["record"], expected_digest=payload["record_digest"]
            ),
        )


__all__ = [
    "RunControlSnapshot",
    "RunSubmissionControlReceipt",
]
