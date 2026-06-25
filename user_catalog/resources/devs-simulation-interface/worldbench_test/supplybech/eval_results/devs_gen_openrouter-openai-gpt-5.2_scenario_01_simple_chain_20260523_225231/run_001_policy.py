# v5: Retailer horizon=1 day; Central DC horizon=2 days (aggressive).

_DEMAND_CYCLE = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]
_CYCLE_LEN = 6

def _demand_per_retailer(period: int) -> float:
    return _DEMAND_CYCLE[(period - 1) % _CYCLE_LEN]

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    product_id = 1
    ip = float(inventory_dict.get(product_id, 0.0))
    target_ip = _demand_per_retailer(period + 1)
    q = target_ip - ip
    if q <= 1e-9:
        return {}
    return {"Central_DC_0": {product_id: float(q)}}

def central_dc_policy_func(period: int, inventory_dict: dict) -> dict:
    product_id = 1
    ip = float(inventory_dict.get(product_id, 0.0))

    # Aggressive: 2-day horizon (less than the 3-day lead time)
    total_target = 0.0
    for k in range(2):
        total_target += 3.0 * _demand_per_retailer(period + k)

    q = total_target - ip
    if q <= 1e-9:
        return {}
    return {"Factory_0": {product_id: float(q)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "CentralDC": central_dc_policy_func,
    "Central_DC": central_dc_policy_func,
}
