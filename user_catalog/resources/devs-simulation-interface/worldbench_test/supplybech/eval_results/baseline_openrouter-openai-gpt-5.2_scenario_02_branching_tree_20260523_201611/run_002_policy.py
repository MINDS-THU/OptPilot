import math
from typing import Dict, Any, Optional

PRODUCT_ID = 1

# ----------------------------
# Demand model (known structure)
# ----------------------------
BASE_MEAN = 25.0          # avg daily demand per retailer
RAND_RANGE = 8.0          # +/-8 (assume roughly uniform noise)
SEASON_AMP = 5.0          # +/-5 seasonal amplitude
SEASON_PERIOD = 14        # 14-day seasonality

# Noise std approximation for Uniform(-a, a): a / sqrt(3)
SIGMA_RETAIL = RAND_RANGE / math.sqrt(3)  # ~4.62
SIGMA_DC = math.sqrt(3) * SIGMA_RETAIL    # aggregation of 3 retailers (assume independent)

# Lead times (days)
LT_RETAIL = 2
LT_DC = 4

# Review period (days) - we place orders daily, so 1 day
REVIEW = 1

# Control parameters (tuned for high retailer stockout penalty vs holding)
Z_RETAIL = 1.6   # service factor at retailers
Z_DC = 1.2       # service factor at DCs (holding cheaper, but stockout penalty lower than retailer)

# Optional order smoothing (mainly for DC to mitigate bullwhip)
DC_SMOOTH_ALPHA = 0.6

# Hard caps (safety to avoid runaway ordering if inventory_position becomes very negative)
MAX_ORDER_RETAIL = 250.0
MAX_ORDER_DC = 2000.0

# Global memory (persists across calls in a single simulation run)
_STATE: Dict[str, Dict[str, float]] = {}


def _seasonal_mean(day_index_0_based: int) -> float:
    """Expected mean demand for a retailer on a given day (0-based)."""
    season = SEASON_AMP * math.sin(2.0 * math.pi * (day_index_0_based % SEASON_PERIOD) / SEASON_PERIOD)
    return BASE_MEAN + season


def _expected_demand_over_horizon(start_day_0_based: int, horizon_days: int, scale: float = 1.0) -> float:
    """Sum of forecast means over next horizon_days, scaled (e.g., DC serves 3 retailers)."""
    return sum(_seasonal_mean(start_day_0_based + i) for i in range(1, horizon_days + 1)) * scale


def _get_node_name(kwargs: Dict[str, Any]) -> Optional[str]:
    """Try to infer node name if simulator passes it in kwargs under various keys."""
    for k in ("node_name", "name", "agent_id", "node", "facility", "entity_name"):
        v = kwargs.get(k, None)
        if isinstance(v, str) and v:
            return v
    return None


def _retailer_upstream(node_name: Optional[str]) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1

    If node_name is unavailable, default to Regional_DC_0 (framework may ignore invalid keys anyway).
    """
    if not node_name:
        return "Regional_DC_0"
    try:
        idx = int(str(node_name).split("_")[-1])
        return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
    except Exception:
        return "Regional_DC_0"


def retailer_policy_func(period: int, inventory_dict: dict, **kwargs) -> dict:
    """
    Base-stock (order-up-to) policy using known seasonal mean + safety stock.
    inventory_dict: {product_id: inventory_position}
    Returns: {"Regional_DC_X": {1: order_qty}}
    """
    node_name = _get_node_name(kwargs) or "Retailer_UNKNOWN"
    upstream = _retailer_upstream(node_name)

    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    # Horizon covers lead time + review period
    horizon = LT_RETAIL + REVIEW
    t0 = period - 1  # 0-based "today"
    mu = _expected_demand_over_horizon(t0, horizon, scale=1.0)

    safety = Z_RETAIL * SIGMA_RETAIL * math.sqrt(horizon)
    S = mu + safety

    order_qty = max(0.0, S - inv_pos)
    order_qty = min(order_qty, MAX_ORDER_RETAIL)

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict, **kwargs) -> dict:
    """
    DC base-stock with mild order smoothing to reduce bullwhip.
    inventory_dict: {product_id: inventory_position}
    Returns: {"Factory_0": {1: order_qty}}
    """
    node_name = _get_node_name(kwargs) or "Regional_DC_UNKNOWN"
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    horizon = LT_DC + REVIEW
    t0 = period - 1

    # DC demand is aggregate of 3 retailers it serves
    mu = _expected_demand_over_horizon(t0, horizon, scale=3.0)
    safety = Z_DC * SIGMA_DC * math.sqrt(horizon)
    S = mu + safety

    desired = max(0.0, S - inv_pos)

    # Smoothing (per-DC state)
    st = _STATE.setdefault(node_name, {})
    last_order = float(st.get("last_order", 0.0))
    order_qty = DC_SMOOTH_ALPHA * desired + (1.0 - DC_SMOOTH_ALPHA) * last_order

    order_qty = max(0.0, min(order_qty, MAX_ORDER_DC))
    st["last_order"] = float(order_qty)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}