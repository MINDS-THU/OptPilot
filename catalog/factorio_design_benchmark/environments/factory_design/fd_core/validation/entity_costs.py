"""
Entity cost table for benchmark-side cost reporting.

This module defines a reusable scalar cost for supported entities based on
official Factorio Wiki recipes / total raw information. The benchmark no
longer treats inventory as a hard validity constraint; instead, these costs
are used as a performance metric for accepted designs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ..schemas.production_line import ProductionLine


# ============================================================
# Cost model metadata
# ============================================================


COST_MODEL_METADATA: Dict[str, Any] = {
    "version": "factorio_recipe_cost_v1",
    "description": (
        "Scalar entity cost derived from official Factorio Wiki total-raw data "
        "or recursive recipe decomposition. Values are used only as cost metrics, "
        "not as hard validation constraints."
    ),
    "primitive_unit_costs": {
        "craft_time": 0.25,
        "iron_plate": 1.0,
        "copper_plate": 1.0,
        "steel_plate": 5.0,
        "plastic_bar": 2.0,
        "stone_brick": 1.0,
        "stone": 1.0,
        "sulfuric_acid": 0.05,
        "lubricant": 0.05,
    },
}


def _cost(raw_basis: Mapping[str, float], unit_costs: Mapping[str, float]) -> float:
    return sum(unit_costs[key] * amount for key, amount in raw_basis.items())


_PRIMITIVE_UNIT_COSTS: Dict[str, float] = dict(COST_MODEL_METADATA["primitive_unit_costs"])

_INTERMEDIATE_UNIT_COSTS: Dict[str, float] = {}

_INTERMEDIATE_UNIT_COSTS["electronic_circuit"] = _cost(
    {"craft_time": 1.25, "copper_plate": 1.5, "iron_plate": 1.0},
    _PRIMITIVE_UNIT_COSTS,
)

_INTERMEDIATE_UNIT_COSTS["advanced_circuit"] = _cost(
    {
        "craft_time": 9.5,
        "copper_plate": 5.0,
        "plastic_bar": 2.0,
        "electronic_circuit": 2.0,
    },
    {**_PRIMITIVE_UNIT_COSTS, **_INTERMEDIATE_UNIT_COSTS},
)

_INTERMEDIATE_UNIT_COSTS["battery"] = _cost(
    {
        "craft_time": 4.0,
        "iron_plate": 1.0,
        "copper_plate": 1.0,
        "sulfuric_acid": 20.0,
    },
    _PRIMITIVE_UNIT_COSTS,
)

_INTERMEDIATE_UNIT_COSTS["engine_unit"] = _cost(
    {"craft_time": 11.5, "iron_plate": 4.0, "steel_plate": 1.0},
    _PRIMITIVE_UNIT_COSTS,
)

_INTERMEDIATE_UNIT_COSTS["electric_engine_unit"] = _cost(
    {
        "craft_time": 12.5,
        "iron_plate": 3.0,
        "electronic_circuit": 1.0,
        "engine_unit": 2.0,
        "lubricant": 15.0,
    },
    {**_PRIMITIVE_UNIT_COSTS, **_INTERMEDIATE_UNIT_COSTS},
)

_INTERMEDIATE_UNIT_COSTS["flying_robot_frame"] = _cost(
    {
        "craft_time": 23.75,
        "battery": 2.0,
        "copper_plate": 4.5,
        "electronic_circuit": 1.0,
        "electric_engine_unit": 3.0,
        "steel_plate": 1.0,
    },
    {**_PRIMITIVE_UNIT_COSTS, **_INTERMEDIATE_UNIT_COSTS},
)


@dataclass(frozen=True)
class EntityCostEntry:
    scalar_cost: float
    raw_basis: Dict[str, float]
    source_url: str
    note: str = ""


def _entry(raw_basis: Mapping[str, float], source_url: str, note: str = "") -> EntityCostEntry:
    return EntityCostEntry(
        scalar_cost=_cost(raw_basis, {**_PRIMITIVE_UNIT_COSTS, **_INTERMEDIATE_UNIT_COSTS}),
        raw_basis=dict(raw_basis),
        source_url=source_url,
        note=note,
    )


ENTITY_COST_TABLE: Dict[str, EntityCostEntry] = {
    "electric-mining-drill": _entry(
        {"craft_time": 8.25, "copper_plate": 4.5, "iron_plate": 23.0},
        "https://wiki.factorio.com/Electric_mining_drill",
    ),
    "electric-furnace": _entry(
        {
            "craft_time": 52.5,
            "copper_plate": 25.0,
            "iron_plate": 10.0,
            "plastic_bar": 10.0,
            "steel_plate": 10.0,
            "stone_brick": 10.0,
        },
        "https://wiki.factorio.com/Electric_furnace",
    ),
    "assembling-machine-1": _entry(
        {"craft_time": 4.25, "copper_plate": 4.5, "iron_plate": 22.0},
        "https://wiki.factorio.com/Assembling_machine_1",
    ),
    "assembling-machine-2": _entry(
        {"craft_time": 13.5, "copper_plate": 9.0, "iron_plate": 35.0, "steel_plate": 2.0},
        "https://wiki.factorio.com/Assembling_machine_2",
    ),
    "inserter": _entry(
        {"craft_time": 2.25, "copper_plate": 1.5, "iron_plate": 4.0},
        "https://wiki.factorio.com/Inserter",
    ),
    "long-handed-inserter": _entry(
        {"craft_time": 3.25, "copper_plate": 1.5, "iron_plate": 7.0},
        "https://wiki.factorio.com/Long-handed_inserter",
    ),
    "logistic-chest-passive-provider": _entry(
        {
            "craft_time": 14.25,
            "copper_plate": 9.5,
            "iron_plate": 5.0,
            "plastic_bar": 2.0,
            "steel_plate": 8.0,
        },
        "https://wiki.factorio.com/Passive_provider_chest",
    ),
    "logistic-chest-requester": _entry(
        {
            "craft_time": 14.25,
            "copper_plate": 9.5,
            "iron_plate": 5.0,
            "plastic_bar": 2.0,
            "steel_plate": 8.0,
        },
        "https://wiki.factorio.com/Requester_chest",
    ),
    "logistic-chest-active-provider": _entry(
        {
            "craft_time": 14.25,
            "copper_plate": 9.5,
            "iron_plate": 5.0,
            "plastic_bar": 2.0,
            "steel_plate": 8.0,
        },
        "https://wiki.factorio.com/Active_provider_chest",
        note="Uses the same base recipe family as other logistics chests.",
    ),
    "logistic-chest-storage": _entry(
        {
            "craft_time": 14.25,
            "copper_plate": 9.5,
            "iron_plate": 5.0,
            "plastic_bar": 2.0,
            "steel_plate": 8.0,
        },
        "https://wiki.factorio.com/Storage_chest",
        note="Uses the same base recipe family as other logistics chests.",
    ),
    "logistic-chest-buffer": _entry(
        {
            "craft_time": 14.25,
            "copper_plate": 9.5,
            "iron_plate": 5.0,
            "plastic_bar": 2.0,
            "steel_plate": 8.0,
        },
        "https://wiki.factorio.com/Buffer_chest",
        note="Uses the same base recipe family as other logistics chests.",
    ),
    "roboport": _entry(
        {
            "craft_time": 455.0,
            "copper_plate": 225.0,
            "iron_plate": 180.0,
            "plastic_bar": 90.0,
            "steel_plate": 45.0,
        },
        "https://wiki.factorio.com/Roboport",
    ),
    "medium-electric-pole": _entry(
        {"craft_time": 2.0, "copper_plate": 1.0, "iron_plate": 2.0, "steel_plate": 2.0},
        "https://wiki.factorio.com/Medium_electric_pole",
    ),
    "big-electric-pole": _entry(
        {"craft_time": 3.5, "copper_plate": 2.0, "iron_plate": 4.0, "steel_plate": 5.0},
        "https://wiki.factorio.com/Big_electric_pole",
    ),
    "substation": _entry(
        {
            "craft_time": 49.5,
            "copper_plate": 28.0,
            "iron_plate": 10.0,
            "plastic_bar": 10.0,
            "steel_plate": 10.0,
        },
        "https://wiki.factorio.com/Substation",
    ),
    "offshore-pump": _entry(
        {"craft_time": 3.0, "iron_plate": 7.0},
        "https://wiki.factorio.com/Offshore_pump",
    ),
    "boiler": _entry(
        {"craft_time": 3.0, "iron_plate": 4.0, "stone": 5.0},
        "https://wiki.factorio.com/Boiler",
    ),
    "steam-engine": _entry(
        {"craft_time": 7.0, "iron_plate": 31.0},
        "https://wiki.factorio.com/Steam_engine",
    ),
    "pipe": _entry(
        {"craft_time": 0.5, "iron_plate": 1.0},
        "https://wiki.factorio.com/Pipe",
    ),
    "iron-chest": _entry(
        {"craft_time": 0.5, "iron_plate": 8.0},
        "https://wiki.factorio.com/Iron_chest",
    ),
    "logistic-robot": _entry(
        {"craft_time": 0.5, "advanced_circuit": 2.0, "flying_robot_frame": 1.0},
        "https://wiki.factorio.com/Logistic_robot",
        note="Scalar cost uses recursive recipe decomposition via advanced circuits and flying robot frames.",
    ),
}


def get_entity_cost(entity_type: str) -> Optional[float]:
    entry = ENTITY_COST_TABLE.get(entity_type)
    return entry.scalar_cost if entry else None


def count_entities_in_production_line(production_line: ProductionLine) -> Dict[str, int]:
    counts: Dict[str, int] = {}

    for block in production_line.blocks.values():
        for machine in block.machines:
            counts[machine.type] = counts.get(machine.type, 0) + 1
        for inserter in block.inserters:
            counts[inserter.type] = counts.get(inserter.type, 0) + 1
        for chest in block.logistics_chests:
            counts[chest.type] = counts.get(chest.type, 0) + 1
        for container in block.containers:
            counts[container.type] = counts.get(container.type, 0) + 1

    for entity in production_line.global_entities:
        counts[entity.type] = counts.get(entity.type, 0) + 1

    if production_line.logistic_robot_count and production_line.logistic_robot_count > 0:
        counts["logistic-robot"] = (
            counts.get("logistic-robot", 0) + production_line.logistic_robot_count
        )

    return dict(sorted(counts.items()))


def summarize_entity_costs(production_line: ProductionLine) -> Dict[str, Any]:
    entity_counts = count_entities_in_production_line(production_line)
    breakdown: Dict[str, Dict[str, float]] = {}
    unknown_entity_types = []
    total_cost = 0.0

    for entity_type, count in entity_counts.items():
        entry = ENTITY_COST_TABLE.get(entity_type)
        if entry is None:
            unknown_entity_types.append(entity_type)
            continue

        subtotal = count * entry.scalar_cost
        total_cost += subtotal
        breakdown[entity_type] = {
            "count": count,
            "unit_cost": entry.scalar_cost,
            "subtotal": subtotal,
        }

    return {
        "cost_model": COST_MODEL_METADATA["version"],
        "total_cost": total_cost,
        "entity_counts": entity_counts,
        "entity_cost_breakdown": breakdown,
        "unknown_entity_types": sorted(unknown_entity_types),
    }
