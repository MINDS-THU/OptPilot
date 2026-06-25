import math
from typing import Dict, Any

PRODUCT_ID = 1

def _seasonal_mean_demand(period: int, base: float = 25.0, amp: float = 5.0, season: int = 14) -> float:
    """
    Deterministic seasonal component of mean demand.
    """
    # period starts at 1
    return max(0.0, base + amp * math.sin(2.0 * math.pi * (period / season)))

def _try_get_node_name(inventory_dict: Dict[Any, Any]) -> str | None:
    """
    Best-effort extraction of node identity from inventory_dict.
    Many simulators attach metadata fields. We support a few common patterns.
    """
    for k in ("__node_name__", "node_name", "name", "node", "id", "__id__"):
        v = inventory_dict.get(k, None)
        if isinstance(v, str) and v:
            return v
    return None

def _upstream_for_retailer(node_name: str | None) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1
    Fallback to Regional_DC_0 if unknown.
    """
    if isinstance(node_name, str) and node_name.startswith("Retailer_"):
        try:
            idx = int(node_name.split("_")[-1])
            return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            return "Regional_DC_0"
    return "Regional_DC_0"

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock policy on inventory position for retailers.

    Protection period = lead_time(2) + review_period(1) = 3 days
    Service level chosen high due to high stockout cost (120) vs holding (3/day).
    """
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    node_name = _try_get_node_name(inventory_dict)
    upstream = _upstream_for_retailer(node_name)

    # Demand model (approx): mean with 14-day seasonality + random noise.
    mu = _seasonal_mean_demand(period)  # ~25 +/- 5
    sigma = 6.0  # robust daily std approximation for random component

    protection = 3.0  # days
    z = 2.05          # ~98% cycle service level

    target = mu * protection + z * sigma * math.sqrt(protection)

    # Guardrails: prevent too-low targets and extreme spikes.
    target = max(target, 70.0)          # keep some minimum coverage
    target = min(target, 160.0)         # avoid overstock at expensive retailer holding cost

    order_qty = max(0.0, target - inv_pos)

    # Optional order cap to avoid oscillation if inventory signals are noisy
    order_qty = _clamp(order_qty, 0.0, 200.0)

    return {upstream: {PRODUCT_ID: float(order_qty)}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock policy on inventory position for Regional DCs.

    Protection period = lead_time(4) + review_period(1) = 5 days
    DC demand is aggregate of 3 retailers:
      mean = 3 * retailer_mean
      std  = sqrt(3) * retailer_std  (assuming independent noise)
    Higher service level is used to prevent downstream stockouts.
    """
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    mu_r = _seasonal_mean_demand(period)
    sigma_r = 6.0

    n_retailers = 3.0
    mu = n_retailers * mu_r
    sigma = math.sqrt(n_retailers) * sigma_r

    protection = 5.0
    z = 2.33  # ~99% service level (buffer downstream)

    target = mu * protection + z * sigma * math.sqrt(protection)

    # Guardrails for DC (holding is cheap, but avoid runaway inventory)
    target = max(target, 320.0)
    target = min(target, 700.0)

    order_qty = max(0.0, target - inv_pos)
    order_qty = _clamp(order_qty, 0.0, 1200.0)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}