from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import yaml
import json
from .devs_execute import DEVSExecute
from typing import Optional

from ...utils import get_content_strict
from .result_summary_contract import (
    require_event_trace_contract,
    require_result_summary_contract,
)
from .runner_argument_contract import require_runner_argument_contract
from ..generated_member_contract import require_generated_member_contract
from ..generated_python_response import extract_generated_python_response
from src.llm_resilience import litellm_retry_options
from litellm import completion
import litellm

litellm.drop_params = True


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
    return extract_generated_python_response(
        text,
        filename="<generated_runner>",
        artifact_label="runner",
    )


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
Business-event output remains the model's responsibility. The runner must still
write the small, post-run result summary described below.
{scenario}

## **[System Registry]**
Mainly reflects the Top-level Model's information, might contain some information about the sub-models.
{system_info}

## **[Critical Utils & Libraries]**
The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Script Requirements]**
You must construct the script in the following **exact order**.

### Runner Scope
- The runner only parses command line arguments, creates the global clock, instantiates the root model, optionally wraps it with the standard deterministic startup harness described below, creates the Coordinator, and calls initialize/simulate/exit.
- Do NOT read or consume stdin in the runner. If any model uses `external_io.target="stdin"`, that model is responsible for reading stdin itself.
- The default demonstration MUST have a deterministic startup path and produce meaningful observations after simulation time 0. Prefer an autonomous source model that schedules its own finite first event. If the root model's declared protocol instead requires an external startup input, create exactly one small, schema-valid demonstration event for that declared input and wrap the root model with `ReliableInjectionSystem`. Do not invent undeclared ports, build a general business input stream, or call model ports directly.
- Do NOT implement business output, logging, or arbitrary file writing in the
  runner unless the root model specification explicitly requires it. The only
  standard exceptions are the event-trace attachment and exact result-summary
  contract below.

### 1. Imports
- **General**: Import `Coordinator`, `SimulationClock` from `xdevs.sim`.
- **Utils**: Import `set_global_clock` from `devs_project.devs_utils.devs_context`.
- **Event trace**: Import `attach_event_trace` from
  `devs_project.devs_utils.event_trace`.
- **Startup injection (Conditional)**: Import `ReliableInjectionSystem` from
  `devs_project.devs_utils.inject` only when the root model requires a declared
  external startup input for the default demonstration.
- **Target Model**: Use a **relative import** for the model class. 
    - Logic: If script is at `runner.py` and model is at `target.py`, use `from .target import {class_name}`.

### 2. Configuration (ArgParse)
Initialize `argparse.ArgumentParser`: 
- Create arguments for `{class_name}` initialization parameters and `simulate_time` (or other name like `simulation_time` if specified in the scenario). 
- **CRITICAL**: You MUST check the Model Specification or System Registry to determine the EXACT `model_init_args` of the target model class. Only create argparse arguments for those exact parameters. Do NOT invent extra parameters. Do NOT pass simulation-level configs (like test_name) as model init args unless the model's __init__ explicitly accepts them.
- **CRITICAL**: Set `default` values based on the **Simulation Scenario**.
- Treat these defaults as one small, meaningful demonstration scenario for a
  first-time student. Every `add_argument` call must use an optional literal
  `--long-name` and an explicit finite literal scalar `default` (`str`, `bool`,
  `int`, or `float`). The Run form and automatic validation use these exact
  values.
- Do not use `required=True`, positional arguments, `nargs`, collection
  defaults/types, or list-building actions such as `append`. External
  stdin/file content belongs to the model's existing external-IO path, not to a
  required generated CLI argument.
- Do not use `type=bool`. For a one-way boolean flag, use
  `action="store_true", default=False` or
  `action="store_false", default=True`; otherwise use a scalar parser.
- **CRITICAL**: if the args are specified in the `Simulation Scenario`, ensure their names match exactly.
- Parse the arguments into variables (e.g., `args = parser.parse_args()`).

### 3. Initialization (The Logic is Strict)
- **Step 3.1**: Create the clock: `clock = SimulationClock()`.
- **Step 3.2**: **CRITICAL**: Register the clock globally: `set_global_clock(clock)`.
- **Step 3.3**: Instantiate the core model `{class_name}`: `core_model = {class_name}(...)`.
    - Ensure you pass the correct arguments (e.g., `name="{class_name}"`, `parent=None`, and other params defined in Step 2).
- **Step 3.4 (Startup Harness)**:
    - Normally use the autonomous core directly: `model = core_model`.
    - Only when the declared root input is required to start the demonstration,
      create one deterministic event at simulation time 0 whose port and payload
      exactly match that input's schema, then set `model = ReliableInjectionSystem(name="harness", parent=None, core_model=core_model, events=demo_events)`.
- **Step 3.5**: Create the Simulator: `sim = Coordinator(model, clock)`.
- **Step 3.6**: Immediately call `attach_event_trace(sim, model)`. This standard
  generated utility records atomic-model output ports as bounded JSONL when a
  managed result directory is available. Do not replace it with custom logging.

### 4. Simulation Execution
- Call `sim.initialize()` only after `attach_event_trace(sim, model)`.
- To avoid missing end-of-horizon internal events at exactly `t==simulate_time`, run with a tiny epsilon horizon:
  - Determine a numeric horizon in the simulation clock's unit from the scenario arguments.
  - If the CLI duration is already numeric, use `numeric_horizon = float(simulate_time)`.
  - If the CLI duration is a formatted string such as `HH:MM:SS:mmm`, parse it explicitly into numeric simulation seconds first. Do NOT call `float()` directly on a formatted duration string.
  - `effective_end = numeric_horizon + 1e-9`
  - `sim.simulate_time(effective_end)`
  - Keep all emitted business timestamps and KPI semantics anchored to `simulate_time`.
- Call `sim.exit()`.

### 5. Result Summary (Required)
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
  generated model really exposes (for example completed jobs, throughput, lost
  sales, average inventory, delay, or cost), then call
  `write_simulation_summary(...)`.
- The System Registry contains a `generated_interface` extracted
  deterministically from each generated class. Before directly accessing a
  model or child attribute, property, or method, use only an exact member listed
  in the applicable registry entry. Follow `child_instances` mappings when
  traversing from the root to a child. Never derive a member name from prose,
  domain conventions, or a similar-looking name in another class.
- The metric names and extraction expressions are part of this generated
  simulator's domain contract. Use the model specification and exact generated
  interface to choose them. Do not use reflection, attribute-name guessing,
  placeholder values, or a generic `"score"`.
- If the model exposes no trustworthy outcome KPI, write an empty `metrics`
  object and a clear `metric_note` explaining what model state should be exposed
  before optimization. The run summary is still useful, but do not pretend an
  input such as the requested horizon is an optimization outcome.
- When you do emit metrics, also declare them at module scope, directly below
  `OPTPILOT_RESULT_FILE`, as one literal dict so downstream tools can read the
  contract without running the simulator:
  `OPTPILOT_METRICS = {{"<metric_name>": {{"direction": "maximize" | "minimize",
  "description": "<one short sentence>"}}, ...}}`.
  Keys must exactly match the keys passed to `write_simulation_summary`; the
  first entry should be the primary optimization objective; use plain string
  literals only (no expressions). Omit the declaration entirely when `metrics`
  is empty.
- xDEVS 3 ``Port`` objects do not have a singular ``.value`` attribute. Never
  use expressions such as ``model.output[name].value``. A port exposes
  ``.values``, but its transient values may be empty after a simulation step;
  prefer stable counters or summaries retained by the model. If no stable KPI
  is available, write empty metrics with an honest ``metric_note`` instead of
  guessing a value or inserting zero placeholders.


### 6. Policy Hook (only when the specification names an optimizable decision)
- When the user's specification explicitly asks for a tunable/optimizable
  decision policy (for example "the dispatch rule should be improvable" or
  "expose the routing decision as an editable policy"), declare where that
  decision lives. Do NOT restructure the model or create new modules:
  locate the already-built component file that owns the decision (the
  deciding atomic, or the dedicated decision component if one exists).
  - Ensure that file's docstring documents the editing contract: which
    method holds the selection logic; every input the decision sees (port
    names and the fields of their payloads, with types and meanings);
    every output it must emit and the protocol invariants its peers rely
    on (for example paired outputs or request/response handshakes). Add
    or extend the docstring if anything is missing.
  - The selection logic inside that file must be deterministic: it must
    not import or use `random`.
  - Declare the hook at module scope in the runner, directly below
    `OPTPILOT_RESULT_FILE`:
    `OPTPILOT_POLICY = {{"file": "<that component file's path, which must
    start with devs_project/ — e.g. devs_project/System_libs/Decider.py>",
    "entrypoint": "<the top-level class or function name defined in that
    file that owns the decision>",
    "description": "<one short sentence>"}}`.
    The declared file must exist and must define the declared entrypoint
    at top level; the manifest builder statically verifies both and
    silently drops declarations that point at nothing.
  Omit the declaration entirely when the specification does not ask for
  an optimizable decision.

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
            self.tool_dir / sub_path / "devs_project/runner_example.py"
        ]
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = [
            "set_global_clock",
            "attach_event_trace",
            "injection_tools",
            # "get_raw_input_content",
            # "logger",
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

        system_info_path = Path(system_info_file_path)
        if not system_info_path.is_absolute():
            system_info_path = self.working_directory / system_info_path
        system_registry = {}
        try:
            with open(system_info_path, "r", encoding="utf-8") as f:
                system_info = f.read()
            try:
                parsed_system_info = json.loads(system_info)
                system_registry = parsed_system_info
                system_info = json.dumps(
                    parsed_system_info,
                    ensure_ascii=False,
                    indent=2,
                )
            except (TypeError, json.JSONDecodeError):
                pass
        except OSError:
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
                    **litellm_retry_options(),
                )
                code = get_content_strict(response)

                code = extract_xml_code(code)
                require_result_summary_contract(code)
                require_event_trace_contract(code)
                require_generated_member_contract(
                    code,
                    system_registry,
                    model_class_name,
                    filename=str(full_path),
                )
                require_runner_argument_contract(code, filename=str(full_path))

                full_path.parent.mkdir(parents=True, exist_ok=True)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)

                return f"SUCCESS: top-level simulation runner script generated"

            except Exception as e:
                last_fail_info = f"FAILURE: Error creating top-level simulation runner script. Reason: {str(e)}"
                print(f"Attempt {attempt + 1} failed: {str(e)}")
                prompt += (
                    "\n\nThe previous runner was rejected by deterministic validation: "
                    f"{e}\nReturn a corrected complete runner that follows every "
                    "Event Trace, Result Summary, and suggested-scenario argument requirements."
                )

        raise RuntimeError(last_fail_info)
