from typing import Optional, Literal
import json
import re
import time
import litellm
from litellm import completion
from pydantic import BaseModel, Field

litellm.drop_params = True

from ...base_types import (
    GlobalPlanNode, DetailedPlan, SimpleDetailedPlan,
    ModelSpecification, TypedEntity, PortEntity, ProtocolSpec
)
from ...utils import get_content_strict, extract_json
from ...wrapped_completion import completion_with_logging


# ====== Raw LLM response schemas (String formats for prompts) ======
_RAW_ATOMIC_SCHEMA = """{
  "class_name": "ModelName",
  "model_type": "atomic",
  "function": "pure responsibility, state machine, event handling, and timing.",
  "logging": "ALL logging/output requirements: event names, payload key names & types, format, timing",
  "model_init_args": [{"name": "name of the arg", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}."}, ...],
  "input_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...],
  "output_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...]
}"""

_RAW_COUPLED_SCHEMA = """{
  "class_name": "ModelName",
  "model_type": "coupled",
  "function": "overall purpose and capability of this entire subsystem as a whole. No active routing logic inside the coupled wrapper itself.",
  "coupling_specification": "describe EIC/IC/EOC. Must specify model_name.IN/OUT.port_name.",
  "model_init_args": [{"name": "name of the arg", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}."}, ...],
  "input_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...],
  "output_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...]
}"""

_RAW_SIMPLE_SCHEMA = """{
  "class_name": "ModelName",
  "model_type": "atomic" or "coupled",
  "function": "1-2 sentences responsibility",
  "logging": "1-2 sentences",
  "model_init_args": [{"name": "name of the arg", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}."}, ...],
  "input_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...],
  "output_ports": [{"name": "name of the port", "type": "int/str/float/dict/list/...", "structure": "Description. IF dict/list, MUST use strict code format like {'key': type}.", "protocol": {"initial_state": "...", "initial_signal": "...", "description": "..."}}, ...]
}"""


def _build_prompt(
    target_name: str,
    requirements: str,
    global_plan_str: str,
    children_names_str: str,
    parent_simple_str: str,
    parent_detail_str: str,
    is_root: bool,
    is_coupled: bool,
) -> str:
    """Build the dynamic prompt based on module type using structural XML tags."""

    # ==========================================
    # 1. 基础信息区 (Base Context)
    # ==========================================
    base_prompt = f"""
<SystemRole>
You are a DEVS System Architect. 
</SystemRole>

<TargetContext>
Module Name: {target_name}
Module Type: {'COUPLED (Has children)' if is_coupled else 'ATOMIC (Leaf node)'}
Direct Children: {children_names_str if children_names_str else 'None'}
</TargetContext>

<SystemRequirements>
{requirements}
</SystemRequirements>

<GlobalPlanOverview>
{global_plan_str}
</GlobalPlanOverview>
"""

    # ==========================================
    # 2. 继承关系区 (Inheritance Rules)
    # ==========================================
    if is_root:
        inheritance = """
<InheritanceRules>
- This is the ROOT model. Keep the input/output ports minimal.
- model_init_args: get essential args.
- input_ports / output_ports: keep ports minimal, only contain the required function.
- Output Strategy: Business events are logged locally by each module using `self.logger.info(...)`. Every module is individually responsible for its own business output. Use ports for data transfer between modules (e.g., passing generation_time so the receiver can compute latency and log locally). Do not create a separate collector module for output — each module handles its own logging.
- If the system reads from standard input (`stdin`), explicitly designate exactly ONE child module for this task. Multiple modules listening to `stdin` simultaneously will cause read conflicts and must be avoided.
</InheritanceRules>
"""
    else:
        inheritance = f"""
<InheritanceRules>
**Model's Simple Plan** (this model's initial interface from parent):
{parent_simple_str}

**Parent's Detailed Plan** (system context):
{parent_detail_str}

- model_init_args: inherit from Parent's Simple Plan.
- input_ports / output_ports: inherit from Parent's Simple Plan.
</InheritanceRules>
"""

    # ==========================================
    # 3. 核心任务隔离区 (Task Instruction)
    # ==========================================
    if is_coupled:
        instruction = """
<TaskInstruction>
Generate a detailed specification for the COUPLED module and simple specifications for its direct children.

[STEP 1: Design Coupled Wrapper (detailed_plan)]
- function: Describe the overall purpose and capability of this entire subsystem as a unified whole. While the coupled wrapper itself only contains structural connections (no active routing/state logic), this field should summarize what the encapsulated subsystem achieves.

[STEP 2: Design Children (children_plans)]
- function: 1-2 sentences.
- logging: 1-2 sentences. This defines the **Terminal Output** (stdout/JSONL). Extract ALL requirements about "printing", "recording", or "final output". If a payload needs to be seen by the human user, define it here.
- model_init_args: full args (name/type/structure); always start with name (str) and parent (Coupled | None).
- input_ports / output_ports: 
    - full port definitions for sibling and parent matching. MUST make sure the structures of ports match strictly, and the name aligned with the coupling_specification. Children with children in the global plan -> `coupled`; leaf children -> `atomic`.
    - output_ports defines the **Simulation Data Flow**. Define ports ONLY for data that must be sent to another DEVS module (sibling or parent). NEVER define an output port simply to "print" or "log" something.
- port protocol: For the ports, you must carefully design the protocol to avoid deadlocks. CRITICAL: You MUST explicitly assign EXACTLY ONE side to send the initial signal at t=0. Never use "MAY" or "either side" in protocol descriptions — this ambiguity causes deadlock. For example, if processors must report "ready" before a router can dispatch, state: "processors MUST send initial {'status': 'idle'} at t=0 in initialize()". Every protocol MUST have exactly one unambiguous initial_signal sender.
- If requirements include dynamic multiplicity (e.g., count='arg:x'), children_plans and model_init_args MUST preserve the runtime count argument and support instantiating all required instances (not a single representative instance).

[STEP 3: Design coupling_specification]
- MUST define the network routing here. It's ONLY allowed to use these 3 strict DEVS connection patterns. List them clearly line-by-line:
    1. EIC (External Input Coupling): Routing external data IN to a child. Format: `parent.IN.port_name -> child.IN.port_name`
    2. IC (Internal Coupling): Data flowing between siblings. Format: `child_A.OUT.port_name -> child_B.IN.port_name`
    3. EOC (External Output Coupling): Routing child results OUT to the parent. Format: `child.OUT.port_name -> parent.OUT.port_name`

- CRITICAL COUPLING RULES:
    1. NO HALLUCINATIONS: Every port name you use MUST EXACTLY MATCH either the Parent's inherited ports or the Children's ports you defined in STEP 1. DO NOT invent ports.
    2. NOT ALL PORTS NEED PARENT CONNECTIONS: Children can communicate entirely with each other via IC. Some ports may remain uncoupled.
    3. If a child family has runtime multiplicity, describe coupling semantics as full instance-level expansion where required by scenario (e.g., one-to-many or full bipartite), not a sampled single-route shortcut.
</TaskInstruction>
"""
    else:
        instruction = """
<TaskInstruction>
Generate a detailed specification for the ATOMIC module.

[ATOMIC detailed_plan Design]
- function: pure responsibility, state machine, event handling, timing. Do NOT describe logging here.
- logging: Extract ALL logging requirements from the original requirements that apply to this model. Note that the things output to stdout are viewed as  "logging", while output ports are not. MUST clearly state payload structure, format, and timing. Must keep strict consistency with the requirements (especially the key names, event names, model names, entity names, etc.)
- model_init_args: copy from Model's Simple Plan.
- input_ports / output_ports: copy from Model's Simple Plan.

[Terminology Disambiguation: "Output" vs "Logging" - CRITICAL]
In the raw requirements, the word "output" is often used ambiguously. You MUST strictly map it to DEVS concepts as follows:
1. **Logging (stdout/file)**: If the requirement says "output to screen", "print", "save to file", "record", or "output the final statistics", this maps to Local Logging. DO NOT create DEVS Output Ports for this. DO NOT create dedicated "Log Collector" modules for this. Every module prints its own logs.
2. **DEVS Port Output**: If the requirement says "send to the next process", "route data to", or if a downstream module NEEDS this data to continue the simulation, this maps to DEVS Output Ports.
</TaskInstruction>
"""

    # ==========================================
    # 4. 字段格式指引区 (Field Guidance)
    # ==========================================
    guidance = """
<FieldGuidance>
- For ANY `model_init_args`, `input_ports`, or `output_ports` that use `dict` or `list` types, you MUST use a strict **Python Dict/List representation** to define the structure.
- Strict Format Examples:
    - BAD (Vague Summary): "Information about the sent packet including sequence number and retry flag."
    - BAD (Vague List): "A list of jobs."
    - GOOD (Strict Dict): "Packet info. Format: {'sequence_number': int, 'control_bit': str, 'is_retry': bool}."
    - GOOD (Strict List): "List of jobs. Format: [{'job_id': int, 'priority': float}]."
    - GOOD (Nested): "Format: {'metadata': {'timestamp': float, 'source': str}, 'payload': list[int]}."
- Types allowed: int, float, bool, str, dict, list. 
- Port protocol: Must include initial_state (state at T=0), initial_signal (signal at startup), description.
</FieldGuidance>
"""

    # 组合拼接
    return (base_prompt + inheritance + instruction + guidance).strip()

# ====== Pydantic raw response models ======

class _RawAtomicDetailed(BaseModel):
    class_name: str = Field(description="Name of the atomic model class. Must match target_name exactly.")
    model_type: Literal["atomic"] = Field(description="Must be 'atomic'.")
    function: str = Field(description="Pure responsibility, state machine, event handling, and timing.")
    logging: str = Field(description="ALL logging details — event names, payload keys/types, format, timing.")
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Essential init args.")
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawCoupledDetailed(BaseModel):
    class_name: str = Field(description="Name of the coupled model class. Must match target_name exactly.")
    model_type: Literal["coupled"] = Field(description="Must be 'coupled'.")
    function: str = Field(description="Overall purpose and capability of the entire subsystem. NO active routing logic inside.")
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawSimple(BaseModel):
    class_name: str = Field(description="Name of the child model class.")
    model_type: Literal["atomic", "coupled"] = Field(description="Children with children -> 'coupled'; leaf -> 'atomic'.")
    function: str = Field(description="1-2 sentences describing responsibility.")
    logging: str = Field(description="1-2 sentences logging requirements.")
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawCoupledResponse(BaseModel):
    detailed_plan: _RawCoupledDetailed = Field(description="Detailed specification for this coupled wrapper.")
    children_plans: list[_RawSimple] = Field(default_factory=list, description="Simple specifications for direct children.")
    coupling_specification: str = Field(description="Describe EIC/IC/EOC line-by-line. e.g., parent.IN.port -> child.IN.port")

class PlanGenResult:
    def __init__(self, detailed_plan: DetailedPlan, children_plans: list[SimpleDetailedPlan]):
        self.detailed_plan = detailed_plan
        self.children_plans = children_plans


def _make_detailed_atomic(raw: _RawAtomicDetailed) -> DetailedPlan:
    return DetailedPlan(
        class_name=raw.class_name,
        model_type="atomic",
        specification=ModelSpecification(
            function=raw.function,
            logging=raw.logging,
            model_init_args=raw.model_init_args,
            input_ports=raw.input_ports,
            output_ports=raw.output_ports,
        ),
        coupling_specification=None,  # Forced None for atomic
    )

def _make_detailed_coupled(raw: _RawCoupledDetailed, coupling: str) -> DetailedPlan:
    return DetailedPlan(
        class_name=raw.class_name,
        model_type="coupled",
        specification=ModelSpecification(
            function=raw.function,
            logging="",  # Forced empty string for coupled
            model_init_args=raw.model_init_args,
            input_ports=raw.input_ports,
            output_ports=raw.output_ports,
        ),
        coupling_specification=coupling,
    )

def _make_simple(raw: _RawSimple) -> SimpleDetailedPlan:
    return SimpleDetailedPlan(
        class_name=raw.class_name,
        model_type=raw.model_type if raw.model_type in ("atomic", "coupled") else "atomic",
        function=raw.function,
        logging=raw.logging,
        model_init_args=raw.model_init_args,
        input_ports=raw.input_ports,
        output_ports=raw.output_ports,
    )

# ====== Generator ======

class DetailedPlanGenerator:
    """Single method: generate detailed plan for a model + simple plans for its children."""

    def __init__(self, model_id: dict[str, str], disable_check: bool = True):
        self.model_id = model_id
        self.disable_check = disable_check

    def _get_model(self) -> str:
        if isinstance(self.model_id, dict):
            return self.model_id.get('strong', self.model_id.get('default', ''))
        return self.model_id

    def _fmt_global(self, plan: list[GlobalPlanNode]) -> str:
        lines = []
        for n in plan:
            ci = f" -> children: {', '.join(n.children_names)}" if n.children_names else " -> (atomic)"
            lines.append(f"- {n.name}: {n.description}{ci}")
        return "\n".join(lines)

    def _fmt_simple(self, plan: SimpleDetailedPlan) -> str:
        parts = [
            f"class_name: {plan.class_name}",
            f"model_type: {plan.model_type}",
            f"function: {plan.function}",
            f"logging: {plan.logging}",
        ]
        if plan.model_init_args:
            parts.append("model_init_args: " + ", ".join(f"{a.name} ({a.type}): {a.structure}" for a in plan.model_init_args))
        if plan.input_ports:
            parts.append("input_ports: " + ", ".join(f"{p.name} ({p.type}): {p.structure}" for p in plan.input_ports))
        if plan.output_ports:
            parts.append("output_ports: " + ", ".join(f"{p.name} ({p.type}): {p.structure}" for p in plan.output_ports))
        return "\n".join(parts)

    def _fmt_detailed(self, plan: DetailedPlan) -> str:
        s = plan.specification
        parts = [
            f"class_name: {plan.class_name}",
            f"model_type: {plan.model_type}",
            f"function: {s.function}",
            f"logging: {s.logging}",
            f"coupling_specification: {plan.coupling_specification or 'null'}",
        ]
        if s.input_ports:
            parts.append("input_ports: " + ", ".join(f"{p.name} ({p.type}): {p.structure}" for p in s.input_ports))
        if s.output_ports:
            parts.append("output_ports: " + ", ".join(f"{p.name} ({p.type}): {p.structure}" for p in s.output_ports))
        return "\n".join(parts)

    def generate(
        self,
        target_name: str,
        requirements: str,
        global_plan: list[GlobalPlanNode],
        children_names: list[str],
        parent_simple_plan: Optional[SimpleDetailedPlan] = None,
        parent_detailed_plan: Optional[DetailedPlan] = None,
        retry: int = 3,
    ) -> PlanGenResult:
        is_root = parent_simple_plan is None
        is_coupled = len(children_names) > 0  # 动态判断节点类型

        gstr = self._fmt_global(global_plan)
        cstr = ", ".join(children_names) if children_names else "None (leaf)"
        pstr = self._fmt_simple(parent_simple_plan) if parent_simple_plan else "(N/A - root)"
        dstr = self._fmt_detailed(parent_detailed_plan) if parent_detailed_plan else "(N/A - root)"

        model = self._get_model()

        # 根据类型动态选择 ResponseFormat
        # 注意：Atomic 模式下直接使用 _RawAtomicDetailed，不再包额外的一层 response wrapper
        ResponseModel = _RawCoupledResponse if is_coupled else _RawAtomicDetailed

        for attempt in range(retry):
            try:
                prompt = _build_prompt(
                    target_name=target_name,
                    requirements=requirements,
                    global_plan_str=gstr,
                    children_names_str=cstr,
                    parent_simple_str=pstr,
                    parent_detail_str=dstr,
                    is_root=is_root,
                    is_coupled=is_coupled, # 将类型传入，用于隔离 Prompt
                )
                
                resp = completion_with_logging(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    phase=f"phase1b_detailed_plan_{'coupled' if is_coupled else 'atomic'}",
                    target=target_name,
                    attempt=attempt,
                    temperature=0.5,
                    response_format=ResponseModel, # 动态传入精确 Schema
                )
                
                raw = extract_json(get_content_strict(resp))
                parsed = ResponseModel.model_validate(raw)

                if is_coupled:
                    assert isinstance(parsed, _RawCoupledResponse)
                    det = _make_detailed_coupled(parsed.detailed_plan, parsed.coupling_specification)
                    chs = [_make_simple(c) for c in getattr(parsed, "children_plans", [])]
                else:
                    # 对于 Atomic, 解析出来的 parsed 直接就是 detailed_plan 的主体
                    assert isinstance(parsed, _RawAtomicDetailed)
                    det = _make_detailed_atomic(parsed)
                    chs = [] # Atomic 永远返回空子节点列表

                if det.class_name != target_name:
                    raise ValueError(f"Expected '{target_name}', got '{det.class_name}'")
                
                if is_coupled and children_names:
                    got = {c.class_name for c in chs}
                    for cn in children_names:
                        if cn not in got:
                            raise ValueError(f"Child '{cn}' missing")

                print(f"[DetailedPlan] {target_name}: type={det.model_type}, children={len(chs)}")
                return PlanGenResult(detailed_plan=det, children_plans=chs)

            except Exception as e:
                es = str(e)
                if "rate" in es.lower() or "429" in es:
                    wait = 10 * (attempt + 1)
                    print(f"[DetailedPlan] Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                print(f"[DetailedPlan] Attempt {attempt + 1} failed for '{target_name}': {e}")
                if attempt < retry - 1:
                    time.sleep(2)

        raise Exception(f"Failed to generate plan for '{target_name}' after {retry} attempts")
