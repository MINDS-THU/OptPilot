"""Validated public wrapper around the extracted event-driven engine."""

from __future__ import annotations

from collections.abc import Mapping

from .rescheduling_engine import EventDrivenReschedulingEngine


class RollingMILPController(EventDrivenReschedulingEngine):
    """Run either the monolithic or two-stage rolling-MILP policy."""

    def __init__(self, simulation, settings):
        if not isinstance(settings, Mapping):
            raise TypeError("RollingMILPController settings must be a mapping.")
        variant = str(settings.get("variant", "")).strip().lower()
        if variant not in {"monolithic", "two_stage"}:
            raise ValueError("Rolling-MILP variant must be 'monolithic' or 'two_stage'.")
        expected_two_stage = variant == "two_stage"
        configured_two_stage = bool(settings.get("use_two_stage_decomposition", expected_two_stage))
        if configured_two_stage != expected_two_stage:
            raise ValueError(
                "Candidate variant and use_two_stage_decomposition disagree; "
                "the baseline identity must remain explicit."
            )
        super().__init__(simulation=simulation, **dict(settings))


__all__ = ["RollingMILPController"]
