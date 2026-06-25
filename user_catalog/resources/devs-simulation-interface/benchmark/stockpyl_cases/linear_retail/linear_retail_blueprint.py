"""Simple linear supply chain blueprint example."""

metadata = {
    "domain": "Linear_Retail",
    "description": "Factory -> DC -> Retailer linear chain for a single product.",
    "products_mapping": {"1": "SKU_A"},
}

cli_args_schema = {
    "num_retailers": {
        "type": "int",
        "default": 3,
        "description": "Number of retailers connected to the DC.",
    }
}

stdin_schema = {
    "is_used": True,
    "format_description": "Whitespace separated daily demand sequence for each retailer.",
}

topology = {
    "node_groups": {
        "Factory": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 500000.0},
            "policy": {"type": "BS", "base_stock_level": 500000},
        },
        "DC": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 1200.0},
            "lead_time": 2,
            "holding_cost": 0.8,
            "stockout_cost": 35.0,
            "policy": {"type": "sS", "reorder_point": 350, "order_up_to_level": 1500},
        },
        "Retailer": {
            "role": "retailer",
            "count": "arg:num_retailers",
            "initial_inventory": {"1": 140.0},
            "lead_time": 1,
            "stockout_cost": 90.0,
            "policy": {"type": "BS", "base_stock_level": 160},
        },
    },
    "edges": [
        {"from_group": "Factory", "to_group": "DC"},
        {"from_group": "DC", "to_group": "Retailer"},
    ],
}

event_schema = {
    "DAILY_STATE": {
        "description": "End-of-day inventory snapshot.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "Ending inventory for product 1."},
            "backorder_qty": {"type": "float", "description": "Ending backorder quantity."},
        },
    }
}


def retailer_demand(period: int, product_id: int) -> float:
    """Retail demand follows stdin sequence; fallback demand is 16.0 units/day."""
    if product_id != 1:
        return 0.0
    payload = globals().get("STDIN_DATA", "")
    vals = [float(x) for x in payload.split()] if payload else []
    idx = period - 1
    return vals[idx] if idx < len(vals) else 16.0


custom_hooks = {
    "demand_func": {"Retailer": retailer_demand},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    inv = float(raw_state.get("inventory_level", {}).get("1", 0.0))
    bo = float(raw_state.get("backorder", 0.0))
    return [
        {
            "time": period,
            "node": semantic_node_id,
            "event": "DAILY_STATE",
            "inventory_qty": inv,
            "backorder_qty": bo,
        }
    ]


def extract_kpis(logs: list) -> dict:
    total_bo = 0.0
    avg_inv = 0.0
    count = 0
    for log in logs:
        if log.get("event") == "DAILY_STATE":
            total_bo += float(log.get("backorder_qty", 0.0))
            avg_inv += float(log.get("inventory_qty", 0.0))
            count += 1
    return {
        "total_backorder": total_bo,
        "avg_inventory": (avg_inv / count) if count else 0.0,
    }


test_cases = [
    {
        "case_name": "Baseline",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {"num_retailers": 3},
        "stdin_payload": "18 18 20 16 16 14 14 18",
    }
]
