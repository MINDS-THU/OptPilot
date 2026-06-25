# 供应链补货策略优化任务

## 场景描述

你负责为一个三级供应链网络设计补货策略。网络结构如下：

```
Factory (工厂，无限供应)
    ↓ lead_time=3天
DC_0 (中央仓库，初始库存: Product_A=600, Product_B=400)
    ↓ lead_time=2天
Retailer_0, Retailer_1, Retailer_2, Retailer_3
(各初始库存: Product_A=80, Product_B=50)
```

### 产品信息
- **Product_A (product_id=1)**: 高需求产品，均值18件/天，21天季节性周期
- **Product_B (product_id=2)**: 低需求产品，均值12件/天，14天季节性周期

### 成本参数
| 节点 | 仓储成本(元/件/天) | 缺货成本(元/件) | 补货提前期 |
|------|-------------------|----------------|-----------|
| DC | 0.5 | 30.0 | 3天 (从工厂) |
| Retailer | 2.0 | 80.0 | 2天 (从DC) |

### 需求模式
每个零售门店每天面临**随机需求**，由 seed 控制可复现性。
- 两种产品的需求具有**季节性波动**（正弦函数）
- Product_A: base=18, noise=±6, seasonal_amp=8, period=21天
- Product_B: base=12, noise=±4, seasonal_amp=5, period=14天
- 季节性脉冲在两种产品之间**不完全同步**（周期不同）

### 仿真周期
- 共 100 天
- 随机种子默认 42（可通过 CLI 参数修改）

## 你的任务

为 **Retailer 层** 设计一个**多产品补货策略函数**，同时管理 Product_A 和 Product_B 的补货。

### 函数签名

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照，格式如 {1: 80.5, 2: 50.3}
            键为 product_id (1=Product_A, 2=Product_B)
            值为 inventory_position（库存位置 = 实物库存 + 在途订单 - 欠单）

    Returns:
        订货指令字典，格式如：
        {
            "DC_0": {1: 50.0, 2: 30.0}
        }
        键为上游节点语义名（本场景中只有 "DC_0"），
        值为 {product_id: 订货数量}。
        如果不订货，返回空字典 {}。
    """
```

### 输出格式要求

你的代码必须包含一个 `POLICY_MOUNTS` 字典，声明函数挂载到哪个节点组：

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    # 你的多产品策略逻辑
    ...

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
```

### 优化目标

**最小化总成本** = 总仓储成本 + 总缺货成本

- 仓储成本：库存越多，成本越高（2.0元/件/天，两种产品相同）
- 缺货成本：需求未满足时产生惩罚（80元/件，两种产品相同）

### 关键挑战

1. **多产品协调**：两种产品需求模式不同（均值、波动、季节性周期不同），需要分别制定策略
2. **季节性预测**：Product_A 有21天周期，Product_B 有14天周期，策略需要考虑季节性
3. **共享仓储**：两种产品共享同一个仓储空间，库存过多会增加总持有成本
4. **提前期**：从 DC 补货需要 2 天，需要提前预测需求

### 约束
- 订货数量必须 >= 0
- 只能向上游节点 "DC_0" 订货
- 需要同时为两种产品制定补货决策
