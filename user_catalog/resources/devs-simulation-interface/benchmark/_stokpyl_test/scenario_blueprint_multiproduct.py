"""Two-product blueprint to test multiproduct behavior."""

metadata = {
    "domain": "Multiproduct_Outlet",
    "description": "Supplier to outlets with two products and custom multiproduct demand/cost hooks.",
    "products_mapping": {"1": "Widget_A", "2": "Widget_B"},
}

cli_args_schema = {
    "num_outlets": {
        "type": "int",
        "default": 3,
        "description": "Number of outlet nodes.",
    },
    "demand_scale": {
        "type": "float",
        "default": 1.0,
        "description": "Multiplier for both product demands.",
    },
}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin is used.",
}

topology = {
    "node_groups": {
        "MegaSupplier": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 900000.0, "2": 900000.0},
            "policy": {"type": "BS", "base_stock_level": 900000},
        },
        "Outlet": {
            "role": "retailer",
            "count": "arg:num_outlets",
            "initial_inventory": {"1": 90.0, "2": 65.0},
            "lead_time": 1,
            "stockout_cost": 70.0,
            "policy": {"type": "BS", "base_stock_level": 130},
        },
    },
    "edges": [
        {"from_group": "MegaSupplier", "to_group": "Outlet"},
    ],
}

event_schema = {
    "PRODUCT_STATE": {
        "description": "Per-product inventory snapshot.",
        "keys": {
            "product_id": {"type": "int", "description": "Product index."},
            "inventory_qty": {"type": "float", "description": "Product ending inventory."},
        },
    },
    "PRODUCT_SHORTAGE": {
        "description": "Per-product shortage event.",
        "keys": {
            "product_id": {"type": "int", "description": "Product index."},
            "missed_qty": {"type": "float", "description": "Unfulfilled quantity."},
        },
    },
}


def outlet_demand_func(period: int, product_id: int) -> float:
    args = globals().get("DYNAMIC_ARGS", {})
    scale = float(args.get("demand_scale", 1.0))

    if product_id == 1:
        base = 14.0
        pulse = 4.0 if period % 5 == 0 else 0.0
    elif product_id == 2:
        base = 9.0
        pulse = 2.0 if period % 4 == 0 else 0.0
    else:
        return 0.0

    return max(0.0, scale * (base + pulse))


def outlet_holding_cost_func(inventory_dict: dict) -> float:
    inv_1 = max(0.0, float(inventory_dict.get(1, 0.0)))
    inv_2 = max(0.0, float(inventory_dict.get(2, 0.0)))
    return 0.9 * inv_1 + 1.3 * inv_2


def outlet_stockout_cost_func(shortage_dict: dict) -> float:
    short_1 = max(0.0, float(shortage_dict.get(1, 0.0)))
    short_2 = max(0.0, float(shortage_dict.get(2, 0.0)))
    return 80.0 * short_1 + 120.0 * short_2


custom_hooks = {
    "demand_func": {"Outlet": outlet_demand_func},
    "holding_cost_func": {"Outlet": outlet_holding_cost_func},
    "stockout_cost_func": {"Outlet": outlet_stockout_cost_func},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    logs = []
    inventory_map = raw_state.get("inventory_level", {})

    for pid in (1, 2):
        inv = float(inventory_map.get(str(pid), 0.0))
        logs.append(
            {
                "time": period,
                "node": semantic_node_id,
                "event": "PRODUCT_STATE",
                "product_id": pid,
                "inventory_qty": inv,
            }
        )

        shortage = max(0.0, -inv)
        if shortage > 0:
            logs.append(
                {
                    "time": period,
                    "node": semantic_node_id,
                    "event": "PRODUCT_SHORTAGE",
                    "product_id": pid,
                    "missed_qty": shortage,
                }
            )

    return logs


def check_product_ids(logs: list):
    for log in logs:
        if log.get("event") in ("PRODUCT_STATE", "PRODUCT_SHORTAGE"):
            assert int(log["product_id"]) in (1, 2)


tier2_checkers = [check_product_ids]
tier3_checkers = []


def extract_kpis(logs: list) -> dict:
    shortage_1 = 0.0
    shortage_2 = 0.0
    avg_abs_inventory = 0.0
    inv_count = 0

    for log in logs:
        event = log.get("event")
        if event == "PRODUCT_SHORTAGE":
            if int(log["product_id"]) == 1:
                shortage_1 += float(log.get("missed_qty", 0.0))
            else:
                shortage_2 += float(log.get("missed_qty", 0.0))
        elif event == "PRODUCT_STATE":
            avg_abs_inventory += abs(float(log.get("inventory_qty", 0.0)))
            inv_count += 1

    return {
        "shortage_product_1": float(shortage_1),
        "shortage_product_2": float(shortage_2),
        "avg_abs_inventory": float(avg_abs_inventory / inv_count) if inv_count > 0 else 0.0,
    }


test_cases = [
    {
        "case_name": "Multiproduct_Base",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {"num_outlets": 3, "demand_scale": 1.0},
        "stdin_payload": "",
    },
    {
        "case_name": "Multiproduct_HeavyDemand",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {"num_outlets": 6, "demand_scale": 1.7},
        "stdin_payload": "",
    },
]
