import sys
import json
import argparse
import importlib.util

from stockpyl.supply_chain_node import SupplyChainNode
from stockpyl.supply_chain_network import SupplyChainNetwork
from stockpyl.supply_chain_product import SupplyChainProduct
from stockpyl.policy import Policy
from stockpyl.demand_source import DemandSource
from stockpyl.sim import simulation

# ==========================================
# 第一部分：防波堤 (Adapters) - 屏蔽所有底层脏逻辑
# ==========================================
class LLMDemandSource(DemandSource):
    def __init__(self, demand_func, product_id):
        super().__init__(type='CUSTOM')
        self.demand_func = demand_func
        self.custom_product_id = product_id

    def validate_parameters(self):
        return
        
    def generate_demand(self, period=None, *args, **kwargs):
        sim_period = 0 if period is None else int(period)
        hook_period = sim_period + 1
        raw_val = self.demand_func(period=hook_period, product_id=self.custom_product_id)
        return max(0.0, float(raw_val))

class LLMPolicy(Policy):
    def __init__(self, order_func, product_id, semantic_to_id_map):
        super().__init__(type='CUSTOM', product=product_id)
        self.order_func = order_func
        self.custom_product_id = product_id
        self.semantic_to_id = semantic_to_id_map
        self.id_to_semantic = {node_id: semantic for semantic, node_id in semantic_to_id_map.items()}
        
    def validate_parameters(self):
        return

    def _build_inventory_snapshot(self):
        node = self.node
        if node is None:
            raise AttributeError('Policy node is not set.')

        inventory_snapshot = {}
        for prod_id in node.product_indices:
            if not isinstance(prod_id, int) or prod_id <= 0:
                continue
            try:
                inventory_snapshot[prod_id] = float(node.state_vars_current.inventory_position(product=prod_id))
            except Exception:
                initial_inventory = node.get_attribute('initial_inventory_level', product=prod_id)
                inventory_snapshot[prod_id] = float(initial_inventory or 0.0)

        return inventory_snapshot

    def _extract_routing_quantity(self, routing_dict, upstream_semantic):
        upstream_payload = routing_dict[upstream_semantic]
        if not isinstance(upstream_payload, dict):
            raise TypeError(f'policy_func must return dict[str, dict], but upstream payload for {upstream_semantic} is not dict.')

        qty = float(upstream_payload.get(self.custom_product_id, 0.0) or 0.0)
        if qty < 0:
            raise ValueError(f'policy_func returned negative order quantity for product {self.custom_product_id} from {upstream_semantic}.')
        return qty

    def _build_order_quantity_dict(self, fg_qty, routing_dict):
        node = self.node
        target_prod = self.custom_product_id

        order_quantity_dict = {None: {None: fg_qty}}

        rm_by_supplier = {}
        allowed_upstreams = set()

        raw_materials = node.raw_materials_by_product(product=target_prod, return_indices=True, network_BOM=True)
        for rm_idx in raw_materials:
            suppliers = node.raw_material_suppliers_by_raw_material(raw_material=rm_idx, return_indices=True, network_BOM=True)
            for pred_idx in suppliers:
                if pred_idx not in order_quantity_dict:
                    order_quantity_dict[pred_idx] = {}
                order_quantity_dict[pred_idx][rm_idx] = 0.0
                rm_by_supplier.setdefault(pred_idx, []).append(rm_idx)

                if pred_idx is None:
                    allowed_upstreams.add('EXTERNAL')
                else:
                    semantic = self.id_to_semantic.get(pred_idx)
                    if semantic is not None:
                        allowed_upstreams.add(semantic)

        unknown_upstreams = [key for key in routing_dict.keys() if key not in allowed_upstreams]
        if unknown_upstreams:
            raise ValueError(f'policy_func returned unknown upstream keys: {unknown_upstreams}')

        for upstream_semantic in routing_dict.keys():
            qty = self._extract_routing_quantity(routing_dict, upstream_semantic)
            if qty == 0.0:
                continue

            supplier_idx = None if upstream_semantic == 'EXTERNAL' else self.semantic_to_id[upstream_semantic]
            supplier_rms = rm_by_supplier.get(supplier_idx, [])

            if len(supplier_rms) == 0:
                raise ValueError(f'Upstream {upstream_semantic} does not supply product {target_prod} to node {node.index}.')
            if len(supplier_rms) > 1:
                raise ValueError(
                    f'Upstream {upstream_semantic} maps to multiple raw materials {supplier_rms}; '
                    'policy_func must provide an unambiguous route for this model.'
                )

            order_quantity_dict[supplier_idx][supplier_rms[0]] = qty

        return order_quantity_dict
        
    def get_order_quantity(self, *args, **kwargs):
        node = self.node
        if node is None:
            raise AttributeError('Policy node is not set.')

        sim_period = 0 if node.network is None or node.network.period is None else int(node.network.period)
        hook_period = sim_period + 1
        target_prod = self.custom_product_id
        
        inventory_dict = self._build_inventory_snapshot()
        if 'inventory_position' in kwargs:
            inventory_dict[target_prod] = float(kwargs['inventory_position'])

        llm_routing = self.order_func(period=hook_period, inventory_dict=inventory_dict)
        if not isinstance(llm_routing, dict):
            raise TypeError('policy_func must return dict[str, dict[int, float]].')

        fg_order_qty = 0.0
        for upstream_semantic in llm_routing.keys():
            fg_order_qty += self._extract_routing_quantity(llm_routing, upstream_semantic)

        if not kwargs.get('include_raw_materials'):
            return fg_order_qty

        return self._build_order_quantity_dict(fg_order_qty, llm_routing)

def make_holding_cost_wrapper(node_ref, func):
    def holding_cost(items_held):
        inv_dict = getattr(node_ref.state_vars_current, "inventory_level", {})
        pos_inv = {k: max(0.0, float(v)) for k, v in inv_dict.items() if isinstance(k, int) and k > 0}
        return float(func(inventory_dict=pos_inv))
    return holding_cost

def make_stockout_cost_wrapper(node_ref, func):
    def stockout_cost(inventory_level):
        inv_dict = getattr(node_ref.state_vars_current, "inventory_level", {})
        shortage_dict = {k: max(0.0, -float(v)) for k, v in inv_dict.items() if isinstance(k, int) and k > 0}
        return float(func(shortage_dict=shortage_dict))
    return stockout_cost

# ==========================================
# 第二部分：沙盒执行核心
# ==========================================
class OracleRunner:
    def __init__(self, blueprint_path: str, dynamic_args: dict, stdin_data: str):
        self.dynamic_args = dynamic_args
        self.stdin_data = stdin_data
        self.blueprint = self._load_blueprint(blueprint_path)
        self.network = None
        self.id_to_semantic = {} 
        self.semantic_to_id = {} 

    def _load_blueprint(self, path: str):
        spec = importlib.util.spec_from_file_location("blueprint", path)
        bp = importlib.util.module_from_spec(spec)
        import kpi_utils
        sys.modules['kpi_utils'] = kpi_utils 
        spec.loader.exec_module(bp)
        setattr(bp, 'DYNAMIC_ARGS', self.dynamic_args)
        setattr(bp, 'STDIN_DATA', self.stdin_data)
        return bp

    def _resolve_count(self, count_val):
        if isinstance(count_val, int): return count_val
        if isinstance(count_val, str) and count_val.startswith("arg:"):
            return int(self.dynamic_args[count_val.split("arg:")[1]])
        return 1

    def build_network(self):
        self.network = SupplyChainNetwork()
        topo = self.blueprint.topology
        hooks = self.blueprint.custom_hooks
        current_idx = 1
        group_to_instances = {}
        
        for group_name, config in topo["node_groups"].items():
            count = self._resolve_count(config["count"])
            group_to_instances[group_name] = []
            for i in range(count):
                semantic_name = f"{group_name}_{i}"
                self.id_to_semantic[current_idx] = semantic_name
                self.semantic_to_id[semantic_name] = current_idx
                group_to_instances[group_name].append(semantic_name)
                current_idx += 1
                
        for group_name, config in topo["node_groups"].items():
            for semantic_name in group_to_instances[group_name]:
                node_id = self.semantic_to_id[semantic_name]
                node = SupplyChainNode(index=node_id)

                inv_dict = {int(k): float(v) for k, v in config["initial_inventory"].items()}
                product_ids = sorted([pid for pid in inv_dict.keys() if pid > 0])
                for pid in product_ids:
                    node.add_product(SupplyChainProduct(index=pid))
                node.initial_inventory_level = {pid: inv_dict[pid] for pid in product_ids}

                if "lead_time" in config: node.shipment_lead_time = int(config["lead_time"])

                if "holding_cost" in config:
                    node.local_holding_cost = float(config["holding_cost"])
                if "stockout_cost" in config:
                    node.stockout_cost = float(config["stockout_cost"])
                if config.get("role") == "source":
                    node.supply_type = {pid: 'U' for pid in product_ids}

                inventory_policy_by_product = {}
                demand_source_by_product = {}

                for pid in product_ids:
                    if group_name in hooks.get("policy_func", {}):
                        inventory_policy_by_product[pid] = LLMPolicy(hooks["policy_func"][group_name], pid, self.semantic_to_id)
                    else:
                        p_cfg = dict(config["policy"])
                        p_cfg["product"] = pid
                        inventory_policy_by_product[pid] = Policy(**p_cfg)
                         
                    if group_name in hooks.get("demand_func", {}):
                        demand_source_by_product[pid] = LLMDemandSource(hooks["demand_func"][group_name], pid)

                node.inventory_policy = inventory_policy_by_product
                node.demand_source = demand_source_by_product if demand_source_by_product else None

                if group_name in hooks.get("holding_cost_func", {}):
                    old_rate = float(config.get("holding_cost", 0.0))
                    node.local_holding_cost = 0.0
                    node.in_transit_holding_cost = old_rate
                    node.local_holding_cost_function = make_holding_cost_wrapper(node, hooks["holding_cost_func"][group_name])

                if group_name in hooks.get("stockout_cost_func", {}):
                    node.stockout_cost_function = make_stockout_cost_wrapper(node, hooks["stockout_cost_func"][group_name])

                self.network.add_node(node)

        for edge in topo.get("edges", []):
            if "from_group" in edge and "to_group" in edge:
                for f_sem in group_to_instances[edge["from_group"]]:
                    for t_sem in group_to_instances[edge["to_group"]]:
                        self.network.add_edge(self.semantic_to_id[f_sem], self.semantic_to_id[t_sem])

    def execute_and_stream_logs(self, seed: int, num_periods: int):
        self.build_network()
        simulation(self.network, num_periods=num_periods, rand_seed=seed)
        
        all_logs = []
        for node_id, semantic_name in self.id_to_semantic.items():
            node_obj = self.network.nodes_by_index[node_id]
            for stockpyl_period in range(num_periods):
                if stockpyl_period >= len(node_obj.state_vars) or node_obj.state_vars[stockpyl_period] is None:
                    continue

                sv = node_obj.state_vars[stockpyl_period]
                raw_inventory = {
                    k: float(v)
                    for k, v in getattr(sv, 'inventory_level', {}).items()
                    if isinstance(k, int) and k > 0
                }
                inventory_level_dict = {
                    str(k): max(0.0, v)
                    for k, v in raw_inventory.items()
                }
                backorder_qty = sum(
                    max(0.0, -v)
                    for v in raw_inventory.values()
                )

                purified_state = {
                    'inventory_level': inventory_level_dict,
                    'backorder': float(backorder_qty),
                    'holding_cost_incurred': float(getattr(sv, 'holding_cost_incurred', 0.0) or 0.0),
                    'stockout_cost_incurred': float(getattr(sv, 'stockout_cost_incurred', 0.0) or 0.0),
                }

                semantic_period = stockpyl_period + 1
                logs = self.blueprint.log_extractor(period=semantic_period, semantic_node_id=semantic_name, raw_state=purified_state)
                if not isinstance(logs, list):
                    raise TypeError('log_extractor must return a list.')
                all_logs.extend(logs)
                
        all_logs.sort(key=lambda x: x.get("time", 0))
        for log in all_logs:
            print(json.dumps(log, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--blueprint", type=str, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--periods", type=int, default=100)
    args, unknown = parser.parse_known_args()
    
    dyn_params = {unknown[i].lstrip("-"): unknown[i+1] for i in range(0, len(unknown), 2) if unknown[i].startswith("--")}
    dyn_params.setdefault("seed", args.seed)
    stdin_data = sys.stdin.read() if not sys.stdin.isatty() else ""
    
    runner = OracleRunner(args.blueprint, dyn_params, stdin_data)
    runner.execute_and_stream_logs(args.seed, args.periods)
