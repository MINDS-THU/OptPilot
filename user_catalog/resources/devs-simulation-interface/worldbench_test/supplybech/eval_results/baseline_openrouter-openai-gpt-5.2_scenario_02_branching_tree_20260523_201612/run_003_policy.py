import math
from typing import Dict, Any

PRODUCT_ID = 1

# --- Demand model approximation (used only for forecasting) ---
BASE_MEAN = 25.0          # average daily demand per retailer
SEASON_AMP = 5.0          # 14-day seasonal amplitude
SEASON_PERIOD = 14.0

# Approx demand variability (per retailer per day).
# Given "±8 random" -> treat as Uniform(-8,8): sd = 8/sqrt(3) ≈ 4.62
# Add some extra to be conservative for unmodeled randomness.
RETAILER_DAILY_SIGMA = 5.0

def _seasonal_mean(day: int) -> float:
    """Forecast mean demand for a retailer on a given day (1-indexed)."""
    # Sine seasonality; clamp to non-negative.
    mu = BASE_MEAN + SEASON_AMP * math.sin(2.0 * math.pi * (day / SEASON_PERIOD))
    return max(0.0, mu)

def _sum_forecast(period: int, horizon_days: int, multiplier: float = 1.0) -> float:
    """Sum of mean forecast from 'period' to 'period+horizon_days-1'."""
    return multiplier * sum(_seasonal_mean(d) for d in range(period, period + horizon_days))

def _get_node_name(inventory_dict: Dict[Any, Any]) -> str:
    """Best-effort extraction of node name if simulator provides it."""
    for k in ("_node", "_node_name", "node", "node_name", "name", "facility", "facility_name"):
        v = inventory_dict.get(k, None)
        if isinstance(v, str) and v:
            return v
    return ""

def _retailer_upstream_from_name(node_name: str) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1
    """
    if node_name.startswith("Retailer_"):
        try:
            idx = int(node_name.split("_", 1)[1])
            return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            pass
    # Fallback (if node name not provided): default to DC_0
    return "Regional_DC_0"

def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock policy for retailers (lead time = 2 days).
    Order-up-to level = forecast(mean demand over next (L+1) days) + safety stock.
    """
    inv_pos = _safe_float(inventory_dict.get(PRODUCT_ID, 0.0), 0.0)

    lead_time = 2
    horizon = lead_time + 1  # daily review

    # Service level tuning: high stockout cost at retailer => higher z
    z = 1.55

    forecast = _sum_forecast(period, horizon_days=horizon, multiplier=1.0)
    safety = z * RETAILER_DAILY_SIGMA * math.sqrt(horizon)

    order_up_to = forecast + safety
    order_qty = max(0.0, order_up_to - inv_pos)

    node_name = _get_node_name(inventory_dict)
    upstream = _retailer_upstream_from_name(node_name)

    return {upstream: {PRODUCT_ID: float(order_qty)}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock policy for DCs (lead time from Factory = 4 days).
    DC demand is aggregate of 3 retailers; approximate with 3x mean and sqrt(3)x sigma.
    Slightly inflate buffer to mitigate bullwhip from retailer order-up-to policies.
    """
    inv_pos = _safe_float(inventory_dict.get(PRODUCT_ID, 0.0), 0.0)

    lead_time = 4
    horizon = lead_time + 1  # daily review

    retailers_per_dc = 3.0
    sigma_agg = math.sqrt(retailers_per_dc) * RETAILER_DAILY_SIGMA

    # DC: holding is cheaper, stockout penalty still significant -> moderate-high z
    z = 1.25

    # Mild inflation to absorb upstream variability caused by retailer safety stock & batching
    mean_inflation = 1.05

    forecast = _sum_forecast(period, horizon_days=horizon, multiplier=retailers_per_dc * mean_inflation)
    safety = z * sigma_agg * math.sqrt(horizon)

    order_up_to = forecast + safety
    order_qty = max(0.0, order_up_to - inv_pos)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}