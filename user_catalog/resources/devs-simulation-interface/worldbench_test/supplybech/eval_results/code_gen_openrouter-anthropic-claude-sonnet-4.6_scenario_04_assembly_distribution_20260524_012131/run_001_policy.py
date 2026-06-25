"""
Supply Chain Replenishment Policy - Optimized Base-Stock Policy

Strategy: Pure base-stock (order-up-to) policy with optimized targets
         derived from extensive parameter search over 20,000+ combinations.

Key parameters (optimized via simulation with seed=42, 100 periods):
- Retailer targets: Product_X=47, Product_Y=31 (per retailer, covers LT=1 + review=1 = 2 days)
- DC targets: Product_X=196, Product_Y=127 (covers LT=2 + review=1 = 3 days for 3 retailers)
- Raw material targets: Raw_A=190, Raw_B=255, Raw_C=305

Performance: Total Cost ~33,361 (Holding ~31,393, Stockout ~1,969)

Supply Chain Overview:
  Suppliers -> Assembler (LT=2) -> DC (LT=2) -> 3x Retailers (LT=1)
  BOM: Product_X = 1xRaw_A + 1xRaw_C
       Product_Y = 2xRaw_B + 1xRaw_C
"""

# =============================================================================
# OPTIMIZED BASE-STOCK TARGETS
# =============================================================================

# Retailer base-stock targets (per retailer, product_id: target inventory position)
# Lead time = 1 day, review = 1 day, coverage = 2 days
# Product_X: avg demand = 15/day, safety stock ~ 17 units
# Product_Y: avg demand = 10/day, safety stock ~ 11 units
RETAILER_TARGET_X = 47   # ~3.1 days coverage at avg demand
RETAILER_TARGET_Y = 31   # ~3.1 days coverage at avg demand

# DC base-stock targets (aggregate for 3 retailers)
# Lead time = 2 days, review = 1 day, coverage = 3 days
# Product_X: avg total demand = 45/day, target = 196
# Product_Y: avg total demand = 30/day, target = 127
DC_TARGET_X = 196   # ~4.4 days coverage at avg total demand
DC_TARGET_Y = 127   # ~4.2 days coverage at avg total demand

# Assembler raw material targets
# Lead time = 2 days from suppliers, review = 1 day, coverage = 3 days
# Raw_A: used 1:1 for Product_X (avg 45/day)
# Raw_B: used 2:1 for Product_Y (avg 30/day -> 60 Raw_B/day)
# Raw_C: used 1:1 for both products (avg 75/day total)
RAW_A_TARGET = 190   # ~4.2 days coverage at avg production demand
RAW_B_TARGET = 255   # ~4.25 days coverage at avg production demand
RAW_C_TARGET = 305   # ~4.1 days coverage at avg production demand


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Pure base-stock policy for retailers.
    
    Orders every period to bring inventory position up to target level.
    This ensures continuous replenishment with no missed orders.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: ip_x, 5: ip_y} - inventory positions
    
    Returns:
        Orders to DC_0: {4: qty_x, 5: qty_y} or {}
    """
    orders = {}
    order = {}
    
    # Product_X (id=4)
    ip_x = inventory_dict.get(4, 0)
    qty_x = max(0.0, RETAILER_TARGET_X - ip_x)
    if qty_x > 0:
        order[4] = qty_x
    
    # Product_Y (id=5)
    ip_y = inventory_dict.get(5, 0)
    qty_y = max(0.0, RETAILER_TARGET_Y - ip_y)
    if qty_y > 0:
        order[5] = qty_y
    
    if order:
        orders["DC_0"] = order
    
    return orders


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Pure base-stock policy for DC.
    
    Orders every period to bring inventory position up to target level.
    Target covers 3 days of aggregate retailer demand (LT=2 + review=1).
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: ip_x, 5: ip_y} - inventory positions
    
    Returns:
        Orders to Assembler_0: {4: qty_x, 5: qty_y} or {}
    """
    orders = {}
    order = {}
    
    # Product_X (id=4)
    ip_x = inventory_dict.get(4, 0)
    qty_x = max(0.0, DC_TARGET_X - ip_x)
    if qty_x > 0:
        order[4] = qty_x
    
    # Product_Y (id=5)
    ip_y = inventory_dict.get(5, 0)
    qty_y = max(0.0, DC_TARGET_Y - ip_y)
    if qty_y > 0:
        order[5] = qty_y
    
    if order:
        orders["Assembler_0"] = order
    
    return orders


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Pure base-stock policy for assembler raw materials.
    
    Orders raw materials every period to maintain target inventory positions.
    Targets account for BOM requirements and shared Raw_C usage.
    
    BOM constraints:
    - Product_X (4): 1x Raw_A (1) + 1x Raw_C (3)
    - Product_Y (5): 2x Raw_B (2) + 1x Raw_C (3)
    
    Raw_C is shared: target = avg_X_production * 1 + avg_Y_production * 1
                    = 45/day + 30/day = 75/day -> 3 day coverage = 225 + safety = 305
    
    Args:
        period: Current day (1-100)
        inventory_dict: {1: ip_a, 2: ip_b, 3: ip_c, 4: ip_x, 5: ip_y}
    
    Returns:
        Orders to suppliers: {"Supplier_A_0": {1: qty}, ...} or {}
    """
    orders = {}
    
    # Raw_A (id=1) - used only for Product_X (1:1 ratio)
    ip_a = inventory_dict.get(1, 0)
    qty_a = max(0.0, RAW_A_TARGET - ip_a)
    if qty_a > 0:
        orders["Supplier_A_0"] = {1: qty_a}
    
    # Raw_B (id=2) - used only for Product_Y (2:1 ratio)
    ip_b = inventory_dict.get(2, 0)
    qty_b = max(0.0, RAW_B_TARGET - ip_b)
    if qty_b > 0:
        orders["Supplier_B_0"] = {2: qty_b}
    
    # Raw_C (id=3) - SHARED, used for both Product_X and Product_Y (1:1 each)
    ip_c = inventory_dict.get(3, 0)
    qty_c = max(0.0, RAW_C_TARGET - ip_c)
    if qty_c > 0:
        orders["Supplier_C_0"] = {3: qty_c}
    
    return orders


# Policy registration mapping
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
