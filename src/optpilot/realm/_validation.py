"""Small validation helpers shared by persisted realm value records."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def required_text(value: Any, label: str, *, max_bytes: int = 512) -> str:
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


def optional_text(value: Any, label: str, *, max_bytes: int = 512) -> str | None:
    if value is None:
        return None
    return required_text(value, label, max_bytes=max_bytes)


def nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def finite_time(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite timestamp.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite timestamp.")
    return result


def lower_hex_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _LOWER_HEX_DIGEST.fullmatch(value):
        raise ValueError(f"{label} must be a 64-character lowercase hexadecimal digest.")
    return value


def freeze_json(value: Any, *, label: str = "metadata", depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError(f"{label} exceeds the maximum nesting depth.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number.")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, child in value.items():
            key = required_text(key, f"{label} key", max_bytes=256)
            frozen[key] = freeze_json(child, label=label, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(child, label=label, depth=depth + 1) for child in value)
    raise ValueError(f"{label} contains a non-JSON value of type {type(value).__name__}.")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value
