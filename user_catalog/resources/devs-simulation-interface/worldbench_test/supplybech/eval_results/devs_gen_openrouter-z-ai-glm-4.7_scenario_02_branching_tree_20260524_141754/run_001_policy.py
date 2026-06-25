
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer ordering policy using order-up-to (base-stock) strategy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Current inventory positions by product_id
                       inventory_position = physical_inventory + in_transit - backorders
    
    Returns:
        Order dict with upstream DC and product quantities
    """
    # Product ID
    product_id = 1
    
    # Get current inventory position
    inv_pos = inventory_dict.get(product_id, 0.0)
    
    # Parameters for retailer (based on task requirements)
    lead_time = 2  # days from DC
    avg_daily_demand = 25.0
    
    # Lead time demand
    lead_time_demand = avg_daily_demand * lead_time  # 50 units
    
    # Demand variability (±8 uniform => std ≈ 8/√3 ≈ 4.62)
    demand_std = 4.62
    
    # Service level target based on cost ratio
    # shortage_cost = 120, holding_cost = 3.0
    critical_ratio = 120.0 / (120.0 + 3.0)  # ≈ 0.9756
    
    # Z-score for 97.5% service level
    z_score = 1.96
    
    # Safety stock
    safety_stock = z_score * demand_std * math.sqrt(lead_time)  # ≈ 12.8
    
    # Seasonal adjustment (14-day cycle, ±5 amplitude)
    seasonal_factor = 5.0 * math.sin(2 * math.pi * period / 14)
    
    # Order-up-to level
    order_up_to = lead_time_demand + safety_stock + seasonal_factor
    
    # Calculate order quantity
    order_qty = max(0.0, round(order_up_to - inv_pos, 2))
    
    # Return order - note: actual upstream DC is determined by node configuration
    return {"Regional_DC_0": {product_id: order_qty}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Distribution Center ordering policy using order-up-to (base-stock) strategy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Current inventory positions by product_id
    
    Returns:
        Order dict with Factory and product quantities
    """
    # Product ID
    product_id = 1
    
    # Get current inventory position
    inv_pos = inventory_dict.get(product_id, 0.0)
    
    # Parameters for DC (based on task requirements)
    lead_time = 4  # days from Factory
    num_retailers = 3  # Each DC serves 3 retailers
    avg_daily_demand_retailer = 25.0
    avg_daily_demand = avg_daily_demand_retailer * num_retailers  # 75 units/day
    
    # Lead time demand
    lead_time_demand = avg_daily_demand * lead_time  # 300 units
    
    # Demand variability at DC (sum of 3 retailers' demands)
    demand_std_retailer = 4.62
    demand_std = demand_std_retailer * math.sqrt(num_retailers)  # ≈ 8.0
    
    # Service level target based on cost ratio
    # shortage_cost = 40, holding_cost = 0.8
    critical_ratio = 40.0 / (40.0 + 0.8)  # ≈ 0.9804
    
    # Z-score for 98% service level
    z_score = 2.05
    
    # Safety stock with smoothing to reduce bullwhip effect
    safety_stock = z_score * demand_std * math.sqrt(lead_time)  # ≈ 32.8
    
    # Seasonal adjustment (aggregated from 3 retailers, 14-day cycle)
    seasonal_factor = 3 * 5.0 * math.sin(2 * math.pi * period / 14)  # ±15 units
    
    # Order-up-to level
    order_up_to = lead_time_demand + safety_stock + seasonal_factor
    
    # Calculate order quantity
    order_qty = max(0.0, round(order_up_to - inv_pos, 2))
    
    # Return order to Factory
    return {"Factory_0": {product_id: order_qty}}


# Policy mounts dictionary
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
