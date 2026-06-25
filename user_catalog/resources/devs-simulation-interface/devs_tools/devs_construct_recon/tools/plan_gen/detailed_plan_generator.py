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
    ModelSpecification, TypedEntity, PortEntity, ProtocolSpec, ExternalIOStream
)
from ...utils import get_content_strict, extract_json
from ...wrapped_completion import completion_with_logging

from .detailed_plan_prompt import (
    BASE_PROMPT,
    ATOMIC_INSTRUCTION,
    COUPLED_INSTRUCTION,
    FIELD_GUIDANCE,
)

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

    base_prompt = BASE_PROMPT.format(
        target_name=target_name,
        module_type='COUPLED (Has children)' if is_coupled else 'ATOMIC (Leaf node)',
        children_names_str=children_names_str if children_names_str else 'None',
        requirements=requirements,
        global_plan_str=global_plan_str,
    )

    if is_root:
        inheritance = """
<InheritanceRules>
- This is the ROOT model. Keep the input/output ports minimal.
- Keep only essential passive configuration in root model_init_args.
- Do not create a separate output collector unless the requirements explicitly describe a module with that role.
- If the system reads from standard input (`stdin`), explicitly designate exactly ONE child module for this task. Multiple modules listening to `stdin` simultaneously will cause read conflicts and must be avoided.
- Root init args are passive configuration. Identify at least one child model responsible for the first active behavior when the simulation must start autonomously. Mark an output port `initial_signal` as active only when the business protocol actually requires a startup message; autonomous work does not need an invented startup port message.
- Follow the shared FieldContract for external_io, ports, and init args.
</InheritanceRules>
"""
    else:
        inheritance = f"""
<InheritanceRules>
**Model's Simple Plan** (this model's initial interface from parent):
{parent_simple_str}

**Parent's Detailed Plan** (system context):
{parent_detail_str}

- This model's own model_init_args, input_ports, and output_ports MUST exactly inherit from Model's Simple Plan.
- If this model is coupled, its direct children_plans are newly designed internal specifications. Child ports must be consistent with this model's own ports, sibling data flow, and coupling_specification, but they do not simply copy Parent's Simple Plan.
</InheritanceRules>
"""

    if is_coupled:
        instruction = COUPLED_INSTRUCTION
    else:
        instruction = ATOMIC_INSTRUCTION

    guidance = FIELD_GUIDANCE

    # 组合拼接
    return (base_prompt + inheritance + instruction + guidance).strip()

# ====== Pydantic raw response models ======

class _RawAtomicDetailed(BaseModel):
    class_name: str = Field(description="Name of the atomic model class. Must match target_name exactly.")
    model_type: Literal["atomic"] = Field(description="Must be 'atomic'.")
    function: str = Field(description="Behavioral responsibility, observable input/output behavior, timing expectations, and edge cases.")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="Direct external IO implemented by this atomic model itself.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list, description="Essential init args.")
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawCoupledDetailed(BaseModel):
    class_name: str = Field(description="Name of the coupled model class. Must match target_name exactly.")
    model_type: Literal["coupled"] = Field(description="Must be 'coupled'.")
    function: str = Field(description="Overall purpose and capability of the entire subsystem. NO active routing logic inside.")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="Direct external IO implemented by this coupled wrapper itself. Usually empty.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawSimple(BaseModel):
    class_name: str = Field(description="Name of the child model class.")
    model_type: Literal["atomic", "coupled"] = Field(description="Children with children -> 'coupled'; leaf -> 'atomic'.")
    function: str = Field(description="1-2 sentences describing responsibility.")
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="External IO responsibility of the whole subtree rooted at this child.",
    )
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


def _validate_model_init_args(owner: str, args: list[TypedEntity]) -> None:
    names = [arg.name for arg in args]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"{owner}: duplicate model_init_args: {duplicate_names}")
    if names[:2] != ["name", "parent"]:
        raise ValueError(
            f"{owner}: model_init_args must start with exactly one 'name' and one 'parent'; got {names[:2]}"
        )


def _validate_raw_plan(parsed: _RawAtomicDetailed | _RawCoupledResponse) -> None:
    if isinstance(parsed, _RawCoupledResponse):
        _validate_model_init_args(parsed.detailed_plan.class_name, parsed.detailed_plan.model_init_args)
        for child in parsed.children_plans:
            _validate_model_init_args(child.class_name, child.model_init_args)
        return
    _validate_model_init_args(parsed.class_name, parsed.model_init_args)


def _make_detailed_atomic(raw: _RawAtomicDetailed) -> DetailedPlan:
    return DetailedPlan(
        class_name=raw.class_name,
        model_type="atomic",
        specification=ModelSpecification(
            function=raw.function,
            external_io=raw.external_io,
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
            external_io=raw.external_io,
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
        external_io=raw.external_io,
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
            f"external_io: {plan.external_io}",
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
            f"external_io: {s.external_io}",
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
                _validate_raw_plan(parsed)

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
