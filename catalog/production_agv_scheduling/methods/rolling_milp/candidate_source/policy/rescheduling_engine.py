from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Dict, List

from .command_dispatcher import SequentialAGVDispatcher
from .event_monitor import ReplanEventMonitor, TriggerEvent
from .model_solver import MIPTaskScheduler
from .state_extractor import SimulationStateExtractor


class EventDrivenReschedulingEngine:
    """
    Full chain orchestrator:
    - event monitoring (new_order/rework only)
    - state extraction
    - MIP solve
    - sequential AGV command dispatch
    """

    def __init__(
        self,
        simulation,
        variant: str,
        solver_time_limit_sec: float = 2.0,
        step_sec: float = 0.5,
        realtime_sleep_sec: float = 0.0,
        min_replan_interval_sec: float = 8.0,
        max_raw_products: int = 24,
        max_future_tasks: int = 24,
        future_horizon_sec: float = 120.0,
        max_mip_tasks: int = 120,
        accept_partial_mip_solution: bool = False,
        fallback_mode: str = "heuristic",
        use_two_stage_decomposition: bool = True,
        adaptive_task_cap: bool = True,
        adaptive_min_mip_tasks: int = 48,
        adaptive_max_mip_tasks: int = 200,
        logger: logging.Logger | None = None,
    ):
        self.simulation = simulation
        self.factory = simulation.factory
        self.command_handler = simulation.command_handler
        self.variant = str(variant)
        self.fallback_mode = str(fallback_mode)
        self.step_sec = float(step_sec)
        self.realtime_sleep_sec = float(realtime_sleep_sec)
        self.min_replan_interval_sec = max(0.0, float(min_replan_interval_sec))
        self.solver_time_limit_sec = float(solver_time_limit_sec)
        self.logger = logger or logging.getLogger(__name__)

        self.adaptive_task_cap = bool(adaptive_task_cap)
        self.adaptive_min_mip_tasks = max(12, int(adaptive_min_mip_tasks))
        self.adaptive_max_mip_tasks = max(self.adaptive_min_mip_tasks, int(adaptive_max_mip_tasks))
        self.base_max_raw_products = int(max_raw_products)
        self.base_max_future_tasks = int(max_future_tasks)
        self.base_future_horizon_sec = float(future_horizon_sec)

        self.reserved_products = set()

        self.monitor = ReplanEventMonitor(self.factory)
        self.extractor = SimulationStateExtractor(
            self.factory,
            max_raw_products=max_raw_products,
            max_future_tasks=max_future_tasks,
            future_horizon_sec=future_horizon_sec,
        )
        self.scheduler = MIPTaskScheduler(
            solver_time_limit_sec=solver_time_limit_sec,
            max_mip_tasks=max_mip_tasks,
            accept_partial_solution=accept_partial_mip_solution,
            use_two_stage_decomposition=use_two_stage_decomposition,
            fallback_mode=fallback_mode,
        )
        self.dispatcher = SequentialAGVDispatcher(
            factory=self.factory,
            command_handler=self.command_handler,
            reserved_products=self.reserved_products,
            logger=self.logger,
        )

        self.replan_count = 0
        self.last_plan_status = "none"
        self.last_replan_time = float("-inf")
        self.deferred_event_deltas: Dict[str, int] = {}
        self.plan_status_counts: Counter[str] = Counter()
        self.fallback_reason_counts: Counter[str] = Counter()
        self.solver_outcome_detail_counts: Counter[str] = Counter()
        self.solver_status_detail_counts: Counter[str] = Counter()
        self.empty_replans = 0
        self.heuristic_fallback_replans = 0

    def _scale_cap(self, base_cap: int, new_mip_cap: int) -> int:
        if base_cap <= 0:
            return base_cap
        ratio = new_mip_cap / max(self.adaptive_max_mip_tasks, 1)
        scaled = max(4, int(round(base_cap * ratio * 1.25)))
        return min(base_cap, scaled)

    def _scale_horizon(self, new_mip_cap: int) -> float:
        if self.base_future_horizon_sec <= 0:
            return self.base_future_horizon_sec
        ratio = new_mip_cap / max(self.adaptive_max_mip_tasks, 1)
        scaled = self.base_future_horizon_sec * (0.8 + 0.4 * ratio)
        return max(30.0, min(self.base_future_horizon_sec, scaled))

    def _adapt_task_caps(self, solve_elapsed_sec: float, pool_size: int, queued: int, status: str, now: float) -> None:
        if not self.adaptive_task_cap:
            return

        current_cap = int(self.scheduler.max_mip_tasks)
        if current_cap <= 0:
            return

        new_cap = current_cap
        slow = solve_elapsed_sec >= 0.95 * self.solver_time_limit_sec
        degraded = status in {"Not Solved", "Undefined", "Integer Feasible", "fallback_heuristic"}
        fast = solve_elapsed_sec <= 0.45 * self.solver_time_limit_sec and status == "Optimal"
        light_queue = queued <= max(16, int(0.25 * current_cap)) and pool_size <= max(40, int(0.6 * current_cap))

        if slow or degraded:
            new_cap = max(self.adaptive_min_mip_tasks, int(current_cap * 0.85))
        elif fast and light_queue:
            new_cap = min(self.adaptive_max_mip_tasks, int(current_cap * 1.10) + 1)

        if new_cap == current_cap:
            return

        new_raw_cap = self._scale_cap(self.base_max_raw_products, new_cap)
        new_future_cap = self._scale_cap(self.base_max_future_tasks, new_cap)
        new_future_horizon = self._scale_horizon(new_cap)

        self.scheduler.set_scale_controls(max_mip_tasks=new_cap)
        self.extractor.set_task_caps(
            max_raw_products=new_raw_cap,
            max_future_tasks=new_future_cap,
            future_horizon_sec=new_future_horizon,
        )

        self.logger.info(
            "[t=%.2f] Adaptive cap update: mip_tasks %d -> %d, raw_cap=%d, future_cap=%d, future_horizon=%.1f",
            now,
            current_cap,
            new_cap,
            new_raw_cap,
            new_future_cap,
            new_future_horizon,
        )

    def initial_plan(self) -> None:
        self._replan(reason="startup")

    def _replan(self, reason: str, events: List[TriggerEvent] | None = None) -> None:
        now = float(self.factory.env.now)

        event_desc = reason
        if events:
            event_desc = ",".join(f"{e.event_type}+{e.delta}" for e in events)

        self.logger.info("[t=%.2f] Replan start, reason=%s", now, event_desc)

        # Freeze semantics: no env.run while solving.
        agvs = self.extractor.extract_agvs(now)
        tasks = self.extractor.extract_tasks(now, self.reserved_products)

        category_counter = Counter(t.category for t in tasks)
        self.logger.info(
            "[t=%.2f] Task pool size=%d, categories=%s",
            now,
            len(tasks),
            dict(category_counter),
        )

        solve_start = time.perf_counter()
        plan = self.scheduler.solve(tasks=tasks, agvs=agvs, now=now)
        solve_elapsed = time.perf_counter() - solve_start
        self.dispatcher.load_plan(plan.assignments_by_agv, replace_pending=True)

        self.replan_count += 1
        self.last_plan_status = plan.status
        self.plan_status_counts[plan.status] += 1
        if plan.status == "empty":
            self.empty_replans += 1
        if plan.status == "fallback_heuristic":
            self.heuristic_fallback_replans += 1

        diagnostics = plan.diagnostics or {}
        for reason, count in diagnostics.get("fallback_reason_counts", {}).items():
            self.fallback_reason_counts[str(reason)] += int(count)
        for detail, count in diagnostics.get("status_detail_counts", {}).items():
            self.solver_outcome_detail_counts[str(detail)] += int(count)
        solver_status = diagnostics.get("solver_status")
        if solver_status is not None:
            self.solver_status_detail_counts[str(solver_status)] += 1

        queue_sizes = self.dispatcher.queue_sizes()
        total_queued = sum(queue_sizes.values())

        self.logger.info(
            "[t=%.2f] Replan done, status=%s, objective=%.2f, selected_tasks=%d, queued=%d, solve_sec=%.3f, mip_cap=%d",
            now,
            plan.status,
            plan.objective_value,
            len(plan.selected_task_ids),
            total_queued,
            solve_elapsed,
            int(self.scheduler.max_mip_tasks),
        )
        if plan.status == "fallback_heuristic":
            self.logger.warning(
                "[t=%.2f] Explicit diagnostic heuristic fallback: %s", now, plan.message
            )
        self._adapt_task_caps(
            solve_elapsed_sec=solve_elapsed,
            pool_size=len(tasks),
            queued=total_queued,
            status=plan.status,
            now=now,
        )
        self.last_replan_time = now

    def _can_replan(self, now: float) -> bool:
        return (now - self.last_replan_time) >= self.min_replan_interval_sec - 1e-9

    def _defer_events(self, events: List[TriggerEvent]) -> None:
        for event in events:
            self.deferred_event_deltas[event.event_type] = self.deferred_event_deltas.get(event.event_type, 0) + int(event.delta)

    def _consume_deferred_events(self, now: float) -> List[TriggerEvent]:
        events = [
            TriggerEvent(event_type=event_type, delta=delta, timestamp=now)
            for event_type, delta in sorted(self.deferred_event_deltas.items())
            if delta > 0
        ]
        self.deferred_event_deltas.clear()
        return events

    def run(self, until: float) -> None:
        self.initial_plan()

        while float(self.factory.env.now) < float(until):
            self.dispatcher.tick()

            now = float(self.factory.env.now)
            if self.deferred_event_deltas and self._can_replan(now):
                deferred_events = self._consume_deferred_events(now)
                if deferred_events:
                    self._replan(reason="deferred_event", events=deferred_events)

            events = self.monitor.poll()
            if events:
                now = float(self.factory.env.now)
                if self._can_replan(now):
                    self._replan(reason="event", events=events)
                else:
                    self._defer_events(events)
                    cooldown_left = max(0.0, self.min_replan_interval_sec - (now - self.last_replan_time))
                    self.logger.info(
                        "[t=%.2f] Replan deferred due to cooldown %.2fs, deferred_events=%s",
                        now,
                        cooldown_left,
                        self.deferred_event_deltas,
                    )

            now = float(self.factory.env.now)
            next_t = min(float(until), now + self.step_sec)
            if next_t <= now:
                break

            self.factory.run(until=next_t)

            if self.realtime_sleep_sec > 0.0:
                time.sleep(self.realtime_sleep_sec)

        self.logger.info(
            "Rescheduling engine finished at t=%.2f, replans=%d, last_plan_status=%s",
            float(self.factory.env.now),
            self.replan_count,
            self.last_plan_status,
        )

    def collect_metrics(self) -> Dict[str, Any]:
        """Collect comprehensive simulation metrics at stop time."""
        result: Dict[str, Any] = {
            "engine": {
                "simulation_time": float(self.factory.env.now),
                "controller": "rolling_milp",
                "variant": self.variant,
                "gurobi_required": True,
                "solver_time_limit_seconds": self.solver_time_limit_sec,
                "min_replan_interval_minutes": self.min_replan_interval_sec,
                "fallback_mode": self.fallback_mode,
                "replans": int(self.replan_count),
                "last_plan_status": self.last_plan_status,
                "empty_replans": int(self.empty_replans),
                "plan_status_counts": dict(self.plan_status_counts),
                "fallback_reason_counts": dict(self.fallback_reason_counts),
                "solver_outcome_detail_counts": dict(self.solver_outcome_detail_counts),
                "solver_status_detail_counts": dict(self.solver_status_detail_counts),
                "heuristic_fallback_replans": int(self.heuristic_fallback_replans),
                "all_replans_milp": self.heuristic_fallback_replans == 0,
            }
        }

        kpi = getattr(self.factory, "kpi_calculator", None)
        if kpi is None:
            result["kpi"] = None
            return result

        current_kpis = kpi.calculate_current_kpis()
        final_score = kpi.get_final_score()
        active_orders = {
            oid: {
                "created_at": float(order.created_at),
                "deadline": float(order.deadline),
                "items_total": int(order.items_total),
                "items_completed": int(order.items_completed),
            }
            for oid, order in kpi.active_orders.items()
        }

        result["kpi"] = {
            "current": (
                current_kpis.model_dump()
                if hasattr(current_kpis, "model_dump")
                else dict(current_kpis)
            ),
            "final_score": final_score,
            "stats": {
                "total_orders": int(kpi.stats.total_orders),
                "completed_orders": int(kpi.stats.completed_orders),
                "on_time_orders": int(kpi.stats.on_time_orders),
                "total_products": int(kpi.stats.total_products),
                "quality_passed_products": int(kpi.stats.quality_passed_products),
                "scrapped_products": int(kpi.stats.scrapped_products),
                "material_costs": float(kpi.stats.material_costs),
                "energy_costs": float(kpi.stats.energy_costs),
                "maintenance_costs": float(kpi.stats.maintenance_costs),
                "scrap_costs": float(kpi.stats.scrap_costs),
                "agv_completed_tasks": int(kpi.stats.agv_completed_tasks),
                "agv_active_charges": int(kpi.stats.agv_active_charges),
                "agv_passive_charges": int(kpi.stats.agv_passive_charges),
            },
            "active_orders": active_orders,
            "completed_orders": [
                {
                    "order_id": order.order_id,
                    "created_at": float(order.created_at),
                    "completed_at": float(order.completed_at) if order.completed_at is not None else None,
                    "deadline": float(order.deadline),
                    "items_total": int(order.items_total),
                    "items_completed": int(order.items_completed),
                    "is_on_time": bool(order.is_on_time) if order.is_on_time is not None else None,
                }
                for order in kpi.completed_orders
            ],
        }

        return result
