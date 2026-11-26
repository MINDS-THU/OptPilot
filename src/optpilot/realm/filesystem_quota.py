"""Shared portable bounds for managed writable filesystem trees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


@dataclass(frozen=True, order=True)
class FilesystemQuota:
    """Portable logical limits for one managed writable tree."""

    max_entries: int
    max_file_bytes: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        _positive_int(self.max_entries, "filesystem quota max_entries")
        _positive_int(self.max_file_bytes, "filesystem quota max_file_bytes")
        _positive_int(self.max_total_bytes, "filesystem quota max_total_bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_entries": self.max_entries,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FilesystemQuota":
        if not isinstance(payload, Mapping):
            raise TypeError("filesystem quota must be a mapping.")
        expected = {
            "max_entries",
            "max_file_bytes",
            "max_total_bytes",
        }
        actual = set(payload)
        if actual != expected:
            raise ValueError(
                "filesystem quota fields differ; "
                f"missing={sorted(expected - actual)!r}, "
                f"extra={sorted(actual - expected)!r}."
            )
        result = cls(
            max_entries=payload["max_entries"],
            max_file_bytes=payload["max_file_bytes"],
            max_total_bytes=payload["max_total_bytes"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("filesystem quota is not canonical.")
        return result


__all__ = ["FilesystemQuota"]
