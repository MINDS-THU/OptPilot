
from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import yaml
import json
import shutil
import re

# 保持原有的 SpecificFileSaver 不变
class SpecificFileSaver(Tool):
    name = "save_file" # 默认名，初始化时会被覆盖
    description = "Saves content to a specific file."
    inputs = {
        "code_content": {
            "type": "string",
            "description": "The complete code string to be saved."
        }
    }
    output_type = "string"

    def __init__(self, name: str, description: str, target_path: str):
        super().__init__()
        self.name = name
        self.description = description
        self.target_path = target_path
        self.has_executed = False

    def forward(self, code_content: str) -> str:
        try:
            directory = os.path.dirname(self.target_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)
            
            with open(self.target_path, "w", encoding="utf-8") as f:
                f.write(code_content)
                
            self.has_executed = True
            return f"SUCCESS: Content saved to {os.path.basename(self.target_path)}."
        except Exception as e:
            return f"ERROR: Failed to save file. {str(e)}"

# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
GLOBAL_STANDARDS = """
### [Global Standards]
Adhere to the following engineering standards for all model types:

#### 1. Imports & Dependencies
- **Whitelist**: Restrict imports to the following packages: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs` (and `xdevs.models`).
- **Project Utils**: Import necessary utilities (e.g., `get_sim_logger`, `get_current_time`) from `devs_project.devs_utils.xxx`. Refer to [Utils] for detailed import statements.
- Other submodels in the project can be imported as needed.

#### 2. Coding Conventions
- **Explicit Configuration**: Define all configuration parameters explicitly in `__init__`. Omit `*args` and `**kwargs`.
- **Logging**: Log all key events using `self.logger.info(...)`.
"""

ATOMIC_INSTRUCTIONS = """
### [Atomic Model Specifics]
1. **Inheritance**: Inherit from `Atomic`.
3. **Constructor (`__init__`)**:
    - Signature: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
    - Steps:
        1. Call `super().__init__(name)`.
        2. Assign `self.parent = parent`.
        3. Initialize logger: `self.logger = get_sim_logger(self)`.
        4. Register Ports: Use `self.add_in_port(Port(type, "name"))` and `self.add_out_port(Port(type, "name"))`.
        5. Initialize State: Set member variables and call `self.hold_in(phase, time)`. 
4. **Core Behaviors**:
    - Implement `initialize(self)`: Set initial state. Set phase/sigma using `self.hold_in(phase, time)`. Log initialization.
        - It can not send any output. If you need to send a initial signal (e.g. report you are ready), you can use `self.hold_in(phase, time)` to schedule the event, prepare the payload, and send it in `lambdaf`.
        - If any port has `init_behavior="ACTIVE"`, the `initialize` method **MUST** schedule an immediate event using `self.hold_in("SOME_STATE", 0)`.
    - Implement `deltext(self, e)`: Only do the following:
        - Handle external events (`self.input["port"].values`).
        - Get internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
        - Prepare the payload of the next lambdaf. Make sure the prepared payload variable is the one used in `lambdaf`.
        - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`.
        - Log events (if needed). 
    - Implement `deltint(self)`: Only do the following:
        - Get internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
        - Handle internal timeouts. 
        - Prepare the payload of the next lambdaf. Make sure the prepared payload is the one used in `lambdaf`.
        - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`.
        - Log events (if needed). 
    - Implement `lambdaf(self)`: Only do the following, any other operations should be done in the following `deltint`:
        - Send output via `self.output["port"].add(payload)`.
        - Log events.
        - *HINT*: payload should be prepared before, and the state, sigma, statistic counters, etc. should be updated in the following `deltint`.
    - Implement `exit(self)`: Cleanup and final stats logging.
    - **Event Handling Logic**:
        - **Execution Sequence (CRITICAL)**: `lambdaf` will send outputs before `deltint` schedules the next internal event. Thus, the payload sent in `lambdaf` should be prepared in the previous `deltint`, `deltext`, or `initialize`. 
        - **Confluent Events (`deltcon`)**: By default, internal events (`deltint`) take precedence over external events when they occur simultaneously. Explicitly override the `deltcon(self)` method ONLY IF you need to change this logic (e.g., to process external events first).
        - **Initialization**: If a signal or information should be sent at initialization(i.e. protocol.init_behavior="ACTIVE"), you can use `self.hold_in("INIT", 0)` to schedule the event and send it in `lambdaf`. This is the only way to send a signal at initialization.
"""

COUPLED_INSTRUCTIONS = """
### [Coupled Model Specifics]
1. **Inheritance**: Inherit from `Coupled`.
2. **Container Logic**: Treat this class as a pure structure container. Implement ONLY `__init__`.
3. **Constructor (`__init__`)**:
    - Signature: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
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
"""

# ============================================================================
# RUNNER GENERATION PROMPT (STANDALONE)
# ============================================================================
RUNNER_PROMPT_TEMPLATE = """
You are an expert DEVS simulation engineer using the `xdevs` framework.

## **[Mission]**
Generate a simulation runner script (`{runner_filename}`) to setup and run the model `{class_name}`. The script is a smoke test for the model. It should test the model's main behavior in a minimum way (e.g. sending 1-10 event / packet / object / customer)

## **[Context]**
- **Target Model**: `{class_name}` (Absolute Path: `{root_model_path}`, Relative Path: `{file_path}`).
- **Runner Save Path**: `{runner_path}`.
- **Spec**: {spec}
- **Scenario**: {scenario}
- **System Registry**: `{system_info_path}` (Optional: Use `{tool_name}` to read this if you need to inspect constructor arguments or sub-component details. But it is quite long, you might need to filter it out by writing a simple script. )

## **[Critical Utils & Libraries]**
The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Engineering Standards]**
{global_standards}

{coupled_standards}

{atomic_standards}

## **[Task: Simulation Runner Generation]**
You should interact with the tools, and do the following step by step. Only call one tool in one step, because you need to analyze the result first before you can decide what to do next.

**Important**: When creating the `TestBench` class or `Generator` class inside this script, apply the [Engineering Standards] strictly.

**Step 1: Test Bench Analysis (CRITICAL)**
Analyze if `{class_name}` is a standalone system or a sub-component (e.g., a processor, a queue).
- **If it is a sub-component**: You MUST create a `TestBench` class (a CoupledModel) in the script.
    - Create a simple `Generator` (Source) to feed data into input ports.
    - Create a `Collector` (Sink) to receive data from output ports.
    - Wire them: `Generator -> {class_name} -> Collector`.
    - Use this `TestBench` as the root model for simulation.
- **If it is a standalone system**: Use it directly.

**Step 2: Script Construction**
Special: Make sure the simulation time is long enough to observe the behavior of the model. You can use a large value like `1000`.
1. **Imports**: 
   - Standard `xdevs.sim` imports.
   - `{model_rel_import}` (Relative import).
   - `set_global_clock` from `devs_project.devs_utils.devs_context`.
    - **Injection (Conditional)**: IF the scenario requires external event injection:
        - Import `ReliableInjectionSystem` from `devs_project.devs_utils.inject`.
        - Import `get_raw_input_content` from `devs_project.devs_utils.inject`.
2. **Setup**:
    - IF the Model Specification has input_ports, and **Simulation Scenario** clearly specified them (e.g., "inject X at time T"):
        - **DO NOT** use `open()` or `argparse` for file paths. 
        - Do not read from the stdin/file. Instead, you can do it in two ways: 
            1. hard-code all the required input in the code, and inject them using the provided tool `injection_tools`. Then, implement a helper function (e.g., `parse_schedule(text)`) to parse `raw_text` into a list of event dicts `[{{"time":..., "port":..., "payload":...}}]`.
            2. Implement a Wrapper Model as shown in the example code. You might need to implement modules more than just Generator and Collector, based on the need. 
   - `clock = SimulationClock()`
   - `set_global_clock(clock)`
   - Instantiate your model (or your generated `TestBench`).
    - **Harness Wrapping**:
        - **IF Injection is used**:
            - Instantiate the harness: `model = ReliableInjectionSystem(name="harness", parent=None, core_model={class_name}_instance, events=parsed_events)`.
            - Note: The `ReliableInjectionSystem` becomes the top-level model to be simulated.
        - **ELSE**:
            - Use the core model directly: `model = {class_name}_instance`.
   - `sim = Coordinator(model, clock)`
3. **Execution**:
    - Call `sim.initialize()`.
    - Call `sim.simulate_time(simulate_time)`.
    - Call `sim.exit()`.

**Step 3: Save Using Tool**
Call `{runner_tool_name}` with the complete Python code. e.g. 
```python
{runner_tool_name}(f\"\"\"\\
{{runner_code}}
\"\"\")
```

**[Reference Template]**
Use this code as your simulation strict template. Do not change the logic flow. 
```python
{simu_example}
```

## **[Final Output]**

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

class SimulationRunnerCreator(Tool):
    name = "simulation_runner_generator"
    description = "Generates a DEVS simulation runner script (Phase 1). Handles test-bench generation for sub-modules."
    inputs = {
        "model_file_path": {"type": "string", "description": "Path to the top-level model code file."},
        "model_class_name": {"type": "string", "description": "Class name of the top-level model."},
        "model_spec": {"type": "string", "description": "The functional specification of the root model."},
        "system_info_file_path": {"type": "string", "description": "Path to the JSON file containing info for ALL models."},
        "simulation_scenario": {"type": "string", "description": "Description of the simulation scenario."},
        "simu_save_path": {"type": "string", "description": "Path to save the simulation script (e.g. runner.py)."}
    }
    output_type = "string"

    def __init__(self, read_file_tool: Tool, model_id: str = "gpt-4o", working_directory: str = "./working_dir"):
        super().__init__()
        self.read_file_tool = read_file_tool
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.simu_example_files = [
            self.tool_dir / sub_path / "devs_project/runner_example_unit.py"
        ]
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = ["set_global_clock", "logger", "get_current_time", "injection_tools"]
        self.definitions_file = self.tool_dir / sub_path / "definitions.md"

    def _read_materials(self):
        simu_example_content = ""
        util_desc = ""
        
        for example_file in self.simu_example_files:
            with open(example_file, "r") as f:
                simu_example_content += f.read()
        
        with open(self.util_desc_file, "r") as f:
            all_utils = yaml.safe_load(f)
        for util in self.injected_utils:
            if util in all_utils:
                util_desc += f"- {util}: {all_utils[util]}\n"
                
        return simu_example_content, util_desc

    def forward(self, model_file_path: str, model_class_name: str, model_spec: str, system_info_file_path: str, simulation_scenario: str, simu_save_path: str) -> str:
        simu_example, util_desc = self._read_materials()
        
        # 1. 路径处理
        full_runner_path = self.working_directory / simu_save_path
        abs_runner_path = str(full_runner_path.resolve())

        # 2. 计算相对路径 (用于 import)
        try:
            model_rel_path = Path(model_file_path).relative_to(Path(simu_save_path).parent)
            dot_path = ".".join(model_rel_path.with_suffix('').parts)
            model_rel_import = f"from .{dot_path} import {model_class_name}"
            print(f"Model relative import: {model_rel_import}")
        except ValueError:
            model_rel_path = Path(model_file_path).name 
            model_rel_import = f"from .target import {model_class_name}"

        # ========================================================================
        # PHASE 1: 生成 Runner
        # ========================================================================
        print(f"[Runner Generator] Generating simulation runner for {model_class_name}...")
        
        runner_saver = SpecificFileSaver(
            name="save_simulation_runner", 
            description="Saves the simulation runner/test-bench Python code.",
            target_path=abs_runner_path
        )

        runner_prompt = RUNNER_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            file_path=model_rel_path,
            model_rel_import=model_rel_import,
            spec=model_spec,
            root_model_path=model_file_path,
            system_info_path=system_info_file_path,
            tool_name=self.read_file_tool.name,
            scenario=simulation_scenario,
            runner_tool_name=runner_saver.name,
            runner_filename=full_runner_path.name,
            runner_path=full_runner_path,
            simu_example=simu_example,
            util_desc=util_desc,
            global_standards=GLOBAL_STANDARDS,
            coupled_standards=COUPLED_INSTRUCTIONS,
            atomic_standards=ATOMIC_INSTRUCTIONS,
        )

        model1 = LiteLLMModel(model_id=self.model_id, temperature=0.1)
        agent1 = CodeAgent(
            tools=[self.read_file_tool, runner_saver],
            model=model1,
            additional_authorized_imports=["os", "sys", "logging", "pathlib", "json", "yaml", "argparse"],
            max_steps=30, 
            max_print_outputs_length=4000,
        )
        
        for _ in range(3):
            try:
                result1 = agent1.run(runner_prompt, reset=True)
                print(f"[Runner Generator] Result: {result1}")
            except Exception as e:
                print(f"[Runner Generator ERROR] {str(e)}")
                continue
            
            if not runner_saver.has_executed:
                print("CRITICAL: Failed to save the simulation runner. retry...")
                runner_prompt += "\n\nYou MUST NOT write formatting markers or stop sequences in your Thought."
                continue
            break
          
        if not runner_saver.has_executed:
            raise Exception("CRITICAL: Failed to save the simulation runner.")
        
        print("[Runner Generator] ✓ Runner saved successfully")
        
        # 返回值要求：和原来一样返回 json dumps
        return json.dumps(result1)