"""
场景4：双产品组装-分销网络
Supplier_A(1) ─┐
Supplier_B(1) ─┤
Supplier_C(1) ─┤→ Assembler(1) → DC(1) → Retailer(3)
               │
产品X = 1×A + 1×C
产品Y = 2×B + 1×C
组装完成后通过分销中心向零售门店供货。
LLM 需要控制 Retailer、DC、Assembler 三层的补货策略。
"""

import sys
import os
import math
import random as _builtin_random

_scenario_dir = os.path.dirname(os.path.abspath(__file__))
if _scenario_dir not in sys.path:
    sys.path.insert(0, _scenario_dir)

from policy_cache.policy import POLICY_MOUNTS

# ==========================================
# 1. 静态映射域
# ==========================================
metadata = {
    "domain": "Assembly_Distribution_Network",
    "description": "双产品组装网络：三个原材料供应商向组装厂供货，组装厂生产两种成品（产品X和产品Y），通过分销中心向零售门店供货。产品X需要原材料A和C，产品Y需要原材料B和C。",
    "products_mapping": {
        "1": "Raw_Material_A",
        "2": "Raw_Material_B",
        "3": "Raw_Material_C",
        "4": "Finished_Product_X",
        "5": "Finished_Product_Y"
    }
}

# ==========================================
# 2. 命令行参数契约
# ==========================================
cli_args_schema = {
    "seed": {
        "type": "int",
        "default": 42,
        "description": "随机种子，控制需求随机性。"
    }
}

# ==========================================
# 3. 标准输入契约
# ==========================================
stdin_schema = {
    "is_used": False,
    "format_description": "No stdin is used."
}

# ==========================================
# 4. 动态拓扑域
# ==========================================
topology = {
    "node_groups": {
        "Supplier_A": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Supplier_B": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"2": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Supplier_C": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"3": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Assembler": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {
                "1": 500.0,
                "2": 500.0,
                "3": 500.0,
                "4": 100.0,
                "5": 80.0
            },
            "lead_time": 2,
            "holding_cost": 0.3,
            "stockout_cost": 20.0,
            "bill_of_materials": {
                "4": {"1": 1.0, "3": 1.0},
                "5": {"2": 2.0, "3": 1.0}
            },
            "policy": {"type": "BS", "base_stock_level": 300}
        },
        "DC": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"4": 200.0, "5": 150.0},
            "lead_time": 2,
            "holding_cost": 0.5,
            "stockout_cost": 30.0,
            "policy": {"type": "sS", "reorder_point": 100, "order_up_to_level": 500}
        },
        "Retailer": {
            "role": "retailer",
            "count": 3,
            "initial_inventory": {"4": 50.0, "5": 40.0},
            "lead_time": 1,
            "holding_cost": 2.0,
            "stockout_cost": 80.0,
            "policy": {"type": "BS", "base_stock_level": 0}
        }
    },
    "edges": [
        {"from_group": "Supplier_A", "to_group": "Assembler"},
        {"from_group": "Supplier_B", "to_group": "Assembler"},
        {"from_group": "Supplier_C", "to_group": "Assembler"},
        {"from_group": "Assembler", "to_group": "DC"},
        {"from_group": "DC", "to_group": "Retailer"}
    ]
}

# ==========================================
# 5. 事件契约域
# ==========================================
event_schema = {
    "DAILY_SUMMARY": {
        "description": "每天结束时记录的节点期末状态总结。",
        "keys": {
            "inventory_qty": {"type": "float", "description": "期末实物库存数量。"},
            "holding_cost": {"type": "float", "description": "当日产生的仓储费用。"},
            "stockout_cost": {"type": "float", "description": "当日产生的缺货费用。"}
        }
    },
    "SHORTAGE_OCCURRED": {
        "description": "当节点面临需求但库存不足时触发。",
        "keys": {
            "missed_qty": {"type": "float", "description": "未能满足的缺货数量。"}
        }
    }
}

# ==========================================
# 6. 自定义逻辑域
# ==========================================

_PRODUCT_DEMAND = {
    4: {"base": 15, "noise": 5, "seasonal_amp": 6, "period": 21},
    5: {"base": 10, "noise": 4, "seasonal_amp": 4, "period": 14},
}

_demand_rng = None

def _get_demand_rng():
    global _demand_rng
    if _demand_rng is None:
        seed = int(globals().get('DYNAMIC_ARGS', {}).get('seed', 42))
        _demand_rng = _builtin_random.Random(seed)
    return _demand_rng


def retailer_demand_func(period: int, product_id: int) -> float:
    if product_id not in _PRODUCT_DEMAND:
        return 0.0

    rng = _get_demand_rng()
    params = _PRODUCT_DEMAND[product_id]

    base = float(params["base"])
    noise = rng.uniform(-float(params["noise"]), float(params["noise"]))
    seasonal = float(params["seasonal_amp"]) * math.sin(
        2 * math.pi * period / float(params["period"])
    )
    return max(0.0, base + noise + seasonal)


def retailer_holding_cost_func(inventory_dict: dict) -> float:
    cost = 0.0
    for pid in (4, 5):
        qty = inventory_dict.get(pid, 0.0)
        cost += max(0.0, qty) * 2.0
    return cost


custom_hooks = {
    "demand_func": {"Retailer": retailer_demand_func},
    "policy_func": POLICY_MOUNTS,
    "holding_cost_func": {"Retailer": retailer_holding_cost_func}
}

# ==========================================
# 7. 语义日志转换函数
# ==========================================
def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    logs = []

    for pid in (1, 2, 3, 4, 5):
        inv = raw_state.get("inventory_level", {}).get(str(pid), 0.0)
        h_cost = raw_state.get("holding_cost_incurred", 0.0)
        s_cost = raw_state.get("stockout_cost_incurred", 0.0)

        logs.append({
            "time": period,
            "node": semantic_node_id,
            "event": "DAILY_SUMMARY",
            "inventory_qty": float(inv),
            "holding_cost": float(h_cost),
            "stockout_cost": float(s_cost)
        })

    backorder = raw_state.get("backorder", 0.0)
    if backorder > 0:
        logs.append({
            "time": period,
            "node": semantic_node_id,
            "event": "SHORTAGE_OCCURRED",
            "missed_qty": float(backorder)
        })

    return logs

# ==========================================
# 8. 自动化裁判域
# ==========================================
def check_inventory_never_negative(logs: list):
    for log in logs:
        if log.get("event") == "DAILY_SUMMARY":
            assert log["inventory_qty"] >= 0, \
                f"负库存：第 {log['time']} 天，节点 {log['node']}"

def check_shortage_positive(logs: list):
    for log in logs:
        if log.get("event") == "SHORTAGE_OCCURRED":
            assert log["missed_qty"] > 0, \
                f"无效缺货记录：数量 <= 0"

tier2_checkers = [check_inventory_never_negative, check_shortage_positive]
tier3_checkers = []

# ==========================================
# 9. KPI 提取函数
# ==========================================
def extract_kpis(logs: list) -> dict:
    total_holding_cost = 0.0
    total_stockout_cost = 0.0
    total_missed_qty = 0.0

    for log in logs:
        if log.get("event") == "DAILY_SUMMARY":
            total_holding_cost += log.get("holding_cost", 0.0)
            total_stockout_cost += log.get("stockout_cost", 0.0)
        elif log.get("event") == "SHORTAGE_OCCURRED":
            total_missed_qty += log.get("missed_qty", 0.0)

    return {
        "total_holding_cost": float(total_holding_cost),
        "total_stockout_cost": float(total_stockout_cost),
        "total_missed_qty": float(total_missed_qty),
        "total_cost": float(total_holding_cost + total_stockout_cost)
    }
