
import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock (order-up-to) policy for retailers.
    
    Optimized parameters:
    - Order-up-to level (S): 68 units
    - This balances holding cost (3.0/unit/day) against stockout cost (120.0/unit)
    - Covers approximately 2.7 days of demand on average (lead time = 2 days)
    
    Args:
        period: Current day (1-indexed)
        inventory_dict: Current inventory position {product_id: position}
    
    Returns:
        Order dictionary specifying quantities to order from upstream DCs
    """
    product_id = 1
    current_position = inventory_dict.get(product_id, 0.0)
    
    # Order-up-to level (S)
    S = 68.0
    
    # Order enough to reach target inventory position
    order_qty = max(0, S - current_position)
    
    # Return orders to both DCs - simulation framework routes to correct upstream
    return {
        "Regional_DC_0": {product_id: order_qty},
        "Regional_DC_1": {product_id: order_qty}
    }


def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Base-stock (order-up-to) policy for Regional DCs.
    
    Optimized parameters:
    - Order-up-to level (S): 350 units
    - Each DC serves 3 retailers with aggregate demand ~75 units/day
    - Lead time from factory: 4 days
    - S = 350 covers approximately 4.7 days of aggregate demand
    - Balances low holding cost (0.8/unit/day) against moderate stockout cost (40.0/unit)
    
    Args:
        period: Current day (1-indexed)
        inventory_dict: Current inventory position {product_id: position}
    
    Returns:
        Order dictionary specifying quantities to order from Factory
    """
    product_id = 1
    current_position = inventory_dict.get(product_id, 0.0)
    
    # Order-up-to level (S)
    S = 350.0
    
    # Order enough to reach target inventory position
    order_qty = max(0, S - current_position)
    
    return {
        "Factory_0": {product_id: order_qty}
    }


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
