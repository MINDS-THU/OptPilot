
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer replenishment policy using order-up-to level (S) strategy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Inventory position {product_id: quantity}
    
    Returns:
        Order dictionary {"DC_0": {product_id: quantity}}
    """
    # Product IDs
    PRODUCT_X = 4
    PRODUCT_Y = 5
    
    # Demand parameters
    base_demand_X = 15
    base_demand_Y = 10
    noise_X = 5
    noise_Y = 4
    
    # Lead time from DC
    lead_time = 1
    
    # Seasonal parameters
    seasonal_amp_X = 6
    seasonal_amp_Y = 4
    period_X = 21
    period_Y = 14
    
    # Calculate seasonal factor
    seasonal_factor_X = seasonal_amp_X * math.sin(2 * math.pi * period / period_X)
    seasonal_factor_Y = seasonal_amp_Y * math.sin(2 * math.pi * period / period_Y)
    
    # Expected demand during lead time
    expected_demand_X = base_demand_X * lead_time + seasonal_factor_X
    expected_demand_Y = base_demand_Y * lead_time + seasonal_factor_Y
    
    # Safety stock based on demand variability
    # For uniform(-noise, noise), std = noise / sqrt(3)
    std_X = noise_X / math.sqrt(3) * math.sqrt(lead_time)
    std_Y = noise_Y / math.sqrt(3) * math.sqrt(lead_time)
    
    # High service level for retailers (shortage cost is 80, holding cost is 2)
    z_score = 1.8
    safety_stock_X = z_score * std_X + abs(seasonal_amp_X)
    safety_stock_Y = z_score * std_Y + abs(seasonal_amp_Y)
    
    # Order-up-to level
    order_up_to_X = max(0, expected_demand_X + safety_stock_X)
    order_up_to_Y = max(0, expected_demand_Y + safety_stock_Y)
    
    # Current inventory position
    inv_X = inventory_dict.get(PRODUCT_X, 0)
    inv_Y = inventory_dict.get(PRODUCT_Y, 0)
    
    # Order quantity
    order_qty_X = max(0, round(order_up_to_X - inv_X))
    order_qty_Y = max(0, round(order_up_to_Y - inv_Y))
    
    if order_qty_X > 0 or order_qty_Y > 0:
        return {"DC_0": {PRODUCT_X: order_qty_X, PRODUCT_Y: order_qty_Y}}
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC replenishment policy using order-up-to level strategy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Inventory position {product_id: quantity}
    
    Returns:
        Order dictionary {"Assembler_0": {product_id: quantity}}
    """
    # Product IDs
    PRODUCT_X = 4
    PRODUCT_Y = 5
    
    # Demand parameters (serves 3 retailers)
    base_demand_X = 15 * 3  # 3 retailers
    base_demand_Y = 10 * 3
    noise_X = 5
    noise_Y = 4
    
    # Lead time from Assembler
    lead_time = 2
    
    # Seasonal parameters
    seasonal_amp_X = 6
    seasonal_amp_Y = 4
    period_X = 21
    period_Y = 14
    
    # Calculate seasonal factor
    seasonal_factor_X = seasonal_amp_X * math.sin(2 * math.pi * period / period_X) * 3
    seasonal_factor_Y = seasonal_amp_Y * math.sin(2 * math.pi * period / period_Y) * 3
    
    # Expected demand during lead time
    expected_demand_X = base_demand_X * lead_time + seasonal_factor_X
    expected_demand_Y = base_demand_Y * lead_time + seasonal_factor_Y
    
    # Safety stock
    std_X = noise_X * math.sqrt(3) / math.sqrt(3) * math.sqrt(lead_time)
    std_Y = noise_Y * math.sqrt(3) / math.sqrt(3) * math.sqrt(lead_time)
    
    z_score = 1.8
    safety_stock_X = z_score * std_X + abs(seasonal_amp_X * 3)
    safety_stock_Y = z_score * std_Y + abs(seasonal_amp_Y * 3)
    
    # Order-up-to level
    order_up_to_X = max(0, expected_demand_X + safety_stock_X)
    order_up_to_Y = max(0, expected_demand_Y + safety_stock_Y)
    
    # Current inventory position
    inv_X = inventory_dict.get(PRODUCT_X, 0)
    inv_Y = inventory_dict.get(PRODUCT_Y, 0)
    
    # Order quantity
    order_qty_X = max(0, round(order_up_to_X - inv_X))
    order_qty_Y = max(0, round(order_up_to_Y - inv_Y))
    
    if order_qty_X > 0 or order_qty_Y > 0:
        return {"Assembler_0": {PRODUCT_X: order_qty_X, PRODUCT_Y: order_qty_Y}}
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler replenishment policy with BOM constraints.
    
    BOM:
    - Product_X (4): 1×Raw_A (1) + 1×Raw_C (3)
    - Product_Y (5): 2×Raw_B (2) + 1×Raw_C (3)
    
    Args:
        period: Current day (1-100)
        inventory_dict: Inventory position {product_id: quantity}
    
    Returns:
        Order dictionary {"Supplier_X_0": {product_id: quantity}}
    """
    # Product IDs
    RAW_A = 1
    RAW_B = 2
    RAW_C = 3
    PRODUCT_X = 4
    PRODUCT_Y = 5
    
    # Demand parameters (serves 3 retailers)
    base_demand_X = 15 * 3
    base_demand_Y = 10 * 3
    noise_X = 5
    noise_Y = 4
    
    # Lead time from suppliers
    lead_time = 2
    
    # Seasonal parameters
    seasonal_amp_X = 6
    seasonal_amp_Y = 4
    period_X = 21
    period_Y = 14
    
    # Calculate seasonal factor
    seasonal_factor_X = seasonal_amp_X * math.sin(2 * math.pi * period / period_X) * 3
    seasonal_factor_Y = seasonal_amp_Y * math.sin(2 * math.pi * period / period_Y) * 3
    
    # Expected demand for finished goods during lead time
    expected_demand_X = base_demand_X * lead_time + seasonal_factor_X
    expected_demand_Y = base_demand_Y * lead_time + seasonal_factor_Y
    
    # Safety stock for finished goods
    std_X = noise_X * math.sqrt(3) / math.sqrt(3) * math.sqrt(lead_time)
    std_Y = noise_Y * math.sqrt(3) / math.sqrt(3) * math.sqrt(lead_time)
    
    z_score = 1.7  # Slightly lower for assembler
    safety_stock_X = z_score * std_X + abs(seasonal_amp_X * 3)
    safety_stock_Y = z_score * std_Y + abs(seasonal_amp_Y * 3)
    
    # Target finished goods inventory
    target_fg_X = max(0, expected_demand_X + safety_stock_X)
    target_fg_Y = max(0, expected_demand_Y + safety_stock_Y)
    
    # Current finished goods inventory
    current_fg_X = inventory_dict.get(PRODUCT_X, 0)
    current_fg_Y = inventory_dict.get(PRODUCT_Y, 0)
    
    # Calculate required production
    produce_X = max(0, round(target_fg_X - current_fg_X))
    produce_Y = max(0, round(target_fg_Y - current_fg_Y))
    
    # Calculate raw material requirements using BOM
    # Product_X: 1×Raw_A + 1×Raw_C
    # Product_Y: 2×Raw_B + 1×Raw_C
    need_raw_A = produce_X * 1
    need_raw_B = produce_Y * 2
    need_raw_C = produce_X * 1 + produce_Y * 1
    
    # Calculate target raw material inventory
    # Account for lead time demand + safety stock + current production needs
    raw_demand_lt_A = base_demand_X * lead_time * 1
    raw_demand_lt_B = base_demand_Y * lead_time * 2
    raw_demand_lt_C = (base_demand_X * 1 + base_demand_Y * 1) * lead_time
    
    safety_stock_A = safety_stock_X * 1
    safety_stock_B = safety_stock_Y * 2
    safety_stock_C = safety_stock_X * 1 + safety_stock_Y * 1
    
    target_raw_A = max(0, raw_demand_lt_A + safety_stock_A + need_raw_A)
    target_raw_B = max(0, raw_demand_lt_B + safety_stock_B + need_raw_B)
    target_raw_C = max(0, raw_demand_lt_C + safety_stock_C + need_raw_C)
    
    # Current raw material inventory
    current_raw_A = inventory_dict.get(RAW_A, 0)
    current_raw_B = inventory_dict.get(RAW_B, 0)
    current_raw_C = inventory_dict.get(RAW_C, 0)
    
    # Order quantity for raw materials
    order_qty_A = max(0, round(target_raw_A - current_raw_A))
    order_qty_B = max(0, round(target_raw_B - current_raw_B))
    order_qty_C = max(0, round(target_raw_C - current_raw_C))
    
    # Build order dictionary
    orders = {}
    if order_qty_A > 0:
        orders["Supplier_A_0"] = {RAW_A: order_qty_A}
    if order_qty_B > 0:
        orders["Supplier_B_0"] = {RAW_B: order_qty_B}
    if order_qty_C > 0:
        orders["Supplier_C_0"] = {RAW_C: order_qty_C}
    
    return orders


# Policy mounts dictionary as required by task
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
