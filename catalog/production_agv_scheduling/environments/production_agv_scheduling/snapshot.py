from typing import Dict, List, Any
from factory_sim.run_multi_line_simulation import MultiLineFactorySimulation
from factory_sim.simulation.entities.product import Product

def create_snapshot(simulation: MultiLineFactorySimulation) -> Dict[str, Any]:
    factory = simulation.factory
    command_handler = simulation.command_handler
    
    snapshot = {
        "time": factory.env.now,
        "lines": {},
        "warehouses": {},
        "products": {},
        "commands": {}
    }
    
    # Capture command statuses
    if hasattr(command_handler, 'commands'):
        snapshot["commands"] = command_handler.commands.copy()

    # Warehouses
    if hasattr(factory, 'warehouse') and factory.warehouse:
        snapshot["warehouses"]["Warehouse"] = {
            "id": factory.warehouse.id,
            "position": factory.warehouse.position,
            "interacting_point": factory.warehouse.interacting_points[0],
            "buffer": [p.id for p in factory.warehouse.buffer.items]
        }
    if hasattr(factory, 'raw_material') and factory.raw_material:
        snapshot["warehouses"]["RawMaterial"] = {
            "id": factory.raw_material.id,
            "position": factory.raw_material.position,
            "interacting_point": factory.raw_material.interacting_points[0],
            "buffer": [p.id for p in factory.raw_material.buffer.items]
        }

    # Helper to register products
    def register_products(container, location_id, buffer_type=None):
        for p in container:
            priority = "low"
            if hasattr(factory, 'kpi_calculator') and factory.kpi_calculator:
                if p.order_id in factory.kpi_calculator.active_orders:
                    priority = factory.kpi_calculator.active_orders[p.order_id].priority
            
            # Extract last update time from history as a proxy for "arrival at current state"
            last_update_time = 0.0
            if p.history:
                last_update_time = p.history[-1][0]

            snapshot["products"][p.id] = {
                "id": p.id,
                "type": p.product_type,
                "location": location_id,
                "buffer_type": buffer_type,
                "quality_status": p.quality_status.value if hasattr(p.quality_status, 'value') else str(p.quality_status),
                "order_id": p.order_id,
                "priority": priority,
                "process_step": p.process_step,
                "last_update_time": last_update_time
            }

    if hasattr(factory, 'warehouse') and factory.warehouse: 
        register_products(factory.warehouse.buffer.items, factory.warehouse.id)
    if hasattr(factory, 'raw_material') and factory.raw_material: 
        register_products(factory.raw_material.buffer.items, factory.raw_material.id)

    # Lines
    for line_name, line in factory.lines.items():
        line_data = {
            "stations": {},
            "agvs": {},
            "conveyors": {}
        }
        
        # Stations
        for station in line.stations.values():
            line_data["stations"][station.id] = {
                "id": station.id,
                "status": station.status.value,
                "buffer": [p.id for p in station.buffer.items],
                "buffer_size": station.buffer_size,
                "position": station.position,
                "interacting_point": station.interacting_points[0] if station.id.startswith("Station") else station.interacting_points[-1],
                # Add processing product info
                "processing_product_id": station.current_product_id
            }
            register_products(station.buffer.items, station.id)
            
            # If station is processing a product, we should register it too if we can access the object.
            # But we can't easily access the object if it's not in a container.
            # However, we can infer its existence.
            # For now, we just record the ID in station data.
            
            # QualityCheck might have output_buffer
            if hasattr(station, 'output_buffer'):
                line_data["stations"][station.id]["output_buffer"] = [p.id for p in station.output_buffer.items]
                register_products(station.output_buffer.items, station.id, "output_buffer")

        # AGVs
        for agv in line.agvs.values():
            line_data["agvs"][agv.id] = {
                "id": agv.id,
                "status": agv.status.value,
                "current_point": agv.current_point,
                "target_point": agv.target_point,
                "battery_level": agv.battery_level,
                "payload": [p.id for p in agv.payload.items],
                "payload_capacity": agv.payload_capacity
            }
            register_products(agv.payload.items, agv.id)
            
        # Conveyors (especially TripleBufferConveyor)
        for conveyor in line.conveyors.values():
            conveyor_data = {
                "id": conveyor.id,
                "status": conveyor.status.value,
                "interacting_point": conveyor.interacting_points[0]
            }
            
            if hasattr(conveyor, 'main_buffer'):
                conveyor_data["main_buffer"] = [p.id for p in conveyor.main_buffer.items]
                register_products(conveyor.main_buffer.items, conveyor.id, "main")
            elif hasattr(conveyor, 'buffer'):
                conveyor_data["buffer"] = [p.id for p in conveyor.buffer.items]
                register_products(conveyor.buffer.items, conveyor.id)
                
            if hasattr(conveyor, 'upper_buffer'):
                conveyor_data["upper_buffer"] = [p.id for p in conveyor.upper_buffer.items]
                register_products(conveyor.upper_buffer.items, conveyor.id, "upper")
                
            if hasattr(conveyor, 'lower_buffer'):
                conveyor_data["lower_buffer"] = [p.id for p in conveyor.lower_buffer.items]
                register_products(conveyor.lower_buffer.items, conveyor.id, "lower")
                
            line_data["conveyors"][conveyor.id] = conveyor_data
        
        snapshot["lines"][line_name] = line_data

    return snapshot
