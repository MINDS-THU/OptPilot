# Stockpyl Cases Registry

本文件记录 `stockpyl_cases` 目录中的场景组织方式与构建结果。

## 目录约定

- 每个场景独立一个文件夹，避免文件名冲突与产物覆盖。
- 每个场景文件夹至少包含：
  - 一个 blueprint 文件（`*.py`）
  - 一个 `datapoint/` 目录（通过 `build_datapoint_from_blueprint_dir.py` 构建）

## 已注册场景

| 场景ID | 场景目录 | Blueprint | Datapoint目录 | 状态 |
|---|---|---|---|---|
| linear_retail | `linear_retail/` | `linear_retail/linear_retail_blueprint.py` | `linear_retail/datapoint/` | ✅ 已构建，master_pipeline通过 |
| custom_hooks | `custom_hooks/` | `custom_hooks/custom_hooks_blueprint.py` | `custom_hooks/datapoint/` | ✅ 已构建，master_pipeline通过 |
| multiproduct | `multiproduct/` | `multiproduct/multiproduct_blueprint.py` | `multiproduct/datapoint/` | ✅ 已构建，master_pipeline通过 |
| static_tree_new | `static_tree_new/` | `static_tree_new/datapoint/scenario_blueprint.py` | `static_tree_new/datapoint/` | ✅ 已构建 |
| seasonal_promo_chain | `seasonal_promo_chain/` | `seasonal_promo_chain/scenario_blueprint_datapoint/scenario_blueprint.py` | `seasonal_promo_chain/scenario_blueprint_datapoint/` | ✅ 已构建，evaluated (score=0.5, 实现问题) |
| stochastic_seeded_noise | `stochastic_seeded_noise/` | `stochastic_seeded_noise/stochastic_seeded_noise_blueprint.py` | `stochastic_seeded_noise/datapoint/` | ✅ 已构建，master_pipeline通过，evaluated (score=0.5, 实现问题) |

## 每个 datapoint 的标准产物

每个 `datapoint/` 下应包含以下核心文件：

- `scenario_blueprint.py`
- `description.yaml`
- `config.json`
- `outputs/*.json`（golden 分布数据）

并包含可复现运行脚本（`oracle_runner.py`、`build_test_suite.py`、`checker.py`、`master_pipeline.py` 等）。

## 快速重建命令

在 `_stokpyl_test` 目录执行（示例）：

```bash
python build_datapoint_from_blueprint_dir.py \
  --blueprint-dir /home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/benchmark/stockpyl_cases/linear_retail \
  --output-dir /home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/benchmark/stockpyl_cases/linear_retail/datapoint \
  --overwrite
```

其他场景只需替换 `--blueprint-dir` 和 `--output-dir`。

## 场景类型分类

| 类型 | 场景 | 特征 |
|---|---|---|
| 确定性 (Deterministic) | `linear_retail`, `custom_hooks`, `multiproduct`, `static_tree_new`, `seasonal_promo_chain` | 无随机源，KPI 可精确复现；`test_cases` 无需 `seed_mode` |
| 随机种子控制 (Stochastic, seed-controlled) | `stochastic_seeded_noise` | 含随机噪声/采样，需 `seed` CLI arg + `test_cases.seed_mode`；KPI 为分布而非单值 |


