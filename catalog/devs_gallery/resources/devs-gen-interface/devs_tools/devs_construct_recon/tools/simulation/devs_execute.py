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
import signal
import shlex  # 新增引用，用于解析命令行参数字符串
import threading
from typing import Optional, List

from src.progress import ProgressReporter

from default_tools.generated_execution import (
    ExecutionBoundaryError,
    GeneratedExecutionBoundary,
    PythonLaunch,
    stage_installed_xdevs_package,
)
from default_tools.interface_output_action import (
    InterfaceOutputActionClient,
    OutputActionError,
    OutputActionExecutor,
    OutputActionResult,
)
from default_tools.path_security import (
    UnsafePathError,
    resolve_confined_path,
    validate_regular_tree,
)

PROJECT_FOLDER_NAME = "devs_project"
UTILS_DIR = os.path.join(Path(os.path.dirname(__file__)).parent.parent, "materials", "devs_project", "devs_utils")
MAX_EXECUTION_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_RESPONSE_OUTPUT_CHARS = 4000
PROCESS_STOP_GRACE_SECONDS = 0.5
PROCESS_POLL_INTERVAL_SECONDS = 0.02


class _BoundedCapture:
    """Drain a subprocess pipe while retaining no more than ``limit`` bytes."""

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


def _read_stream(stream, capture: _BoundedCapture, overflow_event: threading.Event) -> None:
    if stream is None:
        return
    try:
        while True:
            try:
                chunk = stream.read(64 * 1024)
            except (OSError, ValueError):
                # Process-group cleanup can close a pipe while its reader is
                # blocked.  The bytes already retained remain useful.
                return
            if not chunk:
                return
            capture.add(chunk)
            if capture.truncated:
                overflow_event.set()
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


def _signal_process_group(process, signal_number: int) -> None:
    """Signal the isolated process group, falling back to the direct child."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal_number)
            return
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            pass
    if process.poll() is not None:
        return
    try:
        if signal_number == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _process_group_exists(process) -> bool:
    if os.name != "posix":
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_process_group(process) -> None:
    """Stop the child and every descendant that inherited its process group."""

    _signal_process_group(process, signal.SIGTERM)
    deadline = time.monotonic() + PROCESS_STOP_GRACE_SECONDS
    while _process_group_exists(process) and time.monotonic() < deadline:
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)
    _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        _signal_process_group(process, signal.SIGKILL)


def _bounded_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "[... earlier output omitted ...]\n"
    return marker + text[-max(limit - len(marker), 0):]


def _python_module_name(relative_file: str) -> str:
    path = Path(relative_file)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UnsafePathError("The main file must be a canonical relative path.")
    if path.suffix != ".py":
        raise UnsafePathError("The main file must be a Python source file.")
    module_parts = list(path.with_suffix("").parts)
    if not module_parts or any(not part.isidentifier() for part in module_parts):
        raise UnsafePathError("The main file path is not a valid Python module path.")
    return ".".join((PROJECT_FOLDER_NAME, *module_parts))

class DEVSExecute(Tool):
    name = "devs_execute"
    description = (
        "Execute a DEVS model file or project from a temporary copy. "
        "It supports execution of single Python scripts or complex projects involving multiple files. "
        "For a generated simulation bundle, pass its dedicated folder and "
        "main_file='run.py'; managed launches execute that exact bundle through "
        "the same action used by the student-facing Run button. Other source "
        "layouts are copied into a temporary directory and executed in module mode. "
        "The tool captures bounded output and runs generated code in a separate, "
        "network-disabled, resource-limited container. It never forwards interface credentials. "
        "The path must name a dedicated file or subfolder relative to the agent's "
        "working directory; do not pass '.'."
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
            "description": "Deprecated compatibility input. Import policy is enforced by the outer isolated runtime, not this process helper.",
            "nullable": True
        },
        "stdin_content": {
            "type": "string", 
            "description": "Standalone-only standard input. Omit this in an OptPilot managed launch; an empty string is treated as omitted.",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(
        self,
        working_directory: str = "./working_dir",
        *,
        execution_mode: str | None = None,
        allow_trusted_process: bool = False,
        container_engine: str | None = None,
        container_image: str | None = None,
        output_action_executor: OutputActionExecutor | None = None,
        progress_reporter: ProgressReporter | None = None,
    ):
        super().__init__()
        # 保存工作目录的绝对路径，作为所有文件操作的基准根目录
        self.working_directory = working_directory
        self.working_dir_path = Path(self.working_directory).resolve()
        # 确保该目录存在
        self.working_dir_path.mkdir(parents=True, exist_ok=True)
        self.execution_boundary = GeneratedExecutionBoundary(
            mode=execution_mode,
            allow_trusted_process=allow_trusted_process,
            engine=container_engine,
            image=container_image,
            python_executable=sys.executable,
        )
        self.output_action_executor = (
            output_action_executor
            if output_action_executor is not None
            else InterfaceOutputActionClient.from_environment()
        )
        self.progress_reporter = progress_reporter

    def forward(self, project_path: str, timeout: int = 30, 
                command_args: Optional[str] = None,
                stdout_file: Optional[str] = None,
                stderr_file: Optional[str] = None,
                allowed_libraries: str = "xdevs,logging,math,random,time,collections,itertools,json,sys,pathlib,statistics,dataclasses,typing",
                main_file: str = "main.py",
                stdin_content: Optional[str] = None) -> str:
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                activity_key="agent_test_simulation",
                state="started",
                title="Testing the generated simulation",
                detail="The agent is executing a bounded copy in the prepared runtime.",
                technical_name="devs_execute",
            )
        try:
            response = self._execute(
                project_path=project_path,
                timeout=timeout,
                command_args=command_args,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
                allowed_libraries=allowed_libraries,
                main_file=main_file,
                stdin_content=stdin_content,
            )
        except Exception:
            if self.progress_reporter is not None:
                self.progress_reporter.emit(
                    activity_key="agent_test_simulation",
                    state="failed",
                    title="Simulation test encountered a problem",
                    detail="The agent will inspect the failure and may revise the generated files.",
                    technical_name="devs_execute",
                )
            raise
        succeeded = response.startswith("STATUS: SUCCESS")
        if self.progress_reporter is not None:
            self.progress_reporter.emit(
                activity_key="agent_test_simulation",
                state="completed" if succeeded else "failed",
                title=(
                    "Generated simulation ran successfully"
                    if succeeded
                    else "Generated simulation needs a repair"
                ),
                detail=(
                    "The agent's bounded execution check completed successfully."
                    if succeeded
                    else "The execution check found a problem for the agent to correct."
                ),
                technical_name="devs_execute",
            )
        return response

    def _execute(self, project_path: str, timeout: int = 30,
                 command_args: Optional[str] = None,
                 stdout_file: Optional[str] = None,
                 stderr_file: Optional[str] = None,
                 allowed_libraries: str = "xdevs,logging,math,random,time,collections,itertools,json,sys,pathlib,statistics,dataclasses,typing",
                 main_file: str = "main.py",
                 stdin_content: Optional[str] = None) -> str:
        
        print(f"Starting DEVSExecute with file_or_project_path: {project_path}, timeout: {timeout}, command_args: {command_args}, stdout_file: {stdout_file}, stderr_file: {stderr_file}, allowed_libraries: {allowed_libraries}, main_file: {main_file}")
        # 1. 默认值处理
        if timeout is None:
            timeout = 30
        try:
            timeout = max(1, min(int(timeout), MAX_EXECUTION_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return "STATUS: FAILED\nReason: Timeout must be an integer number of seconds."
        allowed_libs = [
            lib.strip()
            for lib in (allowed_libraries or "").split(",")
            if lib.strip()
        ]
        # Models frequently supply nullable string fields as ``""``.  Empty
        # stdin has no observable content and is therefore equivalent to
        # omitting stdin; rejecting it prevented the generation agent from ever
        # reaching the real simulator failure.
        if stdin_content == "":
            stdin_content = None
        
        # 2. 关键修正：路径解析逻辑
        # 将输入的相对路径与 working_directory 拼接，而不是依赖系统 CWD
        try:
            target_path = resolve_confined_path(self.working_dir_path, project_path)
            
            # 容错处理：如果 Agent 忘记了 devs_models/ 前缀，或者是创建工具默认放到了子文件夹
            if not target_path.exists():
                potential_path = resolve_confined_path(
                    self.working_dir_path,
                    Path("devs_models") / Path(project_path),
                )
                if potential_path.exists():
                    target_path = potential_path
                else:
                    # 如果都找不到，返回包含工作目录路径的详细错误，帮助 Agent 自检
                    return f"STATUS: FAILED\nReason: File or directory '{project_path}' not found in working directory '{self.working_dir_path}'."

            if target_path.is_dir():
                validate_regular_tree(target_path)
            elif not target_path.is_file():
                return "STATUS: FAILED\nReason: Path must be a regular file or directory."

        except (UnsafePathError, ValueError) as e:
            return f"STATUS: FAILED\nReason: Access denied: {e}"
        except Exception as e:
            return f"STATUS: FAILED\nReason: Error resolving path: {e}"

        # A generated bundle already has the exact layout consumed by the
        # Catalog action: <bundle>/run.py plus its source tree.  Do not wrap it
        # in another synthetic ``devs_project`` package, because that changes
        # import semantics and tests something different from the Run button.
        requested_main_file = main_file or "main.py"
        if (
            self.output_action_executor is not None
            and target_path.is_dir()
            and requested_main_file == "run.py"
        ):
            try:
                resolve_confined_path(
                    target_path,
                    "run.py",
                    must_exist=True,
                    expected="file",
                )
            except (UnsafePathError, FileNotFoundError) as exc:
                return f"STATUS: FAILED\nReason: Invalid main file 'run.py': {exc}"
            if stdin_content is not None:
                return (
                    "STATUS: FAILED\n"
                    "FAILURE_KIND: UNSUPPORTED_INPUT\n"
                    "Reason: Managed simulator execution does not accept "
                    "standard input. Pass bounded command arguments instead."
                )
            try:
                exact_arguments = shlex.split(command_args) if command_args else []
            except Exception as exc:
                return f"STATUS: FAILED\nReason: Error parsing command_args: {exc}"
            return self._run_through_output_action(
                source_directory=target_path,
                arguments=exact_arguments,
                timeout=timeout,
                target_path=target_path,
                main_file="run.py",
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )

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
                            if sibling.is_symlink() or not sibling.is_file():
                                return f"STATUS: FAILED\nReason: Unsafe sibling source '{sibling.name}'."
                            shutil.copy2(sibling, temp_dir_path / PROJECT_FOLDER_NAME / sibling.name)

                elif target_path.is_dir():
                    # Case B: 项目目录
                    # 复制整个目录结构
                    shutil.copytree(target_path, temp_dir_path / PROJECT_FOLDER_NAME, dirs_exist_ok=True)
                    target_main_file = main_file or "main.py"
                    try:
                        main_source = resolve_confined_path(
                            target_path,
                            target_main_file,
                            must_exist=True,
                            expected="file",
                        )
                    except (UnsafePathError, FileNotFoundError) as e:
                        return f"STATUS: FAILED\nReason: Invalid main file '{target_main_file}': {e}"
                    target_main_file = main_source.relative_to(target_path).as_posix()
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
            try:
                target_module_name = _python_module_name(target_main_file)
            except UnsafePathError as e:
                return f"STATUS: FAILED\nReason: {e}"
            
            # 将 launcher 放在 temp_dir 根目录下 (PROJECT_FOLDER_NAME 的上一级)
            launcher_path = self._create_launcher_script(
                temp_dir_path,
                target_module_name,
                allowed_libs,
                filename=(
                    "run.py"
                    if self.output_action_executor is not None
                    else "safe_launcher.py"
                ),
            )

            # Container execution never installs dependencies.  It receives a
            # trusted copy of the already-installed pure-Python xDEVS package
            # through the read-only scratch workspace instead.
            if (
                self.output_action_executor is None
                and self.execution_boundary.mode == "container"
            ):
                try:
                    stage_installed_xdevs_package(temp_dir_path)
                except ExecutionBoundaryError as exc:
                    return f"STATUS: FAILED\nReason: {exc}"

            # 5. 运行子进程
            start_time = time.monotonic()
            execution_time = 0.0
            success = False
            stdout, stderr = "", ""
            failure_reason = None

            # Build Python arguments first. The execution boundary decides
            # whether these run in the required container or an explicitly
            # enabled trusted-local process used by unit tests.
            python_args = ["-u", launcher_path.name]
            # 如果有额外参数，解析后追加
            if command_args:
                try:
                    args_list = shlex.split(command_args)
                    python_args.extend(args_list)
                except Exception as e:
                    return f"STATUS: FAILED\nReason: Error parsing command_args: {e}"

            if self.output_action_executor is not None:
                if stdin_content is not None:
                    return (
                        "STATUS: FAILED\n"
                        "FAILURE_KIND: UNSUPPORTED_INPUT\n"
                        "Reason: Managed simulator execution does not accept "
                        "standard input. Pass bounded command arguments instead."
                    )
                return self._run_through_output_action(
                    source_directory=temp_dir_path,
                    arguments=python_args[2:],
                    timeout=timeout,
                    target_path=target_path,
                    main_file=main_file,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                )
            
            stdout_capture = _BoundedCapture(MAX_STDOUT_BYTES)
            stderr_capture = _BoundedCapture(MAX_STDERR_BYTES)
            overflow_event = threading.Event()
            process = None
            readers = []
            stdin_writer = None
            launch: PythonLaunch | None = None
            try:
                launch = self.execution_boundary.build_python_launch(
                    temp_dir_path,
                    python_args,
                    home_directory=temp_dir_path,
                    temporary_directory=temp_dir_path,
                    stdin_open=stdin_content is not None,
                )
                # A new session gives this execution its own process group, so
                # timeouts and output-limit failures also stop grandchildren.
                process = subprocess.Popen(
                    launch.argv,
                    cwd=str(launch.cwd),
                    stdin=subprocess.PIPE if stdin_content is not None else subprocess.DEVNULL,
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
                        args=(stream, capture, overflow_event),
                        daemon=True,
                    )
                    reader.start()
                    readers.append(reader)
                if stdin_content is not None:
                    stdin_writer = threading.Thread(
                        target=_write_stdin,
                        args=(process.stdin, stdin_content),
                        daemon=True,
                    )
                    stdin_writer.start()

                while process.poll() is None:
                    elapsed = time.monotonic() - start_time
                    if overflow_event.is_set():
                        failure_reason = (
                            "Execution exceeded the stdout or stderr output limit "
                            f"({MAX_STDOUT_BYTES} bytes per stream)."
                        )
                        self.execution_boundary.force_remove(launch)
                        break
                    if elapsed >= timeout:
                        failure_reason = f"Execution timed out after {timeout} seconds."
                        self.execution_boundary.force_remove(launch)
                        break
                    time.sleep(PROCESS_POLL_INTERVAL_SECONDS)

                # A program can exit after spawning children. Always close its
                # entire process group before releasing the temporary tree.
                _terminate_process_group(process)
                for reader in readers:
                    reader.join(timeout=1.0)
                if stdin_writer is not None:
                    stdin_writer.join(timeout=1.0)

                stdout = stdout_capture.text()
                stderr = stderr_capture.text()
                execution_time = time.monotonic() - start_time
                if stdout_capture.truncated or stderr_capture.truncated:
                    failure_reason = (
                        "Execution exceeded the stdout or stderr output limit "
                        f"({MAX_STDOUT_BYTES} bytes per stream)."
                    )
                success = failure_reason is None and process.returncode == 0
                if not success and failure_reason is None:
                    failure_reason = f"Simulation exited with code {process.returncode}."
            except ExecutionBoundaryError as e:
                execution_time = time.monotonic() - start_time
                success = False
                failure_reason = str(e)
            except Exception as e:
                execution_time = time.monotonic() - start_time
                success = False
                failure_reason = f"Subprocess internal error: {e}"
            finally:
                self.execution_boundary.force_remove(launch)
                if process is not None:
                    _terminate_process_group(process)
                    if process.stdin is not None and not process.stdin.closed:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                for reader in readers:
                    reader.join(timeout=1.0)
                if stdin_writer is not None:
                    stdin_writer.join(timeout=1.0)
                stdout = stdout or stdout_capture.text()
                stderr = stderr or stderr_capture.text()

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
            
            if success:
                output_sections = []
                if key_results:
                    output_sections.append(
                        "--- EXTRACTED RESULTS ---\n"
                        + "\n".join([f"- {result}" for result in key_results])
                    )
                elif stdout.strip():
                    output_sections.append("--- STDOUT ---\n" + stdout.strip())
                if stderr.strip():
                    output_sections.append("--- STDERR WARNINGS ---\n" + stderr.strip())
                if output_sections:
                    response += "\n" + _bounded_tail(
                        "\n\n".join(output_sections), MAX_RESPONSE_OUTPUT_CHARS
                    )
            else:
                response += f"REASON: {failure_reason or 'Execution failed.'}\n"
                error_output = stderr.strip()
                if not error_output and stdout.strip():
                    error_output = stdout.strip()
                if error_output:
                    response += "\n--- ERROR OUTPUT ---\n" + _bounded_tail(
                        error_output, MAX_RESPONSE_OUTPUT_CHARS
                    )
                
            print(f" DEVS model execution success, response: {response}")
            
            return response

    def _run_through_output_action(
        self,
        *,
        source_directory: Path,
        arguments: List[str],
        timeout: int,
        target_path: Path,
        main_file: str,
        stdout_file: Optional[str],
        stderr_file: Optional[str],
    ) -> str:
        """Run the agent's scratch simulator through OptPilot's file broker."""

        assert self.output_action_executor is not None
        started = time.monotonic()
        result: OutputActionResult | None = None
        try:
            result = self.output_action_executor.execute(
                source_directory=source_directory,
                arguments=arguments,
                results_directory=None,
                timeout_seconds=timeout,
                response_timeout_seconds=max(90.0, float(timeout) + 30.0),
            )
            execution_time = result.duration_seconds
            stdout = result.stdout
            stderr = result.stderr
            if result.stdout_truncated or result.stderr_truncated:
                success = False
                failure_kind = "OUTPUT_LIMIT"
                failure_reason = (
                    "Execution exceeded the stdout or stderr output limit."
                )
            elif result.status == "succeeded":
                success = True
                failure_kind = None
                failure_reason = None
            elif result.status == "timed_out":
                success = False
                failure_kind = "TIMEOUT"
                failure_reason = (
                    f"Execution timed out in the isolated runtime after "
                    f"{execution_time:.2f} seconds."
                )
            elif result.status == "cancelled":
                success = False
                failure_kind = "CANCELLED"
                failure_reason = "Execution was cancelled."
            elif result.status == "infrastructure_failed":
                success = False
                failure_kind = "EXECUTION_INFRASTRUCTURE"
                detail = (
                    f" ({result.failure_code})" if result.failure_code else ""
                )
                failure_reason = (
                    "OptPilot could not establish the isolated simulation "
                    f"runtime{detail}."
                )
            elif result.status == "rejected":
                success = False
                failure_kind = "EXECUTION_INFRASTRUCTURE"
                detail = (
                    f" ({result.failure_code})" if result.failure_code else ""
                )
                failure_reason = (
                    f"OptPilot rejected the simulation execution request{detail}."
                )
            else:
                success = False
                failure_kind = "GENERATED_CODE"
                failure_reason = (
                    f"Simulation exited with code {result.exit_code}."
                    if result.exit_code is not None
                    else "Simulation failed in the isolated runtime."
                )
        except OutputActionError as exc:
            execution_time = time.monotonic() - started
            stdout = ""
            stderr = ""
            success = False
            failure_kind = "EXECUTION_INFRASTRUCTURE"
            failure_reason = (
                "OptPilot could not run the simulator in the isolated "
                f"interface runtime: {exc}"
            )
        except Exception as exc:
            execution_time = time.monotonic() - started
            stdout = ""
            stderr = ""
            success = False
            failure_kind = "EXECUTION_INFRASTRUCTURE"
            failure_reason = (
                "Simulation execution failed while communicating with the "
                f"isolated interface runtime: {exc}"
            )

        if stdout_file:
            self._save_log(stdout_file, stdout)
        if stderr_file:
            self._save_log(stderr_file, stderr)

        key_results = self._extract_key_results(
            stdout, stderr, success, execution_time
        )
        response = f"STATUS: {'SUCCESS' if success else 'FAILED'}\n"
        response += f"TARGET: {target_path.name}, {main_file}\n"
        response += f"TIME: {execution_time:.2f}s\n"
        if failure_kind is not None:
            response += f"FAILURE_KIND: {failure_kind}\n"
        if stdout_file:
            response += f"STDOUT_FILE: {stdout_file}\n"
        if stderr_file:
            response += f"STDERR_FILE: {stderr_file}\n"
        if success:
            sections: List[str] = []
            if key_results:
                sections.append(
                    "--- EXTRACTED RESULTS ---\n"
                    + "\n".join(f"- {item}" for item in key_results)
                )
            elif stdout.strip():
                sections.append("--- STDOUT ---\n" + stdout.strip())
            if stderr.strip():
                sections.append("--- STDERR WARNINGS ---\n" + stderr.strip())
            if sections:
                response += "\n" + _bounded_tail(
                    "\n\n".join(sections), MAX_RESPONSE_OUTPUT_CHARS
                )
        else:
            response += f"REASON: {failure_reason or 'Execution failed.'}\n"
            error_output = stderr.strip() or stdout.strip()
            if error_output:
                response += "\n--- ERROR OUTPUT ---\n" + _bounded_tail(
                    error_output, MAX_RESPONSE_OUTPUT_CHARS
                )
        return response

    def _create_launcher_script(
        self,
        temp_dir: Path,
        module_name: str,
        allowed_libs: List[str],
        *,
        filename: str = "safe_launcher.py",
    ) -> Path:
        """Creates a 'safe_launcher.py' that sets up the import hook and runs the user code as a module."""
        launcher_content = """
import sys
import runpy
import os
import traceback

# 简单的环境设置
target_module = {module_name!r}

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
""".format(module_name=module_name)

        launcher_path = temp_dir / filename
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

        return results


    def _save_log(self, log_file: str, content: str):
        try:
            # 强制日志保存在 working_directory 下
            target_path = resolve_confined_path(self.working_dir_path, log_file)

            target_path.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after directory creation so an existing special path or
            # newly introduced symlink is never opened for writing.
            target_path = resolve_confined_path(self.working_dir_path, log_file)
            with open(target_path, "w", encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            print(f"Failed to write log file: {e}")
