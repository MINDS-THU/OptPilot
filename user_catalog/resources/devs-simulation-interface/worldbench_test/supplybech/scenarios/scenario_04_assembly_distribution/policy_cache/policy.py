
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer policy: Order-up-to policy with seasonal adjustment.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position} where keys are 4, 5
    
    Returns:
        {"DC_0": {4: order_qty_X, 5: order_qty_Y}}
    """
    inv_4 = inventory_dict.get(4, 0)  # Product_X
    inv_5 = inventory_dict.get(5, 0)  # Product_Y
    
    # Seasonal adjustment factors
    # Product_X: period=21, Product_Y: period=14
    seasonal_4 = 1 + 0.3 * math.sin(2 * math.pi * period / 21)
    seasonal_5 = 1 + 0.3 * math.sin(2 * math.pi * period / 14)
    
    # Order-up-to levels (base = lead_time * daily_demand + safety_stock)
    # Lead time = 1 day, base demand: X=15, Y=10
    # Safety stock covers variability and seasonality
    base_S_4 = 30  # ~2 days demand + safety
    base_S_5 = 25  # ~2.5 days demand + safety
    
    S_4 = base_S_4 * seasonal_4
    S_5 = base_S_5 * seasonal_5
    
    # Order quantities
    q_4 = max(0, S_4 - inv_4)
    q_5 = max(0, S_5 - inv_5)
    
    orders = {}
    if q_4 > 0 or q_5 > 0:
        orders["DC_0"] = {}
        if q_4 > 0:
            orders["DC_0"][4] = q_4
        if q_5 > 0:
            orders["DC_0"][5] = q_5
    
    return orders


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC policy: Order-up-to policy coordinating 3 retailers.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position}
    
    Returns:
        {"Assembler_0": {4: order_qty_X, 5: order_qty_Y}}
    """
    inv_4 = inventory_dict.get(4, 0)  # Product_X
    inv_5 = inventory_dict.get(5, 0)  # Product_Y
    
    # Seasonal adjustment
    seasonal_4 = 1 + 0.3 * math.sin(2 * math.pi * period / 21)
    seasonal_5 = 1 + 0.3 * math.sin(2 * math.pi * period / 14)
    
    # Aggregate demand from 3 retailers
    # Daily demand: X = 3*15 = 45, Y = 3*10 = 30
    # Lead time = 2 days from Assembler
    # Order-up-to = 3 days of demand + safety stock
    base_S_4 = 150  # 3 * 45 + safety
    base_S_5 = 100  # 3 * 30 + safety
    
    S_4 = base_S_4 * seasonal_4
    S_5 = base_S_5 * seasonal_5
    
    q_4 = max(0, S_4 - inv_4)
    q_5 = max(0, S_5 - inv_5)
    
    orders = {}
    if q_4 > 0 or q_5 > 0:
        orders["Assembler_0"] = {}
        if q_4 > 0:
            orders["Assembler_0"][4] = q_4
        if q_5 > 0:
            orders["Assembler_0"][5] = q_5
    
    return orders


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler policy: Order raw materials based on BOM and finished goods demand.
    
    BOM:
    - Product_X (4): 1×Raw_A (1) + 1×Raw_C (3)
    - Product_Y (5): 2×Raw_B (2) + 1×Raw_C (3)
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position} for products 1-5
    
    Returns:
        {
            "Supplier_A_0": {1: order_qty_A},
            "Supplier_B_0": {2: order_qty_B},
            "Supplier_C_0": {3: order_qty_C}
        }
    """
    inv_1 = inventory_dict.get(1, 0)  # Raw_A
    inv_2 = inventory_dict.get(2, 0)  # Raw_B
    inv_3 = inventory_dict.get(3, 0)  # Raw_C
    inv_4 = inventory_dict.get(4, 0)  # Product_X
    inv_5 = inventory_dict.get(5, 0)  # Product_Y
    
    # Seasonal adjustment for finished goods targets
    seasonal_4 = 1 + 0.3 * math.sin(2 * math.pi * period / 21)
    seasonal_5 = 1 + 0.3 * math.sin(2 * math.pi * period / 14)
    
    # Target inventory levels for finished goods (matching DC order-up-to)
    target_4 = 150 * seasonal_4
    target_5 = 100 * seasonal_5
    
    # Gap in finished goods that needs to be produced
    gap_4 = max(0, target_4 - inv_4)
    gap_5 = max(0, target_5 - inv_5)
    
    # Raw materials needed to fill the gaps
    # Product_X: 1×Raw_A + 1×Raw_C per unit
    # Product_Y: 2×Raw_B + 1×Raw_C per unit
    raw_A_needed = gap_4
    raw_B_needed = 2 * gap_5
    raw_C_needed = gap_4 + gap_5  # Shared between both products
    
    # Raw material order-up-to levels
    # Lead time = 2 days from suppliers
    # Safety stock factor accounts for demand variability and lead time
    safety_factor = 2.0
    
    base_A = raw_A_needed * safety_factor + 150  # Additional base safety stock
    base_B = raw_B_needed * safety_factor + 200
    base_C = raw_C_needed * safety_factor + 150
    
    # Order quantities (ensure non-negative)
    q_1 = max(0, base_A - inv_1)
    q_2 = max(0, base_B - inv_2)
    q_3 = max(0, base_C - inv_3)
    
    orders = {}
    if q_1 > 0:
        orders["Supplier_A_0"] = {1: q_1}
    if q_2 > 0:
        orders["Supplier_B_0"] = {2: q_2}
    if q_3 > 0:
        orders["Supplier_C_0"] = {3: q_3}
    
    return orders


# Policy mounting dictionary as required by task specification
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
