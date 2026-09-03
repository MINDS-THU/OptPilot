import json
import time
from typing import Optional, Tuple
from litellm import completion
from pydantic import BaseModel, Field

# 复用现有的数据结构
from ...base_types import PlanResult
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging

# ==============================================================================
# Judge Prompt: 强调 ROI (投入产出比) 和 Mock 成本
# ==============================================================================
JUDGE_PROMPT = """
## [Task]
You are a Senior QA Architect for a Discrete Event Simulation (DEVS) system.
Your task is to decide whether to run a **"Simulation-Based Unit Test"** for a newly generated Python class.

We want to test logic, but we want to AVOID testing "Mocking Hell", and save the premium time for more valuable tasks.

Do not vote true unless it is really necessary. 

## [Decision Criteria]

### ✅ VOTE "TRUE" (Should Test) if satisfy all of:
1. **Logic Complexity**: The model contains multiple components' interaction.
2. **Feasibility**: It is a pure logic model or uses simple interfaces. It is EASY to mock its inputs.
3. **Coupled**: It is a Coupled model. 

### ❌ VOTE "FALSE" (Skip Test) if satisfy any of:
1. **Triviality**: The model is a model with basic function (e.g. atomic model). 
2. **Mocking Hell**: The model interacts with complex external systems (e.g., raw sockets, heavy external APIs, hardware drivers, complex corrdinator) where writing a correct Mock/Stub is harder/riskier than the model itself. The test can be reserved to later integrated testing phase. (e.g. need four or more mocks to interact with the model)

## [Input Model Info]
- **Name**: {name}
- **Type**: {model_type}
- **Specification**: {spec}

## [Code Preview]
(Truncated for analysis)
```python
{code_snippet}
```

## [Output Requirement]

Analyze the code complexity and mockability. Return your decision in JSON.
"""

class SimulationJudgement(BaseModel):
    should_test: bool = Field(..., description="True if the model is complex enough and feasible to test.")
    reasoning: str = Field(..., description="Brief explanation of complexity vs mockability.")

class SimulationNecessityJudge:
    def __init__(self, model_id: str):
        self.model_id = model_id

    def forward(
        self, 
        model_plan: PlanResult,
        code_content: str
    ) -> bool:
        """
        Main entry point. 
        Returns True if simulation check is recommended.
        """
        return self._apply_llm_judgment(model_plan, code_content)

    def _apply_llm_judgment(self, model_plan: PlanResult, code_content: str) -> bool:
        """
        Call LLM to decide based on logic complexity vs mocking cost.
        """
        # 准备 Prompt 数据
        prompt = JUDGE_PROMPT.format(
            name=model_plan.model_info.class_name,
            model_type=model_plan.type,
            spec=model_plan.model_info.specification.model_dump_json(),
            # 截取前 1500 字符足够判断 import 和主要逻辑结构，省钱
            code_snippet=code_content[:1500] 
        )

        for attempt in range(1):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase2_sim_necessity_judge",
                    target=model_plan.model_info.class_name,
                    attempt=attempt,
                    temperature=0.5, 
                    response_format=SimulationJudgement, 
                )

                content = get_content_strict(response)
                result = SimulationJudgement.model_validate_json(content)
                
                icon = "✅" if result.should_test else "⏭️"
                print(f"   >> [Judge: AI] {icon} {model_plan.model_info.class_name} -> {result.reasoning}")
        
                return result.should_test
            except Exception as e:
                print(f"   >> [Judge: AI] Error: {e}")
                continue

        return False