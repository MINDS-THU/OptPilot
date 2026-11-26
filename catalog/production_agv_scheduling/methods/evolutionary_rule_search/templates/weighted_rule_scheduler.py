"""Executable 14-weight policy extracted from the paper's reference code.

NumPy was removed from the original normalization helper so candidates remain
dependency-free.  Scheduling behavior is otherwise kept aligned with the
source implementation.
"""

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

LINE_METHODS = ["default", "sq", "lwt", "met"]
TASK_METHODS = ["default", "spt", "lwkr", "lopnr", "edd", "cr", "ms", "fifo"]
AGV_METHODS = ["default", "nvf"]
NO_ASSIGN_KEY = "__no_assign__"


def normalize_weights(weights: Sequence[float], expected_len: int) -> List[float]:
    arr = [max(0.0, float(value)) for value in weights]
    if len(arr) != expected_len:
        raise ValueError(f"Expected {expected_len} weights, got {len(arr)}")
    total = sum(arr)
    if total <= 1e-12:
        return [1.0 / float(expected_len)] * expected_len
    return [value / total for value in arr]


def rank_normalize(value_by_key: Dict[Any, float], higher_is_better: bool = True) -> Dict[Any, float]:
    if not value_by_key:
        return {}

    def sort_key(item: Tuple[Any, float]) -> Tuple[float, str]:
        key, value = item
        value_key = -value if higher_is_better else value
        return (value_key, str(key))

    ranked_items = sorted(value_by_key.items(), key=sort_key)
    count = len(ranked_items)
    if count == 1:
        key, _ = ranked_items[0]
        return {key: 1.0}

    normalized: Dict[Any, float] = {}
    for rank, (key, _value) in enumerate(ranked_items):
        normalized[key] = 1.0 - (rank / float(count - 1))
    return normalized


class WeightedHeuristicScheduler:
    """
    Three-layer weighted heuristic scheduler.

    Layer 1: line selection weights over [default, sq, lwt, met]
    Layer 2: task priority weights over [default, spt, lwkr, lopnr, edd, cr, ms, fifo]
    Layer 3: AGV dispatch weights over [default, nvf]
    """

    def __init__(
        self,
        line_weights: Optional[Sequence[float]] = None,
        task_weights: Optional[Sequence[float]] = None,
        agv_weights: Optional[Sequence[float]] = None,
    ):
        self.line_weights = normalize_weights(line_weights or [1.0, 0.0, 0.0, 0.0], len(LINE_METHODS))
        self.task_weights = normalize_weights(
            task_weights or [1.0] + [0.0] * (len(TASK_METHODS) - 1), len(TASK_METHODS)
        )
        self.agv_weights = normalize_weights(agv_weights or [1.0, 0.0], len(AGV_METHODS))

        # Key: product_id, Value: agv unique id
        self.assigned_products: Dict[str, str] = {}
        # Key: product_id, Value: line_name
        self.product_line: Dict[str, str] = {}
        # Key: unique_agv_id, Value: task dict
        self.agv_tasks: Dict[str, Dict[str, Any]] = {}
        # Key: command_id, Value: command context
        self.issued_commands: Dict[str, Dict[str, Any]] = {}
        self._command_counter = 0

    def _next_command_id(self) -> str:
        self._command_counter += 1
        return f"scheduler-command-{self._command_counter:08d}"

    def _active_single_method(self, weights: Sequence[float], methods: Sequence[str]) -> Optional[str]:
        tol = 1e-9
        active_indices = [idx for idx, value in enumerate(weights) if float(value) > tol]
        if len(active_indices) != 1:
            return None

        active_idx = active_indices[0]
        if abs(float(weights[active_idx]) - 1.0) > tol:
            return None
        return methods[active_idx]

    def _select_line_for_product_exact(self, method: str, product_data: Dict[str, Any], snapshot: Dict[str, Any]) -> Optional[str]:
        candidate_lines = list(snapshot["lines"].keys())
        if not candidate_lines:
            return None

        if method == "default":
            return None

        if method == "sq":
            wip_stats = snapshot.get("global_stats", {}).get("wip", {})
            min_wip = float("inf")
            selected = candidate_lines[0]
            random.shuffle(candidate_lines)
            for line_name in candidate_lines:
                current_wip = wip_stats.get(line_name, 0)
                if current_wip < min_wip:
                    min_wip = current_wip
                    selected = line_name
            return selected

        if method == "lwt":
            line_work = {line_name: 0.0 for line_name in candidate_lines}
            for p_data in snapshot["products"].values():
                owner = p_data.get("line_owner")
                if owner and owner in line_work:
                    line_work[owner] += p_data.get("metrics", {}).get("remaining_time", 0.0)

            min_work = float("inf")
            selected = candidate_lines[0]
            random.shuffle(candidate_lines)
            for line_name in candidate_lines:
                current_work = line_work[line_name]
                if current_work < min_work:
                    min_work = current_work
                    selected = line_name
            return selected

        if method == "met":
            product_type = product_data["type"]
            macro_times = snapshot.get("static", {}).get("macro_times", {})

            min_time = float("inf")
            selected = candidate_lines[0]
            random.shuffle(candidate_lines)
            for line_name in candidate_lines:
                times = macro_times.get(line_name, {})
                if product_type == "P3":
                    total_time = float(times.get("P3_phase1", 0.0)) + float(times.get("P3_phase2", 0.0))
                else:
                    total_time = float(times.get(product_type, 0.0))

                if total_time <= 0.0:
                    total_time = float("inf")

                if total_time < min_time:
                    min_time = total_time
                    selected = line_name
            return selected

        return None

    def _line_method_scores(
        self,
        method: str,
        product_data: Dict[str, Any],
        snapshot: Dict[str, Any],
        candidate_lines: List[str],
    ) -> Dict[str, float]:
        scores = {line_name: 0.0 for line_name in candidate_lines}
        scores[NO_ASSIGN_KEY] = 0.0

        if method == "default":
            scores[NO_ASSIGN_KEY] = 1.0
            return scores

        if method == "sq":
            wip_stats = snapshot.get("global_stats", {}).get("wip", {})
            raw = {line_name: float(wip_stats.get(line_name, 0.0)) for line_name in candidate_lines}
            normalized = rank_normalize(raw, higher_is_better=False)
            scores.update(normalized)
            return scores

        if method == "lwt":
            line_work = {line_name: 0.0 for line_name in candidate_lines}
            for p_data in snapshot["products"].values():
                owner = p_data.get("line_owner")
                if owner in line_work:
                    line_work[owner] += float(p_data.get("metrics", {}).get("remaining_time", 0.0))
            normalized = rank_normalize(line_work, higher_is_better=False)
            scores.update(normalized)
            return scores

        if method == "met":
            product_type = product_data["type"]
            macro_times = snapshot.get("static", {}).get("macro_times", {})
            raw: Dict[str, float] = {}
            for line_name in candidate_lines:
                times = macro_times.get(line_name, {})
                if product_type == "P3":
                    total_time = float(times.get("P3_phase1", 0.0)) + float(times.get("P3_phase2", 0.0))
                else:
                    total_time = float(times.get(product_type, 0.0))
                if total_time <= 0.0:
                    total_time = float("inf")
                raw[line_name] = total_time
            normalized = rank_normalize(raw, higher_is_better=False)
            scores.update(normalized)
            return scores

        return scores

    def _select_line_for_product(self, product_data: Dict[str, Any], snapshot: Dict[str, Any]) -> Optional[str]:
        exact_method = self._active_single_method(self.line_weights, LINE_METHODS)
        if exact_method is not None:
            return self._select_line_for_product_exact(exact_method, product_data, snapshot)

        candidate_lines = sorted(snapshot["lines"].keys())
        if not candidate_lines:
            return None

        aggregate_scores = {line_name: 0.0 for line_name in candidate_lines}
        aggregate_scores[NO_ASSIGN_KEY] = 0.0

        for idx, method in enumerate(LINE_METHODS):
            weight = float(self.line_weights[idx])
            if weight <= 0.0:
                continue
            method_scores = self._line_method_scores(method, product_data, snapshot, candidate_lines)
            for key in aggregate_scores:
                aggregate_scores[key] += weight * float(method_scores.get(key, 0.0))

        # Prefer assigning a concrete line when scores tie.
        def best_key_fn(item: Tuple[str, float]) -> Tuple[float, int, str]:
            key, score = item
            is_real_line = 1 if key != NO_ASSIGN_KEY else 0
            return (score, is_real_line, key)

        best_key = max(aggregate_scores.items(), key=best_key_fn)[0]
        if best_key == NO_ASSIGN_KEY:
            return None
        return best_key

    def _task_raw_priority(
        self,
        method: str,
        p_data: Dict[str, Any],
        source: str,
        buffer_type: Optional[str],
        current_time: float,
    ) -> float:
        metrics = p_data.get("metrics", {})

        if method == "default":
            order_priority = 1
            p_priority = p_data.get("priority", "low")
            if p_priority == "high":
                order_priority = 3
            elif p_priority == "medium":
                order_priority = 2

            base_priority = 10
            if source == "QualityCheck" and buffer_type == "output_buffer":
                base_priority = 5
            elif source == "Conveyor_CQ" and buffer_type in ("upper", "lower"):
                base_priority = 3
            elif source == "RawMaterial":
                base_priority = 1

            return float(base_priority + order_priority)

        if method == "spt":
            return -float(metrics.get("next_op_time", 1000.0))

        if method == "lwkr":
            return -float(metrics.get("remaining_time", 1000.0))

        if method == "lopnr":
            return -float(metrics.get("remaining_ops", 999))

        if method == "edd":
            return -float(p_data.get("due_date", 999999.0))

        if method == "cr":
            due_date = float(p_data.get("due_date", 999999.0))
            remaining_time = float(metrics.get("remaining_time", 1.0))
            if remaining_time <= 0.001:
                remaining_time = 0.001
            cr_val = (due_date - current_time) / remaining_time
            return -cr_val

        if method == "ms":
            due_date = float(p_data.get("due_date", 999999.0))
            remaining_time = float(metrics.get("remaining_time", 0.0))
            slack = due_date - current_time - remaining_time
            return -slack

        if method == "fifo":
            arrival_time = float(p_data.get("last_update_time", current_time))
            return -arrival_time

        return 0.0

    def _apply_weighted_task_priority(self, pending_tasks: List[Dict[str, Any]], current_time: float) -> None:
        if not pending_tasks:
            return

        exact_method = self._active_single_method(self.task_weights, TASK_METHODS)
        if exact_method is not None:
            for task in pending_tasks:
                p_data = task["product_data"]
                task["priority"] = self._task_raw_priority(
                    method=exact_method,
                    p_data=p_data,
                    source=task["source"],
                    buffer_type=task.get("source_buffer"),
                    current_time=current_time,
                )
            pending_tasks.sort(key=lambda task: task["priority"], reverse=True)
            return

        aggregate_scores = {task["product_id"]: 0.0 for task in pending_tasks}

        for idx, method in enumerate(TASK_METHODS):
            weight = float(self.task_weights[idx])
            if weight <= 0.0:
                continue

            raw_by_product: Dict[str, float] = {}
            for task in pending_tasks:
                p_data = task["product_data"]
                raw_by_product[task["product_id"]] = self._task_raw_priority(
                    method=method,
                    p_data=p_data,
                    source=task["source"],
                    buffer_type=task.get("source_buffer"),
                    current_time=current_time,
                )

            normalized = rank_normalize(raw_by_product, higher_is_better=True)
            for product_id, score in normalized.items():
                aggregate_scores[product_id] += weight * score

        for task in pending_tasks:
            task["priority"] = aggregate_scores.get(task["product_id"], 0.0)

        pending_tasks.sort(key=lambda task: (-float(task["priority"]), str(task["product_id"])))

    def _default_pair_ranking(
        self,
        line_name: str,
        available_agvs: List[str],
        pending_tasks: List[Dict[str, Any]],
    ) -> List[Tuple[str, int]]:
        ranking: List[Tuple[str, int]] = []
        agv_candidates = sorted(available_agvs)
        remaining_task_indices = list(range(len(pending_tasks)))

        for agv_id in agv_candidates:
            chosen_index: Optional[int] = None
            for task_index in remaining_task_indices:
                task = pending_tasks[task_index]
                task_line = task.get("line_id")
                if task_line and task_line != line_name:
                    continue
                if self._can_agv_perform_task(agv_id, task):
                    chosen_index = task_index
                    break

            if chosen_index is not None:
                ranking.append((agv_id, chosen_index))
                remaining_task_indices.remove(chosen_index)

        return ranking

    def _nvf_pair_ranking(
        self,
        line_name: str,
        available_agvs: List[str],
        pending_tasks: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
    ) -> List[Tuple[str, int]]:
        ranking: List[Tuple[str, int]] = []
        remaining_agvs = list(available_agvs)
        agv_data_map = snapshot["lines"][line_name]["agvs"]
        travel_times = snapshot.get("static", {}).get("travel_times", {})

        def distance(agv_id: str, target_point: str) -> float:
            agv_curr_point = agv_data_map[agv_id]["current_point"]
            if agv_curr_point == target_point:
                return 0.0
            return float(travel_times.get(agv_curr_point, {}).get(target_point, 999.0))

        for task_index, task in enumerate(pending_tasks):
            if not remaining_agvs:
                break
            task_line = task.get("line_id")
            if task_line and task_line != line_name:
                continue

            feasible_agvs = [
                agv_id
                for agv_id in remaining_agvs
                if self._can_agv_perform_task(agv_id, task)
            ]
            if not feasible_agvs:
                continue

            best_agv = min(feasible_agvs, key=lambda agv_id: (distance(agv_id, task["source_point"]), agv_id))
            ranking.append((best_agv, task_index))
            remaining_agvs.remove(best_agv)

        return ranking

    def _dispatch_agvs(
        self,
        line_name: str,
        available_agvs: List[str],
        pending_tasks: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        if not available_agvs or not pending_tasks:
            return []

        exact_method = self._active_single_method(self.agv_weights, AGV_METHODS)
        if exact_method is not None:
            if exact_method == "default":
                ranking = self._default_pair_ranking(line_name, available_agvs, pending_tasks)
                return [(agv_id, pending_tasks[task_index]) for agv_id, task_index in ranking]
            if exact_method == "nvf":
                ranking = self._nvf_pair_ranking(line_name, available_agvs, pending_tasks, snapshot)
                return [(agv_id, pending_tasks[task_index]) for agv_id, task_index in ranking]

        agv_data_map = snapshot["lines"][line_name]["agvs"]
        travel_times = snapshot.get("static", {}).get("travel_times", {})

        def pair_distance(agv_id: str, task_index: int) -> float:
            agv_curr_point = agv_data_map[agv_id]["current_point"]
            target_point = pending_tasks[task_index]["source_point"]
            if agv_curr_point == target_point:
                return 0.0
            return float(travel_times.get(agv_curr_point, {}).get(target_point, 999.0))

        # Build feasible pairs for safety checks and tie-break use.
        feasible_pairs: List[Tuple[str, int]] = []
        for task_index, task in enumerate(pending_tasks):
            task_line = task.get("line_id")
            if task_line and task_line != line_name:
                continue
            for agv_id in available_agvs:
                if self._can_agv_perform_task(agv_id, task):
                    feasible_pairs.append((agv_id, task_index))

        if not feasible_pairs:
            return []

        pair_scores: Dict[Tuple[str, int], float] = {pair: 0.0 for pair in feasible_pairs}
        ranked_pairs_seen = set()

        for idx, method in enumerate(AGV_METHODS):
            weight = float(self.agv_weights[idx])
            if weight <= 0.0:
                continue

            if method == "default":
                ranking = self._default_pair_ranking(line_name, available_agvs, pending_tasks)
            elif method == "nvf":
                ranking = self._nvf_pair_ranking(line_name, available_agvs, pending_tasks, snapshot)
            else:
                ranking = []

            if not ranking:
                continue

            ranked_pairs_seen.update(ranking)

            rank_scores: Dict[Tuple[str, int], float] = {}
            if len(ranking) == 1:
                rank_scores[ranking[0]] = 1.0
            else:
                for rank, pair in enumerate(ranking):
                    rank_scores[pair] = 1.0 - (rank / float(len(ranking) - 1))

            for pair, score in rank_scores.items():
                if pair in pair_scores:
                    pair_scores[pair] += weight * score

        # Keep all pairs that were selected by at least one method-specific ranking.
        # This preserves one-hot behavior where the last ranked pair can legitimately receive score 0.
        candidate_pairs = [pair for pair in ranked_pairs_seen if pair in pair_scores]
        if not candidate_pairs:
            candidate_pairs = self._default_pair_ranking(line_name, available_agvs, pending_tasks)

        def sort_key(pair: Tuple[str, int]) -> Tuple[float, float, float, str, str]:
            agv_id, task_index = pair
            task = pending_tasks[task_index]
            return (
                -pair_scores.get(pair, 0.0),
                -float(task.get("priority", 0.0)),
                pair_distance(agv_id, task_index),
                agv_id,
                str(task["product_id"]),
            )

        chosen_assignments: List[Tuple[str, Dict[str, Any]]] = []
        used_agvs = set()
        used_tasks = set()

        for agv_id, task_index in sorted(candidate_pairs, key=sort_key):
            if agv_id in used_agvs or task_index in used_tasks:
                continue
            chosen_assignments.append((agv_id, pending_tasks[task_index]))
            used_agvs.add(agv_id)
            used_tasks.add(task_index)

        return chosen_assignments

    def run(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        commands = []
        current_time = snapshot["time"]

        self._update_state(snapshot)

        # Layer 1: assign line for new raw-material products
        for p_id, p_data in snapshot["products"].items():
            if p_id not in self.assigned_products and p_id not in self.product_line:
                if p_data["location"] == "RawMaterial":
                    selected_line = self._select_line_for_product(p_data, snapshot)
                    if selected_line:
                        self.product_line[p_id] = selected_line
                        snapshot["products"][p_id]["line_owner"] = selected_line
                        if "global_stats" in snapshot and "wip" in snapshot["global_stats"]:
                            wip_map = snapshot["global_stats"]["wip"]
                            wip_map[selected_line] = wip_map.get(selected_line, 0) + 1

        for line_name, line_data in snapshot["lines"].items():
            available_agvs = []
            busy_agvs = []

            for agv_id, agv_data in line_data["agvs"].items():
                unique_agv_id = f"{line_name}_{agv_id}"
                if unique_agv_id in self.agv_tasks:
                    busy_agvs.append(agv_id)
                    continue

                if agv_data["status"] == "idle" and not agv_data["payload"]:
                    available_agvs.append(agv_id)
                else:
                    busy_agvs.append(agv_id)

            pending_tasks: List[Dict[str, Any]] = []

            for p_id, p_data in snapshot["products"].items():
                if p_id in self.assigned_products:
                    continue

                owner_line = self.product_line.get(p_id)
                if owner_line and owner_line != line_name:
                    continue

                next_dest = self._get_next_destination(p_data)
                if not next_dest:
                    continue

                source = p_data["location"]
                buffer_type = p_data.get("buffer_type")

                if source in line_data["stations"]:
                    if buffer_type != "output_buffer":
                        continue
                    if p_data.get("quality_status") == "scrap":
                        continue

                if source.startswith("Conveyor"):
                    if source == "Conveyor_CQ":
                        if buffer_type == "main":
                            continue
                    else:
                        continue

                source_point = self._get_interacting_point(line_data, source, snapshot)
                dest_point = self._get_interacting_point(line_data, next_dest, snapshot)

                if source_point and dest_point:
                    pending_tasks.append(
                        {
                            "type": "transfer",
                            "product_id": p_id,
                            "product_data": p_data,
                            "source": source,
                            "source_point": source_point,
                            "source_buffer": buffer_type,
                            "destination": next_dest,
                            "destination_point": dest_point,
                            "line_id": owner_line,
                            "priority": 0.0,
                        }
                    )

            self._apply_weighted_task_priority(pending_tasks, current_time)

            new_assignments = self._dispatch_agvs(line_name, available_agvs, pending_tasks, snapshot)
            for agv_id, task_to_assign in new_assignments:
                unique_agv_id = f"{line_name}_{agv_id}"

                if task_to_assign.get("line_id") is None:
                    task_to_assign["line_id"] = line_name
                    self.product_line[task_to_assign["product_id"]] = line_name

                self.agv_tasks[unique_agv_id] = task_to_assign
                self.assigned_products[task_to_assign["product_id"]] = unique_agv_id

                cmd_id = self._next_command_id()
                cmd = {
                    "line_id": line_name,
                    "command_id": cmd_id,
                    "action": "move",
                    "target": agv_id,
                    "params": {"target_point": task_to_assign["source_point"]},
                }
                commands.append(cmd)
                self.issued_commands[cmd_id] = {
                    "agv_id": unique_agv_id,
                    "type": "move_to_source",
                    "task": task_to_assign,
                }

            for agv_id in busy_agvs:
                unique_agv_id = f"{line_name}_{agv_id}"
                if unique_agv_id not in self.agv_tasks:
                    continue

                task = self.agv_tasks[unique_agv_id]
                agv_data = line_data["agvs"][agv_id]

                if agv_data["payload"]:
                    if task["product_id"] in agv_data["payload"]:
                        if agv_data["current_point"] != task["destination_point"]:
                            if agv_data["status"] in ("idle", "interacting"):
                                cmd_id = self._next_command_id()
                                cmd = {
                                    "line_id": line_name,
                                    "command_id": cmd_id,
                                    "action": "move",
                                    "target": agv_id,
                                    "params": {"target_point": task["destination_point"]},
                                }
                                commands.append(cmd)
                                self.issued_commands[cmd_id] = {
                                    "agv_id": unique_agv_id,
                                    "type": "move_to_dest",
                                    "task": task,
                                }
                        else:
                            if agv_data["status"] == "idle":
                                cmd_id = self._next_command_id()
                                cmd = {
                                    "line_id": line_name,
                                    "command_id": cmd_id,
                                    "action": "unload",
                                    "target": agv_id,
                                    "params": {"product_id": task["product_id"]},
                                }
                                commands.append(cmd)
                                self.issued_commands[cmd_id] = {
                                    "agv_id": unique_agv_id,
                                    "type": "unload",
                                    "task": task,
                                }
                else:
                    if agv_data["current_point"] == task["source_point"]:
                        if agv_data["status"] == "idle":
                            load_params = {"product_id": task["product_id"]}
                            if "source_buffer" in task:
                                load_params["buffer"] = task["source_buffer"]

                            cmd_id = self._next_command_id()
                            cmd = {
                                "line_id": line_name,
                                "command_id": cmd_id,
                                "action": "load",
                                "target": agv_id,
                                "params": load_params,
                            }
                            commands.append(cmd)
                            self.issued_commands[cmd_id] = {
                                "agv_id": unique_agv_id,
                                "type": "load",
                                "task": task,
                            }
                    else:
                        if agv_data["status"] == "idle":
                            cmd_id = self._next_command_id()
                            cmd = {
                                "line_id": line_name,
                                "command_id": cmd_id,
                                "action": "move",
                                "target": agv_id,
                                "params": {"target_point": task["source_point"]},
                            }
                            commands.append(cmd)
                            self.issued_commands[cmd_id] = {
                                "agv_id": unique_agv_id,
                                "type": "move_to_source_retry",
                                "task": task,
                            }

        return commands

    def _update_state(self, snapshot: Dict[str, Any]) -> None:
        if "commands" in snapshot:
            for cmd_id, _response in snapshot["commands"].items():
                if cmd_id in self.issued_commands:
                    pass

        completed_agvs = []
        for unique_agv_id, task in self.agv_tasks.items():
            product_id = task["product_id"]
            if product_id in snapshot["products"]:
                product_data = snapshot["products"][product_id]
                if product_data["location"] == task["destination"]:
                    completed_agvs.append(unique_agv_id)

        for unique_agv_id in completed_agvs:
            task = self.agv_tasks.pop(unique_agv_id)
            if task["product_id"] in self.assigned_products:
                del self.assigned_products[task["product_id"]]

    def _get_interacting_point(
        self, line_data: Dict[str, Any], device_id: str, snapshot: Dict[str, Any]
    ) -> Optional[str]:
        if device_id in line_data["stations"]:
            points = line_data["stations"][device_id]["interacting_point"]
            if points:
                return points

        if device_id in line_data["conveyors"]:
            points = line_data["conveyors"][device_id]["interacting_point"]
            if points:
                return points

        if "warehouses" in snapshot and device_id in snapshot["warehouses"]:
            points = snapshot["warehouses"][device_id]["interacting_point"]
            if points:
                return points

        if device_id == "RawMaterial":
            return "P0"
        if device_id == "Warehouse":
            return "P9"

        return None

    def _get_next_destination(self, product_data: Dict[str, Any]) -> Optional[str]:
        product_type = product_data["type"]
        current_loc = product_data["location"]
        process_step = product_data.get("process_step", 0)
        quality_status = product_data.get("quality_status")

        if current_loc == "QualityCheck":
            if quality_status == "rework":
                return "StationC"
            if quality_status == "pass":
                return "Warehouse"
            if quality_status == "scrap":
                return None

        routes = {
            "P1": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],
            "P2": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],
            "P3": [
                "RawMaterial",
                "StationA",
                "StationB",
                "StationC",
                "StationB",
                "StationC",
                "QualityCheck",
                "Warehouse",
            ],
        }

        if product_type not in routes:
            return None

        route = routes[product_type]
        if process_step + 1 < len(route):
            return route[process_step + 1]
        return None

    def _can_agv_perform_task(self, agv_id: str, task: Dict[str, Any]) -> bool:
        source = task["source"]
        buffer_type = task.get("source_buffer")

        if source == "Conveyor_CQ":
            if buffer_type == "lower" and agv_id != "AGV_1":
                return False
            if buffer_type == "upper" and agv_id != "AGV_2":
                return False

        return True


def create_scheduler(
    line_weights: Optional[Sequence[float]] = None,
    task_weights: Optional[Sequence[float]] = None,
    agv_weights: Optional[Sequence[float]] = None,
) -> WeightedHeuristicScheduler:
    return WeightedHeuristicScheduler(
        line_weights=line_weights,
        task_weights=task_weights,
        agv_weights=agv_weights,
    )
