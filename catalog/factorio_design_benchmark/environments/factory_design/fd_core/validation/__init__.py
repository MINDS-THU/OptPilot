# -*- coding: utf-8 -*-
"""
Benchmark Validation Module - Logistics Robot Version

Validates ProductionLine designs with logistics robot network.
"""

from .validator import (
    ValidationResult,
    ProductionLineValidator,
    validate_production_line,
    TaskConfig,
    ENTITY_SIZES,
    VALID_MACHINE_TYPES,
    VALID_INSERTER_TYPES,
    VALID_LOGISTICS_CHEST_TYPES,
    VALID_CONTAINER_TYPES,
    ROBOPORT_LOGISTICS_RADIUS,
)

__all__ = [
    "ValidationResult",
    "ProductionLineValidator",
    "validate_production_line",
    "TaskConfig",
    "ENTITY_SIZES",
    "VALID_MACHINE_TYPES",
    "VALID_INSERTER_TYPES",
    "VALID_LOGISTICS_CHEST_TYPES",
    "VALID_CONTAINER_TYPES",
    "ROBOPORT_LOGISTICS_RADIUS",
]
