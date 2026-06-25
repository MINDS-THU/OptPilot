# DEVS Skill Pipeline — 开发计划

> 最后更新: 2025-04-21

## 目标

构建一个 **输入自然语言描述 → 输出可执行 Python 代码库** 的系统，利用 opencode 作为 coding agent，通过 skill 指导其完成 DEVS 建模。

## 架构

```
用户 YAML 需求
    │
    ▼
┌──────────────────────────────────────┐
│  run_opencode_skill.py               │
│  1. 读取 YAML config                 │
│  2. 在 workspace 中创建 skill 文件    │
│  3. 在 workspace 中创建 opencode.json │
│     (权限限制)                        │
│  4. 调用 opencode run (CLI)          │
│     (独立 process group)             │
│  5. opencode 在 workspace 中写代码    │
│  6. 输出 <<<GENERATION_RESULT>>>     │
└──────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────┐
│  master_pipeline.py (复用)           │
│  eval_pipeline.py → checker (复用)   │
└──────────────────────────────────────┘
```

## 文件结构

```
devs_baseline/devs_skill/
├── PLAN.md                              ← 本文件
├── skills/
│   ├── task-decomposition/SKILL.md      ← Skill 1: DEVS 任务拆分指导
│   └── code-writing/SKILL.md            ← Skill 2: DEVS 代码写作指导
├── materials/
│   ├── framework_comparison.md          ← simpy vs xdevs.py 决策表
│   ├── context_template.json            ← 子 agent 上下文模板
│   └── plan_output_schema.json          ← 输出 JSON Schema
├── examples/
│   ├── decomposition_flat_example.json  ← 全量拆分示例
│   └── decomposition_recursive_example.json ← 递归拆分示例
├── test_cases/
│   ├── 01_mm1_queue.yaml                ← 简单: M/M/1 排队
│   ├── 02_abp_protocol.yaml             ← 中等: ABP 协议
│   └── 03_hospital_system.yaml          ← 复杂: 医院系统
├── opencode_permission.json             ← 权限配置模板
├── run_opencode_skill.py                ← 核心: generator pipeline 入口
├── test_runner.py                       ← 开发测试工具
└── README.md                            ← 使用说明
```

## 运行模式 (正交设计)

`--mode` 和 `--framework` 是**正交参数**，不是混在一起的：

| `--mode` | `--framework` | 说明 | 适用场景 |
|----------|---------------|------|----------|
| `single` | `auto` | 一次调用 + skill 指导，框架自选 | 简单系统 |
| `single` | `simpy` | 一次调用 + skill 指导 + 强制 simpy | simpy 更合适时 |
| `single` | `xdevs` | 一次调用 + skill 指导 + 强制 xdevs.py | 需要严格 DEVS 语义时 |
| `two-stage` | `auto` | 两阶段（先生成 plan，再写代码），框架自选 | 中等系统 |
| `two-stage` | `simpy` | 两阶段 + 强制 simpy | 同上 |
| `two-stage` | `xdevs` | 两阶段 + 强制 xdevs.py | 同上 |
| `bare` | *(忽略)* | 无任何 skill 指导，opencode 自由发挥 | 基线对比 |

## 安全隔离

每次运行创建独立 workspace，包含：

1. **opencode.json** — 权限限制：
   - `external_directory`: 全部 deny
   - `webfetch/websearch/codesearch`: deny
   - `task`: deny
   - 危险 bash 命令 deny: `sudo`, `curl`, `wget`, `pip install`, `rm -rf /` 等
2. **Process group 隔离** — 子进程通过 `os.setsid()` 创建独立进程组，超时时通过 `os.killpg()` 只杀子进程组，**不会误杀父 opencode 进程**
3. **子进程超时** — 600s 硬限制
4. **独立目录** — 每次运行在 `devs_tester/devs_skill_runs/<mode>_<framework>_<timestamp>_<uuid>/` 下

## Skill 格式

opencode 的 skill 格式: `.opencode/skills/<name>/SKILL.md`，包含 YAML frontmatter:

```yaml
---
name: task-decomposition
description: Guide for decomposing requirements into DEVS model hierarchies
---
```

## 与现有系统集成

- `master_pipeline.py`: 新增 `opencode_skill` framework 分支
- `unified_runner.py`: 在 `FRAMEWORKS` 中新增配置
- 评测完全复用 `eval_pipeline.py` + `checker_script`

## 开发工作流

```
修改 SKILL.md → python test_runner.py --case ABP --mode single --framework auto → 查看结果 → 修改 → 重复
```

test_runner.py 支持:
- Level 1: 快速结构验证 (直接调 LLM)
- Level 2: 端到端集成 (通过 run_opencode_skill.py)
- Level 3: 回归测试 (批量跑所有 test_cases)

## 已知发现

1. `--dangerously-skip-permissions` 在当前 opencode 版本 (1.3.17) 中**不存在**，权限控制完全通过 `opencode.json` 配置实现。`--pure` 会禁用文件写入插件，所以不能用。

2. `opencode run` 的 message 参数是 positional args，多行 prompt 作为单个参数传递没问题（subprocess 不会 split）。

3. **opencode edit loop 问题**: 在 `single+simpy` 测试中，opencode 在编辑 `models/sender.py` 时陷入循环（write → edit failed → read → write），最终超时。这可能是 opencode 的 edit tool 与文件内容不匹配导致的。

4. `bare` mode 测试 ABP 协议得分 0.7684：核心协议逻辑正确（bit alternation, noise model, ACK validity 全 1.0），但 entity 命名约定错误（用了 `Sender/Receiver/Subnet` 而非 checker 期望的 `sender/receiver/subnet`），说明 skill 指导的重要性。

5. Workspace 路径不要放 `/tmp`，用 `devs_tester/devs_skill_runs/` 下的独立目录。
