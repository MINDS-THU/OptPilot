import os
import sys
import json
import yaml
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import traceback
import shutil

# 📌 加载 .env
load_dotenv(override=True)

import litellm
litellm.register_model({
    "openrouter/deepseek/deepseek-v3.2": {
        "max_output_tokens": 60_000,
    },
    "openrouter/qwen/qwen3-coder": {
        "max_output_tokens": 100_000,
    }
})

# === OpenHands 相关模块 ===
from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
# from openhands.sdk.context.condenser import LLMSummarizingCondenser

def prepare_workspace_assets(workspace: Path, gen_plan_folder: Path, materials_root: Path = Path("./materials")):
    """
    📌 预处理步骤：Fail fast 模式 (无 try-except)
    """
    print(f"[Setup] Preparing workspace at {workspace}...")
    
    # --- 1. 处理生成计划 (Architecture Plans) ---
    dest_plan_dir = workspace / "architecture_plans"
    dest_plan_dir.mkdir(parents=True, exist_ok=True)
    
    # 直接列出所有计划文件
    plan_files = list(gen_plan_folder.glob("*_architecture_plan.json"))
    if not plan_files:
        print(f"[Warning] No architecture plans found in {gen_plan_folder}")

    for plan_file in plan_files:
        # 直接读取，出错直接崩
        with open(plan_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 移除 _context_used
        if "_context_used" in data:
            del data["_context_used"]
            
        old_file_path = data['model_info']['file_path']
        new_file_path = "/".join(old_file_path.split("/")[1:])
        data['model_info']['file_path'] = new_file_path
        
        # 写入
        with open(dest_plan_dir / plan_file.name, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  -> Processed plan: {plan_file.name}")

    # --- 2. 处理 xDEVS 参考资料 ---
    ref_dir = workspace / "reference_assets"
    ref_dir.mkdir(parents=True, exist_ok=True)
    
    # (1) 复制定义文件
    def_file = materials_root / "definitions.md"
    if def_file.exists():
        shutil.copy(def_file, ref_dir / "definitions.md")
    else:
        print(f"[Warning] definitions.md not found at {def_file}")
    
    # (2) 复制示例代码 (支持通配符匹配)
    devs_project_src = materials_root / "devs_project"
    
    # 定义需要匹配的模式，对应 atomic_example_xxx, coupled_example_xxx, runner_example_xxx
    patterns = [
        "atomic_example_*.py", 
        "coupled_example_*.py", 
        "runner_example_*.py"  # 包含 runner_example_inject.py
    ]
    
    for pattern in patterns:
        found_files = list(devs_project_src.glob(pattern))
        for src in found_files:
            shutil.copy(src, ref_dir / src.name)
            print(f"  -> Copied reference: {src.name}")

    # (3) 复制工具包 (devs_utils)
    utils_src = devs_project_src / "devs_utils"
    
    if utils_src.exists():
        dest_utils = ref_dir / "devs_utils"
        if dest_utils.exists():
            shutil.rmtree(dest_utils) # 先删后拷，确保干净
        shutil.copytree(utils_src, dest_utils)
        print("  -> Copied devs_utils package")
    
    # (4) 复制说明文档
    utils_desc = materials_root / "utils_desc.yaml"
    if utils_desc.exists():
        shutil.copy(utils_desc, ref_dir / "utils_desc.yaml")

def load_task_requirements(config_path: Path) -> str:
    """
    读取配置文件，并注入关于 workspace 中已存在文件的上下文提示
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        if config_path.suffix.lower() == ".json":
            params = json.load(f)
        else:
            params = yaml.safe_load(f)

    raw_requirements = str(params.get("requirements", "")).strip()
    
    # 构建增强版 Prompt
    requirements = "\n".join([
        "### Project Requirements",
        raw_requirements,
        "",
        "### Operational Constraints",
        "1. **Entry Point**: You MUST create a Python project where the entry point is named `run.py`.",
        "   - The user will execute: `python run.py [args]`.",
        "   - If your logic is split into multiple files, `run.py` must act as the wrapper/CLI entry.",
        "2. **Simulation Tool Selection**: You have two choices for Discrete Event Simulation (DES):",
        "   - **Option A (Default)**: Use `simpy` (standard python library).",
        "   - **Option B (Advanced)**: Use `xdevs` (a custom/niche library).",
        "",
        "### Context & Assets Provided",
        "I have prepared the following files in your current workspace:",
        "",
        "**1. Architecture Plans (Input Data):**",
        "   - Location: `./architecture_plans/`",
        "   - Contains JSON files defining the models (`xx_architecture_plan.json`).",
        "   - **Rule**: Implement the models as described in these plans. If a plan conflicts with the requirements text, prioritize the **requirements text**.",
        "",
        "**2. xDEVS Reference Materials (If choosing Option B):**",
        "   - Location: `./reference_assets/`",
        "   - `definitions.md`: Conceptual definitions of Atomic, Coupled, Component, Port.",
        "   - `*_example_*.py`: Code examples showing how to implement Atomic/Coupled models and how to RUN them.",
        "   - `runner_example_inject.py`: **Critical**: Use this to understand how to initialize/inject the simulation runner (it differs from simpy).",
        "   - `devs_utils/`: A folder containing utility code you can import and use.",
        "   - `utils_desc.yaml`: Documentation for the utilities.",
        "",
        "### Final Instruction",
        "After finishing implementation, ensure `python run.py --help` works and the simulation runs correctly.",
        "Save all files and submit."
    ])
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

def run_task(agent: Agent, req_text: str, workspace: Path) -> dict:
    """
    让 OpenHands agent 在 workspace 上执行任务
    """
    convo = Conversation(
        agent=agent, workspace=str(workspace),
        max_iteration_per_run=50
    )
    convo.send_message(req_text)
    convo.run()
    
    stats = convo.conversation_stats
    model_usage = {}
    for _, met in stats.usage_to_metrics.items():
        token_usage = met.accumulated_token_usage
        assert token_usage is not None
        model_name = token_usage.model
        input_tokens = token_usage.prompt_tokens
        output_tokens = token_usage.completion_tokens
        thinking = token_usage.reasoning_tokens
        model_usage[model_name] = {
            "input": input_tokens,
            "output": output_tokens,
            "thinking": thinking,
            "calls": len(met.token_usages)
        }
    return model_usage

def main():
    parser = argparse.ArgumentParser(description="OpenHands Automation Wrapper")
    parser.add_argument("--config", required=True, help="Task YAML/JSON file with requirements")
    parser.add_argument("--gen_plan_folder", required=True, help="Folder containing architecture plans")
    parser.add_argument("--workspace", required=True, help="Output workspace root")
    parser.add_argument("--model_id", default="gpt-4o", help="LLM model name")
    # 如果 materials 路径不固定，也可以作为参数传入，这里暂时硬编码相对路径
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    
    gen_plan_folder = Path(args.gen_plan_folder).resolve()
    
    # === 0. 准备工作区 (Pre-processing) ===
    # 假设脚本运行时，materials 文件夹在当前目录下
    materials_path = Path("./materials").resolve()
    
    try:
        prepare_workspace_assets(workspace, gen_plan_folder, materials_path)
    except Exception as e:
        print(f"[Error] 准备工作区失败: {e}", file=sys.stderr)
        # 即使准备资产失败，也可以选择是否继续，或者直接退出。这里选择退出以防 Agent 瞎写。
        sys.exit(1)

    # === 1. 读取任务需求 ===
    config_path = Path(args.config).resolve()
    try:
        requirement_text = load_task_requirements(config_path)
    except Exception as e:
        print(f"[Error] 读取任务要求失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[Info] Requirement with context loaded.\n")

    # === 2. 初始化 Agent ===
    print(f"[Info] 初始化 OpenHands agent, model: {args.model_id}")
    try:
        agent = init_agent(args.model_id)
    except Exception as e:
        print(f"[Error] 初始化 agent 失败: {e}", file=sys.stderr)
        sys.exit(1)

    start_time = datetime.now()
    status = "failed"
    error_msg = ""
    run_result = {}

    try:
        # === 3. 让 agent 执行任务 ===
        run_result = run_task(agent, requirement_text, workspace)
        status = "success"
        print(f"[Info] 任务执行统计: {run_result}")
    except Exception as e:
        error_msg = traceback.format_exc()
        status = "error"
        print(f"[Error] Agent execution failed: {e}")

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # === 4. 检查生成的 run.py ===
    sim_cwd = ""
    sim_entry = ""
    if status == "success":
        run_py = workspace / "run.py"
        if run_py.exists():
            sim_cwd = str(workspace) # 通常就在 workspace 根目录
            sim_entry = "run.py"
        else:
            # 尝试在子目录找 run.py (防止 agent 也就是在子目录建了项目)
            found_runs = list(workspace.rglob("run.py"))
            if found_runs:
                sim_cwd = str(found_runs[0].parent)
                sim_entry = "run.py"
            else:
                status = "failed"
                error_msg = "Agent completed but 'run.py' was not found in workspace."

    output_info = {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": error_msg,
        "agent": "OpenHands", 
        "token_usage": run_result,
    }

    print(f"\n<<<GENERATION_RESULT>>>\n{json.dumps(output_info, indent=2)}\n<<<GENERATION_RESULT>>>")

if __name__ == "__main__":
    main()