def _demand_for_day(period: int) -> float:
    """
    Deterministic 6-day repeating demand pattern per retailer (units/day).
    period is 1-indexed.
    """
    cycle = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]
    return cycle[(period - 1) % len(cycle)]


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer replenishment policy (lead_time Retailer<-Central_DC = 1 day).

    inventory_dict provides inventory_position = on_hand + in_transit - backlog.
    With daily review and deterministic demand, set an order-up-to target equal to
    *tomorrow's* demand so inventory is replenished just in time.

    Returns:
        {} or {"Central_DC_0": {1: qty}}
    """
    product_id = 1
    ip = float(inventory_dict.get(product_id, 0.0))

    target_ip = _demand_for_day(period + 1)  # cover next day's demand
    qty = target_ip - ip
    if qty <= 0.0:
        return {}
    return {"Central_DC_0": {product_id: float(qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
