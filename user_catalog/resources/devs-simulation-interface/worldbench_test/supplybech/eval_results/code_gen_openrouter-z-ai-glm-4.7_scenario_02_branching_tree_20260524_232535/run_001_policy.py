import math

# Policy parameters - tuned for optimal balance
RETAILER_ORDER_UP_TO = 78  # Cover ~3 days demand with safety stock
DC_ORDER_UP_TO = 370  # Cover ~5 days demand for 3 retailers with safety stock

# Global counters for routing determination
# These track how many times each policy function has been called
_retailer_call_count = 0
_dc_call_count = 0

# Known node mappings
RETAILER_TO_DC = {
    "Retailer_0": "Regional_DC_0",
    "Retailer_1": "Regional_DC_0", 
    "Retailer_2": "Regional_DC_0",
    "Retailer_3": "Regional_DC_1",
    "Retailer_4": "Regional_DC_1",
    "Retailer_5": "Regional_DC_1"
}
ALL_RETAILERS = ["Retailer_0", "Retailer_1", "Retailer_2", "Retailer_3", "Retailer_4", "Retailer_5"]
ALL_DCS = ["Regional_DC_0", "Regional_DC_1"]

def _get_retailer_index():
    """
    Determine which retailer index is currently being processed.
    Returns the retailer name based on call count modulo 6.
    """
    global _retailer_call_count
    idx = _retailer_call_count % len(ALL_RETAILERS)
    return ALL_RETAILERS[idx]

def _get_dc_index():
    """
    Determine which DC index is currently being processed.
    Returns the DC name based on call count modulo 2.
    """
    global _dc_call_count
    idx = _dc_call_count % len(ALL_DCS)
    return ALL_DCS[idx]

def retailer_policy_func(period, inventory_dict):
    """
    Retailer policy: Order-up-to level with seasonal adjustment
    
    Args:
        period: Current period (1-100)
        inventory_dict: {product_id: inventory_position}
    
    Returns:
        Orders to upstream DC: {"Regional_DC_X": {product_id: order_quantity}}
    """
    global _retailer_call_count
    
    # Determine current retailer name using call counter
    retailer_name = _get_retailer_index()
    
    # Determine upstream DC based on retailer name
    dc_name = RETAILER_TO_DC.get(retailer_name, "Regional_DC_0")
    
    # Seasonal adjustment based on 14-day cycle
    seasonal_factor = 5 * math.sin(2 * math.pi * period / 14)
    target_level = RETAILER_ORDER_UP_TO + seasonal_factor
    
    orders = {}
    for product_id, inventory_position in inventory_dict.items():
        order_qty = max(0, target_level - inventory_position)
        
        if order_qty > 0.1:  # Threshold to avoid micro-orders
            orders[dc_name] = {product_id: round(order_qty)}
    
    # Increment call counter for next call
    _retailer_call_count += 1
    
    return orders


def dc_policy_func(period, inventory_dict):
    """
    DC policy: Order-up-to level with seasonal adjustment
    
    Args:
        period: Current period (1-100)
        inventory_dict: {product_id: inventory_position}
    
    Returns:
        Orders to Factory: {"Factory_0": {product_id: order_quantity}}
    """
    global _dc_call_count
    
    # Seasonal adjustment - smoothed for DC (aggregates 3 retailers' demand)
    seasonal_factor = 8 * math.sin(2 * math.pi * period / 14)
    target_level = DC_ORDER_UP_TO + seasonal_factor
    
    orders = {}
    for product_id, inventory_position in inventory_dict.items():
        order_qty = max(0, target_level - inventory_position)
        
        if order_qty > 0.1:  # Threshold to avoid micro-orders
            orders["Factory_0"] = {product_id: round(order_qty)}
    
    # Increment call counter for next call
    _dc_call_count += 1
    
    return orders


POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
