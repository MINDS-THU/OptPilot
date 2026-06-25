from smolagents import Tool
import os
from pathlib import Path
import yaml
import time
import litellm
from litellm import completion
import json
litellm.drop_params = True
from ...base_types import PlanResult, StandardContext, StandardContextModel, format_context_str
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging

import ast
import re

def extract_xml_code(text):
    start_tag = "<python_code>"
    end_tag = "</python_code>"
    
    if start_tag in text and end_tag in text:
        # rindex 找最后一个开始标签（防止模型输出了多个版本）
        start_index = text.rindex(start_tag) + len(start_tag)
        end_index = text.find(end_tag, start_index)
        code = text[start_index:end_index].strip()
        
        # 同样进行 ast.parse 检查...
        return code
    raise ValueError("No <python_code> tags found")

def process_sub_models(sub_models: list[StandardContextModel], target_file_path: Path) -> str:
    """Calculates relative paths for imports if sub_models_info is provided. And formulates the sub_models_info into a string."""
    if not sub_models or sub_models is None:
        return "N/A"
    target_file_path = Path(target_file_path)
        
    try:
        target_dir = target_file_path.parent
        all_sm = [sm.model_dump() for sm in sub_models]
        for sm in all_sm:
            sm_path = Path(sm["file_path"])
            rel_path = os.path.relpath(str(sm_path), str(target_dir))
            sm['relative_file_path'] = rel_path.replace("\\", "/")
            sm.pop("file_path")
            sm.pop("logic_path")
        
        return json.dumps(all_sm)
    except Exception as e:
        print(f"Warning: Failed to process sub-models info: {e}")
        return json.dumps([sm.model_dump_json() for sm in sub_models])

# ==============================================================================
# UNIFIED PROMPT TEMPLATES
# ==============================================================================

GLOBAL_STANDARDS = """
## [Global Standards - STRICT]
1. **Imports**: Whitelist: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs` (and `xdevs.models`). Use `devs_project.devs_utils.xxx` for project utilities.
2. **Typing**: Use ONLY `int`, `float`, `str`, `bool`, `dict`, and `list` for ports and arguments.
3. **Strict Consistency**: 
    - Ports MUST exactly match the names and types in the [Specification]. Do NOT add, remove, or rename ports.
    - `__init__` arguments MUST exactly match the [Specification]. Do NOT add `*args` or `**kwargs`.
    - Required event names and required payload keys in [Specification] are HARD requirements. Do NOT replace required events with near-synonyms.
4. **Logging**: 
    - Log events EXACTLY as specified using `self.logger.info({"key": "value"}, log_type=...)`. 
    - The main message MUST be a dictionary. 
    - DO NOT log events that belong to sub-models.
    - For auxiliary/debug logs, avoid reusing required business event names in the `event` key unless the payload fully satisfies that required schema.
5. **Output Strategy**:
    - Each module logs its own business events locally using `self.logger.info(...)`. Follow the plan's output architecture — if the plan included a dedicated output module, route events to it through ports; otherwise, log locally.
    - Use ports for data transfer between modules as defined in the plan. Do not add extra output-only ports beyond what the plan specifies.
6. **Log Field Name Convention**: The Specification defines the exact field names for log records (e.g., which field identifies the source module, which fields are in the payload). Use those exact field names and values from the Specification. If a Specification says the module identifier is "reception", use "reception" — do not substitute the Python class name or change casing.
7. **Clean Code**: Store internal hardcoded parameters in a `self.param` dictionary. Write minimal code. Do NOT create unnecessary helper methods.
"""

ATOMIC_INSTRUCTIONS = """
### [Atomic Core Rules - STRICT]
0. Must import: `from xdevs.models import Atomic, Coupled, Port`. Inherit from `Atomic`.
1. `initialize()`: Set initial state using `self.hold_in(phase, sigma)`. DO NOT send output here. If initialization requires an immediate output, schedule it with `self.hold_in("INIT", 0)` and send it in `lambdaf`.
2. `deltint()`: Update state and call `self.hold_in(phase, sigma)`. Prepare the payload variable for the NEXT output. DO NOT use `self.output[...].add()` here!
3. `deltext(e)`: Read inputs via `for packet in self.input["port"].values:`. Deduct elapsed time if staying in same phase `self.hold_in(self.phase, self.ta() - e)`.
4. `lambdaf()`: THIS IS THE ONLY PLACE YOU CAN OUTPUT. Use `self.output["port"].add(payload)`. DO NOT change state or sigma here.
5. `exit()`: release the resource, write potential logs. 
6. **Logging Requirements**: Log business events locally using `self.logger.info(...)`. Ensure payload keys and values match the Specification exactly.
7. **Ports and Init Args**: Register all ports and `__init__` arguments exactly as stated in the Specification. `__init__` must always start with `(self, name: str, parent: Coupled | None, ...)`.
8. **Execution Sequence (CRITICAL)**: `lambdaf` will send outputs before `deltint` schedules the next internal event. Thus, the payload sent in `lambdaf` should be prepared in the previous `deltint`, `deltext`, or `initialize`. 
9. **Confluent Events (`deltcon`)**: By default, internal events (`deltint`) take precedence over external events when they occur simultaneously. Explicitly override the `deltcon(self)` method ONLY IF you need to change this logic (e.g., to process external events first).
"""

COUPLED_INSTRUCTIONS = """
### [Coupled Core Rules - STRICT]
0. Must import: `from xdevs.models import Coupled, Port`. Inherit from `xdevs.models.Coupled`. Use relative imports for sub-models (e.g., `from .folder.file import SubModelName`).
1. **Container Logic**: Treat this class as a PURE structure container. Implement ONLY `__init__`. NO state machines, NO event handlers, NO custom methods.
2. **Constructor (`__init__`) Steps**:
    - Signature MUST start exactly with `(self, name: str, parent: Coupled | None, ...)`.
    - Step 1: `super().__init__(name)` and `self.parent = parent`.
    - Step 2: `self.logger = get_sim_logger(self)`.
    - Step 3: Register Ports using `self.add_in_port()` and `self.add_out_port()`.
    - Step 4: Instantiate Components and register them via `self.add_component(instance)`.
    - Child constructor arguments must already contain effective scenario defaults required at runtime; do NOT depend on mutating parent `self.param` after child creation.
    - Step 5: Define Couplings using `self.add_coupling(src, dst)`. Just implement the mentioned couplings(If name slightly differs, you can adjust the coupling wisely). Connections are in 3 types: (1)EIC: self input -> sub-model input; (2)IC: some-sub-model output -> some-sub-model input; (3)EOC: sub-model output -> self output.
    - Step 6: Log creation.
3. **Coupling Precedence**: [Context Info] is the SINGLE SOURCE OF TRUTH for sub-model names and port names. If the [Specification] differs, you MUST follow the [Context Info].
4. **Runtime Multiplicity (CRITICAL)**: If [Specification] or [Context Info] includes dynamic counts (e.g., `arg:num_x`), instantiate ALL required child instances with deterministic names like `<Group>_<index>` and build couplings programmatically. Do NOT collapse to one representative instance.
"""

MODEL_SKILLS_STDIN = """
**Standard Input Generator Pattern (CRITICAL)**
If this atomic model's role is to read simulated events from `stdin`, YOU MUST NOT use `input()` or `while True`. You MUST implement the following lazy-read state machine:
    - **In `initialize()`**: Set `self.state['iterator'] = iter(sys.stdin)`. Try to read the first line using `line = next(self.state['iterator'], None)`. Parse the `timestamp` and `payload`. Use the parsed absolute timestamp directly as sigma (e.g., `sigma = max(0.0, timestamp - get_current_time())`). Do NOT subtract offsets or normalize times. Store the payload in `self.state['next_event']` and call `self.hold_in("ACTIVE", sigma)`. If no line, `self.passivate()`.
    - **In `lambdaf()`**: Output `self.state['next_event']` via `self.output["port"].add(...)`.
    - **In `deltint()`**: Try to read the next line using `line = next(self.state['iterator'], None)`. If EOF, `self.passivate()`. If valid, parse the new `timestamp`, use the absolute timestamp directly to calculate sigma, store the new payload, and call `self.hold_in("ACTIVE", sigma)`.
"""

MAIN_PROMPT_TEMPLATE = """
## [Task]
Construct a complete Python file containing a **{model_type} DEVS model** named `{name}` using `xdevs.py`.

{global_standards}

{model_specific_instructions}

{model_skills}

{feedback}

## [Context Info]
**Sub-Models (for Coupled definitions)**: 
{sub_models}

**System Context**:
(The environment around this model)
{context_str}

## [Utils]
{util_desc}

## [Class Definitions]
{definitions}

## [Specification]
The ports, logic, logging dict keys, and parameters of the model should strictly follow the specification (including their types, functions), only two can be added / modified: in __init__ args, `name: str`, and `parent: Coupled | None`:
{spec}

## [Reference Example]
Refer to this example for coding style and imports:
{example}

## [Output]
Return the Python code enclosed in <python_code> tags. 
Do not use markdown backticks.

Example:
Think step by step, decompose the requirements and state machine.
Finally the enclosed code.
<python_code>
import ...
class MyModel(Atomic or Coupled):
    ...
</python_code>
"""

# \==============================================================================

TYPE_TO_CLASS_NAME = {
    "atomic": "Atomic",
    "coupled": "Coupled",
}

class ModelCreator:
    def __init__(self, model_id: str, working_directory: str = "./working_dir"):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)
        
        # Define material paths
        self.tool_dir = Path(__file__).parent.parent.parent
        print(f"Tool directory: {self.tool_dir}")
        self.util_desc_file = self.tool_dir / "materials/util_desc.yaml"
        self.definitions_files = {
            "atomic": self.tool_dir / "materials/definitions_atomic_fast.md",
            "coupled": self.tool_dir / "materials/definitions_coupled_fast.md",
        }
        self.injected_utils = ["logger", "get_current_time"]
        
        # Example files map
        self.examples_map = {
            "atomic": [
                self.tool_dir / "materials/devs_project/atomic_example_fast.py",
            ],
            "coupled": [
                self.tool_dir / "materials/devs_project/coupled_example_fast.py",
            ]
        }

    def _read_materials(self, model_type: str):
        example_content = ""
        definitions_content = ""
        util_desc = ""
        
        # Load Examples based on type
        target_examples = self.examples_map.get(model_type, [])
        for example_file in target_examples:
            if example_file.exists():
                with open(example_file, "r") as f:
                    content = f.read()
                    example_content += f"```python\n{content}\n```\n"
        
        # Load Definitions
        definitions_file = self.definitions_files.get(model_type, None)
        if definitions_file:
            definitions_file = Path(definitions_file)
            if definitions_file.exists():
                with open(definitions_file, "r") as f:
                    definitions_content = f.read()
        
        # Load Utils
        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"
        
        print(f"length of example_content: {len(example_content)}, definitions_content: {len(definitions_content)}, util_desc: {len(util_desc)}")
        
        return example_content, definitions_content, util_desc

    def forward(self, model_plan: PlanResult, context: StandardContext, feedback: str) -> str:

        if model_plan.type not in ["atomic", "coupled"]:
            return f"FAILURE: Invalid model_type '{model_plan.type}'. Must be 'atomic' or 'coupled'."

        # Prepare Materials
        example_code, definitions, util_desc = self._read_materials(model_plan.type)
        
        # Select Specific Instructions
        specific_instructions = ATOMIC_INSTRUCTIONS if model_plan.type == "atomic" else COUPLED_INSTRUCTIONS
        
        # Process Sub-models (Coupled Only logic applied via Utils, but safe to run for both)
        processed_sub_models = process_sub_models(model_plan.children_plan, model_plan.model_info.file_path)

        context_str = format_context_str(context, use_path=True, use_parent=True, use_siblings=True, use_global_plan=True)

        # Build Prompt
        model_spec = model_plan.model_info.specification.to_llm_json()
        if model_plan.coupling_specification:
            model_spec += f"\n**Coupling Specification (basicly follow these couplings)**:\n{model_plan.coupling_specification}\n"
            
        # prepare skills
        if model_plan.type == "atomic":
            model_skills = MODEL_SKILLS_STDIN
        else:
            model_skills = ""
            
        prompt = MAIN_PROMPT_TEMPLATE.format(
            model_type=TYPE_TO_CLASS_NAME[model_plan.type],
            name=model_plan.model_info.class_name,
            global_standards=GLOBAL_STANDARDS,
            model_specific_instructions=specific_instructions,
            sub_models=processed_sub_models,
            spec=model_spec,
            definitions=definitions,
            example=example_code,
            util_desc=util_desc,
            context_str=context_str,
            feedback=feedback,
            model_skills=model_skills,
        )

        full_path = self.working_directory / model_plan.model_info.file_path
        
        last_fail_info = ""
        for attempt in range(5):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase2_code_generation",
                    target=model_plan.model_info.class_name,
                    attempt=attempt,
                    temperature=0.5
                )
                code = get_content_strict(response)
                
                code = extract_xml_code(code)
                
                full_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)
                
                return f"SUCCESS: {model_plan.type} model '{model_plan.model_info.class_name}' created at '{full_path}'."
                
            except Exception as e:
                last_fail_info = f"FAILURE: Error creating {model_plan.type} model '{model_plan.model_info.class_name}'. Reason: {str(e)}"
                print(f"Attempt {attempt + 1} failed: {str(e)}")
                
        return last_fail_info
