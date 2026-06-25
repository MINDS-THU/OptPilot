import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    零售商补货策略：Order-Up-To (订货至上) 策略
    结合季节性因子和安全库存来动态调整目标库存水平。
    """
    # 获取当前产品库存位置 (on-hand + in-transit - backorders)
    # 假设只有一个产品 product_id=1
    current_inventory = inventory_dict.get(1, 0.0)
    
    # --- 参数设置 ---
    base_demand = 25.0       # 平均日需求
    lead_time = 2            # 补货提前期 (天)
    safety_stock = 20.0      # 安全库存 (应对随机波动)
    seasonality_amp = 5.0    # 季节性振幅
    cycle_length = 14.0      # 季节性周期 (天)
    
    # --- 计算季节性因子 ---
    # 使用正弦波模拟季节性: sin(2 * pi * period / cycle)
    seasonality_factor = seasonality_amp * math.sin(2 * math.pi * period / cycle_length)
    
    # --- 计算目标库存 ---
    # 预测提前期内的需求 = (基础需求 + 季节性波动) * 提前期
    forecast_demand_lt = (base_demand + seasonality_factor) * lead_time
    
    # 目标库存 = 预测需求 + 安全库存
    target_inventory = forecast_demand_lt + safety_stock
    
    # --- 计算订货量 ---
    order_quantity = target_inventory - current_inventory
    
    # 订货量不能为负
    if order_quantity < 0:
        order_quantity = 0.0
        
    # --- 返回订货指令 ---
    # 注意：由于函数签名未提供当前节点ID，无法动态判断是 Regional_DC_0 还是 Regional_DC_1。
    # 在实际仿真中，通常框架会根据拓扑自动路由，或者需要传入 node_id 参数。
    # 这里为了代码完整性，默认返回 Regional_DC_0，实际应用需根据节点ID修改。
    # 假设：如果节点ID包含 0,1,2 则发往 DC_0，否则发往 DC_1。
    # 由于无法获取ID，此处仅做逻辑演示。
    
    return {"Regional_DC_0": {1: order_quantity}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    区域仓库补货策略：Order-Up-To (订货至上) 策略
    考虑到下游有3个零售商，需求是零售商的3倍，且需要平滑牛鞭效应。
    """
    # 获取当前产品库存位置
    current_inventory = inventory_dict.get(1, 0.0)
    
    # --- 参数设置 ---
    # DC 服务于 3 个零售商，每个平均 25，故基础需求为 75
    base_demand = 75.0       
    lead_time = 4            # 补货提前期 (天)
    safety_stock = 60.0      # 安全库存 (DC 层级需要更多缓冲以应对聚合风险)
    seasonality_amp = 15.0   # 季节性振幅 (3个零售商叠加: 5 * 3)
    cycle_length = 14.0      # 季节性周期 (天)
    
    # --- 计算季节性因子 ---
    seasonality_factor = seasonality_amp * math.sin(2 * math.pi * period / cycle_length)
    
    # --- 计算目标库存 ---
    forecast_demand_lt = (base_demand + seasonality_factor) * lead_time
    target_inventory = forecast_demand_lt + safety_stock
    
    # --- 计算订货量 ---
    order_quantity = target_inventory - current_inventory
    
    if order_quantity < 0:
        order_quantity = 0.0
        
    # --- 返回订货指令 ---
    # DC 只能向 Factory_0 订货
    return {"Factory_0": {1: order_quantity}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}