from typing import List, Literal, Optional, Union, Dict, Any 
from pydantic import BaseModel, Field
from smolagents import Tool
import litellm
from litellm import completion
import json
litellm.drop_params = True
from ...base_types import ModelSpecification, StandardContextModel, StandardContext, format_context_str
from ...utils import get_content_strict

# ==============================================================================
# Tool: ModelSpecFormulator (Formalize Description)
# ==============================================================================

class ModelSpecOutput(BaseModel):
    core_model: ModelSpecification = Field(description="The core model specification.")
    controller_info: str = Field(description="The controller information.")

FORMULATOR_PROMPT = """
## [Role & Objective]
You are a **DEVS Logic Architect**. Your task is to analyze the requirements and produce a strict specification for the core model `{name}`.

**Your Goal**: 
1. **Separate Concerns**: Isolate the *Core Logic* (Model) from the *External Controller*.
    - The External Controller is responsible for stdin reading, external event injecting, arguments parsing, simulation control, and writing list[dict] logs in logger to stdout/stderr as JSONL. 
    - The Core Model is responsible for the internal logic, state, and writing logs to logger. 
2. **Extract Logic**: You are not writing code, but you MUST extract the specific algorithmic rules (model structure, math, probabilities, delays) into the `function` field.
3. **Extract Logging**: If any logging/event_output is required, describe what to log in the `logging` field.  
    - You can add new logging events, but do not change the existing ones.
    - Unless specified, the output should be completed by the core model.

## [Input Data]
**Target Model Name**: `{name}`
**Raw Requirements**: 
{req}

**System Context (Environment)**: 
{context_str}

**Reviewer Feedback**:
{feedback}

## [Field Filling Guidelines (CRITICAL)]

You MUST follow these strict rules when populating the JSON fields:

### `core_model.function` (The Logic Source of Truth)
* **Rule**: You **MUST COPY & TRANSLATE** specific business rules from the Raw Requirements into bullet points.
    * State: "Maintain a balance variable."
    * Math: "Update: balance = balance - amount."
    * Probability: "Logic: 50% chance to pass, 50% to fail."
    * Timing: "Delay: Processing takes exactly 10.0 seconds."
    * Anything else. 
* **Ambiguity**: If requirements conflict, choose the most specific one.

### `core_model.logging`:  
* If the raw requirements specify the output, it should be implemented through logging. Describe them in the `logging` field. Do not create output ports for writing `stdout`. 
    - Special: If the output format (key, event, etc. ) is specified, you must clearly state them, because the output checker will directly parse the logs.

### `core_model.input_ports` / `output_ports` -> `structure`
* **Rule**: If `type` is `dict` or `list`, the `structure` field **CANNOT be vague**.
* **Format**: Use Python-like pseudo-code to define keys and types.
    * *Rejectable*: `structure="User object"`
    * *Acceptable*: `structure="{{'id': int, 'amount': float, 'valid': bool}}"`

### `core_model.input_ports` / `output_ports` -> `protocol`
* **`initial_state`**: Usually "None" for standard event ports. If it's a credit-based system, specify initial credits.
* **`initial_signal`**: Usually "None". Only specify if the model MUST send a message at t=0.

### `controller_info` (The External Logic Container)
* **Rule**: Put **ALL** implementation details related to the Simulation Engine or OS here.
* **Content**: 
    * "Reading from sys.stdin and parsing lines (If any)."
    * "Inject the input data into the core model (if any). "
    * "Parse the arguments and control the simulation to run for a specific simulation time."
    * "Parsing command line arguments (argparse)."
    * "Writing logs from logger to stdout/stderr."
    * The random seed setting is recommended to be set here. 
* **Reason**: Keeping this out of `core_model` prevents the checker from rejecting your design for "Engine Violation".

## [Design Constraints]
- **Time Unit**: You must specify the time unit in the `function` field (e.g., "1 unit = 1 second" or "1 unit is 10 millisecond").
- **Logging**: The model uses an internal logger for output. Do NOT create output ports for logs.
"""

class ModelSpecFormulator:
    def __init__(self, model_id: str = "gpt-4o"):
        super().__init__()
        self.model_id = model_id

    def forward(self, model_name: str, requirements: str, context: StandardContext, feedback_context: Optional[str] = None) -> ModelSpecification:
        feedback = f"\n\n## [DESIGN CONSTRAINTS]\n{feedback_context}\n" if feedback_context else "(No Feedback)"
        context_str = format_context_str(context, use_parent=True, use_siblings=True)
        prompt = FORMULATOR_PROMPT.format(name=model_name, req=requirements, feedback=feedback, context_str=context_str)

        for attempt in range(5):
            try: 
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    response_format=ModelSpecOutput,
                )
                result = get_content_strict(response)
                model_spec = ModelSpecOutput.model_validate_json(result)
                return model_spec.core_model
            except Exception as e:
                print(f"Attempt {attempt + 1}: Error in generating model spec: {e}")
                continue
        
        raise Exception("Failed to generate model spec after multiple attempts")

