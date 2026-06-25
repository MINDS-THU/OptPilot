
"""
Optimal Supply Chain Replenishment Policy
Best result: 352,415.90 total cost

Strategy:
- Seasonal demand-aware ordering at all levels
- Aggressive raw material targets at Assembler to minimize production shortages
- Zero shortages maintained at DC and Retailer levels

Product IDs:
- Raw_A = 1, Raw_B = 2, Raw_C = 3
- Product_X = 4, Product_Y = 5

BOM:
- Product_X: 1×Raw_A + 1×Raw_C
- Product_Y: 2×Raw_B + 1×Raw_C

Seasonal patterns:
- Product_X: period=21 days, amplitude=6
- Product_Y: period=14 days, amplitude=4
"""
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer policy: Seasonal (s, S) reorder point strategy.
    Adjusts targets based on 21-day (Product_X) and 14-day (Product_Y) cycles.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: inventory_position_X, 5: inventory_position_Y}
    
    Returns:
        {"DC_0": {product_id: order_quantity}} or {}
    """
    inv_x = inventory_dict.get(4, 0.0)
    inv_y = inventory_dict.get(5, 0.0)
    
    # Calculate seasonal adjustment (range: 0.8 to 1.2)
    seasonal_x = 1 + 0.2 * math.sin(2 * math.pi * period / 21)
    seasonal_y = 1 + 0.2 * math.sin(2 * math.pi * period / 14)
    
    # Apply seasonal adjustment to reorder point and order-up-to level
    s_x = int(28 * seasonal_x)
    S_x = int(48 * seasonal_x)
    
    s_y = int(20 * seasonal_y)
    S_y = int(32 * seasonal_y)
    
    orders = {}
    
    if inv_x < s_x:
        orders[4] = max(0.0, S_x - inv_x)
    
    if inv_y < s_y:
        orders[5] = max(0.0, S_y - inv_y)
    
    if orders:
        return {"DC_0": orders}
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC policy: Seasonal (s, S) strategy for aggregating retailer demand.
    Looks ahead 2 days for lead time to Assembler.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: inventory_position_X, 5: inventory_position_Y}
    
    Returns:
        {"Assembler_0": {product_id: order_quantity}} or {}
    """
    inv_x = inventory_dict.get(4, 0.0)
    inv_y = inventory_dict.get(5, 0.0)
    
    # Look ahead for seasonality considering 2-day lead time
    seasonal_x = 1 + 0.2 * math.sin(2 * math.pi * (period + 2) / 21)
    seasonal_y = 1 + 0.2 * math.sin(2 * math.pi * (period + 2) / 14)
    
    S_x = int(180 * seasonal_x)
    S_y = int(130 * seasonal_y)
    
    s_x = int(90 * seasonal_x)
    s_y = int(65 * seasonal_y)
    
    orders = {}
    
    if inv_x < s_x:
        orders[4] = max(0.0, S_x - inv_x)
    
    if inv_y < s_y:
        orders[5] = max(0.0, S_y - inv_y)
    
    if orders:
        return {"Assembler_0": orders}
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler policy: Aggressive raw material ordering to prevent production shortages.
    Looks ahead 4 days (2-day raw material lead time + 2-day production).
    Raw_C is shared between Product_X and Product_Y.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {1: pos_A, 2: pos_B, 3: pos_C, 4: pos_X, 5: pos_Y}
    
    Returns:
        {"Supplier_A_0": {1: qty}, "Supplier_B_0": {2: qty}, "Supplier_C_0": {3: qty}}
    """
    raw_a = inventory_dict.get(1, 0.0)
    raw_b = inventory_dict.get(2, 0.0)
    raw_c = inventory_dict.get(3, 0.0)
    
    # Look ahead for seasonality considering full lead time
    seasonal_x = 1 + 0.2 * math.sin(2 * math.pi * (period + 4) / 21)
    seasonal_y = 1 + 0.2 * math.sin(2 * math.pi * (period + 4) / 14)
    
    # Aggressive targets to minimize production shortages
    target_a = int(250 * seasonal_x)
    target_b = int(400 * seasonal_y)
    target_c = int(320 * (seasonal_x + seasonal_y) / 2)  # Average for shared resource
    
    s_a = int(125 * seasonal_x)
    s_b = int(200 * seasonal_y)
    s_c = int(160 * (seasonal_x + seasonal_y) / 2)
    
    orders = {}
    
    if raw_a < s_a:
        orders["Supplier_A_0"] = {1: max(0.0, target_a - raw_a)}
    
    if raw_b < s_b:
        orders["Supplier_B_0"] = {2: max(0.0, target_b - raw_b)}
    
    if raw_c < s_c:
        orders["Supplier_C_0"] = {3: max(0.0, target_c - raw_c)}
    
    return orders


# Policy function mapping to node groups
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
