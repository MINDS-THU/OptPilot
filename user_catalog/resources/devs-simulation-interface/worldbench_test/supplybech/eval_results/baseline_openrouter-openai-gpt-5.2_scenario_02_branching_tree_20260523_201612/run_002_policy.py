import math
import builtins
import inspect
from typing import Dict, Any, Optional

# ----------------------------
# Tunable parameters (robust defaults)
# ----------------------------

PRODUCT_ID = 1

# Demand model (given by problem statement)
BASE_MEAN_DEMAND = 25.0          # per retailer per day
RANDOM_HALF_RANGE = 8.0          # uniform-ish +/- 8
SEASONAL_AMPLITUDE = 5.0         # +/- 5
SEASONAL_PERIOD = 14.0           # 14-day cycle

# Lead times
LT_RETAILER = 2  # Retailer <- DC
LT_DC = 4        # DC <- Factory
REVIEW_PERIOD = 1

# Safety factor choices (service level proxy)
# Retailer has very high stockout penalty => higher z
Z_RETAILER = 2.05   # ~98% cycle service (proxy)
# DC shortage cost is lower, but DC stockouts propagate downstream => keep fairly high
Z_DC = 1.65         # ~95% (proxy)

# Standard deviation approximation for random component:
# If demand noise is approximately Uniform(-a, a), sd = a/sqrt(3)
NOISE_SD = RANDOM_HALF_RANGE / math.sqrt(3.0)


def _seasonal_multiplier(period: int) -> float:
    """
    Deterministic seasonal component as a sine wave, mean 0, amplitude 1.
    Uses period starting at 1.
    """
    # Align with period index; exact phase is not critical in robust policies.
    return math.sin(2.0 * math.pi * (period / SEASONAL_PERIOD))


def _forecast_retailer_daily_demand(period: int) -> float:
    """Retailer daily mean forecast including deterministic seasonality."""
    return BASE_MEAN_DEMAND + SEASONAL_AMPLITUDE * _seasonal_multiplier(period)


def _get_current_node_name() -> Optional[str]:
    """
    Try hard to infer current node name from:
    - globals in this module
    - builtins (sometimes simulators stash context there)
    - caller stack locals
    Returns None if not found.
    """
    # Common names in simulators
    candidate_keys = (
        "CURRENT_NODE", "CURRENT_NODE_NAME", "NODE_NAME", "node_name",
        "current_node", "current_node_name", "node", "name"
    )

    # 1) module globals
    g = globals()
    for k in candidate_keys:
        v = g.get(k, None)
        if isinstance(v, str) and v:
            return v

    # 2) builtins
    for k in candidate_keys:
        v = getattr(builtins, k, None)
        if isinstance(v, str) and v:
            return v

    # 3) call stack locals (walk a few frames)
    frame = inspect.currentframe()
    try:
        f = frame.f_back if frame else None
        depth = 0
        while f is not None and depth < 10:
            loc = f.f_locals
            for k in candidate_keys:
                v = loc.get(k, None)
                if isinstance(v, str) and v:
                    return v
            f = f.f_back
            depth += 1
    finally:
        # Avoid reference cycles
        del frame

    return None


def _retailer_upstream_dc(node_name: Optional[str]) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1
    If node_name unknown, default to Regional_DC_0.
    """
    if isinstance(node_name, str) and node_name.startswith("Retailer_"):
        try:
            idx = int(node_name.split("_", 1)[1])
            return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            pass
    return "Regional_DC_0"


def retailer_policy_func(period: int, inventory_dict: Dict[int, float]) -> Dict[str, Dict[int, float]]:
    """
    Order-up-to policy on inventory position:
      order = max(0, S - IP)

    Protection period = LT + review = 2 + 1 = 3 days
    S = forecast_mean * protection + safety_stock
    safety_stock = z * sd * sqrt(protection)
    """
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    protection = LT_RETAILER + REVIEW_PERIOD  # 3
    mu = _forecast_retailer_daily_demand(period)
    mu = max(0.0, mu)

    sigma_prot = NOISE_SD * math.sqrt(protection)
    safety = Z_RETAILER * sigma_prot

    S = mu * protection + safety

    order_qty = max(0.0, S - ip)

    node_name = _get_current_node_name()
    upstream = _retailer_upstream_dc(node_name)

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: Dict[int, float]) -> Dict[str, Dict[int, float]]:
    """
    DC order-up-to policy with aggregated demand.

    Each DC serves 3 retailers:
      mean = 3 * retailer_mean
      sd (independent noise) = sqrt(3) * retailer_sd

    Protection period = LT + review = 4 + 1 = 5 days
    """
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    protection = LT_DC + REVIEW_PERIOD  # 5

    retailer_mu = _forecast_retailer_daily_demand(period)
    retailer_mu = max(0.0, retailer_mu)

    mu = 3.0 * retailer_mu
    # Aggregate noise sd across 3 retailers
    sigma_daily = math.sqrt(3.0) * NOISE_SD
    sigma_prot = sigma_daily * math.sqrt(protection)
    safety = Z_DC * sigma_prot

    S = mu * protection + safety

    order_qty = max(0.0, S - ip)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}