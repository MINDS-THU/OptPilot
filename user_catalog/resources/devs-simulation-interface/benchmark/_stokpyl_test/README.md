# Stockpyl Benchmark Toolkit README

这个目录是一套围绕 `scenario_blueprint.py` 的评测基建工具，用来完成下面几件事：

- 根据 blueprint 组装 `stockpyl` 网络并运行 Oracle（`oracle_runner.py`）
- 多次运行 Oracle，生成 KPI 分布金标准（`build_test_suite.py`）
- 生成评测说明文档（`build_description.py`）
- 对日志做分层判卷（`checker.py`）
- 一键跑完整闭环（`master_pipeline.py`）
- 从「只有 blueprint 的目录」自动构建完整标准数据点（`build_datapoint_from_blueprint_dir.py`）

---

## 1. 目录内关键脚本

- `scenario_blueprint.py`
  - 当前主蓝图（被 `build_test_suite.py` / `checker.py` / `build_description.py` 默认导入）
- `oracle_runner.py`
  - 单次仿真执行器，读取 blueprint，输出 JSONL 日志
- `build_test_suite.py`
  - 多次运行 Oracle，生成 `outputs/*_golden.json` 与 `config.json`
- `checker.py`
  - 按 Tier1~Tier4 校验日志与 KPI 分布
- `build_description.py`
  - 生成 `description.yaml`（可选择是否启用 LLM 润色）
- `master_pipeline.py`
  - 一键执行：build suite -> build description -> self consistency test
- `build_datapoint_from_blueprint_dir.py`
  - 从 blueprint 目录自动复制运行时文件并构建完整数据点

辅助验证脚本：

- `direct_stockpyl_scenario_check.py`
  - 仅针对当前 `scenario_blueprint.py` 的场景手写 direct stockpyl 仿真，对比 oracle 输出
- `compare_oracle_vs_direct_stockpyl.py`
  - 独立 direct 组装脚本，对比 oracle 输出（更通用）
- `smoke_test_extra_blueprints.py`
  - 对额外蓝图做冒烟测试

---

## 2. 最常见工作流

### 2.1 仅跑一次 Oracle

```bash
python oracle_runner.py --blueprint scenario_blueprint.py --seed 42 --periods 100
```

如需动态参数：

```bash
python oracle_runner.py --blueprint scenario_blueprint.py --seed 42 --num_retailers 3
```

---

### 2.2 生成 Golden + config

```bash
python build_test_suite.py
```

调试时可减少轮数：

```bash
python build_test_suite.py --oracle_runs 10
```

---

### 2.3 一键跑完整闭环

```bash
python master_pipeline.py
```

可选参数：

```bash
python master_pipeline.py --blueprint scenario_blueprint.py --oracle-runs 30
python master_pipeline.py --blueprint scenario_blueprint.py --oracle-runs 10 --use-llm
```

说明：

- `--use-llm` 会让 `build_description.py` 启用 LLM 润色
- 不带 `--use-llm` 时，默认 `--no-llm`，速度更快且不依赖外部模型调用

---

### 2.4 从 blueprint 目录构建标准完整数据点（推荐）

假设你有一个目录，里面只有一个 blueprint 文件：

```bash
python build_datapoint_from_blueprint_dir.py \
  --blueprint-dir /path/to/blueprint_dir
```

如果目录里不止一个 `.py`，可显式指定：

```bash
python build_datapoint_from_blueprint_dir.py \
  --blueprint-dir /path/to/blueprint_dir \
  --blueprint-file my_blueprint.py \
  --output-dir /path/to/output_datapoint \
  --oracle-runs 30
```

只准备文件，不执行 pipeline：

```bash
python build_datapoint_from_blueprint_dir.py \
  --blueprint-dir /path/to/blueprint_dir \
  --prepare-only
```

完成后，输出目录中通常会有：

- `scenario_blueprint.py`
- `oracle_runner.py`, `build_test_suite.py`, `checker.py`, `master_pipeline.py`, ...
- `description.yaml`
- `config.json`
- `outputs/*.json`

---

## 3. Blueprint 契约（最小要求）

建议 blueprint 至少包含：

- `metadata`
- `cli_args_schema`
- `stdin_schema`
- `topology`
- `event_schema`
- `log_extractor(period, semantic_node_id, raw_state) -> list`
- `extract_kpis(logs) -> dict`
- `test_cases`

可选：

- `custom_hooks`（`demand_func` / `policy_func` / `holding_cost_func` / `stockout_cost_func`）
- `tier2_checkers`, `tier3_checkers`

---

## 4. 输出物说明

- `outputs/<case>_golden.json`
  - 每个 case 的 KPI 分布金标准
- `config.json`
  - 评测矩阵配置（case、参数、golden 路径等）
- `description.yaml`
  - 用于题面/说明的结构化文档

---

## 5. 验证与排错建议

1. 先跑单次 Oracle，确保 blueprint 本身可执行：
   - `python oracle_runner.py --blueprint scenario_blueprint.py`
2. 再跑小规模 Golden：
   - `python build_test_suite.py --oracle_runs 2`
3. 再跑全链路：
   - `python master_pipeline.py --oracle-runs 10`
4. 若要核对 oracle 组装正确性：
   - `python direct_stockpyl_scenario_check.py --case_name Base_Condition`

---

## 8. 随机场景专项工作流

本工具链支持随机种子控制场景（stochastic, seed-controlled）的完整闭环，关键差异如下：

### 8.1 Blueprint 要求

- `cli_args_schema` 必须声明 `seed` 参数（`type: int`）
- hook 函数 docstring 需注明哪种随机性受 seed 控制
- `test_cases` 需配置 `seed_mode/seed_start/seed_arg` 字段
- 详见 `stockpyl_cases/BLUEPRINT_TEXT_GUIDELINES.md` §8

### 8.2 Seed 传播链路

```
blueprint.test_cases.seed_mode
  → build_test_suite.py 写入 config.json (seed_mode/seed_start/seed_arg)
    → eval_runner.py.resolve_sim_args_for_run() 为每次 run 生成递增 seed
      → 生成代码的 run.py 收到 --seed <value>
        → 仿真引擎使用 rand_seed=<value>
```

同时 `oracle_runner.py` 将 `seed` 注入 `DYNAMIC_ARGS`，确保 hook 函数在 golden 生成阶段也能读取 seed。

### 8.3 验证随机性生效

```bash
# 用不同 seed 跑 Oracle，确认 KPI 不同
cd <datapoint>
python oracle_runner.py --blueprint scenario_blueprint.py --seed 2026 --periods 90 --num_stores 5
python oracle_runner.py --blueprint scenario_blueprint.py --seed 2027 --periods 90 --num_stores 5
# 若 KPI 不同 → 随机性生效
```

### 8.4 确定性场景随机性检查

```bash
# 对确定性场景用 seed=42/43/44 跑，KPI 应完全相同
python oracle_runner.py --blueprint scenario_blueprint.py --seed 42 --periods 100 --num_retailers 3
python oracle_runner.py --blueprint scenario_blueprint.py --seed 43 --periods 100 --num_retailers 3
# 若 KPI 完全相同 → 无隐藏随机源
```

### 8.5 已知注意事项

- 随机场景 oracle_runs 较少时（<30），选单个稳定 KPI 避免自检失败（见 `COMMON_ERRORS.md` §10）
- 确定性场景不应在 `cli_args_schema` 中声明 `seed`
- 所有确定性场景已通过 seed 42/43/44 随机性检查

---

## 8. 随机场景专项工作流

本工具链支持随机种子控制场景（stochastic, seed-controlled）的完整闭环，关键差异如下：

### 8.1 Blueprint 要求

- `cli_args_schema` 必须声明 `seed` 参数（`type: int`）
- hook 函数 docstring 需注明哪种随机性受 seed 控制
- `test_cases` 需配置 `seed_mode/seed_start/seed_arg` 字段
- 详见 `stockpyl_cases/BLUEPRINT_TEXT_GUIDELINES.md` §8

### 8.2 Seed 传播链路

```
blueprint.test_cases.seed_mode
  → build_test_suite.py 写入 config.json (seed_mode/seed_start/seed_arg)
    → eval_runner.py.resolve_sim_args_for_run() 为每次 run 生成递增 seed
      → 生成代码的 run.py 收到 --seed <value>
        → 仿真引擎使用 rand_seed=<value>
```

同时 `oracle_runner.py` 将 `seed` 注入 `DYNAMIC_ARGS`，确保 hook 函数在 golden 生成阶段也能读取 seed。

### 8.3 验证随机性生效

```bash
# 用不同 seed 跑 Oracle，确认 KPI 不同
cd <datapoint>
python oracle_runner.py --blueprint scenario_blueprint.py --seed 2026 --periods 90 --num_stores 5
python oracle_runner.py --blueprint scenario_blueprint.py --seed 2027 --periods 90 --num_stores 5
# 若 KPI 不同 → 随机性生效
```

### 8.4 确定性场景随机性检查

```bash
# 对确定性场景用 seed=42/43/44 跑，KPI 应完全相同
python oracle_runner.py --blueprint scenario_blueprint.py --seed 42 --periods 100 --num_retailers 3
python oracle_runner.py --blueprint scenario_blueprint.py --seed 43 --periods 100 --num_retailers 3
# 若 KPI 完全相同 → 无隐藏随机源
```

### 8.5 已知注意事项

- 随机场景 oracle_runs 较少时（<30），选单个稳定 KPI 避免自检失败（见 `COMMON_ERRORS.md` §10）
- 确定性场景不应在 `cli_args_schema` 中声明 `seed`
- 所有确定性场景已通过 seed 42/43/44 随机性检查

---

## 6. 环境说明

- Python 3.10+
- 依赖：`stockpyl`, `numpy`, `scipy`, `yaml`, `litellm`（仅 `--use-llm` 时需要模型可用）
- 本目录可能存在 `.env`（例如模型调用配置）；脚本按正常环境变量读取，不需要也不应在流程中打印其内容。

---

## 7. 备注

- 本工具链的设计目标是「严格接口对齐 + 显式报错」，避免 fallback 式掩盖错误。
- 若你要把它用于新场景，优先复用 `build_datapoint_from_blueprint_dir.py`，可以最快生成可评测的数据点目录。
