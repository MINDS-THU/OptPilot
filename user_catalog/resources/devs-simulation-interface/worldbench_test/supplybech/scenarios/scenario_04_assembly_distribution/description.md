# 供应链补货策略优化任务

## 场景描述

你负责为一个**四级组装-分销供应链网络**设计补货策略。网络结构如下：

```
Supplier_A_0 (原材料A供应商，无限供应) ─┐
Supplier_B_0 (原材料B供应商，无限供应) ─┤ lead_time=2天
Supplier_C_0 (原材料C供应商，无限供应) ─┤
                                        ▼
                              Assembler_0 (组装厂)
                              初始库存: Raw_A=500, Raw_B=500, Raw_C=500
                                      Product_X=100, Product_Y=80
                                        │ lead_time=2天
                                        ▼
                              DC_0 (分销中心)
                              初始库存: Product_X=200, Product_Y=150
                                        │ lead_time=1天
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
              Retailer_0          Retailer_1          Retailer_2
        (各初始: X=50, Y=40)  (各初始: X=50, Y=40)  (各初始: X=50, Y=40)
```

### 产品信息

- **Raw_Material_A (product_id=1)**: 原材料A，仅用于生产 Product_X
- **Raw_Material_B (product_id=2)**: 原材料B，仅用于生产 Product_Y
- **Raw_Material_C (product_id=3)**: 原材料C，**共享原材料**，同时用于 Product_X 和 Product_Y
- **Finished_Product_X (product_id=4)**: 成品X，高需求产品
- **Finished_Product_Y (product_id=5)**: 成品Y，低需求产品

### BOM（物料清单）关系

| 成品 | 所需原材料 |
|------|-----------|
| Product_X (4) | 1×Raw_A (1) + 1×Raw_C (3) |
| Product_Y (5) | 2×Raw_B (2) + 1×Raw_C (3) |

> 注意：Raw_Material_C 是两种产品的**共享原材料**，需要合理分配。

### 成本参数

| 节点 | 仓储成本(元/件/天) | 缺货成本(元/件) | 补货提前期 |
|------|-------------------|----------------|-----------|
| Assembler | 0.3 | 20.0 | 2天 (从供应商) |
| DC | 0.5 | 30.0 | 2天 (从组装厂) |
| Retailer | 2.0 | 80.0 | 1天 (从DC) |

### 需求模式

每个零售门店每天面临**随机需求**，由 seed 控制可复现性。

- **Product_X (4)**: base=15, noise=±5, seasonal_amp=6, period=21天
- **Product_Y (5)**: base=10, noise=±4, seasonal_amp=4, period=14天

需求公式：`demand = base + uniform(-noise, +noise) + seasonal_amp * sin(2*pi*period/cycle)`

### 仿真周期

- 共 100 天
- 随机种子默认 42（可通过 CLI 参数修改）

## 你的任务

你需要为 **三层节点** 分别设计补货策略函数：

1. **Retailer 层**：从 DC_0 订购 Product_X 和 Product_Y
2. **DC 层**：从 Assembler_0 订购 Product_X 和 Product_Y
3. **Assembler 层**：从三个供应商订购原材料 A、B、C

### 函数签名

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照，格式如 {4: 30.5, 5: 25.3}
            键为 product_id (4=Product_X, 5=Product_Y)
            值为 inventory_position（库存位置 = 实物库存 + 在途订单 - 欠单）

    Returns:
        订货指令字典，格式如：
        {
            "DC_0": {4: 50.0, 5: 30.0}
        }
        键为上游节点语义名（本场景中只有 "DC_0"），
        值为 {product_id: 订货数量}。
        如果不订货，返回空字典 {}。
    """

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照，格式如 {4: 150.0, 5: 100.0}
            键为 product_id (4=Product_X, 5=Product_Y)
            值为 inventory_position

    Returns:
        订货指令字典，格式如：
        {
            "Assembler_0": {4: 200.0, 5: 150.0}
        }
        键为上游节点语义名（本场景中只有 "Assembler_0"），
        值为 {product_id: 订货数量}。
        如果不订货，返回空字典 {}。
    """

def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照，格式如 {1: 400.0, 2: 350.0, 3: 300.0, 4: 80.0, 5: 60.0}
            键为 product_id (1=Raw_A, 2=Raw_B, 3=Raw_C, 4=Product_X, 5=Product_Y)
            值为 inventory_position

    Returns:
        订货指令字典，格式如：
        {
            "Supplier_A_0": {1: 200.0},
            "Supplier_B_0": {2: 300.0},
            "Supplier_C_0": {3: 250.0}
        }
        键为上游供应商节点语义名，
        值为 {product_id: 订货数量}。
        如果不订货，返回空字典 {}。

    注意：
        - 原材料订货数量必须 >= 0
        - 需要考虑 BOM 约束：Product_X 需要 1×A + 1×C，Product_Y 需要 2×B + 1×C
        - Raw_C 是共享原材料，需要协调分配
    """
```

### 输出格式要求

你的代码必须包含一个 `POLICY_MOUNTS` 字典，声明每个函数挂载到哪个节点组：

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    # Retailer 层策略
    ...

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    # DC 层策略
    ...

def assembler_policy_func(period: int, inventory_dict: dict) -> dict:
    # Assembler 层策略
    ...

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "DC": dc_policy_func,
    "Assembler": assembler_policy_func,
}
```

### 优化目标

**最小化总成本** = 总仓储成本 + 总缺货成本

- 仓储成本：库存越多，成本越高（Assembler 0.3/件/天，DC 0.5/件/天，Retailer 2.0/件/天）
- 缺货成本：需求未满足时产生惩罚（Assembler 20元/件，DC 30元/件，Retailer 80元/件）

### 关键挑战

1. **多级协调**：三层策略需要相互配合，Retailer 的需求波动会向上游传递（牛鞭效应）
2. **BOM 约束**：Assembler 需要根据成品需求计算原材料采购量，遵循 BOM 比例
3. **共享原材料竞争**：Raw_C 同时用于 Product_X 和 Product_Y，需要合理分配
4. **多产品协调**：两种产品需求模式不同（均值、波动、季节性周期不同）
5. **提前期差异**：从供应商到 Assembler 需要 2 天，从 Assembler 到 DC 需要 2 天，从 DC 到 Retailer 需要 1 天

### 约束

- 订货数量必须 >= 0
- Retailer 只能向 "DC_0" 订货
- DC 只能向 "Assembler_0" 订货
- Assembler 只能向 "Supplier_A_0"、"Supplier_B_0"、"Supplier_C_0" 订货
- 需要同时为所有相关产品制定补货决策
