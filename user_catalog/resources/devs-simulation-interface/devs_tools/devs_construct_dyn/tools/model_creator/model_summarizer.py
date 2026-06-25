from smolagents import Tool
from pathlib import Path
from typing import List, Optional, Tuple, Union
from pydantic import BaseModel, Field, ValidationError
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

# ==============================================================================
# PYDANTIC MODELS (用于结构定义和验证)
# ==============================================================================

class LoggingInfo(BaseModel):
    log_type: str = Field(..., description="The type of logging")
    msg: str = Field(..., description="The detailed structure of main msg")
    description: str = Field(..., description="Describe the meaning & timing of the logging")

class TempModelSpec(BaseModel):
    function: str = Field(..., description="The high-level Responsibility & Workflow & Logic. ")
    model_init_args: list[TypedEntity] = Field(
        default_factory=list, 
        description="Parameters required to initialize the model class (e.g., initial_count, processing_time)."
    )
    input_ports: list[PortEntity] = Field(
        default_factory=list, 
        description="Data inputs received by this model."
    )
    output_ports: list[PortEntity] = Field(
        default_factory=list, 
        description="Data outputs sent by this model."
    )
    
    logging: list[LoggingInfo] = Field(..., description="The logging information of the model")

class DEVSModelExtraction(BaseModel):
    class_name: str = Field(..., description="The name of the Python class")
    specification: TempModelSpec
    analysis_error: Optional[str] = Field(None, description="If there are issues with the analysis (especially when you can't determine the arguments), this field will be populated with a message to better guide the code generator to generate the code correctly.")

# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
SUMMARIZE_PROMPT_TEMPLATE = """
## [Task]
Analyze the provided Python code for a DEVS model.
Extract the model's metadata into a strict structure based on the schema below. If the code is not clear or ambiguous, return an error message in the `analysis_error` field.

## [Rules]
- **model_init_args**: You must extract all the arguements from the `__init__` method (except `self`). You should make sure that the usage of all args are clear (especially for the str, dict types, and possible options). Fill `analysis_error` if:
  - the `__init__` method uses `*args`, `**kwargs`, or an ambiguous dict / object. 
  - the str representing things like `option` or `strategy`, but no clear option list is provided.
  - any other ambiguity.
- **logging**: Look for the docstring and logging usage to fill `logging`. You must clearly state the `log_type` and the structure of main msg. 
  - You only need to extract the logger used in this model. The logging inside sub-models are not needed, as they are handled by their own models.
  - If it is described in the docstring, just copy the releted part in docstring, you can add more details if needed. (e.g. to keep the name of data clear, or other needs. )
- **ports**: Refer to the docstring for port definitions. 
  - Fill the `analysis_error` if: 
    - the docstring is missing or unclear; 
    - the docstring does not match the actual code; 
    - any other ambiguity.
- **function**: Look for the main logic of the model. For communication protocols, find how it starts, send, receive, and ends(if any). 
  - For coupled models, you must copy the submodels described in the docstring, especially the relation between the class name and the instance name.

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
        """计算唯一标识 Hash"""
        plan_str = model_plan.model_dump_json() 
        combined_content = code_content + plan_str
        return hashlib.sha256(combined_content.encode('utf-8')).hexdigest()

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

    def forward(self, model_plan: PlanResult) -> StandardContextModel | str:
        """输入是这个模型的PlanResult信息结构（如果是Coupled Model，这个结构里面会维护好他的孩子的实际生成的信息，这个信息在总结的时候是重要的），正常输出是将其总结的结果返回回来；如果出现报错，返回会是一个json字符串，里面包含这些key: error表示错误类型，details表示错误详情，path表示该文件的地址"""
        full_path = self.working_directory / model_plan.model_info.file_path
        
        if not full_path.exists():
            return json.dumps({
                "error": f"File not found at {full_path}",
                "details": "",
                "path": str(model_plan.model_info.file_path)
            })

        with open(full_path, "r", encoding="utf-8") as f:
            code_content = f.read()

        cache_key = self._compute_hash(code_content, model_plan)
        cached_result = self._load_from_cache(cache_key)
        if cached_result:
            return cached_result

        sub_models_str = process_sub_models(
            sub_models=model_plan.children_plan,
            target_file_path=model_plan.model_info.file_path,
        )
        
        current_feedback = ""
        for i in range(5):
            try:
                feedback_str = f"## [Feedback] Previous attempt failed, here is the feedback: {current_feedback}\n" if current_feedback else ""
                prompt = SUMMARIZE_PROMPT_TEMPLATE.format(
                    code=code_content,
                    sub_models=sub_models_str,
                    feedback=feedback_str
                )
                response = completion(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    response_format=DEVSModelExtraction 
                )
                
                # LiteLLM/OpenAI 保证返回符合结构的 JSON 字符串
                result = get_content_strict(response)
                
                # 二次校验与对象转换
                validated_data = DEVSModelExtraction.model_validate_json(result)
                break
            except Exception as e:
                print(f"Error occurred while processing {full_path}: {e}")
                current_feedback = f"{current_feedback}\n{str(e)}"

        # 3. 业务逻辑检查 (Check for Analysis Error)
        if validated_data.analysis_error:
            return json.dumps({
                "error": "Analysis Failed",
                "details": validated_data.analysis_error,
                "path": str(model_plan.model_info.file_path)
            })

        # 4. 构建最终输出
        val_spec = validated_data.specification
        result = StandardContextModel(
            class_name=validated_data.class_name,
            file_path=model_plan.model_info.file_path,
            logic_path=model_plan.model_info.logic_path,
            specification=ModelSpecification(
                function=val_spec.function,
                input_ports=val_spec.input_ports,
                output_ports=val_spec.output_ports,
                logging=json.dumps([i.model_dump(mode='json') for i in val_spec.logging]),
                model_init_args=val_spec.model_init_args,
            ),
        )

        self._save_to_cache(cache_key, result)

        return result