# REALM-Bench JSSP + DEVS-GEN 评估方案

> **目标**: 验证 DEVS-GEN 作为打包工具能否显著提升 LLM 在经典调度问题上的推理能力。

## 一、实验设计

### 1.1 对比条件

```
┌─────────────────────────────────────────────────────────┐
│              同一份场景描述 + JSSP 实例数据               │
│   "有 15 台机器，20 个作业，每作业 3-5 道工序..."         │
└────────────┬────────────────────┬───────────────────────┘
             │                    │
             ▼                    ▼
┌─────────────────────┐  ┌──────────────────────────────┐
│  Condition A         │  │  Condition B                  │
│  Baseline            │  │  DEVS-GEN assisted             │
│                      │  │                                │
│  Tools:              │  │  Tools:                        │
│  - Python 解释器      │  │  - devs_construct_tree         │
│  (仅做数值计算)       │  │  - devs_execute               │
│                      │  │  - 文件读/写/列表 (修代码)     │
│  LLM 直接推理调度     │  │  LLM: 建模 → 仿真 → 评估 → 迭代│
└─────────┬───────────┘  └─────────────┬──────────────────┘
          │                            │
          ▼                            ▼
┌─────────────────────────────────────────────────────────┐
│                    对比评估                               │
│  • makespan gap to UB (%)                                │
│  • 是否找到可行解 (validity)                              │
│  • 推理步数 / token 消耗                                  │
│  • 定性分析: 推理策略差异                                 │
└─────────────────────────────────────────────────────────┘
```

### 1.2 为什么选 JSSP

| 维度 | 评估 |
|------|------|
| **问题本质** | 天然离散事件系统 — 作业到达→排队→机器加工→完工 |
| **参数明确** | n_jobs, n_machines, processing_times, machine_sequence，直接可读 |
| **Ground Truth** | 69 个实例都有 known Upper Bound（最优 makespan） |
| **基线可比** | REALM-Bench 已有 ALAS 基线结果，可直接对比 |
| **DEVS 独有价值** | 展示 LLM 通过仿真进行调度策略搜索的能力 |

---

## 二、需要编写的文件

```
worldbench_test/
├── DEVS-GEN_Benchmark评估计划.md          # 已有
├── REALM-Bench/                            # git clone 来的
├── realmbench_runner/
│   ├── __init__.py
│   ├── instance_loader.py                  # 解析 TA/DMU 格式 → 结构化数据
│   ├── scene_description.py                # 生成场景描述文本（给 LLM 看的）
│   ├── config_template.yaml                # DEVS construct 的 YAML 模板
│   ├── tools.py                            # 封装 devs_construct_tree + devs_execute
│   ├── run_baseline.py                     # Condition A 主脚本
│   ├── run_devs_assisted.py                # Condition B 主脚本
│   └── evaluate.py                         # 对比分析 A vs B
├── results/
│   ├── baseline/
│   └── devs_assisted/
└── README.md                               # 使用说明
```

---

## 三、文件详细说明

### 3.1 `instance_loader.py`

解析 REALM-Bench J1 目录下的 JSSP 实例文件，支持三种格式：

**TA 格式** (e.g., `TA01.txt`):
```
Nb of jobs, Nb of Machines, Time seed, Machine seed, Upper bound, Lower bound
        15        15  840612802  398197754      1231      1005
Times
 94 66 10 53 ...    ← n_jobs x n_machines 处理时间矩阵
Machines
  7 13  5  8 ...    ← n_jobs x n_machines 机器序列矩阵
```

**DMU 格式** (e.g., `cscmax_20_15_1.txt`):
```
20 15                                    ← n_jobs n_machines
  6 105   5  16   0  48   3 114 ...      ← per-job: (machine_id, processing_time) 对
```

输出统一数据结构:
```python
@dataclass
class JSSPInstance:
    name: str           # e.g., "TA01"
    n_jobs: int         # 15
    n_machines: int     # 15
    ub: int             # 1231 (known upper bound)
    lb: int             # 1005 (known lower bound)
    # jobs[j][op] = (machine_id, processing_time)
    jobs: list[list[tuple[int, int]]]
```

### 3.2 `scene_description.py`

生成 LLM 可读的场景描述。两个 condition 共享同一份场景描述，区别仅在于可用工具。

```python
def build_scene_description(instance: JSSPInstance) -> str:
    """生成完整的 JSSP 场景描述"""
```

包含：
- 问题定义（什么是 JSSP）
- 实例参数（n_jobs, n_machines, 各作业的工序序列）
- 任务目标（最小化 makespan，已知 UB = X）
- 输出格式要求

### 3.3 `config_template.yaml`

DEVS construct 工具的 YAML 配置模板。`scenario` 部分是通用的，只需填 `root_model_name` 和 `base_folder`。

```yaml
root_model_name: JSSP_Simulator
requirements:
  general: |
    ### General Implementation Requirements
    1. Language & Environment:
    - Target Language: Python 3.10+
    - Standard Libraries: argparse, sys, json, logging, collections, random, xdevs.
    
    2. Input Interface:
    - CLI: --instance_file (path to job data JSON)
    - CLI: --dispatch_rule (string: FIFO/SPT/LPT/MWKR/random)
    - CLI: --seed (int, default 42)
    - CLI: --simulate_time (int, simulation time limit)
    
    3. Output (stdout, JSONL):
    Each line: {"time": float, "entity": str, "event": str, "payload": dict}
    - job_start/job_end events
    - simulation_end event with makespan
    
  scenario: |
    ### Scenario: Job Shop Scheduling Simulation
    
    系统包含以下组件:
    
    **Job Generator** (Atomic):
    - 读取实例文件，在 t=0 释放所有作业
    
    **Job Dispatcher** (Atomic):
    - 维护作业队列，机器空闲时按 dispatch rule 选择下一个作业
    - Dispatch rules: FIFO, SPT, LPT, MWKR, random
    
    **Machine** (Atomic, N 个实例):
    - State: idle/busy
    - 接收作业后开始加工，完成后变为 idle
    
    **KPI Collector** (Atomic):
    - 记录每个作业的完成时间
    - 所有作业完成后输出 makespan
    
  args_input_output: |
    ### Command Line Arguments:
    --instance_file, --dispatch_rule, --seed, --simulate_time
    
    ### stdout (JSONL):
    - job_start, job_end, machine_state_change, simulation_end

base_folder: jssp_model
skip_simulation_check: false
only_ensure_executable: false
```

### 3.4 `tools.py`

```python
def create_devs_tools(working_directory, model_id):
    """Condition B: DEVS 工具集"""
    file_tools = {
        "read": SeeTextFile(working_directory),
        "list": ListDir(working_directory),
        "write": ModifyFile(working_directory),
    }
    devs_construct = DEVSConstruct(...)    # devs_construct_pure_fast_plan
    devs_execute = DEVSExecute(...)         # devs_execute
    return [devs_construct, devs_execute] + list(file_tools.values())

def create_baseline_tools():
    """Condition A: 无 DEVS 工具"""
    return []
```

### 3.5 `run_baseline.py` — Condition A

```python
def run_baseline(instance, model_id, output_dir):
    """LLM 无 DEVS 工具，直接推理调度方案"""
    scene_desc = build_scene_description(instance)
    model = LiteLLMModel(model_id=model_id)
    
    agent = CodeAgent(
        tools=[],  # 无 DEVS 工具
        model=model,
        max_steps=15,
        additional_authorized_imports=["json", "random", "itertools", "collections"],
    )
    
    prompt = f"""{scene_desc}
Solve this JSSP instance. You can write Python code to compute schedules.
Return: best makespan, strategy, reasoning steps.
"""
    
    result = agent.run(prompt)
    return extract_and_save_result(result, instance, "baseline")
```

### 3.6 `run_devs_assisted.py` — Condition B

```python
def run_devs_assisted(instance, model_id, workspace_dir, output_dir):
    """LLM 有 DEVS 工具，通过仿真迭代优化调度方案"""
    scene_desc = build_scene_description(instance)
    model = LiteLLMModel(model_id=model_id)
    tools = create_devs_tools(workspace_dir, model_id)
    
    agent = ToolCallingAgent(
        model=model, tools=tools, max_steps=30, planning_interval=3,
    )
    
    prompt = f"""{scene_desc}
可用工具:
- devs_construct_tree: 构建 DEVS 仿真模型
- devs_execute: 运行仿真
- 文件编辑工具: 读取/修改代码（修复 bug）

步骤:
1. 用 devs_construct_tree 构建 JSSP 仿真模型
2. 如果 devs_execute 失败，读取错误信息，用文件工具修复代码
3. 尝试不同 dispatch rule (FIFO/SPT/LPT/MWKR)，记录 makespan
4. 迭代优化，找到最佳 makespan
5. 对比已知 UB = {instance.ub}
"""
    
    result = agent.run(prompt)
    return extract_and_save_result(result, instance, "devs_assisted")
```

### 3.7 `evaluate.py`

```python
def evaluate(baseline_results, devs_results):
    """对比两个条件的结果"""
    for rb, rd in zip(baseline_results, devs_results):
        print(f"Instance: {rb['instance']} (UB={rb['ub']})")
        print(f"  Baseline:     {rb['makespan']}  (gap: {rb['gap_pct']:.1f}%)")
        print(f"  DEVS-assisted: {rd['makespan']}  (gap: {rd['gap_pct']:.1f}%)")
        print(f"  Improvement: {rb['gap_pct'] - rd['gap_pct']:+.1f}%")
```

---

## 四、实例选择

从 REALM-Bench J1 目录选择 5-10 个实例，从小到大：

| 实例 | jobs × machines | UB | 来源 |
|------|-----------------|-----|------|
| ABZ5 | 10 × 10 | 1234 | abzswvyn |
| ABZ6 | 10 × 10 | 943 | abzswvyn |
| TA01 | 15 × 15 | 1231 | TA |
| DMU01 | 20 × 15 | (待查) | DMU |
| DMU11 | 30 × 15 | (待查) | DMU |

先从 ABZ5 (10×10) 和 TA01 (15×15) 做起，验证流程。

---

## 五、执行流程

```bash
# Step 1: 克隆 REALM-Bench
git clone https://github.com/genglongling/REALM-Bench.git worldbench_test/REALM-Bench

# Step 2: 跑 Baseline
python worldbench_test/realmbench_runner/run_baseline.py \
    --instances ABZ5,ABZ6,TA01 \
    --model_id openai/qwen3.6-plus \
    --output_dir worldbench_test/results/baseline/

# Step 3: 跑 DEVS-assisted
python worldbench_test/realmbench_runner/run_devs_assisted.py \
    --instances ABZ5,ABZ6,TA01 \
    --model_id openai/qwen3.6-plus \
    --workspace /tmp/jssp_devs_ws \
    --output_dir worldbench_test/results/devs_assisted/

# Step 4: 对比
python worldbench_test/realmbench_runner/evaluate.py \
    --baseline worldbench_test/results/baseline/*.json \
    --devs worldbench_test/results/devs_assisted/*.json
```

---

## 六、时间表

| 天 | 内容 | 工时 |
|----|------|------|
| D1 上午 | 写 `instance_loader.py` + `scene_description.py` | 2h |
| D1 下午 | 写 `config_template.yaml` + `tools.py` | 3h |
| D2 上午 | 写 `run_baseline.py` + 测试跑通 ABZ5 | 3h |
| D2 下午 | 写 `run_devs_assisted.py` + 测试跑通 ABZ5 | 4h |
| D3 上午 | 跑 5 个实例 × 2 条件（机器跑，人观察） | 2-3h 等待 |
| D3 下午 | 写 `evaluate.py` + 分析结果 + 准备 paper 图表 | 3h |

---

## 八、实测验证结果 (2026-05-16, 更新)

### 8.1 `run_evaluation.py` 实际运行的发现

通过直接调用 `run_evaluation.py` 并注册我们的 test runner，**确认了关键事实**：

| 问题 | 结论 |
|------|------|
| `run_evaluation.py` 会遍历 J1 目录的 69 个真实实例吗？ | **不会。** 只使用 `TASK_DEFINITIONS` 中硬编码的 toy data |
| P11 拿到的是什么数据？ | 3 machines, 5 jobs, 硬编码的 processing_times |
| `datasets/J1/` 目录的数据被代码读取过吗？ | **从未。** 评测框架没有任何地方读取该目录 |
| 其他场景 (P1-P10) 也都是 toy data 吗？ | **是的。** 所有 11 个任务都使用 hardcoded 小规模数据 |

**结论**：REALM-Bench 的评测框架 (`run_evaluation.py`) 是一个**协议定义 + 指标计算框架**，它定义了"如何评测一个 planner"，但不负责加载真实 benchmark 实例。P11 的 3×5 数据只是一个示例。

### 8.2 与 `run_evaluation.py` 的集成方式

评测框架是 plugin 式的。我们成功注册了两个新 runner：

```
run_evaluation.py
    ↓
get_framework_runners()
    ├── 'langgraph'      (失败: 无 agent_frameworks/ 目录)
    ├── 'autogen'        (失败)
    ├── 'crewai'         (失败)
    ├── 'swarm'          (失败)
    ├── 'baseline_litellm'   ← 我们的 Condition A
    └── 'devs_gen_litellm'   ← 我们的 Condition B
```

### 8.3 需要修改的文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `evaluation/framework_runners.py` | +5 行 (lazy import + runner 注册) | 在 `get_framework_runners()` 中注册 |
| `evaluation/evaluator.py` | 改 5 行 (robust metric averaging) | LLM 输出的 string 时间值导致 `sum()` 失败 |

所有改动记录在 `REALM-Bench/agent_frameworks_devs/MODIFICATIONS_LOG.md`。

### 8.4 新增文件

```
REALM-Bench/agent_frameworks_devs/
├── __init__.py              # 空
├── devs_gen_runner.py       # BaselineLitellmRunner + DevsGenLitellmRunner
├── MODIFICATIONS_LOG.md     # 修改记录
└── llm_logs/<timestamp>/    # LLM 调用日志
    ├── task_P11.json        # 每个任务的完整 task_definition
    ├── call_NNNN_<model>.json  # 每次 LLM 调用的完整输入/输出
    └── _all_calls.jsonl     # 合并日志
```

### 8.5 实际运行的模型测试

| 模型 | 任务数 | 成功/总 | 平均耗时 | 日志位置 |
|------|--------|---------|----------|----------|
| GPT-5.2 (via OpenRouter) | 11 | 11/11 | 43s (baseline), 40s (devs) | `llm_logs/` |
| Qwen3-Coder-30B (via OpenRouter) | 11 | 11/11 | 9s (baseline), 26s (devs) | `llm_logs/` |

### 8.6 用法

```bash
cd REALM-Bench

# 用 GPT-5.2 跑所有 11 个任务
DEVS_MODEL_ID=openrouter/openai/gpt-5.2 \
  python run_evaluation.py \
    --frameworks baseline_litellm,devs_gen_litellm \
    --tasks P11,P1,P2,P3,P4,P5,P6,P7,P8,P9,P10 \
    --runs 1 --no-viz

# 用 Qwen3-Coder 跑
DEVS_MODEL_ID=openrouter/qwen/qwen3-coder-30b-a3b-instruct \
  python run_evaluation.py \
    --frameworks baseline_litellm,devs_gen_litellm \
    --tasks P11,P1,P2,P3,P4,P5,P6,P7,P8,P9,P10 \
    --runs 1 --no-viz
```

### 8.7 下一步关键决策

既然 `run_evaluation.py` 不加载真实 JSSP 实例，我们有两条路：

**选项 1**：按 REALM-Bench 的协议跑，只评测 P1-P11 的 toy data
- 优点：完全遵循标准协议
- 缺点：toy data 太小，无法展示 DEVS 的优势；P11 的 3×5 JSSP 没有任何挑战

**选项 2**：自己加一个 instance loader，把 J1 的真实 69 个实例注入 TASK_DEFINITIONS
- 优点：可以在真实 benchmark 上评估，数据规模有意义 (15×15 到 50×20)
- 缺点：需要修改 `task_definitions.py` 或 `run_evaluation.py`（新增代码但遵循协议）
- 工作量：~50 行代码

**推荐选项 2**——这符合你的初衷"在实际任务上跑"。

---

## 十、风险评估（更新）

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| DEVS 模型生成失败 | 无法跑 Condition B | 准备预写好的 DEVS 模型作为 fallback |
| LLM 不会用 dispatch rule 参数 | 仿真跑不出有意义结果 | 在 prompt 中明确指导 |
| 仿真超时 | 无法得到 makespan | 设置合理 timeout，选小实例 |
| Baseline 也能写出好调度 | 差距不显著 | 选稍大实例（20×15+），增加难度 |
| DMU/ABZ 无 UB | 无法评估 gap | 使用 OR-Library 已知最优解查表 |
| LLM 代码有 bug | 仿真失败 | 给 LLM 文件编辑工具修代码 |

---

*2026年5月制定*
