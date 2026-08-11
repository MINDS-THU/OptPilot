"""Static evaluation of a Factorio production-line design.

The candidate is one file, ``production_line.json``. It is handed to the
vendored benchmark validator (``fd_core``), which returns a pass/fail verdict,
the ids of every failed check, and a recipe-based cost breakdown. This module
turns that verdict into OptPilot metrics.

Metric design notes:

* ``failed_check_count`` is the primary search signal — it degrades smoothly
  as a design gets closer to valid, whereas ``static_valid`` is all-or-nothing.
* the six ``failed_*`` family counters localise the remaining work (schema,
  recipe, geometry, logistics, power, terrain) so a method can steer.
* ``total_entity_cost`` is reported but is a poor sole objective: the design
  self-declares its logistic robot count, which dominates cost (about 84% of
  the bundled example), so minimising cost alone drives robots to zero without
  making the factory better. Use it as a tie-breaker among valid designs.

No production rate is reported. The static checks cannot derive throughput;
``actual_rate`` exists only in the benchmark's Factorio execution mode, which
is not part of this environment (see the package README).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

from fd_core.tasks import get_task
from fd_core.validation import validate_production_line


CANDIDATE_FILE = "production_line.json"

# The validator's dotted check ids group by leading family number.
_CHECK_FAMILIES = {
    "V1": "failed_schema",
    "V2": "failed_recipe",
    "V3": "failed_geometry",
    "V4": "failed_logistics",
    "V5": "failed_power",
    "V6": "failed_terrain",
}


def _resolve_task_id(settings: Mapping[str, Any]) -> str:
    """Per-launch input wins over the environment's declared default."""

    inputs = settings.get("inputs")
    if isinstance(inputs, Mapping) and inputs.get("task_id"):
        return str(inputs["task_id"])
    return str(settings.get("task_id") or "iron_gear_low_easy")


def _candidate_root(candidate_runtime: Mapping[str, Any], context: Mapping[str, Any]) -> Path:
    """Resolve the staged candidate directory the retained runner prepared."""

    workspace = Path(str(context.get("workspace") or ".")).resolve()
    return Path(
        str((candidate_runtime or {}).get("candidateRoot") or workspace / "candidate")
    ).resolve()


def _read_design(root: Path) -> tuple[Any, str]:
    """Return the parsed design, or the raw text when it is not valid JSON.

    Malformed JSON is deliberately forwarded to the validator rather than
    raised: the benchmark scores it as a failed V1.1 check, which is exactly
    the feedback a design method needs on its next turn.
    """

    path = root / CANDIDATE_FILE
    if not path.is_file():
        return {}, f"{CANDIDATE_FILE} is missing from the candidate."
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text), ""
    except json.JSONDecodeError:
        return text, ""


def evaluate(
    candidate_runtime: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, float]:
    settings = context.get("settings") or {}
    task = get_task(_resolve_task_id(settings))
    design, _note = _read_design(_candidate_root(candidate_runtime, context))

    result = validate_production_line(design, task.to_validation_config())

    families = {name: 0.0 for name in _CHECK_FAMILIES.values()}
    for check_id in result.failed_checks:
        family = _CHECK_FAMILIES.get(str(check_id).split(".", 1)[0].upper())
        if family:
            families[family] += 1.0

    cost = result.metrics.get("cost") or {}
    entity_counts = cost.get("entity_counts") or {}

    return {
        "static_valid": 1.0 if result.passed else 0.0,
        "failed_check_count": float(len(result.failed_checks)),
        "warning_count": float(len(result.warnings)),
        "total_entity_cost": float(cost.get("total_cost") or 0.0),
        "entity_count": float(sum(entity_counts.values())) if entity_counts else 0.0,
        **families,
    }
