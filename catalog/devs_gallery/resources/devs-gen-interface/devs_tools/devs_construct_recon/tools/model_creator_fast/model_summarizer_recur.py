from typing import Dict, Any
import json
import copy
from pathlib import Path
from ...base_types import PlanResult, StandardContextModel, PlanTreeNode

# 假设 ModelSummarizer 已经在同级或可引用的位置
from .model_summarizer import ModelSummarizer 

class HierarchySummarizer:
    """
    负责在 Phase 2 代码生成结束后，自底向上重新总结整个模型树。
    这确保了耦合模型（Coupled Model）的描述是基于子节点实际生成的代码行为，
    而不是 Phase 1 时的预期计划。
    """
    def __init__(self, model_id: str, working_directory: str = "./working_dir", verbose: bool = True):
        """
        Args:
            model_summarizer: 已经初始化好的 ModelSummarizer 实例
            verbose: 是否打印进度
        """
        self.summarizer = ModelSummarizer(model_id, working_directory)
        self.verbose = verbose
        # 用于收集更新后的所有节点信息，方便外部更新 registry
        self.refined_registry: Dict[str, StandardContextModel] = {}

    def summarize_tree(self, root_node: 'PlanTreeNode') -> StandardContextModel:
        """
        主入口：传入根节点，返回经过重新总结的根节点信息。
        同时会填充 self.refined_registry。
        """
        self.refined_registry = {}
        if self.verbose:
            print(f"\n📝 [Summary] Starting Bottom-Up Summarization for root: {root_node.model_info.class_name}")
            
        final_root_summary = self._process_node(root_node)
        
        if self.verbose:
            print(f"✅ [Summary] Completed. Refined {len(self.refined_registry)} models.")
            
        return final_root_summary

    def _process_node(self, node: 'PlanTreeNode') -> StandardContextModel:
        """
        递归处理函数：
        1. 先递归处理所有子节点 (Bottom-Up)。
        2. 收集子节点最新的 Summary。
        3. 更新当前节点的 Plan (将 children_plan 替换为最新的 summaries)。
        4. 调用 ModelSummarizer 生成当前节点的 Summary。
        """
        
        # --- 1. 递归处理子节点 (Bottom-Up) ---
        updated_children_summaries = []
        if node.children:
            for child_node in node.children:
                # 递归调用
                child_summary = self._process_node(child_node)
                updated_children_summaries.append(child_summary)

        # --- 2. 准备当前节点的 Plan ---
        # 我们必须深拷贝一份 Plan，以免修改原始引用（虽然 Phase2 已经结束，但保持数据纯洁较好）
        current_plan_input = copy.deepcopy(node.plan)

        # 如果是 Coupled Model，关键步骤是将刚才递归回来的、最新的子节点信息填进去
        # 这样 ModelSummarizer 就能看到子节点实际长什么样，而不是计划长什么样
        if current_plan_input.type == 'coupled':
            # 这里的 children_plan 在定义里通常是 List[StandardContextModel]
            current_plan_input.children_plan = updated_children_summaries
            if self.verbose:
                print(f"  Start summarizing Coupled Model: {node.model_info.class_name} (with {len(updated_children_summaries)} updated children)...")
        else:
            if self.verbose:
                print(f"  Start summarizing Atomic Model: {node.model_info.class_name}...")

        # --- 3. 调用 ModelSummarizer ---
        result = self.summarizer.forward(model_plan=current_plan_input)

        # --- 4. 结果处理与错误检查 ---

        if isinstance(result, StandardContextModel):
            # 成功情况
            final_summary = result
        else:
            # 未知类型，回退
            final_summary = node.model_info

        # --- 5. 更新注册表并返回 ---
        # 确保名字一致
        if final_summary.class_name != node.model_info.class_name:
             # 防御性编程：防止 LLM 幻觉改名
             final_summary.class_name = node.model_info.class_name
        
        self.refined_registry[final_summary.class_name] = final_summary
        return final_summary