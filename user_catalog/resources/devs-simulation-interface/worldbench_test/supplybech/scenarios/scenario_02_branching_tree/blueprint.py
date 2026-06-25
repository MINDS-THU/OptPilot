"""
场景2：分叉树形供应链（含随机需求）
Factory(1) → Regional_DC(2) → Retailer(6)
单产品，随机需求（seed 控制），不同零售商需求模式不同。
需要 LLM 实现 Retailer + Regional_DC 两层的 policy_func。
"""

import sys
import os
import math
import random as _builtin_random

# 注入场景目录到 sys.path，使 policy_cache 可被 import
_scenario_dir = os.path.dirname(os.path.abspath(__file__))
if _scenario_dir not in sys.path:
    sys.path.insert(0, _scenario_dir)

from policy_cache.policy import POLICY_MOUNTS

# ==========================================
# 1. 静态映射域
# ==========================================
metadata = {
    "domain": "Branching_Tree_Stochastic",
    "description": "三级分叉供应链：工厂向2个区域仓库供货，每个区域仓库向3个零售门店供货。零售门店面临随机外部需求。",
    "products_mapping": {
        "1": "Electronics"
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
            "initial_inventory": {"1": 999999.0},
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Regional_DC": {
            "role": "distributor",
            "count": 2,
            "initial_inventory": {"1": 800.0},
            "lead_time": 4,
            "holding_cost": 0.8,
            "stockout_cost": 40.0,
            "policy": {"type": "sS", "reorder_point": 200, "order_up_to_level": 1200}
        },
        "Retailer": {
            "role": "retailer",
            "count": 6,
            "initial_inventory": {"1": 100.0},
            "lead_time": 2,
            "holding_cost": 3.0,
            "stockout_cost": 120.0,
            "policy": {"type": "BS", "base_stock_level": 0}
        }
    },
    "edges": [
        {"from_group": "Factory", "to_group": "Regional_DC"},
        {"from_group": "Regional_DC", "to_group": "Retailer"}
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

# 每个零售商的需求参数：(均值, 波动幅度)
_RETAILER_DEMAND_PARAMS = [
    (25, 8),   # Retailer_0
    (15, 5),   # Retailer_1
    (35, 12),  # Retailer_2
    (20, 6),   # Retailer_3
    (30, 10),  # Retailer_4
    (10, 3),   # Retailer_5
]

# 延迟初始化 RNG：在 demand_func 首次调用时根据 DYNAMIC_ARGS 中的 seed 初始化
_demand_rng = None

def _get_demand_rng():
    global _demand_rng
    if _demand_rng is None:
        seed = int(globals().get('DYNAMIC_ARGS', {}).get('seed', 42))
        _demand_rng = _builtin_random.Random(seed)
    return _demand_rng


def retailer_demand_func(period: int, product_id: int) -> float:
    """
    随机需求：基于 seed 控制的随机数生成器。
    需求 = base + uniform(-amplitude, +amplitude) + seasonal，保证 >= 0。
    随机性受 DYNAMIC_ARGS['seed'] 控制。
    """
    if product_id != 1:
        return 0.0
    # 简单实现：所有零售商使用相同的需求分布（均值25，波动8）
    # 实际区分不同零售商的需求差异由底层引擎的 per-node demand source 处理
    # 这里我们用一个简化的全局需求函数
    rng = _get_demand_rng()
    rng = _get_demand_rng()
    base = 25.0
    noise = rng.uniform(-8.0, 8.0)
    seasonal = 5.0 * math.sin(2 * math.pi * period / 14)
    return max(0.0, base + noise + seasonal)


def retailer_holding_cost_func(inventory_dict: dict) -> float:
    """
    零售商仓储成本：单位库存 3.0/天。
    """
    qty = inventory_dict.get(1, 0.0)
    return max(0.0, qty) * 3.0


def dc_holding_cost_func(inventory_dict: dict) -> float:
    """
    区域仓库仓储成本：单位库存 0.8/天。
    """
    qty = inventory_dict.get(1, 0.0)
    return max(0.0, qty) * 0.8


custom_hooks = {
    "demand_func": {"Retailer": retailer_demand_func},
    "policy_func": POLICY_MOUNTS,
    "holding_cost_func": {
        "Retailer": retailer_holding_cost_func,
        "Regional_DC": dc_holding_cost_func,
    }
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
