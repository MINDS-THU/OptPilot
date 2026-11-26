# src/game_logic/kpi_calculator.py
import simpy
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from dataclasses import dataclass, field

from factory_sim.config.schemas import KPIUpdate, NewOrder
from factory_sim.config.topics import KPI_UPDATE_TOPIC
from factory_sim.utils.mqtt_client import MQTTClient
from factory_sim.utils.topic_manager import TopicManager
from factory_sim.utils.sqlite_db import SimulationDatabase

if TYPE_CHECKING:
    from factory_sim.simulation.entities.product import Product

@dataclass
class ProductTracking:
    """Track individual product for production cycle calculation."""
    product_id: str
    product_type: str  # P1, P2, P3
    order_id: str
    start_time: float  # When order is created
    theoretical_time: float  # Based on product type
    production_start_time: Optional[float] = None  # When it actually enters production (StationA)
    end_time: Optional[float] = None  # When it exits the production line

@dataclass
class OrderTracking:
    """Track individual order progress for KPI calculation."""
    order_id: str
    created_at: float
    deadline: float
    items_total: int
    priority: str = "low"  # Added priority field
    items_completed: int = 0
    completed_at: Optional[float] = None
    is_on_time: Optional[bool] = None
    total_cost: float = 0.0
    products: List[ProductTracking] = field(default_factory=list)

@dataclass
class ProductionStats:
    """Production statistics for KPI calculation."""
    total_orders: int = 0
    completed_orders: int = 0
    on_time_orders: int = 0
    total_products: int = 0
    quality_passed_products: int = 0
    scrapped_products: int = 0
    
    # Time tracking
    total_production_time: float = 0.0
    total_simulation_time: float = 0.0
    weighted_production_cycle_sum: float = 0.0  # Sum of (actual/theoretical) ratios
    
    # Cost tracking
    material_costs: float = 0.0
    energy_costs: float = 0.0
    maintenance_costs: float = 0.0
    scrap_costs: float = 0.0
    
    # Fault tracking
    total_faults: int = 0
    correct_diagnoses: int = 0
    total_recovery_time: float = 0.0
    
    # Device utilization (device_id -> working_time)
    device_working_time: Dict[str, float] = field(default_factory=dict)
    device_total_time: Dict[str, float] = field(default_factory=dict)
    
    # AGV metrics
    agv_active_charges: int = 0  # 主动充电次数
    agv_passive_charges: int = 0  # 被动充电次数
    agv_total_charge_time: float = 0.0  # 总充电时间
    agv_completed_tasks: int = 0  # AGV完成的任务数
    agv_transport_time: Dict[str, float] = field(default_factory=dict)  # AGV运输时间
    agv_fault_time: Dict[str, float] = field(default_factory=dict)  # AGV故障时间
    agv_charge_time: Dict[str, float] = field(default_factory=dict)  # AGV充电时间

class KPICalculator:
    """
    Calculates KPIs according to PRD 3.4 Section 2.8 specifications.
    
    KPI categories defined in PRD:
    - Production Efficiency: Order completion rate, production cycle, device utilization
    - Quality & Cost: First pass rate, total production cost
    - AGV Efficiency: Charge strategy, energy efficiency, utilization
    
    Note: Competition scoring weights are not specified in PRD, so we use:
    - Production Efficiency: 40%
    - Quality & Cost: 30%
    - AGV Efficiency: 30%
    """
    
    def __init__(self, env: simpy.Environment, mqtt_client: Optional[MQTTClient] = None, database: Optional[SimulationDatabase] = None, topic_manager: Optional[TopicManager] = None, config: Optional[Dict[str, Any]] = None):
        self.env = env
        self.mqtt_client = mqtt_client
        self.database = database
        self.topic_manager = topic_manager
        self.stats = ProductionStats()
        self.active_orders: Dict[str, OrderTracking] = {}
        self.completed_orders: List[OrderTracking] = []
        
        # Load configuration from YAML if not provided
        if config is None:
            from factory_sim.utils.config_loader import load_factory_config
            config = load_factory_config()
        
        # Load KPI weights from config
        kpi_weights = config.get('kpi_weights', {})
        self.weights = {
            'production_efficiency': kpi_weights.get('production_efficiency', 0.40),
            'quality_cost': kpi_weights.get('quality_cost', 0.30),
            'agv_efficiency': kpi_weights.get('agv_efficiency', 0.30)
        }
        
        # Load sub-weights for each KPI category
        self.efficiency_weights = kpi_weights.get('efficiency_components', {
            'order_completion': 0.40,
            'production_cycle': 0.40,
            'device_utilization': 0.20
        })
        
        self.quality_cost_weights = kpi_weights.get('quality_cost_components', {
            'first_pass_rate': 0.40,
            'cost_efficiency': 0.60
        })
        
        self.agv_weights = kpi_weights.get('agv_components', {
            'charge_strategy': 0.30,
            'energy_efficiency': 0.40,
            'utilization': 0.30
        })
        
        # Load cost parameters from config
        kpi_costs = config.get('kpi_costs', {})
        self.cost_parameters = {
            'material_cost_per_product': kpi_costs.get('material_cost_per_product', {'P1': 10.0, 'P2': 15.0, 'P3': 20.0}),
            'energy_cost_per_second': kpi_costs.get('energy_cost_per_second', 0.1),
            'energy_cost_multiplier_peak': kpi_costs.get('energy_cost_multiplier_peak', 1.5),
            'maintenance_cost_base': kpi_costs.get('maintenance_cost_base', 8.0),
            'scrap_cost_multiplier': kpi_costs.get('scrap_cost_multiplier', 0.8),
        }
        
        # Load theoretical production times from order generator config
        order_gen_config = config.get('order_generator', {})
        self.theoretical_production_times = order_gen_config.get('theoretical_production_times', {
            'P1': 160.0,
            'P2': 200.0,
            'P3': 250.0
        })
        
        # Track active products
        self.active_products: Dict[str, ProductTracking] = {}
        
        # Track last KPI state for change detection
        self.last_kpi_hash = None
        
        # Track active faults count
        self._active_faults_count = 0

        # Initial KPI publish
        self._check_and_publish_kpi_update()

    def _check_and_publish_kpi_update(self):
        """Calculate KPIs and publish only if changed."""
        kpi_update = self.calculate_current_kpis()
        
        # Create a hash of key KPI values to detect changes
        kpi_hash = (
            round(kpi_update.order_completion_rate, 2),
            round(kpi_update.average_production_cycle, 2),
            round(kpi_update.total_production_cost, 2),
            kpi_update.total_orders,
            kpi_update.completed_orders,
            kpi_update.total_products,
            kpi_update.active_faults,
            round(kpi_update.charge_strategy_efficiency, 2),
            round(kpi_update.agv_energy_efficiency, 2)
        )
        
        # Only publish if KPIs have changed
        if kpi_hash != self.last_kpi_hash:
            self.last_kpi_hash = kpi_hash
            self._publish_kpi_update(kpi_update)

    def register_new_order(self, order: NewOrder):
        """Register a new order for tracking."""
        total_items = sum(item.quantity for item in order.items)
        
        order_tracking = OrderTracking(
            order_id=order.order_id,
            created_at=order.created_at,
            deadline=order.deadline,
            items_total=total_items,
            priority=order.priority  # Store priority
        )
        
        self.active_orders[order.order_id] = order_tracking
        self.stats.total_orders += 1
        
        # Trigger KPI update on new order
        self._check_and_publish_kpi_update()
        
        # Create product tracking for each product (material costs will be added when taken from warehouse)
        for item in order.items:
            # Create product tracking for each product
            for i in range(item.quantity):
                product_id = f"{order.order_id}_P{item.product_type}_{i}"
                product_tracking = ProductTracking(
                    product_id=product_id,
                    product_type=item.product_type,
                    order_id=order.order_id,
                    start_time=self.env.now,  # Order creation time
                    theoretical_time=self.theoretical_production_times[item.product_type]
                )
                order_tracking.products.append(product_tracking)
                self.active_products[product_id] = product_tracking

    def complete_order_item(self, order_id: str, product_type: Optional[str] = None, passed_quality: bool = True):
        """Mark an order item as completed."""
        if order_id not in self.active_orders:
            return
            
        order = self.active_orders[order_id]
        order.items_completed += 1
        self.stats.total_products += 1
        
        # Find the product tracking and update it
        if product_type:
            for product in order.products:
                if product.product_type == product_type and product.end_time is None:
                    product.end_time = self.env.now
                    
                    # Use production_start_time if available, otherwise fall back to start_time
                    actual_start = product.production_start_time if product.production_start_time is not None else product.start_time
                    actual_time = product.end_time - actual_start
                    theoretical_time = product.theoretical_time
                    
                    # Add to weighted production cycle sum only for passed products
                    if theoretical_time > 0 and passed_quality:
                        self.stats.weighted_production_cycle_sum += actual_time / theoretical_time
                    
                    # Remove from active products
                    if product.product_id in self.active_products:
                        del self.active_products[product.product_id]
                    break
        
        if passed_quality:
            self.stats.quality_passed_products += 1
        else:
            self.stats.scrapped_products += 1
            # Add scrap cost
            if product_type:
                scrap_cost = self.cost_parameters['material_cost_per_product'][product_type] * self.cost_parameters['scrap_cost_multiplier']
                order.total_cost += scrap_cost
                self.stats.scrap_costs += scrap_cost
        
        # Check if order is complete
        if order.items_completed >= order.items_total:
            self._complete_order(order)

    def _complete_order(self, order: OrderTracking):
        """Complete an order and update statistics."""
        order.completed_at = self.env.now
        order.is_on_time = order.completed_at <= order.deadline
        
        self.stats.completed_orders += 1
        if order.is_on_time:
            self.stats.on_time_orders += 1
            
        # Add to production time tracking
        production_time = order.completed_at - order.created_at
        self.stats.total_production_time += production_time
        
        # Move to completed orders
        self.completed_orders.append(order)
        del self.active_orders[order.order_id]
        
        # Trigger KPI update on order completion
        self._check_and_publish_kpi_update()

    def add_energy_cost(self, device_id: str, line_id: Optional[str], duration: float, is_peak_hour: bool = False):
        """Add energy costs for device operation."""
        # Validate parameters to help debug
        try:
            if not isinstance(duration, (int, float)):
                raise TypeError(f"Duration must be a number, got {type(duration).__name__}: {duration}")
        except Exception as e:
            print(f"[DEBUG] add_energy_cost called with: device_id={device_id}, line_id={line_id}, duration={duration}, is_peak_hour={is_peak_hour}")
            raise
        
        base_cost = duration * self.cost_parameters['energy_cost_per_second']
        if is_peak_hour:
            base_cost *= self.cost_parameters['energy_cost_multiplier_peak']
        
        self.stats.energy_costs += base_cost
        
        # Update device working time with line_id to avoid conflicts
        # Note: Some devices (Station) only use this method to track working time
        internal_device_key = f"{line_id}_{device_id}" if line_id else device_id
        if internal_device_key not in self.stats.device_working_time:
            self.stats.device_working_time[internal_device_key] = 0.0
        self.stats.device_working_time[internal_device_key] += duration
        
        # Trigger KPI update on energy cost change
        self._check_and_publish_kpi_update()

    def add_maintenance_cost(self, _device_id: str, _maintenance_type: str, was_correct_diagnosis: bool):
        """Add maintenance costs and track diagnosis accuracy."""
        base_cost = self.cost_parameters['maintenance_cost_base']
        
        # Penalty for incorrect diagnosis
        if not was_correct_diagnosis:
            base_cost *= 2.0  # Double cost for wrong diagnosis
            
        self.stats.maintenance_costs += base_cost
        self.stats.total_faults += 1
        
        if was_correct_diagnosis:
            self.stats.correct_diagnoses += 1
        
        # Trigger KPI update on maintenance event
        self._check_and_publish_kpi_update()
    
    def update_active_faults_count(self, count: int):
        """Update the active faults count (called by FaultSystem)"""
        self._active_faults_count = count
        self._check_and_publish_kpi_update()
    
    def mark_production_start(self, product: 'Product'):
        """Mark when a product actually starts production (enters first station)."""
        # Find the product tracking by order_id and product_type
        order_id = product.order_id
        product_type = product.product_type
        
        # Look for the first unstarted product of this type in this order
        for tracking_id, tracking in self.active_products.items():
            if (tracking.order_id == order_id and 
                tracking.product_type == product_type and 
                tracking.production_start_time is None):
                tracking.production_start_time = self.env.now
                print(f"[KPI] Product {product.id} (tracking: {tracking_id}) started production at {self.env.now:.2f}")
                break
    
    def register_agv_charge(self, agv_id: str, line_id: Optional[str], is_active: bool, charge_duration: float):
        """Register AGV charging event."""
        if is_active:
            self.stats.agv_active_charges += 1
        else:
            self.stats.agv_passive_charges += 1
        
        self.stats.agv_total_charge_time += charge_duration
        
        # Update AGV charge time tracking with line_id to avoid conflicts
        internal_agv_key = f"{line_id}_{agv_id}" if line_id else agv_id
        if internal_agv_key not in self.stats.agv_charge_time:
            self.stats.agv_charge_time[internal_agv_key] = 0.0
        self.stats.agv_charge_time[internal_agv_key] += charge_duration
        
        # Trigger KPI update on charging event
        self._check_and_publish_kpi_update()
    
    def register_agv_task_complete(self, _agv_id: str, _line_id: Optional[str] = None):
        """Register AGV task completion."""
        self.stats.agv_completed_tasks += 1
        
        # Trigger KPI update on AGV task completion
        self._check_and_publish_kpi_update()
    
    def update_agv_transport_time(self, agv_id: str, line_id: Optional[str], transport_time: float):
        """Update AGV transport time."""
        internal_agv_key = f"{line_id}_{agv_id}" if line_id else agv_id
        if internal_agv_key not in self.stats.agv_transport_time:
            self.stats.agv_transport_time[internal_agv_key] = 0.0
        self.stats.agv_transport_time[internal_agv_key] += transport_time
        
        # Trigger KPI update on AGV transport time change
        self._check_and_publish_kpi_update()
    
    def update_agv_fault_time(self, agv_id: str, line_id: Optional[str], fault_time: float):
        """Update AGV fault time."""
        internal_agv_key = f"{line_id}_{agv_id}" if line_id else agv_id
        if internal_agv_key not in self.stats.agv_fault_time:
            self.stats.agv_fault_time[internal_agv_key] = 0.0
        self.stats.agv_fault_time[internal_agv_key] += fault_time
        
        # Trigger KPI update on AGV fault time change
        self._check_and_publish_kpi_update()

    def add_fault_recovery_time(self, recovery_time: float):
        """Track fault recovery time for robustness metrics."""
        self.stats.total_recovery_time += recovery_time
        
        # Trigger KPI update on fault recovery
        self._check_and_publish_kpi_update()

    def update_device_utilization(self, device_id: str, line_id: Optional[str], total_time: float):
        """Update device total time for utilization calculation."""
        internal_device_key = f"{line_id}_{device_id}" if line_id else device_id
        self.stats.device_total_time[internal_device_key] = total_time
        # Ensure device has a working_time entry to prevent KeyError
        if internal_device_key not in self.stats.device_working_time:
            self.stats.device_working_time[internal_device_key] = 0.0
        
        # Trigger KPI update on device utilization change
        self._check_and_publish_kpi_update()
    
    def track_device_working_time(self, device_id: str, line_id: Optional[str], duration: float):
        """Track actual working time for a device"""
        internal_device_key = f"{line_id}_{device_id}" if line_id else device_id
        if internal_device_key not in self.stats.device_working_time:
            self.stats.device_working_time[internal_device_key] = 0.0
        self.stats.device_working_time[internal_device_key] += duration
        
        # Trigger KPI update on device working time change
        self._check_and_publish_kpi_update()

    def calculate_current_kpis(self) -> KPIUpdate:
        """Calculate current KPI values according to PRD 3.4 Section 2.8 formulas."""
        current_time = self.env.now
        self.stats.total_simulation_time = current_time
        
        # Production Efficiency Metrics (40%)
        # 1. 订单完成率 (按时完成订单数 / 总订单数)
        order_completion_rate = (
            (self.stats.on_time_orders / self.stats.total_orders * 100) 
            if self.stats.total_orders > 0 else 0.0
        )
        
        # 2. 加权平均生产周期 (实际时间与理论时间的比率)
        # 改进：考虑产品完成率，避免选择性完成快速产品的策略
        base_cycle_ratio = (
            (self.stats.weighted_production_cycle_sum / self.stats.quality_passed_products)
            if self.stats.quality_passed_products > 0 else 0.0
        )
        
        # 计算产品完成率（已完成的产品数 / 已开始生产的产品总数）
        total_started_products = len(self.active_products) + self.stats.total_products
        product_completion_rate = (
            self.stats.quality_passed_products / total_started_products
            if total_started_products > 0 else 0.0
        )
        
        # 应用完成率权重：完成率低会增加生产周期值（惩罚）
        # 如果完成率是100%，则不影响；如果完成率是50%，则周期值翻倍
        if product_completion_rate > 0:
            average_production_cycle = base_cycle_ratio / product_completion_rate
        else:
            average_production_cycle = 0.0  # No production = 0 efficiency
        
        # 3. 按时交付率 (这个指标与订单完成率重复，可用于额外分析)
        on_time_delivery_rate = (
            (self.stats.on_time_orders / self.stats.completed_orders * 100)
            if self.stats.completed_orders > 0 else 0.0
        )
        
        # Quality Metrics
        first_pass_rate = (
            (self.stats.quality_passed_products / self.stats.total_products * 100)
            if self.stats.total_products > 0 else 0.0
        )
        
        # Device Utilization
        device_utilization = {}
        for device_id in self.stats.device_working_time:
            working_time = self.stats.device_working_time[device_id]
            total_time = self.stats.device_total_time.get(device_id, current_time)
            utilization = (working_time / total_time * 100) if total_time > 0 else 0.0
            device_utilization[device_id] = utilization
        
        average_device_utilization = (
            sum(device_utilization.values()) / len(device_utilization)
            if device_utilization else 0.0
        )
        
        # Cost Control Metrics (30%)
        total_production_cost = (
            self.stats.material_costs + self.stats.energy_costs + 
            self.stats.maintenance_costs + self.stats.scrap_costs
        )
        
        # AGV Metrics
        # 充电策略效率
        total_charges = self.stats.agv_active_charges + self.stats.agv_passive_charges
        charge_strategy_efficiency = (
            (self.stats.agv_active_charges / total_charges * 100)
            if total_charges > 0 else 0.0  # No charging activity = 0 efficiency
        )
        
        # AGV能效比 (完成任务数 / 总充电时间)
        # 时间单位：秒
        agv_energy_efficiency = (
            (self.stats.agv_completed_tasks / self.stats.agv_total_charge_time)  # tasks per second of charging
            if self.stats.agv_total_charge_time > 0 else 0.0
        )
        
        # AGV利用率
        agv_utilization = {}
        for agv_id in self.stats.agv_transport_time:
            transport_time = self.stats.agv_transport_time.get(agv_id, 0.0)
            fault_time = self.stats.agv_fault_time.get(agv_id, 0.0)
            charge_time = self.stats.agv_charge_time.get(agv_id, 0.0)
            total_time = current_time - fault_time - charge_time
            
            if total_time > 0:
                # Cap utilization at 100% to handle edge cases
                agv_utilization[agv_id] = min(100.0, transport_time / total_time * 100)
            else:
                agv_utilization[agv_id] = 0.0
        
        average_agv_utilization = (
            sum(agv_utilization.values()) / len(agv_utilization)
            if agv_utilization else 0.0
        )
        
        return KPIUpdate(
            timestamp=round(current_time, 2),
            
            # Production Efficiency (40%)
            order_completion_rate=round(order_completion_rate, 2),
            average_production_cycle=round(average_production_cycle, 2),
            on_time_delivery_rate=round(on_time_delivery_rate, 2),
            device_utilization=round(average_device_utilization, 2),
            
            # Quality Metrics
            first_pass_rate=round(first_pass_rate, 2),
            
            # Cost Control (30%)
            total_production_cost=round(total_production_cost, 2),
            material_costs=round(self.stats.material_costs, 2),
            energy_costs=round(self.stats.energy_costs, 2),
            maintenance_costs=round(self.stats.maintenance_costs, 2),
            scrap_costs=round(self.stats.scrap_costs, 2),
            
            # AGV Efficiency Metrics
            charge_strategy_efficiency=round(charge_strategy_efficiency, 2),
            agv_energy_efficiency=round(agv_energy_efficiency, 2),
            agv_utilization=round(average_agv_utilization, 2),
            
            # Raw Counts
            total_orders=self.stats.total_orders,
            completed_orders=self.stats.completed_orders,
            active_orders=len(self.active_orders),
            total_products=self.stats.total_products,
            active_faults=self._active_faults_count
        )

    def _publish_kpi_update(self, kpi_update: KPIUpdate):
        """Publish KPI update to MQTT."""
        if self.mqtt_client:
            try:
                if self.topic_manager:
                    topic = self.topic_manager.get_kpi_topic()
                else:
                    from factory_sim.config.topics import KPI_UPDATE_TOPIC
                    topic = KPI_UPDATE_TOPIC
                self.mqtt_client.publish(topic, kpi_update.model_dump_json())
                # print(f"[{self.env.now:.2f}] 📊 KPI Update published")
            except Exception as e:
                print(f"[{self.env.now:.2f}] ❌ Failed to publish KPI update: {e}")
        if self.database:
            try:
                self.database.insert_data('kpi', kpi_update.model_dump())
            except Exception as e:
                print(f"[{self.env.now:.2f}] ❌ Failed to insert KPI update into database: {e}")
        
    def force_kpi_update(self):
        """Force an immediate KPI update (bypasses change detection)."""
        kpi_update = self.calculate_current_kpis()
        self._publish_kpi_update(kpi_update)
        
        # Update the hash to reflect current state
        self.last_kpi_hash = (
            round(kpi_update.order_completion_rate, 2),
            round(kpi_update.average_production_cycle, 2),
            round(kpi_update.total_production_cost, 2),
            kpi_update.total_orders,
            kpi_update.completed_orders,
            kpi_update.total_products,
            kpi_update.active_faults,
            round(kpi_update.charge_strategy_efficiency, 2),
            round(kpi_update.agv_energy_efficiency, 2)
        )

    def get_final_score(self) -> Dict[str, Any]:
        """Calculate final competition score based on PRD 3.4 Section 2.8 KPIs.
        
        Since PRD 3.4 doesn't specify competition scoring weights, we use:
        - Production Efficiency: 40% (order completion, production cycle, device utilization)
        - Quality & Cost: 30% (first pass rate, total cost)
        - AGV Efficiency: 30% (charge strategy, energy efficiency, utilization)
        """
        kpis = self.calculate_current_kpis()
        
        # 生产效率评分 (40%)
        # 包含：订单完成率、加权平均生产周期、设备利用率
        
        # 生产周期评分：只有在有产品生产时才计算
        if self.stats.quality_passed_products > 0 and kpis.average_production_cycle > 0:
            # 比率越接近1越好（实际时间接近理论时间）
            production_cycle_score = min(100, 100 / max(1, kpis.average_production_cycle))
        else:
            # 没有生产任何产品，周期效率为0
            production_cycle_score = 0
        
        efficiency_components = {
            'order_completion': min(100, kpis.order_completion_rate),  # 已经是百分比
            'production_cycle': production_cycle_score,
            'device_utilization': min(100, kpis.device_utilization)  # 已经是百分比
        }
        
        # 子权重分配
        efficiency_score = (
            efficiency_components['order_completion'] * self.efficiency_weights.get('order_completion', 0.4) +
            efficiency_components['production_cycle'] * self.efficiency_weights.get('production_cycle', 0.4) +
            efficiency_components['device_utilization'] * self.efficiency_weights.get('device_utilization', 0.2)
        ) * self.weights['production_efficiency']  # 使用配置的权重
        
        # 成本控制评分 (30%)
        # 基于总生产成本，需要与基准成本比较
        if self.stats.total_products > 0:
            # 如果有生产产品，计算成本效率
            # 基准成本：使用配置中的平均材料成本
            avg_material_cost = sum(self.cost_parameters['material_cost_per_product'].values()) / len(self.cost_parameters['material_cost_per_product'])
            baseline_cost = self.stats.total_products * avg_material_cost
            actual_cost = kpis.total_production_cost
            # 成本越低越好，所以用基准成本除以实际成本
            cost_efficiency = min(100, baseline_cost / max(1, actual_cost) * 100)
        else:
            # 如果没有生产任何产品
            if kpis.total_production_cost > 0:
                # 有成本但没有产出，效率为0
                cost_efficiency = 0
            else:
                # 没有成本也没有产出，给予0分
                cost_efficiency = 0
        
        # 质量与成本评分 (30%)
        # 包含：一次通过率和总生产成本
        quality_cost_components = {
            'first_pass_rate': min(100, kpis.first_pass_rate),  # 已经是百分比
            'cost_efficiency': cost_efficiency
        }
        
        quality_cost_score = (
            quality_cost_components['first_pass_rate'] * self.quality_cost_weights.get('first_pass_rate', 0.4) +
            quality_cost_components['cost_efficiency'] * self.quality_cost_weights.get('cost_efficiency', 0.6)
        ) * self.weights['quality_cost']  # 使用配置的权重
        
        # AGV效率评分 (30%)
        # 包含：充电策略效率、AGV能效比、AGV利用率
        agv_components = {
            'charge_strategy': min(100, kpis.charge_strategy_efficiency),  # 已经是百分比
            'energy_efficiency': min(100, kpis.agv_energy_efficiency * 100),  # 0.1 tasks/s = 100分 (每秒完成0.1个任务为满分)
            'utilization': min(100, kpis.agv_utilization)  # 已经是百分比
        }
        
        # AGV效率权重
        agv_score = (
            agv_components['charge_strategy'] * self.agv_weights.get('charge_strategy', 0.3) +
            agv_components['energy_efficiency'] * self.agv_weights.get('energy_efficiency', 0.4) +
            agv_components['utilization'] * self.agv_weights.get('utilization', 0.3)
        ) * self.weights['agv_efficiency']
        
        total_score = efficiency_score + quality_cost_score + agv_score
        
        return {
            "efficiency_score": efficiency_score,
            "efficiency_components": efficiency_components,
            "quality_cost_score": quality_cost_score,
            "quality_cost_components": quality_cost_components,
            "agv_score": agv_score,
            "agv_components": agv_components,
            "total_score": total_score,
            "raw_kpis": {
                "order_completion_rate": kpis.order_completion_rate,
                "average_production_cycle": kpis.average_production_cycle,
                "device_utilization": kpis.device_utilization,
                "first_pass_rate": kpis.first_pass_rate,
                "total_production_cost": kpis.total_production_cost,
                "charge_strategy_efficiency": kpis.charge_strategy_efficiency,
                "agv_energy_efficiency": kpis.agv_energy_efficiency,
                "agv_utilization": kpis.agv_utilization
            }
        } 