# -*- coding: utf-8 -*-
"""
Factorio Recipe Data

Contains recipe definitions and utility functions for recipe analysis.
"""

from typing import Dict, List, Set, Any
from dataclasses import dataclass


@dataclass
class Recipe:
    """Recipe definition."""

    name: str
    inputs: Dict[str, float]  # item -> quantity
    outputs: Dict[str, float]  # item -> quantity
    time: float  # crafting time in seconds
    machine_type: str  # "furnace", "assembling-machine", "mining-drill"

    @property
    def output_rate(self) -> Dict[str, float]:
        """Output items per second (base, not accounting for machine speed)."""
        return {item: qty / self.time for item, qty in self.outputs.items()}


# ============================================================
# Recipe Definitions
# ============================================================

RECIPES: Dict[str, Recipe] = {
    # === Raw Materials (Mining) ===
    "iron-ore": Recipe(
        name="iron-ore",
        inputs={},
        outputs={"iron-ore": 1},
        time=1.0,  # Base mining time
        machine_type="mining-drill",
    ),
    "copper-ore": Recipe(
        name="copper-ore",
        inputs={},
        outputs={"copper-ore": 1},
        time=1.0,
        machine_type="mining-drill",
    ),
    "stone": Recipe(
        name="stone",
        inputs={},
        outputs={"stone": 1},
        time=1.0,
        machine_type="mining-drill",
    ),
    "coal": Recipe(
        name="coal",
        inputs={},
        outputs={"coal": 1},
        time=1.0,
        machine_type="mining-drill",
    ),
    # === Smelting ===
    "iron-plate": Recipe(
        name="iron-plate",
        inputs={"iron-ore": 1},
        outputs={"iron-plate": 1},
        time=3.2,
        machine_type="furnace",
    ),
    "copper-plate": Recipe(
        name="copper-plate",
        inputs={"copper-ore": 1},
        outputs={"copper-plate": 1},
        time=3.2,
        machine_type="furnace",
    ),
    "steel-plate": Recipe(
        name="steel-plate",
        inputs={"iron-plate": 5},
        outputs={"steel-plate": 1},
        time=16.0,
        machine_type="furnace",
    ),
    "stone-brick": Recipe(
        name="stone-brick",
        inputs={"stone": 2},
        outputs={"stone-brick": 1},
        time=3.2,
        machine_type="furnace",
    ),
    # === Basic Components ===
    "iron-gear-wheel": Recipe(
        name="iron-gear-wheel",
        inputs={"iron-plate": 2},
        outputs={"iron-gear-wheel": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "iron-stick": Recipe(
        name="iron-stick",
        inputs={"iron-plate": 1},
        outputs={"iron-stick": 2},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "copper-cable": Recipe(
        name="copper-cable",
        inputs={"copper-plate": 1},
        outputs={"copper-cable": 2},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "pipe": Recipe(
        name="pipe",
        inputs={"iron-plate": 1},
        outputs={"pipe": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "assembling-machine-1": Recipe(
        name="assembling-machine-1",
        inputs={"electronic-circuit": 3, "iron-gear-wheel": 5, "iron-plate": 9},
        outputs={"assembling-machine-1": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "assembling-machine-2": Recipe(
        name="assembling-machine-2",
        inputs={
            "assembling-machine-1": 1,
            "electronic-circuit": 3,
            "iron-gear-wheel": 5,
            "steel-plate": 2,
        },
        outputs={"assembling-machine-2": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    # === Circuits ===
    "electronic-circuit": Recipe(
        name="electronic-circuit",
        inputs={"iron-plate": 1, "copper-cable": 3},
        outputs={"electronic-circuit": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "advanced-circuit": Recipe(
        name="advanced-circuit",
        inputs={"electronic-circuit": 2, "copper-cable": 4, "plastic-bar": 2},
        outputs={"advanced-circuit": 1},
        time=6.0,
        machine_type="assembling-machine",
    ),
    # === Logistics ===
    "transport-belt": Recipe(
        name="transport-belt",
        inputs={"iron-plate": 1, "iron-gear-wheel": 1},
        outputs={"transport-belt": 2},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "inserter": Recipe(
        name="inserter",
        inputs={"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1},
        outputs={"inserter": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "fast-inserter": Recipe(
        name="fast-inserter",
        inputs={"inserter": 1, "iron-plate": 2, "electronic-circuit": 2},
        outputs={"fast-inserter": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    # === Science Packs ===
    "automation-science-pack": Recipe(
        name="automation-science-pack",
        inputs={"copper-plate": 1, "iron-gear-wheel": 1},
        outputs={"automation-science-pack": 1},
        time=5.0,
        machine_type="assembling-machine",
    ),
    "logistic-science-pack": Recipe(
        name="logistic-science-pack",
        inputs={"inserter": 1, "transport-belt": 1},
        outputs={"logistic-science-pack": 1},
        time=6.0,
        machine_type="assembling-machine",
    ),
    # === Military ===
    "firearm-magazine": Recipe(
        name="firearm-magazine",
        inputs={"iron-plate": 4},
        outputs={"firearm-magazine": 1},
        time=1.0,
        machine_type="assembling-machine",
    ),
    "piercing-rounds-magazine": Recipe(
        name="piercing-rounds-magazine",
        inputs={"firearm-magazine": 1, "steel-plate": 1, "copper-plate": 5},
        outputs={"piercing-rounds-magazine": 1},
        time=3.0,
        machine_type="assembling-machine",
    ),
    "grenade": Recipe(
        name="grenade",
        inputs={"coal": 10, "iron-plate": 5},
        outputs={"grenade": 1},
        time=8.0,
        machine_type="assembling-machine",
    ),
    "stone-wall": Recipe(
        name="stone-wall",
        inputs={"stone-brick": 5},
        outputs={"stone-wall": 1},
        time=0.5,
        machine_type="assembling-machine",
    ),
    "military-science-pack": Recipe(
        name="military-science-pack",
        inputs={
            "piercing-rounds-magazine": 1,
            "grenade": 1,
            "stone-wall": 2,
        },
        outputs={"military-science-pack": 2},
        time=10.0,
        machine_type="assembling-machine",
    ),
}

# Raw materials that don't need to be crafted
RAW_MATERIALS = {"iron-ore", "copper-ore", "stone", "coal", "crude-oil", "water"}


# ============================================================
# Utility Functions
# ============================================================


def get_recipe(name: str) -> Recipe:
    """Get recipe by name."""
    if name not in RECIPES:
        raise KeyError(f"Unknown recipe: {name}")
    return RECIPES[name]


def get_recipe_chain(target: str, visited: Set[str] = None) -> List[str]:
    """
    Get all recipes needed to produce target item.
    Returns recipes in dependency order (dependencies first).
    """
    if visited is None:
        visited = set()

    if target in visited or target in RAW_MATERIALS:
        return []

    visited.add(target)

    if target not in RECIPES:
        return []

    recipe = RECIPES[target]
    chain = []

    # First, get dependencies
    for input_item in recipe.inputs:
        chain.extend(get_recipe_chain(input_item, visited))

    # Then add this recipe
    chain.append(target)

    return chain


def get_required_inputs(target: str) -> Dict[str, Set[str]]:
    """
    Get all raw materials and intermediate products needed for target.

    Returns:
        Dict with keys 'raw' and 'intermediate'
    """
    chain = get_recipe_chain(target)

    raw = set()
    intermediate = set()

    for recipe_name in chain:
        recipe = RECIPES[recipe_name]
        for input_item in recipe.inputs:
            if input_item in RAW_MATERIALS:
                raw.add(input_item)
            else:
                intermediate.add(input_item)

    # Remove the target itself from intermediates
    intermediate.discard(target)

    return {"raw": raw, "intermediate": intermediate}


def calculate_machines_needed(
    target: str,
    target_rate: float,  # items per minute
    machine_speeds: Dict[str, float] = None,
) -> Dict[str, float]:
    """
    Calculate number of machines needed for target production rate.

    Args:
        target: Target product
        target_rate: Target production rate (items/minute)
        machine_speeds: Machine crafting speed multipliers

    Returns:
        Dict mapping recipe name to number of machines needed (float)
    """
    if machine_speeds is None:
        machine_speeds = {
            "furnace": 2.0,  # electric-furnace speed
            "assembling-machine": 0.5,  # assembling-machine-1 speed
            "mining-drill": 0.5,  # electric-mining-drill speed
        }

    chain = get_recipe_chain(target)

    # Calculate required rates for each product (items/second)
    required_rates: Dict[str, float] = {target: target_rate / 60}

    # Work backwards through chain
    for recipe_name in reversed(chain):
        recipe = RECIPES[recipe_name]
        output_rate = required_rates.get(recipe_name, 0)

        # Calculate crafts per second needed
        output_per_craft = recipe.outputs.get(recipe_name, 1)
        crafts_per_second = output_rate / output_per_craft

        # Add required rates for inputs
        for input_item, input_qty in recipe.inputs.items():
            input_rate = crafts_per_second * input_qty
            required_rates[input_item] = required_rates.get(input_item, 0) + input_rate

    # Calculate machines needed
    machines_needed = {}
    for recipe_name in chain:
        recipe = RECIPES[recipe_name]
        output_rate = required_rates.get(recipe_name, 0)
        output_per_craft = recipe.outputs.get(recipe_name, 1)

        speed = machine_speeds.get(recipe.machine_type, 1.0)
        crafts_per_second = output_rate / output_per_craft
        machine_crafts_per_second = speed / recipe.time

        machines = crafts_per_second / machine_crafts_per_second
        machines_needed[recipe_name] = machines

    return machines_needed


def format_recipe(recipe: Recipe) -> str:
    """Format recipe as human-readable string."""
    inputs = " + ".join(f"{qty} {item}" for item, qty in recipe.inputs.items())
    outputs = " + ".join(f"{qty} {item}" for item, qty in recipe.outputs.items())

    if inputs:
        return f"{inputs} -> {outputs} ({recipe.time}s)"
    else:
        return f"Mining: {outputs} ({recipe.time}s base)"


def format_recipe_chain(target: str) -> str:
    """Format entire recipe chain as readable text."""
    chain = get_recipe_chain(target)
    lines = [f"Recipe chain for {target}:"]

    for i, recipe_name in enumerate(chain, 1):
        recipe = RECIPES[recipe_name]
        lines.append(f"  {i}. {format_recipe(recipe)}")

    return "\n".join(lines)
