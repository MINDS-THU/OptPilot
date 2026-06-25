def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    基于确定性需求的精确补货策略
    
    需求模式: [30, 30, 30, 10, 10, 10] 6天循环
    Lead time: 1天 (零售商 -> 中央仓库)
    缺货成本: 100元/件 >> 仓储成本: 2.5元/件/天
    
    策略: 目标库存策略 (Order-Up-To Level)
    - 每天计算需要覆盖的未来需求
    - 补货至目标水平，避免缺货同时控制库存
    """
    
    # 确定性需求模式（6天循环）
    DEMAND_PATTERN = [30, 30, 30, 10, 10, 10]
    CYCLE_LENGTH = 6
    PRODUCT_ID = 1
    UPSTREAM = "Central_DC_0"
    
    # Lead time = 1天，今天订货明天到
    LEAD_TIME = 1
    
    # 覆盖天数 = lead time + 额外缓冲天数
    # 缺货成本极高，保留2天缓冲
    COVER_DAYS = LEAD_TIME + 2  # 覆盖未来3天需求
    
    # 获取当前库存位置（已包含在途订单）
    current_ip = inventory_dict.get(PRODUCT_ID, 0)
    
    # 计算从明天开始未来 COVER_DAYS 天的需求
    # period 从1开始，今天是 period
    # 今天的需求已经发生，我们关心明天起的需求
    future_demand = 0
    for d in range(1, COVER_DAYS + 1):
        # 未来第d天对应的周期位置
        future_day = period + d  # 从1开始的天数
        cycle_index = (future_day - 1) % CYCLE_LENGTH
        future_demand += DEMAND_PATTERN[cycle_index]
    
    # 目标库存水平 = 覆盖未来需求 + 安全缓冲
    # 安全缓冲：额外1天的平均需求（90/6=15件）
    avg_daily_demand = sum(DEMAND_PATTERN) / CYCLE_LENGTH  # = 15件/天
    safety_stock = avg_daily_demand * 1  # 1天平均需求作为安全库存
    
    target_ip = future_demand + safety_stock
    
    # 计算订货量
    order_qty = max(0, target_ip - current_ip)
    
    # 取整（实际业务中通常整件订货）
    order_qty = round(order_qty)
    
    if order_qty > 0:
        return {UPSTREAM: {PRODUCT_ID: float(order_qty)}}
    else:
        return {}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}