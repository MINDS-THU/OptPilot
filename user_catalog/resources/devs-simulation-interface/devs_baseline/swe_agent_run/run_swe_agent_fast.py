import os
import sys
import json
import yaml  # 需要 pip install pyyaml
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# 📌 加载.env
load_dotenv(override=True)

import glob

def from_trajectory_file(traj_path: str) -> dict:
    """
    解析.traj JSON 文件。
    """
    # 尝试自动寻找：如果是目录，找里面最新的.traj
    if os.path.isdir(traj_path):
        files = glob.glob(os.path.join(traj_path, "**/*.traj"), recursive=True)
        if files:
            # 按修改时间排序，取最新的
            traj_path = max(files, key=os.path.getmtime)
        else:
            raise FileNotFoundError(f"在目录 {traj_path} 下未找到.traj 文件")
    else:
        raise FileNotFoundError(f"文件未找到: {traj_path}")

    try:
        with open(traj_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 数据结构兼容性处理
        # 某些版本直接在根节点，某些在 info 节点
        info = data['info']
        model_stats = info['model_stats']
        
        return {
            "input": model_stats['tokens_sent'], 
            "output": model_stats['tokens_received'],
            "thinking": 0, 
            "calls": model_stats['api_calls'],
        }
        
    except json.JSONDecodeError:
        raise ValueError(f"文件 {traj_path} 不是有效的 JSON 格式")
    except Exception as e:
        raise RuntimeError(f"解析 SWE-agent 轨迹文件失败: {e}")


def build_cli_prompt(requirement_text: str) -> str:
    """
    构建 SWE-agent 专用的 Prompt
    """
    return "\n".join([
        requirement_text,
        "Constraint: You MUST create a Python project.",
        "The entry point MUST be named 'run.py'.",
        "After finish all the tasks, save and submit. ", 
        "You should use tools of Discrete Event Simulation (DES) to simulate the system.",
        "For example, you can use the simpy library, it is installed by default.", 
        # "You can also use the xdevs library, it is installed by default, and the usage is avalailable at `/data/xdevs_usage`, and several example files in `/data/devs_usage/devs_project`.",
    ])

def setup_git_repo(workspace: Path):
    """
    SWE-agent 必须在一个 Git 仓库中运行。
    如果是空文件夹，我们需要初始化它。
    """
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
    
    # 检查是否已经是 git 仓库
    if (workspace / ".git").exists():
        return

    print(f"[Info] Initializing git repo at {workspace} to satisfy SWE-agent requirements...")
    try:
        # 初始化 git
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        
        # 创建一个占位文件
        (workspace / ".gitignore").write_text("*.pyc\n__pycache__/\n")
        
        # 设置 git 用户信息 (必须设置，否则 commit 会失败)
        subprocess.run(["git", "config", "user.email", "agent@bot.com"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SWE Agent"], cwd=workspace, check=True, capture_output=True)
        
        subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=workspace, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"[Error] Failed to initialize git repo: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="SWE-agent Automation Wrapper")
    # 1. 改动：使用 --config 替代 --requirement
    parser.add_argument("--config", required=True, help="Task YAML/JSON file with requirements")
    parser.add_argument("--workspace", required=True, help="Output workspace root")
    parser.add_argument("--model_id", default="gpt-4o", help="LLM model name (e.g., gpt-4o, claude-3-5-sonnet)")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    
    # === 读取任务配置文件 ===
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"[Error] Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[Info] Loading requirements from {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            # 兼容 JSON 和 YAML
            if config_path.suffix.lower() == ".json":
                task_params = json.load(f)
            else:
                task_params = yaml.safe_load(f)
        
        # 获取需求文本，如果为空则默认为空字符串
        requirement_text = str(task_params.get("requirements", ""))
        
        if not requirement_text.strip():
            print("[Warning] 'requirements' field is empty in the config file.", file=sys.stderr)

    except Exception as e:
        print(f"[Error] Failed to parse config file: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. 准备 Git 环境
    setup_git_repo(workspace)

    # 3. 构建 Prompt 并写入 problem.md 文件
    # SWE-agent 推荐将长 Prompt 写入文件，避免命令行转义问题
    prompt = build_cli_prompt(requirement_text)
    problem_file = workspace / "problem.md"
    problem_file.write_text(prompt, encoding="utf-8")

    instance_template = """\
<uploaded_files>
{{working_dir}}
</uploaded_files>
I've uploaded a python code repository in the directory {{working_dir}}. Consider the following description:

<pr_description>
{{problem_statement}}
</pr_description>

Goal: generate the code and files directly and then submit.
Do NOT run any scripts/tests. Do NOT attempt to reproduce issues.
After creating run.py and any required files, submit immediately.

Follow these steps to resolve the issue:
1. As a first step, it might be a good idea to find and read code relevant to the <pr_description> (if any)
2. Create & Edit the sourcecode of the repo to resolve the issue
3. Directly submit the code and files
Your thinking should be thorough and so it's fine if it's very long.
"""

    # 4. 构建 SWE-agent 命令
    docker_args = [
        "--add-host=host.docker.internal:host-gateway",
        "-e", "HTTP_PROXY=http://host.docker.internal:7890",
        "-e", "HTTPS_PROXY=http://host.docker.internal:7890",
        "-e", "NO_PROXY=localhost,127.0.0.1,::1",
    ]
    completion_args = {
        "drop_params": True
    }
    cmd = [
        "sweagent", "run",
        "--agent.model.name", args.model_id,
        "--env.repo.type", "local",
        "--env.repo.path", str(workspace),
        "--problem_statement.path", str(problem_file),
        "--actions.apply_patch_locally", "True",
        "--agent.model.per_instance_call_limit", "30",
        "--agent.model.per_instance_cost_limit", "0",
        "--agent.model.total_cost_limit", "0",
        "--agent.tools.enable_bash_tool", "false", 
        "--agent.templates.instance_template", instance_template,
        # "--agent.tools.parse_function.type", "thought_action",
        "--env.deployment.image", "python-xdevs-simpy",
        "--env.deployment.pull", "never",
        f"--env.deployment.docker_args={json.dumps(docker_args)}", 
        f"--agent.model.completion_kwargs={json.dumps(completion_args)}", 
    ]

    print(f"[Info] Starting SWE-agent task...")
    print(f"[Info] Model: {args.model_id}")
    print(f"[Info] Target: {workspace}")
    print(f"[Info] Command: {' '.join(cmd)}")
    print("-" * 40)

    start_time = datetime.now()
    status = "failed"
    error_msg = ""

    try:
        # === 使用 Popen 实现实时流式输出 ===
        process = subprocess.Popen(
            cmd,
            env=os.environ,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # 合并 stderr 到 stdout
            text=True,
            bufsize=1
        )

        # 实时打印 CLI 输出
        if process.stdout:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()

        return_code = process.wait()

        if return_code == 0:
            status = "success"
        else:
            status = "failed"
            error_msg = f"SWE-agent exited with code {return_code}"

    except Exception as e:
        error_msg = str(e)
        status = "error"

    # 5. 验证结果
    run_py_path = workspace / "run.py"
    if status == "success" and not run_py_path.exists():
        status = "failed"
        error_msg = "Agent finished but 'run.py' was not generated in the workspace."

    token_usage = {
        args.model_id: from_trajectory_file(str(workspace))
    }

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # 6. 生成标准输出格式
    output_info = {
        "status": status,
        "sim_cwd": str(run_py_path.parent),
        "sim_entry": str(run_py_path.name),
        "duration": duration,
        "error": error_msg,
        "agent": "swe-agent", 
        "token_usage": token_usage,
    }

    print(f"\n<<<GENERATION_RESULT>>>\n{json.dumps(output_info, indent=2)}\n<<<GENERATION_RESULT>>>")


if __name__ == "__main__":
    main()