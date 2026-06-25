import math

# Final multi-product replenishment policy for Retailer nodes.
# Uses seasonal mean demand forecast + safety stock, implemented as an order-up-to (base-stock) policy
# on inventory position (on_hand + on_order - backorder), which is provided by the simulator.

SUPPLIER_NAME = "DC_0"
HORIZON_DAYS = 100

# product_id: (base, seasonal_amp, seasonal_period, noise_range)
PRODUCT_PARAMS = {
    1: (18.0, 8.0, 21.0, 6.0),  # Product_A
    2: (12.0, 5.0, 14.0, 4.0),  # Product_B
}

# Policy tunables (chosen for high stockout penalty at retailers)
COVER_DAYS_BY_PID = {
    1: 6,  # slightly longer cover for higher-mean product
    2: 5,
}

Z_BY_PID = {
    1: 2.5,
    2: 2.3,
}


def _mean_demand(pid: int, day: int) -> float:
    """Deterministic seasonal mean (phase=0 as per scenario description)."""
    base, amp, period, _noise = PRODUCT_PARAMS[pid]
    mu = base + amp * math.sin(2.0 * math.pi * (float(day) / period))
    return mu if mu > 0.0 else 0.0


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    remaining_days = max(0, HORIZON_DAYS - int(period))
    order_payload: dict[int, float] = {}

    for pid_raw, ip_raw in inventory_dict.items():
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        if pid not in PRODUCT_PARAMS:
            continue

        try:
            ip = float(ip_raw)
        except Exception:
            ip = 0.0

        cover = int(COVER_DAYS_BY_PID.get(pid, 5))
        # taper at horizon end
        eff_cover = max(0, min(cover, remaining_days))
        if eff_cover <= 0:
            target = 0.0
            expected = 0.0
        else:
            expected = 0.0
            for k in range(1, eff_cover + 1):
                expected += _mean_demand(pid, int(period) + k)

            # Safety stock: uniform noise std = r/sqrt(3); scale with sqrt(eff_cover)
            _base, _amp, _per, noise_range = PRODUCT_PARAMS[pid]
            demand_std = float(noise_range) / math.sqrt(3.0)
            z = float(Z_BY_PID.get(pid, 2.3))
            safety = z * demand_std * math.sqrt(float(eff_cover))

            target = expected + safety

        qty = target - ip
        if qty > 0.0:
            # Soft cap to avoid pathological huge orders if IP is temporarily distorted
            max_qty = 3.0 * (expected if expected > 1e-9 else 1.0)
            if qty > max_qty:
                qty = max_qty
            order_payload[pid] = qty

    return {} if not order_payload else {SUPPLIER_NAME: order_payload}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
