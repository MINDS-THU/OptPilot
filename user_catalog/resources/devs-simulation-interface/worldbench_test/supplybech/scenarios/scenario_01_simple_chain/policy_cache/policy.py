
# Final Policy: Minimal Inventory Policy (Target=0)
# 
# This policy maintains minimal inventory at retailers by targeting a 
# base-stock level of 0. Since the DC has sufficient inventory (target 2000,
# initial 1500) and retailer lead time is only 1 day, retailers can rely
# on daily orders to meet demand.
#
# This minimizes retailer holding costs while avoiding stockouts due to
# the abundant DC supply buffer.

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    '''
    Minimal inventory policy (base-stock target = 0).
    
    Maintains just-in-time inventory by ordering only to bring the
    inventory position to 0, minimizing holding costs.
    
    Args:
        period: Current day (1-indexed) - not used for this simple policy
        inventory_dict: Current inventory position {product_id: inventory_position}
    
    Returns:
        Order dict for Central_DC_0
    '''
    product_id = 1
    
    # Target base-stock level (minimal inventory)
    target_level = 0
    
    # Get current inventory position
    inventory_position = inventory_dict.get(product_id, 0)
    
    # Calculate order quantity to bring inventory position to target
    # If inventory_position is positive, we don't need to order (target=0)
    # If inventory_position is negative (backorders), order to clear them
    order_qty = max(0, target_level - inventory_position)
    
    # Return order if positive
    if order_qty > 0:
        return {"Central_DC_0": {product_id: order_qty}}
    return {}


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
