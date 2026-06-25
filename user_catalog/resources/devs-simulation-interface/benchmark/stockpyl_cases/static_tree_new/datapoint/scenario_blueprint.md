
# 赛道一：多产品离散事件仿真蓝图 (Blueprint) 生成规范 v2.0

`scenario_blueprint.py` 是贯穿出题、执行、判卷全流程的唯一中枢配置。该文件必须是一个合法的纯 Python 文件，并严格包含以下 **11 个顶级域**。

## 核心机制：语义实例命名 (Semantic Naming Rule)

本框架底层采用“节点组”机制。引擎初始化时，会自动生成确定性的实例名称，规则为：`{组名}_{索引}`（索引从 0 开始连续编号）。例如：如果组名为 `"Retailer"`，数量为 3，将生成 `"Retailer_0"`, `"Retailer_1"`, `"Retailer_2"`。出题 LLM 在处理日志翻译与连线时，必须基于此命名规则。

---

## 1. 静态映射域：`metadata`

**数据类型**：字典 (`dict`)
**作用**：定义场景的基础业务信息与物理标识映射。

* `domain` (str): 工业领域名称（如 "E-Commerce"）。
* `description` (str): 业务规则与物理定律的详细描述（直接输出到考卷）。
* `products_mapping` (dict): 这是全局产品的注册表。键必须是表示产品 ID 的字符串形式的整数（如 "1", "2"），值为产品名称（如 "Smartphone"）。后续所有涉及库存和订货的字典，其产品键必须与此处定义的 ID 严格对齐！

## 2. 命令行参数契约：`cli_args_schema`

**数据类型**：字典 (`dict`)
**作用**：声明该场景支持外部传入哪些动态参数（如网络规模、成本设定）。这些声明将用于生成测试用例文档。

* 键为参数名（如 `"num_retailers"`）。
* 值为字典：包含 `type` (数据类型, 如 `"int"`), `default` (默认值), `description` (参数业务含义的自然语言描述)。

## 3. 标准输入契约：`stdin_schema`

**数据类型**：字典 (`dict`)
**作用**：定义如果系统有通过系统管道 (`stdin`) 输入的大规模序列数据，其格式是怎样的。

* `is_used` (bool): 标识是否需要接收标准输入。
* `format_description` (str): 详细描述每行数据的含义、分隔符（该字符串将原封不动编织进考卷的 `description.yaml` 中）。

## 4. 动态拓扑域：`topology`


**数据类型**：字典 (`dict`)
**作用**：定义仿真底层的节点组、数量参数以及它们之间的连接关系。
**必须包含的键**：

* `node_groups` (dict): 节点组配置。键为节点组名称（如 `"Central_DC"`, `"Retailer"`）。值为包含以下键的字典：
* `role` (str): 必填。只能是 `"source"`, `"distributor"`, `"retailer"` 之一。
* `count` (int 或 str): 必填。定义该组实例的数量。可以是静态整数（如 `10`），也可以是读取外部配置的参数化字符串（严格格式：`"arg:参数名"`，如 `"arg:num_retailers"`）。
* `lead_time` (int): 选填。向上游订货的运输天数延迟。
* `holding_cost` (float): 选填。单位货物每期的持有成本。
* `stockout_cost` (float): 选填。单位货物的缺货惩罚成本。
* `initial_inventory` (dict): 必填。 物理引擎要求必须显式声明期初库存。键必须是 products_mapping 中定义的真实产品 ID（字符串格式的数字），值为浮点数。（例如 {"1": 150.0}。如果该节点存储多种产品，则需一并列出 {"1": 150.0, "2": 200.0}）。
* `policy` (dict): **必填**。定义节点的默认补货策略。
    * **强制约束**：为了满足底层物理引擎的静态完整性检查，**网络中的每一个节点组都必须声明一个合法的策略对象**。
    * 即使你打算在 `custom_hooks` 中使用 Python 函数完全覆写该节点的策略，你也必须在这里提供一个“兜底占位策略”（例如 `{"type": "BS", "base_stock_level": 0}`）。
    * 必须严格使用底层物理引擎所需的精确属性名：
        * `(s, S)` 策略：`{"type": "sS", "reorder_point": 50, "order_up_to_level": 100}`
        * `基础库存 (Base Stock)` 策略：`{"type": "BS", "base_stock_level": 100}`
        * `(r, Q)` 策略：`{"type": "rQ", "reorder_point": 50, "order_quantity": 100}`

* `edges` (list): 边配置。利用 Python 的动态列表生成能力，支持两种连接模式，可混合使用：
    * **模式 A（组级全连接）**：包含 `from_group` 和 `to_group`。例如 `{"from_group": "Central_DC", "to_group": "Retailer"}`。底层会自动将上游组内的所有实例与下游组内的所有实例建立全连接。
    * **模式 B（实例级精确连接）**：包含 `from` 和 `to`。值必须为精确的实例名称。例如 `{"from": "Factory_0", "to": "Distributor_1"}`。特别地，你可以通过 Python 的 `for` 循环和列表推导式生成，后续赋值进来，用于构建稀疏网络。


## 5. 事件契约域：`event_schema`

**数据类型**：字典 (`dict`)
**作用**：声明 `log_extractor` 会产生、且 `checker.py` 将要验证的所有合法事件类型及其字段结构。此字典将被底层框架用于执行强类型数据检查，并自动提取生成面向考生的测试说明书。
**结构规范**：
* 字典的**键**必须为事件的大写字符串名称（如 `"ORDER_PLACED"`，必须与 `log_extractor` 生成的 `event` 字段严格一致）。
* 字典的**值**是包含以下两项内容的字典：
    * `description` (str): 面向考生的自然语言，详细解释该事件发生的业务时机和物理意义。
    * `keys` (dict): 声明该事件中（除全局自带的 `time`, `node`, `event` 之外的）所有必需的业务字段。
        * `keys` 字典的键为字段名（如 `"qty"`）。
        * `keys` 字典的值是包含 `type` 和 `description` 的字典。
        * `type` (str): 仅限 `"int"`, `"float"`, `"str"`, `"bool"`, `"dict"`, `"list"`。
        * `description` (str): 面向考生解释该字段的含义与取值范围。

**示例**：
```python
event_schema = {
    "SHORTAGE_OCCURRED": {
        "description": "当节点面临下游需求但实物库存不足时触发。",
        "keys": {
            "product": {"type": "str", "description": "发生缺货的产品名称"},
            "missed_qty": {"type": "float", "description": "因为缺货未能满足的数量（需为正数）"}
        }
    }
}
```
注意：宽容判定机制生效。如果日志输出中包含了未在此字典中声明的其他事件，校验框架会自动忽略它而不会报错。



## 6. 自定义逻辑域：`custom_hooks` 

**作用**：用纯 Python 函数接管底层物理引擎逻辑。必须为每一个函数提供详尽的 `"""Docstring"""` 解释业务定律。

### 【API 1】外部需求生成：`demand_func`

* **入参**:
    * `period` (int): 当前系统处于第几天。
    * `product_id` (int): 当前正在计算需求的产品 ID（如 1 或 2）。
* **返回**: `float` (当日该节点该产品的外部基础需求量)。
* **示例**:
```python
def retailer_demand_func(period: int, product_id: int) -> float:
    if product_id == 1:
        return 20.0  # 手机每天面临 20 台需求
    return 0.0
```


### 【API 2】多源动态路由补货：`policy_func`

* **业务含义**：大模型在此拥有“上帝视角”。你可以根据网络中各产品的存货情况，决定同时向多个不同的供应商发出不同产品的采购指令。
* **入参**:
    * `period` (int): 当前天数。
    * `inventory_dict` (dict): 当前节点所有产品的账面库存字典。格式为 `{产品ID: 库存量}`，如 `{1: 45.0, 2: 10.0}`。
* **返回**:
    * **路由字典 (`dict`)**: 格式为 `{"目标上游名称": {产品ID: 订购数量}}`。
    * **目标名称规范**：如果向蓝图拓扑中建立过连线的真实内部上游订货，直接使用其带索引的语义名称（如 `"Central_DC_0"`）；如果向系统外部的市场/无尽供应商紧急采购，必须使用系统保留字 **`"EXTERNAL"`**。
* **示例**:
```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    orders = {}

    # 策略：产品 1 少于 50 台时，向内部中央仓采购
    ip_1 = inventory_dict.get(1, 0.0)
    if ip_1 <= 50:
        orders["Central_DC_0"] = {1: 150.0 - ip_1}

    # 策略：产品 2 严重缺货时，进行多源采购分摊风险
    ip_2 = inventory_dict.get(2, 0.0)
    if ip_2 < 20:
        qty = 50.0 - ip_2
        # 一半向内部中央仓采购
        if "Central_DC_0" not in orders: orders["Central_DC_0"] = {}
        orders["Central_DC_0"][2] = qty * 0.5
        # 另一半向外部市场紧急采购
        orders["EXTERNAL"] = {2: qty * 0.5}

    return orders

```

### 【API 3】动态仓储成本：`holding_cost_func`

* **入参**: `inventory_dict` (dict)。一个只包含**正向实物库存**的字典，格式为 `{产品ID: 库存量}`。
* **返回**: `float` (当期产生的仓储成本总金额)。
* **示例**:
```python
def retailer_holding_cost_func(inventory_dict: dict) -> float:
    cost = 0.0
    cost += inventory_dict.get(1, 0.0) * 2.5  # 手机仓储费 2.5/件
    cost += inventory_dict.get(2, 0.0) * 4.0  # 平板仓储费 4.0/件
    return cost

```

### 【API 4】动态缺货惩罚：`stockout_cost_func`

* **入参**: `shortage_dict` (dict)。一个只包含**缺货数量绝对值**的字典，格式为 `{产品ID: 缺货数量}`。如缺货 10 件，字典中值为 10.0。
* **返回**: `float` (当期产生的缺货惩罚总金额)。
* **示例**:
```python
def retailer_stockout_cost_func(shortage_dict: dict) -> float:
    cost = 0.0
    cost += shortage_dict.get(1, 0.0) * 100.0 # 手机缺货罚百
    cost += shortage_dict.get(2, 0.0) * 50.0  # 平板缺货罚五十
    return cost

```

### hook 挂载列表

最后提供一个hook挂载说明列表：
* **数据类型**：字典 (`dict`)
* **作用**：允许注入 Python 函数以覆盖指定“节点组”的默认逻辑。挂载后，该组下的**所有实例**都会自动生效。如果不需篡改，填空字典 `{}`。
* **必须包含的键**（格式皆为 `{"节点组名": callable}`）：

---

## 7. 状态净化与日志转换 (`log_extractor`)

**数据类型**：函数 (`callable`)
**作用**：将底层引擎产生的、经过实例名称净化后的状态变量字典，提取并转换为标准的事件日志列表。
**函数签名**：`def log_extractor(period: int, semantic_node_id: str, raw_state: dict) -> list[dict]`
**输入参数说明**：
* `period`: 当前运行的仿真天数。
* `semantic_node_id`: 当前处理的节点实例名称（字符串，由底层引擎基于“组名_索引”自动生成，如 `"Retailer_0"`, `"Central_DC_1"`）。
* `raw_state`: 底层引擎在这一天结束时该节点的全部状态数据。经过底层系统的净化处理，所有的数字节点 ID 都已被自动替换为**具体的语义实例名称**或 `"null"`（代表系统外部世界）。其结构严格如下：
    * `inventory_level` (dict): 实物库存。格式：`{"产品ID字符串": float}`。
    * `order_quantity` (dict): 向上游发出的采购量。格式：`{"目标实例名称 (或 'null')": {"产品ID字符串": float}}`。
    * `inbound_order` (dict): 收到下游发来的订单量。格式：`{"来源实例名称 (或 'null')": {"产品ID字符串": float}}`。
    * `outbound_shipment` (dict): 实际向下游发出的货物量。格式：`{"目标实例名称 (或 'null')": {"产品ID字符串": float}}`。
    * `inbound_shipment_pipeline` (dict): 在途物资数组。格式：`{"来源实例名称": {"产品ID字符串": [今天到达的量, 明天到达的量, 后天到达的量, ...]}}`。
    * `demand` (float): 当日面临的外部需求总量。
    * `fulfilled_demand` (float): 当日满足的外部需求总量。
    * `backorder` (float): 历史累计且尚未满足的缺货总量。
    * `holding_cost_incurred` (float): 当日仓储成本。
    * `stockout_cost_incurred` (float): 当日缺货惩罚成本。

**输出格式要求**：
* 必须返回一个包含字典的列表 `list[dict]`。如果当日无需要记录的事件，返回空列表 `[]`。
* 列表中的每个字典必须至少包含以下三个键：
* `time` (int): 必须等于传入的 `period`。
* `event` (str): 事件的大写名称（如 `"ORDER_PLACED"`, `"DAILY_SUMMARY"`）。
* `node` (str): 必须直接使用传入的 `semantic_node_id`（如 `"Retailer_0"`）。
* 其余键（如 `quantity`, `target`, `product_name`）可根据业务逻辑自由添加。但是格式一定要和event_schema一致。


## 8. 自动化裁判域：`tier2_checkers` 与 `tier3_checkers`

**数据类型**：列表 (`list[callable]`)
**作用**：定义针对转换后标准日志的正确性验证逻辑。`tier2_checkers` 用于检查单一实例或事件的局部逻辑合法性，`tier3_checkers` 用于检查全仿真周期的系统不变量（如跨越所有节点组的物质守恒定律）。
**列表中每个函数的签名**：`def check_xxx(logs: list[dict]) -> None`
**输入参数说明**：

* `logs`: 这是一个包含了单次仿真中所有天数、所有实例节点经过 `log_extractor` 处理后，按时间排序合并在一起的标准日志列表。

**行为规范**：

* 函数内必须使用 Python 的 `assert` 语句进行条件判断。
* 如果日志数据违反了物理定律或业务逻辑，必须执行 `assert False, "错误描述"` 抛出 `AssertionError`。
* 如果校验通过，函数无需返回任何值（即返回 `None`）。


## 9. 全局 KPI 提取函数：`extract_kpis`

**数据类型**：函数 (`callable`)
**作用**：从单次仿真的标准日志中提取出用于多轮运行对比的关键数值。
**函数签名**：`def extract_kpis(logs: list[dict]) -> dict[str, float]`
**输入参数说明**：

* `logs`: 包含了单次仿真所有信息的标准日志列表。格式与提供给检查器的参数完全一致。

**行为规范与输出要求**：

* 该函数内部严禁使用 `assert` 语句或抛出异常。
* 被允许且推荐在函数首部使用 `import kpi_utils`，调用提供的统计 SDK 工具（如 `kpi_utils.sum_metric`, `kpi_utils.count_events`）来简化代码编写。
* 必须通过遍历列表累加或统计数据，最终返回一个一维字典。
* 字典的键必须为字符串（KPI 的名称，如 `"total_system_cost"`，`"average_backorder"`），值必须为浮点数或整数。
* 该函数的返回值将交由底层测试套件汇总，用于生成 `config.json` 中的 Golden Data 分布区间，并在最终评测时与被测模型的同名 KPI 进行数学比对。



## 10. 评测驱动集：`test_cases`

**数据类型**：列表 (`list[dict]`)
**作用**：定义针对该场景的官方评测用例配置。外围管理脚本将根据此列表调度 `oracle_runner` 与被测模型。

* `case_name` (str): 用例标识。
* `runs` (int): 评测被测 LLM 时所需运行的次数（通常为 30-50）。
* `oracle_runs` (int): 生成分布金标准时所需运行的次数（用于逼近真实分布，通常为 500-1000）。
* `cli_kwargs` (dict): 该用例需传入的动态参数字典（键必须在 `cli_args_schema` 中）。
* `stdin_payload` (str): 运行时直接打入 `stdin` 的超长文本数据（若无需输入则为空字符串）。

## 11. 运行时全局注入变量：`DYNAMIC_ARGS` 与 `STDIN_DATA`

**作用**：提供接受外部大规模不确定性参数的能力，替代代码硬编码。

* `DYNAMIC_ARGS` (`dict[str, str]`): 运行时通过命令行传入的动态键值对。
* `STDIN_DATA` (`str`): 运行时通过系统管道截获的长序列文本数据。

> ⚠️ **极其重要的作用域警告（Strict Scope Limitation）**
> 这两个全局变量的注入**仅在 `oracle_runner.py` 生成标准数据的生命周期内发生**。在评测阶段，目标 LLM 并无此全局变量，且裁判脚本 (`checker.py`) 在导入蓝图时也不会注入它们。
> **强制约束**：
> 1. 出题 LLM **绝对不能**在 `tier2_checkers`、`tier3_checkers` 或 `extract_kpis` 中调用这俩全局变量！
> 2. 它们的作用域被严格封印，**仅允许在 `custom_hooks` 中的自定义动态函数**（如需要根据每天的参数动态改变需求的 `demand_func`）中被访问解析。
> 
> 

```python
# 正确使用示例 (仅用于 custom_hooks 逻辑)：
parsed_demand_sequence = [float(x) for x in STDIN_DATA.split()] if 'STDIN_DATA' in globals() and STDIN_DATA else [] # 后面这个if判断十分重要，防止其他时候import出现报错。

def custom_demand_func(period):
    if period - 1 < len(parsed_demand_sequence):
        return parsed_demand_sequence[period - 1]
    return float(DYNAMIC_ARGS.get("default_demand", 10.0)) if 'DYNAMIC_ARGS' in globals() else 10.0

```