# 供应链补货策略优化任务

## 场景描述

你负责为一个三级分叉供应链网络设计补货策略。网络结构如下：

```
Factory (工厂，无限供应)
    ↓ lead_time=4天
Regional_DC_0 (区域仓库A，初始库存=800)    Regional_DC_1 (区域仓库B，初始库存=800)
    ↓ lead_time=2天                              ↓ lead_time=2天
Retailer_0, Retailer_1, Retailer_2        Retailer_3, Retailer_4, Retailer_5
(各初始库存=100)                            (各初始库存=100)
```

### 产品信息
- 单一产品：Electronics (product_id=1)

### 成本参数
| 节点 | 仓储成本(元/件/天) | 缺货成本(元/件) | 补货提前期 |
|------|-------------------|----------------|-----------|
| Regional_DC | 0.8 | 40.0 | 4天 (从工厂) |
| Retailer | 3.0 | 120.0 | 2天 (从区域仓库) |

### 需求模式
每个零售门店每天面临**随机需求**，由 seed 控制可复现性。
- 平均需求约 25 件/天
- 波动范围 ±8 件
- 包含 14 天周期的季节性波动（振幅 ±5 件）

### 仿真周期
- 共 100 天
- 随机种子默认 42（可通过 CLI 参数修改）

## 你的任务

为 **Retailer 层** 和 **Regional_DC 层** 设计补货策略函数。

### 函数签名

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始，1~100）
        inventory_dict: 当前节点的库存快照，格式如 {1: 120.5}
            键为 product_id，值为 inventory_position（库存位置 = 实物库存 + 在途订单 - 欠单）

    Returns:
        订货指令字典，格式如：
        {
            "Regional_DC_0": {1: 50.0}   # 或 "Regional_DC_1"，取决于该 retailer 的上游
        }
    """

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    Args:
        period: 当前天数（从1开始）
        inventory_dict: 区域仓库的库存快照

    Returns:
        订货指令字典，格式如：
        {
            "Factory_0": {1: 200.0}
        }
    """
```

### 输出格式要求

你的代码必须包含一个 `POLICY_MOUNTS` 字典，声明函数挂载到哪个节点组：

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    # 你的 Retailer 策略逻辑
    ...

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    # 你的 DC 策略逻辑
    ...

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
}
```

### 优化目标

**最小化总成本** = 总仓储成本 + 总缺货成本

- 仓储成本：库存越多，成本越高（DC 0.8/件/天，Retailer 3.0/件/天）
- 缺货成本：需求未满足时产生惩罚（DC 40元/件，Retailer 120元/件）

### 关键挑战

1. **牛鞭效应**：Retailer 层的需求波动会向上游放大，DC 层需要平滑处理
2. **提前期较长**：DC 从工厂补货需要 4 天，Retailer 从 DC 补货需要 2 天
3. **随机需求**：需求有随机波动和季节性，策略需要鲁棒
4. **多级协调**：Retailer 和 DC 的策略需要相互配合

### 约束
- 订货数量必须 >= 0
- Retailer 只能向对应的 Regional_DC 订货（Retailer_0/1/2 → Regional_DC_0，Retailer_3/4/5 → Regional_DC_1）
- DC 只能向 Factory_0 订货
