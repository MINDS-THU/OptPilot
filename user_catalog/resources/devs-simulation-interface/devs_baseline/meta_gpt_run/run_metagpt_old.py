import os
import sys
import json
import yaml
import argparse
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# 📌 加载 .env
load_dotenv(override=True)

def build_cli_prompt(requirement_text: str) -> str:
    """
    只负责拼接 prompt 的主体内容
    """
    return "\n".join([
        requirement_text,
        "Constraint: You MUST create a Python project.",
        "The entry point MUST be named 'run.py'."
    ])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Task YAML/JSON file with requirements")
    parser.add_argument("--workspace", required=True, help="Output workspace root")
    parser.add_argument("--rounds", type=int, default=100,
                        help="Number of MetaGPT iteration rounds")
    parser.add_argument("--investment", type=float, default=40.0,
                        help="MetaGPT investment budget")
    parser.add_argument("--model_id", default=None,
                        help="Optional LLM model override")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # === 临时覆盖全局 MetaGPT 配置 ===
    # 1) 原全局 config2.yaml 路径
    home_meta = Path.home() / ".metagpt" / "config2.yaml"
    backup_meta = home_meta.with_suffix(".yaml.bak")

    # 2) 备份现有全局配置（如果存在）
    if home_meta.exists():
        print(f"[info] Backing up existing global config to {backup_meta}")
        shutil.copy2(home_meta, backup_meta)

    # 3) 生成临时全局配置
    openai_api_model = args.model_id or os.getenv("OPENAI_API_MODEL", "")
    
    if openai_api_model.startswith("openrouter/"):
        openai_api_model = openai_api_model[len("openrouter/"):]
        openai_api_key = os.getenv("OPENROUTER_API_KEY", "")
        openai_api_base = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    else:
        openai_api_key = os.getenv("OPENAI_API_KEY", "")
        openai_api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

    new_global_config = {
        "llm": {
            "api_type": "openai",
            "api_key": openai_api_key,
            "base_url": openai_api_base,
            "model": openai_api_model,
        }
    }

    home_meta.parent.mkdir(parents=True, exist_ok=True)
    with open(home_meta, "w", encoding="utf-8") as f:
        yaml.safe_dump(new_global_config, f)

    print(f"[info] Written temporary global config to {home_meta}")

    try:
        # === 读取任务配置文件 ===
        config_path = Path(args.config).resolve()
        if not config_path.exists():
            print(f"[error] Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)

        with open(config_path, "r", encoding="utf-8") as f:
            task_params = (json.load(f) if config_path.suffix == ".json"
                           else yaml.safe_load(f))

        requirement_text = str(task_params.get("requirements", ""))

        # === 写入 prompt 文件 ===
        cli_prompt = build_cli_prompt(requirement_text)
        prompt_file = workspace / "metagpt_prompt.txt"
        prompt_file.write_text(cli_prompt)

        project_name = str(task_params.get("base_folder", "")) or f"project_{int(datetime.now().timestamp())}"

        cmd = [
            "metagpt",
            cli_prompt,
            "--project-name", project_name,
            "--n-round", str(args.rounds),
            "--investment", str(args.investment),
        ]

        print("🛠 Running MetaGPT CLI:\n", " ".join(cmd))

        process = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # 实时打印 CLI 输出
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()

        rc = process.wait()
        if rc != 0:
            print(f"\n🛑 MetaGPT CLI exited with code {rc}", file=sys.stderr)
            sys.exit(rc)

        project_dir = workspace
        # === 智能查找 run.py ===
        # rglob("run.py") 会递归查找该目录下所有文件夹
        found_files = list(project_dir.rglob("run.py"))

        output_info = {"status": "fail", "timestamp": datetime.now().isoformat()}

        if found_files:
            # 策略：如果有多个 run.py，优先选择路径层级最浅的（最靠近项目根目录的）
            # 这里的 len(p.parts) 越小，说明目录层级越少
            best_entry = min(found_files, key=lambda p: len(p.parts))
            
            output_info["status"] = "success"
            
            # 将工作目录 (sim_cwd) 设置为 run.py 实际所在的文件夹
            # 这样后续命令 "cd {sim_cwd} && python run.py" 才能正确执行
            output_info["sim_cwd"] = str(best_entry.parent)
            output_info["sim_entry"] = best_entry.name
            
            print(f"[info] 🎯 Detected entry point: {best_entry}")
        else:
            # (可选) 稍微扩大一点搜索范围，如果真的没有 run.py，看看有没有 main.py 提示用户
            alternatives = list(project_dir.rglob("main.py"))
            if alternatives:
                print(f"[warn] 'run.py' not found, but found 'main.py' at {alternatives[0]}. Compliance check failed.")
            
            output_info["error"] = "run.py not found in generated project"

        print(f"\n<<<GENERATION_RESULT>>>\n{json.dumps(output_info)}\n<<<GENERATION_RESULT>>>")

        if output_info["status"] != "success":
            sys.exit(1)

    finally:
        # === 恢复原全局配置 ===
        if backup_meta.exists():
            print(f"[info] Restoring global config from backup")
            shutil.move(backup_meta, home_meta)
        else:
            print(f"[info] Removing temporary global config")
            try:
                home_meta.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
