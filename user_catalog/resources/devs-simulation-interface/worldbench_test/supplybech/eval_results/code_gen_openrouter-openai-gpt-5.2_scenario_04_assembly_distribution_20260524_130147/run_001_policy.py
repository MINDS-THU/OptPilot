import math

# product_id definitions
RAW_A, RAW_B, RAW_C = 1, 2, 3
PROD_X, PROD_Y = 4, 5

N_RETAILERS = 3

# Tuned safety factors (order-up-to)
Z_R = {PROD_X: 1.8, PROD_Y: 1.7}          # Retailer safety (LT=1)
Z_DC = {PROD_X: 2.8, PROD_Y: 2.6}         # DC safety (LT=2)

# Raw-material safety (LT=2, shared C). Modest buffers for robustness.
Z_RAW = {RAW_A: 0.8, RAW_B: 0.8, RAW_C: 1.2}

def _mu_per_retailer(pid: int, day: int) -> float:
    """Expected consumer demand per retailer (noise mean = 0)."""
    if pid == PROD_X:
        base, amp, period = 15.0, 6.0, 21.0
        return max(0.0, base + amp * math.sin(2.0 * math.pi * day / period))
    if pid == PROD_Y:
        base, amp, period = 10.0, 4.0, 14.0
        return max(0.0, base + amp * math.sin(2.0 * math.pi * day / period))
    return 0.0

def _std_per_retailer(pid: int) -> float:
    """Std of uniform noise component per retailer."""
    if pid == PROD_X:
        return 5.0 / math.sqrt(3.0)
    if pid == PROD_Y:
        return 4.0 / math.sqrt(3.0)
    return 0.0

def _order_up_to(ip: float, target: float) -> float:
    q = target - ip
    return q if q > 0.0 else 0.0

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Daily review (R=1), lead time = 1 day.
    Base-stock: cover next day's expected demand + safety.
    """
    req = {}
    L = 1
    for pid in (PROD_X, PROD_Y):
        mu_L = sum(_mu_per_retailer(pid, period + i) for i in range(1, L + 1))
        std_L = _std_per_retailer(pid) * math.sqrt(L)
        S = mu_L + Z_R[pid] * std_L
        ip = float(inventory_dict.get(pid, 0.0))
        q = _order_up_to(ip, S)
        if q > 0.0:
            req[pid] = q

    if not req:
        return {}
    return {"DC_0": req}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Daily review, lead time = 2 days from Assembler to DC.
    Base-stock: cover next 2 days of expected total (3 retailers) demand + safety.
    """
    req = {}
    L = 2
    for pid in (PROD_X, PROD_Y):
        mu_L = sum(_mu_per_retailer(pid, period + i) for i in range(1, L + 1)) * N_RETAILERS
        std_day = _std_per_retailer(pid) * math.sqrt(N_RETAILERS)
        std_L = std_day * math.sqrt(L)
        S = mu_L + Z_DC[pid] * std_L
        ip = float(inventory_dict.get(pid, 0.0))
        q = _order_up_to(ip, S)
        if q > 0.0:
            req[pid] = q

    if not req:
        return {}
    return {"Assembler_0": req}

def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Raw-material ordering to support near-future assembly under BOM constraints.

    Look ahead H=4 days of expected *total* consumer demand (3 retailers),
    translate to raw consumption by BOM:
      X: 1*A + 1*C
      Y: 2*B + 1*C
    Maintain raw base-stocks with modest safety, especially for shared C.
    """
    H = 4

    exp_x = sum(_mu_per_retailer(PROD_X, period + i) for i in range(1, H + 1)) * N_RETAILERS
    exp_y = sum(_mu_per_retailer(PROD_Y, period + i) for i in range(1, H + 1)) * N_RETAILERS

    mu_A = exp_x
    mu_B = 2.0 * exp_y
    mu_C = exp_x + exp_y

    stdx_day = _std_per_retailer(PROD_X) * math.sqrt(N_RETAILERS)
    stdy_day = _std_per_retailer(PROD_Y) * math.sqrt(N_RETAILERS)

    std_A = stdx_day * math.sqrt(H)
    std_B = (2.0 * stdy_day) * math.sqrt(H)
    std_C = math.sqrt((stdx_day ** 2) + (stdy_day ** 2)) * math.sqrt(H)

    S_A = mu_A + Z_RAW[RAW_A] * std_A
    S_B = mu_B + Z_RAW[RAW_B] * std_B
    S_C = mu_C + Z_RAW[RAW_C] * std_C

    ipA = float(inventory_dict.get(RAW_A, 0.0))
    ipB = float(inventory_dict.get(RAW_B, 0.0))
    ipC = float(inventory_dict.get(RAW_C, 0.0))

    qA = _order_up_to(ipA, S_A)
    qB = _order_up_to(ipB, S_B)
    qC = _order_up_to(ipC, S_C)

    out = {}
    if qA > 0.0:
        out["Supplier_A_0"] = {RAW_A: qA}
    if qB > 0.0:
        out["Supplier_B_0"] = {RAW_B: qB}
    if qC > 0.0:
        out["Supplier_C_0"] = {RAW_C: qC}
    return out

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
