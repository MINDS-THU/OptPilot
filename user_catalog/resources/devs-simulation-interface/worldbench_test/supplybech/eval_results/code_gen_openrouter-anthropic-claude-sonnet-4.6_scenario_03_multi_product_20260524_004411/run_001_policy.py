
import math


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Seasonal-aware multi-product replenishment policy for retailers.
    
    Uses an order-up-to (base-stock) policy with seasonal adjustments:
    1. Expected demand forecast over protection interval (lead_time + review period)
    2. Base safety stock from noise uncertainty (z * sigma * sqrt(T))
    3. Seasonal level adjustment: extra stock during high season
    4. Seasonal trend adjustment: extra stock when demand is rising
    
    Optimized parameters (via grid search simulation):
    - Product A (id=1): z=1.52, level_factor=0.24, trend_factor=0.30
    - Product B (id=2): z=1.44, level_factor=0.13, trend_factor=1.12
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position}
            inventory_position = on_hand + in_transit - backlog
    
    Returns:
        {"DC_0": {product_id: order_qty}} or {} if no orders needed
    """
    # Product parameters (known from problem description)
    params = {
        1: {
            "base": 18,          # mean daily demand
            "noise": 6,          # uniform noise ±6
            "seasonal_amp": 8,   # seasonal amplitude
            "period": 21,        # seasonal period (days)
            "z": 1.52,           # safety stock factor
            "level_f": 0.24,     # seasonal level adjustment factor
            "trend_f": 0.30,     # seasonal trend adjustment factor
        },
        2: {
            "base": 12,          # mean daily demand
            "noise": 4,          # uniform noise ±4
            "seasonal_amp": 5,   # seasonal amplitude
            "period": 14,        # seasonal period (days)
            "z": 1.44,           # safety stock factor
            "level_f": 0.13,     # seasonal level adjustment factor
            "trend_f": 1.12,     # seasonal trend adjustment factor
        },
    }
    
    lead_time = 2          # days from DC to retailer
    review_period = 1      # review every day
    protection_interval = lead_time + review_period  # = 3 days
    
    product_orders = {}
    
    for pid, p in params.items():
        ip = inventory_dict.get(pid, 0.0)
        
        # ---- 1. Forecast expected demand over protection interval ----
        # Account for known seasonal pattern deterministically
        expected_demand = 0.0
        for t in range(protection_interval):
            day = period + t
            seasonal = p["seasonal_amp"] * math.sin(2 * math.pi * day / p["period"])
            expected_demand += max(0.0, p["base"] + seasonal)
        
        # ---- 2. Safety stock for random noise ----
        # Noise is uniform(-noise, +noise), so std = noise / sqrt(3)
        daily_noise_std = p["noise"] / math.sqrt(3)
        protection_std = daily_noise_std * math.sqrt(protection_interval)
        safety_stock = p["z"] * protection_std
        
        # ---- 3. Seasonal level adjustment ----
        # When we are in a high-demand season, carry extra buffer
        day_mid = period + protection_interval // 2
        seasonal_level = math.sin(2 * math.pi * day_mid / p["period"])  # range: -1 to +1
        level_adjustment = p["level_f"] * p["seasonal_amp"] * max(0.0, seasonal_level)
        
        # ---- 4. Seasonal trend adjustment ----
        # When demand is rising (positive derivative of seasonal), carry extra safety
        seasonal_trend = math.cos(2 * math.pi * day_mid / p["period"])  # range: -1 to +1
        trend_adjustment = p["trend_f"] * protection_std * max(0.0, seasonal_trend)
        
        # ---- 5. Compute order-up-to level and order quantity ----
        order_up_to = expected_demand + safety_stock + level_adjustment + trend_adjustment
        order_qty = max(0.0, order_up_to - ip)
        
        if order_qty > 0.1:
            product_orders[pid] = round(order_qty, 1)
    
    if product_orders:
        return {"DC_0": product_orders}
    return {}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
