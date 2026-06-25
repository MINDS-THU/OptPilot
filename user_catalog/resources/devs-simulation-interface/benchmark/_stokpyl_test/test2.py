import json
from stockpyl.supply_chain_network import serial_system
from stockpyl.sim import simulation
from stockpyl.policy import Policy

def run_multi_echelon_probe():
    print("=== Step 1: 初始化 4 节点网络 (模拟多层订货商与多产品流转) ===")
    network = serial_system(
        num_nodes=4,
        node_order_in_system=[4, 3, 2, 1],
        echelon_holding_cost=[1, 2, 3, 4],
        stockout_cost=50,
        shipment_lead_time=[1, 2, 1, 1],
        demand_type='N', mean=20, standard_deviation=5
    )

    # 为所有节点挂载策略，防止报错
    network.nodes_by_index[1].inventory_policy = Policy(type='sS', reorder_point=40, order_up_to_level=100)
    network.nodes_by_index[2].inventory_policy = Policy(type='sS', reorder_point=80, order_up_to_level=200)
    network.nodes_by_index[3].inventory_policy = Policy(type='sS', reorder_point=150, order_up_to_level=400)
    network.nodes_by_index[4].inventory_policy = Policy(type='BS', base_stock_level=2000)

    print("=== Step 2: 运行仿真 ===")
    simulation(network, num_periods=3)

    print("\n=== Step 3: 提取关键动态字典 (已过滤空值，专注核心逻辑) ===")
    # 我们只看 Period 2 的数据，此时系统已经产生上下游联动
    target_period = 2
    
    for node_idx in [1, 2, 3, 4]:
        node = network.nodes_by_index[node_idx]
        state = node.state_vars[target_period]
        
        print(f"\n--- Node {node_idx} | Period {target_period} 核心嵌套字典 ---")
        
        # 只提取那些是字典结构的核心变量
        fields_to_check = [
            'inventory_level', 
            'inbound_order', 
            'outbound_shipment', 
            'order_quantity', 
            'inbound_shipment_pipeline'
        ]
        
        for field in fields_to_check:
            val = getattr(state, field, None)
            if val:
                # 打印出来，验证我们的猜想
                print(f"{field:<25}: {json.dumps(val, default=str)}")

if __name__ == "__main__":
    run_multi_echelon_probe()