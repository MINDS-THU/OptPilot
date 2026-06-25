import os
import sys
import re
import json
import time
import tempfile
import traceback
from pathlib import Path
from typing import Dict, Any, Optional

from dotenv import load_dotenv
load_dotenv()

_THIS_DIR = Path(__file__).parent
_REALM_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_REALM_ROOT))
sys.path.insert(0, str(_REALM_ROOT / "evaluation"))
sys.path.insert(0, str(_REALM_ROOT / "src"))

# Add HAMLET path for DEVS tools
_HAMLET_CORE = Path("/home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core")
sys.path.insert(0, str(_HAMLET_CORE))

class DevsGenRunner:
    """
    DEVS-GEN: LLM with smolagents CodeAgent + devs_construct_tree + devs_execute.

    The agent can:
    1. Call devs_construct_tree to build a DEVS simulation model
    2. Call run_python to run simulations with different parameters
    3. Use file tools to write instance data, read simulation output
    4. Write the final optimal policy to a file.
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.environ.get("SUPPLYBENCH_MODEL_ID", "openrouter/openai/gpt-5.2")

    def __call__(self, description_path: str) -> Dict[str, Any]:
        start_time = time.time()

        desc_text = Path(description_path).read_text(encoding="utf-8")

        working_dir = Path(tempfile.mkdtemp(prefix="supplybech_devs_gen_ca_", dir="/tmp"))
        print(f"\n  [DEVS-GEN-CA:{self.model_id}] Supply Chain Policy Generation")
        print(f"    Working dir: {working_dir}")

        try:
            tools = self._create_devs_tools(working_dir)

            from smolagents import CodeAgent, LiteLLMModel
            agent_model = LiteLLMModel(model_id=self.model_id)
            agent = CodeAgent(
                model=agent_model,
                tools=tools,
                max_steps=80,
                planning_interval=20,
                name="supply_chain_devs_gen_agent",
                description="Agent that uses DEVS simulation to optimize supply chain policy functions.",
            )

            user_prompt = self._build_prompt(desc_text, working_dir)

            print(f"    Starting smolagents CodeAgent loop (max 50 steps)...")
            result = agent.run(user_prompt, reset=True)

            execution_time = time.time() - start_time
            result_str = str(result) if result else ""

            # Extract policy code from the saved file
            policy_code = self._extract_policy_code(result_str, working_dir)

            return {
                "policy_code": policy_code,
                "raw_response": result_str,
                "model_id": self.model_id,
                "latency": execution_time,
                "success": bool(policy_code),
            }

        except Exception as e:
            print(f"    DEVS-GEN-CA ERROR: {e}")
            traceback.print_exc()
            return {
                "policy_code": "",
                "raw_response": "",
                "model_id": self.model_id,
                "latency": time.time() - start_time,
                "success": False,
                "error": str(e),
            }
        finally:
            print(f"    Working dir kept at: {working_dir}")

    def _build_prompt(self, desc_text: str, working_dir: Path) -> str:
        return """{desc_text}

### Available Tools
- **devs_construct_tree**: Build a DEVS simulation model from a natural language description.
    - Args: root_model_name, requirements (detailed system description), base_folder
    - Generates a Python project under <base_folder>/devs_project/ with a run.py entry point at <base_folder>/run.py.
    - The code it generated is complicated, so avoid directly reading or editing the generated code files. You are only allowed to read one file to see the args: <base_folder>/run.py and <base_folder>/devs_project/run_<root_model_name>.py
- **run_python**: Execute a Python file and read its output.
- **list_dir**: List files in the working directory.
- **see_file**: Read a file's contents.
- **create_file**: Create a file with content.
- **smart_replace**: Make targeted edits to an existing file.

### CRITICAL EXECUTION RULES: STEP-BY-STEP ONLY
You are a reasoning agent. You MUST execute your plan ONE step at a time and wait for the environment's observation before proceeding.
- **DO NOT** write a single giant code block that creates the file, runs the simulation, and calls `final_answer` all at once.
- **DO NOT** call `final_answer` in the same turn as `run_python`. You cannot know if your code works until you actually observe the stdout/stderr from `run_python` in the NEXT turn.
- The code block you write in one turn will be executed immediately all at once! so do the minimal step you need to take in that turn, wait and observe the results, then decide your next step in the following turn.

### Expected Workflow
You are at some one stage of the workflow. So you should infer the current stage based on the history, and never skip steps.
The controller will excute your code block, and return the stdout/stderr output to you in the next turn.
1. **Turn 1 (Construct Simulator)**: Analyze the problem topology, costs, and I/O. Call `devs_construct_tree` to build a generic supply chain simulator.
   - root_model_name = 'SupplyChain_Simulator', base_folder = 'supply_chain_model'
   - **CRITICAL DEVS REQUIREMENT**: In your natural language requirements, you MUST instruct the DEVS model generator to accept a command-line argument for the policy file (e.g., `--policy path/to/policy.py`). The built project must dynamically import this file, extract the `POLICY_MOUNTS` dictionary, and assign the custom policy functions to the correct node models. Also specify how to calculate and print the KPI (total costs) at the end of the simulation: It should print the KPI to a file you specified (e.g. `--outfile kpi.txt`). 
   - **STOP** and wait for the model construction to finish.
2. **Turn 2 (Design & Write Policy)**: Based on the scenario, design a specific replenishment policy. Use the `create_file` tool to write your policy into a standalone Python file (e.g., `test_policy_v1.py`).
   - This file MUST contain the `POLICY_MOUNTS` dictionary.
   - **STOP** and wait for confirmation that the file is created.
3. **Turn 3 (Execute & Evaluate)**: Use `run_python` to execute the simulator with your policy file: `run_python(filename="run.py", cwd="supply_chain_model", args="--policy test_policy_v1.py")`.
   - Use `stdout_file` and `stderr_file` if the output is long.
   - Only if the simulation fails, you can try to diagnose the error and fix it in 3 trials. If failed, just return a file with your policy without simulation.
   - **STOP** and analyze the printed KPIs and costs.
4. **Turn 4+ (Iterate & Optimize)**: You must create new policy files (e.g. `test_policy_v2.py`) to test, and repeat the `run_python` execution. Less than 8 iterations is recommended.
   - Compare the costs. Make sure you see the kpi yourself and choose the best one! 
   - You can write a script to run grid search over parameters.
   - **STOP** and analyze the printed KPIs and costs.
5. **Final Turn (Submit)**: At the final turn, do not use `run_python`, just ensure your BEST policy code is saved into some file. Then, call `final_answer('path_to_the_file.py')`, this will terminate the agent and never return, so make sure you have tested and compared the costs before calling `final_answer`.

### CRITICAL: Output Format & Policy File Requirements
The file you returned MUST contain valid Python code including:
1. All necessary policy function definition(s)
2. A POLICY_MOUNTS dictionary mapping node group names to functions, for example:
```python
def retailer_policy_func(...):
    # Your implementation here

POLICY_MOUNTS = {{
    "Retailer": retailer_policy_func,
}}
```
Your final answer MUST be EXACTLY the file path (either relative to the working directory or absolute) of the file containing the final code, and absolutely nothing else.""".format(desc_text=desc_text)

    def _extract_policy_code(self, raw_response: str, working_dir: Path) -> str:
        """Extract Python policy code by reading the file path returned by the agent."""
        
        # 1. 优先尝试解析 LLM 返回的路径
        # 去除大模型可能附带的引号、反引号、空格或句号
        clean_path = raw_response.strip(" '\".`\n")
        
        if clean_path:
            target_file = Path(clean_path)
            # 如果是相对路径，则拼上 working_dir
            if not target_file.is_absolute():
                target_file = working_dir / clean_path
                
            if target_file.exists() and target_file.is_file():
                content = target_file.read_text(encoding="utf-8")
                if 'POLICY_MOUNTS' in content:
                    print(f"    [Success] Read policy from LLM returned path: {target_file.name}")
                    return content
                else:
                    print(f"    [Warning] File '{target_file.name}' returned by LLM exists but is missing POLICY_MOUNTS.")
            else:
                print(f"    [Warning] The path returned by LLM does not exist: {clean_path}")

        # 2. Fallback: 如果大模型返回的路径不对（比如返回了一段废话），开启全目录扫描兜底
        print("    [Fallback] Scanning directory for files containing POLICY_MOUNTS...")
        policy_files = list(working_dir.glob("**/*.py"))
        for pf in policy_files:
            # 排除掉仿真框架的默认入口文件
            if pf.name == "run.py": continue 
            
            content = pf.read_text(encoding="utf-8")
            if 'POLICY_MOUNTS' in content:
                print(f"    [Success] Found fallback policy file: {pf.name}")
                return content

        print("    [Error] No valid policy code found via returned path or fallback scan.")
        return ""

    def _create_devs_tools(self, working_dir: Path) -> list:
        """Create the DEVS tool set for the smolagents agent."""
        tools = []

        # File editing tools
        try:
            from default_tools.file_editing.file_editing_tools import (
                ListDir, SeeTextFile, CreateFileWithContent, SmartReplace,
            )
            tools.extend([
                ListDir(str(working_dir)),
                SeeTextFile(str(working_dir)),
                CreateFileWithContent(str(working_dir)),
                SmartReplace(str(working_dir)),
            ])
        except ImportError as e:
            print(f"    File tools not available: {e}")

        # DEVS construct tool
        try:
            from devs_tools.devs_construct_pure_fast_plan.devs_construct_dyn_fast import (
                DEVSConstructTreeFastConcur,
            )
            file_tools = {
                "read": SeeTextFile(str(working_dir)),
                "list": ListDir(str(working_dir)),
            }
            construct_tool = DEVSConstructTreeFastConcur(
                file_tools=file_tools,
                model_id={"weak": self.model_id, "strong": self.model_id},
                working_directory=str(working_dir),
                disable_check=True,
                concur_num=10,
            )
            tools.append(construct_tool)
            print(f"    DEVS construct tool loaded")
        except Exception as e:
            print(f"    DEVS construct tool failed: {e}")


        # Python execute tool
        try:
            from agent_frameworks.python_execute import RunPythonFile
            tools.append(RunPythonFile(working_directory=str(working_dir)))
            print(f"    Python execute tool loaded")
        except Exception as e:
            print(f"    Python execute tool failed: {e}")

        return tools
