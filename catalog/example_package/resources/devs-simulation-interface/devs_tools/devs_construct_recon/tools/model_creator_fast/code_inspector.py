from smolagents import Tool, LiteLLMModel
from pathlib import Path
import os
import yaml
from ...utils import get_content_strict
from litellm import completion

# ==============================================================================
# INSPECTOR PROMPT (注入专家知识)
# ==============================================================================
INSPECTOR_SYSTEM_PROMPT = """
You are a Senior DEVS (Discrete Event System Specification) Architect and Code Reviewer.
Your goal is to perform STATIC ANALYSIS on the provided Python code to find logical flaws that might NOT cause a crash but will lead to incorrect simulation results.

## **[Critical DEVS Anti-Patterns]**

You must aggressively scan the code for the following specific logical bugs:

### 1. The "State Interruption/Swallowing" Bug (CRITICAL)
**Scenario**: The model is in a busy state (e.g., `phase="PROCESSING"`, `sigma=10.0`) waiting for `deltint`.
**Trigger**: An external event arrives (`deltext` called).
**The Bug**: Inside `deltext`, the code processes the input and immediately calls `self.hold_in("OUTPUT_SOMETHING", 0)`.
**Consequence**: The original "PROCESSING" state and its remaining time are **OVERWRITTEN and LOST**. The simulated task finishes instantly or disappears.
**Correct Pattern**: In `deltext`, if the model is busy, it must `resume` the previous state or store the remaining time before scheduling a new output.

### 2. The "Infinite Zero-Time Loop"
**Scenario**: `deltint` transitions to State A (sigma=0), which transitions to State B (sigma=0), which goes back to State A.
**Consequence**: Simulation clock freezes, stack overflow.

### 3. Potential Cold Start Issue
Check for potential cold-start deadlock that might cause the model to not output properly

### Any other logical flaws that are not covered above

The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Analysis Strategy]**
1. Read the `Input Code` carefully.
2. Mentally simulate the lifecycle: `initialize` -> `deltext` (while busy) -> `deltint` -> `lambdaf`.
3. Highlight any logic that violates the DEVS formalism or the Anti-Patterns listed above.
4. Ignore minor formatting issues. Focus on **LOGIC**.

## **[Output Format]**
Return a structured report. If the code looks fine, say "No critical logic flaws found."
If flaws are found:
- **Flaw Type**: (e.g., State Interruption)
- **Location**: (Function name, Line approximation)
- **Explanation**: Why this is wrong.
- **Suggested Fix**: A brief snippet or description of how to fix it.
"""

class CodeInspector(Tool):
    name = "code_inspector"
    description = "Performs static analysis on DEVS model code to detect logical bugs (like state interruption, infinite loops)."
    inputs = {
        "target_file_path": {
            "type": "string",
            "description": "Path to the model file to be inspected."
        },
        "focus_area": {
            "type": "string",
            "description": "Optional specific concern (e.g., 'check for state interruption' or 'check input handling').",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, working_directory: str, model_id: str):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.model = LiteLLMModel(model_id=model_id, temperature=0.1)
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = ["set_global_clock", "logger", "get_current_time"]

    def _read_file(self, path_str: str) -> str:
        try:
            p = self.working_directory / path_str
            if p.exists():
                return p.read_text(encoding="utf-8")
            return "[File not found]"
        except Exception as e:
            return f"[Error reading file: {str(e)}]"

    def _read_materials(self):
        util_desc = ""
        
        with open(self.util_desc_file, "r") as f:
            all_utils = yaml.safe_load(f)
        for util in self.injected_utils:
            if util in all_utils:
                util_desc += f"- {util}: {all_utils[util]}\n"
                
        return util_desc

    def forward(self, target_file_path: str, focus_area: str = "") -> str:
        # 1. 读取目标代码
        code_content = self._read_file(target_file_path)
        if code_content.startswith("[Error") or code_content.startswith("[File"):
            return f"Failed to inspect: {code_content}"

        # 2. 构造 Prompt
        user_prompt = f"""
Please inspect the following DEVS Model Code.

**Target File**: {target_file_path}
**Specific Focus**: {focus_area if focus_area else "General Logic Integrity"}

```python
{code_content}
```
"""
        messages = [
            {"role": "system", "content": INSPECTOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]
        response = completion(
            model=self.model_id,
            messages=messages,
            temperature=0.5,
        )
        result = get_content_strict(response)

        return result