from smolagents import Tool
import os
from pathlib import Path
import yaml
import litellm
from litellm import completion
import json
litellm.drop_params = True
from ...base_types import PlanResult, StandardContext, StandardContextModel, format_context_str
from ...utils import get_content_strict

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
        
        return json.dumps(all_sm, indent=2)
    except Exception as e:
        print(f"Warning: Failed to process sub-models info: {e}")
        return json.dumps([sm.model_dump_json() for sm in sub_models])

# ==============================================================================
# UNIFIED PROMPT TEMPLATES
# ==============================================================================

GLOBAL_STANDARDS = """
## [Global Standards]
Adhere to the following engineering standards for all model types:

### 1. Imports & Dependencies
- **Whitelist**: Restrict imports to the following packages: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs` (and `xdevs.models`).
- **Project Utils**: Import necessary utilities (e.g., `get_sim_logger`, `get_current_time`) from `devs_project.devs_utils.xxx`. Refer to [Utils] for detailed import statements.
- Other submodels in the project can be imported as needed.

### 2. Data Protocol & Typing
- You are only allowed to use the following Atomic Primitives and Composite Types in ports and arguments. 
- **Atomic Primitives**: Only `int`, `float`, `str`, `bool` instances for all base values.
- **Composite Types**: Only `dict` and `list`.
- **Consistency**: Ensure that all ports and arguments are the same as those stated in the [Specification]. 
- **Recursive Schema Definition**:
    - Recursively define the structure of all composite arguments (in `__init__`) and port data types (in Docstrings).
    - Continue the definition until all fields resolve to Atomic Primitives.
    - **Dictionaries**: Explicitly list every Key name, the Type of its Value, and the explanation of it. 
    - **Lists**: Explicitly state the Type of elements contained in the list.
- Special: only the `parent` argument of the `__init__` method can be of type `Coupled | None`.

### 3. Type Docstring Schema
- **Structure**: Follow this hierarchy for describing types in Class and Method docstrings:
    - Root: `name (type): Description`
    - Dict Keys: Indent 2-4 spaces, `key_name (type): Description`
    - List Items: Indent 2-4 spaces, `- (type): Description`
- *Exception*: For Logging, you can refer to the structure described in the ports section, as they are often the same.
- **Example**:
    ```python
    \"\"\"
    ...
    - in_packet (dict): Network packet.
        header (dict): Protocol header.
            src_ip (str): Source IP.
        payload (str): Data content.
    - batch_updates (list[dict]): Updates.
        - (dict): Single update.
            node_id (int): Target ID.
    \"\"\"
    ```

### 4. Coding Conventions
- **Explicit Configuration**: Define all configuration parameters explicitly in `__init__`. Omit `*args` and `**kwargs`.
- **Logging**: Log all key events using `self.logger.info({{"key": "value"}}, log_type=...)`. The main msg must be a dict. They must have the exact precise structure and keys as the context (especially the system goal). You should explain the structure of the data in the docstring. However, you only need to list the logging happen in this model, those in submodels are strictly forbiddened.
- **Parameter Storage**: Store internal hardcoded parameters (not passed via `__init__`) in a `self.param` dictionary.
"""

ATOMIC_INSTRUCTIONS = """
### [Atomic Model Specifics]
1. **Inheritance**: Inherit from `Atomic`.
2. **Docstring**: The class MUST include a standard class docstring strictly following this format:
    ```python
    class {name}(Atomic):
        \"\"\"
        Function: 
            - ...General function
            - ...Every state, and what to output after the state is over.
        Logging in this model:
            - ...
            - ...
        Input Ports:
          - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
        Output Ports:
          - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
        \"\"\"
    ```
3. **Constructor (`__init__`)**:
    - Signature: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
    - Docstring: should have a docstring describing the arguments, including the detailed type and description. using the following format:
        ```python
        \"\"\"
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            arg_name1 (type): description
        \"\"\"
        ```
    - Steps:
        1. Call `super().__init__(name)`.
        2. Assign `self.parent = parent`.
        3. Initialize logger: `self.logger = get_sim_logger(self)`.
        4. Register Ports: Use `self.add_in_port(Port(type, "name"))` and `self.add_out_port(Port(type, "name"))`.
        5. Initialize State: Set member variables and call `self.hold_in(phase, time)`. 
        6. Log creation: `self.logger.info({{keys: values, ...}}, log_type=...)`
4. **Core Behaviors**:
    - Implement `initialize(self)`: Set initial state. Set phase/sigma using `self.hold_in(phase, time)`. Log initialization.
        - It can not send any output. If you need to send a initial signal (e.g. report you are ready), you can use `self.hold_in(phase, time)` to schedule the event, prepare the payload, and send it in `lambdaf`.
        - If any port has `initial_signal`, the `initialize` method **MUST** schedule an immediate event using `self.hold_in("SOME_STATE", 0)`.
    - Implement `lambdaf(self)`: Only do the output, any other operations should be done in the following `deltint`:
        - Send output via `self.output["port"].add(payload)`.
        - DO NOT change the state, sigma, kpi_counter, etc. Leave that to the following `deltint`.
    - Implement `deltint(self)`: Only do the following:
        - Get the old internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
        - Handle internal timeouts. And update the internal queue / kpi_counter / etc. accordingly. (Because deltint is called right after lambdaf, which means the output of the old state is already sent). 
        - Prepare the payload of the output of the next phase. Make sure the prepared payload is the one used in `lambdaf` of the new phase.
        - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`. If not interrupted, the model will emit the output prepared after sigma time units. 
        - Log events (if needed). 
    - Implement `deltext(self, e)`: Only do the following:
        - Handle external events (`self.input["port"].values`).
        - Get internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
        - Prepare the payload of the next lambdaf. Make sure the prepared payload variable is the one used in `lambdaf`.
        - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`.
        - Log events (if needed). 
    - Implement `exit(self)`: Cleanup and final stats logging.
    - **Event Handling Logic**:
        - **Execution Sequence (CRITICAL)**: `lambdaf` will send outputs before `deltint` schedules the next internal event. Thus, the payload sent in `lambdaf` should be prepared in the previous `deltint`, `deltext`, or `initialize`. 
        - **Confluent Events (`deltcon`)**: By default, internal events (`deltint`) take precedence over external events when they occur simultaneously. Explicitly override the `deltcon(self)` method ONLY IF you need to change this logic (e.g., to process external events first).
        - **Initialization**: Realize the ports.protocol's initialize descriptions: 
            - initial_signal: If a signal or information should be sent at initialization(i.e. protocol.initial_signal), you can use `self.hold_in("INIT", 0)` to schedule the event and send it in `lambdaf`. This is the only way to send a signal at initialization.
            - initial_state: modify the logic and initial values to make sure it is realized. 
"""

COUPLED_INSTRUCTIONS = """
### [Coupled Model Specifics]
1. **Inheritance**: Inherit from `xdevs.models.Coupled`.
2. **Docstring**: The class MUST include a standard class docstring strictly following this format:
    ```python
    class {name}(Coupled):
        \"\"\"
        Function: 
          - ...
          - ...
          - Sub-models: 
            - sub_model_class_name: name=sub_model_instance_name. Brief description.
        Logging in this model:
          - ...
          - ...
        Input Ports:
          - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
        Output Ports:
          - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
        \"\"\"
    ```
3. **Container Logic**: Treat this class as a pure structure container. Implement ONLY `__init__`.
4. **Sub-models Imports**: Use relative imports for sub-models (e.g., `from .folder.file import SubModelName`).
5. **Constructor (`__init__`)**:
    - Signature: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
    - Docstring: should have a docstring describing the arguments, including the detailed type and description. using the following format:
        ```python
        \"\"\"
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            arg_name1 (type): description
        \"\"\"
        ```
    - Steps:
        1. Call `super().__init__(name)`.
        2. Assign `self.parent = parent`.
        3. Initialize logger: `self.logger = get_sim_logger(self)`.
        4. Register Ports: Use `self.add_in_port(...)` and `self.add_out_port(...)`.
        5. Instantiate Components: Create sub-model instances and register them via `self.add_component(instance)`.
        6. Define Couplings: Use `self.add_coupling(src, dst)` for:
            - **EIC**: `self.input["port_name"]` -> `sub.input["port_name"]`
            - **IC**: `sub_a.output["port_name"]` -> `sub_b.input["port_name"]`
            - **EOC**: `sub.output["port_name"]` -> `self.output["port_name"]`
        7. Log creation: `self.logger.info(...)`
    - Note: For steps 5-6, you should refer to Sub-Models to get the right init args names and port names. These information can be used as a correction and supplement to the coupling logic (in case some names are inconsistent). 
"""

MAIN_PROMPT_TEMPLATE = """
## [Task]
Construct a complete Python file containing a **{model_type} DEVS model** named `{name}` using `xdevs.py`.

{global_standards}

{model_specific_instructions}

{feedback}

## [Context Info]
**Sub-Models (for Coupled definitions)**: 
{sub_models}

**System Context**:
(The environment around this model)
(You must especially guarantee the JSONL output requirements are met.)
{context_str}

## [Utils]
{util_desc}

## [Class Definitions]
{definitions}

## [Specification]
The ports, logic, and parameters of the model should strictly follow the specification (including their types, functions), only two can be added / modified: in __init__ args, `name: str`, and `parent: Coupled | None`:
{spec}

## [Reference Example]
Refer to this example for coding style and imports:
{example}

## [Output]
Return the Python code enclosed in <python_code> tags. 
Do not use markdown backticks.

Example:
<python_code>
class MyModel(Atomic):
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
            "atomic": self.tool_dir / "materials/definitions_atomic.md",
            "coupled": self.tool_dir / "materials/definitions_coupled.md",
        }
        self.injected_utils = ["logger", "get_current_time"]
        
        # Example files map
        self.examples_map = {
            "atomic": [
                self.tool_dir / "materials/devs_project/atomic_example_web.py",
            ],
            "coupled": [
                self.tool_dir / "materials/devs_project/coupled_example_web.py",
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

        context_str = format_context_str(context, use_path=True, use_parent=True, use_siblings=True, use_system_goal=True)

        # Build Prompt
        prompt = MAIN_PROMPT_TEMPLATE.format(
            model_type=TYPE_TO_CLASS_NAME[model_plan.type],
            name=model_plan.model_info.class_name,
            global_standards=GLOBAL_STANDARDS,
            model_specific_instructions=specific_instructions,
            sub_models=processed_sub_models,
            spec=model_plan.model_info.specification.to_llm_json(),
            definitions=definitions,
            example=example_code,
            util_desc=util_desc,
            context_str=context_str,
            feedback=feedback,
        )

        full_path = self.working_directory / model_plan.model_info.file_path
        
        last_fail_info = ""
        for attempt in range(3):
            try:
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
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