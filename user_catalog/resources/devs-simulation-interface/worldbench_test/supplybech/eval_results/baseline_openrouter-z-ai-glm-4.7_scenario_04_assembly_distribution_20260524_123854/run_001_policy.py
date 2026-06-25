import math

# Product IDs
RAW_A = 1
RAW_B = 2
RAW_C = 3
PRODUCT_X = 4
PRODUCT_Y = 5

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer Policy:
    - Uses an Order-Up-To (OUT) strategy.
    - Forecast includes seasonal component based on the period.
    - Lead time is 1 day. We cover lead time + 1 day safety buffer.
    """
    # 1. Forecast Demand (Base + Seasonality)
    # Product X: base=15, amp=6, period=21
    forecast_x = 15 + 6 * math.sin(2 * math.pi * period / 21)
    # Product Y: base=10, amp=4, period=14
    forecast_y = 10 + 4 * math.sin(2 * math.pi * period / 14)
    
    # 2. Calculate Target Inventory Position
    # Coverage = Lead Time (1) + Safety Buffer (1) = 2 days
    # Safety Stock covers max noise (approx 5 for X, 4 for Y)
    target_x = forecast_x * 2 + 5.0
    target_y = forecast_y * 2 + 4.0
    
    # 3. Calculate Order Quantity
    inv_x = inventory_dict.get(PRODUCT_X, 0.0)
    inv_y = inventory_dict.get(PRODUCT_Y, 0.0)
    
    order_x = max(0.0, target_x - inv_x)
    order_y = max(0.0, target_y - inv_y)
    
    return {"DC_0": {PRODUCT_X: order_x, PRODUCT_Y: order_y}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC Policy:
    - Aggregates demand for 3 retailers.
    - Uses OUT strategy with seasonal forecasting.
    - Lead time is 2 days. We cover lead time + 1 day buffer.
    """
    # 1. Forecast Demand (3 * Retailer Demand)
    # Product X
    forecast_x = 3 * (15 + 6 * math.sin(2 * math.pi * period / 21))
    # Product Y
    forecast_y = 3 * (10 + 4 * math.sin(2 * math.pi * period / 14))
    
    # 2. Calculate Target Inventory Position
    # Coverage = Lead Time (2) + Safety Buffer (1) = 3 days
    # Safety Stock: Higher variance due to aggregation (3 retailers)
    target_x = forecast_x * 3 + 30.0
    target_y = forecast_y * 3 + 24.0
    
    # 3. Calculate Order Quantity
    inv_x = inventory_dict.get(PRODUCT_X, 0.0)
    inv_y = inventory_dict.get(PRODUCT_Y, 0.0)
    
    order_x = max(0.0, target_x - inv_x)
    order_y = max(0.0, target_y - inv_y)
    
    return {"Assembler_0": {PRODUCT_X: order_x, PRODUCT_Y: order_y}}

def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler Policy:
    - Calculates raw material needs based on BOM and downstream demand.
    - Adjusts raw material orders based on current Finished Goods inventory (FG offset).
    - Uses OUT strategy for raw materials.
    """
    # 1. Forecast Demand (Demand from DC)
    demand_x = 3 * (15 + 6 * math.sin(2 * math.pi * period / 21))
    demand_y = 3 * (10 + 4 * math.sin(2 * math.pi * period / 14))
    
    # 2. Check Finished Goods Inventory
    inv_fg_x = inventory_dict.get(PRODUCT_X, 0.0)
    inv_fg_y = inventory_dict.get(PRODUCT_Y, 0.0)
    
    # Target FG Level (Lead Time 2 + Safety)
    target_fg_x = demand_x * 2 + 30.0
    target_fg_y = demand_y * 2 + 24.0
    
    # Calculate Excess FG to reduce raw material ordering
    excess_fg_x = max(0.0, inv_fg_x - target_fg_x)
    excess_fg_y = max(0.0, inv_fg_y - target_fg_y)
    
    # 3. Calculate Net Raw Material Requirements (BOM Explosion)
    # Product X: 1A + 1C
    # Product Y: 2B + 1C
    
    # Gross Requirements
    req_raw_a = demand_x
    req_raw_b = 2 * demand_y
    req_raw_c = demand_x + demand_y
    
    # Net Requirements (Gross - Excess FG)
    net_req_raw_a = max(0.0, req_raw_a - excess_fg_x)
    net_req_raw_b = max(0.0, req_raw_b - excess_fg_y)
    # Raw C is shared, so reduce by total excess FG (converted to C equivalent)
    net_req_raw_c = max(0.0, req_raw_c - (excess_fg_x + excess_fg_y))
    
    # 4. Calculate Target Raw Material Position
    # Lead Time for Raw Materials = 2 days. Coverage = 2 days.
    # Safety Stock for Raw Materials
    target_raw_a = net_req_raw_a * 2 + 30.0
    target_raw_b = net_req_raw_b * 2 + 48.0
    target_raw_c = net_req_raw_c * 2 + 54.0
    
    # 5. Calculate Order Quantity
    inv_raw_a = inventory_dict.get(RAW_A, 0.0)
    inv_raw_b = inventory_dict.get(RAW_B, 0.0)
    inv_raw_c = inventory_dict.get(RAW_C, 0.0)
    
    order_a = max(0.0, target_raw_a - inv_raw_a)
    order_b = max(0.0, target_raw_b - inv_raw_b)
    order_c = max(0.0, target_raw_c - inv_raw_c)
    
    return {
        "Supplier_A_0": {RAW_A: order_a},
        "Supplier_B_0": {RAW_B: order_b},
        "Supplier_C_0": {RAW_C: order_c}
    }

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}