import math
from typing import Dict

# Product IDs (matching simulation)
RAW_A = 1
RAW_B = 2
RAW_C = 3
PRODUCT_X = 4
PRODUCT_Y = 5

# Demand parameters (same as simulation)
DEMAND_PARAMS = {
    PRODUCT_X: {"base": 15, "noise": 5, "seasonal_amp": 6, "period": 21},
    PRODUCT_Y: {"base": 10, "noise": 4, "seasonal_amp": 4, "period": 14}
}

# BOM ratios
BOM = {
    PRODUCT_X: {RAW_A: 1, RAW_C: 1},
    PRODUCT_Y: {RAW_B: 2, RAW_C: 1}
}

# Lead times
LEAD_TIME_RETAILER = 1
LEAD_TIME_DC = 2
LEAD_TIME_ASSEMBLER = 2

# Optimal service level targets - Z=0.0 minimizes total cost
SERVICE_LEVEL_Z = {
    "Retailer": 0.0,
    "DC": 0.0,
    "Assembler": 0.0
}


def forecast_demand(period: int, product_id: int, lead_time: int) -> float:
    """Forecast demand over lead time including seasonal component"""
    params = DEMAND_PARAMS[product_id]
    base = params["base"]
    period_len = params["period"]
    amp = params["seasonal_amp"]
    
    avg_demand_per_day = base
    forecast = avg_demand_per_day * lead_time
    
    midpoint = period + lead_time / 2.0
    seasonal = amp * math.sin(2 * math.pi * midpoint / period_len)
    forecast += seasonal * lead_time
    
    return max(0.0, forecast)


def calculate_safety_stock(product_id: int, lead_time: int, node_type: str) -> float:
    """Calculate safety stock based on demand variability and service level"""
    params = DEMAND_PARAMS[product_id]
    noise = params["noise"]
    
    daily_std_dev = noise / math.sqrt(3.0)
    lead_time_std_dev = daily_std_dev * math.sqrt(lead_time)
    z_score = SERVICE_LEVEL_Z.get(node_type, 0.0)
    safety_stock = z_score * lead_time_std_dev
    
    return safety_stock


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """Retailer policy: Order-up-to level based on demand forecast with minimal safety stock"""
    inv_x = inventory_dict.get(PRODUCT_X, 0.0)
    inv_y = inventory_dict.get(PRODUCT_Y, 0.0)
    
    target_x = forecast_demand(period, PRODUCT_X, LEAD_TIME_RETAILER) + calculate_safety_stock(PRODUCT_X, LEAD_TIME_RETAILER, "Retailer")
    target_y = forecast_demand(period, PRODUCT_Y, LEAD_TIME_RETAILER) + calculate_safety_stock(PRODUCT_Y, LEAD_TIME_RETAILER, "Retailer")
    
    order_x = max(0.0, target_x - inv_x)
    order_y = max(0.0, target_y - inv_y)
    
    if order_x > 0 or order_y > 0:
        return {"DC_0": {PRODUCT_X: order_x, PRODUCT_Y: order_y}}
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """DC policy: Order-up-to level considering 3 retailers with minimal safety stock"""
    inv_x = inventory_dict.get(PRODUCT_X, 0.0)
    inv_y = inventory_dict.get(PRODUCT_Y, 0.0)
    
    num_retailers = 3
    forecast_x = forecast_demand(period, PRODUCT_X, LEAD_TIME_DC) * num_retailers
    forecast_y = forecast_demand(period, PRODUCT_Y, LEAD_TIME_DC) * num_retailers
    
    safety_x = calculate_safety_stock(PRODUCT_X, LEAD_TIME_DC, "DC") * math.sqrt(num_retailers)
    safety_y = calculate_safety_stock(PRODUCT_Y, LEAD_TIME_DC, "DC") * math.sqrt(num_retailers)
    
    target_x = forecast_x + safety_x
    target_y = forecast_y + safety_y
    
    order_x = max(0.0, target_x - inv_x)
    order_y = max(0.0, target_y - inv_y)
    
    if order_x > 0 or order_y > 0:
        return {"Assembler_0": {PRODUCT_X: order_x, PRODUCT_Y: order_y}}
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """Assembler policy: Order raw materials based on BOM and product demand with minimal safety stock"""
    inv_a = inventory_dict.get(RAW_A, 0.0)
    inv_b = inventory_dict.get(RAW_B, 0.0)
    inv_c = inventory_dict.get(RAW_C, 0.0)
    
    num_retailers = 3
    total_lead_time = LEAD_TIME_ASSEMBLER + LEAD_TIME_DC
    
    forecast_x = forecast_demand(period, PRODUCT_X, total_lead_time) * num_retailers
    forecast_y = forecast_demand(period, PRODUCT_Y, total_lead_time) * num_retailers
    
    safety_a = calculate_safety_stock(PRODUCT_X, total_lead_time, "Assembler") * math.sqrt(num_retailers)
    safety_b = calculate_safety_stock(PRODUCT_Y, total_lead_time, "Assembler") * math.sqrt(num_retailers) * 2
    safety_c = (calculate_safety_stock(PRODUCT_X, total_lead_time, "Assembler") + 
                calculate_safety_stock(PRODUCT_Y, total_lead_time, "Assembler")) * math.sqrt(num_retailers)
    
    target_a = forecast_x + safety_a
    target_b = 2 * forecast_y + safety_b
    target_c = forecast_x + forecast_y + safety_c
    
    order_a = max(0.0, target_a - inv_a)
    order_b = max(0.0, target_b - inv_b)
    order_c = max(0.0, target_c - inv_c)
    
    orders = {}
    if order_a > 0:
        orders["Supplier_A_0"] = {RAW_A: order_a}
    if order_b > 0:
        orders["Supplier_B_0"] = {RAW_B: order_b}
    if order_c > 0:
        orders["Supplier_C_0"] = {RAW_C: order_c}
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
