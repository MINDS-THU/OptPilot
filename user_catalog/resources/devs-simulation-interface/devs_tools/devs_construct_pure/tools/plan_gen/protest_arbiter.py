import json
from dataclasses import dataclass
from typing import Literal, Optional
from enum import Enum
from pydantic import BaseModel, Field
from litellm import completion

from ...base_types import PlanResult, StandardContext, format_context_str
from ...utils import get_content_strict

# ==============================================================================
# 1. 数据结构定义
# ==============================================================================

class ArbitrationAction(str, Enum):
    SELF_FIX = "SELF_FIX"        # 父亲决定：是我的问题（我有资源没分下去，或者我内部逻辑有问题），我来改Plan
    ESCALATE = "ESCALATE"        # 父亲决定：我也搞不定（我也没有这个资源），得找爷爷修改我的Spec
    REJECT = "REJECT"            # 父亲决定：孩子你在瞎搞（需求里根本没这回事），驳回你的抗议

@dataclass
class ArbitrationResult:
    action: ArbitrationAction
    instruction: str             # SELF_FIX -> 指导 Refiner 修改 Plan; ESCALATE -> 向上级申请变更; REJECT -> 指导 Child 重新理解

# ==============================================================================
# 2. LLM 交互模型 (Pydantic)
# ==============================================================================

class BlameAssignment(BaseModel):
    """
    仲裁结果的结构化分析。
    """
    analysis: str = Field(..., description="Step-by-step analysis of who has the data and who needs it.")
    
    root_cause: Literal["PARENT_FORGOT_DELEGATION", "PARENT_LACKS_RESOURCE", "CHILD_HALLUCINATION"] = Field(
        ..., 
        description="PARENT_FORGOT_DELEGATION: Parent has data but didn't give to child. "
                    "PARENT_LACKS_RESOURCE: Parent doesn't have data, so Parent implies Grandparent missed it. "
                    "CHILD_HALLUCINATION: The data is not needed/exist based on project requirements."
    )
    
    fix_instruction: str = Field(..., description="The specific instruction string based on the action.")

# ==============================================================================
# 3. Prompt 设计
# ==============================================================================

ARBITER_PROMPT = """
## [Role]
You are the **System Architect (Arbiter)** responsible for the component "{parent_name}".
Your child component "{child_name}" has issued a PROTEST regarding the design specification you provided.
You must determine whose fault it is.

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [The Complaint]
The Child says: "{complaint}"

## [Investigation Context]
1. **My Constraints (What I have)**:
{parent_spec}
   
2. **My Plan (What I designed)**:
{children_spec}

3. **System Context**:
This section describes the **EXISTING** environment around `{parent_name}`.
{context_str}

## [Judgment Rules]
**Priority 1: Check Yourself (SELF_FIX)**
- If the child needs data X, and **YOU HAVE X** (in My Inputs or via another Child's output), but you did not pass it to the child -> It is **YOUR FAULT**.
- Action: `PARENT_FORGOT_DELEGATION`.
- Instruction: Tell the Plan Refiner exactly to change which children's plan, and how to change. You can also try to decide if some children should be removed or added. It is often the case that it's your fault because you forgot to delegate the data to the child, or the child is completely unnecessary.

**Priority 2: Check Upstream (ESCALATE)**
- If the child needs data X, and you **DO NOT HAVE X**, but X is legitimately required by the Original Requirements -> It is **YOUR PAULT's FAULT** (The Global Spec is wrong).
- Action: `PARENT_LACKS_RESOURCE`.
- Instruction: Explain to the Grandparent what you need, and why you need it, and how it should be provided. 

**Priority 3: Check Child (REJECT)**
- If the child asks for data X that is completely irrelevant to the project or can be calculated internally -> It is the **CHILD'S FAULT**.
    - Special: event logging / output is recommended to be implemented using a provided logging tool, unless specified to log through a port.
- Action: `CHILD_HALLUCINATION`.
- Instruction: Tell the Plan Refiner to emphasize and clarify the function related to the child's complaint. And tell the plan refiner, it only can modify this child's plan, do not modify anything else. 
"""

# ==============================================================================
# 4. EscalationArbiter 实现
# ==============================================================================

class EscalationArbiter:
    """
    [Phase 1] 当捕获到子节点的抗议时，进行责任归属判定。
    """
    def __init__(self, model_id: str = "gpt-4o"):
        self.model_id = model_id

    def _format_port_list(self, ports: list) -> str:
        return ", ".join([p.name for p in ports]) if ports else "(None)"

    def judge(self, self_plan: PlanResult, self_context: StandardContext, child_complaint: str, child_path: str) -> ArbitrationResult:
        """
        核心仲裁逻辑。
        """
        print(f"      [Arbiter] ⚖️  Investigating complaint from '{child_path}'...")

        # 1. 提取关键信息
        parent_model = self_plan.model_info
        
        # 找到抱怨的那个孩子的具体 Spec
        target_child_spec = next((c for c in self_plan.children_plan if c.logic_path == child_path), None)
        if not target_child_spec:
            print(f"      [Arbiter] ⚠️  Child '{child_path}' not found in children plan!")
        
        context_str = format_context_str(
            context=self_context,
            use_function=True, use_model_init_args=True, use_ports=True, 
            use_parent=True, use_siblings=True, use_path=True, use_system_goal=True,
        )

        # 2. 构建 Prompt
        prompt = ARBITER_PROMPT.format(
            parent_name=parent_model.class_name,
            child_name=child_path.split('.')[-1], # 简化的名字
            complaint=child_complaint,
            parent_spec=parent_model.specification.model_dump_json(),
            children_spec=json.dumps([i.model_dump(mode='json') for i in self_plan.children_plan]),
            context_str=context_str
        )

        # 3. LLM 判决
        try:
            response = completion(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.05, # 必须绝对理性
                response_format=BlameAssignment
            )
            content = get_content_strict(response)
            judgment = BlameAssignment.model_validate_json(content)

            print(f"      [Arbiter] 🔨 Verdict: {judgment.root_cause}")

            # 4. 映射到 ArbitrationResult
            if judgment.root_cause == "PARENT_FORGOT_DELEGATION":
                return ArbitrationResult(
                    action=ArbitrationAction.SELF_FIX,
                    instruction=f"[Self-Correction] I have the data but forgot to wire it. {judgment.fix_instruction}"
                )
            
            elif judgment.root_cause == "PARENT_LACKS_RESOURCE":
                return ArbitrationResult(
                    action=ArbitrationAction.ESCALATE,
                    instruction=f"[Escalation Request] I cannot satisfy child request because I lack inputs. Reason: {judgment.fix_instruction}"
                )
            
            elif judgment.root_cause == "CHILD_HALLUCINATION":
                return ArbitrationResult(
                    action=ArbitrationAction.REJECT,
                    instruction=f"[Guidance] Child `{child_path}`'s request is invalid. {judgment.fix_instruction}"
                )
            
        except Exception as e:
            print(f"      [Arbiter Error] {e}")
            # 出错时的保守策略：假设是自己的问题，尝试重构
        
        return ArbitrationResult(
            action=ArbitrationAction.SELF_FIX,
            instruction=f"Arbiter failed to parse. Assuming self-error. Complaint was: {child_complaint}, try to fix it. "
        )
        