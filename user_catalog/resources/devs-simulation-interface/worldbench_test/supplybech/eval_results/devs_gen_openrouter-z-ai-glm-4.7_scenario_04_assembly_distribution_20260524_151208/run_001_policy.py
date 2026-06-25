
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer层补货策略 - Order-up-to (Base Stock) Policy
    
    参数分析 (基于任务要求):
    - Product_X (product_id=4): base=15, noise=±5, seasonal_amp=6, period=21天
    - Product_Y (product_id=5): base=10, noise=±4, seasonal_amp=4, period=14天
    - 从DC补货提前期: 1天
    - 持有成本: 2.0/件/天, 缺货成本: 80.0/件
    - 初始库存: X=50, Y=40
    """
    # 基础日需求 (每个零售商)
    base_demand = {4: 15.0, 5: 10.0}
    
    # 季节性周期
    cycle_x = 21.0
    cycle_y = 14.0
    
    # 季节性振幅
    amp_x = 6.0
    amp_y = 4.0
    
    # 季节性因子 (sin函数)
    seasonal_factor_x = math.sin(2 * math.pi * period / cycle_x)
    seasonal_factor_y = math.sin(2 * math.pi * period / cycle_y)
    
    # 季节性调整后的期望需求
    seasonal_demand = {
        4: base_demand[4] + amp_x * seasonal_factor_x,
        5: base_demand[5] + amp_y * seasonal_factor_y
    }
    
    # 提前期
    lead_time = 1
    
    # 需求波动 (噪声标准差近似: noise/√3)
    daily_std_x = 5.0 / math.sqrt(3)
    daily_std_y = 4.0 / math.sqrt(3)
    
    # 提前期内需求标准差
    leadtime_std_x = daily_std_x * math.sqrt(lead_time)
    leadtime_std_y = daily_std_y * math.sqrt(lead_time)
    
    # 临界比率 (用于服务水平计算)
    # 零售层缺货成本极高 (80.0)，需要高服务水平
    critical_ratio = 80.0 / (2.0 + 80.0)  # ≈ 0.976
    
    # 安全库存 (基于临界比率的正态分布分位数)
    # critical_ratio ≈ 0.976 对应 z ≈ 2.0
    safety_factor = 2.0
    safety_stock = {
        4: leadtime_std_x * safety_factor,
        5: leadtime_std_y * safety_factor
    }
    
    # Order-up-to level = 期望需求(提前期+审查期) + 安全库存
    # 审查期=1天(每日决策)
    order_up_to = {
        4: max(0, seasonal_demand[4] * (lead_time + 1) + safety_stock[4]),
        5: max(0, seasonal_demand[5] * (lead_time + 1) + safety_stock[5])
    }
    
    # 当前库存位置 (从inventory_dict获取)
    inv_pos_4 = inventory_dict.get(4, 0.0)
    inv_pos_5 = inventory_dict.get(5, 0.0)
    
    # 订货量 = Order-up-to - 当前库存位置
    order_4 = max(0.0, order_up_to[4] - inv_pos_4)
    order_5 = max(0.0, order_up_to[5] - inv_pos_5)
    
    # 构建返回的订单字典
    orders = {}
    if order_4 > 0.01:
        orders[4] = order_4
    if order_5 > 0.01:
        orders[5] = order_5
    
    if orders:
        return {"DC_0": orders}
    
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC层补货策略 - Order-up-to Policy with Demand Aggregation
    
    参数分析 (基于任务要求):
    - 服务3个零售商, 总日需求约为单零售商的3倍
    - Product_X: 总日均约45 (15*3)
    - Product_Y: 总日均约30 (10*3)
    - 从Assembler补货提前期: 2天
    - 持有成本: 0.5/件/天, 缺货成本: 30.0/件
    - 初始库存: X=200, Y=150
    """
    # 3个零售商的总基础日需求
    base_demand = {4: 45.0, 5: 30.0}
    
    # 季节性周期
    cycle_x = 21.0
    cycle_y = 14.0
    
    # 季节性振幅 (3个零售商的总振幅)
    amp_x = 6.0 * 3  # 每个零售商6, 3个零售商
    amp_y = 4.0 * 3  # 每个零售商4, 3个零售商
    
    # 季节性因子
    seasonal_factor_x = math.sin(2 * math.pi * period / cycle_x)
    seasonal_factor_y = math.sin(2 * math.pi * period / cycle_y)
    
    # 季节性调整后的期望需求
    seasonal_demand = {
        4: base_demand[4] + amp_x * seasonal_factor_x,
        5: base_demand[5] + amp_y * seasonal_factor_y
    }
    
    # 提前期
    lead_time = 2
    
    # 需求波动 (3个独立均匀分布之和的标准差)
    daily_std_x = (5.0 * 3) / math.sqrt(3)  # 3个零售商的噪声之和
    daily_std_y = (4.0 * 3) / math.sqrt(3)
    
    # 提前期内需求标准差
    leadtime_std_x = daily_std_x * math.sqrt(lead_time)
    leadtime_std_y = daily_std_y * math.sqrt(lead_time)
    
    # 临界比率
    critical_ratio = 30.0 / (0.5 + 30.0)  # ≈ 0.984
    safety_factor = 2.2  # 更高的安全因子
    
    safety_stock = {
        4: leadtime_std_x * safety_factor,
        5: leadtime_std_y * safety_factor
    }
    
    # Order-up-to level
    order_up_to = {
        4: max(0, seasonal_demand[4] * (lead_time + 1) + safety_stock[4]),
        5: max(0, seasonal_demand[5] * (lead_time + 1) + safety_stock[5])
    }
    
    # 当前库存位置
    inv_pos_4 = inventory_dict.get(4, 0.0)
    inv_pos_5 = inventory_dict.get(5, 0.0)
    
    # 订货量
    order_4 = max(0.0, order_up_to[4] - inv_pos_4)
    order_5 = max(0.0, order_up_to[5] - inv_pos_5)
    
    orders = {}
    if order_4 > 0.01:
        orders[4] = order_4
    if order_5 > 0.01:
        orders[5] = order_5
    
    if orders:
        return {"Assembler_0": orders}
    
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler层补货策略 - Raw Material Ordering based on BOM
    
    参数分析 (基于任务要求):
    - 需要根据成品需求计算原材料需求
    - BOM: 
      * Product_X (4) 需要 1×Raw_A (1) + 1×Raw_C (3)
      * Product_Y (5) 需要 2×Raw_B (2) + 1×Raw_C (3)
    - Raw_C是共享资源
    - 从供应商补货提前期: 2天
    - 持有成本: 0.3/件/天, 缺货成本: 20.0/件
    - 初始库存: Raw_A=500, Raw_B=500, Raw_C=500, X=100, Y=80
    """
    # 3个零售商汇总到DC的日需求 (成品)
    base_demand_fg = {4: 45.0, 5: 30.0}
    
    # 季节性因子
    cycle_x = 21.0
    cycle_y = 14.0
    seasonal_factor_x = math.sin(2 * math.pi * period / cycle_x)
    seasonal_factor_y = math.sin(2 * math.pi * period / cycle_y)
    
    # 季节性调整后的成品需求
    seasonal_demand_fg = {
        4: base_demand_fg[4] + 18.0 * seasonal_factor_x,  # 6*3
        5: base_demand_fg[5] + 12.0 * seasonal_factor_y   # 4*3
    }
    
    # 原材料日需求 (基于BOM)
    raw_demand = {
        1: seasonal_demand_fg[4] * 1.0,  # Raw_A: 仅用于Product_X
        2: seasonal_demand_fg[5] * 2.0,  # Raw_B: 用于Product_Y, 2倍
        3: seasonal_demand_fg[4] * 1.0 + seasonal_demand_fg[5] * 1.0  # Raw_C: 共享
    }
    
    # 提前期
    lead_time = 2
    
    # 需求波动估算
    daily_std_raw_a = (5.0 * 3) / math.sqrt(3)  # 基于Product_X的波动
    daily_std_raw_b = (4.0 * 3) / math.sqrt(3) * 2  # 基于Product_Y的波动, 2倍
    daily_std_raw_c = daily_std_raw_a + daily_std_raw_b  # 共享资源
    
    leadtime_std = {
        1: daily_std_raw_a * math.sqrt(lead_time),
        2: daily_std_raw_b * math.sqrt(lead_time),
        3: daily_std_raw_c * math.sqrt(lead_time)
    }
    
    # 临界比率
    critical_ratio = 20.0 / (0.3 + 20.0)  # ≈ 0.985
    safety_factor = 2.3
    
    # 安全库存 (共享资源Raw_C需要更多缓冲)
    safety_stock = {
        1: leadtime_std[1] * safety_factor,
        2: leadtime_std[2] * safety_factor,
        3: leadtime_std[3] * safety_factor * 1.5  # 共享资源的额外安全因子
    }
    
    # Order-up-to level for raw materials
    order_up_to = {
        1: max(0, raw_demand[1] * (lead_time + 1) + safety_stock[1]),
        2: max(0, raw_demand[2] * (lead_time + 1) + safety_stock[2]),
        3: max(0, raw_demand[3] * (lead_time + 1) + safety_stock[3])
    }
    
    # 当前原材料库存位置 (从inventory_dict获取)
    inv_pos_1 = inventory_dict.get(1, 0.0)
    inv_pos_2 = inventory_dict.get(2, 0.0)
    inv_pos_3 = inventory_dict.get(3, 0.0)
    
    # 订货量
    order_1 = max(0.0, order_up_to[1] - inv_pos_1)
    order_2 = max(0.0, order_up_to[2] - inv_pos_2)
    order_3 = max(0.0, order_up_to[3] - inv_pos_3)
    
    # 构建订单字典
    orders = {}
    if order_1 > 0.01:
        orders["Supplier_A_0"] = {1: order_1}
    if order_2 > 0.01:
        orders["Supplier_B_0"] = {2: order_2}
    if order_3 > 0.01:
        orders["Supplier_C_0"] = {3: order_3}
    
    return orders


# Policy挂载声明
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
