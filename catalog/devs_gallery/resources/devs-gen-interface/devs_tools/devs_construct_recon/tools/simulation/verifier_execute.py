from smolagents import Tool
import os
import signal
import sys
import subprocess
import tempfile
import shutil
import threading
import time
from pathlib import Path
import json
from typing import List, Dict, Optional, Union, Tuple
from dataclasses import dataclass

from default_tools.generated_execution import (
    ExecutionBoundaryError,
    GeneratedExecutionBoundary,
    PythonLaunch,
)
from default_tools.path_security import (
    UnsafePathError,
    resolve_confined_path,
)


MAX_EXECUTION_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
PROCESS_STOP_GRACE_SECONDS = 0.5
PROCESS_POLL_INTERVAL_SECONDS = 0.02


class _BoundedCapture:
    def __init__(self, limit: int):
        self.limit = limit
        self.chunks: List[bytes] = []
        self.size = 0
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        remaining = self.limit - self.size
        if remaining > 0:
            kept = chunk[:remaining]
            self.chunks.append(kept)
            self.size += len(kept)
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


def _read_stream(stream, capture: _BoundedCapture, overflow: threading.Event) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            capture.add(chunk)
            if capture.truncated:
                overflow.set()
    except (OSError, ValueError):
        return
    finally:
        stream.close()


def _write_stdin(stream, content: str) -> None:
    if stream is None:
        return
    try:
        stream.write(content.encode("utf-8"))
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _terminate(process) -> None:
    if process is None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

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
    def __init__(
        self,
        working_directory: str = "./working_dir",
        *,
        execution_mode: str | None = None,
        allow_trusted_process: bool = False,
        container_engine: str | None = None,
        container_image: str | None = None,
    ):
        self.working_directory = Path(working_directory).resolve()
        self.execution_boundary = GeneratedExecutionBoundary(
            mode=execution_mode,
            allow_trusted_process=allow_trusted_process,
            engine=container_engine,
            image=container_image,
            python_executable=sys.executable,
        )

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
            full_script_path = resolve_confined_path(
                self.working_directory,
                script_path,
                must_exist=True,
                expected="file",
            )
            timeout = max(1, min(int(timeout), MAX_EXECUTION_TIMEOUT_SECONDS))
        except FileNotFoundError:
            return ExecutionResult(-1, "", "", f"Script file not found: {script_path}")
        except UnsafePathError as e:
            return ExecutionResult(-1, "", "", f"Unsafe script path: {e}")
        except (TypeError, ValueError):
            return ExecutionResult(-1, "", "", "Timeout must be an integer number of seconds.")
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
                copied_destinations = {target_script_name}
                for item in files_to_copy:
                    src_rel = item["src"]
                    dest_name = item["dest"] # 如果为None，则保留原名
                    
                    try:
                        src_full = resolve_confined_path(
                            self.working_directory,
                            src_rel,
                            must_exist=True,
                            expected="file",
                        )
                    except FileNotFoundError:
                        return ExecutionResult(-1, "", "", f"Input file not found: {src_rel}")
                    except UnsafePathError as e:
                        return ExecutionResult(-1, "", "", f"Unsafe input file {src_rel}: {e}")

                    final_dest_name = dest_name if dest_name else src_full.name
                    # 确保目标目录结构存在（如果dest包含子目录）
                    try:
                        dest_full_path = resolve_confined_path(
                            temp_dir_path, final_dest_name
                        )
                    except UnsafePathError as e:
                        return ExecutionResult(-1, "", "", f"Unsafe input destination: {e}")
                    relative_destination = dest_full_path.relative_to(temp_dir_path).as_posix()
                    if relative_destination in copied_destinations:
                        return ExecutionResult(
                            -1,
                            "",
                            "",
                            f"Duplicate input destination is not allowed: {relative_destination}",
                        )
                    copied_destinations.add(relative_destination)
                    dest_full_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    shutil.copy2(src_full, dest_full_path)

                # --- B. 执行脚本 (Execution) ---
                launch: PythonLaunch | None = None
                process = None
                stdout_capture = _BoundedCapture(MAX_STDOUT_BYTES)
                stderr_capture = _BoundedCapture(MAX_STDERR_BYTES)
                overflow = threading.Event()
                readers = []
                writer = None
                failure = None
                try:
                    launch = self.execution_boundary.build_python_launch(
                        temp_dir_path,
                        ("-u", target_script_name),
                        home_directory=temp_dir_path,
                        temporary_directory=temp_dir_path,
                        stdin_open=stdin_content is not None,
                    )
                    process = subprocess.Popen(
                        launch.argv,
                        cwd=str(launch.cwd),
                        stdin=(
                            subprocess.PIPE
                            if stdin_content is not None
                            else subprocess.DEVNULL
                        ),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=launch.environment,
                        start_new_session=(os.name == "posix"),
                        bufsize=0,
                    )
                    for stream, capture in (
                        (process.stdout, stdout_capture),
                        (process.stderr, stderr_capture),
                    ):
                        reader = threading.Thread(
                            target=_read_stream,
                            args=(stream, capture, overflow),
                            daemon=True,
                        )
                        reader.start()
                        readers.append(reader)
                    if stdin_content is not None:
                        writer = threading.Thread(
                            target=_write_stdin,
                            args=(process.stdin, stdin_content),
                            daemon=True,
                        )
                        writer.start()
                    started = time.monotonic()
                    while process.poll() is None:
                        if overflow.is_set():
                            failure = (
                                "Execution exceeded the stdout or stderr output limit "
                                f"({MAX_STDOUT_BYTES} bytes per stream)."
                            )
                            self.execution_boundary.force_remove(launch)
                            break
                        if time.monotonic() - started >= timeout:
                            failure = f"Execution timed out after {timeout}s."
                            self.execution_boundary.force_remove(launch)
                            break
                        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)
                    _terminate(process)
                    for reader in readers:
                        reader.join(timeout=1.0)
                    if writer is not None:
                        writer.join(timeout=1.0)
                finally:
                    self.execution_boundary.force_remove(launch)
                    _terminate(process)
                stdout_str = stdout_capture.text()
                stderr_str = stderr_capture.text()
                if failure:
                    return ExecutionResult(-1, stdout_str, stderr_str, failure)
                
                # --- C. 保存输出 (Save Output) ---
                if stdout_save_path:
                    out_path = resolve_confined_path(
                        self.working_directory, stdout_save_path
                    )
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path = resolve_confined_path(
                        self.working_directory, stdout_save_path
                    )
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(stdout_str)
                        
                if stderr_save_path:
                    err_path = resolve_confined_path(
                        self.working_directory, stderr_save_path
                    )
                    err_path.parent.mkdir(parents=True, exist_ok=True)
                    err_path = resolve_confined_path(
                        self.working_directory, stderr_save_path
                    )
                    with open(err_path, 'w', encoding='utf-8') as f:
                        f.write(stderr_str)

                return ExecutionResult(
                    return_code=process.returncode if process is not None else -1,
                    stdout=stdout_str,
                    stderr=stderr_str
                )

            except ExecutionBoundaryError as e:
                return ExecutionResult(-1, "", "", str(e))
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
        "The validator and logs are copied into the same credential-free, network-disabled "
        "container boundary used for generated simulators. "
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

    def __init__(
        self,
        working_directory: str = "./working_dir",
        **execution_options,
    ):
        super().__init__()
        self.working_directory = working_directory
        # 实例化通用执行器
        self.executor = PythonScriptExecutor(
            working_directory=working_directory,
            **execution_options,
        )
    
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
