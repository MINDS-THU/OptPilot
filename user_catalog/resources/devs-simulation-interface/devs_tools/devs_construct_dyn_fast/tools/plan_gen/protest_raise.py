import json
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field
from litellm import completion

from ...base_types import StandardContextModel, StandardContext, format_context_str
from ...utils import get_content_strict

# ==============================================================================
# 1. 数据结构定义
# ==============================================================================

class ProtestAction(str, Enum):
    PROTEST = "PROTEST"          # 拒绝：条件不足，无法完成任务
    ACCEPT = "ACCEPT"            # 接受：条件具备，可以开工

@dataclass
class ProtestResult:
    action: ProtestAction
    reason: str = ""             # 具体的抗议理由，用于反馈给父节点修正 Spec

# ==============================================================================
# 2. LLM 交互结构 (Pydantic Models)
# ==============================================================================

class FeasibilityIssue(BaseModel):
    issue_type: Literal["MISSING_INPUT", "UNDEFINED_OUTPUT", "AMBIGUOUS_LOGIC", "MAGIC_DATA_ACCESS"]
    description: str = Field(..., description="Short description of the problem.")
    suggestion: str = Field(..., description="What port or parameter is needed to fix this?")

class FeasibilityAssessment(BaseModel):
    """
    LLM 的评估结果。
    """
    status: Literal["FEASIBLE", "INFEASIBLE"] = Field(..., description="Can you implement this model with provided interface?")
    reasoning: str = Field(..., description="Chain of thought analysis.")
    critical_issues: list[FeasibilityIssue] = Field(default_factory=list, description="List of blocking issues if INFEASIBLE.")

# ==============================================================================
# 3. PROMPT 定义
# ==============================================================================

PROTEST_PROMPT = """
## [Role]
You are a critical Systems Engineer assigned to implement a specific component named **"{model_name}"**.
Your manager has given you a "Specification" (Inputs, Outputs, and Function Description).
You must evaluate if this specification is **technically feasible** before accepting the job.

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [System Context]
This section describes the **EXISTING** environment around `{model_name}`.
Use this to understand **WHO** connects to `{model_name}`'s ports and **WHAT** kind of data flows there.
{context_str}

## [Your Specification]
- **Path**: {model_spec}

## [Evaluation Criteria]
Check for the following **FATAL** flaws. If any exist, you MUST mark status as **INFEASIBLE**. You must be sure that the flaws are **irresolvable**. Vague or minor flaws are not considered fatal, you can ignore them.

1. **Input Starvation**: The description asks you to process data X, but there is NO input port for X.
	- *Example*: Function says "Average the temperature readings", but you have NO input port related to temperature.
2. **Magic Data Access**: The description assumes you have access to global data or sibling data without a port.
	- *Example*: "Check the status of the Database component." (You cannot "see" the Database component unless passed via a port).
3. **Telepathic Output**: The description says "Send packet to User", but there is NO output port connected to a User nor a Router. 
    - Special: event logging / output is recommended to be implemented using a provided logging tool, unless specified to log through a port.

## [Instruction]
- Be "lazy" but smart. Do not assume you can "figure it out later". If the ports are missing, you cannot do your job.
- Ignore minor naming preferences. Focus on **Data Flow Feasibility**.
- If everything looks reasonable (e.g., you have an input to process and an output to send result), mark as **FEASIBLE**.

"""

# ==============================================================================
# 4. ProtestAgent 实现
# ==============================================================================

class ProtestAgent:
    """
    [Phase 1] 负责在生成子节点之前，扮演子节点进行预检。
    判断是否接受当前的 Spec。
    """
    def __init__(self, model_id: str = "gpt-4o"):
        self.model_id = model_id

    def check(self, model_info: StandardContextModel, context: StandardContext) -> ProtestResult:
        """
        调用 LLM，输入 requirements 和 context，判断是否缺少关键信息 (端口/参数)。
        """
        # 1. 准备上下文
        # 我们只关心与"我"有关的上下文，以及我的定义
        context_str = format_context_str(
            context=context, 
            use_function=True, use_ports=True,
            use_path=True, use_parent=True, use_siblings=True,
        )

        prompt = PROTEST_PROMPT.format(
            model_name=model_info.class_name,
            context_str=context_str,
            model_spec=model_info.specification.model_dump_json()
        )

        print(f"      [Protest Check] 🕵️  {model_info.class_name} is evaluating feasibility...")

        # 2. 调用 LLM
        try:
            response = completion(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, # 需要理性判断，低温度
                response_format=FeasibilityAssessment
            )
            content = get_content_strict(response)
            assessment = FeasibilityAssessment.model_validate_json(content)

            # 3. 解析结果
            if assessment.status == "FEASIBLE":
                print(f"      [Protest Result] ✅ ACCEPT. Logic seems sound.")
                return ProtestResult(action=ProtestAction.ACCEPT)
            else:
                # 汇总抗议理由
                reasons = []
                for issue in assessment.critical_issues:
                    reasons.append(f"[{issue.issue_type}] {issue.description} Suggestion: {issue.suggestion}")
                
                full_reason = " | ".join(reasons)
                print(f"      [Protest Result] 🛑 PROTEST! Reasons: {full_reason}")
                return ProtestResult(action=ProtestAction.PROTEST, reason=full_reason)

        except Exception as e:
            # 如果 LLM 调用失败，为了流程稳健性，选择默认通过并报 Warning
            error_msg = f"ProtestAgent internal error: {str(e)}"
            print(f"      [Protest Error] {error_msg}")
            return ProtestResult(action=ProtestAction.ACCEPT, reason=error_msg)