import sys
import traceback
from stockpyl.supply_chain_node import SupplyChainNode
from stockpyl.supply_chain_network import SupplyChainNetwork
from stockpyl.policy import Policy
from stockpyl.demand_source import DemandSource
from stockpyl.sim import initialize, step

print("==================================================")
print("🔍 Stockpyl 底层 API 探针 (Hook Explorer V3)")
print("==================================================")

network = SupplyChainNetwork()
node = SupplyChainNode(index=1)
# 挂载两个产品，看看引擎怎么处理
node.initial_inventory_level = {1: 100.0, 2: 100.0}

# ==========================================
# 探针 1：需求源 (DemandSource) - 修复了不对称初始化
# ==========================================
class ExplorerDemand(DemandSource):
    def __init__(self):
        # 严格遵守报错提示：不传 product
        super().__init__(type=None)
        
    def generate_demand(self, *args, **kwargs):
        print(f"\n[探针触发] 🎯 DemandSource.generate_demand()")
        print(f"   -> 传入位置参数 (args): {args}")
        print(f"   -> 传入命名参数 (kwargs): {kwargs}")
        # 先试探性返回单一数值，看引擎是否会报错或自动应用到所有产品
        return 10.0

# ==========================================
# 探针 2：订货策略 (Policy)
# ==========================================
class ExplorerPolicy(Policy):
    def __init__(self, product):
        # Policy 是绑定产品的，必须传
        super().__init__(type='CUSTOM', product=product)
        
    def validate_parameters(self): pass
        
    def get_order_quantity(self, *args, **kwargs):
        print(f"\n[探针触发] 📦 Policy.get_order_quantity() (绑定的产品: {self.product})")
        print(f"   -> 传入位置参数 (args): {args}")
        print(f"   -> 传入命名参数 (kwargs): {kwargs}")
        return {None: {self.product: 15.0}}

# ==========================================
# 探针 3 & 4：成本函数
# ==========================================
def explorer_holding_cost(*args, **kwargs):
    print("\n[探针触发] 💰 local_holding_cost_function()")
    print(f"   -> 传入位置参数 (args): {args}")
    print(f"   -> 传入命名参数 (kwargs): {kwargs}")
    return 5.0

def explorer_stockout_cost(*args, **kwargs):
    print("\n[探针触发] 🚨 stockout_cost_function()")
    print(f"   -> 传入位置参数 (args): {args}")
    print(f"   -> 传入命名参数 (kwargs): {kwargs}")
    return 10.0

# 挂载组件
node.demand_source = ExplorerDemand()
# 我们给 Policy 绑定产品 1
node.inventory_policy = ExplorerPolicy(product=1)
node.local_holding_cost_function = explorer_holding_cost
node.stockout_cost_function = explorer_stockout_cost

network.add_node(node)

# ==========================================
# 执行试探并捕获
# ==========================================
try:
    print("\n>>> 开始初始化引擎 (initialize) ...")
    initialize(network=network, num_periods=2, rand_seed=42)
    
    print("\n>>> 开始推进第一天 (step) ...")
    step(network=network, consistency_checks=False)
    
    print("\n✅ 探针运行结束，未发生崩溃！我们成功摸清了所有参数签名！")
    
except Exception as e:
    print(f"\n❌ 引擎运行中途崩溃！但我们应该已经捕获到了一些 Hook 打印：")
    traceback.print_exc(limit=4, file=sys.stdout)