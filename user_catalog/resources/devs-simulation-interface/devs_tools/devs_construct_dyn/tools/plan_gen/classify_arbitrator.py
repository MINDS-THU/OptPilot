from typing import Literal, List, Optional, Dict, Any
from pydantic import BaseModel, Field
from smolagents import Tool
import litellm
from litellm import completion
import json
litellm.drop_params = True

from ...base_types import StandardContext, StandardContextModel, format_context_str
from ...utils import get_content_strict

# --- Data Model for Judge Output ---
class ArbitrationResult(BaseModel):
    judge_reason: str = Field(..., description="The judge's final reasoning based on the criteria.")
    final_verdict: Literal["atomic", "coupled"]

# --- The Judge Prompt ---
ARBITRATOR_PROMPT = """
## [Role]
You are a Senior DEVS Systems Architect acting as a **Judge**. 
There is a disagreement between models regarding the architectural type of the system `{name}`.

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [System Context]
(Information about the environment and the overall project goals)
{context_str}

## [Committee Opinions]
Here are the arguments presented by each reviewer:

{opinions_formatted}

## [The Law (Classification Criteria)]
You must judge based on the exact same criteria provided to the original classifiers:

- You should only consider the function described to decide if the model is atomic or coupled. You should not consider the complexity of logging and error handler requirements.
- **Atomic Model Criteria (Must meet ALL to be Atomic):**
  - **Indivisibility:** The logic represents a single, basic action (e.g., Generator, Queue, Router, Delay).
  - **Single Responsibility:** It manages ONE simple state lifecycle. (e.g. Idle -> Active -> Done)
  - **No Internal Flow:** It does not describe a pipeline of distinct steps.
- **Coupled Model Criteria (If ANY are true, it MUST be Coupled):**
  - **Internal Workflow:** The requirement describes a process with multiple distinct stages (e.g., a "Server" that has an internal "buffer" AND a "processing unit").
  - **Component Composition:** The description implies the existence of sub-components.
  - **Complex Logic:** If the state transition logic requires a massive set of variables to track progress.
  - **God Object:** If the model handles input, processing, AND storage logic simultaneously.

## [Task]
Analyze the **Requirements** below (It does not state all the components, so it is allowed to add unmentioned components if necessary). Evaluate the arguments provided above. 
Determine the correct architectural type.
- If "Coupled" votes identified valid reason, rule **Coupled**.
- If "Coupled" votes are based on trivial internal variables or hallucinations, rule **Atomic**.
- **Context Usage**: Use the [System Context] to gauge complexity. If the Project Goal is huge but this model is a deep leaf node, it is more likely to be Atomic unless specified otherwise.

## [Requirements]
These are the functional requirements of the system (Structure not detailed):
{req}
"""

class ModelArbitrator:
    def __init__(self, default_judge_id: str):
        super().__init__()
        self.default_judge_id = default_judge_id

    def forward(self, model_info: StandardContextModel, context: StandardContext, votes_summary: str, judge_model_id: Optional[str] = None) -> ArbitrationResult:
        if judge_model_id is None:
            judge_model_id = self.default_judge_id
            
        try:
            # 2. Format them nicely for the prompt
            formatted_text = votes_summary
            context_str = format_context_str(context,
                use_function=True,use_ports=True,
                use_path=True, use_system_goal=True, use_parent=True, use_siblings=True,
            )

            # 3. Construct Prompt
            prompt = ARBITRATOR_PROMPT.format(
                name=model_info.class_name,
                context_str=context_str,
                opinions_formatted=formatted_text,
                req=model_info.specification.model_dump_json(),
            )
            
            # 4. Call Judge
            response = completion(
                model=judge_model_id,
                messages=[{"role": "user", "content": prompt}],
                drop_params=True, 
                response_format=ArbitrationResult
            )
            response_str = get_content_strict(response)
            return ArbitrationResult.model_validate_json(response_str)
            
        except Exception as e:
            return ArbitrationResult(
                final_verdict="atomic", 
                judge_reason=f"Arbitration Error: {str(e)}"
            )