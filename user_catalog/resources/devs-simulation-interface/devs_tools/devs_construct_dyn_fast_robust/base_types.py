from pydantic import BaseModel, Field
from typing import Literal, Optional
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
from pydantic import model_validator
import json

class RequirementEscalation(Exception):
    """Phase 1: 子节点向父节点抗议，要求提供资源"""
    def __init__(self, complaint: str, source_path: str):
        self.complaint = complaint
        self.source_path = source_path
    
class ProtocolSpec(BaseModel):
    initial_state: str = Field(..., description="The initial states of the port. e.g. if it has any credits / tokens")
    initial_signal: str = Field(..., description="The initial signal sent by the port. If no signals, just say no. ")
    description: str = Field(..., description="Description of the protocol, including possible params")
    
class TypedEntity(BaseModel):
    """
    Defines a specific argument, input port, or output port.
    """
    name: str = Field(..., description="Variable or port name. should be a valid Python identifier.")
    type: str = Field(
        ..., 
        description="Python type hint (e.g., 'int', 'str', 'List[int]', 'Dict[str, float]'). Keep structures simple."
    )
    structure: str = Field(
        ..., 
        description="Structure of the data. CRITICAL: If 'type' is a complex structure (like dict or list), "
                    "you MUST detail the expected format, keys, and value constraints here."
    )
    
class PortEntity(TypedEntity):
    """
    Defines a specific argument, input port, or output port.
    """
    protocol: ProtocolSpec = Field(
        ...,
        description="The protocol for this port. Including initiation and data exchange."
    )
    
class LogEntity(BaseModel):
    key: str = Field(..., description="the name of the key")
    value: str = Field(..., description="the type, structure, and description of value")

class LogEntry(BaseModel):
    dict_content: list[LogEntity] = Field(..., description="The key-value pairs of the log")
    extra_info: str = Field(..., description="The additional infomation, including unspecified content, log timing, etc.")

class LogContent(BaseModel):
    detailed: list[LogEntry] = Field(..., description="The log entries with structure specifically stated")
    general: str = Field(..., description="Logging requirements that are not detailed specified")

class ModelSpecification(BaseModel):
    """
    Detailed functional specification and Interface definition.
    Used by both Atomic and Coupled models to define their EXTERNAL behavior and ports.
    """
    function: str = Field(..., description="The Responsibility & Workflow & Logic, as well as user specified details.")
    logging: str = Field(..., description="logging requirements. e.g. What specific data should be logged for debugging/analysis.")
    
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
    def to_llm_json(self) -> str:
        """
        生成专门给 LLM 看的精简版 JSON。
        剔除了 ProtocolSpec 中冗余的 pattern, role, semantic_type 字段。
        """
        # 定义需要排除的字段结构
        # '__all__' 表示应用到 list 中的每一项
        # exclude_rules = {
        #     'input_ports': {
        #         '__all__': {
        #             'protocol': {'pattern': True, 'role': True, 'semantic_type': True}
        #         }
        #     },
        #     'output_ports': {
        #         '__all__': {
        #             'protocol': {'pattern': True, 'role': True, 'semantic_type': True}
        #         }
        #     }
        # }
        # data_dict = self.model_dump(exclude=exclude_rules, mode='json')
        data_dict = self.model_dump(mode='json')
        return json.dumps(data_dict, ensure_ascii=False)
    
class StandardContextModel(BaseModel):
    """对于一个模型的生成要求；或者该模型的实际信息"""
    class_name: str = Field(..., description="Name of the model class")
    file_path: Path = Field(..., description="Path of the model file in the hierarchy")
    logic_path: str = Field(..., description="Path of the model logic in the hierarchy")
    specification: ModelSpecification = Field(..., description="High-level requirements for this model.")
    def to_llm_json(self) -> str:
        """剔除 ProtocolSpec 中冗余的 pattern, role, semantic_type 字段"""
        data_dict = {
            "class_name": self.class_name,
            "file_path": str(self.file_path),
            "logic_path": self.logic_path,
            "specification": self.specification.to_llm_json()
        }
        return json.dumps(data_dict, ensure_ascii=False)

class PlanResult(BaseModel):
    """模型生成计划；或者该模型的实际生成结果"""
    type: Literal["atomic", "coupled"] = Field(..., description="Type of the model")
    model_info: StandardContextModel = Field(..., description="Model information.")
    children_plan: list[StandardContextModel] = Field(
        default_factory=list, description="List of direct children sub-models."
    )
    coupling_specification: Optional[str] = Field(
        None, description="briefly describe how sub-models connect. The details are in children_plan."
    )
    def to_llm_json(self) -> str:
        """剔除 ProtocolSpec 中冗余的 pattern, role, semantic_type 字段"""
        data_dict = {
            "type": self.type,
            "model_info": self.model_info.to_llm_json(),
            "children_plan": [child.to_llm_json() for child in self.children_plan],
            "coupling_specification": self.coupling_specification
        }
        return json.dumps(data_dict, ensure_ascii=False)
    
class StandardContext(BaseModel):
    """
    Standard context for a model.
    """
    logic_path: str = Field(
        ..., description="The path of the model in the hierarchy. e.g., 'root.sub_model1.sub_model2'"
    )
    original_project_requirements: str = Field(
        ..., description="The original project requirements that this model is designed to fulfill. plain text. "
    )
    ancestors: list[StandardContextModel] = Field(
        default_factory=list, description="List of ancestors' specifications."
    )
    siblings: list[StandardContextModel] = Field(
        default_factory=list, description="List of siblings' specifications."
    )
    def to_llm_json(self) -> str:
        """剔除 ProtocolSpec 中冗余的 pattern, role, semantic_type 字段"""
        data_dict = {
            "logic_path": self.logic_path,
            "original_project_requirements": self.original_project_requirements,
            "ancestors": [ancestor.to_llm_json() for ancestor in self.ancestors],
            "siblings": [sibling.to_llm_json() for sibling in self.siblings]
        }
        return json.dumps(data_dict, ensure_ascii=False)

class SubModelPlan(BaseModel):
    name: str = Field(..., description="Name of the sub-model")
    specification: ModelSpecification = Field(..., description="High-level requirements for this sub-model")

class CoupledDecomposition(BaseModel):
    children_plan: list[SubModelPlan] = Field(
        ..., description="List of direct children sub-models."
    )
    coupling_specification: str = Field(
        ..., description="briefly describe how sub-models connect. The details are in children_plan."
    )

@dataclass
class PlanTreeNode:
    model_info: StandardContextModel
    plan: PlanResult
    context: StandardContext
    libs_dir: Path
    children: list['PlanTreeNode']
    # Phase 2 产物
    constructed_model: Optional[StandardContextModel] = None


def sub_model_plan_to_standard_context_model(sub_model_plan: SubModelPlan, parent_model_info: StandardContextModel) -> StandardContextModel:
    libs_dir = parent_model_info.file_path.parent / f"{parent_model_info.class_name}_libs"
    return StandardContextModel(
        class_name=sub_model_plan.name,
        file_path=libs_dir / sub_model_plan.name,
        logic_path=f"{parent_model_info.logic_path}.{sub_model_plan.name}",
        specification=sub_model_plan.specification
    )
    
def coupled_plan_to_plan_result(coupled_plan: CoupledDecomposition, model_info: StandardContextModel) -> PlanResult:
    return PlanResult(
        type="coupled",
        model_info=model_info,
        children_plan=[
            sub_model_plan_to_standard_context_model(child_plan, model_info)
            for child_plan in coupled_plan.children_plan
        ],
        coupling_specification=coupled_plan.coupling_specification
    )

def format_context_str(
    context: StandardContext,
    
    use_function: bool = False,
    use_logging: bool = False,
    use_model_init_args: bool = False,
    use_ports: bool = False,
    
    use_path: bool = False,
    use_system_goal: bool = False,
    use_parent: bool = False,
    use_siblings: bool = False,
) -> str:
    """Helper to format context dictionary into a readable string for Prompts."""
    if not context:
        return "No external context provided (Root model or isolated)."
        
    path = context.logic_path
    ancestors = context.ancestors
    siblings = context.siblings 
    project_goal = context.original_project_requirements
    
    # Format Parent
    parent_info = "Root (No Parent)"
    if ancestors:
        parent = ancestors[-1]
        p_reqs = {}
        if use_function: p_reqs["function"] = parent.specification.function
        if use_logging: p_reqs["logging"] = parent.specification.logging
        if use_model_init_args: p_reqs["model_init_args"] = parent.specification.model_init_args
        if use_ports:
            p_reqs["input_ports"] = parent.specification.input_ports
            p_reqs["output_ports"] = parent.specification.output_ports
        parent_info = f"Name: {parent.class_name}: {p_reqs}"
    
    # Format Siblings
    siblings_info = ""
    if siblings:
        for sib in siblings:
            s_reqs = {}
            if use_function: s_reqs["function"] = sib.specification.function
            if use_logging: s_reqs["logging"] = sib.specification.logging
            if use_model_init_args: s_reqs["model_init_args"] = sib.specification.model_init_args
            if use_ports:
                s_reqs["input_ports"] = sib.specification.input_ports
                s_reqs["output_ports"] = sib.specification.output_ports
            siblings_info += f"   * {sib.class_name}: {s_reqs}\n"
    else:
        siblings_info = "   (No Siblings)"

    results = []
    if use_path: results.append(f"**Current Path**: {path}\n")
    if use_system_goal: results.append(f"**System Goal**: {project_goal}\n")
    if use_parent: results.append(f"**Parent**: {parent_info}\n")
    if use_siblings: results.append(f"**Siblings**: \n{siblings_info}")

    return "".join(results)