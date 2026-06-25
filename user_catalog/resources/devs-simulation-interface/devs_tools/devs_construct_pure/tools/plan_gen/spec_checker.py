
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
import litellm
from litellm import completion
litellm.drop_params = True

from ...base_types import PlanResult, StandardContext, ModelSpecification, format_context_str
from ...utils import get_content_strict

# ==============================================================================
# 1. Logic 3: SpecValidator (Validates ModelSpecification)
# ==============================================================================

class SpecIssue(BaseModel):
    category: Literal["noisy", "ambiguity", "consistency", "immutability", "unknown"] = Field(
        ..., 
        description=(
            "- 'noisy': The spec contains irrelevant details (simulation time, engine config, visualization)."
            "- 'ambiguity': Complex types (dict/list/custom) missing strict descriptions of structure/keys.\n"
            "- 'consistency': Ports do not match the function logic or missing necessary IO.\n"
            "- 'immutability': Modified variables/types that were explicitly fixed in requirements."
        )
    )
    severity: Literal["CRITICAL", "WARNING"]
    description: str
    suggestion: str

class SpecReview(BaseModel):
    is_valid: bool
    issues: List[SpecIssue] = []
    feedback_summary: Optional[str] = None

SPEC_CHECK_PROMPT = """
## [Task]
You are a **Senior DEVS Architect** acting as a **Pragmatic Quality Gatekeeper**.
Your task is to review a `ModelSpecification` against the `Raw Requirements` to ensure it is **implementable** and **logically complete**.

**Core Philosophy**: 
You are validating the **Blueprint (Interface & Logic)**, not the **Construction (Internal Code)**.
You must distinguish between "Critical Flaws" (Blockers) and "Valid Design Choices" (Passable).
Only say NO if the design falls into the **Rejection Criteria**, and do not meet any of the **Arbitration Filters**.

## [Rejection Criteria (When to say NO)]
Reject (`is_valid=False`) ONLY if these **BLOCKERS** exist:

1. Logic Vacuum: The `function` field is empty or completely misses a core business rule.
    * Blocker: Req says "Delay 10s and 50% fail probability", but Spec `function` says generic "Process item". (Missing Math/Time).
    * Pass: If the main core logic is covered. 
2. Flawed Output: The output structures are not clearly defined in the `logging` field.
    * Blocker: If the output format is specified in the Raw Requirements, but they are not clearly stated in `logging`. 
3. Interface Disconnect: 
    * Blocker: Missing a Port required to talk to a neighbor in the `System Context`.
    * Blocker: A port is defined as `dict` or `list` but `structure` is empty/vague (e.g., just "data"). 
4. Engine Violation: 
    * Blocker: The Spec explicitly defines ports/logic for `sys.stdin`, or `argparse`. 
    * Blocker: The Spec defines external ports for logging / output (They must be handled by the logger, not the port)
    * Pass: For initial args, it can define corresponding `input_ports` and `model_init_args` to handle the parsed args and stdin. But do not need to handle the simulation control args like simulation duration, do not report if it is missing.

## [Arbitration Filters]
Before rejecting, check these **FILTERS** to see if the design is **valid**:

### Filter 1: The "Black Box" Principle (Interface vs. Internals)
* **Concept**: Requirements often describe internal workflows (e.g., "Step A -> Step B -> Step C").
* **Rule**: If the Spec defines the correct **Global Input** (Step A) and **Global Output** (Step C), it is **VALID**.
* **Do NOT Reject** if internal connections (A->B) are missing from the `output_ports`. Those are internal state transitions, not external ports.
    * *Example (Factory)*: Requirement says "Machine cuts wood -> paints wood". Spec has `in_raw_wood` and `out_painted_wood`. -> **PASS**.
    * *Example*: Do not ask for connections inside the model unless it is literally specified in the requirements.

### Filter 2: The "Controller Boundary" Principle (Model vs. Environment)
* **Concept**: The Model is a passive entity inside a simulation. It does not own the Operating System.
* **Rule**: The Model MUST NOT handle CLI args, raw `stdin` parsing, or global simulation time.
    - The External Controller is responsible for stdin reading, external event injecting, arguments parsing, simulation control, and writing list[dict] logs in logger to stdout/stderr as JSONL. 
    - The Core Model is responsible for the internal logic, state, and writing logs/output dicts to logger. 
    - The logger is specially designed for log and output. It will output to `stdout`, so do not report if the spec does not define `sys.stdout`.
* **Do NOT Reject** if the spec ignores requirements about input handling.

### Filter 3: The "Benefit of the Doubt" (Ambiguity Resolution)
* **Concept**: Requirements are often vague about specific algorithms (e.g., "Queue requests", "Verify password").
* **Rule**: If the Requirements do not specify the *exact algorithm* (FIFO/LIFO, Retry Limit), **ANY reasonable interpretation** in the Spec is acceptable.
    * *Example (Server)*: Requirement says "Handle multiple requests". Spec says "Process sequentially". -> **PASS** (Don't demand complex concurrency unless specified).

## [Feedback Generation Rules]
If critical issues are found, you must generate a `feedback_summary` intended for a fresh, unaware generator.
- **Crucial Context**: The new generator has NOT seen the previous failed result. It only sees the original requirements and your feedback as a "Hint" or "Addendum".
- **Guidelines**:
    1. **Formulate as Requirements**: Do NOT describe what went wrong. Instead, describe what **MUST** be done correctly.
    2. **No Meta-Talk**: Do NOT mention "previous attempt", "errors", "correction", "analysis", or "you forgot".
    3. **Be Explicit**: Provide concrete directives based on the failures you detected. Do NOT mention those instructions they already satisfied.
    4. **Focus on Model Responsibilities**: Emphasize what the model must do, not external formatting.
- **Examples**:
    - *Bad (Refers to past)*: "The dict structure was empty."
    - *Good (Forward-looking)*: "If using `dict` or `list` types, you MUST explicitly describe their structure (keys and value types) in the structure field."

## [Input]
**Raw Requirements**: 
{req}

**System Context**:
{context_str}

**Specification**: 
{spec}
"""

class SpecValidator:
    def __init__(self, model_id: str = "gpt-4o"):
        super().__init__()
        self.model_id = model_id

    def forward(self, model_name: str, model_spec: ModelSpecification, requirements: str, context: StandardContext) -> SpecReview:
        context_str = format_context_str(context, use_parent=True, use_siblings=True)
        prompt = SPEC_CHECK_PROMPT.format(
            req=requirements, 
            spec=model_spec.model_dump_json(),
            context_str=context_str,
            name=model_name
        )

        review = None
        for attempt in range(5):
            try:
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    response_format=SpecReview
                )
        
                result = get_content_strict(response)
                review = SpecReview.model_validate_json(result)
                break
            except Exception as e:
                print(f"Error: {e}, attempt: {attempt}")
                continue
            
        if review is None:
            raise Exception("Failed to validate spec")

        if not any(i.severity == "CRITICAL" for i in review.issues):
            review.is_valid = True
        else:
            review.is_valid = False
            
        return review


