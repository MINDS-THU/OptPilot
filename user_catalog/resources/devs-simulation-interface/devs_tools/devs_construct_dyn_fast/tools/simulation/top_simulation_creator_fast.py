from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import yaml
import json
from .devs_execute import DEVSExecute
from typing import Optional

from ...utils import get_content_strict
from litellm import completion
import litellm

litellm.drop_params = True


class DEVSExecuteWrapper(Tool):
    name = "devs_execute"
    description = (
        "Execute the target DEVS model project within a controlled temporary environment. "
        "The tool captures stdout/stderr, manages timeouts, and provides a basic sandbox to restrict imports to allowed libraries only. "
    )
    inputs = {
        "timeout": {
            "type": "integer",
            "description": "Maximum execution time in seconds (default: 30).",
            "nullable": True,
        },
        "command_args": {
            "type": "string",
            "description": "Command line arguments to pass to the script, as a single string (e.g., '--epochs 10 --lr 0.01').",
            "nullable": True,
        },
        "allowed_libraries": {
            "type": "string",
            "description": "Comma-separated list of allowed root packages (default: numpy,xdevs,logging,math,random,time,collections,itertools).",
            "nullable": True,
        },
        "stdin_content": {
            "type": "string",
            "description": "Content to be passed to the script via standard input (STDIN).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        core: DEVSExecute,
        stdout_file: str,
        stderr_file: str,
        project_path: str,
        main_file: str,
    ):
        super().__init__()
        self.core = core
        self.fixed_args = {
            "stdout_file": stdout_file,
            "stderr_file": stderr_file,
            "project_path": project_path,
            "main_file": main_file,
        }
        self.has_executed = False

    def forward(
        self,
        timeout: int = 30,
        command_args: Optional[str] = None,
        allowed_libraries: str = "numpy,xdevs,logging,math,random,time,collections,itertools",
        stdin_content: Optional[str] = None,
    ) -> str:
        self.has_executed = True
        return self.core.forward(
            timeout=timeout,
            command_args=command_args,
            allowed_libraries=allowed_libraries,
            stdin_content=stdin_content,
            **self.fixed_args,
        )


class SpecificFileSaver(Tool):
    name = "save_simulation_code"
    description = "Saves the provided Python code string to the target file. You do not need to specify the path."
    inputs = {
        "code_content": {
            "type": "string",
            "description": "The complete Python code string to be saved.",
        }
    }
    output_type = "string"

    def __init__(self, target_path: str):
        super().__init__()
        self.target_path = target_path  # 路径在初始化时被“锁死”
        self.has_executed = False

    def forward(self, code_content: str) -> str:
        try:
            # 确保父目录存在
            directory = os.path.dirname(self.target_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)

            with open(self.target_path, "w", encoding="utf-8") as f:
                f.write(code_content)

            self.has_executed = True
            return f"SUCCESS: Code saved to system."
        except Exception as e:
            return f"ERROR: Failed to save code. {str(e)}"


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


# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
SIMULATION_PROMPT_TEMPLATE = """
You are an expert DEVS simulation engineer using the `xdevs` framework.

## **[Task]**
Generate a Python simulation runner script for `{class_name}` using `argparse` for parameterization.

## **[Context]**
- **Target Model Class**: `{class_name}`
- **Target Model File**: `{file_path}` (Relative to the simulation script)
- **Model Specification**:
{spec}
- **Simulation Scenario**: 
You should not deal with the output logic, as it is handled by the model itself.
{scenario}

## **[System Registry]**
Mainly reflects the Top-level Model's information, might contain some information about the sub-models.
{system_info}

## **[Critical Utils & Libraries]**
The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Script Requirements]**
You must construct the script in the following **exact order**.

### 1. Imports
- **General**: Import `Coordinator`, `SimulationClock` from `xdevs.sim`.
- **Utils**: Import `set_global_clock` from `devs_project.devs_utils.devs_context`.
- **Injection (Conditional)**: IF the scenario requires external event injection:
    - Import `ReliableInjectionSystem` from `devs_project.devs_utils.inject`.
    - Import `get_raw_input_content` from `devs_project.devs_utils.inject`.
- **Target Model**: Use a **relative import** for the model class. 
    - Logic: If script is at `runner.py` and model is at `target.py`, use `from .target import {class_name}`.

### 2. Configuration (ArgParse & Input Parsing)
- **Step 2.1**: Initialize `argparse.ArgumentParser`.
    - Create arguments for `{class_name}` initialization parameters and `simulate_time` (or other name like `simulation_time` if specified in the scenario). Make sure the parameters do exists in the model specification. 
    - **CRITICAL**: Set `default` values based on the **Simulation Scenario**.
    - **CRITICAL**: if the args are specified in the `Simulation Scenario`, ensure their names match exactly.
    - Parse the arguments into variables (e.g., `args = parser.parse_args()`).
- **Step 2.2 (Input Parsing)**: IF the Model Specification has input_ports, and **Simulation Scenario** clearly specified them (e.g., "inject X at time T"):
    - If it Simulation Scenario mentioned to read from file / stdin, call `raw_text = get_raw_input_content()` to safely read Stdin. 
    - Implement a helper function (e.g., `parse_schedule(text)`) to parse `raw_text` into a list of event dicts `[{{"time":..., "port":..., "payload":...}}]`.
    - Ensure the parser matches the data format described in the Scenario.

### 3. Initialization (The Logic is Strict)
- **Step 3.1**: Create the clock: `clock = SimulationClock()`.
- **Step 3.2**: **CRITICAL**: Register the clock globally: `set_global_clock(clock)`.
- **Step 3.3**: Instantiate the model `{class_name}`.
    - Ensure you pass the correct arguments (e.g., `name="{class_name}"`, `parent=None`, and other params defined in Step 2).
- **Step 3.4 (Harness Wrapping)**:
    - **IF Injection is used**:
        - Instantiate the harness: `model = ReliableInjectionSystem(name="harness", parent=None, core_model={class_name}_instance, events=parsed_events)`.
        - Note: The `ReliableInjectionSystem` becomes the top-level model to be simulated.
    - **ELSE**:
        - Use the core model directly: `model = {class_name}_instance`.
- **Step 3.5**: Create the Simulator: `sim = Coordinator(model, clock)`.

### 4. Simulation Execution
- Call `sim.initialize()`.
- To avoid missing end-of-horizon internal events at exactly `t==simulate_time`, run with a tiny epsilon horizon:
  - `effective_end = float(simulate_time) + 1e-9`
  - `sim.simulate_time(effective_end)`
  - Keep all emitted business timestamps and KPI semantics anchored to `simulate_time`.
- Call `sim.exit()`.

## **[Reference Code]**
Use this code as your strict template. Do not change the logic flow. 
```python
{example}
```
   
## **[Output Requirement]**
Return the Python code enclosed in <python_code> tags. 
Do not use markdown backticks.

Example:
<python_code>
...
if __name__ == "__main__":
    main()
</python_code>
"""
# ==============================================================================


class TopSimulationCreatorFast(Tool):
    name = "top_simulation_generator"
    description = "Generates a DEVS simulation runner script. Can access a system-wide model registry file to understand component details via the provided tool. Return a JSON description of the arguments."
    inputs = {
        "model_file_path": {
            "type": "string",
            "description": "Path to the top-level model code file.",
        },
        "model_class_name": {
            "type": "string",
            "description": "Class name of the top-level model.",
        },
        "model_spec": {
            "type": "string",
            "description": "The functional specification of the root model.",
        },
        "system_info_file_path": {
            "type": "string",
            "description": "Path to the JSON file containing info for ALL models in the system.",
        },
        "simulation_scenario": {
            "type": "string",
            "description": "Description of the simulation scenario.",
        },
        "save_path": {
            "type": "string",
            "description": "Path to save the simulation script.",
        },
        "stdout_save_path": {
            "type": "string",
            "description": "Path to save the stdout of the simulation runner.",
        },
        "stderr_save_path": {
            "type": "string",
            "description": "Path to save the stderr of the simulation runner.",
        },
    }
    output_type = "string"

    def __init__(
        self,
        read_file_tool: Tool,
        model_id: str = "gpt-4o",
        working_directory: str = "./working_dir",
    ):
        super().__init__()
        self.read_file_tool = read_file_tool
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.example_files = [
            self.tool_dir / sub_path / "devs_project/runner_example_inject.py"
        ]
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = [
            "set_global_clock",
            "injection_tools",
            "get_raw_input_content",
            "logger",
            "get_current_time",
        ]
        self.definitions_file = self.tool_dir / sub_path / "definitions.md"

    def _read_materials(self):
        all_example_content = ""
        definitions_content = ""
        util_desc = ""

        for example_file in self.example_files:
            if example_file.exists():
                with open(example_file, "r") as f:
                    example_content = f.read()
                    all_example_content += example_content

        if self.definitions_file.exists():
            with open(self.definitions_file, "r") as f:
                definitions_content = f.read()

        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"

        return all_example_content, definitions_content, util_desc

    def forward(
        self,
        model_file_path: str,
        model_class_name: str,
        model_spec: str,
        system_info_file_path: str,
        simulation_scenario: str,
        save_path: str,
        stdout_save_path: str,
        stderr_save_path: str,
    ) -> str:
        print(
            f"Generating simulation runner script for model '{model_class_name}' at '{save_path}': {model_spec}"
        )

        example_code, definitions, util_desc = self._read_materials()

        # 1. 准备绝对路径
        full_save_path = self.working_directory / save_path
        abs_save_path = str(full_save_path.resolve())

        # relative to the simulation save path
        model_rel_path = Path(model_file_path).relative_to(Path(save_path).parent)

        try:
            with open(system_info_file_path, "r") as f:
                system_info = f.read()
            try:
                system_info = json.loads(system_info)
                if isinstance(system_info, dict):
                    # 获取最后一个键值对
                    last_key = list(system_info.keys())[-1]
                    system_info = system_info[last_key]
                elif isinstance(system_info, list):
                    system_info = system_info[-1]
            except:
                system_info = system_info
        except:
            code_full_path = (self.working_directory / model_file_path).resolve()
            with open(code_full_path, "r") as f:
                system_info = f.read()

        prompt = SIMULATION_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            file_path=model_rel_path,
            spec=model_spec,
            system_info=system_info,
            scenario=simulation_scenario,
            example=example_code,
            util_desc=util_desc,
        )

        # 5. 运行
        last_fail_info = ""
        full_path = Path(abs_save_path)
        for attempt in range(3):
            try:
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                )
                code = get_content_strict(response)

                code = extract_xml_code(code)

                full_path.parent.mkdir(parents=True, exist_ok=True)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)

                return f"SUCCESS: top-level simulation runner script generated"

            except Exception as e:
                last_fail_info = f"FAILURE: Error creating top-level simulation runner script. Reason: {str(e)}"
                print(f"Attempt {attempt + 1} failed: {str(e)}")

        return last_fail_info
