# 裁判执行引擎 (Checker) 架构说明书

`checker.py` 是评测套件的核心判卷中枢。它严格基于四层防御体系（The 4-Tier Logic）运作，并通过动态桥接的方式，利用出题大模型生成的 `scenario_blueprint.py` 对目标代码产生的输出进行无情裁决。

## 一、 判分机制与四层防御体系

裁判脚本不包含任何业务硬编码，通过四个连续的维度对目标输出施加压力测试：

### 1. Tier 1: 强类型 Schema 格式校验 (`LOG_FORMAT_CORRECTNESS`)
* **原理**：读取蓝图文件中的 `event_schema` 字典。
* **行为**：宽容无关日志。但一旦目标事件名称匹配，将执行严格的键存在性与数据类型校验（如 `qty` 必须为 `float`）。类型错误将直接打断该轮判定，防止脏数据引发底层框架崩溃。

### 2. Tier 2 & 3: 物理逻辑校验 (`COMPONENT_LEVEL` & `SYSTEM_LEVEL`)
* **原理**：挂载蓝图中的 `tier2_checkers` (局部组件逻辑) 和 `tier3_checkers` (全局不变量与物质守恒)。
* **行为**：通过捕获 `AssertionError`，判断大模型生成的仿真是否违反底层物理定律（如全系统包裹凭空消失）。

### 3. Tier 4: 分布相似性兜底校验 (`KPI_SATISFACTION`)
* **原理**：采用 **Wasserstein Distance (推土机距离 / EMD)** 算法，检验目标 LLM 的运行结果与 Oracle 基准结果在宏观物理分布上的对齐度。
* **行为**：
    * 从 `self.global_config` 中获取 `golden_data_path`，**自行加载外部庞大的 JSON 文件以防止内存溢出或命令行参数超限**。
    * 提取目标模型的跨运行 KPI 数组。
    * 若计算所得的搬运代价（EMD）大于允许容差（如基准均值的 5%），则判定分布失真（判定为 `False`）。这完美兼容了离散仿真中合理的随机游走现象。

## 二、 输入接口与 Context Mapping

`checker.py` 的外部参数严格映射自 `config.json`，且所有未知的 `--` 命名参数将被自动捕获至 `self.global_config`。

**评测环境启动命令示例：**
```bash
# 外围脚本传入 log 路径，并通过 --golden_data_path 指定基准大文件的位置
python checker.py \
    run_0.jsonl run_1.jsonl run_2.jsonl ... \
    --golden_data_path outputs/inventory_20d_golden.json \
    --kpi_tolerance_margin 0.05
```

**run_details[].meta 中的 Seed 追溯**：对于随机场景，每个 run 的 `run_details[].meta.sim_args["--seed"]` 会记录该次执行使用的种子值，便于回溯 KPI 分布异常时确认种子是否正确递增。

**run_details[].meta 中的 Seed 追溯**：对于随机场景，每个 run 的 `run_details[].meta.sim_args["--seed"]` 会记录该次执行使用的种子值，便于回溯 KPI 分布异常时确认种子是否正确递增。