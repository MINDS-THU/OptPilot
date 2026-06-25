# DEVS-GEN Benchmark 评估计划与实施方案

**基于 REALM-Bench 与 OptiGuide 的外部验证路线**

2026年5月

---

## 目录

- [一、背景与目标](#一背景与目标)
  - [1.1 我们的工具流](#11-我们的工具流)
  - [1.2 对 Benchmark 的核心需求](#12-对-benchmark-的核心需求)
- [二、REALM-Bench 详细介绍](#二realm-bench-详细介绍)
  - [2.1 基本信息](#21-基本信息)
  - [2.2 已有资产](#22-已有资产)
  - [2.3 适合 DEVS 的场景](#23-适合-devs-的场景)
  - [2.4 使用计划](#24-使用计划)
- [三、OptiGuide What-if 详细介绍](#三optiguide-what-if-详细介绍)
  - [3.1 基本信息](#31-基本信息)
  - [3.2 已有资产](#32-已有资产)
  - [3.3 使用计划](#33-使用计划)
- [四、对比总结与实施路线图](#四对比总结与实施路线图)

---

## 一、背景与目标

### 1.1 我们的工具流

我们开发的 **DEVS-GEN** 是一个基于自然语言描述生成可执行 DEVS（Discrete Event System Specification）世界模型的流程。其核心目标是让 LLM Agent 能够通过外部仿真工具进行可靠的"what-if"推理与规划决策。

具体使用流程包含**两个工具**：

- **工具一 —— 建模工具**：LLM 提供自然语言场景描述，工具后台生成可执行的 DEVS 模型
- **工具二 —— 仿真运行工具**：LLM 根据建好的模型，提供输入参数，运行仿真并把结果返回用于推理

### 1.2 对 Benchmark 的核心需求

为验证该流程的有效性，我们需要寻找现成的、可复用的评估基准。对于任何候选 Benchmark，必须满足**三个核心约束**：

**必须开源**：否则无法重现评估流程。

**参数明确、动态模式清晰**：因为我们的 DEVS 建模工具需要足够充分的系统参数才能准确建模，模糊或不完整的信息会导致建模失败。

**支持规划或 what-if 推理任务**：不仅限于问答，而是能让 LLM 通过外部工具辅助完成决策或分析任务。

---

## 二、REALM-Bench 详细介绍

### 2.1 基本信息

| 属性 | 内容 |
|------|------|
| 论文标题 | REALM-Bench: A Real-World Planning Benchmark for LLMs and Multi-Agent Systems |
| 发表场地 | **KDD 2026**（CCF-A / 核心会议） |
| 机构 | Stanford University（另有 Freisinger 等多合作者） |
| GitHub | https://github.com/genglongling/REALM-Bench（已开源，40+ stars） |
| 许可证 | MIT License |
| 评估范式 | 多 Agent 协作解决实际规划问题，有明确的评估指标体系 |

REALM-Bench 的核心设计理念是"从世界中来"：它不是构造虚构的问答对，而是提供具有**明确参数定义的实际规划问题**，让 LLM 通过多轮推理和工具调用来构建解决方案。

### 2.2 已有资产

#### 2.2.1 场景库（14 个问题域）

REALM-Bench 提供了 P1-P11 和 J1-J4 共 14 个问题域，涵盖了从简单路由到复杂供应链的多种场景。与我们 DEVS 工具最相关的场景包括：

| 场景编号 | 名称 | 类型 | DEVS 适配度 |
|----------|------|------|-------------|
| P3 / P4 | Urban Ride-Sharing | 网约车调度 | 高 |
| P10 | GPU Supply Chain | 供应链规划 | 高 |
| P11 / J1-J4 | Job Shop Scheduling | 作业车间调度 | **极高** |
| P7 | Disaster Relief | 灾后救援资源分配 | 中 |
| P1-P2 | Campus Tour | 路由规划 | 中 |

#### 2.2.2 数据格式与参数结构

每个场景的实例以 JSON 或标准文本格式存储，**参数极其明确**。以 P3（网约车）为例，`p3_instance_001.json` 包含：

- `city_map`：8 个地点的完整距离矩阵（正反向均有）
- `vehicles`：车辆容量、当前位置、剩余油量
- `ride_requests`：乘客上车点、下车点、时间窗口

以 J1（JSSP）为例，文件格式为标准的 `(machine_id, processing_time)` 对，第一行为 `"20 15"` 表示 20 个作业、15 台机器。

#### 2.2.3 评估框架与 Agent 支持

REALM-Bench 提供了完整的评估基础设施：

- **评估指标**：Plan Quality、Optimality、Constraint Satisfaction、Resource Usage、Adaptation to Disruptions
- **Agent 框架**：内置 LangGraph、AutoGen、CrewAI、OpenAI Swarm 4 种主流多 Agent 框架的集成示例
- **Tool Agent 模块**：`src/tool_agent` 目录支持 LLM 调用外部工具（SQL 查询、数据分析等）

### 2.3 适合 DEVS 的场景

| 场景 | 关键 DEVS 要素 | 使用方式 |
|------|---------------|----------|
| **JSSP** | 作业到达、排队、机器加工、完工 | 仿真不同调度策略的表现 |
| **网约车** | 乘客到达、车辆移动、服务完成 | 仿真不同路线方案的效率 |
| **供应链** | 订单到达、库存变化、运输延迟 | 仿真 what-if 情景下的成本变化 |

### 2.4 使用计划

#### 2.4.1 整体流程

对于每个选定的 REALM-Bench 场景，使用流程如下：

1. **场景描述生成**：将 JSON / 文本实例转换为自然语言描述（需为每种场景类型写模板）
2. **DEVS 建模**：LLM 调用工具一，生成 DEVS 模型
3. **规划方案生成**：LLM 基于模型结构，提出候选调度 / 分配方案
4. **仿真验证**：LLM 调用工具二，对每个方案运行 DEVS 仿真，获取 KPI
5. **方案选择**：LLM 比较仿真结果，选择最优方案
6. **评估对比**：用 REALM-Bench 的评估脚本对比我们的结果与基线

#### 2.4.2 具体任务示例

**JSSP 场景**："一个有 15 台机器的车间，20 个作业各有 3-5 道工序，每道工序的加工时间和可用机器已知。请优化调度方案使总完工时间最短。"

**网约车场景**："城市有 8 个区域，5 辆车，10 个乘客订单。请安排车辆路线使总行驶时间最短。"

**供应链场景**："GPU 供应链有 3 个供应商、5 种组件、2 个组装设施，预算限制为 X。请制定采购计划使成本最低。"

#### 2.4.3 需要补充的工作

- 为每种场景类型编写自然语言描述模板（约 1-2 天 / 场景）
- 集成 REALM-Bench 的评估脚本到我们的流程中
- 从多 Agent 设计适配为单 LLM + 外部工具

---

## 三、OptiGuide What-if 详细介绍

### 3.1 基本信息

| 属性 | 内容 |
|------|------|
| 论文标题 | OptiGuide: Large Language Models for Supply Chain Optimization |
| 机构 | Microsoft Research（Beibin Li, Hongseok Namkoong 等） |
| GitHub | https://github.com/microsoft/OptiGuide（已开源，600+ stars） |
| 许可证 | MIT License |
| 评估范式 | 自然语言 What-if 问题，有明确的 Ground Truth 结果 |

OptiGuide 是微软研究院开发的供应链优化框架，其 **What-if Benchmark** 是我们调研中发现的**唯一一个**"拥有现成自然语言问题集 + 明确 Ground Truth" 的 Benchmark。

### 3.2 已有资产

#### 3.2.1 What-if 问题集

OptiGuide 提供了 6 个场景，每个场景有 100-500 个 what-if 问题，以 JSON 格式存储。

| 场景 | 描述 | 问题数量 | 适配度 |
|------|------|----------|--------|
| **Coffee** | 咖啡分销网络 | ~500 | 高 |
| **Facility** | 设施选址问题 | ~200 | 中 |
| **Netflow** | 多商品网络流 | ~300 | 高 |
| **TSP** | 旅行商路径 | ~200 | 中 |
| **Workforce** | 人员排班与分配 | ~150 | 中 |
| **Diet** | 饮食搭配优化 | ~100 | 低 |

#### 3.2.2 JSON 数据格式

每个问题以下面的 JSON 结构存储，**可以直接被我们的流程复用**：

```json
{
  "QUESTION": "What is the impact of a 29% demand increase at cafe2?",
  "VALUE-CAFE": "cafe2",
  "VALUE-NUMBER": 29,
  "DATA CODE": "light_coffee_needed_for_cafe['cafe2'] *= 1.29\ndark_coffee_needed_for_cafe['cafe2'] *= 1.29",
  "TYPE": "demand-increase",
  "GT EXEC RESULT": 2612.0
}
```

- `QUESTION` 字段可直接作为 LLM 的输入
- `DATA CODE` 提供了参数修改的精确描述
- `GT EXEC RESULT` 提供了评估基准

#### 3.2.3 问题类型分布

问题按类型分类，涵盖供应链中常见的 what-if 情景：

- `demand-increase`：某个节点需求增加 X%
- `demand-decrease`：某个节点需求减少 X%
- `supplier-change`：供应商容量或成本变化
- `edge-failure`：某条运输路径中断

### 3.3 使用计划

#### 3.3.1 整体流程

OptiGuide 的使用流程与原始设计有所不同 —— 原始流程使用 **MILP 优化求解**，我们将替换为 **DEVS 仿真推理**：

1. **场景描述**：从 OptiGuide 应用代码中提取每个场景的自然语言描述（含供应商、客户、路线、成本等参数）
2. **DEVS 建模**：LLM 调用工具一，将供应链场景建模为 DEVS 离散事件网络
3. **What-if 实验设计**：对每个问题，LLM 设计两组实验 —— baseline（当前参数）和 what-if（修改后参数）
4. **DEVS 仿真运行**：LLM 调用工具二，分别运行两组实验，获取时序 KPI 分布
5. **推理回答**：LLM 比较两组结果，回答"如果...会怎样"的问题
6. **评估**：将我们的仿真结果与 OptiGuide 的 GT EXEC RESULT 对比（允许合理误差）

#### 3.3.2 具体任务示例

**Coffee 场景**："某咖啡馆需求增加 29%，这对总成本和物流路径有什么影响？"

**Facility 场景**："如果在城市 B 新建一个仓库，总运输成本会降低多少？"

**Netflow 场景**："如果从供应商 A 到仓库 C 的路径中断，需要重新路由多少流量？"

#### 3.3.3 需要补充的工作

- 从 OptiGuide 应用代码中提取场景的自然语言描述（场景参数已在代码中，需重构为文本）
- 将 MILP 优化评估适配为 DEVS 仿真评估（定义新的对比指标）

---

## 四、对比总结与实施路线图

### 核心特征对比

| 维度 | REALM-Bench | OptiGuide |
|------|-------------|-----------|
| **核心任务** | 多场景规划与调度 | What-if 分析 |
| **自然语言问题** | 需自己写模板 | 现成 JSON 问题集 |
| **评估框架** | 完整的评估脚本 | 只有 Ground Truth 数值 |
| **Agent 框架** | 内置 4 种多 Agent 框架 | 无，需自己构建 |
| **DEVS 适配难度** | 中（需写描述模板） | 中（需适配评估方式） |
| **拿来就用** | 需适配工作 | 接近可用（问题集现成） |

### 推荐实施路线

| 阶段 | 目标 | 内容 |
|------|------|------|
| **Phase 1** | Quickest win | 从 OptiGuide 入手，利用现成的 what-if 问题集，快速验证工具流在供应链场景上的可行性 |
| **Phase 2** | 展示规划能力 | 扩展到 REALM-Bench J 系列，用 JSSP 场景展示工具在制造调度规划上的能力 |
| **Phase 3** | 展示动态系统能力 | 扩展到 REALM-Bench P 系列，用网约车和供应链场景展示在动态系统上的能力 |

---

*本计划基于截至 2026 年 5 月的调研结果制定。两个 Benchmark 均为活跃维护的开源项目，具体实施时建议参考其最新版本的文档。*
