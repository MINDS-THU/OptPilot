import math
from typing import Dict

# ---------- Helper: inverse standard normal CDF (Acklam approximation) ----------
def _inv_norm_cdf(p: float) -> float:
    """
    Approximate inverse CDF (quantile) for standard normal distribution.
    Valid for 0 < p < 1.
    Reference: Peter John Acklam's approximation.
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

    # Define break-points
    plow = 0.02425
    phigh = 1 - plow

    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")

    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    elif p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                 ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    else:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)


# ---------- Demand forecast (expected value) ----------
def _forecast_mean_demand(product_id: int, day: int) -> float:
    """
    Expected demand for a retailer on a given day (ignoring random noise).
    Uses the provided sinusoidal seasonality.
    """
    if product_id == 1:
        base, amp, period = 18.0, 8.0, 21.0
    elif product_id == 2:
        base, amp, period = 12.0, 5.0, 14.0
    else:
        return 0.0

    # day starts from 1
    seasonal = amp * math.sin(2.0 * math.pi * (day / period))
    mu = base + seasonal
    return max(0.0, mu)


def _noise_sd(product_id: int) -> float:
    """
    Demand noise is assumed uniform in [-a, +a], so Var = a^2/3, SD = a/sqrt(3).
    """
    if product_id == 1:
        a = 6.0
    elif product_id == 2:
        a = 4.0
    else:
        a = 0.0
    return a / math.sqrt(3.0)


def retailer_policy_func(period: int, inventory_dict: Dict[int, float]) -> dict:
    """
    Multi-product order-up-to policy for Retailers ordering from DC_0.

    Args:
        period: current day index, starting from 1
        inventory_dict: {product_id: inventory_position}

    Returns:
        {} or {"DC_0": {product_id: order_qty, ...}}
    """
    # Lead time from DC to Retailer
    L = 2

    # Cost parameters at Retailer
    holding_cost_per_unit_per_day = 2.0
    stockout_cost_per_unit = 80.0

    # Critical ratio and z-value for safety stock
    # Overage cost approximated as holding cost for ~1 day of extra stock (daily review).
    overage_cost = holding_cost_per_unit_per_day * 1.0
    critical_ratio = stockout_cost_per_unit / (stockout_cost_per_unit + overage_cost)
    z = _inv_norm_cdf(critical_ratio)

    orders = {}

    for pid in (1, 2):
        inv_pos = float(inventory_dict.get(pid, 0.0))

        # Forecast demand over protection period (next L days)
        forecast_L = 0.0
        for k in range(1, L + 1):
            forecast_L += _forecast_mean_demand(pid, period + k)

        # Safety stock based on noise over L days
        sd_day = _noise_sd(pid)
        sd_L = sd_day * math.sqrt(L)
        safety = max(0.0, z * sd_L)

        # Order-up-to level (target inventory position)
        S = forecast_L + safety

        # Order quantity
        q = max(0.0, S - inv_pos)

        # Optional: suppress tiny orders to avoid churn
        if q >= 0.5:
            # Keep reasonable numeric stability; simulation typically accepts float
            orders[pid] = float(round(q, 2))

    if not orders:
        return {}

    return {"DC_0": orders}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}