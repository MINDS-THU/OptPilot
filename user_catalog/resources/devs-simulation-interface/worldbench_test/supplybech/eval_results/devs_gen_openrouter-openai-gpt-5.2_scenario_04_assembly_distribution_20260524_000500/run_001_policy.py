
import math

# ---------------- Product IDs ----------------
RAW_A = 1
RAW_B = 2
RAW_C = 3
PROD_X = 4
PROD_Y = 5

# ---------------- Demand model (expected value) ----------------
def _expected_daily_demand(product_id: int, t: int) -> float:
    """
    Expected demand (noise mean = 0). t starts at 1.
    Uses sin(2*pi*t/cycle) as the seasonal component.
    """
    if product_id == PROD_X:
        base, amp, cycle = 15.0, 6.0, 21.0
        return max(0.0, base + amp * math.sin(2.0 * math.pi * (t / cycle)))
    if product_id == PROD_Y:
        base, amp, cycle = 10.0, 4.0, 14.0
        return max(0.0, base + amp * math.sin(2.0 * math.pi * (t / cycle)))
    return 0.0

def _noise_sd(product_id: int) -> float:
    """
    Noise is uniform(-a, +a), sd = a/sqrt(3).
    """
    if product_id == PROD_X:
        a = 5.0
        return a / math.sqrt(3.0)
    if product_id == PROD_Y:
        a = 4.0
        return a / math.sqrt(3.0)
    return 0.0

def _sum_expected(product_id: int, start_t: int, days: int, multiplier: float = 1.0) -> float:
    # sum for future days start_t+1 ... start_t+days
    s = 0.0
    for k in range(1, days + 1):
        s += multiplier * _expected_daily_demand(product_id, start_t + k)
    return s

def _safety_stock(product_id: int, days: int, z: float, multiplier_sd: float = 1.0) -> float:
    """
    Safety stock based on i.i.d. noise only: z * sd * sqrt(days).
    Multiplier accounts for aggregation across retailers (sd scales with sqrt(n)).
    """
    sd = _noise_sd(product_id) * multiplier_sd
    return max(0.0, z * sd * math.sqrt(max(0.0, float(days))))

def _order_up_to(ip: float, target: float) -> float:
    q = float(target) - float(ip)
    return q if q > 0.0 else 0.0

# ---------------- Retailer policy ----------------
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer -> DC (LT=1 day).
    Keep retailer buffers modest due to high holding cost (2.0), but still protect against noise.
    """
    ip_x = float(inventory_dict.get(PROD_X, 0.0))
    ip_y = float(inventory_dict.get(PROD_Y, 0.0))

    LT = 1
    review_buffer = 1
    P = LT + review_buffer  # protection window in days

    # z tuned moderate: push most buffering upstream to DC (cheaper holding)
    z_x = 1.0
    z_y = 0.9

    target_x = _sum_expected(PROD_X, period, P, 1.0) + _safety_stock(PROD_X, P, z_x)
    target_y = _sum_expected(PROD_Y, period, P, 1.0) + _safety_stock(PROD_Y, P, z_y)

    qx = _order_up_to(ip_x, target_x)
    qy = _order_up_to(ip_y, target_y)

    if qx <= 0.0 and qy <= 0.0:
        return {}
    return {"DC_0": {PROD_X: qx, PROD_Y: qy}}

# ---------------- DC policy ----------------
def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC -> Assembler (LT=2 days). DC serves 3 retailers.
    DC is the main risk-pooling buffer (holding cost 0.5, much lower than retailers),
    to reduce very expensive retailer stockouts (80 per unit).
    """
    ip_x = float(inventory_dict.get(PROD_X, 0.0))
    ip_y = float(inventory_dict.get(PROD_Y, 0.0))

    LT = 2
    review_buffer = 2
    P = LT + review_buffer

    n_retailers = 3.0
    # Aggregated mean scales with n; sd scales with sqrt(n)
    sd_mult = math.sqrt(n_retailers)

    # Higher z at DC to protect service level
    z_x = 1.7
    z_y = 1.5

    target_x = _sum_expected(PROD_X, period, P, n_retailers) + _safety_stock(PROD_X, P, z_x, multiplier_sd=sd_mult)
    target_y = _sum_expected(PROD_Y, period, P, n_retailers) + _safety_stock(PROD_Y, P, z_y, multiplier_sd=sd_mult)

    qx = _order_up_to(ip_x, target_x)
    qy = _order_up_to(ip_y, target_y)

    if qx <= 0.0 and qy <= 0.0:
        return {}
    return {"Assembler_0": {PROD_X: qx, PROD_Y: qy}}

# ---------------- Assembler (raw procurement) policy ----------------
def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler orders raw materials from suppliers (LT=2 days).
    Use aggregated finished-demand forecast and map via BOM:
      X: 1*A + 1*C
      Y: 2*B + 1*C
    Raw_C is shared: keep extra safety for C to avoid starving either product.
    """
    ip_a = float(inventory_dict.get(RAW_A, 0.0))
    ip_b = float(inventory_dict.get(RAW_B, 0.0))
    ip_c = float(inventory_dict.get(RAW_C, 0.0))

    n_retailers = 3.0
    sd_mult = math.sqrt(n_retailers)

    supplier_LT = 2
    # Cover enough time so assembler can keep DC supplied despite 2-day upstream and 2-day downstream lags
    # Conservative because assembler holding cost is low (0.3) and shortages propagate downstream.
    P = supplier_LT + 5  # 7 days

    # Forecast finished demand over the raw protection horizon
    exp_x = _sum_expected(PROD_X, period, P, n_retailers)
    exp_y = _sum_expected(PROD_Y, period, P, n_retailers)

    # Safety in finished-demand units (noise only), then translate through BOM
    ss_x = _safety_stock(PROD_X, P, z=1.6, multiplier_sd=sd_mult)
    ss_y = _safety_stock(PROD_Y, P, z=1.4, multiplier_sd=sd_mult)

    # Raw targets (inventory position) in raw units
    target_a = (exp_x + ss_x) + 60.0
    target_b = (2.0 * (exp_y + ss_y)) + 100.0

    # Shared C: need X+Y; add extra safety beyond mapped (to absorb allocation mismatch)
    extra_c = 80.0
    target_c = ((exp_x + ss_x) + (exp_y + ss_y)) + extra_c

    qa = _order_up_to(ip_a, target_a)
    qb = _order_up_to(ip_b, target_b)
    qc = _order_up_to(ip_c, target_c)

    orders = {}
    if qa > 0.0:
        orders["Supplier_A_0"] = {RAW_A: qa}
    if qb > 0.0:
        orders["Supplier_B_0"] = {RAW_B: qb}
    if qc > 0.0:
        orders["Supplier_C_0"] = {RAW_C: qc}
    return orders

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
