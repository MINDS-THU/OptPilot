import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product replenishment policy for Retailer nodes.
    Optimized safety factor based on cost ratio analysis.
    """
    # Product parameters
    product_params = {
        1: {  # Product_A
            'base': 18,
            'amplitude': 8,
            'period': 21,
            'safety_factor': 1.5  # Slightly higher to reduce shortage
        },
        2: {  # Product_B
            'base': 12,
            'amplitude': 5,
            'period': 14,
            'safety_factor': 1.5
        }
    }
    
    lead_time = 2
    
    orders = {}
    
    for product_id, params in product_params.items():
        base = params['base']
        amplitude = params['amplitude']
        seasonal_period = params['period']
        safety_factor = params['safety_factor']
        
        # Expected demand during lead time
        expected_demand_lt = 0
        for day_offset in range(lead_time):
            future_day = period + day_offset
            expected_daily = base + amplitude * math.sin(2 * math.pi * future_day / seasonal_period)
            expected_demand_lt += max(0, expected_daily)
        
        # Demand variability
        noise_range = 6 if product_id == 1 else 4
        demand_std = (noise_range / math.sqrt(3)) + (amplitude / math.sqrt(2))
        
        # Safety stock
        safety_stock = safety_factor * demand_std * math.sqrt(lead_time)
        
        # Reorder point
        reorder_point = expected_demand_lt + safety_stock
        
        current_position = inventory_dict.get(product_id, 0)
        order_quantity = reorder_point - current_position
        
        if order_quantity > 0:
            orders[product_id] = order_quantity
    
    return orders

policy = retailer_policy_func

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
