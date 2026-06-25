"""Static tree blueprint with default stockpyl policies only."""

metadata = {
    "domain": "Static_Tree_DefaultPolicy",
    "description": "A static 3-layer distribution tree with no custom hooks.",
    "products_mapping": {"1": "Standard_Item"},
}

cli_args_schema = {}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin is used.",
}

topology = {
    "node_groups": {
        "Plant": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 50000.0},
            "policy": {"type": "BS", "base_stock_level": 50000},
        },
        "Regional_DC": {
            "role": "distributor",
            "count": 2,
            "initial_inventory": {"1": 180.0},
            "lead_time": 2,
            "holding_cost": 0.6,
            "stockout_cost": 20.0,
            "policy": {"type": "sS", "reorder_point": 70, "order_up_to_level": 260},
        },
        "Store": {
            "role": "retailer",
            "count": 3,
            "initial_inventory": {"1": 65.0},
            "lead_time": 1,
            "holding_cost": 1.2,
            "stockout_cost": 35.0,
            "policy": {"type": "BS", "base_stock_level": 95},
        },
    },
    "edges": [
        {"from_group": "Plant", "to_group": "Regional_DC"},
        {"from_group": "Regional_DC", "to_group": "Store"},
    ],
}

event_schema = {
    "PERIOD_STATE": {
        "description": "Period end inventory state.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "Product 1 ending inventory."},
            "backorder_qty": {"type": "float", "description": "Product 1 ending backlog."},
        },
    }
}

custom_hooks = {}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    inventory_qty = float(raw_state.get("inventory_level", {}).get("1", 0.0))
    backorder_qty = max(0.0, -inventory_qty)
    return [
        {
            "time": period,
            "node": semantic_node_id,
            "event": "PERIOD_STATE",
            "inventory_qty": inventory_qty,
            "backorder_qty": backorder_qty,
        }
    ]


def check_state_values_are_finite(logs: list):
    for log in logs:
        if log.get("event") == "PERIOD_STATE":
            assert abs(float(log["inventory_qty"])) < 1e12
            assert abs(float(log["backorder_qty"])) < 1e12


tier2_checkers = [check_state_values_are_finite]
tier3_checkers = []


def extract_kpis(logs: list) -> dict:
    total_backorder = 0.0
    avg_abs_inventory = 0.0
    count = 0
    for log in logs:
        if log.get("event") != "PERIOD_STATE":
            continue
        total_backorder += float(log.get("backorder_qty", 0.0))
        avg_abs_inventory += abs(float(log.get("inventory_qty", 0.0)))
        count += 1

    return {
        "total_backorder_qty": float(total_backorder),
        "avg_abs_inventory": float(avg_abs_inventory / count) if count > 0 else 0.0,
    }


test_cases = [
    {
        "case_name": "Static_Default",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {},
        "stdin_payload": "",
    }
]
