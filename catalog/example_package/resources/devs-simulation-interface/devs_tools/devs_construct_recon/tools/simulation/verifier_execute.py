from smolagents import Tool
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
import json
from typing import List, Dict, Optional, Union, Tuple
from dataclasses import dataclass

# ==========================================
# 1. 通用 Python 脚本执行器 (Generic Executor)
# ==========================================

@dataclass
class ExecutionResult:
    return_code: int
    stdout: str
    stderr: str
    error_message: Optional[str] = None

class PythonScriptExecutor:
    """
    一个通用的 Python 脚本隔离执行器。
    功能：
    1. 将指定文件复制到临时目录（支持重命名）。
    2. 支持向 Stdin 注入文本。
    3. 执行指定的 Python 脚本。
    4. 将 Stdout/Stderr 保存到指定路径。
    5. 返回执行结果。
    """
    def __init__(self, working_directory: str = "./working_dir"):
        self.working_directory = Path(working_directory).resolve()

    def execute(self, 
                script_path: str, 
                files_to_copy: List[Dict[str, str]], 
                stdin_content: Optional[str] = None,
                stdout_save_path: Optional[str] = None,
                stderr_save_path: Optional[str] = None,
                timeout: int = 30) -> ExecutionResult:
        """
        Args:
            script_path: 要运行的主脚本路径（相对于 working_directory）。
            files_to_copy: 需要复制的文件列表。格式示例：
                           [{"src": "data/input.txt", "dest": "input.txt"}, 
                            {"src": "utils/helper.py", "dest": None}] # None 表示保留原名
            stdin_content: 希望注入到标准输入的文本字符串。
            stdout_save_path: 执行后的 stdout 保存路径（相对于 working_directory）。如果不传则不保存文件。
            stderr_save_path: 执行后的 stderr 保存路径（相对于 working_directory）。如果不传则不保存文件。
            timeout: 超时时间（秒）。
        """
        
        # 1. 基础路径解析与检查
        try:
            full_script_path = (self.working_directory / script_path).resolve()
            if not full_script_path.exists():
                return ExecutionResult(-1, "", "", f"Script file not found: {script_path}")
        except Exception as e:
            return ExecutionResult(-1, "", "", f"Path resolution error: {str(e)}")

        # 2. 创建临时环境并执行
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            
            try:
                # --- A. 准备文件 (Files Copying) ---
                # 总是先复制主脚本过去，确保它在当前执行上下文中
                target_script_name = full_script_path.name
                shutil.copy2(full_script_path, temp_dir_path / target_script_name)

                # 复制其他依赖文件
                for item in files_to_copy:
                    src_rel = item["src"]
                    dest_name = item["dest"] # 如果为None，则保留原名
                    
                    src_full = (self.working_directory / src_rel).resolve()
                    if not src_full.exists():
                        return ExecutionResult(-1, "", "", f"Input file not found: {src_rel}")
                    
                    if not str(src_full).startswith(str(self.working_directory)):
                        return ExecutionResult(-1, "", "", f"Input file {src_rel} is outside working directory.")

                    final_dest_name = dest_name if dest_name else src_full.name
                    # 确保目标目录结构存在（如果dest包含子目录）
                    dest_full_path = temp_dir_path / final_dest_name
                    dest_full_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    shutil.copy2(src_full, dest_full_path)

                # --- B. 执行脚本 (Execution) ---
                cmd = [sys.executable, target_script_name]
                
                result = subprocess.run(
                    cmd,
                    cwd=str(temp_dir_path),
                    input=stdin_content, # 注入 Stdin
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                
                stdout_str = result.stdout
                stderr_str = result.stderr
                
                # --- C. 保存输出 (Save Output) ---
                if stdout_save_path:
                    out_path = (self.working_directory / stdout_save_path).resolve()
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(stdout_str)
                        
                if stderr_save_path:
                    err_path = (self.working_directory / stderr_save_path).resolve()
                    err_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(err_path, 'w', encoding='utf-8') as f:
                        f.write(stderr_str)

                return ExecutionResult(
                    return_code=result.returncode,
                    stdout=stdout_str,
                    stderr=stderr_str
                )

            except subprocess.TimeoutExpired:
                return ExecutionResult(-1, "", "", f"Execution timed out after {timeout}s.")
            except Exception as e:
                return ExecutionResult(-1, "", "", f"Internal execution error: {str(e)}")


# ==========================================
# 2. Wrapper Tool: DEVSLogValidator
# ==========================================

class DEVSLogValidator(Tool):
    name = "devs_log_validator"
    description = (
        "Validates the execution results by running a specific Python validation script against "
        "the stdout and stderr output files generated by a previous execution. "
        "The validator is copied to a temporary environment and executed securely, the stdout_file and stderr_file are copied to the validator's working directory, with the names 'stdout.txt' and 'stderr.txt'. "
        "It uses a secure executor to run the validation in a temporary environment. "
        "Returns a JSON string indicating pass/fail status."
    )
    inputs = {
        "validator_file_path": {
            "type": "string",
            "description": "Path to the python validation script (relative to working_dir)."
        },
        "stdout_file_path": {
            "type": "string",
            "description": "Path to the stdout file generated by the execution (relative to working_dir)."
        },
        "stderr_file_path": {
            "type": "string",
            "description": "Path to the stderr file generated by the execution (relative to working_dir)."
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout for the validation script in seconds (default: 30).",
            "nullable": True
        },
        "stdout_name_in_docker": {
            "type": "string",
            "description": "Name of the stdout file in the docker container (default: stdout.txt).",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, working_directory: str = "./working_dir"):
        super().__init__()
        self.working_directory = working_directory
        # 实例化通用执行器
        self.executor = PythonScriptExecutor(working_directory=working_directory)
    
    def forward(self, validator_file_path: str, stdout_file_path: str, stderr_file_path: str, timeout: int = 30, stdout_name_in_docker: str = "stdout.txt") -> str:
        if timeout is None: timeout = 30
        
        # 1. 构建文件映射 (Configuration)
        # 验证器脚本通常假设它在当前目录下读取 'stdout.txt' 和 'stderr.txt'
        # 所以我们将传入的日志文件映射为这两个固定名称
        files_map = [
            {
                "src": stdout_file_path,
                "dest": stdout_name_in_docker
            },
            {
                "src": stderr_file_path,
                "dest": "stderr.txt"
            }
        ]

        # 2. 调用通用执行器
        # 注意：这里我们不需要保存校验脚本本身的输出到文件，只需要获取返回字符串即可，
        # 所以 save_stdout_to 和 save_stderr_to 设为 None。
        # 也不需要注入 stdin，设为 None。
        result = self.executor.execute(
            script_path=validator_file_path,
            files_to_copy=files_map,
            stdin_content=None,
            stdout_save_path=None, 
            stderr_save_path=None,
            timeout=timeout
        )

        # 3. 处理结果并保持原有接口格式
        if result.error_message:
            # 执行器层面报错（如文件找不到、超时等）
            return json.dumps({
                "passed": False,
                "message": result.error_message,
                "detail": "Executor failed before running validation logic."
            })

        if result.return_code == 0:
            # 脚本执行成功 (Exit Code 0) -> 视为通过
            return json.dumps({
                "passed": True,
                "message": "Validation passed successfully.",
                "scripte_output": result.stdout.strip()
            })
        else:
            # 脚本执行失败 (Exit Code != 0) -> 视为不通过
            # 优先取 stderr，如果为空则取 stdout
            error_msg = result.stderr.strip()
            if not error_msg:
                error_msg = result.stdout.strip()

            return json.dumps({
                "passed": False,
                "message": "Validation script failed.",
                "detail": error_msg[-1000:] # 防止过长
            })