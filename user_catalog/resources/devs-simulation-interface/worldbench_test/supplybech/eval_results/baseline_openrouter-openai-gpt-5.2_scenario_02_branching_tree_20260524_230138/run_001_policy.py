import math
from typing import Dict, Any

PRODUCT_ID = 1

# --- Demand model (given by problem statement) ---
BASE_MEAN = 25.0          # avg daily demand per retailer
SEASON_AMP = 5.0          # 14-day seasonality amplitude
SEASON_PERIOD = 14.0

# random fluctuation ~ ±8; treat as Uniform(-8, 8) => std = 8/sqrt(3)
RAND_STD = 8.0 / math.sqrt(3.0)

# Lead times
LT_RETAILER = 2  # Retailer -> DC
LT_DC = 4        # DC -> Factory

SIM_HORIZON = 100


def _seasonal_mean(period: int) -> float:
    """
    Expected mean demand for a single retailer at 'period' (1..100),
    using a 14-day sinusoidal seasonality around BASE_MEAN.
    """
    # period starts at 1; use (period-1) to align cycle start
    theta = 2.0 * math.pi * ((period - 1) % int(SEASON_PERIOD)) / SEASON_PERIOD
    return BASE_MEAN + SEASON_AMP * math.sin(theta)


def _remaining_days(period: int) -> int:
    # inclusive horizon: period in [1, SIM_HORIZON]
    return max(0, SIM_HORIZON - period + 1)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _get_inventory_position(inventory_dict: Dict) -> float:
    """
    Inventory snapshot is specified as {product_id: inventory_position}.
    Some simulators may include metadata fields; ignore non-product keys.
    """
    if PRODUCT_ID in inventory_dict:
        return _safe_float(inventory_dict[PRODUCT_ID], 0.0)
    # fallback: try numeric keys
    for k, v in inventory_dict.items():
        if isinstance(k, (int, float)) and int(k) == PRODUCT_ID:
            return _safe_float(v, 0.0)
    return 0.0


def _infer_node_name(inventory_dict: Dict) -> str:
    for key in ("node_name", "_node_name", "name", "node"):
        if key in inventory_dict and isinstance(inventory_dict[key], str):
            return inventory_dict[key]
    return ""


def _infer_upstream_for_retailer(inventory_dict: Dict) -> str:
    """
    Retailer_0/1/2 -> Regional_DC_0
    Retailer_3/4/5 -> Regional_DC_1

    If upstream explicitly provided, use it; else infer from node name or id.
    """
    # explicit upstream
    if "upstream" in inventory_dict and isinstance(inventory_dict["upstream"], str):
        return inventory_dict["upstream"]

    node_name = _infer_node_name(inventory_dict)
    if node_name.startswith("Retailer_"):
        try:
            rid = int(node_name.split("_")[1])
            return "Regional_DC_0" if rid <= 2 else "Regional_DC_1"
        except Exception:
            pass

    # alternative id fields
    for key in ("retailer_id", "id", "idx", "index"):
        if key in inventory_dict:
            try:
                rid = int(inventory_dict[key])
                return "Regional_DC_0" if rid <= 2 else "Regional_DC_1"
            except Exception:
                pass

    # default fallback (constraint-safe in many simulators that bind per-node anyway)
    return "Regional_DC_0"


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Order-up-to policy for retailers based on forecast over (LT+1) days and safety stock.
    Uses higher safety (high stockout penalty at retailer).
    """
    ip = _get_inventory_position(inventory_dict)
    upstream = _infer_upstream_for_retailer(inventory_dict)

    # dynamic coverage; reduce near end to avoid leftover inventory
    cover = min(LT_RETAILER + 1, _remaining_days(period))

    # Forecast mean daily demand for this period
    mu = _seasonal_mean(period)

    # Safety stock (robust): z * sigma * sqrt(cover)
    # Choose higher z due to high shortage cost (120) vs holding (3/day)
    z = 1.9
    sigma = RAND_STD
    safety = z * sigma * math.sqrt(max(1.0, float(cover)))

    # Target inventory position (order-up-to level)
    target_ip = mu * float(cover) + safety

    # Order quantity to raise IP to target
    order_qty = max(0.0, target_ip - ip)

    # Soft cap to avoid extreme bullwhip (still allows catch-up)
    # 6 days of mean demand is usually enough to recover without huge spikes.
    cap = 6.0 * mu
    order_qty = min(order_qty, cap)

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Order-up-to policy for Regional DCs, assuming each DC serves 3 retailers.
    Uses moderate safety (DC holding is cheaper, shortage cost lower than retailer).
    """
    ip = _get_inventory_position(inventory_dict)

    cover = min(LT_DC + 1, _remaining_days(period))

    # Aggregate demand from 3 retailers
    mu_r = _seasonal_mean(period)
    n_retailers = 3
    mu = n_retailers * mu_r

    # Aggregate demand std: sqrt(n)*sigma (assuming independence)
    sigma = math.sqrt(n_retailers) * RAND_STD

    # Moderate z to balance DC holding (0.8/day) vs DC shortage (40)
    z = 1.5
    safety = z * sigma * math.sqrt(max(1.0, float(cover)))

    target_ip = mu * float(cover) + safety

    order_qty = max(0.0, target_ip - ip)

    # Cap to reduce order spikes; still allows replenishment
    cap = 8.0 * mu_r * n_retailers  # about 8 days of aggregate mean
    order_qty = min(order_qty, cap)

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}