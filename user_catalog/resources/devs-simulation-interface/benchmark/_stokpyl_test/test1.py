import json
from stockpyl.supply_chain_network import serial_system
from stockpyl.sim import simulation
from stockpyl.policy import Policy

def run_comprehensive_probe():
    print("=== Step 1: 初始化全要素网络 ===")
    network = serial_system(
        num_nodes=3,
        node_order_in_system=[3, 2, 1],
        echelon_holding_cost=[1, 2, 4],
        stockout_cost=50,
        shipment_lead_time=[1, 2, 1],
        demand_type='N', mean=20, standard_deviation=5
    )

    # 挂载策略
    network.nodes_by_index[1].inventory_policy = Policy(type='sS', reorder_point=40, order_up_to_level=100)
    network.nodes_by_index[2].inventory_policy = Policy(type='sS', reorder_point=100, order_up_to_level=300)
    network.nodes_by_index[3].inventory_policy = Policy(type='BS', base_stock_level=1000)

    print("=== Step 2: 运行仿真 ===")
    # stockpyl 有进度条，说明它在认真推进时间步
    simulation(network, num_periods=5)

    print("\n=== Step 3: 毫无保留的原始状态 Dump ===")
    node1 = network.nodes_by_index[1]
    node2 = network.nodes_by_index[2]
    node3 = network.nodes_by_index[3]

    # 遍历状态列表（索引即为 period）
    cnt = 0
    for period, state in enumerate(node1.state_vars):
        if period == 0 or state is None: 
            continue
        
        # 提取 state 对象的所有非私有属性
        raw_state_dict = {
            k: v for k, v in vars(state).items() 
            if not k.startswith('_')
        }
        
        print(f"\n--- Node 1 (Retailer) | Period {period} 原始全量数据 ---")
        print(json.dumps(raw_state_dict, default=str))
        with open(f"node1_period{period}_raw_state.json", "w") as f:
            json.dump(raw_state_dict, f, default=str)
        cnt += 1
        if cnt >= 2: break
    
    cnt = 0
    for period, state in enumerate(node2.state_vars):
        if period == 0 or state is None: 
            continue
        
        raw_state_dict = {
            k: v for k, v in vars(state).items() 
            if not k.startswith('_')
        }
        
        print(f"\n--- Node 2 (Distributor) | Period {period} 原始全量数据 ---")
        print(json.dumps(raw_state_dict, default=str))
        with open(f"node2_period{period}_raw_state.json", "w") as f:
            json.dump(raw_state_dict, f, default=str)
        cnt += 1
        if cnt >= 2: break
        
    cnt = 0
    for period, state in enumerate(node3.state_vars):
        if period == 0 or state is None: 
            continue
        
        raw_state_dict = {
            k: v for k, v in vars(state).items() 
            if not k.startswith('_')
        }
        
        print(f"\n--- Node 3 (Factory) | Period {period} 原始全量数据 ---")
        print(json.dumps(raw_state_dict, default=str))
        with open(f"node3_period{period}_raw_state.json", "w") as f:
            json.dump(raw_state_dict, f, default=str)
        cnt += 1
        if cnt >= 2: break

if __name__ == "__main__":
    run_comprehensive_probe()