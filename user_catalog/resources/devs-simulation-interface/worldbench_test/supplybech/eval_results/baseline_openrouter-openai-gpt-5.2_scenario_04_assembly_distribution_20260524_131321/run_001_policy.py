import math
from typing import Dict

# ----------------------------
# Helpers: demand model + stats
# ----------------------------

PI2 = 2.0 * math.pi

DEMAND_PARAMS = {
    # product_id: (base, noise_half_range, seasonal_amp, cycle_len)
    4: (15.0, 5.0, 6.0, 21.0),  # Product_X
    5: (10.0, 4.0, 4.0, 14.0),  # Product_Y
}

N_RETAILERS = 3

LEADTIME_RETAILER = 1  # DC -> Retailer
LEADTIME_DC = 2        # Assembler -> DC
LEADTIME_SUPPLIER = 2  # Supplier -> Assembler

# Protection periods (lead time + 1 day review)
H_RETAILER = LEADTIME_RETAILER + 1  # 2 days
H_DC = LEADTIME_DC + 1              # 3 days
# Raw materials should protect for: supplier LT + downstream ship LT + 1 review
H_RAW = LEADTIME_SUPPLIER + LEADTIME_DC + 1  # 5 days
H_ASM_FG = LEADTIME_DC + 1  # 3 days (assembler finished goods buffer for DC)

def _expected_daily_demand(product_id: int, day: int) -> float:
    """Expected demand (noise mean=0) for a single retailer at a given day (1-indexed)."""
    base, _, amp, cycle = DEMAND_PARAMS[product_id]
    seasonal = amp * math.sin(PI2 * (day / cycle))
    return max(0.0, base + seasonal)

def _daily_sigma(product_id: int) -> float:
    """Std dev of uniform noise U(-r, r) where r=noise_half_range."""
    _, r, _, _ = DEMAND_PARAMS[product_id]
    return r / math.sqrt(3.0)

def _sum_expected_demand(product_id: int, start_day: int, horizon: int) -> float:
    return sum(_expected_daily_demand(product_id, start_day + k) for k in range(horizon))

def _norm_ppf(p: float) -> float:
    """
    Approximate inverse CDF (percent point function) of standard normal.
    Acklam's approximation; valid for 0<p<1.
    """
    # Coefficients in rational approximations
    a = [-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00]

    # Clamp away from 0/1
    p = min(1.0 - 1e-12, max(1e-12, p))

    plow = 0.02425
    phigh = 1.0 - plow

    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        num = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
        den = ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        return num / den
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        num = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
        den = ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        return -(num / den)

    q = p - 0.5
    r = q * q
    num = (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q
    den = (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    return num / den

def _z_from_costs(cu: float, holding_per_day: float, protection_days: int) -> float:
    """
    Newsvendor-style mapping: service level = Cu / (Cu + Co),
    where Co approximated as holding_per_day * protection_days.
    """
    co = max(1e-9, holding_per_day * float(protection_days))
    sl = cu / (cu + co)
    # Keep within a reasonable range
    sl = min(0.999, max(0.50, sl))
    return _norm_ppf(sl)

def _order_up_to(ip: float, S: float) -> float:
    return max(0.0, S - float(ip))

# ----------------------------
# Policy functions
# ----------------------------

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer orders from DC_0 for finished products 4 (X) and 5 (Y).
    Base-stock with seasonal mean forecast + safety stock.
    """
    # Costs at retailer: holding 2.0 per unit per day, shortage 80 per unit
    z = _z_from_costs(cu=80.0, holding_per_day=2.0, protection_days=H_RETAILER)

    orders = {}

    for pid in (4, 5):
        ip = float(inventory_dict.get(pid, 0.0))
        mu = _sum_expected_demand(pid, period, H_RETAILER)  # single retailer
        sigma = _daily_sigma(pid) * math.sqrt(H_RETAILER)
        S = mu + z * sigma
        q = _order_up_to(ip, S)
        if q > 1e-9:
            orders[pid] = q

    return {"DC_0": orders} if orders else {}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC orders from Assembler_0 for finished products 4 (X) and 5 (Y).
    Aggregates demand from 3 retailers.
    """
    # Costs at DC: holding 0.5 per unit per day, shortage 30 per unit
    z = _z_from_costs(cu=30.0, holding_per_day=0.5, protection_days=H_DC)

    orders = {}

    for pid in (4, 5):
        ip = float(inventory_dict.get(pid, 0.0))

        mu_1 = _sum_expected_demand(pid, period, H_DC)  # per retailer
        mu = N_RETAILERS * mu_1

        sigma_daily = _daily_sigma(pid)
        sigma_daily_agg = math.sqrt(N_RETAILERS) * sigma_daily
        sigma = sigma_daily_agg * math.sqrt(H_DC)

        S = mu + z * sigma
        q = _order_up_to(ip, S)
        if q > 1e-9:
            orders[pid] = q

    return {"Assembler_0": orders} if orders else {}

def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler orders raw materials from suppliers (A=1, B=2, C=3).
    Uses forecast of aggregate downstream demand, converts via BOM:
      X(4): 1*A + 1*C
      Y(5): 2*B + 1*C
    Raw_C shared: plan against combined consumption uncertainty.
    """
    # Costs at assembler: holding 0.3 per unit per day, shortage 20 per unit
    z_raw = _z_from_costs(cu=20.0, holding_per_day=0.3, protection_days=H_RAW)
    # Also maintain some finished-goods buffer at assembler to protect DC lead time
    z_fg = _z_from_costs(cu=30.0, holding_per_day=0.3, protection_days=H_ASM_FG)

    ip_A = float(inventory_dict.get(1, 0.0))
    ip_B = float(inventory_dict.get(2, 0.0))
    ip_C = float(inventory_dict.get(3, 0.0))
    ip_X = float(inventory_dict.get(4, 0.0))
    ip_Y = float(inventory_dict.get(5, 0.0))

    # Aggregate expected finished demand over raw protection horizon
    muX = N_RETAILERS * _sum_expected_demand(4, period, H_RAW)
    muY = N_RETAILERS * _sum_expected_demand(5, period, H_RAW)

    # Aggregate uncertainty (noise only)
    sigX_daily_agg = math.sqrt(N_RETAILERS) * _daily_sigma(4)
    sigY_daily_agg = math.sqrt(N_RETAILERS) * _daily_sigma(5)

    # Convert to raw consumption std dev
    sigA = sigX_daily_agg * math.sqrt(H_RAW)                 # A consumption = X
    sigB = (2.0 * sigY_daily_agg) * math.sqrt(H_RAW)         # B consumption = 2Y
    sigC_daily = math.sqrt(sigX_daily_agg**2 + sigY_daily_agg**2)  # C consumption = X+Y
    sigC = sigC_daily * math.sqrt(H_RAW)

    # Base raw targets (order-up-to levels)
    S_A = muX + z_raw * sigA
    S_B = (2.0 * muY) + z_raw * sigB
    S_C = (muX + muY) + z_raw * sigC

    # Finished goods buffer correction: if assembler FG position low, order extra raw
    # to help rebuild FG for near-term DC coverage (without being overly aggressive).
    muX_fg = N_RETAILERS * _sum_expected_demand(4, period, H_ASM_FG)
    muY_fg = N_RETAILERS * _sum_expected_demand(5, period, H_ASM_FG)

    sigX_fg = sigX_daily_agg * math.sqrt(H_ASM_FG)
    sigY_fg = sigY_daily_agg * math.sqrt(H_ASM_FG)

    S_X_fg = muX_fg + z_fg * sigX_fg
    S_Y_fg = muY_fg + z_fg * sigY_fg

    need_prod_X = max(0.0, S_X_fg - ip_X)
    need_prod_Y = max(0.0, S_Y_fg - ip_Y)

    # Apply partial adjustment to avoid double counting with horizon-based plan
    adj = 0.6
    S_A += adj * need_prod_X
    S_B += adj * (2.0 * need_prod_Y)
    S_C += adj * (need_prod_X + need_prod_Y)

    # Compute orders
    qA = _order_up_to(ip_A, S_A)
    qB = _order_up_to(ip_B, S_B)
    qC = _order_up_to(ip_C, S_C)

    orders = {}
    if qA > 1e-9:
        orders["Supplier_A_0"] = {1: qA}
    if qB > 1e-9:
        orders["Supplier_B_0"] = {2: qB}
    if qC > 1e-9:
        orders["Supplier_C_0"] = {3: qC}

    return orders

# ----------------------------
# Required mount dictionary
# ----------------------------
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}