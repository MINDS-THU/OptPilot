from typing import Optional, Literal
import json
import re
import time
import litellm
from litellm import completion
from pydantic import BaseModel, ConfigDict, Field

litellm.drop_params = True

from ...base_types import (
    GlobalPlanNode, DetailedPlan, SimpleDetailedPlan,
    ModelSpecification, TypedEntity, PortEntity, ProtocolSpec, ExternalIOStream,
    AtomicImplementationExample, RequirementLedger,
)
from ...utils import get_content_strict, extract_json
from ...wrapped_completion import completion_with_logging
from ...schema_repair import repair_schema_output

from .detailed_plan_prompt import (
    BASE_PROMPT,
    ATOMIC_INSTRUCTION,
    ROOT_ATOMIC_INSTRUCTION,
    COUPLED_INSTRUCTION,
    INHERITED_COUPLED_INSTRUCTION,
    RESPONSIBILITY_GUIDANCE,
    COUPLING_EXAMPLE,
    ROOT_COUPLED_EXAMPLE,
    ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE,
    ROOT_TIMEOUT_FEEDBACK_COUPLED_EXAMPLE,
    ROOT_AGGREGATED_REPORT_COUPLED_EXAMPLE,
    ROOT_FINAL_ONLY_COUPLED_EXAMPLE,
    ROOT_TIMED_STDIN_COUPLED_EXAMPLE,
    INHERITED_COUPLED_EXAMPLE,
    ROOT_ATOMIC_EXAMPLE,
    FIELD_GUIDANCE,
)
from .requirement_ledger import (
    format_requirement_focus,
    format_requirement_flow_focus,
    requirement_ids_for_subtree,
)
from ..model_creator_fast.unified_model_examples import format_atomic_example_catalog

def _select_root_coupled_example(requirements: str) -> str:
    """Choose the closest complete root pattern without adding semantic rules."""
    text = requirements.casefold()
    one_json_document = bool(
        re.search(r"\b(?:exactly one|one|single)\s+json\s+(?:object|document)\b", text)
    ) and not bool(
        re.search(
            r"\bjson\s+(?:object|document)\s+per\s+"
            r"(?:observation|event|record|line|step)\b",
            text,
        )
    )
    explicitly_not_jsonl = bool(
        re.search(r"\b(?:do\s+not|don't|not)\s+(?:print|emit|write)?\s*jsonl\b", text)
    )
    aggregate_noun = bool(
        re.search(r"\b(?:events|operations|combined|report|summary)\b", text)
    )
    combined_report = one_json_document and explicitly_not_jsonl and aggregate_noun
    if combined_report:
        return ROOT_AGGREGATED_REPORT_COUPLED_EXAMPLE
    final_state = "final state" in text or "final_state" in text
    final_timing = any(
        marker in text
        for marker in (
            "output at the end",
            "once at the end",
            "at end of simulation",
            "at the end of simulation",
        )
    )
    if final_state and final_timing:
        return ROOT_FINAL_ONLY_COUPLED_EXAMPLE
    timeout_feedback = "timeout" in text and any(
        marker in text
        for marker in ("ack", "acknowledg", "feedback", "retransmit")
    )
    if timeout_feedback:
        return ROOT_TIMEOUT_FEEDBACK_COUPLED_EXAMPLE
    timed_stdin = "stdin" in text and any(
        marker in text
        for marker in (
            "timestamped",
            "timestamp",
            "hh:mm:ss",
        )
    ) and any(
        marker in text
        for marker in (
            "each integer",
            "every integer",
            "whole-second",
            "whole second",
        )
    )
    if timed_stdin:
        return ROOT_TIMED_STDIN_COUPLED_EXAMPLE
    return ROOT_COUPLED_EXAMPLE


def _has_reciprocal_explicit_flows(requirement_flow_str: str) -> bool:
    """Detect a request/reply topology without guessing from domain words."""
    pairs = {
        (source.strip().casefold(), target.strip().casefold())
        for source, target in re.findall(
            r"^\s*-\s*\[[^]]+\]\s+([^;\n]+?)\s*->\s*([^;\n]+?)\s*;",
            requirement_flow_str,
            flags=re.MULTILINE,
        )
    }
    return any((target, source) in pairs for source, target in pairs)


def _build_prompt(
    target_name: str,
    requirements: str,
    global_plan_str: str,
    children_names_str: str,
    parent_simple_str: str,
    parent_detail_str: str,
    is_root: bool,
    is_coupled: bool,
    requirement_focus_str: str = "(No requirement ledger was supplied.)",
    requirement_flow_str: str = "(No explicit flow index was supplied.)",
    sibling_contracts_str: str = "[]",
    plan_repair_feedback: str = "",
    planning_perspective: str = "",
) -> str:
    """Build the dynamic prompt based on module type using structural XML tags."""

    header = BASE_PROMPT.format(
        target_name=target_name,
        module_type='COUPLED (Has children)' if is_coupled else 'ATOMIC (Leaf node)',
        children_names_str=children_names_str if children_names_str else 'None',
    )

    if is_root:
        locked_contract = """
<InheritanceRules>
- This is the ROOT model. Keep the input/output ports minimal.
- Keep root model_init_args minimal, but do not drop explicit scenario constants that descendants need. Preserve concrete values such as rates, durations, capacities, thresholds, and time windows through constructor arguments or child specifications; never replace them with placeholders.
- If the system reads from standard input (`stdin`), explicitly designate exactly ONE child module for this task. Multiple modules listening to `stdin` simultaneously will cause read conflicts and must be avoided.
- Root init args are passive configuration. Identify at least one child model responsible for the first active behavior when the simulation must start autonomously. Mark an output port `initial_signal` as active only when the business protocol actually requires a startup message; autonomous work does not need an invented startup port message.
- Keep runner-control values such as `simulate_time`, `simulation_time`, or a pure observation horizon out of root `model_init_args` unless model behavior itself directly depends on that value. The runner owns the Coordinator stop condition.
- Follow the shared FieldContract for external_io, ports, and init args.
</InheritanceRules>
"""
    else:
        locked_contract = f"""
<LockedInheritedContract>
This parent-issued JSON is the single authority for the current module's
identity, constructor, ports, port protocols, brief role, and delegated external
IO. It appears before all broader context so later instructions cannot silently
override it.

{parent_simple_str}
</LockedInheritedContract>
"""

    system_context = f"""
<RelevantRequirementsForCurrentSubtree>
{requirement_focus_str}
</RelevantRequirementsForCurrentSubtree>

<SystemRequirements>
{requirements}
</SystemRequirements>

<GlobalPlanOverview>
{global_plan_str}
</GlobalPlanOverview>
"""

    parent_context = ""
    if not is_root:
        parent_context = f"""
<ParentContext>
**Parent's Detailed Plan** (system context):
{parent_detail_str}
</ParentContext>

<SiblingContracts>
These sibling plans are read-only context for understanding what this module
receives, sends, and does not need to duplicate.
{sibling_contracts_str}
</SiblingContracts>
"""

    if is_root and is_coupled:
        instruction = COUPLED_INSTRUCTION
        root_example = _select_root_coupled_example(requirements)
        # The generic and protocol examples may both contain nested/family
        # composition. Final-only and scheduled-input examples are deliberately
        # minimal and should not receive unrelated routing variants.
        if (
            root_example == ROOT_COUPLED_EXAMPLE
            and _has_reciprocal_explicit_flows(requirement_flow_str)
        ):
            root_example = ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE
        stage_example = root_example
        if root_example in (
            ROOT_COUPLED_EXAMPLE,
            ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE,
            ROOT_TIMEOUT_FEEDBACK_COUPLED_EXAMPLE,
        ):
            stage_example += COUPLING_EXAMPLE
    elif is_root:
        instruction = ROOT_ATOMIC_INSTRUCTION
        stage_example = ROOT_ATOMIC_EXAMPLE
    elif is_coupled:
        instruction = INHERITED_COUPLED_INSTRUCTION
        stage_example = INHERITED_COUPLED_EXAMPLE
    else:
        instruction = ATOMIC_INSTRUCTION
        # The behavioral catalog below is already the atomic stage's example
        # set. A second, function-heavy JSON example caused weak models to copy
        # unrelated state updates and external records into otherwise correct
        # source plans, while response_format already supplies the exact shape.
        stage_example = ""

    example_catalog_context = ""
    if not is_coupled:
        example_catalog_context = f"""
<AtomicImplementationExampleCatalog>
Choose exactly one identifier. Each entry describes the complete reference
file that code generation will receive; no source code is shown at this stage.
{format_atomic_example_catalog()}
</AtomicImplementationExampleCatalog>
"""

    final_notes: list[str] = []
    if not is_root:
        final_notes.append(
            "Re-read LockedInheritedContract before responding. Do not repeat or "
            "modify the current module interface. If it is insufficient, populate "
            "interface_change_requests instead of inventing fields."
        )
    if is_coupled:
        final_notes.append(
            "Finalize child ports before coupling_rules, then copy every endpoint "
            "from the final port lists or the locked current boundary."
        )
    final_reminder = """
<FinalContractReminder>
{notes}
</FinalContractReminder>
""".format(notes="\n".join(final_notes) if final_notes else "Follow the response schema exactly.")
    flow_context = ""
    if is_coupled:
        flow_context = f"""
<ExplicitInterModelFlowIndex>
{requirement_flow_str}
</ExplicitInterModelFlowIndex>
"""
    repair_context = ""
    if plan_repair_feedback:
        if is_root:
            repair_guidance = (
                "Apply the correction to the current hierarchy's child contracts "
                "and coupling rules. Preserve every compatible decision."
            )
        else:
            repair_guidance = (
                "The current `<LockedInheritedContract>` may already contain the "
                "correction. Treat it as authoritative and do not repeat a request "
                "for a deficiency it has resolved."
            )
        repair_context = f"""
<PlanRepairFeedback>
This is the historical reason a previous complete plan was regenerated, not an
additional user requirement. {repair_guidance} Correct only any part that is
still unresolved while preserving the original behavior.
{plan_repair_feedback}
</PlanRepairFeedback>
"""
    perspective_context = ""
    if planning_perspective:
        perspective_context = f"""
<PlanningPerspective>
{planning_perspective}
</PlanningPerspective>
"""
    return (
        header
        + locked_contract
        + system_context
        + parent_context
        + repair_context
        + perspective_context
        + RESPONSIBILITY_GUIDANCE
        + instruction
        + example_catalog_context
        + stage_example
        + FIELD_GUIDANCE
        + flow_context
        + final_reminder
    ).strip()


def _build_validation_repair_prompt(
    *,
    requirements: str,
    requirement_focus_str: str,
    requirement_flow_str: str = "(No explicit flow index was supplied.)",
    previous_candidate_json: str,
    validation_feedback: str,
    previously_observed_errors: list[str] | None = None,
) -> str:
    """Build a focused retry prompt without replaying the planning tutorial."""

    previous_errors = ""
    if previously_observed_errors:
        previous_errors = f"""
<PreviouslyObservedErrors>
These errors occurred in older candidates. Their fixes must be retained; do not
reintroduce them while correcting the current candidate.
{chr(10).join(previously_observed_errors)}
</PreviouslyObservedErrors>
"""

    return f"""
<SystemRole>
You repair one structured DEVS plan candidate after deterministic validation.
</SystemRole>

<SystemRequirements>
{requirements}
</SystemRequirements>

<RequirementFocus>
{requirement_focus_str}
</RequirementFocus>

<ExplicitInterModelFlowIndex>
{requirement_flow_str}
</ExplicitInterModelFlowIndex>

<PreviousCandidate>
{previous_candidate_json}
</PreviousCandidate>

{previous_errors}

<ValidationErrors>
{validation_feedback}
</ValidationErrors>

Return the complete corrected object in the required response schema. Make the
smallest coherent edit that resolves every listed error. Preserve all compatible
fields, interfaces, routing rules, and required behavior.

Two schema boundaries remain in force during repair:
- A coupling rule routes between declared DEVS ports. Never use stdin, stdout,
  stderr, or a file as a coupling endpoint.
- For a new `external_io` item, `target` names the OS resource and `content`
  states the record format, source, timing, and multiplicity.

<RepairExample>
If an older error required Source to retain stdin and the current error says
Writer lacks stdout, add stdout to Writer while leaving Source's stdin entry
unchanged. A repair is monotonic across independent constraints.

Wrong: add `{{"name":"stdout", ...}}` to a child's `output_ports`.
Right: leave its DEVS ports unchanged and add
`{{"target":"stdout","content":"Write the required record when ..."}}` to the
atomic child that observes that event.
</RepairExample>
""".strip()


def _build_root_reconciliation_prompt(
    *,
    target_name: str,
    requirements: str,
    requirement_focus_str: str,
    requirement_flow_str: str = "(No explicit flow index was supplied.)",
    global_plan_str: str,
    children_names: list[str],
    candidate_payloads: list[dict],
    include_endpoint_audit_example: bool = False,
    repair_feedback: str = "",
) -> str:
    """Ask for one canonical plan, not prose criticism or a vote."""

    candidates = "\n\n".join(
        f"<Candidate{index}>\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + f"\n</Candidate{index}>"
        for index, payload in enumerate(candidate_payloads, start=1)
    )
    requirement_focus_context = f"""
<RelevantRequirementsForCurrentSubtree>
{requirement_focus_str}
</RelevantRequirementsForCurrentSubtree>
"""
    if len(candidate_payloads) == 1:
        role = "You revise one draft of a ROOT COUPLED DEVS plan."
        candidate_instruction = (
            "The candidate is a fallible working draft, not a locked contract. "
            "Audit its event flow and interfaces before reusing it. Preserve fields "
            "that survive that audit, correct every concrete omission, contradiction, "
            "or unrealizable interface, and return the complete revised plan. If no "
            "correction is needed, return the complete candidate unchanged."
        )
        # The full original request and the compact explicit flow index remain
        # present. Repeating the root's all-requirement ledger JSON here adds a
        # second lossy paraphrase and makes the focused revision unnecessarily
        # long.
        requirement_focus_context = ""
    else:
        role = "You reconcile independent drafts of one ROOT COUPLED DEVS plan."
        candidate_instruction = (
            "The candidates are drafts, not authorities: preserve compatible parts, "
            "resolve their disagreements from the original requirements, and correct "
            "an error even when all drafts share it."
        )
    root_example = _select_root_coupled_example(requirements)
    if (
        root_example == ROOT_COUPLED_EXAMPLE
        and _has_reciprocal_explicit_flows(requirement_flow_str)
    ):
        root_example = ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE
    if root_example == ROOT_FINAL_ONLY_COUPLED_EXAMPLE:
        workflow_instruction = """
Follow the final-state StageExample when the required direct child owns the
complete evolving state: keep the root structural, pass CLI configuration by
constructor, let that child write its own final record, and leave ports and
coupling_rules empty. Add a DEVS port only when the original requirements name
a distinct model that consumes the event; a field in the final JSON record is
not such an event.
""".strip()
    elif root_example == ROOT_AGGREGATED_REPORT_COUPLED_EXAMPLE:
        workflow_instruction = """
Follow the combined-report StageExample when facts from several producers must
become exactly one final JSON document. Route those facts through DEVS ports to
one collector, give stdout only to that collector, and let it write once after
simulation completion. Do not turn the required event list into per-event
JSONL, and do not make the input reader own facts produced by later stages.
""".strip()
    elif root_example == ROOT_TIMED_STDIN_COUPLED_EXAMPLE:
        workflow_instruction = """
Follow the scheduled-input StageExample when one timestamped stdin schedule
drives observations at fixed simulation times. Give stdin to one source, let
that source own the periodic clock, and send one selected value to the state
model at each required observation time. State used only by that same atomic
model remains internal; it needs no feedback port or self-coupling.
""".strip()
    else:
        workflow_instruction = """
Trace every required event from the model that produces it to the model that
consumes it and through any required completion, acknowledgement, availability,
or release feedback. Make each fixed child source a declared output port and
each fixed child destination a declared input port. Prefer the smallest coupling
graph that completes the required workflow. For a route found in only one draft,
keep it only when the original requirements or a necessary progress path gives
the destination a reason to consume that event. An atomic model's scheduled
service completion is handled by its internal transition; emitting or logging
the result does not by itself require coupling that output back to the same
model. A name that appears only in the required output/log schema is an
observation, not a message to be consumed; do not invent a same-named input or
route unless the workflow explicitly identifies a consumer. Remember that one
output coupled to multiple child inputs broadcasts each event to all of them;
when the workflow selects exactly one recipient, represent that selection with
distinct routes or an explicit tagged-message filtering contract.

Before returning, walk the graph once in both directions:
- A rejection, logout, or completion that is only logged and ends that item's
  processing is terminal; it needs no invented consumer.
- A root route may touch only a direct child's declared boundary. If that child
  is itself coupled, expose a boundary port here and let its later detail plan
  route that port to or from the appropriate grandchild.
- A model may emit only information it can know. If waiting work and available
  resources are owned by different children, exchange the needed events or put
  the matching decision in the child that has both; do not make the queue emit
  a selected resource identifier it never received.
- A family name in a rule denotes the runtime instances of that family. Code
  generation must realize the rule for the concrete instances, not treat the
  model class itself as a coupling endpoint.
""".strip()

    coupling_variants = (
        COUPLING_EXAMPLE
        if root_example in (
            ROOT_COUPLED_EXAMPLE,
            ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE,
            ROOT_TIMEOUT_FEEDBACK_COUPLED_EXAMPLE,
        )
        else ""
    )
    if len(candidate_payloads) == 1 and "\n- [" in requirement_flow_str:
        # The candidate itself is now the concrete worked example. Repeating a
        # different full architecture in this focused revision encourages
        # analogy instead of checking the current event graph.
        root_example = ""
        coupling_variants = ""

    endpoint_audit_example = ""
    if include_endpoint_audit_example:
        endpoint_audit_example = """
<EndpointAuditExample>
Suppose Worker declares output `done` and Queue declares output `available` but
no matching inputs. `Worker.done -> Queue.available` is invalid because both
endpoints are outputs. Either add a genuinely consumed Queue input to Queue's
plan and route to that input, or omit the route when `done` is only logged.
Apply this same source-output/destination-input check to the current draft.
</EndpointAuditExample>
""".strip()

    repair_context = ""
    if repair_feedback:
        repair_context = f"""
<RequiredRepairs>
The previous candidate failed child or deterministic validation for the reasons
below. This is repair evidence, not an additional user requirement. Apply every
compatible requested change to the candidate, including matching peer ports and
coupling rules, while preserving unaffected decisions.
{repair_feedback}
</RequiredRepairs>
""".strip()

    return f"""
<SystemRole>
{role}
</SystemRole>

<TargetContext>
Root class: {target_name}
Required direct children: {json.dumps(children_names, ensure_ascii=False)}
</TargetContext>

{requirement_focus_context}

<SystemRequirements>
{requirements}
</SystemRequirements>

<GlobalPlanOverview>
{global_plan_str}
</GlobalPlanOverview>

{candidates}

{root_example}

{coupling_variants}

<ExplicitInterModelFlowIndex>
{requirement_flow_str}
</ExplicitInterModelFlowIndex>

{endpoint_audit_example}

{repair_context}

<TaskInstruction>
Produce one complete replacement plan in the required response schema. Do not
return criticism, confidence, uncertainties, or a choice between candidates.
{candidate_instruction}

{workflow_instruction}

Keep the required direct-child names and types from the global plan. Keep OS
operations in child `external_io`; stdin, stdout, stderr, and files are not
coupling endpoints. The coupled root itself performs no active behavior.

Return the complete root `detailed_plan`, every `children_plans` entry, and all
actual `coupling_rules`, including routes on which progress or capacity release
depends. Finalize the revised child port lists first, then copy every coupling
endpoint from those lists (or the declared root boundary). For every route,
confirm that the source child behavior emits that event and the target child
behavior consumes it; never route an earlier input as a later downstream event.
Match every line in ExplicitInterModelFlowIndex to a realizable route. If the
candidate lacks the required source output or target input, revise that child
interface and function before adding the route; do not substitute a similarly
named stdout record or an event with a different producer or consumer.
Do not defer a discrepancy to later child planning or code generation.
</TaskInstruction>
""".strip()

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
    implementation_example: AtomicImplementationExample = Field(
        description="One complete-file example selected by its behavioral description."
    )

class _RawCoupledDetailed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class_name: str = Field(description="Name of the coupled model class. Must match target_name exactly.")
    model_type: Literal["coupled"] = Field(description="Must be 'coupled'.")
    function: str = Field(description="Overall purpose and capability of the entire subsystem. NO active routing logic inside.")
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)

class _RawSimple(BaseModel):
    class_name: str = Field(description="Name of the child model class.")
    model_type: Literal["atomic", "coupled"] = Field(description="Children with children -> 'coupled'; leaf -> 'atomic'.")
    function: str = Field(
        description=(
            "1-2 sentences describing responsibility. Distinguish a DEVS message "
            "sent to another model from a record written to an OS stream; state "
            "both when both occur."
        )
    )
    external_io: list[ExternalIOStream] = Field(
        default_factory=list,
        description="External IO responsibility of the whole subtree rooted at this child.",
    )
    model_init_args: list[TypedEntity] = Field(default_factory=list)
    input_ports: list[PortEntity] = Field(default_factory=list)
    output_ports: list[PortEntity] = Field(default_factory=list)
    related_requirement_ids: list[str] = Field(
        default_factory=list,
        description=(
            "All ledger IDs relevant to this child subtree, including shared "
            "workflow, IO, schema, timing, and parameter requirements."
        ),
    )

class _RawCoupledResponse(BaseModel):
    detailed_plan: _RawCoupledDetailed = Field(description="Detailed specification for this coupled wrapper.")
    children_plans: list[_RawSimple] = Field(default_factory=list, description="Simple specifications for direct children.")
    coupling_rules: list[str] = Field(
        default_factory=list,
        description=(
            "One complete natural-language routing rule per item. A rule may "
            "describe a fixed edge or a repeated, indexed, conditional, EIC, IC, "
            "or EOC family of connections. In a fixed IC A.x -> B.y, copy x from "
            "A.output_ports and y from B.input_ports. An EIC is parent.input -> "
            "child.input; an EOC is child.output -> parent.output. Never use an "
            "input as a source, an output as a target, or a port absent from the "
            "corresponding declaration. Endpoints are DEVS ports, never stdin, "
            "stdout, stderr, or files. Include actual routes only; use an empty "
            "list when no coupling is needed."
        ),
    )


class InterfaceChangeRequest(BaseModel):
    field: Literal[
        "model_init_args", "input_ports", "output_ports", "external_io", "other"
    ] = Field(
        description=(
            "Locked contract field that prevents the required behavior. Select "
            "external_io for stdin, stdout, stderr, or file operations; select "
            "input_ports/output_ports only for DEVS messages exchanged with "
            "another model."
        )
    )
    reason: str = Field(description="Why the locked field is insufficient.")
    requested_change: str = Field(description="Minimal parent-level change requested.")


class InterfaceChangeIssue(BaseModel):
    """One child request that must be applied by its direct parent plan."""

    target_name: str = Field(
        description="Child model whose inherited contract is insufficient."
    )
    request: InterfaceChangeRequest


class InterfaceChangeRequired(ValueError):
    """Raised when a child cannot legally implement its inherited contract."""

    def __init__(self, issues: list[InterfaceChangeIssue]):
        if not issues:
            raise ValueError("InterfaceChangeRequired needs at least one issue")
        self.issues = tuple(issues)
        payload = [issue.model_dump(mode="json") for issue in self.issues]
        super().__init__(
            "Interface changes requested: "
            + json.dumps(payload, ensure_ascii=False)
        )


class _RawInheritedAtomicExpansion(BaseModel):
    function: str = Field(description="Complete behavioral expansion of the atomic model.")
    implementation_example: AtomicImplementationExample = Field(
        description="One complete-file example selected by its behavioral description."
    )
    interface_change_requests: list[InterfaceChangeRequest] = Field(default_factory=list)


class _RawInheritedCoupledResponse(BaseModel):
    function: str = Field(description="Capability achieved by the coupled subtree.")
    children_plans: list[_RawSimple] = Field(default_factory=list)
    coupling_rules: list[str] = Field(
        default_factory=list,
        description=(
            "One complete natural-language DEVS-port routing rule per item. In a "
            "fixed IC A.x -> B.y, copy x from A.output_ports and y from "
            "B.input_ports. An EIC is parent.input -> child.input; an EOC is "
            "child.output -> parent.output. Never reverse those roles or name an "
            "undeclared port. OS targets such as stdin, stdout, stderr, and files "
            "are not endpoints. Include actual routes only; use an empty list when "
            "no coupling is needed."
        ),
    )
    interface_change_requests: list[InterfaceChangeRequest] = Field(default_factory=list)

class PlanGenResult:
    def __init__(self, detailed_plan: DetailedPlan, children_plans: list[SimpleDetailedPlan]):
        self.detailed_plan = detailed_plan
        self.children_plans = children_plans


def _discard_satisfied_interface_requests(
    requests: list[InterfaceChangeRequest],
    inherited: SimpleDetailedPlan | None,
) -> list[InterfaceChangeRequest]:
    """Drop only requests that are mechanically already true in the contract."""
    if inherited is None:
        return requests
    existing_streams = {item.target.casefold() for item in inherited.external_io}
    retained = []
    for request in requests:
        requested_change = request.requested_change.strip().casefold().rstrip(".")
        if requested_change in {"no change needed", "no changes required"}:
            print(
                f"[DetailedPlan] Ignoring explicit no-op interface request for "
                f"'{inherited.class_name}': {request.field}"
            )
            continue
        if request.field == "external_io":
            mentioned_streams = set(
                re.findall(
                    r"\b(?:stdin|stdout|stderr)\b",
                    f"{request.reason} {request.requested_change}".casefold(),
                )
            )
            if mentioned_streams and mentioned_streams <= existing_streams:
                print(
                    f"[DetailedPlan] Ignoring already-satisfied external_io "
                    f"request for '{inherited.class_name}': "
                    f"{sorted(mentioned_streams)}"
                )
                continue
        retained.append(request)
    return retained


def _normalize_root_coupled_response_layout(raw: object) -> object:
    """Rehome exact root fields when a provider nests them one level too deep.

    This is deliberately structural rather than semantic: it recognizes only the
    schema's exact field names and refuses to choose between conflicting copies.
    """
    if not isinstance(raw, dict):
        return raw
    detailed_plan = raw.get("detailed_plan")
    if not isinstance(detailed_plan, dict):
        return raw

    normalized = dict(raw)
    normalized_detail = dict(detailed_plan)
    changed = False
    for field_name in ("children_plans", "coupling_rules"):
        if field_name not in normalized_detail:
            continue
        nested_value = normalized_detail[field_name]
        top_value = normalized.get(field_name)
        if field_name not in normalized or top_value is None or top_value == []:
            normalized[field_name] = nested_value
        elif top_value != nested_value:
            raise ValueError(
                f"Conflicting root coupled field '{field_name}' appears both "
                "at the top level and inside detailed_plan."
            )
        normalized_detail.pop(field_name)
        changed = True

    if changed:
        normalized["detailed_plan"] = normalized_detail
    return normalized


def _is_non_retryable_provider_error(message: str) -> bool:
    folded = message.casefold()
    return "403" in folded and "request is prohibited due to a violation" in folded


def _collect_model_init_arg_errors(owner: str, args: list[TypedEntity]) -> list[str]:
    names = [arg.name for arg in args]
    errors: list[str] = []
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        errors.append(f"{owner}: duplicate model_init_args: {duplicate_names}")
    if names[:2] != ["name", "parent"]:
        required_prefix = [
            {"name": "name", "type": "str", "structure": "Model instance name"},
            {
                "name": "parent",
                "type": "object",
                "structure": "Framework parent reference or None",
            },
        ]
        errors.append(
            f"{owner}: model_init_args must start with exactly one 'name' and one "
            f"'parent'; got {names[:2]}. Required JSON prefix: "
            + json.dumps(required_prefix, ensure_ascii=False)
        )
    allowed_business_types = {"int", "float", "bool", "str", "dict", "list"}
    for argument in args:
        if argument.name == "parent":
            continue
        if argument.type not in allowed_business_types:
            errors.append(
                f"{owner}: model_init_arg {argument.name!r} uses unsupported "
                f"type {argument.type!r}. Only the framework 'parent' argument "
                "may use object; expose business values as explicit primitive "
                "arguments or a dict with its complete key schema."
            )
    return errors


def _collect_reserved_os_port_errors(
    owner: str,
    input_ports: list[PortEntity],
    output_ports: list[PortEntity],
) -> list[str]:
    errors = []
    for direction, ports in (("input", input_ports), ("output", output_ports)):
        reserved = sorted(
            {
                port.name
                for port in ports
                if port.name.casefold() in {"stdin", "stdout", "stderr"}
            }
        )
        if reserved:
            errors.append(
                f"{owner}: OS stream name(s) {reserved} cannot be DEVS {direction} "
                "ports. Put each operation in external_io with its exact target; "
                "create a DEVS port only for a message consumed by another model."
            )
    return errors


def _collect_raw_plan_errors(
    parsed: _RawAtomicDetailed
    | _RawCoupledResponse
    | _RawInheritedAtomicExpansion
    | _RawInheritedCoupledResponse,
) -> list[str]:
    errors: list[str] = []
    if isinstance(parsed, _RawCoupledResponse):
        errors.extend(
            _collect_model_init_arg_errors(
                parsed.detailed_plan.class_name, parsed.detailed_plan.model_init_args
            )
        )
        errors.extend(
            _collect_reserved_os_port_errors(
                parsed.detailed_plan.class_name,
                parsed.detailed_plan.input_ports,
                parsed.detailed_plan.output_ports,
            )
        )
        for child in parsed.children_plans:
            errors.extend(
                _collect_model_init_arg_errors(child.class_name, child.model_init_args)
            )
            errors.extend(
                _collect_reserved_os_port_errors(
                    child.class_name, child.input_ports, child.output_ports
                )
            )
    elif isinstance(parsed, _RawInheritedCoupledResponse):
        for child in parsed.children_plans:
            errors.extend(
                _collect_model_init_arg_errors(child.class_name, child.model_init_args)
            )
            errors.extend(
                _collect_reserved_os_port_errors(
                    child.class_name, child.input_ports, child.output_ports
                )
            )
    elif isinstance(parsed, _RawAtomicDetailed):
        errors.extend(_collect_model_init_arg_errors(parsed.class_name, parsed.model_init_args))
        errors.extend(
            _collect_reserved_os_port_errors(
                parsed.class_name, parsed.input_ports, parsed.output_ports
            )
        )
    return errors


def _validate_raw_plan(
    parsed: _RawAtomicDetailed
    | _RawCoupledResponse
    | _RawInheritedAtomicExpansion
    | _RawInheritedCoupledResponse,
) -> None:
    errors = _collect_raw_plan_errors(parsed)
    if errors:
        raise ValueError("Detailed-plan validation errors:\n- " + "\n- ".join(errors))


def _requirements_explicitly_require_stream(
    requirements: str, stream: str, *aliases: str
) -> bool:
    folded = (
        requirements.casefold()
        .replace("sys.stdin", "stdin")
        .replace("sys.stdout", "stdout")
    )
    names = tuple(
        name.casefold()
        .replace("sys.stdin", "stdin")
        .replace("sys.stdout", "stdout")
        for name in (stream, *aliases)
    )
    has_explicit_negation = False
    has_unconditional_mention = False
    for clause in re.split(r"[\n.!?;]+", folded):
        if not any(name in clause for name in names):
            continue
        escaped = "|".join(re.escape(name) for name in names)
        negated = any(
            re.search(pattern, clause)
            for pattern in (
                rf"\bno\s+(?:\w+\s+){{0,3}}(?:{escaped})\b",
                rf"\bwithout\s+(?:\w+\s+){{0,3}}(?:{escaped})\b",
                rf"(?:{escaped})\b.{{0,40}}\b(?:not required|not used|unused)\b",
                rf"\b(?:does not|doesn't|do not|don't)\s+require\b.{{0,40}}(?:{escaped})\b",
            )
        )
        if negated:
            has_explicit_negation = True
            continue
        conditional = bool(
            re.search(
                r"\b(?:if|when)\b.{0,80}\b(?:required|needed|provided|available)\b",
                clause,
            )
            or re.search(r"\boptional(?:ly)?\b", clause)
        )
        if conditional:
            continue
        has_unconditional_mention = True
        if re.search(
            r"\b(?:read|reads|reading|consume|consumes|receive|receives|"
            r"accept|accepts|parse|parses|load|loads|write|writes|writing|"
            r"print|prints|emit|emits|send|sends)\b",
            clause,
        ):
            return True
    return has_unconditional_mention and not has_explicit_negation


def _collect_root_external_io_delegation_errors(
    requirements: str, children: list[SimpleDetailedPlan]
) -> list[str]:
    """Validate placement of required OS streams while root retry is local."""

    needs_stdout = _requirements_explicitly_require_stream(
        requirements, "stdout", "sys.stdout"
    )
    needs_stdin = _requirements_explicitly_require_stream(
        requirements, "stdin", "sys.stdin"
    )
    stdout_owners = [
        child.class_name
        for child in children
        if any(stream.target == "stdout" for stream in child.external_io)
    ]
    stdin_owners = [
        child.class_name
        for child in children
        if any(stream.target == "stdin" for stream in child.external_io)
    ]
    errors: list[str] = []
    if needs_stdout and not stdout_owners:
        errors.append(
            "Required stdout is not assigned to any direct child subtree. For "
            "each missing record type, make the atomic model that produces it "
            "write it through external_io, or pass the operation down one coupled "
            "child subtree. Do not duplicate a record type already written by "
            "another child."
        )
    if needs_stdin and len(stdin_owners) != 1:
        required_item = {
            "target": "stdin",
            "content": (
                "Read the required input stream directly; preserve its exact "
                "format, timing, and multiplicity from SystemRequirements."
            ),
        }
        errors.append(
            "Exactly one direct child subtree must receive the required stdin "
            f"stream; current readers are {stdin_owners!r}. Other children receive parsed "
            "data through DEVS ports. Add one external_io item shaped like "
            + json.dumps(required_item, ensure_ascii=False)
            + " to the appropriate child, with content specialized to the actual request."
        )
    return errors


def _validate_root_external_io_delegation(
    requirements: str, children: list[SimpleDetailedPlan]
) -> None:
    errors = _collect_root_external_io_delegation_errors(requirements, children)
    if errors:
        raise ValueError("Invalid root external IO delegation: " + " ".join(errors))


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
            implementation_example=raw.implementation_example,
        ),
        coupling_rules=[],
    )

def _make_detailed_coupled(
    raw: _RawCoupledDetailed, coupling_rules: list[str]
) -> DetailedPlan:
    # Exact repetition cannot add routing semantics, but weaker models sometimes
    # append an unchanged candidate rule again during a final revision. Preserve
    # order and wording while removing only byte-for-byte duplicates.
    unique_rules = list(dict.fromkeys(coupling_rules))
    return DetailedPlan(
        class_name=raw.class_name,
        model_type="coupled",
        specification=ModelSpecification(
            function=raw.function,
            external_io=[],
            model_init_args=raw.model_init_args,
            input_ports=raw.input_ports,
            output_ports=raw.output_ports,
        ),
        coupling_rules=unique_rules,
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
        related_requirement_ids=raw.related_requirement_ids,
    )


def _make_inherited_atomic(
    parent: SimpleDetailedPlan,
    raw: _RawInheritedAtomicExpansion,
) -> DetailedPlan:
    return DetailedPlan(
        class_name=parent.class_name,
        model_type="atomic",
        specification=ModelSpecification(
            function=raw.function,
            external_io=[item.model_copy(deep=True) for item in parent.external_io],
            model_init_args=[item.model_copy(deep=True) for item in parent.model_init_args],
            input_ports=[item.model_copy(deep=True) for item in parent.input_ports],
            output_ports=[item.model_copy(deep=True) for item in parent.output_ports],
            implementation_example=raw.implementation_example,
        ),
        coupling_rules=[],
    )


def _make_inherited_coupled(
    parent: SimpleDetailedPlan,
    raw: _RawInheritedCoupledResponse,
) -> DetailedPlan:
    return DetailedPlan(
        class_name=parent.class_name,
        model_type="coupled",
        specification=ModelSpecification(
            function=raw.function,
            # The simple plan carries responsibility delegated to this subtree;
            # the coupled wrapper itself remains a pure structural container.
            external_io=[],
            model_init_args=[item.model_copy(deep=True) for item in parent.model_init_args],
            input_ports=[item.model_copy(deep=True) for item in parent.input_ports],
            output_ports=[item.model_copy(deep=True) for item in parent.output_ports],
            implementation_mechanisms=[],
        ),
        coupling_rules=list(dict.fromkeys(raw.coupling_rules)),
    )


def _typed_entity_contract(entity: TypedEntity) -> tuple[str, str]:
    """Return the executable part of a typed interface declaration.

    ``structure`` and the fields inside ``ProtocolSpec`` are explanatory prose.
    A child planner may legitimately paraphrase them, so they are not suitable
    retry gates.  The parent-issued simple plan remains authoritative and is
    copied back after the executable names/types have been checked.
    """
    return (
        entity.name.strip(),
        "".join(entity.type.split()),
    )


def _normalized_port_contract(
    owner: str,
    label: str,
    ports: list[PortEntity],
) -> tuple[tuple[str, str], ...]:
    names = [port.name.strip() for port in ports]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"{owner}: duplicate {label}: {duplicates}")
    # Port declaration order is not part of the DEVS interface contract.
    return tuple(sorted((_typed_entity_contract(port) for port in ports), key=lambda item: item[0]))


def _validate_inherited_interface(
    parent_simple_plan: SimpleDetailedPlan,
    detailed_plan: DetailedPlan,
) -> DetailedPlan:
    """Validate executable identity, then inherit the canonical parent interface.

    The simple plan is the interface authority.  We reject changes that affect
    Python/DEVS wiring (class, model type, argument/port names or types), while
    accepting harmless prose paraphrases.  Returning a copy rather than
    mutating the parsed object makes the canonicalization explicit to callers.
    """
    owner = detailed_plan.class_name
    mismatches: list[str] = []
    if owner != parent_simple_plan.class_name:
        mismatches.append(
            f"class_name expected {parent_simple_plan.class_name!r}, got {owner!r}"
        )
    if detailed_plan.model_type != parent_simple_plan.model_type:
        mismatches.append(
            f"model_type expected {parent_simple_plan.model_type!r}, "
            f"got {detailed_plan.model_type!r}"
        )

    expected_args = tuple(
        _typed_entity_contract(item) for item in parent_simple_plan.model_init_args
    )
    actual_args = tuple(
        _typed_entity_contract(item) for item in detailed_plan.specification.model_init_args
    )
    if actual_args != expected_args:
        mismatches.append(
            f"model_init_args expected {expected_args!r}, got {actual_args!r}"
        )

    for label in ("input_ports", "output_ports"):
        expected_ports = _normalized_port_contract(
            parent_simple_plan.class_name,
            label,
            getattr(parent_simple_plan, label),
        )
        actual_ports = _normalized_port_contract(
            owner,
            label,
            getattr(detailed_plan.specification, label),
        )
        if actual_ports != expected_ports:
            mismatches.append(
                f"{label} (including protocols) expected {expected_ports!r}, "
                f"got {actual_ports!r}"
            )

    if mismatches:
        raise ValueError(
            f"Inherited interface mismatch for '{parent_simple_plan.class_name}': "
            + "; ".join(mismatches)
        )

    canonical = detailed_plan.model_copy(deep=True)
    canonical.specification.model_init_args = [
        item.model_copy(deep=True) for item in parent_simple_plan.model_init_args
    ]
    canonical.specification.input_ports = [
        item.model_copy(deep=True) for item in parent_simple_plan.input_ports
    ]
    canonical.specification.output_ports = [
        item.model_copy(deep=True) for item in parent_simple_plan.output_ports
    ]
    return canonical


def _collect_child_plan_errors(
    *,
    target_name: str,
    expected_names: list[str],
    children: list[SimpleDetailedPlan],
    global_plan: list[GlobalPlanNode],
    requirement_ledger: RequirementLedger | None = None,
) -> list[str]:
    errors: list[str] = []
    got_names = [child.class_name for child in children]
    if len(got_names) != len(set(got_names)):
        errors.append(f"Duplicate children returned for '{target_name}': {got_names}")
    expected = set(expected_names)
    got = set(got_names)
    if got != expected:
        errors.append(
            f"Child set mismatch for '{target_name}': "
            f"missing={sorted(expected - got)}, extra={sorted(got - expected)}"
        )
    topology = {node.name: node for node in global_plan}
    for child in children:
        if child.class_name not in topology:
            continue
        expected_type = "coupled" if topology[child.class_name].children_names else "atomic"
        if child.model_type != expected_type:
            errors.append(
                f"Child '{child.class_name}' type mismatch: expected "
                f"'{expected_type}', got '{child.model_type}'"
            )
        if requirement_ledger is not None:
            known_ids = {item.id for item in requirement_ledger.items}
            unknown_ids = sorted(set(child.related_requirement_ids) - known_ids)
            if unknown_ids:
                errors.append(
                    f"Child '{child.class_name}' references unknown requirement "
                    f"IDs: {unknown_ids}"
                )
    return errors


def _discard_unknown_child_requirement_links(
    children: list[SimpleDetailedPlan],
    requirement_ledger: RequirementLedger | None,
) -> None:
    """Drop unknown navigation IDs without guessing aliases or changing plans."""
    if requirement_ledger is None:
        return
    known_ids = {item.id for item in requirement_ledger.items}
    for child in children:
        unknown = [
            requirement_id
            for requirement_id in child.related_requirement_ids
            if requirement_id not in known_ids
        ]
        if unknown:
            print(
                f"[DetailedPlan] Ignoring unknown requirement IDs on "
                f"'{child.class_name}': {sorted(set(unknown))}"
            )
        child.related_requirement_ids = list(
            dict.fromkeys(
                requirement_id
                for requirement_id in child.related_requirement_ids
                if requirement_id in known_ids
            )
        )


def _discard_unexpected_child_plans(
    *,
    target_name: str,
    expected_names: list[str],
    children: list[SimpleDetailedPlan],
) -> list[SimpleDetailedPlan]:
    """Drop child plans that cannot belong to this locked hierarchy level.

    The global plan already fixes the direct-child set.  A detailed-plan model
    sometimes repeats a grandchild beside its coupled parent; retaining that
    entry cannot change the hierarchy and only causes a needless repair loop.
    Missing, duplicated, or wrongly typed expected children remain validation
    errors and are deliberately not repaired here.
    """
    expected = set(expected_names)
    unexpected = [child.class_name for child in children if child.class_name not in expected]
    if unexpected:
        print(
            f"[DetailedPlan] Ignoring non-direct child plans on '{target_name}': "
            f"{unexpected}"
        )
    return [child for child in children if child.class_name in expected]


def _collect_invented_child_plan_errors(
    *,
    target_name: str,
    expected_names: list[str],
    children: list[SimpleDetailedPlan],
    global_plan: list[GlobalPlanNode],
) -> list[str]:
    """Report newly invented children while still tolerating repeated descendants.

    A weaker model may repeat a real grandchild at the current hierarchy level;
    that harmless duplicate is discarded by ``_discard_unexpected_child_plans``.
    A name absent from the global plan is different: silently dropping it can
    also silently drop the model's attempted repair (for example, an invented
    Logger that owns the only stdout operation).  Return that distinction to the
    model so it can move the responsibility onto one of the locked children.
    """

    expected = set(expected_names)
    known = {node.name for node in global_plan}
    invented = sorted(
        {
            child.class_name
            for child in children
            if child.class_name not in expected and child.class_name not in known
        }
    )
    if not invented:
        return []
    return [
        f"Invented direct child plans for '{target_name}': {invented}. The direct-child "
        f"set is locked to {expected_names}; do not add a Logger or other new module "
        "during detailed planning. Move any needed external_io or behavior from the "
        "invented child onto the existing child that actually produces the record."
    ]


def _fold_repeated_descendant_external_io(
    *,
    expected_names: list[str],
    children: list[SimpleDetailedPlan],
    global_plan: list[GlobalPlanNode],
) -> None:
    """Preserve IO obligations when a response repeats a real descendant.

    Detailed planning is level-local, but weaker models sometimes include a
    grandchild because that leaf is the concrete process-stream owner.  The
    hierarchy remains locked: copy only its external-IO obligations and related
    requirement links to the unique direct child subtree that contains it.  The
    descendant entry itself is still discarded afterwards.
    """

    topology = {node.name: node for node in global_plan}

    def descendants(name: str) -> set[str]:
        found: set[str] = set()
        start = topology.get(name)
        pending = list(start.children_names) if start is not None else []
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            node = topology.get(current)
            if node is not None:
                pending.extend(node.children_names)
        return found

    direct_descendants = {name: descendants(name) for name in expected_names}
    direct_by_name = {
        child.class_name: child
        for child in children
        if child.class_name in set(expected_names)
    }
    for child in children:
        if child.class_name in direct_by_name or child.class_name not in topology:
            continue
        carriers = [
            name
            for name, members in direct_descendants.items()
            if child.class_name in members and name in direct_by_name
        ]
        if len(carriers) != 1:
            continue
        carrier = direct_by_name[carriers[0]]
        existing_streams = {
            json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            for item in carrier.external_io
        }
        for stream in child.external_io:
            key = json.dumps(
                stream.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            )
            if key not in existing_streams:
                carrier.external_io.append(stream.model_copy(deep=True))
                existing_streams.add(key)
        carrier.related_requirement_ids = list(
            dict.fromkeys(
                carrier.related_requirement_ids + child.related_requirement_ids
            )
        )


def _validate_and_order_children(
    *,
    target_name: str,
    expected_names: list[str],
    children: list[SimpleDetailedPlan],
    global_plan: list[GlobalPlanNode],
    requirement_ledger: RequirementLedger | None = None,
) -> list[SimpleDetailedPlan]:
    _discard_unknown_child_requirement_links(children, requirement_ledger)
    _fold_repeated_descendant_external_io(
        expected_names=expected_names,
        children=children,
        global_plan=global_plan,
    )
    invented_errors = _collect_invented_child_plan_errors(
        target_name=target_name,
        expected_names=expected_names,
        children=children,
        global_plan=global_plan,
    )
    children = _discard_unexpected_child_plans(
        target_name=target_name,
        expected_names=expected_names,
        children=children,
    )
    errors = _collect_child_plan_errors(
        target_name=target_name,
        expected_names=expected_names,
        children=children,
        global_plan=global_plan,
        requirement_ledger=requirement_ledger,
    )
    errors.extend(invented_errors)
    if errors:
        raise ValueError("Detailed-plan validation errors:\n- " + "\n- ".join(errors))
    by_name = {child.class_name: child for child in children}
    return [by_name[name] for name in expected_names]

# ====== Generator ======

class DetailedPlanGenerator:
    """Single method: generate detailed plan for a model + simple plans for its children."""

    def __init__(
        self,
        model_id: dict[str, str],
        enable_schema_repair: bool = False,
        continue_with_locked_interfaces: bool = False,
        root_endpoint_audit_example: bool = False,
    ):
        self.model_id = model_id
        self.enable_schema_repair = enable_schema_repair
        self.continue_with_locked_interfaces = continue_with_locked_interfaces
        self.root_endpoint_audit_example = root_endpoint_audit_example

    def _get_model(self) -> str:
        if isinstance(self.model_id, dict):
            return self.model_id.get('strong', self.model_id.get('default', ''))
        return self.model_id

    def _fmt_global(self, plan: list[GlobalPlanNode]) -> str:
        lines = []
        for n in plan:
            ci = f" -> children: {', '.join(n.children_names)}" if n.children_names else " -> (atomic)"
            requirement_ids = ", ".join(n.related_requirement_ids) or "none"
            lines.append(
                f"- {n.name}: {n.description}{ci}; "
                f"related requirements: {requirement_ids}"
            )
        return "\n".join(lines)

    def _fmt_simple(self, plan: SimpleDetailedPlan) -> str:
        # Preserve the complete interface contract.  The earlier prose formatter
        # silently discarded every port protocol, even though the prompt asked
        # the child planner to inherit it exactly.
        return json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)

    def _fmt_detailed(self, plan: DetailedPlan) -> str:
        # Include model_init_args and full ProtocolSpec fields as structured
        # JSON so inheritance is lossless and unambiguous.
        return json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)

    @staticmethod
    def root_candidate_payload(result: PlanGenResult) -> dict:
        """Serialize a validated root result back into the root response shape."""

        detailed = result.detailed_plan
        specification = detailed.specification
        return {
            "detailed_plan": {
                "class_name": detailed.class_name,
                "model_type": detailed.model_type,
                "function": specification.function,
                "model_init_args": [
                    item.model_dump(mode="json")
                    for item in specification.model_init_args
                ],
                "input_ports": [
                    item.model_dump(mode="json") for item in specification.input_ports
                ],
                "output_ports": [
                    item.model_dump(mode="json") for item in specification.output_ports
                ],
            },
            "children_plans": [
                child.model_dump(mode="json") for child in result.children_plans
            ],
            "coupling_rules": list(detailed.coupling_rules),
        }

    def _validated_root_coupled_result(
        self,
        *,
        parsed: _RawCoupledResponse,
        target_name: str,
        requirements: str,
        global_plan: list[GlobalPlanNode],
        children_names: list[str],
        requirement_ledger: Optional[RequirementLedger],
    ) -> PlanGenResult:
        detailed = _make_detailed_coupled(
            parsed.detailed_plan, parsed.coupling_rules
        )
        children = [_make_simple(child) for child in parsed.children_plans]
        _discard_unknown_child_requirement_links(children, requirement_ledger)
        _fold_repeated_descendant_external_io(
            expected_names=children_names,
            children=children,
            global_plan=global_plan,
        )
        errors = _collect_raw_plan_errors(parsed)
        errors.extend(
            _collect_invented_child_plan_errors(
                target_name=target_name,
                expected_names=children_names,
                children=children,
                global_plan=global_plan,
            )
        )
        if detailed.class_name != target_name:
            errors.append(
                f"Expected class_name '{target_name}', got '{detailed.class_name}'"
            )
        errors.extend(
            _collect_child_plan_errors(
                target_name=target_name,
                expected_names=children_names,
                children=children,
                global_plan=global_plan,
                requirement_ledger=requirement_ledger,
            )
        )
        errors.extend(
            _collect_root_external_io_delegation_errors(requirements, children)
        )
        errors = list(dict.fromkeys(errors))
        if errors:
            raise ValueError(
                "Root-plan reconciliation validation errors:\n- "
                + "\n- ".join(errors)
            )
        by_name = {child.class_name: child for child in children}
        return PlanGenResult(
            detailed_plan=detailed,
            children_plans=[by_name[name] for name in children_names],
        )

    def reconcile_root_candidates(
        self,
        *,
        target_name: str,
        requirements: str,
        global_plan: list[GlobalPlanNode],
        children_names: list[str],
        candidates: list[PlanGenResult],
        requirement_ledger: Optional[RequirementLedger] = None,
        retry: int = 2,
        repair_feedback: str = "",
    ) -> PlanGenResult:
        """Reconcile independently valid root drafts in one bounded LLM pass."""

        if not candidates:
            raise ValueError("At least one root-plan candidate is required")
        if not children_names:
            raise ValueError("Root-plan reconciliation is only for coupled roots")

        relevant_ids = requirement_ids_for_subtree(global_plan, target_name)
        requirement_focus_str = format_requirement_focus(
            requirement_ledger, relevant_ids, target_name=target_name
        )
        requirement_flow_str = format_requirement_flow_focus(
            requirement_ledger, relevant_ids
        )
        candidate_payloads = [
            self.root_candidate_payload(candidate) for candidate in candidates
        ]
        prompt = _build_root_reconciliation_prompt(
            target_name=target_name,
            requirements=requirements,
            requirement_focus_str=requirement_focus_str,
            requirement_flow_str=requirement_flow_str,
            global_plan_str=self._fmt_global(global_plan),
            children_names=children_names,
            candidate_payloads=candidate_payloads,
            include_endpoint_audit_example=self.root_endpoint_audit_example,
            repair_feedback=repair_feedback,
        )
        model = self._get_model()
        max_attempts = max(1, min(retry, 2))
        last_error: Exception | None = None
        previous_candidate_json = ""
        validation_feedback = ""

        for attempt in range(max_attempts):
            active_prompt = prompt
            if validation_feedback and previous_candidate_json:
                active_prompt = _build_validation_repair_prompt(
                    requirements=requirements,
                    requirement_focus_str=requirement_focus_str,
                    previous_candidate_json=previous_candidate_json,
                    validation_feedback=validation_feedback,
                )
            try:
                response = completion_with_logging(
                    model=model,
                    messages=[{"role": "user", "content": active_prompt}],
                    phase="phase1b_root_plan_reconciliation",
                    target=target_name,
                    attempt=attempt,
                    temperature=0.2,
                    response_format=_RawCoupledResponse,
                )
                content = get_content_strict(response)
                try:
                    raw = extract_json(content)
                    raw = _normalize_root_coupled_response_layout(raw)
                    parsed = _RawCoupledResponse.model_validate(raw)
                except Exception as parse_error:
                    if not self.enable_schema_repair:
                        raise
                    repair = repair_schema_output(
                        model=model,
                        response_model=_RawCoupledResponse,
                        original_prompt=active_prompt,
                        original_output=content,
                        validation_error=parse_error,
                        phase="phase1b_root_plan_reconciliation",
                        target=target_name,
                        attempt=attempt,
                        extra_instructions=(
                            "Return only a complete reconciled root coupled plan "
                            "with detailed_plan, children_plans, and coupling_rules."
                        ),
                    )
                    if not repair.success:
                        raise ValueError(
                            "Schema repair failed for root-plan reconciliation: "
                            f"{repair.error}"
                        ) from parse_error
                    parsed = repair.parsed
                previous_candidate_json = parsed.model_dump_json(indent=2)
                return self._validated_root_coupled_result(
                    parsed=parsed,
                    target_name=target_name,
                    requirements=requirements,
                    global_plan=global_plan,
                    children_names=children_names,
                    requirement_ledger=requirement_ledger,
                )
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)[:4_000]
                if _is_non_retryable_provider_error(validation_feedback):
                    break
                print(
                    f"[RootPlanReconciliation] Attempt {attempt + 1} failed "
                    f"for '{target_name}': {exc}"
                )
                if attempt < max_attempts - 1:
                    time.sleep(2)

        raise RuntimeError(
            f"Failed to reconcile root plan for '{target_name}' after at most "
            f"{max_attempts} attempts; last error: {last_error}"
        ) from last_error

    def generate(
        self,
        target_name: str,
        requirements: str,
        global_plan: list[GlobalPlanNode],
        children_names: list[str],
        parent_simple_plan: Optional[SimpleDetailedPlan] = None,
        parent_detailed_plan: Optional[DetailedPlan] = None,
        retry: int = 3,
        requirement_ledger: Optional[RequirementLedger] = None,
        sibling_simple_plans: Optional[list[SimpleDetailedPlan]] = None,
        plan_repair_feedback: str = "",
        planning_perspective: str = "",
    ) -> PlanGenResult:
        is_root = parent_simple_plan is None
        is_coupled = len(children_names) > 0  # 动态判断节点类型

        gstr = self._fmt_global(global_plan)
        cstr = ", ".join(children_names) if children_names else "None (leaf)"
        pstr = self._fmt_simple(parent_simple_plan) if parent_simple_plan else "(N/A - root)"
        dstr = self._fmt_detailed(parent_detailed_plan) if parent_detailed_plan else "(N/A - root)"
        sibling_contracts_str = json.dumps(
            [item.model_dump(mode="json") for item in (sibling_simple_plans or [])],
            ensure_ascii=False,
            indent=2,
        )
        relevant_requirement_ids = requirement_ids_for_subtree(global_plan, target_name)
        if parent_simple_plan is not None:
            relevant_requirement_ids = list(
                dict.fromkeys(
                    relevant_requirement_ids
                    + parent_simple_plan.related_requirement_ids
                )
            )
        requirement_focus_str = format_requirement_focus(
            requirement_ledger,
            relevant_requirement_ids,
            target_name=target_name,
        )
        requirement_flow_str = format_requirement_flow_focus(
            requirement_ledger, relevant_requirement_ids
        )
        if is_coupled and requirement_ledger is not None:
            topology = {node.name: node for node in global_plan}
            child_assignments = {
                child_name: topology[child_name].related_requirement_ids
                for child_name in children_names
            }
            requirement_focus_str += (
                "\n\nDirect-child relevance map (inclusive, shared IDs are expected):\n"
                + json.dumps(child_assignments, ensure_ascii=False, indent=2)
            )

        model = self._get_model()

        # A non-root response cannot restate its inherited interface. Its schema
        # contains only the fields that this expansion is allowed to decide.
        if is_root:
            ResponseModel = _RawCoupledResponse if is_coupled else _RawAtomicDetailed
        else:
            ResponseModel = (
                _RawInheritedCoupledResponse
                if is_coupled
                else _RawInheritedAtomicExpansion
            )

        max_attempts = max(1, min(retry, 3))
        last_error: Exception | None = None
        validation_feedback = ""
        previous_candidate_json = ""
        previous_semantic_errors: tuple[str, ...] | None = None
        observed_validation_errors: list[str] = []
        for attempt in range(max_attempts):
            stop_after_error = False
            try:
                if validation_feedback and previous_candidate_json:
                    prompt = _build_validation_repair_prompt(
                        requirements=requirements,
                        requirement_focus_str=requirement_focus_str,
                        requirement_flow_str=requirement_flow_str,
                        previous_candidate_json=previous_candidate_json,
                        validation_feedback=validation_feedback,
                        previously_observed_errors=observed_validation_errors[:-1],
                    )
                else:
                    prompt = _build_prompt(
                        target_name=target_name,
                        requirements=requirements,
                        global_plan_str=gstr,
                        children_names_str=cstr,
                        parent_simple_str=pstr,
                        parent_detail_str=dstr,
                        sibling_contracts_str=sibling_contracts_str,
                        is_root=is_root,
                        is_coupled=is_coupled, # 将类型传入，用于隔离 Prompt
                        requirement_focus_str=requirement_focus_str,
                        requirement_flow_str=requirement_flow_str,
                        plan_repair_feedback=plan_repair_feedback,
                        planning_perspective=planning_perspective,
                    )

                resp = completion_with_logging(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    phase=f"phase1b_detailed_plan_{'coupled' if is_coupled else 'atomic'}",
                    target=target_name,
                    attempt=attempt,
                    temperature=0.0 if validation_feedback else 0.5,
                    response_format=ResponseModel, # 动态传入精确 Schema
                )

                content = get_content_strict(resp)
                try:
                    raw = extract_json(content)
                    if is_root and is_coupled:
                        raw = _normalize_root_coupled_response_layout(raw)
                    parsed = ResponseModel.model_validate(raw)
                except Exception as parse_error:
                    if not self.enable_schema_repair:
                        raise
                    if is_root and is_coupled:
                        repair_instruction = (
                            "Preserve only detailed_plan, children_plans, and coupling_rules."
                        )
                    elif is_coupled:
                        repair_instruction = (
                            "Preserve only function, children_plans, coupling_rules, "
                            "and interface_change_requests; "
                            "do not restate the inherited current interface."
                        )
                    elif is_root:
                        repair_instruction = "Preserve the complete root atomic plan."
                    else:
                        repair_instruction = (
                            "Preserve only function, implementation_example, and "
                            "interface_change_requests; do not restate the inherited interface."
                        )
                    repair = repair_schema_output(
                        model=model,
                        response_model=ResponseModel,
                        original_prompt=prompt,
                        original_output=content,
                        validation_error=parse_error,
                        phase=f"phase1b_detailed_plan_{'coupled' if is_coupled else 'atomic'}",
                        target=target_name,
                        attempt=attempt,
                        extra_instructions=(
                            "This is a DEVS detailed-plan schema repair. "
                            + repair_instruction
                        ),
                    )
                    if not repair.success:
                        raise ValueError(
                            f"Schema repair failed after parse/validation error: {parse_error}; "
                            f"repair_error={repair.error}"
                        ) from parse_error
                    parsed = repair.parsed

                previous_candidate_json = parsed.model_dump_json(indent=2)

                interface_requests = _discard_satisfied_interface_requests(
                    list(getattr(parsed, "interface_change_requests", [])),
                    parent_simple_plan,
                )
                if interface_requests:
                    if self.continue_with_locked_interfaces:
                        request_text = json.dumps(
                            [
                                item.model_dump(mode="json")
                                for item in interface_requests
                            ],
                            ensure_ascii=False,
                        )
                        print(
                            f"[DetailedPlan] Continuing '{target_name}' with its "
                            "locked inherited interface; unapplied requests: "
                            f"{request_text}"
                        )
                    else:
                        raise InterfaceChangeRequired(
                            [
                                InterfaceChangeIssue(
                                    target_name=target_name, request=request
                                )
                                for request in interface_requests
                            ]
                        )

                if is_root and is_coupled:
                    assert isinstance(parsed, _RawCoupledResponse)
                    det = _make_detailed_coupled(
                        parsed.detailed_plan, parsed.coupling_rules
                    )
                    chs = [_make_simple(c) for c in getattr(parsed, "children_plans", [])]
                elif is_root:
                    assert isinstance(parsed, _RawAtomicDetailed)
                    det = _make_detailed_atomic(parsed)
                    chs = []
                elif is_coupled:
                    assert parent_simple_plan is not None
                    assert isinstance(parsed, _RawInheritedCoupledResponse)
                    det = _make_inherited_coupled(parent_simple_plan, parsed)
                    chs = [_make_simple(c) for c in parsed.children_plans]
                else:
                    assert parent_simple_plan is not None
                    assert isinstance(parsed, _RawInheritedAtomicExpansion)
                    det = _make_inherited_atomic(parent_simple_plan, parsed)
                    chs = []

                _discard_unknown_child_requirement_links(chs, requirement_ledger)
                if is_coupled and children_names:
                    _fold_repeated_descendant_external_io(
                        expected_names=children_names,
                        children=chs,
                        global_plan=global_plan,
                    )
                invented_child_errors: list[str] = []
                if is_coupled and children_names:
                    invented_child_errors = _collect_invented_child_plan_errors(
                        target_name=target_name,
                        expected_names=children_names,
                        children=chs,
                        global_plan=global_plan,
                    )
                    chs = _discard_unexpected_child_plans(
                        target_name=target_name,
                        expected_names=children_names,
                        children=chs,
                    )

                semantic_errors = _collect_raw_plan_errors(parsed)
                semantic_errors.extend(invented_child_errors)
                if det.class_name != target_name:
                    semantic_errors.append(
                        f"Expected class_name '{target_name}', got '{det.class_name}'"
                    )
                if is_coupled and children_names:
                    semantic_errors.extend(_collect_child_plan_errors(
                        target_name=target_name,
                        expected_names=children_names,
                        children=chs,
                        global_plan=global_plan,
                        requirement_ledger=requirement_ledger,
                    ))
                    if is_root:
                        semantic_errors.extend(
                            _collect_root_external_io_delegation_errors(
                                requirements, chs
                            )
                        )

                semantic_errors = list(dict.fromkeys(semantic_errors))
                if semantic_errors:
                    signature = tuple(semantic_errors)
                    stop_after_error = (
                        previous_semantic_errors is not None
                        and signature == previous_semantic_errors
                    )
                    previous_semantic_errors = signature
                    suffix = (
                        "\nNo validation progress was made from the previous candidate."
                        if stop_after_error
                        else ""
                    )
                    raise ValueError(
                        "Detailed-plan validation errors:\n- "
                        + "\n- ".join(semantic_errors)
                        + suffix
                    )

                if is_coupled and children_names:
                    by_name = {child.class_name: child for child in chs}
                    chs = [by_name[name] for name in children_names]

                print(f"[DetailedPlan] {target_name}: type={det.model_type}, children={len(chs)}")
                return PlanGenResult(detailed_plan=det, children_plans=chs)

            except InterfaceChangeRequired:
                # Retrying only the child would encourage it to hide a real
                # parent-contract deficiency. Bubble this up for one whole-plan
                # regeneration with the explicit request as feedback.
                raise
            except Exception as e:
                last_error = e
                es = str(e)
                validation_feedback = es[:4_000]
                if (
                    validation_feedback.startswith("Detailed-plan validation errors:")
                    and validation_feedback not in observed_validation_errors
                ):
                    observed_validation_errors.append(validation_feedback)
                if _is_non_retryable_provider_error(es):
                    print(
                        f"[DetailedPlan] Non-retryable provider rejection for "
                        f"'{target_name}': {e}"
                    )
                    break
                if "rate" in es.lower() or "429" in es:
                    wait = 10 * (attempt + 1)
                    print(f"[DetailedPlan] Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                print(f"[DetailedPlan] Attempt {attempt + 1} failed for '{target_name}': {e}")
                if stop_after_error:
                    break
                if attempt < max_attempts - 1:
                    time.sleep(2)

        raise RuntimeError(
            f"Failed to generate plan for '{target_name}' after at most {max_attempts} attempts; "
            f"last error: {last_error}"
        ) from last_error
