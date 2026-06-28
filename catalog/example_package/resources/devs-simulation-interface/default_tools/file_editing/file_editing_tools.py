from smolagents.tools import Tool
import os
from typing import Any, Optional
import importlib.util
import difflib
import ast

class ListDir(Tool):
    name = "list_dir"
    description = (
        "List files in the chosen directory. Use this to explore the directory structure. "
        "Note: only files under the allowed working directory are accessible."
    )
    inputs = {"directory": {"type": "string", "description": "The directory to check."}}
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, directory: str) -> Any:
        try:
            chosen_dir = self._safe_path(directory)
        except PermissionError as e:
            return str(e)
        if not os.path.exists(chosen_dir):
            return f"The directory {directory} does not exist. Please start checking from the root directory."
        files = os.listdir(chosen_dir)
        if files == []:
            return f"The directory {directory} is empty."
        else:
            return '\n'.join(files)

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path


class SeeTextFile(Tool):
    name = "see_text_file"
    description = (
        "View the content of a chosen plain text file (e.g., .txt, .md, .py). "
        "It adds line numbers for reference in a distinct 'Gutter' format (e.g., '001 | code'). "
        "These line numbers and the vertical bar '|' are NOT part of the file content. "
        "Do NOT include them when generating new content."
    )
    inputs = {
        "filename": {"type": "string", "description": "Name of the file to check."},
        "include_line_numbers": {"type": "boolean", "description": "Whether to include line numbers in the output."},
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str, include_line_numbers: bool) -> Any:
        try:
            filepath = self._safe_path(filename)
        except PermissionError as e:
            return str(e)
        
        if not os.path.exists(filepath):
            return f"The file {filename} does not exist."
            
        with open(filepath, "r", encoding="utf-8") as file:
            lines = file.readlines()
        
        # 优化：使用 Gutter 风格，并且用 0 填充对齐，保持视觉缩进结构
        # 格式： "001 | import os"
        if include_line_numbers:
            formatted_lines = [f"{i+1:04d} | {line}" for i, line in enumerate(lines)]
            return "".join(formatted_lines)
        else:
            return "".join(lines)
        
        return "".join(formatted_lines)

    def _safe_path(self, path: str) -> str:
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class ReadBinaryAsMarkdown(Tool):
    name = "read_binary_as_markdown"
    description = (
        "Read a binary file (PDF, Word, Excel, PowerPoint, image, audio, etc.) and convert it to markdown using MarkItDown. "
        "Returns the markdown content if successful, or an error message if conversion fails. "
        "Only files under the working directory are accessible."
    )
    inputs = {
        "filename": {"type": "string", "description": "Name of the file to read and convert."}
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str) -> Any:
        try:
            filepath = self._safe_path(filename)
        except PermissionError as e:
            return str(e)
        if not os.path.exists(filepath):
            return f"The file '{filename}' does not exist."
        try:
            from markitdown import MarkItDown

            md = MarkItDown()
            result = md.convert(filepath)
            if hasattr(result, "text_content") and result.text_content:
                return result.text_content
            else:
                return f"The file '{filename}' could not be converted to markdown."
        except Exception as e:
            return f"Error reading '{filename}': {str(e)}"

    def _safe_path(self, path: str) -> str:
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class SmartReplace(Tool):
    name = "smart_replace"
    description = (
        "Surgically replace a specific piece of code with new content. "
        "Unlike the previous version, this tool favors PRECISION over fuzzy matching. "
        "It handles indentation differences but requires exact content matching otherwise. "
        "It creates a SYNTAX CHECK before saving to ensure the file remains valid Python. "
        "To prevent ambiguity, you should provide the 'begin_line' or ensure the 'target_text' is unique."
        "Please do not include the line numbers like ``001 |`` in the texts."
    )
    inputs = {
        "filename": {
            "type": "string", 
            "description": "File to modify."
        },
        "target_text": {
            "type": "string", 
            "description": "The exact code block to be replaced. Indentation (leading spaces) is tolerant, but content must match."
        },
        "replacement_text": {
            "type": "string", 
            "description": "The new code block."
        },
        "begin_line": {
            "type": "integer", 
            "description": "The starting line number (1-based) of the target_text. HIGHLY RECOMMENDED to prevent replacing the wrong occurrence.", 
            "nullable": True
        },
        "context_above": {
            "type": "string",
            "description": "Optional: 1-2 lines of code immediately ABOVE the target_text to verify uniqueness.",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str, target_text: str, replacement_text: str, begin_line: Optional[int] = None, context_above: Optional[str] = None) -> Any:
        filepath = os.path.join(self.working_dir, filename)
        if not os.path.exists(filepath):
            return f"Error: File {filename} not found."

        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 预处理：准备目标块的行列表（去除末尾换行符以便逐行比对）
        target_lines = [line.rstrip() for line in target_text.splitlines()]
        if not target_lines:
            return "Error: target_text is empty."
        
        n_target = len(target_lines)
        
        # 1. 确定搜索范围 (Search Window)
        start_search_idx = 0
        end_search_idx = len(lines)

        if begin_line is not None:
            center_idx = begin_line - 1
            start_search_idx = max(0, center_idx - 10)
            end_search_idx = min(len(lines), center_idx + n_target + 10)
        
        candidates = []

        # 2. 扫描并寻找匹配
        for i in range(start_search_idx, end_search_idx - n_target + 1):
            
            match = True
            
            for j in range(n_target):
                file_line = lines[i + j].rstrip()
                tgt_line = target_lines[j]
                
                # 空行逻辑
                if not tgt_line.strip():
                    if file_line.strip():
                        match = False
                        break
                    else:
                        continue

                # 内容逻辑
                if file_line.strip() != tgt_line.strip():
                    match = False
                    break
                
            if match:
                # 3. Context 校验
                if context_above:
                    ctx_lines = [l.strip() for l in context_above.splitlines() if l.strip()]
                    ctx_match = True
                    current_check_idx = i - 1
                    for ctx_l in reversed(ctx_lines):
                        if current_check_idx < 0:
                            ctx_match = False
                            break
                        if lines[current_check_idx].strip() != ctx_l:
                            ctx_match = False
                            break
                        current_check_idx -= 1
                    
                    if not ctx_match:
                        continue 
                
                candidates.append(i)

        # 4. 决策逻辑
        if len(candidates) == 0:
            msg = f"Error: Could not find the target text in {filename}. Please see the file again and ensure the target_text matches exactly"
            if begin_line:
                 msg += f" (Searched around line {begin_line})"
            return msg

        if len(candidates) > 1:
            if begin_line:
                best_idx = min(candidates, key=lambda x: abs(x - (begin_line - 1)))
                if abs(best_idx - (begin_line - 1)) <= 3:
                    final_idx = best_idx
                else:
                    return f"Error: Found {len(candidates)} matches around line {begin_line}, but none were close enough."
            else:
                return f"Error: Ambiguous match. Found {len(candidates)} occurrences. Please provide 'begin_line'."
        else:
            final_idx = candidates[0]

        # 5. 准备新内容 (In-Memory Modification)
        original_first_line = lines[final_idx]
        original_indent = original_first_line[:len(original_first_line) - len(original_first_line.lstrip())]
        
        new_lines_list = replacement_text.splitlines()
        
        final_replacement_lines = []
        for nl in new_lines_list:
            if not nl.startswith(" ") and not nl.startswith("\t"): 
                final_replacement_lines.append(original_indent + nl + "\n")
            else:
                final_replacement_lines.append(nl + "\n")
        
        if not new_lines_list:
             final_replacement_lines = []

        # 创建一个副本进行替换，用于语法检查
        # 注意：这里我们直接操作 lines 列表，但还没有写入磁盘
        # 为了安全回滚，如果后续检查失败，我们直接 return 错误即可
        lines_backup = lines[:] # 浅拷贝备份（虽然后面直接不写文件就行，但为了逻辑清晰）
        
        lines[final_idx : final_idx + n_target] = final_replacement_lines
        
        new_file_content = "".join(lines)

        # 6. AST 语法检查 (New Feature)
        # 只针对 Python 文件进行检查
        if filename.endswith(".py"):
            try:
                ast.parse(new_file_content)
            except SyntaxError as e:
                # 发现语法错误，拒绝写入
                return (f"Error: The proposed change would create invalid Python code (SyntaxError).\n"
                        f"Line {e.lineno}: {e.msg}\n"
                        f"Please check your indentation or closing brackets.")
            except Exception as e:
                return f"Error: Validation failed: {str(e)}"

        # 7. 写入文件 (只有通过检查才执行)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_file_content)

        return f"Success: Replaced content starting at line {final_idx + 1}."

class ModifyFile(Tool):
    name = "modify_file"
    description = (
        "Modify a plain text file by replacing specific lines with new content. "
        "Only works with plain text files (e.g., .txt, .py, .md). Ensure correct indentation. "
        "Not applicable for binary files such as .pdf, .docx, or spreadsheets."
    )
    inputs = {
        "filename": {"type": "string", "description": "Name of the file to modify."},
        "start_line": {"type": "integer", "description": "Start line number to replace."},
        "end_line": {"type": "integer", "description": "End line number to replace."},
        "new_content": {"type": "string", "description": "New content to insert (with proper indentation)."}
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str, start_line: int, end_line: int, new_content: str) -> Any:
        try:
            filepath = self._safe_path(filename)
        except PermissionError as e:
            return str(e)
        if not os.path.exists(filepath):
            return f"The file {filename} does not exist."
        with open(filepath, "r+", encoding="utf-8") as file:
            lines = file.readlines()
            lines[start_line - 1:end_line] = [new_content + "\n"]
            file.seek(0)
            file.truncate()
            file.write("".join(lines))
        return "Content modified."

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class CreateFileWithContent(Tool):
    name = "create_file_with_content"
    description = (
        "Create a new plain text file (e.g., .txt, .py, .md) and write content into it. "
        "If parent folders in the specified path do not exist, they will be created automatically. "
        "This tool does not support creating binary files such as .pdf, .docx, or images."
    )
    inputs = {
        "filename": {"type": "string", "description": "Name of the file to create."},
        "content": {"type": "string", "description": "Content to write into the file."}
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str, content: str) -> Any:
        try:
            filepath = self._safe_path(filename)
        except PermissionError as e:
            return str(e)

        # Ensure parent directories exist (but remain within working_dir)
        parent_dir = os.path.dirname(filepath)
        if parent_dir and not os.path.exists(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except Exception as e:
                return f"Failed to create parent directories for '{filename}': {e}"

        try:
            with open(filepath, "w", encoding="utf-8") as file:
                file.write(content)
        except Exception as e:
            return f"Failed to create or write file '{filename}': {e}"
        return "File created successfully."

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class SearchKeyword(Tool):
    name = "search_keyword"
    description = (
        "Search for a keyword in a plain text file or recursively in all plain text files within a folder. "
        "Returns matching lines with file names, line numbers and context lines before and after each match. "
        "Only supports plain text files (e.g., .txt, .py, .md). Not suitable for binary formats like .pdf, .docx, .xlsx."
    )
    inputs = {
        "path": {"type": "string", "description": "Path to the file or folder to search in."},
        "keyword": {"type": "string", "description": "Keyword to search for."},
        "context_lines": {
            "type": "integer",
            "description": "Number of lines to include before and after each match."
        }
    }
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, path: str, keyword: str, context_lines: int) -> Any:
        try:
            target_path = self._safe_path(path)
        except PermissionError as e:
            return str(e)
        if not os.path.exists(target_path):
            return f"The path '{path}' does not exist."

        if os.path.isfile(target_path):
            return self._search_in_file(target_path, keyword, context_lines, display_path=path)
        elif os.path.isdir(target_path):
            results = []
            for root, _, files in os.walk(target_path):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, self.working_dir)
                    try:
                        result = self._search_in_file(fpath, keyword, context_lines, display_path=rel_path)
                        if "No matches found" not in result:
                            results.append(result)
                    except Exception as e:
                        results.append(f"[{rel_path}]: Error reading file ({e})")
            return "\n\n".join(results) if results else f"No matches found for '{keyword}' in folder '{path}'."
        else:
            return f"The path '{path}' is neither a file nor a directory."

    def _search_in_file(self, filepath: str, keyword: str, context_lines: int, display_path: str) -> str:
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                lines = file.readlines()
        except UnicodeDecodeError:
            return f"[{display_path}]: Cannot read binary or non-text file."

        num_lines = len(lines)
        match_indices = [i for i, line in enumerate(lines) if keyword in line]

        if not match_indices:
            return f"[{display_path}]: No matches found for '{keyword}'."

        output_lines = set()
        for idx in match_indices:
            start = max(0, idx - context_lines)
            end = min(num_lines, idx + context_lines + 1)
            output_lines.update(range(start, end))

        sorted_output = sorted(output_lines)
        formatted_output = [f"{i+1}: {lines[i].rstrip()}" for i in sorted_output]

        return f"--- Matches in [{display_path}] ---\n" + "\n".join(formatted_output)

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class DeleteFileOrFolder(Tool):
    name = "delete_file_or_folder"
    description = (
        "Delete a specified file or folder. This action is irreversible."
        "If no filename is provided, the tool will delete everything in the working directory."
        "Only files under the allowed working directory are accessible."
    )
    inputs = {"filename": {"type": "string", "description": "Name of the file or folder to delete."}}
    output_type = "string"

    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = working_dir
    
    def forward(self, filename: str) -> Any:
        if filename == "":
            abs_working_dir = os.path.abspath(self.working_dir)
            # Only delete inside the working directory
            for root, dirs, files in os.walk(abs_working_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            return "All files and folders in the working directory have been deleted."
        else:
            try:
                filepath = self._safe_path(filename)
            except PermissionError as e:
                return str(e)
            if os.path.exists(filepath):
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    return f"The file {filename} has been deleted."
                elif os.path.isdir(filepath):
                    os.rmdir(filepath)
                    return f"The folder {filename} has been deleted."
                else:
                    return f"The path {filename} is neither a file nor a folder."
            else:
                return f"The file or folder {filename} does not exist."

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path

class LoadObjectFromPythonFile(Tool):
    name = "load_object_from_python_file"
    description = "Load a class or method from a Python file so it can be used by the agent."
    inputs = {
        "filename": {"type": "string", "description": "The Python file to load from."},
        "object_name": {"type": "string", "description": "The name of the class or method to load."}
    }
    output_type = "object"  # We return an actual callable Python object

    def __init__(self, working_dir: str):
        super().__init__()
        self.working_dir = working_dir

    def forward(self, filename: str, object_name: str) -> Any:
        try:
            file_path = self._safe_path(filename)
        except PermissionError as e:
            raise FileNotFoundError(str(e))
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"The file {filename} does not exist.")

        # Create a module spec
        module_name = os.path.splitext(os.path.basename(file_path))[0]
        spec = importlib.util.spec_from_file_location(module_name, file_path)

        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for file {filename}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, object_name):
            raise AttributeError(f"The object {object_name} was not found in {filename}")

        return getattr(module, object_name)

    def _safe_path(self, path: str) -> str:
        # Prevent absolute paths and directory traversal
        abs_working_dir = os.path.abspath(self.working_dir)
        abs_path = os.path.abspath(os.path.join(self.working_dir, path))
        if not abs_path.startswith(abs_working_dir):
            raise PermissionError("Access outside the working directory is not allowed.")
        return abs_path
