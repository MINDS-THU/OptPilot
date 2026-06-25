# final_policy.py
# Replenishment policies for a 3-tier supply chain (Factory -> Regional_DC -> Retailer)
# Single product: Electronics (product_id=1)

from __future__ import annotations
import math
from typing import Dict, Any

PRODUCT_ID = 1

# Optional context hooks:
# Some simulators set a module-level variable before calling the policy.
CURRENT_NODE: str | None = None

def _seasonal_multiplier(period: int, cycle: int = 14) -> float:
    """
    Returns sin-based seasonal term with period `cycle`.
    Phase is set to 0; this matches a common "starts at 0" seasonality convention.
    """
    # period starts at 1; keep it as-is to remain deterministic.
    return math.sin(2.0 * math.pi * (period / cycle))

def _infer_upstream_for_retailer(inventory_dict: Dict[Any, Any]) -> str:
    """
    Best-effort inference of the retailer's upstream DC.
    Priority:
      1) explicit upstream fields in inventory_dict
      2) module-level CURRENT_NODE convention Retailer_i -> Regional_DC_(i//3)
      3) fallback to Regional_DC_0 (safe default)
    """
    # 1) Explicit upstream hint
    for key in ("upstream", "_upstream", "upstream_node", "parent", "source"):
        val = inventory_dict.get(key)
        if isinstance(val, str) and "Regional_DC" in val:
            return val

    # Sometimes upstream might be passed as a string key with None/True value
    for k in inventory_dict.keys():
        if isinstance(k, str) and k.startswith("Regional_DC_"):
            return k

    # 2) CURRENT_NODE
    global CURRENT_NODE
    if isinstance(CURRENT_NODE, str) and CURRENT_NODE.startswith("Retailer_"):
        try:
            idx = int(CURRENT_NODE.split("_")[-1])
            return "Regional_DC_0" if idx < 3 else "Regional_DC_1"
        except Exception:
            pass

    # 3) Fallback
    return "Regional_DC_0"

def _infer_factory_for_dc(inventory_dict: Dict[Any, Any]) -> str:
    # Factory node name is fixed by spec; keep hook for robustness.
    for key in ("upstream", "_upstream", "upstream_node", "parent", "source"):
        val = inventory_dict.get(key)
        if isinstance(val, str) and "Factory" in val:
            return val
    return "Factory_0"

def _get_inventory_position(inventory_dict: Dict[Any, Any], product_id: int = PRODUCT_ID) -> float:
    val = inventory_dict.get(product_id, 0.0)
    try:
        return float(val)
    except Exception:
        return 0.0

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer: daily review, order-up-to (base-stock) on inventory position.

    Demand model (approx):
      d_t ≈ 25 + 5*sin(2π t/14)  (plus random noise)
    We cover lead time L=2 plus review period 1 => (L+1)=3 days of demand.
    High stockout penalty => choose relatively high service level safety stock.
    """
    ip = _get_inventory_position(inventory_dict, PRODUCT_ID)

    # Forecast mean daily demand (seasonal)
    mu = 25.0
    amp = 5.0
    daily = mu + amp * _seasonal_multiplier(period)

    # Parameters
    lead_time = 2
    cover_days = lead_time + 1  # base-stock convention
    # Approx daily std dev (noise + minor model misspecification)
    sigma_daily = 6.0
    # Safety factor (around ~98% one-sided)
    z = 2.05

    target = daily * cover_days + z * sigma_daily * math.sqrt(cover_days)

    # Keep target within reasonable bounds (avoid extreme swings)
    target = max(45.0, min(140.0, target))

    order_qty = max(0.0, target - ip)

    # Optional: cap daily order to avoid destabilizing bursts
    order_qty = min(order_qty, 200.0)

    upstream = _infer_upstream_for_retailer(inventory_dict)
    return {upstream: {PRODUCT_ID: float(order_qty)}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Regional_DC: daily review, order-up-to on inventory position.

    Approximates aggregate demand of 3 retailers:
      d_t ≈ 3*(25 + 5*sin(2π t/14)) = 75 + 15*sin(...)
    Cover factory lead time L=4 plus review period 1 => 5 days.
    Lower holding cost and lower stockout penalty vs Retailer => moderate safety.
    """
    ip = _get_inventory_position(inventory_dict, PRODUCT_ID)

    mu = 75.0
    amp = 15.0
    daily = mu + amp * _seasonal_multiplier(period)

    lead_time = 4
    cover_days = lead_time + 1

    # Approx daily std dev for sum of 3 retailers (assume weak correlation)
    sigma_daily = 11.0
    z = 1.65  # ~95% one-sided

    target = daily * cover_days + z * sigma_daily * math.sqrt(cover_days)

    # Reasonable bounds to prevent pathological ordering
    target = max(200.0, min(900.0, target))

    order_qty = max(0.0, target - ip)
    order_qty = min(order_qty, 2000.0)

    factory = _infer_factory_for_dc(inventory_dict)
    return {factory: {PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
