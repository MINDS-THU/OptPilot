from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import json
import yaml
from ..simulation.devs_execute import DEVSExecute
from .code_modifier import CodeRefiner
from typing import Optional

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
            "nullable": True
        },
        "command_args": {
            "type": "string",
            "description": "Command line arguments to pass to the script, as a single string (e.g., '--epochs 10 --lr 0.01').",
            "nullable": True
        },
        "allowed_libraries": {
            "type": "string", 
            "description": "Comma-separated list of allowed root packages (default: numpy,xdevs,logging,math,random,time,collections,itertools).", 
            "nullable": True
        },
        "stdin_content": {
            "type": "string", 
            "description": "Content to be passed to the script via standard input (STDIN).", 
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, core: DEVSExecute, stdout_file: str, stderr_file: str, project_path: str, main_file: str):
        super().__init__()
        self.core = core
        self.fixed_args = {
            "stdout_file": stdout_file,
            "stderr_file": stderr_file,
            "project_path": project_path,
            "main_file": main_file
        }
    
    def forward(self, 
                timeout: int = 30, 
                command_args: Optional[str] = None, 
                allowed_libraries: str = "numpy,xdevs,logging,math,random,time,collections,itertools", 
                stdin_content: Optional[str] = None) -> str:
        return self.core.forward(
            timeout=timeout,
            command_args=command_args,
            allowed_libraries=allowed_libraries,
            stdin_content=stdin_content,
            **self.fixed_args
        )

# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
RUNNER_FIXER_PROMPT = """
You are an expert DevOps & Simulation Reliability Engineer.

## **[Mission]**
Your task is to ensure the DEVS Simulation Runner Script `{target_file}` executes successfully without crashing. Make sure if it needs input, it will not crash when parsing the correct input.
**Target criteria: Exit Code 0.**

## **[Constraints]**
1. **Scope**: You are ONLY allowed to modify the Runner Script (`{target_file}`). 
   - You MUST NOT modify the core model files (Atomic/Coupled models). 
   - If the Runner is calling the model incorrectly, fix the Runner.
   - If the Runner has syntax errors, fix the Runner.
2. **Tools**: 
   - Use `{execute_tool_name}` to run the simulation and check for crashes. 
   - Use `{save_tool_name}` to overwrite the Runner Script with fixes. (Do not use this tool unless the simulation really crashed.)

## **[Context]**
- **Runner Script**: `{target_file}`
- **Runner Args**: {runner_args}
- **Model Specification**: {spec}
- **Utils Available**:
{util_desc}
- **Simulation Scenario**: {simulation_scenario}

## **[Strategy]**

You should interact with the tools, and do the following step by step. Only call one tool in one step, because you need to analyze the result first before you can decide what to do next.

1. Analyze the Runner Args and Simulation Scenario to understand the arguments and the input format required. 
    - Look for `input()` or stdin reading to understand required `input_str`.
2. **Execute & Test**: 
   - Construct valid arguments and input based on your analysis.
   - Call `{execute_tool_name}`.
3. **Debug Loop**:
   - **IF Crash (Exit Code != 0)**: Read the traceback in the output. Common issues:
     - **Import Errors**: The runner might use incorrect relative imports. Fix them.
     - **Argument Mismatch**: The runner might be passing wrong args to the Model Constructor. Check `Model Specification` and fix the Runner's instantiation logic.
     - **Syntax Errors**: Fix Python syntax.
     - Others: Read the `{target_file}` to understand how it sets up the simulation, is it consistent with the requirement?
   - **Action**: Modify the code using `{save_tool_name}` and **RE-RUN** `devs_execute` to verify.
   
4. **Completion**:
   - Once the simulation runs with Exit Code 0, you are done.
   - Return the final `sim_args` and `input_str` that worked.
"""

# ==============================================================================
# MAIN TOOL: SimulationRunnerFixer
# ==============================================================================
class SimulationRunnerFixer(Tool):
    name = "simulation_runner_fixer"
    description = (
        "Automatically attempts to run the simulation runner script, detects crashes, "
        "and iteratively fixes the runner script (only) until it starts successfully. "
        "It self-constructs input arguments/stdin needed for execution."
    )
    inputs = {
        "project_path": {
            "type": "string",
            "description": "Path to the project directory."
        },
        "runner_file_path": {
            "type": "string",
            "description": "Path to the top-level simulation python script."
        },
        "stdout_save_path": {
            "type": "string",
            "description": "Path to save the stdout of the simulation runner."
        },
        "stderr_save_path": {
            "type": "string",
            "description": "Path to save the stderr of the simulation runner."
        },
        "model_spec": {
            "type": "string",
            "description": "The specification of the model being run (to understand constructor args)."
        },
        "simulation_scenario": {
            "type": "string",
            "description": "Any additional context regarding the simulation scenario."
        },
        "runner_args": {
            "type": "string",
            "description": "Explain the arguments of the runner script."
        }
    }
    output_type = "string"

    def __init__(self, file_system_tools: dict[str, Tool], model_id: str = "gpt-4o", working_directory: str = "./working_dir"):
        """
        Args:
            file_system_tools: Dict containing 'read' tool. (Write access is restricted internally).
            model_id: LLM model ID.
            working_directory: Root dir.
        """
        super().__init__()
        self.file_system_tools_dict = file_system_tools
        self.read_file_tool = file_system_tools.get('read')
        self.code_refiner = CodeRefiner(working_directory, default_model=model_id)
        
        self.devs_execute_tool = DEVSExecute(working_directory)
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        
        # Load materials for prompt context
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = ["set_global_clock", "logger", "get_current_time", "injection_tools"]

    def _read_materials(self):
        util_desc = ""
        try:
            if self.util_desc_file.exists():
                with open(self.util_desc_file, "r") as f:
                    all_utils = yaml.safe_load(f)
                for util in self.injected_utils:
                    if util in all_utils:
                        util_desc += f"- {util}: {all_utils[util]}\n"
        except Exception:
            pass
        return util_desc

    def forward(self, project_path: str, runner_file_path: str, model_spec: str, simulation_scenario: str, runner_args: str, stdout_save_path: str, stderr_save_path: str) -> str:
        # 1. Prepare Paths
        full_runner_path = self.working_directory / runner_file_path
        abs_runner_path = str(full_runner_path.resolve())
        
        # 3. Initialize the Internal Agent
        # Tools: Read (Generic), Save (Restricted to Runner), Execute (Run Simulation)
        execute_wrapper = DEVSExecuteWrapper(
            core=self.devs_execute_tool,
            stdout_file=stdout_save_path,
            stderr_file=stderr_save_path,
            project_path=project_path,
            main_file=os.path.relpath(runner_file_path, project_path),
        )
        tools = [self.read_file_tool, self.code_refiner, execute_wrapper]
        
        model = LiteLLMModel(model_id=self.model_id, temperature=0.1)
        
        agent = CodeAgent(
            tools=tools,
            model=model,
            additional_authorized_imports=["pathlib", "json", "re", "yaml"],
            max_steps=50, 
            max_print_outputs_length=4000,
        )
        
        # 4. Construct Prompt
        util_desc = self._read_materials()
        prompt = RUNNER_FIXER_PROMPT.format(
            project_path=project_path,
            target_file=str(runner_file_path),
            spec=model_spec,
            util_desc=util_desc,
            save_tool_name=self.code_refiner.name,
            simulation_scenario=simulation_scenario,
            execute_tool_name=self.devs_execute_tool.name,
            runner_args=runner_args,
        )
        
        print(f"[RunnerFixer] Starting autonomous repair loop for {runner_file_path}...")
        
        # 5. Run Agent
        try:
            result = agent.run(prompt, reset=True)
            return f"RUNNER FIX COMPLETED. Final Status: {result}"
        except Exception as e:
            return f"RUNNER FIX FAILED: {str(e)}"