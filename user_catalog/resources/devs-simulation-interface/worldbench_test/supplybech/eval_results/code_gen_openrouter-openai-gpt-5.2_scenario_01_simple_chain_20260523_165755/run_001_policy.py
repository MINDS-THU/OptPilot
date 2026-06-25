# -*- coding: utf-8 -*-
"""
Retailer replenishment policy (Retailer_0/1/2) for the given 3-tier supply chain.

- Single product: product_id = 1
- Deterministic cyclic daily demand (6-day cycle):
    [30, 30, 30, 10, 10, 10]
- Retailer -> Central_DC lead time: 1 day
- inventory_dict provides inventory position:
    inventory_position = on_hand + on_order - backorders

Policy:
- Daily order-up-to (base-stock), time-varying target.
- Target covers demand over the next (L + 1) = 2 days, plus a small safety buffer.
"""

PRODUCT_ID = 1
UPSTREAM_NODE = "Central_DC_0"

DEMAND_PATTERN = (30.0, 30.0, 30.0, 10.0, 10.0, 10.0)
CYCLE = len(DEMAND_PATTERN)

LOOKAHEAD_DAYS = 2
SAFETY_BUFFER = 5.0


def _demand(day_1_based: int) -> float:
    return DEMAND_PATTERN[(day_1_based - 1) % CYCLE]


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    target = float(SAFETY_BUFFER)
    for k in range(1, LOOKAHEAD_DAYS + 1):
        target += _demand(period + k)

    order_qty = target - inv_pos
    if order_qty <= 1e-9:
        return {}

    if order_qty < 0.0:
        order_qty = 0.0

    return {UPSTREAM_NODE: {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
