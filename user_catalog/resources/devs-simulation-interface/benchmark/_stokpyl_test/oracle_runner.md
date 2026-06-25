# Oracle Runner 说明文档（当前实现）

`oracle_runner.py` 是单次仿真执行器：

- 读取蓝图与运行参数
- 组装 `stockpyl` 网络
- 执行一次仿真
- 输出标准 JSONL 事件日志

它不负责统计分布计算；分布构建由 `build_test_suite.py` 等上层脚本完成。

---

## 1. 输入协议

### 1.1 核心参数

- `--blueprint`（必填）：蓝图路径
- `--seed`（可选，默认 `42`）：随机种子
- `--periods`（可选，默认 `100`）：仿真周期数

### 1.2 动态参数注入

所有额外 `--key value` 参数会被收集为 `dynamic_args`，并注入蓝图全局变量：

- `DYNAMIC_ARGS`：`dict[str, str]`

例如：`--num_retailers 20 --retailer_holding_cost 5.0` 会注入为：

```python
{
  "num_retailers": "20",
  "retailer_holding_cost": "5.0"
}
```

**Seed 特殊传播行为**：`--seed` 参数除了传给 `stockpyl.sim.simulation(rand_seed=seed)` 外，还会被强制注入 `DYNAMIC_ARGS["seed"]`（通过 `dyn_params.setdefault("seed", args.seed)`），确保蓝图 hook 函数能通过 `DYNAMIC_ARGS["seed"]` 读取当前运行的 seed 值。这为随机场景的 hook 提供了统一的 seed 访问接口。

**Seed 特殊传播行为**：`--seed` 参数除了传给 `stockpyl.sim.simulation(rand_seed=seed)` 外，还会被强制注入 `DYNAMIC_ARGS["seed"]`（通过 `dyn_params.setdefault("seed", args.seed)`），确保蓝图 hook 函数能通过 `DYNAMIC_ARGS["seed"]` 读取当前运行的 seed 值。这为随机场景的 hook 提供了统一的 seed 访问接口。

### 1.3 标准输入注入

`stdin` 全量读取后注入蓝图全局变量：

- `STDIN_DATA`：`str`

用于蓝图中的外部需求流等长序列输入。

---

## 2. 网络组装语义（关键实现）

当前 `oracle_runner.py` 的组装策略与 `stockpyl` 接口严格对齐，避免隐式兜底。

### 2.1 节点与实例展开

- 根据 `topology.node_groups` 展开组实例（如 `Retailer_0 ... Retailer_n`）
- 建立双向映射：
  - `id_to_semantic: node_id -> semantic_name`
  - `semantic_to_id: semantic_name -> node_id`

### 2.2 产品建模（重要）

每个节点会显式创建真实产品对象：

- 从 `initial_inventory` 中读取正整数产品 ID
- 对每个产品执行 `node.add_product(SupplyChainProduct(index=pid))`
- `node.initial_inventory_level` 只保留真实产品键

这样可避免 `stockpyl` 退回 dummy product 路径导致的策略空指针问题。

### 2.3 源节点供给语义

若 `config.role == "source"`，会设置：

- `node.supply_type = {pid: 'U' for pid in product_ids}`

即源节点对对应产品是外部无限供给，匹配 `stockpyl` 对 source 端库存位置计算的预期。

### 2.4 策略与需求 Hook 挂接

- 普通策略：使用蓝图静态 `policy` 配置构造 `Policy(**cfg)`
- 自定义策略：使用 `LLMPolicy`
- 自定义需求：使用 `LLMDemandSource`

两者均为 `stockpyl` 原生扩展方式（继承类），不修改上层蓝图契约。

### 2.5 成本函数 Hook

如配置 `holding_cost_func` / `stockout_cost_func`：

- `local_holding_cost_function` 接受当前节点正库存字典
- `stockout_cost_function` 接受当前节点缺货字典
- 字典仅包含真实产品（`pid > 0`）

---

## 3. Hook 语义与严格校验

### 3.1 周期编号约定

`stockpyl` 仿真内部周期是 `0..T-1`，蓝图语义周期是 `1..T`。

- 调用 `demand_func` / `policy_func` 时统一传 `period = sim_period + 1`
- 日志提取时也统一输出 `period = stockpyl_period + 1`

### 3.2 `policy_func` 返回结构要求

`LLMPolicy` 对返回值执行严格校验：

- 顶层必须是 `dict`
- 每个上游键对应值必须是 `dict`
- 数量必须可转为 `float` 且不能为负
- 上游键必须可解析（语义上游名或 `EXTERNAL`）
- 若映射到多个原材料导致路由歧义，直接报错

不会做静默降级或包装式补救（例如把错误类型强行包装成可运行结构）。

---

## 4. 日志输出协议

### 4.1 输出流约束

- `stdout`：仅输出 JSONL 事件
- `stderr`：运行异常与调试信息

### 4.2 `raw_state` 字段来源

传给蓝图 `log_extractor` 的 `raw_state` 当前为：

- `inventory_level`：真实产品期末库存（字符串键）
- `backorder`：真实产品负库存绝对值总和
- `holding_cost_incurred`：`stockpyl` 的 `holding_cost_incurred`
- `stockout_cost_incurred`：`stockpyl` 的 `stockout_cost_incurred`

---

## 5. 一致性验证脚本

新增脚本：`compare_oracle_vs_direct_stockpyl.py`

用途：

- 独立构建一份 direct stockpyl 网络（不复用 `OracleRunner` 类）
- 与 `oracle_runner.py` 子进程输出做日志多重集对比
- 同时打印 KPI 对比结果

### 5.1 使用示例

```bash
python compare_oracle_vs_direct_stockpyl.py \
  --case_name Base_Condition \
  --seed 42 \
  --periods 100

python compare_oracle_vs_direct_stockpyl.py \
  --case_name Scale_Stress_Test \
  --seed 42 \
  --periods 100
```

若一致，会输出 `[OK]` 并给出日志数量与 KPI。

---

## 6. 压测生成验证

使用：

```bash
python build_test_suite.py --oracle_runs 30
```

当前可稳定完成：

- `Base_Condition` 30/30
- `Scale_Stress_Test` 30/30

并生成：

- `outputs/Base_Condition_golden.json`
- `outputs/Scale_Stress_Test_golden.json`
- `config.json`

---

## 7. 设计原则（本版本）

- 不改蓝图上层接口契约
- 不做错误掩盖型 fallback
- 出现接口语义不一致时优先抛出显式异常
- 通过独立 direct 组装脚本做输出一致性校验
