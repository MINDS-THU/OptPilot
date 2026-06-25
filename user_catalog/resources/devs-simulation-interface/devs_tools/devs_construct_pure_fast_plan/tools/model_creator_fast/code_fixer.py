from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import json
import yaml
import shutil
from ..simulation.devs_execute import DEVSExecute
from ..simulation.verifier_execute import DEVSLogValidator
from .code_modifier import CodeRefiner
from .code_inspector import CodeInspector

# ==============================================================================
# PROMPT TEMPLATES (复用并扩展你的标准)
# ==============================================================================
# 这里我们需要包含之前的标准，因为修复代码时也必须遵守这些规范
GLOBAL_STANDARDS = """
### [Global Standards]
- **Imports**: Whitelist: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs`, `devs_project.devs_utils.*`.
- **Coding**: explicit `__init__` args, use `self.logger.info`, store hardcoded params in `self.param`.
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
        - If any port has `initial_signal`, the `initialize` method **MUST** schedule an immediate event using `self.hold_in("SOME_STATE", 0)`.
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
        - **Confluent Events (`deltcon(self)`)**: By default, internal events (`deltint`) take precedence over external events when they occur simultaneously. Explicitly override the `deltcon(self)` method ONLY IF you need to change this logic (e.g., to process external events first).
        - **Initialization**: If a signal or information should be sent at initialization(i.e. protocol.initial_signal), you can use `self.hold_in("INIT", 0)` to schedule the event and send it in `lambdaf`. This is the only way to send a signal at initialization.
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

SIMU_INSTRUCTIONS = """
### [Simulation Specifics]
1. **Imports**: 
   - Standard `xdevs.sim` imports.
   - `from .target import class_name` (Relative import).
   - `set_global_clock` from `devs_project.devs_utils.devs_context`.
2. **Setup**:
   - `clock = SimulationClock()`
   - `set_global_clock(clock)`
   - Instantiate your model (or your generated `TestBench`).
   - `sim = Coordinator(model, clock)`
3. **Execution**:
   - `sim.initialize()`
   - `sim.simulate_time(simulate_time)`.
   - `sim.exit()`
   
### [Verification Specifics]
It should read from "stdout.txt", using robust log parsing. e.g. : 
```python
import json
logs = []
with open("stdout.txt", "r") as f:
    for line in f:
        try:
            log = json.loads(line)
            logs.append(log)
        except json.JSONDecodeError:
            continue
```
"""

FIXER_PROMPT_TEMPLATE = """
You are an expert DEVS Simulation Debugger and Code Fixer.

## **[Mission]**
Your task is to fix the Python code for a DEVS simulation model that failed verification.

You have full authority to modify:
1. The Model Code (`{target_file}`) and its sub-modules.
2. The Simulation Runner (`{simu_file}`) (if the test setup is wrong). Its quality is not guaranteed, you should change its logic first to meet up with the simulation model's behavior. Especially check if it generate or interact with the model correctly. Usually it's not the simulation logic's problem, but the test_bench models are flawed. 
3. The Log Verifier (`{veri_file}`) (if the validation logic is flawed). Its quality is not guaranteed, you should check it first to make sure the verification is correct. It may not know the right key of the log (e.g. the correct key of main msg), you should change it first to meet up with the simulation model's behavior & key names. For example, the filtering strategy may be wrong, you should check if the logs is really wrong, or if the filtering strategy is wrong.

The code is copied to `/tmp/xxxx/devs_project` to run, using command `python -m devs_project.xxx`, so the absolute imports should be `devs_project.*` .

Please keep the following unchanged:
1. The Model Structure. 
2. The input_ports, output_ports, and `__init__` parameters of the top model `{target_file}`. 

## **[Diagnostic Data]**
- **Target File**: `{target_file}`
- **Execution Status**: Failed
- **Standard Error (Crash Log)**: 
```text
{stderr_content}
```

* **Verifier Output (Logic Check)**:
```text
{verifier_content}
```

* Here is an example structure of the logger output:
{{"_log_type": "RESULT", "_level": "INFO", "_wall_time": "2025-12-01T20:38:56.543184", "_sim_time": 65.61632990046728, "_model_path": ["triage_consultation_system", "Consultation"], ...(other user specified keys)}}
You MUST check the code file to know the corresponding user specified keys. 
You are recommended use the `_log_type`, `_model_path`, and other keys user specified to filter the log entries. 
- `_log_type`: "RESULT" or "PROCESS" or "ERROR". Please see the target code file to check what it uses exactly.
- `_model_path`: It is the list representing the logic hierarchy of the model, you can use it to filter logs from specific models.

## **[Design Context]**

* **Plan**: {plan}
* **Simulation Context**: {context}
* **Project Structure**: I have summarized the source code of the current model AND all its sub-modules into a single reference file: `{all_models_spec_path}`. You can use it to find out the function and location of each model, as well as their logging strategy, to help you understand the model and logging better. 
* **Execution Log**: It is stored in {stdout_path}. Note that it is quite long, so you should try to only read the snippets or write a log parser to extract relevant information. Do not read the full log directly.  
    - Special: You should make sure the verifier do not check for events not included in the following list (unless it check for the event indicating success, but only those indicating failure are in the list): {all_events}

*Note*: If the plan specifies the logging format, you should make sure the log format is correct.

## **[Standards & Rules]**

You must adhere to the Project Standards to make sure the runner if coorect (especially when the runner implemented some auxiliary atomic / coupled models).
 
{global_standards}
{coupled_standards}
{atomic_standards}
{simu_standards}

### **[Critical Utils & Libraries]**
The following utilities are available by the simulation code and **MUST** be used correctly:
{util_desc}

## **[Strategy]**

You should interact with the tools, and do the following step by step. Only call one tool in one step, because you need to analyze the result first before you can decide what to do next.

1. **Analyze**: Read the stderr and verifier output to pinpoint the failure. Fix the crash first. If the crash is not fixed, the simulation will not run.
  * If `Exit Code != 0` (Crash): Look for syntax errors, missing imports, or attribute errors in the model and sub-models.
  * If `Verifier Failed`: Compare the logic in the model against the `{plan}` and the verification rules. You can also think if the plan is correct. e.g. If the output is missing, you should check the log and find out where the data haulted (e.g. data in queue, but never processed, so no result), is there any deadlock?
  * If the error is simply because the simulation is not long enough: You can try to increase the simulation time, or change other args (remember to change the runner's default args as well). 
2. **Locate**: You can start with the following steps. 
    - You must state the real issue correctly, and you can use `{code_inspector_name}` to discuss, help you analysis. 
    - **WorkFlow**: First check the log file to get roughly where the issue originate, and check the runner and verifier to see if they are correct. Then, make a Hypotheses. You can also edit the code to add more logging to help you locate the issue and verify the hypothesis. After that, re-run and read the new logs to locate the issue. Repeat this process until you find the issue, and finally fix it.
    - Editing code check: You can add more self.logger.info() and run the simulation to get more information about the model's behavior, and check what really went wrong. 
    - Reading log check: You can also browse the log file to find out where the issue originate (e.g. You can track an entity/event from generated to final, where the data/interaction haulted(maybe a data enqueued, but it is never sent to processor), if there is any deadlock).
    - Reading code check: Use `{code_inspector_name}` to check the code. you can also use your file reading tools to inspect the specific lines in models or the Runner/Verifier. 
3. **Fix**: Use your file writing/editing tools to apply the fix. **You can rewrite the entire file if necessary.**

You can use the provided `devs_execute` and `devs_log_validator` tools to re-run the simulation and validate the logs. And you can change the args of the simulation using the tool. 
If you changed the args, you should make sure the default value in the simulation runner is changed to your new setting. Because the script will be run using the default args. 
If you changed the code, you should first check if the simulation can run without error, and then check if the logs are correct. 

The available args are: `{sim_args}`.

## **[Final Output]**

You must finish your execution by calling `final_answer` with a summary string describing what you fixed.
Example: 
```python
final_answer("Fixed AttributeError in lambdaf by initializing self.state correctly.")
```
"""

RUNTIME_FIXER_PROMPT_TEMPLATE = """
You are an expert DEVS Simulation Debugger.

## **[Mission]**
Your task is to fix the Python code for a DEVS simulation model that **CRASHED** during execution.
The goal is to ensure the simulation runs successfully without raising exceptions (Exit Code 0).

You have full authority to modify:
1. The Model Code (`{target_file}`) and its sub-modules.
2. The Simulation Runner (`{simu_file}`) (if the test setup itself is causing the crash). 

The code is copied to `/tmp/xxxx/devs_project` to run, using command `python -m devs_project.xxx`, so the absolute imports should be `devs_project.*` .

Please keep the following unchanged:
1. The Model Structure. 
2. The input_ports, output_ports, and `__init__` parameters of the top model `{target_file}`. 

## **[Diagnostic Data]**
- **Target File**: `{target_file}`
- **Execution Status**: CRASHED (Runtime Error)
- **Standard Error (Traceback)**: 
```text
{stderr_content}
```

## **[Design Context]**

* **Plan**: {plan}
* **Simulation Context**: (You must guarantee that your changes of the code does not violate the JSONL output requirements, if any)
{context}
* **Project Structure**: I have summarized the source code of the current model AND all its sub-modules into a single reference file: `{all_models_spec_path}`. You can use it to find out the function and location of each model to help you debug.
* **Execution Log (Partial)**: Stored in {stdout_path}. If the crash happened mid-simulation, the last few lines here might indicate where the model stopped.

## **[Standards & Rules]**

You must adhere to the Project Standards.
{global_standards}
{coupled_standards}
{atomic_standards}
{simu_standards}

## **[Critical Utils & Libraries]**

The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Strategy]**

You should interact with the tools, and do the following step by step. 

1. **Analyze Traceback**: Read the `{stderr_content}` carefully. Identify the specific file, line number, and exception type.
2. **Locate**: Use your file reading tools to inspect the specific lines in the Model or the Runner.
* **Check for Common DEVS Errors**:
* Forgot to initialize state variables in `__init__`: add the initialization. 
* Port name mismatches (e.g., trying to send to a non-existent port): find the real port name and fix it. 
* If the simulation timed out, the simulation time limit is likely too high for the 30s real-time constraint. Or there might be traps in the code itself. Or the injection may not be correct.
* Sometimes the event injection is not correctly used, or the event is not correctly parsed. 

3. **Fix**: Use your file writing/editing tools to apply the fix. **You can rewrite the entire file if necessary.**
    - You should fix to make sure the script can run with the default args, without any input from stdin or files. 
4. **Sanity Check**: Ensure your fix adheres to Python syntax and the [Global Standards].
5. use the provided `devs_execute` tool to re-run the simulation to see if the crash is resolved

The available args for the simulation are: `{sim_args}`.

## **[Final Output]**

You must finish your execution by calling `final_answer` with a summary string describing the crash reason and the fix.
Example: 
```python
final_answer("Fixed AttributeError in lambdaf by initializing self.queue in __init__.")
```
"""


class CodeFixer(Tool):
    name = "code_fixer"
    description = "Analyzes execution logs and verifier feedback to automatically fix broken DEVS model code."
    inputs = {
        "target_file_path": {
            "type": "string",
            "description": "Path to the main model file that is currently being tested."
        },
        "all_models_spec_path": {
            "type": "string",
            "description": "Path to the file, containing: the code/descriptions of the current model and all its sub-modules."
        },
        "stdout_path": {
            "type": "string",
            "description": "Path to the execution standard output log."
        },
        "stderr_path": {
            "type": "string",
            "description": "Path to the execution standard error log."
        },
        "verifier_output": {
            "type": "string",
            "description": "The output message from the verification tool (RuleExecutor)."
        },
        "model_plan": {
            "type": "string",
            "description": "The original design plan describing how the model should behave."
        },
        "model_context": {
            "type": "string",
            "description": "The background context for the model."
        },
        "sim_args": {
            "type": "string",
            "description": "A string of arguments that can be passed to the simulation runner."
        },
        "simu_file": {
            "type": "string",
            "description": "Path to the simulation runner script."
        },
        "veri_file": {
            "type": "string",
            "description": "Path to the verification script."
        }
    }
    output_type = "string"

    def __init__(self, file_system_tools: dict[str, Tool], model_id: str, working_directory: str = "./working_dir"):
        """
        Args:
            file_system_tools: A dict of existing Tools for reading/writing files. 
                            (e.g., read_file, write_file, list_dir provided by the system maintainer)
            model_id: LLM model ID.
            working_directory: Root dir for path resolution.
        """
        super().__init__()
        print(f"file_system_tools: {[f'{k}: {v.name}' for k, v in file_system_tools.items()]}")
        if 'write' in file_system_tools:
            file_system_tools.pop('write')
        self.file_system_tools = list(file_system_tools.values())
        self.devs_excute_tool = DEVSExecute(working_directory)
        self.veri_excute_tool = DEVSLogValidator(working_directory)
        self.file_system_tools_dict = file_system_tools
        self.code_refiner = CodeRefiner(working_directory, default_model=model_id)
        self.code_inspector = CodeInspector(working_directory, model_id=model_id)
        
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        
        # 定义原子和耦合模型的规范字符串 (这里为了简洁省略了具体内容，实际使用需填充)
        self.global_standards = GLOBAL_STANDARDS
        self.atomic_standards = ATOMIC_INSTRUCTIONS 
        self.coupled_standards = COUPLED_INSTRUCTIONS
        self.simu_standards = SIMU_INSTRUCTIONS
        
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = ["set_global_clock", "logger", "get_current_time", "injection_tools"]

    def _read_log_file(self, path_str: str) -> str:
        """Helper to safely read log files."""
        try:
            p = self.working_directory / path_str
            if p.exists():
                return p.read_text(encoding="utf-8")
            return "[File not found]"
        except Exception as e:
            return f"[Error reading log: {str(e)}]"

    def _read_materials(self):
        util_desc = ""
        
        with open(self.util_desc_file, "r") as f:
            all_utils = yaml.safe_load(f)
        for util in self.injected_utils:
            if util in all_utils:
                util_desc += f"- {util}: {all_utils[util]}\n"
                
        return util_desc

    def forward(
        self, target_file_path: str, simu_file: str, veri_file: str, 
        stdout_path: str, stderr_path: str, 
        all_models_spec_path: str, verifier_output: str, model_plan: str, model_context: str, sim_args: str
    ) -> str:
        # 2. 读取日志内容
        stderr_content = self._read_log_file(stderr_path)

        if not stderr_content.strip():
            stderr_content = "(No system crash detected. Focus on Verifier Output.)"

        util_desc = self._read_materials()

        try: 
            logs = []
            with open(self.working_directory / stdout_path, 'r') as f:
                for line in f:
                    try:
                        log = json.loads(line)
                        logs.append(log)
                    except json.JSONDecodeError:
                        pass
            print(f"[Fixer] Loaded logs from {stdout_path}")
            all_events_set = set()
            for log in logs:
                all_events_set.add(json.dumps({
                    "_model_path": log.get("_model_path"),
                    "_log_type": log.get("_log_type"),
                    "data_dict_keys": list(log.keys()),
                }))
            all_events = json.dumps(list(all_events_set))
            print(f"[Fixer] Analyzed {len(logs)} logs, found events: {all_events}")
        except Exception as e:
            print(f"[Fixer WARNING] Failed to load logs: {str(e)}")
            all_events = "Failed to analyze the log, please carefully check all the instance names and events"
            input()


        # 3. 初始化 Agent
        # 我们把传入的 file_system_tools 注册给这个内部 Agent
        model = LiteLLMModel(model_id=self.model_id, temperature=0.1)
        tools = self.file_system_tools + [self.devs_excute_tool, self.code_refiner]
        if veri_file:
            tools.append(self.veri_excute_tool)
            tools.append(self.code_inspector)
        agent = CodeAgent(
            tools=tools, 
            model=model,
            additional_authorized_imports=["os", "sys", "logging", "pathlib", "re", "json"],
            max_steps=50, 
            max_print_outputs_length=4000,
        )

        # 4. 组装 Prompt
        if veri_file:
            prompt = FIXER_PROMPT_TEMPLATE.format(
                target_file=target_file_path,
                simu_file=simu_file,
                veri_file=veri_file,
                
                stderr_content=stderr_content[-1000:], # 截断防止 token 溢出
                verifier_content=verifier_output[-1000:],
                
                plan=model_plan,
                context=model_context,
                all_models_spec_path=all_models_spec_path,
                stdout_path=stdout_path,
                
                global_standards=self.global_standards,
                atomic_standards=self.atomic_standards,
                coupled_standards=self.coupled_standards,
                simu_standards=self.simu_standards,
                
                sim_args=sim_args,
                util_desc=util_desc,
                read_tool_name=self.file_system_tools_dict['read'].name,
                all_events=all_events,
                code_inspector_name=self.code_inspector.name
            )
        else:
            prompt = RUNTIME_FIXER_PROMPT_TEMPLATE.format(
                target_file=target_file_path,
                simu_file=simu_file,
                
                stderr_content=stderr_content[-1000:], # 截断防止 token 溢出
                
                plan=model_plan,
                context=model_context,
                all_models_spec_path=all_models_spec_path,
                stdout_path=stdout_path,
                
                global_standards=self.global_standards,
                atomic_standards=self.atomic_standards,
                coupled_standards=self.coupled_standards,
                simu_standards=self.simu_standards,
                
                sim_args=sim_args,
                util_desc=util_desc,
                read_tool_name=self.file_system_tools_dict['read'].name
            )

        # 5. 执行循环 (Retry Logic)
        current_input = prompt
        should_reset = True
        result = agent.run(current_input, reset=should_reset)
        return f"FIX ATTEMPT COMPLETED: {str(result)}"