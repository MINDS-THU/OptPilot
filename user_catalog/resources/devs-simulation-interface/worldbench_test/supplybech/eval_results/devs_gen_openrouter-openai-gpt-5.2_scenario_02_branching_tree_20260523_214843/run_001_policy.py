
"""
Replenishment Policy v11: retailer protection_days=2.65, safety=22.
"""
import math

PRODUCT_ID = 1
_ret_last_period = None
_ret_call_idx = 0


def _seasonal_mean_demand(day: int) -> float:
    return 25.0 + 5.0 * math.sin(2.0 * math.pi * (day + 1) / 14.0)


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    global _ret_last_period, _ret_call_idx
    if _ret_last_period != period:
        _ret_last_period = period
        _ret_call_idx = 0

    upstream = "Regional_DC_0" if _ret_call_idx < 3 else "Regional_DC_1"
    _ret_call_idx += 1

    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))

    protection_days = 2.65
    safety = 22.0
    target_S = protection_days * _seasonal_mean_demand(period) + safety

    order_qty = max(0.0, target_S - inv_pos)
    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    inv_pos = float(inventory_dict.get(PRODUCT_ID, 0.0))
    mean_total_per_day = 3.0 * _seasonal_mean_demand(period)
    protection_days = 5.7
    safety = 110.0
    target_S = protection_days * mean_total_per_day + safety
    order_qty = max(0.0, target_S - inv_pos)
    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
