"""
场景3：多产品共享仓储供应链
Factory(1) → DC(1) → Retailer(4)
两种产品共享仓储容量，需求相关（季节性脉冲同步）。
需要 LLM 实现 Retailer 层的多产品 policy_func，同时管理两种产品的补货。
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
    "domain": "Multi_Product_Shared_Capacity",
    "description": "单级分销网络：工厂向中央仓库供货，中央仓库向4个零售门店供货。两种产品共享仓储容量，需求具有相关性（季节性同步）。",
    "products_mapping": {
        "1": "Component_A",
        "2": "Component_B"
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
        "Factory": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 999999.0, "2": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "DC": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 600.0, "2": 400.0},
            "lead_time": 3,
            "holding_cost": 0.5,
            "stockout_cost": 30.0,
            "policy": {"type": "sS", "reorder_point": 200, "order_up_to_level": 1000}
        },
        "Retailer": {
            "role": "retailer",
            "count": 4,
            "initial_inventory": {"1": 80.0, "2": 50.0},
            "lead_time": 2,
            "holding_cost": 2.0,
            "stockout_cost": 80.0,
            "policy": {"type": "BS", "base_stock_level": 0}
        }
    },
    "edges": [
        {"from_group": "Factory", "to_group": "DC"},
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

# 产品需求参数
_PRODUCT_DEMAND = {
    1: {"base": 18, "noise": 6, "seasonal_amp": 8, "period": 21},
    2: {"base": 12, "noise": 4, "seasonal_amp": 5, "period": 14},
}

# 延迟初始化 RNG
_demand_rng = None

def _get_demand_rng():
    global _demand_rng
    if _demand_rng is None:
        seed = int(globals().get('DYNAMIC_ARGS', {}).get('seed', 42))
        _demand_rng = _builtin_random.Random(seed)
    return _demand_rng


def retailer_demand_func(period: int, product_id: int) -> float:
    """
    多产品随机需求：每种产品有独立的均值、波动和季节性。
    需求 = base + uniform(-noise, +noise) + seasonal_amp * sin(2*pi*period/cycle)
    随机性受 DYNAMIC_ARGS['seed'] 控制。
    """
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
    """
    零售商仓储成本：两种产品共享仓储，单位库存 2.0/天/件。
    """
    cost = 0.0
    for pid in (1, 2):
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

    for pid in (1, 2):
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
