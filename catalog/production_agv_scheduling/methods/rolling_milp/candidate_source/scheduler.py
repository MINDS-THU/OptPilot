"""Public entrypoint for a simulation-bound rolling-MILP candidate."""

from __future__ import annotations

from collections.abc import Mapping

from policy.controller import RollingMILPController
from policy.settings import SETTINGS


def create_controller(simulation, settings=None):
    """Create the event-driven controller for one initialized simulation.

    Candidate-owned defaults are immutable.  An evaluator may pass explicit
    overrides under ``rolling_milp``; unrelated environment settings are
    intentionally ignored.
    """

    merged = dict(SETTINGS)
    if isinstance(settings, Mapping):
        overrides = settings.get("rolling_milp", {})
        if isinstance(overrides, Mapping):
            unknown = sorted(set(overrides).difference(merged))
            if unknown:
                raise ValueError(f"Unknown rolling_milp controller settings: {unknown}")
            merged.update(overrides)
    return RollingMILPController(simulation=simulation, settings=merged)


__all__ = ["create_controller"]
