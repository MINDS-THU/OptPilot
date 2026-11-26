from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional, List
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
import hashlib
import json
import keyword
import re


class GlobalPlanNode(BaseModel):
    """全局初步计划中的单个模块节点（扁平list）"""
    name: str = Field(..., description="Module name, valid Python identifier")
    description: str = Field(..., description="Brief description of module functionality (1-2 sentences)")
    children_names: list[str] = Field(default_factory=list, description="List of direct child module names. Empty for atomic models.")


class ProtocolSpec(BaseModel):
    initial_state: str = Field(default="empty", description="The initial states of the port.")
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
    type: str = Field(default="str", description="Python type hint (e.g., 'int', 'str', 'List[int]').")
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


class ModelSpecification(BaseModel):
    function: str = Field(default="", description="The Responsibility & Workflow & Logic.")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="External IO streams used by this model outside DEVS ports.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Parameters required to initialize the model class.")
    input_ports: list[PortEntity] = Field(default_factory=list, description="Data inputs received by this model.")
    output_ports: list[PortEntity] = Field(default_factory=list, description="Data outputs sent by this model.")

    def to_llm_json(self) -> str:
        data_dict = self.model_dump(mode='json')
        return json.dumps(data_dict, ensure_ascii=False)


class GeneratedPythonInterface(BaseModel):
    """Public Python surface extracted deterministically from generated source."""

    instance_attributes: list[str] = Field(
        default_factory=list,
        description="Public attributes assigned directly on self by the generated class.",
    )
    properties: list[str] = Field(
        default_factory=list,
        description="Public @property names declared by the generated class.",
    )
    public_methods: list[str] = Field(
        default_factory=list,
        description="Public methods declared directly by the generated class.",
    )
    child_instances: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Public self attribute to a directly constructed child or homogeneous child "
            "collection found deterministically in generated source."
        ),
    )


class StandardContextModel(BaseModel):
    class_name: str = Field(..., description="Name of the model class")
    file_path: Path = Field(..., description="Path of the model file in the hierarchy")
    logic_path: str = Field(..., description="Path of the model logic in the hierarchy")
    specification: ModelSpecification = Field(..., description="High-level requirements for this model.")
    generated_interface: GeneratedPythonInterface = Field(
        default_factory=GeneratedPythonInterface,
        description="Exact public Python surface extracted from generated source, when available.",
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
    specification: ModelSpecification = Field(..., description="Full specification: function, external IO, DEVS ports, init args")
    coupling_specification: Optional[str] = Field(None, description="Coupling logic (EIC/IC/EOC) for coupled models")


class SimpleDetailedPlan(BaseModel):
    """
    简化详细计划：
    - model_init_args: 完整的 TypedEntity (name/type/structure)，子模型需要知道父模型提供什么
    - input_ports/output_ports: 完整的 PortEntity (name/type/structure/protocol)，用于对接
    - function/external_io: 简短描述即可
    - 没有 coupling_specification（coupling 需要在详细计划中基于子模型端口信息才能确定）
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


@dataclass
class PlanTreeNode:
    model_info: StandardContextModel
    plan: PlanResult
    context: StandardContext
    libs_dir: Path
    children: list['PlanTreeNode']
    constructed_model: Optional[StandardContextModel] = None


class PlanArtifactNode(BaseModel):
    """Serializable form of one exact planning-tree node.

    ``PlanTreeNode`` remains the mutable runtime object used while source is
    generated.  This Pydantic counterpart contains only the completed planning
    state, so it can safely cross a user-confirmation boundary and later create
    a fresh runtime tree without sharing construction mutations.
    """

    model_info: StandardContextModel
    plan: PlanResult
    context: StandardContext
    libs_dir: Path
    children: list["PlanArtifactNode"] = Field(default_factory=list)

    @classmethod
    def from_plan_tree(cls, node: PlanTreeNode) -> "PlanArtifactNode":
        return cls(
            model_info=node.model_info,
            plan=node.plan,
            context=node.context,
            libs_dir=node.libs_dir,
            children=[cls.from_plan_tree(child) for child in node.children],
        )

    def to_plan_tree(self) -> PlanTreeNode:
        """Return a new mutable construction tree for one build attempt."""

        return PlanTreeNode(
            model_info=self.model_info.model_copy(deep=True),
            plan=self.plan.model_copy(deep=True),
            context=self.context.model_copy(deep=True),
            libs_dir=Path(self.libs_dir),
            children=[child.to_plan_tree() for child in self.children],
            constructed_model=None,
        )


class PlanGraphPort(BaseModel):
    name: str
    direction: Literal["input", "output"]
    type: str = "str"
    structure: str = ""


class PlanGraphNode(BaseModel):
    id: str
    name: str
    model_type: Literal["atomic", "coupled"]
    parent_id: Optional[str] = None
    responsibility: str = ""
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PlanGraphPort] = Field(default_factory=list)
    output_ports: list[PlanGraphPort] = Field(default_factory=list)


class PlanGraphContainment(BaseModel):
    parent_id: str
    child_id: str


class PlanGraphEndpoint(BaseModel):
    node_id: str
    port_name: str
    boundary: Literal["model", "parent_input", "parent_output"] = "model"


class PlanGraphCoupling(BaseModel):
    owner_node_id: str
    coupling_type: Literal["EIC", "IC", "EOC"]
    source: PlanGraphEndpoint
    target: PlanGraphEndpoint
    multiplicity: int = Field(default=1, ge=1)


class PlanGraph(BaseModel):
    """Bounded, deterministic projection of a proposed DEVS structure.

    Only strict ``endpoint -> endpoint`` coupling lines whose model and port
    names resolve against the plan are included.  The review UI can therefore
    render this object without interpreting free-form LLM text.  An omitted
    count explicitly signals that the graph is incomplete instead of silently
    inventing a connection.
    """

    root_node_id: str
    nodes: list[PlanGraphNode]
    containment: list[PlanGraphContainment] = Field(default_factory=list)
    couplings: list[PlanGraphCoupling] = Field(default_factory=list)
    omitted_coupling_count: int = Field(default=0, ge=0)

    @property
    def is_complete(self) -> bool:
        return self.omitted_coupling_count == 0


def _canonical_relative_folder(value: Path) -> Path:
    value = Path(value)
    if (
        value == Path(".")
        or not value.parts
        or value.is_absolute()
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise ValueError("Plan artifact folders must be canonical relative paths")
    return value


def build_structure_graph(global_plan: list[GlobalPlanNode]) -> PlanGraph:
    """Project an approved hierarchy without inventing ports or couplings."""

    parent_by_name: dict[str, str] = {}
    containment: list[PlanGraphContainment] = []
    for node in global_plan:
        for child_name in node.children_names:
            parent_by_name[child_name] = node.name
            containment.append(
                PlanGraphContainment(parent_id=node.name, child_id=child_name)
            )

    return PlanGraph(
        root_node_id=global_plan[0].name if global_plan else "",
        nodes=[
            PlanGraphNode(
                id=node.name,
                name=node.name,
                model_type="coupled" if node.children_names else "atomic",
                parent_id=parent_by_name.get(node.name),
                responsibility=node.description,
            )
            for node in global_plan
        ],
        containment=containment,
        couplings=[],
        omitted_coupling_count=0,
    )


class StructurePlanArtifact(BaseModel):
    """Deterministic, reviewable component hierarchy approved by the user.

    This artifact deliberately stops at component identity, parent/child
    containment, model kind, and responsibility.  Ports, protocols, and
    couplings are derived privately only after this outline is approved.
    """

    schema_version: Literal["devs.structure-plan.v1"] = "devs.structure-plan.v1"
    review_scope: Literal["component_hierarchy"] = "component_hierarchy"
    connections_defined: Literal[False] = False
    root_model_name: str
    requirements: str
    project_folder: Path
    devs_project_folder: Path
    global_plan: list[GlobalPlanNode]
    graph: PlanGraph

    @field_validator("project_folder", "devs_project_folder")
    @classmethod
    def _relative_safe_folder(cls, value: Path) -> Path:
        return _canonical_relative_folder(value)

    @model_validator(mode="after")
    def _validate_hierarchy(self) -> "StructurePlanArtifact":
        if self.devs_project_folder != self.project_folder / "devs_project":
            raise ValueError("devs_project_folder must be project_folder/devs_project")
        if not self.global_plan:
            raise ValueError("Structure plan must contain at least one component")
        if self.global_plan[0].name != self.root_model_name:
            raise ValueError("Structure plan root must be the first component")

        names = [node.name for node in self.global_plan]
        if len(names) != len(set(names)):
            raise ValueError("Structure plan component names must be unique")
        for name in names:
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"Unsafe structure component name: {name}")

        known_names = set(names)
        parents: dict[str, list[str]] = {name: [] for name in names}
        for node in self.global_plan:
            if len(node.children_names) != len(set(node.children_names)):
                raise ValueError(
                    f"Structure component '{node.name}' repeats a child"
                )
            for child_name in node.children_names:
                if child_name not in known_names:
                    raise ValueError(
                        f"Unknown child '{child_name}' of '{node.name}'"
                    )
                if child_name == node.name:
                    raise ValueError(
                        f"Structure component '{node.name}' cannot contain itself"
                    )
                parents[child_name].append(node.name)

        if parents[self.root_model_name]:
            raise ValueError("Structure plan root cannot have a parent")
        for name in names[1:]:
            if len(parents[name]) != 1:
                raise ValueError(
                    f"Structure component '{name}' must have exactly one parent"
                )

        children_by_name = {
            node.name: list(node.children_names) for node in self.global_plan
        }
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("Structure plan hierarchy contains a cycle")
            if name in visited:
                return
            visiting.add(name)
            for child_name in children_by_name[name]:
                visit(child_name)
            visiting.remove(name)
            visited.add(name)

        visit(self.root_model_name)
        if visited != known_names:
            missing = ", ".join(sorted(known_names - visited))
            raise ValueError(
                f"Structure plan contains components outside the root hierarchy: {missing}"
            )

        expected_graph = build_structure_graph(self.global_plan)
        if expected_graph.model_dump(mode="json") != self.graph.model_dump(
            mode="json"
        ):
            raise ValueError("Structure plan graph does not match its hierarchy")
        return self

    def to_serializable_dict(self) -> dict:
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_serializable_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PlanArtifact(BaseModel):
    """Exact detailed plan derived privately from an approved structure."""

    schema_version: Literal["devs.plan-artifact.v1"] = "devs.plan-artifact.v1"
    root_model_name: str
    requirements: str
    project_folder: Path
    devs_project_folder: Path
    approved_structure_digest: str
    root: PlanArtifactNode
    graph: PlanGraph

    @field_validator("project_folder", "devs_project_folder")
    @classmethod
    def _relative_safe_folder(cls, value: Path) -> Path:
        return _canonical_relative_folder(value)

    @field_validator("approved_structure_digest")
    @classmethod
    def _valid_structure_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("approved_structure_digest must be a SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _validate_exact_target_and_root(self) -> "PlanArtifact":
        if self.devs_project_folder != self.project_folder / "devs_project":
            raise ValueError("devs_project_folder must be project_folder/devs_project")
        if self.root.model_info.class_name != self.root_model_name:
            raise ValueError("Artifact root name does not match the planning tree")
        if self.root.model_info.logic_path != self.root_model_name:
            raise ValueError("Artifact root logic path must equal its model name")
        if self.graph.root_node_id != self.root.model_info.logic_path:
            raise ValueError("Artifact graph root does not match the planning tree")
        return self

    def to_serializable_dict(self) -> dict:
        """Return a JSON-compatible representation suitable for persistence."""

        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        """Return stable JSON for approval evidence and digest calculation."""

        return json.dumps(
            self.to_serializable_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_plan_tree(self) -> PlanTreeNode:
        return self.root.to_plan_tree()


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COUPLING_HEADER_RE = re.compile(r"^(EIC|IC|EOC)\s*:\s*(.*)$", re.IGNORECASE)


def _snake_case_identifier(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _model_aliases(name: str) -> set[str]:
    lowered = name.lower()
    return {lowered, _snake_case_identifier(name)}


def _graph_port(port: PortEntity, direction: Literal["input", "output"]) -> PlanGraphPort:
    return PlanGraphPort(
        name=port.name,
        direction=direction,
        type=port.type,
        structure=port.structure,
    )


def _walk_artifact_nodes(
    node: PlanArtifactNode,
    parent_id: Optional[str] = None,
) -> tuple[list[PlanGraphNode], list[PlanGraphContainment]]:
    node_id = node.model_info.logic_path
    nodes = [
        PlanGraphNode(
            id=node_id,
            name=node.model_info.class_name,
            model_type=node.plan.type,
            parent_id=parent_id,
            responsibility=node.model_info.specification.function,
            model_init_args=node.model_info.specification.model_init_args,
            input_ports=[
                _graph_port(port, "input")
                for port in node.model_info.specification.input_ports
            ],
            output_ports=[
                _graph_port(port, "output")
                for port in node.model_info.specification.output_ports
            ],
        )
    ]
    containment: list[PlanGraphContainment] = []
    for child in node.children:
        child_id = child.model_info.logic_path
        containment.append(PlanGraphContainment(parent_id=node_id, child_id=child_id))
        child_nodes, child_containment = _walk_artifact_nodes(child, node_id)
        nodes.extend(child_nodes)
        containment.extend(child_containment)
    return nodes, containment


def _parse_endpoint_parts(value: str) -> Optional[tuple[str, Optional[str], str]]:
    parts = [part.strip() for part in value.strip().split(".")]
    if not all(_SAFE_IDENTIFIER_RE.fullmatch(part) for part in parts):
        return None
    if len(parts) == 2:
        return parts[0], None, parts[1]
    if len(parts) == 3 and parts[1].upper() in {"IN", "OUT"}:
        return parts[0], parts[1].upper(), parts[2]
    return None


def _resolve_child_alias(
    token: str,
    children: list[PlanArtifactNode],
) -> Optional[PlanArtifactNode]:
    normalized = token.lower()
    candidates = [
        child
        for child in children
        if normalized in _model_aliases(child.model_info.class_name)
    ]
    if len(candidates) == 1:
        return candidates[0]

    # Homogeneous child collections are commonly planned as one class but
    # described as table_0, table_1, ... in the coupling specification.  Fold
    # those strict numeric instance suffixes back to the planned child class.
    collection_match = re.fullmatch(r"(.+?)_?\d+", normalized)
    if collection_match:
        base = collection_match.group(1).rstrip("_")
        candidates = [
            child
            for child in children
            if base in _model_aliases(child.model_info.class_name)
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _resolve_graph_endpoint(
    raw: str,
    *,
    owner: PlanArtifactNode,
    expected_direction: Literal["input", "output"],
) -> Optional[PlanGraphEndpoint]:
    parsed = _parse_endpoint_parts(raw)
    if parsed is None:
        return None
    model_token, explicit_direction, port_name = parsed
    if explicit_direction is not None:
        declared_direction = "input" if explicit_direction == "IN" else "output"
        if declared_direction != expected_direction:
            return None

    if model_token.lower() == "parent":
        boundary = "parent_input" if expected_direction == "input" else "parent_output"
        required_tag = "IN" if expected_direction == "input" else "OUT"
        if explicit_direction != required_tag:
            return None
        ports = (
            owner.model_info.specification.input_ports
            if expected_direction == "input"
            else owner.model_info.specification.output_ports
        )
        if port_name not in {port.name for port in ports}:
            return None
        return PlanGraphEndpoint(
            node_id=owner.model_info.logic_path,
            port_name=port_name,
            boundary=boundary,
        )

    child = _resolve_child_alias(model_token, owner.children)
    if child is None:
        return None
    ports = (
        child.model_info.specification.input_ports
        if expected_direction == "input"
        else child.model_info.specification.output_ports
    )
    if port_name not in {port.name for port in ports}:
        return None
    return PlanGraphEndpoint(
        node_id=child.model_info.logic_path,
        port_name=port_name,
        boundary="model",
    )


def _couplings_for_node(
    owner: PlanArtifactNode,
) -> tuple[list[PlanGraphCoupling], int]:
    specification = owner.plan.coupling_specification or ""
    current_kind: Optional[Literal["EIC", "IC", "EOC"]] = None
    parsed: list[PlanGraphCoupling] = []
    omitted = 0

    for raw_line in specification.splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        if not line:
            continue
        header = _COUPLING_HEADER_RE.fullmatch(line)
        if header:
            current_kind = header.group(1).upper()  # type: ignore[assignment]
            line = header.group(2).strip()
            if not line or line.lower() in {"none", "n/a", "null"}:
                continue
        if line.lower() in {"none", "n/a", "null"}:
            continue
        if current_kind is None or "->" not in line or line.count("->") != 1:
            omitted += 1
            continue
        source_raw, target_raw = [part.strip() for part in line.split("->", 1)]
        if current_kind == "EIC":
            source_direction, target_direction = "input", "input"
        elif current_kind == "EOC":
            source_direction, target_direction = "output", "output"
        else:
            source_direction, target_direction = "output", "input"
        source = _resolve_graph_endpoint(
            source_raw,
            owner=owner,
            expected_direction=source_direction,
        )
        target = _resolve_graph_endpoint(
            target_raw,
            owner=owner,
            expected_direction=target_direction,
        )
        if source is None or target is None:
            omitted += 1
            continue
        expected_boundaries = {
            "EIC": ("parent_input", "model"),
            "IC": ("model", "model"),
            "EOC": ("model", "parent_output"),
        }[current_kind]
        if (source.boundary, target.boundary) != expected_boundaries:
            omitted += 1
            continue
        parsed.append(
            PlanGraphCoupling(
                owner_node_id=owner.model_info.logic_path,
                coupling_type=current_kind,
                source=source,
                target=target,
            )
        )

    # Collapse repeated homogeneous-instance routes into one safe class-level
    # edge while retaining how many planned instances the text described.
    grouped: dict[str, PlanGraphCoupling] = {}
    for coupling in parsed:
        key = json.dumps(
            coupling.model_dump(mode="json", exclude={"multiplicity"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        if key in grouped:
            grouped[key].multiplicity += 1
        else:
            grouped[key] = coupling.model_copy(deep=True)

    couplings = list(grouped.values())
    for child in owner.children:
        child_couplings, child_omitted = _couplings_for_node(child)
        couplings.extend(child_couplings)
        omitted += child_omitted
    return couplings, omitted


def build_plan_graph(root: PlanArtifactNode) -> PlanGraph:
    """Build a safe review graph without reading or generating source files."""

    nodes, containment = _walk_artifact_nodes(root)
    couplings, omitted = _couplings_for_node(root)
    return PlanGraph(
        root_node_id=root.model_info.logic_path,
        nodes=nodes,
        containment=containment,
        couplings=couplings,
        omitted_coupling_count=omitted,
    )


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
    use_external_io: bool = False,
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
        if use_external_io: p_reqs["external_io"] = parent.specification.external_io
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
            if use_external_io: s_reqs["external_io"] = sib.specification.external_io
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
