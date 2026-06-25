# final_policy.py
# Replenishment policy for Retailer nodes in a 3-echelon supply chain scenario.

from typing import Dict

# Deterministic cyclic demand pattern (units/day) for each retailer, repeating every 6 days.
_DEMAND_CYCLE = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]
_CYCLE_LEN = 6

# Tunable parameters for a robust order-up-to policy using inventory position (IP).
# Lead time Retailer <- Central_DC_0 is 1 day, so "tomorrow" demand is the key driver.
_ALPHA_DAY_AFTER = 0.5  # partial coverage for day-after-tomorrow demand to reduce timing risk
_SAFETY_UNITS = 5.0     # small fixed buffer to avoid costly stockouts with minimal holding impact
_EPS = 1e-9


def _cycle_demand_for_period(period: int) -> float:
    """Demand in the given period (period starts at 1)."""
    return _DEMAND_CYCLE[(period - 1) % _CYCLE_LEN]


def retailer_policy_func(period: int, inventory_dict: Dict[int, float]) -> dict:
    """
    Retailer replenishment policy (inventory-position order-up-to).

    Args:
        period: current day number (1..100)
        inventory_dict: {product_id: inventory_position}

    Returns:
        Order dict: {"Central_DC_0": {1: qty}} or {} if no order.
    """
    # No need to order after the final simulation period.
    if period >= 100:
        return {}

    product_id = 1

    # Inventory position (on-hand + in-transit - backorders)
    ip = float(inventory_dict.get(product_id, 0.0))

    # With 1-day lead time, an order placed now is intended to cover demand in period+1.
    demand_tomorrow = _cycle_demand_for_period(period + 1)
    demand_day_after = _cycle_demand_for_period(period + 2)

    # Time-phased target inventory position (order-up-to level).
    target_ip = demand_tomorrow + _ALPHA_DAY_AFTER * demand_day_after + _SAFETY_UNITS

    order_qty = target_ip - ip
    if order_qty <= _EPS:
        return {}

    # Enforce nonnegativity.
    if order_qty < 0.0:
        order_qty = 0.0

    return {"Central_DC_0": {product_id: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
