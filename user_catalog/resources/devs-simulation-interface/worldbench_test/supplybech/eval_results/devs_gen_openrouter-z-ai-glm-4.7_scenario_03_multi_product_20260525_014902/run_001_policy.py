import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product replenishment policy for retailers.
    
    Implements an order-up-to (s, S) policy with seasonal demand forecasting
    and safety stock optimization based on cost structure.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position}
                       Keys: 1=Product_A, 2=Product_B
                       Values: inventory position
    
    Returns:
        Order dict: {"DC_0": {product_id: order_quantity}}
    """
    
    # Product parameters from task specification
    # Product_A (id=1): base=18, amp=8, period=21 days, noise=±6
    # Product_B (id=2): base=12, amp=5, period=14 days, noise=±4
    
    product_params = {
        1: {
            "base_demand": 18,
            "seasonal_amp": 8,
            "seasonal_period": 21,
            "safety_stock_multiplier": 1.8,
            "noise_range": 12
        },
        2: {
            "base_demand": 12,
            "seasonal_amp": 5,
            "seasonal_period": 14,
            "safety_stock_multiplier": 1.8,
            "noise_range": 8
        }
    }
    
    # Cost parameters
    holding_cost = 2.0      # yuan/unit/day
    stockout_cost = 80.0    # yuan/unit
    cost_ratio = stockout_cost / holding_cost  # 40:1 favors holding inventory
    
    # Lead time from DC (days)
    lead_time = 2
    
    # Review period (daily review)
    review_period = 1
    
    # Calculate order quantities for each product
    orders = {}
    
    for product_id in [1, 2]:
        params = product_params[product_id]
        base_demand = params["base_demand"]
        seasonal_amp = params["seasonal_amp"]
        seasonal_period = params["seasonal_period"]
        safety_multiplier = params["safety_stock_multiplier"]
        noise_range = params["noise_range"]
        
        # Current inventory position (default to 0 if not in dict)
        inv_pos = inventory_dict.get(product_id, 0.0)
        
        # Calculate seasonal adjustment (sine wave)
        # Use (period - 1) for 0-based calculation since period starts at 1
        seasonal_factor = math.sin(2 * math.pi * (period - 1) / seasonal_period)
        seasonal_demand = base_demand + seasonal_amp * seasonal_factor
        
        # Ensure non-negative demand (truncated at 0)
        expected_daily_demand = max(0.1, seasonal_demand)
        
        # Calculate demand during lead time + review period
        protection_period = lead_time + review_period
        expected_demand_protection = expected_daily_demand * protection_period
        
        # Calculate safety stock based on demand uncertainty
        # For uniform distribution, standard deviation = range / sqrt(12)
        demand_std = noise_range / math.sqrt(12)
        safety_stock = safety_multiplier * demand_std * math.sqrt(protection_period)
        
        # Target order-up-to level
        target_level = expected_demand_protection + safety_stock
        
        # Calculate order quantity (order up to target)
        order_qty = target_level - inv_pos
        
        # Ensure non-negative order
        if order_qty < 0:
            order_qty = 0
        
        # Round to nearest integer
        order_qty = max(0, round(order_qty))
        
        if order_qty > 0:
            orders[product_id] = float(order_qty)
    
    # Return order dict in the specified format
    if orders:
        return {"DC_0": orders}
    else:
        return {}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
