# -*- coding: utf-8 -*-
"""
Production Line Validator - Logistics Robot Version

Validates ProductionLine designs for the Factorio benchmark.
Uses logistics robots instead of transport belts.

Validation Categories:
- V1: Schema (JSON validity, required fields, entity types)
- V2: Recipe Chain (valid recipes and complete production path)
- V3: Spatial (map bounds, entity overlap, coordinate alignment)
- V4: Logistics Robot Network (roboport coverage, provider/requester matching, local material flow)
- V5: Power (all machines powered via pole network)
- V6: Special Position Requirements (mining drills on ore, offshore pumps at water, steam direction, non-pump entities on land)
"""

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import ValidationError

from ..schemas.production_line import (
    ProductionLine,
    Block,
    Machine,
    Inserter,
    Container,
    LogisticsChest,
    GlobalEntity,
    Position,
    Direction,
    BoundingBox,
)
from .entity_costs import summarize_entity_costs


# ============================================================
# Game Data Constants
# ============================================================

VALID_MACHINE_TYPES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "electric-furnace",
    "stone-furnace",
    "steel-furnace",
    "electric-mining-drill",
    "burner-mining-drill",
    "chemical-plant",
    "oil-refinery",
}

VALID_INSERTER_TYPES = {
    "inserter",
    "long-handed-inserter",
    "fast-inserter",
    "burner-inserter",
    "stack-inserter",
    "filter-inserter",
}

VALID_CONTAINER_TYPES = {
    "wooden-chest",
    "iron-chest",
    "steel-chest",
}

VALID_LOGISTICS_CHEST_TYPES = {
    "logistic-chest-passive-provider",
    "logistic-chest-active-provider",
    "logistic-chest-requester",
    "logistic-chest-storage",
    "logistic-chest-buffer",
}

VALID_POLE_TYPES = {
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
}

VALID_POWER_SOURCE_TYPES = {
    "steam-engine",
}

VALID_STEAM_POWER_TYPES = {
    "offshore-pump",
    "boiler",
    "steam-engine",
    "pipe",
}

VALID_GLOBAL_ENTITY_TYPES = (
    VALID_POLE_TYPES
    | VALID_POWER_SOURCE_TYPES
    | VALID_STEAM_POWER_TYPES
    | {"roboport", "radar", "lamp"}
)

# Entity sizes (width, height) in tiles
ENTITY_SIZES = {
    "assembling-machine-1": (3, 3),
    "assembling-machine-2": (3, 3),
    "assembling-machine-3": (3, 3),
    "electric-furnace": (3, 3),
    "stone-furnace": (2, 2),
    "steel-furnace": (2, 2),
    "electric-mining-drill": (3, 3),
    "burner-mining-drill": (2, 2),
    "chemical-plant": (3, 3),
    "oil-refinery": (5, 5),
    "inserter": (1, 1),
    "long-handed-inserter": (1, 1),
    "fast-inserter": (1, 1),
    "burner-inserter": (1, 1),
    "stack-inserter": (1, 1),
    "filter-inserter": (1, 1),
    "small-electric-pole": (1, 1),
    "medium-electric-pole": (1, 1),
    "big-electric-pole": (2, 2),
    "substation": (2, 2),
    "steam-engine": (3, 5),
    # Logistics chests (1x1)
    "logistic-chest-passive-provider": (1, 1),
    "logistic-chest-active-provider": (1, 1),
    "logistic-chest-requester": (1, 1),
    "logistic-chest-storage": (1, 1),
    "logistic-chest-buffer": (1, 1),
    # Regular containers
    "wooden-chest": (1, 1),
    "iron-chest": (1, 1),
    "steel-chest": (1, 1),
    # Logistics infrastructure
    "roboport": (4, 4),
    # Steam power (boiler facing north/south: 3 wide x 2 tall)
    "offshore-pump": (2, 1),
    "boiler": (3, 2),
    "pipe": (1, 1),
}

# Inserter reach distances
INSERTER_REACH = {
    "inserter": 1,
    "long-handed-inserter": 2,
    "fast-inserter": 1,
    "burner-inserter": 1,
    "stack-inserter": 1,
    "filter-inserter": 1,
}

# Direction vectors
DIRECTION_VECTORS = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}

# Pole supply areas (radius from center)
POLE_SUPPLY_AREAS = {
    "small-electric-pole": 2.5,
    "medium-electric-pole": 3.5,
    "big-electric-pole": 2,
    "substation": 9,
}

# Pole wire reach (max distance to connect to another pole)
POLE_WIRE_REACH = {
    "small-electric-pole": 7.5,
    "medium-electric-pole": 9,
    "big-electric-pole": 30,
    "substation": 18,
}

# Benchmark static roboport coverage check: chest center within +/-24 per axis.
ROBOPORT_LOGISTICS_RADIUS = 24

# Machines that need power
POWER_CONSUMERS = VALID_MACHINE_TYPES | VALID_INSERTER_TYPES | {"roboport"}

STEAM_ALIGNMENT_EXEMPT_TYPES = {"offshore-pump", "boiler", "steam-engine", "pipe"}

MATERIAL_IO_MACHINE_TYPES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "electric-furnace",
    "stone-furnace",
    "steel-furnace",
}

PROVIDER_CHEST_TYPES = {
    "logistic-chest-passive-provider",
    "logistic-chest-active-provider",
}


# ============================================================
# Validation Result
# ============================================================


@dataclass
class ValidationResult:
    """Result of validation checks."""

    passed: bool = True
    failed_checks: List[str] = field(default_factory=list)
    details: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    warning_details: Dict[str, str] = field(default_factory=dict)
    unpowered_entities: List[Dict] = field(default_factory=list)
    collision_pairs: List[Dict] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def add_failure(self, check_id: str, detail: str):
        """Add a failed check."""
        self.passed = False
        self.failed_checks.append(check_id)
        if check_id in self.details:
            self.details[check_id] += "; " + detail
        else:
            self.details[check_id] = detail

    def add_warning(self, check_id: str, detail: str):
        """Add a warning (does not cause validation to fail)."""
        self.warnings.append(check_id)
        self.warning_details[check_id] = detail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_checks": self.failed_checks,
            "details": self.details,
            "warnings": self.warnings,
            "warning_details": self.warning_details,
            "unpowered_entities": self.unpowered_entities,
            "collision_pairs": self.collision_pairs,
            "metrics": self.metrics,
        }


# ============================================================
# Task Configuration
# ============================================================


@dataclass
class TaskConfig:
    """Task configuration needed for validation."""

    map_bounds: BoundingBox
    ore_patches: List[Dict]
    water_patches: List[Dict]
    target_product: str
    target_rate: float

    @classmethod
    def from_dict(cls, data: Dict) -> "TaskConfig":
        return cls(
            map_bounds=BoundingBox(**data["map_bounds"]),
            ore_patches=data.get("ore_patches", []),
            water_patches=data.get("water_patches", []),
            target_product=data["target_product"],
            target_rate=data.get("target_rate_per_minute", data.get("target_rate")),
        )


# ============================================================
# Helper Functions
# ============================================================


# Entities whose width/height swap when facing east or west
ROTATABLE_ENTITIES = {"boiler", "steam-engine"}


def _get_entity_size(entity_type: str, direction: str = "north") -> Tuple[int, int]:
    """Get entity (width, height) accounting for rotation."""
    w, h = ENTITY_SIZES.get(entity_type, (1, 1))
    if entity_type in ROTATABLE_ENTITIES and direction in ("east", "west"):
        w, h = h, w  # Swap dimensions when rotated 90 degrees
    return w, h


def _get_entity_bbox(pos: Position, entity_type: str, direction: str = "north") -> Tuple[float, float, float, float]:
    """Get bounding box (left, top, right, bottom) for an entity at position."""
    w, h = _get_entity_size(entity_type, direction)
    half_w, half_h = w / 2, h / 2
    return (pos.x - half_w, pos.y - half_h, pos.x + half_w, pos.y + half_h)


def _bboxes_overlap(bbox1: Tuple, bbox2: Tuple, margin: float = 0.01) -> bool:
    """Check if two bounding boxes overlap."""
    l1, t1, r1, b1 = bbox1
    l2, t2, r2, b2 = bbox2
    return not (r1 <= l2 + margin or r2 <= l1 + margin or
                b1 <= t2 + margin or b2 <= t1 + margin)


def _bbox_contains_bbox(
    outer: Tuple[float, float, float, float],
    inner: Tuple[float, float, float, float],
    margin: float = 0,
) -> bool:
    """Check if outer fully contains inner."""
    outer_l, outer_t, outer_r, outer_b = outer
    inner_l, inner_t, inner_r, inner_b = inner
    return (
        outer_l - margin <= inner_l
        and outer_t - margin <= inner_t
        and outer_r + margin >= inner_r
        and outer_b + margin >= inner_b
    )


def _block_bbox_tuple(block: Block) -> Tuple[float, float, float, float]:
    """Return a block bounding box as (left, top, right, bottom)."""
    bbox = block.bounding_box
    return (
        bbox.left_top.x,
        bbox.left_top.y,
        bbox.right_bottom.x,
        bbox.right_bottom.y,
    )


def _valid_bbox_rect(bbox: Tuple[float, float, float, float]) -> bool:
    """Check strict rectangle ordering."""
    left, top, right, bottom = bbox
    return left < right and top < bottom


def _normalize_bbox(bbox: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """Return bbox with sorted left/top/right/bottom for secondary diagnostics."""
    left, top, right, bottom = bbox
    return (min(left, right), min(top, bottom), max(left, right), max(top, bottom))


def _bbox_overlaps_rect(bbox: Tuple[float, float, float, float], rect: Tuple[float, float, float, float]) -> bool:
    """Check whether an entity bbox overlaps a water rectangle interior."""
    l1, t1, r1, b1 = bbox
    l2, t2, r2, b2 = rect
    return not (r1 <= l2 or r2 <= l1 or b1 <= t2 or b2 <= t1)


def _section_kind_and_priority(etype: str, section_id: str) -> Tuple[str, int]:
    """Return a coarse ownership label and downstream priority for repair feedback."""
    if section_id != "global":
        return f"block:{section_id}", 0
    if etype == "roboport":
        return "logistics", 1
    if etype in VALID_POLE_TYPES or etype in VALID_POWER_SOURCE_TYPES or etype in VALID_STEAM_POWER_TYPES:
        return "power", 2
    return "global", 1


def _bbox_dict(bbox: Tuple[float, float, float, float]) -> Dict[str, float]:
    left, top, right, bottom = bbox
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _point_in_bbox(px: float, py: float, bbox: Tuple) -> bool:
    """Check if point is inside bounding box."""
    l, t, r, b = bbox
    return l <= px <= r and t <= py <= b


def _distance(p1: Position, p2: Position) -> float:
    """Euclidean distance between two positions."""
    return math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)


def _inserter_transfer_points(inserter: Inserter) -> Tuple[float, float, float, float]:
    """Return pickup and drop points for benchmark-facing inserter direction semantics."""
    reach = INSERTER_REACH.get(inserter.type, 1)
    direction = inserter.direction
    dx, dy = DIRECTION_VECTORS.get(direction, (0, -1))
    pickup_x = inserter.position.x - dx * reach
    pickup_y = inserter.position.y - dy * reach
    drop_x = inserter.position.x + dx * (reach + 0.2)
    drop_y = inserter.position.y + dy * (reach + 0.2)
    return pickup_x, pickup_y, drop_x, drop_y


def _mining_drill_output_point(machine: Machine) -> Tuple[float, float]:
    """Approximate the output tile center for a mining drill."""
    direction = machine.direction or "north"
    dx, dy = DIRECTION_VECTORS.get(direction, (0, -1))
    return machine.position.x + dx * 2, machine.position.y + dy * 2


def _water_patch_bbox(patch: Dict) -> Tuple[float, float, float, float]:
    """Return a rectangular water patch bbox as (left, top, right, bottom)."""
    position = patch.get("position", {})
    if isinstance(position, dict):
        cx = position.get("x", 0)
        cy = position.get("y", 0)
    elif isinstance(position, (list, tuple)) and len(position) >= 2:
        cx, cy = position[0], position[1]
    else:
        cx, cy = patch.get("x", 0), patch.get("y", 0)

    width = patch.get("width", 0)
    height = patch.get("height", 0)
    return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


def _distance_to_rect_boundary(x: float, y: float, rect: Tuple[float, float, float, float]) -> float:
    """Distance from a point to a rectangle boundary, supporting inside/outside points."""
    left, top, right, bottom = rect
    if left <= x <= right and top <= y <= bottom:
        return min(x - left, right - x, y - top, bottom - y)

    dx = max(left - x, 0, x - right)
    dy = max(top - y, 0, y - bottom)
    return math.sqrt(dx ** 2 + dy ** 2)


def _bbox_near_water_shoreline(entity_bbox: Tuple[float, float, float, float], patch: Dict) -> bool:
    """Approximate Factorio offshore-pump shoreline placement against a water patch."""
    rect = _water_patch_bbox(patch)
    center_x = (entity_bbox[0] + entity_bbox[2]) / 2
    center_y = (entity_bbox[1] + entity_bbox[3]) / 2
    entity_radius = max(entity_bbox[2] - entity_bbox[0], entity_bbox[3] - entity_bbox[1]) / 2
    shoreline_tolerance = 1.0
    return _distance_to_rect_boundary(center_x, center_y, rect) <= entity_radius + shoreline_tolerance


def _point_in_rect(x: float, y: float, rect: Tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def _pump_direction_points_to_water(pump: GlobalEntity, patch: Dict) -> bool:
    """Return whether an offshore pump direction points into the water rectangle."""
    direction = getattr(pump, "direction", "north") or "north"
    dx, dy = DIRECTION_VECTORS.get(direction, (0, -1))
    rect = _water_patch_bbox(patch)
    return _point_in_rect(pump.position.x + dx, pump.position.y + dy, rect)


def _points_close(p1: Tuple[float, float], p2: Tuple[float, float], tol: float = 0.55) -> bool:
    """Check whether two benchmark-grid points are effectively the same."""
    return abs(p1[0] - p2[0]) <= tol and abs(p1[1] - p2[1]) <= tol


def _steam_points_close(p1: Tuple[float, float], p2: Tuple[float, float]) -> bool:
    """Steam fluid ports are tile-specific; do not allow half-tile drift."""
    return _points_close(p1, p2, tol=0.1)


def _entity_point(entity: GlobalEntity) -> Tuple[float, float]:
    return (entity.position.x, entity.position.y)


def _steam_vectors(direction: str) -> Tuple[int, int, int, int]:
    """Return forward and perpendicular vectors for a steam-chain direction."""
    dx, dy = DIRECTION_VECTORS.get(direction, (0, -1))
    return dx, dy, -dy, dx


def _boiler_water_pipe_points(boiler: GlobalEntity) -> List[Tuple[float, float]]:
    """Expected pipe centers on the boiler water-input short side."""
    direction = getattr(boiler, "direction", "north") or "north"
    dx, dy, px, py = _steam_vectors(direction)
    cx, cy = boiler.position.x, boiler.position.y
    return [
        (cx - 0.5 * dx + 2 * px, cy - 0.5 * dy + 2 * py),
        (cx - 0.5 * dx - 2 * px, cy - 0.5 * dy - 2 * py),
    ]


def _boiler_steam_pipe_point(boiler: GlobalEntity) -> Tuple[float, float]:
    """Expected pipe center on the boiler steam-output long side."""
    direction = getattr(boiler, "direction", "north") or "north"
    dx, dy, _, _ = _steam_vectors(direction)
    return (boiler.position.x + 1.5 * dx, boiler.position.y + 1.5 * dy)


def _engine_input_pipe_point(engine: GlobalEntity) -> Tuple[float, float]:
    """Expected upstream pipe center on a steam engine's input short side."""
    direction = getattr(engine, "direction", "north") or "north"
    dx, dy, _, _ = _steam_vectors(direction)
    return (engine.position.x - 3 * dx, engine.position.y - 3 * dy)


# ============================================================
# Validator Class
# ============================================================


class ProductionLineValidator:
    """
    Validates ProductionLine designs with logistics robot network.
    """

    def __init__(self, task_config: TaskConfig):
        self.config = task_config

    def validate(self, output: Any) -> ValidationResult:
        """Run all validation checks."""
        result = ValidationResult()

        # V1: Schema
        pl = self._validate_schema(output, result)
        if pl is None:
            return result

        # V2: Recipe Chain
        self._validate_recipe_chain(pl, result)

        # V3: Spatial Constraints
        self._validate_spatial(pl, result)

        # V4: Logistics Robot Network
        self._validate_logistics_network(pl, result)

        # V5: Power Grid
        self._validate_power_grid(pl, result)

        # V6: Special Position Requirements
        self._validate_special_positions(pl, result)

        # Metric reporting: entity cost
        self._annotate_cost_metrics(pl, result)

        return result

    # ============================================================
    # V1: Schema Validation
    # ============================================================

    def _validate_schema(self, output: Any, result: ValidationResult) -> Optional[ProductionLine]:
        """V1.1-V1.2: Schema and entity-type validation."""

        # V1.1: JSON Parseable
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError as e:
                result.add_failure("V1.1", f"Invalid JSON: {e}")
                return None

        if not isinstance(output, dict):
            result.add_failure("V1.1", f"Expected dict, got {type(output).__name__}")
            return None

        try:
            pl = ProductionLine(**output)
        except ValidationError as e:
            result.add_failure("V1.1", f"Schema validation failed: {e}")
            return None

        # V1.2: Valid Entity Types
        errors = []
        for block_id, block in pl.blocks.items():
            for m in block.machines:
                if m.type not in VALID_MACHINE_TYPES:
                    errors.append(f"Invalid machine '{m.type}' in block '{block_id}'")
            for ins in block.inserters:
                if ins.type not in VALID_INSERTER_TYPES:
                    errors.append(f"Invalid inserter '{ins.type}' in block '{block_id}'")
            for chest in block.logistics_chests:
                if chest.type not in VALID_LOGISTICS_CHEST_TYPES:
                    errors.append(f"Invalid logistics chest '{chest.type}' in block '{block_id}'")
            for cont in block.containers:
                if cont.type not in VALID_CONTAINER_TYPES:
                    errors.append(f"Invalid container '{cont.type}' in block '{block_id}'")
        for ge in pl.global_entities:
            if ge.type not in VALID_GLOBAL_ENTITY_TYPES:
                errors.append(f"Invalid global entity type '{ge.type}'")
        if errors:
            result.add_failure("V1.2", "; ".join(errors))

        return pl

    def _check_recipes(self, pl: ProductionLine, result: ValidationResult):
        """V2.1: Check recipes are known to the benchmark recipe table."""
        from ..tasks.recipes import RECIPES
        errors = []
        for block_id, block in pl.blocks.items():
            for machine in block.machines:
                if machine.recipe and machine.recipe not in RECIPES:
                    errors.append(f"Unknown recipe '{machine.recipe}' for '{machine.name}' in block '{block_id}'")
        if errors:
            result.add_failure("V2.1", "; ".join(errors))

    # ============================================================
    # V3: Spatial Constraints
    # ============================================================

    def _validate_spatial(self, pl: ProductionLine, result: ValidationResult):
        """V3.1-V3.5: Spatial validation."""

        # List of (name, type, position, block_id, direction)
        all_entities = []

        for block_id, block in pl.blocks.items():
            for m in block.machines:
                d = getattr(m, "direction", "north") or "north"
                all_entities.append((m.name, m.type, m.position, block_id, d))
            for ins in block.inserters:
                d = getattr(ins, "direction", "north") or "north"
                all_entities.append((ins.name, ins.type, ins.position, block_id, d))
            for chest in block.logistics_chests:
                d = getattr(chest, "direction", "north") or "north"
                all_entities.append((chest.name, chest.type, chest.position, block_id, d))
            for cont in block.containers:
                d = getattr(cont, "direction", "north") or "north"
                all_entities.append((cont.name, cont.type, cont.position, block_id, d))
        for ge in pl.global_entities:
            d = getattr(ge, "direction", "north") or "north"
            all_entities.append((ge.name, ge.type, ge.position, "global", d))

        # V3.1: Map bounds
        errors = []
        for name, etype, pos, bid, dirn in all_entities:
            bbox = _get_entity_bbox(pos, etype, dirn)
            map_bb = self.config.map_bounds
            if (bbox[0] < map_bb.left_top.x or bbox[2] > map_bb.right_bottom.x or
                bbox[1] < map_bb.left_top.y or bbox[3] > map_bb.right_bottom.y):
                errors.append(f"'{name}' ({etype}) at ({pos.x},{pos.y}) outside map bounds")
        if errors:
            result.add_failure("V3.1", "; ".join(errors[:5]))

        # V3.2: Entity overlap detection
        overlap_errors = []
        collision_pairs = []
        for i in range(len(all_entities)):
            for j in range(i + 1, len(all_entities)):
                n1, t1, p1, b1, d1 = all_entities[i]
                n2, t2, p2, b2, d2 = all_entities[j]
                bb1 = _get_entity_bbox(p1, t1, d1)
                bb2 = _get_entity_bbox(p2, t2, d2)
                if _bboxes_overlap(bb1, bb2):
                    section1, priority1 = _section_kind_and_priority(t1, b1)
                    section2, priority2 = _section_kind_and_priority(t2, b2)
                    e1 = {
                        "name": n1,
                        "type": t1,
                        "section": section1,
                        "position": {"x": p1.x, "y": p1.y},
                        "bbox": _bbox_dict(bb1),
                    }
                    e2 = {
                        "name": n2,
                        "type": t2,
                        "section": section2,
                        "position": {"x": p2.x, "y": p2.y},
                        "bbox": _bbox_dict(bb2),
                    }
                    if priority1 >= priority2:
                        downstream, upstream = e1, e2
                    else:
                        downstream, upstream = e2, e1
                    collision_pairs.append({
                        "downstream_entity": downstream,
                        "upstream_entity": upstream,
                    })
                    overlap_errors.append(
                        f"'{downstream['name']}' overlaps '{upstream['name']}'"
                    )
        if overlap_errors:
            result.collision_pairs = collision_pairs
            result.add_failure("V3.2", "; ".join(overlap_errors[:5]))

        # V3.3: Position alignment (tile centers), direction-aware
        alignment_errors = []
        for name, etype, pos, bid, dirn in all_entities:
            if etype in STEAM_ALIGNMENT_EXEMPT_TYPES:
                continue
            w, h = _get_entity_size(etype, dirn)
            if w % 2 == 1:  # Odd size: x.5 coordinate
                if pos.x != round(pos.x * 2) / 2 or pos.x == int(pos.x):
                    alignment_errors.append(f"'{name}' ({etype} {w}x{h}): x should be x.5, got {pos.x}")
            else:  # Even size: integer coordinate
                if pos.x != int(pos.x):
                    alignment_errors.append(f"'{name}' ({etype} {w}x{h}): x should be integer, got {pos.x}")
            if h % 2 == 1:
                if pos.y != round(pos.y * 2) / 2 or pos.y == int(pos.y):
                    alignment_errors.append(f"'{name}' ({etype} {w}x{h}): y should be x.5, got {pos.y}")
            else:
                if pos.y != int(pos.y):
                    alignment_errors.append(f"'{name}' ({etype} {w}x{h}): y should be integer, got {pos.y}")
        if alignment_errors:
            result.add_failure("V3.3", "; ".join(alignment_errors[:5]))

        # V3.4: Block bounding boxes are valid, non-overlapping rectangles
        block_errors = []
        block_items = list(pl.blocks.items())
        block_bboxes: Dict[str, Tuple[float, float, float, float]] = {}
        for block_id, block in block_items:
            bbox = _block_bbox_tuple(block)
            block_bboxes[block_id] = bbox
            if not _valid_bbox_rect(bbox):
                block_errors.append(
                    f"Block '{block_id}' has invalid bounding_box: "
                    f"left_top=({bbox[0]},{bbox[1]}), right_bottom=({bbox[2]},{bbox[3]})"
                )
        valid_block_bboxes = [
            (block_id, _normalize_bbox(bbox))
            for block_id, bbox in block_bboxes.items()
            if _valid_bbox_rect(bbox)
        ]
        for i, (id1, bbox1) in enumerate(valid_block_bboxes):
            for id2, bbox2 in valid_block_bboxes[i + 1 :]:
                if _bboxes_overlap(bbox1, bbox2):
                    block_errors.append(
                        f"Block '{id1}' bounding_box overlaps block '{id2}' bounding_box"
                    )
        if block_errors:
            result.add_failure("V3.4", "; ".join(block_errors[:5]))

        # V3.5: Each block-owned entity footprint is fully inside its block bounding box
        containment_errors = []
        for block_id, block in block_items:
            block_bbox = _normalize_bbox(block_bboxes[block_id])
            for entity in [*block.machines, *block.inserters, *block.logistics_chests, *block.containers]:
                direction = getattr(entity, "direction", "north") or "north"
                entity_bbox = _get_entity_bbox(entity.position, entity.type, direction)
                if not _bbox_contains_bbox(block_bbox, entity_bbox):
                    containment_errors.append(
                        f"'{entity.name}' ({entity.type}) in block '{block_id}' "
                        "is not fully inside its block bounding_box"
                    )
        if containment_errors:
            result.add_failure("V3.5", "; ".join(containment_errors[:5]))

    # ============================================================
    # V6: Special Position Requirements
    # ============================================================

    def _validate_special_positions(self, pl: ProductionLine, result: ValidationResult):
        """V6: Validate terrain-dependent entity placement."""
        self._validate_mining_drill_positions(pl, result)
        self._validate_offshore_pump_positions(pl, result)
        self._validate_non_pump_entities_on_land(pl, result)
        self._validate_steam_entity_directions(pl, result)

    def _validate_mining_drill_positions(self, pl: ProductionLine, result: ValidationResult):
        """V6.1: Mining drills must be on ore patches."""
        errors = []
        for block_id, block in pl.blocks.items():
            for machine in block.machines:
                if "mining-drill" in machine.type:
                    on_ore = False
                    drill_bbox = _get_entity_bbox(machine.position, machine.type)
                    for patch in self.config.ore_patches:
                        # Support both flat format {"x":0,"y":0} and nested {"position":{"x":0,"y":0}}
                        if "position" in patch and isinstance(patch["position"], dict):
                            px = patch["position"].get("x", 0)
                            py = patch["position"].get("y", 0)
                        elif "position" in patch and isinstance(patch["position"], (list, tuple)) and len(patch["position"]) >= 2:
                            px, py = patch["position"][0], patch["position"][1]
                        else:
                            px, py = patch.get("x", 0), patch.get("y", 0)
                        radius = patch.get("radius", 10)
                        # Check if drill overlaps with ore patch circle
                        drill_cx = (drill_bbox[0] + drill_bbox[2]) / 2
                        drill_cy = (drill_bbox[1] + drill_bbox[3]) / 2
                        dist = math.sqrt((drill_cx - px) ** 2 + (drill_cy - py) ** 2)
                        if dist <= radius + 1.5:  # Allow some margin
                            on_ore = True
                            break
                    if not on_ore:
                        errors.append(
                            f"Drill '{machine.name}' at ({machine.position.x},{machine.position.y}) "
                            f"not on any ore patch"
                        )
        if errors:
            result.add_failure("V6.1", "; ".join(errors))

    def _validate_offshore_pump_positions(self, pl: ProductionLine, result: ValidationResult):
        """V6.2: Offshore pumps must be placed at a configured water shoreline."""
        offshore_pumps = [ge for ge in pl.global_entities if ge.type == "offshore-pump"]
        if not offshore_pumps:
            return

        if not self.config.water_patches:
            result.add_failure(
                "V6.2",
                "Offshore pumps found but task config contains no water patches",
            )
            return

        errors = []
        for pump in offshore_pumps:
            direction = getattr(pump, "direction", "north") or "north"
            pump_bbox = _get_entity_bbox(pump.position, pump.type, direction)
            shoreline_patches = [
                patch
                for patch in self.config.water_patches
                if _bbox_near_water_shoreline(pump_bbox, patch)
            ]
            if not shoreline_patches:
                errors.append(
                    f"Offshore pump '{pump.name}' at ({pump.position.x},{pump.position.y}) "
                    "is not adjacent to or overlapping any configured water shoreline"
                )
                continue
            if not any(_pump_direction_points_to_water(pump, patch) for patch in shoreline_patches):
                errors.append(
                    f"Offshore pump '{pump.name}' at ({pump.position.x},{pump.position.y}) "
                    f"has direction '{direction}' that does not point into the configured water patch"
                )

        if errors:
            result.add_failure("V6.2", "; ".join(errors[:5]))

    def _validate_non_pump_entities_on_land(self, pl: ProductionLine, result: ValidationResult):
        """V6.3: Non-offshore entities must not overlap configured water rectangles."""
        if not self.config.water_patches:
            return

        water_rects = [_water_patch_bbox(patch) for patch in self.config.water_patches]
        errors = []

        all_entities = []
        for block_id, block in pl.blocks.items():
            for entity in [*block.machines, *block.inserters, *block.logistics_chests, *block.containers]:
                all_entities.append((entity, block_id))
        for entity in pl.global_entities:
            all_entities.append((entity, "global"))

        for entity, block_id in all_entities:
            if entity.type == "offshore-pump":
                continue
            direction = getattr(entity, "direction", "north") or "north"
            bbox = _get_entity_bbox(entity.position, entity.type, direction)
            if any(_bbox_overlaps_rect(bbox, rect) for rect in water_rects):
                errors.append(
                    f"'{entity.name}' ({entity.type}) in '{block_id}' overlaps configured water"
                )

        if errors:
            result.add_failure("V6.3", "; ".join(errors[:5]))

    def _validate_steam_entity_directions(self, pl: ProductionLine, result: ValidationResult):
        """V6.4: Boiler, steam-engine, and pipe positions must form a plausible steam chain."""
        boilers = [ge for ge in pl.global_entities if ge.type == "boiler"]
        steam_engines = [ge for ge in pl.global_entities if ge.type == "steam-engine"]
        if not boilers or not steam_engines:
            return

        boiler_directions = {getattr(boiler, "direction", "north") or "north" for boiler in boilers}
        engine_directions = {getattr(engine, "direction", "north") or "north" for engine in steam_engines}
        if boiler_directions != engine_directions:
            result.add_failure(
                "V6.4",
                "Boilers and steam engines must face the same direction within the steam chain; "
                f"boiler directions={sorted(boiler_directions)}, "
                f"steam-engine directions={sorted(engine_directions)}",
            )
            return

        pipes = [ge for ge in pl.global_entities if ge.type == "pipe"]
        pipe_points = [_entity_point(pipe) for pipe in pipes]
        boiler_points = [_entity_point(boiler) for boiler in boilers]
        engine_points = [_entity_point(engine) for engine in steam_engines]

        errors = []
        for boiler in boilers:
            water_points = _boiler_water_pipe_points(boiler)
            if not any(
                any(_steam_points_close(candidate, pipe_point) for pipe_point in pipe_points)
                for candidate in water_points
            ):
                expected = ", ".join(f"({x:g},{y:g})" for x, y in water_points)
                errors.append(
                    f"Boiler '{boiler.name}' has no water-side pipe on its input short side; "
                    f"expected a pipe near one of {expected}"
                )

            steam_point = _boiler_steam_pipe_point(boiler)
            has_steam_output = any(_steam_points_close(steam_point, pipe_point) for pipe_point in pipe_points)
            if not has_steam_output:
                expected = f"({steam_point[0]:g},{steam_point[1]:g})"
                errors.append(
                    f"Boiler '{boiler.name}' has no steam-side pipe along its output direction; "
                    f"expected a pipe near {expected}"
                )

        for engine in steam_engines:
            input_pipe = _engine_input_pipe_point(engine)
            has_input_pipe = any(_steam_points_close(input_pipe, pipe_point) for pipe_point in pipe_points)
            direction = getattr(engine, "direction", "north") or "north"
            dx, dy, _, _ = _steam_vectors(direction)
            upstream_engine_point = (engine.position.x - 5 * dx, engine.position.y - 5 * dy)
            has_upstream_engine = any(_steam_points_close(upstream_engine_point, other_point) for other_point in engine_points)
            if not (has_input_pipe or has_upstream_engine):
                expected = f"({input_pipe[0]:g},{input_pipe[1]:g})"
                errors.append(
                    f"Steam engine '{engine.name}' has no steam input on its upstream short side; "
                    f"expected a pipe near {expected} or a previous steam engine in the row"
                )

        if errors:
            result.add_failure("V6.4", "; ".join(errors[:5]))

    # ============================================================
    # V5: Power Grid
    # ============================================================

    def _validate_power_grid(self, pl: ProductionLine, result: ValidationResult):
        """V5.1: Consumers must be covered by poles connected to a power source."""
        
        # Collect all poles and power sources
        poles = []
        power_sources = []
        for ge in pl.global_entities:
            if ge.type in VALID_POLE_TYPES:
                poles.append(ge)
            if ge.type in VALID_POWER_SOURCE_TYPES:
                power_sources.append(ge)

        if not poles:
            result.add_failure("V5.1", "No electric poles found")
            return

        if not power_sources:
            result.add_failure("V5.2", "No power generation source found")
            return

        powered_pole_indexes = self._get_source_connected_poles(poles, power_sources)
        if not powered_pole_indexes:
            result.add_failure(
                "V5.2",
                "No electric pole is connected to a power source. Place a pole or substation so its supply area covers at least one steam-engine, then bridge from that pole network to consumers.",
            )
            return

        # Collect all power consumers
        consumers = []
        for block_id, block in pl.blocks.items():
            for m in block.machines:
                if m.type in POWER_CONSUMERS:
                    consumers.append((m.name, m.type, m.position, block_id))
            for ins in block.inserters:
                consumers.append((ins.name, ins.type, ins.position, block_id))
        # Roboports also need power
        for ge in pl.global_entities:
            if ge.type == "roboport":
                consumers.append((ge.name, ge.type, ge.position, "global"))

        # Check each consumer is within at least one source-connected
        # electric pole/substation supply area.
        # Keep a short diagnosis for common cases where a consumer is covered only
        # by isolated electric infrastructure that is not connected to the
        # generation network.
        unpowered = []
        for name, etype, pos, bid in consumers:
            powered = False
            for pole_index in powered_pole_indexes:
                pole = poles[pole_index]
                supply_radius = POLE_SUPPLY_AREAS.get(pole.type, 0)
                # Check if entity center is within a source-connected pole supply area
                if abs(pos.x - pole.position.x) <= supply_radius and \
                   abs(pos.y - pole.position.y) <= supply_radius:
                    powered = True
                    break
            if not powered:
                covering_unpowered_power_entities = []
                covering_powered_power_entities = []
                for pole_index, pole in enumerate(poles):
                    supply_radius = POLE_SUPPLY_AREAS.get(pole.type, 0)
                    if abs(pos.x - pole.position.x) <= supply_radius and \
                       abs(pos.y - pole.position.y) <= supply_radius:
                        pole_summary = (
                            f"'{pole.name}' ({pole.type}) at "
                            f"({pole.position.x},{pole.position.y})"
                        )
                        if pole_index in powered_pole_indexes:
                            covering_powered_power_entities.append(pole_summary)
                        else:
                            covering_unpowered_power_entities.append(pole_summary)
                if covering_powered_power_entities:
                    reason = (
                        "covered by source-connected electric pole/substation(s), "
                        "but internal power check still marked it unpowered"
                    )
                elif covering_unpowered_power_entities:
                    reason = (
                        "covered only by electric pole/substation(s) not connected "
                        "to a power source: "
                        + ", ".join(covering_unpowered_power_entities[:3])
                    )
                else:
                    reason = "not inside the supply area of any electric pole/substation"
                unpowered.append({
                    "name": name, "type": etype,
                    "section": _section_kind_and_priority(etype, bid)[0],
                    "position": {"x": pos.x, "y": pos.y},
                    "reason": reason,
                })

        if unpowered:
            result.unpowered_entities = unpowered
            details = "; ".join(
                f"'{e['name']}' ({e['type']}) at "
                f"({e['position']['x']},{e['position']['y']}): {e['reason']}"
                for e in unpowered[:5]
            )
            result.add_failure("V5.1", f"{len(unpowered)} entities not powered: {details}")

    def _get_source_connected_poles(self, poles: List[GlobalEntity], power_sources: List[GlobalEntity]) -> Set[int]:
        """Return pole indexes in components that are connected to at least one power source."""
        source_connected = set()
        for i, pole in enumerate(poles):
            radius = POLE_SUPPLY_AREAS.get(pole.type, 0)
            if any(
                abs(source.position.x - pole.position.x) <= radius
                and abs(source.position.y - pole.position.y) <= radius
                for source in power_sources
            ):
                source_connected.add(i)

        if not source_connected:
            return set()

        connected = set(source_connected)
        changed = True
        while changed:
            changed = False
            for i, pole in enumerate(poles):
                if i in connected:
                    continue
                for j in connected:
                    other = poles[j]
                    max_distance = min(
                        POLE_WIRE_REACH.get(pole.type, 0),
                        POLE_WIRE_REACH.get(other.type, 0),
                    )
                    if _distance(pole.position, other.position) <= max_distance:
                        connected.add(i)
                        changed = True
                        break

        return connected

    # ============================================================
    # V4: Logistics Robot Network
    # ============================================================

    def _validate_logistics_network(self, pl: ProductionLine, result: ValidationResult):
        """V4: Validate logistics robot network."""

        # Collect all logistics chests and roboports
        all_logistics_chests = pl.get_all_logistics_chests()
        roboports = pl.get_all_roboports()

        if not roboports and all_logistics_chests:
            result.add_failure("V4.1", "Logistics chests found but no roboport placed. "
                             "Roboport is required for logistics robot network.")
            return

        # VR1: All logistics chests within roboport coverage
        if roboports:
            uncovered = []
            for chest in all_logistics_chests:
                covered = False
                for rp in roboports:
                    if (abs(chest.position.x - rp.position.x) <= ROBOPORT_LOGISTICS_RADIUS and
                        abs(chest.position.y - rp.position.y) <= ROBOPORT_LOGISTICS_RADIUS):
                        covered = True
                        break
                if not covered:
                    uncovered.append(f"'{chest.name}' at ({chest.position.x},{chest.position.y})")

            if uncovered:
                result.add_failure("V4.1",
                    f"{len(uncovered)} logistics chests outside roboport coverage (center must be within +/-24 per axis): "
                    + "; ".join(uncovered[:5]))

        # VR2: Requester chests have valid request_filters
        requesters_without_filters = []
        invalid_filters = []
        for chest in all_logistics_chests:
            if chest.type == "logistic-chest-requester":
                if not chest.request_filters:
                    requesters_without_filters.append(f"'{chest.name}'")
                else:
                    for rf in chest.request_filters:
                        rf_index = getattr(rf, "index", 0)
                        if rf_index < 1 or rf_index > 30:
                            invalid_filters.append(
                                f"'{chest.name}': request filter index for '{rf.name}' must be 1-30, got {rf_index}"
                            )
                        if not rf.name:
                            invalid_filters.append(f"'{chest.name}': empty filter name")
                        if getattr(rf, "count", 0) <= 0:
                            invalid_filters.append(
                                f"'{chest.name}': non-positive request count for '{rf.name}'"
                            )

        if requesters_without_filters:
            result.add_failure("V4.2",
                f"Requester chests without request_filters (robots won't deliver!): "
                + "; ".join(requesters_without_filters))

        if invalid_filters:
            result.add_failure("V4.2",
                f"Invalid request filters: " + "; ".join(invalid_filters))

        if all_logistics_chests and (pl.logistic_robot_count is None or pl.logistic_robot_count <= 0):
            result.add_failure(
                "V4.6",
                "Robot logistics chests are present, so top-level logistic_robot_count "
                "must be a positive integer inserted into the roboport network."
            )

        # VR3: Provider-Requester pairing check
        # For each requested item, there should be at least one provider chest
        requested_items = set()
        for chest in all_logistics_chests:
            if chest.type == "logistic-chest-requester" and chest.request_filters:
                for rf in chest.request_filters:
                    requested_items.add(rf.name)

        provider_items = set()
        for chest in all_logistics_chests:
            if chest.type in ("logistic-chest-passive-provider", "logistic-chest-active-provider"):
                # Provider chests don't specify items - they provide whatever is inserted
                # We can't fully validate this statically, but we note the existence
                provider_items.add(chest.name)

        if requested_items and not provider_items:
            result.add_failure("V4.3",
                f"Requester chests request items ({requested_items}) but no provider chests found. "
                "Robots need provider chests as source.")

        # VR4: Roboport network connectivity (if multiple roboports)
        if len(roboports) > 1:
            # Check that all roboports form a connected network
            # Two roboports are connected if their logistics areas overlap
            connected = {0}  # Start with first roboport
            changed = True
            while changed:
                changed = False
                for i in range(len(roboports)):
                    if i in connected:
                        continue
                    for j in connected.copy():
                        dist = _distance(roboports[i].position, roboports[j].position)
                        # Two roboports are connected if distance <= 2 * radius
                        if dist <= 2 * ROBOPORT_LOGISTICS_RADIUS:
                            connected.add(i)
                            changed = True
                            break

            if len(connected) < len(roboports):
                disconnected = [roboports[i].name for i in range(len(roboports)) if i not in connected]
                result.add_failure("V4.4",
                    f"Roboport network not connected. Disconnected: {', '.join(disconnected)}")

        # VR5: Inserter positions (must be adjacent to machine AND chest)
        self._validate_inserter_positions(pl, result)

        # VR7: Required machine/chest material-flow interfaces
        self._validate_material_flow_interfaces(pl, result)

    def _validate_inserter_positions(self, pl: ProductionLine, result: ValidationResult):
        """V4.5: Check inserters can reach both their source and destination."""
        errors = []

        for block_id, block in pl.blocks.items():
            # Collect all entity bounding boxes in this block
            block_entities = {}  # name -> (type, bbox)
            for m in block.machines:
                block_entities[m.name] = (m.type, _get_entity_bbox(m.position, m.type))
            for chest in block.logistics_chests:
                block_entities[chest.name] = (chest.type, _get_entity_bbox(chest.position, chest.type))
            for cont in block.containers:
                block_entities[cont.name] = (cont.type, _get_entity_bbox(cont.position, cont.type))

            for ins in block.inserters:
                pickup_x, pickup_y, drop_x, drop_y = _inserter_transfer_points(ins)

                # Check pickup reaches some entity
                pickup_reaches = False
                for ename, (etype, ebbox) in block_entities.items():
                    if _point_in_bbox(pickup_x, pickup_y, ebbox):
                        pickup_reaches = True
                        break

                # Check drop reaches some entity
                drop_reaches = False
                for ename, (etype, ebbox) in block_entities.items():
                    if _point_in_bbox(drop_x, drop_y, ebbox):
                        drop_reaches = True
                        break

                if not pickup_reaches:
                    errors.append(
                        f"Inserter '{ins.name}' in block '{block_id}': "
                        f"pickup at ({pickup_x:.1f},{pickup_y:.1f}) doesn't reach any entity"
                    )
                if not drop_reaches:
                    errors.append(
                        f"Inserter '{ins.name}' in block '{block_id}': "
                        f"drop at ({drop_x:.1f},{drop_y:.1f}) doesn't reach any entity"
                    )

        if errors:
            result.add_failure("V4.5", "; ".join(errors[:5]))

    def _validate_material_flow_interfaces(self, pl: ProductionLine, result: ValidationResult):
        """V4.7: Check required inserter-mediated machine/chest interfaces."""
        errors = []

        for block_id, block in pl.blocks.items():
            block_entities = {}
            for m in block.machines:
                block_entities[m.name] = (
                    m.type,
                    _get_entity_bbox(m.position, m.type, getattr(m, "direction", "north") or "north"),
                )
            for chest in block.logistics_chests:
                block_entities[chest.name] = (
                    chest.type,
                    _get_entity_bbox(chest.position, chest.type, getattr(chest, "direction", "north") or "north"),
                )
            for cont in block.containers:
                block_entities[cont.name] = (
                    cont.type,
                    _get_entity_bbox(cont.position, cont.type, getattr(cont, "direction", "north") or "north"),
                )

            inserter_pick_sources: Set[str] = set()
            inserter_drop_targets: Set[str] = set()

            for ins in block.inserters:
                pickup_x, pickup_y, drop_x, drop_y = _inserter_transfer_points(ins)
                for entity_name, (_etype, bbox) in block_entities.items():
                    if _point_in_bbox(pickup_x, pickup_y, bbox):
                        inserter_pick_sources.add(entity_name)
                    if _point_in_bbox(drop_x, drop_y, bbox):
                        inserter_drop_targets.add(entity_name)

            mining_output_targets: Set[str] = set()
            for machine in block.machines:
                if "mining-drill" not in machine.type:
                    continue
                output_x, output_y = _mining_drill_output_point(machine)
                for entity_name, (etype, bbox) in block_entities.items():
                    if etype in VALID_CONTAINER_TYPES | PROVIDER_CHEST_TYPES and _point_in_bbox(output_x, output_y, bbox):
                        mining_output_targets.add(entity_name)

            for machine in block.machines:
                if machine.type not in MATERIAL_IO_MACHINE_TYPES:
                    continue
                if machine.name not in inserter_drop_targets:
                    errors.append(
                        f"Machine '{machine.name}' ({machine.type}) in block '{block_id}' "
                        "has no inserter input/drop into it"
                    )
                if machine.name not in inserter_pick_sources:
                    errors.append(
                        f"Machine '{machine.name}' ({machine.type}) in block '{block_id}' "
                        "has no inserter output/pickup from it"
                    )

            for cont in block.containers:
                if cont.name not in inserter_drop_targets and cont.name not in mining_output_targets:
                    errors.append(
                        f"Container '{cont.name}' in block '{block_id}' is not fed by an inserter drop "
                        "or direct mining-drill output"
                    )

            for chest in block.logistics_chests:
                if chest.type in PROVIDER_CHEST_TYPES:
                    if chest.name not in inserter_drop_targets and chest.name not in mining_output_targets:
                        errors.append(
                            f"Provider chest '{chest.name}' in block '{block_id}' is not fed by an inserter drop "
                            "or direct mining-drill output"
                        )
                if chest.type == "logistic-chest-requester" and chest.name not in inserter_pick_sources:
                    errors.append(
                        f"Requester chest '{chest.name}' in block '{block_id}' is not picked from by any inserter"
                    )

        if errors:
            result.add_failure("V4.7", "; ".join(errors[:5]))

    # ============================================================
    # V2: Recipe Chain Completeness
    # ============================================================

    def _validate_recipe_chain(self, pl: ProductionLine, result: ValidationResult):
        """V2: Check recipe validity and target-product producibility."""
        from ..tasks.recipes import RECIPES, get_recipe_chain

        self._check_recipes(pl, result)
        target = self.config.target_product

        active_assembly_recipes = set()
        has_furnace = False
        for block in pl.blocks.values():
            for machine in block.machines:
                if machine.type in ("electric-furnace", "stone-furnace", "steel-furnace"):
                    has_furnace = True
                elif machine.type in ("assembling-machine-1", "assembling-machine-2", "assembling-machine-3"):
                    if machine.recipe:
                        active_assembly_recipes.add(machine.recipe)

        recipe_chain = get_recipe_chain(target)
        if target in RECIPES and target not in recipe_chain:
            recipe_chain.append(target)

        missing_producers = []
        for recipe_name in recipe_chain:
            recipe = RECIPES.get(recipe_name)
            if not recipe:
                continue
            if recipe.machine_type == "assembling-machine":
                if recipe_name not in active_assembly_recipes:
                    missing_producers.append(recipe_name)
            elif recipe.machine_type == "furnace":
                # Furnaces auto-select the plate recipe from supplied input during execution.
                if not has_furnace:
                    missing_producers.append(recipe_name)

        if missing_producers:
            result.add_failure(
                "V2.2",
                "Missing producer machine(s) for required recipe(s) in the target chain: "
                + ", ".join(missing_producers)
                + ". Add assembling machines with these explicit recipes, or furnaces for plate smelting.",
            )

    def _annotate_cost_metrics(self, pl: ProductionLine, result: ValidationResult):
        """Attach cost metrics without affecting pass/fail validity."""
        cost_summary = summarize_entity_costs(pl)
        result.metrics["cost"] = cost_summary

        unknown_entity_types = cost_summary.get("unknown_entity_types", [])
        if unknown_entity_types:
            result.add_warning(
                "C1",
                "Missing cost entries for entity types: " + ", ".join(unknown_entity_types),
            )


# ============================================================
# Convenience Function
# ============================================================


def validate_production_line(output: Any, validation_config: Dict) -> ValidationResult:
    """
    Validate a production line output.

    Args:
        output: Dict or JSON string of the design
        validation_config: Dict with map_bounds, ore_patches, target_product, target_rate.
            Extra keys are ignored by the validator.

    Returns:
        ValidationResult with all check results
    """
    task_config = TaskConfig.from_dict(validation_config)
    validator = ProductionLineValidator(task_config)
    return validator.validate(output)
