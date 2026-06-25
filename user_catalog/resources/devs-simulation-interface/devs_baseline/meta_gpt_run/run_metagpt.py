import os
import sys
import json
import yaml
import asyncio
import argparse
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

class GlobalConfigOverride:
    """
    上下文管理器：
    1. 临时覆盖 ~/.metagpt/config2.yaml。
    2. 创建临时工作目录并将 CWD 切换至该处进行生成。
    3. 退出时将生成结果同步回原目标目录，并恢复 CWD 和配置。
    """
    def __init__(self, args):
        self.args = args
        self.original_cwd = os.getcwd()
        self.final_workspace = Path(args.workspace).resolve()
        
        # 临时目录相关
        self.tmp_dir_obj = None
        self.tmp_workspace = None

        # 配置备份相关
        self.home_meta = Path.home() / ".metagpt" / "config2.yaml"
        self.backup_meta = self.home_meta.with_suffix(".yaml.bak")
        self.backup_exists = False

    def __enter__(self):
        # --- 1. 处理配置文件备份 ---
        if self.home_meta.exists():
            print(f"[Config] Backing up global config to {self.backup_meta}")
            shutil.copy2(self.home_meta, self.backup_meta)
            self.backup_exists = True

        # --- 2. 创建临时工作空间 ---
        # 使用 tempfile 创建一个干净的临时文件夹
        self.tmp_dir_obj = tempfile.TemporaryDirectory(prefix="metagpt_tmp_")
        self.tmp_workspace = Path(self.tmp_dir_obj.name).resolve()
        
        print(f"[Env] Temporary workspace created at: {self.tmp_workspace}")
        
        # 切换当前工作目录到临时路径
        os.chdir(self.tmp_workspace)
        
        # --- 3. 写入新的临时全局配置 ---
        openai_api_model = self.args.model_id or os.getenv("OPENAI_API_MODEL", "gpt-4-turbo")
        
        # 简单的模型分发逻辑
        config_data = self._build_config_dict(openai_api_model)

        self.home_meta.parent.mkdir(parents=True, exist_ok=True)
        with open(self.home_meta, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_data, f)
        
        print(f"[Config] Overwritten global config at {self.home_meta}")
        return self

    def _build_config_dict(self, model_id):
        if model_id.startswith("openrouter/"):
            return {
                "llm": {
                    "api_type": "openrouter",
                    "api_key": os.getenv("OPENROUTER_API_KEY", ""),
                    "base_url": os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
                    "model": model_id[len("openrouter/"):]
                }
            }
        elif model_id.startswith("nebius/"):
            return {
                "llm": {
                    "api_type": "openai",
                    "api_key": os.getenv("NEBIUS_API_KEY", ""),
                    "base_url": os.getenv("NEBIUS_API_BASE", "https://api.nebius.ai/v1"),
                    "model": model_id[len("nebius/"):]
                }
            }
        else:
            return {
                "llm": {
                    "api_type": "openai",
                    "api_key": os.getenv("OPENAI_API_KEY", ""),
                    "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    "model": model_id
                }
            }

    def __exit__(self, exc_type, exc_val, exc_tb):
        # --- 1. 将临时目录的内容复制回最终目标目录 ---
        try:
            if self.tmp_workspace and self.tmp_workspace.exists():
                print(f"[Env] Copying generated files from {self.tmp_workspace} to {self.final_workspace}")
                # 确保目标目录存在
                self.final_workspace.mkdir(parents=True, exist_ok=True)
                # 使用 copytree 的 dirs_exist_ok 参数（Python 3.8+）进行合并
                shutil.copytree(self.tmp_workspace, self.final_workspace, dirs_exist_ok=True)
        except Exception as e:
            print(f"🛑 Error during file synchronization: {e}")

        # --- 2. 恢复原始 CWD ---
        print(f"[Env] Restoring CWD to {self.original_cwd}")
        os.chdir(self.original_cwd)

        # --- 3. 清理临时目录 ---
        if self.tmp_dir_obj:
            self.tmp_dir_obj.cleanup()

        # --- 4. 恢复原始配置 ---
        if self.backup_exists:
            print(f"[Config] Restoring global config from backup")
            shutil.move(self.backup_meta, self.home_meta)
        else:
            print(f"[Config] Removing temporary global config")
            if self.home_meta.exists():
                self.home_meta.unlink()
                

def build_cli_prompt(requirement_text: str) -> str:
    return "\n".join([
        requirement_text,
        "Constraint: You MUST create a Python project.",
        "The entry point MUST be named 'run.py'.",
        "Generate the full project without any user interaction. Because the user is strictly forbidened from interacting with you. ",
    ])

async def run_task(args):
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. 读取需求
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"🛑 Error: Config file not found at {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        task_params = json.load(f) if config_path.suffix == ".json" else yaml.safe_load(f)
    
    raw_requirement = str(task_params.get("requirements", ""))
    enhanced_requirement = build_cli_prompt(raw_requirement)
    project_name = str(task_params.get("base_folder", "")) or f"project_{int(datetime.now().timestamp())}"

    # =========================================================================
    # 关键修改：先进入 Context Manager 覆写配置，再 Import MetaGPT
    # =========================================================================
    with GlobalConfigOverride(args) as glob_conf:
        print("[System] Config overwritten. Now importing MetaGPT...")
        
        # 📌 延迟导入：确保 import 时读取的是我们刚刚写入的新文件
        from metagpt.team import Team
        from metagpt.context import Context
        from metagpt.config2 import config  # 此时 config 会加载新写入的 config2.yaml
        from metagpt.roles import (
            Architect,
            DataAnalyst,
            Engineer2,
            ProductManager,
            TeamLeader,
        )
        
        # 初始化项目特定的配置 (主要用于生成目录结构)
        config.update_via_cli(
            project_path=glob_conf.tmp_workspace, 
            project_name=project_name, 
            inc=False, 
            reqa_file="", 
            max_auto_summarize_code=0
        )
        
        print(f"[MetaGPT] Initialized with model: {config.llm.model}")
        print(f"[MetaGPT] Workspace path: {config.workspace.path}")
        print(f"project_name: {project_name}")

        ctx = Context(config=config)
        company = Team(context=ctx)
        company.hire([
            TeamLeader(),
            ProductManager(),
            Architect(),
            Engineer2(),
            DataAnalyst(),
        ])
        
        company.invest(args.investment)
        
        # --- 下面是标准的运行逻辑 ---
        start_time = datetime.now()
        error_msg = ""
        status = "fail"
        
        try:
            await company.run(n_round=args.rounds, idea=enhanced_requirement)
            status = "success"
        except Exception as e:
            error_msg = str(e)
            print(f"🛑 运行异常: {e}")
            import traceback
            traceback.print_exc()

        duration = (datetime.now() - start_time).total_seconds()

        # Token 统计
        stats = company.cost_manager
        token_usage = {
            config.llm.model: {
                "input": stats.total_prompt_tokens,
                "output": stats.total_completion_tokens,
                "thinking": 0,
                "calls": 0 
            }
        }

    # 查找 run.py
    actual_project_root = workspace
    found_files = list(actual_project_root.rglob("run.py"))
    
    sim_cwd, sim_entry = str(actual_project_root), "run.py"
    
    if found_files:
        # 路径最短优先
        best_entry = min(found_files, key=lambda p: len(p.parts))
        sim_cwd, sim_entry = str(best_entry.parent), best_entry.name
        print(f"✅ Found entry point: {best_entry}")
    else:
        if status == "success":
            error_msg = "run.py not found in generated project"

    output_info = {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": error_msg,
        "agent": "metagpt-software-company-sdk", 
        "token_usage": token_usage,
        "project_name": project_name
    }

    print(f"\n<<<GENERATION_RESULT>>>\n{json.dumps(output_info, indent=4)}\n<<<GENERATION_RESULT>>>")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--investment", type=float, default=40.0)
    parser.add_argument("--model_id", default=None)
    args = parser.parse_args()

    asyncio.run(run_task(args))

if __name__ == "__main__":
    main()