---
sidebar_position: 5
slug: /research/trace-guided-heuristic-design
title: 评测工厂调度策略
description: 在模拟工厂里运行一项策略，查看一个短班次内的实际指标。
featured: true
preview_summary: 从短班次的基线调度策略出发，在同一模拟器和评分规则下评估修改后的策略。
---

工厂收到订单后，调度策略决定怎样使用机器和自动导引车。评分程序把模拟工厂的公开状态传给策略，再执行它返回的指令。因此，实验作者可以换一项策略，同时复用同一套模型和评分程序。

公开清单提供一个基线策略和一项短班次研究 `smoke`。先运行基线并查看实际记录的指标。添加第二项策略后，可以在相同班次条件下比较效率、质量或车辆利用率。清单还定义了一个会发生设备故障的较长班次，可以单独评测。

[*LLM-Guided Heuristic Design from Simulation Traces*](https://arxiv.org/abs/2608.09343) 研究怎样利用仿真轨迹改进策略。此处可以直接运行的研究评测给定的调度策略。要进行自动搜索，需要另行配置生成器。[运行调度实验（英文）](https://minds-thu.github.io/OptPilot/reference/llm-policy-search/)
