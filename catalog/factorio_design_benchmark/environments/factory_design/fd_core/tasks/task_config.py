# -*- coding: utf-8 -*-
"""
Benchmark Task Configuration

Defines the schema for benchmark task configurations.
"""

from typing import Dict, List, Literal, Optional, Tuple, Any
from pydantic import BaseModel, Field


class OrePatch(BaseModel):
    """Ore patch configuration."""

    ore_type: Literal["iron-ore", "copper-ore", "stone", "coal"]
    position: Tuple[float, float] = Field(..., description="Center position (x, y)")
    radius: float = Field(..., gt=0, description="Patch radius")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.ore_type,
            "position": {"x": self.position[0], "y": self.position[1]},
            "radius": self.radius,
        }


class WaterPatch(BaseModel):
    """Water body configuration for steam power."""

    position: Tuple[float, float] = Field(..., description="Center position (x, y)")
    width: int = Field(10, gt=0, description="Width in tiles")
    height: int = Field(6, gt=0, description="Height in tiles")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position": {"x": self.position[0], "y": self.position[1]},
            "width": self.width,
            "height": self.height,
        }


class EnvironmentConfig(BaseModel):
    """Environment configuration."""

    map_width: int = Field(..., gt=0)
    map_height: int = Field(..., gt=0)
    ore_patches: List[OrePatch]
    water_patches: List[WaterPatch] = Field(
        default_factory=list,
        description="Water bodies for offshore pump placement (steam power)",
    )
    difficulty: Literal["easy", "hard"]

    @property
    def map_bounds(self) -> Dict[str, Any]:
        """Get map bounds as dict."""
        hw, hh = self.map_width / 2, self.map_height / 2
        return {
            "left_top": {"x": -hw, "y": -hh},
            "right_bottom": {"x": hw, "y": hh},
        }


DEFAULT_COST_MODEL = "factorio_recipe_cost_v1"


class BenchmarkTaskConfig(BaseModel):
    """
    Complete configuration for a benchmark task.

    This is the central configuration that defines everything needed
    to instantiate a task, generate prompts, and validate outputs.
    """

    # === Basic Info ===
    task_id: str = Field(..., description="Unique task identifier")
    level: Literal[1, 2, 3, 4] = Field(..., description="Difficulty level")

    # === Target ===
    target_product: str = Field(..., description="Target product name")
    target_rate_per_minute: float = Field(
        ...,
        gt=0,
        alias="target_rate",
        description="Target rate in items per minute",
    )

    # === Environment ===
    environment: EnvironmentConfig

    # === Cost metric ===
    cost_model: str = Field(
        DEFAULT_COST_MODEL,
        description="Cost model version used for reporting and comparison",
    )

    # === Metadata ===
    description: str = Field("", description="Human-readable task description")
    capacity_level: Literal["low", "high"] = Field(..., description="Capacity level")

    class Config:
        allow_population_by_field_name = True
        frozen = True

    @property
    def target_rate(self) -> float:
        """Backward-compatible alias for code that still uses target_rate."""
        return self.target_rate_per_minute

    def to_validation_config(self) -> Dict[str, Any]:
        """Convert to validation config format."""
        return {
            "map_bounds": self.environment.map_bounds,
            "ore_patches": [p.to_dict() for p in self.environment.ore_patches],
            "water_patches": [w.to_dict() for w in self.environment.water_patches],
            "cost_model": self.cost_model,
            "target_product": self.target_product,
            "target_rate_per_minute": self.target_rate_per_minute,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.task_id,
            "level": self.level,
            "target_product": self.target_product,
            "target_rate_per_minute": self.target_rate_per_minute,
            "map_width": self.environment.map_width,
            "map_height": self.environment.map_height,
            "ore_patches": [p.to_dict() for p in self.environment.ore_patches],
            "cost_model": self.cost_model,
            "description": self.description,
            "capacity_level": self.capacity_level,
            "difficulty": self.environment.difficulty,
        }


# ============================================================
# Task Factory Functions
# ============================================================


def create_level1_task(
    product: Literal["iron-plate", "iron-gear-wheel"],
    capacity: Literal["low", "high"],
    difficulty: Literal["easy", "hard"],
) -> BenchmarkTaskConfig:
    """Create a Level 1 task configuration."""

    # Target rates
    rates = {
        "iron-plate": {"low": 10, "high": 30},
        "iron-gear-wheel": {"low": 10, "high": 30},
    }

    # Environment configs
    if difficulty == "easy":
        env = EnvironmentConfig(
            map_width=50,
            map_height=50,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(0, 0), radius=15),
            ],
            water_patches=[
                WaterPatch(position=(0, -22), width=10, height=6),
            ],
            difficulty="easy",
        )
    else:
        env = EnvironmentConfig(
            map_width=30,
            map_height=30,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(5, 5), radius=8),
            ],
            water_patches=[
                WaterPatch(position=(5, -10), width=8, height=4),
            ],
            difficulty="hard",
        )

    product_short = product.replace("-", "_").replace("iron_", "")
    if product == "iron-plate":
        product_short = "iron_plate"
    elif product == "iron-gear-wheel":
        product_short = "iron_gear"

    return BenchmarkTaskConfig(
        task_id=f"{product_short}_{capacity}_{difficulty}",
        level=1,
        target_product=product,
        target_rate_per_minute=rates[product][capacity],
        environment=env,
        description=f"Produce {rates[product][capacity]} {product}/min",
        capacity_level=capacity,
    )


def create_level2_task(
    product: Literal["electronic-circuit", "automation-science-pack"],
    capacity: Literal["low", "high"],
    difficulty: Literal["easy", "hard"],
) -> BenchmarkTaskConfig:
    """Create a Level 2 task configuration."""

    rates = {
        "electronic-circuit": {"low": 20, "high": 50},
        "automation-science-pack": {"low": 6, "high": 15},
    }

    if difficulty == "easy":
        env = EnvironmentConfig(
            map_width=60,
            map_height=60,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(-15, 0), radius=12),
                OrePatch(ore_type="copper-ore", position=(15, 0), radius=12),
            ],
            water_patches=[
                WaterPatch(position=(0, -25), width=12, height=6),
            ],
            difficulty="easy",
        )
    else:
        env = EnvironmentConfig(
            map_width=40,
            map_height=40,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(-10, -10), radius=6),
                OrePatch(ore_type="copper-ore", position=(10, 10), radius=6),
            ],
            water_patches=[
                WaterPatch(position=(0, -15), width=8, height=4),
            ],
            difficulty="hard",
        )

    product_short = {
        "electronic-circuit": "electronic_circuit",
        "automation-science-pack": "science_pack_red",
    }[product]

    return BenchmarkTaskConfig(
        task_id=f"{product_short}_{capacity}_{difficulty}",
        level=2,
        target_product=product,
        target_rate_per_minute=rates[product][capacity],
        environment=env,
        description=f"Produce {rates[product][capacity]} {product}/min",
        capacity_level=capacity,
    )


def create_level3_task(
    product: Literal["inserter", "logistic-science-pack"],
    capacity: Literal["low", "high"],
    difficulty: Literal["easy", "hard"],
) -> BenchmarkTaskConfig:
    """Create a Level 3 task configuration."""

    rates = {
        "inserter": {"low": 12, "high": 24},
        "logistic-science-pack": {"low": 5, "high": 10},
    }

    if difficulty == "easy":
        env = EnvironmentConfig(
            map_width=80,
            map_height=80,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(-25, 0), radius=15),
                OrePatch(ore_type="copper-ore", position=(25, 0), radius=15),
            ],
            water_patches=[
                WaterPatch(position=(0, -35), width=14, height=6),
            ],
            difficulty="easy",
        )
    else:
        env = EnvironmentConfig(
            map_width=50,
            map_height=50,
            ore_patches=[
                OrePatch(ore_type="iron-ore", position=(-15, -15), radius=8),
                OrePatch(ore_type="copper-ore", position=(15, 15), radius=8),
            ],
            water_patches=[
                WaterPatch(position=(0, -20), width=10, height=4),
            ],
            difficulty="hard",
        )

    product_short = {
        "inserter": "inserter",
        "logistic-science-pack": "science_pack_green",
    }[product]

    return BenchmarkTaskConfig(
        task_id=f"{product_short}_{capacity}_{difficulty}",
        level=3,
        target_product=product,
        target_rate_per_minute=rates[product][capacity],
        environment=env,
        description=f"Produce {rates[product][capacity]} {product}/min",
        capacity_level=capacity,
    )


def create_level4_task(
    product: Literal["military-science-pack", "assembling-machine-2"],
    capacity: Literal["low", "high"],
    difficulty: Literal["easy", "hard"],
) -> BenchmarkTaskConfig:
    """Create a Level 4 task configuration."""

    rates = {
        "military-science-pack": {"low": 1, "high": 5},
        "assembling-machine-2": {"low": 1, "high": 5},
    }

    if product == "military-science-pack":
        if difficulty == "easy":
            env = EnvironmentConfig(
                map_width=120,
                map_height=120,
                ore_patches=[
                    OrePatch(ore_type="iron-ore", position=(-36, 0), radius=18),
                    OrePatch(ore_type="copper-ore", position=(36, 0), radius=16),
                    OrePatch(ore_type="stone", position=(0, 32), radius=14),
                    OrePatch(ore_type="coal", position=(0, -32), radius=14),
                ],
                water_patches=[
                    WaterPatch(position=(0, -52), width=14, height=6),
                ],
                difficulty="easy",
            )
        else:
            env = EnvironmentConfig(
                map_width=90,
                map_height=90,
                ore_patches=[
                    OrePatch(ore_type="iron-ore", position=(-28, 24), radius=12),
                    OrePatch(ore_type="copper-ore", position=(28, -24), radius=10),
                    OrePatch(ore_type="stone", position=(-28, -24), radius=10),
                    OrePatch(ore_type="coal", position=(28, 24), radius=10),
                ],
                water_patches=[
                    WaterPatch(position=(0, -40), width=10, height=4),
                ],
                difficulty="hard",
            )
        product_short = "military_science_pack"
    else:
        if difficulty == "easy":
            env = EnvironmentConfig(
                map_width=100,
                map_height=100,
                ore_patches=[
                    OrePatch(ore_type="iron-ore", position=(-25, 0), radius=20),
                    OrePatch(ore_type="copper-ore", position=(25, 0), radius=16),
                ],
                water_patches=[
                    WaterPatch(position=(0, -42), width=14, height=6),
                ],
                difficulty="easy",
            )
        else:
            env = EnvironmentConfig(
                map_width=80,
                map_height=80,
                ore_patches=[
                    OrePatch(ore_type="iron-ore", position=(-22, 18), radius=14),
                    OrePatch(ore_type="copper-ore", position=(22, -18), radius=12),
                ],
                water_patches=[
                    WaterPatch(position=(0, -35), width=10, height=4),
                ],
                difficulty="hard",
            )
        product_short = "assembling_machine_2"

    return BenchmarkTaskConfig(
        task_id=f"{product_short}_{capacity}_{difficulty}",
        level=4,
        target_product=product,
        target_rate_per_minute=rates[product][capacity],
        environment=env,
        description=f"Produce {rates[product][capacity]} {product}/min",
        capacity_level=capacity,
    )
