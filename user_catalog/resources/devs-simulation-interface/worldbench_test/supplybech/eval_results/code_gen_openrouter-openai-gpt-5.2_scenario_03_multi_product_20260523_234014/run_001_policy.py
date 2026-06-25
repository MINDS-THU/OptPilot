
import math

# Multi-product retailer replenishment policy for ordering from "DC_0".
# Uses inventory_position = on_hand + on_order - backlog.
#
# Strategy: seasonality-aware base-stock (order-up-to) using a short forecast
# horizon (covers the 2-day lead time) plus safety stock for random noise.

SIM_HORIZON = 100
LEAD_TIME = 2  # DC -> Retailer lead time (days)

# Demand parameters (from problem statement)
PRODUCT_PARAMS = {
    1: {"base": 18.0, "noise": 6.0, "amp": 8.0, "period": 21.0},  # Product_A
    2: {"base": 12.0, "noise": 4.0, "amp": 5.0, "period": 14.0},  # Product_B
}

# Tuned parameters (local multi-seed optimization under given costs)
BASE_HORIZON = 2   # days of forecast demand to cover in inventory position
Z = 1.8            # safety factor on demand noise
K_AMP = 0.0        # optional extra seasonality cushion (kept at 0)

def _expected_demand(pid: int, t: int) -> float:
    """Expected demand (no noise) with sinusoidal seasonality."""
    p = PRODUCT_PARAMS[pid]
    return p["base"] + p["amp"] * math.sin(2.0 * math.pi * (t / p["period"]))

def _noise_std(pid: int) -> float:
    """Std dev for uniform noise in [-a, a]."""
    a = PRODUCT_PARAMS[pid]["noise"]
    return a / math.sqrt(3.0)

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: current day (1..100)
        inventory_dict: {product_id: inventory_position}

    Returns:
        {"DC_0": {product_id: order_qty}} or {}.
    """
    if not inventory_dict:
        return {}

    remaining = SIM_HORIZON - int(period) + 1
    # If an order cannot arrive before the horizon ends, do not place it.
    if remaining <= LEAD_TIME:
        return {}

    horizon = min(BASE_HORIZON, remaining)

    order_to_dc = {}
    for pid, ip in inventory_dict.items():
        if pid not in PRODUCT_PARAMS:
            continue

        ip = float(ip)

        # Forecast demand sum over the next "horizon" days (starting tomorrow).
        forecast = 0.0
        for k in range(1, horizon + 1):
            forecast += _expected_demand(pid, period + k)

        safety = Z * _noise_std(pid) * math.sqrt(horizon) + K_AMP * PRODUCT_PARAMS[pid]["amp"]
        target_ip = max(0.0, forecast + safety)

        qty = target_ip - ip
        if qty > 0:
            order_to_dc[pid] = qty

    return {"DC_0": order_to_dc} if order_to_dc else {}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
