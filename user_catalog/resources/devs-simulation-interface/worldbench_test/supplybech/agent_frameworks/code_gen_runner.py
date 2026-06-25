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

class CodeGenRunner:
    """
    Code-GEN with CodeAgent: LLM writes Python code, executes it via run_python tool,
    debugs iteratively, and eventually outputs the final policy code with POLICY_MOUNTS 
    to a specific file, returning the file path.
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.environ.get("SUPPLYBENCH_MODEL_ID", "openrouter/openai/gpt-5.2")

    def __call__(self, description_path: str) -> Dict[str, Any]:
        start_time = time.time()

        # Read description
        desc_text = Path(description_path).read_text(encoding="utf-8")

        # Create working directory
        working_dir = Path(tempfile.mkdtemp(prefix="supplybech_code_gen_ca_", dir="/tmp"))
        print(f"\n  [CODE-GEN-CA:{self.model_id}] Supply Chain Policy Generation")
        print(f"    Working dir: {working_dir}")

        try:
            tools = self._create_tools(working_dir)

            # 【修改点 1】: 切换为 CodeAgent
            from smolagents import CodeAgent, LiteLLMModel
            agent_model = LiteLLMModel(model_id=self.model_id)
            agent = CodeAgent(
                model=agent_model,
                tools=tools,
                max_steps=50,
                planning_interval=10,
                name="supply_chain_code_gen_agent",
                description="Agent that writes Python code to generate and test supply chain policy functions.",
            )

            user_prompt = self._build_prompt(desc_text, working_dir)

            print(f"    Starting smolagents CodeAgent loop (max 20 steps)...")
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
            print(f"    CODE-GEN-CA ERROR: {e}")
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
        # 【修改点 2】: 明确指示写入文件并返回路径
        return """{desc_text}

### Instance Data
A complete scenario description is provided above. It contains the supply chain topology, cost parameters, demand patterns, and the function signature you need to implement.

### Available Tools
- **run_python**: Execute a Python file and read its output.
- **list_dir**: List files in the working directory.
- **see_file**: Read a file's contents.
- **create_file**: Create a file with content.
- **smart_replace**: Make targeted edits to an existing file.

### Expected Workflow
You are at some one stage of the workflow. So you should infer the current stage based on the history, and never skip steps.
1. Analyze the supply chain problem: topology, costs, demand patterns.
2. Design a replenishment policy that minimizes total cost (holding + stockout).
3. Write at least two Python file containing your policy function(s) and the POLICY_MOUNTS dictionary using the create_file tool.
4. Write a test script to validate your policy logic, then run it with the run_python tool.
5. Test several iterations to get the best policy you can, after the execution, must read the kpi to check the cost to determine which one is the best. Do not test too much as you are constrained by the execution time limit. Less than 6 iterations is recommended.
6. Save the final best Python policy code into a file named 'final_policy.py' in your working directory.
7. Return ONLY the path to 'final_policy.py' as your final answer.

### CRITICAL: Output Format & Policy File Requirements
The file 'final_policy.py' MUST contain valid Python code including:
1. One or more policy function definitions (e.g., `retailer_policy_func`)
2. A `POLICY_MOUNTS` dictionary mapping node group names to functions:
```python
POLICY_MOUNTS = {{
    "Retailer": retailer_policy_func,
}}
```

Do NOT return the code in your final answer string.
Your final answer MUST be EXACTLY the file path (either relative to the working directory or absolute) of the file containing the final code, and absolutely nothing else.""".format(desc_text=desc_text)

    def _extract_policy_code(self, raw_response: str, working_dir: Path) -> str:
        """Extract Python policy code by reading the file path returned by the agent."""
        # 【修改点 3】: 直接通过返回的文件路径去提取代码，带有兜底机制
        
        # 1. 解析并清理 LLM 返回的路径
        clean_path = raw_response.strip(" '\".`\n")
        
        if clean_path:
            target_file = Path(clean_path)
            # 处理相对路径
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

        # 2. Fallback: 全目录扫描兜底
        print("    [Fallback] Scanning directory for files containing POLICY_MOUNTS...")
        policy_files = list(working_dir.glob("**/*.py"))
        for pf in policy_files:
            # 跳过可能的测试脚本（如果有特定的测试脚本命名习惯也可以在这里加上）
            if "test" in pf.name.lower() and pf.name != "final_policy.py": 
                continue 
                
            content = pf.read_text(encoding="utf-8")
            if 'POLICY_MOUNTS' in content:
                print(f"    [Success] Found fallback policy file: {pf.name}")
                return content

        print("    [Error] No valid policy code found via returned path or fallback scan.")
        return ""

    def _create_tools(self, working_dir: Path) -> list:
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

        # Python execute tool
        try:
            from agent_frameworks.python_execute import RunPythonFile
            tools.append(RunPythonFile(working_directory=str(working_dir)))
            print(f"    Python execute tool loaded")
        except Exception as e:
            print(f"    Python execute tool failed: {e}")

        return tools
