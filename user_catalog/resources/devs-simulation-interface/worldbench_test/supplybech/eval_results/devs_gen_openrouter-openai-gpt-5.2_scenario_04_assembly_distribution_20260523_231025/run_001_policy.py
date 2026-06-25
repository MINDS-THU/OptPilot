
# test_policy_v12.py
# v9 but with slightly lower Assembler Raw_C safety (10 instead of 15).

import math


def _pid(pid) -> str:
    return str(int(pid))


def _get_inv(inv_dict: dict, pid) -> float:
    if pid in inv_dict:
        return float(inv_dict[pid])
    spid = _pid(pid)
    if spid in inv_dict:
        return float(inv_dict[spid])
    try:
        ipid = int(pid)
        if ipid in inv_dict:
            return float(inv_dict[ipid])
    except Exception:
        pass
    return 0.0


def _mean_demand_one_retailer(pid_finished: int, day: int) -> float:
    if int(pid_finished) == 4:
        base, amp, cycle = 15.0, 6.0, 21.0
    elif int(pid_finished) == 5:
        base, amp, cycle = 10.0, 4.0, 14.0
    else:
        return 0.0
    mu = base + amp * math.sin(2.0 * math.pi * (day / cycle))
    return mu if mu > 0.0 else 0.0


def _sum_forecast_one_retailer(pid_finished: int, start_day: int, horizon_days: int) -> float:
    return sum(_mean_demand_one_retailer(pid_finished, d) for d in range(start_day, start_day + horizon_days))


def _order_up_to(inv_pos: float, target: float) -> float:
    q = float(target) - float(inv_pos)
    return q if q > 0.0 else 0.0


def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    horizon = 2
    inv_x = _get_inv(inventory_dict, 4)
    inv_y = _get_inv(inventory_dict, 5)

    fc_x = _sum_forecast_one_retailer(4, period + 1, horizon)
    fc_y = _sum_forecast_one_retailer(5, period + 1, horizon)

    safety_x = 2.0
    safety_y = 1.0

    Sx = fc_x + safety_x
    Sy = fc_y + safety_y

    ox = _order_up_to(inv_x, Sx)
    oy = _order_up_to(inv_y, Sy)

    if ox <= 0.0 and oy <= 0.0:
        return {}
    return {"DC_0": {_pid(4): ox, _pid(5): oy}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    horizon = 3
    n_retailers = 3.0

    inv_x = _get_inv(inventory_dict, 4)
    inv_y = _get_inv(inventory_dict, 5)

    fc_x = n_retailers * _sum_forecast_one_retailer(4, period + 1, horizon)
    fc_y = n_retailers * _sum_forecast_one_retailer(5, period + 1, horizon)

    safety_x = 3.0
    safety_y = 3.0

    Sx = fc_x + safety_x
    Sy = fc_y + safety_y

    ox = _order_up_to(inv_x, Sx)
    oy = _order_up_to(inv_y, Sy)

    if ox <= 0.0 and oy <= 0.0:
        return {}
    return {"Assembler_0": {_pid(4): ox, _pid(5): oy}}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    horizon = 3
    n_retailers = 3.0

    inv_a = _get_inv(inventory_dict, 1)
    inv_b = _get_inv(inventory_dict, 2)
    inv_c = _get_inv(inventory_dict, 3)

    fc_x = n_retailers * _sum_forecast_one_retailer(4, period + 1, horizon)
    fc_y = n_retailers * _sum_forecast_one_retailer(5, period + 1, horizon)

    req_a = fc_x
    req_b = 2.0 * fc_y
    req_c = fc_x + fc_y

    safety_a = 0.0
    safety_b = 0.0
    safety_c = 10.0

    Sa = req_a + safety_a
    Sb = req_b + safety_b
    Sc = req_c + safety_c

    oa = _order_up_to(inv_a, Sa)
    ob = _order_up_to(inv_b, Sb)
    oc = _order_up_to(inv_c, Sc)

    out = {}
    if oa > 0.0:
        out["Supplier_A_0"] = {_pid(1): oa}
    if ob > 0.0:
        out["Supplier_B_0"] = {_pid(2): ob}
    if oc > 0.0:
        out["Supplier_C_0"] = {_pid(3): oc}
    return out


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
