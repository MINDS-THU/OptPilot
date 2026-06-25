import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    多产品动态补货策略，基于季节性预测的(s,S)策略
    
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照 {product_id: inventory_position}
    
    Returns:
        订货指令字典
    """
    
    # ============================================================
    # 产品参数配置
    # ============================================================
    PRODUCTS = {
        1: {  # Product_A
            "base": 18.0,
            "noise": 6.0,
            "seasonal_amp": 8.0,
            "period": 21.0,
            "holding_cost": 2.0,
            "stockout_cost": 80.0,
        },
        2: {  # Product_B
            "base": 12.0,
            "noise": 4.0,
            "seasonal_amp": 5.0,
            "period": 14.0,
            "holding_cost": 2.0,
            "stockout_cost": 80.0,
        },
    }
    
    LEAD_TIME = 2          # Retailer -> DC 提前期（天）
    REVIEW_PERIOD = 1      # 每天检查一次
    
    # ============================================================
    # 辅助函数：预测未来 t 天的季节性需求
    # ============================================================
    def predict_demand(product_id: int, start_day: int, horizon: int) -> float:
        """
        预测从 start_day 开始的 horizon 天累计需求（单个零售商）
        使用正弦季节性模型
        """
        params = PRODUCTS[product_id]
        base = params["base"]
        amp = params["seasonal_amp"]
        period = params["period"]
        
        total = 0.0
        for d in range(horizon):
            day = start_day + d
            # 季节性因子（与仿真中的需求生成保持一致）
            seasonal = amp * math.sin(2 * math.pi * day / period)
            daily_demand = max(0.0, base + seasonal)
            total += daily_demand
        return total
    
    def predict_daily_demand(product_id: int, day: int) -> float:
        """预测某天的日需求"""
        params = PRODUCTS[product_id]
        base = params["base"]
        amp = params["seasonal_amp"]
        period = params["period"]
        seasonal = amp * math.sin(2 * math.pi * day / period)
        return max(0.0, base + seasonal)
    
    # ============================================================
    # 辅助函数：计算安全库存
    # ============================================================
    def compute_safety_stock(product_id: int, current_day: int) -> float:
        """
        动态安全库存计算
        基于需求标准差和提前期，考虑季节性调整
        """
        params = PRODUCTS[product_id]
        noise = params["noise"]
        stockout_cost = params["stockout_cost"]
        holding_cost = params["holding_cost"]
        
        # 服务水平 z 值：基于成本比率
        # critical_ratio = stockout_cost / (stockout_cost + holding_cost)
        # 80/(80+2) ≈ 0.976 -> z ≈ 2.0
        # 使用稍保守的值以平衡持有成本
        z_score = 1.8  # 对应约96%服务水平
        
        # 提前期内的需求标准差（假设每天独立）
        sigma_lead = noise * math.sqrt(LEAD_TIME + REVIEW_PERIOD)
        
        # 基础安全库存
        base_safety = z_score * sigma_lead
        
        # 季节性调整：在季节性高峰前增加安全库存
        # 预测未来几天的季节性趋势
        future_demand = predict_daily_demand(product_id, current_day + LEAD_TIME)
        avg_demand = params["base"]
        
        # 季节性比率：高峰期增加安全库存
        seasonal_ratio = future_demand / avg_demand if avg_demand > 0 else 1.0
        seasonal_ratio = max(0.7, min(1.5, seasonal_ratio))  # 限制调整范围
        
        adjusted_safety = base_safety * seasonal_ratio
        
        return adjusted_safety
    
    # ============================================================
    # 辅助函数：计算再订货点和目标库存
    # ============================================================
    def compute_reorder_point(product_id: int, current_day: int) -> float:
        """
        再订货点 s = 提前期内预测需求 + 安全库存
        """
        # 提前期内的预测需求（从明天开始，覆盖 lead_time 天）
        lead_demand = predict_demand(product_id, current_day + 1, LEAD_TIME)
        
        # 安全库存
        safety_stock = compute_safety_stock(product_id, current_day)
        
        reorder_point = lead_demand + safety_stock
        return reorder_point
    
    def compute_target_inventory(product_id: int, current_day: int) -> float:
        """
        目标库存 S = 再订货点 + review period 内的预测需求
        覆盖 lead_time + review_period 天的需求 + 安全库存
        """
        params = PRODUCTS[product_id]
        
        # 覆盖 lead_time + review_period 天的总需求
        total_horizon = LEAD_TIME + REVIEW_PERIOD
        total_demand = predict_demand(product_id, current_day + 1, total_horizon)
        
        # 安全库存
        safety_stock = compute_safety_stock(product_id, current_day)
        
        # 目标库存
        target = total_demand + safety_stock
        
        # 额外的前瞻性调整：如果即将进入高峰期，提前备货
        # 查看未来 3 天的需求趋势
        near_future_demand = predict_daily_demand(product_id, current_day + LEAD_TIME + 2)
        current_demand = predict_daily_demand(product_id, current_day)
        
        if current_demand > 0:
            trend_ratio = near_future_demand / current_demand
        else:
            trend_ratio = 1.0
        
        # 如果需求上升趋势明显，增加目标库存
        if trend_ratio > 1.15:
            # 额外备货：预测高峰期额外需求
            extra_days = 2
            extra_demand = predict_demand(
                product_id, 
                current_day + total_horizon + 1, 
                extra_days
            ) * (trend_ratio - 1.0)
            target += extra_demand
        
        return target
    
    # ============================================================
    # 主策略逻辑：为每个产品计算订货量
    # ============================================================
    orders = {}
    
    for product_id in [1, 2]:
        # 获取当前库存位置
        current_ip = inventory_dict.get(product_id, 0.0)
        
        # 计算再订货点
        reorder_point = compute_reorder_point(product_id, period)
        
        # 计算目标库存
        target_inventory = compute_target_inventory(product_id, period)
        
        # (s, S) 策略：当库存位置低于再订货点时，补货至目标库存
        if current_ip < reorder_point:
            order_qty = target_inventory - current_ip
            order_qty = max(0.0, order_qty)
            
            # 最小订货量约束（避免频繁小额订货）
            min_order = predict_daily_demand(product_id, period) * 0.5
            if order_qty < min_order and order_qty > 0:
                order_qty = min_order
            
            if order_qty > 0:
                orders[product_id] = round(order_qty, 1)
        
        # 即使库存位置高于再订货点，如果即将进入高峰期也考虑预防性补货
        else:
            # 检查未来 LEAD_TIME 天后的需求是否显著高于当前
            future_peak = predict_daily_demand(product_id, period + LEAD_TIME)
            current_level = predict_daily_demand(product_id, period)
            
            # 如果高峰即将来临且库存不够充裕
            if future_peak > current_level * 1.2:
                # 计算高峰期额外需求
                extra_buffer = (future_peak - current_level) * LEAD_TIME * 0.5
                if current_ip < reorder_point + extra_buffer:
                    order_qty = target_inventory + extra_buffer - current_ip
                    order_qty = max(0.0, order_qty)
                    if order_qty > 0:
                        orders[product_id] = round(order_qty, 1)
    
    # 如果有订货，返回订货指令
    if orders:
        return {"DC_0": orders}
    else:
        return {}


# ============================================================
# 策略挂载声明
# ============================================================
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}