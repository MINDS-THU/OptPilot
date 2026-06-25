# -*- coding: utf-8 -*-
"""
Retailer replenishment policy for the 3-tier supply chain.

Policy idea (v1):
- Deterministic cyclic demand with 1-day lead time from Central_DC -> Retailer.
- Use a daily order-up-to (base-stock) target based on tomorrow's demand.
- inventory_dict provides inventory_position = on_hand + on_order - backorder.

Return format must be:
{"Central_DC_0": {1: qty}}  or {}.
"""

from typing import Dict

PRODUCT_ID = 1
UPSTREAM_NAME = "Central_DC_0"

# Deterministic 6-day demand cycle given in the task
DEMAND_CYCLE = [30, 30, 30, 10, 10, 10]

# Small safety buffer to hedge minor event-ordering nuances
SAFETY_STOCK = 0.0


def _demand_for_day(day_1_indexed: int) -> float:
    """Return deterministic demand for a given 1-indexed day."""
    return float(DEMAND_CYCLE[(day_1_indexed - 1) % len(DEMAND_CYCLE)])


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: current day (1..100)
        inventory_dict: {product_id: inventory_position}

    Returns:
        Order instructions to upstream Central_DC_0, or {}.
    """
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    # With 1-day lead time and end-of-day ordering, a natural target is tomorrow's demand.
    tomorrow_demand = _demand_for_day(period + 1)
    target_position = tomorrow_demand + float(SAFETY_STOCK)

    order_qty = target_position - ip
    if order_qty <= 0:
        return {}

    return {UPSTREAM_NAME: {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
