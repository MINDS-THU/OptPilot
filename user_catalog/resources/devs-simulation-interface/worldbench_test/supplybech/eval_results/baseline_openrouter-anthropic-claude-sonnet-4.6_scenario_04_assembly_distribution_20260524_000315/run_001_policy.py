import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer层补货策略：(s, S) 策略
    提前期 = 1天
    Product_X (4): base=15, noise=±5, seasonal_amp=6, period=21天
    Product_Y (5): base=10, noise=±4, seasonal_amp=4, period=14天
    """
    # 季节性因子计算
    seasonal_x = 6 * math.sin(2 * math.pi * period / 21)
    seasonal_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # 当前期望日需求（含季节性）
    demand_x = max(10, 15 + seasonal_x)  # 最低10
    demand_y = max(6, 10 + seasonal_y)   # 最低6
    
    # 提前期 = 1天
    lead_time = 1
    
    # 安全系数（缺货成本80元 vs 仓储成本2元，高缺货惩罚，需要较高安全库存）
    # 服务水平 ≈ 1 - holding/stockout = 1 - 2/80 = 97.5%，对应z≈1.96
    safety_factor = 2.0
    
    # 标准差估计（noise + seasonal波动）
    std_x = 5.0  # uniform(-5,5) std ≈ 5/√3 ≈ 2.9, 加季节性约4
    std_y = 4.0  # uniform(-4,4) std ≈ 4/√3 ≈ 2.3, 加季节性约3
    
    # 目标库存 = 提前期需求 + 安全库存 + 当天需求缓冲
    # 覆盖 lead_time+1 天的需求（订货到达前还需消耗1天）
    review_period = 1  # 每天检查
    
    target_x = (lead_time + review_period) * demand_x + safety_factor * std_x * math.sqrt(lead_time + review_period)
    target_y = (lead_time + review_period) * demand_y + safety_factor * std_y * math.sqrt(lead_time + review_period)
    
    # 再订货点（低于此水平时触发订货）
    reorder_x = lead_time * demand_x + safety_factor * std_x * math.sqrt(lead_time)
    reorder_y = lead_time * demand_y + safety_factor * std_y * math.sqrt(lead_time)
    
    orders = {}
    order_to_dc = {}
    
    inv_x = inventory_dict.get(4, 0)
    inv_y = inventory_dict.get(5, 0)
    
    # 如果库存位置低于再订货点，补货至目标水平
    if inv_x < reorder_x:
        qty_x = max(0, target_x - inv_x)
        order_to_dc[4] = round(qty_x)
    
    if inv_y < reorder_y:
        qty_y = max(0, target_y - inv_y)
        order_to_dc[5] = round(qty_y)
    
    if order_to_dc:
        orders["DC_0"] = order_to_dc
    
    return orders


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC层补货策略：(s, S) 策略
    提前期 = 2天（从Assembler）
    服务3个零售商
    """
    # 季节性因子
    seasonal_x = 6 * math.sin(2 * math.pi * period / 21)
    seasonal_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # 3个零售商的合计日需求
    n_retailers = 3
    demand_x = max(30, n_retailers * (15 + seasonal_x))
    demand_y = max(18, n_retailers * (10 + seasonal_y))
    
    # 提前期 = 2天
    lead_time = 2
    review_period = 1
    
    # 安全系数（缺货成本30元 vs 仓储成本0.5元）
    # 服务水平 ≈ 1 - 0.5/30 ≈ 98.3%，对应z≈2.1
    safety_factor = 2.1
    
    # 合计标准差（3个独立零售商）
    std_x_single = 5.0
    std_y_single = 4.0
    std_x = math.sqrt(n_retailers) * std_x_single  # ≈ 8.66
    std_y = math.sqrt(n_retailers) * std_y_single  # ≈ 6.93
    
    # 目标库存
    target_x = (lead_time + review_period) * demand_x + safety_factor * std_x * math.sqrt(lead_time + review_period)
    target_y = (lead_time + review_period) * demand_y + safety_factor * std_y * math.sqrt(lead_time + review_period)
    
    # 再订货点
    reorder_x = lead_time * demand_x + safety_factor * std_x * math.sqrt(lead_time)
    reorder_y = lead_time * demand_y + safety_factor * std_y * math.sqrt(lead_time)
    
    orders = {}
    order_to_assembler = {}
    
    inv_x = inventory_dict.get(4, 0)
    inv_y = inventory_dict.get(5, 0)
    
    if inv_x < reorder_x:
        qty_x = max(0, target_x - inv_x)
        order_to_assembler[4] = round(qty_x)
    
    if inv_y < reorder_y:
        qty_y = max(0, target_y - inv_y)
        order_to_assembler[5] = round(qty_y)
    
    if order_to_assembler:
        orders["Assembler_0"] = order_to_assembler
    
    return orders


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler层补货策略：(s, S) 策略
    提前期 = 2天（从供应商）
    
    BOM:
    - Product_X (4): 1×Raw_A (1) + 1×Raw_C (3)
    - Product_Y (5): 2×Raw_B (2) + 1×Raw_C (3)
    
    Raw_C 是共享原材料
    """
    # 季节性因子
    seasonal_x = 6 * math.sin(2 * math.pi * period / 21)
    seasonal_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # 3个零售商的合计日需求（Assembler需要满足整个下游）
    n_retailers = 3
    demand_x = max(30, n_retailers * (15 + seasonal_x))  # Product_X日需求
    demand_y = max(18, n_retailers * (10 + seasonal_y))  # Product_Y日需求
    
    # 原材料日需求（根据BOM）
    # Raw_A: 1×demand_x
    # Raw_B: 2×demand_y
    # Raw_C: 1×demand_x + 1×demand_y (共享)
    demand_a = demand_x
    demand_b = 2 * demand_y
    demand_c = demand_x + demand_y  # 共享原材料
    
    # 提前期 = 2天（从供应商）
    # Assembler还需要额外缓冲：DC提前期2天 + 零售商提前期1天 = 3天的下游提前期
    # 总覆盖期 = 供应商提前期 + 安全缓冲
    lead_time = 2
    review_period = 1
    
    # 安全系数（缺货成本20元 vs 仓储成本0.3元）
    # 服务水平 ≈ 1 - 0.3/20 ≈ 98.5%，对应z≈2.17
    safety_factor = 2.2
    
    # 标准差（考虑3个零售商的需求波动）
    std_x = math.sqrt(n_retailers) * 5.0  # ≈ 8.66
    std_y = math.sqrt(n_retailers) * 4.0  # ≈ 6.93
    
    std_a = std_x
    std_b = 2 * std_y
    std_c = math.sqrt(std_x**2 + std_y**2)  # 独立需求的合并标准差
    
    # 目标库存（覆盖提前期+审查期）
    # 额外增加下游管道库存的缓冲（DC提前期2天 + 零售商提前期1天）
    pipeline_buffer = 3  # 下游总提前期
    
    target_a = (lead_time + review_period + pipeline_buffer) * demand_a + safety_factor * std_a * math.sqrt(lead_time + review_period)
    target_b = (lead_time + review_period + pipeline_buffer) * demand_b + safety_factor * std_b * math.sqrt(lead_time + review_period)
    target_c = (lead_time + review_period + pipeline_buffer) * demand_c + safety_factor * std_c * math.sqrt(lead_time + review_period)
    
    # 再订货点
    reorder_a = (lead_time + pipeline_buffer) * demand_a + safety_factor * std_a * math.sqrt(lead_time)
    reorder_b = (lead_time + pipeline_buffer) * demand_b + safety_factor * std_b * math.sqrt(lead_time)
    reorder_c = (lead_time + pipeline_buffer) * demand_c + safety_factor * std_c * math.sqrt(lead_time)
    
    # 当前库存位置
    inv_a = inventory_dict.get(1, 0)
    inv_b = inventory_dict.get(2, 0)
    inv_c = inventory_dict.get(3, 0)
    
    orders = {}
    
    # Raw_A 订货
    if inv_a < reorder_a:
        qty_a = max(0, target_a - inv_a)
        orders["Supplier_A_0"] = {1: round(qty_a)}
    
    # Raw_B 订货
    if inv_b < reorder_b:
        qty_b = max(0, target_b - inv_b)
        orders["Supplier_B_0"] = {2: round(qty_b)}
    
    # Raw_C 订货（共享原材料，需要满足X和Y的需求）
    if inv_c < reorder_c:
        qty_c = max(0, target_c - inv_c)
        orders["Supplier_C_0"] = {3: round(qty_c)}
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}