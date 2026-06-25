# test_policy_v7.py
# Tuned: S = d1 + alpha*d2 (alpha=0.45)

CYCLE_DEMAND = [30, 30, 30, 10, 10, 10]
PRODUCT_ID = 1
UPSTREAM = "Central_DC_0"
ALPHA = 0.45

def _demand_for_day_index(day_index_0based: int) -> float:
    return float(CYCLE_DEMAND[day_index_0based % len(CYCLE_DEMAND)])

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    tomorrow_index_0based = period
    d1 = _demand_for_day_index(tomorrow_index_0based)
    d2 = _demand_for_day_index(tomorrow_index_0based + 1)

    S = d1 + ALPHA * d2

    order_qty = S - ip
    if order_qty <= 1e-9:
        return {}
    return {UPSTREAM: {PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
