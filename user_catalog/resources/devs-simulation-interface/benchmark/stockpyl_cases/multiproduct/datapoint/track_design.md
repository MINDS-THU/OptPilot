

# 🏆 复杂系统仿真 Benchmark：核心架构与迁移指南

## 一、 核心设计哲学 (The Philosophy)

本架构的底层逻辑是 **“考教分离”** 与 **“文档即代码（Docs as Code）”**。
我们绝对不让出题大模型直接去写 `run.py` 或 `checker.py`。大模型只负责配置“物理沙盘规则”，其余的所有执行、调度、判卷工作，全部交由高度工程化的底层脚手架接管。

## 二、 核心五大组件 (The 5 Pillars)

整个系统由一个中枢和四个自动化脚手架组成。**在迁移到新赛道时，只需替换底层的物理引擎适配器，这五大组件的协作关系永远不变。**

### 1. 中枢大脑：`scenario_blueprint.py` (蓝图契约)

这是贯穿全局的唯一数据源（Single Source of Truth）。

* **静态声明**：拓扑结构、支持的命令行参数、合法的事件 Schema 格式。
* **动态钩子**：用 Python 函数覆写底层物理逻辑（并附带 Docstring 说明）。
* **校验逻辑**：单次局部断言（Tier 2）、单次全局断言（Tier 3）、多轮 KPI 提取逻辑。
* **测试用例**：定义 Benchmark 需要跑的具体参数矩阵和 stdin 数据流。

### 2. 制卷机：`build_description.py` (考卷生成器)

* **职责**：提取蓝图中的拓扑配置和事件 Schema，通过反射（Reflection）抓取动态逻辑的 Docstring，并调用 LLM 润色。
* **产出**：输出完美格式化的 `description.yaml`，作为给被测 LLM 唯一的开发参考文档。

### 3. 纯粹执行器：`oracle_runner.py` (标答引擎)

* **职责**：一个绝对纯粹的无状态管道工具。它加载蓝图，将其翻译为底层物理引擎（如 `stockpyl` 或 `Ciw`）的对象。
* **特性**：执行**底层 ID 语义化清洗**（将框架内部恶心的数字 ID 替换为 `Retailer_0` 这种人类可读的实例名）。
* **产出**：只向标准输出 (`stdout`) 打印纯净的 JSONL 标准日志。

### 4. 流水线管家：`build_test_suite.py` (测试集装配机)

* **职责**：读取蓝图里的 `test_cases`，多线程疯狂拉起 `oracle_runner.py` 执行数百次压测。
* **产出 1**：提取并保存数百次运行结果的巨型**分布金标准文件** (`*_golden.json`)。
* **产出 2**：组装并生成评测平台唯一认的运行配置文件 `config.json`。

### 5. 铁面裁判：`checker.py` (四层防御判卷机)

* **职责**：加载被测模型生成的日志，通过四大维度执行无情裁决。

---

## 三、 四层防御判卷体系 (The 4-Tier Defense)

这是本架构最核心的学术/工程创新，用于彻底锁死 LLM 的幻觉空间。

* **Tier 1: 强类型契约排雷 (`LOG_FORMAT_CORRECTNESS`)**
* **机制**：宽容无关废话，但一旦命中目标事件，执行严格的 JSON Schema 键存在性与强类型校验。
* **意义**：提前拦下脏数据，保护后续物理断言不崩溃。


* **Tier 2: 局部物理定律 (`COMPONENT_LEVEL`)**
* **机制**：执行蓝图中的单点物理断言（如“库存不能为负”、“排队时间不能小于0”）。
* **意义**：捕获组件级的微观幻觉。


* **Tier 3: 全局系统守恒 (`SYSTEM_LEVEL`)**
* **机制**：执行系统级不变量检查（如“全网发出的货物 = 收到的货物 + 在途货物”）。
* **意义**：捕获跨组件协作时的宏观幻觉。


* **Tier 4: 宏观分布对齐 (`KPI_SATISFACTION`)**
* **机制**：放弃单次均值比较。采用 **Wasserstein Distance (推土机距离 / EMD)** 算法，衡量目标 LLM 的多轮运行分布与 Oracle 巨量样本分布之间的搬运代价。
* **意义**：完美兼容离散事件仿真中**合理但不精确的随机游走波动**，极其精准地识别根本性的物理分布漂移。



---

## 四、 数据流转桥接机制 (Data Bridge)

单次仿真日志无法包含多轮运行的宏观信息。系统通过巧妙的桥接完成上下文聚合：

1. `validate_logic` 阶段处理单次日志，调用蓝图的 `extract_kpis` 提取指标，写入 `self.stats`。
2. 底层框架自动收集所有 `stats` 打包为 `batch_stats` 列表。
3. `validate_kpis` (Tier 4) 从 `batch_stats` 中抽出特定指标聚合成数组，与外部 JSON 文件读取的 Golden Data 执行 SciPy 距离计算。

---

## 五、 赛道迁移指南：如何适配新领域？

当你准备将这套架构迁移到下一个赛道（例如**赛道二：排队论网络 Ciw**）时，你**不需要修改任何顶层逻辑**，只需要做以下“换壳”操作：

1. **改写底层执行器 (`oracle_runner.py`)**：
* 将 `import stockpyl` 替换为 `import ciw`。
* 将 `build_network` 方法中的逻辑，改为解析蓝图的组节点，生成 Ciw 的服务台（Servers）、路由矩阵（Routing Matrix）和到达率（Arrival Distributions）。


2. **重新定义领域元数据 (`scenario_blueprint.py` 模板)**：
* 拓扑中的 `role` 从 `distributor` 变为 `M/M/1_Queue` 等。
* `event_schema` 从 `ORDER_PLACED` 变为 `CUSTOMER_ARRIVAL`, `SERVICE_START`, `SERVICE_END`。


3. **调整物理定律 (Tier 2/3)**：
* 守恒定律从“库存转移”变为“利特尔法则 (Little's Law)”（系统平均人数 = 到达率 × 平均逗留时间）或“顾客数量守恒”。
