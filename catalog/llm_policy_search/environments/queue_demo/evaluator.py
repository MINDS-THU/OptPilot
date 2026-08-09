"""Evaluator for the queue-demo template: domain hooks only.

Everything domain-independent (seeded replications, worst-run selection,
trace dump, aggregation) lives in ``des_replication.py``; this file wires
the demo simulator into it and exposes the ``exact_seed_replay``
capability the policy-search method consumes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from des_replication import evaluate_policy_candidate
from simulator import run_once


def evaluate(candidate_runtime: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    return evaluate_policy_candidate(candidate_runtime, context, run_once=run_once)


def replay_candidate(
    *,
    candidate_dir: Path,
    settings: Mapping[str, Any],
    seed: int,
    database_path: Path,
) -> Dict[str, Any]:
    """Exact-seed replay capability: one replication with its trace."""

    return dict(
        run_once(
            candidate_dir=Path(candidate_dir),
            settings=dict(settings),
            seed=int(seed),
            database_path=Path(database_path),
        )
    )
