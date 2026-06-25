"""Custom hook focused blueprint: demand/policy/holding/stockout."""

metadata = {
    "domain": "Custom_Hooks_Retail",
    "description": "Factory-Hub-Retail setup with custom retail hooks.",
    "products_mapping": {"1": "Device"},
}

cli_args_schema = {
    "num_retailers": {
        "type": "int",
        "default": 4,
        "description": "Retail node count.",
    },
    "base_demand": {
        "type": "float",
        "default": 18.0,
        "description": "Baseline market demand per retailer per day.",
    },
    "retail_unit_holding": {
        "type": "float",
        "default": 1.4,
        "description": "Retail unit holding cost.",
    },
}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin is used.",
}

topology = {
    "node_groups": {
        "Factory": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 250000.0},
            "policy": {"type": "BS", "base_stock_level": 250000},
        },
        "Hub": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 900.0},
            "lead_time": 2,
            "holding_cost": 0.8,
            "stockout_cost": 45.0,
            "policy": {"type": "sS", "reorder_point": 250, "order_up_to_level": 1400},
        },
        "Retail": {
            "role": "retailer",
            "count": "arg:num_retailers",
            "initial_inventory": {"1": 120.0},
            "lead_time": 1,
            "stockout_cost": 120.0,
            "policy": {"type": "BS", "base_stock_level": 0},
        },
    },
    "edges": [
        {"from_group": "Factory", "to_group": "Hub"},
        {"from_group": "Hub", "to_group": "Retail"},
    ],
}

event_schema = {
    "DAILY_SUMMARY": {
        "description": "Period summary for product 1.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "End inventory."},
            "holding_cost": {"type": "float", "description": "Holding cost in current day."},
            "stockout_cost": {"type": "float", "description": "Stockout cost in current day."},
        },
    },
    "SHORTAGE_OCCURRED": {
        "description": "Backorder trigger.",
        "keys": {
            "missed_qty": {"type": "float", "description": "Unfulfilled quantity."},
        },
    },
}


def retail_demand_func(period: int, product_id: int) -> float:
    if product_id != 1:
        return 0.0

    args = globals().get("DYNAMIC_ARGS", {})
    base = float(args.get("base_demand", 18.0))
    seasonal = 5.0 if period % 7 in (6, 0) else 0.0
    return max(0.0, base + seasonal)


def retail_policy_func(period: int, inventory_dict: dict) -> dict:
    ip = float(inventory_dict.get(1, 0.0))
    orders = {}
    if ip <= 35.0:
        orders["Hub_0"] = {1: 95.0 - ip}
    return orders


def retail_holding_cost_func(inventory_dict: dict) -> float:
    args = globals().get("DYNAMIC_ARGS", {})
    unit_cost = float(args.get("retail_unit_holding", 1.4))
    return max(0.0, float(inventory_dict.get(1, 0.0))) * unit_cost


def retail_stockout_cost_func(shortage_dict: dict) -> float:
    shortage = max(0.0, float(shortage_dict.get(1, 0.0)))
    return shortage * 120.0


custom_hooks = {
    "demand_func": {"Retail": retail_demand_func},
    "policy_func": {"Retail": retail_policy_func},
    "holding_cost_func": {"Retail": retail_holding_cost_func},
    "stockout_cost_func": {"Retail": retail_stockout_cost_func},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    inv = float(raw_state.get("inventory_level", {}).get("1", 0.0))
    holding_cost = float(raw_state.get("holding_cost_incurred", 0.0))
    stockout_cost = float(raw_state.get("stockout_cost_incurred", 0.0))

    logs = [
        {
            "time": period,
            "node": semantic_node_id,
            "event": "DAILY_SUMMARY",
            "inventory_qty": inv,
            "holding_cost": holding_cost,
            "stockout_cost": stockout_cost,
        }
    ]

    shortage = max(0.0, float(raw_state.get("backorder", 0.0)))
    if shortage > 0:
        logs.append(
            {
                "time": period,
                "node": semantic_node_id,
                "event": "SHORTAGE_OCCURRED",
                "missed_qty": shortage,
            }
        )

    return logs


def check_shortage_positive(logs: list):
    for log in logs:
        if log.get("event") == "SHORTAGE_OCCURRED":
            assert float(log["missed_qty"]) > 0.0


tier2_checkers = [check_shortage_positive]
tier3_checkers = []


def extract_kpis(logs: list) -> dict:
    total_holding = 0.0
    total_stockout = 0.0
    total_shortage = 0.0
    for log in logs:
        if log.get("event") == "DAILY_SUMMARY":
            total_holding += float(log.get("holding_cost", 0.0))
            total_stockout += float(log.get("stockout_cost", 0.0))
        elif log.get("event") == "SHORTAGE_OCCURRED":
            total_shortage += float(log.get("missed_qty", 0.0))

    return {
        "system_total_holding_cost": float(total_holding),
        "system_total_stockout_cost": float(total_stockout),
        "system_total_shortage_qty": float(total_shortage),
    }


test_cases = [
    {
        "case_name": "Retail_Hook_Base",
        "runs": 10,
        "oracle_runs": 20,
        "cli_kwargs": {"num_retailers": 4, "base_demand": 18.0, "retail_unit_holding": 1.4},
        "stdin_payload": "",
    },
    {
        "case_name": "Retail_Hook_Stress",
        "runs": 10,
        "oracle_runs": 20,
        "cli_kwargs": {"num_retailers": 12, "base_demand": 27.0, "retail_unit_holding": 2.1},
        "stdin_payload": "",
    },
]
