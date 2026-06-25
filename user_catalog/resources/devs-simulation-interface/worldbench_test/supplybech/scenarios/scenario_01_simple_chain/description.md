# 供应链补货策略优化任务

## 场景描述

你负责为一个三级供应链网络设计补货策略。网络结构如下：

```
Factory (工厂，无限供应)
    ↓ lead_time=3天
Central_DC (中央仓库，初始库存=1500)
    ↓ lead_time=1天
Retailer_0, Retailer_1, Retailer_2 (3个零售门店，各初始库存=150)
```

### 产品信息
- 单一产品：Smartphone (product_id=1)

### 成本参数
| 节点 | 仓储成本(元/件/天) | 缺货成本(元/件) |
|------|-------------------|----------------|
| Central_DC | 1.0 | 50.0 |
| Retailer | 2.5 | 100.0 |

### 需求模式
每个零售门店每天面临相同的确定性需求，按6天周期循环：
`[30, 30, 30, 10, 10, 10]` 件/天

### 仿真周期
- 共 100 天

## 你的任务

为 **Retailer 层** 的3个零售门店设计一个补货策略函数 `retailer_policy_func`。

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
            "Central_DC_0": {1: 50.0}
        }
        键为上游节点语义名（本场景中只有 "Central_DC_0"），
        值为 {product_id: 订货数量}。
        如果不订货，返回空字典 {}。
    """
```

### 输出格式要求

你的代码必须包含一个 `POLICY_MOUNTS` 字典，声明函数挂载到哪个节点组：

```python
def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    # 你的策略逻辑
    ...

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
}
```

### 优化目标

**最小化总成本** = 总仓储成本 + 总缺货成本

- 仓储成本：库存越多，成本越高
- 缺货成本：需求未满足时产生惩罚，成本很高（100元/件）

你需要在"不过度库存"和"不频繁缺货"之间找到平衡。

### 约束
- 订货数量必须 >= 0
- 只能向上游节点 "Central_DC_0" 订货
