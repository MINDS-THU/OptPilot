import uuid
from typing import Dict, List, Any, Optional

from param_estimator import AVERAGE_ORDER_INTERVAL


class Scheduler:
    def __init__(self):
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

    def run(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        commands = []
        current_time = snapshot["time"]
        
        # 1. Update state based on snapshot
        self._update_state(snapshot)
        
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
                        # Use priority from snapshot if available, else default
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
                            
                        total_priority = base_priority + order_priority
                        
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

            # Assign tasks to available AGVs
            # Sort tasks by priority
            pending_tasks.sort(key=lambda x: x["priority"], reverse=True)
            
            for agv_id in available_agvs:
                unique_agv_id = f"{line_name}_{agv_id}"
                if not pending_tasks:
                    break
                    
                # Find a task this AGV can perform
                task_to_assign = None
                task_index = -1
                
                for i, task in enumerate(pending_tasks):
                    task_line = task.get("line_id")
                    if task_line and task_line != line_name:
                        continue
                    if self._can_agv_perform_task(agv_id, task):
                        task_to_assign = task
                        task_index = i
                        break
                
                if task_to_assign:
                    pending_tasks.pop(task_index)
                    
                    # Assign new task
                    if task_to_assign.get("line_id") is None:
                        task_to_assign["line_id"] = line_name
                        self.product_line[task_to_assign["product_id"]] = line_name
                    self.agv_tasks[unique_agv_id] = task_to_assign
                    self.assigned_products[task_to_assign["product_id"]] = unique_agv_id
                    
                    # Generate command to start task (Move to source)
                    cmd_id = str(uuid.uuid4())
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
                                cmd_id = str(uuid.uuid4())
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
                                cmd_id = str(uuid.uuid4())
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
                            
                            cmd_id = str(uuid.uuid4())
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
                            cmd_id = str(uuid.uuid4())
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

def create_scheduler() -> Scheduler:
    return Scheduler()