import os
import json
import argparse
from pathlib import Path
from dotenv import load_dotenv
from smolagents import LiteLLMModel, CodeAgent, ToolCallingAgent, Tool
from devs_display.backend.server import DEFAULT_REGISTRY_PATH, run_devs_display_backend
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
from devs_tools.devs_construct_recon.constructor import DEVSConstructRecon
from devs_tools.devs_construct_recon.tools.simulation.devs_execute import DEVSExecute
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
    agent_planning_interval=10,
    agent_max_steps=80,
    manager_use_strong=False,
    agent_log_level="DEBUG",
    concur_num=4,
    construct_variant="recon",
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

    construct_variants = {"recon": DEVSConstructRecon}
    try:
        construct_cls = construct_variants[construct_variant]
    except KeyError as exc:
        supported = ", ".join(sorted(construct_variants))
        raise ValueError(
            f"Unsupported construct_variant '{construct_variant}'. Supported variants: {supported}"
        ) from exc

    construct_file_tools = {
        "read": SeeTextFile(working_directory),
        "write": SmartReplace(working_directory),
        "list": ListDir(working_directory),
    }
    effective_concur_num = concur_num if concur else 1
    print(
        f"construct_variant = {construct_variant}, "
        f"construct_cls = {construct_cls.__name__}, "
        f"concur_num = {effective_concur_num}"
    )

    devs_tree_construct_tool = construct_cls(
        file_tools=construct_file_tools,
        model_id=model_id,
        working_directory=working_directory,
        disable_check=disable_check,
        concur_num=effective_concur_num,
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

def _find_existing_display_workspace(base_temp_dir: str) -> str | None:
    registry_path = Path(DEFAULT_REGISTRY_PATH)
    if registry_path.exists():
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
            sessions = [
                entry for entry in registry.get("sessions", [])
                if entry.get("workspace_path") and Path(entry["workspace_path"]).is_dir()
            ]
            sessions.sort(key=lambda entry: entry.get("updated_at") or entry.get("last_seen_at") or entry.get("created_at") or "", reverse=True)
            if sessions:
                return sessions[0]["workspace_path"]
        except Exception as exc:
            print(f"[devs_display] Failed to read session registry: {exc}")
    return None


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
        default="server",
        choices=["server", "cli"],
        help="Run the backend API server for the graphical interface, or use the agent from the CLI.",
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
        help="Directory for backend logs and session-level runtime files.",
    )
    argparser.add_argument(
        "--index_dir",
        type=str,
        default=None,
        help="The directory where the vector store index will be stored.",
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
        default=10,
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
        default="recon",
        choices=["recon"],
        help="DEVS constructor implementation to use.",
    )
    args = argparser.parse_args()

    # Ensure the base temp_files directory exists
    base_temp_dir = "devs_app/working_dirs"
    Path(base_temp_dir).mkdir(parents=True, exist_ok=True)

    # Server mode owns workspace/session registry selection. If the caller does
    # not pass a workspace, reuse the latest registered session workspace.
    if args.working_directory is None:
        existing_workspace = _find_existing_display_workspace(base_temp_dir) if args.mode == "server" else None
        if existing_workspace:
            args.working_directory = existing_workspace
            print(f"[devs_display] Reusing existing workspace: {args.working_directory}")
        else:
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

    elif args.mode == "server":
        print("Launching API Server...")
        def devs_display_agent_factory(workspace: str):
            workspace_signature = f"{date_time_signature}_{Path(workspace).name}"
            return create_devs_agent(
                model_id={
                    "weak": args.model_id,
                    "strong": args.model_id_strong,
                },
                working_directory=workspace,
                persistent_storage=args.persistent_storage,
                index_dir=args.index_dir,
                signature=workspace_signature,
                disable_check=args.disable_check,
                concur=args.concur_generate,
                agent_planning_interval=args.agent_planning_interval,
                agent_max_steps=args.agent_max_steps,
                manager_use_strong=args.manager_use_strong,
                agent_log_level=args.agent_log_level,
                concur_num=args.concur_num,
                construct_variant=args.construct_variant,
            )

        run_devs_display_backend(
            manager_agent=manager_agent,
            working_directory=args.working_directory,
            agent_factory=devs_display_agent_factory,
        )
