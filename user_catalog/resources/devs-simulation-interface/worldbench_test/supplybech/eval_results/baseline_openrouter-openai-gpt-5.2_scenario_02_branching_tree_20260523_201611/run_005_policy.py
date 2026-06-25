import math
from typing import Dict, Any

PRODUCT_ID = 1

# ---- Tunable parameters (can be adjusted) ----
# Demand model approximations (per retailer, per day)
BASE_MEAN = 25.0          # average demand
SEASON_AMP = 5.0          # +/- amplitude, 14-day cycle
SEASON_PERIOD = 14.0

# Approx demand std per retailer per day (random +/-8 plus some other variability)
# Uniform(-8,8) std = 8/sqrt(3)=4.62; add some extra for model mismatch -> ~6.5
RETAILER_SIGMA = 6.5

# Service factor (z) - higher at retailer due to high stockout cost (120)
RETAILER_Z = 1.65

# DC sees ~3 retailers; assume independent random parts, but shared seasonality.
# We'll approximate sigma aggregation with sqrt(n) for random component and a small add-on.
DC_N_RETAILERS = 3
DC_Z = 1.75

# Lead times (days)
LT_RETAILER = 2  # Retailer <- DC
LT_DC = 4        # DC <- Factory

# Review period is daily; for order-up-to, protection period ≈ LT + 1
REVIEW_PERIOD = 1


def _seasonal_mean(period: int) -> float:
    """
    Expected mean demand per retailer for given day (1..100), using a 14-day sinusoid.
    If your simulator's seasonality phase differs, you can shift the angle here.
    """
    # Sinusoid in [-1,1]
    angle = 2.0 * math.pi * (period % SEASON_PERIOD) / SEASON_PERIOD
    return BASE_MEAN + SEASON_AMP * math.sin(angle)


def _order_up_to_level(mu: float, sigma: float, lead_time: int, z: float) -> float:
    """
    Base-stock target S for inventory position:
        S = mu * (L + R) + z * sigma * sqrt(L + R)
    where R is review period (daily ordering -> 1).
    """
    protection = lead_time + REVIEW_PERIOD
    return mu * protection + z * sigma * math.sqrt(protection)


def _infer_node_name(inventory_dict: Dict[Any, Any]) -> str:
    """
    Try to infer the current node name if the simulator passes it via inventory_dict metadata.
    Supports several common keys.
    """
    for k in ("node_name", "_node_name", "__node_name__", "name", "node", "node_id", "__node_id__"):
        if k in inventory_dict and isinstance(inventory_dict[k], str):
            return inventory_dict[k]
    return ""


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer order-up-to policy:
    - Computes a seasonal expected mean demand
    - Computes base-stock target for inventory position
    - Orders max(0, S - inventory_position)
    """
    node_name = _infer_node_name(inventory_dict)

    # Decide upstream DC based on retailer index (Retailer_0/1/2 -> DC_0, Retailer_3/4/5 -> DC_1)
    # Fallback: default to Regional_DC_0 if node name is unavailable.
    upstream = "Regional_DC_0"
    if node_name:
        # Extract trailing integer if present
        idx = None
        try:
            if "Retailer_" in node_name:
                idx = int(node_name.split("Retailer_")[-1])
        except Exception:
            idx = None
        if idx is not None and idx >= 3:
            upstream = "Regional_DC_1"

    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    mu = _seasonal_mean(period)  # per-retailer
    sigma = RETAILER_SIGMA

    S = _order_up_to_level(mu=mu, sigma=sigma, lead_time=LT_RETAILER, z=RETAILER_Z)
    order_qty = max(0.0, S - inv_pos)

    # Optional mild quantization (keeps floats but avoids tiny jitter)
    if order_qty < 1e-6:
        order_qty = 0.0

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC order-up-to policy (to Factory):
    - Uses aggregated seasonal mean (3 retailers)
    - Uses aggregated sigma approximation
    - Orders to restore DC inventory position to target S
    """
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    mu_retail = _seasonal_mean(period)
    mu = DC_N_RETAILERS * mu_retail

    # Aggregate sigma: random components ~ sqrt(n); add small extra for correlated seasonality/model error
    sigma = math.sqrt(DC_N_RETAILERS) * RETAILER_SIGMA + 2.0

    S = _order_up_to_level(mu=mu, sigma=sigma, lead_time=LT_DC, z=DC_Z)
    order_qty = max(0.0, S - inv_pos)

    if order_qty < 1e-6:
        order_qty = 0.0

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}