import math

PRODUCT_ID = 1

# Demand model parameters (known from simulator)
BASE_DEMAND = 25.0
NOISE_UNIF_A = 8.0              # uniform(-A, A)
SEASON_AMP = 5.0
SEASON_PERIOD = 14.0

# Effective lead times: +1 day because orders are shipped the next day in the simulator.
L_RETAILER_EFF = 3   # 2 (transport) + 1 (order processing)
L_DC_EFF = 5         # 4 (transport) + 1

# Safety factors (tunable)
Z_RETAILER = 2.2
Z_DC = 2.0

def seasonal_multiplier(period: int) -> float:
    return SEASON_AMP * math.sin(2.0 * math.pi * (period % SEASON_PERIOD) / SEASON_PERIOD)

def forecast_mean_demand_per_retailer(day: int) -> float:
    # Expected demand = base + season; noise mean is 0
    return BASE_DEMAND + seasonal_multiplier(day)

def forecast_mu_over_horizon_per_retailer(start_day: int, horizon: int) -> float:
    return sum(forecast_mean_demand_per_retailer(start_day + i) for i in range(1, horizon + 1))

def sigma_noise_sum(horizon: int, daily_sigma: float) -> float:
    return math.sqrt(horizon) * daily_sigma

# daily noise variance for uniform(-A, A) is A^2/3
DAILY_SIGMA_RETAILER = math.sqrt((NOISE_UNIF_A ** 2) / 3.0)  # ~4.619

def retailer_policy_func(period, inventory_dict, node_name, node_group, upstream_name):
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    mu = forecast_mu_over_horizon_per_retailer(period, L_RETAILER_EFF)
    sigma = sigma_noise_sum(L_RETAILER_EFF, DAILY_SIGMA_RETAILER)
    S = mu + Z_RETAILER * sigma

    order_qty = max(0.0, S - ip)
    return {upstream_name: {PRODUCT_ID: float(order_qty)}}

def dc_policy_func(period, inventory_dict, node_name, node_group, upstream_name):
    ip = float(inventory_dict.get(PRODUCT_ID, 0.0))

    # DC serves 3 retailers; aggregate mean is 3x and noise variance sums (independent noises)
    mu_one = forecast_mu_over_horizon_per_retailer(period, L_DC_EFF)
    mu = 3.0 * mu_one

    daily_sigma_dc = math.sqrt(3.0) * DAILY_SIGMA_RETAILER
    sigma = sigma_noise_sum(L_DC_EFF, daily_sigma_dc)

    S = mu + Z_DC * sigma

    order_qty = max(0.0, S - ip)
    return {upstream_name: {PRODUCT_ID: float(order_qty)}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
