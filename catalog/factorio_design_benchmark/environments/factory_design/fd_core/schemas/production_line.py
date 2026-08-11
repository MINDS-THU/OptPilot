# -*- coding: utf-8 -*-
"""
Benchmark Output Schema for Factorio Production Line Design.

This schema defines the hierarchical structure that agents must output:
    ProductionLine -> Block -> Machine

Logistics system: Uses logistics robots (not transport belts).
Items are moved between machines via:
    Machine -> Inserter -> Provider Chest -> [Logistic Robot] -> Requester Chest -> Inserter -> Machine

Design Principles:
1. Hierarchical: Clear ProductionLine -> Block -> Machine structure
2. Validation-friendly: Easy to validate at each level
3. Execution-ready: Can be directly converted to Factorio placement commands
4. Robot logistics: No belts, no path planning - robots handle item transport
"""

from typing import Dict, List, Optional, Literal, Union
from pydantic import BaseModel, Field
from enum import Enum


# ============================================================
# Basic Types
# ============================================================


class Position(BaseModel):
    """2D position on the Factorio map."""

    x: float = Field(..., description="X coordinate")
    y: float = Field(..., description="Y coordinate")

    def __hash__(self):
        return hash((self.x, self.y))

    def __eq__(self, other):
        if isinstance(other, Position):
            return self.x == other.x and self.y == other.y
        return False


class Direction(str, Enum):
    """Entity facing direction."""

    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"


class BoundingBox(BaseModel):
    """
    Rectangular area on the map.

    Used for:
    - Block boundaries (defines where a block's entities can be placed)
    - Map boundaries (defines the playable area)

    Coordinate System:
    - X increases to the RIGHT
    - Y increases DOWNWARD (Factorio convention)
    - left_top is the upper-left corner (smaller x, smaller y)
    - right_bottom is the lower-right corner (larger x, larger y)

    Example:
        A block from (-10, -5) to (10, 5) is 20 wide x 10 tall, centered near origin.
    """

    left_top: Position = Field(
        ..., description="Upper-left corner (min x, min y in Factorio coordinates)"
    )
    right_bottom: Position = Field(
        ..., description="Lower-right corner (max x, max y in Factorio coordinates)"
    )

    @property
    def width(self) -> float:
        return self.right_bottom.x - self.left_top.x

    @property
    def height(self) -> float:
        return self.right_bottom.y - self.left_top.y

    def contains(self, pos: Position, margin: float = 0) -> bool:
        """Check if a position is inside this bounding box (with optional margin)."""
        return (
            self.left_top.x - margin <= pos.x <= self.right_bottom.x + margin
            and self.left_top.y - margin <= pos.y <= self.right_bottom.y + margin
        )


# ============================================================
# Entity Level (Lowest level of hierarchy)
# ============================================================


class Machine(BaseModel):
    """
    A production machine (drill, furnace, assembler, etc.).

    Machines process items: consume inputs, produce outputs based on recipe.
    """

    name: str = Field(..., description="Unique identifier for this machine")
    type: str = Field(
        ...,
        description="Machine type: electric-mining-drill, electric-furnace, assembling-machine-2, etc.",
    )
    position: Position = Field(..., description="Center position of the machine")
    direction: Direction = Field(
        Direction.NORTH, description="Facing direction of the machine"
    )
    recipe: Optional[str] = Field(
        None,
        description="Recipe to set (for assemblers). Furnaces auto-detect. Drills don't need.",
    )

    class Config:
        use_enum_values = True


class Inserter(BaseModel):
    """
    An inserter that moves items between adjacent entities.

    Inserters pick up items from one side and place them on the other side.
    Direction indicates the DROP direction (where the inserter places items TO).

    Pickup position: inserter_position - direction_vector * 1.0
    Drop position: inserter_position + direction_vector * 1.2

    Common pattern with logistics:
        Provider Chest <- Inserter <- Machine  (output side)
        Requester Chest -> Inserter -> Machine  (input side)
    """

    name: str = Field(..., description="Unique identifier for this inserter")
    type: str = Field(
        "inserter",
        description="Inserter type: inserter, fast-inserter, long-handed-inserter",
    )
    position: Position = Field(..., description="Position of the inserter")
    direction: Direction = Field(
        ...,
        description="Direction the inserter FACES (drop / destination direction). Pickup is behind.",
    )

    class Config:
        use_enum_values = True


class Container(BaseModel):
    """
    A regular storage container (chest) for storing items.
    Used as final output storage (not for logistics network).
    """

    name: str = Field(..., description="Unique identifier")
    type: str = Field(
        "iron-chest",
        description="Container type: wooden-chest, iron-chest, steel-chest",
    )
    position: Position = Field(..., description="Position of the container")
    direction: Direction = Field(
        Direction.NORTH, description="Direction (usually north)"
    )

    class Config:
        use_enum_values = True


# ============================================================
# Logistics Robot System
# ============================================================


class RequestFilter(BaseModel):
    """
    A request filter for a requester chest.
    Specifies what item to request and the buffer amount to maintain.
    """

    index: int = Field(..., description="Filter slot index (1-30)")
    name: str = Field(..., description="Item name to request (e.g., 'iron-ore')")
    count: int = Field(
        ...,
        description="Buffer amount to maintain. Semantic positivity is checked by logistics validation.",
    )


class LogisticsChest(BaseModel):
    """
    A logistics chest that participates in the robot logistics network.

    Types:
    - logistic-chest-passive-provider: Items placed here are available for robots to pick up.
      Use for machine OUTPUT (inserter moves items from machine to this chest).

    - logistic-chest-requester: Requests specific items from the logistics network.
      Use for machine INPUT (robots deliver items, inserter moves to machine).
      Must set request_filters to specify what items to request.

    - logistic-chest-active-provider: Actively pushes items to requesters/storage.
    - logistic-chest-storage: General logistics storage.
    - logistic-chest-buffer: Like storage but also responds to requests.

    Common patterns:
        Machine output: Machine -> Inserter -> logistic-chest-passive-provider
        Machine input:  logistic-chest-requester -> Inserter -> Machine
    """

    name: str = Field(..., description="Unique identifier")
    type: str = Field(
        ...,
        description="Chest type: logistic-chest-passive-provider, logistic-chest-requester, logistic-chest-active-provider, logistic-chest-storage, logistic-chest-buffer",
    )
    position: Position = Field(..., description="Position of the chest")
    direction: Direction = Field(
        Direction.NORTH, description="Direction (usually north)"
    )

    # Request filters (only for logistic-chest-requester and logistic-chest-buffer)
    request_filters: Optional[List[RequestFilter]] = Field(
        None,
        description="Items to request from logistics network. Required for logistic-chest-requester.",
    )

    class Config:
        use_enum_values = True


# ============================================================
# Block Level (Middle level of hierarchy)
# ============================================================


class Block(BaseModel):
    """
    A functional block containing machines that work together.

    A block is a logical grouping of machines performing related functions,
    such as "iron smelting block" or "circuit assembly block".

    Items flow WITHIN a block via inserters between machines and logistics chests.
    Items flow BETWEEN blocks via the logistics robot network (provider -> requester).

    No belt routing needed! Logistics robots handle all inter-entity transport.
    """

    id: str = Field(..., description="Unique block identifier")
    name: str = Field(..., description="Human-readable name (e.g., 'Iron Smelting')")
    description: Optional[str] = Field(None, description="What this block does")

    # Block boundaries
    bounding_box: BoundingBox = Field(..., description="Block area on the map")

    # Machines inside this block
    machines: List[Machine] = Field(
        default_factory=list, description="Production machines in this block"
    )

    # Inserters inside this block
    inserters: List[Inserter] = Field(
        default_factory=list, description="Inserters for moving items between machines and chests"
    )

    # Logistics chests (provider and requester chests for robot logistics)
    logistics_chests: List[LogisticsChest] = Field(
        default_factory=list,
        description="Logistics chests for robot-based item transport",
    )

    # Regular containers (output chests, not for logistics network)
    containers: List[Container] = Field(
        default_factory=list, description="Regular storage containers (final output)"
    )


# ============================================================
# Global Entities (Outside blocks)
# ============================================================


class GlobalEntity(BaseModel):
    """
    Infrastructure entity not belonging to any specific block.

    Examples: substations, roboports, offshore-pump, boiler, steam-engine, pipe.
    Roboports are critical - they define the logistics network coverage area.
    Steam power chain: offshore-pump -> pipe -> boiler -> steam-engine.
    """

    name: str = Field(..., description="Unique identifier")
    type: str = Field(
        ...,
        description="Entity type: substation, big-electric-pole, roboport, offshore-pump, boiler, steam-engine, pipe, etc.",
    )
    position: Position = Field(..., description="Entity position")
    direction: Direction = Field(Direction.NORTH, description="Facing direction")

    class Config:
        use_enum_values = True


# ============================================================
# Production Line (Top level - Final output schema)
# ============================================================


class ProductionLine(BaseModel):

    class Config:
        allow_population_by_field_name = True
    """
    Complete production line design using logistics robots.

    This is what agents must output. It contains:
    - blocks: Functional blocks with their internal layouts
    - global_entities: Infrastructure (roboports, substations, steam power entities)
    - optional logistic_robot_count: total robots inserted into the accepted network

    Item transport is handled by logistics robots:
    - Passive provider chests -> Logistic robots -> Requester chests
    - No belts or belt connections needed!

    The benchmark will:
    1. Validate this schema can be parsed
    2. Validate the design (spatial, power, logistics coverage)
    3. Place entities in Factorio and insert robots into roboports
    4. Run simulation to check if production achieves target rate
    """

    # Reasoning (for debugging and analysis)
    reasoning: Optional[str] = Field(
        None, description="Step-by-step design reasoning from the LLM"
    )

    # Metadata
    task_name: str = Field(..., description="Name of the task being solved")
    target_product: str = Field(..., description="Product this line produces")
    target_rate_per_minute: float = Field(
        ...,
        alias="target_rate",
        description="Target production rate in items per minute",
    )

    @property
    def target_rate(self) -> float:
        """Backward-compatible alias for code that still uses target_rate."""
        return self.target_rate_per_minute
    logistic_robot_count: Optional[int] = Field(
        None,
        description=(
            "Optional total number of logistics robots to insert into the "
            "roboport network during execution."
        ),
    )

    # Hierarchical structure
    blocks: Dict[str, Block] = Field(
        default_factory=dict, description="All functional blocks (key = block ID)"
    )

    # Global infrastructure (roboports, power)
    global_entities: List[GlobalEntity] = Field(
        default_factory=list,
        description="Infrastructure entities: roboports (REQUIRED for logistics), substations, offshore-pump, boiler, steam-engine, pipe",
    )

    # Optional: Summary for human readability
    summary: Optional[str] = Field(
        None, description="Brief description of the design approach"
    )

    @property
    def total_machines(self) -> int:
        """Count total production machines."""
        return sum(len(block.machines) for block in self.blocks.values())

    @property
    def total_inserters(self) -> int:
        """Count total inserters."""
        return sum(len(block.inserters) for block in self.blocks.values())

    @property
    def total_logistics_chests(self) -> int:
        """Count total logistics chests."""
        return sum(len(block.logistics_chests) for block in self.blocks.values())

    def get_all_positions(self) -> List[Position]:
        """Get all entity positions for collision detection."""
        positions = []
        for block in self.blocks.values():
            for machine in block.machines:
                positions.append(machine.position)
            for inserter in block.inserters:
                positions.append(inserter.position)
            for chest in block.logistics_chests:
                positions.append(chest.position)
            for container in block.containers:
                positions.append(container.position)
        for entity in self.global_entities:
            positions.append(entity.position)
        return positions

    def get_all_logistics_chests(self) -> List[LogisticsChest]:
        """Get all logistics chests from all blocks."""
        chests = []
        for block in self.blocks.values():
            chests.extend(block.logistics_chests)
        return chests

    def get_all_roboports(self) -> List[GlobalEntity]:
        """Get all roboports from global entities."""
        return [e for e in self.global_entities if e.type == "roboport"]


# ============================================================
# Example Output (Logistics Robot Version)
# ============================================================

EXAMPLE_PRODUCTION_LINE = {
    "reasoning": "Mining -> Provider Chest -> [Robot] -> Requester Chest -> Smelting. ",
    "task_name": "iron_plate_low_easy",
    "target_product": "iron-plate",
    "target_rate_per_minute": 30,
    "logistic_robot_count": 50,
    "blocks": {
        "mining_block": {
            "id": "mining_block",
            "name": "Iron Mining",
            "description": "Mines iron ore and provides it to logistics network",
            "bounding_box": {
                "left_top": {"x": -10, "y": -5},
                "right_bottom": {"x": 0, "y": 5},
            },
            "machines": [
                {
                    "name": "drill_1",
                    "type": "electric-mining-drill",
                    "position": {"x": -5.5, "y": 0.5},
                    "direction": "south",
                }
            ],
            "inserters": [
                {
                    "name": "ins_drill_out",
                    "type": "inserter",
                    "position": {"x": -3.5, "y": 0.5},
                    "direction": "east",
                }
            ],
            "logistics_chests": [
                {
                    "name": "ore_provider",
                    "type": "logistic-chest-passive-provider",
                    "position": {"x": -2.5, "y": 0.5},
                }
            ],
        },
        "smelting_block": {
            "id": "smelting_block",
            "name": "Iron Smelting",
            "description": "Requests iron ore, smelts into iron plates, provides to network",
            "bounding_box": {
                "left_top": {"x": 5, "y": -5},
                "right_bottom": {"x": 20, "y": 5},
            },
            "machines": [
                {
                    "name": "furnace_1",
                    "type": "electric-furnace",
                    "position": {"x": 10.5, "y": 0.5},
                    "recipe": "iron-plate",
                }
            ],
            "inserters": [
                {
                    "name": "ins_furnace_in",
                    "type": "inserter",
                    "position": {"x": 8.5, "y": 0.5},
                    "direction": "east",
                },
                {
                    "name": "ins_furnace_out",
                    "type": "inserter",
                    "position": {"x": 12.5, "y": 0.5},
                    "direction": "east",
                },
            ],
            "logistics_chests": [
                {
                    "name": "ore_requester",
                    "type": "logistic-chest-requester",
                    "position": {"x": 7.5, "y": 0.5},
                    "request_filters": [
                        {"index": 1, "name": "iron-ore", "count": 100}
                    ],
                },
                {
                    "name": "plate_provider",
                    "type": "logistic-chest-passive-provider",
                    "position": {"x": 13.5, "y": 0.5},
                },
            ],
            "containers": [
                {
                    "name": "output_chest",
                    "type": "iron-chest",
                    "position": {"x": 18.5, "y": 0.5},
                }
            ],
        },
    },
    "global_entities": [
        {
            "name": "roboport_1",
            "type": "roboport",
            "position": {"x": 4, "y": 8},
            "direction": "north",
        },
        {
            "name": "substation_1",
            "type": "substation",
            "position": {"x": 0, "y": 0},
            "direction": "north",
        },
        {
            "name": "substation_2",
            "type": "substation",
            "position": {"x": 16, "y": 0},
            "direction": "north",
        },
        {
            "name": "pump_1",
            "type": "offshore-pump",
            "position": {"x": -4.5, "y": -19.5},
            "direction": "south",
        },
        {
            "name": "boiler_1",
            "type": "boiler",
            "position": {"x": -4, "y": -16},
            "direction": "north",
        },
        {
            "name": "engine_1",
            "type": "steam-engine",
            "position": {"x": -4.5, "y": -11.5},
            "direction": "north",
        },
    ],
}


# ============================================================
# Schema Export for Documentation
# ============================================================


def get_schema_json() -> str:
    """Get JSON schema for documentation."""
    import json

    return json.dumps(ProductionLine.model_json_schema(), indent=2)


if __name__ == "__main__":
    # Validate example
    from pydantic import ValidationError

    try:
        pl = ProductionLine(**EXAMPLE_PRODUCTION_LINE)
        print("Example validates successfully!")
        print(f"   Total machines: {pl.total_machines}")
        print(f"   Total inserters: {pl.total_inserters}")
        print(f"   Total logistics chests: {pl.total_logistics_chests}")
        print(f"   Roboports: {len(pl.get_all_roboports())}")
        print(f"   Blocks: {list(pl.blocks.keys())}")
    except ValidationError as e:
        print(f"Validation error: {e}")
