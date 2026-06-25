# SupplyBench 构建计划

## 设计目标
评测 LLM 在供应链补货策略生成上的能力。LLM 接受一个供应链配置蓝图（节点、成本、需求模式），输出自定义的 `policy_func` 补充库存策略。将策略嵌入仿真运行，用两个指标评估：(1) 仿真是否成功跑完，(2) 总成本。

## 目录结构

```
supplybech/
├── PLAN.md
├── run.py                              # CLI 入口
│
├── engine/                             # 仿真引擎（从 _stokpyl_test 复制 + 适配）
│   ├── __init__.py
│   ├── oracle_runner.py                # 复用 _stokpyl_test/oracle_runner.py
│   └── kpi_utils.py                    # 复用 _stokpyl_test/kpi_utils.py
│
├── scenarios/
│   ├── scenario_01_simple_chain/       # 场景1：简单线性链
│   │   ├── __init__.py
│   │   ├── blueprint.py
│   │   ├── description.md
│   │   └── policy_cache/
│   │       ├── __init__.py
│   │       └── policy.py
│   │
│   ├── scenario_02_branching_tree/     # 场景2：分叉树形（随机需求）
│   │   ├── __init__.py
│   │   ├── blueprint.py
│   │   ├── description.md
│   │   └── policy_cache/
│   │       ├── __init__.py
│   │       └── policy.py
│   │
│   └── scenario_03_multi_product/      # 场景3：多产品共享仓储
│       ├── __init__.py
│       ├── blueprint.py
│       ├── description.md
│       └── policy_cache/
│           ├── __init__.py
│           └── policy.py
│
├── agent_frameworks/                   # Runner（共用，不依赖场景）
│   ├── __init__.py
│   ├── baseline_runner.py              # baseline: 纯 LLM → 输出 Python 代码字符串
│   ├── code_gen_runner.py              # code_gen TC: LLM 写代码 + run_python 工具调试
│   └── devs_gen_runner.py              # devs_gen TC: LLM 用 devs_construct_tree + devs_execute
│
├── evaluation/
│   ├── __init__.py
│   ├── evaluator.py                    # 编排全流程
│   └── scorer.py                       # 从结果中计算 run_success + total_cost
│
└── eval_results/                       # 评测结果输出
    └── {framework}_{model}_{scenario}_{timestamp}/
        ├── run_001_policy.py           # LLM 生成的策略代码
        ├── run_001_logs.jsonl          # 语义日志
        ├── run_001_kpis.json           # KPI 结果
        └── summary.json                # 聚合结果
```

## 场景对比

| 维度 | 场景1 Simple Chain | 场景2 Branching Tree | 场景3 Multi-Product |
|------|-------------------|---------------------|---------------------|
| **拓扑** | Factory→DC→3 Retailers（线性） | Factory→2 DCs→6 Retailers（分叉） | Factory→DC→4 Retailers（线性） |
| **产品数** | 1 | 1 | **2** |
| **需求** | 确定性周期波动 | 随机（seed 控制） | 随机 + **季节性**（两种产品不同周期） |
| **需实现 policy** | Retailer | Retailer + Regional_DC | Retailer（**多产品**） |
| **节点数** | 5 | 9 | 6 |
| **关键挑战** | 基本补货逻辑 | 牛鞭效应、多级协调 | **多产品库存协调、共享仓储、季节性预测** |
| **评测方式** | seed=42 跑多次（结果相同） | seed=42,43,44... 递增（取平均） | seed=42,43,44... 递增（取平均） |

## 核心流程

```
run.py
  │
  ├─ 1. evaluator.load_scenario("scenario_XXX")
  │     读取 blueprint.py, description.md
  │
  ├─ 2. for i in range(num_runs):
  │     │
  │     ├─ a. runner(description_path) → policy_code_str
  │     │
  │     ├─ b. 写入 policy_cache/policy.py
  │     │
  │     ├─ c. subprocess: python engine/oracle_runner.py --blueprint blueprint.py --seed (seed+i)
  │     │     → 捕获 stdout 中的 JSONL logs
  │     │
  │     └─ d. import blueprint → 调 extract_kpis(logs) → KPI dict
  │
  └─ 3. scorer.aggregate(results) → 保存 summary.json
```

## 评测指标

1. **run_success**: 仿真是否无异常跑完 (bool)
2. **total_cost**: 总成本 = holding_cost + stockout_cost (float)
3. **success_rate**: 成功次数 / 总次数
4. **avg_total_cost**: 成功运行的平均总成本

## 实施步骤

- [x] 创建目录结构 + engine/ (复制 oracle_runner.py, kpi_utils.py)
- [x] 创建场景1: blueprint.py + description.md + policy_cache/
- [x] 创建场景2: blueprint.py + description.md + policy_cache/
- [x] 创建场景3: blueprint.py + description.md + policy_cache/
- [x] 创建 agent_frameworks/baseline_runner.py
- [x] 创建 agent_frameworks/code_gen_runner.py
- [x] 创建 agent_frameworks/devs_gen_runner.py
- [x] 创建 evaluation/evaluator.py + scorer.py
- [x] 创建 run.py (含 --framework 参数)
- [x] 端到端测试验证
