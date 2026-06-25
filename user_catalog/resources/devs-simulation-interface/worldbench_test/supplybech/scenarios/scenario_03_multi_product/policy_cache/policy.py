
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Optimized multi-product replenishment policy for retailers.
    
    This policy implements a dynamic order-up-to (S) strategy that:
    1. Forecasts demand with seasonal adjustments
    2. Calculates safety stock based on demand variability and cost structure
    3. Adjusts targets based on simulation period (early build-up vs steady state)
    4. Coordinates between two products with different demand patterns
    
    Cost structure consideration:
    - Retailer holding cost: 2.0元/件/天
    - Retailer shortage cost: 80.0元/件
    - Critical ratio = 80/(80+2*LT) = 80/84 ≈ 0.95 (target service level)
    
    Args:
        period: Current simulation day (1-100)
        inventory_dict: {product_id: inventory_position} where inventory_position 
                       = physical inventory + in_transit - backorders
    
    Returns:
        Order dictionary: {"DC_0": {product_id: order_quantity}} or {}
    """
    
    # Product-specific parameters derived from task description
    product_params = {
        1: {  # Product_A: High demand, 21-day seasonal cycle
            "base_demand": 18.0,      # Mean daily demand
            "seasonal_amp": 8.0,      # Seasonal amplitude
            "period": 21.0,           # Seasonal cycle length (days)
            "demand_std": 6.0,        # Demand noise (±6)
            "lead_time": 2,           # DC to Retailer lead time
            "holding_cost": 2.0,      # 元/件/天
            "shortage_cost": 80.0     # 元/件
        },
        2: {  # Product_B: Low demand, 14-day seasonal cycle
            "base_demand": 12.0,
            "seasonal_amp": 5.0,
            "period": 14.0,
            "demand_std": 4.0,
            "lead_time": 2,
            "holding_cost": 2.0,
            "shortage_cost": 80.0
        }
    }
    
    orders = {}
    
    for product_id, inv_pos in inventory_dict.items():
        if product_id not in product_params:
            continue
            
        params = product_params[product_id]
        lead_time = params["lead_time"]
        
        # Step 1: Forecast demand over lead time + review period
        # Use seasonal adjustment based on sine wave pattern
        total_demand_forecast = 0.0
        for t in range(lead_time + 1):  # Lead time + 1 review day
            future_period = period + t
            # Seasonal factor: amp * sin(2π * period / cycle_length)
            seasonal_factor = params["seasonal_amp"] * math.sin(
                2 * math.pi * future_period / params["period"]
            )
            daily_demand = params["base_demand"] + seasonal_factor
            total_demand_forecast += max(0, daily_demand)  # Ensure non-negative
        
        # Step 2: Calculate optimal safety stock
        # Using critical ratio approach: CR = Cu / (Cu + Co)
        # Cu = underage cost (shortage) = 80
        # Co = overage cost (holding * lead_time) = 2 * 2 = 4
        # CR = 80 / 84 ≈ 0.95
        # Z-score for 95% service level ≈ 1.645
        critical_ratio = params["shortage_cost"] / (
            params["shortage_cost"] + params["holding_cost"] * lead_time
        )
        # Approximate z-score from critical ratio
        z_score = 1.645  # For ~95% service level
        
        # Safety stock = z * σ * sqrt(lead_time)
        safety_stock = z_score * params["demand_std"] * math.sqrt(lead_time + 1)
        
        # Step 3: Calculate order-up-to level (S)
        # S = lead_time_demand + safety_stock + target_base_inventory
        
        # Target base inventory: cover average demand between DC replenishments
        # DC has 3-day lead time from factory, retailers need buffer
        dc_replenishment_cycle = 3  # Factory to DC lead time
        buffer_days = dc_replenishment_cycle + lead_time + 1
        target_base_inventory = params["base_demand"] * buffer_days
        
        # Early period adjustment: build stock aggressively in first 10 days
        early_period_boost = 0.0
        if period <= 5:
            early_period_boost = 30.0
        elif period <= 10:
            early_period_boost = 20.0
        elif period <= 15:
            early_period_boost = 10.0
        
        # Final order-up-to level
        order_up_to_level = (
            total_demand_forecast + 
            safety_stock + 
            target_base_inventory + 
            early_period_boost
        )
        
        # Step 4: Calculate order quantity
        order_qty = order_up_to_level - inv_pos
        
        # Apply constraints
        order_qty = max(0, order_qty)  # Non-negative
        order_qty = round(order_qty, 1)  # Round to 1 decimal
        
        # Minimum order quantity to avoid inefficient tiny orders
        if 0 < order_qty < 10:
            order_qty = 10.0
        
        # Add to orders if meaningful
        if order_qty > 0:
            if "DC_0" not in orders:
                orders["DC_0"] = {}
            orders["DC_0"][product_id] = order_qty
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
