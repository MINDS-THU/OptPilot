from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import json
import yaml
import shutil
import subprocess
import sys
import tempfile
import time
import contextlib
import re
import signal
from collections import Counter, defaultdict
from ..simulation.devs_execute import DEVSExecute
from ..simulation.verifier_execute import DEVSLogValidator
from .code_modifier import CodeRefiner
from .code_inspector import CodeInspector


REPAIR_STREAM_ARCHIVE_BYTES = 1 * 1024 * 1024
REPAIR_PROCESS_FILE_BYTES = 32 * 1024 * 1024

# ==============================================================================
# PROMPT TEMPLATES (复用并扩展你的标准)
# ==============================================================================
# 这里我们需要包含之前的标准，因为修复代码时也必须遵守这些规范
GLOBAL_STANDARDS = """
### [Global Standards]
- **Imports**: Generated model files use `from xdevs.models import Atomic, Coupled, Port`; other standard-library, project, `numpy`, and `pandas` imports may be added when required.
- **Coding**: Preserve the declared constructor and DEVS interfaces. Do not rewrite working code merely to impose a preferred style.
- **External IO**: Implement required external_io directly with Python IO such as `print(json.dumps(record), flush=True)`, `sys.stdin`, `sys.stderr`, or file writes.
"""

ATOMIC_INSTRUCTIONS = """
### [Atomic Model Specifics]
1. Inherit from `Atomic`; call `super().__init__(name)`, set `self.parent`, and register the exact declared ports.
2. Implement `initialize`, `deltext`, `lambdaf`, `deltint`, and `exit` using the scenario's stated state and timing behavior.
3. Emit DEVS values through `self.output[port].add(value)` when `lambdaf` runs. The simulator calls `lambdaf` before `deltint` at an internal event.
4. State changes inside `lambdaf` are allowed. Fix them only when their observable consequence conflicts with the specification.
5. Use simulation time, and ensure zero-time transition chains terminate.
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
        3. Register Ports: Use `self.add_in_port(...)` and `self.add_out_port(...)`.
        4. Instantiate Components: Create sub-model instances and register them via `self.add_component(instance)`.
        5. Define Couplings: Use `self.add_coupling(src, dst)` for:
            - **EIC**: `self.input["port_name"]` -> `sub.input["port_name"]`
            - **IC**: `sub_a.output["port_name"]` -> `sub_b.input["port_name"]`
            - **EOC**: `sub.output["port_name"]` -> `self.output["port_name"]`
        6. Do not emit external IO unless the coupled model itself requires it.
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

* The stdout/file output format is defined by the model's `external_io` specification and by the actual code. Do not assume logger-specific keys such as `_log_type` or `_model_path` unless the generated code explicitly emits them.

## **[Design Context]**

* **Plan**: {plan}
* **Simulation Context**: {context}
* **Project Structure**: I have summarized the source code of the current model AND all its sub-modules into a single reference file: `{all_models_spec_path}`. You can use it to find out the function, location, and external IO strategy of each model.
* **Execution Log**: It is stored in {stdout_path}. Note that it is quite long, so you should try to only read the snippets or write a log parser to extract relevant information. Do not read the full log directly.
    - Special: You should make sure the verifier do not check for events not included in the following list (unless it check for the event indicating success, but only those indicating failure are in the list): {all_events}

*Note*: If the plan specifies external_io content, you should make sure the output/input content, derivation logic, and timing are correct.

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
    - **WorkFlow**: First check stdout/stderr or external output files to get roughly where the issue originates, and check the runner and verifier to see if they are correct. Then, make a hypothesis. You can also edit the code to add temporary direct `print(...)` diagnostics to help locate the issue and verify the hypothesis. After that, re-run and inspect the new output. Repeat this process until you find the issue, and finally fix it.
    - Editing code check: You can add temporary direct `print(...)` diagnostics and run the simulation to get more information about the model's behavior. Remove or keep them only if they match external_io requirements.
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

FINAL_REPAIR_PROMPT_TEMPLATE = """
You are a coding agent responsible for final runtime repair of a generated DEVS project.

## Mission
Run the generated project, inspect the scenario/spec and generated code, fix obvious runtime or semantic implementation mistakes, and rerun until the project behaves consistently with the scenario or the repair budget is exhausted.

## Scope
- Project directory: `{project_path}`
- Entry point: `{entry_file}`
- Main model file: `{target_file}`
- System/model info: `{all_models_spec_path}`
- Repair budget: at most {max_repair_rounds} run/inspect/fix rounds.

You may modify generated project files under `{project_path}`. Prefer minimal fixes. Keep the top model public constructor and declared ports stable unless the generated code is internally inconsistent with its own plan/spec.
Do not rewrite multiple subsystem files from scratch unless the run is crashing and a narrow fix is impossible. Prefer targeted edits to the runner or the single model where the data flow stops.
Do not add benchmark-specific constants or special cases. Fix the generated implementation so it follows the provided scenario/spec in a general way.
If the scenario says the simulation stops at a fixed horizon, do not try to drain all entities after that horizon. In that case, unfinished entities at the stop time are valid unless the scenario says otherwise.

## Context
Scenario requirements:
```text
{requirements}
```

Model plan/spec:
```text
{model_plan}
```

Runner args summary:
```text
{sim_args}
```

## Tools
- Use `{execute_tool_name}` to run `{entry_file}` directly in `{project_path}`. It stores stdout/stderr for inspection.
- The execution tool returns a compact JSONL summary. Use that summary first.
- Use `{bounded_read_tool_name}` for targeted file snippets. Avoid full-file reads.
- Use `{bounded_list_tool_name}` to discover generated files before changing imports or module paths.
- Use `{refiner_tool_name}` to edit files.

## Process
Repeat a simple loop:
1. Run the entry point.
2. Inspect stdout/stderr and relevant code.
3. Identify the concrete implementation issue.
4. Apply the smallest useful fix.
5. Rerun.

Before editing, write down the diagnosis in your own reasoning:
- the failing symptom from `direct_project_execute`,
- the file/function where the defect is located,
- the evidence that rules out a runner-only problem versus an atomic/coupled model state problem,
- the smallest code change that can address that specific defect.

Do not call `{refiner_tool_name}` until this diagnosis is concrete. The edit instruction passed to `{refiner_tool_name}` must name the exact file/function to change and must preserve all unrelated behavior.

Keep the loop concise. Do not print full source files or full stdout logs into the conversation. Do not read the full stdout file unless it has only a few lines. Prefer the execution tool's JSONL summary, tail output, short targeted file snippets, and small event-count summaries. If stdout is large, inspect only enough records to locate the issue.

Do not build a separate checker framework. You may write small temporary analysis snippets in your reasoning, but keep committed project changes focused on the generated DEVS code and runner.

If the run times out but stdout contains many valid records and stderr has no traceback, first decide whether this is:
- a runner horizon problem,
- an atomic/coupled model scheduling problem,
- or a same-time feedback loop / repeated event with no time progress.
For fixed-horizon scenarios, do not rewrite the runner unless the runner demonstrably ignores `simulation_time`, uses a drain-until-empty policy, or crashes. If stdout/stderr shows repeated same-time events, duplicate helper availability, stale returns, or no time progress, treat it as a model state/scheduling defect and inspect the relevant atomic model before editing the runner.
Before changing imports, module paths, class names, or replacing a whole file, first confirm the actual generated file names and local APIs from the project. Do not invent module paths or class names.
If the compact summary shows a lifecycle or event-count gap, and a targeted code snippet shows a pending payload or output variable being overwritten inside a loop over multiple due items, fix that multiplicity bug immediately by storing all pending payloads in a list/queue and emitting each item. Do not keep reading more files after this concrete issue is found.
Use concrete numeric statements found in the scenario text as checks when they exist, such as a stated default number of generated arrivals. Do not invent expected metrics that are not stated in the scenario text.

Do not call `final_answer` with a plan, a readiness statement, or a partial diagnosis. If the run fails, times out, or the output summary contradicts the scenario/spec, you must apply at least one targeted code fix and rerun before finishing. Only finish without editing when the direct run already succeeds and the compact output summary is consistent with the scenario/spec.
If the repair budget is exhausted after at least one fix and rerun, finish with the final run status and the remaining concrete issue instead of starting broad rewrites.

Finish by calling `final_answer` with a concise summary that includes:
- the final `direct_project_execute` status,
- whether code was changed,
- the concrete issue fixed, if any.
"""


class DirectProjectExecute(Tool):
    name = "direct_project_execute"
    description = "Run the generated project entry point directly in its project directory and save stdout/stderr logs."
    inputs = {
        "timeout": {
            "type": "integer",
            "description": "Maximum wall-clock seconds for this run.",
            "nullable": True,
        },
        "command_args": {
            "type": "string",
            "description": "Command line args passed to the entry point.",
            "nullable": True,
        },
        "stdin_content": {
            "type": "string",
            "description": "Optional stdin content.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        working_directory: str,
        project_path: str,
        entry_file: str = "run.py",
        stdout_file: str = "final_repair_stdout.txt",
        stderr_file: str = "final_repair_stderr.txt",
    ):
        super().__init__()
        self.working_directory = Path(working_directory).resolve()
        self.project_path = project_path
        self.project_dir = (self.working_directory / project_path).resolve()
        self.entry_file = entry_file
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file

    def forward(self, timeout: int = 120, command_args: str | None = None, stdin_content: str | None = None) -> str:
        if timeout is None:
            timeout = 120
        if not str(self.project_dir).startswith(str(self.working_directory)):
            return "STATUS: FAILED\nReason: project_path is outside working_directory."
        entry_path = self.project_dir / self.entry_file
        if not entry_path.exists():
            return f"STATUS: FAILED\nReason: entry point not found: {self.project_path}/{self.entry_file}"

        cmd = [sys.executable, self.entry_file]
        if command_args:
            import shlex
            cmd.extend(shlex.split(command_args))

        prlimit = Path("/usr/bin/prlimit")
        if prlimit.is_file():
            cmd = [
                str(prlimit),
                f"--fsize={REPAIR_PROCESS_FILE_BYTES}",
                "--as=4294967296",
                "--nofile=256",
                "--core=0",
                "--",
                *cmd,
            ]

        start = time.time()
        with tempfile.TemporaryFile() as stdout_temp, tempfile.TemporaryFile() as stderr_temp:
            process = None
            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_dir),
                    stdin=subprocess.PIPE,
                    stdout=stdout_temp,
                    stderr=stderr_temp,
                    text=True,
                    start_new_session=(os.name == "posix"),
                )
                process.communicate(input=stdin_content, timeout=timeout)
                returncode = process.returncode
                status = "SUCCESS" if returncode == 0 else "FAILED"
            except subprocess.TimeoutExpired:
                assert process is not None
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
                returncode = 124
                status = "TIMEOUT"
            stdout_temp.seek(0)
            stderr_temp.seek(0)
            stdout_bytes = stdout_temp.read(REPAIR_STREAM_ARCHIVE_BYTES + 1)
            stderr_bytes = stderr_temp.read(REPAIR_STREAM_ARCHIVE_BYTES + 1)
        stdout_truncated = len(stdout_bytes) > REPAIR_STREAM_ARCHIVE_BYTES
        stderr_truncated = len(stderr_bytes) > REPAIR_STREAM_ARCHIVE_BYTES
        stdout = stdout_bytes[:REPAIR_STREAM_ARCHIVE_BYTES].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:REPAIR_STREAM_ARCHIVE_BYTES].decode("utf-8", errors="replace")
        if status == "TIMEOUT":
            stderr += f"\n[SYSTEM] timed out after {timeout}s"

        stdout_path = self.project_dir / self.stdout_file
        stderr_path = self.project_dir / self.stderr_file
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
        summary = self._summarize_stdout(stdout)

        return (
            f"STATUS: {status}\n"
            f"RETURNCODE: {returncode}\n"
            f"TIME: {time.time() - start:.2f}s\n"
            f"ARCHIVE_TRUNCATED: stdout={stdout_truncated}, stderr={stderr_truncated}\n"
            f"STDOUT_FILE: {self.project_path}/{self.stdout_file}\n"
            f"STDERR_FILE: {self.project_path}/{self.stderr_file}\n"
            f"JSONL_SUMMARY:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n"
            f"STDOUT_TAIL:\n{stdout[-1000:]}\n"
            f"STDERR_TAIL:\n{stderr[-1000:]}"
        )

    def _coerce_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _summarize_stdout(self, stdout: str) -> dict:
        event_counts = Counter()
        unique_order_ids = defaultdict(set)
        ids_by_event = defaultdict(lambda: defaultdict(set))
        bad_jsonl = 0
        parsed = 0
        max_time = None
        first_records = []
        last_records = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad_jsonl += 1
                continue
            if not isinstance(obj, dict):
                bad_jsonl += 1
                continue
            parsed += 1
            if len(first_records) < 3:
                first_records.append(obj)
            last_records.append(obj)
            if len(last_records) > 3:
                last_records.pop(0)
            if isinstance(obj.get("time"), (int, float)):
                max_time = obj["time"] if max_time is None else max(max_time, float(obj["time"]))
            event = obj.get("event")
            if event:
                event_counts[event] += 1
                payload = obj.get("payload") or {}
                if isinstance(payload, dict):
                    order_id = payload.get("order_id")
                    if order_id is not None:
                        unique_order_ids[event].add(order_id)
                    for key, value in payload.items():
                        if key == "id" or key.endswith("_id"):
                            try:
                                hash(value)
                            except TypeError:
                                continue
                            ids_by_event[key][event].add(value)
        lifecycle_gaps = self._summarize_lifecycle_gaps(ids_by_event)
        return {
            "line_count": len(stdout.splitlines()),
            "parsed_json_records": parsed,
            "bad_jsonl_lines": bad_jsonl,
            "max_time": max_time,
            "event_counts": dict(event_counts),
            "unique_order_id_counts": {k: len(v) for k, v in unique_order_ids.items()},
            "lifecycle_gaps": lifecycle_gaps,
            "first_records": first_records,
            "last_records": last_records,
        }

    def _summarize_lifecycle_gaps(self, ids_by_event: dict) -> list[dict]:
        create_words = ("generated", "created", "arrived", "arrival", "submitted", "started")
        terminal_words = (
            "completed",
            "delivered",
            "departed",
            "abandoned",
            "rejected",
            "dropped",
            "expired",
            "cancelled",
            "canceled",
            "failed",
            "done",
        )
        finished_terminal_nouns = (
            "delivery",
            "shipment",
            "transport",
            "order",
            "customer",
            "job",
            "task",
            "request",
            "service",
        )
        gaps = []
        for id_field, events in ids_by_event.items():
            created_ids = set()
            terminal_ids = set()
            create_events = []
            terminal_events = []
            for event, values in events.items():
                event_l = str(event).lower()
                if any(word in event_l for word in create_words):
                    created_ids.update(values)
                    create_events.append(event)
                is_terminal = (
                    any(word in event_l for word in terminal_words)
                    or (
                        "finished" in event_l
                        and any(word in event_l for word in finished_terminal_nouns)
                    )
                )
                if is_terminal:
                    terminal_ids.update(values)
                    terminal_events.append(event)
            if created_ids and terminal_events and len(terminal_ids) < len(created_ids):
                gaps.append({
                    "id_field": id_field,
                    "created_count": len(created_ids),
                    "terminal_count": len(terminal_ids),
                    "created_events": sorted(set(create_events)),
                    "terminal_events": sorted(set(terminal_events)),
                })
        return gaps


class BoundedProjectRead(Tool):
    name = "bounded_project_read"
    description = "Read a bounded slice of a generated project file. Use this instead of reading whole large files or logs."
    inputs = {
        "path": {
            "type": "string",
            "description": "Path relative to the working directory.",
        },
        "start_line": {
            "type": "integer",
            "description": "1-based starting line number. Defaults to 1.",
            "nullable": True,
        },
        "line_count": {
            "type": "integer",
            "description": "Maximum number of lines to read. Defaults to 120.",
            "nullable": True,
        },
        "max_chars": {
            "type": "integer",
            "description": "Maximum returned characters. Defaults to 8000.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, working_directory: str):
        super().__init__()
        self.working_directory = Path(working_directory).resolve()

    def forward(
        self,
        path: str,
        start_line: int | None = 1,
        line_count: int | None = 120,
        max_chars: int | None = 8000,
    ) -> str:
        start_line = max(1, int(start_line or 1))
        line_count = max(1, min(int(line_count or 120), 160))
        max_chars = max(1000, min(int(max_chars or 8000), 12000))

        target = (self.working_directory / path).resolve()
        if not str(target).startswith(str(self.working_directory)):
            return "ERROR: path is outside working_directory."
        if not target.exists():
            return f"ERROR: file not found: {path}"
        if not target.is_file():
            return f"ERROR: path is not a file: {path}"

        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        begin = start_line - 1
        end = min(len(lines), begin + line_count)
        selected = [f"{idx + 1:04d} | {lines[idx]}" for idx in range(begin, end)]
        text = "\n".join(selected)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated by bounded_project_read] ..."
        return f"FILE: {path}\nLINES: {start_line}-{end} of {len(lines)}\n{text}"


class BoundedProjectList(Tool):
    name = "bounded_project_list"
    description = "List files and directories under the generated project working directory with bounded output."
    inputs = {
        "path": {
            "type": "string",
            "description": "Directory path relative to the working directory. Defaults to '.'.",
            "nullable": True,
        },
        "max_entries": {
            "type": "integer",
            "description": "Maximum number of entries to return. Defaults to 200.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, working_directory: str):
        super().__init__()
        self.working_directory = Path(working_directory).resolve()

    def forward(self, path: str | None = ".", max_entries: int | None = 200) -> str:
        max_entries = max(1, min(int(max_entries or 200), 500))
        target = (self.working_directory / (path or ".")).resolve()
        if not str(target).startswith(str(self.working_directory)):
            return "ERROR: path is outside working_directory."
        if not target.exists():
            return f"ERROR: directory not found: {path}"
        if not target.is_dir():
            return f"ERROR: path is not a directory: {path}"

        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            kind = "dir" if child.is_dir() else "file"
            rel = child.relative_to(self.working_directory)
            entries.append(f"{kind:4s} {rel}")
            if len(entries) >= max_entries:
                entries.append("... [truncated by bounded_project_list] ...")
                break
        return f"DIR: {path or '.'}\n" + "\n".join(entries)


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
        self.injected_utils = ["set_global_clock", "get_current_time", "injection_tools"]

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
                    "event": log.get("event"),
                    "record_keys": list(log.keys()),
                }))
            all_events = json.dumps(list(all_events_set))
            print(f"[Fixer] Analyzed {len(logs)} logs, found events: {all_events}")
        except Exception as e:
            print(f"[Fixer WARNING] Failed to load logs: {str(e)}")
            all_events = "Failed to analyze the log, please carefully check all the instance names and events"


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

    def final_repair(
        self,
        project_path: str,
        entry_file: str,
        target_file_path: str,
        all_models_spec_path: str,
        requirements: str,
        model_plan: str,
        sim_args: str,
        max_repair_rounds: int = 25,
        timeout: int = 120,
    ) -> str:
        execute_tool = DirectProjectExecute(
            working_directory=str(self.working_directory),
            project_path=project_path,
            entry_file=entry_file,
        )
        bounded_read_tool = BoundedProjectRead(str(self.working_directory))
        bounded_list_tool = BoundedProjectList(str(self.working_directory))
        smoke_command_args = self._infer_final_repair_smoke_args(requirements)

        prompt = FINAL_REPAIR_PROMPT_TEMPLATE.format(
            project_path=project_path,
            entry_file=entry_file,
            target_file=target_file_path,
            all_models_spec_path=all_models_spec_path,
            requirements=requirements,
            model_plan=model_plan,
            sim_args=sim_args,
            max_repair_rounds=max_repair_rounds,
            execute_tool_name=execute_tool.name,
            bounded_read_tool_name=bounded_read_tool.name,
            bounded_list_tool_name=bounded_list_tool.name,
            refiner_tool_name=self.code_refiner.name,
        )
        prompt += (
            f"\n\nStart by calling `{execute_tool.name}` with timeout={timeout}, "
            f"command_args={smoke_command_args!r}, stdin_content=''. "
            "Use these same smoke-run args for reruns unless you are specifically "
            "diagnosing CLI argument parsing."
        )

        prompt_log_dir = None
        try:
            spec_parent = (self.working_directory / all_models_spec_path).parent
            prompt_log_dir = spec_parent if spec_parent.name == "_analysis_logs" else spec_parent / "_analysis_logs"
            prompt_log_dir.mkdir(parents=True, exist_ok=True)
            (prompt_log_dir / "final_runtime_repair_prompt.txt").write_text(prompt, encoding="utf-8")
        except Exception:
            pass

        try:
            precheck_report = execute_tool.forward(timeout=timeout, command_args=smoke_command_args, stdin_content="")
            if prompt_log_dir is not None:
                (prompt_log_dir / "final_runtime_repair_precheck.txt").write_text(
                    precheck_report,
                    encoding="utf-8",
                )

            status_line = precheck_report.splitlines()[0] if precheck_report.splitlines() else ""
            summary = {}
            if "JSONL_SUMMARY:\n" in precheck_report and "\nSTDOUT_TAIL:" in precheck_report:
                summary_text = precheck_report.split("JSONL_SUMMARY:\n", 1)[1].split("\nSTDOUT_TAIL:", 1)[0]
                try:
                    summary = json.loads(summary_text)
                except json.JSONDecodeError:
                    summary = {}
            req_l = requirements.lower()
            metric_mismatches = []
            generated_match = re.search(r"produces\s+(\d+)\s+generated\s+orders", req_l)
            if generated_match:
                expected_generated = int(generated_match.group(1))
                actual_generated = (summary.get("event_counts") or {}).get("order_generated")
                if actual_generated != expected_generated:
                    metric_mismatches.append({
                        "metric": "order_generated",
                        "expected": expected_generated,
                        "actual": actual_generated,
                    })
            fixed_horizon = (
                "simulation stops at `simulation_time`" in req_l
                or "simulation stops at simulation_time" in req_l
                or "does not continue after closing" in req_l
                or "after `simulation_time` are not processed or reported" in req_l
                or "after simulation_time are not processed or reported" in req_l
            )
            if (
                status_line == "STATUS: SUCCESS"
                and int(summary.get("bad_jsonl_lines", 0)) == 0
                and not metric_mismatches
                and (fixed_horizon or not summary.get("lifecycle_gaps"))
            ):
                return (
                    "FINAL REPAIR SKIPPED: direct_project_execute STATUS=SUCCESS; "
                    f"bad_jsonl_lines={summary.get('bad_jsonl_lines', 0)}; "
                    f"fixed_horizon={fixed_horizon}; "
                    f"metric_mismatches={metric_mismatches}; "
                    f"lifecycle_gaps={summary.get('lifecycle_gaps', [])}; "
                    "no coding-agent repair needed."
                )

            tools = [bounded_read_tool, bounded_list_tool]
            if "list" in self.file_system_tools_dict:
                tools.append(self.file_system_tools_dict["list"])
            tools.extend([self.code_refiner, execute_tool])

            model = LiteLLMModel(model_id=self.model_id, temperature=0.1)
            agent = CodeAgent(
                tools=tools,
                model=model,
                additional_authorized_imports=[
                    "collections",
                    "json",
                    "math",
                    "os",
                    "pathlib",
                    "re",
                    "statistics",
                    "sys",
                ],
                max_steps=max(6, max_repair_rounds * 3),
                max_print_outputs_length=2500,
            )

            if prompt_log_dir is not None:
                agent_log_path = prompt_log_dir / "final_runtime_repair_agent.log"
                with agent_log_path.open("w", encoding="utf-8", errors="replace") as log_f:
                    with contextlib.redirect_stdout(log_f), contextlib.redirect_stderr(log_f):
                        result = agent.run(prompt, reset=True)
            else:
                result = agent.run(prompt, reset=True)
            return f"FINAL REPAIR COMPLETED: {result}"
        except Exception as e:
            return f"FINAL REPAIR FAILED: {e}"

    def _infer_final_repair_smoke_args(self, requirements: str) -> str:
        req_l = (requirements or "").lower()
        if "--periods" in req_l:
            return "--periods 3"
        return ""
