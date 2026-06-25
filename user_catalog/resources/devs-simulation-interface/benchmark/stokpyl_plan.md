

### 赛道一：宏观物流与库存调配 (底层 `stockpyl`)

此赛道关注离散周期下的宏观资源流动、库存阈值触发（如 s,S 策略）、缺货惩罚与牛鞭效应。

#### 1. Blueprint 核心结构规范

```python
# scenario_blueprint.py (Track 1)

metadata = {
    "domain": "E-Commerce", # 领域标签
    "description": "中央仓为两个前置仓供货的电商网络...", # 纯自然语言描述
    "terminology": { # 术语映射表
        "N0": "供应商", "N1": "中央仓", "N2": "华东前置仓"
    }
}

topology = {
    "nodes": {
        "N0": {"role": "source"}, # 源头节点（无限产能）
        "N1": {
            "role": "distributor", 
            "lead_time": 3,       # 必须为整数
            "holding_cost": 2.0, 
            "stockout_cost": 50.0,
            "policy": {"type": "sS", "s": 500, "S": 2000} # 可选内置策略
        },
        "N2": {
            "role": "retailer",
            "lead_time": 1,
            # ... 其他参数
        }
    },
    "edges": [
        {"from": "N0", "to": "N1"},
        {"from": "N1", "to": "N2"}
    ]
}

# 自定义逻辑注入点 (Hooks)
custom_hooks = {
    # 动态需求：f(period: int) -> float
    "demand_func": lambda period: 100 if period % 7 != 0 else 300,
    
    # 选填：自定义非线性成本 f(inventory_level: float) -> float
    "holding_cost_func": None,
    
    # 选填：自定义拉动策略 f(node_state, product, inventory_position, period) -> float
    "policy_func": None,
    
    # 选填：节点宕机逻辑 f(node_state, period) -> bool
    "disruption_func": None
}

# 组件级检查器 (基于标准的 SDES 宏观物流日志格式)
def check_inventory_conservation(logs):
    # 断言逻辑...
    pass

checkers = [check_inventory_conservation]

```

#### 2. 下游 Adapter 如何使用这些元素？

* **`metadata`**：
* **用途**：本地脚本通过模板引擎读取 `description` 和 `terminology`，将其拼接、渲染为 `description.yaml`。这个 YAML 就是未来发给被测 LLM 的纯自然语言考题。


* **`topology.nodes` & `topology.edges**`：
* **用途**：本地 Adapter 遍历节点字典，实例化 `SupplyChainNode`。遍历边字典，调用 `network.add_successor(node_from, node_to)` 构建有向图。


* **`custom_hooks["demand_func"]`**：
* **用途**：本地 Adapter 会实例化我们预先写好的 `LLMDemandSource` 包装类（继承自 `DemandSource`），并将这个 lambda 函数传入，覆盖底层的 `generate_demand()` 方法。


* **`custom_hooks["policy_func"]`**：
* **用途**：如果存在，Adapter 实例化 `LLMPolicy` 包装类，跳过 `type` 校验，在 `get_order_quantity()` 中调用此函数，实现极致复杂的拉货逻辑。


* **`checkers`**：
* **用途**：本地 Adapter 在 `stockpyl` 上打好探针、跑完仿真后，将生成的绝对正确的 `golden_logs.jsonl` 传入这些函数。如果抛出 `AssertionError`，触发自动重试或丢弃该蓝图。
