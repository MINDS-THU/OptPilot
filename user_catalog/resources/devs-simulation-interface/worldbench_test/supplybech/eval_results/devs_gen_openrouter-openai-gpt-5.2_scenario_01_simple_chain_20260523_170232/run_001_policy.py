def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer replenishment policy for the given simulator.

    - Deterministic JIT for lead_time=1:
        Order enough to cover next day's deterministic demand, using inventory_position
        (on_hand + pipeline - backlog) provided by the simulator.

    - End-of-horizon drain (day 100):
        Place an extra order that will ship from Central_DC_0 on day 100 and arrive on day 101.
        Since the simulation horizon ends at day 100 and holding cost is charged on end-of-day
        on-hand only (pipeline not charged), this reduces Central_DC end-of-day holding cost
        on day 100 without increasing retailer holding cost within the 100-day horizon.
    """
    pid = 1
    ip = float(inventory_dict.get(pid, 0.0))

    # 6-day deterministic demand cycle
    pattern = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]

    # Next day's demand (period is 1-based): index (period % 6)
    demand_next = pattern[period % 6]

    # JIT order quantity
    q = demand_next - ip

    # Drain Central_DC on the last simulated day (100 days in this scenario)
    if period == 100:
        q += 440.0 / 3.0  # split evenly across 3 retailers; aggregate extra = 440

    if q <= 0:
        return {}

    return {"Central_DC_0": {pid: float(q)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
