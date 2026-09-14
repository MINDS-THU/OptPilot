import json
import keyword
import re
import litellm
from pydantic import BaseModel, Field

litellm.drop_params = True

from ...base_types import GlobalPlanNode, RequirementLedger
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging
from .requirement_ledger import format_requirement_ledger


class GlobalPlanResponse(BaseModel):
    modules: list[GlobalPlanNode]


def complete_root_requirement_links(
    modules: list[GlobalPlanNode], requirement_ids: list[str]
) -> list[GlobalPlanNode]:
    """Make the root's system-wide relevance explicit without another LLM retry.

    The root denotes the complete system, so every project requirement is
    relevant to planning its subtree. Descendant links remain the architect's
    inclusive relevance judgments and are not inferred here.
    """
    if not modules:
        return modules
    allowed = set(requirement_ids)
    # Requirement IDs are navigation metadata, not executable semantics.  A
    # model occasionally drops a zero (for example R011 -> R11); guessing an
    # alias would be unsafe, but rejecting an otherwise usable architecture is
    # also disproportionate.  Discard only the unknown association and retain
    # the authoritative IDs below on the root.
    for module in modules:
        unknown = [
            requirement_id
            for requirement_id in module.related_requirement_ids
            if requirement_id not in allowed
        ]
        if unknown:
            print(
                f"[GlobalPlan] Ignoring unknown requirement IDs on "
                f"'{module.name}': {sorted(set(unknown))}"
            )
        module.related_requirement_ids = list(
            dict.fromkeys(
                requirement_id
                for requirement_id in module.related_requirement_ids
                if requirement_id in allowed
            )
        )
    root_ids = list(dict.fromkeys(modules[0].related_requirement_ids))
    root_seen = set(root_ids)
    root_ids.extend(
        requirement_id
        for requirement_id in requirement_ids
        if requirement_id not in root_seen
    )
    modules[0].related_requirement_ids = root_ids
    return modules


def complete_explicit_module_requirement_links(
    modules: list[GlobalPlanNode], requirement_ledger: RequirementLedger
) -> list[GlobalPlanNode]:
    """Attach a requirement to every module it names explicitly.

    The planner still decides broader relevance. This deterministic completion
    only prevents the lossy case where a shared flow names both endpoints but
    the model associates it with one endpoint alone.
    """

    def normalize(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.casefold())

    for module in modules[1:]:
        module_name = normalize(module.name)
        if len(module_name) < 4:
            continue
        linked = list(dict.fromkeys(module.related_requirement_ids))
        seen = set(linked)
        for item in requirement_ledger.items:
            explicit_roles = {
                normalize(role)
                for flow in item.flows
                for role in (flow.producer, flow.consumer)
            }
            if (
                module_name in normalize(item.detail)
                or any(
                    role in module_name or module_name in role
                    for role in explicit_roles
                )
            ) and item.id not in seen:
                linked.append(item.id)
                seen.add(item.id)
        module.related_requirement_ids = linked
    return modules


def validate_global_plan(
    modules: list[GlobalPlanNode],
    root_name: str,
    requirement_ids: set[str] | None = None,
) -> list[GlobalPlanNode]:
    """Reject ambiguous or disconnected module trees before detailed planning."""
    if not modules:
        raise ValueError("Global plan must contain at least the root module")
    names = [module.name for module in modules]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"Duplicate module names: {duplicates}")
    invalid = sorted(
        name for name in names if not name.isidentifier() or keyword.iskeyword(name)
    )
    if invalid:
        raise ValueError(f"Invalid Python module/class names: {invalid}")
    if modules[0].name != root_name:
        raise ValueError(f"First module must be '{root_name}', got '{modules[0].name}'")

    known = set(names)
    parent_count = {name: 0 for name in names}
    children_by_name: dict[str, list[str]] = {}
    for module in modules:
        children = module.children_names
        if len(children) != len(set(children)):
            raise ValueError(f"Module '{module.name}' lists a child more than once")
        if module.name in children:
            raise ValueError(f"Module '{module.name}' cannot be its own child")
        unknown = sorted(set(children) - known)
        if unknown:
            raise ValueError(
                f"Children referenced by '{module.name}' are missing: {unknown}"
            )
        children_by_name[module.name] = list(children)
        for child in children:
            parent_count[child] += 1

    if parent_count[root_name] != 0:
        raise ValueError(f"Root module '{root_name}' cannot have a parent")
    bad_parents = {
        name: count
        for name, count in parent_count.items()
        if name != root_name and count != 1
    }
    if bad_parents:
        raise ValueError(
            "Every non-root module must have exactly one parent; "
            f"got {bad_parents}"
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"Global plan contains a cycle through '{name}'")
        if name in visited:
            return
        visiting.add(name)
        for child in children_by_name[name]:
            visit(child)
        visiting.remove(name)
        visited.add(name)

    visit(root_name)
    disconnected = sorted(known - visited)
    if disconnected:
        raise ValueError(f"Global plan has disconnected modules: {disconnected}")

    if requirement_ids is not None:
        assigned = {
            requirement_id
            for module in modules
            for requirement_id in module.related_requirement_ids
        }
        unknown_ids = sorted(assigned - requirement_ids)
        missing_ids = sorted(requirement_ids - assigned)
        if unknown_ids:
            raise ValueError(
                f"Global plan references unknown requirement IDs: {unknown_ids}"
            )
        if missing_ids:
            raise ValueError(
                f"Global plan dropped requirement IDs: {missing_ids}"
            )
    return modules


GLOBAL_PLAN_PROMPT = """
## [Role]
You are a **DEVS System Architect**. Your task is to design the overall module hierarchy for a DEVS simulation system.

## [Input]
**System Name**: `{root_name}`
**Requirements**:
{requirements}

## [Requirement Ledger]
This is a navigation aid over the unabridged requirements above. Every ID must
remain associated with at least one relevant module. It does not replace or
override the original text.
{requirement_ledger}

## [Task]
Decompose the system into a hierarchical module structure. Return a list of modules.

## [Rules]
Make these boundary decisions before minimizing the module count:
- Command-line arguments are parsed by the generated `run.py` entry point and
  passed through the root constructor. Do not create an InputHandler atomic,
  configuration ports, or startup messages solely to transport CLI values.
- The generated runtime requires the root to be coupled. When the complete
  behavior is one coherent state machine, give the root exactly one atomic
  child; do not split CLI handling or final printing into extra children.
- If the request reads stdin, exactly one leaf description must explicitly say
  that it directly reads stdin. When stdin contains future event times, do not
  merge that reader into an atomic model that also schedules service, timeout,
  or other business events; use a small source atomic.
- For ordinary per-event JSONL, every leaf that produces a required record must
  say that it writes that record. Do not move all output to the stdin reader or
  invent a central writer unless aggregation is explicitly required.
- If stdout must be exactly one combined JSON document containing facts from
  several models, include one collector leaf. Producers send their structured
  facts to it through DEVS ports; only the collector writes stdout after the
  simulation. Reading the input does not by itself make a source the collector.

1. The first module MUST be the coupled root system `{root_name}` and must have
   at least one child. Use multiple children only for genuinely interacting
   state machines; otherwise use one coherent atomic child.
2. Every name mentioned in any `children_names` MUST appear as a `name` somewhere in the list.
3. Use hierarchical decomposition only when it clarifies the model. Prefer a shallow structure when a process can be implemented coherently by one atomic model or a small number of atomic models.
4. Keep descriptions SHORT (1-2 sentences). State what the module does, its key
   message handoffs, and any OS operation the module directly performs. Avoid
   the ambiguous phrase "emits an event": say either "sends a DEVS message to
   <module>", "writes a record to stdout", or explicitly say that it does both.
5. Module names should be valid Python class names (PascalCase, no spaces/special chars).
6. Atomic (leaf) modules should have `children_names: []`.
7. Do NOT over-decompose. 3-4 levels of hierarchy is usually sufficient, but the optimal number depends on the complexity of the system.
8. Except for the required root wrapper, do not introduce a coupled model only
   to satisfy a shape preference. If a subsystem has a single coherent state
   machine, make it atomic instead of splitting it into artificial
   controller/pool/interface modules.
9. DEVS Principles:
  - A system is hierarchical.
  - Atomic models have behavior but NO sub-components.
  - Coupled models have sub-components and routing (couplings) but NO behavior.
  - A child output may connect to a sibling input (IC) or to its current coupled
    boundary (EOC). A current boundary input may connect to a child input (EIC).
  - Mention required inter-module handoffs in descriptions; detailed planning
    will choose the exact port names.
10. If requirements specify multiplicity (e.g., count=N or count='arg:x'), the architecture MUST preserve that multiplicity at runtime. Represent the repeated entity as one atomic blueprint module whose parent instantiates it repeatedly; do not collapse all instances into one representative atomic model.
11. Requirement continuity: The root represents the complete system, so its
    `related_requirement_ids` should contain every supplied ledger ID. Put each
    ledger ID in `related_requirement_ids` for
    every module whose planning or implementation needs that requirement. This
    is an inclusive relevance association, not an assignment of OS operations: one ID may
    appear on several cooperating modules. Every supplied ID must appear at
    least once, and no new IDs may be invented.

## [DEVS messages and OS IO]
Keep these mechanisms separate:
1. `stdin`, `stdout`, `stderr`, files, and services are OS interactions performed
   directly by a leaf atomic model. They are recorded later in `external_io`.
2. Data needed by another DEVS model travels through a DEVS output port and a
   coupling. stdout and files are never parent ports or EOC targets.

## [IMPORTANT: KEEP IT SIMPLE]
- **Minimize the number of modules**: Only create modules that are truly necessary.
- **Prefer shallow hierarchies**: A flat structure with fewer levels is better than a deep one.
- **Each module should have a clear, distinct responsibility**: Avoid overlapping functions.
- **Atomic modules should be self-contained**: Each should implement a complete, coherent piece of functionality.
- **Keep tightly coupled state together**: Queues, resources, and timeout rules that must be updated in one consistent event order should stay in the same atomic model unless the requirements clearly describe separate interacting subsystems.

## [Field Guidance]
- `name`: Valid Python identifier, PascalCase. Example: "EventSource", "CoreProcessor". One name per module, unique across the entire list.
- `description`: 1-2 sentences stating what the module does, the messages it
  exchanges with other modules, and any OS operation it directly performs.
  Name the DEVS recipient of a message and distinguish that message from a
  stdout record. Do not invent exact port names at this stage.
- `children_names`: List of child module names. Empty list `[]` for atomic (leaf) modules.
- `related_requirement_ids`: Ledger IDs relevant to this module. Use `[]` only
  when no ledger is supplied or no listed requirement concerns the module.

## [Example]
For a system "SmartFactory" with requirements "read orders from stdin, process components, and write event records to stdout":
- modules (illustrative IDs):
  - name: "SmartFactory", description: "Top-level coupled model. Routes orders through assembly and quality checking.", children_names: ["OrderSource", "AssemblyRobot", "QualityChecker"], related_requirement_ids: ["R001", "R002", "R003"]
  - name: "OrderSource", description: "The only atomic model that reads stdin. It sends parsed orders to AssemblyRobot and writes the message records it produces to stdout.", children_names: [], related_requirement_ids: ["R001", "R003"]
  - name: "AssemblyRobot", description: "Atomic model. It assembles received orders, sends finished parts to QualityChecker, and writes its own state and sent-message records to stdout.", children_names: [], related_requirement_ids: ["R002", "R003"]
  - name: "QualityChecker", description: "Atomic model. It checks finished parts and writes its own state and result records to stdout.", children_names: [], related_requirement_ids: ["R002", "R003"]

For a timestamped stdin schedule that drives periodic state observations, use
one source and one state process; the state process that creates each record
writes it directly:
- name: "ScheduleSystem", description: "Coupled root. Routes scheduled values to the state process.", children_names: ["ScheduleSource", "StateProcess"], related_requirement_ids: ["R001", "R002", "R003"]
- name: "ScheduleSource", description: "The only atomic model that reads the timestamped stdin schedule. It sends the applicable value at every required observation time.", children_names: [], related_requirement_ids: ["R001", "R002"]
- name: "StateProcess", description: "Atomic model. Updates its retained state from each scheduled value and writes the observation it produces to stdout.", children_names: [], related_requirement_ids: ["R002", "R003"]
There is no separate writer because no cross-model aggregation is required.

For a multi-stage workflow whose only stdout is one combined final document,
make aggregation explicit rather than letting each stage print JSONL:
- name: "AccessSystem", description: "Coupled root. Routes requests through the processing pipeline and routes observable facts to one final report collector.", children_names: ["RequestSource", "AccessPipeline", "ReportCollector"], related_requirement_ids: ["R001", "R002", "R003"]
- name: "RequestSource", description: "The only atomic model that reads the request stream. It sends each parsed request to AccessPipeline and sends the corresponding input observation to ReportCollector.", children_names: [], related_requirement_ids: ["R001", "R002"]
- name: "AccessPipeline", description: "Atomic model. Applies the ordered delays and busy-state rules, sends each resulting event and operation fact to ReportCollector, and does not write stdout.", children_names: [], related_requirement_ids: ["R002", "R003"]
- name: "ReportCollector", description: "Atomic model. Collects event and operation facts, derives the final summary fields, and writes exactly one combined JSON object to stdout when the simulation exits.", children_names: [], related_requirement_ids: ["R003"]

For a CLI-configured model with one tightly coupled numeric state and one final
record, keep only a structural root and one stateful child:
`[{{"name":"PopulationSystem","description":"Coupled root. Contains the complete
population process.","children_names":["PopulationProcess"],
"related_requirement_ids":["R001","R002"]}},
{{"name":"PopulationProcess","description":"Atomic model. Advances the complete
state from constructor parameters and writes the required final record once.",
"children_names":[],"related_requirement_ids":["R001","R002"]}}]`.
A final record produced from this child's own state is not cross-model
aggregation. Do not add an InputHandler, OutputHandler, configuration-message
port, or final-state port around it.

For a configurable family of independent workers coordinated by one dispatcher,
keep the repeated model as an atomic blueprint directly under the coupled root:
- name: "FleetSystem", description: "Coupled root. Creates `num_workers` Worker instances and routes tagged assignments and availability reports.", children_names: ["Dispatcher", "Worker"], related_requirement_ids: ["R001", "R002"]
- name: "Dispatcher", description: "Atomic model. Selects an available worker and sends a tagged assignment.", children_names: [], related_requirement_ids: ["R001"]
- name: "Worker", description: "Atomic blueprint instantiated `num_workers` times by FleetSystem, with a stable worker ID; accepts only assignments addressed to that ID.", children_names: [], related_requirement_ids: ["R001", "R002"]
Do not insert a leaf `WorkerPool` that claims to contain Worker instances: a
module that actually contains child instances is coupled and must list its child
blueprint in `children_names`.
"""


class GlobalPlanGenerator:
    """生成全局初步计划：单次LLM调用，返回扁平list，然后解析为树结构"""

    def __init__(self, model_id: str = "gpt-4o"):
        self.model_id = model_id

    def forward(
        self,
        root_name: str,
        requirements: str,
        retry: int = 3,
        requirement_ledger: RequirementLedger | None = None,
    ) -> list[GlobalPlanNode]:
        """
        Generate the global plan in a single LLM call.
        Returns a list of GlobalPlanNode.
        """
        ledger_text = (
            format_requirement_ledger(requirement_ledger)
            if requirement_ledger is not None
            else "(No ledger supplied.)"
        )
        ordered_requirement_ids = (
            [item.id for item in requirement_ledger.items]
            if requirement_ledger is not None
            else None
        )
        requirement_ids = (
            set(ordered_requirement_ids)
            if ordered_requirement_ids is not None
            else None
        )
        prompt = GLOBAL_PLAN_PROMPT.format(
            root_name=root_name,
            requirements=requirements,
            requirement_ledger=ledger_text,
        )

        previous_output = ""
        validation_feedback = ""
        for attempt in range(retry):
            response = None
            try:
                messages = [{"role": "user", "content": prompt}]
                if previous_output and validation_feedback:
                    messages.extend(
                        [
                            {"role": "assistant", "content": previous_output},
                            {
                                "role": "user",
                                "content": (
                                    "The preceding global-plan candidate was rejected by "
                                    "the deterministic validator:\n"
                                    f"{validation_feedback}\n"
                                    "Return the complete corrected JSON object. Preserve "
                                    "valid architecture decisions and correct the stated "
                                    "defect exactly."
                                ),
                            },
                        ]
                    )
                response = completion_with_logging(
                    model=self.model_id,
                    messages=messages,
                    phase="phase1a_global_plan",
                    target=root_name,
                    attempt=attempt,
                    temperature=0.0 if validation_feedback else 0.5,
                    response_format=GlobalPlanResponse,
                )
                raw_content = get_content_strict(response)

                # Validate through pydantic
                parsed = GlobalPlanResponse.model_validate_json(raw_content)
                modules = parsed.modules
                if ordered_requirement_ids is not None:
                    modules = complete_root_requirement_links(
                        modules, ordered_requirement_ids
                    )
                    assert requirement_ledger is not None
                    modules = complete_explicit_module_requirement_links(
                        modules, requirement_ledger
                    )
                modules = validate_global_plan(
                    modules, root_name, requirement_ids=requirement_ids
                )

                print(f"[GlobalPlan] Generated {len(modules)} modules")
                return modules

            except Exception as e:
                print(f"[GlobalPlan] Attempt {attempt + 1} failed: {e}")
                if response is not None:
                    try:
                        previous_output = get_content_strict(response)
                    except Exception:
                        previous_output = ""
                validation_feedback = str(e)
                # Fallback to manual extraction if response_format fails
                try:
                    if response is None:
                        raise ValueError("No model response was available for fallback parsing")
                    raw_content = get_content_strict(response)
                    plan_list = self._extract_json_list(raw_content)
                    modules = [GlobalPlanNode.model_validate(m) for m in plan_list]
                    if ordered_requirement_ids is not None:
                        modules = complete_root_requirement_links(
                            modules, ordered_requirement_ids
                        )
                        assert requirement_ledger is not None
                        modules = complete_explicit_module_requirement_links(
                            modules, requirement_ledger
                        )
                    modules = validate_global_plan(
                        modules,
                        root_name,
                        requirement_ids=requirement_ids,
                    )

                    print(f"[GlobalPlan] Generated {len(modules)} modules (fallback)")
                    return modules
                except Exception:
                    continue

        raise Exception(f"Failed to generate global plan after {retry} attempts")

    def _extract_json_list(self, content: str) -> list:
        """Extract a JSON list from LLM response, handling markdown fences and surrounding text."""
        content = content.strip()

        # Try direct parse
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # Try stripping markdown code fences
        import re
        fence_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', content, re.DOTALL)
        if fence_match:
            try:
                data = json.loads(fence_match.group(1))
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        # Try finding the first '[' and last ']'
        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start:end+1])
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract a valid JSON list from response. Content preview: {content[:200]}")
