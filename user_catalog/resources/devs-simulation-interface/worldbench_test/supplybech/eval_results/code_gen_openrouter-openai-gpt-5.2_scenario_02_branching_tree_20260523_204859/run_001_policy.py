
# final_policy.py
# Replenishment policy module for the 3-tier supply chain scenario.
#
# Provides:
#   - retailer_policy_func(period, inventory_dict) -> dict
#   - dc_policy_func(period, inventory_dict) -> dict
#   - POLICY_MOUNTS mapping node groups to policy functions
#
# Policy design:
#   - Retailers: base-stock (order-up-to) over 2-day lead time with seasonal mean and safety stock
#   - DCs: base-stock over 4-day lead time for aggregate of 3 retailers + order smoothing to reduce bullwhip
#
# Notes on robustness:
#   - inventory_dict is expected to contain inventory position for product_id=1, like {1: inv_pos}
#   - If the simulator provides node identity inside inventory_dict (e.g., node_name), we use it to:
#       * route retailer orders to the correct upstream DC
#       * maintain per-node smoothing state at DCs
#   - If node identity is not provided, routing defaults to Regional_DC_0 and smoothing is disabled.

import math

PRODUCT_ID = 1

# Optional hook: some simulators may set this before calling policies.
CURRENT_NODE_NAME = None

# Persistent state for smoothing (keyed by node name if available).
_STATE = {}


def _seasonal_term(period, amplitude=5.0, season_len=14):
    """Sine approximation of 14-day seasonal fluctuation (±amplitude)."""
    return amplitude * math.sin(2.0 * math.pi * (period % season_len) / float(season_len))


def _infer_node_name(inventory_dict):
    """Try to infer node identity from inventory_dict or global CURRENT_NODE_NAME."""
    if not isinstance(inventory_dict, dict):
        return None

    for k in ("node", "node_name", "_node", "__node__", "name", "_name"):
        v = inventory_dict.get(k)
        if isinstance(v, str) and v:
            return v

    meta = inventory_dict.get("meta")
    if isinstance(meta, dict):
        for k in ("node", "node_name", "name"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                return v

    global CURRENT_NODE_NAME
    if isinstance(CURRENT_NODE_NAME, str) and CURRENT_NODE_NAME:
        return CURRENT_NODE_NAME

    return None


def _product_inventory_position(inventory_dict, product_id=PRODUCT_ID):
    """Extract inventory_position for the given product id; fallback to 0.0."""
    if not isinstance(inventory_dict, dict):
        return 0.0
    if product_id in inventory_dict:
        return float(inventory_dict[product_id])
    if str(product_id) in inventory_dict:
        return float(inventory_dict[str(product_id)])
    return 0.0


def _retailer_upstream(node_name):
    """Map retailer node name to its allowed upstream DC."""
    # Constraint: Retailer_0/1/2 -> Regional_DC_0 ; Retailer_3/4/5 -> Regional_DC_1
    if not node_name:
        return "Regional_DC_0"  # safest default if identity unavailable

    if "Retailer_" in node_name:
        try:
            idx = int(node_name.split("Retailer_")[-1])
            return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            pass

    # Heuristic fallback
    if "DC_1" in node_name or node_name.endswith("_1"):
        return "Regional_DC_1"
    return "Regional_DC_0"


def retailer_policy_func(period, inventory_dict):
    """
    Retailer base-stock policy.

    Args:
        period (int): day index starting from 1
        inventory_dict (dict): {product_id: inventory_position} (+ optional metadata)

    Returns:
        dict: {"Regional_DC_0" or "Regional_DC_1": {1: order_qty}}
    """
    node_name = _infer_node_name(inventory_dict)
    upstream = _retailer_upstream(node_name)

    inv_pos = _product_inventory_position(inventory_dict, PRODUCT_ID)

    # Approximate demand:
    # mean ≈ 25/day + seasonal(±5), random component roughly uniform(±8)
    lead_time = 2.0
    mean_daily = 25.0 + _seasonal_term(period, amplitude=5.0, season_len=14)

    mu_lt = mean_daily * lead_time

    # Uniform(-8, 8) standard deviation = 8/sqrt(3)
    sigma_daily = 8.0 / math.sqrt(3.0)
    sigma_lt = sigma_daily * math.sqrt(lead_time)

    # Retailer: very high stockout penalty (120) vs holding (3/day)
    # Choose a relatively high z while avoiding excessive holding.
    z = 1.9
    safety = z * sigma_lt

    base_stock = mu_lt + safety

    order_qty = base_stock - inv_pos
    if order_qty < 0.0:
        order_qty = 0.0

    # Clamp to reduce extreme spikes (bullwhip / pathological inputs)
    if order_qty > 180.0:
        order_qty = 180.0

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period, inventory_dict):
    """
    Regional DC base-stock + smoothing policy.

    Args:
        period (int): day index starting from 1
        inventory_dict (dict): {product_id: inventory_position} (+ optional metadata)

    Returns:
        dict: {"Factory_0": {1: order_qty}}
    """
    node_name = _infer_node_name(inventory_dict)
    inv_pos = _product_inventory_position(inventory_dict, PRODUCT_ID)

    lead_time = 4.0
    n_retailers = 3.0

    mean_daily_total = n_retailers * (25.0 + _seasonal_term(period, amplitude=5.0, season_len=14))
    mu_lt = mean_daily_total * lead_time

    sigma_daily_retail = 8.0 / math.sqrt(3.0)
    sigma_daily_total = sigma_daily_retail * math.sqrt(n_retailers)
    sigma_lt = sigma_daily_total * math.sqrt(lead_time)

    # DC holding is cheap (0.8/day) and DC stockouts can cascade to retailers.
    # Keep a higher service buffer at DC.
    z = 2.3
    safety = z * sigma_lt

    # Extra cushion against seasonal phase mismatch and discreteness
    seasonal_cushion = 0.5 * n_retailers * 5.0 * lead_time

    base_stock = mu_lt + safety + seasonal_cushion

    raw_order = base_stock - inv_pos
    if raw_order < 0.0:
        raw_order = 0.0

    # Exponential smoothing of the order signal to reduce bullwhip.
    alpha = 0.6
    if node_name:
        prev = _STATE.get(node_name, {}).get("prev_order", 0.0)
        order_qty = alpha * raw_order + (1.0 - alpha) * prev
        _STATE.setdefault(node_name, {})["prev_order"] = order_qty
    else:
        order_qty = raw_order

    # Clamp
    if order_qty > 2500.0:
        order_qty = 2500.0

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
