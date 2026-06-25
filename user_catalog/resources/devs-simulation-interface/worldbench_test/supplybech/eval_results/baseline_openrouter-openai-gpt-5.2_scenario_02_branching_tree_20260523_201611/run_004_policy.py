import math
from typing import Dict, Any

# ----------------------------
# Helper utilities
# ----------------------------

_PRODUCT_ID = 1

def _seasonal_mean_demand(period: int) -> float:
    """
    Expected daily mean demand with 14-day seasonality.
    Base ~25, seasonal amplitude ~5.
    """
    base = 25.0
    amp = 5.0
    # period starts at 1; keep season continuous
    season = amp * math.sin(2.0 * math.pi * (period / 14.0))
    mu = base + season
    return max(0.0, mu)

def _sigma_daily() -> float:
    """
    Random fluctuation is about +/-8 (roughly uniform), std ~ 8/sqrt(3).
    """
    return 8.0 / math.sqrt(3.0)

def _get_inventory_position(inventory_dict: dict, product_id: int = _PRODUCT_ID) -> float:
    """
    inventory_dict is expected like {1: 120.5}. Be defensive if wrapped.
    """
    if inventory_dict is None:
        return 0.0
    # Common variants: {1: x} or {"inventory": {1: x}}
    if product_id in inventory_dict and isinstance(inventory_dict[product_id], (int, float)):
        return float(inventory_dict[product_id])
    inv = inventory_dict.get("inventory") if isinstance(inventory_dict, dict) else None
    if isinstance(inv, dict) and product_id in inv:
        return float(inv[product_id])
    # fallback: try string key
    if str(product_id) in inventory_dict:
        return float(inventory_dict[str(product_id)])
    return 0.0

def _order_up_to(ip: float, target: float, cap: float = None) -> float:
    raw = max(0.0, target - ip)
    if cap is not None:
        raw = min(raw, cap)
    return float(raw)

# State for mild smoothing at DC level (optional but helpful to reduce bullwhip)
_DC_LAST_ORDER: Dict[str, float] = {}

def _detect_node_name(inventory_dict: dict) -> str:
    """
    Best-effort extraction of current node name if simulator passes metadata.
    If unavailable, return empty string.
    """
    if not isinstance(inventory_dict, dict):
        return ""
    for k in ("node_name", "name", "current_node", "node", "_node_name", "_name"):
        v = inventory_dict.get(k)
        if isinstance(v, str):
            return v
    return ""

# ----------------------------
# Policy functions
# ----------------------------

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer: daily order-up-to (base-stock) with high service level.
    Lead time retailer<-DC is 2 days. Daily review => protection period = L + 1 = 3.
    """
    ip = _get_inventory_position(inventory_dict, _PRODUCT_ID)

    L = 2.0
    protection = L + 1.0  # periodic review 1 day
    mu = _seasonal_mean_demand(period)
    sigma = _sigma_daily()

    # High service level due to very high stockout penalty at retailer (120/unit)
    z = 2.05  # ~98% one-sided
    target = mu * protection + z * sigma * math.sqrt(protection)

    # Mild cap to avoid extreme spikes (still generous enough to prevent chronic stockouts)
    cap = 2.5 * mu * protection + 30.0

    q = _order_up_to(ip, target, cap=cap)

    # Return both possible upstream DCs; simulator should accept the valid one for this retailer.
    return {
        "Regional_DC_0": {_PRODUCT_ID: q},
        "Regional_DC_1": {_PRODUCT_ID: q},
    }

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Regional DC: base-stock on aggregated demand of 3 retailers, with mild smoothing.
    Lead time DC<-Factory is 4 days. Daily review => protection period = L + 1 = 5.
    """
    ip = _get_inventory_position(inventory_dict, _PRODUCT_ID)
    node_name = _detect_node_name(inventory_dict) or "Regional_DC"

    L = 4.0
    protection = L + 1.0  # 5 days

    mu_r = _seasonal_mean_demand(period)
    sigma_r = _sigma_daily()

    # Each DC serves 3 retailers
    n = 3.0
    mu = n * mu_r
    sigma = math.sqrt(n) * sigma_r

    # DC stockout penalty is lower (40/unit) and holding is low (0.8/unit/day),
    # but still keep fairly high service to protect retailers.
    z = 1.88  # ~97% one-sided
    target = mu * protection + z * sigma * math.sqrt(protection)

    # Raw order-up-to
    raw_cap = 2.2 * mu * protection + 120.0
    raw_q = _order_up_to(ip, target, cap=raw_cap)

    # Mild smoothing to reduce bullwhip (without starving replenishment)
    last = _DC_LAST_ORDER.get(node_name, raw_q)
    beta = 0.30  # weight on previous order
    q_smooth = beta * last + (1.0 - beta) * raw_q

    # Guardrails: don't smooth too low/high vs raw
    q = max(0.0, q_smooth)
    q = max(q, 0.70 * raw_q)                 # avoid under-ordering
    q = min(q, 1.30 * raw_q + 30.0)          # avoid overshoot

    _DC_LAST_ORDER[node_name] = float(q)

    return {"Factory_0": {_PRODUCT_ID: float(q)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}