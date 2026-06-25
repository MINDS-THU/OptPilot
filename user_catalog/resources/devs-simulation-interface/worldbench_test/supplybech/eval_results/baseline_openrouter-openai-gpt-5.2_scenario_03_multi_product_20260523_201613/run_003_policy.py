import math
from typing import Dict, Any

# --- Demand model parameters (given by problem statement) ---
PRODUCT_PARAMS = {
    1: {  # Product_A
        "base": 18.0,
        "noise_half_range": 6.0,     # noise in ±6
        "seasonal_amp": 8.0,
        "seasonal_period": 21.0,
    },
    2: {  # Product_B
        "base": 12.0,
        "noise_half_range": 4.0,     # noise in ±4
        "seasonal_amp": 5.0,
        "seasonal_period": 14.0,
    }
}

UPSTREAM_NODE = "DC_0"
LEAD_TIME_DAYS = 2  # Retailer replenishment lead time from DC


def _expected_daily_demand(product_id: int, day: int) -> float:
    """
    Deterministic forecast of expected demand for a given product and day index (1-based).
    Seasonal term modeled as sinusoid; noise has mean 0 and is handled in safety stock.
    """
    p = PRODUCT_PARAMS[product_id]
    base = p["base"]
    amp = p["seasonal_amp"]
    period = p["seasonal_period"]

    seasonal = amp * math.sin(2.0 * math.pi * (day / period))
    mu = base + seasonal
    return max(0.0, mu)


def _noise_std_per_day(product_id: int) -> float:
    """
    Noise is assumed uniform in [-a, a], std = a / sqrt(3).
    """
    a = PRODUCT_PARAMS[product_id]["noise_half_range"]
    return a / math.sqrt(3.0)


def retailer_policy_func(period: int, inventory_dict: Dict[int, float]) -> Dict[str, Dict[int, float]]:
    """
    Multi-product base-stock policy with seasonal mean forecast and safety stock.
    inventory_dict provides inventory position (on-hand + on-order - backorders).
    """
    # Forecast horizon: cover lead time plus a small extra buffer (review period ~= 1 day)
    horizon = LEAD_TIME_DAYS + 1  # days

    # High shortage penalty vs holding cost -> target high service level
    # z≈1.88 corresponds to ~97% cycle service (normal approximation)
    z = 1.88

    order_plan: Dict[int, float] = {}

    for product_id, inv_pos in inventory_dict.items():
        if product_id not in PRODUCT_PARAMS:
            continue

        # Mean demand over horizon (sum of expected daily demand for the next 'horizon' days)
        mean_h = 0.0
        for d in range(period + 1, period + horizon + 1):
            mean_h += _expected_daily_demand(product_id, d)

        # Safety stock from noise over the horizon
        sigma_day = _noise_std_per_day(product_id)
        sigma_h = sigma_day * math.sqrt(horizon)
        safety = z * sigma_h

        # Order-up-to level
        S = mean_h + safety

        # Order quantity based on inventory position
        q = max(0.0, S - float(inv_pos))

        # Optional small deadband to avoid micro-orders
        if q < 0.5:
            q = 0.0

        if q > 0.0:
            # Keep as float; simulator typically accepts floats
            order_plan[product_id] = q

    if not order_plan:
        return {}

    return {UPSTREAM_NODE: order_plan}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}