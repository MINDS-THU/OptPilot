"""
场景1：简单线性链供应链
Factory(1) -> Central_DC(1) -> Retailer(3)
单产品，确定性周期需求。
"""

import sys
import os

# 注入场景目录到 sys.path，使 policy_cache 可被 import
_scenario_dir = os.path.dirname(os.path.abspath(__file__))
if _scenario_dir not in sys.path:
    sys.path.insert(0, _scenario_dir)

from policy_cache.policy import POLICY_MOUNTS

# ==========================================
# 1. 静态映射域
# ==========================================
metadata = {
    "domain": "Simple_Linear_Chain",
    "description": "三级供应链：工厂向中央仓库供货，中央仓库向3个零售门店供货。零售门店面临外部确定性需求。",
    "products_mapping": {
        "1": "Smartphone"
    }
}

# ==========================================
# 2. 命令行参数契约
# ==========================================
cli_args_schema = {}

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
            "initial_inventory": {"1": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Central_DC": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 1500.0},
            "lead_time": 3,
            "holding_cost": 1.0,
            "stockout_cost": 50.0,
            "policy": {"type": "sS", "reorder_point": 500, "order_up_to_level": 2000}
        },
        "Retailer": {
            "role": "retailer",
            "count": 3,
            "initial_inventory": {"1": 150.0},
            "lead_time": 1,
            "holding_cost": 2.5,
            "stockout_cost": 100.0,
            "policy": {"type": "BS", "base_stock_level": 0}
        }
    },
    "edges": [
        {"from_group": "Factory", "to_group": "Central_DC"},
        {"from_group": "Central_DC", "to_group": "Retailer"}
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

# 确定性周期需求模式
_DEMAND_SEQUENCE = [30.0, 30.0, 30.0, 10.0, 10.0, 10.0]

def retailer_demand_func(period: int, product_id: int) -> float:
    """
    确定性周期需求：按 6 天周期循环 [30, 30, 30, 10, 10, 10]。
    """
    if product_id != 1:
        return 0.0
    idx = (period - 1) % len(_DEMAND_SEQUENCE)
    return _DEMAND_SEQUENCE[idx]


def retailer_holding_cost_func(inventory_dict: dict) -> float:
    """
    零售商仓储成本：单位库存 2.5/天。
    """
    qty = inventory_dict.get(1, 0.0)
    return max(0.0, qty) * 2.5


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

    inv = raw_state.get("inventory_level", {}).get("1", 0.0)
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
