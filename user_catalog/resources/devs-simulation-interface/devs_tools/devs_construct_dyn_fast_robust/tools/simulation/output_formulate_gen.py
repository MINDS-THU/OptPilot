from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import json
import shutil
from .verifier_execute import PythonScriptExecutor

# ==============================================================================
# HELPER TOOL: SpecificFileSaver (用于保存生成的 Python 脚本)
# ==============================================================================
class SpecificFileSaver(Tool):
    name = "save_file"
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

class ScriptRunnerWrapper(Tool):
    name = "devs_script_exe"
    description = (
        "Executes a specific Python script for formulation or processing. "
        "It injects the content of a previously generated stdout log into the script's standard input (stdin). "
        "Returns the standard output (stdout) of the script."
    )
    inputs = {
        "timeout": {
            "type": "integer",
            "description": "Timeout for the script in seconds (default: 30).",
            "nullable": True
        }
    }
    output_type = "string"
    
    def __init__(self, working_directory: str, stdout_file_path: str, stderr_file_path: str, target_path: str):
        """
        Args:
            working_directory: 工作目录根路径
            stdout_file_path: 也就是上一轮执行的输出文件，将作为本次执行的 Stdin 输入
            stderr_file_path: 上一轮的错误文件（本逻辑暂时未用到，但保留接口兼容）
        """
        super().__init__()
        self.working_directory = working_directory
        self.stdout_file_path = stdout_file_path
        self.stderr_file_path = stderr_file_path
        self.target_path = target_path
        # 实例化通用执行器
        self.executor = PythonScriptExecutor(working_directory=working_directory)
    
    def forward(self, timeout: int = 30) -> str:
        if timeout is None: timeout = 30
        
        script_file_path = self.target_path
        # 1. 准备 Stdin 内容 (读取 stdout_file_path)
        try:
            # 拼接完整路径
            source_file = (Path(self.working_directory) / self.stdout_file_path).resolve()
            
            # 检查文件是否存在
            if not source_file.exists():
                return json.dumps({
                    "passed": False,
                    "message": f"Source file for stdin not found: {self.stdout_file_path}",
                    "detail": "Cannot inject stdin because the source file is missing."
                })
            
            # 读取内容
            stdin_content = source_file.read_text(encoding='utf-8')
            
        except Exception as e:
            return json.dumps({
                "passed": False,
                "message": f"Error reading source file: {str(e)}",
                "detail": "Failed to prepare stdin content."
            })

        # 2. 调用通用执行器
        # 这里的逻辑是：
        # - script_path: 传入的脚本
        # - files_to_copy: 暂时不需要复制额外文件，除非脚本依赖其他文件（这里假设单脚本）
        # - stdin_content: 刚刚读取的文本
        # - stdout_save_path: None (直接获取字符串返回)
        result = self.executor.execute(
            script_path=script_file_path,
            files_to_copy=[], 
            stdin_content=stdin_content, # <--- 核心修改：注入 Stdin
            stdout_save_path=None,
            stderr_save_path=None,
            timeout=timeout
        )

        # 3. 处理返回结果
        if result.error_message:
            return json.dumps({
                "status": "error",
                "message": result.error_message,
                "detail": "Executor failed to start."
            })

        if result.return_code == 0:
            return json.dumps({
                "status": "success",
                "output": result.stdout.strip(), # <--- 返回脚本输出
                "stderr_log": result.stderr.strip() # 可选：同时也返回stderr以便调试
            })
        else:
            return json.dumps({
                "status": "failed",
                "message": "Script execution returned non-zero exit code.",
                "detail": result.stderr.strip() or result.stdout.strip()
            })
    
# ============================================================================
# SUMMARY GENERATION PROMPT
# ============================================================================
SUMMARY_PROMPT_TEMPLATE = """
You are an expert Data Analyst specializing in Discrete Event Simulation (DEVS) logs.

## **[Mission]**
Generate a Python script (`{summary_filename}`) that processes the simulation log (`stdout.txt`), aggregates low-level events, and reconstructs the data into the specific format required by the scenario.

Each line of the input simulation log is a valid JSON, with the structure: {{"log_type": ..., "level": ..., "wall_time": ..., "sim_time": ..., "model_path": [...], "event": ..., "data": {{...}}}}

**CORE CHALLENGE**: 
The **Raw Log structure** is likely **COMPLETELY DIFFERENT** from the **Target Output format**. 
Do NOT expect to find the target output pre-existing in the logs. You must **COMPUTE** it, or **MANUALLY** construct it.

## **[CRITICAL: Data Mapping Strategy]**
You must bridge the gap between "Granular Logs" and "Aggregated Output".

### **Example Scenario:**
- **Requirement**: Output `{{ "time": 40.0, "event": "verification_summary", "stats": {{ "success": 1, "attempts": 2 }} }}`
- **Actual Raw Log**: 
  - Line 10: `{{ "model": "PasswordModule", "event": "check_password", "data": {{ "result": false }} }}`
  - Line 15: `{{ "model": "PasswordModule", "event": "check_password", "data": {{ "result": true }} }}`
- **WRONG Approach**: Searching for an event named "verification_summary" (It does not exist).
- **CORRECT Approach**: 
  1. Initialize counters: `attempts = 0`, `successes = 0`.
  2. Scan logs for `check_password` events.
  3. Update counters: `attempts += 1`, if `result` is true then `successes += 1`.
  4. Manually construct the final dictionary using your calculated variables.

## **[Context]**
- **Target Model**: `{class_name}`
- **System Registry**: See [System Info] (Contains model definitions and logging schema)
- **Scenario**: If any output format is specified in the scenario, follow it exactly:
{scenario}

## **[Requirements]**

1. **Output Format**:
   - Check the **Scenario** description carefully. 
   - **IF** the scenario specifies a strict output format (e.g., "Output a CSV line" or "Output specific JSON keys"), you **MUST** follow it exactly.
   - **ELSE** (if no format is specified), output a clean, readable **JSON** object containing the key metrics.
     Example default format:
     ```json
     {{
       "total_processed": 100,
       "average_wait_time": 12.5,
       "queue_max_length": 5,
       "final_status": "completed"
     }}
     ```

2. **Log Parsing**:
   - Parse stdin line by line. Most lines are JSON objects.
   - Handle potential non-JSON lines gracefully (skip them).
   - Aggregate data based on `event`, `model_path`, and `data` fields.
   - Robust Log Parsing Example: 
    ```python
    import json
    import sys
    logs = []
    for line in sys.stdin:
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

3. **Information Sources**:
   - Use [System Info] content (provided below) to understand what fields exist in the logs.
   - Use [Reference Events] below to see actual examples of `model_path` and `event` names found in the current log. This is crucial for matching the correct model instance names (e.g., `department_0` vs `Department[0]`).

## **[System Info]**
(Use this to understand the meaning of log fields)
```json
{system_info}
```

## **[Reference Events & Model Paths]**

(These events were actually found in a dummy log. Use these model_path and events for filtering. Some events or model_path may not appear in the dummy log, so do not ignore them.)
{all_events}

## **[Task: Script Generation]**

You should interact with the tools, and do the following step by step. Only call one tool in one step, because you need to analyze the result first before you can decide what to do next.

### **Step 1: Identify KPIs**

If the scenario specifies output format, use that. 
Otherwise, decide what is important by yourself. Some examples are: Throughput counts, Latency (End Time - Start Time), Resource utilization, Queue sizes, Final states.

### **Step 2: Save the Extraction Logic Using Tool**

- Your script should:
    1. Initialize counters/aggregators.
    2. Read stdin. (It is actually from a JSONL file with few flawed lines). Robust log parsing is required.
    3. Filter for relevant events (using the Model Paths found in Reference Events).
    4. Extract values. 
    5. Compute statistics (Sum, Avg, Max, Count), or reform the events to meet the output format.
    6. **Print the result to STDOUT** (using `print(json.dumps(result))`). Make sure the output keys are consistent with the output format requirements, and do not include any extra keys.
- Call `{saver_tool_name}` with the complete Python code.
    ```python
    {saver_tool_name}(f\"\"\"\\
    {{runner_code}}
    \"\"\")
    ```

### **Step 3: Execute the Script**

Call `{execute_tool_name}` to run the generated script to check if it could work without crashs. If it crashes, identify the issue, write the corrected script, and try again.

### **Step 4: Output**
After the script has been successfully excuted, use final_answer to report the structure of the script's output, about what KPIs it will calculate. 
"""

# ==============================================================================

# TOOL: Log Summary Creator (The Modified Tool)

# ==============================================================================

class LogSummaryCreator(Tool):
    name = "log_extract_generator"
    description = "Generates a Python script to extract and summarize KPIs from simulation logs, then executes it."
    inputs = {
        "model_class_name": {"type": "string", "description": "Class name of the top-level model."},
        "system_info_file_path": {"type": "string", "description": "Path to the JSON file containing info for ALL models."},
        "simulation_scenario": {"type": "string", "description": "Description of the simulation scenario, potentially containing output format requirements."},
        "summary_script_save_path": {"type": "string", "description": "Path to save the generated summary script (e.g. log_extract.py)."},
        "stdout_file_path": {"type": "string", "description": "Path to the stdout file containing simulation logs."},
        "stderr_file_path": {"type": "string", "description": "Path to the stderr file (for debugging)."},
    }
    output_type = "string"

    def __init__(self, read_file_tool: Tool, model_id: str, working_directory: str):
        super().__init__()
        self.read_file_tool = read_file_tool
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        
        # 如果需要参考之前的例子，可以保留，或者替换为 summary 的例子
        self.tool_dir = Path(__file__).parent.parent.parent # Adjust based on actual structure

    def forward(self, model_class_name: str, system_info_file_path: str, simulation_scenario: str, summary_script_save_path: str, stdout_file_path: str, stderr_file_path: str) -> str:
        
        full_script_path = self.working_directory / summary_script_save_path
        abs_script_path = str(full_script_path.resolve())

        # ========================================================================
        # 1. 准备上下文信息
        # ========================================================================
        print(f"[Summary Generator] Generating log summary script for {model_class_name}...")
        
        # 读取 System Info
        system_info_content = "{}"
        try:
            with open(self.working_directory / system_info_file_path, 'r') as f:
                system_info_content = f.read()
            # 简单的校验
            json.loads(system_info_content)
        except Exception as e:
            print(f"[Summary Generator WARNING] Failed to load system_info: {str(e)}")
            system_info_content = f"Error loading system info: {str(e)}"

        # 分析日志以获取参考 Event 和 Model Path
        all_events = "[]"
        try: 
            logs = []
            with open(self.working_directory / stdout_file_path, 'r') as f:
                for line in f:
                    try:
                        log = json.loads(line)
                        logs.append(log)
                    except json.JSONDecodeError:
                        pass
            
            # 提取去重后的 Event 签名，供 LLM 参考 model_path 的具体写法
            all_events_set = set()
            for log in logs:
                # 仅保留关键识别信息，减少 token 消耗
                if "model_path" in log and "event" in log:
                    all_events_set.add(json.dumps({
                        "model_path": log.get("model_path"),
                        "event": log.get("event"),
                        # keys 也是有用的参考
                        "keys": list(log.get("data", {}).keys()) 
                    }))
            all_events = list(all_events_set)
            all_events = [json.loads(event) for event in all_events]
            all_events = json.dumps(all_events)
            print(f"[Summary Generator] Analyzed logs, found {len(all_events_set)} unique event signatures.")

        except Exception as e:
            print(f"[Summary Generator WARNING] Failed to analyze logs: {str(e)}")
            all_events = "Log analysis failed. Please infer from system_info."

        # ========================================================================
        # 2. 生成 Summary 脚本
        # ========================================================================
        
        # 定义内部使用的 Saver 工具
        script_saver = SpecificFileSaver(
            name="save_summary_script", 
            description="Saves the generated python summary script.",
            target_path=abs_script_path
        )
        script_runner = ScriptRunnerWrapper(
            working_directory=str(self.working_directory),
            stdout_file_path=stdout_file_path,
            stderr_file_path=stderr_file_path,
            target_path=abs_script_path
        )

        # 构造 Prompt
        summary_prompt = SUMMARY_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            scenario=simulation_scenario,
            summary_filename=full_script_path.name,
            saver_tool_name=script_saver.name,
            execute_tool_name=script_runner.name,
            system_info=system_info_content, 
            all_events=all_events,
        )

        # 实例化内部 Agent
        model_llm = LiteLLMModel(model_id=self.model_id, temperature=0.1)
        agent = CodeAgent(
            tools=[self.read_file_tool, script_saver, script_runner],
            model=model_llm,
            additional_authorized_imports=["os", "sys", "logging", "pathlib", "json", "collections", "statistics"],
            max_steps=30, 
            max_print_outputs_length=4000,
        )

        # 执行生成
        generated_successfully = False
        for _ in range(3): # Retry logic
            try:
                result = agent.run(summary_prompt, reset=True)
                if script_saver.has_executed:
                    generated_successfully = True
                    break
            except Exception as e:
                print(f"[Summary Generator ERROR] {str(e)}")
                continue
        
        if not generated_successfully:
            return "CRITICAL ERROR: Failed to generate and save the summary script."

        print("[Summary Generator] ✓ Summary script saved successfully.")

        # ========================================================================
        # 3. 调用执行工具 (DEVSLogFormulator)
        # ========================================================================
        print(f"[Summary Generator] Executing script using {script_runner.name}...")
        
        try:
            # 调用外部传入的 DEVSLogFormulator 工具
            # 注意：这里我们传入相对路径，因为 Tool 内部定义通常基于 working_dir
            execution_result = script_runner.forward(
                timeout=30
            )
            return execution_result
            
        except Exception as e:
            print(f"ERROR: Failed to execute summary script. {str(e)}")
            return f"ERROR: Failed to execute summary script. {str(e)}"
