from smolagents import Tool
import os
from pathlib import Path
import yaml
import time
import litellm
from litellm import completion
import json
from dataclasses import dataclass
litellm.drop_params = True
from ...base_types import PlanResult, StandardContext, StandardContextModel, format_context_str
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging

from .unified_model_skill import select_skills, CreatorSkill
from .unified_model_prompt import (
    GLOBAL_STANDARDS,
    ATOMIC_INSTRUCTIONS,
    COUPLED_INSTRUCTIONS,
    MAIN_PROMPT_TEMPLATE,
)

import ast
import re

def extract_xml_code(text):
    start_tag = "<python_code>"
    end_tag = "</python_code>"
    
    if start_tag in text and end_tag in text:
        # rindex 找最后一个开始标签（防止模型输出了多个版本）
        start_index = text.rindex(start_tag) + len(start_tag)
        end_index = text.find(end_tag, start_index)
        code = text[start_index:end_index].strip()
        ast.parse(code)
        compile(code, "<generated_model>", "exec")
        return code
    raise ValueError("No <python_code> tags found")

def process_sub_models(sub_models: list[StandardContextModel], target_file_path: Path) -> str:
    """Calculates relative paths for imports if sub_models_info is provided. And formulates the sub_models_info into a string."""
    if not sub_models or sub_models is None:
        return "N/A"
    target_file_path = Path(target_file_path)
        
    try:
        target_dir = target_file_path.parent
        all_sm = [sm.model_dump() for sm in sub_models]
        for sm in all_sm:
            sm_path = Path(sm["file_path"])
            rel_path = os.path.relpath(str(sm_path), str(target_dir))
            sm['relative_file_path'] = rel_path.replace("\\", "/")
            sm.pop("file_path")
            sm.pop("logic_path")
        
        return json.dumps(all_sm)
    except Exception as e:
        print(f"Warning: Failed to process sub-models info: {e}")
        return json.dumps([sm.model_dump_json() for sm in sub_models])


# \==============================================================================

TYPE_TO_CLASS_NAME = {
    "atomic": "Atomic",
    "coupled": "Coupled",
}


class ModelCreator:
    def __init__(self, model_id: str, working_directory: str = "./working_dir"):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)
        
        # Define material paths
        self.tool_dir = Path(__file__).parent.parent.parent
        print(f"Tool directory: {self.tool_dir}")
        self.util_desc_file = self.tool_dir / "materials/util_desc.yaml"
        self.definitions_files = {
            "atomic": self.tool_dir / "materials/definitions_atomic_fast.md",
            "coupled": self.tool_dir / "materials/definitions_coupled_fast.md",
        }
        self.injected_utils = ["get_current_time"]
        
        # Example files map
        self.examples_map = {
            "atomic": [
                self.tool_dir / "materials/devs_project/atomic_example_fast.py",
            ],
            "coupled": [
                self.tool_dir / "materials/devs_project/coupled_example_fast.py",
            ]
        }

    def _read_materials(self, model_type: str):
        example_content = ""
        definitions_content = ""
        util_desc = ""
        
        # Load Examples based on type
        target_examples = self.examples_map.get(model_type, [])
        for example_file in target_examples:
            if example_file.exists():
                with open(example_file, "r") as f:
                    content = f.read()
                    example_content += f"```python\n{content}\n```\n"
        
        # Load Definitions
        definitions_file = self.definitions_files.get(model_type, None)
        if definitions_file:
            definitions_file = Path(definitions_file)
            if definitions_file.exists():
                with open(definitions_file, "r") as f:
                    definitions_content = f.read()
        
        # Load Utils
        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"
        
        print(f"length of example_content: {len(example_content)}, definitions_content: {len(definitions_content)}, util_desc: {len(util_desc)}")
        
        return example_content, definitions_content, util_desc

    def _format_selected_skills(self, skills: list[CreatorSkill]) -> str:
        if not skills:
            return "(No optional creator skills selected for this model.)"
        names = ", ".join(skill.name for skill in skills)
        prompts = "\n\n".join(skill.prompt.strip() for skill in skills)
        return f"Selected skills: {names}\n\n{prompts}"

    def forward(self, model_plan: PlanResult, context: StandardContext, feedback: str) -> str:

        if model_plan.type not in ["atomic", "coupled"]:
            return f"FAILURE: Invalid model_type '{model_plan.type}'. Must be 'atomic' or 'coupled'."

        # Prepare Materials
        example_code, definitions, util_desc = self._read_materials(model_plan.type)
        
        # Select Specific Instructions
        specific_instructions = ATOMIC_INSTRUCTIONS if model_plan.type == "atomic" else COUPLED_INSTRUCTIONS
        
        # Process Sub-models (Coupled Only logic applied via Utils, but safe to run for both)
        processed_sub_models = process_sub_models(model_plan.children_plan, model_plan.model_info.file_path)

        context_str = format_context_str(context, use_path=True, use_parent=True, use_siblings=True, use_global_plan=True)

        # Build Prompt
        model_spec = model_plan.model_info.specification.to_llm_json()
        if model_plan.coupling_specification:
            model_spec += (
                "\n**Coupling Specification (planned topology reference only; actual generated "
                "child interfaces in [Sub-Models] / [Context Info] take precedence)**:\n"
                f"{model_plan.coupling_specification}\n"
            )
            
        # prepare optional creator skills
        selected_skills = select_skills(model_plan, context)
        print(f"[ModelCreator] Selected skills for {model_plan.model_info.class_name}: {[s.name for s in selected_skills]}")
        model_skills = self._format_selected_skills(selected_skills)
            
        prompt = MAIN_PROMPT_TEMPLATE.format(
            model_type=TYPE_TO_CLASS_NAME[model_plan.type],
            name=model_plan.model_info.class_name,
            global_standards=GLOBAL_STANDARDS,
            model_specific_instructions=specific_instructions,
            sub_models=processed_sub_models,
            spec=model_spec,
            definitions=definitions,
            example=example_code,
            util_desc=util_desc,
            context_str=context_str,
            feedback=feedback,
            model_skills=model_skills,
        )

        full_path = self.working_directory / model_plan.model_info.file_path
        
        last_fail_info = ""
        for attempt in range(5):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase2_code_generation",
                    target=model_plan.model_info.class_name,
                    attempt=attempt,
                    temperature=0.5
                )
                code = get_content_strict(response)
                
                code = extract_xml_code(code)
                
                full_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)
                
                return f"SUCCESS: {model_plan.type} model '{model_plan.model_info.class_name}' created at '{full_path}'."
                
            except Exception as e:
                last_fail_info = f"FAILURE: Error creating {model_plan.type} model '{model_plan.model_info.class_name}'. Reason: {str(e)}"
                print(f"Attempt {attempt + 1} failed: {str(e)}")
                
        return last_fail_info
