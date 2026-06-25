import argparse
import importlib.util
import json
import subprocess
import sys
from collections import Counter

from stockpyl.demand_source import DemandSource
from stockpyl.policy import Policy
from stockpyl.sim import simulation
from stockpyl.supply_chain_network import SupplyChainNetwork
from stockpyl.supply_chain_node import SupplyChainNode
from stockpyl.supply_chain_product import SupplyChainProduct


def load_blueprint(path, dynamic_args, stdin_data):
    spec = importlib.util.spec_from_file_location('blueprint', path)
    bp = importlib.util.module_from_spec(spec)
    import kpi_utils

    sys.modules['kpi_utils'] = kpi_utils
    spec.loader.exec_module(bp)
    setattr(bp, 'DYNAMIC_ARGS', dynamic_args)
    setattr(bp, 'STDIN_DATA', stdin_data)
    return bp


class DirectDemandSource(DemandSource):
    def __init__(self, demand_func, product_id):
        super().__init__(type='CUSTOM')
        self.demand_func = demand_func
        self.product_id = product_id

    def validate_parameters(self):
        return

    def generate_demand(self, period=None, *args, **kwargs):
        sim_period = 0 if period is None else int(period)
        hook_period = sim_period + 1
        val = self.demand_func(period=hook_period, product_id=self.product_id)
        return max(0.0, float(val))


class DirectPolicy(Policy):
    def __init__(self, order_func, product_id, semantic_to_id):
        super().__init__(type='CUSTOM', product=product_id)
        self.order_func = order_func
        self.product_id = product_id
        self.semantic_to_id = semantic_to_id
        self.id_to_semantic = {node_id: semantic for semantic, node_id in semantic_to_id.items()}

    def validate_parameters(self):
        return

    def _inventory_snapshot(self):
        if self.node is None:
            raise AttributeError('Policy node is not set.')

        result = {}
        for prod_id in self.node.product_indices:
            if not isinstance(prod_id, int) or prod_id <= 0:
                continue
            try:
                result[prod_id] = float(self.node.state_vars_current.inventory_position(product=prod_id))
            except Exception:
                init_inv = self.node.get_attribute('initial_inventory_level', product=prod_id)
                result[prod_id] = float(init_inv or 0.0)
        return result

    def _parse_routing(self, period):
        inventory_dict = self._inventory_snapshot()
        routing = self.order_func(period=period, inventory_dict=inventory_dict)
        if not isinstance(routing, dict):
            raise TypeError('policy_func must return dict[str, dict[int, float]].')
        return routing

    def _qty_from_upstream(self, routing, upstream_semantic):
        payload = routing[upstream_semantic]
        if not isinstance(payload, dict):
            raise TypeError(f'policy_func upstream payload for {upstream_semantic} must be dict.')
        qty = float(payload.get(self.product_id, 0.0) or 0.0)
        if qty < 0:
            raise ValueError(f'policy_func returned negative order quantity for product {self.product_id}.')
        return qty

    def _map_order_quantity_dict(self, fg_qty, routing):
        if self.node is None:
            raise AttributeError('Policy node is not set.')

        order_dict = {None: {None: fg_qty}}
        allowed_upstreams = set()
        rm_by_supplier = {}

        raw_materials = self.node.raw_materials_by_product(product=self.product_id, return_indices=True, network_BOM=True)
        for rm_idx in raw_materials:
            suppliers = self.node.raw_material_suppliers_by_raw_material(raw_material=rm_idx, return_indices=True, network_BOM=True)
            for supplier_idx in suppliers:
                order_dict.setdefault(supplier_idx, {})[rm_idx] = 0.0
                rm_by_supplier.setdefault(supplier_idx, []).append(rm_idx)
                if supplier_idx is None:
                    allowed_upstreams.add('EXTERNAL')
                elif supplier_idx in self.id_to_semantic:
                    allowed_upstreams.add(self.id_to_semantic[supplier_idx])

        unknown_upstreams = [u for u in routing.keys() if u not in allowed_upstreams]
        if unknown_upstreams:
            raise ValueError(f'policy_func returned unknown upstream keys: {unknown_upstreams}')

        for upstream_semantic in routing.keys():
            qty = self._qty_from_upstream(routing, upstream_semantic)
            if qty == 0.0:
                continue

            supplier_idx = None if upstream_semantic == 'EXTERNAL' else self.semantic_to_id[upstream_semantic]
            supplier_rms = rm_by_supplier.get(supplier_idx, [])

            if len(supplier_rms) == 0:
                raise ValueError(f'Upstream {upstream_semantic} does not supply product {self.product_id}.')
            if len(supplier_rms) > 1:
                raise ValueError(
                    f'Upstream {upstream_semantic} maps to multiple raw materials {supplier_rms}; route is ambiguous.'
                )

            order_dict[supplier_idx][supplier_rms[0]] = qty

        return order_dict

    def get_order_quantity(self, *args, **kwargs):
        if self.node is None:
            raise AttributeError('Policy node is not set.')

        sim_period = 0 if self.node.network is None or self.node.network.period is None else int(self.node.network.period)
        hook_period = sim_period + 1
        routing = self._parse_routing(period=hook_period)

        fg_qty = 0.0
        for upstream in routing.keys():
            fg_qty += self._qty_from_upstream(routing, upstream)

        if not kwargs.get('include_raw_materials'):
            return fg_qty

        return self._map_order_quantity_dict(fg_qty=fg_qty, routing=routing)


def make_holding_cost_wrapper(node_ref, hook_func):
    def holding_cost(_items_held):
        inv_dict = getattr(node_ref.state_vars_current, 'inventory_level', {})
        pos_inv = {k: max(0.0, float(v)) for k, v in inv_dict.items() if isinstance(k, int) and k > 0}
        return float(hook_func(inventory_dict=pos_inv))

    return holding_cost


def make_stockout_cost_wrapper(node_ref, hook_func):
    def stockout_cost(_inventory_level):
        inv_dict = getattr(node_ref.state_vars_current, 'inventory_level', {})
        shortage = {k: max(0.0, -float(v)) for k, v in inv_dict.items() if isinstance(k, int) and k > 0}
        return float(hook_func(shortage_dict=shortage))

    return stockout_cost


def resolve_count(count_val, dynamic_args):
    if isinstance(count_val, int):
        return count_val
    if isinstance(count_val, str) and count_val.startswith('arg:'):
        arg_name = count_val.split('arg:')[1]
        return int(dynamic_args[arg_name])
    return 1


def build_direct_network(blueprint, dynamic_args):
    network = SupplyChainNetwork()
    topo = blueprint.topology
    hooks = blueprint.custom_hooks

    id_to_semantic = {}
    semantic_to_id = {}
    group_to_instances = {}
    current_idx = 1

    for group_name, cfg in topo['node_groups'].items():
        count = resolve_count(cfg['count'], dynamic_args)
        group_to_instances[group_name] = []
        for i in range(count):
            semantic = f'{group_name}_{i}'
            id_to_semantic[current_idx] = semantic
            semantic_to_id[semantic] = current_idx
            group_to_instances[group_name].append(semantic)
            current_idx += 1

    for group_name, cfg in topo['node_groups'].items():
        for semantic in group_to_instances[group_name]:
            node_idx = semantic_to_id[semantic]
            node = SupplyChainNode(index=node_idx)

            inv_cfg = {int(k): float(v) for k, v in cfg['initial_inventory'].items()}
            product_ids = sorted(pid for pid in inv_cfg.keys() if pid > 0)

            for pid in product_ids:
                node.add_product(SupplyChainProduct(index=pid))

            node.initial_inventory_level = {pid: inv_cfg[pid] for pid in product_ids}

            if 'lead_time' in cfg:
                node.shipment_lead_time = int(cfg['lead_time'])
            if 'holding_cost' in cfg:
                node.local_holding_cost = float(cfg['holding_cost'])
            if 'stockout_cost' in cfg:
                node.stockout_cost = float(cfg['stockout_cost'])
            if cfg.get('role') == 'source':
                node.supply_type = {pid: 'U' for pid in product_ids}

            inv_policy = {}
            demand_source = {}

            for pid in product_ids:
                if group_name in hooks.get('policy_func', {}):
                    inv_policy[pid] = DirectPolicy(hooks['policy_func'][group_name], pid, semantic_to_id)
                else:
                    p_cfg = dict(cfg['policy'])
                    p_cfg['product'] = pid
                    inv_policy[pid] = Policy(**p_cfg)

                if group_name in hooks.get('demand_func', {}):
                    demand_source[pid] = DirectDemandSource(hooks['demand_func'][group_name], pid)

            node.inventory_policy = inv_policy
            node.demand_source = demand_source if demand_source else None

            if group_name in hooks.get('holding_cost_func', {}):
                old_rate = float(cfg.get('holding_cost', 0.0))
                node.local_holding_cost = 0.0
                node.in_transit_holding_cost = old_rate
                node.local_holding_cost_function = make_holding_cost_wrapper(node, hooks['holding_cost_func'][group_name])

            if group_name in hooks.get('stockout_cost_func', {}):
                node.stockout_cost_function = make_stockout_cost_wrapper(node, hooks['stockout_cost_func'][group_name])

            network.add_node(node)

    for edge in topo.get('edges', []):
        if 'from_group' not in edge or 'to_group' not in edge:
            continue
        for from_sem in group_to_instances[edge['from_group']]:
            for to_sem in group_to_instances[edge['to_group']]:
                network.add_edge(semantic_to_id[from_sem], semantic_to_id[to_sem])

    return network, id_to_semantic


def extract_logs_from_network(blueprint, network, id_to_semantic, periods):
    all_logs = []
    for node_id, semantic in id_to_semantic.items():
        node = network.nodes_by_index[node_id]
        for stockpyl_period in range(periods):
            if stockpyl_period >= len(node.state_vars) or node.state_vars[stockpyl_period] is None:
                continue

            sv = node.state_vars[stockpyl_period]
            raw_inventory = {
                k: float(v)
                for k, v in getattr(sv, 'inventory_level', {}).items()
                if isinstance(k, int) and k > 0
            }
            inv = {
                str(k): max(0.0, v)
                for k, v in raw_inventory.items()
            }
            backorder = sum(
                max(0.0, -v)
                for v in raw_inventory.values()
            )

            raw_state = {
                'inventory_level': inv,
                'backorder': float(backorder),
                'holding_cost_incurred': float(getattr(sv, 'holding_cost_incurred', 0.0) or 0.0),
                'stockout_cost_incurred': float(getattr(sv, 'stockout_cost_incurred', 0.0) or 0.0),
            }

            logs = blueprint.log_extractor(period=stockpyl_period + 1, semantic_node_id=semantic, raw_state=raw_state)
            if not isinstance(logs, list):
                raise TypeError('log_extractor must return a list.')
            all_logs.extend(logs)

    all_logs.sort(key=lambda x: x.get('time', 0))
    return all_logs


def run_direct(blueprint, dynamic_args, seed, periods):
    network, id_to_semantic = build_direct_network(blueprint=blueprint, dynamic_args=dynamic_args)
    simulation(network, num_periods=periods, rand_seed=seed)
    return extract_logs_from_network(blueprint=blueprint, network=network, id_to_semantic=id_to_semantic, periods=periods)


def run_oracle_subprocess(blueprint_path, dynamic_args, stdin_data, seed, periods):
    cmd = [
        sys.executable,
        'oracle_runner.py',
        '--blueprint',
        blueprint_path,
        '--seed',
        str(seed),
        '--periods',
        str(periods),
    ]
    for key, value in dynamic_args.items():
        cmd.extend([f'--{key}', str(value)])

    proc = subprocess.run(cmd, input=stdin_data, text=True, capture_output=True, encoding='utf-8')
    if proc.returncode != 0:
        raise RuntimeError(f'oracle_runner failed with code {proc.returncode}: {proc.stderr}')

    logs = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        logs.append(json.loads(line))
    return logs


def parse_dynamic_unknown(unknown):
    if len(unknown) % 2 != 0:
        raise ValueError(f'Unpaired dynamic args: {unknown}')
    parsed = {}
    for i in range(0, len(unknown), 2):
        key = unknown[i]
        val = unknown[i + 1]
        if not key.startswith('--'):
            raise ValueError(f'Unexpected arg format: {key}. Expected --key value')
        parsed[key.lstrip('-')] = val
    return parsed


def get_case_payload(blueprint, case_name):
    for case in getattr(blueprint, 'test_cases', []):
        if case.get('case_name') == case_name:
            return dict(case.get('cli_kwargs', {})), str(case.get('stdin_payload', ''))
    raise ValueError(f'Cannot find case_name={case_name} in blueprint.test_cases')


def canonical_counter(logs):
    return Counter(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in logs)


def main():
    parser = argparse.ArgumentParser(description='Compare oracle_runner against independent direct stockpyl assembly.')
    parser.add_argument('--blueprint', type=str, default='scenario_blueprint.py')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--periods', type=int, default=100)
    parser.add_argument('--case_name', type=str, default='Base_Condition')
    args, unknown = parser.parse_known_args()

    dynamic_overrides = parse_dynamic_unknown(unknown)
    bp_for_case = load_blueprint(args.blueprint, {}, '')
    base_kwargs, stdin_data = get_case_payload(bp_for_case, args.case_name)

    merged_dynamic_args = {**base_kwargs, **dynamic_overrides}

    blueprint = load_blueprint(args.blueprint, merged_dynamic_args, stdin_data)
    direct_logs = run_direct(blueprint=blueprint, dynamic_args=merged_dynamic_args, seed=args.seed, periods=args.periods)
    oracle_logs = run_oracle_subprocess(
        blueprint_path=args.blueprint,
        dynamic_args=merged_dynamic_args,
        stdin_data=stdin_data,
        seed=args.seed,
        periods=args.periods,
    )

    direct_counter = canonical_counter(direct_logs)
    oracle_counter = canonical_counter(oracle_logs)

    if direct_counter != oracle_counter:
        direct_only = list((direct_counter - oracle_counter).items())
        oracle_only = list((oracle_counter - direct_counter).items())

        print('[FAIL] oracle_runner and direct stockpyl logs differ.')
        print(f'  direct_log_count={len(direct_logs)} oracle_log_count={len(oracle_logs)}')
        print('  direct_only_samples:')
        for payload, count in direct_only[:5]:
            print(f'    count={count} log={payload}')
        print('  oracle_only_samples:')
        for payload, count in oracle_only[:5]:
            print(f'    count={count} log={payload}')

        direct_kpis = blueprint.extract_kpis(direct_logs)
        oracle_kpis = blueprint.extract_kpis(oracle_logs)
        print(f'  direct_kpis={json.dumps(direct_kpis, ensure_ascii=False, sort_keys=True)}')
        print(f'  oracle_kpis={json.dumps(oracle_kpis, ensure_ascii=False, sort_keys=True)}')
        sys.exit(1)

    direct_kpis = blueprint.extract_kpis(direct_logs)
    oracle_kpis = blueprint.extract_kpis(oracle_logs)

    print('[OK] oracle_runner and direct stockpyl logs are identical (multiset compare).')
    print(f'  case_name={args.case_name} seed={args.seed} periods={args.periods}')
    print(f'  dynamic_args={json.dumps(merged_dynamic_args, ensure_ascii=False, sort_keys=True)}')
    print(f'  log_count={len(oracle_logs)}')
    print(f'  kpis={json.dumps(oracle_kpis, ensure_ascii=False, sort_keys=True)}')

    if direct_kpis != oracle_kpis:
        print('[WARN] logs equal but KPI dicts differ by representation:')
        print(f'  direct_kpis={json.dumps(direct_kpis, ensure_ascii=False, sort_keys=True)}')
        print(f'  oracle_kpis={json.dumps(oracle_kpis, ensure_ascii=False, sort_keys=True)}')


if __name__ == '__main__':
    main()
