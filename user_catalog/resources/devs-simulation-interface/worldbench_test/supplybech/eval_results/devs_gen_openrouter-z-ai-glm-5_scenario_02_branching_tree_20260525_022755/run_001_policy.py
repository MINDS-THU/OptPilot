"""
Supply Chain Replenishment Policy - Version 8
Fine-tuned: Try retailer base-stock 125 with DC base-stock 850
"""

# Retailer base-stock: 125 (between V5's 120 which had shortages and V7's 130)
RETAILER_BASE_STOCK = 125

# DC base-stock: 850 (same as V7)
DC_BASE_STOCK = 850


def retailer_policy(period: int, inventory_position: float) -> float:
    """
    Retailer base-stock policy.
    """
    order_qty = max(0.0, RETAILER_BASE_STOCK - inventory_position)
    return order_qty


def dc_policy(period: int, inventory_dict: dict) -> dict:
    """
    DC base-stock policy.
    """
    if not inventory_dict:
        return {"Factory_0": {}}
    
    product_key = list(inventory_dict.keys())[0]
    current_ip = inventory_dict.get(product_key, 0)
    order_qty = max(0.0, DC_BASE_STOCK - current_ip)
    
    return {"Factory_0": {product_key: order_qty}}


def policy(period: int, inventory_data) -> any:
    """
    Unified policy function for both Retailer and DC.
    """
    if isinstance(inventory_data, dict):
        return dc_policy(period, inventory_data)
    else:
        return retailer_policy(period, inventory_data)


POLICY_MOUNTS = {
    "Retailer": lambda p, ip: retailer_policy(p, ip),
    "Regional_DC": lambda p, inv_dict: dc_policy(p, inv_dict),
}
