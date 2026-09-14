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
    related_requirement_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Requirement-ledger IDs relevant to this module. The same ID may "
            "be associated with several cooperating modules."
        ),
    )


class RequirementSource(BaseModel):
    """A deterministic section of the unabridged user requirements."""

    id: str = Field(..., description="Stable source-section ID, such as S01")
    title: str = Field(..., description="Original section heading or a neutral fallback title")
    text: str = Field(..., description="Verbatim section text")


class RequirementFlow(BaseModel):
    """One explicitly required cross-module message or handoff."""

    producer: str = Field(description="Named producer role/module from the request")
    consumer: str = Field(description="Named receiver role/module from the request")
    producer_port: str = Field(
        default="",
        description="Explicit producer output-port name, or empty when unspecified",
    )
    consumer_port: str = Field(
        default="",
        description="Explicit receiver input-port name, or empty when unspecified",
    )
    payload: str = Field(default="", description="Message or payload meaning")
    trigger: str = Field(default="", description="Condition or timing of the handoff")


class RequirementItem(BaseModel):
    """One self-contained implementation obligation extracted from the request."""

    id: str = Field(..., description="Stable neutral requirement ID, such as R001")
    detail: str = Field(
        ...,
        description=(
            "Self-contained requirement including its conditions, timing, schema, "
            "and edge cases where applicable"
        ),
    )
    source_section_ids: list[str] = Field(
        default_factory=list,
        description="Whole source sections that establish this requirement",
    )
    flows: list[RequirementFlow] = Field(
        default_factory=list,
        description="Explicit cross-module flows contained in this requirement",
    )


class RequirementLedger(BaseModel):
    """Loss-aware navigation layer; never a replacement for the raw request."""

    sources: list[RequirementSource] = Field(default_factory=list)
    items: list[RequirementItem] = Field(default_factory=list)


class ProtocolSpec(BaseModel):
    initial_signal: str = Field(
        default="None",
        description=(
            "Startup interaction behavior of the port. Describe only whether the port actively sends "
            "or expects to receive a startup message. Do not prescribe payload content here; describe "
            "payload schema in the port structure and runtime protocol in the description."
        ),
    )
    description: str = Field(default="", description="Description of the protocol, including possible params")


class TypedEntity(BaseModel):
    name: str = Field(default="", description="Variable or port name. should be a valid Python identifier.")
    type: str = Field(
        default="str",
        description=(
            "Primitive Python type name: int, float, bool, str, dict, or list. "
            "The framework parent argument alone uses object. Put container "
            "element details in structure, not in this field."
        ),
    )
    structure: str = Field(default="", description="Structure of the data. For dict/list, detail the expected format.")


class PortEntity(TypedEntity):
    protocol: ProtocolSpec = Field(default_factory=ProtocolSpec, description="The protocol for this port.")


class ExternalIOStream(BaseModel):
    target: Literal["stdin", "stdout", "stderr", "file", "other"] = Field(
        default="stdout",
        description="External IO target outside DEVS ports. Use exactly one of: stdin, stdout, stderr, file, other.",
    )
    content: str = Field(
        default="",
        description=(
            "Complete external IO contract: content schema, source/derivation logic, timing, read/write direction, and any path/resource details."
        ),
    )


ImplementationMechanism = Literal[
    "autonomous_start",
    "periodic_internal_event",
    "zero_delay_response",
    "timed_processing",
    "buffering",
    "timeout_feedback",
    "external_jsonl",
    "stdin_schedule",
    "file_read",
    "file_write",
    # Accepted for compatibility with plans created before access direction was
    # split. Retrieval resolves this legacy tag using the external_io content.
    "file_io",
    "runtime_multiplicity",
]


# Stable identifiers for the complete atomic reference files shown during code
# generation.  Atomic detail planning selects one of these directly after
# reading the short behavior descriptions in its prompt.
AtomicImplementationExample = Literal[
    "final_periodic_state",
    "logged_exclusive_dispatch_queue",
    "logged_available_worker",
    "reactive_multiport_batch",
    "timed_payload_channel",
    "timed_then_zero_delay_reply",
    "multi_stage_timed_processor",
    "autonomous_timed_worker",
    "logged_delayed_channel",
    "logged_buffered_server",
    "logged_timeout_feedback_protocol",
    "autonomous_periodic_source",
    "logged_periodic_state_with_async_updates",
    "periodic_state_with_async_parameter_updates",
    "periodic_controller_with_async_setpoint",
    "reactive_zero_delay_router",
    "reactive_scalar_transform",
    "fifo_buffered_timed_service",
    "single_inflight_timed_service",
    "timeout_feedback_protocol",
    "jsonl_event_sink",
    "jsonl_state_sink",
    "jsonl_stateful_input_processor",
    "final_aggregate_sink",
    "stdin_jsonl_timestamped_event_source",
    "stdin_plaintext_timestamped_event_source",
    "stdin_preloaded_schedule_periodic_lookup",
    "stdin_startup_batch_source",
    "file_input_source",
    "file_record_sink",
]


class ModelSpecification(BaseModel):
    function: str = Field(default="", description="The Responsibility & Workflow & Logic.")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="External IO streams used by this model outside DEVS ports.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Parameters required to initialize the model class.")
    input_ports: list[PortEntity] = Field(default_factory=list, description="Data inputs received by this model.")
    output_ports: list[PortEntity] = Field(default_factory=list, description="Data outputs sent by this model.")
    implementation_mechanisms: list[ImplementationMechanism] = Field(
        default_factory=list,
        description=(
            "Legacy retrieval hints for plans created before direct example "
            "selection; new detailed plans use implementation_example."
        ),
    )
    implementation_example: AtomicImplementationExample | None = Field(
        default=None,
        description=(
            "One complete-file atomic implementation example selected during "
            "detailed planning from its behavioral description."
        ),
    )

    def to_llm_json(self) -> str:
        data_dict = self.model_dump(mode='json')
        return json.dumps(data_dict, ensure_ascii=False)


class GeneratedPythonInterface(BaseModel):
    """Public Python surface extracted deterministically from generated source."""

    instance_attributes: list[str] = Field(default_factory=list)
    properties: list[str] = Field(default_factory=list)
    public_methods: list[str] = Field(default_factory=list)
    child_instances: dict[str, str] = Field(default_factory=dict)


class StandardContextModel(BaseModel):
    class_name: str = Field(..., description="Name of the model class")
    file_path: Path = Field(..., description="Path of the model file in the hierarchy")
    logic_path: str = Field(..., description="Path of the model logic in the hierarchy")
    specification: ModelSpecification = Field(..., description="High-level requirements for this model.")
    generated_interface: GeneratedPythonInterface = Field(
        default_factory=GeneratedPythonInterface,
        description="Exact public Python surface extracted from generated source.",
    )

    def to_llm_json(self) -> str:
        data_dict = {
            "class_name": self.class_name,
            "file_path": str(self.file_path),
            "logic_path": self.logic_path,
            "specification": self.specification.to_llm_json(),
            "generated_interface": self.generated_interface.model_dump(mode="json"),
        }
        return json.dumps(data_dict, ensure_ascii=False)


class PlanResult(BaseModel):
    type: Literal["atomic", "coupled"] = Field(..., description="Type of the model")
    model_info: StandardContextModel = Field(..., description="Model information.")
    children_plan: list[StandardContextModel] = Field(default_factory=list, description="List of direct children sub-models.")
    coupling_rules: list[str] = Field(
        default_factory=list,
        description=(
            "One natural-language routing rule per item. Rules may describe fixed, "
            "repeated, indexed, conditional, EIC, IC, or EOC connections."
        ),
    )

    def to_llm_json(self) -> str:
        data_dict = {
            "type": self.type,
            "model_info": self.model_info.to_llm_json(),
            "children_plan": [child.to_llm_json() for child in self.children_plan],
            "coupling_rules": self.coupling_rules
        }
        return json.dumps(data_dict, ensure_ascii=False)


class StandardContext(BaseModel):
    logic_path: str = Field(..., description="The path of the model in the hierarchy.")
    original_project_requirements: str = Field(..., description="The original project requirements.")
    global_plan: list[GlobalPlanNode] = Field(default_factory=list, description="The structural global plan of the whole system.")
    ancestors: list[StandardContextModel] = Field(default_factory=list, description="List of ancestors' specifications.")
    siblings: list[StandardContextModel] = Field(default_factory=list, description="List of siblings' specifications.")
    requirement_ledger: Optional[RequirementLedger] = Field(
        default=None,
        description="Structured navigation over the unabridged project requirements.",
    )
    relevant_requirement_ids: list[str] = Field(
        default_factory=list,
        description="Requirement IDs relevant to the current model or coupled subtree.",
    )

    def to_llm_json(self) -> str:
        data_dict = {
            "logic_path": self.logic_path,
            "original_project_requirements": self.original_project_requirements,
            "global_plan": [node.model_dump() for node in self.global_plan], # 👇 序列化新增字段
            "ancestors": [ancestor.to_llm_json() for ancestor in self.ancestors],
            "siblings": [sibling.to_llm_json() for sibling in self.siblings],
            "requirement_ledger": (
                self.requirement_ledger.model_dump(mode="json")
                if self.requirement_ledger
                else None
            ),
            "relevant_requirement_ids": self.relevant_requirement_ids,
        }
        return json.dumps(data_dict, ensure_ascii=False)


class SubModelPlan(BaseModel):
    name: str = Field(..., description="Name of the sub-model")
    specification: ModelSpecification = Field(..., description="High-level requirements for this sub-model")


class CoupledDecomposition(BaseModel):
    children_plan: list[SubModelPlan] = Field(..., description="List of direct children sub-models.")
    coupling_rules: list[str] = Field(
        default_factory=list,
        description="One natural-language routing rule per item.",
    )


class DetailedPlan(BaseModel):
    """详细计划：每个节点的完整规格"""
    class_name: str = Field(..., description="Name of the model class")
    model_type: Literal["atomic", "coupled"] = Field(..., description="Type of the model")
    specification: ModelSpecification = Field(..., description="Full specification: function, external IO, DEVS ports, init args")
    coupling_rules: list[str] = Field(
        default_factory=list,
        description=(
            "One natural-language routing rule per item for the coupled model; "
            "empty for atomic models."
        ),
    )


class SimpleDetailedPlan(BaseModel):
    """
    简化详细计划：
    - model_init_args: 完整的 TypedEntity (name/type/structure)，子模型需要知道父模型提供什么
    - input_ports/output_ports: 完整的 PortEntity (name/type/structure/protocol)，用于对接
    - function/external_io: 简短描述即可
    - 没有 coupling_rules（coupling 需要在详细计划中基于子模型端口信息才能确定）
    """
    class_name: str = Field(..., description="Name of the model class")
    model_type: Literal["atomic", "coupled"] = Field(..., description="atomic or coupled")
    function: str = Field(..., description="Brief responsibility & workflow (1-2 sentences).")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="Brief external IO requirements.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Full init args with name/type/structure.")
    input_ports: list[PortEntity] = Field(default_factory=list, description="Full port definitions for interface matching.")
    output_ports: list[PortEntity] = Field(default_factory=list, description="Full port definitions for interface matching.")
    related_requirement_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Requirement-ledger IDs relevant to planning or implementing this "
            "child subtree. IDs may be shared across siblings."
        ),
    )


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
        coupling_rules=coupled_plan.coupling_rules
    )


def format_context_str(
    context: StandardContext,
    use_function: bool = False,
    use_external_io: bool = False,
    use_model_init_args: bool = False,
    use_ports: bool = False,
    use_path: bool = False,
    use_system_goal: bool = False,
    use_global_plan: bool = False,
    use_parent: bool = False,
    use_siblings: bool = False,
    use_brief_function: bool = False,
    use_parent_model_init_args: Optional[bool] = None,
    use_sibling_model_init_args: Optional[bool] = None,
) -> str:
    if not context:
        return "No external context provided (Root model or isolated)."

    path = context.logic_path
    ancestors = context.ancestors
    siblings = context.siblings
    project_goal = context.original_project_requirements
    brief_functions = {node.name: node.description for node in context.global_plan}
    parent_init_args = (
        use_model_init_args
        if use_parent_model_init_args is None
        else use_parent_model_init_args
    )
    sibling_init_args = (
        use_model_init_args
        if use_sibling_model_init_args is None
        else use_sibling_model_init_args
    )

    parent_info = "Root (No Parent)"
    if ancestors:
        parent = ancestors[-1]
        serialized_spec = parent.specification.model_dump(mode="json")
        p_reqs = {}
        if use_function: p_reqs["function"] = parent.specification.function
        elif use_brief_function:
            p_reqs["function"] = brief_functions.get(
                parent.class_name, parent.specification.function
            )
        if use_external_io:
            p_reqs["external_io"] = serialized_spec["external_io"]
        if parent_init_args:
            p_reqs["model_init_args"] = serialized_spec["model_init_args"]
        if use_ports:
            p_reqs["input_ports"] = serialized_spec["input_ports"]
            p_reqs["output_ports"] = serialized_spec["output_ports"]
        details = f": {json.dumps(p_reqs, ensure_ascii=False)}" if p_reqs else ""
        parent_info = f"Name: {parent.class_name}{details}"

    siblings_info = ""
    if siblings:
        for sib in siblings:
            serialized_spec = sib.specification.model_dump(mode="json")
            s_reqs = {}
            if use_function: s_reqs["function"] = sib.specification.function
            elif use_brief_function:
                s_reqs["function"] = brief_functions.get(
                    sib.class_name, sib.specification.function
                )
            if use_external_io:
                s_reqs["external_io"] = serialized_spec["external_io"]
            if sibling_init_args:
                s_reqs["model_init_args"] = serialized_spec["model_init_args"]
            if use_ports:
                s_reqs["input_ports"] = serialized_spec["input_ports"]
                s_reqs["output_ports"] = serialized_spec["output_ports"]
            details = f": {json.dumps(s_reqs, ensure_ascii=False)}" if s_reqs else ""
            siblings_info += f"   * {sib.class_name}{details}\n"
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
