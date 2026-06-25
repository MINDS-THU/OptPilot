# final_policy.py
# Retailer replenishment policy for a 3-tier supply chain, single product (product_id=1).

_DEMAND_CYCLE = [30, 30, 30, 10, 10, 10]  # deterministic 6-day cycle
_PRODUCT_ID = 1
_UPSTREAM = "Central_DC_0"

def _demand_for_day(day: int) -> float:
    """Return deterministic demand for a given 1-indexed day."""
    return float(_DEMAND_CYCLE[(day - 1) % len(_DEMAND_CYCLE)])

def _sum_future_demand(period: int, horizon_days: int) -> float:
    """
    Sum of demand for days (period+1) ... (period+horizon_days).
    period is 1-indexed.
    """
    total = 0.0
    for k in range(1, horizon_days + 1):
        total += _demand_for_day(period + k)
    return total

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Daily-review, deterministic base-stock policy using inventory position.

    Inventory position = on_hand + on_order - backorder (provided by simulator).

    We order up to a target position equal to the sum of deterministic demand over a
    short future horizon. With retailer lead time = 1 day, we cover:
      - 1 day (lead time) + 2 days buffer = 3 days future demand.

    This provides robustness to temporary upstream/DC shortfalls while controlling
    holding cost at the retailer.
    """
    inv_pos = float(inventory_dict.get(_PRODUCT_ID, 0.0))

    horizon_days = 3
    target_pos = _sum_future_demand(period, horizon_days)

    order_qty = target_pos - inv_pos
    if order_qty <= 0.0:
        return {}

    return {_UPSTREAM: {_PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
