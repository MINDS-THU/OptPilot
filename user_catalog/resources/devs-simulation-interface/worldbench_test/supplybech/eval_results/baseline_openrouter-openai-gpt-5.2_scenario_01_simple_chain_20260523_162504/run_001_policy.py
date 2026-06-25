from typing import Dict

PRODUCT_ID = 1
UPSTREAM_NODE = "Central_DC_0"

DEMAND_CYCLE = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]
CYCLE_LEN = len(DEMAND_CYCLE)

# With daily review and lead_time=1 day from Central_DC to Retailer,
# protection period is (lead_time + review_period) = 2 days.
PROTECTION_DAYS = 2


def _forecast_demand_sum(period: int, days: int) -> float:
    start_idx = (period - 1) % CYCLE_LEN
    total = 0.0
    for k in range(days):
        total += DEMAND_CYCLE[(start_idx + k) % CYCLE_LEN]
    return total


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    target_ip = _forecast_demand_sum(period, PROTECTION_DAYS)
    order_qty = target_ip - ip
    if order_qty <= 1e-9:
        return {}

    return {UPSTREAM_NODE: {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}