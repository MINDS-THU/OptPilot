import math
from typing import Dict

# -----------------------------
# Helpers: demand forecast + inverse normal CDF (Acklam approx)
# -----------------------------

def _inv_norm_cdf(p: float) -> float:
    """Approximate inverse CDF (quantile) of standard normal N(0,1).
    Peter J. Acklam's approximation. Valid for 0<p<1.
    """
    p = min(max(p, 1e-12), 1 - 1e-12)

    # Coefficients in rational approximations
    a = [-3.969683028665376e+01,
          2.209460984245205e+02,
         -2.759285104469687e+02,
          1.383577518672690e+02,
         -3.066479806614716e+01,
          2.506628277459239e+00]

    b = [-5.447609879822406e+01,
          1.615858368580409e+02,
         -1.556989798598866e+02,
          6.680131188771972e+01,
         -1.328068155288572e+01]

    c = [-7.784894002430293e-03,
         -3.223964580411365e-01,
         -2.400758277161838e+00,
         -2.549732539343734e+00,
          4.374664141464968e+00,
          2.938163982698783e+00]

    d = [ 7.784695709041462e-03,
          3.224671290700398e-01,
          2.445134137142996e+00,
          3.754408661907416e+00]

    # Define break-points.
    plow = 0.02425
    phigh = 1 - plow

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        num = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
        den = ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        return num / den
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        num = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5])
        den = ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        return -(num / den)

    q = p - 0.5
    r = q * q
    num = (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q
    den = (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4]) * r + 1.0)
    return num / den


def _seasonal_mean_demand(product_id: int, day: int) -> float:
    """Expected daily demand (mean), noise mean=0, with seasonality."""
    if product_id == 4:  # Product_X
        base, amp, cycle = 15.0, 6.0, 21.0
    elif product_id == 5:  # Product_Y
        base, amp, cycle = 10.0, 4.0, 14.0
    else:
        return 0.0
    mean = base + amp * math.sin(2.0 * math.pi * day / cycle)
    return max(0.0, mean)


def _uniform_sd(noise_half_width: float) -> float:
    # For U(-a, a), sd = a / sqrt(3)
    return float(noise_half_width) / math.sqrt(3.0)


def _sum_forecast(product_id: int, start_day: int, horizon_days: int) -> float:
    return sum(_seasonal_mean_demand(product_id, d) for d in range(start_day, start_day + horizon_days))


def _order_up_to(inventory_position: float, mu_L: float, sd_L: float, z: float, alpha: float = 1.0) -> float:
    """Base-stock order: order = alpha * max(0, S - IP) where S=mu_L + z*sd_L."""
    S = mu_L + z * sd_L
    return max(0.0, alpha * (S - float(inventory_position)))


# -----------------------------
# Policies
# -----------------------------

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer: daily order-up-to with lead time 1 day, high stockout cost => high service level.
    """
    L = 1  # DC -> Retailer lead time
    # Critical fractile ~ Cu/(Cu+Co) with Cu=80, Co=2
    p = 80.0 / (80.0 + 2.0)
    z = _inv_norm_cdf(p)

    # Demand noise (per retailer)
    sd_x = _uniform_sd(5.0)
    sd_y = _uniform_sd(4.0)

    # Forecast over lead time
    mu_x_L = _sum_forecast(4, period, L)
    mu_y_L = _sum_forecast(5, period, L)
    sd_x_L = sd_x * math.sqrt(L)
    sd_y_L = sd_y * math.sqrt(L)

    ip_x = float(inventory_dict.get(4, 0.0))
    ip_y = float(inventory_dict.get(5, 0.0))

    # Mild damping to reduce bullwhip while keeping service high
    alpha = 0.9

    qx = _order_up_to(ip_x, mu_x_L, sd_x_L, z, alpha=alpha)
    qy = _order_up_to(ip_y, mu_y_L, sd_y_L, z, alpha=alpha)

    orders = {}
    if qx > 1e-9 or qy > 1e-9:
        orders["DC_0"] = {4: float(qx), 5: float(qy)}
    return orders


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC: aggregate of 3 retailers, lead time 2 days, moderate holding cost, meaningful stockout cost.
    """
    L = 2  # Assembler -> DC lead time
    n_retailers = 3

    # Critical fractile with Cu=30, Co=0.5
    p = 30.0 / (30.0 + 0.5)
    z = _inv_norm_cdf(p)

    # Aggregate expected demand (approx end-customer demand sum)
    mu_x_L = sum(n_retailers * _seasonal_mean_demand(4, d) for d in range(period, period + L))
    mu_y_L = sum(n_retailers * _seasonal_mean_demand(5, d) for d in range(period, period + L))

    # Aggregate sd assuming independent retailer demands
    sd_x_day = math.sqrt(n_retailers) * _uniform_sd(5.0)
    sd_y_day = math.sqrt(n_retailers) * _uniform_sd(4.0)
    sd_x_L = sd_x_day * math.sqrt(L)
    sd_y_L = sd_y_day * math.sqrt(L)

    ip_x = float(inventory_dict.get(4, 0.0))
    ip_y = float(inventory_dict.get(5, 0.0))

    # Slight damping (DC orders are major bullwhip source)
    alpha = 0.85

    qx = _order_up_to(ip_x, mu_x_L, sd_x_L, z, alpha=alpha)
    qy = _order_up_to(ip_y, mu_y_L, sd_y_L, z, alpha=alpha)

    orders = {}
    if qx > 1e-9 or qy > 1e-9:
        orders["Assembler_0"] = {4: float(qx), 5: float(qy)}
    return orders


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler: order raw materials A/B/C from suppliers (lead time 2).
    Uses multi-day raw coverage to buffer downstream lead times and demand noise.
    BOM:
      X: 1*A + 1*C
      Y: 2*B + 1*C
    """
    L_raw = 2  # Supplier -> Assembler lead time
    n_retailers = 3

    # Coverage horizon: raw lead time + outbound lead time (Assembler->DC is 2)
    # This helps prevent frequent raw shortages that would explode downstream stockouts.
    H = L_raw + 2  # =4 days

    # Critical fractile with Cu=20, Co=0.3 (for assembler-level shortages)
    p = 20.0 / (20.0 + 0.3)
    z = _inv_norm_cdf(p)

    # Aggregate finished-goods demand forecast (end demand proxy)
    mu_x_H = sum(n_retailers * _seasonal_mean_demand(4, d) for d in range(period, period + H))
    mu_y_H = sum(n_retailers * _seasonal_mean_demand(5, d) for d in range(period, period + H))

    # Aggregate demand sd per day
    sd_x_day = math.sqrt(n_retailers) * _uniform_sd(5.0)
    sd_y_day = math.sqrt(n_retailers) * _uniform_sd(4.0)

    # Over horizon H
    sd_x_H = sd_x_day * math.sqrt(H)
    sd_y_H = sd_y_day * math.sqrt(H)

    # Raw requirements (mean over horizon)
    mu_A = mu_x_H
    mu_B = 2.0 * mu_y_H
    mu_C = mu_x_H + mu_y_H

    # Raw sd over horizon (linear scaling; assume X and Y independent)
    sd_A = sd_x_H
    sd_B = 2.0 * sd_y_H
    sd_C = math.sqrt(sd_x_H ** 2 + sd_y_H ** 2)

    # Slight prioritization toward X on shared C via extra safety tied to X (since X is higher-volume)
    # Implemented as a small additional buffer proportional to X uncertainty.
    sd_C_eff = math.sqrt(sd_C ** 2 + (0.35 * sd_x_H) ** 2)

    ip_A = float(inventory_dict.get(1, 0.0))
    ip_B = float(inventory_dict.get(2, 0.0))
    ip_C = float(inventory_dict.get(3, 0.0))

    # Damping to avoid oscillatory raw orders
    alpha = 0.9

    qA = _order_up_to(ip_A, mu_A, sd_A, z, alpha=alpha)
    qB = _order_up_to(ip_B, mu_B, sd_B, z, alpha=alpha)
    qC = _order_up_to(ip_C, mu_C, sd_C_eff, z, alpha=alpha)

    orders = {}
    if qA > 1e-9:
        orders["Supplier_A_0"] = {1: float(qA)}
    if qB > 1e-9:
        orders["Supplier_B_0"] = {2: float(qB)}
    if qC > 1e-9:
        orders["Supplier_C_0"] = {3: float(qC)}
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}