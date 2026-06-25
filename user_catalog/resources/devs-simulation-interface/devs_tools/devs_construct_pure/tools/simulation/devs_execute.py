from smolagents import Tool
import sys
import subprocess
import tempfile
import time
import shutil
from pathlib import Path
import re
import json
import os
import shlex  # 新增引用，用于解析命令行参数字符串
from typing import Optional, List

PROJECT_FOLDER_NAME = "devs_project"
UTILS_DIR = os.path.join(Path(os.path.dirname(__file__)).parent.parent, "materials", "devs_project", "devs_utils")

class DEVSExecute(Tool):
    name = "devs_execute"
    description = (
        "Execute a DEVS model file or project within a controlled temporary environment. "
        "It supports execution of single Python scripts or complex projects involving multiple files. "
        "It will copy the project into a temporary directory, along with the devs_utils, and execute the specified script using module mode. "
        "The tool captures stdout/stderr, manages timeouts, and provides a basic sandbox to restrict imports to allowed libraries only. "
        "The path provided is relative to the agent's working directory. For example, 'devs_models/model.py'"
    )
    inputs = {
        "project_path": {
            "type": "string", 
            "description": "Path to the DEVS project directory relative to the working directory (e.g. simulations/HospitalSimu)."
        },
        "main_file": {
            "type": "string", 
            "description": "If input is a directory, specify the entry point file name (default: main.py). Ignored if input is a file. Relative to the project directory.", 
            "nullable": True
        },
        "timeout": {
            "type": "integer", 
            "description": "Maximum execution time in seconds (default: 30).", 
            "nullable": True
        },
        "command_args": {
            "type": "string",
            "description": "Command line arguments to pass to the script, as a single string (e.g., '--epochs 10 --lr 0.01').",
            "nullable": True
        },
        "stdout_file": {
            "type": "string", 
            "description": "Path (relative to working_dir) to save the raw standard output (STDOUT). (e.g. 'simulations/HospitalSimu/stdout.txt')", 
            "nullable": True
        },
        "stderr_file": {
            "type": "string",
            "description": "Path (relative to working_dir) to save the raw standard error (STDERR). Useful for debugging. (e.g. 'simulations/HospitalSimu/stderr.txt')",
            "nullable": True
        },
        "allowed_libraries": {
            "type": "string", 
            "description": "Comma-separated list of allowed root packages (default: numpy,xdevs,logging,math,random,time,collections,itertools).", 
            "nullable": True
        },
        "stdin_content": {
            "type": "string", 
            "description": "Content to be passed to the script via standard input (STDIN).", 
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, working_directory: str = "./working_dir"):
        super().__init__()
        # 保存工作目录的绝对路径，作为所有文件操作的基准根目录
        self.working_directory = working_directory
        self.working_dir_path = Path(self.working_directory).resolve()
        # 确保该目录存在
        self.working_dir_path.mkdir(parents=True, exist_ok=True)

    def forward(self, project_path: str, timeout: int = 30, 
                command_args: Optional[str] = None,
                stdout_file: Optional[str] = None,
                stderr_file: Optional[str] = None,
                allowed_libraries: str = "numpy,xdevs,logging,math,random,time,collections,itertools", 
                main_file: str = "main.py",
                stdin_content: Optional[str] = None) -> str:
        
        print(f"Starting DEVSExecute with file_or_project_path: {project_path}, timeout: {timeout}, command_args: {command_args}, stdout_file: {stdout_file}, stderr_file: {stderr_file}, allowed_libraries: {allowed_libraries}, main_file: {main_file}")
        # 1. 默认值处理
        if timeout is None: timeout = 30
        allowed_libs = [lib.strip() for lib in allowed_libraries.split(",") if lib.strip()]
        
        # 2. 关键修正：路径解析逻辑
        # 将输入的相对路径与 working_directory 拼接，而不是依赖系统 CWD
        try:
            target_path = (self.working_dir_path / project_path).resolve()
            
            # 容错处理：如果 Agent 忘记了 devs_models/ 前缀，或者是创建工具默认放到了子文件夹
            if not target_path.exists():
                potential_path = (self.working_dir_path / "devs_models" / project_path).resolve()
                if potential_path.exists():
                    target_path = potential_path
                else:
                    # 如果都找不到，返回包含工作目录路径的详细错误，帮助 Agent 自检
                    return f"STATUS: FAILED\nReason: File or directory '{project_path}' not found in working directory '{self.working_dir_path}'."

            # 安全检查：防止路径穿越
            if not str(target_path).startswith(str(self.working_dir_path)):
                return f"STATUS: FAILED\nReason: Access denied. Path '{project_path}' is outside the working directory."

        except Exception as e:
            return f"STATUS: FAILED\nReason: Error resolving path: {e}"

        # 3. 执行逻辑 (在临时目录中运行，不污染工作目录)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            
            # 准备执行环境
            target_main_file = None
            
            try:
                if target_path.is_file():
                    # Case A: 单文件
                    # 将文件复制到临时目录根部
                    dest_file = temp_dir_path / PROJECT_FOLDER_NAME / target_path.name
                    dest_file.parent.mkdir(parents=True, exist_ok=True) # Ensure parent dir exists
                    shutil.copy2(target_path, dest_file)
                    target_main_file = target_path.name
                    
                    # 额外优化：如果同一目录下有其他 .py 文件（依赖项），尝试一起复制
                    parent_dir = target_path.parent
                    for sibling in parent_dir.glob("*.py"):
                        if sibling.name != target_path.name:
                            shutil.copy2(sibling, temp_dir_path / PROJECT_FOLDER_NAME / sibling.name)

                elif target_path.is_dir():
                    # Case B: 项目目录
                    # 复制整个目录结构
                    shutil.copytree(target_path, temp_dir_path / PROJECT_FOLDER_NAME, dirs_exist_ok=True)
                    target_main_file = main_file
                    if not (temp_dir_path / PROJECT_FOLDER_NAME / target_main_file).exists():
                         return f"STATUS: FAILED\nReason: Main file '{main_file}' not found in project directory '{project_path}'."
                else:
                    return "STATUS: FAILED\nReason: Path must be a file or directory."
                
                # 还要把 ./devs_construct_tree_chain_record/materials/devs_project/devs_utils 复制到临时目录中
                try:
                    if os.path.exists(UTILS_DIR):
                        shutil.copytree(UTILS_DIR, temp_dir_path / PROJECT_FOLDER_NAME / "devs_utils", dirs_exist_ok=True)
                except Exception:
                    # 如果找不到 utils 目录，可能是在非标准环境，暂时忽略，依赖用户提供的代码自洽
                    pass
        
            except Exception as e:
                return f"STATUS: FAILED\nReason: Error copying files to execution environment: {e}"

            # 4. 创建启动器脚本 (Sandboxing Layer)
            # 修改为模块执行模式: python -m devs_project.target_file
            
            # 确保包根目录下有 __init__.py
            init_file = temp_dir_path / PROJECT_FOLDER_NAME / "__init__.py"
            if not init_file.exists():
                init_file.touch()

            # 构建模块名称
            # target_main_file 是相对于 PROJECT_FOLDER_NAME 的路径 (例如 "model.py" 或 "sub/main.py")
            # 我们需要把它转换为点号分隔的模块路径
            rel_path = Path(target_main_file).with_suffix("").as_posix()
            module_path = rel_path.replace("/", ".")
            target_module_name = f"{PROJECT_FOLDER_NAME}.{module_path}"
            
            # 将 launcher 放在 temp_dir 根目录下 (PROJECT_FOLDER_NAME 的上一级)
            launcher_path = self._create_launcher_script(temp_dir_path, target_module_name, allowed_libs)

            # 5. 运行子进程
            start_time = time.time()
            execution_time = 0.0
            success = False
            stdout, stderr = "", ""

            # 构建基础命令
            cmd = [sys.executable, "-u", str(launcher_path)]
            # 如果有额外参数，解析后追加
            if command_args:
                try:
                    args_list = shlex.split(command_args)
                    cmd.extend(args_list)
                except Exception as e:
                    return f"STATUS: FAILED\nReason: Error parsing command_args: {e}"
            
            try:
                # CWD 设置为 temp_dir_path，这样 Python 解释器可以将 PROJECT_FOLDER_NAME 视为一个包
                result = subprocess.run(
                    cmd,
                    cwd=str(temp_dir_path),
                    capture_output=True,
                    text=True,
                    input=stdin_content,
                    timeout=timeout
                )
                execution_time = time.time() - start_time
                success = result.returncode == 0
                stdout = result.stdout
                stderr = result.stderr
            except subprocess.TimeoutExpired as e:
                execution_time = timeout
                success = False
                # 2. 从异常对象中抢救已有的输出
                # 注意：e.stdout 和 e.stderr 可能为 None（虽然设置了 capture_output=True 通常会有值，但为了安全建议判空）
                stdout = str(e.stdout) if e.stdout else ""
                stderr = str(e.stderr) if e.stderr else ""
                
                # 3. 将超时信息追加到 stderr 后面，而不是覆盖它
                stderr += f"\n\n[SYSTEM ERROR] Execution timed out after {timeout} seconds."
            except Exception as e:
                execution_time = time.time() - start_time
                success = False
                stderr = f"Subprocess internal error: {str(e)}"

            # 6. 处理日志和结果
            
            # 保存标准输出 (STDOUT)
            if stdout_file:
                self._save_log(stdout_file, stdout)

            # 保存标准错误 (STDERR)
            if stderr_file:
                self._save_log(stderr_file, stderr)

            # 提取关键结果 (使用更新后的逻辑)
            key_results = self._extract_key_results(stdout, stderr, success, execution_time)
            
            # 构建方便解析的统一响应格式
            status_str = "SUCCESS" if success else "FAILED"
            response = f"STATUS: {status_str}\n"
            response += f"TARGET: {target_path.name}, {main_file}\n"
            response += f"TIME: {execution_time:.2f}s\n"
            
            if stdout_file:
                response += f"STDOUT_FILE: {stdout_file}\n"
            if stderr_file:
                response += f"STDERR_FILE: {stderr_file}\n"
            
            # if key_results:
            #     response += "\n--- EXTRACTED RESULTS ---\n" + "\n".join([f"- {r}" for r in key_results])
            
            if not success:
                # 如果失败了，除了提取的结果，还要把原始的 stderr 附上
                response += f"\n\n--- ERROR TRACE (Last 1000 chars) ---\n{stderr.strip()[-1000:]}"
                
            print(f" DEVS model execution success, response: {response}")
            
            return response

    def _create_launcher_script(self, temp_dir: Path, module_name: str, allowed_libs: List[str]) -> Path:
        """Creates a 'safe_launcher.py' that sets up the import hook and runs the user code as a module."""
        launcher_content = """
import sys
import builtins
import runpy
import os
import traceback

# 简单的环境设置
target_module = "{module_name}"

# 将当前工作目录显式加入 sys.path
sys.path.insert(0, os.getcwd())

try:
    # 使用 run_module 替代 run_path，实现类似 python -m project.module 的效果
    # 这允许代码中使用相对导入 (e.g. from . import utils)
    # 注意：Runpy 会保留 sys.argv，所以外部传入的参数可以被 target_module 读取
    runpy.run_module(target_module, run_name="__main__", alter_sys=True)
except Exception as e:
    traceback.print_exc()
    sys.exit(1)
""".format(allowed_libs=allowed_libs, module_name=module_name)

        launcher_path = temp_dir / "safe_launcher.py"
        with open(launcher_path, "w", encoding='utf-8') as f:
            f.write(launcher_content)
        return launcher_path

    def _extract_key_results(self, stdout: str, stderr: str, success: bool, execution_time: float) -> List[str]:
        """
        Modified extraction logic to handle structured JSON logs.
        Format example: 
        {"_log_type": "RESULT", "_level": "INFO", ..., }
        """
        results = []
        
        # 将 stdout 按行分割
        lines = stdout.splitlines()
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            try:
                # 尝试解析每一行为 JSON
                log_entry = json.loads(line)
                
                # 获取消息类型
                msg_type = log_entry.get("type", "").upper()
                data = log_entry
                
                # 策略：只提取 RESULT 和 ERROR 类型，或者是明确的异常
                if msg_type == "RESULT":
                    # 格式化输出：{data_content}
                    # 将 data 字典转为紧凑的字符串显示
                    data_str = json.dumps(data, ensure_ascii=False)
                    results.append(f"{data_str}")
                
                elif msg_type == "ERROR":
                    results.append(f"[LOG ERROR] {json.dumps(data, ensure_ascii=False)}")
                
                # 如果类型是 PROCESS，暂时忽略，避免刷屏，除非用户有特殊需求
                # elif msg_type == "PROCESS":
                #     pass 

            except json.JSONDecodeError:
                # 如果不是 JSON，检查是否是 Python 的 Traceback 或其他重要错误文本
                # 这里做简单的关键字匹配，不直接报错
                lower_line = line.lower()
                if "error" in lower_line or "traceback" in lower_line or "exception" in lower_line:
                    # 截取过长的错误行
                    display_line = line[:200] + "..." if len(line) > 200 else line
                    results.append(f"[RAW OUTPUT ERROR] {display_line}")
                continue

        # 如果没有提取到任何 JSON 结果，但程序运行成功且有输出，尝试提取最后一行作为兜底
        if not results and success and len(lines) > 0:
            last_line = lines[-1]
            # 只有当最后一行看起来不像是在重复之前已知的无用信息时才添加
            if "process" not in last_line.lower(): 
                 results.append(f"[Last Output] {last_line[:200]}")

        return results


    def _save_log(self, log_file: str, content: str):
        try:
            # 强制日志保存在 working_directory 下
            target_path = (self.working_dir_path / log_file).resolve()
            
            # 安全检查
            if not str(target_path).startswith(str(self.working_dir_path)):
                print(f"Warning: Log file path '{log_file}' attempts to write outside working directory. Ignored.")
                return

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            print(f"Failed to write log file: {e}")