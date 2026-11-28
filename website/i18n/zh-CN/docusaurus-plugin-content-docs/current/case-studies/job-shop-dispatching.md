---
sidebar_position: 6
slug: /research/coopa
title: 检查 COOPA 求解器的答案
description: 检查一次运筹求解的目标值、问题陈述和报告。
featured: true
preview_summary: 用自然语言描述一个决策问题，查看 COOPA 返回的目标值和求解过程记录。
---

[COOPA](https://arxiv.org/abs/2606.27611) 研究如何求解自然语言描述的运筹优化问题。OptPilot 示例中的 `solve` 研究给定一道题，让生成器提交一个答案。候选方案包括报告的目标值、是否求得答案，以及 JSON 格式的求解记录。

评分程序检查报告格式和必填字段；如果求解器报告找到了答案，还会检查目标值是否为有限数值。指标 `solved` 为 1 表示通过了这些检查。若要断言方案可行或达到最优，还需要独立核对约束，并与可信解或已知的界进行比较。

参照指南设置 COOPA 环境、选择模型和编辑问题，然后审阅研究设置。[运行并解读 COOPA 示例（英文）](https://minds-thu.github.io/OptPilot/reference/or-solving/)
