
# 评测套件组装管家 (Test Suite Builder) 说明书

`build_test_suite.py` 是串联蓝图定义与评测平台的“桥梁”。它负责自动化执行巨量的预计算，并将所有的环境参数和检验标准打包为平台可识别的配置文件。

## 一、 工作流原理

1. **解析契约**：读取 `scenario_blueprint.py` 中的 `test_cases` 列表。
2. **环境压测**：对于每一个测试用例，它会利用多线程启动 $N$ 次（如 500 次）`oracle_runner.py` 子进程。通过真实的 `stdin` 和 `CLI args` 注入环境，收集标准 JSONL 日志。
3. **分布归档**：调用蓝图中的 `extract_kpis` 提取指标，将庞大的浮点数数组保存为物理 JSON 文件（防止内存超限）。
4. **生成矩阵**：自动拼接出符合评测平台标准的 `config.json` 运行配置矩阵。

## 二、 关键数据格式说明

组装完毕后，系统将生成以下两类核心文件。

### 1. 分布金标准文件 (`outputs/*_golden.json`)
由 Oracle 大量压测产生，供 `checker.py` 中的 Wasserstein EMD 算法读取。
* **文件位置**：通常为 `outputs/用例名称_golden.json`。
* **结构规范**：
```json
{
  "expected_kpis_distributions": {
    "total_system_cost": [8501.2, 8490.5, 8510.0, 8495.5, ...], // 长度通常为 500
    "average_backlog": [12.5, 13.0, 12.8, 14.1, ...]
  },
  "oracle_runs_completed": 500,
  "source_case": "High_Demand_Shock"
}

```

### 2. 评测运行矩阵 (`config.json`)

评测平台的**唯一执行入口**。它告诉沙盒系统：跑什么参数、喂什么数据、怎么判分。

* **结构规范**：一个包含多个数据条目（Data Entry）的 JSON 数组。

```json
[
  {
    "name": "E-Commerce_Base_Condition",
    "description": "测试常规需求下的零售分销策略表现。",
    "sim_timeout": 30.0,
    "checker_args": {
      "golden_data_path": "outputs/Base_Condition_golden.json",
      "kpi_tolerance_margin": 0.05
    },
    "cases": [
      {
        "num": 30,  // 对被测大模型生成代码的执行次数
        "sim_args": {
          "--num_retailers": "2",
          "--backorder_cost": "10.0"
        },
        "sim_stdin": "20.5 0.0\n15.0 1.2\n",
        "checker_config": {}
      }
    ]
  }
]

```

## 三、 Context Mapping 的最终闭环

当评测平台依据此 `config.json` 启动评测时：

1. **沙盒**会拉起目标大模型的 `run.py` 30 次（即 `cases[0].num`）。
2. 每次拉起，都会传入 `--num_retailers 2 --backorder_cost 10.0`，并将 `sim_stdin` 中的长文本注入系统管道。
3. 30 次结束后，平台将所有的日志路径打包，拉起一次 `checker.py`。
4. 平台会将 `checker_args` 中的 `golden_data_path` 注入为全局环境变量。`checker.py` 自行打开该 JSON 文件进行分布提取与比对，完成 Tier 4 最终判决。
