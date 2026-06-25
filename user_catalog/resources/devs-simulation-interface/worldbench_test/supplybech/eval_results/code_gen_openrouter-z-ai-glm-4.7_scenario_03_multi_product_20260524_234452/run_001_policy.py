import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product replenishment policy for retailers.
    Uses order-up-to policy with seasonal demand forecasting and optimized safety stock.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Current inventory positions {1: pos_A, 2: pos_B}
    
    Returns:
        Order quantity dict {"DC_0": {1: qty_A, 2: qty_B}}
    """
    lead_time = 2
    
    # Calculate seasonal multiplier (forecast at order arrival time)
    # Product_A has 21-day seasonal cycle, Product_B has 14-day cycle
    seasonal_a = math.sin(2 * math.pi * (period + lead_time) / 21)
    seasonal_b = math.sin(2 * math.pi * (period + lead_time) / 14)
    
    # Expected demand over lead time with seasonality
    expected_demand_a = lead_time * 18 + 8 * seasonal_a
    expected_demand_b = lead_time * 12 + 5 * seasonal_b
    
    # Safety stock based on critical fractile analysis
    # For holding cost = 2.0, stockout cost = 80.0, lead_time = 2
    # Critical fractile = 80 / (80 + 2*2) = 80/84 ≈ 0.952
    # Corresponds to z ≈ 1.65 for ~95% service level
    safety_stock_a = 1.65 * math.sqrt(lead_time) * 6
    safety_stock_b = 1.65 * math.sqrt(lead_time) * 4
    
    # Order-up-to levels with optimized buffers
    order_up_to_a = expected_demand_a + safety_stock_a + 8
    order_up_to_b = expected_demand_b + safety_stock_b + 5
    
    # Clamp to reasonable ranges
    order_up_to_a = max(35, min(100, order_up_to_a))
    order_up_to_b = max(25, min(70, order_up_to_b))
    
    # Get current inventory positions
    pos_a = inventory_dict.get(1, 0)
    pos_b = inventory_dict.get(2, 0)
    
    # Calculate order quantities (order-up-to policy)
    order_qty_a = max(0, order_up_to_a - pos_a)
    order_qty_b = max(0, order_up_to_b - pos_b)
    
    # Return order dictionary if any orders to place
    if order_qty_a > 0 or order_qty_b > 0:
        return {"DC_0": {1: order_qty_a, 2: order_qty_b}}
    return {}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
