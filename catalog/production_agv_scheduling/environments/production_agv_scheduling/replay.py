"""Public exact-seed replay helper for trace-aware methods."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from simulation_runner import run_policy_once


def replay_candidate(
    *,
    candidate_dir: str | Path,
    settings: dict,
    seed: int,
    database_path: str | Path,
) -> dict[str, Any]:
    """Replay a candidate at one seed and write its SQLite trace.

    Only the exact ``database_path`` file may be replaced.  All simulator state
    remains local to this call, and no broker or other network service is used.
    """

    return run_policy_once(
        candidate_dir=candidate_dir,
        settings=settings,
        seed=seed,
        database_path=database_path,
    )

