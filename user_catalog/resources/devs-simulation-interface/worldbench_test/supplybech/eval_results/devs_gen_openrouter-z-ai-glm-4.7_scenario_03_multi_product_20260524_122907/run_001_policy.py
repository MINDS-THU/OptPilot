"""
Retailer Replenishment Policy for Multi-Product Supply Chain
Task: Design a multi-product replenishment strategy for Retailer layer
Optimization Goal: Minimize total cost = holding cost + shortage cost
"""

import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product replenishment policy for Retailer nodes.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Inventory snapshot {product_id: inventory_position}
                       where inventory_position = physical_inventory + in_transit - backorders
    
    Returns:
        Order dict {"DC_0": {product_id: order_quantity}} or {} if no order
    """
    
    # Lead time from DC to Retailer: 2 days
    LEAD_TIME = 2
    
    # Demand parameters (from task specification)
    # Product_A (product_id=1): high demand, 21-day seasonal cycle
    # Product_B (product_id=2): low demand, 14-day seasonal cycle
    demand_params = {
        1: {
            'base_demand': 18.0,
            'seasonal_amp': 8.0,
            'seasonal_period': 21.0,
            'noise_std': 3.0,
        },
        2: {
            'base_demand': 12.0,
            'seasonal_amp': 5.0,
            'seasonal_period': 14.0,
            'noise_std': 2.0,
        }
    }
    
    # Cost parameters
    holding_cost = 2.0  # yuan/unit/day
    shortage_cost = 80.0  # yuan/unit
    
    # Critical ratio for service level
    # CR = shortage_cost / (shortage_cost + holding_cost * lead_time)
    # CR = 80 / (80 + 2 * 2) = 80 / 84 ≈ 0.952
    # This indicates a 95% service level target is appropriate
    
    # Safety stock parameters
    # Z-score for 95% service level ≈ 1.645
    z_score = 1.645
    
    orders = {}
    
    for product_id, inv_position in inventory_dict.items():
        if product_id not in demand_params:
            continue
        
        params = demand_params[product_id]
        
        # Forecast seasonal demand over lead time period
        seasonal_factor_sum = 0.0
        for day_ahead in range(1, LEAD_TIME + 1):
            future_day = period + day_ahead
            season_phase = 2 * math.pi * future_day / params['seasonal_period']
            seasonal_factor = math.sin(season_phase)
            seasonal_factor_sum += seasonal_factor
        
        avg_seasonal_factor = seasonal_factor_sum / LEAD_TIME
        
        # Expected demand during lead time including seasonality
        expected_lt_demand = params['base_demand'] * LEAD_TIME + params['seasonal_amp'] * avg_seasonal_factor
        
        # Calculate safety stock based on demand uncertainty
        # Safety stock = z * sigma * sqrt(lead_time)
        safety_stock = z_score * params['noise_std'] * math.sqrt(LEAD_TIME)
        
        # Additional seasonal buffer to account for forecast error
        seasonal_buffer = params['seasonal_amp'] * 0.3
        
        # Order-up-to level (S)
        S = expected_lt_demand + safety_stock + seasonal_buffer
        
        # Ensure minimum order-up-to level covers base demand for lead_time + 1 day
        min_S = params['base_demand'] * (LEAD_TIME + 1)
        S = max(S, min_S)
        
        # Calculate order quantity to reach order-up-to level
        order_qty = S - inv_position
        
        # Only place order if positive quantity needed
        if order_qty > 0.5:  # Small threshold to avoid micro-orders
            order_qty = max(0.0, round(order_qty))
            orders[product_id] = float(order_qty)
    
    # Return order dict if any orders to place
    if orders:
        return {"DC_0": orders}
    else:
        return {}


# Policy mounting configuration as required by task
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
