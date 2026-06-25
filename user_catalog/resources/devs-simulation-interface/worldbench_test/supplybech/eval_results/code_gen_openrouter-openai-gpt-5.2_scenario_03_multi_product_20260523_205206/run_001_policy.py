
import math
from typing import Dict

# =========================
# Retailer replenishment policy (multi-product)
# =========================
# Implements a seasonal-forecast order-up-to (base-stock) policy using inventory_position:
#   inventory_position = on_hand + on_order - backlog
#
# Order-up-to level per product:
#   S = sum_{i=1..H} E[D(t+i)] + z * sigma * sqrt(H)
#
# where:
#   - H = lead_time (2) + buffer_days (per-product)
#   - E[D] uses the known seasonal mean model (sinusoid)
#   - sigma approximates the noise using a uniform [-r, r] assumption: r / sqrt(3)
#
# Returns:
#   {}                          if no order
#   {"DC_0": {pid: qty, ...}}   otherwise
# =========================

UPSTREAM_NODE = "DC_0"

PRODUCTS = {
    # product_id: parameters from problem statement
    1: {"base": 18.0, "noise_range": 6.0, "seasonal_amp": 8.0, "period": 21.0},  # Product_A
    2: {"base": 12.0, "noise_range": 4.0, "seasonal_amp": 5.0, "period": 14.0},  # Product_B
}

def _expected_demand(pid: int, day: int) -> float:
    """
    Expected (mean) demand with seasonality.

    Note: The exact simulator phase is not provided; we use a standard convention:
        E[D(day)] = base + amp * sin(2*pi*day/period)
    clipped at >= 0.
    """
    p = PRODUCTS[pid]
    base = p["base"]
    amp = p["seasonal_amp"]
    per = p["period"]
    val = base + amp * math.sin(2.0 * math.pi * (day / per))
    return max(0.0, val)

def _noise_sigma(pid: int) -> float:
    """If noise is uniform in [-r, r], sigma = r / sqrt(3)."""
    r = PRODUCTS[pid]["noise_range"]
    return r / math.sqrt(3.0)

def retailer_policy_func(period: int, inventory_dict: Dict[int, float]) -> Dict[str, Dict[int, float]]:
    # Lead time from DC to Retailer (given)
    L = 2

    # Per-product tuning:
    # Product_A higher mean/volatility => slightly longer protection horizon.
    buffer_days = {1: 3, 2: 2}     # H = L + buffer
    z_factor = {1: 1.70, 2: 1.60}  # safety stock multiplier

    orders: Dict[int, float] = {}

    for pid in (1, 2):
        inv_pos = float(inventory_dict.get(pid, 0.0))

        H = L + int(buffer_days.get(pid, 2))

        # Mean demand over the protection horizon
        mean_dem = 0.0
        for i in range(1, H + 1):
            mean_dem += _expected_demand(pid, period + i)

        # Safety stock for noise over H days
        sigma_h = _noise_sigma(pid) * math.sqrt(H)
        safety = float(z_factor.get(pid, 1.6)) * sigma_h

        S = mean_dem + safety

        qty = S - inv_pos
        if qty > 1e-9:
            # Round up to integer units to reduce stockout risk
            orders[pid] = float(math.ceil(qty))

    if not orders:
        return {}
    return {UPSTREAM_NODE: orders}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
