# ABP Benchmark Evaluation Pipeline

自动化评测 Alternating Bit Protocol (ABP) 模拟器的Pipeline。

## 目录结构

```
benchmark/
├── README.md                 # 本文档
├── SYSTEM_PROMPT.md          # ABP协议规范 (DEVS模型说明)
├── ABP_D1.yaml               # 代码生成配置 (给Agent的规范)
├── BENCHMARK_TEST_CASES.md   # 测试用例详细说明
│
├── master_pipeline.py        # 顶层Pipeline (代码生成 + 评测)
├── eval_pipeline.py          # 评测Pipeline (运行模拟器 + 验证)
├── checker.py                # 验证器 (继承 BaseValidator)
├── checker_utils.py          # 验证器基础框架 (BaseValidator, RuleType, ScoringMethod)
├── abp_test_config.json      # 测试配置 (9个测试用例)
├── run_eval.sh               # 快速运行脚本
│
├── sample_simulator/         # 样例模拟器
│   └── run.py                # Python ABP模拟器参考实现
│
└── ref/                      # 参考实现
    ├── ABP2/                 # 验证器参考
    ├── ABP3/                 # 验证器参考
    └── dev_teseter_2/        # Pipeline参考
```

## 快速开始

### 0. 一键运行 (推荐)

```bash
# 使用默认样例模拟器运行所有测试
./run_eval.sh

# 测试自己的模拟器
./run_eval.sh --sim_cwd /path/to/your/simulator

# 详细输出
./run_eval.sh --verbose
```

### 1. 运行评测 (eval_pipeline)

直接测试已有的模拟器代码：

```bash
python eval_pipeline.py \
  --config_file abp_test_config.json \
  --output_dir ./results \
  --sim_script run.py \
  --sim_cwd /path/to/your/simulator \
  --validator_script ./checker.py \
  --pass_threshold 0.9
```

### 2. 完整流程 (master_pipeline)

包含代码生成和评测的完整流程：

```bash
python master_pipeline.py \
  --project_root /path/to/agent/project \
  --workspace ./temp_workspace \
  --gen_config /path/to/ABP_D1.yaml \
  --test_config ./abp_test_config.json \
  --validator ./checker.py \
  --pass_threshold 0.9
```

### 3. 单独验证

验证单个JSONL输出文件：

```bash
python checker.py output.jsonl \
  --total 5 \
  --seed 42 \
  --expect_retry 0
```

## Pipeline 流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                        master_pipeline.py                           │
├─────────────────────────────────────────────────────────────────────┤
│  Phase 1: Code Generation                                           │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Agent生成Python模拟代码 → 返回(sim_cwd, sim_entry)           │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ↓                                      │
│  Phase 2: Evaluation                                                │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                     eval_pipeline.py                          │  │
│  │  ┌─────────────────────────────────────────────────────────┐  │  │
│  │  │  For each test case:                                    │  │  │
│  │  │    1. python {sim_script} {sim_args}                    │  │  │
│  │  │    2. stdout → model_output.jsonl                       │  │  │
│  │  │    3. python checker.py {output} {validator_args}       │  │  │
│  │  │    4. 解析JSON结果 → 评分                               │  │  │
│  │  └─────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ↓                                      │
│                      final_report.json                              │
└─────────────────────────────────────────────────────────────────────┘
```

## 模拟器输出格式

模拟器必须输出 **JSONL** 格式到 **stdout**，每行一个JSON对象：

```json
{"time": 0.0, "entity": "sender", "event": "delay_start", "payload": {"type": "preparation", "duration": 10}}
{"time": 10.0, "entity": "sender", "event": "packet_sent", "payload": {"seq_num": 1, "bit": 0, "is_retry": false}}
{"time": 10.0, "entity": "subnet", "event": "packet_get", "payload": {"behavior": "pass", "channel": "forward", "noise_value": 25}}
{"time": 13.0, "entity": "receiver", "event": "delay_start", "payload": {"type": "processing", "duration": 10}}
{"time": 23.0, "entity": "receiver", "event": "packet_received", "payload": {"seq_num": 1, "bit": 0}}
{"time": 23.0, "entity": "subnet", "event": "packet_get", "payload": {"behavior": "pass", "channel": "backward", "noise_value": 25}}
{"time": 26.0, "entity": "sender", "event": "ack_received", "payload": {"ack_bit": 0, "is_valid": true}}
```

### 事件类型

| Entity | Event | Payload |
|--------|-------|---------|
| sender | delay_start | `{"type": "preparation", "duration": <ms>}` |
| sender | packet_sent | `{"seq_num": <int>, "bit": <0\|1>, "is_retry": <bool>}` |
| sender | ack_received | `{"ack_bit": <0\|1>, "is_valid": <bool>}` |
| receiver | delay_start | `{"type": "processing", "duration": <ms>}` |
| receiver | packet_received | `{"seq_num": <int>, "bit": <0\|1>}` |
| subnet | packet_get | `{"behavior": <"drop"\|"pass">, "channel": <"forward"\|"backward">, "noise_value": <int>}` |

## 命令行参数

模拟器必须支持以下命令行参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--total_packets` | int | 5 | 发送的数据包总数 |
| `--seed` | int | 42 | 噪声生成器种子 |
| `--timeout` | int | 20 | 发送方超时时间 (ms) |
| `--sender_delay` | int | 10 | 发送方准备延迟 (ms) |
| `--receiver_delay` | int | 10 | 接收方处理延迟 (ms) |
| `--channel_delay` | int | 3 | 信道传输延迟 (ms) |
| `--simulate_time` | int | 1000 | 总模拟时间 (ms) |

## 评分规则

### 13条验证规则 (总权重 9.5)

#### 格式正确性 (权重 2.0)
- `log_format` (0.5): JSONL格式正确性 [BINARY]
- `event_existence` (0.5): 关键事件存在性 [RATIO]
- `json_integrity` (0.5): JSON字段完整性 [RATIO]
- `entity_correctness` (0.5): 实体与事件匹配 [RATIO]

#### 逻辑正确性 (权重 4.5)
- `noise_model` (1.0): LCG噪声计算 `x = (17*x + 11) % 100` [RATIO]
- `bit_alternation` (1.0): 比特交替 (0,1,0,1...) [RATIO]
- `sequence_continuity` (1.0): 序号连续 (1,2,3...) [RATIO]
- `ack_validity` (1.0): ACK有效性判断 [RATIO]
- `delay_parameters` (0.5): 延迟参数使用 [RATIO]

#### 行为一致性 (权重 3.0)
- `timing_correctness` (1.0): 时序关系 [RATIO]
- `completion_correctness` (1.0): 协议完成 [BINARY]
- `retransmission_behavior` (0.5): 重传行为 [BINARY]
- `event_statistics` (0.5): 事件统计 [THRESHOLD: 20%]

### 评分方法

- **BINARY**: 全对=1.0, 有错=0.0
- **RATIO**: 正确数/总数
- **THRESHOLD**: 错误率≤阈值=1.0, 否则=0.0

### 总分计算

```
total_score = Σ(rule.score × rule.weight) / 9.5
```

## 测试用例

| 名称 | 描述 | 主要验证点 |
|------|------|-----------|
| L0_Smoke_Test | 基础连接测试 | 2个包, 无重传 |
| L1_Noise_Trap | 噪声测试 | 5个包, 有重传 |
| L1_Single_Packet | 单包测试 | 1个包 |
| L1_Different_Seed | 不同种子 | seed=100 |
| L1_Custom_Delays | 自定义延迟 | 15ms延迟 |
| L1_Tight_Timeout | 紧凑超时 | timeout=15ms |
| L2_Stress_Test | 压力测试 | 50个包 |
| L2_High_Loss | 高丢包率 | seed=7 |
| L2_Long_Simulation | 长时间仿真 | 100个包 |

## 输出结果

评测完成后生成以下文件：

```
output_dir/
├── final_report.json          # 最终汇总报告
└── {test_name}/
    ├── model_output.jsonl     # 模拟器原始输出
    ├── model_debug.log        # 模拟器stderr
    └── val_result.json        # 验证详细结果
```

### final_report.json 示例

```json
{
  "summary": {
    "total_cases": 9,
    "passed": 9,
    "failed": 0,
    "average_score": 0.9868,
    "pass_threshold": 0.9
  },
  "cases": [
    {"name": "L0_Smoke_Test", "status": "PASS", "score": 1.0},
    ...
  ]
}
```

## 样例模拟器

`sample_simulator/run.py` 提供了一个参考实现，可用于测试Pipeline：

```bash
# 测试样例模拟器
python eval_pipeline.py \
  --config_file abp_test_config.json \
  --output_dir ./test_results \
  --sim_script run.py \
  --sim_cwd ./sample_simulator \
  --validator_script ./checker.py
```

## 参考

- `ref/ABP2/ABP_D1.yaml` - ABP协议规范
- `ref/ABP2/checker.py` - 验证器原始实现
- `ref/dev_teseter_2/eval_pipeline.py` - Pipeline原始实现
