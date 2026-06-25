"""Seed-controlled stochastic demand scenario for pipeline validation."""

import random


metadata = {
    "domain": "Stochastic_Seeded_Noise_Chain",
    "description": "Plant to hub to stores network with seed-controlled random demand shocks.",
    "products_mapping": {"1": "SKU_Noise"},
}


cli_args_schema = {
    "num_stores": {
        "type": "int",
        "default": 5,
        "description": "Number of store nodes in the last echelon.",
    },
    "base_demand": {
        "type": "float",
        "default": 20.0,
        "description": "Baseline daily demand per store for product 1.",
    },
    "demand_sigma": {
        "type": "float",
        "default": 4.0,
        "description": "Standard deviation of daily random demand shocks.",
    },
    "seed": {
        "type": "int",
        "default": 2026,
        "description": "Random seed controlling demand shock sampling.",
    },
    "simulate_time": {
        "type": "int",
        "default": 90,
        "description": "Simulation horizon in semantic days.",
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
            "initial_inventory": {"1": 700000.0},
            "policy": {"type": "BS", "base_stock_level": 700000},
        },
        "Hub": {
            "role": "distributor",
            "count": 1,
            "initial_inventory": {"1": 1300.0},
            "lead_time": 2,
            "holding_cost": 0.8,
            "stockout_cost": 40.0,
            "policy": {"type": "sS", "reorder_point": 380, "order_up_to_level": 1600},
        },
        "Store": {
            "role": "retailer",
            "count": "arg:num_stores",
            "initial_inventory": {"1": 120.0},
            "lead_time": 1,
            "holding_cost": 1.3,
            "stockout_cost": 110.0,
            "policy": {"type": "BS", "base_stock_level": 0},
        },
    },
    "edges": [
        {"from_group": "Plant", "to_group": "Hub"},
        {"from_group": "Hub", "to_group": "Store"},
    ],
}


event_schema = {
    "DAILY_COST_STATE": {
        "description": "End-of-day inventory and cost state for one semantic node.",
        "keys": {
            "inventory_qty": {"type": "float", "description": "Ending inventory quantity."},
            "holding_cost": {"type": "float", "description": "Incremental daily holding cost."},
            "stockout_cost": {"type": "float", "description": "Incremental daily stockout cost."},
        },
    }
}


def store_demand(period: int, product_id: int) -> float:
    """Product-1 demand is baseline plus seed-controlled Gaussian noise and weekend uplift."""
    if product_id != 1:
        return 0.0
    args = globals().get("DYNAMIC_ARGS", {})
    base = float(args.get("base_demand", 20.0))
    sigma = max(0.0, float(args.get("demand_sigma", 4.0)))
    seed = int(args.get("seed", 2026))

    weekend_uplift = 5.0 if period % 7 in (6, 0) else 0.0
    rng = random.Random(seed * 1000003 + period * 97 + product_id * 17)
    noise = rng.gauss(0.0, sigma)
    return max(0.0, base + weekend_uplift + noise)


def store_policy(period: int, inventory_dict: dict) -> dict:
    """Store orders from Hub_0 up to 140 units when inventory position is at most 45."""
    ip = float(inventory_dict.get(1, 0.0))
    if ip <= 45.0:
        return {"Hub_0": {1: max(0.0, 140.0 - ip)}}
    return {}


def store_holding_cost(inventory_dict: dict) -> float:
    """Store holding cost is 1.3 multiplied by positive product-1 inventory."""
    return 1.3 * max(0.0, float(inventory_dict.get(1, 0.0)))


def store_stockout_cost(shortage_dict: dict) -> float:
    """Store stockout cost is 110 multiplied by product-1 shortage quantity."""
    return 110.0 * max(0.0, float(shortage_dict.get(1, 0.0)))


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
            "event": "DAILY_COST_STATE",
            "inventory_qty": float(raw_state.get("inventory_level", {}).get("1", 0.0)),
            "holding_cost": float(raw_state.get("holding_cost_incurred", 0.0)),
            "stockout_cost": float(raw_state.get("stockout_cost_incurred", 0.0)),
        }
    ]


def extract_kpis(logs: list) -> dict:
    total_holding = 0.0
    for log in logs:
        if log.get("event") != "DAILY_COST_STATE":
            continue
        total_holding += float(log.get("holding_cost", 0.0))
    return {
        "total_holding_cost": total_holding,
    }


test_cases = [
    {
        "case_name": "Noise_Base",
        "runs": 6,
        "oracle_runs": 24,
        "seed_mode": "incremental",
        "seed_start": 2026,
        "seed_arg": "seed",
        "cli_kwargs": {
            "num_stores": 5,
            "base_demand": 20.0,
            "demand_sigma": 4.0,
            "seed": 2026,
            "simulate_time": 90,
        },
        "stdin_payload": "",
    }
]
