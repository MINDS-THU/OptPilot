import math
from typing import Dict, Any

PRODUCT_ID = 1

# --- Utilities ---
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def _get_node_name(inventory_dict: dict) -> str:
    """
    Best-effort extraction of node name from possible simulator payloads.
    If the simulator only passes {product_id: inventory_position}, this may be empty.
    """
    for k in ("node_name", "name", "__node_name__", "__node__", "_node", "node"):
        v = inventory_dict.get(k, None)
        if isinstance(v, str) and v:
            return v
    return ""

def _inv_norm_cdf(p: float) -> float:
    """
    Approximation of inverse standard normal CDF (Acklam's approximation).
    Valid for 0<p<1.
    """
    p = _clamp(p, 1e-12, 1 - 1e-12)

    # Coefficients in rational approximations
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]

    # Define break-points
    plow = 0.02425
    phigh = 1 - plow

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    elif p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                 ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    else:
        q = p - 0.5
        r = q*q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)

def _seasonal_mean(period: int, base: float = 25.0, amp: float = 5.0, cycle: int = 14) -> float:
    """
    Deterministic seasonal mean demand component described in the prompt.
    """
    # Use period starting at 1; shift so day1 isn't forced to sin(0)=0 if you prefer.
    # Here we keep it simple and stable.
    theta = 2.0 * math.pi * ((period - 1) % cycle) / cycle
    return base + amp * math.sin(theta)

# --- Policy parameters (tunable) ---
# Demand noise approx: uniform ±8 -> std = 8/sqrt(3) ≈ 4.62
RETAILER_NOISE_STD = 8.0 / math.sqrt(3.0)
DC_NOISE_STD = math.sqrt(3.0) * RETAILER_NOISE_STD  # sum of 3 retailers (independent approx)

# Lead times
RETAILER_LT = 2  # days from DC to retailer
DC_LT = 4        # days from factory to DC
REVIEW_PERIOD = 1  # daily ordering

# Costs (used to set service levels approximately)
H_RETAILER = 3.0
CU_RETAILER = 120.0
H_DC = 0.8
CU_DC = 40.0

def _service_level(Cu: float, h: float, L: int) -> float:
    """
    Approximate critical fractile for a base-stock policy:
    treat effective overage cost as holding over the lead-time window.
    """
    Co = h * max(1, L)  # simplistic effective overage cost scaling with lead time
    return _clamp(Cu / (Cu + Co), 0.50, 0.999)

RETAILER_SL = _service_level(CU_RETAILER, H_RETAILER, RETAILER_LT)  # ~0.95+
DC_SL = _service_level(CU_DC, H_DC, DC_LT)                          # ~0.92+

RETAILER_Z = _inv_norm_cdf(RETAILER_SL)
DC_Z = _inv_norm_cdf(DC_SL)

# Extra safety multipliers (small) to be robust to correlation/seasonality mismatch
RETAILER_SAFETY_MULT = 1.05
DC_SAFETY_MULT = 1.10

# Practical caps to avoid extreme orders if inventory_position is very negative/positive
RETAILER_MAX_ORDER = 250.0
DC_MAX_ORDER = 1200.0


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer: dynamic base-stock with seasonality.
    Order-up-to level covers protection period = lead time + review period.
    """
    inv_pos = _safe_float(inventory_dict.get(PRODUCT_ID, 0.0), 0.0)

    # Identify correct upstream DC if possible
    node_name = _get_node_name(inventory_dict)
    upstream = "Regional_DC_0"
    if node_name:
        # Retailer_0/1/2 -> DC_0 ; Retailer_3/4/5 -> DC_1
        # Robust parsing: look for trailing integer
        idx = None
        try:
            idx = int(node_name.split("_")[-1])
        except Exception:
            idx = None
        if idx is not None and idx >= 3:
            upstream = "Regional_DC_1"

    # Forecast mean demand (seasonal)
    mu_daily = _seasonal_mean(period, base=25.0, amp=5.0, cycle=14)

    # Protection period (order placed today arrives after LT; review daily)
    pp = RETAILER_LT + REVIEW_PERIOD  # 3 days

    mu_pp = mu_daily * pp
    sigma_pp = RETAILER_NOISE_STD * math.sqrt(pp)

    S = mu_pp + (RETAILER_Z * sigma_pp * RETAILER_SAFETY_MULT)

    # Order quantity to raise inventory position to S
    order_qty = max(0.0, S - inv_pos)

    # Stabilize: cap and avoid tiny churn
    order_qty = min(order_qty, RETAILER_MAX_ORDER)
    if order_qty < 1e-6:
        order_qty = 0.0

    return {upstream: {PRODUCT_ID: float(order_qty)}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC: dynamic base-stock with seasonality, sized to cover 3 retailers.
    Uses longer protection period due to 4-day factory lead time.
    """
    inv_pos = _safe_float(inventory_dict.get(PRODUCT_ID, 0.0), 0.0)

    # Mean daily demand seen by DC = sum of 3 retailers
    mu_daily_retailer = _seasonal_mean(period, base=25.0, amp=5.0, cycle=14)
    mu_daily = 3.0 * mu_daily_retailer

    # Protection period for DC
    pp = DC_LT + REVIEW_PERIOD  # 5 days

    mu_pp = mu_daily * pp
    sigma_pp = DC_NOISE_STD * math.sqrt(pp)

    S = mu_pp + (DC_Z * sigma_pp * DC_SAFETY_MULT)

    order_qty = max(0.0, S - inv_pos)

    # Additional bullwhip damping: soft cap daily change by limiting max order
    order_qty = min(order_qty, DC_MAX_ORDER)
    if order_qty < 1e-6:
        order_qty = 0.0

    return {"Factory_0": {PRODUCT_ID: float(order_qty)}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}