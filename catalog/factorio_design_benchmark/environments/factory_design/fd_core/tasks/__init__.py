# -*- coding: utf-8 -*-
"""
Benchmark Tasks Module

Modular task configuration system for FLE benchmark.
"""

from .task_config import (
    BenchmarkTaskConfig,
    OrePatch,
    EnvironmentConfig,
)
from .recipes import (
    RECIPES,
    get_recipe,
    get_recipe_chain,
    get_required_inputs,
)
from .task_registry import (
    BenchmarkTaskRegistry,
    get_task,
    list_tasks,
    list_tasks_by_level,
)

__all__ = [
    # Config
    "BenchmarkTaskConfig",
    "OrePatch",
    "EnvironmentConfig",
    # Recipes
    "RECIPES",
    "get_recipe",
    "get_recipe_chain",
    "get_required_inputs",
    # Registry
    "BenchmarkTaskRegistry",
    "get_task",
    "list_tasks",
    "list_tasks_by_level",
]
