### 赛道二：排队论与并发调度 (底层 `Ciw`)

此赛道关注异质实体的并发抢占、排班机制、状态依赖路由以及排队放弃行为。

#### 1. Blueprint 核心结构规范

```python
# scenario_blueprint.py (Track 2)
import random

metadata = {
    "domain": "Hospital_ER",
    "description": "包含分诊台和急诊室的医疗系统。重症直接进入抢救室...",
    "terminology": {
        "N1": "分诊台", "N2": "普通诊室", "N3": "重症抢救室"
    }
}

topology = {
    "nodes": {
        "N1": {"capacity": 2}, # 静态容量
        "N2": {
            # 复杂排班：前 120 时间单位 2 人，后 120 时间单位 3 人，换班时重启任务
            "capacity_schedule": {"counts": [2, 3], "shift_ends": [120, 240], "preemption": "restart"}
        },
        "N3": {"capacity": 1}
    },
    "arrivals": [
        {
            "target": "N1", 
            # 非平稳到达：分段泊松分布
            "rate_schedule": {"rates": [0.5, 0.1], "endpoints": [120, 240]},
            # 实体初始化属性：生成时携带
            "entity_init_func": lambda: {"severity": random.choice(["normal", "critical"])}
        }
    ],
    # 选填：中途放弃耐心时间分布 f() -> float
    "reneging_dists": {"N1": lambda: random.expovariate(1/30.0)}
}

# 自定义逻辑注入点 (Hooks)
custom_hooks = {
    # 状态依赖路由 f(current_node_id: str, entity_attributes: dict) -> str
    "routing_func": lambda node, attr: "N3" if attr.get("severity") == "critical" else "N2",
    
    # 动态服务时间 f(current_node_id: str, entity_attributes: dict, current_time: float, queue_length: int) -> float
    "service_time_func": None,
    
    # 顾客看到排队太长止步 f(current_node_id: str, entity_attributes: dict, queue_length: int) -> bool
    "balking_func": None
}

# 组件级检查器 (基于标准的 SDES 排队日志格式)
def check_trauma_room_exclusive(logs):
    # 断言逻辑...
    pass

checkers = [check_trauma_room_exclusive]

```

#### 2. 下游 Adapter 如何使用这些元素？

* **`topology.nodes["capacity_schedule"]`**：
* **用途**：本地 Adapter 识别到这个键，会直接将其参数解包并传入 `ciw.Schedule()`，随后注入到 `create_network` 的 `number_of_servers` 列表中。


* **`topology.arrivals["rate_schedule"]`**：
* **用途**：本地 Adapter 将其转化为 `ciw.dists.PoissonIntervals`，原生实现早晚高峰。


* **`topology.arrivals["entity_init_func"]`**：
* **用途**：本地 Adapter 继承 `ciw.ArrivalNode`，在生成顾客对象时，调用该函数并将其返回值挂载到顾客的 `custom_attributes` 字典上。


* **`custom_hooks["routing_func"]`**：
* **用途**：本地 Adapter 实例化预先写好的 `LLMRouter`（继承自 `ciw.routing.NodeRouting`），在 `next_node()` 方法中调用大模型的函数。返回的字符串（如 "N3"）会被 Adapter 映射回节点索引（如 3），返回 "EXIT" 则映射为 `-1` 离开系统。


* **`custom_hooks["service_time_func"]`**：
* **用途**：本地 Adapter 实例化 `LLMServiceDistribution`（继承自 `ciw.dists.Distribution`），在底层的 `sample(t, ind)` 触发时，提取参数并调用大模型的计算逻辑。


* **`checkers`**：
* **用途**：本地 Adapter 调用 `Q.get_all_records()`，将其转换为统一的字典列表，然后喂给 Checker 函数做沙盒测试。
