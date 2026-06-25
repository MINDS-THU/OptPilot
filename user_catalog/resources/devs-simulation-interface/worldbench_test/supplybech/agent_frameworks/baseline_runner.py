"""
Baseline Runner: 纯 LLM 推理 → 输出 Python 策略代码字符串。
"""

import os
import re
import json
import time
import litellm

from pathlib import Path
from typing import Dict, Any, Optional


class BaselineRunner:
    """
    读取场景 description.md，构建 prompt，调用 LLM，
    提取包含 POLICY_MOUNTS 的 Python 代码字符串。
    """

    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or os.environ.get("SUPPLYBENCH_MODEL_ID", "openrouter/openai/gpt-5.2")

    def __call__(self, description_path: str) -> Dict[str, Any]:
        start_time = time.time()

        # 读取 description.md
        desc_text = Path(description_path).read_text(encoding="utf-8")

        system_prompt = (
            "You are a supply chain optimization expert. "
            "Your task is to write a Python policy function for a supply chain simulation. "
            "You may explain your reasoning, but you MUST enclose your final Python policy code "
            "within triple backticks: \n```python\n... \n```\n at the end of your response. "
            "Ensure the code includes the POLICY_MOUNTS dictionary."
        )

        user_prompt = desc_text

        try:
            response = litellm.completion(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                timeout=1800,
            )

            raw_response = response.choices[0].message.content or "" # type: ignore
            latency = time.time() - start_time

            # 提取 Python 代码（去除可能的 markdown fence）
            policy_code = self._extract_python_code(raw_response)

            return {
                "policy_code": policy_code,
                "raw_response": raw_response,
                "model_id": self.model_id,
                "latency": latency,
                "success": True,
            }

        except Exception as e:
            return {
                "policy_code": "",
                "raw_response": "",
                "model_id": self.model_id,
                "latency": time.time() - start_time,
                "success": False,
                "error": str(e),
            }

    @staticmethod
    def _extract_python_code(raw: str) -> str:
        """去除 markdown code block 围栏，返回纯 Python 代码。"""
        # 尝试匹配 ```python ... ``` 或 ``` ... ```
        match = re.search(r'```(?:python)?\s*\n(.*?)```', raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 如果没有 fence，尝试返回整个字符串
        return raw.strip()
