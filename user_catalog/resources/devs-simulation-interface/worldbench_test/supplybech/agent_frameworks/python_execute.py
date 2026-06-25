from smolagents import Tool
import sys
import subprocess
import shlex
import os
from pathlib import Path
from typing import Dict, List, Any, Optional

class RunPythonFile(Tool):
    """Execute a Python file in a specified working directory and return stdout/stderr."""
    name = "run_python"
    description = (
        "Execute a Python file and return its output. "
        "Use this to run algorithms or simulations you've written."
    )
    inputs = {
        "filename": {
            "type": "string",
            "description": "Filename to execute. If 'cwd' is specified, this should be relative to that cwd (e.g., 'run.py'). Otherwise, relative to the root workspace.",
        },
        "args": {
            "type": "string",
            "nullable": True,
            "description": "Command line arguments to pass to the script",
        },
        "cwd": {
            "type": "string",
            "nullable": True,
            "description": "Optional subdirectory to run the script in (e.g., 'supply_chain_model'). If provided, the script and output files are resolved relative to this directory.",
        },
        "timeout": {
            "type": "integer",
            "nullable": True,
            "description": "Maximum execution time in seconds (default: 60)",
        },
        "stdout_file": {
            "type": "string",
            "nullable": True,
            "description": "Optional filename to redirect standard output. Highly recommended if you expect long outputs. Resolved relative to 'cwd' if provided.",
        },
        "stderr_file": {
            "type": "string",
            "nullable": True,
            "description": "Optional filename to redirect standard error. Resolved relative to 'cwd' if provided.",
        },
    }
    output_type = "string"

    def __init__(self, working_directory: str = "./working_dir"):
        super().__init__()
        self.working_dir_path = Path(working_directory).resolve()
        self.working_dir_path.mkdir(parents=True, exist_ok=True)

    def forward(
        self, 
        filename: str, 
        args: Optional[str] = None, 
        cwd: Optional[str] = None,
        timeout: int = 60,
        stdout_file: Optional[str] = None,
        stderr_file: Optional[str] = None
    ) -> str:
        
        # 1. 确定实际的执行目录
        if cwd:
            exec_dir = (self.working_dir_path / cwd).resolve()
            # 安全检查：防止 LLM 传入 cwd="../../../etc" 之类的路径逃逸
            if not str(exec_dir).startswith(str(self.working_dir_path)):
                return f"ERROR: The requested cwd '{cwd}' is outside the allowed workspace '{self.working_dir_path}'."
            exec_dir.mkdir(parents=True, exist_ok=True)
        else:
            exec_dir = self.working_dir_path

        # 2. 解析脚本路径
        filepath = exec_dir / filename
        if not filepath.exists():
            return f"ERROR: File '{filename}' not found in directory '{exec_dir}'"
        
        cmd = [sys.executable, "-u", str(filepath)]
        if args:
            try:
                cmd.extend(shlex.split(args))
            except Exception:
                cmd.append(args)

        # 默认捕获输出到内存
        stdout_target = subprocess.PIPE
        stderr_target = subprocess.PIPE
        
        f_out = None
        f_err = None

        try:
            # 3. 解析重定向文件路径（相对于 exec_dir）
            if stdout_file:
                out_path = exec_dir / stdout_file
                os.makedirs(out_path.parent, exist_ok=True)
                f_out = open(out_path, "w", encoding="utf-8")
                stdout_target = f_out
                
            if stderr_file:
                err_path = exec_dir / stderr_file
                os.makedirs(err_path.parent, exist_ok=True)
                f_err = open(err_path, "w", encoding="utf-8")
                stderr_target = f_err

            # 执行命令
            result = subprocess.run(
                cmd,
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                timeout=timeout,
                cwd=str(exec_dir),
            )

            # 构建返回给 LLM 的摘要信息
            output_msgs = []
            
            # 告诉 LLM 具体的相对路径，方便它用 read_file 工具去读取
            out_relative_path = os.path.join(cwd or "", stdout_file) if stdout_file else ""
            err_relative_path = os.path.join(cwd or "", stderr_file) if stderr_file else ""

            if stdout_file:
                output_msgs.append(f"[INFO] STDOUT successfully redirected to '{out_relative_path}'.")
            elif result.stdout:
                output_msgs.append(f"=== STDOUT ===\n{result.stdout.strip()}")
                
            if stderr_file:
                output_msgs.append(f"[INFO] STDERR successfully redirected to '{err_relative_path}'.")
            elif result.stderr:
                output_msgs.append(f"=== STDERR ===\n{result.stderr.strip()}")
                
            output_msgs.append(f"EXIT CODE: {result.returncode}")
            return "\n".join(output_msgs)

        except subprocess.TimeoutExpired:
            return f"ERROR: Execution timed out after {timeout} seconds"
        except Exception as e:
            return f"ERROR: {e}"
        finally:
            # 确保无论脚本成功还是崩溃，文件句柄都能被正确关闭
            if f_out:
                f_out.close()
            if f_err:
                f_err.close()