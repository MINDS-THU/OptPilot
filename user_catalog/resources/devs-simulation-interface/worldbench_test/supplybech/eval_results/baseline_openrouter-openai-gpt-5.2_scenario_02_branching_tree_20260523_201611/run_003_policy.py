import math
import os
import inspect
from typing import Dict, Any

PRODUCT_ID = 1

# ----------------------------
# Helpers: infer current node name (best-effort)
# ----------------------------
def _infer_node_name(inventory_dict: Dict[Any, Any]) -> str:
    """
    Try multiple ways to infer which node is currently being controlled.
    Falls back to "" if not found.
    """
    # 1) Sometimes simulators inject node name into the inventory_dict under a meta key
    for k in ("node_name", "_node_name", "name", "_name", "entity", "entity_name", "_entity_name"):
        if k in inventory_dict and isinstance(inventory_dict[k], str):
            return inventory_dict[k]

    # 2) Environment variables
    for env_k in ("SC_NODE_NAME", "NODE_NAME", "CURRENT_NODE", "ENTITY_NAME"):
        v = os.environ.get(env_k)
        if v:
            return v

    # 3) Inspect call stack for common local variable names
    try:
        frame = inspect.currentframe()
        # walk a few frames up
        for _ in range(8):
            if frame is None:
                break
            frame = frame.f_back
            if frame is None:
                break
            for k in ("node_name", "current_node", "node", "entity_name", "name"):
                v = frame.f_locals.get(k)
                if isinstance(v, str) and v:
                    return v
    except Exception:
        pass

    return ""


def _retailer_upstream_dc(retailer_name: str) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1
    If unknown, default to Regional_DC_0 (best-effort).
    """
    if "Retailer_" in retailer_name:
        try:
            idx = int(retailer_name.split("Retailer_")[1].split()[0].split("_")[0])
            return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"
        except Exception:
            pass
    # fallback heuristics
    if retailer_name.endswith(("0", "1", "2")):
        return "Regional_DC_0"
    if retailer_name.endswith(("3", "4", "5")):
        return "Regional_DC_1"
    return "Regional_DC_0"


# ----------------------------
# Simple seasonal forecast model (given problem statement)
# ----------------------------
def _seasonal_mean(base: float, amp: float, period: int, season_len: int = 14) -> float:
    # Use a smooth sinusoid; phase isn't specified, so we start from period=1.
    # Clamp to non-negative demand.
    x = 2.0 * math.pi * ((period - 1) % season_len) / float(season_len)
    return max(0.0, base + amp * math.sin(x))


# ----------------------------
# Optional order smoothing state (per inferred node name)
# ----------------------------
_LAST_ORDER_BY_NODE: Dict[str, float] = {}


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    (s, S)-like order-up-to policy with seasonal mean forecast and safety stock.
    Orders to the appropriate Regional_DC based on retailer index.
    """
    node_name = _infer_node_name(inventory_dict)
    upstream = _retailer_upstream_dc(node_name)

    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    # Lead time Retailer <- DC is 2 days; daily review => cover (L + 1) days
    L = 2
    cover_days = L + 1

    # Demand model approximation from prompt
    mu = _seasonal_mean(base=25.0, amp=5.0, period=period, season_len=14)

    # Variability approximation: random +/-8 plus other noise => use ~6 stdev
    sigma = 6.0

    # Service level tuning: high retailer stockout penalty => higher z
    z = 1.35  # ~91%+ cycle service proxy
    safety = z * sigma * math.sqrt(cover_days)

    S = mu * cover_days + safety

    # Practical caps to avoid runaway inventory due to inference errors
    S = min(max(S, 0.0), 160.0)

    desired = max(0.0, S - inv_pos)

    # Mild smoothing to reduce bullwhip (only if we can identify node)
    alpha = 0.65  # closer to 1 => more responsive
    if node_name:
        last = _LAST_ORDER_BY_NODE.get(node_name, 0.0)
        order_qty = max(0.0, alpha * desired + (1.0 - alpha) * last)
        _LAST_ORDER_BY_NODE[node_name] = order_qty
    else:
        order_qty = desired

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Order-up-to policy for Regional DC ordering from Factory_0.
    Uses aggregate seasonal mean (3 retailers) and higher lead time (4 days).
    Includes smoothing to reduce bullwhip.
    """
    node_name = _infer_node_name(inventory_dict)
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    upstream = "Factory_0"

    # Lead time DC <- Factory is 4 days; daily review => cover (L + 1) days
    L = 4
    cover_days = L + 1

    # Aggregate of 3 retailers per DC
    mu = _seasonal_mean(base=75.0, amp=15.0, period=period, season_len=14)

    # Aggregate uncertainty ~ sqrt(3) times retailer sigma
    sigma = math.sqrt(3.0) * 6.0  # ~10.39

    # DC has lower holding cost and moderate stockout cost; keep decent buffer
    z = 1.15
    safety = z * sigma * math.sqrt(cover_days)

    S = mu * cover_days + safety

    # Practical caps (DC starts at 800; but we don't want excessive replenishment)
    S = min(max(S, 0.0), 900.0)

    desired = max(0.0, S - inv_pos)

    # Stronger smoothing at DC to reduce bullwhip upstream
    alpha = 0.45
    key = node_name if node_name else "Regional_DC"
    last = _LAST_ORDER_BY_NODE.get(key, 0.0)
    order_qty = max(0.0, alpha * desired + (1.0 - alpha) * last)
    _LAST_ORDER_BY_NODE[key] = order_qty

    return {upstream: {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}