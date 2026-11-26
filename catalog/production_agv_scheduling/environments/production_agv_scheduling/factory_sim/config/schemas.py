"""Dependency-free data records used by the extracted simulator.

The upstream project used Pydantic models mainly as attribute containers and
for ``model_dump``/``model_dump_json``.  These small records preserve that API
without adding a native-extension dependency to the environment runtime.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Dict, List, Optional


class DeviceStatus(str, Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    MAINTENANCE = "maintenance"
    SCRAP = "scrap"
    WORKING = "working"
    BLOCKED = "blocked"
    FAULT = "fault"
    MOVING = "moving"
    INTERACTING = "interacting"
    CHARGING = "charging"


class OrderPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecordModel:
    """Minimal typed-record API compatible with the simulator's usage."""

    def __init__(self, **values: Any) -> None:
        fields = self._fields()
        unknown = sorted(set(values) - set(fields))
        if unknown:
            raise TypeError(f"Unexpected {type(self).__name__} fields: {unknown!r}")
        for name in fields:
            if name in values:
                value = values[name]
            elif hasattr(type(self), name):
                value = copy.deepcopy(getattr(type(self), name))
            else:
                raise TypeError(f"Missing required {type(self).__name__} field: {name}")
            setattr(self, name, value)
        self._validate()

    @classmethod
    def _fields(cls) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for parent in reversed(cls.__mro__):
            result.update(getattr(parent, "__annotations__", {}))
        return result

    @classmethod
    def model_validate(cls, value: Mapping[str, Any]) -> "RecordModel":
        if not isinstance(value, Mapping):
            raise TypeError(f"{cls.__name__} input must be an object.")
        return cls(**dict(value))

    def _validate(self) -> None:
        pass

    def model_dump(self) -> Dict[str, Any]:
        return {name: _plain(getattr(self, name)) for name in self._fields()}

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False)


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, RecordModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


class DeviceDetailedStatus(RecordModel):
    device_id: str
    device_type: str
    current_status: DeviceStatus
    temperature: float
    vibration_level: float
    power_consumption: float
    efficiency_rate: float
    cycle_count: int
    last_maintenance_time: float
    operating_hours: float
    fault_symptom: Optional[str] = None
    frozen_until: Optional[float] = None
    precision_level: Optional[float] = None
    tool_wear_level: Optional[float] = None
    lubricant_level: Optional[float] = None
    battery_level: Optional[float] = None
    position_accuracy: Optional[float] = None
    load_weight: Optional[float] = None


class DiagnosisResult(RecordModel):
    device_id: str
    diagnosis_command: str
    is_correct: bool
    repair_time: float
    penalty_applied: bool
    affected_devices: List[str] = []
    can_skip: bool


class AgentCommand(RecordModel):
    command_id: Optional[str] = None
    action: str
    target: str
    params: Dict[str, Any] = {}

    def _validate(self) -> None:
        if self.command_id is not None and not isinstance(self.command_id, str):
            raise TypeError("command_id must be a string or null.")
        if not isinstance(self.action, str) or not self.action:
            raise TypeError("action must be a non-empty string.")
        if not isinstance(self.target, str) or not self.target:
            raise TypeError("target must be a non-empty string.")
        if not isinstance(self.params, Mapping):
            raise TypeError("params must be an object.")
        self.params = dict(self.params)


class SystemResponse(RecordModel):
    timestamp: float
    command_id: Optional[str] = None
    response: str


class ProductInfo(RecordModel):
    id: str
    product_type: str
    quality_score: float
    rework_count: int = 0


class StationStatus(RecordModel):
    timestamp: float
    source_id: str
    status: DeviceStatus
    message: Optional[str] = None
    buffer: List[str]
    stats: Dict[str, Any]
    output_buffer: List[str] = []


class AGVStatus(RecordModel):
    timestamp: float
    source_id: str
    status: DeviceStatus
    speed_mps: float
    current_point: str
    position: Dict[str, float]
    target_point: Optional[str] = None
    estimated_time: float
    payload: List[str]
    battery_level: float
    message: Optional[str] = None


class ConveyorStatus(RecordModel):
    timestamp: float
    source_id: str
    status: DeviceStatus
    message: Optional[str] = None
    buffer: List[str]
    upper_buffer: Optional[List[str]] = None
    lower_buffer: Optional[List[str]] = None


class WarehouseStatus(RecordModel):
    timestamp: float
    source_id: str
    message: str
    buffer: List[str]
    stats: Dict[str, Any]


class OrderItem(RecordModel):
    product_type: str
    quantity: int

    def _validate(self) -> None:
        if not isinstance(self.product_type, str) or not self.product_type:
            raise TypeError("product_type must be a non-empty string.")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer.")


class NewOrder(RecordModel):
    order_id: str
    created_at: float
    items: List[OrderItem]
    priority: OrderPriority
    deadline: float

    def _validate(self) -> None:
        if not isinstance(self.priority, OrderPriority):
            self.priority = OrderPriority(self.priority)
        self.items = [item if isinstance(item, OrderItem) else OrderItem.model_validate(item) for item in self.items]


class FaultAlert(RecordModel):
    timestamp: float
    device_id: str
    alert_type: str
    symptom: str
    fault_type: Optional[str]
    estimated_duration: Optional[float]
    message: str


class KPIUpdate(RecordModel):
    timestamp: float
    order_completion_rate: float
    average_production_cycle: float
    on_time_delivery_rate: float
    device_utilization: float
    first_pass_rate: float
    total_production_cost: float
    material_costs: float
    energy_costs: float
    maintenance_costs: float
    scrap_costs: float
    charge_strategy_efficiency: float = 0.0
    agv_energy_efficiency: float = 0.0
    agv_utilization: float = 0.0
    total_orders: int
    completed_orders: int
    active_orders: int
    total_products: int
    active_faults: int


class FactoryStatus(RecordModel):
    timestamp: float
    total_stations: int
    total_agvs: int
    active_orders: int
    total_orders: int
    completed_orders: int
    active_faults: int
    simulation_time: float


DATABASE_SCHEMA = {
    "warehouses": [
        "timestamp REAL",
        "buffer TEXT",
        "message TEXT",
    ],
    "orders": [
        "timestamp REAL",
        "order_id TEXT",
        "items TEXT",
        "priority TEXT",
        "deadline REAL",
    ],
    "stations": [
        "timestamp REAL",
        "status TEXT",
        "buffer TEXT",
        "message TEXT",
    ],
    "qualitychecks": [
        "timestamp REAL",
        "status TEXT",
        "buffer TEXT",
        "output_buffer TEXT",
        "message TEXT",
    ],
    "agvs": [
        "timestamp REAL",
        "status TEXT",
        "current_point TEXT",
        "target_point TEXT",
        "estimated_time REAL",
        "position TEXT",
        "payload TEXT",
        "battery_level REAL",
        "message TEXT",
    ],
    "conveyors": [
        "timestamp REAL",
        "status TEXT",
        "buffer TEXT",
        "message TEXT",
    ],
    "triple_conveyors": [
        "timestamp REAL",
        "status TEXT",
        "buffer TEXT",
        "upper_buffer TEXT",
        "lower_buffer TEXT",
        "message TEXT",
    ],
    "faults": [
        "timestamp REAL",
        "alert_type TEXT",
        "symptom TEXT",
        "fault_type TEXT",
        "estimated_duration REAL",
        "message TEXT",
    ],
    "kpi": [
        "timestamp REAL",
        "order_completion_rate REAL",
        "average_production_cycle REAL",
        "on_time_delivery_rate REAL",
        "device_utilization REAL",
        "first_pass_rate REAL",
        "total_production_cost REAL",
        "material_costs REAL",
        "energy_costs REAL",
        "maintenance_costs REAL",
        "scrap_costs REAL",
        "charge_strategy_efficiency REAL",
        "agv_energy_efficiency REAL",
        "agv_utilization REAL",
        "total_orders INTEGER",
        "completed_orders INTEGER",
        "active_orders INTEGER",
        "total_products INTEGER",
        "active_faults INTEGER",
    ],
    "response": [
        "timestamp REAL",
        "command_id TEXT",
        "response TEXT",
    ],
}

