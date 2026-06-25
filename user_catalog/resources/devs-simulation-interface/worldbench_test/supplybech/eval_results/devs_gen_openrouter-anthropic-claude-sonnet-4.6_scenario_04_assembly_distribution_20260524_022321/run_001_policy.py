#!/usr/bin/env python3
"""
Supply Chain Replenishment Policy - Optimized Order-Up-To Strategy
===================================================================

This file implements an optimized order-up-to (base-stock) replenishment policy
for a 4-level assembly-distribution supply chain:
  Suppliers (A, B, C) -> Assembler_0 -> DC_0 -> Retailers (0, 1, 2)

Products:
  - product_id=1: Raw_Material_A (for Product_X)
  - product_id=2: Raw_Material_B (for Product_Y)
  - product_id=3: Raw_Material_C (shared, for both X and Y)
  - product_id=4: Finished_Product_X (high demand)
  - product_id=5: Finished_Product_Y (low demand)

BOM:
  Product_X (4): 1×Raw_A + 1×Raw_C
  Product_Y (5): 2×Raw_B + 1×Raw_C

Policy Parameters (optimized via simulation):
  Retailer: X=30, Y=20 (LT=1 day)
  DC: X=132, Y=86 (LT=2 days, serves 3 retailers)
  Assembler raw materials (LT=2 days from suppliers):
    Raw_A: target=130 (1 per X, X avg demand = 45/day from 3 retailers)
    Raw_B: target=170 (2 per Y, Y avg demand = 30/day => needs 60/day)
    Raw_C: target=200 (shared: 1 per X + 1 per Y = 75/day)

Optimized for: supply_chain_model2 simulator + task specification compatibility
"""

# =============================================================================
# Policy Parameters (tuned to minimize total cost)
# =============================================================================

# Retailer: LT=1 day
# Daily demand: X avg=15 (max~26), Y avg=10 (max~18)  
# Target = 2 * avg + small safety (keeps holding cost low)
RETAILER_TARGET_X = 30.0
RETAILER_TARGET_Y = 20.0

# DC: LT=2 days, serves 3 retailers
# Aggregate demand: X avg=45/day, Y avg=30/day
# Target covers ~3 days of average demand
DC_TARGET_X = 132.0
DC_TARGET_Y = 86.0

# Assembler: LT=2 days from suppliers  
# Must supply DC orders with BOM-aware raw material calculation
ASSEMBLER_TARGET_X = 130.0  # Finished Product_X
ASSEMBLER_TARGET_Y = 87.0   # Finished Product_Y

# Raw material targets for Assembler (BOM-aware)
# Raw_A: 1 per X => need ~45/day; with LT=2 => target = 2*45 + safety = 130
# Raw_B: 2 per Y => need ~60/day; with LT=2 => target = 2*60 + safety = 170
# Raw_C: 1 per X + 1 per Y => need ~75/day; with LT=2 => target = 2*75 + safety = 200
RAW_A_TARGET = 130.0   # 1×Raw_A per Product_X
RAW_B_TARGET = 170.0   # 2×Raw_B per Product_Y
RAW_C_TARGET = 200.0   # 1×Raw_C per Product_X + 1×Raw_C per Product_Y (shared)


# =============================================================================
# Task-Specification Compatible Function Signatures
# =============================================================================

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer replenishment policy (order-up-to).
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: inv_pos_X, 5: inv_pos_Y}
            inventory_position = on_hand + in_transit
    
    Returns:
        {"DC_0": {4: order_qty_X, 5: order_qty_Y}} or {}
    """
    pos_x = inventory_dict.get(4, 0.0)
    pos_y = inventory_dict.get(5, 0.0)
    
    order_x = max(0.0, RETAILER_TARGET_X - pos_x)
    order_y = max(0.0, RETAILER_TARGET_Y - pos_y)
    
    if order_x > 0 or order_y > 0:
        return {"DC_0": {4: order_x, 5: order_y}}
    return {}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC replenishment policy (order-up-to).
    
    Args:
        period: Current day (1-100)
        inventory_dict: {4: inv_pos_X, 5: inv_pos_Y}
    
    Returns:
        {"Assembler_0": {4: order_qty_X, 5: order_qty_Y}} or {}
    """
    pos_x = inventory_dict.get(4, 0.0)
    pos_y = inventory_dict.get(5, 0.0)
    
    order_x = max(0.0, DC_TARGET_X - pos_x)
    order_y = max(0.0, DC_TARGET_Y - pos_y)
    
    if order_x > 0 or order_y > 0:
        return {"Assembler_0": {4: order_x, 5: order_y}}
    return {}


def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Assembler raw material replenishment policy (order-up-to with BOM awareness).
    
    Args:
        period: Current day (1-100)
        inventory_dict: {1: pos_A, 2: pos_B, 3: pos_C, 4: pos_X, 5: pos_Y}
            All values are inventory positions (on_hand + in_transit)
    
    Returns:
        {
            "Supplier_A_0": {1: qty},
            "Supplier_B_0": {2: qty},
            "Supplier_C_0": {3: qty}
        } or {}
    
    Notes:
        - BOM: Product_X needs 1×Raw_A + 1×Raw_C
        - BOM: Product_Y needs 2×Raw_B + 1×Raw_C
        - Raw_C is shared between X and Y production
    """
    pos_a = inventory_dict.get(1, 0.0)
    pos_b = inventory_dict.get(2, 0.0)
    pos_c = inventory_dict.get(3, 0.0)
    
    order_a = max(0.0, RAW_A_TARGET - pos_a)
    order_b = max(0.0, RAW_B_TARGET - pos_b)
    order_c = max(0.0, RAW_C_TARGET - pos_c)
    
    result = {}
    if order_a > 0:
        result["Supplier_A_0"] = {1: order_a}
    if order_b > 0:
        result["Supplier_B_0"] = {2: order_b}
    if order_c > 0:
        result["Supplier_C_0"] = {3: order_c}
    
    return result


# =============================================================================
# Simulator-Compatible Policy Functions (supply_chain_model2 format)
# =============================================================================
# These are adapter functions that wrap the main policy functions above.
# They accept the context dict format used by supply_chain_model2 simulator.

def _retailer_policy_adapter(context: dict) -> dict:
    """Adapter for supply_chain_model2 retailer policy call format."""
    inv_pos = context.get("inventory_position", {})
    pos_x = inv_pos.get(4, 0.0)
    pos_y = inv_pos.get(5, 0.0)
    
    order_x = max(0.0, RETAILER_TARGET_X - pos_x)
    order_y = max(0.0, RETAILER_TARGET_Y - pos_y)
    
    return {4: order_x, 5: order_y}


def _dc_policy_adapter(context: dict) -> dict:
    """Adapter for supply_chain_model2 DC policy call format."""
    inv_pos = context.get("inventory_position", {})
    pos_x = inv_pos.get(4, 0.0)
    pos_y = inv_pos.get(5, 0.0)
    
    order_x = max(0.0, DC_TARGET_X - pos_x)
    order_y = max(0.0, DC_TARGET_Y - pos_y)
    
    return {4: order_x, 5: order_y}


def _assembler_policy_adapter(context: dict) -> dict:
    """Adapter for supply_chain_model2 assembler policy call format."""
    inv_pos = context.get("inventory_position", {})
    pos_x = inv_pos.get(4, 0.0)
    pos_y = inv_pos.get(5, 0.0)
    
    order_x = max(0.0, ASSEMBLER_TARGET_X - pos_x)
    order_y = max(0.0, ASSEMBLER_TARGET_Y - pos_y)
    
    return {4: order_x, 5: order_y}


# =============================================================================
# POLICY_MOUNTS - Required by both task spec and simulator
# =============================================================================

POLICY_MOUNTS = {
    # Task-specification required keys (capital letters)
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
    
    # supply_chain_model2 simulator keys (lowercase)
    "retailer_0": _retailer_policy_adapter,
    "retailer_1": _retailer_policy_adapter,
    "retailer_2": _retailer_policy_adapter,
    "dc": _dc_policy_adapter,
    "assembler": _assembler_policy_adapter,
}
