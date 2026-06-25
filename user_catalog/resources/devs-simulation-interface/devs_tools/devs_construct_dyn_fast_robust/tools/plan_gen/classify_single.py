from typing import List, Literal, Optional, Union, Dict, Any
from pydantic import BaseModel, Field
from smolagents import Tool
import litellm
from litellm import completion
import json

litellm.drop_params = True
from ...base_types import (
    ModelSpecification,
    StandardContextModel,
    StandardContext,
    format_context_str,
)
from ...utils import get_content_strict

# ==============================================================================
# Tool: ModelClassifier (Judge Atomic vs Coupled)
# ==============================================================================


class ModelClassificationResult(BaseModel):
    reasoning: str = Field(
        ...,
        description="Brief explanation of why this classification was chosen. When reasoning, you can change your opinion if you think the model is more complex than initially thought.",
    )
    model_type: Literal["atomic", "coupled", "not_sure"]
    submodels: List[str] = Field(
        default_factory=list,
        description="If coupled or not_sure, list the names of the proposed sub-components. If atomic, leave empty.",
    )


CLASSIFIER_PROMPT = """
## [Task]
You are a DEVS Systems Architect. Analyze the requirements for model `{name}` and determine if it must be **Atomic** or **Coupled**.

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [Requirements]
{req}

## [System Context]
This section describes the **EXISTING** environment around `{name}`.
{context_str}

## [Criteria]
- You should only consider the function described to decide if the model is atomic or coupled. You should not consider the complexity of logging and error handler requirements.
- The requirement does not state all the sub-model details, so it is allowed to add unmentioned components if necessary.
- **Atomic Model Criteria (Must meet ALL to be Atomic):**
  - **Indivisibility:** The logic represents a single, basic action, and could not be divided (e.g., Generator, Queue, Router, Delay).
  - **Single Responsibility:** It manages ONE simple state lifecycle. (e.g. Idle -> Active -> (Other) -> Done)
  - **No Internal Flow:** It does not describe a pipeline of distinct steps.
  - **Simple Logic:** The state transition logic requires a small set of variables to track progress. 
- **Coupled Model Criteria (If ANY are true, it MUST be Coupled):**
  - **Internal Workflow:** The requirement describes a process with multiple distinct stages (e.g., a "Server" that has an internal "buffer" AND a "processing unit").
  - **Component Composition:** The description implies the existence of multiple meaningful sub-components.
  - **Complex Logic:** If the state transition logic requires a massive set of variables to track progress.
  - **God Object:** If the model handles input, processing, AND storage logic simultaneously.
- **Not Sure:** If you are not sure about the model type, you can choose this option and provide your reasoning.

- *Special*: Please distinguish between `A complex model containing multiple departments` and `A department coordinator`. The former is coupled, the latter is atomic (if the router logic is not complex). Distinguish them based on the ports: coordinator must itself have ports to interact with siblings it coordinates.

## [Instruction]
- If you choose **Coupled**, you MUST list the possible `submodels` you identified in the JSON output.
- If you choose **Atomic**, `submodels` should be empty.
"""


class ModelClassifier:
    def __init__(self, model_id: str = "gpt-4o"):
        super().__init__()
        self.model_id = model_id

    def forward(
        self,
        model_info: StandardContextModel,
        context: StandardContext,
        llm_id: Optional[str] = None,
    ) -> ModelClassificationResult:
        # Combine into the context string
        context_str = format_context_str(
            context,
            use_function=True,
            use_path=True,
            use_parent=True,
            use_siblings=True,
        )

        if llm_id is None:
            llm_id = self.model_id
        try:
            prompt = CLASSIFIER_PROMPT.format(
                name=model_info.class_name,
                req=model_info.specification,
                context_str=context_str,
            )
            response = completion(
                model=llm_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format=ModelClassificationResult,
            )
            response_str = get_content_strict(response)
            result = ModelClassificationResult.model_validate_json(response_str)
            return result
        except Exception as e:
            print(f"Error: {str(e)}\n")
            return ModelClassificationResult(
                reasoning=f"Error: {str(e)}", model_type="atomic", submodels=[]
            )
