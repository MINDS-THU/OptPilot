# src/game_logic/fault_system.py
import random
import simpy
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum
from factory_sim.utils.mqtt_client import MQTTClient
from factory_sim.config.schemas import DeviceStatus, FaultAlert
import json 
from factory_sim.utils.topic_manager import TopicManager
from factory_sim.utils.sqlite_db import SimulationDatabase

@dataclass
class FaultDefinition:
    """简化的故障定义"""
    symptom: str
    min_duration: float  # 最小故障持续时间（秒）
    max_duration: float  # 最大故障持续时间（秒）

class FaultType(Enum):
    STATION_FAULT = "station_fault"
    AGV_FAULT = "agv_fault"
    CONVEYOR_FAULT = "conveyor_fault"

class FaultSystem:
    """
    简化的故障系统：冻结设备，过一段时间解冻
    """

    def __init__(self, env: simpy.Environment, devices: Dict, mqtt_client: Optional[MQTTClient] = None, database: Optional[SimulationDatabase] = None, topic_manager: Optional[TopicManager] = None, line_id: Optional[str] = None, kpi_calculator=None, **kwargs):
        self.env = env
        self.factory_devices = devices
        self.mqtt_client = mqtt_client
        self.database = database
        self.topic_manager = topic_manager
        self.line_id = line_id
        self.kpi_calculator = kpi_calculator
        self.active_faults: Dict[str, 'SimpleFault'] = {}
        self.fault_processes: Dict[str, simpy.Process] = {}
        self.pending_agv_faults: Dict[str, FaultType] = {} # 新增：用于挂起对繁忙AGV的故障

        duration_range = kwargs.get("fault_duration_range", (20.0, 60.0))
        if len(duration_range) != 2 or duration_range[0] <= 0 or duration_range[1] < duration_range[0]:
            raise ValueError("fault_duration_range must be [positive_min, max].")
        min_duration, max_duration = map(float, duration_range)
        self.fault_definitions = {
            FaultType.STATION_FAULT: FaultDefinition(
                symptom="Station Vibration",
                min_duration=min_duration,
                max_duration=max_duration,
            ),
            FaultType.AGV_FAULT: FaultDefinition(
                symptom="AGV Stuck",
                min_duration=min_duration,
                max_duration=max_duration,
            ),
            FaultType.CONVEYOR_FAULT: FaultDefinition(
                symptom="Conveyor Stuck",
                min_duration=min_duration,
                max_duration=max_duration,
            )
        }

        # 故障注入参数
        self.fault_injection_interval = kwargs.get('fault_injection_interval', (120, 300))

        # 开始故障注入过程
        self.env.process(self.run_fault_injection())

    def run_fault_injection(self):
        """故障注入主循环"""
        while True:
            # 等待下次故障注入
            wait_time = random.uniform(*self.fault_injection_interval)
            yield self.env.timeout(wait_time)

            # 注入随机故障
            self.inject_random_fault()

    def inject_random_fault(self, target_device: Optional[str] = None, fault_type: Optional[FaultType] = None):
        """注入随机故障"""
        if fault_type is None:
            fault_type = random.choice(list(FaultType))

        if target_device is None:
            target_device = self._select_target_device(fault_type)

        # Check if the device has already been injected with a fault
        if target_device in self.active_faults:
            print(f"[{self.env.now:.2f}] ⚠️  设备 {target_device} 已有故障，跳过注入")
            return

        device = self.factory_devices[target_device]

        # For AGVs, if they are not idle, pend the fault instead of skipping
        if fault_type == FaultType.AGV_FAULT and device.status != DeviceStatus.IDLE:
            if target_device not in self.pending_agv_faults:
                self.pending_agv_faults[target_device] = fault_type
                print(f"[{self.env.now:.2f}] ⚠️  AGV {target_device} is currently {device.status.value}, fault injection is pending.")
            else:
                print(f"[{self.env.now:.2f}] ⚠️  AGV {target_device} already has a pending fault, skipping new injection.")
            return

        # Inject the fault now for non-AGVs or idle AGVs
        self._inject_fault_now(target_device, fault_type)

    def _select_target_device(self, fault_type: FaultType) -> str:
        """根据故障类型选择目标设备"""
        if fault_type == FaultType.AGV_FAULT:
            # AGV故障
            agv_devices = [dev_id for dev_id in self.factory_devices.keys() if "AGV" in dev_id]
            return random.choice(agv_devices) if agv_devices else "AGV_1"
        elif fault_type == FaultType.CONVEYOR_FAULT:
            # 传送带故障 except Conveyor_CQ
            conveyor_devices = [dev_id for dev_id in self.factory_devices.keys() if "Conveyor" in dev_id and "CQ" not in dev_id]
            return random.choice(conveyor_devices) if conveyor_devices else "Conveyor_AB"
        else:
            # 工站故障
            station_devices = [dev_id for dev_id in self.factory_devices.keys() 
                             if "Station" in dev_id or "Quality" in dev_id]
            return random.choice(station_devices) if station_devices else "StationA"

    def _inject_fault_now(self, device_id: str, fault_type: FaultType, duration: Optional[float] = None):
        """立即注入故障的核心逻辑"""
        if device_id in self.active_faults:
            # This check is important for when called externally
            print(f"[{self.env.now:.2f}] ⚠️  设备 {device_id} 已有故障，无法注入新故障")
            return

        if duration is None:
            fault = self._create_fault(device_id, fault_type)
        else: # For manual injection with specified duration
            fault = SimpleFault(
                device_id=device_id,
                fault_type=fault_type,
                symptom=self.fault_definitions[fault_type].symptom,
                duration=duration,
                start_time=self.env.now
            )

        self.active_faults[device_id] = fault

        device = self.factory_devices[device_id]

        # Special handling for conveyors - interrupt processing instead of main action
        if hasattr(device, 'interrupt_all_processing'):
            interrupted_count = device.interrupt_all_processing()
            print(f"[{self.env.now:.2f}] 🚫 {device_id}: Interrupted {interrupted_count} processing operations")
        # For other devices, interrupt the main action
        elif hasattr(device, 'action') and device.action and device.action.is_alive and device.action != self.env.active_process:
            device.action.interrupt("Fault injected")

        device.set_status(DeviceStatus.FAULT)
        device.publish_status(f"[{self.env.now:.2f}] {device_id}: Fault injected: {fault.symptom}")

        # If the device has a fault symptom attribute, set it
        if hasattr(device, 'fault_symptom'):
            device.fault_symptom = fault.symptom

        print(f"[{self.env.now:.2f}] 💥 故障注入: {device_id}")
        print(f"   - 症状: {fault.symptom}")
        print(f"   - 持续时间: {fault.duration:.1f}s")
        print(f"   - 🚫 设备已冻结")

        self._send_fault_alert(device_id, fault)

        # Report maintenance cost to KPI calculator (fault detection)
        if self.kpi_calculator:
            # Assume correct diagnosis for auto-generated faults
            self.kpi_calculator.add_maintenance_cost(device_id, fault.symptom, was_correct_diagnosis=True)

        # Start fault process
        fault_process = self.env.process(self._run_fault_process(fault))
        self.fault_processes[device_id] = fault_process

    def _create_fault(self, device_id: str, fault_type: FaultType) -> 'SimpleFault':
        """创建简单故障实例"""
        definition = self.fault_definitions[fault_type]
        duration = random.uniform(definition.min_duration, definition.max_duration)

        return SimpleFault(
            device_id=device_id,
            fault_type=fault_type,
            symptom=definition.symptom,
            duration=duration,
            start_time=self.env.now
        )

    def _run_fault_process(self, fault: 'SimpleFault'):
        """运行故障过程（等待故障持续时间）"""
        try:
            # Wait for the fault duration
            yield self.env.timeout(fault.duration)

            # Fault duration ended, automatically unfreeze the device
            self._clear_fault(fault.device_id)

        except simpy.Interrupt:
            # Fault process interrupted (e.g., manual repair)
            print(f"[{self.env.now:.2f}] 🔧 故障过程被中断: {fault.device_id}")

    def _clear_fault(self, device_id: str):
        """Clear the fault and unfreeze the device"""
        if device_id in self.active_faults:
            fault = self.active_faults[device_id]
            fault_symptom = fault.symptom
            # Calculate recovery time before deleting the fault
            recovery_time = self.env.now - fault.start_time

            del self.active_faults[device_id]

            # Clear the fault process
            if device_id in self.fault_processes:
                del self.fault_processes[device_id]

            # Unfreeze the device
            device = self.factory_devices[device_id]
            if hasattr(device, 'recover'):
                device.recover()
            else:
                # Fallback to default if no specific recover method
                device.set_status(DeviceStatus.IDLE)

            # Clear the fault symptom
            if hasattr(device, 'fault_symptom'):
                device.fault_symptom = None

            print(f"[{self.env.now:.2f}] ✅ 故障自动解除: {device_id}")
            print(f"   - 🔓 设备已解冻")

            # Report recovery time to KPI calculator
            if self.kpi_calculator and recovery_time > 0:
                self.kpi_calculator.add_fault_recovery_time(recovery_time)

                # Track AGV fault time specifically
                if fault.fault_type == FaultType.AGV_FAULT:
                    self.kpi_calculator.update_agv_fault_time(device_id, self.line_id, recovery_time)

            # Send recovery alert
            self._send_recovery_alert(device_id, fault_symptom)

    def _send_fault_alert(self, device_id: str, fault: 'SimpleFault'):
        """发送故障警报"""
        alert_data = FaultAlert(
            timestamp=self.env.now,
            device_id=device_id,
            alert_type="fault_injected",
            symptom=fault.symptom,
            fault_type=fault.fault_type.value,
            estimated_duration=fault.duration,
            message=f"Device {device_id} has fault: {fault.symptom}"
        )

        if self.mqtt_client and self.topic_manager and self.line_id:
            topic = self.topic_manager.get_fault_alert_topic(self.line_id)
            self.mqtt_client.publish(topic, alert_data.model_dump_json())

        if self.database:
            table_name = f"{self.line_id.lower()}_fault" if self.line_id else "fault"
            keys_to_remove = ["device_id"]
            self.database.insert_data(table_name, {k: v for k, v in alert_data.model_dump().items() if k not in keys_to_remove})

    def _send_recovery_alert(self, device_id: str, last_symptom: str):
        """发送恢复警报"""
        alert_data = FaultAlert(
            timestamp=self.env.now,
            device_id=device_id,
            alert_type="fault_recovered",
            symptom=last_symptom,
            fault_type=None,  # We don't know the fault type anymore after clearing it
            estimated_duration=None,
            message=f"Device {device_id} fault has been automatically recovered",
        )

        if self.mqtt_client and self.topic_manager and self.line_id:
            topic = self.topic_manager.get_fault_alert_topic(self.line_id)
            self.mqtt_client.publish(topic, alert_data.model_dump_json())

        if self.database:
            table_name = f"{self.line_id.lower()}_fault" if self.line_id else "fault"
            keys_to_remove = ["device_id"]
            self.database.insert_data(table_name, {k: v for k, v in alert_data.model_dump().items() if k not in keys_to_remove})

    def force_clear_fault(self, device_id: str) -> bool:
        """强制清除故障（调试用）"""
        if device_id in self.active_faults:
            # 中断故障过程
            if device_id in self.fault_processes:
                self.fault_processes[device_id].interrupt()

            # 清除故障
            self._clear_fault(device_id)
            print(f"[{self.env.now:.2f}] 🔧 强制清除故障: {device_id}")
            return True

        print(f"[{self.env.now:.2f}] ❌ 设备 {device_id} 无故障需要清除")
        return False

    def get_device_symptom(self, device_id: str) -> Optional[str]:
        """获取设备症状"""
        if device_id in self.active_faults:
            return self.active_faults[device_id].symptom
        return None

    def is_device_faulty(self, device_id: str) -> bool:
        """检查设备是否有故障"""
        return device_id in self.active_faults

    def get_fault_info(self, device_id: str) -> Optional[Dict]:
        """获取设备故障信息"""
        if device_id in self.active_faults:
            fault = self.active_faults[device_id]
            remaining_time = fault.duration - (self.env.now - fault.start_time)
            return {
                "device_id": device_id,
                "fault_type": fault.fault_type.value,
                "symptom": fault.symptom,
                "duration": fault.duration,
                "remaining_time": max(0, remaining_time),
                "start_time": fault.start_time
            }
        return None

    def get_all_fault_info(self) -> List[Dict]:
        """获取所有故障信息"""
        fault_info_list = []
        for device_id in self.active_faults.keys():
            fault_info = self.get_fault_info(device_id)
            if fault_info is not None:
                fault_info_list.append(fault_info)
        return fault_info_list

    def get_available_devices(self) -> List[str]:
        """获取可用设备列表（无故障的设备）"""
        available = []
        for device_id, device in self.factory_devices.items():
            if device_id not in self.active_faults and device.status != DeviceStatus.FAULT:
                available.append(device_id)
        return available

    def get_fault_stats(self) -> Dict:
        """获取故障统计信息"""
        return {
            "active_faults": len(self.active_faults),
            "fault_devices": list(self.active_faults.keys()),
            "available_devices": len(self.get_available_devices()),
            "total_devices": len(self.factory_devices)
        }

@dataclass
class SimpleFault:
    """简化的故障实例"""
    device_id: str
    fault_type: FaultType
    symptom: str
    duration: float  # 故障持续时间（秒）
    start_time: float

# 为了向后兼容，保留原有的类名
FaultSystem = FaultSystem
ActiveFault = SimpleFault
