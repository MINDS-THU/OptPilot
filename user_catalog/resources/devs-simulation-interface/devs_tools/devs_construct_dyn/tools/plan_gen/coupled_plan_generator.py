from typing import List, Literal, Optional, Union, Dict, Any 
from pydantic import BaseModel, Field
from smolagents import Tool
import litellm
from litellm import completion
import json
litellm.drop_params = True
from ...base_types import ModelSpecification, StandardContextModel, StandardContext, SubModelPlan, CoupledDecomposition, format_context_str
from ...utils import get_content_strict

# ==============================================================================
# Tool: CoupledSplitter (Split Coupled Models)
# ==============================================================================

# [CHANGED] Updated Prompt to include Context section and Guidance on using context for child specs
SPLITTER_PROMPT = """
## [Task]
You are a DEVS System Architect. The model `{name}` has been identified as a **COUPLED** model.
Your task is to decompose it into its **DIRECT** children (Sub-models).

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [Architecture Strategy]
Before generating fields, adhere to these structural rules:
- **Hierarchical Granularity**: Decompose only to the **immediate next level**, and seperate to several meaningful children. Do NOT jump to bottom-level atomic components if an intermediate grouping makes sense. e.g. Hospital -> Departments -> Doctors.
- You should always decompose a coupled model into **at least** two children.
- Multiplicity & Instantiation:
    - Rule: The children_plan list defines CLASSES (Types), not instances. You must define a Class EXACTLY ONCE, even if it is used multiple times.
    - Instance Names: The coupling_specification determines the Instance Names.
    - Scenario A (Loops): If you need 5 Servers, define ONE child Server. In coupling, say "Instantiate 5 Servers as server_0 to server_4".
    - Scenario B (Discrete Roles): If you need a "Forward Channel" and a "Backward Channel" with identical logic, define ONE child Channel. In coupling, say "Instantiate Channel as fwd_ch and bwd_ch".
    - Wiring: Always use Instance Names (e.g., server_0, fwd_ch) in your wiring descriptions, NOT the Class Name.
- **Child or Sibling**: Identify if a sub-model should be another one's child or a sibling. If A mainly interacts with the components of B, then A should be B's child, do not make A a sibling of B.
- Provide a clear class name for each child (a valid Python identifier).
- Pass-through Principle: You are a structural architect, not a behavior designer. Your job is to move the existing logic requirements from the Parent to the correct Children, not to create new logic.

## [Field Generation Guidelines]
You must generate a `children_plan` where each child contains the following specific fields. Follow these constraints strictly:

### 1. function (Logic Delegation & Fidelity)
Defines the responsibility boundaries based strictly on the Parent Spec.
- Scope Assignment: Identify which part of the [Parent Model Specification]'s logic is delegated to this child.
- Intelligent Strict Fidelity (Copy Rule): You must copy most of the Parent Spec's constraints (e.g. "Service time = Exp(5)"), especially the calculation, coupling, units. Meanwhile, you should adapt implementation details if they are logically incomplete. 
    - You are only allowed to enrich the flow between childs, and make sure the external interface are exactly the same. 
    - **Example**: If the Spec implies tracking a specific `customer_id` for logging, but defines all ports as simple strings:
        - The **Internal Port** between `Child_A` and `Child_B` SHOULD be upgraded to a `dict` (e.g., `{{'signal': 'next', 'id': 123}}`).
        - The **External Output Port** (if the signal leaves the system) MUST remain a `str` (implying `Child_B` must strip the metadata before sending to the parent's output).
- No Over-Design (Stop Rule): If the [Parent Model Specification] is vague about a detail (e.g., it just says "Process the request" without specifying how), DO NOT invent a mechanism (e.g., do NOT write "Process using FIFO queue" if FIFO wasn't mentioned). Leave undefined details for the next level of decomposition.
- Black Box View: Describe the input-to-output behavior. Do not mention internal sub-components (grand-children) or how they are wired inside this child unless it is explicitly mentioned in the Parent Spec.

### 2. logging
- **Inheritance**: extract the logging information from [Parent Model Specification] and assign them to the specific child responsible for that task. Strictly keep the logging format (including field names, event names, and entity names) consistent with the Parent Spec.
- Describe the logging format in the field `logging` of every child seperatedly, especially the standardized fields and values. Must keep the same format as the Parent Spec (You must specify the field names, etc. in the logging field of the child, because it could not see the parent's logging field).

### 3. input_ports / output_ports
*Defines the interface. Includes Port Encapsulation and Data Schema.*
- **Encapsulation**: Explicitly declare ALL ports. Even if the child is a Coupled model, it needs physical ports to act as a gateway for its internal logic.
- **Coupling Logic**: all the ports found in [Parent Model Specification] must be assigned to one or more specific child responsible for that task. And the internal coupling logic should also be clear. 
    - Definition: Describe the wiring using Instance Names.
    - Every parent input_port should be connected to one or more child input_ports. 
    - Every parent output_port should be connected with one or more child output_ports.
    - **Multiplicity Handling**: If connecting to a Multiplicity Child (e.g. 5 Servers), usage of Index Placeholders (e.g., `server_{{i}}`) in the coupling description is encouraged to denote the pattern.
- **Completness**: The couplings between the children should be complete for their own internal logic. 
- **Multiplicity**: If multiple identical ports exist for one child(e.g., Router has 5 ports connecting to 5 Servers), define the port **ONCE**, and state their instance names clearly using placeholders (e.g., name: "packet_{{i}}_out")
- `structure`: Follow the Data Type Strictness. 
- `protocol`: how the child interacts with its neighbors (Parent/Siblings). This aggregates the Context, Protocol, and Handshake logic. And you should make sure the target port do exists. 
    - **Context Awareness**: Explicitly name the partner (instance name), and the target port in interactions. And you MUST explicitly check the partner's role to prevent mismatches. e.g. "Receives processed frame from [Sibling-Decoder: prossed_frame] or raw data from [Parent: raw_data]." (Discribe the partner from the child's perspective)
    - For EIC and EOC, just extract the ports from the [Parent Model Specification] and assign them to the specific child responsible for that port. 
    - For IC: ensure its I/O ports and External Protocol match perfectly with its **siblings** (the other children you are creating), and explain the following: 
        - **Initial State**: Describe port state at T=0 (e.g., "empty", "waiting", "has initial token").
        - **Initial Signals**: If port sends signals at startup, describe them (e.g., "Sends 5 'ready' signals to [Sibling-Manager: task_in] at T=0").
        - **Flow Description**: Briefly explain data/signal flow and any constraints.
        
### 4. model_init_args
*Defines the configuration parameters.*
- **Inheritance**: Map the numerical/logical parameters found in [Parent Model Specification] to these arguments (e.g., pass `queue_capacity=100` if the parent spec says so).
- Follow the Data Type Strictness. 
- Ensure it include a field `name: str`, which is the unique name of the model instance. 

### Data Type Strictness
1. **Type Descriptions**: 
    - This is the requirements for the `model_init_args`, `input_ports`, and `output_ports`. 
    - Only int, float, bool, str, dict, list are allowed.
2. **Complex Type Schema**:
    - For any input or output port that uses a 'dict' or 'list' type, you MUST explicitly describe the structure in the structure field. This includes listing all keys, their types, and a brief structure for each key. 
    - **No Gold Plating**: strictly adhere to the data fields specified in the [Parent Model Specification] and [System Context]. **DO NOT** add optional fields (like `currency`, `timestamp`, `trace_id`) unless they are explicitly mentioned. Keep the data schema minimal and functional. 
        - *Exception*: If the parent model specification does not specify the data structure, you should implement it. 
    - Example: type="dict", structure="Job info containing keys: 'id' (int): the job id , 'arrival_time' (float): the arrival time of the job , and 'priority' (int): the priority of the job, 0 for top, bigger implies lower priotiry."

### Prevent Cold-Start Deadlocks
**Check for Circular Dependencies**: If there's a closed loop (e.g. (1)Manager send job to Worker after receiving finished job from Worker, (2) A→B→C→A, (3) Queue -> Router -> Processor), you should prevent the cold-start deadlock. Here are some example ways: 
- **Requirement**: For any loop / waiting, ensure it can break the deadlock. Clearly explain the initialization logic in the `protocol` field.
- **Examples**:
    1. **Active Trigger**: "In a closed feedback loop (A↔B), configure Model A to send an initial 'start' signal to Model B at T=0 to kick off the cycle."
    2. **Pre-loaded Credit**: "Initialize the Manager with `credits=N`, allowing it to dispatch N tasks immediately without waiting for the first 'worker_free' signals."
    3. **Early Registration**: "Configure Workers to send a 'register' event to the Router at T=0, ensuring the Router has valid destinations populated before the first packet arrives."
- **Supreme Court Rule**: If [Parent Model Specification] implies a connection that creates a cold-start deadlock (unless explicitly desired), you MUST fix it by ensuring proper initialization.

{feedback}

## [System Context]
This section describes the **EXISTING** environment around `{name}`.
Use this to understand **WHO** connects to `{name}`'s ports and **WHAT** kind of data flows there.
{context_str}

## [Parent Model Specification]
(This is the model `{name}` you are splitting now)
{spec}

"""

class CoupledSplitter:
    def __init__(self, model_id: str = "gpt-4o"):
        super().__init__()
        self.model_id = model_id

    def forward(self, model_info: StandardContextModel, context: StandardContext, feedback_context: Optional[str] = None) -> CoupledDecomposition:
        feedback = f"\n\n## [DESIGN CONSTRAINTS]\n{feedback_context}\n" if feedback_context else ""
        
        # Format Context String
        context_str = format_context_str(
            context=context,
            use_function=True,
            use_logging=False,
            use_ports=True,
            
            use_parent=True,
            use_siblings=True,
            use_path=True,
        )

        prompt = SPLITTER_PROMPT.format(name=model_info.class_name, spec=model_info.specification.model_dump_json(), context_str=context_str, feedback=feedback)

        for attempt in range(5):
            try:
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    response_format=CoupledDecomposition,
                )
                result = get_content_strict(response)
                return CoupledDecomposition.model_validate_json(result)
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                continue
            
        raise Exception("Failed to generate coupled decomposition")