import math
from typing import Dict

# ----------------------------
# Retailer replenishment policy
# ----------------------------

def _expected_daily_demand(product_id: int, day: int) -> float:
    """
    Expected demand (noise mean = 0) with sinusoidal seasonality.
    Day starts from 1.
    """
    if product_id == 1:  # Product_A
        base = 18.0
        amp = 8.0
        period = 21.0
    elif product_id == 2:  # Product_B
        base = 12.0
        amp = 5.0
        period = 14.0
    else:
        return 0.0

    # Assume seasonality phase aligned so that day=1 corresponds to sin(2π/period)
    seasonal = amp * math.sin(2.0 * math.pi * (day / period))
    return max(0.0, base + seasonal)


def _noise_std(product_id: int) -> float:
    """
    Demand noise is uniform in [-a, +a]. Std = a / sqrt(3).
    """
    if product_id == 1:
        a = 6.0
    elif product_id == 2:
        a = 4.0
    else:
        a = 0.0
    return a / math.sqrt(3.0)


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock policy using seasonal forecast over lead time (L=2 days).
    inventory_dict values are inventory_position = on_hand + on_order - backorders.
    """
    upstream = "DC_0"
    L = 2  # lead time from DC to retailer (days)

    # Cost parameters at retailer
    holding_cost = 2.0   # per unit per day
    stockout_cost = 80.0 # per unit

    # Critical fractile (newsvendor-style) -> z for safety stock
    # fractile = Cu / (Cu + Co)
    # Here Co approximated by 1-day holding cost per unit.
    fractile = stockout_cost / (stockout_cost + holding_cost)

    # Convert fractile to z. Use a robust approximation (Acklam-like rational approx is overkill);
    # instead use a small piecewise mapping sufficient for this specific fractile range.
    # For fractile ~ 0.9756, z ~ 1.97.
    # If costs change, fall back to a simple clamp + inverse-erf approximation.
    def inv_norm_cdf(p: float) -> float:
        p = min(max(p, 1e-6), 1.0 - 1e-6)
        # Approx via inverse error function: Phi^{-1}(p)=sqrt(2)*erfinv(2p-1)
        # Python's math has erf but not erfinv; use a numeric approximation for erfinv.
        # Winitzki approximation:
        x = 2.0 * p - 1.0
        a = 0.147  # Winitzki constant
        ln = math.log(1.0 - x * x)
        first = 2.0 / (math.pi * a) + ln / 2.0
        second = ln / a
        erfinv = math.copysign(math.sqrt(max(0.0, math.sqrt(first * first - second) - first)), x)
        return math.sqrt(2.0) * erfinv

    z = inv_norm_cdf(fractile)

    orders: Dict[int, float] = {}

    for product_id in (1, 2):
        inv_pos = float(inventory_dict.get(product_id, 0.0))

        # Forecast mean demand over the next L days (days period+1 ... period+L)
        mu_L = 0.0
        for k in range(1, L + 1):
            mu_L += _expected_daily_demand(product_id, period + k)

        # Safety stock based on lead-time demand std
        sigma_d = _noise_std(product_id)
        sigma_L = math.sqrt(L) * sigma_d
        safety_stock = z * sigma_L

        # Order-up-to level
        S = mu_L + safety_stock

        q = max(0.0, S - inv_pos)

        # Avoid tiny numerical orders
        if q >= 1e-6:
            orders[product_id] = float(q)

    if not orders:
        return {}

    return {upstream: orders}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}