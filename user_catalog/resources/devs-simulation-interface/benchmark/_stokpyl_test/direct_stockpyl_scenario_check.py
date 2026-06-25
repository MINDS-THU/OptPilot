import argparse
import json
import subprocess
import sys
from collections import Counter

import scenario_blueprint as bp
from stockpyl.demand_source import DemandSource
from stockpyl.policy import Policy
from stockpyl.sim import simulation
from stockpyl.supply_chain_network import SupplyChainNetwork
from stockpyl.supply_chain_node import SupplyChainNode
from stockpyl.supply_chain_product import SupplyChainProduct


class RetailDemand(DemandSource):
    def __init__(self, demand_sequence: list[float], product_id: int = 1):
        super().__init__(type='CUSTOM')
        self.demand_sequence = demand_sequence
        self.product_id = product_id

    def validate_parameters(self):
        return

    def generate_demand(self, period=None, *args, **kwargs):
        if self.product_id != 1:
            return 0.0

        sim_period = 0 if period is None else int(period)
        hook_period = sim_period + 1
        idx = hook_period - 1
        if idx < len(self.demand_sequence):
            return float(self.demand_sequence[idx])
        return 20.0


class RetailPolicy(Policy):
    def __init__(self):
        super().__init__(type='CUSTOM', product=1)

    def validate_parameters(self):
        return

    def get_order_quantity(self, *args, **kwargs):
        if self.node is None:
            raise AttributeError('Retail policy node is not set.')

        if 'inventory_position' in kwargs:
            ip = float(kwargs['inventory_position'])
        else:
            ip = float(self.node.state_vars_current.inventory_position(product=1))

        order_qty = max(0.0, 150.0 - ip) if ip <= 50.0 else 0.0

        if not kwargs.get('include_raw_materials'):
            return order_qty

        # Retail has one supplier (Central_DC) and one raw material (product 1).
        return {
            None: {None: order_qty},
            2: {1: order_qty},
        }


def make_retail_holding_cost(dynamic_args):
    unit_cost = float(dynamic_args.get('retailer_holding_cost', 2.5))

    def holding_cost(_items_held):
        node = holding_cost.node_ref
        inv_dict = getattr(node.state_vars_current, 'inventory_level', {})
        inv = max(0.0, float(inv_dict.get(1, 0.0)))
        return inv * unit_cost

    return holding_cost


def parse_stdin_sequence(stdin_data: str) -> list[float]:
    if not stdin_data.strip():
        return []
    return [float(x) for x in stdin_data.split()]


def build_direct_network(dynamic_args: dict, stdin_data: str):
    demand_sequence = parse_stdin_sequence(stdin_data)

    network = SupplyChainNetwork()

    # Node 1: Factory_0
    factory = SupplyChainNode(index=1)
    factory.add_product(SupplyChainProduct(index=1))
    factory.initial_inventory_level = {1: 999999.0}
    factory.inventory_policy = {1: Policy(type='BS', product=1, base_stock_level=999999)}
    factory.supply_type = {1: 'U'}
    network.add_node(factory)

    # Node 2: Central_DC_0
    central = SupplyChainNode(index=2)
    central.add_product(SupplyChainProduct(index=1))
    central.initial_inventory_level = {1: 1500.0}
    central.shipment_lead_time = 3
    central.local_holding_cost = 1.0
    central.stockout_cost = 50.0
    central.inventory_policy = {1: Policy(type='sS', product=1, reorder_point=500, order_up_to_level=2000)}
    network.add_node(central)

    # Retailers: nodes 3..(2+num_retailers)
    num_retailers = int(dynamic_args.get('num_retailers', 3))
    for i in range(num_retailers):
        idx = 3 + i
        retailer = SupplyChainNode(index=idx)
        retailer.add_product(SupplyChainProduct(index=1))
        retailer.initial_inventory_level = {1: 150.0}
        retailer.shipment_lead_time = 1
        retailer.stockout_cost = 100.0
        retailer.inventory_policy = {1: RetailPolicy()}
        retailer.demand_source = {1: RetailDemand(demand_sequence=demand_sequence, product_id=1)}
        retailer.local_holding_cost = 0.0
        retailer.in_transit_holding_cost = 0.0
        bound_holding_cost = make_retail_holding_cost(dynamic_args)
        bound_holding_cost.node_ref = retailer
        retailer.local_holding_cost_function = bound_holding_cost
        network.add_node(retailer)

    # Edges
    network.add_edge(1, 2)
    for i in range(num_retailers):
        network.add_edge(2, 3 + i)

    id_to_semantic = {1: 'Factory_0', 2: 'Central_DC_0'}
    for i in range(num_retailers):
        id_to_semantic[3 + i] = f'Retailer_{i}'

    return network, id_to_semantic


def extract_logs_from_network(network, id_to_semantic: dict, periods: int):
    logs = []
    for node_id, semantic_name in id_to_semantic.items():
        node = network.nodes_by_index[node_id]
        for stockpyl_period in range(periods):
            if stockpyl_period >= len(node.state_vars):
                continue
            sv = node.state_vars[stockpyl_period]
            if sv is None:
                continue

            raw_inventory = {
                k: float(v)
                for k, v in getattr(sv, 'inventory_level', {}).items()
                if isinstance(k, int) and k > 0
            }
            raw_state = {
                'inventory_level': {
                    str(k): max(0.0, v)
                    for k, v in raw_inventory.items()
                },
                'backorder': float(
                    sum(
                        max(0.0, -v)
                        for v in raw_inventory.values()
                    )
                ),
                'holding_cost_incurred': float(getattr(sv, 'holding_cost_incurred', 0.0) or 0.0),
                'stockout_cost_incurred': float(getattr(sv, 'stockout_cost_incurred', 0.0) or 0.0),
            }
            extracted = bp.log_extractor(
                period=stockpyl_period + 1,
                semantic_node_id=semantic_name,
                raw_state=raw_state,
            )
            logs.extend(extracted)

    logs.sort(key=lambda x: x.get('time', 0))
    return logs


def run_direct(seed: int, periods: int, dynamic_args: dict, stdin_data: str):
    network, id_to_semantic = build_direct_network(dynamic_args, stdin_data)
    simulation(network, num_periods=periods, rand_seed=seed)
    return extract_logs_from_network(network, id_to_semantic, periods)


def run_oracle(seed: int, periods: int, dynamic_args: dict, stdin_data: str):
    cmd = [
        sys.executable,
        'oracle_runner.py',
        '--blueprint',
        'scenario_blueprint.py',
        '--seed',
        str(seed),
        '--periods',
        str(periods),
    ]
    for k, v in dynamic_args.items():
        cmd.extend([f'--{k}', str(v)])

    proc = subprocess.run(cmd, input=stdin_data, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f'oracle_runner failed:\n{proc.stderr}')

    logs = []
    for line in proc.stdout.splitlines():
        if line.strip():
            logs.append(json.loads(line))
    return logs


def canonical_counter(logs):
    return Counter(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in logs)


def main():
    parser = argparse.ArgumentParser(description='Scenario-specific direct stockpyl vs oracle check.')
    parser.add_argument('--case_name', type=str, default='Base_Condition')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--periods', type=int, default=100)
    args = parser.parse_args()

    case = None
    for tc in bp.test_cases:
        if tc.get('case_name') == args.case_name:
            case = tc
            break
    if case is None:
        raise ValueError(f'Unknown case_name={args.case_name}')

    dynamic_args = dict(case.get('cli_kwargs', {}))
    stdin_data = str(case.get('stdin_payload', ''))

    direct_logs = run_direct(args.seed, args.periods, dynamic_args, stdin_data)
    oracle_logs = run_oracle(args.seed, args.periods, dynamic_args, stdin_data)

    direct_counter = canonical_counter(direct_logs)
    oracle_counter = canonical_counter(oracle_logs)

    if direct_counter != oracle_counter:
        print('[FAIL] direct stockpyl script does not match oracle_runner output.')
        direct_only = list((direct_counter - oracle_counter).items())
        oracle_only = list((oracle_counter - direct_counter).items())
        print(f'  direct_log_count={len(direct_logs)} oracle_log_count={len(oracle_logs)}')
        print('  direct_only_samples:')
        for payload, count in direct_only[:5]:
            print(f'    count={count} log={payload}')
        print('  oracle_only_samples:')
        for payload, count in oracle_only[:5]:
            print(f'    count={count} log={payload}')
        print(f'  direct_kpis={json.dumps(bp.extract_kpis(direct_logs), ensure_ascii=False, sort_keys=True)}')
        print(f'  oracle_kpis={json.dumps(bp.extract_kpis(oracle_logs), ensure_ascii=False, sort_keys=True)}')
        sys.exit(1)

    kpis = bp.extract_kpis(oracle_logs)
    print('[OK] direct stockpyl scenario script matches oracle_runner output.')
    print(f'  case_name={args.case_name} seed={args.seed} periods={args.periods}')
    print(f'  dynamic_args={json.dumps(dynamic_args, ensure_ascii=False, sort_keys=True)}')
    print(f'  log_count={len(oracle_logs)}')
    print(f'  kpis={json.dumps(kpis, ensure_ascii=False, sort_keys=True)}')


if __name__ == '__main__':
    main()
