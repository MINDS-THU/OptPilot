import math

# Product IDs
RAW_A = 1
RAW_B = 2
RAW_C = 3
PROD_X = 4
PROD_Y = 5

# Retailers count (fixed in scenario)
N_RETAILERS = 3


def _clip_nonneg(x: float) -> float:
    return x if x > 0.0 else 0.0


def _demand_mean(product_id: int, day: int) -> float:
    """
    Deterministic mean/forecast demand for a single retailer on a given day.

    Uses: base + seasonal_amp*sin(2*pi*day/cycle).
    (Noise has mean 0, so it's excluded from the mean forecast.)
    """
    if product_id == PROD_X:
        base, amp, cycle = 15.0, 6.0, 21.0
    elif product_id == PROD_Y:
        base, amp, cycle = 10.0, 4.0, 14.0
    else:
        return 0.0
    return max(0.0, base + amp * math.sin(2.0 * math.pi * (day / cycle)))


def _noise_sigma(product_id: int) -> float:
    """For uniform(-a, a): sigma = a/sqrt(3)."""
    if product_id == PROD_X:
        a = 5.0
    elif product_id == PROD_Y:
        a = 4.0
    else:
        a = 0.0
    return a / math.sqrt(3.0) if a > 0.0 else 0.0


def _sum_forecast(product_id: int, start_day: int, horizon: int, n_retailers: int = 1) -> float:
    return sum(_demand_mean(product_id, start_day + i) for i in range(horizon)) * float(n_retailers)


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer -> DC (LT=1 day).
    Order-up-to with a 2-day protection period. Slightly more conservative for X.
    """
    L = 1
    protection = L + 1  # daily review
    ip_x = float(inventory_dict.get(PROD_X, 0.0))
    ip_y = float(inventory_dict.get(PROD_Y, 0.0))

    mu_x = _sum_forecast(PROD_X, period, protection, n_retailers=1)
    mu_y = _sum_forecast(PROD_Y, period, protection, n_retailers=1)

    # High retailer stockout penalty -> higher safety. X prioritized.
    ss_x = 2.4 * _noise_sigma(PROD_X) * math.sqrt(protection) + 4.0
    ss_y = 2.1 * _noise_sigma(PROD_Y) * math.sqrt(protection) + 2.5

    S_x = mu_x + ss_x
    S_y = mu_y + ss_y

    q_x = _clip_nonneg(S_x - ip_x)
    q_y = _clip_nonneg(S_y - ip_y)

    if q_x <= 0.0 and q_y <= 0.0:
        return {}
    return {"DC_0": {PROD_X: q_x, PROD_Y: q_y}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC -> Assembler (LT=2 days).
    Order-up-to using aggregated demand of 3 retailers, 3-day protection period.
    Mild smoothing to reduce bullwhip without starving downstream.
    """
    L = 2
    protection = L + 1  # daily review
    alpha = 0.95

    ip_x = float(inventory_dict.get(PROD_X, 0.0))
    ip_y = float(inventory_dict.get(PROD_Y, 0.0))

    mu_x = _sum_forecast(PROD_X, period, protection, n_retailers=N_RETAILERS)
    mu_y = _sum_forecast(PROD_Y, period, protection, n_retailers=N_RETAILERS)

    sigma_x = math.sqrt(N_RETAILERS) * _noise_sigma(PROD_X)
    sigma_y = math.sqrt(N_RETAILERS) * _noise_sigma(PROD_Y)

    # X gets more buffer (higher demand + higher downstream risk)
    ss_x = 1.9 * sigma_x * math.sqrt(protection) + 20.0
    ss_y = 1.8 * sigma_y * math.sqrt(protection) + 12.0

    S_x = mu_x + ss_x
    S_y = mu_y + ss_y

    desired_x = _clip_nonneg(S_x - ip_x)
    desired_y = _clip_nonneg(S_y - ip_y)

    q_x = alpha * desired_x
    q_y = alpha * desired_y

    if q_x <= 0.0 and q_y <= 0.0:
        return {}
    return {"Assembler_0": {PROD_X: q_x, PROD_Y: q_y}}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler orders raw materials from unlimited suppliers (LT=2 days).

    Heuristic:
    - Forecast aggregated end-demand (3 retailers) over horizon = raw_LT + DC_LT + 1 = 5 days.
    - Net against assembler finished-goods inventory position (to avoid double-buffering).
    - Convert net requirements to raw-material targets via BOM.
    - Keep extra buffer for shared Raw_C to reduce contention.
    """
    raw_L = 2
    dc_L = 2
    protection = raw_L + dc_L + 1  # 5 days

    ip_a = float(inventory_dict.get(RAW_A, 0.0))
    ip_b = float(inventory_dict.get(RAW_B, 0.0))
    ip_c = float(inventory_dict.get(RAW_C, 0.0))

    ip_fx = float(inventory_dict.get(PROD_X, 0.0))
    ip_fy = float(inventory_dict.get(PROD_Y, 0.0))

    dem_x = _sum_forecast(PROD_X, period, protection, n_retailers=N_RETAILERS)
    dem_y = _sum_forecast(PROD_Y, period, protection, n_retailers=N_RETAILERS)

    need_x = _clip_nonneg(dem_x - ip_fx)
    need_y = _clip_nonneg(dem_y - ip_fy)

    sigma_x = math.sqrt(N_RETAILERS) * _noise_sigma(PROD_X)
    sigma_y = math.sqrt(N_RETAILERS) * _noise_sigma(PROD_Y)

    ss_x = 1.7 * sigma_x * math.sqrt(protection) + 12.0
    ss_y = 1.6 * sigma_y * math.sqrt(protection) + 10.0

    # BOM conversion to raw targets
    target_a = need_x + ss_x
    target_b = 2.0 * need_y + 2.0 * ss_y
    target_c = (need_x + need_y) + (ss_x + ss_y) + 35.0  # extra shared-material cushion

    q_a = _clip_nonneg(target_a - ip_a)
    q_b = _clip_nonneg(target_b - ip_b)
    q_c = _clip_nonneg(target_c - ip_c)

    orders = {}
    if q_a > 0.0:
        orders["Supplier_A_0"] = {RAW_A: q_a}
    if q_b > 0.0:
        orders["Supplier_B_0"] = {RAW_B: q_b}
    if q_c > 0.0:
        orders["Supplier_C_0"] = {RAW_C: q_c}
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
