from smolagents import Tool, CodeAgent
from pathlib import Path
import os
import yaml
import json
from .devs_execute import DEVSExecute
from .result_summary_contract import (
    require_event_trace_contract,
    require_result_summary_contract,
)
from .runner_argument_contract import require_runner_argument_contract
from ..generated_member_contract import require_generated_member_contract
from typing import Optional
from src.llm_resilience import ResilientLiteLLMModel, litellm_retry_options


class DEVSExecuteWrapper(Tool):
    name = "devs_execute"
    description = (
        "Execute the target DEVS model project from a credential-free temporary copy. "
        "Execution uses the credential-free, network-disabled generated-code container boundary. "
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
            "description": "Deprecated compatibility input. The outer isolated runtime owns import and execution policy.",
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
        allowed_libraries: str = "xdevs,logging,math,random,time,collections,itertools,json,sys,pathlib,statistics,dataclasses,typing",
        stdin_content: Optional[str] = None,
    ) -> str:
        self.has_executed = False
        result = self.core.forward(
            timeout=timeout,
            command_args=command_args,
            allowed_libraries=allowed_libraries,
            stdin_content=stdin_content,
            **self.fixed_args,
        )
        uses_suggested_defaults = command_args is None or not command_args.strip()
        self.has_executed = (
            result.startswith("STATUS: SUCCESS") and uses_suggested_defaults
        )
        return result


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


# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
SIMULATION_PROMPT_TEMPLATE = """
You are an expert DEVS simulation engineer using the `xdevs` framework.

## **[Task]**
You must do one step at one code block, do not mix them up.
1. Generate a Python simulation runner script for `{class_name}` using `argparse` for parameterization.
2. **SAVE** the script using the tool `{save_tool_name}`.
3. **SMOKE TEST** Use `{execute_tool_name}` with no command-line overrides so the exact suggested defaults are tested. If it crashes, fix the script and run it again. Once the simulation runs with Exit Code 0 using its defaults, it is complete.
4. **ANALYZE** the `argparse` arguments you created.
5. **RETURN** a structured JSON description of these arguments as your Final Answer.

The code is copied to `/tmp/xxxx/devs_project` to run, so the absolute imports should be `devs_project.*` , so do not regard this as an error. 

## **[Context]**
- **Target Model Class**: `{class_name}`
- **Target Model File**: `{file_path}` (Relative to the simulation script)
- **Model Specification**:
{spec}
- **Simulation Scenario**: 
{scenario}

## **[System Resources]**
- **System Registry**: `{system_info_path}` 
- Use `{tool_name}` to read this if you need to inspect constructor arguments or sub-component details. 

## **[Critical Utils & Libraries]**
The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Script Requirements]**
You must construct the script in the following **exact order**.

### Startup Contract
- The default demonstration MUST have a deterministic startup path and produce
  meaningful observations after simulation time 0. Prefer a root model whose
  source component schedules its own finite first event.
- If the declared root protocol instead requires an external startup input and
  no external schedule is supplied, create exactly one small, schema-valid
  event at simulation time 0 and inject it with `ReliableInjectionSystem`.
  Never call a model port directly or invent an undeclared startup port.

### 1. Imports
- **General**: Import `Coordinator`, `SimulationClock` from `xdevs.sim`.
- **Utils**: Import `set_global_clock` from `devs_project.devs_utils.devs_context`.
- **Event trace**: Import `attach_event_trace` from
  `devs_project.devs_utils.event_trace`.
- **Injection (Conditional)**: IF the scenario supplies external events OR the
  declared root protocol requires the deterministic default startup event:
    - Import `ReliableInjectionSystem` from `devs_project.devs_utils.inject`.
    - Import `get_raw_input_content` from `devs_project.devs_utils.inject` only
      when an external schedule must be read.
- **Target Model**: Use a **relative import** for the model class. 
    - Logic: If script is at `runner.py` and model is at `target.py`, use `from .target import {class_name}`.

### 2. Configuration (ArgParse & Input Parsing)
- **Step 2.1**: Initialize `argparse.ArgumentParser`.
    - Create arguments for `{class_name}` initialization parameters and `simulate_time` (or other name like `simulation_time` if specified in the scenario). Make sure the parameters do exists in the model specification. 
    - **CRITICAL**: Set `default` values based on the **Simulation Scenario**.
    - Treat these defaults as one small, meaningful demonstration scenario for
      a first-time student. Every `add_argument` call must use an optional
      literal `--long-name` and an explicit finite literal scalar `default`
      (`str`, `bool`, `int`, or `float`). The Run form and smoke test both use
      these exact values.
    - Do not use `required=True`, positional arguments, `nargs`, collection
      defaults/types, or list-building actions such as `append`. External
      stdin/file content belongs to the model's existing external-IO path, not
      to a required generated CLI argument.
    - Do not use `type=bool`. For a one-way boolean flag, use
      `action="store_true", default=False` or
      `action="store_false", default=True`; otherwise use a scalar parser.
    - **CRITICAL**: if the args are specified in the `Simulation Scenario`, ensure their names match exactly.
    - Parse the arguments into variables (e.g., `args = parser.parse_args()`).
- **Step 2.2 (Input Parsing)**: IF the Model Specification has input_ports, and **Simulation Scenario** clearly specified them (e.g., "inject X at time T"):
    - If it Simulation Scenario mentioned to read from file / stdin, call `raw_text = get_raw_input_content()` to safely read Stdin. 
    - Implement a helper function (e.g., `parse_schedule(text)`) to parse `raw_text` into a list of event dicts `[{{"time":..., "port":..., "payload":...}}]`.
    - Ensure the parser matches the data format described in the Scenario.
- **Step 2.3 (Default Startup)**: IF the root model's declared protocol requires
  an external input to begin and Step 2.2 did not supply one, create exactly one
  deterministic event at time 0 for that input. Its payload must match the
  declared port structure. Do not add a startup event to an autonomous model.

### 3. Initialization (The Logic is Strict)
- **Step 3.1**: Create the clock: `clock = SimulationClock()`.
- **Step 3.2**: **CRITICAL**: Register the clock globally: `set_global_clock(clock)`.
- **Step 3.3**: Instantiate the core model `{class_name}` as `{class_name}_instance`.
    - Ensure you pass the correct arguments (e.g., `name="{class_name}"`, `parent=None`, and other params defined in Step 2).
- **Step 3.4 (Harness Wrapping)**:
    - **IF Injection is used**:
        - Instantiate the harness: `model = ReliableInjectionSystem(name="harness", parent=None, core_model={class_name}_instance, events=parsed_events)`.
        - Note: The `ReliableInjectionSystem` becomes the top-level model to be simulated.
    - **ELSE**:
        - Use the core model directly: `model = {class_name}_instance`.
- **Step 3.5**: Create the Simulator: `sim = Coordinator(model, clock)`.
- **Step 3.6**: Immediately call `attach_event_trace(sim, model)`. This standard
  generated utility records atomic-model output ports as bounded JSONL when a
  managed result directory is available. Do not replace it with custom logging.

### 4. Simulation Execution
- Call `sim.initialize()` only after `attach_event_trace(sim, model)`.
- To avoid missing end-of-horizon internal events at exactly `t==simulate_time`, run with a tiny epsilon horizon:
  - `effective_end = float(simulate_time) + 1e-9`
  - `sim.simulate_time(effective_end)`
  - Keep all emitted business timestamps and KPI semantics anchored to `simulate_time`.
- Call `sim.exit()`.

### 5. Result Summary (Required)
- Business-event output stays in the DEVS model. The runner additionally writes
  one small post-run summary for students and downstream tools.
- At module scope, declare exactly:
  `OPTPILOT_RESULT_FILE = "summary.json"`.
- Implement `write_simulation_summary(metrics, simulated_time, metric_note=None)`.
  It must be dependency-free and must:
  - Return without writing when `OPTPILOT_SIMULATION_RESULTS_DIR` is absent.
  - Keep only explicitly supplied `bool`, `int`, or finite `float` metric values.
    Convert model counters or NumPy-like scalars explicitly before passing them;
    never serialize NaN or infinity.
  - Create the supplied result directory and atomically write
    `summary.json` as UTF-8 JSON with this shape:
    `{{"schema_version": "devs.simulation-result.v1", "metrics": {{...}},
    "run": {{"completed": true, "simulated_time": <finite number>}}}}`.
  - Include `metric_note` outside `metrics` when supplied.
- After `sim.exit()`, collect stable, domain-meaningful outcome KPIs that the
  generated model really exposes, then call `write_simulation_summary(...)`.
  The System Registry contains a `generated_interface` extracted
  deterministically from each generated class. Before directly accessing a
  model or child attribute, property, or method, read the applicable registry
  entry and use only an exact member listed there. Follow `child_instances`
  mappings when traversing from the root to a child. Never derive a member name
  from prose, domain conventions, or a similar-looking name in another class.
- Use the model specification and exact generated interface to choose metric
  names and extraction expressions. Do not use reflection, attribute-name guessing,
  placeholder values, or a generic `"score"`.
- If the model exposes no trustworthy outcome KPI, write an empty `metrics`
  object and a clear `metric_note` explaining what model state should be exposed
  before optimization. Do not present an input such as the requested horizon as
  an optimization outcome.
- When you do emit metrics, also declare them at module scope, directly below
  `OPTPILOT_RESULT_FILE`, as one literal dict so downstream tools can read the
  contract without running the simulator:
  `OPTPILOT_METRICS = {{"<metric_name>": {{"direction": "maximize" | "minimize",
  "description": "<one short sentence>"}}, ...}}`.
  Keys must exactly match the keys passed to `write_simulation_summary`; the
  first entry should be the primary optimization objective; use plain string
  literals only (no expressions). Omit the declaration entirely when `metrics`
  is empty.


### 6. Policy Hook (only when the specification names an optimizable decision)
- When the user's specification explicitly asks for a tunable/optimizable
  decision policy (for example "the dispatch rule should be improvable" or
  "expose the routing decision as an editable policy"), factor that one
  decision out of the atomic components into a separate module
  `devs_project/policy.py`:
  - `policy.py` defines exactly one top-level, zero-argument
    `create_policy()` returning the policy object; the deciding component
    imports it and calls `policy.run(snapshot)` at each decision point,
    where `snapshot` is a plain dict of documented, JSON-serializable
    fields (list the fields in a docstring inside policy.py).
  - `policy.py` must be deterministic and dependency-free: no imports of
    simulator internals, os, sys, subprocess, socket, pathlib, importlib,
    or random.
  - Declare the hook at module scope in the runner, directly below
    `OPTPILOT_RESULT_FILE`:
    `OPTPILOT_POLICY = {{"file": "devs_project/policy.py",
    "entrypoint": "create_policy",
    "description": "<one short sentence>"}}`.
  Omit the module and the declaration entirely when the specification
  does not ask for an optimizable decision.

## **[Reference Code]**
Use this code as your strict template. Do not change the logic flow. 
```python
{example}
```

## **[Excute requirement]**
1. **Execute & Test**: 
   - Construct valid arguments and input based on your analysis.
   - Call `{execute_tool_name}`.
2. **Debug Loop**:
   - **IF Crash (Exit Code != 0)**: Read the traceback in the output.
   - **Action**: Modify the code using `{save_tool_name}`(be careful! it will fully overwrite the file) and **RE-RUN** `devs_execute` to verify.
3. **Completion**: Once the simulation runs with Exit Code 0, it's ok.
   
## **[Output Requirement]**
After you have successfully saved the code using the tool, your Final Answer must be a JSON list of the arguments you defined. 
You must finish your execution with the following logic:
1. Create a Python list containing the arguments details. 
2. Call final_answer with the JSON dump of this list. Example: 
```python
import json
args_info = [
    {{"arg_name": "--count", "type": "int", "default": 10, "description": "Item count"}},
    {{"arg_name": "--rate", "type": "float", "default": 1.5, "description": "Processing rate"}}
]
final_answer(json.dumps(args_info))
```
"""
# ==============================================================================


class TopSimulationCreator(Tool):
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
            self.tool_dir / sub_path / "devs_project/runner_example.py"
        ]
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = [
            "set_global_clock",
            "attach_event_trace",
            "injection_tools",
            "get_raw_input_content",
            "logger",
            "get_current_time",
        ]
        self.definitions_file = self.tool_dir / sub_path / "definitions.md"

        self.devs_execute_tool = DEVSExecute(working_directory)

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

        # 2. 动态创建“锁定路径”的保存工具
        current_save_tool = SpecificFileSaver(target_path=abs_save_path)
        execute_wrapper = DEVSExecuteWrapper(
            core=self.devs_execute_tool,
            stdout_file=stdout_save_path,
            stderr_file=stderr_save_path,
            project_path=str(Path(save_path).parent),
            main_file=str(Path(save_path).name),
        )

        # Instantiate the model and the agent
        model = ResilientLiteLLMModel(
            model_id=self.model_id,
            temperature=0.1,
            **litellm_retry_options(),
        )
        agent = CodeAgent(
            tools=[self.read_file_tool, current_save_tool, execute_wrapper],
            model=model,
            additional_authorized_imports=[
                "os",
                "sys",
                "logging",
                "pathlib",
                "json",
                "yaml",
            ],
            max_steps=30,
            max_print_outputs_length=4000,
        )

        # relative to the simulation save path
        model_rel_path = Path(model_file_path).relative_to(Path(save_path).parent)

        prompt = SIMULATION_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            file_path=model_rel_path,
            spec=model_spec,
            system_info_path=system_info_file_path,
            tool_name=self.read_file_tool.name,
            scenario=simulation_scenario,
            save_tool_name=current_save_tool.name,
            example=example_code,
            util_desc=util_desc,
            execute_tool_name=execute_wrapper.name,
        )

        system_info_path = Path(system_info_file_path)
        if not system_info_path.is_absolute():
            system_info_path = self.working_directory / system_info_path
        try:
            system_registry = json.loads(
                system_info_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            system_registry = {}

        # 5. 运行
        max_retries = 3
        current_input = prompt
        should_reset = True
        for attempt in range(max_retries):
            print(f"Attempt {attempt + 1} of {max_retries}")
            current_save_tool.has_executed = False
            execute_wrapper.has_executed = False
            result_json_string = str(agent.run(current_input, reset=should_reset))
            validation_errors = []

            # B1. 校验是否保存了文件
            if not current_save_tool.has_executed:
                validation_errors.append(
                    f"CRITICAL ERROR: You forgot to save the code! "
                    f"You MUST call the tool '{current_save_tool.name}' to write the file to disk."
                )
            else:
                try:
                    generated_source = full_save_path.read_text(encoding="utf-8")
                    require_result_summary_contract(
                        generated_source,
                        filename=str(full_save_path),
                    )
                    require_event_trace_contract(
                        generated_source,
                        filename=str(full_save_path),
                    )
                    require_generated_member_contract(
                        generated_source,
                        system_registry,
                        model_class_name,
                        filename=str(full_save_path),
                    )
                    require_runner_argument_contract(
                        generated_source,
                        filename=str(full_save_path),
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    validation_errors.append(f"CRITICAL ERROR: {exc}")

            if not execute_wrapper.has_executed:
                validation_errors.append(
                    f"CRITICAL ERROR: You must prove the suggested scenario works. "
                    f"Call '{execute_wrapper.name}' without command_args and fix "
                    "the runner until that default execution succeeds."
                )

            # B2. 校验返回格式
            try:
                result_json = json.loads(result_json_string)
                if not isinstance(result_json, list):
                    validation_errors.append(
                        f"CRITICAL ERROR: The return value is not a list. "
                        f"Please return a list of args."
                    )
            except json.JSONDecodeError as e:
                validation_errors.append(
                    f"CRITICAL ERROR: The return value is not a valid JSON string. "
                    f"Please return a JSON string."
                )

            # 6. 验证并返回
            if not validation_errors:
                return str(result_json_string)
            else:
                print("\n".join(validation_errors))
                current_input = "\n".join(validation_errors) + "\n" + current_input
                should_reset = False

        raise Exception(
            "Failed to generate the simulation script after multiple attempts."
        )
