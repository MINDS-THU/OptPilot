
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Retailer replenishment policy using order-up-to strategy.
    
    Args:
        period: Current day (1-100)
        inventory_dict: {product_id: inventory_position} where inventory_position 
                       = physical inventory + in_transit - backorders
    
    Returns:
        Order dict {"Central_DC_0": {product_id: order_quantity}} or {} if no order
    """
    # Order-up-to level for retailers
    # Based on demand pattern [30, 30, 30, 10, 10, 10] with lead_time=1 day
    # Max demand = 30, lead time = 1, so target = 50 to cover peak with safety buffer
    # This balances high shortage cost (100) vs holding cost (2.5)
    ORDER_UP_TO_LEVEL = 50.0
    
    inventory_position = inventory_dict.get(1, 0.0)
    
    # Calculate order quantity needed to reach target level
    order_quantity = ORDER_UP_TO_LEVEL - inventory_position
    
    # Only order if we need to replenish (quantity > 0)
    if order_quantity > 0:
        return {"Central_DC_0": {1: order_quantity}}
    else:
        return {}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
