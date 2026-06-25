import sys
import os
import json
import yaml
import argparse
from pathlib import Path
from dotenv import load_dotenv
from smolagents import LiteLLMModel, CodeAgent, ToolCallingAgent, Tool
from src.agent_conversation_ui import GradioUI
from devs_display.backend.server import run_devs_display_backend
from src.monitoring import AgentLogger, LogLevel
from datetime import datetime

import litellm

litellm.register_model(
    {
        "openai/qwen3.6-plus": {
            "litellm_provider": "openai",
            "mode": "chat",
            "max_input_tokens": 131072,
            "max_output_tokens": 131072,
            "max_tokens": 65536,
        },
        "openrouter/deepseek/deepseek-v3.2": {
            "litellm_provider": "openrouter",
            "mode": "chat",
            "max_input_tokens": 123840,
            "max_output_tokens": 123840,
            "max_tokens": 123840,
        },
        "openrouter/qwen/qwen3-coder": {
            "litellm_provider": "openrouter",
            "mode": "chat",
            "max_input_tokens": 200000,
            "max_output_tokens": 200000,
            "max_tokens": 200000,
        },
        "openrouter/z-ai/glm-4.7": {
            "litellm_provider": "openrouter",
            "mode": "chat",
            "max_input_tokens": 200000,
            "max_output_tokens": 128000,
            "max_tokens": 200000,
        },
    }
)

from default_tools.file_editing.file_editing_tools import (
    ListDir,
    SeeTextFile,
    ReadBinaryAsMarkdown,
    ModifyFile,
    SmartReplace,
    CreateFileWithContent,
)
from devs_tools.devs_construct_dyn.devs_construct_dyn import DEVSConstructTree
from devs_tools.devs_construct_dyn_fast.devs_construct_dyn import DEVSConstructTreeFast
from devs_tools.devs_construct_dyn_fast.devs_construct_dyn_fast import (
    DEVSConstructTreeFastConcur,
)
from devs_tools.devs_construct_pure_fast_plan.devs_construct_dyn_fast import (
    DEVSConstructTreeFastConcur as DEVSConstructTreeFastConcurFastPlan,
)
from devs_tools.devs_construct_recon.devs_construct_dyn_fast import (
    DEVSConstructTreeFastConcur as DEVSConstructRecon,
)
from devs_tools.devs_construct_dyn_fast_robust.devs_construct_dyn import (
    DEVSConstructTreeFast as DEVSConstructTreeFastRobust,
)
from devs_tools.devs_construct_dyn_fast_robust.devs_construct_dyn_fast import (
    DEVSConstructTreeFastConcur as DEVSConstructTreeFastConcurRobust,
)

# from devs_tools.devs_execute import DEVSExecute
from devs_tools.devs_construct_dyn.tools.simulation.devs_execute import DEVSExecute
import tempfile
import time

from collections import defaultdict

# Load environment variables
load_dotenv(override=True)


class TokenTracker:
    def __init__(self):
        # 结构: {model_name: {'input': 0, 'output': 0, 'thinking': 0, 'calls': 0, 'total': 0}}
        self.stats = defaultdict(
            lambda: {"input": 0, "output": 0, "thinking": 0, "calls": 0, "total": 0}
        )

    def track(self, kwargs, completion_response, start_time, end_time):
        """LiteLLM 成功回调函数"""
        try:
            # 1. 获取模型名称 (优先取 response 中的，如果没有则取调用参数中的)
            # 兼容对象属性访问 (response.model) 和字典访问 (response['model'])
            model_name = (
                getattr(completion_response, "model", None)
                or completion_response.get("model")
                or kwargs.get("model")
                or "unknown-model"
            )

            # 2. 获取 usage 对象
            # usage 可能是一个对象 (Pydantic) 也可能是一个字典
            if hasattr(completion_response, "usage"):
                usage = completion_response.usage
            else:
                usage = completion_response.get("usage", None)

            if not usage:
                return

            # 3. 提取标准 Token (兼容 对象.属性 和 字典.get)
            if hasattr(usage, "prompt_tokens"):
                input_tokens = getattr(usage, "prompt_tokens", 0)
                output_tokens = getattr(usage, "completion_tokens", 0)
                total_tokens = getattr(usage, "total_tokens", 0)
                # 获取 details 对象
                details = getattr(usage, "completion_tokens_details", None)
            else:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                # 获取 details 字典
                details = usage.get("completion_tokens_details", None)

            # 4. 提取 "思考/推理" Token (修复点)
            thinking_tokens = 0
            if details:
                if isinstance(details, dict):
                    # 如果是字典，用 get
                    thinking_tokens = details.get("reasoning_tokens", 0)
                else:
                    # 如果是 Wrapper 对象，用 getattr
                    thinking_tokens = getattr(details, "reasoning_tokens", 0)

            # 5. 累加数据
            self.stats[model_name]["input"] += input_tokens
            self.stats[model_name]["output"] += output_tokens
            self.stats[model_name]["thinking"] += thinking_tokens
            self.stats[model_name]["calls"] += 1
            self.stats[model_name]["total"] += total_tokens

        except Exception as e:
            # 打印错误但不中断程序，方便排查
            print(f"[TokenTracker Error] {str(e)}")

    def get_report(self):
        """返回最终需要的 dict 格式"""
        return dict(self.stats)

    def print_summary(self):
        """打印易读的统计信息"""
        print("\n" + "=" * 30)
        print("  TOKEN USAGE SUMMARY")
        print("=" * 30)
        for model, counts in self.stats.items():
            print(f"Model: {model}")
            print(f"  - Calls:    {counts['calls']}")
            print(f"  - Input:    {counts['input']}")
            print(f"  - Output:   {counts['output']}")
            if counts["thinking"] > 0:
                print(f"  - Thinking: {counts['thinking']} (Included in Output)")
            print(f"  - Total:    {counts['total']}")
            print("-" * 30)


# --- 初始化并注册回调 ---
token_tracker = TokenTracker()
litellm.success_callback = [token_tracker.track]


def create_devs_agent(
    model_id: dict,
    working_directory="working_dir",
    persistent_storage="persistent_storage",
    index_dir="index_dir",
    signature=None,
    disable_check=False,
    concur=False,
    agent_planning_interval=4,
    agent_max_steps=80,
    manager_use_strong=False,
    agent_log_level="DEBUG",
    concur_num=4,
    construct_variant="fast",
):
    ### Set up the model ###
    # here we use LiteLLMModel.
    # Alternatively, you can use InferenceClientModel, VLLMModel or TransformersModel depending on your chosen LLM model backend
    # Use a stronger manager model in checked/debug workflows for better tool orchestration.
    manager_model_id = (
        model_id["strong"]
        if (manager_use_strong or not disable_check)
        else model_id["weak"]
    )
    model = LiteLLMModel(model_id=manager_model_id)

    ### Set up the tools ###
    # tools for working with the local working directory
    working_directory_file_editing_tools = [
        ListDir(working_directory),
        SeeTextFile(working_directory),
        ReadBinaryAsMarkdown(working_directory),
        ModifyFile(working_directory),
        CreateFileWithContent(working_directory),
    ]

    devs_tools: list[Tool] = []

    print(f"disable_check = {disable_check}")

    use_robust_construct = construct_variant == "fast_robust"
    use_fast_plan = construct_variant == "fast_plan"
    use_recon = construct_variant == "recon"
    construct_cls_concur = (
        DEVSConstructTreeFastConcurRobust
        if use_robust_construct
        else (DEVSConstructTreeFastConcurFastPlan if use_fast_plan else DEVSConstructTreeFastConcur)
    )
    construct_cls_single = (
        DEVSConstructTreeFastRobust if use_robust_construct else (DEVSConstructTreeFastConcurFastPlan if use_fast_plan else DEVSConstructTreeFast)
    )
    construct_cls_single = (
        DEVSConstructTreeFastRobust if use_robust_construct else (DEVSConstructTreeFastConcurFastPlan if use_fast_plan else DEVSConstructTreeFast)
    )

    if not disable_check and concur:
        devs_tree_construct_tool = construct_cls_concur(
            file_tools={
                "read": SeeTextFile(working_directory),
                "write": SmartReplace(working_directory),
                "list": ListDir(working_directory),
            },
            model_id=model_id,
            working_directory=working_directory,
            disable_check=False,
            concur_num=4,
        )
    elif not disable_check:
        devs_tree_construct_tool = construct_cls_single(
            file_tools={
                "read": SeeTextFile(working_directory),
                "write": SmartReplace(working_directory),
                "list": ListDir(working_directory),
            },
            model_id=model_id,
            working_directory=working_directory,
            disable_check=False,
        )
    elif not concur:
        devs_tree_construct_tool = construct_cls_concur(
            file_tools={
                "read": SeeTextFile(working_directory),
                "write": SmartReplace(working_directory),
                "list": ListDir(working_directory),
            },
            model_id=model_id,
            working_directory=working_directory,
            disable_check=True,
            concur_num=1,
        )
    else:
        print(f"in concur")
        devs_tree_construct_tool = construct_cls_concur(
            file_tools={
                "read": SeeTextFile(working_directory),
                "write": SmartReplace(working_directory),
                "list": ListDir(working_directory),
            },
            model_id=model_id,
            working_directory=working_directory,
            disable_check=True,
            concur_num=concur_num,
        )
    devs_tools.append(devs_tree_construct_tool)

    devs_execute_tool = DEVSExecute(working_directory=working_directory)
    devs_tools.append(devs_execute_tool)

    ### Set up the agent ###
    app_name = "devs_app"
    level_map = {
        "DEBUG": LogLevel.DEBUG,
        "INFO": LogLevel.INFO,
        "WARNING": LogLevel.INFO,
        "ERROR": LogLevel.ERROR,
    }
    resolved_level = level_map.get(str(agent_log_level).upper(), LogLevel.DEBUG)
    # Here we configure the logger to save the agent's log to a txt file in the persistent storage
    mananger_logger = AgentLogger(
        level=resolved_level,
        save_to_file=os.path.join(
            persistent_storage, f"manager_agent_log_{signature}.txt"
        ),
        name=app_name,
    )
    # tools = working_directory_file_editing_tools+search_tools+knowledge_base_retrieval_tools+knowledge_base_update_tools+visual_qa_tools+devs_tools
    tools = working_directory_file_editing_tools + devs_tools
    # manager agent is responsible for directly talking with user and call sub-agents to complete user tasks
    manager_agent = CodeAgent(
        tools=tools,
        model=model,
        managed_agents=[],
        planning_interval=agent_planning_interval,
        additional_authorized_imports=["json", "re", "math", "typing", "pathlib"],
        max_steps=agent_max_steps,
        logger=mananger_logger,
        name=app_name,
        description="This is a DEVS agent application that can construct, execute, and analyze DEVS models using xDEVS.py.",
    )
    mananger_logger.visualize_agent_tree(manager_agent)
    return manager_agent


def _normalize_tool_params(tool_params):
    if not isinstance(tool_params, dict):
        raise TypeError("Tool parameters must be a dict")

    if not tool_params.get("base_folder"):
        tool_params["base_folder"] = "."

    tool_params.setdefault("skip_simulation_check", False)
    tool_params.setdefault("only_ensure_executable", False)

    requirements = tool_params.get("requirements")
    if isinstance(requirements, dict):
        sections = []
        for key in ("general", "scenario", "args_input_output"):
            value = requirements.get(key)
            if value:
                sections.append(f"{key}:\n{value}")
        tool_params["requirements"] = "\n\n".join(sections) if sections else str(requirements)
    elif requirements is not None and not isinstance(requirements, str):
        tool_params["requirements"] = str(requirements)

    return tool_params


if __name__ == "__main__":
    argparser = argparse.ArgumentParser(description="Run the DEVS Agent")
    argparser.add_argument(
        "--model_id",
        type=str,
        default="gpt-4.1",
        help="The ID of the model to use for the agent.",
    )
    argparser.add_argument(
        "--model_id_strong",
        type=str,
        default="gpt-5.2",
        help="The ID of the model to use for the agent.",
    )
    argparser.add_argument(
        "--mode",
        type=str,
        default="gradio",
        choices=["gradio", "cli", "server", "tool_debug", "tool_debug_agent"],
        help="The mode to run the agent in. 'gradio' for web interface, 'cli' for command line interface.",
    )
    argparser.add_argument(
        "--working_directory",
        type=str,
        default=None,
        help="The directory where the agent will store its working files.",
    )
    argparser.add_argument(
        "--persistent_storage",
        type=str,
        default=None,
        help="A structured directory that contains the persistent files, e.g. code snippets, papers, and other resources.",
    )
    argparser.add_argument(
        "--index_dir",
        type=str,
        default=None,
        help="The directory where the vector store index will be stored.",
    )
    argparser.add_argument(
        "--debug_args_file",
        type=str,
        default="devs_app/devs_model_inputs/example1.json",
        help="Path to the JSON file containing tool parameters for debugging.",
    )
    argparser.add_argument(
        "--target_tool",
        type=str,
        default="devs_construct_tree",
        help="The name of the tool you want to debug (must match tool.name).",
    )
    argparser.add_argument(
        "--disable_check",
        action="store_true",
        help="Disable the check",
    )
    argparser.add_argument(
        "--concur_generate",
        action="store_true",
    )
    argparser.add_argument(
        "--agent_planning_interval",
        type=int,
        default=4,
        help="Planning interval for manager CodeAgent.",
    )
    argparser.add_argument(
        "--agent_max_steps",
        type=int,
        default=80,
        help="Max reasoning steps for manager CodeAgent.",
    )
    argparser.add_argument(
        "--manager_use_strong",
        action="store_true",
        help="Force manager CodeAgent to use strong model for orchestration.",
    )
    argparser.add_argument(
        "--agent_log_level",
        type=str,
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity for manager agent runtime.",
    )
    argparser.add_argument(
        "--concur_num",
        type=int,
        default=4,
        help="Concurrency used by devs_construct_tree concurrent mode.",
    )
    argparser.add_argument(
        "--construct_variant",
        type=str,
        default="fast",
        choices=["fast", "fast_robust", "fast_plan"],
        help="Select constructor implementation variant.",
    )
    args = argparser.parse_args()

    # Ensure the base temp_files directory exists
    base_temp_dir = "devs_app/working_dirs"
    Path(base_temp_dir).mkdir(parents=True, exist_ok=True)

    # Set the save directory to a default if not provided
    if args.working_directory is None:
        curr_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.working_directory = tempfile.mkdtemp(
            dir=base_temp_dir, prefix=f"working_directory_{curr_time}_"
        )
    if args.persistent_storage is None:
        args.persistent_storage = "devs_app/persistent_storage"
    Path(args.persistent_storage).mkdir(parents=True, exist_ok=True)
    if args.index_dir is None:
        args.index_dir = "devs_app/index_dir"
    Path(args.index_dir).mkdir(parents=True, exist_ok=True)

    # create a date time signature
    date_time_signature = time.strftime("%Y%m%d_%H%M%S")

    # Create the agent
    manager_agent = create_devs_agent(
        model_id={
            "weak": args.model_id,
            "strong": args.model_id_strong,
        },
        working_directory=args.working_directory,
        persistent_storage=args.persistent_storage,
        index_dir=args.index_dir,
        signature=date_time_signature,
        disable_check=args.disable_check,
        concur=args.concur_generate,
        agent_planning_interval=args.agent_planning_interval,
        agent_max_steps=args.agent_max_steps,
        manager_use_strong=args.manager_use_strong,
        agent_log_level=args.agent_log_level,
        concur_num=args.concur_num,
        construct_variant=args.construct_variant,
    )

    if args.mode == "cli":
        # Run the agent in CLI mode
        while True:
            try:
                manager_agent.run(
                    "Based on the conversation so far, talk with the user to understand the user's task and complete the task.",
                    reset=False,
                )
                print("Agent finished running. Waiting for next command...")
                print("Press Ctrl+C to exit.")
            except KeyboardInterrupt:
                print("Exiting...")
                break

    elif args.mode == "gradio":
        # Run the agent in Gradio mode
        print("Launching Gradio UI...")
        GradioUI(agent=manager_agent, file_upload_folder=args.working_directory).launch(
            share=False
        )

    elif args.mode == "server":
        print("Launching API Server...")
        run_devs_display_backend(
            manager_agent=manager_agent, working_directory=args.working_directory
        )

    elif args.mode == "tool_debug":
        # === 适配器模式：将内部 Tool 包装为标准 Code Agent 行为 ===
        print(f"--- [Adapter] Starting Tool: {args.target_tool} ---")

        # 1. 准备参数
        param_file = Path(args.debug_args_file)
        if not param_file.exists():
            print(f"Error: Parameter file '{args.debug_args_file}' not found.")
            sys.exit(1)

        try:
            with open(param_file, "r", encoding="utf-8") as f:
                if param_file.suffix.lower() == ".json":
                    tool_params = json.load(f)
                elif param_file.suffix.lower() in [".yaml", ".yml"]:
                    tool_params = yaml.safe_load(f)
                else:
                    raise Exception(f"Unsupported file format: {param_file}")
        except Exception as e:
            print(f"Error: Failed to parse parameters: {e}")
            sys.exit(1)

        # 2. 确定“契约”路径
        # 评测器通过 CLI 传入 working_directory (沙盒根目录)
        # YAML 参数传入 base_folder (项目子目录)
        tool_params = _normalize_tool_params(tool_params)
        sandbox_root = Path(args.working_directory).resolve()
        base_folder_name = tool_params.get("base_folder", ".")  # 默认为当前目录

        # 计算出仿真模型实际应该存在的目录 (Simulation CWD)
        # e.g., /tmp/work_dir_123/abp_model
        sim_cwd = sandbox_root / base_folder_name

        # 约定的入口文件名
        entry_filename = "run.py"
        expected_entry_path = sim_cwd / entry_filename

        print(f"[Adapter] Sandbox Root: {sandbox_root}")
        print(f"[Adapter] Expected Project Root: {sim_cwd}")
        print(f"[Adapter] Expected Entry Point: {expected_entry_path}")

        # 3. 查找并执行工具
        target_tool = next(
            (
                tool
                for name, tool in manager_agent.tools.items()
                if name == args.target_tool
            ),
            None,
        )
        if not target_tool:
            print(f"Error: Tool '{args.target_tool}' not found.")
            sys.exit(1)

        try:
            start_time = time.time()
            # === 核心：调用工具生成代码 ===
            # 工具内部会使用 args.working_directory 作为基础，并创建 base_folder
            result = target_tool.forward(**tool_params)
            end_time = time.time()

            print(f"\n--- Tool Execution Finished ({end_time - start_time:.2f}s) ---")

            usage_report = token_tracker.get_report()
            # 也可以在这里直接打印到控制台看一眼
            token_tracker.print_summary()

            # 4. 验证契约 (Verify Contract)
            output_info = {
                "status": "fail",
                "sim_cwd": str(sim_cwd),
                "sim_entry": entry_filename,
                "timestamp": datetime.now().isoformat(),
                "token_usage": usage_report,
            }

            if not sim_cwd.exists():
                output_info["error"] = (
                    f"Base folder '{base_folder_name}' was not created."
                )
            elif not expected_entry_path.exists():
                output_info["error"] = (
                    f"Entry file '{entry_filename}' missing in project root."
                )
            else:
                output_info["status"] = "success"
                # 还可以把工具返回的文本摘要放进去
                output_info["tool_response_preview"] = str(result)[:100]

            # 5. 输出标准握手信号 (Handshake Signal)
            # 无论成功失败，都输出这段 JSON，供 Pipeline 解析
            print("\n<<<GENERATION_RESULT>>>")
            print(json.dumps(output_info, indent=None))
            print("<<<GENERATION_RESULT>>>")

            if output_info["status"] != "success":
                # 如果生成失败，Adapter 本身以非0退出，方便 Shell 脚本捕获
                sys.exit(1)

        except Exception as e:
            print("\n=== Execution Crashed ===")
            import traceback

            traceback.print_exc()

            error_info = {"status": "crash", "error": str(e)}
            print("\n<<<GENERATION_RESULT>>>")
            print(json.dumps(error_info))
            print("<<<GENERATION_RESULT>>>")
            sys.exit(1)

    elif args.mode == "tool_debug_agent":
        print(f"--- [Integrated Debug Agent] Starting Tool: {args.target_tool} ---")

        param_file = Path(args.debug_args_file)
        if not param_file.exists():
            print(f"Error: Parameter file '{args.debug_args_file}' not found.")
            sys.exit(1)

        try:
            with open(param_file, "r", encoding="utf-8") as f:
                if param_file.suffix.lower() == ".json":
                    tool_params = json.load(f)
                elif param_file.suffix.lower() in [".yaml", ".yml"]:
                    tool_params = yaml.safe_load(f)
                else:
                    raise Exception(f"Unsupported file format: {param_file}")
        except Exception as e:
            print(f"Error: Failed to parse parameters: {e}")
            sys.exit(1)

        tool_params = _normalize_tool_params(tool_params)
        sandbox_root = Path(args.working_directory).resolve()
        base_folder_name = str(tool_params.get("base_folder", "."))
        sim_cwd = sandbox_root / base_folder_name
        entry_filename = "run.py"
        expected_entry_path = sim_cwd / entry_filename
        smoke_stdout = f"{base_folder_name}/_debug/smoke.stdout"
        smoke_stderr = f"{base_folder_name}/_debug/smoke.stderr"

        debug_param_file = (
            sandbox_root / base_folder_name / "_debug" / "integrated_tool_params.json"
        )
        debug_param_file.parent.mkdir(parents=True, exist_ok=True)
        debug_param_file.write_text(
            json.dumps(tool_params, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        compact_prompt = (
            "Run an integrated generation-debug loop with tools only.\n"
            f"Step 1: read params from file '{base_folder_name}/_debug/integrated_tool_params.json', then call `{args.target_tool}` exactly once using those params.\n"
            "Step 2: smoke test with `devs_execute` using:\n"
            f"- project_path='{base_folder_name}'\n"
            "- main_file='run.py'\n"
            "- timeout=120\n"
            "- command_args='--simulate_time 3 --seed 0'\n"
            f"- stdout_file='{smoke_stdout}'\n"
            f"- stderr_file='{smoke_stderr}'\n"
            "Step 3: if smoke test fails, do minimal targeted fixes and rerun smoke test (max 2 repair loops).\n"
            "Step 4: finish with a short summary including final paths."
        )

        try:
            start_time = time.time()
            result = manager_agent.run(compact_prompt, reset=True)
            end_time = time.time()
            print(f"\n--- Integrated Agent Finished ({end_time - start_time:.2f}s) ---")

            usage_report = token_tracker.get_report()
            token_tracker.print_summary()

            output_info = {
                "status": "fail",
                "sim_cwd": str(sim_cwd),
                "sim_entry": entry_filename,
                "timestamp": datetime.now().isoformat(),
                "token_usage": usage_report,
                "smoke_stdout": str(sandbox_root / smoke_stdout),
                "smoke_stderr": str(sandbox_root / smoke_stderr),
            }

            if not sim_cwd.exists():
                output_info["error"] = (
                    f"Base folder '{base_folder_name}' was not created."
                )
            elif not expected_entry_path.exists():
                output_info["error"] = (
                    f"Entry file '{entry_filename}' missing in project root."
                )
            else:
                output_info["status"] = "success"
                output_info["tool_response_preview"] = str(result)[:200]

            print("\n<<<GENERATION_RESULT>>>")
            print(json.dumps(output_info, indent=None))
            print("<<<GENERATION_RESULT>>>")

            if output_info["status"] != "success":
                sys.exit(1)

        except Exception as e:
            print("\n=== Integrated Execution Crashed ===")
            import traceback

            traceback.print_exc()
            error_info = {"status": "crash", "error": str(e)}
            print("\n<<<GENERATION_RESULT>>>")
            print(json.dumps(error_info))
            print("<<<GENERATION_RESULT>>>")
            sys.exit(1)
