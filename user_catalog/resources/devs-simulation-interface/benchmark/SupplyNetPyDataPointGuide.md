# SupplyNetPy 数据点生成操作指南

目标：给 coding agent 一个固定流程，低改动地批量生成可评测的数据点（description + run + checker + config + golden_data）。

## 1. 最小交付清单

每个数据点目录至少包含：
- `description.yaml`
- `run.py`（reference simulator）
- `checker.py`
- `config.json`（L1 + L2）
- `tools/generate_golden.py`
- `golden_data/*.json`

## 2. description 模板（严格三段）

### 2.1 general（固定环境约束）
只写环境与接口，不写业务参数。

建议模板：
```yaml
general: |
  ### General Implementation Requirements
  - Python 3.10+
  - `python run.py` 可执行
  - 使用 argparse 接收参数
  - 使用 `--seed` 同时设置 `random.seed(seed)` 与 `numpy.random.seed(seed)`
  - stdout 输出 JSONL；stderr 可输出任意调试信息
```

### 2.2 scenario（业务语义 + 参数表）
要把关键业务参数写全，但不要写代码实现细节。

建议包含：
- 单元组件模板：节点类型、容量、初始库存、持有成本、补货策略参数。
- 拓扑模板：边列表（source, sink, cost, lead_time）。
- 需求模板：到达分布、数量分布。
- KPI 语义：profit/revenue/cost 等字段含义与单位。

### 2.3 args_input_output（参数+输入输出契约）
必须明确：
- CLI 参数：`--simulate_time`, `--seed`
- stdin 是否需要（多数 case 不需要）
- stdout 必须出现的业务事件（例如 `sim_trace`）
- required 字段列表
- 明确“允许额外事件/字段，checker 只过滤并检查 required business events/fields”

## 3. run.py 模板（reference）

推荐结构：
1. 解析 args，设置 seed。
2. 构建 SupplyNetPy 网络（节点、策略、链路、需求）。
3. 运行仿真，抽取最终 KPI。
4. 输出一条 `sim_trace` JSONL。

注意：
- 保证固定 seed 下可复现。
- 保证字段名与 `description`/`checker` 完全一致。

## 4. checker.py 模板（稳健型）

必备规则层次：
1. `schema`：required 字段完整性。
2. `identity`：如 `profit = revenue - total_cost`。
3. `bounds`：非负、上下界关系。
4. `L1 expected`：固定 seed 精确值（或容差）。
5. `L2 distribution`：多次运行分布检验（KS + 退化分布处理）。

稳健性要求：
- 忽略无关日志行/额外事件。
- 只过滤并检查业务目标事件。
- SciPy 不可用时提供 KS fallback。

## 5. config.json 模板

至少两个 entry：

1) L1 单次精确
- 固定 seed 与 horizon
- `checker_args` 包含 expected KPI

2) L2 多次分布
- 多个固定 seed（建议 0..9）
- `checker_args` 包含 `golden_data_path`, `min_samples`, KS 阈值

## 6. 固定脚本流程（可交给 agent）

### 6.1 填空 spec（机械化入口）
复制并填写：`benchmark/specs/supplynetpy_case_spec_template.json`

核心只需填：
- `case_name`
- `horizon`
- `expected`（L1锚点）
- `l2_seeds`
- `golden_data_path`

推荐同时填：
- `sim_timeout`（复杂场景建议 >2 秒，避免评测阶段超时被误判）

### 6.2 一键创建目录骨架（由 spec 生成）
```bash
python tools/build_supplynetpy_case.py --spec benchmark/specs/<CaseName>.spec.json
```

### 6.3 兼容旧入口（preset 快速克隆）
```bash
python tools/create_supplynetpy_datapoint.py --case_name <CaseName> --preset test2
```

### 6.4 生成 golden data
```bash
conda run -n hamlet_env python benchmark/<CaseName>/tools/generate_golden.py \
  --sim_script benchmark/<CaseName>/run.py \
  --simulate_time 20 \
  --runs 30 \
  --seed_start 0 \
  --output benchmark/<CaseName>/golden_data/<case>_20d_distribution.json
```

### 6.5 运行评测
```bash
conda run -n hamlet_env python devs_tester/eval_pipeline.py \
  --config_file benchmark/<CaseName>/config.json \
  --output_dir /tmp/<CaseName>_eval \
  --sim_script benchmark/<CaseName>/run.py \
  --checker_script benchmark/<CaseName>/checker.py
```

### 6.6 调整与复跑
- 若 L1 失败：优先检查 KPI 字段语义与口径。
- 若 L2 失败：检查随机过程定义、seed 使用、golden 数据来源一致性。

## 7. 本仓库可直接复用模板来源

- 基准模板来源：`benchmark/BakerySup2`
- 自动脚本：`tools/create_supplynetpy_datapoint.py`
- spec 构建脚本：`tools/build_supplynetpy_case.py`

这个流程下，agent 只需“填业务参数表 + 跑三条命令”即可生成合法数据点。
