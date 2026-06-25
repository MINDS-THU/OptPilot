import math

# Product parameters
PRODUCT_PARAMS = {
    1: {
        "name": "Product_A",
        "base_demand": 18,
        "noise_range": 6,
        "seasonal_amp": 8,
        "period": 21,
    },
    2: {
        "name": "Product_B",
        "base_demand": 12,
        "noise_range": 4,
        "seasonal_amp": 5,
        "period": 14,
    },
}

# Policy parameters
LEAD_TIME = 2  # days from DC to Retailer
REVIEW_PERIOD = 1  # daily review
COVERAGE_DAYS = LEAD_TIME + REVIEW_PERIOD  # = 3 days

# Cost parameters for critical ratio
HOLDING_COST = 2.0  # per item per day
STOCKOUT_COST = 80.0  # per item

# Critical ratio for service level
CRITICAL_RATIO = STOCKOUT_COST / (STOCKOUT_COST + HOLDING_COST)
# CR = 80 / 82 ≈ 0.9756
# This corresponds to a high service level

# Z-score for high service level
Z_SCORE = 2.5


def predict_demand(product_id, start_day, coverage_days):
    """Predict demand for a product over a coverage period starting from start_day."""
    params = PRODUCT_PARAMS[product_id]
    base = params["base_demand"]
    amp = params["seasonal_amp"]
    period = params["period"]
    
    total_demand = 0.0
    for d in range(coverage_days):
        day = start_day + d
        seasonal = amp * math.sin(2 * math.pi * day / period)
        total_demand += base + seasonal
    
    return max(0, total_demand)


def calculate_safety_stock(product_id, coverage_days):
    """Calculate safety stock based on demand variance including seasonal component."""
    params = PRODUCT_PARAMS[product_id]
    noise_range = params["noise_range"]
    amp = params["seasonal_amp"]
    
    # Standard deviation from noise (uniform distribution)
    daily_std_noise = noise_range / math.sqrt(3)
    
    # Additional variance from seasonal component
    # Seasonal varies from -amp to +amp, treating as approximate uniform
    daily_std_seasonal = amp / math.sqrt(3)
    
    # Combined daily standard deviation
    daily_std = math.sqrt(daily_std_noise**2 + daily_std_seasonal**2)
    
    # Standard deviation of demand over coverage days
    coverage_std = daily_std * math.sqrt(coverage_days)
    
    # Safety stock = Z * sigma
    safety_stock = Z_SCORE * coverage_std
    
    # Add a small buffer for uncertainty
    safety_stock += 2.0
    
    return safety_stock


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product replenishment policy for retailers.
    
    Uses a dynamic Order-Up-To (Base-Stock) policy:
    - Target level = Predicted demand over coverage period + Safety stock
    - Order quantity = Target level - Current inventory position
    """
    orders = {"DC_0": {}}
    
    for product_id in [1, 2]:
        # Get current inventory position
        inventory_position = inventory_dict.get(product_id, 0.0)
        
        # Predict demand for the coverage period (lead time + review)
        predicted_demand = predict_demand(product_id, period, COVERAGE_DAYS)
        
        # Calculate safety stock
        safety_stock = calculate_safety_stock(product_id, COVERAGE_DAYS)
        
        # Calculate target level (Order-Up-To level S)
        target_level = predicted_demand + safety_stock
        
        # Calculate order quantity
        order_qty = target_level - inventory_position
        
        # Only order if positive
        if order_qty > 0:
            orders["DC_0"][product_id] = round(order_qty, 2)
        else:
            orders["DC_0"][product_id] = 0.0
    
    # Remove empty orders
    if not orders["DC_0"]:
        return {}
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
