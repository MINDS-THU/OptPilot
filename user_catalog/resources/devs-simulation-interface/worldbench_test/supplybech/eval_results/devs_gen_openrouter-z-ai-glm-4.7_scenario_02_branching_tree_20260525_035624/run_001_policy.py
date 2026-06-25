
"""
Test Policy Version 2 - Optimized Order-Up-To Policy
Based on analysis of v1 results:
- Retailer demand ranges from ~3 to ~17 units/day
- Critical ratio = 120/(120+3) = 0.975 (very high service level needed)
- Need much higher inventory targets to avoid shortages
"""

def retailer_policy_func(on_hand, backorders):
    """
    Optimized order-up-to policy for retailers.
    
    Analysis:
    - Max demand observed: ~17 units/day
    - With critical ratio 0.975, need ~2.17 standard deviations of safety stock
    - Target should cover multiple days of worst-case demand
    
    Optimized target: 60 units
    - Covers ~3.5 days of maximum demand
    - Should prevent most shortages
    """
    target_level = 60
    
    # Current inventory position = on_hand - backorders
    inventory_position = on_hand - backorders
    
    # Order quantity = max(0, target - inventory_position)
    order_qty = max(0, target_level - inventory_position)
    
    return int(order_qty)


def dc_policy_func(dc_model):
    """
    Optimized order-up-to policy for Regional DC.
    
    Analysis:
    - Serves 2 retailers, each with demand up to ~17 units/day
    - Total maximum demand: ~34 units/day
    - With DC shortage cost 40 and holding cost 0.8
    - Critical ratio = 40/(40+0.8) = 0.98
    - Need even higher service level at DC
    
    Optimized target: 150 units
    - Covers ~4.4 days of maximum total demand
    - Accounts for variability and coordination needs
    """
    # Access DC state through the model instance
    on_hand = dc_model.state["on_hand"]
    in_transit = dc_model.state["in_transit"]
    total_backorders = sum(b["qty"] for b in dc_model.state["backorders_queue"])
    
    # Inventory position = on_hand + in_transit - backorders
    inventory_position = on_hand + in_transit - total_backorders
    
    # Optimized target inventory level for DC
    target_level = 150
    
    # Order quantity = max(0, target - inventory_position)
    order_qty = max(0, target_level - inventory_position)
    
    return int(order_qty)


# Policy mount configuration
POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
