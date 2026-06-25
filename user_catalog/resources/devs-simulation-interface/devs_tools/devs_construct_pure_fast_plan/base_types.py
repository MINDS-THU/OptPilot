from pydantic import BaseModel, Field
from typing import Literal, Optional, List
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
import json


class GlobalPlanNode(BaseModel):
    """全局初步计划中的单个模块节点（扁平list）"""
    name: str = Field(..., description="Module name, valid Python identifier")
    description: str = Field(..., description="Brief description of module functionality (1-2 sentences)")
    children_names: list[str] = Field(default_factory=list, description="List of direct child module names. Empty for atomic models.")


class ProtocolSpec(BaseModel):
    initial_state: str = Field(default="empty", description="The initial states of the port.")
    initial_signal: str = Field(default="None", description="The initial signal sent by the port.")
    description: str = Field(default="", description="Description of the protocol, including possible params")


class TypedEntity(BaseModel):
    name: str = Field(default="", description="Variable or port name. should be a valid Python identifier.")
    type: str = Field(default="str", description="Python type hint (e.g., 'int', 'str', 'List[int]').")
    structure: str = Field(default="", description="Structure of the data. For dict/list, detail the expected format.")


class PortEntity(TypedEntity):
    protocol: ProtocolSpec = Field(default_factory=ProtocolSpec, description="The protocol for this port.")


class ModelSpecification(BaseModel):
    function: str = Field(default="", description="The Responsibility & Workflow & Logic.")
    logging: str = Field(default="", description="logging requirements.")
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Parameters required to initialize the model class.")
    input_ports: list[PortEntity] = Field(default_factory=list, description="Data inputs received by this model.")
    output_ports: list[PortEntity] = Field(default_factory=list, description="Data outputs sent by this model.")

    def to_llm_json(self) -> str:
        data_dict = self.model_dump(mode='json')
        return json.dumps(data_dict, ensure_ascii=False)


class StandardContextModel(BaseModel):
    class_name: str = Field(..., description="Name of the model class")
    file_path: Path = Field(..., description="Path of the model file in the hierarchy")
    logic_path: str = Field(..., description="Path of the model logic in the hierarchy")
    specification: ModelSpecification = Field(..., description="High-level requirements for this model.")

    def to_llm_json(self) -> str:
        data_dict = {
            "class_name": self.class_name,
            "file_path": str(self.file_path),
            "logic_path": self.logic_path,
            "specification": self.specification.to_llm_json()
        }
        return json.dumps(data_dict, ensure_ascii=False)


class PlanResult(BaseModel):
    type: Literal["atomic", "coupled"] = Field(..., description="Type of the model")
    model_info: StandardContextModel = Field(..., description="Model information.")
    children_plan: list[StandardContextModel] = Field(default_factory=list, description="List of direct children sub-models.")
    coupling_specification: Optional[str] = Field(None, description="briefly describe how sub-models connect.")

    def to_llm_json(self) -> str:
        data_dict = {
            "type": self.type,
            "model_info": self.model_info.to_llm_json(),
            "children_plan": [child.to_llm_json() for child in self.children_plan],
            "coupling_specification": self.coupling_specification
        }
        return json.dumps(data_dict, ensure_ascii=False)


class StandardContext(BaseModel):
    logic_path: str = Field(..., description="The path of the model in the hierarchy.")
    original_project_requirements: str = Field(..., description="The original project requirements.")
    global_plan: list[GlobalPlanNode] = Field(default_factory=list, description="The structural global plan of the whole system.")
    ancestors: list[StandardContextModel] = Field(default_factory=list, description="List of ancestors' specifications.")
    siblings: list[StandardContextModel] = Field(default_factory=list, description="List of siblings' specifications.")

    def to_llm_json(self) -> str:
        data_dict = {
            "logic_path": self.logic_path,
            "original_project_requirements": self.original_project_requirements,
            "global_plan": [node.model_dump() for node in self.global_plan], # 👇 序列化新增字段
            "ancestors": [ancestor.to_llm_json() for ancestor in self.ancestors],
            "siblings": [sibling.to_llm_json() for sibling in self.siblings]
        }
        return json.dumps(data_dict, ensure_ascii=False)


class SubModelPlan(BaseModel):
    name: str = Field(..., description="Name of the sub-model")
    specification: ModelSpecification = Field(..., description="High-level requirements for this sub-model")


class CoupledDecomposition(BaseModel):
    children_plan: list[SubModelPlan] = Field(..., description="List of direct children sub-models.")
    coupling_specification: str = Field(..., description="briefly describe how sub-models connect.")


class DetailedPlan(BaseModel):
    """详细计划：每个节点的完整规格"""
    class_name: str = Field(..., description="Name of the model class")
    model_type: Literal["atomic", "coupled"] = Field(..., description="Type of the model")
    specification: ModelSpecification = Field(..., description="Full specification: function, logging, IO ports, init args")
    coupling_specification: Optional[str] = Field(None, description="Coupling logic (EIC/IC/EOC) for coupled models")


class SimpleDetailedPlan(BaseModel):
    """
    简化详细计划：
    - model_init_args: 完整的 TypedEntity (name/type/structure)，子模型需要知道父模型提供什么
    - input_ports/output_ports: 完整的 PortEntity (name/type/structure/protocol)，用于对接
    - function/logging: 简短描述即可
    - 没有 coupling_specification（coupling 需要在详细计划中基于子模型端口信息才能确定）
    """
    class_name: str = Field(..., description="Name of the model class")
    model_type: Literal["atomic", "coupled"] = Field(..., description="atomic or coupled")
    function: str = Field(..., description="Brief responsibility & workflow (1-2 sentences).")
    logging: str = Field(default="", description="Brief logging requirements.")
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Full init args with name/type/structure.")
    input_ports: list[PortEntity] = Field(default_factory=list, description="Full port definitions for interface matching.")
    output_ports: list[PortEntity] = Field(default_factory=list, description="Full port definitions for interface matching.")


@dataclass
class PlanTreeNode:
    model_info: StandardContextModel
    plan: PlanResult
    context: StandardContext
    libs_dir: Path
    children: list['PlanTreeNode']
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
    use_global_plan: bool = False,
    use_parent: bool = False,
    use_siblings: bool = False,
) -> str:
    if not context:
        return "No external context provided (Root model or isolated)."

    path = context.logic_path
    ancestors = context.ancestors
    siblings = context.siblings
    project_goal = context.original_project_requirements

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
    if use_global_plan and context.global_plan:
        gp_str = "\n".join([
            f"- {n.name}: {n.description} (children: {', '.join(n.children_names) if n.children_names else 'none'})" 
            for n in context.global_plan
        ])
        results.append(f"**System Architecture (Global Plan)**:\n{gp_str}\n")
    if use_system_goal: results.append(f"**System Goal**: {project_goal}\n")
    if use_parent: results.append(f"**Parent**: {parent_info}\n")
    if use_siblings: results.append(f"**Siblings**: \n{siblings_info}")

    return "".join(results)
