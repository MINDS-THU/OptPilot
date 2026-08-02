from typing import Dict, Any, Optional
import collections
import heapq
from factory_sim.config.path_timing import PATH_SEGMENT_TIMES
from snapshot import create_snapshot as create_base_snapshot

class StaticFactoryData:
    """
    Holds static data calculated from configuration for heuristic usage.
    """
    def __init__(self, layout_config: Dict):
        self.layout = layout_config
        self.travel_times: Dict[str, Dict[str, float]] = {}
        self.process_times: Dict[str, Dict[str, Dict[str, float]]] = {} # line -> station -> product -> time
        self.conveyor_times: Dict[str, Dict[str, float]] = {} # line -> conveyor -> time
        self.macro_operations: Dict[str, Dict[str, float]] = {} # line -> product_type -> total_line_time
        
        self._build_travel_time_matrix()
        self._extract_process_times()
        self._calculate_macro_times()

    def _build_travel_time_matrix(self):
        """
        Computes all-pairs shortest paths based on PATH_SEGMENT_TIMES.
        Result is self.travel_times[start][end] = seconds
        """
        graph = collections.defaultdict(list)
        nodes = set()
        
        # Build graph
        for (u, v), cost in PATH_SEGMENT_TIMES.items():
            graph[u].append((v, cost))
            graph[v].append((u, cost)) # Bi-directional
            nodes.add(u)
            nodes.add(v)
            
        # Dijkstra for each node
        for start_node in nodes:
            distances = {node: float('inf') for node in nodes}
            distances[start_node] = 0
            queue = [(0, start_node)]
            
            while queue:
                current_dist, u = heapq.heappop(queue)
                
                if current_dist > distances[u]:
                    continue
                
                for v, weight in graph[u]:
                    distance = current_dist + weight
                    if distance < distances[v]:
                        distances[v] = distance
                        heapq.heappush(queue, (distance, v))
            
            self.travel_times[start_node] = distances

    def _extract_process_times(self):
        """Extracts average processing times."""
        for line_cfg in self.layout.get('production_lines', []):
            line_name = line_cfg['name']
            self.process_times[line_name] = {}
            self.conveyor_times[line_name] = {}
            
            # Stations
            for station in line_cfg.get('stations', []):
                s_id = station['id']
                self.process_times[line_name][s_id] = {}
                for p_type, times in station.get('processing_times', {}).items():
                    # Average of [min, max]
                    avg_time = sum(times) / len(times)
                    self.process_times[line_name][s_id][p_type] = avg_time
            
            # Conveyors
            for conveyor in line_cfg.get('conveyors', []):
                c_id = conveyor['id']
                # Assume standard transfer time
                self.conveyor_times[line_name][c_id] = conveyor.get('transfer_time', 5.0)

    def _calculate_macro_times(self):
        """
        Calculates the time for automated sections.
        Definitions:
        - P1/P2 Standard: A -> Conv -> B -> Conv -> C -> Conv -> QC
        - P3 Phase 1: A -> Conv -> B -> Conv -> C
        - P3 Phase 2: B -> Conv -> C -> Conv -> QC
        """
        
        for line_name, line_data in self.process_times.items():
            self.macro_operations[line_name] = {}
            
            # Initial check if line setup is complete enough
            if "StationA" not in line_data:
                continue
                
            product_types = line_data["StationA"].keys()
            
            t_a = lambda p: line_data.get("StationA", {}).get(p, 0.0)
            t_b = lambda p: line_data.get("StationB", {}).get(p, 0.0)
            t_c = lambda p: line_data.get("StationC", {}).get(p, 0.0)
            t_qc = lambda p: line_data.get("QualityCheck", {}).get(p, 0.0)
            
            # Conveyor times (default 5.0 in get method fallback)
            c_times = self.conveyor_times.get(line_name, {})
            t_ab = c_times.get("Conveyor_AB", 5.0)
            t_bc = c_times.get("Conveyor_BC", 5.0)
            t_cq = c_times.get("Conveyor_CQ", 5.0)
            
            for p_type in product_types:
                if p_type == "P3":
                    # P3 Phase 1: A -> AB -> B -> BC -> C
                    time_p1 = t_a(p_type) + t_ab + t_b(p_type) + t_bc + t_c(p_type)
                    self.macro_operations[line_name]["P3_phase1"] = time_p1
                    
                    # P3 Phase 2: B -> BC -> C -> CQ -> QC
                    time_p2 = t_b(p_type) + t_bc + t_c(p_type) + t_cq + t_qc(p_type)
                    self.macro_operations[line_name]["P3_phase2"] = time_p2
                    
                    # Fallback default key
                    self.macro_operations[line_name][p_type] = time_p1
                else:
                    # Standard: A -> AB -> B -> BC -> C -> CQ -> QC
                    total = t_a(p_type) + t_ab + t_b(p_type) + t_bc + t_c(p_type) + t_cq + t_qc(p_type)
                    self.macro_operations[line_name][p_type] = total


def create_enhanced_snapshot(simulation: Any, static_data: Optional[StaticFactoryData] = None) -> Dict[str, Any]:
    """
    Creates a snapshot with added heuristic information:
    - Pre-calculated distances
    - Estimated remaining processing times
    - Macro-operation costs
    """
    # 1. Get base snapshot
    base_snapshot = create_base_snapshot(simulation)
    
    # Initialize static data if not provided (lazy load)
    if not static_data:
        static_data = StaticFactoryData(simulation.factory.layout)
    
    # 2. Inject Static Context
    base_snapshot["static"] = {
        "travel_times": static_data.travel_times,
        "macro_times": static_data.macro_operations
    }
    
    # 3. Calculate Global WIP Stats for Initial Line Selection
    wip_stats = collections.defaultdict(int)
    
    # Helper to find line owner by checking containment
    def find_line_owner(pid: str) -> Optional[str]:
        for line_name, line_info in base_snapshot["lines"].items():
            # Check Stations (buffer, output_buffer, processing)
            for s_data in line_info["stations"].values():
                if pid in s_data["buffer"]: return line_name
                if "output_buffer" in s_data and pid in s_data["output_buffer"]: return line_name
                if s_data.get("processing_product_id") == pid: return line_name

            # Check Conveyors
            for c_data in line_info["conveyors"].values():
                # Check all possible buffer lists
                for key in ["buffer", "main_buffer", "upper_buffer", "lower_buffer"]:
                    if key in c_data and pid in c_data[key]: return line_name

            # Check AGVs
            for agv_data in line_info["agvs"].values():
                if pid in agv_data["payload"]: return line_name
        return None

    # 4. Enhance Product Data and Build Stats
    routes = {
        "P1": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],
        "P2": ["RawMaterial", "StationA", "StationB", "StationC", "QualityCheck", "Warehouse"],  
        "P3": ["RawMaterial", "StationA", "StationB", "StationC", "StationB", "StationC", "QualityCheck", "Warehouse"]
    }

    for p_id, p_data in base_snapshot["products"].items():
        # Determine Line Owner
        line_owner = find_line_owner(p_id)
        
        # If found, increment stats
        if line_owner:
            wip_stats[line_owner] += 1
            
        p_data["line_owner"] = line_owner

        # Inject DDL (Due Date) from Order info
        due_date = 999999.0
        if hasattr(simulation, 'factory') and hasattr(simulation.factory, 'kpi_calculator'):
            kpi_calc = simulation.factory.kpi_calculator
            order_id = p_data.get("order_id")
            
            if order_id and order_id in kpi_calc.active_orders:
                order_info = kpi_calc.active_orders[order_id]
                due_date = order_info.deadline

        p_data["due_date"] = due_date

        
        # Calculate Remaining Work (LWKR), Remaining Ops (MOPNR), Next Op Time (SPT)
        p_type = p_data["type"]
        step = p_data["process_step"]
        route = routes.get(p_type, [])
        
        remaining_time = 0.0
        next_op_time = 0.0
        remaining_ops = 0
        
        # Calculate Remaining Ops
        if step < len(route):
            remaining_ops = len(route) - 1 - step
        
        # Use first available line for estimation if line_owner is None (assuming line1 stats)
        if line_owner:
            est_line = line_owner
        else:
            # Fallback to first available line in static config if "line1" missing
            if "line1" in static_data.process_times:
                est_line = "line1"
            elif static_data.process_times:
                est_line = list(static_data.process_times.keys())[0]
            else:
                est_line = "line1" 
        
        current_loc = p_data["location"]
        
        # --- Time Calculation Logic ---
        
        if current_loc == "RawMaterial":
            if p_type == "P3":
                # P3 Phase 1 + Phase 2 + Transfer(approx 10s)
                t_p1 = static_data.macro_operations.get(est_line, {}).get("P3_phase1", 0.0)
                t_p2 = static_data.macro_operations.get(est_line, {}).get("P3_phase2", 0.0)
                next_op_time = t_p1
                remaining_time = t_p1 + 7.0 + t_p2
            else:
                # P1/P2 Standard
                t_m = static_data.macro_operations.get(est_line, {}).get(p_type, 0.0)
                next_op_time = t_m
                remaining_time = t_m
            
        elif current_loc == "QualityCheck":
            status = p_data["quality_status"]
            if status == "rework":
                # Rework: C -> QC
                t_c = static_data.process_times.get(est_line, {}).get("StationC", {}).get(p_type, 0.0)
                t_qc = static_data.process_times.get(est_line, {}).get("QualityCheck", {}).get(p_type, 0.0)
                t_conv = static_data.conveyor_times.get(est_line, {}).get("Conveyor_CQ", 5.0)
                
                next_op_time = t_c + t_conv + t_qc
                remaining_time = next_op_time
                remaining_ops += 2 # Penalize rework
                 
            elif status == "pass":
                next_op_time = 0.0
                remaining_time = 0.0
        
        elif p_type == "P3" and step == 3:
            # P3 Special Logic for Second Loop Transition (StationC -> StationB)
            # Treat the transfer + Phase 2 as a macro operation similar to RawMaterial -> Start
            t_p2 = static_data.macro_operations.get(est_line, {}).get("P3_phase2", 0.0)
            next_op_time = t_p2
            remaining_time = t_p2
                 
        else:
            # Logic for mid-stream products (StationA, B, C, Conveyors), mainly for lwt logic
            # Calculate remaining time by summing up subsequent stations in the route
            start_calc_idx = step
            
            # If travelling (conveyor), move to next station
            if str(current_loc).startswith("Conveyor"):
                start_calc_idx += 1
            
            for i in range(start_calc_idx, len(route)):
                node = route[i]
                
                # 1. Processing Time
                # Use get() chain to safely return 0.0 for Warehouse/RawMaterial or missing config
                proc_time = static_data.process_times.get(est_line, {}).get(node, {}).get(p_type, 0.0)
                remaining_time += proc_time
                
                # 2. Transfer Time to NEXT node
                if i < len(route) - 1:
                    next_node = route[i+1]
                    t_trans = 0.0
                    
                    # Generic lookup for standard conveyors
                    if node == "StationA" and next_node == "StationB":
                        t_trans = static_data.conveyor_times.get(est_line, {}).get("Conveyor_AB", 5.0)
                    elif node == "StationB" and next_node == "StationC":
                        t_trans = static_data.conveyor_times.get(est_line, {}).get("Conveyor_BC", 5.0)
                    elif node == "StationC" and next_node == "QualityCheck":
                        t_trans = static_data.conveyor_times.get(est_line, {}).get("Conveyor_CQ", 5.0)
                    # P3 Loop back C->B check (AGV transfer)
                    elif node == "StationC" and next_node == "StationB":
                        t_trans = 7.0 # Estimated AGV travel
                    
                    remaining_time += t_trans
        
        p_data["metrics"] = {
            "remaining_time": remaining_time,
            "next_op_time": next_op_time,
            "remaining_ops": remaining_ops
        }

    
    base_snapshot["global_stats"] = {
        "wip": dict(wip_stats)
    }
    
    return base_snapshot
