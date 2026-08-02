from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class TriggerEvent:
    event_type: str
    delta: int
    timestamp: float


class ReplanEventMonitor:
    """
    Detects only two trigger types by monotonic counters:
    - new_order: KPI total_orders increased
    - rework: sum(QualityCheck.reworked_count) increased
    """

    def __init__(self, factory):
        self.factory = factory
        self.last_total_orders = self._read_total_orders()
        self.last_total_reworks = self._read_total_reworks()

    def _read_total_orders(self) -> int:
        if not getattr(self.factory, "kpi_calculator", None):
            return 0
        stats = getattr(self.factory.kpi_calculator, "stats", None)
        if stats is None:
            return 0
        return int(getattr(stats, "total_orders", 0))

    def _read_total_reworks(self) -> int:
        total_reworks = 0
        for line in self.factory.lines.values():
            qc = line.stations.get("QualityCheck")
            if not qc:
                continue
            total_reworks += int(qc.stats.get("reworked_count", 0))
        return total_reworks

    def poll(self) -> List[TriggerEvent]:
        now = float(self.factory.env.now)
        events: List[TriggerEvent] = []

        total_orders = self._read_total_orders()
        if total_orders > self.last_total_orders:
            events.append(
                TriggerEvent(
                    event_type="new_order",
                    delta=total_orders - self.last_total_orders,
                    timestamp=now,
                )
            )
        self.last_total_orders = total_orders

        total_reworks = self._read_total_reworks()
        if total_reworks > self.last_total_reworks:
            events.append(
                TriggerEvent(
                    event_type="rework",
                    delta=total_reworks - self.last_total_reworks,
                    timestamp=now,
                )
            )
        self.last_total_reworks = total_reworks

        return events
