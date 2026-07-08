import os
import re
from typing import Optional, Any
from smolagents import Tool
from litellm import completion
from ...utils import get_content_strict
import ast

# ==============================================================================
# Refiner Prompt: 核心在于 "Conservation" (守恒律)
# ==============================================================================
REFINER_SYSTEM_PROMPT = """
You are a Senior Code Refiner. 
Your task is to rewrite the provided source code based STRICTLY on the user's modification instructions.

## Golden Rules
1. **Minimal Intervention**: Only change what is necessary to satisfy the instruction. 
2. **Conservation**: Do NOT change logic, variable names, or formatting in parts of the code unrelated to the instruction.
3. **Completeness**: You must output the **FULL** file content, not just the diff.
"""

class CodeRefiner(Tool):
    name = "code_refiner"
    description = (
        "An intelligent tool that rewrites a file based on natural language instructions. "
        "Use this for complex refactoring, adding docstrings, or logic changes that are hard to describe with line numbers. Or if the tranditional diff-based approach failed too many times."
        "It reads the full file, applies changes using an LLM, and overwrites the file."
    )
    inputs = {
        "filename": {
            "type": "string", 
            "description": "The path to the file to be modified."
        },
        "instruction": {
            "type": "string", 
            "description": "Highly detailed natural language instructions on what to modify. Code is allowed."
        }
    }
    output_type = "string"

    def __init__(self, working_dir: str, default_model: str = "gpt-4o"):
        super().__init__()
        self.working_dir = working_dir
        self.default_model = default_model

    def _clean_code_block(self, text: str) -> str:
        """Helper to strip markdown code blocks if the LLM includes them."""
        # 匹配 ```python ... ``` 或者 ``` ... ```
        pattern = r"^```(?:\w+)?\s*\n(.*?)\n```$"
        match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
        if match:
            return match.group(1)
        return text

    def _validate_python_syntax(self, code: str) -> Optional[str]:
        """
        利用 ast.parse 检查语法。
        如果通过返回 None，如果失败返回错误描述。
        """
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"SyntaxError at line {e.lineno}, offset {e.offset}: {e.msg}"
        except Exception as e:
            # 捕获其他可能的编码错误
            return f"Parinsg Error: {str(e)}"

    def forward(self, filename: str, instruction: str) -> Any:
        filepath = os.path.join(self.working_dir, filename)
        if not os.path.exists(filepath):
            return f"Error: File {filename} not found."

        # 1. 读取原始内容
        with open(filepath, 'r', encoding='utf-8') as f:
            original_code = f.read()

        # 2. 构建基础 Prompt
        base_user_content = f"""
## Target File: {filename}

## Original Code:
<code_content>
{original_code}
</code_content>

## Modification Instruction:
{instruction}

## Task:
Output the COMPLETE updated code based on the instruction.
"""
        used_model = self.default_model
        last_error_msg = None
        max_retries = 3

        # ==============================================================================
        # 3. 自动重试循环 (Smart Retry Loop)
        # ==============================================================================
        for attempt in range(max_retries):
            try:
                # 动态构建本次的 Prompt
                # 如果是重试（attempt > 0），则追加上一次的错误信息，引导模型自我修复
                if attempt > 0 and last_error_msg:
                    print(f"[CodeRefiner] Attempt {attempt + 1}/{max_retries}: Retrying due to error...")
                    current_user_content = base_user_content + f"""
\n
## PREVIOUS ATTEMPT FAILED
Your previous attempt to modify this code failed with the following error:
<error_feedback>
{last_error_msg}
</error_feedback>

Please CAREFULLY make sure the code do not have this error. Ensure valid Python syntax.
"""
                else:
                    current_user_content = base_user_content

                # 调用 LLM
                response = completion(
                    model=used_model,
                    messages=[
                        {"role": "system", "content": REFINER_SYSTEM_PROMPT},
                        {"role": "user", "content": current_user_content}
                    ],
                    # 重试时稍微增加一点温度，避免陷入死循环，但保持在低位
                    temperature=0.05 if attempt == 0 else 0.1, 
                )
                
                new_code_raw = get_content_strict(response)
                new_code = self._clean_code_block(new_code_raw)
                
                # 校验 1: 空内容检查
                if not new_code.strip():
                    last_error_msg = "The model returned empty content."
                    continue # 进入下一次循环

                # 校验 2: 语法检查 (仅针对 Python)
                if filename.endswith(".py"):
                    syntax_error = self._validate_python_syntax(new_code)
                    if syntax_error:
                        # 记录具体的语法错误，供下一次 Prompt 使用
                        last_error_msg = f"SyntaxError found: {syntax_error}"
                        continue # 进入下一次循环

                # ==============================================================================
                # 成功分支
                # ==============================================================================
                # 如果能走到这里，说明没有异常且通过了所有检查
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_code)
                    
                return f"Success: {filename} has been rewritten based on instructions (Succeeded at attempt {attempt + 1})."

            except Exception as e:
                # 捕获 API 报错或其他运行时异常
                last_error_msg = f"Runtime Exception: {str(e)}"
                # 继续循环重试

        # ==============================================================================
        # 失败分支 (三次都失败)
        # ==============================================================================
        return f"Error: Failed to modify {filename} after {max_retries} attempts. \nLast Error: {last_error_msg}"