"""Reusable retained execution controls for canonical local runs.

The profile is operational input, not mutable dispatcher state.  A creator
binds it into the durable plan that creates a run, and every initial or
recovery dispatcher reuses that exact retained value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


RUN_EXECUTION_PROFILE_SCHEMA = "optpilot.run-execution-profile.v1"
MAX_RUN_EXECUTION_CONTROL_SECONDS = 86_400.0


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a positive finite number.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    if normalized > MAX_RUN_EXECUTION_CONTROL_SECONDS:
        raise ValueError(
            f"{name} must not exceed {MAX_RUN_EXECUTION_CONTROL_SECONDS:g} seconds."
        )
    return normalized


@dataclass(frozen=True)
class RunExecutionProfile:
    """Immutable local-provider controls shared by canonical run creators."""

    controller_ttl_seconds: float = 300.0
    heartbeat_interval_seconds: float | None = None
    attempt_ttl_seconds: float = 300.0
    method_start_timeout_seconds: float = 10.0
    method_request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        controller_ttl = _positive_seconds(
            self.controller_ttl_seconds, "controller_ttl_seconds"
        )
        object.__setattr__(
            self,
            "controller_ttl_seconds",
            controller_ttl,
        )
        heartbeat_interval = (
            controller_ttl / 3.0
            if self.heartbeat_interval_seconds is None
            else _positive_seconds(
                self.heartbeat_interval_seconds,
                "heartbeat_interval_seconds",
            )
        )
        if heartbeat_interval >= controller_ttl:
            raise ValueError(
                "heartbeat_interval_seconds must be less than "
                "controller_ttl_seconds."
            )
        object.__setattr__(
            self,
            "heartbeat_interval_seconds",
            heartbeat_interval,
        )
        object.__setattr__(
            self,
            "attempt_ttl_seconds",
            _positive_seconds(self.attempt_ttl_seconds, "attempt_ttl_seconds"),
        )
        object.__setattr__(
            self,
            "method_start_timeout_seconds",
            _positive_seconds(
                self.method_start_timeout_seconds,
                "method_start_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "method_request_timeout_seconds",
            _positive_seconds(
                self.method_request_timeout_seconds,
                "method_request_timeout_seconds",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_ttl_seconds": self.attempt_ttl_seconds,
            "controller_ttl_seconds": self.controller_ttl_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "method_request_timeout_seconds": self.method_request_timeout_seconds,
            "method_start_timeout_seconds": self.method_start_timeout_seconds,
            "schema": RUN_EXECUTION_PROFILE_SCHEMA,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunExecutionProfile":
        if not isinstance(value, Mapping) or set(value) != {
            "attempt_ttl_seconds",
            "controller_ttl_seconds",
            "heartbeat_interval_seconds",
            "method_request_timeout_seconds",
            "method_start_timeout_seconds",
            "schema",
        }:
            raise ValueError("Run execution profile is malformed.")
        if value.get("schema") != RUN_EXECUTION_PROFILE_SCHEMA:
            raise ValueError("Run execution profile is unsupported.")
        if value["heartbeat_interval_seconds"] is None:
            raise ValueError(
                "Retained run execution profile must contain a concrete "
                "heartbeat interval."
            )
        numeric_keys = (
            "attempt_ttl_seconds",
            "controller_ttl_seconds",
            "heartbeat_interval_seconds",
            "method_request_timeout_seconds",
            "method_start_timeout_seconds",
        )
        if any(type(value[key]) is not float for key in numeric_keys):
            raise ValueError(
                "Retained run execution profile numbers must use canonical "
                "floating-point encoding."
            )
        result = cls(
            controller_ttl_seconds=value["controller_ttl_seconds"],
            heartbeat_interval_seconds=value["heartbeat_interval_seconds"],
            attempt_ttl_seconds=value["attempt_ttl_seconds"],
            method_start_timeout_seconds=value["method_start_timeout_seconds"],
            method_request_timeout_seconds=value["method_request_timeout_seconds"],
        )
        if result.to_dict() != dict(value):
            raise ValueError("Retained run execution profile is not canonical.")
        return result


__all__ = [
    "MAX_RUN_EXECUTION_CONTROL_SECONDS",
    "RUN_EXECUTION_PROFILE_SCHEMA",
    "RunExecutionProfile",
]
