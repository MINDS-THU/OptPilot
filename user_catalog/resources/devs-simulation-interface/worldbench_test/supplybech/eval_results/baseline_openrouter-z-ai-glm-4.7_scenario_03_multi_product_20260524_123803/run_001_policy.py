import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    零售商多产品补货策略。
    采用基础库存策略，结合季节性需求预测和安全库存计算。
    """
    
    # --- 策略参数配置 ---
    lead_time = 2  # 从DC补货的提前期
    
    # 服务水平因子 Z
    # 缺货成本 Cu = 80, 仓储成本 Co = 2
    # 临界比率 = Cu / (Cu + Co) = 80 / 82 ≈ 0.9756
    # 对应正态分布分位数 Z ≈ 1.96
    z_score = 1.96
    
    # 产品配置
    # sigma 估算：假设噪声范围覆盖约4个标准差 (Range / 4)
    products = {
        1: { # Product_A
            'base': 18, 
            'amp': 8, 
            'period': 21, 
            'sigma': 6.0 / 4.0  # noise +/- 6 -> range 12 -> sigma ~ 3.0
        },
        2: { # Product_B
            'base': 12, 
            'amp': 5, 
            'period': 14, 
            'sigma': 4.0 / 4.0  # noise +/- 4 -> range 8 -> sigma ~ 2.0
        }
    }
    
    orders = {}
    
    for pid, config in products.items():
        # 获取当前库存位置
        # inventory_dict 包含：实物库存 + 在途订单 - 欠单
        current_pos = inventory_dict.get(pid, 0.0)
        
        # 1. 预测提前期内的需求
        forecast_demand_lt = 0
        for day_ahead in range(1, lead_time + 1):
            t = period + day_ahead
            # 季节性需求模型: Base + Amp * sin(2*pi*t/Period)
            seasonal_factor = math.sin(2 * math.pi * t / config['period'])
            daily_demand = config['base'] + config['amp'] * seasonal_factor
            forecast_demand_lt += daily_demand
            
        # 2. 计算安全库存
        # SS = Z * sigma_daily * sqrt(LeadTime)
        safety_stock = z_score * config['sigma'] * math.sqrt(lead_time)
        
        # 3. 计算目标库存水平
        target_level = forecast_demand_lt + safety_stock
        
        # 4. 计算订货量
        order_qty = target_level - current_pos
        
        # 订货量必须 >= 0
        if order_qty > 0:
            orders[pid] = order_qty
            
    # 返回订货指令
    if orders:
        return {"DC_0": orders}
    else:
        return {}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}