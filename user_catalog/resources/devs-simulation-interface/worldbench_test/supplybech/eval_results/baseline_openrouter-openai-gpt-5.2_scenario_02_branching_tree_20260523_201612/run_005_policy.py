import math
from typing import Dict, Any

# -----------------------
# Internal helpers/state
# -----------------------

_STATE = {
    "dc_prev_order": {},        # key: node_name -> float
    "ret_prev_order": {},       # key: node_name -> float
}

def _get_node_name(inventory_dict: Dict[Any, Any]) -> str:
    """
    The simulator sometimes provides metadata inside inventory_dict.
    We try a few common keys; otherwise fall back to "".
    """
    for k in ("node", "node_name", "_node_name", "name", "__node_name__", "__node__"):
        v = inventory_dict.get(k, None)
        if isinstance(v, str) and v:
            return v
    return ""

def _sin_season(period: int, amp: float, cycle: int = 14, phase_shift: float = 0.0) -> float:
    # period starts from 1
    return amp * math.sin(2.0 * math.pi * ((period + phase_shift) / cycle))

def _order_up_to(ip: float, S: float) -> float:
    return max(0.0, float(S - ip))

def _norm_sd_uniform(a: float) -> float:
    """
    For Uniform(-a, a): sd = a/sqrt(3)
    """
    return a / math.sqrt(3.0)

# -----------------------
# Policies
# -----------------------

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer order-up-to based on inventory position.
    Protection period uses (L + 1) with daily review; L=2 => 3 days.
    """
    node_name = _get_node_name(inventory_dict)

    # Infer upstream DC by retailer index if possible
    upstream = None
    if node_name.startswith("Retailer_"):
        try:
            idx = int(node_name.split("_")[-1])
            upstream = "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            upstream = None
    # Fallback: try explicit upstream metadata if provided
    if upstream is None:
        upstream = inventory_dict.get("upstream", None) or inventory_dict.get("_upstream", None)
    # Last resort (should rarely happen)
    if upstream is None:
        upstream = "Regional_DC_0"

    product_id = 1
    ip = float(inventory_dict.get(product_id, 0.0))

    # Demand model (given in prompt): mean ~25/day, seasonal +/-5, random +/-8
    base_mu = 25.0
    mu_t = base_mu + _sin_season(period, amp=5.0, cycle=14)
    mu_t = max(0.0, mu_t)

    # Protection period: lead time 2 + 1 day review buffer
    P = 3.0

    # Random component sd: Uniform(-8,8)
    sd_day = _norm_sd_uniform(8.0)

    # Service factor: high stockout penalty (120) vs holding (3/day) => high service
    # Choose a fairly high z; keep it stable for robustness.
    z = 2.1

    mu_P = mu_t * P
    sd_P = sd_day * math.sqrt(P)
    S = mu_P + z * sd_P

    # Mild smoothing to reduce oscillation, but do not slow down if IP is negative.
    desired = _order_up_to(ip, S)
    if ip < 0.0:
        order_qty = desired
    else:
        alpha = 0.35
        prev = float(_STATE["ret_prev_order"].get(node_name, desired))
        order_qty = alpha * desired + (1.0 - alpha) * prev

    order_qty = max(0.0, float(order_qty))
    _STATE["ret_prev_order"][node_name] = order_qty

    return {upstream: {product_id: order_qty}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC order-up-to based on inventory position, with smoothing to mitigate bullwhip.
    Protection period uses (L + 1) with daily review; L=4 => 5 days.
    Downstream aggregation: 3 retailers per DC.
    """
    node_name = _get_node_name(inventory_dict)

    product_id = 1
    ip = float(inventory_dict.get(product_id, 0.0))

    # Aggregate demand for 3 retailers
    base_mu_r = 25.0
    mu_r_t = base_mu_r + _sin_season(period, amp=5.0, cycle=14)
    mu_r_t = max(0.0, mu_r_t)

    mu_dc_t = 3.0 * mu_r_t

    # Protection period: lead time 4 + 1 day review buffer
    P = 5.0

    # Aggregate random sd: sum of 3 independent retailers
    sd_day_r = _norm_sd_uniform(8.0)
    sd_day_dc = math.sqrt(3.0) * sd_day_r

    # DC stockout penalty (40) vs holding (0.8/day): moderate-high service, but less than retailer
    z = 1.7

    mu_P = mu_dc_t * P
    sd_P = sd_day_dc * math.sqrt(P)
    S = mu_P + z * sd_P

    desired = _order_up_to(ip, S)

    # Smoothing (stronger than retailer) to reduce amplification,
    # but respond immediately when IP is negative (backlog risk).
    if ip < 0.0:
        order_qty = desired
    else:
        alpha = 0.45
        prev = float(_STATE["dc_prev_order"].get(node_name, desired))
        order_qty = alpha * desired + (1.0 - alpha) * prev

    order_qty = max(0.0, float(order_qty))
    _STATE["dc_prev_order"][node_name] = order_qty

    return {"Factory_0": {product_id: order_qty}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}