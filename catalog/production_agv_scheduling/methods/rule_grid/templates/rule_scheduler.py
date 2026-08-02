"""Executable rule-grid policy extracted from the paper's reference code.

The OptPilot rule-grid method owns this module and stages it into every
candidate bundle.  The production simulator only sees the scheduler API.
"""

import random
from typing import Dict, List, Any, Optional, Tuple


class Scheduler:
    def __init__(self, line_selection_method: str = "default", task_priority_method: str = "default", agv_dispatch_method: str = "default"):
        self.line_selection_method = line_selection_method
        self.task_priority_method = task_priority_method
        self.agv_dispatch_method = agv_dispatch_method

        # Track assigned tasks to avoid duplicates
        # Key: product_id, Value: agv_id
        self.assigned_products: Dict[str, str] = {}
        # Track which line owns each product once a line has picked it (RawMaterial shared until assigned)
        self.product_line: Dict[str, str] = {}

        # Track AGV current task
        # Key: agv_id, Value: Task Dict
        self.agv_tasks: Dict[str, Dict[str, Any]] = {}

        # Track issued commands to verify completion
        # Key: command_id, Value: Task Dict
        self.issued_commands: Dict[str, Dict[str, Any]] = {}
        self._command_counter = 0

    def _next_command_id(self) -> str:
        self._command_counter += 1
        return f"scheduler-command-{self._command_counter:08d}"

    def _select_line_for_product(self, product_data: Dict[str, Any], snapshot: Dict[str, Any]) -> Optional[str]:
        """
        Layer 1: Initial Line Allocation Strategy.
        Decides which line a new product should belong to.
        """
        method = self.line_selection_method

        # Candidates (Available Lines)
        # assuming line names are "line1", "line2", "line3" or dynamic
        candidate_lines = list(snapshot["lines"].keys())
        if not candidate_lines: return None

        if method == "default":
            # Default: No explicit assignment (First-Come-First-Serve by any line's AGV)
            return None

        elif method == "random":
            return random.choice(candidate_lines)

        elif method == "sq":
            # Shortest Queue: Pick line with min WIP
            wip_stats = snapshot.get("global_stats", {}).get("wip", {})
            # Default WIP is 0 if not in stats
            min_wip = float('inf')
            selected = candidate_lines[0]

            # Shuffle candidates to break ties randomly
            random.shuffle(candidate_lines)

            for line_name in candidate_lines:
                curr_wip = wip_stats.get(line_name, 0)
                if curr_wip < min_wip:
                    min_wip = curr_wip
                    selected = line_name
            return selected

        elif method == "lwt":
            # Least Work Remaining: Pick line with min total remaining work, sum up metric.remaining_time for all products in that line
            line_work = {l: 0.0 for l in candidate_lines}
            for p_data in snapshot["products"].values():
                owner = p_data.get("line_owner")
                if owner and owner in line_work:
                    line_work[owner] += p_data.get("metrics", {}).get("remaining_time", 0.0)

            # Pick min
            min_work = float('inf')
            selected = candidate_lines[0]
            random.shuffle(candidate_lines)

            for line_name in candidate_lines:
                curr_work = line_work[line_name]
                if curr_work < min_work:
                    min_work = curr_work
                    selected = line_name
            return selected

        elif method == "met":
            # Minimum Execution Time: Pick line with min theoretical processing time for this product (Does not consider current line load/traffic)
            p_type = product_data["type"]
            macro_times = snapshot.get("static", {}).get("macro_times", {})

            min_time = float('inf')
            selected = candidate_lines[0]
            random.shuffle(candidate_lines)

            for line_name in candidate_lines:
                times = macro_times.get(line_name, {})
                t_total = 0.0
                if p_type == "P3":
                    t_total = times.get("P3_phase1", 0.0) + times.get("P3_phase2", 0.0)
                else:
                    t_total = times.get(p_type, 0.0)
                # If t_total is 0 (missing config), treat as inf to avoid selecting bad line
                if t_total <= 0: t_total = float('inf')

                if t_total < min_time:
                    min_time = t_total
                    selected = line_name
            return selected

        return None

    def _calculate_priority(self, p_data: Dict[str, Any], source: str, buffer_type: Optional[str], current_time: float = 0.0) -> float:
        """
        Layer 2: Task Priority Strategy.
        Calculates priority based on the selected heuristic method.
        """
        method = self.task_priority_method
        metrics = p_data.get("metrics", {})

        if method == "default":
            # Default Logic (Original composed priority)
            order_priority = 1
            p_priority = p_data.get("priority", "low")
            if p_priority == "high": order_priority = 3
            elif p_priority == "medium": order_priority = 2

            # Source-based base priority: QualityCheck output > Conveyor_CQ upper/lower > RawMaterial > others
            base_priority = 10  # default lowest
            if source == "QualityCheck" and buffer_type == "output_buffer":
                base_priority = 5
            elif source == "Conveyor_CQ" and buffer_type in ("upper", "lower"):
                base_priority = 3
            elif source == "RawMaterial":
                base_priority = 1

            return float(base_priority + order_priority)

        elif method == "spt":
            # SPT: Shortest Processing Time for NEXT operation
            # Lower time = Higher Priority.
            # Invert: -time (or 1/time). We use negative time so sort(reverse=True) puts small time first.
            t = metrics.get("next_op_time", 1000.0)
            return -t

        # elif method == "lpt":
        #     # LPT: Longest Processing Time
        #     t = metrics.get("next_op_time", 0.0)
        #     return t

        elif method == "lwkr":
            # LWKR: Least Work Remaining (Total)
            # Prefer products close to finish.
            # Small remaining_time -> High Priority
            t = metrics.get("remaining_time", 1000.0)
            return -t

        elif method == "lopnr":
            # LOPNR: Least Operations Remaining
            # Low remaining_ops -> High Priority
            ops = metrics.get("remaining_ops", 999)
            return -float(ops)

        # elif method == "mopnr":
        #     # MOPNR: Most Operations Remaining
        #     # High remaining_ops -> High Priority
        #     ops = metrics.get("remaining_ops", 0)
        #     return float(ops)

        elif method == "edd":
            # Earliest Due Date
            # Earlier DDL (smaller value) -> Higher Priority
            ddl = p_data.get("due_date", 999999.0)
            prio = -ddl

            # Penalize RawMaterial to prioritize WIP (prevent deadlock)
            # if source == "RawMaterial":
            #      prio -= 10000000.0
            return prio

        elif method == "cr":
            # Critical Ratio: (Due Date - Current Time) / Remaining Processing Time. Small CR (<1 means late) -> High Priority.
            # If PT is 0 (finished), CR is infinite (low priority). We want small logic value to trigger high final priority.
            # Final sort is reverse=True. So we need to invert CR. Priority = -CR.
            ddl = p_data.get("due_date", 999999.0)
            rem_time = metrics.get("remaining_time", 1.0)
            if rem_time <= 0.001: rem_time = 0.001

            cr_val = (ddl - current_time) / rem_time
            prio = -cr_val

            # Penalize RawMaterial to prioritize WIP (prevent deadlock)
            # if source == "RawMaterial":
            #      prio -= 10000000.0
            return prio

        elif method == "ms":
            # MS: Minimum Slack
            # Slack = Due Date - Current Time - Remaining Processing Time
            # Smaller Slack -> Higher Priority
            ddl = p_data.get("due_date", 999999.0)
            rem_time = metrics.get("remaining_time", 0.0)

            slack = ddl - current_time - rem_time
            prio = -slack

            # Penalize RawMaterial to prioritize WIP (prevent deadlock)
            # if source == "RawMaterial":
            #      prio -= 10000000.0
            return prio

        elif method == "fifo":
             # FIFO: First In First Out (based on buffer arrival time)
             # Older arrival time (smaller value) -> Higher priority
             # Use last_update_time from snapshot which represents when it entered current state
             t_arrival = p_data.get("last_update_time", current_time)
             return -t_arrival

        # elif method == "lifo":
        #     # LIFO: Last In First Out
        #     # Newer arrival time (larger value) -> Higher priority
        #     t_arrival = p_data.get("last_update_time", 0.0)
        #     return t_arrival

        elif method == "random":
             # Random Priority
             return random.random()

    def _dispatch_agvs(self, line_name: str, available_agvs: List[str], pending_tasks: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Layer 3: Vehicle Dispatching Strategy.
        Matches available AGVs to pending tasks.
        Returns a list of (agv_id, task) tuples.
        """
        assignments = []
        method = self.agv_dispatch_method

        # Base: If no tasks/agvs, return empty
        if not available_agvs or not pending_tasks:
            return []

        # Default / Random / First-Available
        # Considers tasks in Priority Order (Task-initiated)
        if method in ["default", "random"]:
            # Prepare AGV list order
            agv_candidates = list(available_agvs)
            if method == "default":
                agv_candidates.sort() # Validated deterministic order (by AGV ID)
            else:
                random.shuffle(agv_candidates) # Completely random order

            for agv_id in agv_candidates:
                if not pending_tasks: break

                # Find the first feasible task for this AGV
                task_to_assign = None
                task_index = -1

                for i, task in enumerate(pending_tasks):
                    task_line = task.get("line_id")
                    if task_line and task_line != line_name: continue
                    if self._can_agv_perform_task(agv_id, task):
                        task_to_assign = task
                        task_index = i
                        break

                if task_to_assign:
                    pending_tasks.pop(task_index)
                    assignments.append((agv_id, task_to_assign))

        elif method == "nvf":
            # NVF: Nearest Vehicle First
            # Strategy: Iterate tasks by priority. For each task, find the nearest available AGV.

            # Static time map
            travel_times = snapshot.get("static", {}).get("travel_times", {})
            agv_data_map = snapshot["lines"][line_name]["agvs"]

            # Helper to get dist
            def get_dist(agv_curr_point, target_point):
                if agv_curr_point == target_point: return 0.0
                return travel_times.get(agv_curr_point, {}).get(target_point, 999.0)

            # Valid candidates for this round
            remaining_agvs = list(available_agvs)
            assigned_task_indices = []

            for i, task in enumerate(pending_tasks):
                if not remaining_agvs:
                    break
                task_line = task.get("line_id")
                if task_line and task_line != line_name: continue

                # Find eligible AGVs for this task
                candidates = [agv for agv in remaining_agvs if self._can_agv_perform_task(agv, task)]
                if not candidates:
                    continue

                # Find nearest candidate
                best_agv = None
                min_dist = float('inf')
                for agv_id in candidates:
                    agv_point = agv_data_map[agv_id]["current_point"]
                    dist = get_dist(agv_point, task["source_point"])
                    if dist < min_dist:
                        min_dist = dist
                        best_agv = agv_id
                if best_agv:
                    assignments.append((best_agv, task))
                    remaining_agvs.remove(best_agv)
                    assigned_task_indices.append(i)

            # Remove assigned tasks from pending_tasks list (reverse order)
            for i in sorted(assigned_task_indices, reverse=True):
                pending_tasks.pop(i)

        return assignments

    def run(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        commands = []
        current_time = snapshot["time"]

        # 1. Update state based on snapshot
        self._update_state(snapshot)

        # Layer 1: Line Selection for unassigned products
        for p_id, p_data in snapshot["products"].items():
            if p_id not in self.assigned_products and p_id not in self.product_line:
                # Only select line if product is at RawMaterial (Entry point)
                if p_data["location"] == "RawMaterial":
                    selected_line = self._select_line_for_product(p_data, snapshot)
                    if selected_line:
                        self.product_line[p_id] = selected_line

                        # Sync back to snapshot immediately for next logic steps (mainly for lwt)
                        snapshot["products"][p_id]["line_owner"] = selected_line
                        # Update WIP stats dynamically for subsequent allocations in this tick (mainly for sq)
                        if "global_stats" in snapshot and "wip" in snapshot["global_stats"]:
                            wip_map = snapshot["global_stats"]["wip"]
                            wip_map[selected_line] = wip_map.get(selected_line, 0) + 1

        # Iterate over lines
        for line_name, line_data in snapshot["lines"].items():
            # Get available AGVs
            available_agvs = []
            busy_agvs = []

            for agv_id, agv_data in line_data["agvs"].items():
                unique_agv_id = f"{line_name}_{agv_id}"

                # Treat AGVs with an active task as busy even if they are currently idle.
                # Otherwise, we might reassign them before they load/unload, causing them to loop at P0 without picking up material.
                if unique_agv_id in self.agv_tasks:
                    busy_agvs.append(agv_id)
                    continue

                # Truly idle only when no payload, idle status, and no pending task.
                if agv_data["status"] == "idle" and not agv_data["payload"]:
                    available_agvs.append(agv_id)
                else:
                    busy_agvs.append(agv_id)

            # Identify pending tasks based on products
            pending_tasks = []

            # Scan all products in the snapshot to see if they need transport
            for p_id, p_data in snapshot["products"].items():
                # Skip if already assigned
                if p_id in self.assigned_products:
                    continue

                # Enforce line ownership if already bound
                owner_line = self.product_line.get(p_id)
                if owner_line and owner_line != line_name:
                    continue

                # Determine next destination
                next_dest = self._get_next_destination(p_data)

                if next_dest:
                    # Needs transport
                    source = p_data["location"]
                    buffer_type = p_data.get("buffer_type")

                    # --- Filter out locations where AGV should NOT pick up ---

                    # 1. Stations: Only pick up from output_buffer (e.g., QualityCheck)
                    # Input buffers (buffer_type is None) are for processing.
                    if source in line_data["stations"]:
                        if buffer_type != "output_buffer":
                            continue
                        # Skip scrapped products in QualityCheck output buffer
                        if p_data.get("quality_status") == "scrap":
                            continue

                    # 2. Conveyors:
                    # - Conveyor_AB, Conveyor_BC: Internal transfer only. No AGV pickup.
                    # - Conveyor_CQ: 'main' buffer is internal. 'upper'/'lower' need AGV.
                    if source.startswith("Conveyor"):
                        if source == "Conveyor_CQ":
                            if buffer_type == "main":
                                continue
                        else:
                            # All other conveyors (AB, BC) are fully internal/automated
                            continue

                    # Determine source point and destination point
                    source_point = self._get_interacting_point(line_data, source, snapshot)
                    dest_point = self._get_interacting_point(line_data, next_dest, snapshot)

                    if source_point and dest_point:
                        # Layer 2: Priority Calculation
                        total_priority = self._calculate_priority(p_data, source, buffer_type, current_time)

                        pending_tasks.append({
                            "type": "transfer",
                            "product_id": p_id,
                            "source": source,
                            "source_point": source_point,
                            "source_buffer": buffer_type,
                            "destination": next_dest,
                            "destination_point": dest_point,
                            "line_id": owner_line,
                            "priority": total_priority
                        })

            # Sort tasks by priority (Layer 2)
            pending_tasks.sort(key=lambda x: x["priority"], reverse=True)

            # Layer 3: Dispatch (Assign tasks to available AGVs)
            # Pass snapshot to dispatch for NVF logic
            new_assignments = self._dispatch_agvs(line_name, available_agvs, pending_tasks, snapshot)

            for agv_id, task_to_assign in new_assignments:
                unique_agv_id = f"{line_name}_{agv_id}"

                # Assign new task
                if task_to_assign.get("line_id") is None:
                    task_to_assign["line_id"] = line_name
                    self.product_line[task_to_assign["product_id"]] = line_name
                self.agv_tasks[unique_agv_id] = task_to_assign
                self.assigned_products[task_to_assign["product_id"]] = unique_agv_id

                # Generate command to start task (Move to source)
                cmd_id = self._next_command_id()
                cmd = {
                    "line_id": line_name,
                    "command_id": cmd_id,
                    "action": "move",
                    "target": agv_id,
                    "params": {"target_point": task_to_assign["source_point"]}
                }
                commands.append(cmd)
                self.issued_commands[cmd_id] = {"agv_id": unique_agv_id, "type": "move_to_source", "task": task_to_assign}


            # Handle busy AGVs (continue task)
            for agv_id in busy_agvs:
                unique_agv_id = f"{line_name}_{agv_id}"
                if unique_agv_id not in self.agv_tasks:
                    continue

                task = self.agv_tasks[unique_agv_id]
                agv_data = line_data["agvs"][agv_id]

                # If AGV has (or is believed to have) the product, drive it to destination
                # If AGV has payload, it means it picked up the product.
                if agv_data["payload"]:
                    # Check if it's the right product
                    if task["product_id"] in agv_data["payload"]:
                        # Move to destination as soon as AGV is free to accept a command
                        if agv_data["current_point"] != task["destination_point"]:
                             if agv_data["status"] in ("idle", "interacting"):
                                cmd_id = self._next_command_id()
                                cmd = {
                                    "line_id": line_name,
                                    "command_id": cmd_id,
                                    "action": "move",
                                    "target": agv_id,
                                    "params": {"target_point": task["destination_point"]}
                                }
                                commands.append(cmd)
                                self.issued_commands[cmd_id] = {"agv_id": unique_agv_id, "type": "move_to_dest", "task": task}
                        else:
                            # At destination, unload
                            if agv_data["status"] == "idle":
                                cmd_id = self._next_command_id()
                                cmd = {
                                    "line_id": line_name,
                                    "command_id": cmd_id,
                                    "action": "unload",
                                    "target": agv_id,
                                    "params": {"product_id": task["product_id"]}
                                }
                                commands.append(cmd)
                                self.issued_commands[cmd_id] = {"agv_id": unique_agv_id, "type": "unload", "task": task}
                else:
                    # No payload, moving to source or loading
                    if agv_data["current_point"] == task["source_point"]:
                        # At source, load
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
                                "params": load_params
                            }
                            commands.append(cmd)
                            self.issued_commands[cmd_id] = {"agv_id": unique_agv_id, "type": "load", "task": task}
                    else:
                        # Moving to source
                        # If idle (e.g. interrupted?), move again
                        if agv_data["status"] == "idle":
                            cmd_id = self._next_command_id()
                            cmd = {
                                "line_id": line_name,
                                "command_id": cmd_id,
                                "action": "move",
                                "target": agv_id,
                                "params": {"target_point": task["source_point"]}
                            }
                            commands.append(cmd)
                            self.issued_commands[cmd_id] = {"agv_id": unique_agv_id, "type": "move_to_source_retry", "task": task}

        return commands

    def _update_state(self, snapshot: Dict[str, Any]):
        """
        Updates internal state based on snapshot.
        - Checks command responses.
        - Checks if tasks are completed (product arrived at destination).
        """
        # 1. Check command responses
        if "commands" in snapshot:
            for cmd_id, response in snapshot["commands"].items():
                if cmd_id in self.issued_commands:
                    # Handle error if needed
                    pass

        # 2. Check task completion
        completed_agvs = []
        for unique_agv_id, task in self.agv_tasks.items():
            p_id = task["product_id"]

            # Check if product is at destination
            if p_id in snapshot["products"]:
                p_data = snapshot["products"][p_id]
                if p_data["location"] == task["destination"]:
                    # Task completed!
                    completed_agvs.append(unique_agv_id)

        for unique_agv_id in completed_agvs:
            task = self.agv_tasks.pop(unique_agv_id)
            if task["product_id"] in self.assigned_products:
                del self.assigned_products[task["product_id"]]

    def _get_interacting_point(self, line_data: Dict[str, Any], device_id: str, snapshot: Dict[str, Any]) -> Optional[str]:
        """Helper to find the interacting point for a device."""
        # Check stations
        if device_id in line_data["stations"]:
            points = line_data["stations"][device_id]["interacting_point"]
            if points: return points

        # Check conveyors
        if device_id in line_data["conveyors"]:
            points = line_data["conveyors"][device_id]["interacting_point"]
            if points: return points

        # Check warehouses (global)
        if "warehouses" in snapshot:
            if device_id in snapshot["warehouses"]:
                points = snapshot["warehouses"][device_id]["interacting_point"]
                if points: return points

        # Hardcoded fallback for known global devices if not found in line
        if device_id == "RawMaterial": return "P0" # Assuming P0 is for RawMaterial
        if device_id == "Warehouse": return "P9" # Assuming P9 is for Warehouse

        return None

    def _get_next_destination(self, product_data: Dict[str, Any]) -> Optional[str]:
        p_type = product_data["type"]
        current_loc = product_data["location"]
        process_step = product_data.get("process_step", 0)
        quality_status = product_data.get("quality_status")

        # Handle Quality Rework
        if current_loc == "QualityCheck":
            if quality_status == "rework":
                return "StationC"
            elif quality_status == "pass":
                return "Warehouse"
            elif quality_status == "scrap":
                return None
            else:
                # If unknown, maybe it's still processing or waiting for check
                # If it's in output buffer, it should have a status.
                pass

        # Handle Standard Routes
        routes = {
            "P1": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],
            "P2": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],
            "P3": ["RawMaterial", "StationA", "StationB", "StationC", "StationB", "StationC", "QualityCheck", "Warehouse"]
        }

        if p_type not in routes:
            return None

        route = routes[p_type]

        # Determine next step based on process_step
        # process_step is the index of the current location in the route
        if process_step + 1 < len(route):
            next_loc = route[process_step + 1]
            return next_loc

        return None

    def _can_agv_perform_task(self, agv_id: str, task: Dict[str, Any]) -> bool:
        source = task["source"]
        buffer = task.get("source_buffer")

        # Constraint: Conveyor_CQ buffers
        if source == "Conveyor_CQ":
            if buffer == "lower" and agv_id != "AGV_1":
                return False
            if buffer == "upper" and agv_id != "AGV_2":
                return False

        return True
