# -*- coding: utf-8 -*-
"""
Benchmark Task Registry

Central registry for all benchmark tasks.
Supports loading from JSON config files or programmatic creation.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Literal

from .task_config import (
    BenchmarkTaskConfig,
    EnvironmentConfig,
    OrePatch,
    WaterPatch,
    create_level1_task,
    create_level2_task,
    create_level3_task,
    create_level4_task,
)


class BenchmarkTaskRegistry:
    """
    Central registry for benchmark tasks.

    Provides methods to access and filter tasks.
    """

    def __init__(self):
        self._tasks: Dict[str, BenchmarkTaskConfig] = {}
        self._load_json_configs()  # First try JSON files
        self._initialize_remaining_tasks()  # Fill gaps with programmatic creation

    def _load_json_configs(self):
        """Load task configs from JSON files in configs directory."""
        configs_dir = Path(__file__).parent / "configs"

        if not configs_dir.exists():
            configs_dir.mkdir(parents=True, exist_ok=True)
            return

        for json_file in configs_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)

                if data.get("task_id") != json_file.stem:
                    print(
                        f"Warning: Skipping {json_file}: task_id "
                        f"'{data.get('task_id')}' does not match filename "
                        f"'{json_file.stem}'"
                    )
                    continue

                task = self._json_to_task_config(data)
                self._tasks[task.task_id] = task
            except Exception as e:
                print(f"Warning: Failed to load {json_file}: {e}")

    def _json_to_task_config(self, data: dict) -> BenchmarkTaskConfig:
        """Convert JSON data to BenchmarkTaskConfig."""
        env_data = data["environment"]

        ore_patches = [
            OrePatch(
                ore_type=patch["ore_type"],
                position=tuple(patch["position"]),
                radius=patch["radius"],
            )
            for patch in env_data["ore_patches"]
        ]

        water_patches = [
            WaterPatch(
                position=tuple(wp["position"]),
                width=wp.get("width", 10),
                height=wp.get("height", 6),
            )
            for wp in env_data.get("water_patches", [])
        ]

        environment = EnvironmentConfig(
            map_width=env_data["map_width"],
            map_height=env_data["map_height"],
            ore_patches=ore_patches,
            water_patches=water_patches,
            difficulty=env_data["difficulty"],
        )

        return BenchmarkTaskConfig(
            task_id=data["task_id"],
            level=data["level"],
            target_product=data["target_product"],
            target_rate_per_minute=data.get(
                "target_rate_per_minute",
                data.get("target_rate"),
            ),
            environment=environment,
            cost_model=data.get("cost_model", "factorio_recipe_cost_v1"),
            description=data.get("description", ""),
            capacity_level=data.get("capacity_level", "low"),
        )

    def _initialize_remaining_tasks(self):
        """Initialize remaining tasks not loaded from JSON."""

        # Level 1: iron-plate, iron-gear-wheel
        for product in ["iron-plate", "iron-gear-wheel"]:
            for capacity in ["low", "high"]:
                for difficulty in ["easy", "hard"]:
                    task = create_level1_task(product, capacity, difficulty)
                    # Only add if not already loaded from JSON
                    if task.task_id not in self._tasks:
                        self._tasks[task.task_id] = task

        # Level 2: electronic-circuit, automation-science-pack
        for product in ["electronic-circuit", "automation-science-pack"]:
            for capacity in ["low", "high"]:
                for difficulty in ["easy", "hard"]:
                    task = create_level2_task(product, capacity, difficulty)
                    if task.task_id not in self._tasks:
                        self._tasks[task.task_id] = task

        # Level 3: inserter, logistic-science-pack
        for product in ["inserter", "logistic-science-pack"]:
            for capacity in ["low", "high"]:
                for difficulty in ["easy", "hard"]:
                    task = create_level3_task(product, capacity, difficulty)
                    if task.task_id not in self._tasks:
                        self._tasks[task.task_id] = task

        # Level 4: military-science-pack, assembling-machine-2
        for product in ["military-science-pack", "assembling-machine-2"]:
            for capacity in ["low", "high"]:
                for difficulty in ["easy", "hard"]:
                    task = create_level4_task(product, capacity, difficulty)
                    if task.task_id not in self._tasks:
                        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> BenchmarkTaskConfig:
        """Get task by ID."""
        if task_id not in self._tasks:
            available = ", ".join(list(self._tasks.keys())[:5])
            raise KeyError(
                f"Unknown task: {task_id}. "
                f"Available: {available}... ({len(self._tasks)} total)"
            )
        return self._tasks[task_id]

    def list_all(self) -> List[str]:
        """List all task IDs."""
        return list(self._tasks.keys())

    def list_by_level(self, level: int) -> List[str]:
        """List task IDs for a specific level."""
        return [task_id for task_id, task in self._tasks.items() if task.level == level]

    def list_by_product(self, product: str) -> List[str]:
        """List task IDs for a specific product."""
        return [
            task_id
            for task_id, task in self._tasks.items()
            if task.target_product == product
        ]

    def list_by_difficulty(self, difficulty: str) -> List[str]:
        """List task IDs for a specific environment difficulty."""
        return [
            task_id
            for task_id, task in self._tasks.items()
            if task.environment.difficulty == difficulty
        ]

    def filter(
        self,
        level: Optional[int] = None,
        product: Optional[str] = None,
        capacity: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> List[str]:
        """Filter tasks by multiple criteria."""
        results = []
        for task_id, task in self._tasks.items():
            if level is not None and task.level != level:
                continue
            if product is not None and task.target_product != product:
                continue
            if capacity is not None and task.capacity_level != capacity:
                continue
            if difficulty is not None and task.environment.difficulty != difficulty:
                continue
            results.append(task_id)
        return results

    def get_summary(self) -> Dict[str, List[str]]:
        """Get summary of tasks by level."""
        return {
            "level_1": self.list_by_level(1),
            "level_2": self.list_by_level(2),
            "level_3": self.list_by_level(3),
            "level_4": self.list_by_level(4),
        }

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._tasks

    def __iter__(self):
        return iter(self._tasks.values())


# Global registry instance
_registry = BenchmarkTaskRegistry()


# ============================================================
# Module-level convenience functions
# ============================================================


def get_task(task_id: str) -> BenchmarkTaskConfig:
    """Get task configuration by ID."""
    return _registry.get(task_id)


def list_tasks() -> List[str]:
    """List all task IDs."""
    return _registry.list_all()


def list_tasks_by_level(level: int) -> List[str]:
    """List task IDs for a specific level."""
    return _registry.list_by_level(level)


def filter_tasks(
    level: Optional[int] = None,
    product: Optional[str] = None,
    capacity: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> List[str]:
    """Filter tasks by criteria."""
    return _registry.filter(level, product, capacity, difficulty)


def get_registry() -> BenchmarkTaskRegistry:
    """Get the global registry instance."""
    return _registry
