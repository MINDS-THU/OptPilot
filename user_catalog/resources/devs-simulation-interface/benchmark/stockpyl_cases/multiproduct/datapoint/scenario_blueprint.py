"""Multiproduct blueprint example for description generation tests."""

metadata = {
    "domain": "Multiproduct_Demo",
    "description": "Supplier to outlets with two products and product-specific demand hooks.",
    "products_mapping": {"1": "Product_A", "2": "Product_B"},
}

cli_args_schema = {
    "num_outlets": {
        "type": "int",
        "default": 4,
        "description": "Number of outlet nodes.",
    },
    "simulate_time": {
        "type": "int",
        "default": 100,
        "description": "Simulation horizon in semantic days.",
    }
}

stdin_schema = {
    "is_used": False,
    "format_description": "No stdin data is required.",
}

topology = {
    "node_groups": {
        "Supplier": {
            "role": "source",
            "count": 1,
            "initial_inventory": {"1": 700000.0, "2": 600000.0},
            "policy": {"type": "BS", "base_stock_level": 700000},
        },
        "Outlet": {
            "role": "retailer",
            "count": "arg:num_outlets",
            "initial_inventory": {"1": 110.0, "2": 90.0},
            "lead_time": 1,
            "stockout_cost": 85.0,
            "policy": {"type": "BS", "base_stock_level": 150},
        },
    },
    "edges": [
        {"from_group": "Supplier", "to_group": "Outlet"},
    ],
}

event_schema = {
    "PRODUCT_DAILY": {
        "description": "Per-product daily state at each node.",
        "keys": {
            "product_id": {"type": "int", "description": "Product index."},
            "inventory_qty": {"type": "float", "description": "Ending inventory for the product."},
        },
    }
}


def outlet_demand(period: int, product_id: int) -> float:
    """Product A and B have different baseline and periodic pulse demand."""
    if product_id == 1:
        return 12.0 + (3.0 if period % 5 == 0 else 0.0)
    if product_id == 2:
        return 8.0 + (2.0 if period % 4 == 0 else 0.0)
    return 0.0


custom_hooks = {
    "demand_func": {"Outlet": outlet_demand},
}


def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list:
    out = []
    for pid in (1, 2):
        out.append(
            {
                "time": period,
                "node": semantic_node_id,
                "event": "PRODUCT_DAILY",
                "product_id": pid,
                "inventory_qty": float(raw_state.get("inventory_level", {}).get(str(pid), 0.0)),
            }
        )
    return out


def extract_kpis(logs: list) -> dict:
    total_abs_inventory = 0.0
    n = 0
    for log in logs:
        if log.get("event") == "PRODUCT_DAILY":
            total_abs_inventory += abs(float(log.get("inventory_qty", 0.0)))
            n += 1
    return {
        "avg_abs_inventory": (total_abs_inventory / n) if n else 0.0,
    }


test_cases = [
    {
        "case_name": "Multi_Base",
        "runs": 8,
        "oracle_runs": 20,
        "cli_kwargs": {"num_outlets": 4, "simulate_time": 100},
        "stdin_payload": "",
    }
]
