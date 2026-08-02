from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .compat import AgentCommand
from .types import ScheduledTask


@dataclass
class _MacroExecution:
    scheduled_task: ScheduledTask
    steps: List[Tuple[str, Dict]]
    next_step_index: int = 0


@dataclass
class _InFlightCommand:
    command_id: str
    action: str
    params: Dict


class SequentialAGVDispatcher:
    """
    Publishes AGV commands strictly one-by-one per AGV.
    A next command is sent only after the previous command has a response.
    """

    SUCCESS_KEYWORDS = (
        "success",
        "picked up",
        "loaded onto",
        "unloaded",
        "arrived",
        "already at",
        "charged",
        "battery level is enough",
    )

    ERROR_KEYWORDS = (
        "failed",
        "not found",
        "unknown",
        "can not",
        "cannot",
        "unable",
        "empty",
        "missing",
        "deadlock",
        "error",
        "exception",
        "violat",
        "too low",
        "unable",
        "\u65e0\u6cd5",
        "\u5f02\u5e38",
        "\u9519\u8bef",
        "\u5931\u8d25",
    )

    def __init__(self, factory, command_handler, reserved_products: Set[str], logger):
        self.factory = factory
        self.command_handler = command_handler
        self.reserved_products = reserved_products
        self.logger = logger

        self.pending_by_agv: Dict[str, List[ScheduledTask]] = {}
        self.active_macro_by_agv: Dict[str, Optional[_MacroExecution]] = {}
        self.inflight_by_agv: Dict[str, Optional[_InFlightCommand]] = {}
        self._last_payload_block_log_time: Dict[str, float] = {}
        self._command_counter = 0

        for line_id, line in self.factory.lines.items():
            for agv_id in line.agvs.keys():
                agv_key = f"{line_id}:{agv_id}"
                self.pending_by_agv[agv_key] = []
                self.active_macro_by_agv[agv_key] = None
                self.inflight_by_agv[agv_key] = None

    def load_plan(self, assignments_by_agv: Dict[str, List[ScheduledTask]], replace_pending: bool = True) -> None:
        if replace_pending:
            for agv_key, queue in self.pending_by_agv.items():
                for scheduled in queue:
                    self.reserved_products.discard(scheduled.task.product_id)
                self.pending_by_agv[agv_key] = []

        active_products = {
            macro.scheduled_task.task.product_id
            for macro in self.active_macro_by_agv.values()
            if macro is not None
        }

        for agv_key, new_list in assignments_by_agv.items():
            if agv_key not in self.pending_by_agv:
                continue
            for scheduled in new_list:
                pid = scheduled.task.product_id
                if pid in active_products:
                    continue
                if pid in self.reserved_products:
                    continue
                self.pending_by_agv[agv_key].append(scheduled)
                self.reserved_products.add(pid)

            self.pending_by_agv[agv_key].sort(key=lambda st: (st.planned_start, st.task.task_id))

    def tick(self) -> None:
        for agv_key in list(self.pending_by_agv.keys()):
            self._consume_response_if_ready(agv_key)

            if self.inflight_by_agv[agv_key] is not None:
                continue

            macro = self.active_macro_by_agv[agv_key]
            if macro is not None:
                self._send_next_step_if_possible(agv_key)
                continue

            self._start_next_macro_if_possible(agv_key)

    def _consume_response_if_ready(self, agv_key: str) -> None:
        inflight = self.inflight_by_agv[agv_key]
        if inflight is None:
            return

        command_id = inflight.command_id
        if command_id not in self.command_handler.commands:
            return

        response = self.command_handler.commands.pop(command_id)
        self.inflight_by_agv[agv_key] = None

        macro = self.active_macro_by_agv[agv_key]
        if macro is None:
            return

        if self._is_error_response(response):
            if self._is_retryable_response(response):
                task = macro.scheduled_task.task
                self.logger.info(
                    "AGV %s transient response (%s): %s; retrying task=%s product=%s",
                    agv_key,
                    inflight.action,
                    response,
                    task.task_id,
                    task.product_id,
                )
                return

            task = macro.scheduled_task.task
            self.logger.warning(
                "AGV %s command failed (%s): %s; task=%s product=%s",
                agv_key,
                inflight.action,
                response,
                task.task_id,
                task.product_id,
            )
            self.reserved_products.discard(task.product_id)
            self.active_macro_by_agv[agv_key] = None
            return

        macro.next_step_index += 1
        if macro.next_step_index >= len(macro.steps):
            task = macro.scheduled_task.task
            self.logger.info(
                "AGV %s completed task=%s product=%s",
                agv_key,
                task.task_id,
                task.product_id,
            )
            self.reserved_products.discard(task.product_id)
            self.active_macro_by_agv[agv_key] = None

    def _start_next_macro_if_possible(self, agv_key: str) -> None:
        if not self._agv_can_accept_command(agv_key):
            return

        payload_count = self._agv_payload_count(agv_key)
        if payload_count > 0:
            now = float(self.factory.env.now)
            last_log = self._last_payload_block_log_time.get(agv_key, float("-inf"))
            if now - last_log >= 10.0:
                self.logger.warning(
                    "AGV %s has residual payload=%d while idle; skip new macro dispatch until payload is cleared",
                    agv_key,
                    payload_count,
                )
                self._last_payload_block_log_time[agv_key] = now
            return

        queue = self.pending_by_agv[agv_key]
        if not queue:
            return

        now = float(self.factory.env.now)
        selected_index = None
        for idx, scheduled in enumerate(queue):
            if scheduled.planned_start > now + 1e-6:
                continue
            if self._task_ready(scheduled.task):
                selected_index = idx
                break

        # If no planned-start-ready task found, try any ready task to avoid dead waiting.
        if selected_index is None:
            for idx, scheduled in enumerate(queue):
                if self._task_ready(scheduled.task):
                    selected_index = idx
                    break

        if selected_index is None:
            return

        scheduled = queue.pop(selected_index)
        macro = _MacroExecution(
            scheduled_task=scheduled,
            steps=self._build_steps(scheduled.task),
            next_step_index=0,
        )
        self.active_macro_by_agv[agv_key] = macro
        self._send_next_step_if_possible(agv_key)

    def _send_next_step_if_possible(self, agv_key: str) -> None:
        macro = self.active_macro_by_agv[agv_key]
        if macro is None:
            return
        if self.inflight_by_agv[agv_key] is not None:
            return
        if not self._agv_can_accept_command(agv_key):
            return

        if macro.next_step_index >= len(macro.steps):
            return

        action, params = macro.steps[macro.next_step_index]
        line_id, agv_id = agv_key.split(":", 1)

        self._command_counter += 1
        safe_agv_key = agv_key.replace(":", "-")
        command_id = f"rolling-milp-{safe_agv_key}-{self._command_counter:08d}"
        command = AgentCommand(
            command_id=command_id,
            action=action,
            target=agv_id,
            params=params,
        )

        try:
            self.command_handler._execute_command(line_id, command)
        except Exception as exc:
            task = macro.scheduled_task.task
            self.logger.exception(
                "Failed to submit command for AGV %s, task=%s product=%s: %s",
                agv_key,
                task.task_id,
                task.product_id,
                exc,
            )
            self.reserved_products.discard(task.product_id)
            self.active_macro_by_agv[agv_key] = None
            self.inflight_by_agv[agv_key] = None
            return

        self.inflight_by_agv[agv_key] = _InFlightCommand(
            command_id=command_id,
            action=action,
            params=params,
        )

    def _agv_payload_count(self, agv_key: str) -> int:
        line_id, agv_id = agv_key.split(":", 1)
        agv = self.factory.lines[line_id].agvs[agv_id]
        payload = getattr(agv, "payload", None)
        if payload is None:
            return 0
        items = getattr(payload, "items", None)
        if items is None:
            return 0
        return len(items)

    def _agv_can_accept_command(self, agv_key: str) -> bool:
        line_id, agv_id = agv_key.split(":", 1)
        agv = self.factory.lines[line_id].agvs[agv_id]
        status = getattr(getattr(agv, "status", None), "value", str(getattr(agv, "status", "unknown")))
        if status != "idle":
            return False
        if getattr(agv, "action", None) is not None:
            return False
        return True

    def _task_ready(self, task) -> bool:
        pid = task.product_id

        if task.category == "raw_to_station_a":
            return any(p.id == pid for p in self.factory.raw_material.buffer.items)

        line = self.factory.lines.get(task.line_id)
        if line is None:
            return False

        if task.category in {"qc_pass_to_warehouse", "qc_rework_to_station_c"}:
            qc = line.stations.get("QualityCheck")
            if not qc:
                return False
            return any(p.id == pid for p in qc.output_buffer.items)

        if task.category == "future_qc_pass_to_warehouse":
            qc = line.stations.get("QualityCheck")
            if not qc:
                return False
            for p in qc.output_buffer.items:
                if p.id != pid:
                    continue
                q_status = getattr(getattr(p, "quality_status", None), "value", "unknown")
                return q_status == "pass"
            return False

        if task.category == "cq_loop_to_station_b":
            cq = line.conveyors.get("Conveyor_CQ")
            if not cq:
                return False
            buffer_name = task.source_buffer or "main"
            return any(p.id == pid for p in cq.get_buffer(buffer_name).items)

        return False

    @staticmethod
    def _build_steps(task) -> List[Tuple[str, Dict]]:
        return [
            ("move", {"target_point": task.source_point}),
            ("load", {"product_id": task.product_id}),
            ("move", {"target_point": task.destination_point}),
            ("unload", {"product_id": task.product_id}),
        ]

    def queue_sizes(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self.pending_by_agv.items()}

    def _is_error_response(self, response: str) -> bool:
        text = (response or "").lower()
        if any(key in text for key in self.SUCCESS_KEYWORDS):
            return False
        return any(key in text for key in self.ERROR_KEYWORDS)

    @staticmethod
    def _is_retryable_response(response: str) -> bool:
        text = (response or "").lower()
        retryable_tokens = (
            "emergency charging",
            "already charging",
            "battery level is too low",
            "low battery",
        )
        return any(tok in text for tok in retryable_tokens)
