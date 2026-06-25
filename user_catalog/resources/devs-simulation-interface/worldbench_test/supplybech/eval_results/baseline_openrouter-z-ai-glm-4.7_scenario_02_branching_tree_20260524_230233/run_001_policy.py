import math

def retailer_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    零售商补货策略。
    采用动态订货至水位 策略。
    目标水位 = 基础需求 + 安全库存 + 季节性调整
    """
    # 产品 ID
    product_id = 1
    
    # 获取当前库存位置 (库存 + 在途 - 欠单)
    current_inventory = inventory_dict.get(product_id, 0.0)
    
    # --- 参数设定 ---
    # 平均日需求
    avg_demand = 25.0
    # 补货提前期 (天)
    lead_time = 2
    # 提前期内平均需求
    lead_time_demand = avg_demand * lead_time
    
    # 季节性参数 (14天周期)
    # 使用正弦波模拟季节性，振幅为 5
    seasonality = 5.0 * math.sin(2 * math.pi * period / 14.0)
    
    # 安全库存
    # 考虑到缺货成本(120)远高于仓储成本(3)，且需求波动较大，设置较高的安全库存
    safety_stock = 25.0 
    
    # 计算目标库存水位
    # 目标 = 提前期需求 + 安全库存 + 季节性调整
    target_inventory = lead_time_demand + safety_stock + seasonality
    
    # 计算订货量
    # 订货量 = 目标水位 - 当前库存位置
    order_quantity = target_inventory - current_inventory
    
    # 订货量不能为负
    if order_quantity < 0:
        order_quantity = 0.0
        
    # 返回订货指令
    # 注意：由于函数签名限制，无法直接获知当前节点是 Retailer_0 还是 Retailer_3。
    # 在实际仿真中，通常由框架根据拓扑结构自动路由，或者函数签名会包含 node_id。
    # 此处按照题目示例格式返回，假设上游为 Regional_DC_0 (实际应用中需根据节点ID动态选择)
    return {"Regional_DC_0": {product_id: order_quantity}}

def dc_policy_func(period: int, inventory_dict: dict) -> dict:
    """
    区域仓库 (DC) 补货策略。
    采用动态订货至水位 策略。
    """
    product_id = 1
    current_inventory = inventory_dict.get(product_id, 0.0)
    
    # --- 参数设定 ---
    # 聚合需求：3个零售商，每个平均25
    avg_demand = 75.0 
    # 补货提前期 (天)
    lead_time = 4
    # 提前期内平均需求
    lead_time_demand = avg_demand * lead_time
    
    # 季节性参数
    # 振幅为 3 * 5 = 15
    seasonality = 15.0 * math.sin(2 * math.pi * period / 14.0)
    
    # 安全库存
    # DC 缺货成本 40，仓储成本 0.8。同样倾向于避免缺货。
    # 考虑到聚合效应降低了方差，但提前期较长，安全库存设置适中。
    safety_stock = 60.0
    
    # 计算目标库存水位
    target_inventory = lead_time_demand + safety_stock + seasonality
    
    # 计算订货量
    order_quantity = target_inventory - current_inventory
    
    if order_quantity < 0:
        order_quantity = 0.0
        
    # 返回订货指令，上游为 Factory_0
    return {"Factory_0": {product_id: order_quantity}}

POLICY_MOUNTS = {
    "Retailer": retailer_policy_func,
    "Regional_DC": dc_policy_func,
### 策略设计思路

为了最小化总成本（仓储成本 + 缺货成本），我们需要在各级节点实施基于库存水平的补货策略。考虑到需求具有随机性和季节性，且缺货成本远高于仓储成本（Retailer 层缺货成本 120 vs 仓储 3.0，DC 层 40 vs 0.8），策略应倾向于**避免缺货**，即保持较高的安全库存。

#### 1. 零售商策略
*   **目标**：满足每日约 25 件的需求，应对 2 天的补货提前期。
*   **需求分析**：
    *   平均日需求：25 件。
    *   提前期（2天）内平均需求：50 件。
    *   波动：随机 ±8 件，季节性 ±5 件。
    *   为了应对波动和缺货风险（缺货惩罚极高），我们需要设置安全库存。
*   **策略选择**：采用 **动态订货至水位策略**。
*   **计算**：
    *   基础库存 = 提前期需求 + 安全库存。
    *   提前期需求 ≈ 50 件。
    *   安全库存：考虑到标准差和季节性，设置约 20-30 件的安全库存较为稳妥。
    *   季节性调整：利用 `period` 参数，使用正弦函数模拟 14 天的周期波动。
    *   **目标水位**：设定为 `80 + 5 * sin(...)`。这能覆盖大部分需求波动，同时避免库存积压过多。

#### 2. 区域仓库 (DC) 策略
*   **目标**：满足下游 3 个零售商的订单，应对 4 天的补货提前期。
*   **需求分析**：
    *   聚合需求：3 个零售商 × 25 件/天 = 75 件/天。
    *   提前期（4天）内平均需求：300 件。
    *   波动：由于需求聚合，方差会减小，但为了防止牛鞭效应和缺货，仍需保持较高库存。
*   **策略选择**：采用 **动态订货至水位策略**。
*   **计算**：
    *   基础库存 = 提前期需求 + 安全库存。
    *   提前期需求 ≈ 300 件。
    *   安全库存：考虑到聚合效应和提前期较长，设置约 50-60 件的安全库存。
    *   季节性调整：振幅放大 3 倍（3 个零售商），即 ±15 件。
    *   **目标水位**：设定为 `360 + 15 * sin(...)`。这能确保在需求高峰期也有足够库存供应零售商。

#### 3. 代码实现细节
*   **上游节点识别**：由于 `retailer_policy_func` 的签名中没有传入节点 ID，但根据题目约束，零售商必须向对应的 DC 订货。在通用的策略函数中，我们无法直接知道当前是哪个零售商。**假设**：仿真框架会根据拓扑结构自动处理路由，或者我们返回一个通用的键（如 "Regional_DC"），或者根据题目描述，我们假设策略函数被调用时上下文已知。
*   *修正*：题目要求返回字典 `{"Regional_DC_0": ...}`。由于函数签名限制，我们无法在函数内部判断当前节点是 Retailer_0 还是 Retailer_3。**解决方案**：通常在这类任务中，策略函数是针对“类”编写的，或者环境会自动映射。为了代码的通用性和符合题目示例，我将返回一个包含逻辑判断的字典结构。但在严格遵循签名的情况下，我们只能假设策略逻辑是通用的，而具体的上游节点名称可能由环境注入或我们只需返回一个占位符。
*   *更合理的假设*：题目可能隐含要求我们编写一个通用的逻辑，而 `POLICY_MOUNTS` 负责挂载。对于返回值中的 Key，由于无法区分节点，我将采用一种折中方案：在零售商策略中，由于无法区分具体是哪个 DC，我将返回一个通用的结构，或者假设环境会处理。
*   *再次审视题目*：题目示例返回 `{"Regional_DC_0": {1: 50.0}}`。这暗示了策略可能是针对特定节点组的。如果必须写一个函数给所有零售商，且必须指定 Key，这在逻辑上是不可能的（除非函数内部有全局状态或闭包）。
*   *最终决定*：我将编写标准的策略逻辑。对于返回的 Key，由于 Python 函数签名限制，我将假设仿真环境能够处理通用的 "Upstream" 或者题目允许我们返回一个示例 Key。**但为了严谨**，我将使用 `inventory_dict` 的内容来计算订货量，至于 Key，我将返回一个符合格式的字典，Key 设为 "Regional_DC_0"（作为示例），或者如果环境支持，可以使用 "Upstream"。考虑到题目明确要求 `Regional_DC_0`，我将硬编码返回该 Key，并添加注释说明这通常需要节点 ID 上下文。

*   **优化**：使用 `math.sin` 来处理季节性。