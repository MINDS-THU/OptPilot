import math
from typing import Dict

# --- Retailer replenishment policy (multi-product) ---

_PRODUCT_PARAMS = {
    # product_id: (base, seasonal_amp, period, noise_half_range)
    1: (18.0, 8.0, 21.0, 6.0),  # Product_A
    2: (12.0, 5.0, 14.0, 4.0),  # Product_B
}

_UPSTREAM_NODE = "DC_0"

def _expected_demand(pid: int, day: int) -> float:
    """
    Deterministic expectation of demand (noise mean=0), with sinusoidal seasonality.
    day is 1-indexed.
    """
    base, amp, per, _noise = _PRODUCT_PARAMS[pid]
    # Sinusoidal seasonal component; expectation over noise is zero.
    val = base + amp * math.sin(2.0 * math.pi * (day / per))
    return max(0.0, val)

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Order-up-to policy using inventory position (on-hand + pipeline - backlog).
    Incorporates seasonality forecast and safety stock from noise.
    """
    # Lead time Retailer <- DC is 2 days; add +1 day buffer to reduce stockout risk
    L = 2
    H = L + 1  # forecast horizon for target inventory position

    # Safety factor: tuned for high stockout penalty (80) vs holding cost (2/day)
    z = 1.7

    orders: Dict[int, float] = {}

    for pid in (1, 2):
        inv_pos = float(inventory_dict.get(pid, 0.0))

        base, amp, per, noise_half_range = _PRODUCT_PARAMS[pid]

        # Forecast expected demand over next H days (period+1 ... period+H)
        mu = 0.0
        for k in range(1, H + 1):
            mu += _expected_demand(pid, period + k)

        # Demand uncertainty: noise is uniform in [-a, a], std = a/sqrt(3)
        sigma_day = noise_half_range / math.sqrt(3.0)
        safety = z * sigma_day * math.sqrt(H)

        # Simple trend buffer: if approaching rising seasonal phase, add a bit more
        d_now = _expected_demand(pid, period)
        d_end = _expected_demand(pid, period + H)
        trend = max(0.0, d_end - d_now)
        trend_buffer = 0.30 * trend * H

        # Target inventory position (order-up-to level)
        S = max(0.0, mu + safety + trend_buffer)

        # Order to raise inventory position to S
        q = max(0.0, S - inv_pos)

        # Deadband to avoid tiny orders; cap to avoid extreme spikes
        if q < 0.1:
            q = 0.0
        q = min(q, 300.0)

        if q > 0.0:
            orders[pid] = float(q)

    if not orders:
        return {}
    return {_UPSTREAM_NODE: orders}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}