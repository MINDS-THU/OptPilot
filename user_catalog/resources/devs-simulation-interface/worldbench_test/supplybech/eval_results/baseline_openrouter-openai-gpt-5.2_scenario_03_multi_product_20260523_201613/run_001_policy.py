import math
from typing import Dict

# ----------------------------
# Retailer replenishment policy
# ----------------------------

HORIZON_DAYS = 100
UPSTREAM_NODE = "DC_0"
LEAD_TIME_DAYS = 2  # DC -> Retailer lead time

# Demand model parameters (mean structure used for forecasting)
PRODUCT_PARAMS = {
    # product_id: dict(base, seasonal_amp, period, noise_half_range)
    1: {"base": 18.0, "amp": 8.0, "period": 21.0, "noise": 6.0},  # Product_A
    2: {"base": 12.0, "amp": 5.0, "period": 14.0, "noise": 4.0},  # Product_B
}

# Safety stock tuning (higher -> fewer stockouts, more holding)
# Given very high stockout penalty (80) vs holding (2/day), we bias to high service.
Z_VALUE = 2.05  # ~98% one-sided service in a normal approximation

# Protection period: cover lead time demand + a small buffer (review/forecast error cushion)
EXTRA_BUFFER_DAYS = 1  # in addition to lead time


def _seasonal_mean_demand(product_id: int, day: int) -> float:
    """
    Deterministic seasonal mean forecast for a given product and day (1-indexed).
    Uses a sine wave with specified period and amplitude.
    """
    p = PRODUCT_PARAMS[product_id]
    base, amp, period = p["base"], p["amp"], p["period"]
    # Phase is set to 0; differing periods already desynchronize A/B.
    seasonal = amp * math.sin(2.0 * math.pi * (day / period))
    return max(0.0, base + seasonal)


def _noise_std(product_id: int) -> float:
    """
    Approximate standard deviation of the random noise.
    If noise is uniform in [-a, a], std = a / sqrt(3).
    """
    a = PRODUCT_PARAMS[product_id]["noise"]
    return a / math.sqrt(3.0)


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Multi-product retailer replenishment policy (order-up-to based on seasonal forecast).

    Args:
        period: current day (1..100)
        inventory_dict: {product_id: inventory_position}

    Returns:
        {"DC_0": {product_id: order_qty, ...}} or {}
    """
    # Remaining days in simulation (including today as period)
    remaining = HORIZON_DAYS - period
    if remaining <= 0:
        return {}

    protection_days = min(LEAD_TIME_DAYS + EXTRA_BUFFER_DAYS, remaining)
    if protection_days <= 0:
        return {}

    orders: Dict[int, float] = {}

    for product_id in (1, 2):
        inv_pos = float(inventory_dict.get(product_id, 0.0))

        # Expected demand over the protection window (next protection_days)
        exp_demand = 0.0
        for k in range(1, protection_days + 1):
            exp_demand += _seasonal_mean_demand(product_id, period + k)

        # Safety stock: z * sigma * sqrt(n)
        sigma = _noise_std(product_id)
        safety = Z_VALUE * sigma * math.sqrt(protection_days)

        target_inventory_position = exp_demand + safety

        order_qty = max(0.0, target_inventory_position - inv_pos)

        # Avoid tiny numerical orders
        if order_qty > 1e-6:
            orders[product_id] = float(order_qty)

    if not orders:
        return {}
    return {UPSTREAM_NODE: orders}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}