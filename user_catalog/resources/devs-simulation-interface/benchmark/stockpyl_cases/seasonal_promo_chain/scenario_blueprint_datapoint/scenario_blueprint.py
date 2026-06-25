"""Seasonal + promotion demand scenario for description sufficiency checks."""

metadata = {
    "domain": "Seasonal_Promo_Chain",
    "description": "Factory to hubs to stores network with seasonal demand and a short promotion window.",
    "products_mapping": {"1": "SKU_Promo"},
}

cli_args_schema = {
    "num_stores": {
        "type": "int",
        "default": 6,
        "description": "Number of store nodes.",
    },
    "base_demand": {
        "type": "float",
        "default": 18.0,
        "description": "Baseline daily demand for each store.",
    },
    "promo_start": {
        "type": "int",
        "default": 6,
        "description": "First day of promotion window (inclusive).",
    },
    "promo_end": {
        "type": "int",
        "default": 8,
        "description": "Last day of promotion window (inclusive).",
    },
    "promo_lift": {
        "type": "float",
        "default": 10.0,
        "description": "Extra daily demand during promotion window.",
    },
    "simulate_time": {
        "type": "int",
        "default": 120,
        "description": "Simulation horizon in semantic days.",
    },
}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin data is required.",
}

topology = {
    "node_groups": {
        "Factory": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 800000.0},
            "policy": {"type": "BS", "base_stock_level": 800000},
        },
        "Hub": {
            "role": "distributor",
            "count": 2,
            "initial_inventory": {"1": 900.0},
            "lead_time": 2,
            "holding_cost": 0.9,
            "stockout_cost": 40.0,
            "policy": {"type": "sS", "reorder_point": 300, "order_up_to_level": 1300},
        },
        "Store": {
            "role": "retailer",
            "count": "arg:num_stores",
            "initial_inventory": {"1": 130.0},
            "lead_time": 1,
            "holding_cost": 1.4,
            "stockout_cost": 95.0,
            "policy": {"type": "BS", "base_stock_level": 0},
        },
    },
    "edges": [
        {"from_group": "Factory", "to_group": "Hub"},
        {"from_group": "Hub", "to_group": "Store"},
    ],
}

event_schema = {
    "DAILY_COST_STATE": {
        "description": "End-of-day state and incremental cost event.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "Ending on-hand inventory."},
            "holding_cost": {"type": "float", "description": "Incremental holding cost for the day."},
            "stockout_cost": {"type": "float", "description": "Incremental stockout cost for the day."},
            "demand_qty": {"type": "float", "description": "External demand realized during the day."},
            "fulfilled_qty": {"type": "float", "description": "Demand fulfilled during the day."},
        },
    }
}


def store_demand(period: int, product_id: int) -> float:
    """Store demand equals baseline + weekly pulse + optional promotion uplift for product 1."""
    if product_id != 1:
        return 0.0
    args = globals().get("DYNAMIC_ARGS", {})
    base = float(args.get("base_demand", 18.0))
    promo_start = int(args.get("promo_start", 6))
    promo_end = int(args.get("promo_end", 8))
    promo_lift = float(args.get("promo_lift", 10.0))
    weekly_pulse = 4.0 if period % 7 in (5, 6) else 0.0
    promo = promo_lift if promo_start <= period <= promo_end else 0.0
    return max(0.0, base + weekly_pulse + promo)


def store_policy(period: int, inventory_dict: dict) -> dict:
    """Store orders from Hub_0 up to 150 when inventory position is at or below 50."""
    ip = float(inventory_dict.get(1, 0.0))
    if ip <= 50.0:
        return {"Hub_0": {1: max(0.0, 150.0 - ip)}}
    return {}


def store_holding_cost(inventory_dict: dict) -> float:
    """Store holding cost is 1.4 multiplied by positive on-hand inventory."""
    return 1.4 * max(0.0, float(inventory_dict.get(1, 0.0)))


def store_stockout_cost(shortage_dict: dict) -> float:
    """Store stockout cost is 95.0 multiplied by shortage quantity."""
    return 95.0 * max(0.0, float(shortage_dict.get(1, 0.0)))


custom_hooks = {
    "demand_func": {"Store": store_demand},
    "policy_func": {"Store": store_policy},
    "holding_cost_func": {"Store": store_holding_cost},
    "stockout_cost_func": {"Store": store_stockout_cost},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    inv = float(raw_state.get("inventory_level", {}).get("1", 0.0))
    h_cost = float(raw_state.get("holding_cost_incurred", 0.0))
    s_cost = float(raw_state.get("stockout_cost_incurred", 0.0))
    demand = float(raw_state.get("demand", 0.0))
    fulfilled = float(raw_state.get("fulfilled_demand", 0.0))
    return [
        {
            "time": period,
            "node": semantic_node_id,
            "event": "DAILY_COST_STATE",
            "inventory_qty": inv,
            "holding_cost": h_cost,
            "stockout_cost": s_cost,
            "demand_qty": demand,
            "fulfilled_qty": fulfilled,
        }
    ]


def extract_kpis(logs: list) -> dict:
    total_holding = 0.0
    total_stockout = 0.0
    total_demand = 0.0
    total_fulfilled = 0.0
    for log in logs:
        if log.get("event") != "DAILY_COST_STATE":
            continue
        total_holding += float(log.get("holding_cost", 0.0))
        total_stockout += float(log.get("stockout_cost", 0.0))
        total_demand += float(log.get("demand_qty", 0.0))
        total_fulfilled += float(log.get("fulfilled_qty", 0.0))
    service_level = (total_fulfilled / total_demand) if total_demand > 0.0 else 1.0
    return {
        "total_holding_cost": total_holding,
        "total_stockout_cost": total_stockout,
        "service_level": service_level,
    }


test_cases = [
    {
        "case_name": "Promo_Base",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {
            "num_stores": 6,
            "base_demand": 18.0,
            "promo_start": 6,
            "promo_end": 8,
            "promo_lift": 10.0,
            "simulate_time": 120,
        },
        "stdin_payload": "",
    }
]
