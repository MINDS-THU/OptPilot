from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .path_timing import get_travel_time
from .types import AGVSnapshot, TransportTask


class SimulationStateExtractor:
    """
    Extracts dispatchable state from simulation objects without modifying core code.
    """

    def __init__(
        self,
        factory,
        max_raw_products: int = 24,
        max_future_tasks: int = 24,
        future_horizon_sec: float = 120.0,
    ):
        self.factory = factory
        self.max_raw_products = int(max_raw_products)
        self.max_future_tasks = int(max_future_tasks)
        self.future_horizon_sec = float(future_horizon_sec)

    def set_task_caps(
        self,
        max_raw_products: int | None = None,
        max_future_tasks: int | None = None,
        future_horizon_sec: float | None = None,
    ) -> None:
        if max_raw_products is not None:
            self.max_raw_products = int(max_raw_products)
        if max_future_tasks is not None:
            self.max_future_tasks = int(max_future_tasks)
        if future_horizon_sec is not None:
            self.future_horizon_sec = float(future_horizon_sec)

    def extract_agvs(self, now: float) -> Dict[str, AGVSnapshot]:
        agvs: Dict[str, AGVSnapshot] = {}
        for line_id, line in self.factory.lines.items():
            for agv_id, agv in line.agvs.items():
                status = getattr(getattr(agv, "status", None), "value", str(getattr(agv, "status", "unknown")))
                projected_start_point = getattr(agv, "target_point", None) or agv.current_point

                if status == "idle":
                    projected_available_time = now
                elif status == "moving":
                    projected_available_time = now + max(float(getattr(agv, "estimated_time", 0.0) or 0.0), 0.5)
                else:
                    projected_available_time = now + 1.0

                agv_key = f"{line_id}:{agv_id}"
                agvs[agv_key] = AGVSnapshot(
                    agv_key=agv_key,
                    line_id=line_id,
                    agv_id=agv_id,
                    current_point=agv.current_point,
                    projected_start_point=projected_start_point,
                    projected_available_time=projected_available_time,
                    operation_time=float(getattr(agv, "operation_time", 1.0) or 1.0),
                    status=status,
                )
        return agvs

    def extract_tasks(self, now: float, reserved_products: Set[str]) -> List[TransportTask]:
        tasks: List[TransportTask] = []

        raw_tasks = self._extract_raw_tasks(now, reserved_products)
        qc_tasks = self._extract_quality_output_tasks(now, reserved_products)
        cq_tasks = self._extract_cq_side_buffer_tasks(now, reserved_products)

        tasks.extend(raw_tasks)
        tasks.extend(qc_tasks)
        tasks.extend(cq_tasks)

        # Add deterministic look-ahead tasks for products already in production lines.
        # We keep one task per product in the pool to avoid duplicate-reservation conflicts.
        materialized_product_ids = {
            t.product_id for t in (raw_tasks + qc_tasks + cq_tasks)
        }
        tasks.extend(
            self._extract_future_deterministic_wip_tasks(
                now=now,
                reserved_products=reserved_products,
                excluded_product_ids=materialized_product_ids,
            )
        )

        return tasks

    def _extract_future_deterministic_wip_tasks(
        self,
        now: float,
        reserved_products: Set[str],
        excluded_product_ids: Set[str],
    ) -> List[TransportTask]:
        tasks: List[TransportTask] = []

        for line_id, line in self.factory.lines.items():
            seen_ids: Set[str] = set()
            for product in self._iter_line_products(line_id):
                if product.id in seen_ids:
                    continue
                seen_ids.add(product.id)

                if product.id in reserved_products:
                    continue
                if product.id in excluded_product_ids:
                    continue
                if product.current_location in {"RawMaterial", "Warehouse"}:
                    # RawMaterial tasks are handled separately; Warehouse is finished flow.
                    continue

                eta_to_qc_output = self._estimate_eta_to_qc_output(line_id, product, now)
                release = now + max(0.0, eta_to_qc_output)

                if self.future_horizon_sec > 0.0 and release > now + self.future_horizon_sec:
                    continue

                task_id = f"future_qc::{line_id}::{product.id}::to_warehouse"
                tasks.append(
                    TransportTask(
                        task_id=task_id,
                        product_id=product.id,
                        product_type=product.product_type,
                        category="future_qc_pass_to_warehouse",
                        line_id=line_id,
                        source_point="P8",
                        destination_point="P9",
                        source_device="QualityCheck",
                        destination_device="Warehouse",
                        source_buffer="output_buffer",
                        release_time=release,
                        selection_group=task_id,
                        metadata={
                            "eligible_agv_keys": self._line_agv_keys(line_id),
                            "predicted": True,
                            "current_location": product.current_location,
                        },
                    )
                )

        tasks.sort(key=lambda t: (t.release_time, t.task_id))
        if self.max_future_tasks > 0:
            tasks = tasks[: self.max_future_tasks]

        return tasks

    def _iter_line_products(self, line_id: str):
        line = self.factory.lines[line_id]

        for station in line.stations.values():
            for p in list(station.buffer.items):
                yield p
            if hasattr(station, "output_buffer"):
                for p in list(station.output_buffer.items):
                    yield p

        for conveyor in line.conveyors.values():
            if hasattr(conveyor, "buffer"):
                for p in list(conveyor.buffer.items):
                    yield p
            if hasattr(conveyor, "main_buffer"):
                for p in list(conveyor.main_buffer.items):
                    yield p
            if hasattr(conveyor, "upper_buffer"):
                for p in list(conveyor.upper_buffer.items):
                    yield p
            if hasattr(conveyor, "lower_buffer"):
                for p in list(conveyor.lower_buffer.items):
                    yield p

    @staticmethod
    def _avg_processing_time(station, product_type: str) -> float:
        if station is None:
            return 5.0
        t_min, t_max = station.processing_times.get(product_type, (5, 5))
        return (float(t_min) + float(t_max)) / 2.0

    @staticmethod
    def _find_product_index(items: list, product_id: str) -> int:
        for idx, p in enumerate(items):
            if p.id == product_id:
                return idx
        return -1

    def _station_item_remaining(self, station, item, now: float) -> float:
        avg = self._avg_processing_time(station, item.product_type)
        if station is None:
            return avg

        if getattr(station, "current_product_id", None) == item.id:
            total = float(getattr(station, "current_product_total_time", None) or avg)
            elapsed = float(getattr(station, "current_product_elapsed_time", None) or 0.0)
            started_at = getattr(station, "current_product_start_time", None)
            if started_at is not None:
                elapsed += max(0.0, now - float(started_at))
            return max(0.0, total - elapsed)

        return avg

    def _station_block_penalty(self, station, product_type: str) -> float:
        if station is None:
            return 0.0

        downstream = getattr(station, "downstream_conveyor", None)
        if downstream is None:
            return 0.0

        penalty = 0.0
        transfer_time = float(getattr(downstream, "transfer_time", 5.0) or 5.0)

        try:
            if not downstream.can_operate():
                penalty += transfer_time
        except Exception:
            pass

        try:
            if downstream.is_full():
                penalty += transfer_time
        except Exception:
            pass

        ds_station = getattr(downstream, "downstream_station", None)
        if ds_station is not None:
            ds_proc = self._avg_processing_time(ds_station, product_type)
            try:
                if not ds_station.can_operate():
                    penalty += 0.5 * ds_proc
            except Exception:
                pass
            try:
                if ds_station.is_full():
                    penalty += 0.5 * ds_proc
            except Exception:
                pass

        return penalty

    def _station_current_eta(self, station, product, now: float) -> float:
        if station is None:
            return 5.0

        items = list(station.buffer.items)
        idx = self._find_product_index(items, product.id)
        if idx < 0:
            return self._station_future_eta(station, product.product_type, now)

        queue_ahead = sum(self._station_item_remaining(station, p, now) for p in items[:idx])
        own_remaining = self._station_item_remaining(station, items[idx], now)
        block_penalty = self._station_block_penalty(station, product.product_type)
        return queue_ahead + own_remaining + block_penalty

    def _station_future_eta(self, station, product_type: str, now: float) -> float:
        if station is None:
            return 5.0

        own_processing = self._avg_processing_time(station, product_type)
        items = list(station.buffer.items)
        if not items:
            return own_processing + self._station_block_penalty(station, product_type)

        queue_work = 0.0
        for i, item in enumerate(items):
            rem = self._station_item_remaining(station, item, now)
            queue_work += rem if i == 0 else 0.8 * rem

        block_penalty = self._station_block_penalty(station, product_type)
        return own_processing + 0.35 * queue_work + block_penalty

    def _conveyor_item_remaining(self, conveyor, product_id: str, now: float) -> float:
        transfer_time = float(getattr(conveyor, "transfer_time", 5.0) or 5.0)
        elapsed = float(getattr(conveyor, "product_elapsed_times", {}).get(product_id, 0.0))
        start_map = getattr(conveyor, "product_start_times", {})
        if product_id in start_map:
            elapsed += max(0.0, now - float(start_map[product_id]))

        if elapsed <= 0.0 and product_id not in start_map:
            return transfer_time
        return max(0.0, transfer_time - elapsed)

    def _conveyor_block_penalty(self, conveyor, product_type: str) -> float:
        if conveyor is None:
            return 0.0

        ds_station = getattr(conveyor, "downstream_station", None)
        if ds_station is None:
            return 0.0

        penalty = 0.0
        ds_proc = self._avg_processing_time(ds_station, product_type)
        try:
            if not ds_station.can_operate():
                penalty += ds_proc
        except Exception:
            pass
        try:
            if ds_station.is_full():
                penalty += ds_proc
        except Exception:
            pass
        return penalty

    def _conveyor_current_eta(self, conveyor, buffer_items: list, product, now: float) -> float:
        if conveyor is None:
            return 5.0

        idx = self._find_product_index(buffer_items, product.id)
        if idx < 0:
            return self._conveyor_future_eta(conveyor, buffer_items, product.product_type, now)

        queue = sum(self._conveyor_item_remaining(conveyor, p.id, now) for p in buffer_items[: idx + 1])
        block_penalty = self._conveyor_block_penalty(conveyor, product.product_type)
        return queue + block_penalty

    def _conveyor_future_eta(self, conveyor, buffer_items: list, product_type: str, now: float) -> float:
        if conveyor is None:
            return 5.0

        transfer_time = float(getattr(conveyor, "transfer_time", 5.0) or 5.0)
        if not buffer_items:
            return transfer_time + self._conveyor_block_penalty(conveyor, product_type)

        queue_work = sum(0.8 * self._conveyor_item_remaining(conveyor, p.id, now) for p in buffer_items)
        block_penalty = self._conveyor_block_penalty(conveyor, product_type)
        return transfer_time + 0.35 * queue_work + block_penalty

    def _estimate_eta_to_qc_output(self, line_id: str, product, now: float) -> float:
        """
        Deterministic ETA proxy from current WIP location to QC output availability.
        Refined ETA uses:
        - whether product is currently processing,
        - processed ratio (elapsed/total),
        - queue ahead,
        - queue/block penalties.
        It still ignores stochastic quality outcomes and random faults.
        """
        line = self.factory.lines[line_id]
        station_a = line.stations.get("StationA")
        station_b = line.stations.get("StationB")
        station_c = line.stations.get("StationC")
        qc = line.stations.get("QualityCheck")
        conv_ab = line.conveyors.get("Conveyor_AB")
        conv_bc = line.conveyors.get("Conveyor_BC")
        conv_cq = line.conveyors.get("Conveyor_CQ")

        ptype = product.product_type
        loc = product.current_location

        buffer_ab = list(conv_ab.buffer.items) if conv_ab is not None and hasattr(conv_ab, "buffer") else []
        buffer_bc = list(conv_bc.buffer.items) if conv_bc is not None and hasattr(conv_bc, "buffer") else []
        buffer_cq_main = list(conv_cq.main_buffer.items) if conv_cq is not None and hasattr(conv_cq, "main_buffer") else []

        t_a = self._station_future_eta(station_a, ptype, now)
        t_b = self._station_future_eta(station_b, ptype, now)
        t_c = self._station_future_eta(station_c, ptype, now)
        t_q = self._station_future_eta(qc, ptype, now)
        t_ab = self._conveyor_future_eta(conv_ab, buffer_ab, ptype, now)
        t_bc = self._conveyor_future_eta(conv_bc, buffer_bc, ptype, now)
        t_cq = self._conveyor_future_eta(conv_cq, buffer_cq_main, ptype, now)

        line_agvs = list(line.agvs.values())
        avg_op = sum(float(getattr(a, "operation_time", 1.0) or 1.0) for a in line_agvs) / max(len(line_agvs), 1)
        base_cq_to_b_agv = self.safe_travel_time("P6", "P3") + 2.0 * avg_op
        cq_upper_len = len(conv_cq.upper_buffer.items) if conv_cq is not None and hasattr(conv_cq, "upper_buffer") else 0
        cq_lower_len = len(conv_cq.lower_buffer.items) if conv_cq is not None and hasattr(conv_cq, "lower_buffer") else 0
        side_backlog = cq_upper_len + cq_lower_len
        available_agv = sum(
            1
            for agv in line_agvs
            if getattr(getattr(agv, "status", None), "value", str(getattr(agv, "status", "unknown"))) == "idle"
        )
        agv_parallel = max(1, available_agv)
        side_queue_penalty = 0.4 * (side_backlog / agv_parallel) * base_cq_to_b_agv
        t_cq_to_b_agv = base_cq_to_b_agv + side_queue_penalty

        stationc_visits = int(getattr(product, "visit_count", {}).get("StationC", 0))

        # P1/P2 deterministic chain to QC output.
        if ptype in {"P1", "P2"}:
            if loc.startswith("StationA"):
                cur = self._station_current_eta(station_a, product, now)
                return cur + t_ab + t_b + t_bc + t_c + t_cq + t_q
            if loc.startswith("Conveyor_AB"):
                cur = self._conveyor_current_eta(conv_ab, buffer_ab, product, now)
                return cur + t_b + t_bc + t_c + t_cq + t_q
            if loc.startswith("StationB"):
                cur = self._station_current_eta(station_b, product, now)
                return cur + t_bc + t_c + t_cq + t_q
            if loc.startswith("Conveyor_BC"):
                cur = self._conveyor_current_eta(conv_bc, buffer_bc, product, now)
                return cur + t_c + t_cq + t_q
            if loc.startswith("StationC"):
                cur = self._station_current_eta(station_c, product, now)
                return cur + t_cq + t_q
            if loc.startswith("Conveyor_CQ"):
                cur = self._conveyor_current_eta(conv_cq, buffer_cq_main, product, now)
                return cur + t_q
            if loc.startswith("QualityCheck"):
                cur = self._station_current_eta(qc, product, now)
                return cur
            return t_q

        # P3 has the StationC->StationB->StationC loop before QC.
        if loc.startswith("StationA"):
            # A->B->C(first)->CQ->AGV back to B->C(second)->CQ->Q
            cur = self._station_current_eta(station_a, product, now)
            return cur + t_ab + t_b + t_bc + t_c + t_cq + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("Conveyor_AB"):
            cur = self._conveyor_current_eta(conv_ab, buffer_ab, product, now)
            return cur + t_b + t_bc + t_c + t_cq + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("StationB"):
            # If StationC already visited once, this is usually the second B.
            cur = self._station_current_eta(station_b, product, now)
            if stationc_visits >= 1:
                return cur + t_bc + t_c + t_cq + t_q
            return cur + t_bc + t_c + t_cq + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("Conveyor_BC"):
            cur = self._conveyor_current_eta(conv_bc, buffer_bc, product, now)
            if stationc_visits >= 1:
                return cur + t_c + t_cq + t_q
            return cur + t_c + t_cq + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("StationC"):
            cur = self._station_current_eta(station_c, product, now)
            if stationc_visits >= 1:
                return cur + t_cq + t_q
            return cur + t_cq + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("Conveyor_CQ"):
            # Side buffers or first-pass main conveyor are both approximated here.
            cur = self._conveyor_current_eta(conv_cq, buffer_cq_main, product, now)
            if stationc_visits >= 1:
                return cur + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
            return cur + t_cq_to_b_agv + t_b + t_bc + t_c + t_cq + t_q
        if loc.startswith("QualityCheck"):
            cur = self._station_current_eta(qc, product, now)
            return cur

        return t_q

    def _line_agv_keys(self, line_id: str) -> Set[str]:
        line = self.factory.lines[line_id]
        return {f"{line_id}:{agv_id}" for agv_id in line.agvs.keys()}

    def _extract_raw_tasks(self, now: float, reserved_products: Set[str]) -> List[TransportTask]:
        raw_tasks: List[TransportTask] = []
        raw_buffer = list(self.factory.raw_material.buffer.items)

        if self.max_raw_products > 0:
            raw_buffer = raw_buffer[: self.max_raw_products]

        for product in raw_buffer:
            if product.id in reserved_products:
                continue

            selection_group = f"raw::{product.id}"
            for line_id in self.factory.lines.keys():
                task_id = f"raw::{product.id}::{line_id}"
                raw_tasks.append(
                    TransportTask(
                        task_id=task_id,
                        product_id=product.id,
                        product_type=product.product_type,
                        category="raw_to_station_a",
                        line_id=line_id,
                        source_point="P0",
                        destination_point="P1",
                        source_device="RawMaterial",
                        destination_device="StationA",
                        source_buffer=None,
                        release_time=now,
                        selection_group=selection_group,
                        metadata={
                            "eligible_agv_keys": self._line_agv_keys(line_id),
                        },
                    )
                )

        return raw_tasks

    def _extract_quality_output_tasks(self, now: float, reserved_products: Set[str]) -> List[TransportTask]:
        tasks: List[TransportTask] = []
        for line_id, line in self.factory.lines.items():
            qc = line.stations.get("QualityCheck")
            if not qc:
                continue

            for product in list(qc.output_buffer.items):
                if product.id in reserved_products:
                    continue

                q_status = getattr(getattr(product, "quality_status", None), "value", "unknown")
                if q_status == "rework":
                    category = "qc_rework_to_station_c"
                    destination_point = "P5"
                    destination_device = "StationC"
                else:
                    category = "qc_pass_to_warehouse"
                    destination_point = "P9"
                    destination_device = "Warehouse"

                task_id = f"qc::{line_id}::{product.id}::{category}"
                tasks.append(
                    TransportTask(
                        task_id=task_id,
                        product_id=product.id,
                        product_type=product.product_type,
                        category=category,
                        line_id=line_id,
                        source_point="P8",
                        destination_point=destination_point,
                        source_device="QualityCheck",
                        destination_device=destination_device,
                        source_buffer="output_buffer",
                        release_time=now,
                        selection_group=task_id,
                        metadata={
                            "eligible_agv_keys": self._line_agv_keys(line_id),
                        },
                    )
                )

        return tasks

    def _extract_cq_side_buffer_tasks(self, now: float, reserved_products: Set[str]) -> List[TransportTask]:
        tasks: List[TransportTask] = []

        for line_id, line in self.factory.lines.items():
            cq = line.conveyors.get("Conveyor_CQ")
            if not cq:
                continue

            for buffer_name in ("upper", "lower"):
                buffer_store = cq.get_buffer(buffer_name)
                for product in list(buffer_store.items):
                    if product.id in reserved_products:
                        continue

                    task_id = f"cq::{line_id}::{buffer_name}::{product.id}"
                    eligible_keys = self._line_agv_keys(line_id)

                    # Match simulator's fixed mapping: AGV_1->lower, AGV_2->upper
                    if buffer_name == "lower":
                        eligible_keys = {k for k in eligible_keys if k.endswith(":AGV_1")}
                    else:
                        eligible_keys = {k for k in eligible_keys if k.endswith(":AGV_2")}

                    if not eligible_keys:
                        continue

                    tasks.append(
                        TransportTask(
                            task_id=task_id,
                            product_id=product.id,
                            product_type=product.product_type,
                            category="cq_loop_to_station_b",
                            line_id=line_id,
                            source_point="P6",
                            destination_point="P3",
                            source_device="Conveyor_CQ",
                            destination_device="StationB",
                            source_buffer=buffer_name,
                            release_time=now,
                            selection_group=task_id,
                            metadata={
                                "eligible_agv_keys": eligible_keys,
                            },
                        )
                    )

        return tasks

    @staticmethod
    def safe_travel_time(from_point: str, to_point: str) -> float:
        if from_point == to_point:
            return 0.0
        travel = float(get_travel_time(from_point, to_point))
        if travel < 0:
            return 1e6
        return travel
