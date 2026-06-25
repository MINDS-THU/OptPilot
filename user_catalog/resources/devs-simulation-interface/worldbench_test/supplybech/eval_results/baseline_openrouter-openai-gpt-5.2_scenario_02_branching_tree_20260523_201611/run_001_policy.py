import math
import re
from typing import Dict, Any

PRODUCT_ID = 1

# -----------------------------
# Utilities
# -----------------------------
def _get_node_name(kwargs: Dict[str, Any]) -> str | None:
    """
    Try common keys used by simulators to pass current node identity.
    """
    for k in ("node_name", "name", "agent_id", "agent_name", "node", "current_node"):
        v = kwargs.get(k, None)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_index(node_name: str) -> int | None:
    """
    Extract trailing integer from names like 'Retailer_4' or 'Regional_DC_1'.
    """
    if not node_name:
        return None
    m = re.search(r"_(\d+)$", node_name)
    return int(m.group(1)) if m else None


def _seasonal_multiplier(period: int, cycle: int = 14) -> float:
    """
    Deterministic seasonal pattern aligned to a 14-day cycle.
    Returns sin term in [-1, 1].
    """
    # Use period starting at 1; phase choice is arbitrary but consistent.
    return math.sin(2.0 * math.pi * (period / cycle))


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# -----------------------------
# Policy functions
# -----------------------------
def retailer_policy_func(period: int, inventory_dict: dict, **kwargs) -> dict:
    """
    Base-stock (order-up-to) policy for Retailers.
    inventory_dict values are inventory position = on_hand + on_order - backorder.
    """
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    node_name = _get_node_name(kwargs) or ""
    idx = _extract_index(node_name)

    # Map retailer -> its upstream DC
    if idx is None:
        upstream = "Regional_DC_0"  # fallback if node identity is not provided
    else:
        upstream = "Regional_DC_0" if idx < 3 else "Regional_DC_1"

    # Demand model (approx): mean 25, seasonal amplitude 5, random ±8 (handled via safety stock)
    base_mean = 25.0
    seasonal_amp = 5.0
    season = _seasonal_multiplier(period, 14)
    forecast_daily = base_mean + seasonal_amp * season  # deterministic forecast

    # Lead time from DC is 2 days; with daily review add 1 day protection
    L = 2.0
    protection_days = L + 1.0  # 3 days coverage

    # Random component: ±8 plus misc -> use std ~ 6 as a robust proxy
    demand_std = 6.0

    # High stockout penalty at retailer -> high service level (z ~ 1.65)
    z = 1.65

    # Safety stock for protection horizon
    safety = z * demand_std * math.sqrt(protection_days)

    # Extra buffer for peak seasonality over the protection window
    seasonal_buffer = max(0.0, seasonal_amp * protection_days)

    # Order-up-to level
    S = forecast_daily * protection_days + safety + 0.5 * seasonal_buffer

    # Avoid too aggressive stocking due to high holding cost at retailer
    # Keep S within a reasonable band
    S = _clip(S, lo=60.0, hi=160.0)

    order_qty = max(0.0, S - inv_pos)

    # Limit single-day spikes to reduce bullwhip (retailer can't suddenly order too much)
    order_qty = _clip(order_qty, 0.0, 200.0)

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict, **kwargs) -> dict:
    """
    Base-stock policy for Regional DCs ordering from Factory.
    """
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    # Each DC serves 3 retailers
    retailers_per_dc = 3

    # Aggregate forecast from retailer layer
    base_mean_r = 25.0
    seasonal_amp_r = 5.0
    season = _seasonal_multiplier(period, 14)

    forecast_daily_r = base_mean_r + seasonal_amp_r * season
    forecast_daily_dc = retailers_per_dc * forecast_daily_r  # mean ~75, seasonal amp ~15

    # Factory lead time is 4 days; with daily review add 1 day protection
    L = 4.0
    protection_days = L + 1.0  # 5 days coverage

    # Aggregate uncertainty: std grows with sqrt(n)
    demand_std_r = 6.0
    demand_std_dc = math.sqrt(retailers_per_dc) * demand_std_r  # ~10.4

    # DC shortage penalty is lower than retailer, but still significant -> moderately high service
    z = 1.40  # slightly lower than retailer to reduce DC holding and bullwhip

    safety = z * demand_std_dc * math.sqrt(protection_days)

    seasonal_amp_dc = retailers_per_dc * seasonal_amp_r  # 15
    seasonal_buffer = max(0.0, seasonal_amp_dc * protection_days)

    S = forecast_daily_dc * protection_days + safety + 0.4 * seasonal_buffer

    # Reasonable bounds for DC (avoid extreme overstock / understock)
    S = _clip(S, lo=250.0, hi=900.0)

    order_qty = max(0.0, S - inv_pos)

    # Smooth spikes at DC as well (bullwhip control)
    order_qty = _clip(order_qty, 0.0, 2000.0)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}