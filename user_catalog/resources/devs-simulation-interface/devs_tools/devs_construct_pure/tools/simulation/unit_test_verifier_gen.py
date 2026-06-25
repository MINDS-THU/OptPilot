
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

# ============================================================================
# VERIFIER GENERATION PROMPT (STANDALONE WITH SYSTEM_INFO INJECTED)
# ============================================================================
VERIFIER_PROMPT_TEMPLATE = """
You are an expert DEVS simulation test engineer.

## **[Mission]**
Generate a practical and tolerant log verifier script (`{verifier_filename}`) that parses and checks the **key outcomes** of a simulation.
- **Primary Goal**: Confirm that the core scenario functions roughly as intended. **Do not** aim for exhaustive or strict validation of every detail.
- **Focus on important KPIs and key events**. Skip fine-grained validation 
    - e.g. You should not check exact counts, timing, model creation logs, data structure of unimportant events.
    - e.g. You can check if the model generated several products, but don't check whther the number generated exactly match the expected number, due to randomness and system delays.
- **Be forgiving and pragmatic**. The script's role is to catch major failures, not to enforce strict specifications.
- **Avoid over-engineering**. It's okay if the verifier is somewhat lenient.

## **[Context]**
- **Target Model**: `{class_name}`
- **Model Path**: `{root_model_path}`
- **Runner Path**: `{runner_path}` (you can check it for the instance name of `{class_name}`)
- **Scenario**: {scenario}
- **System Registry**: `{system_info_path}` (Contains logging schema information)

## **[System Info]**
The system registry below contains all the possible model definitions and logging specifications.
If system_info is unclear, check the real code to keep the verifier simple and functional.
Do NOT check any logging that is not in the system_info.
(coupled model is just a collection of couplings, so it just report the modeling creation events. )
```json
{system_info}
```

## **[All available model_path]**
These are all of the model_path that are actually in the log, do not check any other. 
**Use it as your primary reference** for model_name, and data key names:
{all_events}

## **[Task: Verifier Generation]**

You should interact with the tools, and do the following step by step. Only call one tool in one step, because you need to analyze the result first before you can decide what to do next.

### **Step 1: Review System Info**
- Identify the main logging events associated with `{class_name}` from system_info.
- Note the key data fields for those events.

### **Step 2: Write Tolerant Verifier Logic & Save Using Tool**

#### Requirements of Verifier Script
Your script should:

**A. Robust Log Parsing**
The `stdout.txt` may contain mixed content. Parse it pragmatically:
```python
import json
logs = []
with open("stdout.txt", "r") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            log = json.loads(line)
            logs.append(log)
        except json.JSONDecodeError:
            # Silently skip non-JSON lines (e.g., debug prints)
            continue
```

**B. Filter Relevant Logs**
Focus on logs related to the target model or scenario.
- **Preferred**: Filter by `model_path` or other keys that are in [All available model_path].
- **Be flexible**: It's acceptable if some irrelevant logs are included or some relevant ones are missed, as long as the main events are captured.

**C. Check Key Events & Data (Tolerantly)**
For each *important* event identified:
- **Check for key event existence**: Use case-insensitive or partial matching if necessary.
- **Apply lenient validation**: Use approximate checks (e.g., `> 0`, `is not None`, range checks) rather than exact equality.
  - **Critical Reminder**: Simulation outputs are often approximate. For example, if 23 items are generated, it's normal that only around 19-22 might be processed due to delays or randomness. **Never** enforce exact counts unless absolutely required by the scenario.
  - Partial match might rsult in irrelevant logs filtered, but it's okay as long as the main events are captured (you can use a flag to check if at least one target event is found)

**[Reference Template]**
Use this code as a structural template, but adapt it to be **more tolerant and focused on key outcomes**.
```python
{veri_example}
```

#### Save using tool
Call `{verifier_tool_name}` with the complete Python code.
```python
{verifier_tool_name}(f\"\"\"\\
{{runner_code}}
\"\"\")
```

## **[Output]**
briefly report what you checked. 
```python
final_answer(...)
```
"""

# ==============================================================================
# TOOL 2: Log Verifier Creator (Phase 2 Logic)
# ==============================================================================
class LogVerifierCreator(Tool):
    name = "log_verifier_generator"
    description = "Generates a log verifier script (Phase 2). Validates simulation output against system registry."
    inputs = {
        "model_file_path": {"type": "string", "description": "Path to the top-level model code file (for reference)."},
        "model_class_name": {"type": "string", "description": "Class name of the top-level model."},
        "system_info_file_path": {"type": "string", "description": "Path to the JSON file containing info for ALL models."},
        "simulation_scenario": {"type": "string", "description": "Description of the simulation scenario."},
        "simu_save_path": {"type": "string", "description": "Path where the runner was saved (for context)."},
        "veri_save_path": {"type": "string", "description": "Path to save the verifier script (e.g. verifier.py)."},
        "stdout_file_path": {"type": "string", "description": "Path to the stdout file containing simulation logs."}
    }
    output_type = "string"

    def __init__(self, read_file_tool: Tool, model_id: str = "gpt-4o", working_directory: str = "./working_dir"):
        super().__init__()
        self.read_file_tool = read_file_tool
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.veri_example_files = [
            self.tool_dir / sub_path / "devs_project/verifier_example.py"
        ]

    def _read_materials(self):
        veri_example_content = ""
        for example_file in self.veri_example_files:
            with open(example_file, "r") as f:
                veri_example_content += f.read()
        return veri_example_content

    def forward(self, model_file_path: str, model_class_name: str, system_info_file_path: str, simulation_scenario: str, simu_save_path: str, veri_save_path: str, stdout_file_path: str) -> str:
        veri_example = self._read_materials()
        
        full_verifier_path = self.working_directory / veri_save_path
        abs_verifier_path = str(full_verifier_path.resolve())

        # ========================================================================
        # PHASE 2: 生成 Verifier
        # ========================================================================
        print(f"[Verifier Generator] Generating log verifier for {model_class_name}...")
        
        # 主动读入 system_info 文件
        try:
            with open(self.working_directory / system_info_file_path, 'r') as f:
                system_info_content = f.read()
            # 尝试解析 JSON 以检查有效性
            json.loads(system_info_content)
            print(f"[Verifier Generator] Loaded system_info from {system_info_file_path}")
        except Exception as e:
            print(f"[Verifier Generator WARNING] Failed to load system_info: {str(e)}")
            input()
            system_info_content = f"{{\"error\": \"Failed to load system_info: {str(e)}\"}}"
            
        try: 
            logs = []
            with open(self.working_directory / stdout_file_path, 'r') as f:
                for line in f:
                    try:
                        log = json.loads(line)
                        logs.append(log)
                    except json.JSONDecodeError:
                        pass
            print(f"[Verifier Generator] Loaded logs from {stdout_file_path}")
            all_events_set = set()
            for log in logs:
                all_events_set.add(json.dumps({
                    "_model_path": log.get("_model_path"),
                    "_log_type": log.get("_log_type"),
                    "data_dict_keys": list(log.keys()),
                }))
            all_events = json.dumps(list(all_events_set))
            print(f"[Verifier Generator] Analyzed {len(logs)} logs, found events: {all_events}")

        except Exception as e:
            print(f"[Verifier Generator WARNING] Failed to load logs: {str(e)}")
            all_events = "Failed to analyze the log, please carefully check all the instance names and events"
            input()
        # exit()
        # 生成 Verifier

        verifier_saver = SpecificFileSaver(
            name="save_log_verifier", 
            description="Saves the validation/verifier Python code.",
            target_path=abs_verifier_path
        )

        verifier_prompt = VERIFIER_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            root_model_path=model_file_path,
            runner_path=simu_save_path,
            scenario=simulation_scenario,
            system_info_path=system_info_file_path,
            verifier_tool_name=verifier_saver.name,
            verifier_filename=full_verifier_path.name,
            veri_example=veri_example,
            system_info=system_info_content, 
            all_events=all_events,
        )

        model2 = LiteLLMModel(model_id=self.model_id, temperature=0.1)
        agent2 = CodeAgent(
            tools=[self.read_file_tool, verifier_saver],
            model=model2,
            additional_authorized_imports=["os", "sys", "logging", "pathlib", "json", "yaml", "argparse"],
            max_steps=30, 
            max_print_outputs_length=4000,
        )
    
        for _ in range(3):
            try:
                result2 = agent2.run(verifier_prompt, reset=True)
                assert isinstance(result2, str)
                print(f"[Verifier Generator] Result: {result2}")
            except Exception as e:
                print(f"[Verifier Generator ERROR] {str(e)}")
                continue
            
            if not verifier_saver.has_executed:
                print("CRITICAL: Failed to save the verifier.")
                continue
            break
        
        assert isinstance(result2, str)
        if not verifier_saver.has_executed:
            raise Exception("CRITICAL: Failed to save the verifier.")

        print("[Verifier Generator] ✓ Verifier saved successfully")

        # 返回值要求：返回 result2
        return result2