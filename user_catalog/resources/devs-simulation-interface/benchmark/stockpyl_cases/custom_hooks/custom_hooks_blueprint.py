"""Blueprint with policy/demand/cost custom hooks."""

metadata = {
    "domain": "Custom_Hooks_Demo",
    "description": "Three-stage network emphasizing custom policy and cost hooks.",
    "products_mapping": {"1": "Device"},
}

cli_args_schema = {
    "num_stores": {
        "type": "int",
        "default": 5,
        "description": "Number of retail stores.",
    },
    "base_demand": {
        "type": "float",
        "default": 22.0,
        "description": "Baseline daily demand at each store.",
    },
}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin data is required.",
}

topology = {
    "node_groups": {
        "Plant": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 900000.0},
            "policy": {"type": "BS", "base_stock_level": 900000},
        },
        "Hub": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 1800.0},
            "lead_time": 2,
            "holding_cost": 1.1,
            "stockout_cost": 60.0,
            "policy": {"type": "sS", "reorder_point": 500, "order_up_to_level": 2200},
        },
        "Store": {
            "role": "retailer",
            "count": "arg:num_stores",
            "initial_inventory": {"1": 150.0},
            "lead_time": 1,
            "stockout_cost": 130.0,
            "policy": {"type": "BS", "base_stock_level": 0},
        },
    },
    "edges": [
        {"from_group": "Plant", "to_group": "Hub"},
        {"from_group": "Hub", "to_group": "Store"},
    ],
}

event_schema = {
    "SUMMARY": {
        "description": "Daily summary event.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "Ending inventory."},
            "holding_cost": {"type": "float", "description": "Holding cost in the day."},
            "stockout_cost": {"type": "float", "description": "Stockout cost in the day."},
        },
    }
}


def store_demand(period: int, product_id: int) -> float:
    """Demand has weekday baseline and weekend spikes."""
    if product_id != 1:
        return 0.0
    base = float(globals().get("DYNAMIC_ARGS", {}).get("base_demand", 22.0))
    spike = 6.0 if period % 7 in (6, 0) else 0.0
    return max(0.0, base + spike)


def store_policy(period: int, inventory_dict: dict) -> dict:
    """Store orders from Hub_0 up to target level 130 when IP <= 40."""
    ip = float(inventory_dict.get(1, 0.0))
    if ip <= 40.0:
        return {"Hub_0": {1: max(0.0, 130.0 - ip)}}
    return {}


def store_holding_cost(inventory_dict: dict) -> float:
    """Store holding cost is 1.7 * positive inventory for product 1."""
    return 1.7 * max(0.0, float(inventory_dict.get(1, 0.0)))


def store_stockout_cost(shortage_dict: dict) -> float:
    """Store stockout cost is 130 * shortage quantity for product 1."""
    return 130.0 * max(0.0, float(shortage_dict.get(1, 0.0)))


custom_hooks = {
    "demand_func": {"Store": store_demand},
    "policy_func": {"Store": store_policy},
    "holding_cost_func": {"Store": store_holding_cost},
    "stockout_cost_func": {"Store": store_stockout_cost},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    return [
        {
            "time": period,
            "node": semantic_node_id,
            "event": "SUMMARY",
            "inventory_qty": float(raw_state.get("inventory_level", {}).get("1", 0.0)),
            "holding_cost": float(raw_state.get("holding_cost_incurred", 0.0)),
            "stockout_cost": float(raw_state.get("stockout_cost_incurred", 0.0)),
        }
    ]


def extract_kpis(logs: list) -> dict:
    total_h = 0.0
    total_s = 0.0
    for log in logs:
        if log.get("event") == "SUMMARY":
            total_h += float(log.get("holding_cost", 0.0))
            total_s += float(log.get("stockout_cost", 0.0))
    return {
        "total_holding_cost": total_h,
        "total_stockout_cost": total_s,
    }


test_cases = [
    {
        "case_name": "Hook_Base",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {"num_stores": 5, "base_demand": 22.0},
        "stdin_payload": "",
    }
]
