from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class TransportTask:
    task_id: str
    product_id: str
    product_type: str
    category: str
    line_id: Optional[str]
    source_point: str
    destination_point: str
    source_device: str
    destination_device: str
    source_buffer: Optional[str]
    release_time: float
    selection_group: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_raw_option(self) -> bool:
        return self.category == "raw_to_station_a"


@dataclass
class AGVSnapshot:
    agv_key: str
    line_id: str
    agv_id: str
    current_point: str
    projected_start_point: str
    projected_available_time: float
    operation_time: float
    status: str


@dataclass
class ScheduledTask:
    task: TransportTask
    agv_key: str
    planned_start: float
    planned_end: float


@dataclass
class PlanResult:
    status: str
    objective_value: float
    selected_line_by_product: Dict[str, str]
    assignments_by_agv: Dict[str, List[ScheduledTask]]
    selected_task_ids: Set[str]
    message: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)
