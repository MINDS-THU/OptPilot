
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer layer replenishment policy using order-up-to level (base-stock) policy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position} for products 4 (Product_X) and 5 (Product_Y)
    
    Returns:
        {"DC_0": {4: order_qty_X, 5: order_qty_Y}} or {} if no orders
    """
    # Get current inventory positions
    inv_x = inventory_dict.get(4, 0.0)  # Product_X
    inv_y = inventory_dict.get(5, 0.0)  # Product_Y
    
    # Demand parameters (per retailer per day)
    # Product_X: base=15, noise=±5, seasonal_amp=6, period=21
    # Product_Y: base=10, noise=±4, seasonal_amp=4, period=14
    
    # Calculate seasonal adjustment factor
    season_factor_x = 6 * math.sin(2 * math.pi * period / 21)
    season_factor_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # Expected daily demand
    avg_demand_x = 15 + season_factor_x
    avg_demand_y = 10 + season_factor_y
    
    # Lead time from DC: 1 day
    lead_time = 1
    
    # Safety stock calculation based on newsvendor critical fractile
    # Critical ratio = shortage_cost / (shortage_cost + holding_cost)
    # For retailers: 80 / (80 + 2) ≈ 0.975
    # For uniform distribution, safety stock = z * sigma * sqrt(lead_time)
    # std_dev of uniform(a,b) = (b-a) / sqrt(12)
    std_x = (20) / math.sqrt(12)  # range: 15+5 to 15-5 = 20
    std_y = (16) / math.sqrt(12)  # range: 10+4 to 10-4 = 16
    
    # z-score for 97.5% service level
    z_score = 1.96
    safety_stock_x = z_score * std_x * math.sqrt(lead_time)
    safety_stock_y = z_score * std_y * math.sqrt(lead_time)
    
    # Order-up-to level = lead_time demand + safety stock
    order_up_to_x = avg_demand_x * lead_time + safety_stock_x
    order_up_to_y = avg_demand_y * lead_time + safety_stock_y
    
    # Calculate order quantities (round to integer)
    order_x = max(0.0, round(order_up_to_x - inv_x))
    order_y = max(0.0, round(order_up_to_y - inv_y))
    
    if order_x > 0 or order_y > 0:
        return {"DC_0": {4: order_x, 5: order_y}}
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC layer replenishment policy using order-up-to level policy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position} for products 4 (Product_X) and 5 (Product_Y)
    
    Returns:
        {"Assembler_0": {4: order_qty_X, 5: order_qty_Y}} or {} if no orders
    """
    # Get current inventory positions
    inv_x = inventory_dict.get(4, 0.0)  # Product_X
    inv_y = inventory_dict.get(5, 0.0)  # Product_Y
    
    # Total demand from 3 retailers
    num_retailers = 3
    lead_time = 2  # days from Assembler
    
    # Calculate seasonal adjustment factor
    season_factor_x = 6 * math.sin(2 * math.pi * period / 21)
    season_factor_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # Expected daily demand per retailer
    daily_demand_x = 15 + season_factor_x
    daily_demand_y = 10 + season_factor_y
    
    # Total lead time demand
    demand_lt_x = daily_demand_x * num_retailers * lead_time
    demand_lt_y = daily_demand_y * num_retailers * lead_time
    
    # Safety stock with risk pooling
    # Critical ratio = 30 / (30 + 0.5) ≈ 0.984
    std_x = 20 / math.sqrt(12)
    std_y = 16 / math.sqrt(12)
    
    # z-score for 98.4% service level
    z_score = 2.15
    # Risk pooling reduces variability by sqrt(num_retailers)
    safety_stock_x = z_score * std_x * math.sqrt(lead_time * num_retailers)
    safety_stock_y = z_score * std_y * math.sqrt(lead_time * num_retailers)
    
    # Order-up-to level
    order_up_to_x = demand_lt_x + safety_stock_x
    order_up_to_y = demand_lt_y + safety_stock_y
    
    # Calculate order quantities
    order_x = max(0.0, round(order_up_to_x - inv_x))
    order_y = max(0.0, round(order_up_to_y - inv_y))
    
    if order_x > 0 or order_y > 0:
        return {"Assembler_0": {4: order_x, 5: order_y}}
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler layer replenishment policy for raw materials.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {1: Raw_A, 2: Raw_B, 3: Raw_C, 4: Product_X, 5: Product_Y}
    
    Returns:
        {"Supplier_A_0": {1: qty_A}, "Supplier_B_0": {2: qty_B}, "Supplier_C_0": {3: qty_C}}
    """
    # Get current inventory positions
    inv_raw_a = inventory_dict.get(1, 0.0)  # Raw_Material_A
    inv_raw_b = inventory_dict.get(2, 0.0)  # Raw_Material_B
    inv_raw_c = inventory_dict.get(3, 0.0)  # Raw_Material_C (shared)
    inv_prod_x = inventory_dict.get(4, 0.0)  # Product_X
    inv_prod_y = inventory_dict.get(5, 0.0)  # Product_Y
    
    # Total demand from all retailers (3 retailers)
    num_retailers = 3
    lead_time = 2  # days from suppliers
    
    # Calculate seasonal adjustment factors
    season_factor_x = 6 * math.sin(2 * math.pi * period / 21)
    season_factor_y = 4 * math.sin(2 * math.pi * period / 14)
    
    # Daily demand per retailer
    daily_x = 15 + season_factor_x
    daily_y = 10 + season_factor_y
    
    # Total daily demand across all retailers
    total_daily_x = daily_x * num_retailers
    total_daily_y = daily_y * num_retailers
    
    # Demand over lead time
    demand_lt_x = total_daily_x * lead_time
    demand_lt_y = total_daily_y * lead_time
    
    # Safety stock for finished goods
    std_x = 20 / math.sqrt(12)
    std_y = 16 / math.sqrt(12)
    z_score = 2.15
    
    # Safety stock with risk pooling
    safety_stock_x = z_score * std_x * math.sqrt(lead_time * num_retailers)
    safety_stock_y = z_score * std_y * math.sqrt(lead_time * num_retailers)
    
    # Target finished goods inventory
    target_x = demand_lt_x + safety_stock_x
    target_y = demand_lt_y + safety_stock_y
    
    # Production needed to reach target
    needed_x = max(0.0, target_x - inv_prod_x)
    needed_y = max(0.0, target_y - inv_prod_y)
    
    # Raw materials required based on BOM
    # Product_X requires: 1×Raw_A + 1×Raw_C
    # Product_Y requires: 2×Raw_B + 1×Raw_C
    raw_a_needed = needed_x * 1  # 1 unit of A per X
    raw_b_needed = needed_y * 2  # 2 units of B per Y
    raw_c_needed = needed_x * 1 + needed_y * 1  # 1 unit of C per X and per Y
    
    # Add safety stock for raw materials (upstream needs higher buffer)
    # Raw materials face uncertainty from both demand and production
    raw_safety_factor = 1.5  # Additional buffer for raw materials
    raw_a_target = raw_a_needed * raw_safety_factor
    raw_b_target = raw_b_needed * raw_safety_factor
    raw_c_target = raw_c_needed * raw_safety_factor
    
    # Order raw materials to reach target levels
    order_raw_a = max(0.0, round(raw_a_target - inv_raw_a))
    order_raw_b = max(0.0, round(raw_b_target - inv_raw_b))
    order_raw_c = max(0.0, round(raw_c_target - inv_raw_c))
    
    # Build order dictionary
    orders = {}
    if order_raw_a > 0:
        orders["Supplier_A_0"] = {1: order_raw_a}
    if order_raw_b > 0:
        orders["Supplier_B_0"] = {2: order_raw_b}
    if order_raw_c > 0:
        orders["Supplier_C_0"] = {3: order_raw_c}
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
