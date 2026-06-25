import os
import sys
import json
import yaml
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import traceback

# 📌 加载 .env
load_dotenv(override=True)

import litellm

litellm.register_model(
    {
        "openrouter/deepseek/deepseek-v3.2": {
            "max_output_tokens": 60_000,
        },
        "openrouter/qwen/qwen3-coder": {
            "max_output_tokens": 100_000,
        },
    }
)

# === OpenHands 相关模块 ===
from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
# from openhands.sdk.context.condenser import LLMSummarizingCondenser

XDEVS_FRAMEWORK_GUIDE = """
## xdevs.py Framework Guide

You MUST use **xdevs.py** (formal DEVS framework) for this simulation.

### Core API
```python
from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock
```

### Atomic Model Template
```python
class ModelName(Atomic):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        self.hold_in("<INITIAL_PHASE>", <sigma>)

    def initialize(self):
        self.hold_in("<PHASE>", <sigma>)

    def lambdaf(self):
        # Output ONLY. Do NOT modify state.
        self.output["<port_name>"].add(<payload>)

    def deltint(self):
        # Internal transition (timeout). Update state.
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def deltext(self, e):
        # External transition (input). Process self.input["port"].values
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def exit(self):
        # Cleanup and final KPI logging
        pass
```

### Coupled Model Template (ONLY __init__)
```python
class SystemName(Coupled):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        # Instantiate sub-models
        child = ChildModel(name="child_0", parent=self, <config>)
        self.add_component(child)
        # Define couplings
        self.add_coupling(self.input["port"], child.input["port"])        # EIC
        self.add_coupling(child_a.output["port"], child_b.input["port"])  # IC
        self.add_coupling(child.output["port"], self.output["port"])      # EOC
```

### Entry Point (run.py)
```python
from xdevs.sim import Coordinator, SimulationClock

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate_time", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    root = SystemName(name="system", parent=None, <config>)
    coord = Coordinator(root, clock=SimulationClock(0))
    coord.initialize()
    coord.simulate_time(args.simulate_time)

if __name__ == "__main__":
    main()
```

### Critical Rules
1. **ALL Atomic models MUST implement all 5 abstract methods**: `initialize`, `lambdaf`, `deltint`, `deltext`, `exit`. Missing any one will cause `TypeError: Can't instantiate abstract class`.
2. **Use `Coordinator`** for running simulations with Coupled models, NOT `Simulator` (which is for Atomic-only models).
3. **Event sequence**: `lambdaf` sends output BEFORE `deltint` schedules next event. Payload must be prepared in previous `deltint`/`deltext`/`initialize`.
4. **lambdaf purity**: Only output, no state modification.
5. **hold_in**: Always call at end of `initialize`, `deltint`, `deltext`.
6. **Initial signals**: Use `self.hold_in("INIT", 0)` to schedule immediate event.
7. **Coupled models**: ONLY `__init__` — no `deltint`, `deltext`, `lambdaf`, `initialize`.
8. **add_component** BEFORE **add_coupling**.
9. **JSONL output**: `print(json.dumps({"time": ..., "entity": "...", "event": "...", "payload": {...}}), file=sys.stdout, flush=True)`
10. **Debug to stderr**, JSONL to stdout.

### File Organization
You may choose single-file or multi-file:
- **Single-file**: All models in `run.py` (no imports between model files)
- **Multi-file**: `run.py` + `<SystemName>_libs/` directory with `__init__.py` and sub-model files
  - Use relative imports: `from <SystemName>_libs.ChildModel import ChildModel`
"""

BARE_XDEVS_PROMPT = """You are a DEVS simulation expert. Build a complete Python simulation system using **xdevs.py** (formal DEVS framework).

## Requirements
{requirements}

{xdevs_framework_guide}

## Workflow

### Step 1: Plan the Component Hierarchy
Think about the system structure:
- What are the main components (Atomic models)?
- How do they connect (Coupled model with EIC/IC/EOC)?
- What are the input/output ports for each component?
- What parameters does each component need?
- Decide: single-file or multi-file organization

### Step 2: Implement
1. Create all Atomic model classes with proper state machines
2. Create the Coupled model that composes sub-models
3. Write `run.py` as the entry point with argparse and Simulator
4. Ensure JSONL output matches the specification exactly

### Step 3: Test
Run the simulation with basic parameters to verify it works.

## CRITICAL: I/O Format Compliance
The Input/Output format is a STRICT CONTRACT. Follow it exactly — field names, value formats, entity names, event types, and JSON structure.
"""

def extract_model_usage(convo: Conversation | None) -> dict:
    """Best-effort token usage extraction from conversation stats."""
    if convo is None:
        return {}
    stats = getattr(convo, "conversation_stats", None)
    if stats is None:
        return {}
    usage_to_metrics = getattr(stats, "usage_to_metrics", None)
    if not usage_to_metrics:
        return {}

    model_usage = {}
    for _, met in usage_to_metrics.items():
        token_usage = getattr(met, "accumulated_token_usage", None)
        if token_usage is None:
            continue
        model_name = getattr(token_usage, "model", "unknown")
        input_tokens = int(getattr(token_usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(token_usage, "completion_tokens", 0) or 0)
        thinking = int(getattr(token_usage, "reasoning_tokens", 0) or 0)
        calls = len(getattr(met, "token_usages", []) or [])
        model_usage[model_name] = {
            "input": input_tokens,
            "output": output_tokens,
            "thinking": thinking,
            "calls": calls,
        }
    return model_usage


def load_task_requirements(config_path: Path) -> str:
    """
    读取配置文件中的 requirements 字段作为任务描述
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        if config_path.suffix.lower() == ".json":
            params = json.load(f)
        else:
            params = yaml.safe_load(f)

    raw_requirements = str(params.get("requirements", "")).strip()
    requirements = "\n".join(
        [
            raw_requirements,
            "Constraint: You MUST create a Python project.",
            "The entry point MUST be named 'run.py'.",
            "After finish all the tasks, save and submit. ",
        ]
    )
    requirements = BARE_XDEVS_PROMPT.format(
        requirements=requirements,
        xdevs_framework_guide=XDEVS_FRAMEWORK_GUIDE,
    )
    return requirements


def init_agent(model_name: str) -> Agent:
    """
    使用 OpenHands SDK 初始化一个 agent
    """
    if model_name.startswith("openrouter/"):
        # model_name = model_name[len("openrouter/"):]
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        api_base = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    elif model_name.startswith("nebius/"):
        # model_name = model_name[len("nebius/"):]
        api_key = os.getenv("NEBIUS_API_KEY", "")
        api_base = os.getenv("NEBIUS_API_BASE", "https://nebius.ai/api/v1")
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        api_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # if model_name == "openrouter/deepseek/deepseek-v3.2":
    #     # print("DeepSeek max_tokens:", litellm.get_max_tokens("openrouter/deepseek/deepseek-v3.2"))
    #     llm = LLM(
    #         model=model_name,
    #         api_key=api_key,
    #         base_url=api_base,
    #         max_output_tokens=120_000,
    #     )
    # if model_name == "openrouter/qwen/qwen3-coder":
    #     llm = LLM(
    #         model=model_name,
    #         api_key=api_key,
    #         base_url=api_base,
    #         max_output_tokens=200_000,
    #     )
    # else:
    #     llm = LLM(
    #         model=model_name,
    #         api_key=api_key,
    #         base_url=api_base,
    #     )

    llm = LLM(
        model=model_name,
        api_key=api_key,
        base_url=api_base,
    )

    # 配置 agent 所需工具
    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]
    # condenser = LLMSummarizingCondenser(
    #     llm=llm.model_copy(update={"usage_id": "condenser"}), max_size=10, keep_first=2
    # )

    return Agent(
        llm=llm,
        tools=tools,
        # condenser=condenser,
    )


def run_task(agent: Agent, req_text: str, workspace: Path) -> tuple[dict, str]:
    """
    让 OpenHands agent 在 workspace 上执行任务。
    无论成功失败，都尽量返回 token_usage。
    """
    convo: Conversation | None = None
    run_error = ""
    try:
        convo = Conversation(agent=agent, workspace=str(workspace))
        convo.send_message(req_text)
        convo.run()
    except Exception:
        run_error = traceback.format_exc()
    model_usage = extract_model_usage(convo)
    return model_usage, run_error


def main():
    parser = argparse.ArgumentParser(description="OpenHands Automation Wrapper")
    parser.add_argument(
        "--config", required=True, help="Task YAML/JSON file with requirements"
    )
    parser.add_argument("--workspace", required=True, help="Output workspace root")
    parser.add_argument(
        "--model_id",
        default="gpt-4o",
        help="LLM model name (e.g., gpt-4o, claude-3-5-sonnet)",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # === 读取任务需求 ===
    config_path = Path(args.config).resolve()
    try:
        requirement_text = load_task_requirements(config_path)
    except Exception as e:
        print(f"[Error] 读取任务要求失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not requirement_text:
        print(
            "[Warning] 'requirements' 字段为空，将继续执行，但 agent 可能不会有效工作。"
        )

    print(f"[Info] Requirement:\n{requirement_text}\n")

    # === 初始化 OpenHands agent ===
    print(f"[Info] 初始化 OpenHands agent, model: {args.model_id}")
    try:
        agent = init_agent(args.model_id)
    except Exception as e:
        print(f"[Error] 初始化 agent 失败: {e}", file=sys.stderr)
        sys.exit(1)

    start_time = datetime.now()
    status = "failed"
    error_msg = ""

    # === 让 agent 执行任务 ===
    run_result, run_error = run_task(agent, requirement_text, workspace)
    if run_error:
        status = "error"
        error_msg = run_error
    else:
        status = "success"
    print(f"[Info] 任务执行统计: {run_result}")

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # === 检查生成的 run.py ===
    sim_cwd = ""
    sim_entry = ""
    if status == "success":
        run_py = workspace / "run.py"
        if run_py.exists():
            sim_cwd = str(run_py.parent)
            sim_entry = run_py.name
        else:
            status = "failed"
            error_msg = "Agent completed but 'run.py' was not found."

    # === 输出结果格式 ===
    output_info = {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": error_msg,
        "agent": "OpenHands",
        "token_usage": run_result,
    }

    print(
        f"\n<<<GENERATION_RESULT>>>\n{json.dumps(output_info, indent=2)}\n<<<GENERATION_RESULT>>>"
    )


if __name__ == "__main__":
    main()
