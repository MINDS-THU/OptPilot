# DES 仿真大模型评测基准：数据点构建标准规范

为了保证大语言模型（LLM）在离散事件仿真（DES）代码生成任务中的评测稳定性、可解释性以及场景的可扩展性，每个评测数据点必须严格遵循本规范。

## 核心交付物

一个标准的数据点目录必须包含以下三个核心文件。所有评测流水线与大模型 Agent 的交互均以此三者为唯一边界：

1. **`description.yaml`**：场景契约，提供给 LLM 侧的唯一 Prompt 来源。
2. **`config.json`**：运行矩阵，定义单次运行参数与全局判定阈值。
3. **`checker.py`**：评分脚本，基于四层防御体系的自动化裁判。

---

## 一、 `description.yaml` (场景契约规范)

该文件是 LLM 生成仿真代码的唯一依据。**必须严格切分为三个不可互相越界的区块**，确保“业务定律”与“代码实现”解耦。

### 1.1 分区规范

* **`general` (环境与客观限制)**
    * **必须包含**：运行语言、可用标准库、入口点规范。
    * **致命底线**：**必须显式声明全局统一的时间单位（强力推荐毫秒）**，以防模型在 `timeout` 和 `sys.stdin` 解析时产生幻觉。
    * **禁区**：严禁出现业务特定名词（如节点、订单、拓扑）。
* **`scenario` (业务物理定律 - 声明式)**
    * **必须包含**：系统目标、拓扑结构、实体行为定律（如状态转移、容量限制、时间延迟）。
    * **禁区**：严禁指导模型如何设计类、函数或继承关系。仅陈述“现实世界的规则”。
* **`args_input_output` (I/O 契约)**
    * **必须包含**：CLI 参数格式、`stdin` 格式、`stdout` 必填的 JSONL Schema。
    * **防御性底线**：必须明确声明**“允许输出额外调试日志和冗余字段，评测器会自动过滤”**，防止模型因防御性编程而违背 Prompt。

### 1.2 模板示例

```yaml
root_model_name: SupplyChain_Inventory_Node
requirements: 
  general: |
    ### General Implementation Requirements
    1. Language & Environment: Python 3.10+, using standard libraries and `simpy`/`xdevs`.
    2. Input Interface: Use `argparse` for CLI. Read dynamic input from `sys.stdin` line-by-line.
    3. Output Interface: 
       - sys.stdout: MUST print JSONL objects containing required events.
       - sys.stderr: Print any debug/progress logs here.
    4. Time Unit: The underlying simulation base time unit is strictly MILLISECONDS. All delays mentioned in seconds (e.g., "10s") MUST be converted to milliseconds (e.g., 10000) for simulation timeouts.

  scenario: |
    1. System Objective: Simulate a single inventory node with an (s, S) replenishment policy.
    2. Entity Behaviors:
       - Customer Demand: Arrives following a Poisson process (lambda=10 units/hour).
       - Inventory Policy: If current_inventory <= s (reorder point), order (S - current_inventory) units.
       - Lead Time: Orders arrive after exactly 48 hours.

  args_input_output: |
    1. CLI Arguments: MUST accept `--simulation_time` (float, in milliseconds) and `--seed` (int).
    2. Stdout JSON Schema:
       You MUST output the following Required Event Types. You MAY output additional event types or add extra fields to the required events for debugging. The evaluator will strictly filter and grade ONLY the required types and fields.
       
       Required Events:
       - `{"type": "demand_arrival", "val": {"quantity": <int>}}`
       - `{"type": "replenishment_received", "val": {"quantity": <int>}}`
```

---


## 二、 `config.json` (运行配置矩阵)

该文件定义了仿真系统的运行参数矩阵、输入数据以及对应的评测阈值。其根节点必须是一个 JSON 数组（`List[Dict]`），数组中的每一个对象代表一个独立的数据条目（测试集）。

**执行生命周期说明：** 评测流水线会依次处理每个数据条目。对于每个条目，系统会先执行内部定义的所有 `cases`（仿真运行），收集所有的输出日志。**待该条目下所有仿真结束后，系统只会调用一次 `checker.py`**，将所有日志文件路径和全局判断标准传入，进行最终的综合打分。

### 2.1 数据条目字段规范 (Data Entry)

每个数据条目字典必须遵循以下字段契约：

| 字段名 | 类型 | 必填 | 描述与使用规范 |
| :--- | :--- | :--- | :--- |
| `name` | `str` | 是 | 数据条目的标识名称。 |
| `description` | `str` | 否 | 自然语言描述，仅供人类开发者阅读和维护使用。 |
| `sim_timeout` | `float` | 否 | （全局兜底）该数据条目下所有仿真执行的默认最大超时时间（秒）。 |
| `checker_args` | `dict` | 是 | **全局评判参数**。作为命令行命名参数传递给 `checker.py`（服务于 `KPI_SATISFACTION` 层级规则）。**对于大型标准分布对比（Golden Data），请勿在此硬编码数据，应提供文件的相对路径**（例如 `"golden_data_path": "golden_data/dist.json"`），由 Checker 自行读取。 |
| `cases` | `list[dict]` | 是 | 仿真执行实例的定义列表。每个 dict 代表一次特定参数下的仿真配置。 |

### 2.2 执行实例字段规范 (Cases)

在 `cases` 列表中的每一个字典，定义了单次仿真的具体上下文：

| 字段名 | 类型 | 必填 | 描述与使用规范 |
| :--- | :--- | :--- | :--- |
| `num` | `int` | 是 | 使用当前 case 配置重复执行仿真的次数（通常配合随机种子使用，若种子固定则通常设为 1）。 |
| `sim_timeout` | `float`| 否 | （单点覆盖）覆盖外层的默认超时时间，针对本次运行的特定超时设置（秒）。 |
| `sim_args` | `dict` | 是 | 传递给仿真器（`run.py`）的命令行参数（如 `--seed`, `--simulation_time` 等）。 |
| `sim_stdin` | `str` | 否 | 传递给仿真器标准输入（`stdin`）的字符串内容。 |
| `sim_stdin_file` | `str` | 否 | 指定一个包含 `stdin` 内容的文件路径（相对于 `config.json` 所在目录）。**注意：** 如果存在此字段，将忽略 `sim_stdin` 字段。 |
| `checker_config` | `dict` | 是 | **单次运行专属预期**。通过 Sidecar 模式注入单次运行上下文。**仅存放**与本次输入参数严格对应的确定性预期（服务于 `COMPONENT_LEVEL` 等层级）。 |
| `checker_extra_file` | `str` | 否 | 指定一个包含额外校验规则的文件路径（相对于 `config.json` 目录，必须为 JSON）。用于规避 `checker_config` 体积过大的问题（如巨型对照表）。 |

### 2.3 模板示例 (包含 Golden Data 路径模式)

```json
[
  {
    "name": "Inventory_sS_Policy_Distribution",
    "description": "测试多次运行后的 KPI 分布是否符合预先生成的 Golden Data",
    "checker_args": {
      "golden_data_path": "golden_data/inventory_20d_distribution.json",
      "ks_p_value_threshold": 0.05,
      "min_valid_runs": 8
    },
    "cases": [
      {
        "num": 1,
        "sim_timeout": 15.0,
        "sim_args": {
          "--simulation_time": 2592000000,
          "--seed": 42
        },
        "sim_stdin_file": "inputs/baseline_demand.txt",
        "checker_config": {
          "expected_initial_order_qty": 150
        }
      },
      {
        "num": 1,
        "sim_timeout": 15.0,
        "sim_args": {
          "--simulation_time": 2592000000,
          "--seed": 1024
        },
        "sim_stdin_file": "inputs/baseline_demand.txt",
        "checker_config": {
          "expected_initial_order_qty": 120
        }
      }
    ]
  }
]
```
---


## 三、 `checker.py` (四层防御评分体系与实现规范)

`checker.py` 是评测的核心裁判，必须基于 `checker_utils.py` 框架（`BaseValidator`）开发。评测逻辑必须严格封印在四个不可僭越的层级中，并通过标准化的上下文接口读取配置。

### 3.1 配置参数的读取与映射 (Context Mapping)

`config.json` 中定义的复杂参数，无需开发者手动解析文件，`checker_utils.py` 会在生命周期中自动将其映射为类的属性，供开发者在对应的方法中直接使用：

| `config.json` 来源 | `checker.py` 中的访问方式 | 作用域与推荐使用场景 |
| :--- | :--- | :--- |
| `checker_args` | `self.global_config` | **全局作用域**。通常在 `validate_kpis()` 中读取，用于获取判定多次运行分布的宏观阈值（如 `ks_threshold`, `min_success_rate`）。 |
| `cases -> checker_config` | `self.checker_config` | **单次运行作用域**。在 `validate_logic()` 中读取。用于获取**本次仿真**的确定性预期（如特定 seed 下的预期订单量）。 |
| `cases -> sim_args` | `self.sim_args` | **单次运行作用域**。在 `validate_logic()` 中读取。用于获知本次仿真的输入参数（如获知当前的 `simulation_time` 以验证日志时间戳是否越界）。 |
| `cases -> sim_stdin` | `self.sim_stdin` | **单次运行作用域**。在 `validate_logic()` 中读取。当需要验证模型是否正确响应了动态输入时使用。 |
| `cases -> checker_extra_file`| `self.current_extra` | **单次运行作用域**。在 `validate_logic()` 中读取。用于加载超大型的对照表或复杂的 JSON 规则树。 |

### 3.2 评分四层体系 (The 4-Tier Logic)

为了精准定位模型的错误类型，所有注册的规则（`self.register_rule`）必须归属于以下四层之一：

| 层级 | 名称 | 定位与检查内容 | 作用域 | 推荐计分法 |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1** | `LOG_FORMAT_CORRECTNESS` (格式正确性) | **排雷**：模型是否遵循了 JSONL 输出契约，目标事件名和必填字段的类型是否合法。**必须宽容未知字段与无关日志。** | 单次运行 | `BINARY` (错一票否决) |
| **Tier 2** | `COMPONENT_LEVEL` (局部物理定律) | **抓过程幻觉**：局部组件的状态转移是否合法（例如：某节点的`当前库存 == 初始 + 入库 - 出库`，且任何时刻 `库存 >= 0`）。 | 单次运行 | `RATIO` 或 `BINARY` |
| **Tier 3** | `SYSTEM_LEVEL` (单次全局不变量) | **抓宏观幻觉**：在单次仿真结束时，跨组件的资源是否守恒（例如：系统发包总数 == 收包总数 + 丢包总数 + 在途总数）。 | 单次运行 | `RELATIVE_ERROR` |
| **Tier 4** | `KPI_SATISFACTION` (多 Run 全局分布) | **终极兜底**：基于多个随机种子下的运行结果，检验系统的核心 KPI（均值、方差）或执行 KS 分布检验，判断是否符合预期。 | 跨越所有运行 | `THRESHOLD` |

### 3.3 跨运行数据传递机制 (Data Bridge)

**如何让 Tier 4 知道前面每次运行的结果？** Tier 4 的逻辑写在 `validate_kpis(self, batch_stats)` 中，它不应该再去重新解析原始日志。数据的流转依靠 `self.stats` 字典完成：

1. **写入（在单次运行中）：** 在 `validate_logic()` 遍历解析某次日志时，除了做 Tier 1-3 的规则校验，还应将**提取出的关键业务指标**（如本次的吞吐量、总延迟）写入 `self.stats['xxx']`。
2. **自动打包：** 单次运行结束时，框架会自动深拷贝 `self.stats`，连同 `self.current_meta` 一起塞入大列表 `batch_stats` 中。
3. **读取（在全局统计中）：** 在 `validate_kpis(batch_stats)` 中，直接遍历 `batch_stats` 提取之前存入的指标，进行数学统计或分布检验。

### 3.4 代码结构模板

```python
from checker_utils import BaseValidator, RuleType, ScoringMethod
import sys

class SimulationChecker(BaseValidator):
    def define_rules(self):
        # Tier 1 & 2: 格式与组件物理定律
        self.register_rule("format_check", "日志Schema校验", RuleType.LOG_FORMAT_CORRECTNESS, scoring_method=ScoringMethod.BINARY)
        self.register_rule("inventory_conservation", "库存转移守恒", RuleType.COMPONENT_LEVEL, scoring_method=ScoringMethod.RATIO)
        
        # Tier 3: 单次全局不变量
        self.register_rule("system_balance", "全系统包裹守恒", RuleType.SYSTEM_LEVEL, scoring_method=ScoringMethod.BINARY)
        
        # Tier 4: 全局 KPI 兜底 (注意: type 必须为 MULTIPLE_RUN)
        self.register_rule("kpi_backlog", "平均缺货量分布", RuleType.MULTIPLE_RUN, scoring_method=ScoringMethod.THRESHOLD)

    def validate_logic(self):
        """Tier 1 到 Tier 3 的单次运行校验 (每次仿真调用一次)"""
        comp_rule = self.rules['inventory_conservation']
        sys_rule = self.rules['system_balance']
        
        # 读取 config.json -> cases -> checker_config 中的单次预期
        expected_qty = self.checker_config.get("expected_initial_order_qty") 
        
        total_sent = 0
        total_received = 0
        run_total_backlog = 0 # 准备提取的 KPI 原始数据

        for entry in self.logs:
            if entry.get("type") not in ["demand_arrival", "packet_sent", "packet_received"]:
                continue
                
            # 执行 Tier 2 组件定律校验
            if entry["type"] == "demand_arrival":
                comp_rule.add_case(is_correct=(entry["val"]["quantity"] >= 0))
            
            # 收集用于 Tier 3 和 Tier 4 的数据
            if entry["type"] == "packet_sent":
                total_sent += 1
            if entry["type"] == "packet_received":
                total_received += 1

        # 执行 Tier 3 宏观不变量校验 (单次运行维度)
        sys_rule.add_case(is_correct=(total_sent >= total_received))

        # 【跨层级数据传递】: 将本 Run 的统计值写入 self.stats，供 Tier 4 使用
        self.stats['run_backlog'] = run_total_backlog
        self.stats['run_sent'] = total_sent

    def validate_kpis(self, batch_stats):
        """Tier 4 跨运行全局兜底校验 (所有仿真结束后调用一次)"""
        kpi_rule = self.rules['kpi_backlog']
        
        # 读取 config.json -> checker_args 中的全局参数
        expected_avg = self.global_config.get('expected_avg_backlog', 0)
        margin = self.global_config.get('backlog_tolerance_margin', 0)
        golden_path = self.global_config.get('golden_data_path')
        
        # 从 batch_stats 中提取之前所有 run 存入的 'run_backlog'
        if not batch_stats:
            kpi_rule.add_error("没有收集到任何有效运行数据")
            return
            
        actual_avg = sum(run['run_backlog'] for run in batch_stats) / len(batch_stats)
        
        # 执行 KPI 判定
        if abs(actual_avg - expected_avg) <= margin:
             kpi_rule.add_case(is_correct=True)
        else:
             kpi_rule.add_error(f"KPI 异常: 期望 {expected_avg}, 实际 {actual_avg}")
             kpi_rule.add_case(is_correct=False)

if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("log_files", nargs="+")
    # 此处的 args 会自动接收 config.json -> checker_args 传入的所有命名参数
    parser.add_argument("--expected_avg_backlog", type=float)
    parser.add_argument("--backlog_tolerance_margin", type=float)
    parser.add_argument("--golden_data_path", type=str)
    
    args, unknown = parser.parse_known_args()
    validator = SimulationChecker(args.log_files, vars(args))
    print(json.dumps(validator.run(), ensure_ascii=False))
```