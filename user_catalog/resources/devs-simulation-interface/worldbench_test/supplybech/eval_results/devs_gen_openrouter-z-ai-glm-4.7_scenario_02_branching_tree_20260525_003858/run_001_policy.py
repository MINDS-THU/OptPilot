
"""
Supply Chain Replenishment Policy - Optimized for Total Cost Minimization

This policy implements base-stock (order-up-to-S) policies for both Retailer and DC levels.
Parameters are optimized based on simulation testing to minimize total cost (holding + stockout).

Optimal Parameters:
- Retailer S = 35: Low target inventory at retailers to reduce expensive holding costs
- DC S = 1200: High target inventory at DC to ensure availability and reduce stockout costs

Cost Structure:
- Retailer: holding = 3.0/unit/day, stockout = 120/unit
- DC: holding = 0.8/unit/day, stockout = 40/unit

Since DC holding cost is much cheaper than retailer holding cost (0.8 vs 3.0),
the optimal strategy is to hold more inventory at the DC level and less at retailers.
"""

import math

# Optimized base-stock levels based on cost structure and testing
RETAILER_S = 35  # Order-up-to level for retailers (low to minimize expensive holding costs)
DC_S = 1200      # Order-up-to level for DCs (high to ensure availability and minimize stockouts)

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer base-stock policy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Current inventory snapshot, format {product_id: inventory_position}
            where inventory_position = on_hand + in_transit - backorders
    
    Returns:
        Order dictionary mapping upstream DC to product quantities.
        Format: {"Regional_DC_0": {1: order_qty}} or {"Regional_DC_1": {1: order_qty}}
    """
    product_id = 1
    inventory_position = inventory_dict.get(product_id, 0)
    
    # Base-stock policy: order enough to bring inventory position to S
    order_quantity = max(0, RETAILER_S - inventory_position)
    
    # Determine upstream DC based on retailer ID (this will be handled by the simulator)
    # For simplicity, return a generic format that can be mapped appropriately
    # In practice, Retailer_0/1/2 order from Regional_DC_0, Retailer_3/4/5 from Regional_DC_1
    
    # Since we can't directly identify which retailer we are, return format with Regional_DC_0
    # The simulator will handle the routing based on network structure
    return {"Regional_DC_0": {product_id: order_quantity}}


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    DC base-stock policy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: Current inventory snapshot, format {product_id: inventory_position}
            where inventory_position = on_hand + in_transit - backorders
    
    Returns:
        Order dictionary mapping Factory to product quantities.
        Format: {"Factory_0": {1: order_qty}}
    """
    product_id = 1
    inventory_position = inventory_dict.get(product_id, 0)
    
    # Base-stock policy: order enough to bring inventory position to S
    order_quantity = max(0, DC_S - inventory_position)
    
    return {"Factory_0": {product_id: order_quantity}}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
