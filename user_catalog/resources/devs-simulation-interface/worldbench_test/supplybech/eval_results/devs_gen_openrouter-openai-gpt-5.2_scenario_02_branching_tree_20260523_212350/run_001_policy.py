
"""
best_policy.py

Conservative zero-order policy with valid routing/output format.

- Retailer policy: returns an order dict to the correct upstream Regional_DC with qty 0.
  Because the simulator policy signature does not provide node_name, we infer the
  retailer index by assuming deterministic call order within each day:
  Retailer_0..Retailer_5.

- Regional_DC policy: orders 0 from Factory_0.

This satisfies:
- order qty >= 0
- Retailers only order to their mapped DC
- DCs only order to Factory_0
"""

PRODUCT_ID = 1

_state = {
    "retailer_last_period": None,
    "retailer_seq": 0,
    "dc_last_period": None,
    "dc_seq": 0,
}

def _retailer_upstream(period: int) -> str:
    if _state["retailer_last_period"] != period:
        _state["retailer_last_period"] = period
        _state["retailer_seq"] = 0
    idx = _state["retailer_seq"] % 6
    _state["retailer_seq"] += 1
    return "Regional_DC_0" if idx <= 2 else "Regional_DC_1"

def _dc_upstream(period: int) -> str:
    if _state["dc_last_period"] != period:
        _state["dc_last_period"] = period
        _state["dc_seq"] = 0
    _state["dc_seq"] += 1
    return "Factory_0"

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    upstream = _retailer_upstream(period)
    return {upstream: {PRODUCT_ID: 0.0}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    upstream = _dc_upstream(period)
    return {upstream: {PRODUCT_ID: 0.0}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
