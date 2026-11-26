from smolagents import Tool
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
import litellm
from litellm import completion
import json
import hashlib
import sqlite3
import time

litellm.drop_params = True
from ...base_types import StandardContextModel, PlanResult, ModelSpecification, TypedEntity, PortEntity
from .unified_model_creator import process_sub_models
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging
from .generated_interface import extract_generated_python_interface


# Cache entries are only reusable when both the serialized contract and the
# prompt that produced its semantic summary are unchanged. Keep these explicit
# rather than relying on incidental source hashes so releases can invalidate old
# summaries deliberately.
_SUMMARY_CACHE_SCHEMA_VERSION = "standard-context-with-generated-interface-v1"
_SUMMARY_PROMPT_VERSION = "devs-model-summary-v2"


# ====== Output format section (aligned with pydantic schema) ======
# NOTE: Braces are doubled ({{ }}) for .format() compatibility
_SUMMARIZE_OUTPUT_FORMAT = """
## [Output Format]
Return ONLY a JSON object with keys `class_name` and `specification`.

`specification` is:
{{
  "function": "string",
  "external_io": [{{"target": "stdin|stdout|stderr|file|other", "content": "content schema, source/derivation logic, timing, read/write direction, and resource/path details"}}, ...],
  "model_init_args": [{{"name": "str", "type": "str", "structure": "str"}}, ...],
  "input_ports": [{{"name": "str", "type": "str", "structure": "str", "protocol": {{"initial_state": "str", "initial_signal": "str", "description": "str"}}}}, ...],
  "output_ports": [{{"name": "str", "type": "str", "structure": "str", "protocol": {{"initial_state": "str", "initial_signal": "str", "description": "str"}}}}, ...]
}}
"""


class DEVSModelExtraction(BaseModel):
    class_name: str = Field(default="", description="The name of the Python class")
    specification: ModelSpecification = Field(default_factory=ModelSpecification)


# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
SUMMARIZE_PROMPT_TEMPLATE = """
## [Task]
Analyze the provided Python code for a DEVS model. Extract the model's metadata.

## [Rules] (RELAXED for fast_plan mode - keep summaries concise)
- **model_init_args**: Extract all arguments from `__init__` (except `self`). The first two MUST be `name` (str) and `parent` (dict).
- **external_io**: Extract external IO used in THIS model, not sub-models. Keep each stream concise but concrete.
  - `print(...)` or `sys.stdout.write(...)` maps to `target="stdout"`.
  - `sys.stdin` maps to `target="stdin"`.
  - `sys.stderr.write(...)` or `print(..., file=sys.stderr)` maps to `target="stderr"`.
  - `open(..., "r"/"w"/"a")` maps to `target="file"`; put path source/pattern and read/write mode in `content`.
- **ports**: Extract port definitions from docstring. Brief descriptions are fine.
- **function**: Summarize the main logic. For coupled models, list submodels with their instance names.

## [Field Guidance]
- `class_name`: The Python class name from the code.
- `specification.function`: Summarize main logic briefly. For coupled models, list sub-model instance names.
- `specification.external_io`: List of external streams. Each item has `target` and `content`; `target` must be one of `stdin`, `stdout`, `stderr`, `file`, or `other`. The content must include schema, source/derivation logic, timing, read/write behavior, and path/resource details when relevant.
- `specification.model_init_args`: Extract all `__init__` params. Each item: {{"name": "param_name", "type": "int|str|float|bool|dict|list", "structure": "brief description"}}.
- `specification.input_ports` / `output_ports`: Extract from docstring. Each item: {{"name": "port_name", "type": "...", "structure": "...", "protocol": {{"initial_state": "...", "initial_signal": "...", "description": "..."}}}}.

## [Example]
{{
  "class_name": "PacketSender",
  "specification": {{
    "function": "Sends a fixed number of packets at regular intervals. Sub-models: self.sender (PacketGenerator), self.timer (IntervalClock).",
    "external_io": [
      {{
        "target": "stdout",
        "content": "JSON Lines records emitted when each packet is sent. Each record is derived from the sent packet and current simulation time: {{'event': 'packet_sent', 'packet_id': int, 'time': float}}."
      }}
    ],
    "model_init_args": [
      {{"name": "name", "type": "str", "structure": "Model instance name"}},
      {{"name": "parent", "type": "Coupled | None", "structure": "Parent coupled model, or None"}},
      {{"name": "total_packets", "type": "int", "structure": "Number of packets to send"}}
    ],
    "input_ports": [
      {{
        "name": "in_start",
        "type": "dict",
        "structure": "{{'trigger': bool}}",
        "protocol": {{
          "initial_state": "empty",
          "initial_signal": "None",
          "description": "Receives start trigger from external controller"
        }}
      }}
    ],
    "output_ports": [
      {{
        "name": "out_packet",
        "type": "dict",
        "structure": "{{'id': int, 'data': str}}",
        "protocol": {{
          "initial_state": "empty",
          "initial_signal": "None",
          "description": "Sends generated packets to downstream"
        }}
      }}
    ]
  }}
}}

{feedback}

## [Sub-Models Info]
{sub_models}

## [Code]
```python
{code}
```
"""

# ==============================================================================
# TOOL IMPLEMENTATION
# ==============================================================================

class ModelSummarizer:
    def __init__(self, model_id: str, working_directory: str = "./working_dir"):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.cache_db_path = self.working_directory / ".model_summary_cache.db"
        self._init_db()

    def _init_db(self):
        """[New] 初始化数据库表结构"""
        # 使用 timeout 防止多进程启动时抢占锁报错
        try:
            with sqlite3.connect(self.cache_db_path, timeout=30) as conn:
                # 创建简单的 Key-Value 表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS summary_cache (
                        hash_key TEXT PRIMARY KEY,
                        json_value TEXT,
                        updated_at REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            # 极端情况下如果无法创建DB，打印日志但不中断主流程（降级为无缓存模式）
            print(f"⚠️ Cache DB Init Failed: {e}")

    def _compute_hash(self, code_content: str, model_plan: PlanResult) -> str:
        """Return a cache key scoped to source, contract, prompt, and model."""
        cache_input = {
            "schema_version": _SUMMARY_CACHE_SCHEMA_VERSION,
            "prompt_version": _SUMMARY_PROMPT_VERSION,
            "model_id": self.model_id,
            "code_sha256": hashlib.sha256(code_content.encode("utf-8")).hexdigest(),
            "model_plan": model_plan.model_dump(mode="json"),
        }
        canonical_input = json.dumps(
            cache_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()

    def _load_from_cache(self, cache_key: str) -> Optional[StandardContextModel]:
        """[Changed] 从 SQLite 读取缓存 (并发安全)"""
        if not self.cache_db_path.exists():
            return None
        
        try:
            # check_same_thread=False 允许在不同线程使用连接（虽然这里是每次新建连接）
            # timeout=10 意味着如果数据库被锁，会等待10秒
            with sqlite3.connect(self.cache_db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT json_value FROM summary_cache WHERE hash_key = ?", (cache_key,))
                row = cursor.fetchone()
                
                if row:
                    print(f"✅ Cache Hit: {cache_key[:8]}...")
                    return StandardContextModel.model_validate_json(row[0])
        except Exception as e:
            print(f"⚠️ Cache read error: {e}")
        
        return None

    def _save_to_cache(self, cache_key: str, result: StandardContextModel):
        """[Changed] 写入 SQLite 缓存 (并发安全)"""
        try:
            json_str = result.model_dump_json()
            with sqlite3.connect(self.cache_db_path, timeout=10) as conn:
                # INSERT OR REPLACE 是原子操作：如果 key 存在则更新，不存在则插入
                conn.execute(
                    "INSERT OR REPLACE INTO summary_cache (hash_key, json_value, updated_at) VALUES (?, ?, ?)",
                    (cache_key, json_str, time.time())
                )
                conn.commit()
        except Exception as e:
            print(f"⚠️ Cache write error: {e}")

    def _extract_json_obj(self, content: str) -> dict:
        """Extract a JSON object from LLM response."""
        content = content.strip()
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        import re
        fence_match = re.search(r'```(?:json)?\s*({.*?})\s*```', content, re.DOTALL)
        if fence_match:
            try:
                data = json.loads(fence_match.group(1))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start:end+1])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not extract JSON object. Content: {content[:200]}")

    def forward(self, model_plan: PlanResult) -> StandardContextModel:
        """输入是这个模型的PlanResult信息结构（如果是Coupled Model，这个结构里面会维护好他的孩子的实际生成的信息，这个信息在总结的时候是重要的），正常输出是将其总结的结果返回回来"""
        full_path = self.working_directory / model_plan.model_info.file_path

        with open(full_path, "r", encoding="utf-8") as f:
            code_content = f.read()

        generated_interface = extract_generated_python_interface(
            code_content,
            model_plan.model_info.class_name,
            filename=str(full_path),
            child_class_names=(child.class_name for child in model_plan.children_plan),
        )

        cache_key = self._compute_hash(code_content, model_plan)
        cached_result = self._load_from_cache(cache_key)
        if cached_result:
            cached_result = cached_result.model_copy(
                update={
                    # Planning owns model identity. A summary—especially one
                    # loaded from an older cache—must never rename or relocate
                    # the generated file in the system registry.
                    "class_name": model_plan.model_info.class_name,
                    "file_path": model_plan.model_info.file_path,
                    "logic_path": model_plan.model_info.logic_path,
                    "generated_interface": generated_interface,
                }
            )
            self._save_to_cache(cache_key, cached_result)
            return cached_result

        sub_models_str = process_sub_models(
            sub_models=model_plan.children_plan,
            target_file_path=model_plan.model_info.file_path,
        )
        
        current_feedback = ""
        validated_data = None
        for i in range(5):
            try:
                feedback_str = f"## [Feedback] Previous attempt failed, here is the feedback: {current_feedback}\n" if current_feedback else ""
                prompt = SUMMARIZE_PROMPT_TEMPLATE.format(
                    code=code_content,
                    sub_models=sub_models_str,
                    feedback=feedback_str
                )
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase2_summarize",
                    target=model_plan.model_info.class_name,
                    attempt=i,
                    temperature=0.5,
                    response_format=DEVSModelExtraction,
                )

                result = get_content_strict(response)
                json_obj = self._extract_json_obj(result)
                validated_data = DEVSModelExtraction.model_validate(json_obj)
                break
            except Exception as e:
                print(f"Error occurred while processing {full_path}: {e}")
                current_feedback = f"{current_feedback}\n{str(e)}"

        if validated_data is None:
            raise Exception(f"Failed to summarize model at {full_path} after 5 attempts")

        # 4. 构建最终输出
        val_spec = validated_data.specification
        result = StandardContextModel(
            # The LLM summarizes behavior; the plan remains the sole authority
            # for stable model identity.
            class_name=model_plan.model_info.class_name,
            file_path=model_plan.model_info.file_path,
            logic_path=model_plan.model_info.logic_path,
            specification=ModelSpecification(
                function=val_spec.function,
                input_ports=val_spec.input_ports,
                output_ports=val_spec.output_ports,
                external_io=val_spec.external_io,
                model_init_args=val_spec.model_init_args,
            ),
            generated_interface=generated_interface,
        )

        self._save_to_cache(cache_key, result)

        return result
