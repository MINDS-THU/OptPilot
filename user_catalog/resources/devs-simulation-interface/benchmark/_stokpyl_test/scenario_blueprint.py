"""
场景名称：消费电子产品多级分销网络
说明：此蓝图定义了一个包含工厂、中央仓和多个零售商的三级供应链网络，并演示了如何接受 stdin 动态需求流以及使用 CLI 参数动态控制网络规模。
"""

import sys
# 允许在此处 import 外挂的统计 SDK，底层引擎会负责注入
import kpi_utils

# ==========================================
# 1. 静态映射域
# ==========================================
metadata = {
    "domain": "Consumer_Electronics",
    "description": "本场景模拟了一个消费电子产品（智能手机）的三级供应链。中央仓库向工厂订货，分散各地的零售门店向中央仓订货。零售门店将直接面临外部市场的每日需求冲击。",
    "products_mapping": {
        "1": "Smartphone"
    }
}

# ==========================================
# 2. 命令行参数契约
# ==========================================
cli_args_schema = {
    "num_retailers": {
        "type": "int",
        "default": 3,
        "description": "系统中底层零售门店的数量。"
    },
    "retailer_holding_cost": {
        "type": "float",
        "default": 2.5,
        "description": "零售门店单位手机每天的仓储成本。"
    }
}

# ==========================================
# 3. 标准输入契约
# ==========================================
stdin_schema = {
    "is_used": True,
    "format_description": "多行文本输入。每一行代表一天，包含一个浮点数，表示当天每个零售商面临的市场基础需求量。以空格或换行符分隔。"
}

# ==========================================
# 4. 动态拓扑域
# ==========================================
topology = {
    "node_groups": {
        "Factory": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 999999.0}, # 【必须配置初始库存】
            "policy": {"type": "BS", "base_stock_level": 999999}
        },
        "Central_DC": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 1500.0}, # 【必须配置初始库存】
            "lead_time": 3,
            "holding_cost": 1.0,
            "stockout_cost": 50.0,
            "policy": {"type": "sS", "reorder_point": 500, "order_up_to_level": 2000} 
        },
        "Retailer": {
            "role": "retailer",
            "count": "arg:num_retailers", 
            "initial_inventory": {"1": 150.0}, # 【必须配置初始库存】
            "lead_time": 1,
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
# 5. 事件契约域 (用于 Tier 1 强类型排雷)
# ==========================================
event_schema = {
    "DAILY_SUMMARY": {
        "description": "每天结束时记录的节点期末状态总结。",
        "keys": {
            "inventory_qty": {"type": "float", "description": "期末 Smartphone 的实物库存数量。"},
            "holding_cost": {"type": "float", "description": "当日产生的仓储费用。"}
        }
    },
    "SHORTAGE_OCCURRED": {
        "description": "当节点面临下游或外部需求，但库存不足以全额满足时触发。",
        "keys": {
            "missed_qty": {"type": "float", "description": "当日未能满足而转化为欠单缺货的数量（必定大于 0）。"}
        }
    }
}

# ==========================================
# 6. 自定义逻辑域 (务必提供 Docstring)
# ==========================================

# 【更新】：增加 product_id 参数，以符合多产品扩展签名
def retailer_demand_func(period: int, product_id: int) -> float:
    """
    [物理定律说明]：
    零售商面临的市场需求完全由外部标准输入流 (stdin) 决定。
    如果在第 N 天，stdin 中有对应的数据，则需求量为该数据；
    如果 stdin 数据耗尽或未提供，则所有零售商每天面临默认的 20 台需求量。
    """
    if product_id != 1:
        return 0.0

    # 安全读取底层引擎注入的全局变量 STDIN_DATA
    raw_data = globals().get('STDIN_DATA', '')
    parsed_sequence = [float(x) for x in raw_data.split()] if raw_data else []
    
    if period - 1 < len(parsed_sequence):
        return parsed_sequence[period - 1]
    return 20.0


# 【更新】：剔除了缝合的多产品逻辑，专注于产品 1，符合路由字典规范
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    [物理定律说明]：
    零售商采用严格的 (s, S) 补货策略。补货点 s 固定为 50，目标库存 S 固定为 150。
    当账面库存 (inventory_position) 小于等于 50 时，向上游发出采购请求。
    """
    orders = {}
    
    # 策略：对于产品 1，如果少于等于 50 台，向内部上游 (Central_DC_0) 订货补齐至 150
    ip_1 = inventory_dict.get(1, 0.0)
    if ip_1 <= 50:
        if "Central_DC_0" not in orders: 
            orders["Central_DC_0"] = {}
        orders["Central_DC_0"][1] = 150.0 - ip_1
        
    return orders


# 【更新】：入参改为 inventory_dict，完美契合 Oracle Runner 包装器
def retailer_holding_cost_func(inventory_dict: dict) -> float:
    """
    [物理定律说明]：
    零售商的持有成本系数可通过外部参数动态调整。将产品 1 的库存数量乘以动态参数计算得出。
    """
    args = globals().get('DYNAMIC_ARGS', {})
    unit_cost = float(args.get("retailer_holding_cost", 2.5))
    
    # 获取产品 1 的正向实物库存
    qty_1 = inventory_dict.get(1, 0.0)
    
    return qty_1 * unit_cost

custom_hooks = {
    "demand_func": {"Retailer": retailer_demand_func},
    "policy_func": {"Retailer": retailer_policy_func},
    "holding_cost_func": {"Retailer": retailer_holding_cost_func}
}

# ==========================================
# 7. 语义日志转换函数
# ==========================================
def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    logs = []
    
    # 提取期末库存与仓储成本
    inv = raw_state.get("inventory_level", {}).get("1", 0.0)
    h_cost = raw_state.get("holding_cost_incurred", 0.0)
    
    logs.append({
        "time": period,
        "node": semantic_node_id,
        "event": "DAILY_SUMMARY",
        "inventory_qty": float(inv),
        "holding_cost": float(h_cost)
    })
    
    # 提取缺货事件
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
# 8. 自动化裁判域 (Tier 2 & Tier 3)
# ==========================================
def check_inventory_never_negative(logs: list):
    """检查任何节点在任何时刻的实物库存不能为负数"""
    for log in logs:
        if log.get("event") == "DAILY_SUMMARY":
            assert log["inventory_qty"] >= 0, f"违反物理定律：在第 {log['time']} 天，节点 {log['node']} 出现了负库存！"

def check_shortage_must_be_positive(logs: list):
    """检查如果记录了缺货事件，缺货量必须有实质数值"""
    for log in logs:
        if log.get("event") == "SHORTAGE_OCCURRED":
            assert log["missed_qty"] > 0, f"无效的缺货记录：数量不能小于等于 0"

tier2_checkers = [check_inventory_never_negative, check_shortage_must_be_positive]
tier3_checkers = [] 

# ==========================================
# 9. 全局 KPI 提取函数 (用于 Tier 4)
# ==========================================
def extract_kpis(logs: list) -> dict:
    """提取单次仿真的核心分布指标"""
    total_holding_cost = 0.0
    total_shortage_qty = 0.0
    
    for log in logs:
        if log.get("event") == "DAILY_SUMMARY":
            total_holding_cost += log.get("holding_cost", 0.0)
        elif log.get("event") == "SHORTAGE_OCCURRED":
            total_shortage_qty += log.get("missed_qty", 0.0)
            
    return {
        "system_total_holding_cost": float(total_holding_cost),
        "system_total_missed_qty": float(total_shortage_qty)
    }

# ==========================================
# 10. 评测驱动集
# ==========================================
test_cases = [
    {
        "case_name": "Base_Condition",
        "runs": 30,            
        "oracle_runs": 500,    
        "cli_kwargs": {
            "num_retailers": 3,
            "retailer_holding_cost": 2.5
        },
        "stdin_payload": "30.0\n30.0\n30.0\n10.0\n10.0\n10.0\n" 
    },
    {
        "case_name": "Scale_Stress_Test",
        "runs": 30,
        "oracle_runs": 500,
        "cli_kwargs": {
            "num_retailers": 20, 
            "retailer_holding_cost": 5.0
        },
        "stdin_payload": "50.0\n" * 100 
    }
]