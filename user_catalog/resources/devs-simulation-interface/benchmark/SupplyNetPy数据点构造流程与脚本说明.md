# SupplyNetPy 数据点构造流程与脚本说明

本文基于以下现有资料整理，并结合当前仓库的实现给出一套可直接落地的中文流程：

- `benchmark/BakerySup2`
- `benchmark/BenchmarkDatasetWorkflow.md`
- `benchmark/SupplyNetPyDataPointGuide.md`
- `benchmark/OTrain_newchecker`

目标：

1. 统一数据点构造流程，减少“每次重做”的成本。
2. 在仅检查最终 KPI 之外，引入过程日志校验，避免“单点故障导致全局误判”。
3. 提供一个“少量参数输入 -> 自动生成新数据点”的脚本入口。

## 1. 当前标准构造流程（整理版）

## 1.1 最小交付物

每个 benchmark 数据点目录建议至少包含：

- `description.yaml`
- `run.py`（参考实现 / oracle）
- `checker.py`（规则与评分）
- `config.json`（L1 + L2）
- `tools/generate_golden.py`
- `golden_data/*.json`

典型结构如下：

```text
benchmark/<CaseName>/
  description.yaml
  run.py
  checker.py
  config.json
  tools/generate_golden.py
  golden_data/<case>_<horizon>_distribution.json
```

## 1.2 description.yaml 的职责分层

保持三段分离：

- `general`：环境、入口、seed、stdout/stderr 契约。
- `scenario`：业务语义、参数、拓扑、KPI 含义。
- `args_input_output`：CLI、stdin、stdout 事件契约、必填字段、容错边界。

关键原则：

- 业务逻辑写在 `scenario`，不要混入实现细节。
- 输入输出契约写在 `args_input_output`，特别是“哪些字段必须有、哪些额外日志允许”。

## 1.3 run/checker/config/golden 的协同

1) `run.py`
- 固定 seed 时必须可复现（`random` + `numpy`）。
- 输出 JSONL，至少有最终业务结果事件（通常 `sim_trace`）。

2) `checker.py`
- 先做 schema，再做 identity/bounds，再做 L1/L2。
- 只检查目标业务事件，忽略无关日志和额外字段。

3) `config.json`
- L1：固定 seed + 固定 horizon + expected KPI。
- L2：多 seed + golden_data 分布对比（KS + 退化分布处理）。

4) `golden_data`
- 使用参考 `run.py` 批量跑 seeds 生成，保证可追溯。

## 2. 过程日志检查增强（借鉴 OTrain_newchecker 的思路）

`OTrain_newchecker` 的核心启发：

- 不只看最终结果（KPI），还要验证“过程逻辑是否成立”。
- 规则按层次组织，可精确定位故障来源。

针对 SupplyNetPy（BakerySup2 模板）已加入如下增强：

1) 在 `run.py` 中新增 `process_trace` 事件（最终时刻输出）
- 按 group(0/1)输出 demand/fulfilled/shortage/backorders 等中间汇总。
- 同时输出 totals 和 fill_rate。

2) 在 `checker.py` 中新增过程规则
- `process_trace_present`：是否输出过程事件。
- `process_trace_schema`：过程事件结构/类型/恒等关系是否正确。
- `process_vs_kpi_consistency`：`process_trace` 与 `sim_trace` 是否一致。

这样可以避免以下误判：

- 仅因一个局部组件写错导致 KPI 全偏，但不清楚问题在哪。
- 模型“碰巧”凑出接近 KPI，但过程语义不成立。

## 3. args_input_output 优化建议（已在模板落地）

之前常见问题：

- 输出事件到底要几个、哪个用于评分，不够清晰。
- “允许额外日志”与“必需字段”混在一起，阅读负担高。

现在建议固定为 9 个小节：

1. CLI 参数（required）
2. stdin 约束
3. stdout 总体格式
4. 必需业务事件
5. 通用 top-level schema
6. `sim_trace.payload` 必填字段
7. `process_trace.payload` 必填字段
8. 跨事件一致性
9. KPI 硬约束

参考实现见：`benchmark/BakerySup2/description.yaml`

## 4. 一键生成新数据点脚本（少量参数版）

新增脚本：`tools/create_supplynetpy_datapoint_minimal.py`

脚本做的事：

1. 从模板目录克隆新 case。
2. 自动更新 `description.yaml` 的 `root_model_name`。
3. 自动跑一次 L1（`run.py`）提取 expected anchor（profit/revenue/inventory_waste）。
4. 自动写入 `config.json`（L1 + L2）。
5. 可选自动生成 golden data。

## 4.1 最简用法

```bash
python tools/create_supplynetpy_datapoint_minimal.py \
  --case_name MyBakeryCase \
  --horizon 20 \
  --l1_seed 0 \
  --l2_seeds 0-9 \
  --generate_golden
```

说明：

- `--l2_seeds` 支持 `0-9` 或 `0,1,2,3`。
- 不传 `--golden_data_path` 时，会自动用 `golden_data/<case>_<horizon>d_distribution.json`。
- 目录已存在时可加 `--force` 覆盖。

## 4.2 生成后建议立刻执行

```bash
conda run -n hamlet_env python devs_tester/eval_pipeline.py \
  --config_file benchmark/MyBakeryCase/config.json \
  --output_dir /tmp/MyBakeryCase_eval \
  --sim_script benchmark/MyBakeryCase/run.py \
  --checker_script benchmark/MyBakeryCase/checker.py
```

## 5. 推荐验收清单

- 固定 seed 的 L1 可重复。
- L2 的最小样本数、KS 阈值与 golden 生成范围一致。
- 添加无关日志后 checker 仍稳定。
- 失败时能区分：schema 问题、过程逻辑问题、最终 KPI 问题。

以上流程可直接用于后续批量扩展 benchmark 数据点，并保留较好的可解释性和可维护性。
